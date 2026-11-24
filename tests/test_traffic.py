# -*- coding: utf-8 -*-
"""采集流量计量：口径、CDP 事件折算、直取计数代理。

只测纯逻辑和本地回环，不碰真实直播间/浏览器。
"""
from __future__ import annotations

import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.traffic import (CdpTrafficCounter, CountingProxy, TrafficMeter,
                         parse_bitrate_kbps, parse_video_resolution,
                         resolution_kbps, sanitize_traffic)


def test_meter_snapshot_math():
    # 固定起点，避免用 sleep 等真实时间。
    m = TrafficMeter(started_at=time.monotonic() - 10.0)
    m.add_platform(10_000_000)      # 10 MB / 10s = 8 Mbps = 3600 MB/h
    m.add_audio(1_000_000)          # 1 MB / 10s = 800 kbps
    snap = m.snapshot()
    assert snap["platform_bytes"] == 10_000_000
    assert snap["platform_mbps"] == pytest.approx(8.0, rel=0.02)
    assert snap["platform_mb_per_hour"] == pytest.approx(3600.0, rel=0.02)
    assert snap["audio_kbps"] == pytest.approx(800.0, rel=0.02)
    assert snap["ratio"] == pytest.approx(0.1, rel=0.02)


def test_meter_estimate_and_exact_precedence():
    m = TrafficMeter(started_at=time.monotonic() - 10.0)
    m.set_estimate_kbps(3000)
    assert m.snapshot()["source"] == "estimate"
    # 精确源出现后，估算不许把它盖回 estimate。
    m.set_source("cdp", "CDP")
    m.set_estimate_kbps(1000)
    assert m.snapshot()["source"] == "cdp"
    # 精确源断掉时可以降级回估算。
    m.demote_source("断了")
    assert m.snapshot()["source"] == "estimate"
    assert m.snapshot()["note"] == "断了"


def test_estimate_without_bytes_gives_rate():
    m = TrafficMeter(started_at=time.monotonic() - 10.0)
    m.set_estimate_kbps(3000)       # 3 Mbps -> 10s 约 3.75 MB
    snap = m.snapshot()
    assert snap["source"] == "estimate"
    assert snap["platform_bytes"] == pytest.approx(3_750_000, rel=0.05)
    assert snap["platform_mbps"] == pytest.approx(3.0, rel=0.05)


def test_estimate_change_accumulates_instead_of_rewriting_history():
    """直播中途换码率：累计字节只能继续涨，不能按最新码率重算整场。"""
    now = [1000.0]
    m = TrafficMeter(started_at=980.0, clock=lambda: now[0])
    m.set_estimate_kbps(6000)       # 启动至今 20s × 6 Mbps = 15 MB
    assert m.snapshot()["platform_bytes"] == pytest.approx(15_000_000, rel=0.01)

    now[0] += 10
    m.set_estimate_kbps(450)        # 先用 6 Mbps 结算这 10s，再换档
    assert m.snapshot()["platform_bytes"] == pytest.approx(22_500_000, rel=0.01)

    now[0] += 10                    # 10s × 450 kbps = 562,500 B
    snap = m.snapshot()
    assert snap["platform_bytes"] == pytest.approx(23_062_500, rel=0.01)
    assert snap["platform_bytes"] > 22_500_000


def test_exact_takeover_keeps_estimate_prefix_and_demotion_does_not_double_count():
    now = [1000.0]
    m = TrafficMeter(started_at=1000.0, clock=lambda: now[0])
    m.set_estimate_kbps(3000)
    now[0] += 10
    m.set_source("proxy", "计数代理实测")   # 接管前 10s 按估算结算
    m.add_platform(1_000_000)               # 代理实测字节
    assert m.snapshot()["platform_bytes"] == pytest.approx(4_750_000, rel=0.01)

    m.demote_source("代理断了")
    now[0] += 10                            # 降级后按估算继续，不补精确那段
    assert m.snapshot()["platform_bytes"] == pytest.approx(8_500_000, rel=0.01)


