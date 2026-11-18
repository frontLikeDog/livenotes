# -*- coding: utf-8 -*-
"""采集流量计量：平台侧下行 + 本地音频上行。

三种采集方式送进服务的音频契约完全一样（16 kHz 单声道 s16le，32 KB/s），
真正不同的是「从平台拿声音」时下载了多少字节：

  - ``per_room`` / ``shared``：浏览器播放器下载整路视频，挂到 CDP Network
    上按 ``dataReceived`` / ``loadingFinished`` 累计，得到页面侧真实下行；
  - ``direct``：ffmpeg 拉 FLV/HLS。默认按 ffmpeg 打印的码率/分辨率估算；
    设置 ``LIVENOTES_TRAFFIC_PROXY=1`` 时走本地计数代理，拿到精确字节数。

计量永远不能拖垮采集：所有失败路径都只把 ``source`` 标成 ``unavailable``，
不影响录音、识别和现有的看门狗。
"""
from __future__ import annotations

import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

MB = 1_000_000.0
EXACT_SOURCES = ("proxy", "cdp", "mixed")
SOURCES = ("proxy", "cdp", "estimate", "mixed", "unavailable")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TrafficMeter:
    """一个房间的流量计数器。所有方法线程安全，读快照不会阻塞采集。"""

    def __init__(self, started_at: float | None = None) -> None:
        self._lock = threading.Lock()
        self._start = (time.monotonic() if started_at is None
                       else float(started_at))
        self._platform = 0
        self._audio = 0
        self._source = "unavailable"
        self._note = ""
        self._estimate_kbps = 0.0
        self._last_update = time.monotonic()

    # ---------------------------------------------------------------- 写入
    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()
            self._platform = 0
            self._audio = 0
            self._source = "unavailable"
            self._note = ""
            self._estimate_kbps = 0.0
            self._last_update = time.monotonic()

    def add_platform(self, n: int | float) -> None:
        n = int(max(0, _num(n)))
        if not n:
            return
        with self._lock:
            self._platform += n
            self._last_update = time.monotonic()

    def set_platform_bytes(self, total: int | float) -> None:
        """代理累计值直接覆盖；只增不减，避免重连时读数回退。"""
        total = int(max(0, _num(total)))
        with self._lock:
            if total > self._platform:
                self._platform = total
                self._last_update = time.monotonic()

    def add_audio(self, n: int | float) -> None:
        n = int(max(0, _num(n)))
        if not n:
            return
        with self._lock:
            self._audio += n
            self._last_update = time.monotonic()

    def set_source(self, source: str, note: str = "") -> None:
        source = str(source or "").strip().lower()
        if source not in SOURCES:
            source = "unavailable"
        with self._lock:
            # 精确源（代理/CDP）一旦出现，估算不许把它覆盖回去。
            if self._source in EXACT_SOURCES and source not in EXACT_SOURCES:
                return
            self._source = source
            if note:
                self._note = str(note)[:200]

    def set_estimate_kbps(self, kbps: int | float, note: str = "") -> None:
        kbps = _num(kbps)
        if kbps <= 0:
            return
        with self._lock:
            self._estimate_kbps = kbps
            if self._source not in EXACT_SOURCES:
                self._source = "estimate"
                if note:
                    self._note = str(note)[:200]
            self._last_update = time.monotonic()

    def demote_source(self, note: str = "") -> None:
        """精确计量断掉时降级：有估算就退回估算，没有就标不可用。"""
        with self._lock:
            self._source = "estimate" if self._estimate_kbps > 0 else "unavailable"
            if note:
                self._note = str(note)[:200]

    # ---------------------------------------------------------------- 读取
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            start = self._start
            platform = self._platform
            audio = self._audio
            source = self._source
            note = self._note
            estimate = self._estimate_kbps
        elapsed = max(0.001, time.monotonic() - start)
        if platform <= 0 and estimate > 0:
            platform = int(estimate * 1000.0 / 8.0 * elapsed)
            if source == "unavailable":
                source = "estimate"
        platform_bps = platform * 8.0 / elapsed
        audio_bps = audio * 8.0 / elapsed
        return {
            "platform_bytes": int(platform),
            "audio_bytes": int(audio),
            "elapsed_s": round(elapsed, 1),
            "platform_bps": round(platform_bps, 0),
            "platform_mbps": round(platform_bps / 1_000_000.0, 3),
            "platform_mb_per_hour": round(platform / MB / elapsed * 3600.0, 1),
            "audio_kbps": round(audio_bps / 1000.0, 1),
            "audio_mb_per_hour": round(audio / MB / elapsed * 3600.0, 1),
            "ratio": round(audio / platform, 4) if platform else 0.0,
            "source": source,
            "note": note,
            "updated_at": now_iso(),
        }


# ---------------------------------------------------------------- 浏览器 CDP
class CdpTrafficCounter:
    """把 CDP Network 事件折算成「这个页面收了多少字节」。

    ``dataReceived`` 是流式响应最可靠的口径（直播流可能永远不触发
    ``loadingFinished``）；``loadingFinished.encodedDataLength`` 用来补上
    缓存命中/没有 dataReceived 的响应差额，避免重复计数。
    """

    def __init__(self, meter: TrafficMeter, max_seen: int = 512) -> None:
        self.meter = meter
        self._seen: dict[str, int] = {}
        self._max_seen = max(8, int(max_seen))
        self._lock = threading.Lock()

    def feed(self, event: dict) -> None:
        method = str((event or {}).get("method") or "")
        params = (event or {}).get("params") or {}
        rid = str(params.get("requestId") or "")
        if method == "Network.dataReceived":
            n = int(max(0, _num(params.get("dataLength"))))
            if not n:
                return
            with self._lock:
                self._seen[rid] = self._seen.get(rid, 0) + n
                if len(self._seen) > self._max_seen:
                    # 长直播会有大量短请求；只保留最近的一批 requestId。
                    self._seen.pop(next(iter(self._seen)))
            self.meter.set_source("cdp", "CDP Network 实测")
            self.meter.add_platform(n)
        elif method == "Network.loadingFinished":
            total = int(max(0, _num(params.get("encodedDataLength"))))
            with self._lock:
                seen = self._seen.pop(rid, 0)
            if total > seen:
                self.meter.set_source("cdp", "CDP Network 实测")
                self.meter.add_platform(total - seen)
        elif method == "Network.loadingFailed":
            with self._lock:
                self._seen.pop(rid, None)


