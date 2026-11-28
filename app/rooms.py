# -*- coding: utf-8 -*-
"""监听房间：存 URL、状态、会话映射。

用户只粘贴链接，房间名由采集端从页面里读出来回填 —— 不让人抄标题。

状态机：pending -> starting -> live，任何一步失败落到 failed 并记原因。
启动阶段不再靠前端猜：采集代理在拉起浏览器 / 打开页面 / 打开采集页时上报
stage，采集页在取音频、建会话、首块入库时继续上报；失败时带 failed_stage。
stage 取值 1..6，对应前端六步（1 浏览器、2 页面、3 音频、4 会话、5 入库、6 完成）。

采集浏览器的存活不写进 rooms.json：代理每轮 tick 往 /api/capture/status
发心跳，服务只在内存里留最近一次，避免每 3 秒重写一次房间文件。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from typing import Any

from . import liveprobe
from .config import DATA_DIR
from .traffic import sanitize_traffic

PATH = DATA_DIR / "rooms.json"
_LOCK = threading.Lock()

# 采集代理心跳：room_id -> 最近一次上报（只在内存里，服务重启后自然清空）
_CAP_LOCK = threading.Lock()
_CAPTURE: dict[str, dict] = {}

# 这些参数只跟分享渠道/播放器有关，不影响房间本身；不剔掉会导致同一个房间被当成两个
_TRACKING = re.compile(
    r"^(utm_|spm|from|enter_|share_|is_|did_|timestamp|sec_uid|u_code|"
    r"web_id|previous_page|enter_from|camera_id|camera_type|match_id)", re.I)

# 直取旧版本把 reflow 里的 room id 当成 web_rid，拼出 live.douyin.com/<room id>。
# 抖音对那个地址只回「服务器开小差了」——所以它绝不能拿来当页面地址。
_REFLOW_ID = re.compile(r"/reflow/(\d{6,})")
_LIVE_PAGE = re.compile(r"^https?://live\.douyin\.com/(\d{6,})/?(?:[?#].*)?$", re.I)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_url(url: str) -> str:
    """规范化：补协议、去追踪参数、去尾斜杠、host 小写。"""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    try:
        p = urllib.parse.urlsplit(u)
    except ValueError:
        return u
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not _TRACKING.match(k)]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                                    urllib.parse.urlencode(q), ""))


def _read() -> list[dict]:
    if not PATH.is_file():
        return []
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return data.get("rooms") or []


def _write(rooms: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps({"rooms": rooms}, ensure_ascii=False, indent=2), encoding="utf-8")


def _heal_fake_live_url(room: dict) -> bool:
    """清掉旧直取拼出来的假 live 地址（room id 当 web_rid 的那个）。

    不清的话「显示页面」会一直打开一个没有房间的地址，抖音只回
    「服务器开小差了」；清掉后前端退回打开 reflow 页，采集端下一次上报
    会写回真实的 live.douyin.com/<web_rid>。
    """
    live = _LIVE_PAGE.match(str(room.get("resolved_url") or "").strip())
    reflow = _REFLOW_ID.search(str(room.get("url") or ""))
    if not (live and reflow) or live.group(1) != reflow.group(1):
        return False
    room["resolved_url"] = ""
    room["updated_at"] = now()
    return True


def _heal_rooms(rooms: list[dict]) -> bool:
    changed = False
    for r in rooms:
        changed = _heal_fake_live_url(r) or changed
    return changed


def list_rooms() -> list[dict]:
    with _LOCK:
        rooms = _read()
        if _heal_rooms(rooms):
            _write(rooms)
        return rooms


def get(room_id: str) -> dict | None:
    for r in list_rooms():
        if r.get("id") == room_id:
            return r
    return None


def get_by_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    for r in list_rooms():
        if r.get("session_id") == session_id:
            return r
    return None


def _identities(room: dict) -> set[str]:
    """这个房间认得出的一切身份：房号页、入口链接、分享原文、历史别名。"""
    urls = [room.get("url"), room.get("resolved_url"), room.get("resolved_from")]
    urls += list(room.get("alt_urls") or [])
    return {i for i in (liveprobe.room_identity(u) for u in urls) if i}


def _identity_of(room: dict) -> str:
    ids = _identities(room)
    return sorted(ids)[0] if ids else ""


def add(url: str, resolved_from: str = "", name: str = "") -> tuple[dict, bool]:
    """返回 (房间, 是否新建)。同一个直播间重复提交不会产生第二路。

    "同一个直播间"按房号判：抖音认 live.douyin.com/<web_rid>，别的平台认
    room_key。只看链接文本不行——同一间语音厅换标题重开，分享出来的 reflow
    id 就换了，只看链接会把同一间攒成 4 条（飞书房间列表就是这么膨胀的）。

    resolved_from 是分享/短链原文：同一个分享文案再发一次时，落地页地址
    可能被规范化成不同形式，用它兜住重复。

    name 是从分享文案里认出来的主播名：先垫上，采集端落地后会换成
    「主播名 · 房间标题」的真名（name_source=share -> page）。
    """
    norm = normalize_url(url)
    if not norm:
        raise ValueError("链接是空的")
    src = normalize_url(resolved_from) if resolved_from else ""
    hint = str(name or "").strip()[:60]
    with _LOCK:
        rooms = _read()
        healed = _heal_rooms(rooms)
        ident = liveprobe.room_identity(norm, src)
        for r in rooms:
            # 同一个房间可能一次是短链/reflow、一次是真实房号；都算命中。
            if not (norm in (r.get("url"), r.get("resolved_url"))
                    or (src and src == r.get("resolved_from"))
                    or (ident and ident in _identities(r))):
                continue
            # 老把 reflow 当身份的记录：这次拿到了房号（或反过来），
            # 把新链接记成别名；否则下次换一种分享它还是认不出来。
            extra = norm not in (r.get("url"), r.get("resolved_url"),
                                 r.get("resolved_from"))
            changed = False
            if hint and not str(r.get("name") or "").strip():
                # 老房间还没有名字时，顺手用这次分享文案里的主播名补上，
                # 免得重发一次链接反而又退回显示域名。
                r.update(name=hint, name_source="share")
                changed = True
            if extra:
                r.setdefault("alt_urls", []).append(norm)
                r["alt_urls"] = r["alt_urls"][-8:]
                changed = True
            if changed or healed:
                r["updated_at"] = now()
                _write(rooms)
            return r, False
        # seq 是稳定的展示编号（CH01…），加/删别的房间不会让它变。
        # 老记录没有 seq，前端会退回按当前顺序编号。
        seq = max([int(r.get("seq") or 0) for r in rooms] + [0]) + 1
        # 同一秒内连加几路、或删了又加时，HHMMSS + len%10 会撞 id。
        # 房间 id 同时是 Chrome profile 名，撞了就是两路共用一个浏览器。
        stamp = datetime.now().strftime("%H%M%S")
        used = {str(r.get("id") or "") for r in rooms}
        rid = "rm" + stamp + str(len(rooms) % 10)
        if rid in used:
            rid = "rm" + stamp + uuid.uuid4().hex[:4]
            while rid in used:
                rid = "rm" + stamp + uuid.uuid4().hex[:4]
        room = {
            "id": rid,
            "url": norm,
            # 采集浏览器落地后的真实房号（live.douyin.com/<web_rid>）。
            # 短链/reflow 只是入口，用它去重、显示和重试都更可靠。
            "resolved_url": "",
            # 用户贴进来的分享/短链原文，重复发送时用来去重
            "resolved_from": src,
            # 同一间认过的其它链接（换标题重开后的新 reflow、稳定下来的
            # 房号页）。只用于判重，不参与采集；最多留 8 条。
            "alt_urls": [],
            # 展示名。空着的话前端只能退回显示域名（webcast.amemv.com），
            # 于是「哪一路是哪个直播间」就没人说得清。
            "name": hint,
            # 名字是哪来的：share（分享文案垫的）/ page（采集端从页面读到的
            # 真名）/ user（人工改的，采集端不许覆盖）。空 = 还没有名字。
            "name_source": "share" if hint else "",
            "state": "pending",
            "error": "",
            "session_id": "",
            # 最近一次采集引擎：direct / shared / per_room。空 = 还没上报。
            "capture_engine": "",
            # 直取失败后不再自动切浏览器：这里亮出「可以切」，等收听页确认后
            # switch_requested 才置位，采集代理下一轮 tick 才会真的拉起浏览器。
            "switch_ready": False,
            "switch_engine": "",
            "switch_reason": "",
            "switch_requested": False,
            "created_at": now(),
            "updated_at": now(),
            "seq": seq,
            "stage": 0,
            "failed_stage": None,
            "stage_note": "",
            "stage_at": now(),
            "saved": False,
            "has_recording": False,
            # 服务重启时还没正常结束、被收口成 stopped 的房间。前端据此
            # 显示「已中断」而不是普通的「已停止」；重试会清掉。
            "interrupted": False,
        }
        rooms.append(room)
        _write(rooms)
        return room, True


def update(room_id: str, **fields: Any) -> dict | None:
    """按字段更新房间。

    显式传 None 会把字段清成 null（例如重试时清掉 failed_stage）；不传的
    字段保持原值。调用方应只传本次真的收到的字段。
    """
    with _LOCK:
        rooms = _read()
        for r in rooms:
            if r.get("id") == room_id:
                r.update(fields)
                if set(fields) & {"state", "stage", "failed_stage", "stage_note"}:
                    r["stage_at"] = now()
                r["updated_at"] = now()
                _write(rooms)
                return r
    return None


def remove(room_id: str) -> bool:
    with _LOCK:
        rooms = _read()
        left = [r for r in rooms if r.get("id") != room_id]
        if len(left) == len(rooms):
            return False
        _write(left)
    with _CAP_LOCK:
        _CAPTURE.pop(room_id, None)
    return True


def _merged_name(keep: dict, drop: dict) -> tuple[str, str]:
    """留下哪一路时用哪个名字：人工改过的赢，其次是页面读到的真名。"""
    rank = {"user": 3, "page": 2, "share": 1, "": 0}
    if rank.get(str(keep.get("name_source") or ""), 0) >= \
            rank.get(str(drop.get("name_source") or ""), 0):
        return str(keep.get("name") or ""), str(keep.get("name_source") or "")
    return str(drop.get("name") or ""), str(drop.get("name_source") or "")


def absorb_resolved(room_id: str, resolved: str) -> dict | None:
    """采集端报来真实房号后，把同一间的其它记录并掉。

    两条分享链接落成同一个房号页时，只有 ``resolved_url`` 写进来这一刻
    才知道它们是一间；判重发生在加房间时（那时还只有 reflow id），
    必须在这里补一次。在录的那条留下，别的并成别名，回放不受影响。
    """
    rid = str(room_id or "")
    target = str(resolved or "").strip()
    ident = liveprobe.room_identity(target)
    if not rid or not ident:
        return None
    with _LOCK:
        rooms = _read()
        me = next((r for r in rooms if r.get("id") == rid), None)
        if me is None:
            return None
        if target and target not in (me.get("url"), me.get("resolved_url"),
                                     me.get("resolved_from")) \
                and target not in (me.get("alt_urls") or []):
            me.setdefault("alt_urls", []).append(target)
            me["alt_urls"] = me["alt_urls"][-8:]
        same = [i for i, r in enumerate(rooms) if ident in _identities(r)]
        if not same:
            same = [rooms.index(me)]
        keep_i = min(same, key=lambda i: _keep_key(rooms[i]))
        keep = rooms[keep_i]
        if keep.get("id") != rid:
            # 另一条在录：给它留下这一路的新房号，回复里的记录照样用它
            keep.setdefault("alt_urls", []).append(target)
            keep["alt_urls"] = keep["alt_urls"][-8:]
        dead: set[int] = set()
        running = [i for i, r in enumerate(rooms)
                   if ident in _identities(r)
                   and r.get("state") in ("live", "starting")]
        merged: list[str] = []
        if len(running) <= 1:
            for i, r in enumerate(rooms):
                if i == keep_i or ident not in _identities(r):
                    continue
                dead.add(i)
                merged.append(str(r.get("id") or ""))
                for u in [r.get("url"), r.get("resolved_url"),
                          r.get("resolved_from")] + list(r.get("alt_urls") or []):
                    u = str(u or "").strip()
                    if u and u not in [keep.get("url"), keep.get("resolved_url"),
                                       keep.get("resolved_from")] + \
                            list(keep.get("alt_urls") or []):
                        keep.setdefault("alt_urls", []).append(u)
                if r.get("session_id"):
                    keep.setdefault("merged_sessions", [])
                    if r["session_id"] not in keep["merged_sessions"]:
                        keep["merged_sessions"].append(r["session_id"])
        keep["alt_urls"] = list(keep.get("alt_urls") or [])[-8:]
        keep["updated_at"] = now()
        _write([r for i, r in enumerate(rooms) if i not in dead])
        out = dict(keep)
    if merged:
        with _CAP_LOCK:
            for x in merged:
                _CAPTURE.pop(x, None)
        print("[房间] %s 与 %s 是同一间，已合并" % (rid, ",".join(merged)),
              flush=True)
    return out


_STATE_RANK = {"live": 4, "starting": 3, "pending": 2, "failed": 1, "stopped": 0}


def _keep_key(r: dict) -> tuple:
    """一组重复里留哪条：在录的 > 启动中 > 待启动 > 失败/停止。

    同档时留创建早的那条：它是在听的、有 CH 编号和会话的那一路。
    绝不能反过来——把 live 那条并掉，采集代理下一拍就回收浏览器，
    正在录的一场会断在这里。所以排在最前面的是「状态最高 + 创建最早」。
    """
    return (0 - _STATE_RANK.get(str(r.get("state") or ""), 0),
            str(r.get("created_at") or "9999"), str(r.get("id") or ""))


def merge_duplicates() -> dict:
    """把同一间直播间的多条房间并成一条。返回给人看的合并结果。

    产生原因：分享文案每次换个标题重开都是新的 reflow id，而老版本判重
    只看链接，于是同一间语音厅在飞书列表里越攒越多。这里按 ``room_identity``
    归组，每组留一条：
      - 在录的 > 启动中 > 待启动 > 失败/停止；同档留创建早的那条；
      - 名字取更像真名的那个（user > page > share），别名并进 alt_urls；
      - 被并掉的那几条的会话记进 ``merged_sessions``，回放还能找到。

    有两条以上还在 live/starting 的组**整个跳过**：那是两路都在录，
    并掉哪条都会让正在录的一场断掉。这种情况只能人看着删（/删除），
    删除会先正常停会话。
    """
    with _LOCK:
        rooms = _read()
        groups: dict[str, list[int]] = {}
        for i, r in enumerate(rooms):
            ident = _identity_of(r)
            if ident:
                groups.setdefault(ident, []).append(i)
        dup = {k: v for k, v in groups.items() if len(v) > 1}
        if not dup:
            return {"ok": True, "groups": [], "removed": 0, "conflicts": []}
        dead: set[int] = set()
        report: list[dict] = []
        conflicts: list[dict] = []
        for ident, idxs in dup.items():
            running = [i for i in idxs
                       if rooms[i].get("state") in ("live", "starting")]
            if len(running) > 1:
                conflicts.append({"identity": ident,
                                  "rooms": [str(rooms[i].get("id") or "")
                                            for i in running]})
                continue
            keep_i = min(idxs, key=lambda i: _keep_key(rooms[i]))
            keep = rooms[keep_i]
            dropped_ids = []
            others = [rooms[i] for i in idxs if i != keep_i]
            # 名字先整体挑一次：在录的那条不一定名字最全（老记录名还是从
            # 分享文案垫的短名），一条条按优先级比会把 user 名冲掉。
            for drop in others:
                nm, ns = _merged_name(keep, drop)
                if nm:
                    keep["name"], keep["name_source"] = nm, ns
            for i in idxs:
                if i == keep_i:
                    continue
                drop = rooms[i]
                dead.add(i)
                dropped_ids.append(str(drop.get("id") or ""))
                for u in [drop.get("url"), drop.get("resolved_url"),
                          drop.get("resolved_from")] + list(drop.get("alt_urls") or []):
                    u = str(u or "").strip()
                    if u and u not in (keep.get("url"), keep.get("resolved_url"),
                                       keep.get("resolved_from")) \
                            and u not in (keep.get("alt_urls") or []):
                        keep.setdefault("alt_urls", []).append(u)
                if drop.get("session_id"):
                    keep.setdefault("merged_sessions", [])
                    if drop["session_id"] not in keep["merged_sessions"]:
                        keep["merged_sessions"].append(drop["session_id"])
            keep["alt_urls"] = list(keep.get("alt_urls") or [])[-8:]
            keep["updated_at"] = now()
            report.append({"identity": ident, "kept": keep.get("id"),
                           "kept_ch": keep.get("seq"), "dropped": dropped_ids,
                           "name": keep.get("name") or ""})
        if not dead:
            return {"ok": True, "groups": [], "removed": 0, "conflicts": conflicts}
        rooms = [r for i, r in enumerate(rooms) if i not in dead]
        _write(rooms)
    with _CAP_LOCK:
        for item in report:
            for rid in item["dropped"]:
                _CAPTURE.pop(rid, None)
    return {"ok": True, "groups": report,
            "removed": sum(len(g["dropped"]) for g in report),
            "conflicts": conflicts}


def find_duplicates() -> list[dict]:
    """只看不改：同一间直播间出现在几条记录里。给飞书列表现场提示。"""
    rooms = _read()
    groups: dict[str, list[dict]] = {}
    for r in rooms:
        ident = _identity_of(r)
        if ident:
            groups.setdefault(ident, []).append(r)
    out = []
    for ident, rows in groups.items():
        if len(rows) > 1:
            out.append({"identity": ident,
                        "rooms": [{"id": r.get("id"), "seq": r.get("seq"),
                                   "name": r.get("name") or "",
                                   "state": r.get("state") or ""}
                                  for r in rows]})
    return out


# ---------------------------------------------------------------- 采集端心跳

def capture_update(room_id: str, alive: bool, pid: int | None = None,
                   adopted: bool = False, reason: str = "",
                   traffic: dict | None = None) -> dict:
    """记录采集代理的一次心跳。age_s 由读取时现算，避免时钟漂移。"""
    rid = str(room_id or "")
    if not rid:
        raise ValueError("缺少房间 id")
    try:
        pid_i = int(pid) if pid else None
    except (TypeError, ValueError):
        pid_i = None
    entry = {
        "alive": bool(alive),
        "pid": pid_i,
        "adopted": bool(adopted),
        "reason": str(reason or "")[:200],
        "at": now(),
        "_mono": time.monotonic(),
    }
    clean = sanitize_traffic(traffic)
    if clean is not None:
        entry["traffic"] = clean
    with _CAP_LOCK:
        _CAPTURE[rid] = entry
        if len(_CAPTURE) > 200:
            oldest = min(_CAPTURE, key=lambda k: _CAPTURE[k]["_mono"])
            _CAPTURE.pop(oldest, None)
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def capture_status() -> dict[str, dict]:
    with _CAP_LOCK:
        out: dict[str, dict] = {}
        now_mono = time.monotonic()
        for rid, e in _CAPTURE.items():
            item = {k: v for k, v in e.items() if not k.startswith("_")}
            item["age_s"] = round(max(0.0, now_mono - e["_mono"]), 1)
            out[rid] = item
        return out


def traffic_report() -> dict[str, Any]:
    """按房间汇总最近一次流量快照，给 /api/traffic 和排障页用。"""
    status = capture_status()
    rows: list[dict[str, Any]] = []
    totals = {
        "rooms": 0,
        "platform_bytes": 0,
        "audio_bytes": 0,
        "platform_mb_per_hour": 0.0,
        "audio_mb_per_hour": 0.0,
    }
    for rid, entry in status.items():
        traffic = entry.get("traffic")
        if not isinstance(traffic, dict):
            continue
        row = {"id": rid, "alive": bool(entry.get("alive")),
               "age_s": entry.get("age_s")}
        row.update(traffic)
        rows.append(row)
        totals["rooms"] += 1
        totals["platform_bytes"] += int(traffic.get("platform_bytes") or 0)
        totals["audio_bytes"] += int(traffic.get("audio_bytes") or 0)
        totals["platform_mb_per_hour"] += float(
            traffic.get("platform_mb_per_hour") or 0)
        totals["audio_mb_per_hour"] += float(
            traffic.get("audio_mb_per_hour") or 0)
    totals["platform_mb_per_hour"] = round(totals["platform_mb_per_hour"], 1)
    totals["audio_mb_per_hour"] = round(totals["audio_mb_per_hour"], 1)
    return {"generated_at": now(), "rooms": rows, "totals": totals}
