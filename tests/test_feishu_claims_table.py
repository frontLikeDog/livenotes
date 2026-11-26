# -*- coding: utf-8 -*-
"""观点表（飞书多维表格）：按场次整体替换、幂等、状态派生与失败降级。"""
from __future__ import annotations

import pytest

from app import claims, feishu, feishu_publish, store
from helpers import add_rows, add_session, utt_row
from test_feishu_publish import FakeBot

SID = "S_CLM"


def _add_person(conn, pid: str, name: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO persons(person_id, name, created_at) VALUES (?,?,?)",
        (pid, name, store.now()))
    conn.commit()


def _session_with_claims(conn, sid: str = SID, *,
                         started_at: str = "2026-11-25T21:00:00",
                         title: str = "AI 出海圆桌",
                         points: list[dict] | None = None,
                         speakers: list[tuple] | None = None) -> str:
    add_session(conn, sid, title=title, started_at=started_at, duration_s=3600)
    add_rows(conn, sid, [
        utt_row(0, "先做东南亚冷启动更快", t=549, speaker="SPK_00", label="说话人1"),
        utt_row(1, "中东客单价更高适合工具", t=3725, speaker="SPK_01", label="说话人2"),
        utt_row(2, "中东本地化客服谁来做？", t=3800, speaker="SPK_01", label="说话人2"),
    ])
    for sp in (speakers if speakers is not None else [
            ("SPK_00", "阿哲", "per_azhe", "confirmed"),
            ("SPK_01", "小北", "", "")]):
        spk, name, pid, pstate = sp
        store.upsert_speaker(conn, sid, spk, name)
        if pid:
            _add_person(conn, pid, name)
            conn.execute(
                "UPDATE speakers SET person_id=?, person_state=? "
                "WHERE session_id=? AND speaker_id=?",
                (pid, pstate or "confirmed", sid, spk))
            conn.commit()
    pts = points if points is not None else [
        {
            "title": "先做东南亚", "gist": "东南亚支付与生态更成熟",
            "t_from": 549, "t_to": 560, "claim_speaker": "阿哲",
            "stance": "出海应该先做东南亚",
            "evidence": [{"id": 1, "t_start": 549, "speaker_label": "阿哲",
                          "speaker_id": "SPK_00", "topic": "AI 出海",
                          "category": "substantive",
                          "quote": "先做东南亚冷启动更快"}],
        },
        {
            "title": "先做中东", "gist": "中东工具付费意愿强",
            "t_from": 3725, "t_to": 3740, "claim_speaker": "小北",
            "stance": "工具类应该先做中东",
            "evidence": [{"id": 2, "t_start": 3725, "speaker_label": "小北",
                          "speaker_id": "SPK_01", "topic": "AI 出海",
                          "category": "substantive",
                          "quote": "中东客单价更高适合工具"}],
        },
        {
            "title": "客服归属", "gist": "中东本地化客服谁来做",
            "t_from": 3800, "t_to": 3810, "claim_speaker": "小北",
            "stance": "中东的本地化客服谁来做",
            "evidence": [{"id": 3, "t_start": 3800, "speaker_label": "小北",
                          "speaker_id": "SPK_01", "topic": "本地化",
                          "category": "question_consult",
                          "quote": "中东本地化客服谁来做？"}],
        },
    ]
    store.save_summary(conn, sid, "content", {}, "fake", {
        "session_title": title, "overview": "争论先做东南亚还是中东。",
        "topics": ["AI 出海", "本地化"], "points": pts,
    })
    claims.rebuild_session(conn, sid)
    return sid


def _claims_table_id(conn) -> str:
    return store.get_kv(conn, "feishu_base_claims_table_id")


def _claims_rows(bot, conn) -> list[dict]:
    tid = _claims_table_id(conn)
    return list(bot.client.bitable.v1.app_table_record.by_table.get(tid, {}).values())


# ---------------------------------------------------------------- 1. 基础建表与写入

