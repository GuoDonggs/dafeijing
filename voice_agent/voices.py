# -*- coding: utf-8 -*-
"""音色表：把「第几号」翻译成人能看懂的名字。

两个引擎的"音色"含义不一样，这里统一成同一个接口：

- **vits**（vits-zh-ll）：5 个固定的角色音，下标 0~4 就是那 5 个人。
- **chattts**：没有固定音色表 —— 它按一个**随机种子**生成说话人向量。
  所以"音色"就是一个整数种子：同一个种子永远是同一个人，换种子就换人。
  用户想固定某个音色，记下种子号即可。

Kokoro 已经移除：它的 0~2 号是英文音色（拿它念中文会发闷发粗、带电流声），
而且中文自然度一直不理想，不如把这一档换成 ChatTTS。
"""

from __future__ import annotations

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

# ───────────────────────── ChatTTS（种子即音色）─────────────────────────

# 挑过的几个种子，实测听感自然；用户也可以填任意非负整数
CHATTTS_SEEDS: tuple[int, ...] = (0, 42, 1111, 2024, 3407, 6666, 8888, 12345)
CHATTTS_DEFAULT = 42
CHATTTS_MAX_SEED = 999999


def engine_of(engine: str) -> str:
    """规范化引擎名。"""
    name = str(engine or "").strip().lower()
    if name in ("chattts", "chat_tts", "chat"):
        return "chattts"
    return "vits"


def count(engine: str, num_speakers: int = 0) -> int:
    if num_speakers > 0:
        return int(num_speakers)
    return CHATTTS_MAX_SEED if engine_of(engine) == "chattts" else len(VITS_VOICES)


def is_chinese(engine: str, sid: int) -> bool:
    """这个音色能不能念中文 —— 两个引擎都可以。"""
    return True


def is_female(engine: str, sid: int) -> bool:
    if engine_of(engine) != "vits":
        return True   # ChatTTS 的种子不分性别
    info = VITS_VOICES.get(int(sid))
    return bool(info) and info[1] == "女声"


def name(engine: str, sid: int) -> str:
    i = int(sid)
    if engine_of(engine) == "chattts":
        return "seed" + str(i)
    info = VITS_VOICES.get(i)
    return info[0] if info else "vits-" + str(i)


def label(engine: str, sid: int) -> str:
    """给界面用的一行标签。"""
    i = int(sid)
    if engine_of(engine) == "chattts":
        return "种子 " + str(i) + ("（默认）" if i == CHATTTS_DEFAULT else "")
    info = VITS_VOICES.get(i)
    if not info:
        return "第 " + str(i) + " 号"
    return "{} · {} · 约 {} Hz".format(info[0], info[1], info[2])


def describe(engine: str, sid: int) -> str:
    if engine_of(engine) == "chattts":
        return "ChatTTS 按这个种子生成说话人；同一个种子永远是同一个人"
    info = VITS_VOICES.get(int(sid))
    if not info:
        return "未知音色"
    return "{}，{}，基频约 {} Hz".format(info[0], info[1], info[2])


def chinese_sids(engine: str, num_speakers: int = 0) -> list[int]:
    total = count(engine, num_speakers)
    if engine_of(engine) == "chattts":
        return [s for s in CHATTTS_SEEDS if s < total]
    return list(range(total))


def catalog(engine: str, num_speakers: int = 0, male: bool | None = None) -> list[tuple[int, str]]:
    """给下拉框用的 (sid, 标签) 列表。"""
    sids = chinese_sids(engine, num_speakers)
    if engine_of(engine) == "vits" and male is not None:
        sids = [i for i in sids if is_female(engine, i) != bool(male)]
    return [(i, label(engine, i)) for i in sids]


def resolve(engine: str, value, num_speakers: int = 0) -> int | None:
    """把用户写的音色解析成下标。

    接受：数字（"42"）、vits 的名字（"suyingxue"）、种子写法（"seed42"）。
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
    if eng == "chattts":
        digits = "".join(ch for ch in low if ch.isdigit())
        if digits:
            i = int(digits)
            return i if 0 <= i < total else None
        return None
    for sid, info in VITS_VOICES.items():
        if low == info[0].lower():
            return sid
    digits = "".join(ch for ch in low if ch.isdigit())
    if not digits:
        return None
    n = int(digits)
    pool = chinese_sids(eng, num_speakers)
    if low[:1] in ("男", "m"):
        pool = [i for i in pool if not is_female(eng, i)]
    elif low[:1] in ("女", "f"):
        pool = [i for i in pool if is_female(eng, i)]
    if 1 <= n <= len(pool):
        return pool[n - 1]
    return None


def sanitize(engine: str, sid: int, num_speakers: int = 0) -> tuple[int, str]:
    """把配置里的下标修成能用的，并给一句说明。"""
    total = count(engine, num_speakers)
    i = int(sid)
    if engine_of(engine) == "chattts":
        if not (0 <= i <= CHATTTS_MAX_SEED):
            return CHATTTS_DEFAULT, "种子 " + str(i) + " 超出范围，已改成 " + str(CHATTTS_DEFAULT)
        return i, ""
    if total > 0 and not (0 <= i < total):
        fixed = max(0, min(i, total - 1))
        return fixed, "音色下标 {} 超出范围（0~{}），已改为 {}".format(i, total - 1, fixed)
    return i, ""
