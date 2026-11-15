"""HUD 验收：启动隔离桌面壳 + 真实 ASR 会话，截记录卡 / 通知 / 收尾三屏。

和 run_ui_shots.py 的区别：这里不只切视图，还会通过引擎 API 真正建一路
LiveSession、按 16k PCM 喂桌面里的 speech.wav，让 HUD 显示真实计时、
逐字稿和说话人；随后验证暂停、通知抽屉和「结束并生成笔记」。

用法（仓库根目录）：
    python -I desktop/test/run_hud_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
EXE = ROOT / "desktop" / "src-tauri" / "target" / "debug" / "livenotes-desktop.exe"
RUN_DIR = Path(os.environ.get("LOCALAPPDATA", str(ROOT))) / "Temp" / "livenotes-hud"
HOME = RUN_DIR / "home"
DATA = HOME / "data"
OUT = ROOT / "docs" / "prototypes" / "shots" / "hud-check"
SPEECH = ROOT / "desktop" / "test" / "speech.wav"

try:
    from websockets.sync.client import connect as ws_connect
except Exception as e:  # pragma: no cover
    print(f"[hud] websockets unavailable: {e}")
    sys.exit(2)


def log(msg: str) -> None:
    print(f"[hud] {msg}", flush=True)


FAILS: list[str] = []


def check(name: str, ok: bool, detail=None) -> bool:
    log(("PASS " if ok else "FAIL ") + name
        + ("" if detail is None else " · " + str(detail)))
    if not ok:
        FAILS.append(name)
    return bool(ok)


# ---------------------------------------------------------------- CDP
def devtools_port() -> int:
    f = HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort"
    try:
        return int(f.read_text(encoding="utf-8").splitlines()[0])
    except Exception:
        return 0


def targets() -> list[dict]:
    port = devtools_port()
    if not port:
        return []
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return []


def cdp_eval(ws, expr: str, timeout: float = 20.0):
    mid = cdp_eval.seq = getattr(cdp_eval, "seq", 100) + 1
    ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "awaitPromise": True,
                                   "returnByValue": True, "userGesture": True}}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = json.loads(ws.recv(timeout=timeout))
        if msg.get("id") == mid:
            res = msg.get("result", {})
            if "exceptionDetails" in res:
                return {"error": str(res["exceptionDetails"].get("text"))}
            return (res.get("result") or {}).get("value")
    return None


# ---------------------------------------------------------------- engine
def engine_url() -> str:
    path = DATA / "runtime" / "engine.json"
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            port = int(info.get("port") or 0)
            if port:
                return f"http://127.0.0.1:{port}"
        except Exception:
            pass
        time.sleep(0.4)
    raise RuntimeError("engine.json not found")


def api(base: str, path: str, body: dict | None = None, *, method: str = ""):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data,
                                 method=method or ("POST" if body is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{path} -> {e.code}: {raw[:300]}") from None


def pcm_16k(path: Path):
    """读 wav 并线性重采样成 16k / s16le mono。"""
    import numpy as np
    with wave.open(str(path), "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError(f"need 16-bit wav, got {width * 8} bit")
    x = np.frombuffer(raw, dtype="<i2").astype("float32")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        n = int(round(len(x) * 16000 / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype("<i2").tobytes()


def post_chunk(base: str, sid: str, pcm: bytes) -> None:
    req = urllib.request.Request(base + "/api/live/chunk", data=pcm, method="POST")
    req.add_header("X-Live-Id", sid)
    req.add_header("Content-Type", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


def start_feeder(base: str, sid: str, stop: threading.Event) -> threading.Thread:
    """按真实速度循环喂 speech.wav，保持看门狗认为链路还活着。"""
    pcm = pcm_16k(SPEECH)
    step = 3200 * 5                     # 0.1s

    def run():
        pos = 0
        while not stop.is_set():
            chunk = pcm[pos:pos + step]
            if not chunk:
                pos = 0
                continue
            try:
                post_chunk(base, sid, chunk)
            except Exception:
                if stop.is_set():
                    return
                time.sleep(0.2)
                continue
            pos += step
            stop.wait(0.1)

    th = threading.Thread(target=run, name="hud-feeder", daemon=True)
    th.start()
    return th


def wait_live(base: str, sid: str, min_utts: int = 0, timeout: float = 45) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            d = api(base, "/api/live/list")
            for s in d.get("sessions") or []:
                if s.get("id") == sid:
                    last = s
                    if int(s.get("n_utterances") or 0) >= min_utts:
                        return s
        except Exception:
            pass
        time.sleep(0.5)
    return last


# ---------------------------------------------------------------- shots
def shot(pid: int, name: str) -> bool:
    sys.path.insert(0, str(ROOT / "desktop" / "test"))
    try:
        import win_shot
    except Exception as e:
        log(f"shot helper unavailable: {e}")
        return False
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        r = win_shot.capture(pid, OUT / name, "LiveNotes")
        log(f"shot {name} {r}")
        return True
    except Exception as e:
        log(f"shot {name} failed: {e}")
        return False


def dump_state(ws, tag: str) -> dict:
    raw = cdp_eval(ws, "JSON.stringify(window.__LN_SHELL__ && window.__LN_SHELL__.state())")
    try:
        st = json.loads(raw) if raw else {}
    except Exception:
        st = {"raw": raw}
    log(f"{tag} state={json.dumps(st, ensure_ascii=False)}")
    return st


def hud_text(ws) -> dict:
    expr = """JSON.stringify({
      state: document.getElementById('hudState')?.innerText || '',
      room: document.getElementById('hudRoom')?.textContent || '',
      timer: document.getElementById('hudTimer')?.textContent || '',
      stats: document.getElementById('hudStats')?.textContent || '',
      pause: document.getElementById('hudPause')?.textContent || '',
      lines: document.querySelectorAll('#hudLines .hud-line').length,
      partial: document.getElementById('hudPartial')?.textContent || '',
      finish: !document.getElementById('finishCard')?.hidden,
      finishTitle: document.getElementById('fcTitle')?.textContent || '',
      finishSteps: Array.from(document.querySelectorAll('#fcSteps li')).map(function(li){
        return li.className + ':' + (li.querySelector('span')?.textContent || '');
      }),
      notifOpen: !document.getElementById('notifDrawer')?.hidden,
      notifItems: document.querySelectorAll('#notifList .notif-item').length,
      notices: document.querySelectorAll('#noticeStack .notice').length
    })"""
    raw = cdp_eval(ws, expr)
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {"raw": raw}


def main() -> int:
    if not EXE.exists():
        log(f"missing exe: {EXE}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    HOME.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    run_exe = RUN_DIR / EXE.name
    shutil.copy2(EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(HOME),
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_RESUME_ROOMS": "1",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "1",
        "LIVENOTES_AUTO_EXIT_MS": "240000",
        "LIVENOTES_PYTHON": sys.executable,
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (RUN_DIR / "app.out").open("wb")
    log(f"launch {run_exe.name}")
    proc = subprocess.Popen([str(run_exe)], cwd=str(RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    feeder_stop = threading.Event()
    base = ""
    sid = ""
    rid = ""
    try:
        target = None
        deadline = time.time() + 90
        while time.time() < deadline and proc.poll() is None:
            for t in targets():
                if t.get("type") == "page" and "shell.html" in (t.get("url") or ""):
                    target = t
                    break
            if target:
                break
            time.sleep(0.5)
        if not target:
            log("cdp target not found")
            return 1
        base = engine_url()
        log(f"engine {base}")
        log(f"cdp {target.get('url')}")

        with ws_connect(target["webSocketDebuggerUrl"], open_timeout=10,
                        max_size=8 * 1024 * 1024) as ws:
            ws.send(json.dumps({"id": 0, "method": "Runtime.enable"}))
            for _ in range(80):
                if cdp_eval(ws, "typeof window.__LN_SHELL__", timeout=5) == "object":
                    break
                time.sleep(0.25)
            time.sleep(1.0)

            # 建房间 + 真会话；先置 live，HUD 才会把它当成正在记录。
            room = api(base, "/api/rooms", {"url": "https://live.douyin.com/908070605",
                                            "name": "深夜情感连麦 · 阿哲"})["room"]
            rid = room["id"]
            started = api(base, "/api/live/start", {
                "title": "深夜情感连麦 · 阿哲", "room_id": rid, "source_mode": "tab"})
            sid = started["session_id"]
            log(f"room={rid} session={sid}")
            api(base, "/api/rooms/state", {"id": rid, "state": "live", "stage": 6,
                                           "session_id": sid})
            feeder = start_feeder(base, sid, feeder_stop)

            s = wait_live(base, sid, min_utts=1, timeout=45)
            log(f"live after first utterance: {json.dumps(s, ensure_ascii=False)[:500]}")

            cdp_eval(ws, "window.__LN_SHELL__.view('hud')")
            time.sleep(2.5)
            dump_state(ws, "hud")
            first = hud_text(ws)
            log("hud text " + json.dumps(first, ensure_ascii=False))
            check("进入记录卡", cdp_eval(
                ws, "window.__LN_SHELL__.state().view") == "hud")
            check("记录中状态", first.get("state") == "正在记录")
            check("显示直播间名", first.get("room") == "深夜情感连麦 · 阿哲")
            check("计时在走", first.get("timer") not in ("", "00:00:00"),
                  first.get("timer"))
            check("最近字幕已出现", int(first.get("lines") or 0) >= 1,
                  first.get("lines"))
            shot(proc.pid, "01-recording.png")

            # 暂停：走界面按钮，验证计时冻结、按钮变「继续」。
            cdp_eval(ws, "document.getElementById('hudPause').click()")
            time.sleep(2.6)
            paused = hud_text(ws)
            log("paused " + json.dumps(paused, ensure_ascii=False))
            check("暂停状态", paused.get("state") == "已暂停")
            check("暂停按钮变继续", paused.get("pause") == "继续")
            shot(proc.pid, "02-paused.png")
            time.sleep(1.2)
            paused2 = hud_text(ws)
            log("paused+1.2s timer=" + str(paused2.get("timer")))
            check("暂停后计时冻结", paused2.get("timer") == paused.get("timer"),
                  paused.get("timer"))

            cdp_eval(ws, "document.getElementById('hudPause').click()")
            time.sleep(2.4)
            resumed = hud_text(ws)
            log("resumed " + json.dumps(resumed, ensure_ascii=False))
            check("恢复记录", resumed.get("state") == "正在记录"
                  and resumed.get("pause") == "暂停")

            # 通知：发一条测试通知 -> 应用内提示 + 徽标；再打开抽屉。
            api(base, "/api/notifications/test", {})
            # 壳每 4 秒轮询一次；至少跨过一拍再断言。
            time.sleep(5.6)
            notif_state = dump_state(ws, "notif")
            notif = hud_text(ws)
            log("notif text " + json.dumps(notif, ensure_ascii=False))
            check("未读徽标计数", int(notif_state.get("notifUnread") or 0) >= 1)
            check("前台出现应用内提示", int(notif.get("notices") or 0) >= 1)
            shot(proc.pid, "03-notice.png")
            cdp_eval(ws, "document.getElementById('notifBtn').click()")
            time.sleep(0.9)
            drawer = hud_text(ws)
            log("drawer " + json.dumps(drawer, ensure_ascii=False))
            check("通知抽屉打开", drawer.get("notifOpen") is True)
            check("抽屉里有通知", int(drawer.get("notifItems") or 0) >= 1)
            shot(proc.pid, "04-drawer.png")
            # 通知设置：带总开关、系统通知、声音和九类事件。
            cdp_eval(ws, "document.querySelector('#notifPrefs summary').click()")
            time.sleep(0.6)
            shot(proc.pid, "05-drawer-prefs.png")
            prefs_text = str(cdp_eval(
                ws, "(document.getElementById('npBody')||{}).innerText || ''"))
            log("prefs " + prefs_text)
            check("通知设置可展开", "允许通知" in prefs_text
                  and "发一条测试通知" in prefs_text)
            cdp_eval(ws, "document.getElementById('notifClose').click()")
            time.sleep(0.4)

            # 收尾：先弹确认，再走真实 close -> finalize -> summary 任务。
            cdp_eval(ws, "document.getElementById('hudFinish').click()")
            time.sleep(0.8)
            shot(proc.pid, "06-finish-confirm.png")
            cdp_eval(ws, "document.querySelector('[data-mfinish]')?.click()")
            time.sleep(1.0)
            log("finish start " + json.dumps(hud_text(ws), ensure_ascii=False))
            shot(proc.pid, "07-finishing.png")

            deadline = time.time() + 75
            last = {}
            while time.time() < deadline:
                last = hud_text(ws)
                log("finish tick " + json.dumps(
                    {k: last.get(k) for k in ("finishTitle", "finishSteps")},
                    ensure_ascii=False))
                if any(x.startswith(("done", "fail")) for x in last.get("finishSteps") or []):
                    if last.get("finishTitle") in ("笔记已生成", "笔记没生成成功"):
                        break
                time.sleep(2.0)
            dump_state(ws, "finish")
            shot(proc.pid, "08-finish-final.png")
            log("finish final " + json.dumps(last, ensure_ascii=False))
            check("收尾完成卡", last.get("finishTitle") == "笔记已生成")
            check("收尾三步全完成", all(
                str(x).startswith("done:") for x in (last.get("finishSteps") or [])),
                last.get("finishSteps"))
            check("完成按钮切开为主操作", cdp_eval(
                ws, "document.getElementById('fcOpen').classList.contains('primary')"
            ) is True)

            # 通知中心里应该已经有 note_ready / note_failed。
            api(base, "/api/notifications/test", {})
            time.sleep(2.4)
            cdp_eval(ws, "document.getElementById('notifBtn').click()")
            time.sleep(0.8)
            shot(proc.pid, "09-drawer-final.png")
            feed = api(base, "/api/notifications")
            kinds = [n.get("kind") for n in (feed.get("items") or [])]
            log("notifications " + json.dumps(
                [{"kind": n.get("kind"), "title": n.get("title"),
                  "read": n.get("read")} for n in (feed.get("items") or [])],
                ensure_ascii=False))
            check("笔记完成通知已落库", "note_ready" in kinds)

            # 「再记一场」应回到空态，而不是被刚停下的房间重新拉回收尾卡。
            cdp_eval(ws, "document.getElementById('notifClose').click()")
            time.sleep(0.3)
            cdp_eval(ws, "document.getElementById('fcLater').click()")
            cdp_eval(ws, "window.__LN_SHELL__.view('hud')")
            time.sleep(1.2)
            idle_raw = cdp_eval(ws, """JSON.stringify({
              view: window.__LN_SHELL__.state().view,
              finish: !document.getElementById('finishCard').hidden,
              idle: !document.getElementById('hudIdle').hidden,
              card: !document.getElementById('hudCard').hidden
            })""")
            try:
                idle = json.loads(idle_raw) if idle_raw else {}
            except Exception:
                idle = {}
            log("after dismiss " + str(idle))
            check("收尾后能回到记录卡空态", idle.get("view") == "hud"
                  and idle.get("idle") is True and idle.get("finish") is False)
            shot(proc.pid, "10-hud-idle.png")

            # 再开一场，验证记录中关窗：不询问、直接收托盘、录音继续。
            api(base, "/api/rooms/state", {"id": rid, "state": "pending", "stage": 0})
            started2 = api(base, "/api/live/start", {
                "title": "深夜情感连麦 · 阿哲", "room_id": rid, "source_mode": "tab"})
            sid = started2["session_id"]
            api(base, "/api/rooms/state", {"id": rid, "state": "live", "stage": 6,
                                           "session_id": sid})
            start_feeder(base, sid, feeder_stop)
            s2 = wait_live(base, sid, min_utts=1, timeout=45)
            log(f"second session {sid} utts={s2.get('n_utterances')}")
            cdp_eval(ws, "window.__LN_SHELL__.view('hud')")
            time.sleep(1.5)
            cdp_eval(ws, "window.__TAURI__.window.getCurrentWindow().close()")
            time.sleep(1.5)
            log("close second " + json.dumps(hud_text(ws), ensure_ascii=False))
            # 首次收托盘会延迟 3.2 秒并弹一次说明；等够再判断窗口是否真的消失。
            time.sleep(4.0)
            sys.path.insert(0, str(ROOT / "desktop" / "test"))
            import win_shot
            visible = bool(win_shot._windows_for_pid(proc.pid, "LiveNotes"))
            before = wait_live(base, sid, min_utts=0, timeout=3)
            log(f"tray hidden visible_window={visible} process_alive={proc.poll() is None} "
                f"live={json.dumps(before, ensure_ascii=False)[:220]}")
            check("关窗后主窗口收起", visible is False)
            check("关窗后进程还在", proc.poll() is None)
            check("关窗后仍在记录", bool(before.get("running")) is True)
            # 收起状态下的新通知应改走系统 toast：壳会记录最后一次投递结果。
            test_item = api(base, "/api/notifications/test", {}).get("item") or {}
            test_id = int(test_item.get("id") or 0)
            # 隐藏窗口的定时器可能被 WebView 节流；多等两拍再判断。
            time.sleep(8.0)
            hidden_state = {}
            try:
                hidden_state = json.loads(cdp_eval(
                    ws, "JSON.stringify(window.__LN_SHELL__.state())") or "{}")
            except Exception:
                pass
            last = hidden_state.get("notifLastPresent") or {}
            last_os = hidden_state.get("notifLastOs") or {}
            log("system notify last=" + json.dumps(last, ensure_ascii=False)
                + " os=" + json.dumps(last_os, ensure_ascii=False)
                + " test_id=" + str(test_id))
            check("隐藏时判定为后台", last.get("foreground") is False)
            check("系统通知带同一条通知 id", int(last_os.get("id") or 0) == test_id)
            check("系统通知已交给 Rust", last_os.get("result") == "sent")
            log("hidden notif state " + json.dumps(hidden_state, ensure_ascii=False))
            time.sleep(2.2)
            after = wait_live(base, sid, min_utts=0, timeout=3)
            log(f"recording in tray before={before.get('n_utterances')} "
                f"after={after.get('n_utterances')} running={after.get('running')}")
            check("收起后逐字稿继续增长",
                  int(after.get("n_utterances") or 0) > int(before.get("n_utterances") or 0),
                  f"{before.get('n_utterances')} -> {after.get('n_utterances')}")
            tray_feed = api(base, "/api/notifications")
            kinds = [n.get("kind") for n in (tray_feed.get("items") or [])]
            log("tray notifications " + json.dumps(kinds, ensure_ascii=False)
                + " tray_hint_count=" + str(kinds.count("tray_hint")))
            check("首次收起提示只发一次", kinds.count("tray_hint") == 1)
        log(f"RESULT {'PASS' if not FAILS else 'FAIL'} failures={FAILS}")
        return 1 if FAILS else 0
    finally:
        feeder_stop.set()
        try:
            if base and sid:
                api(base, "/api/live/stop", {"session_id": sid})
        except Exception:
            pass
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
