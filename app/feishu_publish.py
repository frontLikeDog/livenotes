# -*- coding: utf-8 -*-
"""飞书发布器：一场 = 一篇云文档 + 一行场次表 + 一张收尾卡。

为什么放在引擎里而不是机器人里：总结成功后引擎就知道「可以发布了」，
不需要等机器人轮询；而飞书只是**发布产物**，本地 SQLite 仍是唯一真相源。

三条纪律（docs/47）：

1. **幂等**：``feishu_publish`` 唯一键 ``(session_id, kind)``；版本 hash 没变
   就不重写正文，避免清空块时丢掉评论锚点。重试也只补失败的那一部分。
2. **失败可降级**：文档失败 → 表格行仍可查；表格失败 → 文档仍可读；
   飞书没配置 → 静默跳过，不影响采集、转写和总结。
3. **不加载模型、不做双向同步**：这里只组装 Markdown + 调 HTTP。文档里的
   人工编辑不回流；唯一反向通道是补名（``/api/speakers/name``）。

配置（环境变量优先，其次 data/feishu.json）：

    FEISHU_PUBLISH_ENABLED           总开关；有 app_id/secret 时默认开，显式 0 关
    FEISHU_PUBLISH_FOLDER_TOKEN     场次文档文件夹；不填复用 doc_folder_token
    FEISHU_BASE_FOLDER_TOKEN        场次表文件夹；不填复用文档文件夹
    FEISHU_BASE_APP_TOKEN           可选：预建 Base；不填首次发布自动建
    FEISHU_BASE_TABLE_ID            可选：预建场次表；不填自动建
    FEISHU_BASE_CLAIMS_TABLE_ID     可选：预建观点表；不填自动建
    FEISHU_PUBLISH_BASE=0           只发文档、不写场次表与观点表
    FEISHU_BASE_ATTACHMENTS=0       只写链接列，不传「录音」「逐字稿」附件
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import traceback
from typing import Any

from . import claims, export, feishu, feishu_drive, feishu_http, identity, logs, store, timefmt

KIND_SESSION = "session"
KIND_CLAIMS = "claims"
STATE_OK = "ok"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"

DOC_READY = "ready"
DOC_PENDING_TRANSCRIPT = "pending_transcript"
DOC_PENDING_SUMMARY = "pending_summary"
DOC_FAILED_SUMMARY = "failed_summary"

BASE_NAME = "LiveNotes 场次"
TABLE_NAME = "场次"
TABLE_CLAIMS = "观点"
CLAIMS_DEFAULT_VIEW = "最近观点"

# 场次表字段（docs/47 §3.2）。MVP 不用「关联」字段：文本 session_id + URL，
# upsert 简单，也不会因为跨表 token 变化整表失效。选项就是写入时用的那几个值。
FIELD_DEFS: list[dict[str, Any]] = [
    {"field_name": "标题", "type": 1},
    {"field_name": "日期", "type": 5},
    {"field_name": "房间", "type": 1},
    {"field_name": "形态", "type": 3,
     "options": ["单人", "圆桌", "问答"]},
    {"field_name": "主持", "type": 1},
    {"field_name": "上麦人数", "type": 2},
    {"field_name": "时长分钟", "type": 2},
    {"field_name": "一句话", "type": 1},
    {"field_name": "观点数", "type": 2},
    {"field_name": "高光数", "type": 2},
    {"field_name": "状态", "type": 3,
     "options": ["待转写", "待总结", "已发布", "发布失败"]},
    {"field_name": "文档链接", "type": 15},
    {"field_name": "录音链接", "type": 15},
    {"field_name": "逐字稿链接", "type": 15},
    # 附件字段（docs/50 §3.2）：Base 附件要 media token；飞书租户/版本不支持
    # type 17 时这两个字段是「可选」的，建不出来就退回上面的 URL 字段。
    {"field_name": "录音", "type": 17, "optional": True},
    {"field_name": "逐字稿", "type": 17, "optional": True},
    {"field_name": "session_id", "type": 1},
    # 主题词是自由文本，做成多选会因为「选项不存在」写不进去；先用文本存，
    # Base 里仍能用「包含」筛选，P1 再统一管理选项后迁移。
    {"field_name": "标签", "type": 1},
    # 云端链路：区分「完整采集」与「仅采集，等离线补字」的场次。
    {"field_name": "采集", "type": 1},
]

# 观点表字段（docs/47 §3.2、§3.6.3）。主字段必须是第一个字段（「观点」）；
# 主题是本场自由词表，用文本不用多选避免选项缺失整行失败；不用关联字段，
# 用文本 session_id / person_id + URL。状态只建三个：不做「被反驳」，避免词面误报。
CLAIMS_FIELD_DEFS: list[dict[str, Any]] = [
    {"field_name": "观点", "type": 1},
    {"field_name": "谁", "type": 1},
    {"field_name": "主题", "type": 1},
    {"field_name": "类别", "type": 3, "options": ["主张", "问题"]},
    {"field_name": "状态", "type": 3, "options": ["新提出", "重申", "修正"]},
    {"field_name": "场次", "type": 1},
    {"field_name": "日期", "type": 5},
    {"field_name": "时间码", "type": 1},
    {"field_name": "原话", "type": 1},
    {"field_name": "场次文档链接", "type": 15},
    {"field_name": "claim_id", "type": 1},
    {"field_name": "session_id", "type": 1},
    {"field_name": "person_id", "type": 1},
]

_BASE_READY: set[tuple[str, str]] = set()
_BASE_GRANTED: set[tuple[str, str]] = set()
# 租户不支持附件字段（type 17）时记在这里，后续行不再带附件值。
_NO_ATTACH: set[tuple[str, str]] = set()


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# 后台发布去重；_TABLES 是旧实现留下的缓存名，测试会清它，保留为空占位。
_INFLIGHT: set[str] = set()
_TABLES: dict[str, str] = {}
_BOT = None


def _session_lock(sid: str) -> threading.Lock:
    """同一场只允许一个发布在跑：自动发布和 /发布 同时到达也只建一篇文档。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[sid] = lock
        return lock


# ---------------------------------------------------------------- 配置 / 状态

def config() -> dict:
    """发布配置的统一读法：环境变量优先，其次 data/feishu.json。

    同时保留两套键名：``enabled`` / ``folder`` / ``owner`` 是云端链路与
    HTTP 状态接口用的短名，``publish_*`` / ``base_*`` 是引擎内部沿用的原名。
    """
    raw = feishu.load_config()
    folder = str(raw.get("publish_folder_token")
                 or raw.get("doc_folder_token") or "")
    owner = str(raw.get("publish_owner")
                or raw.get("owner_open_id") or raw.get("open_id") or "")
    on = bool(raw.get("publish_enabled"))
    return {
        "enabled": on,
        "publish_enabled": on,
        "app_id": str(raw.get("app_id") or ""),
        "app_secret": str(raw.get("app_secret") or ""),
        "folder": folder,
        "publish_folder_token": folder,
        "doc_folder_token": str(raw.get("doc_folder_token") or ""),
        "owner": owner,
        "owner_open_id": owner,
        "open_id": str(raw.get("open_id") or ""),
        "doc_url_base": str(raw.get("doc_url_base") or ""),
        "base_enabled": bool(raw.get("base_enabled", True)),
        "base_app_token": str(raw.get("base_app_token") or ""),
        "base_table_id": str(raw.get("base_table_id") or ""),
        "base_claims_table_id": str(raw.get("base_claims_table_id") or ""),
        "base_folder_token": str(raw.get("base_folder_token") or ""),
        "base_url_base": str(raw.get("base_url_base") or ""),
        "archive_transcript": bool(raw.get("archive_transcript", True)),
        "archive_chat_id": str(raw.get("archive_chat_id") or ""),
        "archive_chat_perm": str(raw.get("archive_chat_perm") or "view"),
        "base_attachments": bool(raw.get("base_attachments", True)),
    }


