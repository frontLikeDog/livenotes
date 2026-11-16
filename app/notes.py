# -*- coding: utf-8 -*-
"""笔记聚合与检索索引。

P1 的「笔记页」不是一个新存储：总结、高光、书签、逐字稿都已经在库里，
这里只做三件事：

1. ``build_note``：把一场的所有可用内容聚成一个前端能直接渲染的结构；
2. ``derive_state``：没有 ``notes`` 行的历史场次也能给出状态；
3. ``index_session``：把总结/要点/高光/书签写进 ``note_units``，
   全库搜索和问笔记共用这一份索引。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import store


def _summary(conn: sqlite3.Connection, sid: str) -> dict:
    row = store.latest_summary(conn, sid, "content")
    if not row:
        rows = [dict(r) for r in conn.execute(
            "SELECT payload_json FROM summaries WHERE session_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (sid,))]
        return {}
    payload = row.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def _speaker_names(conn: sqlite3.Connection, sid: str) -> dict[str, str]:
    return {str(r["speaker_id"]): str(r["display_name"] or r["speaker_id"])
            for r in conn.execute(
                "SELECT speaker_id,display_name FROM speakers WHERE session_id=?",
                (sid,))}


def _duration(conn: sqlite3.Connection, sid: str) -> float:
    row = conn.execute("SELECT COALESCE(MAX(t_end),0) AS t FROM utterances "
                       "WHERE session_id=?", (sid,)).fetchone()
    return float(row["t"] or 0) if row else 0.0


def bookmarks(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """书签 = marks（全部）+ 人工/实时收藏的高光，按时间排序去重。"""
    out: list[dict] = []
    for r in conn.execute(
            "SELECT id,t,note,kind,created_at FROM marks WHERE session_id=? "
            "ORDER BY t", (sid,)):
        out.append({"id": "m%d" % int(r["id"]), "ref_id": str(r["id"]),
                    "t": float(r["t"] or 0), "note": str(r["note"] or ""),
                    "kind": str(r["kind"] or "mark"),
                    "source": "recording" if str(r["kind"] or "") != "bookmark"
                    else "reader",
                    "created_at": r["created_at"]})
    for r in conn.execute(
            "SELECT id,t_start,peak_t,text,quote,hook,kind,source,speaker_label "
            "FROM highlights WHERE session_id=? "
            "AND (kind='manual' OR source IN ('manual','live')) "
            "ORDER BY peak_t", (sid,)):
        t = float(r["peak_t"] or r["t_start"] or 0)
        if any(abs(float(b["t"]) - t) < 0.5 for b in out if b["kind"] != "mark"):
            continue
        out.append({"id": "h%d" % int(r["id"]), "ref_id": str(r["id"]),
                    "t": t, "note": str(r["quote"] or r["hook"] or r["text"] or ""),
                    "kind": "highlight", "source": str(r["source"] or "manual"),
                    "speaker": str(r["speaker_label"] or ""),
                    "created_at": ""})
    out.sort(key=lambda x: float(x.get("t") or 0))
    return out


def build_note(conn: sqlite3.Connection, sid: str) -> dict:
    """一场笔记的完整聚合。不存在返回 ``{"ok": False}``。"""
    s = store.get_session(conn, sid)
    if not s or s.get("deleted_at"):
        return {"ok": False, "error": "这篇笔记不存在"}
    summary = _summary(conn, sid)
    note = store.get_note(conn, sid)
    state, progress = store.derive_note_state(conn, sid, note)
    run = store.get_active_run(conn, sid)
    if run and state not in ("ready", "partial"):
        state = "running" if run.get("status") == "running" else "queued"
        progress = int(run.get("progress") or progress or 0)
    names = _speaker_names(conn, sid)
    host_id = str(summary.get("host_speaker") or "")
    streamer = names.get(host_id, "") if host_id else ""
    if not streamer:
        first = conn.execute(
            "SELECT display_name FROM speakers WHERE session_id=? "
            "ORDER BY speaker_id LIMIT 1", (sid,)).fetchone()
        streamer = str(first["display_name"] or "") if first else ""
    highlights = store.list_highlights(conn, sid, 20)
    # 收藏的排前面（先给人自己留的），再按分数。来源仍用 source 标明。
    highlights.sort(key=lambda h: (0 if str(h.get("source") or "") in
                                   ("manual", "live") else 1,
                                   -float(h.get("score") or 0)))
    marks = bookmarks(conn, sid)
    speakers = []
    for r in conn.execute(
            "SELECT sp.speaker_id, sp.display_name, COUNT(u.id) AS n, "
            "COALESCE(SUM(u.t_end-u.t_start),0) AS secs "
            "FROM speakers sp LEFT JOIN utterances u ON u.session_id=sp.session_id "
            "AND u.speaker_id=sp.speaker_id WHERE sp.session_id=? "
            "GROUP BY sp.speaker_id ORDER BY secs DESC", (sid,)):
        speakers.append({"speaker_id": r["speaker_id"],
                         "label": r["display_name"] or r["speaker_id"],
                         "n": int(r["n"] or 0),
                         "seconds": round(float(r["secs"] or 0), 1)})
    points = []
    for i, p in enumerate(summary.get("points") or []):
        ev = p.get("evidence") or []
        quote = ""
        if ev:
            quote = str(ev[0].get("text") or "")[:160]
        points.append({
            "i": i, "title": str(p.get("title") or ""),
            "gist": str(p.get("gist") or ""),
            "t_from": p.get("t_from"), "t_to": p.get("t_to"),
            "quote": quote, "speakers": p.get("speakers") or [],
            "evidence": [{"id": e.get("id"), "t": e.get("t_start"),
                          "text": str(e.get("text") or "")[:200],
                          "speaker": e.get("speaker_label") or ""}
                         for e in ev[:4]],
        })
    generated_at = (note or {}).get("generated_at") or summary.get("generated_at") \
        or (s.get("started_at") or "")
    rel = _related(conn, sid, summary.get("topics") or [], streamer)
    return {
        "ok": True,
        "session": {"session_id": sid, "title": summary.get("session_title")
                    or s.get("title") or "未命名直播",
                    "started_at": s.get("started_at") or "",
                    "duration_s": float(s.get("duration_s") or _duration(conn, sid)),
                    "source_mode": s.get("source_mode") or "",
                    "audio_state": "ready" if s.get("audio_path") else "none"},
        "note_state": state, "progress": progress,
        "note": note or {},
        "summary": {"overview": summary.get("overview") or "",
                    "topics": summary.get("topics") or [],
                    "engine_kind": summary.get("engine_kind") or "",
                    "caveats": summary.get("caveats") or [],
                    "stats": summary.get("stats") or {}},
        "streamer": streamer, "points": points,
        "highlights": highlights, "bookmarks": marks,
        "speakers": speakers, "generated_at": generated_at,
        "related": rel,
        "counts": {"points": len(points), "highlights": len(highlights),
                   "bookmarks": len(marks), "speakers": len(speakers)},
    }


def _related(conn: sqlite3.Connection, sid: str, topics: list[str],
             streamer: str, limit: int = 4) -> list[dict]:
    """同主播 / 同主题的最近场次。规则匹配，够用且不花模型钱。"""
    out: list[dict] = []
    rows = [dict(r) for r in conn.execute(
        "SELECT s.id,s.title,s.started_at,s.duration_s,"
        " (SELECT payload_json FROM summaries su WHERE su.session_id=s.id "
        "  ORDER BY su.created_at DESC, su.id DESC LIMIT 1) AS payload_json "
        "FROM sessions s WHERE s.id<>? AND s.deleted_at IS NULL "
        "ORDER BY s.started_at DESC LIMIT 30", (sid,))]
    topic_set = {str(t) for t in topics if t}
    for r in rows:
        payload = {}
        try:
            payload = json.loads(r.pop("payload_json") or "{}")
        except (TypeError, ValueError):
            payload = {}
        same_host = bool(streamer and payload.get("host_name") == streamer)
        common = topic_set & {str(t) for t in (payload.get("topics") or [])}
        if not same_host and not common:
            continue
        out.append({"session_id": r["id"],
                    "title": payload.get("session_title") or r["title"],
                    "started_at": r["started_at"],
                    "duration_s": float(r["duration_s"] or 0),
                    "reason": "同一个主播" if same_host else
                    ("、".join(sorted(common)[:2]) if common else "相关")})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 索引

def _point_text(p: dict) -> str:
    bits = [str(p.get("title") or ""), str(p.get("gist") or "")]
    for e in (p.get("evidence") or [])[:2]:
        bits.append(str(e.get("text") or ""))
    return " ".join(b for b in bits if b).strip()


def index_session(conn: sqlite3.Connection, sid: str) -> int:
    """重建一场的 note_units。没有总结也索引书签/高光，问笔记至少能找到原话。"""
    s = store.get_session(conn, sid)
    if not s:
        return 0
    summary = _summary(conn, sid)
    title = str(summary.get("session_title") or s.get("title") or "")
    units: list[dict] = []
    if summary.get("overview"):
        units.append({"kind": "summary", "ref_id": "overview", "t": None,
                      "title": title, "text": str(summary["overview"])[:1000],
                      "weight": 0.7})
    for i, p in enumerate(summary.get("points") or []):
        text = _point_text(p)
        if not text:
            continue
        units.append({"kind": "point", "ref_id": str(i),
                      "t": p.get("t_from"), "title": title, "text": text,
                      "speaker": "、".join((p.get("speakers") or [])[:2]),
                      "weight": 1.0})
    for h in store.list_highlights(conn, sid, 50):
        quote = str(h.get("quote") or h.get("hook") or h.get("text") or "").strip()
        if not quote:
            continue
        units.append({"kind": "highlight", "ref_id": str(h.get("id") or ""),
                      "t": h.get("peak_t") or h.get("t_start"),
                      "speaker": str(h.get("speaker_label") or ""),
                      "title": title, "text": quote[:800], "weight": 0.9})
    for b in bookmarks(conn, sid):
        note = str(b.get("note") or "").strip()
        if not note:
            continue
        units.append({"kind": "bookmark", "ref_id": str(b.get("ref_id") or ""),
                      "t": b.get("t"), "title": title,
                      "text": note[:500], "weight": 0.8})
    n = store.replace_note_units(conn, sid, units)
    if summary:
        state = "ready" if (summary.get("overview") or summary.get("points")) else "partial"
        store.upsert_note(conn, sid, state=state, progress=100,
                          generated_at=summary.get("generated_at") or store.now())
    return n


def ensure_index(conn: sqlite3.Connection, limit: int = 0,
                 rebuild: bool = False) -> int:
    """启动时补索引：有总结但 note_units 为空（或要求全量重建）的场次。"""
    if rebuild:
        rows = conn.execute("SELECT id FROM sessions WHERE deleted_at IS NULL "
                            "ORDER BY started_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT s.id FROM sessions s WHERE s.deleted_at IS NULL AND EXISTS("
            "SELECT 1 FROM summaries su WHERE su.session_id=s.id) AND NOT EXISTS("
            "SELECT 1 FROM note_units nu WHERE nu.session_id=s.id) "
            "ORDER BY s.started_at DESC" +
            (" LIMIT %d" % int(limit) if limit else "")).fetchall()
    n = 0
    for r in rows:
        try:
            index_session(conn, r["id"])
            n += 1
        except Exception:
            continue
    return n


def suggest_questions(conn: sqlite3.Connection, sid: str = "",
                      limit: int = 3) -> list[str]:
    """空态建议问题。规则拼接，不调模型。"""
    if sid:
        summary = _summary(conn, sid)
        topics = [str(t) for t in (summary.get("topics") or []) if t][:3]
        title = str(summary.get("session_title") or "")
        out = []
        if topics:
            out.append("这场的%s讲了哪些关键结论？" % topics[0])
            if len(topics) > 1:
                out.append("%s和%s之间有什么关系？" % (topics[0], topics[1]))
        if title:
            out.append("《%s》最值得回听的是哪一段？" % title[:20])
        return out[:limit]
    rows = [dict(r) for r in conn.execute(
        "SELECT payload_json FROM summaries WHERE payload_json<>'' "
        "ORDER BY created_at DESC, id DESC LIMIT 8")]
    topics: list[str] = []
    for r in rows:
        try:
            data = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for t in data.get("topics") or []:
            t = str(t).strip()
            if t and t not in topics:
                topics.append(t)
        if len(topics) >= 4:
            break
    out = []
    if topics:
        out.append("最近关于「%s」有哪些值得回看的观点？" % topics[0])
        if len(topics) > 1:
            out.append("「%s」和「%s」可以怎么结合？" % (topics[0], topics[1]))
    out.append("上周记录里最值得回听的三段是什么？")
    return out[:limit]


# ---------------------------------------------------------------- 时间范围

_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_time_scope(text: str, now: datetime | None = None) -> dict:
    """从问题里解析时间范围。识别不了返回空 dict（= 全部）。

    只覆盖常见说法；解析错了比不解析更糟，所以拿不准就返回空。
    """
    t = str(text or "")
    now = now or datetime.now()
    day = datetime(now.year, now.month, now.day)
    monday = day - timedelta(days=day.weekday())

    def _out(since: datetime, until: datetime) -> dict:
        return {"since": since.isoformat(timespec="seconds"),
                "until": until.isoformat(timespec="seconds")}

    if re.search(r"上周|上个星期|上一周", t):
        return _out(monday - timedelta(days=7), monday)
    if re.search(r"这周|本周|这个星期", t):
        return _out(monday, monday + timedelta(days=7))
    if re.search(r"上个月|上月", t):
        first = day.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return _out(prev, first)
    m = re.search(r"最近\s*([一二两三四五六七八九十\d]+)\s*天", t)
    if m:
        raw = m.group(1)
        n = _CN_NUM.get(raw) or (int(raw) if raw.isdigit() else 0)
        if n:
            return _out(day - timedelta(days=n), day + timedelta(days=1))
    m = re.search(r"(\d{1,2})\s*月", t)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            year = now.year if month <= now.month else now.year - 1
            start = datetime(year, month, 1)
            end = datetime(year + (month == 12), (month % 12) + 1, 1)
            return _out(start, end)
    if re.search(r"今年", t):
        return _out(datetime(now.year, 1, 1), datetime(now.year + 1, 1, 1))
    return {}


def scope_label(scope: dict) -> str:
    since, until = scope.get("since"), scope.get("until")
    if not since:
        return "全部笔记"
    return "%s 至 %s" % (str(since)[:10], str(until or "")[:10])
