# -*- coding: utf-8 -*-
"""离线补转写：从 spool/WAV 增量出字，不碰采集链路。"""
from __future__ import annotations

import types
import wave

import numpy as np

from app import live, offline_asr, store


class FakeVad:
    """每累计 emit_at 个采样就吐一句；flush 把尾巴也吐出来。"""

    def __init__(self, emit_at: int = 16000):
        self.emit_at = int(emit_at)
        self.buf: list[np.ndarray] = []
        self.total = 0
        self.last = 0
        self.segments: list[types.SimpleNamespace] = []

    def accept_waveform(self, x) -> None:
        self.buf.append(np.array(x, dtype="float32", copy=True))
        self.total += len(x)
        if self.total - self.last >= self.emit_at:
            self._emit()

    def _emit(self) -> None:
        if not self.buf:
            return
        samples = np.concatenate(self.buf)
        self.segments.append(types.SimpleNamespace(samples=samples, start=self.last))
        self.last = self.total
        self.buf = []

    def flush(self) -> None:
        self._emit()

    def empty(self) -> bool:
        return not self.segments

    @property
    def front(self):
        return self.segments[0]

    def pop(self) -> None:
        self.segments.pop(0)

    def is_speech_detected(self) -> bool:
        return False


class FakeEngine:
    def __init__(self):
        self.loaded = 0
        self.texts = 0

    def load(self) -> None:
        self.loaded += 1

    def transcribe(self, samples) -> str:
        self.texts += 1
        return "这是一句测试发言"

    def new_embedder(self):
        return object()

    def embed(self, samples, extractor=None):
        # 同一场都当成同一个人，方便断言标签
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


def _pcm(seconds: float, value: int = 1000) -> bytes:
    n = int(live.SAMPLE_RATE * seconds)
    return (np.ones(n, dtype="<i2") * value).tobytes()


def _session(conn, sid: str, *, audio_path=None, duration=0.0,
             capture_only=1, original=None) -> None:
    store.insert_session(conn, {
        "id": sid, "title": sid, "started_at": "2026-11-25T10:00:00",
        "duration_s": duration, "source_mode": "direct",
        "capture_only": capture_only, "sample_rate": live.SAMPLE_RATE,
        "audio_path": audio_path,
    })
    if original:
        store.set_audio_meta(conn, sid, audio_original_path=original)


def _utts(conn, sid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT t_start,t_end,text,speaker_label FROM utterances "
        "WHERE session_id=? ORDER BY t_start", (sid,))]


def test_pcm_incremental_writes_utterances(db, isolated_paths):
    sid = "LIVE_TEST_PCM"
    _session(db, sid)
    (isolated_paths / "audio" / (sid + ".pcm")).write_bytes(_pcm(2.0))

    eng = FakeEngine()
    worker = offline_asr.OfflineAsr(
        engine=eng, vad_factory=lambda: FakeVad(16000), speakers=False,
        audio_dir=isolated_paths / "audio")

    out = worker.run_once(sid=sid)
    assert out["sessions"] == 1
    rows = _utts(db, sid)
    # 2 秒音频按 512 窗喂给假 VAD，16000 并不是 512 的整数倍：
    # 第一句在 16384 采样处切出，剩下的不够再切一句。
    assert len(rows) == 1
    assert rows[0]["text"] == "这是一句测试发言"
    assert abs(rows[0]["t_start"] - 0.0) < 0.05
    prog = store.get_offline_asr(db, sid)
    assert prog["status"] == "running"
    assert prog["processed_s"] >= 1.9

    # 没有新音频：不重复插入
    worker.run_once(sid=sid)
    assert len(_utts(db, sid)) == 1

    # 再追加一秒，同一个进程状态继续；VAD 再吐一句
    with open(isolated_paths / "audio" / (sid + ".pcm"), "ab") as f:
        f.write(_pcm(1.0, 1200))
    worker.run_once(sid=sid)
    assert len(_utts(db, sid)) == 2


def test_finished_wav_flushes_and_marks_done(db, isolated_paths, monkeypatch):
    sid = "LIVE_TEST_WAV"
    wav = isolated_paths / "audio" / (sid + ".wav")
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(live.SAMPLE_RATE)
        w.writeframes(_pcm(2.0))
    _session(db, sid, audio_path=str(wav), duration=2.0)

    called = []
    monkeypatch.setattr(offline_asr.recluster, "recluster_session",
                        lambda conn, s: called.append(s) or {"speakers": []})

    worker = offline_asr.OfflineAsr(
        engine=FakeEngine(), vad_factory=lambda: FakeVad(16000),
        speakers=True, audio_dir=isolated_paths / "audio")
    out = worker.run_once(sid=sid)
    assert out["done"] == 1
    rows = _utts(db, sid)
    assert len(rows) == 2
    assert rows[0]["speaker_label"].startswith("说话人")
    prog = store.get_offline_asr(db, sid)
    assert prog["status"] == "done"
    assert prog["n_utterances"] == 2
    assert called == [sid]


def test_restart_skips_already_inserted(db, isolated_paths):
    sid = "LIVE_TEST_DUP"
    _session(db, sid)
    (isolated_paths / "audio" / (sid + ".pcm")).write_bytes(_pcm(1.0))
    store.insert_utterances(db, [{
        "session_id": sid, "t_start": 0.0, "t_end": 1.0,
        "speaker_id": "SPK_00", "speaker_label": "说话人", "text": "旧结果",
    }])
    store.set_offline_asr(db, sid, processed_s=0.0, status="running")

    worker = offline_asr.OfflineAsr(
        engine=FakeEngine(), vad_factory=lambda: FakeVad(16000), speakers=False,
        audio_dir=isolated_paths / "audio")
    worker.run_once(sid=sid)
    rows = _utts(db, sid)
    assert len(rows) == 1
    assert rows[0]["text"] == "旧结果"


def test_status_api_sees_offline_transcript_without_engine_restart(db):
    """旧引擎内存里 n_utterances=0，livebrief 也要从库里补出来。"""
    from app import livebrief

    sid = "LIVE_TEST_STATUS"
    _session(db, sid)
    store.insert_utterances(db, [{
        "session_id": sid, "t_start": 0.0, "t_end": 1.0,
        "speaker_id": "SPK_00", "speaker_label": "说话人", "text": "库里的一句",
    }])
    rooms = [{"id": "rm1", "seq": 1, "name": "语音厅", "state": "live",
              "session_id": sid, "capture_engine": "direct"}]
    sessions = [{"id": sid, "capture_only": True, "n_utterances": 0,
                 "elapsed": 10.0, "audible_s": 9.0, "last_line": "",
                 "last_chunk_ago_s": 0.2, "running": True}]
    code, data = livebrief.api(db, rooms, sessions, {},
                               {"kind": "status", "room_id": "rm1"})
    assert code == 200
    room = data["rooms"][0]
    assert room["n_utterances"] == 1
    assert room["last_line"] == "库里的一句"


def test_ignores_full_pipeline_session(db, isolated_paths):
    sid = "LIVE_TEST_FULL"
    _session(db, sid, capture_only=0, duration=10.0)
    (isolated_paths / "audio" / (sid + ".pcm")).write_bytes(_pcm(1.0))
    worker = offline_asr.OfflineAsr(
        engine=FakeEngine(), vad_factory=lambda: FakeVad(16000),
        audio_dir=isolated_paths / "audio")
    out = worker.run_once(sid=sid)
    assert out["sessions"] == 0
    assert _utts(db, sid) == []
