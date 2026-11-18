# 40 · 分发与更新：发版流水线、三条更新通道与签名路线

> 状态：方案待评审，未实现
> 日期：2026-11-18
> 关联：[docs/20](20-tauri-2-adr.md) §4.10 更新、§5 打包；[docs/18](18-desktop-packaging-options.md)；
> [docs/21](21-desktop-diagnostics.md) 诊断包；[docs/22](22-desktop-usage-guide.md) 安装教程；
> [docs/27](27-process-lifecycle.md) 进程生命周期；[docs/35](35-hud-tray-notification-design.md) 托盘与通知；
> [docs/38](38-p2-growth-and-monetization-options.md) 商业化
> 一句话：**「每次更新要不要发一次 EXE」不是分发问题，是「一次构建 → 三条通道 → 客户端自取」的工程问题。**

---

## 0. 结论先行

1. **不要人工发 EXE。** 每次发版仍然会构建安装包，但「发」的动作变成跑一条发布命令：
   产物上传到更新源，客户端自己检查、下载、安装。
2. **壳更新用 Tauri 2 自带的 `tauri-plugin-updater`**：检查、NSIS 静默安装、minisign 签名校验
   都已经现成，自研壳更新器只会多一类事故。
3. **先接受一次全量 278MiB，再拆包。** Tauri updater 没有差分更新；真正的解法不是差分，
   是把 257MB 的模型从安装包里拿出来、让引擎能独立升级。常规更新应该落在「壳 + 引擎」量级，
   模型半年不动一次。
4. **签名是首装的入场券，不是锦上添花。** 从浏览器下载的安装包会过 SmartScreen；
   应用内更新下载不经过浏览器，通常不带 Mark-of-the-Web，所以签名优先服务「第一次安装」。
5. **回滚不能靠 `latest.json` 指回旧版本。** updater 只按 semver 往上升；回滚 = 发一个
   版本号更高的修复版，或服务端功能开关关掉出问题的能力。
6. **分三步落地**：阶段 1 壳自动更新（1–2 人日）→ 阶段 2 拆包（3–5 人日）→
   阶段 3 签名 / 灰度 / 指标（约 3 人日）。阶段 2 之前，先把**模型目录放到用户数据目录**，
   别钉死在安装包里。

---

## 1. 现状（代码事实）

| 项 | 现状 | 出处 |
|---|---|---|
| 壳 | Tauri 2.12.5；依赖里**没有** `tauri-plugin-updater` | [Cargo.toml](../desktop/src-tauri/Cargo.toml) |
| 安装器 | NSIS，`installMode=currentUser`，`resources/**/*` 随包 | [tauri.conf.json](../desktop/src-tauri/tauri.conf.json) |
| 引擎 | PyInstaller onedir；打包态从 `<exe_dir>/resources/engine/engine.exe` 启动，也接受 `<exe_dir>/engine` | [engine.rs:408-460](../desktop/src-tauri/src/engine.rs) |
| 引擎健康 | 壳轮询 `/api/health`，只含 `pid` / `port` / `started_at`，**没有版本号** | [engine.rs:205](../desktop/src-tauri/src/engine.rs)、[server.py:1063](../app/server.py)、[runtime.py:196](../app/runtime.py) |
| 版本号 | 三处各写一份 `0.1.0`：`package.json`、`tauri.conf.json`、`Cargo.toml`；引擎服务头硬编码 `LiveNotes/0.1` | [package.json](../desktop/package.json)、[server.py:764](../app/server.py) |
| 诊断包 | 只报壳版本（`CARGO_PKG_VERSION`），引擎只报状态/端口 | [diag.rs:1501](../desktop/src-tauri/src/diag.rs) |
| 体积 | `resources` 634.5MB，安装包 **278.33MiB**，壳本体 10.3MB | [docs/20](20-tauri-2-adr.md) §8 验收记录 |
| 模型 | `models/` 257MB：SenseVoice int8 239MB、CAM++ 28MB、silero VAD 0.6MB | 本机实测 |
| 数据 | `%LOCALAPPDATA%\LiveNotes\data`，SQLite；迁移是**只增列**的 `ALTER TABLE` | [engine.rs:465](../desktop/src-tauri/src/engine.rs)、[store.py:477](../app/store.py) |
| 签名 | 壳、`engine.exe`、安装包均未签名 | [docs/20](20-tauri-2-adr.md) §8 待办 5 |
| 发版 | `desktop/build-release.cmd` 只跑 `npx tauri build`，不产出更新清单、不上传 | [build-release.cmd](../desktop/build-release.cmd) |

