# -*- coding: utf-8 -*-
"""云端 E2E：仅采集 → 离线补转写 → 自动收尾 → 发布 → 飞书 /总结。

这是 docs/46 §5.2 指出的缺口：各段单独有测试，但没有一条用例证明
「云端只录音的一路，最后能在手机上看飞书文档和阶段性总结」。
这里把三段真实边界接起来：

  * 离线进程读真实 PCM/WAV、真 VAD 替身、真 SQLite；
  * 收尾走真实 HTTP 接口 + 真调度器（模型换成固定返回）；
  * 发布走真实 feishu_publish，lark 客户端换成内存替身。

不截图、不联网，按项目测试口径只做轻量集成。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

from app import (feishu_publish, live, livebrief, llm, offline_asr, scheduler,
                 server, settings, store)
from test_feishu_publish import _bot
from test_offline_asr import FakeEngine, FakeVad, _pcm

SID = "LIVE_CLOUD_E2E"


def _cfg() -> dict:
    return {"base": "http://example.invalid/v1", "key": "k", "model": "test-model",
            "temperature": 0.2, "timeout": 5, "configured": True, "source": "test"}


class LongEngine(FakeEngine):
    """每段都出一句足够长的话：总结闸门要求 ≥15 条 / ≥400 字。"""

    def transcribe(self, samples) -> str:
        self.texts += 1
        return ("这是一句测试发言，用来验证离线补转写之后的自动收尾链路是否完整，"
                "以及发布是否始终只建一篇文档。")


@pytest.fixture
def sched(monkeypatch):
    s = scheduler.Scheduler(concurrency=1, backoff_base=0, backoff_max=0,
                            persist=True, name="cloud-e2e")
    for kind, handler in scheduler.SCHEDULER._handlers.items():
        s.register(kind, handler)
    monkeypatch.setattr(scheduler, "SCHEDULER", s)
    yield s
    s.shutdown()


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


def _json(base: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        base + path, data=data, method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _wav(path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(live.SAMPLE_RATE)
        w.writeframes(_pcm(seconds))


def _fake_finalize(calls: list):
    def fake(cfg, payload, timeout, total_timeout=None):
        calls.append(1)
        body = {
            "overview": "整场在聊 AI 出海先做中东还是东南亚。",
            "points": [
                {"title": "先做东南亚", "gist": "冷启动更快，一个月能跑出模型。",
                 "evidence_ids": [1, 2, 3]},
                {"title": "先做中东", "gist": "客单价高，但本地化慢。",
                 "evidence_ids": [4, 5, 6]},
            ],
            "topics": ["AI 出海"],
            "title": "AI 出海先做哪边",
            "title_alts": ["出海中东还是东南亚"],
        }
        return {"choices": [{"message": {"content": json.dumps(
                    body, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 30,
                          "total_tokens": 130}}
    return fake


def _fake_brief(messages, **kw):
    user = json.loads(messages[1]["content"])
    ids = [int(u[0]) for u in user["u"]]
    return json.dumps({
        "overview": "这场主要在讨论 AI 出海的市场选择。",
        "points": [
            {"title": "先做东南亚", "gist": "冷启动更快。",
             "evidence_ids": ids[:2]},
            {"title": "先做中东", "gist": "客单价高。", "evidence_ids": ids[2:4]},
        ],
    }, ensure_ascii=False)


def test_cloud_chain_capture_offline_finalize_publish_and_brief(
        db, sched, http_base, isolated_paths, monkeypatch):
    # ---- 1. 云端只录了一路：库里只有场次，没有逐字稿 -------------------
    store.insert_session(db, {
        "id": SID, "title": "语音厅 · 云端", "started_at": "2026-11-25T20:00:00",
        "duration_s": 0.0, "source_mode": "direct", "capture_only": 1,
        "sample_rate": live.SAMPLE_RATE,
    })
    pcm_path = isolated_paths / "audio" / (SID + ".pcm")
    pcm_path.write_bytes(_pcm(2.0))

    llm_calls: list = []
    monkeypatch.setattr(settings, "resolve", lambda *a, **k: _cfg())
    monkeypatch.setattr(llm, "_post_chat", _fake_finalize(llm_calls))
    pub_cfg = {"enabled": True, "app_id": "a", "app_secret": "s", "folder": "",
               "owner": "ou_owner", "doc_url_base": "",
               "base_app_token": "app_x", "base_table_id": "tbl_1",
               "base_url_base": ""}
    monkeypatch.setattr(feishu_publish, "config", lambda: dict(pub_cfg))
    bot = _bot()
    monkeypatch.setattr(feishu_publish, "_default_bot", lambda cfg: bot)

    worker = offline_asr.OfflineAsr(
        engine=LongEngine(), vad_factory=lambda: FakeVad(16000), speakers=False,
        audio_dir=isolated_paths / "audio", api_base=http_base)

    # 直播中：只补字，不收尾（没有 audio_path，文件还在长）
    out = worker.run_once(sid=SID)
    assert out["sessions"] == 1 and out["done"] == 0
    assert store.get_offline_asr(db, SID)["status"] == "running"
    assert store.get_offline_asr(db, SID)["finalize_state"] == ""

    # ---- 2. 收尾：WAV 落盘后补完尾巴，自动触发引擎的收尾 ---------------
    wav_path = isolated_paths / "audio" / (SID + ".wav")
    _wav(wav_path, 18.0)
    store.finish_session(db, SID, 18.0, str(wav_path), audio_format="wav")

    out = worker.run_once(sid=SID)
    assert out["done"] == 1
    prog = store.get_offline_asr(db, SID)
    assert prog["status"] == "done"
    assert prog["n_utterances"] >= 15
    assert prog["finalize_state"] == "queued"
    assert prog["finalize_job"]

    # 轮询引擎的任务，直到收尾完成（调度器线程里跑模型 + 发布）。
    state = "queued"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        worker.run_once(sid=SID)
        state = str(store.get_offline_asr(db, SID)["finalize_state"])
        if state in ("done", "failed"):
            break
        time.sleep(0.05)
    assert state == "done", state
    assert len(llm_calls) == 1        # 同一场只总结一次
    # 离线进程就是靠这个接口跟任务进度的（任务在内存里时也能查到）
    code, job = _json(http_base, "/api/jobs?job_id=" +
                      str(store.get_offline_asr(db, SID)["finalize_job"]))
    assert code == 200 and job["job"]["status"] == "succeeded"

    # ---- 3. 总结与索引真的落库了 -------------------------------------
    summary = store.latest_summary(db, SID, "content")
    assert summary and summary["payload"]["overview"]
    units = db.execute("SELECT COUNT(*) AS n FROM note_units WHERE session_id=?",
                       (SID,)).fetchone()["n"]
    assert units > 0
    assert store.get_note(db, SID)["state"] == "ready"

    # ---- 4. 发布：一篇文档 + 场次表一行 + 完成卡片 --------------------
    row = store.get_publish(db, SID)
    assert row["state"] == "ok", row
    assert row["doc_url"].endswith("/doc_1")
    assert row["record_id"] == "rec_1"
    assert len(bot.client.docx_v.creates) == 1
    assert len(bot.client.records.created) == 1
    assert len(bot.cards) == 1
    md = bot.client.docx_v.converts[0]
    assert "整场在聊 AI 出海先做中东还是东南亚。" in md
    assert "## 观点地图" in md and "冷启动更快" in md
    assert "## 逐字稿" not in md       # 手机端只留结论

    # 重复发布不新增文档/行
    again = feishu_publish.publish(db, SID)
    assert again["skipped"] is True and again["reason"] == "unchanged"
    assert len(bot.client.docx_v.creates) == 1
    assert len(bot.client.records.created) == 1

    # ---- 5. 补出字之后：网页总结接口与飞书 /总结 都能用 ----------------
    code, data = _json(http_base, "/api/summarize",
                       {"session_id": SID, "filters": {}})
    assert code == 200, data
    assert data.get("overview")

    code, out = _json(http_base, "/api/publish?session_id=" + SID)
    assert code == 200 and out["enabled"] is True
    assert out["publish"]["doc_url"] == row["doc_url"]

    livebrief.reset()
    monkeypatch.setattr(livebrief, "BRIEF_LLM", True)
    monkeypatch.setattr(livebrief, "BRIEF_DAILY_CAP", 100)
    monkeypatch.setattr(livebrief, "BRIEF_SESSION_TOKEN_CAP", 0)
    monkeypatch.setattr(livebrief, "_chat", _fake_brief)
    room = {"id": "rm1", "seq": 1, "name": "语音厅", "state": "stopped",
            "session_id": SID, "capture_engine": "direct"}
    code, brief = livebrief.api(db, [room], [], {},
                                {"kind": "summary", "room_id": "rm1",
                                 "refresh": "1"})
    assert code == 200, brief
    assert brief["ok"] and brief["engine"] == "llm"
    assert brief["points"] and brief["room"]["ch"] == "CH01"


def test_finalize_endpoint_refuses_empty_capture_only(db, http_base, monkeypatch):
    from app import tasks

    store.insert_session(db, {
        "id": "LIVE_CLOUD_EMPTY", "title": "还没补出字", "started_at":
        "2026-11-25T21:00:00", "duration_s": 10.0, "source_mode": "direct",
        "capture_only": 1,
    })
    code, out = _json(http_base, "/api/sessions/finalize",
                      {"session_id": "LIVE_CLOUD_EMPTY"})
    assert code == 409
    assert out["not_ready"] is True and out["capture_only"] is True

    # 补出字之后同一个接口就放行。这里只验接口契约；真的排队/跑总结
    # 在上一条用例里走过，这里不把任务甩给全局调度器（会在夹具拆除后继续跑）。
    class _Job:
        id = "job_stub"
        status = "queued"
        cached = False

    monkeypatch.setattr(tasks, "submit_finalize", lambda sid, **kw: _Job())
    store.insert_utterances(db, [{
        "session_id": "LIVE_CLOUD_EMPTY", "t_start": 0.0, "t_end": 1.0,
        "speaker_id": "SPK_00", "speaker_label": "说话人", "text": "补出来的一句",
    }])
    code, out = _json(http_base, "/api/sessions/finalize",
                      {"session_id": "LIVE_CLOUD_EMPTY"})
    assert code == 200 and out["job_id"] == "job_stub"
