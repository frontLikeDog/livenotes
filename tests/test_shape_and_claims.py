# -*- coding: utf-8 -*-
"""场次形态、观点归属与「按人分组」的单元 / 轻量集成测试。

三个层级各测一遍：
1. ``app.shape``：只看行数据，0 token，形态与角色判定；
2. ``analysis``：理解段给要点补上 ``claim_speaker`` / ``stance``；本地抽取式已被拒绝；
3. ``notes.build_note``：圆桌按人分组、单人保持扁平、导出跟着换标题。
"""
from __future__ import annotations

import json

from app import analysis, export, notes, shape, store
from app.config import LIVE_MAX_SPEAKERS


def _rows(spec):
    """[(speaker_id, label, category, seconds, 句数), ...] -> 连续发言行。"""
    rows, t = [], 0.0
    for spk, label, cat, secs, n in spec:
        each = float(secs) / max(1, n)
        for _ in range(n):
            rows.append({"speaker_id": spk, "speaker_label": label,
                         "t_start": t, "t_end": t + each,
                         "category": cat, "overlap": 0})
            t += each + 1.0
    return rows, t


# ---------------------------------------------------------------- 形态

def test_roundtable_host_is_control_talk_not_longest_speaker():
    """语音厅里主持人话最少但控场最多：不能把话最多的人认成主持。"""
    spec = [("S0", "老王", "host_control", 60, 2)]          # 只暖场 + 点名
    spec += [("S%d" % (i + 1), "主讲%d" % i, "substantive", 360, 12)
             for i in range(5)]
    rows, dur = _rows(spec)
    out = shape.summarize(rows, duration_s=dur)
    assert out["kind"] == shape.SHAPE_ROUNDTABLE
    assert out["host_id"] == "S0"
    assert out["roles"]["S0"]["role"] == shape.ROLE_HOST
    assert out["roles"]["S0"]["share"] < 0.10
    assert out["roles"]["S1"]["role"] == shape.ROLE_SPEAKER
    assert out["label"] == "圆桌讨论"


def test_single_speaker_session_is_single_output():
    rows, dur = _rows([("S0", "阿哲", "substantive", 1200, 40),
                       ("S1", "听众", "question_consult", 60, 3)])
    out = shape.summarize(rows, duration_s=dur)
    assert out["kind"] == shape.SHAPE_SINGLE
    assert out["host_id"] == "S0"


def test_qa_room_with_many_short_askers_is_not_single():
    """一个答主 + 一群只问不答的人：时长上像单人，整理上必须算问答厅。"""
    spec = [("S0", "主持", "host_control", 20, 1), ("S1", "答主", "substantive", 1125, 40)]
    spec += [("Q%d" % i, "问者%d" % i, "question_consult", 63, 3) for i in range(5)]
    rows, dur = _rows(spec)
    out = shape.summarize(rows, duration_s=dur)
    assert out["kind"] == shape.SHAPE_QA
    assert out["host_id"] == "S1"            # 答主接手主持位，但不夺走内容
    assert out["roles"]["Q0"]["role"] == shape.ROLE_ASKER


def test_overlap_rows_are_ignored():
    rows, dur = _rows([("S0", "阿哲", "substantive", 600, 20)])
    rows.append({"speaker_id": "S9", "speaker_label": "串台",
                 "t_start": 10, "t_end": 500, "category": "substantive", "overlap": 1})
    out = shape.summarize(rows, duration_s=dur)
    assert "S9" not in out["roles"]
    assert out["kind"] == shape.SHAPE_SINGLE


def test_empty_session_and_speaker_cap_warning():
    out = shape.summarize([])
    assert out["kind"] == "" and out["roles"] == {}

    spec = [("S%02d" % i, "人%d" % i, "substantive", 120, 4)
            for i in range(LIVE_MAX_SPEAKERS)]
    rows, dur = _rows(spec)
    out = shape.summarize(rows, duration_s=dur)
    assert any("上限" in c for c in out["caveats"])


def test_session_shape_reads_from_db(db):
    store.insert_session(db, {"id": "S_SHAPE", "title": "语音厅", "started_at":
                              "2026-11-18T20:00:00", "duration_s": 900,
                              "source_mode": "tab"})
    store.insert_utterances(db, [
        {"session_id": "S_SHAPE", "speaker_id": "SPK_00", "speaker_label": "老王",
         "t_start": 0, "t_end": 10, "category": "host_control", "text": "欢迎"},
        {"session_id": "S_SHAPE", "speaker_id": "SPK_01", "speaker_label": "阿哲",
         "t_start": 11, "t_end": 300, "category": "substantive", "text": "我认为"},
    ])
    out = shape.session_shape(db, "S_SHAPE")
    assert out["kind"] == shape.SHAPE_SINGLE      # 1 人 290 秒，占比压过一切
    assert out["roles"]["SPK_01"]["role"] == shape.ROLE_HOST


