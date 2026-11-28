# -*- coding: utf-8 -*-
"""P1 页面与接口冒烟：路由分开、基座锚点、二维码与笔记库接口。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app import live, server, settings, store


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


def _text(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=10) as r:
        assert r.status == 200
        return r.read().decode("utf-8")


def _json(base: str, path: str) -> dict:
    return json.loads(_text(base, path))


def test_library_and_file_center_are_separate_pages(http_base):
    library = _text(http_base, "/library")
    assert 'id="libGrid"' in library
    assert "笔记库" in library

    files = _text(http_base, "/files")
    assert 'id="tabs"' in files
    assert 'id="libGrid"' not in files


def test_p1_pages_keep_base_anchors(http_base):
    pages = [
        ("/follows", 'id="followList"'),
        ("/weekly", 'id="weeklyBody"'),
        ("/delivery", 'id="dlvList"'),
        ("/note?session_id=DEMO", 'id="noteDoc"'),
    ]
    for path, marker in pages:
        text = _text(http_base, path)
        assert marker in text, path
        assert 'data-ln-region="topbar"' in text, path
        assert 'data-ln-region="stage"' in text, path


def test_qa_suggest_can_be_disabled(http_base):
    settings.set_prefs({"qa_suggest_enabled": False})
    assert _json(http_base, "/api/qa/suggest") == {"items": [], "enabled": False}
    settings.set_prefs({"qa_suggest_enabled": True})
    data = _json(http_base, "/api/qa/suggest")
    assert data["enabled"] is True
    assert "items" in data


def test_perf_endpoint_reports_power_state(http_base):
    data = _json(http_base, "/api/perf")
    assert "low_power" in data
    assert "power" in data
    assert "partials" in data["effective"]


def test_qr_endpoint_returns_matrix(http_base):
    data = _json(http_base, "/api/qr?text=LiveNotes")
    assert data["size"] == 17 + 4 * data["version"]
    assert len(data["rows"]) == data["size"]
    assert all(len(row) == data["size"] for row in data["rows"])

    with pytest.raises(urllib.error.HTTPError) as err:
        _json(http_base, "/api/qr?text=")
    assert err.value.code == 400


def test_library_cards_endpoint(http_base, db):
    store.insert_session(db, {
        "id": "S1", "title": "选品夜谈", "started_at": "2026-11-10T20:00:00",
        "duration_s": 3600, "source_mode": "browser",
    })
    store.upsert_note(db, "S1", state="ready", progress=100,
                      generated_at="2026-11-10T22:00:00")
    data = _json(http_base, "/api/library/cards?limit=12")
    assert data["items"]
    row = data["items"][0]
    assert row["session_id"] == "S1"
    assert row["note_state"] == "ready"
    assert data["counts"]["all"] == 1

    searched = _json(http_base, "/api/search?q=%E9%80%89%E5%93%81&types=notes")
    assert searched["q"] == "选品"
    assert "rows" in searched


def test_library_filter_status_reports_current_result_count(http_base):
    """笔记库筛选状态：页面有 #libCount + 关键词框，数量显示「共 N 条」。"""
    page = _text(http_base, "/library")
    assert 'id="libCount"' in page
    assert "共 0 条" in page
    assert 'id="libSearch"' in page
    for marker in ('data-lib-filter="all"', 'data-lib-filter="week"',
                   'data-lib-filter="ready"', 'data-lib-filter="running"'):
        assert marker in page, marker
    assert "生成中" in page
    # 空态容器保留
    assert 'id="libEmpty"' in page


