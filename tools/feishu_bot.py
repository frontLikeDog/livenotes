# -*- coding: utf-8 -*-
"""飞书机器人常驻进程：长连接收消息，收到就回。

    python tools/feishu_bot.py
    python tools/feishu_bot.py --say "上线了"     # 先给记住的那个人发一条，测试发送通路

命令：
    贴链接（或 /watch 链接）  -> 开听。陌生站点第一次会弹卡片让你确认
    /状态 [CH]                -> 在听哪几路、有没有断流；CH02 只看一路
    /rooms                    -> 正在听哪几路，各自什么状态
    /stop 1 或 /stop CH02      -> 停掉一路（浏览器一起关）
    /发布 [CH]                -> 场次发成飞书云文档 + 场次表一行
    /文档 [CH]                -> 发布状态、文档链接、失败原因
    /白名单                   -> 已放行的站点
    关注 FDE、OPC / 关注列表 / 取消关注 X / 话题 X
    /找人 阿哲 /搜 关键词 /问 问题
    /补名 [CH01] [1 阿哲 2 小北] -> 发试听 + 表单卡，或直接写回名字
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import (clips, feishu, feishu_publish, lan, profile, reading, report,
                 share, store, timefmt, watchlink)  # noqa: E402
from app.config import (BASE_URL, CAPTURE_ONLY, LIVE_STALL_WARN_S,  # noqa: E402
                        PORT)

MAX_ROOMS = watchlink.MAX_ROOMS

# P2 自然语言实验：默认关闭。打开后只对“白名单群聊里 @机器人”的消息生效，
# 私聊仍然只认命令——这是防误触发的底线。
NL_ENABLED = os.environ.get("FEISHU_NL_ENABLED", "0").lower() in ("1", "true", "yes", "on")

# 采集代理和本服务在这台机器上，优先走回环：给手机看的链接由
# app.lan.base_url() 生成，开启局域网访问后才是局域网地址。
_BASES = [b.rstrip("/") for b in (
    os.environ.get("LIVENOTES_API"),
    "http://127.0.0.1:%d" % PORT,
    BASE_URL,
) if b]
_BASE: str | None = None

_STATE = {"pending": ("⏳", "待启动"), "starting": ("⏳", "启动中"),
          "live": ("🟢", "监听中"), "failed": ("⚠️", "失败"),
          "stopped": ("⏸", "已停止")}
_ACTIVE = ("pending", "starting", "live")


def _api(path: str, payload: dict | None = None, timeout: float = 6.0) -> dict:
    """调本机服务。上次成功的地址优先，其它兜底——换端口时不用改两处。"""
    global _BASE
    order = ([_BASE] if _BASE else []) + [b for b in _BASES if b != _BASE]
    last: Exception | None = None
    for base in order:
        try:
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                base + path, data=data,
                method="POST" if payload is not None else "GET",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _BASE = base
                return json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            # 409/429 这类业务错误也是有效响应，别当成“服务没起来”去试下一个地址。
            try:
                data = json.loads(e.read().decode("utf-8") or "{}")
            except Exception:
                data = None
            if isinstance(data, dict) and ("ok" in data or "error" in data):
                _BASE = base
                return data
            last = e
        except Exception as e:
            last = e
    raise RuntimeError("本机服务没响应（%s）：%s" % (path, last))


def _rooms() -> list[dict]:
    return _api("/api/rooms").get("rooms") or []


def _ordered(rooms: list[dict]) -> list[dict]:
    """在听的在前面。序号（/stop 1）按这个顺序数。"""
    return sorted(rooms, key=lambda r: (r.get("state") not in _ACTIVE,
                                        r.get("created_at") or ""))


def _label(r: dict) -> str:
    icon, cn = _STATE.get(str(r.get("state")), ("·", str(r.get("state"))))
    name = r.get("name") or "（还在读名字）"
    if r.get("state") == "starting" and r.get("stage"):
        name += " · 第%s步" % r["stage"]
    if r.get("state") == "failed":
        if r.get("failed_stage") is not None:
            name += " · 第%s步失败" % r["failed_stage"]
        if r.get("error"):
            name += " · " + str(r["error"])[:40]
    return "%s %s %s" % (icon, name, cn)


def _ch(r: dict) -> str:
    """稳定展示编号：CH01、CH02。seq 加/删房间不会变。"""
    try:
        seq = int(r.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return "CH%02d" % seq if seq > 0 else "CH—"


def _ch_list(rooms: list[dict]) -> str:
    return "、".join("%s %s" % (_ch(r), r.get("name") or r.get("id") or "")
                    for r in _ordered(rooms))


def _hms(sec) -> str:
    sec = max(0, int(float(sec or 0)))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


_LOOPBACK_RE = re.compile(
    r"^https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?(?:/|$)", re.I)


def _is_phone_url(url: str) -> bool:
    """这个链接手机能不能打开：回环和私网地址都不行。"""
    url = str(url or "").strip()
    if not url or _LOOPBACK_RE.match(url + "/"):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").strip("[]")
    except ValueError:
        return False
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True          # 域名：交给公网 DNS，不在这里判死


def _public_base() -> str:
    """给手机点的链接必须是公网地址；云端只绑回环/私网时返回空串。

    阿里云节点会从桌面端继承一个 172.24.x.x 的局域网地址；它不是回环，
    却同样打不开。只看 loopback 会把私网链接发到手机上，所以这里用
    ``ipaddress.is_global`` 再挡一层。
    """
    base = (lan.base_url() or "").rstrip("/")
    return base if _is_phone_url(base) else ""


def _play_hint(room: dict) -> str:
    """场次结束后的回听入口：有公网页面给链接，没有就让用户发 /分享。"""
    base = _public_base()
    sid = str(room.get("session_id") or "")
    if base and sid:
        return "回放：%s/reader?session=%s" % (base, sid)
    if CAPTURE_ONLY:
        return "听这一段：发 /分享 %s 90秒" % _ch(room)
    return "回放：%s/reader?session=%s" % (lan.base_url(), sid)


def _live_hint(room: dict) -> str:
    base = _public_base()
    if base:
        return "看实时：%s/live" % base
    if CAPTURE_ONLY:
        return "听这一段：发 /分享 %s 90秒" % _ch(room)
    return "看实时：%s/live" % lan.base_url()


def _span(p: dict) -> str:
    """要点时间：「14:10–14:12，录音 0:49–1:17」。

    群里回头找「谁几点说的」用的是北京时间；录音内偏移留着，方便回听时对照。
    老简报没有墙上时间就只给偏移——宁可少一行，也不编一个时间。
    """
    off = "%s–%s" % (_hms(p.get("t_from")), _hms(p.get("t_to")))
    a, b = str(p.get("from_clock") or ""), str(p.get("to_clock") or "")
    if not a or not b:
        return off
    if a == b:
        return "%s，录音 %s" % (a, off)
    if a.startswith("次日 ") and b.startswith("次日 "):
        b = b[3:]
    elif len(a) > 6 and len(b) > 6 and a[:5] == b[:5] and a[5] == " " and b[5] == " ":
        b = b[6:]                      # 「11-10 00:05–00:07」日期只写一次
    return "%s–%s，录音 %s" % (a, b, off)


def _tz_note(data: dict) -> str:
    if ((data.get("coverage") or {}).get("clock_tz")
            or (data.get("window") or {}).get("clock_tz")):
        return "时间为北京时间"
    return ""


def _note_with_tz(data: dict, tail: str) -> str:
    tz = _tz_note(data)
    return ("%s · %s" % (tz, tail)) if tz else tail


def _ago(sec) -> str:
    if sec is None:
        return "—"
    sec = max(0, int(float(sec)))
    if sec < 60:
        return "%d 秒前" % sec
    if sec < 3600:
        return "%d 分钟前" % (sec // 60)
    return "%d 小时 %d 分前" % (sec // 3600, (sec % 3600) // 60)


_CH_RE = re.compile(r"^(?:CH)?0*(\d{1,3})$", re.I)


def _resolve_room(rooms: list[dict], target: str,
                  allow_plain_seq: bool = True) -> dict | None:
    """按 CH 编号 / 房间 id / URL 找房间，不按名字猜。

    allow_plain_seq=False 时，裸数字不再当 CH —— /stop 1 要继续表示
    “当前排序的第 1 路”，跟固定编号 CH01 不是一回事。
    """
    t = (target or "").strip()
    if not t:
        return None
    m = _CH_RE.match(t)
    if m and (allow_plain_seq or t.upper().startswith("CH")):
        seq = int(m.group(1))
        for r in rooms:
            try:
                if int(r.get("seq") or 0) == seq:
                    return r
            except (TypeError, ValueError):
                continue
        return None
    for r in rooms:
        if t == str(r.get("id") or ""):
            return r
    key = t.rstrip("/")
    for r in rooms:
        for u in (r.get("url"), r.get("resolved_url")):
            u = str(u or "").rstrip("/")
            if u and (key == u or (key.startswith(("http://", "https://")) and key in u)):
                return r
    return None


def _full_text(active: list[dict]) -> str:
    lines = ["已经 %d/%d 路了，先停一路再发：" % (len(active), MAX_ROOMS)]
    for i, r in enumerate(active, 1):
        lines.append("%d. %s %s" % (i, _ch(r), _label(r)))
    lines.append("停：/stop 1 或 /stop %s" % _ch(active[0]))
    return "\n".join(lines)


def _start_room(url: str, resolved_from: str = "", label: str = "") -> str:
    """把一条已通过判定/确认的链接变成一路监听。返回给人看的结果。

    label 是从分享文案里认出来的主播名：先垫成房间名，采集端落地后再换成
    「主播名 · 房间标题」。只为让"刚加进来那 10 秒"也认得出是哪一路。
    """
    rooms = _rooms()
    for r in rooms:
        # 短链/reflow 和真实房号都算同一个房间；分享原文用来兜住重复发送。
        same = (url in (r.get("url"), r.get("resolved_url"))
                or (resolved_from and resolved_from == r.get("resolved_from")))
        if not same:
            continue
        if r.get("state") in _ACTIVE:
            _, cn = _STATE.get(str(r.get("state")), ("", ""))
            return "已经在听了（%s）：%s\n看状态：/rooms" % (
                cn, r.get("name") or r.get("url") or "")
        _api("/api/rooms/state", {"id": r["id"], "state": "pending", "error": ""})
        return "重开了这一路：%s\n上次是「%s」。" % (
            url, _STATE.get(str(r.get("state")), ("", "?"))[1])

    active = [r for r in rooms if r.get("state") in _ACTIVE]
    if len(active) >= MAX_ROOMS:
        return _full_text(active)
    resp = _api("/api/rooms", {"url": url, "resolved_from": resolved_from,
                               "name": label})
    room = resp.get("room") or {}
    if not resp.get("created"):
        return "这一路已经在了：%s" % (room.get("name") or url)
    n = len(active) + 1
    # 回复里带上主播名：链接是一长串域名时，人只能靠这行字确认"开对了没有"。
    head = "🎧 已开始（第 %d/%d 路）%s\n" % (n, MAX_ROOMS,
                                          ("· " + label) if label else "")
    return (head + "%s\n页面正在打开，约 10 秒。开好了我喊你。" % url)


# ---------------------------------------------------------------- 卡片

def _confirm_card(p: dict) -> dict:
    """陌生站点确认卡。按钮是回调，飞书后台没订阅回调时静默无反应，
    所以卡片底部的文字兜底不能省。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "orange",
                   "title": {"tag": "plain_text", "content": "🎧 新站点，要你点一下"}},
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**站点**\n%s" % p["site"]}},
                {"is_short": True,
                 "text": {"tag": "lark_md", "content": "**确认码**\n%s" % p["code"]}},
            ]},
            {"tag": "div", "text": {"tag": "lark_md", "content": p["url"][:200]}},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                "这个站不在已知平台里（抖音 / 快手 / B 站）。确认后我会用本机浏览器"
                "打开它、录下里面的声音，并记成长期白名单——以后这个站的链接直接开听。"}},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary",
                 "text": {"tag": "plain_text", "content": "允许并开始监听"},
                 "value": {"act": "allow", "code": p["code"]}},
                {"tag": "button", "type": "default",
                 "text": {"tag": "plain_text", "content": "不用了"},
                 "value": {"act": "deny", "code": p["code"]}},
            ]},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "按钮没反应就回我：确认 %s（%s内有效）" % (p["code"], watchlink.ttl_text())}]},
        ],
    }


def _done_card(title: str, body: str, template: str = "green") -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]}


def handle_card(value: dict, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> tuple[str, dict | None]:
    """点卡片按钮。返回 (toast, 原地替换的卡片)。

    简报类按钮要调模型或读库，不能在卡片回调里等——先回 toast，
    真正的生成放到后台线程，结果单独发一条消息。
    """
    act = str(value.get("act") or "")
    if act == "naming_cancel":
        _naming_drop(str(value.get("nonce") or ""))
        return ("好，这次不补名", None)
    if act == "naming_submit":
        pending, names, err = _naming_from_form(value)
        if not pending:
            return (err or "这张补名卡过期了", None)
        if not names:
            return (err or "还没填名字", None)
        threading.Thread(target=_naming_apply,
                         args=(pending, names, open_id, chat_id, chat_type),
                         daemon=True).start()
        return ("正在保存名字…", None)
    if act in ("share_confirm", "share_cancel", "share_clip"):
        threading.Thread(target=_share_action,
                         args=(act, value, open_id, chat_id, chat_type),
                         daemon=True).start()
        return ("正在处理…", None)
    if act.startswith("brief_"):
        if act == "brief_cancel":
            _take_nonce(value.get("nonce") or "", open_id)
            return ("好，已取消", None)
        threading.Thread(
            target=_brief_action,
            args=(act, value, open_id, chat_id, chat_type), daemon=True).start()
        return ("正在处理…", None)
    if act in ("publish_retry", "publish_name"):
        sid = str(value.get("session_id") or "")
        if not sid:
            return ("这个按钮的场次信息丢了，发 /文档 重新拿。", None)
        if act == "publish_retry":
            threading.Thread(
                target=_publish_worker,
                args=(sid, "", open_id, chat_id, chat_type, True),
                daemon=True).start()
            return ("正在重新发布…", None)
        threading.Thread(
            target=_naming_worker_for_sid,
            args=(sid, open_id, chat_id, chat_type), daemon=True).start()
        return ("试听和补名卡在下面", None)

    code = str(value.get("code") or "")
    p = watchlink.pending_get(code)
    if not p:
        return ("这张确认过期了，把链接再发一次就行。", None)

    if act == "deny":
        watchlink.pending_take(code)
        return ("好，已取消", _done_card(
            "已取消", "%s 不会进白名单，也不会开浏览器。" % p["site"], "grey"))

    rooms = _rooms()
    active = [r for r in rooms if r.get("state") in _ACTIVE]
    if len(active) >= MAX_ROOMS:
        return ("已满 %d 路，先停一路" % MAX_ROOMS, _done_card(
            "已满 %d 路" % MAX_ROOMS, _full_text(active) + "\n\n停好之后把链接再发一次，"
            "这个站已经记住了。", "orange"))

    watchlink.pending_take(code)
    watchlink.allow(p["site"], note="飞书确认")
    reply = _start_room(p["url"])
    print("[卡片] allow %s -> %s" % (p["site"], reply.splitlines()[0]), flush=True)
    return ("已允许，开始监听", _done_card("✅ 已允许这个站点", reply))


# ---------------------------------------------------------------- 文本

def _ts(x: float) -> str:
    x = max(0, int(x or 0))
    return "%d:%02d" % (x // 60, x % 60)


def _link(sid: str, t: float) -> str:
    return "%s/reader?session=%s&t=%d" % (lan.base_url(), sid, int(t))


def _split_mixed(term: str) -> list[str]:
    """把「OPC政策」拆成 ["OPC","政策"]。中英文混排时用户往往连写，
    但 ASR 出来的是"OPC 的政策"，整串 LIKE 必然落空。"""
    parts = re.findall(r"[A-Za-z0-9]+|[一-鿿]+", term or "")
    return parts if len(parts) > 1 else []


def cmd_topic(words: str, limit: int = 5) -> str:
    """按关键词捞最近一场的段落。用 LIKE 而不是 FTS：
    两字中文词（"退款"）在 trigram 下会静默返回 0 条，这是本项目踩过的坑。
    """
    terms = [w for w in re.split(r"[、,，；;\s]+", words or "") if w]
    if not terms:
        return "用法：/话题 OPC政策\n多个词用逗号或顿号隔开。"
    conn = store.connect()
    try:
        like = " OR ".join(["(u.text LIKE ? OR u.topic LIKE ?)"] * len(terms))
        args: list = []
        for t in terms:
            args += ["%" + t + "%", "%" + t + "%"]
        # 跨场次查：刚开的场次往往只有几十句，只查"最近一场"会让人以为搜索坏了。
        # 用收到的顺序倒序排，最新说的排前面。
        cols = ("SELECT u.session_id, u.t_start, u.speaker_label, u.text, s.title, "
                "s.started_at, s.started_utc, s.tz_offset_min "
                "FROM utterances u JOIN sessions s ON s.id = u.session_id ")
        rows = conn.execute(
            cols + "WHERE s.deleted_at IS NULL AND (" + like + ") "
            "ORDER BY s.started_at DESC, u.t_start ASC LIMIT ?",
            tuple(args) + (limit,)).fetchall()
        note = ""
        if not rows:
            subs = [p for t in terms for p in _split_mixed(t)] or []
            if subs:
                like2 = " OR ".join(["(u.text LIKE ? OR u.topic LIKE ?)"] * len(subs))
                args2: list = []
                for t in subs:
                    args2 += ["%" + t + "%", "%" + t + "%"]
                rows = conn.execute(
                    cols + "WHERE s.deleted_at IS NULL AND (" + like2 + ") "
                    "ORDER BY s.started_at DESC, u.t_start ASC LIMIT ?",
                    tuple(args2) + (limit,)).fetchall()
                if rows:
                    note = "（整串没匹配到，按 %s 分开找的）" % "、".join(subs)
        if not rows:
            return "没找到「%s」。换个词试试（中文两字以上、专有名词效果最好）。" % "、".join(terms)
        out = ["「%s」共 %d 段（最近说的在前）%s：" % ("、".join(terms), len(rows), note)]
        for r in rows:
            # 时间给两份（北京 / UTC）：飞书里收到的偏移量对不上任何人的表，
            # 而"几点说的"才是用户回找的锚点。
            when = timefmt.clock_line(
                {"started_at": r["started_at"], "started_utc": r["started_utc"],
                 "tz_offset_min": r["tz_offset_min"]}, r["t_start"])
            out.append("\n%s · 录音 %s · %s · 《%s》\n「%s…」\n回听 %s" % (
                when, _ts(r["t_start"]), r["speaker_label"] or "",
                (r["title"] or "")[:14], (r["text"] or "")[:46],
                _link(r["session_id"], r["t_start"])))
        return "\n".join(out)
    finally:
        conn.close()


# ---------------------------------------------------------------- 读侧：找人 / 搜 / 问

def _mmdd(iso) -> str:
    s = str(iso or "").strip()
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})", s)
    return ("%s-%s" % (m.group(1), m.group(2))) if m else s[:10]


