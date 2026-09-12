# -*- coding: utf-8 -*-
"""主状态机：唤醒 → 听 → 想 → 说，随时可被打断。

线程模型（刻意保持简单，只有两条线程）：

    采集线程（主循环）      喂唤醒词、喂 VAD、收句子，外加一次识别
       │                    识别实测 RTF 0.03（1 秒音频约 35ms，且被
       │                    max_utterance_ms 封顶），这点停顿换来状态机极简；
       │                    真正耗时的 LLM 与工具调用都在工作线程里
       ▼
    工作线程（按需创建）    大脑 → 工具 → TTS 播放

打断有两条路：
- 说话/播报中喊唤醒词 → 立即停播、取消当前任务、重新开始听（barge-in）；
- 播报播放按小块写、每块查一次停止事件，所以停下来的延迟是几十毫秒。
"""

from __future__ import annotations

import os
import queue
import random
import re
import sys
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from . import audio as audio_io
from . import journal
from . import rules
from . import security
from . import tools
from .brain import Brain
from .config import Config
from .speaker import Voiceprint
from .speech import Asr, Tts, VadSegmenter, WakeWord

__all__ = ["VoiceAgent"]

# 状态：idle 待命 / listen 正在听 / think 正在干活 / wait 处理中 / speaking 播报
_IDLE, _LISTEN, _THINK, _WAIT = "idle", "listen", "think", "wait"

# 刚开始播报的这一小段里，不认「喊唤醒词打断」。
# 原因：没有回声消除，扬声器的起音最容易被自己的 KWS 当成唤醒词，
# 一打断整句回复就没了 —— 用户听到的正是"它回复了却没出声"。
BARGE_IN_GRACE_S = 0.8

#: 否定字：出现在肯定词前面时，「行」就不再是「行」。
#: 语音确认是整个权限模型里**唯一**的人工闸门，把「不太行」听成「行」
#: 等于把关机、执行命令这类操作放了行 —— 所以这里只看字面，宁可多问一次。
_NEGATIVE_CHARS = "不没别勿非甭毋无莫休"
#: 答话开头的语气词（「嗯，行」里的「嗯，」先剥掉再判）。
_FILLER = re.compile(r"^[\s，,。.、！!~～呃嗯哦噢唉哎诶啊呀呐唔额]+")
#: 反问 / 犹豫的尾巴：跟在一个肯定词后面就不是同意（「好什么好」）。
_DOUBT_TAILS = ("什么", "啥")
#: 出现这些说法一律交给语义判断（「行，再说吧」不是干脆的同意）。
#: 「不过 / 但是」这类转折：答案是"可以，不过…"时不能当成干脆的同意
_DOUBT_WORDS = ("怎么可能", "为什么", "真的吗", "至于吗", "再说", "等下",
                "不过", "但是", "可是", "只是", "然而", "有点")
#: 超过这个长度就别拿词表硬判：长句里几乎一定夹着「是 / 行 / 好」这类字。
_WORDLIST_MAX = 16


class TurnToken:
    """一轮任务的「请停下」信号：**绑定当轮的 epoch**。

    以前这里传的是共享的 threading.Event（self._interrupt），而**新任务一开始
    就会把它 clear()** —— 于是旧任务从一次长工具调用里醒过来时发现「没人让我停」，
    接着把整个任务跑完：日志里就是「上一个任务还在跑」。用户报的正是这个。

    epoch 只会往前走，所以旧任务的信号一旦作废就**永远是停**，谁也没法把它复活。
    接口只需要 is_set()（Llm._post 和 brain 都是这么用的），另外补一个 wait()
    兼容 threading.Event 的用法。
    """

    __slots__ = ("_agent", "_epoch")

    def __init__(self, agent: "VoiceAgent", epoch: int) -> None:
        self._agent = agent
        self._epoch = int(epoch)

    def is_set(self) -> bool:
        """还要不要继续：epoch 变了（被新任务取代）或者引擎停了。"""
        return self._agent._epoch != self._epoch or self._agent._stopping.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """兼容 threading.Event 的用法：等它被置位（或超时），返回是否已置位。"""
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def set(self) -> None:
        """把这一轮标记成作废（等价于「有新任务接替」）。"""
        self._agent._epoch += 1

    def clear(self) -> None:
        """什么都不做：作废是不可逆的 —— 这正是它比 Event 可靠的地方。"""


