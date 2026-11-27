# -*- coding: utf-8 -*-
"""轻量飞书 REST HTTP 客户端测试：校验所有端点 URL/方法/参数/multipart/token 刷新。"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse

import pytest

from app import feishu, feishu_http


class _FakeRawResp:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Recorder:
    """拦截 FeishuClient._opener，记录每次发出的 HTTP 请求并按队列/规则回包。"""

    def __init__(self, handler=None) -> None:
        self.calls: list[dict] = []
        self._handler = handler
        self.token_calls = 0

    def __call__(self, req, timeout=60):
        url = req.full_url
        method = req.get_method()
        headers = {k.lower(): v for k, v in req.header_items()}
        body = req.data
        parsed = urllib.parse.urlsplit(url)
        rec = {
            "method": method,
            "url": url,
            "path": parsed.path,
            "query": urllib.parse.parse_qs(parsed.query, keep_blank_values=True),
            "headers": headers,
            "body": body,
            "timeout": timeout,
        }
        self.calls.append(rec)

        if parsed.path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.token_calls += 1
            tok = "t_tok_%d" % self.token_calls
            return _FakeRawResp(json.dumps({
                "code": 0, "msg": "ok",
                "tenant_access_token": tok, "expire": 7200,
            }).encode("utf-8"))

        if self._handler is not None:
            res = self._handler(rec)
            if isinstance(res, tuple):
                status, payload = res
                raw = (json.dumps(payload).encode("utf-8")
                       if isinstance(payload, (dict, list)) else payload)
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url, status, "err", {}, io.BytesIO(raw))
                return _FakeRawResp(raw, status=status)
            return _FakeRawResp(json.dumps(res).encode("utf-8"))

        return _FakeRawResp(json.dumps({"code": 0, "msg": "ok", "data": {}}).encode("utf-8"))


def test_bot_and_http_client_do_not_import_lark_oapi():
    """实例化 feishu.Bot 与走 REST 客户端绝不触发 import lark_oapi。"""
    was_loaded = "lark_oapi" in sys.modules
    bot = feishu.Bot({"app_id": "cli_test", "app_secret": "sec_test"})
    assert isinstance(bot.client, feishu_http.FeishuClient)
    if not was_loaded:
        assert "lark_oapi" not in sys.modules


def test_token_cached_and_auto_refreshed_on_expired_code():
    hits = {"api": 0}

    def handle(rec):
        hits["api"] += 1
        if hits["api"] == 1:
            return (400, {"code": 99991663, "msg": "Invalid access token"})
        return {"code": 0, "msg": "ok", "data": {"items": [{"table_id": "tbl_1"}]}}

    recorder = _Recorder(handle)
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)
    r1 = c.bitable.v1.app_table.list(feishu_http.obj(app_token="app_1", page_size=100))
    assert r1.success()
    assert r1.data.items[0].table_id == "tbl_1"
    assert recorder.token_calls == 2

    # 第二次调用复用缓存的 token，不再请求 auth 接口
    r2 = c.bitable.v1.app_table.list(feishu_http.obj(app_token="app_1", page_size=100))
    assert r2.success()
    assert recorder.token_calls == 2
    api_calls = [x for x in recorder.calls if "/auth/" not in x["path"]]
    assert api_calls[0]["headers"]["authorization"] == "Bearer t_tok_1"
    assert api_calls[1]["headers"]["authorization"] == "Bearer t_tok_2"
    assert api_calls[2]["headers"]["authorization"] == "Bearer t_tok_2"


def test_http_error_json_and_non_json_parsed_cleanly():
    seq = [
        (403, {"code": 1254302, "msg": "RolePermNotAllow"}),
        (502, b"<html>Bad Gateway</html>"),
    ]
    recorder = _Recorder(lambda rec: seq.pop(0))
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)

    r1 = c.bitable.v1.app.create(feishu_http.obj(request_body=feishu_http.obj(name="T")))
    assert not r1.success()
    assert r1.code == 1254302 and "RolePermNotAllow" in r1.msg

    r2 = c.bitable.v1.app.create(feishu_http.obj(request_body=feishu_http.obj(name="T")))
    assert not r2.success()
    assert r2.code == 502 and "Bad Gateway" in r2.msg


def test_docx_table_merge_info_stripped_before_json_wire():
    """回归：convert 返回的 table.property.merge_info 置 None 后，序列化必须省略该键。"""
    def handle(rec):
        if rec["path"].endswith("/blocks/convert"):
            return {
                "code": 0, "msg": "ok",
                "data": {
                    "first_level_block_ids": ["b1"],
                    "blocks": [{
                        "block_id": "b1", "block_type": 31,
                        "table": {"property": {"row_size": 2, "column_width": [100],
                                               "merge_info": [{"row_span": 1}]}},
                    }],
                },
            }
        return {"code": 0, "msg": "ok", "data": {}}

    recorder = _Recorder(handle)
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)
    conv = c.docx.v1.document.convert(
        feishu_http.obj(request_body=feishu_http.obj(
            content_type="markdown", content="|a|\n|-|\n|1|")))
    assert conv.success()
    fixed = feishu.Bot._strip_table_merge_info(conv.data.blocks)
    assert fixed == 1

    c.docx.v1.document_block_descendant.create(feishu_http.obj(
        document_id="doc_1", block_id="doc_1",
        request_body=feishu_http.obj(
            children_id=conv.data.first_level_block_ids,
            index=0, descendants=conv.data.blocks),
    ))
    desc_call = [x for x in recorder.calls if x["path"].endswith("/descendant")][0]
    sent = json.loads(desc_call["body"].decode("utf-8"))
    prop = sent["descendants"][0]["table"]["property"]
    assert "merge_info" not in prop
    assert prop["column_width"] == [100]


def test_bitable_fields_preserved_as_plain_dict_and_items_not_shadowed():
    """record.fields 保持普通 dict；data.items 不与 dict.items 冲突。"""
    def handle(rec):
        if rec["path"].endswith("/records/rec_1"):
            return {
                "code": 0, "msg": "ok",
                "data": {"record": {
                    "record_id": "rec_1",
                    "fields": {"标题": "直播一", "录音": [{"file_token": "mt_1"}]},
                }},
            }
        return {
            "code": 0, "msg": "ok",
            "data": {"items": [{"record_id": "rec_1"}], "has_more": False, "page_token": ""},
        }

    recorder = _Recorder(handle)
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)
    got = c.bitable.v1.app_table_record.get(
        feishu_http.obj(app_token="app_1", table_id="tbl_1", record_id="rec_1"))
    assert got.success()
    assert isinstance(got.data.record.fields, dict)
    assert dict(got.data.record.fields)["标题"] == "直播一"
    assert isinstance(got.data.record.fields["录音"][0], dict)

    searched = c.bitable.v1.app_table_record.search(feishu_http.obj(
        app_token="app_1", table_id="tbl_1", page_size=50,
        request_body=feishu_http.obj(automatic_fields=False),
    ))
    assert searched.success()
    assert len(searched.data.items) == 1
    assert searched.data.items[0].record_id == "rec_1"


def test_multipart_uploads_and_retry_without_stream_exhaustion():
    """multipart 编码正确包含字段名/文件名/二进制内容，token 过期重试时流不丢失。"""
    hits = {"n": 0}

    def handle(rec):
        hits["n"] += 1
        if hits["n"] == 1:
            return (401, {"code": 99991663, "msg": "token expired"})
        return {"code": 0, "msg": "ok", "data": {"file_token": "ft_up"}}

    recorder = _Recorder(handle)
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)
    resp = c.drive.v1.media.upload_all(feishu_http.obj(
        request_body=feishu_http.obj(
            file_name="clip.m4a", parent_type="bitable_file",
            parent_node="app_1", size=5, file=io.BytesIO(b"AUDIO"),
        )
    ))
    assert resp.success() and resp.data.file_token == "ft_up"
    up_calls = [x for x in recorder.calls if x["path"] == "/open-apis/drive/v1/medias/upload_all"]
    assert len(up_calls) == 2
    assert up_calls[0]["body"] == up_calls[1]["body"]
    raw = up_calls[1]["body"]
    assert b'name="file_name"\r\n\r\nclip.m4a' in raw
    assert b'name="parent_type"\r\n\r\nbitable_file' in raw
    assert b'name="file"; filename="clip.m4a"' in raw
    assert b"\r\nAUDIO\r\n" in raw


def test_all_rest_endpoints_wire_specs():
    """逐个核对 IM / Docx / Drive / Bitable 端点的 HTTP 方法、路径、查询参数。"""
    recorder = _Recorder()
    c = feishu_http.FeishuClient("cli_a", "sec_b", opener=recorder)

    # IM
    c.im.v1.message.create(feishu_http.obj(
        receive_id_type="chat_id",
        request_body=feishu_http.obj(receive_id="oc_1", msg_type="text", content="{}")))
    c.im.v1.file.create(feishu_http.obj(
        request_body=feishu_http.obj(file_type="opus", file_name="a.opus",
                                     duration=1000, file=io.BytesIO(b"x"))))
    # Docx
    c.docx.v1.document.create(feishu_http.obj(
        request_body=feishu_http.obj(title="T", folder_token="fld_1")))
    c.docx.v1.document_block_children.get(feishu_http.obj(
        document_id="d1", block_id="d1", page_size=500, page_token="p2"))
    c.docx.v1.document_block_children.batch_delete(feishu_http.obj(
        document_id="d1", block_id="d1",
        request_body=feishu_http.obj(start_index=0, end_index=3)))
    c.docx.v1.document_block.patch(feishu_http.obj(
        document_id="d1", block_id="d1",
        request_body=feishu_http.obj(update_text_elements=feishu_http.obj(elements=[]))))
    # Drive
    c.drive.v1.permission_member.create(feishu_http.obj(
        token="tk_1", type="folder", need_notification=False,
        request_body=feishu_http.obj(member_type="openchat", member_id="oc_1", perm="view")))
    c.drive.v1.file.create_folder(feishu_http.obj(
        request_body=feishu_http.obj(name="dir", folder_token="fld_0")))
    c.drive.v1.file.list(feishu_http.obj(folder_token="fld_0", page_size=200))
    c.drive.v1.file.delete(feishu_http.obj(file_token="ft_1", type="file"))
    c.drive.v1.file.move(feishu_http.obj(
        file_token="ft_1", request_body=feishu_http.obj(type="file", folder_token="fld_2")))
    c.drive.v1.file.upload_prepare(feishu_http.obj(
        request_body=feishu_http.obj(file_name="a.m4a", parent_type="explorer",
                                     parent_node="fld_0", size=100)))
    c.drive.v1.file.upload_part(feishu_http.obj(
        request_body=feishu_http.obj(upload_id="u1", seq=0, size=2, file=io.BytesIO(b"ab"))))
    c.drive.v1.file.upload_finish(feishu_http.obj(
        request_body=feishu_http.obj(upload_id="u1", block_num=1)))
    # Bitable
    c.bitable.v1.app_table.create(feishu_http.obj(
        app_token="app_1", request_body=feishu_http.obj(table=feishu_http.obj(name="场次"))))
    c.bitable.v1.app_table_field.list(feishu_http.obj(
        app_token="app_1", table_id="tbl_1", page_size=100))
    c.bitable.v1.app_table_field.create(feishu_http.obj(
        app_token="app_1", table_id="tbl_1",
        request_body=feishu_http.obj(field_name="标题", type=1)))
    c.bitable.v1.app_table_field.delete(feishu_http.obj(
        app_token="app_1", table_id="tbl_1", field_id="fld_id"))
    c.bitable.v1.app_table_record.create(feishu_http.obj(
        app_token="app_1", table_id="tbl_1",
        request_body=feishu_http.obj(fields={"标题": "A"})))
    c.bitable.v1.app_table_record.update(feishu_http.obj(
        app_token="app_1", table_id="tbl_1", record_id="rec_1",
        request_body=feishu_http.obj(fields={"标题": "B"})))
    c.bitable.v1.app_table_record.delete(feishu_http.obj(
        app_token="app_1", table_id="tbl_1", record_id="rec_1"))
    c.bitable.v1.app_table_record.batch_create(feishu_http.obj(
        app_token="app_1", table_id="tbl_1",
        request_body=feishu_http.obj(records=[feishu_http.obj(fields={"观点": "X"})])))
    c.bitable.v1.app_table_record.batch_delete(feishu_http.obj(
        app_token="app_1", table_id="tbl_1",
        request_body=feishu_http.obj(records=["rec_1"])))

    api_calls = [(x["method"], x["path"], x["query"])
                 for x in recorder.calls if "/auth/" not in x["path"]]
    expected = [
        ("POST", "/open-apis/im/v1/messages", {"receive_id_type": ["chat_id"]}),
        ("POST", "/open-apis/im/v1/files", {}),
        ("POST", "/open-apis/docx/v1/documents", {}),
        ("GET", "/open-apis/docx/v1/documents/d1/blocks/d1/children",
         {"page_size": ["500"], "page_token": ["p2"]}),
        ("DELETE", "/open-apis/docx/v1/documents/d1/blocks/d1/children/batch_delete", {}),
        ("PATCH", "/open-apis/docx/v1/documents/d1/blocks/d1", {}),
        ("POST", "/open-apis/drive/v1/permissions/tk_1/members",
         {"type": ["folder"], "need_notification": ["false"]}),
        ("POST", "/open-apis/drive/v1/files/create_folder", {}),
        ("GET", "/open-apis/drive/v1/files", {"folder_token": ["fld_0"], "page_size": ["200"]}),
        ("DELETE", "/open-apis/drive/v1/files/ft_1", {"type": ["file"]}),
        ("POST", "/open-apis/drive/v1/files/ft_1/move", {}),
        ("POST", "/open-apis/drive/v1/files/upload_prepare", {}),
        ("POST", "/open-apis/drive/v1/files/upload_part", {}),
        ("POST", "/open-apis/drive/v1/files/upload_finish", {}),
        ("POST", "/open-apis/bitable/v1/apps/app_1/tables", {}),
        ("GET", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/fields", {"page_size": ["100"]}),
        ("POST", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/fields", {}),
        ("DELETE", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/fields/fld_id", {}),
        ("POST", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records", {}),
        ("PUT", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records/rec_1", {}),
        ("DELETE", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records/rec_1", {}),
        ("POST", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records/batch_create", {}),
        ("POST", "/open-apis/bitable/v1/apps/app_1/tables/tbl_1/records/batch_delete", {}),
    ]
    assert api_calls == expected
