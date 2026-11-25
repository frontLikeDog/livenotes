# -*- coding: utf-8 -*-
"""全局配置。路径全部基于项目根目录。"""
from __future__ import annotations

import os
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
WEB_DIR = APP_DIR / "web"
# 压测/自测可以把运行数据放到临时目录，避免污染真实场次
DATA_DIR = Path(os.environ.get("LIVENOTES_DATA_DIR", str(ROOT / "data")))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "livenotes.db"

HOST = os.environ.get("LIVENOTES_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVENOTES_PORT", "8765"))
# 服务重启后要不要自动接着采上次没结束的房间。默认关：进程退出时残留的
# pending/starting/live 只是「上次进程还活着」的化石状态，自动拉起会在用户
# 不知情时重开直播间。无人值守的机器可以显式设 1。
RESUME_ROOMS = os.environ.get("LIVENOTES_RESUME_ROOMS", "0").lower() in ("1", "true", "yes")
# 仅采集模式：不加载 ASR/声纹模型，不做实时字幕、说话人和总结，只把音频落盘。
# 面向 1~2GB 的 VPS 录制节点。逐场可用 /api/live/start 的 capture_only 覆盖。
CAPTURE_ONLY = os.environ.get("LIVENOTES_CAPTURE_ONLY", "0").lower() in (
    "1", "true", "yes", "on")

# 离线补转写：给仅采集场次补逐字稿的独立进程。它扫当前的 .pcm spool 和
# 收尾后的 WAV，按 VAD 断句后写入 utterances；不改变采集链路，也不要求
# 服务端加载模型。默认只处理最近两天内的场次，老场次用 CLI --backfill 指定。
OFFLINE_ASR_INTERVAL_S = float(os.environ.get(
    "LIVENOTES_OFFLINE_ASR_INTERVAL", "5"))
OFFLINE_ASR_DAYS = float(os.environ.get(
    "LIVENOTES_OFFLINE_ASR_DAYS", "2"))
# 每轮每个场次最多补这么多秒音频，避免一场几小时的积压把其它场次饿死。
OFFLINE_ASR_MAX_FEED_S = float(os.environ.get(
    "LIVENOTES_OFFLINE_ASR_MAX_FEED_S", "120"))
# 关掉声纹可以再省一份 CAM++ 和逐句 embedding；代价是只有「说话人」一个标签。
OFFLINE_ASR_SPEAKERS = os.environ.get(
    "LIVENOTES_OFFLINE_ASR_SPEAKERS", "1").lower() in ("1", "true", "yes", "on")
# 补完字后自动触发一次收尾（总结 → 发布）。关掉时仍可手动调用
# POST /api/sessions/finalize；失败按 RETRY 秒退避重试，避免模型/飞书临时故障时
# 永远停在「转写完成、没有总结」。
OFFLINE_ASR_FINALIZE = os.environ.get(
    "LIVENOTES_OFFLINE_ASR_FINALIZE", "1").lower() in ("1", "true", "yes", "on")
OFFLINE_ASR_FINALIZE_RETRY_S = float(os.environ.get(
    "LIVENOTES_OFFLINE_ASR_FINALIZE_RETRY", "600"))

# 飞书原生阅读面（docs/47）：总结完成后把场次发布成一篇云文档 + 多维表格
# 里的一行。默认关 —— 没配飞书凭证的机器不应该因为发布失败而受影响。
# 凭证和开关都可由 data/feishu.json 覆盖（环境变量优先）。
FEISHU_PUBLISH_ENABLED = os.environ.get(
    "FEISHU_PUBLISH_ENABLED", "0").lower() in ("1", "true", "yes", "on")


def _lan_ip() -> str:
    """取本机局域网 IP。手机点卡片链接时不能用 127.0.0.1，这里自动填对。

    不能用 "连 8.8.8.8 看出口网卡" 那招：装了代理/WSL 的机器会选中
    198.18.x 或 172.17.x 这类虚拟网卡，手机根本连不上。改成枚举后按网段择优。
    """
    import socket
    cands: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip.startswith(("127.", "169.254.", "198.18.", "198.19.")):
                continue
            if ip not in cands:
                cands.append(ip)
    except OSError:
        pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0                      # 家用路由器最常见的网段，优先
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return 2
            except ValueError:
                pass
        return 3

    return sorted(cands, key=rank)[0] if cands else HOST


