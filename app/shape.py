# -*- coding: utf-8 -*-
"""场次形态与场内角色：单人输出 / 圆桌讨论 / 问答厅，以及「谁在主持」。

为什么单独一层：语音厅里「话最多的人」不等于「主持」——主持只做暖场和点名，
内容在轮流上麦的十几个人身上。沿用「发言时长第一 = 主播」会把一场圆桌读成
一个人的独白（见 docs/41 §1）。这里只用规则和已有分类标签，0 token、可复现；
判错了顶多影响分组视图和默认标签，不会污染总结与证据。

输入行只需要 ``speaker_id / speaker_label / t_start / t_end / category``，
按时间升序；``overlap=1`` 的行（多人重叠、未转写）直接跳过。
"""
from __future__ import annotations

from typing import Any

from .config import LIVE_MAX_SPEAKERS

SHAPE_SINGLE = "single"
SHAPE_ROUNDTABLE = "roundtable"
SHAPE_QA = "qa"

SHAPE_LABEL = {
    SHAPE_SINGLE: "单人输出",
    SHAPE_ROUNDTABLE: "圆桌讨论",
    SHAPE_QA: "问答厅",
}

ROLE_HOST = "host"
ROLE_SPEAKER = "speaker"
ROLE_ASKER = "asker"
ROLE_GUEST = "guest"

ROLE_LABEL = {
    ROLE_HOST: "主持",
    ROLE_SPEAKER: "主讲",
    ROLE_ASKER: "提问",
    ROLE_GUEST: "上麦",
}

# 单人输出的判定：第一名占全场人声的比例
SINGLE_SHARE = 0.55
# 问答厅：提问句占比 + 有效发言人数
QA_RATIO = 0.25
QA_MAX_SPEAKERS = 3
# 「有效发言人」= 累计实质发言不少于这么多秒
EFFECTIVE_S = 30.0
# 主持判定看「控场话密度」而不是条数：一句「欢迎」只有几秒，够不上；
# 而语音厅主持一场可能只说几十条短话，条数门槛会把真人主持挡在外面。
HOST_MIN_S = 20.0
HOST_DENSITY = 0.3


