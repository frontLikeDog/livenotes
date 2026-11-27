# -*- coding: utf-8 -*-
"""把云端分享包 / 逐字稿导进本地库。

    python tools/import_session.py --file D:/Downloads/LIVE_20261127_2130.zip
    python tools/import_session.py --file cloud.txt --replace
    python tools/import_session.py --file transcript.srt --title "今晚语音厅"

同一个来源重复导入是幂等的：已经在库里就原样返回，加 ``--replace`` 才覆盖
（会清掉本机为这一场生成的总结、笔记和打点）。
录音可单独补：``--file cloud.txt --audio D:/Downloads/audio.m4a``。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import importer, store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导入云端场次（分享包 / SRT / 逐字稿）")
    ap.add_argument("--file", required=True, help="分享包 zip、transcript.srt 或逐字稿 txt")
    ap.add_argument("--audio", default="", help="单独提供的录音（m4a/wav/mp3）")
    ap.add_argument("--title", default="", help="覆盖场次标题")
    ap.add_argument("--started", default="", help="覆盖开始时间，如 2026-11-27 21:30")
    ap.add_argument("--replace", action="store_true", help="覆盖已导入过的同一场")
    args = ap.parse_args()

    conn = store.connect()
    try:
        store.init_db(conn)
        out = importer.import_file(
            conn, args.file, name=Path(args.file).name, replace=args.replace,
            title=args.title, started_at=args.started,
            audio_file=(args.audio or None))
    except importer.ImportProblem as e:
        print("导入失败：%s" % e, file=sys.stderr)
        return 2
    finally:
        conn.close()

    if out.get("existed"):
        print("这一场已经在库里：%s《%s》（%d 句）"
              % (out["session_id"], out["title"], out["n_utterances"]))
        print("要覆盖请加 --replace。")
    else:
        print("已导入：%s《%s》" % (out["session_id"], out["title"]))
        print("  %d 句 · %d 位说话人 · 时长 %.1f 秒 · 录音：%s"
              % (out["n_utterances"], out["n_speakers"], out["duration_s"],
                 "有" if out.get("has_audio") else "无"))
    for w in out.get("warnings") or []:
        print("  注意：%s" % w)
    print("打开：/reader?session=%s" % out["session_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
