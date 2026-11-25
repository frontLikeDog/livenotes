# -*- coding: utf-8 -*-
"""对比：完整链路 vs 仅采集，进程常驻内存差多少。只用于选型，不进测试集。

    .venv/Scripts/python.exe tmp/bench/capture_only_mem.py
"""
import os
import sys
import tempfile
import time

os.environ["LIVENOTES_DATA_DIR"] = tempfile.mkdtemp(prefix="ln-capmem-")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

import numpy as np
import psutil

from app import live, store


def rss() -> float:
    return psutil.Process().memory_info().rss / 1e6


def feed(sid: str, seconds: float) -> None:
    pcm = (np.ones(live.CHUNK_SAMPLES, dtype="<i2") * 1200).tobytes()
    for _ in range(max(1, int(seconds / 0.256))):
        live.feed(sid, pcm)
    time.sleep(0.4)


def main() -> int:
    conn = store.connect()
    store.init_db(conn)
    conn.close()
    base = rss()
    print("import 后             : %.1f MB" % base)

    t0 = time.perf_counter()
    cap = live.start("仅采集", source_mode="direct", capture_only=True)
    feed(cap["session_id"], 2.0)
    print("仅采集 1 路录音中     : %.1f MB（启动 %.0f ms，未加载模型）"
          % (rss(), (time.perf_counter() - t0) * 1000))
    live.stop(cap["session_id"], finalize=False)

    t0 = time.perf_counter()
    full = live.start("完整", source_mode="direct")
    load_ms = (time.perf_counter() - t0) * 1000
    feed(full["session_id"], 2.0)
    print("完整链路 1 路录音中   : %.1f MB（含模型加载 %.0f ms）"
          % (rss(), load_ms))
    live.stop(full["session_id"], finalize=False)
    print("收尾后                 : %.1f MB" % rss())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
