# -*- coding: utf-8 -*-
"""离线补转写：给「仅采集」场次补逐字稿。

仅采集节点只把 PCM 写进 ``data/audio/<session>.pcm``，不加载模型，也不产生
utterances。总结需要文字，所以这里用一个**独立进程**做增量转写：

    spool / WAV  ->  silero VAD  ->  SenseVoice  ->  utterances / embeddings

和实时链路的区别：

* 不跟采集抢线程。采集仍是仅采集，CPU/内存出问题只会杀掉这个进程，
  录音和飞书机器人不受影响。
* VAD 在进程内持续保留，按 spool 的新增字节增量喂；只有场次收尾后才 flush。
  这样句子的时间戳和断句跟实时链路一致，不会每轮都在边界上切断。
* 进度落在 ``offline_asr`` 表。进程重启从上次的 processed_s 继续；
  极少数跨越重启的句子可能断成两段，但不会整场重跑。
* 声纹照常提取并保存，场次收尾后复用 ``recluster`` 做全场重分人；
  直播中的标签先用「说话人N」，不影响总结。
* 补完字后，通过引擎的 ``POST /api/sessions/finalize`` 触发正常的收尾链路
  （总结 → 发布），不在本进程里加载 LLM/飞书。触发状态落在
  ``offline_asr.finalize_state``，进程重启不会重复总结，失败会退避重试。

默认只处理最近 ``LIVENOTES_OFFLINE_ASR_DAYS`` 天内的场次；历史场次用
``tools/offline_asr.py --backfill --session SID`` 显式补。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import analysis, audio as _audio, logs, recluster, store
from .config import (
    AUDIO_DIR,
    LIVE_MAX_SEGMENT_S,
    LIVE_MAX_SPEAKERS,
    LIVE_MIN_SPEECH_S,
    OFFLINE_ASR_DAYS,
    OFFLINE_ASR_FINALIZE,
    OFFLINE_ASR_FINALIZE_RETRY_S,
    OFFLINE_ASR_INTERVAL_S,
    OFFLINE_ASR_MAX_FEED_S,
    OFFLINE_ASR_SPEAKERS,
    PORT,
    SAMPLE_RATE,
    VAD_MODEL,
)
from .live import Clusters, Engine, SPK_MIN_SECONDS, WINDOW, _meaningful

PCM_BYTES_PER_S = SAMPLE_RATE * 2


def _default_vad():
    """和 LiveSession 相同的 silero 参数；缓冲给足 60 秒。"""
    from sherpa_onnx import VadModelConfig, VoiceActivityDetector

    cfg = VadModelConfig()
    cfg.silero_vad.model = str(VAD_MODEL)
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.55
    cfg.silero_vad.min_speech_duration = LIVE_MIN_SPEECH_S
    cfg.silero_vad.max_speech_duration = LIVE_MAX_SEGMENT_S
    cfg.silero_vad.window_size = WINDOW
    cfg.sample_rate = SAMPLE_RATE
    cfg.num_threads = 1
    cfg.debug = False
    return VoiceActivityDetector(cfg, buffer_size_in_seconds=60)


def _log(msg: str, **fields: Any) -> None:
    try:
        logs.log_event("offline_asr", msg=msg, **fields)
    except Exception:
        pass
    print("[offline-asr] " + msg, flush=True)


class _State:
    """一场的增量状态。只有 Worker 的线程会碰它，不需要锁。"""

    def __init__(self, sid: str, start_s: float, *,
                 engine: Any, vad_factory: Callable[[], Any],
                 speakers: bool, audio_dir: Path,
                 start_n: int = 0) -> None:
        self.sid = sid
        self.engine = engine
        self.audio_dir = Path(audio_dir)
        self.vad = vad_factory()
        # VAD 的 seg.start 是「喂进这个 VAD 的第几个采样」。从 processed_s
        # 重启时，base_s 把相对下标换回场次内的绝对秒数。
        self.base_s = max(0.0, float(start_s))
        self.read_s = self.base_s       # 文件里下一个要读的位置
        self.fed_s = self.base_s        # 已喂进 VAD 的整窗位置
        self.carry = None               # 不足一个 VAD 窗的尾样本（numpy）
        self.speakers = bool(speakers)
        self.clusters = None
        self.labels: dict[str, str] = {}
        self.n = max(0, int(start_n or 0))
        self.files_done = False

    # -------------------------------------------------------------- 读音频
    def _feed_bytes(self, pcm: bytes) -> None:
        import numpy as np

        if not pcm:
            return
        x = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
        if self.carry is not None and len(self.carry):
            x = np.concatenate([self.carry, x])
        n = (len(x) // WINDOW) * WINDOW
        for i in range(0, n, WINDOW):
            self.vad.accept_waveform(x[i:i + WINDOW])
        self.carry = x[n:]
        self.fed_s += n / SAMPLE_RATE

    def _feed_pcm(self, path: Path, max_s: float) -> int:
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        start = int(round(self.read_s * PCM_BYTES_PER_S))
        end = min(size - (size % 2), start + int(max_s * PCM_BYTES_PER_S))
        if end <= start:
            return 0
        with open(path, "rb") as f:
            f.seek(start)
            pcm = f.read(end - start)
        self.read_s += len(pcm) / PCM_BYTES_PER_S
        self._feed_bytes(pcm)
        return len(pcm)

    def _feed_wav(self, path: Path, max_s: float) -> int:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1
                    or w.getsampwidth() != 2):
                return self._feed_ffmpeg(path, max_s, self.read_s)
            total = w.getnframes()
            start = int(round(self.read_s * SAMPLE_RATE))
            if start >= total:
                self.files_done = True
                return 0
            n = min(total - start, int(max_s * SAMPLE_RATE))
            w.setpos(start)
            frames = w.readframes(n)
        self.read_s += len(frames) / PCM_BYTES_PER_S
        self._feed_bytes(frames)
        return len(frames)

    def _feed_ffmpeg(self, path: Path, max_s: float, seek_s: float) -> int:
        """m4a 或非 16k 单声道 WAV：让 ffmpeg 解成 16k/s16le。"""
        ff = _audio.ffmpeg_path()
        if not ff:
            raise RuntimeError("没有 ffmpeg，读不了 %s" % path.suffix)
        cmd = [ff, "-v", "error"]
        if seek_s > 0:
            cmd += ["-ss", "%.3f" % seek_s]
        cmd += ["-i", str(path), "-f", "s16le", "-ac", "1",
                "-ar", str(SAMPLE_RATE), "-"]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            pcm = p.stdout.read(int(max_s * PCM_BYTES_PER_S)) if p.stdout else b""
        finally:
            try:
                p.kill()
            except OSError:
                pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        if pcm:
            self.read_s += len(pcm) / PCM_BYTES_PER_S
            self._feed_bytes(pcm)
        else:
            self.files_done = True
        return len(pcm)

    # -------------------------------------------------------------- 转写
    def _speaker(self, conn: sqlite3.Connection, emb, dur: float) -> tuple[str, str]:
        if not self.speakers or emb is None:
            return "SPK_00", "说话人"
        import numpy as np

        if self.clusters is None:
            self.clusters = Clusters(np, LIVE_MAX_SPEAKERS)
        spk = self.clusters.assign(emb, dur)
        self.clusters.add(spk, dur)
        label = self.labels.get(spk)
        if not label:
            label = "说话人%d" % (len(self.labels) + 1)
            self.labels[spk] = label
            try:
                store.upsert_speaker(conn, self.sid, spk, label)
            except Exception:
                traceback.print_exc()
        return spk, label

    def _drain(self, conn: sqlite3.Connection) -> int:
        import numpy as np

        inserted = 0
        while not self.vad.empty():
            seg = self.vad.front
            samples = np.array(seg.samples, dtype="float32", copy=True)
            start = int(seg.start)
            self.vad.pop()
            dur = len(samples) / SAMPLE_RATE
            if dur < LIVE_MIN_SPEECH_S:
                continue
            if dur > LIVE_MAX_SEGMENT_S + 1:
                samples = samples[: int((LIVE_MAX_SEGMENT_S + 1) * SAMPLE_RATE)]
                dur = len(samples) / SAMPLE_RATE
            t0 = self.base_s + start / SAMPLE_RATE
            try:
                text = self.engine.transcribe(samples)
            except Exception:
                traceback.print_exc()
                continue
            if not _meaningful(text):
                continue
            # 进程重启/进度回退时可能重新转出同一条：时间戳是稳定的，直接跳过。
            try:
                dup = conn.execute(
                    "SELECT 1 FROM utterances WHERE session_id=? "
                    "AND ABS(t_start-?)<0.05 LIMIT 1",
                    (self.sid, round(t0, 2))).fetchone()
            except sqlite3.Error:
                dup = None
            if dup:
                continue
            try:
                cls = analysis.classify(text)
            except Exception:
                traceback.print_exc()
                cls = {}
            emb = None
            if self.speakers and dur >= SPK_MIN_SECONDS:
                try:
                    # 单线程，直接用共享的 CAM++（内部一把锁），不再为每场
                    # 复制一份 extractor（实测每份约 39MB）。
                    emb = self.engine.embed(samples)
                except Exception:
                    traceback.print_exc()
            spk, label = self._speaker(conn, emb, dur)
            try:
                ids = store.insert_utterances(conn, [{
                    "session_id": self.sid,
                    "t_start": round(t0, 2), "t_end": round(t0 + dur, 2),
                    "speaker_id": spk, "speaker_label": label, "text": text,
                    "asr_conf": 0.9, "overlap": 0,
                    "category": cls.get("category", "substantive"),
                    "value_tag": cls.get("value_tag", "normal"),
                    "importance": cls.get("importance", 0.5),
                    "topic": cls.get("topic", ""),
                    "low_value_reason": cls.get("low_value_reason", ""),
                }])
            except Exception:
                traceback.print_exc()
                continue
            if emb is not None and ids:
                try:
                    store.save_embeddings(
                        conn, self.sid,
                        [(ids[0], np.float16(emb).tobytes(), int(emb.shape[0]))])
                except Exception:
                    traceback.print_exc()
            self.n += 1
            inserted += 1
        return inserted

    # -------------------------------------------------------------- 主循环
    def pump(self, conn: sqlite3.Connection, row: dict,
             max_feed_s: float) -> tuple[bool, int]:
        """补一段。返回 (是否已收尾, 本轮读到的字节数)。"""
        sid = self.sid
        pcm = self.audio_dir / (sid + ".pcm")
        audio = str(row.get("audio_path") or "")
        orig = str(row.get("audio_original_path") or "")
        finished = bool(audio)
        path: Path | None = None
        if finished:
            for cand in (orig, audio):
                if cand and Path(cand).is_file():
                    path = Path(cand)
                    break
            if path is None and pcm.is_file():
                path = pcm
        elif pcm.is_file():
            path = pcm
        if path is None:
            return False, 0

        dur0 = float(row.get("duration_s") or 0)
        if finished and dur0 > 0 and self.read_s > dur0 + 5:
            # 文件被替换/进度来自另一份录音：从头补，已有句子按时间戳去重。
            self.read_s = self.fed_s = self.base_s = 0.0
            self.carry = None

        got = 0
        try:
            if path.suffix.lower() == ".pcm":
                got = self._feed_pcm(path, max_feed_s)
            elif path.suffix.lower() == ".wav":
                got = self._feed_wav(path, max_feed_s)
            else:
                got = self._feed_ffmpeg(path, max_feed_s, self.read_s)
        except Exception as e:
            raise RuntimeError("%s: %s" % (type(e).__name__, e)) from e

        # 收尾后才 flush；直播中的 spool 不能在这里切断句子。
        if finished:
            dur = float(row.get("duration_s") or 0)
            at_end = self.files_done or (dur > 0 and self.read_s >= dur - 0.25)
            if at_end:
                try:
                    self.vad.flush()
                    self._drain(conn)
                except Exception:
                    traceback.print_exc()
                return True, got
        self._drain(conn)
        return False, got


class OfflineAsr:
    """扫描仅采集场次，增量补逐字稿。"""

    def __init__(self, *, engine: Any = None,
                 vad_factory: Callable[[], Any] | None = None,
                 interval: float = OFFLINE_ASR_INTERVAL_S,
                 days: float = OFFLINE_ASR_DAYS,
                 max_feed_s: float = OFFLINE_ASR_MAX_FEED_S,
                 speakers: bool = OFFLINE_ASR_SPEAKERS,
                 audio_dir: Path | None = None,
                 finalize: bool = OFFLINE_ASR_FINALIZE,
                 finalize_retry_s: float = OFFLINE_ASR_FINALIZE_RETRY_S,
                 api_base: str | None = None) -> None:
        self.engine = engine
        self.vad_factory = vad_factory or _default_vad
        self.interval = max(0.5, float(interval))
        self.days = max(0.0, float(days))
        self.max_feed_s = max(5.0, float(max_feed_s))
        self.speakers = bool(speakers)
        self.audio_dir = Path(audio_dir or AUDIO_DIR)
        # 补完自动收尾走引擎的 HTTP 接口：模型和飞书都不进这个进程。
        self.finalize = bool(finalize)
        self.finalize_retry_s = max(30.0, float(finalize_retry_s))
        self.api_base = (api_base or os.environ.get("LIVENOTES_API")
                         or "http://127.0.0.1:%d" % PORT).rstrip("/")
        self.states: dict[str, _State] = {}
        self._loaded = False

    def _ensure_engine(self) -> None:
        if self.engine is None:
            self.engine = Engine()
        if not self._loaded:
            self.engine.load()
            self._loaded = True
            _log("模型已加载", speakers=self.speakers)

    def _targets(self, conn: sqlite3.Connection, *,
                 sid: str | None = None, backfill: bool = False,
                 limit: int = 0) -> list[dict]:
        if sid:
            row = store.get_session(conn, sid)
            rows = [row] if row else []
        elif backfill:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE capture_only=1 "
                "AND deleted_at IS NULL ORDER BY started_at DESC")]
        else:
            since = (datetime.now() - timedelta(days=self.days)).isoformat(
                timespec="seconds")
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sessions WHERE capture_only=1 "
                "AND deleted_at IS NULL "
                "AND (started_at>=? OR audio_path IS NULL) "
                "ORDER BY started_at DESC", (since,))]
        out: list[dict] = []
        for r in rows:
            if not r or int(r.get("capture_only") or 0) != 1:
                continue
            prog = store.get_offline_asr(conn, str(r["id"])) or {}
            if prog.get("status") == "done":
                continue
            if prog.get("status") == "error":
                try:
                    ts = datetime.fromisoformat(str(prog.get("updated_at") or ""))
                    if (datetime.now() - ts).total_seconds() < 600:
                        continue
                except ValueError:
                    pass
            out.append(r)
        return out[:limit] if limit > 0 else out

    # ---------------------------------------------------------- 收尾触发
    def _http_json(self, path: str, payload: dict | None = None,
                   timeout: float = 8.0) -> tuple[int, dict]:
        """调本机引擎。返回 (HTTP 状态, JSON)；4xx/5xx 的 JSON 也照常返回。"""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.api_base + path, data=data,
            method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8") or "{}"
                return int(getattr(r, "status", 200) or 200), json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                body = {}
            return int(e.code), body if isinstance(body, dict) else {}

    def _request_finalize(self, conn: sqlite3.Connection, sid: str) -> None:
        """补完字 → 请引擎排队一次正常的收尾（总结 → 发布）。"""
        if not self.finalize:
            return
        prev = store.get_offline_asr(conn, sid) or {}
        if str(prev.get("finalize_state") or "") in ("queued", "done"):
            return
        try:
            code, out = self._http_json(
                "/api/sessions/finalize",
                {"session_id": sid, "recluster": False})
        except Exception as e:
            msg = "引擎没响应：%s" % e
            store.set_offline_finalize(conn, sid, state="failed", error=msg)
            _log("收尾触发失败 %s: %s" % (sid, msg))
            return
        if code == 200 and out.get("ok"):
            jid = str(out.get("job_id") or "")
            store.set_offline_finalize(conn, sid, state="queued", job=jid,
                                       error="")
            _log("已排队收尾 %s（%s）" % (sid, jid))
            return
        msg = str(out.get("error") or out.get("message") or "HTTP %s" % code)
        if code == 409 and (out.get("not_ready") or out.get("capture_only")):
            # 逐字稿还没入库（极少见：flush 之后可能有短暂延迟）：下一轮再试，
            # 不记成失败，免得退避 10 分钟。
            store.set_offline_finalize(conn, sid, state="", error=msg)
            _log("收尾暂缓 %s: %s" % (sid, msg))
            return
        store.set_offline_finalize(conn, sid, state="failed", error=msg)
        _log("收尾触发被拒绝 %s: %s" % (sid, msg))

    def _poll_finalize(self, conn: sqlite3.Connection) -> int:
        """跟进已触发的收尾；失败的按间隔重试。返回本轮确认完成的场次数。"""
        if not self.finalize:
            return 0
        try:
            rows = store.list_offline_finalize(
                conn, states=("queued", "failed"), limit=8)
        except sqlite3.Error:
            return 0
        done = 0
        for row in rows:
            sid = str(row.get("session_id") or "")
            state = str(row.get("finalize_state") or "")
            if not sid:
                continue
            if state == "failed":
                ts = None
                try:
                    ts = datetime.fromisoformat(str(row.get("finalize_at") or ""))
                except ValueError:
                    ts = None
                if ts and (datetime.now() - ts).total_seconds() < self.finalize_retry_s:
                    continue
                store.set_offline_finalize(conn, sid, state="")
                self._request_finalize(conn, sid)
                continue
            jid = str(row.get("finalize_job") or "")
            if not jid:
                store.set_offline_finalize(conn, sid, state="failed",
                                           error="没有任务号")
                continue
            try:
                code, out = self._http_json(
                    "/api/jobs?job_id=" + urllib.parse.quote(jid))
            except Exception:
                continue          # 引擎暂时没响应，下一轮再查
            job = out.get("job") if isinstance(out.get("job"), dict) else out
            status = str((job or {}).get("status") or "")
            if not job or (code != 200 and not status):
                store.set_offline_finalize(
                    conn, sid, state="failed",
                    error="任务丢失（引擎可能重启过，发 /发布 可手动重试）")
                continue
            if status == "succeeded":
                store.set_offline_finalize(conn, sid, state="done", error="")
                done += 1
                _log("收尾完成 %s" % sid)
            elif status in ("failed", "cancelled", "interrupted"):
                store.set_offline_finalize(
                    conn, sid, state="failed",
                    error=str((job or {}).get("error") or status)[:200])
                _log("收尾失败 %s: %s" % (sid, (job or {}).get("error") or status))
        return done

    def run_once(self, *, sid: str | None = None, backfill: bool = False,
                 limit: int = 0, conn: sqlite3.Connection | None = None) -> dict:
        own = conn is None
        conn = conn or store.connect()
        fed = done = finalized = 0
        rows: list[dict] = []
        try:
            try:
                conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone()
            except sqlite3.OperationalError:
                # 全新数据目录（还没起过引擎）：把库建起来，别让 --once 直接崩。
                store.init_db(conn)
            store.ensure_offline_asr(conn)
            # 先收掉上一轮的收尾状态：即使这一轮没有可补的场次，也要跟进。
            finalized = self._poll_finalize(conn)
            rows = self._targets(conn, sid=sid, backfill=backfill, limit=limit)
            if not rows:
                return {"ok": True, "sessions": 0, "fed_bytes": 0, "done": 0,
                        "finalized": finalized}
            self._ensure_engine()
            for row in rows:
                sid2 = str(row["id"])
                prog = store.get_offline_asr(conn, sid2) or {}
                start_s = float(prog.get("processed_s") or 0)
                st = self.states.get(sid2)
                if st is None:
                    st = _State(sid2, start_s, engine=self.engine,
                                vad_factory=self.vad_factory,
                                speakers=self.speakers,
                                audio_dir=self.audio_dir,
                                start_n=int(prog.get("n_utterances") or 0))
                    self.states[sid2] = st
                try:
                    finished, got = st.pump(conn, row, self.max_feed_s)
                    fed += got
                    store.set_offline_asr(
                        conn, sid2, processed_s=round(st.fed_s, 3),
                        status=("running" if not finished else "done"),
                        error="", n_utterances=st.n)
                    if finished:
                        self._finish(conn, st)
                        done += 1
                        self.states.pop(sid2, None)
                except Exception as e:
                    traceback.print_exc()
                    msg = "%s: %s" % (type(e).__name__, e)
                    _log("场次失败 %s: %s" % (sid2, msg))
                    store.set_offline_asr(conn, sid2, status="error", error=msg,
                                          n_utterances=st.n)
                    self.states.pop(sid2, None)
        finally:
            if own:
                conn.close()
        alive = {str(r["id"]) for r in rows}
        for sid2 in list(self.states):
            if sid2 not in alive:
                self.states.pop(sid2, None)
        # 再查一次：短场次的总结可能几秒就完成，不必等下一轮 5 秒。
        finalized += self._poll_finalize(conn)
        return {"ok": True, "sessions": len(rows), "fed_bytes": fed,
                "done": done, "finalized": finalized}

    def _finish(self, conn: sqlite3.Connection, st: _State) -> None:
        # 即使本轮只补了尾巴、没有新 embedding，库里也可能已有上一轮存的声纹；
        # 全场重分人照跑，句子太少时 recluster 自己会拒绝。
        if self.speakers:
            try:
                info = recluster.recluster_session(conn, st.sid)
                _log("重分人完成 %s: %d 人" % (
                    st.sid, len(info.get("speakers") or [])))
            except ValueError as e:
                _log("重分人跳过 %s: %s" % (st.sid, e))
            except Exception:
                traceback.print_exc()
        store.set_offline_asr(conn, st.sid, processed_s=round(st.fed_s, 3),
                              status="done", error="", n_utterances=st.n)
        _log("场次完成 %s: %d 句, %.0f 秒" % (st.sid, st.n, st.fed_s))
        # 补完字只是拿到材料；总结/发布仍由引擎的调度器做。触发失败会在
        # 下一轮由 _poll_finalize 重试，不影响已经落库的逐字稿。
        # 整场一句有效发言都没有（静音/纯 BGM）时不触发：引擎也会回
        # 「还没有逐字稿」，每 5 秒重试一次只会刷日志。
        if st.n:
            self._request_finalize(conn, st.sid)

    def run_forever(self) -> None:
        _log("启动", interval=self.interval, days=self.days,
             max_feed_s=self.max_feed_s, speakers=self.speakers)
        last_log = 0.0
        while True:
            try:
                out = self.run_once()
                now = time.monotonic()
                if out.get("sessions") and (out.get("done") or now - last_log >= 60):
                    _log("本轮 %(sessions)d 场, 读入 %(fed_bytes)d 字节, "
                         "收尾 %(done)d 场, 确认收尾 %(finalized)d 场" % out)
                    last_log = now
            except KeyboardInterrupt:
                raise
            except Exception:
                traceback.print_exc()
            time.sleep(self.interval)
