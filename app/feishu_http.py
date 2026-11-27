# -*- coding: utf-8 -*-
"""轻量飞书 REST HTTP 客户端（纯标准库，零 lark_oapi 依赖）。

为什么不用 lark_oapi 调 REST：
``import lark_oapi`` 会链式导入整个开放平台上千个模型文件，Windows 下冷启动
动辄十几秒、在沙箱或杀毒扫描下甚至超时，还会多占几十 MB 内存。
真正需要 ``lark_oapi`` 的只有 ``feishu.run()`` 的 WebSocket 长连接；其余
IM / 云文档 / 云空间 / 多维表格调用都是标准的带 ``tenant_access_token``
的 JSON / multipart HTTP 请求。

设计原则：
1. **对外接口形状与原有 ``client.<svc>.v1.<res>.<method>(req)`` 一致**：
   既有测试里的 ``FakeBot`` / ``FakeTables`` / ``FakeFiles`` 无需重写。
2. **纯标准库**：仅用 ``urllib.request`` / ``json`` / ``threading`` / ``uuid``，
   导入耗时 < 2ms，且不要求采集节点或测试环境安装 ``lark-oapi`` / ``requests``。
3. **token 缓存 + 过期自动重试一次**：提前 10 分钟刷新；遇到飞书 token 失效
   错误码（99991663 / 99991661 / 99991668）自动清缓存重取并重放一次请求。
"""
from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

DEFAULT_DOMAIN = "https://open.feishu.cn"
_TOKEN_EXPIRED_CODES = frozenset({99991663, 99991661, 99991668})


class DataObj:
    """轻量属性对象：既可作请求体/请求参数，也可承载响应 ``data``。

    刻意不继承 ``dict``，避免飞书响应里的 ``items`` / ``values`` / ``get``
    等字段名与 ``dict`` 方法冲突。
    """

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self) -> str:
        inner = ", ".join("%s=%r" % (k, v) for k, v in self.__dict__.items())
        return "DataObj(%s)" % inner


def obj(**kwargs: Any) -> DataObj:
    """构造一个轻量请求/数据对象，跳过值为 ``None`` 的可选字段。"""
    return DataObj(**{k: v for k, v in kwargs.items() if v is not None})


class ApiResp:
    """与 ``lark_oapi`` 响应对齐的最小封装：``.success()`` / ``.code`` / ``.msg`` / ``.data``。"""

    def __init__(self, code: int = 0, msg: str = "ok", data: Any = None,
                 status_code: int = 200) -> None:
        self.code = int(code)
        self.msg = str(msg or "")
        self.data = data
        self.status_code = int(status_code)

    def success(self) -> bool:
        return self.code == 0

    def __repr__(self) -> str:
        return "ApiResp(code=%r, msg=%r)" % (self.code, self.msg)


def _wrap_data(val: Any, key: str = "") -> Any:
    """把响应 JSON 转成支持属性访问的 ``DataObj``。

    多维表格记录的 ``fields`` 是用户自定义列名映射，保留为普通 ``dict``，
    方便调用方 ``dict(rec.fields)`` 与 ``isinstance(x, dict)``。
    """
    if key == "fields":
        return val
    if isinstance(val, dict):
        return DataObj(**{k: _wrap_data(v, k) for k, v in val.items()})
    if isinstance(val, list):
        return [_wrap_data(x) for x in val]
    return val


