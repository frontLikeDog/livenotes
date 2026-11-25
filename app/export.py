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


# ---------------------------------------------------------------- 发布用 Markdown
# 场次文档的目标读者在手机上：只放「结论 + 观点 + 原话 + 人物」，不放逐字稿。
# 没有总结就明确写「待补转写 / 待总结」，不生成规则版兜底稿（docs/47 §3.1）。

_SHAPE_SHORT = {"single": "单人", "roundtable": "圆桌", "qa": "问答"}

_DOC_STATUS = {
    "ready": "已总结",
    "pending_transcript": "待补转写",
    "pending_summary": "待总结",
    "failed_summary": "总结失败",
}


def _pub_time(seconds) -> str:
    """场次内时间码：不足一小时写 MM:SS，超过写 HH:MM:SS。"""
    try:
        t = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    return fmt_ts(t, with_hours=t >= 3600)


def _pub_start(s) -> tuple[str, str, int]:
    """开始时间 -> (2026-11-25 22:30, 11-25 22:30, epoch_ms)。解析不了返回空。"""
    raw = str(s or "").strip()
    if not raw:
        return "", "", 0
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "", "", 0
    return (dt.strftime("%Y-%m-%d %H:%M"), dt.strftime("%m-%d %H:%M"),
            int(dt.timestamp() * 1000))


def _pub_dur(seconds) -> str:
    try:
        sec = max(0, int(round(float(seconds or 0))))
    except (TypeError, ValueError):
        sec = 0
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h and m:
        return "%d 小时 %d 分" % (h, m)
    if h:
        return "%d 小时" % h
    return "%d 分钟" % max(1, m) if sec else "0 分钟"