def _cfg(bot=None) -> dict:
    cfg = getattr(bot, "cfg", None)
    if isinstance(cfg, dict) and cfg:
        return cfg
    try:
        return config()
    except Exception:
        return {}


def _default_bot(cfg: dict):
    """懒建一个 Bot。只在真发布时创建，仅采集节点不装 lark 也能启动。"""
    global _BOT
    with _LOCKS_GUARD:
        if _BOT is None:
            bot = feishu.Bot()
            folder = str(cfg.get("folder") or cfg.get("publish_folder_token") or "")
            if folder:
                bot.cfg = dict(bot.cfg or {})
                bot.cfg["doc_folder_token"] = folder
            _BOT = bot
        return _BOT


def reset_bot() -> None:
    """测试/换凭证用：丢掉缓存的 Bot。"""
    global _BOT
    with _LOCKS_GUARD:
        _BOT = None


def enabled(bot=None) -> bool:
    """总开关 + 凭证齐不齐。没有飞书凭证时发布器整体静默跳过。"""
    cfg = _cfg(bot)
    on = cfg.get("publish_enabled", cfg.get("enabled"))
    return bool(on) and bool(cfg.get("app_id") and cfg.get("app_secret"))


def _archive_on(cfg: dict) -> bool:
    """发布时自动归档录音；配置缺失按开处理（这一轮的默认行为）。"""
    v = cfg.get("archive_on_publish")
    return True if v is None else bool(v)


def _owner(bot) -> str:
    cfg = _cfg(bot)
    return str(getattr(bot, "owner_open_id", "")
               or cfg.get("publish_owner")
               or cfg.get("owner")
               or cfg.get("owner_open_id")
               or cfg.get("open_id") or "")


def has_published(conn: sqlite3.Connection, sid: str) -> bool:
    row = store.get_publish(conn, sid)
    return bool(row and (row.get("doc_url") or row.get("doc_token")))


def status(conn: sqlite3.Connection, sid: str) -> dict:
    """/文档 与网页读发布状态：一行记录 + 这一刻的文档状态。"""
    row = store.get_publish(conn, sid)
    doc = {}
    try:
        doc = export.build_publish(conn, sid)
    except Exception:
        doc = {}
    if not row:
        return {"ok": True, "session_id": sid, "published": False,
                "doc_state": doc.get("state") or "",
                "title": doc.get("title") or "",
                "publish": None}
    meta = row.get("meta") or {}
    return {
        "ok": True, "session_id": sid, "published": True,
        "state": row.get("state") or "", "doc_state": meta.get("doc_state")
        or doc.get("state") or "",
        "title": doc.get("title") or "", "overview": doc.get("overview") or "",
        "doc_url": row.get("doc_url") or "", "record_id": row.get("record_id") or "",
        "base_url": meta.get("base_url") or "", "record_url": meta.get("record_url") or "",
        "error": row.get("error") or "", "version": row.get("version") or "",
        "published_at": row.get("published_at") or "",
        "latency_ms": int(row.get("latency_ms") or 0),
        "attempts": int(row.get("attempts") or 0),
        "counts": doc.get("counts") or {},
        "duration_s": doc.get("duration_s") or 0,
        "publish": row,
    }


def _version(title: str, markdown: str) -> str:
    raw = (str(title or "") + "\n" + str(markdown or "")).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ---------------------------------------------------------------- 发布主流程

def _bot_or_error(bot=None) -> tuple[Any, str]:
    if bot is not None:
        return bot, ""
    try:
        return _default_bot(config()), ""
    except Exception as e:
        return None, "飞书未配置：%s" % e


