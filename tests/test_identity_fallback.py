# -*- coding: utf-8 -*-
"""零标注兜底：没实名时，分享卡/观点地图/圆桌仍然有「谁」。

不测「猜名字」——只测展示层：
1. 有直播间昵称就用昵称，标来源；
2. 只有聚类/位置标签时，用按首次发言排序的「本场第 N 位上麦者」；
3. 兜底名不进 ``claims.who``，所以不会把两个陌生人跨场合并。
"""
from __future__ import annotations

from app import cards, claims, identity, notes, roundtable, store


def _pt(who, stance, topic, uid, speaker_id, cat="substantive", t=0.0):
    """一条总结要点：claim_speaker 故意留空，模拟「没来得及标注」。"""
    return {
        "title": "话题：%s" % topic, "gist": "", "t_from": t, "t_to": t + 18,
        "speakers": [who], "claim_speaker": "",
        "stance": stance,
        "evidence": [{"id": uid, "t_start": t, "t_end": t + 18,
                      "speaker_id": speaker_id, "speaker_label": who,
                      "category": cat, "topic": topic, "quote": stance,
                      "text": stance}],
    }


def _session(db):
    sid = "S_FALLBACK"
    store.insert_session(db, {"id": sid, "title": "零标注一场",
                              "started_at": "2026-11-19T20:00:00",
                              "duration_s": 120, "source_mode": "tab"})
    spec = [
        ("SPK_00", "主播", 0.0, 15.0, "欢迎来到直播间"),
        ("SPK_01", "小嘎皮", 21.0, 36.0, "账期一到三个月吧"),
        ("SPK_02", "聚类标签 09", 42.0, 58.0, "我补充一个数据"),
        ("SPK_03", "嘉宾1", 61.0, 78.0, "抽成按什么口径算"),
    ]
    rows = [{"session_id": sid, "speaker_id": spk, "speaker_label": label,
             "t_start": t0, "t_end": t1, "text": text,
             "category": ("question_consult" if i == 3 else "substantive"),
             "topic": ("抽成" if i == 3 else "结算")}
            for i, (spk, label, t0, t1, text) in enumerate(spec)]
    ids = store.insert_utterances(db, rows)
    for spk, label, *_ in spec:
        store.upsert_speaker(db, sid, spk, label)
    store.save_summary(db, sid, "content", {}, "extractive", {
        "session_title": "零标注一场", "overview": "",
        "topics": ["结算", "抽成"],
        "points": [
            _pt("小嘎皮", "账期一到三个月左右", "结算", ids[1], "SPK_01", t=21),
            _pt("聚类标签 09", "平台抽成是最大变量", "结算", ids[2], "SPK_02", t=42),
            _pt("嘉宾1", "抽成按什么口径算", "抽成", ids[3], "SPK_03",
                cat="question_consult", t=61),
        ],
    })
    claims.rebuild_session(db, sid)
    return sid


def test_nickname_with_dot_is_not_truncated():
    # 「·」是昵称的一部分，不是角色分隔符；截成最后一段会让两个人合并。
    assert identity.clean_name("阿哲·每晚8点") == ("阿哲·每晚8点", "nickname")
    assert identity.clean_name("主播·阿哲·每晚8点") == ("阿哲·每晚8点", "nickname")
    assert identity.clean_name("嘉宾 · 2") == ("", "session")
    assert identity.clean_name("主播 · 阿哲") == ("阿哲", "nickname")


def test_bare_number_is_a_session_label_not_a_person():
    # LLM 有时把麦位编号直接当 claim_speaker；裸数字不能进跨场署名。
    assert identity.clean_name("3") == ("", "session")
    assert identity.clean_name("０３") == ("", "session")
    assert identity.clean_name("3号") == ("3号", "nickname")


def test_claim_person_uses_matching_evidence_not_first(db):
    """嘉宾的观点不能因为证据先引用主播就记到主播名下。"""
    sid = "S_OWNER"
    store.insert_session(db, {"id": sid, "title": "归属测试",
                              "started_at": "2026-11-19T22:00:00",
                              "duration_s": 60, "source_mode": "tab"})
    ids = store.insert_utterances(db, [
        {"session_id": sid, "speaker_id": "SPK_00", "speaker_label": "主播",
         "t_start": 0, "t_end": 5, "text": "你先说"},
        {"session_id": sid, "speaker_id": "SPK_03", "speaker_label": "嘉宾1",
         "t_start": 5, "t_end": 10, "text": "抽成按流水算"},
    ])
    store.upsert_speaker(db, sid, "SPK_00", "主播")
    store.upsert_speaker(db, sid, "SPK_03", "嘉宾1")
    store.upsert_person(db, "per_host", "阿哲")
    store.upsert_person(db, "per_guest", "小林")
    store.set_speaker_person(db, sid, "SPK_00", "per_host", 0.9, 0.1, "confirmed")
    store.set_speaker_person(db, sid, "SPK_03", "per_guest", 0.9, 0.1, "confirmed")
    store.save_summary(db, sid, "content", {}, "extractive", {
        "session_title": "归属测试", "overview": "", "topics": ["抽成"],
        "points": [{
            "title": "抽成", "claim_speaker": "嘉宾1",
            "stance": "抽成按流水算", "t_from": 5, "t_to": 10,
            "evidence": [
                {"id": ids[0], "t_start": 0, "t_end": 5,
                 "speaker_id": "SPK_00", "speaker_label": "主播",
                 "quote": "你先说", "text": "你先说"},
                {"id": ids[1], "t_start": 5, "t_end": 10,
                 "speaker_id": "SPK_03", "speaker_label": "嘉宾1",
                 "quote": "抽成按流水算", "text": "抽成按流水算"},
            ],
        }],
    })
    claims.rebuild_session(db, sid)
    row = claims.rows_for(db, session_id=sid)[0]
    assert row["person_id"] == "per_guest"
    assert identity.claim_ref(row, identity.roster(db, sid))["who"] == "小林"


