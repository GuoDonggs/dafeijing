# -*- coding: utf-8 -*-
"""命令行入口：python -m voice_agent <子命令>"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from . import __version__, audio as audio_io, tools
from . import voices as voice_table
from .agent import VoiceAgent
from .config import PROJECT_ROOT, Config, ConfigError, MODEL_DIR_CANDIDATES

PROJECT_SKILL_DIR = PROJECT_ROOT / "skills"

__all__ = ["main"]


def _fix_console() -> None:
    """Windows 控制台默认不是 UTF-8，中文直接 print 会抛编码错误。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _log(message: str) -> None:
    """日志必须立刻输出：run 模式常被重定向到文件或放进后台，行缓冲会吞掉进度。"""
    print(message, flush=True)


def _load(args) -> Config:
    try:
        return Config.load(getattr(args, "config", None))
    except ConfigError as exc:
        print("[配置错误] " + str(exc), file=sys.stderr)
        raise SystemExit(2) from exc


def cmd_run(args) -> int:
    cfg = _load(args)
    if cfg.missing_models:
        print("缺少模型：" + "、".join(cfg.missing_models), file=sys.stderr)
        print("先运行  python scripts/download_models.py", file=sys.stderr)
        return 2
    agent = VoiceAgent(cfg, log=_log)
    print("加载模型中……", flush=True)
    report = agent.load()
    print("  " + "  ".join(name + " {:.1f}s".format(sec) for name, sec in report.items()), flush=True)
    print("大脑模式：" + agent.brain.mode, flush=True)
    try:
        agent.run()
    except Exception as exc:  # noqa: BLE001 - 常驻进程的兜底，不能让用户只看到栈
        print("[运行出错] " + str(exc), file=sys.stderr)
        return 1
    return 0