def to_wire(val: Any) -> Any:
    """把 ``DataObj`` / 嵌套结构转成可 JSON 序列化的基础类型（忽略 ``DataObj`` 里的 ``None``）。"""
    if isinstance(val, DataObj):
        return {k: to_wire(v) for k, v in val.__dict__.items() if v is not None}
    if isinstance(val, dict):
        return {k: to_wire(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [to_wire(x) for x in val]
    return val


def _safe_filename(name: str) -> str:
    cleaned = str(name or "file").replace("\r", "").replace("\n", "").replace('"', "_")
    return cleaned or "file"


def encode_multipart(body: Any) -> tuple[bytes, str]:
    """把 ``DataObj`` 或 ``dict`` 编码成 ``multipart/form-data`` 字节流。

    文件字段（具有 ``.read`` 方法或 ``bytes``）直接写入二进制段，其余非 ``None``
    字段转为 UTF-8 文本段。返回 ``(payload_bytes, content_type_header)``。
    """
    fields: dict[str, Any]
    if isinstance(body, DataObj):
        fields = dict(body.__dict__)
    elif isinstance(body, dict):
        fields = dict(body)
    elif hasattr(body, "__dict__"):
        fields = dict(vars(body))
    else:
        fields = {}

    boundary = "----LiveNotesFeishu" + uuid.uuid4().hex
    buf = io.BytesIO()
    fallback_name = _safe_filename(str(fields.get("file_name") or ""))

    file_items: list[tuple[str, Any]] = []
    for k, v in fields.items():
        if v is None:
            continue
        if hasattr(v, "read") or isinstance(v, (bytes, bytearray)):
            file_items.append((k, v))
            continue
        buf.write(("--%s\r\n" % boundary).encode("ascii"))
        buf.write(('Content-Disposition: form-data; name="%s"\r\n\r\n' % k).encode("utf-8"))
        buf.write(str(v).encode("utf-8"))
        buf.write(b"\r\n")

    for k, v in file_items:
        raw = bytes(v) if isinstance(v, (bytes, bytearray)) else v.read()
        fname = fallback_name
        if not fname or fname == "file":
            vname = getattr(v, "name", "")
            if isinstance(vname, (str, bytes)) and vname:
                if isinstance(vname, bytes):
                    vname = vname.decode("utf-8", "replace")
                fname = _safe_filename(os.path.basename(vname))
        buf.write(("--%s\r\n" % boundary).encode("ascii"))
        buf.write((
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n' % (k, fname or "file")
        ).encode("utf-8"))
        buf.write(raw)
        buf.write(b"\r\n")

    buf.write(("--%s--\r\n" % boundary).encode("ascii"))
    return buf.getvalue(), "multipart/form-data; boundary=" + boundary


def _build_url(domain: str, uri: str, paths: dict[str, Any] | None = None,
               queries: dict[str, Any] | None = None) -> str:
    out = uri
    for k, v in (paths or {}).items():
        encoded = urllib.parse.quote(str("" if v is None else v), safe="")
        out = out.replace(":" + k, encoded)
    if not (out.startswith("http://") or out.startswith("https://")):
        out = domain.rstrip("/") + out
    if queries:
        pairs: list[tuple[str, str]] = []
        for k, v in queries.items():
            if v is None:
                continue
            if isinstance(v, bool):
                s = "true" if v else "false"
            else:
                s = str(v)
            pairs.append((k, s))
        if pairs:
            out += ("&" if "?" in out else "?") + urllib.parse.urlencode(pairs)
    return out


def _req_paths(req: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: getattr(req, k, "") for k in keys}


def _req_queries(req: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        v = getattr(req, k, None)
        if v is not None:
            out[k] = v
    return out


class FeishuClient:
    """轻量飞书 OpenAPI REST 客户端。"""

    def __init__(self, app_id: str, app_secret: str, *,
                 timeout: float = 60.0, domain: str = DEFAULT_DOMAIN,
                 opener: Callable[..., Any] | None = None) -> None:
        self.app_id = str(app_id or "")
        self.app_secret = str(app_secret or "")
        self.timeout = float(timeout)
        self.domain = str(domain or DEFAULT_DOMAIN).rstrip("/")
        self._opener = opener or urllib.request.urlopen
        self._token_lock = threading.Lock()
        self._token = ""
        self._token_expire = 0.0

        # 挂载与 lark_oapi 同名的资源命名空间
        self.im = DataObj(v1=DataObj(
            message=_ImMessage(self),
            file=_ImFile(self),
        ))
        self.docx = DataObj(v1=DataObj(
            document=_DocxDocument(self),
            document_block_descendant=_DocxBlockDescendant(self),
            document_block_children=_DocxBlockChildren(self),
            document_block=_DocxBlock(self),
        ))
        self.drive = DataObj(v1=DataObj(
            permission_member=_DrivePermissionMember(self),
            file=_DriveFile(self),
            media=_DriveMedia(self),
        ))
        self.bitable = DataObj(v1=DataObj(
            app=_BitableApp(self),
            app_table=_BitableAppTable(self),
            app_table_field=_BitableAppTableField(self),
            app_table_record=_BitableAppTableRecord(self),
        ))

    def _raw_http(self, method: str, url: str, headers: dict[str, str],
                  body: bytes | None) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener(req, timeout=self.timeout) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                return status, resp.read()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return int(getattr(e, "code", 500) or 500), raw

    def _parse_json_resp(self, status: int, raw: bytes) -> ApiResp:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            if 200 <= status < 300:
                return ApiResp(code=0, msg="ok", data=None, status_code=status)
            return ApiResp(code=status, msg="HTTP %d" % status, status_code=status)
        try:
            payload = json.loads(text)
        except ValueError:
            return ApiResp(code=status if status >= 400 else -1,
                           msg="非 JSON 响应 (HTTP %d): %s" % (status, text[:200]),
                           status_code=status)
        if not isinstance(payload, dict):
            return ApiResp(code=-1, msg="响应格式异常", status_code=status)
        code = int(payload.get("code", 0 if 200 <= status < 300 else status))
        msg = str(payload.get("msg") or "")
        data = _wrap_data(payload.get("data")) if "data" in payload else None
        return ApiResp(code=code, msg=msg, data=data, status_code=status)

    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        """获取并缓存自建应用 ``tenant_access_token``（提前 10 分钟刷新）。"""
        now = time.time()
        if not force_refresh and self._token and now < self._token_expire:
            return self._token
        with self._token_lock:
            now = time.time()
            if not force_refresh and self._token and now < self._token_expire:
                return self._token
            url = self.domain + "/open-apis/auth/v3/tenant_access_token/internal"
            body = json.dumps(
                {"app_id": self.app_id, "app_secret": self.app_secret},
                ensure_ascii=False,
            ).encode("utf-8")
            status, raw = self._raw_http(
                "POST", url, {"Content-Type": "application/json; charset=utf-8"}, body
            )
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                raise RuntimeError("获取 tenant_access_token 失败：HTTP %d" % status)
            code = int(payload.get("code", -1))
            if code != 0 or not payload.get("tenant_access_token"):
                raise RuntimeError(
                    "获取 tenant_access_token 失败：code=%s %s"
                    % (code, payload.get("msg") or "")
                )
            token = str(payload["tenant_access_token"])
            expire = int(payload.get("expire") or 7200)
            margin = 600 if expire > 600 else max(30, expire // 2)
            self._token = token
            self._token_expire = time.time() + max(1, expire - margin)
            return token

    def _invalidate_token(self, failed_token: str) -> None:
        with self._token_lock:
            if self._token == failed_token:
                self._token = ""
                self._token_expire = 0.0

    def request(self, method: str, uri: str, *,
                paths: dict[str, Any] | None = None,
                queries: dict[str, Any] | None = None,
                json_body: Any = None,
                multipart_body: Any = None) -> ApiResp:
        url = _build_url(self.domain, uri, paths=paths, queries=queries)
        base_headers: dict[str, str] = {}
        payload: bytes | None = None
        if multipart_body is not None:
            payload, ctype = encode_multipart(multipart_body)
            base_headers["Content-Type"] = ctype
        elif json_body is not None:
            payload = json.dumps(to_wire(json_body), ensure_ascii=False).encode("utf-8")
            base_headers["Content-Type"] = "application/json; charset=utf-8"

        for attempt in range(2):
            token = self.get_tenant_access_token(force_refresh=(attempt > 0))
            headers = dict(base_headers)
            headers["Authorization"] = "Bearer " + token
            status, raw = self._raw_http(method, url, headers, payload)
            resp = self._parse_json_resp(status, raw)
            if resp.code in _TOKEN_EXPIRED_CODES and attempt == 0:
                self._invalidate_token(token)
                continue
            return resp
        return resp  # pragma: no cover


# ---------------------------------------------------------------- 资源端点映射

class _Res:
    def __init__(self, client: FeishuClient) -> None:
        self._c = client


class _ImMessage(_Res):
    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/im/v1/messages",
            queries=_req_queries(req, ("receive_id_type",)),
            json_body=getattr(req, "request_body", None),
        )


class _ImFile(_Res):
    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/im/v1/files",
            multipart_body=getattr(req, "request_body", None),
        )


class _DocxDocument(_Res):
    def convert(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/docx/v1/documents/blocks/convert",
            queries=_req_queries(req, ("user_id_type",)),
            json_body=getattr(req, "request_body", None),
        )

    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/docx/v1/documents",
            json_body=getattr(req, "request_body", None),
        )


class _DocxBlockDescendant(_Res):
    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/docx/v1/documents/:document_id/blocks/:block_id/descendant",
            paths=_req_paths(req, ("document_id", "block_id")),
            json_body=getattr(req, "request_body", None),
        )