# ---------------------------------------------------------------- 归属与主张

def test_claim_speaker_prefers_real_name_over_position_label():
    """同样的差距下，有人名的那位优先署名，位置标签只是占位。"""
    ev = [
        {"speaker_label": "嘉宾1", "t_start": 0, "t_end": 20},
        {"speaker_label": "阿哲", "t_start": 21, "t_end": 36},
    ]
    assert analysis._claim_speaker(ev) == "阿哲"


def test_claim_speaker_ignores_machine_cluster_labels():
    """「聚类标签 09」是机器占位，不能当成人名加权，也不能压过真名。"""
    assert not analysis._looks_like_name("聚类标签 09")
    assert not analysis._looks_like_name("嘉宾2")
    assert analysis._looks_like_name("阿哲")
    assert analysis._looks_like_name("主播·阿哲")        # 展示层拼的「位置 · 人名」
    assert analysis._looks_like_name("嘉宾2 · 小鹿（疑似）")
    assert not analysis._looks_like_name("主播")
    ev = [
        {"speaker_label": "聚类标签 09", "t_start": 0, "t_end": 40},
        {"speaker_label": "小鹿", "t_start": 41, "t_end": 26},
    ]
    assert analysis._claim_speaker(ev) == "小鹿"


def test_claim_speaker_accumulates_by_time():
    ev = [
        {"speaker_label": "阿哲", "t_start": 0, "t_end": 60},
        {"speaker_label": "小鹿", "t_start": 61, "t_end": 70},
    ]
    assert analysis._claim_speaker(ev) == "阿哲"


def test_stance_skips_fillers_and_takes_the_real_claim():
    assert analysis._stance("我认为明年会被收紧，因为备案变了。") == "明年会被收紧"
    assert analysis._stance("其实吧，还是要看复购。") == "还是要看复购"
    assert analysis._stance("我觉得吧，就是说，先做一次模拟面试录音回听，"
                            "很多人不是不会，是讲不清楚。") == "先做一次模拟面试录音回听"
    assert analysis._stance("") == ""


def test_stance_never_returns_a_bare_filler():
    for text in ("我觉得吧，先算清楚现金流能撑几个月，再决定要不要走。",
                 "就是说，账号限流先查违规通知，再看完播曲线。"):
        st = analysis._stance(text)
        assert st not in ("我觉得", "就是说", "我认为", "")
        assert len(st) >= 4


def test_attach_claims_uses_evidence_time_and_first_quote():
    """没有模型归属时：署名按证据时长累加，立场取第一条证据的主张句。"""
    pts = [{"evidence": [
        {"speaker_label": "阿哲", "t_start": 0, "t_end": 30,
         "quote": "我觉得吧，复购率比单场 GMV 更重要。"},
        {"speaker_label": "小鹿", "t_start": 31, "t_end": 55,
         "quote": "我补充一个案例，去年就吃过这个亏。"},
        {"speaker_label": "老王", "t_start": 56, "t_end": 62,
         "quote": "欢迎来到直播间"},
    ]}]
    analysis._attach_claims(pts)
    p = pts[0]
    assert p["claim_speaker"] == "阿哲"
    assert p["stance"] == "复购率比单场 GMV 更重要"


def test_attach_claims_keeps_model_given_values():
    pts = [{"evidence": [{"speaker_label": "阿哲", "t_start": 0, "t_end": 9,
                          "quote": "原话"}],
            "claim_speaker": "小鹿", "stance": "模型写的立场"}]
    analysis._attach_claims(pts)
    assert pts[0]["claim_speaker"] == "小鹿"
    assert pts[0]["stance"] == "模型写的立场"


# ---------------------------------------------------------------- 笔记聚合

def _seed(conn, sid, spec):
    """入库一场；返回插入的 utterance id 列表（顺序同 spec）。"""
    people = {}
    for spk, label, cat, text, secs in spec:
        people.setdefault(spk, label)
    store.insert_session(conn, {"id": sid, "title": "语音厅一场",
                                "started_at": "2026-11-18T20:00:00",
                                "duration_s": 3000, "source_mode": "tab"})
    t, rows = 0.0, []
    for spk, label, cat, text, secs in spec:
        rows.append({"session_id": sid, "speaker_id": spk, "speaker_label": label,
                     "t_start": t, "t_end": t + secs, "category": cat,
                     "value_tag": "normal", "text": text})
        t += secs + 1
    ids = store.insert_utterances(conn, rows)
    for spk, label in people.items():
        store.upsert_speaker(conn, sid, spk, label)
    return ids


