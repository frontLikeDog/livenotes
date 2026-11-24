# 45 · 离线补转写（仅采集节点出总结）

> 面向：部署、实现
> 依据：[43 · 仅采集模式](43-capture-only-mode.md)、[44 · 云端飞书机器人](44-cloud-feishu-bot.md)
> 代码：`app/offline_asr.py`、`tools/offline_asr.py`
> 结论时间：2026-11-25

## 1. 解决什么

仅采集节点按设计不加载 ASR，数据库里没有 `utterances`，飞书的
`/总结` 只能回「还没有字幕」。离线补转写用一个**独立进程**，把已录的
PCM/WAV 增量转成文字写回库；总结、近聊、观点、回放全部复用现有逻辑，
采集链路一行不改。

它和「完整实时链路」的区别：

| | 完整实时 | 仅采集 + 离线补转写 |
|---|---|---|
| 采集进程内存 | 模型 + ASR 池，约 350–400MB | 约 60MB，不变 |
| 字幕延迟 | 实时 | 通常几十秒；积压时每轮最多补 120 秒 |
| 说话人 | 实时在线聚类 | 直播中「说话人N」，收尾后全场重聚类 |
| 飞书总结 | 有 | 有（等补出字后） |
| 故障边界 | 模型拖垮采集进程 | 补转写进程被 OOM 只影响它自己 |

## 2. 工作方式

```
data/audio/<sid>.pcm ──增量读──┐
                               ├─ silero VAD ─ SenseVoice ─ utterances
data/audio/<sid>.wav ──收尾读──┘        │
                                        └─ CAM++ embedding ─ 收尾重聚类
```

- 采集中的 `.pcm` 是无头 16k/单声道/s16le，按字节偏移增量读；VAD 状态
  在进程内一直保留，不会每轮在缓冲区边界切句。
- 场次收尾后 `audio_path` 出现，进程改读 WAV（优先 `audio_original_path`），
  把 spool 最后一小段补完，再 `vad.flush()`。
- 进度写在 `offline_asr` 表（`processed_s / status / n_utterances`）。
  进程重启从上次位置继续；同一时间戳的句子会去重，不会整场重跑。
- 声纹照常保存；场次收尾后调用 `recluster.recluster_session()` 做全场
  重分人。句子少于 4 条时自动跳过，标签保持「说话人N」。
- 不修改 `sessions.capture_only`：这一场仍然是「仅采集」，只是多了
  一份事后补出的逐字稿。

## 3. 部署

只装了 `requirements-capture.txt` 的机器，补实时依赖和模型：

```bash
cd /opt/livenotes
.venv/bin/pip install -r requirements-live.txt
.venv/bin/python tools/fetch_models.py     # 约 270MB，已存在会跳过
```

> 下载器只校验文件大小，并行分片偶尔会写出「大小对、内容坏」的
> ONNX（本次 `campplus.onnx` 就报了 `Protobuf parsing failed`）。
> 遇到时删掉文件重下，或从另一台已跑通的机器复制并比对 md5。

`/etc/systemd/system/livenotes-offline-asr.service`：

```ini
[Unit]
Description=LiveNotes offline ASR (capture-only backfill)
After=network-online.target livenotes.service
Wants=network-online.target
Requires=livenotes.service

[Service]
Type=simple
User=livenotes
Group=livenotes
WorkingDirectory=/opt/livenotes
EnvironmentFile=/opt/livenotes/livenotes.env
Environment=PYTHONUNBUFFERED=1
Environment=LIVENOTES_ASR_THREADS=1
Environment=MALLOC_ARENA_MAX=2
ExecStart=/opt/livenotes/.venv/bin/python /opt/livenotes/tools/offline_asr.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
Nice=10
# 1GB 机器上把补转写关在自己的 cgroup 里：真顶不住时只杀它，
# 不碰采集和飞书机器人。模型常驻约 300–400MB，按机器余量调。
MemoryHigh=480M
MemoryMax=560M
MemorySwapMax=900M
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
systemctl enable --now livenotes-offline-asr
journalctl -u livenotes-offline-asr -f
```

`livenotes.env` 可调：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `LIVENOTES_OFFLINE_ASR_INTERVAL` | `5` | 每轮扫描间隔（秒） |
| `LIVENOTES_OFFLINE_ASR_DAYS` | `2` | 自动补最近几天内的场次 |
| `LIVENOTES_OFFLINE_ASR_MAX_FEED_S` | `120` | 每轮每场最多补多少秒 |
| `LIVENOTES_OFFLINE_ASR_SPEAKERS` | `1` | 关掉可省 CAM++ 和 embedding，只留一个「说话人」标签 |

> 不需要为了补转写重启 `livenotes.service`：总结接口直接读库。
> 飞书 `/状态` 里的「补转写 N 句」要等引擎和机器人重启后才显示；
> 不重启也不影响 `/总结`。

## 4. 手动命令

```bash
# 常驻（systemd 就是这么跑）
.venv/bin/python tools/offline_asr.py

# 只跑一轮，排障用
.venv/bin/python tools/offline_asr.py --once

# 指定一场补到收尾（历史场次也行）
.venv/bin/python tools/offline_asr.py --session LIVE_20261125_003510

# 所有仅采集场次补一轮；注意 CPU 和磁盘
.venv/bin/python tools/offline_asr.py --backfill --limit 5
```

进度与结果：

```bash
sqlite3 /opt/livenotes/data/livenotes.db \
  "select session_id,round(processed_s),status,n_utterances,error from offline_asr;"
sqlite3 /opt/livenotes/data/livenotes.db \
  "select count(*) from utterances where session_id='LIVE_...';"
```

## 5. 资源与边界

- 模型：SenseVoiceSmall int8 239MB + CAM++ 28MB + onnxruntime/numpy。
  实测阿里云北京节点（`MemTotal` 701MB）空载 RSS 约 455MB；补完
  3 小时 40 分积压后约 440MB RSS + 200MB swap。单线程解码，
  `Nice=10` 让采集和飞书优先。
- 内存：同一台机器上飞书机器人常驻约 170MB，和补转写不能同时装进
  701MB 物理内存；内核会把空闲的机器人换出，发命令时再换入。
  功能可用，但手机端首次响应会慢几秒。长期要两者都流畅，建议 2GB。
- 首次补长积压时，模型工作集加机器人会触发持续换页，`vmstat` 的
  `si/so` 可到 10MB/s；追平后只按 5 秒增量跑，压力回落到接近 0。
  追平后重启一次本服务可把换出的页收回，RSS 回到 450MB 左右。
- 2 vCPU 上 1 路语音厅通常能跟上；两路以上先看
  `journalctl -u livenotes-offline-asr` 的「读入字节」是否持续为 0
  （一直为 0 说明已经追平），以及 CPU 占用。
- 历史场次只保留 WAV 2 天；超过保留期只剩 m4a 时，进程会用 ffmpeg
  解码，速度慢一些但能补。
- 总结仍需要 `settings.json` 里的大模型配置；没配或调用失败时直接
  报错，不再生成本地抽取版。补齐配置后重新 `/总结` 即可。
- 转写质量与实时链路相同：BGM、抢话、远场仍会影响断句和错字；
  总结卡片上的「可能存在错字」不是免责声明，是实际情况。

## 6. 测试

```bash
python -m pytest tests/test_offline_asr.py tests/test_feishu_capture_only.py -q
```

用例覆盖：PCM 增量出字与时间戳、无新音频不重复、追加音频继续、
WAV 收尾 flush 并标记 done、重启按时间戳去重、完整链路场次被忽略、
飞书在补出字后显示「补转写 N 句」并放出总结按钮。
