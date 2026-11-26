# 51 · 实施提示词：发布即归档

> 用途：直接交给实施者（人或 AI）的完整任务书，单独立项，不含其它阶段。
> 关联：[docs/50](50-cloud-folders-and-archive.md) §1（完整设计）、[docs/37](37-cloud-storage-and-feishu-share.md) §12
> 一句话：一场发布到飞书时，自动把整场 m4a 归档进云空间，并把链接写进文档正文与场次表；失败只记日志，绝不改变发布结果。

---

## 任务（贴给实施者）

在 `livenotes` 仓库里，把「云端录音归档」从**手动按钮**改成**发布时自动执行**。

现在的状态：
- 归档能力已经有了：`app/feishu_drive.py::archive_session()`，上传 m4a、授权给 owner、写回 `sessions.drive_token/drive_url/drive_uploaded_at`。
- 但只有用户亲手点「存入云空间」才会调用它（`app/server.py` 的 `/api/cloud/save`，以及 `app/web/files.html`、`app/web/reader.html` 的两个按钮）。
- 发布链路（`app/feishu_publish.py`）完全不碰云空间。所以 `export.build_publish()` 读到的 `audio_url` 绝大多数时候是空的（`app/export.py:375`）。
- 结果：场次表的「录音链接」常空，文档里 `ready` 状态连「## 录音」这一节都没有（只有 `pending_transcript` 分支写了，`app/export.py:483`）。

你要做的：发布时先归档、再生成正文，让「录音」在文档和场次表里都是有值的。

---

## 边界（明确不要做）

- **不要**建「一场一个文件夹」（docs/50 阶段 1）。这一轮只有一个大文件夹 `FEISHU_AUDIO_FOLDER_TOKEN`，照旧。
- **不要**动 Base 附件字段、不要新建字段、不要碰 `bitable:app`。
- **不要**加重转写、不要加 `_v1/_v2`。
- **不要**引入队列、后台线程池、重试表。上传就在发布线程里同步做。
- **不要**改 `/api/cloud/save` 和两个前端按钮的行为——手动入口保留，只是不再是唯一入口。
- **不要**改 `archive_session()` 的对外返回结构，`tests/test_feishu_drive.py` 已经锁住了它。

---

## 开工前必须知道的一个实测结论（2026-11-27）

用 `tools/spike_drive_preview.py` 在真实租户上传过 m4a / mp3 / opus / wav：

- `upload_all` 的返回体里**有 `url`**，是租户域名：`https://<tenant>.feishu.cn/file/<token>`。
- 通用域名 **`https://feishu.cn/file/<token>` 不可用**：301 到 `www.feishu.cn` 再跳登录页，
  手机上点开是飞书首页，不是文件。
- 但 `app/feishu_drive.py::file_url()` 的默认值正是这个通用域名（第 68 行），
  只有在 `FEISHU_DRIVE_URL_BASE` 被显式配置时才正确。`archive_session()` 第 244 行
  是 `out.get("url") or file_url(...)`，所以正常情况下走的是接口给的 url，兜底才出错。

**所以这一轮顺手补一条**（属于本任务范围，不额外算工作量）：`file_url()` 在没有任何
base 时，不要拼通用域名；返回空串，让调用方知道「没有可信链接」。对应地，
`archive_session()` / `ensure_archived()` 里 url 为空时，`drive_url` 写 `None`，
文档里的「## 录音」就不出现——**宁可没有链接，也不要给一个跳到首页的链接**。

- **不要**为此去调 `tenant/info` 之类的接口查域名；没有 base 就是不写链接。

---

## 先读这些（按顺序）