def _note_roundtable(conn, sid="S_RT"):
    ids = _seed(conn, sid, [
        ("SPK_00", "老王", "host_control", "欢迎来到直播间，下一位上麦", 6),
        ("SPK_00", "老王", "host_control", "麦序排一下，有请阿哲", 6),
        ("SPK_01", "阿哲", "substantive", "我认为明年 OPC 会被收紧，因为备案门槛变了。", 40),
        ("SPK_02", "小鹿", "substantive", "我不同意，真正的变量是平台抽成，不是备案。", 38),
        ("SPK_03", "大周", "substantive", "我补充一个案例，去年就吃过这个亏。", 30),
    ])
    a_zhe, xiao_lu, da_zhou = ids[2], ids[3], ids[4]

    def _ev(uid, label, text):
        return {"id": uid, "t_start": 13, "text": text, "speaker_label": label}

    store.save_summary(conn, sid, "content", {}, "extractive", {
        "session_title": "一场圆桌",
        "overview": "",
        "topics": ["OPC 政策"],
        "points": [
            {"title": "OPC 政策", "gist": "两种判断",
             "t_from": 13, "t_to": 60, "speakers": ["阿哲", "小鹿"],
             "claim_speaker": "阿哲", "stance": "明年 OPC 会被收紧",
             "evidence": [_ev(a_zhe, "阿哲", "我认为明年 OPC 会被收紧"),
                          _ev(xiao_lu, "小鹿", "真正的变量是平台抽成")]},
            {"title": "选品逻辑", "gist": "别只看单场",
             "t_from": 61, "t_to": 100, "speakers": ["小鹿", "大周"],
             "claim_speaker": "小鹿", "stance": "变量是平台抽成",
             "evidence": [_ev(xiao_lu, "小鹿", "真正的变量是平台抽成"),
                          _ev(da_zhou, "大周", "去年就吃过这个亏")]},
            {"title": "关键案例", "gist": "抽成算法",
             "t_from": 101, "t_to": 130, "speakers": ["大周", "阿哲"],
             "claim_speaker": "大周", "stance": "去年吃过亏",
             "evidence": [_ev(da_zhou, "大周", "去年就吃过这个亏"),
                          _ev(a_zhe, "阿哲", "备案门槛变了")]},
        ],
    })
    return sid, (a_zhe, xiao_lu, da_zhou)


def test_build_note_groups_points_by_claim_speaker(db):
    sid, _ = _note_roundtable(db)
    out = notes.build_note(db, sid)
    assert out["ok"]
    assert out["shape"]["kind"] == shape.SHAPE_ROUNDTABLE
    groups = out["by_speaker"]
    # 主持没有要点，不出组；组间按角色与发言量排序
    assert [g["who"] for g in groups] == ["阿哲", "小鹿", "大周"]
    assert groups[0]["points"][0]["stance"] == "明年 OPC 会被收紧"
    assert all(g["role"] for g in groups)
    assert out["host"] == "老王"


def test_build_note_single_session_keeps_points_flat(db):
    ids = _seed(db, "S_ONE", [
        ("SPK_00", "阿哲", "substantive", "我认为复购率才是核心指标，因为获客越来越贵。", 600),
        ("SPK_01", "听众", "question_consult", "那新号怎么起步呢？", 20),
    ])
    store.save_summary(db, "S_ONE", "content", {}, "extractive", {
        "session_title": "单人分享", "overview": "", "topics": ["复购"],
        "points": [{"title": "复购", "gist": "核心指标", "t_from": 0, "t_to": 600,
                    "speakers": ["阿哲"], "claim_speaker": "阿哲",
                    "stance": "复购率才是核心指标",
                    "evidence": [{"id": ids[0], "t_start": 0,
                                  "text": "复购率才是核心指标", "speaker_label": "阿哲"}]}],
    })
    out = notes.build_note(db, "S_ONE")
    assert out["shape"]["kind"] == shape.SHAPE_SINGLE
    assert len(out["by_speaker"]) == 1
    assert out["points"][0]["claim_speaker"] == "阿哲"


def test_export_markdown_uses_people_sections_for_roundtable(db):
    sid, _ = _note_roundtable(db)
    text = export.to_note_md(db, sid)
    assert "## 观点" in text
    assert "### 阿哲（主讲）" in text
    assert "明年 OPC 会被收紧" in text
    assert "## 要点" not in text
    assert "形态：圆桌讨论" in text


