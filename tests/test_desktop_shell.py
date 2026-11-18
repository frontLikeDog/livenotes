# -*- coding: utf-8 -*-
"""桌面壳导航：页面入口、视图元数据与 URL 路由必须同时接上。

壳没有前端测试框架，页签漏一处最常见的表现是「点了没反应」或
「打开是空白 iframe」。这里用静态检查盯住新页面的四处接线。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "desktop" / "dist"


def _read(name: str) -> str:
    return (SHELL / name).read_text(encoding="utf-8")


def test_delivery_view_is_wired_into_shell():
    html = _read("shell.html")
    js = _read("shell.js")

    assert 'data-view="delivery"' in html
    assert "delivery: { title: '精修交付'" in js
    assert "view === 'delivery'" in js
    assert "setFrame(API + '/delivery?'" in js
    assert "path === '/delivery'" in js
    assert "id: 'delivery'" in js


# ---------------------------------------------------------------- 多路 → 记录卡
# 35 §4.6：≥2 路在录时不进 HUD，但收听页必须有入口。之前只有拦截、
# 没有入口，多路时应用内根本进不去记录卡（只有托盘/通知能进）。
def test_multi_route_hud_has_entry_points():
    html = _read("shell.html")
    js = _read("shell.js")

    # 舞台上方那条常驻提示条（35 §4.6 的「查看记录卡」）
    assert 'id="multiBar"' in html
    assert "function renderMultiBar()" in js
    assert "renderMultiBar();" in js

    # 房间行右键菜单
    assert "label: '进入记录卡'" in js

    # 右栏操作区
    assert 'data-act="hud"' in js

    # 进入时必须带具体房间，否则记录卡仍停在上一路
    assert "function openHudFor(room)" in js
    assert "switchView('hud', { force: true, room: room.id })" in js


def test_multi_route_bar_is_rendered_when_two_rooms_live():
    """提示条只在收听页、且至少 2 路在录时出现；1 路时它不该占位。"""
    js = _read("shell.js")
    block = js.split("function renderMultiBar()", 1)[1].split("\n}\n", 1)[0]
    assert "live.length < 2" in block
    assert "S.view !== 'listen'" in block


def test_multi_route_toast_offers_next_step():
    """拦截 toast 必须可行动：原实现只说「先在这里选一路」，听完无处可点。"""
    js = _read("shell.js")
    assert "先在这里选一路" in js
    assert "label: '选一路'" in js
    assert "function pickMultiRoom()" in js


def test_hud_multi_switch_back_preserves_room():
    """记录卡 → 多路 要落回刚看的那一路，不能停在上一次选中的另一路。"""
    js = _read("shell.js")
    assert "function backToMulti()" in js
    assert "$('#hudMulti').onclick = () => backToMulti();" in js


def test_shell_js_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 Node")
    for name in ("shell.js",):
        res = subprocess.run([node, "--check", str(SHELL / name)],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        assert res.returncode == 0, "%s\n%s" % (name, res.stderr)


def test_shell_html_ids_are_all_present():
    """多路入口的元素 id 必须真的在 DOM 里：JS 查不到只会静默什么都不做。"""
    html = _read("shell.html")
    js = _read("shell.js")
    for i in ("multiBar", "multiBarText", "multiRooms", "multiBarGo"):
        assert 'id="%s"' % i in html, i
        assert "#%s" % i in js, i
