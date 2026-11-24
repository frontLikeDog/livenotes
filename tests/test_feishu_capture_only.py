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
