# -*- coding: utf-8 -*-
"""音色表测试：不需要模型、不需要显卡。

这一组盯的是「配置写了什么」和「耳朵里听到什么」之间那个翻译层。
两个引擎的"音色"含义不一样：vits 是 5 个固定角色音，ChatTTS 是一个随机种子。
这里把几条契约钉死。

运行：python tests/test_voices.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时文件挪到临时目录：测试**绝不能**碰用户真实的对话记录 / 记忆 / 标记 ——
# 否则「上次聊过什么」会渗进断言（真出现过：webui 那句回复变成「跟刚才一样」）。
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name


from voice_agent import voices as V  # noqa: E402
from voice_agent.config import Config  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print("  [通过] " + name + (("  " + detail) if detail else ""))
    else:
        FAILED.append(name)
        print("  [失败] " + name + (("  " + detail) if detail else ""))


def main() -> int:
    print()
    print("=== 引擎归一化 ===")
    check("vits 是默认", V.engine_of("") == "vits" and V.engine_of("vits") == "vits")
    check("chattts 的几种写法都认",
          all(V.engine_of(x) == "chattts" for x in ("chattts", "ChatTTS", "chat_tts", "chat")))
    check("不认识的名字回落到 vits（老配置写着 kokoro 也不会炸）",
          V.engine_of("kokoro") == "vits")

    print()
    print("=== vits：5 个固定角色音 ===")
    check("一共 5 个", len(V.VITS_VOICES) == 5 and V.count("vits") == 5)
    names = [V.name("vits", i) for i in range(5)]
    check("名字来自模型自带的 speakers 字段",
          names == ["suyingxue", "gunian", "fushiyu", "bingjiao", "bazong"], "、".join(names))
    check("标签里带性别和音高",
          "女声" in V.label("vits", 0) and "Hz" in V.label("vits", 0), V.label("vits", 0))
    check("性别判断正确",
          V.is_female("vits", 0) and not V.is_female("vits", 1)
          and V.is_female("vits", 3) and not V.is_female("vits", 4))

    print()
    print("=== chattts：种子即音色 ===")
    check("默认种子在候选里", V.CHATTTS_DEFAULT in V.CHATTTS_SEEDS, str(V.CHATTTS_DEFAULT))
    check("标签写成「种子 N」", "种子" in V.label("chattts", 42), V.label("chattts", 42))
    check("种子不区分性别", V.is_female("chattts", 42))
    check("种子上限远大于候选个数", V.count("chattts") > len(V.CHATTTS_SEEDS),
          str(V.count("chattts")))
    check("下拉框给的是挑过的种子",
          len(V.catalog("chattts")) == len(V.CHATTTS_SEEDS), str(len(V.catalog("chattts"))))

    print()
    print("=== 按名字 / 编号 / 种子选 ===")
    cases = [
        ("vits", "suyingxue", 0),
        ("vits", "bazong", 4),
        ("vits", "2", 2),
        ("vits", "女声1", 0),
        ("vits", "男声1", 1),
        ("vits", "不认识", None),
        ("chattts", "seed42", 42),
        ("chattts", "42", 42),
        ("chattts", "  2024  ", 2024),
        ("chattts", "abc", None),
    ]
    for engine, want, expect in cases:
        got = V.resolve(engine, want)
        check("resolve({}, {})".format(engine, want.strip()), got == expect, "得到 " + str(got))

    print()
    print("=== 越界值要被夹回来，而且要说明 ===")
    got, note = V.sanitize("vits", 9)
    check("vits 越界夹回 4", got == 4 and bool(note), str(got))
    got, note = V.sanitize("vits", 3)
    check("vits 正常值不动", got == 3 and not note)
    got, note = V.sanitize("chattts", 99999999)
    check("chattts 种子越界夹回默认", got == V.CHATTTS_DEFAULT and bool(note), str(got))
    got, note = V.sanitize("chattts", 777)
    check("自定义种子放行", got == 777 and not note)

    print()
    print("=== styles 不许把引擎解析好的音色盖回去 ===")
    from voice_agent.config import TtsCfg

    cfg = TtsCfg(speaker_id=7, speed=1.0, styles={"reply": {"speed": 1.1}})
    style = cfg.style("reply")
    check("没写 speaker_id 的语气不带 speaker_id", "speaker_id" not in style, str(style))
    check("语速仍然按语气覆盖", style["speed"] == 1.1)
    cfg2 = TtsCfg(speaker_id=7, styles={"reply": {"speaker_id": 3}})
    check("显式写了 speaker_id 就照办", cfg2.style("reply").get("speaker_id") == 3)

    print()
    print("=== 配置里写 voice 名字 / 种子能读出来 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("tts:\n  engine: chattts\n  voice: seed42\n", encoding="utf-8")
        loaded = Config.load(path)
        check("tts.voice 解析正确", loaded.tts.voice == "seed42", loaded.tts.voice)
        check("引擎解析成 chattts", V.engine_of(loaded.tts.engine) == "chattts")
        check("种子能被解析成下标", V.resolve("chattts", loaded.tts.voice) == 42)
        path.write_text("tts:\n  engine: vits\n  voice: bazong\n", encoding="utf-8")
        loaded = Config.load(path)
        check("vits 按名字选", V.resolve("vits", loaded.tts.voice) == 4, loaded.tts.voice)

    print()
    print("=== 多音字：拼音 → 模型词典的注音符号 ===")
    from voice_agent import polyphone as P

    check("chóng → ㄔ ㄨ ㄥ ˊ",
          P.syllable_tokens("chóng") == ["ㄔ", "ㄨ", "ㄥ", "ˊ"],
          str(P.syllable_tokens("chóng")))
    check("数字调写法一样认", P.syllable_tokens("chong2") == P.syllable_tokens("chóng"))
    check("轻声不加声调数字也是 ˙", P.syllable_tokens("bu") == ["ㄅ", "ㄨ", "˙"],
          str(P.syllable_tokens("bu")))
    check("zhi 里的 i 不发音", P.syllable_tokens("zhi3") == ["ㄓ", "ˇ"],
          str(P.syllable_tokens("zhi3")))
    check("ju/qu/xu 里的 u 是 ü", P.syllable_tokens("ju4") == ["ㄐ", "ㄩ", "ˋ"],
          str(P.syllable_tokens("ju4")))
    check("ong → ㄨ ㄥ", P.syllable_tokens("dong1") == ["ㄉ", "ㄨ", "ㄥ", "ˉ"],
          str(P.syllable_tokens("dong1")))
    check("iong → ㄩ ㄥ", P.syllable_tokens("xiong2") == ["ㄒ", "ㄩ", "ㄥ", "ˊ"],
          str(P.syllable_tokens("xiong2")))
    check("er → ㄦ", P.syllable_tokens("er2") == ["ㄦ", "ˊ"], str(P.syllable_tokens("er2")))
    check("整串读音和逐字列表两种写法等价",
          P.word_tokens("chóng qìng") == P.word_tokens(["chóng", "qìng"])
          == P.word_tokens(["chóng qìng"]),
          str(P.word_tokens("chóng qìng")))
    check("认不出来的音节不硬编（返回空）", P.syllable_tokens("zzz9") == [])

    print()
    print("=== 多音字：只在「模型读错」时才写进合并词典 ===")
    fake = Path(tempfile.mkdtemp(prefix="va-lex-")) / "lexicon.txt"
    # 造一份迷你词典：重庆 读错（zhòng），银行 读对，成都 干脆没有
    fake.write_text("重庆 ㄓ ㄨ ㄥ ˋ ㄑ ㄧ ㄥ ˋ\n\n银行 ㄧ ㄣ ˊ ㄏ ㄤ ˊ\n\n",
                    encoding="utf-8")
    entries = P.meaningful_entries(fake)
    check("模型读错的词要修正（重庆）", "重庆" in entries, str(entries.get("重庆")))
    check("模型读对的词不动它（银行）", "银行" not in entries)
    check("模型没有的词补上（成都）", "成都" in entries)
    check("用户自己加的词也算（行不行）",
          "行不行" in P.meaningful_entries(fake, {"行不行": "xíng bu xíng"}))
    check("拼音写错了就不收（宁可不改，也不要写错）",
          "行不行" not in P.meaningful_entries(fake, {"行不行": "zzz"}))

    print()
    print("=== 多音字：合并词典 ===")
    out = fake.parent / "merged.txt"
    merged = P.merge_lexicon(fake, out)
    check("合并成功", merged is not None and out.is_file())
    table = P.read_lexicon(out)
    check("修正后的读音写进去了",
          table.get("重庆") == " ".join(P.word_tokens("chóng qìng")), str(table.get("重庆")))
    check("模型原本就对的词还在（银行）", table.get("银行") == "ㄧ ㄣ ˊ ㄏ ㄤ ˊ")
    check("同名的旧条目被替换掉，不会留两条",
          sum(1 for ln in out.read_text(encoding="utf-8").splitlines()
              if ln.startswith("重庆 ")) == 1)
    again = P.merge_lexicon(fake, out)
    check("重复生成内容稳定（幂等）", again is not None
          and P.read_lexicon(out) == table)
    check("合并词典用空行分隔条目（模型词典就是这个格式）",
          "\n\n" in out.read_text(encoding="utf-8"))

    print()
    print("=== 多音字：用户词表 + 缓存失效 ===")
    P.save_user_words({"测试词一": "cè shì cí yī"})
    check("写进去还能读出来", P.load_user_words() == {"测试词一": "cè shì cí yī"})
    first = P.lexicon_for(fake)
    check("给 TTS 的是合并后的词典", P.read_lexicon(first).get("测试词一") is not None,
          str(first))
    P.save_user_words({"测试词二": "cè shì cí èr"})
    second = P.lexicon_for(fake)
    check("改了词表会重新合并（缓存要失效）",
          P.read_lexicon(second).get("测试词二") is not None)
    check("旧的词不再出现在词典里", P.read_lexicon(second).get("测试词一") is None)
    check("词典读不出来时退回原词典（不抛异常）",
          P.lexicon_for(fake.parent / "不存在.txt") == fake.parent / "不存在.txt")

    print()
    print("=== 多音字：拿模型真实词典对表（有模型才跑） ===")
    cfg = Config.load()
    real = cfg.models.get("tts_lexicon")
    if real is None:
        print("  [跳过] 没装语音合成模型")
    else:
        model_table = P.read_lexicon(real)
        from pypinyin import Style, pinyin

        checked = explained = 0
        for ch, symbols in model_table.items():
            if len(ch) != 1 or not ("\u4e00" <= ch <= "\u9fff"):
                continue
            checked += 1
            readings = {s[0] for s in pinyin(ch, style=Style.TONE3, heteronym=True)}
            if any(" ".join(P.syllable_tokens(r)) == symbols for r in readings):
                explained += 1
        ratio = explained / max(1, checked)
        check("换算表能解释模型词典里 " + str(checked) + " 个字条的读音（%.1f%%）"
              % (ratio * 100), ratio > 0.99, "%.3f" % ratio)
        entries = P.meaningful_entries(real)
        check("内置词表里每个词都是「模型缺的或读错的」",
              len(entries) > 100,
              str(len(entries)) + " 个词")
        check("抽查：重庆/成都/行走 都需要修正",
              all(w in entries for w in ("重庆", "成都", "行走")))

    print()
    if FAILED:
        print("失败 " + str(len(FAILED)) + " 项：" + "、".join(FAILED))
        return 1
    print("音色测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
