# -*- coding: utf-8 -*-
"""飞书云空间归档：一次上传、分片上传、授权、写库。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app import feishu_drive, live, server, store
from helpers import add_session


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Resp:
    def __init__(self, data=None, code=0, msg="ok"):
        self.data = data
        self.code = code
        self.msg = msg

    def success(self) -> bool:
        return self.code == 0


class FakeFile:
    def __init__(self) -> None:
        self.all_calls: list = []
        self.prepare_calls: list = []
        self.parts: list = []
        self.finish_calls: list = []
        self.delete_calls: list = []
        self.block_size = 4

    def upload_all(self, req):
        self.all_calls.append(req.request_body)
        return Resp(Obj(file_token="ft_all", url="https://x.feishu.cn/file/ft_all"))

    def upload_prepare(self, req):
        body = req.request_body
        self.prepare_calls.append(body)
        n = max(1, (int(body.size) + self.block_size - 1) // self.block_size)
        return Resp(Obj(upload_id="up1", block_size=self.block_size, block_num=n))

    def upload_part(self, req):
        self.parts.append(req.request_body)
        return Resp(Obj())

    def upload_finish(self, req):
        self.finish_calls.append(req.request_body)
        return Resp(Obj(file_token="ft_fin", url=""))

    def delete(self, req):
        self.delete_calls.append(req)
        return Resp(Obj())


class FakeMembers:
    def __init__(self) -> None:
        self.calls: list = []

    def create(self, req):
        self.calls.append(req)
        return Resp(Obj())


class FakeClient:
    def __init__(self) -> None:
        self.file = FakeFile()
        self.members = FakeMembers()
        self.drive = Obj(v1=Obj(file=self.file, permission_member=self.members))


class FakeBot:
    def __init__(self, folder="fld_1", owner="ou_owner") -> None:
        self.cfg = {"audio_folder_token": folder, "drive_url_base": ""}
        self.client = FakeClient()
        self.owner_open_id = owner


def _write(tmp_path, name="a.m4a", size=8):
    p = tmp_path / name
    p.write_bytes(b"x" * size)
    return p


def test_upload_all_for_small_file(tmp_path):
    bot = FakeBot()
    p = _write(tmp_path, size=8)
    out = feishu_drive.upload_file(bot.client, p, "fld_1", "标题.m4a")
    assert out == {"ok": True, "file_token": "ft_all",
                   "url": "https://x.feishu.cn/file/ft_all",
                   "size": 8, "chunks": 1}
    body = bot.client.file.all_calls[0]
    assert body.file_name == "标题.m4a"
    assert body.parent_type == "explorer"
    assert body.parent_node == "fld_1"
    assert body.size == 8


def test_chunked_upload_when_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_drive, "MAX_SINGLE_UPLOAD", 4)
    bot = FakeBot()
    p = _write(tmp_path, size=10)
    out = feishu_drive.upload_file(bot.client, p, "fld_1", "big.m4a")
    assert out["ok"] and out["file_token"] == "ft_fin"
    assert out["chunks"] == 3
    assert [b.size for b in bot.client.file.parts] == [4, 4, 2]
    assert [b.seq for b in bot.client.file.parts] == [0, 1, 2]
    assert bot.client.file.finish_calls[0].block_num == 3
    assert not bot.client.file.all_calls


def test_upload_rejects_missing_folder_and_empty_file(tmp_path):
    bot = FakeBot(folder="")
    p = _write(tmp_path)
    assert not feishu_drive.upload_file(bot.client, p, "", "a.m4a")["ok"]
    empty = tmp_path / "empty.m4a"
    empty.write_bytes(b"")
    assert not feishu_drive.upload_file(bot.client, empty, "fld_1", "e.m4a")["ok"]


def test_grant_member_uses_file_type():
    bot = FakeBot()
    out = feishu_drive.grant_member(bot.client, "ft_1", "ou_1")
    assert out == {"ok": True}
    req = bot.client.members.calls[0]
    assert req.token == "ft_1"
    assert req.type == "file"
    assert req.request_body.member_type == "openid"
    assert req.request_body.member_id == "ou_1"
    assert req.request_body.perm == "full_access"


def test_archive_session_writes_columns(db, tmp_path):
    p = _write(tmp_path, name="S1.m4a", size=8)
    add_session(db, "S1", audio_path=str(p), title="周三复盘")
    bot = FakeBot()

    out = feishu_drive.archive_session(db, store.get_session(db, "S1"), bot=bot)

    assert out["ok"] and out["shared"]
    assert out["file_token"] == "ft_all"
    assert out["url"] == "https://x.feishu.cn/file/ft_all"
    row = store.get_session(db, "S1")
    assert row["drive_token"] == "ft_all"
    assert row["drive_url"] == "https://x.feishu.cn/file/ft_all"
    assert row["drive_uploaded_at"]


def test_archive_session_without_url_base_keeps_link_empty(db, tmp_path):
    """接口没给 url、又没配租户域名：drive_url 写 NULL，而不是通用域名。"""
    p = _write(tmp_path, name="S6.m4a", size=8)
    add_session(db, "S6", audio_path=str(p))
    bot = FakeBot()
    bot.client.file.upload_all = lambda req: Resp(Obj(file_token="ft_nourl"))

    out = feishu_drive.archive_session(db, store.get_session(db, "S6"), bot=bot)

    assert out["ok"] and out["url"] == ""
    row = store.get_session(db, "S6")
    assert row["drive_token"] == "ft_nourl"
    assert row["drive_url"] is None


def test_file_url_needs_tenant_base():
    assert feishu_drive.file_url("ft_1") == ""
    assert (feishu_drive.file_url("ft_1", "https://x.feishu.cn/file")
            == "https://x.feishu.cn/file/ft_1")


def test_archive_session_without_folder(db, tmp_path):
    p = _write(tmp_path, name="S2.m4a")
    add_session(db, "S2", audio_path=str(p))
    out = feishu_drive.archive_session(db, store.get_session(db, "S2"),
                                       bot=FakeBot(folder=""))
    assert not out["ok"]
    assert "FEISHU_AUDIO_FOLDER_TOKEN" in out["error"]


def test_archive_session_without_audio(db):
    add_session(db, "S3", audio_path=None)
    out = feishu_drive.archive_session(db, store.get_session(db, "S3"),
                                       bot=FakeBot())
    assert not out["ok"]
    assert "没有可上传的音频" in out["error"]


def test_safe_name():
    assert feishu_drive.safe_name("a/b:c?.m4a") == "a_b_c_.m4a"
    assert feishu_drive.safe_name("   ") == "recording.m4a"


def test_audio_path_prefers_transcoded_m4a(monkeypatch, tmp_path):
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    (tmp_path / "S5.wav").write_bytes(b"w")
    (tmp_path / "S5.m4a").write_bytes(b"m")
    got = feishu_drive.audio_path_for(
        {"id": "S5", "audio_path": str(tmp_path / "S5.wav")})
    assert got is not None and got.name == "S5.m4a"


def test_archive_session_rejects_huge_wav(db, tmp_path, monkeypatch):
    p = tmp_path / "S4.wav"
    p.write_bytes(b"x" * 16)
    monkeypatch.setattr(feishu_drive, "MAX_WAV_UPLOAD", 8)
    add_session(db, "S4", audio_path=str(p))
    out = feishu_drive.archive_session(db, store.get_session(db, "S4"),
                                       bot=FakeBot())
    assert not out["ok"] and "转码" in out["error"]


# ---------------------------------------------------------------- 幂等归档

def _archived_session(db, tmp_path, sid, *, token="ft_old",
                      uploaded="2026-11-25T22:00:00",
                      transcoded="2026-11-25T21:00:00"):
    p = _write(tmp_path, name=sid + ".m4a", size=8)
    add_session(db, sid, audio_path=str(p), title="周三复盘")
    store.set_audio_meta(db, sid, drive_token=token,
                         drive_url="https://x.feishu.cn/file/" + token,
                         drive_uploaded_at=uploaded,
                         audio_transcoded_at=transcoded)
    return store.get_session(db, sid)


def test_ensure_archived_skips_when_up_to_date(db, tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    s = _archived_session(db, tmp_path, "S20")
    bot = FakeBot()

    out = feishu_drive.ensure_archived(db, s, bot=bot)

    assert out["ok"] and out["skipped"]
    assert out["file_token"] == "ft_old"
    assert out["url"] == "https://x.feishu.cn/file/ft_old"
    assert bot.client.file.all_calls == []
    assert bot.client.file.delete_calls == []


def test_ensure_archived_reuploads_after_new_transcode(db, tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    s = _archived_session(db, tmp_path, "S21",
                          uploaded="2026-11-25T21:00:00",
                          transcoded="2026-11-25T22:00:00")
    bot = FakeBot()

    out = feishu_drive.ensure_archived(db, s, bot=bot)

    assert out["ok"] and not out["skipped"] and out["replaced"]
    assert out["file_token"] == "ft_all"
    assert len(bot.client.file.all_calls) == 1
    assert len(bot.client.file.delete_calls) == 1
    assert bot.client.file.delete_calls[0].file_token == "ft_old"
    row = store.get_session(db, "S21")
    assert row["drive_token"] == "ft_all"
    assert row["drive_url"] == "https://x.feishu.cn/file/ft_all"
    assert row["drive_uploaded_at"]


def test_ensure_archived_without_audio_returns_error(db, tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    add_session(db, "S22", audio_path=None)
    out = feishu_drive.ensure_archived(db, store.get_session(db, "S22"),
                                       bot=FakeBot())
    assert not out["ok"] and not out["skipped"]
    assert "没有可上传的音频" in out["error"]


def test_ensure_archived_huge_wav_returns_error(db, tmp_path, monkeypatch):
    p = tmp_path / "S23.wav"
    p.write_bytes(b"x" * 16)
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    monkeypatch.setattr(feishu_drive, "MAX_WAV_UPLOAD", 8)
    add_session(db, "S23", audio_path=str(p))
    out = feishu_drive.ensure_archived(db, store.get_session(db, "S23"),
                                       bot=FakeBot())
    assert not out["ok"] and "转码" in out["error"]


def test_archive_session_and_ensure_archived_write_same_columns(
        db, tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_drive, "AUDIO_DIR", tmp_path)
    p1 = _write(tmp_path, name="S24.m4a", size=8)
    add_session(db, "S24", audio_path=str(p1), title="A")
    p2 = _write(tmp_path, name="S25.m4a", size=8)
    add_session(db, "S25", audio_path=str(p2), title="B")

    feishu_drive.archive_session(db, store.get_session(db, "S24"),
                                 bot=FakeBot())
    feishu_drive.ensure_archived(db, store.get_session(db, "S25"),
                                 bot=FakeBot())

    for sid in ("S24", "S25"):
        row = store.get_session(db, sid)
        assert row["drive_token"] == "ft_all"
        assert row["drive_url"] == "https://x.feishu.cn/file/ft_all"
        assert row["drive_uploaded_at"]


# ---------------------------------------------------------------- HTTP 路由

@pytest.fixture
def http_base(db):
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _post(base: str, path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_cloud_save_route_wires_result(http_base, db, monkeypatch):
    add_session(db, "S9", title="路由测试")
    monkeypatch.setattr(feishu_drive, "archive_session", lambda conn, s: {
        "ok": True, "session_id": s["id"], "url": "https://x/file/ft"})
    status, out = _post(http_base, "/api/cloud/save", {"session_id": "S9"})
    assert status == 200
    assert out["ok"] and out["url"] == "https://x/file/ft"


def test_cloud_save_route_404_and_trash(http_base, db, monkeypatch):
    status, out = _post(http_base, "/api/cloud/save", {"session_id": "nope"})
    assert status == 404 and out["error"] == "场次不存在"

    add_session(db, "S10")
    store.trash_session(db, "S10")
    status, out = _post(http_base, "/api/cloud/save", {"session_id": "S10"})
    assert status == 400 and "回收站" in out["error"]
