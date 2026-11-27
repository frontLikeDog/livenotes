# -*- coding: utf-8 -*-
"""把整场录音存进飞书云空间（D2：应用云空间 + 授权给操作者）。

为什么不是「用户自己的空间」：那需要 user_access_token 和 OAuth 回调，桌面端
没有公网域名时跑不通。应用用 tenant_access_token 上传到自己的文件夹，再把文件
授权给操作者，分享仍然走飞书原生权限 —— 用户在飞书里选人/选群/设权限，我们
不做接收人选择器。

两条硬约束：
  - ``upload_all`` 单文件上限 20 MB，分片上传也绕不过（2026-11-27 实测），
    更长的场次按时间轴切成多段分别上传；
  - 应用上传的文件归应用所有，不授权的话用户点开只有「无权限」。

本地仍是真相源：上传失败只影响「云端有没有副本」，不影响录音与回听。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

from . import audio, feishu_http, logs, store
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


def folder_name(session: dict) -> str:
    """一场在云空间里的目录名：``2026-11-25_2130_开源AI聊天室``。

    用开始时间 + 房间名；解析不了时间就退到日期，房间查不到就用标题。
    目录名只负责可读，真正的索引是 ``sessions.drive_folder_token``。
    """
    sid = str(session.get("id") or "")
    raw = str(session.get("started_at") or "").strip()
    stamp = ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            stamp = datetime.strptime(raw[:19], fmt).strftime("%Y-%m-%d_%H%M")
            break
        except ValueError:
            continue
    if not stamp:
        try:
            stamp = datetime.fromisoformat(raw).strftime("%Y-%m-%d_%H%M")
        except (TypeError, ValueError):
            stamp = raw[:10] if raw else ""
    label = ""
    if sid:
        try:
            from . import rooms
            room = rooms.get_by_session(sid) or {}
            label = str(room.get("name") or "")
        except Exception:
            label = ""
    label = label or str(session.get("title") or "") or sid or "场次"
    core = "%s_%s" % (stamp or sid or "未定", label[:20])
    return safe_name(core, sid or "场次")


def create_folder(client, parent_token: str, name: str) -> dict:
    """在云空间建一个文件夹。同名已存在时飞书会报错，由调用方 list 兜底。"""
    if not parent_token:
        return {"ok": False, "error": "没有配置父文件夹 token"}
    try:
        body = feishu_http.obj(name=safe_name(name), folder_token=str(parent_token))
        resp = client.drive.v1.file.create_folder(
            feishu_http.obj(request_body=body))
        if not _ok(resp):
            return {"ok": False, "error": "建文件夹失败：" + _err(resp)}
        data = getattr(resp, "data", None)
        token = str(getattr(data, "token", "") or "")
        if not token:
            return {"ok": False, "error": "建文件夹没返回 token"}
        return {"ok": True, "token": token,
                "url": str(getattr(data, "url", "") or "")}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": "建文件夹异常：%s" % e}


def list_folder(client, folder_token: str) -> list[dict]:
    """拉完一个文件夹下的直接子项：``[{token,name,type,url}]``。"""
    if not folder_token:
        return []
    try:
        out: list[dict] = []
        token = ""
        for _ in range(50):
            req = feishu_http.obj(
                folder_token=str(folder_token),
                page_size=200,
                page_token=token or None,
            )
            resp = client.drive.v1.file.list(req)
            if not _ok(resp):
                return out
            data = getattr(resp, "data", None)
            for f in (getattr(data, "files", None) or []):
                out.append({
                    "token": str(getattr(f, "token", "") or ""),
                    "name": str(getattr(f, "name", "") or ""),
                    "type": str(getattr(f, "type", "") or ""),
                    "url": str(getattr(f, "url", "") or ""),
                })
            token = str(getattr(data, "next_page_token", "") or "")
            if not getattr(data, "has_more", False) or not token:
                break
        return out
    except Exception as e:
        logs.log_event("feishu.drive_list_failed", level="warn",
                       folder_token=str(folder_token)[:12], error=str(e))
        return []


def _archive_chat_id(bot) -> str:
    cfg = getattr(bot, "cfg", {}) or {}
    chat = str(cfg.get("archive_chat_id") or "").strip()
    if chat:
        return chat
    ids = cfg.get("chat_ids") or []
    return str(ids[0]).strip() if ids else ""


def _grant_folder_to_archive_chat(conn, bot, folder_token: str, sid: str) -> str:
    """把场次文件夹授权给群；成功写 ``drive_folder_granted_at``，失败下次再试。"""
    chat = _archive_chat_id(bot)
    if not chat or not folder_token:
        return ""
    fresh = store.get_session(conn, sid) or {}
    if fresh.get("drive_folder_granted_at"):
        return ""
    perm = str((getattr(bot, "cfg", {}) or {}).get("archive_chat_perm")
               or "view")
    out = grant_folder_to_chat(bot.client, folder_token, chat, perm=perm)
    if out.get("ok"):
        store.set_audio_meta(conn, sid,
                             drive_folder_granted_at=store.now())
        return ""
    err = str(out.get("error") or "")
    logs.log_event("feishu.drive_folder_grant_failed", level="warn",
                   session_id=sid, chat_id=chat[:12], error=err[:200])
    return err


def ensure_session_folder(conn, session: dict, bot) -> dict:
    """取或建这场专属的云空间文件夹，返回 ``{ok,token,url,created,error}``。

    已有 ``drive_folder_token`` 直接用；建失败（多半同名）时列父目录找同名
    文件夹复用；拿到 token 后写库，并按配置授权给群。
    """
    sid = str(session.get("id") or "")
    fresh = (store.get_session(conn, sid) if sid else None) or session or {}
    token = str(fresh.get("drive_folder_token") or "")
    url = str(fresh.get("drive_folder_url") or "")
    created = False
    if not token:
        cfg = getattr(bot, "cfg", {}) or {}
        parent = str(cfg.get("audio_folder_token") or "").strip()
        if not parent:
            return {"ok": False, "token": "", "url": "", "created": False,
                    "error": "没有配置云空间文件夹：设置 FEISHU_AUDIO_FOLDER_TOKEN"}
        name = folder_name(fresh)
        made = create_folder(bot.client, parent, name)
        if made.get("ok"):
            token = str(made.get("token") or "")
            url = str(made.get("url") or "")
            created = True
        else:
            for f in list_folder(bot.client, parent):
                if f.get("name") == name and f.get("token"):
                    token = str(f["token"])
                    url = str(f.get("url") or "")
                    break
            if not token:
                return {"ok": False, "token": "", "url": "", "created": False,
                        "error": str(made.get("error") or "建场次文件夹失败")}
        store.set_audio_meta(conn, sid, drive_folder_token=token,
                             drive_folder_url=url or None)
        logs.log_event("feishu.drive_folder", session_id=sid,
                       folder_token=token[:12], created=created)
    grant_error = _grant_folder_to_archive_chat(conn, bot, token, sid)
    return {"ok": True, "token": token, "url": url, "created": created,
            "grant_error": grant_error}


def _target_folder(conn, session: dict, bot) -> dict:
    """归档目标文件夹：优先场次专属目录；建不出来就退回父文件夹。

    退回是刻意的：目录结构是加分项，不能因为它把整场录音的上传拖失败。
    """
    parent = str((getattr(bot, "cfg", {}) or {}).get("audio_folder_token")
                 or "").strip()
    if not parent:
        return {"ok": False,
                "error": "没有配置云空间文件夹：设置 FEISHU_AUDIO_FOLDER_TOKEN"}
    res = ensure_session_folder(conn, session, bot)
    if res.get("ok"):
        return res
    logs.log_event("feishu.drive_folder_fallback", level="warn",
                   session_id=session.get("id"),
                   error=str(res.get("error") or "")[:200])
    return {"ok": True, "token": parent, "url": "", "created": False,
            "fallback": True, "error": str(res.get("error") or "")}


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
    with open(path, "rb") as f:
        body = feishu_http.obj(
            file_name=name, parent_type="explorer", parent_node=folder,
            size=size, file=f,
        )
        req = feishu_http.obj(request_body=body)
        resp = client.drive.v1.file.upload_all(req)
    return _result(resp, size, 1)


def _upload_chunked(client, path: Path, name: str, folder: str, size: int) -> dict:
    info = feishu_http.obj(
        file_name=name, parent_type="explorer", parent_node=folder, size=size,
    )
    prep = client.drive.v1.file.upload_prepare(
        feishu_http.obj(request_body=info))
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
            body = feishu_http.obj(
                upload_id=upload_id, seq=seq, size=len(chunk),
                file=io.BytesIO(chunk),
            )
            resp = client.drive.v1.file.upload_part(
                feishu_http.obj(request_body=body))
            if not _ok(resp):
                return {"ok": False,
                        "error": "第 %d 片上传失败：%s" % (seq, _err(resp))}
            seq += 1
    if seq != block_num:
        return {"ok": False,
                "error": "分片数不符：应传 %d 片，实际 %d 片" % (block_num, seq)}

    fin = client.drive.v1.file.upload_finish(
        feishu_http.obj(
            request_body=feishu_http.obj(upload_id=upload_id, block_num=block_num)))
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
                 perm: str = "full_access", doc_type: str = "file",
                 member_type: str = "openid") -> dict:
    """把云空间资源授权给某个人或某个群。

    ``doc_type`` 是飞书资源类型（file / folder / bitable ...），``member_type``
    是接收方类型（openid / openchat）。不授权的话，用户点链接只会看到无权限。
    """
    if not file_token or not open_id:
        return {"ok": False, "error": "缺少 file_token 或 open_id"}
    try:
        body = feishu_http.obj(
            member_type=str(member_type or "openid"),
            member_id=str(open_id),
            perm=perm or "full_access",
        )
        req = feishu_http.obj(
            token=str(file_token),
            type=str(doc_type or "file"),
            need_notification=False,
            request_body=body,
        )
        resp = client.drive.v1.permission_member.create(req)
        if _ok(resp):
            logs.log_event("feishu.drive_shared", file_token=file_token[:12],
                           doc_type=doc_type, member_type=member_type)
            return {"ok": True}
        err = _err(resp)
    except Exception as e:
        traceback.print_exc()
        err = str(e)
    logs.log_event("feishu.drive_share_failed", level="warn",
                   file_token=file_token[:12], error=err)
    return {"ok": False, "error": err}


def grant_folder_to_chat(client, folder_token: str, chat_id: str,
                         perm: str = "view") -> dict:
    """把文件夹授权给群：之后放进这个文件夹的文件自动对群成员可见。

    默认 ``view``：能看能转，不能删。``need_notification=False`` 是硬要求，
    否则每建一个场次目录都会在群里弹一次通知。
    """
    return grant_member(client, folder_token, chat_id, perm=perm,
                        doc_type="folder", member_type="openchat")


def _default_bot():
    from . import feishu
    return feishu.Bot()


def _delete_file(client, token: str) -> bool:
    """删掉被替换掉的旧云端副本。失败只记 warn，多一个孤儿文件不影响结果。"""
    try:
        resp = client.drive.v1.file.delete(
            feishu_http.obj(file_token=str(token), type="file"))
        if _ok(resp):
            return True
        err = _err(resp)
    except Exception as e:
        traceback.print_exc()
        err = str(e)
    logs.log_event("feishu.drive_delete_failed", level="warn",
                   file_token=str(token)[:12], error=err)
    return False


def _replace_file(client, folder_token: str, file_name: str, path: Path,
                  old_token: str = "") -> dict:
    """上传新文件，并删掉被替换的旧文件（飞书没有覆盖写）。"""
    out = upload_file(client, path, folder_token, file_name)
    if not out.get("ok"):
        return out
    new_token = str(out.get("file_token") or "")
    replaced = False
    if old_token and new_token and str(old_token) != new_token:
        replaced = _delete_file(client, str(old_token))
    res = dict(out)
    res["replaced"] = replaced
    return res


def move_file(client, file_token: str, folder_token: str,
              file_type: str = "file") -> dict:
    """把一个云空间文件/文件夹移到另一个文件夹；移动保留原 token 和链接。"""
    if not file_token or not folder_token:
        return {"ok": False, "error": "缺少 file_token 或 folder_token"}
    try:
        body = feishu_http.obj(
            type=str(file_type or "file"),
            folder_token=str(folder_token),
        )
        resp = client.drive.v1.file.move(
            feishu_http.obj(file_token=str(file_token), request_body=body))
        if not _ok(resp):
            return {"ok": False, "error": "移动文件失败：" + _err(resp)}
        return {"ok": True}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": "移动文件异常：%s" % e}


# ---------------------------------------------------------------- 切片归档

def _parts_from(session: dict) -> list[dict]:
    """读 ``drive_parts_json``；坏数据当空列表，不让它拖垮归档。"""
    raw = str(session.get("drive_parts_json") or "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [dict(x) for x in data if isinstance(x, dict)]


def _archive_sliced(conn, session: dict, bot, path: Path, target: dict) -> dict:
    """超过单文件上限的录音：切段上传到同一场次文件夹，库里记 parts。

    文档和场次表的「录音」链接指向场次文件夹（一段一个文件，按名字排序），
    文件夹本身也授权给操作者，手机上点进去能逐段播放。
    """
    sid = str(session.get("id") or "")
    folder = str(target.get("token") or "")
    folder_url = str(target.get("url") or "")
    cfg = getattr(bot, "cfg", {}) or {}
    owner = str(getattr(bot, "owner_open_id", "")
                or cfg.get("owner_open_id") or "")
    work = Path(tempfile.mkdtemp(prefix="lnsplit-", dir=str(path.parent)))
    try:
        parts, err = audio.split_m4a(path, MAX_SINGLE_UPLOAD, work)
        if not parts:
            return {"ok": False, "error": err or "切片失败"}
        title = safe_name(str(session.get("title") or sid))
        uploaded: list[dict] = []
        for i, part in enumerate(parts, 1):
            name = "%s_%02d%s" % (title, i, path.suffix)
            out = upload_file(bot.client, part, folder, name)
            if not out.get("ok"):
                for u in uploaded:
                    _delete_file(bot.client, str(u.get("token") or ""))
                return {"ok": False,
                        "error": "第 %d/%d 段上传失败：%s" % (
                            i, len(parts), out.get("error") or "")}
            token = str(out.get("file_token") or "")
            url = out.get("url") or file_url(
                token, str(cfg.get("drive_url_base") or ""))
            share = grant_member(bot.client, token, owner) if owner else {
                "ok": False, "error": "没有可授权的操作者"}
            uploaded.append({"token": token, "url": url, "name": name,
                             "size": int(out.get("size")
                                         or part.stat().st_size),
                             "shared": bool(share.get("ok"))})

        # 文件夹链接要能点开，文件夹本身也得授权给操作者。
        folder_shared = False
        if owner and folder:
            folder_shared = bool(grant_member(
                bot.client, folder, owner, doc_type="folder").get("ok"))

        # 新副本全部成功后再删旧副本（单个文件或旧分段）。
        old = [str(session.get("drive_token") or "")]
        old += [str(p.get("token") or "") for p in _parts_from(session)]
        keep = {u["token"] for u in uploaded}
        for tok in dict.fromkeys(t for t in old if t and t not in keep):
            _delete_file(bot.client, tok)

        url = folder_url or str(uploaded[0].get("url") or "")
        store.set_audio_meta(
            conn, sid, drive_token=None, drive_url=url or None,
            drive_uploaded_at=store.now(),
            drive_parts_json=json.dumps(uploaded, ensure_ascii=False),
            audio_media_token=None)
        logs.log_event("feishu.drive_archived", session_id=sid,
                       size=path.stat().st_size, parts=len(uploaded),
                       folder_token=folder[:12], sliced=True,
                       folder_shared=folder_shared)
        res = {"ok": True, "session_id": sid, "file_token": "", "url": url,
               "size": path.stat().st_size, "chunks": len(uploaded),
               "parts": len(uploaded), "part_list": uploaded, "sliced": True,
               "shared": all(u["shared"] for u in uploaded),
               "folder_token": folder, "folder_url": folder_url,
               "folder_created": bool(target.get("created")),
               "folder_shared": folder_shared, "replaced": False}
        if target.get("fallback"):
            res["folder_error"] = str(target.get("error") or "")
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- Base 附件素材

def _media_upload_all(client, path: Path, name: str, parent_node: str,
                      parent_type: str, size: int) -> dict:
    with open(path, "rb") as f:
        body = feishu_http.obj(
            file_name=name, parent_type=parent_type, parent_node=parent_node,
            size=size, file=f,
        )
        resp = client.drive.v1.media.upload_all(
            feishu_http.obj(request_body=body))
    return _result(resp, size, 1)


def _media_upload_chunked(client, path: Path, name: str, parent_node: str,
                          parent_type: str, size: int) -> dict:
    info = feishu_http.obj(
        file_name=name, parent_type=parent_type, parent_node=parent_node,
        size=size,
    )
    prep = client.drive.v1.media.upload_prepare(
        feishu_http.obj(request_body=info))
    if not _ok(prep):
        return {"ok": False, "error": "素材分片初始化失败：" + _err(prep)}
    data = getattr(prep, "data", None)
    upload_id = str(getattr(data, "upload_id", "") or "")
    block_size = int(getattr(data, "block_size", 0) or 0) or _DEFAULT_BLOCK
    block_num = int(getattr(data, "block_num", 0) or 0)
    if not upload_id:
        return {"ok": False, "error": "素材分片没返回 upload_id"}
    if block_num <= 0:
        block_num = max(1, (size + block_size - 1) // block_size)

    seq = 0
    with open(path, "rb") as f:
        while seq < block_num:
            chunk = f.read(block_size)
            if not chunk:
                break
            body = feishu_http.obj(
                upload_id=upload_id, seq=seq, size=len(chunk),
                file=io.BytesIO(chunk),
            )
            resp = client.drive.v1.media.upload_part(
                feishu_http.obj(request_body=body))
            if not _ok(resp):
                return {"ok": False,
                        "error": "素材第 %d 片上传失败：%s" % (seq, _err(resp))}
            seq += 1
    if seq != block_num:
        return {"ok": False,
                "error": "素材分片数不符：应传 %d 片，实际 %d 片"
                         % (block_num, seq)}
    fin = client.drive.v1.media.upload_finish(
        feishu_http.obj(
            request_body=feishu_http.obj(upload_id=upload_id, block_num=block_num)))
    return _result(fin, size, block_num)


def upload_media(client, path, parent_node: str,
                 parent_type: str = "bitable_file",
                 file_name: str = "") -> dict:
    """上传 Base 附件素材，返回 ``{ok,file_token,size,chunks}``。

    附件字段（type 17）要的是 media token，不是普通文件的 file_token；
    ``parent_node`` 是 Base 的 app_token。
    """
    p = Path(path)
    if not parent_node:
        return {"ok": False, "error": "没有 Base app_token"}
    if not p.is_file():
        return {"ok": False, "error": "素材文件不存在：%s" % p.name}
    size = p.stat().st_size
    if size <= 0:
        return {"ok": False, "error": "素材文件为空"}
    name = safe_name(file_name or p.name, p.name)
    try:
        if size <= MAX_SINGLE_UPLOAD:
            return _media_upload_all(client, p, name, parent_node,
                                     parent_type, size)
        return _media_upload_chunked(client, p, name, parent_node,
                                     parent_type, size)
    except Exception as e:
        traceback.print_exc()
        logs.log_event("feishu.drive_media_upload_failed", level="warn",
                       file=p.name, size=size, error=str(e))
        return {"ok": False, "error": "素材上传异常：%s" % e}


def _write_tmp(text: str, suffix: str = ".txt") -> Path:
    fd, name = tempfile.mkstemp(prefix="livenotes-", suffix=suffix)
    os.close(fd)
    p = Path(name)
    p.write_text(text, encoding="utf-8")
    return p


def transcript_name(session: dict) -> str:
    sid = str(session.get("id") or "")
    return safe_name(folder_name(session) + ".txt",
                     (sid or "transcript") + ".txt")


def ensure_transcript(conn, session: dict, bot=None, text: str = "") -> dict:
    """把逐字稿写成 txt 放进场次文件夹，内容没变就不重传。

    返回 ``{ok,skipped,text,token,url,hash,error}``；``text`` 始终带回，附件
    素材上传可以继续用，不依赖文件夹上传是否成功。
    """
    sid = str(session.get("id") or "")
    cfg = getattr(bot, "cfg", {}) or {}
    body = str(text or "")
    if not body:
        try:
            from . import export
            body = export.to_archive_txt(conn, sid)
        except Exception:
            traceback.print_exc()
            body = ""
    if not body.strip():
        return {"ok": False, "skipped": True, "text": "", "token": "", "url": "",
                "hash": "", "error": "还没有逐字稿"}
    if cfg.get("archive_transcript") is False:
        return {"ok": True, "skipped": True, "text": body, "token": "", "url": "",
                "hash": "", "error": ""}

    digest = hashlib.sha1(body.encode("utf-8")).hexdigest()
    fresh = (store.get_session(conn, sid) if sid else None) or session or {}
    old_token = str(fresh.get("transcript_token") or "")
    old_hash = str(fresh.get("transcript_hash") or "")
    old_url = str(fresh.get("transcript_url") or "")
    if old_token and old_hash == digest:
        return {"ok": True, "skipped": True, "text": body, "token": old_token,
                "url": old_url, "hash": digest, "error": ""}
    if old_hash != digest:
        # 内容变了：旧附件素材指向旧逐字稿，先失效，传成功后再补新 token。
        store.set_audio_meta(conn, sid, transcript_media_token=None)

    try:
        bot = bot or _default_bot()
        target = _target_folder(conn, fresh, bot)
        if not target.get("ok"):
            return {"ok": False, "skipped": False, "text": body, "token": "",
                    "url": "", "hash": digest,
                    "error": str(target.get("error") or "没有可用的云空间文件夹")}
        name = transcript_name(fresh)
        tmp = _write_tmp(body)
        try:
            out = _replace_file(bot.client, target["token"], name, tmp,
                                old_token)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        if not out.get("ok"):
            logs.log_event("feishu.transcript_upload_failed", level="warn",
                           session_id=sid, error=str(out.get("error") or "")[:200])
            return {"ok": False, "skipped": False, "text": body, "token": "",
                    "url": "", "hash": digest,
                    "error": str(out.get("error") or "逐字稿上传失败")}
        token = str(out.get("file_token") or "")
        url = (str(out.get("url") or "")
               or file_url(token, str(cfg.get("drive_url_base") or "")))
        store.set_audio_meta(
            conn, sid, transcript_token=token, transcript_url=url or None,
            transcript_hash=digest, transcript_uploaded_at=store.now(),
            # 内容变了，Base 附件素材也要重传。
            transcript_media_token=None)
        logs.log_event("feishu.transcript_uploaded", session_id=sid,
                       token=token[:12], bytes=len(body.encode("utf-8")))
        return {"ok": True, "skipped": False, "text": body, "token": token,
                "url": url, "hash": digest, "error": ""}
    except Exception as e:
        traceback.print_exc()
        logs.log_event("feishu.transcript_upload_failed", level="warn",
                       session_id=sid, error=str(e)[:200])
        return {"ok": False, "skipped": False, "text": body, "token": "",
                "url": "", "hash": digest, "error": str(e)}


def prepare_attachments(conn, session: dict, bot, app_token: str) -> dict:
    """为场次表准备「录音」「逐字稿」两个附件字段的 media token。

    上传失败只收集到 ``errors``，调用方仍要照常写这一行；附件是加分项，
    不能让整行失败。token 缓存在 sessions 列里，重复发布不重传。
    """
    sid = str(session.get("id") or "")
    out: dict = {"ok": True, "audio_token": "", "transcript_token": "",
                 "transcript_url": "", "errors": []}
    if not app_token:
        out["ok"] = False
        out["errors"].append("没有 Base app_token")
        return out
    cfg = getattr(bot, "cfg", {}) or {}
    if cfg.get("archive_transcript") is False and cfg.get("base_attachments") is False:
        return out

    tr = ensure_transcript(conn, session, bot)
    text = str(tr.get("text") or "")
    if tr.get("ok"):
        out["transcript_url"] = str(tr.get("url") or "")
    elif not tr.get("skipped"):
        out["errors"].append("逐字稿：%s" % tr.get("error", ""))
    fresh = (store.get_session(conn, sid) if sid else None) or session or {}

    # 录音附件：本地有音频就传一份 media；和文件夹里的副本是两份，服务不同入口。
    audio_token = str(fresh.get("audio_media_token") or "")
    if not audio_token:
        path = audio_path_for(fresh)
        if path:
            name = safe_name(str(fresh.get("title") or sid) + path.suffix,
                             path.name)
            up = upload_media(bot.client, path, app_token,
                              file_name=name)
            if up.get("ok"):
                audio_token = str(up.get("file_token") or "")
                store.set_audio_meta(conn, sid, audio_media_token=audio_token)
            else:
                out["errors"].append("录音附件：%s" % up.get("error", ""))
    out["audio_token"] = audio_token

    # 逐字稿附件：用同一份 txt 再传一次 media。
    transcript_token = str(fresh.get("transcript_media_token") or "")
    if not transcript_token and text.strip():
        tmp = _write_tmp(text)
        try:
            up = upload_media(bot.client, tmp, app_token,
                              file_name=transcript_name(fresh))
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        if up.get("ok"):
            transcript_token = str(up.get("file_token") or "")
            store.set_audio_meta(conn, sid,
                                 transcript_media_token=transcript_token)
        else:
            out["errors"].append("逐字稿附件：%s" % up.get("error", ""))
    out["transcript_token"] = transcript_token
    out["ok"] = not out["errors"]
    return out


def _do_archive(conn, session: dict, bot, path: Path) -> dict:
    """上传 + 授权 + 写库。手动归档与发布前归档共用这一段，写出的列一致。"""
    sid = str(session.get("id") or "")
    target = _target_folder(conn, session, bot)
    if not target.get("ok"):
        return target
    folder = str(target["token"])

    size = path.stat().st_size
    if size > MAX_SINGLE_UPLOAD:
        # 飞书单文件上限 20MB，分片上传也绕不过：m4a 切片，别的格式先转码。
        if path.suffix.lower() in (".m4a", ".mp4"):
            return _archive_sliced(conn, session, bot, path, target)
        return {"ok": False,
                "error": "文件 %.1f MB 超过飞书单文件上限，且 %s 不能切片，"
                         "先转成 m4a 再归档"
                         % (size / 1048576.0, path.suffix or "无后缀")}

    title = str(session.get("title") or sid)
    name = safe_name(title + path.suffix, (sid or "recording") + path.suffix)
    out = _replace_file(bot.client, folder, name, path,
                        str(session.get("drive_token") or ""))
    if not out.get("ok"):
        return out

    token = out["file_token"]
    url = out.get("url") or file_url(token, str((bot.cfg or {}).get("drive_url_base") or ""))
    owner = str(getattr(bot, "owner_open_id", "")
                or (bot.cfg or {}).get("owner_open_id") or "")
    share = grant_member(bot.client, token, owner) if owner else {
        "ok": False, "error": "没有可授权的操作者（先给机器人发一句话）"}

    # 没有可信链接时写 NULL：文档里的「## 录音」据此决定出不出现。
    # audio_media_token 清空：音频换代后，Base 附件必须重新上传。
    # drive_parts_json 清空：这一版是整文件，旧的切片副本已经作废。
    old_parts = _parts_from(session)
    store.set_audio_meta(conn, sid, drive_token=token, drive_url=url or None,
                         drive_uploaded_at=store.now(),
                         drive_parts_json=None,
                         audio_media_token=None)
    for p in old_parts:
        tok = str(p.get("token") or "")
        if tok and tok != token:
            _delete_file(bot.client, tok)
    logs.log_event("feishu.drive_archived", session_id=sid,
                   size=out.get("size"), chunks=out.get("chunks"),
                   shared=bool(share.get("ok")),
                   folder_token=folder[:12], created=bool(target.get("created")))
    res = {"ok": True, "session_id": sid, "file_token": token, "url": url,
           "size": out.get("size", 0), "chunks": out.get("chunks", 1),
           "parts": 0,
           "shared": bool(share.get("ok")),
           "share_error": "" if share.get("ok") else share.get("error", ""),
           "folder_token": folder, "folder_url": str(target.get("url") or ""),
           "folder_created": bool(target.get("created")),
           "replaced": bool(out.get("replaced"))}
    if target.get("fallback"):
        res["folder_error"] = str(target.get("error") or "")
    return res


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
    old_parts = _parts_from(session)
    uploaded_at = str(session.get("drive_uploaded_at") or "")
    transcoded_at = str(session.get("audio_transcoded_at") or "")
    if (old_token or old_parts) and uploaded_at >= transcoded_at:
        return {"ok": True, "skipped": True, "file_token": old_token,
                "url": str(session.get("drive_url") or ""),
                "parts": len(old_parts),
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
        return {"ok": True, "skipped": False, "file_token": token,
                "url": str(out.get("url") or ""),
                "parts": int(out.get("parts") or 0),
                "replaced": bool(out.get("replaced")),
                "folder_token": str(out.get("folder_token") or ""),
                "folder_url": str(out.get("folder_url") or ""),
                "folder_created": bool(out.get("folder_created")),
                "folder_error": str(out.get("folder_error") or ""),
                "error": ""}
    except Exception as e:
        traceback.print_exc()
        logs.log_event("feishu.drive_archive_failed", level="warn",
                       session_id=sid, error=str(e))
        return {"ok": False, "skipped": False, "file_token": "", "url": "",
                "replaced": False, "error": str(e)}
