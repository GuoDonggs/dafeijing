# -*- coding: utf-8 -*-
"""技能（Skill）：不写框架代码也能给助手加能力。

支持两种写法，放在 ./skills 或 ~/.voice-agent/skills 里即可被自动加载：

**1. YAML 声明式（推荐给普通用户）**

    # skills/greet.yaml
    name: greet
    title: 打招呼
    description: 用一句自定义的话跟用户打招呼。用户说「跟我打个招呼」时调用。
    parameters:
      who:
        type: string
        description: 怎么称呼用户
        required: true
    action:
      type: say
      text: "你好呀，{who}！我是你的电脑助手。"

**2. Python 编程式（需要任意逻辑时）**

    # skills/disk.py
    def handler(drive: str = "C") -> str:
        return drive + " 盘一切正常"

    TOOLS = [{
        "name": "disk_health",
        "title": "硬盘体检",
        "description": "检查某个盘的健康状况。",
        "parameters": {"drive": {"type": "string", "description": "盘符"}},
        "handler": handler,
    }]

两种写法都会被转成统一的 Tool 注册进工具表，于是 LLM 能自动发现它们，
离线规则模式也能通过 JSON 调用。技能出错只会让这一个技能不可用，
不会影响整台助手启动。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .config import PROJECT_ROOT

__all__ = ["SkillLoader", "SkillInfo", "SKILL_DIRS", "skill_template", "tool_template",
           "USER_SKILL_DIR", "USER_TOOL_DIR", "PROJECT_TOOL_DIR"]


USER_SKILL_DIR = Path.home() / ".voice-agent" / "skills"
# 「自定义工具」和「技能」是同一套文件格式、同一个加载器，区别只在放在哪个目录、
# 界面上怎么称呼：tools/ 放"给助手加一个能力"，skills/ 放"教它一套说法"。
# 两者最终都变成工具表里的一条，LLM 与离线规则都能用。
USER_TOOL_DIR = Path.home() / ".voice-agent" / "tools"
PROJECT_TOOL_DIR = PROJECT_ROOT / "tools"
SKILL_DIRS: tuple[Path, ...] = (PROJECT_ROOT / "skills", USER_SKILL_DIR,
                                PROJECT_TOOL_DIR, USER_TOOL_DIR)

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_ACTION_TYPES = ("say", "shell", "open", "url", "app", "sequence")


@dataclass
class SkillInfo:
    """一个技能文件的加载结果，供 CLI / UI 展示。"""

    name: str
    title: str
    description: str
    source: Path
    kind: str = "yaml"
    tools: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "source": str(self.source),
            "kind": self.kind,
            "tools": list(self.tools),
            "error": self.error,
        }


def normalize_triggers(raw: Any) -> tuple:
    """把 triggers 统一成 ((说法, {预设参数}), ...)。

    允许两种写法：
        triggers: ["网络信息", "我的 IP"]              # 只给说法
        triggers:                                     # 说法 + 预设参数
          - phrase: 打个招呼
            args: {who: 你}
    """
    out: list[tuple[str, dict]] = []
    if isinstance(raw, str):
        raw = [raw]
    for item in raw or []:
        if isinstance(item, str):
            phrase, args = item.strip(), {}
        elif isinstance(item, dict):
            phrase = str(item.get("phrase") or item.get("text") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
        else:
            continue
        if phrase:
            out.append((phrase, dict(args)))
    return tuple(out)


def _render(template: str, values: dict[str, Any]) -> str:
    """把 {参数名} 换成实际参数；认不出来的占位符原样留着，方便排查。"""
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values or values[key] is None:
            return match.group(0)
        return str(values[key])

    return _PLACEHOLDER.sub(replace, str(template))


class _Missing(dict):
    """format_map 用：缺参数时不要把 KeyError 抛给用户。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _required_checker(parameters: dict):
    """生成一个「必填参数检查器」：缺参数就返回提示，让模型补上再调用。"""
    required = [str(name) for name in (parameters.get("required") or [])]

    def check(kwargs: dict) -> str:
        missing = [name for name in required if not str(kwargs.get(name, "")).strip()]
        if missing:
            return "缺少必要参数：" + "、".join(missing) + "，请补全后再调用"
        return ""

    return check


