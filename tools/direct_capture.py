# -*- coding: utf-8 -*-
"""抖音直取采集：解析出 FLV/HLS -> ffmpeg 拉 16k 单声道 PCM -> /api/live/chunk。

为什么单独一个模块：浏览器采集的会话是采集页自己建的，direct 没有页面，
必须由代理自己调 /api/live/start；把它和 SharedAgent 混在一个类里会让
「谁建会话、谁停会话」变得不清楚。这里只做抖音，B 站/快手等以后按同样
的 resolver 接口加，不改变主循环。

解析优先级（按地址保质期排）：
  1. live.douyin.com/<web_rid>：房号跨场次稳定，页面首屏 JSON 里直接嵌着
     当前这一场的 stream_url；
  2. reflow 分享页里现成的播放地址（页面没改版时仍可用，顺带兜底主播名）；
  3. streamlink。
reflow 路径里的是 room id，一场直播一个：锚点重新开播后那张页面就永远停在
上一场，还继续吐上一场的播放地址——先取地址就会把死地址交给 ffmpeg，反复
404。web_rid 不变，所以以它为准。

失败策略：
  - 解析失败 / ffmpeg 退出 / 15 秒没有成功送出的音频块 -> 用同一个 session
    重新解析、重启 ffmpeg，最多 3 次；
  - 3 次仍失败 -> 默认把房间标成 failed 并亮出「切换浏览器采集」，等用户在
    收听页确认后才切（设置页或 LIVENOTES_DIRECT_FALLBACK=ask）；设 auto 才是
    旧的无感回退（per_room，不可用再 shared），none 则只失败不提供切换。
  - 会话先留着（服务端看门狗 120 秒后收尾），用户马上点切换能接着同一场录。
"""
from __future__ import annotations

import array
import gzip
import html
import http.client
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

FALLBACK_MODES = ("ask", "auto", "per_room", "shared", "none")

try:  # 作为 tools.direct_capture 导入（测试）
    from .capture_agent import (Agent, SharedAgent, SharedBrowser, api,
                                find_chrome, find_shared_browser)
except ImportError:  # 作为 python tools/capture_agent.py 的依赖导入
    from capture_agent import (Agent, SharedAgent, SharedBrowser, api,
                               find_chrome, find_shared_browser)

from app.traffic import (CountingProxy, parse_bitrate_kbps,  # noqa: E402
                         parse_video_resolution, resolution_kbps)

CHUNK_SAMPLES = 4096
CHUNK_BYTES = CHUNK_SAMPLES * 2       # s16le：256ms
DIRECT_WARN_S = 10.0                  # 先提示，不重建
DIRECT_RECOVER_AFTER_S = 15.0         # 超过这个时间没有成功送块就重建
DIRECT_RETRY_S = 10.0                 # 解析/启动失败后的退避
DIRECT_MAX_RESTARTS = 3
# 网络类解析失败（超时/连接重置/临时错误页）的补试节奏；明确下播不补试。
RESOLVE_RETRY_DELAYS = (3.0, 10.0)
FIRST_CHUNK_WAIT_S = 8.0
RESOLVE_TIMEOUT_S = 20.0
# 直取只取声音，默认从最低可用画质拉流；high 留给排障/特殊场次。
_DEFAULT_QUALITY = "low"
# 静音探针：块照推、但内容连续全零时，定期抓一次直播间页面确认还在不在播。
# 抖音下播后连接常常还挂着，ffmpeg 持续按实时速率吐静音帧——这时"有没有块"
# 和 ffmpeg 退没退出都说明不了问题，只有页面状态能给答案。
DIRECT_ZERO_RMS = 0.0001              # 与 app.config.LIVE_ZERO_RMS 同口径
DIRECT_ZERO_PROBE_AFTER_S = 120.0     # 连续全零这么久才值得去问一次页面
DIRECT_ZERO_PROBE_EVERY_S = 60.0      # 两次探测的最小间隔，别把抖音页面当轮询接口
PROBE_TIMEOUT_S = 8.0

# 精确流量计量：流先经过本地计数代理再交给 ffmpeg。默认跟随设置页；
# LIVENOTES_TRAFFIC_PROXY=1/0 仅作为脚本的临时覆盖（代理只监听 127.0.0.1）。
def _env_traffic_proxy() -> bool | None:
    raw = os.environ.get("LIVENOTES_TRAFFIC_PROXY", "").strip().lower()
    if not raw:
        return None
    return raw in ("1", "true", "yes", "on")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ResolveError(RuntimeError):
    """解析不出可播放的流地址。

    ``retryable`` 表示这次失败像网络抖动（超时、连接重置、临时错误页），
    值得隔几秒再试；明确下播 / 链接不是直播间 / 未安装 streamlink 不重试，
    免得让用户白等十几秒。
    """

    def __init__(self, msg: str = "", retryable: bool = False) -> None:
        super().__init__(msg)
        self.retryable = bool(retryable)


