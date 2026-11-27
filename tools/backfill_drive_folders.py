# -*- coding: utf-8 -*-
"""把历史场次的云端录音移进各自的场次文件夹。

    python tools/backfill_drive_folders.py --dry          # 只打印会建哪些文件夹
    python tools/backfill_drive_folders.py --limit 5      # 先拿 5 场试
    python tools/backfill_drive_folders.py --all          # 全部

只处理 ``sessions.drive_token`` 非空的场次：先取或建场次文件夹，再把已有
文件 **移动** 进去。移动不改变 file_token，场次表里已有的链接不会失效。

默认最多处理 20 场，防止一次误跑把整个云空间搬一遍；``--all`` 才全量。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import feishu, feishu_drive, store  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="历史场次录音移进各自云空间文件夹")
    ap.add_argument("--dry", action="store_true", help="只打印，不建目录/不移动")
    ap.add_argument("--limit", type=int, default=20,
                    help="最多处理多少场（默认 20；--all 时忽略）")
    ap.add_argument("--all", action="store_true", help="处理全部场次")
    args = ap.parse_args(argv)

    conn = store.connect()
    try:
        store.init_db(conn)
        sessions = [
            s for s in store.list_sessions(conn)
            if str(s.get("drive_token") or "").strip()
            and not s.get("deleted_at")
        ]
        if not args.all:
            sessions = sessions[:max(0, int(args.limit))]
        print("有云端录音的场次：%d 场（本次处理 %d 场）"
              % (len(sessions), len(sessions)))
        if not sessions:
            return 0

        try:
            bot = feishu.Bot()
        except Exception as e:
            print("飞书未配置：%s" % e)
            return 2

        done = failed = moved = 0
        for s in sessions:
            sid = str(s.get("id") or "")
            title = str(s.get("title") or sid)
            name = feishu_drive.folder_name(s)
            had_folder = bool(str(s.get("drive_folder_token") or ""))
            print("\n%s《%s》" % (sid, title))
            print("  文件夹：%s%s" % (name, "（已存在，跳过建目录）"
                                      if had_folder else ""))
            if args.dry:
                done += 1
                continue
            res = feishu_drive.ensure_session_folder(conn, s, bot)
            if not res.get("ok"):
                print("  ! 失败：%s" % res.get("error"))
                failed += 1
                continue
            print("  %s：%s" % ("新建" if res.get("created") else "复用",
                               res.get("url") or res.get("token")))
            if not had_folder or res.get("created"):
                mv = feishu_drive.move_file(
                    bot.client, str(s["drive_token"]), str(res["token"]))
                if mv.get("ok"):
                    moved += 1
                    print("  已移动录音：%s" % s["drive_token"])
                else:
                    print("  ! 移动失败：%s" % mv.get("error"))
                    failed += 1
                    continue
            done += 1
        print("\n完成：%d 场处理成功，%d 场失败，%d 个文件移动"
              % (done, failed, moved))
        return 0 if failed == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
