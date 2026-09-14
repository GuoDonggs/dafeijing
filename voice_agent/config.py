# -*- coding: utf-8 -*-
"""配置加载：YAML → dataclass，并负责模型文件路径解析。

两个刻意的设计：

1. **每个配置项都有默认值**，config.yaml 可以只写想改的那几行；
2. **模型目录自动探测**：先看项目内 ./models，再看同机的 ../voice-assistant/models，
   避免为了跑一个 demo 重复下载 500 MB 权重。都没有时给出可执行的下载提示。
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.yaml"

# 老布局的模型目录（程序目录旁边 / 隔壁项目）。新布局由 models_dir_candidates() 拼。
LEGACY_MODEL_DIRS = (
    PROJECT_ROOT / "models",
    PROJECT_ROOT.parent / "voice-assistant" / "models",
)
#: 兼容旧名字
MODEL_DIR_CANDIDATES = LEGACY_MODEL_DIRS


def models_dir_candidates(explicit: Any = None) -> tuple[Path, ...]:
    """模型目录的探测顺序（谁在前用谁）。

    1. 显式指定的（配置里写 models_dir / paths.models_dir，或环境变量）；
    2. **<数据目录>/models** —— 自动下载就装在这里，和 build/、logs/、downloads/
       同一个父目录，用户备份/迁移只要搬一个目录；
    3. 程序目录/models、../voice-assistant/models —— 老用户的现成布局，不能让人重下。
    """
    from . import paths  # noqa: PLC0415 - 放函数里，避免模块级循环导入

    order: list[Path] = []
    if explicit:
        order.append(Path(str(explicit)).expanduser())
    env = os.environ.get("VOICE_AGENT_MODELS_DIR", "").strip()
    if env:
        order.append(Path(env).expanduser())
    order.append(paths.data_dir() / "models")
    order.extend(LEGACY_MODEL_DIRS)
    return tuple(order)

# sherpa-onnx 官方模型包名（scripts/download_models.py 会下到这些目录里）
KWS_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
ASR_DIR = "sherpa-onnx-paraformer-zh-2023-09-14"
PUNCT_DIR = "sherpa-onnx-punct-ct-transformer-zh-en-vocab272727-2024-04-12-int8"
TTS_DIR = "sherpa-onnx-vits-zh-ll"
# ChatTTS 不走 sherpa-onnx，权重由 ChatTTS 自己的包从 HuggingFace 拉，
# 所以这里没有对应的 MODEL_FILES 条目。

# 每个模型需要的文件：{逻辑名: 相对 models 的路径}
MODEL_FILES: dict[str, str] = {
    "vad": "silero_vad.onnx",
    "kws_tokens": KWS_DIR + "/tokens.txt",
    "kws_encoder": KWS_DIR + "/encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "kws_decoder": KWS_DIR + "/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "kws_joiner": KWS_DIR + "/joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
    "asr_model": ASR_DIR + "/model.int8.onnx",
    "asr_tokens": ASR_DIR + "/tokens.txt",
    "punct_model": PUNCT_DIR + "/model.int8.onnx",
    "tts_model": TTS_DIR + "/model.onnx",
    "tts_tokens": TTS_DIR + "/tokens.txt",
    "tts_lexicon": TTS_DIR + "/lexicon.txt",
}

# 这些缺了不影响运行（有回退方案），所以不参与「模型不齐」的判定
OPTIONAL_MODELS = ("punct_model",)


def sherpa_gpu_built() -> bool:
    """sherpa-onnx 这个安装包本身有没有把 GPU 编进去。

    这一步很关键：官方 pip wheel 是 **CPU-only** 构建，即使机器上有显卡、
    即使 onnxruntime-gpu 里列出了 CUDAExecutionProvider，sherpa 也会在
    初始化时打印一句「Please compile with -DSHERPA_ONNX_ENABLE_GPU=ON」然后
    悄悄退回 CPU。只看 onnxruntime 就宣称「正在用 GPU」是会误导人的。
    """
    try:
        import sherpa_onnx  # noqa: PLC0415

        root = Path(sherpa_onnx.__file__).parent
    except Exception:  # noqa: BLE001
        return False
    markers = ("cuda", "cudnn", "cublas")
    for pattern in ("*.dll", "*.so", "*.so.*", "*.dylib"):
        for path in root.rglob(pattern):
            name = path.name.lower()
            if any(marker in name for marker in markers):
                return True
    return False


def detect_provider(requested: str) -> tuple[str, str]:
    """把配置里的 auto/cpu/cuda 解析成 onnxruntime 的 provider，并给一句人话说明。

    做成函数而不是常量，是因为「本机到底有没有 GPU」要现场问 onnxruntime，
    而且要在界面上如实告诉用户最终用的是哪一个 —— 请求了 GPU 却悄悄跑在 CPU 上
    是最容易让人误判性能问题的情况。
    """
    want = (requested or "auto").strip().lower()
    if want in ("cpu",):
        return "cpu", "CPU（配置指定）"
    try:
        import onnxruntime as ort  # noqa: PLC0415

        has_cuda = "CUDAExecutionProvider" in set(ort.get_available_providers())
    except Exception:  # noqa: BLE001
        return "cpu", "CPU（读不到 onnxruntime）"
    if not has_cuda:
        return "cpu", "CPU（onnxruntime 里没有 CUDA）"
    if not sherpa_gpu_built():
        # 说清楚原因，否则用户会以为「配了 GPU 却没变快」是别的问题
        return "cpu", "CPU（sherpa-onnx 是 CPU 版构建，需换 GPU 版才能用显卡）"
    return "cuda", "GPU（CUDA）"

# TTS 的 FST 规则（数字/日期/电话/多音字），存在时读得更自然
TTS_RULE_FSTS = (
    TTS_DIR + "/date.fst",
    TTS_DIR + "/new_heteronym.fst",
    TTS_DIR + "/number.fst",
    TTS_DIR + "/phone.fst",
)


class ConfigError(RuntimeError):
    """配置或模型缺失；消息里直接给出下一步该做什么。"""


# YAML 1.1 把 yes/no/on/off 一律当布尔值：写成 {`no`: [...]} 的文件读出来键是 False。
# PyYAML 保存时又会把字符串 "no" 加引号写成 'no'，两边都合法，所以读取时必须还原，
# 否则用户在 confirm.no 里写的词会被静默忽略，只剩默认值在生效。
_BOOL_KEY_NAMES = {True: "yes", False: "no"}


def normalize_keys(node: Any) -> Any:
    """递归把布尔键还原成 yes/no 字符串，其余原样保留。"""
    if isinstance(node, dict):
        out: dict = {}
        for key, value in node.items():
            if isinstance(key, bool):
                key = _BOOL_KEY_NAMES[key]
            out[key if isinstance(key, str) else str(key)] = normalize_keys(value)
        return out
    if isinstance(node, list):
        return [normalize_keys(item) for item in node]
    return node


def _str_list(value: Any, default: list[str]) -> list[str]:
    """把配置项统一成非空字符串列表。

    用户在 YAML 里很容易把列表写成单个字符串：

        wake:
          keywords: 大肥鲸        # 少了方括号

    直接 for 循环会逐字符迭代，得到 ['小','爱','同','学'] —— 相当于配了四个
    单字唤醒词，后果是满屋子的误唤醒。所以这里统一兼容「字符串 / 列表」两种写法，
    字符串还会按中英文逗号、顿号切开。
    """
    if value is None:
        return list(default)
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[,，、]", value)]
        items = [item for item in items if item]
        return items or list(default)
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if str(item).strip()]
        return items or list(default)
    return list(default)


def _get(mapping: Any, key: str, default: Any) -> Any:
    """从 dict 里取 a.b.c 形式的键，缺省返回 default。"""
    node: Any = mapping
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


# ─────────────────────────── 分层配置 ───────────────────────────


# 三档预设：直接决定「占多少资源」和「合成好不好听」。
# 实测（本机 RTX 4060 Ti）：VITS 合成 3.9 秒音频用 1.0 秒（纯 CPU）；
# ChatTTS 合成 4~5 秒音频要 4.5~7 秒（显卡），自然度高得多但慢。
# 所以默认给"够快"的 VITS，想要更好听的人自己切到 quality。
SPEECH_PROFILES: dict[str, dict] = {
    "fast": {"threads": 2, "tts_engine": "vits",
             "note": "低占用：线程最少、用最快的合成，适合后台常驻"},
    "balanced": {"threads": 4, "tts_engine": "vits",
                 "note": "默认：速度和资源平衡"},
    "quality": {"threads": 6, "tts_engine": "chattts", "tts_speed": 1.06,
                "note": "高质量：ChatTTS 对话式合成，明显更自然，但要显卡、更慢"},
}


@dataclass
class SpeakerCfg:
    """声纹：只有主人的声音能唤醒。默认关闭 —— 配错了会把自己锁在门外。"""

    enabled: bool = False
    model: str = ""          # 留空 = 自动找 models/speaker/*.onnx
    profile: str = ""        # 留空 = build/voiceprint.json
    threshold: float = 0.55  # 余弦相似度阈值，越高越严格


@dataclass
class SpeechCfg:
    """语音推理的算力配置：CPU / GPU、线程数、占用档位。"""

    device: str = "auto"      # auto | cpu | cuda
    threads: int = 0          # 0 = 按 profile 自动；也可以直接写 2 / 4 / 8
    profile: str = "balanced"  # fast | balanced | quality
    profile_note: str = ""
    provider: str = "cpu"     # 由 device 解析出来的实际值
    provider_text: str = ""

    def resolve(self) -> None:
        self.provider, self.provider_text = detect_provider(self.device)

    @property
    def low_footprint(self) -> bool:
        return 0 < self.threads <= 2

    def num_threads(self, default: int = 4) -> int:
        return max(1, int(self.threads) if self.threads else int(default))


@dataclass
class AudioCfg:
    input_device: Any = None
    output_device: Any = None
    sample_rate: int = 16000
    block_ms: int = 32
    input_gain: float = 1.0
    # 输出音量：界面上那个音量条改的就是它。play() 里当作总增益，
    # 所以播报和提示音一起变，而且**立刻生效**，不用重启。
    output_gain: float = 1.0

    @property
    def block_size(self) -> int:
        return max(160, int(self.sample_rate * self.block_ms / 1000))

    @property
    def output_percent(self) -> int:
        return int(round(max(0.0, min(float(self.output_gain), 2.0)) * 100))


# 界面主题：深色底 + 一个可换的主色。主色会同时影响桌面窗口和网页控制台。
ACCENT_PRESETS: dict[str, str] = {
    "blue": "#0A84FF",     # iOS 系统蓝，默认
    "teal": "#32D0C6",
    "green": "#30D158",
    "purple": "#BF5AF2",
    "pink": "#FF375F",
    "orange": "#FF9F0A",
    "indigo": "#5E5CE6",
    "graphite": "#8E8E93",
}


def resolve_accent(value: Any) -> str:
    """把主题色写法统一成 #RRGGBB。

    接受预设名（blue / teal / …）和任意 #RRGGBB；认不出来回退默认蓝。
    注意别直接丢给 QColor：Qt 认识 CSS 的那套颜色名，QColor("teal") 会得到
    #008080（CSS 的 teal），而不是我们预设里的那个青绿。
    """
    raw = str(value or "").strip()
    if not raw:
        return ACCENT_PRESETS["blue"]
    if raw.lower() in ACCENT_PRESETS:
        return ACCENT_PRESETS[raw.lower()]
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", raw):
        return raw if raw.startswith("#") else "#" + raw
    return ACCENT_PRESETS["blue"]


@dataclass
class PathsCfg:
    """程序自己产生的文件放哪（见 voice_agent/paths.py）。"""

    # 留空 = 「程序目录/build」。改成一个绝对路径就能把记忆、截图、缓存、
    # 审计日志全部挪到别处（比如 D 盘）。
    data_dir: str = ""


@dataclass
class SecurityCfg:
    """权限：这个助手能动本机的什么。见 voice_agent/security.py 的长注释。"""

    # 只读 / 标准 / 放开。名字沿用 DSH 的 sandbox 词表。
    mode: str = "workspace-write"
    # 明文 HTTP 的模型地址要不要照用。默认 False：那种链路上任何人都能
    # 改写模型的回答（也就能塞工具调用），所以自动降到只读。
    allow_insecure: bool = False
    # 一分钟内最多弹几次确认（防"疲劳战术"），0 = 不限
    max_prompts_per_minute: int = 6
    # 同一个操作连着要几次就拦下，0 = 不限
    max_same_action: int = 3
    # 一律拒绝的工具名（最高优先级，任何模式都不放行）。
    # 例：["run_command", "write_file"] —— 只想让它查、不想让它改的用法。
    deny_tools: list[str] = field(default_factory=list)
    # 额外要求确认的工具名（在下面的"底线名单"之外再加）
    always_confirm: list[str] = field(default_factory=list)
    # 「放开」模式下**仍然要确认**的底线名单。默认是执行命令 / 关机 / 杀进程 /
    # 重启退出程序 / 定时盯梢 —— 这几个是最危险的动作（盯梢会反复跑用户给的
    # 命令，等于一条不需要确认的执行旁路），放开权限不该等于把底线交出去。
    # 想真的完全不问：把这里清空，并把 keep_floor_when_empty 设成 false。
    floor_tools: list[str] = field(default_factory=lambda: [
        "run_command", "power", "kill_process", "restart_self", "quit_self",
        "start_watch"])
    # 名单被清空时，要不要保留内置的那几个底线（默认保留，安全优先）
    keep_floor_when_empty: bool = True
    # 审计日志（build/audit.jsonl）
    audit: bool = True


@dataclass
class UiCfg:
    # 主色：写预设名（blue / teal / …）或任意 #RRGGBB。空 = 用默认蓝。
    accent: str = ""
    # 主界面底部是否显示速览四格（小屏想更清爽可以关掉）
    show_stats: bool = True
    # 主界面是否显示"本轮问答"（刚才听到的提问 + 助手的回复）。
    # 开着的好处是不用点开菜单就知道它听成了什么 —— 识别错了能当场发现。
    show_turn: bool = True

    def accent_hex(self) -> str:
        return resolve_accent(self.accent)


@dataclass
class WakeCfg:
    enabled: bool = True
    keywords: list[str] = field(default_factory=lambda: ["大肥鲸"])
    threshold: float = 0.25
    score: float = 1.5
    cooldown_ms: int = 1500
    replies: list[str] = field(default_factory=lambda: ["我在"])
    reply_random: bool = True
    num_trailing_blanks: int = 1


@dataclass
class AsrCfg:
    punctuation: bool = True
    num_threads: int = 4
    # 留空 = 跟 speech.device（推荐）。写 cpu / cuda 可以只给识别单独指定算力。
    # 以前这里默认写死 "cpu"，而这个字段没人读 —— 用户在配置里写
    # asr.provider: cuda 完全没反应，因为真正生效的一直是 speech.device。
    provider: str = ""
    corrections: list[list[str]] = field(default_factory=list)


@dataclass
class TtsCfg:
    enabled: bool = True
    # vits（默认）：vits-zh-ll 的 5 人角色音，纯 CPU、快、模型 130 MB
    # chattts     ：ChatTTS 对话式中文，自然得多；要显卡、显存约 2 GB、慢
    engine: str = "vits"
    num_threads: int = 2
    # 留空 = 跟 speech.device（推荐）；写 cpu / cuda 可以只给合成单独指定算力
    provider: str = ""
    # ChatTTS 专用：用哪块设备，以及要不要 torch.compile 加速
    device: str = "auto"
    compile: bool = False
    # 音色：vits 写名字（suyingxue）或编号；chattts 写种子（seed42 或直接 42）。
    # 留空则用 speaker_id。
    voice: str = ""
    speaker_id: int = 0
    speed: float = 1.0
    volume: float = 1.0
    # 响度按 RMS 定（0.10 约等于正常说话），峰值交给软限幅收在 target_peak 以内。
    # 早先按峰值归一化，等于把底噪一起抬 9 dB，安静时能听见嘶嘶声。
    target_rms: float = 0.10
    target_peak: float = 0.9
    # 底噪门：低于这个电平（dBFS）的段落往下压一点，最大压 8 dB。
    # 只想碰到真正的静音，不碰软辅音；设成 -100 就是关掉。
    noise_gate: float = -55.0
    max_gain: float = 3.0
    # 朗读前把英文单词音译成汉字：vits-zh 念不出拉丁词，deepseek / hello
    # 这种会被当成 OOV **整词丢掉**（听起来就是"这句话少了一截"）。
    # 内置表 + 学习缓存零延迟，只有没见过的新词才会问一次模型。
    translit: bool = True
    # 语气参数。确认那句默认放慢一点：**听不清就没法确认**，
    # 而确认是整个安全模型里唯一的人工闸门。
    styles: dict[str, dict] = field(default_factory=lambda: {"confirm": {"speed": 0.94}})

    def style(self, kind: str) -> dict:
        """某种语气下的合成参数。

        只并进「这个语气显式改了」的项。以前这里无条件塞 speaker_id，结果
        引擎侧解析好的音色会被配置里的原始下标盖掉 ——
        想换音色怎么都换不动，就是这么来的。
        要让某种语气用不同音色，在 styles 里显式写 speaker_id 即可。
        """
        base: dict = {"speed": self.speed}
        base.update(self.styles.get(kind) or {})
        return base


# 用例 → 说明：不同用途可以挂不同的模型
LLM_PURPOSES = {
    "chat": "主对话（要会调用工具）",
    "judge": "判定「确认 / 取消」这类小问题（要快）",
    "vision": "看图（屏幕找图、截图理解，需要支持图片输入的模型）",
}


@dataclass
class LlmCfg:
    enabled: bool = True
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    api_key: str = ""
    temperature: float = 0.3
    # 一轮对话里最多让模型调几次工具。6 太少：稍微多步一点的任务
    # （"看看磁盘、再打开浏览器、顺便搜一下"）就会撞上限，
    # 用户听到的是"这件事分了好几步还没做完"。
    max_rounds: int = 12
    timeout_s: float = 30.0
    # 思考程度：off / low / medium / high / max。越深越慢越贵。
    # 语音场景默认 low —— 思考是"看不见的沉默"，用户只听到助手不说话。
    reasoning_effort: str = "low"
    # 各家网关的专有字段（例如 DeepSeek 的 thinking、Qwen 的 enable_thinking），
    # 直接原样并进请求体，避免为了支持某个厂商去改代码
    extra_body: dict = field(default_factory=dict)
    # 看图时送给视觉模型的截图最长边（像素）。截图是 4K 的话，
    # 一张图几百万像素，又慢又贵，而识别效果几乎没区别；
    # 但字小的界面缩太狠会看不清，所以做成可调。
    vision_max_side: int = 1280
    # 多模型路由：用途 → profiles 里的名字
    routes: dict = field(default_factory=dict)
    # 多个模型档案：名字 → {base_url, model, api_key, reasoning_effort, ...}
    profiles: dict = field(default_factory=dict)
    # 由 resolve() 填上，用来标识「这份配置是给哪个用途的」
    purpose: str = "chat"
    name: str = ""
    vision: bool = False

    def resolved_key(self) -> str:
        """配置里没写就从环境变量取（DeepSeek 优先）。"""
        if self.api_key.strip():
            return self.api_key.strip()
        for env in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
            value = os.environ.get(env, "").strip()
            if value:
                return value
        return ""

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.resolved_key() and self.model and self.base_url)

    def resolve(self, purpose: str = "chat") -> "LlmCfg":
        """取某个用途实际使用的模型配置。

        routes 说明「用途 → 档案名」，档案里没写的字段回落到顶层默认值；
        没有配 profiles 时就等于顶层这一组，行为和以前完全一致。
        """
        name = str((self.routes or {}).get(purpose) or "")
        raw = (self.profiles or {}).get(name) if name else None
        if not isinstance(raw, dict):
            merged = replace(self, purpose=purpose, name=name or "默认")
            merged.vision = bool(self.vision)
            return merged
        merged = replace(
            self,
            base_url=str(raw.get("base_url") or self.base_url),
            model=str(raw.get("model") or self.model),
            # 档案里没写 key 就沿用顶层的，避免每加一个档案都要重填一次密钥
            api_key=str(raw.get("api_key") or self.api_key),
            temperature=float(raw.get("temperature", self.temperature)),
            timeout_s=float(raw.get("timeout_s", self.timeout_s)),
            reasoning_effort=str(raw.get("reasoning_effort", self.reasoning_effort)),
            vision=bool(raw.get("vision", False)),
            purpose=purpose,
            name=name,
        )
        merged.routes = {}
        merged.profiles = {}
        return merged

    def describe(self, purpose: str = "chat") -> str:
        resolved = self.resolve(purpose)
        effort = resolved.reasoning_effort or "默认"
        return resolved.model + "（思考 " + effort + "）"


@dataclass
class ConfirmCfg:
    enabled: bool = True
    timeout_ms: int = 10000
    # 判定顺序：先看 no，再看 yes —— 「不行」里含「行」，先判 no 才不会误放行。
    # 词表只兜底，语义判定交给 LLM（brain.judge），所以这里只收明确的口语说法。
    yes: list[str] = field(
        default_factory=lambda: [
            "确认", "确定", "可以", "同意", "执行", "继续", "没问题",
            "好的", "好", "行", "是", "对", "弄吧", "干吧", "yes", "ok",
        ]
    )
    no: list[str] = field(
        default_factory=lambda: [
            "取消", "不要", "不用", "别", "停", "否", "算了", "先不", "不想", "不必",
            "不好", "不行", "no",
        ]
    )
    prompt: str = "这是敏感操作，确认执行吗？"


@dataclass
class AgentCfg:
    barge_in_wake: bool = True
    listen_timeout_ms: int = 8000
    # ── 连续对话（追问窗口）──
    # 一次任务做完之后，要不要继续收音一小会儿，让用户不用再喊一次唤醒词。
    # 这是「像人」和「像命令行」之间最关键的一处差别。
    #
    # auto（默认）：由 LLM 决定 —— 它反问了、或者主动调了 keep_listening，
    #               才留窗口；只是汇报个结果就回待命。
    # always      ：只要窗口 > 0 就每次都留（老行为）。
    # off         ：从不留窗口，说完就回待命。
    follow_up_mode: str = "auto"
    # 窗口长度（毫秒）；<= 0 视为不留窗口
    follow_up_ms: int = 6000
    # 收音/确认/结束的提示音，让用户知道什么时候该说话
    cues: bool = True
    # 单句的**兜底**上限（毫秒）：VAD 内部的"最长一段"用它和
    # listen_hard_limit_ms 里较大的那个，正常收尾靠的是静音判定。
    # 别把它调得比 listen_hard_limit_ms 小 —— 那会让 VAD 抢在 agent 前面
    # 把一句没说完的话切下来（说长指令被掐断就是这么来的）。
    max_utterance_ms: int = 15000
    # 一轮"听指令"最多听多久（毫秒）。到点不是丢掉重来，而是把已经录到的
    # 那一段交给识别 —— 说得长不该被惩罚。0 = 不限。
    listen_hard_limit_ms: int = 45000
    min_silence_ms: int = 700
    min_speech_ms: int = 250
    vad_threshold: float = 0.5
    # 输入检测的电平门槛：指声音量（RMS）高于它就算"有人在说话"。
    # 有些人说话轻、或者离麦克风远，光靠 VAD 会以为没人开口 ——
    # 于是"等你说完"的窗口提前过期，话说到一半助手就走了。
    voice_floor: float = 0.008
    # ── 子代理 ──
    # 把"要跑好几步"的事丢到后台单独做：主对话先回一句"我去查"，
    # 做完再播报结果。只有长任务才会用到它。
    subagent_enabled: bool = True
    subagent_max: int = 3           # 同时最多几个；0 = 不限制（内部仍有硬上限）
    subagent_rounds: int = 8        # 每个子代理最多调几次工具；0 = 不限制
    subagent_announce: bool = True  # 子代理做完要不要主动播报
    # 定时盯梢命中 / 出错 / 到点时要不要主动播报（和子代理分开：
    # 只想关掉其中一类的人不该被迫把另一类也关掉）
    watch_announce: bool = True
    exit_words: list[str] = field(default_factory=lambda: ["退下", "再见"])
    persona: str = ("你是运行在用户电脑上的语音助手，名字叫「大肥鲸」。"
                    "回答会被朗读，所以要短、要口语化，不要罗列 Markdown。")
    confirm: ConfirmCfg = field(default_factory=ConfirmCfg)


def _copy_dataclass(target: Any, source: Any) -> None:
    """把 source 的字段逐个写进 target（同类型、就地改，不换对象）。"""
    for f in dataclasses.fields(source):
        setattr(target, f.name, getattr(source, f.name))


@dataclass
class Config:
    """整份配置 + 解析好的模型路径。"""

    path: Path | None
    models_dir: Path
    models: dict[str, Path]
    missing_models: list[str]
    tts_rule_fsts: list[Path]
    tts_dict_dir: Path

    speech: SpeechCfg
    speaker: SpeakerCfg
    audio: AudioCfg
    wake: WakeCfg
    asr: AsrCfg
    tts: TtsCfg
    llm: LlmCfg
    agent: AgentCfg
    security: SecurityCfg
    paths: PathsCfg
    ui: UiCfg
    raw: dict

    # -- 热更新 -----------------------------------------------------------
    def update_from(self, other: Config) -> list[str]:
        """把另一份配置的值**就地**搬进来，返回被改动的顶层名字。

        为什么不是「重新 load 一份、把 self.cfg 换掉」：agent 和它内部的
        wake / asr / tts 都握着一开始那份 cfg 的引用，换了对象，它们手里的
        还是旧的 —— 于是除了重启引擎，改什么都没反应。就地改则立刻生效，
        剩下真正需要重启的只有「构造模型时读过」的那几项（见 Console.RESTART_KEYS）。
        """
        changed: list[str] = []
        for f in dataclasses.fields(other):
            new = getattr(other, f.name)
            old = getattr(self, f.name, None)
            if (dataclasses.is_dataclass(new) and dataclasses.is_dataclass(old)
                    and type(new) is type(old)):
                if new != old:
                    _copy_dataclass(old, new)
                    changed.append(f.name)
            elif new != old:
                setattr(self, f.name, new)
                changed.append(f.name)
        return changed

    # -- 模型可用性 -------------------------------------------------------
    @property
    def auto_models_dir(self) -> Path:
        """自动下载会装到哪：<数据目录>/models（和日志、下载缓存同一个父目录）。"""
        from . import paths  # noqa: PLC0415

        return paths.data_dir() / "models"

    def models_help(self) -> str:
        """缺模型时给用户看的那几句（打包版和源码运行都走得通）。"""
        base = ("自动下载：python -m voice_agent models --download"
                "（桌面版打开时会直接问你，也可以点「关于 → 模型」）\n"
                "  下载位置：" + str(self.auto_models_dir) + "（和日志、下载缓存同一个目录）\n"
                "  已经有模型：python -m voice_agent models --dir D:\\path\\to\\models")
        if getattr(sys, "frozen", False):
            base += ("\n  （打包版就在安装目录里：把 models 文件夹整体放进 "
                     + str(PROJECT_ROOT) + " 也行）")
        return base

    def has(self, *names: str) -> bool:
        return all(name in self.models for name in names)

    def require(self, *names: str) -> dict[str, Path]:
        """取模型路径；缺哪个就报哪个，并提示怎么补。"""
        missing = [n for n in names if n not in self.models]
        if missing:
            detail = "\n".join("  - " + n + ": " + str(self.models_dir / MODEL_FILES[n]) for n in missing)
            raise ConfigError(
                "缺少模型文件（" + str(self.models_dir) + "）：\n" + detail + "\n"
                + self.models_help()
            )
        return {n: self.models[n] for n in names}

    # -- 加载 -------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        cfg_path: Path | None = None
        if path is not None:
            cfg_path = Path(path)
        elif DEFAULT_CONFIG_PATH.is_file():
            cfg_path = DEFAULT_CONFIG_PATH

        raw: dict = {}
        if cfg_path is not None:
            if not cfg_path.is_file():
                raise ConfigError("配置文件不存在：" + str(cfg_path))
            raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError("配置文件顶层必须是映射：" + str(cfg_path))
            raw = normalize_keys(raw)

        # paths.models_dir 是新的写法（和 paths.data_dir 一伙），models_dir 是老的
        models_dir = _detect_models_dir(
            _get(raw, "models_dir", None) or _get(raw, "paths.models_dir", None))
        models, missing = _resolve_models(models_dir)
        # 用 dataclass 自己的默认值兜底，别在别处再抄一份列表 —— 抄漏了就会出现
        # 「默认值改了但配置没写时仍然是旧值」这种极难发现的问题。
        confirm_defaults = ConfirmCfg()
        agent_defaults = AgentCfg()
        profile = str(_get(raw, "speech.profile", "balanced")).strip().lower()
        if profile not in SPEECH_PROFILES:
            profile = "balanced"
        preset = SPEECH_PROFILES[profile]
        # 显式写了 threads / tts.engine 就以用户为准，没写才用档位预设
        threads_raw = _get(raw, "speech.threads", None)
        speech = SpeechCfg(
            device=str(_get(raw, "speech.device", "auto")),
            threads=int(threads_raw) if threads_raw else int(preset["threads"]),
            profile=profile,
            profile_note=str(preset["note"]),
        )
        speech.resolve()
        engine_raw = _get(raw, "tts.engine", None)
        tts_engine = (str(engine_raw).strip().lower() if engine_raw
                      else str(preset["tts_engine"]))
        speed_raw = _get(raw, "tts.speed", None)
        # quality 档位（ChatTTS）语流偏慢，预设里会提一点速；用户显式写了就以用户为准
        tts_speed = float(speed_raw) if speed_raw else float(preset.get("tts_speed", 1.0))

        return cls(
            path=cfg_path,
            models_dir=models_dir,
            models=models,
            missing_models=missing,
            tts_rule_fsts=[p for p in (models_dir / f for f in TTS_RULE_FSTS) if p.is_file()],
            tts_dict_dir=models_dir / TTS_DIR / "dict",

            speech=speech,
            speaker=SpeakerCfg(
                enabled=bool(_get(raw, "speaker.enabled", False)),
                model=str(_get(raw, "speaker.model", "") or ""),
                profile=str(_get(raw, "speaker.profile", "") or ""),
                threshold=float(_get(raw, "speaker.threshold", 0.55)),
            ),
            audio=AudioCfg(
                input_device=_get(raw, "audio.input_device", None),
                output_device=_get(raw, "audio.output_device", None),
                sample_rate=int(_get(raw, "audio.sample_rate", 16000)),
                block_ms=int(_get(raw, "audio.block_ms", 32)),
                input_gain=float(_get(raw, "audio.input_gain", 1.0)),
                output_gain=float(_get(raw, "audio.output_gain", 1.0)),
            ),
            wake=WakeCfg(
                enabled=bool(_get(raw, "wake.enabled", True)),
                keywords=_str_list(_get(raw, "wake.keywords", None), ["大肥鲸"]),
                threshold=float(_get(raw, "wake.threshold", 0.25)),
                score=float(_get(raw, "wake.score", 1.5)),
                cooldown_ms=int(_get(raw, "wake.cooldown_ms", 1500)),
                replies=_str_list(_get(raw, "wake.replies", None), ["我在"]),
                reply_random=bool(_get(raw, "wake.reply_random", True)),
                num_trailing_blanks=int(_get(raw, "wake.num_trailing_blanks", 1)),
            ),
            asr=AsrCfg(
                punctuation=bool(_get(raw, "asr.punctuation", True)),
                num_threads=int(_get(raw, "asr.num_threads", 4)),
                provider=str(_get(raw, "asr.provider", "") or ""),
                corrections=[
                    [str(pair[0]), str(pair[1])]
                    for pair in (_get(raw, "asr.corrections", None) or [])
                    if isinstance(pair, (list, tuple)) and len(pair) >= 2
                ],
            ),
            tts=TtsCfg(
                enabled=bool(_get(raw, "tts.enabled", True)),
                engine=tts_engine or "vits",
                num_threads=int(_get(raw, "tts.num_threads", 2)),
                provider=str(_get(raw, "tts.provider", "") or ""),
                device=str(_get(raw, "tts.device", "auto") or "auto"),
                compile=bool(_get(raw, "tts.compile", False)),
                voice=str(_get(raw, "tts.voice", "") or ""),
                speaker_id=int(_get(raw, "tts.speaker_id", 0)),
                speed=tts_speed,
                volume=float(_get(raw, "tts.volume", 1.0)),
                target_rms=float(_get(raw, "tts.target_rms", 0.10)),
                target_peak=float(_get(raw, "tts.target_peak", 0.9)),
                noise_gate=float(_get(raw, "tts.noise_gate", -55.0)),
                translit=bool(_get(raw, "tts.translit", True)),
                max_gain=float(_get(raw, "tts.max_gain", 3.0)),
                styles=dict(_get(raw, "tts.styles", None) or {}),
            ),
            llm=LlmCfg(
                enabled=bool(_get(raw, "llm.enabled", True)),
                base_url=str(_get(raw, "llm.base_url", "https://api.deepseek.com/v1")),
                model=str(_get(raw, "llm.model", "deepseek-chat")),
                api_key=str(_get(raw, "llm.api_key", "") or ""),
                temperature=float(_get(raw, "llm.temperature", 0.3)),
                max_rounds=int(_get(raw, "llm.max_rounds", 12)),
                timeout_s=float(_get(raw, "llm.timeout_s", 30)),
                reasoning_effort=str(_get(raw, "llm.reasoning_effort", "low")),
                vision_max_side=int(_get(raw, "llm.vision_max_side", 1280)),
                extra_body=dict(_get(raw, "llm.extra_body", None) or {}),
                routes=dict(_get(raw, "llm.routes", None) or {}),
                profiles={
                    str(key): dict(value)
                    for key, value in (_get(raw, "llm.profiles", None) or {}).items()
                    if isinstance(value, dict)
                },
            ),
            agent=AgentCfg(
                barge_in_wake=bool(_get(raw, "agent.barge_in_wake", True)),
                listen_timeout_ms=int(_get(raw, "agent.listen_timeout_ms", 8000)),
                follow_up_mode=str(_get(raw, "agent.follow_up_mode", "auto") or "auto").strip().lower(),
                follow_up_ms=int(_get(raw, "agent.follow_up_ms", 6000)),
                cues=bool(_get(raw, "agent.cues", True)),
                max_utterance_ms=int(_get(raw, "agent.max_utterance_ms", 15000)),
                listen_hard_limit_ms=int(_get(raw, "agent.listen_hard_limit_ms", 45000)),
                min_silence_ms=int(_get(raw, "agent.min_silence_ms", 700)),
                min_speech_ms=int(_get(raw, "agent.min_speech_ms", 250)),
                vad_threshold=float(_get(raw, "agent.vad_threshold", 0.5)),
                voice_floor=float(_get(raw, "agent.voice_floor", 0.008)),
                subagent_enabled=bool(_get(raw, "agent.subagent_enabled", True)),
                subagent_max=int(_get(raw, "agent.subagent_max", 3)),
                subagent_rounds=int(_get(raw, "agent.subagent_rounds", 8)),
                subagent_announce=bool(_get(raw, "agent.subagent_announce", True)),
                watch_announce=bool(_get(raw, "agent.watch_announce", True)),
                exit_words=_str_list(_get(raw, "agent.exit_words", None), agent_defaults.exit_words),
                persona=str(_get(raw, "agent.persona", AgentCfg.persona)),
                confirm=ConfirmCfg(
                    enabled=bool(_get(raw, "agent.confirm.enabled", True)),
                    timeout_ms=int(_get(raw, "agent.confirm.timeout_ms", 10000)),
                    yes=_str_list(_get(raw, "agent.confirm.yes", None), confirm_defaults.yes),
                    no=_str_list(_get(raw, "agent.confirm.no", None), confirm_defaults.no),
                    prompt=str(_get(raw, "agent.confirm.prompt", confirm_defaults.prompt)),
                ),
            ),
            paths=PathsCfg(
                data_dir=str(_get(raw, "paths.data_dir", "") or ""),
            ),
            security=SecurityCfg(
                mode=str(_get(raw, "security.mode", "workspace-write") or "workspace-write"),
                allow_insecure=bool(_get(raw, "security.allow_insecure", False)),
                max_prompts_per_minute=int(_get(raw, "security.max_prompts_per_minute", 6)),
                max_same_action=int(_get(raw, "security.max_same_action", 3)),
                deny_tools=_str_list(_get(raw, "security.deny_tools", None), []),
                always_confirm=_str_list(_get(raw, "security.always_confirm", None), []),
                floor_tools=_str_list(_get(raw, "security.floor_tools", None),
                                      SecurityCfg().floor_tools),
                keep_floor_when_empty=bool(
                    _get(raw, "security.keep_floor_when_empty", True)),
                audit=bool(_get(raw, "security.audit", True)),
            ),
            ui=UiCfg(
                accent=str(_get(raw, "ui.accent", "") or ""),
                show_stats=bool(_get(raw, "ui.show_stats", True)),
                show_turn=bool(_get(raw, "ui.show_turn", True)),
            ),
            raw=raw,
        )


def _detect_models_dir(explicit: Any = None) -> Path:
    """找出该用哪个模型目录（见 models_dir_candidates 的顺序）。"""
    candidates = models_dir_candidates(explicit)
    for candidate in candidates:
        if (candidate / "silero_vad.onnx").is_file() or (candidate / ASR_DIR).is_dir():
            return Path(candidate).expanduser().resolve()
    # 一个都没有：把"自动下载会装到哪"告诉调用方（第一个候选就是它）
    return Path(candidates[0]).expanduser().resolve()


def _resolve_models(root: Path) -> tuple[dict[str, Path], list[str]]:
    found: dict[str, Path] = {}
    missing: list[str] = []
    for name, rel in MODEL_FILES.items():
        path = root / rel
        if path.is_file():
            found[name] = path
        elif name not in OPTIONAL_MODELS:
            # 可选模型（标点模型）有回退方案，缺了不算错误
            missing.append(name)
    return found, missing