1. `app/feishu_drive.py`：全文。重点 `audio_path_for()`（优先 m4a）、`upload_file()`（>20 MB 自动分片）、`grant_member()`、`archive_session()`（第 212 行起，看它怎么算文件名、怎么处理超大 WAV）。
2. `app/feishu_publish.py`：`_publish()` 第 255 行起。重点看 `doc = export.build_publish(conn, sid)`（第 283 行）之前有什么、之后 `cfg` / `owner` / `version` 怎么算（第 290~299 行）。
3. `app/export.py`：`build_publish()`（第 325 行起）、`audio_url` 的来历（第 375 行）、`ready` 分支的结尾（第 470~475 行左右）。
4. `app/store.py`：`set_audio_meta()` / `_AUDIO_META_COLS`（第 980 行起）、`get_publish()` / `upsert_publish()`（第 3055 行起，注意 `meta` 进出 `meta_json`）。
5. `app/audio.py`：`transcode_session()`（第 177 行起）、`_mark()`（第 222 行）。
6. `tests/test_feishu_drive.py`：`FakeFile` / `FakeMembers` / `FakeClient` / `FakeBot`（第 40~90 行）。
7. `tests/test_feishu_publish.py`：`FakeBot`（第 122 行起）和 `test_publish_creates_one_doc_and_one_row`（第 303 行）。

---

## 改动一：`app/feishu_drive.py` 新增 `ensure_archived()`

```python
def ensure_archived(conn, session: dict, bot=None) -> dict:
    """幂等归档：已经传过且不比转码旧就跳过。

    返回 {ok, skipped, file_token, url, replaced, error}。
    任何失败都不抛异常——云端副本缺失不影响本地录音、逐字稿和发布结果。
    """
```

判定顺序（就是这个顺序，别调）：

1. `sid = str(session.get("id") or "")` 为空 → `{"ok": False, "error": "缺少场次 id"}`。
2. `path = audio_path_for(session)` 为 None → `{"ok": False, "error": "这场没有可上传的音频"}`。
3. `path.suffix == ".wav"` 且 `size > MAX_WAV_UPLOAD` → `{"ok": False, "error": "WAV 还没转码完成（%d MB）"}`（和 `archive_session()` 第 223 行的措辞保持一致）。
4. **跳过条件**：`session.get("drive_token")` 非空，且 `drive_uploaded_at >= (audio_transcoded_at or "")`（后者为空视为满足）→ `{"ok": True, "skipped": True, ...}`，直接返回，不调任何接口。
5. 否则上传。文件名、文件夹、授权逻辑**复用 `archive_session()` 里已有的那段**——把它抽成内部函数 `_do_archive(conn, session, bot, path)`，`archive_session()` 与 `ensure_archived()` 都调它，保证两条路径写库的字段完全一致。
6. 覆盖场景（第 4 步不满足、`drive_token` 却有值 = 文件换代了）：上传成功后调
   `client.drive.v1.file.delete(DeleteFileRequest.builder().file_token(old).type("file").build())`
   删旧文件，返回 `replaced=True`。删除失败只 `logs.log_event(..., level="warn")`，不影响 `ok`。
7. 全程用 `try/except Exception` 兜底，异常转 `{"ok": False, "error": str(e)}`，并记一条 `feishu.drive_archive_failed`。

**不要**在这里做权限/配额预检。

---

## 改动二：`app/feishu_publish.py::_publish` 接上

位置：`_publish()` 里 `if not enabled(bot_obj): ... return` 之后、`doc = export.build_publish(conn, sid)` **之前**。

```python
        cfg = _cfg(bot_obj)          # ← 从下面第 291 行挪上来
        archive = {"ok": True, "skipped": True}
        if _archive_on(cfg):
            try:
                archive = feishu_drive.ensure_archived(conn, s, bot_obj)
            except Exception as e:
                traceback.print_exc()
                archive = {"ok": False, "error": str(e)}

        doc = export.build_publish(conn, sid)   # 必须在归档之后：audio_url 参与正文与版本 hash
```

要点：

- **顺序不能反。** `build_publish()` 从数据库读 `drive_url` 拼正文，`version = _version(标题, 正文)` 又拿正文算 hash，所以归档必须先落库。
- **补一条日志**：`logs.log_event("feishu.archive", session_id=sid, ok=..., skipped=..., error=...)`。
- **归档失败不要进 `warnings` 列表。** 现有代码里 `warnings` 会被拼成 `error` 字段并让 `state` 变成 `partial`；归档失败不该让文档看起来是坏的。只写进 `meta`：

```python
        meta["archive_skipped"] = bool(archive.get("skipped"))
        meta["archive_error"] = "" if archive.get("ok") else str(archive.get("error") or "")
```

