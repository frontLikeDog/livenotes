# -*- coding: utf-8 -*-
"""问笔记：只用本机笔记做检索，答案必须带可点引用。

P1 不引入向量库（docs/36 §8.2）：``note_units`` 的 FTS5 trigram + 类型权重
+ 新鲜度就够回答「上周阿哲讲了哪些选品方法」这类问题。模型只看到检索到的
片段（每条 ≤200 字、总预算 ≤3000 tokens），不发送整场逐字稿和音频。

证据门：模型返回的 citations 必须是给定单元的子集；引用非法就剔除，全空
则失败。没有配置模型、模型调用失败或输出不合格时，问答直接失败并给出
原因，不再拼一段本地摘录。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import claims, glossary, llm, logs, notes, scheduler, settings, store

PROMPT_VERSION = "qa-v2"      # v2：追问带上一轮回答作为上下文
TOP_K = 10
TOKEN_BUDGET = 3000
MAX_UNIT_CHARS = 200
CACHE_HOURS = 24
PREV_ANSWER_CHARS = 200        # 只带上一轮答案摘要，不塞整段对话（docs/41 §4.3）
FOLLOWUP_LIMIT = 3


def _top_k() -> int:
    try:
        from . import config
        return int(config.perf()["effective"].get("qa_top_k") or TOP_K)
    except Exception:
        return TOP_K


def _token_budget() -> int:
    try:
        from . import config
        return int(config.perf()["effective"].get("qa_tokens") or TOKEN_BUDGET)
    except Exception:
        return TOKEN_BUDGET


def _scope_json(question: str, session_id: str, parent_id: int = 0) -> dict:
    scope = notes.parse_time_scope(question)
    if session_id:
        scope["session_id"] = session_id
    # 追问的答案依赖上一轮上下文：缓存键必须带上 parent_id，否则同一个问题
    # 从另一条线程问出来会错误命中上一条线程的缓存。
    if parent_id:
        scope["parent_id"] = int(parent_id)
    return scope


def _recency(started_at: str) -> float:
    try:
        t = datetime.fromisoformat(str(started_at))
        days = max(0.0, (datetime.now() - t.replace(tzinfo=None)).total_seconds()
                   / 86400.0)
        return 0.15 if days <= 30 else (0.07 if days <= 90 else 0.0)
    except (TypeError, ValueError, OSError):
        return 0.0


def _claim_units(conn: sqlite3.Connection, question: str, scope: dict,
                 top_k: int) -> list[dict]:
    """按「人 × 主题」召回观点原子，转成和 note_units 同形的单元。

    为什么单独一条通道：观点原子只是从要点里抽出来的字段，正文里不一定
    还留着问句里的词。问「阿哲怎么看待 OPC」时，句子里没有「看待」，
    FTS 未必命中；但 ``claims`` 里有 (阿哲, OPC 政策) 这一条，直接读即可。
    只在问题里真的出现了某个主题词时才走这里，避免把无关问题带偏。
    """
    topics = [t for t in store.claim_topics(conn) if t and t in question]
    if not topics:
        return []
    rows = []
    for t in topics[:3]:
        rows.extend(claims.rows_for(conn, topic=t, limit=top_k * 6))
    if not rows:
        return []
    since, until = str(scope.get("since") or ""), str(scope.get("until") or "")
    out: list[dict] = []
    for r in rows:
        at = str(r.get("started_at") or "")
        if since and at < since:
            continue
        if until and at >= until:
            continue
        out.append({
            "id": 0, "session_id": str(r.get("session_id") or ""),
            "kind": "claim", "ref_id": str(r.get("claim_id") or ""),
            "t": r.get("t"), "title": str(r.get("session_title") or ""),
            "speaker": str(r.get("who") or ""),
            "text": ("%s：%s" % (r.get("who") or "", r.get("stance") or "")).strip("："),
            "weight": 1.3, "started_at": at,
        })
    return out


def retrieve(conn: sqlite3.Connection, question: str, scope: dict,
             top_k: int | None = None) -> list[dict]:
    """召回 + 排序。返回带 ``n``（引用序号）的单元列表。"""
    top_k = top_k or _top_k()
    sid = str(scope.get("session_id") or "")
    claim_units = _claim_units(conn, question, scope, top_k) if not sid else []
    if sid:
        # 「问这场」优先取本场全部要点/高光，不让其他场挤掉。
        units = store.note_units_for_session(conn, sid, 80)
        if not units:
            notes.index_session(conn, sid)
            units = store.note_units_for_session(conn, sid, 80)
    else:
        expanded = glossary.expand_query(question) or [question]
        units = store.search_note_units(
            conn, expanded, limit=top_k * 4,
            since=str(scope.get("since") or ""),
            until=str(scope.get("until") or ""))
        # 观点原子排前面：它是「谁的主张」，比「哪场笔记里提到过」更接近问题。
        if claim_units:
            units = claim_units + units
    if not units:
        return []
    scored: list[tuple[float, dict]] = []
    qterms = [t for t in re.split(r"[\s，。？！、,.?!]+", question) if len(t) >= 2]
    for u in units:
        rank = float(u.get("rank") or 0.0)
        text = str(u.get("text") or "")
        title = str(u.get("title") or "")
        coverage = sum(1 for t in qterms if t in text or t in title)
        score = (-rank if rank < 0 else 0.0) + float(u.get("weight") or 1.0) * 0.2
        score += _recency(str(u.get("started_at") or ""))
        score += min(4, coverage) * 0.15
        if sid:
            score += 0.4
        scored.append((score, u))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 同一场最多 3 条（问单场时不限制）
    per_session: dict[str, int] = {}
    picked: list[dict] = []
    for _score, u in scored:
        key = str(u.get("session_id") or "")
        if not sid and per_session.get(key, 0) >= 3:
            continue
        per_session[key] = per_session.get(key, 0) + 1
        u = dict(u)
        u["text"] = str(u.get("text") or "")[:MAX_UNIT_CHARS]
        picked.append(u)
        if len(picked) >= top_k:
            break
    # 预算：超出就按分数（已排序）截断
    budget = _token_budget()
    used, out = 0, []
    for u in picked:
        cost = llm.estimate_tokens(str(u.get("text") or "")) + 20
        if out and used + cost > budget:
            break
        used += cost
        out.append(u)
    for i, u in enumerate(out, 1):
        u["n"] = i
    return out


def _fmt_t(t: float | None) -> str:
    try:
        t = max(0, int(float(t or 0)))
        return "%02d:%02d" % (t // 60 % 60, t % 60) if t < 3600 else \
            "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)
    except (TypeError, ValueError):
        return ""


def _citation(u: dict) -> dict:
    return {"n": u.get("n"), "unit_id": int(u.get("id") or 0),
            "session_id": str(u.get("session_id") or ""),
            # ref_id 是观点原子的 claim_id（要点是序号）：追问要据此回查
            # 「谁在什么主题上说过」，只靠标题和摘要不够。
            "ref_id": str(u.get("ref_id") or ""),
            "title": str(u.get("title") or ""),
            "kind": str(u.get("kind") or ""),
            "t": u.get("t"), "t_text": _fmt_t(u.get("t")),
            "speaker": str(u.get("speaker") or ""),
            "text": str(u.get("text") or "")}


def _context(units: list[dict]) -> str:
    lines = []
    for u in units:
        who = (" · " + str(u.get("speaker"))) if u.get("speaker") else ""
        t = _fmt_t(u.get("t"))
        when = (" @" + t) if t else ""
        lines.append("[%d] %s%s%s：%s" % (
            int(u["n"]), str(u.get("title") or "笔记"), who, when,
            str(u.get("text") or "")))
    return "\n".join(lines)


_QA_PROMPT = """你是 LiveNotes 的笔记助手。只根据下面给出的笔记片段回答问题。