def test_library_cards_total_tracks_filters_keyword_sort_and_paging(http_base, db):
    """/api/library/cards 返回当前筛选下的 total；翻页不改变，无结果为 0。"""
    from datetime import datetime, timedelta
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = today - timedelta(days=today.weekday())
    for i in range(6):
        sid = "S%d" % i
        store.insert_session(db, {
            "id": sid,
            "title": "选品夜谈" if i == 0 else "复盘 %d" % i,
            # 前 2 场本周，其余本周之前
            "started_at": (monday + timedelta(hours=i)).isoformat(timespec="seconds")
                          if i < 2 else
                          (monday - timedelta(days=1 + i, hours=i)).isoformat(timespec="seconds"),
            "duration_s": 60 * (i + 1), "source_mode": "browser",
        })
    store.upsert_note(db, "S0", state="ready", progress=100,
                      generated_at="2026-11-10T22:00:00")

    base = "/api/library/cards?limit=2"

    # 全部
    data = _json(http_base, base)
    assert data["total"] == 6 == data["counts"]["all"]
    assert len(data["items"]) == 2
    cursor = data["next_cursor"]
    assert cursor
    # 翻页：不重复、total 不变、数量与列表一致
    seen = {x["session_id"] for x in data["items"]}
    page = _json(http_base, base + "&cursor=" + cursor)
    assert page["total"] == data["total"] == 6
    assert not seen & {x["session_id"] for x in page["items"]}

    # 排序不改总数；duration 排序翻页不重复
    for sort in ("time", "duration", "generated"):
        d = _json(http_base, base + "&sort=" + sort)
        assert d["total"] == 6, sort
        if d["next_cursor"]:
            d2 = _json(http_base, base + "&sort=" + sort + "&cursor=" + d["next_cursor"])
            assert d2["total"] == 6
            ids1 = {x["session_id"] for x in d["items"]}
            ids2 = {x["session_id"] for x in d2["items"]}
            assert not ids1 & ids2, sort

    # 本周
    week = _json(http_base, base + "&filter=week")
    assert week["total"] == 2
    # 已生成
    ready = _json(http_base, base + "&filter=ready")
    assert ready["total"] == 1
    # 生成中：无 running → 0，但空状态所需的 counts.all 仍在
    running = _json(http_base, base + "&filter=running")
    assert running["total"] == 0
    assert running["items"] == []
    assert running["counts"]["all"] == 6

    # 关键词：输入/清空
    kw = _json(http_base, base + "&q=%E9%80%89%E5%93%81")  # 选品
    assert kw["total"] == 1
    assert kw["items"][0]["session_id"] == "S0"
    cleared = _json(http_base, base + "&q=")
    assert cleared["total"] == 6
    # 关键词无匹配 → total=0
    none = _json(http_base, base + "&q=zzz-not-exist")
    assert none["total"] == 0
    assert none["items"] == []
    # 关键词 + 筛选组合
    combo = _json(http_base, base + "&filter=ready&q=%E9%80%89%E5%93%81")
    assert combo["total"] == 1
    # 关键词 + 排序组合
    combo_sort = _json(http_base, base + "&q=%E5%A4%8D%E7%9B%98&sort=duration")  # 复盘
    assert combo_sort["total"] == 5


def test_library_cards_keyword_matches_card_fields_and_note_units(http_base, db):
    """关键词覆盖卡片展示字段与 note_units，历史总结未建索引也能命中。"""
    sid = "S1"
    store.insert_session(db, {
        "id": sid, "title": "原始标题", "started_at": "2026-11-10T20:00:00",
        "duration_s": 60, "source_mode": "browser",
    })
    # 历史总结：只有 summaries.payload_json，没有 note_units 索引
    db.execute(
        "INSERT INTO summaries(session_id, scope, filter_json, engine, "
        "payload_json, created_at) VALUES (?,?,?,?,?,?)",
        (sid, "content", "{}", "test",
         '{"session_title": "选品专场", "overview": "本场讲复购策略", '
         '"host_speaker": "阿哲", "topics": ["选品", "复购"], "points": []}',
         "2026-11-10T21:00:00"))
    db.commit()
    assert db.execute("SELECT COUNT(*) AS n FROM note_units").fetchone()["n"] == 0

    def total(q):
        return _json(http_base,
                     "/api/library/cards?limit=24&q=" + urllib.parse.quote(str(q)))["total"]

    base = _json(http_base, "/api/library/cards?limit=24")
    card = base["items"][0]
    # 卡片展示的字段都要能搜到：标题 / 主播 / 概述 / 主题
    for kw, expect in [(card["title"], 1), (card["streamer"] or card["title"], 1)]:
        assert total(kw) == expect, kw
    assert total("选品专场") == 1          # summary session_title
    assert total("原始标题") == 1          # session title
    assert total("复购策略") == 1          # summary overview
    assert total("阿哲") == 1              # host_speaker 主播名
    assert total("复购") == 1              # topics
    assert total("zzz-not-exist") == 0

    # note_units 命中路径：要点正文 / 署名
    db.execute(
        "INSERT INTO note_units(session_id, kind, ref_id, t, speaker, title, "
        "text, weight, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (sid, "point", "0", None, "小王、阿哲", "选品专场",
         "这条要点讲 ROI 回收", 1.0, None))
    db.commit()
    assert total("ROI") == 1               # note_units.text
    assert total("小王") == 1              # note_units.speaker