def _person_list_text(conn) -> str:
    """没有参数时的「找人」入口：底库 + 只有昵称的观点署名。"""
    lines: list[str] = []
    persons = store.list_persons(conn)
    if persons:
        lines.append("记得的人：")
        for p in persons[:10]:
            pid = str(p.get("person_id") or "")
            n = conn.execute("SELECT COUNT(*) FROM claims WHERE person_id=?",
                             (pid,)).fetchone()[0]
            bits = []
            if int(p.get("n_sessions") or 0):
                bits.append("%d 场" % int(p["n_sessions"]))
            if int(n or 0):
                bits.append("%d 条观点" % int(n))
            lines.append("· %s%s" % (p.get("name") or pid,
                                     ("（%s）" % " · ".join(bits)) if bits else ""))
    try:
        rows = conn.execute(
            "SELECT who, COUNT(*) AS n, COUNT(DISTINCT session_id) AS ns"
            " FROM claims WHERE who<>'' GROUP BY who"
            " ORDER BY ns DESC, n DESC LIMIT 10").fetchall()
    except Exception:
        rows = []
    nick = [r for r in rows
            if str(r["who"] or "") not in {str(p.get("name") or "") for p in persons}]
    if nick:
        lines.append("")
        lines.append("只有昵称、还没认成跨场人物的人：")
        for r in nick[:8]:
            lines.append("· %s（%d 场 · %d 条观点）" % (
                r["who"], int(r["ns"] or 0), int(r["n"] or 0)))
    if not lines:
        return ("底库里还没有人。先在某场用 /补名 给上麦者补个名字，"
                "或者等总结生成观点后再试。")
    lines.append("")
    lines.append("看谁：/找人 阿哲")
    return "\n".join(lines)