def _tidy(text: str) -> str:
    """把占位符替换后留下的空标点收拾干净：你好，！ → 你好！"""
    out = str(text)
    for _ in range(3):
        out = out.replace("，，", "，").replace("，。", "。").replace("，！", "！")
        out = out.replace("：，", "：").replace("、，", "，").replace(" ，", " ")
    return out.strip()


def _template_fields(action: dict) -> dict[str, set[str]]:
    """收集动作模板里出现的 {占位符}：{来源: 占位符集合}。"""
    found: dict[str, set[str]] = {}

    def scan(label: str, value: Any) -> None:
        if isinstance(value, str):
            names = set(_PLACEHOLDER.findall(value))
            if names:
                found.setdefault(label, set()).update(names)

    scan("text", action.get("text"))
    scan("command", action.get("command"))
    scan("target", action.get("target") or action.get("url") or action.get("app"))
    for index, step in enumerate(action.get("steps") or []):
        if isinstance(step, dict) and isinstance(step.get("args"), dict):
            for key, value in step["args"].items():
                scan("steps[" + str(index) + "]." + str(key), value)
    return found


def _say_handler(text: str, **_extra: Any) -> str:
    return text


def _make_action(spec: dict, title: str, parameters: dict | None = None) -> tuple[Callable[..., str], bool]:
    """把 YAML 里的 action 编译成 (处理函数, 是否需要确认)。"""
    from . import tools as tools_mod  # 延迟导入，避免循环依赖

    parameters = parameters or {"type": "object", "properties": {}}
    if not isinstance(spec, dict):
        raise ValueError("action 必须是一个映射")
    kind = str(spec.get("type") or "").strip().lower()
    if kind not in _ACTION_TYPES:
        raise ValueError("action.type 只能是 " + " / ".join(_ACTION_TYPES))

    if kind == "say":
        template = str(spec.get("text") or "").strip()
        if not template:
            raise ValueError("action.type=say 需要 text")
        check = _required_checker(parameters)

        def handler(**kwargs: Any) -> str:
            problem = check(kwargs)
            if problem:
                return problem
            return _tidy(_render(template, kwargs))

        return handler, False

    if kind == "shell":
        template = str(spec.get("command") or "").strip()
        if not template:
            raise ValueError("action.type=shell 需要 command")
        timeout = int(spec.get("timeout") or 30)
        # 执行任意命令是有副作用的，默认要求语音确认；要绕过就显式写 confirm: false
        needs_confirm = bool(spec.get("confirm", True))

        check = _required_checker(parameters)

        def handler(**kwargs: Any) -> str:
            problem = check(kwargs)
            if problem:
                return problem
            command = _render(template, kwargs)
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=max(3, min(timeout, 120)),
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return "命令执行超时了"
            except Exception as exc:  # noqa: BLE001
                return "命令执行失败：" + str(exc)[:80]
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            output = re.sub(r"\s+", " ", output)
            if not output:
                return "执行完了，没有输出"
            return output[:400] + ("……后面还有" if len(output) > 400 else "")

        return handler, needs_confirm

    if kind in ("open", "url", "app"):
        template = str(spec.get("target") or spec.get("url") or spec.get("app") or "").strip()
        if not template:
            raise ValueError("action.type=" + kind + " 需要 target")

        def handler(**kwargs: Any) -> str:
            target = _render(template, kwargs)
            if kind == "app":
                return tools_mod.open_app(target)
            return tools_mod.open_url(target)

        return handler, False

    steps = spec.get("steps") or []
    if not isinstance(steps, list) or not steps:
        raise ValueError("action.type=sequence 需要非空的 steps 列表")

    def handler(**kwargs: Any) -> str:
        results: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            tool_name = str(step.get("tool") or "").strip()
            raw_args = step.get("args") or {}
            if isinstance(raw_args, dict):
                args = {key: _render(str(value), kwargs) if isinstance(value, str) else value
                        for key, value in raw_args.items()}
            else:
                args = {}
            results.append(tools_mod.call(tool_name, args))
        return "；".join(part for part in results if part) or "执行完了"

    # 组合技能里只要有一环是敏感工具（关机、执行命令……），整个技能就先问一句，
    # 否则 steps: [{tool: power, args: {action: shutdown}}] 在语音路径上会直接执行。
    #
    # 关键点：技能按文件名顺序加载，「查不到的工具」也必须按敏感处理。
    # 早先这里用 getattr(..., "confirm", False)，于是 aaa.yaml 里引用 zzz.yaml 定义的
    # confirm:true 工具时判成 False，组合技能就成了绕过确认的后门。
    from .tools import REGISTRY as _REGISTRY

    sensitive = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        entry = _REGISTRY.get(str(step.get("tool") or ""))
        if entry is None or entry.confirm:
            sensitive = True
            break
    return handler, bool(spec.get("confirm", sensitive))


