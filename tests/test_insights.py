# -*- coding: utf-8 -*-
"""主动提醒：默认关、每场最多一条、只说事实（docs/41 §4.5）。

三种提醒只在「用户明确打开」后产生；开关粒度到单类。这里不测文案是否
好听，只守住产品边界：关着不打扰、一场不连弹、不判断谁对谁错。
"""
from __future__ import annotations

from app import claims, insights, notify, settings
from tests.test_claims_profile import _point, _session


def _on():
    settings.set_prefs({"notify_enabled": True, "insight_enabled": True,
                        "notify_muted": []})


def _two_sessions(db):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被平台收紧", "OPC 政策", 1)],
             topics=("OPC 政策",))
    _session(db, "S2", "2026-11-16T20:00:00", "第二场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "其实是备案门槛被抬高了", "OPC 政策", 2)],
             topics=("OPC 政策",))
    claims.rebuild_session(db, "S1")
    claims.rebuild_session(db, "S2")


# ---------------------------------------------------------------- 默认关

def test_insights_are_off_by_default_and_silent(db):
    _two_sessions(db)
    assert insights.after_finalize(db, "S2") is None
    assert notify.feed()["items"] == []


def test_insights_need_their_own_switch(db):
    _on()
    settings.set_prefs({"insight_enabled": False})
    _two_sessions(db)
    assert insights.after_finalize(db, "S2") is None
    assert notify.emit("insight_reversal", "观点有变化", "测试") is None


def test_settings_view_exposes_insights_switch(db):
    view = notify.settings_view()
    assert view["insights"] is False
    assert {k["kind"] for k in view["kinds"]} >= set(notify.INSIGHT_KINDS)
    out = notify.update_settings({"notify_insights": True})
    assert out["insights"] is True
    assert notify.enabled("insight_reversal") is True


# ---------------------------------------------------------------- 观点反转

def test_reversal_is_emitted_once_per_session(db):
    _on()
    _two_sessions(db)
    item = insights.after_finalize(db, "S2")
    assert item and item["kind"] == "insight_reversal"
    assert item["title"] == "观点有变化"
    assert "OPC 政策" in item["body"]
    assert item["action"]["view"] == "note"
    assert item["action"]["session_id"] == "S2"
    # 同一场再收尾一次：不再补第二条（哪怕还有别的规则命中）
    assert insights.after_finalize(db, "S2") is None
    assert len(notify.feed()["items"]) == 1


def test_reversal_does_not_judge_right_or_wrong(db):
    _on()
    _two_sessions(db)
    item = insights.after_finalize(db, "S2")
    for word in ("错了", "正确", "自相矛盾", "打脸"):
        assert word not in item["body"]


# ---------------------------------------------------------------- 老问题

def test_old_question_gets_an_answer_alert(db):
    _on()
    _session(db, "S_Q", "2026-11-01T20:00:00", "提问场",
             [("SPK_00", "路人", "", "")],
             [_point("路人", "抽成按什么口径算", "结算", 1,
                     cat="question_consult")],
             topics=("结算",))
    _session(db, "S_A", "2026-11-20T20:00:00", "回答场",
             [("SPK_00", "小鹿", "per_lu", "confirmed")],
             [_point("小鹿", "抽成按阶梯口径算，每月结算", "结算", 2)],
             topics=("结算",))
    claims.rebuild_session(db, "S_Q")
    claims.rebuild_session(db, "S_A")

    item = insights.after_finalize(db, "S_A")
    assert item and item["kind"] == "insight_answer"
    assert item["title"] == "老问题有答案了"
    assert "抽成按什么口径算" in item["body"]
    assert "小鹿" in item["body"]


# ---------------------------------------------------------------- 常客上麦

def _person_with_claims(db, pid, name, n=5):
    for i in range(n):
        sid = "P%d" % i
        _session(db, sid, "2026-10-%02dT20:00:00" % (i + 1), "旧场%d" % i,
                 [("SPK_00", name, pid, "confirmed")],
                 [_point(name, "第 %d 条观点" % i, "OPC 政策", i + 1)],
                 topics=("OPC 政策",))
        claims.rebuild_session(db, sid)
    _session(db, "S_NEW", "2026-11-19T20:00:00", "新场",
             [("SPK_00", name, pid, "confirmed")], [], topics=())


def test_frequent_speaker_alert_needs_enough_history(db):
    _on()
    _person_with_claims(db, "per_a", "阿哲", n=5)
    item = insights.speaker_appeared(db, "S_NEW", "per_a", "阿哲")
    assert item and item["kind"] == "insight_speaker"
    assert "5 条观点" in item["body"]
    assert item["action"] == {"view": "people", "person_id": "per_a"}

    # 同一场最多一条：换个人也不再弹。
    _person_with_claims(db, "per_b", "小鹿", n=5)
    assert insights.speaker_appeared(db, "S_NEW", "per_b", "小鹿") is None


def test_frequent_speaker_needs_five_claims(db):
    _on()
    _person_with_claims(db, "per_c", "大周", n=4)
    assert insights.speaker_appeared(db, "S_NEW", "per_c", "大周") is None
