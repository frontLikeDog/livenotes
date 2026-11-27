# -*- coding: utf-8 -*-
"""Base 附件字段探针：一次真租户实验，验证「录像/逐字稿」能不能挂进场次表。

    python tools/spike_base_attachment.py            # 跑完自动清理临时字段与行
    python tools/spike_base_attachment.py --keep     # 留着现场，自己去 Base 看

回答的问题（docs/50 §3.2 的技术不确定性）：

1. ``drive/v1/medias/upload_all`` 以 ``parent_type=bitable_file``、
   ``parent_node=<app_token>`` 上传得到的 media token，能不能写进 type 17 附件字段；
2. 普通云空间文件 token（``drive/v1/files/upload_all``）写进附件字段会不会被拒；
3. 写进去之后读回来是什么形状，``tmp_url`` 能不能下载。

退出码：0 = 附件字段可用（2a）；1 = 不可用，退回链接列（2b）；2 = 没配置。

临时字段名带 ``spike_`` 前缀，跑完会删；``--keep`` 时不删，方便人工看一眼。
这个工具不碰正式字段，也不改任何场次行。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import feishu, feishu_drive, store  # noqa: E402


def _ok(resp) -> bool:
    try:
        return bool(resp.success())
    except Exception:
        return False


def _err(resp) -> str:
    return "code=%s %s" % (getattr(resp, "code", ""), getattr(resp, "msg", ""))


def _resolve(conn, args) -> tuple[str, str]:
    from app import feishu_publish
    cfg = feishu_publish.config()
    app_token = (args.app_token or cfg.get("base_app_token")
                 or store.get_kv(conn, "feishu_base_app_token") or "")
    table_id = (args.table_id or cfg.get("base_table_id")
                or store.get_kv(conn, "feishu_base_table_id") or "")
    return str(app_token), str(table_id)


def _add_field(client, app_token: str, table_id: str, name: str) -> dict:
    from lark_oapi.api.bitable.v1 import (
        AppTableField, CreateAppTableFieldRequest)
    field = AppTableField.builder().field_name(name).type(17).build()
    resp = client.bitable.v1.app_table_field.create(
        CreateAppTableFieldRequest.builder().app_token(app_token)
        .table_id(table_id).request_body(field).build())
    if not _ok(resp):
        return {"ok": False, "error": "建附件字段失败：" + _err(resp)}
    data = getattr(resp, "data", None)
    fid = str(getattr(getattr(data, "field", None), "field_id", "") or "")
    if not fid:
        return {"ok": False, "error": "建附件字段没返回 field_id"}
    return {"ok": True, "field_id": fid}


def _drop_field(client, app_token: str, table_id: str, field_id: str) -> None:
    from lark_oapi.api.bitable.v1 import DeleteAppTableFieldRequest
    try:
        client.bitable.v1.app_table_field.delete(
            DeleteAppTableFieldRequest.builder().app_token(app_token)
            .table_id(table_id).field_id(field_id).build())
    except Exception as e:
        print("  ! 删临时字段失败：%s" % e)


def _add_record(client, app_token: str, table_id: str,
                values: dict) -> dict:
    from lark_oapi.api.bitable.v1 import (
        AppTableRecord, CreateAppTableRecordRequest)
    body = AppTableRecord.builder().fields(values).build()
    resp = client.bitable.v1.app_table_record.create(
        CreateAppTableRecordRequest.builder().app_token(app_token)
        .table_id(table_id).request_body(body).build())
    if not _ok(resp):
        return {"ok": False, "error": "写临时行失败：" + _err(resp)}
    rec = getattr(getattr(resp, "data", None), "record", None)
    return {"ok": True, "record_id": str(getattr(rec, "record_id", "") or "")}


def _get_record(client, app_token: str, table_id: str,
                record_id: str) -> dict:
    from lark_oapi.api.bitable.v1 import GetAppTableRecordRequest
    resp = client.bitable.v1.app_table_record.get(
        GetAppTableRecordRequest.builder().app_token(app_token)
        .table_id(table_id).record_id(record_id).build())
    if not _ok(resp):
        return {"ok": False, "error": "读回记录失败：" + _err(resp)}
    rec = getattr(getattr(resp, "data", None), "record", None)
    return {"ok": True, "fields": dict(getattr(rec, "fields", None) or {})}


def _drop_record(client, app_token: str, table_id: str, record_id: str) -> None:
    if not record_id:
        return
    from lark_oapi.api.bitable.v1 import DeleteAppTableRecordRequest
    try:
        client.bitable.v1.app_table_record.delete(
            DeleteAppTableRecordRequest.builder().app_token(app_token)
            .table_id(table_id).record_id(record_id).build())
    except Exception as e:
        print("  ! 删临时行失败：%s" % e)


def _tiny_txt() -> Path:
    fd, name = tempfile.mkstemp(prefix="livenotes-spike-", suffix=".txt")
    os.close(fd)
    p = Path(name)
    p.write_text("livenotes spike %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"),
                 encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Base 附件字段真租户探针")
    ap.add_argument("--app-token", default="", help="覆盖 Base app_token")
    ap.add_argument("--table-id", default="", help="覆盖场次表 table_id")
    ap.add_argument("--keep", action="store_true", help="不清理临时字段与行")
    args = ap.parse_args(argv)

    conn = store.connect()
    tmp = _tiny_txt()
    bot = None
    app_token = table_id = ""
    field_id = record_id = ""
    try:
        store.init_db(conn)
        app_token, table_id = _resolve(conn, args)
        if not app_token or not table_id:
            print("找不到 Base：先发布一场让它自动建表，或用 --app-token/--table-id")
            return 2
        try:
            bot = feishu.Bot()
        except Exception as e:
            print("飞书未配置：%s" % e)
            return 2

        print("Base：%s / 表：%s" % (app_token, table_id))
        stamp = time.strftime("%H%M%S")
        made = _add_field(bot.client, app_token, table_id, "spike_附件_" + stamp)
        if not made.get("ok"):
            print("✗ %s" % made.get("error"))
            print("结论：这个租户/版本建不出附件字段 → 走 2b（只写链接列）。")
            return 1
        field_id = made["field_id"]
        print("  临时字段：%s" % field_id)

        # 1) media token
        media = feishu_drive.upload_media(
            bot.client, tmp, app_token, file_name="spike_" + stamp + ".txt")
        if not media.get("ok"):
            print("✗ 素材上传失败：%s" % media.get("error"))
            print("结论：media 上传走不通 → 走 2b。")
            return 1
        mt = str(media.get("file_token") or "")
        print("  media token：%s" % mt)

        spike_key = "spike_%s" % stamp
        row = _add_record(bot.client, app_token, table_id,
                          {"session_id": spike_key,
                           "spike_附件_" + stamp: [{"file_token": mt}]})
        if not row.get("ok"):
            print("✗ %s" % row.get("error"))
            print("结论：附件值写不进行 → 走 2b。")
            return 1
        record_id = row["record_id"]
        got = _get_record(bot.client, app_token, table_id, record_id)
        att = (got.get("fields") or {}).get("spike_附件_" + stamp) or []
        print("  读回：%s" % (att,))
        has_token = bool(att) and isinstance(att, list) and any(
            isinstance(x, dict) and (x.get("file_token") or x.get("tmp_url"))
            for x in att)
        if not has_token:
            print("✗ 读回没有 file_token/tmp_url")
            print("结论：附件写进去但读不回来 → 走 2b。")
            return 1

        # 2) 普通云空间 file token 能不能冒充附件
        folder = str((bot.cfg or {}).get("audio_folder_token") or "")
        fup = (feishu_drive.upload_file(bot.client, tmp, folder,
                                        "spike_" + stamp + ".txt")
               if folder else {"ok": False, "error": "没配云空间文件夹"})
        if fup.get("ok"):
            ft = str(fup.get("file_token") or "")
            from lark_oapi.api.bitable.v1 import (
                AppTableRecord, UpdateAppTableRecordRequest)
            body = AppTableRecord.builder().fields(
                {"spike_附件_" + stamp: [{"file_token": ft}]}).build()
            resp = bot.client.bitable.v1.app_table_record.update(
                UpdateAppTableRecordRequest.builder().app_token(app_token)
                .table_id(table_id).record_id(record_id)
                .request_body(body).build())
            print("  普通 file token 写入：%s"
                  % ("接受" if _ok(resp) else "被拒（" + _err(resp) + "）"))
            feishu_drive._delete_file(bot.client, ft)
        else:
            print("  跳过 file token 对照：%s" % fup.get("error"))

        print("✓ 附件字段可用（2a）：media token 能写进 type 17 并读回。")
        return 0
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
        if args.keep:
            print("（--keep：临时字段 %s / 行 %s 保留）" % (field_id, record_id))
        elif bot is not None:
            if record_id:
                _drop_record(bot.client, app_token, table_id, record_id)
            if field_id:
                _drop_field(bot.client, app_token, table_id, field_id)
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
