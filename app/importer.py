# -*- coding: utf-8 -*-
"""云端导入包：把云采集节点的场次带回本地引擎。

云端只有「仅采集」，没有公网页面；它产出的东西能带出来的只有两种：

- 飞书机器人 `/分享包` 发的 zip（``meta.json`` + ``transcript.srt`` +
  可选 ``audio.m4a`` + ``bookmarks.json``）；
- 云空间场次文件夹里的逐字稿 txt（``export.to_archive_txt`` 的格式）。

这里把这两种文件还原成一场本地能阅读、回放、补名、总结的场次。

边界必须如实写进会话，界面也要照实说：

- 导入的是**成品文字**，没有 ``utterance_emb``：不能重新分人，补名只改本场
  显示，不会注册声纹，也不会在以后的场次里自动认人；
- 不重新转写：``capture_only`` 保持 0，离线补转写不会把它当成待补的仅采集
  场次，也就不会拿导入的文字再跑一遍 ASR；
- 同一份来源重复导入是幂等的（场次 id 由来源场次 id / 文本指纹决定）；
  只有显式 ``replace`` 才覆盖，而且会清掉本机为这一场生成的总结、笔记和打点。
  已发布到飞书的文档记录（``feishu_publish``）保留，重发时更新同一篇。

不做「本地抽取兜底」：解析不出任何一句话就明确失败，不硬造一场空场次。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import wave
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import analysis, audio, logs, notes, store
from .config import AUDIO_DIR
from .timefmt import CN_TZ

#: 上传/导入的硬上限。分享包里的 m4a 32kbps 约 14MB/小时，1GB 足够长场次；
#: 超过就明确拒绝，不在请求线程里把整包读进内存。
MAX_UPLOAD_BYTES = 1024 ** 3
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024
MAX_META_BYTES = 1 * 1024 * 1024
MAX_BOOKMARKS_BYTES = 4 * 1024 * 1024
MAX_ROWS = 300_000
MAX_TEXT = 5000
MAX_LABEL = 40
MAX_MARKS = 500

AUDIO_SUFFIXES = (".m4a", ".mp4", ".wav", ".mp3", ".aac", ".ogg", ".opus")

_TAG_RE = re.compile(r"<[^>]{1,40}>")
#: ``[HH:MM:SS] 正文`` / ``[MM:SS] 正文``；方括号可省略。
_TS_LINE = re.compile(r"^\[?(\d{1,2}):(\d{2})(?::(\d{2}))?\]?\s*(.*)$")
#: 来源逐字稿里的「本场第 3 位上麦者」是位置标签，不是昵称。
_POSITIONAL_IMPORT = re.compile(r"^本场第\s*(\d+)\s*位上麦者$")

NO_VOICEPRINT_WARNING = (
    "这一场是云端导入：没有保存声纹，不能重新分人；补名只改本场显示，"
    "不会自动认人。")
NAMING_HINT = "导入场次没有声纹：补名只改本场显示，不会注册声纹、下次也不会自动认。"


class ImportProblem(ValueError):
    """可展示给用户的导入失败：文件不对、内容为空、超过上限。"""


# ---------------------------------------------------------------- 文本解析

def _clean_text(text: str) -> str:
    s = _TAG_RE.sub("", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:MAX_TEXT]


def _hms_parts(h: str, m: str, s: str | None) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s or 0)


def _parse_srt_ts(raw: str) -> float | None:
    """``00:01:02,500`` / ``00:01:02.500`` / ``01:02.5`` -> 秒。"""
    t = str(raw or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        parts = t.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, TypeError):
        return None


def parse_srt(text: str) -> tuple[list[dict], int]:
    """解析 SRT，返回 (rows, 跳过的块数)。块格式坏了就跳过并计数，不猜时间码。"""
    text = str(text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    rows: list[dict] = []
    skipped = 0
    for block in re.split(r"\n\s*\n", text):
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        idx = None
        for i, ln in enumerate(lines):
            if "-->" in ln:
                idx = i
                break
        if idx is None:
            skipped += 1
            continue
        left, _, right = lines[idx].partition("-->")
        t0 = _parse_srt_ts(left)
        t1 = _parse_srt_ts(right.split()[0] if right.split() else "")
        body = _clean_text(" ".join(lines[idx + 1:]))
        if t0 is None or t1 is None or not body:
            skipped += 1
            continue
        rows.append({"t_start": t0, "t_end": max(t1, t0 + 0.3), "text": body})
    return rows, skipped


def _parse_dur(seg: str) -> float:
    """``2 小时 5 分`` / ``3 分钟`` -> 秒。读不出来就 0（后面按正文尾部兜底）。"""
    hours = re.search(r"(\d+)\s*小时", seg)
    mins = re.search(r"(\d+)\s*分", seg)
    if not hours and not mins:
        return 0.0
    return float((int(hours.group(1)) if hours else 0) * 3600
                 + (int(mins.group(1)) if mins else 0) * 60)


def _norm_started(raw: str) -> str:
    """来源开始时间 -> 本地库里统一的 naive ISO（北京墙上时间）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(CN_TZ)
    return dt.replace(tzinfo=None).isoformat(timespec="seconds")


