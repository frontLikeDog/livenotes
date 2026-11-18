# -*- coding: utf-8 -*-
"""精修交付：项目、报告版本、质检、打包与交付状态。

只测后端行为，不启动浏览器。涉及 ffmpeg 的切片在别处已覆盖，
这里统一用 ``include_audio=False`` 走纯文字交付包。
"""
from __future__ import annotations

import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from helpers import add_rows, add_session, utt_row

from app import delivery

SID = "S_DLV"


def _seed(conn, **kw):
    row = {
        "title": "11-18 选品专场",
        "started_at": "2026-11-18T20:00:00",
        "duration_s": 600.0,
        "speakers": [{"speaker_id": "SPK_00", "display_name": "阿哲", "verified": 1}],
    }
    row.update(kw)
    add_session(conn, SID, **row)
    add_rows(conn, SID, [
        utt_row(0, "今天我们重点讲三个选品方向，先从秋冬外套开始。",
                speaker="SPK_00", label="阿哲", importance=0.9),
        utt_row(1, "第二个方向是家居香氛，复购率比外套更高。",
                t=30, speaker="SPK_00", label="阿哲", importance=0.8),
    ])


def _project(conn, **kw):
    params = {"event_name": "双十一选品复盘", "client_name": "品牌 A"}
    params.update(kw)
    return delivery.create_project(conn, SID, **params)


def test_create_defaults_and_validation(db):
    _seed(db)
    out = _project(db)
    p = out["project"]
    assert p["id"].startswith("dlv_")
    assert p["state"] == "draft"
    assert p["package_key"] == "standard"
    assert p["price_cents"] == 99900
    assert p["due_at"] == (date.today() + timedelta(days=3)).isoformat()
    assert out["report"] is None
    assert any(c["key"] == "session" and c["status"] == "pass"
               for c in out["qa"]["checks"])

    with pytest.raises(ValueError):
        delivery.create_project(db, "NOPE")