def _publish(sid: str, *, bot=None, conn: sqlite3.Connection | None = None,
             force: bool = False, notify: bool = True) -> dict:
    """发布一场。返回 {ok, state, doc_url, record_id, unchanged, error...}。

    - ``force=True``：显式重发布（用户发 /发布），即使版本没变也重写正文。
    - ``notify=True``：发布完成后给 owner 发一张收尾卡；失败不影响发布结果。
    """
    started = time.monotonic()
    own = conn is None
    conn = conn or store.connect()
    lock = _session_lock(str(sid))
    lock.acquire()
    try:
        s = store.get_session(conn, sid)
        if not s:
            return {"ok": False, "session_id": sid, "error": "场次不存在"}
        if s.get("deleted_at"):
            return {"ok": False, "session_id": sid, "error": "这场在回收站里"}
        bot_obj, why = _bot_or_error(bot)
        if bot_obj is None:
            logs.log_event("feishu.publish_skipped", session_id=sid, reason=why)
            return {"ok": False, "skipped": True, "session_id": sid, "error": why}
        if not enabled(bot_obj):
            logs.log_event("feishu.publish_skipped", session_id=sid,
                           reason="disabled")
            return {"ok": False, "skipped": True, "session_id": sid,
                    "error": "发布器没开（FEISHU_PUBLISH_ENABLED）"}

        cfg = _cfg(bot_obj)
        # 先归档再生成正文：build_publish 从库里读 drive_url 拼「录音」，
        # 版本 hash 也拿正文算，顺序反了文档就会少这一节。
        archive: dict = {"ok": True, "skipped": True, "error": ""}
        if _archive_on(cfg):
            try:
                archive = feishu_drive.ensure_archived(conn, s, bot_obj)
            except Exception as e:
                traceback.print_exc()
                archive = {"ok": False, "skipped": False, "error": str(e)}
            logs.log_event(
                "feishu.archive",
                level="info" if archive.get("ok") else "warn", session_id=sid,
                ok=bool(archive.get("ok")), skipped=bool(archive.get("skipped")),
                error=str(archive.get("error") or ""))

        doc = export.build_publish(conn, sid)
        if not doc.get("ok"):
            return {"ok": False, "session_id": sid,
                    "skipped": bool(doc.get("skipped")),
                    "reason": str(doc.get("reason") or ""),
                    "error": doc.get("error") or "生成发布内容失败"}

        row = store.get_publish(conn, sid) or {}
        owner = _owner(bot_obj)
        version = _version(doc["title"], doc["markdown"])
        base_on = bool(cfg.get("base_enabled", True))
        same_version = row.get("version") == version
        # 上次正文没写成功（state=failed）时必须重写；其余同版本情况正文不动，
        # 只补表格/授权，避免反复清空块把评论锚点弄丢。
        can_reuse = (bool(row.get("doc_token")) and same_version and not force
                     and row.get("state") in (STATE_OK, STATE_PARTIAL))

        # 版本没变、上次也成功：文档不动（保住评论锚点）；若观点签名变了只补观点表。
        if (same_version and not force and row.get("state") == STATE_OK
                and row.get("doc_url")
                and (row.get("record_id") or not base_on)):
            claims_res: dict = {"ok": True, "skipped": True}
            if base_on:
                try:
                    claims_res = _upsert_claims(
                        conn, bot_obj, sid, str(row.get("doc_url") or ""),
                        force=False)
                except Exception as e:
                    traceback.print_exc()
                    claims_res = {"ok": False, "error": str(e)}
                if not claims_res.get("ok"):
                    err_msg = "观点表没写成功：%s" % claims_res.get("error", "")
                    latency_ms = int((time.monotonic() - started) * 1000)
                    store.upsert_publish(
                        conn, sid, KIND_SESSION, state=STATE_PARTIAL,
                        error=err_msg[:1000], latency_ms=latency_ms,
                        attempts=int(row.get("attempts") or 0) + 1)
                    logs.log_event(
                        "feishu.publish_ok", level="info", session_id=sid,
                        state=STATE_PARTIAL, kind=KIND_SESSION,
                        latency_ms=latency_ms, doc_state=doc["state"],
                        error=err_msg[:200])
                    return {"ok": True, "unchanged": False,
                            "claims_updated": False, "session_id": sid,
                            "state": STATE_PARTIAL, "doc_state": doc["state"],
                            "title": doc["title"],
                            "doc_url": row.get("doc_url") or "",
                            "record_id": row.get("record_id") or "",
                            "record_url": (row.get("meta") or {}).get("record_url") or "",
                            "base_url": (row.get("meta") or {}).get("base_url") or "",
                            "version": version, "latency_ms": latency_ms,
                            "error": err_msg, "warnings": [err_msg],
                            "overview": doc.get("overview") or "",
                            "counts": doc.get("counts") or {}}
                if not claims_res.get("skipped"):
                    latency_ms = int((time.monotonic() - started) * 1000)
                    if claims_res.get("grant_error"):
                        err_msg = "观点表授权失败：%s" % claims_res["grant_error"]
                        new_meta = dict(row.get("meta") or {})
                        new_meta["base_ok"] = False
                        store.upsert_publish(
                            conn, sid, KIND_SESSION, state=STATE_PARTIAL,
                            error=err_msg[:1000], meta=new_meta,
                            latency_ms=latency_ms,
                            attempts=int(row.get("attempts") or 0) + 1)
                        logs.log_event(
                            "feishu.publish_ok", level="info", session_id=sid,
                            state=STATE_PARTIAL, kind=KIND_SESSION,
                            latency_ms=latency_ms, doc_state=doc["state"],
                            error=err_msg[:200])
                        return {"ok": True, "unchanged": False,
                                "claims_updated": True, "session_id": sid,
                                "state": STATE_PARTIAL, "doc_state": doc["state"],
                                "title": doc["title"],
                                "doc_url": row.get("doc_url") or "",
                                "record_id": row.get("record_id") or "",
                                "record_url": new_meta.get("record_url") or "",
                                "base_url": new_meta.get("base_url") or "",
                                "version": version, "latency_ms": latency_ms,
                                "error": err_msg, "warnings": [err_msg],
                                "overview": doc.get("overview") or "",
                                "counts": doc.get("counts") or {}}
                    return {"ok": True, "unchanged": False,
                            "claims_updated": True, "session_id": sid,
                            "state": STATE_OK, "doc_state": doc["state"],
                            "title": doc["title"],
                            "doc_url": row.get("doc_url") or "",
                            "record_id": row.get("record_id") or "",
                            "record_url": (row.get("meta") or {}).get("record_url") or "",
                            "base_url": (row.get("meta") or {}).get("base_url") or "",
                            "version": version, "latency_ms": latency_ms,
                            "overview": doc.get("overview") or "",
                            "counts": doc.get("counts") or {}}
            logs.log_event("feishu.publish_skipped", session_id=sid,
                           reason="unchanged", version=version)
            return {"ok": True, "unchanged": True, "session_id": sid,
                    "state": STATE_OK, "doc_state": doc["state"],
                    "title": doc["title"], "doc_url": row.get("doc_url") or "",
                    "record_id": row.get("record_id") or "",
                    "record_url": (row.get("meta") or {}).get("record_url") or "",
                    "base_url": (row.get("meta") or {}).get("base_url") or "",
                    "version": version, "latency_ms": 0,
                    "overview": doc.get("overview") or "",
                    "counts": doc.get("counts") or {}}

        warnings: list[str] = []
        share_with = owner if (not row.get("doc_token")
                               or row.get("state") != STATE_OK or force) else ""
        if can_reuse:
            doc_res = {"ok": True, "document_id": row.get("doc_token") or "",
                       "url": row.get("doc_url") or "", "reused": True}
            # 上次失败在授权：复用正文时补一次授权，不重写整篇。
            if share_with and row.get("doc_token"):
                grant = bot_obj._grant_doc(str(row["doc_token"]), owner)
                if not grant.get("ok"):
                    warnings.append("文档授权失败：%s" % grant.get("error", ""))
        elif row.get("doc_token"):
            doc_res = bot_obj.overwrite_docx(
                str(row["doc_token"]), doc["title"], doc["markdown"],
                share_with=share_with)
        else:
            doc_res = bot_obj.create_docx(
                doc["title"], doc["markdown"], share_with=share_with,
                folder_token=cfg.get("publish_folder_token") or "")

        doc_ok = bool(doc_res.get("ok"))
        doc_token = str(doc_res.get("document_id") or row.get("doc_token") or "")
        doc_url = str(doc_res.get("url") or row.get("doc_url") or "")
        doc_error = "" if doc_ok else str(doc_res.get("error") or "文档发布失败")
        if doc_res.get("share_error"):
            warnings.append("文档授权失败：%s" % doc_res["share_error"])
        if not doc_ok:
            warnings.append(doc_error)

        old_meta = row.get("meta") or {}
        base_res: dict = {"ok": True, "skipped": True}
        if base_on:
            # 文档和场次表都已为当前版本写过（例如上次仅观点表失败），重试只补观点表。
            reuse_base = (can_reuse and bool(row.get("record_id"))
                          and bool(old_meta.get("base_ok")) and doc_ok)
            if reuse_base:
                base_res = {"ok": True, "skipped": True,
                            "record_id": row.get("record_id") or "",
                            "record_url": old_meta.get("record_url") or "",
                            "base_url": old_meta.get("base_url") or ""}
            else:
                try:
                    base_res = _upsert_session_row(conn, bot_obj, doc, doc_ok,
                                                   doc_url, row.get("record_id") or "")
                except Exception as e:
                    traceback.print_exc()
                    base_res = {"ok": False, "error": str(e)}
                if not base_res.get("ok"):
                    warnings.append("场次表没写成功：%s" % base_res.get("error", ""))
                elif base_res.get("grant_error"):
                    warnings.append("场次表授权失败：%s" % base_res["grant_error"])

        claims_res = {"ok": True, "skipped": True}
        if base_on:
            claims_doc_url = doc_url if doc_ok else ""
            try:
                claims_res = _upsert_claims(conn, bot_obj, sid, claims_doc_url,
                                            force=force)
            except Exception as e:
                traceback.print_exc()
                claims_res = {"ok": False, "error": str(e)}
            if not claims_res.get("ok"):
                warnings.append("观点表没写成功：%s" % claims_res.get("error", ""))
            elif claims_res.get("grant_error") and not base_res.get("grant_error"):
                warnings.append("观点表授权失败：%s" % claims_res["grant_error"])

        state = STATE_OK
        if not doc_ok:
            state = STATE_FAILED
        elif warnings:
            state = STATE_PARTIAL
        latency_ms = int((time.monotonic() - started) * 1000)
        meta = {
            "doc_state": doc["state"],
            "base_url": base_res.get("base_url") or old_meta.get("base_url") or "",
            "record_url": (base_res.get("record_url")
                           or old_meta.get("record_url") or ""),
            "base_skipped": bool(base_res.get("skipped")),
            "base_ok": bool(base_res.get("ok")) and not bool(base_res.get("grant_error")),
            # 附件是加分项：失败只进 meta，不让整场变成 partial。
            "base_attach_error": str(base_res.get("attach_error") or ""),
            "folder_url": (str(archive.get("folder_url") or "")
                           or str(old_meta.get("folder_url") or "")),
            # 归档失败不改发布状态：文档没有录音链接仍然是一篇完整文档。
            "archive_skipped": bool(archive.get("skipped")),
            "archive_parts": int(archive.get("parts") or 0),
            "archive_error": ("" if archive.get("ok")
                              else str(archive.get("error") or "")),
        }
        row = store.upsert_publish(
            conn, sid, KIND_SESSION, state=state, version=version,
            doc_token=doc_token, doc_url=doc_url,
            record_id=str(base_res.get("record_id") or row.get("record_id") or ""),
            error="；".join(w for w in warnings if w)[:1000],
            meta=meta, latency_ms=latency_ms,
            attempts=int(row.get("attempts") or 0) + 1,
            published_at=(store.now() if doc_ok else row.get("published_at")))
        logs.log_event(
            "feishu.publish_ok" if doc_ok else "feishu.publish_failed",
            level="info" if doc_ok else "warn", session_id=sid,
            state=state, kind=KIND_SESSION, latency_ms=latency_ms,
            doc_state=doc["state"], error=row.get("error") or "")

        result = {
            "ok": doc_ok, "session_id": sid, "state": state,
            "doc_state": doc["state"], "title": doc["title"],
            "doc_url": doc_url, "record_id": row.get("record_id") or "",
            "record_url": meta["record_url"], "base_url": meta["base_url"],
            "version": version, "latency_ms": latency_ms,
            "error": row.get("error") or "", "warnings": warnings,
            "overview": doc.get("overview") or "",
            "counts": doc.get("counts") or {},
            "base_skipped": bool(base_res.get("skipped")),
            "attach_error": str(base_res.get("attach_error") or ""),
        }
        if notify:
            # 失败也发卡：云端无人值守时，红卡是用户知道「该重试了」的唯一入口。
            try:
                card = wrapup_card(result, doc)
                bot_obj.send_card(card)
            except Exception:
                traceback.print_exc()
        return result
    finally:
        lock.release()
        if own:
            conn.close()


