# -*- coding: utf-8 -*-
"""按需补名：只补 top 3–5，补一次就入声纹底库，跳过不影响出卡。

这里不测前端渲染，只测后端口径：
1. 按发言时长排序、只取前几位；
2. 「已命名」只认已确认/声纹自动/真实昵称，位置标签不算；
3. 一键命名既改本场显示名，也注册声纹，还能把观点归属接回去；
4. 产品指标换成 top 3 命名率和可分享内容产出率。
"""
from __future__ import annotations

import json
import threading
import urllib.request

import numpy as np
import pytest

from app import claims, identity, live, naming, server, store


def _vec(x: float, y: float) -> bytes:
    return np.float16([x, y]).tobytes()


def _session(db) -> str:
    sid = "S_NAMING"
    store.insert_session(db, {"id": sid, "title": "按需补名一场",
                              "started_at": "2026-11-19T21:00:00",
                              "duration_s": 300, "source_mode": "tab"})
    spec = [
        ("SPK_00", "主播", 0.0, 90.0, 3, "欢迎来到直播间"),
        ("SPK_01", "小嘎皮", 100.0, 170.0, 3, "账期一到三个月吧"),
        ("SPK_02", "聚类标签 09", 180.0, 240.0, 3, "我补充一个数据"),
        ("SPK_03", "嘉宾1", 250.0, 270.0, 1, "抽成按什么口径算"),
    ]
    rows, ids = [], []
    for spk, label, t0, t1, n, text in spec:
        step = (t1 - t0) / n
        for i in range(n):
            rows.append({
                "session_id": sid, "speaker_id": spk, "speaker_label": label,
                "t_start": t0 + i * step, "t_end": t0 + (i + 1) * step,
                "text": text + (" %d" % i if n > 1 else ""),
                "category": ("question_consult" if spk == "SPK_03" else "substantive"),
                "topic": "结算",
            })
    ids = store.insert_utterances(db, rows)
    for spk, label, *_ in spec:
        store.upsert_speaker(db, sid, spk, label)
    # 只给 SPK_02 声纹：底库里有一个「阿哲」，应该被提示成「像是阿哲」。
    emb_ids = [uid for uid, row in zip(ids, rows) if row["speaker_id"] == "SPK_02"]
    store.save_embeddings(db, sid, [(uid, _vec(1.0, 0.0), 2) for uid in emb_ids])
    store.upsert_person(db, "per_a", "阿哲")
    store.add_person_emb(db, [("per_a", "S_OTHER", "SPK_09", 2,
                               _vec(0.98, 0.08), 1.0)])
    # 一条观点，用来验证可分享内容产出率，以及命名后观点归属能接回去。
    store.save_summary(db, sid, "content", {}, "extractive", {
        "session_title": "按需补名一场", "overview": "", "topics": ["结算"],
        "points": [{
            "title": "结算", "gist": "", "claim_speaker": "",
            "stance": "账期一到三个月", "t_from": 180, "t_to": 200,
            "evidence": [{
                "id": emb_ids[0], "t_start": 180, "t_end": 200,
                "speaker_id": "SPK_02", "speaker_label": "聚类标签 09",
                "category": "substantive", "topic": "结算",
                "quote": "账期一到三个月", "text": "账期一到三个月",
            }],
        }],
    })
    claims.rebuild_session(db, sid)
    return sid


def test_top_speakers_sorted_and_named_flags(db):
    sid = _session(db)
    out = naming.top_speakers(db, sid)
    assert out["ok"] is True
    assert [r["speaker_id"] for r in out["speakers"]] == [
        "SPK_00", "SPK_01", "SPK_02", "SPK_03"]
    by_id = {r["speaker_id"]: r for r in out["speakers"]}
    assert by_id["SPK_00"]["named"] is False       # 「主播」是角色，不是名字
    assert by_id["SPK_01"]["named"] is True        # 直播间昵称算已命名
    assert by_id["SPK_02"]["named"] is False       # 聚类标签不算
    assert by_id["SPK_02"]["label"] == "本场第 3 位上麦者"
    assert by_id["SPK_02"]["suggestion"]["name"] == "阿哲"
    assert by_id["SPK_02"]["preview"]["t_end"] - by_id["SPK_02"]["preview"]["t_start"] <= 3.0
    assert out["top3"] == {"slots": 3, "named": 1, "rate": round(1 / 3, 4)}


def test_name_speaker_renames_enrolls_and_rebuilds_claims(db):
    sid = _session(db)
    out = naming.name_speaker(db, sid, "SPK_02", "阿哲", person_id="per_a")
    assert out["ok"] is True and out["enrolled"] is True
    assert out["person_id"] == "per_a"
    roster = identity.roster(db, sid)
    assert roster["by_id"]["SPK_02"]["who"] == "阿哲"
    assert roster["by_id"]["SPK_02"]["source"] == "confirmed"
    row = claims.rows_for(db, session_id=sid)[0]
    assert row["person_id"] == "per_a"
    assert identity.claim_ref(row, roster)["who"] == "阿哲"
    after = naming.top_speakers(db, sid)
    assert after["top3"]["named"] == 2
    assert after["speakers"][2]["named"] is True