class VoiceAgent:
    def __init__(self, cfg: Config, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = journal.adapt(log)
        self.brain = Brain(cfg, log=log)

        # 后台子代理做完事要主动汇报，但只能在「真的闲着」的时候开口 ——
        # 抢在用户正听的回答前面说话，听起来就是助手在自言自语。
        # 所以回调只负责入队，真正的播报交给采集循环择机做。
        self.brain.subagents.on_done = self._on_subagent_done
        self.brain.watcher.on_hit = self._on_watch_hit
        self._announce: deque = deque(maxlen=5)
        self._announce_lock = threading.Lock()
        self._announce_busy = False

        # 控制「程序自己」：开新会话 / 重启 / 退出。
        # 界面（PyQt）可以注册一个 app_hook 接管重启和退出 —— 它才知道怎么
        # 优雅地关掉窗口再把自己拉起来；没注册（命令行、网页版）就走默认路径。
        self.app_hook: Callable[[str], None] | None = None
        self.self_action = ""
        self._self_guard = threading.Event()
        tools.set_self_handler(self.self_control)

        self.wake: WakeWord | None = None
        self.vad: VadSegmenter | None = None
        self.asr: Asr | None = None
        self.tts: Tts | None = None
        self.mic: audio_io.Mic | None = None
        # 合成引擎"按需补建"用的锁（见 _ensure_tts）
        self._tts_lock = threading.Lock()

        self._state = _IDLE
        self._listen_target = "command"       # command = 听指令，confirm = 听确认
        self._listen_started = 0.0
        self._listen_timeout_ms = 0
        self._listen_follow_up = False
        self._heard_speech = False
        self._speech_started_at = 0.0
        # 输入检测用的状态：最后一次听到人声的时刻、累计说了多久。
        # 超时判定从"说了多久"改成"静了多久"，全靠这两个值。
        self._last_voice_at = 0.0
        self._voice_ms = 0.0
        # 收音保护期：唤醒词和提示音的尾音还没散干净，这段音频不参与录音，
        # 否则会出现「喊完唤醒词，助手把唤醒词本身当成一条指令执行了」。
        # 按**样本数**而不是墙上时间来算：麦克风本来就是实时的，两者等价，
        # 但样本数在离线回放/测试里也是确定的。
        self._guard_samples = 0
        # 对话记录：界面直接展示，不用去翻日志
        self.transcript: deque[dict] = deque(maxlen=60)
        # 唤醒词前后的一小段音频：声纹要拿它来判断"是不是主人在说话"。
        # 2.5 秒足够覆盖一次完整的唤醒词，又不至于把上一句话也拖进来。
        blocks = max(1, int(2.5 * self.cfg.audio.sample_rate / max(1, self.cfg.audio.block_size)))
        self._recent: deque = deque(maxlen=blocks)
        self.voiceprint = Voiceprint(cfg)
        self._speaking = threading.Event()     # 正在通过扬声器说话
        self._stop_speak = threading.Event()   # 立刻停播
        # 这一轮播报是什么时候开始的：用来挡掉"自己把自己打断"
        self._speaking_since = 0.0
        self._interrupt = threading.Event()    # 立刻取消当前任务（停播用）
        #: 引擎级停止：stop() 置位、start() 清掉。TurnToken 会看它 ——
        #: 停止之后旧任务不该再往下跑。
        self._stopping = threading.Event()
        self._confirm_q: queue.Queue[str] = queue.Queue()
        # 这一轮里已经答过的确认：同一个操作（同一句确认提示）不再问第二遍。
        # 用户的抱怨是"明明确认过了，它又问一遍，好像刚才那句白说了" ——
        # 模型重发同一个调用时就会这样。一轮结束就清空。
        self._confirm_memory: dict[str, bool] = {}
        self._worker: threading.Thread | None = None
        self._epoch = 0
        # 工作线程自己的代次放在 thread-local 里：确认流程是「哪个任务问的就归哪个任务」，
        # 用实例变量会被后来的任务覆盖，导致正在等回答的任务被误判成「已过期」。
        self._local = threading.local()
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._in_device: int | None = None
        self._out_device: int | None = None
        #: 唤醒词开关（wake.enabled）。关掉之后只能从界面派发指令。
        self._wake_on = bool(getattr(cfg.wake, "enabled", True))

        # 给界面看的「最近发生了什么」
        self.last_heard = ""
        self.last_reply = ""
        self.turns = 0

    # ───────────────────── 生命周期 ─────────────────────

    def load(self) -> dict:
        """加载全部模型；返回各阶段耗时，便于体检。"""
        started = time.perf_counter()
        report: dict = {}
        step = time.perf_counter()
        self.wake = WakeWord(self.cfg)
        report["唤醒词"] = time.perf_counter() - step
        step = time.perf_counter()
        self.vad = VadSegmenter(self.cfg)
        report["端点检测"] = time.perf_counter() - step
        step = time.perf_counter()
        self.asr = Asr(self.cfg)
        report["语音识别"] = time.perf_counter() - step
        step = time.perf_counter()
        self.tts = Tts(self.cfg) if self.cfg.tts.enabled else None
        report["语音合成"] = time.perf_counter() - step
        report["合计"] = time.perf_counter() - started
        return report

    def open_devices(self) -> None:
        """按配置解析麦克风 / 扬声器（换设备后重新调用即可生效）。"""
        self._in_device = audio_io.resolve_device(self.cfg.audio.input_device, "input")
        self._out_device = audio_io.resolve_device(self.cfg.audio.output_device, "output")
        if self.tts is not None:
            self.tts.device = self._out_device

    def start(self, announce: bool = True) -> None:
        """非阻塞启动：需要时加载模型，开麦克风，后台跑状态机。"""
        if self._running:
            return
        if self.wake is None:
            self.load()
        assert self.wake and self.vad and self.asr
        self.open_devices()

        self._wake_on = bool(getattr(self.cfg.wake, "enabled", True))
        if announce:
            # 唤醒词只打进日志：马上要开麦克风，从扬声器里念出来会变成自唤醒
            self.log("[agent] 唤醒词：" + ("、".join(self.cfg.wake.keywords)
                                           if self._wake_on else "已关闭"))
            self._speak("语音助手已就绪，随时听候吩咐。", kind="notice")

        self._stopping.clear()
        self._running = True
        self.mic = audio_io.Mic(
            device=self._in_device,
            sample_rate=self.cfg.audio.sample_rate,
            block_size=self.cfg.audio.block_size,
            gain=self.cfg.audio.input_gain,
        )
        self.mic.start()
        self._loop_thread = threading.Thread(target=self._loop, name="voice-agent-loop", daemon=True)
        self._loop_thread.start()
        if self._wake_on:
            self.log("[agent] 已开始监听，喊「" + self.cfg.wake.keywords[0] + "」唤醒我")
        else:
            self.log("[agent] 唤醒词已关闭（wake.enabled: false）："
                     "只能用界面上的「派发」或「录音测试」下指令")

    def _loop(self) -> None:
        """采集循环：只做喂唤醒词 / 喂 VAD / 收句子。

        超时检查必须每轮都跑：麦克风每 32ms 就送来一块，read() 几乎从不超时，
        挂在「read 返回 None」的分支里等于永远不检查 —— 唤醒了却不说话，
        助手就会一直停在「正在听」，喊第二遍也没用（此时唤醒词被喂进了 VAD）。
        """
        try:
            while self._running:
                self._check_timeout()
                self._check_announce()
                block = self.mic.read(timeout=0.2) if self.mic is not None else None
                if block is None:
                    continue
                self._on_block(block)
        except Exception as exc:  # noqa: BLE001 - 循环不能悄悄死掉
            self.log("[agent] 主循环异常退出：" + str(exc))
        finally:
            self._running = False

    def run(self) -> None:
        """阻塞运行（命令行模式），直到 Ctrl+C 或 stop()。"""
        self.start(announce=True)
        try:
            while self._running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.log("\n[agent] 收到中断，退出")
        finally:
            self.stop()

    def stop(self) -> None:
        """停止监听并释放设备；可再次 start()。"""
        self._stop_background("停止引擎")
        self._stopping.set()
        self._running = False
        self._stop_speak.set()
        self._interrupt.set()
        loop = self._loop_thread
        if loop is not None and loop.is_alive() and loop is not threading.current_thread():
            loop.join(timeout=2.0)
        self._loop_thread = None
        if self.mic is not None:
            self.mic.close()
            self.mic = None
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._speaking.clear()
        self._recent.clear()
        with self._announce_lock:
            self._announce.clear()
        self._announce_busy = False
        # 必须清掉：否则再次 start() 时「已就绪」和「我在」会被当成「正在停止」而被静默丢弃
        self._stop_speak.clear()
        self._interrupt.clear()
        self._state = _IDLE

    # 兼容旧名字
    def close(self) -> None:
        self.stop()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def subagents(self):
        """后台子代理管理器（界面查进度用）。"""
        return self.brain.subagents

    def status(self) -> dict:
        """给网页 UI / 外部程序看的一份快照。"""
        state_text = {
            _IDLE: "待命",
            _LISTEN: "正在听",
            _THINK: "思考中",
            _WAIT: "处理中",
        }.get(self._state, self._state)
        if self._speaking.is_set():
            state_text = "播报中"
        return {
            "running": self._running,
            "state": self._state,
            "state_text": state_text,
            "speaking": self._speaking.is_set(),
            "models_loaded": self.wake is not None,
            "mode": self.brain.mode,
            "llm_ready": self.brain.llm is not None,
            # 界面要如实展示"跑在什么算力、用的哪个合成引擎"
            "provider_text": self.cfg.speech.provider_text,
            "tts_engine": self.tts.engine if self.tts else self.cfg.tts.engine,
            "wake_words": list(self.cfg.wake.keywords),
            "wake_hits": self.wake.hits if self.wake else 0,
            "voiceprint": {
                "enabled": bool(self.voiceprint.enabled),
                "ready": bool(self.voiceprint.available and self.voiceprint.enrolled),
                "text": self.voiceprint.status_text(),
                "threshold": self.voiceprint.threshold,
                "accepted": self.voiceprint.accepted,
                "rejected": self.voiceprint.rejected,
                "last_score": round(self.voiceprint.last_score, 3),
            },
            "last_heard": self.last_heard,
            "last_reply": self.last_reply,
            "transcript": list(self.transcript)[-24:],
            "follow_up_ms": int(self.cfg.agent.follow_up_ms),
            "follow_up_mode": str(self.cfg.agent.follow_up_mode),
            "listen_timeout_ms": int(self.cfg.agent.listen_timeout_ms),
            # 输入检测：界面据此显示「听到你开口了，慢慢说」
            "listen_heard": bool(self._heard_speech),
            "listen_ms": int(self._voice_ms),
            # 后台子代理 / 定时轮询：界面拿它显示「派出去的活还在跑」
            "subagents": self.brain.subagents.snapshot(),
            "watches": self.brain.watcher.snapshot(),
            # 权限：界面拿它显示当前模式和"链路可不可信"
            "security": security.snapshot(),
            "turns": self.turns,
            "mic_level": round(float(self.mic.level), 4) if self.mic else 0.0,
            "asr_rtf": round(self.asr.rtf, 4) if self.asr else 0.0,
            "tts_rtf": round(self.tts.rtf, 4) if self.tts else 0.0,
            # 出声情况：跳过（没内容）/ 失败（播不出来）/ 被打断
            "tts_skipped": int(getattr(self.tts, "skipped", 0)) if self.tts else 0,
            "tts_failed": int(getattr(self.tts, "failed", 0)) if self.tts else 0,
            "input_device": self._in_device,
            "output_device": self._out_device,
        }

    def dispatch(self, text: str) -> None:
        """把一段文字当成「刚识别出来的指令」派发下去（网页 UI 的输入框用）。"""
        value = (text or "").strip()
        if not value:
            return
        if not self._running:
            raise RuntimeError("引擎还没启动")
        self.last_heard = value
        # 从界面派发的指令也要进对话记录，否则界面上只看到一堆助手回复
        self._note("user", value)
        self._spawn(self._handle_command, value)

    def cancel(self) -> None:
        """取消当前任务并停播，回到待命（界面的「打断」按钮）。"""
        self._stop_background("用户按了打断")
        self._epoch += 1  # 让正在跑的任务作废，免得它回头又把状态写回去
        self._interrupt.set()
        self._stop_speak.set()
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self._stop_speak.clear()
        self._interrupt.clear()
        self._speaking.clear()
        self._state = _IDLE
        self._note("system", "已打断当前任务")
        self.log("[agent] 已打断当前任务（旧任务已作废，不会再发起新的动作）")

    # ───────────────────── 音频回调（采集线程） ─────────────────────

    def _on_block(self, block: np.ndarray) -> None:
        assert self.wake and self.vad
        if not self._wake_on:
            # wake.enabled: false —— 唤醒词整个关掉，只能从界面派发指令。
            # 以前这一项**没有任何代码读**：用户以为关了，喊一声它还是答应。
            if self._state == _LISTEN:
                self._mark_voice(block)
                utterance = self.vad.feed(block)
                if utterance is not None and utterance.size >= int(
                        self.cfg.audio.sample_rate * self.cfg.agent.min_speech_ms / 1000):
                    self._on_utterance(utterance)
            return
        # 播报期间：只跑唤醒词，用于打断
        if self._speaking.is_set():
            if self.cfg.agent.barge_in_wake and self.wake.feed(block):
                # 刚开口的一小段里不认打断：扬声器的起音最容易被自己的 KWS
                # 当成唤醒词，一打断整句回复就没了 —— 用户听到的就是"它没理我"。
                if time.monotonic() - self._speaking_since < BARGE_IN_GRACE_S:
                    self.log("[agent] 播报刚开始，忽略这次唤醒词（防自打断）")
                else:
                    self._barge_in()
            return

        if self._state in (_IDLE, _THINK):
            # 留一小段"刚才的音频"给声纹用
            self._recent.append(np.asarray(block, dtype=np.float32).reshape(-1))
            # 干活期间也保持唤醒词在线，喊一声即可打断重来
            if self.wake.feed(block):
                if self._state == _THINK:
                    self._barge_in()
                else:
                    self._on_wake()
            return

        if self._state == _LISTEN:
            if self._guard_samples > 0:
                # 还在保护期：丢掉唤醒词/提示音的尾音
                self._guard_samples = max(0, self._guard_samples - int(block.size))
                return
            self._mark_voice(block)
            utterance = self.vad.feed(block)
            if utterance is None:
                return
            min_samples = int(self.cfg.audio.sample_rate * self.cfg.agent.min_speech_ms / 1000)
            if utterance.size < min_samples:
                return
            self._on_utterance(utterance)

    def _mark_voice(self, block: np.ndarray) -> None:
        """输入检测：这一小块音频里有没有人在说话。

        两路一起看，缺一路都会漏：

        - VAD 说有人在说 —— 最可靠，但它要攒够一小段才敢下结论；
        - 电平高于 voice_floor —— 说话轻、离麦远的人 VAD 可能一直不触发，
          只看 VAD 的话助手会认为"你没开口"，于是窗口提前过期走人。

        顺便记录"最后一次听到人声的时刻"和累计说话时长，超时判定要用。
        """
        try:
            voiced = self.vad.speech_detected
        except Exception:  # noqa: BLE001 - VAD 读不到就当没听到
            voiced = False
        if not voiced:
            samples = np.asarray(block, dtype=np.float32).reshape(-1)
            if samples.size:
                level = float(np.sqrt(np.mean(samples ** 2)))
                voiced = level >= float(getattr(self.cfg.agent, "voice_floor", 0.008))
        if not voiced:
            return
        now = time.monotonic()
        self._last_voice_at = now
        self._voice_ms += block.size * 1000.0 / max(1, self.cfg.audio.sample_rate)
        if not self._heard_speech:
            self._heard_speech = True
            self.log("[agent] 听到你开口了，慢慢说")
        if not self._speech_started_at:
            self._speech_started_at = now

    def _stand_down(self, note: str = "（没听到你说话）") -> None:
        """收工回待命（没形成指令）。"""
        self._state = _IDLE
        if self.vad is not None:
            self.vad.reset()
        if self._listen_follow_up:
            # 追问窗口静默结束：不要每次答完都“叮”一声，太吵
            self.log("[agent] 追问窗口结束，回到待命")
            return
        self.log("[agent] 等待超时，回到待命：" + note)
        self._note("system", note)
        self._cue("timeout")

    def _flush_utterance(self) -> None:
        """把 VAD 里已经录到的一段交出去识别 —— **不丢**。

        这是这次改动的核心：以前到了时限就把状态清回待命，用户说了十几秒的
        一段话直接没了（他听到的是一声"叮"，然后助手又回到待命）。
        现在只有两种情况才会真的放弃：压根没听到人声，或者录到的比
        min_speech_ms 还短（那确实不成句子）。
        """
        samples = None
        try:
            samples = self.vad.flush() if self.vad is not None else None
        except Exception as exc:  # noqa: BLE001 - 收尾失败也不能卡住主循环
            self.log("[agent] 收音收尾失败：" + str(exc)[:60])
        min_samples = int(self.cfg.audio.sample_rate * self.cfg.agent.min_speech_ms / 1000)
        if samples is None or samples.size < min_samples:
            self._stand_down("（没听清，回到待命）")
            return
        self.log("[agent] 收尾，把已录到的 {:.1f} 秒交给识别".format(
            samples.size / self.cfg.audio.sample_rate))
        self._on_utterance(samples)

    def _check_timeout(self) -> None:
        """唤醒之后一直没等到一句完整的指令 → 该收尾就收尾。

        判定分三段。**关键是别再拿"说了多久"当超时** —— 那正是
        "话稍微长一点就被丢掉"的原因：

        1. 一直没听到人说话 → listen_timeout_ms 到点回待命（原样保留）；
        2. 听到了人声 → 计时基准换成**静音时长**：只要还在说就一直等，
           停下来超过 max(min_silence*2, 0.8s) 才收尾（正常是 VAD 先给出整句，
           这里是兜底：VAD 没切出句子时，把已经录到的一段交出去）；
        3. 说个没完（超过 listen_hard_limit_ms，默认 45 秒）→ 不再等，
           同样把已经录到的一段交出去识别，而不是丢掉重来。
        """
        if self._state != _LISTEN or self._listen_target != "command":
            return
        if not self._listen_started:
            return
        now = time.monotonic()
        config = self.cfg.agent

        if not self._heard_speech:
            if now <= self._listen_started + self._listen_timeout_ms / 1000.0:
                return
            self._stand_down()
            return

        limit = int(getattr(config, "listen_hard_limit_ms", 45000))
        if limit > 0 and now > self._listen_started + limit / 1000.0:
            self.log("[agent] 这一轮已经说了 {:.0f} 秒，先按已经录到的内容处理".format(
                now - self._listen_started))
            self._flush_utterance()
            return

        quiet_for = now - max(self._last_voice_at, self._listen_started)
        grace = max(0.8, config.min_silence_ms * 2 / 1000.0)
        if quiet_for < grace:
            return
        if self._voice_ms >= config.min_speech_ms:
            self._flush_utterance()
        else:
            self._stand_down("（没听到完整的句子）")

    # ───────────────────── 控制程序自己 ─────────────────────

    def self_control(self, action: str, reason: str = "") -> str:
        """开新会话 / 重启 / 退出（工具层调过来的）。

        重启和退出都要**先把话说完**：回复还在合成、播放，立刻 os._exit 就会
        被听成"它答应了然后就没了"。所以这类动作延迟一秒多再执行。
        """
        what = str(action or "").strip().lower()
        if what in ("new_session", "new", "reset"):
            return self.new_session(reason)
        if what in ("restart", "reboot"):
            self.schedule_self_action("restart")
            return "好，我这就重启，稍等一下。"
        if what in ("quit", "exit", "close"):
            self.schedule_self_action("quit")
            return "好，我先退下了，需要的时候再叫我。"
        return "不认识这个操作：" + str(action)

    def new_session(self, reason: str = "") -> str:
        """开一个新会话：清掉大脑的上下文（界面上的记录留着，那是历史）。"""
        self.brain.reset()
        self._note("system", "—— 新会话 ——" + (("（" + str(reason) + "）") if reason else ""))
        self.last_heard = ""
        self.last_reply = ""
        self.log("[agent] 已开启新会话，上下文清空")
        return "好，之前的先放一边，我们从头说。"

    def schedule_self_action(self, action: str, delay: float = 1.8) -> None:
        """安排一次对自己的操作；重复请求只认第一次。"""
        if self._self_guard.is_set():
            return
        self._self_guard.set()

        def later() -> None:
            time.sleep(max(0.0, float(delay)))
            self.self_action = action
            self.log("[agent] 执行自身操作：" + action)
            hook = self.app_hook
            try:
                if hook is not None:
                    # 界面自己会重启 / 退出，我们只要停下麦克风
                    self.stop()
                    hook(action)
                    return
                if action == "restart":
                    self._relaunch()
                self.stop()
            except Exception as exc:  # noqa: BLE001 - 退不干净也得退
                self.log("[agent] 自身操作失败：" + str(exc)[:80])
                self.stop()
            finally:
                if hook is None:
                    # 命令行 / 网页版：没有界面可以接管，直接把进程结束掉。
                    # 用 os._exit 是有意的 —— 这会儿可能在别的线程里，
                    # 走正常退出路径会卡在非守护线程上，用户看到的是"它没退"。
                    os._exit(0)

        threading.Thread(target=later, name="voice-agent-selfctl", daemon=True).start()

    @staticmethod
    def _relaunch() -> None:
        """把自己重新拉起来（命令行版）。"""
        import subprocess

        command = VoiceAgent.relaunch_command()
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000008
            subprocess.Popen(command, creationflags=flags, close_fds=True)
        except Exception as exc:  # noqa: BLE001
            print("[agent] 重启失败：" + str(exc), file=sys.stderr)

    @staticmethod
    def relaunch_command() -> list[str]:
        """重新启动本程序的命令行（打包版和源码版不一样）。"""
        if getattr(sys, "frozen", False):
            return [sys.executable, *sys.argv[1:]]
        return [sys.executable, "-m", "voice_agent", *(sys.argv[1:] or ["ui"])]

    # ───────────────────── 子代理汇报 ─────────────────────

    def _on_subagent_done(self, item) -> None:
        """子代理做完时的回调（跑在子代理线程里）。

        这里**只记录和入队**，绝不直接说话：此刻用户可能正在听另一条回答，
        而这个线程也不知道麦克风那边是什么状况。
        """
        text = (item.result or "").strip()
        name = item.name or item.id
        if not text:
            self.log("[subagent] " + name + " 没给出结论，跳过汇报")
            return
        self._note("assistant", "「" + name + "」回话：" + text)
        if not self.cfg.agent.subagent_announce:
            self.log("[subagent] 汇报（配置为不播报）：" + text[:40])
            return
        with self._announce_lock:
            if len(self._announce) == self._announce.maxlen:
                # 队列满说明积压了好几条没播 —— 说出来，别让它静悄悄地丢
                self.log("[subagent] 待播汇报积压，最早的一条不再单独播报")
            self._announce.append((name, text))

    def _on_watch_hit(self, item) -> None:
        """轮询命中 / 出错 / 盯到点（跑在轮询线程里）：只入队，由主循环择机播报。"""
        name = item.id
        text = item.last or "条件成立了"
        if getattr(item, "state", "") == "error":
            text = "盯不下去了：" + (item.error or text)
        elif getattr(item, "state", "") == "stopped":
            text = text or "盯的时间到了，先停下"
        self._note("assistant", "【" + name + "】" + text)
        # 用**自己**的开关：以前借的是 subagent_announce（文档写的是"子代理做完
        # 主动汇报"），用户只想关子代理播报，却连"盯着…告诉我"的唯一出口一起没了。
        if not bool(getattr(self.cfg.agent, "watch_announce", True)):
            self.log("[watch] 命中（配置为不播报）：" + text[:40])
            return
        with self._announce_lock:
            if len(self._announce) == self._announce.maxlen:
                self.log("[watch] 待播汇报积压，最早的一条不再单独播报")
            self._announce.append((name, text))

    def _check_announce(self) -> None:
        """在采集循环里择机播报子代理的汇报。

        条件卡得严是故意的：只有**真的待命**、没在播报、也没有任务线程时才开口。
        播报放进单独的线程而不是就地播，是因为 _speak 会一直阻塞到念完 ——
        卡在采集循环里，麦克风的数据就没人收了，表现为「醒着却听不见」。
        """
        if self._announce_busy or not self._announce or self._state != _IDLE:
            return
        if self._speaking.is_set():
            return
        worker = self._worker
        if worker is not None and worker.is_alive():
            return
        with self._announce_lock:
            if not self._announce:
                return
            name, text = self._announce.popleft()
        self._announce_busy = True
        threading.Thread(target=self._announce_worker, args=(name, text),
                         name="voice-agent-announce", daemon=True).start()

    def _announce_worker(self, name: str, text: str) -> None:
        try:
            self.log("[agent] 汇报后台结果：" + text[:40])
            self._speak(name + "：" + text, kind="notice")
        except Exception as exc:  # noqa: BLE001 - 汇报失败不该影响主循环
            self.log("[agent] 子代理汇报播报失败：" + str(exc)[:80])
        finally:
            self._announce_busy = False

    # ───────────────────── 状态迁移 ─────────────────────

    def _on_wake(self) -> None:
        # 声纹校验放在最前面：不是主人的声音就当作没听见，
        # 既不答应、也不开麦，连对话记录都不留 —— 否则"谁喊都应"就没意义了。
        allowed, score, note = self._check_voiceprint()
        if not allowed:
            self.log("[agent] 唤醒词命中，但" + note + "，忽略")
            self._note("system", "有人喊了唤醒词，但声纹不匹配，已忽略")
            self._cue("timeout")
            return
        if score:
            self.log("[agent] 声纹通过：" + str(round(score, 3)))
        self.log("[agent] 唤醒词命中")
        self.log("[agent] 进入听指令状态（超时 "
                 + str(self.cfg.agent.listen_timeout_ms) + "ms，追问模式 "
                 + str(self.cfg.agent.follow_up_mode) + "）", "detail")
        self._note("system", "唤醒词命中")
        self.wake.reset()
        self.vad.reset()
        # 先把状态切到「正在听」，应答语放完再由 _arm_listening 正式计时
        self._begin_listen("command")
        reply = random.choice(self.cfg.wake.replies) if self.cfg.wake.reply_random else self.cfg.wake.replies[0]
        self._spawn(self._speak_then_listen, reply)

    def _check_voiceprint(self) -> tuple[bool, float, str]:
        """拿刚才那一小段音频做声纹校验。没开启/没注册时一律放行。"""
        if not self.voiceprint.enabled:
            return True, 0.0, ""
        audio = np.concatenate(list(self._recent)) if self._recent else None
        if audio is None or audio.size == 0:
            return True, 0.0, "没有可用的音频"
        return self.voiceprint.check(audio)

    def _speak_then_listen(self, reply: str, epoch: int | None = None) -> None:
        epoch = self._epoch_of(epoch)
        if self._stale(epoch):
            return
        self._speak(reply, kind="wake")
        if not self._stale(epoch) and self._state == _LISTEN:
            self._arm_listening()

    def _begin_listen(self, target: str, timeout_ms: int | None = None, follow_up: bool = False) -> None:
        if self.vad is None:
            return
        self.vad.reset()
        self._state = _LISTEN
        self._listen_target = target
        self._listen_timeout_ms = int(
            timeout_ms if timeout_ms is not None else self.cfg.agent.listen_timeout_ms
        )
        self._listen_follow_up = follow_up
        self._listen_started = time.monotonic()
        self._heard_speech = False
        self._speech_started_at = 0.0
        self._last_voice_at = 0.0
        self._voice_ms = 0.0
        # 防止上一轮的尾音被当成这一轮的开头（arm 时会重新设成完整保护期）
        self._guard_samples = int(self.cfg.audio.sample_rate * 0.15)

    def _arm_listening(self) -> None:
        """真正开始收音前的收尾动作。

        顺序有讲究：先把麦克风队列里积压的、包含扬声器尾音的数据丢掉，
        再用一段提示音告诉用户「现在可以说」，最后才重新计时。
        少了这一步，助手会把自己的尾音当成用户开口，或者用户根本不知道该何时说话。
        """
        if self.mic is not None:
            self.mic.flush()
        self._cue("listen")
        if self.mic is not None:
            self.mic.flush()
        time.sleep(0.08)   # 让提示音的余音彻底过去
        if self.vad is not None:
            self.vad.reset()
        self._listen_started = time.monotonic()
        self._heard_speech = False
        self._speech_started_at = 0.0
        self._last_voice_at = 0.0
        self._voice_ms = 0.0
        self._guard_samples = int(self.cfg.audio.sample_rate * 0.2)

    def note(self, role: str, text: str) -> None:
        """对外记一条对话（界面把"用户敲的那句"也放进来时用）。"""
        self._note(role, text)

    def _note(self, role: str, text: str) -> None:
        """记一条对话（role: user / assistant / system），给网页界面用。"""
        if not text:
            return
        self.transcript.append({"role": role, "text": str(text), "ts": time.strftime("%H:%M:%S")})

    def _cue(self, kind: str) -> None:
        """播放交互提示音；期间同样屏蔽麦克风，免得把自己的提示音录进去。"""
        if not self.cfg.agent.cues or not self.cfg.tts.enabled:
            return
        try:
            samples, rate = audio_io.tone(kind)
        except Exception:
            return
        self._speaking.set()
        try:
            # 0.5 是提示音相对人声的配比；总音量由 audio.play() 统一乘
            audio_io.play(
                samples, rate,
                device=self._out_device,
                gain=0.5,
                stop_event=self._stop_speak,
            )
        except Exception as exc:  # noqa: BLE001 - 提示音放不出来不该影响对话
            self.log("[agent] 提示音失败：" + str(exc))
        finally:
            self._speaking.clear()

    def _on_utterance(self, samples: np.ndarray) -> None:
        assert self.asr
        target = self._listen_target
        self._state = _WAIT
        started = time.perf_counter()
        try:
            text = self.asr.transcribe(samples, punctuate=True)
        except Exception as exc:  # noqa: BLE001 - 识别失败不能让采集循环停下来
            self.log("[asr] 识别失败：" + str(exc))
            self._state = _IDLE
            return
        elapsed = (time.perf_counter() - started) * 1000
        self.log("[asr] {:.0f}ms {:.2f}s 音频 → {!r}".format(elapsed, samples.size / self.cfg.audio.sample_rate, text))

        if target == "confirm":
            self._confirm_q.put(text)
            return
        if not text.strip():
            self._state = _IDLE
            return
        self.last_heard = text
        self._note("user", text)
        if rules.is_exit(text, self.cfg.agent.exit_words):
            self._spawn(self._say_notice, "好，我先退下了。")
            return
        self._spawn(self._handle_command, text)

    def _say_notice(self, text: str, epoch: int | None = None) -> None:
        epoch = self._epoch_of(epoch)
        if self._stale(epoch):
            return
        self._speak(text, kind="notice")
        if not self._stale(epoch):
            self._state = _IDLE

    def _handle_command(self, text: str, epoch: int | None = None) -> None:
        epoch = self._epoch_of(epoch)
        if self._stale(epoch):
            return
        self._state = _THINK
        self._interrupt.clear()
        self._stop_speak.clear()
        # 这一轮自己的取消信号：**不要**把共享的 self._interrupt 交给大脑 ——
        # 下一个任务开头会 clear() 它，于是旧任务从长工具里醒来时以为"没人让我停"，
        # 接着把整个任务跑完（用户看到的就是"上一个任务还在跑"）。
        token = self._turn_token(epoch)
        # 新一轮：上一轮的确认不再复用（同一个操作重新问一遍才安全）
        self._confirm_memory.clear()
        started = time.perf_counter()
        try:
            reply = self.brain.respond(text, confirm=self._confirm_for(token), interrupt=token)
        except Exception as exc:  # noqa: BLE001 - 大脑出错也要说一句，不能静默
            self.log("[agent] 处理出错：" + str(exc))
            reply = "刚才处理的时候出错了。"
        elapsed = time.perf_counter() - started
        if token.is_set():
            # 说清楚"上一个任务到此为止"，日志里能一眼看到它没有继续跑
            self.log("[agent] 任务已作废（{:.1f}s，被新任务/打断取代，不会再有动作）"
                     .format(elapsed))
            return
        self.log("[brain] {:.1f}s → {}".format(elapsed, reply[:120]))
        self.log("[agent] 这一轮说完：用时 %.1fs，回复 %d 字，追问窗口 %s"
                 % (elapsed, len(reply), "开" if self._follow_up_window(reply)[0] else "关"),
                 "detail")
        self.last_reply = reply
        self.turns += 1
        self._note("assistant", reply)
        if reply.strip():
            self._speak(reply, kind="reply")
        if token.is_set():
            return
        # 追问窗口：答完之后继续收音一小会儿，用户不用再喊一次唤醒词。
        # 这是「像人」和「像命令行」之间最关键的一处差别。
        window, why = self._follow_up_window(reply)
        if window > 0:
            self.log("[agent] 追问窗口 " + str(window) + "ms" + why)
            self._begin_listen("command", timeout_ms=window, follow_up=True)
            self._arm_listening()
        else:
            self._state = _IDLE

    def _follow_up_window(self, reply: str) -> tuple[int, str]:
        """答完这句要不要继续听，听多久。返回（毫秒，原因）。

        老版本只看 follow_up_ms：设了就一直留窗口，助手于是"赖着不走"，
        用户不接着问也得干等它超时。现在默认 auto，由 LLM 决定：

        - 模型调了 keep_listening（它反问了、或还要用户补充）→ 留；
        - 回复本身是个问句（漏调工具时的兜底）→ 留；
        - 其余（汇报完就完事）→ 回待命。
        """
        mode = str(self.cfg.agent.follow_up_mode or "auto").strip().lower()
        window = int(self.cfg.agent.follow_up_ms)
        if mode == "off" or window <= 0:
            return 0, ""
        if mode == "always":
            return window, "（配置要求每次都留）"
        if getattr(self.brain, "wants_followup", False):
            return window, "（模型说要接着听）"
        if reply.strip().endswith(("？", "?")):
            return window, "（刚反问了用户一句）"
        return 0, ""

    def _confirm_for(self, token: "TurnToken"):
        """把确认通道绑到这一轮的 token 上（工具层只认 (question, fingerprint)）。"""

        def ask(question: str, fingerprint: str = "") -> bool:
            return self._ask_confirm(question, fingerprint, token)

        return ask

    def _ask_confirm(self, question: str, fingerprint: str = "",
                     token: "TurnToken | None" = None) -> bool:
        """敏感操作前的语音确认：问一句，听一句，再判断同意与否。

        token 是这一轮的取消信号（绑 epoch）；没传就退回共享的 self._interrupt ——
        直接用共享那个会在"新任务把它 clear 掉"之后误判成"没人让我停"。
        """
        interrupt = token or self._interrupt
        if not self.cfg.agent.confirm.enabled:
            return True
        if self.tts is None and self.cfg.tts.enabled:
            self._ensure_tts()      # 刚在设置里打开的，这里补建，别误判成"关着"
        if self.tts is None or not self.cfg.tts.enabled:
            # 问不出口就没办法确认。宁可拒绝，也不能默默执行敏感操作。
            self.log("[agent] 需要确认，但语音播报已关闭，按拒绝处理")
            return False
        epoch = self._current_epoch()
        if self._stale(epoch):
            return False
        prompt = question or self.cfg.agent.confirm.prompt
        # 身份用**指纹**（工具+参数），不是提示文本：命令的提示都被"说人话"成
        # "列出文件"了，两条不同的命令会撞成同一个 key，上一句的同意就被
        # 当成这一句的同意 —— 这是真会出事的。
        key = fingerprint or self._confirm_key(prompt)
        remembered = self._confirm_memory.get(key)
        if remembered is not None:
            # 同一个操作这一轮已经问过：直接用上次的答案，别再问第二遍
            self.log("[agent] 这一步刚才已经确认过（" + ("同意" if remembered else "拒绝")
                     + "），不再重复问")
            return remembered
        answer = ""
        for attempt in range(2):
            # 先清队列**再开口**：上一句确认的迟到回答（用户答慢了、
            # 或者上一次根本没问就直接用缓存返回了）会躺在队列里，
            # 等到这次开口之后才被清 —— 那就成了"拿上一句的回答答这一句"。
            while not self._confirm_q.empty():
                self._confirm_q.get_nowait()
            self._note("system", prompt if attempt == 0 else ("（再问一次）" + prompt))
            self._cue("confirm")
            self._speak(prompt, kind="confirm")
            if interrupt.is_set() or self._stale(epoch):
                return False
            while not self._confirm_q.empty():  # 清掉过期回答
                self._confirm_q.get_nowait()
            self._begin_listen("confirm")
            self._arm_listening()
            timeout = self.cfg.agent.confirm.timeout_ms / 1000.0
            try:
                answer = self._confirm_q.get(timeout=timeout)
            except queue.Empty:
                answer = ""
            # 等待期间可能已经被唤醒词打断/被新任务取代，此时不能再碰状态
            if interrupt.is_set() or self._stale(epoch):
                self.log("[agent] 确认期间任务已被打断，忽略这次回答")
                return False
            self._state = _THINK
            if answer.strip():
                break
            if attempt == 0:
                # 一次没听清就问第二遍 —— 直接判"拒绝"的话，用户会看到模型
                # 又发起同一个操作、再问一遍，像是"刚才那句确认白说了"。
                self.log("[agent] 确认没听清，再问一次")
                self._speak("我没听清。" + self._action_words(prompt, ask=True)
                            + "说「确认」或者「取消」。", kind="notice")
        if not answer.strip():
            self.log("[agent] 确认两次都没听到回答，按拒绝处理")
            self._speak("还是没听到，我先不做了。", kind="notice")
            self._confirm_memory[key] = False
            return False

        value = answer.strip()
        self._note("user", value)
        self.log("[agent] 确认回答：" + value)
        verdict = self._confirm_verdict(value)
        if verdict is None:
            verdict = self.brain.judge(prompt, value)
        if verdict is None:
            # 语义判断不可用：既没听到明确的「确认」也没听到「取消」，保守拒绝
            self._speak("我没听准，为安全起见先不执行。", kind="notice")
            self._confirm_memory[key] = False
            return False
        self._confirm_memory[key] = bool(verdict)
        return bool(verdict)

    def _confirm_verdict(self, answer: str) -> bool | None:
        """用词表判「同意 / 拒绝」；判不准返回 None（交给语义判断）。

        不能简单写成 `word in answer`：默认词表里有「行」「是」「好」这些单字，
        而**否定说法里也含这些字** —— 实测「不太行」「不是」「不是这个意思」
        全被判成了同意，而确认是整个权限模型里唯一的人工闸门，听反了就是
        关机、执行命令被放行。所以按三条规则来：

        1. 否定词表 **先**看（和 config 里的注释一致：先判 no 才不会误放行）；
        2. 肯定词要看它前面有没有否定字、后面有没有反问尾巴 ——「不太行」的
           「行」前面是「不」，「好什么好」的「好」后面是「什么」，都不算数；
        3. 句子太长（> 16 字）或带犹豫说法时词表不硬判（长句里几乎一定夹着
           「是 / 行 / 好」），交给 LLM；LLM 也用不上就保守拒绝。
        """
        text = _FILLER.sub("", str(answer or "").strip())
        text = re.sub(r"[\s，,。.、！!~～]+$", "", text)
        if not text:
            return None
        confirm = self.cfg.agent.confirm
        for word in (confirm.no or []):
            if word and word in text:
                return False
        if len(text) > _WORDLIST_MAX or any(mark in text for mark in _DOUBT_WORDS):
            return None
        for word in (confirm.yes or []):
            if not word:
                continue
            start = 0
            while True:
                index = text.find(word, start)
                if index < 0:
                    break
                before = text[max(0, index - 3):index]
                after = text[index + len(word):index + len(word) + 2]
                # 前面是反问尾巴也算（「好什么好」的第二个「好」前面就是「好什么」）
                if (not any(ch in _NEGATIVE_CHARS for ch in before)
                        and not before.endswith(_DOUBT_TAILS)
                        and after not in _DOUBT_TAILS):
                    return True
                start = index + 1
        return None

    @staticmethod
    def _confirm_key(prompt: str) -> str:
        """同一句确认提示算同一个操作（工具名和参数都已经拼在里面了）。"""
        return re.sub(r"\s+", "", str(prompt or ""))[:120]

    @staticmethod
    def _action_words(prompt: str, ask: bool = False) -> str:
        """从确认提示里抠出"要做什么"，好用在第二遍的短问句里。

        确认提示本身的结尾就是"…，确认吗？"，直接拼第二遍会变成
        "我没听清。要要关机吗？" —— 所以这里去掉原句的疑问收尾，
        再按需要补一个"要不要…"。
        """
        text = str(prompt or "").strip()
        text = text.replace("，确认吗？", "").replace("确认吗？", "").strip("，。 ")
        if not text:
            return "要不要继续"
        if ask:
            if text.startswith("要"):
                return "要不要" + text[1:] + "？"
            return "要不要" + text + "？"
        return text[:24]

    # ───────────────────── 播报 / 打断 ─────────────────────

    def speak(self, text: str, kind: str = "reply") -> None:
        """对外播报一句（CLI 与外部调用用这个，不要碰 _speak）。"""
        self._speak(text, kind)

    def _ensure_tts(self) -> bool:
        """按需把合成引擎建起来。

        这条路上曾经有个静默的坑：**启动时 tts.enabled 是关的、后来在设置里
        打开** —— agent.load() 当时按 false 把 self.tts 留成了 None，
        而 _speak 只看到 None 就直接返回。用户的表现是「设置里明明打开了
        语音播报，它却一声不吭，也没有任何提示」。这里补建一次，
        建不起来就把原因写进日志（绝不静默）。
        """
        if self.tts is not None:
            return True
        with self._tts_lock:
            if self.tts is not None:
                return True
            try:
                self.tts = Tts(self.cfg)
                self.tts.device = self._out_device
                self.log("[tts] 语音播报已按新设置启用（引擎：" + self.tts.engine + "）")
                return True
            except Exception as exc:  # noqa: BLE001 - 建不起来要说清楚
                self.log("[tts] 语音播报打开了，但合成引擎起不来：" + str(exc)[:120])
                self.tts = None
                return False

    def _speak(self, text: str, kind: str = "reply") -> None:
        if not text.strip():
            return
        if self.tts is None and self.cfg.tts.enabled:
            self._ensure_tts()
        if self.tts is None or not self.cfg.tts.enabled:
            self.log("[tts-off] " + text)
            return
        self._speaking.set()
        self._speaking_since = time.monotonic()
        try:
            ok = self.tts.speak(text, kind=kind, stop_event=self._stop_speak,
                                device=self._out_device)
            if not ok and not self._stop_speak.is_set():
                # 不是被打断，那就是声卡没打开（被独占、设备刚切过）——
                # 再试一次；还不行就把这件事明确写进日志，别让它静悄悄地没了
                time.sleep(0.15)
                ok = self.tts.speak(text, kind=kind, stop_event=self._stop_speak,
                                    device=self._out_device)
                if not ok and not self._stop_speak.is_set():
                    self.log("[tts] 这条回复两次都没播出来：" + text[:40])
        except Exception as exc:  # noqa: BLE001
            self.log("[tts] 播报失败：" + str(exc))
        finally:
            self._speaking.clear()

    def _stop_background(self, why: str) -> None:
        """把还在后台跑的子代理一起叫停。

        用户喊一声"停"意思是"别做了"。子代理是独立线程：主对话回了待命，
        它还在一步接一步地调工具、烧 token、占着模型 —— 用户看到的就是
        "它说停下了，可日志里还在跑上一个任务"。
        **定时盯梢（watch）不动**：那是用户明确让它长期盯着的事，
        要停得说一句「别盯了」。
        """
        try:
            stopped = self.brain.subagents.cancel_all(why)
        except Exception as exc:  # noqa: BLE001 - 叫停失败不该影响打断本身
            self.log("[agent] 叫停子代理失败：" + str(exc)[:80])
            return
        if stopped:
            self.log("[agent] 已叫停 " + str(stopped) + " 个还在跑的子代理")

    def _barge_in(self) -> None:
        """喊唤醒词打断：停播 + 取消任务 + 重新开始听。

        顺序很重要：**先**推进 epoch 让旧任务作废，**再**发取消信号。
        反过来（发信号 → join 超时 → 清信号）会留下一个窗口：旧线程恰好在那时
        从阻塞里醒来，把刚设好的「正在听」状态改写回 thinking，于是采集循环只喂
        唤醒词、不再收指令，助手就「聋」了，得再喊一次才恢复。

        这一轮工作的取消信号是**绑 epoch 的 TurnToken**（见 TurnToken 的说明）：
        新任务一开始，旧任务的 token 就永远作废，哪怕共享的 _interrupt 被 clear 掉 ——
        这正是"打断之后旧任务还在跑"的根因。_interrupt / _stop_speak 只是让
        停播和确认等待尽快退出的快车道。
        """
        self.log("[agent] 打断当前任务")
        self._stop_background("用户打断")
        self._epoch += 1
        self._interrupt.set()
        self._stop_speak.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
            if worker.is_alive():
                # 说清楚它的性质：卡在某次工具调用/HTTP 里，但**已经作废**，
                # 不会再发起任何新的工具调用（用户看到这行就知道不用担心）
                self.log("[agent] 上一个任务卡在阻塞调用里（工具或网络），已作废："
                         "它不会再发起新的动作，日志里后续的 [tool] 行都是它收尾")
        self._stop_speak.clear()
        self._speaking.clear()
        self._on_wake()

    def _spawn(self, func: Callable[..., None], *args) -> None:
        """启动工作线程，并让上一个任务「过期」。

        过期的任务即使还在跑也再也不能改状态。没有这道保险会出现这种情况：
        用户喊唤醒词打断 → 旧任务其实卡在「等确认」里没退干净 → 2 秒后它超时醒来，
        把 state 又写回 think → 助手看起来就像「喊了它却不理我」。
        """
        worker = self._worker
        if worker is not None and worker.is_alive():
            self.log("[agent] 上一个任务还在跑，先等它结束")
            worker.join(timeout=1.0)
        self._epoch += 1
        self._worker = threading.Thread(
            target=self._run_worker, args=(func, self._epoch, args), daemon=True
        )
        self._worker.start()

    def _run_worker(self, func: Callable[..., None], epoch: int, args: tuple) -> None:
        self._local.epoch = epoch
        try:
            func(*args, epoch=epoch)
        except Exception as exc:  # noqa: BLE001 - 工作线程不能静默死掉
            self.log("[agent] 任务执行出错：" + str(exc))

    def _stale(self, epoch: int) -> bool:
        """这个任务的 epoch 是否已经被更新的任务取代。"""
        return epoch != self._epoch

    def _epoch_of(self, epoch: int | None) -> int:
        """没显式给代次就按「当前任务」处理。

        直接调用工作函数（CLI、测试、将来的扩展）时不该因为代次默认 0 而被
        误判成过期任务 —— 那种失败是静默的：函数一进去就 return，什么都不做。
        """
        return self._epoch if epoch is None else int(epoch)

    def _turn_token(self, epoch: int | None = None) -> "TurnToken":
        """这一轮的取消信号。绑定 epoch —— 新任务一开始，它立刻作废且不可复活。"""
        return TurnToken(self, self._epoch_of(epoch))

    def _current_epoch(self) -> int:
        """当前执行流的任务代次。

        在工作线程里就是它被创建时的代次；不在工作线程里（CLI 直接调用、
        测试同步调用）返回最新代次，也就是「永远不算过期」。
        """
        return int(getattr(self._local, "epoch", self._epoch))

    # ───────────────────── 文本模式（测试/CLI） ─────────────────────

    def ask(self, text: str, speak: bool = False, confirm: Callable[[str], bool] | None = None) -> str:
        """文本进、文本出；用于命令行验证、网页文本框与自动化测试。

        confirm 决定敏感操作怎么确认。**不传就等于一律拒绝** ——
        以前这里用「有 TTS 就直接放行」，结果输入「关机」真的会关机。
        """
        self._state = _THINK
        # 界面上敲的指令也绑一个 token：这样「打断」按钮能让在途的模型调用立刻放弃，
        # 不用干等那一轮跑完（以前 ask 没有取消信号，点了打断还得等）
        token = self._turn_token()
        try:
            reply = self.brain.respond(text, confirm=confirm or (lambda _question: False),
                                       interrupt=token)
        finally:
            self._state = _IDLE
        if token.is_set():
            self.log("[agent] 这条文字指令已被打断（不再继续）")
            return "（这条指令被打断了）"
        if speak and self.tts is not None:
            self._speak(reply, kind="reply")
        return reply

