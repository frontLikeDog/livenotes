# -*- coding: utf-8 -*-
"""产品文案：一张术语表 + 一张错误表。

界面不出现「ASR / 采集 / 聚类 / token / 引擎 / 断流」这类词；失败必须说人话，
并且告诉用户「什么还在」。后端只在这里做一次映射，页面不用各写一套。
"""
from __future__ import annotations

#: 统一术语：旧说法 -> 新说法（也用于前端 title/aria 文案）
TERMS = {
    "ASR": "语音转文字",
    "asr": "语音转文字",
    "采集": "记录",
    "采集器": "记录程序",
    "引擎": "记录程序",
    "断流": "听不到声音",
    "聚类": "区分说话人",
    "token": "模型用量",
    "LLM": "AI",
    "RAG": "问笔记",
    "summary": "笔记",
}

#: 错误关键词 -> 人话。顺序有意义：越具体的放前面。
_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("未配置", "not configured", "no api key", "缺少 key", "missing key"),
     "还没有配置 AI 模型。可以先用本地要点，或去设置里填一个模型。"),
    (("401", "403", "unauthorized", "invalid api key", "invalid_key",
      "authentication"),
     "模型的 Key 不对或过期了。去设置里检查一下，录音和逐字稿都还在。"),
    (("429", "rate limit", "too many requests", "quota", "额度"),
     "模型今天有点忙或额度用完了。稍后再试，录音和逐字稿都还在。"),
    (("timeout", "timed out", "超时"),
     "等模型回应超时了。可以重试一次，录音和逐字稿都还在。"),
    (("connection", "network", "dns", "unreachable", "连不上", "网络"),
     "网络连不上模型服务。检查一下网络或代理，录音和逐字稿都还在。"),
    (("disk", "no space", "磁盘", "空间不足"),
     "磁盘空间不够了。清理旧录音或换一个存储位置。"),
    (("permission", "access denied", "eacces", "权限"),
     "没有权限读写这个位置。换一个目录，或检查文件夹权限。"),
    (("ffmpeg", "audio", "wav", "m4a", "音频"),
     "音频处理没成功。录音原文件还在，可以稍后重试。"),
    (("browser", "chrome", "浏览器", "webdriver"),
     "浏览器没有正常打开。重试一次，或改用直取音频。"),
    (("not found", "404", "不存在"),
     "找不到这个内容，可能已经被清理了。"),
    (("locked", "busy", "database is locked", "占用"),
     "文件正被另一个程序占用。关掉它再试一次。"),
    (("no such column", "no such table", "sqlite"),
     "本地数据需要升级。重启一次应用，数据不会丢。"),
)

DEFAULT_ERROR = "这一步没成功。可以重试一次；已经录下的内容都还在。"


def human_error(error) -> str:
    """异常 / 错误串 -> 一句人话。永远不返回堆栈或英文错误码。"""
    raw = str(error or "")
    low = raw.lower()
    for keys, text in _RULES:
        if any(k.lower() in low for k in keys):
            return text
    return DEFAULT_ERROR


def term(word: str) -> str:
    return TERMS.get(word, word)


def note_state(state: str) -> tuple[str, str]:
    """笔记状态 -> (标签, 说明)。页面和通知共用同一套说法。"""
    return {
        "empty": ("还没有笔记", "结束记录后会自动生成"),
        "insufficient": ("内容太少", "这场发言不足，暂时没有要点"),
        "queued": ("排队中", "马上开始整理"),
        "running": ("正在整理", "可以先看逐字稿"),
        "ready": ("笔记已就绪", ""),
        "partial": ("部分完成", "有些内容没整理出来"),
        "failed": ("没能生成", "录音和逐字稿都还在，可以重试"),
        "stale": ("内容有更新", "重新生成一下会更好"),
    }.get(str(state or ""), ("未知状态", ""))
