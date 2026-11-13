# -*- coding: utf-8 -*-
"""局域网访问：让同一 Wi-Fi 下的手机打开飞书卡片里的回听链接。

桌面壳把引擎绑在 127.0.0.1 的临时端口上（不暴露、也不触发防火墙），
但飞书卡片是给手机看的。这里在开启后另起一个监听：

    0.0.0.0:<port>  →  复用同一个 Handler，只读同一份数据库

主监听不动，桌面壳、本机采集、getDisplayMedia 都不受影响。状态落在
data/runtime/lan.json，引擎重启后自动恢复；飞书机器人是独立进程，也读
这个文件来生成链接，所以开启后不需要重启它。

默认值：接上飞书机器人（有 feishu.json / FEISHU_APP_ID）时，首次启动就
自动开启——飞书卡片只会在手机上打开，回环链接对它是死链。用户显式关掉
之后会写进状态文件，之后不再自动开；没有飞书的环境仍然只监听回环。

安全边界：服务本身没有登录，开启后同网段设备都能打开。这是 docs/15、
docs/18 里已经接受的取舍；界面上必须写清楚，默认只在接上飞书时自动开，
用户随时可以关掉。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .config import BASE_URL, DATA_DIR, HOST, PORT, lan_ip

DEFAULT_PORT = int(os.environ.get("LIVENOTES_LAN_PORT", "8765"))
_STATE_PATH = DATA_DIR / "runtime" / "lan.json"

_LOCK = threading.RLock()
_SRV = None
_THREAD: threading.Thread | None = None
_INFO: dict[str, Any] = {}
_FIREWALL = "unknown"          # ok / missing / unknown
_FIREWALL_ERROR = ""
_LAST_ERROR = ""

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _read_state() -> dict[str, Any]:
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(info: dict[str, Any]) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        # Windows 上目标文件正被读（飞书机器人每次拼链接都会读）时，
        # os.replace 会抛 PermissionError。重试几次就够了，不能把状态悄悄丢掉。
        for attempt in range(6):
            try:
                os.replace(tmp, _STATE_PATH)
                return
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(0.02)
    except OSError:
        # 状态写不下去不影响本次监听；只是重启后要重新开。
        pass


def feishu_configured() -> bool:
    """飞书机器人接上了吗？接上就说明链接会被手机打开，按默认开处理。"""
    if os.environ.get("LIVENOTES_NO_FEISHU", "").lower() in ("1", "true", "yes", "on"):
        return False
    if os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"):
        return True
    try:
        return (DATA_DIR / "feishu.json").is_file()
    except OSError:
        return False


def local_only() -> bool:
    """当前前缀是不是只能在电脑上打开（没有可用的局域网监听）。"""
    host = (urlsplit(base_url()).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def base_url() -> str:
    """生成给手机看的链接前缀。

    优先级：显式 LIVENOTES_BASE_URL > 服务本身监听在可达地址（--lan）>
    运行时开启的局域网监听 > 回环。回环地址只能在电脑上打开，但至少
    不会把「看起来能连、实际没人监听」的局域网地址发出去。
    """
    env = os.environ.get("LIVENOTES_BASE_URL")
    if env:
        return env.rstrip("/")
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        return BASE_URL.rstrip("/")
    state = _read_state()
    if state.get("enabled") and state.get("url"):
        return str(state["url"]).rstrip("/")
    return BASE_URL.rstrip("/")


def manual_command(port: int) -> str:
    """防火墙没放行时，给用户一条能直接粘贴的管理员命令。"""
    name = _rule_name(port)
    return ('netsh advfirewall firewall add rule name="%s" dir=in action=allow '
            'protocol=TCP localport=%d profile=private,public' % (name, port))


def _rule_name(port: int) -> str:
    return "LiveNotes LAN %d" % port


def firewall_ok(port: int) -> bool:
    """只查「我们建过的那条入站规则」；查不到不等于一定被拦，
    但状态页据此提示用户手动放行。"""
    if os.name != "nt":
        return True
    try:
        r = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             "name=%s" % _rule_name(port)],
            capture_output=True, timeout=6,
            creationflags=_CREATE_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _ensure_firewall(port: int, elevate: bool) -> str:
    """返回 ok / missing。只有用户点开关时才弹 UAC；开机自动恢复不弹。"""
    global _FIREWALL_ERROR
    if firewall_ok(port):
        return "ok"
    # 无交互环境（CI、服务账号）里弹不出 UAC，直接返回手动命令。
    if not elevate or os.name != "nt" or _env_on("LIVENOTES_LAN_NO_ELEVATE"):
        return "missing"
    # 端口是整数，规则名由固定前缀拼成，不存在注入面。用 netsh 而不是
    # New-NetFirewallRule：后者在旧系统上可能没加载模块。
    arg = ('advfirewall firewall add rule name="%s" dir=in action=allow '
           'protocol=TCP localport=%d profile=private,public'
           % (_rule_name(port), port))
    script = (
        "$ErrorActionPreference='Stop';"
        "$p = Start-Process -FilePath netsh.exe -Verb RunAs -PassThru -Wait "
        "-WindowStyle Hidden -ArgumentList '%s';"
        "exit $p.ExitCode" % arg.replace("'", "''")
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, timeout=90,
            creationflags=_CREATE_NO_WINDOW)
        if r.returncode != 0:
            _FIREWALL_ERROR = "添加防火墙规则失败，可能取消了管理员授权"
    except (OSError, subprocess.SubprocessError) as e:
        _FIREWALL_ERROR = "添加防火墙规则失败：%s" % e
    return "ok" if firewall_ok(port) else "missing"


def _pick_port(preferred: int) -> int:
    """优先用固定端口（链接稳定）；被别的程序占了就退回临时端口。"""
    candidates = [preferred] if preferred else []
    candidates.append(0)
    last: OSError | None = None
    for p in candidates:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("0.0.0.0", p))
                return int(s.getsockname()[1])
            finally:
                s.close()
        except OSError as e:
            last = e
    raise RuntimeError("找不到可用的局域网端口：%s" % last)


def _start_server(port: int):
    """在 0.0.0.0 上起第二个 HTTP 服务；端口被占时换一个。"""
    from . import server as server_mod

    try:
        srv = server_mod.make_server("0.0.0.0", port)
        return srv, int(srv.server_address[1])
    except SystemExit:
        alt = _pick_port(0)
        srv = server_mod.make_server("0.0.0.0", alt)
        return srv, int(srv.server_address[1])


def start(port: int | None = None, elevate: bool = False) -> dict[str, Any]:
    global _SRV, _THREAD, _INFO, _FIREWALL, _LAST_ERROR
    with _LOCK:
        if _SRV is not None:
            return status()
        ip = lan_ip()
        if ip.startswith(("127.", "169.254.")):
            raise RuntimeError("没有找到局域网 IP：先连上 Wi-Fi / 网线再开启")
        preferred = DEFAULT_PORT if port is None else int(port)
        try:
            srv, actual = _start_server(preferred)
        except (Exception, SystemExit) as e:
            _LAST_ERROR = str(e)
            raise RuntimeError(str(e)) from e
        th = threading.Thread(target=srv.serve_forever, name="lan-http", daemon=True)
        th.start()
        _SRV, _THREAD = srv, th
        _LAST_ERROR = ""
        _INFO = {
            "enabled": True,
            "port": actual,
            "url": "http://%s:%d" % (ip, actual),
            "pid": os.getpid(),
        }
        _write_state(_INFO)
        _FIREWALL = _ensure_firewall(actual, elevate=elevate)
        if elevate and _FIREWALL == "missing":
            # 用户拒绝了管理员授权：记下来，下次启动不再弹。
            _INFO["firewall_declined"] = True
            _write_state(_INFO)
        return status()


def _shutdown(srv) -> None:
    try:
        srv.shutdown()
    finally:
        srv.server_close()


def stop() -> dict[str, Any]:
    global _SRV, _THREAD, _FIREWALL
    with _LOCK:
        srv = _SRV
        _SRV = None
        _THREAD = None
        if srv is not None:
            # 在别的线程里关：当前请求很可能正跑在这个服务上。
            threading.Thread(target=_shutdown, args=(srv,),
                             name="lan-http-stop", daemon=True).start()
        _FIREWALL = "unknown"
        info = dict(_INFO) if _INFO else _read_state()
        info.update({"enabled": False, "pid": os.getpid()})
        info.pop("url", None)
        _INFO.clear()
        _INFO.update(info)
        _write_state(info)
        return status()


def publish_external() -> None:
    """--lan 启动：主监听本身就是局域网地址。

    飞书机器人等 helper 是独立进程，HOST 还是默认回环；把真实地址写进
    状态文件，它们生成链接时才不会退回 127.0.0.1。
    """
    if HOST in ("127.0.0.1", "localhost", "::1"):
        return
    _write_state({
        "enabled": True,
        "external": True,
        "port": PORT,
        "url": BASE_URL.rstrip("/"),
        "pid": os.getpid(),
    })


def _ensure_firewall_async(port: int) -> None:
    """首次自动开启时在后台补防火墙规则。

    弹 UAC 最长会等 90 秒，不能放在 autostart 里同步跑：那时主服务还没
    serve_forever，桌面壳的健康检查会超时、把引擎当坏进程杀掉。放后台后
    监听已经能响应，规则加不加得上只决定手机能不能连进来。用户拒绝授权
    就记进状态，下次启动不再弹。
    """
    def run() -> None:
        global _FIREWALL
        result = _ensure_firewall(port, elevate=True)
        with _LOCK:
            _FIREWALL = result
            if _SRV is None:
                return
            info = dict(_INFO)
            if result == "missing":
                info["firewall_declined"] = True
            _INFO.clear()
            _INFO.update(info)
            _write_state(info)

    threading.Thread(target=run, name="lan-firewall", daemon=True).start()


def autostart() -> None:
    """引擎启动时恢复上次的开启状态。失败只记录，不影响主服务。

    没有状态文件（这台机器第一次跑）时，接上飞书就默认开：飞书卡片里的
    回听链接只会在手机上打开，回环地址对它是死链。用户关过一次后会留下
    `enabled: false`，之后照办，不再自动开。
    """
    global _LAST_ERROR
    state = _read_state()
    if state.get("external"):
        # 上一次是 --lan 起的，本次是回环启动：不能把「那次开放」当成
        # 用户这次的意愿，否则不加 --lan 也会悄悄暴露到局域网。
        _write_state({"enabled": False, "pid": os.getpid()})
        return
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        return                      # --lan 已经直接监听 0.0.0.0
    if state:
        if not state.get("enabled"):
            return                  # 用户显式关过：尊重这个选择
        port = int(state.get("port") or DEFAULT_PORT)
    else:
        if not feishu_configured():
            return                  # 没有飞书：保持只监听本机
        port = DEFAULT_PORT
    declined = bool(state.get("firewall_declined"))
    # 先把旧的 url 清掉：上一次进程可能没退干净，不能让飞书机器人拿到
    # 一个「文件里写着开启、实际没监听」的地址。
    initial = {"enabled": True, "port": port, "pid": os.getpid()}
    if declined:
        initial["firewall_declined"] = True
    _write_state(initial)
    try:
        st = start(port=port, elevate=False)
    except Exception as e:
        _LAST_ERROR = str(e)
        return
    if declined:
        # 上次拒绝过管理员授权，这次别再弹；标记要留着，否则下次启动又弹。
        with _LOCK:
            info = dict(_INFO, firewall_declined=True)
            _INFO.clear()
            _INFO.update(info)
            _write_state(info)
        return
    # 没有放行规则时手机连不进来（Windows 防火墙按 exe 路径放行）。第一次
    # 自动开启补一次规则；用户拒绝过就不再打扰，留给设置页里手动开关。
    if st.get("firewall") == "missing":
        _ensure_firewall_async(int(st.get("port") or port))


def status() -> dict[str, Any]:
    global _FIREWALL
    with _LOCK:
        # --lan 启动时主监听本身就是 0.0.0.0，没有第二个监听可开关。
        if HOST not in ("127.0.0.1", "localhost", "::1"):
            return {
                "enabled": True,
                "managed": False,
                "persisted": True,
                "url": BASE_URL.rstrip("/"),
                "ip": lan_ip(),
                "port": PORT,
                "firewall": "unknown",
                "firewall_error": "",
                "manual_command": manual_command(PORT),
                "error": "",
            }
        running = _SRV is not None
        info = dict(_INFO) if _INFO else _read_state()
        port = int(info.get("port") or DEFAULT_PORT)
        if running and _FIREWALL == "missing":
            _FIREWALL = "ok" if firewall_ok(port) else "missing"
        return {
            "enabled": running,
            "managed": True,
            "persisted": bool(info.get("enabled")),
            "url": str(info.get("url") or "") if running else "",
            "ip": lan_ip(),
            "port": port,
            "firewall": _FIREWALL if running else "unknown",
            "firewall_error": _FIREWALL_ERROR,
            "manual_command": manual_command(port),
            "error": _LAST_ERROR,
        }


def set_enabled(enabled: bool) -> dict[str, Any]:
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        if enabled:
            return status()
        raise RuntimeError("当前以 --lan 启动，局域网已经开放；"
                           "要关闭请重启服务并去掉 --lan")
    return start(elevate=True) if enabled else stop()


def reset_for_tests(state_path=None) -> None:
    """测试用：停监听、清内存状态。生产代码不要调用。"""
    global _STATE_PATH, _INFO, _FIREWALL, _LAST_ERROR
    if state_path is not None:
        _STATE_PATH = state_path
    stop()
    _INFO = {}
    _FIREWALL = "unknown"
    _LAST_ERROR = ""
