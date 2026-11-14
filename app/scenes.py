# -*- coding: utf-8 -*-
"""把逐字稿切成「场面」，再判断哪几段值得回听。

为什么不是按句子挑：一句高分话离开上下文常常什么都不是（「就是我大概听了一下，
就是意思是你你」也能拿到不错的语速分）。观众要的是 30~90 秒的一小段戏：谁和谁、
在聊什么、哪一句最值得听。

两层，失败可降级：
  segment()  纯规则，按静默 / 话题 / 时长切段，离线实时都能算；
  judge()    一次模型调用，给候选段写钩子、挑原声、打标签。失败就用规则版。

证据闸门照旧：模型给的 quote 必须在原台词里找得到；找不到就用规则版，不允许编。
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sqlite3

from . import highlight, llm, settings, store, timefmt

# ---------------------------------------------------------------- 可调参数

SCENE_GAP_S = float(os.environ.get("LIVENOTES_SCENE_GAP_S", "8"))
SCENE_MIN_S = float(os.environ.get("LIVENOTES_SCENE_MIN_S", "20"))
SCENE_MAX_S = float(os.environ.get("LIVENOTES_SCENE_MAX_S", "90"))
SCENE_MERGE_GAP_S = float(os.environ.get("LIVENOTES_SCENE_MERGE_GAP_S", "12"))
LIMIT = int(os.environ.get("LIVENOTES_SCENE_LIMIT", "3"))
CANDIDATES = int(os.environ.get("LIVENOTES_SCENE_CANDIDATES", "12"))

TAGS = ("金句", "冲突", "干货", "反转", "问答", "故事", "看点")
_GENERIC = {"", "综合", "综合/未归类", "未归类", "其他", "其它"}
_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()《》「」【】…—\-]+")
_FILLER = re.compile(r"^(?:嗯|啊|哦|呃|那个|就是说|然后|这个|所以)+[，,。]?")
_CLAUSE = re.compile(r"[，。！？；、,.!?;:]")
_TICKS = chr(96) * 3


def _norm(s: str) -> str:
    return _PUNCT.sub("", str(s or ""))


def _cjk(s: str) -> int:
    return sum(1 for ch in (s or "") if ch.isalnum() or "一" <= ch <= "鿿")


def _clean(s, limit: int = 120) -> str:
    t = re.sub(r"\s+", " ", str(s or "")).strip().strip("「」\"'“”")
    return t[:limit]


# ---------------------------------------------------------------- 切段

def _best(group: list[dict]) -> dict | None:
    """一段里"最值得听"的那句：必须是有信息量的句子，不拿语气词当峰值。"""
    solid = [f for f in group
             if f.get("value_tag") != "low"
             and f.get("category") in ("substantive", "question_consult")
             and int(f.get("chars") or 0) >= 8]
    if not solid:
        return None
    best = max(solid, key=lambda f: (float(f.get("score") or 0),
                                     int(f.get("chars") or 0)))
    if float(best.get("score") or 0) < highlight.TH:
        return None
    return best


def _rule_hook(text: str) -> str:
    t = _FILLER.sub("", (text or "").strip())
    for part in _CLAUSE.split(t):
        part = part.strip()
        if 6 <= _cjk(part) <= 18:
            return part
    if _cjk(t) > 18:
        return t[:18]
    return t or "值得回听"


def _rule_tag(f: dict) -> str:
    if f.get("interrupt"):
        return "冲突"
    if int(f.get("num") or 0) >= 2:
        return "干货"
    if f.get("qa"):
        return "问答"
    if f.get("concl"):
        return "观点"
    if int(f.get("chars") or 0) >= 50:
        return "故事"
    return "看点"


def _scene(group: list[dict]) -> dict | None:
    best = _best(group)
    if best is None:
        return None
    lines = [{"t": round(float(f["t_start"]), 1),
              "speaker": f.get("speaker_label") or "",
              "text": f.get("text") or ""} for f in group]
    n = len(group)
    score = round(float(best["score"]) + 0.12 * min(4, max(0, n - 1)), 2)
    text = best.get("text") or ""
    return {
        "t_start": round(float(group[0]["t_start"]), 2),
        "t_end": round(float(group[-1]["t_end"]), 2),
        "peak_t": round(float(best["t_start"]), 2),
        "kind": best.get("kind") or "info",
        "level": ("P0" if float(best.get("score") or 0) >= highlight.PUSH_TH
                  else "P1"),
        "score": score,
        "info_z": best.get("info_z", 0), "arousal_z": best.get("arousal_z", 0),
        "why": best.get("why") or "这段信息密度靠前",
        "speaker_label": best.get("speaker_label") or "",
        "text": text,
        "quote": text,
        "hook": _rule_hook(text),
        "tag": _rule_tag(best),
        "n_utts": n,
        "lines": lines,
        "best": best,
    }


def segment(feats: list[dict]) -> list[dict]:
    """按静默 / 话题转向 / 最长时长切段；太短的段并回上一段。

    feats 必须是按时间排好、与逐句特征一致的列表。返回值按原时间顺序。
    """
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for f in feats:
        if not cur:
            cur = [f]
            continue
        prev = cur[-1]
        gap = float(f["t_start"]) - float(prev["t_end"])
        dur = float(cur[-1]["t_end"]) - float(cur[0]["t_start"])
        topic_shift = (f.get("topic") and prev.get("topic")
                       and f["topic"] != prev["topic"]
                       and f["topic"] not in _GENERIC
                       and prev["topic"] not in _GENERIC)
        too_long = (float(f["t_end"]) - float(cur[0]["t_start"])) > SCENE_MAX_S
        if gap > SCENE_GAP_S or (topic_shift and dur >= SCENE_MIN_S) or too_long:
            groups.append(cur)
            cur = [f]
        else:
            cur.append(f)
    if cur:
        groups.append(cur)

    merged: list[list[dict]] = []
    for g in groups:
        dur = float(g[-1]["t_end"]) - float(g[0]["t_start"])
        chars = sum(int(x.get("chars") or 0) for x in g)
        if merged and (dur < SCENE_MIN_S or chars < 12):
            prev = merged[-1]
            gap = float(g[0]["t_start"]) - float(prev[-1]["t_end"])
            if (gap <= SCENE_MERGE_GAP_S
                    and float(g[-1]["t_end"]) - float(prev[0]["t_start"])
                    <= SCENE_MAX_S * 1.5):
                merged[-1] = prev + g
                continue
        merged.append(g)

    out: list[dict] = []
    for g in merged:
        sc = _scene(g)
        if sc:
            out.append(sc)
    return out


def candidates(feats: list[dict], limit: int = CANDIDATES) -> list[dict]:
    """切段后按分数取候选，并给"有戏"的段留配额（复用 highlight 的双轴混排）。"""
    scenes = segment(feats)
    if not scenes:
        return []
    scenes.sort(key=lambda x: -float(x["score"]))
    return highlight._mix(scenes, limit)


# ---------------------------------------------------------------- 模型判断

_PROMPT = """你在为一档中文直播挑「值得回听的名场面」。候选片段已经按结构信号初筛过，
但结构分高不等于内容好。请按内容判断，挑出最值得回听的 3 段，宁缺毋滥。

