# -*- coding: utf-8 -*-
"""云端仅采集节点的飞书回执：状态措辞、重试命令、无公网页面时的回听提示。"""
from __future__ import annotations

import tools.feishu_bot as fb


def _room(state="live", sid="S1"):
    return {"id": "rm1", "seq": 1, "state": state, "name": "语音厅",
            "session_id": sid, "capture_engine": "direct"}


def test_status_capture_only_shows_audible_not_zero_sentences():
    sess = {"capture_only": True, "elapsed": 125.0, "audible_s": 120.0,
            "n_utterances": 0, "last_chunk_ago_s": 0.2, "running": True}
    out = fb._status_one(_room(), sess, None)
    assert "仅录制" in out
    assert "有声 2:00" in out
    assert "0 句" not in out
    assert "还没有字幕" not in out


def test_status_stopped_capture_only_uses_share_hint(monkeypatch):
    monkeypatch.setattr(fb, "CAPTURE_ONLY", True)
    room = _room(state="stopped")
    room["has_recording"] = True
    out = fb._status_one(room, None, None)
    assert "/分享 CH01 90秒" in out
    assert "127.0.0.1" not in out


def test_console_text_capture_only_has_audible():
    data = {"rooms": [{
        "room_id": "rm1", "ch": "CH01", "name": "语音厅", "room_state": "live",
        "session_id": "S1", "elapsed": 125, "audible_s": 120,
        "capture_only": True, "n_utterances": 0,
    }]}
    out = fb._console_text(data)
    assert "仅录制" in out and "有声 2:00" in out
    assert "0 句" not in out


def test_capture_only_after_offline_transcript_shows_count():
    r = {"elapsed": 125, "audible_s": 120, "n_utterances": 3,
         "capture_only": True}
    assert "补转写 3 句" in fb._capture_bits(r)
    assert "仅录制" not in fb._capture_bits(r)

    sess = {"capture_only": True, "elapsed": 125.0, "audible_s": 120.0,
            "n_utterances": 3, "last_line": "这是补出来的一句",
            "last_chunk_ago_s": 0.2, "running": True}
    out = fb._status_one(_room(), sess, None)
    assert "补转写 3 句" in out
    assert "最后一句 这是补出来的一句" in out


def test_console_card_shows_summary_after_offline_transcript():
    data = {"rooms": [{
        "room_id": "rm1", "ch": "CH01", "name": "语音厅", "room_state": "live",
        "session_id": "S1", "elapsed": 125, "audible_s": 120,
        "capture_only": True, "n_utterances": 3,
    }]}
    card = fb._console_card(data)
    text = str(card)
    assert "brief_summary" in text and "brief_topics" in text
    assert "补转写 3 句" in text


def test_public_base_rejects_private_ip(monkeypatch):
    monkeypatch.setattr(fb.lan, "base_url",
                        lambda: "http://172.24.4.219:46535")
    assert fb._public_base() == ""
    monkeypatch.setattr(fb.lan, "base_url",
                        lambda: "http://39.106.210.20:46535")
    assert fb._public_base() == "http://39.106.210.20:46535"


def test_summary_hides_private_reader_links(monkeypatch):
    monkeypatch.setattr(fb, "CAPTURE_ONLY", True)
    monkeypatch.setattr(fb, "_public_base", lambda: "")
    data = {
        "room": {"ch": "CH01"},
        "coverage": {"t_to": 60, "included": 3},
        "overview": "概述",
        "points": [{
            "title": "话题", "gist": "要点", "t_from": 1, "t_to": 2,
            "evidence": [{"quote": "原话",
                          "reader_url": "http://172.24.4.219:46535/live?t=1"}],
        }],
    }
    text = fb._summary_text(data)
    assert "172.24.4.219" not in text
    assert "原话" in text
    assert "/分享 CH01 90秒" in text


def test_cmd_retry_requeues_stopped_room(monkeypatch):
    calls = []

    def fake_api(path, payload=None, **kw):
        calls.append((path, payload))
        return {}

    monkeypatch.setattr(fb, "_rooms", lambda: [_room(state="stopped")])
    monkeypatch.setattr(fb, "_api", fake_api)
    out = fb.cmd_retry("CH01")
    assert calls == [("/api/rooms/state",
                      {"id": "rm1", "state": "pending", "error": ""})]
    assert "已重新排队" in out


def test_cmd_retry_refuses_running_room(monkeypatch):
    calls = []
    monkeypatch.setattr(fb, "_rooms", lambda: [_room(state="live")])
    monkeypatch.setattr(fb, "_api",
                        lambda *a, **k: calls.append((a, k)) or {})
    out = fb.cmd_retry("CH01")
    assert "已经在跑" in out
    assert calls == []
