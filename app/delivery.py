# -*- coding: utf-8 -*-
"""精修交付：把一场直播整理成可以直接发给客户的报告包。

这不是「再生成一篇笔记」，而是一条内部生产线：

    建项目 → 生成草稿 → 人工校对/补写 → 质检 → 打包 → 交付 → 修订

每个项目绑定一场直播，但一场直播可以交付给多个客户，所以 ``session_id``
不唯一。报告按版本落库，交付包永远取最新一版；重新打包不会覆盖历史版本。

设计约束：
- 不新增 Python 依赖：报告同时出 Markdown 与自包含 HTML（浏览器可直接打印 PDF）；
- 不调模型也能出草稿：复用 report.py 的「总结 → 高光 → 原句」降级与证据字段；
- 音频片段用 AAC m4a，客户在 Windows/微信/飞书里能直接播；
- 所有文件落在 ``data/deliveries/<project_id>/``，打包成同名 zip。
"""
from __future__ import annotations

import html as _html
import json
import re
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import audio, clips as clipmod, export, report, store
from .config import DELIVERIES_DIR
from .models import fmt_ts

#: 套餐：价格是默认值，建项目时可以改；clips 是期望的片段数。
PACKAGES = {
    "standard": {"label": "标准复盘", "price_cents": 99900, "clips": 3, "days": 3},
    "deep": {"label": "深度复盘", "price_cents": 199900, "clips": 5, "days": 2},
    "event": {"label": "活动 / 峰会", "price_cents": 499900, "clips": 8, "days": 3},
}

STATES = {
    "draft": "草稿",
    "producing": "制作中",
    "review": "待交付",
    "delivered": "已交付",
    "revising": "修订中",
}

#: 报告固定章节。顺序就是页面和导出顺序；required 只影响质检提示，不阻止导出。
SECTION_SPECS = [
    ("lead", "导语", "text", True),
    ("context", "本场概览", "text", True),
    ("points", "核心内容", "points", True),
    ("quotes", "金句", "quotes", False),
    ("clips", "值得回听", "clips", False),
    ("actions", "行动建议", "text", False),
    ("questions", "待确认", "text", False),
]

MANUAL_CHECKS = {
    "authorized": "已确认主播/嘉宾授权与使用范围",
    "scope": "已确认交付范围与截止时间",
    "proofread": "逐字稿已人工校对",
    "glossary": "术语与敏感信息已处理",
    "reviewed": "报告已人工通读",
}

_TS_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


# ---------------------------------------------------------------- 基础

def _now() -> str:
    return store.now()


def _new_id() -> str:
    import secrets
    return "dlv_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        out = json.loads(str(raw or ""))
        return out if isinstance(out, type(default)) else default
    except (ValueError, TypeError):
        return default


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _pkg(key: str) -> dict:
    return PACKAGES.get(str(key or ""), PACKAGES["standard"])


def _state(key: str) -> str:
    return STATES.get(str(key or ""), str(key or ""))


def _duration(seconds: float) -> str:
    return report.duration_text(float(seconds or 0))


def _slug(text: str, limit: int = 26) -> str:
    s = _ILLEGAL.sub(" ", str(text or "")).strip()
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .-_")
    return (s[:limit].strip() or "片段")