def test_role_prefixed_nickname_is_not_a_position_label():
    # 角色词后面跟编号/单独出现才是占位；「主播小王」是真实昵称。
    assert identity.is_positional("主播") is True
    assert identity.is_positional("嘉宾2") is True
    assert identity.is_positional("说话人 3") is True
    assert identity.is_positional("运营") is True
    assert identity.is_positional("3") is True
    assert identity.is_positional("０３") is True
    assert identity.is_positional("主播小王") is False
    assert identity.is_positional("主持人小美") is False
    assert identity.clean_name("主播小王") == ("主播小王", "nickname")
    assert identity.clean_name("主播 · 阿哲") == ("阿哲", "nickname")
    assert identity.clean_name("运营") == ("", "session")


def test_roster_uses_nickname_role_then_session_number(db):
    sid = _session(db)
    roster = identity.roster(db, sid)
    by_id = roster["by_id"]
    assert by_id["SPK_00"]["who"] == "主播"
    assert by_id["SPK_00"]["source"] == "role"
    assert by_id["SPK_01"]["who"] == "小嘎皮"
    assert by_id["SPK_01"]["source"] == "nickname"
    assert by_id["SPK_02"]["who"] == "本场第 3 位上麦者"
    assert by_id["SPK_02"]["source"] == "session"
    assert by_id["SPK_03"]["who"] == "本场第 4 位上麦者"
    assert [e["ordinal"] for e in roster["entries"]] == [1, 2, 3, 4]


def test_confirmed_person_wins_over_nickname(db):
    sid = _session(db)
    store.upsert_person(db, "per_a", "阿哲")
    store.set_speaker_person(db, sid, "SPK_01", "per_a", 0.9, 0.1, "confirmed")
    entry = identity.roster(db, sid)["by_id"]["SPK_01"]
    assert entry["who"] == "阿哲" and entry["source"] == "confirmed"


def test_claim_fallback_does_not_enter_claims_who(db):
    sid = _session(db)
    rows = claims.rows_for(db, session_id=sid)
    assert rows and all(str(r["who"]) == "" for r in rows)
    roster = identity.roster(db, sid)
    labels = [identity.claim_ref(r, roster)["who"] for r in rows]
    assert "小嘎皮" in labels
    assert "本场第 3 位上麦者" in labels


def test_cards_expose_display_label_and_source(db):
    sid = _session(db)
    rows = cards.session_claims(db, sid)
    assert rows[0]["who_label"] == "小嘎皮"
    assert rows[0]["who_source"] == "nickname"
    assert rows[1]["who_label"] == "本场第 3 位上麦者"
    assert rows[1]["who_source"] == "session"

    claim = claims.get(db, rows[1]["claim_id"])
    ref = identity.claim_ref(claim, identity.roster(db, sid))
    page = cards.claim_page(claim, ref)
    assert "本场第 3 位上麦者" in page
    assert "本场编号" in page
    assert "聚类标签 09" not in page


def test_notes_groups_unknown_speakers_separately(db):
    sid = _session(db)
    note = notes.build_note(db, sid)
    who = [g["who"] for g in note["by_speaker"]]
    assert "本场第 3 位上麦者" in who
    assert "本场第 4 位上麦者" in who
    assert "本场共同讨论" not in who
    # 落库署名仍是空，跨场历史不会把两个陌生同名合并
    assert all(p["claim_speaker"] == "" for p in note["points"])
    assert all(p["claim_speaker_label"] for p in note["points"])


def test_opinion_map_uses_fallback_names(db):
    sid = _session(db)
    m = notes.build_note(db, sid)["opinion_map"]
    settlement = next(g for g in m["topics"] if g["label"] == "结算")
    assert {e["who"] for e in settlement["entries"]} == {
        "小嘎皮", "本场第 3 位上麦者"}
    assert settlement["entries"][0]["who_source"] == "nickname"
    assert [q["who"] for q in m["open_questions"]] == ["本场第 4 位上麦者"]


def test_roundtable_uses_fallback_names_and_source(db):
    sid = _session(db)
    data = roundtable.build(db, sid)
    names = {p["name"] for p in data["participants"]}
    assert names == {"主播", "小嘎皮", "本场第 3 位上麦者", "本场第 4 位上麦者"}
    settlement = next(g for g in data["topics"] if g["topic"] == "结算")
    assert {e["who"] for e in settlement["entries"]} == {
        "小嘎皮", "本场第 3 位上麦者"}
    assert [q["who"] for q in data["open_questions"]] == ["本场第 4 位上麦者"]
    md = roundtable.markdown(data)
    assert "本场第 3 位上麦者" in md
    assert "本场编号" in md
