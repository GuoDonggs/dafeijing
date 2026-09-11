# -*- coding: utf-8 -*-
"""语音四件套：唤醒词(KWS) / 端点检测(VAD) / 识别(ASR) / 合成(TTS)。

全部用 sherpa-onnx 本地推理，CPU 实时，不联网、不花钱、不上传音频。
每个类都刻意只暴露 feed()/run() 这样的小接口，方便主状态机串起来。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

from . import audio as audio_io
from . import textutil
from . import voices as voice_table
from .config import Config

__all__ = ["WakeWord", "VadSegmenter", "Asr", "Tts"]


# ───────────────────────────── 唤醒词 ─────────────────────────────


class WakeWord:
    """流式关键词检测：喂音频块，命中返回关键词。"""

    def __init__(self, cfg: Config, keywords_path: str | Path | None = None) -> None:
        import sherpa_onnx  # noqa: PLC0415

        paths = cfg.require("kws_tokens", "kws_encoder", "kws_decoder", "kws_joiner")
        self.cfg = cfg
        self.sample_rate = cfg.audio.sample_rate
        self.keywords = list(cfg.wake.keywords)
        self.threshold = float(cfg.wake.threshold)
        self.score = float(cfg.wake.score)
        self.cooldown_ms = int(cfg.wake.cooldown_ms)

        target = Path(keywords_path) if keywords_path else (
            Path(__file__).resolve().parent.parent / "build" / "keywords.generated.txt"
        )
        extra = None
        raw = cfg.raw.get("wake", {}) if isinstance(cfg.raw, dict) else {}
        if isinstance(raw.get("pinyin"), dict):
            extra = {str(k): tuple(str(v) for v in ([val] if isinstance(val, str) else val))
                     for k, val in raw["pinyin"].items()}
        self.keywords_path, self.problems = textutil.build_keywords_file(
            self.keywords, paths["kws_tokens"], target, extra
        )

        # KWS 模型只有几 MB，放 GPU 反而要额外的搬运开销，固定用 CPU
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(paths["kws_tokens"]),
            encoder=str(paths["kws_encoder"]),
            decoder=str(paths["kws_decoder"]),
            joiner=str(paths["kws_joiner"]),
            keywords_file=str(self.keywords_path),
            num_threads=cfg.speech.num_threads(2),
            sample_rate=int(self.sample_rate),
            feature_dim=80,
            max_active_paths=4,
            keywords_score=self.score,
            keywords_threshold=self.threshold,
            num_trailing_blanks=int(cfg.wake.num_trailing_blanks),
            provider="cpu",
        )
        self._stream = self._spotter.create_stream()
        self._last_hit = 0.0
        self.hits = 0
        self.seconds_fed = 0.0

    def feed(self, block: np.ndarray) -> str | None:
        """喂一块音频，命中唤醒词时返回关键词，否则 None。"""
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return None
        self._stream.accept_waveform(int(self.sample_rate), samples)
        self.seconds_fed += samples.size / float(self.sample_rate)
        keyword: str | None = None
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                keyword = str(result)
                self._spotter.reset_stream(self._stream)
                break
        if keyword is None:
            return None
        now = time.monotonic()
        if (now - self._last_hit) * 1000.0 < self.cooldown_ms:
            return None
        self._last_hit = now
        self.hits += 1
        return keyword

    def flush(self) -> str | None:
        """喂一小段静音，把「已经听到但还没确认」的关键词逼出来。

        实测：KWS 需要关键词之后再有点音频（num_trailing_blanks 个空白帧）
        才会报命中，所以「说完唤醒词就停住」的离线音频必须补静音，
        否则永远等不到结果。实时麦克风会自然补上，这里是为文件和测试准备的。
        """
        silence = np.zeros(int(self.sample_rate * 0.6), dtype=np.float32)
        hit: str | None = None
        for start in range(0, silence.size, int(self.sample_rate * 0.1)):
            hit = self.feed(silence[start : start + int(self.sample_rate * 0.1)]) or hit
        return hit

    def reset(self) -> None:
        self._spotter.reset_stream(self._stream)

    def reset_cooldown(self) -> None:
        """清掉防抖冷却，允许立刻再次唤醒。"""
        self._last_hit = 0.0


# ─────────────────────────── 端点检测 ───────────────────────────


class VadSegmenter:
    """silero-VAD 端点检测：自动判断「说完了」，把一句话切出来。"""

    def __init__(self, cfg: Config) -> None:
        import sherpa_onnx  # noqa: PLC0415

        paths = cfg.require("vad")
        self.sample_rate = int(cfg.audio.sample_rate)
        self._cfg = sherpa_onnx.VadModelConfig()
        self._cfg.silero_vad.model = str(paths["vad"])
        self._cfg.silero_vad.threshold = float(cfg.agent.vad_threshold)
        self._cfg.silero_vad.min_silence_duration = cfg.agent.min_silence_ms / 1000.0
        self._cfg.silero_vad.min_speech_duration = cfg.agent.min_speech_ms / 1000.0
        self._cfg.silero_vad.max_speech_duration = cfg.agent.max_utterance_ms / 1000.0
        self._cfg.sample_rate = self.sample_rate
        self._cfg.num_threads = 1
        self._cfg.provider = "cpu"
        self._vad = sherpa_onnx.VoiceActivityDetector(
            self._cfg, buffer_size_in_seconds=max(30, int(cfg.agent.max_utterance_ms / 1000) + 10)
        )

    def feed(self, block: np.ndarray) -> np.ndarray | None:
        """喂一块音频；攒够一句话就返回这句话的样本，否则 None。"""
        samples = np.asarray(block, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return None
        self._vad.accept_waveform(samples)
        # 多个 segment 正常不会同批出现；真出现就取最长的那段
        return self._drain()

    @property
    def speech_detected(self) -> bool:
        """当前是否已经听到人声（用于「唤醒了但你没说话」的超时判定）。"""
        try:
            return bool(self._vad.is_speech_detected())
        except Exception:
            return False

    def flush(self) -> np.ndarray | None:
        """强制收尾：把已经录到的一句话吐出来。"""
        self._vad.flush()
        return self._drain()

    def _drain(self) -> np.ndarray | None:
        utterance: np.ndarray | None = None
        while not self._vad.empty():
            chunk = np.asarray(self._vad.front.samples, dtype=np.float32).reshape(-1)
            self._vad.pop()
            if chunk.size and (utterance is None or chunk.size > utterance.size):
                utterance = chunk
        return utterance

    def reset(self) -> None:
        try:
            self._vad.reset()
        except Exception:
            pass


# ───────────────────────────── 语音识别 ─────────────────────────────


def _soft_clip(x: np.ndarray, knee: float, ceiling: float) -> np.ndarray:
    """软限幅：knee 以下原样通过，以上按 tanh 压，渐近贴近 ceiling。

    硬 clip 会削出方波，谐波就是那种"噼里啪啦"的失真；tanh 只是把顶峰磨圆，
    听感上自然得多。knee 以下增益严格是 1，所以小信号不会被改。
    """
    if ceiling <= 0 or x.size == 0:
        return x
    knee = max(1e-6, min(knee, ceiling))
    span = max(1e-6, ceiling - knee)
    mag = np.abs(x)
    over = mag > knee
    if not bool(np.any(over)):
        return x
    out = x.astype(np.float32).copy()
    head = mag[over]
    out[over] = np.sign(x[over]) * (knee + span * np.tanh((head - knee) / span))
    return out


def _with_provider_fallback(build, cfg: Config, what: str):
    """按配置的 provider 构建模型；GPU 起不来就回退 CPU 并说清楚。

    有的模型（尤其是量化过的）在 CUDA 上会因为算子不支持而初始化失败。
    与其让整个助手起不来，不如降级到 CPU，并把这件事明确写进日志和状态里。
    """
    provider = cfg.speech.provider
    try:
        return build(provider)
    except Exception as exc:  # noqa: BLE001
        if provider == "cpu":
            raise
        print("[speech] " + what + " 用 " + provider + " 初始化失败（" + str(exc)[:80]
              + "），回退到 CPU", file=sys.stderr, flush=True)
        cfg.speech.provider = "cpu"
        cfg.speech.provider_text = "CPU（GPU 初始化失败，已回退）"
        return build("cpu")


class Asr:
    """Paraformer 中文识别，可选补标点与同音字纠正。"""

    def __init__(self, cfg: Config) -> None:
        import sherpa_onnx  # noqa: PLC0415

        paths = cfg.require("asr_model", "asr_tokens")
        self.cfg = cfg
        self.sample_rate = int(cfg.audio.sample_rate)
        self._lock = threading.Lock()
        threads = cfg.speech.num_threads(cfg.asr.num_threads)
        self._rec = _with_provider_fallback(
            lambda provider: sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(paths["asr_model"]),
                tokens=str(paths["asr_tokens"]),
                num_threads=threads,
                sample_rate=self.sample_rate,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
                provider=provider,
            ),
            cfg,
            "语音识别",
        )
        self._punct = None
        if cfg.asr.punctuation and cfg.has("punct_model"):
            try:
                pcfg = sherpa_onnx.OfflinePunctuationConfig(
                    model=sherpa_onnx.OfflinePunctuationModelConfig(
                        ct_transformer=str(cfg.models["punct_model"]),
                        num_threads=1,
                        provider="cpu",
                    )
                )
                self._punct = sherpa_onnx.OfflinePunctuation(pcfg)
            except Exception:
                self._punct = None
        self.total_audio_ms = 0.0
        self.total_decode_ms = 0.0

    def transcribe(self, samples: np.ndarray, punctuate: bool = False) -> str:
        """识别一段 16k 单声道音频。"""
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return ""
        started = time.perf_counter()
        with self._lock:
            stream = self._rec.create_stream()
            stream.accept_waveform(self.sample_rate, audio)
            self._rec.decode_stream(stream)
            text = (stream.result.text or "").strip()
        self.total_audio_ms += 1000.0 * audio.size / self.sample_rate
        self.total_decode_ms += (time.perf_counter() - started) * 1000.0
        text = textutil.apply_corrections(text, self.cfg.asr.corrections)
        if punctuate and self._punct is not None and text:
            try:
                with self._lock:
                    text = (self._punct.add_punctuation(text) or text).strip()
            except Exception:
                pass
        return text

    @property
    def rtf(self) -> float:
        return (self.total_decode_ms / self.total_audio_ms) if self.total_audio_ms else 0.0


# ───────────────────────────── 语音合成 ─────────────────────────────


class Tts:
    """语音合成 + 分句流式播放，支持随时打断。

    两种引擎，用 tts.engine 选：
    - kokoro：100 个中文音色（3~57 女声、58~102 男声），可挑的余地大
    - vits  ：vits-zh-ll 的 5 人角色音，体积小、速度快

    两个坑都在"下标"上，代码里兜住：
    - Kokoro 的 0~2 号是**英文音色**，念中文会发闷发粗还带电流声，_pick_speaker
      会自动换成中文音色（配置写名字更稳，例如 voice: zf_070）；
    - 合成出来的原始波形峰值只有 0.2~0.5，按峰值硬拉到 0.9 会把底噪一起抬上来，
      所以响度用 RMS 定，再软限幅收尾。
    """

    def __init__(self, cfg: Config) -> None:
        import sherpa_onnx  # noqa: PLC0415

        self.cfg = cfg
        self._lock = threading.Lock()
        self.engine, self.engine_note = self._pick_engine(cfg)
        threads = cfg.speech.num_threads(cfg.tts.num_threads)

        def build(provider: str):
            if self.engine == "kokoro":
                paths = cfg.require("kokoro_model", "kokoro_voices", "kokoro_tokens")
                kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
                    model=str(paths["kokoro_model"]),
                    voices=str(paths["kokoro_voices"]),
                    tokens=str(paths["kokoro_tokens"]),
                    data_dir=str(cfg.kokoro_dir / "espeak-ng-data"),
                    dict_dir=str(cfg.kokoro_dir / "dict")
                    if (cfg.kokoro_dir / "dict").is_dir() else "",
                    lexicon=",".join(
                        str(cfg.kokoro_dir / name) for name in
                        ("lexicon-us-en.txt", "lexicon-zh.txt")
                        if (cfg.kokoro_dir / name).is_file()
                    ),
                    length_scale=1.0,
                )
                model_cfg = sherpa_onnx.OfflineTtsModelConfig(
                    kokoro=kokoro, num_threads=threads, provider=provider, debug=False
                )
            else:
                paths = cfg.require("tts_model", "tts_tokens", "tts_lexicon")
                vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(paths["tts_model"]),
                    lexicon=str(paths["tts_lexicon"]),
                    tokens=str(paths["tts_tokens"]),
                    data_dir="",
                    dict_dir=str(cfg.tts_dict_dir) if cfg.tts_dict_dir.is_dir() else "",
                )
                model_cfg = sherpa_onnx.OfflineTtsModelConfig(
                    vits=vits, num_threads=threads, provider=provider, debug=False
                )
            return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
                model=model_cfg,
                max_num_sentences=1,
                rule_fsts=",".join(str(p) for p in cfg.tts_rule_fsts),
                # 标点处的停顿：VITS 需要 1.15 才有"人味"；
                # Kokoro 本身停顿就长，再加就拖沓了。
                silence_scale=1.0 if self.engine == "kokoro" else 1.15,
            ))

        self._tts = _with_provider_fallback(build, cfg, "语音合成")
        audio_io.warm_resampler()  # 重采样要用的 scipy 提前导入，别卡在第一次播报上
        self.speaker_id, self.speaker_note = self._pick_speaker(cfg)
        # 分块策略：
        #   第一块切短（first_chars），只为让声音快点出来 —— 这是唯一一次等待；
        #   后面按整句走（sentence_chars），因为每切一刀，那一块都要重新起调，
        #   切得越碎语气越假，接缝还越多（接缝处有淡入淡出，听感上会一顿一顿）。
        #   之所以敢把块放大，是因为 speak() 里开了预取线程：播第一块时，
        #   后面的块已经在合成了。
        self.first_chars = 16 if self.engine == "kokoro" else 24
        self.sentence_chars = 46 if self.engine == "kokoro" else 60
        self.split_chars = "。！？；\n!?;"
        self.comma_chars = "，、,:："
        self.calls = 0
        self.total_audio_ms = 0.0
        self.total_synth_ms = 0.0
        self.device = None
        print("[tts] 引擎：" + self.engine + " — " + self.engine_note
              + "；音色：" + self.voice_label
              + "；算力：" + cfg.speech.provider_text, file=sys.stderr, flush=True)
        if self.speaker_note:
            print("[tts] 注意：" + self.speaker_note, file=sys.stderr, flush=True)

    def _pick_speaker(self, cfg: Config) -> tuple[int, str]:
        """定音色：配置里写名字（zf_070 / suyingxue）比写数字好，写数字也认。

        这里兜住最常见的一个坑：Kokoro 的 0~2 号是英文音色，拿它念中文会发闷、
        发粗、带电流声。配置写了英文音色就自动换中文音色，并在启动日志里说明。
        """
        total = self.num_speakers
        want = voice_table.resolve(self.engine, cfg.tts.voice, total)
        if want is None:
            want = int(cfg.tts.speaker_id)
        sid, note = voice_table.sanitize(self.engine, want, total)
        return sid, note

    @property
    def voice_label(self) -> str:
        return voice_table.label(self.engine, self.speaker_id)

    @staticmethod
    def _pick_engine(cfg: Config) -> tuple[str, str]:
        """决定用哪个合成引擎；请求了 kokoro 但模型不全时明确回退。"""
        want = (cfg.tts.engine or "vits").strip().lower()
        has_kokoro = cfg.has("kokoro_model", "kokoro_voices", "kokoro_tokens")
        if want == "kokoro" and has_kokoro:
            return "kokoro", "Kokoro 多语种（音色更多）"
        if want == "kokoro":
            return "vits", "VITS（配置要求 Kokoro，但模型不全，已回退）"
        return "vits", "VITS（配置指定）"

    @property
    def sample_rate(self) -> int:
        return int(self._tts.sample_rate)

    @property
    def num_speakers(self) -> int:
        return int(self._tts.num_speakers)

    def synthesize(self, text: str, kind: str = "reply") -> tuple[np.ndarray, int]:
        """合成一段音频（不播放）。"""
        clean = textutil.clean_for_tts(text)
        if not clean:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        style = self.cfg.tts.style(kind)
        # 音色以引擎解析出来的 self.speaker_id 为准（构造时已经把英文音色换掉了），
        # 只有 styles 里显式写了 speaker_id 才按语气覆盖
        chosen = style.get("speaker_id")
        speaker = self.speaker_id if chosen is None else int(chosen)
        if self.num_speakers > 0:
            speaker = max(0, min(speaker, self.num_speakers - 1))
        else:
            speaker = 0
        speed = float(style.get("speed", self.cfg.tts.speed))
        started = time.perf_counter()
        with self._lock:
            generated = self._tts.generate(clean, sid=speaker, speed=speed)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        samples = np.asarray(generated.samples, dtype=np.float32).reshape(-1)
        rate = int(generated.sample_rate)
        if self.cfg.tts.volume != 1.0:
            samples = samples * float(self.cfg.tts.volume)
        samples = self._polish(samples, rate)
        self.calls += 1
        self.total_audio_ms += 1000.0 * samples.size / max(1, rate)
        self.total_synth_ms += elapsed_ms
        return samples, rate

    def _polish(self, samples: np.ndarray, rate: int) -> np.ndarray:
        """响度与底噪：去直流 -> 按 RMS 定响度 -> 轻下扩张 -> 软限幅。

        为什么不再「把峰值拉到 0.9」：vits-zh-ll 的原始峰值只有 0.24 左右，
        按峰值拉到 0.9 要乘 3.7 倍（被 max_gain 截到 3.0），底噪跟着抬 9.5 dB，
        安静段落就是一片嘶声 —— 听感上很像"电流声"。按 RMS 定响度只需 1.9 倍，
        语音一样响，底噪低 4 dB；再配一道很轻的下扩张，静音处更干净。

        峰值仍然要管，但用软限幅而不是硬 clip：硬削会削出方波谐波，
        那才是真的刺耳。
        """
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return x
        x = x - float(np.mean(x))

        target = float(self.cfg.tts.target_rms)
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        if rms > 1e-6 and target > 0:
            gain = max(0.2, min(target / rms, float(self.cfg.tts.max_gain)))
            x = (x * gain).astype(np.float32)

        gate = float(self.cfg.tts.noise_gate)
        if gate > -90.0:
            x = self._expand(x, rate, gate)

        ceiling = float(self.cfg.tts.target_peak)
        if ceiling > 0:
            x = _soft_clip(x, ceiling * 0.82, ceiling)
        return x.astype(np.float32)

    @staticmethod
    def _expand(x: np.ndarray, rate: int, threshold_db: float,
                ratio: float = 2.0, max_cut_db: float = 8.0) -> np.ndarray:
        """很轻的下扩张：只在明显低于语音电平时往下压，压掉模型底噪。

        阈值默认 -55 dBFS —— 比正常语音低 35 dB 左右，所以只碰到真正的静音，
        不会拿软辅音开刀。增益包络按 30ms 平滑，避免帧边界出现咔哒声。
        """
        win = max(1, int(0.010 * rate))
        n = x.size
        pad = (-n) % win
        xp = np.concatenate([x, np.zeros(pad, dtype=np.float32)]) if pad else x
        frames = xp.reshape(-1, win).astype(np.float64)
        rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
        if rms.size == 0:
            return x
        level_db = 20.0 * np.log10(rms + 1e-12)
        below = threshold_db - level_db
        cut_db = np.where(below > 0.0, np.maximum(-max_cut_db, -below * (ratio - 1.0)), 0.0)
        gain = np.power(10.0, cut_db / 20.0)
        smooth = max(1, int(0.030 * rate / win))
        if smooth > 1 and gain.size > smooth:
            kernel = np.ones(smooth, dtype=np.float64) / smooth
            gain = np.convolve(gain, kernel, mode="same")
        envelope = np.repeat(gain, win)[:n].astype(np.float32)
        return (x * envelope).astype(np.float32)

    def speak(
        self,
        text: str,
        kind: str = "reply",
        stop_event: threading.Event | None = None,
        device: int | None = None,
    ) -> bool:
        """合成并播放；返回 False 表示被打断或设备出错。

        第一块短、后面的块长，中间的合成提前在后台线程里做（预取）。
        这样耳朵只等第一块，而整段话是按整句合成的 —— 语气比"每 22 个字
        重新起一次调"自然得多，接缝也少。
        """
        pieces = self.chunks(text)
        if not pieces:
            return True
        target = device if device is not None else self.device

        # 只有一块：直接合成直接播，省掉线程切换
        if len(pieces) == 1:
            if stop_event is not None and stop_event.is_set():
                return False
            samples, rate = self.synthesize(pieces[0], kind=kind)
            if samples.size == 0:
                return True
            # 总音量由 audio.play() 统一乘，这里不再乘一遍（会变成音量平方）
            return audio_io.play(samples, rate, device=target, stop_event=stop_event)

        out: queue.Queue = queue.Queue(maxsize=2)
        done = object()
        cancel = threading.Event()

        def stopped() -> bool:
            return cancel.is_set() or (stop_event is not None and stop_event.is_set())

        def offer(item) -> bool:
            """放得进去就放，被打断就放弃 —— 免得生产者永远堵在满队列上。"""
            while not cancel.is_set():
                try:
                    out.put(item, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def produce() -> None:
            try:
                for piece in pieces:
                    if stopped():
                        break
                    samples, rate = self.synthesize(piece, kind=kind)
                    if samples.size and not offer((samples, rate)):
                        break
            except Exception as exc:  # 合成崩了要让播放侧知道，不能干等
                offer(("error", exc))
            offer(done)

        worker = threading.Thread(target=produce, name="tts-prefetch", daemon=True)
        worker.start()
        finished = True
        try:
            while True:
                item = out.get()
                if item is done:
                    break
                if isinstance(item, tuple) and item and item[0] == "error":
                    print("[tts] 合成失败：" + repr(item[1]), file=sys.stderr, flush=True)
                    finished = False
                    break
                samples, rate = item
                if stopped():
                    finished = False
                    break
                if not audio_io.play(samples, rate, device=target, stop_event=stop_event):
                    finished = False
                    break
        finally:
            cancel.set()
        return finished

    def chunks(self, text: str) -> list[str]:
        """把要念的话切成合成单元：第一块短（快点出声），其余按整句。"""
        clean = textutil.clean_for_tts(text)
        if not clean:
            return []
        parts = textutil.split_sentences(
            clean, max_chars=self.sentence_chars, split_chars=self.split_chars)
        if not parts:
            parts = [clean]
        head = parts[0]
        if len(head) > self.first_chars:
            cut = self._cut_at(head, self.first_chars)
            if 0 < cut < len(head):
                parts = [head[:cut], head[cut:]] + list(parts[1:])
        return [p for p in (s.strip() for s in parts) if p]

    def _cut_at(self, text: str, limit: int) -> int:
        """在 limit 附近找停顿点；实在没有标点就硬切，但尽量别切在正中间。"""
        low = max(1, limit - 10)
        for i in range(min(len(text) - 1, limit + 6), low - 1, -1):
            if text[i] in self.comma_chars:
                return i + 1
        return min(limit, len(text))

    @property
    def rtf(self) -> float:
        return (self.total_synth_ms / self.total_audio_ms) if self.total_audio_ms else 0.0


