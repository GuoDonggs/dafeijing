# -*- coding: utf-8 -*-
"""离线意图路由：没有 API Key 也能用的兜底大脑。

它不是「退化版」——常用指令（打开应用、调音量、看磁盘、截图、锁屏…）
本来就是固定句式，正则比大模型更快、更稳、还完全离线。
正则接不住的自由表达，才交给 LLM。
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["route", "is_exit", "help_text", "chitchat"]

_FILLER = re.compile(r"^(帮我|请|麻烦|给我|你|先|快|现在|那个|我想|我要|能不能|可以|帮我把)+")
# 只剥掉真正没有语义的客气尾巴；「谢谢」是内容词，剥了就没法回「不客气」了
_POLITE = re.compile(r"(一下|好吗|吧|please)$")

_CHITCHAT = (
    (re.compile(r"^(你好|您好|哈喽|嗨|在吗|早上好|晚上好|下午好)"), "我在，有什么可以帮你的？"),
    (re.compile(r"你(叫|是)(什么|谁)|你叫什么名字|你是谁"), "我是你的电脑语音助手，喊我名字就能使唤我。"),
    (re.compile(r"你(能|会|可以)(做|干)(什么|啥)|有什么功能|帮助|怎么用"), "我能打开应用、搜东西、调音量、截图、查电脑状态，也能执行命令。说一声就行。"),
    (re.compile(r"(谢谢|多谢|感谢)"), "不客气。"),
    (re.compile(r"(真棒|厉害|聪明|乖)"), "过奖了。"),
    (re.compile(r"^(没事|算了|不用了|取消)"), "好的。"),
)

_DOMAIN = re.compile(r"([\w-]+\.)+(com|cn|net|org|io|ai|top|xyz|me|dev|app)(/\S*)?")

#: 全角数字「３０」也要认（用户是"说"出来的，ASR 有时给全角）
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
#: 输出设备相关的说法
_DEVICE_TALK = re.compile(r"(输出设备|播放设备|扬声器|音响|耳机|声音从)")
_SWITCH_TALK = re.compile(r"(切换|换到|换成|换去|切到|改用)")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_number(text: str) -> int | None:
    """把「三十」「四十」「九十五」这类说法读成数字（只处理 0-100）。

    ASR 经常给中文数字 —— 「音量调到三十」如果不认，就会退成"调大一点"，
    用户得看着屏幕再喊一遍。
    """
    # 「百分之五十」= 50：先把"百分之"去掉，否则里面的"百"会被当成一百
    raw = re.sub(r"百分之", "", str(text or ""))
    found = re.search(r"[零〇一二两三四五六七八九十百]{1,4}", raw)
    if not found:
        return None
    # 「一点」「一些」「一下」里的"一"不是数量：后面跟着这些字就不算数字，
    # 否则"声音大一点"会被读成"音量设成 1%"
    if raw[found.end():found.end() + 1] in ("点", "些", "下", "会", "会儿"):
        return None
    body = found.group(0)
    if "百" in body:
        head = body.split("百")[0]
        rest = body.split("百")[1]
        total = (_CN_DIGITS.get(head, 1) if head else 1) * 100
        if rest:
            total += _chinese_number(rest) or 0
        return total if total <= 100 else None
    if "十" in body:
        head, _, tail = body.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    total = 0
    for ch in body:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total


def _clean(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"[。！？!?，,、\s]+$", "", value)
    value = _POLITE.sub("", value)
    return value.strip()


def _strip_filler(text: str) -> str:
    previous = None
    value = text
    while previous != value:
        previous = value
        value = _FILLER.sub("", value).strip()
    return value


def is_exit(text: str, words: list[str]) -> bool:
    value = _clean(text)
    return any(word and word in value for word in words)


def chitchat(text: str) -> str | None:
    value = _clean(text)
    for pattern, reply in _CHITCHAT:
        if pattern.search(value):
            return reply
    return None


def help_text() -> str:
    return (
        "试试说：打开记事本、帮我搜一下明天的天气、音量调大一点、"
        "截个图、看看 C 盘还剩多少空间、锁屏、记住我的车停在 B2 层。"
    )


def _route_skill(core: str, value: str) -> tuple[str, dict[str, Any]] | None:
    """匹配自定义技能声明的触发词。

    没有 API Key 时，这是用户自己加的技能唯一能被说出来的入口，
    所以触发词要比通用的「打开/搜索」规则优先。
    """
    from . import tools as tools_mod

    def flat(text: str) -> str:
        """忽略大小写和空格再比对：「我的IP」「我的 ip」「我的 Ip」都算同一个说法。"""
        return re.sub(r"\s+", "", str(text)).lower()

    tools_mod.autoload_skills()
    haystack = flat(value)
    for tool in tools_mod.REGISTRY.values():
        if not tool.triggers:
            continue
        for phrase, preset in tool.triggers:
            if phrase and flat(phrase) in haystack:
                return tool.name, dict(preset)
    # 兜底：直接念出了技能的名字或标题
    for tool in tools_mod.REGISTRY.values():
        if tool.source == "builtin":
            continue
        names = [tool.name] + ([tool.title] if tool.title else [])
        for name in names:
            if name and len(name) >= 2 and flat(name) in haystack:
                return tool.name, {}
    return None


def route(text: str) -> tuple[str, dict[str, Any]] | None:
    """把一句话映射成 (工具名, 参数)。接不住返回 None。"""
    value = _clean(text)
    if not value:
        return None
    core = _strip_filler(value)

    # ── 退出 / 闲聊 ────────────────────────────────────────────────
    if re.search(r"(关机|关闭电脑)", value):
        return "power", {"action": "shutdown"}
    if re.search(r"(重启|重新启动)电脑|重启电脑", value):
        return "power", {"action": "restart"}
    if re.search(r"(睡眠|休眠)", value) and "电脑" in value or value in ("睡眠", "休眠"):
        return "power", {"action": "sleep"}

    # ── 时间 / 状态 ────────────────────────────────────────────────
    if re.search(r"(几点|时间|日期|几号|星期几|今天星期)", value):
        return "get_time", {}
    disk = re.search(r"([A-Za-z])\s*盘", value)
    if re.search(r"(磁盘|硬盘|空间|内存|cpu|处理器|电量|电池|开机多久|运行了多久)", value, re.I):
        kind = "全部"
        if disk:
            kind = disk.group(1).upper() + "盘"
        elif re.search(r"内存", value):
            kind = "内存"
        elif re.search(r"(cpu|处理器)", value, re.I):
            kind = "cpu"
        elif re.search(r"(电量|电池)", value):
            kind = "电池"
        return "system_info", {"kind": kind}

    # ── 音量 / 输出设备 / 媒体 ─────────────────────────────────────
    # 输出设备那条要排在音量前面：**「换成耳机」里也有"声音"的意思**，
    # 被音量那条抢走就变成调音量了。名字本身（耳机/音响/扬声器）不从目标里剥掉 ——
    # 它就是用户要的那台设备。
    if _DEVICE_TALK.search(value) or _SWITCH_TALK.search(value):
        if re.search(r"(有哪些|列一下|列出|看看|是什么|哪个|几个|什么设备)", value):
            return "output_device", {"action": "list"}
        target = value
        for word in ("请", "帮我", "麻烦", "把", "我的", "一下", "声音", "音频",
                     "输出设备", "播放设备", "输出", "设备", "切换", "换到", "换成",
                     "换去", "切到", "改用", "到", "成", "用", "的"):
            target = target.replace(word, "")
        target = target.strip(" 的。")
        if target:
            return "output_device", {"action": "switch", "name": target}
        return "output_device", {"action": "list"}
    # 静音相关先判：它不一定带"音量/声音"两个字（「静音」「别静音」）
    if re.search(r"(静音|别出声|别响了|别发声|别响)", value):
        if re.search(r"(取消|解除|恢复|不要|别)", value):
            return "output_volume", {"action": "unmute"}
        return "output_volume", {"action": "mute"}
    # "音量调到 30" / "音量 30" / "调到百分之三十" → 绝对值
    if re.search(r"音量|声音|喇叭", value):
        number = re.search(r"([0-9０-９]{1,3})", value)
        percent = int(str(number.group(1)).translate(_FULLWIDTH)) if number else None
        if percent is None:
            percent = _chinese_number(value)
        if percent is not None and 0 <= percent <= 100:
            return "output_volume", {"action": "set", "percent": percent}
        if re.search(r"(取消静音|恢复声音|开声)", value):
            return "output_volume", {"action": "unmute"}
        if re.search(r"(大|高|上|加|升|响|调)", value):
            return "output_volume", {"action": "up"}
        if re.search(r"(小|低|下|减|降|轻)", value):
            return "output_volume", {"action": "down"}
        return "output_volume", {"action": "get"}
    if re.search(r"(下一首|下一个|切歌|换首歌)", value):
        return "media_control", {"action": "next"}
    if re.search(r"(上一首|上一个|退回)", value):
        return "media_control", {"action": "prev"}
    if re.search(r"(暂停|继续播放|播放音乐|停止播放|停下音乐|别放了)", value):
        return "media_control", {"action": "play_pause"}

    # ── 屏幕 ───────────────────────────────────────────────────────
    if re.search(r"(截图|截屏|屏幕拍下来|屏幕截)", value):
        return "screenshot", {}
    if re.search(r"(锁屏|锁定屏幕|锁一下)", value):
        return "lock_screen", {}
    if re.search(r"(显示桌面|最小化所有|回到桌面)", value):
        return "window", {"action": "desktop"}
    if re.search(r"(切换窗口|换个窗口)", value):
        return "window", {"action": "switch"}
    if re.search(r"(关闭窗口|关掉这个窗口)", value):
        return "window", {"action": "close"}

    # ── 剪贴板 ─────────────────────────────────────────────────────
    if re.search(r"(剪贴板|复制了什么|粘贴板)", value):
        return "clipboard", {"action": "get"}

    # ── 记忆 ───────────────────────────────────────────────────────
    remember = re.search(r"^(记住|记一下|帮我记)(.+)", core)
    if remember:
        return "remember", {"text": remember.group(2).strip()}
    if re.search(r"(我之前|我让你|记不记得|回忆|还记得)", value):
        return "recall", {"query": ""}

    # ── 自定义技能（触发词优先于下面的通用「打开/搜索」规则）────────
    skill_hit = _route_skill(core, value)
    if skill_hit is not None:
        return skill_hit

    # ── 打开 / 搜索 ────────────────────────────────────────────────
    search = re.search(r"(搜索|搜一下|搜个|查一下|查询|百度|谷歌)(.+)", core)
    if search:
        # 不能用 str.strip(" 一下个")：它是**按字符集**剥的，
        # "搜索一下个税" 会被剥成 "税"、"打开个人所得税" 会被剥成 "人所得税"。
        # 只剥一次前缀词。
        return "web_search", {"query": re.sub(r"^[\s一了下个吧]+", "", search.group(2)).strip()}
    url = _DOMAIN.search(value)
    if url and re.search(r"(打开|访问|上)", value):
        return "open_url", {"url": url.group(0)}
    opened = re.search(r"(打开|启动|运行|开一下|开启)(.+)", core)
    if opened:
        target = re.sub(r"^[\s一了下个吧]+", "", opened.group(2)).strip()
        if _DOMAIN.search(target):
            return "open_url", {"url": target}
        return "open_app", {"name": target}

    # ── 文件 ───────────────────────────────────────────────────────
    listing = re.search(r"(看看|列出|显示|打开)?(桌面|下载|文档|图片|音乐|视频|[A-Za-z]盘)(里|中)?(的)?(文件|文件夹|有什么|内容)", value)
    if listing:
        return "list_files", {"path": listing.group(2)}
    finding = re.search(r"(找|搜索|查)(一下)?(叫|名为|名字是)?(.+?)(的文件|文件)", value)
    if finding:
        return "search_files", {"name": finding.group(4).strip('"「」')}
    reading = re.search(r"(读一下|念一下|看看)(.+?)(的内容|写了什么)", value)
    if reading:
        return "read_file", {"path": reading.group(2).strip()}

    # ── 输入 ───────────────────────────────────────────────────────
    typing = re.search(r"^(输入|打上|打出来|打字)(.+)", core)
    if typing:
        return "type_text", {"text": typing.group(2).strip()}
    return None
