# 50 · 云空间按场次组织：可执行改法

> 面向：实现
> 依据：只读审查结论（HEAD `2f93fda`，2026-11-26）。相关：[docs/37](37-cloud-storage-and-feishu-share.md) §12、[docs/47](47-feishu-native-reading-surface.md) §3.2、[docs/48](48-验收清单.md) §2
> 结论时间：2026-11-27

## 0. 先说清楚顺序和理由

目标四件事：**一场一个文件夹 → 逐字稿进文件夹 → 挂到场次表 → 文件夹级授权**。

代码现状决定了顺序必须从「录音链接常空」开始，否则后面三步全落在一个空字段上：

| 现状 | 依据 |
|---|---|
| 发布链路不碰云空间，`drive_url` 只有用户手动点「存入云空间」才有 | [feishu_drive.py:212](../app/feishu_drive.py:212)、[server.py:2734](../app/server.py:2734) |
| 文档正文的「录音」一节只在 `pending_transcript` 分支写，`ready` 分支根本没有 | [export.py:483](../app/export.py:483) |
| 场次表的 `录音链接` 依赖 `doc.audio_url`，所以经常是空的 | [feishu_publish.py:676](../app/feishu_publish.py:676)、[export.py:375](../app/export.py:375) |
| 所有场次共用一个 `audio_folder_token`，文件名 = 标题 | [feishu_drive.py:232-238](../app/feishu_drive.py:232) |
| 重复存同场会「同名并存」，不做去重 | [feishu_drive.py:216](../app/feishu_drive.py:216) |

**一处调整**：阶段 2（挂 Base 附件）排在阶段 3（文件夹授权）之前的原顺序保留，但阶段 2 依赖一个当前**不可用**的前提——云端租户没有 `bitable:app`，场次表根本建不出来（[48:99-107](48-验收清单.md)）。所以：

- 阶段 0 / 1 / 3 今天就能做，不依赖 Base。
- 阶段 2 只能在权限下来后做；权限没下来之前，用阶段 2 的「降级方案」先给两个 URL 字段。

阶段 3 只有一个函数，可随时插在阶段 1 之后，不必等阶段 2。

---

## 1. 阶段 0：发布即归档（让「录音链接」不再是空的）

### 1.1 目标

一场发布成功时，云空间里一定有它的 m4a，`sessions.drive_url` 一定有值，文档正文和场次表都能点开听。手动「存入云空间」保留，但不再是唯一入口。

### 1.2 改法

**新函数**：`app/feishu_drive.py`

```python
def ensure_archived(conn, session: dict, bot=None) -> dict:
    """幂等归档：已经传过且不比转码旧就跳过。

    返回 {ok, skipped, file_token, url, replaced, error}。
    失败不抛异常——发布只是投影，没有云端副本不影响本地任何东西。
    """
```

判定规则（顺序即优先级）：

1. `audio_path_for(session)` 返回 None → `{ok: False, error: "这场没有可上传的音频"}`。
2. 拿到的是 `.wav` 且 > `MAX_WAV_UPLOAD` → 返回错误，**不当成发布失败**（沿用现逻辑）。
3. `drive_token` 非空，且 `drive_uploaded_at >= audio_transcoded_at`（后者为空视为已满足）→ `{ok: True, skipped: True}`。
4. 否则走上传；若旧 `drive_token` 存在且点 3 不成立（文件换了），上传成功后调 `drive/v1/files/:token/delete?type=file` 删旧文件，把 `replaced=True` 带回。删除失败只记日志，不阻断。
5. 写回 `store.set_audio_meta(conn, sid, drive_token=..., drive_url=..., drive_uploaded_at=...)`。

**文件名**：`safe_name(标题 + 后缀)` 保持不变（阶段 1 会把它挪进专属文件夹，文件名本身仍要能读）。

