# -*- coding: utf-8 -*-
"""飞书发布：一篇文章 + 场次表一行，幂等、失败隔离、骨架稿。

用假 lark 客户端覆盖 docx/bitable 的真实调用形状（不联网、不碰租户），
只验证「同一场永远只有一篇文档、一行记录」和「失败不拖累录制/总结」。
"""
from __future__ import annotations

import pytest

from app import export, feishu, feishu_publish, store
from helpers import add_rows, add_session, utt_row


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


class FakeDocx:
    """记录 convert/create/descendant/children 四类调用。"""

    def __init__(self) -> None:
        self.converts: list[str] = []
        self.creates: list = []
        self.descendants: list = []
        self.deletes: list = []
        self.children: dict[str, list[str]] = {}
        self.fail_descendant = False
        self.fail_rewrite_delete = False
        self.document = Obj(convert=self._convert, create=self._create)
        self.descendant = Obj(create=self._descendant)
        self.block_children = Obj(get=self._get_children,
                                  batch_delete=self._delete_children)

    def _convert(self, req):
        self.converts.append(str(req.request_body.content))
        return Resp(Obj(blocks=[Obj(block_id="blk_new")],
                        first_level_block_ids=["blk_new"]))

    def _create(self, req):
        self.creates.append(req.request_body)
        doc_id = "doc_%d" % len(self.creates)
        self.children[doc_id] = ["old_1", "old_2"]   # 新文档自带空段落
        return Resp(Obj(document=Obj(document_id=doc_id)))

    def _descendant(self, req):
        if self.fail_descendant:
            self.fail_descendant = False
            return Resp(code=1770001, msg="block write denied")
        self.descendants.append((req.document_id, list(req.request_body.children_id)))
        self.children[req.document_id] = list(req.request_body.children_id)
        return Resp(Obj())

    def _get_children(self, req):
        return Resp(Obj(items=[Obj(block_id=x)
                               for x in self.children.get(req.document_id, [])],
                        has_more=False))

    def _delete_children(self, req):
        doc_id = req.document_id
        body = req.request_body
        self.deletes.append((doc_id, int(body.start_index), int(body.end_index)))
        if self.fail_rewrite_delete:
            self.fail_rewrite_delete = False
            return Resp(code=1770002, msg="index invalid")
        self.children[doc_id] = []
        return Resp(Obj())


class FakeMembers:
    def __init__(self) -> None:
        self.calls: list = []

    def create(self, req):
        self.calls.append(req)
        return Resp(Obj())


class FakeAppTable:
    def __init__(self) -> None:
        self.created: list = []

    def list(self, req):
        return Resp(Obj(items=[Obj(table_id="tbl_1", name="场次")],
                        has_more=False))

    def create(self, req):
        self.created.append(req.request_body)
        return Resp(Obj(table_id="tbl_2"))


class FakeRecords:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}
        self.created: list = []
        self.updated: list = []
        self.searches = 0
        self.fail_create = 0

    def search(self, req):
        self.searches += 1
        cond = req.request_body.filter.conditions[0]
        sid = list(cond.value)[0]
        items = [Obj(record_id=rid, fields=dict(f))
                 for rid, f in self.records.items() if f.get("session_id") == sid]
        return Resp(Obj(items=items))

    def create(self, req):
        if self.fail_create:
            self.fail_create -= 1
            return Resp(code=1254000, msg="field not found")
        rid = "rec_%d" % (len(self.records) + 1)
        self.records[rid] = dict(req.request_body.fields)
        self.created.append((rid, req.request_body.fields))
        return Resp(Obj(record=Obj(record_id=rid)))

    def update(self, req):
        rid = req.record_id
        self.records[rid] = dict(req.request_body.fields)
        self.updated.append((rid, req.request_body.fields))
        return Resp(Obj(record=Obj(record_id=rid)))


