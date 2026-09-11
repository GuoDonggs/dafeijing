# -*- coding: utf-8 -*-
"""声纹：只有主人的声音才能唤醒。

用 sherpa-onnx 的说话人向量模型（3D-Speaker ERes2Net，中文）把一段语音压成
512 维向量，再和注册时存下来的向量比余弦相似度。

两个刻意的设计：

1. **默认关闭**。声纹是一件"配错了就把自己锁在门外"的功能，
   所以必须由用户显式打开；打开但没注册时，程序会**放行并反复提醒**，
   而不是把人挡在外面 —— 那样只能改配置文件才能恢复。
2. **只在唤醒那一下验证**。唤醒词通常 0.8~1.5 秒，足够算一次向量，
   又不用等整句话说完，用户感觉不到多出来的这一步。
"""

from __future__ import annotations

import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .config import PROJECT_ROOT, Config

__all__ = ["Voiceprint", "PROFILE_PATH"]

PROFILE_PATH = PROJECT_ROOT / "build" / "voiceprint.json"
MODEL_DIR = PROJECT_ROOT / "models" / "speaker"
DEFAULT_THRESHOLD = 0.55

# ── 录音体检的门槛 ──
# 为什么要体检："录了 3 秒"和"说了 3 秒"是两回事。麦克风静音、用户没开口、
# 离麦太远，录到的都是一段近乎静音的音频 —— 拿它注册出来的声纹谁都不像，
# 之后要么认不出主人、要么谁都能唤醒，而且极难查。所以在**注册之前**就拦下来。
MIN_SPEECH_S = 1.2        # 一段里至少要有这么久的"确实在说话"
SPEECH_RMS = 0.010        # 单帧 RMS 超过它算说话
MIN_PEAK = 0.030          # 整段峰值低于它 = 基本没声音
CLIP_PEAK = 0.985         # 峰值贴着 1.0 = 爆音，多半离麦太近
FRAME_S = 0.02


def find_model(explicit: str = "") -> Path | None:
    """找声纹模型：先看配置，再在 models/speaker 里挑一个 .onnx。"""
    if explicit:
        path = Path(explicit)
        return path if path.is_file() else None
    if MODEL_DIR.is_dir():
        for candidate in sorted(MODEL_DIR.glob("*.onnx")):
            return candidate
    return None


