# -*- coding: utf-8 -*-
"""低价值识别、四维总结、以及「总结必须能点回原文」的证据闸门。

设计红线：任何一条总结要点，如果没有绑定到真实存在的 utterance id，
就直接丢弃，不允许模型凭空生成一句看起来像结论的话。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import identity, llm, scheduler, settings, shape, store, titles
from .config import LLM_SUMMARY_MAX_ROWS, LLM_SUMMARY_TOTAL_TIMEOUT
from .models import CATEGORY_META, category_label, fmt_ts

# ---------------------------------------------------------------- 分类 / 降权

AD_HINTS = ["加微信", "加个微信", "私信", "小黄车", "下单", "优惠券", "福利", "扣1", "扣个1",
            "报价", "代做", "代购", "报名", "扫码", "vx", "威信", "链接", "带货", "团购",
            "立减", "试听", "课程", "关注一下", "点个关注", "走平台"]
HOST_HINTS = ["上麦", "下一位", "麦序", "有请", "连一下", "把麦", "欢迎来到", "咱们今天",
              "扣个", "扣 1", "稍等啊", "没点关注", "开麦", "下麦", "家人们", "排一下队"]
QUESTION_HINTS = ["请问", "想问", "问一下", "怎么办", "能不能", "有没有", "该不该", "要不要",
                  "求助", "咨询", "适合吗", "怎么", "吗？", "吗?", "能连麦", "老师我"]
CHITCHAT_HINTS = ["哈哈", "在吗", "吃了吗", "晚上好", "早上好", "无聊", "点歌", "唱首",
                  "么么哒", "来啦", "打卡", "水一下", "困了", "好冷清", "有人吗", "饿死",
                  "声音好听", "默默", "路过", "嘿嘿"]
CONCLUSION_HINTS = ["建议", "因为", "所以", "结论", "重点", "第一", "第二", "注意", "别",
                    "先", "一定要", "关键", "数据", "复盘", "风险", "其实"]

TOPIC_RULES = [
    ("职场跳槽与面试", ["跳槽", "离职", "offer", "Offer", "面试", "简历", "入职", "试用期", "汇报线", "外包"]),
    ("薪资与谈判",     ["薪资", "涨薪", "工资", "谈薪", "期望薪资", "预算区间", "劝退", "赔偿"]),
    ("情感与关系",     ["分手", "对象", "恋爱", "相亲", "结婚", "冷暴力", "相处", "关系", "异地"]),
    ("副业与变现",     ["副业", "变现", "接单", "自媒体", "选题", "完播", "账号", "限流", "违规", "抽成"]),
    ("租房与生活",     ["租房", "房租", "房东", "押金", "合同", "搬家", "通勤"]),
]

FILLER_RE = re.compile(r"^(嗯|啊|哦|对|就是|然后|那个|吧|哈+)[\s，。、！~]*$")

# 改提示词/改编码格式/改送模型的句子范围就 +1，否则旧总结会被指纹误判为"还能用"。
# v3：长直播改为按时间分层抽样（不再只取前 350 句），旧缓存必须作废。
# v4：理解段开始要求 claim_speaker / stance（观点归属），旧要点没有归属，必须重跑。
SUMMARY_PROMPT_VERSION = "v4"


def _pack_rows(rows: list[dict], char_limit: int = 200,
               with_topic: bool = True, with_cat: bool = True) -> dict:
    """把逐句压成"表 + 位置数组"，省掉每行重复的 JSON 键名。

    实测一场 321 句：原来的对象数组序列化 38,554 字符，其中原文只占 6,071；
    换成下表 + 数组后 13,000 字符出头（-66%），而模型要读的信息一条没少。

    u 的每一项：[id, 起始秒, 说话人下标, (类别下标), (主题下标), 原文]

    with_cat=False 用于「理解段」：那一步发生在标注之前，逐句类别还是关键词规则给的，
    喂给模型既占 token 又误导它，不如只给说话人和原文。
    """
    spk: list[str] = []
    cat: list[str] = []
    top: list[str] = []
    spk_i: dict[str, int] = {}
    cat_i: dict[str, int] = {}
    top_i: dict[str, int] = {}
    u: list[list] = []
    for r in rows:
        s = str(r.get("speaker_label") or "")
        c = str(r.get("category") or "")
        t = str(r.get("topic") or "") if with_topic else ""
        if s not in spk_i:
            spk_i[s] = len(spk); spk.append(s)
        if with_cat and c not in cat_i:
            cat_i[c] = len(cat); cat.append(c)
        if with_topic and t not in top_i:
            top_i[t] = len(top); top.append(t)
        item = [r["id"], round(float(r.get("t_start") or 0.0), 1), spk_i[s]]
        if with_cat:
            item.append(cat_i[c])
        if with_topic:
            item.append(top_i[t])
        item.append(str(r.get("text") or "")[:char_limit])
        u.append(item)
    out: dict[str, Any] = {"spk": spk}
    if with_cat:
        out["cat"] = cat
    if with_topic:
        out["top"] = top
    out["u"] = u
    return out


def _even_pick(items: list[dict], n: int) -> list[dict]:
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    step = len(items) / float(n)
    return [items[min(len(items) - 1, int(i * step))] for i in range(n)]


def sample_rows(rows: list[dict], max_rows: int) -> list[dict]:
    """长直播分层抽样：按 10 分钟分桶均匀取，再保留最后 30 句。

    为什么不是 rows[:max_rows]：那样只覆盖开头，长直播后半场根本没进模型，
    总结自然"只总结了前半场"。按时间分桶能保证首尾都在，调用次数却不变。
    """
    max_rows = max(30, int(max_rows))
    if len(rows) <= max_rows:
        return list(rows)
    latest = rows[-30:]
    pool = rows[:-30]
    remaining = max(0, max_rows - len(latest))
    buckets: dict[int, list[dict]] = {}
    for r in pool:
        buckets.setdefault(int(float(r.get("t_start") or 0) // 600), []).append(r)
    per = max(1, remaining // max(1, len(buckets)))
    picked: list[dict] = []
    for b in buckets.values():
        picked.extend(_even_pick(b, min(per, len(b))))
    picked = list({int(r["id"]): r for r in picked}.values())
    if len(picked) < remaining:
        chosen = {int(r["id"]) for r in picked}
        rest = [r for r in pool if int(r["id"]) not in chosen]
        picked.extend(_even_pick(rest, remaining - len(picked)))
    out = list({int(r["id"]): r for r in picked + latest}.values())
    return sorted(out, key=lambda r: (float(r.get("t_start") or 0), int(r["id"])))


def classify(text: str, overlap: bool = False, asr_conf: float = 1.0) -> dict[str, Any]:
    """返回分类、价值档、重要度、话题、降权原因。规则优先，可替换为 LLM 分类器。"""
    t = (text or "").strip()
    if overlap or asr_conf < 0.45 or not t:
        return dict(category="chitchat", value_tag="low", importance=0.0,
                    topic="", low_value_reason="多人重叠 / ASR 置信度过低，不纳入总结")
    hits = lambda words: [w for w in words if w in t]  # noqa: E731
    ad, host, q, chat = hits(AD_HINTS), hits(HOST_HINTS), hits(QUESTION_HINTS), hits(CHITCHAT_HINTS)
    if ad:
        cat, reason = "ad", "广告/引流关键词：" + "、".join(ad[:3])
    elif host and len(t) <= 40:
        cat, reason = "host_control", "主持控场话术，无实质信息"
    elif FILLER_RE.match(t) or (len(t) <= 6 and not q):
        cat, reason = "chitchat", "无信息量短句/语气词"
    elif chat and len(t) <= 24 and not q:
        cat, reason = "chitchat", "寒暄闲聊：" + "、".join(chat[:2])
    elif q and len(t) <= 60:
        cat, reason = "question_consult", "提问类发言，作为话题锚点保留、总结时降权"
    else:
        cat, reason = "substantive", ""

    value_tag = CATEGORY_META[cat][2]
    base = {"high": 0.72, "normal": 0.45, "low": 0.12}[value_tag]
    importance = base + min(0.18, len(t) / 400) + 0.10 * bool(hits(CONCLUSION_HINTS))
    if cat == "ad":
        importance = min(importance, 0.18)
    if cat == "chitchat":
        importance = min(importance, 0.15)

    topic = ""
    best = 0
    for name, kws in TOPIC_RULES:
        n = sum(1 for k in kws if k in t)
        if n > best:
            best, topic = n, name
    return dict(category=cat, value_tag=value_tag,
                importance=round(min(1.0, importance), 3), topic=topic or "综合/未归类",
                low_value_reason=reason)


# ---------------------------------------------------------------- 作用域

@dataclass
class Scope:
    session: dict
    filters: dict
    mode: str
    all_rows: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)


def session_digest(conn: sqlite3.Connection, sid: str) -> str:
    """本场内容的稳定摘要，用来判断「同一份稿子」是否变过。

    取所有会进模型输入的字段：说话人 id/显示名、分类、价值档、重叠标记、
    置信度、话题、原文。改名、改分类、改话题、文本变化都会让摘要变，
    重复点击才会重新生成；单纯刷新页面不会。
    """
    h = hashlib.sha1()
    h.update(("v2|" + str(sid or "") + "|").encode("utf-8"))
    for r in conn.execute(
            "SELECT id, speaker_id, speaker_label, category, value_tag, overlap, "
            "asr_conf, topic, text FROM utterances WHERE session_id=? ORDER BY id",
            (sid,)):
        h.update(("%s|%s|%s|%s|%s|%s|%s|%s|%s;" % tuple(r)).encode("utf-8"))
    return h.hexdigest()[:20]


def _scope_sid(scope: "Scope") -> str:
    return str(scope.session.get("id") or scope.filters.get("session_id") or "")


def _wall(session: dict, t: float) -> str:
    try:
        dt = datetime.fromisoformat(session["started_at"]) + timedelta(seconds=t)
        return dt.strftime("%H:%M:%S")
    except (ValueError, KeyError, TypeError):
        return fmt_ts(t)


def build_scope(conn: sqlite3.Connection, sid: str, filters: dict, mode: str) -> Scope:
    session = store.get_session(conn, sid) or {}
    base = dict(filters)
    base.update(session_id=sid, exclude_low_value=False, exclude_overlap=False, min_conf=None)
    all_rows = store.query_utterances(conn, base, limit=20000)

    final = dict(filters)
    final.update(session_id=sid,
                 exclude_low_value=bool(filters.get("exclude_low_value", True)),
                 exclude_overlap=True, min_conf=0.45)
    rows = store.query_utterances(conn, final, limit=20000)

    by_cat: dict[str, int] = {}
    overlap_s = 0.0
    low_conf = 0
    for r in all_rows:
        if r["overlap"]:
            overlap_s += max(0.0, r["t_end"] - r["t_start"])
        elif r["asr_conf"] < 0.45:
            low_conf += 1
        elif r["value_tag"] == "low" and final["exclude_low_value"]:
            by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1

    untranscribed = float(session.get("untranscribed_seconds") or 0)
    coverage = {
        "matched": len(all_rows),
        "included": len(rows),
        "excluded_low_value": by_cat,
        "excluded_low_value_total": sum(by_cat.values()),
        "excluded_overlap_s": round(overlap_s, 1),
        "excluded_low_conf": low_conf,
        "untranscribed_s_whole_session": untranscribed,
        "coverage_ratio": round(len(rows) / len(all_rows), 3) if all_rows else 0.0,
    }

    caveats: list[str] = []
    if session.get("source_mode") == "mixed":
        caveats.append(
            "本场为混音兜底：说话人是聚类标签而非实名，重叠语音已剔除；"
            "总结只覆盖可转写片段，不代表全场完整内容。")
    if session.get("source_mode") == "demo":
        caveats.append("当前为演示数据，仅用于验证检索/总结/回溯链路，不是真实采集结果。")
    if overlap_s > 0:
        caveats.append(f"当前范围内有 {overlap_s:.0f} 秒重叠语音未转写，相关发言不参与结论。")
    if untranscribed > 0:
        caveats.append(f"整场另有约 {untranscribed / 60:.0f} 分钟无人声/未转写区间（冷场、BGM、丢帧）。")
    if all_rows and len(rows) / len(all_rows) < 0.5:
        caveats.append("当前筛选剔除了过半发言，结论可能偏向高价值片段，请结合原文复核。")
    if not rows:
        caveats.append("当前条件下没有可用发言，无法生成有证据支撑的总结。")

    w = session.get("warnings")
    if isinstance(w, str):
        try:
            w = json.loads(w)
        except json.JSONDecodeError:
            w = []
    for item in w or []:
        caveats.append("⚠ " + str(item))

    return Scope(session=session, filters=final, mode=mode,
                 all_rows=all_rows, rows=rows, coverage=coverage, caveats=caveats)


# ---------------------------------------------------------------- 总结

def _evidence(row: dict, scope: Scope) -> dict:
    return {
        "id": row["id"],
        "t_start": row["t_start"], "t_end": row["t_end"],
        "timecode": fmt_ts(row["t_start"]),
        "wall_time": _wall(scope.session, row["t_start"]),
        "speaker_id": row["speaker_id"], "speaker_label": row["speaker_label"],
        "category": row["category"], "topic": row["topic"],
        "quote": row["text"][:90],
        "audio_url": f"/api/audio?session_id={scope.session.get('id','')}"
                     f"&t_start={row['t_start']:.1f}&t_end={row['t_end']:.1f}",
    }


_TITLE_RULES = """
另外顺手给这场直播起个名字（不额外调用模型，就在这次输出里）：
- 6~16 个汉字，名词短语或判断句；不要疑问句、不要问号；
- 不写日期、时间、人数（系统已有，写了就重复）；
- 不用「本期/这期/今天/这场/直播/分享/干货/复盘」开头；
- 不加引号、书名号、句末标点；
- 必须能和别的场次区分：写清「谁 × 什么主题 × 什么场景」，不要只写主题；
- title_alts 给 2 个角度不同的备选（主题+对象 / 结论 / 场景冲突）；
- 全是闲聊、广告、控场、没有可命名内容时，title 写 null。"""


_LLM_GROUP_PROMPT = """你在整理一份多人语音直播的可检索纪要，按类别分组。
下面的发言已经按类别归好。请为每个类别分别提炼要点。

