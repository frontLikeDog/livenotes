# -*- coding: utf-8 -*-
"""零标注兜底：没实名时，也要能给一条观点一个可指代的人。

分享卡、观点地图、圆桌摘要都需要「这句话是谁说的」。但语音厅里每天都有
新人，要求用户逐场标注不现实。这里把称呼分成三层，成本从低到高：

1. 声纹/人工确认的人：用 ``persons.name``；
2. 本场真实昵称（平台麦位名、人工改名）：原样展示，标「直播间昵称」；
3. 只有位置/聚类标签：按首次发言顺序生成「本场第 N 位上麦者」。

关键边界：fallback **只存在于展示层**，不写进 ``claims.who``。``who`` 仍然
只存可跨场比较的署名，否则两个同名陌生人会在人物档案里被合并。
"""
from __future__ import annotations

import json
import re
import sqlite3

# 位置/机器标签：这些不是名字，不能直接当「谁」展示。
# 角色词只有「单独出现」或「后面只跟编号」才算位置标签：
# 「主播小王」「主持人小美」是真实昵称，不能因为前缀是角色词就被吞掉；
# 「嘉宾2」「说话人 3」「聚类标签 09」「SPK_02」仍然是机器标签。
_POSITIONAL_RE = re.compile(
    r"^(?:主播|主持人|嘉宾|说话人|用户|观众|有人|未识别|连麦|路人|网友|运营|未知)"
    r"(?:\s*[0-9０-９]+)?$"
    r"|^(?:聚类标签|簇)\s*[0-9０-９]*$"
    r"|^SPK[_-]?\d+$", re.I)
_ROLE_LABELS = {"主播", "主持人"}

SOURCE_LABELS = {
    "confirmed": "已确认",
    "auto": "声纹识别",
    "candidate": "疑似",
    "nickname": "直播间昵称",
    "role": "角色",
    "session": "本场编号",
    "name": "本场署名",
    "shared": "",
}


def is_positional(label: str) -> bool:
    text = str(label or "").strip()
    if not text:
        return False
    # 裸数字是麦位编号（LLM/老数据里常见），和「嘉宾2」一样不能当人名。
    if re.fullmatch(r"[0-9０-９]+", text):
        return True
    return bool(_POSITIONAL_RE.match(text))


def clean_name(label: object) -> tuple[str, str]:
    """公开的标签清理：返回 (可展示的昵称, nickname|role|session)。

    位置标签返回 ``("", "role"/"session")``，调用方应改用本场编号兜底；
    只有 ``nickname`` 才是可以落到 ``claims.who`` 的署名。
    """
    return _clean_label(label)


def _clean_label(label: object) -> tuple[str, str]:
    """把 ``主播 · 阿哲（疑似）`` 拆成可展示的昵称与来源。"""
    text = str(label or "").strip()
    if not text:
        return "", ""
    text = re.sub(r"（疑似）$", "", text).strip()
    if "·" in text:
        parts = [p.strip() for p in text.split("·") if p.strip()]
        # 只剥掉开头的角色段（「主播 · 阿哲」）；昵称里的「·」是名字的一部分，
        # 「阿哲·每晚8点」截成「每晚8点」会让两个人跨场合并。也绝不取最后一段。
        while len(parts) > 1 and is_positional(parts[0]):
            parts.pop(0)
        if not parts:
            return "", ""
        joined = "·".join(parts)
        if joined in _ROLE_LABELS:
            return joined, "role"
        if is_positional(joined) or re.fullmatch(r"[0-9０-９]+", joined):
            return "", "session"
        return joined, "nickname"
    if re.fullmatch(r"[0-9０-９]+", text):
        # 裸数字也是麦位编号（LLM 有时直接给 "3"），不能当跨场署名。
        return "", "session"
    if is_positional(text):
        return (text, "role") if text in _ROLE_LABELS else ("", "session")
    return text, "nickname"


