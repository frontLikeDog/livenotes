# 49 · 实施提示词：观点表接进飞书多维表格

> 用途：直接交给实施者（人或 AI）的完整任务书。
> 关联：[docs/47](47-feishu-native-reading-surface.md) §3.2 / §3.6.3 / §6，[docs/48](48-验收清单.md)
> 一句话：每场总结里的每条观点（`claims`），在飞书 Base 里落一行；按场次整体替换，幂等，失败不影响文档和采集。

---

## 任务

在 `livenotes` 仓库里，给已有的飞书发布器**增加第二张多维表格：观点表**。

现在的状态：发布器已经能把一场发布成「一篇云文档 + 场次表一行」（`app/feishu_publish.py`，P0 已完成，有测试）。你要加的是：同一场里的每条观点，写进同一个 Base 的「观点」表。这补的是「跨场追一个观点」——现在只能一场一场搜原话，没法把所有场次里同一主题的观点排在一起。

边界：Base 是索引，不是正文。逐字稿、长文本、音频都不进表。

---

## 先读这些（按顺序，别跳过）

1. [docs/47-feishu-native-reading-surface.md](47-feishu-native-reading-surface.md)
   - §3.2：观点表的字段设计
   - §3.6.3：写入规则（按场次整体替换、`claim_id` 的来历）
   - §6：单向发布，Base 不是真相源
   - §11：P0 落地时和方案的有意差异（哪些「没做」是故意的）
2. `app/feishu_publish.py`：`_publish()` 主流程、`ensure_base()`、`_create_table()`、`_find_table()`、`_ensure_fields()`、`_find_record()`、`_record_fields()`、`_upsert_session_row()`、`FIELD_DEFS`
3. `app/claims.py`：`extract()` / `rebuild_session()` / `rows_for()` / `person_key()` / `similarity()` / `REVISION_RATIO` / `SAME_RATIO`
4. `app/store.py` 第 378 行起：`claims` 表结构；第 479 行起：`feishu_publish` 台账
5. `tests/test_feishu_publish.py`：假飞书（`FakeApps` / `FakeTables` / `FakeFields` / `FakeRecords`）和 fixture 写法
6. `tests/helpers.py`：`add_session` / `add_rows` / `utt_row`
7. `app/timefmt.py::offset()`：把秒数格式化成 `9:09` / `1:02:05`

---

## 数据来源（不要重新算观点）

`claims` 表已经是「谁 × 主题 × 主张」的原子，每场 finalize 时由 `claims.rebuild_session()` 幂等重建。观点表只是它的投影。

关键事实：

- `claim_id` = `hash(session_id, 要点序号)`，同一场稳定；换模型重新总结会全变 → **按场次整体替换，不逐条 upsert**。
- `person_id` 可能为空。显示名规则：`person_id` 非空 → `persons.name`；否则用 `claims.who`（已命名者是昵称，未命名是「本场第 N 位上麦者」这类标签）。宁可显示槽位名，也不要猜。
- `topic` 是本场标注词，自由文本，不是闭集选项 → 表里用**文本**字段，不用多选（多选写不存在的选项会让整行写入失败）。
- `t` 是场次内秒数，用 `timefmt.offset(t)` 转成 `9:09` 形式的**纯文本**，不假装能点。
- 日期用 `session.started_at` 转毫秒（参考 `export.build_publish` 的 `start_ms` 写法，或 `export._pub_start`）。
- 场次文档链接**只能**来自 `feishu_publish` 台账（`kind="session"`）的 `doc_url`；没有就留空，不要编链接、不要拿本地 reader 链接冒充。

---

## 表结构

在**已有的 Base 里**新建一张名为 `观点` 的表（和 `场次` 表同一个 `app_token`），主字段必须是第一个字段。

| 字段 | 类型 | 来源 / 规则 |
|---|---|---|
| 观点 | 文本（主字段） | `claims.stance`；空则退 `gist`，再空退 `quote` 前 80 字 |
| 谁 | 文本 | `person_id` 非空 → `persons.name`；否则 `claims.who` |
| 主题 | 文本 | `claims.topic`；空着也照写 |
| 类别 | 单选 | `主张` / `问题`（`claims.kind`：`claim` / `question`） |
| 状态 | 单选 | `新提出` / `重申` / `修正`，见下文「状态派生」 |
| 场次 | 文本 | 场次标题（`sessions.title`） |
| 日期 | 日期 | `started_at` → 毫秒时间戳 |
| 时间码 | 文本 | `timefmt.offset(t)`，纯文本 |
| 原话 | 文本 | `claims.quote`（已截 300 字） |
| 场次文档链接 | URL | 台账 `doc_url`；没有就不写这个键 |
| claim_id | 文本 | 幂等/去重键，不给人看 |
| session_id | 文本 | 隐藏，排查和批量删除用 |
| person_id | 文本 | 可为空；将来关联人物表用 |

