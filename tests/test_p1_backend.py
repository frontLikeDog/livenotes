# -*- coding: utf-8 -*-
"""P1 后端单元与轻量集成：二维码、偏好、关注、笔记库、书签、问答、周报、导出。"""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from app import (config, export, follows, notes, notify, qa, qr, settings,
                 store, weekly)


def _session(conn, sid: str, title: str, started_at: str,
             duration_s: float = 3600) -> None:
    store.insert_session(conn, {
        "id": sid, "title": title, "started_at": started_at,
        "duration_s": duration_s, "source_mode": "browser",
    })


def _summary(conn, sid: str, payload: dict) -> int:
    return store.save_summary(conn, sid, "content", {}, "local", payload)


def _seed_note(conn, sid: str = "S1", started_at: str = "2026-11-10T20:00:00"):
    _session(conn, sid, "选品夜谈", started_at, 5400)
    _summary(conn, sid, {
        "session_title": "选品夜谈",
        "host_speaker": "SPK_01",
        "overview": "这场讲了选品要看复购率，而不是单场 GMV。",
        "topics": ["选品", "复购"],
        "points": [{
            "title": "先看复购率",
            "gist": "复购率决定长期生意。",
            "t_from": 120,
            "t_to": 180,
            "speakers": ["阿哲"],
            "evidence": [{"id": 1, "t_start": 120, "text": "复购率比单场 GMV 更重要。",
                          "speaker_label": "阿哲"}],
        }],
    })
    store.insert_utterances(conn, [
        {"session_id": sid, "t_start": 120, "t_end": 128,
         "speaker_id": "SPK_01", "speaker_label": "阿哲",
         "text": "复购率比单场 GMV 更重要。"},
        {"session_id": sid, "t_start": 300, "t_end": 310,
         "speaker_id": "SPK_01", "speaker_label": "阿哲",
         "text": "三字词复购率也要能搜到。"},
    ])
    store.upsert_note(conn, sid, state="ready", progress=100,
                      generated_at="2026-11-10T22:00:00")
    return sid


# ---------------------------------------------------------------- 二维码

def test_qr_format_bits_match_iso_table():
    expected = [0x5412, 0x5125, 0x5E7C, 0x5B4B,
                0x45F9, 0x40CE, 0x4F97, 0x4AA0]
    for mask, bits in enumerate(expected):
        assert qr._bch(mask, 0x537, 10, 0x5412) == bits


def test_qr_matrix_structure_and_version():
    out = qr.text_rows("https://192.168.1.8:8765/note?session_id=LIVE_1")
    assert out["version"] in range(1, 11)
    assert out["size"] == 17 + 4 * out["version"]
    assert len(out["rows"]) == out["size"]
    assert all(len(row) == out["size"] for row in out["rows"])
    last = out["size"] - 1
    # 三个定位角：外圈黑、内侧白、中心黑
    assert out["rows"][0][0] == "1"
    assert out["rows"][0][last] == "1"
    assert out["rows"][last][0] == "1"
    assert out["rows"][1][1] == "0"
    assert out["rows"][3][3] == "1"


def test_qr_capacity_and_version_pick():
    assert qr.capacity_bytes(1) == 14
    assert qr.capacity_bytes(10) == 213
    assert qr._pick_version(14) == 1
    assert qr._pick_version(15) == 2
    # 中文按 UTF-8 字节算：5 个字 = 15 字节，v1 放不下
    assert qr.make("中文中文中")["version"] == 2
    with pytest.raises(ValueError):
        qr.make("x" * (qr.capacity_bytes(10) + 1))


def test_qr_deterministic_and_every_mask_valid():
    assert qr.text_rows("复购率")["rows"] == qr.text_rows("复购率")["rows"]
    for mask in range(8):
        out = qr.make("LiveNotes", mask=mask)
        assert out["mask"] == mask
        assert len(out["rows"]) == out["size"]


# ---------------------------------------------------------------- 设置 / 通知