**一句话**：包能打出来，也能装，但**发出去之后没有任何回程**——用户停在哪个版本、装没装上、
失败在哪一步，全都不知道。

---

## 2. 问题定义：每次发一个 EXE，贵在哪

| 成本 | 具体表现 | 不解决的后果 |
|---|---|---|
| 人工 | 每个用户单独传文件、说清点哪个、处理「装到哪了」 | 发版频率被压缩，修一个错要攒成一次大版本 |
| 用户注意力 | 用户要重新下载 278MB、重新安装、担心数据没了 | 大量用户永不升级，版本碎片化 |
| 带宽 | 1000 用户 × 278MB ≈ 280GB / 次 | 每次更新几十到上百元流量，且全是重复的模型 |
| 信任 | 未签名安装包，浏览器下载触发 SmartScreen | 首装流失；内部试用也会被安全软件拦 |
| 可观测 | 不知道失败率、版本分布 | 出问题只能靠用户描述，不能止损 |

拆包之后，常规更新只带「壳 + 引擎」，模型和 ffmpeg 留在本地，用户升级的是代码而不是资产。

---

## 3. 目标形态

```
                    ┌─ 壳通道（低频，每次发版都可能有）
发布流水线 ─────────┤   tauri-plugin-updater + latest.json + minisign
(release.cmd)       │
                    ├─ 引擎通道（中频，几十 MB，zip + sha256）
                    │   components/engine/<version>/ + 指针切换
                    │
                    └─ 模型通道（低频，257MB，首装自带或首启下载）
                        components/models/<version>/ + 文件级清单
```

| 通道 | 多久变一次 | 内容 | 交付方式 | 失败后果 |
|---|---|---|---|---|
| 壳 | 每次发版 | `LiveNotes.exe` + WebView2 壳 + 静态页 | NSIS 更新包，应用内静默/被动安装 | 装不上就留在旧版，可手动下载 |
| 引擎 | 功能迭代 | `engine.exe` onedir（onnxruntime、sherpa、Python 运行时） | zip 下载 → 校验 → 解压版本目录 → 指针切换 | 回退上一版本目录或出厂引擎 |
| 模型 | 半年量级 | SenseVoice / CAM++ / VAD | 首装自带；后续按文件清单增量 | 无模型时拒绝录音，但笔记/回放可用 |

---

## 4. 阶段 1：壳自动更新（1–2 人日）

### 4.1 组成

- `tauri-plugin-updater`：检查、下载、验签、运行 NSIS 安装器；
- 安装完成后调用 `AppHandle::restart()`（Tauri 2.12.5 自带，不需要额外的 process 插件）；
- 私钥离线保管，构建机通过 `TAURI_SIGNING_PRIVATE_KEY` /
  `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` 注入；
- `bundle.createUpdaterArtifacts: true`，构建额外产出 `.nsis.zip` 与 `.sig`。

```json
{
  "bundle": {
    "createUpdaterArtifacts": true
  },
  "plugins": {
    "updater": {
      "pubkey": "<tauri signer generate 出来的公钥>",
      "endpoints": ["https://dl.<domain>/livenotes/stable/latest.json"],
      "windows": { "installMode": "passive" }
    }
  }
}
```

> 由 Rust 侧直接检查/安装时不需要给前端加 `updater:default` capability；
> 只有在页面 JS 里调用 `@tauri-apps/plugin-updater` 才需要放开。

### 4.2 `latest.json`

```json
{
  "version": "0.2.0",
  "notes": "修复：录音中切换房间偶发断流",
  "pub_date": "2026-11-20T02:00:00Z",
  "platforms": {
    "windows-x86_64": {
      "signature": "<.nsis.zip.sig 的内容>",
      "url": "https://dl.<domain>/livenotes/0.2.0/LiveNotes_0.2.0_x64-setup.nsis.zip"
    }
  }
}
```