字段类型编号沿用现有 `FIELD_DEFS` 的写法：`1` 文本、`2` 数字、`3` 单选、`5` 日期、`15` URL。

默认视图名用 `最近观点`。**不要加筛选视图**：`app_table.view` 的筛选条件请求体在本仓库没有验证过，和场次表的处理保持一致。

---

## 建表与配置

- 复用 `ensure_base()` 拿到 `app_token`；把 `_find_table()` / `_create_table()` 参数化（表名 + 字段定义 + 默认视图名），默认值保持现有的 `场次` / `FIELD_DEFS`，不要复制粘贴第二份。
- `_ensure_fields()` 已经按 `(app_token, table_id)` 缓存，可以直接复用；把字段定义改成参数传入。
- 找不到 `观点` 表就建；建表失败按普通失败处理（见失败语义），不要抛异常到发布器外。
- `table_id` 记进 `index_meta` 的 kv：`feishu_base_claims_table_id`。环境变量 `FEISHU_BASE_CLAIMS_TABLE_ID` 优先，其次这个 kv。进程重启不重建、不重复建表。
- 授权：Base（`app_token`）级授权已经覆盖表内所有表，不需要按表再授权。实施时确认一下 `ensure_base()` 的 `_grant_doc(..., doc_type="bitable")` 在复用已有 Base 时也会执行；不会的话，第一次写观点表前补一次。
- 开关沿用 `FEISHU_PUBLISH_BASE=0`（`config()` 里的 `base_enabled`）：关掉时场次表和观点表都不写。**不要新增独立开关**，少一个用户要理解的东西。
- 只有本场确实有 claims、或台账里已经有成行记录时才确保建表/执行删除。没有总结、从没写过这一场 → 直接跳过，不建空表。

---

## 写入策略：按场次整体替换

新函数 `_upsert_claims(conn, bot, sid, doc_url, force=False)`，流程：

1. 读本场观点：`claims.rows_for(conn, session_id=sid, limit=1000)`；同时取场次标题、`started_at`、`persons` 名字映射。
2. 算签名：把每条 `claim_id|person_id|who|topic|stance|kind|quote|t|状态` 按固定顺序拼起来，加 `doc_url`，做 sha1 前 16 位。
3. 查台账 `store.get_publish(conn, sid, "claims")`：签名没变、上次 `state=="ok"`、且不是 `force` → 直接跳过，**不发任何删/写请求**。
4. 否则：
   - 按 `session_id` 搜索本场所有记录（`SearchAppTableRecordRequest`，`page_size=100`，翻页到 `has_more=false`；过滤条件写法照抄 `_find_record`：`Condition(field_name="session_id", operator="is", value=[sid])`）。
   - `batch_delete` 收集到的全部 `record_id`，每块 ≤500。
   - `batch_create` 新行，每块 ≤500（`BatchCreateAppTableRecordRequest` / `AppTableRecord`）。
   - 先删后写，不要逐条 update：要点数量会变，逐条 update 会留下多余旧行。
5. 写台账：`store.upsert_publish(conn, sid, "claims", state=ok/failed, version=签名, error=..., latency_ms=...)`。**没有观点时也要走这一步**（签名是空集），并把该场旧行删干净——重跑总结可能把观点清空，不能留孤儿行。
6. 观测：成功记 `logs.log_event("feishu.claims_upsert", session_id=..., records=N, deleted=N, created=N, action="created"|"skipped")`；失败写进台账并在 `_publish()` 的 warnings 里体现。

失败窗口说明：删成功、建失败 → 台账 `failed`；下次重试仍是「先搜（此时为空）→ 再建」，不会重复，也不会多删别的场次（过滤条件只有本场 `session_id`）。

---

## 状态派生（新提出 / 重申 / 修正）

对每一条 claim：

1. 分组键：`key = claims.person_key(person_id, who)`，主题用 `claim.topic`。
2. 找**更早场次**里同 key、同主题的最近一条（按 `sessions.started_at, claims.t` 倒序取 1）。**同场内的多条不互相比较**——一场里有几个要点，「纠正自己上一句」会把正常发言误判成改口。
3. 没有更早的 → `新提出`。
4. `claims.similarity(本场 stance, 上一条 stance)`：
   - `>= SAME_RATIO` → `重申`
   - `< REVISION_RATIO` → `修正`
   - 其余 → `新提出`（同主题下的新说法）
