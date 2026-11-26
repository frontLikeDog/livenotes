# -*- coding: utf-8 -*-
"""探针：飞书云空间里点开音频，是在线播放，还是只能下载？

这个问题决定「手机能不能听整场」这条产品判断（docs/37 §12.3 的待实测项）。
接口拿不到「能否预览」这个答案，只能把几种格式并排传上去，人在手机上点一遍看结果。

    python tools/spike_drive_preview.py            # 上传测试文件，打印链接
    python tools/spike_drive_preview.py --cleanup  # 删掉探针文件夹（用它打印的 token）

做什么：
  1. 在应用自己的根目录建一个临时文件夹 `LiveNotes 预览探针 <时间>`；
  2. 取一场真实 m4a，切 60 秒做样本，转成 m4a / mp3 / opus / wav 四种；
     再把整场 m4a 传一份（真实生产形态）；
  3. 授权给 owner（否则手机上点开是无权限）；
  4. 列目录 + 查 meta，打印每个文件的 token、大小、接口报的类型；
     链接用接口返回的租户域名拼，不用通用 feishu.cn（那条会跳到 www 首页）。

不做什么：不写数据库、不动 data/、不改任何配置。凭证只从 FEISHU_APP_ID/SECRET
或 data/feishu.json(.bak) 读，不打印出来。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文 + 表情会变成乱码。先切 UTF-8 再说。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CANDIDATES = [
    Path(os.environ.get("LIVENOTES_DATA_DIR", "")) / "feishu.json"
    if os.environ.get("LIVENOTES_DATA_DIR") else None,
    Path(os.environ.get("LOCALAPPDATA", "")) / "LiveNotes" / "data" / "feishu.json"
    if os.environ.get("LOCALAPPDATA") else None,
    ROOT / "data" / "feishu.json",
    Path(os.environ.get("LOCALAPPDATA", "")) / "LiveNotes" / "data" / "feishu.json.bak"
    if os.environ.get("LOCALAPPDATA") else None,
    ROOT / "data" / "feishu.json.bak",
]
AUDIO_DIRS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "LiveNotes" / "data" / "audio"
    if os.environ.get("LOCALAPPDATA") else None,
    ROOT / "data" / "audio",
]


def _redact(v: str) -> str:
    v = str(v or "")
    return (v[:6] + "…" + str(len(v))) if v else "<空>"


def load_creds() -> dict:
    for p in CANDIDATES:
        if not p or not p.is_file():
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        app_id = os.environ.get("FEISHU_APP_ID") or str(raw.get("app_id") or "")
        secret = os.environ.get("FEISHU_APP_SECRET") or str(raw.get("app_secret") or "")
        if app_id and secret:
            print("[creds] 用 %s  app_id=%s" % (p, _redact(app_id)))
            owner = str(os.environ.get("FEISHU_OWNER_OPEN_ID")
                        or raw.get("owner_open_id") or raw.get("open_id") or "")
            if owner:
                print("[creds] owner=%s" % _redact(owner))
            return {"app_id": app_id, "app_secret": secret, "owner": owner}
    raise SystemExit(
        "找不到飞书凭证。设置 FEISHU_APP_ID / FEISHU_APP_SECRET，"
        "或把 feishu.json 放到 data/ 下。")


def pick_source() -> Path:
    best: tuple[int, Path] | None = None
    for d in AUDIO_DIRS:
        if not d or not d.is_dir():
            continue
        for p in d.glob("*.m4a"):
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size <= 0:
                continue
            # 取最小的那一场：探针不需要长录音，小文件传得快、手机加载快。
            if best is None or size < best[0]:
                best = (size, p)
    if not best:
        raise SystemExit("没找到 m4a。请给一场真实录音，或手动改 AUDIO_DIRS。")
    print("[src] %s（%.2f MB）" % (best[1].name, best[0] / 1048576))
    return best[1]


def ffmpeg() -> str:
    try:
        from app.audio import ffmpeg_path
        got = ffmpeg_path()
        if got:
            return got
    except Exception:
        pass
    return "ffmpeg"


def build_variants(src: Path, out: Path) -> list[Path]:
    """切 60 秒做四种格式；整场 m4a 另传一份。ffmpeg 不在就只传整场。"""
    files: list[Path] = []
    exe = ffmpeg()
    jobs = [
        ("probe_60s.m4a", ["-c:a", "aac", "-b:a", "32k"]),
        ("probe_60s.mp3", ["-c:a", "libmp3lame", "-b:a", "64k"]),
        ("probe_60s.opus", ["-c:a", "libopus", "-b:a", "24k"]),
        ("probe_60s.wav", ["-c:a", "pcm_s16le"]),
    ]
    for name, codec in jobs:
        dst = out / name
        cmd = [exe, "-y", "-loglevel", "error", "-t", "60", "-i", str(src),
               "-ac", "1", "-ar", "16000"] + codec + [str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as e:
            print("[ffmpeg] %s 跳过：%s" % (name, e))
            continue
        if r.returncode != 0 or not dst.is_file() or dst.stat().st_size <= 0:
            err = (r.stderr or b"").decode("utf-8", "ignore")[:200]
            print("[ffmpeg] %s 失败：%s" % (name, err))
            continue
        files.append(dst)
    return files


def main() -> int:
    cleanup_token = ""
    for i, a in enumerate(sys.argv[1:]):
        if a == "--cleanup":
            cleanup_token = sys.argv[i + 2] if len(sys.argv) > i + 2 else ""
    creds = load_creds()

    import lark_oapi as lark
    from lark_oapi.api.drive.v1 import (
        BaseMember, BatchQueryMetaRequest, CreateFolderFileRequest,
        CreateFolderFileRequestBody, CreatePermissionMemberRequest,
        DeleteFileRequest, ListFileRequest, MetaRequest, RequestDoc,
        UploadAllFileRequest, UploadAllFileRequestBody)

    client = lark.Client.builder().app_id(creds["app_id"]) \
        .app_secret(creds["app_secret"]).log_level(lark.LogLevel.WARNING).build()

    if cleanup_token:
        return do_cleanup(client, cleanup_token)

    # 0) 先确认凭证和权限能用：列一次应用根目录。
    probe = client.drive.v1.file.list(ListFileRequest.builder().page_size(1).build())
    if not probe.success():
        print("[x] 读根目录失败 code=%s msg=%s" % (probe.code, probe.msg))
        print("    多半是缺 drive:drive 权限，或应用没开通云空间。")
        return 2
    print("[ok] 凭证可用，根目录可读")

    # 1) 建临时文件夹（folder_token 留空 = 建在应用自己的根目录）。
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    name = "LiveNotes 预览探针 %s" % stamp
    made = client.drive.v1.file.create_folder(
        CreateFolderFileRequest.builder().request_body(
            CreateFolderFileRequestBody.builder().name(name).folder_token("").build()
        ).build())
    if not made.success():
        print("[x] 建文件夹失败 code=%s msg=%s" % (made.code, made.msg))
        return 2
    folder = str(getattr(made.data, "token", "") or "")
    folder_url = str(getattr(made.data, "url", "") or "")
    print("[ok] 文件夹 %s  token=%s" % (name, folder))

    # 2) 准备样本并上传。
    src = pick_source()
    uploaded: list[tuple[str, str, int]] = []
    with tempfile.TemporaryDirectory(prefix="lnprobe-") as td:
        td_path = Path(td)
        variants = build_variants(src, td_path)
        # 整场原件也用生产命名传一份，看真实形态。
        full = td_path / ("probe_full" + src.suffix)
        try:
            full.write_bytes(src.read_bytes())
            variants.append(full)
        except OSError as e:
            print("[warn] 复制整场失败：%s" % e)

        for p in variants:
            size = p.stat().st_size
            r = client.drive.v1.file.upload_all(
                UploadAllFileRequest.builder().request_body(
                    UploadAllFileRequestBody.builder()
                    .file_name(p.name).parent_type("explorer").parent_node(folder)
                    .size(size).file(open(p, "rb")).build()).build())
            if not r.success():
                print("[x] 上传 %s 失败 code=%s msg=%s" % (p.name, r.code, r.msg))
                continue
            tok = str(getattr(r.data, "file_token", "") or "")
            uploaded.append((p.name, tok, size))
            print("[ok] 上传 %s（%.2f MB）token=%s" % (p.name, size / 1048576, tok))

    if not uploaded:
        print("[x] 一个文件都没传上去。")
        return 3

    # 3) 授权给 owner，否则手机点开是无权限。
    if creds.get("owner"):
        for fname, tok, _ in uploaded:
            g = client.drive.v1.permission_member.create(
                CreatePermissionMemberRequest.builder()
                .token(tok).type("file").need_notification(False)
                .request_body(BaseMember.builder().member_type("openid")
                              .member_id(creds["owner"]).perm("full_access").build())
                .build())
            if not g.success():
                print("[warn] 授权 %s 失败 code=%s msg=%s" % (fname, g.code, g.msg))
        g = client.drive.v1.permission_member.create(
            CreatePermissionMemberRequest.builder()
            .token(folder).type("folder").need_notification(False)
            .request_body(BaseMember.builder().member_type("openid")
                          .member_id(creds["owner"]).perm("full_access").build())
            .build())
        print("[%s] 文件夹授权给 owner" % ("ok" if g.success() else "warn"))
    else:
        print("[warn] 没有 owner_open_id，手机上点开可能无权限")

    # 4) 列目录：接口把这个文件当成什么类型。
    listing = client.drive.v1.file.list(
        ListFileRequest.builder().folder_token(folder).build())
    kinds: dict[str, tuple[str, str]] = {}   # 文件名 -> (接口 type, 文件 url)
    if listing.success():
        for f in (getattr(listing.data, "files", None) or []):
            kinds[str(getattr(f, "name", ""))] = (
                str(getattr(f, "type", "") or ""), str(getattr(f, "url", "") or ""))

    # 5) meta：doc_type 是飞书自己认定的文档类型，能提示它有没有可预览的渲染器。
    toks = [t for _, t, _ in uploaded]
    metas: dict[str, str] = {}
    if toks:
        m = client.drive.v1.meta.batch_query(
            BatchQueryMetaRequest.builder().user_id_type("open_id")
            .request_body(MetaRequest.builder()
                          .request_docs([RequestDoc.builder()
                                         .doc_token(t).doc_type("file").build()
                                         for t in toks]).build()).build())
        if m.success():
            for one in (getattr(m.data, "metas", None) or []):
                metas[str(getattr(one, "doc_token", "") or "")] = \
                    str(getattr(one, "doc_type", "") or "")
        else:
            print("[warn] meta 查询失败 code=%s msg=%s" % (m.code, m.msg))

    # 链接必须用租户域名：通用 feishu.cn/file/<token> 会 301 到 www 营销页，
    # 手机点开是首页而不是文件。租户域名从 list 返回的 url 里反推。
    base = os.environ.get("FEISHU_DRIVE_URL_BASE") or ""
    if not base:
        for f in (getattr(listing.data, "files", None) or []):
            u = str(getattr(f, "url", "") or "")
            if "/file/" in u:
                base = u.split("/file/")[0] + "/file/"
                break
    if not base:
        base = ("https://%s.feishu.cn/file/"
                % (creds.get("tenant", "") or "<你的租户>"))
    if not base.endswith("/"):
        base += "/"

    print("\n" + "=" * 72)
    print("在手机上逐个点开下面这些链接，记录「点开之后发生了什么」：")
    print("  A 直接出现播放器，能拖进度条            -> 可在线播放")
    print("  B 出现文件详情页 + 「下载」按钮           -> 只能下载")
    print("  C 提示「不支持预览」/「用其他应用打开」   -> 不可预览")
    print("  D 无权限                                -> 授权没生效")
    print("=" * 72)
    print("%-18s %8s %-10s %s" % ("文件", "大小MB", "接口type", "链接"))
    for fname, tok, size in uploaded:
        typ = kinds.get(fname, ("?", ""))[0] or metas.get(tok, "?")
        # list 已经给了每条的 url，优先用它；没有才按租户域名拼。
        shown = kinds.get(fname, ("?", ""))[1] or (base + tok)
        print("%-18s %8.2f %-10s %s" % (fname, size / 1048576, typ, shown))
    print("\n文件夹：%s" % (folder_url or ("https://feishu.cn/drive/folder/" + folder)))
    print("清理（传完测完再跑）：")
    print("  python tools/spike_drive_preview.py --cleanup %s" % folder)
    print("\n把每个文件是 A/B/C/D 贴回来，我据此改 docs/51 的验收口径。")
    return 0


def do_cleanup(client, folder: str) -> int:
    from lark_oapi.api.drive.v1 import DeleteFileRequest, ListFileRequest
    listing = client.drive.v1.file.list(
        ListFileRequest.builder().folder_token(folder).page_size(200).build())
    if not listing.success():
        print("[x] 列目录失败 code=%s msg=%s" % (listing.code, listing.msg))
        return 2
    n = 0
    for f in (getattr(listing.data, "files", None) or []):
        tok = str(getattr(f, "token", "") or "")
        typ = "folder" if str(getattr(f, "type", "")) == "folder" else "file"
        if not tok:
            continue
        r = client.drive.v1.file.delete(
            DeleteFileRequest.builder().file_token(tok).type(typ).build())
        print("[%s] 删 %s" % ("ok" if r.success() else "x", getattr(f, "name", tok)))
        n += 1
    r = client.drive.v1.file.delete(
        DeleteFileRequest.builder().file_token(folder).type("folder").build())
    print("[%s] 删文件夹 %s" % ("ok" if r.success() else "x", folder))
    print("共删 %d 个文件" % n)
    return 0 if r.success() else 1


if __name__ == "__main__":
    raise SystemExit(main())