- 静态文件即可，`Cache-Control: no-store`；安装包本身用长缓存（文件名带版本号）。
- 更新源不直连 GitHub：国内用对象存储 + CDN（OSS / COS / 七牛），GitHub Releases 只做海外镜像。

### 4.3 检查与安装的产品约束

| 规则 | 行为 | 原因 |
|---|---|---|
| 检查时机 | 启动后 3–5 分钟、之后每 6 小时、设置页手动 | 避开启动高峰，不给引擎启动添堵 |
| 录音/采集/排队任务进行中 | 只提示，**不下载不安装** | 重启引擎会中断正在进行的场次 |
| 下载完成但未到空闲 | 托盘/首页提示「已就绪，结束后安装」 | 用户可自行选择「立即重启」 |
| 安装前 | 先停引擎、等待进程退出，再运行安装器 | `engine.exe` 占用 `resources/engine` 会导致覆盖失败 |
| 强更 | 不做客户端强更；安全事件用服务端开关 + 醒目提示 | 避免半夜把用户应用强制重启 |
| 失败 | 保留旧版本，报告可读错误，给手动下载链接 | 安装器失败不能变成「应用消失」 |
| 网络失败 | 指数退避，不弹连续错误 | 内网/弱网环境很常见 |

提示样式沿用 [docs/35](35-hud-tray-notification-design.md) 的应用内提示 + 托盘通知，不走系统弹窗轰炸。

### 4.4 发布脚本

新增 `desktop/release.cmd`（或 `tools/release.py`，便于处理签名与清单）：

```
读 VERSION → package-engine.py → npx tauri build
  → 产出 LiveNotes_<ver>_x64-setup.exe / .nsis.zip / .sig
  → 生成 latest.json（version / notes / pub_date / signature / url）
  → 上传安装包与 latest.json 到对象存储
  → 打印「本地自测 + 灰度」检查清单
```

上传凭据只放发布机的环境变量，不进仓库。`latest.json` 最后上传：先传安装包，再切清单，
保证任何一个客户端读到的清单都指向已存在的产物。

### 4.5 版本号单一来源（阶段 1 顺带做）

现在 `package.json`、`tauri.conf.json`、`Cargo.toml`、引擎服务头各写一份版本。
建议根目录 `VERSION` 作为唯一来源，构建脚本注入：

- `tauri.conf.json` 的 `version`（构建前改写或 `tauri build --config` 覆盖）；
- `Cargo.toml` 的 `version`（`build.rs` 读 `VERSION` 写进编译期环境变量，
  诊断包从该变量取值）；
- 引擎：`package-engine.py` 生成 `engine-version.json` 随 onedir 落盘，内容为
  `{"version": "...", "api": 1, "min_shell": "0.2.0"}`；引擎启动时读取，
  `/api/health` 增加 `version` 字段（壳本来就在打这个接口，不新增端点）；
- 服务头 `LiveNotes/0.1` 改为读取同一来源。

没有这个，阶段 2 的「引擎与壳分开升级」无法判断兼容性。

### 4.6 验收（阶段 1）

1. 干净 Windows 11 VM：装 0.1.0 → 发布 0.2.0 → 应用内提示 → 安装 → 重启后版本正确；
2. 录音进行中触发更新：不安装；结束后提示安装仍成功；
3. 断网、下载中途断、签名不匹配：旧版本可继续用，错误可读，不出现半装状态；
4. 升级后：`%LOCALAPPDATA%\LiveNotes\data` 中的场次、设置、录音都在；
5. 安装后引擎能被壳拉起，诊断包同时显示壳与引擎版本。

---

## 5. 阶段 2：拆包，让常规更新从 278MB 降到「壳 + 引擎」（3–5 人日）

### 5.1 目录布局

```
%LOCALAPPDATA%\LiveNotes\
  data\                      # 已有：录音、SQLite、设置、日志
  components\
    models\<version>\        # 257MB，含 manifest.json（文件 + sha256 + bytes）
    engine\<version>\        # onedir，含 engine-version.json
    current.json             # {"engine": "0.4.2", "models": "2026.11"}
    staging\                 # 下载中，完成后原子改名
```