**接入点**：`app/feishu_publish.py::_publish`，在 `export.build_publish(conn, sid)` **之前**、`enabled()` 检查**之后**：

```python
        # 发布即归档：文档正文和场次表的「录音」指向同一个 drive_url。
        # 放在 build_publish 之前，是因为 audio_url 参与正文生成和 version hash。
        archive_res = {"ok": True, "skipped": True}
        if cfg_archive_on(bot_obj):
            try:
                archive_res = feishu_drive.ensure_archived(conn, s, bot_obj)
            except Exception as e:
                traceback.print_exc()
                archive_res = {"ok": False, "error": str(e)}
```

`warnings` 里加一条 `"录音没存进云空间：%s"`，**但不改 `state`**——云端副本缺失不该把整场标成 `partial`，那会让收尾卡变橙、`/文档` 提示重试，而重试也救不了配额或权限问题。这条只进 `meta_json`：

```python
        meta["archive_skipped"] = bool(archive_res.get("skipped"))
        meta["archive_error"] = "" if archive_res.get("ok") else archive_res.get("error", "")
```

**开关**：新配置 `FEISHU_ARCHIVE_ON_PUBLISH`，默认 `1`（这就是本轮的目的）。`0` 时行为回到今天。

> 为什么不做成纯后台：`export.build_publish` 读的是数据库里的 `drive_url`，先传完才能把链接写进同一版正文。放到后台就得二次发布，正文版本 hash 会跳两次、评论锚点丢一次。

### 1.3 正文补「录音」行

`app/export.py::build_publish` 的 `ready` 分支（约 400~475 行）在 `## 相关场次` 之后、结尾 `---` 之前插入：

```python
        if audio_url:
            lines += ["## 录音", "", "- 整场录音：%s" % audio_url, ""]
```

注意 `audio_url` 已经是 `drive_url or minutes_url`（[export.py:375](../app/export.py:375)），所以回填过妙记的场次也会出现这一节。这是阶段 0 唯一改正文的地方。

### 1.4 转码完成补一次发布

m4a 是收尾后才生成的；如果骨架在只有 WAV 时就发布了，`drive_url` 会缺一轮。在 `app/audio.py::transcode_session` 成功分支（`_mark(...)` 之后）加：

```python
        # 转码出 m4a 后再发布一次：云端归档只认 m4a，第一次可能是空的。
        try:
            from . import feishu_publish
            if feishu_publish.has_published(conn, sid):
                feishu_publish.publish_session_async(sid, only_if_published=True)
        except Exception:
            traceback.print_exc()
```

延迟导入，避免 `audio` ↔ `feishu_publish` 循环依赖（仓库里已有同样写法，见 [server.py:3039](../app/server.py:3039)）。

版本 hash 会因为正文多了录音链接而变化 → 走 `overwrite_docx` 重写一次。这是一次性的，第二次起 `same_version` 短路。

### 1.5 已知代价

`POST /api/publish` 在请求线程里同步发布（[server.py:2754](../app/server.py:2754)）。加了归档后，一次显式发布会多出 5~40 秒的上传时间。三个选择：

- **A（建议）**：接受。桌面端等结果，慢一点好过"发了但链接是空的"。
- B：`/api/publish` 改成只排队、立刻返回 `{queued: true}`；但现有前端在等结果。
- C：归档只在 `publish_session_async` 里做，`/api/publish` 传 `archive=False`。会让同一条路径行为不一致。

先按 A 落地，把实测耗时写进 [docs/48](48-验收清单.md)；超过 60 秒再考虑 B。

---

## 2. 阶段 1：一场一个文件夹

### 2.1 核心决定：索引是 token，不是文件夹名

用户原方案是 `2026-11-25_房间名_S01/`。**名字照做，但不拿它当索引**——这正好落在原清单的边界「不要用文件名当索引」上：

