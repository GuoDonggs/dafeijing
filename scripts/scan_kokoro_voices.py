# -*- coding: utf-8 -*-
"""扫描 Kokoro 全部音色：音高、噪声、电流声指标。

没有耳朵就用指标代替：
  f0        基频中位数（Hz）—— 越低越"粗"，女声一般 180~260
  flat      谱平坦度（0 调性 / 1 噪声）—— 越高越像电流声
  hf        6kHz 以上能量占比 —— 齿音与嘶嘶声
  rough     帧间能量抖动 —— 沙哑、毛刺
  clicks    |Δx| 突跳次数 —— 爆音、啪啪声
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from voice_agent import voices as kv  # noqa: E402

import sherpa_onnx  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
KOKORO = pathlib.Path(r"D:\aicode\dsh\voice-assistant\models\kokoro-int8-multi-lang-v1_1")

TEXTS = [
    "你好，我是小爱同学，有什么可以帮你的吗？",
    "好的，已经帮你打开了浏览器。",
]


def build():
    cfg = sherpa_onnx.OfflineTtsKokoroModelConfig(
        model=str(KOKORO / "model.int8.onnx"),
        voices=str(KOKORO / "voices.bin"),
        tokens=str(KOKORO / "tokens.txt"),
        data_dir=str(KOKORO / "espeak-ng-data"),
        dict_dir=str(KOKORO / "dict") if (KOKORO / "dict").is_dir() else "",
        lexicon=",".join(str(KOKORO / n) for n in ("lexicon-us-en.txt", "lexicon-zh.txt")
                         if (KOKORO / n).is_file()),
        length_scale=1.0,
    )
    return sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=cfg, num_threads=8, provider="cpu", debug=False),
        max_num_sentences=1, silence_scale=1.0,
    ))


def f0_track(x: np.ndarray, sr: int) -> np.ndarray:
    """自相关法基频，只取有声帧。"""
    win, hop = int(0.040 * sr), int(0.010 * sr)
    lo, hi = int(sr / 400), int(sr / 70)
    out = []
    for s in range(0, max(1, len(x) - win), hop):
        seg = x[s:s + win]
        if seg.size < win:
            break
        if float(np.sqrt(np.mean(seg ** 2))) < 0.02:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[win - 1:]
        if ac[0] <= 1e-9:
            continue
        ac = ac / ac[0]
        seg2 = ac[lo:hi]
        if seg2.size == 0:
            continue
        k = int(seg2.argmax())
        if seg2[k] > 0.35:
            out.append(sr / (lo + k))
    return np.asarray(out, dtype=np.float32)


def metrics(x: np.ndarray, sr: int) -> dict:
    if x.size == 0:
        return {}
    peak = float(np.max(np.abs(x)))
    if peak > 0:
        x = x / peak * 0.8
    win, hop = int(0.040 * sr), int(0.010 * sr)
    flats, hfs, rms_list = [], [], []
    for s in range(0, max(1, len(x) - win), hop):
        seg = x[s:s + win]
        if seg.size < win:
            break
        r = float(np.sqrt(np.mean(seg ** 2)))
        rms_list.append(r)
        if r < 0.02:
            continue
        spec = np.abs(np.fft.rfft(seg * np.hanning(win))) ** 2 + 1e-12
        freqs = np.fft.rfftfreq(win, 1.0 / sr)
        flats.append(float(np.exp(np.mean(np.log(spec))) / np.mean(spec)))
        hfs.append(float(spec[freqs > 6000].sum() / spec.sum()))
    f0 = f0_track(x, sr)
    d = np.abs(np.diff(x))
    rms = float(np.sqrt(np.mean(x ** 2))) + 1e-9
    clicks = int(np.sum(d > 8.0 * rms))
    med_e = float(np.median(rms_list)) if rms_list else 0.0
    rough = float(np.mean(np.abs(np.diff(np.asarray(rms_list)))) / med_e) if med_e else 0.0
    return {
        "dur": round(len(x) / sr, 3),
        "f0": round(float(np.median(f0)), 1) if f0.size else 0.0,
        "f0_std": round(float(np.std(f0)), 1) if f0.size else 0.0,
        "voiced": round(float(f0.size) * 0.010 / (len(x) / sr), 3),
        "flat": round(float(np.mean(flats)), 4) if flats else 0.0,
        "hf": round(float(np.mean(hfs)), 4) if hfs else 0.0,
        "rough": round(rough, 4),
        "clicks": clicks,
    }


def main() -> int:
    tts = build()
    print("采样率", tts.sample_rate, " 音色数", tts.num_speakers, file=sys.stderr, flush=True)
    sids = list(range(0, tts.num_speakers))
    out = {}
    t0 = time.time()
    for i, sid in enumerate(sids):
        agg = []
        for text in TEXTS:
            g = tts.generate(text, sid=sid, speed=1.0)
            x = np.asarray(g.samples, dtype=np.float32).reshape(-1)
            agg.append(metrics(x, int(g.sample_rate)))
        keys = [k for k in agg[0] if isinstance(agg[0][k], (int, float))]
        merged = {k: round(float(np.mean([a[k] for a in agg])), 4) for k in keys}
        merged["name"] = kv.name("kokoro", sid)
        merged["clicks"] = int(sum(a["clicks"] for a in agg))
        out[str(sid)] = merged
        if (i + 1) % 10 == 0:
            print("  进度 %d/%d  %.0fs" % (i + 1, len(sids), time.time() - t0),
                  file=sys.stderr, flush=True)
    p = ROOT / "build" / "kokoro_scan.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写出", p, file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
