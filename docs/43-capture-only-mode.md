# 43 · 仅采集模式（capture-only）

> 面向：部署、实现
> 依据：[13 · 直取抖音流](13-direct-capture-spike.md)、[42 · 采集流量计量](42-traffic-metering.md)
> 代码：`app/config.py`、`app/live.py`、`app/server.py`
> 结论时间：2026-11-24

## 1. 为什么要有它

完整实时链路的固定成本不在采集，而在识别：SenseVoiceSmall int8 约 239MB、
CAM++ 约 28MB，加上 ONNX Runtime、ASR worker 和 VAD，2GB 内存的机器同时跑
1~2 路就很紧。而很多录制节点只需要**把声音完整录下来**，转写可以回到有算力的
机器上再做。

`LIVENOTES_CAPTURE_ONLY=1` 时，服务端只做三件事：

1. 接收 16 kHz / 单声道 / s16le PCM，写进 spool；
2. 停录时把 spool 收成 WAV，再按原逻辑转 m4a；
3. 记录时长、音量、丢块和断流指标，供收听页/看板查看。

不加载 ASR、不加载声纹、不起 VAD、不启动共享 ASR 工作池，也不产生逐字稿、
说话人、总结、高光和飞书推送。

## 2. 开关

| 位置 | 写法 | 说明 |
|---|---|---|
| 进程级 | `LIVENOTES_CAPTURE_ONLY=1` | 这台机器上所有新场次默认仅采集 |
| 单场 | `POST /api/live/start` 带 `"capture_only": true/false` | 覆盖进程级；不传则跟进程级走 |
| 查询 | `GET /api/live/info` 的 `capture_only` | 前端据此显示「仅采集模式」 |

场次会在 `sessions.capture_only` 落一列（老库自动补列，历史场次默认 0），
所以重启、回放、列表都能分清这一场有没有逐字稿。

## 3. 行为差异

| 能力 | 完整模式 | 仅采集 |
|---|---|---|
| 录音 / WAV / m4a | 有 | 有，同一套 spool、收尾、转码、保留期 |
| 实时字幕、临时字幕 | 有 | 无 |
| 说话人聚类、改名、重聚类 | 有 | 无 |
| 逐字稿、观点、总结、高光、周报 | 有 | 无（这一场没有文本） |
| 飞书实时推送 / 收工回顾 | 有 | 无 |
| 断流 / 全零静音看门狗 | 有 | 有（纯 stdlib RMS 判全零） |
| 收听页 `/live`、回放 `/reader`、文件中心 | 有 | 有；`/reader` 只放录音并明确写「仅采集」 |
| 采集指标（时长、丢块、队列） | 有 | 有；另有「有声时长」`audible_s` |

未转写时长在仅采集场次里没有意义：`recognized_s` 恒为 0，UI 不展示它。

## 4. 资源预期与选型

仅采集仍然需要 Python 与 numpy（应用模块在导入期会 import numpy），但不再有
239MB 模型与 ASR 工作池的内存峰值。本机实测（Windows，16 核，同口径只 import
`app.live`）：

| 状态 | 进程 RSS |
|---|---:|
| import 后 | 40.8 MB |
| 仅采集 1 路录音中 | **41.4 MB**（启动 0.4s，未加载模型） |
| 完整链路 1 路录音中 | 395.8 MB（自动起 8 个 ASR worker，模型加载 2.4s） |
| 真实 `run.py` 服务 + 1 路仅采集 | **51.9 MB**（进程树合计） |

直取本身另有每路约 30~40MB 的 ffmpeg 进程、CPU < 2%（见
[13](13-direct-capture-spike.md)），HTTP 服务与采集代理也会占一些。所以 1GB
内存级别的 VPS 可以承担 1~2 路录制；3 路以上仍建议 2GB 起，并配 swap。

磁盘按 WAV 115MB/小时/路估算，转 m4a 后约 15MB/小时/路。`LIVENOTES_WAV_RETENTION_DAYS`
默认 7 天，录制节点务必按保留期和磁盘容量核算。

流量另算：直取的下行取决于直播码率（1080p 常见 3~6 Mbps），1 小时约 1.4~2.7GB。
购买「100GB 流量包」的机器前，先确认它是**入站还是出站**计费，以及超额单价。
详见 [42](42-traffic-metering.md)。

2026-11-24 在一台 2 核 1G / 2Mbps 的 Debian 12 VPS 上用真实抖音语音厅实测：
直取解析 0.9s，low 画质落到 `radio` 音频流，下行 **257 kbps（约 110MB/小时）**；
2 分 19 秒录成 WAV 4.3MB、转 m4a 561KB（16kHz 单声道 AAC 33kbps，
mean_volume −23.2dB，不是静音）。全程引擎 64MB + 采集代理 36MB + ffmpeg 55MB，
丢块 0、队列 0。语音厅只有音频，2Mbps 的机器跑 2 路也绰绰有余；视频直播间
仍按上面的码率估算。

## 5. 部署

```bash
pip install -r requirements-capture.txt
# ffmpeg 需要单独安装并加入 PATH：直取与 m4a 转码都用它。
# 没有 ffmpeg 也能录：WAV 照样落盘，只是不转码，文件更大。
export LIVENOTES_CAPTURE_ONLY=1
export LIVENOTES_CAPTURE_MODE=direct
export LIVENOTES_NO_BROWSER=1       # 无桌面的 VPS 不需要拉起浏览器
python run.py --lan
```

另开一个进程，让采集代理按房间直取抖音流（`/rooms` 里添加房间后由它拉起 ffmpeg）：

```bash
LIVENOTES_CAPTURE_MODE=direct python tools/capture_agent.py --base http://127.0.0.1:8765
```

Linux 上直取只支持抖音；浏览器采集的 Chrome 发现逻辑目前是 Windows 专用。
服务本身没有登录鉴权，放到公网必须套反向代理做认证，并只暴露需要的路径。

启动横幅会打印 `[仅采集]` 提示；`/api/live/info` 的 `ready` 不再依赖模型文件，
`missing` 恒为空。

手机端（出门只带手机、不暴露公网端口）的接入方式见
[44 · 云端飞书机器人](44-cloud-feishu-bot.md)。

## 6. 测试

```bash
python -m pytest tests/test_capture_only.py -q
```

用例把 `ENGINE.load`、`POOL.register/start/drain_session`、`tasks.submit_finalize`
全部换成会抛异常的替身：采集链路一旦碰模型或工作池就失败。另有 HTTP 用例验证
`/api/live/start` 的 `capture_only` 透传、状态字段和 `sessions.capture_only` 落库。
