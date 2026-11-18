# -*- coding: utf-8 -*-
"""主动提醒：观点反转、老问题有新答案、常客上麦（docs/41 §4.5）。

只做这三件事，而且守两条边界：

1. **默认关**：``settings.insight_enabled`` 为 False 时 ``notify.enabled``
   直接拒绝所有 ``insight_*``，不会产生任何通知。
2. **每场最多一条**：计数在 ``notifications`` 表里按 ``session_id`` 做，
   「常客上麦」在记录中就发过了，收尾时就不再补「观点反转」——宁可错过
   第二条，也不要在同一场里连弹几次。

不判断谁对谁错：文案只陈述「和上次不一样」「上次没人答，这场有人答了」
「TA 在你这儿讲过多少」，对错留给用户。
"""
from __future__ import annotations

import sqlite3

from . import claims, notify, store

#: 常客门槛：至少留下过这么多条观点才提醒「TA 上麦了」。
FREQUENT_CLAIMS = 5
#: 问题与答案的字面/关键词重合达到这个值，才算「这个问题被回答了」。
ANSWER_RATIO = 0.3
ANSWER_COVERAGE = 0.25


def _key(row: dict) -> str:
    return claims.person_key(str(row.get("person_id") or ""),
                             str(row.get("who") or ""))


def _rows_for_key(conn: sqlite3.Connection, key: str, topic: str,
                  limit: int = 200) -> list[dict]:
    if not key:
        return []
    if key.startswith("name:"):
        return claims.rows_for(conn, who=key[len("name:"):], topic=topic,
                               limit=limit)
    return claims.rows_for(conn, person_id=key, topic=topic, limit=limit)


def _count(conn: sqlite3.Connection, sid: str) -> int:
    """这一场已经发过几条主动提醒（三种类型合计）。"""
    if not sid:
        return 0
    marks = ",".join("?" * len(notify.INSIGHT_KINDS))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE session_id=? "
        "AND kind IN (" + marks + ")", (sid,) + notify.INSIGHT_KINDS).fetchone()
    return int(row["n"] or 0) if row else 0


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text or "")
                   if ch.isalnum() or "一" <= ch <= "鿿")


def _coverage(question: str, answer: str) -> float:
    """问题里的二字词有多大比例出现在答案里。

    不直接用 difflib：问题和答案的句式天然不同（「抽成怎么算」对「抽成是
    阶梯的」），字面相似度会低到几乎总是 0。二字词覆盖率对中文更稳。
    """
    q, a = _norm(question), _norm(answer)
    if len(q) < 2 or not a:
        return 0.0
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in a) / float(len(grams))


def _answers(question: str, answer: str) -> bool:
    if not question or not answer:
        return False
    return (claims.similarity(question, answer) >= ANSWER_RATIO
            or _coverage(question, answer) >= ANSWER_COVERAGE)


def _day(at: str) -> str:
    s = str(at or "")
    return s[5:10].replace("-", "-") if len(s) >= 10 else "上一次"


def _reversal(conn: sqlite3.Connection, sid: str,
              session: dict) -> dict | None:
    """本场哪条新观点和这个人在更早场次里的说法相反。"""
    before = str(session.get("started_at") or "")
    best: dict | None = None
    for cur in claims.rows_for(conn, session_id=sid, limit=500):
        if str(cur.get("kind") or "") != "claim":
            continue
        topic = str(cur.get("topic") or "")
        stance = str(cur.get("stance") or "").strip()
        key = _key(cur)
        if not key or not stance or not claims.topic_specific(topic):
            continue
        hist = [r for r in _rows_for_key(conn, key, topic)
                if str(r.get("session_id") or "") != sid
                and (not before or str(r.get("started_at") or "") < before)]
        if not hist:
            continue
        prev = hist[-1]              # rows_for 按时间升序
        old = str(prev.get("stance") or "").strip()
        if not old:
            continue
        ratio = claims.similarity(old, stance)
        if ratio >= claims.REVISION_RATIO:
            continue
        if best is None or ratio < best["ratio"]:
            best = {"ratio": ratio, "who": str(cur.get("who") or "他"),
                    "topic": topic, "old": old[:120], "new": stance[:120],
                    "claim_id": str(cur.get("claim_id") or ""),
                    "t": cur.get("t"), "prev_at": str(prev.get("started_at") or ""),
                    "prev_session": str(prev.get("session_id") or "")}
    return best


