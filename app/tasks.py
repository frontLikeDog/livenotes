# -*- coding: utf-8 -*-
"""业务 LLM 任务：把「总结 / 润色 / AI 分类 / 下播收尾」登记到全局调度器。

调度器本身不认识业务，只认 kind + payload + handler；这个模块是唯一的注册处。
好处：
  - 任务台账里存的是可序列化的 payload，重启后仍能按 kind 找回处理器重试；
  - 指纹只包含"会改变输出"的输入（筛选条件、内容摘要、提示词版本、模型），
    不包含 auto/force 这类执行开关，所以「下播自动总结」和「用户紧接着点生成总结」
    会命中同一次调用，而不是把同一份稿子传两遍。
"""
from __future__ import annotations

import traceback

from . import (analysis, classify, insights, logs, notes, notify, polish, qa,
               reader, recluster, scheduler, store, titles)

_FILTER_KEYS = ("t_from", "t_to", "categories", "speakers", "q",
                "exclude_low_value", "exclude_overlap", "min_conf")


def normalize_filters(filters: dict | None) -> dict:
    """把 HTTP 传来的筛选条件收敛成稳定形状，指纹才不会因为键顺序/None 抖动。"""
    f = dict(filters or {})
    out = {k: f.get(k) for k in _FILTER_KEYS}
    out["categories"] = sorted(str(x) for x in (out.get("categories") or []))
    out["speakers"] = sorted(str(x) for x in (out.get("speakers") or []))
    out["q"] = str(out.get("q") or "").strip()[:120]
    for key in ("exclude_low_value", "exclude_overlap"):
        v = out.get(key)
        out[key] = True if v is None else bool(v)
    out["min_conf"] = out.get("min_conf")
    return out


def _digest(sid: str) -> str:
    conn = store.connect()
    try:
        return analysis.session_digest(conn, sid)
    finally:
        conn.close()


def summary_payload(filters: dict, mode: str, engine: str, digest: str,
                    auto: bool = False, recluster: bool = False) -> dict:
    return {
        "filters": normalize_filters(filters), "mode": mode, "engine": engine,
        "digest": digest, "prompt": analysis.SUMMARY_PROMPT_VERSION,
        "auto": bool(auto), "recluster": bool(recluster),
    }


def fingerprint_payload(payload: dict) -> dict:
    """只保留真正会改变模型输出的输入。

    - auto / recluster：只影响失败后的本地命名、以及收尾前要不要重聚类，
      不改变模型输入；
    - exclude_overlap / min_conf：build_scope 里被强制为 True / 0.45，
      用户勾不勾、传什么值都不会改变送给模型的内容，不能拿来拆分指纹。
    """
    fp = {k: v for k, v in payload.items() if k not in ("auto", "recluster")}
    filters = dict(fp.get("filters") or {})
    filters.pop("exclude_overlap", None)
    filters.pop("min_conf", None)
    fp["filters"] = filters
    return fp


# ---------------------------------------------------------------- 总结

def _whole_scope(payload: dict) -> bool:
    """这次总结是不是整场口径。筛选稿不发布：文档代表「整场」。"""
    f = payload.get("filters") or {}
    return not (f.get("categories") or f.get("speakers")
                or str(f.get("q") or "").strip()
                or f.get("t_from") is not None or f.get("t_to") is not None)


def _publish_quiet(conn, sid: str) -> None:
    """发布是投影：失败只记日志，绝不影响总结/收尾。"""
    try:
        from . import feishu_publish
        feishu_publish.publish(sid, conn=conn)
    except Exception:
        traceback.print_exc()
        try:
            logs.log_event("feishu.publish_error", level="warn", session_id=sid,
                           error=str(traceback.format_exc())[:200])
        except Exception:
            pass


