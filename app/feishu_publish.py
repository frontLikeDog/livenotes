# -*- coding: utf-8 -*-
"""发布到飞书原生阅读面：一场 = 一篇云文档 + 场次表里的一行。

为什么不是同步：SQLite 是唯一真相，飞书只是投影。发布只读库、不写库
（除了自己的台账），也不做反向同步 —— 唯一的反向通道是「补名」，那是 P1。
所以发布失败时，录制、补转写、总结都照常工作，文档等下一次重试即可。

幂等：
  * version = sha256(标题 + 正文 + 署名 + 模板版本)，写进 ``feishu_publish``；
    版本没变就不重复转块、不重复建文档。
  * 文档建好但正文写失败时，把 document_id 记下来，重试走 ``rewrite_docx``，
    不会出现第二篇空文档。
  * 场次表按 session_id 查已有行：有就更新，没有才新建。

失败隔离：
  * 文档失败 → 记 failed + 错误；飞书没配 / 没开 → 静默跳过（只写日志）。
  * 文档成功、场次表失败 → state=partial，下次发布只补表，不重写正文。
  * 关卡片失败 → 不影响发布结果，下一场再发。

调用方（都不在本模块里做）：
  * ``tasks._run_summary`` 成功/失败后自动发布；
  * ``live.stop`` 对仅采集场次发布「待补转写」骨架；
  * 飞书 ``/发布`` 与 HTTP ``POST /api/publish`` 手动重试。
"""
from __future__ import annotations

import hashlib
import threading
import time
import traceback

from . import export, feishu, logs, metrics, store

_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_TABLES: dict[str, str] = {}          # base_app_token -> table_id
_BOT = None                            # 进程内复用；懒建，没开发布就不碰 lark


def config() -> dict:
    """发布配置。环境变量优先，其次 data/feishu.json。"""
    cfg = feishu.load_config()
    return {
        "enabled": bool(cfg.get("publish_enabled")),
        "app_id": str(cfg.get("app_id") or ""),
        "app_secret": str(cfg.get("app_secret") or ""),
        "folder": str(cfg.get("publish_folder_token") or ""),
        "owner": str(cfg.get("publish_owner") or cfg.get("owner_open_id") or ""),
        "doc_url_base": str(cfg.get("doc_url_base") or ""),
        "base_app_token": str(cfg.get("base_app_token") or ""),
        "base_table_id": str(cfg.get("base_table_id") or ""),
        "base_url_base": str(cfg.get("base_url_base") or ""),
    }


def _default_bot(cfg: dict):
    """懒建一个 Bot。只在真发布时创建，仅采集节点不装 lark 也能启动。"""
    global _BOT
    with _LOCK:
        if _BOT is None:
            bot = feishu.Bot()
            # 发布可以单独指定文件夹；没指定就沿用云文档文件夹。
            if cfg.get("folder"):
                bot.cfg = dict(bot.cfg or {})
                bot.cfg["doc_folder_token"] = cfg["folder"]
            _BOT = bot
        return _BOT


def reset_bot() -> None:
    """测试/换凭证用：丢掉缓存的 Bot。"""
    global _BOT
    with _LOCK:
        _BOT = None


def version_of(doc: dict) -> str:
    h = hashlib.sha256()
    for part in (doc.get("title") or "", doc.get("markdown") or "",
                 doc.get("names") or "", doc.get("template") or ""):
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _err(resp) -> str:
    return "code=%s %s" % (getattr(resp, "code", ""), getattr(resp, "msg", ""))


# ---------------------------------------------------------------- 场次表

# 字段类型：1 文本 / 2 数字 / 5 日期 / 15 超链接。
# 不建关联字段：场次表要能单独分享，关联到人物表会带走权限问题。
_BASE_FIELDS = (
    ("标题", 1), ("日期", 5), ("房间", 1), ("形态", 1), ("主持", 1),
    ("上麦人数", 2), ("时长分钟", 2), ("一句话", 1), ("观点数", 2),
    ("高光数", 2), ("状态", 1), ("文档链接", 15), ("录音链接", 15),
    ("session_id", 1), ("标签", 1), ("采集", 1),
)
_BASE_NAME = "场次"