class FakeClient:
    def __init__(self) -> None:
        self.docx_v = FakeDocx()
        self.members = FakeMembers()
        self.tables = FakeAppTable()
        self.records = FakeRecords()
        self.docx = Obj(v1=Obj(document=self.docx_v.document,
                               document_block_descendant=self.docx_v.descendant,
                               document_block_children=self.docx_v.block_children))
        self.drive = Obj(v1=Obj(permission_member=self.members))
        self.bitable = Obj(v1=Obj(app_table=self.tables,
                                  app_table_record=self.records))


def _bot() -> feishu.Bot:
    b = feishu.Bot.__new__(feishu.Bot)     # 绕过 lark 初始化
    client = FakeClient()
    b.cfg = {"doc_folder_token": "", "doc_url_base": ""}
    b.client = client
    b.open_id = b.owner_open_id = "ou_owner"
    b.cards = []
    b.texts = []
    b.send_card = lambda card, open_id="": (b.cards.append((card, open_id)) or True)
    b.send_text = lambda text, open_id="": (b.texts.append(text) or True)
    return b


@pytest.fixture
def publish_cfg(monkeypatch):
    cfg = {"enabled": True, "app_id": "a", "app_secret": "s", "folder": "",
           "owner": "ou_owner", "doc_url_base": "",
           "base_app_token": "app_x", "base_table_id": "tbl_1",
           "base_url_base": ""}
    monkeypatch.setattr(feishu_publish, "config", lambda: dict(cfg))
    feishu_publish._TABLES.clear()
    return cfg


def _seed(conn, sid: str = "S_PUB", *, capture_only: int = 0,
          with_summary: bool = True, n_rows: int = 6,
          filter_json: dict | None = None) -> None:
    add_session(conn, sid, title="AI 出海圆桌",
                started_at="2026-11-25T20:00:00", duration_s=3600,
                capture_only=capture_only,
                speakers=[{"speaker_id": "SPK_00", "display_name": "阿哲"},
                          {"speaker_id": "SPK_01", "display_name": "小北"}])
    if n_rows:
        rows = []
        for i in range(n_rows):
            left = i % 2 == 0
            rows.append(utt_row(
                i,
                "东南亚买量便宜，一个月能跑出模型。" if left else "中东客单价高，但本地化慢。",
                speaker="SPK_00" if left else "SPK_01",
                label="阿哲" if left else "小北"))
        add_rows(conn, sid, rows)
    if not with_summary:
        return
    payload = {
        "overview": "整场围绕 AI 出海先做中东还是东南亚展开。",
        "session_title": "AI 出海，先做中东还是东南亚",
        "topics": ["AI 出海"],
        "points": [
            {"title": "先做东南亚", "stance": "先做东南亚",
             "gist": "冷启动更快，一个月能跑出模型。",
             "claim_speaker": "阿哲", "speakers": ["SPK_00"],
             "t_from": 20, "t_to": 40, "topic": "AI 出海",
             "evidence": [{"text": "东南亚买量便宜，一个月能跑出模型。",
                           "t_start": 20, "speaker_label": "阿哲",
                           "speaker_id": "SPK_00",
                           "category": "substantive"}]},
            {"title": "先做中东", "stance": "先做中东",
             "gist": "客单价高，但本地化慢。",
             "claim_speaker": "小北", "speakers": ["SPK_01"],
             "t_from": 40, "t_to": 60, "topic": "AI 出海",
             "evidence": [{"text": "中东客单价高，但本地化慢。",
                           "t_start": 40, "speaker_label": "小北",
                           "speaker_id": "SPK_01",
                           "category": "substantive"}]},
        ],
        "stats": {"points": 2},
    }
    store.save_summary(conn, sid, "content",
                       filter_json or {"session_id": sid}, "llm", payload, "fp1")
    store.upsert_note(conn, sid, state="ready", progress=100)


