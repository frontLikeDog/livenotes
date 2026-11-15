# -*- coding: utf-8 -*-
"""通知台账：幂等、已读/已投递、按事件静音、笔记完成/失败文案。"""
from __future__ import annotations

from app import notify, settings


def test_emit_dedupes_by_key_and_feed_returns_latest(db):
    a = notify.emit("stream_stalled", "听不到声音了", "已 30 秒没有声音。",
                    session_id="S1", dedupe_key="stalled:S1:1")
    b = notify.emit("stream_stalled", "重复", "不该出现",
                    session_id="S1", dedupe_key="stalled:S1:1")
    assert a and b is None
    out = notify.feed()
    assert len(out["items"]) == 1
    assert out["items"][0]["kind"] == "stream_stalled"
    assert out["items"][0]["action"]["view"] == "hud"
    assert out["unread"] == 1
    assert out["latest_id"] == a["id"]
    assert out["settings"]["enabled"] is True


def test_feed_since_id_and_read_state(db):
    first = notify.emit("note_ready", "笔记已生成", "1 个要点",
                        session_id="S1", dedupe_key="note_ready:S1")
    second = notify.emit("note_failed", "没生成成功", "录音还在",
                         session_id="S2", dedupe_key="note_failed:S2")
    assert first and second
    out = notify.feed(since_id=first["id"])
    assert [x["id"] for x in out["items"]] == [second["id"]]
    notify.mark_read([second["id"]])
    out = notify.feed()
    assert out["unread"] == 1
    assert [x["id"] for x in out["items"] if x["read"]] == [second["id"]]
    assert notify.mark_read(all_=True)["unread"] == 0


def test_delivered_is_idempotent_and_reported(db):
    n = notify.emit("silent", "静音", "两分钟没声音",
                    session_id="S1", dedupe_key="silent:S1:1")
    assert notify.mark_delivered([n["id"]]) == 1
    assert notify.mark_delivered([n["id"]]) == 0
    assert notify.feed()["items"][0]["delivered"] is True


def test_master_switch_and_per_kind_mute(db):
    settings.set_prefs({"notify_muted": ["silent"]})
    assert notify.emit("silent", "静音", "", session_id="S1") is None
    assert notify.emit("stream_stalled", "断流", "", session_id="S1") is not None
    settings.set_prefs({"notify_enabled": False})
    assert notify.emit("stream_stalled", "断流", "", session_id="S2") is None
    # 关掉通知不影响已经落库的历史，通知中心仍可查。
    assert len(notify.feed()["items"]) == 1


def test_episode_allows_a_second_notification(db):
    assert notify.stream_stalled("S1", "阿哲", 1, 35)
    assert notify.stream_stalled("S1", "阿哲", 1, 40) is None
    assert notify.stream_stalled("S1", "阿哲", 2, 35)


def test_note_ready_and_failed_carry_action_and_human_copy(db):
    ready = notify.note_ready("S1", {
        "session_title": "今晚聊选品", "points": [1, 2, 3],
        "highlights": [1, 2]})
    assert ready["title"] == "《今晚聊选品》的笔记已生成"
    assert "3 个要点" in ready["body"] and "2 段高光" in ready["body"]
    assert ready["action"] == {"view": "note", "session_id": "S1"}
    failed = notify.note_failed("S2", "模型额度用完了\nTraceback ...")
    assert failed["level"] == "warn"
    assert "录音和逐字稿都在" in failed["body"]
    assert "Traceback" not in failed["body"]


def test_settings_view_lists_kinds_and_updates(db):
    view = notify.settings_view()
    assert {k["kind"] for k in view["kinds"]} >= {"stream_stalled", "note_ready"}
    out = notify.update_settings({"notify_system": False,
                                  "notify_muted": ["disk_low", "disk_low"]})
    assert out["system"] is False
    assert out["muted"] == ["disk_low"]
    assert settings.prefs()["notify_system"] is False


def test_live_ended_copy_distinguishes_stall_from_down(db):
    """断流收尾是「我们停了」，只有流还在、内容全零才说「直播可能结束了」。"""
    stall = notify.live_ended("S1", "阿哲")
    assert stall["title"] == "已停止记录"
    assert "录音和逐字稿已保存" in stall["body"]
    down = notify.live_ended("S2", "阿哲", down=True)
    assert down["title"] == "直播可能结束了"
    assert "已停止记录" in down["body"]


def test_disk_low_only_below_threshold(db):
    assert notify.check_disk(min_bytes=0) is None
    item = notify.check_disk(min_bytes=10 ** 18)
    assert item and item["kind"] == "disk_low"
    assert notify.check_disk(min_bytes=10 ** 18) is None      # 同一天只发一次
