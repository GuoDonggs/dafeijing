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

    def verify_now(self, seconds: float = 3.0) -> dict:
        """录一小段并直接给分数，用来在设置里"试一下"。"""
        from . import audio as audio_io  # noqa: PLC0415

        device = audio_io.resolve_device(self.cfg.audio.input_device, "input")
        mic = audio_io.Mic(device=device, sample_rate=self.cfg.audio.sample_rate,
                           block_size=self.cfg.audio.block_size)
        mic.start()
        try:
            chunks = []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                block = mic.read(timeout=0.2)
                if block is not None:
                    chunks.append(block)
        finally:
            mic.close()
        if not chunks:
            return {"ok": False, "error": "没录到声音"}
        audio = np.concatenate(chunks)
        allowed, score, note = self.check(audio)
        return {"ok": True, "allowed": allowed, "score": round(score, 3), "note": note}

    def enroll_now(self, seconds: float = 3.0, name: str = "owner") -> dict:
        """录一小段并注册成声纹。"""
        from . import audio as audio_io  # noqa: PLC0415

        device = audio_io.resolve_device(self.cfg.audio.input_device, "input")
        mic = audio_io.Mic(device=device, sample_rate=self.cfg.audio.sample_rate,
                           block_size=self.cfg.audio.block_size)
        mic.start()
        try:
            chunks = []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                block = mic.read(timeout=0.2)
                if block is not None:
                    chunks.append(block)
        finally:
            mic.close()
        if not chunks:
            return {"ok": False, "error": "没录到声音"}
        return self.enroll(np.concatenate(chunks), name)

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
