# -*- coding: utf-8 -*-
"""导出：Markdown 纪要 / SRT 字幕。

只导出数据库里真实存在的发言，不做二次加工——复盘的信任基础是"看到的就是当时听到的"。
"""
from __future__ import annotations

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
    if note.get("streamer"):
        out.append("主播：%s" % note["streamer"])
    out.append("时间：%s　时长：%s" % (s.get("started_at") or "-",
                                    fmt_ts(s.get("duration_s") or 0)))
    out.append("")
    if summary.get("overview"):
        out += ["## 一句话", "", str(summary["overview"]), ""]
    points = note.get("points") or []
    if points:
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
