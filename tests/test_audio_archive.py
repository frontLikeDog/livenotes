# -*- coding: utf-8 -*-
"""录音归档：WAV -> m4a、保留期清理、PCM 切片与内容类型。"""
from __future__ import annotations

import math
import os
import struct
import time
import wave
from pathlib import Path

import pytest
from helpers import add_session

from app import audio, store


def _write_wav(path: Path, seconds: float = 1.0, freq: float = 440.0) -> None:
    n = int(seconds * 16000)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * freq * i / 16000)))
            for i in range(n)))


def test_content_type_by_suffix():
    assert audio.content_type("x.wav") == "audio/wav"
    assert audio.content_type("x.m4a") == "audio/mp4"
    assert audio.content_type("x.opus") == "audio/ogg"
    assert audio.content_type("x.bin") == "application/octet-stream"


def test_slice_pcm_is_sample_accurate(isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    # 2 秒：前半 1000、后半 -1000，便于检查切点
    samples = [1000] * 16000 + [-1000] * 16000
    pcm.write_bytes(b"".join(struct.pack("<h", x) for x in samples))
    out = isolated_paths / "slice.wav"
    assert audio._slice_pcm(pcm, out, 0.5, 1.0)
    with wave.open(str(out), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        assert w.getnframes() == 16000
        first = struct.unpack("<h", w.readframes(1))[0]
    assert first == 1000


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_transcode_updates_session_and_keeps_original(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))

    out = audio.transcode_session("S_TEST", wav, conn=db)
    assert out and out.suffix == ".m4a" and out.stat().st_size > 0

    s = store.get_session(db, "S_TEST")
    assert s["audio_path"] == str(out)
    assert s["audio_format"] == "m4a"
    assert s["audio_original_path"] == str(wav)
    assert int(s["audio_bytes"]) == out.stat().st_size
    assert wav.is_file()          # 保留期内不删原文件


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_cleanup_originals_only_after_archive(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    out = audio.transcode_session("S_TEST", wav, conn=db)
    assert out

    # days=-1 表示永久保留
    assert audio.cleanup_originals(db, days=-1) == 0
    assert wav.is_file()

    # days=0 且归档可播：删原 WAV，但不清 audio_path
    assert audio.cleanup_originals(db, days=0) == 1
    assert not wav.exists()
    s = store.get_session(db, "S_TEST")
    assert s["audio_path"] == str(out)
    assert s["audio_original_path"] is None


def test_cleanup_originals_keeps_when_archive_missing(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 0.5)
    add_session(db, "S_TEST", audio_path=str(wav))
    store.set_audio_meta(db, "S_TEST", audio_format="wav",
                         audio_original_path=str(wav))
    # audio_path 仍指向 WAV，说明还没转码，不能删
    assert audio.cleanup_originals(db, days=0) == 0
    assert wav.is_file()


# ---------------------------------------------------------------- 归档完整性
#
# 桌面壳退出会连 ffmpeg 一起回收：转码写到一半被杀，留下没有 moov 的
# 半截 m4a。归档逻辑不能把这种文件当成成品，更不能因此删掉原 WAV。


def _mp4_blob(moov: bool) -> bytes:
    """最小可解析的 MP4：ftyp + mdat（+ moov）。"""
    ftyp = struct.pack(">I", 24) + b"ftypM4A " + b"\x00" * 12
    mdat = struct.pack(">I", 40) + b"mdat" + b"\x00" * 32
    data = ftyp + mdat
    if moov:
        data += struct.pack(">I", 8) + b"moov"
    return data


def test_mp4_has_moov_detects_truncation(tmp_path):
    good = tmp_path / "good.m4a"
    good.write_bytes(_mp4_blob(True))
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(_mp4_blob(False))
    assert audio.mp4_has_moov(good)
    assert not audio.mp4_has_moov(bad)
    assert not audio.mp4_has_moov(tmp_path / "missing.m4a")


def test_to_m4a_keeps_old_archive_when_ffmpeg_dies(isolated_paths, monkeypatch):
    """ffmpeg 中途被杀：不能把半截输出留在正式路径上。"""
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 0.5)
    dst = isolated_paths / "audio" / "S_TEST.m4a"
    dst.write_bytes(b"OLD-ARCHIVE")

    def fake_run(args, timeout=1800.0):
        Path(args[-1]).write_bytes(b"PARTIAL")     # 模拟写到一半被杀
        return False, "ffmpeg killed"

    monkeypatch.setattr(audio, "_run", fake_run)
    ok, _err = audio.to_m4a(wav, dst)
    assert not ok
    assert dst.read_bytes() == b"OLD-ARCHIVE"
    assert not list(dst.parent.glob("*.part.m4a"))


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_transcode_replaces_truncated_archive(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    bad = wav.with_name("S_TEST.m4a")
    bad.write_bytes(_mp4_blob(False))
    os.utime(bad, (time.time() + 5, time.time() + 5))   # 比 WAV 新
    store.set_audio_meta(db, "S_TEST", audio_path=str(bad), audio_format="m4a",
                         audio_bytes=bad.stat().st_size,
                         audio_original_path=str(wav))
    out = audio.transcode_session("S_TEST", conn=db)
    assert out == bad and out.is_file()
    assert audio.mp4_has_moov(out)
    assert out.stat().st_size > len(_mp4_blob(False))
    assert store.get_session(db, "S_TEST")["audio_path"] == str(out)


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_backfill_repairs_truncated_archive(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 1.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    bad = wav.with_name("S_TEST.m4a")
    bad.write_bytes(_mp4_blob(False))
    store.set_audio_meta(db, "S_TEST", audio_path=str(bad), audio_format="m4a",
                         audio_bytes=bad.stat().st_size,
                         audio_original_path=str(wav))
    assert audio.backfill_wavs(db) == 1          # 从原 WAV 重转修复
    assert audio.mp4_has_moov(bad)
    assert audio.backfill_wavs(db) == 0          # 已完整，不再重复跑


def test_failed_repair_falls_back_to_wav(db, isolated_paths, monkeypatch):
    """归档补不出来时不能让库继续指向半截 m4a——退回原 WAV 至少能播。"""
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 0.5)
    bad = wav.with_name("S_TEST.m4a")
    bad.write_bytes(_mp4_blob(False))
    add_session(db, "S_TEST", audio_path=str(bad))
    store.set_audio_meta(db, "S_TEST", audio_format="m4a",
                         audio_bytes=bad.stat().st_size,
                         audio_original_path=str(wav))
    monkeypatch.setattr(audio, "to_m4a", lambda *a, **kw: (False, "no ffmpeg"))
    assert audio.transcode_session("S_TEST", conn=db) is None
    s = store.get_session(db, "S_TEST")
    assert s["audio_path"] == str(wav)
    assert s["audio_format"] == "wav"
    assert s["audio_original_path"] is None


def test_cleanup_originals_keeps_when_archive_truncated(db, isolated_paths):
    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 0.5)
    bad = wav.with_name("S_TEST.m4a")
    bad.write_bytes(_mp4_blob(False))
    add_session(db, "S_TEST", audio_path=str(bad))
    store.set_audio_meta(db, "S_TEST", audio_format="m4a",
                         audio_original_path=str(wav))
    # 归档不完整：原 WAV 是唯一能播的，保留期到了也不能删
    assert audio.cleanup_originals(db, days=0) == 0
    assert wav.is_file()


def test_cleanup_partials_removes_stale_files(isolated_paths):
    old = isolated_paths / "audio" / "S_TEST.abc.part.m4a"
    old.write_bytes(b"x")
    stamp = time.time() - 2 * 86400
    os.utime(old, (stamp, stamp))
    fresh = isolated_paths / "audio" / "S_TEST.def.part.m4a"
    fresh.write_bytes(b"x")
    assert audio.cleanup_partials() == 1
    assert not old.exists() and fresh.exists()


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_slice_m4a_returns_wav_for_reader(db, isolated_paths):
    """归档成 m4a 后，reader 的 t_start/t_end 仍要拿到能直接播的 WAV。"""
    import io

    wav = isolated_paths / "audio" / "S_TEST.wav"
    _write_wav(wav, 2.0)
    add_session(db, "S_TEST", audio_path=str(wav))
    m4a = audio.transcode_session("S_TEST", wav, conn=db)
    assert m4a

    data = audio.slice_wav_bytes(m4a, 0.5, 1.0)
    assert data and data[:4] == b"RIFF"
    with wave.open(io.BytesIO(data), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
        # AAC 帧边界有几十毫秒误差，不要求精确到采样点
        assert 0.5 * 16000 * 0.8 <= w.getnframes() <= 0.5 * 16000 * 1.2


@pytest.mark.skipif(not audio.ffmpeg_path(), reason="没有 ffmpeg")
def test_extract_opus_from_pcm(isolated_paths):
    pcm = isolated_paths / "audio" / "S_TEST.pcm"
    pcm.write_bytes(b"\x00\x00" * 16000 * 3)
    out = isolated_paths / "clip.opus"
    ok, err = audio.extract_opus(pcm, out, 0.5, 1.0)
    assert ok, err
    assert out.stat().st_size > 0
    assert audio.content_type(out) == "audio/ogg"