def lan_ip() -> str:
    """手机能连上的本机地址；找不到局域网网卡时退回回环（调用方据此提示）。"""
    return _lan_ip()


def _default_base_url() -> str:
    """链接前缀：只在服务确实监听在可达地址时才用局域网 IP。

    旧逻辑是「HOST 只要是回环就自动换成局域网 IP」，但桌面壳把引擎绑在
    127.0.0.1 的随机端口上——拼出来的 http://192.168.x.x:<端口> 指向一个
    没人监听的地址，手机点开只会超时。回环就老实写回环，局域网访问由
    app/lan.py 在显式开启后接管（它会给出真实监听的地址）。
    """
    env = os.environ.get("LIVENOTES_BASE_URL")
    if env:
        return env.rstrip("/")
    if HOST in ("127.0.0.1", "localhost", "::1"):
        return "http://127.0.0.1:%d" % PORT
    host = _lan_ip() if HOST in ("0.0.0.0", "::") else HOST
    return "http://%s:%d" % (host, PORT)


BASE_URL = _default_base_url()
# 高光推送：默认每天最多 5 条。单用户场景下，"烦"比"漏"代价大得多，
# 但 3 条 + 30 分钟冷却叠起来，一场两小时的直播最多只响一次——用户会以为没在推。
PUSH_DAILY_CAP = int(os.environ.get("LIVENOTES_PUSH_CAP", "5"))
PUSH_ENABLED = os.environ.get("LIVENOTES_PUSH", "1").lower() not in ("0", "false", "no")
# 同一条直播里，两条实时推送之间至少隔多少秒（按录音内时间算）。
# 旧默认 1800 太保守：一句话值不值得看，40 秒后就没意义了，而半小时的间隔
# 让它只能"事后补发"。10 分钟是打扰与时效的折中。
PUSH_COOLDOWN_S = float(os.environ.get("LIVENOTES_PUSH_COOLDOWN", "600"))
# 单场实时推送上限：宁可让回顾补齐，也别让一场 3 小时的直播响 20 次。
PUSH_SESSION_CAP = int(os.environ.get("LIVENOTES_PUSH_SESSION_CAP", "3"))
# 结束时的「本场高光回顾」：实时推送没覆盖到的，在这一条里补齐。
# 关掉后，结束只补推最后一条压着没发的，不发回顾。
PUSH_DIGEST = os.environ.get("LIVENOTES_PUSH_DIGEST", "1").lower() not in ("0", "false", "no")
# 卡片 vs 纯文本：卡片把时间做成字段、回听做成按钮，手机上少一次点按；
# 但卡片 schema 随飞书版本变，所以失败必须回退纯文本（见 live._send）。
PUSH_CARD = os.environ.get("LIVENOTES_PUSH_CARD", "1").lower() not in ("0", "false", "no")

# 实时监听用的本地模型（不装也能用演示/回放，只是「实时监听」不可用）
MODELS_DIR = Path(os.environ.get("LIVENOTES_MODELS_DIR", str(ROOT / "models")))
SENSEVOICE_MODEL = MODELS_DIR / "sensevoice" / "model.int8.onnx"
SENSEVOICE_TOKENS = MODELS_DIR / "sensevoice" / "tokens.txt"
VAD_MODEL = MODELS_DIR / "vad" / "silero_vad.onnx"
SPEAKER_MODEL = MODELS_DIR / "speaker" / "campplus.onnx"

LIVE_ASR_LANGUAGE = os.environ.get("LIVENOTES_ASR_LANGUAGE", "zh")
# 在线聚类的上限。原来的 3 太紧：第 4 个人一出现就被静默并进最近的那个簇，
# 整场再也纠不回来。现在放宽到 8，并在结束后用全场声纹重聚类（app/recluster.py）
# 把结果覆盖掉，所以这个数字只影响"实时阶段先怎么显示"。
LIVE_MAX_SPEAKERS = int(os.environ.get("LIVENOTES_MAX_SPEAKERS", "8"))
LIVE_MIN_SPEECH_S = 0.3          # 短于这个时长的人声不送 ASR
LIVE_MAX_SEGMENT_S = 20          # VAD 自带的软上限
LIVE_FORCE_SEGMENT_S = float(os.environ.get("LIVENOTES_FORCE_SEGMENT_S", "4"))
# ↑ 直播 BGM 会让 VAD 一直判"在说话"，max_speech_duration 在长语音里不保证按时切；
#   到点强制 flush，保证每 4 秒左右必出一句，延迟有上界。

