# -*- coding: utf-8 -*-
"""控制台后端：把 VoiceAgent 包成一个可以被界面操控的对象。

两个前端共用它：
- voice_agent/gui.py    原生桌面窗口（默认）
- voice_agent/webui.py  浏览器页面（ui --web）

刻意不引入任何 HTTP 或 GUI 依赖：这里只回答「现在什么状态、怎么启动、
配置怎么写、技能怎么管」这些问题，界面长什么样由前端决定。
"""

from __future__ import annotations

import dataclasses
import queue
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import yaml

from . import __version__, audio as audio_io, journal, paths, security, tools
from . import voices as voice_table
from .agent import VoiceAgent
from .config import (
    DEFAULT_CONFIG_PATH,
    EXAMPLE_CONFIG_PATH,
    PROJECT_ROOT,
    Config,
    ConfigError,
    normalize_keys,
)
from .skills import (
    PROJECT_TOOL_DIR,
    SKILL_DIRS,
    SkillLoader,
    dep_names,
    install_deps,
    skill_template,
    tool_template,
)

WEB_DIR = Path(__file__).resolve().parent / "web"
PROJECT_SKILL_DIR = PROJECT_ROOT / "skills"
MAX_BODY = 512 * 1024
LOG_LIMIT = 600


class Console:
    """把一个 VoiceAgent 包成可以远程操控的服务端对象。"""

    def __init__(self, config_path: Path | None, host: str = "127.0.0.1", port: int = 8760) -> None:
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.host = host
        self.port = port
        self.token = secrets.token_urlsafe(24)
        # 新建技能/工具写到哪里；测试可以指到临时目录，避免污染真实的 ./skills
        self.skill_dir = PROJECT_SKILL_DIR
        self.tool_dir = PROJECT_TOOL_DIR
        #: 界面注册的「重启 / 退出程序」实现（PyQt 窗口接管；
        #: 命令行和网页版没有，就走 agent 的默认路径）
        self.app_hook = None
        self.logs: deque[dict] = deque(maxlen=LOG_LIMIT)
        # 每条日志一个单调递增的序号。界面靠它判断"哪几条是新的" ——
        # 用下标是不行的：deque 一旦写满就开始从头丢，下标会永远追不上，
        # 日志窗口会静悄悄地不再更新（这正是以前的毛病）。
        self._log_seq = 0
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._engine_lock = threading.RLock()
        self._starting = False
        # 每次启停都推进一次：加载到一半用户点了「停止」，回来就不能再启动
        self._start_token = 0
        self._recording = False
        self.last_error = ""
        self.agent: VoiceAgent | None = None
        # 日志文件：这次的启动也记一行，排查时先看它
        journal.install_crash_handler()
        journal.info(journal.process_line(), tag="start")
        journal.prune()
        self.cfg = self._load_config()
        # 总音量是模块级状态（播报和提示音走不同路径），启动时同步一次
        audio_io.set_output_gain(self.cfg.audio.output_gain)
        # 改了但还没重启引擎的设置项：键 -> 中文名，界面拿它弹「需要重启」
        self.missing_restart: dict[str, str] = {}
        # 试听进度：idle / loading / playing / done / error。
        # 界面靠它显示状态，不然"正在加载"会一直挂在设置页上没人清。
        self.audition: dict = {"state": "idle", "voice": "", "error": "", "at": 0.0}
        self.skills: list = []
        self.reload_skills(announce=False)

    # 哪些设置改了必须重启引擎：共同点是「构造模型/开音频流时只读一次」。
    # 其余的（语速、音色、音量、LLM、追问窗口……）都是就地生效，不该打扰用户。
    RESTART_KEYS: dict[str, str] = {
        "speech.profile": "资源档位",
        "speech.device": "推理算力",
        "speech.threads": "线程数",
        "tts.engine": "合成引擎",
        "tts.num_threads": "合成线程数",
        "tts.provider": "合成算力",
        "wake.keywords": "唤醒词",
        "wake.threshold": "唤醒灵敏度",
        "wake.score": "唤醒词打分",
        "audio.input_device": "麦克风",
        "audio.output_device": "扬声器",
        "audio.sample_rate": "采样率",
        "audio.input_gain": "麦克风增益",
        "agent.vad_threshold": "断句灵敏度",
        "agent.min_silence_ms": "静音判定",
        "agent.min_speech_ms": "最短语音",
        "asr.num_threads": "识别线程数",
        "asr.provider": "识别算力",
        "speaker.model": "声纹模型",
        "speaker.profile": "声纹档案",
    }
    # 注意 speaker.enabled / speaker.threshold 不在这里：它们每次现读配置，
    # 在界面上勾一下就生效（阈值是 Voiceprint.threshold 属性，开关也是属性）。

    # ───────────────── 基础设施 ─────────────────

    def _load_config(self) -> Config:
        if not self.config_path.is_file() and EXAMPLE_CONFIG_PATH.is_file():
            # 首次运行：没有 config.yaml 就用示例兜底，界面里点保存才真正落盘
            self.log("[ui] 没有 " + self.config_path.name + "，先按示例配置运行")
            config = Config.load(EXAMPLE_CONFIG_PATH)
        else:
            config = Config.load(self.config_path)
        security.configure(config)
        self._apply_data_dir(config)
        if not security.snapshot()["transport_ok"]:
            # 这条必须吵一点：明文中转站能改写模型的回答，也就能塞工具调用
            self.log("[安全] " + str(security.snapshot()["transport_note"])
                     + " → 已自动降为「只读」模式。"
                     + "要照用请把 security.allow_insecure 设成 true（不建议）")
            security.audit({"event": "transport_downgrade", "mode": security.mode()})
        return config

    def log(self, message: str, level: str = "info") -> None:
        """记一条日志。

        - **两个地方都写**：界面（deque + 订阅者）和 <数据目录>/logs/ 下的文件。
          文件是给"事后翻账"用的：界面那个缓冲区会滚掉，闪退更是什么都不剩。
        - level="detail" 是排查用的细节（参数全文、耗时、token），界面默认不显示，
          日志窗口里勾上「详细」就能一起看。
        """
        text = str(message)
        journal.write(level, text)
        self._log_seq += 1
        item = {"type": "log", "ts": time.strftime("%H:%M:%S"), "text": text,
                "level": "detail" if str(level) == "detail" else "info",
                "seq": self._log_seq}
        self.logs.append(item)
        self._broadcast(item)

    def _broadcast(self, item: dict) -> None:
        with self._sub_lock:
            targets = list(self._subscribers)
        for target in targets:
            try:
                target.put_nowait(item)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=500)
        with self._sub_lock:
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._sub_lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    # ───────────────── 引擎 ─────────────────

    def start_engine(self) -> dict:
        with self._engine_lock:
            if self._starting:
                return {"ok": True, "starting": True}
            if self.agent is not None and self.agent.running:
                return {"ok": True, "running": True}
            self._starting = True
            self.last_error = ""
            token = self._start_token

        def work() -> None:
            try:
                if self.agent is None:
                    with self._engine_lock:
                        if self.agent is None:
                            self.agent = self._new_agent()
                self.log("[ui] 正在加载语音模型……")
                report = self.agent.load()
                if token != self._start_token:
                    # 加载这几秒里用户按了「停止」，就别再打开麦克风了
                    self.log("[ui] 启动已被取消")
                    return
                self.log("[ui] 模型就绪：" + "  ".join(
                    name + " " + format(sec, ".1f") + "s" for name, sec in report.items()
                ))
                # 引擎是按最新配置造起来的，之前攒的「待重启」提示可以撤了
                self.missing_restart.clear()
                self.agent.start(announce=True)
            except Exception as exc:  # noqa: BLE001 - 失败要通过界面告诉用户
                self.last_error = str(exc)
                self.log("[ui] 启动失败：" + str(exc))
            finally:
                with self._engine_lock:
                    self._starting = False

        threading.Thread(target=work, name="voice-agent-start", daemon=True).start()
        return {"ok": True, "starting": True}

    def stop_engine(self) -> dict:
        with self._engine_lock:
            self._start_token += 1   # 让还没加载完的启动流程作废
            agent = self.agent
        if agent is not None:
            agent.stop()
            self.log("[ui] 已停止监听，麦克风已释放")
        return {"ok": True, "running": False}

    def restart_engine(self) -> dict:
        """重启引擎：改了声卡、线程数、唤醒词这类「构造时读一次」的设置之后用它。

        必须把 agent 丢掉重建 —— 模型和音频流都还握着旧的参数，
        只是 stop/start 一遍不会重新读配置。
        """
        with self._engine_lock:
            self._start_token += 1
            agent = self.agent
            self.agent = None
        if agent is not None:
            agent.stop()
            deadline = time.monotonic() + 12.0
            while getattr(agent, "running", False) and time.monotonic() < deadline:
                time.sleep(0.1)
        self.missing_restart.clear()
        self.log("[ui] 正在重启引擎，让新设置生效……")
        return self.start_engine()

    def pending_restart(self) -> list[str]:
        """还没生效的设置项（中文名，去重后按字母序）。"""
        return sorted(set(self.missing_restart.values()))

    def begin_recording(self) -> bool:
        """录音测试的互斥锁：同一时刻只允许一路录音。"""
        with self._engine_lock:
            if self._recording:
                return False
            self._recording = True
            return True

    def end_recording(self) -> None:
        with self._engine_lock:
            self._recording = False

    def web_confirm(self, question: str) -> bool:
        """网页端没有语音通道时无法完成确认：一律拒绝，并写清楚原因。

        宁可让用户多点一次「启动监听」，也不能让一个 HTTP 请求把关机、执行命令
        这类敏感操作默默放行。
        """
        self.log("[ui] 敏感操作需要语音确认，但引擎未启动，已拒绝：" + question)
        return False

    def ensure_agent(self) -> VoiceAgent:
        """文本指令不需要麦克风，但需要大脑；没有引擎时临时建一个。

        必须加锁：start_engine 在后台线程里要花几秒加载模型，这期间如果来了
        一条文字指令，两个线程会各自 new 一个 VoiceAgent，后赋值的那个赢，
        另一个带着已加载的模型变成孤儿 —— 状态显示和执行用的就不是同一个对象了。
        """
        with self._engine_lock:
            if self.agent is None:
                self.agent = self._new_agent()
            return self.agent

    def _apply_data_dir(self, config) -> None:  # noqa: ANN001
        """装载数据目录，顺手把老位置的文件搬过来（只搬一次）。

        以前截图放在「图片/voice-agent」、映射表放在项目根，现在统一到数据目录下；
        升级不该让用户以为东西丢了，所以这里做一次性迁移。
        """
        configured = str(getattr(getattr(config, "paths", None), "data_dir", "") or "")
        paths.set_data_dir(configured)
        # 旧位置 -> 新位置。marks.json 也要搬：换了数据目录之后，
        # 用户之前框的"范围1"、标的"点1"不该就这么消失（它们还在旧目录里，
        # 再框一次又会从"范围1"重新编号，看起来就像全丢了）。
        legacy_data = paths.legacy_data_dir()
        moved = paths.migrate_legacy([
            (Path.home() / "Pictures" / "voice-agent", paths.sub("screenshots")),
            (PROJECT_ROOT / "apps.yaml", paths.sub("apps")),
            (legacy_data / "marks.json", paths.sub("marks")),
            (legacy_data / "memory.json", paths.sub("memory")),
            (legacy_data / "voiceprint.json", paths.sub("voiceprint")),
        ], log=self.log)
        if moved:
            self.log("[ui] 运行时文件已集中到 " + str(paths.data_dir()))
        # 标记仓库是全局单例：换了目录要让它按新位置重读
        try:
            from . import marks as marks_mod  # noqa: PLC0415

            marks_mod.store.reload()
        except Exception:  # noqa: BLE001 - 重读失败不该影响启动
            pass

    def _new_agent(self) -> VoiceAgent:
        """造一个引擎并接上界面注册的回调。

        「重启 / 退出程序」这类操作最后要落到界面上（它才知道怎么关窗口、
        怎么把自己拉起来），引擎只负责把请求转发过来。
        """
        agent = VoiceAgent(self.cfg, log=self.log)
        agent.app_hook = self.app_hook
        return agent

    # ───────────────── 配置 ─────────────────

    def config_text(self) -> str:
        if self.config_path.is_file():
            return self.config_path.read_text(encoding="utf-8")
        if EXAMPLE_CONFIG_PATH.is_file():
            return EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8")
        return ""

    def _backup(self) -> str:
        """备份现有配置；备份不成也不该拦住保存（更不能把异常抛给 Qt）。"""
        try:
            if self.config_path.is_file():
                backup = self.config_path.with_suffix(self.config_path.suffix + ".bak")
                backup.write_text(self.config_path.read_text(encoding="utf-8"),
                                  encoding="utf-8")
                return backup.name
        except OSError as exc:
            # 配置被编辑器/同步盘占着、磁盘满、目录只读……都算。说一句就好：
            # 这里要是抛出去，Qt 槽里的异常会让整个进程 abort（窗口直接消失）。
            self.log("[ui] 没能备份配置：" + str(exc)[:80])
        return ""

    def _validate_config_text(self, text: str) -> str:
        """返回错误说明；空串表示这份配置真的能被加载。

        必须先验证再落盘：早先是「先写文件、再 Config.load」，一旦用户填了类型不对
        的值，错误虽然弹给了他，坏文件却已经写下去了 —— 下次启动连配置页都打不开，
        只能手动改文件才能恢复。
        """
        try:
            parsed = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            return "YAML 语法错误：" + str(exc)[:200]
        if not isinstance(parsed, dict):
            return "配置顶层必须是映射"
        probe = self.config_path.with_name(self.config_path.name + ".probe")
        try:
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text(text, encoding="utf-8")
            Config.load(probe)
        except ConfigError as exc:
            return "配置无法加载：" + str(exc)[:200]
        except (ValueError, TypeError) as exc:
            # 例如 sample_rate: abc —— 数值转换失败，同样属于「这份配置不能用」
            return "配置项的取值不合法：" + str(exc)[:160]
        except OSError as exc:
            return "无法写入临时文件：" + str(exc)[:120]
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        return ""

    def save_config_text(self, text: str, message: str = "配置已保存",
                         keys: list[str] | None = None) -> dict:
        problem = self._validate_config_text(text)
        if problem:
            return {"ok": False, "error": problem}
        backup = self._backup()
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": "写入失败：" + str(exc)[:120]}
        return self._after_config_change(
            message + ("（原文件备份为 " + backup + "）" if backup else ""), keys=keys)

    # 界面上用逗号写、配置文件里是列表的项
    LIST_KEYS = ("wake.keywords", "wake.replies", "agent.exit_words",
                 "security.deny_tools", "security.always_confirm", "security.floor_tools")

    def settings(self) -> dict:
        """给设置表单用的一份扁平常量快照（不暴露真实 API Key）。"""
        cfg = self.cfg
        return {
            "wake.keywords": "、".join(cfg.wake.keywords),
            "wake.replies": "、".join(cfg.wake.replies),
            "wake.threshold": cfg.wake.threshold,
            "tts.enabled": cfg.tts.enabled,
            "tts.speaker_id": cfg.tts.speaker_id,
            "tts.speed": cfg.tts.speed,
            "llm.enabled": cfg.llm.enabled,
            "llm.base_url": cfg.llm.base_url,
            "llm.model": cfg.llm.model,
            "llm.api_key": "",
            "llm.api_key_set": bool(cfg.llm.resolved_key()),
            "agent.barge_in_wake": cfg.agent.barge_in_wake,
            # **别漏键**：网页版的「保存设置」是整张表单提交的，
            # 少了哪个键，前端拿到 undefined 就按"未勾选"提交回去 ——
            # 用户点一次保存，提示音就被静默关掉了（真实的 bug）。
            "agent.cues": cfg.agent.cues,
            "agent.confirm.enabled": cfg.agent.confirm.enabled,
            "agent.listen_timeout_ms": cfg.agent.listen_timeout_ms,
            # 子代理：改完立刻生效（管理器每次都从 cfg 现读，不用重启）
            "agent.subagent_enabled": cfg.agent.subagent_enabled,
            "agent.subagent_max": cfg.agent.subagent_max,
            "agent.subagent_rounds": cfg.agent.subagent_rounds,
            "agent.subagent_announce": cfg.agent.subagent_announce,
            "agent.watch_announce": cfg.agent.watch_announce,
            "speech.profile": cfg.speech.profile,
            "speech.device": cfg.speech.device,
            "speech.threads": cfg.speech.threads,
            "tts.engine": cfg.tts.engine,
            "tts.voice": cfg.tts.voice,
            # 输出音量：界面上的音量条。同时给一份百分比，省得界面自己换算
            "audio.output_gain": round(float(audio_io.get_output_gain()), 3),
            "audio.volume_percent": int(round(audio_io.get_output_gain() * 100)),
            "ui.accent": cfg.ui.accent,
            "ui.accent_hex": cfg.ui.accent_hex(),
            "ui.show_turn": cfg.ui.show_turn,
            # 设置页会直接 values.get(key) 拿这些来初始化控件；**漏掉任何一个，
            # 界面就会显示一个假值**（SegmentedControl 落到第 0 项）：
            # 追问窗口显示"0 回待命"、max_rounds 显示 6、速览显示未勾选，
            # 用户点一下反而把真实配置改成了这个假值。
            "ui.show_stats": cfg.ui.show_stats,
            "llm.max_rounds": cfg.llm.max_rounds,
            "agent.follow_up_ms": int(cfg.agent.follow_up_ms),
            # 数据目录：程序产生的文件都收在这里
            "paths.data_dir": cfg.paths.data_dir,
            "paths.data_dir_effective": str(paths.data_dir()),
            # 权限：模式与两条限流
            "security.mode": cfg.security.mode,
            "security.allow_insecure": cfg.security.allow_insecure,
            "security.max_prompts_per_minute": cfg.security.max_prompts_per_minute,
            "security.max_same_action": cfg.security.max_same_action,
            "security.audit": cfg.security.audit,
            "security.deny_tools": "、".join(cfg.security.deny_tools),
            "security.always_confirm": "、".join(cfg.security.always_confirm),
            "security.floor_tools": "、".join(cfg.security.floor_tools),
            "security.keep_floor_when_empty": cfg.security.keep_floor_when_empty,
            "llm.reasoning_effort": cfg.llm.reasoning_effort,
            "llm.vision_max_side": cfg.llm.vision_max_side,
            "llm.extra_body": "（高级：直接编辑 YAML）" if cfg.llm.extra_body else "",
            # 只读信息：让用户一眼看到最后跑在什么算力、什么引擎上
            "speech.provider_text": cfg.speech.provider_text,
            "speech.profile_note": cfg.speech.profile_note,
            # 声纹：开关、阈值，以及一句「现在到底会不会拦人」的说明
            "speaker.enabled": cfg.speaker.enabled,
            "speaker.threshold": cfg.speaker.threshold,
            "speaker.status": (self.agent.voiceprint.status_text() if self.agent
                               else ("未开启" if not cfg.speaker.enabled else "已开启，尚未加载")),
            "models_dir": str(cfg.models_dir),
            "config_path": str(self.config_path),
        }

    # ───────────────── 音量 ─────────────────

    def set_volume(self, percent: Any, persist: bool = True) -> dict:
        """调输出音量：**传百分比**（0~150），立刻生效。

        对外统一用百分比：界面上写的是 70%，配置里存的是增益 0.7，混着用迟早
        出错（早先就是这样，拖到 70% 被当成增益 70，直接夹到上限）。

        音量条拖动过程中 persist=False（只改内存里的总增益，声音马上跟着变），
        松手时才 persist=True 落盘 —— 否则每动一格就重写一次配置文件。
        """
        try:
            level = audio_io.set_output_gain(float(percent) / 100.0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "音量得是数字"}
        previous = float(self.cfg.audio.output_gain)
        result: dict = {"ok": True, "percent": int(round(level * 100))}
        if persist:
            # **别在落盘之前改 self.cfg**：_after_config_change 是靠"新旧两份配置的
            # 差集"决定要不要提示重启的，提前把内存改成新值，它就以为"什么都没变"
            # 而把之前攒的「这 1 项要重启才生效」一起清掉 —— 用户以为没事了，
            # 引擎却还在跑旧设置。成功落盘后 update_from 会把新值搬进来。
            saved = self.update_config({"audio.output_gain": round(level, 3)})
            if not saved.get("ok"):
                # 落盘失败就把实时增益也退回去：不然界面显示的音量和 config.yaml
                # 里的不一致，下次启动又变回去，谁也说不清。
                audio_io.set_output_gain(previous)
                return {"ok": False, "error": saved.get("error")}
        return result

    def set_accent(self, value: Any) -> dict:
        """换主题主色。只写配置 —— 界面自己负责刷新生效，不用重启。"""
        raw = "" if value is None else str(value).strip()
        # 同样不预写 self.cfg（理由见 set_volume）：界面上换色是它自己刷新的，
        # 配置落盘后 update_from 会把新值搬进内存。
        return self.update_config({"ui.accent": raw})

    # ───────────────── 音色 ─────────────────

    def voices_payload(self) -> dict:
        """当前引擎的音色清单，给界面下拉框用。

        两个引擎的"音色"含义不一样：vits 是 5 个固定角色音，ChatTTS 是一个
        随机种子（同一个种子永远是同一个人）。这里统一成 (id, 标签) 给界面。
        """
        cfg = self.cfg
        engine = voice_table.engine_of(cfg.tts.engine)
        current = voice_table.resolve(engine, cfg.tts.voice) if cfg.tts.voice else None
        note = ""
        if current is None:
            current, note = voice_table.sanitize(engine, cfg.tts.speaker_id)
        return {
            "engine": engine,
            "current": current,
            "current_name": voice_table.name(engine, current),
            "current_label": voice_table.label(engine, current),
            "note": note,
            "rows": [{"id": sid, "label": label, "name": voice_table.name(engine, sid)}
                     for sid, label in voice_table.catalog(engine)],
            # ChatTTS 的种子不止下拉框里那几个，用户可以自己填
            "freeform": engine == "chattts",
        }

    # 试听念的这句话：短一点，能听出音色就够了
    AUDITION_TEXT = "你好，我是大肥鲸。今天天气不错，有什么可以帮你的吗？"

    def audition_status(self) -> dict:
        """试听进度：界面靠它显示"加载中 / 正在播 / 完了 / 失败"。

        以前只返回一句"正在试听"，界面把提示写在标签上就再也没人管它 ——
        用户看到的就是"一直显示正在加载"。
        """
        return dict(self.audition)

    def clear_audition(self) -> None:
        self.audition = {"state": "idle", "voice": "", "error": "", "at": 0.0}

    def audition_voice(self, value: Any) -> dict:
        """试听一个音色：后台线程里合成并播出来，不卡界面。

        引擎没启动时，以前这里直接返回「合成引擎还没就绪」——而界面只把它写进
        运行日志，用户点了「试听」什么都没发生，也没有任何提示。现在按需把模型
        加载起来，并且把「要等几秒」这件事明确告诉界面。
        """
        cfg = self.cfg
        engine = (cfg.tts.engine or "vits").strip().lower()
        sid = voice_table.resolve(engine, value)
        if sid is None:
            return {"ok": False, "error": "认不出这个音色：" + str(value)}
        if not cfg.tts.enabled:
            return {"ok": False, "error": "语音播报已关闭（tts.enabled 改成 true 再试）"}
        try:
            agent = self.agent or self.ensure_agent()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "引擎起不来：" + str(exc)[:120]}
        label = voice_table.label(engine, sid)
        needs_load = getattr(agent, "tts", None) is None
        self.audition = {
            "state": "loading" if needs_load else "playing",
            "voice": label, "error": "", "at": time.time(),
        }

        def work() -> None:
            try:
                if getattr(agent, "tts", None) is None:
                    self.log("[试听] 正在加载合成模型……")
                    agent.load()
                tts = getattr(agent, "tts", None)
                if tts is None:
                    self.audition.update(state="error",
                                         error="合成模型没起来，先跑 doctor 看看模型齐不齐")
                    return
                self.audition.update(state="playing", at=time.time())
                tts.speaker_id = sid
                ok = tts.speak(self.AUDITION_TEXT)
                self.audition.update(
                    state="done" if ok else "error",
                    error="" if ok else "没播出来（声卡被别的程序占用了？）",
                    at=time.time(),
                )
            except Exception as exc:  # noqa: BLE001 - 试听失败不该影响主流程
                self.audition.update(state="error", error=str(exc)[:160], at=time.time())
                self.log("[试听] " + label + " 播放失败：" + str(exc)[:160])

        self.log("[试听] " + label + ("（首次要先加载模型，等几秒）" if needs_load else ""))
        threading.Thread(target=work, name="audition-voice", daemon=True).start()
        return {"ok": True, "id": sid, "voice": label, "engine": engine,
                "loading": needs_load}

    def update_config(self, updates: dict) -> dict:
        """按 a.b.c 的形式改若干项，其余内容原样保留（注释会丢，所以先备份）。

        **权限模式是个例外**：它必须先过 security.set_mode 的合法性检查
        （只能逐级放宽、名字要对）并写审计，所以这里把它转给 set_permission_mode ——
        以前两个前端都是直写配置，等于绕开了那套校验，审计里也看不到谁改了权限。
        """
        updates = dict(updates or {})
        wanted_mode = updates.pop("security.mode", None)
        result: dict = {"ok": True}
        if wanted_mode is not None:
            result = self.set_permission_mode(str(wanted_mode))
            if not result.get("ok") or not updates:
                return result
        raw: dict = {}
        if self.config_path.is_file():
            try:
                raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                return {"ok": False, "error": "现有配置无法解析：" + str(exc)[:160]}
        else:
            # 首次运行没有 config.yaml，程序是按 config.example.yaml 跑起来的。
            # 以前这里从空表开始合并，于是界面里改**任何一项**都会新建一份
            # 只有那一项的 config.yaml —— 示例里其余的取值（自定义唤醒词、
            # 语音退出词……）静默回退成内置默认值，用户完全看不出发生过什么。
            raw = dict(self.cfg.raw or {})
        if not isinstance(raw, dict):
            raw = {}
        for dotted, value in updates.items():
            if dotted in self.LIST_KEYS and isinstance(value, str):
                value = [part.strip() for part in re.split(r"[,，、]", value) if part.strip()]
            node = raw
            parts = str(dotted).split(".")
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
        # 走同一条「先验证再落盘」的通道，避免表单保存把配置写成读不回来的样子。
        # normalize_keys 把读进来的 False 键还原成 "no"，否则每存一次都会多出一个
        # false: 键，用户的 confirm.no 永远存不对。
        return self.save_config_text(
            yaml.safe_dump(
                normalize_keys(raw), allow_unicode=True, sort_keys=False, default_flow_style=False
            ),
            message="设置已更新",
            keys=list(updates),
        )

    def _apply_voice_live(self) -> None:
        """换了音色就地生效，别等重启。

        音色是唯一一个「改完必须马上听得到区别」的设置：试听用的就是这个引擎，
        要是试听是新声音、正式播报还是旧声音，那才叫难查。引擎没起来就跳过，
        下次构造时会从配置里解析。
        """
        tts = getattr(self.agent, "tts", None) if self.agent else None
        if tts is None:
            return
        try:
            payload = self.voices_payload()
            sid = int(payload.get("current", tts.speaker_id))
            if 0 <= sid < max(1, tts.num_speakers):
                tts.speaker_id = sid
                tts.speaker_note = ""
        except Exception as exc:  # 音色没切成功不该影响配置保存
            self.log("[ui] 音色即时生效失败：" + str(exc)[:120])

    @staticmethod
    def _diff_keys(before: Config, after: Config) -> list[str]:
        """两份配置之间**真的变了**的键（写成 a.b.c）。

        为什么要比对而不是"谁传了什么就算什么改了"：网页版的「原始配置」框
        是整份文本提交的，它压根说不出改了哪几行 —— 于是所有写操作里的
        「改完要不要重建模型客户端 / 要不要提示重启」记账全部落空：
        改了 llm.api_key 只弹一句"已保存"，既不生效也不提示重启。
        """
        out: list[str] = []

        def walk(prefix: str, old: Any, new: Any) -> None:
            for item in dataclasses.fields(new):
                left = getattr(old, item.name, None)
                right = getattr(new, item.name)
                if dataclasses.is_dataclass(right) and dataclasses.is_dataclass(left):
                    walk(prefix + item.name + ".", left, right)
                elif right != left:
                    out.append(prefix + item.name)

        for item in dataclasses.fields(after):
            left = getattr(before, item.name, None)
            right = getattr(after, item.name)
            if dataclasses.is_dataclass(right) and dataclasses.is_dataclass(left):
                walk(item.name + ".", left, right)
            elif right != left:
                out.append(item.name)
        return out

    def _after_config_change(self, message: str, keys: list[str] | None = None) -> dict:
        """保存后把新配置搬到内存里，并判断要不要提示重启。

        关键是**就地更新**（Config.update_from）：agent 和它内部的 wake/asr/tts
        都握着一开始那份 cfg 的引用，换成新对象的话它们永远读不到新值。
        """
        previous = self.cfg
        try:
            fresh = Config.load(self.config_path)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        # 先算差集，再就地更新（update_from 会把 self.cfg 改掉，之后就比不出来了）
        changed = self._diff_keys(previous, fresh)
        keys = sorted(set(changed) | {str(key) for key in (keys or [])})
        self.cfg.update_from(fresh)
        # 权限设置也是"改完立刻生效"，而且它决定工具层放不放行
        security.configure(self.cfg)
        # 数据目录可能被改了：立刻生效（已经在写的文件不受影响）
        self._apply_data_dir(self.cfg)
        self._apply_voice_live()
        # 看图分辨率是"下次截图时读一次"，改完立刻推给工具层
        tools.set_vision_max_side(self.cfg.llm.vision_max_side)
        # 动了 llm.*（多模型档案、密钥、地址）就把客户端重建，不然要重启才生效
        if any(str(key).startswith("llm.") for key in (keys or [])):
            brain = getattr(self.agent, "brain", None)
            if brain is not None:
                try:
                    brain.reload_clients()
                except Exception as exc:  # noqa: BLE001 - 换模型失败不该影响保存
                    self.log("[ui] 重建模型客户端失败：" + str(exc)[:100])
        audio_io.set_output_gain(self.cfg.audio.output_gain)

        # 只把「真的需要重启」的记下来；引擎没在跑就不用提示（下次启动自然是新的）
        running = bool(self.agent and self.agent.running)
        if running:
            # 这一遍下来配置内容和之前一模一样（改了又改回去）→ 旧的提示作废
            if not changed:
                self.missing_restart.clear()
            for key in keys:
                label = self.RESTART_KEYS.get(key)
                if label:
                    self.missing_restart[key] = label
        pending = sorted(self.missing_restart.values())
        note = ("；这几项要重启才生效：" + "、".join(pending)) if pending else ""
        self.log("[ui] " + message + note)
        return {"ok": True, "message": message, "restart_needed": bool(pending),
                "restart_items": pending}

    # ───────────────── 技能 ─────────────────

    def skill_dirs(self) -> tuple[Path, ...]:
        """技能与自定义工具的搜索目录，外加可注入的目标目录（测试用）。"""
        dirs = list(SKILL_DIRS)
        for extra in (self.skill_dir, self.tool_dir):
            if extra not in dirs:
                dirs.append(extra)
        return tuple(dirs)

    def reload_skills(self, announce: bool = True) -> list:
        tools.reset_skills()   # 先卸载上一轮，删掉的技能才不会继续存在
        loader = SkillLoader(self.skill_dirs())
        self.skills = loader.load_all()
        # 两个文件声明了同一个工具名时，后加载的会覆盖前面的，但两个都会显示「正常」——
        # 界面上就成了「两个都装好了，删掉其中一个却没变化」。这里明确标出来。
        seen: dict[str, str] = {}
        for skill in self.skills:
            if not skill.ok:
                continue
            for tool_name in skill.tools:
                if tool_name in seen:
                    skill.error = ("工具名 " + tool_name + " 与 " + Path(seen[tool_name]).name
                                   + " 重复，实际生效的是后加载的那个")
                    skill.tools = []
                    break
                seen[tool_name] = str(skill.source)
        bad = [s for s in self.skills if not s.ok]
        if announce:
            self.log("[skills] 加载 " + str(len(self.skills)) + " 个技能文件，"
                     + ("其中 " + str(len(bad)) + " 个有问题" if bad else "全部正常"))
        return self.skills

    def save_skill(self, filename: str, content: str, kind: str = "skill") -> dict:
        """保存一个自定义技能 / 工具。

        kind="tool" 写到 tools/ 目录 —— 文件格式、加载方式完全一样，
        只是界面上归类成「自定义工具」，和内置工具并排站。
        """
        name = Path(str(filename or "")).name
        if not name.endswith((".yaml", ".yml")):
            name += ".yaml"
        target = (self.tool_dir if str(kind).lower() == "tool" else self.skill_dir) / name
        try:
            data = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            return {"ok": False, "error": "YAML 语法错误：" + str(exc)[:200]}
        if not isinstance(data, dict) or not data.get("name"):
            return {"ok": False, "error": "至少要有一个 name 字段"}
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": "写入失败：" + str(exc)[:120]}
        self.reload_skills()
        match = [s for s in self.skills if str(s.source) == str(target)]
        if match and not match[0].ok:
            return {"ok": False, "error": match[0].error, "path": str(target)}
        self.log("[skills] 已保存 " + str(target))
        return {"ok": True, "path": str(target)}

    def install_skill_deps(self, path: str) -> dict:
        """给某个技能 / 自定义工具装它声明的依赖，装完自动重新加载。

        没有这个入口时，用户唯一的办法是看到报错、自己开终端敲 pip ——
        而这恰恰是"自定义工具"最容易卡住的地方。
        """
        target = Path(str(path or "")).resolve()
        info = next((s for s in self.skills if str(s.source) == str(target)), None)
        if info is None:
            return {"ok": False, "error": "找不到这个文件，先点「重新加载」再看一眼"}
        packages = list(info.missing) or [dep_names(d)[1] for d in info.deps]
        if not packages:
            return {"ok": False, "error": "这个文件没有声明 deps，没什么可装的"}
        self.log("[skills] 正在安装 " + "、".join(packages) + " ……")
        result = install_deps(packages)
        self.reload_skills(announce=False)
        again = next((s for s in self.skills if str(s.source) == str(target)), None)
        still = list(again.missing) if again is not None else []
        if result.get("ok"):
            self.log("[skills] 装好了：" + "、".join(packages))
        else:
            self.log("[skills] 装依赖失败：" + str(result.get("error")))
        return {**result, "missing": still,
                "loaded": bool(again is not None and again.ok)}

    def skill_roots(self) -> list[Path]:
        return [d.resolve() for d in self.skill_dirs()]

    def read_skill(self, path: str) -> dict:
        """读回技能原文，供界面里编辑。"""
        target = Path(str(path or "")).resolve()
        if not any(target.is_relative_to(root) for root in self.skill_roots()) or not target.is_file():
            return {"ok": False, "error": "只能读取技能目录里的文件"}
        try:
            return {
                "ok": True,
                "path": str(target),
                "name": target.name,
                "content": target.read_text(encoding="utf-8"),
            }
        except (OSError, UnicodeDecodeError) as exc:
            return {"ok": False, "error": "读取失败：" + str(exc)[:120]}

    def delete_skill(self, path: str) -> dict:
        target = Path(str(path or "")).resolve()
        allowed = [d.resolve() for d in SKILL_DIRS] + [self.skill_dir.resolve()]
        if not any(target.is_relative_to(root) for root in allowed):
            return {"ok": False, "error": "只能删除技能目录里的文件"}
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                return {"ok": False, "error": "删不掉：" + str(exc)[:120]}
            self.reload_skills()
            self.log("[skills] 已删除 " + str(target))
            return {"ok": True}
        return {"ok": False, "error": "文件不存在"}

    # ───────────────── 状态快照 ─────────────────

    # ───────────────── 工具 / 设备（两个前端共用）─────────────────

    def call_tool(self, name: str, args: Any = None, allow_sensitive: bool = False) -> dict:
        """试运行一个工具。

        敏感工具默认拒绝：界面上的按钮不该成为绕过语音确认的后门。
        """
        tools.autoload_skills()
        entry = tools.REGISTRY.get(str(name or ""))
        if entry is None:
            return {"ok": False, "error": "没有这个工具：" + str(name)}
        if entry.confirm and not allow_sensitive:
            return {"ok": False,
                    "error": "「" + entry.display + "」属于敏感操作，请对着麦克风说一遍再确认"}
        outcome = tools.call_result(entry.name, args or {})
        self.log("[ui] 试运行 " + entry.name + " → " + str(outcome.text)[:80])
        # 按工具自己的成败上报：以前无论结果如何都回 ok=True，
        # 于是"用户取消了这次操作""被安全闸门拒绝了"都会显示成绿色成功。
        if not outcome.ok:
            return {"ok": False, "error": str(outcome.text), "code": outcome.code}
        return {"ok": True, "result": outcome.text}

    def devices(self) -> dict:
        """列出可用的输入 / 输出设备，供界面下拉框使用。"""
        try:
            import sounddevice as sd  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "读不到音频设备：" + str(exc)[:120]}
        inputs: list[dict] = []
        outputs: list[dict] = []
        try:
            default_in, default_out = sd.default.device
        except Exception:
            default_in = default_out = None
        for index, dev in enumerate(sd.query_devices()):
            item = {"index": index, "name": dev.get("name", "?"),
                    "rate": int(dev.get("default_samplerate") or 0)}
            if dev.get("max_input_channels", 0) > 0:
                item["default"] = index == default_in
                inputs.append(dict(item))
            if dev.get("max_output_channels", 0) > 0:
                item["default"] = index == default_out
                outputs.append(dict(item))
        return {
            "ok": True,
            "input": inputs,
            "output": outputs,
            "current": {
                "input": self.cfg.audio.input_device,
                "output": self.cfg.audio.output_device,
                "resolved_input": self.agent.status().get("input_device") if self.agent else None,
                "resolved_output": self.agent.status().get("output_device") if self.agent else None,
            },
        }

    def tools_payload(self) -> list[dict]:
        """工具清单：两个界面（PyQt 桌面版 / 网页版）都用这一份。"""
        tools.autoload_skills()
        return [
            {
                "name": tool.name,
                "title": tool.display,
                "description": tool.description,
                "confirm": tool.confirm,
                # "只有动手时才问"的工具（查权限模式 vs 改权限模式）：
                # 界面上要能区分，否则用户会以为"查一下也要我点头"
                "confirm_if": bool(tool.confirm_if),
                # 同一类操作（鼠标/键盘）在一条指令里只问一次 —— 界面上写清楚，
                # 用户才知道"为什么这次没问"
                "group": tool.group,
                # 结果进模型上下文的字符预算（0 = 用默认值）
                "result_budget": int(tool.result_budget or 0),
                "source": tool.source,
                # builtin / skill / tool（tools/ 目录里的自定义工具）
                "builtin": tool.source == "builtin",
                # 用途标签：界面上和模型看到的是同一份，用户能一眼对上是"哪个工具干的"
                "tags": list(tool.tags),
                "params": list((tool.parameters.get("properties") or {}).keys()),
            }
            for tool in tools.REGISTRY.values()
        ]

    def skills_payload(self) -> dict:
        """技能 / 自定义工具页需要的数据：清单 + 目录 + 两个新建模板。"""
        return {
            "dirs": [str(d) for d in self.skill_dirs()],
            "project_dir": str(self.skill_dir),
            "project_tool_dir": str(self.tool_dir),
            "items": [s.as_dict() for s in self.skills],
            "template": skill_template(),
            "tool_template": tool_template(),
        }

    def record_once(self, timeout: float = 12.0) -> dict:
        """录一句并识别（界面的「录音测试」）。同一时刻只允许一路。"""
        if self.agent is not None and self.agent.running:
            return {"ok": False, "error": "正在监听中，请先停止引擎再做录音测试"}
        if not self.begin_recording():
            return {"ok": False, "error": "已经在录音了，请稍候"}
        try:
            from . import audio as audio_io  # noqa: PLC0415

            agent = self.ensure_agent()
            try:
                if agent.vad is None:
                    agent.load()
            except Exception as exc:  # noqa: BLE001 - 模型不齐时 load 会抛
                # 这个函数可能从 Qt 槽里被调用，异常逃出去会让进程直接 abort
                self.log("[ui] 录音测试起不来：" + str(exc)[:120])
                return {"ok": False, "error": "语音模型没加载起来：" + str(exc)[:120]}
            device = audio_io.resolve_device(self.cfg.audio.input_device, "input")
            mic = audio_io.Mic(device=device, sample_rate=self.cfg.audio.sample_rate,
                               block_size=self.cfg.audio.block_size)
            mic.start()
            self.log("[ui] 开始录音测试……")
            utterance = None
            deadline = time.monotonic() + max(3.0, min(float(timeout or 12.0), 30.0))
            try:
                while time.monotonic() < deadline:
                    block = mic.read(timeout=0.2)
                    if block is None:
                        continue
                    utterance = agent.vad.feed(block) or utterance
                    if utterance is not None:
                        break
                utterance = utterance or agent.vad.flush()
            finally:
                mic.close()
            if utterance is None or utterance.size == 0:
                return {"ok": False, "error": "没听到人声，检查一下麦克风"}
            text = agent.asr.transcribe(utterance, punctuate=True)
            self.log("[ui] 录音测试识别：" + text)
            return {"ok": True, "text": text,
                    "seconds": round(utterance.size / self.cfg.audio.sample_rate, 2)}
        finally:
            self.end_recording()

    def snapshot(self) -> dict:
        agent = self.agent
        status = agent.status() if agent is not None else {
            "running": False, "state": "idle", "state_text": "未启动", "speaking": False,
            "models_loaded": False, "mode": "离线规则", "llm_ready": False,
            "wake_words": list(self.cfg.wake.keywords), "wake_hits": 0,
            "last_heard": "", "last_reply": "", "turns": 0,
            "mic_level": 0.0, "asr_rtf": 0.0, "tts_rtf": 0.0,
            "input_device": None, "output_device": None,
            "transcript": [], "follow_up_ms": int(self.cfg.agent.follow_up_ms),
            "listen_timeout_ms": int(self.cfg.agent.listen_timeout_ms),
            "listen_heard": False, "listen_ms": 0,
            "subagents": {"enabled": bool(self.cfg.agent.subagent_enabled),
                          "total": 0, "running": 0, "text": ""},
            "watches": {"total": 0, "running": 0, "text": ""},
        }
        good = sum(1 for s in self.skills if s.ok)
        return {
            "ok": True,
            "version": __version__,
            "starting": self._starting,
            "last_error": self.last_error,
            "config_path": str(self.config_path),
            "models_dir": str(self.cfg.models_dir),
            "missing_models": list(self.cfg.missing_models),
            "tools": len(tools.REGISTRY),
            "skills": {"total": len(self.skills), "ok": good},
            "status": status,
            # 有设置改了但还没重启引擎：主界面据此弹重启提示
            "restart_needed": bool(self.missing_restart),
            "restart_items": self.pending_restart(),
            # 权限状态：界面上要能一眼看到"现在是什么模式、链路可不可信"
            "security": security.snapshot(),
            # 数据目录清单（设置页展示 + 打开目录用）
            "paths": paths.describe(),
            # 日志文件在哪（界面上给用户"打开日志目录"用；闪退了也能照这个去找）
            "log_file": str(journal.file_path()),
        }

    def open_path_in_shell(self, target) -> str:  # noqa: ANN001
        """在资源管理器里打开一个目录（设置页的「打开目录」用）。"""
        from .tools.windows import open_folder

        return open_folder(str(target))

    def set_permission_mode(self, value: str) -> dict:
        """换权限模式（界面上的用户操作，不需要再确认一遍）。"""
        ok, message = security.set_mode(value, "界面操作")
        if not ok:
            return {"ok": False, "error": message}
        self.log("[ui] " + message)
        return {"ok": True, "message": message, "mode": security.mode()}