def publish(sid: str, *, bot=None, conn: sqlite3.Connection | None = None,
            force: bool = False, notify: bool = True) -> dict:
    """发布入口：任何异常都转为失败结果，调用方（HTTP/线程）不用兜底。"""
    try:
        return _publish(sid, bot=bot, conn=conn, force=force, notify=notify)
    except Exception as e:
        traceback.print_exc()
        msg = "%s: %s" % (type(e).__name__, e)
        try:
            c = conn or store.connect()
            try:
                store.upsert_publish(c, sid, KIND_SESSION, state=STATE_FAILED,
                                     error=msg[:1000], meta={})
            finally:
                if conn is None:
                    c.close()
        except Exception:
            traceback.print_exc()
        try:
            logs.log_event("feishu.publish_error", level="warn", session_id=sid,
                           error=msg[:200])
        except Exception:
            pass
        return {"ok": False, "session_id": sid, "state": STATE_FAILED,
                "error": "发布异常：" + msg}


def after_summary(conn: sqlite3.Connection, sid: str) -> dict:
    """总结成功后调用。发布只是投影，失败不影响本地结果。"""
    return publish(sid, conn=conn)


def publish_async(sid: str, *, force: bool = False) -> bool:
    """后台发布（仅采集下播建骨架用）。返回是否真的排了线程。"""
    if not enabled():
        logs.log_event("feishu.publish_skipped", session_id=sid,
                       reason="disabled")
        return False
    with _LOCKS_GUARD:
        if sid in _INFLIGHT:
            return False
        _INFLIGHT.add(sid)

    def run() -> None:
        conn = store.connect()
        try:
            publish(sid, conn=conn, force=force)
        except Exception:
            traceback.print_exc()
        finally:
            conn.close()
            with _LOCKS_GUARD:
                _INFLIGHT.discard(sid)

    threading.Thread(target=run, name="feishu-publish-" + str(sid),
                     daemon=True).start()
    return True


def publish_session_async(sid: str, *, only_if_published: bool = False) -> bool:
    """后台发布。总结成功、补名成功后调用；开关关着就静默跳过。

    不阻塞总结任务：云文档/表格是网络调用，失败只影响「飞书里有没有」，
    不影响本地录音、逐字稿和总结。
    """
    if not enabled():
        logs.log_event("feishu.publish_skipped", session_id=sid,
                       reason="disabled")
        return False

    def run() -> None:
        conn = store.connect()
        try:
            if only_if_published and not has_published(conn, sid):
                return
            publish(sid, conn=conn)
        except Exception:
            traceback.print_exc()
            logs.log_event("feishu.publish_failed", level="warn",
                           session_id=sid, error=str(traceback.format_exc())[:300])
        finally:
            conn.close()

    threading.Thread(target=run, name="feishu-publish-" + str(sid),
                     daemon=True).start()
    return True


# ---------------------------------------------------------------- 场次表（Base）

def _bitable(bot):
    return bot.client.bitable.v1


def _err(resp) -> str:
    return "code=%s %s" % (getattr(resp, "code", ""), getattr(resp, "msg", ""))


def _opt_property(spec: dict):
    opts = spec.get("options") or []
    if not opts:
        return None
    return feishu_http.obj(
        options=[feishu_http.obj(name=str(o)) for o in opts]
    )


def _create_table(client, app_token: str, table_name: str = TABLE_NAME,
                  field_defs: list[dict[str, Any]] | None = None,
                  default_view_name: str = "最近场次") -> dict:
    defs = FIELD_DEFS if field_defs is None else field_defs

    def _attempt(specs: list[dict[str, Any]]):
        headers = []
        for spec in specs:
            headers.append(feishu_http.obj(
                field_name=spec["field_name"],
                type=int(spec["type"]),
                property=_opt_property(spec),
            ))
        table = feishu_http.obj(
            name=table_name,
            default_view_name=default_view_name,
            fields=headers,
        )
        body = feishu_http.obj(table=table)
        return client.bitable.v1.app_table.create(
            feishu_http.obj(app_token=app_token, request_body=body))

    resp = _attempt(defs)
    skipped_optional = False
    # 附件字段是可选加分项：整个建表被 type 17 拒掉时，去掉可选字段再建一次。
    optional = [s for s in defs if s.get("optional")]
    if not resp.success() and optional:
        resp = _attempt([s for s in defs if not s.get("optional")])
        skipped_optional = True
    if not resp.success():
        return {"ok": False, "error": "建%s表失败：%s" % (table_name, _err(resp))}
    table_id = str(getattr(resp.data, "table_id", "") or "")
    if not table_id:
        return {"ok": False, "error": "建%s表没返回 table_id" % table_name}
    if skipped_optional:
        _NO_ATTACH.add((app_token, table_id))
    return {"ok": True, "table_id": table_id}


def _find_table(client, app_token: str, table_name: str = TABLE_NAME) -> str:
    token = ""
    for _ in range(10):
        req = feishu_http.obj(
            app_token=app_token, page_size=100, page_token=token or None)
        resp = client.bitable.v1.app_table.list(req)
        if not resp.success():
            return ""
        data = resp.data
        for item in (getattr(data, "items", None) or []):
            if str(getattr(item, "name", "")) == table_name:
                return str(getattr(item, "table_id", "") or "")
        token = str(getattr(data, "page_token", "") or "")
        if not getattr(data, "has_more", False) or not token:
            break
    return ""


