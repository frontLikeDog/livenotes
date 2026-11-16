# -*- coding: utf-8 -*-
"""周报：调度、落库、通知。

生成器在 ``app/report.py``（已有叙事、证据门、规则兜底）；这里只补三件事：
存 ``reports`` 表、周一生成、生成后通知一次。页面读结构化数据，Markdown
仍是导出物。

空周不生成、不通知；同一周只生成/通知一次（``(kind, period)`` 唯一键 +
``weekly:<period>`` 去重键）。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from . import logs, notify, report, settings, store
from .config import REPORTS_DIR

_LOCK = threading.RLock()
_WORKER: threading.Thread | None = None
_STOP = threading.Event()
_LAST_CHECK = 0.0


def week_range(period: str = "") -> tuple[date, date, str]:
    """period（2026-W46）-> (周一, 周日, period)；空 = 上一个完整周。"""
    period = str(period or "").strip()
    if period:
        try:
            year, wk = period.split("-W")
            monday = date.fromisocalendar(int(year), int(wk), 1)
            return monday, monday + timedelta(days=6), period
        except (ValueError, TypeError):
            pass
    since, until = report.last_week()
    return since, until, report.period_of(since)


def _summary_text(data: dict) -> str:
    try:
        return "%d 场 · %s · %d 段高光" % (
            int(data["stats"]["sessions"]),
            report.duration_text(data["stats"]["seconds"]),
            int(data["stats"]["highlights"]))
    except (KeyError, TypeError, ValueError):
        return "上周的笔记已经整理好了。"


def _count_sessions(since: date, until: date) -> int:
    conn = store.connect()
    try:
        return len(report.sessions_between(conn, since, until))
    finally:
        conn.close()


def build(period: str = "", use_llm: bool = True,
          notify_user: bool = False) -> dict:
    """生成（或复用）某周周报。返回 reports 行。"""
    since, until, period = week_range(period)
    with _LOCK:
        conn = store.connect()
        try:
            existing = store.report_get(conn, period=period)
            if existing and existing.get("state") == "ready" and \
                    (existing.get("data") or {}).get("sessions"):
                return existing
            if _count_sessions(since, until) <= 0:
                return store.report_upsert(
                    conn, "weekly", period, since.isoformat(), until.isoformat(),
                    state="empty", title="%s · %s" % (
                        report.KINDS["weekly"],
                        report.range_label(since, until)),
                    summary="这周还没有记录。", data={})
            store.report_upsert(
                conn, "weekly", period, since.isoformat(), until.isoformat(),
                state="running", error="")
        finally:
            conn.close()
        # report.build 需要连接：重新开一个，避免把连接关在上面的 finally 里。
        conn = store.connect()
        try:
            try:
                out = report.build(conn, kind="weekly", since=since, until=until,
                                   use_llm=use_llm)
            except Exception as e:
                traceback.print_exc()
                store.report_upsert(
                    conn, "weekly", period, since.isoformat(), until.isoformat(),
                    state="failed", error=str(e)[:200])
                raise
            data = report.structured(out, conn)
            row = store.report_upsert(
                conn, "weekly", period, since.isoformat(), until.isoformat(),
                state="ready", path=str(out.get("path") or ""),
                title=str(out.get("title") or ""),
                summary=_summary_text(data), data=data,
                generated_at=store.now())
        finally:
            conn.close()
    logs.log_event("weekly.ready", period=period,
                   sessions=int((data.get("stats") or {}).get("sessions") or 0))
    if notify_user:
        try:
            notify.weekly_ready(period, row.get("summary") or "",
                                since=since.isoformat(), until=until.isoformat())
        except Exception:
            traceback.print_exc()
    return row


def _should_notify() -> bool:
    """周三之后不再补发，只在周报页显示（docs/36 §7.3）。"""
    return date.today().weekday() <= 2


def ensure(period: str = "", notify_user: bool | None = None) -> dict | None:
    """启动/每日检查：有场次且没有 ready 周报就生成。"""
    if not settings.prefs().get("weekly_enabled", True):
        return None
    since, until, period = week_range(period)
    if _count_sessions(since, until) <= 0:
        conn = store.connect()
        try:
            store.report_upsert(
                conn, "weekly", period, since.isoformat(), until.isoformat(),
                state="empty", title="%s · %s" % (
                    report.KINDS["weekly"], report.range_label(since, until)),
                summary="这周还没有记录。", data={})
        finally:
            conn.close()
        return None
    conn = store.connect()
    try:
        row = store.report_get(conn, period=period)
        if row and row.get("state") == "ready":
            return row
    finally:
        conn.close()
    if notify_user is None:
        notify_user = _should_notify()
    return build(period, notify_user=notify_user)


def get(rid: int = 0, period: str = "") -> dict | None:
    conn = store.connect()
    try:
        row = store.report_get(conn, rid, period)
        if not row:
            # 旧文件降级：没有 reports 行时按文件名补一行
            if period:
                since, until, period = week_range(period)
                path = REPORTS_DIR / ("weekly_%s_%s.md" % (
                    since.isoformat(), until.isoformat()))
                if path.is_file():
                    row = store.report_upsert(
                        conn, "weekly", period, since.isoformat(),
                        until.isoformat(), state="ready", path=str(path),
                        title="%s · %s" % (report.KINDS["weekly"],
                                           report.range_label(since, until)),
                        summary="", data={}, generated_at=store.now())
        if row and not row.get("data"):
            p = Path(str(row.get("path") or ""))
            if p.is_file():
                try:
                    row["markdown"] = p.read_text(encoding="utf-8",
                                                  errors="replace")[:200_000]
                except OSError:
                    row["markdown"] = ""
        return row
    finally:
        conn.close()


def list_reports(limit: int = 12) -> list[dict]:
    conn = store.connect()
    try:
        rows = store.report_list(conn, "weekly", limit)
    finally:
        conn.close()
    for r in rows:
        r.pop("data", None)
    return rows


def migrate_old_files() -> int:
    """启动时扫描 REPORTS_DIR/weekly_*.md，补 reports 行（幂等）。"""
    if not REPORTS_DIR.is_dir():
        return 0
    n = 0
    conn = store.connect()
    try:
        for p in REPORTS_DIR.glob("weekly_*.md"):
            parts = p.stem.split("_")
            if len(parts) != 3:
                continue
            try:
                since = date.fromisoformat(parts[1])
                until = date.fromisoformat(parts[2])
            except ValueError:
                continue
            period = report.period_of(since)
            if store.report_get(conn, period=period):
                continue
            store.report_upsert(
                conn, "weekly", period, since.isoformat(), until.isoformat(),
                state="ready", path=str(p),
                title="%s · %s" % (report.KINDS["weekly"],
                                   report.range_label(since, until)),
                summary="", data={}, generated_at=store.now())
            n += 1
    finally:
        conn.close()
    return n


def _loop() -> None:
    global _LAST_CHECK
    # 启动后等 20 秒，让 migrations / 索引先跑完。
    if _STOP.wait(20.0):
        return
    while not _STOP.is_set():
        try:
            now = datetime.now()
            # 每天 09:00 之后检查一次；进程一直开着也不会重复生成。
            today9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= today9 and (time.time() - _LAST_CHECK) > 6 * 3600:
                _LAST_CHECK = time.time()
                ensure()
        except Exception:
            logs.log_event("weekly.tick_failed", level="warn")
        _STOP.wait(1800.0)


def start_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_loop, name="weekly", daemon=True)
        _WORKER.start()


def stop_worker() -> None:
    _STOP.set()