def _find_table(client, app_token: str) -> str:
    from lark_oapi.api.bitable.v1 import ListAppTableRequest
    page_token = ""
    for _ in range(20):
        builder = (ListAppTableRequest.builder()
                   .app_token(app_token).page_size(100))
        if page_token:
            builder = builder.page_token(page_token)
        resp = client.bitable.v1.app_table.list(builder.build())
        if not resp.success():
            raise RuntimeError("列出场次表失败：" + _err(resp))
        data = resp.data
        for t in (getattr(data, "items", None) or []):
            if str(getattr(t, "name", "") or "") == _BASE_NAME:
                return str(getattr(t, "table_id", "") or "")
        if not getattr(data, "has_more", False):
            break
        page_token = str(getattr(data, "page_token", "") or "")
        if not page_token:
            break
    return ""


def _create_table(client, app_token: str) -> str:
    from lark_oapi.api.bitable.v1 import (
        AppTableCreateHeader, CreateAppTableRequest, ReqTable)
    headers = [AppTableCreateHeader.builder().field_name(name).type(typ).build()
               for name, typ in _BASE_FIELDS]
    body = (ReqTable.builder().name(_BASE_NAME).default_view_name("全部场次")
            .fields(headers).build())
    req = (CreateAppTableRequest.builder()
           .app_token(app_token).request_body(body).build())
    resp = client.bitable.v1.app_table.create(req)
    if not resp.success():
        raise RuntimeError("创建场次表失败：" + _err(resp))
    table_id = str(getattr(resp.data, "table_id", "") or "")
    if not table_id:
        raise RuntimeError("创建场次表没返回 table_id")
    logs.log_event("feishu.base_table_created", app_token=app_token[:8],
                   table_id=table_id)
    return table_id


def _table_id(client, cfg: dict) -> str:
    app_token = cfg["base_app_token"]
    with _LOCK:
        cached = _TABLES.get(app_token) or cfg.get("base_table_id") or ""
        if cached:
            _TABLES[app_token] = cached
            return cached
    table_id = _find_table(client, app_token) or _create_table(client, app_token)
    with _LOCK:
        _TABLES[app_token] = table_id
    return table_id


def _base_url(cfg: dict, table_id: str) -> str:
    app_token = cfg["base_app_token"]
    if not app_token:
        return ""
    base = (cfg.get("base_url_base") or "https://feishu.cn/base/").rstrip("/")
    return "%s/%s?table=%s" % (base, app_token, table_id)


def _row_fields(doc: dict, doc_url: str) -> dict:
    row = doc.get("row") or {}
    fields: dict = {
        "标题": str(row.get("title") or "")[:200],
        "session_id": str(row.get("session_id") or ""),
        "状态": str(row.get("status") or ""),
        "采集": str(row.get("mode") or ""),
        "时长分钟": int(row.get("duration_min") or 0),
        "上麦人数": int(row.get("speakers") or 0),
        "观点数": int(row.get("points") or 0),
        "高光数": int(row.get("highlights") or 0),
    }
    if row.get("date_ms"):
        fields["日期"] = int(row["date_ms"])
    for key, col in (("room", "房间"), ("shape", "形态"), ("host", "主持"),
                     ("overview", "一句话")):
        val = str(row.get(key) or "")[:500]
        if val:
            fields[col] = val
    topics = [str(t) for t in (row.get("topics") or []) if str(t).strip()]
    if topics:
        fields["标签"] = "、".join(topics[:8])[:200]
    if doc_url:
        fields["文档链接"] = {"link": doc_url, "text": "打开文档"}
    if row.get("audio_url"):
        fields["录音链接"] = {"link": str(row["audio_url"]), "text": "播放 / 下载"}
    return fields