def _run_summary(sid: str, payload: dict) -> dict:
    conn = store.connect()
    try:
        if payload.get("recluster"):
            # 收尾任务先按整场声纹重分人，再总结；用户手动点总结不重聚类。
            try:
                recluster.recluster_session(conn, sid)
            except Exception as e:
                logs.log_event("live.recluster_failed", level="warn",
                               session_id=sid, error=str(e))
        f = dict(payload.get("filters") or {})
        f["session_id"] = sid
        try:
            store.upsert_note(conn, sid, state="running", progress=10,
                              error="", generated_at=None)
        except Exception:
            traceback.print_exc()
        out = analysis.make_summary(conn, sid, f, payload.get("mode") or "content",
                                    payload.get("engine") or "auto")
        try:
            # 总结落库后立刻重建检索单元：搜索/问笔记不用等下一次启动。
            notes.index_session(conn, sid)
        except Exception:
            logs.log_event("notes.index_failed", level="warn", session_id=sid,
                           error=str(traceback.format_exc())[:200])
        # 整场总结成功 → 更新飞书文档与场次表（docs/47 §5.1）。
        # 放在 index 之后、通知之前：文档链接先就位，随后那条通知/卡片
        # 指向的是能打开的新内容。自动收尾走下面的 publish_session_async，
        # 这里只同步发布手动总结，避免同一场排队两次。默认关闭时只读配置就返回。
        if _whole_scope(payload) and not payload.get("auto"):
            _publish_quiet(conn, sid)
        if payload.get("auto"):
            logs.log_event("live.finalize_done", session_id=sid,
                           engine=str(out.get("engine") or ""),
                           llm_error=bool(out.get("llm_error")))
            # 主动提醒：观点反转 / 老问题有新答案。默认关，每场最多一条
            # （记录中「常客上麦」若已发过，这里会自己跳过）。
            try:
                insights.after_finalize(conn, sid)
            except Exception:
                traceback.print_exc()
            # 用户已经走了，完成时主动告诉一声。手动点「生成总结」不发，
            # 因为用户就在页面上，再弹通知是打扰；失败走下面的 except 分支。
            try:
                notify.note_ready(sid, out)
            except Exception:
                traceback.print_exc()
            # 发布到飞书。默认关；开关关着或没配置时 publish_session_async
            # 自己静默跳过，网络失败也只记日志，不影响本地总结结果。
            try:
                from . import feishu_publish
                feishu_publish.publish_session_async(sid)
            except Exception:
                traceback.print_exc()
        return out
    except Exception as e:
        # 本地总结已移除：模型失败就是失败，不再落一份抽取式稿冒充总结。
        # 仍然把场次名用结构化信息兜住（auto_local，界面会标「本地命名」），
        # 但不生成任何「要点」——录音和逐字稿都在，用户可以看到原因后重试。
        try:
            store.upsert_note(conn, sid, state="failed",
                              error=str(e).splitlines()[0][:200]
                              if str(e).strip() else "生成失败")
        except Exception:
            traceback.print_exc()
        if payload.get("auto"):
            try:
                r = titles.local_apply(conn, sid)
                logs.log_event("live.local_title", session_id=sid,
                               title=r.get("title"), reason=r.get("reason"))
            except Exception:
                traceback.print_exc()
            # 第一次总结就失败时，发布一篇「待总结」骨架，手机端至少能看到
            # 这场录了什么、为什么失败；已经有旧总结的场次保留旧文档不动。
            if _whole_scope(payload):
                try:
                    has_summary = bool(store.recent_summaries(conn, sid, "content", 1))
                except Exception:
                    has_summary = True
                if not has_summary:
                    _publish_quiet(conn, sid)
            try:
                notify.note_failed(sid, str(e))
            except Exception:
                traceback.print_exc()
        raise
    finally:
        conn.close()


