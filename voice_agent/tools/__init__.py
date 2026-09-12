# -*- coding: utf-8 -*-
"""工具层：一张「助手会做什么」的清单。

这个包分成两半：
- **__init__.py 是清单** —— Tool 定义、全局注册表、调用与确认闸门、给 LLM 的 schema；
- **各个子模块是厨房** —— 真正干活的东西按主题分开：
  windows(系统) / files(文件与记忆) / vision(鼠标屏幕) / apps(应用网页) / system_info(状态)

这么分的好处：想加一个工具，只在清单里加一条注册；想改某个能力的行为，
去对应的子模块，不用在两千行里翻。

三条贯穿所有工具的约定：
- 返回值永远是**一句给人听的中文**（它会直接进 TTS）；
- **永不抛异常**，失败也返回一句话，但会带上 ok=False 让大脑知道这步没成；
- **确认失败即拒绝** —— 敏感工具拿不到确认通道就是拒绝，漏传参数不是放行。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any, Callable

from .. import security
from ._shared import TURN, keep_listening, last_utterance, reset_turn, set_utterance
from .apps import open_app, open_url, web_search
from .files import list_files, read_file, recall, remember, search_files
from .images import find_in_image_tool, list_reference_tool, reference_dir_tool
from .marks import (
    clear_marks_tool,
    list_marks_tool,
    mark_point_tool,
    mark_region_tool,
    remove_mark_tool,
    set_marks_handler,
    show_marks_tool,
)
from .selfctl import (
    new_session_tool,
    permission_mode_tool,
    quit_self_tool,
    restart_self_tool,
    set_self_handler,
)
from .subagents import (
    cancel_subagent_tool,
    set_subagent_handler,
    spawn_subagent_tool,
    subagent_status_tool,
)
from .system_info import get_time, system_info
# 这些名字同时也是一层公开 API：skills.py 的 sequence 动作会写 tools.open_app(...)，
# 所以即使本模块自己不直接调用，也要保持可导入。
from .vision import (
    app_map_tool,
    click_image_tool,
    find_on_screen_tool,
    look_at_screen_tool,
    mouse_click_tool,
    mouse_drag_tool,
    mouse_move_tool,
    mouse_position_tool,
    mouse_scroll_tool,
    resize_image_tool,
    set_vision_handler,
    set_vision_max_side,
)
from .watches import (
    list_watches_tool,
    set_watch_handler,
    start_watch_tool,
    stop_watch_tool,
)
from .windows import (
    clipboard,
    lock_screen,
    media_control,
    power,
    press_keys,
    run_command,
    screenshot,
    type_text,
    volume,
    window,
)

# 子模块里用到的公共常量（子模块回头 import 这里，形成受控的循环：
# 本文件先定义完常量，最后才 import 子模块的注册块）


@dataclass(frozen=True)


class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]
    confirm: bool = False
    #: 只有"真的动手"才敏感的补充判断（参数 → 要不要问）。
    #: permission_mode 就是这一类：**查**权限模式不该弹确认，**改**才该。
    confirm_if: Callable[[dict], bool] | None = None
    title: str = ""
    source: str = "builtin"
    # 离线规则模式用的触发词：((说法, {预设参数}), ...)
    # LLM 模式不依赖它（模型自己看 description 判断），但没有 API Key 时
    # 它是自定义技能唯一能被「说出来」的入口。
    triggers: tuple = ()
    #: 工具结果进入模型上下文时的字符预算（0 = 用大脑的默认值 800）。
    #: 读文件这类"整篇内容就是结果"的工具要放宽，否则模型永远只看到开头 ——
    #: 实测里它于是反复加大 max_chars，最后绕去看屏幕。
    result_budget: int = 0
    #: 操作类别（例如「鼠标」）：**同一条指令里，同一类操作只问一次**。
    #: 语音场景下"拖一下、再点一下、再拖一下"是一条指令的连续动作，
    #: 每次都弹确认等于没法用；但跨指令必须重新问（一轮结束就清空）。
    group: str = ""
    #: 用途标签：**同一件事有好几种做法时**，模型靠它一眼挑对的那个。
    #: 会写在给模型的说明最前面（【找图/本地/不花钱】…），词表见 _TAGS_DOC。
    tags: tuple = ()

    @property
    def follow_up(self) -> bool:
        """这个工具的结果是不是"通常需要用户接着说一句"。

        连续对话的成败就在这一步：查完东西说"在 320,480"然后回待命，用户还得再喊
        一次唤醒词说"点它" —— 像命令行，不像人。这些"查了给你看"的工具答完留个
        窗口，"点它""打开第三个"就能直接接上。
        """
        return self.name in _FOLLOW_UP_TOOLS

    @property
    def display(self) -> str:
        """给人看的短名字：优先 title，其次取描述的第一句。"""
        if self.title:
            return self.title
        head = re.split(r"[。，,.;；]", self.description.strip())[0]
        return head[:16] or self.name

    def wants_confirm(self, args: dict | None = None) -> bool:
        """这一次调用要不要先问一句。

        confirm 是"凡是调用都要问"；有些工具只有真正动手的那一步才敏感
        （查权限模式 vs 改权限模式），用 confirm_if 按参数再判一次。
        判不出来（回调抛异常）算敏感 —— 宁可多问一句。
        """
        if self.confirm:
            return True
        hook = self.confirm_if
        if hook is None:
            return False
        try:
            return bool(hook(dict(args or {})))
        except Exception:  # noqa: BLE001
            return True

    def confirm_question(self, args: dict) -> str:
        """敏感操作前要念给用户听的那句话。

        必须说人话：直接拿 description 拼会念出「关机、重启、睡眠或注销电脑。
        属于敏感操作，调用前应先跟用户确认。：shutdown」，听起来像在念文档。

        **命令和长文本不念原文**：命令是给机器看的（一串路径、参数、引号），
        念出来用户听不懂、还要等好几秒。这一类只报"要干什么"
        （能认出常见动作就说出来），原文留在日志里给需要的人查。
        """
        detail = self._spoken_detail(args or {})
        question = "要" + self.display
        if detail:
            question += "：" + detail
        return question + "，确认吗？"

    def _spoken_detail(self, args: dict) -> str:
        """把参数压成"**念得清楚**的一小句"。

        这里的目标不是"完整"，而是"用户听得懂在问什么"：念不清的东西宁可不念
        （原文在日志和审计里都有）。三类参数分开处理：

        - **命令/脚本/代码**：念出来是一串路径和引号 —— 只说"要干什么"；
        - **正文/文本**：太长就没必要念，说"一段内容"即可；
        - **路径/文件名**：只说"哪个盘、哪个中文名的文件"，
          扩展名（.png）、英文名、目录层级一律不念 —— 它们念出来就是噪音；
        - 其余：短的原样念，含英文且没有中文叫法的直接省掉。
        """
        words: list[str] = []
        for key, value in args.items():
            name = str(key).strip().lower()
            text = " ".join(str(value).split())
            if not text:
                continue
            if name in _QUIET_ARGS:
                # 坐标、毫秒、相似度阈值这类数字念出来只是噪音：
                # 用户要判断的是"它要干什么"，不是"拖了多少像素"
                continue
            if name in ("command", "script", "code"):
                hint = _command_hint(text)
                if hint:
                    words.append(hint)
                continue
            if name in ("content", "text", "body"):
                if len(text) <= 16 and not re.search(r"[A-Za-z]", text):
                    words.append(text)
                else:
                    words.append("一段内容")
                continue
            if name in ("path", "file", "target", "out", "source", "image", "template"):
                spoken = _spoken_path(text)
                if spoken:
                    words.append(spoken)
                continue
            label = _ARG_LABELS.get(name)
            if label and re.fullmatch(r"-?\d+(?:\.\d+)?", text):
                words.append(label[0] + text + label[1])
                continue
            mapped = _ARG_WORDS.get(text.lower())
            if mapped:
                words.append(mapped)
                continue
            spoken = _spoken_name(text)
            if spoken:
                words.append(spoken)
        return "、".join(words[:3])

    def schema(self) -> dict:
        # 标签写在说明最前面：模型扫一眼就知道"这是干什么的、要不要花钱"。
        # 找一张图这件事有三个工具能干（本地找 / 截图问视觉模型 / 连点），
        # 没有这行标签时它经常挑最贵、最慢的那条路。
        description = ("【" + "·".join(self.tags) + "】" + self.description) if self.tags \
            else self.description
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }


def _params(**props: dict) -> dict:
    """把 {参数名: schema} 组装成 JSON Schema。

    一定要先拷贝再 pop：_S_REQ 这类简写是模块级共享对象，原地 pop 之后
    第二个用到它的工具就拿不到 required 了 —— 实测 open_url、web_search、
    search_files、read_file、type_text、press_keys、remember、run_command
    这 8 个工具的必填项会集体消失，模型于是发空参数，用户只听到「没说要打开哪个网址」。
    """
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, spec in props.items():
        item = dict(spec)
        if item.pop("_required", False):
            required.append(name)
        properties[name] = item
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


@dataclass(frozen=True)


class ToolResult:
    """工具的返回值：给人听的一句话 + 给程序看的成败标记。"""

    text: str
    ok: bool = True
    code: str = "ok"

    def __str__(self) -> str:  # 兼容把它当字符串用的老代码
        return self.text


def register(tool: Tool, replace: bool = False) -> None:
    """把一个工具注册进全局工具表；技能加载器也走这里。

    replace=True 时覆盖同名工具 —— 技能因此可以「改写」内置工具的行为。
    """
    if not replace and tool.name in REGISTRY:
        raise ValueError("工具名重复：" + tool.name)
    # 标题和用途标签都在这里统一补：加工具的人只要写 name/description/parameters，
    # 界面上的短名字和给模型看的标签自动就有（少一处漏写的机会）。
    changes: dict = {}
    if not tool.title:
        changes["title"] = _BUILTIN_TITLES.get(tool.name, "")
    if not tool.tags:
        tags = _BUILTIN_TAGS.get(tool.name, ())
        if tags:
            changes["tags"] = tags
    if not tool.group:
        group = _BUILTIN_GROUPS.get(tool.name, "")
        if group:
            changes["group"] = group
    if not tool.result_budget:
        budget = _BUILTIN_BUDGETS.get(tool.name, 0)
        if budget:
            changes["result_budget"] = budget
    if changes:
        tool = _replace(tool, **changes)
    REGISTRY[tool.name] = tool


def unregister(name: str) -> bool:
    return REGISTRY.pop(name, None) is not None


def tool_names() -> list[str]:
    return list(REGISTRY)


def load_skills(dirs: Any = None) -> list:
    """加载技能目录；返回每个技能文件的加载结果（含错误信息）。"""
    from ..skills import SkillLoader

    loader = SkillLoader(dirs) if dirs is not None else SkillLoader()
    return loader.load_all()


def autoload_skills(force: bool = False) -> list:
    """第一次真正用到工具表时加载技能目录，之后走缓存。

    放在这里而不是 import 期，是为了避免「导入 tools 就去读磁盘」这种副作用，
    也让测试可以用 VOICE_AGENT_NO_SKILLS=1 关掉它。
    """
    global _SKILLS_LOADED, SKILL_INFOS
    if _SKILLS_LOADED and not force:
        return SKILL_INFOS
    _SKILLS_LOADED = True
    if os.environ.get("VOICE_AGENT_NO_SKILLS"):
        return SKILL_INFOS
    reset_skills()   # 重新加载前先清空，删掉的技能才不会阴魂不散
    try:
        SKILL_INFOS = load_skills()
    except Exception as exc:  # noqa: BLE001 - 技能目录坏了也不能让助手起不来
        print("[skills] 技能目录加载失败：" + str(exc), file=sys.stderr, flush=True)
        SKILL_INFOS = []
        return SKILL_INFOS
    for info in SKILL_INFOS:
        if not info.ok:
            print("[skills] " + info.source.name + " 加载失败：" + info.error, file=sys.stderr, flush=True)
    return SKILL_INFOS


def _register(tool: Tool) -> None:
    """内置工具的注册入口（允许覆盖，模块被重新导入时不会炸）。"""
    register(tool, replace=True)
    _BUILTIN_SNAPSHOT[tool.name] = REGISTRY[tool.name]


def reset_skills() -> None:
    """卸掉所有由技能注册的工具，并把被技能覆盖的内置工具还原。

    少了这一步，删掉技能文件后它的工具还留在表里、还能被调用 ——
    界面上显示「已删除」，实际上能力没消失，直到重启才生效。
    """
    for name, tool in list(REGISTRY.items()):
        if tool.source != "builtin":
            REGISTRY.pop(name, None)
    for name, tool in _BUILTIN_SNAPSHOT.items():
        REGISTRY[name] = tool


def openai_tools() -> list[dict]:
    """给 LLM 的工具声明列表（含技能提供的工具）。

    先 list() 拍一张快照：技能热重载（界面里保存/删除技能）会在**另一个线程**
    里增删 REGISTRY，直接迭代 values() 会撞上
    "dictionary changed size during iteration"，整轮对话以"处理的时候出错了"结束。
    """
    autoload_skills()
    return [tool.schema() for tool in list(REGISTRY.values())]


def describe() -> str:
    """供 CLI / 网页 UI 展示的工具清单（列表形式）。"""
    autoload_skills()
    snapshot = list(REGISTRY.values())
    width = max((len(t.name) for t in snapshot), default=12)
    lines = []
    for tool in snapshot:
        flag = "  [需确认]" if tool.confirm else (
            "  [动手前确认]" if tool.confirm_if else "")
        origin = "" if tool.source == "builtin" else "  ← " + Path(tool.source).name
        lines.append(
            "  " + tool.name.ljust(width) + "  " + tool.display + "："
            + tool.description.split("。")[0] + flag + origin
        )
    return "\n".join(lines)


def _tail_name(path: str) -> str:
    """长路径只念最后一段（文件名），前面那些目录念了也没人记得住。"""
    parts = re.split(r"[\\/]+", str(path).strip())
    return parts[-1] if parts and parts[-1] else str(path)


#: 常见程序的念法：英文名直接念出来 TTS 是念不清的（不是漏字就是逐字母拼）
_NAME_ZH = {
    "notepad": "记事本", "calc": "计算器", "mspaint": "画图", "explorer": "资源管理器",
    "chrome": "谷歌浏览器", "msedge": "浏览器", "edge": "浏览器", "firefox": "火狐",
    "cmd": "命令行", "powershell": "命令行", "wt": "终端", "taskmgr": "任务管理器",
    "wechat": "微信", "weixin": "微信", "qq": "QQ", "dingtalk": "钉钉",
    "cloudmusic": "网易云音乐", "code": "编辑器", "devenv": "开发工具",
}


def _spoken_path(path: str) -> str:
    """把路径压成"念得清楚"的一小段。

    一张截图存成带时间戳的英文名（盘符 + 反斜杠 + 一串字母数字 + 扩展名），
    原样念出来用户根本听不出在问什么。所以只保留**盘符**和**纯中文的文件名主干**，
    其余（目录、扩展名、英文名、数字串）一概不念。
    """
    raw = str(path or "").strip().replace("/", "\\")
    drive = re.match(r"^([A-Za-z]):", raw)
    parts = [part for part in raw.split("\\") if part]
    name = parts[-1] if parts else raw
    stem = name.rsplit(".", 1)[0] if "." in name else name
    where = (drive.group(1).upper() + "盘") if drive else ""
    spoken = ("「" + stem + "」") if re.fullmatch(r"[\u4e00-\u9fff0-9]{1,10}", stem or "") else ""
    if where and spoken:
        return where + "的" + spoken
    return where or spoken or "一个文件"


def _spoken_name(text: str) -> str:
    """非路径的参数：认识的中文叫法就说，含英文又没叫法的直接省掉。"""
    value = str(text or "").strip()
    if not value or len(value) > 24:
        return ""
    low = value.lower()
    if low.endswith(".exe"):
        low = low[:-4]
    if low in _NAME_ZH:
        return _NAME_ZH[low]
    if re.search(r"[A-Za-z]", value):
        return ""            # 英文名念不清，不如不念
    return value
    
    
def _command_hint(command: str) -> str:
    """一条命令大致在干什么；认不出来就返回空串（确认提示就只说"执行命令"）。

    多个动作都能对上时取**最危险**的那个（表是按危险程度排的），
    并且在末尾标出"组合命令" —— 管道、分号、重定向意味着它不止干一件事。
    """
    raw = str(command or "")
    low = " " + raw.lower() + " "
    label = ""
    for keys, name in _COMMAND_HINTS:
        if any(key in low for key in keys):
            label = name
            break
    if any(token in raw for token in ("|", ";", "&&", "> ", ">>")):
        return (label + "等组合命令") if label else "一条组合命令"
    return label


#: 工具"缺参数"时的开场白。项目里的写法很统一（全是"没说…"），
#: 于是这里能一眼认出来：接下去用户那句话就是来补参数的。
_NEED_INPUT = re.compile(r"^(没说|没听清|没找到叫|看不懂这个|要同时给出)")


def tool_fingerprint(name: str, args: Any) -> str:
    """这次调用的**身份**：工具名 + 参数（按键排序）。

    确认提示是给人听的，会被"说人话"处理（命令只说"列出文件"）；
    但"哪个操作"必须按下层参数算 —— 否则两条不同的命令会共用一句提示，
    上一句的同意就被当成这一句的同意（这是个真的安全漏洞）。
    """
    try:
        payload = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False,
                             sort_keys=True)
    except (TypeError, ValueError):
        payload = str(args)
    return name + "|" + payload[:400]


def _ask_human(on_confirm: Callable, question: str, fingerprint: str,
               group: str = "") -> bool:
    """问用户。确认通道愿意接收就带上**指纹**和**操作类别**。

    引擎用它们做两件事：同一个操作不重复问；同一条指令里同一类操作只问一次
    （见 _BUILTIN_GROUPS）。按签名传参：只收一个参数的老通道照样能用。
    """
    try:
        parameters = inspect.signature(on_confirm).parameters
    except (TypeError, ValueError):
        parameters = {}
    # 只数**位置参数**：把 KEYWORD_ONLY 也算进去的话，写成
    # def f(question, *, fingerprint="") 的通道会被按位置调用，抛 TypeError ——
    # 而这里在 call_result 的 try 之外，异常会变成整轮"处理出错了"。
    positional = [p for p in parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    try:
        if len(positional) >= 3:
            return bool(on_confirm(question, fingerprint, group))
        if len(positional) >= 2:
            return bool(on_confirm(question, fingerprint))
        return bool(on_confirm(question))
    except TypeError:
        # 签名看不准（C 函数、functools.partial…）：退回最保守的一次一参调用。
        # **拿不到确认就是拒绝**，不能因为签名怪就放行。
        try:
            return bool(on_confirm(question))
        except Exception:  # noqa: BLE001
            return False


def _needs_input(text: str) -> bool:
    value = str(text or "").strip()
    return len(value) <= 60 and bool(_NEED_INPUT.match(value))


#: 当前工具调用的「本轮已作废？」查询（**线程私有**：工具都跑在自己的工作
#: 线程里，用全局变量会串台）。
_cancel_ctx = threading.local()


def set_cancel_check(check: Callable[[], bool] | None) -> None:
    """给当前线程设一个"这一轮是不是已经作废了"的查询函数。"""
    _cancel_ctx.check = check


def cancelled() -> bool:
    """当前这次工具调用是不是已经作废（用户打断 / 来了新指令）。

    长工具（执行命令、整盘搜索）在自己的循环里查它，及时收手 —— 以前打断
    只能拦住"还没开始执行的下一步"，已经跑起来的那条命令会一直跑完
    （最长 120 秒）：用户听到的是"已打断"，机器却还在转，日志里随后还冒出
    这次调用的结果。
    """
    check = getattr(_cancel_ctx, "check", None)
    if check is None:
        return False
    try:
        return bool(check())
    except Exception:  # noqa: BLE001 - 判不出来就当没打断
        return False


def _audit_args(args: Any, limit: int = 160) -> str:
    """审计里记参数，但要截断：别把一整篇文件内容写进日志。"""
    try:
        text = json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return text[:limit]


def call(name: str, arguments: Any = None, on_confirm: Callable[[str], bool] | None = None) -> str:
    """执行一个工具，只要那句话（大多数调用方只关心这个）。"""
    return call_result(name, arguments, on_confirm).text


def call_result(name: str, arguments: Any = None,
                on_confirm: Callable[[str], bool] | None = None,
                cancel_check: Callable[[], bool] | None = None) -> ToolResult:
    """执行一个工具，返回（文本, 是否成功, 错误码）。

    两条重要约定：

    1. **确认失败即拒绝**。以前是「有 on_confirm 才问」，于是任何忘了传确认通道的
       调用方都能把关机、执行命令这类工具直接跑掉。现在反过来：敏感工具拿不到
       确认通道就是拒绝 —— 漏传是拒绝，不是放行。
    2. 失败也返回一句中文，但带上 ok=False，让大脑知道「这一步没成」，
       从而在开口前加一句提醒，而不是把错误当成结果自信地念出来。

    cancel_check 是"这一轮还算不算数"的查询（打断 / 新指令），长工具靠它
    在半路收手；不传就是"不许打断"（旧行为）。
    """
    autoload_skills()
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult("没有这个工具：" + str(name), False, "unknown_tool")
    args = arguments
    if args is None:
        # 没给参数 = 这个工具不需要参数（测试和内部调用都这么用）
        args = {}
    elif isinstance(args, str):
        if not args.strip():
            args = {}
        else:
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                # **绝不能**把解不开的参数当成 {} 去调 handler：模型的参数被
                # 截断（max_tokens、网关只回半截 JSON）时，"没有参数"会被
                # 工具自己的默认值接住 —— 而 power 的默认动作就是关机。
                # 说清楚、让大脑重发一次，比猜一个默认值安全得多。
                return ToolResult(
                    "工具参数不是合法的 JSON（可能被截断了），这一步没执行，请重新给出完整参数",
                    False, "bad_arguments")
    if not isinstance(args, dict):
        return ToolResult("工具参数应该是一个对象，这一步没执行", False, "bad_arguments")

    # ── 权限闸门 ──
    # 判定放在这里，而不是让每个工具自己判断：模型（以及它背后的中转站）
    # 说不上话的地方只有这一处。结果词汇沿用 DSH 的审批语义：
    # allowed-once / rejected / unavailable。
    decision = security.check(tool, args)
    if not decision.allowed and not decision.needs_confirm:
        # check() 里已经记过的（deny_tools / 只读拒绝）不再记一遍 ——
        # 同一次拒绝在 audit.jsonl 里出现两行，事后查日志会看糊涂。
        if not decision.audited:
            security.audit({"event": "blocked", "tool": tool.name, "code": decision.code,
                            "tier": decision.tier, "mode": security.mode(),
                            "tainted": security.tainted(), "args": _audit_args(args)})
        return ToolResult(decision.text, False, "denied")

    # 要不要问，**完全听 check() 的**（它知道当前模式、底线名单、额外名单）。
    # 以前这里还挂着一个 `or tool.confirm`，于是"放开模式下一律不问"永远不生效：
    # 声明了 confirm 的工具（拖鼠标、开始盯梢…）在最宽的模式下照样每次都问。
    if decision.needs_confirm:
        if on_confirm is None:
            # 拿不到确认通道 = 拒绝（和 DSH 的 unavailable 一样，fail closed）
            security.audit({"event": "unavailable", "tool": tool.name,
                            "tier": decision.tier, "mode": security.mode()})
            return ToolResult(CANCEL_REPLY, False, "denied")
        # 限流：一分钟最多问几次、同一个操作最多连着问几次。
        # 中转站最爱的就是"反复构造危险调用，把用户问到麻木"。
        # 计数按**指纹**（工具+参数），不是按工具名 —— 否则"列出文件"和
        # "删掉文件"会被当成同一个操作。
        fingerprint = tool_fingerprint(tool.name, args)
        ok, why = security.note_prompt(tool.name, fingerprint)
        if not ok:
            security.audit({"event": "rate_limited", "tool": tool.name,
                            "fingerprint": fingerprint[:80]})
            return ToolResult(security.DELIMITER.format(why) + " " + security.ESCALATION_HINT,
                              False, "denied")
        question = tool.confirm_question(args)
        if security.tainted():
            # 这一轮碰过网页/文件/屏幕——那些内容里可能藏着"去执行 xxx"的指令
            question = "注意，这是看过外部内容之后发起的操作。" + question
        if not _ask_human(on_confirm, question, fingerprint, tool.group):
            security.audit({"event": "rejected", "tool": tool.name, "tier": decision.tier,
                            "mode": security.mode(), "tainted": security.tainted(),
                            "args": _audit_args(args)})
            return ToolResult(CANCEL_REPLY, False, "denied")
        # 一次确认只够一次调用：这里没有"记住你同意过"的状态可以重放
        security.audit({"event": "allowed-once", "tool": tool.name, "tier": decision.tier,
                        "mode": security.mode(), "tainted": security.tainted(),
                        "args": _audit_args(args)})

    # 让工具在自己的长循环里能问"这一轮还算不算数"（打断/新指令 → 立刻收手）
    set_cancel_check(cancel_check)
    try:
        try:
            outcome = ToolResult(str(tool.handler(**args)))
        except TypeError as exc:
            outcome = ToolResult("工具参数不对：" + str(exc)[:80], False, "bad_arguments")
        except Exception as exc:  # noqa: BLE001 - 工具层永不抛出，交给模型兜底
            outcome = ToolResult(ERROR_PREFIX + str(exc)[:100], False, "error")
    finally:
        set_cancel_check(None)

    # 连续对话：这三类结果之后用户通常还要接一句，先把窗口留着 ——
    # 否则他得再喊一次唤醒词才能说"点它"，那就不像人说话了。
    if not TURN.get("follow_up"):
        if outcome.code == "bad_arguments" or _needs_input(outcome.text):
            keep_listening("刚才缺参数，等用户补一句")
        elif outcome.ok and tool.follow_up:
            keep_listening("刚查完，" + tool.display + "之后通常还有下一句")
    return outcome
__all__ = [
    "Tool",
    "REGISTRY",
    "call",
    "set_utterance",
    "last_utterance",
    "openai_tools",
    "describe",
    "ToolResult",
    "CANCEL_REPLY",
    "ERROR_PREFIX",
    "register",
    "unregister",
    "load_skills",
    "autoload_skills",
    "reset_skills",
    "tool_names",
]
#: 答完留个追问窗口的工具：它们的结果是"给用户看一眼"，
#: 接下来那句"点它""打开第三个""把范围1 截下来"才是真正要干的事。
_FOLLOW_UP_TOOLS = frozenset({
    "find_on_screen", "find_in_image", "look_at_screen", "list_files", "search_files",
    "find_files", "list_windows", "list_processes", "list_marks", "mouse_position",
    "app_map", "resize_image", "read_file", "recall", "list_watches", "subagent_status",
})

#: 这些参数念出来纯属噪音（坐标、毫秒、阈值、内部开关）
_QUIET_ARGS = frozenset({
    "x", "y", "x1", "y1", "x2", "y2", "duration_ms", "interval_ms", "confidence",
    "scales", "limit", "max_chars", "quality", "timeout_s", "once", "max_minutes",
    "out", "reason",
})
#: 数字参数的念法：（前缀，后缀）。"延迟 60 秒"比"60"清楚得多
_ARG_LABELS = {
    "delay": ("延迟 ", " 秒"), "delay_s": ("延迟 ", " 秒"), "seconds": ("", " 秒"),
    "timeout_s": ("超时 ", " 秒"), "interval_s": ("每 ", " 秒"), "max_minutes": ("最长 ", " 分钟"),
    "count": ("", " 次"), "times": ("", " 次"), "steps": ("", " 步"),
    "amount": ("", " 格"), "percent": ("", "%"), "limit": ("最多 ", " 个"),
    "rounds": ("最多 ", " 轮"), "max": ("最多 ", " 个"),
}
#: 从命令里认出常见动作，好让确认提示说得出"要干什么"
#: 顺序就是优先级：**危险的动作先说**。
#: 一条命令里同时有 Get-ChildItem 和 Remove-Item 时，"列出文件"是误导 ——
#: 说成"删除文件"用户才会认真看一眼。
_COMMAND_HINTS = (
    (("restart-computer",), "重启电脑"),
    (("shutdown",), "关机"),
    (("remove-item", " del ", "rm ", "erase ", "clear-content"), "删除文件"),
    (("stop-process", "taskkill", "kill "), "结束进程"),
    (("set-content", "out-file", "add-content", ">>", " > "), "写文件"),
    (("copy-item", "copy ", " cp ", "xcopy", "robocopy"), "复制文件"),
    (("move-item", "move ", " mv "), "移动文件"),
    (("new-item", "mkdir", " md "), "新建文件或目录"),
    (("invoke-webrequest", "invoke-restmethod", "curl", "wget"), "联网获取内容"),
    (("start-process", "start ", "saps "), "打开程序"),
    (("test-connection", "ping "), "测试网络"),
    (("get-process", "tasklist"), "查看进程"),
    (("get-service",), "查看服务"),
    (("get-childitem", "dir ", " gci ", "ls "), "列出文件"),
)
_PS_UTF8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;"
_ARG_WORDS = {
    "shutdown": "关机", "restart": "重启", "reboot": "重启", "sleep": "睡眠", "logoff": "注销",
    "up": "调大", "down": "调小", "mute": "静音", "max": "最大", "min": "最小",
    "play_pause": "播放或暂停", "next": "下一首", "prev": "上一首", "stop": "停止",
    "get": "读取", "set": "写入", "all": "全部",
    # 权限模式：这三个值全是英文，不映射就等于**不念** —— 于是提权那句确认
    # 变成光秃秃的「要调整权限，确认吗？」，用户根本不知道自己批准了什么
    # （在只读模式下尤其危险）。确认提示必须说清"切到哪一档"。
    "read-only": "只读模式", "只读": "只读模式",
    "workspace-write": "标准模式", "标准": "标准模式",
    "danger-full-access": "放开权限", "放开": "放开权限",
}
_S = {"type": "string"}
_S_REQ = {"type": "string", "_required": True}
_I = {"type": "integer"}
CANCEL_REPLY = "用户取消了这次操作"
ERROR_PREFIX = "执行失败："
REGISTRY: dict[str, Tool] = {}
_BUILTIN_TITLES = {
    "get_time": "查时间",
    "system_info": "查电脑状态",
    "open_app": "打开应用",
    "open_url": "打开网址",
    "web_search": "搜索",
    "volume": "调音量",
    "media_control": "控制播放",
    "screenshot": "截图",
    "clipboard": "剪贴板",
    "list_files": "看文件夹",
    "search_files": "找文件",
    "read_file": "读文件",
    "type_text": "打字",
    "press_keys": "按快捷键",
    "window": "窗口操作",
    "lock_screen": "锁屏",
    "power": "电源操作",
    "run_command": "执行命令",
    "remember": "记事",
    "recall": "回忆",
    "spawn_subagent": "派子代理",
    "subagent_status": "子代理进度",
    "cancel_subagent": "取消子代理",
    "new_session": "开新会话",
    "restart_self": "重启程序",
    "quit_self": "退出程序",
    "permission_mode": "调整权限",
    "find_in_image": "图里找图",
    "list_reference": "看参考图",
    "reference_dir": "参考图放哪",
    "mark_region": "框选范围",
    "mark_point": "标记点",
    "list_marks": "看标记",
    "remove_mark": "擦掉标记",
    "clear_marks": "清空标记",
    "show_marks": "显示或藏起标记",
    "start_watch": "盯着看",
    "list_watches": "看进度",
    "stop_watch": "别盯了",
    # 鼠标 / 屏幕这一批：确认提示是念 display 的，措辞要能听懂
    "mouse_position": "查鼠标位置",
    "mouse_move": "移动鼠标",
    "mouse_click": "点击鼠标",
    "mouse_drag": "拖拽鼠标",
    "mouse_scroll": "滚动鼠标",
    "find_on_screen": "在屏幕上找图",
    "click_image": "点屏幕上的图",
    "resize_image": "缩放图片",
    "look_at_screen": "看屏幕",
    "app_map": "改应用映射表",
    "open_path": "打开文件夹",
    "write_file": "写文件",
    "edit_file": "改文件",
    "find_files": "按通配符找文件",
    "grep_files": "在文件里搜内容",
    "list_windows": "看窗口列表",
    "focus_window": "切到某个窗口",
    "list_processes": "看进程列表",
    "kill_process": "结束进程",
    "wait": "等一会儿",
    "keep_listening": "继续听你说",
}
#: 用途标签：**同一件事有好几种做法时**，模型靠它挑对的那个。
#: 会显示在给模型的工具说明最前面（【找图·本地·不花钱】…）。
#:
#: 最要紧的是「找图」和「看图」这一组：在屏幕上找一张**认得出的图**这件事，
#: 本地模板匹配（毫秒级、不花钱）和"截图去问视觉模型"（几秒、一次调用）都能干，
#: 不标清楚的话模型经常挑后面那条最慢最贵的路 —— 用户看到的是"它截了屏、想了半天、
#: 才开始动手"。标签 + 系统提示里的速查表把这件事说在前面。
_BUILTIN_TAGS: dict[str, tuple] = {
    # ── 找图 / 看图（最要紧的一组）──
    "find_on_screen": ("找图", "本地", "不花钱"),
    "find_in_image": ("找图", "本地", "不花钱"),
    "click_image": ("找图", "本地", "顺手点它"),
    "list_reference": ("找图", "看有哪些模板"),
    "reference_dir": ("找图", "模板放哪"),
    "resize_image": ("图像", "本地"),
    "look_at_screen": ("看图", "视觉模型", "慢", "花钱"),
    "screenshot": ("截图", "本地"),
    # ── 标记：找到位置之后固定下来，之后一律用名字引用 ──
    "mark_region": ("标记", "框选"),
    "mark_point": ("标记", "点一下"),
    "list_marks": ("标记", "看一遍"),
    "remove_mark": ("标记", "擦掉"),
    "clear_marks": ("标记", "清空"),
    "show_marks": ("标记", "只藏不删"),
    # ── 鼠标 / 键盘 ──
    "mouse_position": ("鼠标", "查位置"),
    "mouse_move": ("鼠标", "移动"),
    "mouse_click": ("鼠标", "点击"),
    "mouse_drag": ("鼠标", "拖拽"),
    "mouse_scroll": ("鼠标", "滚动"),
    "type_text": ("键盘", "打字"),
    "press_keys": ("键盘", "快捷键"),
    # ── 文件 ──
    "list_files": ("文件", "看目录"),
    "search_files": ("文件", "按名字找"),
    "find_files": ("文件", "通配符找"),
    "grep_files": ("文件", "搜内容"),
    "read_file": ("文件", "读"),
    "write_file": ("文件", "写", "会改磁盘"),
    "edit_file": ("文件", "改", "会改磁盘"),
    "open_path": ("文件", "用它打开"),
    "remember": ("记忆", "记下来"),
    "recall": ("记忆", "想起来"),
    # ── 应用 / 网页 ──
    "open_app": ("应用", "打开"),
    "open_url": ("网页", "打开"),
    "web_search": ("网页", "搜一下"),
    "app_map": ("应用", "改映射表"),
    # ── 系统 ──
    "get_time": ("时间", "本地"),
    "system_info": ("系统", "查状态"),
    "list_windows": ("窗口", "看列表"),
    "focus_window": ("窗口", "切到前面"),
    "list_processes": ("进程", "看谁占资源"),
    "kill_process": ("进程", "结束它", "要确认"),
    "wait": ("等待",),
    "volume": ("声音", "调音量"),
    "media_control": ("声音", "播放控制"),
    "clipboard": ("剪贴板",),
    "lock_screen": ("系统", "锁屏"),
    "power": ("电源", "关机重启", "要确认"),
    "run_command": ("系统", "执行命令", "要确认"),
    "window": ("窗口", "桌面/关闭/切换"),
    # ── 交互 / 后台 ──
    "keep_listening": ("连续对话", "别走"),
    "new_session": ("连续对话", "换话题"),
    "start_watch": ("盯梢", "定时看", "要确认"),
    "list_watches": ("盯梢", "看进度"),
    "stop_watch": ("盯梢", "别盯了"),
    "spawn_subagent": ("子代理", "后台干"),
    "subagent_status": ("子代理", "看进度"),
    "cancel_subagent": ("子代理", "叫停"),
    "permission_mode": ("权限", "查或切"),
    "restart_self": ("程序自己", "重启", "要确认"),
    "quit_self": ("程序自己", "退出", "要确认"),
}

#: 结果预算放宽的工具：读文件是"整篇内容就是答案"，800 字连一段说明都装不下。
#: 4000 字大约 1.5k token，够读完一份操作说明；更长的靠 read_file 的 start 续读。
_BUILTIN_BUDGETS: dict[str, int] = {
    "read_file": 4000,
    "grep_files": 1600,
    "find_files": 1600,
    "search_files": 1600,
}

#: 操作类别：**同一条指令里，同一类操作只问一次**。
#:
#: 语音场景里"把窗口拖到左边、再拖一下右边那条""点这里、再点那里"往往是一条
#: 指令里的连续动作；每个动作都弹一次确认，用户就没法用了（他刚说完"确认"，
#: 下一个动作又来问一遍）。但**换一条指令必须重新问** —— 确认记忆一轮结束就清空。
#:
#: 只给"同一件事会连着做很多次"的类别打，而且只让**同意**在类别内复用：
#: 拒绝某一次不等于拒绝这一类。
_BUILTIN_GROUPS: dict[str, str] = {
    "mouse_drag": "鼠标操作",
    "mouse_click": "鼠标操作",
    "click_image": "鼠标操作",
    "mouse_move": "鼠标操作",
    "mouse_scroll": "鼠标操作",
    "type_text": "键盘输入",
    "press_keys": "键盘输入",
}

SKILL_INFOS: list = []
_SKILLS_LOADED = False
_BUILTIN_SNAPSHOT: dict[str, Tool] = {}
# ───────────────────────── 注册表 ─────────────────────────

# ───────────────────────── 注册内置工具 ─────────────────────────
# 放在文件最末尾：此时 Tool / register / _params / _S 全部就绪，
# import 这一下就把那 30 个内置工具注册进表里了。
from . import builtin  # noqa: E402,F401