- 房间名来自 `rooms.json`，房间被删就查不到了（`get_by_session` 遍历文件，[rooms.py:121](../app/rooms.py:121)），历史场次的文件夹名就解释不了自己。
- 标题可人工改（[server.py:2906](../app/server.py:2906)），改完文件夹名过期。

所以：**创建时用可读名字，落库后用 `sessions.drive_folder_token` 定位。** 改名时文件夹不动，也不会因为重名串场。

命名模板：

```
YYYY-MM-DD_HHMM_<房间名或标题前 20 字>
例：2026-11-25_2130_开源AI聊天室
```

`HHMM` 取代 `_S01`：每房间每日场次序需要新维护一个计数器，而 `rooms.json` 里没有 `session_id → room_id` 的反向索引，按房间统计要全表扫；`HHMM` 天然有序且不重复，收益一样。

### 2.2 新函数（`app/feishu_drive.py`）

```python
def folder_name(session: dict) -> str:
    """2026-11-25_2130_开源AI聊天室。解析不了开始时间就退到 日期_sid。"""

def create_folder(client, parent_token: str, name: str) -> dict:
    """drive/v1/files/create_folder。返回 {ok, token, url, error}。

    同名已存在时飞书会报错；调用方用 list_folder 兜底复用。
    """

def list_folder(client, folder_token: str) -> list[dict]:
    """drive/v1/files?folder_token=... 分页拉完，返回 [{token,name,type}]。"""

def ensure_session_folder(conn, session: dict, bot) -> dict:
    """取或建这场专属文件夹，返回 {ok, token, url, created, error}。

    1. sessions.drive_folder_token 有值 → 直接用（不校验，省一次往返）；
    2. 否则 create_folder 在 audio_folder_token 下建；
    3. 建失败（多半是同名）→ list_folder 找同名 folder 复用；
    4. 写回 sessions.drive_folder_token / drive_folder_url。
    """
```

`client.drive.v1.file.create_folder` / `.list` 在 lark-oapi 1.7.5 里都有（`resource/file.py:92`、`:370`），返回体 `{token, url}`。

### 2.3 上传目标从「大文件夹」改为「场次文件夹」

`archive_session` 与 `ensure_archived` 共用一个内部函数：

```python
def _target_folder(conn, session, bot) -> dict:
    """取或建场次文件夹；没配 audio_folder_token 时退回旧行为（None）。"""
```

- 配了 → 上传进 `session_folder`。
- 没配（`audio_folder_token` 为空）→ 保持今天的行为，返回「没配置」错误，不做隐式建目录。

`audio_folder_token` 的角色从「音频都放这里」变成「场次文件夹都建在这里」，语义向后兼容，README 只需改描述。

### 2.4 数据库

`app/store.py::_migrate`（约 548 行）的补列元组里加三项：

```python
                      ("drive_folder_token", "TEXT"),
                      ("drive_folder_url", "TEXT"),
                      ("drive_folder_granted_at", "TEXT")):
```

同步加进 `_AUDIO_META_COLS`（[store.py:980](../app/store.py:980)），`set_audio_meta` 才能写。`sessions` 建表语句也补上（新库直接有）。

### 2.5 历史场次迁移

不自动搬。提供一个工具，和 `tools/backfill_*.py` 同风格：

```
python tools/backfill_drive_folders.py --dry      # 只打印会建哪些文件夹
python tools/backfill_drive_folders.py --limit 5  # 先拿 5 场试
python tools/backfill_drive_folders.py            # 全部
```

只处理 `drive_token` 非空的场次：建文件夹 → `drive/v1/files/:token/move`（body `{type:"file", folder_token:...}`）把已有文件挪进去。**移动而不是重传**，省流量也保留原 file_token（场次表里的链接不会失效）。

---

## 3. 阶段 2：逐字稿落盘 + 挂到场次表

### 3.1 逐字稿文件（不依赖 Base，可以先做）

**新的导出函数**：`app/export.py`

