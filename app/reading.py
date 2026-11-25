# -*- coding: utf-8 -*-
"""读侧：一人一篇「观点档案」Markdown，以及检索/问答里要用的文档链接。

为什么单独一层：发布器管「一场 = 一篇云文档」，这里管「一人 = 一篇档案」
（docs/47 §3.3）。内容只从 SQLite 读：``claims.profile_any`` 给跨场观点，
``store.person_appearances`` 给出没场次；不调模型、不猜人名。

三条纪律：

1. **没有观点就不生成档案**。一个只有出没记录、没有主张的人，生成一篇
   空文档只会让「人物档案」看起来像个通讯录；调用方拿到 ``ok=False`` 后
   应直接说「还没有观点记录」。
2. **按需生成，不在每场结束后自动跑**。语音厅一晚上 8 个人，自动发布就是
   8 篇文档；用户点 `/找人` 时才生成/刷新，重复打开版本没变就不重写。
3. **只读发布台账，不反写**。给场次加文档链接时只查 ``feishu_publish``；
   没有这一行就明确写「未发布」，不伪造链接。

人物档案的文档缓存放在 ``person_docs`` 表（本模块自己建），而不是复用
``feishu_publish``：后者的唯一键是 ``(session_id, kind)``，而人物档案在
「还没认出声纹、只有昵称」时没有 person_id，需要一个稳定的昵称键。
"""
from __future__ import annotations

import hashlib
import inspect
import re
import sqlite3
import traceback
from typing import Any

from . import claims, logs, store

PERSON_KIND = "person"
MAX_TOPICS = 8
MAX_SESSIONS = 20
QUOTE_CHARS = 80
STANCE_CHARS = 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_docs (
    person_key  TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    doc_token   TEXT NOT NULL DEFAULT '',
    doc_url     TEXT NOT NULL DEFAULT '',
    version     TEXT NOT NULL DEFAULT '',
    updated_at  TEXT
)
"""


# ---------------------------------------------------------------- 小工具

def _mmdd(iso: Any) -> str:
    """'2026-11-25T22:30:00' -> '11-25'；认不出来就原样截一段。"""
    s = str(iso or "").strip()
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})", s)
    return ("%s-%s" % (m.group(1), m.group(2))) if m else s[:10]


def _dur(seconds: Any) -> str:
    try:
        s = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return "%d 小时 %d 分" % (h, m) if m else "%d 小时" % h
    return "%d 分" % m if m else "%d 秒" % s


def _clean(text: Any, limit: int = STANCE_CHARS) -> str:
    """一句话进 Markdown 前的清洗：去换行、压空白、截断。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) > limit:
        s = s[:max(0, limit - 1)].rstrip("，,；;。. ") + "…"
    return s


def _same_text(a: Any, b: Any) -> bool:
    """两句话是不是同一句（避免「主张」和「原话」读两遍）。"""
    x = re.sub(r"[\s，。！？；：、,.!?;:]+", "", str(a or ""))
    y = re.sub(r"[\s，。！？；：、,.!?;:]+", "", str(b or ""))
    if not x or not y:
        return False
    return x in y or y in x


def _cell(text: Any, limit: int = 40) -> str:
    """表格单元格：去竖线/换行，避免把表格撑坏。"""
    s = _clean(text, limit).replace("|", "／")
    return s or "-"


def _truncate_quote(text: Any) -> str:
    return _clean(text, QUOTE_CHARS)


# ---------------------------------------------------------------- 场次文档链接

def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        conn.execute("SELECT 1 FROM %s LIMIT 1" % name)
        return True
    except sqlite3.OperationalError:
        return False


def session_doc_url(conn: sqlite3.Connection, sid: str) -> str:
    """这一场的飞书文档链接；没发布过/台账不存在时返回空串。

    直接读表而不是 import ``feishu_publish``：读侧不依赖发布器是否启用，
    发布实现换了（或还没合并）也不会让 `/搜` 挂掉。
    """
    sid = str(sid or "")
    if not sid or not _has_table(conn, "feishu_publish"):
        return ""
    try:
        row = conn.execute(
            "SELECT doc_url FROM feishu_publish WHERE session_id=? AND kind=?",
            (sid, "session")).fetchone()
    except sqlite3.OperationalError:
        return ""
    return str((row["doc_url"] if row else "") or "")


