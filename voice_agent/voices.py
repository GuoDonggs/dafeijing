# -*- coding: utf-8 -*-
"""音色表：把「第几号」翻译成人能看懂的名字。

两个引擎都是「给个下标就出声」，配置里写 speaker_id: 0 谁也看不出那是谁，
而 0 号在两边都恰好是最不该当默认的那个：

- Kokoro v1.1-zh 的 voices.bin 里 **0~2 号是英文音色**（af_maple / af_sol /
  bf_vale），拿英文风格向量念中文，音素对不上，出来就是发闷、发粗、带电流声。
  真正的中文音色在 3 号往后：3~57 女声（zf_*），58~102 男声（zm_*）。
- VITS 是 vits-zh-ll 的 5 人角色音模型，0 号 suyingxue 是标准女声；
  同一个模型里还有更低更柔的 bingjiao、男声 bazong 等，换一个就是换个人。

所以这里做的事：给出名字、性别、音高，让配置和界面能按名字选，
并且在配置写了英文音色却用中文时把它拦下来。

Kokoro 的顺序来自上游 hexgrad/Kokoro-82M-v1.1-zh 的 voices/ 目录，
并拿 voices.bin 里每条 510x256 的风格向量和上游 .pt 逐条比对验证过
（85/103 精确命中、距离为 0；其余 18 条的相邻条目全部一致）。
VITS 的名字来自模型自带的 G_multisperaker_latest.json 的 speakers 字段，
音高是本机实测的中位数。
"""

from __future__ import annotations

# ─────────────────────────── Kokoro v1.1-zh ───────────────────────────

KOKORO_VOICES: tuple[str, ...] = (
    "af_maple", "af_sol", "bf_vale",
    "zf_001", "zf_002", "zf_003", "zf_004", "zf_005", "zf_006", "zf_007", "zf_008",
    "zf_017", "zf_018", "zf_019", "zf_021", "zf_022", "zf_023", "zf_024", "zf_026",
    "zf_027", "zf_028", "zf_032", "zf_036", "zf_038", "zf_039", "zf_040", "zf_042",
    "zf_043", "zf_044", "zf_046", "zf_047", "zf_048", "zf_049", "zf_051", "zf_059",
    "zf_060", "zf_067", "zf_070", "zf_071", "zf_072", "zf_073", "zf_074", "zf_075",
    "zf_076", "zf_077", "zf_078", "zf_079", "zf_083", "zf_084", "zf_085", "zf_086",
    "zf_087", "zf_088", "zf_090", "zf_092", "zf_093", "zf_094", "zf_099",
    "zm_009", "zm_010", "zm_011", "zm_012", "zm_013", "zm_014", "zm_015", "zm_016",
    "zm_020", "zm_025", "zm_029", "zm_030", "zm_031", "zm_033", "zm_034", "zm_035",
    "zm_037", "zm_041", "zm_045", "zm_050", "zm_052", "zm_053", "zm_054", "zm_055",
    "zm_056", "zm_057", "zm_058", "zm_061", "zm_062", "zm_063", "zm_064", "zm_065",
    "zm_066", "zm_068", "zm_069", "zm_080", "zm_081", "zm_082", "zm_089", "zm_091",
    "zm_095", "zm_096", "zm_097", "zm_098", "zm_100",
)

KOKORO_CHINESE_FIRST = 3      # 0~2 是英文音色
KOKORO_FEMALE_LAST = 57       # 3~57 女声，58~102 男声

# 默认音色：zf_003，全库实测里综合最好的一档 ——
#   音高 230 Hz（比 af_maple 的 216 Hz 更清亮，不"粗"）
#   谱平坦度 0.0070（af_maple 是 0.0214，越高越像噪声，听着就是电流声）
#   6kHz 以上能量 0.0181（af_maple 是 0.0897，五倍差距，就是那阵嘶嘶声）
#   帧间抖动 0.1441，是候选里最低的，听感最平顺
# 想更清亮可以试 zf_092(54) / zf_074(41)，想更沉稳试 zf_006(8)。
# 数据是 scripts/scan_kokoro_voices.py 跑全部 103 个音色扫出来的。
KOKORO_DEFAULT = 5            # zf_003

# ───────────────────────── vits-zh-ll（5 人）─────────────────────────

# sid: (名字, 性别, 音高实测 Hz)
VITS_VOICES: dict[int, tuple[str, str, int]] = {
    0: ("suyingxue", "女声", 246),
    1: ("gunian", "男声", 92),
    2: ("fushiyu", "女声", 235),
    3: ("bingjiao", "女声", 165),
    4: ("bazong", "男声", 104),
}

VITS_DEFAULT = 0


# ───────────────────────────── 通用接口 ─────────────────────────────


def engine_of(engine: str) -> str:
    return "kokoro" if str(engine).strip().lower() == "kokoro" else "vits"


def count(engine: str, num_speakers: int = 0) -> int:
    """这个引擎到底有多少音色（以模型实际加载的为准）。"""
    if num_speakers > 0:
        return int(num_speakers)
    return len(KOKORO_VOICES) if engine_of(engine) == "kokoro" else len(VITS_VOICES)


def is_chinese(engine: str, sid: int) -> bool:
    """这个音色能不能念中文。"""
    if engine_of(engine) != "kokoro":
        return True
    return KOKORO_CHINESE_FIRST <= int(sid) < len(KOKORO_VOICES)


