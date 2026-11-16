# 36 · P1 迭代落地方案：让用户第二次打开

> 面向：产品、设计、实现
> 依据：[33 · P0 迭代方案](33-p0-hud-and-note-iteration-plan.md)、[34 · 新首页设计](34-new-homepage-design.md)、[35 · HUD/托盘/通知设计](35-hud-tray-notification-design.md)、[30 · 场面高光与叙事周报](30-scene-highlights-and-narrative-weekly.md)、[29 · 页面宿主契约](29-page-host-contract.md)、[16 · 录制分享选项](16-recording-sharing-options.md)
> 结论时间：2026-11-17
> 一句话：**P0 解决「第一次打开能拿到东西」，P1 解决「第二天为什么还回来」——回来的理由（关注开播）、回来的东西（笔记库/搜索/书签/周报）、回来的深度（问笔记），以及不回来也不骂人的底线（低配、文案、可访问性、导出）。**

阅读路径：只看结论读 §0、§15、§17；看排期读 §2、§15；看每个功能怎么落读 §3–§11；看数据和接口读 §12；看埋点读 §13；看测试读 §14。

---

## 0. 一页纸

### 0.1 这次要交付什么

P1 的九项功能不是九个并列需求，而是三个回访闭环：

| 闭环 | 功能 | 用户脑子里的问题 |
|---|---|---|
| **回来的理由** | 关注 + 开播提醒 + 自动记录 | 「他今天开播了吗？开了就帮我录。」 |
| **回来的东西** | 笔记库卡片流 + 全库搜索、实时高光 + 书签、周报、分享卡片 | 「我上次记的那场在哪？那句话怎么说的？」 |
| **回来的深度** | 问笔记（RAG）、导出集成 | 「我不想翻两小时逐字稿，直接问。」 |
| **底线体验** | 低配模式 / 性能、文案 / 视觉 / 可访问性 | 「别卡、别难懂、别只能用鼠标。」 |

交付后的完整体验：

1. 用户关注一个直播间，关掉应用；主播开播时收到一条人话通知，点开就能自动开始记录（可关）。
2. 第二天打开应用，笔记库是一面可扫读的卡片墙：标题、主播、时长、一句话总结、要点、状态；顶部一个搜索框能搜全部笔记和逐字稿。
3. 记录中按一下就是书签；系统觉得某段值得回听时，HUD 给一张不打断的记录卡，可试听、可收藏。
4. 一篇笔记可以生成一张竖版分享图（金句 / 要点 / 高光），带二维码；手机扫码能只读打开这篇笔记。
5. 每周一打开应用，能看到上周的周报：记了几场、讲了什么、哪三段值得回听。
6. 在笔记库里直接问「上周阿哲讲了哪些选品方法？」，答案带原话、时间码和可点开的出处。
7. 4 核 8G 的旧电脑开「低配」也能稳定录 1–2 路；所有新页面键盘可用、深浅色正常、读屏有标签。

### 0.2 与 P0 的关系：必须先补齐三块地基

这是本方案最重要的判断：**P1 的六个功能模块（关注、卡片/搜索、书签、分享卡、周报、RAG）都踩在 P0 的三块地基上，而当前代码只完成了通知这一块，笔记页和关注底座都还没有。**

| 地基 | 设计出处 | 代码现状（2026-11-17） | P1 处理 |
|---|---|---|---|
| 笔记页 v1 / `notes` 表 / `/api/note` | 33 方案 B | **未实现**：`app/server.py` 无 `/api/note`，`app/store.py` 无 `notes` 表 | 并入 P1 阶段 0（G1） |
| 关注 / 直播状态检测 / `follows` 表 | 34 §5 | **未实现**：无 `follows` 表、无 `/api/follows`、无检测 worker | 并入 P1 阶段 0（G2） |
| HUD / 托盘 / 通知中心 / `notifications` 表 | 35 | **已实现**（`app/notify.py`，9 类事件；壳有通知抽屉） | 只加 4 个事件类型（G3） |

不做阶段 0 的后果很具体：卡片流没有「打开笔记」的落点，开播提醒没有「打开关注」的落点，周报没有「跳到那场笔记」的落点，RAG 的引用也没有可点开的证据页。**因此本方案把阶段 0 算进 P1 排期，作为第一个 release 的前置，不假设 P0 已交付。**

如果 34 的首页仍未实施，P1 不重做首页：关注落到新页面 `/follows`，最近笔记落到 `/library` 顶部；等 34 的首页实施后，两者再收敛为首页模块。这样 P1 不被首页的 27 人日绑架。

### 0.3 不做什么（本迭代）

- 账号体系、云同步、多端同步、团队协作；所有数据仍只在本机。
- 关注主播的动态 Feed、直播日历、粉丝数、头像墙；关注只回答「开播了吗」。
- 向量数据库 / 文本 embedding。P1 的 RAG 用 FTS5 + 术语扩展 + 结构化笔记单元；P2 再评估向量召回。
- 直播画面截图、封面抓取、视频片段导出；分享卡只用文字和排版，避免版权与清晰度问题。
- 移动端 App、移动端记录；手机只读打开分享链接（局域网内）。
- 周报自动推送飞书群；保留现有飞书命令，站内周报优先。
- 通知分组、通知搜索、通知删除单条。
- 英文版 / i18n。
- 首页信息架构大改、监控墙、多路视图重做。

### 0.4 工作量与排期

按 1 名熟悉本仓库的高级工程师估算：

| 工作流 | 内容 | 人日 |
|---|---|---|
| **阶段 0 · G1** | 笔记页 v1、`notes` 表与状态机、`/api/note`、`/note` 页面 | 9–12 |
| **阶段 0 · G2** | `follows` 表、`/api/follows*`、直播检测 worker、`/follows` 页面 | 9–12 |
| **阶段 0 · G3** | 通知新增 4 类事件与设置分组 | 2–3 |
| **A 关注增强** | 开播提醒、自动记录、失败重试、确认文案 | 4–5 |
| **B 笔记库 + 搜索** | 卡片流、筛选/分页、`note_units` 检索索引、全局搜索面板 | 11–14 |
| **C 实时高光 + 书签** | HUD 实时高光卡、书签统一模型、笔记页书签区 | 7–9 |
| **D 分享卡片** | 3 套模板、Canvas 渲染、纯 JS 二维码、LAN 降级 | 6–8 |
| **E 周报** | `reports` 表、定时生成、`/weekly` 页面、周报通知 | 7–10 |
| **F 问笔记（RAG）** | 检索、时间解析、证据门、无模型降级、问答历史 | 10–14 |
| **G 低配模式** | 三档预设、自动判定、参数生效、性能自检 | 5–7 |
| **H 文案/视觉/a11y** | 术语表、错误映射、组件规范、键盘/对比度/读屏 | 4–6 |
| **I 导出集成** | `note_md`、批量/合集、分享包 v2、可选飞书 | 4–6 |
| **联调与验收** | 单测、轻量集成、UI 冒烟、狗粮、性能长跑 | 8–10 |
| **合计** | | **86–116 人日** |

- **2 人并行**（1 人引擎 / 1 人前端 + 壳）：**10–13 周**，其中 R1 5–6 周、R2 5–6 周、缓冲 1–2 周。
- **1 人顺序做**：**18–24 周**。
- **最小可回来切片（2 人 2 周，约 15 人日）**：关注检测 + 开播提醒（不含自动记录）+ 笔记库卡片只读（点进现有工作台）+ 搜索入口 + 通知事件。用于先验证「关注是否真的带来第二天回访」，再决定是否全量投入。
- **砍单顺序**（排期不够时从下往上砍）：F 问笔记 → D 分享卡 → E 周报 → A 的自动记录（保留提醒）→ C 的实时高光（保留书签）。**不能砍**：G1 笔记页、G2 关注底座、B 卡片流与搜索、开播提醒、G 低配、I 的 `note_md`。

### 0.5 验收口径

一次真实用户旅程走完，不需要命令行：

1. 干净 Windows 11：打开应用 → 关注一个直播间 → 关闭应用 → 主播开播 → 收到通知 → 点开自动记录 → 下播 → 收到笔记完成通知 → 打开笔记。
2. 笔记库：至少 10 篇笔记时，首屏是卡片墙；任意卡片一次点击进笔记；搜索一个两字词和一个三字词都能出结果。
3. 记录中：一键书签；实时高光卡出现时不遮挡计时和结束按钮；收藏的高光在收尾重算后仍在。
4. 分享：笔记页生成 1080×1440 PNG；开启「手机访问」时二维码可被手机扫开并只读打开笔记；关闭时不出死码。
5. 周报：周一 09:00 后首次打开应用，能看到上周周报；没有记录时是空态，不发通知。
6. 问笔记：20 篇笔记、10 个真实问题，至少 8 个给出带有效引用的答案；每个引用可点开到原声时间码。
7. 低配：4 核 8G 虚拟机，低配模式录 2 路 2 小时，不丢音频块、CPU 不超过 50%、界面可操作。
8. 只键盘完成：搜索、打开笔记、加书签、导出、关闭弹层；深浅色、125%/150% DPI 正常。
9. 所有新页面通过 `tools/check_web_layers.py`；`tools/ui_smoke.py` 覆盖新增页面；不新增 Python 三方依赖（分享卡二维码用前端纯 JS）。

### 0.6 关键取舍

| 取舍 | 选择 | 理由 |
|---|---|---|
| 笔记页 vs 卡片流谁先 | 笔记页先 | 卡片只是入口，没有完成态页面，卡片点进去还是工作台，P1 的「回来有东西看」不成立 |
| 关注页 vs 新首页谁先 | 关注页先 | 首页是 34 的大工程；关注只需要一个能管理、能看状态、能跳转的页面 |
| 书签是否建新表 | 不建 | 复用 `marks` + 手动高光，只给 `marks` 加 `kind`；避免两套时间码数据 |
| RAG 是否上向量 | P1 不上 | 笔记量是几百到几千单元，FTS5 trigram + 结构化单元 + 词表扩展足够；省一个依赖和一条索引链路 |
| 分享卡在哪渲染 | 前端 Canvas | 不引入 PIL/qrcode；WebView 自带 Canvas；导出 PNG 不经过服务端 |
| 周报是否重写生成器 | 不重写 | `app/report.py` 已有叙事、证据门、规则兜底；P1 只加存储、调度、页面和通知 |
| 低配是版本还是模式 | 模式 | 同一套安装包，按机器能力切换参数；用户可手动覆盖 |
| 实时高光是否自动存 | 不自动 | 只提示，用户收藏才落库；避免收尾时被大量低价值高光淹没 |

---

## 1. 为什么是这些功能

### 1.1 「第二次打开」的四个触发器

用户不会因为「功能多」回来，只会因为四件事回来：

1. **外部事件**：我关注的人开播了 → 关注 + 开播提醒 + 自动记录。
2. **未完成的事**：昨晚录的那场笔记生成了 / 上周的周报出来了 → 通知 + 周报。
3. **可检索的资产**：我记得他讲过一句什么，但忘了在哪 → 笔记库 + 搜索 + 书签。
4. **可表达的结果**：这段值得发给朋友 / 群里 → 分享卡片。

问笔记是第 3 条的加强版：当资产多到翻不动时，用自然语言直接取。低配、文案、可访问性、导出不是增长功能，而是让上面四件事在旧电脑、键盘用户、弱网环境下仍然成立。

### 1.2 设计原则

1. **每个回访入口都要落到一个已经完成的页面。** 通知点开不能是空页；卡片点开不能是工作台。
2. **先给答案，再给能力。** 笔记库先显示「上次拿到了什么」，搜索和问笔记放在同一个输入框里，不要求用户理解「全文检索 / RAG」。
3. **自动行为必须可控。** 自动记录默认关、一次只开一路、失败必告知、绝不自动停止。
4. **用户主动保存的东西优先于算法。** 书签、手动高光、收藏在重算后必须保留。
5. **本地优先，分享有边界。** 分享卡和二维码只在局域网可用时生成；不上传、不建账号、不做公网短链。
6. **工程词不上界面。** 沿用 33 §2.3 的术语规则：采集→记录，断流→听不到声音，聚类→说话人，场次→这场直播，token/队列/阶段码隐藏。

