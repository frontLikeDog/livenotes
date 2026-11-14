# -*- coding: utf-8 -*-
"""周报 / 复盘：把一段时间内的多场直播汇总成一篇可沉淀的文档。

为什么单独成模块，而不是复用单场导出：

1. **周报的单位是「多场」**，要回答的是「这一周聊了什么、哪几场值得回看」；
   单场导出是逐字稿，直接拼接只会得到一份更长的逐字稿。
2. **云端只放能公开的内容**：摘要、要点、原话、妙记链接。局域网 reader 地址
   一律不写进文档——转发出去打不开，等于给了个假入口。
3. **没有总结的场次也要有东西**：优先用已生成的总结；没有就退回高光片段，
   再没有就取高权重原句。报告不能因为某一场没点过「生成总结」就整段空白。

复盘模板在此基础上留出「结论与行动」人工填写区；周报只陈述事实。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

from . import highlight, llm, settings, store, timefmt
from .config import REPORTS_DIR
from .models import category_label

KINDS = {"weekly": "直播周报", "retro": "直播复盘"}
MAX_POINTS = 4
MAX_TOPICS = 6
MAX_QUOTE = 120
MAX_SESSIONS = 30
DETAIL_MIN_S = 180       # 短于 3 分钟且没生成过总结的场次只列标题，不展开
REPORT_RETENTION_DAYS = 30
MAX_STORYLINES = 5
MAX_GOLDEN_QUOTES = 3

_WEEKDAYS = "一二三四五六日"
_CN_NUMS = "一二三四五六七八九十"
_RANGE_SEP = re.compile(r"\s*(?:~|～|至|到|—|－)\s*")
_GENERIC_TOPICS = {"", "综合", "综合/未归类", "未归类", "其他", "其它"}
_NORM_RE = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()《》「」【】…—\-]+")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _day(raw) -> date | None:
    """从 started_at（本地 naive ISO）取日期。解析不了就当不在范围内。"""
    s = str(raw or "").strip()
    if len(s) >= 10:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _parse_day_token(token: str, today: date) -> date:
    t = str(token or "").strip().replace("/", "-").replace(".", "-")
    parts = t.split("-")
    try:
        if len(parts) == 3:
            year = int(parts[0])
            # 三位以内的年份多半是把「11-03-09」这种写法拆错了；静默生成一篇
            # 公元 11 年的报告比直接报错更糟。
            if year < 1000:
                raise ValueError
            return date(year, int(parts[1]), int(parts[2]))
        if len(parts) == 2:
            return date(today.year, int(parts[0]), int(parts[1]))
    except (TypeError, ValueError):
        pass
    raise ValueError("日期看不懂：%s" % token)


def parse_range(text: str = "", today: date | None = None) -> dict:
    """解析用户给的范围，返回 {since, until, label}。

    支持：空（最近 7 天）、本周、上周、今天、最近 N 天 / N天、
    2026-11-03、2026-11-03~2026-11-09、11-03~11-09。
    """
    raw = str(text or "").strip()
    today = today or date.today()
    low = raw.lower().replace(" ", "")
    if not raw:
        since, until = today - timedelta(days=6), today
    elif low in ("本周", "这周", "thisweek"):
        since, until = _week_start(today), today
    elif low in ("上周", "lastweek"):
        this_monday = _week_start(today)
        since, until = this_monday - timedelta(days=7), this_monday - timedelta(days=1)
    elif low in ("今天", "今日", "today"):
        since = until = today
    else:
        m = re.fullmatch(r"(?:最近)?(\d{1,3})\s*(?:天|d|day|days)", low)
        if m:
            n = max(1, min(366, int(m.group(1))))
            since, until = today - timedelta(days=n - 1), today
        else:
            parts = _RANGE_SEP.split(raw, maxsplit=1)
            if len(parts) == 2 and parts[0] and parts[1]:
                since = _parse_day_token(parts[0], today)
                until = _parse_day_token(parts[1], today)
            elif parts and len(parts) == 1:
                since = until = _parse_day_token(parts[0], today)
            else:
                raise ValueError("范围看不懂。例：上周、7天、11-03~11-09")
    if since > until:
        since, until = until, since
    return {"since": since, "until": until, "label": range_label(since, until)}


def range_label(since: date, until: date) -> str:
    if since == until:
        return since.isoformat()
    if since.year == until.year:
        # 只省略年份，不省略月份：写成「11-03 ~ 09」会被读成 3 号到 9 号，
        # 跨月时更歧义。
        return "%s ~ %s" % (since.isoformat(), until.strftime("%m-%d"))
    return "%s ~ %s" % (since.isoformat(), until.isoformat())


def _day_label(d: date) -> str:
    return "%s 周%s" % (d.strftime("%m-%d"), _WEEKDAYS[d.weekday()])


def _date_span(since: date, until: date) -> str:
    if since == until:
        return since.isoformat()
    if since.year == until.year:
        return "%s 至 %s" % (since.isoformat(), until.strftime("%m-%d"))
    return "%s 至 %s" % (since.isoformat(), until.isoformat())


def duration_text(seconds: float) -> str:
    s = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h and m:
        return "%d 小时 %d 分" % (h, m)
    if h:
        return "%d 小时" % h
    return "%d 分钟" % max(1, m) if s else "0 分钟"


def _clean(text, limit: int = 0) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit] if limit and len(s) > limit else s


def _norm(text) -> str:
    return _NORM_RE.sub("", str(text or ""))


def _nums(text) -> set[str]:
    return set(_NUM_RE.findall(str(text or "")))


def _point_title(text, limit: int = 30) -> str:
    t = _clean(text, limit)
    if t in _GENERIC_TOPICS or t.startswith("话题：综合"):
        return ""
    return t


def _overview_text(text) -> str:
    """过滤单场总结里面向网页的套话（「点查看原文」在云文档里没有意义）。"""
    s = _clean(text, 200)
    if not s or "点「查看原文」" in s or s.startswith("本场共纳入"):
        return ""
    return s


def _whole_filters(f: dict) -> bool:
    return (not str(f.get("q") or "").strip()
            and not f.get("categories") and not f.get("speakers")
            and f.get("t_from") is None and f.get("t_to") is None)


def _whole_summary(conn, sid: str) -> dict:
    """取最近一份「整场、无筛选」的已生成总结。

    只认整场口径：筛选后的总结是某个片段的结论，拿进周报会把局部当全局。
    最近一份没有要点时继续往前找，避免一条空结果挡住更早的完整总结。
    """
    for r in store.recent_summaries(conn, sid, "content", 20):
        try:
            f = json.loads(r.get("filter_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(f, dict) or not _whole_filters(f):
            continue
        try:
            payload = json.loads(r.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and (payload.get("points") or payload.get("overview")):
            return payload
    return {}


def _session_topics(conn, sid: str, payload: dict) -> list[str]:
    raw = list((payload or {}).get("topics") or [])
    if not raw:
        raw = list(store.get_ai_topics(conn, sid) or [])
    if not raw:
        raw = [t for t, _ in store.topic_tally(conn, sid)]
    out: list[str] = []
    for t in raw:
        t = _clean(t, 20)
        if t and t not in out and t not in _GENERIC_TOPICS:
            out.append(t)
    return out[:MAX_TOPICS]


def _points_from_summary(payload: dict) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for p in (payload.get("points") or [])[:MAX_POINTS * 2]:
        if not isinstance(p, dict):
            continue
        title = _point_title(p.get("title"))
        gist = _clean(p.get("gist"), 160)
        ev = p.get("evidence") or []
        quote = speaker = ""
        qt = None
        if ev and isinstance(ev[0], dict):
            e = ev[0]
            quote = _clean(e.get("quote") or e.get("text"), MAX_QUOTE)
            speaker = _clean(e.get("speaker_label") or e.get("speaker"), 24)
            try:
                qt = float(e.get("t_start"))
            except (TypeError, ValueError):
                qt = None
        if not (title or gist or quote):
            continue
        key = (title + "|" + gist + "|" + quote).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            t_from = float(p.get("t_from"))
        except (TypeError, ValueError):
            t_from = qt
        try:
            t_to = float(p.get("t_to"))
        except (TypeError, ValueError):
            t_to = None
        out.append({"title": title, "gist": gist, "quote": quote,
                    "speaker": speaker, "t_from": t_from, "t_to": t_to,
                    "quote_t": qt})
        if len(out) >= MAX_POINTS:
            break
    return out


def _scenes_for(conn, sid: str) -> list[dict]:
    """取一场的高光场面（钩子 / 标签 / 原声）。

    优先用库里已经算过的；没有就现算一遍文本特征（不读音频、不调模型）。
    周报不该因为用户没点过「重新分析」就掉回逐字稿。
    """
    rows = store.list_highlights(conn, sid, limit=MAX_POINTS)
    if not rows:
        try:
            rows = highlight.score_session(conn, sid, limit=MAX_POINTS)
        except Exception:
            rows = []
    out: list[dict] = []
    seen: set[str] = set()
    for h in rows:
        quote = _clean(h.get("quote") or h.get("text"), MAX_QUOTE)
        if not quote or _norm(quote) in seen:
            continue
        seen.add(_norm(quote))
        out.append({
            "title": _clean(h.get("hook") or h.get("tag") or "高光片段", 30),
            "gist": _clean(h.get("why"), 120),
            "quote": quote,
            "speaker": _clean(h.get("speaker_label"), 24),
            "tag": _clean(h.get("tag"), 8),
            "t_from": float(h.get("t_start") or h.get("peak_t") or 0),
            "t_to": float(h.get("t_end") or 0) or None,
            "quote_t": float(h.get("peak_t") or h.get("t_start") or 0),
        })
    return out


def _points_from_highlights(scenes: list[dict]) -> list[dict]:
    return [dict(s) for s in scenes[:MAX_POINTS]]


def _points_from_utterances(conn, sid: str) -> list[dict]:
    """最后兜底：按重要度取原句。不调模型、不改库，只读。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT t_start,t_end,speaker_label,text,topic,category,importance "
        "FROM utterances WHERE session_id=? AND COALESCE(overlap,0)=0 "
        "AND COALESCE(category,'') IN ('substantive','question_consult') "
        "AND COALESCE(importance,0)>=0.35 "
        "AND LENGTH(COALESCE(text,''))>=8 "
        "ORDER BY importance DESC, LENGTH(text) DESC LIMIT ?",
        (sid, MAX_POINTS * 3))]
    picked: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        quote = _clean(r.get("text"), MAX_QUOTE)
        if not quote or quote.lower() in seen:
            continue
        seen.add(quote.lower())
        title = _point_title(r.get("topic"), 20) or category_label(
            str(r.get("category") or ""))
        picked.append({
            "title": title or "要点", "gist": "", "quote": quote,
            "speaker": _clean(r.get("speaker_label"), 24),
            "t_from": float(r.get("t_start") or 0),
            "t_to": float(r.get("t_end") or 0) or None,
            "quote_t": float(r.get("t_start") or 0),
        })
        if len(picked) >= MAX_POINTS:
            break
    return sorted(picked, key=lambda p: p.get("t_from") or 0)


