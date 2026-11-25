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

from app import feishu, feishu_publish, live, server, store
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

    def list(self, req):
        self.listed += 1
        return Resp(Obj(items=[], has_more=False, page_token=""))

    def create(self, req):
        self.created.append(req)
        return Resp(Obj(table_id="tbl_1"))


class FakeFields:
    def __init__(self):
        self.created = []

    def list(self, req):
        items = [Obj(field_name=f["field_name"])
                 for f in feishu_publish.FIELD_DEFS]
        return Resp(Obj(items=items, has_more=False, page_token=""))

    def create(self, req):
        self.created.append(req)
        return Resp(Obj())


class FakeRecords:
    def __init__(self):
        self.rows = {}
        self.created = []
        self.updated = []
        self._next = 0

    def search(self, req):
        cond = req.request_body.filter.conditions[0]
        want = str((cond.value or [""])[0])
        items = [Obj(record_id=rid, fields=f)
                 for rid, f in self.rows.items()
                 if str(f.get("session_id") or "") == want]
        return Resp(Obj(items=items, has_more=False, page_token=""))

    def create(self, req):
        self._next += 1
        rid = "rec_%d" % self._next
        self.rows[rid] = dict(req.request_body.fields)
        self.created.append(req)
        return Resp(Obj(record=Obj(record_id=rid,
                                   record_url="https://x.feishu.cn/base/app_1?table=tbl_1&record=%s" % rid)))

    def update(self, req):
        self.rows[str(req.record_id)] = dict(req.request_body.fields)
        self.updated.append(req)
        return Resp(Obj(record=Obj(record_id=str(req.record_id), record_url="")))


class FakeBitable:
    def __init__(self, app_fail=False):
        self.app = FakeApps(fail=app_fail)
        self.app_table = FakeTables()
        self.app_table_field = FakeFields()
        self.app_table_record = FakeRecords()


class FakeClient:
    def __init__(self, app_fail=False):
        self.bitable = Obj(v1=FakeBitable(app_fail=app_fail))


class FakeBot:
    """只实现发布器用到的四个入口，其余走不到。"""

    def __init__(self, doc_fail=False, app_fail=False):
        self.cfg = {
            "publish_enabled": True, "app_id": "cli_x", "app_secret": "s",
            "base_enabled": True, "base_app_token": "", "base_table_id": "",
            "base_folder_token": "", "base_url_base": "",
            "doc_folder_token": "fld_doc", "publish_folder_token": "fld_pub",
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


def test_wrapup_card_links_doc_and_offers_name_fix(db):
    sid = _ready_session(db)
    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=True)
    assert len(bot.cards) == 1
    acts = [b.get("value", {}).get("act") for e in bot.cards[0]["elements"]
            if e.get("tag") == "action" for b in e["actions"]]
    assert "publish_name" in acts
    assert "publish_retry" not in acts          # 成功时不显示重试
    text = feishu_publish.wrapup_text(out, {})
    assert "已发布" in text and "doc_1" in text


# ---------------------------------------------------------------- 覆盖写细节

class FakeConvert:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        if not self.ok:
            return Resp(code=400, msg="convert failed")
        return Resp(Obj(blocks=[Obj(block_id="b1")],
                        first_level_block_ids=["b1"]))


class FakeChildren:
    def __init__(self, count=3, fail=False):
        self.count = count
        self.fail = fail
        self.listed = []
        self.deleted = []

    def list(self, req):
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
    msg = fb.cmd_name("CH01 1 阿哲 2 小北")
    assert "已补名" in msg
    assert [(p, b["speaker_id"], b["name"]) for p, b in calls] == [
        ("/api/speakers/name", "SPK_00", "阿哲"),
        ("/api/speakers/name", "SPK_01", "小北")]


def test_name_pairs_accepts_common_forms_and_keeps_digit_names():
    from tools import feishu_bot as fb
    assert fb._name_pairs("1 阿哲 2 小北") == [(1, "阿哲"), (2, "小北")]
    assert fb._name_pairs("1=阿哲，2：小北") == [(1, "阿哲"), (2, "小北")]
    assert fb._name_pairs("1 阿哲3号") == [(1, "阿哲3号")]
    assert fb._name_pairs("1 阿哲 3号") == [(1, "阿哲")]


def test_cmd_name_lists_people_and_rejects_bad_index(monkeypatch):
    from tools import feishu_bot as fb
    monkeypatch.setattr(fb, "_rooms", _fake_rooms)
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: {
        "ok": True, "speakers": [
            {"speaker_id": "SPK_00", "named": False, "name": "",
             "suggestion": "阿哲", "preview": "第一句"}]})
    listing = fb.cmd_name("CH01")
    assert "未命名" in listing and "/补名" in listing

    calls = []
    monkeypatch.setattr(fb, "_api", lambda path, payload=None, timeout=6.0:
                        calls.append(path) or {"ok": True})
    msg = fb.cmd_name("CH01 9 阿哲")
    assert "不存在" in msg and not calls


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