class _DocxBlockChildren(_Res):
    def get(self, req: Any) -> ApiResp:
        return self._c.request(
            "GET", "/open-apis/docx/v1/documents/:document_id/blocks/:block_id/children",
            paths=_req_paths(req, ("document_id", "block_id")),
            queries=_req_queries(req, ("page_token", "page_size")),
        )

    # 兼容 reading._raw_rewrite 旧调用
    list = get

    def batch_delete(self, req: Any) -> ApiResp:
        return self._c.request(
            "DELETE",
            "/open-apis/docx/v1/documents/:document_id/blocks/:block_id/children/batch_delete",
            paths=_req_paths(req, ("document_id", "block_id")),
            json_body=getattr(req, "request_body", None),
        )


class _DocxBlock(_Res):
    def patch(self, req: Any) -> ApiResp:
        return self._c.request(
            "PATCH", "/open-apis/docx/v1/documents/:document_id/blocks/:block_id",
            paths=_req_paths(req, ("document_id", "block_id")),
            queries=_req_queries(req, ("client_token", "user_id_type")),
            json_body=getattr(req, "request_body", None),
        )


class _DrivePermissionMember(_Res):
    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/permissions/:token/members",
            paths=_req_paths(req, ("token",)),
            queries=_req_queries(req, ("type", "need_notification")),
            json_body=getattr(req, "request_body", None),
        )


