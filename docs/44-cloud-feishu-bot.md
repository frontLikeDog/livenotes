# 44 · 云端飞书机器人（手机端）

> 面向：部署、运维
> 依据：[43 · 仅采集模式](43-capture-only-mode.md)、[14 · 飞书直播简报](14-feishu-live-brief.md)、[17 · 采集生命周期](17-capture-lifecycle.md)
> 代码：`tools/feishu_bot.py`、`app/livebrief.py`
> 结论时间：2026-11-25

## 1. 目标与边界

出门只带手机时，用飞书机器人操作云端的仅采集节点：

- 看每路房间在不在录、断没断流；
- 开始/停止/重试一路；
- 按需把最近一段原声以飞书语音消息发到聊天里。

不做的部分：

- **不连续实时推流**。飞书没有给机器人持续推音频的接口，只能按需截一段
  （单条 ≤5 分钟，`app/clips.py` 的 `MAX_SECONDS`）。
- 机器人本身不做转写/说话人/总结。同机可以另跑一个离线补转写进程
  （[45](45-offline-asr.md)）：它把录音增量转成文字写回库，之后
  `/总结`、`/近聊` 正常可用，采集和机器人都不用改。
- 不开公网端口、不需要域名和 ICP 备案、不用 VPN/组网/远程桌面。
  机器人是**出站长连接**，云端只监听 `127.0.0.1`。

在这台北京服务器上，这是唯一不用备案就能让手机访问的客户端路径；
需要完整网页和实时播放器时，见 [43](43-capture-only-mode.md) 的部署边界，
域名必须先完成 ICP 备案。

## 2. 组成

同一台 VPS 上三个核心 systemd 服务（第四个离线补转写可选）：

| 服务 | 作用 | 端口 |
|---|---|---|
| `livenotes.service` | 引擎：仅采集 + 直取，SQLite、录音、m4a | `127.0.0.1:8765` |
| `livenotes-capture.service` | 采集代理：按房间拉起 ffmpeg | 无 |
| `livenotes-feishu.service` | 飞书机器人：长连接收命令、发状态/语音 | 出站 |
| `livenotes-offline-asr.service` | 可选：离线补转写，给仅采集场次补逐字稿 | 无 |

机器人只读 `data/feishu.json`（`chmod 600`），通过 `LIVENOTES_API`
访问本机引擎；它不直接写数据库。

## 3. 部署

```bash
cd /opt/livenotes
.venv/bin/pip install "lark-oapi>=1.4"     # 长连接与消息 API
```

把桌面端的 `data/feishu.json` 复制到服务器同一路径（只复制，不打印内容）：

```bash
scp -i <key> feishu.json root@<server>:/opt/livenotes/data/feishu.json
ssh root@<server> 'chown livenotes:livenotes /opt/livenotes/data/feishu.json && chmod 600 /opt/livenotes/data/feishu.json'
```

同一个飞书应用**只能留一条长连接**：服务器接管机器人后，把桌面端
`%LOCALAPPDATA%\LiveNotes\data\feishu.json` 改名成 `.bak`（或设
`LIVENOTES_NO_FEISHU=1`），否则命令会被两边随机抢走。仓库里的
`data/feishu.json` 同理。

`/etc/systemd/system/livenotes-feishu.service`：

```ini
[Unit]
Description=LiveNotes Feishu bot (cloud, capture-only)
After=network-online.target livenotes.service
Wants=network-online.target
Requires=livenotes.service

[Service]
Type=simple
User=livenotes
Group=livenotes
WorkingDirectory=/opt/livenotes
EnvironmentFile=/opt/livenotes/livenotes.env
Environment=LIVENOTES_API=http://127.0.0.1:8765
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/livenotes/.venv/bin/python /opt/livenotes/tools/feishu_bot.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/livenotes/data

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now livenotes-feishu
journalctl -u livenotes-feishu -f
```

日志里出现 `connected to wss://msg-frontier.feishu.cn/...` 即长连接成功。
第一次给机器人发消息的人会被记成 owner；这台节点沿用桌面端已有的
`open_id`，不需要重新认主。

## 4. 手机上的命令