def _fallback_overview(session: dict, topics: list[str],
                       has_summary: bool = False) -> str:
    if topics:
        return "本场主要围绕「%s」展开。" % "」「".join(topics[:3])
    n = int(session.get("n_utterances") or 0)
    if n:
        return ("本场共 %d 条发言。" % n if has_summary
                else "本场共 %d 条发言，还没有生成总结。" % n)
    return "本场没有可用字幕。"


def _material(conn, session: dict) -> dict:
    sid = str(session.get("id") or "")
    payload = _whole_summary(conn, sid)
    points = _points_from_summary(payload)
    scenes = _scenes_for(conn, sid)
    source = "总结"
    if not points:
        points = _points_from_highlights(scenes)
        source = "高光"
    if not points:
        points = _points_from_utterances(conn, sid)
        source = "原句"
    topics = _session_topics(conn, sid, payload)
    overview = _overview_text(payload.get("overview")) or _fallback_overview(
        session, topics, has_summary=bool(payload))
    d = _day(session.get("started_at"))
    return {
        "session_id": sid,
        "title": _clean(session.get("title"), 80) or sid,
        "day": d,
        "started_at": str(session.get("started_at") or ""),
        "duration_s": float(session.get("duration_s") or 0),
        "n_utterances": int(session.get("n_utterances") or 0),
        "minutes_url": str(session.get("minutes_url") or "").strip(),
        "overview": overview,
        "topics": topics,
        "points": points,
        "scenes": scenes,
        "source": source if points else "",
        "has_summary": bool(payload),
    }