def test_cdp_counter_dedupes_data_received_and_loading_finished():
    m = TrafficMeter()
    c = CdpTrafficCounter(m)
    c.feed({"method": "Network.dataReceived",
            "params": {"requestId": "r1", "dataLength": 1000}})
    c.feed({"method": "Network.loadingFinished",
            "params": {"requestId": "r1", "encodedDataLength": 1200}})
    # dataReceived 已计 1000，loadingFinished 只补差额 200。
    assert m.snapshot()["platform_bytes"] == 1200
    assert m.snapshot()["source"] == "cdp"

    # 没有 dataReceived 的响应（缓存命中）：按 encodedDataLength 计。
    c.feed({"method": "Network.loadingFinished",
            "params": {"requestId": "r2", "encodedDataLength": 300}})
    assert m.snapshot()["platform_bytes"] == 1500

    # 流式直播永远不 loadingFinished：dataReceived 单独也要能累计。
    c.feed({"method": "Network.dataReceived",
            "params": {"requestId": "r3", "dataLength": 400}})
    assert m.snapshot()["platform_bytes"] == 1900


def test_cdp_counter_cleans_failed_requests():
    m = TrafficMeter()
    c = CdpTrafficCounter(m)
    c.feed({"method": "Network.dataReceived",
            "params": {"requestId": "r1", "dataLength": 10}})
    c.feed({"method": "Network.loadingFailed", "params": {"requestId": "r1"}})
    # 失败请求已计过的字节不会回退；只是清掉 requestId 记录。
    assert m.snapshot()["platform_bytes"] == 10
    c.feed({"method": "Network.loadingFinished",
            "params": {"requestId": "r1", "encodedDataLength": 999}})
    assert m.snapshot()["platform_bytes"] == 1009


def test_counting_proxy_forwards_and_counts():
    payload = b"x" * 100_000

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "video/x-flv")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    upstream.daemon_threads = True
    th = threading.Thread(target=upstream.serve_forever, daemon=True)
    th.start()
    seen: list[int] = []
    proxy = CountingProxy(
        "http://127.0.0.1:%d/live.flv" % upstream.server_address[1],
        on_bytes=seen.append)
    try:
        url = proxy.start()
        with urllib.request.urlopen(url, timeout=5) as r:
            assert r.read() == payload
        assert proxy.bytes == len(payload)
        assert sum(seen) == len(payload)
    finally:
        proxy.stop()
        upstream.shutdown()
        upstream.server_close()


def test_counting_proxy_rejects_wrong_path():
    proxy = CountingProxy("http://127.0.0.1:9/nope")
    try:
        url = proxy.start()
        bad = url.rsplit("/", 1)[0] + "/wrong"
        with pytest.raises(Exception):
            urllib.request.urlopen(bad, timeout=2).read()
    finally:
        proxy.stop()


def test_parse_bitrate_and_resolution():
    text = ("Input #0, flv, from 'http://x/live.flv':\n"
            "  Duration: N/A, start: 0.000000, bitrate: 2500 kb/s\n"
            "    Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps\n")
    assert parse_bitrate_kbps(text) == 2500
    assert parse_video_resolution(text) == (1920, 1080)
    assert resolution_kbps(1920, 1080) == 6000
    assert resolution_kbps(1280, 720) == 3000
    assert resolution_kbps(0, 0) == 0
    # ffprobe 风格 bit_rate=b/s
    assert parse_bitrate_kbps("bit_rate=1800000") == 1800


def test_sanitize_traffic():
    assert sanitize_traffic(None) is None
    assert sanitize_traffic([]) is None
    assert sanitize_traffic({"unknown": 1}) is None
    got = sanitize_traffic({
        "platform_bytes": "123",
        "audio_bytes": -5,
        "platform_mbps": "2.5",
        "source": "bogus",
        "note": "x" * 500,
        "hack": "drop me",
    })
    assert got["platform_bytes"] == 123
    assert got["audio_bytes"] == 0
    assert got["platform_mbps"] == 2.5
    assert got["source"] == "unavailable"
    assert len(got["note"]) == 200
    assert "hack" not in got