def test_p1_prefs_defaults_and_bounds():
    p = settings.prefs()
    assert p["perf_mode"] == "auto"
    assert p["follow_interval_s"] == 120
    assert p["weekly_enabled"] is True
    assert p["weekly_time"] == "mon-09"
    assert p["qa_enabled"] is True
    assert p["qa_suggest_enabled"] is True
    assert p["auto_record_enabled"] is False
    assert p["max_rooms"] == 5
    assert p["low_power_pause"] is True

    settings.set_prefs({"perf_mode": "turbo", "follow_interval_s": 5,
                        "weekly_enabled": False, "qa_enabled": "off",
                        "qa_suggest_enabled": "off", "auto_record_enabled": "on",
                        "weekly_time": "tue-10", "max_rooms": 7,
                        "low_power_pause": "off"})
    p = settings.prefs()
    assert p["perf_mode"] == "auto"          # 非法枚举不落盘
    assert p["follow_interval_s"] == 60      # 下限
    assert p["weekly_enabled"] is False
    assert p["weekly_time"] == "mon-09"      # 非法时间不落盘
    assert p["qa_enabled"] is False
    assert p["qa_suggest_enabled"] is False
    assert p["auto_record_enabled"] is True
    assert p["max_rooms"] == 5               # 只接受 3 / 5 / 8
    assert p["low_power_pause"] is False

    settings.set_prefs({"follow_interval_s": 999})
    assert settings.prefs()["follow_interval_s"] == 600
    settings.set_prefs({"weekly_time": "sun-20", "max_rooms": 8})
    assert settings.prefs()["weekly_time"] == "sun-20"
    assert settings.prefs()["max_rooms"] == 8


def test_notify_p1_kinds_and_groups():
    view = notify.settings_view()
    groups = {k["kind"]: k["group"] for k in view["kinds"]}
    for kind in ("follow_live", "auto_record", "auto_record_failed"):
        assert groups[kind] == "关注"
    assert groups["weekly_ready"] == "周报"
    assert len(view["kinds"]) >= 13
    assert view["detail"] is True

    notify.update_settings({"notify_detail": False})
    assert notify.settings_view()["detail"] is False


# ---------------------------------------------------------------- 关注

def test_follows_state_machine_episode_and_failures(db):
    row = store.add_follow(db, "douyin", "room1",
                           "https://live.douyin.com/room1", "阿哲")
    assert row is not None
    fid = int(row["id"])
    store.update_follow(db, fid, notify=False, auto_record=False)
    row = store.get_follow(db, fid)

    row = follows.apply_probe(row, {"state": "offline", "name": "阿哲"}, active=[])
    assert row["live_state"] == "offline"

    live = {"state": "live", "name": "阿哲", "title": "今晚选品",
            "live_started_at": "2026-11-17T20:00:00"}
    row = follows.apply_probe(row, live, active=[])
    assert row["live_state"] == "live"
    first = row["live_episode"]
    assert first == "2026-11-17T20:00:00"

    row = follows.apply_probe(row, live, active=[])
    assert row["live_episode"] == first   # 同一场不重复提醒

    for _ in range(3):
        row = follows.apply_probe(row, {"state": "unknown", "error": "网络错误"},
                                  active=[])
    assert row["live_state"] == "unknown"
    assert row["live_episode"] == first   # 短暂失败后恢复仍是同一场

    row = follows.apply_probe(row, live, active=[])
    assert row["live_episode"] == first

    row = follows.apply_probe(row, {"state": "offline"}, active=[])
    assert row["live_episode"] == ""
    row = follows.apply_probe(
        row, {"state": "live", "live_started_at": "2026-11-18T20:00:00"},
        active=[])
    assert row["live_episode"] == "2026-11-18T20:00:00"


def test_follows_interval_uses_prefs_and_lite_floor(monkeypatch):
    settings.set_prefs({"follow_interval_s": 60})
    monkeypatch.setattr(config, "perf", lambda: {"mode": "full"})
    assert follows.interval_s() == 60
    monkeypatch.setattr(config, "perf", lambda: {"mode": "lite"})
    assert follows.interval_s() == 180    # 低配不低于 180
    settings.set_prefs({"follow_interval_s": 240})
    assert follows.interval_s() == 240
    settings.set_prefs({"follow_interval_s": 600})
    assert follows.interval_s() == 600


