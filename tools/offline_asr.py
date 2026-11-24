# -*- coding: utf-8 -*-
"""仅采集节点的离线补转写进程。

    # 常驻（systemd）：最近 2 天的仅采集场次，边录边补
    python tools/offline_asr.py

    # 只跑一轮就退出（排障 / 定时器）
    python tools/offline_asr.py --once

    # 指定一场补到收尾（历史场次也行）
    python tools/offline_asr.py --session LIVE_20261125_003510

    # 所有仅采集场次补一轮（注意 CPU/磁盘）
    python tools/offline_asr.py --backfill

模型和实时链路共用 ``models/`` 下的 SenseVoice / silero VAD / CAM++；
只装 ``requirements-capture.txt`` 的机器需要补 ``requirements-live.txt``。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.offline_asr import OfflineAsr  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="给仅采集场次补逐字稿")
    ap.add_argument("--once", action="store_true", help="跑一轮就退出")
    ap.add_argument("--session", default="", help="只处理这一场（补到收尾）")
    ap.add_argument("--backfill", action="store_true", help="处理所有仅采集场次")
    ap.add_argument("--limit", type=int, default=0, help="最多处理几场")
    args = ap.parse_args()

    worker = OfflineAsr()
    if args.session:
        sid = args.session.strip()
        # 一场可能积压几小时：循环补到没有新音频或已收尾，最多 240 轮。
        for _ in range(240):
            out = worker.run_once(sid=sid)
            if not out.get("sessions") or out.get("done"):
                break
            if int(out.get("fed_bytes") or 0) <= 0:
                break
            # 已收尾但 WAV 还没写完时等一轮
            time.sleep(max(0.5, worker.interval))
        return 0
    if args.once or args.backfill:
        out = worker.run_once(backfill=args.backfill, limit=args.limit)
        print("完成：%(sessions)d 场，读入 %(fed_bytes)d 字节，收尾 %(done)d 场" % out)
        return 0
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
