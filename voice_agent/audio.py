# -*- coding: utf-8 -*-
"""麦克风与扬声器 IO。

只用 sounddevice（PortAudio）一层薄封装，刻意不引入播放器线程池：
- 采集回调只做「拷贝 → 入队」，任何重活都放到消费线程，避免丢帧；
- 播放按小块写，每块前检查停止事件，所以打断是毫秒级的。
"""

from __future__ import annotations

import queue
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["Mic", "list_devices", "play", "resolve_device", "write_wav", "read_wav", "tone",
           "set_output_gain", "get_output_gain"]

# ── 总音量 ──
# 界面上那个音量条改的是它。放在模块级而不是配置对象里，是因为播报和提示音
# 走的是不同的调用路径（tts.speak / agent 的提示音），各读各的 cfg；收敛到
# 一处才能"拖一下立刻听到区别"，也不用重启引擎。
_MASTER_GAIN = 1.0


def set_output_gain(value: float) -> float:
    """设置总音量（0~2，1 = 原始音量）。返回实际生效的值。"""
    global _MASTER_GAIN
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _MASTER_GAIN
    if number != number:  # NaN
        return _MASTER_GAIN
    _MASTER_GAIN = max(0.0, min(number, 2.0))
    return _MASTER_GAIN


def get_output_gain() -> float:
    return _MASTER_GAIN

# 交互提示音（现场合成，不用任何音频素材）
_CUES: dict[str, tuple[tuple[float, float], ...]] = {
    "listen": ((880.0, 0.06), (1245.0, 0.08)),          # 上行两音：开始收音
    "confirm": ((659.0, 0.08), (0.0, 0.05), (659.0, 0.08)),  # 双音：需要你确认
    "done": ((587.0, 0.05), (880.0, 0.08)),             # 下行两音：办完了
    "timeout": ((392.0, 0.10), (0.0, 0.04), (330.0, 0.12)),  # 下降音：没听到
}


def tone(kind: str = "listen", sample_rate: int = 16000) -> tuple[np.ndarray, int]:
    """生成一段提示音。频率 0 表示静音间隔。"""
    pieces: list[np.ndarray] = []
    for freq, duration in _CUES.get(kind, _CUES["listen"]):
        count = max(1, int(sample_rate * duration))
        if freq <= 0:
            pieces.append(np.zeros(count, dtype=np.float32))
            continue
        timeline = np.arange(count, dtype=np.float32) / float(sample_rate)
        wave = 0.55 * np.sin(2.0 * np.pi * freq * timeline)
        fade = max(1, int(sample_rate * 0.008))
        envelope = np.ones(count, dtype=np.float32)
        envelope[:fade] = np.linspace(0.0, 1.0, fade, dtype=np.float32)
        envelope[-fade:] = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        pieces.append((wave * envelope).astype(np.float32))
    return np.concatenate(pieces), int(sample_rate)


def _sd():
    import sounddevice as sd  # noqa: PLC0415

    return sd


def list_devices() -> str:
    """返回人类可读的设备清单（供 CLI 打印）。"""
    sd = _sd()
    lines = []
    for index, dev in enumerate(sd.query_devices()):
        kind = []
        if dev.get("max_input_channels", 0) > 0:
            kind.append("输入")
        if dev.get("max_output_channels", 0) > 0:
            kind.append("输出")
        lines.append(
            "{:>2}  {:<8} {}  ({:.0f} Hz)".format(
                index, "/".join(kind), dev.get("name", "?"), dev.get("default_samplerate", 0)
            )
        )
    return "\n".join(lines)


def resolve_device(spec: Any, kind: str = "input") -> int | None:
    """把配置里的 null / 序号 / 名称子串解析成 sounddevice 设备号。"""
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return None
    sd = _sd()
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        return int(spec)
    key = str(spec).strip().lower()
    channels = "max_input_channels" if kind == "input" else "max_output_channels"
    for index, dev in enumerate(sd.query_devices()):
        if dev.get(channels, 0) > 0 and key in str(dev.get("name", "")).lower():
            return index
    raise RuntimeError("找不到名字包含 " + repr(spec) + " 的" + ("麦克风" if kind == "input" else "扬声器"))