def sessions_between(conn, since: date, until: date) -> list[dict]:
    out = []
    for s in store.list_sessions(conn):
        d = _day(s.get("started_at"))
        if d is None or not (since <= d <= until):
            continue
        if int(s.get("n_utterances") or 0) <= 0:
            continue
        s = dict(s)
        s["_day"] = d
        out.append(s)
    out.sort(key=lambda s: (str(s.get("started_at") or ""), str(s.get("id") or "")))
    return out


def _group_days(materials: list[dict]) -> "OrderedDict[date, list[dict]]":
    groups: "OrderedDict[date, list[dict]]" = OrderedDict()
    for m in materials:
        groups.setdefault(m["day"], []).append(m)
    return groups


def _span_text(p: dict) -> str:
    try:
        t0 = float(p.get("t_from") or 0)
    except (TypeError, ValueError):
        t0 = 0.0
    try:
        t1 = float(p.get("t_to") or 0)
    except (TypeError, ValueError):
        t1 = 0.0
    start = timefmt.offset(t0)
    if t1 > t0 + 0.5:
        return "%s–%s" % (start, timefmt.offset(t1))
    return start


def _point_line(p: dict) -> str:
    head = "**%s**（%s）" % (_point_title(p.get("title")) or "要点", _span_text(p))
    body = re.sub(r"[。；;，,]+$", "", _clean(p.get("gist"), 160))
    quote = _clean(p.get("quote"), MAX_QUOTE)
    who = _clean(p.get("speaker"), 24)
    parts = [body] if body else []
    if quote and quote not in body:
        parts.append("原话：「%s」%s" % (quote, (" —— " + who) if who else ""))
    elif quote and who:
        parts.append("—— " + who)
    return "- %s：%s" % (head, "；".join(parts)) if parts else "- %s" % head