def _row_item(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["checklist"] = _loads(d.pop("checklist_json", "{}"), {})
    pkg_key = str(d.get("package") or "standard")
    d["package_key"] = pkg_key if pkg_key in PACKAGES else "standard"
    d["package_label"] = _pkg(d["package_key"])["label"]
    # 列 package 是套餐键；package_json 才是打包结果，别让两者互相覆盖。
    d["package"] = _loads(d.pop("package_json", "{}"), {})
    d["state_label"] = _state(d.get("state"))
    d["price_cents"] = int(d.get("price_cents") or 0)
    d["revision_count"] = int(d.get("revision_count") or 0)
    return d


def _project(conn: sqlite3.Connection, pid: str) -> dict:
    row = conn.execute(
        "SELECT p.*, s.title AS session_title, s.started_at, s.duration_s, "
        "s.audio_path, s.source_mode FROM delivery_projects p "
        "LEFT JOIN sessions s ON s.id=p.session_id WHERE p.id=?", (pid,)).fetchone()
    if not row:
        raise ValueError("交付项目不存在")
    return _row_item(row)


def _utterance_count(conn: sqlite3.Connection, sid: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM utterances WHERE session_id=?", (sid,)).fetchone()
    return int(row["n"] or 0) if row else 0


def _latest_report(conn: sqlite3.Connection, pid: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM delivery_reports WHERE project_id=? "
        "ORDER BY version DESC LIMIT 1", (pid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["sections"] = _loads(d.pop("sections_json", "[]"), [])
    return d


# ---------------------------------------------------------------- 项目

def create_project(conn: sqlite3.Connection, session_id: str, *,
                   client_name: str = "", event_name: str = "",
                   package: str = "standard", due_at: str = "",
                   price_cents: int | None = None, notes: str = "") -> dict:
    sid = str(session_id or "").strip()
    session = store.get_session(conn, sid) if sid else None
    if not session:
        raise ValueError("先选一场已经录完的直播")
    pkg = str(package or "standard")
    if pkg not in PACKAGES:
        pkg = "standard"
    spec = _pkg(pkg)
    due = str(due_at or "").strip()
    if not due:
        due = (date.today() + timedelta(days=spec["days"])).isoformat()
    price = spec["price_cents"] if price_cents is None else max(0, int(price_cents))
    now = _now()
    pid = _new_id()
    conn.execute(
        "INSERT INTO delivery_projects (id,session_id,client_name,event_name,package,"
        "state,due_at,price_cents,checklist_json,package_json,notes,revision_count,"
        "delivered_at,created_at,updated_at) VALUES (?,?,?,?,?,'draft',?,?,?,?,?,0,'',?,?)",
        (pid, sid, str(client_name or "").strip()[:80],
         str(event_name or "").strip()[:120], pkg, due, price,
         "{}", "{}", str(notes or "").strip()[:2000], now, now))
    conn.commit()
    return get_project(conn, pid)


def list_projects(conn: sqlite3.Connection, state: str = "") -> dict:
    rows = conn.execute(
        "SELECT p.*, s.title AS session_title, s.started_at, s.duration_s, "
        "s.audio_path, s.source_mode, "
        "(SELECT MAX(version) FROM delivery_reports r WHERE r.project_id=p.id) "
        "AS report_version FROM delivery_projects p "
        "LEFT JOIN sessions s ON s.id=p.session_id "
        "ORDER BY p.updated_at DESC, p.created_at DESC").fetchall()
    items = [_row_item(r) for r in rows]
    # 统计口径始终是全部项目，不受左侧筛选影响。
    stats = {
        "active": sum(1 for x in items if x["state"] in ("draft", "producing")),
        "review": sum(1 for x in items if x["state"] == "review"),
        # 交付过又进入修订的项目仍算「已交付」，否则月度数字会来回跳。
        "delivered": sum(1 for x in items if x["delivered_at"]),
        "revising": sum(1 for x in items if x["state"] == "revising"),
    }
    flt = str(state or "").strip()
    if flt and flt != "all":
        if flt == "active":
            items = [x for x in items if x["state"] in ("draft", "producing")]
        elif flt == "review":
            items = [x for x in items if x["state"] == "review"]
        elif flt == "delivered":
            items = [x for x in items if x["state"] == "delivered"]
        elif flt == "revising":
            items = [x for x in items if x["state"] == "revising"]
        else:
            items = [x for x in items if x["state"] == flt]
    month = date.today().strftime("%Y-%m")
    revenue = conn.execute(
        "SELECT COALESCE(SUM(price_cents),0) AS n FROM delivery_projects "
        "WHERE substr(delivered_at,1,7)=?", (month,)).fetchone()
    stats["revenue_cents"] = int(revenue["n"] or 0) if revenue else 0
    return {"items": items, "stats": stats}


def update_project(conn: sqlite3.Connection, pid: str, **fields) -> dict:
    p = _project(conn, pid)
    allowed = {"client_name", "event_name", "package", "state", "due_at",
               "price_cents", "notes", "checklist"}
    sets: list[str] = []
    vals: list[Any] = []
    for key in allowed:
        if key not in fields:
            continue
        val = fields[key]
        if key == "package":
            val = str(val or p["package_key"])
            if val not in PACKAGES:
                raise ValueError("不认识的套餐")
        elif key == "state":
            val = str(val or p["state"])
            if val not in STATES:
                raise ValueError("不认识的项目状态")
        elif key == "checklist":
            val = val if isinstance(val, dict) else {}
            val = {str(k)[:40]: bool(v) for k, v in val.items() if k in MANUAL_CHECKS}
            val = _dump(val)
        elif key == "price_cents":
            val = max(0, int(val or 0))
        else:
            val = str(val or "").strip()[:2000 if key == "notes" else 160]
        sets.append(("checklist_json" if key == "checklist" else key) + "=?")
        vals.append(val)
    if not sets:
        return get_project(conn, pid)
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(pid)
    conn.execute("UPDATE delivery_projects SET %s WHERE id=?" % ",".join(sets), vals)
    conn.commit()
    return get_project(conn, pid)


def remove_project(conn: sqlite3.Connection, pid: str) -> None:
    p = _project(conn, pid)
    conn.execute("DELETE FROM delivery_reports WHERE project_id=?", (pid,))
    conn.execute("DELETE FROM delivery_projects WHERE id=?", (pid,))
    conn.commit()
    out = Path(DELIVERIES_DIR) / p["id"]
    if out.is_dir():
        shutil.rmtree(out, ignore_errors=True)
    z = Path(DELIVERIES_DIR) / (p["id"] + ".zip")
    z.unlink(missing_ok=True)


# ---------------------------------------------------------------- 报告

def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_sections(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("报告章节格式不对")
    out: list[dict] = []
    for i, s in enumerate(raw[:40]):
        if not isinstance(s, dict):
            continue
        title = str(s.get("title") or "").strip()[:80]
        if not title:
            continue
        body = str(s.get("body") or "")[:20000]
        items = s.get("items") if isinstance(s.get("items"), list) else []
        clean_items: list[dict] = []
        for it in items[:30]:
            if not isinstance(it, dict):
                continue
            clean_items.append({
                "t0": _f(it.get("t0")),
                "t1": _f(it.get("t1")),
                "t": _f(it.get("t") or it.get("t0")),
                "title": str(it.get("title") or "")[:80],
                "quote": str(it.get("quote") or "")[:600],
                "speaker": str(it.get("speaker") or "")[:40],
            })
        out.append({"key": str(s.get("key") or ("s%d" % i))[:40],
                    "title": title, "body": body,
                    "kind": str(s.get("kind") or "text")[:20],
                    "items": clean_items})
    if not out:
        raise ValueError("报告不能为空")
    return out


def save_report(conn: sqlite3.Connection, pid: str, sections: Any,
                engine: str = "manual") -> dict:
    p = _project(conn, pid)
    clean = _clean_sections(sections)
    row = conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM delivery_reports WHERE project_id=?",
        (pid,)).fetchone()
    version = int(row["v"] or 0) + 1
    conn.execute(
        "INSERT INTO delivery_reports (project_id,version,sections_json,engine,created_at) "
        "VALUES (?,?,?,?,?)", (pid, version, _dump(clean),
                               str(engine or "manual")[:20], _now()))
    state = p["state"]
    if state in ("draft", "revising", "delivered"):
        state = "producing"
    conn.execute("UPDATE delivery_projects SET state=?, updated_at=? WHERE id=?",
                 (state, _now(), pid))
    conn.commit()
    return get_project(conn, pid)


def _point_lines(points: list[dict]) -> tuple[str, list[dict]]:
    lines: list[str] = []
    evidence: list[dict] = []
    for i, p in enumerate(points, 1):
        title = str(p.get("title") or "要点").strip()
        gist = str(p.get("gist") or "").strip()
        quote = str(p.get("quote") or "").strip()
        speaker = str(p.get("speaker") or "").strip()
        t = p.get("quote_t")
        if t is None:
            t = p.get("t_from")
        lines.append("### %d. %s" % (i, title))
        lines.append("")
        if gist:
            lines += [gist, ""]
        if quote:
            who = (speaker + "：") if speaker else ""
            lines.append("> `[%s]` %s%s" % (fmt_ts(float(t or 0)), who, quote))
            lines.append("")
            evidence.append({"t": float(t or 0), "text": quote, "speaker": speaker})
    return "\n".join(lines).strip(), evidence


def _quote_lines(scenes: list[dict], points: list[dict]) -> tuple[str, list[dict]]:
    picked: list[dict] = []
    seen: set[str] = set()
    for s in list(scenes) + list(points):
        quote = str(s.get("quote") or "").strip()
        if not quote or quote in seen:
            continue
        seen.add(quote)
        picked.append({"t": float(s.get("quote_t") or s.get("t_from") or 0),
                       "text": quote, "speaker": str(s.get("speaker") or "").strip()})
        if len(picked) >= 3:
            break
    lines = ["- `[%s]` %s「%s」" % (fmt_ts(x["t"]),
                                   (x["speaker"] + "：") if x["speaker"] else "",
                                   x["text"])
             for x in picked]
    return "\n".join(lines), picked


def _clip_items(scenes: list[dict], limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for s in scenes:
        quote = str(s.get("quote") or "").strip()
        t0 = float(s.get("t_from") or 0)
        t1 = float(s.get("t_to") or 0)
        if t1 <= t0:
            t1 = t0 + 45
        if quote and quote in seen:
            continue
        seen.add(quote)
        out.append({"t0": t0, "t1": min(t1, t0 + 300),
                    "title": str(s.get("title") or quote[:30] or "高光片段")[:80],
                    "quote": quote, "speaker": str(s.get("speaker") or "").strip()})
        if len(out) >= limit:
            break
    return out


def _compose(conn: sqlite3.Connection, p: dict, session: dict,
             material: dict) -> list[dict]:
    sid = str(session["id"])
    spec = _pkg(p["package_key"])
    topics = [str(x) for x in (material.get("topics") or []) if str(x).strip()]
    speakers = store.speaker_totals(conn, sid)
    speaker_text = "、".join(
        "%s（%s）" % (store.speaker_display(x.get("speaker_label"),
                                           x.get("person_name"), x.get("person_state")),
                      fmt_ts(x.get("seconds") or 0))
        for x in speakers[:8])
    context = "\n".join([
        "- 时间：%s" % (session.get("started_at") or "—"),
        "- 时长：%s" % _duration(session.get("duration_s") or 0),
        "- 说话人：%s" % (speaker_text or "—"),
        "- 主题：%s" % ("、".join(topics[:6]) if topics else "—"),
        "- 素材：%d 条发言" % int(session.get("n_utterances") or material.get("n_utterances") or 0),
    ])
    points = material.get("points") or []
    points_body, evidence = _point_lines(points)
    quotes_body, quotes = _quote_lines(material.get("scenes") or [], points)
    clip_items = _clip_items(material.get("scenes") or [], spec["clips"])
    clips_body = "\n".join(
        "- `[%s–%s]` %s%s" % (fmt_ts(x["t0"]), fmt_ts(x["t1"]),
                              (x["title"] + "：") if x["title"] else "",
                              x["quote"] or "")
        for x in clip_items)
    return [
        {"key": "lead", "title": "导语", "kind": "text",
         "body": str(material.get("overview") or "").strip(), "items": []},
        {"key": "context", "title": "本场概览", "kind": "text",
         "body": context, "items": []},
        {"key": "points", "title": "核心内容", "kind": "points",
         "body": points_body, "items": evidence},
        {"key": "quotes", "title": "金句", "kind": "quotes",
         "body": quotes_body, "items": quotes},
        {"key": "clips", "title": "值得回听", "kind": "clips",
         "body": clips_body, "items": clip_items},
        {"key": "actions", "title": "行动建议", "kind": "text",
         "body": "", "items": []},
        {"key": "questions", "title": "待确认", "kind": "text",
         "body": "", "items": []},
    ]


def build_draft(conn: sqlite3.Connection, pid: str, force: bool = False) -> dict:
    p = _project(conn, pid)
    existing = _latest_report(conn, pid)
    if existing and existing.get("engine") == "manual" and not force:
        raise ValueError("已经有手工编辑过的版本；确认覆盖后再重新生成")
    session = store.get_session(conn, p["session_id"])
    if not session:
        raise ValueError("这场直播已经不在了")
    session = dict(session)
    session["n_utterances"] = _utterance_count(conn, p["session_id"])
    material = report.session_material(conn, session)
    sections = _compose(conn, p, session, material)
    out = save_report(conn, pid, sections, engine="draft")
    return out


# ---------------------------------------------------------------- 质检

def qa(conn: sqlite3.Connection, pid: str) -> dict:
    p = _project(conn, pid)
    session = store.get_session(conn, p["session_id"]) or {}
    rep = _latest_report(conn, pid)
    checks: list[dict] = []

    def add(key: str, label: str, status: str, detail: str = "") -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail})

    n_utt = _utterance_count(conn, p["session_id"]) if session else 0
    if not session:
        add("session", "场次存在", "fail", "找不到这场直播")
    elif not n_utt:
        add("session", "有逐字稿", "fail", "这场没有可用的发言")
    else:
        add("session", "有逐字稿", "pass", "%d 条发言" % n_utt)

    sections = rep.get("sections") if rep else []
    lead = next((s for s in sections if s.get("key") == "lead"), {})
    points = next((s for s in sections if s.get("key") == "points"), {})
    if not rep:
        add("report", "报告已生成", "fail", "先点「生成草稿」")
    elif not str(points.get("body") or "").strip():
        add("report", "报告已生成", "fail", "「核心内容」还是空的")
    elif not str(lead.get("body") or "").strip():
        add("report", "报告已生成", "warn", "「导语」还是空的")
    else:
        add("report", "报告已生成", "pass", "v%d" % int(rep.get("version") or 0))

    n_ts = len(_TS_RE.findall(str(points.get("body") or "")))
    if not rep:
        add("evidence", "要点带时间码", "warn", "报告还没生成")
    elif n_ts <= 0:
        add("evidence", "要点带时间码", "fail", "「核心内容」里没有可回听的时间码")
    else:
        add("evidence", "要点带时间码", "pass", "%d 处" % n_ts)

    unknown = conn.execute(
        "SELECT COUNT(*) AS n FROM speakers WHERE session_id=? "
        "AND COALESCE(verified,0)=0", (p["session_id"],)).fetchone()
    n_unknown = int(unknown["n"] or 0) if unknown else 0
    if n_unknown <= 0:
        add("speakers", "说话人已实名", "pass", "全部已确认")
    else:
        status = "fail" if p["package_key"] in ("deep", "event") else "warn"
        add("speakers", "说话人已实名", status, "还有 %d 位未确认" % n_unknown)

    ap = Path(str(session.get("audio_path") or ""))
    audio_ok = bool(str(session.get("audio_path") or "")) and ap.is_file()
    add("audio", "原声可用", "pass" if audio_ok else "warn",
        "已找到录音" if audio_ok else "没有可用录音，交付包只含文字")

    clip_items = next((s.get("items") or [] for s in sections
                       if s.get("key") == "clips"), [])
    need = _pkg(p["package_key"])["clips"]
    if len(clip_items) >= need:
        add("clips", "高光片段", "pass", "%d 段（套餐要求 %d 段）" % (len(clip_items), need))
    elif clip_items:
        add("clips", "高光片段", "warn", "只有 %d 段（套餐建议 %d 段）" % (len(clip_items), need))
    else:
        add("clips", "高光片段", "warn", "还没有可交付的片段")

    if not str(p.get("event_name") or "").strip():
        add("cover", "封面信息", "fail", "还没填活动 / 场次名称")
    elif not str(p.get("client_name") or "").strip():
        add("cover", "封面信息", "warn", "还没填客户名称")
    else:
        add("cover", "封面信息", "pass", "%s · %s" % (p["client_name"], p["event_name"]))

    overall = "fail" if any(c["status"] == "fail" for c in checks) else \
        "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    return {"ok": overall != "fail", "overall": overall, "checks": checks}


def _checklist(p: dict, qa_result: dict) -> dict:
    qmap = {c["key"]: c for c in qa_result.get("checks") or []}
    manual = p.get("checklist") or {}
    items: list[dict] = []
    for key, label in MANUAL_CHECKS.items():
        items.append({"key": key, "label": label, "mode": "manual",
                      "checked": bool(manual.get(key)), "detail": ""})
    auto = [
        ("speakers", "说话人已实名", "speakers"),
        ("report", "报告已生成", "report"),
        ("evidence", "要点带时间码", "evidence"),
        ("audio", "原声可用", "audio"),
    ]
    for key, label, qkey in auto:
        c = qmap.get(qkey) or {}
        items.append({"key": key, "label": label, "mode": "auto",
                      "checked": c.get("status") == "pass",
                      "detail": c.get("detail") or ""})
    built = bool((p.get("package") or {}).get("built_at"))
    items.append({"key": "package", "label": "交付包已生成", "mode": "auto",
                  "checked": built, "detail": (p.get("package") or {}).get("zip") or ""})
    items.append({"key": "delivered", "label": "已交付客户", "mode": "auto",
                  "checked": p.get("state") == "delivered",
                  "detail": p.get("delivered_at") or ""})
    done = sum(1 for x in items if x["checked"])
    return {"items": items, "done": done, "total": len(items)}


def get_project(conn: sqlite3.Connection, pid: str) -> dict:
    p = _project(conn, pid)
    rep = _latest_report(conn, pid)
    checked = qa(conn, pid)
    return {"project": p, "report": rep, "qa": checked,
            "checklist": _checklist(p, checked)}


# ---------------------------------------------------------------- 渲染

def render_markdown(p: dict, rep: dict | None) -> str:
    sections = (rep or {}).get("sections") or []
    title = p.get("event_name") or p.get("session_title") or "直播"
    out = ["# %s · 精修报告" % title, ""]
    out.append("> 客户：%s　场次：%s" % (
        p.get("client_name") or "—", p.get("session_title") or p.get("session_id")))
    out.append("> 交付版本：v%s　时长：%s　生成：%s" % (
        (rep or {}).get("version") or 0,
        _duration(p.get("duration_s") or 0),
        datetime.now().strftime("%Y-%m-%d %H:%M")))
    out.append("")
    for s in sections:
        body = str(s.get("body") or "").strip()
        out += ["## %s" % (s.get("title") or ""), ""]
        if body:
            out += [body, ""]
    out += ["---", "由 LiveNotes 整理；音频与逐字稿来自本机记录。"]
    return "\n".join(out).strip() + "\n"


def _inline(text: str) -> str:
    s = _html.escape(str(text or ""))
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def _body_html(body: str) -> str:
    """把报告章节的轻量 Markdown 转成 HTML（标题/引用/列表/段落）。"""
    lines = str(body or "").splitlines()
    out: list[str] = []
    para: list[str] = []
    quote: list[str] = []
    lst: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>%s</p>" % "<br>".join(_inline(x) for x in para))
            para.clear()

    def flush_quote() -> None:
        if quote:
            out.append("<blockquote>%s</blockquote>" % "<br>".join(
                _inline(x) for x in quote))
            quote.clear()

    def flush_list() -> None:
        if lst:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x) for x in lst))
            lst.clear()

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_list()

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_all()
            continue
        if line.startswith("### "):
            flush_all()
            out.append("<h3>%s</h3>" % _inline(line[4:]))
        elif line.startswith("## "):
            flush_all()
            out.append("<h2>%s</h2>" % _inline(line[3:]))
        elif line.startswith("> "):
            flush_para()
            flush_list()
            quote.append(line[2:])
        elif line.startswith("- "):
            flush_para()
            flush_quote()
            lst.append(line[2:])
        else:
            flush_quote()
            flush_list()
            para.append(line)
    flush_all()
    return "\n".join(out)