def test_export_strips_fillers_but_keeps_meaning(db):
    sid, _ = _note_roundtable(db)
    text = export.to_note_md(db, sid)
    assert "我觉得吧" not in text
    assert "就是说" not in text
    # 去掉口头禅后正文还在，不是把整条要点删了
    assert "明年 OPC 会被收紧" in text
    assert export._strip_fillers("就是说，先看现金流速。") == "先看现金流速。"
    assert export._strip_fillers("我觉得吧，就是说，先算现金流。") == "先算现金流。"
    assert export._strip_fillers("我觉得") == "我觉得"      # 全被剥光就退回原文


def test_export_markdown_keeps_old_shape_for_single(db):
    ids = _seed(db, "S_ONE2", [("SPK_00", "阿哲", "substantive",
                                "我认为复购率才是核心指标，因为获客越来越贵。", 600)])
    store.save_summary(db, "S_ONE2", "content", {}, "extractive", {
        "session_title": "单人分享", "overview": "", "topics": [],
        "points": [{"title": "复购", "gist": "核心指标", "t_from": 0, "t_to": 600,
                    "speakers": ["阿哲"],
                    "evidence": [{"id": ids[0], "t_start": 0,
                                  "text": "复购率才是核心指标", "speaker_label": "阿哲"}]}],
    })
    text = export.to_note_md(db, "S_ONE2")
    assert "## 要点" in text
    assert "## 观点" not in text
    assert "形态：" not in text


# ---------------------------------------------------------------- 理解段落库

def test_make_summary_persists_shape_and_claims(db, monkeypatch):
    sid, (a_zhe, xiao_lu, _) = _note_roundtable(db)
    monkeypatch.setattr(analysis.settings, "resolve",
                        lambda: {"model": "test-model", "configured": True})

    def fake_llm_points(scope, usage=None, errors=None):
        if usage is not None:
            usage["model"] = "test-model"
        pts = [{"title": "OPC 政策", "gist": "两种判断", "t_from": 13, "t_to": 60,
                "speakers": ["阿哲", "小鹿"],
                "evidence": [
                    {"id": a_zhe, "t_start": 13, "text": "我认为明年 OPC 会被收紧",
                     "speaker_label": "阿哲"},
                    {"id": xiao_lu, "t_start": 54, "text": "真正的变量是平台抽成",
                     "speaker_label": "小鹿"},
                ]}]
        analysis._attach_claims(pts)
        return ("模型概述", pts, "一场圆桌", [], ["OPC 政策"], "阿哲")

    monkeypatch.setattr(analysis, "_llm_points", fake_llm_points)
    out = analysis.make_summary(db, sid, dict(analysis.WHOLE_FILTERS),
                                "content", "llm")
    assert out["engine_kind"] == "llm"
    assert out["engine_model"] == "test-model"
    assert out["title_kind"] == "llm"
    assert out["shape"]["kind"] == shape.SHAPE_ROUNDTABLE
    assert out["points"][0]["claim_speaker"] == "阿哲"
    payload = json.loads(db.execute(
        "SELECT payload_json FROM summaries WHERE session_id='S_RT' "
        "ORDER BY id DESC LIMIT 1").fetchone()["payload_json"])
    assert payload["engine_kind"] == "llm"
    assert payload["shape"]["kind"] == shape.SHAPE_ROUNDTABLE
    assert payload["points"][0]["claim_speaker"] == "阿哲"


def test_make_summary_rejects_local_extractive(db):
    _note_roundtable(db)
    try:
        analysis.make_summary(db, "S_RT", dict(analysis.WHOLE_FILTERS),
                              "content", "extractive")
    except RuntimeError as e:
        assert "本地总结已移除" in str(e)
    else:
        assert False, "本地抽取式必须被拒绝"


def test_finalize_refreshes_claim_after_relabel(db):
    sid, (a_zhe, _, _) = _note_roundtable(db)
    # 人工把「阿哲」那条证据改判给「小鹿」：归属必须跟着证据刷新，不能留旧名字
    db.execute("UPDATE utterances SET speaker_label='小鹿' WHERE session_id=? AND id=?",
               (sid, a_zhe))
    db.commit()
    out = analysis.finalize(db, sid)
    assert out["shape"]["kind"] == shape.SHAPE_ROUNDTABLE
    for p in out["points"]:
        labels = {e.get("speaker_label") for e in p.get("evidence") or []}
        active = labels & {"阿哲", "小鹿", "大周"}
        if p.get("claim_speaker"):
            assert p["claim_speaker"] in labels
        if active:
            assert p["claim_speaker"] in active