安装包里的 `resources/models`、`resources/engine` 保留为**出厂兜底**，不再作为唯一来源。

### 5.2 模型通道

- 首装二选一：**建议安装包内置一次**（首装即可用、离线可用），之后不再随壳重发；
  低配/离线环境不需要首启等 257MB 下载。
- 更新走 `manifest.json`：逐文件 sha256 + 大小，按文件粒度增量；SenseVoice 的
  `model.int8.onnx` 是单文件 239MB，换模型时无法按块增量，这是模型通道“低频”的原因。
- 校验失败或下载中断：保留旧版本目录，切回 `current.json` 的上一版。
- 模型再分发许可需逐项确认（[docs/18](18-desktop-packaging-options.md) §3 第 3 条），
  商业包上线前必须收口。

### 5.3 引擎通道

- 远端 `engine/latest.json`：`{"version", "url", "sha256", "min_shell", "notes"}`；
- 壳启动时比较本地版本，满足 `min_shell` 才允许下载/激活；
- 下载 zip → 校验 sha256 → 解压到 `components/engine/<version>/` → 写 `current.json` 指针；
- 切换失败（启动 75 秒内不健康）→ 自动回退上一版本，再不行回退出厂 `resources/engine`；
- 引擎启动路径新增一层：`LIVENOTES_ENGINE_EXE` → `components/current.json` 指向的目录 →
  `resources/engine`。现成的 fallback 结构在 [engine.rs:408-460](../desktop/src-tauri/src/engine.rs)。
- 兼容矩阵：`engine-version.json` 的 `min_shell` 由发布流水线生成并写死；
  壳拒绝激活不满足范围的引擎，引擎拒绝在 API 版本不匹配时启动。

### 5.4 升级前的数据安全

- 迁移是只增列的 `ALTER TABLE`，旧版本引擎通常能读新库，但仍建议：
  升级前对 SQLite 做一次 `VACUUM INTO data/backup/<from>-to-<to>.sqlite`（只备份库，
  不复制录音），失败则这次更新不安装；
- 更新说明里写清「向前不兼容」的版本（如删除列、改索引），这类版本禁止自动安装，
  只提示手动安装并先导出备份。

### 5.5 改动清单

| 文件 | 改什么 |
|---|---|
| `desktop/src-tauri/src/engine.rs` | 引擎查找顺序加用户目录；启动前读 `engine-version.json` 做兼容检查 |
| `desktop/src-tauri/src/lib.rs` | 启动时后台检查引擎/模型通道；更新状态进托盘 |
| `desktop/package-engine.py` | 生成 `engine-version.json`；`--with-models` 改为可选打包模型 |
| `app/runtime.py` / `app/server.py` | `/api/health` 带引擎版本；模型目录解析支持 `LIVENOTES_MODELS_DIR` 已存在，补“用户目录优先” |
| `tools/release.py` | 生成并上传三条通道清单；默认只发壳，`--engine` / `--models` 显式才动 |
| `docs/22` | 安装/升级/回滚的用户侧说明 |

### 5.6 体积与成本变化

| 项 | 拆包前 | 拆包后（预计，待实测回填） |
|---|---|---|
| 首次安装 | 278MiB | 仍约 278MiB（模型内置一次） |
| 常规更新 | 278MiB | 壳 + 引擎压缩包，预计百 MB 以内 |
| 模型更新 | 随壳全量 | 257MB，半年量级 |
| 1000 用户一次更新流量 | ~280GB | 视引擎包实测，约 1/3 以下 |

> 引擎 onedir 的压缩体积在第一次拆包构建后实测回填本表；不要拍脑袋写数字。

---

## 6. 阶段 3：签名、灰度与可观测（约 3 人日）

### 6.1 Windows 代码签名

- 签名对象：安装包、`LiveNotes.exe`、`engine.exe`（PyInstaller 产物最容易被杀软误报）；
- 证书路线：OV/EV 证书（需企业/个体资质）或 Azure Trusted Signing（订阅制、对主体资质有要求），
  按主体情况二选一；