class SkillLoader:
    """扫描技能目录，把 YAML / Python 技能注册进工具表。"""

    def __init__(self, dirs: tuple[Path, ...] | list[Path] = SKILL_DIRS) -> None:
        self.dirs = [Path(d) for d in dirs]
        # 组合技能的敏感性要等所有文件都加载完才能判定，这里先记下来
        self._pending: list[tuple[str, list, Any]] = []

    # -- 对外 -----------------------------------------------------------
    def load_all(self) -> list[SkillInfo]:
        self._pending = []
        infos: list[SkillInfo] = []
        for directory in self.dirs:
            infos.extend(self.load_dir(directory))
        self._resolve_pending()
        return infos

    def _resolve_pending(self) -> None:
        """第二遍：此时所有技能都已注册，才能准确判断组合技能里哪些步骤是敏感的。

        第一遍只能保守地把「还查不到的工具」当成敏感；第二遍如果发现它其实是个
        普通工具，就把确认去掉，免得正常的组合技能每次都要多问一句。
        真正的未知工具（拼错名字）仍然保持敏感。
        """
        from dataclasses import replace

        from .tools import REGISTRY, register

        for name, steps, explicit in self._pending:
            tool = REGISTRY.get(name)
            if tool is None:
                continue
            sensitive = False
            for step in steps:
                if not isinstance(step, dict):
                    continue
                entry = REGISTRY.get(str(step.get("tool") or ""))
                if entry is None or entry.confirm:
                    sensitive = True
                    break
            confirm = bool(sensitive if explicit is None else explicit)
            if tool.confirm != confirm:
                register(replace(tool, confirm=confirm), replace=True)

    def load_dir(self, directory: Path) -> list[SkillInfo]:
        if not directory.is_dir():
            return []
        # 放在 tools/ 里的按「自定义工具」展示，skills/ 里的按「技能」展示；
        # 加载方式完全一样
        from_tools = directory.name.lower() == "tools"
        infos: list[SkillInfo] = []
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in (".yaml", ".yml"):
                info = self._load_yaml(path)
            elif path.suffix.lower() == ".py" and not path.name.startswith("_"):
                info = self._load_python(path)
            else:
                continue
            if from_tools:
                info.kind = "tool"
            infos.append(info)
        return infos

    def ensure_user_dir(self, kind: str = "skill") -> Path:
        """保证用户目录存在，并放一个说明文件与示例。"""
        directory = USER_TOOL_DIR if kind == "tool" else USER_SKILL_DIR
        directory.mkdir(parents=True, exist_ok=True)
        readme = directory / "README.md"
        if not readme.is_file():
            readme.write_text(tool_template() if kind == "tool" else skill_template(),
                              encoding="utf-8")
        return directory

    # -- YAML -----------------------------------------------------------
    def _load_yaml(self, path: Path) -> SkillInfo:
        from .tools import Tool, register

        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("技能文件顶层必须是映射")
            name = str(data.get("name") or path.stem).strip()
            if not _NAME_RE.match(name):
                raise ValueError("name 只能是小写字母开头的英文/数字/下划线，例如 my_skill")
            title = str(data.get("title") or name).strip()
            description = str(data.get("description") or title).strip()
            parameters = self._normalize_parameters(data.get("parameters"))
            action = data.get("action") or {}
            # 模板里写了 {xxx} 却没在 parameters 里声明 —— 多半是拼错了，
            # 与其在朗读时念出 "{xxx}"，不如加载时就报错
            declared = set((parameters.get("properties") or {}).keys())
            for label, names in _template_fields(action).items():
                unknown = sorted(names - declared)
                if unknown:
                    raise ValueError(
                        label + " 里用到的 {" + "}、{".join(unknown) + "} 没有在 parameters 里声明"
                    )
            handler, needs_confirm = _make_action(action, title, parameters)
            if str(action.get("type") or "").strip().lower() == "sequence":
                self._pending.append((name, list(action.get("steps") or []), action.get("confirm")))
            confirm = bool(data.get("confirm", needs_confirm))
            register(Tool(
                name=name,
                title=title,
                description=description,
                parameters=parameters,
                handler=handler,
                confirm=confirm,
                source=str(path),
                triggers=normalize_triggers(data.get("triggers")),
            ), replace=True)
            return SkillInfo(name, title, description, path, "yaml", [name])
        except Exception as exc:  # noqa: BLE001 - 单个技能坏了不能拖垮整体
            return SkillInfo(path.stem, path.stem, "", path, "yaml", [], str(exc)[:200])

    @staticmethod
    def _normalize_parameters(raw: Any) -> dict:
        """允许把参数写成简写形式，统一成 JSON Schema。"""
        if isinstance(raw, dict) and "type" in raw and "properties" not in raw:
            # 用户把整个 schema 当成了「一个参数」，结果是属性名变成 type/description
            raise ValueError(
                "parameters 要写「参数名: 参数定义」，例如\n"
                "  parameters:\n"
                "    who: {type: string, description: 称呼}"
            )
        properties: dict[str, dict] = {}
        required: list[str] = []
        for key, spec in (raw or {}).items():
            if not isinstance(spec, dict):
                spec = {"type": "string", "description": str(spec)}
            item = dict(spec)
            item.pop("_required", None)
            item.setdefault("type", "string")
            item.setdefault("description", key)
            if item.pop("required", False):
                required.append(str(key))
            properties[str(key)] = item
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    # -- Python ---------------------------------------------------------
    def _load_python(self, path: Path) -> SkillInfo:
        from .tools import Tool, register

        try:
            module = self._import_module(path)
        except Exception as exc:  # noqa: BLE001
            return SkillInfo(path.stem, path.stem, "", path, "python", [], "导入失败：" + str(exc)[:160])

        registered: list[str] = []
        try:
            if hasattr(module, "register"):
                # 技能自己 new 出来的 Tool 默认 source="builtin"，那样重新加载时
                # 会被当成内置工具留下来 —— 这里统一打上来源，卸载才干净。
                def register_from_skill(tool: Any, replace: bool = False) -> None:
                    from dataclasses import replace as _replace

                    if isinstance(tool, Tool) and tool.source == "builtin":
                        tool = _replace(tool, source=str(path))
                    register(tool, replace=replace)

                before = set(_registry_names())
                module.register(register_from_skill)
                registered = sorted(set(_registry_names()) - before)
            else:
                raw_tools = getattr(module, "TOOLS", None)
                if not raw_tools:
                    raise ValueError("Python 技能需要定义 TOOLS 列表或 register(registry) 函数")
                for index, raw in enumerate(raw_tools):
                    tool = self._coerce_tool(raw, path)
                    register(tool, replace=True)
                    registered.append(tool.name)
        except Exception as exc:  # noqa: BLE001
            # 回滚：半注册的工具留在表里，界面会显示「加载失败」但工具其实可用
            from .tools import unregister

            for name in registered:
                unregister(name)
            return SkillInfo(path.stem, path.stem, "", path, "python", [], str(exc)[:200])

        title = str(getattr(module, "TITLE", "") or path.stem)
        description = str(getattr(module, "DESCRIPTION", "") or "")
        return SkillInfo(path.stem, title, description, path, "python", registered)

    @staticmethod
    def _coerce_tool(raw: Any, path: Path) -> Any:
        from .tools import Tool

        if isinstance(raw, Tool):
            # 忘了打来源的话，reset_skills 会把它当内置工具留下来，
            # 删掉技能文件后它依然可调用，界面上也不会显示「技能」标记
            from dataclasses import replace as _replace

            tool = _replace(raw, source=str(path)) if raw.source == "builtin" else raw
        elif isinstance(raw, dict):
            tool = Tool(
                name=str(raw.get("name") or ""),
                title=str(raw.get("title") or raw.get("name") or ""),
                description=str(raw.get("description") or ""),
                parameters=raw.get("parameters") or {"type": "object", "properties": {}},
                handler=raw.get("handler"),
                confirm=bool(raw.get("confirm", False)),
                source=str(path),
                triggers=normalize_triggers(raw.get("triggers")),
            )
        else:
            raise ValueError("TOOLS 里的元素必须是 Tool 或 dict")
        if not _NAME_RE.match(tool.name):
            raise ValueError("工具名不合法：" + repr(tool.name))
        if not callable(tool.handler):
            raise ValueError("工具 " + tool.name + " 缺少 handler")
        if not tool.description:
            raise ValueError("工具 " + tool.name + " 缺少 description")
        return tool

    @staticmethod
    def _import_module(path: Path):
        module_name = "voice_agent_skill_" + re.sub(r"\W+", "_", path.stem)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("无法加载 " + str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


def _registry_names() -> list[str]:
    from . import tools as tools_mod

    return list(tools_mod.REGISTRY)


def tool_template() -> str:
    """新工具的模板（UI 的「新建工具」和用户 tools 目录的 README 都用它）。

    格式和技能完全一样 —— 本来就是同一个加载器。放 tools/ 里，
    在界面上就显示成「自定义工具」，和内置工具并排站。
    """
    return """# 大肥鲸 自定义工具

把 .yaml 或 .py 丢进这个目录，就能给助手加一个**属于你自己的工具**。
它和内置工具是平等的：模型能自动发现、能调，离线规则模式也能用触发词喊出来。

## YAML 写法（推荐）

```yaml
name: backup_docs          # 工具名：小写字母开头，英文/数字/下划线
title: 备份文档             # 给人看的名字，语音确认时会念出来
description: >-             # 这句是写给模型看的，说清楚"什么时候该用它"
  把「文档」目录打包备份到 D 盘。用户说「备份一下文档」时调用。
parameters:                # 参数表；{} 里写 required: true 表示必填
  target:
    type: string
    description: 备份到哪里
    required: false
action:
  type: shell              # say / shell / open / url / app / sequence
  command: robocopy "%USERPROFILE%\\Documents" "{target}" /MIR
  confirm: true            # 有副作用的默认就要确认；确认过再执行更安全
triggers:                  # 离线模式（没配 API Key）靠它喊得动
  - 备份文档
  - 备份一下文档
```

## 支持的 action

| type | 必填字段 | 说明 |
| --- | --- | --- |
| say | text | 直接返回这句话（可含 {参数}） |
| shell | command | 执行系统命令并返回输出；**默认需要语音确认** |
| open / url | target | 用浏览器打开网址 |
| app | target | 打开本机应用 |
| sequence | steps | 依次调用已有工具，如 steps: [{tool: get_time, args: {}}] |

## Python 写法（需要任意逻辑时）

```python
def handler(city: str = "") -> str:
    return city + " 今天晴，25 度"

TOOLS = [{
    "name": "weather",
    "title": "天气",
    "description": "查询某个城市的天气。",
    "parameters": {"city": {"type": "string", "description": "城市名"}},
    "handler": handler,
}]
```

> 工具出错只会让这一个工具不可用，不会影响助手启动。
> 改完在界面上点「重新加载」，不用重启。
"""


def skill_template() -> str:
    """新技能的模板（UI 的「新建技能」和用户目录的 README 都用它）。"""
    return """# 大肥鲸 技能目录

把 .yaml 或 .py 文件丢进这个目录，重启助手（或在界面里点「重新加载技能」）即可生效。

## YAML 写法

\u0060\u0060\u0060yaml
name: greet              # 工具名：小写字母开头，英文/数字/下划线
title: 打招呼             # 给人看的名字，语音确认时会念出来
description: 用一句自定义的话跟用户打招呼。用户说「跟我打个招呼」时调用。
parameters:              # 参数表；{} 里可以写 required: true 表示必填
  who:
    type: string
    description: 怎么称呼用户
    required: true
action:
  type: say              # say / shell / open / url / app / sequence
  text: "你好呀，{who}！我是你的电脑助手。"
\u0060\u0060\u0060

## 支持的 action

| type | 必填字段 | 说明 |
| --- | --- | --- |
| say | text | 直接返回这句话（可含 {参数}） |
| shell | command | 执行系统命令并返回输出；**默认需要语音确认** |
| open / url | target | 用浏览器打开网址 |
| app | target | 打开本机应用 |
| sequence | steps | 依次调用已有工具，如 steps: [{tool: get_time, args: {}}] |

## Python 写法

\u0060\u0060\u0060python
def handler(city: str = "") -> str:
    return city + " 今天晴，25 度"

TOOLS = [{
    "name": "weather",
    "title": "查天气",
    "description": "查询指定城市的天气。",
    "parameters": {"city": {"type": "string", "description": "城市名"}},
    "handler": handler,
}]
\u0060\u0060\u0060

返回的字符串会被直接朗读，所以写成人话、别返回 JSON。
"""
