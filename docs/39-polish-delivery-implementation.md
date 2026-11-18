# 39 · M4 精修交付：实现方案与交付 SOP

> 状态：MVP 已实现（内部工作台）
> 日期：2026-11-18
> 关联：[docs/38](38-p2-growth-and-monetization-options.md)、[docs/37](37-cloud-storage-and-feishu-share.md)、[docs/30](30-scene-highlights-and-narrative-weekly.md)
> 一句话：**不是再生成一篇笔记，而是把「一场直播 → 客户能直接用的报告包」做成一条可质检、可版本化、可修订的生产线。**

---

## 0. 先定边界

### 0.1 这一版做什么

一个**内部交付工作台**（`/delivery`）：

```
建项目 → 自动出草稿 → 人工改 / 校对 → 质检 → 打包 → 标记交付 → 记录修订
```

- 每个项目绑定一场已录完的直播，但**一场直播可以交付给多个客户**（`session_id` 不唯一）。
- 报告按版本落库；重新打包永远取最新一版，不覆盖历史。
- 交付物是本地目录 + 同名 zip，可直接发飞书/微信/网盘。

### 0.2 这一版不做什么

| 不做 | 原因 | 替代 |
|---|---|---|
| 客户自助下单 / 支付 | 需要账号、计费、公网入口，属于 docs/38 的地基 | 先人工接单，后台建项目 |
| DOCX / PDF 直接生成 | 不引入 `python-docx` / `pandoc` | 自包含 HTML + 浏览器「打印 → 存为 PDF」 |
| 新的逐字稿编辑器 | 已有校对、改说话人、reader 三套能力 | 交付页只做报告层，逐字稿走原有流程 |
| AI 精修（润色/重写） | 先把「人工 + 现有笔记」跑通，避免把模型不确定性塞进交付 | 见 §8 Phase 2 |
| 公网交付页 / 下载链接 | 需要公网与授权 | 先发 zip；公网只读页见 docs/37 |

---

## 1. 状态机

```
draft ──保存/生成──▶ producing ──打包──▶ review ──标记交付──▶ delivered
  ▲                     │                  │                      │
  └──────重新生成草稿────┘                  └──────记录修订─────────┘
                                                       │
                                                       ▼
                                                   revising
                                                       │
                                              保存 / 重新打包
                                                       ▼
                                                   producing / review
```

- `draft`：刚建项目，还没有报告。
- `producing`：有草稿或人工版本，正在制作。
- `review`：交付包已生成，等客户确认。
- `delivered`：已交付，记录 `delivered_at`。
- `revising`：客户提出修改，`revision_count + 1`，备注里追加一条修订记录。

代码里的合法值在 [`delivery.STATES`](../app/delivery.py)；状态可以手工改，但动作会自动推进，避免忘记改状态。

---

## 2. 数据模型

两张表，加在 [`app/store.py`](../app/store.py) 的 `init_db` 里（`CREATE TABLE IF NOT EXISTS`，老库直接升级）：

### `delivery_projects`

| 字段 | 说明 |
|---|---|
| `id` | `dlv_YYYYMMDD_HHMMSS_xxxx` |
| `session_id` | 绑定的直播场次 |
| `client_name` / `event_name` | 客户与活动名，报告封面用 |
| `package` | `standard` / `deep` / `event` |
| `state` | 见 §1 |
| `due_at` | 截止日期；建项目时按套餐默认 2–3 天 |
| `price_cents` | 成交价（分），默认取套餐价，可改 |
| `checklist_json` | 人工勾选项 |
| `package_json` | 最近一次打包结果（文件、片段数、zip 路径、警告） |
| `notes` | 项目备注 + 修订记录 |
| `revision_count` | 修订次数 |
| `delivered_at` | 交付时间，月度交付额按它统计 |

### `delivery_reports`

| 字段 | 说明 |
|---|---|
| `project_id` + `version` | 唯一键，每次保存新增一版 |
| `sections_json` | 固定七章的正文与证据条目 |
| `engine` | `draft`（自动草稿）/ `manual`（人工保存） |
| `created_at` | 版本时间 |

**为什么版本只增不改**：客户说「还是上一版好」时能找回；重新生成草稿也不会把人工改过的内容冲掉——除非显式 `force`。

---

## 3. 草稿怎么来

`build_draft()` 不调模型也能出第一版，复用 [`report.session_material()`](../app/report.py)：

```
整场总结（有就用）
   ↓ 没有
高光 / scenes（有就用）
   ↓ 没有
逐字稿原句（按重要度 × 长度取）
```

这保证交付报告与笔记、周报用的是**同一套事实与时间码**，不会出现「报告里有一句，回听找不到」。

七章固定结构（顺序即导出顺序）：

