# -*- coding: utf-8 -*-
"""只读探测：一个直播间现在是否在播。

为什么要单独一层：关注列表每两分钟要看一次状态，「检测」和「直取录制」
必须用同一套页面判定，否则会出现「关注说在播、开始记录又解析不到流」。
这里只做 GET，不登录、不带 Cookie、不启动浏览器，也绝不因为一次网络
失败就把状态判成「未开播」——宁可说未知，不可误报。

``probe()`` 永远不抛异常：返回 ``{state: live|offline|unknown, ...}``。
"""
from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

_PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
              "Mobile/15E148 Safari/604.1")

_LIVE_RE = re.compile(r"^https?://live\.douyin\.com/(\d+)", re.I)
_REFLOW_RE = re.compile(r"/(?:douyin/webcast/reflow|share/live)/(\d+)")
_DOUYIN_HOSTS = ("douyin.com", "amemv.com", "iesdouyin.com")

# 只认页面里明确的收播文案。反爬/改版/需要登录统统不算下播。
OFFLINE_MARKS = ("直播已结束", "主播已下播", "主播暂未开播", "直播已关闭",
                 "直播已结束啦", "当前直播已结束")
# 平台自己的临时错误：既不能判 live 也不能判 offline。
ERROR_MARKS = ("服务器开小差了", "页面不存在", "直播间不存在")

_LIVE_PAGE = "https://live.douyin.com/"
_REFLOW_BASE = "https://webcast.amemv.com/douyin/webcast/reflow/"