输入格式（为了省 token 用了紧凑编码，字段含义如下）：
- cat: 类别 key 表，输出里的 category 直接写这里的字符串；
- u: 逐句数组，每项 = [id, 类别下标, 原文]，类别下标指向 cat 表。

类别含义：
- substantive 实质讨论（观点、经验、方案、业务细节）
- question_consult 上麦咨询（提问、求助、咨询）
- host_control 主持控场（点名、欢迎、排麦、过渡）
- chitchat 闲聊（寒暄、附和）
- ad 广告引流（导流、报价、加微信）

输出严格 JSON（不要 markdown 代码块）：
{"title":"这场直播的名字","title_alts":["备选名1","备选名2"],"overview":"一段话概述，不超过120字","groups":[{"category":"substantive","points":[{"title":"不超过20字","gist":"不超过100字","evidence_ids":[整数id,整数id]}]}]}

硬性规则：
1. 每个类别最多 2 个 point；没有信息量的类别可以整组不出现。
2. evidence_ids 必须来自**该类别下**的发言，每个 point 至少 2 个。
3. gist 必须由这些原文直接支持，禁止编造因果、数字或结论。
""" + _TITLE_RULES


def _llm_groups(scope: Scope, usage: dict | None = None,
                errors: list | None = None) -> tuple[str, list[dict], Any, list] | None:
    """按类别分组的 LLM 要点：每个类别单独出点，证据必须落在该类别的发言里。"""
    cfg = settings.resolve()
    if not cfg["configured"]:
        return None
    by_cat: dict[str, list[dict]] = {}
    for r in sample_rows(scope.rows, LLM_SUMMARY_MAX_ROWS):
        by_cat.setdefault(r["category"], []).append(r)
    cats = [c for c in by_cat if len(by_cat[c]) >= 2]
    if not cats:
        return None
    by_id = {r["id"]: r for c in cats for r in by_cat[c]}
    cat_of = {r["id"]: c for c in cats for r in by_cat[c]}
    cat_i = {c: i for i, c in enumerate(cats)}
    try:
        content = llm.chat([
            {"role": "system", "content": _LLM_GROUP_PROMPT},
            {"role": "user", "content": json.dumps({
                "cat": cats,
                "u": [[r["id"], cat_i[r["category"]], r["text"][:200]]
                      for c in cats for r in by_cat[c][:120]],
            }, ensure_ascii=False)},
        ], temperature=0.2, usage=usage,
            session_id=_scope_sid(scope), task="summary",
            total_timeout=LLM_SUMMARY_TOTAL_TIMEOUT)
        content = re.sub(r"^(" + chr(96) * 3 + r")(?:json)?|(" + chr(96) * 3 + r")$", "",
                         content.strip(), flags=re.M).strip()
        data = json.loads(content)
    except Exception as e:
        if errors is not None:
            errors.append(e)
        return None

    groups: list[dict] = []
    for g in data.get("groups", [])[:6]:
        cat = str(g.get("category") or "")
        if cat not in by_cat:
            continue
        pts: list[dict] = []
        for p in (g.get("points") or [])[:2]:
            ids = [int(i) for i in p.get("evidence_ids", [])
                   if str(i).isdigit() and int(i) in by_id and cat_of.get(int(i)) == cat]
            ev = [_evidence(by_id[i], scope) for i in ids]
            if len(ev) < 2:      # 证据闸门：必须落在同一类别，且至少 2 条
                continue
            pts.append({
                "title": str(p.get("title", "未命名"))[:40],
                "gist": str(p.get("gist", ""))[:300],
                "t_from": min(e["t_start"] for e in ev), "t_to": max(e["t_end"] for e in ev),
                "importance": 0.0, "utterance_count": len(ev),
                "speakers": sorted({e["speaker_label"] for e in ev}), "evidence": ev,
            })
        if pts:
            groups.append({"category": cat, "label": category_label(cat),
                           "count": len(by_cat[cat]),
                           "points": store.sort_summary_points(pts)})
    if not groups:
        return None
    return (str(data.get("overview", ""))[:400], groups,
            data.get("title"), data.get("title_alts") or [])


_LLM_PROMPT = """你在整理一份多人语音直播的可检索纪要。只允许使用下面给出的原始发言，禁止补充外部知识、禁止推测。
这一步是「理解整场」，同时还要定下这场直播的主题词表，后面给每句话打标签只能从这套词里选。

