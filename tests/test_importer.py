# -*- coding: utf-8 -*-
"""云端导入包：分享包 zip / SRT / 逐字稿 txt 进本地库。

覆盖真实生产链路的形状：
  * 本地分享包（``share.build_package`` 产物）原样导回另一场；
  * 云空间归档的逐字稿 txt（``export.to_archive_txt``）单独导入；
  * 重复导入幂等、显式 replace 覆盖；
  * 导入场次没有声纹：补名只改显示、不能重聚类（错误文案要说清楚）；
  * HTTP 原始文件体上传 + JSON 小文本上传。

不联网、不截图，按项目口径只做单元/轻量集成。
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import wave
import zipfile
from pathlib import Path

import pytest
from helpers import add_rows, add_session, utt_row

from app import (analysis, audio, identity, importer, live, naming, notes,
                 recluster, server, share, store)


# ---------------------------------------------------------------- 造数据

def _source(db, sid="S_CLOUD", title="云端语音厅"):
    add_session(db, sid, title=title, duration_s=600,
                started_at="2026-11-27T21:30:00", source_mode="direct",
                capture_only=1)
    rows = [
        utt_row(0, "欢迎来到直播间，今晚聊账号风控", speaker="SPK_00", label="主播"),
        utt_row(1, "我先说结论：新号别急着投流", speaker="SPK_01", label="阿哲"),
        utt_row(2, "那预算怎么定？", speaker="SPK_02", label="小北"),
        utt_row(3, "先跑七天自然流量再决定", speaker="SPK_01", label="阿哲"),
    ]
    add_rows(db, sid, rows)
    for spk, label in (("SPK_00", "主播"), ("SPK_01", "阿哲"), ("SPK_02", "小北")):
        store.upsert_speaker(db, sid, spk, label)
    return sid


def _package(db, tmp_path, monkeypatch, sid="S_CLOUD", audio_bytes=False):
    monkeypatch.setattr(share, "SHARE_DIR", tmp_path / "share")
    if audio_bytes:
        wav = tmp_path / "in.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000 * 3)
        store.set_audio_meta(db, sid, audio_path=str(wav), audio_format="wav")
    info = share.build_package(db, sid)
    return share.zip_package(info["dir"])


def _wav(path: Path, seconds: float = 2.0) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(16000 * seconds))
    return path


# ---------------------------------------------------------------- 解析

def test_srt_prefix_only_when_it_looks_like_speakers():
    rows, _skipped = importer.parse_srt(
        "1\n00:00:00,000 --> 00:00:02,000\n主播：欢迎来到直播间\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n阿哲：预算先按日算\n\n"
        "3\n00:00:04,000 --> 00:00:06,000\n小北：那投流怎么定\n")
    # 每一行都有前缀、前缀又少：这正是逐字稿的说话人列。
    labelled, speakers = importer._assign_speakers([r["text"] for r in rows])
    assert [s for _i, s in speakers] == ["主播", "阿哲", "小北"]
    assert labelled[1]["text"] == "预算先按日算"

    # 只有零星一行带冒号：不能把「结论」当成一个人，冒号留在正文里。
    labelled, speakers = importer._assign_speakers(
        ["今天聊账号风控", "结论：先跑七天自然流量", "大家有问题可以上麦"])
    assert [s for _i, s in speakers] == ["说话人"]
    assert labelled[1]["text"] == "结论：先跑七天自然流量"

    labelled, speakers = importer._assign_speakers(
        ["主播：第一句", "阿哲：第二句", "主播：第三句"])
    assert [s for _i, s in speakers] == ["主播", "阿哲"]
    assert labelled[1]["label"] == "阿哲" and labelled[1]["text"] == "第二句"


def test_archive_txt_header_and_rows():
    text = ("# 2026-11-27 21:30 ·「语音厅复盘」\n"
            "场次：LIVE_20261127_213000\n"
            "时长：1 小时 5 分 · 上麦 3 人 · 3 句\n"
            "导出：2026-11-27 22:40\n\n"
            "[00:00:03] 主播：欢迎来到直播间\n"
            "[00:01:10] 阿哲：新号别急着投流\n"
            "[00:02:00] 小北：预算怎么定？\n")
    parsed = importer.parse_archive_txt(text)
    assert parsed["meta"]["title"] == "语音厅复盘"
    assert parsed["meta"]["src_sid"] == "LIVE_20261127_213000"
    assert parsed["meta"]["started_at"] == "2026-11-27T21:30:00"
    assert parsed["meta"]["duration_s"] == 3600 + 5 * 60
    assert len(parsed["rows"]) == 3
    assert parsed["rows"][1]["t_start"] == 70.0
    assert parsed["rows"][1]["text"] == "阿哲：新号别急着投流"


def test_parse_rejects_unknown_file(tmp_path):
    bad = tmp_path / "a.wav"
    bad.write_bytes(b"RIFF....")
    with pytest.raises(importer.ImportProblem):
        importer.parse_any(bad)
    empty = tmp_path / "b.txt"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(importer.ImportProblem):
        importer.parse_any(empty)


def test_package_without_srt_fails(tmp_path):
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("meta.json", "{}")
    with pytest.raises(importer.ImportProblem) as err:
        importer.parse_any(z)
    assert "transcript.srt" in str(err.value)


# ---------------------------------------------------------------- 入库

def test_import_package_roundtrip(db, tmp_path, monkeypatch):
    sid = _source(db)
    zpath = _package(db, tmp_path, monkeypatch)
    out = importer.import_file(db, zpath, name=zpath.name)
    assert out["ok"] and not out["existed"]
    new_sid = out["session_id"]
    assert new_sid == "IMP_" + sid
    sess = store.get_session(db, new_sid)
    assert sess["source_mode"] == "import"
    assert sess["capture_only"] == 0        # 不能再被离线补转写捡走
    assert sess["title"] == "云端语音厅"
    assert sess["started_at"] == "2026-11-27T21:30:00"
    rows = store.session_transcript(db, new_sid)
    assert [r["text"] for r in rows][:2] == [
        "欢迎来到直播间，今晚聊账号风控", "我先说结论：新号别急着投流"]
    roster = identity.roster(db, new_sid)
    who = {r["label"] for r in roster["entries"]}
    assert who == {"主播", "阿哲", "小北"}
    warnings = store.get_meta(db, new_sid)["session"]["warnings"]
    assert any("没有保存声纹" in w for w in warnings)
    assert out["has_audio"] is False
    assert "没有声纹" in out["naming_hint"]


def test_import_is_idempotent_and_replace_wipes_derived(db, tmp_path, monkeypatch):
    sid = _source(db)
    zpath = _package(db, tmp_path, monkeypatch)
    first = importer.import_file(db, zpath, name=zpath.name)
    new_sid = first["session_id"]
    store.insert_mark(db, new_sid, 12.0, "人工打点", "bookmark")
    again = importer.import_file(db, zpath, name=zpath.name)
    assert again["existed"] is True and again["n_utterances"] == 4
    assert len(store.list_marks(db, new_sid)) == 1
    replaced = importer.import_file(db, zpath, name=zpath.name, replace=True)
    assert replaced["existed"] is False and replaced["replaced"] is True
    assert store.list_marks(db, new_sid) == []
    assert len(store.session_transcript(db, new_sid)) == 4


def test_import_txt_and_attach_audio(db, tmp_path, monkeypatch):
    sid = _source(db)
    text = __import__("app.export", fromlist=["export"]).to_archive_txt(db, sid)
    txt = tmp_path / "cloud.txt"
    txt.write_text(text, encoding="utf-8")
    out = importer.import_file(db, txt, name="cloud.txt")
    assert out["ok"] and out["has_audio"] is False
    new_sid = out["session_id"]
    assert new_sid == "IMP_" + sid
    # 文字已经在库里：再导一次带录音的同一来源，只补录音，不重写文字。
    wav = _wav(tmp_path / "audio.wav", seconds=2.0)
    again = importer.import_file(db, txt, name="cloud.txt", audio_file=wav)
    assert again["existed"] is True and again["attached_audio"] is True
    sess = store.get_session(db, new_sid)
    assert sess["audio_path"].endswith(".wav")
    assert Path(sess["audio_path"]).is_file()
    assert len(store.session_transcript(db, new_sid)) == 4


def test_import_package_audio_moves_into_audio_dir(db, tmp_path, monkeypatch):
    sid = _source(db)
    zpath = _package(db, tmp_path, monkeypatch, audio_bytes=True)
    out = importer.import_file(db, zpath, name=zpath.name)
    sess = store.get_session(db, "IMP_" + sid)
    assert out["has_audio"] is True
    path = Path(sess["audio_path"])
    assert path.is_file() and path.stem == "IMP_" + sid


def test_imported_session_naming_is_honest(db, tmp_path, monkeypatch):
    sid = _source(db)
    zpath = _package(db, tmp_path, monkeypatch)
    new_sid = importer.import_file(db, zpath, name=zpath.name)["session_id"]

    top = naming.top_speakers(db, new_sid)
    assert top["voiceprint"] is False
    assert "不会注册声纹" in top["hint"]

    spk = top["speakers"][0]["speaker_id"]
    out = naming.name_speaker(db, new_sid, spk, "老王")
    assert out["ok"] is True and out["enrolled"] is False
    assert "云端导入" in out["reason"]
    # 没有进人物底库：不能凭空造出一个「已确认」的身份。
    assert db.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"] == 0
    entry = identity.roster(db, new_sid)["by_id"][spk]
    assert entry["who"] == "老王"
    assert entry["source"] == "nickname"

    with pytest.raises(ValueError) as err:
        recluster.recluster_session(db, new_sid)
    assert "云端导入" in str(err.value)


def test_imported_session_needs_no_local_fallback(db, tmp_path):
    """解析不出句子就明确失败，不生成空场次。"""
    bad = tmp_path / "说明.txt"
    bad.write_text("这不是逐字稿：只是一段说明文字。", encoding="utf-8")
    with pytest.raises(importer.ImportProblem) as err:
        importer.import_file(db, bad, name="说明.txt")
    assert "没有解析到任何发言" in str(err.value)
    assert db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def http_base(db, tmp_path, monkeypatch):
    monkeypatch.setattr(server.rooms, "PATH", tmp_path / "rooms.json")
    srv = server.make_server("127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        srv.shutdown()
        srv.server_close()
        live.stop_watchdog()


def _upload(base, data: bytes, name: str, headers: dict | None = None):
    h = {"Content-Type": "application/octet-stream",
         "X-File-Name": urllib.parse.quote(name)}
    h.update(headers or {})
    req = urllib.request.Request(base + "/api/import/file", data=data,
                                 method="POST", headers=h)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post_json(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_import_file_and_library(http_base, db):
    srt = ("1\n00:00:00,000 --> 00:00:02,000\n阿哲：新号别急着投流\n\n"
           "2\n00:00:02,000 --> 00:00:04,000\n小北：预算怎么定？\n\n"
           "3\n00:00:04,000 --> 00:00:06,000\n阿哲：先跑七天自然流量\n")
    code, out = _upload(http_base, srt.encode(), "transcript.srt")
    assert code == 201 and out["ok"] and out["n_utterances"] == 3
    sid = out["session_id"]
    assert sid.startswith("IMP_")
    code, same = _upload(http_base, srt.encode(), "transcript.srt")
    assert code == 200 and same["existed"] is True

    with urllib.request.urlopen(http_base + "/api/library") as r:
        lib = json.loads(r.read())
    row = [s for s in lib["sessions"] if s["id"] == sid][0]
    assert row["source_mode"] == "import" and row["n_utterances"] == 3


def test_http_import_text_and_bad_input(http_base, db):
    text = ("# 2026-11-27 21:30 ·「语音厅复盘」\n"
            "场次：LIVE_20261127_213000\n\n[00:00:03] 主播：欢迎来到直播间\n")
    code, out = _post_json(http_base, "/api/import/text", {"text": text})
    assert code == 201 and out["session_id"] == "IMP_LIVE_20261127_213000"
    code, err = _post_json(http_base, "/api/import/text", {"text": "   "})
    assert code == 400
    code, err = _upload(http_base, b"RIFF....", "x.wav")
    assert code == 400 and "只支持" in err["error"]


def test_http_import_audio_attach(http_base, db, tmp_path):
    add_session(db, "IMP_LIVE_X", title="导入的一场", source_mode="import")
    add_rows(db, "IMP_LIVE_X", [utt_row(0, "一句话")])
    wav = _wav(tmp_path / "a.wav")
    code, out = _upload(http_base, wav.read_bytes(), "a.wav",
                        {"X-Import-Audio": "1", "X-Import-Session": "IMP_LIVE_X"})
    assert code == 200 and out["ok"] and out["has_audio"] is True
    assert Path(store.get_session(db, "IMP_LIVE_X")["audio_path"]).is_file()


def test_summary_caveat_mentions_import(db):
    add_session(db, "IMP_X", source_mode="import")
    add_rows(db, "IMP_X", [utt_row(i, "一句有信息量的话，用来凑总结素材 %d" % i)
                           for i in range(4)])
    scope = analysis.build_scope(db, "IMP_X", {}, "analysis")
    assert any("云端导入" in c for c in scope.caveats)