| 命令 | 作用 |
|---|---|
| 直接贴直播间链接 | 新建一路并开始监听 |
| `/状态 [CH]` | 每路状态；仅采集场次显示「已录 / 有声 / 仅录制」 |
| `/rooms` | 房间列表和 CH 编号 |
| `/分享 CH01 90秒` | 把最近 90 秒切成 opus 语音发到飞书 |
| `/分享 CH01 1:30 2分钟` | 从第 1:30 起截 2 分钟 |
| `/分享包 CH01` | 整场 m4a 当文件发到飞书（长场次可能受飞书文件大小限制） |
| `/重试 CH01` | 下播后再开播、或采集失败时重新拉起这一路 |
| `/stop CH01` | 停止这一路并关掉 ffmpeg |
| `/控制台` | 一张卡片看所有房间和按钮 |
| `/发布 [CH]` | 把这场发成飞书云文档 + 场次表一行，完成后发收尾卡（需 `FEISHU_PUBLISH_ENABLED=1`） |
| `/文档 [CH]` | 看这场发布到哪了：状态、文档链接、场次表、失败原因 |
| `/补名 CH01 1 阿哲 2 小北` | 给上麦的人补名字；已发布的场次会在后台重写文档 |
| `/回听` | 没有公网页面时返回 `/分享` 提示，不会给手机发打不开的 127.0.0.1 链接 |

发布器默认关闭；打开后总结完成会自动发布，飞书接口失败只影响发布，
采集、转写、总结照常。幂等规则与失败降级见 [47](47-feishu-native-reading-surface.md) §11。

仅采集场次在离线补转写出字前没有逐字稿，`/近聊`、`/总结`、`/周报`
没有内容可用；出字后状态显示「补转写 N 句」，`/总结` 直接可用。
状态和回执里不会出现「0 句」「还没有字幕」这种像故障的措辞。
云端没有公网页面时，简报证据只保留原话，不再发 127/172.24 这类
手机上打不开的回听链接；要听原声发 `/分享 CH01 90秒`。

## 5. 生命周期

- 房间在监听中时断流：直取会自己重建 ffmpeg（最多 3 次）；
  判定真下播后落 `stopped`，飞书会发一条「已自动收尾」。
- **下播后不会自动等下一场**（产品选择：显式停止高于自动续采）。
  下一场开播时在手机上发 `/重试 CH01`；状态是「已停止」就说明可以重试。
- 引擎重启会把上次残留的 `live/starting` 收口成「已中断」，同样用
  `/重试` 恢复。确实要无人值守续采的机器，可以设
  `LIVENOTES_RESUME_ROOMS=1`，但那是另一套取舍。
- 手动 `/stop` 后不会自动重开；这是有意的。

## 6. 运维

```bash
systemctl status livenotes livenotes-capture livenotes-feishu
journalctl -u livenotes-feishu -n 50 --no-pager
journalctl -u livenotes-capture -n 50 --no-pager
```

- 凭证轮换：改 `data/feishu.json` 后 `systemctl restart livenotes-feishu`。
- 升级 SDK：`.venv/bin/pip install -U "lark-oapi>=1.4"`，重启机器人。
- 磁盘：WAV 保留 2 天、m4a 保留 14 天（见 `livenotes.env`）。
  一路语音厅约 110MB/小时下行，7×24 约 79GB/月；先确认套餐的
  100GB 是入站还是出站口径，再决定能常开几路。
- 内存：飞书机器人（lark-oapi 长连接）常驻约 170MB；加引擎约 30MB、
  采集代理约 20MB，三件套约 220MB。再加离线补转写（实测约 440MB）
  在标称 1GB、实际 `MemTotal` 701MB 的机器上放不下两份热数据：
  必须配 2GB swap，并把补转写关进 `MemoryHigh/MemoryMax` cgroup
  （见 [45](45-offline-asr.md)）。内核会把空闲的机器人换出，命令仍可用，
  但首次响应会慢几秒；要长期流畅建议升 2GB，3 路以上必须 2GB 起。
- 直取节点建议在 `livenotes.env` 设 `LIVENOTES_DIRECT_FALLBACK=none`：
  Linux 上没有可用的浏览器采集，失败时只报错，不再提示「切换到浏览器」。
- 安全：机器人只做出站连接；引擎仍只绑回环；不要把 8765 开到公网。
  `feishu.json` 只给 `livenotes` 读，不要把 app_secret 贴到聊天或日志里。

## 7. 测试

```bash
python -m pytest tests/test_feishu_capture_only.py tests/test_offline_asr.py -q
```

用例覆盖：仅采集状态文案、停止场次的 `/分享` 提示、`/重试` 的
`pending` 落点、运行中的房间拒绝重试。

2026-11-25 在真实抖音语音厅上验证：云端机器人长连接成功；
`/状态` 输出「已录 3:19 · 有声 3:16 · 仅录制」；
`/分享 CH01 60秒` 成功把 60 秒 opus 语音发到飞书私聊。