def test_follows_max_rooms_uses_pref(db):
    settings.set_prefs({"max_rooms": 8})
    assert follows.max_rooms() == 8
    active = [{"state": "live"} for _ in range(8)]
    assert follows._auto_capacity_left(active) is False
    assert follows._auto_capacity_left(active[:7]) is True


def test_auto_record_master_switch_off_falls_back_to_notice(db, monkeypatch):
    row = store.add_follow(db, "douyin", "room2",
                           "https://live.douyin.com/room2", "小北")
    fid = int(row["id"])
    store.update_follow(db, fid, notify=True, auto_record=True)
    row = store.get_follow(db, fid)
    settings.set_prefs({"auto_record_enabled": False})

    started, notified = [], []
    monkeypatch.setattr(follows, "_start_auto_record",
                        lambda *a, **k: started.append(1) or {"ok": True})
    monkeypatch.setattr(follows.notify, "follow_live",
                        lambda *a, **k: notified.append(a))
    follows.apply_probe(row, {"state": "live", "name": "小北",
                              "live_started_at": "2026-11-17T20:00:00"},
                        active=[])
    assert started == []
    assert notified

    settings.set_prefs({"auto_record_enabled": True})
    follows.apply_probe(store.get_follow(db, fid),
                        {"state": "offline"}, active=[])
    follows.apply_probe(store.get_follow(db, fid),
                        {"state": "live", "name": "小北",
                         "live_started_at": "2026-11-18T20:00:00"}, active=[])
    assert started == [1]


# ---------------------------------------------------------------- 笔记库

def test_library_cards_paginate_without_duplicates(db):
    _seed_note(db, "S1", "2026-11-01T20:00:00")
    _seed_note(db, "S2", "2026-11-02T20:00:00")
    _seed_note(db, "S3", "2026-11-03T20:00:00")

    first = store.library_cards(db, limit=2, sort="time")
    assert [x["session_id"] for x in first["items"]] == ["S3", "S2"]
    assert first["next_cursor"]
    assert first["counts"]["all"] == 3
    assert first["counts"]["ready"] == 3

    second = store.library_cards(db, limit=2, sort="time",
                                 cursor=first["next_cursor"])
    ids = [x["session_id"] for x in second["items"]]
    assert ids == ["S1"]
    assert not set(ids) & {x["session_id"] for x in first["items"]}
    assert second["next_cursor"] == ""

    ready = store.library_cards(db, filter="ready")
    assert len(ready["items"]) == 3
    assert all(x["note_state"] == "ready" for x in ready["items"])
    assert ready["items"][0]["summary"].startswith("这场讲了")


def test_note_units_search_two_and_three_chars(db):
    _seed_note(db)
    assert store.replace_note_units(db, "S1", [
        {"kind": "point", "text": "复购率决定长期生意", "t": 120, "weight": 1.2},
        {"kind": "point", "text": "单场 GMV 只是结果", "t": 200, "weight": 1.0},
    ]) == 2

    two = store.search_note_units(db, ["复购"], limit=10)
    assert two and any("复购率" in r["text"] for r in two)
    three = store.search_note_units(db, ["复购率"], limit=10)
    assert three and any("复购率" in r["text"] for r in three)
    assert all(r["session_id"] == "S1" for r in three)


def test_index_session_covers_summary_point_and_bookmark(db):
    _seed_note(db)
    store.insert_mark(db, "S1", 300, "这段要回来看", kind="bookmark")
    store.save_highlight(db, {
        "session_id": "S1", "t_start": 900, "t_end": 940, "peak_t": 900,
        "text": "这个判断方法值得回听", "quote": "这个判断方法值得回听",
        "kind": "info", "source": "live",
    })

    n = notes.index_session(db, "S1")
    assert n >= 3
    units = store.note_units_for_session(db, "S1", 50)
    kinds = {u["kind"] for u in units}
    assert {"summary", "point", "bookmark", "highlight"} <= kinds
    assert any("复购率" in u["text"] for u in units)


# ---------------------------------------------------------------- 书签 / 问答