def test_publish_creates_one_doc_and_one_row(db, publish_cfg):
    _seed(db)
    bot = _bot()
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["ok"] is True and out["state"] == "ok"
    assert out["doc_url"].endswith("/doc_1")
    dv = bot.client.docx_v
    assert len(dv.converts) == 1
    md = dv.converts[0]
    assert "## 观点地图" in md and "先做东南亚" in md
    assert "## 人物" in md and "阿哲" in md
    assert "## 逐字稿" not in md          # 手机端文档只留结论，不搬整场逐字稿
    assert len(dv.creates) == 1

    rec = bot.client.records
    assert len(rec.created) == 1
    fields = rec.created[0][1]
    assert fields["session_id"] == "S_PUB"
    assert fields["状态"] == "已总结"
    assert fields["文档链接"]["link"] == out["doc_url"]
    assert fields["观点数"] >= 1

    row = store.get_publish(db, "S_PUB")
    assert row["state"] == "ok"
    assert row["version"] == out["version"]
    assert row["doc_token"] == "doc_1"
    assert row["record_id"] == "rec_1"
    assert int(row["card_sent"]) == 1
    assert len(bot.cards) == 1
    card = bot.cards[0][0]
    assert card["header"]["title"]["content"] == "📄 场次文档已发布"


def test_base_table_is_created_when_missing(db, publish_cfg):
    _seed(db)
    publish_cfg["base_table_id"] = ""
    feishu_publish._TABLES.clear()
    bot = _bot()
    # 空 Base：第一次发布会自己建「场次」表，然后把这一行写进去。
    bot.client.tables.list = lambda req: Resp(Obj(items=[], has_more=False))
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["ok"] is True and out["state"] == "ok"
    assert len(bot.client.tables.created) == 1
    assert len(bot.client.records.created) == 1
    assert store.get_publish(db, "S_PUB")["record_id"] == "rec_1"


def test_publish_is_idempotent(db, publish_cfg):
    _seed(db)
    bot = _bot()
    first = feishu_publish.publish(db, "S_PUB", bot=bot)
    second = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert second["skipped"] is True and second["reason"] == "unchanged"
    assert second["doc_url"] == first["doc_url"]
    assert len(bot.client.docx_v.converts) == 1
    assert len(bot.client.docx_v.creates) == 1
    assert len(bot.client.records.created) == 1
    assert len(bot.cards) == 1           # 不重复发完成卡片


def test_force_republish_rewrites_same_doc(db, publish_cfg):
    _seed(db)
    bot = _bot()
    feishu_publish.publish(db, "S_PUB", bot=bot)
    # 补名 / 改标题后：内容变化，但文档必须还是同一篇
    db.execute("UPDATE speakers SET display_name='阿哲（改名）' "
               "WHERE session_id=? AND speaker_id='SPK_00'", ("S_PUB",))
    db.commit()
    out = feishu_publish.publish(db, "S_PUB", force=True, bot=bot)
    assert out["ok"] is True and out["doc_url"].endswith("/doc_1")
    dv = bot.client.docx_v
    assert len(dv.creates) == 1           # 没有第二篇文档
    assert len(dv.converts) == 2          # 正文被重新生成
    assert dv.deletes and dv.deletes[0][0] == "doc_1"
    assert len(bot.client.records.updated) == 1
    assert len(bot.client.records.created) == 1
    assert len(store.get_publish(db, "S_PUB")["doc_token"]) > 0


def test_body_failure_keeps_doc_id_and_rewrites(db, publish_cfg):
    _seed(db)
    bot = _bot()
    bot.client.docx_v.fail_descendant = True
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["ok"] is False and out["state"] == "failed"
    row = store.get_publish(db, "S_PUB")
    assert row["doc_token"] == "doc_1"    # 文档已建出来，不能丢
    assert row["version"] == ""           # 正文没写成，不能记成已发布
    assert bot.client.records.created == []

    out2 = feishu_publish.publish(db, "S_PUB", force=True, bot=bot)
    assert out2["ok"] is True and out2["doc_url"].endswith("/doc_1")
    assert len(bot.client.docx_v.creates) == 1
    assert bot.client.docx_v.deletes[0][0] == "doc_1"
    assert store.get_publish(db, "S_PUB")["state"] == "ok"