- 签名不解决「SmartScreen 立刻信任」，只缩短建立声誉的过程；无签名则首装必然被拦；
- 私钥/云签名账号权限最小化，只给发布机。

### 6.2 灰度与熔断

- `latest.json` 按 channel 分目录：`stable` / `beta`；壳的设置页或环境变量
  `LIVENOTES_UPDATE_CHANNEL` 决定读哪个。
- 灰度靠「先只给内部 cohort 的 endpoint」实现，24 小时无异常再切 stable。
- 熔断需要动态端点（云函数 / Worker 返回同一份 JSON），才支持：
  - 禁止升级到某版本（发现严重问题，立刻停发）；
  - 按比例放量；
  - 强制最低版本提示（不自动装，只提示）。
- 静态 `latest.json` 是最小可运维形态；动态端点在阶段 3 再换，接口保持同一份 JSON 结构。

### 6.3 指标（匿名，默认开启，可关）

每次检查/安装上报一条：`{client_id, from_version, to_version, channel, os, arch, result, error_code}`。

- 只上报版本与结果，不上报内容、路径、录音信息；
- `client_id` 为数据目录内随机 GUID，可在设置里重置；
- 关注四个数：版本分布、更新成功率、失败码 TOP3、更新后 7 日留存/崩溃率。

### 6.4 密钥与供应链安全

- minisign 私钥泄漏 = 可以向所有用户推送任意代码；私钥离线上锁，CI 只注入一次；
- 发布账户开双因素；上传权限只到发布目录；
- 每次发版记录产物 sha256，与 `latest.json` 一起归档。

---

## 7. 发版 SOP

### 7.1 正常发版

1. 合并到 `main`，确认 CI/测试通过；
2. 改根 `VERSION`，写发布说明（用户能读懂的一句话）；
3. 跑 `desktop/release.cmd`（含引擎打包、构建、签名、生成清单）；
4. 在干净 VM / 沙箱装**上一版**，从应用内升级到新版本，跑冒烟：
   打开首页、开始一次 30 秒采集、停止、生成笔记录音可播放；
5. 上传 beta，内部 24 小时；
6. 切 stable；
7. 记录：版本号、产物 sha256、灰度时间、已知问题。

### 7.2 回滚

| 情况 | 动作 |
|---|---|
| 新版本小范围故障 | 停发：把 channel 指回上一版清单（新用户不升级）；已升级用户发修复版 |
| 新版本普遍故障 | 发 N+1 修复版，说明里写清问题与处理；必要时在设置页给「下载上一版」链接 |
| 新版本写坏数据 | 用升级前 SQLite 备份 + 修复版恢复；录音文件不随更新变动 |
| 更新通道本身故障 | 保留直链下载页作为兜底，应用内提示「手动下载」 |

> updater 不会降级：任何「回滚」最终都表现为一个版本号更高的发布。

---

## 8. 分发渠道与成本

| 渠道 | 用途 | 说明 |
|---|---|---|
| 自有下载页 + OSS/CDN | **主渠道** | 首装下载、更新清单、直链兜底；国内必须可用 |
| GitHub Releases | 海外/备用镜像 | 不放进 `endpoints` 首选；国内访问不稳定 |
| 微软商店 / MSIX | 以后可选 | 商店审核与权限模型会拖慢迭代；采集/静默启动策略要重验 |
| winget | 技术用户 | 以后一条清单的事，不阻塞 |

成本粗算（按国内对象存储/CDN 阶梯价，约 0.2–0.3 元/GB 出流量，落账前以当期价为准）：

- 拆包前：1000 用户 × 278MB ≈ 280GB ≈ 60–90 元 / 次更新；
- 拆包后：按引擎包实测体积，预计降到 1/3 以下；
- 静态清单与签名文件可忽略不计。

钱不是主要问题，**「用户停在哪个版本」才是**。

---

## 9. 风险与明确不做

