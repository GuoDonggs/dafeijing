# -*- coding: utf-8 -*-
"""音色表测试：不需要模型、不需要显卡。

这一组盯的是「配置写了什么」和「耳朵里听到什么」之间那个翻译层。
两个引擎的"音色"含义不一样：vits 是 5 个固定角色音，ChatTTS 是一个随机种子。
这里把几条契约钉死。

运行：python tests/test_voices.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    if FAILED:
        print("失败 " + str(len(FAILED)) + " 项：" + "、".join(FAILED))
        return 1
    print("音色测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
