# -*- coding: utf-8 -*-
"""P1 页面与接口冒烟：路由分开、基座锚点、二维码与笔记库接口。"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from app import live, server, store


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
        ("/note?session_id=DEMO", 'id="noteDoc"'),
    ]
    for path, marker in pages:
        text = _text(http_base, path)
        assert marker in text, path
        assert 'data-ln-region="topbar"' in text, path
        assert 'data-ln-region="stage"' in text, path


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