| key | 章节 | 类型 | 草稿来源 |
|---|---|---|---|
| `lead` | 导语 | 文字 | 总结 overview；没有则「本场共 N 条发言」 |
| `context` | 本场概览 | 文字 | 时间、时长、说话人、主题、发言数 |
| `points` | 核心内容 | 要点 | 总结要点 / 高光 / 原句；每条带 `[HH:MM:SS]` 与说话人 |
| `quotes` | 金句 | 金句 | 高光里去重后的 3 句 |
| `clips` | 值得回听 | 片段 | 按套餐取 3 / 5 / 8 段，带起止时间码 |
| `actions` | 行动建议 | 文字 | 留空，人工写 |
| `questions` | 待确认 | 文字 | 留空，人工写 |

`points` 的 `items` 同时保存 `t / quote / speaker`，质检据此判断「有没有可回听证据」。

---

## 4. 质检门

`qa()` 输出 `pass / warn / fail`，页面右侧实时显示。质检**不阻止**打包，但阻断项应当在交付前清零。

| 检查 | pass | warn | fail |
|---|---|---|---|
| 场次存在 / 有逐字稿 | 有发言 | — | 场次丢失或 0 条发言 |
| 报告已生成 | 有导语 + 核心内容 | 导语为空 | 没有报告或核心内容为空 |
| 要点带时间码 | ≥1 处 `[HH:MM:SS]` | 报告未生成 | 核心内容没有时间码 |
| 说话人已实名 | 全部 `verified=1` | 标准套餐有未确认 | 深度/活动套餐有未确认 |
| 原声可用 | 录音文件存在 | 没有录音（只发文字） | — |
| 高光片段 | ≥ 套餐要求 | 少于要求 / 为 0 | — |
| 封面信息 | 客户 + 活动名 | 缺客户 | 缺活动名 |

左侧「交付检查」= 5 项人工勾选（授权、范围、逐字稿校对、术语敏感信息、报告通读）+ 6 项自动项（说话人/报告/证据/原声/打包/已交付）。

**人工项必须真的点过**：授权与使用范围是这一单的法律边界，不放进自动判断。

---

## 5. 交付包里有什么

`build_package()` 写入 `data/deliveries/<project_id>/`，再压成同名 zip：

```
报告.md              客户可读的 Markdown
报告.html            自包含 HTML，打印样式已写好 → 浏览器「打印 → PDF」
逐字稿.srt           现有导出，带时间码
逐字稿.md            现有导出，带说话人
高光原声/01_*.m4a    按 clips 章节时间码切，AAC-LC，微信/飞书可直接播
README.md            文件说明 + 授权与保留期
meta.json            项目、客户、版本、片段数、打包时间
```

- 音频切片走 [`audio.extract_m4a()`](../app/audio.py)；源文件缺失时跳过并在 README 里写警告，不阻断文字交付。
- 打包前会清空旧目录，**历史版本只在数据库里**；如果要留旧包，应在客户确认前另存。
- 报告 HTML 不依赖任何外部资源，客户离线也能打开。

### 套餐默认值

| key | 名称 | 默认价 | 片段数 | 默认工期 |
|---|---|---|---|---|
| `standard` | 标准复盘 | ¥999 | 3 | 3 天 |
| `deep` | 深度复盘 | ¥1999 | 5 | 2 天 |
| `event` | 活动 / 峰会 | ¥4999 | 8 | 3 天 |

价格只是默认值，成交价在项目里改；`price_cents` 用于月度交付额统计，不接支付。

---

## 6. 页面与接口

页面：[`app/web/delivery.html`](../app/web/delivery.html) + [`delivery.js`](../app/web/js/delivery.js) + [`delivery.css`](../app/web/css/delivery.css)。