def _ensure_fields(client, app_token: str, table_id: str,
                   field_defs: list[dict[str, Any]] | None = None) -> str:
    """老表补字段：用户在飞书里删了某列，发布不该静默丢这一列。"""
    key = (app_token, table_id)
    if key in _BASE_READY:
        return ""
    defs = FIELD_DEFS if field_defs is None else field_defs
    resp = client.bitable.v1.app_table_field.list(
        feishu_http.obj(app_token=app_token, table_id=table_id, page_size=100))
    if not resp.success():
        return "读多维表格字段失败：" + _err(resp)
    names = {str(getattr(f, "field_name", ""))
             for f in (getattr(resp.data, "items", None) or [])}
    no_attach = key in _NO_ATTACH
    for spec in defs:
        if spec["field_name"] in names:
            continue
        if spec.get("optional") and no_attach:
            continue
        field_body = feishu_http.obj(
            field_name=spec["field_name"],
            type=int(spec["type"]),
            property=_opt_property(spec),
        )
        created = client.bitable.v1.app_table_field.create(
            feishu_http.obj(app_token=app_token, table_id=table_id,
                            request_body=field_body))
        if not created.success():
            if spec.get("optional"):
                # 老表/老租户不支持附件字段：记下来，退回 URL 链接字段。
                _NO_ATTACH.add(key)
                no_attach = True
                logs.log_event("feishu.base_field_skipped", level="warn",
                               field=spec["field_name"], error=_err(created))
                continue
            return "补字段「%s」失败：%s" % (spec["field_name"], _err(created))
    _BASE_READY.add(key)
    return ""


def _ensure_base_grant(bot, app_token: str) -> str:
    """Base 级授权（覆盖表内所有表）。复用已有 Base 时也走一次，确保 owner 可见。"""
    owner = _owner(bot)
    if not app_token or not owner:
        return ""
    key = (app_token, owner)
    if key in _BASE_GRANTED:
        return ""
    grant = bot._grant_doc(app_token, owner, doc_type="bitable")
    if grant.get("ok"):
        _BASE_GRANTED.add(key)
        return ""
    return str(grant.get("error") or "")


def ensure_base(conn: sqlite3.Connection, bot) -> dict:
    """拿到（必要时创建）场次表。token 记进 index_meta，进程重启也不重建。"""
    cfg = _cfg(bot)
    app_token = str(cfg.get("base_app_token")
                    or store.get_kv(conn, "feishu_base_app_token") or "")
    table_id = str(cfg.get("base_table_id")
                   or store.get_kv(conn, "feishu_base_table_id") or "")
    base_url = str(cfg.get("base_url_base")
                   or store.get_kv(conn, "feishu_base_url") or "")
    client = bot.client
    created_base = False

    if not app_token:
        folder = str(cfg.get("base_folder_token") or "").strip()
        req_app = feishu_http.obj(name=BASE_NAME, folder_token=folder or None)
        resp = client.bitable.v1.app.create(
            feishu_http.obj(request_body=req_app))
        if not resp.success():
            return {"ok": False, "error": "建场次表失败：" + _err(resp)}
        app = getattr(resp.data, "app", None)
        app_token = str(getattr(app, "app_token", "") or "")
        base_url = base_url or str(getattr(app, "url", "") or "")
        if not app_token:
            return {"ok": False, "error": "建场次表没返回 app_token"}
        store.set_kv(conn, "feishu_base_app_token", app_token)
        if base_url:
            store.set_kv(conn, "feishu_base_url", base_url)
        created_base = True

    grant_error = _ensure_base_grant(bot, app_token)
    if created_base:
        logs.log_event("feishu.base_created", app_token=app_token,
                       shared=not grant_error)

    if not table_id:
        table_id = _find_table(client, app_token, TABLE_NAME)
        if not table_id:
            made = _create_table(client, app_token, TABLE_NAME, FIELD_DEFS,
                                 "最近场次")
            if not made.get("ok"):
                return made
            table_id = made["table_id"]
        store.set_kv(conn, "feishu_base_table_id", table_id)

    bad = _ensure_fields(client, app_token, table_id, FIELD_DEFS)
    if bad:
        return {"ok": False, "error": bad}
    return {"ok": True, "app_token": app_token, "table_id": table_id,
            "base_url": base_url, "grant_error": grant_error}


def ensure_claims_table(conn: sqlite3.Connection, bot) -> dict:
    """在已有 Base 里拿到（必要时创建）「观点」表。失败返回 ok=False，不外抛。"""
    base = ensure_base(conn, bot)
    if not base.get("ok"):
        return base
    app_token = base["app_token"]
    cfg = _cfg(bot)
    table_id = str(os.environ.get("FEISHU_BASE_CLAIMS_TABLE_ID")
                   or cfg.get("base_claims_table_id")
                   or store.get_kv(conn, "feishu_base_claims_table_id") or "")
    client = bot.client
    if not table_id:
        table_id = _find_table(client, app_token, TABLE_CLAIMS)
        if not table_id:
            made = _create_table(client, app_token, TABLE_CLAIMS,
                                 CLAIMS_FIELD_DEFS, CLAIMS_DEFAULT_VIEW)
            if not made.get("ok"):
                return made
            table_id = made["table_id"]
        store.set_kv(conn, "feishu_base_claims_table_id", table_id)

    bad = _ensure_fields(client, app_token, table_id, CLAIMS_FIELD_DEFS)
    if bad:
        return {"ok": False, "error": bad}
    return {"ok": True, "app_token": app_token, "table_id": table_id,
            "base_url": base.get("base_url") or "",
            "grant_error": base.get("grant_error") or ""}


def _find_record(client, app_token: str, table_id: str, sid: str) -> str:
    body = feishu_http.obj(
        filter=feishu_http.obj(
            conjunction="and",
            conditions=[feishu_http.obj(
                field_name="session_id", operator="is", value=[str(sid)]
            )],
        ),
        automatic_fields=False,
    )
    resp = client.bitable.v1.app_table_record.search(
        feishu_http.obj(app_token=app_token, table_id=table_id,
                        request_body=body))
    if not resp.success():
        return ""
    items = getattr(resp.data, "items", None) or []
    for r in items:
        rid = str(getattr(r, "record_id", "") or "")
        if rid:
            return rid
    return ""


def _record_fields(doc: dict, doc_ok: bool, doc_url: str,
                   attachments: dict | None = None,
                   allow_attachments: bool = True) -> dict:
    state = str(doc.get("state") or "")
    if state == DOC_PENDING_TRANSCRIPT:
        status = "待转写"
    elif state in (DOC_PENDING_SUMMARY, DOC_FAILED_SUMMARY):
        status = "待总结"
    else:
        status = "已发布" if doc_ok else "发布失败"
    fields: dict = {
        "标题": doc.get("base_title") or doc.get("title") or "",
        "房间": doc.get("room_name") or "",
        "形态": doc.get("shape") or "",
        "主持": doc.get("host") or "",
        "上麦人数": int((doc.get("counts") or {}).get("speakers") or 0),
        "时长分钟": int(round(float(doc.get("duration_s") or 0) / 60)),
        "一句话": str(doc.get("overview") or "")[:500],
        "观点数": int((doc.get("counts") or {}).get("points") or 0),
        "高光数": int((doc.get("counts") or {}).get("highlights") or 0),
        "状态": status,
        "session_id": str(doc.get("session_id") or ""),
    }
    if doc.get("capture_only"):
        fields["采集"] = "仅采集"
    if doc.get("started_ms"):
        fields["日期"] = int(doc["started_ms"])
    if doc_url:
        fields["文档链接"] = {"link": doc_url, "text": "打开场次文档"}
    if doc.get("audio_url"):
        n_parts = int(doc.get("audio_parts") or 0)
        fields["录音链接"] = {
            "link": doc["audio_url"],
            "text": ("打开录音（%d 段）" % n_parts) if n_parts > 1 else "打开录音"}
    att = attachments or {}
    if att.get("transcript_url"):
        fields["逐字稿链接"] = {"link": att["transcript_url"],
                                "text": "打开逐字稿"}
    if allow_attachments:
        # Base 附件字段的值是 [{file_token: media_token}]，不是单个字符串。
        if att.get("audio_token"):
            fields["录音"] = [{"file_token": str(att["audio_token"])}]
        if att.get("transcript_token"):
            fields["逐字稿"] = [{"file_token": str(att["transcript_token"])}]
    topics = [str(t).strip() for t in (doc.get("topics") or []) if str(t).strip()]
    if topics:
        fields["标签"] = "、".join(topics[:10])
    return fields