# ---------------------------------------------------------------- 直取计数代理
class CountingProxy:
    """把上游流包一层本地 HTTP 代理，按实际转发字节计数。

    只在 ``LIVENOTES_TRAFFIC_PROXY=1`` 时启用。代理只监听 127.0.0.1，
    随机路径做最小隔离；上游 403/404 会原样转发，ffmpeg 的断流判断不受影响。
    """

    def __init__(self, upstream: str, on_bytes: Callable[[int], None] | None = None,
                 headers: dict[str, str] | None = None,
                 timeout: float = 20.0) -> None:
        self.upstream = str(upstream or "")
        self.on_bytes = on_bytes
        self.headers = dict(headers or {})
        # 模仿当前直取链路里 ffmpeg 的 UA；不要擅自加 Referer/Cookie，
        # 签名地址通常不依赖它们，多带反而可能被 CDN 判成异常请求。
        self.headers.setdefault("User-Agent", "Lavf/60.16.100")
        self.timeout = float(timeout)
        self.token = secrets.token_urlsafe(12)
        self._lock = threading.Lock()
        self._bytes = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            return ""
        port = int(self._server.server_address[1])
        return "http://127.0.0.1:%d/%s" % (port, self.token)

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def start(self) -> str:
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
                if self.path.split("?", 1)[0] != "/" + proxy.token:
                    self.send_error(404)
                    return
                proxy._serve(self)

            def log_message(self, *args):  # 别把每个请求打进 stderr
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        name="traffic-proxy", daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass

    def _serve(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            req = urllib.request.Request(self.upstream, headers=self.headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                handler.send_response(200)
                handler.send_header(
                    "Content-Type", resp.headers.get("Content-Type")
                    or "application/octet-stream")
                handler.send_header("Cache-Control", "no-store")
                handler.end_headers()
                while True:
                    data = resp.read(65536)
                    if not data:
                        break
                    with self._lock:
                        self._bytes += len(data)
                    if self.on_bytes is not None:
                        try:
                            self.on_bytes(len(data))
                        except Exception:
                            pass
                    handler.wfile.write(data)
                    handler.wfile.flush()
        except urllib.error.HTTPError as e:
            # 403/404 必须原样给 ffmpeg：直取的断流判断靠 stderr 里的状态码。
            try:
                handler.send_error(int(e.code), str(e.reason or ""))
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                handler.send_error(502, "traffic proxy upstream failed: %s"
                                   % str(e)[:120])
            except Exception:
                pass


# ---------------------------------------------------------------- 估算
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+(?:\.\d+)?)\s*kb/s", re.I)
_BIT_RATE_RE = re.compile(r"\bbit_rate[=:]\s*(\d+)", re.I)
_VIDEO_RE = re.compile(r"\bVideo:", re.I)
_RES_RE = re.compile(r"\b(\d{2,5})x(\d{2,5})\b")


def parse_bitrate_kbps(text: str) -> float:
    """ffmpeg/ffprobe 输出里的码率，单位 kbps；拿不到返回 0。"""
    raw = str(text or "")
    m = _BITRATE_RE.search(raw)
    if m:
        return _num(m.group(1))
    m = _BIT_RATE_RE.search(raw)
    if m:
        # ffprobe 的 bit_rate 是 b/s
        return _num(m.group(1)) / 1000.0
    return 0.0


def parse_video_resolution(text: str) -> tuple[int, int]:
    for line in str(text or "").splitlines():
        if not _VIDEO_RE.search(line):
            continue
        m = _RES_RE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return 0, 0


def resolution_kbps(width: int, height: int) -> float:
    """按分辨率给一个保守的码率估算，只用于「没有精确计量」时的兜底。"""
    h = int(height or 0)
    if h >= 1080:
        return 6000.0
    if h >= 720:
        return 3000.0
    if h >= 540:
        return 1500.0
    if h >= 360:
        return 750.0
    if h > 0:
        return 450.0
    return 0.0


# ---------------------------------------------------------------- 服务端清洗
def sanitize_traffic(data: Any) -> dict[str, Any] | None:
    """采集端上报的数据不可信：只保留已知字段，数字限制成非负。"""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("platform_bytes", "audio_bytes"):
        if key in data:
            try:
                out[key] = max(0, int(float(data[key])))
            except (TypeError, ValueError):
                continue
    for key in ("elapsed_s", "platform_bps", "platform_mbps",
                "platform_mb_per_hour", "audio_kbps", "audio_mb_per_hour",
                "ratio"):
        if key in data:
            try:
                out[key] = max(0.0, float(data[key]))
            except (TypeError, ValueError):
                continue
    for key in ("source", "note", "updated_at"):
        if key in data:
            out[key] = str(data[key] or "")[:200]
    if "source" in out and out["source"] not in SOURCES:
        out["source"] = "unavailable"
    return out or None