def _pub_clean(text, limit: int = 80) -> str:
    """原话只留能独立成立的句子：压空白、去「他说/就是说」式起头、截断。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    s = re.sub(r"^(?:他说|她说|他说就是|就是说|然后呢|那个|其实)[，,、：:\s]*", "", s)
    if limit and len(s) > limit:
        s = s[:limit].rstrip("，,、；;：:… ") + "…"
    return s


def _pub_person(who: str, source: str = "") -> str:
    """展示用的人名。位置标签保持「本场第 N 位上麦者」，不猜名字。"""
    from . import identity
    name = str(who or "").strip()
    if not name:
        return "一位上麦者"
    clean, kind = identity.clean_name(name)
    if kind == "nickname":
        return clean
    return name


def _pub_room(sid: str) -> dict:
    try:
        from . import rooms
        return rooms.get_by_session(sid) or {}
    except Exception:
        return {}


def _pub_roster(conn: sqlite3.Connection, sid: str) -> list[dict]:
    from . import identity
    try:
        entries = identity.roster(conn, sid).get("entries") or []
    except Exception:
        return []
    return sorted(entries, key=lambda e: (-float(e.get("seconds") or 0),
                                          int(e.get("ordinal") or 0)))


def _pub_related(conn: sqlite3.Connection, note: dict) -> list[str]:
    lines: list[str] = []
    for r in (note.get("related") or [])[:4]:
        when, _, _ = _pub_start(r.get("started_at"))
        short = when[5:] if when else ""
        title = str(r.get("title") or "未命名")
        reason = str(r.get("reason") or "")
        link = ""
        pub = store.get_publish(conn, str(r.get("session_id") or ""))
        if pub and pub.get("doc_url"):
            link = " · [场次文档](%s)" % pub["doc_url"]
        lines.append("- %s ·「%s」%s%s" % (
            short or "—", title[:40], ("· " + reason) if reason else "", link))
    return lines


def build_publish(conn: sqlite3.Connection, sid: str) -> dict:
    """一场的发布视图：标题、Meta、发布用 Markdown、状态。

    ``state``：ready / pending_transcript / pending_summary / failed_summary。
    没有总结时不写摘要、不写要点、不引原文；只写「已经有什么、还缺什么」。
    """
    from . import notes as _notes
    note = _notes.build_note(conn, sid)
    if not note.get("ok"):
        return {"ok": False, "error": str(note.get("error") or "笔记不存在")}
    s = store.get_session(conn, sid) or {}
    session = note.get("session") or {}
    summary = note.get("summary") or {}
    note_row = note.get("note") or {}
    room = _pub_room(sid)
    roster = _pub_roster(conn, sid)
    n_utt = int(conn.execute(
        "SELECT COUNT(*) AS n FROM utterances WHERE session_id=?",
        (sid,)).fetchone()["n"] or 0)
    offline = store.get_offline_asr(conn, sid) or {}
    shape = note.get("shape") or {}
    shape_short = _SHAPE_SHORT.get(str(shape.get("kind") or ""), "")
    has_summary = bool(summary.get("overview") or note.get("points"))
    note_state = str(note_row.get("state") or "")
    note_error = str(note_row.get("error") or "").strip()
    if has_summary:
        state = "ready"
    elif n_utt <= 0:
        state = "pending_transcript"
    elif note_state == "failed" or note_error:
        state = "failed_summary"
    else:
        state = "pending_summary"

    name = str(session.get("title") or s.get("title") or sid)
    start_full, start_short, start_ms = _pub_start(
        session.get("started_at") or s.get("started_at"))
    title = ("%s ·「%s」" % (start_full, name)) if start_full else name
    room_name = str(room.get("name") or "")
    host = str(note.get("host") or note.get("streamer") or "")
    duration_s = float(session.get("duration_s") or 0)
    counts = note.get("counts") or {}
    status_label = _DOC_STATUS.get(state, state)
    topics = [str(t) for t in (summary.get("topics") or []) if str(t).strip()]
    audio_url = str(s.get("drive_url") or s.get("minutes_url") or "")
    mode = _mode_name(str(s.get("source_mode") or ""))
    if s.get("capture_only"):
        mode = "云端仅采集" if mode in ("", "未知") else ("%s · 仅采集" % mode)

    lines = ["# %s%s" % (title, (" · " + shape_short) if shape_short else ""), ""]
    lines.append("> 房间：%s ｜ 时长：%s ｜ 上麦：%d 人 ｜ 主持：%s" % (
        room_name or "—", _pub_dur(duration_s), len(roster),
        _pub_person(host) if host else "—"))
    lines.append("> 采集：%s ｜ 状态：%s ｜ 观点 %d 条 ｜ 高光 %d 条" % (
        mode or "—", status_label, int(counts.get("points") or 0),
        int(counts.get("highlights") or 0)))
    lines.append("")

    if state == "ready":
        overview = str(summary.get("overview") or "").strip()
        if overview:
            lines += ["## 一句话", "", overview, ""]
        om = note.get("opinion_map") or {}
        topics_out = om.get("topics") or []
        if topics_out:
            lines += ["## 观点地图", ""]
            for group in topics_out:
                label = str(group.get("label") or group.get("topic") or "其他")
                lines += ["### %s" % label, ""]
                for e in group.get("entries") or []:
                    who = _pub_person(str(e.get("who") or ""),
                                      str(e.get("who_source") or ""))
                    stance = _pub_clean(e.get("stance") or e.get("gist"), 120)
                    stamp = _pub_time(e.get("t"))
                    tail = ""
                    if e.get("vs"):
                        tail = "（和%s有分歧）" % _pub_person(str(e.get("vs")))
                    elif e.get("disagree"):
                        tail = "（明确反对）"
                    when = "（%s）" % stamp if stamp else ""
                    lines.append("- **%s**：%s%s%s" % (who, stance, tail, when))
                    quote = _pub_clean(e.get("quote"), 80)
                    if quote and quote[:20] not in stance:
                        lines.append("  > 原话：「%s」" % quote)
                lines.append("")
            questions = om.get("open_questions") or []
            if questions:
                lines += ["### 还没答案", ""]
                for q in questions[:3]:
                    who = _pub_person(str(q.get("who") or ""))
                    text = _pub_clean(q.get("text"), 100)
                    stamp = _pub_time(q.get("t"))
                    lines.append("- **%s** 问「%s」，到结束没人接。%s" % (
                        who, text, ("（%s）" % stamp) if stamp else ""))
                lines.append("")
        elif note.get("by_speaker"):
            # 没有主题聚合时按人分组；仍然只写总结里已有的要点。
            lines += ["## 观点", ""]
            for g in note["by_speaker"]:
                who = _pub_person(str(g.get("who") or ""))
                lines += ["### %s" % who, ""]
                for p in g.get("points") or []:
                    stance = _pub_clean(p.get("stance") or p.get("title"), 120)
                    lines.append("- %s" % stance)
                lines.append("")

        highlights = [h for h in (note.get("highlights") or [])
                      if _pub_clean(h.get("quote") or h.get("hook") or h.get("text"), 120)]
        if highlights:
            lines += ["## 值得回听", "",
                      "| 时间 | 谁 | 为什么值得听 | 原话 |",
                      "|---|---|---|---|"]
            for h in highlights[:5]:
                stamp = _pub_time(h.get("peak_t") or h.get("t_start"))
                who = _pub_person(str(h.get("speaker_label") or ""))
                why = _pub_clean(h.get("why") or h.get("hook") or "值得回听", 40)
                quote = _pub_clean(h.get("quote") or h.get("text"), 60)
                lines.append("| %s | %s | %s | 「%s」 |" % (
                    stamp or "—", who, why, quote))
            lines.append("")

        if roster:
            lines += ["## 人物", "", "| 上麦者 | 发言 | 时长 | 档案 |", "|---|---|---|---|"]
            for e in roster[:12]:
                who = _pub_person(str(e.get("who") or ""))
                lines.append("| %s | %d 条 | %s | — |" % (
                    who, int(e.get("n_utterances") or 0),
                    _pub_dur(e.get("seconds") or 0)))
            lines.append("")

        related = _pub_related(conn, note)
        if related:
            lines += ["## 相关场次", ""] + related + [""]
        lines += ["---",
                  "由 LiveNotes 生成。逐字稿与完整时间轴在桌面端；"
                  "本页只保留结论与可引用的原话。"]
    elif state == "pending_transcript":
        lines += ["## 状态", ""]
        detail = "这场只录了音，逐字稿还没补出来。补转写完成后这一页会自动更新。"
        off_state = str(offline.get("status") or "")
        if off_state == "running":
            detail = "补转写正在进行，出字后这一页会自动更新。"
        elif off_state == "error":
            detail = ("补转写失败了（%s）。修好后重跑补转写，这一页会自动更新。"
                      % _pub_clean(offline.get("error"), 80))
        lines += [detail, ""]
        if audio_url:
            lines += ["## 录音", "", "- 整场录音：%s" % audio_url, ""]
        elif s.get("minutes_url"):
            lines += ["## 录音", "", "- 妙记：%s" % s["minutes_url"], ""]
        lines += ["---",
                  "由 LiveNotes 生成。转写完成前不写摘要，也不生成规则版兜底稿。"]
    else:
        reason = note_error or ("总结还没生成" if state == "pending_summary"
                                else "总结失败")
        lines += ["## 状态", ""]
        lines.append("逐字稿已就绪（%d 句 · %d 位说话人），%s。" % (
            n_utt, len(roster), "总结还没生成" if state == "pending_summary"
            else "总结失败：%s" % _pub_clean(reason, 120)))
        lines += ["总结成功后这一页会自动更新；这里不写规则版兜底稿。", ""]
        if roster:
            lines += ["## 人物", "", "| 上麦者 | 发言 | 时长 |", "|---|---|---|"]
            for e in roster[:12]:
                lines.append("| %s | %d 条 | %s |" % (
                    _pub_person(str(e.get("who") or "")),
                    int(e.get("n_utterances") or 0),
                    _pub_dur(e.get("seconds") or 0)))
            lines.append("")
        lines += ["---",
                  "由 LiveNotes 生成。总结失败时只列事实，不生成替代摘要。"]

    body = "\n".join(lines).rstrip() + "\n"
    return {
        "ok": True, "session_id": sid, "title": title,
        "base_title": "%s · %s · %s" % (
            start_short or "未定", room_name or name[:20] or "未命名",
            shape_short or "场次"),
        "markdown": body, "state": state, "status_label": status_label,
        "overview": str(summary.get("overview") or ""),
        "room_name": room_name, "host": _pub_person(host) if host else "",
        "duration_s": duration_s, "started_at": session.get("started_at") or "",
        "started_ms": start_ms, "shape": shape_short,
        "topics": topics, "counts": {"points": int(counts.get("points") or 0),
                                     "highlights": int(counts.get("highlights") or 0),
                                     "speakers": len(roster)},
        "audio_url": audio_url, "error": note_error,
        "n_utterances": n_utt,
    }


def to_publish_md(conn: sqlite3.Connection, sid: str) -> str:
    """发布用场次 Markdown。不存在则抛 ValueError，由调用方转成失败原因。"""
    out = build_publish(conn, sid)
    if not out.get("ok"):
        raise ValueError(str(out.get("error") or "笔记不存在"))
    return str(out["markdown"])


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
