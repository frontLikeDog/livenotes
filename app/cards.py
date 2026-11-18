# -*- coding: utf-8 -*-
"""只读分享页：一条观点 / 一个人的上麦名片。

为什么单独一个模块：分享卡上的二维码要打开「这一条观点」，而不是整场笔记
（docs/41 §6.1）。这两个页面也复用给「上麦者名片」工具，所以它们只依赖
``claims`` / ``store``，不依赖前端框架，手机扫码打开即可读。

隐私边界沿用 docs/38 §8：只展示昵称与文字，不展示头像，不提供原声下载；
页面底部保留「打开完整笔记」和「记录你自己的上麦」两个出口。
"""
from __future__ import annotations

import html
import sqlite3

from . import claims, store


def _e(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _fmt_t(seconds: object) -> str:
    try:
        t = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        return ""
    if t >= 3600:
        return "%d:%02d:%02d" % (t // 3600, t // 60 % 60, t % 60)
    return "%02d:%02d" % (t // 60 % 60, t % 60)


def _day(at: object) -> str:
    s = str(at or "")
    return s[:10] if len(s) >= 10 else "日期不明"


def session_claims(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """一场里可以做成观点卡的原子（有主张句、有原话的优先）。"""
    out = []
    for r in claims.rows_for(conn, session_id=sid, limit=500):
        stance = str(r.get("stance") or "").strip()
        if not stance:
            continue
        out.append({
            "claim_id": str(r.get("claim_id") or ""),
            "who": str(r.get("who") or ""),
            "person_id": str(r.get("person_id") or ""),
            "topic": str(r.get("topic") or ""),
            "stance": stance,
            "quote": str(r.get("quote") or ""),
            "t": r.get("t"),
            "kind": str(r.get("kind") or "claim"),
            "session_id": str(r.get("session_id") or ""),
        })
    # 有原话的先上卡；问题排在主张后面（观点卡讲的是「他说了什么」）。
    out.sort(key=lambda x: (x["kind"] != "claim", not bool(x["quote"]),
                            float(x["t"] or 0)))
    return out


_CSS = """
:root{--bg:#0a0c0f;--panel:#12161c;--line:#242b35;--ink:#ece9e2;--muted:#9aa4b0;
  --faint:#6e7885;--amber:#e7b669;--amber-soft:rgba(231,182,105,.1)}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:radial-gradient(900px 520px at 18% -8%,
  rgba(231,182,105,.14),transparent 62%),var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.wrap{max-width:620px;margin:0 auto;padding:34px 20px 60px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:34px;color:var(--faint);
  font-size:12px;letter-spacing:.08em}
.mark{width:30px;height:30px;border-radius:8px;background:var(--amber);color:#231704;
  display:grid;place-items:center;font-weight:800;font-size:16px}
.eyebrow{font:600 11px/1.4 ui-monospace,Consolas,monospace;letter-spacing:.14em;
  color:var(--amber);margin:0 0 10px}
h1{margin:0 0 6px;font-size:30px;line-height:1.35;letter-spacing:-.01em}
.meta{color:var(--muted);font-size:13px;line-height:1.8}
.topic{display:inline-block;margin:18px 0 10px;padding:4px 10px;border-radius:999px;
  background:var(--amber-soft);color:var(--amber);font-size:12px}
.quote{margin:22px 0 0;padding:24px 22px;border:1px solid var(--line);border-radius:14px;
  background:var(--panel);font-size:19px;line-height:1.75}
.quote .mark{display:none}
.quote .src{margin-top:14px;color:var(--faint);font-size:12px}
.card{margin-top:22px;border:1px solid var(--line);border-radius:14px;background:var(--panel);
  padding:20px 22px}
.card h2{margin:0 0 8px;font-size:14px;color:var(--muted);font-weight:600}
.card p{margin:0;line-height:1.75;font-size:15px}
.claim{border-top:1px solid var(--line);padding:14px 0}
.claim:first-of-type{border-top:0}
.claim .st{font-size:15px;line-height:1.7}
.claim .qv{margin-top:6px;color:var(--faint);font-size:12.5px;line-height:1.65}
.claim .when{margin-top:6px;color:var(--faint);font-size:11.5px}
.foot{display:flex;flex-wrap:wrap;gap:10px;margin-top:30px}
.btn{display:inline-block;padding:10px 16px;border-radius:9px;border:1px solid var(--line);
  color:var(--ink);text-decoration:none;font-size:13px;background:var(--panel)}
.btn.primary{background:var(--amber);border-color:var(--amber);color:#231704;font-weight:700}
.fine{margin-top:26px;color:var(--faint);font-size:11.5px;line-height:1.8}
@media(max-width:520px){h1{font-size:25px}.quote{font-size:17px;padding:20px 18px}
  .wrap{padding:26px 16px 48px}}
"""


def _page(title: str, body: str) -> str:
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0A0C0F">
<title>%s · LiveNotes</title><style>%s</style></head><body>
<div class="wrap"><div class="brand"><span class="mark">L</span>LiveNotes · 实时语记</div>
%s
<p class="fine">音频与逐字稿只保存在原设备；这页只包含昵称和文字，不提供原声下载。</p>
</div></body></html>""" % (_e(title), _CSS, body)


def claim_page(claim: dict) -> str:
    """一条观点的只读页。扫码打开的是它，而不是整场笔记。"""
    who = str(claim.get("who") or "一位上麦者")
    topic = str(claim.get("topic") or "")
    stance = str(claim.get("stance") or "").strip()
    quote = str(claim.get("quote") or "").strip()
    title = str(claim.get("session_title") or "这一场")
    at = _day(claim.get("started_at"))
    t = _fmt_t(claim.get("t"))
    sid = str(claim.get("session_id") or "")
    matter = topic or "观点"
    body = (
        '<p class="eyebrow">观点 · %s</p>'
        '<h1>%s</h1>'
        '<p class="meta">来自《%s》%s%s</p>'
        '<span class="topic">%s</span>'
        '<div class="quote">%s%s</div>'
        '<div class="foot">'
        '<a class="btn" href="/note?session=%s%s">打开完整笔记</a>'
        '<a class="btn primary" href="/">记录你自己的上麦</a>'
        '</div>'
        % (_e(at), _e(who), _e(title), _e(at),
           (" · " + t) if t else "", _e(matter), _e(stance),
           ('<div class="src">「%s」</div>' % _e(quote)) if quote else "",
           _e(sid), ("&amp;t=%d" % int(float(claim.get("t") or 0)))
           if claim.get("t") is not None else ""))
    return _page(who + " · " + matter, body)


def person_page(conn: sqlite3.Connection, person_id: str) -> str | None:
    """上麦者名片：这个人讲过的主题 + 最近一次主张。没有这个人返回 None。"""
    person = store.get_person(conn, person_id)
    if not person:
        return None
    prof = claims.profile(conn, person_id=person_id)
    rows = store.person_appearances(conn, person_id)
    n_sessions = len({str(r.get("session_id") or "") for r in rows})
    seconds = sum(float(r.get("seconds") or 0) for r in rows)
    hours = ("%.1f 小时" % (seconds / 3600.0)) if seconds >= 3600 else \
        ("%d 分钟" % max(0, int(seconds / 60)))
    topics = [g for g in prof.get("topics") or [] if g.get("topic")]
    blocks = []
    for g in topics[:4]:
        latest = g.get("latest") or {}
        items = []
        for it in (g.get("items") or [])[-2:]:
            qv = str(it.get("quote") or "").strip()
            items.append(
                '<div class="claim"><div class="st">%s%s</div>%s'
                '<div class="when">%s · 《%s》%s</div></div>'
                % ("<b>改口 · </b>" if it.get("_changed") else "",
                   _e(it.get("stance")),
                   ('<div class="qv">「%s」</div>' % _e(qv)) if qv else "",
                   _e(_day(it.get("started_at"))),
                   _e(it.get("session_title") or "这一场"),
                   (" · " + _fmt_t(it.get("t"))) if it.get("t") is not None else ""))
        blocks.append(
            '<div class="card"><h2>%s · %d 场 · %d 条</h2>%s</div>'
            % (_e(g.get("topic")), int(g.get("n_sessions") or 0),
               int(g.get("n") or 0), "".join(items)))
    if not blocks:
        blocks.append('<div class="card"><p>还没有可归档的观点。等这个人再上几次麦，'
                      '名片会自己变厚。</p></div>')
    body = (
        '<p class="eyebrow">上麦者名片</p>'
        '<h1>%s</h1>'
        '<p class="meta">出现在 %d 场 · 累计发言 %s · %d 条观点 · %d 个主题</p>'
        '%s'
        '<div class="foot">'
        '<a class="btn" href="/people?person=%s">在人物档案里看全部</a>'
        '<a class="btn primary" href="/">记录你自己的上麦</a>'
        '</div>'
        % (_e(person.get("name")), n_sessions, _e(hours),
           int(prof.get("n_claims") or 0), int(prof.get("n_topics") or 0),
           "".join(blocks), _e(person_id)))
    return _page(str(person.get("name") or "上麦者"), body)
