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

import json
import os
import re
import sys
from dataclasses import dataclass
from dataclasses import replace as _replace
from pathlib import Path
from typing import Any, Callable

from .apps import open_app, open_url, web_search
from .files import list_files, read_file, recall, remember, search_files
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
    title: str = ""
    source: str = "builtin"
    # 离线规则模式用的触发词：((说法, {预设参数}), ...)
    # LLM 模式不依赖它（模型自己看 description 判断），但没有 API Key 时
    # 它是自定义技能唯一能被「说出来」的入口。
    triggers: tuple = ()

    @property
    def display(self) -> str:
        """给人看的短名字：优先 title，其次取描述的第一句。"""
        if self.title:
            return self.title
        head = re.split(r"[。，,.;；]", self.description.strip())[0]
        return head[:16] or self.name

    def confirm_question(self, args: dict) -> str:
        """敏感操作前要念给用户听的那句话。

        必须说人话：直接拿 description 拼会念出「关机、重启、睡眠或注销电脑。
        属于敏感操作，调用前应先跟用户确认。：shutdown」，听起来像在念文档。
        """
        detail = "、".join(
            _ARG_WORDS.get(str(value).strip().lower(), str(value))
            for value in args.values()
            if str(value).strip()
        )
        question = "要" + self.display
        if detail:
            question += "：" + detail
        return question + "，确认吗？"

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
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
    if not tool.title:
        tool = _replace(tool, title=_BUILTIN_TITLES.get(tool.name, ""))
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
    """给 LLM 的工具声明列表（含技能提供的工具）。"""
    autoload_skills()
    return [tool.schema() for tool in REGISTRY.values()]


def describe() -> str:
    """供 CLI / 网页 UI 展示的工具清单（列表形式）。"""
    autoload_skills()
    width = max((len(t.name) for t in REGISTRY.values()), default=12)
    lines = []
    for tool in REGISTRY.values():
        flag = "  [需确认]" if tool.confirm else ""
        origin = "" if tool.source == "builtin" else "  ← " + Path(tool.source).name
        lines.append(
            "  " + tool.name.ljust(width) + "  " + tool.display + "："
            + tool.description.split("。")[0] + flag + origin
        )
    return "\n".join(lines)


def call(name: str, arguments: Any = None, on_confirm: Callable[[str], bool] | None = None) -> str:
    """执行一个工具，只要那句话（大多数调用方只关心这个）。"""
    return call_result(name, arguments, on_confirm).text


def call_result(name: str, arguments: Any = None,
                on_confirm: Callable[[str], bool] | None = None) -> ToolResult:
    """执行一个工具，返回（文本, 是否成功, 错误码）。

    两条重要约定：

    1. **确认失败即拒绝**。以前是「有 on_confirm 才问」，于是任何忘了传确认通道的
       调用方都能把关机、执行命令这类工具直接跑掉。现在反过来：敏感工具拿不到
       确认通道就是拒绝 —— 漏传是拒绝，不是放行。
    2. 失败也返回一句中文，但带上 ok=False，让大脑知道「这一步没成」，
       从而在开口前加一句提醒，而不是把错误当成结果自信地念出来。
    """
    autoload_skills()
    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult("没有这个工具：" + str(name), False, "unknown_tool")
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}

    if tool.confirm:
        if on_confirm is None:
            return ToolResult(CANCEL_REPLY, False, "denied")
        if not on_confirm(tool.confirm_question(args)):
            return ToolResult(CANCEL_REPLY, False, "denied")

    try:
        return ToolResult(str(tool.handler(**args)))
    except TypeError as exc:
        return ToolResult("工具参数不对：" + str(exc)[:80], False, "bad_arguments")
    except Exception as exc:  # noqa: BLE001 - 工具层永不抛出，交给模型兜底
        return ToolResult(ERROR_PREFIX + str(exc)[:100], False, "error")
__all__ = [
    "Tool",
    "REGISTRY",
    "call",
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
_PS_UTF8 = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$OutputEncoding=[System.Text.Encoding]::UTF8;"
_ARG_WORDS = {
    "shutdown": "关机", "restart": "重启", "reboot": "重启", "sleep": "睡眠", "logoff": "注销",
    "up": "调大", "down": "调小", "mute": "静音", "max": "最大", "min": "最小",
    "play_pause": "播放或暂停", "next": "下一首", "prev": "上一首", "stop": "停止",
    "get": "读取", "set": "写入", "all": "全部",
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
}
SKILL_INFOS: list = []
_SKILLS_LOADED = False
_BUILTIN_SNAPSHOT: dict[str, Tool] = {}
# ───────────────────────── 注册表 ─────────────────────────

# ───────────────────────── 注册内置工具 ─────────────────────────
# 放在文件最末尾：此时 Tool / register / _params / _S 全部就绪，
# import 这一下就把那 30 个内置工具注册进表里了。
from . import builtin  # noqa: E402,F401