```python
def to_archive_txt(conn, sid) -> str:
    """给云空间用的逐字稿：自带抬头，脱离系统也能看懂。"""
```

格式：

```
# 2026-11-25 21:30 ·「开源AI聊天室」
场次：LIVE_20261125_213000
时长：73 分钟 · 上麦 6 人 · 1069 句
导出：2026-11-27 10:12

[00:12] 阿哲：……
[00:19] 小北：……
```

为什么不直接复用 `to_txt`：那个是被 `/api/export?format=txt` 用的裸格式（[export.py:106](../app/export.py:106)），加抬头会改变既有下载内容。新函数 = 抬头 + `to_txt` 正文，零风险。

**落盘**：`ensure_session_folder` 之后调 `upload_file(client, txt_path, folder_token, "<文件夹名>.txt")`。文本文件几 MB 以内，走 `upload_all` 一条路径。

**幂等**：已有 `drive_folder_token` 且文件已存在同名 `.txt` → 覆盖上传会重新产生一份（飞书没有覆盖写）。处理方式与音频一致：先 `list_folder` 找同名文件，拿到旧 token → 上传新的 → 删旧的。抽成 `_replace_file(client, folder, name, path)` 给音频和 txt 共用。

### 3.2 挂到 Base：先做 15 分钟探针

**这一步有技术不确定性，不要直接写实现。** Base 附件字段（type 17）要的不是 `drive/v1/files/upload_all` 返回的 file_token，而是 `drive/v1/medias/upload_all` 的 media token（`parent_type` 指向 Base）。SDK 里两条上传路径都在（`media.py:136`、`file.py:546`），但"file token 能不能塞进附件字段"这件事**本仓库没验证过**。

按仓库既有习惯写一个探针：`tools/spike_base_attachment.py`，只做一次真租户实验：

1. `medias/upload_all` 上传一个小 txt，`parent_type="bitable_file"`、`parent_node=<app_token>`；
2. 在场次表建一个临时附件字段，写一行 `[{file_token: <media_token>}]`；
3. 用 `drive/v1/files/upload_all` 的 file_token 再写一次，看是否被拒；
4. 读回来确认 `tmp_url` 可下载；
5. 删掉临时字段和行。

**探针结论决定走 2a 还是 2b。**

#### 2a：附件字段可用（探针通过）

`app/feishu_publish.py::FIELD_DEFS` 加：

```python
    {"field_name": "逐字稿", "type": 17},
    {"field_name": "录音", "type": 17},
```

`_record_fields` 里：`录音` 用阶段 0 的 `drive_token`，`逐字稿` 用 txt 的 token，都要是 media token → 所以**文件要传两份**（一份进文件夹给人看，一份作 media 给附件用），或者干脆只传 media 不建文件夹副本。

取舍：**传两份**。文件夹是给人顺着找的，附件是给 Base 点开听的，两者服务不同入口；同名同大小，多占的配额相比 m4a 可以忽略。

失败处理沿用既有纪律：附件写不进去 → 这一格留空 + warning，不影响整行。

#### 2b：附件字段不可用（探针失败）

不动附件类型，改为两个 URL 字段：

```python
    {"field_name": "逐字稿链接", "type": 15},
```

`_record_fields` 写 `{"link": <txt 的 file url>, "text": "打开逐字稿"}`。效果：手机上点开是飞书原生预览（txt 可搜可转），比附件更轻，缺点是不能在表格里直接下载。

这条路今天就能上，且**不需要 `bitable:app` 之外的任何新权限**——但场次表本身还是建不出来，所以仍然挂在权限后面。

### 3.3 权限前提（必须前置确认）

阶段 2 整段依赖：

1. 租户开通 `bitable:app`（或 `base:app:create`），申请入口见 [docs/48 §2.2](48-验收清单.md)；
2. `FEISHU_PUBLISH_ENABLED=1`；
3. 若走 2a，还要确认应用有 `drive:file`（media 上传）——大概率已具备，因为 `im/v1/files` 与 `drive` 上传都在用。

