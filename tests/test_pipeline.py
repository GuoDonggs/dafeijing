# -*- coding: utf-8 -*-
"""状态机端到端测试：不接麦克风，用合成语音当输入。

这条测试回答一个问题：「喊一声唤醒词 → 说一句指令 → 听到回复」这套状态机
真的能跑通吗？做法是把 TTS 合成的音频当成麦克风输入喂进去：

    TTS("小爱同学") + 0.6s 静音 → 唤醒词命中
    TTS("现在几点了") + 1.2s 静音 → VAD 切句 → ASR → 大脑 → 回复

播放被替换成「记录到列表」，所以既不需要声卡，也不会真的出声。

运行：python tests/test_pipeline.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_agent import audio as audio_io  # noqa: E402
from voice_agent.agent import VoiceAgent  # noqa: E402
from voice_agent.config import Config  # noqa: E402
from voice_agent.speech import WakeWord  # noqa: E402

RATE = 16000
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.float32)


class FakeMic:
    """假麦克风：永远有数据，用来验证「采集循环每轮都会检查超时」。"""

    def __init__(self) -> None:
        self.blocks = 0
        self.level = 0.0

    def read(self, timeout: float = 0.2):  # noqa: ANN201
        time.sleep(0.01)
        self.blocks += 1
        return np.zeros(512, dtype=np.float32)

    def close(self) -> None:
        return


def feed(agent: VoiceAgent, samples: np.ndarray, block: int = 512) -> None:
    """按真实麦克风的节奏，一块一块喂给状态机。"""
    for index in range(0, samples.size, block):
        agent._on_block(samples[index : index + block])


def feed_until_wake(agent: VoiceAgent, samples: np.ndarray, block: int = 512) -> bool:
    """喂到唤醒词命中就停 —— 真人喊完唤醒词也会停下来等应答语。"""
    before = agent.wake.hits
    for index in range(0, samples.size, block):
        agent._on_block(samples[index : index + block])
        if agent.wake.hits > before:
            return True
    return False


def find_command_audio(agent: VoiceAgent, text: str, expect: str,
                       attempts: int = 5) -> tuple[np.ndarray | None, str]:
    """挑一段「本机识别器确实读得对」的合成音频。

    同样是合成音的随机性：同一个「取消」，识别结果偶尔会变成「取香」。
    这条测试要验证的是状态机，不是声学模型的极限，所以先用识别器自己挑一段
    读得准的音频，再喂给状态机。
    """
    for _ in range(attempts):
        pcm = speak_to_pcm(agent, text)
        heard = agent.asr.transcribe(pcm, punctuate=True)  # type: ignore[union-attr]
        if expect in heard:
            return pcm, heard
    return None, ""


def replay_for(agent: VoiceAgent, text: str, expect_word: str, want: str,
               spoken: list, tries: int = 4, rearm=None) -> str:
    """挑一段合成音频喂进状态机，直到听到想要的那句话。

    为什么不能只挑一次：「识别器单独读得对」不等于「走完整条链路还读得对」——
    喂进去时 VAD 会再切一次、收音保护期还会吃掉开头一小段。再加上 VITS 合成
    自带随机噪声，同一句「谢谢」偶尔会被读成别的。这是测试环境的事实，不是
    被测代码的问题，所以这里重试几次，而不是让测试随机变红。

    返回最后一次听到的回复。
    """
    heard_reply = ""
    for _try in range(tries):
        if rearm is not None and agent._state != "listen":
            rearm()
        spoken.clear()
        audio, _heard = find_command_audio(agent, text, expect_word)
        if audio is None:
            continue
        feed(agent, np.concatenate([silence(0.3), audio, silence(1.2)]))
        heard_reply = spoken[-1][1] if spoken else ""
        if heard_reply == want:
            return heard_reply
    return heard_reply


def find_wake_audio(agent: VoiceAgent, word: str, attempts: int = 5) -> np.ndarray | None:
    """挑一段「确实能触发唤醒词」的合成音频。

    VITS 合成带随机噪声，同一句话每次念出来都不一样，而 KWS 对其中一部分
    就是不敏感（实测约 1/3 的合成结果打不出来）。如果只合一次就断言，
    这条测试会随机变红 —— 那不是代码坏了。这里用**独立的检测器**试几次，
    挑到能触发的那一段再喂给状态机，既真实又稳定。
    """
    for _ in range(attempts):
        audio = np.concatenate([speak_to_pcm(agent, word, "wake"), silence(0.7)])
        probe = WakeWord(agent.cfg)          # 独立实例，不碰主状态机
        hit = None
        for index in range(0, audio.size, 512):
            hit = probe.feed(audio[index : index + 512]) or hit
        if hit:
            return audio
    return None


def speak_to_pcm(agent: VoiceAgent, text: str, kind: str = "reply") -> np.ndarray:
    samples, rate = agent.tts.synthesize(text, kind)  # type: ignore[union-attr]
    return audio_io.resample(samples, rate, RATE)


def main() -> int:
    cfg = Config.load()
    # 这条测试验证的是状态机本身，所以要固定两个外部变量：
    # 1) 关掉 LLM —— 用户的 config.yaml 里可能配了 API Key，否则每次断言都变成网络调用；
    # 2) 固定唤醒词 —— 用户可能改成别的词，而合成音能不能触发 KWS 因词而异，
    #    测试不该因为用户改了唤醒词就红。
    cfg.llm.enabled = False
    cfg.wake.keywords = ["小爱同学"]
    logs: list[str] = []
    agent = VoiceAgent(cfg, log=logs.append)
    print("加载模型……")
    agent.load()

    spoken: list[tuple[str, str]] = []
    # 不真的出声，也不开线程：把播放和并发都换成同步直调，测试才可复现
    agent._speak = lambda text, kind="reply": spoken.append((kind, text))  # type: ignore[assignment]

    def fake_spawn(func, *args):  # noqa: ANN001, ANN202
        """同步执行工作函数，但保留真实 _spawn 的代次语义（否则过期判定会失真）。"""
        agent._epoch += 1
        agent._local.epoch = agent._epoch
        func(*args, epoch=agent._epoch)

    agent._spawn = fake_spawn  # type: ignore[assignment]
    # 提示音会真的走扬声器、追问窗口会改变状态流转，都会让断言不稳定；
    # 两者各自有专门的场景在下面单独验证。
    agent.cfg.agent.cues = False
    agent.cfg.agent.follow_up_ms = 0

    # 唤醒词用配置里的（用户可能改过），测试不该假设它一定是「小爱同学」
    wake_word = cfg.wake.keywords[0]
    print("\n场景 1：唤醒词「" + wake_word + "」")
    wake_audio = find_wake_audio(agent, wake_word)
    check("能找到一段能唤醒的合成语音", wake_audio is not None)
    if wake_audio is None:
        print("合成音始终打不动唤醒词，后面的场景无法继续。")
        return 1
    feed(agent, wake_audio)
    hit = agent.wake.hits  # type: ignore[union-attr]
    check("合成语音能唤醒", hit >= 1, "唤醒词命中 " + str(hit) + " 次")
    check("唤醒后有应答语", bool(spoken) and spoken[0][0] == "wake", repr(spoken[0][1] if spoken else ""))
    check("唤醒后进入聆听", agent._state == "listen", agent._state)

    print("\n场景 2：说一句指令，走完 ASR → 大脑 → 播报")
    spoken.clear()
    # 前面补一点静音：真人也是先停一下再开口，顺便避免 VAD 把第一个字切掉
    command = np.concatenate([silence(0.4), speak_to_pcm(agent, "现在几点了"), silence(1.2)])
    feed(agent, command)
    asr_lines = [line for line in logs if line.startswith("[asr]")]
    heard = asr_lines[-1].split("→", 1)[-1].strip() if asr_lines else ""
    reply = spoken[-1][1] if spoken else ""

    check("语音被识别成文字", bool(asr_lines) and len(heard) > 2, repr(heard))
    check("助手给出了回复", bool(reply), repr(reply[:50]))
    check("回复结束后回到待命", agent._state == "idle", agent._state)
    # 识别准确率是声学模型的性质（合成音尤其吃亏），链路正确性用文字输入单独断言
    check("同一句话走大脑的答案正确", "现在" in agent.ask("现在几点了"))
    print("    识别：" + heard + "   回复：" + reply[:40])

    print("\n场景 3：播报中喊唤醒词可以打断")
    spoken.clear()
    agent._interrupt.clear()
    agent._speaking.set()  # 假装正在播报
    # 刚在场景 1 命中过，防抖冷却还没过（真实使用里两次唤醒间隔远大于 1.5s）
    agent.wake.reset_cooldown()  # type: ignore[union-attr]
    barge_audio = find_wake_audio(agent, wake_word)
    fired = False if barge_audio is None else feed_until_wake(agent, barge_audio)
    check("播报期间喊唤醒词能被检测到", fired)
    check("打断事件已置位", agent._interrupt.is_set())
    check("打断后重新开始听", agent._state == "listen", agent._state)
    agent._speaking.clear()
    agent._interrupt.clear()

    print("\n场景 4：敏感操作需要语音确认")
    spoken.clear()

    # 4a 真实路径：确认阶段说的话会被 ASR 识别并投进确认队列
    # 前面补一小段静音：收音保护期会吃掉开头 0.15s，真人也是先停一下再开口
    #
    # 这里要重试几次：「识别器单独读得对」不等于「喂进状态机还读得对」，
    # 再加上 VITS 合成自带随机噪声，一次就断言会让测试随机变红。
    answer = ""
    picked = None
    for _try in range(4):
        agent._begin_listen("confirm")
        cancel_audio, heard = find_command_audio(agent, "取消", "取消")
        if cancel_audio is None:
            continue
        picked = heard
        feed(agent, np.concatenate([silence(0.3), cancel_audio, silence(1.2)]))
        answer = "" if agent._confirm_q.empty() else agent._confirm_q.get_nowait()
        if "取消" in answer:
            break
    check("能挑到一段识别正确的合成音频", picked is not None, repr(picked))
    check("确认阶段听清了回答", "取消" in answer, repr(answer))
    # 4b 判定逻辑：答案是在「问完之后」才到的，所以用一个小线程模拟用户开口
    def answer_later(text: str, delay: float = 0.3) -> None:
        threading.Thread(
            target=lambda: (time.sleep(delay), agent._confirm_q.put(text)), daemon=True
        ).start()

    # 每次换一个"轮次"：同一轮里同一个操作只会问一遍（下面单独验），
    # 所以这里要清掉记忆，模拟用户又发起了一次
    agent._confirm_memory.clear()
    answer_later("取消")
    check("回答「取消」→ 拒绝执行", agent._ask_confirm("要关机，确认吗？") is False)

    # 同一个操作在同一轮里不会被问第二遍 —— 用户的原话是
    # "明明确认过了它又问一遍，好像刚才那句白说了"。
    # 注意这一段**不要**再排"回答"：它走缓存、根本不听麦克风，
    # 排进去的回答会留在队列里污染下一段（这是这个测试抖了很久的原因）。
    check("同一个操作再问时直接复用上次的答案（拒绝）",
          agent._ask_confirm("要关机，确认吗？") is False)

    agent._confirm_memory.clear()
    answer_later("好，你弄吧")
    check("回答「好，你弄吧」→ 放行", agent._ask_confirm("要关机，确认吗？") is True)
    check("同一轮里同样的操作也不再问第二遍（同意）",
          agent._ask_confirm("要关机，确认吗？") is True)

    # 换一轮：清记忆 + 等上一段的线程落地 + 清队列
    agent._confirm_memory.clear()
    time.sleep(0.6)
    while not agent._confirm_q.empty():
        agent._confirm_q.get_nowait()
    # 不说话 = 超时。注意问不清会**再问一遍**（这正是修过的行为），
    # 所以两次尝试各推一个空回答，否则第二次要干等到超时（测试变慢又易抖）。
    # 另外先把队列里上一场景的残留清掉，否则可能被当成"这次听到的回答"。
    while not agent._confirm_q.empty():
        agent._confirm_q.get_nowait()
    threading.Thread(target=lambda: (
        time.sleep(0.1), agent._confirm_q.put(""),
        time.sleep(1.0), agent._confirm_q.put("")), daemon=True).start()
    agent.cfg.agent.confirm.timeout_ms = 800
    check("不回答 → 保守拒绝", agent._ask_confirm("要关机，确认吗？") is False)
    check("没听清时会换个短句再问一遍（而不是直接拒绝）",
          agent._action_words("要执行命令，确认吗？", ask=True) == "要不要执行命令？",
          agent._action_words("要执行命令，确认吗？", ask=True))

    print("\n场景 5：唤醒后一直没人说话 → 超时回到待命")
    # 这条曾经是坏掉的：超时检查原本挂在「mic.read 返回 None」的分支里，
    # 而麦克风每 32ms 就送来一块，那个分支几乎永远不执行。
    agent.cfg.agent.listen_timeout_ms = 250
    agent._begin_listen("command")
    fake = FakeMic()
    agent.mic = fake
    agent._running = True
    pump = threading.Thread(target=agent._loop, daemon=True)
    pump.start()
    time.sleep(0.9)
    agent._running = False
    pump.join(timeout=1.0)
    check("采集循环会持续检查超时", fake.blocks > 20, "喂了 " + str(fake.blocks) + " 块")
    check("超时后回到待命", agent._state == "idle", agent._state)


    print("\n场景 5b：指令说得长，不会被丢掉")
    # 老毛病：超时拿"说了多久"算（语音起点 + max_utterance_ms），
    # 于是话说得长一点，到点直接回待命 —— 已经录到的一整段被扔了，
    # 用户听到的是一声"叮"，然后助手又不理人了。
    # 现在改成按"静了多久"算，而且到点也要把录到的内容交出去识别。
    agent.mic = None
    agent.cfg.agent.listen_timeout_ms = 8000
    agent.cfg.agent.listen_hard_limit_ms = 60000
    agent.cfg.agent.max_utterance_ms = 300        # 故意调得很小：旧代码在这里就会走人
    agent._begin_listen("command")
    agent._arm_listening()

    class StubVad:
        """只为输入检测准备的假 VAD：永远说"没听到人声"。"""

        speech_detected = False

        def feed(self, block):  # noqa: ANN001, ANN201
            return None

        def flush(self):  # noqa: ANN201
            return None

        def reset(self) -> None:
            return

    real_vad = agent.vad
    agent.vad = StubVad()
    agent._mark_voice(np.full(512, 0.05, dtype=np.float32))
    check("电平够大就算用户在说话（不依赖 VAD）", agent._heard_speech)
    agent._heard_speech = False
    agent._last_voice_at = 0.0
    agent._mark_voice(np.zeros(512, dtype=np.float32))
    check("静音不会被当成说话", not agent._heard_speech)
    agent.vad = real_vad

    # 一直说、不停顿：旧代码会在 max_utterance_ms 之后回待命
    agent._begin_listen("command")
    agent._arm_listening()
    speech = speak_to_pcm(agent, "现在几点了")
    for _round in range(3):
        feed(agent, np.concatenate([speech, speech]))
        agent._check_timeout()
        time.sleep(0.35)
        agent._check_timeout()
    check("一直在说话时不会回待命", agent._state == "listen", agent._state)
    check("累计说话时长被记下来了", agent._voice_ms > 500, str(round(agent._voice_ms)))

    # 说到超过硬上限：把已经录到的一段交出去，而不是丢掉
    captured: list = []
    real_utterance = agent._on_utterance
    agent._on_utterance = lambda samples: captured.append(samples)  # type: ignore[assignment]
    try:
        agent.cfg.agent.listen_hard_limit_ms = 100
        time.sleep(0.15)
        agent._check_timeout()
    finally:
        agent._on_utterance = real_utterance   # type: ignore[assignment]
    check("说到超时也要把录到的内容交出去识别",
          bool(captured) and captured[0].size > 0,
          str([int(s.size) for s in captured]))
    agent._state = "idle"
    agent.cfg.agent.listen_hard_limit_ms = 45000
    agent.cfg.agent.max_utterance_ms = 15000

    print("\n场景 6：文本模式下敏感操作默认被拒绝")
    import voice_agent.tools as tools_mod
    from dataclasses import replace as dc_replace

    # 只替换 power 的处理函数，保留 tools.call 里的确认逻辑本身
    calls: list[dict] = []
    original_tool = tools_mod.REGISTRY["power"]
    tools_mod.REGISTRY["power"] = dc_replace(
        original_tool, handler=lambda **kw: (calls.append(kw), "已执行")[1]
    )
    try:
        reply = agent.ask("关机")
    finally:
        tools_mod.REGISTRY["power"] = original_tool
    check("默认确认策略下不执行敏感工具", calls == [], "实际调用：" + str(calls))
    check("对用户说明被拒绝了", reply == tools_mod.CANCEL_REPLY, repr(reply[:40]))

    print("\n场景 7：追问窗口内不用再喊唤醒词")
    # 这一步验的是"窗口机制"本身，所以强制每次都留；
    # "由 LLM 决定要不要留"在场景 7b 里单独验
    agent.cfg.agent.follow_up_mode = "always"
    agent.cfg.agent.follow_up_ms = 1200
    agent.cfg.agent.cues = True          # 这一步要覆盖提示音的播放路径
    agent.mic = None
    agent._begin_listen("command")
    spoken.clear()
    feed(agent, np.concatenate([silence(0.4), speak_to_pcm(agent, "现在几点了"), silence(1.2)]))
    first = spoken[-1][1] if spoken else ""
    check("第一句指令得到回复", bool(first), repr(first[:30]))
    check("回复后进入追问窗口", agent._state == "listen", agent._state)
    check("追问窗口时长被写入状态", agent.status()["follow_up_ms"] == 1200)

    def rearm() -> None:
        agent._begin_listen("command", timeout_ms=1200, follow_up=True)
        agent._arm_listening()

    second = replay_for(agent, "谢谢", "谢", "不客气。", spoken, rearm=rearm)
    check("窗口内可以直接说下一句（无需唤醒词）", second == "不客气。", repr(second))

    time.sleep(1.5)
    agent._check_timeout()
    check("窗口超时后回到待命", agent._state == "idle", agent._state)
    agent.cfg.agent.cues = False

    print("\n场景 7b：要不要接着听，由 LLM（本处用追问标记模拟）决定")
    agent.cfg.agent.follow_up_mode = "auto"
    agent.cfg.agent.follow_up_ms = 4000
    agent.brain.wants_followup = False
    window, _why = agent._follow_up_window("好的，已经打开了浏览器。")
    check("只是汇报结果 -> 不留窗口", window == 0, str(window))
    window, why = agent._follow_up_window("要打开哪一个浏览器？")
    check("反问了用户一句 -> 留窗口", window == 4000, str(window) + " " + why)
    agent.brain.wants_followup = True
    window, why = agent._follow_up_window("好的，已经打开了浏览器。")
    check("模型调了 keep_listening -> 留窗口", window == 4000, why)
    agent.brain.wants_followup = False
    agent.cfg.agent.follow_up_mode = "off"
    window, _why = agent._follow_up_window("要打开哪一个浏览器？")
    check("关掉之后一律不留窗口", window == 0, str(window))

    agent.cfg.agent.follow_up_mode = "auto"
    agent.cfg.agent.follow_up_ms = 0

    print("\n场景 8：对话记录")
    roles = [item["role"] for item in agent.status()["transcript"]]
    check("对话记录里有用户和助手", "user" in roles and "assistant" in roles, str(roles[-6:]))
    check("对话记录文本可读",
          any(item["text"] == "不客气。" for item in agent.status()["transcript"]))

    print("\n场景 9：长回复要切成多块播出来（这里曾经整个播报都炸掉）")
    # 回归测试：预取播放的消费端以前拿 item[0] == "error" 判断出错标记，
    # 而正常消息的第一项是 numpy 数组 —— 数组比较得到逐元素布尔数组，
    # if 它直接抛 "The truth value of an array ... is ambiguous"。
    # 只在"回复够长、切成多块"时才走到，所以短回复怎么测都正常。
    from voice_agent import speech as speech_mod

    played: list = []
    original_play = speech_mod.audio_io.play

    def fake_play(samples, rate, **kwargs):  # noqa: ANN001, ANN202
        played.append(int(samples.size))
        return True

    speech_mod.audio_io.play = fake_play
    try:
        long_text = ("好的，我先看了一下你的磁盘，C 盘还剩 156 GB，D 盘还剩 402 GB，"
                     "空间都还够用。另外浏览器也已经帮你打开了，还要做别的吗？")
        chunks = agent.tts.chunks(long_text)
        ok = agent.tts.speak(long_text, kind="reply")
        check("长回复被切成多块", len(chunks) >= 2, str(len(chunks)) + " 块")
        check("多块都播出来了", ok and len(played) == len(chunks),
              "播了 " + str(len(played)) + " / " + str(len(chunks)))
        check("没有空块", bool(played) and all(size > 0 for size in played), str(played))
    finally:
        speech_mod.audio_io.play = original_play

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("状态机端到端测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
