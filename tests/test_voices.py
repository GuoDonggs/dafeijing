# -*- coding: utf-8 -*-
"""音色表测试：不需要模型、不需要麦克风。

这一组盯的是「配置写了什么」和「耳朵里听到什么」之间那个翻译层。
起因是一个真事：配置里写 tts.engine: kokoro + speaker_id: 0，
而 Kokoro 的 0 号是英文音色 af_maple —— 拿它念中文，出来又闷又粗还带电流声。
所以这里把几条契约钉死。

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
    print("=== 音色表结构 ===")
    check("Kokoro 有 103 个音色", len(V.KOKORO_VOICES) == 103, str(len(V.KOKORO_VOICES)))
    check("0~2 号是英文音色",
          all(not V.is_chinese("kokoro", i) for i in (0, 1, 2)),
          "、".join(V.name("kokoro", i) for i in range(3)))
    check("3~102 号是中文音色",
          all(V.is_chinese("kokoro", i) for i in range(3, 103)))
    check("3~57 是女声、58~102 是男声",
          V.is_female("kokoro", 3) and V.is_female("kokoro", 57)
          and not V.is_female("kokoro", 58) and not V.is_female("kokoro", 102))
    check("默认音色是中文音色", V.is_chinese("kokoro", V.KOKORO_DEFAULT),
          str(V.KOKORO_DEFAULT) + " " + V.name("kokoro", V.KOKORO_DEFAULT))

    print()
    print("=== 英文音色必须被换掉（这次问题的根）===")
    for sid in (0, 1, 2):
        got, note = V.sanitize("kokoro", sid, 103)
        check("Kokoro " + str(sid) + " 号换成中文音色", V.is_chinese("kokoro", got) and bool(note),
              str(sid) + " -> " + str(got) + " " + V.name("kokoro", got))
    got, note = V.sanitize("kokoro", 5, 103)
    check("已经是中文音色就不动它", got == 5 and not note, str(got))
    got, note = V.sanitize("kokoro", 999, 103)
    check("越界下标夹回范围内", 0 <= got < 103 and bool(note), str(got))
    got, note = V.sanitize("vits", 0, 5)
    check("VITS 的音色不受影响", got == 0 and not note)

    print()
    print("=== 按名字 / 编号 / 序号选 ===")
    cases = [
        ("kokoro", "zf_003", 5),
        # 界面上显示的就是「zf_003 · 女声」这种标签，直接粘回来也得认
        ("kokoro", "zf_003 · 女声", 5),
        ("kokoro", "  zf_003  ", 5),
        ("kokoro", "zm_009", 58),
        ("kokoro", "5", 5),
        ("kokoro", "女声1", 3),
        ("kokoro", "男声1", 58),
        ("vits", "suyingxue", 0),
        ("vits", "bazong", 4),
        ("vits", "2", 2),
        ("vits", "不认识", None),
    ]
    for engine, want, expect in cases:
        got = V.resolve(engine, want)
        check("resolve(" + engine + ", " + want + ")", got == expect,
              "得到 " + str(got))

    print()
    print("=== 界面下拉框里只有中文音色 ===")
    kokoro_rows = V.catalog("kokoro")
    check("Kokoro 列表里没有英文音色",
          all(sid >= V.KOKORO_CHINESE_FIRST for sid, _ in kokoro_rows),
          str(len(kokoro_rows)) + " 项")
    female = V.catalog("kokoro", male=False)
    male = V.catalog("kokoro", male=True)
    check("男女分列且加起来是全部", len(female) + len(male) == len(kokoro_rows),
          str(len(female)) + " + " + str(len(male)))
    check("VITS 列表是 5 个人", len(V.catalog("vits", 5)) == 5)

    print()
    print("=== VITS 的 5 个角色音有名字 ===")
    names = [V.name("vits", i) for i in range(5)]
    check("名字来自模型自带的 speakers 字段",
          names == ["suyingxue", "gunian", "fushiyu", "bingjiao", "bazong"],
          "、".join(names))
    check("标签里带性别和音高", "女声" in V.label("vits", 0) and "Hz" in V.label("vits", 0),
          V.label("vits", 0))

    print()
    print("=== styles 不许把引擎解析好的音色盖回去 ===")
    from voice_agent.config import TtsCfg

    cfg = TtsCfg(speaker_id=7, speed=1.0, styles={"reply": {"speed": 1.1}})
    style = cfg.style("reply")
    check("没写 speaker_id 的语气不带 speaker_id", "speaker_id" not in style,
          str(style))
    check("语速仍然按语气覆盖", style["speed"] == 1.1)
    cfg2 = TtsCfg(speaker_id=7, styles={"reply": {"speaker_id": 3}})
    check("显式写了 speaker_id 就照办", cfg2.style("reply").get("speaker_id") == 3)

    print()
    print("=== 配置里写 voice 名字能读出来 ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.yaml"
        path.write_text("tts:\n  engine: kokoro\n  voice: zf_003\n", encoding="utf-8")
        loaded = Config.load(path)
        check("tts.voice 解析正确", loaded.tts.voice == "zf_003", loaded.tts.voice)
        check("音色能被解析成下标", V.resolve("kokoro", loaded.tts.voice) == 5)
        path.write_text("tts:\n  engine: kokoro\n  speaker_id: 0\n", encoding="utf-8")
        loaded = Config.load(path)
        got, note = V.sanitize("kokoro", loaded.tts.speaker_id, 103)
        check("只写 speaker_id: 0 也会被纠正过来", V.is_chinese("kokoro", got), V.name("kokoro", got))

    print()
    if FAILED:
        print("失败 " + str(len(FAILED)) + " 项：" + "、".join(FAILED))
        return 1
    print("音色测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