权限没下来之前，阶段 2 只做 3.1，不做 3.2。

---

## 4. 阶段 3：按文件夹授权

### 4.1 现状

`grant_member` 写死 `type="file"`、`member_type="openid"`（[feishu_drive.py:181-194](../app/feishu_drive.py:181)）。`feishu.Bot._grant_doc` 支持 `doc_type` 但不支持群（[feishu.py:341](../app/feishu.py:341)）。接口本身支持 `openchat` + `type=folder`（[docs/37:438](37-cloud-storage-and-feishu-share.md)）。

### 4.2 改法

两处签名各加一个默认参数，向后兼容：

```python
# app/feishu_drive.py
def grant_member(client, file_token: str, open_id: str,
                 perm: str = "full_access", doc_type: str = "file",
                 member_type: str = "openid") -> dict:

# app/feishu.py
    def _grant_doc(self, document_id: str, open_id: str,
                   perm: str = "full_access", doc_type: str = "docx",
                   member_type: str = "openid") -> dict:
```

新函数：

```python
def grant_folder_to_chat(client, folder_token: str, chat_id: str,
                         perm: str = "view") -> dict:
    """把场次文件夹授权给一个群：之后新加进文件夹的文件自动可见。

    默认 view：群成员能看能转，不能删。要让他们往里放文件再改 edit。
    """
```

`need_notification=False` 必须带（[feishu_drive.py:193](../app/feishu_drive.py:193) 已有），否则每场建目录都会在群里弹一次。

### 4.3 何时授、授给谁

- 时机：`ensure_session_folder` 刚建出文件夹时，或 `drive_folder_granted_at` 为空时。**每次都调也安全**（幂等），但没必要；用 `drive_folder_granted_at` 标记，失败不写标记，下次自动重试。
- 对象：新配置 `FEISHU_ARCHIVE_CHAT_ID`，不填则取 `FEISHU_CHAT_IDS` 的第一个。都为空 → 跳过，不报错（桌面单机用户没有群）。
- 权限：`FEISHU_ARCHIVE_CHAT_PERM`，默认 `view`。

### 4.4 与「一场一授权」的关系

保留 `archive_session` 里授权给 owner 那一步：owner 是操作者，群是读者，两种授权互补。文件夹授权不替代它。

---

## 5. 明确不做的两件事

### 5.1 重转写 _v1/_v2

**现在没有「重转写」这个入口**，所以「重转写覆盖旧稿」没有发生条件：离线补转写是增量、按时间戳去重（[offline_asr.py:231](../app/offline_asr.py:231)），`--backfill` 只挑 `status != done` 的场次（[offline_asr.py:394](../app/offline_asr.py:394)），CLI 没有 reset 参数。

真要防的是**已有的**三处覆盖：

| 覆盖 | 现在怎么防 | 要不要补 |
|---|---|---|
| 重聚类改说话人 | `edited_at` 保护人工改判（[store.py:1465](../app/store.py:1465)） | 够 |
| 人工改字后被 AI 重标 | `update_utterance` 清 `ai_fp`（[store.py:1476](../app/store.py:1476)） | 够 |
| 删场次 | `trash_session` 软删、`restore` 可还原 | 够 |

结论：**等真的加了重转写再做 _v1/_v2**，那时只需要在两处留痕——`archive_txt` 文件名带 `_v2`，以及 `utterances` 加 `asr_version` 列。现在加就是给不存在的功能写防护。

### 5.2 「配额 10%」

代码里没有任何配额查询（`drive` SDK 也没有存储容量接口）。要做「上传前查剩余容量」得先确认租户是否开放了相应接口，属于独立调研，不塞进这轮。