| 不做 | 原因 | 替代 |
|---|---|---|
| 自研壳更新器 | 检查、验签、安装、重启每步都能出错，且没有差异化价值 | `tauri-plugin-updater` |
| 差分/bsdiff 更新 | onedir 大量小文件，收益不稳定，故障面大；先拆包更划算 | 拆通道 |
| 录音中自动重启安装 | 中断用户正在进行的场次，损害大于收益 | 延后到空闲 |
| 用 `latest.json` 降级回滚 | semver 只升不降，客户端不会接受旧版本 | 修复版 + 服务端开关 |
| 首版就发微软商店 | 审核周期、权限策略、采集行为要重验 | 自有下载页先行 |
| 模型每次随壳重发 | 257MB 全是重复下载 | 模型通道 + 用户目录 |
| 把 GitHub 当国内更新源 | 可用性不可控，失败即更新失败 | 对象存储 + CDN |

其他风险：

- **WebView2 运行时**：首装需要 WebView2；离线环境用 `embedBootstrapper` / `offlineInstaller`，
  这与安装包体积直接相关，先按“联网首装”为主，离线包单独出。
- **杀软误报**：PyInstaller + 未签名 = 高风险；签名和「少用 onefile」都要坚持。
- **自动更新与单实例/托盘**：安装前必须走完整的退出流程（[docs/27](27-process-lifecycle.md)），
  否则 NSIS 覆盖文件失败。
- **模型/引擎通道扩大了攻击面**：所有下载都必须 sha256 + 来源域名固定，失败即回退，
  不接受“先解压再说”。

---

## 10. 验收标准（整体）

1. 干净 Windows 11 VM 上，从 N-1 版本应用内升级到 N 版本，无需下载安装包和教程；
2. 升级期间正在录音：不中断，结束后安装成功；
3. 升级后：数据目录、设置、录音、飞书配置全部保留，引擎健康，诊断包显示两个版本；
4. 更新包被篡改/签名不符：拒绝安装，旧版本继续可用；
5. 引擎通道：手动推一个坏引擎，壳自动回退到上一版或出厂版，并在托盘给出可读原因；
6. 模型通道：断点续传、校验失败回退、离线安装包可用；
7. 发布动作：从合并代码到 stable 全量，只有「跑一条命令 + 两次确认」，不手工拼清单；
8. 能回答「现在有多少用户、各是什么版本、最近一次更新失败率多少」。

---

## 11. 待决策（需要拍板）

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| 1 | 更新源用哪家对象存储 | 阿里云 OSS / 腾讯 COS / 七牛 | 跟着已有云账号走，域名 `dl.<domain>`，先不买独立 CDN |
| 2 | 签名主体与预算 | 公司主体 OV/EV / Azure Trusted Signing / 暂不签 | 对外首发前必须签；内部试用可先用未签名 + 哈希校验 |
| 3 | 首装是否内置模型 | 内置（278MB）/ 首启下载（安装包 ~40MB） | 内置一次，阶段 2 之后模型走独立通道 |
| 4 | 是否需要授权码/账号 | 无 / 激活码 / 账号登录 | 与 docs/38 商业化一起定；不阻塞更新链路 |
| 5 | 灰度节奏 | 静态清单 / 动态端点 | 先静态；有 100+ 外部用户后再上动态端点 |

---

## 12. 里程碑

| 阶段 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| M0 | 本文评审、确定对象存储与域名 | 0.5 人日 | 决策 §11-1 |
| M1 | 版本号单一来源；`/api/health` 带引擎版本；诊断包补引擎版本 | 1 人日 | — |
| M2 | updater 接入、`latest.json`、`release.cmd`、应用内提示与「录音中不装」 | 1.5 人日 | M1、签名密钥 |
| M3 | 对象存储 + CDN + 下载页，发布一次真实版本 | 1 人日（偏运维） | M0 |
| M4 | 模型移出安装包、用户目录 + 清单校验 | 2–3 人日 | M2 |
| M5 | 引擎独立通道、兼容矩阵、自动回退 | 3–4 人日 | M4 |
| M6 | 代码签名、beta/stable、匿名指标 | 约 3 人日 | 证书、M3 |

阶段 1（M1–M3）目标：**一周内让「更新」从人工分发变成一次命令**；阶段 2 的目标不是更快的更新，
而是让每次更新的体积对得起它改动的内容。