class Mic:
    """常驻麦克风采集：回调入队，消费方按块取。"""

    def __init__(
        self,
        device: int | None = None,
        sample_rate: int = 16000,
        block_size: int = 512,
        gain: float = 1.0,
        max_blocks: int = 200,
    ) -> None:
        self.device = device
        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.gain = float(gain)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=max_blocks)
        self._stream = None
        self.dropped = 0
        self.status_flags = 0
        self.level = 0.0          # 最近一块的 RMS，给界面画电平条
        self.peak = 0.0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            self.status_flags += 1
        block = np.asarray(indata[:, 0], dtype=np.float32)
        if self.gain != 1.0:
            block = block * self.gain
        # 512 个样本的均方根，开销可以忽略，但界面能立刻看出麦克风有没有在工作
        if block.size:
            self.level = float(np.sqrt(np.mean(np.square(block))))
            self.peak = float(np.max(np.abs(block)))
        try:
            self._queue.put_nowait(block.copy())
        except queue.Full:
            # 消费方落后了：丢最旧的一块，宁可缺一点也不要无限堆积延迟
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(block.copy())
            except queue.Empty:
                pass

    def start(self) -> None:
        sd = _sd()
        stream = sd.InputStream(
            device=self.device,
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        try:
            stream.start()
        except Exception:
            # 启动失败也要把底层句柄关掉，否则这个输入流会一直占着麦克风
            try:
                stream.close()
            except Exception:
                pass
            raise
        self._stream = stream

    def read(self, timeout: float = 0.2) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def flush(self) -> None:
        """丢掉积压的音频，用于「说完一句、重新开始听」。"""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    @property
    def latency_blocks(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        # 先把句柄摘下来再关：stop() 抛异常时如果留着 self._stream，
        # 下一次 close() 才会重试；直接置 None + 跳过 close() 就是永久泄漏。
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for action in (stream.stop, stream.close):
            try:
                action()
            except Exception:
                pass


def _fade(samples: np.ndarray, sample_rate: int, fade_ms: float = 8.0) -> np.ndarray:
    """首尾加短淡入淡出，去掉爆音。"""
    n = int(sample_rate * fade_ms / 1000)
    if n <= 1 or samples.size <= 2 * n:
        return samples
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = samples.copy()
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


# 设备原生采样率只查一次，之后复用
_NATIVE_RATE: dict[Any, int] = {}


def warm_resampler() -> None:
    """后台把 scipy.signal 先导进来。

    它首次导入要 1 秒左右（卷积那部分的一次性开销是 900ms+），正好会卡在
    第一次播报上。开个后台线程预热，启动不受影响，第一次说话也不用等。
    """
    def work() -> None:
        try:
            from scipy.signal import resample_poly  # noqa: F401,PLC0415
        except Exception:
            pass

    threading.Thread(target=work, name="resampler-warmup", daemon=True).start()


def _native_rate(device: int | None, sample_rate: int) -> int:
    """设备最喜欢哪个采样率。

    Windows 下 PortAudio 默认走 MME，而 MME 设备通常是 44100/48000。喂它 16000
    的语音时，这层会用自己的重采样顶上 —— 质量一般，高频容易发毛、带毛刺。
    所以先问设备要原生率，自己用多相滤波器转好再送进去，绕开它。
    """
    if device not in _NATIVE_RATE:
        try:
            info = _sd().query_devices(device, kind="output")
            _NATIVE_RATE[device] = int(info.get("default_samplerate") or 0)
        except Exception:
            _NATIVE_RATE[device] = 0
    native = _NATIVE_RATE[device]
    if native <= 0 or native == int(sample_rate):
        return int(sample_rate)
    if not (8000 <= native <= 192000):
        return int(sample_rate)
    return native


def _write_out(audio: np.ndarray, rate: int, device: int | None,
               stop_event: threading.Event | None, chunk_ms: int) -> bool:
    """按给定采样率开一条流写出去。"""
    sd = _sd()
    chunk = max(160, int(rate * chunk_ms / 1000))
    finished = True
    with sd.OutputStream(
        device=device, samplerate=int(rate), channels=1, dtype="float32"
    ) as stream:
        for start in range(0, audio.size, chunk):
            if stop_event is not None and stop_event.is_set():
                finished = False
                break
            stream.write(audio[start : start + chunk])
    return finished


def play(
    samples: np.ndarray,
    sample_rate: int,
    device: int | None = None,
    gain: float = 1.0,
    stop_event: threading.Event | None = None,
    chunk_ms: int = 80,
) -> bool:
    """阻塞播放，可被 stop_event 立即打断。返回是否完整播完。"""
    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return True
    source_rate = int(sample_rate)
    # 调用方的 gain（各场景自己的配比）× 总音量（用户拖的那个条）
    total = float(gain) * _MASTER_GAIN
    if total != 1.0:
        audio = audio * total
    audio = np.clip(audio, -1.0, 1.0)

    rate = _native_rate(device, source_rate)
    if rate != source_rate:
        audio = resample(audio, source_rate, rate)
    audio = _fade(audio, rate)

    try:
        # 构造函数也可能抛（设备被独占 / 采样率不支持），所以整段都要包住
        return _write_out(audio, rate, device, stop_event, chunk_ms)
    except Exception:
        if rate == source_rate:
            return False
    # 原生率开不起来（有些独占设备的默认值不能直接用），退回原采样率让系统去转
    fallback = _fade(np.clip(np.asarray(samples, dtype=np.float32).reshape(-1)
                             * total, -1.0, 1.0), source_rate)
    try:
        return _write_out(fallback, source_rate, device, stop_event, chunk_ms)
    except Exception:
        return False


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int) -> Path:
    """把 float32 单声道样本写成 16-bit PCM wav。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm = (data * 32767.0).astype("<i2")
    with wave.open(str(target), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(int(sample_rate))
        fh.writeframes(pcm.tobytes())
    return target


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读 wav 成 float32 单声道样本，返回 (samples, sample_rate)。"""
    with wave.open(str(path), "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        frames = fh.readframes(fh.getnframes())
    if width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError("不支持的位深：" + str(width))
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data.astype(np.float32), rate


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """线性重采样；只在测试/播放采样率不一致时用。"""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    try:
        from scipy.signal import resample_poly  # noqa: PLC0415

        from math import gcd

        factor = gcd(int(src_rate), int(dst_rate))
        return resample_poly(samples, dst_rate // factor, src_rate // factor).astype(np.float32)
    except Exception:
        n = int(round(samples.size * dst_rate / float(src_rate)))
        return np.interp(
            np.linspace(0.0, 1.0, n, endpoint=False),
            np.linspace(0.0, 1.0, samples.size, endpoint=False),
            samples,
        ).astype(np.float32)