### 1.3 指标定义（本机口径，不上传）

产品是本地单机应用，没有服务端埋点。P1 的指标全部从本机 `metric_events` 与 `sessions` 计算，在 `指标` 页展示，用于狗粮和种子用户访谈；不上传、不建账号。

| 指标 | 定义 | 目标（方向性） |
|---|---|---|
| 激活 | 首次启动 24 小时内完成 ≥10 分钟记录，并打开过笔记 | ≥ 70% |
| 第二次打开（D1） | 首次成功记录后的次日历日内再次打开应用 | ≥ 45% |
| 有价值回访 | D1 打开后 5 分钟内发生任一动作：打开笔记 / 搜索 / 问笔记 / 看周报 / 从关注开播记录 / 导出 / 分享 | ≥ 70% 的 D1 |
| 关注转化 | 有 ≥1 个关注的用户次日回访率 | ≥ 60% |
| 卡片点击率 | 打开笔记库的人里点开至少一张卡片 | ≥ 35% |
| 搜索成功率 | 搜索后点击任一结果的比例 | ≥ 50% |
| 分享卡生成率 | 打开笔记的人里生成过分享卡 | ≥ 8% |
| 周报打开率 | 周一活跃用户里打开周报 | ≥ 50% |
| RAG 有效率 | 问笔记答案至少含 1 条有效引用的比例 | ≥ 80% |
| RAG 引用点击率 | 有答案的提问里点过引用 | ≥ 20% |
| 低配可用性 | 4 核 8G 机器低配模式 2 路 2 小时无丢块、无卡死 | 100% |

样本量小，目标是方向性的，不做统计显著性承诺。

---

## 2. 依赖与实施顺序

### 2.1 依赖图

```
G1 笔记页 /api/note ─────────┬──▶ B 卡片流（打开笔记）
                             ├──▶ C 书签区
                             ├──▶ D 分享卡（从笔记取内容）
                             ├──▶ E 周报（跳到笔记）
                             └──▶ F RAG（引用落到笔记时间码）

G2 关注底座 ────────────────┬──▶ A 开播提醒
                             └──▶ A 自动记录

G3 通知扩展 ────────────────┬──▶ A 开播/自动记录通知
                             └──▶ E 周报通知

B 的 note_units 索引 ───────┬──▶ B 全库搜索
                             └──▶ F RAG 检索

G 低配 ─────────────────────┴──▶ 所有页面的性能预算
H 文案/视觉/a11y ───────────┴──▶ 所有新页面（并行，不排最后）
```

### 2.2 三个 release

| Release | 内容 | 目的 | 2 人耗时 |
|---|---|---|---|
| **R1「回来有理由」** | G1、G2、G3、A、B、I 的 `note_md`、H 第一遍 | 关注开播能提醒；笔记库能翻能搜；笔记能导出 | 5–6 周 |
| **R2「回来有深度」** | C、D、E、F、G、H 收尾、I 收尾 | 书签/高光、分享卡、周报、问笔记、低配 | 5–6 周 |
| **缓冲** | 联调、狗粮、文案、性能、修复 | —— | 1–2 周 |

每个 release 结束都要能独立演示、独立回滚。R1 不依赖 R2 的任何模块；R2 的模块之间也尽量解耦（分享卡不依赖周报，周报不依赖 RAG）。

### 2.3 阶段 0 的交付边界

阶段 0 只做 P1 需要的最小集，不把 33/34 的全量设计搬进来：

- **G1 笔记页**：只做只读笔记页 + `notes` 状态 + `/api/note` + `note_md` 的最小导出；不做 33 里的重新生成全链路、过期提示、分阶段进度动画（这些在 R2 按需补）。
- **G2 关注底座**：只做 `follows` 表、增删改查、检测 worker、`/follows` 页面、首页/笔记库的摘要入口；不做 34 的完整首页、预检卡、样本笔记。
- **G3 通知**：只加 `follow_live`、`auto_record`、`auto_record_failed`、`weekly_ready` 四个 kind 与设置分组；不改通知中心结构。

---

## 3. 模块 A：关注 + 开播提醒 + 自动记录

### 3.1 产品定义

**关注的对象是直播间，不是人。** 沿用 34 §5.1：`platform + room_key` 唯一，名称只是展示字段，检测结果永远带「几分钟前检测」。

关注的三个动作：

1. **看状态**：直播中 / 未开播 / 检测失败，最近检测时间，最近一场笔记。
2. **收提醒**：`未开播/未知 → 直播中` 时提醒一次；同一场只提醒一次。
3. **自动记录**：每个关注独立开关，默认关；开启时确认；开播后自动开始记录，结束由看门狗收尾。

### 3.2 交互

**`/follows` 页面**（桌面壳新增 `follows` 视图，网页版 `/follows` 可直接打开）：

```
关注                                    [+ 关注]
──────────────────────────────────────────────
[直播中] 阿哲 · 抖音       今晚聊选品     已开播 12 分钟
         2 分钟前检测   [打开直播间] [开始记录] [自动记录 ✓]

[未开播] 小北 · 抖音       上次 11-14    3 分钟前检测
         [打开直播间] [自动记录 ○]

[检测失败] 老周 · 快手     链接可能失效   12 分钟前检测  [重试]
──────────────────────────────────────────────
共 3 个关注 · 检测每 2 分钟一次 · 设置
```

- 状态用「色点 + 文字」，不只靠颜色；直播中的行有且只有一个实心按钮（开始记录 / 回到记录）。
- 自动记录开关是次级控件；首次开启弹确认（文案见 §3.5）。
- 每行「更多」：改名、移除、暂停提醒、复制链接。移除不删历史笔记。
- 空态：「还没有关注。粘贴一个直播间链接，开播时我提醒你。」+ 输入框。
- 添加入口：`/follows` 顶部、笔记页「关注主播」、34 首页预检卡（若已实施）。P1 先做前两个。

**`/library` 顶部** 显示一行关注摘要（直播中 N 个 / 最近开播），点击进 `/follows`；不新增第二个实心按钮。

### 3.3 检测 worker

新增 `app/follows.py`，职责：表读写、状态机、检测调度、开播事件。

**检测方式**：把 `tools/direct_capture.py` 里的 `probe_room_live`、`parse_room_meta`、URL 规范化抽到 `app/liveprobe.py`，工具改为从 app 导入，保证「检测」和「直取录制」用同一套页面判定，不写两套。

```python
# app/liveprobe.py 目标接口
def probe(url: str, platform: str = "", timeout: float = 8.0) -> dict:
    """只读探测。返回 {state: live|offline|unknown, name, title,
    live_started_at, room_key, error}。不启动浏览器、不带 Cookie。"""
```

- 支持平台：抖音优先；快手、B 站尽力而为；其他平台返回 `unknown` + `error="这个平台还不能自动检测"`，UI 保留「打开直播间」。
- 抖音判定沿用现有保守规则：只有页面明确出现「直播已结束」才判 offline；其余按 live；无法解析判 unknown。**宁可说未知，不可误报。**
- 不登录、不携带用户 Cookie、普通 UA、只读 GET。

**调度**：

| 参数 | 值 |
|---|---|
| 间隔 | 默认 120 秒，设置可调 60–300 秒 |
| 抖动 | ±15% |
| 并发 | 2（`ThreadPoolExecutor`） |
| 单次超时 | 8 秒 |
| 失败退避 | 2 → 4 → 8 → 15 分钟，成功后恢复 |
| 上限 | 50 个关注 |
| 启动 | `run.py` 在 `make_server` 之后启动 daemon 线程；应用退出即停 |
| 暂停 | 所有关注的「开播提醒」与「自动记录」都关闭时，降到 300 秒；系统睡眠时线程自然顺延 |

**状态机**（沿用 34 §5.3，补 episode 字段）：

```
unknown --成功且直播中--> live      （新 episode，触发开播事件）
unknown --成功且未开播--> offline
unknown --连续 3 次失败--> unknown  （last_error，UI 显示检测失败）
live    --成功且未开播--> offline   （清 episode）
offline --成功且直播中--> live      （新 episode）
live/offline --单次失败--> 保持原状态，只更新 last_checked_at
live    --连续 3 个周期没有成功检测--> unknown（避免永远显示直播中）
```

- `episode` 取页面能拿到的 `live_started_at`；拿不到时，在 `offline/unknown → live` 的那次检测写入 `live_episode = now()`，直到成功检测到 offline 才清空。这样「live → unknown → live」的抖动不会重复提醒。
- 单次失败不改状态；连续 3 次失败才显示「检测失败」；卡片给「重试」，不自动无限请求。

### 3.4 开播提醒

- 触发：`offline/unknown → live`，且 `follow.notify=1`，且当前没有在记录同一个 `room_key`。
- 同一关注同一 `episode` 只发一条。去重键：`follow_live:<follow_id>:<episode>`。
- 文案：
  - 标题：**你关注的主播开播了**
  - 正文：`《阿哲》正在直播 · 已开播 3 分钟`（拿不到开播时间就说「刚刚开播」）
  - 点击：`view=follow`，带 `follow_id`；如果自动记录已启动，点击直接进 HUD。
- 应用在前台且聚焦：应用内 toast + 通知中心；隐藏/最小化：Windows 系统通知（沿用 35 §6.3）。
- **与自动记录合并**：如果该关注开启了自动记录并且启动成功，不再单独发 `follow_live`，只发 `auto_record`（标题「已自动开始记录《阿哲》」）。如果启动失败，只发 `auto_record_failed`。避免一次开播弹两条。此条需要拍板，见 §17 D5。

### 3.5 自动记录

**确认文案**（首次开启）：

> 开播时自动开始记录？
> LiveNotes 需要保持运行；最多同时记录 5 个直播间。结束记录后会自动生成笔记。

**触发条件**（全部满足）：

1. `offline/unknown → live`；
2. `follow.auto_record=1`；
3. 当前没有在记录同一个 `room_key`；
4. 当前记录数 < `watchlink.MAX_ROOMS`（默认 5，可用 `LIVENOTES_MAX_ROOMS` 覆盖）；
5. 应用在运行、引擎就绪；
6. 当前没有另一个自动记录正在启动（全局锁）。

**行为**：

- 同一时间只自动开始一路。多个关注同时开播时，按检测顺序取第一个；其余只发提醒（如果它们也开了自动记录，提示「另一个关注正在自动记录，这个只提醒」——P1 不做排队）。
- 启动路径复用 `/api/live/start` 的服务端处理体：抽成 `start_live(room_id)`，HTTP 与 worker 共用，不另写一套启动逻辑。
- 成功后：写 `auto_record` 通知（低优先级），`action.view=hud`，`session_id`；`follow.last_session_id` 更新。
- 失败：容量满 / 链接失效 / 平台风控 / 引擎未就绪，发 `auto_record_failed`，正文给人话原因 + 「打开关注列表重试」，不静默。
- 只对「启动失败」做一次 90 秒后的自动重试（网络抖动）；容量满、平台不支持不重试。
- 自动记录**不自动停止**；下播由现有看门狗收尾（断流 120 秒 / 全零 900 秒），收尾后自动生成笔记。
- 应用启动时发现关注正在直播：按同样规则处理；通知正文说明「已开播 N 分钟」。

### 3.6 数据与接口

`follows` 表沿用 34 §5.6，并在建表时直接补三列（如果 34 已经建过表，则走 `_migrate` 的 ALTER 模式）：