def test_draft_reuses_notes_and_evidence(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    out = delivery.build_draft(db, pid)
    rep = out["report"]
    assert rep["engine"] == "draft"
    assert out["project"]["state"] == "producing"
    assert [s["key"] for s in rep["sections"]] == [
        "lead", "context", "points", "quotes", "clips", "actions", "questions"]

    secs = {s["key"]: s for s in rep["sections"]}
    assert "本场共 2 条发言" in secs["lead"]["body"]
    assert "时长：10 分钟" in secs["context"]["body"]
    assert "素材：2 条发言" in secs["context"]["body"]
    assert "### 1." in secs["points"]["body"]
    assert "[00:00:00]" in secs["points"]["body"]
    assert "秋冬外套" in secs["points"]["body"]
    assert secs["points"]["items"][0]["t"] == 0.0

    qa = {c["key"]: c for c in out["qa"]["checks"]}
    assert qa["session"]["status"] == "pass"
    assert qa["report"]["status"] == "pass"
    assert qa["evidence"]["status"] == "pass"
    assert qa["cover"]["status"] == "pass"


def test_manual_save_versions_and_force(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    out = delivery.build_draft(db, pid)
    sections = [dict(s) for s in out["report"]["sections"]]
    sections[0]["body"] = "这是人工改过的导语。"

    saved = delivery.save_report(db, pid, sections)
    assert saved["report"]["version"] == 2
    assert saved["report"]["engine"] == "manual"
    assert saved["report"]["sections"][0]["body"] == "这是人工改过的导语。"
    assert saved["project"]["state"] == "producing"

    with pytest.raises(ValueError):
        delivery.build_draft(db, pid)

    again = delivery.build_draft(db, pid, force=True)
    assert again["report"]["version"] == 3
    assert again["report"]["engine"] == "draft"


def test_sections_tolerate_bad_numbers(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    out = delivery.save_report(db, pid, [{
        "key": "points", "title": "核心内容", "kind": "points", "body": "正文",
        "items": [{"t0": "abc", "t1": None, "t": "?", "title": "片段"}],
    }])
    item = out["report"]["sections"][0]["items"][0]
    assert item["t0"] == 0.0 and item["t1"] == 0.0 and item["t"] == 0.0

    with pytest.raises(ValueError):
        delivery.save_report(db, pid, [])


def test_qa_reports_missing_cover_and_empty_report(db):
    _seed(db)
    pid = delivery.create_project(db, SID)["project"]["id"]
    qa = {c["key"]: c for c in delivery.qa(db, pid)["checks"]}
    assert qa["report"]["status"] == "fail"
    assert qa["cover"]["status"] == "fail"
    assert qa["session"]["status"] == "pass"
    assert delivery.qa(db, pid)["overall"] == "fail"

    delivery.update_project(db, pid, event_name="复盘", client_name="客户")
    out = delivery.build_draft(db, pid)
    qa = {c["key"]: c for c in out["qa"]["checks"]}
    assert qa["cover"]["status"] == "pass"
    # 没有录音、也没有高光片段，只到 warn。
    assert qa["audio"]["status"] == "warn"
    assert out["qa"]["overall"] == "warn"


def test_build_package_files_and_zip(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    delivery.build_draft(db, pid)
    out = delivery.build_package(db, pid, include_audio=False)
    info = out["built"]
    assert out["project"]["state"] == "review"
    assert info["clips"] == 0
    names = set(info["files"])
    assert {"报告.md", "报告.html", "逐字稿.srt", "逐字稿.md",
            "README.md", "meta.json"} <= names

    folder = Path(delivery.DELIVERIES_DIR) / pid
    md = (folder / "报告.md").read_text(encoding="utf-8")
    assert "# 双十一选品复盘 · 精修报告" in md
    assert "品牌 A" in md
    assert "秋冬外套" in md
    html = (folder / "报告.html").read_text(encoding="utf-8")
    assert "<h2>导语</h2>" in html
    assert "<blockquote>" in html
    srt = (folder / "逐字稿.srt").read_text(encoding="utf-8")
    assert "00:00:00,000" in srt
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    assert meta["project_id"] == pid and meta["client"] == "品牌 A"

    zpath = delivery.package_path(db, pid)
    assert zpath and zpath.is_file()
    with zipfile.ZipFile(zpath) as zf:
        znames = set(zf.namelist())
    assert names <= znames


def test_deliver_and_revision(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    with pytest.raises(ValueError):
        delivery.mark_delivered(db, pid)

    delivery.build_draft(db, pid)
    delivery.build_package(db, pid, include_audio=False)
    out = delivery.mark_delivered(db, pid)
    assert out["project"]["state"] == "delivered"
    assert out["project"]["delivered_at"]
    stats = delivery.list_projects(db)["stats"]
    assert stats["delivered"] == 1 and stats["revenue_cents"] == 99900

    out = delivery.add_revision(db, pid, "客户要补嘉宾介绍")
    assert out["project"]["revision_count"] == 1
    assert out["project"]["state"] == "revising"
    assert "[修订 1" in out["project"]["notes"]
    assert "客户要补嘉宾介绍" in out["project"]["notes"]
    # 交付过又修订，仍计入「已交付」与本月交付额。
    stats = delivery.list_projects(db)["stats"]
    assert stats["delivered"] == 1 and stats["revenue_cents"] == 99900

    # 改报告会离开 revising；重新打包后回到待交付。
    saved = delivery.save_report(db, pid, out["report"]["sections"])
    assert saved["project"]["state"] == "producing"
    rebuilt = delivery.build_package(db, pid, include_audio=False)
    assert rebuilt["project"]["state"] == "review"


def test_list_stats_are_global_and_filtered(db):
    _seed(db)
    a = _project(db, event_name="A 场")["project"]["id"]
    b = _project(db, event_name="B 场")["project"]["id"]
    delivery.build_draft(db, a)
    delivery.build_package(db, a, include_audio=False)
    delivery.mark_delivered(db, a)

    all_out = delivery.list_projects(db)
    assert all_out["stats"]["active"] >= 1
    assert all_out["stats"]["delivered"] == 1
    assert all_out["stats"]["revenue_cents"] == 99900

    active = delivery.list_projects(db, "active")
    assert [x["id"] for x in active["items"]] == [b]
    assert active["stats"]["delivered"] == 1

    delivered = delivery.list_projects(db, "delivered")
    assert [x["id"] for x in delivered["items"]] == [a]


def test_update_project_fields_and_remove(db):
    _seed(db)
    pid = _project(db)["project"]["id"]
    out = delivery.update_project(
        db, pid, event_name="新名称", client_name="新客户", package="deep",
        due_at="2026-11-20", price_cents=123400, notes="加急",
        checklist={"authorized": True, "bogus": True})
    p = out["project"]
    assert p["event_name"] == "新名称"
    assert p["package_key"] == "deep" and p["package_label"] == "深度复盘"
    assert p["price_cents"] == 123400
    assert p["checklist"] == {"authorized": True}
    checklist = {x["key"]: x for x in out["checklist"]["items"]}
    assert checklist["authorized"]["checked"] is True
    assert checklist["scope"]["checked"] is False

    with pytest.raises(ValueError):
        delivery.update_project(db, pid, package="nope")

    delivery.build_draft(db, pid)
    delivery.build_package(db, pid, include_audio=False)
    folder = Path(delivery.DELIVERIES_DIR) / pid
    assert folder.is_dir()
    delivery.remove_project(db, pid)
    assert not folder.exists()
    with pytest.raises(ValueError):
        delivery.get_project(db, pid)


def test_render_escapes_client_html(db):
    _seed(db)
    pid = _project(db, event_name="A & B <复盘>")["project"]["id"]
    out = delivery.build_draft(db, pid)
    html = delivery.render_html(out["project"], out["report"])
    assert "A &amp; B &lt;复盘&gt;" in html
    md = delivery.render_markdown(out["project"], out["report"])
    assert "A & B <复盘>" in md