def test_failed_enroll_clears_stale_candidate_name(db):
    """改名后声纹注册失败，旧的「疑似」不能盖住用户输入的名字。"""
    sid = _session(db)
    store.upsert_person(db, "per_a", "阿哲")
    store.set_speaker_person(db, sid, "SPK_03", "per_a", 0.2, 0.05, "cand")
    out = naming.name_speaker(db, sid, "SPK_03", "小林")
    assert out["ok"] is True and out["enrolled"] is False
    entry = identity.roster(db, sid)["by_id"]["SPK_03"]
    assert entry["who"] == "小林" and entry["source"] == "nickname"


def test_failed_enroll_clears_stale_auto_name(db):
    """人工把 auto 匹配的人改成另一个名字，旧声纹身份也不能继续盖住。"""
    sid = _session(db)
    store.set_speaker_person(db, sid, "SPK_03", "per_a", 0.9, 0.1, "auto")
    out = naming.name_speaker(db, sid, "SPK_03", "小林")
    assert out["ok"] is True and out["enrolled"] is False
    entry = identity.roster(db, sid)["by_id"]["SPK_03"]
    assert entry["who"] == "小林" and entry["source"] == "nickname"


def test_legacy_rename_route_runs_full_naming(http_base, db):
    """主界面仍走 /api/speakers/rename：真人名也要注册声纹并接回观点。"""
    sid = _session(db)
    store.set_speaker_person(db, sid, "SPK_03", "per_a", 0.2, 0.05, "cand")
    out = _post(http_base, "/api/speakers/rename", {
        "session_id": sid, "speaker_id": "SPK_03", "name": "小林"})
    assert out["ok"] is True
    assert out["speaker"]["enrolled"] is False
    entry = identity.roster(db, sid)["by_id"]["SPK_03"]
    assert entry["who"] == "小林" and entry["source"] == "nickname"
    # 角色标签仍然只改显示，不报「不是名字」。
    role = _post(http_base, "/api/speakers/rename", {
        "session_id": sid, "speaker_id": "SPK_00", "name": "主播"})
    assert role["ok"] is True


def test_short_speaker_keeps_name_but_reports_not_enrolled(db):
    sid = _session(db)
    out = naming.name_speaker(db, sid, "SPK_03", "小林")
    assert out["ok"] is True
    assert out["enrolled"] is False
    assert "不够注册" in out["reason"]
    entry = identity.roster(db, sid)["by_id"]["SPK_03"]
    assert entry["who"] == "小林" and entry["source"] == "nickname"


def test_name_speaker_accepts_role_prefixed_nickname(db):
    sid = _session(db)
    out = naming.name_speaker(db, sid, "SPK_03", "主播小王")
    assert out["ok"] is True
    entry = identity.roster(db, sid)["by_id"]["SPK_03"]
    assert entry["who"] == "主播小王" and entry["source"] == "nickname"


def test_name_speaker_rejects_position_label(db):
    sid = _session(db)
    out = naming.name_speaker(db, sid, "SPK_03", "嘉宾1")
    assert out["ok"] is False
    assert "位置标签" in out["error"]
    # 被拒绝的占位标签不能把显示名改坏。
    assert identity.roster(db, sid)["by_id"]["SPK_03"]["source"] == "session"


def test_product_health_uses_top3_naming_and_shareable_output(db):
    sid = _session(db)
    before = naming.product_health(db, days=30)
    assert before["sessions"] == 1
    assert before["top3_naming"]["named_slots"] == 1
    assert before["top3_naming"]["slots"] == 3
    assert before["shareable_output"]["with_content"] == 1
    assert before["shareable_output"]["rate"] == 1.0

    naming.name_speaker(db, sid, "SPK_02", "阿哲", person_id="per_a")
    after = naming.product_health(db, days=30)
    assert after["top3_naming"]["named_slots"] == 2
    assert after["top3_naming"]["sessions_all_named"] == 0


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


def _json(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        return json.loads(resp.read().decode("utf-8"))


def test_naming_routes(http_base, db):
    sid = _session(db)
    data = _json(http_base, "/api/sessions/top_speakers?session_id=" + sid)
    assert data["ok"] and data["speakers"][0]["speaker_id"] == "SPK_00"
    named = _post(http_base, "/api/speakers/name", {
        "session_id": sid, "speaker_id": "SPK_02", "name": "阿哲",
        "person_id": "per_a"})
    assert named["ok"] and named["enrolled"] is True
    again = _json(http_base, "/api/sessions/top_speakers?session_id=" + sid)
    assert again["speakers"][2]["label"] == "阿哲"
    health = _json(http_base, "/api/product/health?days=30")
    assert health["ok"] and health["top3_naming"]["named_slots"] == 2