def test_bookmarks_merge_marks_and_collected_highlights(db):
    _seed_note(db)
    m = store.insert_mark(db, "S1", 300, "这段要回来看", kind="bookmark")
    assert m["kind"] == "bookmark"
    assert store.insert_mark(db, "S1", 310, "普通打点")["kind"] == "mark"
    store.save_highlight(db, {
        "session_id": "S1", "t_start": 900, "t_end": 940, "peak_t": 900,
        "text": "收藏的高光", "quote": "收藏的高光",
        "kind": "info", "source": "live",
    })

    items = notes.bookmarks(db, "S1")
    assert len(items) == 3
    assert [x["t"] for x in items] == [300, 310, 900]
    assert items[0]["source"] == "reader"
    assert items[1]["source"] == "recording"
    assert items[2]["kind"] == "highlight"
    assert items[2]["source"] == "live"

    assert store.delete_mark(db, "S1", int(m["id"])) == 1
    assert len(notes.bookmarks(db, "S1")) == 2


def test_qa_without_llm_fails_not_extractive(db):
    _seed_note(db)
    notes.index_session(db, "S1")
    row = store.qa_create(db, "复购率", {})
    out = qa.answer(db, int(row["id"]))
    assert out["ok"] is False
    assert out["state"] == "failed"
    assert "还没配置大模型" in out["error"]
    assert "未使用本地抽取" in out["error"]

    saved = store.qa_get(db, int(row["id"]))
    assert saved["state"] == "failed"
    assert saved["answer"] == ""
    assert saved["citations"] == []
    assert "未使用本地抽取" in saved["error"]


def test_qa_llm_answer_has_citations(db, monkeypatch):
    _seed_note(db)
    notes.index_session(db, "S1")
    monkeypatch.setattr(qa.settings, "resolve",
                        lambda *a, **k: {"configured": True, "model": "test"})
    monkeypatch.setattr(qa.llm, "chat", lambda *a, **k: json.dumps({
        "answer": "复购率比单场 GMV 更重要 [1]。",
        "citations": [1],
        "confidence": "high"}, ensure_ascii=False))
    row = store.qa_create(db, "复购率", {})
    out = qa.answer(db, int(row["id"]))
    assert out["ok"] is True
    assert out["state"] == "ready"
    assert out["engine"] == "llm"
    assert "复购率" in out["answer"]
    assert out["citations"]
    cite = out["citations"][0]
    assert cite["session_id"] == "S1"
    assert cite["text"]


def test_qa_invalid_evidence_fails_not_extractive(db, monkeypatch):
    _seed_note(db)
    notes.index_session(db, "S1")
    monkeypatch.setattr(qa.settings, "resolve",
                        lambda *a, **k: {"configured": True, "model": "test"})
    monkeypatch.setattr(qa.llm, "chat", lambda *a, **k: json.dumps({
        "answer": "模型编的答案 [99]。", "citations": [99]},
        ensure_ascii=False))
    row = store.qa_create(db, "复购率", {})
    out = qa.answer(db, int(row["id"]))
    assert out["state"] == "failed"
    assert "证据校验" in out["error"]
    assert "未使用本地抽取" in out["error"]


def test_qa_retrieve_scoped_to_session(db):
    _seed_note(db, "S1", "2026-11-10T20:00:00")
    _seed_note(db, "S2", "2026-11-11T20:00:00")
    notes.index_session(db, "S1")
    notes.index_session(db, "S2")
    units = qa.retrieve(db, "复购率", {"session_id": "S2"})
    assert units
    assert all(u["session_id"] == "S2" for u in units)


# ---------------------------------------------------------------- 周报

def test_weekly_period_range_and_empty_report(db):
    since, until, period = weekly.week_range("2026-W46")
    assert period == "2026-W46"
    assert since == date(2026, 11, 9)
    assert since.isoweekday() == 1
    assert (until - since).days == 6

    row = weekly.build("2026-W46", use_llm=False)
    assert row["state"] == "empty"
    assert store.report_get(db, period="2026-W46")["state"] == "empty"


