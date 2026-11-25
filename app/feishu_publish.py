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

    FEISHU_PUBLISH_ENABLED=1        总开关；默认关，桌面用户可不开
    FEISHU_PUBLISH_FOLDER_TOKEN     场次文档文件夹；不填复用 doc_folder_token
    FEISHU_BASE_FOLDER_TOKEN        场次表文件夹；不填复用文档文件夹
    FEISHU_BASE_APP_TOKEN           可选：预建 Base；不填首次发布自动建
    FEISHU_BASE_TABLE_ID            可选：预建数据表；不填自动建
    FEISHU_PUBLISH_BASE=0           只发文档、不写场次表
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import traceback
from typing import Any

from . import export, feishu, logs, store

KIND_SESSION = "session"
STATE_OK = "ok"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"

DOC_READY = "ready"
DOC_PENDING_TRANSCRIPT = "pending_transcript"
DOC_PENDING_SUMMARY = "pending_summary"
DOC_FAILED_SUMMARY = "failed_summary"

BASE_NAME = "LiveNotes 场次"
TABLE_NAME = "场次"

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
    {"field_name": "session_id", "type": 1},
    # 主题词是自由文本，做成多选会因为「选项不存在」写不进去；先用文本存，
    # Base 里仍能用「包含」筛选，P1 再统一管理选项后迁移。
    {"field_name": "标签", "type": 1},
    # 云端链路：区分「完整采集」与「仅采集，等离线补字」的场次。
    {"field_name": "采集", "type": 1},
]