- `meta` 已经在函数末尾组装（搜 `"base_ok"` 那一段），加两个 key 即可，`upsert_publish` 会把整个 dict 序列化进 `meta_json`。
- 新配置读法放个模块级小函数：

```python
def _archive_on(cfg: dict) -> bool:
    v = cfg.get("archive_on_publish")
    return True if v is None else bool(v)
```

---

## 改动三：`app/export.py::build_publish` 的 `ready` 分支补「录音」

在 `ready` 分支结尾（`related` 之后、`lines += ["---", ...]` 之前）插入：

```python
        if audio_url:
            lines += ["## 录音", "", "- 整场录音：%s" % audio_url, ""]
```

`audio_url` 已经是 `drive_url or minutes_url`（第 375 行），三处分支都能用；只在 `ready` 里加，`pending_*` 分支本来就有。

---

## 改动四：转码完成后补发一次

`app/audio.py::transcode_session()` 成功分支（第 210 行 `_mark(conn, sid, dst, src)` 之后、`return dst` 之前）：

```python
        # 骨架发布时可能只有 WAV，m4a 出来后补发一次，云端归档才有东西可传。
        try:
            from . import feishu_publish
            if feishu_publish.has_published(conn, sid):
                feishu_publish.publish_session_async(sid, only_if_published=True)
        except Exception:
            traceback.print_exc()
```

延迟导入是必须的（`audio` 在 `feishu_publish` 的依赖链上）。仓库里已有同样写法，照抄 `app/server.py:3039` 附近。

---

## 改动四之二：`file_url()` 不再拼通用域名

`app/feishu_drive.py` 第 66~71 行，改成：

```python
def file_url(file_token: str, base: str = "") -> str:
    """拼云空间文件链接。没有租户域名时返回空串。

    通用域名 https://feishu.cn/file/<token> 会 301 到 www 再跳登录页
    （2026-11-27 实测），手机上点开是飞书首页——不如不给链接。
    """
    root = (base or "").strip()
    if not root:
        return ""
    if not root.endswith("/"):
        root += "/"
    return root + str(file_token)
```

连带改 `archive_session()` 第 243~251 行：`url` 为空时，`drive_url` 传 `None` 而不是空串
（`set_audio_meta` 允许显式 None）。

**现有测试要改一处**：`tests/test_feishu_drive.py::test_archive_session_writes_columns`
（第 144、147 行）现在断言 `url == "https://x.feishu.cn/file/ft_all"`，那个值来自假接口
返回的 url，不受影响；但 `FakeBot` 的 `drive_url_base` 是 `""`，所以要**新增**一条
`test_archive_session_without_url_base_keeps_link_empty`，断言 `drive_url is None`。

---

## 改动五：配置

在 `app/feishu.py::load_config()` 的返回 dict 里加：

```python
        "archive_on_publish": (os.environ.get("FEISHU_ARCHIVE_ON_PUBLISH", "1")
                               .strip().lower() not in ("0", "false", "no", "off")),
```

默认 **1**（这一轮的目的就是默认归档）。`0` 时行为完全回到今天。`app/feishu_publish.py::config()` 已经把 `feishu.load_config()` 的结果透传，不用再改。

`README.md` 环境变量表加一行：

```
| `FEISHU_ARCHIVE_ON_PUBLISH` | `1` | 发布时自动把整场 m4a 归档到云空间；`0` = 只保留手动「存入云空间」 |
```

---

## 测试（只做单元 / 轻量集成，不截图）

### `tests/test_feishu_drive.py`

`FakeClient` 补一个 `drive.v1.file.delete` 的假实现（记录调用即可）。

| 用例 | 断言 |
|---|---|
| `test_ensure_archived_skips_when_up_to_date` | `drive_uploaded_at` 晚于 `audio_transcoded_at` → `skipped=True`，`FakeFile.all_calls` 为空 |
| `test_ensure_archived_reuploads_after_new_transcode` | 转码时间更新 → 真的上传，且 `replaced=True`、旧 token 走了一次 delete |
| `test_ensure_archived_without_audio_returns_error` | 无音频 → `ok=False`，不抛异常 |
| `test_ensure_archived_huge_wav_returns_error` | WAV 超 `MAX_WAV_UPLOAD` → `ok=False`，`error` 含「转码」 |
| `test_archive_session_and_ensure_archived_write_same_columns` | 两条路径写出的 `drive_token/drive_url/drive_uploaded_at` 形状一致 |

