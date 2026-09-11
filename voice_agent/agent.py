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

import queue
import random
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from . import audio as audio_io
from . import rules
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


class VoiceAgent:
    def __init__(self, cfg: Config, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = log
        self.brain = Brain(cfg, log=log)

        # 后台子代理做完事要主动汇报，但只能在「真的闲着」的时候开口 ——
        # 抢在用户正听的回答前面说话，听起来就是助手在自言自语。
        # 所以回调只负责入队，真正的播报交给采集循环择机做。
        self.brain.subagents.on_done = self._on_subagent_done
        self._announce: deque = deque(maxlen=5)
        self._announce_lock = threading.Lock()
        self._announce_busy = False

        self.wake: WakeWord | None = None
        self.vad: VadSegmenter | None = None
        self.asr: Asr | None = None
        self.tts: Tts | None = None
        self.mic: audio_io.Mic | None = None

        self._state = _IDLE
        self._listen_target = "command"       # command = 听指令，confirm = 听确认
        self._listen_started = 0.0
        self._listen_timeout_ms = 0
        self._listen_follow_up = False
        self._heard_speech = False
        self._speech_started_at = 0.0
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
        self._interrupt = threading.Event()    # 立刻取消当前任务
        self._confirm_q: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._epoch = 0
        # 工作线程自己的代次放在 thread-local 里：确认流程是「哪个任务问的就归哪个任务」，
        # 用实例变量会被后来的任务覆盖，导致正在等回答的任务被误判成「已过期」。
        self._local = threading.local()
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._in_device: int | None = None
        self._out_device: int | None = None

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

        if announce:
            # 唤醒词只打进日志：马上要开麦克风，从扬声器里念出来会变成自唤醒
            self.log("[agent] 唤醒词：" + "、".join(self.cfg.wake.keywords))
            self._speak("语音助手已就绪，随时听候吩咐。", kind="notice")

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
        self.log("[agent] 已开始监听，喊「" + self.cfg.wake.keywords[0] + "」唤醒我")

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
            # 后台子代理：界面拿它显示「派出去的活还在跑」
            "subagents": self.brain.subagents.snapshot(),
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
        self.log("[agent] 已打断当前任务")

    # ───────────────────── 音频回调（采集线程） ─────────────────────

    def _on_block(self, block: np.ndarray) -> None:
        assert self.wake and self.vad
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
            if self.vad.speech_detected:
                self._heard_speech = True
                if not self._speech_started_at:
                    self._speech_started_at = time.monotonic()
            utterance = self.vad.feed(block)
            if utterance is None:
                return
            min_samples = int(self.cfg.audio.sample_rate * self.cfg.agent.min_speech_ms / 1000)
            if utterance.size < min_samples:
                return
            self._on_utterance(utterance)

    def _check_timeout(self) -> None:
        """唤醒了却没等来一句提问 → 自动回到待命，别一直占着麦克风。

        两种情况都要兜住：
        1. 一直没人说话 —— 到 listen_timeout_ms 就收工；
        2. 听到了动静（咳嗽、翻书、电视声）但始终没形成完整句子 ——
           从开口那一刻起再给「一整句话」的时间，到点同样收工。
        早先的实现只要检测到过人声就永不过期，于是助手会一直卡在「正在听」。
        """
        if self._state != _LISTEN or self._listen_target != "command":
            return
        if not self._listen_started:
            return
        now = time.monotonic()
        if self._speech_started_at:
            deadline = self._speech_started_at + self.cfg.agent.max_utterance_ms / 1000.0
        else:
            deadline = self._listen_started + self._listen_timeout_ms / 1000.0
        if now <= deadline:
            return
        self._state = _IDLE
        if self.vad is not None:
            self.vad.reset()
        if self._listen_follow_up:
            # 追问窗口静默结束：不要每次答完都“叮”一声，太吵
            self.log("[agent] 追问窗口结束，回到待命")
        else:
            self.log("[agent] 等待超时，回到待命")
            self._note("system", "（没听到你说话）")
            self._cue("timeout")

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
            self.log("[agent] 汇报子代理结果：" + text[:40])
            self._speak("「" + name + "」那边有结果了：" + text, kind="notice")
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
        self._guard_samples = int(self.cfg.audio.sample_rate * 0.2)

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
        started = time.perf_counter()
        try:
            reply = self.brain.respond(text, confirm=self._ask_confirm, interrupt=self._interrupt)
        except Exception as exc:  # noqa: BLE001 - 大脑出错也要说一句，不能静默
            self.log("[agent] 处理出错：" + str(exc))
            reply = "刚才处理的时候出错了。"
        elapsed = time.perf_counter() - started
        if self._interrupt.is_set() or self._stale(epoch):
            self.log("[agent] 任务被打断（{:.1f}s）".format(elapsed))
            return
        self.log("[brain] {:.1f}s → {}".format(elapsed, reply[:120]))
        self.last_reply = reply
        self.turns += 1
        self._note("assistant", reply)
        if reply.strip():
            self._speak(reply, kind="reply")
        if self._interrupt.is_set() or self._stale(epoch):
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

    def _ask_confirm(self, question: str) -> bool:
        """敏感操作前的语音确认：问一句，听一句，再判断同意与否。"""
        if not self.cfg.agent.confirm.enabled:
            return True
        if self.tts is None or not self.cfg.tts.enabled:
            # 问不出口就没办法确认。宁可拒绝，也不能默默执行敏感操作。
            self.log("[agent] 需要确认，但语音播报已关闭，按拒绝处理")
            return False
        epoch = self._current_epoch()
        if self._stale(epoch):
            return False
        prompt = question or self.cfg.agent.confirm.prompt
        self._note("system", prompt)
        self._cue("confirm")
        self._speak(prompt, kind="confirm")
        if self._interrupt.is_set() or self._stale(epoch):
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
        if self._stale(epoch):
            self.log("[agent] 确认期间任务已被打断，忽略这次回答")
            return False
        self._state = _THINK
        if not answer.strip():
            self.log("[agent] 确认超时，按拒绝处理")
            self._speak("没听到回答，我先不做了。", kind="notice")
            return False

        value = answer.strip()
        self._note("user", value)
        self.log("[agent] 确认回答：" + value)
        if any(word and word in value for word in self.cfg.agent.confirm.no):
            return False
        if any(word and word in value for word in self.cfg.agent.confirm.yes):
            return True
        verdict = self.brain.judge(prompt, value)
        if verdict is None:
            # 语义判断不可用：既没听到明确的「确认」也没听到「取消」，保守拒绝
            self._speak("我没听准，为安全起见先不执行。", kind="notice")
            return False
        return verdict

    # ───────────────────── 播报 / 打断 ─────────────────────

    def speak(self, text: str, kind: str = "reply") -> None:
        """对外播报一句（CLI 与外部调用用这个，不要碰 _speak）。"""
        self._speak(text, kind)

    def _speak(self, text: str, kind: str = "reply") -> None:
        if not text.strip() or self.tts is None or not self.cfg.tts.enabled:
            if text.strip():
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

    def _barge_in(self) -> None:
        """喊唤醒词打断：停播 + 取消任务 + 重新开始听。

        顺序很重要：**先**推进 epoch 让旧任务作废，**再**发取消信号。
        反过来（发信号 → join 超时 → 清信号）会留下一个窗口：旧线程恰好在那时
        从阻塞里醒来，把刚设好的「正在听」状态改写回 thinking，于是采集循环只喂
        唤醒词、不再收指令，助手就「聋」了，得再喊一次才恢复。

        epoch 是权威的取消机制；_interrupt / _stop_speak 只是让旧任务尽快退出的
        快车道信号 —— 即使它们随后被新任务清掉，旧任务也不会再有任何副作用。
        """
        self.log("[agent] 打断当前任务")
        self._epoch += 1
        self._interrupt.set()
        self._stop_speak.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
            if worker.is_alive():
                self.log("[agent] 上一个任务仍卡在阻塞调用里，已作废（epoch 已推进）")
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
        try:
            reply = self.brain.respond(text, confirm=confirm or (lambda _question: False))
        finally:
            self._state = _IDLE
        if speak and self.tts is not None:
            self._speak(reply, kind="reply")
        return reply

