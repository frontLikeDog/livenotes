# -*- coding: utf-8 -*-
"""多人圆桌摘要：一场里「有几个人、每个人说了什么、分歧在哪」。

这是 docs/41 §6.2 的 G2' 工具之一。公网版应让用户上传一段录音；本地版先
从「你已经记过的一场」生成——不新增模型调用，只把已经落库的观点原子按
主题重新排一遍。没有总结/没有观点时明确说「还不能生成」，不编内容。

和「笔记页观点地图」的区别：这里面向分享与导出，输出是一份可复制的
Markdown 摘要；判断「分歧」只看原话里有没有明确的反对词，不推断对错。
"""
from __future__ import annotations

import sqlite3

from . import claims, shape, store


def _fmt_t(seconds: object) -> str:
    try:
        t = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        return ""
    if t >= 3600:
        return "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)
    return "%02d:%02d" % (t // 60 % 60, t % 60)


def _duration(seconds: object) -> str:
    try:
        mins = max(0, int(round(float(seconds or 0) / 60.0)))
    except (TypeError, ValueError):
        return ""
    if mins >= 60:
        return "%d 小时 %d 分" % (mins // 60, mins % 60)
    return "%d 分钟" % mins


def build(conn: sqlite3.Connection, sid: str) -> dict:
    session = store.get_session(conn, sid)
    if not session or session.get("deleted_at"):
        return {"ok": False, "error": "这场不存在或已删除"}
    summary = store.latest_summary(conn, sid, "content")
    payload = (summary or {}).get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    shape_info = payload.get("shape")
    if not (isinstance(shape_info, dict) and shape_info.get("kind")):
        shape_info = shape.session_shape(conn, sid)
    roles = (shape_info or {}).get("roles") or {}
    title = str(payload.get("session_title") or session.get("title") or "未命名场次")
    started_at = str(session.get("started_at") or "")
    duration_s = float(session.get("duration_s") or 0.0)

    atoms = claims.rows_for(conn, session_id=sid, limit=800)
    by_who: dict[str, int] = {}
    for r in atoms:
        if str(r.get("kind") or "") != "claim":
            continue
        who = str(r.get("who") or "").strip()
        if who:
            by_who[who] = by_who.get(who, 0) + 1

    participants = []
    for sp in store.speaker_totals(conn, sid):
        sid_sp = str(sp.get("speaker_id") or "")
        role = roles.get(sid_sp) or {}
        name = str(sp.get("person_name") or sp.get("speaker_label") or sid_sp)
        participants.append({
            "speaker_id": sid_sp,
            "name": name,
            "person_id": str(sp.get("person_id") or ""),
            "role": str(role.get("role") or ""),
            "role_label": str(role.get("label") or ""),
            "seconds": round(float(sp.get("seconds") or 0.0), 1),
            "n_utterances": int(sp.get("n") or 0),
            "n_claims": int(by_who.get(name, 0)),
        })
    participants.sort(key=lambda p: (-p["n_claims"], -p["seconds"], p["name"]))

    topics: dict[str, list[dict]] = {}
    questions: list[dict] = []
    for r in atoms:
        topic = claims.topic_label(str(r.get("topic") or ""))
        stance = str(r.get("stance") or "").strip()
        if not stance:
            continue
        who = str(r.get("who") or "").strip() or "本场共同讨论"
        role = roles.get("") or {}
        sp = next((p for p in participants if p["name"] == who), None)
        if sp:
            role = {"role": sp.get("role"), "label": sp.get("role_label")}
        entry = {
            "who": who,
            "person_id": str(r.get("person_id") or ""),
            "role": str(role.get("role") or ""),
            "role_label": str(role.get("label") or ""),
            "topic": topic,
            "stance": stance,
            "quote": str(r.get("quote") or ""),
            "t": r.get("t"),
            "kind": str(r.get("kind") or "claim"),
            "disagree": False,
            "vs": "",
        }
        if entry["kind"] == "question":
            questions.append(entry)
        else:
            entry["disagree"] = claims.disagrees(stance)
            topics.setdefault(topic, []).append(entry)

    topic_rows = []
    n_conflicts = 0
    for label, entries in topics.items():
        entries.sort(key=lambda e: (e.get("t") is None, e.get("t") or 0))
        prev = ""
        for e in entries:
            if e["disagree"] and prev and prev != e["who"]:
                e["vs"] = prev
                n_conflicts += 1
            prev = e["who"]
        conflict = any(e["vs"] for e in entries)
        topic_rows.append({
            "topic": label,
            "n": len(entries),
            "n_people": len({e["who"] for e in entries}),
            "conflict": conflict,
            "t_from": entries[0].get("t"),
            "entries": entries,
        })
    topic_rows.sort(key=lambda g: (g["topic"] == claims.GENERIC_LABEL,
                                   g.get("t_from") is None,
                                   g.get("t_from") or 0))

    answered_topics = {g["topic"] for g in topic_rows}
    open_questions = []
    seen_q: set[str] = set()
    for q in questions:
        # 这个问题所在主题已经有主张，就不算「没人答」；只列整块没人接的。
        if str(q.get("topic") or "") in answered_topics:
            continue
        key = q["stance"]
        if key in seen_q:
            continue
        seen_q.add(key)
        open_questions.append(q)

    return {
        "ok": True,
        "session": {"session_id": sid, "title": title,
                    "started_at": started_at, "duration_s": duration_s},
        "shape": {"kind": str((shape_info or {}).get("kind") or ""),
                  "label": str((shape_info or {}).get("label") or "")},
        "participants": participants,
        "topics": topic_rows,
        "open_questions": open_questions[:8],
        "stats": {
            "people": len(participants),
            "claims": sum(1 for r in atoms if str(r.get("kind") or "") == "claim"),
            "topics": len(topic_rows),
            "conflicts": len([g for g in topic_rows if g["conflict"]]),
            "open_questions": len(open_questions),
        },
    }


def markdown(data: dict) -> str:
    """把圆桌摘要导出成可以直接发到群里的 Markdown。"""
    if not data.get("ok"):
        return "# 圆桌摘要\n\n%s\n" % (data.get("error") or "还不能生成")
    s = data.get("session") or {}
    lines = ["# 圆桌摘要 · %s" % (s.get("title") or "未命名场次"), ""]
    meta = [str(s.get("started_at") or "")[:16].replace("T", " "),
            _duration(s.get("duration_s")),
            "%d 人上麦" % int((data.get("stats") or {}).get("people") or 0),
            "%d 条观点" % int((data.get("stats") or {}).get("claims") or 0)]
    lines.append(" · ".join(x for x in meta if x))
    lines.append("")
    parts = data.get("participants") or []
    if parts:
        lines.append("## 上麦的人")
        for p in parts:
            bits = [p.get("role_label") or "",
                    "%d 条观点" % int(p.get("n_claims") or 0)]
            bits = [b for b in bits if b and b != "0 条观点"]
            lines.append("- %s%s" % (p.get("name") or "未识别",
                                     ("（%s）" % " · ".join(bits)) if bits else ""))
        lines.append("")
    for g in data.get("topics") or []:
        lines.append("## %s%s" % (g.get("topic") or "其他",
                                  "（有分歧）" if g.get("conflict") else ""))
        for e in g.get("entries") or []:
            vs = (" ← 和%s说法不同" % e["vs"]) if e.get("vs") else ""
            lines.append("- **%s**：%s%s" % (e.get("who") or "有人",
                                             e.get("stance") or "", vs))
            if e.get("quote"):
                lines.append("  > %s" % e["quote"])
            if e.get("t") is not None:
                lines.append("  %s" % _fmt_t(e.get("t")))
        lines.append("")
    qs = data.get("open_questions") or []
    if qs:
        lines.append("## 还没答案的问题")
        for q in qs:
            lines.append("- %s%s" % (q.get("stance") or "",
                                     ("（%s 提出）" % q["who"]) if q.get("who") else ""))
        lines.append("")
    lines.append("由 LiveNotes 在本机整理；只记录谁说过什么，不判断对错。")
    return "\n".join(lines) + "\n"