def _answered(conn: sqlite3.Connection, sid: str,
              session: dict) -> dict | None:
    """本场有没有回答一个「以前没人答」的问题。"""
    before = str(session.get("started_at") or "")
    current = [c for c in claims.rows_for(conn, session_id=sid, limit=500)
               if str(c.get("kind") or "") == "claim"
               and str(c.get("stance") or "").strip()
               and claims.topic_specific(str(c.get("topic") or ""))]
    if not current:
        return None
    questions = [dict(r) for r in conn.execute(
        "SELECT c.*, s.started_at FROM claims c "
        "LEFT JOIN sessions s ON s.id=c.session_id "
        "WHERE c.kind='question' AND c.session_id<>? "
        "AND COALESCE(s.started_at,'')<? "
        "ORDER BY COALESCE(s.started_at,'') ASC, c.t ASC LIMIT 300",
        (sid, before or "9999"))]
    best: dict | None = None
    for q in questions:
        topic = str(q.get("topic") or "")
        qtext = str(q.get("stance") or "").strip()
        if not topic or not qtext or not claims.topic_specific(topic):
            continue
        # 这个问题后来（本场之前）有没有被谁答过？答过就不算「上次没人答」。
        later = [dict(r) for r in conn.execute(
            "SELECT c.*, s.started_at FROM claims c "
            "LEFT JOIN sessions s ON s.id=c.session_id "
            "WHERE c.kind='claim' AND c.topic=? AND c.session_id<>? "
            "AND COALESCE(s.started_at,'')>? AND COALESCE(s.started_at,'')<? "
            "LIMIT 200", (topic, sid, str(q.get("started_at") or ""),
                          before or "9999"))]
        if any(_answers(qtext, str(r.get("stance") or "")) for r in later):
            continue
        for cur in current:
            if str(cur.get("topic") or "") != topic:
                continue
            if not _answers(qtext, str(cur.get("stance") or "")):
                continue
            score = max(claims.similarity(qtext, str(cur.get("stance") or "")),
                        _coverage(qtext, str(cur.get("stance") or "")))
            if best is None or score > best["score"]:
                best = {"score": score, "question": qtext[:120],
                        "who": str(cur.get("who") or "有人"),
                        "answer": str(cur.get("stance") or "")[:140],
                        "topic": topic, "t": cur.get("t"),
                        "claim_id": str(cur.get("claim_id") or ""),
                        "asked_at": str(q.get("started_at") or ""),
                        "asked_session": str(q.get("session_id") or "")}
    return best


def after_finalize(conn: sqlite3.Connection, sid: str) -> dict | None:
    """一场总结完成后的主动提醒。每场最多一条；默认关。"""
    if not any(notify.enabled(k) for k in notify.INSIGHT_KINDS):
        return None
    if _count(conn, sid) >= 1:
        return None
    session = store.get_session(conn, sid) or {}
    try:
        rev = _reversal(conn, sid, session)
    except Exception:
        rev = None
    if rev:
        return notify.emit(
            "insight_reversal", "观点有变化",
            "%s 在「%s」上的说法和 %s 不一样：「%s」→「%s」。要回看对比吗？"
            % (rev["who"], rev["topic"], _day(rev["prev_at"]),
               rev["old"], rev["new"]),
            session_id=sid, level="info",
            action={"view": "note", "session_id": sid,
                    "t": rev.get("t"), "claim_id": rev["claim_id"]},
            dedupe_key="insight_reversal:" + sid)
    try:
        ans = _answered(conn, sid, session)
    except Exception:
        ans = None
    if ans:
        return notify.emit(
            "insight_answer", "老问题有答案了",
            "「%s」以前没人答，这场 %s 答了：%s"
            % (ans["question"], ans["who"], ans["answer"]),
            session_id=sid, level="info",
            action={"view": "note", "session_id": sid,
                    "t": ans.get("t"), "claim_id": ans["claim_id"]},
            dedupe_key="insight_answer:" + sid)
    return None


def speaker_appeared(conn: sqlite3.Connection, sid: str, person_id: str,
                     name: str) -> dict | None:
    """记录中认出一个常客。每场最多一条；不知道名字就不发。"""
    pid = str(person_id or "").strip()
    who = str(name or "").strip()
    if not pid or not who or not notify.enabled("insight_speaker"):
        return None
    if _count(conn, sid) >= 1:
        return None
    row = conn.execute(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS ns "
        "FROM claims WHERE person_id=?", (pid,)).fetchone()
    n = int(row["n"] or 0) if row else 0
    if n < FREQUENT_CLAIMS:
        return None
    last = conn.execute(
        "SELECT c.session_id, s.title, s.started_at FROM claims c "
        "LEFT JOIN sessions s ON s.id=c.session_id "
        "WHERE c.person_id=? AND c.session_id<>? "
        "ORDER BY COALESCE(s.started_at,'') DESC LIMIT 1", (pid, sid)).fetchone()
    tail = ""
    if last and last["title"]:
        tail = "，最近一次是《%s》" % str(last["title"])[:40]
    return notify.emit(
        "insight_speaker", "%s 上麦了" % who,
        "TA 在你这儿有 %d 条观点，跨 %d 场%s。"
        % (n, int(row["ns"] or 0), tail),
        session_id=sid, level="info",
        action={"view": "people", "person_id": pid},
        dedupe_key="insight_speaker:%s:%s" % (sid, pid))
