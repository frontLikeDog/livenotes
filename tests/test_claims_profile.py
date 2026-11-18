# -*- coding: utf-8 -*-
"""观点档案：跨场「谁在什么主题上说过什么」。

上一片做到了「本场按人分组」；这一片要回答的是「出了这场还在不在」。所以这里
重点测四件事：

1. 抽取：要从总结要点里拿到 (人, 主题, 主张)，且主题必须来自本场词表；
2. 归属：声纹认出来的用 person_id，认不出来的退回昵称，两者不能混；
3. 时间线：同一个 (人, 主题) 的多场主张按时间排开，改口能被标出来；
4. 不越界：只出现在这一场的观点不该多一个「跨 N 场」徽标。
"""
from __future__ import annotations

from app import claims, notes, store


# ---------------------------------------------------------------- 造数据

def _session(conn, sid, started_at, title, speakers, points, topics=()):
    """一场：speakers = [(spk_id, display, person_id, state)]。"""
    store.insert_session(conn, {"id": sid, "title": title, "started_at": started_at,
                                "duration_s": 1800, "source_mode": "tab"})
    ids = store.insert_utterances(conn, [
        {"session_id": sid, "speaker_id": spk, "speaker_label": disp,
         "t_start": 10.0 * i, "t_end": 10.0 * i + 8, "text": "第%d句" % i,
         "category": "substantive", "topic": (topics[i % len(topics)]
                                              if topics else "")}
        for i, (spk, disp, _pid, _st) in enumerate(speakers)])
    for spk, disp, pid, state in speakers:
        store.upsert_speaker(conn, sid, spk, disp)
        if pid:
            store.set_speaker_person(conn, sid, spk, pid, 0.4, 0.1, state)
    store.save_summary(conn, sid, "content", {}, "extractive", {
        "session_title": title, "overview": "", "topics": list(topics),
        "points": points,
    })
    return ids


def _point(who, stance, topic, uid, t=10.0, cat="substantive"):
    return {"title": "话题：%s" % topic, "gist": "%s 的展开" % stance,
            "t_from": t, "t_to": t + 8, "speakers": [who],
            "claim_speaker": who, "stance": stance,
            "evidence": [{"id": uid, "t_start": t, "speaker_label": who,
                          "category": cat, "topic": topic,
                          "quote": stance + "，这是原话"}]}


def _two_sessions(db):
    """阿哲在 11-02 说「会被收紧」，11-16 改口「其实是门槛抬高」。"""
    a1 = _session(db, "S1", "2026-11-02T20:00:00", "第一场",
                  [("SPK_00", "阿哲", "per_a", "confirmed")],
                  [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 1)],
                  topics=("OPC 政策",))
    a2 = _session(db, "S2", "2026-11-16T20:00:00", "第二场",
                  [("SPK_00", "阿哲", "per_a", "confirmed")],
                  [_point("阿哲", "其实是备案门槛被抬高了", "OPC 政策", 2)],
                  topics=("OPC 政策",))
    return a1, a2


# ---------------------------------------------------------------- 抽取

def test_extract_reads_summary_points_with_person_identity(db):
    _two_sessions(db)
    atoms = claims.extract(db, "S1")
    assert len(atoms) == 1
    a = atoms[0]
    assert a["person_id"] == "per_a"
    assert a["who"] == "阿哲"
    assert a["topic"] == "OPC 政策"
    assert a["stance"] == "明年 OPC 会被平台收紧"
    assert a["claim_id"].startswith("clm_")


def test_topic_comes_from_label_vocabulary_not_point_title(db):
    """跨场要比的是标注词表里的主题，不是模型每次不同的标题文案。"""
    _session(db, "S_T", "2026-11-18T20:00:00", "标题不一样的场",
             [("SPK_00", "小鹿", "", "")],
             [_point("小鹿", "通勤超过一小时就该折算成房租", "租房与生活", 5)],
             topics=("租房与生活",))
    atoms = claims.extract(db, "S_T")
    assert atoms[0]["topic"] == "租房与生活"
    assert atoms[0]["topic"] != atoms[0]["claim_id"]     # 不是从标题乱截的


def test_topic_falls_back_when_only_generic_label(db):
    """证据全是「综合/未归类」时也要给一个主题，否则这条观点会从档案里消失。"""
    _session(db, "S_G", "2026-11-18T20:00:00", "杂项场",
             [("SPK_00", "大周", "", "")],
             [_point("大周", "先算清楚现金流能撑几个月", "综合/未归类", 7)])
    atoms = claims.extract(db, "S_G")
    assert atoms and atoms[0]["topic"] == "综合/未归类"


def test_question_evidence_is_marked_as_question_not_claim(db):
    _session(db, "S_Q", "2026-11-18T20:00:00", "问答场",
             [("SPK_00", "听众", "", "")],
             [_point("听众", "新号怎么起步", "职场跳槽与面试", 9,
                     cat="question_consult")])
    atoms = claims.extract(db, "S_Q")
    assert atoms and atoms[0]["kind"] == "question"


def test_unresolved_people_key_on_nickname_not_shared(db):
    """没认出声纹的人，不能因为同名就被并成一个人。"""
    _session(db, "S_N", "2026-11-18T20:00:00", "没认人",
             [("SPK_00", "聚类标签 09", "", "")],
             [_point("聚类标签 09", "先做一次模拟面试回听", "职场跳槽与面试", 3)])
    atoms = claims.extract(db, "S_N")
    assert atoms[0]["person_id"] == ""
    assert claims.person_key("", "聚类标签 09") == "name:聚类标签 09"
    assert claims.person_key("per_a", "") == "per_a"