def test_weekly_build_structured_data(db):
    _seed_note(db, "S1", "2026-11-10T20:00:00")
    row = weekly.build("2026-W46", use_llm=False)
    assert row["state"] == "ready"
    data = row["data"]
    assert data["stats"]["sessions"] == 1
    assert data["stats"]["seconds"] == pytest.approx(5400)
    assert data["sessions"][0]["session_id"] == "S1"
    assert "选品夜谈" in row["summary"] or "1 场" in row["summary"]


def test_weekly_disabled_does_not_write(db):
    settings.set_prefs({"weekly_enabled": False})
    assert weekly.ensure("2026-W46") is None
    assert store.report_get(db, period="2026-W46") is None


def test_weekly_schedule_follows_pref():
    settings.set_prefs({"weekly_time": "sun-20"})
    assert weekly.scheduled_at(datetime(2026, 11, 15, 21, 0)) == \
        datetime(2026, 11, 15, 20, 0)
    assert weekly._should_notify(datetime(2026, 11, 17, 9, 0)) is True
    assert weekly._should_notify(datetime(2026, 11, 19, 9, 0)) is False

    settings.set_prefs({"weekly_time": "mon-09"})
    assert weekly.scheduled_at(datetime(2026, 11, 17, 10, 0)) == \
        datetime(2026, 11, 16, 9, 0)


# ---------------------------------------------------------------- 导出

def test_export_note_md_includes_bookmarks_not_transcript(db, tmp_path):
    _seed_note(db)
    store.insert_mark(db, "S1", 300, "这段要回来看", kind="bookmark")
    store.save_highlight(db, {
        "session_id": "S1", "t_start": 900, "t_end": 940, "peak_t": 900,
        "text": "这个判断方法值得回听", "quote": "这个判断方法值得回听",
        "kind": "info", "source": "live",
    })

    text = export.to_note_md(db, "S1")
    assert "# 选品夜谈" in text
    assert "## 一句话" in text
    assert "## 要点" in text
    assert "## 书签" in text
    assert "这段要回来看" in text
    assert "## 值得回听" in text
    assert "复购率比单场 GMV 更重要。" in text
    assert "## 逐字稿" not in text

    body, mime, name = export.build(db, "S1", "note_md")
    assert body == text
    assert mime.startswith("text/markdown")
    assert name.endswith(".note.md")

    with pytest.raises(ValueError):
        export.to_note_md(db, "NO_SUCH_SESSION")


# ---------------------------------------------------------------- 性能

def test_perf_lite_changes_runtime_flags(monkeypatch):
    monkeypatch.setattr(config, "system_power", lambda: {
        "supported": False, "on_battery": False, "percent": -1, "charging": False})
    try:
        config.apply_perf_mode("lite")
        p = config.perf()
        assert p["mode"] == "lite"
        e = p["effective"]
        assert e["partials"] is False
        assert e["live_highlight_card"] is False
        assert e["notify_poll_s"] == 6
        assert e["live_poll_s"] == 5
        assert e["library_page"] == 16
        assert e["qa_top_k"] == 6
        assert e["animations"] is False

        config.apply_perf_mode("full")
        p = config.perf()
        assert p["mode"] == "full"
        assert p["effective"]["partials"] is True
        assert p["effective"]["library_page"] == 24
        assert p["effective"]["animations"] is True
    finally:
        config.apply_perf_mode("auto")


def test_low_power_pauses_realtime_only_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "system_power", lambda: {
        "supported": True, "on_battery": True, "percent": 15, "charging": False})
    try:
        config.apply_perf_mode("full", True)
        assert config.low_power() is True
        assert config.partials_enabled() is False
        assert config.perf()["effective"]["live_highlight_card"] is False

        config.apply_perf_mode("full", False)
        assert config.low_power() is False
        assert config.partials_enabled() is True

        config.apply_perf_mode("full", True)
        monkeypatch.setattr(config, "system_power", lambda: {
            "supported": True, "on_battery": True, "percent": 55,
            "charging": False})
        assert config.low_power() is False
        assert config.partials_enabled() is True
    finally:
        config.apply_perf_mode("auto", True)