def test_three_claims_create_three_rows_and_schema(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"

    # 建了「场次」和「观点」两张表，主字段是第一个字段，默认视图「最近观点」
    tables = bot.client.bitable.v1.app_table.tables
    claims_tid = _claims_table_id(db)
    assert claims_tid and claims_tid in tables
    assert tables[claims_tid]["name"] == "观点"
    assert tables[claims_tid]["default_view_name"] == "最近观点"
    assert tables[claims_tid]["fields"][0] == "观点"
    assert feishu_publish.CLAIMS_FIELD_DEFS[0]["field_name"] == "观点"

    rows = _claims_rows(bot, db)
    assert len(rows) == 3
    by_view = {r["观点"]: r for r in rows}

    r1 = by_view["出海应该先做东南亚"]
    assert r1["谁"] == "阿哲"
    assert r1["person_id"] == "per_azhe"
    assert r1["主题"] == "AI 出海"
    assert r1["类别"] == "主张"
    assert r1["状态"] == "新提出"
    assert r1["场次"] == "AI 出海圆桌"
    assert isinstance(r1["日期"], int) and r1["日期"] > 0
    assert r1["时间码"] == "9:09"
    assert r1["原话"] == "先做东南亚冷启动更快"
    assert r1["场次文档链接"]["link"] == out["doc_url"]
    assert r1["session_id"] == sid and r1["claim_id"].startswith("clm_")

    r2 = by_view["工具类应该先做中东"]
    assert r2["谁"] == "小北" and r2["person_id"] == ""
    assert r2["时间码"] == "1:02:05"
    assert r2["类别"] == "主张"

    r3 = by_view["中东的本地化客服谁来做"]
    assert r3["类别"] == "问题"
    assert r3["主题"] == "本地化"

    # 台账落了一行 kind="claims"
    pub = store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)
    assert pub and pub["state"] == "ok" and pub["version"]


def test_reusing_existing_base_still_grants_permission_and_env_claims_id_wins(
        db, monkeypatch):
    sid = _session_with_claims(db)
    bot = FakeBot()
    bot.cfg["base_app_token"] = "app_pre"
    bot.cfg["base_table_id"] = "tbl_sess_pre"
    monkeypatch.setenv("FEISHU_BASE_CLAIMS_TABLE_ID", "tbl_claims_env")
    # 预置字段列表让 _ensure_fields 通过
    bot.client.bitable.v1.app_table_field.fields_by_table["tbl_claims_env"] = [
        f["field_name"] for f in feishu_publish.CLAIMS_FIELD_DEFS]

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    # 复用已有 Base 也要走 permission_member 授权
    assert ("app_pre", "ou_owner", "bitable") in bot.grants
    # 环境变量优先，不新建观点表
    assert not bot.client.bitable.v1.app_table.created
    rows = list(bot.client.bitable.v1.app_table_record.by_table["tbl_claims_env"].values())
    assert len(rows) == 3
    # config() / load_config() 都能读到 base_claims_table_id
    assert feishu.load_config()["base_claims_table_id"] == "tbl_claims_env"
    assert feishu_publish.config()["base_claims_table_id"] == "tbl_claims_env"


# ---------------------------------------------------------------- 2. 重复发布替换

