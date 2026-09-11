# -*- coding: utf-8 -*-
"""文本工具：唤醒词拼音生成、朗读前文本清洗、分句、识别纠错。"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "build_keywords_file",
    "clean_for_tts",
    "split_sentences",
    "apply_corrections",
    "load_vocab",
    "pinyin_readings",
    "spell_letters",
]

# 反引号用转义写，避免与外部脚本文本冲突
_BT = "\u0060"


# ─────────────────── 唤醒词：中文 → KWS 需要的拼音音素 ───────────────────

# pypinyin 覆盖不到时的兜底（保证默认唤醒词离线也能用）
_FALLBACK_PINYIN: dict[str, tuple[str, ...]] = {
    "小": ("xiǎo",), "爱": ("ài",), "同": ("tóng",), "学": ("xué",),
    "你": ("nǐ",), "好": ("hǎo",), "嗨": ("hāi",), "嘿": ("hēi",),
    "语": ("yǔ",), "音": ("yīn",), "助": ("zhù",), "手": ("shǒu",),
    "大": ("dà",), "肥": ("féi",), "鲸": ("jīng",), "智": ("zhì",),
    "能": ("néng",), "深": ("shēn",), "度": ("dù",), "求": ("qiú",),
    "索": ("suǒ",),
}


def pinyin_readings(char: str) -> tuple[str, ...]:
    """一个汉字所有可能的带声调读音（多音字全给，交给词表去挑）。"""
    out: list[str] = []
    try:
        from pypinyin import Style, pinyin  # noqa: PLC0415

        for group in pinyin(char, style=Style.TONE, heteronym=True):
            for syllable in group:
                text = str(syllable).strip()
                # pypinyin 对非汉字原样返回，那不是读音
                if text and text != char and text not in out:
                    out.append(text)
    except Exception:  # pragma: no cover - pypinyin 缺失时走兜底表
        pass
    for syllable in _FALLBACK_PINYIN.get(char, ()):
        if syllable not in out:
            out.append(syllable)
    return tuple(out)


def load_vocab(tokens_path: str | Path) -> set[str]:
    """读 sherpa-onnx 的 tokens.txt，取第一列作为音素词表。"""
    vocab: set[str] = set()
    for line in Path(tokens_path).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            vocab.add(parts[0])
    return vocab


def _tokenize_syllable(syllable: str, vocab: set[str], max_piece: int = 8) -> list[str] | None:
    """把 xiǎo 这样的音节按词表贪心切成 x iǎo；切不动就返回 None。"""
    out: list[str] = []
    i = 0
    total = len(syllable)
    while i < total:
        for size in range(min(max_piece, total - i), 0, -1):
            piece = syllable[i : i + size]
            if piece in vocab:
                out.append(piece)
                i += size
                break
        else:
            return None
    return out


def build_keywords_file(
    keywords: list[str],
    tokens_path: str | Path,
    out_path: str | Path,
    extra_pinyin: dict[str, tuple[str, ...]] | None = None,
) -> tuple[Path, list[str]]:
    """把「大肥鲸」这类中文唤醒词写成 KWS 模型要的 keywords.txt。

    格式为每一行 拼音音素 + 空格 + @原文，例如::

        x iǎo ài t óng x ué @大肥鲸

    返回 (生成的文件路径, 问题列表)。
    """
    vocab = load_vocab(tokens_path)
    target = Path(out_path)
    lines: list[str] = []
    problems: list[str] = []

    for keyword in keywords:
        keyword = str(keyword).strip()
        if not keyword:
            continue
        pieces: list[str] = []
        failed: str | None = None
        for char in keyword:
            if char.isspace():
                continue
            candidates = (extra_pinyin or {}).get(char) or pinyin_readings(char)
            if not candidates:
                failed = "字符 " + repr(char) + " 取不到拼音，可用 wake.pinyin 手动指定读音"
                break
            chosen = None
            for syllable in candidates:
                tokens = _tokenize_syllable(syllable, vocab)
                if tokens:
                    chosen = tokens
                    break
            if chosen is None:
                failed = "字符 " + repr(char) + " 的拼音 " + repr(candidates[0]) + " 无法用模型词表切分"
                break
            pieces.extend(chosen)
        if failed:
            problems.append("唤醒词 " + repr(keyword) + " 生成失败：" + failed)
            continue
        if pieces:
            lines.append(" ".join(pieces) + " @" + keyword)

    content = ("\n".join(lines) + "\n") if lines else ""
    if content:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return target, problems


# ─────────────────── 朗读前清洗 ───────────────────

_FENCE = re.compile(_BT * 3 + r".*?" + _BT * 3, re.S)
_INLINE_CODE = re.compile(_BT + r"([^" + _BT + r"]*)" + _BT)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.、)])\s+", re.M)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", re.S)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]{4,}\|?\s*$", re.M)
_TABLE_PIPE = re.compile(r"^\s*\|(.*)\|\s*$", re.M)
_HR = re.compile(r"^\s*([-*_])\1{2,}\s*$", re.M)
_EMOJI = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U0001F000-\U0001F2FF"
    "\u2600-\u27BF"
    "\uFE0F\u200D"
    "]+"
)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_HTML = re.compile(r"</?[A-Za-z][^>]{0,80}>")
_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_BLANKS = re.compile(r"\n{2,}")
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_WS_BETWEEN_CJK = re.compile("([" + _CJK + r"])\s+(?=[" + _CJK + r"])")

# ── 英文字母 / 单位的中文读法 ───────────────────────────────────────────
# vits-zh-ll 是纯中文模型，lexicon.txt 里没有任何拉丁字母：sherpa 会把
# 「C盘」的 C 判为 OOV 直接丢掉（stderr 里能看到 Ignore OOV 'C'），
# 只念出一个「盘」。所以朗读前先把字母换成读音最接近的汉字。
_LETTER_READING = {
    "a": "诶", "b": "必", "c": "西", "d": "滴", "e": "衣", "f": "艾弗",
    "g": "记", "h": "艾尺", "i": "爱", "j": "借", "k": "开", "l": "艾勒",
    "m": "艾姆", "n": "恩", "o": "欧", "p": "皮", "q": "扣", "r": "阿尔",
    "s": "艾斯", "t": "提", "u": "优", "v": "威", "w": "达不溜", "x": "艾克斯",
    "y": "歪", "z": "贼",
}
_UNIT = re.compile(r"(?<![A-Za-z])([KkMmGgTtPp])[Bb](?![A-Za-z])")
_UNIT_READING = {"k": "开", "m": "艾姆", "g": "记", "t": "提", "p": "拍"}
_SINGLE_LETTER = re.compile(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])")
_ACRONYM = re.compile(r"(?<![A-Za-z])([A-Z]{2,4})(?![A-Za-z])")
_TIME_COLON = re.compile(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)")
_COLON = re.compile(r"[:：]")
_DRIVE_PATH = re.compile(r"(?<![A-Za-z])([A-Za-z]):[\\/]")
_PERCENT = re.compile(r"(\d(?:[\d.]*\d)?)\s*%")
_SUFFIX_UNITS = (
    (re.compile(r"(?<=\d)\s*ms\b", re.IGNORECASE), "毫秒"),
    (re.compile(r"(?<=\d)\s*s\b", re.IGNORECASE), "秒"),
    (re.compile(r"(?<=\d)\s*m\b", re.IGNORECASE), "分钟"),
    (re.compile(r"(?<=\d)\s*h\b", re.IGNORECASE), "小时"),
    (re.compile(r"(?<=\d)\s*(?:℃|°C)", re.IGNORECASE), "摄氏度"),
)


def spell_letters(text: str) -> str:
    """把符号/字母写成中文 TTS 念得出来的说法。

    C盘 → 西盘，156G → 156记，20:44 → 20点44分，12.5% → 百分之12.5。
    """
    if not text:
        return ""
    out = str(text)

    def _letter(match: re.Match) -> str:
        return _LETTER_READING.get(match.group(1).lower(), match.group(1))

    out = _DRIVE_PATH.sub(lambda m: _LETTER_READING.get(m.group(1).lower(), m.group(1)) + "盘", out)
    out = _TIME_COLON.sub(lambda m: m.group(1) + "点" + m.group(2) + "分", out)
    out = _COLON.sub("，", out)
    out = _PERCENT.sub(r"百分之\1", out)
    for pattern, reading in _SUFFIX_UNITS:
        out = pattern.sub(reading, out)
    out = _UNIT.sub(lambda m: _UNIT_READING.get(m.group(1).lower(), m.group(0)), out)
    out = _ACRONYM.sub(lambda m: "".join(_LETTER_READING.get(c.lower(), c) for c in m.group(1)), out)
    return _SINGLE_LETTER.sub(_letter, out)


def clean_minimal(text: str) -> str:
    """兜底清洗：只去掉 Markdown 记号，正文一律留着。

    什么时候用：正常清洗把整段话洗没了（例如模型只回了一个代码块，
    内容全在 ``` 里面）。这时候宁可念得糙一点，也不能一句话都不说 ——
    "回复了但没声音"是最难排查的一类问题。
    """
    out = _EMPHASIS.sub(r"\2", str(text or ""))
    out = _HEADING.sub("", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _FENCE.sub(lambda m: m.group(1) or " ", out)
    out = _WS_BETWEEN_CJK.sub(r"\1", out)
    out = _SPACES.sub(" ", out)
    out = _BLANKS.sub("\n", out).strip()
    if out and out[-1] not in "。！？…!?.;；，,、:：":
        out += "。"
    return out


def clean_for_tts(text: str) -> str:
    """把 Markdown / 代码 / 链接 / emoji 去掉，只留适合朗读的纯文本。

    LLM 的回复天然带格式，而 TTS 会把路径、URL、星号一个个念出来，
    听起来像在念代码。所以朗读前统一洗一遍。
    """
    if not text:
        return ""
    out = str(text)
    out = _FENCE.sub(" ", out)
    out = _IMAGE.sub(" ", out)
    out = _LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _URL.sub(" ", out)
    out = _TABLE_SEP.sub("\n", out)
    out = _TABLE_PIPE.sub(r"\1", out)
    out = _HR.sub(" ", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _EMPHASIS.sub(r"\2", out)
    out = _EMOJI.sub(" ", out)
    out = _QUOTE.sub("", out)
    out = _HTML.sub(" ", out)
    out = out.replace("|", "，").replace("→", "到").replace("~", "到")
    out = _WS_BETWEEN_CJK.sub(r"\1", out)
    out = _SPACES.sub(" ", out)
    out = _BLANKS.sub("\n", out).strip()
    out = spell_letters(out)
    if not out:
        return ""
    # 结尾没有标点时，模型不会收尾停顿，听起来像被掐断
    if out[-1] not in "。！？…!?.;；，,、:：":
        out += "。"
    return out


def split_sentences(text: str, max_chars: int = 60,
                   split_chars: str = "。！？；\n!?;") -> list[str]:
    """按标点切句，再把过短的句子合并，避免一句一顿的机械感。

    split_chars 决定「哪些标点算断句点」：VITS 快，可以整句合成；
    慢的引擎（约 1 倍实时，比如 ChatTTS）需要切得更碎才能在 1~2 秒内出声，
    这时会额外把逗号、顿号也算作断句点。
    """
    text = (text or "").strip()
    if not text:
        return []
    pattern = "[^" + re.escape(split_chars) + "]+[" + re.escape(split_chars) + "]?"
    parts = re.findall(pattern, text)
    out: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out and len(out[-1]) + len(part) <= max_chars:
            out[-1] = out[-1] + part
        else:
            out.append(part)
    return out


def apply_corrections(text: str, rules: list[list[str]] | None) -> str:
    """按配置里的 [正则, 替换] 修正识别结果里的同音字。"""
    out = text or ""
    for rule in rules or []:
        try:
            out = re.sub(rule[0], rule[1], out)
        except re.error:
            continue
    return out