def _upsert_session_row(conn: sqlite3.Connection, bot, doc: dict, doc_ok: bool,
                        doc_url: str, known_record: str = "") -> dict:
    """按 session_id upsert 场次表一行；不会因为重复发布多出一行。

    附件（录音 / 逐字稿）是可选加分项：素材上传失败只记 ``attach_error``，
    行照写；附件字段写不进去时去掉这两个键重试一次，链接字段仍保留。
    """
    base = ensure_base(conn, bot)
    if not base.get("ok"):
        return base
    app_token = base["app_token"]
    table_id = base["table_id"]
    client = bot.client
    key = (app_token, table_id)
    allow_attachments = key not in _NO_ATTACH
    attachments: dict = {}
    attach_error = ""
    cfg = _cfg(bot)
    if allow_attachments and cfg.get("base_attachments", True):
        sid = str(doc.get("session_id") or "")
        session = store.get_session(conn, sid) or {}
        try:
            attachments = feishu_drive.prepare_attachments(
                conn, session, bot, app_token)
            if not attachments.get("ok"):
                attach_error = "；".join(attachments.get("errors") or [])
        except Exception as e:
            traceback.print_exc()
            attach_error = "附件准备失败：%s" % e
            attachments = {}
        if attach_error:
            logs.log_event("feishu.base_attach_failed", level="warn",
                           session_id=sid, error=attach_error[:200])
    else:
        # 不传附件不等于不写链接列：FEISHU_BASE_ATTACHMENTS=0 时仍要
        # 把逐字稿传进云空间、把「逐字稿链接」写进表。
        sid = str(doc.get("session_id") or "")
        session = store.get_session(conn, sid) or {}
        if sid and session and cfg.get("archive_transcript", True):
            try:
                tr = feishu_drive.ensure_transcript(conn, session, bot)
                if tr.get("ok"):
                    attachments = {"transcript_url": str(tr.get("url") or "")}
            except Exception:
                traceback.print_exc()
    fields = _record_fields(doc, doc_ok, doc_url, attachments=attachments,
                            allow_attachments=allow_attachments)
    record_id = str(known_record or "")
    if not record_id:
        record_id = _find_record(client, app_token, table_id,
                                 str(doc.get("session_id") or ""))
    def _write(values: dict):
        record_body = feishu_http.obj(fields=values)
        if record_id:
            return client.bitable.v1.app_table_record.update(
                feishu_http.obj(app_token=app_token, table_id=table_id,
                                record_id=record_id,
                                request_body=record_body)), False
        return client.bitable.v1.app_table_record.create(
            feishu_http.obj(app_token=app_token, table_id=table_id,
                            request_body=record_body)), True

    resp, created = _write(fields)
    if not resp.success() and allow_attachments and (
            "录音" in fields or "逐字稿" in fields):
        # 附件字段可能被租户策略拒绝：去掉附件重试，不让整行失败。
        first_err = _err(resp)
        _NO_ATTACH.add(key)
        allow_attachments = False
        retry = {k: v for k, v in fields.items() if k not in ("录音", "逐字稿")}
        resp, created = _write(retry)
        if resp.success():
            attach_error = attach_error or "附件字段不可用，已退回链接字段"
            logs.log_event("feishu.base_attach_skipped", level="warn",
                           session_id=doc.get("session_id"), error=first_err[:200])
    if not resp.success():
        return {"ok": False, "error": "写场次表失败：" + _err(resp),
                "attach_error": attach_error}
    record = getattr(resp.data, "record", None)
    rid = str(getattr(record, "record_id", "") or record_id)
    record_url = str(getattr(record, "record_url", "") or "")
    logs.log_event("feishu.base_upsert", session_id=doc.get("session_id"),
                   table="session", record_id=rid,
                   action="created" if created else "updated",
                   attachments=bool(fields.get("录音") or fields.get("逐字稿")))
    return {"ok": True, "record_id": rid, "record_url": record_url,
            "base_url": base.get("base_url") or "",
            "grant_error": base.get("grant_error") or "",
            "attach_error": attach_error}


# ---------------------------------------------------------------- 观点表（Base）

def _persons_map(conn: sqlite3.Connection, person_ids: set[str]) -> dict[str, str]:
    pids = sorted(p for p in person_ids if p)
    if not pids:
        return {}
    ph = ",".join("?" * len(pids))
    return {
        str(r["person_id"]): str(r["name"] or "").strip()
        for r in conn.execute(
            "SELECT person_id, name FROM persons WHERE person_id IN (" + ph + ")",
            pids)
        if str(r["name"] or "").strip()
    }


def _claim_who(row: dict, persons_map: dict[str, str], roster_map: dict) -> str:
    """观点表「谁」显示名：person_id 查 persons.name；否则 claims.who；
    未命名退回本场槽位名「本场第 N 位上麦者」，宁可显示槽位名也不要猜。
    """
    pid = str(row.get("person_id") or "").strip()
    if pid and persons_map.get(pid):
        return persons_map[pid]
    who = str(row.get("who") or "").strip()
    if who and not identity.is_positional(who):
        return who
    ref = identity.claim_ref(row, roster_map)
    return str(ref.get("who") or who or "一位上麦者")


def _claim_status(conn: sqlite3.Connection, sid: str, started_at: str,
                  row: dict) -> str:
    """派生单条观点状态：新提出 / 重申 / 修正。

    只和更早场次里同 person_key、同 topic 的最近一条比；同场内多条不互相比较，
    避免把一场里的正常发言判成改口。不做「被反驳」（现有只有词面判断，无归属）。

    已知局限：状态相对更早场次算；补发更早历史场次后，已发布场次的状态不会
    自动重算，除非对该场再 /发布。
    """
    pid = str(row.get("person_id") or "").strip()
    who = str(row.get("who") or "").strip()
    key = claims.person_key(pid, who)
    topic = str(row.get("topic") or "").strip()
    cur_stance = str(row.get("stance") or row.get("gist") or "").strip()
    if not key or not topic or not started_at or not cur_stance:
        return "新提出"
    if key.startswith("name:"):
        prev = conn.execute(
            "SELECT c.stance, c.gist FROM claims c "
            "INNER JOIN sessions s ON s.id=c.session_id "
            "WHERE c.session_id<>? AND c.person_id='' AND c.who=? AND c.topic=? "
            "AND (s.deleted_at IS NULL OR s.deleted_at='') "
            "AND COALESCE(s.started_at,'')<>'' AND s.started_at<? "
            "ORDER BY s.started_at DESC, COALESCE(c.t, -1) DESC, c.claim_id DESC "
            "LIMIT 1",
            (sid, key[len("name:"):], topic, started_at)).fetchone()
    else:
        prev = conn.execute(
            "SELECT c.stance, c.gist FROM claims c "
            "INNER JOIN sessions s ON s.id=c.session_id "
            "WHERE c.session_id<>? AND c.person_id=? AND c.topic=? "
            "AND (s.deleted_at IS NULL OR s.deleted_at='') "
            "AND COALESCE(s.started_at,'')<>'' AND s.started_at<? "
            "ORDER BY s.started_at DESC, COALESCE(c.t, -1) DESC, c.claim_id DESC "
            "LIMIT 1",
            (sid, key, topic, started_at)).fetchone()
    if not prev:
        return "新提出"
    prev_stance = str(prev["stance"] or prev["gist"] or "").strip()
    if not prev_stance:
        return "新提出"
    ratio = claims.similarity(cur_stance, prev_stance)
    if ratio >= claims.SAME_RATIO:
        return "重申"
    if ratio < claims.REVISION_RATIO:
        return "修正"
    return "新提出"