### `tests/test_feishu_publish.py`

| 用例 | 断言 |
|---|---|
| `test_publish_archives_before_build` | 归档返回的 url 出现在 `markdown` 的「## 录音」里 |
| `test_publish_archive_failure_keeps_state_ok` | `ensure_archived` 返回失败时 `state == "ok"`、`error == ""`、`meta["archive_error"]` 有值 |
| `test_publish_archive_disabled_by_config` | `FEISHU_ARCHIVE_ON_PUBLISH=0` 时不调用 `ensure_archived`（monkeypatch 计数） |
| `test_publish_ready_doc_has_audio_section` | `ready` 分支正文含「## 录音」 |

**注意（很容易踩）**：`tests/test_feishu_publish.py` 现有的 `FakeBot` 没有 `client.drive`。默认开关是开的，所以这些老用例会走到 `ensure_archived()` 然后异常。两种处理，二选一：

- 在 `FakeBot` 里塞一个最小 `drive` 桩（推荐，这样默认路径也真的被覆盖到）；
- 或在 `tests/conftest.py` 的 `isolated_paths` 里 `monkeypatch.setenv("FEISHU_ARCHIVE_ON_PUBLISH", "0")`，再单独写启用路径的用例。

**不要**为了让老测试变绿而把默认值改成 0。

### `tests/test_audio_archive.py`

| 用例 | 断言 |
|---|---|
| `test_transcode_republishes_when_published` | 已发布场次转码后，`publish_session_async` 被调用一次且 `only_if_published=True` |
| `test_transcode_does_not_republish_unpublished` | 未发布场次不触发 |

---

## 验收（本机，人工，5 分钟）

1. `cd D:\devFile\livenotes; .venv\Scripts\python.exe -m pytest -q` — 全绿（现有 904 个 + 新增）。
2. 起引擎，文件中心找一场**有 m4a 录音、已总结**的场次，**不要点任何按钮**，直接 `POST /api/publish`（或机器人 `/发布`）。
3. 返回体 `state == "ok"`，`meta.archive_skipped == false`。
4. 飞书打开这篇云文档：末尾有「## 录音」，手机上点开**出现播放器、能拖进度条**。
5. 同一个请求再来一次：返回 `unchanged: true`，云空间**没有**多出第二份音频。
6. 故意把 `FEISHU_AUDIO_FOLDER_TOKEN` 改成一个不存在的 token 再发一次：`state` 仍是 `ok`，`meta.archive_error` 有值，文档正文一切正常（只是没有录音链接）。

> **注：第 4 步可以按「点开能播」验收了。** 2026-11-27 真机确认（`tools/spike_drive_preview.py`
> 上传到真实租户，手机飞书逐个点开）：
>
> | 格式 | 结果 |
> |---|---|
> | m4a（含 5 分 22 秒整场 1.28 MB） | 播放器正常、进度条可拖 |
> | mp3 | 同上 |
> | wav | 同上 |
> | **opus** | 「该文件类型无法查看：.opus」，只能「用其他应用打开」 |
>
> 生产归档用的正是 m4a，所以**不需要额外转 mp3**。唯一红线：**不要把 opus 当归档格式**
> ——它只在飞书语音消息（`im/v1/files` 的 `file_type=opus`）这条路上有意义。
> 另外接口侧的 `type` / `doc_type` 对这种差别**完全无感**（全部报 `file`），
> 以后换格式仍然只能靠真机点一次。

---

## 完成标准

- 只有上面五个改动点，没有别的文件被动。
- 老测试全绿；新增测试覆盖「跳过 / 重传 / 失败不降级 / 开关关闭」四种路径。
- `archive_session()` 的返回结构和对外行为不变。
- 失败场景下，日志里有一条可检索的 `feishu.drive_archive_failed` 或 `feishu.archive` 记录。