def _find_record(client, app_token: str, table_id: str,
                 sid: str, record_id: str = "") -> str:
    from lark_oapi.api.bitable.v1 import (
        Condition, FilterInfo, SearchAppTableRecordRequest,
        SearchAppTableRecordRequestBody)
    if record_id:
        return record_id
    cond = (Condition.builder().field_name("session_id").operator("is")
            .value([sid]).build())
    body = (SearchAppTableRecordRequestBody.builder()
            .filter(FilterInfo.builder().conjunction("and")
                    .conditions([cond]).build())
            .automatic_fields(False).build())
    req = (SearchAppTableRecordRequest.builder()
           .app_token(app_token).table_id(table_id).page_size(5)
           .request_body(body).build())
    resp = client.bitable.v1.app_table_record.search(req)
    if not resp.success():
        raise RuntimeError("查询场次表失败：" + _err(resp))
    for item in (getattr(resp.data, "items", None) or []):
        rid = str(getattr(item, "record_id", "") or "")
        if rid:
            return rid
    return ""


def _upsert_base(client, cfg: dict, doc: dict, doc_url: str,
                 old_record: str = "") -> tuple[bool, str, str]:
    """写场次表。返回 (ok, error, record_id)。没配 Base 时 ok=True、跳过。"""
    if not cfg.get("base_app_token"):
        return True, "", ""
    from lark_oapi.api.bitable.v1 import (
        AppTableRecord, CreateAppTableRecordRequest, UpdateAppTableRecordRequest)
    table_id = _table_id(client, cfg)
    app_token = cfg["base_app_token"]
    sid = str((doc.get("row") or {}).get("session_id") or "")
    fields = _row_fields(doc, doc_url)
    record_id = _find_record(client, app_token, table_id, sid, old_record)
    if record_id:
        body = AppTableRecord.builder().fields(fields).build()
        req = (UpdateAppTableRecordRequest.builder()
               .app_token(app_token).table_id(table_id).record_id(record_id)
               .request_body(body).build())
        resp = client.bitable.v1.app_table_record.update(req)
        if not resp.success():
            return False, "更新场次表失败：" + _err(resp), record_id
        rec = getattr(resp.data, "record", None) if resp.data else None
        return True, "", str(getattr(rec, "record_id", "") or record_id)

    body = AppTableRecord.builder().fields(fields).build()
    req = (CreateAppTableRecordRequest.builder()
           .app_token(app_token).table_id(table_id).request_body(body).build())
    resp = client.bitable.v1.app_table_record.create(req)
    if not resp.success():
        return False, "写入场次表失败：" + _err(resp), ""
    rec = getattr(resp.data, "record", None) if resp.data else None
    rid = str(getattr(rec, "record_id", "") or "")
    if not rid:
        return False, "场次表没返回 record_id", ""
    return True, "", rid


# ---------------------------------------------------------------- 卡片

def publish_card(doc: dict, doc_url: str, record_url: str = "") -> dict:
    state = str(doc.get("state") or "")
    if state == "pending_transcript":
        title, template = "🎙 已建文档 · 待补转写", "orange"
        body = "这场是仅采集，转写完成后文档会自动更新。"
    elif state == "pending_summary":
        title, template = "📄 已建文档 · 待总结", "orange"
        body = "逐字稿已在，总结还没生成；生成后文档会自动更新。"
    else:
        title, template = "📄 场次文档已发布", "green"
        body = str(doc.get("one_line") or "")
    elements: list[dict] = []
    if body:
        elements.append({"tag": "div", "text": {
            "tag": "lark_md",
            "content": "**%s**\n%s" % (str((doc.get("row") or {}).get("title")
                                          or doc.get("title") or "")[:80], body)}})
    actions = []
    if doc_url:
        actions.append({"tag": "button", "type": "primary",
                        "text": {"tag": "plain_text", "content": "打开文档"},
                        "url": doc_url})
    if record_url:
        actions.append({"tag": "button", "type": "default",
                        "text": {"tag": "plain_text", "content": "场次表"},
                        "url": record_url})
    if actions:
        elements.append({"tag": "action", "actions": actions})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
                     "由 LiveNotes 发布；逐字稿与时间轴在桌面端。"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _send_card(bot, cfg: dict, doc: dict, doc_url: str,
               record_url: str = "") -> bool:
    owner = str(cfg.get("owner") or "")
    if not owner:
        return False
    card = publish_card(doc, doc_url, record_url)
    try:
        if bot.send_card(card, owner):
            return True
    except Exception:
        traceback.print_exc()
    try:
        row = doc.get("row") or {}
        return bool(bot.send_text(
            "📄 %s\n%s\n%s" % (row.get("title") or doc.get("title") or "",
                               doc.get("one_line") or "", doc_url), owner))
    except Exception:
        traceback.print_exc()
        return False