def _claims_records(conn: sqlite3.Connection, sid: str,
                    doc_url: str = "") -> tuple[list[dict[str, Any]], str]:
    """把本场 claims 投影成多维表格行列表 + 幂等签名（sha1 前 16 位）。"""
    rows = claims.rows_for(conn, session_id=sid, limit=1000)
    s = store.get_session(conn, sid) or {}
    session_title = str(s.get("title") or "")
    started_at = str(s.get("started_at") or "")
    _, _, started_ms = export._pub_start(started_at)
    persons_map = _persons_map(
        conn, {str(r.get("person_id") or "").strip() for r in rows})
    need_roster = any(
        not (str(r.get("person_id") or "").strip() in persons_map)
        and (not str(r.get("who") or "").strip()
             or identity.is_positional(str(r.get("who") or "")))
        for r in rows)
    roster_map = identity.roster(conn, sid) if need_roster else {}

    records: list[dict[str, Any]] = []
    sig_lines: list[str] = []
    clean_url = str(doc_url or "").strip()
    for c in rows:
        claim_id = str(c.get("claim_id") or "")
        person_id = str(c.get("person_id") or "").strip()
        who_display = _claim_who(c, persons_map, roster_map)
        topic = str(c.get("topic") or "").strip()
        stance = str(c.get("stance") or "").strip()
        gist = str(c.get("gist") or "").strip()
        quote = str(c.get("quote") or "").strip()[:300]
        viewpoint = stance or gist or quote[:80]
        kind_raw = str(c.get("kind") or "claim").strip()
        kind_label = "问题" if kind_raw == "question" else "主张"
        status = _claim_status(conn, sid, started_at, c)
        raw_t = c.get("t")
        t_str = (timefmt.offset(raw_t)
                 if raw_t is not None and raw_t != "" else "")
        fields: dict[str, Any] = {
            "观点": viewpoint,
            "谁": who_display,
            "主题": topic,
            "类别": kind_label,
            "状态": status,
            "场次": session_title or str(c.get("session_title") or ""),
            "时间码": t_str,
            "原话": quote,
            "claim_id": claim_id,
            "session_id": str(sid),
            "person_id": person_id,
        }
        if started_ms:
            fields["日期"] = int(started_ms)
        if clean_url:
            fields["场次文档链接"] = {"link": clean_url, "text": "打开场次文档"}
        records.append(fields)
        sig_lines.append("|".join([
            claim_id, person_id, who_display, topic, viewpoint,
            kind_raw, quote, t_str, status,
        ]))
    raw_sig = "\n".join(sig_lines + ["doc_url=" + clean_url])
    sig = hashlib.sha1(raw_sig.encode("utf-8")).hexdigest()[:16]
    return records, sig


def _search_session_records(client, app_token: str, table_id: str,
                            sid: str) -> tuple[list[str], str]:
    """按 session_id 翻页查出本场全部 record_id（每页 100 条直到 has_more=false）。"""
    body = feishu_http.obj(
        filter=feishu_http.obj(
            conjunction="and",
            conditions=[feishu_http.obj(
                field_name="session_id", operator="is", value=[str(sid)]
            )],
        ),
        automatic_fields=False,
    )
    ids: list[str] = []
    token = ""
    for _ in range(200):
        req = feishu_http.obj(
            app_token=app_token, table_id=table_id,
            page_size=100, page_token=token or None, request_body=body,
        )
        resp = client.bitable.v1.app_table_record.search(req)
        if not resp.success():
            return [], "查询观点表旧行失败：" + _err(resp)
        data = resp.data
        for r in (getattr(data, "items", None) or []):
            rid = str(getattr(r, "record_id", "") or "")
            if rid:
                ids.append(rid)
        token = str(getattr(data, "page_token", "") or "")
        if not getattr(data, "has_more", False) or not token:
            break
    return ids, ""


def _upsert_claims(conn: sqlite3.Connection, bot, sid: str,
                   doc_url: str | None = None,
                   force: bool = False) -> dict:
    """按场次整体替换观点表记录：先按 session_id 删旧行，再批量建新行。

    为什么不逐条 update：claim_id 随要点序号生成，重跑总结要点数量会变，
    逐条 update 会留下孤儿旧行。先删后写；若删成功建失败则台账记 failed，
    下次重试仍是「搜（空）→ 建」，不会重复。
    """
    started = time.monotonic()
    if doc_url is None:
        sess_pub = store.get_publish(conn, sid, KIND_SESSION)
        if sess_pub and sess_pub.get("state") in (STATE_OK, STATE_PARTIAL):
            doc_url = str(sess_pub.get("doc_url") or "")
        else:
            doc_url = ""
    else:
        doc_url = str(doc_url or "")

    records, sig = _claims_records(conn, sid, doc_url)
    prev = store.get_publish(conn, sid, KIND_CLAIMS)

    # 没有观点且台账从未写过：跳过，不建空表也不发请求。
    if not records and prev is None:
        logs.log_event("feishu.claims_upsert", session_id=sid, records=0,
                       deleted=0, created=0, action="skipped")
        return {"ok": True, "skipped": True, "records": 0,
                "deleted": 0, "created": 0, "action": "skipped"}

    # 签名没变且上次成功（非 force）：不发任何删/写请求。
    if (not force and prev and prev.get("state") == STATE_OK
            and prev.get("version") == sig):
        logs.log_event("feishu.claims_upsert", session_id=sid,
                       records=len(records), deleted=0, created=0,
                       action="skipped")
        return {"ok": True, "skipped": True, "records": len(records),
                "deleted": 0, "created": 0, "action": "skipped"}

    attempts = int((prev or {}).get("attempts") or 0) + 1

    def _fail(err: str, deleted: int = 0, created: int = 0) -> dict:
        latency_ms = int((time.monotonic() - started) * 1000)
        store.upsert_publish(
            conn, sid, KIND_CLAIMS, state=STATE_FAILED, version=sig,
            doc_url=doc_url, error=str(err)[:1000],
            meta={"records": len(records), "deleted": deleted,
                  "created": created},
            latency_ms=latency_ms, attempts=attempts)
        logs.log_event("feishu.claims_upsert", level="warn", session_id=sid,
                       records=len(records), deleted=deleted, created=created,
                       action="failed", error=str(err)[:200])
        return {"ok": False, "error": str(err), "records": len(records),
                "deleted": deleted, "created": created}

    tbl = ensure_claims_table(conn, bot)
    if not tbl.get("ok"):
        return _fail(str(tbl.get("error") or "建观点表失败"))

    app_token = tbl["app_token"]
    table_id = tbl["table_id"]
    client = bot.client

    old_ids, search_err = _search_session_records(client, app_token, table_id, sid)
    if search_err:
        return _fail(search_err)

    deleted = 0
    if old_ids:
        for i in range(0, len(old_ids), 500):
            chunk = old_ids[i:i + 500]
            body = feishu_http.obj(records=chunk)
            resp = client.bitable.v1.app_table_record.batch_delete(
                feishu_http.obj(app_token=app_token, table_id=table_id,
                                request_body=body))
            if not resp.success():
                return _fail("删除观点表旧行失败：" + _err(resp),
                             deleted=deleted)
            deleted += len(chunk)

    created = 0
    if records:
        for i in range(0, len(records), 500):
            chunk = records[i:i + 500]
            rec_objs = [feishu_http.obj(fields=f) for f in chunk]
            body = feishu_http.obj(records=rec_objs)
            resp = client.bitable.v1.app_table_record.batch_create(
                feishu_http.obj(app_token=app_token, table_id=table_id,
                                request_body=body))
            if not resp.success():
                return _fail("写入观点表失败：" + _err(resp),
                             deleted=deleted, created=created)
            created += len(chunk)

    latency_ms = int((time.monotonic() - started) * 1000)
    store.upsert_publish(
        conn, sid, KIND_CLAIMS, state=STATE_OK, version=sig,
        doc_url=doc_url, error="",
        meta={"table_id": table_id, "records": len(records),
              "deleted": deleted, "created": created},
        latency_ms=latency_ms, attempts=attempts,
        published_at=store.now())
    logs.log_event("feishu.claims_upsert", session_id=sid,
                   records=len(records), deleted=deleted,
                   created=created, action="created")
    return {"ok": True, "skipped": False, "records": len(records),
            "deleted": deleted, "created": created, "action": "created",
            "table_id": table_id, "grant_error": tbl.get("grant_error") or ""}


