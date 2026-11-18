# -*- coding: utf-8 -*-
"""追问：线程字段、上一轮上下文、规则追问（docs/41 §4.3）。

S4 的 A 部分不引入新的对话表：``qa_answers.thread_id`` + ``parent_id``
足够把一轮问答串起来。这里重点守三件事：

1. 首问第一次被追问时补上 thread_id，追问沿用同一个线程号；
2. 送进模型的上下文只有上一轮问题 + 200 字答案 + 引用标签；
3. 追问建议是规则拼的，关掉开关就一条都不出现。
"""
from __future__ import annotations

from types import SimpleNamespace

from app import claims, qa, settings, store


# ---------------------------------------------------------------- 造数据

def _session(conn, sid, started_at, title, points, topics=()):
    store.insert_session(conn, {"id": sid, "title": title, "started_at": started_at,
                                "duration_s": 1800, "source_mode": "tab"})
    store.insert_utterances(conn, [
        {"session_id": sid, "speaker_id": "SPK_00", "speaker_label": "阿哲",
         "t_start": 10.0 * i, "t_end": 10.0 * i + 8, "text": "第%d句" % i,
         "category": "substantive", "topic": (topics[i % len(topics)]
                                              if topics else "")}
        for i in range(len(points))])
    store.upsert_speaker(conn, sid, "SPK_00", "阿哲")
    store.save_summary(conn, sid, "content", {}, "extractive", {
        "session_title": title, "overview": "", "topics": list(topics),
        "points": points,
    })


def _point(who, stance, topic, uid, t=10.0, cat="substantive"):
    return {"title": "话题：%s" % topic, "gist": "%s 的展开" % stance,
            "t_from": t, "t_to": t + 8, "speakers": [who],
            "claim_speaker": who, "stance": stance,
            "evidence": [{"id": uid, "t_start": t, "speaker_label": who,
                          "category": cat, "topic": topic,
                          "quote": stance + "，这是原话"}]}


def _two_sessions(db):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 1)])
    _session(db, "S2", "2026-11-16T20:00:00", "第二场",
             [_point("阿哲", "其实是备案门槛被抬高了", "OPC 政策", 2)])
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")


def _ready_row(db, question, citations, qid=None):
    row = store.qa_create(db, question, {"session_id": "S2"})
    if qid:
        db.execute("UPDATE qa_answers SET id=? WHERE id=?", (qid, row["id"]))
        db.commit()
        row = store.qa_get(db, qid)
    return store.qa_update(db, row["id"], state="ready", answer="找到相关内容。",
                           citations=citations)


# ---------------------------------------------------------------- 线程

def test_thread_block_carries_previous_question_answer_and_labels(db):
    parent = store.qa_create(db, "首问：OPC 会收紧吗", {"session_id": "S1"})
    store.qa_update(db, parent["id"], state="ready",
                    answer="答" * 260,
                    citations=[{"n": 1, "kind": "claim", "speaker": "阿哲",
                                "title": "第一场"}])
    child = store.qa_create(db, "追问：具体是哪条政策", {}, "", parent["id"])
    block, rule = qa._thread_block(db, child)

    assert "上一轮问题：首问：OPC 会收紧吗" in block
    assert "答" * 200 in block and "答" * 201 not in block
    assert "[1] claim · 阿哲 · 第一场" in block
    assert "不能当作证据" in rule


def test_thread_block_missing_parent_degrades_to_plain_qa(db):
    child = store.qa_create(db, "追问", {}, "", 99999)
    assert qa._thread_block(db, child) == ("", "")


def test_scope_key_includes_parent_id_so_threads_do_not_share_cache():
    plain = qa._scope_json("同一句话", "", 0)
    threaded = qa._scope_json("同一句话", "", 42)
    assert "parent_id" not in plain
    assert threaded["parent_id"] == 42


def test_ask_backfills_root_thread_and_keeps_followup_in_it(db, monkeypatch):
    monkeypatch.setattr(qa.scheduler, "submit",
                        lambda *a, **k: SimpleNamespace(id=1))
    root = qa.ask("首问", "S1")
    assert root["thread_id"] == ""
    follow = qa.ask("追问", "S1", parent_id=root["id"])

    assert follow["parent_id"] == root["id"]
    assert follow["thread_id"] == str(root["id"])
    assert store.qa_get(db, root["id"])["thread_id"] == str(root["id"])
    assert store.qa_get(db, follow["id"])["parent_id"] == root["id"]


# ---------------------------------------------------------------- 规则追问

def test_followups_use_claim_history_not_generic_prompts(db):
    _two_sessions(db)
    cid = db.execute("SELECT claim_id FROM claims WHERE session_id='S2'").fetchone()[0]
    row = _ready_row(db, "阿哲怎么看 OPC 政策",
                     [{"n": 1, "kind": "claim", "ref_id": cid,
                       "session_id": "S2", "speaker": "阿哲"}])

    out = qa.suggest_followups(db, row)
    assert 1 <= len(out) <= qa.FOLLOWUP_LIMIT
    text = " ".join(x["q"] for x in out)
    assert "还讲过什么" in text
    assert "最早是哪一场" in text
    assert all(x["why"] for x in out)


def test_followups_respect_switch(db):
    _two_sessions(db)
    cid = db.execute("SELECT claim_id FROM claims WHERE session_id='S2'").fetchone()[0]
    row = _ready_row(db, "阿哲怎么看 OPC 政策",
                     [{"n": 1, "kind": "claim", "ref_id": cid,
                       "session_id": "S2", "speaker": "阿哲"}])
    settings.set_prefs({"qa_followups_enabled": False})
    assert qa.suggest_followups(db, row) == []


def test_followups_offer_to_check_a_prediction(db):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 1)])
    claims.rebuild_session(db, "S1")
    cid = db.execute("SELECT claim_id FROM claims WHERE session_id='S1'").fetchone()[0]
    row = _ready_row(db, "阿哲怎么看 OPC 政策",
                     [{"n": 1, "kind": "claim", "ref_id": cid,
                       "session_id": "S1", "speaker": "阿哲"}])
    # 只留「预测」这一条规则：把问题问得足够具体，其他规则也会出现，
    # 但至少验证规则里必须有它。
    assert any("验证" in x["q"] for x in qa.suggest_followups(db, row))


def test_followups_flag_an_unanswered_question(db):
    _session(db, "S_Q", "2026-11-02T20:00:00", "问答场",
             [_point("听众", "抽成按什么口径算", "结算", 3,
                     cat="question_consult")])
    claims.rebuild_session(db, "S_Q")
    cid = db.execute("SELECT claim_id FROM claims WHERE session_id='S_Q'").fetchone()[0]
    row = _ready_row(db, "结算里有什么没解决的",
                     [{"n": 1, "kind": "claim", "ref_id": cid,
                       "session_id": "S_Q", "speaker": "听众"}])
    assert any("后来有人回答吗" in x["q"] for x in qa.suggest_followups(db, row))