输入格式（为了省 token 用了紧凑编码，字段含义如下）：
- spk: 说话人表；u: 逐句数组，每项 = [id, 起始秒, 说话人下标, 原文]。
  id 是稳定标识，evidence_ids 里必须用第一列的 id，不要用行号。

输出严格 JSON（不要 markdown 代码块）：
{"title":"这场直播的名字","title_alts":["备选名1","备选名2"],"overview":"一段话概述，不超过120字",
 "topics":["主题1","主题2","主题3"],
 "host_speaker":spk 表里的下标,
 "points":[{"title":"话题名，不超过20字","gist":"讨论要点，不超过100字",
            "claim_speaker":spk 表里的下标（这条主张主要属于谁）,
            "stance":"不超过40字的立场句，只用他自己的话",
            "evidence_ids":[整数id数组，2~4个]}]}

topics 规则：
- 6~10 个，名词短语、每个不超过 6 个汉字（如「设备预算」「账号风控」「引流话术」）；
- 互相不重叠，覆盖尽量多的内容；按重要性从高到低；
- 不要「其他」「杂项」这类兜底主题；句子里找不到依据就不要硬编。
- 后面会拿这套词表给每一句归类，所以每个主题必须能落在一批具体句子上。

硬性规则：
1. evidence_ids 中的 id 必须来自给定发言；每个 point 至少 2 个 id。
2. gist 必须能由这些 id 对应的原文直接支持，不得编造因果或数字。
3. 只保留有信息量的话题；闲聊、广告、控场话术不单独成点。
4. 最多 8 个 point。
5. claim_speaker 必须是 evidence_ids 里出现过的说话人；主张是几个人共同说的就选主要那位。
6. stance 写成陈述句（他主张什么），不要写「他讲了××」；找不到就留空字符串。""" + _TITLE_RULES


# 低于这个规模，自动档不调模型（本地稿已移除）；用户可显式选「强制用大模型」。
LLM_MIN_ROWS = 15
LLM_MIN_CHARS = 400


# 有名字的人（人工改过名 / 声纹底库命中的昵称）优先归属：位置标签
# 「主播/嘉宾2/聚类标签 09」只是这一场里的占位，不配给观点署名。
# 统一用 identity.is_positional，避免这里和展示层各有一份规则、慢慢漂移。

# 展示层把位置和人名拼成「主播 · 阿哲」（store.speaker_display）：
# 真正能署名的在后半段，不能因为前缀是「主播」就把整串当占位。
_NAME_SUFFIX_RE = re.compile(r"^[^·]+·\s*(.+?)\s*(?:（疑似）)?$")


def _looks_like_name(label: str) -> bool:
    text = str(label or "").strip()
    if not text:
        return False
    m = _NAME_SUFFIX_RE.match(text)
    if m:
        text = m.group(1).strip()
        return bool(text) and not identity.is_positional(text)
    return not identity.is_positional(text)


def _claim_speaker(ev: list[dict]) -> str:
    """一条要点归给谁：证据里真名发言最多的人，没有真名才退回位置标签。

    每条证据按自己的时长计票，同名多句累加。「主播/嘉宾2」这类位置标签只是
    本场占位，有真名时一律不署名。返回 speaker_label（空 = 认不出来，
    前端显示「本场」）。
    """
    mass: dict[str, float] = {}
    named: dict[str, float] = {}
    for e in ev:
        label = str(e.get("speaker_label") or "").strip()
        if not label:
            continue
        dur = max(0.0, float(e.get("t_end") or 0.0) - float(e.get("t_start") or 0.0))
        mass[label] = mass.get(label, 0.0) + max(1.0, dur)
        if _looks_like_name(label):
            named[label] = mass[label]      # 跟着 mass 走，同名多句要累加
    # 有真名时只在真名里选：位置标签（嘉宾1/说话人2）是这一场的占位，
    # 拿它给观点署名，用户看到的是一张「嘉宾1 认为……」的卡片。
    pool = named or mass
    if not pool:
        return ""
    return max(pool, key=lambda k: pool[k])


# 口头禅开头：抽出来当「立场」只会得到「我觉得」「就是说」，等于没写。
_DISCOURSE_RE = re.compile(
    r"^(?:我个人觉得|我个人认为|我觉得|我认为|我感觉|我建议|我理解|在我看来|"
    r"其实|就是说|也就是说|换句话说|然后|但是|不过|所以|因此|而且|"
    r"首先|另外|说实话|坦白说|客观讲|对|是的)"
    r"(?:吧|啊|呀|呢|嘛|哈)?[，,、：:\s]*")


def _stance(text: str, limit: int = 40) -> str:
    """主张句：跳过口头禅，取第一个有内容的短分句，去掉语气词尾巴。

    本地兜底，不做生成式改写——原话怎么说的就怎么截，只是别把「我觉得吧」
    当成一个人的观点。
    """
    t = re.sub(r"[\s]+", " ", str(text or "")).strip().strip("“”\"'")
    if not t:
        return ""
    best = ""
    for clause in re.split(r"[，。！？；,.!?;:：]", t):
        head = clause.strip()
        for _ in range(2):                      # 「我觉得吧，就是说」这种连着的
            head = _DISCOURSE_RE.sub("", head).strip()
        head = re.sub(r"[呢吧啊呀嘛哦哈]+$", "", head).strip()
        if not head:
            continue
        if len(head) >= 6:
            return head[:limit]
        best = best or head                     # 都很短就先用第一个，别返回空
    return best[:limit]


def _attach_claims(points: list[dict]) -> None:
    """给每条要点补 claim_speaker / stance（有就不动，保护模型给的归属）。"""
    for p in points or []:
        ev = p.get("evidence") or []
        if not p.get("claim_speaker"):
            p["claim_speaker"] = _claim_speaker(ev)
        if not p.get("stance"):
            p["stance"] = _stance(str(ev[0].get("quote") or ev[0].get("text") or "")
                                  if ev else "")


def _worth_llm(rows: list[dict]) -> tuple[bool, str]:
    n = len(rows)
    chars = sum(len(r.get("text") or "") for r in rows)
    if n < LLM_MIN_ROWS or chars < LLM_MIN_CHARS:
        return False, ("内容太少（%d 条 / %d 字）；本地总结已停用，本次不生成。"
                       "需要的话可以选「强制用大模型」。" % (n, chars))
    return True, ""


def _failure_reason(errors: list) -> str:
    """把「为什么没有生成」说清楚：预算、限流、格式错误要能区分。

    措辞必须落在「本次没生成」上，不能再写「已回退本地抽取式」——那条路已经删了。
    """
    if not errors:
        return "模型没有返回可用的要点，本次没有生成总结"
    e = errors[-1]
    if isinstance(e, scheduler.BudgetExceeded):
        return "%s，本次没有生成总结" % e
    text = str(e).strip()
    return "模型调用失败（%s），本次没有生成总结" % (
        text[:160] if text else type(e).__name__)


def _clean_topics(raw: Any, limit: int = 10) -> list[str]:
    out: list[str] = []
    for t in (raw or []):
        if isinstance(t, dict):
            t = t.get("name") or t.get("topic") or ""
        t = str(t).strip().strip("「」\"'")
        if not t or t in out:
            continue
        if len(t) > 12:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _llm_points(scope: Scope, usage: dict | None = None,
                errors: list | None = None) -> tuple | None:
    """理解段（P1）：一次调用同时出「标题 / 概述 / 要点 / 主题词表 / 主讲人」。

    为什么合并到这通调用里：标题、概述、主题三件事读的是同一份内容，分开调就是
    同一段话让模型读三遍。主题顺带产出的另一个好处是——后面逐句打标签时不用再
    单独提一次主题，词表天然一致。
    """
    cfg = settings.resolve()
    if not cfg["configured"]:
        return None
    candidates = sample_rows(scope.rows, LLM_SUMMARY_MAX_ROWS)
    if not candidates:
        return None
    id_set = {r["id"] for r in candidates}
    try:
        # 这一步在标注之前跑，逐句类别还是关键词规则的，不喂给模型（省 token 也免误导）
        packed = _pack_rows(candidates, char_limit=200, with_topic=False, with_cat=False)
        packed["mode"] = scope.mode
        content = llm.chat([
            {"role": "system", "content": _LLM_PROMPT},
            {"role": "user", "content": json.dumps(packed, ensure_ascii=False)},
        ], temperature=0.2, usage=usage,
            session_id=_scope_sid(scope), task="summary",
            total_timeout=LLM_SUMMARY_TOTAL_TIMEOUT)
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
        data = json.loads(content)
        by_id = {r["id"]: r for r in candidates}
        labels = packed.get("spk") or []

        def _label_at(idx: Any) -> str:
            """模型给的说话人下标 -> 标签；越界或不是整数就当没给。"""
            try:
                i = int(idx)
            except (TypeError, ValueError):
                return ""
            return labels[i] if 0 <= i < len(labels) else ""

        points: list[dict] = []
        for p in data.get("points", [])[:8]:
            ids = [int(i) for i in p.get("evidence_ids", []) if str(i).isdigit() and int(i) in id_set]
            ev = [_evidence(by_id[i], scope) for i in ids]
            # ---- 证据闸门：无有效证据的要点一律丢弃 ----
            if len(ev) < 2:
                continue
            # 归属也要过闸门：模型说的人必须真的出现在这条要点的证据里
            who = _label_at(p.get("claim_speaker"))
            if who and who not in {e["speaker_label"] for e in ev}:
                who = ""
            points.append({
                "title": str(p.get("title", "未命名话题"))[:40],
                "gist": str(p.get("gist", ""))[:300],
                "t_from": min(e["t_start"] for e in ev),
                "t_to": max(e["t_end"] for e in ev),
                "importance": 0.0, "utterance_count": len(ev),
                "speakers": sorted({e["speaker_label"] for e in ev}),
                "claim_speaker": who,
                "stance": str(p.get("stance") or "").strip().strip("“”\"'")[:40],
                "evidence": ev,
            })
        if not points:
            return None
        _attach_claims(points)          # 模型没给或给错的那几条，本地补齐
        # 模型不保证按时间输出；面板的「要点 / 时间」都按 t_from 排，
        # 否则用户会看到 00:12 → 04:12 → 01:48 这种跳跃。
        points = store.sort_summary_points(points)
        topics = _clean_topics(data.get("topics"))
        host = ""
        want = _label_at(data.get("host_speaker"))
        if want:
            host = next((r["speaker_id"] for r in candidates
                         if r.get("speaker_label") == want), "")
        return (str(data.get("overview", ""))[:300], points,
                data.get("title"), data.get("title_alts") or [], topics, host)
    except Exception as e:                  # 网络/格式/限流都当"没有可用总结"
        if errors is not None:
            errors.append(e)
        return None


def _norm_filters(f: dict) -> dict:
    """只留真正影响结论的筛选字段，并补上默认值。

    指纹里直接扔整个 filters 会出事：/api/summarize 会补 exclude_low_value/exclude_overlap，
    /api/summarize/estimate 不补，两边算出来的指纹就不一样，UI 说"能复用"、真跑却重新调模型。
    """
    out: dict[str, Any] = {}
    out["t_from"] = None if f.get("t_from") is None else round(float(f["t_from"]), 1)
    out["t_to"] = None if f.get("t_to") is None else round(float(f["t_to"]), 1)
    out["categories"] = sorted(str(x) for x in (f.get("categories") or []))
    out["speakers"] = sorted(str(x) for x in (f.get("speakers") or []))
    out["q"] = str(f.get("q") or "").strip()
    out["exclude_low_value"] = True if f.get("exclude_low_value") is None else bool(f["exclude_low_value"])
    out["exclude_overlap"] = True if f.get("exclude_overlap") is None else bool(f["exclude_overlap"])
    return out


def _fingerprint(rows: list[dict], mode: str, filters: dict, model: str) -> str:
    """总结输入指纹：原文 + 分类/主题 + 模型 + 提示词版本 + 筛选，任何一项变了就该重算。

    为什么不只看文本：分类改过一次，同样几句原文分到的组会不同，结论也该不同——
    这正是"分类白打了、总结还在吃旧标签"的根源，指纹必须把它算进去。
    """
    h = hashlib.sha1()
    h.update(("%s|%s|%s" % (SUMMARY_PROMPT_VERSION, mode, model)).encode("utf-8", "ignore"))
    h.update(json.dumps(_norm_filters(filters), sort_keys=True, ensure_ascii=False)
             .encode("utf-8", "ignore"))
    for r in rows:
        h.update(("%d|%.2f|%s|%s|%s|%s" % (
            int(r["id"]), float(r.get("t_start") or 0.0), r.get("speaker_label") or "",
            r.get("category") or "", r.get("topic") or "", r.get("text") or "",
        )).encode("utf-8", "ignore"))
    return h.hexdigest()[:20]


def estimate(conn: sqlite3.Connection, sid: str, filters: dict | None = None,
             mode: str = "content", engine: str = "auto") -> dict:
    """生成前先问一句：这次会不会烧 token、能不能生成。UI 用它决定提示文案。

    本地抽取式已移除：不能生成时必须明说原因（没配模型 / 内容太少 / 预算用完），
    不能再用「本地抽取中」这种听起来像还能出结果的话糊过去。
    """
    f = dict(filters or {})
    f.setdefault("session_id", sid)
    scope = build_scope(conn, sid, f, mode)
    rows = scope.rows
    cfg = settings.resolve()
    configured = bool(cfg["configured"])
    ok, why = _worth_llm(rows)
    if not configured:
        ok, why = False, "还没配置大模型（⚙ 设置）"
    elif engine == "llm":
        ok, why = True, ""          # 强制模式：内容少也调，交给用户决定
    budget = scheduler.budget_status(sid)
    if ok and budget["session"]["limit"] and budget["session"]["remaining"] <= 0:
        ok, why = False, ("本场 token 预算已用完（已用 %d / 上限 %d），本次不生成"
                          % (budget["session"]["used"], budget["session"]["limit"]))
    elif ok and budget["daily"]["limit"] and budget["daily"]["remaining"] <= 0:
        ok, why = False, ("今日全局 token 预算已用完（已用 %d / 上限 %d），本次不生成"
                          % (budget["daily"]["used"], budget["daily"]["limit"]))
    fp = _fingerprint(rows, mode, f, cfg["model"] or "") if configured else ""
    hit = (store.summary_by_fingerprint(conn, sid, mode, fp)
           if (fp and engine != "llm") else None)
    if hit:
        ok, why = False, "输入没变，可以直接复用上次结果（0 token）"
    return {
        "rows": len(rows),
        "chars": sum(len(r.get("text") or "") for r in rows),
        "would_use_llm": ok, "why": why, "model": cfg["model"],
        "configured": configured, "engine": "llm" if ok else "none",
        "cached": bool(hit), "fingerprint": fp,
        "thresholds": {"rows": LLM_MIN_ROWS, "chars": LLM_MIN_CHARS},
        "budget": budget,
    }


def make_summary(conn: sqlite3.Connection, sid: str, filters: dict,
                 mode: str = "content", engine_pref: str = "auto") -> dict:
    """生成场次总结：**只允许大模型**。

    2026-11-19 起移除本地抽取式：它给出的只是高权重原句拼贴，用户无法分辨
    「这段结论是模型读出来的，还是机器按关键词拼的」。从这条链路里删掉之后，
    模型不可用就明确失败，不再落一份看起来像总结的本地稿。
    """
    mode = mode if mode in ("content", "time", "category", "filtered") else "content"
    if engine_pref not in ("auto", "llm"):
        raise RuntimeError("本地总结已移除：总结只能由大模型生成")
    cfg = settings.resolve()
    if not cfg["configured"]:
        raise RuntimeError("大模型还没配置：到「设置」里填好接口地址、模型和 API Key，再生成总结")

    scope = build_scope(conn, sid, filters, mode)
    if not scope.rows:
        raise RuntimeError("当前条件下没有可用发言，无法生成总结")

    speakers = sorted({r["speaker_label"] for r in scope.rows})
    cats: dict[str, int] = {}
    for r in scope.rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    stats = {
        "included_utterances": len(scope.rows),
        "speakers": len(speakers), "speaker_labels": speakers,
        "categories": cats,
        "t_from": min((r["t_start"] for r in scope.rows), default=0.0),
        "t_to": max((r["t_end"] for r in scope.rows), default=0.0),
    }

    # ---- 指纹与缓存：输入没变就直接返回上次结果，一次模型都不调 ----
    # 「强制用大模型」不复用缓存：用户点它就是为了重新生成一份。
    fp = _fingerprint(scope.rows, mode, filters, cfg["model"] or "")
    if engine_pref == "auto":
        hit = store.summary_by_fingerprint(conn, sid, mode, fp)
        if hit:
            hit = dict(hit)
            hit["cached"] = True
            hit["usage"] = None
            hit["fingerprint"] = fp
            # 缓存的是"当时的名字"，用户后来可能改过名；标题条要按当前库里的状态画，
            # 否则会出现"总结说叫 A、顶上说叫 B"。
            st = titles.state(conn, sid)
            hit["session_title"] = st["title"]
            hit["title_source"] = st["source"]
            hit["title_applied"] = bool(st["title"]) and st["title"] == hit.get("title")
            hit["engine_reason"] = ("输入没变（文本 / 分类 / 主题 / 模型 / 筛选都没动），"
                                    "直接复用上次结果，本次 0 token")
            return hit

    ok, why = _worth_llm(scope.rows)
    if not ok and engine_pref == "auto":
        raise RuntimeError(why)

    usage: dict = {}
    llm_errors: list = []
    reason = ""
    groups = None
    llm = None
    name_title: Any = None      # 模型顺手起的名字：与总结同一次调用，输入零增量、输出 +约 40 token
    name_alts: list = []
    topics: list[str] = []
    host_speaker: str = ""
    if mode == "category":
        # 按分类：真正的按类分组，而不是把五类混在一起。也走模型——
        # 本地按标签拼出来的「要点」正是这次要移除的东西。
        t0 = time.time()
        g = _llm_groups(scope, usage, llm_errors)
        usage["ms"] = int((time.time() - t0) * 1000)
        if not g:
            raise RuntimeError(_failure_reason(llm_errors))
        overview, groups, name_title, name_alts = g
        points = store.sort_summary_points(
            [p for grp in groups for p in grp["points"]])
        reason = "按分类组织，用大模型写要点"
    else:
        t0 = time.time()
        llm = _llm_points(scope, usage, llm_errors)
        usage["ms"] = int((time.time() - t0) * 1000)
        if not llm:
            raise RuntimeError(_failure_reason(llm_errors))
        overview, points, name_title, name_alts, topics, host_speaker = llm
        reason = ("内容足够，自动使用大模型" if engine_pref == "auto"
                  else "按你的选择，强制使用大模型")

    model_used = str(usage.get("model") or cfg["model"] or "")
    # 不知道模型名就写 "llm"，不要编一个 "configured" 冒充型号。
    engine = ("llm(" + model_used + ")") if model_used else "llm"

    # ---- 把 P1 产出的主题词表落库：后续逐句标注只能用这套词 ----
    # 只有"这句还没被标过"的场次才写：否则改词表会让所有已标句子的指纹失效，
    # 用户只是点了下总结，结果整场标签作废重标，这代价没人愿意付。
    whole_scope = (mode == "content"
                   and not str(filters.get("q") or "").strip()
                   and not filters.get("categories") and not filters.get("speakers")
                   and filters.get("t_from") is None and filters.get("t_to") is None)
    if topics and whole_scope and store.ai_state(conn, sid)["labeled"] == 0:
        store.set_ai_topics(conn, sid, topics)

    shape_info = shape.summarize(scope.rows,
                                 duration_s=float(scope.session.get("duration_s") or 0.0))
    caveats = list(scope.caveats)
    if len(scope.rows) > LLM_SUMMARY_MAX_ROWS:
        caveats.append("内容较长，按时间分层抽样 %d/%d 句送模型；结论覆盖全段，细节可能省略。"
                       % (LLM_SUMMARY_MAX_ROWS, len(scope.rows)))

    # ---- 顺手起名：同一份内容只读一遍，名字与「能不能改」的决定都交给 titles ----
    enough = _worth_llm(scope.rows)[0]
    whole = whole_scope
    if not name_title:
        name_title = titles.structural(conn, sid)     # 模型没起名时的结构化候选
        name_kind = "structural"
    else:
        name_kind = "llm"
    named = titles.apply_summary(conn, sid, title=name_title, alts=name_alts,
                                 engine_kind="llm", whole=whole, enough=enough,
                                 title_kind=name_kind)

    payload = {
        "session_id": sid, "scope": mode, "engine": engine,
        "engine_kind": "llm",
        "engine_model": model_used,
        "engine_reason": reason,
        "llm_error": (str(llm_errors[-1])[:200] if llm_errors else ""),
        "budget": scheduler.budget_status(sid),
        "usage": usage or None,
        "fingerprint": fp,
        "cached": False,
        "groups": groups if mode == "category" else None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "filter": filters, "overview": overview, "stats": stats,
        "coverage": scope.coverage, "caveats": caveats, "points": points,
        "topics": topics or store.get_ai_topics(conn, sid),
        "host_speaker": host_speaker,
        "shape": shape_info,
        "title": named["title"], "title_alts": named["alts"],
        "title_applied": named["applied"], "title_source": named["source"],
        "title_kind": name_kind,
        "title_reason": named["reason"], "session_title": named["current"],
    }
    payload = store.normalize_summary_payload(payload)
    store.save_summary(conn, sid, mode, filters, engine, payload, fp)
    return payload


WHOLE_FILTERS = {"categories": [], "speakers": [], "q": "", "min_conf": None,
                 "exclude_low_value": True, "exclude_overlap": True}


def _summary_for_filters(conn: sqlite3.Connection, sid: str,
                         filters: dict) -> dict | None:
    """取一条"口径和这次一样"的总结，而不是"最新的一条"。

    为什么不能只取最新：用户可能先跑整场，又跑一次带筛选的（写在后面）。
    汇总段盲取最新，就会拿筛选后的要点去重算整场统计、并按整场口径盖章，
    之后整场总结命中缓存，返回的却是筛选内容——错得很难看出来。
    """
    want = _norm_filters(filters)
    for r in store.recent_summaries(conn, sid, "content", 20):
        try:
            got = _norm_filters(json.loads(r["filter_json"] or "{}"))
        except (ValueError, TypeError):
            continue
        if got != want:
            continue
        try:
            return {"id": r["id"], "payload": json.loads(r["payload_json"] or "{}")}
        except (ValueError, TypeError):
            return None
    return None


def finalize(conn: sqlite3.Connection, sid: str, filters: dict | None = None) -> dict:
    """P3 汇总（**0 次模型调用**）：标签写完之后，用最新数据重算统计与证据，并换新指纹。

    必须换指纹的原因：理解段跑在标注之前，它的指纹是按"当时的标签"算出来的。
    标完不更新，下一次点「生成总结」会认为结果过期再调一次模型——白烧一遍。
    换完之后，这一份总结就是"当前标签下的正确结果"，再点直接命中缓存。

    证据里的 category/topic 是快照，也一并按 id 重新取，否则会出现
    "左边筛选说这是广告，右边证据写着实质讨论"。
    """
    filters = dict(filters or WHOLE_FILTERS)
    scope = build_scope(conn, sid, filters, "content")
    row = _summary_for_filters(conn, sid, filters)
    payload = store.normalize_summary_payload((row or {}).get("payload"))

    if not payload:
        # 理解段没跑成：不再本地拼一份假总结。P1 没结果就是没结果，
        # 明确失败，让用户看到原因后重试。
        raise RuntimeError("还没有 AI 总结可汇总：先完成「理解与主题」这一步")

    # ---- 证据重取：标签变了，evidence 里的类别/主题快照要跟着变 ----
    pts = payload.get("points") or []
    ids: list[int] = []
    for p in pts:
        for e in (p.get("evidence") or []):
            try:
                ids.append(int(e["id"]))
            except (KeyError, TypeError, ValueError):
                pass
    rows = {r["id"]: r for r in store.get_evidence(conn, sid, ids)}
    for p in pts:
        keep = []
        for e in (p.get("evidence") or []):
            try:
                keep.append(int(e["id"]))
            except (KeyError, TypeError, ValueError):
                pass
        ev = [_evidence(rows[i], scope) for i in keep if i in rows]
        if len(ev) < 2:              # 证据闸门照旧：不足两条宁可留着原样也不改写
            continue
        p["evidence"] = ev
        p["t_from"] = min(e["t_start"] for e in ev)
        p["t_to"] = max(e["t_end"] for e in ev)
        p["speakers"] = sorted({e["speaker_label"] for e in ev})
        p["utterance_count"] = len(ev)

    # 归属也要跟着新证据刷新；模型给的那个若已不在证据里，退回本地判定。
    names = {e["speaker_label"] for p in pts for e in (p.get("evidence") or [])}
    for p in pts:
        if p.get("claim_speaker") not in names:
            p["claim_speaker"] = ""
    _attach_claims(pts)

    speakers = sorted({r["speaker_label"] for r in scope.rows})
    cats: dict[str, int] = {}
    for r in scope.rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    payload["stats"] = {
        "included_utterances": len(scope.rows),
        "speakers": len(speakers), "speaker_labels": speakers,
        "categories": cats,
        "t_from": min((r["t_start"] for r in scope.rows), default=0.0),
        "t_to": max((r["t_end"] for r in scope.rows), default=0.0),
    }
    payload["coverage"] = scope.coverage
    caveats = list(scope.caveats)
    if str(payload.get("engine_kind") or "") != "llm":
        # 老库里可能还有本地稿；保留内容，但把来源写清楚，绝不冒充 AI。
        caveats.append("这条总结来自旧版本地摘要，不是 AI 生成；重新生成会改用大模型。")
    payload["caveats"] = caveats
    payload["filter"] = filters
    payload["topics"] = store.get_ai_topics(conn, sid)
    payload["shape"] = shape.summarize(
        scope.rows, duration_s=float(scope.session.get("duration_s") or 0.0))
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["cached"] = False
    fp = _fingerprint(scope.rows, "content", filters, settings.resolve()["model"] or "")
    # 和 make_summary 同一条规矩：只有真正走成模型的结果才盖指纹。
    # 老本地稿即使被 P3 刷新了证据，也不该盖指纹冒充可复用的 AI 结果。
    saved_fp = fp if str(payload.get("engine") or "").startswith("llm") else ""
    payload = store.normalize_summary_payload(payload)
    payload["fingerprint"] = saved_fp
    store.save_summary(conn, sid, "content", filters,
                       str(payload.get("engine") or "llm"), payload, saved_fp)
    return payload