# ---------------------------------------------------------------- 收尾卡

def _btn(text: str, value: dict | None = None, primary: bool = False,
         url: str = "") -> dict:
    out = {"tag": "button", "type": "primary" if primary else "default",
           "text": {"tag": "plain_text", "content": text}}
    if value:
        out["value"] = value
    if url:
        out["url"] = url
    return out


def wrapup_card(result: dict, doc: dict) -> dict:
    """发布完成后的收尾卡：文档链接 + 补名入口 + 失败重试。

    卡片按钮是回调，飞书后台没订阅「卡片回传交互」时静默无反应，所以
    note 里必须写文字兜底。重新发布会重写正文，旧评论可能失锚。
    """
    sid = str(result.get("session_id") or "")
    title = str(result.get("title") or doc.get("title") or "这场")
    short = title.split(" · ")[-1] if " · " in title else title
    doc_state = str(result.get("doc_state") or doc.get("state") or "")
    state = str(result.get("state") or "")
    ok = bool(result.get("ok"))
    counts = result.get("counts") or doc.get("counts") or {}
    if doc_state == DOC_PENDING_TRANSCRIPT:
        head = "📄《%s》已建档 · 待补转写" % short
        template = "orange"
        body = ("这场只录了音。补转写出字后，文档会自动更新；"
                "现在先不要把它当总结看。")
    elif doc_state == DOC_PENDING_SUMMARY:
        head = "📄《%s》已建档 · 待总结" % short
        template = "orange"
        body = "逐字稿已就绪，正在等待整场总结；总结出来后会自动更新到同一篇文档。"
    elif doc_state == DOC_FAILED_SUMMARY:
        head = "📄《%s》已建档 · 待总结" % short
        template = "orange"
        body = "逐字稿已就绪，刚才总结没成功；可稍后点「重试发布」或发 /总结。"
    elif ok and state == STATE_PARTIAL:
        head = "📄《%s》已发布（场次表待补）" % short
        template = "orange"
        body = (str(doc.get("overview") or "")
                or "文档已可读；场次表这行没写成功，下次发布或 /发布 会补。")
    elif ok:
        head = "📄《%s》已发布" % short
        template = "green"
        body = str(doc.get("overview") or "") or "这场已经发布。"
    else:
        head = "⚠️《%s》发布失败" % short
        template = "red"
        body = str(result.get("error") or "文档没建出来。内容还在本地，可重试。")

    doc_url = str(result.get("doc_url") or "")
    table_url = str(result.get("record_url") or result.get("base_url") or "")
    actions: list[dict] = []
    if doc_url:
        actions.append(_btn("看文档", primary=True, url=doc_url))
    if table_url:
        actions.append(_btn("场次表", url=table_url))
    if doc_state != DOC_PENDING_TRANSCRIPT:
        actions.append(_btn("补名", {"act": "publish_name",
                                     "session_id": sid}))
    if not ok or state == STATE_PARTIAL:
        actions.append(_btn("重试发布", {"act": "publish_retry",
                                         "session_id": sid},
                            primary=not doc_url))
    if not actions:
        actions.append(_btn("重试发布", {"act": "publish_retry",
                                         "session_id": sid}, primary=True))

    elements: list[dict] = []
    if body:
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": body[:800]}})
    elements.append({"tag": "div", "fields": [
        {"is_short": True,
         "text": {"tag": "lark_md",
                  "content": "**观点**\n%d 条" % int(counts.get("points") or 0)}},
        {"is_short": True,
         "text": {"tag": "lark_md",
                  "content": "**高光**\n%d 条" % int(counts.get("highlights") or 0)}},
    ]})
    elements.append({"tag": "action", "actions": actions})
    note_text = "本场已自动建档；点「看文档」看详情，点「场次表」看全部场次与观点总表。逐字稿与时间轴在桌面端。"
    if not ok or state == STATE_PARTIAL:
        note_text = "按钮没反应可发 /发布 重试。" + note_text
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note_text}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": head}},
            "elements": elements}


def wrapup_text(result: dict, doc: dict | None = None) -> str:
    """卡片发不出去时的纯文本版（群聊未授权卡片、回调没订阅都能用）。"""
    doc = doc or {}
    sid = str(result.get("session_id") or "")
    title = str(result.get("title") or doc.get("title") or "这场")
    state = str(result.get("state") or "")
    doc_state = str(result.get("doc_state") or doc.get("state") or "")
    if not result.get("ok"):
        return ("❌《%s》发布失败：%s\n发 /发布 重试。"
                % (title, result.get("error") or "网络或权限问题"))
    if doc_state == DOC_PENDING_TRANSCRIPT:
        head = "📄《%s》已建档 · 待补转写" % title
    elif doc_state in (DOC_PENDING_SUMMARY, DOC_FAILED_SUMMARY):
        head = "📄《%s》已建档 · 待总结" % title
    elif state == STATE_PARTIAL:
        head = "📄《%s》已发布（场次表待补）" % title
    else:
        head = "📄《%s》已发布" % title
    lines = [head]
    if result.get("overview"):
        lines.append(str(result["overview"]))
    if result.get("doc_url"):
        lines.append("文档：" + str(result["doc_url"]))
    if result.get("record_url"):
        lines.append("场次表：" + str(result["record_url"]))
    if result.get("error"):
        lines.append("注意：" + str(result["error"]))
    lines.append("补名：/补名 %s" % sid)
    return "\n".join(lines)