def _material_stats(materials: list[dict]) -> dict:
    total = sum(m["duration_s"] for m in materials)
    days = len({m["day"] for m in materials})
    detail = [m for m in materials if m.get("detail")]
    return {
        "sessions": len(materials),
        "days": days,
        "duration_s": total,
        "detail": len(detail),
        "short": sum(1 for m in materials if m.get("compact_reason") == "short"),
        "with_summary": sum(1 for m in materials if m["has_summary"]),
        "with_minutes": sum(1 for m in materials if m["minutes_url"]),
        "without_summary": [m for m in materials if not m["has_summary"]],
        "without_minutes": [m for m in materials if not m["minutes_url"]],
        "topics": _cross_topics(materials),
    }


def _cross_topics(materials: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for m in materials:
        for t in m["topics"]:
            groups.setdefault(t, []).append(m)
    rows = [(t, ms) for t, ms in groups.items() if len(ms) >= 2]
    rows.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    return rows[:8]


def _topic_summary(topics: list[tuple[str, list[dict]]]) -> str:
    if not topics:
        return "没有跨场重复的话题。"
    return "、".join("%s（%d 场）" % (t, len(ms)) for t, ms in topics)


def _coverage_lines(stats: dict) -> list[str]:
    lines = []
    if stats.get("short"):
        lines.append("- 展开：%d 场重点；%d 场短场（<3 分钟）只列标题"
                     % (stats.get("detail", 0), stats["short"]))
    if stats.get("truncated"):
        lines.append("- 重点场次过多：只展开 %d 场，另有 %d 场只列标题"
                     % (stats.get("detail", 0), stats["truncated"]))
    if stats["with_summary"] < stats["sessions"]:
        names = "、".join("《%s》" % m["title"] for m in stats["without_summary"][:3])
        more = "" if len(stats["without_summary"]) <= 3 else " 等"
        lines.append("- 未生成总结：%s%s（已尽量用高光/原句兜底）" % (names, more))
    if stats["with_minutes"] < stats["sessions"]:
        names = "、".join("《%s》" % m["title"] for m in stats["without_minutes"][:3])
        more = "" if len(stats["without_minutes"]) <= 3 else " 等"
        lines.append("- 未回填妙记：%s%s" % (names, more))
    return lines


def _short_line(materials: list[dict]) -> str:
    names = "、".join("《%s》（%s）" % (m["title"], duration_text(m["duration_s"]))
                     for m in materials)
    return "未展开：" + names


def _session_head(m: dict) -> list[str]:
    meta = ["时长：%s" % duration_text(m["duration_s"])]
    if m["n_utterances"]:
        meta.append("发言 %d 条" % m["n_utterances"])
    lines = ["### 《%s》" % m["title"], "", "- " + " · ".join(meta)]
    if m["minutes_url"]:
        lines.append("- 妙记：[打开回放](%s)" % m["minutes_url"])
    return lines


def _session_body(m: dict) -> list[str]:
    lines = ["", "**概述**：%s" % m["overview"], ""]
    if m["points"]:
        why = ("本场没有已生成总结" if not m["has_summary"]
               else "总结里没有可用的要点")
        if m["source"] == "高光":
            lines.append("**关键片段**（%s，按场面挑选）" % why)
        elif m["source"] == "原句":
            lines.append("**关键内容**（%s，以下为原句摘录）" % why)
        else:
            lines.append("**关键内容**")
        lines += [_point_line(p) for p in m["points"]]
    else:
        lines.append("**关键内容**：这场还没有可用的总结或高光片段。")
    return lines


# ---------------------------------------------------------------- 主线叙事

_PROMPT = """你在给一档中文直播的团队编辑一期周报。输入是一周里各场直播的要点、
高光片段和话题；你要做的是跨场编辑，不是把每场总结再列一遍。

输入：
- range: 日期范围；stats: 场次、总时长、天数、高频话题；
- sessions: 每场的标题、日期、时长；
- evidence: 可引用的原话与要点，每条有 id（整数）、场次、说话人、时间、原文。

输出严格 JSON（不要 markdown 代码块）：
{"lead":"一到两句话，不超过80字",
 "storylines":[{"title":"12~18个汉字的判断句","body":"80~140字","evidence_ids":[0,1]}],
 "quotes":[{"evidence_id":2},{"evidence_id":5}],
 "questions":["还没答案的问题，不超过30字"]}

硬性规则：
1. 3~5 条主线。主线必须是跨场出现的判断、变化或矛盾，不是「某场说了什么」；
   title 不能是「薪资与谈判」「副业与变现」这种话题标签，要写成一句有信息的话；
2. 每条主线至少引用 2 条 evidence，且尽量来自不同场次；body 只能用 evidence 支持的内容；
3. 不许编造 evidence 里没有的人物、数字、公司、结论；提到数字时，数字必须能在被引用的原文里找到；
4. quotes 选 3 条最像「金句」的原话，只给 evidence_id，不要自己改写；
5. questions 只写材料里确实没有答案的问题，没有就返回空数组；
6. 不要「本期」「这周直播」「干货满满」这类空话。
"""


def _json_obj(text: str) -> dict:
    s = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        data = json.loads(s)
    except ValueError:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _stats_pack(stats: dict) -> dict:
    return {
        "sessions": stats["sessions"],
        "days": stats["days"],
        "duration": duration_text(stats["duration_s"]),
        "topics": [[t, len(ms)] for t, ms in (stats.get("topics") or [])],
    }


def _evidence_pool(materials: list[dict]) -> list[dict]:
    """把所有场次的要点和高光压成一份可引用的证据池。"""
    pool: list[dict] = []
    seen: set[str] = set()
    for m in materials:
        topics = [t for t in (m.get("topics") or []) if t not in _GENERIC_TOPICS]
        items = [("要点", p) for p in m.get("points") or []]
        items += [("高光", s) for s in m.get("scenes") or []]
        for kind, p in items:
            quote = _clean(p.get("quote"), MAX_QUOTE)
            if not quote:
                continue
            key = _norm(quote)[:80]
            if key in seen:
                continue
            seen.add(key)
            pool.append({
                "id": len(pool),
                "session_id": m["session_id"],
                "session": m["title"],
                "day": m["day"].isoformat() if m.get("day") else "",
                "kind": kind,
                "source": m.get("source") or "",
                "topics": topics,
                "topic": topics[0] if topics else "",
                "title": _clean(p.get("title"), 40),
                "quote": quote,
                "speaker": _clean(p.get("speaker"), 24),
                "t_from": float(p.get("t_from") or 0),
                "t_to": p.get("t_to"),
                "tag": _clean(p.get("tag"), 8),
            })
    return pool


_SOURCE_RANK = {"总结": 0, "高光": 1, "原句": 2}


def _rank(e: dict) -> tuple:
    return (_SOURCE_RANK.get(e.get("source") or "", 9),
            -min(90, len(e.get("quote") or "")), e.get("t_from") or 0)


def _pick_evidence(items: list[dict], limit: int = 3) -> list[dict]:
    """先保证每条主线里尽量有不同的场次，再按来源/原话长度补足。"""
    out: list[dict] = []
    seen: set[str] = set()
    for e in sorted(items, key=_rank):
        if e["session_id"] in seen:
            continue
        out.append(e)
        seen.add(e["session_id"])
        if len(out) >= limit:
            return out
    for e in sorted(items, key=_rank):
        if e not in out:
            out.append(e)
            if len(out) >= limit:
                break
    return out


def _golden_ok(e: dict) -> bool:
    q = str(e.get("quote") or "").rstrip()
    # ASR 常把问号吃掉，句尾的「吗/呢」也可当问句。
    return not re.search(r"[？?]$|[吗呢]$", q)


def _pick_quotes(pool: list[dict], limit: int) -> list[dict]:
    pool = [e for e in pool if _golden_ok(e)] or pool
    out: list[dict] = []
    seen: set[str] = set()
    for e in sorted(pool, key=_rank):
        if e["session_id"] in seen:
            continue
        out.append(e)
        seen.add(e["session_id"])
        if len(out) >= limit:
            return out
    for e in sorted(pool, key=_rank):
        if e not in out:
            out.append(e)
            if len(out) >= limit:
                break
    return out[:limit]


def _questions_from_pool(pool: list[dict]) -> list[str]:
    rx = re.compile(r"[？?]|(?:吗|呢|怎么办|要不要|如何|怎么|是不是)")
    out: list[str] = []
    for e in pool:
        if not rx.search(e["quote"]):
            continue
        q = _clean(e["quote"], 40)
        if 6 <= len(q) <= 40 and q not in out:
            out.append(q)
        if len(out) >= 3:
            break
    return out


def _fallback_narrative(materials: list[dict], pool: list[dict], stats: dict,
                        reason: str = "") -> dict:
    groups: dict[str, list[dict]] = {}
    for e in pool:
        for t in e.get("topics") or []:
            if t in _GENERIC_TOPICS:
                continue
            groups.setdefault(t, []).append(e)

    storylines: list[dict] = []
    used: set[int] = set()
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        items = [e for e in items if e["id"] not in used]
        sessions = {e["session_id"] for e in items}
        if len(sessions) < 2:
            continue
        ev = _pick_evidence(items, 3)
        if len(ev) < 2:
            continue
        for e in ev:
            used.add(e["id"])
        # 兜底也要像编辑写的：用组里最具体的一句做标题，而不是「话题被提到」。
        claim = _clean(ev[0].get("title"), 22)
        storylines.append({
            "title": _clean("%s：%s" % (key, claim), 30) if claim
            else "这周反复聊到「%s」" % key,
            "body": ("这周有 %d 场都聊到了「%s」。观点散在各场里，"
                     "下面挑出几条能点回原话的。" % (len(sessions), key)),
            "evidence": ev,
        })
        if len(storylines) >= MAX_STORYLINES:
            break
    if not storylines:
        storylines = [{
            "title": "本周最值得回看的三段",
            "body": "这一周的内容比较分散，没有形成连续主线；先看这三段，能快速抓到这周的状态。",
            "evidence": _pick_evidence(pool, 3),
        }]
    used = {e["id"] for s in storylines for e in s["evidence"]}
    quote_pool = [e for e in pool if e["id"] not in used] or pool
    quotes = _pick_quotes(quote_pool, MAX_GOLDEN_QUOTES)
    quoted = {_norm(e["quote"]) for e in quotes}
    questions = [q for q in _questions_from_pool(pool)
                 if _norm(q) not in quoted]
    return {
        "lead": "本周 %d 场、%s。下面按 %d 条主线整理，每条都能点回原话。" % (
            stats["sessions"], duration_text(stats["duration_s"]), len(storylines)),
        "storylines": storylines,
        "quotes": quotes,
        "questions": questions,
        "engine": "rules", "reason": reason,
    }


def _gate_narrative(data: dict, pool: list[dict], stats: dict,
                    since: date, until: date) -> dict:
    """证据闸门：主线必须能落到 2 条原话、跨场、数字不超纲。"""
    if not isinstance(data, dict):
        return {}
    by_id = {e["id"]: e for e in pool}
    allowed = _nums(range_label(since, until))
    allowed |= _nums(json.dumps(_stats_pack(stats), ensure_ascii=False))
    # 小整数多是「三条主线」这类结构性数字，不该按编造处理。
    allowed |= {str(i) for i in range(0, 11)}
    for e in pool:
        allowed |= _nums(e["quote"]) | _nums(e["title"])
    n_sessions = stats["sessions"]

    storylines: list[dict] = []
    for s in data.get("storylines") or []:
        if not isinstance(s, dict):
            continue
        title = _clean(s.get("title"), 30)
        body = _clean(s.get("body"), 220)
        if not title or not body:
            continue
        ids: list[int] = []
        for x in s.get("evidence_ids") or []:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i in by_id and i not in ids:
                ids.append(i)
        ev = [by_id[i] for i in ids]
        if len(ev) < 2:
            continue
        # 多场周报不能只引用同一场：那就还是"某场说了什么"。
        if n_sessions >= 2 and len({e["session_id"] for e in ev}) < 2:
            continue
        if _nums(title + body) - allowed:
            continue
        storylines.append({"title": title, "body": body, "evidence": ev})
        if len(storylines) >= MAX_STORYLINES:
            break
    if not storylines:
        return {}

    quotes: list[dict] = []
    for q in data.get("quotes") or []:
        try:
            i = int(q.get("evidence_id") if isinstance(q, dict) else q)
        except (TypeError, ValueError):
            continue
        ev = by_id.get(i)
        if ev and ev not in quotes:
            quotes.append(ev)
    # 「金句」读到问句会泄气；有陈述句就先用陈述句。
    quotes = [e for e in quotes if _golden_ok(e)] or quotes

    questions: list[str] = []
    for q in data.get("questions") or []:
        q = _clean(q, 40)
        if 4 <= len(q) <= 40 and not (_nums(q) - allowed):
            questions.append(q)
    # 已经当金句展示过的原话，不要再当「还没答案的问题」重复一遍。
    quoted = {_norm(e["quote"]) for e in quotes}
    questions = [q for q in questions if _norm(q) not in quoted]

    return {"lead": _clean(data.get("lead"), 100),
            "storylines": storylines,
            "quotes": quotes[:MAX_GOLDEN_QUOTES],
            "questions": questions[:3], "engine": "llm"}


def _llm_narrative(materials: list[dict], pool: list[dict], stats: dict,
                   since: date, until: date) -> dict:
    cfg = settings.resolve()
    if not cfg["configured"] or not pool:
        return {}
    packed = {
        "range": range_label(since, until),
        "stats": _stats_pack(stats),
        "sessions": [{"title": m["title"], "day": _day_label(m["day"]),
                      "duration": duration_text(m["duration_s"]),
                      "topics": m["topics"][:4]} for m in materials],
        "evidence": [{"id": e["id"], "session": e["session"], "kind": e["kind"],
                      "title": e["title"], "speaker": e["speaker"],
                      "t": timefmt.offset(e["t_from"]), "quote": e["quote"]}
                     for e in pool],
    }
    content = llm.chat([
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": json.dumps(packed, ensure_ascii=False)},
    ], temperature=0.4, timeout=120, max_tokens=1600, task="report")
    return _gate_narrative(_json_obj(content), pool, stats, since, until)


def _narrative(materials: list[dict], pool: list[dict], stats: dict,
               since: date, until: date, use_llm: bool = True) -> dict:
    if not pool:
        return {"lead": "这一周没有可用的发言内容。", "storylines": [],
                "quotes": [], "questions": [], "engine": "rules",
                "reason": "没有可用发言"}
    if not use_llm:
        return _fallback_narrative(materials, pool, stats, "未启用模型")
    cfg = settings.resolve()
    if not cfg["configured"]:
        return _fallback_narrative(materials, pool, stats, "未配置大模型")
    try:
        got = _llm_narrative(materials, pool, stats, since, until)
        if got:
            return got
        reason = "模型结果未过证据闸门（证据不足或数字对不上）"
    except Exception as e:                   # 网络/预算/格式失败：回退规则版
        reason = "模型调用失败：%s" % (str(e)[:80] or type(e).__name__)
    return _fallback_narrative(materials, pool, stats, reason)


def _who_tail(ev: dict, with_session: bool = True) -> str:
    who = _clean(ev.get("speaker"), 24)
    sess = _clean(ev.get("session"), 80)
    t = timefmt.offset(float(ev.get("t_from") or 0))
    parts = [who]
    if with_session and sess:
        parts.append("《%s》" % sess)
    parts.append(t)
    parts = [p for p in parts if p]
    return (" —— " + " · ".join(parts)) if parts else ""


def _evidence_line(ev: dict) -> str:
    return "- 「%s」%s" % (_clean(ev.get("quote"), MAX_QUOTE), _who_tail(ev))


def _render_narrative(materials: list[dict], since: date, until: date,
                      stats: dict, narrative: dict) -> str:
    lines = [
        "# %s · %s" % (KINDS["weekly"], range_label(since, until)), "",
        "> %s，共 %d 场、%s。" % (
            _date_span(since, until), stats["sessions"],
            duration_text(stats["duration_s"])), "",
    ]
    lead = _clean(narrative.get("lead"), 160)
    if not lead:
        lead = "本周 %d 场、%s。" % (stats["sessions"],
                                     duration_text(stats["duration_s"]))
    lines += [lead, ""]

    lines += ["## 本周主线", ""]
    for i, s in enumerate(narrative.get("storylines") or []):
        num = _CN_NUMS[i] if i < len(_CN_NUMS) else str(i + 1)
        lines += ["### %s、%s" % (num, s["title"]), "", s["body"], ""]
        for ev in s.get("evidence") or []:
            lines.append(_evidence_line(ev))
        lines.append("")

    quotes = narrative.get("quotes") or []
    if quotes:
        lines += ["## 本周金句", ""]
        for i, ev in enumerate(quotes, 1):
            lines.append("%d. 「%s」%s" % (
                i, _clean(ev.get("quote"), MAX_QUOTE),
                _who_tail(ev, with_session=True)))
        lines.append("")

    questions = narrative.get("questions") or []
    if questions:
        lines += ["## 还没答案的问题", ""]
        lines += ["- " + q for q in questions]
        lines.append("")

    lines += ["## 场次与回放", ""]
    for m in materials:
        tail = " · [妙记](%s)" % m["minutes_url"] if m["minutes_url"] else ""
        topics = "、".join(m["topics"][:3])
        topic_tail = (" · " + topics) if topics else ""
        lines.append("- %s · 《%s》 · %s%s%s" % (
            _day_label(m["day"]), m["title"], duration_text(m["duration_s"]),
            topic_tail, tail))

    lines += ["", "## 本周数字", "",
              "- 场次：%d 场，分布在 %d 天" % (stats["sessions"], stats["days"]),
              "- 总时长：%s（平均每场 %s）" % (
                  duration_text(stats["duration_s"]),
                  duration_text(stats["duration_s"] / max(1, stats["sessions"]))),
              "- 素材：%d 场有已生成总结，%d 场有妙记回放" % (
                  stats["with_summary"], stats["with_minutes"]),
              "- 高频话题：%s" % _topic_summary(stats["topics"])]
    cov = [ln for ln in _coverage_lines(stats)
           if ln.startswith("- 未生成总结") or ln.startswith("- 未回填妙记")]
    if cov:
        lines += cov
    return "\n".join(lines).rstrip() + "\n"


def render_weekly(materials: list[dict], since: date, until: date,
                  stats: dict, narrative: dict | None = None) -> str:
    if narrative is None:
        narrative = _fallback_narrative(materials, _evidence_pool(materials), stats)
    return _render_narrative(materials, since, until, stats, narrative)


def render_retro(materials: list[dict], since: date, until: date,
                 stats: dict) -> str:
    title = "%s · %s" % (KINDS["retro"], range_label(since, until))
    lines = [
        "# " + title, "",
        "> %s，共 %d 场、%s。复盘模板：事实部分自动汇总，结论与行动留给人填。" % (
            _date_span(since, until), stats["sessions"],
            duration_text(stats["duration_s"])),
        "", "## 一、数据面", "",
        "- 场次：%d 场，分布在 %d 天" % (stats["sessions"], stats["days"]),
        "- 总时长：%s（平均每场 %s）" % (
            duration_text(stats["duration_s"]),
            duration_text(stats["duration_s"] / max(1, stats["sessions"]))),
        "- 素材：%d 场有已生成总结，%d 场有妙记回放" % (
            stats["with_summary"], stats["with_minutes"]),
        "- 高频话题：%s" % _topic_summary(stats["topics"]),
    ]
    cov = _coverage_lines(stats)
    if cov:
        lines += cov
    lines += ["", "## 二、逐场回顾", ""]
    for d, group in _group_days(materials).items():
        lines += ["### " + _day_label(d), ""]
        shown = [m for m in group if m.get("detail")]
        rest = [m for m in group if not m.get("detail")]
        for m in shown:
            lines += _session_head(m) + _session_body(m) + [""]
        if rest:
            lines += [_short_line(rest), ""]
    lines += ["## 三、跨场观察", "",
              "- 重复话题：%s" % _topic_summary(stats["topics"]),
              "- 场次分布：%s" % "、".join(
                  "%s %d 场" % (_day_label(d), len(g))
                  for d, g in _group_days(materials).items()),
              "", "## 四、结论与行动（待人工确认）", "",
              "### 效果好的", "", "- （待填）", "",
              "### 需要改进的", "", "- （待填）", "",
              "### 下周动作", "", "- [ ] （待填）"]
    return "\n".join(lines).rstrip() + "\n"


def build(conn, kind: str = "weekly", since: date | None = None,
          until: date | None = None, out_dir: Path | str | None = None,
          write: bool = True, use_llm: bool = True) -> dict:
    """生成报告，返回 {kind,title,since,until,markdown,path,sessions,stats}。

    weekly 会先做一次跨场「主线」编辑（模型可用时），失败自动回退规则版；
    retro 仍是原来的逐场模板，结论与行动留给人填。
    """
    kind = (kind or "weekly").lower()
    if kind not in KINDS:
        raise ValueError("未知报告类型：%s" % kind)
    today = date.today()
    since = since or (today - timedelta(days=6))
    until = until or today
    if since > until:
        since, until = until, since
    sessions = sessions_between(conn, since, until)
    materials = [_material(conn, s) for s in sessions]
    for m in materials:
        m["detail"] = bool(m["has_summary"] or m["duration_s"] >= DETAIL_MIN_S)
        m["compact_reason"] = "" if m["detail"] else "short"
    # 重点场次太多时按「有总结优先、时长次之」保留，其余只列标题；
    # 这样不会像「只取最近 N 场」那样把前几天整个丢掉。
    detail = [m for m in materials if m["detail"]]
    truncated = max(0, len(detail) - MAX_SESSIONS)
    if truncated:
        keep = {m["session_id"] for m in sorted(
            detail, key=lambda m: (m["has_summary"], m["duration_s"]),
            reverse=True)[:MAX_SESSIONS]}
        for m in detail:
            if m["session_id"] not in keep:
                m["detail"] = False
                m["compact_reason"] = "cap"
    stats = _material_stats(materials)
    stats["truncated"] = truncated
    narrative = None
    if kind == "weekly":
        pool = _evidence_pool(materials)
        narrative = _narrative(materials, pool, stats, since, until,
                               use_llm=use_llm)
        markdown = _render_narrative(materials, since, until, stats, narrative)
    else:
        markdown = render_retro(materials, since, until, stats)
    path = None
    if write:
        out = Path(out_dir) if out_dir else REPORTS_DIR
        out.mkdir(parents=True, exist_ok=True)
        path = out / ("%s_%s_%s.md" % (kind, since.isoformat(), until.isoformat()))
        path.write_text(markdown, encoding="utf-8")
    return {
        "kind": kind,
        "title": "%s · %s" % (KINDS[kind], range_label(since, until)),
        "since": since, "until": until,
        "markdown": markdown, "path": path, "narrative": narrative,
        "sessions": materials, "stats": stats,
    }


def cleanup(days: int = REPORT_RETENTION_DAYS) -> int:
    """清掉过期报告；报告是中间产物，需要长期留存时复制到别处。"""
    if not REPORTS_DIR.is_dir():
        return 0
    cutoff = time.time() - max(1, int(days)) * 86400
    n = 0
    for p in REPORTS_DIR.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n
