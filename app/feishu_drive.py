# -*- coding: utf-8 -*-
"""把整场录音存进飞书云空间（D2：应用云空间 + 授权给操作者）。

为什么不是「用户自己的空间」：那需要 user_access_token 和 OAuth 回调，桌面端
没有公网域名时跑不通。应用用 tenant_access_token 上传到自己的文件夹，再把文件
授权给操作者，分享仍然走飞书原生权限 —— 用户在飞书里选人/选群/设权限，我们
不做接收人选择器。

两条硬约束：
  - ``upload_all`` 单文件上限 20 MB，超过必须走 prepare/part/finish 分片；
  - 应用上传的文件归应用所有，不授权的话用户点开只有「无权限」。

本地仍是真相源：上传失败只影响「云端有没有副本」，不影响录音与回听。
"""
from __future__ import annotations

import io
import re
import traceback
from pathlib import Path

from . import logs, store
from .config import AUDIO_DIR

# upload_all 的硬上限；超过走分片。放到模块级是为了测试能改小。
MAX_SINGLE_UPLOAD = 20 * 1024 * 1024
# 兜底上传 WAV 的上限：超过就别在请求线程里硬传了，等转码。
MAX_WAV_UPLOAD = 300 * 1024 * 1024
_DEFAULT_BLOCK = 4 * 1024 * 1024

_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(name: str, default: str = "recording.m4a") -> str:
    """去掉文件名里飞书不接受的字符；空名回退到 default。"""
    cleaned = _BAD_NAME.sub("_", str(name or "")).strip(" ._")
    return cleaned[:120] or default


def audio_path_for(session: dict) -> Path | None:
    """找这一场当前可播的音频。

    优先已转码的 m4a：WAV 是未压缩 PCM（约 115 MB/小时），上传又慢又占配额，
    转码完成后 m4a 只有它的十分之一。只有 WAV 时才退回 WAV。
    """
    sid = str(session.get("id") or "")
    raw = str(session.get("audio_path") or "")
    candidates: list[Path] = []
    if sid:
        candidates.append(AUDIO_DIR / (sid + ".m4a"))
    if raw:
        candidates.append(Path(raw))
    if sid:
        candidates.append(AUDIO_DIR / (sid + ".wav"))
        candidates.append(AUDIO_DIR / (sid + ".opus"))
    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if p.is_file():
            return p
    return None


def file_url(file_token: str, base: str = "") -> str:
    """拼云空间文件链接。没有租户域名时返回空串。

    通用域名 https://feishu.cn/file/<token> 会 301 到 www 再跳登录页
    （2026-11-27 实测），手机上点开是飞书首页——不如不给链接。
    """
    root = (base or "").strip()
    if not root:
        return ""
    if not root.endswith("/"):
        root += "/"
    return root + str(file_token)


def _ok(resp) -> bool:
    try:
        return bool(resp.success())
    except Exception:
        return False


def _err(resp) -> str:
    return "code=%s %s" % (getattr(resp, "code", ""), getattr(resp, "msg", ""))


def _result(resp, size: int, chunks: int = 1) -> dict:
    if not _ok(resp):
        return {"ok": False, "error": _err(resp)}
    data = getattr(resp, "data", None)
    token = str(getattr(data, "file_token", "") or "")
    if not token:
        return {"ok": False, "error": "上传成功但没返回 file_token"}
    return {"ok": True, "file_token": token,
            "url": str(getattr(data, "url", "") or ""),
            "size": int(size), "chunks": int(chunks)}


def _upload_all(client, path: Path, name: str, folder: str, size: int) -> dict:
    from lark_oapi.api.drive.v1 import UploadAllFileRequest, UploadAllFileRequestBody
    with open(path, "rb") as f:
        body = (UploadAllFileRequestBody.builder()
                .file_name(name).parent_type("explorer").parent_node(folder)
                .size(size).file(f).build())
        req = UploadAllFileRequest.builder().request_body(body).build()
        resp = client.drive.v1.file.upload_all(req)
    return _result(resp, size, 1)