def parse_archive_txt(text: str) -> dict[str, Any]:
    """解析 ``to_archive_txt`` / ``to_txt`` 的逐字稿，抬头有就取，没有就用正文。"""
    text = str(text or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    meta: dict[str, Any] = {"title": "", "started_at": "", "src_sid": "",
                            "duration_s": 0.0}
    entries: list[tuple[float, str]] = []
    skipped = 0
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            head = line.lstrip("# ").strip()
            m = re.search(r"·「(.*)」\s*$", head)
            if m:
                meta["title"] = m.group(1).strip()
                head = head[:m.start()].strip()
            stamp = _norm_started(head)
            if stamp:
                meta["started_at"] = stamp
            continue
        if line.startswith("场次："):
            meta["src_sid"] = line.split("：", 1)[1].strip()
            continue
        if line.startswith("时长："):
            seg = line.split("：", 1)[1].split("·")[0].strip()
            meta["duration_s"] = _parse_dur(seg)
            continue
        if line.startswith("导出："):
            continue
        m = _TS_LINE.match(line)
        if not m:
            skipped += 1
            continue
        t0 = _hms_parts(m.group(1), m.group(2), m.group(3))
        body = _clean_text(m.group(4))
        if not body:
            skipped += 1
            continue
        entries.append((t0, body))
    rows: list[dict] = []
    for i, (t0, body) in enumerate(entries):
        nxt = entries[i + 1][0] if i + 1 < len(entries) else None
        end = t0 + max(0.8, min(20.0, 0.22 * len(body)))
        if nxt is not None:
            end = min(end, max(t0 + 0.3, nxt))
        rows.append({"t_start": t0, "t_end": end, "text": body})
    return {"meta": meta, "rows": rows, "skipped": skipped}


# ---------------------------------------------------------------- 说话人

def _split_prefix(text: str) -> tuple[str, str]:
    """把 ``主播 · 阿哲（疑似）：正文`` 拆成 (前缀, 正文)。

    只认「第一个冒号前面像名字」的情况：全角冒号，或带空格的半角冒号；
    前缀里不能有句读，不能有 URL 的斜杠。是否真是说话人由整篇的重复度决定。
    """
    for sep in ("：", ": "):
        i = text.find(sep)
        if i <= 0 or i > 40:
            continue
        who = text[:i].strip().lstrip("-—·• ").strip()
        rest = text[i + len(sep):].lstrip()
        if not who or not rest or len(who) > 24:
            continue
        if "/" in who or "http" in who.lower():
            continue
        if any(ch in who for ch in "。！？!?，,、；;…"):
            continue
        return who, rest
    return "", text


def _assign_speakers(texts: list[str]) -> tuple[list[dict], list[tuple[str, str]]]:
    """按整篇的重复度判断前缀是不是说话人，再给每句落到稳定的 SPK_NN。

    单看一行分不清「阿哲：今天讲什么」和「结论：先做 A」。所以先统计：
    每一行都有前缀、且前缀总数不多 → 这确实是逐字稿的说话人列；否则只认
    出现 ≥2 次的前缀，其余冒号当正文标点。
    """
    splits = [_split_prefix(t) for t in texts]
    counts = Counter(who for who, _ in splits if who)
    with_who = sum(1 for who, _ in splits if who)
    keep: set[str]
    if texts and with_who / max(1, len(texts)) >= 0.6 and len(counts) <= 12:
        keep = set(counts)
    else:
        keep = {who for who, n in counts.items() if n >= 2}
    rows: list[dict] = []
    order: list[str] = []
    for (who, rest), raw in zip(splits, texts):
        kept = who in keep
        label = who if kept else ""
        # 没被认成说话人的前缀不是名字，而是正文里的「结论：」，要原样留在句子里。
        text = rest if kept else raw
        m = _POSITIONAL_IMPORT.match(label)
        if m:
            label = "说话人%s" % m.group(1)
        label = label[:MAX_LABEL]
        rows.append({"label": label, "text": text})
        if label not in order:
            order.append(label)
    ids = {label: "SPK_%02d" % i for i, label in enumerate(order)}
    for row in rows:
        row["speaker_id"] = ids[row["label"]]
    speakers = [(ids[label], label or "说话人") for label in order]
    return rows, speakers


# ---------------------------------------------------------------- 文件解析

def _find_member(names: list[str], target: str) -> str | None:
    hit = [n for n in names if n == target or n.endswith("/" + target)]
    if not hit:
        return None
    return sorted(hit, key=len)[0]


def _read_member(zf: zipfile.ZipFile, name: str, limit: int) -> bytes:
    try:
        size = zf.getinfo(name).file_size
    except KeyError:
        return b""
    if size > limit:
        raise ImportProblem("「%s」解压后 %d MB，超过 %d MB 上限"
                            % (name, size // 1048576, max(1, limit // 1048576)))
    with zf.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ImportProblem("「%s」解压后超过 %d MB 上限"
                            % (name, max(1, limit // 1048576)))
    return data


def _copy_member(zf: zipfile.ZipFile, name: str, dst: Path) -> str:
    """流式解出一段录音，不整包进内存；超过上限就明确失败。"""
    total = 0
    try:
        with zf.open(name) as src, open(dst, "wb") as out:
            while True:
                buf = src.read(262144)
                if not buf:
                    break
                total += len(buf)
                if total > MAX_UPLOAD_BYTES:
                    raise ImportProblem("分享包里的录音超过 1 GB，请先在云端切片")
                out.write(buf)
    except ImportProblem:
        dst.unlink(missing_ok=True)
        raise
    if total <= 0:
        dst.unlink(missing_ok=True)
        return ""
    return dst.name


def _safe_num(v: Any) -> float:
    try:
        return max(0.0, float(v or 0))
    except (TypeError, ValueError):
        return 0.0


def _clean_marks(data: Any) -> list[dict]:
    """只收打点/书签；高光是算出来的，导进来会变成假的「当时标过」。"""
    out: list[dict] = []
    for item in (data if isinstance(data, list) else []):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind == "highlight":
            continue
        try:
            t = max(0.0, float(item.get("t") or 0))
        except (TypeError, ValueError):
            continue
        out.append({"t": t, "note": str(item.get("note") or "").strip()[:200],
                    "kind": "bookmark" if kind == "bookmark" else "mark"})
        if len(out) >= MAX_MARKS:
            break
    return out


def parse_package(path: Path, tmpdir: Path) -> dict[str, Any]:
    """解析分享包 zip；录音解到调用方给的临时目录，由调用方负责清理。

    返回的 ``warnings`` 是「包本身的问题」（缺文件、被跳过），和入库时
    叠加的会话说明（没有声纹、没有录音）分开，阅读方不需要再猜。
    """
    warns: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        meta_name = _find_member(names, "meta.json")
        srt_name = _find_member(names, "transcript.srt")
        if not srt_name:
            raise ImportProblem("分享包里没有 transcript.srt，无法还原逐字稿")
        meta: dict[str, Any] = {}
        if meta_name:
            try:
                meta = json.loads(_read_member(zf, meta_name, MAX_META_BYTES)
                                  .decode("utf-8", "replace") or "{}")
            except ValueError:
                warns.append("meta.json 解析不了，标题和时间按文件名和导入时刻记录。")
                meta = {}
            if not isinstance(meta, dict):
                warns.append("meta.json 不是对象，标题和时间按文件名和导入时刻记录。")
                meta = {}
        srt_text = _read_member(zf, srt_name, MAX_TRANSCRIPT_BYTES) \
            .decode("utf-8", "replace")
        rows, skipped = parse_srt(srt_text)
        marks: list[dict] = []
        bm_name = _find_member(names, "bookmarks.json")
        if bm_name:
            try:
                data = json.loads(_read_member(zf, bm_name, MAX_BOOKMARKS_BYTES)
                                  .decode("utf-8", "replace") or "[]")
                marks = _clean_marks(data)
            except ValueError:
                warns.append("bookmarks.json 解析不了，书签没有导入。")
                marks = []
        audio_file = ""
        audio_name = str(meta.get("audio") or "")
        cand = [audio_name] if audio_name in names else []
        if not cand:
            cand = [n for n in names
                    if Path(n).name.lower().startswith("audio.")
                    and Path(n).suffix.lower() in AUDIO_SUFFIXES]
        if cand:
            member = sorted(cand, key=len)[0]
            suffix = Path(member).suffix.lower()
            if suffix in AUDIO_SUFFIXES:
                dst = tmpdir / ("audio" + suffix)
                if _copy_member(zf, member, dst):
                    audio_file = str(dst)
    return {
        "title": str(meta.get("title") or "").strip(),
        "started_at": _norm_started(str(meta.get("started_at") or "")),
        "duration_s": _safe_num(meta.get("duration_s")),
        "src_sid": str(meta.get("session_id") or "").strip(),
        "minutes_url": str(meta.get("minutes_url") or "").strip(),
        "rows": rows,
        "marks": marks,
        "skipped": skipped,
        "warnings": warns,
        "audio_file": audio_file,
        "audio_name": audio_name,
        "source_kind": "package",
    }


def parse_text(text: str, name: str = "") -> dict[str, Any]:
    """解析单独导出的 SRT / 逐字稿 txt。"""
    text = str(text or "").lstrip("﻿")
    if not text.strip():
        raise ImportProblem("文件是空的，没有可导入的内容")
    if name.lower().endswith(".srt") or "-->" in text[:4000]:
        rows, skipped = parse_srt(text)
        return {"title": "", "started_at": "", "duration_s": 0.0, "src_sid": "",
                "minutes_url": "", "rows": rows, "marks": [], "skipped": skipped,
                "warnings": [], "audio_file": "", "audio_name": "",
                "source_kind": "srt"}
    parsed = parse_archive_txt(text)
    return {
        "title": str(parsed["meta"].get("title") or ""),
        "started_at": str(parsed["meta"].get("started_at") or ""),
        "duration_s": float(parsed["meta"].get("duration_s") or 0.0),
        "src_sid": str(parsed["meta"].get("src_sid") or ""),
        "minutes_url": "",
        "rows": parsed["rows"],
        "marks": [],
        "skipped": int(parsed["skipped"] or 0),
        "warnings": [],
        "audio_file": "",
        "audio_name": "",
        "source_kind": "txt",
    }


def parse_any(path: Path, name: str = "") -> tuple[dict[str, Any], Path | None]:
    """按内容判断格式。返回 (parsed, 需要清理的临时目录)。"""
    path = Path(path)
    label = name or path.name
    suffix = Path(label).suffix.lower()
    try:
        is_zip = zipfile.is_zipfile(path)
    except OSError:
        is_zip = False
    if is_zip:
        tmpdir = Path(tempfile.mkdtemp(prefix="livenotes-import-"))
        try:
            parsed = parse_package(path, tmpdir)
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        return parsed, tmpdir
    if suffix not in (".srt", ".txt"):
        raise ImportProblem("只支持分享包 zip、SRT 字幕或逐字稿 txt（当前是「%s」）"
                            % (suffix or "无后缀"))
    text = path.read_bytes()
    if len(text) > MAX_TRANSCRIPT_BYTES:
        raise ImportProblem("逐字稿文件超过 32 MB，请先拆分")
    try:
        decoded = text.decode("utf-8")
    except UnicodeDecodeError:
        decoded = text.decode("gb18030", "replace")
    return parse_text(decoded, name=label), None


# ---------------------------------------------------------------- 入库

def _sid_for(parsed: dict[str, Any]) -> str:
    src = re.sub(r"[^0-9A-Za-z_-]", "_", str(parsed.get("src_sid") or ""))[:48]
    if src:
        return "IMP_" + src
    head = "\x1f".join(str(r.get("text") or "") for r in parsed["rows"][:50])
    digest = hashlib.sha1(head.encode("utf-8")).hexdigest()[:10]
    return "IMP_" + digest


def _check_audio_file(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError:
        return False, "录音文件读不到"
    if size <= 0:
        return False, "录音文件是空的"
    sfx = path.suffix.lower()
    if sfx in (".m4a", ".mp4") and not audio.mp4_has_moov(path):
        return False, "录音不完整（缺少 moov），可能下载中断了"
    if sfx == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if w.getnframes() <= 0:
                    return False, "录音里没有音频帧"
        except (OSError, EOFError, wave.Error):
            return False, "WAV 头读不出来，文件可能不完整"
    return True, ""


def attach_audio(conn, sid: str, src: Path) -> tuple[bool, str]:
    """把一段录音挂到一场上（导入包里的、或文件中心单独补的）。"""
    if src.suffix.lower() not in AUDIO_SUFFIXES:
        return False, "只支持 m4a/wav/mp3/aac/ogg/opus 录音（当前是「%s」）" % (
            src.suffix or "无后缀")
    ok, err = _check_audio_file(src)
    if not ok:
        return False, err
    suffix = src.suffix.lower()
    dst = AUDIO_DIR / (sid + suffix)
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        size = dst.stat().st_size
    except OSError as e:
        return False, "录音复制失败：%s" % e
    store.set_audio_meta(
        conn, sid, audio_path=str(dst), audio_format=suffix.lstrip("."),
        audio_bytes=size, audio_original_path=None,
        audio_transcoded_at=(store.now() if suffix != ".wav" else None))
    return True, ""


def _wipe(conn: sqlite3.Connection, sid: str) -> None:
    """replace 的破坏面：只清这一场派生的东西，不动已发布的文档记录。

    ``feishu_publish`` 留着，重发时才会更新同一篇文档而不是再建一篇；
    总结/笔记/打点/声纹都跟旧句子的 id 绑着，必须一起清掉。
    """
    for table in ("utterances", "speakers", "summaries", "claims", "note_units",
                  "notes", "marks", "highlights", "utterance_emb", "offline_asr"):
        conn.execute("DELETE FROM %s WHERE session_id=?" % table, (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()
    # 旧录音没清掉就会变成没人引用的孤儿；新包里带不带录音都不该留旧文件。
    for suffix in AUDIO_SUFFIXES:
        try:
            (AUDIO_DIR / (sid + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def _title_for(parsed: dict[str, Any], name: str) -> str:
    for cand in (parsed.get("title"), Path(name or "").stem):
        title = str(cand or "").strip()
        if title and title.lower() not in ("transcript", "audio", "meta"):
            return title[:60]
    return "云端导入 " + datetime.now().strftime("%m-%d %H:%M")


def _wall_meta(started: str) -> tuple[str | None, int]:
    """开始时间按北京墙上时间落 started_utc / tz_offset_min，换机器也换算得对。"""
    try:
        dt = datetime.fromisoformat(started).replace(tzinfo=CN_TZ)
    except ValueError:
        return None, 480
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds"), 480


def import_file(conn: sqlite3.Connection, path: Path | str, *, name: str = "",
                replace: bool = False, title: str = "", started_at: str = "",
                audio_file: Path | str | None = None) -> dict[str, Any]:
    """把一份分享包 / 字幕 / 逐字稿导入成一场本地场次。

    ``replace=True`` 才会覆盖已存在的一场；默认在库就原样返回，并在有录音
    时给缺录音的那一场补上录音（文字来源不带音频时最常用）。
    """
    path = Path(path)
    if not path.is_file():
        raise ImportProblem("文件不存在：%s" % path)
    parsed, tmpdir = parse_any(path, name=name)
    try:
        rows = parsed["rows"]
        if not rows:
            raise ImportProblem("没有解析到任何发言，未导入（不生成空场次）")
        if len(rows) > MAX_ROWS:
            raise ImportProblem("逐字稿有 %d 句，超过 %d 句上限，请先拆分"
                                % (len(rows), MAX_ROWS))
        rows.sort(key=lambda r: (float(r["t_start"]), float(r["t_end"])))
        texts = [str(r["text"]) for r in rows]
        labelled, speakers = _assign_speakers(texts)
        for row, lab in zip(rows, labelled):
            row["speaker_id"] = lab["speaker_id"]
            row["label"] = lab["label"]
            row["text"] = lab["text"]

        src_audio = Path(str(audio_file)) if audio_file else (
            Path(str(parsed.get("audio_file"))) if parsed.get("audio_file") else None)
        sid = _sid_for(parsed)
        existing = store.get_session(conn, sid)
        if existing and not replace:
            n = int(conn.execute(
                "SELECT COUNT(*) AS n FROM utterances WHERE session_id=?",
                (sid,)).fetchone()["n"])
            out = {
                "ok": True, "existed": True, "replaced": False,
                "session_id": sid,
                "title": existing.get("title") or "",
                "n_utterances": n,
                "duration_s": float(existing.get("duration_s") or 0),
                "has_audio": bool(existing.get("audio_path")),
                "warnings": [],
                "hint": "这一场已经在库里，未覆盖；需要覆盖请确认后重新导入。",
            }
            if src_audio and not existing.get("audio_path"):
                ok, err = attach_audio(conn, sid, src_audio)
                out["has_audio"] = bool(ok)
                out["attached_audio"] = bool(ok)
                if not ok:
                    out["warnings"] = ["补录音失败：%s" % err]
            return out

        warnings: list[str] = [NO_VOICEPRINT_WARNING]
        warnings.extend(str(w) for w in parsed.get("warnings") or [])
        started = _norm_started(started_at) or str(parsed.get("started_at") or "")
        if not started:
            started = datetime.now().replace(microsecond=0).isoformat(timespec="seconds")
            warnings.append("导入包没带开始时间，按导入时刻记录。")
        row_end = max(float(r["t_end"]) for r in rows)
        duration = max([float(parsed.get("duration_s") or 0), row_end] +
                       ([audio.audio_duration(src_audio)]
                        if src_audio and src_audio.is_file() else []))
        if int(parsed.get("skipped") or 0) > 0:
            warnings.append("有 %d 段内容时间码解析不了，已跳过。"
                            % int(parsed["skipped"]))

        if replace:
            _wipe(conn, sid)
        started_utc, tz_min = _wall_meta(started)
        store.insert_session(conn, {
            "id": sid,
            "title": (title.strip()[:60] if title else _title_for(parsed, name)),
            "started_at": started, "duration_s": round(duration, 1),
            "source_mode": "import", "capture_only": 0, "sample_rate": 16000,
            "started_utc": started_utc, "tz_offset_min": tz_min,
            "audio_path": None, "warnings": warnings, "speakers": [],
        })
        utt_rows: list[dict] = []
        for r in rows:
            cls = analysis.classify(str(r["text"]))
            utt_rows.append({
                "session_id": sid,
                "t_start": round(float(r["t_start"]), 2),
                "t_end": round(max(float(r["t_end"]), float(r["t_start"]) + 0.3), 2),
                "speaker_id": str(r["speaker_id"]),
                "speaker_label": str(r["label"] or "说话人"),
                "text": str(r["text"]),
                "asr_conf": 0.9, "overlap": 0,
                "category": cls.get("category", "substantive"),
                "value_tag": cls.get("value_tag", "normal"),
                "importance": cls.get("importance", 0.5),
                "topic": cls.get("topic", ""),
                "low_value_reason": cls.get("low_value_reason", ""),
            })
        store.insert_utterances(conn, utt_rows)
        for spk, label in speakers:
            store.upsert_speaker(conn, sid, spk, label)
        for mark in parsed.get("marks") or []:
            store.insert_mark(conn, sid, float(mark["t"]),
                              str(mark.get("note") or ""),
                              str(mark.get("kind") or "mark"))
        if parsed.get("minutes_url"):
            store.set_minutes_url(conn, sid, str(parsed["minutes_url"]))

        has_audio = False
        if src_audio:
            ok, err = attach_audio(conn, sid, src_audio)
            has_audio = bool(ok)
            if not ok:
                warnings.append("录音没导进来：%s；文字已保留。" % err)
        else:
            warnings.append("导入包不含录音，这一场只能看文字。")
        # 录音成败是在会话落库之后才知道的；把最终说明整体写回一次。
        conn.execute("UPDATE sessions SET warnings_json=? WHERE id=?",
                     (json.dumps(warnings, ensure_ascii=False), sid))
        conn.commit()
        try:
            notes.index_session(conn, sid)
        except Exception:
            import traceback
            traceback.print_exc()
        logs.log_event("import.session", session_id=sid,
                       kind=str(parsed.get("source_kind") or ""),
                       rows=len(rows), audio=has_audio, replaced=bool(replace))
        return {
            "ok": True, "existed": False, "replaced": bool(replace),
            "session_id": sid,
            "title": store.get_session(conn, sid)["title"],
            "n_utterances": len(rows),
            "n_speakers": len(speakers),
            "duration_s": round(duration, 1),
            "has_audio": has_audio,
            "warnings": warnings,
            "naming_hint": NAMING_HINT,
            "source": str(parsed.get("source_kind") or ""),
        }
    finally:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
