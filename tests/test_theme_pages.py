# -*- coding: utf-8 -*-
"""深色主题回归：入口页都要带主题引导，旧版亮色页要有深色覆盖。"""
from __future__ import annotations

import threading
import urllib.request

import pytest

from app import live, server


@pytest.fixture
def http_base():
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
        assert r.status == 200, path
        return r.read().decode("utf-8")


#: 旧版页面：之前把亮色写死在 CSS 里，靠 theme-dark.css 覆盖。
LEGACY_DARK = ["/", "/files", "/people", "/reader", "/highlights", "/glossary", "/pushes", "/live"]

#: p1 页面：p1.css 自带 studio / paper 两套 token，只需要主题引导。
P1_PAGES = ["/follows", "/library", "/note?session_id=DEMO", "/weekly", "/delivery"]


def test_legacy_pages_boot_theme_and_load_dark_css(http_base):
    for path in LEGACY_DARK:
        text = _text(http_base, path)
        assert '/static/js/theme.js' in text, path
        assert '/static/css/theme-dark.css' in text, path


def test_p1_pages_boot_theme(http_base):
    for path in P1_PAGES:
        assert '/static/js/theme.js' in _text(http_base, path), path


def test_theme_assets_are_served(http_base):
    js = _text(http_base, "/static/js/theme.js")
    assert "ln_theme" in js
    assert "prefers-color-scheme" in js

    css = _text(http_base, "/static/css/theme-dark.css")
    assert 'data-theme="studio"' in css
    assert 'data-theme="dark"' in css
    assert "--bg:#0A0C0F" in css
