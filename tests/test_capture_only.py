# -*- coding: utf-8 -*-
"""仅采集模式：只录音，不加载 ASR/声纹，不产生字幕和总结。

这些用例把「模型调用」全部换成会爆炸的替身：只要采集链路碰了
ENGINE/POOL/VAD，测试就会失败。低配 VPS 上的录制节点靠这条边界活着。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from app import live, server, store

SR = live.SAMPLE_RATE


def _pcm(seconds: float = 1.0, value: int = 1200) -> bytes:
    n = int(SR * seconds)
    return (np.ones(n, dtype="<i2") * value).tobytes()


def _wait_until(fn, timeout: float = 6.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.02)
    raise AssertionError("等待超时")


@pytest.fixture
def no_models(monkeypatch):
    """任何模型/工作池调用都算越界。"""
    def boom(*a, **kw):
        raise AssertionError("仅采集模式不应调用模型或 ASR 工作池")

    monkeypatch.setattr(live.ENGINE, "load", boom)
    monkeypatch.setattr(live.POOL, "register", boom)
    monkeypatch.setattr(live.POOL, "start", boom)
    monkeypatch.setattr(live.POOL, "drain_session", boom)
    monkeypatch.setattr(live.tasks, "submit_finalize", boom)


def _n_utts(sid: str) -> int:
    conn = store.connect()
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM utterances WHERE session_id=?", (sid,)).fetchone()[0])
    finally:
        conn.close()


def test_capture_only_records_without_models(db, no_models):
    out = live.start("仅采集测试", source_mode="direct", capture_only=True)
    sid = out["session_id"]
    assert out["capture_only"] is True
    assert "仅采集" in out["engine"]

    s = live._SESSIONS[sid]
    assert live.status(sid)["capture_only"] is True
    assert live.status(sid)["n_utterances"] == 0

    live.feed(sid, _pcm(1.0, 1000))
    live.feed(sid, _pcm(1.0, 0))
    live.feed(sid, _pcm(1.0, 2000))
    _wait_until(lambda: s.total_samples >= 3 * SR)

    st = live.status(sid)
    assert st["elapsed"] >= 3.0
    assert st["audible_s"] >= 2.0          # 中间那块全零不算有声
    assert st["zero_s"] == 0.0             # 最后一块有声音，连续静音已清零
    assert st["engine"].startswith("仅采集")
    assert st["asr"] == {}
    assert st["speakers"] == []

    ended = live.stop(sid)
    assert ended["capture_only"] is True
    assert ended["n_utterances"] == 0

    conn = store.connect()
    try:
        row = store.get_session(conn, sid)
        assert row["capture_only"] == 1
        assert row["duration_s"] >= 3.0
        # 仅采集没有转写，「未转写时长」不能写成整场时长。
        assert float(row["untranscribed_seconds"] or 0) == 0.0
    finally:
        conn.close()
    assert _n_utts(sid) == 0
    assert not live.is_active(sid)


def test_capture_only_status_sees_offline_transcript(db, no_models):
    """离线进程把字写进库后，采集进程的状态也要能看见。"""
    out = live.start("离线补字", capture_only=True)
    sid = out["session_id"]
    live.feed(sid, _pcm(1.0, 1200))
    _wait_until(lambda: live._SESSIONS[sid].total_samples >= SR)
    store.insert_utterances(db, [{
        "session_id": sid, "t_start": 0.0, "t_end": 1.0,
        "speaker_id": "SPK_00", "speaker_label": "说话人", "text": "离线补出来的一句",
    }])
    st = live.status(sid)
    assert st["n_utterances"] == 1
    assert st["last_line"] == "离线补出来的一句"
    live.stop(sid, finalize=False)


def test_capture_only_reader_says_recording_only(db, no_models):
    """回放页要能分清「没转写」和「转写失败」。"""
    from app import reader

    out = live.start("回放页仅采集", capture_only=True)
    sid = out["session_id"]
    live.feed(sid, _pcm(1.0, 1200))
    _wait_until(lambda: live._SESSIONS[sid].total_samples >= SR)
    live.stop(sid, finalize=False)

    conn = store.connect()
    try:
        data = reader.read(conn, sid)
    finally:
        conn.close()
    assert data["session"]["capture_only"] == 1
    assert data["paragraphs"] == []


def test_capture_only_short_session_is_not_cleanup_candidate(db):
    """仅采集没有逐字稿是正常的，不能把刚录的短片默认勾进清理候选。"""
    from helpers import add_session

    add_session(db, "S_CAP_SHORT", title="仅采集短片", duration_s=30,
                started_at="2026-11-19T20:00:00", capture_only=1)
    add_session(db, "S_TAB_SHORT", title="完整模式空采", duration_s=30,
                started_at="2026-11-19T20:10:00")
    rows = {r["id"]: r for r in
            server._mark_session_quality(store.list_sessions(db))}
    assert rows["S_CAP_SHORT"]["quality"]["empty"] is False
    assert rows["S_TAB_SHORT"]["quality"]["empty"] is True


def test_capture_only_skips_stall_watchdog(db, no_models):
    out = live.start("无定稿也不算停滞", capture_only=True)
    sid = out["session_id"]
    s = live._SESSIONS[sid]
    s.last_speech_s = 2.0
    s.last_final_at = time.monotonic() - 3600
    s.total_samples = int(600 * SR)
    assert s.check_stall() is False
    live.stop(sid, finalize=False)


def test_info_ready_without_model_files(monkeypatch, db):
    monkeypatch.setattr(live, "CAPTURE_ONLY", True)
    monkeypatch.setattr(live.Engine, "missing",
                        staticmethod(lambda: ["/nonexistent/model.onnx"]))
    info = live.info()
    assert info["capture_only"] is True
    assert info["ready"] is True
    assert info["missing"] == []
    assert "仅采集" in info["engine"]
    assert info["asr"] == {}


def test_global_env_flag_is_default(monkeypatch, db, no_models):
    monkeypatch.setattr(live, "CAPTURE_ONLY", True)
    out = live.start("跟全局走")
    assert out["capture_only"] is True
    live.stop(out["session_id"], finalize=False)


# ---------------------------------------------------------------- HTTP 边界

@pytest.fixture
def http_base(db, tmp_path, monkeypatch):
    monkeypatch.setattr(server.rooms, "PATH", tmp_path / "rooms.json")
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        # 用例中途断言失败时，/api/live/stop 可能没跑到；不在这里收尾会
        # 留下工作线程和未关的 spool 文件，污染下一个用例。
        for sid in list(live._SESSIONS):
            try:
                live.stop(sid, finalize=False)
            except Exception:
                pass
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _json(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _chunk(base: str, sid: str, pcm: bytes) -> int:
    req = urllib.request.Request(
        base + "/api/live/chunk", data=pcm, method="POST",
        headers={"Content-Type": "application/octet-stream", "X-Live-Id": sid})
    with urllib.request.urlopen(req) as r:
        return r.status


def test_api_start_passes_capture_only(http_base, db, no_models):
    base = http_base
    code, out = _json(base, "/api/live/start", {"title": "接口仅采集",
                                               "capture_only": True})
    assert code == 201 and out["capture_only"] is True
    sid = out["session_id"]
    assert _chunk(base, sid, _pcm(1.0)) == 200
    _wait_until(lambda: live._SESSIONS[sid].total_samples >= SR)

    code, st = _json(base, "/api/live/status?id=" + sid)
    assert code == 200 and st["capture_only"] is True and st["audible_s"] >= 0.9

    code, ended = _json(base, "/api/live/stop", {"session_id": sid})
    assert code == 200 and ended["capture_only"] is True

    code, denied = _json(base, "/api/summarize", {"session_id": sid, "filters": {}})
    assert code == 400 and denied["capture_only"] is True
    code, denied = _json(base, "/api/summarize/estimate",
                         {"session_id": sid, "filters": {}})
    assert code == 400 and denied["capture_only"] is True
    code, denied = _json(base, "/api/analysis/start", {"session_id": sid})
    assert code == 400 and denied["capture_only"] is True

    code, listing = _json(base, "/api/sessions")
    assert code == 200
    row = next(x for x in listing["sessions"] if x["id"] == sid)
    assert int(row["capture_only"]) == 1
    assert int(row["n_utterances"]) == 0
