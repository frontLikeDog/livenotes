# -*- coding: utf-8 -*-
"""断流看门狗：安静不算断流，没块才算；一轮只收一次。"""
from __future__ import annotations

import time

import pytest

from app import live, rooms, server
from tests import helpers


class FakeSession:
    """只带看门狗读的那几个字段，不加载任何模型。"""

    def __init__(self, sid: str, started: float, last_chunk: float = 0.0,
                 running: bool = True) -> None:
        self.id = sid
        self.title = sid
        self.running = running
        self._started_mono = started
        self._last_chunk_mono = last_chunk
        self._chunk_stall_warned = False

    def mark_chunk_stall_warned(self) -> bool:
        if self._chunk_stall_warned:
            return False
        self._chunk_stall_warned = True
        return True


def test_stalled_sessions_keys_on_audio_blocks_not_speech():
    # a 一直在推流（哪怕主播一句话没说），b 建了会话就没推过块，c 刚建会话。
    live._SESSIONS["a"] = FakeSession("a", started=0.0, last_chunk=250.0)
    live._SESSIONS["b"] = FakeSession("b", started=0.0, last_chunk=0.0)
    live._SESSIONS["c"] = FakeSession("c", started=295.0, last_chunk=0.0)
    live._SESSIONS["d"] = FakeSession("d", started=0.0, last_chunk=0.0, running=False)

    out = {x["id"]: x for x in live.stalled_sessions(120, now=300.0)}
    assert set(out) == {"b"}          # a 刚推过块，c 还在宽限期内，d 已经停了
    assert out["b"]["never"] is True
    assert out["b"]["idle_s"] == 300.0


def test_stalled_sessions_reports_how_long_since_last_block():
    live._SESSIONS["a"] = FakeSession("a", started=0.0, last_chunk=170.0)
    out = live.stalled_sessions(120, now=300.0)
    assert [(x["id"], x["never"]) for x in out] == [("a", False)]
    assert out[0]["idle_s"] == 130.0
    assert "130 秒" in server._stall_reason(out[0])


def test_watch_stalled_closes_each_session_once(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "_auto_close_session",
                        lambda sid, reason: calls.append((sid, reason)))
    with server._STALL_LOCK:
        server._STALL_CLOSING.clear()
    # 这条走的是真实时钟（_watch_stalled 不传 now），所以时间戳必须以当前 monotonic
    # 为基准。写死 0.0 会在刚开机的 CI 容器上假红：容器 uptime 还不到 120 秒时，
    # idle = monotonic() - 0 也小于阈值，看起来像「没有断流」。
    stale = time.monotonic() - 10_000.0
    live._SESSIONS["x"] = FakeSession("x", started=stale, last_chunk=stale)

    try:
        assert server._watch_stalled(idle_s=120) == ["x"]
        # 收尾线程还在跑的时候，下一轮不能对同一场再起一个
        assert server._watch_stalled(idle_s=120) == []
        assert [c[0] for c in calls] == ["x"]
    finally:
        with server._STALL_LOCK:
            server._STALL_CLOSING.clear()


@pytest.fixture
def roomstore(tmp_path, monkeypatch):
    monkeypatch.setattr(rooms, "PATH", tmp_path / "rooms.json")
    with server._STALL_LOCK:
        server._STALL_CLOSING.clear()
    yield
    with server._STALL_LOCK:
        server._STALL_CLOSING.clear()


def _room_with_session(db, tmp_path, state="live", sid="S1"):
    wav = tmp_path / (sid + ".wav")
    wav.write_bytes(b"\0" * 3200)
    helpers.add_session(db, sid, audio_path=str(wav))
    db.commit()
    room, _ = rooms.add("https://live.douyin.com/" + sid)
    return rooms.update(room["id"], state=state, stage=6, session_id=sid)


def test_auto_close_stops_room_and_flags_why(db, tmp_path, roomstore, monkeypatch):
    room = _room_with_session(db, tmp_path)
    stopped: list[str] = []
    monkeypatch.setattr(server.live, "stop", lambda sid: stopped.append(sid))

    server._auto_close_session("S1", "已经 130 秒没收到音频块（浏览器崩溃 / 页面被关 / 网络断）")

    assert stopped == ["S1"]          # 场次真的被停了：WAV/总结/回顾走正常收尾
    r = rooms.get(room["id"])
    assert r["state"] == "stopped"
    assert r["auto_stopped"] is True
    assert "130 秒" in r["auto_stop_reason"]
    assert r["saved"] is True and r["has_recording"] is True