_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:[^"\\]|\\.){0,200})"')
_NICK_RE = re.compile(
    r'"owner"\s*:\s*\{[^{}]{0,400}?"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"')
_ROOM_RE = re.compile(r'"room"\s*:\s*\{')
_OWNER_RE = re.compile(r'"owner"\s*:\s*\{')
_WEB_RID_RE = re.compile(r'"webRid"\s*:\s*"(\d{4,})"')
_STARTED_RE = re.compile(
    r'"(?:live_started_at|liveStartedAt|create_time|createTime)"\s*:\s*"?'
    r'(\d{10,13})"?')
_FLV_BLOCK_RE = re.compile(
    r'"(?:flv_pull_url|flvPullUrl)"\s*:\s*\{([^{}]{0,4000})\}')
_FLV_PAIR_RE = re.compile(
    r'"([A-Za-z0-9_]{1,24})"\s*:\s*"(https?://[^"]+?\.flv[^"]*)"')


def _clean_url(u: str) -> str:
    u = html.unescape(str(u or ""))
    return u.replace("\\u0026", "&").replace("\\/", "/").strip()


def _flatten(text: str) -> str:
    t = str(text or "")
    for _ in range(2):
        t = (t.replace("\\\\u0026", "&").replace("\\u0026", "&")
              .replace("\\\\/", "/").replace("\\/", "/")
              .replace('\\\\"', '"').replace('\\"', '"'))
    return t


def _fetch(url: str, timeout: float, mobile: bool = False) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": _MOBILE_UA if mobile else _PC_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def _final_url(url: str, timeout: float) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except Exception:
        return ""


def _json_str(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except ValueError:
        return html.unescape(raw)


def _tidy(s: str) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip()
    if not t or t.startswith("$") or t.lower() in ("undefined", "null", "none"):
        return ""
    return t


# ---------------------------------------------------------------- 平台识别

def platform_of(url: str) -> str:
    try:
        host = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return ""
    if any(host == d or host.endswith("." + d) for d in _DOUYIN_HOSTS):
        return "douyin"
    if host.endswith("kuaishou.com") or host.endswith("gifshow.com"):
        return "kuaishou"
    if host.endswith("bilibili.com"):
        return "bilibili"
    return ""


def is_douyin(url: str) -> bool:
    return platform_of(url) == "douyin"


def canonical_douyin_url(url: str, timeout: float = 10.0) -> str:
    """直播链接 -> live.douyin.com/<web_rid>；拿不到返回空串。"""
    u = str(url or "").strip()
    m = _LIVE_RE.match(u)
    if m:
        return _LIVE_PAGE + m.group(1)
    if not is_douyin(u):
        return ""
    final = _final_url(u, timeout)
    # 短链也可能直接落到 live.douyin.com/<web_rid>；只认 reflow 落地会让
    # 这种链接永远预解析失败，每次启动都重新赌短链跳转。
    m = _LIVE_RE.match(final)
    if m:
        return _LIVE_PAGE + m.group(1)
    path = urllib.parse.urlsplit(final).path
    m = _REFLOW_RE.search(path)
    landing = _REFLOW_BASE + m.group(1) if m else ""
    if not landing:
        return ""
    try:
        text = _fetch(landing, timeout, mobile=True)
    except Exception:
        return ""
    meta = parse_room_meta(text)
    rid = str(meta.get("web_rid") or "")
    return _LIVE_PAGE + rid if rid.isdigit() else ""


def parse_room_meta(text: str) -> dict:
    """抖音页面（reflow / live）-> {nickname, title, web_rid}。纯函数。"""
    flat = _flatten(text)
    out: dict[str, str] = {}
    nick = _NICK_RE.search(flat)
    if nick:
        out["nickname"] = _tidy(_json_str(nick.group(1)))
    for m in _ROOM_RE.finditer(flat):
        t = _TITLE_RE.search(flat[m.end(): m.end() + 2000])
        if t:
            title = _tidy(_json_str(t.group(1)))
            if title:
                out["title"] = title
                break
    if not out.get("nickname"):
        m = re.search(r'"nickname"\s*:\s*"((?:[^"\\]|\\.){0,80})"', flat)
        if m:
            out["nickname"] = _tidy(_json_str(m.group(1)))
    owner = _OWNER_RE.search(flat)
    if owner:
        rid = _WEB_RID_RE.search(flat[owner.end(): owner.end() + 20000])
        if rid:
            out["web_rid"] = rid.group(1)
    return {k: v for k, v in out.items() if v}


def _live_stream(text: str) -> str:
    flat = _flatten(text)
    found: dict[str, str] = {}
    for block in _FLV_BLOCK_RE.finditer(flat):
        for name, url in _FLV_PAIR_RE.findall(block.group(1)):
            found.setdefault(name, _clean_url(url))
    for url in found.values():
        if url:
            return url
    m = re.search(r"https?://[^\"'\s<>]+\.m3u8[^\"'\s<>]*", flat)
    return _clean_url(m.group(0)) if m else ""


def _started_iso(text: str) -> str:
    m = _STARTED_RE.search(_flatten(text))
    if not m:
        return ""
    try:
        n = int(m.group(1))
        if n > 10 ** 12:          # 毫秒
            n //= 1000
        return datetime.fromtimestamp(n).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


def room_key_of(url: str, platform: str = "") -> str:
    """跨场次稳定的房号。拿不到时退回「主机 + 路径」。"""
    platform = platform or platform_of(url)
    u = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return ""
    if platform == "douyin":
        m = _LIVE_RE.match(u)
        if m:
            return m.group(1)
        m = _REFLOW_RE.search(parts.path)
        if m:
            return m.group(1)
    if platform == "bilibili":
        m = re.search(r"/(\d+)", parts.path)
        if m:
            return m.group(1)
    if platform == "kuaishou":
        m = re.search(r"/u/([^/?#]+)", parts.path)
        if m:
            return m.group(1)
    return (parts.hostname or "") + parts.path.rstrip("/")


def room_identity(*urls: str) -> str:
    """这几种链接是不是同一个直播间：返回同一个跨场次稳定的钥匙，否则空串。

    ``room_key_of`` 对抖音只会从 **live 页** 里认出 web_rid；分享文案里的
    reflow 链接它给的是 reflow 后面的 room id——那个 id 一次开播换一个，
    同一主播换个标题重开就变成另一把钥匙。拿它判重，列表就会越攒越多
    （云端曾出现同一间语音厅 4 个房间项）。

    这里先找所有输入里有没有 live.douyin.com/<web_rid>：只要有一个，
    就用它当身份；都没有才退回 ``room_key_of``（其它平台照旧）。
    抖音只有 reflow/reflow 时**故意返回空串**：认不出来就说认不出来，
    不能拿一次性的 room id 冒充房号。
    """
    live = ""
    for u in urls:
        m = _LIVE_RE.match(str(u or "").strip())
        if m:
            live = m.group(1)
            break
    if live:
        return "douyin:" + live
    platform = ""
    key = ""
    for u in urls:
        u = str(u or "").strip()
        if not u or platform_of(u) == "douyin":
            continue
        k = room_key_of(u, platform_of(u))
        if k:
            platform, key = platform_of(u), k
            break
    return (platform + ":" + key) if key else ""


# ---------------------------------------------------------------- 探测

def _probe_douyin(url: str, timeout: float) -> dict:
    live_url = canonical_douyin_url(url, timeout) or url
    m = _LIVE_RE.match(live_url)
    room_key = m.group(1) if m else room_key_of(url, "douyin")
    try:
        text = _fetch(live_url, timeout)
    except Exception as e:
        return {"state": "unknown", "room_key": room_key,
                "error": "打不开直播间页面（%s）" % (str(e)[:60] or
                                              type(e).__name__)}
    meta = parse_room_meta(text)
    out = {"state": "unknown", "room_key": room_key,
           "name": meta.get("nickname") or "",
           "title": meta.get("title") or "",
           "live_started_at": _started_iso(text), "error": ""}
    if _live_stream(text):
        out["state"] = "live"
        return out
    for mark in OFFLINE_MARKS:
        if mark in text:
            out["state"] = "offline"
            return out
    if any(mark in text for mark in ERROR_MARKS):
        out["error"] = "平台返回错误页，稍后再试"
    else:
        out["error"] = "页面里没有直播状态，可能改版了"
    return out


def _probe_bilibili(url: str, timeout: float) -> dict:
    m = re.search(r"/(\d+)", urllib.parse.urlsplit(url).path)
    if not m:
        return {"state": "unknown", "error": "链接里没有房间号"}
    rid = m.group(1)
    try:
        raw = _fetch("https://api.live.bilibili.com/room/v1/Room/get_info"
                     "?room_id=" + rid, timeout)
        data = json.loads(raw).get("data") or {}
    except Exception as e:
        return {"state": "unknown", "room_key": rid,
                "error": "查询失败（%s）" % (str(e)[:60] or type(e).__name__)}
    status = int(data.get("live_status") or 0)
    anchor = str(((data.get("anchor_info") or {}).get("base_info") or {})
                 .get("uname") or "")
    return {"state": "live" if status == 1 else ("offline" if status == 0 else "unknown"),
            "room_key": rid, "name": anchor,
            "title": str(data.get("title") or ""),
            "live_started_at": str(data.get("live_time") or ""),
            "error": "" if status in (0, 1) else "房间状态暂不可用"}


_KUAISHOU_OFFLINE = ("直播已结束", "主播暂未开播", "未开播", "直播已关闭")


def _probe_kuaishou(url: str, timeout: float) -> dict:
    room_key = room_key_of(url, "kuaishou")
    try:
        text = _fetch(url, timeout, mobile=True)
    except Exception as e:
        return {"state": "unknown", "room_key": room_key,
                "error": "打不开直播间页面（%s）" % (str(e)[:60] or
                                              type(e).__name__)}
    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if mt:
        title = _tidy(html.unescape(mt.group(1))).split("-")[0][:80]
    if re.search(r'"(?:liveStream|playUrls|live_stream|hlsPlayUrl)"', text):
        return {"state": "live", "room_key": room_key, "title": title,
                "live_started_at": "", "error": "", "name": ""}
    for mark in _KUAISHOU_OFFLINE:
        if mark in text:
            return {"state": "offline", "room_key": room_key, "title": title,
                    "live_started_at": "", "error": "", "name": ""}
    return {"state": "unknown", "room_key": room_key, "title": title,
            "live_started_at": "", "name": "",
            "error": "这个平台的自动检测还不稳定"}


def probe(url: str, platform: str = "", timeout: float = 8.0) -> dict:
    """只读探测。返回 {state, name, title, live_started_at, room_key, error}。

    支持抖音（完整） / B 站（官方接口） / 快手（尽力而为）；其他平台一律
    unknown + 人话原因，UI 仍保留「打开直播间」。
    """
    url = str(url or "").strip()
    platform = platform or platform_of(url)
    out: dict[str, Any] = {"state": "unknown", "name": "", "title": "",
                           "live_started_at": "", "room_key": "",
                           "platform": platform, "error": ""}
    if not url:
        out["error"] = "链接是空的"
        return out
    t0 = time.monotonic()
    try:
        if platform == "douyin":
            out.update(_probe_douyin(url, timeout))
        elif platform == "bilibili":
            out.update(_probe_bilibili(url, timeout))
        elif platform == "kuaishou":
            out.update(_probe_kuaishou(url, timeout))
        else:
            out["error"] = "这个平台还不能自动检测"
    except Exception as e:      # 探测永远不能把 worker 带崩
        out["state"] = "unknown"
        out["error"] = "检测失败（%s）" % (str(e)[:60] or type(e).__name__)
    out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    out["platform"] = platform
    if out.get("state") not in ("live", "offline", "unknown"):
        out["state"] = "unknown"
    return out
