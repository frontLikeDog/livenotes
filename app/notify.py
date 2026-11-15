# -*- coding: utf-8 -*-
"""应用内通知：引擎只负责产事件，壳负责决定用应用内提示还是系统 toast。

为什么通知要落库而不是直接推给壳：

- 壳可能正被收起、WebView 可能刚崩、用户可能整晚不在电脑前。事件先落库，
  壳回来时还能看到「昨晚断过 30 秒」「笔记已经生成」。
- 同一条事件会被看门狗每 10 秒检查一次，靠 ``dedupe_key`` 唯一索引做幂等，
  不依赖调用方自己记住"发过没有"。
- 系统通知和应用内通知中心展示同一行数据，不重复计数；点击动作也存
  在 ``action_json`` 里，壳重启后仍知道该跳哪里。

通知文案是产品文案，不是日志：不出现断流/采集/ASR/聚类/token 这类词，
失败时必须说明"录音和逐字稿还在"。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from typing import Any

from . import logs, settings, store
from .config import DATA_DIR

# 事件类型与设置页的开关一一对应。顺序就是设置页里的顺序。
KINDS: tuple[dict[str, str], ...] = (
    {"kind": "stream_stalled", "label": "听不到声音", "desc": "录音链路断开时提醒"},
    {"kind": "stream_recovered", "label": "声音恢复", "desc": "恢复后告知一声，不重复打扰"},
    {"kind": "silent", "label": "直播静音", "desc": "连续两分钟没有声音"},
    {"kind": "live_ended", "label": "直播结束", "desc": "自动停止记录并开始生成笔记"},
    {"kind": "note_ready", "label": "笔记生成", "desc": "结束记录后笔记已就绪"},
    {"kind": "note_failed", "label": "笔记失败", "desc": "生成失败，录音和逐字稿仍在"},
    {"kind": "disk_low", "label": "磁盘不足", "desc": "剩余空间可能不够继续录"},
    {"kind": "engine_error", "label": "记录程序异常", "desc": "本地引擎断开或重启"},
    {"kind": "tray_hint", "label": "托盘提示", "desc": "第一次收进托盘时说明"},
)
KIND_KEYS = tuple(k["kind"] for k in KINDS)
# 失败通知优先级更高，前端用不同颜色。
LEVELS = ("info", "warn", "error")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _action(view: str, *, session_id: str = "", room_id: str = "",
            follow_id: int = 0, t: float | None = None) -> dict:
    out: dict[str, Any] = {"view": view}
    if session_id:
        out["session_id"] = session_id
    if room_id:
        out["room_id"] = room_id
    if follow_id:
        out["follow_id"] = follow_id
    if t is not None:
        out["t"] = float(t)
    return out


def enabled(kind: str) -> bool:
    """总开关 + 单类开关。未知类型默认放行，方便以后加事件不丢通知。"""
    p = settings.prefs()
    if not p.get("notify_enabled", True):
        return False
    return kind not in set(p.get("notify_muted") or [])


def emit(kind: str, title: str, body: str = "", *,
         session_id: str = "", action: dict | None = None,
         dedupe_key: str = "", level: str = "info") -> dict | None:
    """写一条通知。``dedupe_key`` 相同只保留第一条，返回 None。

    返回值是完整行，方便调用方测试/日志；正常业务不需要等它。
    """
    if not enabled(kind):
        return None
    try:
        conn = store.connect()
    except Exception:
        # 通知不是业务主链路：库没初始化/被占用时宁可少一条，也不能让看门狗崩。
        return None
    try:
        created = _now()
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications"
            " (kind, level, session_id, title, body, action_json,"
            "  dedupe_key, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (kind, level if level in LEVELS else "info", session_id or "",
             str(title or "")[:200], str(body or "")[:600],
             _json(action or _action("hud", session_id=session_id)),
             dedupe_key or "", created))
        conn.commit()
        if cur.rowcount <= 0:
            return None
        row = conn.execute(
            "SELECT * FROM notifications WHERE id=?", (cur.lastrowid,)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    out = _row(row)
    logs.log_event("notify.emit", session_id=session_id or "", kind=kind,
                   notification_id=out["id"], dedupe=dedupe_key or "")
    return out


def _row(row) -> dict:
    d = dict(row)
    try:
        d["action"] = json.loads(d.pop("action_json") or "{}")
    except (TypeError, ValueError):
        d["action"] = {}
    d["delivered"] = bool(d.get("delivered_at"))
    d["read"] = bool(d.get("read_at"))
    return d


def feed(since_id: int = 0, limit: int = 50) -> dict:
    """壳轮询用：``since_id`` 之后的新通知 + 未读数。

    ``since_id=0`` 返回最近一页（按时间正序），应用启动时用它把通知中心
    填充起来；但壳不会把这一页重新弹成系统 toast（见 shell 的 boot 逻辑）。
    """
    limit = max(1, min(100, int(limit or 50)))
    since = max(0, int(since_id or 0))
    conn = store.connect()
    try:
        if since:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE id>? ORDER BY id ASC LIMIT ?",
                (since, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            rows = list(reversed(rows))
        latest = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS n FROM notifications").fetchone()["n"]
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL"
        ).fetchone()["n"]
        unread_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM notifications WHERE read_at IS NULL"
            " ORDER BY id DESC LIMIT 99")]
    finally:
        conn.close()
    return {
        "items": [_row(r) for r in rows],
        "latest_id": int(latest or 0),
        "unread": int(unread or 0),
        "unread_ids": unread_ids,
        "settings": settings_view(),
        "server_time": _now(),
    }


def mark_delivered(ids: list[int] | None = None) -> int:
    """标记已展示。投递失败/应用重启后不再重播的依据。"""
    vals = [int(x) for x in (ids or []) if str(x).isdigit()]
    conn = store.connect()
    try:
        if vals:
            q = ",".join("?" * len(vals))
            cur = conn.execute(
                "UPDATE notifications SET delivered_at=? "
                "WHERE delivered_at IS NULL AND id IN (" + q + ")",
                [_now()] + vals)
        else:
            cur = conn.execute(
                "UPDATE notifications SET delivered_at=? WHERE delivered_at IS NULL",
                (_now(),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def mark_read(ids: list[int] | None = None, all_: bool = False) -> dict:
    vals = [int(x) for x in (ids or []) if str(x).isdigit()]
    conn = store.connect()
    try:
        now = _now()
        if all_:
            cur = conn.execute(
                "UPDATE notifications SET read_at=? WHERE read_at IS NULL", (now,))
        elif vals:
            q = ",".join("?" * len(vals))
            cur = conn.execute(
                "UPDATE notifications SET read_at=? "
                "WHERE read_at IS NULL AND id IN (" + q + ")", [now] + vals)
        else:
            cur = None
        conn.commit()
        unread = conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL"
        ).fetchone()["n"]
    finally:
        conn.close()
    return {"ok": True, "changed": cur.rowcount if cur is not None else 0,
            "unread": int(unread or 0)}


def clear() -> int:
    """只给测试/设置页的"清空通知"用；生产不主动删历史。"""
    conn = store.connect()
    try:
        cur = conn.execute("DELETE FROM notifications")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def settings_view() -> dict:
    p = settings.prefs()
    return {
        "enabled": bool(p.get("notify_enabled", True)),
        "system": bool(p.get("notify_system", True)),
        "sound": bool(p.get("notify_sound", True)),
        "muted": list(p.get("notify_muted") or []),
        "kinds": [dict(k) for k in KINDS],
    }


def update_settings(patch: dict) -> dict:
    """壳/设置页保存通知偏好。白名单在 settings.DEFAULT_PREFS 里。"""
    allowed = {k: patch[k] for k in
               ("notify_enabled", "notify_system", "notify_sound", "notify_muted")
               if k in (patch or {})}
    if allowed:
        settings.set_prefs(allowed)
    return settings_view()


# ---------------------------------------------------------------- 业务事件

def session_title(sid: str) -> str:
    if not sid:
        return "这场直播"
    try:
        conn = store.connect()
        try:
            row = store.get_session(conn, sid)
        finally:
            conn.close()
    except Exception:
        row = None
    return str((row or {}).get("title") or "这场直播")


def note_ready(sid: str, result: dict | None = None) -> dict | None:
    """自动收尾的总结任务成功。只有 auto 任务会调，手动点总结不打扰。"""
    out = result or {}
    title = str(out.get("session_title") or session_title(sid))
    points = out.get("points") if isinstance(out.get("points"), list) else []
    highlights = out.get("highlights") if isinstance(out.get("highlights"), list) else []
    bits = []
    if points:
        bits.append("%d 个要点" % len(points))
    if highlights:
        bits.append("%d 段高光" % len(highlights))
    body = " · ".join(bits) if bits else "笔记已生成，可以打开看看。"
    return emit(
        "note_ready", "《%s》的笔记已生成" % title, body,
        session_id=sid, level="info",
        action=_action("note", session_id=sid),
        dedupe_key="note_ready:" + sid)


def note_failed(sid: str, reason: str = "") -> dict | None:
    # 错误信息只取第一行、去掉 URL/Key 之类容易吓人的东西，再截短。
    why = str(reason or "").strip().splitlines()[0] if str(reason or "").strip() else ""
    why = why[:80]
    body = (why + "，录音和逐字稿都在。") if why else "录音和逐字稿都在，可以重试生成。"
    return emit(
        "note_failed", "笔记没生成成功", body,
        session_id=sid, level="warn",
        action=_action("note", session_id=sid),
        # 同一场可能重试多次：每次失败都该告知，不能只报第一次。
        dedupe_key="note_failed:%s:%s" % (sid, _now()))


def live_ended(sid: str, title: str = "", *, down: bool = False) -> dict | None:
    """看门狗自动收尾。

    ``down`` 为真表示「流还在、内容全零」，更像主播真的下播了；断流收尾
    不能说成「直播结束」——用户会以为主播自己关了，其实是浏览器或网络断了。
    """
    name = title or session_title(sid)
    if down:
        head = "直播可能结束了"
        body = "《%s》已停止记录，正在生成笔记。" % name
    else:
        head = "已停止记录"
        body = "《%s》很久没有收到声音，录音和逐字稿已保存，正在生成笔记。" % name
    return emit(
        "live_ended", head, body,
        session_id=sid, level="info",
        action=_action("hud", session_id=sid),
        dedupe_key="live_ended:" + sid)


def stream_stalled(sid: str, title: str, episode: int, idle_s: float = 0) -> dict | None:
    secs = max(1, int(round(idle_s or 30)))
    return emit(
        "stream_stalled", "听不到声音了",
        "《%s》已 %d 秒没有收到声音，可能断开了。" % (title or session_title(sid), secs),
        session_id=sid, level="warn",
        action=_action("hud", session_id=sid),
        dedupe_key="stream_stalled:%s:%d" % (sid, int(episode)))


def stream_recovered(sid: str, title: str, episode: int) -> dict | None:
    return emit(
        "stream_recovered", "声音恢复了",
        "《%s》继续在录。" % (title or session_title(sid)),
        session_id=sid, level="info",
        action=_action("hud", session_id=sid),
        dedupe_key="stream_recovered:%s:%d" % (sid, int(episode)))


def silent(sid: str, title: str, episode: int, zero_s: float = 0) -> dict | None:
    mins = max(1, int(round(float(zero_s or 120) / 60)))
    return emit(
        "silent", "直播可能静音了",
        "《%s》已经连续 %d 分钟没有声音。" % (title or session_title(sid), mins),
        session_id=sid, level="warn",
        action=_action("hud", session_id=sid),
        dedupe_key="silent:%s:%d" % (sid, int(episode)))


def tray_hint() -> dict | None:
    return emit(
        "tray_hint", "已收进托盘，录音继续",
        "右键托盘图标可以暂停或结束。图标可能在任务栏右下角的 ^ 里。",
        level="info", action=_action("hud"),
        dedupe_key="tray_hint")


def test_notification() -> dict | None:
    """设置页的「发一条测试通知」。每次唯一，方便验证系统权限。"""
    stamp = datetime.now().strftime("%H%M%S%f")
    return emit(
        "test", "通知工作正常",
        "看到这条说明 LiveNotes 能把消息送到你面前。",
        level="info", action=_action("hud"),
        dedupe_key="test:" + stamp)


def disk_low(free_bytes: int) -> dict | None:
    gb = max(0.1, float(free_bytes) / (1024 ** 3))
    day = datetime.now().strftime("%Y-%m-%d")
    return emit(
        "disk_low", "磁盘空间不足",
        "还剩 %.1fGB，长直播可能存不下。可以清理旧录音或换存储位置。" % gb,
        level="warn", action=_action("storage"),
        dedupe_key="disk_low:" + day)


def check_disk(min_bytes: int | None = None) -> dict | None:
    """启动/看门狗调用。低于 2GB 发一次，同一天只发一次。"""
    threshold = min_bytes
    if threshold is None:
        threshold = 2 * 1024 ** 3
    try:
        free = shutil.disk_usage(str(DATA_DIR)).free
    except OSError:
        return None
    if free >= threshold:
        return None
    return disk_low(free)