def test_auto_close_leaves_failed_room_reason_alone(db, tmp_path, roomstore, monkeypatch):
    room = _room_with_session(db, tmp_path, state="failed")
    rooms.update(room["id"], error="标签页音频结束（下播 / 刷新 / 关页）")
    monkeypatch.setattr(server.live, "stop", lambda sid: None)

    server._auto_close_session("S1", "已经 130 秒没收到音频块")
    r = rooms.get(room["id"])
    # 采集页给的失败原因更具体，看门狗不许把它盖成"断流"
    assert r["state"] == "failed"
    assert r["error"] == "标签页音频结束（下播 / 刷新 / 关页）"
    assert not r.get("auto_stopped")


def test_auto_close_ignores_room_that_is_gone(db, tmp_path, roomstore, monkeypatch):
    monkeypatch.setattr(server.live, "stop", lambda sid: None)
    server._auto_close_session("不存在的会话", "断流")   # 不抛异常就算过


def test_watch_stalled_can_be_disabled(monkeypatch):
    monkeypatch.setattr(server, "_auto_close_session",
                        lambda sid, reason: (_ for _ in ()).throw(AssertionError("不该收")))
    live._SESSIONS["x"] = FakeSession("x", started=0.0, last_chunk=0.0)
    assert server._watch_stalled(idle_s=0) == []


def test_watch_stalled_warn_alerts_once(monkeypatch):
    """30 秒预警只发一次；同一场再查一轮不能重复发。"""
    live._SESSIONS.clear()          # 前面用例留下的假会话也会被判为断流
    stale = time.monotonic() - 100.0
    live._SESSIONS["w"] = FakeSession("w", started=stale, last_chunk=stale)
    alerts: list[tuple[str, float]] = []
    monkeypatch.setattr(server.rooms, "get_by_session", lambda sid: None)
    monkeypatch.setattr(live, "push_capture_lost",
                        lambda sid, idle: alerts.append((sid, idle)) or True)

    assert server._watch_stalled_warn(warn_s=30) == ["w"]
    assert server._watch_stalled_warn(warn_s=30) == []
    # 告警在后台线程里发；给它一点时间
    for _ in range(100):
        if alerts:
            break
        time.sleep(0.01)
    assert alerts and alerts[0][0] == "w" and alerts[0][1] >= 99.0


def test_watch_stalled_warn_ignores_fresh_chunks(monkeypatch):
    live._SESSIONS.clear()
    stale = time.monotonic() - 10.0
    live._SESSIONS["w"] = FakeSession("w", started=stale, last_chunk=stale)
    monkeypatch.setattr(live, "push_capture_lost",
                        lambda *a: (_ for _ in ()).throw(AssertionError("不该告警")))
    assert server._watch_stalled_warn(warn_s=30) == []


def test_feed_resets_chunk_stall_warning(monkeypatch):
    """块回来后预警标志复位，并记下缺口秒数——下一次再断还能再报。"""
    s = live.LiveSession("测试")
    s.running = True
    s._last_chunk_mono = time.monotonic() - 40.0
    s._chunk_stall_warned = True
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(live.logs, "log_event",
                        lambda event, **kw: events.append((event, kw)))

    s.feed(b"\0\0" * 4096)

    assert s._chunk_stall_warned is False
    assert any(e[0] == "live.capture_resumed" for e in events)
    assert s.q.qsize() == 1


def test_feed_resets_warning_before_first_chunk(monkeypatch):
    """第一块之前就报过警（从没推过流）：第一块到了也要复位，不能一直哑火。"""
    s = live.LiveSession("测试")
    s.running = True
    s._last_chunk_mono = 0.0
    s._chunk_stall_warned = True
    monkeypatch.setattr(live.logs, "log_event", lambda *a, **kw: None)

    s.feed(b"\0\0" * 4096)

    assert s._chunk_stall_warned is False
    assert s.q.qsize() == 1


