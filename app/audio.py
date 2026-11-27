# -*- coding: utf-8 -*-
"""录音归档：WAV -> m4a，飞书片段 opus，原文件保留与清理。

为什么不是「收尾直接写压缩格式」：ASR/VAD 要的是 16k 单声道 PCM，采集链路
不能为了省磁盘去改采样率或只存人声段——那会破坏时间轴，也让「当时确实安静」
不再是可核对的证据。所以策略是：采集期照旧落 PCM/WAV，收尾后异步转一份
m4a 作为长期可播的归档，原 WAV 按保留期删除。

m4a（AAC-LC）而不是 opus 作为归档：Chrome/Edge/Safari/飞书 WebView 都能直接
播，飞书妙记也吃常见音视频格式；opus 只在「飞书语音片段」这种短片段上按需生成。
32 kbps 单声道对语音足够，13 小时一场约 187 MB（WAV 是 1.49 GB）。
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

from . import logs, store
from .config import (AUDIO_AAC_BITRATE, AUDIO_DIR, AUDIO_OPUS_BITRATE,
                     AUDIO_RETENTION_DAYS, CLIPS_DIR, SAMPLE_RATE,
                     WAV_RETENTION_DAYS)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_FFMPEG: str | None | bool = False      # False = 还没探测过
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()

AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}


def ffmpeg_path() -> str | None:
    """优先 LIVENOTES_FFMPEG，其次 PATH。探测结果缓存，避免每场都查一次。"""
    global _FFMPEG
    if _FFMPEG is not False:
        return _FFMPEG or None
    cand = (os.environ.get("LIVENOTES_FFMPEG") or "").strip() or shutil.which("ffmpeg")
    _FFMPEG = cand or ""
    return cand or None


def content_type(path: Path | str) -> str:
    return AUDIO_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def is_audio(path: Path | str) -> bool:
    return Path(path).suffix.lower() in AUDIO_TYPES


def mp4_has_moov(path: Path | str) -> bool:
    """MP4/M4A 是否完整：能顺着顶层 box 走到 moov。

    转码中途被杀（桌面壳退出会连 ffmpeg 一起回收）留下的是 ftyp + 半截
    mdat，没有 moov，播放器和 ffmpeg 都解不了。只看「存在且非空」会把
    这种半成品当成归档，等原 WAV 过保留期被清理就真丢了。
    """
    try:
        path = Path(path)
        size = path.stat().st_size
        if size < 16:
            return False
        with open(path, "rb") as f:
            off = 0
            while off + 8 <= size:
                f.seek(off)
                head = f.read(8)
                if len(head) < 8:
                    return False
                box = struct.unpack(">I", head[:4])[0]
                kind = head[4:8]
                if box == 1:                       # 64 位扩展长度
                    ext = f.read(8)
                    if len(ext) < 8:
                        return False
                    box = struct.unpack(">Q", ext)[0]
                elif box == 0:                     # 一直到文件末尾
                    box = size - off
                if kind == b"moov":
                    return box >= 8 and off + box <= size
                if box < 8:
                    return False
                off += box
        return False
    except OSError:
        return False


def _run(args: list[str], timeout: float = 1800.0) -> tuple[bool, str]:
    ff = ffmpeg_path()
    if not ff:
        return False, "ffmpeg 不在 PATH（或 LIVENOTES_FFMPEG 没配）"
    try:
        p = subprocess.run([ff] + args, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "转码超时"
    except OSError as e:
        return False, str(e)
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    return p.returncode == 0, err[-600:]


def _input_args(src: Path) -> list[str]:
    if src.suffix.lower() == ".pcm":
        return ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1"]
    return []


def to_m4a(src: Path | str, dst: Path | str, bitrate: str | None = None) -> tuple[bool, str]:
    """转 AAC-LC 单声道 m4a。保留 +faststart，网页拖动时不用等整段下载。

    先写同目录临时文件，确认完整后再原子替换 dst：桌面壳退出会连 ffmpeg
    一起回收，直接写 dst 会留下没有 moov 的半截文件，下次启动还会被
    当成「已经转过了」。
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=dst.stem + ".", suffix=".part.m4a",
                                    dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        args = (["-hide_banner", "-loglevel", "error", "-y"] + _input_args(src)
                + ["-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                   "-c:a", "aac", "-b:a", bitrate or AUDIO_AAC_BITRATE,
                   "-movflags", "+faststart", str(tmp)])
        ok, err = _run(args)
        if not ok:
            return False, err
        if not tmp.is_file() or tmp.stat().st_size <= 0:
            return False, "转码没有输出文件"
        if not mp4_has_moov(tmp):
            return False, "转码输出不完整（缺少 moov）"
        try:
            os.replace(tmp, dst)
        except OSError as e:
            return False, str(e)
        return True, ""
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- 切片