输入 scenes 的每一项：
- id：候选编号；start：起始时间；speakers：说话人表；
- lines：逐句数组，[说话人下标, 原文]。

输出严格 JSON（不要 markdown 代码块）：
{"picks":[{"id":0,"hook":"10~16个汉字的钩子","quote":"该段里最值得听的一句原话","tag":"金句","why":"一句话说明为什么值得听"}]}

规则：
1. hook 必须具体、有信息，不要「精彩片段」「值得一看」「干货满满」这类空话；
2. quote 必须从该段 lines 的原文里逐字摘出，不改写、不拼接、不补标点；
3. tag 只能是：金句 / 冲突 / 干货 / 反转 / 问答 / 故事 / 看点；
4. why 不超过 30 字；
5. 只允许使用给出的原文，禁止编造人物、数字、因果；没有值得听的段就返回 {"picks":[]}。
"""


def _json_obj(text: str) -> dict:
    s = re.sub("^" + _TICKS + "(?:json)?|" + _TICKS + "$", "", (text or "").strip(),
               flags=re.M).strip()
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


def _pack(cands: list[dict]) -> dict:
    out = []
    for i, c in enumerate(cands):
        spk: list[str] = []
        lines: list[list] = []
        for ln in c["lines"][:20]:
            name = ln.get("speaker") or "?"
            if name not in spk:
                spk.append(name)
            lines.append([spk.index(name), str(ln.get("text") or "")[:120]])
        out.append({"id": i, "start": timefmt.offset(c["t_start"]),
                    "speakers": spk, "lines": lines})
    return {"scenes": out}


def _pick_quote(quote: str, c: dict) -> str:
    """证据闸门：模型摘的原话必须在整段台词里，找不到就不采用。"""
    q = _norm(quote)
    if not q:
        return ""
    joined = _norm("".join(str(x.get("text") or "") for x in c["lines"]))
    if q in joined:
        return _clean(quote)
    best, ratio = None, 0.0
    for ln in c["lines"]:
        text = str(ln.get("text") or "")
        r = difflib.SequenceMatcher(None, q, _norm(text)).ratio()
        if r > ratio:
            best, ratio = text, r
    # 0.6 以下多半是模型在改写；宁可退回规则版，也不把改写当原声。
    return _clean(best) if best and ratio >= 0.6 else ""


def judge(cands: list[dict], sid: str = "") -> list[dict]:
    if not cands:
        return []
    content = llm.chat([
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": json.dumps(_pack(cands), ensure_ascii=False)},
    ], temperature=0.3, timeout=90, max_tokens=900,
        session_id=sid, task="highlight")
    data = _json_obj(content)
    picks: list[dict] = []
    seen: set[int] = set()
    for p in data.get("picks") or []:
        if not isinstance(p, dict):
            continue
        try:
            i = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if i in seen or not (0 <= i < len(cands)):
            continue
        c = cands[i]
        quote = _pick_quote(str(p.get("quote") or ""), c)
        if not quote:
            continue
        hook = _clean(p.get("hook"), 24)
        if _cjk(hook) < 4:
            hook = c["hook"]
        tag = str(p.get("tag") or "")
        if tag not in TAGS:
            tag = c["tag"]
        picks.append({"id": i, "hook": hook, "quote": quote, "tag": tag,
                      "why": _clean(p.get("why"), 40)})
        seen.add(i)
    return picks[:LIMIT]


# ---------------------------------------------------------------- 落库 / 不落库

def _event(c: dict, sid: str) -> dict:
    best = c.get("best") or {}
    features = {
        "scene": True, "tag": c["tag"], "hook": c["hook"],
        "num": best.get("num", 0), "concl": best.get("concl", 0),
        "qa": best.get("qa", 0), "interrupt": best.get("interrupt", 0),
        "burst": best.get("burst", 0), "chars": best.get("chars", 0),
        "rate": None if best.get("rate") is None else round(float(best["rate"]), 2),
        "med_rate": round(float(best.get("med_rate") or 0.0), 2),
        "n_utts": c["n_utts"],
    }
    return {
        "session_id": sid,
        "t_start": c["t_start"], "t_end": c["t_end"], "peak_t": c["peak_t"],
        "kind": c["kind"], "level": c["level"], "score": c["score"],
        "info_z": c["info_z"], "arousal_z": c["arousal_z"],
        "why": c["why"], "speaker_label": c["speaker_label"],
        "text": c["text"], "n_utts": c["n_utts"],
        "features_json": json.dumps(features, ensure_ascii=False),
        "hook": c["hook"], "tag": c["tag"], "quote": c["quote"],
        "scene_json": json.dumps(c["lines"], ensure_ascii=False),
        "scene": c["lines"],
    }


def assemble(cands: list[dict], picks: list[dict], limit: int, sid: str) -> list[dict]:
    """模型选中的优先，不够再用规则版补；每段只出现一次。"""
    by_id = {i: c for i, c in enumerate(cands)}
    chosen: list[dict] = []
    used: set[int] = set()
    for p in picks:
        i = p.get("id")
        if i in used or i not in by_id:
            continue
        c = dict(by_id[i])
        c.update({"hook": p.get("hook") or c["hook"],
                  "quote": p.get("quote") or c["quote"],
                  "tag": p.get("tag") or c["tag"]})
        if p.get("why"):
            c["why"] = p["why"]
        chosen.append(c)
        used.add(i)
        if len(chosen) >= limit:
            break
    for i, c in enumerate(cands):
        if len(chosen) >= limit:
            break
        if i in used:
            continue
        chosen.append(c)
        used.add(i)
    return [_event(c, sid) for c in chosen[:limit]]


def session_events(conn: sqlite3.Connection, sid: str, limit: int = LIMIT,
                   with_audio: bool = False) -> list[dict]:
    """算一场的场面，不写库、不调模型。收工回顾和报告用它做 0 token 兜底。"""
    rows = store.list_utterances_for_highlight(conn, sid)
    if not rows:
        return []
    sess = store.get_session(conn, sid) or {}
    feats = highlight.score_rows(rows, sess.get("audio_path") if with_audio else None)
    for f in feats:
        f["session_id"] = sid
    cands = candidates(feats, max(CANDIDATES, limit * 4))
    return assemble(cands, [], limit, sid)


def build(conn: sqlite3.Connection, sid: str, limit: int = LIMIT,
          use_llm: bool = True) -> dict:
    """整场重算并落库。模型失败不报错，退回规则版并在返回值里说明。"""
    rows = store.list_utterances_for_highlight(conn, sid)
    if not rows:
        return {"session_id": sid, "n": 0, "highlights": [], "audio": False,
                "engine": "rules", "llm_error": ""}
    sess = store.get_session(conn, sid) or {}
    feats = highlight.score_rows(rows, sess.get("audio_path"))
    for f in feats:
        f["session_id"] = sid
    pairs = []
    for f in feats:
        if f.get("id") is None:
            continue
        pairs.append((json.dumps({
            "rate": None if f.get("rate") is None else round(f["rate"], 2),
            "rms": f["rms"], "chars": f["chars"], "num": f["num"],
            "concl": f["concl"], "qa": f["qa"], "interrupt": f["interrupt"],
            "burst": f["burst"], "kind": f["kind"],
        }, ensure_ascii=False), f["id"]))
    store.save_utterance_features(conn, pairs)

    cands = candidates(feats, max(CANDIDATES, limit * 4))
    picks: list[dict] = []
    engine, err = "rules", ""
    if use_llm and cands:
        cfg = settings.resolve()
        if cfg["configured"]:
            try:
                picks = judge(cands, sid)
                if picks:
                    engine = "llm(" + (cfg["model"] or "configured") + ")"
            except Exception as e:          # 网络/预算/格式都退回规则版
                err = str(e)[:200]
    events = assemble(cands, picks, limit, sid)
    n = store.replace_highlights(conn, sid, events)
    return {"session_id": sid, "n": n, "highlights": events,
            "audio": any(f["rms"] is not None for f in feats),
            "engine": engine, "llm_error": err}