5. **不做「被反驳」**：现有引擎只有 `claims.disagrees()` 对原话做词面判断，没有「谁反驳了谁」的归属，硬写会误报。单选选项先只建三个，将来再加。
6. 已知局限，写进代码注释、不要偷偷修：状态是相对更早场次算的；补发更早的历史场次后，已发布场次的状态不会自动重算，除非对该场再 `/发布`。

---

## 挂到哪（集成点）

全部在 `app/feishu_publish.py`：

- 新增常量：`KIND_CLAIMS = "claims"`、`TABLE_CLAIMS = "观点"`、`CLAIMS_FIELD_DEFS`。
- `_publish()` 的**早退路径**（版本没变、上次成功）：如果 claims 签名变了，仍然只更新观点表，不碰文档正文（保住评论锚点）；返回值加 `claims_updated: True`。
- **正常路径**：场次表 `_upsert_session_row()` 之后调用 `_upsert_claims()`；用 try/except 包起来，失败只加 warning，**绝不让文档失败**。
- 文档失败但观点表成功：观点表照写（它们不依赖文档），只是没有文档链接；签名里包含 `doc_url`，下次文档重试成功后会重写观点表把链接补上。
- 收尾卡、`/文档`、`/发布` 命令**都不用改**。不要顺手扩范围。

---

## 失败语义（必须用测试固定）

| 情况 | 行为 |
|---|---|
| 观点表接口失败 | 文档 `ok`、场次表 `ok`，整场 `state=partial`，`error` 写「观点表没写成功：…」；下次 `/发布` 只重试观点表 |
| 文档失败 | 文档语义不变（`state=failed`）；观点表行照写，只是没有「场次文档链接」 |
| 没有总结 | 不创建行；如果这场的观点被清空，必须删掉旧行 |
| 飞书没配置 / `FEISHU_PUBLISH_BASE=0` | 静默跳过，不写失败台账（与现有语义一致） |
| 删除成功、创建失败 | 台账 `failed`；重试不重复 |
| 记录超过 100 条 | 分页也要删干净、建完整 |

---

## 测试（只做单元 / 轻量集成，不做截图）

新增 `tests/test_feishu_claims_table.py`，复用 `tests/test_feishu_publish.py` 里的假飞书；需要的话把 `Fake*` 抽到 `tests/helpers.py`，但**不要改坏现有测试**。

必须覆盖：

1. 一场 3 条观点 → 创建 3 行。
2. 同一场重复发布 → 先删 3 再建 3，最终仍是 3 行，台账只有一行（`kind="claims"`）。
3. 重跑后要点 3 → 2 → 最终 2 行，无孤儿。
4. 版本没变 → 不发 delete / create 请求。
5. 只有 `doc_url` 变了 → 只重写观点表，不重写文档正文。
6. 观点表接口失败 → 文档 state 仍 `ok`，整场 `state=partial`，error 含「观点表」。
7. 无总结 → 不 create；已有旧行 → 删除。
8. 未命名说话人 → `谁` 是「本场第 N 位上麦者」这类标签，`person_id` 为空。
9. 状态：前一场同主题相似 → `重申`；差异大 → `修正`；首见 → `新提出`。
10. 超过 100 行、分页两页 → 删除干净。
11. `FEISHU_PUBLISH_BASE=0` → 不建表、不发请求。
12. 老表缺字段 → 自动补（照场次表的写法）。

---

## 验收

```bash
.venv/Scripts/python -m pytest tests/test_feishu_claims_table.py tests/test_feishu_publish.py -q
.venv/Scripts/python -m pytest -q
```

- 全量必须绿。开工前先跑一次全量，把基线记下来（当前基线 886+，以你本地为准）。
- 不需要连真飞书；用假客户端就够。真租户验证由另一个人做。

---

## 明确不做

- 不做双向同步；Base 里的编辑不回流引擎。
- 不写逐字稿全文；原话已截 300 字，别再截更长。
- 不做人物表、待办表；不碰场次表的逻辑。
- 不做 Base 表单、仪表盘、自动化；不建飞书任务。
- 不改观点文案、不为了「好看」重写 stance。
- 不加新配置开关（除了可选的 `FEISHU_BASE_CLAIMS_TABLE_ID`）。
- 不用「关联」字段；用文本 `session_id` / `person_id` + URL。

---

## 交付

- `app/feishu_publish.py` 增量：常量、`ensure_claims_table()`、`_claims_records()`、`_upsert_claims()`、`_publish()` 挂钩
- `app/config.py` / `app/feishu.py`：读取 `base_claims_table_id`（可选键）
- `tests/test_feishu_claims_table.py`
- 在 `docs/47` §12 后补一段「观点表实施记录」：字段、替换策略、状态规则、已知局限
- 分支上提交，不直接推 main；提交信息说清「观点表」，方便回溯
