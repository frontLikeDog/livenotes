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


# ---------------------------------------------------------------- 观点地图

def _map_session(db):
    """一场圆桌：一个主题下三个人，其中一人明确反对；另一个主题只有人问。"""
    from tests.test_shape_and_claims import _seed  # noqa: F401  (只借造数据思路)
    sid = "S_MAP"
    store.insert_session(db, {"id": sid, "title": "圆桌一场", "started_at":
                              "2026-11-16T20:00:00", "duration_s": 1800,
                              "source_mode": "tab"})
    spec = [("SPK_00", "老王", "host_control", "欢迎，下一位上麦"),
            ("SPK_01", "阿哲", "substantive", "我认为明年 OPC 会被收紧"),
            ("SPK_02", "小鹿", "substantive", "我不同意，变量是平台抽成"),
            ("SPK_03", "大周", "substantive", "我补充一个案例"),
            ("SPK_04", "路人", "question_consult", "抽成按什么口径算呢"),
            ("SPK_05", "路人二", "question_consult", "那账期怎么算")]
    rows, t = [], 0.0
    for spk, label, cat, text in spec:
        rows.append({"session_id": sid, "speaker_id": spk, "speaker_label": label,
                     "t_start": t, "t_end": t + 20, "category": cat, "text": text})
        t += 21
    ids = store.insert_utterances(db, rows)
    for spk, label, *_ in spec:
        store.upsert_speaker(db, sid, spk, label)

    def pt(who, stance, topic, uid, cat="substantive", t=0.0):
        return {"title": "话题 %s" % topic, "gist": "", "t_from": t, "t_to": t + 20,
                "speakers": [who], "claim_speaker": who, "stance": stance,
                "evidence": [{"id": uid, "t_start": t, "speaker_label": who,
                              "category": cat, "topic": topic, "quote": stance}]}

    store.save_summary(db, sid, "content", {}, "extractive", {
        "session_title": "圆桌一场", "overview": "", "topics": ["OPC 政策", "结算"],
        "points": [
            pt("阿哲", "明年 OPC 会被收紧", "OPC 政策", ids[1], t=21),
            pt("小鹿", "我不同意，真正的变量是平台抽成", "OPC 政策", ids[2], t=42),
            pt("大周", "去年有人吃过这个亏", "OPC 政策", ids[3], t=63),
            pt("路人", "抽成按什么口径算", "结算", ids[4],
               cat="question_consult", t=84),
            pt("路人二", "账期按什么算", "结算", ids[5],
               cat="question_consult", t=105),
        ]})
    return sid, ids


def test_opinion_map_groups_by_topic_not_by_person(db):
    sid, _ = _map_session(db)
    m = notes.build_note(db, sid)["opinion_map"]
    labels = [g["label"] for g in m["topics"]]
    assert "OPC 政策" in labels
    opc = next(g for g in m["topics"] if g["label"] == "OPC 政策")
    assert [e["who"] for e in opc["entries"]] == ["阿哲", "小鹿", "大周"]
    assert opc["n_people"] == 3


def test_opinion_map_marks_disagreement_only_on_explicit_words(db):
    sid, _ = _map_session(db)
    m = notes.build_note(db, sid)["opinion_map"]
    opc = next(g for g in m["topics"] if g["label"] == "OPC 政策")
    rows = {e["who"]: e for e in opc["entries"]}
    assert rows["小鹿"]["disagree"] and rows["小鹿"]["vs"] == "阿哲"
    # 只是补充案例、没有反对词的人不能被标成分歧
    assert not rows["大周"]["disagree"]
    assert opc["conflict"] and m["stats"]["conflicts"] == 1


def test_opinion_map_collects_unanswered_questions(db):
    sid, _ = _map_session(db)
    m = notes.build_note(db, sid)["opinion_map"]
    assert [q["text"] for q in m["open_questions"]] == ["抽成按什么口径算", "账期按什么算"]
    assert all(g["label"] != "结算" for g in m["topics"])   # 全是提问的主题不进地图
    assert m["stats"]["open_questions"] == 2


def test_opinion_map_puts_generic_topic_last(db):
    sid, ids = _map_session(db)
    summary = notes._summary(db, sid)
    summary["points"].append({
        "title": "闲聊", "gist": "", "t_from": 200, "t_to": 210,
        "claim_speaker": "大周", "stance": "随便说两句",
        "evidence": [{"id": ids[3], "t_start": 200, "speaker_label": "大周",
                      "category": "substantive", "topic": "综合", "quote": "随便说两句"}]})
    db.execute("UPDATE summaries SET payload_json=? WHERE session_id=?",
               (__import__("json").dumps(summary, ensure_ascii=False), sid))
    db.commit()
    m = notes.build_note(db, sid)["opinion_map"]
    assert m["topics"][-1]["label"] == claims.GENERIC_LABEL


def test_opinion_map_empty_without_points(db):
    _two_sessions(db)
    db.execute("UPDATE summaries SET payload_json='{}'")
    db.commit()
    assert notes.build_note(db, "S1")["opinion_map"] == {
        "topics": [], "open_questions": [], "stats": {}}


def test_single_speaker_session_map_is_one_entry(db):
    """单人场：地图只有一个人，页面据此仍走扁平要点（不画地图）。"""
    _two_sessions(db)
    m = notes.build_note(db, "S1")["opinion_map"]
    assert len(m["topics"]) == 1
    assert len(m["topics"][0]["entries"]) == 1
    assert m["topics"][0]["entries"][0]["who"] == "阿哲"
