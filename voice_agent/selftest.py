# -*- coding: utf-8 -*-
"""自检：不接麦克风也能验证整条链路是否正常。

依次检查 模型加载 → 语音合成 → 语音识别回环 → 端点检测 → 工具 → 大脑，
最后打印一张表。任何一项失败都会让退出码非 0，方便放进 CI 或安装脚本。
"""

from __future__ import annotations

import sys
import time
import traceback
from typing import Callable

from . import audio as audio_io
from . import tools
from .config import PROJECT_ROOT, Config
from .speech import Asr, Tts, VadSegmenter, WakeWord

__all__ = ["run", "SENTENCE"]

SENTENCE = "好的，D盘还剩一百五十六个G。"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""), flush=True)

    def check(self, name: str, func: Callable[[], str]) -> bool:
        started = time.perf_counter()
        try:
            detail = func()
            self.add(name, True, (detail or "") + "  ({:.2f}s)".format(time.perf_counter() - started))
            return True
        except Exception as exc:  # noqa: BLE001 - 自检要把失败原因讲清楚
            self.add(name, False, str(exc)[:160])
            traceback.print_exc(limit=2)
            return False

    @property
    def ok(self) -> bool:
        return all(row[1] for row in self.rows)

    def summary(self) -> str:
        passed = sum(1 for row in self.rows if row[1])
        return "{}/{} 项通过".format(passed, len(self.rows))


def run(config_path: str | None = None, skip_tools: bool = False) -> bool:
    report = Report()
    print("=== 语音控制电脑 Agent 自检 ===", flush=True)

    cfg: Config | None = None

    def load_config() -> str:
        nonlocal cfg
        cfg = Config.load(config_path)
        return "配置文件 " + (str(cfg.path) if cfg.path else "(默认)") + "，模型目录 " + str(cfg.models_dir)

    report.check("加载配置", load_config)
    if cfg is None:
        print("\n配置都读不出来，后面的检查没有意义。")
        return False

    report.check(
        "模型文件齐全",
        lambda: (_ for _ in ()).throw(RuntimeError("缺少：" + "、".join(cfg.missing_models)))
        if cfg.missing_models
        else "共 " + str(len(cfg.models)) + " 个文件，FST " + str(len(cfg.tts_rule_fsts)) + " 个",
    )

    holder: dict = {}

    def load_wake() -> str:
        wake = WakeWord(cfg)  # type: ignore[arg-type]
        holder["wake"] = wake
        return "唤醒词 " + "、".join(wake.keywords) + "，生成 " + wake.keywords_path.name

    report.check("唤醒词模型", load_wake)

    def load_vad() -> str:
        holder["vad"] = VadSegmenter(cfg)  # type: ignore[arg-type]
        return "silero-VAD 就绪"

    report.check("端点检测模型", load_vad)
    report.check("语音识别模型", lambda: (holder.__setitem__("asr", Asr(cfg)), "paraformer 就绪")[1])  # type: ignore[arg-type]
    report.check("语音合成模型", lambda: (holder.__setitem__("tts", Tts(cfg)), "VITS 就绪，音色数 " + str(holder["tts"].num_speakers))[1])  # type: ignore[arg-type]

    tts: Tts | None = holder.get("tts")
    asr: Asr | None = holder.get("asr")
    vad: VadSegmenter | None = holder.get("vad")

    if tts is not None and asr is not None:
        def roundtrip() -> str:
            samples, rate = tts.synthesize(SENTENCE, "reply")
            if samples.size == 0:
                raise RuntimeError("合成结果为空")
            path = audio_io.write_wav(PROJECT_ROOT / "build" / "selftest.wav", samples, rate)
            pcm = audio_io.resample(samples, rate, 16000)
            heard = asr.transcribe(pcm)
            holder["heard"] = heard
            return (
                "说「" + SENTENCE[:12] + "…」→ 听回「" + heard + "」，合成 RTF {:.2f}，"
                "识别 RTF {:.2f}，音频存 " + path.name
            ).format(tts.rtf, asr.rtf)

        report.check("语音合成 → 识别回环", roundtrip)

    if vad is not None and holder.get("heard"):
        def vad_check() -> str:
            samples, rate = tts.synthesize("打开记事本。", "reply")  # type: ignore[union-attr]
            pcm = audio_io.resample(samples, rate, 16000)
            segment = None
            for index in range(0, pcm.size, 512):
                segment = vad.feed(pcm[index : index + 512]) or segment
            segment = segment or vad.flush()
            if segment is None or segment.size == 0:
                raise RuntimeError("VAD 没有切出语音段")
            return "切出 {:.2f}s 语音段".format(segment.size / 16000)

        report.check("端点检测", vad_check)

    if not skip_tools:
        def tools_check() -> str:
            results = []
            for name, args in (
                ("get_time", {}),
                ("system_info", {"kind": "全部"}),
                ("list_files", {"path": "桌面"}),
                ("clipboard", {"action": "get"}),
            ):
                value = tools.call(name, args)
                if not value:
                    raise RuntimeError(name + " 返回空")
                results.append(name)
            return "可用工具 " + str(len(tools.REGISTRY)) + " 个，实测 " + "、".join(results)

        report.check("电脑控制工具", tools_check)

    from .brain import Brain

    def rules_brain() -> str:
        brain = Brain(cfg, log=lambda _msg: None)  # type: ignore[arg-type]
        reply = brain.respond("现在几点了")
        if "现在" not in reply:
            raise RuntimeError("规则大脑返回异常：" + reply[:60])
        return "模式 " + brain.mode + "，问「现在几点」答「" + reply[:24] + "」"

    report.check("大脑（离线规则）", rules_brain)

    print("\n=== " + report.summary() + ("" if report.ok else "，有失败项") + " ===", flush=True)
    if holder.get("heard"):
        print("提示：识别回环的结果受音色与模型影响，个别字不同是正常的。")
    return report.ok


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    if "--config" in argv:
        index = argv.index("--config")
        path = argv[index + 1] if index + 1 < len(argv) else None
    return 0 if run(path) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