def test_repeat_publish_replaces_all_rows_and_keeps_single_ledger_row(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    rec = bot.client.bitable.v1.app_table_record
    rec.ops.clear()

    out = feishu_publish.publish(sid, bot=bot, conn=db, force=True, notify=False)
    assert out["ok"] and out["state"] == "ok"
    # 先搜 → 再删 3 → 再建 3
    claims_ops = [op for op in rec.ops if op in ("search", "batch_delete", "batch_create")]
    assert claims_ops == ["search", "batch_delete", "batch_create"]
    assert len(rec.batch_deleted[-1].request_body.records) == 3
    assert len(rec.batch_created[-1].request_body.records) == 3
    assert len(_claims_rows(bot, db)) == 3
    n_ledger = db.execute(
        "SELECT COUNT(*) AS n FROM feishu_publish WHERE session_id=? AND kind=?",
        (sid, feishu_publish.KIND_CLAIMS)).fetchone()["n"]
    assert n_ledger == 1


# ---------------------------------------------------------------- 3. 要点减少无孤儿

def test_rerun_summary_fewer_points_leaves_no_orphans(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert len(_claims_rows(bot, db)) == 3

    # 重跑后只剩 2 条要点
    store.save_summary(db, sid, "content", {}, "fake", {
        "session_title": "AI 出海圆桌", "overview": "精简为两条结论。",
        "topics": ["AI 出海"],
        "points": [
            {"title": "先做东南亚", "gist": "生态成熟", "claim_speaker": "阿哲",
             "stance": "出海应该先做东南亚",
             "evidence": [{"id": 1, "t_start": 549, "speaker_label": "阿哲",
                           "speaker_id": "SPK_00", "topic": "AI 出海",
                           "quote": "先做东南亚冷启动更快"}]},
            {"title": "先做中东", "gist": "付费意愿强", "claim_speaker": "小北",
             "stance": "工具类应该先做中东",
             "evidence": [{"id": 2, "t_start": 3725, "speaker_label": "小北",
                           "speaker_id": "SPK_01", "topic": "AI 出海",
                           "quote": "中东客单价更高适合工具"}]},
        ],
    })
    claims.rebuild_session(db, sid)

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    rows = _claims_rows(bot, db)
    assert len(rows) == 2
    assert {r["观点"] for r in rows} == {"出海应该先做东南亚", "工具类应该先做中东"}


# ---------------------------------------------------------------- 4. 幂等跳过

def test_unchanged_version_sends_no_delete_or_create(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    rec = bot.client.bitable.v1.app_table_record
    n_search = len(rec.searches)
    n_del = len(rec.batch_deleted)
    n_cre = len(rec.batch_created)

    again = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert again["ok"] and again["unchanged"] is True
    assert len(rec.searches) == n_search
    assert len(rec.batch_deleted) == n_del
    assert len(rec.batch_created) == n_cre


# ---------------------------------------------------------------- 5. 仅 doc_url 变化

def test_only_doc_url_changed_rewrites_claims_without_touching_doc(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert [c[0] for c in bot.calls] == ["create"]

    # 仅台账里的 doc_url 变了，文档正文和要点未变
    new_url = "https://tenant.feishu.cn/docx/doc_1"
    store.upsert_publish(db, sid, feishu_publish.KIND_SESSION, doc_url=new_url)

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    assert out.get("claims_updated") is True
    assert [c[0] for c in bot.calls] == ["create"]   # 正文未重写，保住评论锚点
    assert not bot.client.bitable.v1.app_table_record.updated  # 场次表未动
    rows = _claims_rows(bot, db)
    assert len(rows) == 3
    assert all(r["场次文档链接"]["link"] == new_url for r in rows)


# ---------------------------------------------------------------- 6. 失败语义与重试

def test_claims_failure_is_partial_and_retry_only_retries_claims(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    bot.client.bitable.v1.app_table_record.batch_create_fail = True

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] is True
    assert out["state"] == "partial"
    assert "观点表" in out["error"]
    assert store.get_publish(db, sid, feishu_publish.KIND_SESSION)["state"] == "partial"
    assert store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)["state"] == "failed"

    # 恢复后重试：不重写文档，不更新场次表，只补写观点表
    bot.client.bitable.v1.app_table_record.batch_create_fail = False
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] is True and out2["state"] == "ok"
    assert [c[0] for c in bot.calls] == ["create"]
    assert not bot.client.bitable.v1.app_table_record.updated
    assert len(_claims_rows(bot, db)) == 3
    assert store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)["state"] == "ok"


def test_doc_failure_still_writes_claims_without_link_and_retry_backfills_link(db):
    sid = _session_with_claims(db)
    bot = FakeBot(doc_fail=True)
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] is False and out["state"] == "failed"
    rows = _claims_rows(bot, db)
    assert len(rows) == 3
    assert all("场次文档链接" not in r for r in rows)
    assert store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)["state"] == "ok"

    # 文档恢复后重试：签名含 doc_url，观点表重写并补上链接
    bot.doc_fail = False
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] is True and out2["state"] == "ok"
    rows2 = _claims_rows(bot, db)
    assert len(rows2) == 3
    assert all(r["场次文档链接"]["link"] == out2["doc_url"] for r in rows2)


def test_delete_succeeds_create_fails_retry_has_no_duplicates(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert len(_claims_rows(bot, db)) == 3

    # 模拟删成功、建失败窗口
    rec = bot.client.bitable.v1.app_table_record
    rec.batch_create_fail = True
    out = feishu_publish.publish(sid, bot=bot, conn=db, force=True, notify=False)
    assert out["state"] == "partial"
    assert store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)["state"] == "failed"
    assert len(_claims_rows(bot, db)) == 0  # 旧行已删

    # 恢复后重试：搜为空 → 直接建 3 行，无重复
    rec.batch_create_fail = False
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] and out2["state"] == "ok"
    assert len(_claims_rows(bot, db)) == 3


# ---------------------------------------------------------------- 7. 无总结 / 观点清空