# ---------------------------------------------------------------- 发布

def publish(conn, sid: str, *, force: bool = False, bot=None) -> dict:
    """发布/更新一场。任何失败都只记录、不抛异常。"""
    try:
        return _publish(conn, sid, force=force, bot=bot)
    except Exception as e:
        traceback.print_exc()
        msg = "%s: %s" % (type(e).__name__, e)
        try:
            store.set_publish(conn, sid, state="failed", error=msg)
        except Exception:
            traceback.print_exc()
        logs.log_event("feishu.publish_error", level="warn", session_id=sid,
                       error=msg)
        return {"ok": False, "state": "failed", "message": "发布异常：" + msg}


def _publish(conn, sid: str, *, force: bool, bot) -> dict:
    cfg = config()
    if not cfg["enabled"]:
        logs.log_event("feishu.publish_skipped", session_id=sid,
                       msg="发布未开启（FEISHU_PUBLISH_ENABLED）")
        return {"ok": False, "skipped": True, "reason": "disabled",
                "message": "发布未开启（设 FEISHU_PUBLISH_ENABLED=1）"}

    doc = export.to_publish_md(conn, sid)
    if not doc.get("ok"):
        if doc.get("skipped"):
            logs.log_event("feishu.publish_skipped", session_id=sid,
                           msg=str(doc.get("message") or doc.get("reason") or ""))
            return doc
        return {"ok": False, "message": str(doc.get("message") or "无法生成发布稿")}

    ver = version_of(doc)
    old = store.get_publish(conn, sid) or {}
    old_state = str(old.get("state") or "")
    old_ver = str(old.get("version") or "")
    body_done = bool(old.get("doc_token")) and old_ver == ver
    if old_state == "ok" and old_ver == ver and not force:
        logs.log_event("feishu.publish_unchanged", session_id=sid,
                       version=ver)
        return {"ok": True, "skipped": True, "reason": "unchanged",
                "state": "ok", "doc_url": old.get("doc_url") or "",
                "record_id": old.get("record_id") or "",
                "version": ver, "status": doc.get("state")}

    started = time.monotonic()
    attempts = int(old.get("attempts") or 0) + 1
    doc_id = str(old.get("doc_token") or "")
    doc_url = str(old.get("doc_url") or "")
    status = str(doc.get("state") or "")
    base_row = dict(old)
    latency = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731

    if not bot:
        try:
            bot = _default_bot(cfg)
        except Exception as e:
            msg = "飞书未配置：" + str(e)
            store.set_publish(conn, sid, state="failed", status=status,
                              error=msg, attempts=attempts,
                              latency_ms=latency())
            logs.log_event("feishu.publish_failed", level="warn", session_id=sid,
                           error=msg)
            return {"ok": False, "state": "failed", "message": msg}

    # 1. 文档正文
    if not body_done:
        if doc_id:
            out = bot.rewrite_docx(doc_id, doc.get("markdown") or "")
        else:
            out = bot.create_docx(doc.get("title") or "", doc.get("markdown") or "",
                                  share_with=str(cfg.get("owner") or ""))
        if not out.get("ok"):
            # 文档可能已经建出来（正文失败）：把 id 存下来，重试原地重写。
            doc_id = str(out.get("document_id") or doc_id or "")
            doc_url = str(out.get("url") or doc_url or "")
            msg = str(out.get("error") or "文档发布失败")
            store.set_publish(conn, sid, state="failed", status=status,
                              doc_token=doc_id, doc_url=doc_url,
                              error=msg, attempts=attempts,
                              latency_ms=latency())
            logs.log_event("feishu.publish_failed", level="warn", session_id=sid,
                           error=msg[:200], doc=bool(doc_id))
            metrics.METRICS.inc("feishu.publish_failed")
            return {"ok": False, "state": "failed", "message": msg,
                    "doc_url": doc_url}
        doc_id = str(out.get("document_id") or doc_id or "")
        doc_url = str(out.get("url") or doc_url or "")
        if not doc_id:
            msg = "飞书没有返回文档 id"
            store.set_publish(conn, sid, state="failed", status=status,
                              error=msg, attempts=attempts,
                              latency_ms=latency())
            return {"ok": False, "state": "failed", "message": msg}
        # 正文成功立刻落版本：即使下一步建表失败，重试也不会重写文档。
        store.set_publish(conn, sid, state="partial", status=status,
                          version=ver, title=doc.get("title") or "",
                          doc_token=doc_id, doc_url=doc_url,
                          attempts=attempts, latency_ms=latency())
    else:
        store.set_publish(conn, sid, status=status, attempts=attempts,
                          doc_token=doc_id, doc_url=doc_url)

    # 2. 场次表一行
    record_id = str(base_row.get("record_id") or "")
    base_ok, base_err, record_id = _upsert_base(
        bot.client if hasattr(bot, "client") else None, cfg, doc, doc_url,
        record_id)
    record_url = _base_url(cfg, _table_id(bot.client, cfg)) if (
        cfg.get("base_app_token") and base_ok and hasattr(bot, "client")) else ""
    state = "ok" if base_ok else "partial"
    error = "" if base_ok else base_err
    row = store.set_publish(
        conn, sid, state=state, status=status, version=ver,
        title=doc.get("title") or "", doc_token=doc_id, doc_url=doc_url,
        record_id=record_id, base_url=record_url, error=error,
        attempts=attempts, latency_ms=latency())

    # 3. 完成卡片：只在「有完整总结」且没发过时发。骨架/待总结不发，
    #    等真发布那一刻再提醒；失败重试也不重复刷屏。
    card_sent = int(row.get("card_sent") or 0)
    if state == "ok" and status == "ready" and not card_sent:
        if _send_card(bot, cfg, doc, doc_url, record_url):
            card_sent = 1
            store.set_publish(conn, sid, card_sent=1)

    logs.log_event(
        "feishu.publish_ok" if state == "ok" else "feishu.publish_partial",
        level="info" if state == "ok" else "warn", session_id=sid,
        version=ver, status=status, doc=bool(doc_id), base=bool(record_id),
        latency_ms=row.get("latency_ms") or 0)
    metrics.METRICS.inc("feishu.publish_ok" if state == "ok"
                        else "feishu.publish_partial")
    out = {"ok": True, "state": state, "status": status, "version": ver,
           "doc_url": doc_url, "record_id": record_id,
           "record_url": record_url, "message": error}
    if error:
        out["message"] = "文档已发布，场次表没写进去：" + error
    return out


def after_summary(conn, sid: str) -> dict:
    """总结成功后调用。发布是投影，失败只记日志。"""
    return publish(conn, sid)


def publish_async(sid: str, *, force: bool = False) -> bool:
    """后台发布（仅采集下播建骨架用）。返回是否真的排了线程。"""
    cfg = config()
    if not cfg["enabled"]:
        logs.log_event("feishu.publish_skipped", session_id=sid,
                       msg="发布未开启（FEISHU_PUBLISH_ENABLED）")
        return False
    with _LOCK:
        if sid in _INFLIGHT:
            return False
        _INFLIGHT.add(sid)

    def run() -> None:
        conn = store.connect()
        try:
            publish(conn, sid, force=force)
        except Exception:
            traceback.print_exc()
        finally:
            conn.close()
            with _LOCK:
                _INFLIGHT.discard(sid)

    threading.Thread(target=run, name="feishu-publish-" + sid, daemon=True).start()
    return True
