# -*- coding: utf-8 -*-
"""S5 内容工具：观点卡/名片只读页 + 圆桌摘要（docs/41 §6.1/§6.2）。

这些能力都从已经落库的场次出发，不新增模型调用。测试守四件事：

1. 观点卡只挑「有主张」的原子，有原话的排前面；
2. 只读页能独立打开，底部有「记录你自己的上麦」出口；
3. 圆桌摘要按主题排人、标分歧、收没答案的问题；
4. 桌面/网页路由真的能打开 /tools、/claim、/card。
"""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from app import cards, claims, live, roundtable, server, store
from tests.test_claims_profile import _point, _session


def _roundtable_session(db):
    """一场三人圆桌：同主题有明确反对，另一个主题只有人问。"""
    sid = "S_RT"
    ids = _session(
        db, sid, "2026-11-16T20:00:00", "圆桌一场",
        [("SPK_00", "阿哲", "per_a", "confirmed"),
         ("SPK_01", "小鹿", "per_lu", "confirmed"),
         ("SPK_02", "大周", "", "")],
        [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1, t=21),
         _point("小鹿", "我不同意，真正的变量是平台抽成", "OPC 政策", 2, t=42),
         _point("大周", "去年有人吃过这个亏", "OPC 政策", 3, t=63),
         _point("听众", "抽成按什么口径算", "结算", 4, t=84,
                cat="question_consult")],
        topics=("OPC 政策", "结算"))
    claims.rebuild_session(db, sid)
    return sid, ids


# ---------------------------------------------------------------- 观点卡

def test_session_claims_prefers_claims_with_quotes(db):
    sid, _ = _roundtable_session(db)
    rows = cards.session_claims(db, sid)
    assert [r["who"] for r in rows][:3] == ["阿哲", "小鹿", "大周"]
    assert rows[-1]["kind"] == "question"
    assert all(r["claim_id"] and r["stance"] for r in rows)
    assert rows[0]["session_id"] == sid


def test_claim_page_is_read_only_and_links_back(db):
    sid, _ = _roundtable_session(db)
    row = cards.session_claims(db, sid)[0]
    claim = claims.get(db, row["claim_id"])
    page = cards.claim_page(claim)
    assert "明年 OPC 会被收紧" in page
    assert "阿哲" in page and "OPC 政策" in page
    assert "记录你自己的上麦" in page
    assert "/note?session=%s" % sid in page
    assert "不提供原声下载" in page


def test_person_card_lists_topics_and_latest_stance(db):
    sid, _ = _roundtable_session(db)
    store.upsert_person(db, "per_a", "阿哲")
    page = cards.person_page(db, "per_a")
    assert page and "上麦者名片" in page
    assert "OPC 政策" in page
    assert "明年 OPC 会被收紧" in page
    assert "/people?person=per_a" in page
    assert cards.person_page(db, "per_missing") is None


# ---------------------------------------------------------------- 圆桌摘要

def test_roundtable_groups_people_topics_and_disagreements(db):
    sid, _ = _roundtable_session(db)
    data = roundtable.build(db, sid)
    assert data["ok"] and data["session"]["session_id"] == sid
    assert {p["name"] for p in data["participants"]} >= {"阿哲", "小鹿", "大周"}
    opc = next(g for g in data["topics"] if g["topic"] == "OPC 政策")
    assert opc["n"] == 3 and opc["conflict"] is True
    lu = next(e for e in opc["entries"] if e["who"] == "小鹿")
    assert lu["disagree"] is True and lu["vs"] == "阿哲"
    assert data["stats"]["conflicts"] == 1
    # 只有提问、没有主张的主题不会被当成「观点主题」
    assert all(g["topic"] != "结算" for g in data["topics"])
    assert [q["stance"] for q in data["open_questions"]] == ["抽成按什么口径算"]


def test_roundtable_markdown_is_publishable_text(db):
    sid, _ = _roundtable_session(db)
    md = roundtable.markdown(roundtable.build(db, sid))
    assert md.startswith("# 圆桌摘要 · 圆桌一场")
    assert "## OPC 政策（有分歧）" in md
    assert "**小鹿**：我不同意" in md and "和阿哲说法不同" in md
    assert "## 还没答案的问题" in md
    assert "不判断对错" in md


def test_roundtable_refuses_without_notes(db):
    store.insert_session(db, {"id": "S_EMPTY", "title": "空场",
                              "started_at": "2026-11-18T20:00:00",
                              "duration_s": 60, "source_mode": "tab"})
    out = roundtable.build(db, "S_EMPTY")
    assert out["ok"] is True          # 有场次就能生成，只是内容为空
    assert out["topics"] == [] and out["participants"] == []
    assert "还不能生成" in roundtable.markdown({"ok": False})


# ---------------------------------------------------------------- 路由

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


def _text(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        assert r.status == 200
        return r.read().decode("utf-8")


def _json(base, path):
    return json.loads(_text(base, path))


def test_tools_page_and_roundtable_api(http_base, db):
    sid, _ = _roundtable_session(db)
    page = _text(http_base, "/tools")
    assert "内容助手" in page and 'id="rtSession"' in page
    data = _json(http_base, "/api/roundtable?session_id=" + sid)
    assert data["ok"] and data["stats"]["people"] >= 3
    md = _text(http_base, "/api/roundtable/export?session_id=" + sid)
    assert "圆桌摘要" in md


def test_claim_and_person_pages_are_public_read_only(http_base, db):
    sid, _ = _roundtable_session(db)
    store.upsert_person(db, "per_a", "阿哲")
    row = cards.session_claims(db, sid)[0]
    claim_html = _text(http_base, "/claim?claim_id=" + row["claim_id"])
    assert "观点" in claim_html and "记录你自己的上麦" in claim_html
    person_html = _text(http_base, "/card?person_id=per_a")
    assert "上麦者名片" in person_html and "OPC 政策" in person_html