# ---------------------------------------------------------------- 人物解析

def person_key(person_id: str = "", name: str = "") -> str:
    """文档缓存键：认出声纹的人用 person_id，只有昵称的人用 ``name:``。"""
    pid = str(person_id or "").strip()
    if pid:
        return pid
    n = str(name or "").strip()
    return ("name:" + n) if n else ""


def _person_id_for_who(conn: sqlite3.Connection, who: str) -> str:
    """昵称精确命中、观点已经挂到某个 person_id 上时，直接认成那个人。

    底库 ``persons`` 还没建行（例如老数据只写过 speakers.person_id）也能查，
    否则「/找人 阿哲」会退化成只有昵称的档案，丢掉跨场身份。
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT person_id FROM claims"
            " WHERE who=? AND person_id<>'' LIMIT 2", (who,)).fetchall()
    except sqlite3.OperationalError:
        return ""
    pids = [str(r["person_id"]) for r in rows if str(r["person_id"] or "")]
    return pids[0] if len(pids) == 1 else ""


def _name_aliases(conn: sqlite3.Connection, q: str, limit: int = 10) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT DISTINCT who FROM claims WHERE who<>'' AND who LIKE ? "
            "ORDER BY who LIMIT ?", ("%" + q.replace("%", "") + "%", limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(r["who"]) for r in rows if str(r["who"] or "").strip()]


def resolve_person(conn: sqlite3.Connection, query: str) -> dict:
    """把用户输入的名字解析成一个人。

    返回 ``kind``：
      * ``person``     —— 底库里有这个人（person_id）
      * ``nickname``   —— 只有观点里的昵称，没有跨场身份
      * ``ambiguous``  —— 命中多个，让用户说得更具体
      * ``none`` / ``empty``
    """
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "kind": "empty", "error": "要说找谁，比如 /找人 阿哲"}
    persons = store.find_persons(conn, q)
    exact = [p for p in persons if str(p.get("name") or "") == q]
    if exact:
        p = exact[0]
        return {"ok": True, "kind": "person", "person_id": str(p["person_id"]),
                "name": str(p["name"])}
    aliases = _name_aliases(conn, q)
    if q in aliases:
        pid = _person_id_for_who(conn, q)
        if pid:
            return {"ok": True, "kind": "person", "person_id": pid, "name": q}
        return {"ok": True, "kind": "nickname", "person_id": "", "name": q}
    names: list[str] = []
    seen: set[str] = set()
    for p in persons:
        n = str(p.get("name") or "").strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    for n in aliases:
        if n not in seen:
            seen.add(n)
            names.append(n)
    if len(names) == 1:
        n = names[0]
        p = next((x for x in persons if str(x.get("name") or "") == n), None)
        if p:
            return {"ok": True, "kind": "person",
                    "person_id": str(p["person_id"]), "name": n}
        pid = _person_id_for_who(conn, n)
        if pid:
            return {"ok": True, "kind": "person", "person_id": pid, "name": n}
        return {"ok": True, "kind": "nickname", "person_id": "", "name": n}
    if names:
        return {"ok": False, "kind": "ambiguous", "matches": names[:10],
                "error": "找到多个像「%s」的人" % q}
    return {"ok": False, "kind": "none",
            "error": "没找到「%s」。可以先用 /搜 找他说过的话。" % q}


# ---------------------------------------------------------------- 档案内容

def _profile_rows(profile: dict) -> list[dict]:
    out: list[dict] = []
    for g in profile.get("topics") or []:
        for r in g.get("items") or []:
            out.append(r)
    return out


def _claim_sort_key(r: dict) -> tuple[str, float]:
    try:
        t = float(r.get("t") or 0)
    except (TypeError, ValueError):
        t = 0.0
    return (str(r.get("started_at") or ""), t)


def _latest_claims(profile: dict, limit: int = 3) -> list[dict]:
    rows = sorted(_profile_rows(profile), key=_claim_sort_key, reverse=True)
    out: list[dict] = []
    for r in rows:
        stance = str(r.get("stance") or r.get("gist") or "").strip()
        if not stance:
            continue
        out.append({
            "topic": claims.topic_label(str(r.get("topic") or "")),
            "stance": _clean(stance),
            "at": _mmdd(r.get("started_at")),
            "session_id": str(r.get("session_id") or ""),
            "session_title": str(r.get("session_title") or ""),
            "t": r.get("t"),
            "kind": str(r.get("kind") or "claim"),
        })
        if len(out) >= limit:
            break
    return out


def _session_facts(conn: sqlite3.Connection, person_id: str,
                   profile: dict) -> list[dict]:
    """这个人的场次清单：优先用声纹出现记录，没有身份就从观点里推。"""
    sessions: dict[str, dict] = {}
    if person_id:
        for a in store.person_appearances(conn, person_id):
            sid = str(a.get("session_id") or "")
            if not sid:
                continue
            cur = sessions.setdefault(sid, {
                "session_id": sid, "title": str(a.get("title") or ""),
                "started_at": str(a.get("started_at") or ""),
                "seconds": 0.0, "n": 0,
            })
            cur["seconds"] += float(a.get("seconds") or 0.0)
            cur["n"] += int(a.get("n") or 0)
    if not sessions:
        for r in _profile_rows(profile):
            sid = str(r.get("session_id") or "")
            if not sid:
                continue
            cur = sessions.setdefault(sid, {
                "session_id": sid, "title": str(r.get("session_title") or ""),
                "started_at": str(r.get("started_at") or ""),
                "seconds": 0.0, "n": 0,
            })
    return sorted(sessions.values(),
                  key=lambda s: (str(s.get("started_at") or ""), s["session_id"]),
                  reverse=True)


def person_facts(conn: sqlite3.Connection, *, person_id: str = "",
                 name: str = "") -> dict:
    """档案头部/表格要的事实：首次、最近、场次、上麦时长、观点数。"""
    profile = claims.profile_any(conn, person_id=person_id, who=name)
    sessions = _session_facts(conn, person_id, profile)
    dates = [str(s.get("started_at") or "") for s in sessions if s.get("started_at")]
    seconds = sum(float(s.get("seconds") or 0.0) for s in sessions)
    return {
        "ok": True,
        "person_id": str(person_id or ""),
        "name": str(name or ""),
        "profile": profile,
        "sessions": sessions,
        "n_sessions": len(sessions),
        "n_claims": int(profile.get("n_claims") or 0),
        "first_at": _mmdd(min(dates)) if dates else "",
        "last_at": _mmdd(max(dates)) if dates else "",
        "seconds": round(seconds, 1),
        "latest": _latest_claims(profile),
    }


def _topic_block(g: dict) -> list[str]:
    """一个主题下的 Markdown：最新主张 + 改口 + 更早。"""
    items = list(g.get("items") or [])
    if not items:
        return []
    latest = dict(g.get("latest") or {})
    latest_id = str(latest.get("claim_id") or "")
    latest_item = next((r for r in items
                        if str(r.get("claim_id") or "") == latest_id), items[-1])
    stance = _clean(latest_item.get("stance") or latest_item.get("gist")
                    or latest.get("stance"))
    if not stance:
        return []
    out = ["### %s" % (claims.topic_label(str(g.get("topic") or ""))
                       or claims.GENERIC_LABEL), ""]
    line = "- **%s**（%s）" % (stance, _mmdd(latest_item.get("started_at")))
    gist = _clean(latest_item.get("gist") or "")
    if gist and not _same_text(gist, stance):
        line += "：%s" % gist
    out.append(line)
    quote = _truncate_quote(latest_item.get("quote"))
    if quote and not _same_text(quote, stance) and not _same_text(quote, gist):
        out.append("  > 「%s」" % quote)
    revisions = [r for r in (g.get("revisions") or []) if r.get("from") or r.get("to")]
    for rev in revisions[-2:]:
        out.append("- **改口**：%s 说「%s」→ %s 说「%s」。" % (
            _mmdd(rev.get("from_at") or rev.get("at")), _clean(rev.get("from")),
            _mmdd(rev.get("to_at") or rev.get("at")), _clean(rev.get("to"))))
    earlier = [r for r in items
               if str(r.get("claim_id") or "") != str(latest_item.get("claim_id") or "")]
    seen: set[str] = set()
    bits: list[str] = []
    for r in reversed(earlier):
        s = _clean(r.get("stance") or r.get("gist"))
        if not s or s in seen:
            continue
        # 改口那一句已经在上面写过，不再当「更早」重复
        if any(_same_text(s, rev.get("to")) or _same_text(s, rev.get("from"))
               for rev in revisions[-2:]):
            continue
        seen.add(s)
        bits.append("%s「%s」" % (_mmdd(r.get("started_at")), s))
        if len(bits) >= 3:
            break
    if bits:
        out.append("- 更早：" + "；".join(bits))
    out.append("")
    return out


def person_markdown(conn: sqlite3.Connection, *, person_id: str = "",
                    name: str = "") -> dict:
    """生成人物档案 Markdown。没有观点时 ``ok=False``，不产生空档案。"""
    name = str(name or "").strip()
    facts = person_facts(conn, person_id=person_id, name=name)
    profile = facts["profile"]
    if not facts["n_claims"]:
        return {"ok": False, "reason": "还没有观点记录", "name": name,
                "person_id": str(person_id or ""), "facts": facts}
    title = "%s · 观点档案" % (name or "这位上麦者")
    head = []
    if facts["first_at"]:
        head.append("首次：%s" % facts["first_at"])
    if facts["last_at"]:
        head.append("最近：%s" % facts["last_at"])
    head.append("场次：%d" % facts["n_sessions"])
    if facts["seconds"]:
        head.append("上麦：%s" % _dur(facts["seconds"]))
    head.append("观点：%d 条" % facts["n_claims"])
    out = ["# %s" % title, "", "> " + " ｜ ".join(head), ""]
    out += ["## 最近在讲什么", ""]
    for g in (profile.get("topics") or [])[:MAX_TOPICS]:
        out += _topic_block(g)
    if not any(line.startswith("### ") for line in out):
        # 有观点原子但全是空壳（理论上 extract 已经过滤）；不伪装成有内容。
        return {"ok": False, "reason": "观点还没有可读内容", "name": name,
                "person_id": str(person_id or ""), "facts": facts}
    out += ["## 出现场次", "", "| 日期 | 场次 | 时长 | 文档 |", "|---|---|---|---|"]
    for s in facts["sessions"][:MAX_SESSIONS]:
        url = session_doc_url(conn, str(s.get("session_id") or ""))
        doc = "[打开](%s)" % url if url else "未发布"
        secs = float(s.get("seconds") or 0.0)
        out.append("| %s | %s | %s | %s |" % (
            _mmdd(s.get("started_at")), _cell(s.get("title")),
            _dur(secs) if secs else "-", doc))
    out += ["", "---",
            "身份来自声纹聚类与人工补名，可能认错；以本人/主持确认为准。"]
    return {
        "ok": True,
        "name": name or "这位上麦者",
        "person_id": str(person_id or ""),
        "title": title,
        "markdown": "\n".join(out),
        "facts": facts,
        "n_claims": facts["n_claims"],
        "n_sessions": facts["n_sessions"],
        "latest": facts["latest"],
    }


# ---------------------------------------------------------------- 档案文档

def ensure_person_docs(conn: sqlite3.Connection) -> None:
    """人物文档缓存表。机器人可能先于引擎重启，读之前自己保证表在。"""
    conn.execute(_SCHEMA)
    conn.commit()


def _version(title: str, markdown: str) -> str:
    h = hashlib.sha256()
    h.update(str(title).encode("utf-8"))
    h.update(b"\0")
    h.update(str(markdown).encode("utf-8"))
    return h.hexdigest()[:16]


def _read_row(conn: sqlite3.Connection, key: str) -> dict | None:
    ensure_person_docs(conn)
    row = conn.execute("SELECT * FROM person_docs WHERE person_key=?",
                       (key,)).fetchone()
    return dict(row) if row else None


def _write_row(conn: sqlite3.Connection, key: str, name: str, token: str,
               url: str, version: str) -> None:
    ensure_person_docs(conn)
    conn.execute(
        "INSERT INTO person_docs(person_key,name,doc_token,doc_url,version,updated_at)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(person_key) DO UPDATE SET"
        " name=excluded.name, doc_token=excluded.doc_token,"
        " doc_url=excluded.doc_url, version=excluded.version,"
        " updated_at=excluded.updated_at",
        (key, name, token, url, version, store.now()))
    conn.commit()


def _doc_url(bot: Any, token: str) -> str:
    fn = getattr(bot, "_doc_url", None)
    if callable(fn):
        try:
            return str(fn(token) or "")
        except Exception:
            pass
    base = str((getattr(bot, "cfg", None) or {}).get("doc_url_base")
               or "https://feishu.cn/docx/").strip()
    return base.rstrip("/") + "/" + token if token else ""


def _call_rewrite(bot: Any, token: str, title: str, markdown: str,
                  share_with: str) -> dict:
    """兼容两种发布器写法：``rewrite_docx(id, md)`` / ``overwrite_docx(id, title, md)``。

    读侧不应该逼发布器改名；先按参数名判断，再退回本模块自带的实现。
    """
    fn = getattr(bot, "rewrite_docx", None) or getattr(bot, "overwrite_docx", None)
    if callable(fn):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        try:
            if "title" in params:
                return dict(fn(str(token), title, markdown, share_with=share_with) or {})
            return dict(fn(str(token), markdown) or {})
        except TypeError:
            try:
                return dict(fn(str(token), title, markdown) or {})
            except Exception as e:
                return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return _raw_rewrite(bot, token, markdown)


def _raw_rewrite(bot: Any, token: str, markdown: str) -> dict:
    """兜底覆盖写：Markdown 转块 → 清空根块 → 从 0 写回。

    发布器还没合并到当前分支时走这里；发布器有 ``rewrite_docx``/
    ``overwrite_docx`` 时不会到这一步。
    """
    client = getattr(bot, "client", None)
    if client is None:
        return {"ok": False, "error": "当前机器人不支持覆盖写"}
    try:
        from lark_oapi.api.docx.v1 import (
            BatchDeleteDocumentBlockChildrenRequest,
            BatchDeleteDocumentBlockChildrenRequestBody,
            CreateDocumentBlockDescendantRequest,
            CreateDocumentBlockDescendantRequestBody,
            ConvertDocumentRequest, ConvertDocumentRequestBody,
            GetDocumentBlockChildrenRequest)

        conv = (ConvertDocumentRequestBody.builder()
                .content_type("markdown").content(str(markdown)).build())
        resp = client.docx.v1.document.convert(
            ConvertDocumentRequest.builder().request_body(conv).build())
        if not resp.success():
            return {"ok": False, "error": "Markdown 转换失败：code=%s %s"
                    % (resp.code, resp.msg)}
        data = resp.data
        blocks = list(getattr(data, "blocks", None) or [])
        first = list(getattr(data, "first_level_block_ids", None) or [])
        if not blocks or not first:
            return {"ok": False, "error": "Markdown 转换没返回内容"}
        total, page_token = 0, ""
        for _ in range(50):
            builder = (GetDocumentBlockChildrenRequest.builder()
                       .document_id(str(token)).block_id(str(token))
                       .page_size(500))
            if page_token:
                builder = builder.page_token(page_token)
            child = client.docx.v1.document_block_children.list(builder.build())
            if not child.success():
                return {"ok": False, "error": "读取文档块失败：code=%s %s"
                        % (child.code, child.msg)}
            total += len(list(getattr(child.data, "items", None) or []))
            page_token = str(getattr(child.data, "page_token", "") or "")
            if not getattr(child.data, "has_more", False) or not page_token:
                break
        if total:
            body = (BatchDeleteDocumentBlockChildrenRequestBody.builder()
                    .start_index(0).end_index(total).build())
            cleared = client.docx.v1.document_block_children.batch_delete(
                BatchDeleteDocumentBlockChildrenRequest.builder()
                .document_id(str(token)).block_id(str(token))
                .request_body(body).build())
            if not cleared.success():
                return {"ok": False, "error": "清空旧正文失败：code=%s %s"
                        % (cleared.code, cleared.msg)}
        desc = (CreateDocumentBlockDescendantRequestBody.builder()
                .children_id(first).index(0).descendants(blocks).build())
        added = client.docx.v1.document_block_descendant.create(
            CreateDocumentBlockDescendantRequest.builder()
            .document_id(str(token)).block_id(str(token))
            .request_body(desc).build())
        if not added.success():
            return {"ok": False, "error": "写入正文失败：code=%s %s"
                    % (added.code, added.msg)}
        return {"ok": True, "blocks": len(blocks), "cleared": total}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def publish_person_doc(conn: sqlite3.Connection, bot: Any, *, person_id: str = "",
                       name: str = "", share_with: str = "") -> dict:
    """按需建/刷新一篇人物档案文档，返回档案数据 + 文档链接。

    ``bot`` 为 None 或建文档失败时仍返回 Markdown，由调用方决定是发文字还是
    明说「文档没建成」；不因为飞书失败让人物档案整个不可读。
    """
    md = person_markdown(conn, person_id=person_id, name=name)
    if not md.get("ok"):
        return md
    key = person_key(person_id, md["name"])
    title, markdown = md["title"], md["markdown"]
    version = _version(title, markdown)
    row = _read_row(conn, key)
    out = dict(md)
    out["person_key"] = key
    if row and row.get("doc_token") and row.get("version") == version:
        out.update({"doc_ok": True, "reused": True, "doc_url": row.get("doc_url") or "",
                    "doc_token": row.get("doc_token") or ""})
        return out
    if bot is None:
        out.update({"doc_ok": False, "error": "机器人不在，档案文档这次没建"})
        return out
    if row and row.get("doc_token"):
        res = _call_rewrite(bot, str(row["doc_token"]), title, markdown, share_with)
        if res.get("ok"):
            url = str(res.get("url") or _doc_url(bot, str(row["doc_token"])))
            _write_row(conn, key, md["name"], str(row["doc_token"]), url, version)
            out.update({"doc_ok": True, "updated": True, "doc_url": url,
                        "doc_token": str(row["doc_token"])})
        else:
            # 旧正文仍然可读：保留旧链接，明确说这次没刷新。
            out.update({"doc_ok": False, "doc_url": row.get("doc_url") or "",
                        "doc_token": str(row["doc_token"]),
                        "error": str(res.get("error") or "档案刷新失败")})
        logs.log_event("reading.person_doc", person_key=key,
                       updated=bool(res.get("ok")),
                       error="" if res.get("ok") else str(res.get("error") or "")[:200])
        return out
    try:
        created = bot.create_docx(title, markdown, share_with=str(share_with or ""))
    except Exception as e:
        traceback.print_exc()
        created = {"ok": False, "error": str(e)}
    token = str(created.get("document_id") or "")
    url = str(created.get("url") or _doc_url(bot, token))
    if token:
        # 正文失败但文档已建出来：也记下来，下次原地补写，不建第二篇。
        _write_row(conn, key, md["name"], token, url, version if created.get("ok") else "")
    out.update({"doc_ok": bool(created.get("ok")), "created": bool(created.get("ok")),
                "doc_url": url, "doc_token": token,
                "error": str(created.get("error") or "")})
    logs.log_event("reading.person_doc", person_key=key,
                   created=bool(created.get("ok")),
                   error="" if created.get("ok") else str(created.get("error") or "")[:200])
    return out


# ---------------------------------------------------------------- 检索辅助

def local_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict]:
    """服务没起来时的本地检索：先笔记单元，再逐字稿；返回与 /api/search 同形。

    刻意不做模型扩展，只做和接口一致的字段映射；拿不到就返回空，不拼假结果。
    """
    q = str(query or "").strip()
    if not q:
        return []
    out: list[dict] = []
    try:
        units = store.search_note_units(conn, [q], limit=limit)
    except sqlite3.OperationalError:
        units = []
    for u in units:
        out.append({
            "kind": str(u.get("kind") or "point"),
            "id": int(u.get("id") or 0),
            "session_id": str(u.get("session_id") or ""),
            "session_title": str(u.get("title") or ""),
            "t_start": u.get("t"),
            "speaker_label": str(u.get("speaker") or ""),
            "text": str(u.get("text") or ""),
        })
    if len(out) < limit:
        try:
            rows = store.search_all(conn, q, limit)
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            out.append({
                "kind": "utterance",
                "id": int(r.get("id") or 0),
                "session_id": str(r.get("session_id") or ""),
                "session_title": str(r.get("title") or ""),
                "t_start": r.get("t_start"),
                "speaker_label": str(r.get("speaker_label") or ""),
                "text": str(r.get("text") or ""),
                "started_at": str(r.get("started_at") or ""),
            })
    return out[:limit]
