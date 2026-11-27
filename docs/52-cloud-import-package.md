# 52 · 云端导入包：把云采集的场次带回桌面

> 面向：实现 / 产品自测
> 依据：[46 §8 D2/D4](46-three-surfaces-governance-review.md)（桌面不接云端引擎，只做导入）、
> [47](47-feishu-native-reading-surface.md)、[50](50-cloud-folders-and-archive.md)
> 代码：`app/importer.py`、`tools/import_session.py`、`POST /api/import/file`、
> `POST /api/import/text`、`app/web/files.html` 的「导入场次」
> 结论时间：2026-11-27

## 1. 为什么是导入包，不是远程引擎

云端节点只监听 `127.0.0.1`、没有备案域名，桌面连不过去；即使连得过去，也要在
鉴权、隧道、版本兼容上再建一套（46 §8 D2 已否决 B）。而云端能带出来的东西很具体：

- 飞书 `/分享包` 发的 zip（`summary.md` + `transcript.srt` + 可选 `audio.m4a` +
  `bookmarks.json`）；
- 云空间场次文件夹里的逐字稿 txt（`export.to_archive_txt` 的格式）。

导入包把这两样还原成一场本地场次：能阅读、能回放、能补名、能重新总结、能进周报
和人物页。这样「白天手机录、晚上桌面整理」是同一条链路，而不是两套历史。

## 2. 支持什么

| 输入 | 处理 |
|---|---|
| 分享包 zip（含 `transcript.srt`） | 读 `meta.json` 的标题/开始时间/时长/妙记链接；书签只收打点，高光不收（高光是算出来的，导进来会变成假的「当时标过」） |
| 单独的 `transcript.srt` | 只取时间码与正文，标题按文件名 |
| 逐字稿 txt（`to_archive_txt` / `to_txt`） | 抬头里的「# 时间 ·「标题」」「场次：」「时长：」都取；正文按 `[HH:MM:SS]` 解析 |
| 录音（包内 `audio.*`，或单独补） | 复制进 `data/audio/<sid>.<后缀>`，写 `sessions.audio_path`；m4a 校验 moov，WAV 校验帧数 |

不支持的：音频文件本身（那是重新转写，不是导入）、飞书文档链接（没有稳定导出接口）。

## 3. 边界（必须如实说，不许假装）

- **没有声纹**：只有文字，没有 `utterance_emb`。所以：
  - 「重新分人」明确拒绝，文案是「云端导入的场次只有文字、没有声纹，不能重新分人」；
  - 补名只改本场显示，**不调用声纹注册**，返回 `enrolled=false`、
    `reason="云端导入的场次没有声纹，名字只改了本场显示"`；
  - 补名面板、收尾卡的提示文案换成 `naming.top_speakers` 返回的 `hint`
    （`voiceprint=false`），不再写「声纹入库，下次自动认」。
- **不重新转写**：`capture_only=0`，离线补转写（`offline_asr._targets` 只看
  `capture_only=1`）不会把它捡走再跑一遍 ASR。
- **不做本地兜底**：解析不出任何一句话就报「没有解析到任何发言，未导入」，
  不生成空场次（同项目「宁可明确失败」的约定）。
- 说话人只在「每一行都有前缀、且前缀种类不多」或「同一前缀出现 ≥2 次」时才认；
  否则冒号留在正文里，绝不把「结论：」当成一个人。
- 来源里的「本场第 3 位上麦者」是位置标签，改写成本场的 `说话人3`，不进署名。

## 4. 幂等与覆盖

- 场次 id：`IMP_` + 来源场次 id（`meta.json` 的 `session_id` 或 txt 抬头里的
  `场次：`），没有来源 id 时用前 50 句文本的 sha1 前缀。
- 已在库再次导入：原样返回 `existed=true`，不覆盖；如果这次带了录音而那一场
  没有，只补录音。
- `replace=true`（CLI `--replace`）：清掉这一场的句子、说话人、总结、观点、笔记索引、
  打点、高光、声纹、离线补转写进度，再重建。`feishu_publish` 保留——重发时更新
  同一篇文档，不另建一篇。

## 5. 入口

```bash
# 命令行（本机最直接）
python tools/import_session.py --file D:/Downloads/LIVE_20261127_2130.zip
python tools/import_session.py --file cloud.txt --audio D:/Downloads/audio.m4a
python tools/import_session.py --file transcript.srt --title "今晚语音厅" --replace
```

网页：文件中心（`/files`）工具栏「导入场次」，或导入场次那一行的「补录音」。
上传走原始文件体（`X-File-Name` + 流式写入临时文件，1GB 上限），不走 multipart——
stdlib 服务没有解析器，也不该为它加一个依赖。小文本可以直接
`POST /api/import/text {"text": "...", "name": "cloud.txt"}`。

### 5.1 手工自测（三步）

1. 飞书里给机器人发 `/分享包 CH01`，把收到的 zip 存到电脑；
2. 打开 `/files` →「导入场次」→ 选这个 zip：列表里应出现「云端导入」的一场，
   句数、时长、说话人数和云端 `/状态` 对得上；
3. 点「阅读」改名、点「播放」听录音：改名提示应是「只改本场显示」，
   「重新分人」应明确说「没有声纹，不能重新分人」。

## 6. 测试

```bash
python -m pytest tests/test_importer.py -q
```

覆盖：SRT 前缀识别（说话人列 vs 正文冒号）、txt 抬头与时间码、分享包原样导回、
幂等与 replace 的破坏面、单独补录音、导入场次补名不注册声纹、重聚类明确拒绝、
HTTP 原始文件上传 / JSON 文本上传 / 错误输入、总结提示里的导入边界。

## 7. 还没做

- 机器人侧「把云端某一场的包直接发给我」（目前是桌面主动选文件导入）；
- 导入后自动跑一次总结（现在和其它场次一样由用户点）；
- 用导入包里的 `summary.md` / `note.md` 还原云端已生成的总结（现在只导逐字稿、
  打点和录音；云端总结仍以飞书文档为准）。