def _console_confirm(question: str) -> bool:
    """终端里的敏感操作确认。

    非交互场景（输出被管道接走、stdin 是 EOF）一律按拒绝处理 ——
    自动化脚本绝不能因为「没人回答」就把关机、执行命令放行。
    """
    try:
        answer = input("  [需要确认] " + question + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("  （当前无法交互确认，按拒绝处理）")
        return False
    return answer in ("y", "yes", "是", "确认", "确定", "可以", "ok", "好")


def cmd_ask(args) -> int:
    cfg = _load(args)
    agent = VoiceAgent(cfg, log=_log)
    if args.speak:
        report = agent.load()
        print("模型加载：" + "  ".join(name + " {:.1f}s".format(sec) for name, sec in report.items()))
    print("大脑模式：" + agent.brain.mode)
    text = " ".join(args.text)
    started = time.perf_counter()
    reply = agent.ask(text, speak=args.speak, confirm=_console_confirm)
    print("你：" + text)
    print("助手：" + reply + "   ({:.2f}s)".format(time.perf_counter() - started))
    if reply == tools.CANCEL_REPLY:
        print("（这条指令属于敏感操作，需要在提示后回答 y 才会执行）")
    return 0


def _save_voice_setting(cfg_path: Path, value: str, engine: str) -> str:
    """把 tts.voice（和 tts.engine）写进配置文件，只动这两行。

    不整份 YAML 重排：用户的 config.yaml 里有注释和顺序，safe_dump 会全冲掉。
    落盘前先让 Config.load 验一遍，坏配置不许写进去。
    """
    text = cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""
    out: list[str] = []
    in_tts = False
    seen_tts = False
    set_voice = False
    set_engine = False
    for line in text.splitlines():
        if re.match(r"^tts:\s*$", line):
            in_tts, seen_tts = True, True
            out.append(line)
            continue
        if in_tts and line and not line[0].isspace():
            in_tts = False
        if in_tts:
            if re.match(r"^\s+voice\s*:", line):
                out.append("  voice: " + value)
                set_voice = True
                continue
            if re.match(r"^\s+engine\s*:", line):
                out.append("  engine: " + engine)
                set_engine = True
                continue
        out.append(line)

    if not seen_tts:
        out.extend(["", "tts:", "  engine: " + engine, "  voice: " + value])
    else:
        head = next(i for i, ln in enumerate(out) if re.match(r"^tts:\s*$", ln))
        if not set_engine:
            out.insert(head + 1, "  engine: " + engine)
        if not set_voice:
            out.insert(head + 1, "  voice: " + value)

    new_text = "\n".join(out).rstrip("\n") + "\n"
    probe = cfg_path.with_name(cfg_path.name + ".probe")
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(new_text, encoding="utf-8")
        Config.load(probe)
    except Exception as exc:
        return "配置没有写入（校验没通过）：" + str(exc)[:140]
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    if cfg_path.is_file():
        cfg_path.with_suffix(cfg_path.suffix + ".bak").write_text(text, encoding="utf-8")
    cfg_path.write_text(new_text, encoding="utf-8")
    return ""


def _audition(cfg, engine: str, sids: list[int], text: str | None) -> int:
    agent = VoiceAgent(cfg, log=_log)
    agent.load()
    tts = agent.tts
    if tts is None:
        print("合成引擎没起来")
        return 1
    if tts.engine != engine:
        print("提示：现在跑的是 " + tts.engine + " 引擎，配置里要写 tts.engine: " + engine)
    line = text or "你好，我是大肥鲸，今天天气不错，有什么可以帮你的吗？"
    total = max(1, tts.num_speakers)
    for n, sid in enumerate(sids):
        if not (0 <= sid < total):
            print("跳过 " + str(sid) + "：模型里只有 " + str(tts.num_speakers) + " 个音色")
            continue
        tts.speaker_id = sid
        print("  [{}/{}] {}  {}".format(n + 1, len(sids), sid,
                                        voice_table.label(tts.engine, sid)), flush=True)
        tts.speak(line, kind="reply")
    return 0


def cmd_voices(args) -> int:
    """列音色表、试听、改配置。

    vits 有 5 个固定角色音，ChatTTS 的音色是一个随机种子 ——
    两种都不是能猜出来的东西，所以这里给出名字，让人按名字挑、点一下试听。
    """
    cfg = _load(args)
    engine = voice_table.engine_of(args.engine or cfg.tts.engine)

    if args.save:
        sid = voice_table.resolve(engine, args.save)
        if sid is None:
            print("认不出这个音色：" + str(args.save) + "（voice_agent voices 看清单）")
            return 2
        path = Path(args.config) if args.config else cfg.config_path
        problem = _save_voice_setting(path, voice_table.name(engine, sid), engine)
        if problem:
            print(problem)
            return 1
        print("已写入 " + str(path))
        print("  engine: " + engine + "   voice: " + voice_table.name(engine, sid)
              + "（" + voice_table.label(engine, sid) + "）")
        print("重启后生效；想马上听：voice_agent voices " + voice_table.name(engine, sid))
        return 0

    male = True if args.male else (False if args.female else None)

    if args.voice:
        sid = voice_table.resolve(engine, args.voice)
        if sid is None:
            print("认不出这个音色：" + str(args.voice) + "（voice_agent voices 看清单）")
            return 2
        return _audition(cfg, engine, [sid], args.text)

    if args.audition:
        pool = [i for i, _ in voice_table.catalog(engine, male=male)]
        start = max(0, args.start)
        picked = pool[start: start + max(1, args.count)]
        if not picked:
            print("这个范围里没有音色")
            return 2
        print("连着听 " + str(len(picked)) + " 个，不想听了按 Ctrl+C 打断。")
        return _audition(cfg, engine, picked, args.text)

    rows = voice_table.catalog(engine, male=male)
    current = cfg.tts.voice or cfg.tts.speaker_id
    cur_sid = voice_table.resolve(engine, current)
    print()
    print("引擎 " + engine + "，模型里有 " + str(voice_table.count(engine)) + " 个音色")
    if cur_sid is None:
        print("当前音色：（配置里的值认不出来，实际会用默认）")
    else:
        fixed, note = voice_table.sanitize(engine, cur_sid)
        print("当前音色：" + voice_table.label(engine, cur_sid)
              + (("   →   实际会用 " + voice_table.label(engine, fixed)) if note else ""))
        if note:
            print("          " + note)
    print()
    for sid, label in rows:
        print("  {:>3}  {}{}".format(sid, label, "   ← 正在用" if sid == cur_sid else ""))
    print()
    print("试听一个：  voice_agent voices zf_070")
    print("连着听：    voice_agent voices --audition --female --count 8")
    print("定下来：    voice_agent voices --set zf_070")
    return 0


def cmd_say(args) -> int:
    cfg = _load(args)
    agent = VoiceAgent(cfg, log=_log)
    agent.load()
    text = " ".join(args.text)
    if args.out:
        samples, rate = agent.tts.synthesize(text, kind=args.kind)  # type: ignore[union-attr]
        path = audio_io.write_wav(args.out, samples, rate)
        print("已生成 " + str(path) + "（{:.2f}s）".format(samples.size / rate))
        return 0
    agent.speak(text, kind=args.kind)
    return 0


def cmd_listen(args) -> int:
    cfg = _load(args)
    agent = VoiceAgent(cfg, log=_log)
    agent.load()
    device = audio_io.resolve_device(cfg.audio.input_device, "input")
    mic = audio_io.Mic(device=device, sample_rate=cfg.audio.sample_rate, block_size=cfg.audio.block_size)
    mic.start()
    print("请说话……（最长 {:.0f} 秒）".format(args.timeout), flush=True)
    utterance = None
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            block = mic.read(timeout=0.2)
            if block is None:
                continue
            utterance = agent.vad.feed(block) or utterance  # type: ignore[union-attr]
            if utterance is not None:
                break
        utterance = utterance or agent.vad.flush()  # type: ignore[union-attr]
    finally:
        mic.close()
    if utterance is None or utterance.size == 0:
        print("没听到人声。")
        return 1
    text = agent.asr.transcribe(utterance, punctuate=True)  # type: ignore[union-attr]
    print("识别结果：" + text)
    print("音频长度：{:.2f}s".format(utterance.size / cfg.audio.sample_rate))
    return 0


def cmd_wake(args) -> int:
    cfg = _load(args)
    agent = VoiceAgent(cfg, log=_log)
    agent.load()
    device = audio_io.resolve_device(cfg.audio.input_device, "input")
    mic = audio_io.Mic(device=device, sample_rate=cfg.audio.sample_rate, block_size=cfg.audio.block_size)
    mic.start()
    print("正在听唤醒词（" + "、".join(cfg.wake.keywords) + "），喊一声试试；Ctrl+C 退出", flush=True)
    deadline = time.monotonic() + args.timeout
    try:
        while time.monotonic() < deadline:
            block = mic.read(timeout=0.2)
            if block is None:
                continue
            hit = agent.wake.feed(block)  # type: ignore[union-attr]
            if hit:
                print("命中唤醒词：" + hit)
                return 0
    except KeyboardInterrupt:
        pass
    finally:
        mic.close()
    print("这段时间里没有听到唤醒词。")
    return 1


def cmd_tools(args) -> int:
    tools.autoload_skills()   # 先加载技能，否则统计和清单会漏掉它们
    builtin = sum(1 for tool in tools.REGISTRY.values() if tool.source == "builtin")
    print("已注册工具 " + str(len(tools.REGISTRY)) + " 个（内置 " + str(builtin)
          + "，技能 " + str(len(tools.REGISTRY) - builtin) + "）：")
    print(tools.describe())
    return 0


def cmd_devices(args) -> int:
    print(audio_io.list_devices())
    return 0


def cmd_doctor(args) -> int:
    tools.autoload_skills()   # 让技能也计入工具总数，否则体检结果和实际能力对不上
    print("语音控制电脑 Agent v" + __version__)
    print("Python：" + sys.version.split()[0] + "  " + sys.executable)
    missing_modules = []
    for module in ("sherpa_onnx", "sounddevice", "numpy", "yaml", "requests", "psutil", "pypinyin"):
        try:
            __import__(module)
        except Exception:
            missing_modules.append(module)
    print("依赖：" + ("齐全" if not missing_modules else "缺少 " + "、".join(missing_modules)))
    try:
        from PyQt6.QtCore import QT_VERSION_STR  # noqa: PLC0415

        print("桌面界面：可用（PyQt6 / Qt " + QT_VERSION_STR + "）")
    except Exception as exc:  # noqa: BLE001
        print("桌面界面：不可用（" + str(exc)[:50] + "）；可以改用  python -m voice_agent ui --web")
    for candidate in MODEL_DIR_CANDIDATES:
        print("模型目录[" + ("有" if candidate.is_dir() else "无") + "]：" + str(candidate))
    try:
        cfg = Config.load(getattr(args, "config", None))
    except ConfigError as exc:
        print("配置：" + str(exc))
        return 2
    print("配置：模型 " + str(len(cfg.models)) + " 个文件，缺失 " + str(len(cfg.missing_models)) + " 个")
    if cfg.missing_models:
        print("  缺失清单：" + "、".join(cfg.missing_models))
    print(
        "LLM："
        + (
            "已配置（" + cfg.llm.model + " @ " + cfg.llm.base_url + "）"
            if cfg.llm.available
            else "未配置 API Key，将使用离线规则模式"
        )
    )
    print("大脑：思考 " + cfg.llm.reasoning_effort + "；"
          + "多模型路由 " + (str(cfg.llm.routes) if cfg.llm.routes else "未配置（都用默认模型）"))
    # 算力要如实汇报：请求了 GPU 却跑在 CPU 上是最容易让人误判性能的情况
    print("语音算力：" + cfg.speech.provider_text
          + "；档位 " + cfg.speech.profile + "（" + cfg.speech.profile_note + "）"
          + "；线程 " + str(cfg.speech.num_threads()))
    print("语音合成引擎：" + cfg.tts.engine
          + ("（ChatTTS：要显卡，首次加载十几秒）"
             if voice_table.engine_of(cfg.tts.engine) == "chattts" else "（VITS：纯 CPU）"))
    # 声纹要说得直白：开了但没录，等于没开
    from .speaker import Voiceprint

    voice = Voiceprint(cfg)
    print("声纹：" + voice.status_text()
          + ("；模型 " + voice.model_name if voice.model_name else "；模型未下载"
             "（python scripts/download_models.py --only speaker）"))
    try:
        print("音频设备：" + str(len(audio_io.list_devices().splitlines())) + " 个")
    except Exception as exc:  # noqa: BLE001
        print("音频设备：读取失败 " + str(exc))
    skills = [info for info in tools.SKILL_INFOS if info.ok]
    builtin = sum(1 for tool in tools.REGISTRY.values() if tool.source == "builtin")
    print("工具：" + str(len(tools.REGISTRY)) + " 个（内置 " + str(builtin)
          + " + 技能 " + str(len(tools.REGISTRY) - builtin) + "）；技能文件 "
          + str(len(tools.SKILL_INFOS)) + " 个，其中正常 " + str(len(skills)) + " 个")
    return 0


def cmd_selftest(args) -> int:
    from .selftest import run

    return 0 if run(getattr(args, "config", None)) else 1


def cmd_log(args) -> int:
    """看运行日志（落盘的那份）。

    界面上的日志窗口一关就没了，闪退更是连一行都不剩 —— 出问题之后
    用户能做的就是打开这个日志、把最后几十行发出来。
    """
    from . import journal

    if args.prune:
        removed = journal.prune(args.keep_days)
        print("清掉了 " + str(removed) + " 个旧日志（保留 " + str(args.keep_days) + " 天）")
        return 0
    path = journal.file_path()
    print("日志文件：" + str(path))
    others = journal.recent_files(7)
    if len(others) > 1:
        print("最近几天的：" + "、".join(item.name for item in others))
    print()
    lines = journal.tail(args.lines, "detail" if args.detail else "info")
    if not lines:
        print("（今天还没有日志）")
        return 0
    for line in lines:
        print(line)
    return 0


def cmd_ui(args) -> int:
    """默认开原生桌面窗口；--web 才起浏览器版。"""
    config = Path(args.config) if getattr(args, "config", None) else None
    if getattr(args, "web", False):
        from .webui import serve

        return serve(config, host=args.host, port=args.port, open_browser=not args.no_browser)

    from .ui.main_window import main as gui_main

    return gui_main(config, autostart=not getattr(args, "no_autostart", False))


def cmd_skills(args) -> int:
    from .skills import SKILL_DIRS, SkillLoader, skill_template

    loader = SkillLoader()
    if args.action == "new":
        target_dir = Path(args.dir) if args.dir else PROJECT_SKILL_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (args.name if args.name.endswith((".yaml", ".yml")) else args.name + ".yaml")
        if target.exists() and not args.force:
            print("已经存在：" + str(target) + "（要覆盖请加 --force）")
            return 1
        target.write_text(skill_template(), encoding="utf-8")
        print("已创建技能模板：" + str(target))
        print("编辑它，然后运行  python -m voice_agent skills check  验证。")
        return 0

    if args.action == "reload":
        loader.ensure_user_dir()

    skills = loader.load_all()
    print("技能目录：")
    for directory in SKILL_DIRS:
        print("  " + ("[有]" if directory.is_dir() else "[无]") + " " + str(directory))
    print("\n共 " + str(len(skills)) + " 个技能文件：")
    if not skills:
        print("  （空）用 python -m voice_agent skills new my_skill 建一个")
        return 0
    for skill in skills:
        mark = "OK " if skill.ok else "错误"
        print("  [" + mark + "] " + skill.name.ljust(18) + (skill.title or "").ljust(12)
              + " " + skill.source.name)
        if skill.ok:
            print("        提供工具：" + "、".join(skill.tools))
        else:
            print("        " + skill.error)
    from . import tools as tools_mod

    print("\n当前工具总数：" + str(len(tools_mod.REGISTRY)))
    return 0 if all(s.ok for s in skills) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice_agent",
        description="语音控制电脑的简易 Agent：唤醒词 → 说话 → 电脑执行 → 语音播报",
    )
    parser.add_argument("--version", action="version", version="voice_agent " + __version__)
    parser.add_argument("--config", help="配置文件路径，默认 ./config.yaml")
    sub = parser.add_subparsers(dest="command")

    # 让 --config 在子命令后面也能写：voice_agent ui --config x.yaml 更符合直觉
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径，默认 ./config.yaml")

    p_run = sub.add_parser("run", parents=[common], help="常驻运行：唤醒词 + 语音对话")
    p_run.set_defaults(func=cmd_run)

    p_ask = sub.add_parser("ask", parents=[common], help="用文字下指令（不占麦克风）")
    p_ask.add_argument("text", nargs="+")
    p_ask.add_argument("--speak", action="store_true", help="同时把回复读出来")
    p_ask.set_defaults(func=cmd_ask)

    p_say = sub.add_parser("say", parents=[common], help="只做语音合成")
    p_say.add_argument("text", nargs="+")
    p_say.add_argument("--kind", default="reply", choices=["reply", "notice", "wake", "confirm", "error"])
    p_say.add_argument("--out", help="存成 wav 而不是播放")
    p_say.set_defaults(func=cmd_say)

    p_listen = sub.add_parser("listen", parents=[common], help="录一句并识别，验证麦克风")
    p_listen.add_argument("--timeout", type=float, default=15.0)
    p_listen.set_defaults(func=cmd_listen)

    p_wake = sub.add_parser("wake", parents=[common], help="只跑唤醒词检测，验证喊得醒")
    p_wake.add_argument("--timeout", type=float, default=30.0)
    p_wake.set_defaults(func=cmd_wake)

    p_ui = sub.add_parser("ui", parents=[common], help="打开控制台（原生桌面窗口）")
    p_ui.add_argument("--web", action="store_true", help="改用浏览器版界面")
    p_ui.add_argument("--host", default="127.0.0.1", help="仅 --web 使用")
    p_ui.add_argument("--port", type=int, default=8760, help="仅 --web 使用")
    p_ui.add_argument("--no-browser", action="store_true", help="仅 --web：不要自动打开浏览器")
    p_ui.add_argument("--no-autostart", action="store_true",
                      help="仅桌面版：启动后不自动开始监听")
    p_ui.set_defaults(func=cmd_ui)

    p_gui = sub.add_parser("gui", parents=[common], help="同 ui（原生桌面窗口）")
    p_gui.set_defaults(func=cmd_ui)

    p_skills = sub.add_parser("skills", parents=[common], help="管理自定义技能")
    p_skills.add_argument("action", nargs="?", default="list", choices=["list", "check", "new", "reload"])
    p_skills.add_argument("name", nargs="?", default="my_skill", help="skills new 时的文件名")
    p_skills.add_argument("--dir", help="skills new 写到哪里，默认 ./skills")
    p_skills.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_skills.set_defaults(func=cmd_skills)

    p_voices = sub.add_parser("voices", parents=[common], help="列出 / 试听 / 切换音色")
    p_voices.add_argument("voice", nargs="?", help="试听这个音色，名字或编号都行")
    p_voices.add_argument("--engine", choices=["vits", "chattts"], help="看哪个引擎的音色")
    p_voices.add_argument("--set", dest="save", metavar="VOICE", help="把音色写进配置文件")
    p_voices.add_argument("--audition", action="store_true", help="连着念一遍，方便挑")
    p_voices.add_argument("--male", action="store_true", help="只看 / 只听男声")
    p_voices.add_argument("--female", action="store_true", help="只看 / 只听女声")
    p_voices.add_argument("--start", type=int, default=0, help="--audition 从第几个开始")
    p_voices.add_argument("--count", type=int, default=8, help="--audition 听几个")
    p_voices.add_argument("--text", help="试听用的文本")
    p_voices.set_defaults(func=cmd_voices)

    sub.add_parser("tools", help="列出可用工具").set_defaults(func=cmd_tools)
    sub.add_parser("devices", help="列出音频设备").set_defaults(func=cmd_devices)
    p_log = sub.add_parser("log", parents=[common], help="看运行日志（落盘的那份）")
    p_log.add_argument("--lines", type=int, default=40, help="看最后几行，默认 40")
    p_log.add_argument("--detail", action="store_true",
                       help="连细节一起看（参数全文、耗时、token 用量）")
    p_log.add_argument("--prune", action="store_true", help="只清理旧日志")
    p_log.add_argument("--keep-days", type=int, default=14, help="清理时保留几天")
    p_log.set_defaults(func=cmd_log)

    sub.add_parser("doctor", help="环境体检").set_defaults(func=cmd_doctor)
    sub.add_parser("selftest", help="端到端自检").set_defaults(func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console()
    # 崩了也要留下痕迹：窗口版没有控制台，栈只打在内存里，
    # 用户能提供的只有一句"它闪退了"（日志文件里能看到完整栈）。
    from . import journal

    journal.install_crash_handler()
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
