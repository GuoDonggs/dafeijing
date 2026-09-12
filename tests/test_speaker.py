# -*- coding: utf-8 -*-
"""声纹测试：用两个不同的合成音色当"两个人"，验证只认录过的那个。

这比"我听着像不像"可靠：合成音色之间的声学差异是确定的，正好用来量
同人/异人的相似度分布，据此确认默认阈值定得合不合适。

运行：python tests/test_speaker.py
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


import numpy as np  # noqa: E402

from voice_agent import audio as audio_io  # noqa: E402
from voice_agent.config import Config  # noqa: E402
from voice_agent.speaker import Voiceprint, find_model  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


SENTENCE = "小爱同学，帮我看看今天的天气怎么样。"


def main() -> int:
    print("=== 声纹测试 ===")
    if find_model() is None:
        print("  [跳过] 没有声纹模型：models/speaker/*.onnx")
        print("         下载：python scripts/download_models.py --only speaker")
        return 0

    cfg = Config.load()
    cfg.speaker.enabled = True
    with tempfile.TemporaryDirectory() as tmp:
        cfg.speaker.profile = str(Path(tmp) / "voiceprint.json")

        from voice_agent.speech import Tts

        tts = Tts(cfg)

        def voice(speaker_id: int, text: str = SENTENCE) -> np.ndarray:
            """用指定音色合成一段音频，当作某个人的声音。

            改的是 tts.speaker_id（引擎解析后的音色），不是 cfg.tts.speaker_id：
            后者只是配置里的原始值，构造时就解析成前者了。
            """
            tts.speaker_id = speaker_id
            samples, rate = tts.synthesize(text, "reply")
            return audio_io.resample(samples, rate, 16000)

        owner = voice(0)
        other = voice(3)
        check("两段测试音频都不为空", owner.size > 16000 and other.size > 16000,
              str(owner.size) + " / " + str(other.size))

        vp = Voiceprint(cfg)
        check("声纹模块可用", vp.available, vp.status_text())

        enrolled = vp.enroll(owner, "owner")
        check("录声纹成功", enrolled.get("ok"), str(enrolled)[:70])
        check("录完之后进入已注册状态", vp.enrolled and vp.available)

        allowed, score, note = vp.check(owner)
        same_score = score
        check("本人再说话 → 放行", allowed, "相似度 " + str(round(score, 3)) + "　" + note)

        allowed2, score2, note2 = vp.check(other)
        other_score = score2
        check("别人说话 → 拦住", not allowed2, "相似度 " + str(round(score2, 3)) + "　" + note2)

        check("同人相似度明显高于异人",
              same_score > other_score,
              "同人 " + str(round(same_score, 3)) + " vs 异人 " + str(round(other_score, 3)))
        check("默认阈值落在两者之间", other_score < cfg.speaker.threshold <= same_score,
              "阈值 " + str(cfg.speaker.threshold))

        # 再录一次应该做平均，而不是覆盖
        again = vp.enroll(owner, "owner")
        check("重复录制会累加", again.get("ok") and again.get("count", 1) >= 2,
              str(again)[:60])

        # 关掉开关就必须放行（不能把人锁在门外）
        vp.enabled = False
        allowed3, _, note3 = vp.check(other)
        check("关掉开关后一律放行", allowed3, note3)

        # 重新加载：声纹要能存下来
        reloaded = Voiceprint(cfg)
        check("声纹持久化", reloaded.enrolled, reloaded.status_text())

        # 太短的音频不乱拦
        vp.enabled = True
        allowed4, _, note4 = vp.check(np.zeros(2000, dtype=np.float32))
        check("音频太短时跳过校验（宁可漏放）", allowed4, note4)

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("声纹测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
