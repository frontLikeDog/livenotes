# -*- coding: utf-8 -*-
"""场面：切段、规则可读层、模型判断的证据闸门。"""
from __future__ import annotations

import json

from helpers import add_rows, add_session, feats, utt_row

from app import highlight, scenes, store


def test_segment_splits_on_gap_and_topic():
    a = feats(0, score=2.0, chars=20, t_end=10, topic="A")
    b = feats(30, score=2.0, chars=20, t_end=40, topic="A")
    assert len(scenes.segment([a, b])) == 2          # 静默 20 秒

    c = feats(0, score=2.0, chars=20, t_end=25, topic="A")
    d = feats(25, score=2.0, chars=20, t_end=50, topic="B")
    assert len(scenes.segment([c, d])) == 2          # 话题转向且已够 20 秒


def test_scene_has_rule_hook_tag_and_quote():
    out = scenes.segment([
        feats(0, score=2.2, chars=30, num=2, t_end=10,
              text="第一名300元，第二名200元，第三名100元"),
    ])
    assert len(out) == 1
    s = out[0]
    assert s["tag"] == "干货"
    assert s["hook"] and s["hook"] != s["text"]
    assert s["quote"] == s["text"]
    assert s["n_utts"] == 1


def test_judge_rejects_quote_not_in_scene(monkeypatch):
    c = scenes.segment([
        feats(0, score=2.2, chars=20, t_end=10, text="成本要拆成三块来算"),
    ])[0]
    payload = {"picks": [{"id": 0, "hook": "他报了个价", "quote": "这句话原文里没有",
                          "tag": "干货", "why": "因为很重要"}]}
    monkeypatch.setattr(scenes.llm, "chat",
                        lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    assert scenes.judge([c]) == []


def test_judge_accepts_quote_from_scene(monkeypatch):
    c = scenes.segment([
        feats(0, score=2.2, chars=20, t_end=10, text="成本要拆成三块来算"),
    ])[0]
    payload = {"picks": [{"id": 0, "hook": "成本要拆开算", "quote": "成本要拆成三块来算",
                          "tag": "干货", "why": "把方法说清楚了"}]}
    monkeypatch.setattr(scenes.llm, "chat",
                        lambda *a, **k: json.dumps(payload, ensure_ascii=False))
    picks = scenes.judge([c])
    assert len(picks) == 1
    assert picks[0]["quote"] == "成本要拆成三块来算"
    assert picks[0]["tag"] == "干货"


def test_build_stores_scene_fields(db, monkeypatch):
    monkeypatch.setattr(highlight, "TH", 1.0)   # 小样本的 z 分数天然偏低
    add_session(db, "S1", duration_s=120)
    add_rows(db, "S1", [
        utt_row(0, "第一名300元，第二名200元，第三名100元", t=0),
        utt_row(1, "这个方案的关键是先把成本拆开", t=10),
        utt_row(2, "我建议先算一笔账再决定", t=20),
        utt_row(3, "所以预算要留出缓冲", t=30),
    ])
    out = scenes.build(db, "S1", limit=3, use_llm=False)
    assert out["n"] >= 1 and out["engine"] == "rules"
    hl = store.list_highlights(db, "S1")
    assert hl[0]["hook"] and hl[0]["tag"] and hl[0]["quote"]
    assert len(hl[0]["scene"]) >= 1
    assert hl[0]["scene"][0]["text"]


def test_session_events_is_zero_token_and_has_hook(db, monkeypatch):
    monkeypatch.setattr(highlight, "TH", 1.0)
    add_session(db, "S1", duration_s=120)
    add_rows(db, "S1", [
        utt_row(0, "第一名300元，第二名200元，第三名100元", t=0),
        utt_row(1, "这个方案的关键是先把成本拆开", t=10),
        utt_row(2, "我建议先算一笔账再决定", t=20),
        utt_row(3, "所以预算要留出缓冲", t=30),
    ])
    ev = scenes.session_events(db, "S1", limit=1)
    assert len(ev) == 1
    assert ev[0]["hook"] and ev[0]["tag"] == "干货"
