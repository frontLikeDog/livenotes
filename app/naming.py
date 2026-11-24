# -*- coding: utf-8 -*-
"""按需补名：不要求每场全员标注，只补「这场谁值得记」的前几个人。

为什么是 top 3–5，而不是全员：
- 语音厅只有 8 个麦位，真正撑起内容的通常就是 3–5 个人；
- 前 3 位往往覆盖了大部分发言时长，补一次名字就能让分享卡、观点地图和
  圆桌摘要都有可指代的人；
- 没补的人仍然有「本场第 N 位上麦者」兜底，跳过不会让任何功能不可用。

补名不是只改一个显示字符串：它同时把这一簇声纹注册进人物底库
（``voiceprint.enroll_speaker``），下次这个人再开口就能自动认出来。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import claims, identity, store, voiceprint

# 只列前几位；超过 5 个以后，补名的边际收益已经很低。
DEFAULT_LIMIT = 5
MAX_LIMIT = 5


def _named(entry: dict) -> bool:
    """有可跨场识别的名字：已确认/自动声纹，或本场真实昵称。

    「主播」「本场第 3 位上麦者」只是可展示的占位，不算命名率里的「已命名」。
    """
    return str(entry.get("source") or "") in ("confirmed", "auto", "nickname")


def _usable_name(name: str) -> bool:
    """这个名字能不能当「人」：位置标签/聚类标签都不行。"""
    return not voiceprint.is_generic_name(name) and not identity.is_positional(name)


def is_usable_name(name: str) -> bool:
    """给旧改名接口复用：「主播/嘉宾1」这类角色标签只能当展示名，不进底库。"""
    return _usable_name(name)


def _preview(conn: sqlite3.Connection, sid: str, speaker_id: str) -> dict | None:
    """给这一簇挑一段最适合试听的 3 秒。

    优先不重叠、时间最长的一句：抢话/回音会让人听不清音色，短句又不足以
    分辨。找不到就返回 None，前端把试听按钮置灰，不伪造音频。
    """
    row = conn.execute(
        "SELECT t_start, t_end FROM utterances"
        " WHERE session_id=? AND speaker_id=? AND t_end>t_start"
        " ORDER BY overlap ASC, (t_end-t_start) DESC, t_start ASC LIMIT 1",
        (sid, speaker_id)).fetchone()
    if not row:
        return None
    t0 = float(row["t_start"] or 0.0)
    t1 = min(float(row["t_end"] or t0), t0 + 3.0)
    if t1 <= t0:
        return None
    return {"t_start": round(t0, 2), "t_end": round(t1, 2),
            "seconds": round(t1 - t0, 2)}


def _suggestion(entry: dict, identified: dict) -> dict | None:
    """声纹底库给的「像是谁」。没有底库或分数不够就什么都不说。"""
    if _named(entry):
        return None
    info = identified.get(str(entry.get("speaker_id") or "")) or {}
    best = None
    for cand in info.get("candidates") or []:
        score = float(cand.get("score") or 0.0)
        name = str(cand.get("name") or "").strip()
        if score < voiceprint.CAND_THRESHOLD or not name:
            continue
        if not _usable_name(name):
            continue
        best = {"person_id": str(cand.get("person_id") or ""),
                "name": name, "score": round(score, 3)}
        break
    if best:
        return best
    # 本场已经有人工/自动打上的「疑似」标签时，即使这次没重算也保留建议。
    if entry.get("source") == "candidate":
        name = str(entry.get("person_name") or "").strip()
        pid = str(entry.get("person_id") or "")
        if name and pid and _usable_name(name):
            return {"person_id": pid, "name": name, "score": None}
    return None


def top_speakers(conn: sqlite3.Connection, sid: str,
                 limit: int = DEFAULT_LIMIT) -> dict:
    """这一场最值得补名的几个人，按发言时长排序。

    返回的 ``speakers`` 里既有已命名的人，也有没命名的；前端据此显示
    「已确认」或输入框。``top3`` 是产品指标口径：前三位麦位里命名了几个。
    """
    sid = str(sid or "")
    sess = store.get_session(conn, sid)
    if not sess:
        return {"ok": False, "error": "场次不存在", "speakers": []}
    try:
        limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    roster = identity.roster(conn, sid)
    entries = [e for e in roster["entries"]
               if e.get("speaker_id") and float(e.get("seconds") or 0) > 0]
    entries.sort(key=lambda e: (-float(e.get("seconds") or 0),
                                int(e.get("ordinal") or 0)))
    top = entries[:limit]
    # 全都已经命名时不必再跑一遍底库比对；这一页只是补名入口。
    identified = (voiceprint.identify_session(conn, sid, top=3)
                  if any(not _named(e) for e in top) else {})
    audio_path = str(sess.get("audio_path") or "")
    try:
        has_audio = bool(audio_path) and Path(audio_path).is_file()
    except OSError:
        has_audio = False

    speakers = []
    for entry in top:
        preview = _preview(conn, sid, str(entry["speaker_id"]))
        suggestion = _suggestion(entry, identified)
        speakers.append({
            "speaker_id": str(entry["speaker_id"]),
            "label": str(entry.get("who") or "一位上麦者"),
            "named": _named(entry),
            "source": str(entry.get("source") or ""),
            "source_label": str(entry.get("source_label") or ""),
            "person_id": str(entry.get("person_id") or ""),
            "person_name": str(entry.get("person_name") or ""),
            "seconds": round(float(entry.get("seconds") or 0.0), 1),
            "n_utterances": int(entry.get("n_utterances") or 0),
            "ordinal": int(entry.get("ordinal") or 0),
            "preview": preview,
            "suggestion": suggestion,
        })

    top3 = speakers[:3]
    slots = len(top3)
    named = sum(1 for s in top3 if s["named"])
    return {
        "ok": True,
        "session_id": sid,
        "title": str(sess.get("title") or ""),
        "has_audio": has_audio,
        "speakers": speakers,
        "top3": {
            "slots": slots,
            "named": named,
            "rate": round(named / slots, 4) if slots else None,
        },
        "thresholds": {"candidate": voiceprint.CAND_THRESHOLD},
        "hint": "补一次名字，声纹入库，下次自动认；跳过不影响出卡。",
    }


def name_speaker(conn: sqlite3.Connection, sid: str, speaker_id: str,
                 name: str, person_id: str = "") -> dict:
    """一键补名：改本场显示名 + 注册声纹 + 重建这一场的观点归属。

    声纹样本不足时仍然保留名字（分享卡先能用），只是把 ``enrolled`` 标成
    False，让界面如实说「下次还不能自动认」，而不是假装成功。
    """
    sid = str(sid or "")
    speaker_id = str(speaker_id or "")
    name = str(name or "").strip()[:40]
    if not sid or not speaker_id:
        return {"ok": False, "error": "缺少 session_id / speaker_id"}
    if not name:
        return {"ok": False, "error": "名字不能为空"}
    if not _usable_name(name):
        return {"ok": False, "error": "「%s」是位置标签，不是名字" % name}
    if not store.get_session(conn, sid):
        return {"ok": False, "error": "场次不存在"}
    if not conn.execute(
            "SELECT 1 FROM utterances WHERE session_id=? AND speaker_id=? LIMIT 1",
            (sid, speaker_id)).fetchone():
        return {"ok": False, "error": "这一簇不存在"}
    pid = str(person_id or "").strip()
    if pid and not store.get_person(conn, pid):
        pid = ""
    store.rename_speaker(conn, sid, speaker_id, name)
    enrolled = voiceprint.enroll_speaker(conn, sid, speaker_id, name,
                                         person_id=pid or None)
    if not enrolled.get("ok"):
        # 声纹没注册成功时，之前匹配出来的「疑似」身份还挂在 speakers 上；
        # 名单会优先显示「阿哲（疑似）」，把用户刚输入的名字盖掉。人已经
        # 明确改名，旧的机器猜测必须让位。
        row = conn.execute(
            "SELECT s.person_state, p.name AS person_name FROM speakers s"
            " LEFT JOIN persons p ON p.person_id=s.person_id"
            " WHERE s.session_id=? AND s.speaker_id=?",
            (sid, speaker_id)).fetchone()
        state = str(row["person_state"] or "") if row else ""
        old_name = str(row["person_name"] or "") if row else ""
        # cand 本来只是机器猜测；auto 匹配也被人工改名推翻时（新名字和底库
        # 里那个人不同），同样要让位，否则名单继续显示旧人，用户改了像没改。
        if state == "cand" or (state == "auto" and old_name and old_name != name):
            store.set_speaker_person(conn, sid, speaker_id, None, state="")
    # 名字一落，观点档案的分组键就可能从「本场第 N 位」变成真人；
    # 不重建的话，分享卡/观点地图要等下一次生成笔记才更新。
    claims.rebuild_session(conn, sid)
    return {
        "ok": True,
        "session_id": sid,
        "speaker_id": speaker_id,
        "name": name,
        "person_id": str(enrolled.get("person_id") or pid or ""),
        "enrolled": bool(enrolled.get("ok")),
        "added": int(enrolled.get("added") or 0),
        "reason": str(enrolled.get("reason") or ""),
    }


def product_health(conn: sqlite3.Connection, days: int = 30,
                   limit: int = 500) -> dict:
    """产品口径：top 3 说话人命名率 + 可分享内容产出率。

    不再看「全员标注率」：语音厅每天有新人，全员标注既做不到，也不该做。
    前三位覆盖大部分发言，补名成本和收益才匹配。

    ``shareable`` 定义为这一场至少有一条可分享的观点、高光或总结要点。
    这是「有没有东西可发」，不是「用户发没发」；发送行为另有埋点。
    """
    try:
        days = max(0, int(days))
    except (TypeError, ValueError):
        days = 30
    where, args = ["s.deleted_at IS NULL", "s.id NOT LIKE 'DEMO%'"], []
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        where.append("COALESCE(s.started_at,'')>=?")
        args.append(since)
    rows = conn.execute(
        "SELECT s.id FROM sessions s WHERE " + " AND ".join(where) +
        " ORDER BY s.started_at DESC LIMIT ?", args + [max(1, int(limit))]).fetchall()

    sessions_with_speech = 0
    sessions_all_top3 = 0
    slots = named = 0
    with_content = 0
    for row in rows:
        sid = str(row["id"])
        roster = identity.roster(conn, sid)
        top3 = sorted([e for e in roster["entries"]
                       if e.get("speaker_id") and float(e.get("seconds") or 0) > 0],
                      key=lambda e: (-float(e.get("seconds") or 0),
                                     int(e.get("ordinal") or 0)))[:3]
        if top3:
            sessions_with_speech += 1
            n = sum(1 for e in top3 if _named(e))
            slots += len(top3)
            named += n
            if n == len(top3):
                sessions_all_top3 += 1
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM claims WHERE session_id=?"
            "        AND (stance<>'' OR gist<>'')) AS claims_n,"
            "       (SELECT COUNT(*) FROM highlights WHERE session_id=?) AS hl_n",
            (sid, sid)).fetchone()
        has_content = int(counts["claims_n"] or 0) > 0 or int(counts["hl_n"] or 0) > 0
        if not has_content:
            # 老场次可能还没重建 claims；有总结要点同样算「有东西可发」。
            summary = store.latest_summary(conn, sid, "content")
            payload = (summary or {}).get("payload") or {}
            has_content = bool(payload.get("points"))
        if has_content:
            with_content += 1
    return {
        "ok": True,
        "window_days": days,
        "sessions": len(rows),
        "top3_naming": {
            "named_slots": named,
            "slots": slots,
            "rate": round(named / slots, 4) if slots else None,
            "sessions_with_speech": sessions_with_speech,
            "sessions_all_named": sessions_all_top3,
            "session_rate": (round(sessions_all_top3 / sessions_with_speech, 4)
                             if sessions_with_speech else None),
        },
        "shareable_output": {
            "sessions": len(rows),
            "with_content": with_content,
            "rate": round(with_content / len(rows), 4) if rows else None,
        },
        "definitions": {
            "top3_naming": "每场按发言时长取前 3 位；已确认/声纹自动/真实昵称算已命名。",
            "shareable_output": "至少一条观点、高光或总结要点的场次占比。",
        },
    }