def test_rebuild_is_idempotent_and_clears_when_summary_gone(db):
    _two_sessions(db)
    assert claims.rebuild_session(db, "S1") == 1
    assert claims.rebuild_session(db, "S1") == 1          # 重跑不叠加
    n = db.execute("SELECT COUNT(*) FROM claims WHERE session_id='S1'").fetchone()[0]
    assert n == 1
    # 总结被删掉（例如用户撤销）：档案也要跟着空，不能留旧观点
    db.execute("DELETE FROM summaries WHERE session_id='S1'")
    db.commit()
    assert claims.rebuild_session(db, "S1") == 0
    assert db.execute("SELECT COUNT(*) FROM claims WHERE session_id='S1'").fetchone()[0] == 0


# ---------------------------------------------------------------- 时间线

def test_profile_groups_by_topic_and_marks_revision(db):
    _two_sessions(db)
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")
    prof = claims.profile(db, person_id="per_a")
    assert prof["n_claims"] == 2 and prof["n_topics"] == 1
    g = prof["topics"][0]
    assert g["topic"] == "OPC 政策" and g["n"] == 2 and g["n_sessions"] == 2
    assert [it["stance"] for it in g["items"]] == ["明年 OPC 会被平台收紧",
                                                   "其实是备案门槛被抬高了"]
    assert g["items"][-1]["_changed"] is True
    assert g["revisions"] and g["revisions"][0]["from"] == "明年 OPC 会被平台收紧"
    assert g["latest"]["session_id"] == "S2"


def test_same_wording_is_not_a_revision(db):
    _session(db, "S1", "2026-11-02T20:00:00", "一",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 1)])
    _session(db, "S2", "2026-11-16T20:00:00", "二",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 2)])
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")
    g = claims.profile(db, person_id="per_a")["topics"][0]
    assert g["revisions"] == []
    assert g["items"][-1]["_same"] is True


def test_profile_separates_topics(db):
    _session(db, "S1", "2026-11-02T20:00:00", "多主题",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1, t=10),
              _point("阿哲", "选品要看复购", "选品逻辑", 2, t=100)],
             topics=("OPC 政策", "选品逻辑"))
    claims.rebuild_session(db, "S1")
    prof = claims.profile(db, person_id="per_a")
    assert [g["topic"] for g in prof["topics"]] == ["OPC 政策", "选品逻辑"]


# ---------------------------------------------------------------- 笔记页

def test_note_marks_cross_session_only_when_it_really_repeats(db):
    _two_sessions(db)
    for sid in ("S1", "S2"):
        claims.rebuild_session(db, sid)
    note = notes.build_note(db, "S2")
    points = [p for g in note["by_speaker"] for p in g["points"]]
    hit = [p for p in points if p["topic"] == "OPC 政策"]
    assert hit and hit[0]["cross_sessions"] == 2

    # 只有一场讲过的主题：不标跨场，避免每个要点都冒徽标
    _session(db, "S3", "2026-11-18T20:00:00", "只一场",
             [("SPK_00", "小鹿", "", "")],
             [_point("小鹿", "通勤折算成房租差价", "租房与生活", 4)],
             topics=("租房与生活",))
    claims.rebuild_session(db, "S3")
    one = [p for g in notes.build_note(db, "S3")["by_speaker"] for p in g["points"]]
    assert one and all(p["cross_sessions"] == 1 for p in one)


def test_note_index_writes_claims_on_rebuild(db):
    _two_sessions(db)
    notes.index_session(db, "S1")
    n = db.execute("SELECT COUNT(*) FROM claims WHERE session_id='S1'").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------- 身份变化

def test_merge_persons_moves_claims(db):
    _two_sessions(db)
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")
    store.upsert_person(db, "per_b", "阿哲（重复）")
    store.merge_persons(db, "per_a", "per_b")
    assert db.execute("SELECT COUNT(*) FROM claims WHERE person_id='per_b'"
                      ).fetchone()[0] == 2


def test_delete_person_keeps_claims_under_session_name(db):
    _two_sessions(db)
    claims.rebuild_session(db, "S1")
    store.upsert_person(db, "per_a", "阿哲")
    store.delete_person(db, "per_a")
    row = db.execute("SELECT person_id,who FROM claims WHERE session_id='S1'").fetchone()
    assert row["person_id"] == "" and row["who"] == "阿哲"


def test_refresh_person_names_updates_who_for_linked_rows(db):
    _two_sessions(db)
    claims.rebuild_session(db, "S1")
    db.execute("UPDATE claims SET who='旧昵称' WHERE session_id='S1'")
    db.commit()
    assert claims.refresh_person_names(db, "per_a", "阿哲") == 1
    assert db.execute("SELECT who FROM claims WHERE session_id='S1'").fetchone()[0] == "阿哲"


# ---------------------------------------------------------------- 检索

def test_qa_claim_channel_finds_person_topic_claims(db):
    from app import qa
    _two_sessions(db)
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")
    units = qa.retrieve(db, "阿哲对 OPC 政策怎么看", {})
    assert units
    assert any(u["kind"] == "claim" for u in units)
    first = units[0]
    assert "阿哲" in first["text"] or first["speaker"] == "阿哲"
