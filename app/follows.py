# -*- coding: utf-8 -*-
"""关注：状态检测、开播提醒、自动记录。

产品边界（docs/36 §3）：

- 关注对象是直播间（platform + room_key），不是人；
- 检测只读、保守：只有页面明确说「直播已结束」才判未开播，其余失败一律
  保持原状态；连续 3 次失败才显示「检测失败」；
- 同一场只提醒一次（episode 去重），自动记录每个关注独立开关、默认关；
- 自动记录一次只启动一路，容量满/启动失败都要给人话通知，不静默。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

from . import liveprobe, logs, notify, rooms, settings, store, watchlink

_LOCK = threading.RLock()
_WORKER: threading.Thread | None = None
_STOP = threading.Event()
#: follow_id -> {room_id, since, retry_at, tried}；等采集代理真的把房间拉起来
_PENDING_AUTO: dict[int, dict] = {}
_PROBE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="follow-probe")


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def interval_s() -> int:
    """检测间隔：设置项 60–300；低配模式不低于 180（docs/36 §9.2）。"""
    try:
        base = int(settings.prefs().get("follow_interval_s") or 120)
    except (TypeError, ValueError):
        base = 120
    try:
        from . import config
        if config.perf().get("mode") == "lite":
            base = max(180, base)
    except Exception:
        pass
    return max(60, min(300, base))


# ---------------------------------------------------------------- 读写

def _view(row: dict, active_rooms: list[dict]) -> dict:
    key = str(row.get("room_key") or "")
    rec = next((r for r in active_rooms
                if r.get("state") in ("pending", "starting", "live")
                and liveprobe.room_key_of(str(r.get("resolved_url")
                                              or r.get("url") or "")) == key), None)
    return {
        "id": int(row["id"]), "platform": row.get("platform") or "",
        "room_key": key, "url": row.get("url") or "",
        "name": row.get("name") or "", "live_title": row.get("live_title") or "",
        "live_state": row.get("live_state") or "unknown",
        "live_started_at": row.get("live_started_at") or "",
        "live_episode": row.get("live_episode") or "",
        "last_live_at": row.get("last_live_at") or "",
        "last_checked_at": row.get("last_checked_at") or "",
        "last_error": row.get("last_error") or "",
        "last_session_id": row.get("last_session_id") or "",
        "fail_count": int(row.get("fail_count") or 0),
        "notify": bool(row.get("notify")), "auto_record": bool(row.get("auto_record")),
        "recording": bool(rec), "room_id": str((rec or {}).get("id") or ""),
        "session_id": str((rec or {}).get("session_id")
                          or row.get("last_session_id") or ""),
    }


def list_view() -> dict:
    conn = store.connect()
    try:
        rows = store.list_follows(conn)
    finally:
        conn.close()
    active = rooms.list_rooms()
    items = [_view(r, active) for r in rows]
    return {
        "items": items,
        "stats": {"total": len(items),
                  "live": sum(1 for i in items if i["live_state"] == "live"),
                  "recording": sum(1 for i in items if i["recording"]),
                  "offline": sum(1 for i in items if i["live_state"] == "offline"),
                  "unknown": sum(1 for i in items if i["live_state"] == "unknown")},
        "interval_s": interval_s(),
        "max_rooms": watchlink.MAX_ROOMS,
        "auto_supported": True,
    }


def get(fid: int) -> dict | None:
    conn = store.connect()
    try:
        row = store.get_follow(conn, fid)
    finally:
        conn.close()
    return _view(row, rooms.list_rooms()) if row else None


def _platform_and_key(url: str, platform: str = "") -> tuple[str, str, str]:
    """链接 -> (platform, room_key, 可打开的规范链接)。"""
    url = str(url or "").strip()
    platform = (platform or liveprobe.platform_of(url)).lower()
    key = liveprobe.room_key_of(url, platform)
    canonical = url
    if platform == "douyin":
        try:
            canonical = liveprobe.canonical_douyin_url(url, timeout=8.0) or url
        except Exception:
            canonical = url
        key = liveprobe.room_key_of(canonical, platform) or key
    return platform, key, canonical


def add(url: str, platform: str = "", name: str = "") -> dict:
    """新增关注。重复关注返回已有项，不报错。"""
    platform, key, canonical = _platform_and_key(url, platform)
    if not platform:
        raise ValueError("这个平台还不能自动检测，暂时只能手动开始记录")
    if not key:
        raise ValueError("链接里没有房间号，换一个直播间链接试试")
    conn = store.connect()
    try:
        if store.count_follows(conn) >= 50:
            raise ValueError("最多关注 50 个直播间，先移除几个吧")
        if not name:
            try:
                probe = liveprobe.probe(canonical, platform, timeout=6.0)
                name = probe.get("name") or ""
            except Exception:
                name = ""
        row = store.add_follow(conn, platform, key, canonical, name)
    finally:
        conn.close()
    if not row:
        raise ValueError("这个链接没能识别出直播间")
    return _view(row, rooms.list_rooms())


def update(fid: int, **fields: Any) -> dict | None:
    allowed = {k: fields[k] for k in ("name", "notify", "auto_record")
               if k in fields}
    conn = store.connect()
    try:
        row = store.update_follow(conn, fid, **allowed)
    finally:
        conn.close()
    return _view(row, rooms.list_rooms()) if row else None


def remove(fid: int) -> bool:
    conn = store.connect()
    try:
        return bool(store.remove_follow(conn, fid))
    finally:
        conn.close()


def precheck(url: str) -> dict:
    """添加前预检：平台、房号、名称、是否已关注。检测失败也返回 200。"""
    platform, key, canonical = _platform_and_key(url)
    conn = store.connect()
    try:
        followed = False
        if platform and key:
            followed = conn.execute(
                "SELECT 1 FROM follows WHERE platform=? AND room_key=?",
                (platform, key)).fetchone() is not None
    finally:
        conn.close()
    out = {"ok": bool(platform and key), "platform": platform,
           "room_key": key, "url": canonical, "followed": followed,
           "name": "", "live_state": "unknown", "error": ""}
    if not platform:
        out["error"] = "这个平台还不能自动检测"
        return out
    probe = liveprobe.probe(canonical, platform, timeout=6.0)
    out.update({"name": probe.get("name") or "",
                "live_state": probe.get("state") or "unknown",
                "live_started_at": probe.get("live_started_at") or "",
                "error": probe.get("error") or ""})
    return out


# ---------------------------------------------------------------- 状态机

def _set(conn, fid: int, **fields: Any) -> dict | None:
    return store.update_follow(conn, fid, **fields)


def _fail(conn, row: dict, probe: dict) -> dict:
    fails = int(row.get("fail_count") or 0) + 1
    state = str(row.get("live_state") or "unknown")
    fields: dict[str, Any] = {"fail_count": fails, "last_checked_at": now(),
                              "last_error": str(probe.get("error") or "")[:200]}
    # live 连续 3 个周期没有成功检测 -> unknown，避免永远显示「直播中」。
    # 保留 live_episode：短暂失败后恢复不应重复提醒。
    if fails >= 3:
        state = "unknown"
    fields["live_state"] = state
    return _set(conn, int(row["id"]), **fields) or row


def _room_already_recording(active: list[dict], room_key: str) -> bool:
    for r in active:
        if r.get("state") not in ("pending", "starting", "live"):
            continue
        if liveprobe.room_key_of(str(r.get("resolved_url")
                                     or r.get("url") or "")) == room_key:
            return True
    return False


def _auto_capacity_left(active: list[dict]) -> bool:
    n = sum(1 for r in active if r.get("state") in ("pending", "starting", "live"))
    return n < watchlink.MAX_ROOMS


def _start_auto_record(row: dict, active: list[dict]) -> dict:
    """把一个关注的开播变成一路采集房间。返回 {ok, room_id, error}。

    不直接调 ``live.start``：浏览器采集必须由采集代理拉起页面、拿到音频后
    再建会话。这里只负责把房间放进 pending，代理下一拍就会接管。
    """
    with _LOCK:
        if _PENDING_AUTO:
            return {"ok": False, "room_id": "", "reason": "busy",
                    "error": "另一个关注正在自动记录"}
        if _room_already_recording(active, str(row["room_key"])):
            return {"ok": False, "room_id": "", "reason": "recording",
                    "error": "这个直播间已经在记录"}
        if not _auto_capacity_left(active):
            return {"ok": False, "room_id": "", "reason": "capacity",
                    "error": "同时最多记录 %d 路，先停一路再试" % watchlink.MAX_ROOMS}
        try:
            room, _created = rooms.add(str(row["url"]), name=str(row.get("name") or ""))
        except Exception as e:
            return {"ok": False, "room_id": "", "reason": "room",
                    "error": "创建房间失败：%s" % str(e)[:80]}
        _PENDING_AUTO[int(row["id"])] = {
            "room_id": room["id"], "since": time.time(), "tried": 0}
        logs.log_event("follows.auto_start", follow_id=int(row["id"]),
                       room_id=room["id"], platform=row.get("platform") or "")
        return {"ok": True, "room_id": room["id"], "reason": "", "error": ""}


def _on_live(conn, row: dict, probe: dict, active: list[dict],
             is_new_episode: bool) -> dict:
    fid = int(row["id"])
    key = str(row["room_key"])
    updates: dict[str, Any] = {"live_state": "live", "fail_count": 0,
                               "last_checked_at": now(), "last_live_at": now(),
                               "last_error": ""}
    if probe.get("name"):
        updates["name"] = str(probe["name"])[:80]
    if probe.get("title"):
        updates["live_title"] = str(probe["title"])[:200]
    if probe.get("live_started_at"):
        updates["live_started_at"] = str(probe["live_started_at"])
    if not row.get("live_episode"):
        updates["live_episode"] = str(probe.get("live_started_at") or now())
    updated = _set(conn, fid, **updates) or row
    episode = str(updated.get("live_episode") or "")
    if not is_new_episode:
        return updated

    already = _room_already_recording(active, key)
    auto = bool(row.get("auto_record"))
    started: dict = {"ok": False}
    if auto and not already:
        started = _start_auto_record(updated, active)
    if started.get("ok"):
        # 与自动记录合并成一条，避免一次开播弹两条（D5）。
        return updated
    if auto and not already:
        notify.auto_record_failed(
            fid, str(updated.get("name") or "这个直播间"),
            str(started.get("error") or "自动记录没能开始"), episode)
    if row.get("notify") and not already:
        notify.follow_live(fid, str(updated.get("name") or "你关注的主播"),
                           episode, str(updated.get("live_title") or ""),
                           str(probe.get("live_started_at") or ""))
    return updated


def _on_offline(conn, row: dict, probe: dict) -> dict:
    updates: dict[str, Any] = {"live_state": "offline", "fail_count": 0,
                               "last_checked_at": now(), "last_error": "",
                               "live_episode": "", "live_started_at": ""}
    if probe.get("name"):
        updates["name"] = str(probe["name"])[:80]
    return _set(conn, int(row["id"]), **updates) or row


def apply_probe(row: dict, probe: dict, active: list[dict] | None = None) -> dict:
    """把一次探测结果落到关注行。纯状态机，worker 和「立即检测」共用。"""
    active = active if active is not None else rooms.list_rooms()
    state = str(probe.get("state") or "unknown")
    prev = str(row.get("live_state") or "unknown")
    conn = store.connect()
    try:
        if state == "unknown" and probe.get("error"):
            return _fail(conn, row, probe)
        if state == "live":
            # 原来不是 live、且这一场之前没见过 -> 新开播；live→unknown→live
            # 的抖动因为 episode 还在，不会重复提醒。
            is_new = prev != "live" and not row.get("live_episode")
            return _on_live(conn, row, probe, active, is_new)
        if state == "offline":
            return _on_offline(conn, row, probe)
        return _fail(conn, row, probe)
    finally:
        conn.close()


def check(fid: int = 0) -> dict:
    """立即检测一个/全部关注。同步，失败也返回 200 + last_error。"""
    conn = store.connect()
    try:
        if fid:
            rows = [store.get_follow(conn, fid)]
        else:
            rows = store.list_follows(conn)
    finally:
        conn.close()
    rows = [r for r in rows if r]
    active = rooms.list_rooms()
    out = []
    for row in rows:
        probe = liveprobe.probe(str(row.get("url") or ""),
                                str(row.get("platform") or ""), timeout=8.0)
        updated = apply_probe(row, probe, active)
        out.append(_view(updated, active))
    return {"items": out, "checked": len(out)}


# ---------------------------------------------------------------- 自动记录落地

def _check_pending(active: list[dict]) -> None:
    """采集代理把房间拉起来后，补通知与 last_session_id；失败则一次重试。"""
    if not _PENDING_AUTO:
        return
    by_id = {str(r.get("id")): r for r in active}
    conn = store.connect()
    try:
        for fid, st in list(_PENDING_AUTO.items()):
            room = by_id.get(str(st.get("room_id")))
            row = store.get_follow(conn, fid)
            if not row:
                _PENDING_AUTO.pop(fid, None)
                continue
            sid = str((room or {}).get("session_id") or "")
            state = str((room or {}).get("state") or "")
            if sid and state in ("live", "starting"):
                _set(conn, fid, last_session_id=sid, last_error="")
                notify.auto_record(fid, sid, str(row.get("name") or "这个直播间"),
                                   str(row.get("live_episode") or ""))
                _PENDING_AUTO.pop(fid, None)
                continue
            if state == "failed":
                err = str((room or {}).get("error") or "采集没有启动")
                if int(st.get("tried") or 0) < 1:
                    st["tried"] = 1
                    st["retry_at"] = time.time() + 90
                    continue
                notify.auto_record_failed(fid, str(row.get("name") or "这个直播间"),
                                          err, str(row.get("live_episode") or ""))
                _PENDING_AUTO.pop(fid, None)
                continue
            # 90 秒还没起来：给一次重试（把房间放回 pending）；仍失败就通知。
            if st.get("retry_at") and time.time() >= float(st["retry_at"]):
                if int(st.get("tried") or 0) == 0:
                    st["tried"] = 1
                    st["retry_at"] = time.time() + 90
                    try:
                        rooms.update(str(st["room_id"]), state="pending",
                                     error="", session_id="")
                    except Exception:
                        pass
                elif time.time() >= float(st.get("retry_at") or 0) + 90:
                    notify.auto_record_failed(
                        fid, str(row.get("name") or "这个直播间"),
                        "采集没有启动，点开关注列表重试", str(row.get("live_episode") or ""))
                    _PENDING_AUTO.pop(fid, None)
    finally:
        conn.close()


# ---------------------------------------------------------------- worker

def tick() -> dict:
    """一轮检测。同步执行，测试可直接调用。"""
    conn = store.connect()
    try:
        rows = store.list_follows(conn)
    finally:
        conn.close()
    active = rooms.list_rooms()
    _check_pending(active)
    if not rows:
        return {"checked": 0, "live": 0}
    results: list[tuple[dict, dict]] = []
    futures = {_PROBE_POOL.submit(liveprobe.probe, str(r.get("url") or ""),
                                  str(r.get("platform") or ""), 8.0): r
               for r in rows}
    for fut in as_completed(futures, timeout=max(15, len(rows) * 4)):
        row = futures[fut]
        try:
            results.append((row, fut.result()))
        except Exception as e:
            results.append((row, {"state": "unknown",
                                  "error": "检测失败（%s）" % str(e)[:60]}))
    for row, probe in results:
        try:
            apply_probe(row, probe, active)
        except Exception:
            logs.log_event("follows.apply_failed", level="warn",
                           follow_id=int(row.get("id") or 0), error=str(probe)[:200])
    return {"checked": len(results),
            "live": sum(1 for _r, p in results if p.get("state") == "live")}


def _loop() -> None:
    # 启动后先等 3 秒：让引擎/采集代理先就绪，别在启动竞态里误判。
    if _STOP.wait(3.0):
        return
    while not _STOP.is_set():
        try:
            tick()
        except Exception:
            logs.log_event("follows.tick_failed", level="warn")
        # 有开播提醒或自动记录在等结果时，缩短间隔（最短 60 秒）。
        wait = interval_s()
        if _PENDING_AUTO:
            wait = min(wait, 60)
        _STOP.wait(wait)


def start_worker() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return
        _STOP.clear()
        _WORKER = threading.Thread(target=_loop, name="follows", daemon=True)
        _WORKER.start()
        logs.log_event("follows.worker_start", interval_s=interval_s())


def stop_worker() -> None:
    _STOP.set()