# 滚动字幕：当前这句还没切完时，每隔 1 秒把已收到的音频先识别一次，作灰色临时字幕。
# 设为 0 可关闭（关闭后退回"4 秒一句"的定稿模式）。
LIVE_PARTIAL_INTERVAL_S = float(os.environ.get("LIVENOTES_PARTIAL_INTERVAL_S", "1.0"))
LIVE_PARTIAL_MIN_S = 0.8      # 太短不识别，避免只有一个字的闪烁
LIVE_PARTIAL_MAX_S = 8.0      # 临时字幕最多看最近 8 秒，防止长句越算越慢
# 断流看门狗：采集页每 256ms 推一块 PCM（安静也推），所以「超过这个秒数一块都没到」
# 就不是安静，而是链路断了——浏览器崩了、页面被关、页面被冻住、采集页卡死。
# 服务端按这个阈值自动收尾（见 server._watch_stalled）：落 WAV、排队总结、发回顾，
# 否则整夜挂着一个 running 的场次，看门狗不跑就只能等进程重启才捞得回来。
# 设 0 关闭。
LIVE_STALL_STOP_S = float(os.environ.get("LIVENOTES_STALL_SECONDS", "120"))
# 断流预警：块停了先发一条飞书，不结束场次。30 秒足够把「浏览器崩了/页面被冻住」
# 和「主播只是安静」区分开——安静时块照推，所以「没块」一定是采集链路出了问题。
# 设 0 关闭。
LIVE_STALL_WARN_S = float(os.environ.get("LIVENOTES_STALL_WARN_SECONDS", "30"))
# 静音看门狗：块照推、链路健康，但内容连续这么久都是「数字全零」（RMS 恰好为 0）
# 就收尾。抖音下播后 CDN 常常还挂着连接、继续按实时速率吐全静音帧，ffmpeg 也照常
# 出 PCM——断流看门狗只数块，永远发现不了。主播真安静的时候会有底噪（RMS 不是 0），
# 所以连续全零几乎只可能是这一场已经没东西可录了。默认 15 分钟，设 0 关闭。
LIVE_ZERO_STOP_S = float(os.environ.get("LIVENOTES_ZERO_STOP_SECONDS", "900"))
# 静音预警：到这个时长只把房间提示改成「音频已连续静音 N 分钟」，不结束场次。
# 用户能提前看见「这一路其实没声音了」，还来得及自己决定要不要停。设 0 关闭。
LIVE_ZERO_WARN_S = float(os.environ.get("LIVENOTES_ZERO_WARN_SECONDS", "120"))
# 判定「数字静音」的 RMS 上限。实测安静房间的底噪在 0.0002~0.003，
# 真正断掉的流是精确的 0（AAC 静音帧解出来就是全零），留一点余量即可。
LIVE_ZERO_RMS = float(os.environ.get("LIVENOTES_ZERO_RMS", "0.0001"))

# ASR 工作池：采集/VAD 与识别解耦，多个会话共享一个识别器（sherpa-onnx 的
# Recognizer 在 CPU provider 下线程安全，Stream 不共享）。0 = 按 CPU 核数自动。
# 工作线程只负责排队/调度，真正的并行度由 ONNX Runtime 的 intra-op 线程决定。
LIVE_ASR_WORKERS = int(os.environ.get("LIVENOTES_ASR_WORKERS", "0"))
LIVE_ASR_THREADS = int(os.environ.get("LIVENOTES_ASR_THREADS", "0"))
# 单会话最多允许多少条定稿排队；超出时丢最旧的定稿（计入 dropped，最终体现在
# 「未转写」时长里），保证慢会话不会吃光内存、也不会无限拖后。
LIVE_ASR_MAX_PENDING_FINALS = int(os.environ.get("LIVENOTES_ASR_MAX_PENDING_FINALS", "8"))
# 临时字幕排队超过这个时长就直接丢：下一帧会覆盖更长的音频，旧结果没有价值。
LIVE_PARTIAL_TTL_S = float(os.environ.get("LIVENOTES_PARTIAL_TTL_S", "2.5"))