def test_base_failure_is_partial_then_retried(db, publish_cfg):
    _seed(db)
    bot = _bot()
    bot.client.records.fail_create = 1
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["ok"] is True and out["state"] == "partial"
    row = store.get_publish(db, "S_PUB")
    assert row["state"] == "partial"
    assert row["version"]                  # 正文已经成功，重试不该重写
    assert "场次表" in row["error"]

    out2 = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out2["ok"] is True and out2["state"] == "ok"
    assert len(bot.client.docx_v.converts) == 1      # 正文没被重写
    assert len(bot.client.records.created) == 1


def test_capture_only_skeleton_then_upgrade(db, publish_cfg):
    _seed(db, "S_CAP", capture_only=1, with_summary=False, n_rows=0)
    bot = _bot()
    out = feishu_publish.publish(db, "S_CAP", bot=bot)
    assert out["state"] == "ok" and out["status"] == "pending_transcript"
    md = bot.client.docx_v.converts[0]
    assert "待补转写" in md
    assert "## 观点地图" not in md and "## 人物" not in md
    row = store.get_publish(db, "S_CAP")
    assert int(row["card_sent"]) == 0     # 骨架不发「已发布」卡片
    assert bot.cards == []

    # 补出字、跑完总结后：同一篇文档原地升级
    _seed(db, "S_CAP", capture_only=1, with_summary=True, n_rows=6)
    out2 = feishu_publish.publish(db, "S_CAP", force=True, bot=bot)
    assert out2["ok"] is True and out2["status"] == "ready"
    assert len(bot.client.docx_v.creates) == 1
    assert "## 观点地图" in bot.client.docx_v.converts[-1]
    assert int(store.get_publish(db, "S_CAP")["card_sent"]) == 1


def test_filtered_latest_summary_is_not_published(db, publish_cfg):
    _seed(db, with_summary=False)
    _seed(db, with_summary=True, n_rows=0,
          filter_json={"session_id": "S_PUB",
                       "categories": ["question_consult"]})
    bot = _bot()
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["ok"] is False and out["skipped"] is True
    assert out["reason"] == "filtered_summary"
    assert bot.client.docx_v.converts == []


def test_disabled_publish_is_a_silent_noop(db, publish_cfg, monkeypatch):
    publish_cfg["enabled"] = False
    _seed(db)
    bot = _bot()
    out = feishu_publish.publish(db, "S_PUB", bot=bot)
    assert out["skipped"] is True and out["reason"] == "disabled"
    assert bot.client.docx_v.converts == []
    assert store.get_publish(db, "S_PUB") is None


def test_unconfigured_feishu_records_failure(db, publish_cfg, monkeypatch):
    def boom(cfg):
        raise RuntimeError("缺少飞书凭证")
    monkeypatch.setattr(feishu_publish, "_default_bot", boom)
    _seed(db)
    out = feishu_publish.publish(db, "S_PUB")
    assert out["ok"] is False and out["state"] == "failed"
    assert "飞书未配置" in out["message"]
    row = store.get_publish(db, "S_PUB")
    assert row["state"] == "failed" and "飞书未配置" in row["error"]


def test_publish_never_raises(db, publish_cfg, monkeypatch):
    monkeypatch.setattr(export, "to_publish_md",
                        lambda conn, sid: (_ for _ in ()).throw(RuntimeError("boom")))
    out = feishu_publish.publish(db, "S_PUB", bot=_bot())
    assert out["ok"] is False and out["state"] == "failed"
    assert "boom" in out["message"]


def test_export_skeleton_has_no_invented_summary(db):
    _seed(db, "S_CAP", capture_only=1, with_summary=False, n_rows=0)
    doc = export.to_publish_md(db, "S_CAP")
    assert doc["ok"] is True and doc["state"] == "pending_transcript"
    assert "待补转写" in doc["markdown"]
    assert "## 观点地图" not in doc["markdown"]
    assert "—" in doc["markdown"] or "0 句" in doc["markdown"]
