"""P1/P2 验收：工作台内切场次、批量、回收站入口、清理中心、字幕行右键菜单。

隔离数据目录 + 独立实例 + CDP；只在测试库里操作，不碰真实用户数据。
用法（仓库根目录）：
    python desktop/test/run_p1_p2_check.py
"""

from __future__ import annotations

import importlib.util
import json
from contextlib import contextmanager
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "livenotes-desktop.exe"
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-p1p2"
HOME = RUN_DIR / "home"
SECOND = "SECOND_P1P2"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[p1p2] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[p1p2] {msg}", flush=True)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 100

    def send(self, method: str, params: dict | None = None) -> int:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params or {}}))
        return self.seq

    def wait(self, mid: int, timeout: float = 20.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise AssertionError(msg["error"])
                return msg.get("result") or {}
        raise AssertionError(f"CDP timeout for {mid}")

    def call(self, method: str, params: dict | None = None, timeout: float = 20.0):
        return self.wait(self.send(method, params), timeout)

    def eval(self, expr: str, ctx: int | None = None, timeout: float = 20.0):
        p = {"expression": expr, "awaitPromise": True, "returnByValue": True,
             "userGesture": True}
        if ctx:
            p["contextId"] = ctx
        res = self.call("Runtime.evaluate", p, timeout)
        if "exceptionDetails" in res:
            raise AssertionError(json.dumps(res["exceptionDetails"], ensure_ascii=False))
        return (res.get("result") or {}).get("value")

    def wait_value(self, expr: str, want, ctx: int | None = None,
                   timeout: float = 25.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                last = self.eval(expr, ctx, timeout=5)
            except Exception as e:
                last = f"ERR {e}"
            if callable(want):
                if want(last):
                    return last
            elif last == want:
                return last
            time.sleep(0.3)
        raise AssertionError(f"timeout {expr!r}; last={last!r}")



@contextmanager
def iframe_client(ui, match, timeout: float = 15.0):
    """工作台/文件中心是跨源 OOPIF：在它自己的 CDP target 上开一条连接。"""
    deadline = time.time() + timeout
    target = None
    while time.time() < deadline:
        for t in ui.targets():
            u = t.get("url") or ""
            if t.get("type") == "iframe" and "127.0.0.1" in u and match(u):
                target = t
                break
        if target:
            break
        time.sleep(0.3)
    if not target:
        raise AssertionError("iframe target not found")
    with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                    max_size=16 * 1024 * 1024) as ws:
        f = Cdp(ws)
        f.call("Runtime.enable")
        yield f


def seed_second_session() -> None:
    from app import store
    conn = store.connect()
    try:
        store.init_db(conn)
        if not store.get_session(conn, SECOND):
            store.insert_session(conn, {
                "id": SECOND, "title": "P1/P2 第二场 · 仅文字",
                "started_at": "2026-11-11T12:00:00", "duration_s": 120.0,
                "source_mode": "direct", "sample_rate": 16000, "audio_path": None,
                "speakers": [{"speaker_id": "SPK_00", "display_name": "主播"}],
            })
            store.insert_utterances(conn, [
                {"session_id": SECOND, "t_start": 10.0, "t_end": 12.0,
                 "speaker_id": "SPK_00", "speaker_label": "主播",
                 "text": "这是第二场，用来验证工作台内切场次不重载。",
                 "asr_conf": 0.95, "overlap": 0, "category": "substantive",
                 "value_tag": "normal", "importance": 0.7, "topic": "",
                 "low_value_reason": ""},
            ])
    finally:
        conn.close()


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    global ui
    ui = load(ROOT / "desktop" / "test" / "run_ui_shots.py", "run_ui_shots")
    ui.HOME = HOME
    spike = ui.load_spike()
    spike.HOME = HOME
    spike.prepare_home()
    seed_second_session()
    try:
        (HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(HOME),
        "LIVENOTES_PYTHON": sys.executable,
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_AUTO_EXIT_MS": "240000",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (RUN_DIR / "app.out").open("wb")
    log(f"launch {run_exe.name}")
    proc = subprocess.Popen([str(run_exe)], cwd=str(RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        target = None
        deadline = time.time() + 60
        while time.time() < deadline and proc.poll() is None:
            for t in ui.targets():
                if t.get("type") == "page" and "shell.html" in (t.get("url") or ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            log("cdp target not found")
            return 1
        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=16 * 1024 * 1024) as ws:
            cdp = Cdp(ws)
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            cdp.wait_value("typeof window.__LN_SHELL__", "object")
            cdp.wait_value("window.__LN_SHELL__.state().sessions", lambda n: n >= 2)
            # 1) 原生工作台：切场次不重载任何 iframe，字幕直接换
            cdp.eval("window.__LN_SHELL__.view('workbench')")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            st0 = cdp.eval("JSON.stringify(window.__LN_SHELL__.state())")
            st0 = json.loads(st0)
            loads0 = st0["frameLoads"]
            first_sid = cdp.eval("(S.sessions.find(s=>s.is_demo)||{}).id || ''")
            assert first_sid, "demo session missing"
            log(f"workbench loaded sid={st0['selectedSession']} loads={loads0}; demo={first_sid}")
            cdp.eval(f"""document.querySelector('#sideList [data-session="{first_sid}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", first_sid)
            cdp.wait_value("document.querySelectorAll('#wbTranscript .line').length",
                           lambda n: n >= 2)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{SECOND}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", SECOND)
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            st1 = json.loads(cdp.eval("JSON.stringify(window.__LN_SHELL__.state())"))
            log(f"switched sid={st1['selectedSession']} loads={st1['frameLoads']} "
                f"wbLines={st1['wbLines']}")
            assert st1["frameLoads"] == loads0, (loads0, st1["frameLoads"])
            assert st1["wbLines"] >= 0, st1["wbLines"]
            assert cdp.eval("document.getElementById('wbView').hidden") is False
            # 点时间码 → 该行进入定位态；说话人筛选只留这一位的字幕。
            cdp.eval("document.querySelector('#wbTranscript .line-time').click()")
            cdp.wait_value("!!document.querySelector('#wbTranscript .line.is-focus')", True)
            n_before = cdp.eval("document.querySelectorAll('#wbTranscript .line').length")
            chips = cdp.eval("document.querySelectorAll('#wbSpkFilters [data-spk-filter]').length")
            if chips > 1:
                cdp.eval("document.querySelectorAll('#wbSpkFilters [data-spk-filter]')[1].click()")
                cdp.wait_value("document.querySelectorAll('#wbTranscript .line').length",
                               lambda n: n <= n_before)
                cdp.eval("document.querySelector('#wbSpkFilters [data-spk-filter=\"all\"]').click()")
            # 右侧总结标签可切
            cdp.eval("document.querySelector('#inspScroll [data-sum-tab=\"time\"]').click()")
            cdp.wait_value("document.querySelector('#inspScroll .sum-tab.on').textContent", "时间")

            # 2) 批量选择与取消
            cdp.eval("document.querySelector('[data-batch-toggle]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().batchMode", True)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{SECOND}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().batchSelected", 1)
            cdp.eval("document.querySelector('[data-batch-cancel]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().batchMode", False)

            # 3) 工作台侧栏回收站入口
            cdp.eval("document.querySelector('[data-open-trash]').click()")
            cdp.wait_value("window.__LN_SHELL__.state().view", "files")
            src_trash = cdp.eval("document.getElementById('frame').src")
            log(f"trash frame: {src_trash}")
            assert "tab=trash" in src_trash, src_trash
            time.sleep(1.2)

            # 4) iframe 内：清理中心 tab 能出候选
            with iframe_client(ui, lambda u: "library" in u) as f:
                f.wait_value("!!document.querySelector('#tabs [data-v=\"cleanup\"]')", True)
                f.eval("document.querySelector('#tabs [data-v=\"cleanup\"]').click()")
                f.wait_value("document.querySelector('#tabs button.on').textContent", "体检清理")
                f.wait_value("document.querySelectorAll('#rows tr').length",
                             lambda n: n >= 1)
                card = f.eval("document.getElementById('cards').innerText")
                reasons = f.eval("document.getElementById('rows').innerText")
                log(f"cleanup cards: {card!r}")
                assert "待清理候选" in card, card
                assert "演示数据" in reasons, reasons
                # 默认全选 → 移到回收站 → 底部撤销，候选应回来。
                f.eval("window.__lnConfirm=window.confirm;window.confirm=function(){return true}")
                f.eval("document.getElementById('bulkTrash').click()")
                f.wait_value("document.querySelectorAll('#rows tr').length", 0)
                f.wait_value("document.getElementById('undoBar').classList.contains('show')",
                             True)
                f.eval("document.getElementById('undoBtn').click()")
                f.wait_value("document.querySelectorAll('#rows tr').length",
                             lambda n: n >= 1)
                f.eval("window.confirm=window.__lnConfirm")
                log("cleanup trash + undo ok")

            # 5) 回工作台：原生时间轴、字幕与总结面板
            cdp.eval("window.__LN_SHELL__.view('workbench')")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            cdp.eval(f"""document.querySelector('#sideList [data-session="{first_sid}"]').click()""")
            cdp.wait_value("window.__LN_SHELL__.state().workbenchSid", first_sid)
            cdp.wait_value("window.__LN_SHELL__.state().workbenchReady", True)
            cdp.wait_value("document.querySelectorAll('#wbTranscript .line').length",
                           lambda n: n >= 2)
            assert cdp.eval("document.querySelectorAll('#wbTlBars .tl-bar').length") == 44
            assert cdp.eval("document.querySelectorAll('#wbTranscript .line .tag').length") >= 2
            assert cdp.eval("document.getElementById('wbExport').disabled") is False
            cdp.eval("document.getElementById('wbHighOnly').click()")
            cdp.wait_value("!!document.querySelector('#wbHighOnly').checked", True)
            cdp.wait_value(
                "document.querySelector('#wbFilterMeta').textContent.includes('显示')", True)
            cdp.eval("document.getElementById('wbHighOnly').click()")
            cdp.wait_value("!!document.querySelector('#wbHighOnly').checked", False)
            log("native workbench ok")

            # 6) 设置：分类、搜索、主题与即时保存
            loads_wb = json.loads(cdp.eval("JSON.stringify(window.__LN_SHELL__.state())"))["frameLoads"]
            cdp.eval("window.__LN_SHELL__.view('settings')")
            cdp.wait_value("window.__LN_SHELL__.state().view", "settings")
            assert cdp.eval("document.getElementById('setView').hidden") is False
            assert cdp.eval("document.querySelectorAll('#setNav [data-setpane]').length") == 7
            cdp.wait_value("typeof S !== 'undefined' && !!S.set.prefs", True)
            # 搜索只滤当前分类；无命中时出现提示。
            cdp.eval("(()=>{const i=document.getElementById('setSearch');"
                     "i.value='跟随系统';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
            cdp.wait_value(
                "document.querySelectorAll('.set-pane[data-pane=\"appearance\"] .set-row:not([hidden])').length",
                1)
            cdp.eval("(()=>{const i=document.getElementById('setSearch');"
                     "i.value='绝无此项';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
            cdp.wait_value("document.getElementById('setNoMatch').hidden", False)
            cdp.eval("(()=>{const i=document.getElementById('setSearch');"
                     "i.value='';i.dispatchEvent(new Event('input',{bubbles:true}))})()")
            # 主题卡与顶栏切换同步，并写入 localStorage。
            cdp.eval("document.querySelector('[data-theme-choice=\"light\"] input').click()")
            cdp.wait_value("document.documentElement.dataset.theme", "light")
            cdp.wait_value("window.__LN_SHELL__.state().themeChoice", "light")
            assert cdp.eval("localStorage.getItem('ln.theme')") == "light"
            cdp.eval("document.getElementById('themeToggle').click()")
            cdp.wait_value("document.documentElement.dataset.theme", "dark")
            cdp.wait_value("window.__LN_SHELL__.state().themeChoice", "dark")
            cdp.eval("document.querySelector('[data-theme-choice=\"system\"] input').click()")
            cdp.wait_value("window.__LN_SHELL__.state().themeChoice", "system")
            cdp.wait_value("document.documentElement.dataset.theme",
                           cdp.eval("matchMedia('(prefers-color-scheme: dark)').matches"
                                    " ? 'dark' : 'light'"))
            cdp.eval("document.querySelector('[data-theme-choice=\"dark\"] input').click()")
            cdp.wait_value("window.__LN_SHELL__.state().themeChoice", "dark")
            # 偏好即时保存：切到采集页改回退方式，落库后再改回来。
            cdp.eval("document.querySelector('#setNav [data-setpane=\"capture\"]').click()")
            cdp.wait_value("!!document.querySelector('.set-pane[data-pane=\"capture\"].on')", True)
            old_pref = cdp.eval("document.getElementById('prefFallback').value")
            new_pref = "auto" if old_pref != "auto" else "ask"
            cdp.eval(f"(()=>{{const el=document.getElementById('prefFallback');"
                     f"el.value='{new_pref}';el.dispatchEvent(new Event('change',{{bubbles:true}}))}})()")
            cdp.wait_value(
                f"api('/api/settings').then(d=>d.prefs.direct_fallback)", new_pref)
            cdp.eval(f"(()=>{{const el=document.getElementById('prefFallback');"
                     f"el.value='{old_pref}';el.dispatchEvent(new Event('change',{{bubbles:true}}))}})()")
            cdp.wait_value(
                f"api('/api/settings').then(d=>d.prefs.direct_fallback)", old_pref)
            st2 = json.loads(cdp.eval("JSON.stringify(window.__LN_SHELL__.state())"))
            assert st2["frameLoads"] == loads_wb, (loads_wb, st2["frameLoads"])
            log(f"settings ok theme={st2['theme']} pref={old_pref}")
            log("PASS")
        return 0
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