class _DriveFile(_Res):
    def create_folder(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/create_folder",
            json_body=getattr(req, "request_body", None),
        )

    def list(self, req: Any) -> ApiResp:
        return self._c.request(
            "GET", "/open-apis/drive/v1/files",
            queries=_req_queries(req, ("page_size", "page_token", "folder_token",
                                       "order_by", "direction", "option", "user_id_type")),
        )

    def delete(self, req: Any) -> ApiResp:
        return self._c.request(
            "DELETE", "/open-apis/drive/v1/files/:file_token",
            paths=_req_paths(req, ("file_token",)),
            queries=_req_queries(req, ("type",)),
        )

    def move(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/:file_token/move",
            paths=_req_paths(req, ("file_token",)),
            json_body=getattr(req, "request_body", None),
        )

    def upload_all(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/upload_all",
            multipart_body=getattr(req, "request_body", None),
        )

    def upload_prepare(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/upload_prepare",
            json_body=getattr(req, "request_body", None),
        )

    def upload_part(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/upload_part",
            multipart_body=getattr(req, "request_body", None),
        )

    def upload_finish(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/files/upload_finish",
            json_body=getattr(req, "request_body", None),
        )


class _DriveMedia(_Res):
    def upload_all(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/medias/upload_all",
            multipart_body=getattr(req, "request_body", None),
        )

    def upload_prepare(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/medias/upload_prepare",
            json_body=getattr(req, "request_body", None),
        )

    def upload_part(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/medias/upload_part",
            multipart_body=getattr(req, "request_body", None),
        )

    def upload_finish(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/drive/v1/medias/upload_finish",
            json_body=getattr(req, "request_body", None),
        )


class _BitableApp(_Res):
    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/bitable/v1/apps",
            json_body=getattr(req, "request_body", None),
        )


class _BitableAppTable(_Res):
    def list(self, req: Any) -> ApiResp:
        return self._c.request(
            "GET", "/open-apis/bitable/v1/apps/:app_token/tables",
            paths=_req_paths(req, ("app_token",)),
            queries=_req_queries(req, ("page_size", "page_token")),
        )

    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/bitable/v1/apps/:app_token/tables",
            paths=_req_paths(req, ("app_token",)),
            json_body=getattr(req, "request_body", None),
        )


class _BitableAppTableField(_Res):
    def list(self, req: Any) -> ApiResp:
        return self._c.request(
            "GET", "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields",
            paths=_req_paths(req, ("app_token", "table_id")),
            queries=_req_queries(req, ("view_id", "page_size", "page_token")),
        )

    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields",
            paths=_req_paths(req, ("app_token", "table_id")),
            queries=_req_queries(req, ("client_token",)),
            json_body=getattr(req, "request_body", None),
        )

    def delete(self, req: Any) -> ApiResp:
        return self._c.request(
            "DELETE",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/fields/:field_id",
            paths=_req_paths(req, ("app_token", "table_id", "field_id")),
        )


class _BitableAppTableRecord(_Res):
    def search(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/search",
            paths=_req_paths(req, ("app_token", "table_id")),
            queries=_req_queries(req, ("user_id_type", "page_token", "page_size")),
            json_body=getattr(req, "request_body", None),
        )

    def create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST", "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records",
            paths=_req_paths(req, ("app_token", "table_id")),
            queries=_req_queries(req, ("user_id_type", "client_token")),
            json_body=getattr(req, "request_body", None),
        )

    def update(self, req: Any) -> ApiResp:
        return self._c.request(
            "PUT",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id",
            paths=_req_paths(req, ("app_token", "table_id", "record_id")),
            queries=_req_queries(req, ("user_id_type", "client_token")),
            json_body=getattr(req, "request_body", None),
        )

    def get(self, req: Any) -> ApiResp:
        return self._c.request(
            "GET",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id",
            paths=_req_paths(req, ("app_token", "table_id", "record_id")),
            queries=_req_queries(req, ("user_id_type", "with_shared_url")),
        )

    def delete(self, req: Any) -> ApiResp:
        return self._c.request(
            "DELETE",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/:record_id",
            paths=_req_paths(req, ("app_token", "table_id", "record_id")),
        )

    def batch_create(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_create",
            paths=_req_paths(req, ("app_token", "table_id")),
            json_body=getattr(req, "request_body", None),
        )

    def batch_delete(self, req: Any) -> ApiResp:
        return self._c.request(
            "POST",
            "/open-apis/bitable/v1/apps/:app_token/tables/:table_id/records/batch_delete",
            paths=_req_paths(req, ("app_token", "table_id")),
            json_body=getattr(req, "request_body", None),
        )