def roster(conn: sqlite3.Connection, sid: str) -> dict:
    """本场说话人名单，按首次发言顺序编号。

    返回 ``{"entries": [...], "by_id": {...}, "by_label": {...}}``。编号是
    本场稳定的：同一个人在同一场里永远得到同一个「第 N 位」。
    """
    rows = conn.execute(
        "SELECT u.speaker_id, MIN(u.t_start) AS first_t,"
        " MAX(s.display_name) AS display_name,"
        " MAX(u.speaker_label) AS speaker_label,"
        " MAX(s.person_id) AS person_id, MAX(s.person_state) AS person_state,"
        " MAX(p.name) AS person_name,"
        " COUNT(*) AS n_utterances,"
        " COALESCE(SUM(u.t_end-u.t_start),0) AS seconds"
        " FROM utterances u"
        " LEFT JOIN speakers s ON s.session_id=u.session_id"
        "   AND s.speaker_id=u.speaker_id"
        " LEFT JOIN persons p ON p.person_id=s.person_id"
        " WHERE u.session_id=? GROUP BY u.speaker_id"
        " ORDER BY first_t, u.speaker_id", (str(sid or ""),)).fetchall()
    entries: list[dict] = []
    by_id: dict[str, dict] = {}
    by_label: dict[str, dict] = {}
    for i, row in enumerate(rows, 1):
        speaker_id = str(row["speaker_id"] or "")
        label, label_kind = _clean_label(row["display_name"] or row["speaker_label"])
        if label_kind != "nickname":
            alt, alt_kind = _clean_label(row["speaker_label"] or "")
            if alt_kind == "nickname":
                label, label_kind = alt, alt_kind
        person_name = str(row["person_name"] or "").strip()
        state = str(row["person_state"] or "")
        if person_name and state in ("auto", "confirmed"):
            who, source = person_name, state
        elif person_name and state == "cand":
            who, source = person_name + "（疑似）", "candidate"
        elif label_kind == "nickname":
            who, source = label, "nickname"
        elif label_kind == "role":
            who, source = label, "role"
        else:
            who, source = "本场第 %d 位上麦者" % i, "session"
        entry = {
            "speaker_id": speaker_id,
            "who": who,
            "source": source,
            "source_label": SOURCE_LABELS.get(source, ""),
            "label": label,
            "person_id": str(row["person_id"] or ""),
            "person_name": person_name,
            "person_state": state,
            "first_t": row["first_t"],
            "n_utterances": int(row["n_utterances"] or 0),
            "seconds": float(row["seconds"] or 0.0),
            "ordinal": i,
        }
        entries.append(entry)
        if speaker_id:
            by_id[speaker_id] = entry
        # 旧总结/旧 claims 里可能直接存了「嘉宾1」「聚类标签 09」；这些
        # 原始标签也要能落到本场编号，否则会退化成「本场共同讨论」。
        for raw in (row["display_name"], row["speaker_label"], label, who):
            text = str(raw or "").strip()
            if text:
                by_label.setdefault(text, entry)
    return {"entries": entries, "by_id": by_id, "by_label": by_label}