def submit_summary(sid: str, filters: dict, mode: str = "content",
                   engine: str = "auto", *, auto: bool = False,
                   recluster: bool = False,
                   force: bool = False) -> scheduler.Job:
    payload = summary_payload(filters, mode, engine, _digest(sid), auto, recluster)
    return scheduler.submit(
        "summary", sid, payload,
        priority=scheduler.PRIORITY_FINALIZE if auto else scheduler.PRIORITY_USER,
        fingerprint_payload=fingerprint_payload(payload), force=force)


def submit_finalize(sid: str, *, recluster: bool = True) -> scheduler.Job:
    """下播后的自动收尾。与用户默认「按内容」总结使用同一套筛选条件。

    离线补转写进程已经做过全场重聚类，触发收尾时传 ``recluster=False``，
    避免同一场跑两遍重聚类（它会改写 speaker_label，白花 CPU）。
    """
    return submit_summary(sid, dict(analysis.WHOLE_FILTERS), "content", "auto",
                          auto=True, recluster=recluster)


# ---------------------------------------------------------------- 润色

def _run_polish(sid: str, payload: dict) -> dict:
    conn = store.connect()
    try:
        return polish.polish(conn, sid, force=bool(payload.get("force")))
    finally:
        conn.close()


def submit_polish(sid: str, force: bool = False) -> scheduler.Job:
    payload = {
        "digest": _digest(sid), "prompt": polish.PROMPT_VERSION,
        "gap": reader.GAP_SECONDS, "max_chars": reader.MAX_CHARS,
        "force": bool(force),
    }
    fp_payload = {k: v for k, v in payload.items() if k != "force"}
    return scheduler.submit(
        "polish", sid, payload, priority=scheduler.PRIORITY_ANALYSIS,
        fingerprint_payload=fp_payload, force=force)


# ---------------------------------------------------------------- AI 分类

def _run_classify(sid: str, payload: dict) -> dict:
    conn = store.connect()
    try:
        return classify.ai_classify(
            conn, sid,
            offset=int(payload.get("offset") or 0),
            limit=int(payload.get("limit") or classify.BATCH),
            topics=payload.get("topics"),
            force=bool(payload.get("force")))
    finally:
        conn.close()


def classify_fingerprint_payload(payload: dict) -> dict:
    """分类的指纹不包含内容摘要。

    分类本身会改 utterances.category，而摘要包含 category——把摘要放进指纹，
    任务跑起来后摘要就变了，用户连点第二下反而对不上正在跑的任务。
    分片位置 + 话题 + 提示词版本足以定义「同一片」；跑完后 cache_ttl=0，
    再点仍然会重新分类（例如先还原了规则）。
    """
    return {k: v for k, v in payload.items() if k != "digest"}


def submit_classify(sid: str, offset: int = 0, limit: int | None = None,
                    topics: list | None = None, force: bool = False) -> scheduler.Job:
    topics_n = sorted(str(t) for t in topics) if isinstance(topics, list) else None
    payload = {
        "offset": max(0, int(offset or 0)),
        "limit": max(1, int(limit or classify.BATCH)),
        "topics": topics_n, "digest": _digest(sid),
        "prompt": classify.PROMPT_VERSION, "force": bool(force),
    }
    # 分类是分片推进的：同一片正在跑时复用，跑完后再点应该允许重新分类（比如先还原了规则）。
    return scheduler.submit(
        "classify", sid, payload, priority=scheduler.PRIORITY_ANALYSIS,
        fingerprint_payload=classify_fingerprint_payload(payload),
        cache_ttl=0)


# ---------------------------------------------------------------- 问笔记

def _run_qa(sid: str, payload: dict) -> dict:
    """问答任务。结果写 qa_answers，HTTP 轮询读行，不靠 Job public 结果。"""
    qid = int(payload.get("qid") or 0)
    if not qid:
        raise ValueError("缺少问题编号")
    return qa.run_job(qid)


scheduler.register("summary", _run_summary)
scheduler.register("polish", _run_polish)
scheduler.register("classify", _run_classify)
scheduler.register("qa", _run_qa)