def asr_workers() -> int:
    """ASR 调度线程数。默认给 10 路并发留足并行槽位，但不把线程开满。"""
    if LIVE_ASR_WORKERS > 0:
        return max(1, min(16, LIVE_ASR_WORKERS))
    if _PERF_MODE == "lite":
        return 2
    n = os.cpu_count() or 2
    if n <= 4:
        return 2
    return max(2, min(8, n - 4))


def asr_threads() -> int:
    """单个解码请求使用的 ONNX Runtime intra-op 线程数（共享会话）。"""
    if LIVE_ASR_THREADS > 0:
        return max(1, min(8, LIVE_ASR_THREADS))
    if _PERF_MODE == "lite":
        return 2
    n = os.cpu_count() or 2
    return max(1, min(4, n // 2))


# ---------------------------------------------------------------- 性能档位
# config 不导入 settings（会循环）：run.py / server.py 读设置后调用
# apply_perf_mode()，环境变量始终优先。
_PERF_MODE = "full"          # 未应用前的默认：按现有流畅参数跑
_PERF_DETECTED: dict = {}
_LOW_POWER_PAUSE = True      # 设置页「低电量时暂停实时转写」
_POWER_CACHE = (0.0, None)   # (monotonic, 结果)；电池状态不必每句话都问系统


def _ram_mb() -> int:
    try:
        if os.name == "nt":
            import ctypes

            class _Mem(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = _Mem()
            m.dwLength = ctypes.sizeof(_Mem)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys // (1024 * 1024))
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                   // (1024 * 1024))
    except Exception:
        return 0


def detect_hardware() -> dict:
    global _PERF_DETECTED
    if not _PERF_DETECTED:
        _PERF_DETECTED = {"cores": os.cpu_count() or 2, "ram_mb": _ram_mb()}
    return dict(_PERF_DETECTED)


def system_power() -> dict:
    """电源状态。Windows 用 GetSystemPowerStatus，其他平台尽量读 sysfs。

    返回 ``supported / on_battery / percent / charging``；读不到时 supported=False，
    调用方按「没有低电量这回事」处理，不能因为读不到电池就停转写。
    """
    global _POWER_CACHE
    now = time.monotonic()
    cached_at, cached = _POWER_CACHE
    if cached is not None and now - cached_at < 15.0:
        return dict(cached)
    out = {"supported": False, "on_battery": False, "percent": -1,
           "charging": False}
    try:
        if os.name == "nt":
            import ctypes

            class _Power(ctypes.Structure):
                _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                            ("BatteryFlag", ctypes.c_ubyte),
                            ("BatteryLifePercent", ctypes.c_ubyte),
                            ("SystemStatusFlag", ctypes.c_ubyte),
                            ("BatteryLifeTime", ctypes.c_ulong),
                            ("BatteryFullLifeTime", ctypes.c_ulong)]

            st = _Power()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
                ac = int(st.ACLineStatus)
                pct = int(st.BatteryLifePercent)
                out.update({
                    "supported": 0 <= pct <= 100,
                    "on_battery": ac == 0,
                    "percent": pct if 0 <= pct <= 100 else -1,
                    "charging": ac == 1,
                })
        else:
            base = Path("/sys/class/power_supply")
            for bat in sorted(base.glob("BAT*")):
                try:
                    state = (bat / "status").read_text(encoding="utf-8").strip().lower()
                    pct = int((bat / "capacity").read_text(encoding="utf-8").strip())
                except (OSError, ValueError):
                    continue
                out.update({
                    "supported": True,
                    "on_battery": state == "discharging",
                    "percent": pct,
                    "charging": state == "charging",
                })
                break
    except Exception:
        pass
    _POWER_CACHE = (now, dict(out))
    return out


def low_power() -> bool:
    """是否该按低电量策略暂停实时转写：未接电源且低于 20%。"""
    if not _LOW_POWER_PAUSE:
        return False
    st = system_power()
    pct = int(st.get("percent") if st.get("percent") is not None else -1)
    return bool(st.get("on_battery")) and 0 <= pct < 20


def auto_mode() -> str:
    """≤4 核或 ≤8GB -> 低配；检测不到内存时只看核数。"""
    d = detect_hardware()
    if int(d.get("cores") or 0) <= 4:
        return "lite"
    ram = int(d.get("ram_mb") or 0)
    if 0 < ram <= 8 * 1024:
        return "lite"
    return "full"


def apply_perf_mode(mode: str = "auto", low_power_pause: bool = True) -> dict:
    """应用性能档位。引擎级参数对下一场生效，不中断当前录音。"""
    global _PERF_MODE, _LOW_POWER_PAUSE
    mode = str(mode or "auto").strip().lower()
    if mode not in ("auto", "full", "lite"):
        mode = "auto"
    _PERF_MODE = auto_mode() if mode == "auto" else mode
    _LOW_POWER_PAUSE = bool(low_power_pause)
    return perf()


def perf() -> dict:
    d = detect_hardware()
    lite = _PERF_MODE == "lite"
    power = system_power()
    saving = low_power()
    effective = {
        "asr_workers": asr_workers(), "asr_threads": asr_threads(),
        "partials": not lite and not saving, "highlight_llm": not lite,
        "live_highlight_card": not lite and not saving,
        "follow_interval_min_s": 180 if lite else 60,
        "notify_poll_s": 6 if lite else 4,
        "live_poll_s": 5 if lite else 3,
        "library_page": 16 if lite else 24,
        "transcript_window": 80 if lite else 200,
        "qa_top_k": 6 if lite else 10,
        "qa_tokens": 1800 if lite else 3000,
        "animations": not lite,
    }
    advice = "lite" if int(d.get("cores") or 0) <= 4 or (
        0 < int(d.get("ram_mb") or 0) <= 8 * 1024) else "full"
    return {"mode": _PERF_MODE, "detected": d, "effective": effective,
            "advice": advice, "low_power": saving, "power": power,
            "low_power_pause": _LOW_POWER_PAUSE,
            "note": "引擎参数在下一场记录生效；界面与轮询立即生效。"}


def partials_enabled() -> bool:
    return _PERF_MODE != "lite" and not low_power()


# 直播中的「疑似认人」：只影响展示与飞书文案，不写底库、不标 verified。
# 每 20 秒对成形的簇比对一次；首次只给「疑似」，连续两次同一个人且满 30 秒
# 才去掉问号——飞书发出去收不回，宁可晚 20 秒。
LIVE_HINT_EVERY_S = float(os.environ.get("LIVENOTES_LIVE_HINT_EVERY", "20"))
LIVE_HINT_MIN_S = float(os.environ.get("LIVENOTES_LIVE_HINT_MIN", "20"))
LIVE_HINT_AUTO_S = float(os.environ.get("LIVENOTES_LIVE_HINT_AUTO", "30"))
LIVE_HINT_CONFIRM = int(os.environ.get("LIVENOTES_LIVE_HINT_CONFIRM", "2"))

# 采集与切分约定
SAMPLE_RATE = 16000          # 声纹/ASR 统一重采样到 16k 单声道
SEGMENT_SECONDS = 60         # 落盘分片长度，防止长直播崩溃丢全场
SUMMARY_BUCKET_SECONDS = 600 # 「按时间段」总结的默认分桶（10 分钟）

# 可选 LLM：正式总结必须有模型；未配置时明确失败，不再生成本地抽取稿。
# 直播简报 / 问笔记等独立能力仍有各自的本地降级，并在输出里标明来源。
LLM_BASE = os.environ.get("LIVENOTES_LLM_BASE", "").rstrip("/")
LLM_KEY = os.environ.get("LIVENOTES_LLM_KEY", "")
LLM_MODEL = os.environ.get("LIVENOTES_LLM_MODEL", "")
LLM_TIMEOUT = int(os.environ.get("LIVENOTES_LLM_TIMEOUT", "60"))
# 单次 socket 读写的超时（上面的 LLM_TIMEOUT）不封顶「整次请求」：中转站只要一直
# 慢慢吐字节，urlopen 就永远不会超时——实测有过一次收尾调用挂 7 分钟没回来。
# 这个值是整次调用的硬上限，到点就放弃本次生成并报错，绝不把收尾线程吊死。
#
# 这是**默认值**，适合分类 / 润色 / 起标题这类短调用。整场总结要长得多，用下面
# 那个单独的 LLM_SUMMARY_TOTAL_TIMEOUT。
LLM_TOTAL_TIMEOUT = int(os.environ.get("LIVENOTES_LLM_TOTAL_TIMEOUT", "180"))

# 整场总结的整次请求上限，比默认值宽松得多。
#
# 实测一场 1720 句、5.3 小时（输入 4.5 万 token）的总结：同一个请求两次生成，
# 输出分别是 4296 和 5646 token，耗时 113 秒和 >180 秒。差距不在网络，在推理型
# 模型的思考量——它每次想多少并不固定。原来的 75 秒会在模型正常生成时把它砍断，
# 三次重试一样长，整晚的总结必然失败（云端 LIVE_20261125_210827 就是这么挂的）；
# 180 秒也只够中位情况，实测仍要重试一次才成功。
#
# 300 秒覆盖到观测到的长尾，同时远小于调度层的 LLM_TASK_TIMEOUT_S（600 秒），
# 所以单个调用不会把收尾线程吊死。
LLM_SUMMARY_TOTAL_TIMEOUT = int(
    os.environ.get("LIVENOTES_LLM_SUMMARY_TOTAL_TIMEOUT", "300"))


def _env_int(name: str, default: int) -> int:
    """环境变量写错不该把服务带崩，回退默认值即可。"""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


# ---- 录音归档与分享 ----
# 收尾后把 WAV 转成 m4a（AAC-LC 单声道）。32k 对语音足够，约为 WAV 的 1/8，
# 且 Chrome/Edge/Safari/飞书 WebView 都能直接播；opus 只在飞书语音片段上按需生成。
AUDIO_AAC_BITRATE = os.environ.get("LIVENOTES_AUDIO_AAC_BITRATE", "32k")
AUDIO_OPUS_BITRATE = os.environ.get("LIVENOTES_AUDIO_OPUS_BITRATE", "24k")
# 原 WAV 保留天数：0 = 转码成功后尽快删；-1 = 永久保留。默认 7 天，给抽查留窗口。
WAV_RETENTION_DAYS = _env_int("LIVENOTES_WAV_RETENTION_DAYS", 7)
# 归档音频保留天数：0 = 永久保留（默认）。要自动清理老录音时再设 30/90。
AUDIO_RETENTION_DAYS = _env_int("LIVENOTES_AUDIO_RETENTION_DAYS", 0)
# 飞书片段缓存与分享包输出目录（都在 data/ 下，不提交）
CLIPS_DIR = DATA_DIR / "clips"
SHARE_DIR = DATA_DIR / "share"
# 精修交付：每个项目一个目录，放报告、逐字稿、片段与 zip。
DELIVERIES_DIR = DATA_DIR / "deliveries"
# 周报/复盘 Markdown：中间产物，默认保留 30 天（见 app/report.cleanup）。
REPORTS_DIR = DATA_DIR / "reports"


# ---- 全局 LLM 任务调度（app/scheduler.py）----
# 同时最多打几个模型请求。10 个直播间一起下播时，收尾任务会排队而不是一起打出去。
LLM_CONCURRENCY = max(1, min(16, _env_int("LIVENOTES_LLM_CONCURRENCY", 2)))
# 单次调用遇到 429/5xx/网络错误时的最大尝试次数（含第一次）。
LLM_MAX_ATTEMPTS = max(1, min(8, _env_int("LIVENOTES_LLM_MAX_ATTEMPTS", 3)))
# 指数退避：base * 2^(n-1)，加抖动，封顶 cap；设为 0 可关闭等待（测试用）。
LLM_BACKOFF_BASE_S = max(0.0, _env_float("LIVENOTES_LLM_BACKOFF_BASE", 1.0))
LLM_BACKOFF_MAX_S = max(0.0, _env_float("LIVENOTES_LLM_BACKOFF_MAX", 30.0))
# token 预算：0 = 不限制，也是默认值。总结/标注跑一半被预算掐断就只能明确失败
#（本地稿已移除），体验很差（长直播尤其明显），所以默认不设上限；要限额时显式打开。
LLM_SESSION_TOKEN_BUDGET = max(0, _env_int("LIVENOTES_LLM_SESSION_TOKEN_BUDGET", 0))
LLM_DAILY_TOKEN_BUDGET = max(0, _env_int("LIVENOTES_LLM_DAILY_TOKEN_BUDGET", 0))
# 送进总结的逐句上限：超过就按时间分层抽样（覆盖全场，而不是只取开头）。
LLM_SUMMARY_MAX_ROWS = max(50, _env_int("LIVENOTES_LLM_SUMMARY_MAX_ROWS", 800))
# 同一指纹的成功结果缓存多久（秒）。期间重复点击/收尾后立刻点总结都直接复用。
LLM_DEDUPE_TTL_S = max(0, _env_int("LIVENOTES_LLM_DEDUPE_TTL", 600))
# HTTP 请求等待一个排队任务完成的上限；超过就返回 504 + job_id，不假装成功。
LLM_TASK_TIMEOUT_S = max(1, _env_int("LIVENOTES_LLM_TASK_TIMEOUT", 600))
# 供应商不返回 usage 时，按这个上限估算输出 token，用于预算预占。
LLM_EXPECTED_OUTPUT_TOKENS = max(64, _env_int("LIVENOTES_LLM_EXPECTED_OUTPUT_TOKENS", 1200))
# 内存里保留最近多少条任务记录（同时落库，重启后仍能在任务面板看到）。
LLM_JOB_HISTORY = max(20, _env_int("LIVENOTES_LLM_JOB_HISTORY", 200))
# 可选的单价（每百万 token，人民币或美元都行，看账单口径）。0 = 不折算花费。
LLM_PRICE_INPUT_PER_MTOK = max(0.0, _env_float("LIVENOTES_LLM_PRICE_INPUT", 0.0))
LLM_PRICE_OUTPUT_PER_MTOK = max(0.0, _env_float("LIVENOTES_LLM_PRICE_OUTPUT", 0.0))

# ---- 飞书直播简报（app/livebrief.py）----
# 简报和结束时的正式总结共用调度器，但有自己的软上限：一次问答不能把整场
# 收尾的预算吃光。预算不足时简报直接报错，不阻塞状态查询。
BRIEF_LLM = os.environ.get("LIVENOTES_BRIEF_LLM", "1").lower() not in ("0", "false", "no")
BRIEF_TOPICS_COOLDOWN_S = max(0.0, _env_float("LIVENOTES_BRIEF_TOPICS_COOLDOWN", 60))
BRIEF_SUMMARY_COOLDOWN_S = max(0.0, _env_float("LIVENOTES_BRIEF_SUMMARY_COOLDOWN", 120))
BRIEF_DAILY_CAP = max(0, _env_int("LIVENOTES_BRIEF_DAILY_CAP", 30))
BRIEF_SESSION_TOKEN_CAP = max(0, _env_int("LIVENOTES_BRIEF_SESSION_TOKEN_CAP", 30000))
BRIEF_SUMMARY_MAX_ROWS = max(30, _env_int("LIVENOTES_BRIEF_SUMMARY_MAX_ROWS", 350))
BRIEF_MAX_MINUTES = max(1, min(240, _env_int("LIVENOTES_BRIEF_MAX_MINUTES", 120)))
BRIEF_TOPICS_TTL_S = max(0.0, _env_float("LIVENOTES_BRIEF_TOPICS_TTL", 90))
BRIEF_SUMMARY_TTL_S = max(0.0, _env_float("LIVENOTES_BRIEF_SUMMARY_TTL", 300))

# ---- 可观测性（app/metrics.py）：采样间隔、保留天数、停滞判定 ----
# 快照落库间隔。15 秒足够还原「那几分钟」；再密就是给 SQLite 找活干。
METRICS_SAMPLE_S = max(5.0, _env_float("LIVENOTES_METRICS_SAMPLE_S", 15.0))
METRICS_RETENTION_DAYS = max(1, _env_int("LIVENOTES_METRICS_RETENTION_DAYS", 7))
# 内存里保留最近多少场的指标（已结束的场次也能在看板看到，不影响落库）。
SESSION_METRICS_MAX = max(8, _env_int("LIVENOTES_METRICS_SESSION_MAX", 64))
# 「有语音但多久没有新定稿」算一次停滞。60 秒足够排除一句长句的等待。
LIVE_STALL_S = max(10.0, _env_float("LIVENOTES_STALL_S", 60.0))
# 录内静默超过这个秒数就不算停滞：本来没人说话，不该报警。
LIVE_STALL_IDLE_S = max(5.0, _env_float("LIVENOTES_STALL_IDLE_S", 30.0))
LIVE_STALL_MIN_S = max(10.0, _env_float("LIVENOTES_STALL_MIN_S", 30.0))


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, CLIPS_DIR, SHARE_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
