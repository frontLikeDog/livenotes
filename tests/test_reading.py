# -*- coding: utf-8 -*-
"""读侧：人物/观点 Markdown、/找人 /搜 /问、补名卡。

这一片不测 UI 截图，只测「手机上看到的那几段字」和「按钮按下去真的会写库」：

1. 人物档案：按主题排主张、标改口、给出没场次；没有观点时不生成空档案；
2. 文档：第一次创建、版本没变复用、版本变了原地重写；失败保留旧链接；
3. 场次文档链接：发布台账有就带，没有就明确写「未发布」；
4. 命令：/找人 /搜 /问 的输出与文档链接；/补名 的名单、试听、表单卡与文字兜底。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import tools.feishu_bot as fb
from app import claims, feishu, reading, store


# ---------------------------------------------------------------- 夹具

class FakeBot:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.chat_texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.chat_cards: list[tuple[str, dict]] = []
        self.audios: list[tuple[str, int]] = []
        self.created: list[tuple[str, str]] = []
        self.rewritten: list[str] = []
        self.create_ok = True
        self.rewrite_ok = True

    def send_text(self, text: str, open_id: str = "") -> bool:
        self.texts.append((open_id, text))
        return True

    def send_card(self, card: dict, open_id: str = "") -> bool:
        self.cards.append((open_id, card))
        return True

    def send_text_to_chat(self, chat_id: str, text: str) -> bool:
        self.chat_texts.append((chat_id, text))
        return True

    def send_card_to_chat(self, chat_id: str, card: dict) -> bool:
        self.chat_cards.append((chat_id, card))
        return True

    def send_audio(self, path, duration_ms: int, open_id: str = "") -> bool:
        self.audios.append((str(path), int(duration_ms)))
        return True

    def send_audio_to_chat(self, chat_id: str, path, duration_ms: int) -> bool:
        self.audios.append((str(path), int(duration_ms)))
        return True

    def create_docx(self, title: str, markdown: str, share_with: str = "") -> dict:
        self.created.append((title, share_with))
        if not self.create_ok:
            return {"ok": False, "error": "租户没开 docx"}
        return {"ok": True, "document_id": "doc_1",
                "url": "https://feishu.cn/docx/doc_1"}

    def rewrite_docx(self, document_id: str, markdown: str) -> dict:
        self.rewritten.append(document_id)
        if not self.rewrite_ok:
            return {"ok": False, "error": "写不进去"}
        return {"ok": True, "url": "https://feishu.cn/docx/doc_1"}


@pytest.fixture(autouse=True)
def clean():
    fb._NAMING.clear()
    fb._SESSION_CACHE.clear()
    fb._HINT_DAY.clear()
    fb._PENDING_CHOICE.clear()
    fb._LAST_CHOICE_ROOM.clear()
    yield
    fb._NAMING.clear()
    fb._SESSION_CACHE.clear()


@pytest.fixture
def bot(monkeypatch):
    b = FakeBot()
    monkeypatch.setattr(fb, "_BOT", b, raising=False)
    return b


def _room(rid="rm1", ch="CH01", sid="S1"):
    return {"id": rid, "seq": int(ch[-2:]), "state": "live", "name": "一号",
            "session_id": sid, "capture_engine": "direct"}


def _session(db, sid, started_at, title, speakers, points, topics=()):
    """一场：speakers = [(spk_id, display, person_id, state)]。"""
    store.insert_session(db, {"id": sid, "title": title, "started_at": started_at,
                              "duration_s": 1800, "source_mode": "tab"})
    store.insert_utterances(db, [
        {"session_id": sid, "speaker_id": spk, "speaker_label": disp,
         "t_start": 10.0 * i, "t_end": 10.0 * i + 8, "text": "第%d句" % i,
         "category": "substantive",
         "topic": (topics[i % len(topics)] if topics else "")}
        for i, (spk, disp, _pid, _st) in enumerate(speakers)])
    for spk, disp, pid, state in speakers:
        store.upsert_speaker(db, sid, spk, disp)
        if pid:
            store.set_speaker_person(db, sid, spk, pid, 0.4, 0.1, state)
    store.save_summary(db, sid, "content", {}, "extractive", {
        "session_title": title, "overview": "", "topics": list(topics),
        "points": points,
    })
    claims.rebuild_session(db, sid)


def _point(who, stance, topic, uid, t=10.0, cat="substantive", quote=""):
    return {"title": "话题：%s" % topic, "gist": "%s 的展开" % stance,
            "t_from": t, "t_to": t + 8, "speakers": [who],
            "claim_speaker": who, "stance": stance,
            "evidence": [{"id": uid, "t_start": t, "speaker_label": who,
                          "category": cat, "topic": topic,
                          "quote": quote or (stance + "，这是原话")}]}


def _two_sessions(db):
    _session(db, "S1", "2026-11-18T20:00:00", "中东工具市场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "先做中东，客单价高值得试", "AI 出海", 1,
                     quote="中东客单价高，值得试。")],
             topics=("AI 出海",))
    _session(db, "S2", "2026-11-25T20:00:00", "今晚聊 AI 出海",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "先做东南亚，冷启动快", "AI 出海", 2,
                     quote="你在中东买量，先亏三个月，还不一定回本。")],
             topics=("AI 出海",))
    store.upsert_person(db, "per_a", "阿哲")


def _publish_table(db, rows=()):
    db.execute(
        "CREATE TABLE IF NOT EXISTS feishu_publish ("
        " session_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'session',"
        " state TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',"
        " doc_token TEXT NOT NULL DEFAULT '', doc_url TEXT NOT NULL DEFAULT '',"
        " record_id TEXT NOT NULL DEFAULT '', updated_at TEXT,"
        " PRIMARY KEY (session_id, kind))")
    for sid, url in rows:
        db.execute("INSERT OR REPLACE INTO feishu_publish"
                   "(session_id,kind,state,doc_url,updated_at)"
                   " VALUES(?,'session','ok',?,?)", (sid, url, store.now()))
    db.commit()


# ---------------------------------------------------------------- 人物档案 Markdown

def test_person_markdown_has_topics_revision_and_sessions(db):
    _two_sessions(db)
    out = reading.person_markdown(db, person_id="per_a", name="阿哲")
    assert out["ok"] is True
    md = out["markdown"]
    assert md.startswith("# 阿哲 · 观点档案")
    assert "首次：11-18" in md and "最近：11-25" in md
    assert "### AI 出海" in md
    assert "先做东南亚，冷启动快" in md
    assert "你在中东买量" in md                      # 原话
    assert "11-18 说「先做中东" in md and "11-25 说「先做东南亚" in md
    assert "## 出现场次" in md
    assert "| 11-25 | 今晚聊 AI 出海 |" in md
    assert "身份来自声纹聚类与人工补名" in md         # 免责声明
    assert out["latest"][0]["stance"].startswith("先做东南亚")


def test_person_markdown_marks_unpublished_session(db):
    _two_sessions(db)
    md = reading.person_markdown(db, person_id="per_a", name="阿哲")["markdown"]
    assert "| 未发布 |" in md                  # 没发布台账时明确写未发布，不造链接


def test_person_markdown_refuses_empty_archive(db):
    _session(db, "S0", "2026-11-01T20:00:00", "空场",
             [("SPK_00", "阿哲", "per_a", "confirmed")], [], topics=())
    out = reading.person_markdown(db, person_id="per_a", name="阿哲")
    assert out["ok"] is False
    assert "观点" in out["reason"]
    assert "markdown" not in out


def test_person_markdown_accepts_nickname_only(db):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [("SPK_00", "阿哲", "", "")],
             [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1)],
             topics=("OPC 政策",))
    out = reading.person_markdown(db, name="阿哲")
    assert out["ok"] is True
    assert out["person_id"] == ""
    assert "# 阿哲 · 观点档案" in out["markdown"]
    assert "未发布" in out["markdown"]


def test_resolve_person_from_claims_when_persons_row_missing(db):
    _two_sessions(db)
    db.execute("DELETE FROM persons")
    db.commit()
    who = reading.resolve_person(db, "阿哲")
    assert who["ok"] is True and who["kind"] == "person"
    assert who["person_id"] == "per_a"


def test_profile_any_merges_person_and_nickname_rows(db):
    _two_sessions(db)
    db.execute(
        "INSERT INTO claims(claim_id,session_id,person_id,who,topic,stance,gist,"
        "quote,t,kind,evidence_json,created_at) VALUES"
        "('clm_old','S1','','阿哲','AI 出海','更早的说法','','',1,'claim','[]',?)",
        (store.now(),))
    db.commit()
    one = claims.profile(db, person_id="per_a")
    both = claims.profile_any(db, person_id="per_a", who="阿哲")
    assert one["n_claims"] == 2
    assert both["n_claims"] == 3


# ---------------------------------------------------------------- 场次文档链接

def test_session_doc_url_reads_ledger_and_tolerates_missing_table(db):
    assert reading.session_doc_url(db, "S1") == ""
    _publish_table(db, [("S1", "https://feishu.cn/docx/abc")])
    assert reading.session_doc_url(db, "S1") == "https://feishu.cn/docx/abc"
    assert reading.session_doc_url(db, "S2") == ""


# ---------------------------------------------------------------- 档案文档缓存

def test_person_doc_creates_once_then_reuses(db, bot):
    _two_sessions(db)
    first = reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲",
                                       share_with="ou_1")
    assert first["doc_ok"] is True and first["doc_url"].endswith("doc_1")
    assert bot.created == [("阿哲 · 观点档案", "ou_1")]
    second = reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲",
                                        share_with="ou_1")
    assert second["doc_ok"] is True and second["reused"] is True
    assert len(bot.created) == 1 and bot.rewritten == []


def test_person_doc_rewrites_when_version_changes(db, bot):
    _two_sessions(db)
    reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲")
    db.execute("UPDATE person_docs SET version='stale'")
    db.commit()
    out = reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲")
    assert out["doc_ok"] is True and out["updated"] is True
    assert bot.rewritten == ["doc_1"]
    assert len(bot.created) == 1


def test_person_doc_keeps_old_link_when_rewrite_fails(db, bot):
    _two_sessions(db)
    reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲")
    db.execute("UPDATE person_docs SET version='stale'")
    db.commit()
    bot.rewrite_ok = False
    out = reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲")
    assert out["doc_ok"] is False
    assert out["doc_url"].endswith("doc_1")          # 旧正文仍然可读
    assert "写不进去" in out["error"]


def test_person_doc_create_failure_still_returns_markdown(db, bot):
    _two_sessions(db)
    bot.create_ok = False
    out = reading.publish_person_doc(db, bot, person_id="per_a", name="阿哲")
    assert out["ok"] is True and out["doc_ok"] is False
    assert "阿哲 · 观点档案" in out["markdown"]
    assert "租户没开 docx" in out["error"]


# ---------------------------------------------------------------- /找人

def test_cmd_find_person_returns_doc_and_latest_claims(db, bot):
    _two_sessions(db)
    text = fb.cmd_find_person("阿哲", "ou_1")
    assert "阿哲 · 观点档案" in text
    assert "https://feishu.cn/docx/doc_1" in text
    assert "先做东南亚" in text
    assert len(bot.created) == 1 and bot.created[0][1] == "ou_1"


def test_cmd_find_person_without_claims_does_not_build_archive(db, bot):
    _session(db, "S0", "2026-11-01T20:00:00", "空场",
             [("SPK_00", "阿哲", "per_a", "confirmed")], [])
    store.upsert_person(db, "per_a", "阿哲")
    text = fb.cmd_find_person("阿哲", "ou_1")
    assert "还没有观点记录" in text
    assert bot.created == []


def test_cmd_find_person_lists_known_people(db, bot):
    _two_sessions(db)
    text = fb.cmd_find_person("", "ou_1")
    assert "记得的人" in text and "阿哲" in text
    assert "/找人 阿哲" in text


# ---------------------------------------------------------------- /搜

def test_cmd_search_marks_doc_or_unpublished(db, bot, monkeypatch):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1)],
             topics=("OPC 政策",))
    _session(db, "S2", "2026-11-16T20:00:00", "第二场",
             [("SPK_00", "小北", "", "")],
             [_point("小北", "其实是门槛被抬高", "OPC 政策", 2)],
             topics=("OPC 政策",))
    _publish_table(db, [("S1", "https://feishu.cn/docx/s1")])

    rows = [
        {"kind": "point", "session_id": "S1", "session_title": "第一场",
         "t_start": 30, "speaker_label": "阿哲", "text": "明年 OPC 会被收紧"},
        {"kind": "utterance", "session_id": "S2", "session_title": "第二场",
         "t_start": 60, "speaker_label": "小北", "text": "其实是门槛被抬高"},
    ]
    monkeypatch.setattr(fb, "_api", lambda *a, **k: {"rows": rows, "expanded": []})
    text = fb.cmd_search("OPC", "ou_1")
    assert "找到 2 条" in text
    assert "https://feishu.cn/docx/s1" in text
    assert "未发布" in text
    assert "0:30" in text and "1:00" in text


def test_cmd_search_without_keyword_shows_usage():
    assert "用法" in fb.cmd_search("")


def test_local_search_falls_back_to_notes(db):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1)],
             topics=("OPC 政策",))
    store.save_summary(db, "S1", "content", {}, "extractive", {
        "session_title": "第一场", "overview": "",
        "topics": ["OPC 政策"],
        "points": [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1)]})
    from app import notes
    notes.index_session(db, "S1")
    rows = reading.local_search(db, "OPC", limit=10)
    assert rows and any(r["session_id"] == "S1" for r in rows)


# ---------------------------------------------------------------- /问

def test_qa_text_formats_answer_citations_and_docs(db, bot):
    _session(db, "S1", "2026-11-02T20:00:00", "第一场",
             [("SPK_00", "阿哲", "per_a", "confirmed")],
             [_point("阿哲", "明年 OPC 会被收紧", "OPC 政策", 1)],
             topics=("OPC 政策",))
    _publish_table(db, [("S1", "https://feishu.cn/docx/s1")])
    text = fb._qa_text({
        "ok": True, "state": "ready",
        "answer": "阿哲认为 [1] 明年 OPC 会收紧。",
        "citations": [{"n": 1, "session_id": "S1", "title": "第一场",
                       "speaker": "阿哲", "t_text": "00:30",
                       "text": "明年 OPC 会被收紧"}],
        "followups": [{"q": "小北怎么看？", "why": "反方"}],
    })
    assert "💡 阿哲认为" in text
    assert "[1] 阿哲 · 《第一场》 00:30" in text
    assert "https://feishu.cn/docx/s1" in text
    assert "还可以问：小北怎么看？" in text


def test_qa_text_no_result_and_failure():
    assert "没找到相关内容" in fb._qa_text({"state": "no_result"})
    assert "问答没成功" in fb._qa_text({"state": "failed", "error": "还没配置大模型"})


def test_cmd_ask_accepts_room_scope(monkeypatch):
    calls: list = []
    monkeypatch.setattr(fb, "_rooms", lambda: [_room()])
    monkeypatch.setattr(fb, "_spawn_qa",
                        lambda *a: calls.append(a))
    out = fb.cmd_ask("CH01 这场谁反对出海？", "ou_1")
    assert "正在查笔记" in out
    assert calls and calls[0][2] == "这场谁反对出海？"   # fn, err_to, ... args
    assert calls[0][3] == "S1"


# ---------------------------------------------------------------- 补名

def _top_payload():
    return {
        "ok": True, "session_id": "S1", "title": "今晚聊 AI 出海",
        "has_audio": True,
        "speakers": [
            {"speaker_id": "SPK_00", "label": "本场第 1 位上麦者",
             "named": False, "seconds": 600, "n_utterances": 30,
             "preview": {"t_start": 10, "t_end": 13, "seconds": 3},
             "suggestion": {"name": "阿哲", "person_id": "per_a", "score": 0.5}},
            {"speaker_id": "SPK_01", "label": "本场第 2 位上麦者",
             "named": True, "seconds": 300, "n_utterances": 15},
            {"speaker_id": "SPK_02", "label": "本场第 3 位上麦者",
             "named": False, "seconds": 200, "n_utterances": 10,
             "preview": {"t_start": 40, "t_end": 43, "seconds": 3}},
        ],
    }


def _pending():
    return fb._naming_pending("S1", "今晚聊 AI 出海", _top_payload(),
                              "ou_1", "", "p2p")


def test_name_pairs_parses_forms_and_rejects_chatter():
    assert fb._name_pairs("1 阿哲 2 小北") == [("1", "阿哲"), ("2", "小北")]
    assert fb._name_pairs("1=阿哲, 2=小北") == [("1", "阿哲"), ("2", "小北")]
    assert fb._name_pairs("A 阿哲 B 小北") == [("A", "阿哲"), ("B", "小北")]
    assert fb._name_pairs("1 个问题 怎么办") == []


def test_naming_card_has_form_and_text_fallback():
    pending = _pending()
    card = fb._naming_card(pending)
    body = json.dumps(card, ensure_ascii=False)
    assert '"tag": "form"' in body
    assert '"name": "name_a"' in body and '"name": "name_b"' in body
    assert '"action_type": "form_submit"' in body
    assert pending["nonce"] in body
    assert "1 阿哲 2 小北" in body                     # 卡片失效时的文字兜底
    assert "声纹建议：阿哲" in body                     # 可信建议预填提示
    assert len(pending["speakers"]) == 2                # 只列未命名的两位


def test_naming_from_text_maps_numbers_and_letters():
    pending = _pending()
    fb._naming_put(pending)
    got, names, err = fb._naming_from_text("1 阿哲 3 小北", "ou_1", "")
    assert got is pending and not err
    assert names == {"SPK_00": "阿哲", "SPK_02": "小北"}
    got, names, err = fb._naming_from_text("B 小北", "ou_1", "")
    assert names == {"SPK_02": "小北"}
    got, names, err = fb._naming_from_text("随便聊聊", "ou_1", "")
    assert got is None and names == {}


def test_naming_from_form_skips_empty_fields():
    pending = _pending()
    fb._naming_put(pending)
    got, names, err = fb._naming_from_form({
        "nonce": pending["nonce"],
        "_form": {"name_a": "阿哲", "name_b": ""}})
    assert got is pending and not err
    assert names == {"SPK_00": "阿哲"}


def test_handle_card_naming_submit_spawns_apply(monkeypatch):
    pending = _pending()
    fb._naming_put(pending)
    calls: list = []
    monkeypatch.setattr(fb, "_naming_apply", lambda *a: calls.append(a))

    class InlineThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(fb.threading, "Thread", InlineThread)
    toast, card = fb.handle_card({
        "act": "naming_submit", "nonce": pending["nonce"],
        "_form": {"name_a": "阿哲", "name_b": "小北"}}, "ou_1")
    assert "正在保存" in toast and card is None
    assert calls and calls[0][1] == {"SPK_00": "阿哲", "SPK_02": "小北"}


def test_handle_card_naming_cancel_drops_pending():
    pending = _pending()
    fb._naming_put(pending)
    toast, _card = fb.handle_card(
        {"act": "naming_cancel", "nonce": pending["nonce"]}, "ou_1")
    assert "不补名" in toast
    assert fb._naming_get(pending["nonce"]) is None


def test_cmd_name_direct_pairs_calls_name_api(db, bot, monkeypatch):
    calls: list = []
    monkeypatch.setattr(fb, "_rooms", lambda: [_room()])
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: _top_payload())
    monkeypatch.setattr(fb, "_api", lambda path, payload=None, timeout=6.0:
                        calls.append((path, payload)) or
                        {"ok": True, "enrolled": True})
    assert fb.cmd_name("CH01 1 阿哲 3 小北", "ou_1") == ""
    assert [c[1]["name"] for c in calls] == ["阿哲", "小北"]
    assert calls[0][0] == "/api/speakers/name"
    assert bot.texts and "已补名" in bot.texts[-1][1]
    assert "声纹已入库" in bot.texts[-1][1]


def test_cmd_name_sends_previews_and_form_card(db, bot, monkeypatch):
    monkeypatch.setattr(fb, "_rooms", lambda: [_room()])
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: _top_payload())
    monkeypatch.setattr(fb.clips, "make_clip", lambda *a, **k: (None, "没音频"))
    assert fb.cmd_name("CH01", "ou_1") == ""
    joined = "\n".join(t for _oid, t in bot.texts)
    assert "还有 2 位没名字" in joined
    assert "A · 本场第 1 位" in joined
    assert "B · 本场第 3 位" in joined
    assert bot.cards and bot.cards[-1][1]["elements"][-1]["tag"] == "form"
    assert len(fb._NAMING) == 1


def test_cmd_name_sends_audio_when_preview_available(db, bot, monkeypatch):
    class Clip:
        path = "x.opus"
        duration = 3.0

    monkeypatch.setattr(fb, "_rooms", lambda: [_room()])
    monkeypatch.setattr(fb, "_top_speakers", lambda sid: _top_payload())
    monkeypatch.setattr(fb.clips, "make_clip", lambda *a, **k: (Clip(), ""))
    fb.cmd_name("CH01", "ou_1")
    assert len(bot.audios) == 2                       # A 和 C 两段试听
    assert bot.audios[0][1] == 3000


def test_route_registers_read_side_commands(monkeypatch):
    monkeypatch.setattr(fb, "cmd_find_person", lambda *a: "F")
    monkeypatch.setattr(fb, "cmd_search", lambda *a: "S")
    monkeypatch.setattr(fb, "cmd_ask", lambda *a: "Q")
    monkeypatch.setattr(fb, "cmd_name", lambda *a: "N")
    assert fb._route("/找人 阿哲", "/找人 阿哲", "ou") == "F"
    assert fb._route("/搜 OPC", "/搜 opc", "ou") == "S"
    assert fb._route("/问 阿哲怎么看", "/问 阿哲怎么看", "ou") == "Q"
    assert fb._route("/补名 CH01", "/补名 ch01", "ou") == "N"


def test_route_accepts_naming_text_fallback(monkeypatch):
    pending = _pending()
    fb._naming_put(pending)
    calls: list = []
    monkeypatch.setattr(fb, "_naming_apply", lambda *a: calls.append(a))

    class InlineThread:
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(fb.threading, "Thread", InlineThread)
    assert fb._route("1 阿哲 3 小北", "1 阿哲 3 小北", "ou_1") == ""
    assert calls and calls[0][1] == {"SPK_00": "阿哲", "SPK_02": "小北"}


# ---------------------------------------------------------------- 卡片回调形状

def test_card_event_value_merges_form_value():
    event = SimpleNamespace(action=SimpleNamespace(
        value={"act": "naming_submit", "nonce": "n1"},
        form_value={"name_a": "阿哲"}))
    value = feishu.card_event_value(event)
    assert value["act"] == "naming_submit" and value["nonce"] == "n1"
    assert value["_form"] == {"name_a": "阿哲"}
    plain = SimpleNamespace(action=SimpleNamespace(value={"act": "x"}))
    assert feishu.card_event_value(plain) == {"act": "x"}
