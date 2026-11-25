# -*- coding: utf-8 -*-
"""轻量 HTTP 压测：并发打常用只读接口，输出 QPS 与 P50/P95。

只用于给 VPS 选型做参考，不进入仓库测试集。
    python tmp/bench/http_bench.py --base http://127.0.0.1:8899 --seconds 10 --concurrency 16
"""
from __future__ import annotations

import argparse
import statistics
import threading
import time
import urllib.request

ENDPOINTS = ["/api/health", "/api/sessions", "/api/library", "/api/traffic",
             "/api/live/info", "/api/rooms", "/"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--concurrency", type=int, default=16)
    args = ap.parse_args()

    deadline = time.monotonic() + args.seconds
    lat: list[float] = []
    errs = [0]
    lock = threading.Lock()
    counts: dict[str, list[float]] = {p: [] for p in ENDPOINTS}

    def worker(i: int) -> None:
        n = 0
        while time.monotonic() < deadline:
            p = ENDPOINTS[(i + n) % len(ENDPOINTS)]
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(args.base + p, timeout=10) as r:
                    r.read()
                dt = time.perf_counter() - t0
                with lock:
                    lat.append(dt)
                    counts[p].append(dt)
            except Exception:
                with lock:
                    errs[0] += 1
            n += 1

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(args.concurrency)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0

    def pct(xs: list[float], q: float) -> float:
        if not xs:
            return 0.0
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(len(xs) * q))] * 1000

    print("concurrency=%d wall=%.1fs requests=%d errors=%d qps=%.1f"
          % (args.concurrency, wall, len(lat), errs[0], len(lat) / wall))
    if lat:
        print("all    P50=%.1fms P95=%.1fms P99=%.1fms max=%.1fms"
              % (pct(lat, .5), pct(lat, .95), pct(lat, .99), max(lat) * 1000))
        for p, xs in counts.items():
            if xs:
                print("%-16s n=%-6d P50=%6.1fms P95=%6.1fms"
                      % (p, len(xs), pct(xs, .5), pct(xs, .95)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