def resolve(roster_map: dict, *, speaker_id: object = "", speaker_label: object = "",
            person_id: object = "", who: object = "") -> dict | None:
    """把一条观点/要点落到本场某个说话人；落不到返回 None。"""
    if not roster_map:
        return None
    pid = str(person_id or "").strip()
    if pid:
        for entry in roster_map.get("entries") or []:
            if entry.get("person_id") == pid:
                return entry
    sid = str(speaker_id or "").strip()
    if sid and sid in (roster_map.get("by_id") or {}):
        return roster_map["by_id"][sid]
    for candidate in (speaker_label, who):
        text = str(candidate or "").strip()
        if not text:
            continue
        hit = (roster_map.get("by_label") or {}).get(text)
        if hit:
            return hit
        cleaned, kind = _clean_label(text)
        if cleaned:
            hit = (roster_map.get("by_label") or {}).get(cleaned)
            if hit:
                return hit
        if kind == "nickname" and cleaned:
            # 有昵称但不在本场 roster 里（老总结/手工数据）：仍然可用作署名。
            return {"who": cleaned, "source": "name",
                    "source_label": SOURCE_LABELS["name"], "speaker_id": "",
                    "label": cleaned, "person_id": "", "person_name": "",
                    "person_state": "", "first_t": None, "seconds": 0.0,
                    "ordinal": 0}
        if not is_positional(text):
            return {"who": text, "source": "name",
                    "source_label": SOURCE_LABELS["name"], "speaker_id": "",
                    "label": text, "person_id": "", "person_name": "",
                    "person_state": "", "first_t": None, "seconds": 0.0,
                    "ordinal": 0}
    return None


def _evidence(row: dict) -> list[dict]:
    raw = row.get("evidence_json")
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    try:
        data = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict)]


def owner_evidence(evidence: list[dict], who: object = "") -> dict | None:
    """要点归属人对应的证据行。

    ``claim_speaker`` 是「嘉宾2」这类位置标签时，不能拿第一条证据当归属人：
    证据里可能先引用了别人的话。只有标签对得上才取那条；标签为空才退回
    第一条（没有更好的信号）。
    """
    ev = [e for e in (evidence or []) if isinstance(e, dict)]
    if not ev:
        return None
    text = str(who or "").strip()
    if not text:
        return ev[0]
    cleaned, _kind = _clean_label(text)
    for e in ev:
        label = str(e.get("speaker_label") or e.get("speaker") or "").strip()
        if not label:
            continue
        if label == text:
            return e
        if cleaned and _clean_label(label)[0] == cleaned:
            return e
    return None


def claim_ref(row: dict, roster_map: dict) -> dict:
    """一条 claims 行的展示身份。优先 person_id，其次证据里的 speaker_id。"""
    direct = resolve(roster_map, person_id=row.get("person_id"), who=row.get("who"))
    if direct and direct.get("source") != "name":
        return direct
    for ev in _evidence(row):
        hit = resolve(roster_map,
                      speaker_id=ev.get("speaker_id"),
                      speaker_label=ev.get("speaker") or ev.get("speaker_label"),
                      who=row.get("who"))
        if hit:
            return hit
    return direct or {"who": "一位上麦者", "source": "shared",
                      "source_label": "", "speaker_id": "", "label": "",
                      "person_id": "", "person_name": "", "person_state": "",
                      "first_t": None, "seconds": 0.0, "ordinal": 0}


def point_ref(point: dict, roster_map: dict) -> dict:
    """总结要点（notes 的 point dict）的展示身份。"""
    who = point.get("claim_speaker")
    evidence = point.get("evidence") or []
    # 先按标签找到归属人自己的证据行，再取它的 speaker_id：同一场里可能有人
    # 同名/同标签，直接按标签查会落到名单里的第一个同名者。
    owner = owner_evidence(evidence, who)
    if owner:
        hit = resolve(roster_map,
                      speaker_id=owner.get("speaker_id"),
                      speaker_label=owner.get("speaker") or owner.get("speaker_label"),
                      who=who)
        if hit and hit.get("source") != "name":
            return hit
    direct = resolve(roster_map, who=who)
    if direct and direct.get("source") != "name":
        return direct
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        hit = resolve(roster_map,
                      speaker_id=ev.get("speaker_id"),
                      speaker_label=ev.get("speaker") or ev.get("speaker_label"),
                      who=who)
        if hit:
            return hit
    return direct or {"who": "", "source": "shared", "source_label": "",
                      "speaker_id": "", "label": "", "person_id": "",
                      "person_name": "", "person_state": "",
                      "first_t": None, "seconds": 0.0, "ordinal": 0}
