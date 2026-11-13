# -*- coding: utf-8 -*-
"""局域网访问：状态文件、动态链接前缀、第二个监听的生命周期。"""
from __future__ import annotations

import json
import urllib.request

import pytest

from app import config, lan


@pytest.fixture(autouse=True)
def isolated_lan(tmp_path, monkeypatch):
    path = tmp_path / "runtime" / "lan.json"
    monkeypatch.setattr(lan, "_STATE_PATH", path)
    lan._INFO.clear()
    monkeypatch.setattr(lan, "_FIREWALL", "unknown")
    monkeypatch.setattr(lan, "_LAST_ERROR", "")
    yield path
    lan.reset_for_tests(path)


def test_default_base_url_is_honest_when_loopback(monkeypatch):
    """只绑回环时不能再拼局域网地址：那个地址没人监听。"""
    monkeypatch.delenv("LIVENOTES_BASE_URL", raising=False)
    assert config.HOST in ("127.0.0.1", "localhost", "::1")
    assert config.BASE_URL == "http://127.0.0.1:%d" % config.PORT


def test_base_url_uses_runtime_state_only_when_enabled(isolated_lan, monkeypatch):
    monkeypatch.delenv("LIVENOTES_BASE_URL", raising=False)
    assert lan.base_url() == config.BASE_URL

    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({
        "enabled": True, "port": 8765, "url": "http://192.168.0.246:8765"}),
        encoding="utf-8")
    assert lan.base_url() == "http://192.168.0.246:8765"

    isolated_lan.write_text(json.dumps({"enabled": False, "port": 8765}),
                            encoding="utf-8")
    assert lan.base_url() == config.BASE_URL


def test_env_base_url_wins(isolated_lan, monkeypatch):
    monkeypatch.setenv("LIVENOTES_BASE_URL", "https://notes.example.com/")
    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({
        "enabled": True, "url": "http://192.168.0.246:8765"}), encoding="utf-8")
    assert lan.base_url() == "https://notes.example.com"


def test_link_generators_use_enabled_lan_url(isolated_lan):
    """飞书卡片里的链接不能只改一半：引擎和简报表都要走动态前缀。"""
    from app import live, livebrief

    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({
        "enabled": True, "port": 8765, "url": "http://192.0.2.10:8765"}),
        encoding="utf-8")
    row = {"id": 1, "t_start": 12.0, "t_end": 13.0,
           "speaker_label": "主播", "text": "一句原话"}
    assert live._reader_link("NOPE", 12) == \
        "http://192.0.2.10:8765/reader?session=NOPE&t=12"
    assert livebrief._evidence(row, "NOPE")["reader_url"] == \
        "http://192.0.2.10:8765/reader?session=NOPE&t=12"