替代方案：`ensure_archived` 的处理逻辑里已经天然限制了体积——只传 m4a、WAV 超 300 MB 直接拒（[feishu_drive.py:223](../app/feishu_drive.py:223)）。真要控量，用 `LIVENOTES_AUDIO_RETENTION_DAYS` 控制本地、云端靠人删。

---

## 6. 变更清单（按文件）

| 文件 | 改动 |
|---|---|
| `app/feishu_drive.py` | 新增 `folder_name` / `create_folder` / `list_folder` / `ensure_session_folder` / `ensure_archived` / `_target_folder` / `_replace_file` / `upload_transcript` / `grant_folder_to_chat`；`grant_member` 加 `doc_type`/`member_type`；`archive_session` 改为复用以上内部函数 |
| `app/feishu_publish.py` | `_publish` 在 `build_publish` 前调 `ensure_archived`；`meta` 写 `archive_skipped`/`archive_error`；`FIELD_DEFS` 加字段（阶段 2）；`_record_fields` 写新字段 |
| `app/export.py` | `build_publish` 的 `ready` 分支补「## 录音」；新增 `to_archive_txt` |
| `app/audio.py` | 转码成功后若已发布，触发一次 `publish_session_async` |
| `app/store.py` | `_migrate` + 建表补 `drive_folder_token`/`drive_folder_url`/`drive_folder_granted_at`；`_AUDIO_META_COLS` 同步 |
| `app/feishu.py` | `load_config` 读新配置；`_grant_doc` 加 `member_type` |
| `app/config.py` 或 `feishu.load_config` | `FEISHU_ARCHIVE_ON_PUBLISH`、`FEISHU_ARCHIVE_CHAT_ID`、`FEISHU_ARCHIVE_CHAT_PERM`、`FEISHU_ARCHIVE_TRANSCRIPT`（是否传逐字稿，默认 1） |
| `tools/backfill_drive_folders.py` | 新建：历史场次移进各自文件夹 |
| `tools/spike_base_attachment.py` | 新建：附件字段探针（阶段 2 前置） |
| `README.md` | 环境变量表加三行；「整场录音可以存进飞书云空间」那段改成「发布时自动存」 |
| `docs/37` | §12.4 的「不要默认全量上传」与本文阶段 0 冲突，加一行指向本文件说明取舍 |
| `docs/48` | 加一节本轮的验收项 |

### 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_ARCHIVE_ON_PUBLISH` | `1` | 发布时自动归档 m4a；`0` 回到只手动 |
| `FEISHU_ARCHIVE_TRANSCRIPT` | `1` | 归档时一并上传逐字稿 txt |
| `FEISHU_ARCHIVE_CHAT_ID` | 空 | 文件夹授权给哪个群；空则取 `FEISHU_CHAT_IDS` 第一个 |
| `FEISHU_ARCHIVE_CHAT_PERM` | `view` | `view` / `edit` / `full_access` |

---

## 7. 测试

只做单元 / 轻量集成，不做验收截图。

### `tests/test_feishu_drive.py`

| 用例 | 断言 |
|---|---|
| `test_ensure_archived_skips_when_up_to_date` | `drive_uploaded_at >= audio_transcoded_at` 时不上传、返回 `skipped` |
| `test_ensure_archived_reuploads_after_transcode` | 转码时间更新后重传，旧 token 走 delete |
| `test_ensure_archived_failure_does_not_raise` | 上传异常 → `{ok: False, error}` |
| `test_folder_name_template` | 正常/缺房间名/缺时间三种输入 |
| `test_ensure_session_folder_reuses_token` | 已有 token 时不调 create |
| `test_ensure_session_folder_reuses_same_name_on_conflict` | create 失败 → list 命中同名 → 复用 |
| `test_grant_folder_to_chat_uses_folder_and_openchat` | 请求体 `type=folder`、`member_type=openchat`、`need_notification=False` |
| `test_upload_transcript_header` | 抬头含标题/场次/时长，正文与 `to_txt` 一致 |