def _find_person_text(data: dict) -> str:
    """把档案数据排成手机上好读的一段文字；文档链接是主入口。"""
    lines = ["👤 %s · 观点档案" % (data.get("name") or "这位上麦者")]
    facts = data.get("facts") or {}
    bits = []
    if facts.get("n_sessions"):
        bits.append("%d 场" % int(facts["n_sessions"]))
    if facts.get("n_claims"):
        bits.append("%d 条观点" % int(facts["n_claims"]))
    if facts.get("last_at"):
        bits.append("最近 %s" % facts["last_at"])
    if facts.get("seconds"):
        try:
            bits.append("上麦 %d 分" % max(1, int(float(facts["seconds"]) // 60)))
        except (TypeError, ValueError):
            pass
    if bits:
        lines.append(" · ".join(bits))
    latest = data.get("latest") or []
    for i, r in enumerate(latest[:3], 1):
        lines.append("%d. [%s] %s（%s）" % (
            i, r.get("topic") or "其他", r.get("stance") or "", r.get("at") or ""))
    url = str(data.get("doc_url") or "")
    if data.get("doc_ok") and url:
        lines.append("档案文档：" + url)
    elif url:
        lines.append("档案文档（这次没刷新）：" + url)
    elif data.get("error"):
        lines.append("档案文档这次没建成：%s" % data["error"])
    else:
        lines.append("档案文档这次没建成，上面是最新观点。")
    return "\n".join(lines)


def _share_person_doc_with_owner(bot, token: str, open_id: str) -> None:
    """群聊里发起人不是 owner 时，把档案也授权给 owner；失败不影响返回。"""
    owner = str(getattr(bot, "owner_open_id", "") or "")
    fn = getattr(bot, "_grant_doc", None)
    if not token or not owner or owner == str(open_id or "") or not callable(fn):
        return
    try:
        fn(token, owner)
    except Exception:
        traceback.print_exc()


def cmd_find_person(rest: str, open_id: str = "", chat_id: str = "",
                    chat_type: str = "p2p") -> str:
    q = str(rest or "").strip(" ：:")
    conn = store.connect()
    try:
        if not q:
            return _person_list_text(conn)
        who = reading.resolve_person(conn, q)
        if not who.get("ok"):
            if who.get("kind") == "ambiguous":
                return ("找到多个像「%s」的人：\n%s\n说得更具体一点，比如 /找人 %s"
                        % (q, "、".join(who.get("matches") or []),
                           (who.get("matches") or [q])[0]))
            return str(who.get("error") or "没找到这个人。")
        top_bot = globals().get("_BOT")
        data = reading.publish_person_doc(
            conn, top_bot,
            person_id=str(who.get("person_id") or ""),
            name=str(who.get("name") or ""),
            share_with=str(open_id or ""))
        if data.get("doc_ok") and data.get("doc_token"):
            _share_person_doc_with_owner(top_bot, str(data["doc_token"]), open_id)
        if not data.get("ok"):
            return ("%s%s。\n总结生成观点后，这里会变成一篇观点档案。"
                    % (who.get("name") or q, data.get("reason") or "还没有观点记录"))
        return _find_person_text(data)
    except Exception as e:
        traceback.print_exc()
        return "读人物档案失败：%s" % _short_err(str(e))
    finally:
        conn.close()


_SESSION_CACHE: dict[str, dict] = {}


def _session_info(sid: str) -> dict:
    sid = str(sid or "")
    if not sid:
        return {}
    if sid in _SESSION_CACHE:
        return _SESSION_CACHE[sid]
    conn = store.connect()
    try:
        s = store.get_session(conn, sid) or {}
    finally:
        conn.close()
    _SESSION_CACHE[sid] = s
    return s


def _search_rows(q: str) -> tuple[list[dict], list[str]]:
    """先走接口（带术语扩展），服务没起来再本地查；返回 (rows, expanded)。"""
    try:
        data = _api("/api/search?" + urllib.parse.urlencode({
            "q": q, "types": "notes,utterances", "limit": 30}), timeout=12)
        rows = data.get("rows") or []
        return list(rows), list(data.get("expanded") or [])
    except Exception:
        conn = store.connect()
        try:
            return reading.local_search(conn, q, limit=20), []
        finally:
            conn.close()


def _search_line_text(rows: list[dict], q: str, expanded: list[str]) -> str:
    """Top 5：场次 + 时间码 + 原话 + 文档链接；没发布就明说「未发布」。"""
    seen: set[tuple[str, int, str]] = set()
    picked: list[dict] = []
    # 笔记单元（结论/观点/高光）优先；逐字稿补足。它们来自同一次搜索，
    # 顺序已经是接口按权重排好的，这里只去重。
    for r in rows:
        sid = str(r.get("session_id") or "")
        try:
            t = int(float(r.get("t_start") or 0))
        except (TypeError, ValueError):
            t = 0
        key = (sid, t, _md_safe(str(r.get("text") or ""))[:24])
        if key in seen:
            continue
        seen.add(key)
        picked.append(r)
        if len(picked) >= 5:
            break
    if not picked:
        return ("没找到「%s」。换个词试试（中文两字以上、专有名词效果最好）。"
                % q)
    head = "🔎「%s」找到 %d 条（结论优先）：" % (q, len(picked))
    if expanded:
        head += "\n（也按 %s 找过）" % "、".join(expanded[:4])
    lines = [head]
    conn = store.connect()
    try:
        for i, r in enumerate(picked, 1):
            sid = str(r.get("session_id") or "")
            s = _session_info(sid)
            when = _mmdd(s.get("started_at"))
            try:
                t_text = _ts(float(r.get("t_start") or 0))
            except (TypeError, ValueError):
                t_text = ""
            title = str(s.get("title") or r.get("session_title") or r.get("title")
                        or "未命名场次")
            who = str(r.get("speaker_label") or "").strip()
            text = str(r.get("text") or "").strip()
            url = reading.session_doc_url(conn, sid)
            lines.append("\n%d. 《%s》%s%s%s" % (
                i, title[:20], (" " + when) if when else "",
                (" " + t_text) if t_text else "",
                (" · " + who) if who else ""))
            lines.append("「%s」" % (text[:70] + ("…" if len(text) > 70 else "")))
            lines.append("文档：" + (url if url else "未发布"))
    finally:
        conn.close()
    return "\n".join(lines)


def cmd_search(rest: str, open_id: str = "", chat_id: str = "",
               chat_type: str = "p2p") -> str:
    q = str(rest or "").strip(" ：:")
    if not q:
        return ("用法：/搜 OPC政策\n多个词用逗号/顿号隔开；"
                "它找的是结论和原话，找文档请用飞书搜索。")
    try:
        rows, expanded = _search_rows(q)
    except Exception as e:
        traceback.print_exc()
        return "搜索失败：%s" % _short_err(str(e))
    return _search_line_text(rows, q, expanded)


def _citation_doc_text(citations: list[dict]) -> list[str]:
    """问答引用里的文档链接：同一场只出现一次，没发布写「未发布」。"""
    out: list[str] = []
    seen: set[str] = set()
    conn = store.connect()
    try:
        for c in citations:
            sid = str(c.get("session_id") or "")
            if not sid or sid in seen:
                continue
            seen.add(sid)
            s = _session_info(sid)
            url = reading.session_doc_url(conn, sid)
            out.append("· 《%s》%s" % (
                str(s.get("title") or c.get("title") or "未命名场次")[:20],
                url if url else "未发布"))
    finally:
        conn.close()
    return out


def _qa_text(data: dict) -> str:
    state = str(data.get("state") or "")
    if state == "no_result":
        return "没找到相关内容。换个说法，或者先确认这场已经生成总结。"
    if state == "failed" or (not data.get("ok") and data.get("error")):
        return "问答没成功：%s" % (data.get("error") or "模型没有生成可用回答")
    ans = str(data.get("answer") or "").strip()
    if not ans:
        return "问答还没结果。稍后再问一次。"
    lines = ["💡 " + ans]
    citations = data.get("citations") or []
    if citations:
        lines.append("")
        lines.append("依据：")
        for c in citations[:5]:
            who = str(c.get("speaker") or "").strip()
            title = str(c.get("title") or "未命名场次")
            t_text = str(c.get("t_text") or "")
            text = str(c.get("text") or "").strip()
            lines.append("[%s] %s《%s》%s" % (
                c.get("n") or "·", (who + " · ") if who else "",
                title[:18], (" " + t_text) if t_text else ""))
            if text:
                lines.append("「%s」" % (text[:80] + ("…" if len(text) > 80 else "")))
        docs = _citation_doc_text(citations)
        if docs:
            lines.append("文档：")
            lines += docs
    followups = data.get("followups") or []
    if followups:
        qs = [str(f.get("q") or f.get("question") or f) for f in followups[:3]
              if str(f.get("q") or f.get("question") or f).strip()]
        if qs:
            lines.append("")
            lines.append("还可以问：" + "；".join(qs))
    return "\n".join(lines)


def _qa_worker(question: str, sid: str, open_id: str, chat_id: str,
               chat_type: str) -> None:
    def say(text: str) -> None:
        try:
            _send_text_target(text, open_id, chat_id, chat_type)
        except Exception:
            traceback.print_exc()

    payload = {"question": question}
    if sid:
        payload["session_id"] = sid
    try:
        data = _api("/api/qa/ask", payload, timeout=20)
    except Exception as e:
        traceback.print_exc()
        say("问笔记失败：%s" % _short_err(str(e)))
        return
    if data.get("error"):
        say("问笔记失败：%s" % data["error"])
        return
    if str(data.get("state") or "") == "ready":
        say(_qa_text(data))
        return
    qid = int(data.get("id") or 0)
    if not qid:
        say("问笔记没拿到任务编号，稍后再试。")
        return
    deadline = time.time() + 150
    last: dict = data
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            last = _api("/api/qa/answer?" + urllib.parse.urlencode({"id": qid}),
                        timeout=15)
        except Exception:
            continue
        if str(last.get("state") or "") in ("ready", "no_result", "failed"):
            break
    say(_qa_text(last))


def _spawn_qa(fn, err_to: str, *args) -> None:
    """问答专用后台线程：失败提示说「问笔记」，不要复用简报的文案。"""
    def _run():
        try:
            fn(*args)
        except Exception:
            traceback.print_exc()
            try:
                _send_text_target("问笔记没成功，稍后再试。", err_to)
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


def cmd_ask(rest: str, open_id: str = "", chat_id: str = "",
            chat_type: str = "p2p") -> str:
    body = str(rest or "").strip(" ：:")
    if not body:
        return ("用法：/问 阿哲对 OPC 怎么看？\n"
                "想限定某一场：/问 CH01 这场谁反对出海？")
    sid = ""
    m = re.match(r"(?i)^(CH\s*0*\d{1,3})\s+(.+)$", body)
    if m:
        room, cands, err = _pick_room_choice(m.group(1))
        if not room:
            if cands:
                return _room_candidates_text(cands, "发：/问 %s 你的问题")
            return err or "没找到这一路。"
        sid = str(room.get("session_id") or "")
        body = m.group(2).strip()
    _spawn_qa(_qa_worker, open_id, body, sid, open_id, chat_id, chat_type)
    return "⏳ 正在查笔记…（要调模型，通常十几秒）"


# ---------------------------------------------------------------- 读侧：补名卡

_NAMING_TTL_S = 30 * 60
_NAMING_LOCK = threading.Lock()
_NAMING: dict[str, dict] = {}


def _naming_prune() -> None:
    now = time.time()
    for nonce in list(_NAMING):
        if now - float(_NAMING[nonce].get("at") or 0) > _NAMING_TTL_S:
            _NAMING.pop(nonce, None)


def _naming_put(pending: dict) -> None:
    with _NAMING_LOCK:
        _naming_prune()
        _NAMING[str(pending["nonce"])] = pending


def _naming_get(nonce: str) -> dict | None:
    with _NAMING_LOCK:
        _naming_prune()
        return _NAMING.get(str(nonce or ""))


def _naming_for_scope(open_id: str, chat_id: str) -> dict | None:
    """文字兜底用：同一会话里最近一张还没过期的补名卡。"""
    with _NAMING_LOCK:
        _naming_prune()
        best: dict | None = None
        for p in _NAMING.values():
            if str(p.get("open_id") or "") != str(open_id or ""):
                continue
            if str(p.get("chat_id") or "") and str(p.get("chat_id") or "") != str(chat_id or ""):
                continue
            if best is None or float(p.get("at") or 0) > float(best.get("at") or 0):
                best = p
        return best


def _naming_drop(nonce: str) -> dict | None:
    with _NAMING_LOCK:
        return _NAMING.pop(str(nonce or ""), None)


def _name_pairs(text: str) -> list[tuple[str, str]]:
    """「1 阿哲 2 小北」「A 阿哲」都认；不把名字里的数字当序号。

    返回 [(键, 名字)]，键是 ``"3"`` 或 ``"A"``。键的语义由调用方决定：
    数字对应「这场上麦名单」的第几位，字母对应补名卡上的 A/B/C。
    """
    tokens = [x for x in re.split(r"[\s,，;；、]+", str(text or "")) if x]
    out: list[tuple[str, str]] = []
    i = 0
    skipped = 0
    while i < len(tokens):
        tok = tokens[i]
        m = re.match(r"^([A-Ca-c]|\d{1,2})\s*[=:：.、-]\s*(.+)$", tok)
        if m:
            key = m.group(1).upper() if m.group(1).isalpha() else m.group(1)
            out.append((key, m.group(2).strip(" ，。；;,.!?！？")))
            i += 1
            continue
        key = tok.upper() if tok.isalpha() and len(tok) == 1 else tok
        if (key.isdigit() and 1 <= int(key) <= 20) or key in ("A", "B", "C"):
            if i + 1 < len(tokens):
                out.append((key, tokens[i + 1].strip(" ，。；;,.!?！？")))
                i += 2
                continue
        skipped += 1
        i += 1
    # 只有「每一段都是序号+名字」才算补名，避免把「1 个问题 怎么办」当成补名。
    if not out or skipped or i != len(tokens):
        return []
    return [(k, n) for k, n in out if n]


_NAME_PAIR_RE = re.compile(r"^(\d{1,2})\s*[=:：.、]\s*(.+)$")


def _name_pairs_explicit(body: str) -> list[tuple[int, str]]:
    """显式 ``/补名`` 用的宽松解析：能认出「1 阿哲 3号」里的第一位。

    文字兜底（``_name_pairs``）必须严格，避免把普通聊天当成补名；用户主动
    发 ``/补名`` 时则应尽量按他写的序号执行，别因为名字里带数字就整条不认。
    """
    tokens = [x for x in re.split(r"[\s,，;；、]+", str(body or "")) if x]
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(tokens):
        m = _NAME_PAIR_RE.match(tokens[i])
        if m:
            out.append((int(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if tokens[i].isdigit() and i + 1 < len(tokens):
            out.append((int(tokens[i]), tokens[i + 1].strip()))
            i += 2
            continue
        i += 1
    return [(idx, name) for idx, name in out if name]


def _naming_card(pending: dict) -> dict:
    """补名表单卡。表单组件在旧客户端可能不显示，所以每张卡都带文字兜底。"""
    elements: list[dict] = [{
        "tag": "div",
        "text": {"tag": "lark_md", "content":
                 "刚才的试听按 **A / B / C** 排。听不清就按上麦顺序估着填，"
                 "留空跳过；也可以直接回「1 阿哲 2 小北」。"},
    }]
    form_els: list[dict] = []
    for sp in pending.get("speakers") or []:
        sug = str(sp.get("suggestion") or "").strip()
        tip = "（声纹建议：%s，不对就改）" % sug if sug else ""
        form_els.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "**%s** · 本场第 %d 位 · %s · %d 句%s"
                     % (sp.get("letter") or "?", int(sp.get("index") or 0),
                        _hms(sp.get("seconds") or 0),
                        int(sp.get("n_utterances") or 0), tip)},
        })
        inp = {
            "tag": "input", "name": sp.get("field") or "",
            "placeholder": {"tag": "plain_text", "content": "填名字，留空跳过"},
            "max_length": 40,
        }
        if sug:
            inp["default_value"] = sug
        form_els.append(inp)
    form_els.append({
        "tag": "button", "name": "naming_submit",
        "text": {"tag": "plain_text", "content": "保存名字"},
        "type": "primary", "action_type": "form_submit",
        "value": {"act": "naming_submit", "nonce": pending.get("nonce") or ""},
    })
    form_els.append({
        "tag": "button", "name": "naming_cancel",
        "text": {"tag": "plain_text", "content": "这次不补"},
        "type": "default",
        "value": {"act": "naming_cancel", "nonce": pending.get("nonce") or ""},
    })
    elements.append({"tag": "form", "name": "naming_form", "elements": form_els})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "blue",
                       "title": {"tag": "plain_text",
                                 "content": "补名 · 《%s》"
                                            % str(pending.get("title") or "这场")[:20]}},
            "elements": elements}


def _naming_pending(sid: str, title: str, data: dict, open_id: str,
                    chat_id: str, chat_type: str) -> dict:
    """从 top_speakers 的结果里挑前 3 位未命名的，配一张卡。"""
    all_speakers = list(data.get("speakers") or [])
    unnamed = [(i, s) for i, s in enumerate(all_speakers, 1) if not s.get("named")]
    picked = unnamed[:3]
    speakers = []
    for slot, (idx, s) in enumerate(picked):
        sug = (s.get("suggestion") or {}).get("name") if isinstance(
            s.get("suggestion"), dict) else ""
        speakers.append({
            "field": "name_%s" % chr(ord("a") + slot),
            "letter": chr(ord("A") + slot),
            "index": idx,
            "speaker_id": str(s.get("speaker_id") or ""),
            "label": str(s.get("label") or ""),
            "seconds": s.get("seconds") or 0,
            "n_utterances": s.get("n_utterances") or 0,
            "suggestion": str(sug or ""),
            "preview": s.get("preview") or None,
        })
    return {
        "nonce": secrets.token_hex(4),
        "at": time.time(),
        "session_id": sid,
        "title": title,
        "open_id": str(open_id or ""),
        "chat_id": str(chat_id or ""),
        "chat_type": str(chat_type or "p2p"),
        "speakers": speakers,
        "all": [{"index": i, "speaker_id": str(s.get("speaker_id") or ""),
                 "label": str(s.get("label") or ""),
                 "named": bool(s.get("named"))}
                for i, s in enumerate(all_speakers, 1)],
        "has_audio": bool(data.get("has_audio")),
    }


def _send_naming_previews(pending: dict, open_id: str, chat_id: str,
                          chat_type: str) -> None:
    """先发每位的 3 秒试听（有就发），再发卡片。发不出音频只影响试听，不阻塞补名。"""
    sid = str(pending.get("session_id") or "")
    for sp in pending.get("speakers") or []:
        caption = "%s · 本场第 %d 位 · %s · %d 句" % (
            sp.get("letter"), int(sp.get("index") or 0),
            _hms(sp.get("seconds") or 0), int(sp.get("n_utterances") or 0))
        _send_text_target(caption, open_id, chat_id, chat_type)
        preview = sp.get("preview") or {}
        if not (pending.get("has_audio") and preview.get("t_start") is not None):
            continue
        try:
            clip, _err = clips.make_clip(sid, float(preview.get("t_start") or 0),
                                         float(preview.get("seconds") or 3.0))
        except Exception:
            traceback.print_exc()
            clip = None
        if not clip:
            continue
        ms = int(clip.duration * 1000)
        try:
            if chat_type == "group" and chat_id:
                _BOT.send_audio_to_chat(chat_id, clip.path, ms)
            else:
                _BOT.send_audio(clip.path, ms, open_id)
        except Exception:
            traceback.print_exc()


def _send_naming_flow(sid: str, title: str, data: dict, open_id: str,
                      chat_id: str, chat_type: str) -> None:
    """发试听 + 补名表单卡。``/补名`` 和收尾卡上的「补名」共用这一条。"""
    speakers = data.get("speakers") or []
    unnamed = [s for s in speakers if not s.get("named")]
    if not speakers:
        _send_text_target("这场还没有可命名的说话人。",
                          open_id, chat_id, chat_type)
        return
    if not unnamed:
        _send_text_target(
            "《%s》前 %d 位都有名字了。要改名发：/补名 %s 1 新名字"
            % (title, len(speakers), _ch(_room_for_sid(sid)) or "CHxx"),
            open_id, chat_id, chat_type)
        return
    pending = _naming_pending(sid, title, data, open_id, chat_id, chat_type)
    _naming_put(pending)
    _send_text_target(
        "👤《%s》还有 %d 位没名字。先听 3 段 3 秒试听，听到谁就填谁；"
        "填完点卡片保存。\n也可以直接回：1 阿哲 2 小北"
        % (title, len(unnamed)), open_id, chat_id, chat_type)
    _send_naming_previews(pending, open_id, chat_id, chat_type)
    _send_card_target(_naming_card(pending),
                      "补名卡没发出来。直接回「1 阿哲 2 小北」也行。",
                      open_id, chat_id, chat_type)


def _naming_worker_for_sid(sid: str, open_id: str, chat_id: str = "",
                           chat_type: str = "p2p") -> None:
    """收尾卡上的「补名」按钮：按 session_id 直接发试听和表单卡。"""
    try:
        data = _top_speakers(sid)
    except Exception as e:
        _send_text_target("读说话人失败：%s" % _short_err(str(e)),
                          open_id, chat_id, chat_type)
        return
    if not data.get("ok"):
        _send_text_target(str(data.get("error") or "这场还没有可命名的说话人。"),
                          open_id, chat_id, chat_type)
        return
    title = str(data.get("title") or "这场")[:20]
    _send_naming_flow(sid, title, data, open_id, chat_id, chat_type)


def _naming_apply(pending: dict, names: dict[str, str], open_id: str,
                  chat_id: str, chat_type: str) -> None:
    """把名字逐个写回引擎。成功后这张卡就作废；全失败则保留，允许再试。"""
    sid = str(pending.get("session_id") or "")
    done: list[str] = []
    bad: list[str] = []
    for spk, name in names.items():
        try:
            out = _api("/api/speakers/name", {
                "session_id": sid, "speaker_id": spk, "name": name})
        except Exception as e:
            bad.append("%s：%s" % (name, _short_err(str(e))))
            continue
        if not out.get("ok"):
            bad.append("%s：%s" % (name, out.get("error") or "没改成"))
            continue
        tag = "声纹已入库" if out.get("enrolled") else "声纹样本不足，下次还不能自动认"
        done.append("%s（%s）" % (name, tag))
    lines: list[str] = []
    if done:
        lines.append("✅ 已补名：" + "、".join(done))
        lines.append("已发布的场次会在后台重写文档；下次 /找人 名字 就能看到他的观点档案。")
    if bad:
        lines.append("没成功的：" + "；".join(bad))
    if not lines:
        lines = ["没有改动。"]
    if done:
        _naming_drop(str(pending.get("nonce") or ""))
    try:
        _send_text_target("\n".join(lines), open_id, chat_id, chat_type)
    except Exception:
        traceback.print_exc()


def _naming_from_form(value: dict) -> tuple[dict | None, dict[str, str], str]:
    pending = _naming_get(str(value.get("nonce") or ""))
    if not pending:
        return None, {}, "这张补名卡过期了，发 /补名 重新来一次。"
    form = value.get("_form") if isinstance(value.get("_form"), dict) else {}
    names: dict[str, str] = {}
    for sp in pending.get("speakers") or []:
        name = str(form.get(sp.get("field") or "") or "").strip()
        if name:
            names[str(sp.get("speaker_id") or "")] = name[:40]
    if not names:
        return pending, {}, "还没填名字。"
    return pending, names, ""


def _naming_from_text(text: str, open_id: str,
                      chat_id: str) -> tuple[dict | None, dict[str, str], str]:
    """文字兜底：「1 阿哲 2 小北」或「A 阿哲」。没有 pending 就不拦普通聊天。"""
    pending = _naming_for_scope(open_id, chat_id)
    if not pending:
        return None, {}, ""
    pairs = _name_pairs(text)
    if not pairs:
        return None, {}, ""
    by_index = {int(s.get("index") or 0): s for s in pending.get("all") or []}
    by_letter = {str(s.get("letter") or ""): s
                 for s in pending.get("speakers") or []}
    names: dict[str, str] = {}
    for key, name in pairs:
        sp = by_letter.get(key) if key in ("A", "B", "C") else by_index.get(int(key))
        if not sp:
            continue
        spk = str(sp.get("speaker_id") or "")
        if spk and name:
            names[spk] = name[:40]
    if not names:
        return None, {}, ""
    return pending, names, ""


def cmd_name(rest: str, open_id: str = "", chat_id: str = "",
             chat_type: str = "p2p") -> str:
    """补名入口：有「1 阿哲」就直接写回；没有就发试听 + 表单卡。

    直接写回沿用与网页相同的 ``/api/speakers/name``：改显示名、注册声纹、
    重建本场观点归属。
    """
    body = str(rest or "").strip(" ：:")
    room: dict | None = None
    m = re.match(r"(?i)^(CH\s*0*\d{1,3})\b\s*(.*)$", body)
    if m:
        target, body = m.group(1), m.group(2).strip()
        rooms = _rooms()
        room = _resolve_room(rooms, target) if rooms else None
        if not room:
            return "没有找到「%s」。\n%s" % (target, cmd_rooms())
    pairs = _name_pairs_explicit(body)
    if pairs:
        if not room:
            room, cands, err = _pick_room_choice("")
            if not room:
                if cands:
                    return _room_candidates_text(cands, "发：/补名 %s 1 阿哲")
                return err or "现在没有可命名的场次。"
        sid = str(room.get("session_id") or "")
        if not sid:
            return "%s 还没有会话 id。" % _ch(room)
        try:
            data = _top_speakers(sid)
        except Exception as e:
            return "读说话人失败：%s" % _short_err(str(e))
        speakers = data.get("speakers") or []
        if not data.get("ok") or not speakers:
            return str(data.get("error") or "这场还没有说话人数据。")
        pending = {"nonce": "", "session_id": sid}
        names: dict[str, str] = {}
        for idx, name in pairs:
            if not (1 <= idx <= len(speakers)):
                continue
            spk = str(speakers[idx - 1].get("speaker_id") or "")
            if spk:
                names[spk] = name
        if not names:
            return "序号超出范围：这场只列了 %d 位。" % len(speakers)
        _naming_apply(pending, names, open_id, chat_id, chat_type)
        return ""
    # 没有序号：列名单、发试听、发表单卡。
    if not room:
        room, cands, err = _pick_room_choice("")
        if not room:
            if cands:
                return _room_candidates_text(cands, "发：/补名 %s")
            return err or "现在没有可命名的场次。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "%s 还没有会话 id。" % _ch(room)
    try:
        data = _top_speakers(sid)
    except Exception as e:
        return "读说话人失败：%s" % _short_err(str(e))
    if not data.get("ok"):
        return str(data.get("error") or "这场还没有可命名的说话人。")
    title = str(data.get("title") or _room_name(room))[:20]
    _send_naming_flow(sid, title, data, open_id, chat_id, chat_type)
    return ""


def _top_speakers(sid: str) -> dict:
    return _api("/api/sessions/top_speakers?" + urllib.parse.urlencode({
        "session_id": sid, "limit": 5}), timeout=15)


def cmd_rooms() -> str:
    rooms = _rooms()
    if not rooms:
        return "现在一路都没在听。把直播间链接直接粘给我就开始。"
    ordered = _ordered(rooms)
    active = [r for r in rooms if r.get("state") in _ACTIVE]
    lines = ["正在听 %d/%d 路" % (len(active), MAX_ROOMS)]
    for i, r in enumerate(ordered, 1):
        lines.append("%d. %s %s" % (i, _ch(r), _label(r)))
        if r.get("url"):
            lines.append("   %s" % r["url"])
    lines.append("")
    lines.append("状态：/状态 或 /状态 CH02")
    lines.append("停一路：/stop 1（上面的序号）或 /stop CH02（固定编号）")
    lines.append("管理页：%s/rooms" % lan.base_url())
    return "\n".join(lines)


_ENGINES = {"direct": "直取", "per_room": "独立浏览器", "shared": "共享浏览器"}


def _last_line(sess: dict) -> str:
    line = str(sess.get("last_line") or "").strip()
    if line:
        return line
    utts = sess.get("utterances") or []
    if utts:
        return str((utts[-1] or {}).get("text") or "").strip()
    return ""


def _status_one(r: dict, sess: dict | None, cap: dict | None) -> str:
    state = str(r.get("state") or "")
    chunk = sess.get("last_chunk_ago_s") if sess else None
    stalled = (state == "live" and chunk is not None
               and float(chunk) >= LIVE_STALL_WARN_S)
    if state == "live" and sess and sess.get("paused"):
        icon, cn = "⏸", "已暂停"
    elif stalled:
        icon, cn = "⚠️", "断流"
    else:
        icon, cn = _STATE.get(state, ("·", state or "未知"))
    lines = ["%s %s  %s %s" % (_ch(r), r.get("name") or "（还在读名字）", icon, cn)]

    if state == "failed":
        if r.get("failed_stage") is not None:
            lines.append("  停在：第 %s 步失败" % r["failed_stage"])
        lines.append("  原因：%s" % str(r.get("error") or "采集端没给原因")[:120])
        if r.get("switch_ready"):
            eng = _ENGINES.get(str(r.get("switch_engine") or ""), str(r.get("switch_engine") or ""))
            lines.append("  直取失败，可以在收听页切换到%s采集。" % eng)
        return "\n".join(lines)

    if state in ("pending", "starting"):
        step = r.get("stage")
        if step:
            note = str(r.get("stage_note") or "").strip()
            lines.append("  启动第 %s 步%s" % (step, ("：" + note) if note else ""))
        else:
            lines.append("  等采集端拉起浏览器…")
        return "\n".join(lines)

    if state == "stopped":
        lines.append("  已停止%s" % ("（自动收尾）" if r.get("auto_stopped") else ""))
        if r.get("auto_stop_reason"):
            lines.append("  原因：%s" % str(r["auto_stop_reason"])[:100])
        lines.append("  录音：%s" % ("已保存" if r.get("has_recording") else "没有录到"))
        sid = str(r.get("session_id") or "")
        if sid:
            lines.append("  " + _play_hint(r))
        return "\n".join(lines)

    sid = str(r.get("session_id") or "")
    if sid and not sess:
        lines.append("  会话丢失：服务可能刚重启，或采集页已关闭。")
        if r.get("has_recording"):
            lines.append("  " + _play_hint(r))
        return "\n".join(lines)

    if not sess:
        lines.append("  还没有会话，等采集端建会话。")
    elif sess.get("capture_only"):
        # 仅采集：逐字稿由离线进程补写。还没有字时写「仅录制」，补上之后
        # 直接给条数和最后一句，别让「0 句」看起来像故障。
        lines.append("  %s%s" % (_capture_bits(sess),
                                 " · 已暂停" if sess.get("paused") else ""))
        if chunk is None:
            lines.append("  最近一块音频：—")
        else:
            broken = state == "live" and float(chunk) >= LIVE_STALL_WARN_S
            lines.append("  最近一块音频：%s%s" % (_ago(chunk), "  ⚠️ 断流" if broken else ""))
        last = str(sess.get("last_line") or "")
        if last:
            lines.append("  最后一句 %s" % last[:40])
        eng = str(r.get("capture_engine") or "")
        if eng:
            lines.append("  采集：%s" % _ENGINES.get(eng, eng))
    else:
        lines.append("  已录 %s · %s 句%s" % (
            _hms(sess.get("elapsed")), int(sess.get("n_utterances") or 0),
            " · 已暂停" if sess.get("paused") else ""))
        last = _last_line(sess)
        if last:
            ago = sess.get("last_final_ago_s")
            lines.append("  最后一句：%s（%s）" % (
                last[:50], _ago(ago) if ago is not None else "刚开听"))
        else:
            lines.append("  还没有字幕，等主播开口。")
        if chunk is None:
            lines.append("  最近一块音频：—")
        else:
            broken = state == "live" and float(chunk) >= LIVE_STALL_WARN_S
            lines.append("  最近一块音频：%s%s" % (_ago(chunk), "  ⚠️ 断流" if broken else ""))
        eng = str(r.get("capture_engine") or "")
        if eng:
            lines.append("  采集：%s" % _ENGINES.get(eng, eng))

    if cap:
        if cap.get("alive") is False:
            why = str(cap.get("reason") or "").strip()
            lines.append("  采集代理：已退出%s" % (("（%s）" % why[:80]) if why else ""))
        elif float(cap.get("age_s") or 0) > 30:
            lines.append("  采集代理：心跳超时 %s" % _ago(cap.get("age_s")))
        else:
            lines.append("  采集代理：运行中")
    elif state == "live":
        lines.append("  采集代理：没有心跳")
    return "\n".join(lines)


def cmd_status(arg: str = "") -> str:
    """0 token 的现场状态：房间 + 会话 + 采集心跳，不调模型。"""
    rooms = _rooms()
    if not rooms:
        return "现在一路都没在听。把直播间链接发给我就开听。"

    target = (arg or "").strip()
    if target:
        room = _resolve_room(rooms, target)
        if not room:
            return "没有找到「%s」。现在有：%s" % (target, _ch_list(rooms))
        picked = [room]
    else:
        picked = [r for r in _ordered(rooms) if r.get("state") in _ACTIVE]
        if not picked:
            last = _ordered(rooms)[0]
            return ("现在没有在听。最近一路：%s %s\n发 /rooms 看全部。"
                    % (_ch(last), _label(last)))

    try:
        sessions = {str(s.get("id")): s
                    for s in (_api("/api/live/list").get("sessions") or [])}
    except Exception:
        sessions = {}
    try:
        capture = _api("/api/capture/status").get("rooms") or {}
    except Exception:
        capture = {}

    blocks = []
    for r in picked:
        sid = str(r.get("session_id") or "")
        sess = sessions.get(sid)
        # 单路时再问一次详情，拿到最新台词；多路只靠列表，避免 N 次请求。
        if sid and r.get("state") in _ACTIVE and (sess is not None or len(picked) == 1):
            try:
                fresh = _api("/api/live/status?id=%s" % urllib.parse.quote(sid))
                if fresh:
                    sess = fresh
            except Exception:
                pass
        blocks.append(_status_one(r, sess, capture.get(str(r.get("id") or ""))))
    if len(blocks) == 1:
        return blocks[0]
    return "🎧 %d 路状态\n\n%s" % (len(blocks), "\n\n".join(blocks))


def _send_text_target(text: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> bool:
    if not text:
        return False
    if chat_type == "group" and chat_id:
        fn = getattr(_BOT, "send_text_to_chat", None)
        if fn:
            return fn(chat_id, text)
    return _BOT.send_text(text, open_id)


def _span_text(start: float | None, duration: float) -> str:
    if start is None:
        return "最近 %d 秒" % int(duration)
    return "从 %s 起 %d 秒" % (_ts(start), int(duration))


# ---------------------------------------------------------------- 片段分享

_SHARE_PENDING: dict[str, dict] = {}
_SHARE_LOCK = threading.Lock()


def _share_nonce(payload: dict) -> str:
    nonce = secrets.token_hex(3).upper()
    with _SHARE_LOCK:
        now = time.time()
        for k in [k for k, v in _SHARE_PENDING.items()
                  if float(v.get("expires") or 0) < now]:
            _SHARE_PENDING.pop(k, None)
        _SHARE_PENDING[nonce] = dict(payload, expires=now + 300)
    return nonce


def _take_share(nonce: str) -> dict | None:
    key = str(nonce or "").upper()
    with _SHARE_LOCK:
        p = _SHARE_PENDING.pop(key, None)
    if not p or float(p.get("expires") or 0) < time.time():
        return None
    return p


def _share_confirm_card(room: dict, nonce: str, start: float | None,
                        duration: float) -> dict:
    body = ("将把 **%s %s** 的 %s 原声发到本群。\n"
            "音频转发后不可撤回，请确认群里的人都可听。"
            % (_ch(room), _md_safe(_room_name(room)),
               _span_text(start, duration)))
    return _brief_card(
        "🎧 要分享这一段原声吗", "orange", body,
        (_btn("发送", {"act": "share_confirm", "nonce": nonce}, True),
         _btn("取消", {"act": "share_cancel", "nonce": nonce})),
        "按钮没反应就回 /确认分享 %s（5 分钟内有效）" % nonce)


def _share_confirm_text(room: dict, nonce: str, start: float | None,
                        duration: float) -> str:
    return ("要把 %s %s 的 %s 原声发到本群吗？\n"
            "音频转发后不可撤回。回复 /确认分享 %s 开始（5 分钟内有效）。"
            % (_ch(room), _room_name(room), _span_text(start, duration), nonce))


def _short_err(text: str, limit: int = 100) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _friendly_net_error(text: str) -> str:
    """飞书 SDK 抛的是 requests 的长异常，给用户看要收敛成一句。"""
    s = str(text or "").lower()
    if "99991672" in s or "131003" in s or "no permission" in s or "permission" in s:
        return "应用没有上传权限，去后台开通 im:resource"
    if "timed out" in s or "timeout" in s:
        return "连接 open.feishu.cn 超时"
    if "connection" in s or "max retries" in s:
        return "连接 open.feishu.cn 失败"
    return _short_err(text) or "飞书接口没通"


def _share_retry_card(room: dict, start: float | None, duration: float,
                      message: str) -> dict:
    value = {"act": "share_clip", "room": room.get("id") or "",
             "seconds": int(duration)}
    if start is not None:
        value["start"] = float(start)
    return _brief_card(
        "⚠️ 分享没发出去", "red",
        _md_safe(message) + "\n\n片段在本机已经生成，重试不会重新截取。",
        (_btn("重试", value, True),),
        _note_with_tz(room, "还不行就发 /分享 %s（默认最近90秒）" % _ch(room)))


def _share_worker(room: dict, sid: str, start: float | None, duration: float,
                  open_id: str, chat_id: str = "", chat_type: str = "p2p") -> None:
    retry_start = start

    def say(text: str) -> None:
        try:
            _send_text_target(text, open_id, chat_id, chat_type)
        except Exception:
            traceback.print_exc()

    def fail(text: str) -> None:
        # 失败不占 30 秒冷却，用户点「重试」要能立刻发。
        clips.release(sid)
        try:
            _send_card_target(_share_retry_card(room, retry_start, duration, text),
                              text, open_id, chat_id, chat_type)
        except Exception:
            traceback.print_exc()
            say(text)

    try:
        ok, wait = clips.allow(sid)
        if not ok:
            say("刚分享过这一场，%d 秒后再试。" % max(1, int(wait)))
            return
        clip, err = clips.make_clip(sid, start, duration)
        if not clip:
            clips.release(sid)
            say("没切成片段：%s" % err)
            return
        # 「最近 N 秒」在这里才解析成具体起点。重试卡必须钉住这个起点，
        # 否则失败后过一会儿再点「重试」，会按新的末尾重新截一段——既不是
        # 用户刚才听到的那段，也和「片段已经生成」的提示对不上。
        retry_start = clip.start
        span = "%s–%s" % (_ts(clip.start), _ts(min(clip.total,
                                                    clip.start + clip.duration)))
        ms = int(clip.duration * 1000)
        sent, err_text = False, ""
        try:
            if chat_type == "group" and chat_id:
                sent = bool(_BOT.send_audio_to_chat(chat_id, clip.path, ms))
            else:
                sent = bool(_BOT.send_audio(clip.path, ms, open_id))
        except Exception as e:
            traceback.print_exc()
            err_text = str(e)
            sent = False
        if not sent:
            try:
                if chat_type == "group" and chat_id:
                    sent = bool(_BOT.send_file_to_chat(chat_id, clip.path))
                else:
                    sent = bool(_BOT.send_file(clip.path, open_id))
            except Exception as e:
                traceback.print_exc()
                err_text = err_text or str(e)
                sent = False
        if not sent:
            err_text = err_text or getattr(_BOT, "last_error", "")
            link = (("%s/live?session=%s&t=%d" if room.get("state") in _ACTIVE
                     else "%s/reader?session=%s&t=%d")
                    % (lan.base_url(), sid, int(clip.start)))
            fail("飞书接口没发出去（%s）。\n%s 的片段已经生成，也可以先在本机回听：%s"
                 % (_friendly_net_error(err_text), span, link))
            return
        # 发送成功后再发抬头，避免出现「有抬头、没音频」的半条消息。
        say("🎧 %s · %s（%d 秒）\n《%s》"
            % (_ch(room), span, int(clip.duration), _room_name(room)[:24]))
        print("[分享] %s %s %s -> %s" % (_ch(room), sid, span,
                                       "群" if chat_type == "group" else "私聊"),
              flush=True)
    except Exception as e:
        traceback.print_exc()
        clips.release(sid)
        say("分享失败：%s" % _short_err(e))


def _share_action(act: str, value: dict, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> None:
    if act == "share_cancel":
        _take_share(value.get("nonce") or "")
        _send_text_target("好，已取消。", open_id, chat_id, chat_type)
        return
    if act == "share_confirm":
        p = _take_share(value.get("nonce") or "")
        if not p:
            _send_text_target("确认码过期或已经用过了。发 /分享 重新来一次。",
                              open_id, chat_id, chat_type)
            return
        room = _room_by_id(p.get("room_id") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _share_worker(room, str(p.get("sid") or ""), p.get("start"),
                      float(p.get("duration") or 90), p.get("open_id") or open_id,
                      p.get("chat_id") or chat_id, p.get("chat_type") or chat_type)
        return
    if act == "share_clip":
        room = _room_by_id(value.get("room") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        sid = str(room.get("session_id") or "")
        if not sid:
            _send_text_target("这一路还没有会话，等几秒再试。",
                              open_id, chat_id, chat_type)
            return
        _share_worker(room, sid, value.get("start"),
                      float(value.get("seconds") or 90), open_id,
                      chat_id, chat_type)


def cmd_share(rest: str, open_id: str, chat_id: str = "",
              chat_type: str = "p2p", confirmed: bool = False) -> str:
    args = clips.parse_share_args("/分享 " + str(rest or ""))
    room, candidates, err = _pick_room_choice(args["ch"])
    if not room:
        if candidates:
            lines = ["有 %d 路可用，选一路再发：" % len(candidates)]
            for r in candidates[:5]:
                lines.append("%s %s（%s）" % (_ch(r), _room_name(r),
                                             _room_state_label(r)))
            lines.append("例：/分享 %s 90秒" % _ch(candidates[0]))
            return "\n".join(lines)
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    if chat_type == "group" and chat_id and not confirmed:
        payload = {"room_id": room.get("id") or "", "sid": sid,
                   "start": args["start"], "duration": args["duration"],
                   "open_id": open_id, "chat_id": chat_id, "chat_type": chat_type}
        nonce = _share_nonce(payload)
        _send_card_target(
            _share_confirm_card(room, nonce, args["start"], args["duration"]),
            _share_confirm_text(room, nonce, args["start"], args["duration"]),
            open_id, chat_id, chat_type)
        return ""
    _send_text_target("⏳ 正在截取 %s 的 %s…"
                      % (_ch(room), _span_text(args["start"], args["duration"])),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_share_worker,
                     args=(room, sid, args["start"], args["duration"],
                           open_id, chat_id, chat_type), daemon=True).start()
    return ""


def cmd_share_confirm(nonce: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> str:
    p = _take_share(nonce)
    if not p:
        return "确认码过期或已经用过了。发 /分享 重新来一次。"
    room = _room_by_id(p.get("room_id") or "")
    if not room:
        return "这个房间已经不在了，发 /rooms 看看。"
    _send_text_target("⏳ 正在截取 %s 的 %s…"
                      % (_ch(room), _span_text(p.get("start"),
                                               float(p.get("duration") or 90))),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_share_worker,
                     args=(room, str(p.get("sid") or ""), p.get("start"),
                           float(p.get("duration") or 90), p.get("open_id") or open_id,
                           p.get("chat_id") or chat_id, p.get("chat_type") or chat_type),
                     daemon=True).start()
    return ""


def _room_candidates_text(candidates: list[dict], example: str) -> str:
    lines = ["有 %d 路可用，选一路再发：" % len(candidates)]
    for r in candidates[:5]:
        lines.append("%s %s（%s）" % (_ch(r), _room_name(r), _room_state_label(r)))
    lines.append(example % _ch(candidates[0]))
    return "\n".join(lines)


def _package_worker(room: dict, sid: str, open_id: str, chat_id: str = "",
                    chat_type: str = "p2p") -> None:
    def say(text: str) -> None:
        _send_text_target(text, open_id, chat_id, chat_type)

    conn = store.connect()
    try:
        info = share.build_package(conn, sid)
    except ValueError as e:
        say("打包失败：%s" % e)
        return
    except Exception as e:
        traceback.print_exc()
        say("打包失败：%s" % e)
        return
    finally:
        conn.close()
    try:
        zpath = share.zip_package(info["dir"])
    except Exception as e:
        traceback.print_exc()
        say("压缩失败：%s" % e)
        return
    mb = zpath.stat().st_size / 1048576.0
    say("📦 %s 分享包：%d 个文件，%.1f MB\n音频：%s"
        % (_ch(room), len(info["files"]) + 1, mb, info.get("audio") or "（无）"))
    sent = False
    try:
        if chat_type == "group" and chat_id:
            sent = bool(_BOT.send_file_to_chat(chat_id, zpath))
        else:
            sent = bool(_BOT.send_file(zpath, open_id))
    except Exception:
        traceback.print_exc()
        sent = False
    if not sent:
        say("文件没能发到飞书（可能超过大小上限）。本机路径：%s" % zpath)


def cmd_package(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    if chat_type == "group" and chat_id:
        return "分享包里是整场原声，私聊我发 /分享包，避免直接发到群里。"
    room, candidates, err = _pick_room_choice(_room_arg(rest) or str(rest or "").strip())
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/分享包 %s")
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    _send_text_target("⏳ 正在打包 %s（摘要 + 字幕 + 音频）…" % _ch(room),
                      open_id, chat_id, chat_type)
    threading.Thread(target=_package_worker,
                     args=(room, sid, open_id, chat_id, chat_type),
                     daemon=True).start()
    return ""


def _report_worker(kind: str, since, until, open_id: str, chat_id: str = "",
                   chat_type: str = "p2p") -> None:
    def say(text: str) -> None:
        _send_text_target(text, open_id, chat_id, chat_type)

    conn = store.connect()
    try:
        info = report.build(conn, kind=kind, since=since, until=until)
    except Exception as e:
        traceback.print_exc()
        say("生成失败：%s" % e)
        return
    finally:
        conn.close()
    if not info["sessions"]:
        say("这段时间（%s）没有可汇总的场次。" % report.range_label(since, until))
        return
    st = info["stats"]
    head = ("%s\n%d 场 · %s · %d 场有总结" % (
        info["title"], st["sessions"], report.duration_text(st["duration_s"]),
        st["with_summary"]))
    created = None
    fn = getattr(_BOT, "create_docx", None)
    if callable(fn):
        try:
            created = fn(info["title"], info["markdown"], share_with=open_id or "")
        except Exception:
            traceback.print_exc()
            created = None
    access = ""
    if isinstance(created, dict):
        if created.get("shared"):
            access = "（已授权给你）"
        elif created.get("share_requested"):
            access = "（自动授权失败：%s）" % (
                created.get("share_error") or "未知原因")
    if isinstance(created, dict) and created.get("ok"):
        say("%s\n飞书云文档%s：%s\n本机备份：%s" % (
            head, access, created.get("url") or "（没拿到链接）", info.get("path") or ""))
        return
    why = (created or {}).get("error") if isinstance(created, dict) else ""
    why = why or "机器人没有云文档权限或租户未开通"
    # create_docx 在「文档建好但正文写入失败」时会带回 url：这时的文档是空的，
    # 但链接本身有效，别把已经建出来的文档丢掉，让用户自己去补正文。
    partial = (created or {}).get("url") if isinstance(created, dict) else ""
    if partial:
        say("%s\n飞书云文档已建好%s，但正文没写进去（%s）：%s\n"
            "已生成 Markdown，可导入或粘贴到飞书文档。" % (
                head, access, why, partial))
    else:
        say("%s\n没能直接建飞书云文档（%s）。\n"
            "已生成 Markdown，可导入或粘贴到飞书文档。" % (head, why))
    path = info.get("path")
    if not path:
        return
    try:
        sent = bool(_BOT.send_file(path, open_id))
    except Exception:
        traceback.print_exc()
        sent = False
    if not sent:
        say("文件没能发到飞书。本机路径：%s" % path)


def _cmd_report(kind: str, rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    if chat_type == "group" and chat_id:
        return "周报/复盘会汇总多场内容，私聊我生成。"
    try:
        r = report.parse_range(rest)
    except ValueError as e:
        return str(e) + "\n例：/周报 上周、/周报 7天、/周报 11-03~11-09"
    _send_text_target("⏳ 正在汇总 %s…" % r["label"], open_id, chat_id, chat_type)
    threading.Thread(target=_report_worker,
                     args=(kind, r["since"], r["until"], open_id, chat_id, chat_type),
                     daemon=True).start()
    return ""


def cmd_weekly(rest: str, open_id: str, chat_id: str = "",
               chat_type: str = "p2p") -> str:
    return _cmd_report("weekly", rest, open_id, chat_id, chat_type)


def cmd_retro(rest: str, open_id: str, chat_id: str = "",
              chat_type: str = "p2p") -> str:
    return _cmd_report("retro", rest, open_id, chat_id, chat_type)


def cmd_minutes(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    raw = str(rest or "").strip()
    m = re.search(r"https?://\S+", raw)
    url = m.group(0).rstrip("，。；;、)）】\"'") if m else ""
    target = ((raw[:m.start()] + " " + raw[m.end():]).strip() if m else raw)
    room, candidates, err = _pick_room_choice(_room_arg(target) or target)
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/妙记 %s " + (url or "<链接>"))
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话，等几秒再试。"
    if not url:
        return "用法：/妙记 CH01 https://xxx.feishu.cn/minutes/xxxx\n先上传 /分享包 里的音频，再把妙记链接发我。"
    conn = store.connect()
    try:
        store.set_minutes_url(conn, sid, url)
    finally:
        conn.close()
    return ("✅ 已记下 %s 的妙记链接：\n%s\n"
            "之后 /回听 %s 会优先给这个链接，简报卡片也会带上。"
            % (_ch(room), url, _ch(room)))


def cmd_replay(rest: str, open_id: str = "", chat_id: str = "",
               chat_type: str = "p2p") -> str:
    room, candidates, err = _pick_room_choice(_room_arg(rest) or str(rest or "").strip())
    if not room:
        if candidates:
            return _room_candidates_text(candidates, "例：/回听 %s")
        return err or "没找到这一路。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "这一路还没有会话。"
    conn = store.connect()
    try:
        s = store.get_session(conn, sid) or {}
    finally:
        conn.close()
    minutes = str(s.get("minutes_url") or "")
    if minutes:
        return "🎧 %s 整场回听（妙记）：\n%s" % (_ch(room), minutes)
    base = _public_base()
    if CAPTURE_ONLY and not base:
        return ("🎧 %s 在云端，没有公网页面，手机打不开网页回放。\n"
                "发 /分享 %s 90秒 听最近一段，或 /分享包 %s 下载整场。"
                % (_ch(room), _ch(room), _ch(room)))
    link = ("%s/live?session=%s" if room.get("state") in _ACTIVE
            else "%s/reader?session=%s") % (base or lan.base_url(), sid)
    return ("🎧 %s 回听：\n%s\n"
            "这是局域网地址，转发给别人打不开；外发请先在妙记上传 /分享包，"
            "再发 /妙记 %s <链接>。" % (_ch(room), link, _ch(room)))


def _send_card_target(card: dict, text: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> bool:
    ok = False
    try:
        if chat_type == "group" and chat_id:
            fn = getattr(_BOT, "send_card_to_chat", None)
            ok = bool(fn(chat_id, card)) if fn else False
        else:
            ok = bool(_BOT.send_card(card, open_id))
    except Exception:
        traceback.print_exc()
        ok = False
    if not ok:
        return _send_text_target(text, open_id, chat_id, chat_type)
    return True


def _brief_api(kind: str, room_id: str = "", **params) -> dict:
    qs = {"kind": kind}
    if room_id:
        qs["room_id"] = room_id
    qs.update({k: v for k, v in params.items() if v is not None})
    timeout = float(params.get("timeout") or 45)
    return _api("/api/live/brief?" + urllib.parse.urlencode(qs),
                timeout=max(6.0, timeout + 5))


def _brief_error(data: dict) -> str:
    err = data.get("error") or {}
    # 服务端 404/500 的兜底响应是 {"error": "文本"}，不是 BriefError 的字典；
    # 错误处理不能再抛异常，否则用户只看到“正在整理…”然后没有下文。
    if not isinstance(err, dict):
        return str(err) or "简报生成失败"
    code = str(err.get("code") or "")
    msg = str(err.get("message") or "简报生成失败")
    if code in ("ambiguous_room", "room_not_found", "no_active_room"):
        return msg + "\n" + cmd_rooms()
    if code == "no_session":
        return msg + "\n发 /状态 看现场，或等采集端重新建会话。"
    if code == "busy":
        wait = err.get("retry_after_s")
        return msg + (("\n约 %d 秒后再试。" % int(wait)) if wait else "")
    return msg


def _spawn(fn, err_to: str, *args, **kwargs) -> None:
    def _run():
        try:
            fn(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            try:
                _send_text_target("简报没生成成功，稍后再试。", err_to)
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()


def _room_name(room: dict) -> str:
    return str(room.get("name") or "未命名直播间")


def _pick_room_choice(target: str) -> tuple[dict | None, list[dict], str]:
    """房间选择：唯一可用的直接返回，多路返回候选，不再用链接让用户猜。"""
    rooms = _rooms()
    if not rooms:
        return None, [], "现在一路都没在听。把直播间链接发给我就开听。"
    if target:
        room = _resolve_room(rooms, target)
        if not room:
            return None, [], "没有找到「%s」。现在有：%s" % (target, _ch_list(rooms))
        return room, [], ""
    active = [r for r in _ordered(rooms) if r.get("state") in _ACTIVE]
    ready = [r for r in active if r.get("session_id")]
    if len(ready) == 1:
        return ready[0], [], ""
    if len(ready) > 1:
        return None, ready, ""
    if active:
        return None, [], "这一路正在启动，还没有会话。等几秒再发一次。"
    history = [r for r in _ordered(rooms) if r.get("session_id")]
    if len(history) == 1:
        return history[0], [], ""
    if len(history) > 1:
        return None, history, ""
    return None, [], "现在还没有可用的会话。发 /状态 看现场，或贴直播间链接。"


def _room_by_id(room_id: str) -> dict | None:
    for r in _rooms():
        if str(r.get("id") or "") == str(room_id or ""):
            return r
    return None


# ---------------------------------------------------------------- 简报格式

def _md_safe(s: str) -> str:
    return re.sub(r"[<>]", "", str(s or "")).replace("`", "'").strip()


def _topics_text(data: dict) -> str:
    w = data.get("window") or {}
    cov = data.get("coverage") or {}
    room = data.get("room") or {}
    tz = "北京时间" if w.get("clock_tz") else ""
    head = "📻 %s · 最近 %d 分钟（%s–%s%s）" % (
        room.get("ch") or "CH—", int(w.get("minutes") or 10),
        w.get("from_clock") or "—", w.get("to_clock") or "—",
        (" " + tz) if tz else "")
    lines = [head]
    extra = "%d 句 · 纳入 %d 句" % (int(cov.get("matched") or 0),
                                   int(cov.get("included") or 0))
    if cov.get("last_utterance_ago_s") is not None:
        extra += " · 最后一句 %s" % _ago(cov["last_utterance_ago_s"])
    lines.append(extra)
    if data.get("cached"):
        ts = str(data.get("cached_at") or data.get("as_of") or "")
        if len(ts) >= 19:
            lines.append("缓存于 %s · 发 /刷新 重做" % ts[11:19])
    if data.get("notice"):
        lines.append("提示：" + str(data["notice"].get("message") or ""))
    for i, t in enumerate(data.get("topics") or [], 1):
        lines.append("")
        lines.append("%d. %s（%s）" % (
            i, _md_safe(t.get("title")), _span(t)))
        lines.append("   " + _md_safe(t.get("gist")))
        ev = t.get("evidence") or []
        if ev:
            lines.append("   「%s」" % _md_safe(ev[0].get("quote")))
            if _is_phone_url(ev[0].get("reader_url")):
                lines.append("   回听 %s" % ev[0].get("reader_url"))
    if data.get("caveats"):
        lines.append("")
        lines.append("说明：" + "；".join(str(x) for x in data["caveats"][:2]))
    if data.get("minutes_url"):
        lines.append("")
        lines.append("整场回听 %s" % data["minutes_url"])
    if not _public_base():
        lines.append("")
        if CAPTURE_ONLY:
            lines.append("（云端没有公网回听页；听原声发 /分享 %s 90秒）"
                         % (room.get("ch") or "CH01"))
        else:
            lines.append("（回听链接是电脑本机地址，手机上打不开；"
                         "在电脑上打开设置 →「手机访问」开启后重新生成。）")
    return "\n".join(lines)


def _summary_text(data: dict) -> str:
    room = data.get("room") or {}
    cov = data.get("coverage") or {}
    as_of = str(data.get("as_of") or "")
    head = "📝 %s · 截至 %s" % (room.get("ch") or "CH—", as_of[11:19] or "—")
    if cov.get("start_clock"):
        head += " · 开播 %s（北京时间）· 已录 %s" % (
            cov["start_clock"], _hms(cov.get("t_to")))
    else:
        head += " · 开播 %s" % _hms(cov.get("t_to"))
    lines = [head]
    line = "覆盖 %d 句 / %s" % (int(cov.get("included") or 0), _hms(cov.get("t_to")))
    if cov.get("sampled_rows"):
        line += " · 抽样 %d 句" % int(cov["sampled_rows"])
    if cov.get("excluded_low_value_total"):
        line += " · 剔除 %d 句低价值" % int(cov["excluded_low_value_total"])
    lines.append(line)
    if data.get("cached"):
        ts = str(data.get("cached_at") or data.get("as_of") or "")
        if len(ts) >= 19:
            lines.append("缓存于 %s · 发 /刷新 重做" % ts[11:19])
    if data.get("notice"):
        lines.append("提示：" + str(data["notice"].get("message") or ""))
    if data.get("overview"):
        lines.append("")
        lines.append("概述：" + _md_safe(data["overview"]))
    for i, p in enumerate(data.get("points") or [], 1):
        lines.append("")
        lines.append("%d. %s（%s）" % (
            i, _md_safe(p.get("title")), _span(p)))
        lines.append("   " + _md_safe(p.get("gist")))
        ev = p.get("evidence") or []
        if ev:
            lines.append("   「%s」" % _md_safe(ev[0].get("quote")))
            if _is_phone_url(ev[0].get("reader_url")):
                lines.append("   回听 %s" % ev[0].get("reader_url"))
    if data.get("caveats"):
        lines.append("")
        lines.append("说明：" + "；".join(str(x) for x in data["caveats"][:3]))
    if data.get("minutes_url"):
        lines.append("")
        lines.append("整场回听 %s" % data["minutes_url"])
    if not _public_base():
        lines.append("")
        if CAPTURE_ONLY:
            lines.append("（云端没有公网回听页；听原声发 /分享 %s 90秒）"
                         % (room.get("ch") or "CH01"))
        else:
            lines.append("（回听链接是电脑本机地址，手机上打不开；"
                         "在电脑上打开设置 →「手机访问」开启后重新生成。）")
    return "\n".join(lines)


def _btn(label: str, value: dict, primary: bool = False) -> dict:
    return {"tag": "button", "type": "primary" if primary else "default",
            "text": {"tag": "plain_text", "content": label}, "value": value}


def _brief_card(title: str, template: str, body: str,
                buttons: tuple[dict, ...] = (), note: str = "") -> dict:
    elements: list[dict] = [{"tag": "div",
                             "text": {"tag": "lark_md", "content": body}}]
    if buttons:
        elements.append({"tag": "action", "actions": list(buttons)})
    if note:
        elements.append({"tag": "note",
                         "elements": [{"tag": "plain_text", "content": note}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": template,
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _topics_card(data: dict) -> dict:
    room = data.get("room") or {}
    w = data.get("window") or {}
    rid = room.get("room_id") or ""
    minutes = int(w.get("minutes") or 10)
    body = _topics_text(data).split("\n", 2)[-1]   # 去掉标题两行，卡片头已有
    return _brief_card(
        "📻 %s · 最近 %d 分钟" % (room.get("ch") or "CH—", minutes), "blue", body,
        (_btn("刷新", {"act": "brief_refresh", "kind": "topics",
                       "room": rid, "minutes": minutes}),
         _btn("分享最近90秒", {"act": "share_clip", "room": rid, "seconds": 90}),
         _btn("整场总结", {"act": "brief_summary", "room": rid,
                          "confirm": 1}, True)),
        _note_with_tz(data, "按钮没反应就发 /近聊 %s" % (room.get("ch") or "CH02")))


def _summary_card(data: dict) -> dict:
    room = data.get("room") or {}
    rid = room.get("room_id") or ""
    body = _summary_text(data).split("\n", 2)[-1]
    return _brief_card(
        "📝 %s · 阶段性总结" % (room.get("ch") or "CH—"), "green", body,
        (_btn("刷新", {"act": "brief_refresh", "kind": "summary", "room": rid}, True),
         _btn("分享最近90秒", {"act": "share_clip", "room": rid, "seconds": 90})),
        _note_with_tz(data, "按钮没反应就发 /刷新；这是阶段性总结，不是最终稿"))


def _brief_confirm_card(room: dict, nonce: str, rows: int) -> dict:
    body = ("将整理 **%s %s** 开播至今的内容（约 %d 句）。\n"
            "预计 1 次模型调用、15~30 秒；结果只用于这次问答，不会改场次名。"
            % (_ch(room), _md_safe(room.get("name") or ""), max(0, int(rows or 0))))
    return _brief_card(
        "📝 要生成阶段性总结吗", "orange", body,
        (_btn("生成", {"act": "brief_confirm", "nonce": nonce,
                       "room": room.get("id") or ""}, True),
         _btn("不用了", {"act": "brief_cancel", "nonce": nonce})),
        "按钮没反应就发 /确认简报 %s（5 分钟内有效）" % nonce)


def _brief_confirm_text(room: dict, nonce: str, rows: int) -> str:
    return ("要整理 %s %s 开播至今的内容（约 %d 句）吗？\n"
            "回复 /确认简报 %s 开始（5 分钟内有效）。"
            % (_ch(room), room.get("name") or "", max(0, int(rows or 0)), nonce))


def _status_icon(state: str) -> str:
    return _STATE.get(state, ("·", state))[0]


def _capture_bits(r: dict) -> str:
    """仅采集场次的时长文案：补转写有结果后别再只写「仅录制」。"""
    n = int(r.get("n_utterances") or 0)
    return "已录 %s · 有声 %s · %s" % (
        _hms(r.get("elapsed")), _hms(r.get("audible_s")),
        ("补转写 %d 句" % n) if n else "仅录制")


def _console_card(data: dict) -> dict:
    rows = data.get("rooms") or []
    elements: list[dict] = []
    for r in rows:
        bits = ["**%s %s**  %s" % (r.get("ch") or "CH—",
                                   _md_safe(r.get("name") or ""),
                                   _status_icon(r.get("room_state")))]
        if r.get("elapsed") is not None:
            if r.get("capture_only"):
                bits.append(_capture_bits(r))
            else:
                bits.append("已录 %s · %s 句" % (_hms(r.get("elapsed")),
                                                int(r.get("n_utterances") or 0)))
        if r.get("last_line"):
            bits.append("最后一句 %s" % _md_safe(str(r["last_line"])[:36]))
        if r.get("capture_engine"):
            bits.append("采集：%s" % _ENGINES.get(str(r["capture_engine"]),
                                                str(r["capture_engine"])))
        if r.get("stalled"):
            bits.append("⚠️ 断流 %s" % _ago(r.get("last_chunk_ago_s")))
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": "\n".join(bits)}})
        acts = [_btn("状态", {"act": "brief_status", "room": r.get("room_id")})]
        if not r.get("capture_only") or int(r.get("n_utterances") or 0) > 0:
            # 仅采集要等离线补转写出字；有字之后近聊/总结才有意义。
            acts += [_btn("近10分钟", {"act": "brief_topics", "room": r.get("room_id"),
                                      "minutes": 10}),
                     _btn("总结", {"act": "brief_summary", "room": r.get("room_id"),
                                  "confirm": 1}, True)]
        if r.get("session_id"):
            acts.append(_btn("分享90秒", {"act": "share_clip",
                                         "room": r.get("room_id"), "seconds": 90}))
            base = _public_base()
            if base or not CAPTURE_ONLY:
                acts.append({"tag": "button", "type": "default",
                             "text": {"tag": "plain_text", "content": "回听"},
                             "url": "%s/reader?session=%s" % (
                                 base or lan.base_url(), r["session_id"])})
        elements.append({"tag": "action", "actions": acts})
    if not elements:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "现在一路都没在听。"}})
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
                     "按钮没反应就发 /状态、/近聊 CH02、/总结 CH02"}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "turquoise",
                       "title": {"tag": "plain_text",
                                 "content": "🎛 直播助手 · %d 路" % len(rows)}},
            "elements": elements}


def _status_card(data: dict) -> dict:
    """状态卡：控制台卡片去掉“全部房间”语义，只强调当前几路在听。"""
    card = _console_card(data)
    live = sum(1 for r in data.get("rooms") or []
               if r.get("room_state") == "live")
    card["header"]["template"] = "blue"
    card["header"]["title"]["content"] = "🎧 直播助手 · %d 路在听" % live
    return card


def _console_text(data: dict) -> str:
    rows = data.get("rooms") or []
    if not rows:
        return "现在一路都没在听。"
    lines = ["🎛 直播助手 · %d 路" % len(rows)]
    for r in rows:
        line = "%s %s  %s" % (r.get("ch") or "CH—", r.get("name") or "",
                              _status_icon(r.get("room_state")))
        if r.get("elapsed") is not None:
            if r.get("capture_only"):
                line += " · " + _capture_bits(r)
            else:
                line += " · 已录 %s · %s 句" % (_hms(r.get("elapsed")),
                                               int(r.get("n_utterances") or 0))
        if r.get("stalled"):
            line += " · ⚠️ 断流"
        lines.append(line)
    lines.append("单路：/状态 CH02、/近聊 CH02、/总结 CH02")
    return "\n".join(lines)


def _sessions_map() -> dict[str, dict]:
    try:
        return {str(s.get("id") or ""): s
                for s in (_api("/api/live/list").get("sessions") or [])}
    except Exception:
        return {}


def _room_state_label(room: dict) -> str:
    state = str(room.get("state") or "")
    if state == "live":
        return "🟢 直播中"
    if state in ("pending", "starting"):
        return "⏳ 启动中"
    if state == "failed":
        return "⚠️ 失败"
    if state == "stopped":
        return "⏸ 已停止"
    return _status_icon(state) + " " + str(_STATE.get(state, ("", state))[1])


def _room_session_bits(room: dict, sessions: dict[str, dict]) -> list[str]:
    s = sessions.get(str(room.get("session_id") or ""))
    if not s:
        return []
    bits: list[str] = []
    if s.get("elapsed") is not None:
        if s.get("capture_only"):
            bits.append(_capture_bits(s))
        else:
            bits.append("已录 %s · %s 句" % (_hms(s.get("elapsed")),
                                            int(s.get("n_utterances") or 0)))
    if s.get("capture_only") and not int(s.get("n_utterances") or 0):
        return bits
    last = str(s.get("last_line") or "")
    if not last:
        utts = s.get("utterances") or []
        if utts:
            last = str((utts[-1] or {}).get("text") or "")
    if last:
        bits.append("最后一句 %s" % _md_safe(last[:36]))
    return bits


def _room_picker_card(kind: str, rooms: list[dict], minutes: int,
                      sessions: dict[str, dict], need_at: bool = False) -> dict:
    elements: list[dict] = []
    for i, r in enumerate(rooms, 1):
        lines = ["**%s %s**  %s" % (_ch(r), _md_safe(_room_name(r)),
                                   _room_state_label(r))]
        lines.extend(_room_session_bits(r, sessions))
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                "content": "\n".join(lines)}})
        rid = r.get("id") or ""
        if kind == "summary":
            btn = _btn("生成这路总结", {
                "act": "brief_summary", "room": rid, "confirm": 1}, i == 1)
        else:
            btn = _btn("看这路", {
                "act": "brief_topics", "room": rid,
                "minutes": int(minutes or 10)}, i == 1)
        elements.append({"tag": "action", "actions": [btn]})
    if kind == "summary":
        title = "📝 选一路生成阶段性总结"
        note = ("预计 1 次模型调用、15~30 秒；按钮没反应就回 1 或 %s；"
                "发 /总结 %s 会再确认一次" % (_ch(rooms[0]), _ch(rooms[0])))
    else:
        title = "📻 最近 %d 分钟看哪一路" % int(minutes or 10)
        note = ("按钮没反应就回 1 或 %s；也可以发 /近聊 %s %d分钟"
                % (_ch(rooms[0]), _ch(rooms[0]), int(minutes or 10)))
    if need_at:
        note += "；群里回编号要先 @我"
    elements.append({"tag": "note", "elements": [{"tag": "plain_text",
                                                  "content": note}]})
    return {"config": {"wide_screen_mode": True},
            "header": {"template": "green" if kind == "summary" else "blue",
                       "title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _room_choice_text(kind: str, rooms: list[dict], minutes: int,
                      sessions: dict[str, dict], need_at: bool = False) -> str:
    if kind == "summary":
        lines = ["有 %d 路可用，回复编号继续生成总结：" % len(rooms)]
        tail = "5 分钟内有效；也可以发 /总结 %s（会再确认一次）" % _ch(rooms[0])
    else:
        lines = ["有 %d 路可用，回复编号看最近 %d 分钟：" % (
            len(rooms), int(minutes or 10))]
        tail = "5 分钟内有效；也可以发 /近聊 %s %d分钟" % (
            _ch(rooms[0]), int(minutes or 10))
    for i, r in enumerate(rooms, 1):
        bits = [_room_state_label(r)]
        bits.extend(_room_session_bits(r, sessions))
        lines.append("%d. %s %s（%s）" % (
            i, _ch(r), _room_name(r), " · ".join(bits)))
    lines.append(tail + ("；群里回编号要先 @我" if need_at else ""))
    return "\n".join(lines)


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}
_PENDING_CHOICE: dict[str, dict] = {}
_LAST_CHOICE_ROOM: dict[str, str] = {}


def _clear_room_choice(open_id: str) -> None:
    with _BRIEF_LOCK:
        _PENDING_CHOICE.pop(open_id or "", None)


def _prefer_room(rooms: list[dict], open_id: str) -> list[dict]:
    """上次选过的那一路排第一：按钮更顺手，编号仍以 CHxx 为准。"""
    rid = _LAST_CHOICE_ROOM.get(open_id or "")
    if not rid:
        return list(rooms)
    for i, r in enumerate(rooms):
        if str(r.get("id") or "") == rid:
            return [r] + rooms[:i] + rooms[i + 1:]
    return list(rooms)


def _normalize_choice(text: str) -> str:
    """把「1 / 第1路 / 第一路 / CH01 / １」统一成待选表里的键。"""
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    s = re.sub(r"\s+", "", s).strip("，。！!?？")
    m = re.fullmatch(r"(?:选|第)?([一二三四五六七八九])(?:路|个|号)?", s)
    if m:
        return str(_CN_NUM[m.group(1)])
    m = re.fullmatch(r"(?:选|第)?0*(\d{1,3})(?:路|个|号)?", s)
    if m:
        return str(int(m.group(1)))
    m = re.fullmatch(r"CH0*(\d{1,3})", s, re.I)
    if m:
        return "CH%02d" % int(m.group(1))
    return s.upper()


def _looks_like_choice(text: str) -> bool:
    s = unicodedata.normalize("NFKC", str(text or "")).strip()
    return bool(re.fullmatch(
        r"(?:选|第)?(?:[一二三四五六七八九]|\d{1,3})(?:路|个|号)?|CH\s*0*\d{1,3}",
        s, re.I))


def _take_room_choice(t: str, open_id: str, chat_id: str = "",
                      chat_type: str = "p2p") -> str | None:
    """文字兜底：回复编号就接着做，不需要再发一遍完整命令。

    没在等选择时返回 None，让调用方继续走普通路由；选项不对时保留待选，
    用户可以马上再回一个编号。
    """
    key = open_id or ""
    with _BRIEF_LOCK:
        p = _PENDING_CHOICE.get(key)
        expired = bool(p and float(p.get("expires") or 0) < time.time())
        if expired:
            _PENDING_CHOICE.pop(key, None)
    if expired:
        if _looks_like_choice(t):
            return "上次的选择已过期（5 分钟）。发 /总结 或 /近聊 重新选。"
        return None
    if not p:
        return None
    rid = str((p.get("tokens") or {}).get(_normalize_choice(t)) or "")
    if not rid:
        if _looks_like_choice(t):
            return ("没有「%s」这个选项。回复 1~%d，或 CH01 这样的编号。"
                    % (str(t).strip(), len(p.get("rids") or [])))
        return None
    with _BRIEF_LOCK:
        if _PENDING_CHOICE.get(key) is not p:
            # 另一条消息/另一个线程已经消费了这次选择；别生成两遍。
            return ""
        _PENDING_CHOICE.pop(key, None)
    _LAST_CHOICE_ROOM[key] = rid
    room = _room_by_id(rid)
    if not room:
        return "这一路已经不在了。发 /状态 看现场。"
    target = _ch(room) if _ch(room) != "CH—" else rid
    if p.get("kind") == "summary":
        return cmd_summary(target, open_id, chat_id, chat_type,
                           skip_confirm=bool(p.get("skip_confirm", True)))
    return cmd_topics("%s %d分钟" % (target, int(p.get("minutes") or 10)),
                      open_id, chat_id, chat_type)


def _ask_room(kind: str, rooms: list[dict], minutes: int, open_id: str,
              chat_id: str = "", chat_type: str = "p2p") -> None:
    """多路时不猜：发一张选择卡，同时留下 5 分钟的文字编号兜底。

    点到/回复某一路就是明确选择，生成总结时不再二次确认。
    """
    rooms = _prefer_room(list(rooms), open_id)
    sessions = _sessions_map()
    tokens: dict[str, str] = {}
    for i, r in enumerate(rooms, 1):
        rid = str(r.get("id") or "")
        if not rid:
            continue
        tokens[str(i)] = rid
        tokens["第%d路" % i] = rid
        cn = next((k for k, v in _CN_NUM.items() if v == i), "")
        if cn:
            tokens["第%s路" % cn] = rid
        ch = _ch(r)
        if ch != "CH—":
            tokens[ch] = rid
    now = time.time()
    with _BRIEF_LOCK:
        for k in [k for k, v in _PENDING_CHOICE.items()
                  if float(v.get("expires") or 0) < now]:
            _PENDING_CHOICE.pop(k, None)
        _PENDING_CHOICE[open_id] = {
            "kind": kind, "minutes": int(minutes or 10), "tokens": tokens,
            "rids": [str(r.get("id") or "") for r in rooms],
            "skip_confirm": True, "at": now,
            "expires": now + 300,
        }
    text = _room_choice_text(kind, rooms, minutes, sessions,
                             need_at=chat_type == "group")
    card = _room_picker_card(kind, rooms, minutes, sessions,
                             need_at=chat_type == "group")
    _send_card_target(card, text, open_id, chat_id, chat_type)


# ---------------------------------------------------------------- 确认码 / 上一条

_BRIEF_PENDING: dict[str, dict] = {}
_BRIEF_LOCK = threading.Lock()
_LAST_BRIEF: dict[str, dict] = {}


def _new_nonce(room_id: str, open_id: str, kind: str = "summary") -> str:
    nonce = secrets.token_hex(3).upper()
    with _BRIEF_LOCK:
        now = time.time()
        for k in [k for k, v in _BRIEF_PENDING.items()
                  if now - float(v.get("at") or 0) > 300]:
            _BRIEF_PENDING.pop(k, None)
        _BRIEF_PENDING[nonce] = {"room_id": room_id, "open_id": open_id,
                                 "kind": kind, "at": now}
    return nonce


def _take_nonce(nonce: str, open_id: str) -> dict | None:
    key = str(nonce or "").upper()
    with _BRIEF_LOCK:
        p = _BRIEF_PENDING.get(key)
        if not p:
            return None
        if time.time() - float(p.get("at") or 0) > 300:
            _BRIEF_PENDING.pop(key, None)
            return None
        # 别人的确认码不能用，而且不能因为别人试了一次就把主人的码吃掉。
        if open_id and p.get("open_id") and open_id != p["open_id"]:
            return None
        _BRIEF_PENDING.pop(key, None)
    return p


# ---------------------------------------------------------------- 生成任务

def _topics_worker(room: dict, minutes: int, open_id: str, chat_id: str = "",
                   chat_type: str = "p2p", refresh: bool = False,
                   ack: bool = True) -> None:
    if ack:
        _send_text_target("⏳ 正在整理 %s 最近 %d 分钟…" % (_ch(room), minutes),
                          open_id, chat_id, chat_type)
    try:
        data = _brief_api("topics", room.get("id") or "", minutes=minutes,
                          refresh=1 if refresh else 0, timeout=60)
    except Exception as e:
        _send_text_target("简报没生成成功：%s" % e, open_id, chat_id, chat_type)
        return
    if not data.get("ok"):
        _send_text_target(_brief_error(data), open_id, chat_id, chat_type)
        return
    _attach_minutes(data, room)
    _LAST_BRIEF[open_id] = {"kind": "topics", "room_id": room.get("id") or "",
                            "minutes": minutes, "at": time.time()}
    _send_card_target(_topics_card(data), _topics_text(data),
                      open_id, chat_id, chat_type)


def _attach_minutes(data: dict, room: dict) -> None:
    """把已回填的妙记链接塞进简报数据，卡片/纯文本都能用同一套渲染。"""
    sid = str((data.get("room") or {}).get("session_id")
              or room.get("session_id") or "")
    if not sid:
        return
    try:
        conn = store.connect()
        try:
            data["minutes_url"] = (store.get_session(conn, sid) or {}).get(
                "minutes_url") or ""
        finally:
            conn.close()
    except Exception:
        traceback.print_exc()


def _finish_summary(data: dict, room: dict, open_id: str, chat_id: str,
                    chat_type: str) -> None:
    _attach_minutes(data, room)
    _LAST_BRIEF[open_id] = {"kind": "summary", "room_id": room.get("id") or "",
                            "minutes": 0, "at": time.time()}
    _send_card_target(_summary_card(data), _summary_text(data),
                      open_id, chat_id, chat_type)


def _summary_worker(room: dict, open_id: str, chat_id: str = "",
                    chat_type: str = "p2p", refresh: bool = False,
                    confirmed: bool = False, ack: bool = True,
                    skip_confirm: bool = False) -> None:
    rid = room.get("id") or ""
    if not confirmed and not refresh:
        try:
            dry = _brief_api("summary", rid, dry=1, timeout=10)
        except Exception as e:
            _send_text_target("简报没生成成功：%s" % e, open_id, chat_id, chat_type)
            return
        if not dry.get("ok"):
            _send_text_target(_brief_error(dry), open_id, chat_id, chat_type)
            return
        if dry.get("cached"):
            _finish_summary(dry, room, open_id, chat_id, chat_type)
            return
        if dry.get("would_use_llm") and not skip_confirm:
            nonce = _new_nonce(rid, open_id)
            _send_card_target(_brief_confirm_card(room, nonce, int(dry.get("rows") or 0)),
                              _brief_confirm_text(room, nonce, int(dry.get("rows") or 0)),
                              open_id, chat_id, chat_type)
            return
    if ack:
        _send_text_target("⏳ 正在生成 %s 的阶段性总结…" % _ch(room),
                          open_id, chat_id, chat_type)
    try:
        data = _brief_api("summary", rid, refresh=1 if (refresh or confirmed) else 0,
                          timeout=90)
    except Exception as e:
        _send_text_target("总结没生成成功：%s" % e, open_id, chat_id, chat_type)
        return
    if not data.get("ok"):
        _send_text_target(_brief_error(data), open_id, chat_id, chat_type)
        return
    _finish_summary(data, room, open_id, chat_id, chat_type)


# ---------------------------------------------------------------- 命令

def parse_topics_args(rest: str) -> tuple[str | None, int]:
    target, minutes = None, 10
    for tok in (rest or "").split():
        m = re.fullmatch(r"(\d{1,3})(?:分钟|m)", tok, re.I)
        if m:
            minutes = max(1, min(120, int(m.group(1))))
        elif re.fullmatch(r"(?:CH)?\d+", tok, re.I):
            target = tok
        else:
            raise ValueError("用法：/近聊 [CH] [30分钟]")
    return target, minutes


def cmd_topics(rest: str, open_id: str, chat_id: str = "",
               chat_type: str = "p2p", refresh: bool = False) -> str:
    try:
        target, minutes = parse_topics_args(rest)
    except ValueError as e:
        return str(e)
    _clear_room_choice(open_id)
    room, candidates, err = _pick_room_choice(target)
    if err:
        return err
    if candidates:
        _ask_room("topics", candidates, minutes, open_id, chat_id, chat_type)
        return ""
    if not room.get("session_id"):
        return "这一路还没有会话。发 /状态 看现场。"
    _LAST_CHOICE_ROOM[open_id or ""] = str(room.get("id") or "")
    _spawn(_topics_worker, open_id, room, minutes, open_id, chat_id, chat_type, refresh, True)
    return ""


def cmd_summary(rest: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p", refresh: bool = False,
                skip_confirm: bool = False) -> str:
    target = (rest or "").strip() or None
    _clear_room_choice(open_id)
    room, candidates, err = _pick_room_choice(target)
    if err:
        return err
    if candidates:
        _ask_room("summary", candidates, 0, open_id, chat_id, chat_type)
        return ""
    if not room.get("session_id"):
        return "这一路还没有会话。发 /状态 看现场。"
    _LAST_CHOICE_ROOM[open_id or ""] = str(room.get("id") or "")
    _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type,
           refresh, False, True, skip_confirm=skip_confirm)
    return ""


def cmd_confirm(nonce: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> str:
    p = _take_nonce(nonce, open_id)
    if not p:
        return "确认码过期或已经用过了。发 /总结 重新来一次。"
    room = _room_by_id(p.get("room_id") or "")
    if not room:
        return "这个房间已经不在了，发 /rooms 看看现在有哪些。"
    _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type, True, True, True)
    return ""


def cmd_refresh(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    last = _LAST_BRIEF.get(open_id)
    if not last or time.time() - float(last.get("at") or 0) > 1800:
        return "还没有可刷新的简报。先发 /近聊 或 /总结。"
    if last.get("kind") == "status":
        return cmd_status(last.get("target") or "")
    room = _room_by_id(last.get("room_id") or "")
    if not room:
        return "上一条的房间已经不在了，发 /rooms 看看现在有哪些。"
    if last.get("kind") == "topics":
        _spawn(_topics_worker, open_id, room, int(last.get("minutes") or 10),
               open_id, chat_id, chat_type, True, True)
    else:
        _spawn(_summary_worker, open_id, room, open_id, chat_id, chat_type, True, True, True)
    return ""


def cmd_console(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    try:
        data = _brief_api("status", timeout=10)
    except Exception as e:
        return "状态没取到：%s" % e
    if not data.get("ok"):
        return _brief_error(data)
    if not (data.get("rooms") or []):
        return "现在一路都没在听。"
    _send_card_target(_status_card(data), _console_text(data),
                      open_id, chat_id, chat_type)
    return ""


def _sub_key(open_id: str, chat_id: str = "", chat_type: str = "p2p") -> str:
    dest = chat_id if (chat_type == "group" and chat_id) else ""
    return "%s|%s" % (open_id, dest)


def cmd_subscribe(arg: str, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> str:
    m = re.search(r"\d+", arg or "")
    minutes = max(5, min(60, int(m.group()))) if m else 10
    dest = chat_id if (chat_type == "group" and chat_id) else ""
    _SUBS[_sub_key(open_id, chat_id, chat_type)] = {
        "minutes": minutes, "next": time.monotonic() + minutes * 60,
        "open_id": open_id, "chat_id": dest,
        "chat_type": "group" if dest else "p2p"}
    return ("已订阅：每 %d 分钟把在听房间的近聊摘要发到%s。\n退出：/退订"
            % (minutes, "这个群" if dest else "私聊"))


def cmd_unsubscribe(open_id: str) -> str:
    keys = [k for k in _SUBS if k.split("|", 1)[0] == open_id]
    for k in keys:
        _SUBS.pop(k, None)
    return "已退订。" if keys else "本来就没有订阅。"


_SUBS: dict[str, dict] = {}


def _subscription_loop() -> None:
    while True:
        time.sleep(15)
        now = time.monotonic()
        due = [sub for sub in list(_SUBS.values())
               if now >= float(sub.get("next") or 0)]
        for sub in due:
            sub["next"] = now + int(sub.get("minutes") or 10) * 60
            try:
                rooms = [r for r in _ordered(_rooms())
                         if r.get("state") in _ACTIVE and r.get("session_id")]
            except Exception:
                continue
            for room in rooms[:MAX_ROOMS]:
                try:
                    _topics_worker(room, int(sub.get("minutes") or 10),
                                   str(sub.get("open_id") or ""),
                                   str(sub.get("chat_id") or ""),
                                   str(sub.get("chat_type") or "p2p"),
                                   False, False)
                except Exception:
                    traceback.print_exc()


def handle_menu(key: str, open_id: str) -> None:
    key = str(key or "").lower()
    if key in ("status", "状态"):
        reply = cmd_status()
    elif key in ("rooms", "房间"):
        reply = cmd_rooms()
    elif key in ("topics", "近聊", "最近10分钟"):
        reply = cmd_topics("", open_id)
    elif key in ("summary", "总结"):
        reply = cmd_summary("", open_id, skip_confirm=True)
    elif key in ("console", "控制台"):
        reply = cmd_console(open_id)
    else:
        reply = _help_text()
    if reply:
        _send_text_target(reply, open_id)


def _brief_action(act: str, value: dict, open_id: str, chat_id: str = "",
                  chat_type: str = "p2p") -> None:
    room = _room_by_id(value.get("room") or "")
    if act == "brief_status":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _LAST_BRIEF[open_id] = {"kind": "status",
                                "target": room.get("id") or "",
                                "room_id": room.get("id") or "",
                                "minutes": 0, "at": time.time()}
        _send_text_target(cmd_status(room.get("id") or ""),
                          open_id, chat_id, chat_type)
        return
    if act == "brief_topics":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _clear_room_choice(open_id)
        _LAST_CHOICE_ROOM[open_id] = str(room.get("id") or "")
        try:
            minutes = int(value.get("minutes") or 10)
        except (TypeError, ValueError):
            minutes = 10
        _topics_worker(room, minutes, open_id, chat_id, chat_type)
        return
    if act == "brief_summary":
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _clear_room_choice(open_id)
        _LAST_CHOICE_ROOM[open_id] = str(room.get("id") or "")
        # 选择卡/控制台按钮本身就是明确选择：不再弹二次确认。
        _summary_worker(room, open_id, chat_id, chat_type,
                        skip_confirm=bool(value.get("confirm")))
        return
    if act == "brief_refresh":
        last = _LAST_BRIEF.get(open_id)
        if not room or not last:
            _send_text_target("还没有可刷新的简报。先发 /近聊 或 /总结。",
                              open_id, chat_id, chat_type)
            return
        kind = value.get("kind") or last.get("kind")
        if kind == "topics":
            _topics_worker(room, int(value.get("minutes") or last.get("minutes") or 10),
                           open_id, chat_id, chat_type, True, True)
        else:
            _summary_worker(room, open_id, chat_id, chat_type,
                            refresh=True, confirmed=True)
        return
    if act == "brief_confirm":
        p = _take_nonce(value.get("nonce") or "", open_id)
        if not p:
            _send_text_target("确认码过期或已经用过了。发 /总结 重新来一次。",
                              open_id, chat_id, chat_type)
            return
        room = _room_by_id(p.get("room_id") or "")
        if not room:
            _send_text_target("这个房间已经不在了，发 /rooms 看看。",
                              open_id, chat_id, chat_type)
            return
        _summary_worker(room, open_id, chat_id, chat_type,
                        refresh=True, confirmed=True)


def cmd_retry(arg: str) -> str:
    """把一路重新排队：下播后再开播、或采集失败时，手机上唯一的恢复入口。"""
    rooms = _rooms()
    if not rooms:
        return "现在没有房间。贴一个直播间链接给我。"
    arg = (arg or "").strip()
    if arg:
        room = _resolve_room(rooms, arg)
        if not room:
            return "没找到「%s」。\n%s" % (arg, cmd_rooms())
        picked = [room]
    else:
        picked = [r for r in _ordered(rooms)
                  if r.get("state") in ("stopped", "failed")]
        if not picked:
            return "现在没有停着的房间。\n" + cmd_rooms()
        if len(picked) > 1:
            return "停着 %d 路，用 /重试 CHxx 指定一路：\n%s" % (
                len(picked), cmd_rooms())
    room = picked[0]
    if room.get("state") in ("live", "starting", "pending"):
        return "%s 已经在跑（%s），不用重试。" % (_ch(room), _label(room))
    _api("/api/rooms/state", {"id": room["id"], "state": "pending", "error": ""})
    print("[重试] %s %s" % (room["id"], room.get("name") or ""), flush=True)
    return ("已重新排队：%s %s\n采集代理会在几秒内重新取流；"
            "好了我会发消息。" % (_ch(room), _label(room)))


def cmd_stop(arg: str) -> str:
    rooms = _rooms()
    if not rooms:
        return "现在没有在听的房间。"
    ordered = _ordered(rooms)
    arg = (arg or "").strip()
    if not arg:
        return "停哪一路？\n" + cmd_rooms()
    room = None
    if arg.isdigit():
        # 裸数字保留旧语义：按当前排序数第几路
        i = int(arg)
        if 1 <= i <= len(ordered):
            room = ordered[i - 1]
    else:
        # CH02 / 房间 id / URL
        room = _resolve_room(rooms, arg, allow_plain_seq=False)
    if not room:
        return "没找到「%s」。\n%s" % (arg, cmd_rooms())
    _api("/api/rooms/close", {"id": room["id"]})
    return "已停：%s %s\n对应的浏览器也关了。" % (_ch(room), _label(room))


# ---------------------------------------------------------------- 发布到飞书

def _room_for_sid(sid: str) -> dict | None:
    for r in _rooms():
        if str(r.get("session_id") or "") == str(sid or ""):
            return r
    return None


def _publish_worker(sid: str, title: str, open_id: str, chat_id: str = "",
                    chat_type: str = "p2p", force: bool = True) -> None:
    """发布放到后台：正文/表格/授权都是网络调用，别卡住长连接。"""
    try:
        data = _api("/api/publish", {"session_id": sid, "force": bool(force)},
                    timeout=120)
    except Exception as e:
        _send_text_target("发布没成功：%s" % _short_err(str(e)),
                          open_id, chat_id, chat_type)
        return
    if data.get("skipped"):
        _send_text_target(
            "发布器没开。让部署的人打开 FEISHU_PUBLISH_ENABLED=1 再发一次；"
            "本地录音和总结不受影响。", open_id, chat_id, chat_type)
        return
    doc = {"title": data.get("title") or title,
           "state": data.get("doc_state") or "",
           "overview": data.get("overview") or "",
           "counts": data.get("counts") or {}}
    card = feishu_publish.wrapup_card(data, doc)
    text = feishu_publish.wrapup_text(data, doc)
    _send_card_target(card, text, open_id, chat_id, chat_type)


def cmd_publish(rest: str, open_id: str = "", chat_id: str = "",
                chat_type: str = "p2p") -> str:
    room, cands, err = _pick_room_choice((rest or "").strip())
    if not room:
        if cands:
            return _room_candidates_text(cands, "发：/发布 %s")
        return err or "现在没有可发布的场次。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "%s 还没有会话 id，等采集端把这场建起来再发。" % _ch(room)
    threading.Thread(target=_publish_worker,
                     args=(sid, _room_name(room), open_id, chat_id,
                           chat_type, True), daemon=True).start()
    return "正在发布 %s《%s》…好了会发一张卡片。" % (_ch(room), _room_name(room))


_DOC_STATE_CN = {"ready": "已总结", "pending_transcript": "待补转写",
                 "pending_summary": "待总结", "failed_summary": "总结失败"}
_PUB_STATE_CN = {"ok": "已发布", "partial": "部分成功", "failed": "发布失败"}


def _doc_text(data: dict, room: dict | None) -> str:
    label = _ch(room) if room else ""
    name = _room_name(room) if room else ""
    title = str(data.get("title") or name or "这场")
    # 兼容只带台账行的老响应：没有 published 时从 publish 行推出同样字段。
    row = data.get("publish") or {}
    if "published" not in data and row:
        meta = row.get("meta") or {}
        data = dict(data)
        data["published"] = bool(row.get("doc_url") or row.get("doc_token"))
        data.setdefault("state", row.get("state") or "")
        data.setdefault("doc_url", row.get("doc_url") or "")
        data.setdefault("base_url", meta.get("base_url")
                        or row.get("base_url") or "")
        data.setdefault("doc_state", row.get("status")
                        or meta.get("doc_state") or "")
        data.setdefault("error", row.get("error") or "")
    if not data.get("published"):
        if data.get("enabled") is False:
            return ("《%s》还没有发布文档。\n"
                    "发布器没开：在 livenotes.env 设 FEISHU_PUBLISH_ENABLED=1 "
                    "后重启。\n本地录音和总结不受影响。"
                    % title)
        return ("《%s》还没发布过。\n"
                "发 /发布%s 生成云文档和场次表。"
                % (title, (" " + label) if label else ""))
    lines = ["📄《%s》%s" % (
        title, _PUB_STATE_CN.get(str(data.get("state") or ""), "已发布"))]
    doc_state = str(data.get("doc_state") or "")
    if doc_state:
        lines.append("内容：" + _DOC_STATE_CN.get(doc_state, doc_state))
    counts = data.get("counts") or {}
    if counts:
        lines.append("观点 %d · 高光 %d · 上麦 %d 人" % (
            int(counts.get("points") or 0), int(counts.get("highlights") or 0),
            int(counts.get("speakers") or 0)))
    if data.get("doc_url"):
        lines.append("文档：" + str(data["doc_url"]))
    if data.get("base_url"):
        lines.append("场次表：" + str(data["base_url"]))
    if data.get("error"):
        lines.append("注意：" + str(data["error"]))
    if doc_state == "pending_transcript":
        lines.append("补转写出字后会自动更新；现在别当总结看。")
    elif doc_state in ("pending_summary", "failed_summary"):
        lines.append("逐字稿有了、总结还没成功；总结出来后会自动更新。")
    lines.append("补名：/补名 %s（或点发布卡片上的「补名」）" % (label or "CHxx"))
    return "\n".join(lines)


def _multi_doc_text(cands: list[dict]) -> str:
    """未指定房间且有多路时，直接列出各路文档与共享场次表链接，免得用户再敲一遍。"""
    lines = ["最近场次的文档（要看某场详情可发 /文档 CHxx）："]
    base_url = ""
    for r in cands[:5]:
        label = _ch(r)
        title = _room_name(r)
        sid = str(r.get("session_id") or "")
        if not sid:
            lines.append("· %s %s（尚未建档）" % (label, title))
            continue
        try:
            data = _api("/api/publish?" + urllib.parse.urlencode({"session_id": sid}))
        except Exception:
            lines.append("· %s %s（状态读取失败）" % (label, title))
            continue
        row = data.get("publish") or {}
        meta = row.get("meta") or {}
        doc_url = str(data.get("doc_url") or row.get("doc_url") or "")
        doc_state = str(data.get("doc_state") or row.get("status")
                        or meta.get("doc_state") or "")
        if not base_url:
            base_url = str(data.get("base_url") or meta.get("base_url")
                           or row.get("base_url") or "")
        state_cn = _DOC_STATE_CN.get(doc_state, doc_state) if doc_state else "未发布"
        lines.append("· %s %s（%s）" % (label, title, state_cn))
        if doc_url:
            lines.append("  文档：" + doc_url)
    if base_url:
        lines.append("\n场次表（全部场次与观点）：" + base_url)
    return "\n".join(lines)


def cmd_doc(rest: str, open_id: str = "", chat_id: str = "",
            chat_type: str = "p2p") -> str:
    arg = (rest or "").strip()
    room, cands, err = _pick_room_choice(arg)
    if not room:
        if cands:
            if not arg:
                return _multi_doc_text(cands)
            return _room_candidates_text(cands, "发：/文档 %s")
        return err or "现在没有可查的场次。"
    sid = str(room.get("session_id") or "")
    if not sid:
        return "%s 还没有会话 id。" % _ch(room)
    try:
        data = _api("/api/publish?" + urllib.parse.urlencode({"session_id": sid}))
    except Exception as e:
        return "读发布状态失败：%s" % _short_err(str(e))
    return _doc_text(data, room)


def cmd_trust() -> str:
    added = watchlink.trust_list()
    lines = ["已知平台（默认放行）：" + "、".join(watchlink.PRESET_SITES)]
    if added:
        lines.append("你确认过的：")
        for site, meta in added.items():
            lines.append("· %s（%s）" % (site, (meta or {}).get("added_at", "")[:10]))
    else:
        lines.append("还没确认过任何陌生站点。")
    pend = watchlink.pending_count()
    if pend:
        lines.append("有 %d 个待确认（%s内有效）。" % (pend, watchlink.ttl_text()))
    return "\n".join(lines)


_YES = re.compile(r"^(确认|同意|允许|可以|ok|okay|yes|y)\s*([A-Za-z0-9]{4})?$", re.I)
_NO = re.compile(r"^(取消|不用了|算了|no|n)\s*([A-Za-z0-9]{4})?$", re.I)


def _answer_pending(text: str) -> str | None:
    """「确认 / 确认 K7Q2 / 取消」——卡片按钮失效时的兜底通道。"""
    m = _YES.match(text) or _NO.match(text)
    if not m:
        return None
    want_ok = bool(_YES.match(text))
    code = (m.group(2) or "").upper()
    p = watchlink.pending_get(code) if code else watchlink.pending_unique()
    if not p:
        n = watchlink.pending_count()
        if n > 1:
            codes = watchlink.pending_codes()
            return "有 %d 个待确认，回的时候带上确认码，比如：确认 %s" % (n, codes[0])
        return "现在没有等你确认的东西。直接把直播间链接粘给我就行。"
    if not want_ok:
        watchlink.pending_take(p["code"])
        return "好，已取消。%s 不会进白名单。" % p["site"]
    watchlink.pending_take(p["code"])
    watchlink.allow(p["site"], note="飞书确认")
    return "✅ 已允许 %s，以后这个站的链接直接开听。\n%s" % (
        p["site"], _start_room(p["url"], label=str(p.get("label") or "")))


def cmd_watch(text: str, open_id: str = "", chat_id: str = "",
              chat_type: str = "p2p") -> str:
    _clear_room_choice(open_id)
    try:
        return _cmd_watch(text, open_id, chat_id, chat_type)
    except RuntimeError as e:
        return ("本机服务没起来，接不上：%s\n先 python run.py，起来之后把链接再发一次。" % e)


def _cmd_watch(text: str, open_id: str = "", chat_id: str = "",
               chat_type: str = "p2p") -> str:
    d = watchlink.decide(text, resolver=watchlink.resolve)
    note = (d.get("note") + "\n") if d.get("note") else ""
    if d["kind"] == "reject":
        return note + d["reason"]
    if d["kind"] == "confirm":
        p = watchlink.pending_new(d["url"], d["site"], open_id,
                                  label=d.get("label") or "")
        fallback = ("%s 这个站我还不认识，要你点一下确认。\n"
                    "回复「确认 %s」就开（%s内有效）。"
                    % (note + d["url"], p["code"], watchlink.ttl_text()))
        # 卡片发不出去（版本/权限）时退回文字。群聊里也回到群里，
        # 否则用户那边一片安静；私聊保持原来的“返回文字由调用方发”。
        if chat_type == "group" and chat_id:
            _send_card_target(_confirm_card(p), fallback, open_id, chat_id, chat_type)
            return ""
        if _BOT.send_card(_confirm_card(p), open_id):
            return ""
        return fallback
    return note + _start_room(d["url"], d.get("resolved_from") or "",
                              label=d.get("label") or "")


_BOT: feishu.Bot  # 在 main() 里赋值，handle_text 用它发消息


def handle_text(text: str, open_id: str, chat_id: str = "",
                chat_type: str = "p2p") -> None:
    print("[收到] %s | %s" % ((open_id or "")[:12], text.replace("\n", " ")[:120]), flush=True)
    t = (text or "").strip()
    low = t.lower()

    try:
        reply = _route(t, low, open_id, chat_id, chat_type)
    except Exception as e:
        traceback.print_exc()
        reply = "出错了：%s" % e
    if not reply:
        return
    ok = _send_text_target(reply, open_id, chat_id, chat_type)
    print("[回复] %s  %s" % ("成功" if ok else "失败", reply.splitlines()[0]), flush=True)


_HINT_DAY: dict[str, str] = {}


def _help_text() -> str:
    return ("贴一个直播间链接给我就开始听（抖音/快手/B 站都行）。\n"
            "第一次遇到陌生网站我会弹卡片让你确认，点一次就记住。\n\n"
            "/状态 [CH] 在听哪几路、有没有断流\n"
            "/近聊 [CH] [10分钟] 最近在聊什么（默认 10 分钟）\n"
            "/总结 [CH] 开播至今的阶段性总结（先确认再生成）\n"
            "/分享 [CH] [90秒] 把最近一段原声发到飞书（群聊需确认）\n"
            "/分享 CH01 1:30 2分钟 从第 1:30 起截 2 分钟\n"
            "/发布 [CH] 把这场发成云文档 + 场次表一行\n"
            "/文档 [CH] 看发布到哪了、文档链接和状态\n"
            "/周报 [上周|7天|11-03~11-09] 汇总多场生成周报（私聊）\n"
            "/复盘 [范围] 生成可填结论/行动的复盘模板（私聊）\n"
            "/刷新 重做上一条简报（绕过缓存）\n"
            "/控制台 一张卡片看所有房间和按钮\n"
            "/订阅 [分钟] /退订 定时把近聊摘要发给你\n"
            "/rooms 房间列表（带 CH 编号）\n"
            "/重试 [CH] 重新拉起一路（下播后再开播用）\n"
            "/stop 1 或 /stop CH02 停掉一路\n"
            "/白名单 看过哪些站点被放行\n"
            "关注 FDE、OPC 给关键词加权\n"
            "/话题 OPC政策 搜历史场次里的原话\n"
            "/找人 阿哲 看这个人的观点档案\n"
            "/搜 OPC政策 找结论和原话（带时间码与文档链接）\n"
            "/问 阿哲怎么看 OPC？ 用笔记回答问题并给引用\n"
            "/补名 [CH] 给上麦的人补名字（试听 + 表单卡）\n\n"
            "群聊里需要 @我，而且只认白名单群；私聊只有主人能用。")


def _daily_hint(open_id: str) -> str:
    """未识别的文本默认不回。每人每天最多提示一次帮助，避免聊天被刷屏。"""
    key = open_id or "-"
    today = date.today().isoformat()
    if _HINT_DAY.get(key) == today:
        return ""
    _HINT_DAY[key] = today
    return "没看懂这条，我只认命令和直播间链接。\n" + _help_text()


def _room_arg(text: str) -> str:
    m = re.search(r"(?i)CH\s*0*(\d{1,3})", text or "")
    return ("CH%02d" % int(m.group(1))) if m else ""


def _nl_route(t: str, open_id: str, chat_id: str, chat_type: str) -> str:
    """P2 自然语言实验：只在白名单群、且 @ 了机器人时到这里。

    仍然只认几个明确意图；认不出来就保持沉默，不在群里接闲聊。
    """
    if not (NL_ENABLED and chat_type == "group" and chat_id):
        return ""
    room = _room_arg(t)
    if any(w in t for w in ("总结", "复盘", "概括", "梳理", "讲了什么", "说了什么")):
        return cmd_summary(room, open_id, chat_id, chat_type)
    if any(w in t for w in ("近聊", "话题", "聊什么", "在聊", "最近")):
        m = re.search(r"(\d{1,3})\s*分钟", t)
        rest = room
        if m:
            rest = ("%s %s分钟" % (room, m.group(1))).strip()
        return cmd_topics(rest, open_id, chat_id, chat_type)
    if any(w in t for w in ("状态", "断流", "还在听", "正常吗", "怎么样")):
        return cmd_status(room)
    return ""


def _route(t: str, low: str, open_id: str = "", chat_id: str = "",
           chat_type: str = "p2p") -> str:
    # 顺序要紧：先认「确认/取消」，再认链接，最后才是关键词命令——
    # 分享文案里常带"关注"两个字，先判关注会把链接吃掉。
    got = _answer_pending(t)
    if got is not None:
        return got

    for verb in ("/watch", "watch", "/听"):
        if low.startswith(verb):
            rest = t[len(verb):].strip(" ：:")
            if not rest:
                return ("用法：把直播间链接直接发给我，或者 /watch <链接>。\n"
                        "抖音、快手、B 站的直播间都行。")
            return cmd_watch(rest, open_id, chat_id, chat_type)

    if watchlink.extract(t):
        return cmd_watch(t, open_id, chat_id, chat_type)

    parts = t.split(None, 1)
    head = parts[0].lower() if parts else ""

    for verb in ("/近聊", "/最新", "/topics"):
        if low.startswith(verb):
            return cmd_topics(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/确认分享", "/share_confirm"):
        if low.startswith(verb):
            return cmd_share_confirm(t[len(verb):].strip(" ：:"), open_id,
                                     chat_id, chat_type)
    for verb in ("/分享包", "/package"):
        if low.startswith(verb):
            return cmd_package(t[len(verb):].strip(" ：:"), open_id,
                               chat_id, chat_type)
    for verb in ("/妙记", "/minutes"):
        if low.startswith(verb):
            return cmd_minutes(t[len(verb):].strip(" ：:"), open_id,
                               chat_id, chat_type)
    for verb in ("/周报", "/weekly"):
        if low.startswith(verb):
            return cmd_weekly(t[len(verb):].strip(" ：:"), open_id,
                              chat_id, chat_type)
    for verb in ("/复盘", "/retro"):
        if low.startswith(verb):
            return cmd_retro(t[len(verb):].strip(" ：:"), open_id,
                             chat_id, chat_type)
    for verb in ("/回听", "/replay"):
        if low.startswith(verb):
            return cmd_replay(t[len(verb):].strip(" ：:"), open_id,
                              chat_id, chat_type)
    for verb in ("/分享", "/share"):
        if low.startswith(verb):
            return cmd_share(t[len(verb):].strip(" ：:"), open_id,
                             chat_id, chat_type)
    for verb in ("/发布", "/publish"):
        if low.startswith(verb):
            return cmd_publish(t[len(verb):].strip(" ：:"), open_id,
                               chat_id, chat_type)
    for verb in ("/文档", "/doc"):
        if low.startswith(verb):
            return cmd_doc(t[len(verb):].strip(" ：:"), open_id,
                           chat_id, chat_type)
    for verb in ("/补名", "/命名", "/name"):
        if low.startswith(verb):
            return cmd_name(t[len(verb):].strip(" ：:"), open_id,
                            chat_id, chat_type)
    for verb in ("/总结", "/summary"):
        if low.startswith(verb):
            return cmd_summary(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/刷新", "/refresh"):
        if low.startswith(verb):
            return cmd_refresh(open_id, chat_id, chat_type)
    for verb in ("/确认简报", "/确认总结"):
        if low.startswith(verb):
            return cmd_confirm(t[len(verb):].strip(" ：:"), open_id, chat_id, chat_type)
    for verb in ("/控制台", "/console", "/菜单"):
        if low.startswith(verb):
            return cmd_console(open_id, chat_id, chat_type)
    # 订阅会持续发消息，不能因为聊天里出现“订阅”两个字就触发。
    if (t.startswith("/订阅") or t == "订阅"
            or re.match(r"^订阅\s*\d", t)):
        return cmd_subscribe(t.split("订阅", 1)[-1].strip(" ：:"), open_id,
                             chat_id, chat_type)
    if t.startswith("/退订") or t == "退订":
        return cmd_unsubscribe(open_id)

    for verb in ("/状态", "/status"):
        if low.startswith(verb):
            target = t[len(verb):].strip(" ：:")
            if open_id:
                _LAST_BRIEF[open_id] = {"kind": "status", "target": target,
                                        "room_id": "", "minutes": 0,
                                        "at": time.time()}
            return cmd_status(target)

    if t.startswith(("/rooms", "/房间", "/列表", "房间列表")):
        return cmd_rooms()
    for verb in ("/重试", "/retry"):
        if low.startswith(verb):
            return cmd_retry(t[len(verb):].strip(" ：:"))
    for verb in ("/stop", "/停", "停掉"):
        if t.startswith(verb):
            return cmd_stop(t[len(verb):].strip(" ：:"))
    if t.startswith(("/白名单", "/信任", "白名单")):
        return cmd_trust()

    if t.startswith(("/关注列表", "关注列表")):
        ws = profile.keywords()
        return ("当前关注词：" + "、".join(ws)) if ws else "还没有关注词。发送：关注 FDE、OPC"
    if t.startswith(("/取消关注", "取消关注")):
        rest = t.split("关注", 1)[-1].strip(" ：:")
        ws = profile.remove(rest)
        return "已移除。" + (("现在：" + "、".join(ws)) if ws else "现在没有关注词了。")
    if t.startswith(("/关注", "关注")):
        rest = t.split("关注", 1)[-1].strip(" ：:")
        if not rest:
            return "用法：关注 FDE、OPC"
        ws = profile.add(rest)
        return "已关注：" + "、".join(ws) + "\n命中这些词的句子会加权，更容易被推。"
    if t.startswith(("/话题", "话题")):
        return cmd_topic(t.split("话题", 1)[-1].strip(" ：:"))

    for verb in ("/找人", "/find"):
        if low.startswith(verb):
            return cmd_find_person(t[len(verb):].strip(" ：:"), open_id,
                                   chat_id, chat_type)
    for verb in ("/搜", "/搜索", "/search"):
        if low.startswith(verb):
            return cmd_search(t[len(verb):].strip(" ：:"), open_id,
                              chat_id, chat_type)
    for verb in ("/问", "/ask"):
        if low.startswith(verb):
            return cmd_ask(t[len(verb):].strip(" ：:"), open_id,
                           chat_id, chat_type)

    if head in ("/hi", "/help", "/帮助") or t in ("你好", "在吗", "哈喽", "帮助"):
        return _help_text()
    # 选择卡的文字兜底：只在确实有 pending 时生效，不影响普通聊天。
    choice = _take_room_choice(t, open_id, chat_id, chat_type)
    if choice is not None:
        return choice
    # 补名卡的文字兜底：「1 阿哲 2 小北」。只有刚发过卡才认，平时不拦截聊天。
    pending, names, _err = _naming_from_text(t, open_id, chat_id)
    if pending is not None and names:
        threading.Thread(target=_naming_apply,
                         args=(pending, names, open_id, chat_id, chat_type),
                         daemon=True).start()
        return ""
    if chat_type == "group" and chat_id:
        return _nl_route(t, open_id, chat_id, chat_type)
    return _daily_hint(open_id)


# ---------------------------------------------------------------- 状态回执

def _notify(text: str) -> None:
    try:
        ok = _BOT.send_text(text)
        print("[通知] %s  %s" % ("成功" if ok else "失败", text.splitlines()[0]), flush=True)
    except Exception:
        traceback.print_exc()


def _watch_rooms() -> None:
    """开好了 / 没打开，都要主动说一声。

    房间里从 starting 变 live 靠的是采集代理回报，用户看不到中间过程；
    没有这条回执，他只能猜"到底听上了没有"。
    """
    seen: dict[str, str] = {}
    interrupted_seen: set[str] = set()
    primed = False
    while True:
        try:
            rooms = _rooms()
        except Exception:
            time.sleep(10)
            continue
        for r in rooms:
            rid, st = str(r.get("id")), str(r.get("state"))
            # 服务重启把在录的房间收口成「已中断」：不主动说一声，用户会以为
            # 还在录。重试后 interrupted 清掉，下次再中断还能再报。
            if r.get("interrupted"):
                if rid not in interrupted_seen:
                    interrupted_seen.add(rid)
                    _notify("⚠️ 采集中断（服务重启）：%s\n发 /重试 %s 继续。"
                            % (r.get("name") or "这个房间", _ch(r)))
            else:
                interrupted_seen.discard(rid)
            prev = seen.get(rid)
            seen[rid] = st
            if not primed or prev == st:
                continue
            if st == "live" and prev != "live":
                _notify("✅ 开好了：%s\n%s\n%s"
                        % (r.get("name") or "这个房间", r.get("url") or "",
                           _live_hint(r)))
            elif st == "failed" and prev != "failed":
                _notify("⚠️ 没打开：%s\n原因：%s\n可能已下播，或链接不是直播间。"
                        "发 /重试 %s 再拉一次。"
                        % (r.get("url") or "", r.get("error") or "采集端没给原因",
                           _ch(r)))
            elif (st == "stopped" and prev in ("live", "starting")
                  and r.get("auto_stopped")):
                # 自动收尾（断流看门狗 / 采集代理判定已下播）：人已经睡了，
                # 得说一声，不然早上只看到三个「已停止」，不知道是录完了还是断了一夜。
                _notify("⚠️ 已自动收尾：%s\n原因：%s\n录音已保存；%s"
                        % (r.get("name") or "这个房间",
                           r.get("auto_stop_reason") or r.get("error") or "没收到音频块",
                           _play_hint(r)))
        primed = True
        time.sleep(4)


def main(argv: list[str]) -> int:
    global _BOT
    try:
        _BOT = feishu.Bot()
    except Exception as e:
        print("启动失败：%s" % e)
        return 2
    print("[feishu] app_id=%s… open_id=%s" % (
        _BOT.app_id[:10], ("已记录" if _BOT.open_id else "还没有（先在飞书里发一句话）")),
        flush=True)

    for i, a in enumerate(argv):
        if a == "--say" and i + 1 < len(argv):
            print("[测试] 发送：%s" % argv[i + 1])
            print("[测试] %s" % ("成功" if _BOT.send_text(argv[i + 1]) else "失败"))
            return 0

    threading.Thread(target=_watch_rooms, daemon=True).start()
    threading.Thread(target=_subscription_loop, daemon=True).start()
    try:
        feishu.run(_BOT, handle_text, handle_card, handle_menu)
    except KeyboardInterrupt:
        print("\n[exit] 已停止")
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