# ---------------------------------------------------------------- 静音看门狗
class SilentSession(FakeSession):
    """块一直在到、但内容全零的场次。"""

    def __init__(self, sid: str, zero_s: float = 0.0, paused: bool = False,
                 running: bool = True) -> None:
        super().__init__(sid, started=0.0, last_chunk=time.monotonic(),
                         running=running)
        self.zero_s = zero_s
        self.paused = paused


def test_zero_audio_accumulates_until_sound_returns():
    """数字静音按时长累计；只要有一块有声就清零（安静房间的底噪也算有声）。"""
    s = live.LiveSession("静音")
    s._note_level(0.0, 16000)
    s._note_level(0.0, 16000)
    assert s.zero_s == 2.0
    s._note_level(0.002, 16000)          # 底噪：不是"没人说话"，是流还活着
    assert s.zero_s == 0.0


def test_silent_sessions_ignore_short_silence_pause_and_stopped():
    live._SESSIONS.clear()
    live._SESSIONS["a"] = SilentSession("a", zero_s=900.0)
    live._SESSIONS["b"] = SilentSession("b", zero_s=30.0)
    live._SESSIONS["c"] = SilentSession("c", zero_s=900.0, paused=True)
    live._SESSIONS["d"] = SilentSession("d", zero_s=900.0, running=False)
    try:
        out = live.silent_sessions(600.0)
        assert [x["id"] for x in out] == ["a"]
        assert out[0]["zero_s"] == 900.0
    finally:
        live._SESSIONS.clear()


def test_watch_silent_closes_each_session_once(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "_auto_close_session",
                        lambda sid, reason: calls.append((sid, reason)))
    with server._STALL_LOCK:
        server._STALL_CLOSING.clear()
    live._SESSIONS.clear()
    live._SESSIONS["z"] = SilentSession("z", zero_s=900.0)
    try:
        assert server._watch_silent(zero_s=600) == ["z"]
        assert server._watch_silent(zero_s=600) == []
        assert [c[0] for c in calls] == ["z"]
        # 直取代理靠「音频块」这个子串判断"断流收尾后要不要重新拉起"。
        # 静音收尾不是断流，理由里不能出现它，否则会立刻把场次重新拉起来。
        assert "音频块" not in calls[0][1]
        assert "分钟音频全零" in calls[0][1]
    finally:
        live._SESSIONS.clear()
        with server._STALL_LOCK:
            server._STALL_CLOSING.clear()


def test_watch_silent_warn_only_nudges_the_room(db, tmp_path, roomstore):
    """预警只改房间提示，不结束场次；同一分钟不重复写库。"""
    room = _room_with_session(db, tmp_path)
    live._SESSIONS.clear()
    live._SESSIONS["S1"] = SilentSession("S1", zero_s=200.0)
    try:
        assert server._watch_silent_warn(warn_s=120) == ["S1"]
        r = rooms.get(room["id"])
        assert r["state"] == "live"
        assert "音频已连续静音" in r["stage_note"]
        assert server._watch_silent_warn(warn_s=120) == []
    finally:
        live._SESSIONS.clear()


def test_clear_silent_note_when_audio_returns(db, tmp_path, roomstore):
    room = _room_with_session(db, tmp_path)
    rooms.update(room["id"],
                 stage_note="音频已连续静音 3 分钟（流还在推，但没有任何声音）")
    live._SESSIONS.clear()
    live._SESSIONS["S1"] = SilentSession("S1", zero_s=1.0)
    try:
        server._clear_silent_notes()
        assert rooms.get(room["id"])["stage_note"] == "音频已恢复"
    finally:
        live._SESSIONS.clear()


def test_watch_silent_can_be_disabled(monkeypatch):
    monkeypatch.setattr(server, "_auto_close_session",
                        lambda sid, reason: (_ for _ in ()).throw(AssertionError("不该收")))
    live._SESSIONS.clear()
    live._SESSIONS["z"] = SilentSession("z", zero_s=900.0)
    try:
        assert server._watch_silent(zero_s=0) == []
    finally:
        live._SESSIONS.clear()