接口（`app/server.py`）：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/delivery/projects?state=` | 列表 + 全局统计 |
| GET | `/api/delivery/project?id=` | 项目 + 最新报告 + 质检 + 检查单 |
| GET | `/api/delivery/report?id=&format=md\|html` | 直接下载报告 |
| GET | `/api/delivery/download?id=` | 下载 zip |
| POST | `/api/delivery/create` | 建项目并立即生成草稿（201） |
| POST | `/api/delivery/update` | 改字段 / 状态 / 人工勾选 |
| POST | `/api/delivery/draft` | 重新生成草稿，`force` 覆盖人工版 |
| POST | `/api/delivery/save` | 保存报告，新增一版 |
| POST | `/api/delivery/qa` | 重跑质检 |
| POST | `/api/delivery/build` | 打包，`audio:false` 可只出文字 |
| POST | `/api/delivery/deliver` | 标记已交付（必须先打包） |
| POST | `/api/delivery/revision` | 记录修订，进入 `revising` |
| POST | `/api/delivery/remove` | 删除项目、版本与本地包（不动直播） |

页面交互要点：

- 左列表筛选「全部 / 制作中 / 待交付 / 已交付」，统计卡始终按全部项目算。
- 报告章节是 `<details>` + `<textarea>`；`Ctrl/Cmd+S` 保存，不保存就离开会提示。
- 打包前若有未保存修改，会先要求保存，避免「包里的还是旧版」。
- 项目 `?id=` 同步到 URL，刷新后回到同一个项目。
- 桌面壳侧栏「交付」、命令面板「精修交付」和 `/delivery` 深链都指向这一页；壳内用 `embed=1` 打开，顶栏让给壳。

---

## 7. 人工交付 SOP

### 7.1 接单（5 分钟）

1. 确认三件事：**授权范围、交付时间、客户要什么**（逐字稿？报告？原声？）。
2. 在「交付」页新建：选场次、填活动名与客户、选套餐、改成交价与截止日。
3. 创建后自动生成草稿；把项目状态留在「制作中」。

### 7.2 制作（标准 60–90 分钟）

1. **先校对逐字稿**：说话人实名、明显错字、术语。交付页不重复做编辑器，走原有 reader / 校对流程。
2. 回交付页点「重新生成草稿」，让报告吃到校对后的文本。
3. 改五处：
   - 导语：写给客户老板看的三句话，不要复述流程；
   - 核心内容：每条保留时间码，删掉无结论的段落；
   - 金句：只留客户愿意对外引用的话；
   - 行动建议：1–3 条下一场能执行的动作；
   - 待确认：数据、承诺、嘉宾称谓等需要客户确认的口径。
4. 点「质检」，把 fail 清零；warn 逐条判断是否能接受。
5. 勾完人工检查项（授权、范围、校对、敏感信息、通读）。
6. 点「生成交付包」→ 下载 zip → 自己先打开 `报告.html` 和一段 m4a 听一遍。
7. 发给客户，状态改「待交付」。

### 7.3 交付后（10 分钟）

- 客户确认 → 「标记已交付」，计入本月交付额。
- 客户要改 → 「记录修订」，写清改什么；改完保存、重新打包，状态回到「待交付」。
- 修订记录自动追加到项目备注，下一单复盘时能看到客户的偏好。

### 7.4 质量红线

- 没有授权不进交付流程；
- 报告里的每个事实句必须能在时间码处回听到；
- 不把客户未确认的数据写成结论；
- 原声片段不公开传播，README 里已写授权说明；
- 删除项目会连本地包一起删，交付前确认客户已收到。

---

## 8. 后续阶段

| 阶段 | 内容 | 依赖 |
|---|---|---|
| P1（已做） | 项目、草稿、版本、质检、打包、交付/修订 | 无 |
| P2 | DOCX 模板（需 `python-docx`）、封面图/客户 logo、报告内嵌片段二维码 | 新依赖 / 公网 |
| P3 | AI 精修：按章节给「改写到什么程度」的提示，人工确认后替换；调用现有 LLM 档案与调度器额度 | `settings` + `scheduler` |
| P4 | 客户确认页：只读链接、逐章评论、确认/驳回回写状态 | docs/37 公网只读页 |
| P5 | 订单与计费：套餐下单、定金、发票、交付额对账 | docs/38 授权/支付地基 |
| P6 | 模板化：直播复盘 / 嘉宾观点 / 课程笔记不同章节与提示词 | 模板引擎 |

**建议先做 P3 的一个窄版本**：只对「导语」和「行动建议」给 3 条候选，人工点选，不自动覆盖。这两章最费时间，又最需要人判断。

---

## 9. 验证

已补单元 / 轻量集成测试：

- [`tests/test_delivery.py`](../tests/test_delivery.py)：建项目默认值、草稿章节与证据、人工版本与 `force`、坏数字容错、质检 fail/warn、打包文件与 zip、交付与修订、统计口径、字段更新与删除、HTML 转义。
- [`tests/test_p1_p2_api.py`](../tests/test_p1_p2_api.py)：`create → save → build(audio=false) → download → deliver` 的 HTTP 往返。
- [`tests/test_p1_pages.py`](../tests/test_p1_pages.py)：`/delivery` 页面路由与基座锚点。
- [`tests/test_desktop_shell.py`](../tests/test_desktop_shell.py)：桌面壳的侧栏入口、视图元数据、iframe 地址与深链路由。
- [`tests/conftest.py`](../tests/conftest.py)：`DELIVERIES_DIR` 指向 `tmp_path`，测试不碰 `data/`。
- [`tools/ui_smoke.py`](../tools/ui_smoke.py)：新增 `/delivery` 页面冒烟（DOM 节点 + 控制台报错）。

当前结果：`python -m pytest -q` → **646 passed**；`tools/check_web_layers.py` 通过；`node --check app/web/js/delivery.js` 与 `node --check desktop/dist/shell.js` 通过；`tools/ui_smoke.py --only delivery` 通过。

---

## 10. 已知限制

1. 报告 HTML 的「PDF」依赖浏览器打印，不保证分页与字体在客户机器上完全一致；重要客户建议人工导出后检查一遍。
2. 音频切片依赖 ffmpeg；没有 ffmpeg 时只出文字包，并在 README 与质检里提示。
3. 报告版本只增不删；项目多了以后需要加「清理 N 天前版本」的策略。
4. 说话人实名按 `speakers.verified` 判断；聚类标签未确认时，标准套餐只 warn，深度/活动套餐 fail。
5. 桌面壳已接入侧栏、命令面板与 `/delivery` 深链；已安装的旧包需要重新打包才会出现入口。