class Voiceprint:
    """声纹检测器。模型按需加载，没开这个功能就完全不碰它。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.enabled = bool(getattr(cfg.speaker, "enabled", False))
        self.profile_path = Path(getattr(cfg.speaker, "profile", "") or PROFILE_PATH)
        self._extractor = None
        self._manager = None
        self._dim = 0
        self._lock = threading.Lock()
        self.last_score = 0.0
        self.rejected = 0
        # 录音进度：界面每 300ms 轮一次，用来显示"还剩几秒 / 现在有没有声音"
        self._progress: dict = {"state": "idle", "seconds": 0.0, "speech": 0.0,
                                "level": 0.0, "left": 0.0, "hint": ""}
        self.accepted = 0
        self._model = find_model(str(getattr(cfg.speaker, "model", "") or ""))
        self._vectors: dict[str, list[float]] = {}
        self._meta: dict[str, int] = {}     # 每个人录了几次
        self.load_profile()

    @property
    def threshold(self) -> float:
        """阈值每次现读配置 —— 在界面上调完立刻生效，不用重启引擎。"""
        return float(getattr(self.cfg.speaker, "threshold", DEFAULT_THRESHOLD))

    # ── 状态 ──
    @property
    def available(self) -> bool:
        """有模型、并且用户打开了开关。"""
        return self.enabled and self._model is not None

    @property
    def enrolled(self) -> bool:
        return bool(self._vectors)

    @property
    def model_name(self) -> str:
        return self._model.name if self._model else ""

    def status_text(self) -> str:
        if not self.enabled:
            return "未开启"
        if self._model is None:
            return "缺少声纹模型（models/speaker/*.onnx）"
        if not self._vectors:
            return "已开启，但还没录声纹 —— 现在任何人都能唤醒"
        total = sum(self._meta.values()) or len(self._vectors)
        return "已开启，阈值 " + str(self.threshold) + "，已录 " + str(total) + " 条"

    # ── 模型 ──
    def _ensure_model(self) -> bool:
        if self._extractor is not None:
            return True
        if self._model is None:
            return False
        try:
            import sherpa_onnx  # noqa: PLC0415

            settings = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self._model),
                num_threads=self.cfg.speech.num_threads(2),
                provider="cpu",       # 声纹模型很小，GPU 的搬运开销反而更大
                debug=False,
            )
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(settings)
            self._dim = int(self._extractor.dim)
            self._manager = sherpa_onnx.SpeakerEmbeddingManager(self._dim)
            for name, vector in self._vectors.items():
                self._manager.add(name, vector)
            print("[speaker] 声纹模型已加载：" + self._model.name
                  + "（" + str(self._dim) + " 维）", file=sys.stderr, flush=True)
            return True
        except Exception as exc:  # noqa: BLE001
            print("[speaker] 声纹模型加载失败：" + str(exc)[:100], file=sys.stderr, flush=True)
            self._extractor = None
            return False

    def embed(self, samples: np.ndarray) -> list[float] | None:
        """把一段 16k 单声道音频压成声纹向量。"""
        audio = np.asarray(samples, dtype=np.float32).reshape(-1)
        if audio.size < 8000:      # 太短算不准，半秒以下直接放弃
            return None
        with self._lock:
            if not self._ensure_model():
                return None
            try:
                stream = self._extractor.create_stream()
                stream.accept_waveform(self.cfg.audio.sample_rate, audio)
                stream.input_finished()
                return list(self._extractor.compute(stream))
            except Exception as exc:  # noqa: BLE001
                print("[speaker] 提取声纹失败：" + str(exc)[:80], file=sys.stderr, flush=True)
                return None

    # ── 注册 ──
    def enroll(self, samples: np.ndarray, name: str = "owner") -> dict:
        """录一条声纹。录 3 条平均一下更稳。"""
        vector = self.embed(samples)
        if vector is None:
            return {"ok": False, "error": "这段音频太短或模型不可用，请说长一点（1 秒以上）"}
        existing = self._vectors.get(name)
        count = int(self._meta.get(name, 0)) + 1
        if existing:
            # 多录几次做加权平均：单次录音总带环境噪声，平均之后判定更稳
            merged = [(a * (count - 1) + b) / count for a, b in zip(existing, vector)]
            self._vectors[name] = merged
        else:
            self._vectors[name] = vector
        self._meta[name] = count
        if self._manager is not None:
            self._manager.add(name, self._vectors[name])
        self.save_profile()
        return {"ok": True, "name": name, "count": count, "dim": self._dim}

    def clear(self, name: str = "") -> None:
        if name:
            self._vectors.pop(name, None)
            self._meta.pop(name, None)
        else:
            self._vectors.clear()
            self._meta.clear()
        self.save_profile()

    # ── 验证 ──
    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def check(self, samples: np.ndarray) -> tuple[bool, float, str]:
        """返回（是否放行, 相似度, 说明）。

        没开开关、没模型、没注册 —— 一律放行，并说明原因。
        只有「开着 + 有模型 + 已注册」时才真的拦人。
        """
        if not self.enabled:
            return True, 0.0, "未开启声纹"
        if self._model is None:
            return True, 0.0, "缺少声纹模型，已放行"
        if not self._vectors:
            return True, 0.0, "还没录声纹，已放行（录一次之后才会真正校验）"
        vector = self.embed(samples)
        if vector is None:
            # 算不出向量（音频太短）时不拦：宁可漏放，也不要因为一声咳嗽把主人关在门外
            return True, 0.0, "这段音频太短，跳过声纹校验"
        best = 0.0
        for name, reference in self._vectors.items():
            best = max(best, self._cosine(vector, reference))
        self.last_score = best
        if best >= self.threshold:
            self.accepted += 1
            return True, best, "声纹匹配 " + str(round(best, 3))
        self.rejected += 1
        return False, best, "声纹不匹配（" + str(round(best, 3)) + " < " + str(self.threshold) + "）"

    # ── 录音（带引导与体检）──

    def progress(self) -> dict:
        """当前录音进度，给界面轮询用。"""
        return dict(self._progress)

    def _set_progress(self, **fields: Any) -> None:
        self._progress.update(fields)

    @staticmethod
    def inspect(audio: np.ndarray, sample_rate: int) -> dict:
        """给一段录音做体检：到底有没有人说话、有没有爆音。

        返回 {seconds, speech_seconds, peak, rms, level, problem, hint}；
        problem 为空串表示这段可以用。
        """
        data = np.asarray(audio, dtype=np.float32).reshape(-1)
        if data.size == 0:
            return {"seconds": 0.0, "speech_seconds": 0.0, "peak": 0.0, "rms": 0.0,
                    "problem": "empty", "hint": "什么都没录到，检查一下麦克风"}
        width = max(1, int(FRAME_S * sample_rate))
        frames = [data[i:i + width] for i in range(0, data.size - width + 1, width)]
        rms_list = [float(np.sqrt(np.mean(f ** 2))) if f.size else 0.0 for f in frames]
        peak = float(np.max(np.abs(data)))
        rms = float(np.sqrt(np.mean(data ** 2)))
        speech_frames = sum(1 for value in rms_list if value >= SPEECH_RMS)
        speech_seconds = speech_frames * FRAME_S

        problem, hint = "", ""
        if peak < MIN_PEAK:
            problem, hint = "silent", "没听到声音。确认麦克风没被静音，然后靠近一点再说一次"
        elif speech_seconds < MIN_SPEECH_S:
            problem, hint = ("too_short",
                             "只听到 " + str(round(speech_seconds, 1)) + " 秒说话声，"
                             "太短了。请完整说一句话，比如「今天天气不错，我想听点音乐」")
        elif peak >= CLIP_PEAK:
            problem, hint = "clipping", "声音太大了（爆音），离麦克风远一点再说一次"
        return {"seconds": round(data.size / sample_rate, 2),
                "speech_seconds": round(speech_seconds, 2),
                "peak": round(peak, 3), "rms": round(rms, 4),
                "level": round(min(1.0, rms * 8), 3),
                "problem": problem, "hint": hint}

    def record_take(self, seconds: float = 4.0) -> dict:
        """录一段并体检。返回 {ok, samples, ...体检结果}。

        seconds 是**最长**录多久：一旦说满了 MIN_SPEECH_S 而且已经录够 2.5 秒，
        就提前收工 —— 让用户等满 4 秒是没必要的，而且他们往往说完就不吭声了。
        """
        from . import audio as audio_io  # noqa: PLC0415

        rate = int(self.cfg.audio.sample_rate)
        self._set_progress(state="recording", seconds=0.0, speech=0.0, level=0.0,
                           left=round(seconds, 1), hint="请说话")
        device = audio_io.resolve_device(self.cfg.audio.input_device, "input")
        mic = audio_io.Mic(device=device, sample_rate=rate,
                           block_size=self.cfg.audio.block_size)
        mic.start()
        try:
            chunks: list[np.ndarray] = []
            started = time.monotonic()
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= seconds:
                    break
                block = mic.read(timeout=0.2)
                if block is None:
                    continue
                chunks.append(np.asarray(block, dtype=np.float32).reshape(-1))
                if chunks:
                    recent = np.concatenate(chunks[-8:])
                    level = float(np.sqrt(np.mean(recent ** 2))) if recent.size else 0.0
                    self._set_progress(seconds=round(elapsed, 1), level=round(min(1.0, level * 8), 3),
                                       left=round(max(0.0, seconds - elapsed), 1))
        finally:
            mic.close()
        if not chunks:
            self._set_progress(state="error", hint="没录到任何音频，检查一下麦克风")
            return {"ok": False, "samples": np.zeros(0, dtype=np.float32),
                    "problem": "empty", "hint": "什么都没录到，检查一下麦克风"}
        audio = np.concatenate(chunks)
        report = self.inspect(audio, rate)
        self._set_progress(
            state="ok" if not report["problem"] else "error",
            seconds=report["seconds"], speech=report["speech_seconds"],
            level=report["level"], hint=report["hint"] or "听起来很清楚，可以了",
        )
        return {"ok": not report["problem"], "samples": audio, **report}

    def verify_now(self, seconds: float = 3.0) -> dict:
        """录一小段并直接给分数，用来在设置里"试一下"。"""
        take = self.record_take(seconds)
        if not take.get("ok"):
            return {"ok": False, "error": take.get("hint") or "这段录音不能用"}
        allowed, score, note = self.check(take["samples"])
        return {"ok": True, "allowed": allowed, "score": round(score, 3), "note": note}

    def enroll_now(self, seconds: float = 4.0, name: str = "owner") -> dict:
        """录一段、体检、再注册。

        体检不过就**不写进档案**，并把原因原样返回给界面 —— 录进一段静音，
        比没录还糟：之后要么认不出主人，要么谁都能唤醒。
        """
        take = self.record_take(seconds)
        if not take.get("ok"):
            return {"ok": False, "error": take.get("hint") or "这段录音不能用",
                    "problem": take.get("problem", ""),
                    "speech_seconds": take.get("speech_seconds", 0.0)}

        # 第 2、3 次录的时候顺手比一比：跟已有的差太多，多半换了个人或者离麦远近差太多
        if self._vectors.get(name):
            vector = self.embed(take["samples"])
            if vector is not None:
                similarity = self._cosine(self._vectors[name], vector)
                if similarity < 0.35:
                    self._set_progress(state="error",
                                       hint="这次听起来和上次差得有点多，建议重录一次")
                    return {"ok": False, "problem": "inconsistent",
                            "error": "这次的声音和之前录的差得有点多（相似度 "
                                     + str(round(similarity, 2)) + "）。"
                                     "同样的距离、同样的音量再说一次",
                            "score": round(similarity, 3)}
        result = self.enroll(take["samples"], name)
        if result.get("ok"):
            result["speech_seconds"] = take.get("speech_seconds", 0.0)
            result["hint"] = take.get("hint", "")
        return result

    # ── 存档 ──
    def load_profile(self) -> None:
        if not self.profile_path.is_file():
            return
        try:
            data = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        vectors = data.get("vectors") or {}
        self._vectors = {str(k): [float(x) for x in v] for k, v in vectors.items() if v}
        self._meta = {str(k): int(v) for k, v in (data.get("counts") or {}).items()}

    def save_profile(self) -> None:
        self.profile_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": 1,
            "model": self.model_name,
            "dim": self._dim,
            "threshold": self.threshold,
            "vectors": self._vectors,
            "counts": getattr(self, "_meta", {}),
        }
        self.profile_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