规则：
1. 每条结论后面用 [n] 标出依据的片段编号；不要编造片段里没有的信息。
2. 如果片段不足以回答，回答「没找到相关内容」，citations 返回空数组。
3. 用中文、口语化、不超过 4 句话。
%s%s
只输出 JSON：{"answer":"...","citations":[1,2],"confidence":"high|medium|low"}

笔记片段：
%s
"""

_THREAD_RULE = """
4. 这是用户在上一轮回答后的追问。上一轮问答只用来理解「他在问什么」，
   不能当作证据；结论仍然只能引用下面的笔记片段。
"""


def _thread_block(conn: sqlite3.Connection, row: dict) -> tuple[str, str]:
    """追问上下文：上一轮问题 + 答案摘要 + 引用 id。

    只带 200 字答案和一串引用编号，不带全文：本地单机、预算敏感
    （docs/41 §4.3）。父问答已被清理时退化成普通问答，不报错。
    """
    pid = int(row.get("parent_id") or 0)
    if not pid:
        return "", ""
    parent = store.qa_get(conn, pid)
    if not parent:
        return "", ""
    answer = str(parent.get("answer") or "").strip()
    if not answer and str(parent.get("state") or "") == "no_result":
        answer = "（上一轮没有找到相关内容）"
    cites = []
    for c in (parent.get("citations") or [])[:6]:
        if not isinstance(c, dict):
            continue
        bits = [str(c.get("kind") or ""), str(c.get("speaker") or ""),
                str(c.get("title") or "")]
        label = " · ".join(b for b in bits if b)
        cites.append("[%s] %s" % (c.get("n") or "?", label))
    block = ("上一轮问题：%s\n上一轮回答：%s%s\n" % (
        str(parent.get("question") or "")[:200],
        answer[:PREV_ANSWER_CHARS],
        ("\n上一轮引用：" + "；".join(cites)) if cites else ""))
    return block, _THREAD_RULE


def _fail(conn: sqlite3.Connection, qid: int, why: str,
          retry: bool = False) -> dict:
    """问答失败就失败，不再拿本地摘录凑一段答案。"""
    msg = (why or "模型没有生成可用回答").strip()
    if not msg.endswith(("。", "！", "？")):
        msg += "。"
    msg += "未使用本地抽取"
    if retry:
        msg += "，请重试"
    store.qa_update(conn, qid, state="failed", answer="", citations=[],
                    engine="", error=msg)
    logs.log_event("qa.failed", level="warn", error=msg[:200])
    return {"ok": False, "state": "failed", "error": msg, "items": []}


def answer(conn: sqlite3.Connection, qid: int) -> dict:
    """执行一条问答。由调度器 worker 调用；同步、可重试。"""
    row = store.qa_get(conn, qid)
    if not row:
        return {"ok": False, "error": "问题不存在"}
    question = str(row.get("question") or "")
    scope = dict(row.get("scope") or {})
    try:
        units = retrieve(conn, question, scope)
    except Exception as e:
        logs.log_event("qa.retrieve_failed", level="warn", error=str(e)[:200])
        units = []
    if not units:
        store.qa_update(conn, qid, state="no_result", answer="",
                        citations=[], engine="")
        return {"ok": True, "state": "no_result", "items": []}

    thread_block, thread_rule = _thread_block(conn, row)
    try:
        configured = bool(settings.resolve().get("configured"))
    except Exception:
        configured = False
    if not configured:
        return _fail(conn, qid, "还没配置大模型")

    try:
        raw = llm.chat([
            {"role": "system",
             "content": "你只根据用户提供的笔记片段回答，输出严格 JSON。"},
            {"role": "user", "content": _QA_PROMPT % (
                thread_block, thread_rule, _context(units))},
        ], temperature=0.2, session_id=str(scope.get("session_id") or ""),
            task="qa", max_tokens=800)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        ans = str(data.get("answer") or "").strip()
        wanted = data.get("citations") or []
        by_n = {int(u["n"]): u for u in units}
        valid: list[dict] = []
        for n in wanted:
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            if n in by_n and by_n[n] not in valid:
                valid.append(by_n[n])
        # 证据门：答案里引用了不存在的编号也剔掉；全空就失败。
        ans = re.sub(r"\[(\d+)\]",
                     lambda m: m.group(0) if int(m.group(1)) in
                     {int(u["n"]) for u in valid} else "", ans).strip()
        if ans == "没找到相关内容":
            store.qa_update(conn, qid, state="no_result", answer=ans,
                            citations=[], engine="llm")
            return {"ok": True, "state": "no_result", "items": []}
        if not ans or not valid:
            return _fail(conn, qid, "模型输出没有通过证据校验")
        text, cites, engine = ans, [_citation(u) for u in valid], "llm"
    except Exception as e:
        logs.log_event("qa.llm_failed", level="warn", error=str(e)[:200])
        text = str(e).strip()
        if isinstance(e, scheduler.BudgetExceeded):
            return _fail(conn, qid, "预算不足（%s）" % (
                text[:120] if text else "模型额度已用完"))
        return _fail(conn, qid, "模型调用失败（%s）" % (
            text[:120] if text else type(e).__name__), retry=True)
    store.qa_update(conn, qid, state="ready", answer=text,
                    citations=cites, engine=engine)
    logs.log_event("qa.answer", engine=engine, citations=len(cites),
                   units=len(units))
    out = {"ok": True, "state": "ready", "answer": text,
           "citations": cites, "engine": engine}
    try:
        out["followups"] = suggest_followups(conn, store.qa_get(conn, qid) or {})
    except Exception:
        out["followups"] = []
    return out


def _claim_rows_for_key(conn: sqlite3.Connection, key: str,
                        topic: str = "") -> list[dict]:
    if not key:
        return []
    if key.startswith("name:"):
        return claims.rows_for(conn, who=key[len("name:"):], topic=topic, limit=200)
    return claims.rows_for(conn, person_id=key, topic=topic, limit=200)


def suggest_followups(conn: sqlite3.Connection, row: dict) -> list[dict]:
    """基于这条答案的 2~3 个追问。规则拼接，0 token（docs/41 §4.3）。

    追问的价值在于「指向下一步动作」，不在措辞漂亮：所以每条都带一个
    ``why``，说明它为什么值得接着问。点击后问答会带上一轮答案作上下文。
    """
    try:
        if not settings.prefs().get("qa_followups_enabled", True):
            return []
    except Exception:
        pass
    if not row or str(row.get("state") or "") != "ready":
        return []
    cites = [c for c in (row.get("citations") or []) if isinstance(c, dict)]
    if not cites:
        return []
    claim_rows: list[dict] = []
    for c in cites:
        if str(c.get("kind") or "") == "claim" and c.get("ref_id"):
            got = claims.get(conn, str(c["ref_id"]))
            if got:
                claim_rows.append(got)
    top = claim_rows[0] if claim_rows else {}
    topic = str(top.get("topic") or "")
    key = claims.person_key(str(top.get("person_id") or ""),
                            str(top.get("who") or ""))
    speaker = next((str(c.get("speaker") or "").strip()
                    for c in cites if str(c.get("speaker") or "").strip()), "")
    asked = str(row.get("question") or "").strip()
    out: list[dict] = []

    def add(question: str, why: str) -> None:
        q = str(question or "").strip()
        if not q or len(q) > 80 or q == asked:
            return
        if any(x["q"] == q for x in out):
            return
        out.append({"q": q, "why": why})

    specific = bool(topic) and claims.topic_specific(topic)
    if str(top.get("kind") or "") == "question" and specific:
        add("「%s」后来有人回答吗？" % topic, "这是一条还没答案的问题")

    # 同一个人、同一个主题的跨场主张：最像「接着聊」的方向。
    if key and specific:
        hist = [r for r in _claim_rows_for_key(conn, key, topic)
                if str(r.get("session_id") or "") != str(top.get("session_id") or "")]
        n_sessions = len({str(r.get("session_id") or "") for r in hist})
        if n_sessions >= 1:
            name = str(top.get("who") or speaker or "他")
            add("%s 在「%s」上还讲过什么？" % (name, topic),
                "同一个人、同一个主题，跨 %d 场" % (n_sessions + 1))

    # 同一场里同一个主题、不同的人：如果说法差得远，给一个「对比」入口。
    if specific:
        sid = str(top.get("session_id") or "")
        if sid:
            same = [r for r in claims.rows_for(conn, session_id=sid, limit=200)
                    if str(r.get("topic") or "") == topic
                    and str(r.get("kind") or "") == "claim"]
            for other in same:
                okey = claims.person_key(str(other.get("person_id") or ""),
                                         str(other.get("who") or ""))
                if not okey or okey == key:
                    continue
                if claims.similarity(str(other.get("stance") or ""),
                                     str(top.get("stance") or "")) < 0.2:
                    add("%s 对「%s」怎么看？" % (str(other.get("who") or "另一位"), topic),
                        "同一场里两人的说法不一样")
                    break

    sessions = {str(c.get("session_id") or "") for c in cites if c.get("session_id")}
    if len(sessions) > 1 and specific:
        add("「%s」在这些场里的说法有变化吗？" % topic, "引用来自 %d 个场次" % len(sessions))

    stance = str(top.get("stance") or "")
    if stance and re.search(r"明年|以后|未来|预计|将要|下一步|会", stance):
        add("「%s」后来有验证吗？" % (topic or asked[:20]), "这条像是对之后情况的判断")

    if specific:
        add("关于「%s」，最早是哪一场说的？" % topic, "把这条放回时间线")
    elif speaker:
        add("%s 在别的场还讲过什么？" % speaker, "顺着这个人继续看")
    return out[:FOLLOWUP_LIMIT]


def followups_for(conn: sqlite3.Connection, qid: int) -> list[dict]:
    return suggest_followups(conn, store.qa_get(conn, qid) or {})


def _cached(conn: sqlite3.Connection, question: str, scope: dict) -> dict | None:
    cutoff = (datetime.now() - timedelta(hours=CACHE_HOURS)).isoformat(
        timespec="seconds")
    scope_key = json.dumps(scope, sort_keys=True, ensure_ascii=False)
    row = conn.execute(
        "SELECT * FROM qa_answers WHERE question=? AND state IN ('ready','no_result') "
        "AND created_at>=? ORDER BY id DESC LIMIT 1",
        (question[:500], cutoff)).fetchone()
    if not row:
        return None
    d = store._qa_row(dict(row))
    try:
        if json.dumps(d.get("scope") or {}, sort_keys=True,
                      ensure_ascii=False) != scope_key:
            return None
    except (TypeError, ValueError):
        return None
    # 索引更新后旧答案失效
    meta = conn.execute("SELECT updated_at FROM index_meta "
                        "WHERE key='note_units_fts'").fetchone()
    if meta and str(meta["updated_at"] or "") > str(d.get("created_at") or ""):
        return None
    return d


def ask(question: str, session_id: str = "", limit: int = 0,
        thread_id: str = "", parent_id: int = 0) -> dict:
    """提交问题。有 24h 缓存就直接返回，否则入队并立即返回 running。

    ``parent_id`` 不为 0 时是追问：只把上一轮答案摘要送进模型（见
    ``_thread_block``），并把 ``thread_id`` 归到首问，方便以后按线程回看。
    """
    question = str(question or "").strip()[:500]
    if not question:
        raise ValueError("问题不能为空")
    conn = store.connect()
    try:
        parent = store.qa_get(conn, int(parent_id)) if parent_id else None
        if parent:
            parent_id = int(parent["id"])
            thread_id = str(parent.get("thread_id") or "") or str(parent["id"])
            if not parent.get("thread_id"):
                # 首问第一次被追问时补上线程号：之后「最近问过」和排查
                # 都能按 thread_id 把一轮对话串起来。
                conn.execute("UPDATE qa_answers SET thread_id=? WHERE id=?",
                             (thread_id, parent_id))
                conn.commit()
        else:
            parent_id = 0
            thread_id = str(thread_id or "")
        scope = _scope_json(question, session_id, parent_id)
        cached = _cached(conn, question, scope)
        if cached:
            return {"ok": True, "cached": True, "id": int(cached["id"]),
                    "state": cached["state"], "answer": cached.get("answer") or "",
                    "citations": cached.get("citations") or [],
                    "engine": cached.get("engine") or "",
                    "thread_id": str(cached.get("thread_id") or thread_id),
                    "parent_id": int(cached.get("parent_id") or parent_id),
                    "followups": suggest_followups(conn, cached)}
        running = store.qa_find_running(conn, question, session_id, parent_id)
        if running:
            return {"ok": True, "reused": True, "id": int(running["id"]),
                    "state": "running", "thread_id": thread_id,
                    "parent_id": parent_id}
        row = store.qa_create(conn, question, scope, thread_id, parent_id)
    finally:
        conn.close()
    job = scheduler.submit(
        "qa", session_id or "qa", {"qid": int(row["id"]), "prompt": PROMPT_VERSION},
        priority=scheduler.PRIORITY_USER,
        fingerprint_payload={"q": question, "scope": scope, "prompt": PROMPT_VERSION},
        cache_ttl=0)
    conn = store.connect()
    try:
        store.qa_update(conn, int(row["id"]), job_id=job.id)
    finally:
        conn.close()
    return {"ok": True, "id": int(row["id"]), "state": "running", "job_id": job.id,
            "thread_id": thread_id, "parent_id": parent_id}


def run_job(qid: int) -> dict:
    conn = store.connect()
    try:
        return answer(conn, qid)
    finally:
        conn.close()


def history(limit: int = 10) -> list[dict]:
    conn = store.connect()
    try:
        rows = store.qa_recent(conn, limit)
    finally:
        conn.close()
    return [{"id": r["id"], "question": r["question"],
             "state": r["state"], "answer": str(r.get("answer") or "")[:160],
             "citations": len(r.get("citations") or []),
             "created_at": r.get("created_at")} for r in rows]


def feedback(qid: int, value: str) -> dict:
    value = value if value in ("up", "down") else ""
    conn = store.connect()
    try:
        row = store.qa_update(conn, qid, feedback=value)
    finally:
        conn.close()
    return {"ok": bool(row), "id": qid, "feedback": value}


def suggest(session_id: str = "") -> list[str]:
    conn = store.connect()
    try:
        return notes.suggest_questions(conn, session_id)
    finally:
        conn.close()


def prune(keep: int = 200) -> int:
    conn = store.connect()
    try:
        return store.qa_prune(conn, keep)
    finally:
        conn.close()