class SessionGone(RuntimeError):
    """服务端返回 404：会话已结束/服务重启过。"""


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def is_douyin(url: str) -> bool:
    """direct 只做抖音；B 站/快手等直接交给浏览器，不浪费一次解析超时。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d)
               for d in ("douyin.com", "amemv.com", "iesdouyin.com"))


_LIVE_RE = re.compile(r"^https?://live\.douyin\.com/(\d+)", re.I)
_REFLOW_RE = re.compile(r"/(?:douyin/webcast/reflow|share/live)/(\d+)")
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")
# live.douyin.com 是桌面版页面：用手机 UA 拿到的首屏 JSON 形状不一样，
# 而且它本来就是为了不装浏览器才抓的，别用移动 UA 去撞。
_PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


_LIVE_PAGE = "https://live.douyin.com/"
_REFLOW_BASE = "https://webcast.amemv.com/douyin/webcast/reflow/"
# reflow 页里没有播放器元素时，用这些文案判断是「没开播」还是「抖音临时报错」。
# 「服务器开小差了」不能算下播：它只是抖音的通用错误提示，直播间可能开着。
_OFFLINE_MARKS = ("直播已结束", "主播已下播", "主播暂未开播", "直播已关闭")
_PAGE_ERROR_MARKS = ("服务器开小差了", "直播间不存在", "页面不存在")


_REDIRECT_ATTEMPTS = 2
_REDIRECT_TIMEOUT_CAP_S = 10.0


def _redirect_target(url: str, timeout: float = 10.0) -> tuple[str, str]:
    """跟一次跳转，返回 (落地地址, 失败原因)。短链（v.douyin.com）靠它落地。

    抖音的跳转偶尔会超过十秒。以前单次超时就返回空串，而且 reflow 和 live
    各跟一次：第一次超时、第二次成功落在 reflow 时，两边都拿不到地址，最后
    只剩一句没有信息量的「没能解析出直播流地址」。这里重试一次，并把最后
    一次的错误带回去，调用方才能说清是「短链打不开」还是「页面没有流」。
    """
    per_try = max(1.0, min(float(timeout or 10.0), _REDIRECT_TIMEOUT_CAP_S))
    err = ""
    for _ in range(_REDIRECT_ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA})
            with urllib.request.urlopen(req, timeout=per_try) as resp:
                return resp.geturl(), ""
        except Exception as e:
            err = str(e)[:120] or type(e).__name__
    return "", err


def _final_url(url: str, timeout: float = 10.0) -> str:
    """跟一次跳转，返回落地地址；失败返回空串。"""
    return _redirect_target(url, timeout)[0]


def _live_target(url: str, timeout: float = 10.0) -> str:
    """链接 -> live.douyin.com/<web_rid>；本来就落不到 live 页就返回空串。

    这是**跨场次稳定**的房号：同一主播重新开播，web_rid 不变，页面里的
    stream_url 会换成新一场的。reflow 里的 room id 做不到这一点。
    """
    u = (url or "").strip()
    m = _LIVE_RE.match(u)
    if m:
        return _LIVE_PAGE + m.group(1)
    if not is_douyin(u):
        return ""
    m = _LIVE_RE.match(_final_url(u, timeout))
    return _LIVE_PAGE + m.group(1) if m else ""


def _landing_url(url: str, timeout: float = 10.0) -> str:
    """抖音直播间链接 -> HTML 里带房间信息的 reflow 分享页。

    reflow 是分享落地页，首屏 JSON 里有主播名、标题和 owner.webRid。
    注意：reflow 路径上的 <id> 是 room id，**不是** web_rid。拿 web_rid
    拼 reflow/<web_rid> 只会得到一张空壳页（抖音回「服务器开小差了」），
    所以 live.douyin.com 链接在这里直接返回空串，交给 _live_target 处理。
    短链先跟一次跳转，落到 reflow 再换回 reflow 形状；返回空串表示不是
    抖音 / 跳转失败 / 落到了 live 页。
    """
    u = (url or "").strip()
    if _LIVE_RE.match(u):
        return ""
    m = _REFLOW_RE.search(urllib.parse.urlsplit(u).path)
    if m:
        return _REFLOW_BASE + m.group(1)
    if not is_douyin(u):
        return ""
    m = _REFLOW_RE.search(urllib.parse.urlsplit(_final_url(u, timeout)).path)
    return _REFLOW_BASE + m.group(1) if m else ""


def canonical_douyin_url(url: str, timeout: float = 10.0) -> str:
    """直播链接 -> 真能打开的 live.douyin.com/<web_rid>；拿不到返回空串。

    注意：reflow 后面的 <id> 是 room id，**不是** web_rid。旧实现拿它直接拼
    live.douyin.com/<id>，实测打开就是「服务器开小差了」——直播间明明开着，
    页面却什么都没有。现在只认 reflow 页 owner 里的 webRid；读不到就返回空，
    让调用方退回 reflow 页，绝不伪造一个看着像、实际没有房间的地址。
    """
    u = (url or "").strip()
    m = _LIVE_RE.match(u)
    if m:
        return "https://live.douyin.com/" + m.group(1)
    landing = _landing_url(u, timeout)
    if not landing:
        return ""
    try:
        text, _cost = _fetch_reflow(landing, timeout)
    except Exception:
        return ""
    return page_url(parse_room_meta(text))


def _clean_url(u: str) -> str:
    """HTML 属性 / JSON 字符串里的播放地址 -> ffmpeg 能直接吃的 URL。"""
    u = html.unescape(str(u or ""))
    return u.replace("\\u0026", "&").replace("\\/", "/").strip()


def _pick_stream_url(text: str, quality: str = _DEFAULT_QUALITY) -> str:
    """reflow 页 -> 播放地址。优先 FLV，其次 HLS。"""
    m = re.search(r'<webcast-reflow-player[^>]*\burl="([^"]+)"', text, re.I)
    if m:
        return _clean_url(m.group(1))
    # 页面后半段是 RSC 内嵌 JSON，播放地址是转义过的 \"flv\":\"https:\/\/...\"。
    flat = text.replace('\\"', '"').replace("\\/", "/")
    stream = _pick_flv_by_quality(_flatten_json(text), quality)
    if stream:
        return stream
    for key in ("flv", "hls"):
        m = re.search(r'"%s"\s*:\s*"(https?://[^"]+)"' % key, flat, re.I)
        if m:
            return _clean_url(m.group(1))
    m = re.search(r'https?://[^\s"\'<>]+\.(?:flv|m3u8)[^\s"\'<>]*', text, re.I)
    return _clean_url(m.group(0)) if m else ""


def _fetch_reflow(landing: str, timeout: float) -> tuple[str, float]:
    req = urllib.request.Request(landing, headers={
        "User-Agent": _MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace"), time.monotonic() - t0


def _fetch_live_page(live_url: str, timeout: float) -> tuple[str, float]:
    """抓 live.douyin.com 桌面页。首屏约 1MB，stream_url 就嵌在里面。"""
    req = urllib.request.Request(live_url, headers={
        "User-Agent": _PC_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace"), time.monotonic() - t0


def _flatten_json(text: str) -> str:
    """摊平页面里 RSC/JS 字符串的转义，只留一层能直接匹配的 JSON。

    播放地址里的 & 可能是 \\u0026，也可能是 \\\\u0026（外面又套了一层字符串）。
    两种都得还原：带着字面反斜杠的 URL 交给 ffmpeg 只会 403/404。
    """
    t = str(text or "")
    for _ in range(2):          # 双层转义：\\u0026 -> & -> &
        t = (t.replace("\\\\u0026", "&").replace("\\u0026", "&")
              .replace("\\\\/", "/").replace("\\/", "/")
              .replace('\\\\"', '"').replace('\\"', '"'))
    return t


# 新版页面用 camelCase（streamUrl.flvPullUrl），老版是 flv_pull_url；
# 两种都要认，否则会白白掉到「first flv」甚至 streamlink 兜底。
_FLV_BLOCK_RE = re.compile(
    r'"(?:flv_pull_url|flvPullUrl)"\s*:\s*\{([^{}]{0,4000})\}')
_FLV_PAIR_RE = re.compile(
    r'"([A-Za-z0-9_]{1,24})"\s*:\s*"(https?://[^"]+?\.flv[^"]*)"')
# 直取只取声音，画质只决定顺带下载的视频有多少。抖音的档位从高到低：
# ORIGION(原画) / FULL_HD1(蓝光) / HD1(超清) / SD1(高清) / SD2(标清) /
# LD1(流畅) / LD2。省流档从最低往上找，避免为了 16k 单声道音频拉 6 Mbps 视频。
_QUALITY_ORDERS = {
    "low": ("LD2", "LD1", "SD2", "SD1", "HD1", "FULL_HD1", "ORIGION"),
    "high": ("ORIGION", "FULL_HD1", "HD1", "SD1", "SD2", "LD1", "LD2"),
}


def _quality_order(quality: str) -> tuple[str, ...]:
    return _QUALITY_ORDERS.get(str(quality or "").strip().lower(),
                               _QUALITY_ORDERS[_DEFAULT_QUALITY])


def _pick_flv_by_quality(flat: str, quality: str = _DEFAULT_QUALITY) -> str:
    """扁平化页面 JSON -> 按画质偏好挑一路 FLV；没有就返回空。"""
    found: dict[str, str] = {}
    for block in _FLV_BLOCK_RE.finditer(flat):
        for name, url in _FLV_PAIR_RE.findall(block.group(1)):
            found.setdefault(name, _clean_url(url))
    order = _quality_order(quality)
    # 省流档倒着找时，未知档位也别抢在已知低档前面：先按偏好，再按发现顺序。
    for q in order:
        if found.get(q):
            return found[q]
    for url in found.values():          # 画质名换了也认，只要有一路
        if url:
            return url
    return ""


def _pick_live_stream(text: str, quality: str = _DEFAULT_QUALITY) -> str:
    """live 页 -> 当前这一场的播放地址。优先 FLV，拿不到退 HLS。"""
    flat = _flatten_json(text)
    stream = _pick_flv_by_quality(flat, quality)
    if stream:
        return stream
    m = re.search(r"https?://[^\"'\s<>]+\.m3u8[^\"'\s<>]*", flat)
    return _clean_url(m.group(0)) if m else ""


def _resolve_live_page(web_rid: str, timeout: float,
                       quality: str = _DEFAULT_QUALITY) -> tuple[str, str]:
    """web_rid -> 当前场次的播放地址；下播 / 空页面抛 ResolveError。

    解析的第一选择：web_rid 不随场次变，页面里的 stream_url 永远是最新一场。
    reflow 里的 room id 一场一个，锚点重新开播后那里只剩上一场的死地址。
    """
    try:
        text, cost = _fetch_live_page(_LIVE_PAGE + str(web_rid or ""), timeout)
    except Exception as e:
        raise ResolveError("抓取直播间页面失败：%s"
                           % (str(e)[:120] or type(e).__name__),
                           retryable=True)
    stream = _pick_live_stream(text, quality)
    if stream:
        return stream, "live %.1fs" % cost
    for mark in _OFFLINE_MARKS:
        if mark in text:
            raise ResolveError("直播间当前没有开播（页面显示「%s」）" % mark)
    raise ResolveError("直播间页面里没有找到直播流地址（可能需要登录/验证）")


def probe_room_live(room: dict,
                    timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """抓一次直播间页面，判断这一场还在不在播。返回 (online, why)。

    只认页面里明确的「直播已结束」这类文案（_OFFLINE_MARKS）。抓不到、反爬、
    页面改版、需要登录，一律按「还在播」处理——宁可多录一会儿，也不能凭一次
    HTTP 失败把用户还在等的场次收掉。online=False 时 why 是给人看的原因。
    """
    url = str((room or {}).get("resolved_url")
              or (room or {}).get("url") or "").strip()
    m = _LIVE_RE.match(url)
    web_rid = m.group(1) if m else ""
    if not web_rid:
        try:
            live_url = canonical_douyin_url(url, timeout)
        except Exception:
            live_url = ""
        web_rid = live_url.rsplit("/", 1)[-1] if live_url else ""
    if not web_rid:
        return True, ""            # 不是抖音 / 没有可探的房号：不下结论
    try:
        _resolve_live_page(web_rid, timeout)
    except ResolveError as e:
        msg = str(e)
        for mark in _OFFLINE_MARKS:
            if mark in msg:
                return False, "直播间已下播（页面显示「%s」）" % mark
        return True, ""            # 页面异常/需要登录：不据此收尾
    except Exception:
        return True, ""
    return True, ""                # 还能解析出流地址：页面认为在播，继续录


# ---------------------------------------------------------------- 房间名
# 直取只从 reflow 页里拿播放地址，页头那堆房间信息就白丢了——结果收听页没有
# 名字可用，只能退回显示域名（webcast.amemv.com），既认不出是谁在播，停了也
# 不知道该删哪一路。其实同一份 HTML 的首屏 JSON 里就写着：
#   "room":{"id":…,"idStr":"…","title":"示例直播标题",…}
#   "owner":{"id":…,"nickname":"示例主播",…}
# 所以不用另开浏览器、不用请求接口，解析一次就够了。名字是锦上添花：
# 抠不到就返回空，绝不能因为它影响录音。
_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.){0,200})"')
_NICK_RE = re.compile(
    r'"owner"\s*:\s*\{[^{}]{0,400}?"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"')
_ROOM_RE = re.compile(r'"room"\s*:\s*\{')
# live.douyin.com/<web_rid> 里的 web_rid 是主播的网页房号，和 reflow 页里的
# room id 不是一回事：拿 room id 去拼 live 地址，抖音会回「服务器开小差了」。
# reflow 页首屏 JSON 的 owner 对象里带着真正的 webRid。
_OWNER_RE = re.compile(r'"owner"\s*:\s*\{')
_WEB_RID_RE = re.compile(r'"webRid"\s*:\s*"(\d{4,})"')
_META_CACHE: dict[str, tuple[float, dict]] = {}
_META_TTL_S = 60.0        # 同一个房间一分钟内不重复抓：开一路只该花这一次
_META_MAX = 32            # 房间上限是 5，留余量；超出直接清空，不做 LRU


def _json_str(raw: str) -> str:
    """JSON 字符串字面量 -> 真字符串（\\uXXXX、&amp; 这类都能吃）。"""
    try:
        return json.loads('"' + raw + '"')
    except ValueError:
        return html.unescape(raw)


def _tidy(s: str) -> str:
    """页面上抓来的文本：去掉换行和多余空白，挡掉 $undefined 这类占位。"""
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if not t or t.startswith("$") or t.lower() in ("undefined", "null", "none"):
        return ""
    return t


def parse_room_meta(text: str) -> dict:
    """reflow 页 HTML -> {"nickname", "title", "web_rid"}。

    页面后半段是 RSC 的内嵌 JSON，引号被转义成 \\"，先摊平再匹配，正则只
    需要认一种写法。抽成纯函数是为了能拿真页面的样本离线测。web_rid 是
    live.douyin.com 的房号，和 reflow 路径里的 room id 不是一回事。
    """
    flat = (text or "").replace('\\"', '"').replace("\\/", "/")
    out: dict[str, str] = {}
    nick = _NICK_RE.search(flat)
    if nick:
        out["nickname"] = _tidy(_json_str(nick.group(1)))
    # "room":{ 在页面里不止一处，取第一个真的带 title 的：标题不在 room 对象
    # 里的那些多半是状态/配置片段，硬取会拿到不相干的字符串。窗口只给 2000
    # 字符：真实页面里 title 就在 id/idStr/status 后面几十个字符，给太大反而
    # 容易越过这个对象、抓到隔壁对象的 title。
    for m in _ROOM_RE.finditer(flat):
        t = _TITLE_RE.search(flat[m.end(): m.end() + 2000])
        if t:
            title = _tidy(_json_str(t.group(1)))
            if title:
                out["title"] = title
                break
    if not out.get("nickname"):
        # 兜底：owner 对象结构变了，就认页面里第一个像人名的 nickname。
        m = re.search(r'"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"', flat)
        if m:
            out["nickname"] = _tidy(_json_str(m.group(1)))
    # 房间的 web_rid：页面里观众/粉丝团对象也可能带 webRid，先锚定 owner，
    # 只从 owner 对象往后这一段里取第一个，避免拿到别人的号。
    owner = _OWNER_RE.search(flat)
    if owner:
        rid = _WEB_RID_RE.search(flat[owner.end(): owner.end() + 20000])
        if rid:
            out["web_rid"] = rid.group(1)
    return {k: v for k, v in out.items() if v}


def page_url(meta: dict) -> str:
    """房间信息 -> 能在浏览器里打开的直播间地址；拿不到 web_rid 返回空串。

    绝不能拿 reflow 里的 room id 顶替 web_rid：那个地址抖音只回
    「服务器开小差了」，看起来像直播间开着却没有任何画面。
    """
    rid = str((meta or {}).get("web_rid") or "").strip()
    return "https://live.douyin.com/" + rid if rid.isdigit() else ""


def _remember_meta(landing: str, meta: dict) -> None:
    """缓存一次抓取到的房间信息（直取解析完顺手留给 page_url 用）。"""
    if not landing or not meta:
        return
    if len(_META_CACHE) >= _META_MAX:
        _META_CACHE.clear()
    _META_CACHE[landing] = (time.time(), dict(meta))


def room_meta(url: str, timeout: float = 8.0) -> dict:
    """链接 -> 房间信息（主播名 / 标题 / web_rid）。抓不到就返回空字典。

    开播时补名字、算「显示页面」地址都会用它；同一个房间 60 秒内只抓一次，
    所以直取失败后重试、自愈重建都不会为同一张 reflow 页反复联网。
    """
    key = str(url or "").strip()
    # 解析成功时会把 meta 同时记在原始短链和落地页两个键上；先查原始链接，
    # 避免为了拿缓存又跟一次可能超时的短链跳转。
    hit = _META_CACHE.get(key)
    if hit and time.time() - hit[0] < _META_TTL_S:
        return dict(hit[1])
    try:
        # 用调用方的超时：补名字只是锦上添花，不能拿默认的 10 秒去堵开播。
        landing = _landing_url(url, timeout)
    except Exception:
        return {}
    if not landing:
        return {}
    hit = _META_CACHE.get(landing)
    if hit and time.time() - hit[0] < _META_TTL_S:
        return dict(hit[1])
    try:
        text, _cost = _fetch_reflow(landing, timeout)
    except Exception:
        return {}
    meta = parse_room_meta(text)
    _remember_meta(landing, meta)
    return dict(meta)


def display_name(meta: dict, limit: int = 60) -> str:
    """房间名：主播名 · 房间标题（缺哪个用哪个）。

    超长先砍标题尾巴、保住主播名——卡片和飞书里都是靠主播名认人的。
    上限 60 和服务端 name[:60] 对齐，别让好名字在最后一步被硬切。
    """
    nick = _tidy(meta.get("nickname"))
    title = _tidy(meta.get("title"))
    if not nick and not title:
        return ""
    if not title:
        return nick[:limit]
    if not nick:
        return title[:limit]
    head = nick + " · "
    room = limit - len(head)
    if len(title) <= room:
        return head + title
    if room < 4:                 # 主播名自己就快吃满额度了，标题只能留很短
        return (head + title[:max(1, room - 1)] + "…")[:limit]
    return head + title[:room - 1] + "…"


def _resolve_streamlink(target: str, timeout: float,
                        quality: str = _DEFAULT_QUALITY) -> tuple[str, str]:
    """旧解析路径：streamlink 的抖音插件。reflow 页结构变化时兜底用。"""
    if not _module_available("streamlink"):
        raise ResolveError("未安装 streamlink")
    pick = "worst" if _quality_order(quality)[0].startswith(("LD", "SD")) else "best"
    cmd = [sys.executable, "-m", "streamlink", "--loglevel", "error",
           "--stream-url", target, pick]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ResolveError("streamlink 解析超时（%.0f 秒）" % timeout,
                           retryable=True)
    except FileNotFoundError as e:
        raise ResolveError("启动 streamlink 失败：%s" % e)
    out = [ln.strip() for ln in (r.stdout or "").splitlines()
           if ln.strip().startswith("http")]
    if r.returncode != 0 or not out:
        err = ((r.stderr or "").strip().splitlines() or ["streamlink 解析失败"])[-1]
        raise ResolveError(err[:300], retryable=True)
    return out[-1], "streamlink %.1fs" % (time.monotonic() - t0)


def _join_errors(*parts: str) -> str:
    return "；".join([p for p in parts if p]) or "没能解析出直播流地址"


def resolve_douyin(url: str, timeout: float = RESOLVE_TIMEOUT_S,
                   quality: str = _DEFAULT_QUALITY) -> tuple[str, str]:
    """抖音直播页 -> 可播放的 FLV/HLS 地址。返回 (stream_url, note)。

    顺序按「地址保质期」排：
      1. live.douyin.com/<web_rid>：房号跨场次稳定，页面里带着当前场次的
         stream_url，优先用它；
      2. reflow 页里现成的播放地址（页面没改版时仍可用）；
      3. streamlink。
    reflow 里的 room id 一场直播一个，锚点重新开播后那张页面永远停在上一场，
    还继续吐上一场的播放地址——**必须先判「直播已结束」再取地址**，否则会
    把一个死地址交给 ffmpeg，反复 404。live 页则永远是最新一场的。

    分享短链（v.douyin.com）只跟一次跳转，落地地址同时用于 reflow 和 live
    两种推导；跳转本身失败时直接报「打不开短链」，不伪装成「没有流地址」。
    """
    u = (url or "").strip()
    # 短链只跟一次跳转，reflow 和 live 共用同一个落地地址。以前两边各跟
    # 一次：第一次超时、第二次成功落在 reflow 时，_live_target 只认 live
    # 页会把它丢掉，最后只剩一句「没能解析出直播流地址」。跳转失败也必须
    # 说清是短链打不开，而不是让人以为直播间没有流。
    if is_douyin(u) and not _LIVE_RE.match(u) and not _REFLOW_RE.search(
            urllib.parse.urlsplit(u).path):
        final, redirect_err = _redirect_target(u, timeout)
        if not final:
            raise ResolveError(
                "打不开抖音直播间链接（跳转失败/超时：%s），稍后重试"
                % (redirect_err or "未知错误"), retryable=True)
        u = final
        if not _LIVE_RE.match(u) and not _REFLOW_RE.search(
                urllib.parse.urlsplit(u).path):
            raise ResolveError(
                "抖音链接没有落到直播页（跳到了 %s），可能已下播或链接不是直播间"
                % u[:160])
    landing = _landing_url(u, timeout)
    live_url = "" if landing else _live_target(u, timeout)
    if not landing and not live_url and not is_douyin(u):
        raise ResolveError("无法从链接里找到抖音直播间房号")

    reflow_err = ""
    reflow_retryable = False
    reflow_text = ""
    reflow_cost = 0.0
    meta: dict = {}
    if landing:
        try:
            reflow_text, reflow_cost = _fetch_reflow(landing, timeout)
        except Exception as e:
            reflow_err = "抓取直播页失败：%s" % (str(e)[:120] or type(e).__name__)
            reflow_retryable = True
        else:
            # 同一次抓取顺手把房间信息（含 web_rid）缓存下来：后面算页面地址、
            # 补房间名都不再为同一张页面多跑一次 HTTP。
            meta = parse_room_meta(reflow_text)
            _remember_meta(landing, meta)
            # 原始短链也缓存一份：后面补名字/算显示地址时即使跳转又抖了，
            # 也能直接命中这次已经解析出来的 web_rid。
            _remember_meta(url, meta)
            _remember_meta(u, meta)

    web_rid = (live_url.rsplit("/", 1)[-1] if live_url
               else str(meta.get("web_rid") or ""))

    # 第一选择：live 页。跨场次稳定的房号 + 当前场次的地址。
    live_err = ""
    live_retryable = False
    if web_rid:
        try:
            return _resolve_live_page(web_rid, timeout, quality)
        except ResolveError as e:
            live_err = str(e)
            live_retryable = bool(getattr(e, "retryable", False))

    # 第二选择：reflow 页里的地址。先判下播/故障，再取地址——顺序反了就会
    # 把已结束那一场残留的播放地址当成可用的。
    if reflow_text:
        for mark in _OFFLINE_MARKS:
            if mark in reflow_text:
                raise ResolveError(_join_errors(
                    live_err, "直播间当前没有开播（页面显示「%s」）" % mark))
        for mark in _PAGE_ERROR_MARKS:
            if mark in reflow_text:
                raise ResolveError(_join_errors(
                    live_err, "抖音页面返回「%s」（临时故障，不一定是没开播），稍后重试"
                    % mark), retryable=True)
        stream = _pick_stream_url(reflow_text, quality)
        if stream:
            return stream, "reflow %.1fs" % reflow_cost
        reflow_err = "reflow 页里没有找到直播流地址"

    target = live_url or ("https://live.douyin.com/" + web_rid if web_rid else "")
    if not target:
        raise ResolveError(_join_errors(live_err, reflow_err),
                           retryable=live_retryable or reflow_retryable)
    try:
        return _resolve_streamlink(target, timeout, quality)
    except ResolveError as e:
        # streamlink 对任何非零退出都标 retryable；不能用它覆盖前两条路径
        # 已经给出的确定结论（例如「页面明确没有流」），否则会把「改版/
        # 没开播」误报成网络问题并多等两轮补试。
        raise ResolveError(_join_errors(
            live_err, "%s；streamlink 兜底也失败：%s" % (reflow_err, e)),
            retryable=(live_retryable or reflow_retryable))


def spawn_ffmpeg(stream: str) -> subprocess.Popen:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg，请安装并加入 PATH")
    # info + nostats：启动时打印输入流码率/分辨率，供流量估算；不打印周期性
    # 进度行，stderr 仍然是「有错才长」的形状，旧看门狗不受影响。
    cmd = [exe, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
           "-i", stream, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0, creationflags=_NO_WINDOW)


def pcm_rms(data: bytes) -> float:
    arr = array.array("h")
    arr.frombytes(data[: len(data) // 2 * 2])
    if not arr:
        return 0.0
    return math.sqrt(sum(x * x for x in arr) / len(arr)) / 32768.0


def post_chunk(base: str, sid: str, data: bytes, timeout: float = 5.0) -> None:
    """POST 一块裸 PCM。404 单独抛，调用方据此重建会话。"""
    u = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname or "127.0.0.1",
                                      u.port or 80, timeout=timeout)
    try:
        conn.request("POST", "/api/live/chunk", body=data, headers={
            "Content-Type": "application/octet-stream",
            "X-Live-Id": sid,
            "Content-Length": str(len(data)),
        })
        resp = conn.getresponse()
        body = resp.read(300)
        if resp.status == 404:
            raise SessionGone("会话已失效")
        if resp.status >= 400:
            raise RuntimeError("HTTP %s %s" % (
                resp.status, body.decode("utf-8", "replace")[:120]))
    finally:
        conn.close()


_GONE_RE = re.compile(r"(?:Server returned|HTTP error)\s*(?:403|404)"
                      r"|(?:403|404)\s*(?:Not Found|Forbidden)", re.I)

# -loglevel info 会把输入流元数据也打到 stderr；只把像错误的行留在
# stderr_tail 里，保持旧逻辑「tail 最后一行就是失败原因」的形状。
_ERR_MARKS = ("error", "failed", "failure", "403", "404", "forbidden",
              "not found", "invalid", "timed out", "timeout",
              "connection refused", "connection reset", "unable",
              "could not", "cannot", "broken pipe", "no such")


def _looks_error(text: str) -> bool:
    low = str(text or "").lower()
    return any(mark in low for mark in _ERR_MARKS)


def _stream_gone(text: str) -> bool:
    """ffmpeg stderr 里是不是「这个地址已经失效」——下播和过期都长这样。

    别拿 rc 判断：Windows 上 CDN 拒绝时 ffmpeg 的退出码是 3419392776 这种
    32 位值（0xCBCFCB08），看不出任何信息，真正的原因只在 stderr 里。
    """
    return bool(_GONE_RE.search(str(text or "")))


_STREAM_ID_RE = re.compile(r"stream-(\d+)")


def _stream_key(url: str) -> str:
    """播放地址的「身份」：把每次都变的签名参数去掉，只留 stream id。

    重新解析一次签名/时间戳必然变，但同一场直播的 stream id 不变；换了
    stream id 才是新的一场。所以判断「还是不是同一个死地址」只能比这个。
    """
    text = str(url or "")
    m = _STREAM_ID_RE.search(urllib.parse.urlsplit(text).path)
    if m:
        return "stream-" + m.group(1)
    return urllib.parse.urlsplit(text).path or text


def _kill_proc(proc) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    except Exception:
        pass


class DirectAgent(Agent):
    """房间 = 一个 ffmpeg 子进程；失败默认停在「待切换」，用户确认后才用浏览器。"""

    adopt_on_start = True
    engine = "direct"

    def __init__(self, base: str) -> None:
        # direct 不需要 Chrome；只有用户确认切换后才懒加载浏览器。
        super().__init__(base, need_browser=False)
        self.fallback: Agent | None = None
        self.fallback_engine = ""
        self.fallback_rooms: set[str] = set()
        # ask（默认）：失败后等人确认；auto：旧的无感回退；none：只报失败。
        mode = os.environ.get("LIVENOTES_DIRECT_FALLBACK", "").strip().lower()
        self.fallback_mode = mode if mode in FALLBACK_MODES else "ask"
        # 画质与流量计量走设置页热更新；画质对下一路解析生效，精确计量对
        # 下一次启动 ffmpeg 生效。环境变量只作临时覆盖。
        self.direct_quality = _DEFAULT_QUALITY
        self.traffic_exact = bool(_env_traffic_proxy())
        # 网络类解析失败：同一房间最多补试两次。明确下播/链接不对不补试。
        self.resolve_retries: dict[str, int] = {}
        self.resolve_retry_at: dict[str, float] = {}
        self.auto_restarts: dict[str, int] = {}
        self.restart_at: dict[str, float] = {}

    def apply_config(self, cfg: dict) -> None:
        """设置页热更新失败策略 / 画质 / 精确计量；环境变量仍优先。"""
        cfg = cfg or {}
        mode = str(cfg.get("direct_fallback") or "").strip().lower()
        if mode in FALLBACK_MODES:
            env = os.environ.get("LIVENOTES_DIRECT_FALLBACK", "").strip().lower()
            if env in FALLBACK_MODES:
                mode = env
            if mode != self.fallback_mode:
                print("直取失败处理：%s -> %s" % (self.fallback_mode, mode),
                      flush=True)
                self.fallback_mode = mode
                # 没在用的回退代理直接丢掉，下一次按新策略重新选引擎；
                # 已经有房间在回退浏览器里跑时保留，避免把正在录的一路打断。
                if not self.fallback_rooms and self.fallback is not None:
                    old, self.fallback, self.fallback_engine = (
                        self.fallback, None, "")
                    try:
                        old.shutdown()
                    except Exception:
                        pass

        quality = str(cfg.get("direct_quality") or "").strip().lower()
        if quality in _QUALITY_ORDERS and quality != self.direct_quality:
            print("直取画质：%s -> %s（下一路解析生效）"
                  % (self.direct_quality, quality), flush=True)
            self.direct_quality = quality

        exact = cfg.get("traffic_exact")
        env_proxy = _env_traffic_proxy()
        if env_proxy is not None:
            exact = env_proxy
        if isinstance(exact, bool) and exact != self.traffic_exact:
            print("直取流量计量：%s -> %s（下一次启动 ffmpeg 生效）"
                  % ("实测" if self.traffic_exact else "估算",
                     "实测" if exact else "估算"), flush=True)
            self.traffic_exact = exact

    # ---------------------------------------------------------------- 工具
    def _resolve_target(self, room: dict) -> str:
        """解析用的链接：优先已经落到的 live.douyin.com/<web_rid> 房号页。

        web_rid 跨场次稳定，重试/自愈时没必要再跟一次分享短链——短链跳转
        恰恰是整条链上最容易超时的一步。resolved_url 只在页面确认过
        owner.webRid 后才写入，可以放心当入口。
        """
        return str((room or {}).get("resolved_url")
                   or (room or {}).get("url") or "").strip()

    def _clear_resolve_retry(self, rid: str) -> None:
        self.resolve_retries.pop(rid, None)
        self.resolve_retry_at.pop(rid, None)

    def _retry_resolve(self, room: dict, err: Exception) -> bool:
        """网络类解析失败：安排一次补试，返回 True 表示这次先不判死。

        明确下播 / 链接不是直播间 / 没装 streamlink 都不补试——补试只会
        让用户多等十几秒，最后还是同一个结果。
        """
        rid = room["id"]
        if not getattr(err, "retryable", False):
            self._clear_resolve_retry(rid)
            return False
        n = int(self.resolve_retries.get(rid) or 0)
        if n >= len(RESOLVE_RETRY_DELAYS):
            self._clear_resolve_retry(rid)
            return False
        delay = RESOLVE_RETRY_DELAYS[n]
        self.resolve_retries[rid] = n + 1
        self.resolve_retry_at[rid] = time.time() + delay
        print("[%s] 解析失败（网络类），%.0f 秒后补试（第 %d 次）：%s"
              % (rid, delay, n + 1, str(err)[:120]), flush=True)
        self.report(rid, "starting", stage=1,
                    note="解析失败，%.0f 秒后自动重试（第 %d 次）：%s"
                         % (delay, n + 1, str(err)[:80]),
                    engine="direct", session_id=room.get("session_id") or None)
        return True

    def _title(self, room: dict) -> str:
        name = str(room.get("name") or "").strip()
        if not name:
            tail = str(room.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            name = "抖音 " + (tail or "直播")
        return "监听中心 · " + name[:60]

    def _capture_event(self, rid: str, kind: str, **extra) -> None:
        ev = {"kind": kind}
        ev.update(extra)
        self.report(rid, engine="direct", capture_event=ev)

    def _new_info(self, proc, sid: str, stream: str, adopted: bool = False) -> dict:
        now = time.time()
        return {
            "proc": proc, "engine": "direct", "sid": sid, "stream": stream,
            "started": now, "last_chunk_at": now, "last_ok_at": now,
            "chunks": 0, "restarts": 0, "first_audio_ms": None,
            "first_chunk": threading.Event(), "live_reported": False,
            "warned": False,
            "retry_at": 0.0, "renew_at": 0.0, "send_errors": 0,
            "adopted": adopted, "fallback": False, "rms": 0.0,
            # 全零音频的起点（墙钟，0 = 当前有声）；probed_at 是上次页面探针时间。
            "zero_since": 0.0, "probed_at": 0.0, "probing": False, "ended": False,
            "gen": 0, "recovering": False, "renewing": False,
            "eof": False, "stderr_tail": "", "stderr_log": "",
            "traffic": None, "traffic_proxy": None, "stream_input": stream,
        }

    def _spawn_ffmpeg(self, rid: str, stream: str, info: dict):
        """流地址 ->（可选计数代理）-> ffmpeg 子进程。

        proxy 由设置页「精确流量计量」启用（环境变量可临时覆盖）；启动失败
        退回直连，并把计量标成不可用——绝不能因为「量流量」把录音搞停。
        """
        meter = info.get("traffic") or self._meter(rid)
        info["traffic"] = meter
        if info.get("traffic_proxy") is not None:
            # 旧代理停掉后精确计数就结束了；_stop_traffic 负责把 meter 降级，
            # 否则它永远锁在 exact，后面忽略所有估算。
            self._stop_traffic(info)
        target = stream
        proxy = None
        if self.traffic_exact:
            def on_bytes(n: int) -> None:
                meter.set_source("proxy", "计数代理实测")
                meter.add_platform(n)
            try:
                proxy = CountingProxy(stream, on_bytes=on_bytes)
                target = proxy.start()
            except Exception as e:
                proxy = None
                target = stream
                meter.demote_source(
                    "计数代理启动失败，改按码率估算：%s" % str(e)[:160])
        else:
            # 设置页把精确计量关掉了：之前这一路可能还是 proxy 源，必须主动
            # 降级；set_source("unavailable") 在 exact 状态下会被拒绝。
            meter.demote_source("精确流量计量已关闭，改按码率估算")
        info["traffic_proxy"] = proxy
        info["stream_input"] = target
        return spawn_ffmpeg(target)

    def _start_reader(self, rid: str, room: dict, info: dict) -> None:
        gen = int(info.get("gen") or 0)
        threading.Thread(target=self._read_loop, args=(rid, room, info, gen),
                         name="direct-" + rid, daemon=True).start()

    def _start_stderr(self, proc, info: dict, gen: int) -> None:
        threading.Thread(target=self._drain_stderr, args=(proc, info, gen),
                         name="direct-err", daemon=True).start()

    def _drain_stderr(self, proc, info: dict, gen: int) -> None:
        try:
            while True:
                line = proc.stderr.readline()
                if not line:
                    return
                if int(info.get("gen") or 0) != gen:
                    return
                text = line.decode("utf-8", "replace").strip()
                if text:
                    info["stderr_log"] = (str(info.get("stderr_log") or "")
                                          + text + "\n")[-4000:]
                    if _looks_error(text):
                        info["stderr_tail"] = (str(info.get("stderr_tail") or "")
                                               + text + "\n")[-800:]
                    meter = info.get("traffic")
                    if meter is not None:
                        # 启动时的 Input/Stream 行带码率或分辨率；拿不到精确
                        # 代理时用这个估算，至少给出量级。
                        kb = parse_bitrate_kbps(text)
                        if kb <= 0:
                            w, h = parse_video_resolution(text)
                            kb = resolution_kbps(w, h)
                        if kb > 0:
                            meter.set_estimate_kbps(
                                kb, "按 ffmpeg 码率/分辨率估算")
        except Exception:
            return

    def _report_page_url(self, rid: str, room: dict) -> str:
        """把真正的 live.douyin.com/<web_rid> 回报给引擎，供「显示页面」打开。

        直取只需要 reflow 页，但那里面的 room id 不是 web_rid；旧实现拿它拼
        地址，前端打开只会看到「服务器开小差了」。这里用 reflow 页 owner 的
        webRid；拿不到就不报，前端会退回打开 reflow 页。
        """
        current = str(room.get("resolved_url") or "").strip()
        if current:
            return current            # 已经是真房号页，不用再跟一次短链
        try:
            meta = room_meta(room.get("url") or "", timeout=5.0)
        except Exception:
            return ""
        target = page_url(meta)
        if not target or target == room.get("url"):
            return ""
        room["resolved_url"] = target
        self.report(rid, url=target)
        return target

    # ---------------------------------------------------------------- 启动
    def bring_up(self, room: dict, session: str = "") -> None:
        rid = room["id"]
        if rid in self.running:
            return
        self.report(rid, "starting", stage=1, note="正在解析抖音流地址",
                    engine="direct", session_id=session or None)
        if not is_douyin(room.get("url") or ""):
            # 先判断再建会话：非抖音链接让浏览器自己建 tab 会话，
            # 不要留下一个 source_mode=direct 的空场次。
            self._fallback(room, {"sid": session or ""},
                           "direct 只支持抖音，改用浏览器采集")
            return
        # 先把名字补上：会话标题（飞书推送、导出文件名都用它）拼的是房间名。
        # 用户手动改过的名字（name_source=user）和采集端已经写过的真名都不要动。
        # 补试时跳过：名字只是锦上添花，不能一轮轮拖慢解析。
        src = str(room.get("name_source") or "")
        if (not self.resolve_retries.get(rid)
                and (src == "share"
                     or (src == "" and not str(room.get("name") or "").strip()))):
            try:
                nm = display_name(room_meta(room.get("url") or "", timeout=5.0))
            except Exception:
                nm = ""
            if nm and nm != room.get("name"):
                room["name"] = nm
                room["name_source"] = "page"
                self.report(rid, name=nm)

        # 先解析、后建会话：解析失败（尤其是短链打不开）时不留一个空会话，
        # 看门狗也不用再跑一次「断流收尾」。网络类失败先补试，不立刻判死。
        t0 = time.monotonic()
        try:
            stream, note = resolve_douyin(self._resolve_target(room),
                                          quality=self.direct_quality)
        except Exception as e:
            self._capture_event(rid, "resolve_failed", error=str(e)[:200])
            if self._retry_resolve(room, e):
                return
            self._fallback_or_fail(room, session or "", "解析失败：%s" % e, 1)
            return
        self._clear_resolve_retry(rid)
        resolve_ms = int((time.monotonic() - t0) * 1000)
        self._capture_event(rid, "resolve_ok", ms=resolve_ms, note=note)
        # 解析时已经抓过 reflow 页，meta 在缓存里：顺手把真实房号报给引擎，
        # 「显示页面」才有正确的 live.douyin.com 地址可开。
        self._report_page_url(rid, room)

        # 解析期间用户可能已经点了停止/删除：不再建会话、不再拉流。
        st = self._room_state(rid)
        if st in ("stopped", "failed", "__missing__"):
            return

        sid = str(session or "")
        if not sid:
            try:
                res = api(self.base, "/api/live/start", {
                    "title": self._title(room), "source_mode": "direct",
                }, timeout=60)
                sid = str(res.get("session_id") or "")
            except Exception as e:
                self.fail(rid, "建会话失败：%s" % e, 1)
                return
            if not sid:
                self.fail(rid, "建会话没返回 session_id", 1)
                return
            # 建会话期间用户可能已经点了停止/删除：这条会话没有房间接管，
            # 先收掉，别让它变成孤儿（停止后迟到的上报也不该再点亮房间）。
            st = self._room_state(rid)
            if st in ("stopped", "failed", "__missing__"):
                self._stop_session(sid)
                return
        self.report(rid, "starting", stage=2,
                    note="已解析流地址（%.1f 秒），正在启动 ffmpeg"
                         % (resolve_ms / 1000.0),
                    engine="direct", session_id=sid)

        info = self._new_info(None, sid, stream)
        info["traffic"] = self._meter(rid, reset=not session)
        try:
            proc = self._spawn_ffmpeg(rid, stream, info)
        except Exception as e:
            self._stop_traffic(info)
            self._fallback_or_fail(room, sid, "ffmpeg 启动失败：%s" % e, 2)
            return
        info["proc"] = proc
        info["resolve_ms"] = resolve_ms
        self.running[rid] = info
        self.beat(rid, True, info)
        self._start_stderr(proc, info, 0)
        self._start_reader(rid, room, info)
        print("[%s] 直取已启动（session=%s，解析 %.1fs）"
              % (rid, sid, resolve_ms / 1000.0), flush=True)

        if not info["first_chunk"].wait(FIRST_CHUNK_WAIT_S):
            self.report(rid, "starting", stage=4,
                        note="ffmpeg 已启动，等待第一块音频",
                        engine="direct", session_id=sid)

    def _read_loop(self, rid: str, room: dict, info: dict, gen: int) -> None:
        proc = info.get("proc")
        if proc is None or proc.stdout is None:
            return
        buf = bytearray()
        try:
            while True:
                data = proc.stdout.read(CHUNK_BYTES)
                if not data:
                    break
                if int(info.get("gen") or 0) != gen:
                    return
                buf.extend(data)
                while len(buf) >= CHUNK_BYTES:
                    chunk = bytes(buf[:CHUNK_BYTES])
                    del buf[:CHUNK_BYTES]
                    if int(info.get("gen") or 0) != gen:
                        return
                    self._on_chunk(rid, room, info, chunk)
        except Exception:
            if int(info.get("gen") or 0) == gen:
                traceback.print_exc()
        finally:
            if int(info.get("gen") or 0) == gen:
                info["eof"] = True

    def _on_chunk(self, rid: str, room: dict, info: dict, chunk: bytes) -> None:
        sid = str(info.get("sid") or "")
        if not sid:
            return
        info["rms"] = pcm_rms(chunk)
        info["chunks"] = int(info.get("chunks") or 0) + 1
        info["last_chunk_at"] = time.time()
        meter = info.get("traffic")
        if meter is not None:
            meter.add_audio(len(chunk))
        # 全零不是"主播安静"：安静的直播间有底噪，只有断掉的流才是精确的 0。
        if info["rms"] < DIRECT_ZERO_RMS:
            if not info.get("zero_since"):
                info["zero_since"] = info["last_chunk_at"]
        else:
            info["zero_since"] = 0.0
        if not info["first_chunk"].is_set():
            info["first_audio_ms"] = int(
                (time.time() - float(info.get("started") or time.time())) * 1000)
            info["first_chunk"].set()
            self.auto_restarts.pop(rid, None)
            self._capture_event(rid, "first_audio", ms=info["first_audio_ms"])
        try:
            post_chunk(self.base, sid, chunk)
            info["last_ok_at"] = time.time()
            info["send_errors"] = 0
            if not info.get("live_reported"):
                # 只有服务端真的收下音频块，才把房间报成 live。旧顺序先报再发：
                # 用户停止后迟到的第一块会把 stopped 重新点亮，_renew_session
                # 再看到 live 就重建会话——「点了停止还在录」会从这里复活。
                info["live_reported"] = True
                self.report(rid, "live", stage=6, note="音频采集已启动",
                            engine="direct", session_id=sid,
                            switch_ready=False, switch_requested=False,
                            switch_reason="")
        except SessionGone:
            self._renew_session(rid, room, info)
        except Exception as e:
            n = int(info.get("send_errors") or 0) + 1
            info["send_errors"] = n
            if n == 1 or n % 50 == 0:
                print("[%s] 发送音频块失败（第 %d 次）：%s" % (rid, n, e), flush=True)

    def _room_state(self, rid: str) -> str:
        """问服务端这个房间现在是什么状态。查不到返回空串。

        只有服务端明确给出了房间列表、里面却没有这个 id，才算「房间已删除」；
        响应形状不对（缺 rooms 字段）一律按「不知道」处理——宁可按原计划
        继续，也不要在刚建好会话后误把用户还在启动的房间收掉。
        """
        try:
            payload = api(self.base, "/api/rooms", timeout=2.5)
        except Exception:
            return ""
        rooms = (payload or {}).get("rooms")
        if not isinstance(rooms, list):
            return ""
        for r in rooms:
            if str(r.get("id") or "") == rid:
                return str(r.get("state") or "")
        return "__missing__"

    def _renew_session(self, rid: str, room: dict, info: dict) -> None:
        """服务重启/会话被清后 404：只有房间还在听时才建新会话接着录。

        用户点「停止」也会让会话 404；旧版不看房间状态就重建，结果
        「点了停止还在录」。房间已停止/删除时直接回收 ffmpeg。
        """
        if info.get("renewing"):
            return
        now = time.time()
        if now < float(info.get("renew_at") or 0.0):
            return
        st = self._room_state(rid)
        if st not in ("live", "starting"):
            if st in ("stopped", "failed", "__missing__"):
                self.reap(rid, "房间已停止/删除", stop_session=False)
            else:
                # 服务暂时不可达：下一块再试，但先压一个重试间隔，别让读线程
                # 每个音频块都打一次 2.5 秒超时的 /api/rooms。
                info["renew_at"] = now + 3.0
            return
        info["renewing"] = True
        info["renew_at"] = now + 3.0
        try:
            res = api(self.base, "/api/live/start", {
                "title": self._title(room), "source_mode": "direct",
            }, timeout=60)
            sid = str(res.get("session_id") or "")
            if not sid:
                return
            info["sid"] = sid
            info["first_chunk"].clear()
            info["live_reported"] = False
            self.report(rid, "live", stage=6, note="会话已失效，已重建",
                        engine="direct", session_id=sid)
            print("[%s] 会话已失效，重建 session=%s" % (rid, sid), flush=True)
        except Exception as e:
            print("[%s] 会话失效，重建失败：%s" % (rid, e), flush=True)
        finally:
            info["renewing"] = False

    # ---------------------------------------------------------------- 守护
    def _check(self, room: dict, info: dict) -> None:
        if (info.get("fallback") or info.get("recovering")
                or info.get("renewing") or info.get("ended")):
            # 页面探针已经判过下播：ffmpeg 随后退出是预期动作，不能再
            # 走进 _recover 把一路已结束的直播重新拉起来。
            return
        rid = room["id"]
        now = time.time()
        proc = info.get("proc")
        if proc is not None and proc.poll() is not None:
            tail = str(info.get("stderr_tail") or "").strip().splitlines()
            last = tail[-1].strip()[:160] if tail else ""
            # stderr 才是原因，rc 只是附注：Windows 上 ffmpeg 的退出码是个
            # 看不懂的 32 位值（比如 3419392776），排障时最没用的就是它。
            why = "ffmpeg 退出（%s）" % (
                ("%s；rc=%s" % (last, proc.returncode)) if last
                else "rc=%s" % proc.returncode)
            self._recover(room, info, why, gone=_stream_gone("\n".join(tail)))
            return
        if info.get("eof"):
            self._recover(room, info, "ffmpeg 输出结束")
            return
        last_ok = float(info.get("last_ok_at") or info.get("started") or now)
        idle = now - last_ok
        if idle >= DIRECT_RECOVER_AFTER_S:
            if now < float(info.get("retry_at") or 0.0):
                return
            self._recover(room, info, "%.0f 秒没有成功送出音频块" % idle)
            return
        if idle >= DIRECT_WARN_S and not info.get("warned"):
            info["warned"] = True
            self.report(rid, "live", stage=6,
                        note="采集断流：%.0f 秒没有音频块" % idle,
                        engine="direct", session_id=info.get("sid"))
        elif idle < 5:
            info["warned"] = False
        # 块照来但内容全零：链路和 ffmpeg 都"正常"，去页面问一次到底还在不在播。
        zero_since = float(info.get("zero_since") or 0.0)
        if (zero_since and not info.get("ended")
                and now - zero_since >= DIRECT_ZERO_PROBE_AFTER_S
                and now - float(info.get("probed_at") or 0.0) >= DIRECT_ZERO_PROBE_EVERY_S):
            info["probed_at"] = now
            threading.Thread(target=self._probe_zero_room, args=(room, info),
                             name="direct-probe-" + rid, daemon=True).start()

    def _probe_zero_room(self, room: dict, info: dict) -> None:
        """静音持续太久：抓页面确认是不是已经下播。

        探不到（网络/反爬/页面改版）一律当还在播，绝不因为一次 HTTP 失败就把
        场次收掉；真下播了才落 stopped 并停会话，WAV/总结/回顾走正常收尾。
        """
        rid = room["id"]
        if info.get("probing") or info.get("ended"):
            return
        info["probing"] = True
        try:
            online, why = probe_room_live(room)
            if online or not why:
                return
            info["ended"] = True
            print("[%s] 静音判下播，自动收尾：%s" % (rid, why), flush=True)
            self._capture_event(rid, "ended", reason=why[:200])
            self.report(rid, "stopped", stage=6, note="自动收尾：" + why,
                        auto_stopped=True, auto_stop_reason=why,
                        engine="direct", session_id=info.get("sid"))
            sid = str(info.get("sid") or "")
            if sid:
                threading.Thread(target=self._stop_session, args=(sid,),
                                 name="end-" + rid, daemon=True).start()
        except Exception:
            traceback.print_exc()
        finally:
            info["probing"] = False

    def _recover(self, room: dict, info: dict, reason: str,
                 gone: bool = False) -> None:
        rid = room["id"]
        if info.get("recovering"):
            return
        info["recovering"] = True
        try:
            n = int(info.get("restarts") or 0) + 1
            if n > DIRECT_MAX_RESTARTS:
                self._fallback(room, info, "重建 %d 次仍失败：%s"
                               % (DIRECT_MAX_RESTARTS, reason))
                return
            info["restarts"] = n
            print("[%s] 直取断流：%s；第 %d 次重建" % (rid, reason, n), flush=True)
            self._capture_event(rid, "reconnect", reason=reason[:200])
            self.report(rid, "live", stage=6,
                        note="采集断流，正在重建（第 %d 次）" % n,
                        engine="direct", session_id=info.get("sid"))

            old = info.get("proc")
            info["gen"] = int(info.get("gen") or 0) + 1
            info["proc"] = None
            info["eof"] = False
            info["warned"] = False
            info["first_chunk"].clear()
            info["chunks"] = 0
            info["last_ok_at"] = time.time()
            # 重建出来的是一路新流：静音计时和探针节流都从头开始，
            # 但 ended 不清——已经判过下播就不再探第二次。
            info["zero_since"] = 0.0
            info["probed_at"] = 0.0
            info["probing"] = False
            _kill_proc(old)

            try:
                stream, note = resolve_douyin(self._resolve_target(room),
                                              quality=self.direct_quality)
            except Exception as e:
                # ffmpeg 已经杀了，计数代理也别继续挂着：它会保留监听端口，
                # 并把已死连接上的字节继续记进这一路。
                self._stop_traffic(info)
                info["retry_at"] = time.time() + DIRECT_RETRY_S
                self.report(rid, "live", stage=6,
                            note="重新解析失败，%d 秒后重试：%s"
                                 % (int(DIRECT_RETRY_S), str(e)[:120]),
                            engine="direct", session_id=info.get("sid"))
                return
            if gone and _stream_key(stream) == _stream_key(info.get("stream")):
                # 重新解析拿回来的还是同一路流（签名/时间戳变了，stream id 没变）：
                # 不是 URL 过期，是这一场真的结束了。再重建两次只会把同一段
                # 404 重放一遍、白等半分钟。
                self._fallback(room, info,
                               "直播流已失效，重新解析拿到的还是同一个地址，"
                               "直播间应该已经下播（%s）" % reason[:120])
                return
            info["retry_at"] = 0.0
            try:
                proc = self._spawn_ffmpeg(rid, stream, info)
            except Exception as e:
                self._stop_traffic(info)
                info["retry_at"] = time.time() + DIRECT_RETRY_S
                self.report(rid, "live", stage=6,
                            note="ffmpeg 重启失败，%d 秒后重试：%s"
                                 % (int(DIRECT_RETRY_S), str(e)[:120]),
                            engine="direct", session_id=info.get("sid"))
                return
            info["proc"] = proc
            info["stream"] = stream
            info["started"] = time.time()
            info["last_ok_at"] = time.time()
            self.beat(rid, True, info)
            self._start_stderr(proc, info, int(info["gen"]))
            self._start_reader(rid, room, info)
            print("[%s] 直取已重建（%s）" % (rid, note), flush=True)
        finally:
            info["recovering"] = False

    # ---------------------------------------------------------------- 回退
    def _get_fallback(self) -> Agent | None:
        if self.fallback is not None:
            return self.fallback
        mode = self.fallback_mode
        # auto / ask 先试独立浏览器（最稳），不可用再试共享；显式 shared 则反过来。
        order = (["shared", "per_room"] if mode == "shared"
                 else ["per_room", "shared"] if mode in ("per_room", "auto", "ask")
                 else [])
        for engine in order:
            try:
                if engine == "shared":
                    exe = find_shared_browser()
                    self.fallback = SharedAgent(self.base, SharedBrowser(exe, self.base))
                    print("直取回退：共享浏览器（%s）" % exe, flush=True)
                else:
                    self.fallback = Agent(self.base)
                    print("直取回退：每房间一个浏览器", flush=True)
                self.fallback_engine = engine
                return self.fallback
            except SystemExit as e:
                print("浏览器回退不可用（%s）：%s" % (engine, e), flush=True)
        self.fallback = None
        return None

    def _fallback_or_fail(self, room: dict, sid: str, reason: str,
                          stage: int) -> None:
        """启动阶段失败：按 fallback_mode 决定是等人确认还是直接切浏览器。"""
        self._fallback(room, {"sid": sid}, reason, stage=stage)

    def _probe_fallback_engine(self) -> str:
        """只探测「能不能切」，不提前拉起浏览器。"""
        mode = self.fallback_mode
        if mode == "none":
            return ""
        if mode == "shared":
            try:
                find_shared_browser()
                return "shared"
            except SystemExit:
                pass
        try:
            find_chrome()
            return "per_room"
        except SystemExit:
            pass
        if mode in ("shared", "auto", "ask"):
            try:
                find_shared_browser()
                return "shared"
            except SystemExit:
                pass
        return ""

    def _switch_to_browser(self, room: dict, confirmed: bool = True) -> None:
        """切到浏览器采集。confirmed=False 用于设置页改成 auto 后的补切。"""
        rid = room["id"]
        reason = str(room.get("switch_reason") or room.get("error") or "直取失败")
        fb = self._get_fallback()
        if fb is None:
            why = "直取失败：%s（没有可用的浏览器采集）" % reason
            self.report(rid, "failed", stage=room.get("stage") or 1,
                        failed_stage=room.get("failed_stage") or 1, error=why,
                        engine="direct", switch_ready=False,
                        switch_requested=False, switch_reason=reason[:200])
            return
        sid = str(room.get("session_id") or "")
        if sid and not self.session_alive(room):
            # 旧会话可能已被断流看门狗收尾：让浏览器自己新建一场，别硬塞旧 id。
            sid = ""
        self._capture_event(rid, "switch_confirmed" if confirmed else "fallback",
                            reason=reason[:200], engine=self.fallback_engine)
        action = "用户确认切换" if confirmed else "按设置自动切换"
        print("[%s] %s，改用浏览器采集：%s" % (rid, action, reason), flush=True)
        label = "共享浏览器" if self.fallback_engine == "shared" else "独立浏览器"
        self.report(rid, "starting", stage=1,
                    note=("已确认切换，正在启动%s" % label
                          if confirmed else "按设置自动切换采集方式"),
                    engine=self.fallback_engine, session_id=sid or None,
                    error="", switch_ready=False, switch_requested=False,
                    switch_reason="")
        self.fallback_rooms.add(rid)
        try:
            fb.bring_up(room, session=sid)
        except Exception as e:
            traceback.print_exc()
            self.fallback_rooms.discard(rid)
            fb.fail(rid, "浏览器回退失败：%s" % e, 1)
            if sid:
                self._stop_session(sid)

    def _fallback(self, room: dict, info: dict, reason: str,
                  stage: int = 1) -> None:
        rid = room["id"]
        sid = str(info.get("sid") or room.get("session_id") or "")
        if self.fallback_mode == "ask":
            # 不自动开浏览器：把失败和切换入口亮在收听页，等用户点头。
            engine = self._probe_fallback_engine()
            self._capture_event(rid, "switch_needed", reason=reason[:200],
                                engine=engine)
            why = "直取失败：%s" % reason
            if not engine:
                why += "（也没有找到可用的浏览器采集）"
            print("[%s] 直取失败，等待用户确认切换：%s" % (rid, reason), flush=True)
            self.report(rid, "failed", stage=stage, failed_stage=stage,
                        error=why, engine="direct", session_id=sid or None,
                        switch_ready=bool(engine), switch_engine=engine,
                        switch_reason=reason[:200], switch_requested=False)
            # 会话先留着（看门狗 120 秒后才收尾）：用户马上点切换能接着同一场录。
            self.reap(rid, "等待用户确认切换", stop_session=False)
            return
        fb = self._get_fallback()
        if fb is None:
            self._capture_event(rid, "fallback", reason=reason[:200])
            self.fail(rid, reason, stage)
            if sid:
                self._stop_session(sid)
            return
        self._capture_event(rid, "fallback", reason=reason[:200])
        print("[%s] 直取失败，回退浏览器：%s" % (rid, reason), flush=True)
        self.report(rid, "starting", stage=1,
                    note="直取失败，切换浏览器采集：%s" % reason[:120],
                    engine=self.fallback_engine,
                    session_id=sid or None,
                    switch_ready=False, switch_requested=False,
                    switch_reason="")
        self.reap(rid, "回退浏览器", stop_session=False)
        self.fallback_rooms.add(rid)
        try:
            fb.bring_up(room, session=sid)
        except Exception as e:
            traceback.print_exc()
            self.fallback_rooms.discard(rid)
            fb.fail(rid, "浏览器回退失败：%s" % e, 1)
            if sid:
                self._stop_session(sid)

    # ---------------------------------------------------------------- 回收
    def reap(self, rid: str, reason: str = "", stop_session: bool = False) -> None:
        info = self.running.pop(rid, None)
        if not info:
            return
        with self._beat_lock:
            self._beat(rid, False, info, reason or "direct stopped")
        self._stop_traffic(info)
        info["gen"] = int(info.get("gen") or 0) + 1
        _kill_proc(info.get("proc"))
        if stop_session and info.get("sid"):
            self._stop_session(info["sid"])
        if reason:
            print("[%s] 回收：%s" % (rid, reason), flush=True)

    def _stop_session(self, sid: str) -> None:
        try:
            api(self.base, "/api/live/stop", {"session_id": sid}, timeout=20)
        except Exception:
            pass        # 服务端即使超时也把收尾跑完了；没停的话看门狗兜底

    def fail(self, rid: str, msg: str, stage: int = 1) -> None:
        print("[%s] 直取失败（第 %d 步）：%s" % (rid, stage, msg), flush=True)
        info = self.running.get(rid) or {}
        sid = str(info.get("sid") or "")
        self.report(rid, "failed", stage=stage, failed_stage=stage, error=msg,
                    engine="direct", session_id=sid or None,
                    switch_ready=False, switch_requested=False, switch_reason="")
        self.reap(rid, "启动失败", stop_session=False)
        if sid:
            self._stop_session(sid)

    # ---------------------------------------------------------------- 主循环
    def tick(self, rooms: list[dict]) -> None:
        alive_ids = {r["id"] for r in rooms}
        for rid in list(self.running):
            if rid not in alive_ids:
                self.reap(rid, "房间已删除")
        for rid in list(self.auto_restarts):
            if rid not in alive_ids:
                self.auto_restarts.pop(rid, None)
                self.restart_at.pop(rid, None)
        for rid in list(self.resolve_retries):
            if rid not in alive_ids:
                self._clear_resolve_retry(rid)

        # 已经回退的房间交给浏览器代理；传空列表也要 tick 一次，让它回收
        # 已删除/已停止的房间。
        if self.fallback is not None:
            fb_rooms = [r for r in rooms if r["id"] in self.fallback_rooms]
            self.fallback.tick(fb_rooms)
            for rid in list(self.fallback_rooms):
                if rid not in alive_ids:
                    self.fallback_rooms.discard(rid)

        to_restart: list[dict] = []
        to_start: list[dict] = []
        for r in rooms:
            rid = r["id"]
            if rid in self.fallback_rooms:
                continue
            if r.get("switch_requested") and r.get("switch_ready"):
                # 用户在收听页确认了「切换到浏览器采集」：优先于当前策略。
                self._switch_to_browser(r)
                continue
            if (r.get("state") == "failed" and r.get("switch_ready")
                    and self.fallback_mode in ("auto", "per_room", "shared")):
                # 设置页刚改成自动切换：已经停在待切换的房间也立刻补切。
                self._switch_to_browser(r, confirmed=False)
                continue
            if (r.get("state") == "failed" and r.get("switch_ready")
                    and self.fallback_mode == "none"):
                # 策略改成「只报失败」：撤掉切换入口，别让页面继续让人点。
                self.report(rid, "failed", switch_ready=False, switch_engine="",
                            switch_reason="", switch_requested=False)
                continue
            state = r.get("state")
            if state == "stopped":
                self._clear_resolve_retry(rid)
                if rid in self.running:
                    self.reap(rid, "已停止")
                if (r.get("auto_stopped")
                        and "音频块" in str(r.get("auto_stop_reason") or "")):
                    to_restart.append(r)
                continue
            if state == "failed":
                self._clear_resolve_retry(rid)
                if rid in self.running:
                    self.reap(rid, "采集失败")
                continue
            if state == "pending":
                # 用户点了重试：不管之前排了几次补试，都从现在重新开始。
                self._clear_resolve_retry(rid)
                if rid in self.running:
                    self.reap(rid, "重新排队")

            info = self.running.get(rid)
            if info is not None:
                self._check(r, info)
                continue
            if state in ("pending", "starting", "live"):
                # 网络类解析失败排了补试：到点前不重新拉，免得每 3 秒撞一次。
                if time.time() < float(self.resolve_retry_at.get(rid) or 0.0):
                    continue
                to_start.append(r)

        # 解析短链最长可能拖几十秒（跳转/回流/streamlink 兜底）。先把仍在
        # 采集的房间检查完，再处理需要解析的：否则一个坏房间会让其他房间的
        # 断流检测和自愈一起停摆，而 ffmpeg 其实还在跑。
        for r in to_restart:
            self._maybe_restart(r)
        for r in to_start:
            rid = r["id"]
            try:
                self.bring_up(r, session=str(r.get("session_id") or ""))
            except Exception as e:
                traceback.print_exc()
                self.fail(rid, "拉起失败：%s" % e, 1)
            time.sleep(1.0)

    def _maybe_restart(self, room: dict) -> None:
        """看门狗因断流收尾后，流还在就重新拉起；真下播不折腾。"""
        rid = room["id"]
        n = int(self.auto_restarts.get(rid) or 0)
        if n >= DIRECT_MAX_RESTARTS:
            return
        now = time.time()
        if now < float(self.restart_at.get(rid) or 0.0):
            return
        self.restart_at[rid] = now + 60.0
        try:
            resolve_douyin(self._resolve_target(room),
                                          quality=self.direct_quality)
        except Exception:
            return
        self.auto_restarts[rid] = n + 1
        print("[%s] 断流收尾后流仍在，重新拉起直取（第 %d 次）"
              % (rid, n + 1), flush=True)
        self.report(rid, "pending", stage=0,
                    note="断流收尾后重新拉起直取", engine="direct",
                    session_id="", auto_stopped=False, auto_stop_reason="")
        room["state"] = "pending"
        room["session_id"] = ""
        room["auto_stopped"] = False
        room["auto_stop_reason"] = ""

    # ---------------------------------------------------------------- 接管
    def adopt(self, rooms: list[dict]) -> None:
        direct_rooms: list[dict] = []
        fallback_rooms: list[dict] = []
        for r in rooms:
            if r.get("state") not in ("live", "starting"):
                continue
            eng = str(r.get("capture_engine") or "")
            # 空 engine 是老数据/切换模式前留下的房间：按浏览器接管，
            # 避免和还在跑的采集页抢同一个房间。
            if eng == "direct":
                direct_rooms.append(r)
            else:
                fallback_rooms.append(r)
        for r in direct_rooms:
            self._adopt_direct(r)
        if fallback_rooms:
            fb = self._get_fallback()
            if fb is not None:
                for r in fallback_rooms:
                    self.fallback_rooms.add(r["id"])
                fb.adopt(fallback_rooms)

    def _adopt_direct(self, room: dict) -> None:
        rid = room["id"]
        if rid in self.running:
            return
        sid = str(room.get("session_id") or "")
        if not sid or not self.session_alive(room):
            return
        try:
            stream, note = resolve_douyin(self._resolve_target(room),
                                          quality=self.direct_quality)
            self._report_page_url(rid, room)
        except Exception as e:
            print("[%s] 接管直取失败：%s" % (rid, e), flush=True)
            return
        info = self._new_info(None, sid, stream, adopted=True)
        info["traffic"] = self._meter(rid, reset=True)
        try:
            proc = self._spawn_ffmpeg(rid, stream, info)
        except Exception as e:
            self._stop_traffic(info)
            print("[%s] 接管直取失败：%s" % (rid, e), flush=True)
            return
        info["proc"] = proc
        self.running[rid] = info
        self.beat(rid, True, info)
        self._start_stderr(proc, info, 0)
        self._start_reader(rid, room, info)
        print("[%s] 接管已在跑的直取采集（session=%s，%s）"
              % (rid, sid, note), flush=True)

    def shutdown(self) -> None:
        for info in list(self.running.values()):
            info["gen"] = int(info.get("gen") or 0) + 1
            _kill_proc(info.get("proc"))
            self._stop_traffic(info)
        self.running.clear()
        super().shutdown()
        if self.fallback is not None:
            self.fallback.shutdown()
