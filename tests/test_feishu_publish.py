# -*- coding: utf-8 -*-
"""飞书发布器：一篇文档 + 一行场次表 + 一张收尾卡。

重点不是"能发出去"，而是重复发不会多出第二篇/第二行、失败能只补失败的那一步、
以及没有总结时绝不在文档里编一段摘要。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app import export, feishu, feishu_drive, feishu_publish, live, server, store
from helpers import add_rows, add_session, utt_row

SID = "S_PUB"


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, data=None, code=0, msg="ok"):
        self.data = data
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self.code == 0


# ---------------------------------------------------------------- 假飞书

class FakeApps:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def create(self, req):
        self.calls.append(req)
        if self.fail:
            return Resp(code=400, msg="no bitable permission")
        return Resp(Obj(app=Obj(app_token="app_1",
                                url="https://x.feishu.cn/base/app_1")))


class FakeTables:
    def __init__(self):
        self.created = []
        self.listed = 0
        self.tables: dict[str, dict] = {}
        self.fail = False
        self.fail_names: set[str] = set()
        self._next = 0

    def list(self, req):
        self.listed += 1
        items = [Obj(table_id=tid, name=t["name"])
                 for tid, t in self.tables.items()]
        return Resp(Obj(items=items, has_more=False, page_token=""))

    def create(self, req):
        self.created.append(req)
        tbl = getattr(getattr(req, "request_body", None), "table", None)
        name = str(getattr(tbl, "name", "") or "")
        if self.fail or name in self.fail_names:
            return Resp(code=500, msg="create table failed")
        self._next += 1
        tid = "tbl_%d" % self._next
        fields = [str(getattr(f, "field_name", ""))
                  for f in (getattr(tbl, "fields", None) or [])]
        self.tables[tid] = {
            "name": name,
            "default_view_name": str(getattr(tbl, "default_view_name", "") or ""),
            "fields": fields,
        }
        return Resp(Obj(table_id=tid))


class FakeFields:
    def __init__(self, tables=None):
        self.created = []
        self._tables = tables
        self.fields_by_table: dict[str, list[str]] = {}
        self.list_fail = False
        self.create_fail = False

    def list(self, req):
        if self.list_fail:
            return Resp(code=500, msg="list fields failed")
        tid = str(getattr(req, "table_id", "") or "")
        if tid in self.fields_by_table:
            names = list(self.fields_by_table[tid])
        elif self._tables and tid in self._tables.tables:
            names = list(self._tables.tables[tid]["fields"])
        else:
            names = [f["field_name"] for f in feishu_publish.FIELD_DEFS]
        items = [Obj(field_name=n) for n in names]
        return Resp(Obj(items=items, has_more=False, page_token=""))

    def create(self, req):
        self.created.append(req)
        if self.create_fail:
            return Resp(code=500, msg="create field failed")
        tid = str(getattr(req, "table_id", "") or "")
        fname = str(getattr(getattr(req, "request_body", None), "field_name", "") or "")
        if tid and fname:
            cur = self.fields_by_table.setdefault(tid, [])
            if fname not in cur:
                cur.append(fname)
        return Resp(Obj())


class FakeRecords:
    def __init__(self):
        self.rows = {}
        self.by_table: dict[str, dict[str, dict]] = {}
        self.created = []
        self.updated = []
        self.batch_created = []
        self.batch_deleted = []
        self.searches = []
        self.ops: list[str] = []
        self.search_page_size: int | None = None
        self.search_fail = False
        self.batch_delete_fail = False
        self.batch_create_fail = False
        self.reject_fields: set[str] = set()
        self._next = 0

    def _table_rows(self, table_id: str) -> dict[str, dict]:
        tid = str(table_id or "")
        if tid:
            return self.by_table.setdefault(tid, {})
        return self.rows

    def search(self, req):
        self.searches.append(req)
        self.ops.append("search")
        if self.search_fail:
            return Resp(code=500, msg="search failed")
        cond = req.request_body.filter.conditions[0]
        want = str((cond.value or [""])[0])
        tid = str(getattr(req, "table_id", "") or "")
        store_rows = self.by_table.get(tid, {}) if tid else self.rows
        matched = [Obj(record_id=rid, fields=f)
                   for rid, f in store_rows.items()
                   if str(f.get("session_id") or "") == want]
        page_size = int(getattr(req, "page_size", 0) or 100)
        if self.search_page_size is not None:
            page_size = min(page_size, self.search_page_size)
        offset = int(str(getattr(req, "page_token", "") or "0") or 0)
        chunk = matched[offset:offset + page_size]
        next_off = offset + len(chunk)
        has_more = next_off < len(matched)
        next_token = str(next_off) if has_more else ""
        return Resp(Obj(items=chunk, has_more=has_more, page_token=next_token))

    def create(self, req):
        if self.reject_fields & set(dict(req.request_body.fields)):
            return Resp(code=400, msg="attachment field not supported")
        self._next += 1
        rid = "rec_%d" % self._next
        fields = dict(req.request_body.fields)
        self.rows[rid] = fields
        tid = str(getattr(req, "table_id", "") or "")
        if tid:
            self._table_rows(tid)[rid] = fields
        self.created.append(req)
        self.ops.append("create")
        return Resp(Obj(record=Obj(record_id=rid,
                                   record_url="https://x.feishu.cn/base/app_1?table=%s&record=%s" % (tid or "tbl_1", rid))))

    def update(self, req):
        if self.reject_fields & set(dict(req.request_body.fields)):
            return Resp(code=400, msg="attachment field not supported")
        rid = str(req.record_id)
        fields = dict(req.request_body.fields)
        self.rows[rid] = fields
        tid = str(getattr(req, "table_id", "") or "")
        if tid:
            self._table_rows(tid)[rid] = fields
        self.updated.append(req)
        self.ops.append("update")
        return Resp(Obj(record=Obj(record_id=rid, record_url="")))

    def batch_create(self, req):
        self.batch_created.append(req)
        self.ops.append("batch_create")
        if self.batch_create_fail:
            return Resp(code=500, msg="batch create failed")
        tid = str(getattr(req, "table_id", "") or "")
        tbl = self._table_rows(tid)
        out = []
        for rec in (getattr(req.request_body, "records", None) or []):
            self._next += 1
            rid = "rec_%d" % self._next
            tbl[rid] = dict(rec.fields)
            out.append(Obj(record_id=rid, fields=dict(rec.fields)))
        return Resp(Obj(records=out))

    def batch_delete(self, req):
        self.batch_deleted.append(req)
        self.ops.append("batch_delete")
        if self.batch_delete_fail:
            return Resp(code=500, msg="batch delete failed")
        tid = str(getattr(req, "table_id", "") or "")
        tbl = self._table_rows(tid)
        for rid in (getattr(req.request_body, "records", None) or []):
            tbl.pop(str(rid), None)
        return Resp(Obj(records=[]))


class FakeBitable:
    def __init__(self, app_fail=False):
        self.app = FakeApps(fail=app_fail)
        self.app_table = FakeTables()
        self.app_table_field = FakeFields(tables=self.app_table)
        self.app_table_record = FakeRecords()


class FakeDriveFiles:
    def __init__(self):
        self.all_calls = []
        self.delete_calls = []
        self.create_folder_calls = []
        self.list_calls = []
        self.move_calls = []

    def upload_all(self, req):
        self.all_calls.append(req)
        return Resp(Obj(file_token="ft_pub", url="https://x.feishu.cn/file/ft_pub"))

    def delete(self, req):
        self.delete_calls.append(req)
        return Resp(Obj())

    def create_folder(self, req):
        self.create_folder_calls.append(req)
        return Resp(Obj(token="fld_session",
                        url="https://x.feishu.cn/drive/folder/fld_session"))

    def list(self, req):
        self.list_calls.append(req)
        return Resp(Obj(files=[], has_more=False, next_page_token=""))

    def move(self, req):
        self.move_calls.append(req)
        return Resp(Obj())


class FakeDriveMedia:
    def __init__(self):
        self.all_calls = []
        self.fail = False

    def upload_all(self, req):
        self.all_calls.append(req)
        if self.fail:
            return Resp(code=400, msg="media upload denied")
        return Resp(Obj(file_token="media_%d" % len(self.all_calls)))


class FakeDriveMembers:
    def __init__(self):
        self.calls = []

    def create(self, req):
        self.calls.append(req)
        return Resp(Obj())


class FakeClient:
    def __init__(self, app_fail=False):
        self.bitable = Obj(v1=FakeBitable(app_fail=app_fail))
        self.media = FakeDriveMedia()
        self.drive = Obj(v1=Obj(file=FakeDriveFiles(),
                                media=self.media,
                                permission_member=FakeDriveMembers()))


class FakeBot:
    """只实现发布器用到的四个入口，其余走不到。"""

    def __init__(self, doc_fail=False, app_fail=False):
        self.cfg = {
            "publish_enabled": True, "app_id": "cli_x", "app_secret": "s",
            "base_enabled": True, "base_app_token": "", "base_table_id": "",
            "base_folder_token": "", "base_url_base": "",
            "doc_folder_token": "fld_doc", "publish_folder_token": "fld_pub",
            "audio_folder_token": "fld_audio", "drive_url_base": "",
            "archive_on_publish": True, "archive_transcript": True,
            "archive_chat_id": "", "archive_chat_perm": "view",
            "base_attachments": True,
        }
        self.owner_open_id = "ou_owner"
        self.client = FakeClient(app_fail=app_fail)
        self.doc_fail = doc_fail
        self.calls: list[tuple] = []
        self.grants: list[tuple] = []
        self.cards: list[dict] = []
        self.doc_markdown = ""

    def create_docx(self, title, markdown, share_with="", folder_token=None):
        self.calls.append(("create", title, markdown, folder_token))
        self.doc_markdown = markdown
        if self.doc_fail:
            return {"ok": False, "error": "写入正文失败：code=400 bad",
                    "document_id": "doc_1", "url": "https://feishu.cn/docx/doc_1"}
        return {"ok": True, "document_id": "doc_1",
                "url": "https://feishu.cn/docx/doc_1", "blocks": 3,
                "shared": bool(share_with)}

    def overwrite_docx(self, document_id, title, markdown, share_with=""):
        self.calls.append(("overwrite", document_id, title, markdown))
        self.doc_markdown = markdown
        if self.doc_fail:
            return {"ok": False, "error": "写入正文失败：code=400 bad",
                    "document_id": str(document_id),
                    "url": "https://feishu.cn/docx/" + str(document_id)}
        return {"ok": True, "document_id": str(document_id),
                "url": "https://feishu.cn/docx/" + str(document_id),
                "blocks": 3, "shared": bool(share_with)}

    def _grant_doc(self, document_id, open_id, perm="full_access",
                   doc_type="docx"):
        self.grants.append((document_id, open_id, doc_type))
        return {"ok": True}

    def send_card(self, card, open_id=""):
        self.cards.append(card)
        return True


def _ready_session(conn, sid=SID):
    add_session(conn, sid, title="周三夜聊", started_at="2026-11-25T21:00:00",
                duration_s=3600)
    add_rows(conn, sid, [
        utt_row(0, "明年 OPC 会被平台收紧", speaker="SPK_00", label="主播"),
        utt_row(1, "我觉得不会，得看备案", speaker="SPK_01", label="说话人2"),
        utt_row(2, "选品还是看复购", speaker="SPK_00", label="主播"),
    ])
    names = [("SPK_00", "阿哲"), ("SPK_01", "小北")]
    for spk, name in names:
        store.upsert_speaker(conn, sid, spk, name)
    store.save_summary(conn, sid, "content", {}, "fake", {
        "session_title": "周三夜聊", "overview": "大家在争 OPC 政策会不会收紧。",
        "topics": ["OPC 政策"],
        "points": [{
            "title": "OPC 政策", "gist": "平台会收紧", "t_from": 0, "t_to": 2,
            "speakers": ["阿哲"], "claim_speaker": "阿哲",
            "stance": "明年 OPC 会被平台收紧",
            "evidence": [{"id": 1, "t_start": 0, "speaker_label": "阿哲",
                          "quote": "明年 OPC 会被平台收紧"}],
        }],
    })
    return sid


# ---------------------------------------------------------------- 发布 / 幂等

def test_publish_creates_one_doc_and_one_row(db):
    sid = _ready_session(db)
    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    assert out["doc_url"].endswith("/doc_1")
    assert [c[0] for c in bot.calls] == ["create"]
    assert bot.calls[0][3] == "fld_pub"        # 发布文档进「场次文档」文件夹
    assert len(bot.client.bitable.v1.app_table_record.created) == 1
    row = store.get_publish(db, sid)
    assert row["state"] == "ok" and row["doc_token"] == "doc_1"
    assert row["record_id"] == "rec_1" and row["version"]
    assert row["meta"]["doc_state"] == "ready"
    # 发布内容只来自总结，不现场编
    assert "## 一句话" in bot.doc_markdown
    assert "明年 OPC 会被平台收紧" in bot.doc_markdown

    again = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert again["ok"] and again["unchanged"] is True
    assert [c[0] for c in bot.calls] == ["create"]       # 没有第二次写正文
    assert len(bot.client.bitable.v1.app_table_record.created) == 1
    assert not bot.client.bitable.v1.app_table_record.updated


def test_content_change_overwrites_same_doc_and_updates_same_row(db):
    sid = _ready_session(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    store.save_summary(db, sid, "content", {}, "fake", {
        "session_title": "周三夜聊", "overview": "争完了，结论是看备案。",
        "topics": ["OPC 政策"],
        "points": [{
            "title": "OPC 政策", "gist": "看备案", "t_from": 0, "t_to": 2,
            "speakers": ["阿哲"], "claim_speaker": "阿哲",
            "stance": "其实是备案门槛被抬高了",
            "evidence": [{"id": 1, "t_start": 0, "speaker_label": "阿哲",
                          "quote": "其实是备案门槛被抬高了"}],
        }],
    })
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and not out.get("unchanged")
    assert [c[0] for c in bot.calls] == ["create", "overwrite"]
    assert bot.calls[1][1] == "doc_1"
    assert len(bot.client.bitable.v1.app_table_record.created) == 1
    assert len(bot.client.bitable.v1.app_table_record.updated) == 1
    assert store.get_publish(db, sid)["doc_token"] == "doc_1"


def test_force_rewrites_even_when_version_unchanged(db):
    sid = _ready_session(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    out = feishu_publish.publish(sid, bot=bot, conn=db, force=True, notify=False)
    assert out["ok"]
    assert [c[0] for c in bot.calls] == ["create", "overwrite"]


# ---------------------------------------------------------------- 没有总结

def test_capture_only_publishes_skeleton_without_summary(db):
    add_session(db, "S_CAP", title="只录了音", started_at="2026-11-25T21:00:00",
                duration_s=600, capture_only=True)
    bot = FakeBot()
    out = feishu_publish.publish("S_CAP", bot=bot, conn=db, notify=False)
    assert out["ok"] and out["doc_state"] == "pending_transcript"
    assert "待补转写" in bot.doc_markdown
    assert "## 一句话" not in bot.doc_markdown
    assert "观点地图" not in bot.doc_markdown
    assert "规则版兜底稿" in bot.doc_markdown
    fields = list(bot.client.bitable.v1.app_table_record.rows.values())[0]
    assert fields["状态"] == "待转写"


def test_failed_summary_publishes_facts_not_fabricated_summary(db):
    add_session(db, "S_FAIL", title="总结挂了", started_at="2026-11-25T21:00:00",
                duration_s=600)
    add_rows(db, "S_FAIL", [utt_row(0, "一句逐字稿")])
    store.upsert_note(db, "S_FAIL", state="failed", error="模型超时")
    bot = FakeBot()
    out = feishu_publish.publish("S_FAIL", bot=bot, conn=db, notify=False)
    assert out["ok"] and out["doc_state"] == "failed_summary"
    assert "总结失败" in bot.doc_markdown
    assert "不写规则版兜底稿" in bot.doc_markdown
    assert "## 一句话" not in bot.doc_markdown


# ---------------------------------------------------------------- 失败与降级

def test_doc_failure_keeps_document_id_and_retry_reuses_it(db):
    sid = _ready_session(db)
    bot = FakeBot(doc_fail=True)
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert not out["ok"] and out["state"] == "failed"
    assert store.get_publish(db, sid)["doc_token"] == "doc_1"

    bot.doc_fail = False
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] and out2["state"] == "ok"
    assert [c[0] for c in bot.calls] == ["create", "overwrite"]
    assert bot.calls[1][1] == "doc_1"          # 没有建第二篇
    assert store.get_publish(db, sid)["doc_token"] == "doc_1"


def test_base_failure_is_partial_and_retry_fills_row_without_rewriting_doc(db):
    sid = _ready_session(db)
    bot = FakeBot(app_fail=True)
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "partial"
    assert out["doc_url"] and "场次表" in out["error"]
    assert store.get_publish(db, sid)["state"] == "partial"

    bot2 = FakeBot()
    out2 = feishu_publish.publish(sid, bot=bot2, conn=db, notify=False)
    assert out2["ok"] and out2["state"] == "ok"
    assert not bot2.calls                      # 正文没重写
    assert len(bot2.client.bitable.v1.app_table_record.created) == 1


def test_disabled_publisher_skips_silently(db):
    sid = _ready_session(db)
    bot = FakeBot()
    bot.cfg["publish_enabled"] = False
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out.get("skipped") is True
    assert not bot.calls
    assert store.get_publish(db, sid) is None


def test_filtered_summary_is_not_published(db):
    sid = _ready_session(db)
    # 最新一条是筛选口径：不能拿筛过的要点冒充整场结论。
    store.save_summary(db, sid, "content", {"q": "OPC"}, "fake", {
        "overview": "只筛了 OPC", "points": []})
    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] is False and out["skipped"] is True
    assert out["reason"] == "filtered_summary"
    assert not bot.calls


def test_publish_never_raises_and_records_failure(db, monkeypatch):
    sid = _ready_session(db)
    bot = FakeBot()

    def boom(conn, sid):
        raise RuntimeError("组装炸了")

    monkeypatch.setattr(feishu_publish.export, "build_publish", boom)
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] is False and out["state"] == "failed"
    assert "组装炸了" in out["error"]
    assert store.get_publish(db, sid)["state"] == "failed"


def test_wrapup_card_links_doc_and_offers_name_fix(db):
    sid = _ready_session(db)
    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=True)
    assert len(bot.cards) == 1
    acts = [b.get("value", {}).get("act") for e in bot.cards[0]["elements"]
            if e.get("tag") == "action" for b in e["actions"]]
    btn_texts = [b.get("text", {}).get("content") for e in bot.cards[0]["elements"]
                 if e.get("tag") == "action" for b in e["actions"]]
    assert "看文档" in btn_texts and "场次表" in btn_texts
    assert "publish_name" in acts
    assert "publish_retry" not in acts          # 成功时不显示重试
    text = feishu_publish.wrapup_text(out, {})
    assert "已发布" in text and "doc_1" in text


# ---------------------------------------------------------------- 覆盖写细节

class FakeConvert:
    def __init__(self, ok=True, blocks=None, first=None):
        self.ok = ok
        self.calls = []
        self._blocks = blocks
        self._first = first

    def __call__(self, req):
        self.calls.append(req)
        if not self.ok:
            return Resp(code=400, msg="convert failed")
        if self._blocks is not None:
            return Resp(Obj(blocks=self._blocks,
                            first_level_block_ids=list(self._first or [])))
        return Resp(Obj(blocks=[Obj(block_id="b1")],
                        first_level_block_ids=["b1"]))


class FakeChildren:
    """方法名必须和 lark-oapi 1.9.0 一致：读是 ``get``，不是 ``list``。

    之前这里写成 ``list``，测试全绿但线上 ``overwrite_docx`` 直接抛
    AttributeError——假对象凭空多出的方法把 bug 挡住了。
    """

    def __init__(self, count=3, fail=False):
        self.count = count
        self.fail = fail
        self.listed = []
        self.deleted = []

    def get(self, req):
        self.listed.append(req)
        if self.fail:
            return Resp(code=500, msg="read failed")
        return Resp(Obj(items=[Obj() for _ in range(self.count)],
                        has_more=False, page_token=""))

    def batch_delete(self, req):
        self.deleted.append(req)
        return Resp(Obj())


class FakeDescendants:
    def __init__(self):
        self.calls = []

    def create(self, req):
        self.calls.append(req)
        return Resp(Obj())


class FakeBlocks:
    def __init__(self):
        self.patches = []

    def patch(self, req):
        self.patches.append(req)
        return Resp(Obj())


class FakeDocx:
    def __init__(self, count=3, children_fail=False):
        self.convert = FakeConvert()
        self.children = FakeChildren(count=count, fail=children_fail)
        self.descendants = FakeDescendants()
        self.blocks = FakeBlocks()
        self.document = Obj(convert=self.convert)


def _real_bot(docx):
    bot = feishu.Bot.__new__(feishu.Bot)
    bot.cfg = {"doc_url_base": "https://x.feishu.cn/docx"}
    bot.client = Obj(docx=Obj(v1=Obj(
        document=docx.document,
        document_block_children=docx.children,
        document_block_descendant=docx.descendants,
        document_block=docx.blocks)))
    bot.last_error = ""
    return bot


def test_overwrite_docx_clears_root_children_then_writes():
    docx = FakeDocx(count=4)
    bot = _real_bot(docx)
    out = bot.overwrite_docx("doc_1", "新标题", "# 正文")
    assert out["ok"] and out["cleared"] == 4
    req = docx.children.deleted[0]
    assert req.request_body.start_index == 0
    assert req.request_body.end_index == 4
    assert docx.descendants.calls[0].request_body.index == 0
    assert docx.blocks.patches                     # 顺手改标题


def test_overwrite_docx_does_not_clear_when_children_unreadable():
    docx = FakeDocx(children_fail=True)
    bot = _real_bot(docx)
    out = bot.overwrite_docx("doc_1", "标题", "# 正文")
    assert not out["ok"] and out["document_id"] == "doc_1"
    assert not docx.children.deleted
    assert not docx.descendants.calls


def test_md_blocks_strips_table_merge_info():
    """表格的 merge_info 原样回传会让飞书报 1770001，写正文整篇失败。

    2026-11-26 实测：convert 返回的表格块带着 merge_info，原样传给
    block_descendant.create 稳定失败；清掉它就正常。这条测试盯住这个坑。
    """
    table = Obj(block_id="t1", block_type=31,
                table=Obj(property=Obj(
                    row_size=2, column_size=2,
                    merge_info=[Obj(row_span=1, col_span=1)],
                    column_width=[100, 100])),
                children=["c1"])
    docx = FakeDocx()
    docx.convert = FakeConvert(blocks=[table, Obj(block_id="p1", block_type=2)],
                               first=["t1", "p1"])
    docx.document = Obj(convert=docx.convert)   # FakeDocx 构造时抓的是旧对象
    bot = _real_bot(docx)
    blocks, first, err = bot._md_blocks("| A | B |\n|---|---|\n| 1 | 2 |\n")
    assert err is None and first == ["t1", "p1"]
    assert blocks[0].table.property.merge_info is None      # 被清掉
    assert blocks[0].table.property.column_width == [100, 100]  # 其余不动


# ---------------------------------------------------------------- HTTP 路由

@pytest.fixture
def http_base(db):
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_naming_route_republishes_only_when_already_published(http_base, db,
                                                              monkeypatch):
    from app import naming
    add_session(db, "S_NAME")
    store.upsert_speaker(db, "S_NAME", "SPK_00", "说话人1")
    monkeypatch.setattr(naming, "name_speaker", lambda *a, **k: {
        "ok": True, "name": "阿哲", "enrolled": False})
    refreshed = []
    monkeypatch.setattr(feishu_publish, "has_published",
                        lambda conn, sid: True)
    monkeypatch.setattr(feishu_publish, "publish_session_async",
                        lambda sid, **k: refreshed.append((sid, k)))

    status, out = _post(http_base, "/api/speakers/name", {
        "session_id": "S_NAME", "speaker_id": "SPK_00", "name": "阿哲"})
    assert status == 200 and out["ok"]
    assert refreshed == [("S_NAME", {"only_if_published": True})]

    refreshed.clear()
    monkeypatch.setattr(feishu_publish, "has_published",
                        lambda conn, sid: False)
    status, _ = _post(http_base, "/api/speakers/name", {
        "session_id": "S_NAME", "speaker_id": "SPK_00", "name": "阿哲"})
    assert status == 200 and not refreshed


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_publish_routes(http_base, db, monkeypatch):
    add_session(db, "S_HTTP")
    monkeypatch.setattr(feishu_publish, "publish", lambda sid, **kw: {
        "ok": True, "session_id": sid, "state": "ok",
        "doc_url": "https://x/doc"})
    status, out = _post(http_base, "/api/publish", {"session_id": "S_HTTP"})
    assert status == 200 and out["doc_url"] == "https://x/doc"

    monkeypatch.setattr(feishu_publish, "status", lambda conn, sid: {
        "ok": True, "session_id": sid, "published": True,
        "state": "ok", "doc_url": "https://x/doc"})
    status, out = _get(http_base, "/api/publish?session_id=S_HTTP")
    assert status == 200 and out["published"] is True

    status, out = _post(http_base, "/api/publish", {"session_id": "nope"})
    assert status == 404


# ---------------------------------------------------------------- 自动发布接线

def test_capture_only_stop_requests_skeleton_publish(monkeypatch):
    from app import live
    sess = live.LiveSession.__new__(live.LiveSession)
    sess.cancelled = False
    sess.id = "S_CAP_STOP"
    sess.capture_only = True
    seen: list = []
    monkeypatch.setattr(feishu_publish, "publish_async",
                        lambda sid, **kw: seen.append(sid) or True)
    sess._auto_finalize()
    assert seen == ["S_CAP_STOP"]


def test_publish_async_is_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(feishu_publish, "enabled", lambda *a, **k: False)
    assert feishu_publish.publish_async("S_OFF") is False


def test_auto_finalize_triggers_async_publish_only_when_auto(db, monkeypatch):
    from app import analysis, insights, notes, notify, tasks
    add_session(db, "S_AUTO")
    monkeypatch.setattr(analysis, "make_summary",
                        lambda *a, **k: {"engine": "fake"})
    monkeypatch.setattr(notes, "index_session", lambda *a, **k: None)
    monkeypatch.setattr(insights, "after_finalize", lambda *a, **k: None)
    monkeypatch.setattr(notify, "note_ready", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(feishu_publish, "publish_session_async",
                        lambda sid, **k: seen.append((sid, k)))

    tasks._run_summary("S_AUTO", {"auto": True, "mode": "content",
                                  "engine": "auto"})
    assert seen == [("S_AUTO", {})]

    seen.clear()
    tasks._run_summary("S_AUTO", {"auto": False, "mode": "content",
                                  "engine": "auto"})
    assert not seen


# ---------------------------------------------------------------- 机器人命令

def _fake_rooms():
    return [{"id": "r1", "seq": 1, "name": "测试房", "state": "stopped",
             "session_id": "S1"}]


def test_cmd_name_maps_numbers_to_speaker_ids(monkeypatch):
    from tools import feishu_bot as fb
    monkeypatch.setattr(fb, "_rooms", _fake_rooms)
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: {
        "ok": True, "speakers": [
            {"speaker_id": "SPK_00", "named": False, "name": "",
             "suggestion": "阿哲", "preview": "第一句"},
            {"speaker_id": "SPK_01", "named": False, "name": "",
             "suggestion": "小北", "preview": "第二句"},
        ]})
    calls = []
    monkeypatch.setattr(fb, "_api", lambda path, payload=None, timeout=6.0:
                        calls.append((path, payload)) or {"ok": True})
    texts = []
    monkeypatch.setattr(fb, "_send_text_target",
                        lambda text, *a, **k: texts.append(text) or True)
    assert fb.cmd_name("CH01 1 阿哲 2 小北", "ou_1") == ""
    assert [(p, b["speaker_id"], b["name"]) for p, b in calls] == [
        ("/api/speakers/name", "SPK_00", "阿哲"),
        ("/api/speakers/name", "SPK_01", "小北")]
    assert any("已补名" in t for t in texts)


def test_name_pairs_accepts_common_forms_and_keeps_digit_names():
    from tools import feishu_bot as fb
    assert fb._name_pairs_explicit("1 阿哲 2 小北") == [(1, "阿哲"), (2, "小北")]
    assert fb._name_pairs_explicit("1=阿哲，2：小北") == [(1, "阿哲"), (2, "小北")]
    assert fb._name_pairs_explicit("1 阿哲3号") == [(1, "阿哲3号")]
    assert fb._name_pairs_explicit("1 阿哲 3号") == [(1, "阿哲")]


def test_cmd_name_lists_people_and_rejects_bad_index(monkeypatch):
    from tools import feishu_bot as fb
    monkeypatch.setattr(fb, "_rooms", _fake_rooms)
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: {
        "ok": True, "title": "测试房", "speakers": [
            {"speaker_id": "SPK_00", "named": False, "name": "",
             "letter": "A", "index": 1, "seconds": 30, "n_utterances": 3,
             "suggestion": "阿哲", "preview": {"t_start": 1, "seconds": 3}}]})
    texts = []
    monkeypatch.setattr(fb, "_send_text_target",
                        lambda text, *a, **k: texts.append(text) or True)
    monkeypatch.setattr(fb, "_send_card_target", lambda *a, **k: True)
    monkeypatch.setattr(fb.clips, "make_clip", lambda *a, **k: (None, "没音频"))
    assert fb.cmd_name("CH01", "ou_1") == ""
    joined = "\n".join(texts)
    assert "还有 1 位没名字" in joined and "1 阿哲" in joined

    calls = []
    monkeypatch.setattr(fb, "_api", lambda path, payload=None, timeout=6.0:
                        calls.append(path) or {"ok": True})
    msg = fb.cmd_name("CH01 9 阿哲", "ou_1")
    assert "超出范围" in msg and not calls


def test_route_publish_and_doc(monkeypatch):
    from tools import feishu_bot as fb
    monkeypatch.setattr(fb, "cmd_publish", lambda rest, *a, **k: "pub:" + rest)
    monkeypatch.setattr(fb, "cmd_doc", lambda rest, *a, **k: "doc:" + rest)
    assert fb._route("/发布 CH01", "/发布 ch01") == "pub:CH01"
    assert fb._route("/文档 CH02", "/文档 ch02") == "doc:CH02"


def test_handle_card_publish_retry_spawns_worker(monkeypatch):
    from tools import feishu_bot as fb
    seen = []
    monkeypatch.setattr(fb, "_publish_worker",
                        lambda sid, title, *a, **k: seen.append(sid))
    toast, card = fb.handle_card({"act": "publish_retry", "session_id": "S1"},
                                 "ou_1")
    assert toast and card is None
    for _ in range(50):
        if seen:
            break
        threading.Event().wait(0.01)
    assert seen == ["S1"]


# ---------------------------------------------------------------- 发布前归档

def _fake_archive(url="https://x.feishu.cn/file/ft_pub"):
    def fake(conn, session, bot=None):
        store.set_audio_meta(conn, session["id"], drive_token="ft_pub",
                             drive_url=url, drive_uploaded_at=store.now())
        return {"ok": True, "skipped": False, "file_token": "ft_pub",
                "url": url, "replaced": False, "error": ""}
    return fake


def test_publish_archives_before_build(db, monkeypatch):
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "ensure_archived", _fake_archive())
    bot = FakeBot()

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok"
    assert "## 录音" in bot.doc_markdown
    assert "https://x.feishu.cn/file/ft_pub" in bot.doc_markdown
    row = store.get_publish(db, sid)
    assert row["meta"]["archive_skipped"] is False
    assert row["meta"]["archive_error"] == ""


def test_publish_archive_failure_keeps_state_ok(db, monkeypatch):
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "ensure_archived",
                        lambda conn, session, bot=None: {
                            "ok": False, "skipped": False, "error": "drive boom"})
    bot = FakeBot()

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok" and out["error"] == ""
    assert "## 录音" not in bot.doc_markdown
    row = store.get_publish(db, sid)
    assert row["meta"]["archive_skipped"] is False
    assert row["meta"]["archive_error"] == "drive boom"


def test_publish_archive_disabled_by_config(db, monkeypatch):
    sid = _ready_session(db)
    bot = FakeBot()
    bot.cfg["archive_on_publish"] = False
    calls = []
    monkeypatch.setattr(feishu_drive, "ensure_archived",
                        lambda *a, **k: calls.append(1) or {"ok": True})

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and not calls
    row = store.get_publish(db, sid)
    assert row["meta"]["archive_skipped"] is True
    assert row["meta"]["archive_error"] == ""


def test_publish_ready_doc_has_audio_section(db):
    sid = _ready_session(db)
    store.set_audio_meta(db, sid, drive_url="https://x.feishu.cn/file/ft_ready")

    doc = export.build_publish(db, sid)

    assert doc["ok"] and doc["state"] == "ready"
    assert "## 录音" in doc["markdown"]
    assert "https://x.feishu.cn/file/ft_ready" in doc["markdown"]


# ---------------------------------------------------------------- 附件挂载

def test_publish_writes_attachment_fields_and_transcript_link(
        db, isolated_paths, monkeypatch):
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", isolated_paths / "audio")
    (isolated_paths / "audio" / (sid + ".m4a")).write_bytes(b"x" * 8)
    bot = FakeBot()

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok"
    fields = list(bot.client.bitable.v1.app_table_record.rows.values())[0]
    assert fields["逐字稿链接"]["link"].startswith("https://x.feishu.cn/file/")
    assert fields["逐字稿"] == [{"file_token": "media_2"}]
    assert fields["录音"] == [{"file_token": "media_1"}]
    row = store.get_publish(db, sid)
    assert row["meta"]["base_attach_error"] == ""
    assert row["meta"]["folder_url"].endswith("fld_session")
    session = store.get_session(db, sid)
    assert session["transcript_token"] == "ft_pub"
    assert session["audio_media_token"] == "media_1"
    assert session["transcript_media_token"] == "media_2"


def test_publish_attachment_failure_keeps_state_ok(db, isolated_paths,
                                                   monkeypatch):
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", isolated_paths / "audio")
    (isolated_paths / "audio" / (sid + ".m4a")).write_bytes(b"x" * 8)
    bot = FakeBot()
    bot.client.media.fail = True

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok"
    assert out["attach_error"]
    fields = list(bot.client.bitable.v1.app_table_record.rows.values())[0]
    assert "录音" not in fields and "逐字稿" not in fields
    assert fields["逐字稿链接"]          # 链接字段仍可靠
    row = store.get_publish(db, sid)
    assert row["meta"]["base_attach_error"]


def test_publish_rejected_attachment_field_falls_back_to_links(
        db, isolated_paths, monkeypatch):
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", isolated_paths / "audio")
    (isolated_paths / "audio" / (sid + ".m4a")).write_bytes(b"x" * 8)
    bot = FakeBot()
    bot.client.bitable.v1.app_table_record.reject_fields = {"录音", "逐字稿"}

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok"
    assert out["attach_error"]
    fields = list(bot.client.bitable.v1.app_table_record.rows.values())[0]
    assert "录音" not in fields and "逐字稿" not in fields
    assert fields["逐字稿链接"] and fields["录音链接"]


def test_publish_without_attachments_still_writes_transcript_link(
        db, isolated_paths, monkeypatch):
    """FEISHU_BASE_ATTACHMENTS=0：不传附件，但「逐字稿链接」要照写。"""
    sid = _ready_session(db)
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", isolated_paths / "audio")
    (isolated_paths / "audio" / (sid + ".m4a")).write_bytes(b"x" * 8)
    bot = FakeBot()
    bot.cfg["base_attachments"] = False

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)

    assert out["ok"] and out["state"] == "ok"
    fields = list(bot.client.bitable.v1.app_table_record.rows.values())[0]
    assert "录音" not in fields and "逐字稿" not in fields
    assert fields["逐字稿链接"]["link"].startswith("https://x.feishu.cn/file/")
    session = store.get_session(db, sid)
    assert session["transcript_token"] == "ft_pub"
    assert not session["audio_media_token"]


def test_publish_switch_defaults_on_when_credentials_present(isolated_paths):
    feishu.CFG_PATH.write_text(json.dumps({
        "app_id": "cli_x", "app_secret": "s"}), encoding="utf-8")
    assert feishu.load_config()["publish_enabled"] is True

    feishu.CFG_PATH.write_text("{}", encoding="utf-8")
    assert feishu.load_config()["publish_enabled"] is False

    feishu.CFG_PATH.write_text(json.dumps({
        "app_id": "cli_x", "app_secret": "s", "publish_enabled": False}),
        encoding="utf-8")
    assert feishu.load_config()["publish_enabled"] is False
