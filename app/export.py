# -*- coding: utf-8 -*-
"""导出：Markdown 纪要 / SRT 字幕。

只导出数据库里真实存在的发言，不做二次加工——复盘的信任基础是"看到的就是当时听到的"。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Iterable

from . import store
from .models import SOURCE_MODE_META, fmt_ts


def _srt_ts(seconds: float) -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _mode_name(code: str) -> str:
    return (SOURCE_MODE_META.get(code) or ("未知", ""))[0]


# 旧版本地稿的 gist 是原句拼接，句首常带「我觉得吧」「就是说」：导出时清掉，
# 读起来才是结论而不是口头禅（模型稿偶尔也会带，统一处理）。分析和存储保持
# 原样，证据要能对回原文。
_FILLER_RE = re.compile(
    r"^(?:我个人觉得|我个人认为|我觉得|我认为|我感觉|我建议|我理解|在我看来|"
    r"其实|就是说|也就是说|换句话说|然后|但是|不过|所以|因此|而且|"
    r"首先|另外|说实话|坦白说|客观讲|对|是的|嗯|呃)"
    r"(?:吧|啊|呀|呢|嘛|哈)?[，,、：:\s]*")


def _strip_fillers(text: str) -> str:
    """把每条分句开头的口头禅去掉；一句都不剩就返回原文。"""
    parts = [p.strip() for p in str(text or "").split("；")]
    out = []
    for p in parts:
        for _ in range(2):
            p = _FILLER_RE.sub("", p).strip()
        if p:
            out.append(p)
    return "；".join(out) if out else str(text or "")


def to_srt(conn: sqlite3.Connection, sid: str) -> str:
    rows = store.session_transcript(conn, sid)
    out = []
    for i, r in enumerate(rows, 1):
        start = float(r["t_start"])
        end = max(float(r["t_end"]), start + 0.3)
        who = store.speaker_display(r["speaker_label"], r.get("person_name"),
                                    r.get("person_state"))
        text = (r["text"] or "").strip()
        out.append("%d\n%s --> %s\n%s\n" % (i, _srt_ts(start), _srt_ts(end),
                                            (who + "：" + text) if who else text))
    return "\n".join(out)


def to_markdown(conn: sqlite3.Connection, sid: str) -> str:
    s = store.get_session(conn, sid) or {}
    rows = store.session_transcript(conn, sid)
    spk = store.speaker_totals(conn, sid)
    marks = store.list_marks(conn, sid)
    dur = float(s.get("duration_s") or 0)
    out = ["# %s" % (s.get("title") or sid), ""]
    out.append("- 场次：`%s`" % sid)
    out.append("- 开始：%s　时长：%s" % (s.get("started_at") or "-", fmt_ts(dur)))
    out.append("- 采集：%s" % _mode_name(s.get("source_mode") or ""))
    out.append("- 发言：%d 条　导出时间：%s"
               % (len(rows), datetime.now().strftime("%Y-%m-%d %H:%M")))
    out.append("")
    if spk:
        out.append("## 说话人")
        out.append("")
        out.append("| 说话人 | 发言 | 时长 |")
        out.append("|---|---|---|")
        for p in spk:
            out.append("| %s | %d | %s |" % (
                store.speaker_display(p["speaker_label"], p.get("person_name"),
                                      p.get("person_state")),
                p["n"], fmt_ts(p["seconds"] or 0)))
        out.append("")
    if marks:
        out.append("## 打点")
        out.append("")
        for m in marks:
            out.append("- `%s` %s" % (fmt_ts(m["t"]), m.get("note") or ""))
        out.append("")
    out.append("## 逐字稿")
    out.append("")
    for r in rows:
        who = store.speaker_display(r["speaker_label"], r.get("person_name"),
                                    r.get("person_state"))
        flag = " ⚠重叠" if r.get("overlap") else ""
        out.append("**%s %s**%s　%s" % (fmt_ts(r["t_start"]), who, flag, (r["text"] or "").strip()))
        out.append("")
    return "\n".join(out)


def to_txt(conn: sqlite3.Connection, sid: str) -> str:
    rows = store.session_transcript(conn, sid)
    out = []
    for r in rows:
        who = store.speaker_display(r["speaker_label"], r.get("person_name"),
                                    r.get("person_state"))
        out.append("[%s] %s%s" % (fmt_ts(r["t_start"]), (who + "：") if who else "",
                                  (r["text"] or "").strip()))
    return "\n".join(out)


def to_note_md(conn: sqlite3.Connection, sid: str) -> str:
    """一篇「笔记」的 Markdown：默认不带逐字稿（逐字稿用 md/srt 单独导出）。"""
    from . import notes as _notes
    note = _notes.build_note(conn, sid)
    if not note.get("ok"):
        raise ValueError(str(note.get("error") or "笔记不存在"))
    s = note["session"]
    summary = note["summary"]
    out = ["# %s" % (s.get("title") or sid), ""]
    shape = note.get("shape") or {}
    if shape.get("label") and shape.get("kind") != "single":
        out.append("形态：%s · %d 位说话人" % (shape["label"],
                                             int(shape.get("speaker_count") or 0)))
    if note.get("streamer"):
        out.append("%s：%s" % ("主持" if shape.get("kind") not in ("", "single")
                              else "主播", note["streamer"]))
    out.append("时间：%s　时长：%s" % (s.get("started_at") or "-",
                                    fmt_ts(s.get("duration_s") or 0)))
    out.append("")
    if summary.get("overview"):
        out += ["## 一句话", "", str(summary["overview"]), ""]
    points = note.get("points") or []
    groups = note.get("by_speaker") or []
    # 圆桌/问答按人分；单人场沿用原来的扁平列表，导出格式不变。
    if points and len(groups) > 1 and shape.get("kind") != "single":
        out += ["## 观点", ""]
        for g in groups:
            role = ("（%s）" % g["role_label"]) if g.get("role_label") else ""
            out += ["### %s%s" % (g.get("who") or "本场共同讨论", role), ""]
            for p in g.get("points") or []:
                stance = p.get("stance") or p.get("title") or "要点"
                detail = _strip_fillers(p.get("gist") or "")
                if detail and detail.strip() != stance.strip():
                    out.append("- **%s**：%s" % (stance, detail))
                else:
                    out.append("- **%s**" % stance)
                # 原文只在它不是这条要点的来源句时才引：stance/gist 都是从它截的，
                # 再引一遍就是把同一句话读第三遍。
                quote = str(p.get("quote") or "").strip()
                used = [str(q) for q in (stance, detail) if q]
                if quote and not any(q[:30] in quote for q in used):
                    out.append("  > %s" % quote)
            out.append("")
    elif points:
        out += ["## 要点", ""]
        for i, p in enumerate(points, 1):
            out.append("### %d. %s" % (i, p.get("title") or "要点"))
            out.append("")
            if p.get("gist"):
                out.append(str(p["gist"]))
                out.append("")
            if p.get("quote"):
                out.append("> %s" % p["quote"])
                out.append("")
    highlights = [h for h in (note.get("highlights") or [])
                  if str(h.get("source") or "") in ("manual", "live")]
    if highlights:
        out += ["## 值得回听", ""]
        for h in highlights[:8]:
            out.append("- `%s` %s" % (fmt_ts(h.get("peak_t") or h.get("t_start") or 0),
                                      h.get("quote") or h.get("hook")
                                      or h.get("text") or ""))
        out.append("")
    if note.get("bookmarks"):
        out += ["## 书签", ""]
        for b in note["bookmarks"]:
            out.append("- `%s` %s" % (fmt_ts(b.get("t") or 0),
                                      b.get("note") or ""))
        out.append("")
    if note.get("speakers"):
        out += ["## 说话人", "", "| 说话人 | 发言 | 时长 |", "|---|---|---|"]
        for p in note["speakers"]:
            out.append("| %s | %d | %s |" % (p.get("label") or p.get("speaker_id"),
                                             p.get("n") or 0,
                                             fmt_ts(p.get("seconds") or 0)))
        out.append("")
    out.append("---")
    out.append("由 LiveNotes 本地生成，没有上传任何内容。")
    return "\n".join(out)


# ---------------------------------------------------------------- 飞书发布
#
# 发布稿是「手机单端」能读的那一版：只留结论、可引用的原话和回听入口，
# 逐字稿留在桌面端。它仍然只做聚合，不新增任何模型调用（docs/47 §4）。

PUBLISH_TEMPLATE_VERSION = "1"


def _whole_filter(raw) -> bool:
    """这份总结是不是「整场口径」。老数据没有 filter_json 时按整场处理。"""
    import json

    if not raw:
        return True
    try:
        f = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return True
    if not isinstance(f, dict):
        return True
    if f.get("categories") or f.get("speakers"):
        return False
    if str(f.get("q") or "").strip():
        return False
    return all(f.get(k) is None for k in ("t_from", "t_to", "min_conf"))


def _clip(text: str, limit: int = 90) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _at(seconds) -> str:
    try:
        return fmt_ts(float(seconds or 0), with_hours=False)
    except (TypeError, ValueError):
        return "00:00"


def _publish_status(n_utterances: int, capture_only: bool,
                    has_summary: bool) -> str:
    if not n_utterances and capture_only:
        return "pending_transcript"
    if not has_summary:
        return "pending_summary"
    return "ready"


_PUBLISH_STATUS_TEXT = {"pending_transcript": "待补转写",
                        "pending_summary": "待总结", "ready": "已总结"}


def to_publish_md(conn: sqlite3.Connection, sid: str) -> dict:
    """生成发布到飞书的 Markdown 与场次表字段。失败/跳过都返回 dict，不抛。

    返回：
        ok / skipped / reason / message
        title / markdown / names / state（pending_transcript|pending_summary|ready）
        row：多维表格「场次」表的一行（链接字段留空，由发布方回填）
    """
    import json
    from datetime import datetime as _dt

    from . import notes as _notes, timefmt

    s = store.get_session(conn, sid)
    if not s or s.get("deleted_at"):
        return {"ok": False, "reason": "missing", "message": "场次不存在"}
    n = int(conn.execute("SELECT COUNT(*) AS n FROM utterances WHERE session_id=?",
                         (sid,)).fetchone()["n"])
    capture_only = int(s.get("capture_only") or 0) == 1
    speakers = store.speaker_totals(conn, sid)

    latest = store.recent_summaries(conn, sid, "content", 1)
    if latest and not _whole_filter(latest[0].get("filter_json")):
        # 最新一条是筛选口径：直接发布会把「筛过的要点」冒充整场结论。
        return {"ok": False, "skipped": True, "reason": "filtered_summary",
                "message": "最新总结是筛选口径，先重跑整场总结再发布"}
    summary = store.latest_summary(conn, sid, "content") if latest else None
    state = _publish_status(n, capture_only, bool(summary))
    status_text = _PUBLISH_STATUS_TEXT[state]

    note = _notes.build_note(conn, sid) if summary else {}
    if note and not note.get("ok"):
        note = {}
    shape_info = (note or {}).get("shape") or {}
    title_plain = str((note or {}).get("session", {}).get("title")
                      or s.get("title") or sid)
    title = "%s ·《%s》" % (timefmt.start_hm(s), title_plain)
    shape_label = str(shape_info.get("label") or "")
    if state == "ready" and shape_label and shape_info.get("kind") != "single":
        title += " · " + shape_label
    title = _clip(title, 120)

    duration = float(s.get("duration_s") or 0)
    host = str((note or {}).get("host") or (note or {}).get("streamer") or "")
    speakers_n = int(shape_info.get("speaker_count") or len(speakers))
    audio_url = str(s.get("drive_url") or s.get("minutes_url") or "")
    room_name = ""
    try:
        from . import rooms as _rooms
        room = _rooms.get_by_session(sid) or {}
        room_name = str(room.get("name") or "")
    except Exception:
        room_name = ""

    meta = ["时长 %s" % fmt_ts(duration)]
    if state == "ready":
        meta.append("上麦 %d 人" % speakers_n if speakers_n else "上麦 —")
    else:
        meta.append("逐字稿 %d 句" % n)
    if host:
        meta.append("主持：%s" % host)
    if capture_only:
        meta.append("采集：仅采集")
    meta.append("状态：%s" % status_text)

    out = ["# %s" % title, "", "> " + " ｜ ".join(meta), ""]

    if state == "pending_transcript":
        out += ["这场是仅采集，逐字稿还没补出来。",
                "补写完成后这篇文档会自动更新成完整的总结、观点和原话。", ""]
    elif state == "pending_summary":
        note_row = store.get_note(conn, sid) or {}
        why = "总结还没生成。"
        if str(note_row.get("state") or "") == "failed":
            why = "上次总结失败：%s" % (str(note_row.get("error") or "")[:120]
                                       or "原因未知")
        out += ["## 逐字稿概况", "",
                "- 已补 %d 句，%d 位说话人" % (n, len(speakers))]
        for sp in speakers[:6]:
            out.append("- %s：%d 句 / %s" % (
                store.speaker_display(sp["speaker_label"], sp.get("person_name"),
                                      sp.get("person_state")),
                int(sp.get("n") or 0), fmt_ts(sp.get("seconds") or 0)))
        out += ["", "## 总结", "", why, ""]
    else:
        overview = str(((note or {}).get("summary") or {}).get("overview") or "")
        if overview:
            out += [overview, ""]
        out += _publish_opinions(note)
        out += _publish_highlights(note)
        out += _publish_people(note, shape_info)
        rel = (note or {}).get("related") or []
        if rel:
            out += ["## 相关场次", ""]
            for r in rel[:4]:
                when = str(r.get("started_at") or "")[5:16].replace("T", " ")
                out.append("- %s ·《%s》 · %s" % (when, _clip(r.get("title"), 40),
                                                 r.get("reason") or "相关"))
            out += [""]

    if audio_url:
        out += ["## 录音", "", "- [整场录音](%s)" % audio_url, ""]
    out += ["---",
            "由 LiveNotes 生成。内容只来自本场逐字稿与总结；"
            "逐字稿、时间轴和回听在桌面端。"]

    overview = str(((note or {}).get("summary") or {}).get("overview") or "")
    started = str(s.get("started_at") or "")
    try:
        date_ms = int(_dt.fromisoformat(started).timestamp() * 1000)
    except (TypeError, ValueError):
        date_ms = 0
    topics = [str(t) for t in (((note or {}).get("summary") or {}).get("topics") or [])
              if str(t).strip()][:8]
    counts = (note or {}).get("counts") or {}
    row = {
        "session_id": sid, "title": title, "title_plain": title_plain,
        "started_at": started, "date_ms": date_ms,
        "duration_min": int(round(duration / 60.0)), "room": room_name,
        "shape": shape_label, "host": host, "speakers": speakers_n,
        "overview": overview, "points": int(counts.get("points") or 0),
        "highlights": int(counts.get("highlights") or 0),
        "status": status_text, "audio_url": audio_url,
        "doc_url": "", "topics": topics,
        "mode": "仅采集" if capture_only else "完整",
    }
    names = "|".join(str(sp.get("label") or "") for sp in speakers)
    return {"ok": True, "title": title, "markdown": "\n".join(out),
            "state": state, "status_text": status_text, "names": names,
            "row": row, "template": PUBLISH_TEMPLATE_VERSION,
            "one_line": _clip(overview or ("%d 句逐字稿，%d 位说话人"
                                          % (n, speakers_n)), 120)}


def _publish_opinions(note: dict) -> list[str]:
    """观点地图：按主题列人，标出明确分歧和没人回答的问题。"""
    om = note.get("opinion_map") or {}
    topics = [g for g in (om.get("topics") or []) if g.get("entries")]
    out: list[str] = []
    if topics:
        out += ["## 观点地图", ""]
        for g in topics:
            out += ["### %s" % (g.get("label") or "其他"), ""]
            for e in g.get("entries") or []:
                stance = _clip(e.get("stance") or e.get("gist") or "", 60)
                gist = _clip(e.get("gist") or "", 100)
                bits = "**%s**" % (e.get("who") or "本场共同讨论")
                if stance:
                    bits += "：%s" % stance
                if gist and gist.strip() != stance.strip():
                    bits += "，%s" % gist
                bits += "（%s）" % _at(e.get("t"))
                if e.get("vs"):
                    bits += " ↔ 与%s相反" % e["vs"]
                out.append("- " + bits)
                quote = _clip(e.get("quote"), 90)
                if quote and quote[:20] not in (stance + gist):
                    out.append("  > %s" % quote)
            out.append("")
    questions = [q for q in (om.get("open_questions") or []) if q.get("text")]
    if questions:
        out += ["### 还没答案", ""]
        for q in questions[:3]:
            line = "- %s「%s」" % ((q.get("who") + "：") if q.get("who") else "",
                                  _clip(q.get("text"), 80))
            if q.get("t") is not None:
                line += "（%s）" % _at(q.get("t"))
            line += "，到结束没人接"
            out.append(line)
        out.append("")
    if not topics and note.get("points"):
        out += ["## 要点", ""]
        for p in (note.get("points") or [])[:10]:
            out.append("- **%s**：%s（%s）" % (
                _clip(p.get("title") or p.get("stance"), 50),
                _clip(p.get("gist"), 120), _at(p.get("t_from"))))
        out.append("")
    return out


def _publish_highlights(note: dict) -> list[str]:
    rows = [h for h in (note.get("highlights") or [])
            if str(h.get("source") or "") in ("manual", "live")]
    if not rows:
        rows = (note.get("highlights") or [])[:5]
    if not rows:
        return []
    out = ["## 值得回听", ""]
    for h in rows[:6]:
        who = str(h.get("speaker_label") or "")
        line = "- `%s`%s %s" % (
            _at(h.get("peak_t") if h.get("peak_t") is not None
                else h.get("t_start")),
            (" " + who) if who else "",
            _clip(h.get("quote") or h.get("hook") or h.get("text"), 80))
        why = _clip(h.get("why") or "", 60)
        if why:
            line += " —— %s" % why
        out.append(line)
    out += [""]
    return out


def _publish_people(note: dict, shape_info: dict) -> list[str]:
    rows = note.get("speakers") or []
    if not rows:
        return []
    roles = shape_info.get("roles") or {}
    out = ["## 人物", "", "| 人物 | 角色 | 发言 | 时长 |", "|---|---|---|---|"]
    for p in rows[:12]:
        sid = str(p.get("speaker_id") or "")
        role = str((roles.get(sid) or {}).get("label") or "—")
        out.append("| %s | %s | %d | %s |" % (
            _clip(p.get("label") or sid, 30) or "—", role,
            int(p.get("n") or 0), fmt_ts(p.get("seconds") or 0)))
    out += [""]
    return out


def build(conn: sqlite3.Connection, sid: str, fmt: str) -> tuple[str, str, str]:
    """返回 (正文, MIME, 文件名)。"""
    fmt = (fmt or "md").lower()
    if fmt == "srt":
        return to_srt(conn, sid), "application/x-subrip; charset=utf-8", sid + ".srt"
    if fmt == "txt":
        return to_txt(conn, sid), "text/plain; charset=utf-8", sid + ".txt"
    if fmt in ("note_md", "note", "notes"):
        return (to_note_md(conn, sid), "text/markdown; charset=utf-8",
                sid + ".note.md")
    return to_markdown(conn, sid), "text/markdown; charset=utf-8", sid + ".md"
