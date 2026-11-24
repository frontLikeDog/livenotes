# -*- coding: utf-8 -*-
"""观点档案：把「一场里的要点」变成「一个人在一个主题上说过什么」。

为什么单独一层：总结里的 ``points`` 只在一场内成立——换一场，同一件事的结论
散在另一个 ``payload_json`` 里，没人把它们连起来。于是「阿哲怎么看 OPC」
只能靠全文搜到哪句算哪句（docs/41 §1.4）。这一层把每场要点里的
``(谁, 主题, 主张, 原话, 时间)`` 抽成原子落进 ``claims``，读的时候按
「人 × 主题」拼回去。

三条设计决定：

1. **0 token**：只读已经落库的总结，不新增任何模型调用。没有总结的场次
   就没有观点——这是诚实的空，而不是「再问一次模型」。
2. **按 (人, 主题) 读时分组，而不是写时合并**：合并是有损的（观点改口会被
   抹平）。这里保留每场每条的原子，读的时候按时间排开，「上次说什么、这次
   说什么、变没变」自然就有了。规则比较只用来**标注**「改口了」，不删数据。
3. **主题必须来自本场标注词表**（``utterances.topic``），不新造词。词表是
   AI 标注时定下的闭集，跨场才比得起来。

人：优先用声纹认出来的 ``person_id``（跨场稳定）；没有就退回本场昵称，
用 ``name:`` 前缀区分——宁可分成两个人，也不要把两个同名的人合成一个。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from typing import Any

from . import identity, store

# 同一个人在同一主题下，相邻两次主张相似度低于这个值 = 改口了。
# 0.35 是保守值：宁可漏报一次变化，也不要把「换了个说法」说成「自相矛盾」。
REVISION_RATIO = 0.35
# 相邻两次高于这个值 = 基本是同一句话的重复，不算新观点（只在人物页折叠显示）。
SAME_RATIO = 0.55

_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:“”\"'（）()【】\[\]—…~]+")
_GENERIC_TOPICS = {"", "综合", "综合/未归类", "未归类", "其他", "其它", "闲聊"}
# 兜底主题在人物页显示的标签（数组里存的是原始标注，展示时才换）
GENERIC_LABEL = "其他"

# 明确的反对词。观点地图只在原话里真出现了这些词时才标「分歧」——
# 「两个人说法不一样」不等于「一个人反对另一个人」，后者是推断，不做。
_DISAGREE_RE = re.compile(
    r"不同意|不认同|不赞成|我反对|不对|未必|恰恰相反|反过来说|"
    r"不是这样|站不住|说不通|错(?:了|误)")


def topic_label(topic: str) -> str:
    """主题的展示/分组标签：未归类的一律折成「其他」，排到最后。"""
    t = str(topic or "").strip()
    return GENERIC_LABEL if t in _GENERIC_TOPICS else t


def disagrees(text: str) -> bool:
    """这句话有没有在明确反对前面某个人（而不是仅仅换了个说法）。"""
    t = str(text or "").replace("对不对", "").replace("是不是", "")
    return bool(t and _DISAGREE_RE.search(t))


def _norm(text: str) -> str:
    return _PUNCT.sub("", str(text or ""))


def _ratio(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def similarity(a: str, b: str) -> float:
    """两句话的字面相似度（去掉标点/空白后）。追问、提醒、圆桌摘要共用。"""
    return _ratio(a, b)


def topic_specific(topic: str) -> bool:
    """是不是一个能跨场比较的具体主题；「综合/闲聊」比了只会误报。"""
    return str(topic or "").strip() not in _GENERIC_TOPICS


def get(conn: sqlite3.Connection, claim_id: str) -> dict | None:
    """按 claim_id 读一条观点，带上场次标题与日期（分享卡/只读页用）。"""
    cid = str(claim_id or "").strip()
    if not cid:
        return None
    row = conn.execute(
        "SELECT c.*, s.title AS session_title, s.started_at "
        "FROM claims c LEFT JOIN sessions s ON s.id=c.session_id "
        "WHERE c.claim_id=?", (cid,)).fetchone()
    return dict(row) if row else None


def person_key(person_id: str, who: str) -> str:
    """读时分组用的键：认出来的人用 person_id，没认出来的人退回昵称。"""
    pid = str(person_id or "").strip()
    if pid:
        return pid
    name = str(who or "").strip()
    return ("name:" + name) if name else ""


def point_topic(point: dict) -> str:
    """一条要点讲的是什么：取它证据里出现最多的本场主题词。

    为什么不直接用 ``point["title"]``：模型起的标题是给人看的（「两种判断」），
    跨场比不了；``topic`` 是标注阶段定下的闭集，同一件事在不同场次里是同一个词。
    """
    tally: dict[str, int] = {}
    fallback: dict[str, int] = {}
    for e in point.get("evidence") or []:
        t = str(e.get("topic") or "").strip()
        if not t:
            continue
        if t in _GENERIC_TOPICS:
            fallback[t] = fallback.get(t, 0) + 1
        else:
            tally[t] = tally.get(t, 0) + 1
    if tally:
        return max(tally.items(), key=lambda kv: (kv[1], kv[0]))[0]
    # 只有兜底主题时也要给一个：这条观点至少能和别的兜底观点排到一起，
    # 人物页里显示成「其他」也比丢掉强。
    if fallback:
        return max(fallback.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return ""


def point_kind(point: dict) -> str:
    """观点还是问题：证据以提问为主的是「问题」，不是「主张」。"""
    ev = point.get("evidence") or []
    if not ev:
        return "claim"
    asks = sum(1 for e in ev if str(e.get("category") or "") == "question_consult")
    return "question" if asks * 2 > len(ev) else "claim"


def _claim_id(sid: str, idx: int) -> str:
    h = hashlib.sha1(("%s|%d" % (sid, idx)).encode("utf-8", "ignore")).hexdigest()
    return "clm_" + h[:12]


def _people_maps(conn: sqlite3.Connection,
                 sid: str) -> tuple[dict[str, str], dict[str, str]]:
    """本场「显示名/说话人 id -> person_id」，只认已确认/自动认定的。"""
    by_label: dict[str, str] = {}
    by_speaker: dict[str, str] = {}
    for r in conn.execute(
            "SELECT speaker_id, display_name, person_id, person_state FROM speakers "
            "WHERE session_id=? AND person_id IS NOT NULL AND person_id<>''",
            (sid,)):
        state = str(r["person_state"] or "")
        if state not in ("auto", "confirmed"):
            continue
        pid = str(r["person_id"])
        by_speaker[str(r["speaker_id"])] = pid
        name = str(r["display_name"] or "").strip()
        if name:
            by_label.setdefault(name, pid)
    return by_label, by_speaker


def extract(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """从这一场最新的总结里抽出观点原子。没有总结返回空列表。"""
    payload = {}
    row = store.latest_summary(conn, sid, "content")
    if row:
        payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    points = payload.get("points") or []
    if not points:
        return []
    people, people_by_speaker = _people_maps(conn, sid)
    out: list[dict] = []
    for i, p in enumerate(points):
        stance = str(p.get("stance") or "").strip()
        gist = str(p.get("gist") or "").strip()
        # 没有主张句的要点不构成观点（只有标题的空壳不值得进档案）
        if not stance and not gist:
            continue
        raw_who = str(p.get("claim_speaker") or "").strip()
        # 「嘉宾1 / 聚类标签 09 / 主播」是本场占位，不配跨场署名；落库为空，
        # 展示时再由 identity 兜底成「本场第 N 位上麦者」。
        who, who_kind = identity.clean_name(raw_who)
        if who_kind != "nickname":
            who = ""
        ev = p.get("evidence") or []
        first = ev[0] if ev else {}
        pid = people.get(who, "") if who else ""
        if not pid:
            # 只在证据里找到「claim_speaker 本人」那一行再取 person_id。
            # 直接拿第一条证据会把嘉宾的观点记到先开口的主播名下。
            owner = identity.owner_evidence(ev, raw_who)
            if owner:
                pid = people_by_speaker.get(str(owner.get("speaker_id") or ""), "")
        out.append({
            "claim_id": _claim_id(sid, i),
            "session_id": sid,
            "person_id": pid,
            "who": who,
            "topic": point_topic(p),
            "stance": (stance or gist)[:120],
            "gist": gist[:300],
            "quote": str(first.get("quote") or first.get("text") or "")[:300],
            "t": first.get("t_start"),
            "kind": point_kind(p),
            "evidence": [{"id": e.get("id"), "t": e.get("t_start"),
                          "speaker": str(e.get("speaker_label") or ""),
                          "speaker_id": str(e.get("speaker_id") or "")}
                         for e in ev[:6]],
        })
    return out


def rebuild_session(conn: sqlite3.Connection, sid: str) -> int:
    """整场重建观点原子。幂等；没有总结时清空（不留上一次的旧观点）。"""
    atoms = extract(conn, sid)
    conn.execute("DELETE FROM claims WHERE session_id=?", (sid,))
    if atoms:
        ts = store.now()
        conn.executemany(
            "INSERT INTO claims(claim_id,session_id,person_id,who,topic,stance,gist,"
            "quote,t,kind,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(a["claim_id"], a["session_id"], a["person_id"], a["who"], a["topic"],
              a["stance"], a["gist"], a["quote"], a["t"], a["kind"],
              json.dumps(a["evidence"], ensure_ascii=False), ts) for a in atoms])
    conn.commit()
    return len(atoms)


def rows_for(conn: sqlite3.Connection, *, person_id: str = "", who: str = "",
             topic: str = "", session_id: str = "", since: str = "",
             limit: int = 500) -> list[dict]:
    """按人/主题/场次读观点原子，带上场次标题与日期（人物页、检索共用）。"""
    where, args = [], []
    if person_id:
        where.append("c.person_id=?")
        args.append(person_id)
    elif who:
        where.append("c.person_id='' AND c.who=?")
        args.append(who)
    if topic:
        where.append("c.topic=?")
        args.append(topic)
    if session_id:
        where.append("c.session_id=?")
        args.append(session_id)
    if since:
        where.append("COALESCE(s.started_at,'')>=?")
        args.append(since)
    scope = (" WHERE " + " AND ".join(where)) if where else ""
    return [dict(r) for r in conn.execute(
        "SELECT c.*, s.title AS session_title, s.started_at FROM claims c "
        "LEFT JOIN sessions s ON s.id=c.session_id" + scope +
        " ORDER BY COALESCE(s.started_at,'') ASC, c.t ASC, c.claim_id LIMIT ?",
        args + [max(1, int(limit))])]


def _topic_group(items: list[dict]) -> dict:
    """把一个 (人, 主题) 下的原子按时间排开，标出「改口」和「重复」。"""
    items = sorted(items, key=lambda r: (str(r.get("started_at") or ""),
                                         float(r.get("t") or 0)))
    revs: list[dict] = []
    prev = ""
    for r in items:
        s = str(r.get("stance") or "")
        if prev:
            ratio = _ratio(prev, s)
            r["_same"] = ratio >= SAME_RATIO
            r["_changed"] = ratio < REVISION_RATIO
            if r["_changed"]:
                revs.append({"at": r.get("started_at") or "",
                             "from": prev, "to": s,
                             "session_id": r.get("session_id") or ""})
        else:
            r["_same"] = False
            r["_changed"] = False
        prev = s
    latest = items[-1] if items else {}
    return {
        "topic": str(latest.get("topic") or ""),
        "n": len(items),
        "n_sessions": len({r.get("session_id") for r in items}),
        "first_at": str(items[0].get("started_at") or "") if items else "",
        "last_at": str(latest.get("started_at") or ""),
        "latest": {"session_id": latest.get("session_id") or "",
                   "title": latest.get("session_title") or "",
                   "stance": latest.get("stance") or "",
                   "t": latest.get("t"),
                   "claim_id": latest.get("claim_id") or ""},
        "revisions": revs,
        "items": items,
    }


def profile(conn: sqlite3.Connection, person_id: str = "", who: str = "",
            limit: int = 500) -> dict:
    """一个人的观点档案：按主题分组的全部主张 + 改口记录。

    ``who`` 是没有 person_id 时的退路（聚类标签场次里没认出来的人）。
    """
    rows = rows_for(conn, person_id=person_id, who=who, limit=limit)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = str(r.get("topic") or "")
        groups.setdefault(key, []).append(r)
    topics = [_topic_group(v) for v in groups.values()]
    # 观点多的主题在前；没主题（词表里没有）的放最后，而不是丢掉。
    topics.sort(key=lambda g: (not bool(g["topic"]), -g["n_sessions"], -g["n"],
                               str(g["topic"])))
    return {"topics": topics, "n_claims": len(rows),
            "n_topics": len([g for g in topics if g["topic"]])}


def history_index(conn: sqlite3.Connection, keys: set[str],
                  topics: set[str]) -> dict[tuple[str, str], dict]:
    """笔记页用：这批「人 × 主题」在本场之外还有多少场。

    只返回 ``n_sessions > 1`` 的键——只出现在这一场的观点不需要多一个徽标。
    ``keys`` 是 ``person_key()`` 的结果，``topics`` 是主题词。
    """
    keys = {k for k in keys if k}
    topics = {t for t in topics if t}
    if not keys or not topics:
        return {}
    # 「name:阿哲」里的昵称要拿去和 claims.who 比，person_id 则原样比。
    pids = sorted(k for k in keys if not k.startswith("name:"))
    whos = sorted(k[len("name:"):] for k in keys if k.startswith("name:"))
    conds, args = [], []
    if pids:
        conds.append("c.person_id IN (" + ",".join("?" * len(pids)) + ")")
        args.extend(pids)
    if whos:
        # 有 person_id 的行也要按昵称能对上：笔记页分组时手里没有 person_id。
        conds.append("c.who IN (" + ",".join("?" * len(whos)) + ")")
        args.extend(whos)
    ph_t = ",".join("?" * len(topics))
    rows = conn.execute(
        "SELECT c.person_id, c.who, c.topic, c.session_id, s.started_at "
        "FROM claims c LEFT JOIN sessions s ON s.id=c.session_id "
        "WHERE c.topic IN (" + ph_t + ") AND (" + " OR ".join(conds) + ")",
        list(topics) + args).fetchall()
    acc: dict[tuple[str, str], dict] = {}
    for r in rows:
        topic = str(r["topic"] or "")
        # 同一个人的两种键都登记：笔记页按昵称分组时手里没有 person_id，
        # 人物页/检索按 person_id 查。少登记一个，另一种视图就会漏掉场次。
        candidates = {(person_key(str(r["person_id"] or ""), str(r["who"] or "")),
                       topic)}
        if str(r["person_id"] or ""):
            candidates.add((person_key("", str(r["who"] or "")), topic))
        for key in candidates:
            cur = acc.setdefault(key, {"sessions": set(), "first_at": "",
                                       "last_at": ""})
            cur["sessions"].add(str(r["session_id"] or ""))
            at = str(r["started_at"] or "")
            if at:
                cur["first_at"] = (min(cur["first_at"], at) if cur["first_at"] else at)
                cur["last_at"] = max(cur["last_at"], at) if cur["last_at"] else at
    out: dict[tuple[str, str], dict] = {}
    for key, v in acc.items():
        if len(v["sessions"]) <= 1:
            continue
        out[key] = {"n_sessions": len(v["sessions"]),
                    "first_at": v["first_at"], "last_at": v["last_at"]}
    return out


def refresh_person_names(conn: sqlite3.Connection, person_id: str,
                         name: str) -> int:
    """人改名之后，把档案里那些没认出人、但署名正好是这个人的旧观点接回去。

    为什么需要：``who`` 存的是当时的本场署名，认人之前是昵称、之后可能改成
    人名。改名后如果不刷新，人物页会出现「一半观点挂在 person_id 下、
    一半还挂在旧昵称下」，看起来像丢数据。
    """
    name = str(name or "").strip()
    if not name:
        return 0
    cur = conn.execute(
        "UPDATE claims SET who=? WHERE person_id=? AND who<>?", (name, person_id, name))
    conn.commit()
    return cur.rowcount


def revisions_for(conn: sqlite3.Connection, person_id: str, topic: str,
                  limit: int = 20) -> list[dict]:
    """跨场改口记录（人物页/主动提醒共用）。"""
    prof = profile(conn, person_id=person_id, limit=limit * 20)
    for g in prof["topics"]:
        if g["topic"] == topic:
            return g["revisions"]
    return []