def test_no_summary_never_creates_table_and_cleared_claims_deletes_old_rows(db):
    # 从未写过且没有总结：不建观点表，不调 batch_create
    add_session(db, "S_EMPTY", title="只录了音", started_at="2026-11-25T20:00:00",
                duration_s=300, capture_only=True)
    bot = FakeBot()
    out = feishu_publish.publish("S_EMPTY", bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    assert not _claims_table_id(db)
    assert not bot.client.bitable.v1.app_table_record.batch_created
    assert store.get_publish(db, "S_EMPTY", feishu_publish.KIND_CLAIMS) is None

    # 之前写过 3 行，重跑总结清空了观点：删干净旧行，不建新行，台账落空集签名
    sid = _session_with_claims(db)
    feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert len(_claims_rows(bot, db)) == 3
    n_cre_before = len(bot.client.bitable.v1.app_table_record.batch_created)

    store.save_summary(db, sid, "content", {}, "fake", {
        "session_title": "AI 出海圆桌", "overview": "本次无明确主张。",
        "topics": [], "points": [],
    })
    claims.rebuild_session(db, sid)
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] and out2["state"] == "ok"
    assert len(_claims_rows(bot, db)) == 0
    assert len(bot.client.bitable.v1.app_table_record.batch_created) == n_cre_before
    pub = store.get_publish(db, sid, feishu_publish.KIND_CLAIMS)
    assert pub and pub["state"] == "ok" and pub["version"]


# ---------------------------------------------------------------- 8. 未命名说话人

def test_unnamed_speaker_uses_session_slot_label_and_empty_person_id(db):
    sid = "S_ANON"
    add_session(db, sid, title="匿名场", started_at="2026-11-25T21:00:00",
                duration_s=1200)
    add_rows(db, sid, [
        utt_row(0, "第一段发言", t=10, speaker="SPK_00", label="说话人1"),
        utt_row(1, "第二段发言提出新想法", t=30, speaker="SPK_01", label="嘉宾2"),
    ])
    store.save_summary(db, sid, "content", {}, "fake", {
        "session_title": "匿名场", "overview": "匿名讨论。",
        "topics": ["定价"],
        "points": [{
            "title": "定价", "gist": "先收年费再做按量", "claim_speaker": "嘉宾2",
            "stance": "",  # 顺便验证 stance 空退 gist
            "evidence": [{"id": 2, "t_start": 30, "speaker_label": "嘉宾2",
                          "speaker_id": "SPK_01", "topic": "定价",
                          "quote": "第二段发言提出新想法"}],
        }],
    })
    claims.rebuild_session(db, sid)

    bot = FakeBot()
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    rows = _claims_rows(bot, db)
    assert len(rows) == 1
    assert rows[0]["谁"] == "本场第 2 位上麦者"
    assert rows[0]["person_id"] == ""
    assert rows[0]["观点"] == "先收年费再做按量"


# ---------------------------------------------------------------- 9. 状态派生

def test_claim_status_derivation_first_repeat_revision_and_same_session_isolated(db):
    _add_person(db, "per_azhe", "阿哲")

    # 较早场次（S1）：同场内两条同人同主题不互相比较，都应是「新提出」
    add_session(db, "S1", title="第一场", started_at="2026-11-18T21:00:00",
                duration_s=1800)
    db.executemany(
        "INSERT INTO claims(claim_id,session_id,person_id,who,topic,stance,gist,"
        "quote,t,kind,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("clm_s1_1", "S1", "per_azhe", "阿哲", "出海方向",
             "中东客单价高应该先做中东工具", "", "原话1", 60, "claim", "[]", store.now()),
            ("clm_s1_2", "S1", "per_azhe", "阿哲", "出海方向",
             "其实东南亚支付生态也值得研究", "", "原话2", 120, "claim", "[]", store.now()),
            ("clm_s1_3", "S1", "per_azhe", "阿哲", "付费策略",
             "年费订阅比按次付费留存高很多", "", "原话3", 180, "claim", "[]", store.now()),
        ])
    db.commit()
    recs1, _ = feishu_publish._claims_records(db, "S1", "")
    assert [r["状态"] for r in recs1] == ["新提出", "新提出", "新提出"]

    # 较晚场次（S2）：
    # - 付费策略：与 S1 高度相似 → 重申
    # - 出海方向：与 S1 最近一条（t=120）差异很大 → 修正
    # - 招聘（首次出现主题）→ 新提出
    add_session(db, "S2", title="第二场", started_at="2026-11-25T21:00:00",
                duration_s=1800)
    db.executemany(
        "INSERT INTO claims(claim_id,session_id,person_id,who,topic,stance,gist,"
        "quote,t,kind,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("clm_s2_1", "S2", "per_azhe", "阿哲", "付费策略",
             "年费订阅比按次付费留存高", "", "原话A", 30, "claim", "[]", store.now()),
            ("clm_s2_2", "S2", "per_azhe", "阿哲", "出海方向",
             "完全放弃海外全力做国内私域", "", "原话B", 90, "claim", "[]", store.now()),
            ("clm_s2_3", "S2", "per_azhe", "阿哲", "团队招聘",
             "早期不要招纯管理岗", "", "原话C", 150, "claim", "[]", store.now()),
        ])
    db.commit()
    recs2, _ = feishu_publish._claims_records(db, "S2", "")
    by_id = {r["claim_id"]: r["状态"] for r in recs2}
    assert by_id["clm_s2_1"] == "重申"
    assert by_id["clm_s2_2"] == "修正"
    assert by_id["clm_s2_3"] == "新提出"