# 飞书云空间单文件上限实测恰好 20MB（2026-11-27：20,971,521 被拒、
# 20,971,520 成功，prepare/part/finish 分片上传也一样被挡）。
# 长场次只能按时间轴切片，每段都落在上限内。
SPLIT_SAFETY = 0.9


def _parse_hms(text: str) -> float:
    """``01:02:03.45`` / ``123.4`` -> 秒。解析失败返回 0。"""
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    try:
        parts = raw.split(":")
        if len(parts) == 3:
            return (float(parts[0]) * 3600 + float(parts[1]) * 60
                    + float(parts[2]))
        return float(raw)
    except ValueError:
        return 0.0


def audio_duration(path: Path | str) -> float:
    """读音频时长（秒）：优先 ffprobe，没有就解析 ffmpeg 的 stderr。"""
    src = Path(path)
    ff = ffmpeg_path()
    if not ff:
        return 0.0
    probe = shutil.which("ffprobe") or ""
    if not probe:
        cand = Path(ff).with_name("ffprobe" + (".exe" if os.name == "nt" else ""))
        probe = str(cand) if cand.is_file() else ""
    try:
        if probe:
            p = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=60, creationflags=_CREATE_NO_WINDOW)
            return _parse_hms((p.stdout or b"").decode("utf-8", "replace"))
        p = subprocess.run([ff, "-hide_banner", "-i", str(src)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=60, creationflags=_CREATE_NO_WINDOW)
        for line in (p.stderr or b"").decode("utf-8", "replace").splitlines():
            line = line.strip()
            if line.startswith("Duration:"):
                return _parse_hms(line.split("Duration:", 1)[1].split(",")[0])
        return 0.0
    except (OSError, subprocess.TimeoutExpired):
        return 0.0


def split_m4a(src: Path | str, limit_bytes: int,
              out_dir: Path | str | None = None) -> tuple[list[Path], str]:
    """按大小把 m4a 切成若干段，每段不超过 ``limit_bytes``。

    飞书云空间单文件上限 20MB，分片上传也绕不过；长场次只能切片。
    用 ``-c copy`` 按时间轴切，不重新编码，音质不变。
    返回 ``(parts, error)``；任何一段失败都返回空列表，已生成的文件由调用方清理。
    """
    src = Path(src)
    if src.suffix.lower() not in (".m4a", ".mp4"):
        # .aac 通常是裸 ADTS，没有 moov，切出来也不是可校验的 MP4；先转 m4a。
        return [], "只有 m4a/mp4 能切片（当前 %s）" % (src.suffix or "无后缀")
    if not src.is_file() or src.stat().st_size <= 0:
        return [], "音频文件不存在或为空"
    size = src.stat().st_size
    target = max(1, int(int(limit_bytes) * SPLIT_SAFETY))
    n = max(2, (size + target - 1) // target)
    total = audio_duration(src)
    if total <= 0:
        return [], "读不出音频时长，无法切片"
    out = Path(out_dir) if out_dir else src.parent
    out.mkdir(parents=True, exist_ok=True)
    seg = total / n
    parts: list[Path] = []
    for i in range(n):
        part = out / ("%s_%02d%s" % (src.stem, i + 1, src.suffix))
        args = (["-hide_banner", "-loglevel", "error", "-y",
                 "-ss", "%.3f" % (i * seg), "-i", str(src),
                 "-t", "%.3f" % seg, "-map", "0:a:0", "-c", "copy",
                 "-avoid_negative_ts", "make_zero", str(part)])
        ok, err = _run(args, timeout=600)
        if not ok or not part.is_file() or part.stat().st_size <= 0:
            return [], "第 %d 段切片失败：%s" % (i + 1, err or "没有输出")
        if part.stat().st_size > int(limit_bytes):
            return [], "第 %d 段 %d MB 仍超过上限" % (
                i + 1, part.stat().st_size // (1024 * 1024))
        if not mp4_has_moov(part):
            return [], "第 %d 段不完整（缺少 moov）" % (i + 1)
        parts.append(part)
    return parts, ""


def to_opus(src: Path | str, dst: Path | str, bitrate: str | None = None) -> tuple[bool, str]:
    """转 Ogg Opus，给飞书语音消息用。短片段才走这里，不做整场归档。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = (["-hide_banner", "-loglevel", "error", "-y"] + _input_args(src)
            + ["-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-c:a", "libopus", "-b:a", bitrate or AUDIO_OPUS_BITRATE,
               "-vbr", "on", "-application", "voip", str(dst)])
    return _run(args)


# ---------------------------------------------------------------- 归档

def transcode_session(sid: str, wav_path: Path | str | None = None,
                      conn=None) -> Path | None:
    """把一场的 WAV/PCM 转成 m4a，成功后更新 sessions.audio_path。

    失败不改数据库：WAV 仍然可播，只是没省下空间。调用方（收尾线程）不能
    因为转码失败把「录音落盘」算失败。
    """
    own = conn is None
    conn = conn or store.connect()
    try:
        s = store.get_session(conn, sid)
        if not s:
            return None
        src = Path(str(wav_path or s.get("audio_original_path")
                       or s.get("audio_path") or ""))
        if not src.is_file() or src.stat().st_size <= 0:
            return None
        if src.suffix.lower() not in (".wav", ".pcm"):
            return None
        dst = src.parent / (sid + ".m4a")
        if dst.is_file() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
            # 已经转过了（服务重启/重复收尾），不重复跑 ffmpeg。
            # 但必须确认它是完整 MP4：转码中途被杀留下的是没有 moov 的半截
            # 文件，把它当成品写库会让这一场永远播不出来。
            if mp4_has_moov(dst):
                _mark(conn, sid, dst, src)
                return dst
            logs.log_event("audio.archive_invalid", level="warn", session_id=sid,
                           path=str(dst))
        ok, err = to_m4a(src, dst)
        if not ok or not dst.is_file() or dst.stat().st_size <= 0:
            logs.log_event("audio.transcode_failed", level="warn", session_id=sid,
                           src=str(src), error=err)
            _fallback_to_original(conn, sid, src, dst)
            return None
        _mark(conn, sid, dst, src)
        logs.log_event("audio.transcoded", session_id=sid, src=str(src),
                       dst=str(dst), bytes=dst.stat().st_size)
        # 骨架发布时可能只有 WAV，m4a 出来后补发一次，云端归档才有东西可传。
        try:
            from . import feishu_publish
            if feishu_publish.has_published(conn, sid):
                feishu_publish.publish_session_async(sid, only_if_published=True)
        except Exception:
            traceback.print_exc()
        return dst
    except Exception:
        traceback.print_exc()
        return None
    finally:
        if own:
            conn.close()


def _mark(conn, sid: str, dst: Path, src: Path) -> None:
    store.set_audio_meta(conn, sid, audio_path=str(dst), audio_format="m4a",
                         audio_bytes=dst.stat().st_size,
                         audio_transcoded_at=store.now(),
                         audio_original_path=str(src))


def _fallback_to_original(conn, sid: str, src: Path, failed: Path) -> None:
    """归档补不出来时退回原 WAV：宁可文件大，也不能让库指向播不了的 m4a。"""
    if src.suffix.lower() != ".wav":
        return
    s = store.get_session(conn, sid) or {}
    if str(s.get("audio_path") or "") != str(failed):
        return
    try:
        size = src.stat().st_size
    except OSError:
        return
    store.set_audio_meta(conn, sid, audio_path=str(src), audio_format="wav",
                         audio_bytes=size, audio_transcoded_at=None,
                         audio_original_path=None)
    logs.log_event("audio.archive_fallback", level="warn", session_id=sid,
                   path=str(src), archive=str(failed))


def schedule_transcode(sid: str, wav_path: Path | str | None = None) -> None:
    """异步转码。同一场只允许一个线程，收尾/重启重复调用不会叠加。"""
    sid = str(sid or "")
    if not sid:
        return
    with _LOCK:
        if sid in _INFLIGHT:
            return
        _INFLIGHT.add(sid)

    def run() -> None:
        try:
            transcode_session(sid, wav_path)
        except Exception:
            traceback.print_exc()
        finally:
            with _LOCK:
                _INFLIGHT.discard(sid)

    threading.Thread(target=run, name="audio-transcode-" + sid, daemon=True).start()


def cleanup_originals(conn=None, days: int | None = None) -> int:
    """删除已转码的原始 WAV。只删「当前可播文件不是它自己」的场次。"""
    days = WAV_RETENTION_DAYS if days is None else int(days)
    if days < 0:
        return 0
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        cutoff = time.time() - max(0, days) * 86400
        rows = conn.execute(
            "SELECT id, audio_path, audio_original_path FROM sessions "
            "WHERE audio_original_path IS NOT NULL AND audio_original_path <> ''"
        ).fetchall()
        for r in rows:
            orig = Path(str(r["audio_original_path"]))
            cur = Path(str(r["audio_path"] or ""))
            if not orig.is_file():
                store.set_audio_meta(conn, r["id"], audio_original_path=None)
                continue
            try:
                same = cur and cur.is_file() and cur.resolve() == orig.resolve()
                if same or not cur.is_file():
                    continue
                if cur.suffix.lower() in (".m4a", ".mp4") and not mp4_has_moov(cur):
                    # 归档本身是半截文件：原 WAV 是唯一能播的那份，先留着。
                    logs.log_event("audio.original_kept", level="warn",
                                   session_id=r["id"], path=str(orig),
                                   archive=str(cur))
                    continue
                if orig.stat().st_mtime > cutoff:
                    continue
                orig.unlink(missing_ok=True)
                store.set_audio_meta(conn, r["id"], audio_original_path=None)
                n += 1
                logs.log_event("audio.original_removed", session_id=r["id"],
                               path=str(orig))
            except OSError:
                traceback.print_exc()
        return n
    finally:
        if own:
            conn.close()


def cleanup_expired(conn=None, days: int | None = None) -> int:
    """按保留期删除归档音频。默认关闭（0 = 永久保留）。"""
    days = AUDIO_RETENTION_DAYS if days is None else int(days)
    if days <= 0:
        return 0
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT id, audio_path, audio_original_path FROM sessions "
            "WHERE audio_path IS NOT NULL AND audio_path <> ''"
        ).fetchall()
        for r in rows:
            p = Path(str(r["audio_path"]))
            try:
                if not p.is_file() or p.stat().st_mtime > cutoff:
                    continue
                p.unlink(missing_ok=True)
                orig = Path(str(r["audio_original_path"] or ""))
                if orig and orig != p:
                    orig.unlink(missing_ok=True)
                store.drop_audio(conn, r["id"])
                n += 1
                logs.log_event("audio.expired", session_id=r["id"], path=str(p))
            except OSError:
                traceback.print_exc()
        return n
    finally:
        if own:
            conn.close()


def backfill_wavs(conn=None, limit: int = 0) -> int:
    """把历史 WAV 逐场转成 m4a；顺带修复被转码中断弄坏的归档。

    老库没有 audio_original_path，转码成功后由 _mark 补上；这样保留期清理
    才有依据。limit=0 表示不限场次（CLI/启动补转用）。
    """
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        sql = ("SELECT id, audio_path, audio_original_path FROM sessions "
               "WHERE (audio_path LIKE '%.wav' "
               "       AND (audio_format IS NULL OR audio_format = 'wav')) "
               "   OR (audio_path LIKE '%.m4a' AND audio_original_path IS NOT NULL "
               "       AND audio_original_path <> '') "
               "ORDER BY started_at ASC")
        if limit and limit > 0:
            sql += " LIMIT %d" % int(limit)
        rows = conn.execute(sql).fetchall()
        for r in rows:
            ap = str(r["audio_path"] or "")
            is_m4a = ap.lower().endswith((".m4a", ".mp4"))
            if is_m4a and mp4_has_moov(ap):
                continue          # 归档完整，不用每次都过一遍 ffmpeg
            # 修复时要从原 WAV 重转，不能把坏掉的 m4a 当输入
            hint = r["audio_original_path"] if is_m4a else r["audio_path"]
            if transcode_session(r["id"], hint, conn=conn):
                n += 1
        return n
    finally:
        if own:
            conn.close()


def start_backfill() -> threading.Thread:
    """启动后台补转历史 WAV；只跑一次，失败不影响服务。"""
    def run() -> None:
        try:
            n = backfill_wavs()
            if n:
                logs.log_event("audio.backfill", count=n)
        except Exception:
            traceback.print_exc()

    t = threading.Thread(target=run, name="audio-backfill", daemon=True)
    t.start()
    return t


def cleanup_clips(max_age_s: float = 86400.0) -> int:
    """片段是缓存，不是资产：超过一天就删，避免 data/clips 无限增长。"""
    if not CLIPS_DIR.is_dir():
        return 0
    cutoff = time.time() - max(60.0, float(max_age_s))
    n = 0
    for p in CLIPS_DIR.glob("*.opus"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n


def cleanup_partials(max_age_s: float = 86400.0) -> int:
    """转码临时文件是垃圾：进程被杀留下的 *.part.m4a 超过一天就删。"""
    if not AUDIO_DIR.is_dir():
        return 0
    cutoff = time.time() - max(60.0, float(max_age_s))
    n = 0
    for p in AUDIO_DIR.glob("*.part.m4a"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n


def start_sweeper(interval_s: float = 6 * 3600) -> threading.Thread:
    """定期清理原 WAV / 过期归档 / 片段缓存 / 分享包 / 报告。启动时也跑一次。"""
    def loop() -> None:
        while True:
            try:
                removed = cleanup_originals()
                expired = cleanup_expired()
                clips = cleanup_clips()
                parts = cleanup_partials()
                # share/report 都依赖 audio（模块级），只能在这里延迟导入。
                from . import report as _report, share as _share
                packages = _share.cleanup()
                reports = _report.cleanup()
                if removed or expired or clips or parts or packages or reports:
                    logs.log_event("audio.sweep", originals=removed,
                                   expired=expired, clips=clips, parts=parts,
                                   packages=packages, reports=reports)
            except Exception:
                traceback.print_exc()
            time.sleep(max(300.0, float(interval_s)))

    t = threading.Thread(target=loop, name="audio-sweeper", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 切片

def _slice_wav(src: Path, dst: Path, start: float, duration: float) -> bool:
    try:
        with wave.open(str(src), "rb") as w:
            rate = w.getframerate()
            a = int(max(0.0, start) * rate)
            b = a + int(max(0.2, duration) * rate)
            w.setpos(min(a, w.getnframes()))
            data = w.readframes(max(0, min(b, w.getnframes()) - a))
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(data)
        return bool(data)
    except (OSError, EOFError, wave.Error):
        traceback.print_exc()
        return False


def _slice_pcm(src: Path, dst: Path, start: float, duration: float) -> bool:
    """spool 是 16k/单声道/s16le：按样本数切，比 ffmpeg seek 更准也更快。"""
    try:
        offset = int(max(0.0, start) * SAMPLE_RATE) * 2
        nbytes = int(max(0.2, duration) * SAMPLE_RATE) * 2
        with open(src, "rb") as f:
            f.seek(offset)
            data = f.read(nbytes)
        if not data:
            return False
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(data)
        return True
    except OSError:
        traceback.print_exc()
        return False


def extract_opus(src: Path | str, dst: Path | str, start: float,
                 duration: float) -> tuple[bool, str]:
    """从 WAV/PCM/m4a/opus 切一段，输出飞书语音用的 opus。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.2, float(duration))
    if src.suffix.lower() in (".wav", ".pcm"):
        with tempfile.TemporaryDirectory(prefix="livenotes-clip-") as td:
            tmp = Path(td) / "slice.wav"
            ok = (_slice_pcm(src, tmp, start, duration) if src.suffix.lower() == ".pcm"
                  else _slice_wav(src, tmp, start, duration))
            if not ok:
                return False, "这一段音频读不出来（可能已经写完了或文件被清理）"
            return to_opus(tmp, dst)
    args = (["-hide_banner", "-loglevel", "error", "-y",
             "-ss", "%.3f" % max(0.0, float(start)),
             "-t", "%.3f" % duration, "-i", str(src), "-vn",
             "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-c:a", "libopus", "-b:a", AUDIO_OPUS_BITRATE,
             "-vbr", "on", "-application", "voip", str(dst)])
    return _run(args, timeout=120.0)


def extract_m4a(src: Path | str, dst: Path | str, start: float,
                duration: float) -> tuple[bool, str]:
    """从 WAV/PCM/m4a/opus 切一段，输出交付包用的 AAC m4a。

    和 extract_opus 的区别只有一个编码器：客户拿到的片段要能在 Windows
    播放器/微信/飞书里直接播，opus 在部分环境下打不开。
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.2, float(duration))
    if src.suffix.lower() in (".wav", ".pcm"):
        with tempfile.TemporaryDirectory(prefix="livenotes-clip-") as td:
            tmp = Path(td) / "slice.wav"
            ok = (_slice_pcm(src, tmp, start, duration) if src.suffix.lower() == ".pcm"
                  else _slice_wav(src, tmp, start, duration))
            if not ok:
                return False, "这一段音频读不出来（可能已经写完了或文件被清理）"
            return to_m4a(tmp, dst)
    args = (["-hide_banner", "-loglevel", "error", "-y",
             "-ss", "%.3f" % max(0.0, float(start)),
             "-t", "%.3f" % duration, "-i", str(src), "-vn",
             "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-c:a", "aac", "-b:a", AUDIO_AAC_BITRATE,
             "-movflags", "+faststart", str(dst)])
    return _run(args, timeout=120.0)


def slice_wav_bytes(path: Path | str, t0: float, t1: float) -> bytes | None:
    """给 /api/audio?t_start=&t_end= 的兼容出口：非 WAV 文件用 ffmpeg 现切。"""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            import io
            with wave.open(str(path), "rb") as w:
                rate, ch, sw, n = (w.getframerate(), w.getnchannels(),
                                   w.getsampwidth(), w.getnframes())
                a = min(n, int(max(0, t0) * rate))
                b = min(n, int(max(t0 + 0.2, t1) * rate))
                w.setpos(a)
                raw = w.readframes(max(0, b - a))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setnchannels(ch); out.setsampwidth(sw); out.setframerate(rate)
                out.writeframes(raw)
            return buf.getvalue()
        except (OSError, EOFError, wave.Error):
            return None
    ff = ffmpeg_path()
    if not ff:
        return None
    # 输出到临时文件而不是 pipe：WAV 头里的长度需要回写，pipe 上只能写
    # 0xFFFFFFFF，浏览器拿到后读不出时长。
    fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="slice_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    args = [ff, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "%.3f" % max(0.0, float(t0)),
            "-t", "%.3f" % max(0.2, float(t1) - float(t0)),
            "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-acodec", "pcm_s16le", str(tmp_path)]
    try:
        p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=300, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        tmp_path.unlink(missing_ok=True)
        return None
    if p.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        return None
    try:
        data = tmp_path.read_bytes()
    except OSError:
        data = b""
    finally:
        tmp_path.unlink(missing_ok=True)
    return data or None