### `tests/test_feishu_publish.py`

| 用例 | 断言 |
|---|---|
| `test_publish_archives_audio_before_build` | 发布的 `markdown` 含「## 录音」且链接 = 上传返回的 url |
| `test_publish_archive_failure_keeps_state_ok` | 归档失败时 `state` 仍 `ok`，`meta.archive_error` 有值 |
| `test_publish_ready_doc_has_audio_section` | `ready` 分支正文含录音链接 |
| `test_publish_archive_disabled_by_config` | `FEISHU_ARCHIVE_ON_PUBLISH=0` 时不调用 `ensure_archived` |
| `test_record_fields_include_transcript_link` | 阶段 2b 路径：`逐字稿链接` 有值 |

### `tests/test_audio_archive.py`

| 用例 | 断言 |
|---|---|
| `test_transcode_republishes_when_published` | 已发布场次转码后调度了一次 `publish_session_async` |

假对象沿用现有 `FakeFile` / `FakeMembers`（[test_feishu_drive.py:41](../tests/test_feishu_drive.py:41)），新增 `FakeFolders`（`create_folder` / `list`）。

---

## 8. 验收（人工，按顺序）

阶段 0 之后：

1. 本机 `pytest -q`（现有 887 + 新增全绿）。
2. 起引擎，文件中心找一场有录音的，**不点任何按钮**，直接 `/发布`。
3. 飞书里打开云文档：正文末尾有「## 录音」，点开能听。
4. 场次表该行 `录音链接` 有值。（Base 没权限时跳过，看 `meta_json` 的 `archive_error`）
5. 再 `/发布` 一次：`unchanged=true`，云端没有多出第二份音频。

阶段 1 之后：

6. `data/feishu.json` 不动，发布**新一场**，云空间 `audio_folder_token` 下多一个 `2026-11-27_HHMM_房间名/`，里面是 `标题.m4a`。
7. 同一场再发一次：文件夹不重复建（`created=false`），文件替换不并存。
8. `python tools/backfill_drive_folders.py --dry` 列出历史场次，`--limit 1` 真跑一场，确认文件被**移动**（原链接仍可打开）。

阶段 3 之后：

9. 群里拉一个没被单独授权的人，点文件夹链接能看。
10. 新建一场，文件自动出现在同一文件夹里，新人也能看。

---

## 9. 风险与回滚

| 风险 | 触发条件 | 处置 |
|---|---|---|
| 发布变慢 | `/api/publish` 同步上传 | 先测耗时；>60 秒改排队（阶段 0 §1.5 的 B） |
| 上传失败拖累发布 | 网络/配额 | 已设计为 warning，`state` 不变 |
| 文件夹重名 | 同一天同房间同分钟 | `list_folder` 复用，不建第二个 |
| 历史迁移搬错 | `--backfill` 全量跑 | 工具默认 `--limit` 必填，`--dry` 先行；move 可逆（再 move 回去） |
| 附件字段类型不被支持 | 阶段 2a 探针失败 | 走 2b URL 字段，功能不丢 |
| Base 权限长期不开 | 租户管理员不动 | 阶段 2 整段挂起，0/1/3 照常交付 |

回滚：阶段 0/3 是纯增量（新函数 + 开关），`FEISHU_ARCHIVE_ON_PUBLISH=0` 即回到今天；新增的 `sessions` 列不影响老读路径（`SELECT *` + `dict`）。

---

## 10. 一句话总结

**先让每场发布都自带一个能点开的录音链接（阶段 0），再把云端从「一个大文件夹」改成「一场一个文件夹、用 token 索引」（阶段 1），然后逐字稿跟着场次落盘（阶段 2 的 3.1），最后把文件夹授权给群（阶段 3）。** 附件字段和 `_v1/_v2` 都不在这一轮的主线上——前者卡在 `bitable:app`，后者卡在没有重转写。
