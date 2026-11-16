# -*- coding: utf-8 -*-
"""问笔记：只用本机笔记做检索，答案必须带可点引用。

P1 不引入向量库（docs/36 §8.2）：``note_units`` 的 FTS5 trigram + 类型权重
+ 新鲜度就够回答「上周阿哲讲了哪些选品方法」这类问题。模型只看到检索到的
片段（每条 ≤200 字、总预算 ≤3000 tokens），不发送整场逐字稿和音频。

证据门：模型返回的 citations 必须是给定单元的子集；引用非法就剔除，全空
则降级为「没找到」。没有模型/模型失败时返回抽取式原话列表，不报错、不空屏。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import glossary, llm, logs, notes, scheduler, settings, store

PROMPT_VERSION = "qa-v1"
TOP_K = 10
TOKEN_BUDGET = 3000
MAX_UNIT_CHARS = 200
CACHE_HOURS = 24


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


def _scope_json(question: str, session_id: str) -> dict:
    scope = notes.parse_time_scope(question)
    if session_id:
        scope["session_id"] = session_id
    return scope


def _recency(started_at: str) -> float:
    try:
        t = datetime.fromisoformat(str(started_at))
        days = max(0.0, (datetime.now() - t.replace(tzinfo=None)).total_seconds()
                   / 86400.0)
        return 0.15 if days <= 30 else (0.07 if days <= 90 else 0.0)
    except (TypeError, ValueError, OSError):
        return 0.0


def retrieve(conn: sqlite3.Connection, question: str, scope: dict,
             top_k: int | None = None) -> list[dict]:
    """召回 + 排序。返回带 ``n``（引用序号）的单元列表。"""
    top_k = top_k or _top_k()
    sid = str(scope.get("session_id") or "")
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

只输出 JSON：{"answer":"...","citations":[1,2],"confidence":"high|medium|low"}

笔记片段：
%s
"""


def _extractive(units: list[dict]) -> tuple[str, list[dict], str]:
    if not units:
        return "", [], "extractive"
    top = units[:5]
    lines = ["找到 %d 段相关内容：" % len(units)]
    for u in top:
        t = _fmt_t(u.get("t"))
        prefix = ("%s " % t) if t else ""
        lines.append("%s%s" % (prefix, str(u.get("text") or "")[:120]))
    return "\n".join(lines), [_citation(u) for u in top], "extractive"


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
                        citations=[], engine="extractive")
        return {"ok": True, "state": "no_result", "items": []}

    text, cites, engine = _extractive(units)
    try:
        configured = bool(settings.resolve().get("configured"))
    except Exception:
        configured = False
    if configured:
        try:
            raw = llm.chat([
                {"role": "system",
                 "content": "你只根据用户提供的笔记片段回答，输出严格 JSON。"},
                {"role": "user", "content": _QA_PROMPT % _context(units)},
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
            # 证据门：答案里引用了不存在的编号也剔掉；全空则降级。
            ans = re.sub(r"\[(\d+)\]",
                         lambda m: m.group(0) if int(m.group(1)) in
                         {int(u["n"]) for u in valid} else "", ans).strip()
            if ans and valid and ans != "没找到相关内容":
                text, cites, engine = ans, [_citation(u) for u in valid], "llm"
            elif ans == "没找到相关内容":
                store.qa_update(conn, qid, state="no_result", answer=ans,
                                citations=[], engine="llm")
                return {"ok": True, "state": "no_result", "items": []}
        except Exception as e:
            logs.log_event("qa.llm_failed", level="warn", error=str(e)[:200])
            text, cites, engine = _extractive(units)
    store.qa_update(conn, qid, state="ready", answer=text,
                    citations=cites, engine=engine)
    logs.log_event("qa.answer", engine=engine, citations=len(cites),
                   units=len(units))
    return {"ok": True, "state": "ready", "answer": text,
            "citations": cites, "engine": engine}


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


def ask(question: str, session_id: str = "", limit: int = 0) -> dict:
    """提交问题。有 24h 缓存就直接返回，否则入队并立即返回 running。"""
    question = str(question or "").strip()[:500]
    if not question:
        raise ValueError("问题不能为空")
    scope = _scope_json(question, session_id)
    conn = store.connect()
    try:
        cached = _cached(conn, question, scope)
        if cached:
            return {"ok": True, "cached": True, "id": int(cached["id"]),
                    "state": cached["state"], "answer": cached.get("answer") or "",
                    "citations": cached.get("citations") or [],
                    "engine": cached.get("engine") or ""}
        running = store.qa_find_running(conn, question, session_id)
        if running:
            return {"ok": True, "reused": True, "id": int(running["id"]),
                    "state": "running"}
        row = store.qa_create(conn, question, scope)
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
    return {"ok": True, "id": int(row["id"]), "state": "running", "job_id": job.id}


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