def _upload_chunked(client, path: Path, name: str, folder: str, size: int) -> dict:
    from lark_oapi.api.drive.v1 import (
        FileUploadInfo, UploadFinishFileRequest, UploadFinishFileRequestBody,
        UploadPartFileRequest, UploadPartFileRequestBody, UploadPrepareFileRequest)

    info = (FileUploadInfo.builder()
            .file_name(name).parent_type("explorer").parent_node(folder)
            .size(size).build())
    prep = client.drive.v1.file.upload_prepare(
        UploadPrepareFileRequest.builder().request_body(info).build())
    if not _ok(prep):
        return {"ok": False, "error": "分片初始化失败：" + _err(prep)}
    data = getattr(prep, "data", None)
    upload_id = str(getattr(data, "upload_id", "") or "")
    block_size = int(getattr(data, "block_size", 0) or 0) or _DEFAULT_BLOCK
    block_num = int(getattr(data, "block_num", 0) or 0)
    if not upload_id:
        return {"ok": False, "error": "分片初始化没返回 upload_id"}
    if block_num <= 0:
        block_num = max(1, (size + block_size - 1) // block_size)

    seq = 0
    with open(path, "rb") as f:
        while seq < block_num:
            chunk = f.read(block_size)
            if not chunk:
                break
            body = (UploadPartFileRequestBody.builder()
                    .upload_id(upload_id).seq(seq).size(len(chunk))
                    .file(io.BytesIO(chunk)).build())
            resp = client.drive.v1.file.upload_part(
                UploadPartFileRequest.builder().request_body(body).build())
            if not _ok(resp):
                return {"ok": False,
                        "error": "第 %d 片上传失败：%s" % (seq, _err(resp))}
            seq += 1
    if seq != block_num:
        return {"ok": False,
                "error": "分片数不符：应传 %d 片，实际 %d 片" % (block_num, seq)}

    fin = client.drive.v1.file.upload_finish(
        UploadFinishFileRequest.builder().request_body(
            UploadFinishFileRequestBody.builder()
            .upload_id(upload_id).block_num(block_num).build()).build())
    return _result(fin, size, block_num)


def upload_file(client, path, folder_token: str, file_name: str = "") -> dict:
    """上传一个文件到云空间文件夹。<=20 MB 一次传，否则分片。

    ``client`` 是 ``lark.Client``；测试里可以传形状相同的假对象。
    返回 ``{ok, file_token, url, size, chunks}`` 或 ``{ok: False, error}``。
    """
    p = Path(path)
    if not folder_token:
        return {"ok": False, "error": "没有配置云空间文件夹 token"}
    if not p.is_file():
        return {"ok": False, "error": "音频文件不存在：%s" % p.name}
    size = p.stat().st_size
    if size <= 0:
        return {"ok": False, "error": "音频文件为空"}
    name = safe_name(file_name or p.name, p.name)
    try:
        if size <= MAX_SINGLE_UPLOAD:
            return _upload_all(client, p, name, folder_token, size)
        return _upload_chunked(client, p, name, folder_token, size)
    except Exception as e:
        traceback.print_exc()
        logs.log_event("feishu.drive_upload_failed", level="warn",
                       file=p.name, size=size, error=str(e))
        return {"ok": False, "error": "上传异常：%s" % e}


def grant_member(client, file_token: str, open_id: str,
                 perm: str = "full_access") -> dict:
    """把云空间文件授权给某个人。不授权的话，用户点链接只会看到无权限。"""
    if not file_token or not open_id:
        return {"ok": False, "error": "缺少 file_token 或 open_id"}
    try:
        from lark_oapi.api.drive.v1 import BaseMember, CreatePermissionMemberRequest
        body = (BaseMember.builder()
                .member_type("openid").member_id(str(open_id))
                .perm(perm or "full_access").build())
        req = (CreatePermissionMemberRequest.builder()
               .token(str(file_token)).type("file")
               .need_notification(False).request_body(body).build())
        resp = client.drive.v1.permission_member.create(req)
        if _ok(resp):
            logs.log_event("feishu.drive_shared", file_token=file_token[:12])
            return {"ok": True}
        err = _err(resp)
    except Exception as e:
        traceback.print_exc()
        err = str(e)
    logs.log_event("feishu.drive_share_failed", level="warn",
                   file_token=file_token[:12], error=err)
    return {"ok": False, "error": err}


def _default_bot():
    from . import feishu
    return feishu.Bot()


def _delete_file(client, token: str) -> bool:
    """删掉被替换掉的旧云端副本。失败只记 warn，多一个孤儿文件不影响结果。"""
    try:
        from lark_oapi.api.drive.v1 import DeleteFileRequest
        resp = client.drive.v1.file.delete(
            DeleteFileRequest.builder().file_token(str(token)).type("file").build())
        if _ok(resp):
            return True
        err = _err(resp)
    except Exception as e:
        traceback.print_exc()
        err = str(e)
    logs.log_event("feishu.drive_delete_failed", level="warn",
                   file_token=str(token)[:12], error=err)
    return False


def _do_archive(conn, session: dict, bot, path: Path) -> dict:
    """上传 + 授权 + 写库。手动归档与发布前归档共用这一段，写出的列一致。"""
    sid = str(session.get("id") or "")
    folder = str((bot.cfg or {}).get("audio_folder_token") or "").strip()
    if not folder:
        return {"ok": False,
                "error": "没有配置云空间文件夹：设置 FEISHU_AUDIO_FOLDER_TOKEN"}

    title = str(session.get("title") or sid)
    name = safe_name(title + path.suffix, (sid or "recording") + path.suffix)
    out = upload_file(bot.client, path, folder, name)
    if not out.get("ok"):
        return out

    token = out["file_token"]
    url = out.get("url") or file_url(token, str((bot.cfg or {}).get("drive_url_base") or ""))
    owner = str(getattr(bot, "owner_open_id", "")
                or (bot.cfg or {}).get("owner_open_id") or "")
    share = grant_member(bot.client, token, owner) if owner else {
        "ok": False, "error": "没有可授权的操作者（先给机器人发一句话）"}

    # 没有可信链接时写 NULL：文档里的「## 录音」据此决定出不出现。
    store.set_audio_meta(conn, sid, drive_token=token, drive_url=url or None,
                         drive_uploaded_at=store.now())
    logs.log_event("feishu.drive_archived", session_id=sid,
                   size=out.get("size"), chunks=out.get("chunks"),
                   shared=bool(share.get("ok")))
    return {"ok": True, "session_id": sid, "file_token": token, "url": url,
            "size": out.get("size", 0), "chunks": out.get("chunks", 1),
            "shared": bool(share.get("ok")),
            "share_error": "" if share.get("ok") else share.get("error", "")}


def archive_session(conn, session: dict, bot=None) -> dict:
    """把一场录音上传到云空间并授权给操作者，成功后把 token/链接写回库里。

    失败不抛异常：返回 ``{ok: False, error}``，由接口层转成 4xx/5xx。
    重复调用会重新上传并覆盖库里的链接（飞书侧同名文件会并存，不做去重）。
    """
    sid = str(session.get("id") or "")
    path = audio_path_for(session)
    if not path:
        return {"ok": False, "error": "这场没有可上传的音频"}
    # WAV 是兜底：超长场次的 WAV 动辄 1 GB，别让用户在请求线程里等半小时。
    if path.suffix.lower() == ".wav" and path.stat().st_size > MAX_WAV_UPLOAD:
        return {"ok": False,
                "error": "WAV 还没转码完成（%d MB），等 m4a 生成后再存"
                         % (path.stat().st_size // (1024 * 1024))}
    try:
        bot = bot or _default_bot()
    except Exception as e:
        return {"ok": False, "error": "飞书未配置：%s" % e}
    return _do_archive(conn, session, bot, path)


def ensure_archived(conn, session: dict, bot=None) -> dict:
    """幂等归档：已经传过且不比转码旧就跳过。

    返回 {ok, skipped, file_token, url, replaced, error}。
    任何失败都不抛异常——云端副本缺失不影响本地录音、逐字稿和发布结果。
    """
    sid = str(session.get("id") or "")
    if not sid:
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False, "error": "缺少场次 id"}
    path = audio_path_for(session)
    if not path:
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False, "error": "这场没有可上传的音频"}
    if path.suffix.lower() == ".wav" and path.stat().st_size > MAX_WAV_UPLOAD:
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False,
                "error": "WAV 还没转码完成（%d MB），等 m4a 生成后再存"
                         % (path.stat().st_size // (1024 * 1024))}

    old_token = str(session.get("drive_token") or "")
    uploaded_at = str(session.get("drive_uploaded_at") or "")
    transcoded_at = str(session.get("audio_transcoded_at") or "")
    if old_token and uploaded_at >= transcoded_at:
        return {"ok": True, "skipped": True, "file_token": old_token,
                "url": str(session.get("drive_url") or ""),
                "replaced": False, "error": ""}

    try:
        bot = bot or _default_bot()
    except Exception as e:
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False, "error": "飞书未配置：%s" % e}
    try:
        out = _do_archive(conn, session, bot, path)
        if not out.get("ok"):
            err = str(out.get("error") or "")
            logs.log_event("feishu.drive_archive_failed", level="warn",
                           session_id=sid, error=err)
            return {"ok": False, "skipped": False, "file_token": "", "url": "",
                    "replaced": False, "error": err}
        token = str(out.get("file_token") or "")
        replaced = bool(old_token and old_token != token
                        and _delete_file(bot.client, old_token))
        return {"ok": True, "skipped": False, "file_token": token,
                "url": str(out.get("url") or ""), "replaced": replaced,
                "error": ""}
    except Exception as e:
        traceback.print_exc()
        logs.log_event("feishu.drive_archive_failed", level="warn",
                       session_id=sid, error=str(e))
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False, "error": str(e)}