_BASE_READY: set[tuple[str, str]] = set()


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
        "base_folder_token": str(raw.get("base_folder_token") or ""),
        "base_url_base": str(raw.get("base_url_base") or ""),
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

        doc = export.build_publish(conn, sid)
        if not doc.get("ok"):
            return {"ok": False, "session_id": sid,
                    "skipped": bool(doc.get("skipped")),
                    "reason": str(doc.get("reason") or ""),
                    "error": doc.get("error") or "生成发布内容失败"}

        row = store.get_publish(conn, sid) or {}
        cfg = _cfg(bot_obj)
        owner = _owner(bot_obj)
        version = _version(doc["title"], doc["markdown"])
        base_on = bool(cfg.get("base_enabled", True))
        same_version = row.get("version") == version
        # 上次正文没写成功（state=failed）时必须重写；其余同版本情况正文不动，
        # 只补表格/授权，避免反复清空块把评论锚点弄丢。
        can_reuse = (bool(row.get("doc_token")) and same_version and not force
                     and row.get("state") in (STATE_OK, STATE_PARTIAL))

        # 版本没变、上次也成功：什么都不写。重试命令不该把同一篇正文反复清空。
        if (same_version and not force and row.get("state") == STATE_OK
                and row.get("doc_url")
                and (row.get("record_id") or not base_on)):
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

        base_res: dict = {"ok": True, "skipped": True}
        if base_on:
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

        state = STATE_OK
        if not doc_ok:
            state = STATE_FAILED
        elif warnings:
            state = STATE_PARTIAL
        latency_ms = int((time.monotonic() - started) * 1000)
        old_meta = row.get("meta") or {}
        meta = {
            "doc_state": doc["state"],
            "base_url": base_res.get("base_url") or old_meta.get("base_url") or "",
            "record_url": (base_res.get("record_url")
                           or old_meta.get("record_url") or ""),
            "base_skipped": bool(base_res.get("skipped")),
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
    from lark_oapi.api.bitable.v1 import (
        AppTableFieldProperty, AppTableFieldPropertyOption)
    opts = spec.get("options") or []
    if not opts:
        return None
    return (AppTableFieldProperty.builder().options([
        AppTableFieldPropertyOption.builder().name(str(o)).build()
        for o in opts]).build())


def _create_table(client, app_token: str) -> dict:
    from lark_oapi.api.bitable.v1 import (
        AppTableCreateHeader, CreateAppTableRequest, CreateAppTableRequestBody,
        ReqTable)
    headers = []
    for spec in FIELD_DEFS:
        b = (AppTableCreateHeader.builder()
             .field_name(spec["field_name"]).type(int(spec["type"])))
        prop = _opt_property(spec)
        if prop is not None:
            b = b.property(prop)
        headers.append(b.build())
    table = (ReqTable.builder().name(TABLE_NAME)
             .default_view_name("最近场次").fields(headers).build())
    body = CreateAppTableRequestBody.builder().table(table).build()
    resp = client.bitable.v1.app_table.create(
        CreateAppTableRequest.builder().app_token(app_token)
        .request_body(body).build())
    if not resp.success():
        return {"ok": False, "error": "建场次表失败：" + _err(resp)}
    table_id = str(getattr(resp.data, "table_id", "") or "")
    if not table_id:
        return {"ok": False, "error": "建场次表没返回 table_id"}
    return {"ok": True, "table_id": table_id}


def _find_table(client, app_token: str) -> str:
    from lark_oapi.api.bitable.v1 import ListAppTableRequest
    token = ""
    for _ in range(10):
        b = ListAppTableRequest.builder().app_token(app_token).page_size(100)
        if token:
            b = b.page_token(token)
        resp = client.bitable.v1.app_table.list(b.build())
        if not resp.success():
            return ""
        data = resp.data
        for item in (getattr(data, "items", None) or []):
            if str(getattr(item, "name", "")) == TABLE_NAME:
                return str(getattr(item, "table_id", "") or "")
        token = str(getattr(data, "page_token", "") or "")
        if not getattr(data, "has_more", False) or not token:
            break
    return ""


def _ensure_fields(client, app_token: str, table_id: str) -> str:
    """老表补字段：用户在飞书里删了「观点数」，发布不该静默丢这一列。"""
    key = (app_token, table_id)
    if key in _BASE_READY:
        return ""
    from lark_oapi.api.bitable.v1 import (
        AppTableField, CreateAppTableFieldRequest, ListAppTableFieldRequest)
    resp = client.bitable.v1.app_table_field.list(
        ListAppTableFieldRequest.builder().app_token(app_token)
        .table_id(table_id).page_size(100).build())
    if not resp.success():
        return "读场次表字段失败：" + _err(resp)
    names = {str(getattr(f, "field_name", ""))
             for f in (getattr(resp.data, "items", None) or [])}
    for spec in FIELD_DEFS:
        if spec["field_name"] in names:
            continue
        b = (AppTableField.builder()
             .field_name(spec["field_name"]).type(int(spec["type"])))
        prop = _opt_property(spec)
        if prop is not None:
            b = b.property(prop)
        created = client.bitable.v1.app_table_field.create(
            CreateAppTableFieldRequest.builder().app_token(app_token)
            .table_id(table_id).request_body(b.build()).build())
        if not created.success():
            return "补字段「%s」失败：%s" % (spec["field_name"], _err(created))
    _BASE_READY.add(key)
    return ""


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

    if not app_token:
        from lark_oapi.api.bitable.v1 import CreateAppRequest, ReqApp
        b = ReqApp.builder().name(BASE_NAME)
        folder = str(cfg.get("base_folder_token") or "").strip()
        if folder:
            b = b.folder_token(folder)
        resp = client.bitable.v1.app.create(
            CreateAppRequest.builder().request_body(b.build()).build())
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
        grant_error = ""
        owner = _owner(bot)
        if owner:
            grant = bot._grant_doc(app_token, owner, doc_type="bitable")
            if not grant.get("ok"):
                grant_error = str(grant.get("error") or "")
        logs.log_event("feishu.base_created", app_token=app_token,
                       shared=not grant_error)

    if not table_id:
        table_id = _find_table(client, app_token)
        if not table_id:
            made = _create_table(client, app_token)
            if not made.get("ok"):
                return made
            table_id = made["table_id"]
        store.set_kv(conn, "feishu_base_table_id", table_id)

    bad = _ensure_fields(client, app_token, table_id)
    if bad:
        return {"ok": False, "error": bad}
    return {"ok": True, "app_token": app_token, "table_id": table_id,
            "base_url": base_url,
            "grant_error": locals().get("grant_error", "")}


def _find_record(client, app_token: str, table_id: str, sid: str) -> str:
    from lark_oapi.api.bitable.v1 import (
        Condition, FilterInfo, SearchAppTableRecordRequest,
        SearchAppTableRecordRequestBody)
    body = (SearchAppTableRecordRequestBody.builder()
            .filter(FilterInfo.builder().conjunction("and").conditions([
                Condition.builder().field_name("session_id").operator("is")
                .value([str(sid)]).build()]).build())
            .automatic_fields(False).build())
    resp = client.bitable.v1.app_table_record.search(
        SearchAppTableRecordRequest.builder().app_token(app_token)
        .table_id(table_id).request_body(body).build())
    if not resp.success():
        return ""
    items = getattr(resp.data, "items", None) or []
    for r in items:
        rid = str(getattr(r, "record_id", "") or "")
        if rid:
            return rid
    return ""


def _record_fields(doc: dict, doc_ok: bool, doc_url: str) -> dict:
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
        fields["录音链接"] = {"link": doc["audio_url"], "text": "打开录音"}
    topics = [str(t).strip() for t in (doc.get("topics") or []) if str(t).strip()]
    if topics:
        fields["标签"] = "、".join(topics[:10])
    return fields


def _upsert_session_row(conn: sqlite3.Connection, bot, doc: dict, doc_ok: bool,
                        doc_url: str, known_record: str = "") -> dict:
    """按 session_id upsert 场次表一行；不会因为重复发布多出一行。"""
    base = ensure_base(conn, bot)
    if not base.get("ok"):
        return base
    app_token = base["app_token"]
    table_id = base["table_id"]
    client = bot.client
    fields = _record_fields(doc, doc_ok, doc_url)
    record_id = str(known_record or "")
    if not record_id:
        record_id = _find_record(client, app_token, table_id,
                                 str(doc.get("session_id") or ""))
    from lark_oapi.api.bitable.v1 import (
        AppTableRecord, CreateAppTableRecordRequest, UpdateAppTableRecordRequest)
    record_body = AppTableRecord.builder().fields(fields).build()
    if record_id:
        resp = client.bitable.v1.app_table_record.update(
            UpdateAppTableRecordRequest.builder().app_token(app_token)
            .table_id(table_id).record_id(record_id)
            .request_body(record_body).build())
        created = False
    else:
        resp = client.bitable.v1.app_table_record.create(
            CreateAppTableRecordRequest.builder().app_token(app_token)
            .table_id(table_id).request_body(record_body).build())
        created = True
    if not resp.success():
        return {"ok": False, "error": "写场次表失败：" + _err(resp)}
    record = getattr(resp.data, "record", None)
    rid = str(getattr(record, "record_id", "") or record_id)
    record_url = str(getattr(record, "record_url", "") or "")
    logs.log_event("feishu.base_upsert", session_id=doc.get("session_id"),
                   table="session", record_id=rid,
                   action="created" if created else "updated")
    return {"ok": True, "record_id": rid, "record_url": record_url,
            "base_url": base.get("base_url") or "",
            "grant_error": base.get("grant_error") or ""}


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
    elif doc_state in (DOC_PENDING_SUMMARY, DOC_FAILED_SUMMARY):
        head = "📄《%s》已建档 · 待总结" % short
        template = "orange"
        body = "逐字稿已就绪，总结还没成功。总结出来后文档会自动更新。"
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
    actions: list[dict] = []
    if doc_url:
        actions.append(_btn("看文档", primary=True, url=doc_url))
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
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
        "按钮没反应就发 /文档 或 /发布。重新发布会重写正文，旧评论可能失锚；"
        "逐字稿与时间轴在桌面端。"}]})
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