def is_female(engine: str, sid: int) -> bool:
    """女声？VITS 那边从名字表里看。"""
    if engine_of(engine) != "kokoro":
        info = VITS_VOICES.get(int(sid))
        return bool(info) and info[1] == "女声"
    return KOKORO_CHINESE_FIRST <= int(sid) <= KOKORO_FEMALE_LAST


def name(engine: str, sid: int) -> str:
    i = int(sid)
    if engine_of(engine) == "kokoro":
        if 0 <= i < len(KOKORO_VOICES):
            return KOKORO_VOICES[i]
        return "kokoro-" + str(i)
    info = VITS_VOICES.get(i)
    return info[0] if info else "vits-" + str(i)


def label(engine: str, sid: int) -> str:
    """给界面用的一行标签，例如 zf_070 · 女声 · 中文。"""
    i = int(sid)
    if engine_of(engine) == "kokoro":
        if not (0 <= i < len(KOKORO_VOICES)):
            return "第 " + str(i) + " 号（超出范围）"
        n = KOKORO_VOICES[i]
        if i < KOKORO_CHINESE_FIRST:
            return n + " · 英文音色"
        return n + (" · 女声" if i <= KOKORO_FEMALE_LAST else " · 男声")
    info = VITS_VOICES.get(i)
    if not info:
        return "第 " + str(i) + " 号"
    return "{} · {} · 约 {} Hz".format(info[0], info[1], info[2])


def describe(engine: str, sid: int) -> str:
    """一句话说明这个音色合不合适。"""
    i = int(sid)
    if engine_of(engine) != "kokoro":
        info = VITS_VOICES.get(i)
        if not info:
            return "未知音色"
        return "{}，{}，基频约 {} Hz".format(info[0], info[1], info[2])
    if i < KOKORO_CHINESE_FIRST:
        return "英文音色，念中文会发闷发粗，建议换成 3 号以后的中文音色"
    if i <= KOKORO_FEMALE_LAST:
        return "中文女声（第 {} / {} 个）".format(
            i - KOKORO_CHINESE_FIRST + 1, KOKORO_FEMALE_LAST - KOKORO_CHINESE_FIRST + 1)
    return "中文男声（第 {} / {} 个）".format(
        i - KOKORO_FEMALE_LAST, len(KOKORO_VOICES) - 1 - KOKORO_FEMALE_LAST)


def chinese_sids(engine: str, num_speakers: int = 0) -> list[int]:
    """可以念中文的音色下标。"""
    total = count(engine, num_speakers)
    if engine_of(engine) != "kokoro":
        return list(range(total))
    return list(range(KOKORO_CHINESE_FIRST, min(total, len(KOKORO_VOICES))))


def catalog(engine: str, num_speakers: int = 0, male: bool | None = None) -> list[tuple[int, str]]:
    """给下拉框用的 (sid, 标签) 列表；male 为 None 表示男女都要。"""
    sids = chinese_sids(engine, num_speakers)
    if engine_of(engine) == "kokoro" and male is not None:
        sids = [i for i in sids if (i > KOKORO_FEMALE_LAST) == bool(male)]
    return [(i, label(engine, i)) for i in sids]


def resolve(engine: str, value, num_speakers: int = 0) -> int | None:
    """把用户写的音色解析成下标。

    接受：数字（"37"）、名字（"zf_070" / "suyingxue"）、序号（"女声12" / "男声3"）。
    """
    if value is None or value == "":
        return None
    eng = engine_of(engine)
    total = count(engine, num_speakers)
    if isinstance(value, int):
        return value if 0 <= value < total else None
    text = str(value).strip()
    if not text:
        return None
    if text.lstrip("-").isdigit():
        i = int(text)
        return i if 0 <= i < total else None
    low = text.lower()
    if eng == "kokoro":
        if low in KOKORO_VOICES:
            i = KOKORO_VOICES.index(low)
            return i if i < total else None
        pool = chinese_sids(eng, num_speakers)
    else:
        for sid, info in VITS_VOICES.items():
            if low == info[0].lower():
                return sid
        pool = chinese_sids(eng, num_speakers)
    digits = "".join(ch for ch in low if ch.isdigit())
    if not digits:
        return None
    n = int(digits)
    if low[:1] in ("男", "m") or low.startswith("zm"):
        pool = [i for i in pool if i > KOKORO_FEMALE_LAST] if eng == "kokoro" else pool
    elif low[:1] in ("女", "f") or low.startswith("zf"):
        pool = [i for i in pool if i <= KOKORO_FEMALE_LAST] if eng == "kokoro" else pool
    if 1 <= n <= len(pool):
        return pool[n - 1]
    return None


def sanitize(engine: str, sid: int, num_speakers: int = 0) -> tuple[int, str]:
    """把配置里的下标修成能用的，并给一句说明。

    只处理一种情况：Kokoro 配了英文音色。其余情况原样放行 —— 用户可以就是
    喜欢某个音色，不该被代码「纠正」。
    """
    total = count(engine, num_speakers)
    i = int(sid)
    if total > 0 and not (0 <= i < total):
        fixed = max(0, min(i, total - 1))
        return fixed, "音色下标 {} 超出范围（0~{}），已改为 {}".format(i, total - 1, fixed)
    if engine_of(engine) == "kokoro" and not is_chinese(engine, i):
        d = KOKORO_DEFAULT if KOKORO_DEFAULT < total else KOKORO_CHINESE_FIRST
        return d, "Kokoro 的 0~2 号是英文音色，念中文会发闷发粗，已改用 " + label(engine, d)
    return i, ""