# ---------------------------------------------------------------- 10. 超 100 行分页删除

def test_over_100_rows_paginates_search_and_deletes_all(db):
    sid = "S_MANY"
    add_session(db, sid, title="超长圆桌", started_at="2026-11-25T21:00:00",
                duration_s=7200)
    db.executemany(
        "INSERT INTO claims(claim_id,session_id,person_id,who,topic,stance,gist,"
        "quote,t,kind,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [("clm_m_%03d" % i, sid, "", "阿哲", "主题", "观点第 %d 条" % i, "",
          "原话 %d" % i, float(i * 10), "claim", "[]", store.now())
         for i in range(125)])
    db.commit()

    bot = FakeBot()
    rec = bot.client.bitable.v1.app_table_record
    rec.search_page_size = 100

    res1 = feishu_publish._upsert_claims(db, bot, sid, "https://feishu.cn/docx/d1")
    assert res1["ok"] and res1["created"] == 125
    assert len(_claims_rows(bot, db)) == 125

    # 强制重发：search 必须翻 2 页（100 + 25），125 条全部删干净并重建 125 条
    rec.searches.clear()
    rec.batch_deleted.clear()
    res2 = feishu_publish._upsert_claims(db, bot, sid, "https://feishu.cn/docx/d1",
                                         force=True)
    assert res2["ok"] and res2["deleted"] == 125 and res2["created"] == 125
    assert len(rec.searches) == 2
    assert len(_claims_rows(bot, db)) == 125


# ---------------------------------------------------------------- 11. base_enabled=0

def test_base_disabled_skips_claims_table_and_sends_no_requests(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    bot.cfg["base_enabled"] = False
    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    assert not bot.client.bitable.v1.app.calls
    assert not bot.client.bitable.v1.app_table.created
    assert not bot.client.bitable.v1.app_table_record.batch_created
    assert not bot.client.bitable.v1.app_table_record.searches
    assert store.get_publish(db, sid, feishu_publish.KIND_CLAIMS) is None


# ---------------------------------------------------------------- 12. 老表缺字段自动补

def test_existing_claims_table_missing_fields_auto_backfills(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    bot.cfg["base_app_token"] = "app_old"
    bot.cfg["base_table_id"] = "tbl_sess_old"
    bot.cfg["base_claims_table_id"] = "tbl_claims_old"
    # 老表只有前 3 个字段，缺其余 10 个
    bot.client.bitable.v1.app_table_field.fields_by_table["tbl_claims_old"] = [
        "观点", "谁", "主题"]

    out = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out["ok"] and out["state"] == "ok"
    created_names = [
        str(req.request_body.field_name)
        for req in bot.client.bitable.v1.app_table_field.created
        if req.table_id == "tbl_claims_old"
    ]
    expected_missing = [
        f["field_name"] for f in feishu_publish.CLAIMS_FIELD_DEFS
        if f["field_name"] not in {"观点", "谁", "主题"}
    ]
    assert created_names == expected_missing
    assert len(bot.client.bitable.v1.app_table_record.by_table["tbl_claims_old"]) == 3


def test_base_grant_failure_is_retried_and_not_swallowed_on_retry(db):
    sid = _session_with_claims(db)
    bot = FakeBot()
    fail_bitable_grant = True

    def flaky_grant(document_id, open_id, perm="full_access", doc_type="docx"):
        bot.grants.append((document_id, open_id, doc_type))
        if doc_type == "bitable" and fail_bitable_grant:
            return {"ok": False, "error": "no perm"}
        return {"ok": True}

    bot._grant_doc = flaky_grant

    out1 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out1["ok"] is True and out1["state"] == "partial"
    assert "场次表授权失败" in out1["error"]

    # 权限仍失败时重试：不能吞掉授权失败而误标成 ok
    out2 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out2["ok"] is True and out2["state"] == "partial"
    assert "场次表授权失败" in out2["error"]

    # 权限恢复后重试：补授权成功并回到 ok
    fail_bitable_grant = False
    out3 = feishu_publish.publish(sid, bot=bot, conn=db, notify=False)
    assert out3["ok"] is True and out3["state"] == "ok"
    assert ("app_1", "ou_owner", "bitable") in bot.grants