```sql
CREATE TABLE IF NOT EXISTS follows (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  platform         TEXT NOT NULL,
  room_key         TEXT NOT NULL,
  url              TEXT NOT NULL,
  name             TEXT NOT NULL DEFAULT '',
  live_title       TEXT NOT NULL DEFAULT '',
  live_state       TEXT NOT NULL DEFAULT 'unknown',  -- live|offline|unknown
  live_started_at  TEXT,
  live_episode     TEXT NOT NULL DEFAULT '',          -- P1 新增：去重
  last_live_at     TEXT,
  last_checked_at  TEXT,
  last_error       TEXT NOT NULL DEFAULT '',
  last_session_id  TEXT NOT NULL DEFAULT '',          -- P1 新增：最近一场
  fail_count       INTEGER NOT NULL DEFAULT 0,        -- P1 新增：连续失败
  notify           INTEGER NOT NULL DEFAULT 1,
  auto_record      INTEGER NOT NULL DEFAULT 0,
  created_at       TEXT NOT NULL,
  UNIQUE(platform, room_key)
);
```

接口沿用 34 §6.4：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/follows` | 完整列表 + 状态 + `last_session_id` |
| POST | `/api/follows` | `{url}` 或 `{platform, room_key, url}`；返回最新列表 |
| POST | `/api/follows/update` | `{id, name?, notify?, auto_record?}` |
| POST | `/api/follows/remove` | `{id}`；不删历史 |
| POST | `/api/follows/check` | `{id?}`；不传则全部；返回 200 + `last_error` |
| POST | `/api/watch/precheck` | 34 §6.3，用于添加前预检；P1 可先只返回平台/房间名/已关注 |

写操作成功后返回最新列表，前端不本地拼状态。检测失败也返回 200 + `last_error`。

### 3.7 技术落点

| 文件 | 改动 |
|---|---|
| `app/follows.py` | 新增：CRUD、状态机、`worker()`、`on_live()`、`maybe_auto_record()` |
| `app/liveprobe.py` | 新增：从 `tools/direct_capture.py` 抽出的只读探测 |
| `tools/direct_capture.py` | 改为从 `app.liveprobe` 导入，删除重复实现 |
| `app/store.py` | `_SCHEMA` 加 `follows`；`_migrate` 补 `fail_count` 等列 |
| `app/server.py` | 关注路由；`start_live()` 抽公共函数 |
| `app/notify.py` | 加 3 个 kind 与业务事件函数 |
| `app/settings.py` | `follow_interval_s`（60–300，默认 120） |
| `run.py` | 启动检测 worker |
| `desktop/dist/shell.js` | 新增 `follows` 视图与通知路由 |
| `app/web/follows.html` + `js/follows/*` | 新增基座页面 |
| `tools/ui_smoke.py` | PAGES 加 `/follows` |

### 3.8 验收

- 关注一个真实抖音直播间，主播开播后 ≤ 2 个检测周期 + 网络时延收到通知；同一场不重复。
- 关闭应用期间开播：启动后检测到直播中，按自动记录设置处理；不补发关闭期间的每条开播通知。
- 自动记录开启：开播后自动开始，HUD 出现该路；容量满时收到失败通知，正文说明原因。
- 自动记录关闭：只提醒，不创建房间。
- 检测失败 3 次：卡片显示「检测失败」，不显示「未开播」；重试成功后恢复。
- 移除关注：历史笔记和录音仍在。

---

## 4. 模块 B：笔记库卡片流 + 全库搜索

### 4.1 从「文件中心」到「笔记库」

现有 `/library` 是一张表：场次、时间、时长、音频大小、操作。它回答「文件在哪」，不回答「我记过什么」。P1 把它改成卡片流，保留「录音 / 回收站 / 清理」tab，但默认 tab 是「笔记」。

页面骨架：

```
笔记库                                        [搜索全部笔记  ⌘K]
[全部] [有笔记] [生成中] [待生成] [本周] [关注]     最近记录 ▾
──────────────────────────────────────────────────────────────
2026 年 11 月
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 11-14 · 1:42 │ │ 11-13 · 0:58 │ │ 11-12 · 2:10 │
│ 中小主播的选品…│ │ 抖音起号的三个…│ │ 复盘：这场为什么…│
│ 阿哲 · 抖音   │ │ 小北 · 抖音   │ │ 老周 · 快手   │
│ 低价不是核心… │ │ 先做人群再做… │ │ 流量来了接不住…│
│ 要点 3 · 高光 3│ │ 要点 4 · 高光 2│ │ 生成中 62%    │
│ [打开笔记]    │ │ [打开笔记]    │ │ [查看进度]    │
└──────────────┘ └──────────────┘ └──────────────┘
```

### 4.2 卡片规格

| 区域 | 内容 | 规则 |
|---|---|---|
| 封面区 | 日期、时长、状态标签 | 不用直播截图；用主题色渐变 + 首条高光金句做底纹；无高光时用纯色 |
| 标题 | 笔记标题 | 最多 2 行；没有标题时用「11-14 的直播」 |
| 来源 | 主播 / 房间名 · 平台 | 没有房间信息时省略平台 |
| 一句话 | 总结的 overview | 最多 2 行；没有总结时显示「还没有生成笔记」 |
| 元信息 | 要点数 · 高光数 · 说话人数 | 只用有值的项；生成中显示进度 |
| 操作 | 打开笔记 / 更多 | 整卡可点；「更多」里是导出、分享卡、关注主播、删除 |

**状态**（与 33 §4.5 的 `note_state` 一致）：

| 状态 | 卡片表现 | 操作 |
|---|---|---|
| `ready` | 正常卡片 | 打开笔记 |
| `partial` | 标签「部分完成」 | 打开笔记；提示缺什么 |
| `stale` | 标签「原文有更新」 | 打开笔记；建议重新生成 |
| `running/queued` | 进度条 + 「生成中 62%」 | 查看进度；不自动跳转 |
| `failed` | 标签「生成失败」+ 一行原因 | 重试；打开逐字稿 |
| `empty` | 「只有逐字稿」 | 打开工作台；生成笔记 |
| `insufficient` | 「内容太少，没生成笔记」 | 打开逐字稿 |

- 生成中的卡片每 3 秒轮询一次，只轮询当前视口内的；页面隐藏时停轮询。
- 卡片点击进 `/note?session=<id>`；生成中进 `/note` 的进度态；失败进失败态 + 重试。
- 「更多 → 分享卡片」在 R2 接入；R1 先留位。

**筛选与排序**：

- 全部 / 有笔记 / 生成中 / 待生成 / 本周 / 关注（关注过的房间的场次）。
- 排序：最近记录（默认）/ 最长 / 最近生成。
- 分组：按月；同月内按开始时间倒序。
- 分页：每页 24 张，滚动到底加载下一页；游标是 `(started_at, id)`，不用 offset，避免删除后跳页。
- 1000 场以内不虚拟化；超过 1000 场按 24 张分页即可。

### 4.3 全库搜索

**入口**：

1. 笔记库顶部搜索框；
2. 全局 `Ctrl+K`（桌面壳与网页版都支持）；
3. 笔记页顶部「搜索全部笔记」；
4. 桌面壳现有的「选中文字 → 全库搜索」postMessage `ln-run-search` 保留。

**结果面板**（覆盖在页面上方，不跳页）：

```
搜索：选品
────────────────────────────────────────
笔记 3
  中小主播的选品与复购        阿哲 · 11-14
  「低价不是核心，复购取决于选品和信任……」
逐字稿 12
  阿哲 12:30  「选品要看复购，不是看单场 GMV……」
人物 1
  阿哲（说话人 1）· 出现 8 场
术语 1
  复购率  → 术语表
────────────────────────────────────────
↑↓ 选择   Enter 打开   Esc 关闭     在全部笔记中搜索
```

- 分组顺序：笔记 → 逐字稿 → 人物 → 术语；每组最多 5 条，回车打开第一条。
- 结果行显示来源、标题/说话人、时间码、命中片段（高亮关键词，前后各 20 字）。
- 搜索防抖 250ms；少于 1 字不请求；两字词走 LIKE，三字及以上走 FTS5 trigram（沿用 `store.search_all` 的策略）。
- 空态：「没有找到。试试更短的关键词，或者直接问笔记。」（R2 接入问笔记）。
- 最近搜索 10 条，只存本机 localStorage；可一键清空。
- 搜索不改变当前页面状态；打开结果后 Esc 回到原位置。

### 4.4 检索索引 `note_units`

搜索要覆盖的不只是逐字稿，还有笔记要点、总结、高光、书签。P1 新增一张结构化检索表，同时服务全库搜索和 RAG：

```sql
CREATE TABLE IF NOT EXISTS note_units (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  kind       TEXT NOT NULL,   -- summary | point | highlight | bookmark
  t          REAL,            -- 可跳转时间码；summary 为 NULL
  text       TEXT NOT NULL,
  source_key TEXT NOT NULL UNIQUE,  -- point:<sid>:<idx> / hl:<id> / mark:<id>
  meta_json  TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS note_units_fts USING fts5(
  text, content='note_units', content_rowid='id', tokenize='trigram'
);
```

- 写入时机：笔记生成完成（`note_ready`）、高光替换、手动高光开关、书签增删、总结重建。
- 实现：`app/notes.py` 的 `index_session(conn, sid)` 做「按 `source_key` upsert + 删除已不存在的 source_key」，幂等。
- 启动时 `ensure_index()`：检查 `index_meta` 的版本；没有索引则后台分块重建（每批 50 场），不阻塞启动。
- 两字词回退 LIKE（trigram 对两字词无效，沿用现有策略）。
- 索引只存文本片段，不存音频、不存完整逐字稿；单条 `text` 截断到 500 字。

### 4.5 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/library/cards?limit=24&cursor=&filter=all|ready|running|nogen|week|follow&sort=time|duration|generated` | 卡片流；返回 `{items, next_cursor, counts, stats}` |
| GET | `/api/library` | 保留旧表格接口，兼容现有页面和测试 |
| GET | `/api/search?q=&types=notes,utterances,people,glossary&limit=` | 扩展：默认行为不变；`types` 可缩小范围；结果行带 `kind` |
| GET | `/api/note?session_id=` | 笔记聚合，见 §5 / 33 §6.1 |

`/api/library/cards` 的 item：

```json
{
  "session_id": "20261114_2103_ab12",
  "title": "中小主播的选品与复购",
  "streamer": "阿哲", "platform": "douyin",
  "started_at": "2026-11-14 21:03", "duration_s": 6120,
  "note_state": "ready", "progress": 100,
  "summary": "低价不是核心，复购取决于选品和信任……",
  "n_points": 3, "n_highlights": 3, "speaker_count": 2,
  "n_bookmarks": 1, "audio_state": "ready",
  "topics": ["选品", "复购"]
}
```

### 4.6 技术落点

- `app/store.py`：`list_sessions` 保持现状；新增 `library_cards(conn, cursor, filter, sort, limit)`，用一条 SQL 左连 `summaries`（取最新）、`notes`，避免 N+1。
- 现有 `_library_rows` 对每行 `Path.stat()` 音频大小，1000 行会慢；卡片接口不算音频大小，只返回 `audio_state`（`audio_path` 非空即可），大小只在详情/文件 tab 算。
- `app/notes.py`：`derive_state(conn, sid)`、`build_note`、`index_session`、`ensure_index`。
- `app/server.py`：`/api/library/cards`、`/api/search` 扩展、页面路由 `/note`。
- 前端：`app/web/library.html` 重写为卡片流；`app/web/js/library/*`；搜索面板 `app/web/js/searchpalette.js`，所有基座页面共用（不在页面里写 `embed` 逻辑）。
- 性能：1000 场首屏 P95 < 400ms；搜索 P95 < 200ms；卡片滚动 60fps（低配模式降到 30fps 且关闭封面渐变）。

### 4.7 验收

- 10 篇以上笔记时，首屏是卡片；点任意卡片 1 次进入笔记。
- 生成中的卡片有进度，完成后自动变「打开笔记」（收到 `note_ready` 通知或轮询）。
- 两字词（如「选品」）和三字词（如「复购率」）都能搜到；结果点开落在对应时间码。
- 搜索面板键盘完整：`Ctrl+K` 打开、↑↓ 选择、Enter 打开、Esc 关闭。
- 1000 场数据下翻页不跳页、不重复、不卡顿。

---

## 5. 模块 C：实时高光 + 书签

### 5.1 书签的产品定义

**书签 = 用户在记录中或读稿时主动标记的一个时间点。** 它和「高光」的区别：

| | 书签 | 高光 |
|---|---|---|
| 谁产生 | 用户按「标记」 | 算法评分 + 用户收藏 |
| 形态 | 一个时间点 + 一句备注 | 一个 20–90 秒的片段 + 金句 |
| 用途 | 「这里我要回来看」 | 「这段值得回听/分享」 |
| 存储 | `marks`（P1 加 `kind`） | `highlights`（P1 加 `source`；`kind` 仍是评分类型 `info/live/both`） |

P1 不建新表：书签继续用 `marks`，加一列 `kind`（`mark` | `bookmark`），默认 `mark`；`note` 字段就是备注。笔记页的「书签」区把 `marks` 和 `source in ('manual','live')` 的高光合并展示，按时间排序。

**这里有一个现成字段的坑**：`highlights.kind` 现在的取值是 `info / live / both / manual`，其中 `live` 表示「情绪/互动型高光」，不是「实时产生的」。所以 P1 不改 `kind` 的语义，新增 `source` 列（`auto` | `scenes` | `live` | `manual`）来标记来源；收藏实时高光时保留原 `kind`，写 `source='live'`。

### 5.2 记录中：一键书签

- HUD 现有「标记」按钮保留；点击立即写入 `marks`，toast「已加书签 00:12:30」+「撤销」（5 秒）。
- 快捷键：`B`（HUD 聚焦时）；不抢占输入框。
- 长按不采用（不可发现）；「标记」按钮旁的小提示第一次出现时说明「按一下加书签」。
- 书签写入不阻塞录音；失败时 toast 说明，不弹窗。

### 5.3 记录中：实时高光卡

现有 `OnlineScorer` 已经在实时评分（窗口 900 秒、阈值 2.5、hold 45 秒、immediate 3.2），`live.py` 也已经把强高光推到飞书。P1 把同一批事件在本地露出来：

- `/api/live/status` 增加 `live_highlights`：最近 3 条、未过期的候选，字段 `{id, t0, t1, quote, score, level}`；新增 `GET /api/live/highlights?session_id=&since=` 供 HUD 轮询（3 秒，仅在记录中）。
- HUD 在计时卡下方显示一张窄条，不遮挡计时/结束：

```
┌────────────────────────────────────────────┐
│ 这段话可能值得回听  12:30–12:58            │
│ 「选品要看复购，不是看单场 GMV……」          │
│ [试听 20 秒]  [收藏]  [不用]                │
└────────────────────────────────────────────┘
```

- 同时只显示 1 条；用户「不用」后 2 分钟内不再弹；暂停/收尾时不弹。
- 「试听」播放 `t0-3s` 到 `t1+3s`，复用现有音频播放器；「收藏」写入 `highlights`，保留事件原本的 `kind`（`info/live/both`），写 `source='live'`，去重键 `live:<session>:<t0>`。
- 实时高光卡不进入通知中心，不弹系统通知；它是记录界面的轻提示。
- 低配模式下关闭实时高光卡（仍保留书签），见 §9。

### 5.4 笔记页与阅读器

- 笔记页新增「书签」区：时间码、备注、来源（记录中 / 读稿时）、跳转。
- 阅读器（`/reader`）左侧逐字稿支持「加书签」；已有 `marks` 的显示为书签图标。
- 高光区把 `source in ('manual','live')` 的高光排在自动高光前面，标「你收藏的」。
- 收尾重算高光时，`store.replace_highlights` 必须保留 `source in ('manual','live')`；当前只保留 `kind='manual'`，需要扩展为「`kind='manual' OR source IN ('manual','live')`」。
- 导出 `note_md` 包含书签列表；分享卡可选择「用这条书签做金句卡」。

### 5.5 数据与接口

```sql
-- 迁移 1：书签类型；保留 note 作为备注；不新增表。
ALTER TABLE marks ADD COLUMN kind TEXT NOT NULL DEFAULT 'mark';
-- 迁移 2：高光来源；不改变 kind 的评分语义。
ALTER TABLE highlights ADD COLUMN source TEXT NOT NULL DEFAULT 'auto';
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/live/mark` | 现有；扩展 `{session_id, t?, note?, kind?}`；`t` 缺省为当前 elapsed |
| DELETE | `/api/live/mark?id=` | 新增：撤销书签 |
| GET | `/api/live/highlights?session_id=&since=` | 新增：实时高光候选 |
| POST | `/api/highlights/save` | 新增：`{session_id, t0, t1, quote?, kind?}`；保留评分类型，写 `source='live'` |
| GET | `/api/note?session_id=` | 返回体增加 `bookmarks: [{id,t,note,kind}]` |

`/api/note` 其余字段以 33 §6.1 为准，P1 只增不改。

### 5.6 技术落点

| 文件 | 改动 |
|---|---|
| `app/live.py` | `status()` 带 `live_highlights`；`mark()` 接受 kind；新增 `save_live_highlight()` |
| `app/highlight.py` | 只读复用 `OnlineScorer` 的事件；不改评分 |
| `app/store.py` | `marks.kind`、`highlights.source` 迁移；`insert_mark`/`list_marks` 带 kind；`replace_highlights` 保留 `kind='manual' OR source IN ('manual','live')` |
| `app/scenes.py` | 保存实时高光时复用 `scenes` 的 quote/hook 结构，避免两套 |
| `app/server.py` | 新增路由 |
| `app/web/js/index/live.js` / HUD in `desktop/dist/shell.js` | 实时高光条、书签撤销 |
| `app/web/note.html` | 书签区 |

### 5.7 验收

- 记录中按「标记」1 次完成书签；5 秒内可撤销；书签在笔记页可见、可跳转。
- 实时高光卡出现时，计时、暂停、结束按钮仍可点；「不用」后 2 分钟内不再出现。
- 收藏的实时高光在收尾 `scenes.build` 重算后仍在，并出现在笔记页「你收藏的」。
- 导出 `note_md` 包含书签；分享卡可选书签作金句。

---

## 6. 模块 D：分享卡片（竖版图 + 二维码）

### 6.1 产品形态

入口：笔记页「分享」→「生成分享卡」；笔记库卡片「更多」→「分享卡片」；周报页「生成周报卡」（R2 复用同一渲染器）。

三套模板：

| 模板 | 内容 | 适合 |
|---|---|---|
| **金句卡** | 一句原话（≤120 字）+ 说话人 + 时间码 + 笔记标题 | 发朋友圈 / 群 |
| **要点卡** | 标题 + 3 条要点（每条 ≤42 字）+ 二维码 | 发社群 / 收藏 |
| **高光卡** | 一段 20–90 秒高光的时间段 + 金句 + 3 个标签 | 发回听邀请 |

规格：

- 1080 × 1440（3:4 竖版），PNG；安全边距 72px；标题最大 2 行，每行 ≤18 字；要点最多 3 条；超出截断加省略号。
- 字体用系统字体栈（Windows：微软雅黑 / Segoe UI；手机查看不依赖 Web Font）；字号 64/40/32/28。
- 底部：日期、LiveNotes 标识、二维码（180×180，仅 LAN 可用时）。
- 深浅色不影响导出；导出固定浅色纸面（打印/转发更稳）。
- 文件名：`LiveNotes_<标题>_<YYYYMMDD>.png`，非法字符替换，最长 80 字。

### 6.2 二维码与局域网

分享卡的价值在「手机扫一下能看」，但产品是本地单机、无账号、无公网。沿用 16 的边界：

- **只有 `lan.status().enabled` 为真时才生成二维码**；二维码内容是 `http://<局域网IP>:<端口>/note?session=<id>&share=1`。
- 关闭「手机访问」时，不画二维码，底部改为「在 LiveNotes 中打开这场笔记」；不生成死码。
- 二维码旁标注「同一 Wi-Fi 下可打开」；页面顶部提示「二维码只在局域网有效，换网络后可能失效」。
- `/note?share=1` 是只读模式：隐藏生成、删除、设置等操作；手机上单列布局，最小宽度 360px。
- 不在卡片上放完整逐字稿、说话人真实姓名（除非用户选）；默认只放标题、金句、时间码。
- 分享卡不写入数据库、不上传；只存在于用户下载的文件里。

### 6.3 技术落点

- **前端 Canvas 渲染**：`app/web/js/sharecard.js`，输入是 `/api/note` 的精简 payload + 用户选择的模板/条目，输出 `canvas`。
- **二维码纯 JS**：vendor 一个 MIT 的 QR 编码器（`app/web/js/vendor/qrcodegen.js`，约 10KB），不引入 Python `qrcode` / `Pillow`，不改 `requirements.txt`。
- **导出**：`canvas.toBlob('image/png')` → 下载；支持「复制图片」时用 `ClipboardItem`，不支持则只给下载。
- **预览**：对话框内按 0.5 缩放预览，确认后按 2x 渲染（2160×2880）再缩到 1080×1440，保证文字清晰。
- **降级**：Canvas 不可用（极老 WebView）时，隐藏入口并提示「当前环境不支持生成图片，可复制文字」。
- **性能**：生成 < 500ms；PNG 300–800KB；低配模式关闭阴影/渐变，生成 < 800ms。
- **无障碍**：分享卡对话框可用键盘操作；生成结果有 `aria-live` 提示；图片提供 alt 文本（标题 + 日期）。

### 6.4 接口

不需要新的服务端渲染接口。复用：

- `GET /api/note?session_id=` 取内容；
- `GET /api/share/info?session_id=` 扩展返回 `{lan_enabled, note_url, qr_url}`；
- `POST /api/metric/event` 记 `share_card_export`（§13）。

如果 `lan_enabled=false`，`note_url` 为空，前端不画二维码。

### 6.5 验收

- 笔记页生成金句/要点/高光三种卡，均为 1080×1440 PNG，文字不溢出、不重叠。
- 开启「手机访问」时，手机扫码能打开只读笔记页；关闭时不出现二维码。
- 二维码内容随 LAN IP 变化重新生成；旧卡上的码失效时，页面提示重新生成。
- 长标题、超长金句、无高光、无要点四种边界都有合理截断或占位。
- 生成分享卡不产生服务端临时文件；不上传任何内容。

---

## 7. 模块 E：周报

### 7.1 产品定义

周报回答三个问题：**上周记了几场？讲了什么？哪三段值得回听？**

`/weekly` 页面：

```
周报                                    上周（11.10–11.16） ▾
──────────────────────────────────────────────────────────
一句话：上周你记录了 4 场直播，主题集中在「选品」和「起号」。
[4 场] [6 小时 12 分] [9 段高光] [3 位主播]

本周主题
  选品  ████████ 42%
  起号  █████ 26%
  复盘  ███ 16%

每日
  周一  阿哲《今晚聊选品》        1:42  [打开笔记]
  周三  小北《起号的前七天》      0:58  [打开笔记]
  ...

值得回听
  1. 阿哲 12:30  「选品要看复购……」  [试听] [打开笔记]
  2. ...
──────────────────────────────────────────────────────────
[导出 Markdown]  [生成周报卡]  [推送到飞书（可选）]
```

- 默认显示上周；下拉可切最近 12 周。
- 没有记录的周：空态「这周还没有记录」+「去记录一场」；不生成空周报、不发通知。
- 周报里的场次、高光都可点回笔记/时间码。
- 导出 Markdown 复用 `app/report.py` 的 `render_weekly`；站内页面用结构化数据渲染，不直接渲染 Markdown。

### 7.2 生成与调度

现有 `app/report.py` 已有周报的叙事、证据池、证据门、规则兜底，`tools/weekly_report.py` 和飞书 `/周报 --doc` 已能用。P1 不重写生成器，补三件事：

1. **存储**：新增 `reports` 表，记录周期、状态、文件路径、结构化数据。
2. **调度**：`run.py` 启动一个每日 09:00 的轻量 tick；周一（或周一之后首次启动）检查上周是否有场次，没有就跳过；有且没有 `ready` 记录就提交生成。
3. **通知**：生成成功后发 `weekly_ready`，每周一条。

`reports` 表：

```sql
CREATE TABLE IF NOT EXISTS reports (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,          -- weekly | retro
  period       TEXT NOT NULL,          -- 2026-W46（ISO 周）
  since        TEXT NOT NULL,
  until        TEXT NOT NULL,
  state        TEXT NOT NULL,          -- queued|running|ready|failed|empty
  path         TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  summary      TEXT NOT NULL DEFAULT '',
  data_json    TEXT NOT NULL DEFAULT '{}',
  error        TEXT NOT NULL DEFAULT '',
  generated_at TEXT,
  UNIQUE(kind, period)
);
```

- `data_json` 存页面要用的结构化数据（场次、主题占比、高光、每日分组），由 `report.py` 在生成时一并产出；Markdown 文件仍是导出物。
- 幂等：唯一键 `(kind, period)`；同一周重复触发只更新状态，不重复生成。指纹包含「场次数 + 最后一场结束时间 + 提示词版本」，场次没变就复用。
- 无 LLM：走现有规则叙事，不阻塞、不报错。
- 生成耗时目标：≤ 30 秒（20 场以内）；失败重试 1 次，仍失败就 `failed` + 页面「重新生成」，不打扰用户。
- 旧周报迁移：启动时扫描 `REPORTS_DIR/weekly_*.md`，按文件名补 `reports` 行（`data_json` 为空，页面降级为 Markdown 渲染），幂等。
- 保留期沿用 30 天；清理文件时同步删 `reports` 行。

### 7.3 通知

- `weekly_ready`：标题「上周的周报出来了」，正文「4 场 · 6 小时 12 分 · 9 段高光」，点击 `view=weekly`，去重键 `weekly:<period>`。
- 默认开；归入「周报」设置分组。
- 应用在周一 09:00 没运行：下次启动时补生成，但**只发一条**；如果已经过了周三，不再补发通知，只在周报页显示。
- 明确不发：空周、生成失败的重试中、同一周第二次生成。

### 7.4 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/reports?kind=weekly&limit=12` | 列表：period、state、summary、generated_at |
| GET | `/api/reports/get?id=` 或 `?period=2026-W46` | 结构化 `data` + Markdown 内容（只读文件，≤200KB） |
| POST | `/api/reports/build` | `{kind:"weekly", period?}`；提交调度器；返回 job/状态 |
| POST | `/api/reports/push` | 可选：推送到飞书；复用现有文档推送能力 |

页面 `/weekly` 是基座页面，遵循 29；桌面壳新增 `weekly` 视图。

### 7.5 验收

- 周一 09:00 后打开应用（应用在运行或刚启动），能看到上周周报；同一周只生成一次、只通知一次。
- 上周没有记录：不生成、不通知、页面空态。
- 20 场数据的周报生成 ≤ 30 秒；无 LLM 也有规则版。
- 周报里的场次和高光能跳到笔记对应时间码。
- 导出 Markdown 与页面内容一致；飞书命令不回归。

---

## 8. 模块 F：问笔记（RAG）

### 8.1 产品定义

入口：

1. 笔记库顶部搜索框旁的「问笔记」tab；
2. 笔记页「问这场」；
3. `Ctrl+K` 搜索面板底部的「直接问笔记」。

答案格式：

```
问：上周阿哲讲了哪些选品方法？

答：他主要讲了三点：先看复购而不是单场 GMV；用少量 SKU 测人群；
   把退货率作为选品的一票否决项。[1][2]

[1] 阿哲《今晚聊选品》 12:30  「选品要看复购，不是看单场 GMV……」
[2] 阿哲《今晚聊选品》 28:10  「退货率高的品，起量越快死得越快……」

以上来自你的 2 场笔记 · 查看全部出处
```

- 引用必须可点：跳到笔记页对应时间码并播放。
- 「只看这场」开关：在笔记页默认只检索本场；在笔记库默认全部。
- 没有答案：「没找到相关内容。可以试试换个说法，或者直接搜索。」+ 搜索入口。
- 建议问题：空态给 3 条，基于最近场次的主题生成（规则拼接，不调模型）。
- 历史：最近 10 个问题，只存本机；可清空。
- 反馈：每条答案「有用 / 没用」，只存本地，用于后续调权。

### 8.2 检索设计（P1 不引入向量）

**检索单元**来自 §4.4 的 `note_units`：

| 类型 | 来源 | 权重 |
|---|---|---|
| `point` | 总结里的要点（含 evidence 原话） | 1.0 |
| `highlight` | 高光（quote/hook + 时间码） | 0.9 |
| `summary` | 整场 overview | 0.7 |
| `bookmark` | 书签备注 | 0.8 |

**流程**：

1. **时间范围解析**（`app/qa.py` 的 `parse_time_scope`）：识别「上周 / 这周 / 最近三天 / 上个月 / 11 月」等，转成 `since/until`；识别不了就用全部。
2. **查询扩展**：`glossary.expand_query`（现有），别名/拆分都参与。
3. **召回**：
   - `note_units_fts` MATCH（≥3 字）或 LIKE（<3 字）；
   - 命中 `utterances_fts` 的逐字稿作为补充证据（只取该场前 3 条，避免淹没笔记单元）；
   - 关注/主播名命中时，按房间过滤场次。
4. **排序**：BM25（FTS5 rank）+ 类型权重 + 时间新鲜度（30 天半衰）+ 关键词覆盖率；同一场最多 3 条；总分截断到 top 10。
5. **范围**：`session_id` 给定则只取该场，且优先取该场全部 point/highlight（最多 8 条），保证「这场」问题不被其他场挤掉。
6. **预算**：每条单元 ≤ 200 字；总上下文 ≤ 3000 tokens（`llm.estimate_tokens`）；超出按分数截断。
7. **回答**：调度器 `qa` 任务，提示词要求「只根据给定片段回答，每条结论带 [n]，找不到就说没找到」；输出 JSON `{answer, citations:[unit_id], confidence}`。
8. **证据门**：`citations` 必须是给定 unit_id 的子集；非法 id 剔除；剔除后为空则降级为「没找到」；answer 里引用了不存在的 [n] 也剔除。沿用 `scenes.judge` 的「证据不存在就不采信」原则。
9. **缓存**：指纹 = 问题 + 范围 + 索引版本；24 小时内同一问题直接返回；索引更新后指纹变化，自动失效。
10. **无 LLM / 失败**：返回抽取式结果「找到 N 段相关内容」+ top 5 原话 + 时间码；页面标明「未配置模型，先给你相关原话」，不显示错误码。

### 8.3 数据与接口

```sql
CREATE TABLE IF NOT EXISTS qa_answers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  question       TEXT NOT NULL,
  scope_json     TEXT NOT NULL DEFAULT '{}',
  state          TEXT NOT NULL,        -- running|ready|no_result|failed
  answer         TEXT NOT NULL DEFAULT '',
  citations_json TEXT NOT NULL DEFAULT '[]',
  engine         TEXT NOT NULL DEFAULT '',   -- llm|extractive
  feedback       TEXT NOT NULL DEFAULT '',   -- up|down|''
  job_id         TEXT NOT NULL DEFAULT '',
  error          TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
-- 只保留最近 200 条；启动/每日清理。
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/qa/ask` | `{question, session_id?, limit?}`；返回 `{id, state:"running"}` |
| GET | `/api/qa/answer?id=` | 轮询：`state`、`answer`、`citations`、`engine` |
| GET | `/api/qa/history?limit=10` | 最近问题与答案摘要 |
| POST | `/api/qa/feedback` | `{id, feedback:"up|down"}` |
| GET | `/api/qa/suggest?session_id=` | 3 条建议问题（规则生成） |

- 异步：LLM 可能 5–15 秒，不阻塞 HTTP；前端 1 秒轮询一次，页面隐藏时停。
- 单并发：同一时间只跑一个 `qa` 任务；重复提问复用运行中的任务（指纹相同）。
- 调度器：`scheduler.register("qa", ...)`，优先级 `PRIORITY_USER`，缓存 24 小时（自建指纹缓存，不用 `cache_ttl` 的默认语义）。

### 8.4 隐私与成本

- 只把检索到的片段发给模型，不发整场逐字稿、不发音频、不发文件名；单条 ≤200 字，总 ≤3000 tokens。
- 首次使用弹一次说明：「问笔记会把相关片段发给模型；不配置模型时只做本地检索。」可不再提示。
- 设置里 `qa_enabled`（默认开）；关闭后搜索仍可用，问笔记入口隐藏。
- 每日 token 预算沿用调度器配置；超预算时只走抽取式。
- 日志只记 `qa_ask` 事件和耗时/token 数，不记问题和答案正文。
- 诊断包导出不含问答内容。

### 8.5 技术落点

| 文件 | 改动 |
|---|---|
| `app/qa.py` | 新增：时间解析、召回、排序、证据门、抽取式降级、历史 |
| `app/tasks.py` | 注册 `qa` 任务；`submit_qa` |
| `app/scheduler.py` | 不改；用现有优先级/预算/重试 |
| `app/notes.py` | `note_units` 索引与 `index_session` |
| `app/server.py` | `/api/qa/*` 路由 |
| `app/store.py` | `qa_answers` 表、检索查询 |
| `app/web/js/searchpalette.js` | 问笔记 tab、答案渲染、引用点击 |
| `app/web/note.html` | 「问这场」面板 |

### 8.6 验收

- 20 篇笔记、10 个真实问题：≥8 个返回带至少 1 条有效引用的答案；0 条引用指向不存在的单元。
- 每个引用点击后落在笔记页对应时间码并开始播放。
- 「上周」「最近三天」「这场」三个范围都能正确过滤。
- 不配置 LLM：返回相关原话列表，不报错、不空屏。
- 同一问题 24 小时内重复提问不重复计费；索引更新后能拿到新答案。
- 问笔记关闭后入口消失，搜索不受影响。

---

## 9. 模块 G：低配模式 / 性能

### 9.1 三档

设置 → 性能：

| 档位 | 适用 | 行为 |
|---|---|---|
| **自动（默认）** | 不想管 | 启动时检测：≤4 核或 ≤8GB 内存 → 低配；否则流畅 |
| **流畅** | 8 核以上 | 现有默认参数 |
| **低配** | 4 核 / 8GB 或更旧 | 降并发、关部分实时能力、降轮询频率、减动效 |

用户手动选择优先于自动；环境变量优先于设置（见 §9.3）。

### 9.2 参数表

| 参数 | 流畅 | 低配 | 生效时机 |
|---|---|---|---|
| ASR worker 数 | `min(8, 核数-4)`，下限 2 | 2 | 下一场记录 |
| ASR 线程数 | `min(4, 核数//2)` | 2 | 下一场记录 |
| 部分结果（实时字幕） | 开（现有默认 TTL 2.5s） | 关（只出定稿） | 下一场记录 |
| 高光 LLM 评审 | 开 | 关（只用规则） | 下一场记录 |
| 实时高光卡 | 开 | 关（保留书签） | 立即 |
| 关注检测间隔 | 120s | 180s | 立即 |
| 通知轮询 | 4s | 6s | 立即 |
| 首页/状态轮询 | 3s | 5s | 立即 |
| 笔记库每页 | 24 | 16 | 立即 |
| 列表动效/渐变 | 开 | 关 | 立即 |
| 逐字稿渲染窗口 | 200 句 | 80 句 | 立即 |
| 周报生成 | 到点即生成 | 空闲/下次打开时生成 | 立即 |
| RAG top_k / 上下文 | 10 / 3000 tokens | 6 / 1800 tokens | 立即 |
| 分享卡阴影/渐变 | 开 | 关 | 立即 |

**不降的**：音频采集、ASR 模型精度、定稿完整率、笔记生成、导出。低配不是「功能阉割」，是「少做实时花活，保住录音和笔记」。

### 9.3 技术落点

- `app/settings.py`：`perf_mode`（`auto|full|lite`，默认 `auto`），带枚举校验。
- `app/config.py`：新增 `apply_perf_mode(mode)` 与 `perf()` 查询；现有 `asr_workers()` / 线程数改为「显式环境变量 > 已应用模式 > 自动公式」。
  - 环境变量（如 `LIVENOTES_ASR_WORKERS`、`LIVENOTES_CAPTURE_MODE`、`LIVENOTES_MAX_ROOMS`）始终优先，方便诊断和压测。
  - 为避免 `config` ↔ `settings` 循环导入：`run.py` 启动时读 `settings.prefs()` 后调用 `config.apply_perf_mode()`；设置页保存后由 `server.py`（同时导入两者）再调用一次。
- `run.py`：启动时应用模式；检测核数/内存写入日志与 `/api/perf`。
- `app/server.py`：`GET /api/perf` 返回 `{mode, effective:{...}, detected:{cores, ram_mb}, advice}`；设置页「性能自检」调用它。
- 前端：轮询间隔从 `/api/perf` 取；不在页面里写死 4 秒。
- 生效策略：引擎级参数（ASR worker/线程、部分结果、高光评审）只对下一场生效，设置页明确写「下一场记录生效」；UI/轮询立即生效，避免中断当前录音。

### 9.4 性能预算

| 场景 | 目标 |
|---|---|
| 1 路记录 P95 定稿延迟 | ≤ 1.2 秒（流畅）/ ≤ 1.5 秒（低配） |
| 10 路记录 CPU | ≤ 5 核（流畅，12 核机器参考 README 4.2 核） |
| 10 路记录 RSS | ≤ 1.3GB |
| 2 路低配 CPU | ≤ 2 核（4 核机器 ≤ 50%） |
| 笔记库 1000 场首屏 | P95 ≤ 400ms；低配 ≤ 800ms |
| 搜索 | P95 ≤ 200ms |
| 笔记页打开 | P95 ≤ 300ms |
| 周报生成 | ≤ 30s（20 场） |
| 分享卡生成 | ≤ 500ms |
| 前端长任务 | 无 > 100ms 的同步阻塞（低配 > 150ms 告警） |

测量方式：沿用现有 `tools/stress_live.py`、`tools/selftest_asr_pool.py` 和指标页的 session_metrics；新增 `tools/bench_perf.py` 跑「1/2/10 路 + 1000 场库 + 搜索 + 笔记页」的本地基准，输出 JSON，CI 只跑轻量阈值（不跑 10 路长跑）。

### 9.5 现有性能债（P1 顺手修）

1. `/api/library` 返回全部场次且逐行 `stat()` 音频文件 → 卡片接口分页且不算大小。
2. 笔记库/工作台首屏加载全部逐字稿 → 按窗口加载。
3. 通知轮询固定 4 秒 → 按模式调整。
4. 搜索两字词走 LIKE 全表 → 加 `session_id`/时间范围可选过滤；1000 场以内可接受。
5. `note_units` 索引重建在后台分块，不阻塞启动。

### 9.6 验收

- 4 核 8G 虚拟机，低配，2 路 2 小时：无丢块、CPU ≤ 50%、RSS ≤ 1.2GB、界面可操作。
- 切换档位不中断当前录音；引擎级参数在下一场生效，页面有说明。
- 设置页显示「当前：4 核 / 8GB → 建议低配」和实际生效参数。
- 1000 场库首屏与搜索达到 §9.4 目标；低配模式下降级符合参数表。

---

## 10. 模块 H：文案、视觉、可访问性

### 10.1 文案：一张术语表 + 一张错误表

**术语表**（新增/统一，写入 `host.json` 的 `profile.copy` 与页面文案）：

| 不说 | 说 |
|---|---|
| 采集 / 抓流 / 拉流 | 记录 |
| 断流 / stall | 听不到声音 |
| 场次 / session | 这场直播 |
| 聚类 / 说话人 02 | 说话人 2 |
| ASR / 识别 / 转写 | 逐字稿 |
| token / 队列 / 阶段码 | 不出现 |
| RAG / 向量 / 检索 | 问笔记 |
| 关注对象 / room_key | 直播间 / 主播 |
| 生成失败 / error | 笔记没生成成功（说明什么还在） |

**错误表**：新增 `app/copy.py`，服务端所有面向用户的错误都过一层：

```python
def human(kind: str, detail: str = "") -> str:
    """把内部错误映射成一句人话 + 下一步。不返回堆栈、URL、Key。"""
```

覆盖：LLM 失败、链接失效、平台不支持、容量满、磁盘不足、音频缺失、导出失败、分享卡失败。前端只展示 `human()` 的结果，不再各自拼错误。

**空态与确认**：每个新页面至少有一句空态文案 + 一个下一步；自动记录、删除、清空、关闭手机访问有确认文案。禁用词在 CI 里检查（`tools/check_web_layers.py` 扩展一份禁用词表：`采集 / 断流 / 聚类 / token / ASR / 场次` 出现在用户可见文本即失败，白名单为设置页/诊断页）。

### 10.2 视觉

- 沿用现有主题（纸面 / 工作室）与设计令牌；不引入新框架、不引入 Web Font。
- 新组件：
  - **卡片**：圆角 12，内边距 16，标题 17/24，正文 14/22，元信息 12/18；hover 只加 1px 边框和 2% 阴影，不做位移动画。
  - **状态标签**：直播中（红点 + 文字）、生成中（进度条 + 文字）、失败（警示色 + 文字），不只靠颜色。
  - **搜索面板**：宽 640，最大高 70vh；分组标题 12px；选中行用 2px 左侧强调条。
  - **空态**：一句价值 + 一个主按钮；插图用 CSS 几何，不加载图片。
- 一个页面只有一个实心强调按钮（沿用 34 §2.1）；新页面评审用截图检查。
- 900×640 最小窗口、125%/150%/200% DPI、深浅色都要过；移动端只读笔记页 360px 起。

### 10.3 可访问性

P1 的硬指标：

1. **键盘**：所有新页面可只用键盘完成主任务；`Ctrl+K` 搜索、`Esc` 关闭、`↑↓` 选择、`Enter` 打开、`Tab` 顺序合理；焦点环 2px 可见。
2. **读屏**：卡片有 `aria-label`（标题 + 主播 + 时长 + 状态）；通知/toast 用 `aria-live="polite"`；生成中进度用 `role="progressbar"` + `aria-valuenow`。
3. **对比度**：正文 ≥ 4.5:1，大字 ≥ 3:1；状态色不单独承载信息。
4. **点击目标**：≥ 32×32px；HUD 按钮 ≥ 40px。
5. **动效**：尊重 `prefers-reduced-motion`；低配关闭非必要动画。
6. **表单**：输入框有 label，不只是 placeholder；错误用文字 + `aria-describedby`。
7. **图片**：分享卡预览有 alt；二维码有文字说明。
8. **缩放**：200% 文字缩放不截断关键操作。

落地：

- 新增 `tools/a11y_check.py`：静态检查新页面的 `img alt`、`aria-label`、`tabindex`、`role`、禁用 `outline:none` 无替代、对比度令牌；接入 CI，只报新增问题。
- 手动清单：5 个核心任务键盘走查 + NVDA 读一遍笔记页/笔记库/关注页；记录在 PR 描述里。
- 可访问性不是最后一个阶段：每个模块的 PR 必须带键盘走查；H 在 R1/R2 各做一遍，不集中到最后。

### 10.4 验收

- 键盘完成：搜索 → 打开笔记 → 加书签 → 导出 → 关闭弹层。
- NVDA 能读出笔记标题、要点、时间码、按钮用途；通知有播报。
- 深浅色、125%/150% DPI、900×640 无截断。
- 用户可见文本禁用词 0 处（诊断/设置白名单除外）。
- 所有错误提示都是人话 + 下一步，不出现堆栈、URL、Key、阶段码。

---

## 11. 模块 I：导出集成

### 11.1 `note_md`：一篇笔记的 Markdown

33 §4.4.6 已把 `note_md` 定为默认导出格式，P1 落地：

```markdown
# 中小主播的选品与复购

> 阿哲 · 抖音 · 2026-11-14 21:03 · 1 小时 42 分 · LiveNotes

## 一句话

低价不是核心，复购取决于选品和信任。

## 要点

### 1. 先看复购，不看单场 GMV

选品的第一指标是复购率，单场爆发不代表品选对了。

> [00:12:30] 阿哲：「选品要看复购，不是看单场 GMV。」
> [00:13:10] 阿哲：「复购高的品，起量慢一点也值得。」

### 2. 用少量 SKU 测人群

...

## 值得回听

1. [00:12:30–00:13:10]「选品要看复购……」— 阿哲
2. ...

## 书签

- [00:05:12] 这里讲了退换货（记录中）
- [00:48:20] 这段可以做成短视频（读稿时）

## 说话人

| 说话人 | 发言时长 | 句数 |
|---|---|---|
| 阿哲 | 58:12 | 412 |
| 连麦嘉宾 | 21:30 | 156 |
```

规则：

- 默认**不包含完整逐字稿**；用户在导出对话框勾选「附带逐字稿」才追加，避免文件过大。
- 每个要点必须带至少 1 条 evidence；没有 evidence 的要点不导出（沿用 33 的证据门）。
- 时间码统一 `[HH:MM:SS]`；超过 1 小时也补零。
- 文件名：`YYYYMMDD-<标题>-笔记.md`，非法字符替换，最长 80 字；重名加 `-2`。
- 导出实现放 `app/export.py` 的 `to_note_markdown(conn, sid)`，服务端只此一份；笔记页、卡片「更多」、批量导出都调它。

### 11.2 批量与合集

现有 `/api/export/batch` 支持 `md/srt/txt` 的 zip。P1 扩展：

| 格式 | 产物 | 说明 |
|---|---|---|
| `note_md` | 每场一个 `.md` 打 zip | 从笔记库多选 |
| `notes_bundle` | 一个 `笔记合集_<since>_<until>.md` | 带目录，按时间排序，适合归档/打印 |
| `share_package` | 现有分享包 + `note.md` + `bookmarks.json` | 单场；不打包分享卡 PNG |

- 批量导出 20 场目标 < 10 秒；流式写 zip，不一次性把全部内容放内存。
- zip 内文件名去重、去非法字符；空笔记的场次跳过并在 `README.txt` 说明。
- 导出任务在请求内完成（本地、无网络）；超过 50 场提示分批。

### 11.3 分享包 v2

现有 `app/share.py` 的包：`summary.md`、`transcript.srt`、`audio.m4a`、`README.txt`、`meta.json`。P1 增加：

- `note.md`（同 `note_md`，不含逐字稿）；
- `bookmarks.json`（时间码 + 备注）；
- `README.txt` 更新：说明「note.md 是笔记，transcript.srt 是逐字稿，audio 是原声」；保留合规提示「仅用于个人备份，请勿公开传播他人直播内容」。
- 音频仍只在有音频时打包；没有音频时 README 说明。

### 11.4 飞书（可选，不阻塞验收）

- 复用 `app/report.py` 已有的 docx 推送能力，给笔记页加「推送到飞书」。
- 只在已配置飞书机器人时显示；失败给「复制 Markdown」兜底。
- P1 不要求；排期富余再做，工作量 +1–2 人日。

### 11.5 验收

- 一篇 20 分钟、3 个要点的笔记，`note_md` 在 Typora/Obsidian 打开结构正确，时间码可读。
- 没有 evidence 的要点不出现在导出里；没有书签时「书签」区省略。
- 批量导出 20 场 < 10 秒；zip 内文件名正确、无重复。
- 分享包解压后 `note.md`、`transcript.srt`、`audio.m4a`、`README.txt` 齐全；没有音频时说明清楚。
- 导出不触发 LLM、不修改数据库。

---

## 12. 数据、接口、页面总表

### 12.1 表增量

| 表 | 状态 | 说明 |
|---|---|---|
| `follows` | 新增 | 34 §5.6 + P1 三列（`live_episode`、`last_session_id`、`fail_count`） |
| `notes` | 新增 | 33 §4.5，session_id 主键、state、stages_json、progress、error、updated_at |
| `note_units` + `note_units_fts` | 新增 | 全库搜索与 RAG 共用的结构化索引 |
| `qa_answers` | 新增 | 问答历史与反馈；保留 200 条 |
| `reports` | 新增 | 周报/复盘的状态、路径、结构化数据；唯一键 `(kind, period)` |
| `index_meta` | 新增 | 索引版本，控制重建 |
| `marks` | 改 | `ALTER TABLE marks ADD COLUMN kind TEXT NOT NULL DEFAULT 'mark'` |
| `highlights` | 改 | `ALTER TABLE highlights ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'`；`kind` 语义不变 |
| `notifications` | 不改 | 加 4 个 kind，无 schema 变更 |
| `settings` | 不改 | `DEFAULT_PREFS` 增键 |

迁移策略：

- 全部走 `store._SCHEMA`（`CREATE TABLE IF NOT EXISTS`）+ `_migrate` 的 `ALTER` 模式；启动时幂等执行。
- FTS 表在 `init_db` 时创建；若 `index_meta` 版本不符或表为空，后台重建。
- `notes` 不强制回填：卡片和 `/api/note` 对没有 `notes` 行的场次按「有总结→ready，无总结→empty/insufficient」推导；首次打开笔记时写入。
- `reports` 启动时扫描旧 `weekly_*.md` 补行，`data_json` 为空时页面降级渲染 Markdown。
- 删除场次时同步删 `note_units`、`qa_answers` 中相关引用（或保留但标记，P1 直接删，简单）。

### 12.2 API 总表

| 方法 | 路径 | 模块 | 状态 |
|---|---|---|---|
| GET | `/api/note?session_id=` | G1 | 新增 |
| POST | `/api/note/rebuild` | G1 | 新增（最小版可先不做） |
| GET | `/api/library/cards` | B | 新增 |
| GET | `/api/search` | B | 扩展 `types`，默认兼容 |
| GET | `/api/follows` | G2 | 新增 |
| POST | `/api/follows` | G2 | 新增 |
| POST | `/api/follows/update` | G2 | 新增 |
| POST | `/api/follows/remove` | G2 | 新增 |
| POST | `/api/follows/check` | G2 | 新增 |
| POST | `/api/watch/precheck` | G2 | 新增 |
| POST | `/api/live/mark` | C | 扩展 kind/t，新增 DELETE |
| DELETE | `/api/live/mark?id=` | C | 新增 |
| GET | `/api/live/highlights` | C | 新增 |
| POST | `/api/highlights/save` | C | 新增 |
| GET | `/api/share/info` | D | 扩展 `lan_enabled/note_url` |
| GET | `/api/reports` | E | 新增 |
| GET | `/api/reports/get` | E | 新增 |
| POST | `/api/reports/build` | E | 新增 |
| POST | `/api/reports/push` | E | 可选 |
| POST | `/api/qa/ask` | F | 新增 |
| GET | `/api/qa/answer` | F | 新增 |
| GET | `/api/qa/history` | F | 新增 |
| GET | `/api/qa/suggest` | F | 新增 |
| POST | `/api/qa/feedback` | F | 新增 |
| GET | `/api/perf` | G | 新增 |
| GET | `/api/export?format=note_md` | I | 扩展 |
| POST | `/api/export/batch` | I | 扩展格式 |
| GET | `/api/share/package` | I | 内容扩展 |
| POST | `/api/metric/event` | §13 | 新增，白名单 |
| GET | `/api/product/metrics` | §13 | 新增，诊断页用 |

所有新接口：

- 返回 JSON，错误也返回 JSON + 人话 `error`；
- 写操作返回最新状态，前端不本地拼；
- 失败不用 5xx 表达业务失败（如关注检测失败返回 200 + `last_error`）。

### 12.3 通知类型增量

| kind | 分组 | 标题 | 去重键 |
|---|---|---|---|
| `follow_live` | 关注 | 你关注的主播开播了 | `follow_live:<follow_id>:<episode>` |
| `auto_record` | 关注 | 已自动开始记录 | `auto_record:<session_id>` |
| `auto_record_failed` | 关注 | 自动记录没开始 | `auto_record_failed:<follow_id>:<episode>` |
| `weekly_ready` | 周报 | 上周的周报出来了 | `weekly:<period>` |

- `notify.KINDS` 从 9 类扩到 13 类；设置页自动多出四个开关。
- 分组映射写在 `notify.py`（`GROUP` 字典），壳按分组渲染，不在前端硬编码。
- `notify_detail`（默认 true）：关闭后通知不显示直播标题/主播名，只显示「你关注的主播」「一场直播」。

### 12.4 设置项增量

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `perf_mode` | enum | `auto` | `auto|full|lite` |
| `follow_interval_s` | int | 120 | 60–300 |
| `weekly_enabled` | bool | true | 是否生成周报 |
| `qa_enabled` | bool | true | 是否显示问笔记 |
| `notify_detail` | bool | true | 通知是否显示标题/主播名 |

`settings.DEFAULT_PREFS` 增加上述键与校验；`notify_muted` 继续承载单类开关。

### 12.5 页面与导航

| 页面 | 路径 | 类型 | 说明 |
|---|---|---|---|
| 笔记页 | `/note?session=` | 基座 | G1；桌面壳新增 `note` 视图；支持 `?t=` 定位、`?share=1` 只读 |
| 笔记库 | `/library` | 基座 | 改造为卡片流；保留文件/回收站/清理 tab |
| 关注 | `/follows` | 基座 | G2；桌面壳新增 `follows` 视图 |
| 周报 | `/weekly` | 基座 | E；桌面壳新增 `weekly` 视图 |
| 搜索面板 | 全局组件 | 基座 | `Ctrl+K`；所有页面共用 |
| 首页 | `/home` | 基座 | 34 的范围，P1 不强制；未实施时用 `/library` 顶部摘要 |

导航调整最小化：

- 现有 `files` 视图改名为「笔记库」，仍指向 `/library`；新增 `follows`、`weekly`；`note` 作为 `workbench` 的升级落点（有笔记进 `/note`，没笔记进工作台）。
- 不删除 `listen` / `workbench` / `people` / `capture`；它们仍是「更多」里的专业视图。
- 桌面壳 `VIEW_META` 增加 `note/follows/weekly`；`VIEW_THEME` 都归 `paper`。

### 12.6 文件清单

**新增**

- `app/follows.py`、`app/liveprobe.py`、`app/notes.py`、`app/qa.py`、`app/copy.py`
- `app/web/note.html`、`app/web/css/note.css`、`app/web/js/note/*`
- `app/web/follows.html`、`app/web/css/follows.css`、`app/web/js/follows/*`
- `app/web/weekly.html`、`app/web/css/weekly.css`、`app/web/js/weekly/*`
- `app/web/js/library/*`（重写）、`app/web/js/searchpalette.js`、`app/web/js/sharecard.js`
- `app/web/js/vendor/qrcodegen.js`
- `desktop/web/css/p1-overlay.css`、`desktop/web/js/p1-overlay.js`（桌面专属观感）
- `tests/test_follows.py`、`test_auto_record.py`、`test_notes_api.py`、`test_library_cards.py`、`test_bookmarks.py`、`test_qa.py`、`test_reports_weekly.py`、`test_export_note_md.py`、`test_perf_mode.py`
- `tools/a11y_check.py`、`tools/bench_perf.py`

**修改**

- `app/store.py`（表/迁移/检索/分页）
- `app/server.py`（路由/页面/`/api/perf`/埋点）
- `app/notify.py`（4 个 kind/分组/事件）
- `app/settings.py`（5 个 pref）
- `app/config.py`（性能模式）
- `app/live.py`（实时高光/书签）
- `app/highlight.py` / `app/scenes.py`（保留 `source='live'` 的高光）
- `app/export.py`（`note_md`/合集）
- `app/share.py`（note.md/bookmarks.json）
- `app/report.py`（reports 表/结构化数据/调度辅助）
- `app/tasks.py`（`qa`/`weekly` 任务注册）
- `run.py`（关注 worker/周报 tick/索引重建）
- `desktop/dist/shell.js` / `shell.html`（视图/通知路由/轮询）
- `tools/ui_smoke.py`、`tools/check_web_layers.py`、`tools/direct_capture.py`
- `README.md`（新功能/设置/性能）
- `docs/29` 的 `host.json` 若新增 copy key 则同步

---

## 13. 埋点与指标落地

### 13.1 事件

复用 `store.record_metric_event()` 和现有 `metric_events` 表；新增 `POST /api/metric/event`，只接受白名单：

| 事件 | 触发 |
|---|---|
| `app_open` | 壳启动 / 网页版首次加载，每天一次 |
| `record_start` / `record_stop` | 开始/结束记录 |
| `note_ready` / `note_open` | 笔记生成完成 / 打开笔记 |
| `library_open` / `card_click` | 打开笔记库 / 点卡片 |
| `search_submit` / `search_click` | 提交搜索 / 点结果 |
| `bookmark_add` / `highlight_save` | 书签 / 收藏高光 |
| `follow_add` / `follow_notify_click` | 添加关注 / 点开播通知 |
| `auto_record_start` / `auto_record_fail` | 自动记录成功/失败 |
| `weekly_open` / `weekly_notify_click` | 打开周报 / 点周报通知 |
| `qa_ask` / `qa_cite_click` / `qa_feedback` | 问笔记 / 点引用 / 反馈 |
| `share_card_export` / `export` | 生成分享卡 / 导出 |
| `perf_mode_set` | 切换性能档位 |

- 事件只带 `session_id`、`follow_id`、`kind`、耗时、数量等结构化字段；不带搜索词、问题、答案、逐字稿。
- 前端事件经 `POST /api/metric/event`；服务端事件直接调 `record_metric_event`。

### 13.2 指标页

`/metrics` 增加「产品」区块：

- 近 28 天：打开天数、记录场次、笔记数、D1/D7 回访（按首次记录日分群）；
- 回访触发分布：关注开播 / 未读笔记 / 周报 / 主动打开；
- 卡片点击率、搜索成功率、问笔记有效率、分享卡生成率；
- 性能：当前档位、平均定稿延迟、长任务次数。

只在本机展示；「导出产品数据」只导出聚合计数（无内容），供种子用户自愿提供。

### 13.3 隐私

- 不上传；不建账号；不写远程日志。
- 诊断包默认不含搜索词、问题、答案、通知正文；只含事件名、时间、计数。
- 分享卡的二维码指向局域网地址，不经过任何第三方服务。

---

## 14. 测试与发布

### 14.1 单元测试（重点）

| 文件 | 覆盖 |
|---|---|
| `tests/test_follows.py` | 状态机（live/offline/unknown）、单次失败不改状态、连续 3 次失败、episode 去重、退避、50 上限 |
| `tests/test_auto_record.py` | 触发条件、容量满、同房间不重复、单路串行、失败通知、90 秒重试只一次 |
| `tests/test_notes_api.py` | `/api/note` 字段冻结、状态推导（ready/partial/running/failed/empty/insufficient）、无总结降级 |
| `tests/test_library_cards.py` | 游标分页、筛选、排序、状态映射、1000 场不重不漏 |
| `tests/test_bookmarks.py` | `marks.kind` / `highlights.source` 迁移、撤销、收尾重算保留 `source in (manual,live)`、导出包含书签 |
| `tests/test_qa.py` | 时间范围解析、两字/三字召回、排序、同场限流、引用子集校验、无 LLM 抽取式、24h 缓存 |
| `tests/test_reports_weekly.py` | ISO 周边界、空周、幂等、旧文件补行、无 LLM 规则版 |
| `tests/test_export_note_md.py` | golden 文件、无 evidence 的要点剔除、文件名清洗、批量 zip |
| `tests/test_perf_mode.py` | 自动判定、环境变量优先、引擎参数下一场生效、`/api/perf` |
| `tests/test_search_units.py` | `note_units` upsert/删除、FTS trigram、两字 LIKE、索引重建 |

沿用 `tests/conftest.py` 的临时 DATA_DIR 隔离；不触网、不调真实模型（fake LLM）。

### 14.2 轻量集成

- 起 `make_server` 临时实例，走真实 HTTP：关注增删查、`/api/note`、`/api/library/cards`、`/api/qa/ask`（fake LLM）、`/api/reports/build`、`/api/export?format=note_md`。
- 通知链路：`emit` → `feed` → `mark_read`；4 个新 kind 的去重键。
- 迁移：旧库（无新表）启动后自动建表、索引、旧周报补行；不丢数据。

### 14.3 UI 与契约

- `tools/ui_smoke.py` 的 `PAGES` 增加 `/note?session=demo`、`/follows`、`/weekly`；`/library` 已存在。
- `tools/check_web_layers.py`：新页面必须有 `data-ln-region="topbar"` / `"stage"`，基座不得出现 `embed=1` / `__LN_DESKTOP__`；`host.json` 新 copy key 必须被使用；新增禁用词检查。
- `tools/a11y_check.py`：静态规则 + 5 个核心任务的键盘走查清单。
- 桌面壳：`note/follows/weekly` 视图切换、通知点击路由、`Ctrl+K`。

### 14.4 性能与稳定性

- `tools/bench_perf.py`：1/2/10 路 ASR、1000 场卡片流、搜索、笔记页、分享卡；输出 P50/P95/CPU/RSS。
- `tools/stress_live.py` 增加低配参数组合。
- 长跑：2 路 2 小时、10 路 30 分钟；检查丢块、内存增长、SQLite 锁。
- CI：pytest + `check_web_layers` + `a11y_check`（新增问题为 0）+ 构建；性能基准 nightly，不阻塞每个 PR。

### 14.5 发布与回滚

- R1、R2 各自一个分支/PR，按模块拆分提交；每个模块可独立回滚。
- 数据迁移只增表/加列，不做破坏性变更；回滚旧版本时新表保留、旧代码忽略。
- 狗粮 2 周：3 位种子用户，每周一次访谈 + 指标复盘；R1 后先验证「关注是否带来 D1 回访」，再决定 R2 的 RAG 投入。
- 发布门槛：§0.5 的九条验收 + 单测全绿 + 无 P0/P1 缺陷 + 低配 2 路 2 小时通过。

---

## 15. 排期与任务拆解

### 15.1 R1（5–6 周，2 人）

| # | 任务 | 人日 | 依赖 |
|---|---|---|---|
| 1 | `notes` 表、状态机、`/api/note`、`app/notes.py` | 4–5 | —— |
| 2 | `/note` 基座页面 + 桌面壳 `note` 视图 | 5–7 | 1 |
| 3 | `follows` 表、CRUD、`/api/follows*` | 3–4 | —— |
| 4 | `liveprobe` 抽取 + 检测 worker + 状态机 | 4–5 | 3 |
| 5 | `/follows` 页面 + 桌面壳视图 | 2–3 | 3 |
| 6 | 通知 4 类 kind + 分组 + 设置 | 2–3 | 5 |
| 7 | 开播提醒 + 自动记录 + 失败重试 | 4–5 | 4、6 |
| 8 | `note_units` 索引 + `/api/library/cards` | 4–5 | 1 |
| 9 | 笔记库卡片流前端 + 分页/筛选 | 4–5 | 8 |
| 10 | 全局搜索面板 + `/api/search` 扩展 | 3–4 | 8 |
| 11 | `note_md` 导出 + 分享包加 note.md | 3–4 | 1 |
| 12 | H 第一遍：术语/错误表/键盘 | 2–3 | 2、5、9 |
| 13 | 联调、单测、狗粮准备 | 4–5 | 全部 |
| | **R1 合计** | **44–58** | |

> 2 人并行时按「引擎（1、3、4、6、7、8、11）/ 前端（2、5、9、10、12）」分工，联调共同做。实际日历 5–6 周。

### 15.2 R2（5–6 周，2 人）

| # | 任务 | 人日 | 依赖 |
|---|---|---|---|
| 14 | 实时高光暴露 + HUD 卡 + 书签统一 | 4–5 | R1 |
| 15 | 笔记页/阅读器书签区 + 重算保留 | 3–4 | 14 |
| 16 | 分享卡 Canvas + 3 模板 + 预览/下载 | 4–5 | R1 |
| 17 | 纯 JS 二维码 + LAN 只读笔记页 | 2–3 | 16 |
| 18 | `reports` 表 + 周报结构化数据 + 调度 | 3–4 | R1 |
| 19 | `/weekly` 页面 + 导出/分享卡入口 | 3–4 | 18 |
| 20 | 周报通知 + 空态/补生成 | 1–2 | 18 |
| 21 | RAG 检索（时间解析/召回/排序/索引） | 5–7 | R1 的 8 |
| 22 | RAG 生成/证据门/无模型降级/历史 | 5–7 | 21 |
| 23 | 低配模式三档 + 自动判定 + `/api/perf` | 3–4 | —— |
| 24 | 性能债修复 + 基准脚本 | 2–3 | 23 |
| 25 | H 第二遍：读屏/对比度/缩放/禁用词 | 2–3 | 16、19、22 |
| 26 | 导出合集/批量扩展 + 可选飞书 | 1–2 | 11 |
| 27 | 联调、全量验收、性能长跑 | 4–5 | 全部 |
| | **R2 合计** | **42–58** | |

### 15.3 日历视图

```
周 1–2   G1 笔记页 + G2 关注底座（并行）
周 3     G3 通知 + A 提醒/自动记录 + B 索引/卡片后端
周 4–5   B 卡片流/搜索 + I note_md + H 第一遍
周 6     R1 联调 + 狗粮/修复（缓冲）
周 7     C 书签/实时高光
周 8     D 分享卡 + E 周报开始
周 9     E 周报 + F RAG 检索
周 10    F RAG 生成 + G 低配
周 11    H 第二遍 + I 收尾 + R2 联调
周 12    全量验收/性能长跑/发布
周 13    风险缓冲（按实际进度启用）
```

### 15.4 1 人路径

按 R1（9–12 周）→ R2（9–12 周）顺序做；先把 G1/G2/A/B 做成可用闭环，再逐项加 C/D/E/F/G。**不要在 G1 之前做 RAG 或分享卡**——它们都依赖笔记页的取数。

---

## 16. 风险与对策

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 平台反爬导致关注检测不稳定 | 高 | 开播提醒失效 | 保守状态机、单次失败不改状态、3 次失败显示「检测失败」、保留手动打开；不登录、不带 Cookie；抖音优先，其他平台标注「尽力检测」 |
| 自动记录误触发/占满容量 | 中 | 多录无关内容、磁盘压力 | 默认关、首次确认、单路串行、< 5 路上限、同房间去重、失败必告知、不自动停 |
| 二维码是死链 | 中 | 分享体验崩 | 只在 LAN 开启时生成；IP 变化重新生成；关闭时不出码；卡片本身不依赖二维码 |
| RAG 编造引用/成本失控 | 中 | 信任与费用 | 引用子集校验、无证据降级、单条/总预算、单并发、24h 缓存、每日 token 预算 |
| 笔记页/卡片流把 P0 的工期吞掉 | 高 | R1 延期 | 阶段 0 只做最小集；卡片流在 R1 先只读；砍单顺序明确；2 周切片先验证关注价值 |
| 低配模式下 ASR 质量下降 | 低 | 笔记变差 | 低配只降并发/实时能力，不降模型精度；worker 最少 2；验收用真实 2 路 2 小时 |
| 1000 场库变慢 | 中 | 回访体验差 | 游标分页、卡片接口不算音频大小、索引后台重建、性能基准门槛 |
| 新旧数据迁移丢内容 | 低 | 数据事故 | 只增表/加列；启动幂等；旧周报补行；迁移测试用旧库样本 |
| 分享卡字体/排版在旧机器溢出 | 中 | 图难看 | Canvas 固定字号 + 截断规则 + 2x 渲染；三种边界截图验收 |
| 通知过多导致用户关闭 | 中 | 回访链路失效 | 一次开播只发一条（自动记录合并）、周报一周一条、沿用 35 的 5 条/小时冷却 |

---

## 17. 需要拍板的决策（附建议）

| # | 决策 | 建议 | 理由 |
|---|---|---|---|
| D1 | P0 的三块地基是否并入 P1 排期 | **并入，作为阶段 0** | 否则卡片/提醒/周报/RAG 都没有落点；阶段 0 只做最小集，不搬 33/34 全量 |
| D2 | 书签是否新建表 | **不建，复用 `marks` + 手动高光** | 一套时间码数据，导出/跳转/撤销都简单；加一列 `kind` 足够 |
| D3 | 分享卡在哪渲染 | **前端 Canvas + 纯 JS 二维码** | 不新增 Python 依赖；无服务端临时文件；WebView 能力足够 |
| D4 | RAG 是否引入向量检索 | **P1 不引，FTS5 + `note_units` + 词表扩展** | 当前数据量下足够；少一个依赖、少一条索引链路；P2 用评测再决定 |
| D5 | 开播提醒与自动记录是否合并为一条通知 | **合并**：自动成功只发 `auto_record`，失败只发 `auto_record_failed`，未开启才发 `follow_live` | 一次开播弹两条会让人关通知；35 的矩阵可按此修订 |
| D6 | 周报是否发通知 | **发，周一一条，默认开，空周不发** | 周报是「第二次打开」的外部触发器；没有通知就只能靠用户自己想起来 |
| D7 | 低配切换是否立即生效 | **UI/轮询立即，ASR/高光评审下一场生效** | 不中断当前录音；设置页明确说明 |
| D8 | `note_md` 是否默认带逐字稿 | **不带，导出对话框可勾选** | 笔记是产物，逐字稿是证据；默认文件小、可读 |
| D9 | 自动记录默认开还是关 | **默认关，首次开启确认** | 自动录错内容的代价高于少录一场；符合 34 §5.4 |
| D10 | P1 是否重做首页 | **不重做**；关注落 `/follows`，最近笔记落 `/library` 顶部 | 首页是 34 的独立工程；P1 不被 27 人日绑架 |

---

## 18. 一句话总结

P1 不是再加九个功能，而是把「记录」变成「会回来的习惯」：**关注给一个回来的理由，笔记库给一个回来的地方，搜索和问笔记给一个回来的方法，分享卡和周报给一个回来的面子，低配和可访问性保证这一切在旧电脑上也成立。** 先补笔记页和关注底座两块地基，再用两个 release 把闭环跑通；排期不够时按 §0.4 的顺序砍，别砍地基。