def render_html(p: dict, rep: dict | None) -> str:
    sections = (rep or {}).get("sections") or []
    title = p.get("event_name") or p.get("session_title") or "直播"
    parts = []
    for s in sections:
        parts.append('<section><h2>%s</h2>%s</section>' % (
            _html.escape(str(s.get("title") or "")),
            _body_html(str(s.get("body") or ""))))
    meta = "客户：%s　场次：%s　版本：v%s　时长：%s" % (
        _html.escape(p.get("client_name") or "—"),
        _html.escape(p.get("session_title") or ""),
        (rep or {}).get("version") or 0,
        _html.escape(_duration(p.get("duration_s") or 0)))
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s · 精修报告</title>
<style>
:root{--ink:#1d2330;--muted:#6b7480;--line:#e6e9ef;--accent:#8a5a2b;--bg:#fbfaf7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
main{max-width:820px;margin:0 auto;padding:56px 32px 80px}
h1{font-size:30px;line-height:1.35;margin:0 0 10px}
h2{font-size:20px;margin:38px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:24px 0 6px}
p{margin:8px 0}
blockquote{margin:10px 0;padding:10px 14px;border-left:3px solid var(--accent);
 background:#fff;color:#3c4450;border-radius:0 8px 8px 0}
ul{margin:8px 0 8px 22px;padding:0}
li{margin:4px 0}
code{font-family:Consolas,"SF Mono",monospace;font-size:13px;color:var(--accent);
 background:#f3efe8;padding:1px 5px;border-radius:4px}
.meta{color:var(--muted);font-size:13px;margin-bottom:30px}
.foot{margin-top:56px;padding-top:16px;border-top:1px solid var(--line);
 color:var(--muted);font-size:12px}
@media print{body{background:#fff}main{max-width:none;padding:0 8mm}
 h2{page-break-after:avoid}section{page-break-inside:auto}}
</style></head><body><main>
<h1>%s · 精修报告</h1>
<p class="meta">%s</p>
%s
<p class="foot">由 LiveNotes 整理；音频与逐字稿来自本机记录。</p>
</main></body></html>""" % (_html.escape(title), _html.escape(title), meta,
                           "\n".join(parts))


# ---------------------------------------------------------------- 打包

def _readme(p: dict, rep: dict | None, files: list[str], warnings: list[str]) -> str:
    lines = [
        "# 交付包说明",
        "",
        "- 项目：%s" % (p.get("event_name") or p.get("session_title") or p.get("id")),
        "- 客户：%s" % (p.get("client_name") or "—"),
        "- 场次：%s（%s）" % (p.get("session_title") or "—", p.get("started_at") or "—"),
        "- 版本：v%s　生成：%s" % ((rep or {}).get("version") or 0,
                                   datetime.now().strftime("%Y-%m-%d %H:%M")),
        "- 套餐：%s" % p.get("package_label"),
        "",
        "## 文件",
        "",
    ]
    lines += ["- `%s`" % f for f in files]
    lines += [
        "",
        "## 使用与授权",
        "",
        "1. 本包内容来自公开直播的本机记录，仅供客户内部复盘使用。",
        "2. 音频含主播与嘉宾原声，未经许可不要公开传播、二次剪辑或用于商业投放。",
        "3. 需要对外发布时，请先取得主播/嘉宾授权，并只引用报告中的文字与短片段。",
        "4. 客户信息与录音按约定保留期删除；到期后可要求提供方删除本地副本。",
    ]
    if warnings:
        lines += ["", "## 本次打包提示", ""] + ["- %s" % w for w in warnings]
    return "\n".join(lines) + "\n"


def build_package(conn: sqlite3.Connection, pid: str,
                  include_audio: bool = True) -> dict:
    p = _project(conn, pid)
    rep = _latest_report(conn, pid)
    if not rep:
        raise ValueError("先生成报告草稿，再打包")
    out = Path(DELIVERIES_DIR) / pid
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    (out / "报告.md").write_text(render_markdown(p, rep), encoding="utf-8")
    (out / "报告.html").write_text(render_html(p, rep), encoding="utf-8")
    (out / "逐字稿.srt").write_text(export.to_srt(conn, p["session_id"]),
                                    encoding="utf-8")
    (out / "逐字稿.md").write_text(export.to_markdown(conn, p["session_id"]),
                                   encoding="utf-8")

    clip_items = next((s.get("items") or [] for s in (rep.get("sections") or [])
                       if s.get("key") == "clips"), [])
    clips_dir = out / "高光原声"
    made = 0
    if include_audio and clip_items:
        clips_dir.mkdir(exist_ok=True)
        src, total, kind = clipmod.source_for(p["session_id"], conn=conn)
        if not src:
            warnings.append("没有可用录音，未生成原声片段")
        else:
            for i, item in enumerate(clip_items, 1):
                t0 = max(0.0, float(item.get("t0") or 0))
                t1 = float(item.get("t1") or 0)
                if t1 <= t0:
                    t1 = t0 + 45
                duration = max(5.0, min(300.0, t1 - t0))
                if total and t0 >= total:
                    warnings.append("第 %d 段起点超出录音时长，已跳过" % i)
                    continue
                if total:
                    duration = max(1.0, min(duration, total - t0))
                dst = clips_dir / ("%02d_%s.m4a" % (i, _slug(item.get("title")
                                                              or item.get("quote"))))
                ok, err = audio.extract_m4a(src, dst, t0, duration)
                if ok and dst.is_file() and dst.stat().st_size > 0:
                    made += 1
                else:
                    warnings.append("第 %d 段切片失败：%s" % (i, err or "ffmpeg 不可用"))
                    dst.unlink(missing_ok=True)
    elif include_audio and not clip_items:
        warnings.append("报告里还没有高光片段")

    files = []
    for f in sorted(out.rglob("*")):
        if f.is_file():
            files.append(str(f.relative_to(out)).replace("\\", "/"))
    files += ["README.md", "meta.json"]
    (out / "README.md").write_text(_readme(p, rep, files, warnings), encoding="utf-8")
    meta = {
        "project_id": pid, "session_id": p["session_id"],
        "title": p.get("event_name") or p.get("session_title") or "",
        "client": p.get("client_name") or "",
        "package": p.get("package_key"), "version": (rep or {}).get("version") or 0,
        "clips": made, "warnings": warnings,
        "built_at": _now(),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    files = sorted(str(f.relative_to(out)).replace("\\", "/")
                   for f in out.rglob("*") if f.is_file())
    zpath = Path(DELIVERIES_DIR) / (pid + ".zip")
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, arcname=str(f.relative_to(out)).replace("\\", "/"))
    tmp.replace(zpath)

    info = {"built_at": _now(), "zip": str(zpath), "files": files,
            "clips": made, "warnings": warnings}
    state = "review"
    conn.execute(
        "UPDATE delivery_projects SET package_json=?, state=?, updated_at=? WHERE id=?",
        (_dump(info), state, _now(), pid))
    conn.commit()
    out = get_project(conn, pid)
    out["built"] = info
    return out


def mark_delivered(conn: sqlite3.Connection, pid: str) -> dict:
    p = _project(conn, pid)
    info = p.get("package") or {}
    if not info.get("built_at"):
        raise ValueError("先生成交付包，再标记已交付")
    now = _now()
    conn.execute(
        "UPDATE delivery_projects SET state='delivered', delivered_at=?, updated_at=? "
        "WHERE id=?", (now, now, pid))
    conn.commit()
    return get_project(conn, pid)


def add_revision(conn: sqlite3.Connection, pid: str, note: str = "") -> dict:
    p = _project(conn, pid)
    n = int(p.get("revision_count") or 0) + 1
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = "[修订 %d · %s] %s" % (n, stamp, str(note or "").strip()[:500])
    notes = (str(p.get("notes") or "").rstrip() + "\n" + line).strip()
    conn.execute(
        "UPDATE delivery_projects SET revision_count=?, state='revising', notes=?, "
        "updated_at=? WHERE id=?", (n, notes, _now(), pid))
    conn.commit()
    return get_project(conn, pid)


def package_path(conn: sqlite3.Connection, pid: str) -> Path | None:
    p = _project(conn, pid)
    z = Path(str((p.get("package") or {}).get("zip") or ""))
    return z if z.is_file() else None