def test_autostart_defaults_on_when_feishu_configured(isolated_lan, tmp_path,
                                                      monkeypatch):
    """第一次跑且接了飞书：飞书链接只有手机用，自动开局域网监听。"""
    (tmp_path / "feishu.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(lan, "DATA_DIR", tmp_path)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.setattr(lan, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(lan, "_ensure_firewall", lambda port, elevate: "ok")
    monkeypatch.setattr(lan, "DEFAULT_PORT", 0)
    lan.autostart()
    try:
        st = lan.status()
        assert st["enabled"] is True
        assert st["url"].startswith("http://192.0.2.10:")
        assert lan.base_url() == st["url"]
        saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
        assert saved["enabled"] is True
    finally:
        lan.stop()


def test_autostart_stays_off_without_feishu(isolated_lan, tmp_path, monkeypatch):
    """没接飞书时不能悄悄把无密码服务开到局域网。"""
    monkeypatch.setattr(lan, "DATA_DIR", tmp_path)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    lan.autostart()
    assert lan.status()["enabled"] is False
    assert not isolated_lan.exists()


def test_autostart_respects_explicit_off(isolated_lan, tmp_path, monkeypatch):
    """用户关过一次后，即使接了飞书也不再自动开。"""
    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({"enabled": False, "port": 8765}),
                            encoding="utf-8")
    (tmp_path / "feishu.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(lan, "DATA_DIR", tmp_path)
    lan.autostart()
    assert lan.status()["enabled"] is False


def test_autostart_restores_enabled_state(isolated_lan, monkeypatch):
    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setattr(lan, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(lan, "_ensure_firewall", lambda port, elevate: "ok")
    monkeypatch.setattr(lan, "DEFAULT_PORT", 0)
    monkeypatch.setattr(lan, "HOST", "127.0.0.1")
    lan.autostart()
    try:
        assert lan.status()["enabled"] is True
    finally:
        lan.stop()


def test_autostart_keeps_firewall_declined(isolated_lan, monkeypatch):
    """拒绝过一次管理员授权后，重启也不能再弹 UAC。"""
    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({
        "enabled": True, "firewall_declined": True}), encoding="utf-8")
    monkeypatch.setattr(lan, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(lan, "_ensure_firewall", lambda port, elevate: "missing")
    monkeypatch.setattr(lan, "DEFAULT_PORT", 0)
    monkeypatch.setattr(lan, "HOST", "127.0.0.1")
    lan.autostart()
    try:
        saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
        assert saved["enabled"] is True
        assert saved.get("firewall_declined") is True
    finally:
        lan.stop()


def test_async_firewall_records_decline(isolated_lan, monkeypatch):
    """首次自动开启弹一次 UAC；拒绝后记进状态，下次启动不再打扰。"""
    import time

    monkeypatch.setattr(lan, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(lan, "_ensure_firewall", lambda port, elevate: "ok")
    st = lan.start(port=0)
    try:
        monkeypatch.setattr(lan, "_ensure_firewall",
                            lambda port, elevate: "missing")
        lan._ensure_firewall_async(st["port"])
        saved = {}
        deadline = time.time() + 3
        while time.time() < deadline:
            saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
            if saved.get("firewall_declined"):
                break
            time.sleep(0.02)
        assert saved.get("firewall_declined") is True
    finally:
        lan.stop()


def test_local_only_flags_loopback_prefix(isolated_lan, monkeypatch):
    monkeypatch.delenv("LIVENOTES_BASE_URL", raising=False)
    assert lan.local_only() is True
    isolated_lan.parent.mkdir(parents=True, exist_ok=True)
    isolated_lan.write_text(json.dumps({
        "enabled": True, "port": 8765, "url": "http://192.0.2.10:8765"}),
        encoding="utf-8")
    assert lan.local_only() is False


def test_cached_brief_links_follow_current_prefix(isolated_lan):
    """开/关手机访问后，TTL 内命中的旧简报也不能把 127 链接发给手机。"""
    from app import livebrief

    key = ("S", "summary", (0, 1), 0, livebrief.PROMPT_VERSION, "")
    payload = {"ok": True, "kind": "summary",
               "points": [{"evidence": [
                   {"quote": "x",
                    "reader_url": "http://127.0.0.1:62996/reader?session=S&t=12"}]}]}
    livebrief.reset()
    try:
        livebrief._cache_put(key, payload, 60)
        isolated_lan.parent.mkdir(parents=True, exist_ok=True)
        isolated_lan.write_text(json.dumps({
            "enabled": True, "port": 8765, "url": "http://192.0.2.10:8765"}),
            encoding="utf-8")
        hit = livebrief._cache_get(key)
        assert hit["points"][0]["evidence"][0]["reader_url"] == \
            "http://192.0.2.10:8765/reader?session=S&t=12"
    finally:
        livebrief.reset()


def test_start_and_stop_listener(isolated_lan, monkeypatch):
    """起第二个监听后，同一端口上用回环也能访问；关掉后状态回落。"""
    # URL 用假局域网 IP（只影响拼链接），实际连接走回环验证服务确实起来了。
    monkeypatch.setattr(lan, "lan_ip", lambda: "192.0.2.10")
    monkeypatch.setattr(lan, "_ensure_firewall", lambda port, elevate: "ok")

    st = lan.start(port=0)
    try:
        assert st["enabled"] is True
        assert st["url"].startswith("http://192.0.2.10:")
        port = st["port"]
        assert port > 0
        assert lan.base_url() == st["url"]
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/health" % port, timeout=3) as r:
            assert r.status == 200
        saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
        assert saved["enabled"] is True and saved["port"] == port
    finally:
        st = lan.stop()

    assert st["enabled"] is False
    assert lan.base_url() == config.BASE_URL
    saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
    assert saved["enabled"] is False


def test_start_refuses_without_lan_ip(isolated_lan, monkeypatch):
    monkeypatch.setattr(lan, "lan_ip", lambda: "127.0.0.1")
    with pytest.raises(RuntimeError):
        lan.start(port=0)
    assert lan.status()["enabled"] is False


def test_manual_command_mentions_port_and_profiles():
    cmd = lan.manual_command(8765)
    assert "localport=8765" in cmd
    assert "profile=private,public" in cmd
    assert "netsh" in cmd


def test_firewall_missing_without_elevation(monkeypatch):
    monkeypatch.setattr(lan, "firewall_ok", lambda port: False)
    assert lan._ensure_firewall(8765, elevate=False) == "missing"


def test_status_in_lan_startup_mode_is_read_only(isolated_lan, monkeypatch):
    """--lan 起的主监听已经在 0.0.0.0 上，设置页只展示、不再起第二个。"""
    monkeypatch.setattr(lan, "HOST", "0.0.0.0")
    monkeypatch.setattr(lan, "BASE_URL", "http://192.0.2.10:8765")
    st = lan.status()
    assert st["enabled"] is True
    assert st["managed"] is False
    assert st["url"] == "http://192.0.2.10:8765"
    assert lan.set_enabled(True)["enabled"] is True
    with pytest.raises(RuntimeError):
        lan.set_enabled(False)


def test_lan_startup_state_is_not_carried_into_loopback_run(isolated_lan, monkeypatch):
    """--lan 那次运行写下的地址给 helper 用；下次回环启动必须失效，
    否则用户不加 --lan 也会被悄悄暴露到局域网。"""
    monkeypatch.setattr(lan, "HOST", "0.0.0.0")
    monkeypatch.setattr(lan, "BASE_URL", "http://192.0.2.10:8765")
    monkeypatch.setattr(lan, "PORT", 8765)
    lan.publish_external()
    saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
    assert saved["external"] is True
    assert saved["url"] == "http://192.0.2.10:8765"

    monkeypatch.setattr(lan, "HOST", "127.0.0.1")
    lan.autostart()
    saved = json.loads(isolated_lan.read_text(encoding="utf-8"))
    assert saved["enabled"] is False
    assert "url" not in saved


def test_api_lan_routes(isolated_lan, monkeypatch):
    """GET 读状态、POST 开关：只验证路由和参数，不真的对外监听。"""
    import threading
    import urllib.request

    from app import server

    calls = []
    fake = {"enabled": False, "persisted": False, "url": "", "ip": "192.0.2.10",
            "port": 8765, "firewall": "unknown", "firewall_error": "",
            "manual_command": "netsh ...", "error": ""}
    monkeypatch.setattr(lan, "status", lambda: dict(fake))
    monkeypatch.setattr(lan, "set_enabled",
                        lambda on: calls.append(on) or dict(fake, enabled=bool(on)))

    srv = server.make_server("127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(base + "/api/lan", timeout=3) as r:
            assert json.loads(r.read())["ip"] == "192.0.2.10"
        req = urllib.request.Request(
            base + "/api/lan", data=json.dumps({"enabled": True}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            assert json.loads(r.read())["enabled"] is True
        assert calls == [True]
    finally:
        srv.shutdown()
        srv.server_close()


def test_api_lan_reports_start_error(isolated_lan, monkeypatch):
    import threading
    import urllib.error
    import urllib.request

    from app import server

    def boom(on):
        raise RuntimeError("没有找到局域网 IP")

    monkeypatch.setattr(lan, "set_enabled", boom)
    monkeypatch.setattr(lan, "status", lambda: {
        "enabled": False, "persisted": False, "url": "", "ip": "127.0.0.1",
        "port": 8765, "firewall": "unknown", "firewall_error": "",
        "manual_command": "", "error": ""})

    srv = server.make_server("127.0.0.1", 0)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        req = urllib.request.Request(
            base + "/api/lan", data=b'{"enabled": true}',
            headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 400
        body = json.loads(exc.value.read())
        assert body["error"] == "没有找到局域网 IP"
    finally:
        srv.shutdown()
        srv.server_close()
