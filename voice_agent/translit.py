# -*- coding: utf-8 -*-
"""英文单词音译：中文 TTS 念不出拉丁字母，就让模型把它写成汉字。

为什么需要它：vits-zh-ll 是纯中文模型，lexicon 里没有任何拉丁词。
textutil.spell_letters 只处理了**单个字母**和 2~4 个字母的缩写
（CPU → 西皮优），于是像 deepseek、hello、ASCII 这种词会被当成 OOV
直接丢掉 —— 用户听到的就是"这句话里少了一截"。

三层兜底，从快到慢：

1. **内置表**：最常见的那些词（api / cpu / python / windows / deepseek…）
   写死在代码里，零延迟、离线可用；
2. **学习缓存**（build/translit.json）：问过一次模型就永久记住，
   第二次遇到同一个词不再联网；
3. **问模型**：剩下的词攒一批让 LLM 给出汉字读音（"deepseek → 迪普西克"），
   超时或失败就退回"逐字母念"—— 念得生硬，但总比整段消失强。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable

__all__ = ["apply", "lookup", "learn", "cache_path", "known_words", "describe"]

#: 最常见的词直接写死：这些词几乎每天都会出现在回答里，
#: 不该为了它们每次都去问一遍模型。
BUILTIN: dict[str, str] = {
    # 技术缩写（念字母）
    "api": "诶皮爱", "cpu": "西皮优", "gpu": "记皮优", "ai": "诶爱",
    "ok": "欧开", "id": "爱滴", "ip": "爱皮", "ui": "优爱", "os": "欧艾斯",
    "pc": "皮西", "usb": "优艾斯必", "url": "优阿尔艾勒", "http": "艾尺提提皮",
    "https": "艾尺提提皮艾斯", "sql": "艾斯扣艾勒", "json": "杰森",
    "yaml": "亚木勒", "html": "艾尺提艾姆艾勒", "css": "西艾斯艾斯",
    "pdf": "皮滴艾弗", "exe": "伊艾克斯伊", "dll": "滴艾勒艾勒",
    "ascii": "阿斯克", "utf": "优提艾弗", "gb": "记必", "mb": "艾姆必",
    "kb": "开必", "tb": "提必", "wifi": "歪fai", "app": "诶皮皮",
    "gpt": "记皮提", "llm": "艾勒艾勒艾姆", "tts": "提提艾斯",
    "asr": "诶艾斯阿尔", "vad": "维诶滴", "kws": "开达不溜艾斯",
    # 常见词（按读音写汉字）
    "python": "派森", "windows": "温豆斯", "linux": "利纳克斯",
    "mac": "麦克", "google": "谷歌", "chrome": "克龙", "edge": "艾吉",
    "github": "给特哈布", "deepseek": "迪普西克", "openai": "欧盆诶爱",
    "chatgpt": "柴特记皮提", "excel": "伊克赛尔", "word": "沃德",
    "ppt": "皮皮提", "wechat": "微信", "qq": "扣扣", "vs": "威艾斯",
    "code": "扣德", "codes": "扣德", "hello": "哈喽", "hi": "嗨",
    "world": "沃尔德", "okay": "欧开", "yes": "耶斯", "no": "弄",
    "sorry": "搜瑞", "thanks": "三克斯", "error": "艾若", "fail": "费欧",
    "success": "塞克塞斯", "true": "处", "false": "佛斯", "null": "那欧",
    "test": "泰斯特", "demo": "戴莫", "cache": "开什", "token": "透肯",
    "prompt": "普龙普特", "agent": "诶真特", "model": "模型",
    "tokenizer": "透肯奈泽", "server": "瑟沃", "client": "克莱恩特",
    "user": "优泽", "admin": "艾德敏", "root": "入特", "path": "帕斯",
}

#: 出现这些形态就不用问模型，直接逐字母念（缩写、全大写、没有元音的词）
_ACRONYM = re.compile(r"^[A-Z0-9]{1,6}$")
_WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]{0,40}")
_VOWELS = set("aeiouyAEIOUY")

_CACHE: dict[str, str] = {}
_LOCK = threading.Lock()
_LOADED = False


def cache_path() -> Path:
    import os

    base = os.environ.get("VOICE_AGENT_BUILD_DIR")
    root = Path(base) if base else Path(__file__).resolve().parent.parent / "build"
    return root / "translit.json"


def _load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    path = cache_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        with _LOCK:
            for key, value in data.items():
                if isinstance(value, str) and value.strip():
                    _CACHE[str(key).lower()] = value.strip()


def _save() -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            data = dict(_CACHE)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass   # 存不下就下次再问一遍，不该影响说话


def known_words() -> dict[str, str]:
    """内置表 + 学习缓存合起来的一份快照（界面/测试用）。"""
    _load()
    with _LOCK:
        merged = dict(BUILTIN)
        merged.update(_CACHE)
    return merged


def lookup(word: str) -> str:
    """这个词怎么念；查不到返回空串。"""
    key = str(word or "").strip().lower()
    if not key:
        return ""
    _load()
    with _LOCK:
        found = _CACHE.get(key) or BUILTIN.get(key)
    return found or ""


def _spell(word: str) -> str:
    """逐字母念。文本清洗那边已经有一份字母读音表，直接复用。"""
    from . import textutil  # noqa: PLC0415 - 避免模块级循环导入

    return "".join(textutil._LETTER_READING.get(ch.lower(), ch) for ch in word)


def _looks_like_word(word: str) -> bool:
    """像"一个英文单词"而不是缩写/型号：有小写字母、够长、含元音。"""
    if len(word) < 3 or _ACRONYM.match(word):
        return False
    if not any(ch.islower() for ch in word):
        return False
    return any(ch in _VOWELS for ch in word)


def learn(words: list[str], client: Any, log: Callable[[str], None] | None = None,
          timeout_note: bool = True) -> dict[str, str]:
    """问模型：这些英文词按中文读音该怎么写。返回学到的一批。"""
    pending = []
    for word in words:
        key = str(word or "").strip().lower()
        if key and key not in pending and not lookup(key) and _looks_like_word(key):
            pending.append(key)
    if not pending or client is None:
        return {}
    if log is not None:
        log("[tts] 问模型要这几个词的中文读音：" + "、".join(pending[:8]))
    prompt = (
        "把下面的英文词按**中文读音**写成汉字，让中文语音合成能念出来。"
        "要求：只写汉字（可以用数字和常用音译字），不要音标、不要英文、不要解释。"
        "人名和品牌名按常见中文译名，没有通用译名就按读音写。\n"
        "只输出一个 JSON 对象，键是原词（小写），值是汉字读音。\n"
        + "\n".join(pending)
    )
    try:
        message = client.chat([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001 - 音译失败不能影响说话
        if log is not None:
            note = "（超时）" if "time" in str(exc).lower() and timeout_note else ""
            log("[tts] 音译没成功" + note + "：" + str(exc)[:80])
        return {}
    learned = _parse(message.get("content") or "")
    if learned:
        with _LOCK:
            _CACHE.update(learned)
        _save()
    return learned


def _parse(text: str) -> dict[str, str]:
    """从模型回复里抠出 JSON；抠不出来就按行解析 "word: 汉字"。"""
    raw = str(text or "").strip()
    if not raw:
        return {}
    start, end = raw.find("{"), raw.rfind("}")
    data: Any = None
    if 0 <= start < end:
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            data = None
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            word = str(key).strip().lower()
            reading = str(value or "").strip()
            if word and reading and _CJK_ONLY.search(reading):
                out[word] = reading
        return out
    for line in raw.splitlines():
        parts = re.split(r"[:：\-–>]+", line, maxsplit=1)
        if len(parts) != 2:
            continue
        word = parts[0].strip().strip('"\' ,').lower()
        reading = parts[1].strip().strip('"\' ,')
        if word and reading and _CJK_ONLY.search(reading):
            out[word] = reading
    return out


_CJK_ONLY = re.compile(r"[\u4e00-\u9fff]")


def apply(text: str, client: Any = None, log: Callable[[str], None] | None = None,
          learn_limit: int = 12) -> str:
    """把文本里的英文词换成能念出来的中文。

    查不到的：像单词的问模型（有缓存），像缩写的逐字母念。
    """
    raw = str(text or "")
    if not raw:
        return ""
    words = _WORD.findall(raw)
    if not words:
        return raw
    _load()
    mapping: dict[str, str] = {}
    unknown: list[str] = []
    for word in words:
        key = word.lower()
        if key in mapping:
            continue
        found = lookup(key)
        if found:
            mapping[key] = found
            continue
        if _looks_like_word(word):
            unknown.append(word)
        else:
            mapping[key] = _spell(word)      # 缩写：逐字母
    if unknown and client is not None and learn_limit > 0:
        learned = learn(unknown[:learn_limit], client, log=log)
        mapping.update(learned)
    for word in unknown:
        mapping.setdefault(word.lower(), lookup(word.lower()) or _spell(word))

    def _replace(match: re.Match) -> str:
        original = match.group(0)
        return mapping.get(original.lower(), original)

    return _WORD.sub(_replace, raw)


def describe() -> str:
    """一句话说明现在认识多少词（给 doctor / 界面用）。"""
    _load()
    with _LOCK:
        learned = len(_CACHE)
    return "内置 " + str(len(BUILTIN)) + " 个，学过 " + str(learned) + " 个"