def summarize(rows: list[dict], duration_s: float = 0.0) -> dict[str, Any]:
    """把一场的发言压成形态 + 角色。

    ``rows`` 必须按时间升序（``store.query_utterances`` 与 ``session_shape``
    都是这个顺序）；重叠段（``overlap=1``）不参与任何统计。
    """
    stats: dict[str, dict] = {}
    order: list[str] = []
    total_s = 0.0
    span_end = 0.0
    switches = 0
    n_rows = 0
    n_question = 0
    prev: str | None = None

    for r in rows or []:
        if r.get("overlap"):
            continue
        sid = str(r.get("speaker_id") or "")
        if not sid:
            continue
        dur = max(0.0, float(r.get("t_end") or 0.0) - float(r.get("t_start") or 0.0))
        st = stats.get(sid)
        if st is None:
            st = stats[sid] = {
                "speaker_id": sid,
                "label": str(r.get("speaker_label") or sid),
                "seconds": 0.0, "sub_s": 0.0, "n": 0,
                "host_n": 0, "host_s": 0.0, "ask_n": 0, "longest": 0.0,
            }
            order.append(sid)
        cat = str(r.get("category") or "")
        st["seconds"] += dur
        st["n"] += 1
        st["longest"] = max(st["longest"], dur)
        if cat == "substantive":        # 提问不算「实质输出」：那是别人的答案
            st["sub_s"] += dur
        if cat == "host_control":
            st["host_n"] += 1
            st["host_s"] += dur
        if cat == "question_consult":
            st["ask_n"] += 1
            n_question += 1
        total_s += dur
        n_rows += 1
        span_end = max(span_end, float(r.get("t_end") or 0.0))
        if prev is not None and sid != prev:
            switches += 1
        prev = sid

    if not order or total_s <= 0:
        return {"kind": "", "label": "", "host_id": "", "top_id": "",
                "speaker_count": 0, "effective": 0, "roles": {},
                "host_share": 0.0, "qa_ratio": 0.0, "switch_per_min": 0.0,
                "duration_s": float(duration_s or 0.0), "caveats": []}

    for st in stats.values():
        st["share"] = round(st["seconds"] / total_s, 3)
        st["avg_s"] = round(st["seconds"] / max(1, st["n"]), 1)

    def _rank(key: str) -> list[str]:
        return sorted(order, key=lambda s: (stats[s][key], stats[s]["n"]), reverse=True)

    # 「谁在输出内容」按实质发言量算，不按总时长：主持人可以说很多流程话。
    top = _rank("sub_s")[0] or _rank("seconds")[0]
    top_share = stats[top]["share"]
    # 「有效发言人」按实质输出算：只提问、只控场的人不算，否则一场问答厅
    # 会被十几个只问一句的人稀释成「很多人」。
    effective = [s for s in order if stats[s]["sub_s"] >= EFFECTIVE_S]
    qa_ratio = n_question / max(1, n_rows)

    # 主持：控场话时长第一、且控场话占他自己发言的比例够高的人。
    # 只数条数会把「说了两句欢迎的嘉宾」认成主持，只按总时长又会选到话最多的人。
    host_id = ""
    best = _rank("host_s")[0]
    density = stats[best]["host_n"] / max(1, stats[best]["n"])
    if stats[best]["host_s"] >= HOST_MIN_S and density >= HOST_DENSITY:
        host_id = best

    # 问答厅的「主持」多半就是答主本人（开场问一句、然后一直答），
    # 不把答主算作主持会让整场群龙无首。
    qa_kind = (qa_ratio >= QA_RATIO and len(effective) <= QA_MAX_SPEAKERS
               and n_question >= 3)
    if qa_kind and stats[top]["sub_s"] > 0:
        host_id = top

    # 先判问答厅：一个答主滔滔不绝 + 一群只问不答的人，从时长看像单人输出，
    # 但整理方式完全不同（要成「问题清单 + 谁答的」，不是一套要点）。
    if qa_kind:
        kind = SHAPE_QA
    elif top_share >= SINGLE_SHARE:
        kind = SHAPE_SINGLE
        host_id = top                     # 单人场里输出内容的人就是这场的主播
    else:
        kind = SHAPE_ROUNDTABLE

    host_share = stats[host_id]["share"] if host_id else top_share
    top3 = {s for s in _rank("sub_s")[:3] if stats[s]["sub_s"] > 0}

    roles: dict[str, dict] = {}
    for sid in order:
        st = stats[sid]
        avg = st["seconds"] / max(1, st["n"])
        ask_ratio = st["ask_n"] / max(1, st["n"])
        if sid == host_id:
            role = ROLE_HOST
        elif st["ask_n"] and ask_ratio >= 0.5 and st["sub_s"] < EFFECTIVE_S:
            # 只问不答：提问者（连麦咨询里很多听众整场只问一句）。
            # 不设发言条数门槛，否则「一人只问一次」的问答厅里没人算提问者。
            role = ROLE_ASKER
        elif sid in top3 and st["sub_s"] >= EFFECTIVE_S:
            role = ROLE_SPEAKER
        else:
            role = ROLE_GUEST
        roles[sid] = {"role": role, "label": ROLE_LABEL[role],
                      "seconds": round(st["seconds"], 1),
                      "sub_s": round(st["sub_s"], 1),
                      "n": st["n"], "share": st["share"]}

    duration = float(duration_s or 0.0) or span_end
    caveats: list[str] = []
    if len(order) >= LIVE_MAX_SPEAKERS:
        caveats.append(
            "本场识别到 %d 位说话人，已达到实时区分上限：同时说话的部分会被并入"
            "其他说话人，可在逐字稿里「重新分人」再校对。" % len(order))
    if kind == SHAPE_ROUNDTABLE and not host_id:
        caveats.append("没有识别出主持人（控场话术太少），按发言量列出了前几位主讲。")

    return {
        "kind": kind, "label": SHAPE_LABEL[kind],
        "host_id": host_id, "top_id": top,
        "speaker_count": len(order), "effective": len(effective),
        "roles": roles,
        "host_share": round(host_share, 3),
        "top_share": round(top_share, 3),
        "qa_ratio": round(qa_ratio, 3),
        "switch_per_min": round(switches / (duration / 60.0), 2) if duration else 0.0,
        "duration_s": round(duration, 1),
        "caveats": caveats,
    }


def session_shape(conn, sid: str) -> dict[str, Any]:
    """从库里算一场的形态（打开笔记时用；老总结没有存档字段时的兜底）。"""
    from . import store
    sess = store.get_session(conn, sid) or {}
    rows = [dict(r) for r in conn.execute(
        "SELECT speaker_id, speaker_label, t_start, t_end, category, overlap "
        "FROM utterances WHERE session_id=? ORDER BY t_start, id", (sid,))]
    return summarize(rows, duration_s=float(sess.get("duration_s") or 0.0))
