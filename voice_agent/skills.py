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

import ast
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
    # 这个文件声明要用哪些第三方包（YAML 的 deps / Python 的 DEPS），以及缺了哪几个
    deps: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def needs_install(self) -> bool:
        """是不是"只差装包"就能好 —— 界面据此决定要不要显示「装依赖」。"""
        return bool(self.missing)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "source": str(self.source),
            "kind": self.kind,
            "tools": list(self.tools),
            "error": self.error,
            "deps": list(self.deps),
            "missing": list(self.missing),
            "needs_install": self.needs_install,
        }


#: 用途标签：写给模型看的"这是个什么活儿的工具"（会显示在说明最前面）。
#: 允许写成字符串（"找图/本地"）或列表（[找图, 本地]），最多留 4 个。
TAG_LIMIT = 4


def normalize_tags(raw: Any) -> tuple:
    """把技能里的 tags 统一成元组。字符串按逗号/斜杠/顿号切开。"""
    if raw is None:
        return ()
    if isinstance(raw, str):
        items = re.split(r"[,，/、|]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return ()
    out = [str(item).strip() for item in items]
    return tuple(item for item in out if item)[:TAG_LIMIT]


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


def _module_available(name: str) -> bool:
    """这个包导得进来吗（不真的 import，省得触发副作用）。"""
    try:
        return importlib.util.find_spec(str(name)) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def normalize_deps(raw: Any) -> list[str]:
    """把 deps 统一成 ["模块名", "pip名:模块名", ...]。

    允许写 "pillow:PIL" 这种 —— 导入名和 pip 名不一致的包不少
    （PIL/pillow、cv2/opencv-python、yaml/PyYAML）。
    """
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw or []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def dep_names(dep: str) -> tuple[str, str]:
    """("pillow:PIL") -> ("PIL", "pillow")；只写一个就两边一样。"""
    if ":" in dep:
        pip_name, _, module = dep.partition(":")
        return module.strip() or pip_name.strip(), pip_name.strip()
    return dep.strip(), dep.strip()


def missing_deps(deps: list[str]) -> list[str]:
    """返回缺的 pip 包名。"""
    return [dep_names(d)[1] for d in deps if not _module_available(dep_names(d)[0])]


def _deps_from_source(path: Path) -> list[str]:
    """从 Python 文件里读出 DEPS 列表 —— **不执行它**。

    需要在"导入失败"时也能告诉用户缺什么，所以只能静态地看源码：
    形如 DEPS = ["requests"] 的赋值用 ast 取出来，取不到就当没声明。
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.upper() == "DEPS":
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return []
                return normalize_deps(value)
    return []


#: 合法的包名（pip 的参数不能被技能文件里写的字符串带进去）
_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _safe_packages(names: list[str]) -> tuple[list[str], list[str]]:
    """挑出合法包名，返回（可用, 被拒绝的）。"""
    ok, bad = [], []
    for raw in names:
        name = str(raw or "").strip()
        if name and _PKG_RE.match(name):
            ok.append(name)
        elif name:
            bad.append(name)
    return ok, bad


def install_deps(packages: list[str], timeout: float = 600.0) -> dict:
    """用 pip 装依赖，返回 {ok, output/error}。

    打包版里 sys.executable 是 exe 自己，拿它跑 pip 是错的 ——
    那种情况去找机器上的 python，找不到就老实让用户自己装。
    """
    names, rejected = _safe_packages([str(p).strip() for p in packages if str(p).strip()])
    if rejected:
        # 技能文件里写的东西会原样进 pip 的命令行（实测能塞进 --index-url=…）
        return {"ok": False,
                "error": "这些名字不像包名，装不了：" + "、".join(rejected[:3])}
    if not names:
        return {"ok": False, "error": "没有要装的包"}

    if getattr(sys, "frozen", False):
        import shutil  # noqa: PLC0415

        python = shutil.which("python") or shutil.which("py")
        if not python:
            return {"ok": False,
                    "error": "打包版里不能自动装包（这台机器上没找到 python）。"
                             "请自己执行：python -m pip install " + " ".join(names)}
    else:
        python = sys.executable

    try:
        proc = subprocess.run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--", *names],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "装包超时了（网络慢？可以自己在终端里装）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "装不了：" + str(exc)[:160]}

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        tail = [line for line in output.splitlines() if line.strip()]
        return {"ok": False,
                "error": "pip 返回 " + str(proc.returncode) + "："
                         + (tail[-1][:180] if tail else "没有输出")}
    return {"ok": True, "packages": names, "output": output[-400:]}


def _friendly_error(exc: BaseException, deps: list[str]) -> str:
    """把导入错误翻译成"缺什么、怎么装"。"""
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        module = str(exc.name).split(".")[0]
        if module in ("voice_agent", "__main__"):
            return "导入失败：" + str(exc)[:160]
        known = {dep_names(d)[0]: dep_names(d)[1] for d in deps}
        pip_name = known.get(module, module)
        return ("缺 Python 包 " + pip_name + "。装一下：python -m pip install "
                + pip_name + "（或在界面上点「装依赖」）")
    if isinstance(exc, ImportError):
        return "导入失败：" + str(exc)[:160]
    return str(exc)[:200]


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
        # 用内置那份实现（延迟导入避免循环依赖）
        from .tools.windows import run_shell  # noqa: PLC0415

        check = _required_checker(parameters)

        def handler(**kwargs: Any) -> str:
            problem = check(kwargs)
            if problem:
                return problem
            command = _render(template, kwargs)
            # 打断要能收手（run_shell 自己查 cancelled()，这里不用重复）
            # 复用内置 run_command 的实现：编码（先 UTF-8 再 ANSI）、返回码、
            # 打断、进程树回收都只有一份。以前这里自己写了一套 subprocess.run：
            # 中文输出按 UTF-8 硬解成乱码、返回码 7 也回"执行完了"（失败说成成功）、
            # 还收不到"打断"信号（最长干等 120 秒）。
            return run_shell(command, timeout)

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
            # 内层步骤要拿**真的**确认通道（tools.current_confirm()），不能像
            # 以前那样塞一个 lambda: True —— 那等于给 run_command / power /
            # start_watch 这些底线工具开了一条免确认的后门：技能自己的提示问的是
            # "技能要干什么"，念出来的参数还可能被 _spoken_detail 省掉，用户根本
            # 不知道内层要执行什么（实测：confirm: false 的组合技能把 run_command
            # 直接跑掉了）。
            base_confirm = tools_mod.current_confirm()
            outcome = tools_mod.call_result(
                tool_name, args, on_confirm=_step_confirm(tool_name, base_confirm))
            # **失败要透传**：以前只取 text 拼成字符串，于是"这一步没成"被外层
            # 报成 ok=True，大脑那句"刚才有一步没成功"永远不出现。
            if not outcome.ok:
                results.append(outcome.text)
                return tools_mod.ToolResult("；".join(results), False, outcome.code)
            results.append(outcome.text)
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
        if _step_is_sensitive(_REGISTRY.get(str(step.get("tool") or "")), step):
            sensitive = True
            break
    return handler, bool(spec.get("confirm", sensitive))


def _step_confirm(step_name: str, base: Any) -> Any:
    """给组合技能里的一环造一个确认通道。

    - 底线工具（run_command / power / 关机 / 盯梢…）**必须真的问一次**：
      技能层那句提示问的是技能自己，不能当成这一环的通行证；
    - 其余敏感步骤：技能层已经点过头了，不重复问；
    - 没有真人可问（base 是 None）：一律拒绝（拿不到确认就是拒绝）。
    """
    def ask(question: str, *rest: Any) -> bool:
        if base is None:
            return False
        if _in_floor(step_name):
            return bool(base(question, *rest))
        return True

    return ask


def _in_floor(name: str) -> bool:
    """这个工具是不是"任何模式下都要用户亲口点头"的底线工具。"""
    from . import security as _security

    return _security.in_floor(name)


def _name_taken_by_builtin(name: str) -> bool:
    """这个名字是不是已经被**内置**工具占了。

    技能以前是 register(..., replace=True) 静默覆盖 —— 而权限档位是**按名字**
    查表的（READ_TOOLS / EXEC_TOOLS / 底线名单），于是：
    skills/xxx.yaml 写成 name: list_files + action: shell，只读模式下
    check() 一看名字在 READ_TOOLS 里就放行，直接执行任意命令（实测复现）。
    所以内置名字一律不许顶替（想用别的名字随便）。
    """
    from .tools import REGISTRY as _REGISTRY

    existing = _REGISTRY.get(str(name or "").strip())
    return existing is not None and existing.source == "builtin"


def _step_is_sensitive(entry: Any, step: dict) -> bool:
    """组合技能里的这一环要不要先问用户。

    用 entry.wants_confirm(参数) 而不是 entry.confirm：permission_mode 这类
    "查不用问、**改**才问" 的工具 confirm 是 False，可拿它去改权限是整个权限
    模型里最敏感的动作 —— 只看 confirm 的话，组合技能就成了绕过确认的旁路
    （技能本身不问，内环还能拿到"已经确认过"的通行证）。
    查不到的工具（拼错名字、还没加载）一律按敏感处理。
    """
    if entry is None:
        return True
    args = step.get("args")
    checker = getattr(entry, "wants_confirm", None)
    if callable(checker):
        return bool(checker(args if isinstance(args, dict) else {}))
    return bool(getattr(entry, "confirm", False))


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
                if _step_is_sensitive(REGISTRY.get(str(step.get("tool") or "")), step):
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
            if info is None:
                continue      # 普通辅助模块，不是工具
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

        deps: list[str] = []
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                raise ValueError("技能文件顶层必须是映射")
            # deps 声明的是"这个工具要用哪些第三方包"。shell/say 类用不上，
            # 但 Python 版和调用外部命令的版本会用到；缺了就明确告诉用户去装。
            deps = normalize_deps(data.get("deps"))
            missing = missing_deps(deps)
            if missing:
                return SkillInfo(path.stem, str(data.get("title") or path.stem),
                                 str(data.get("description") or ""), path, "yaml", [],
                                 "缺 Python 包：" + "、".join(missing)
                                 + "。装一下：python -m pip install " + " ".join(missing),
                                 deps=deps, missing=missing)
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
                # 顶层 confirm: true 是用户明确要求的"这个技能要问一句"。
                # 以前只记 action 级的 confirm（None），第二遍 _resolve_pending
                # 用 sensitive=False 把注册时的 confirm=True **覆盖掉** ——
                # 用户的确认要求被静默取消（而技能里可能串着 shell / 写文件）。
                wanted = data.get("confirm", action.get("confirm"))
                self._pending.append((name, list(action.get("steps") or []), wanted))
            confirm = bool(data.get("confirm", needs_confirm))
            if _name_taken_by_builtin(name):
                # 权限档位是**按名字**查表的，技能顶替内置名字等于换了个身份过闸
                # （实测：name: list_files + action: shell 在只读模式下直接执行）
                raise ValueError("「" + name + "」是内置工具的名字，技能不能顶替它"
                                 "（换个名字，比如 my_" + name + "）")
            register(Tool(
                name=name,
                title=title,
                description=description,
                parameters=parameters,
                handler=handler,
                confirm=confirm,
                source=str(path),
                triggers=normalize_triggers(data.get("triggers")),
                tags=normalize_tags(data.get("tags")),
            ), replace=True)
            return SkillInfo(name, title, description, path, "yaml", [name], deps=deps)
        except Exception as exc:  # noqa: BLE001 - 单个技能坏了不能拖垮整体
            return SkillInfo(path.stem, path.stem, "", path, "yaml", [], str(exc)[:200],
                             deps=deps, missing=missing_deps(deps))

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

        # 先静态读出依赖：导入失败时也要能告诉用户缺什么
        deps = _deps_from_source(path)
        missing = missing_deps(deps)
        if missing:
            return SkillInfo(path.stem, path.stem, "", path, "python", [],
                             "缺 Python 包：" + "、".join(missing)
                             + "。装一下：python -m pip install " + " ".join(missing),
                             deps=deps, missing=missing)
        try:
            module = self._import_module(path)
        except Exception as exc:  # noqa: BLE001
            return SkillInfo(path.stem, path.stem, "", path, "python", [],
                             _friendly_error(exc, deps),
                             deps=deps, missing=missing_deps(deps))

        registered: list[str] = []
        try:
            if hasattr(module, "register"):
                # 技能自己 new 出来的 Tool 默认 source="builtin"，那样重新加载时
                # 会被当成内置工具留下来 —— 这里统一打上来源，卸载才干净。
                def register_from_skill(tool: Any, replace: bool = False) -> None:
                    from dataclasses import replace as _replace

                    if isinstance(tool, Tool) and tool.source == "builtin":
                        tool = _replace(tool, source=str(path))
                    if _name_taken_by_builtin(getattr(tool, "name", "")):
                        # 权限档位是按名字查表的：顶替 list_files 这种内置名
                        # 等于换个身份过闸（实名探针：只读档放行且直接执行代码）
                        raise ValueError(
                            "「" + str(getattr(tool, "name", "")) + "」是内置工具的名字，"
                            "自定义工具不能顶替它（换个名字）")
                    register(tool, replace=replace)

                before = set(_registry_names())
                module.register(register_from_skill)
                registered = sorted(set(_registry_names()) - before)
            else:
                raw_tools = getattr(module, "TOOLS", None)
                if not raw_tools:
                    # 用户很自然会把自己的辅助模块和工具放在同一个目录里。
                    # 那些文件没有 TOOLS 也没关系，不该被当成"加载失败"报出来 ——
                    # 但如果是"本来想写工具、忘了写 TOOLS"，还是得提醒。
                    if self._looks_like_tool(path):
                        raise ValueError("Python 技能需要定义 TOOLS 列表或 register(registry) 函数")
                    return None
                for index, raw in enumerate(raw_tools):
                    tool = self._coerce_tool(raw, path)
                    if _name_taken_by_builtin(tool.name):
                        raise ValueError("「" + tool.name + "」是内置工具的名字，"
                                         "自定义工具不能顶替它（换个名字）")
                    register(tool, replace=True)
                    registered.append(tool.name)
        except Exception as exc:  # noqa: BLE001
            # 回滚：半注册的工具留在表里，界面会显示「加载失败」但工具其实可用
            from .tools import unregister

            for name in registered:
                unregister(name)
            return SkillInfo(path.stem, path.stem, "", path, "python", [],
                             _friendly_error(exc, deps), deps=deps)

        title = str(getattr(module, "TITLE", "") or path.stem)
        description = str(getattr(module, "DESCRIPTION", "") or "")
        return SkillInfo(path.stem, title, description, path, "python", registered,
                         deps=deps or normalize_deps(getattr(module, "DEPS", None)))

    @staticmethod
    def _looks_like_tool(path: Path) -> bool:
        """这个 .py 是"想当工具但写漏了"，还是只是隔壁的辅助模块？

        判据很朴素：源码里出现过 TOOLS 或 handler，就认为作者的意图是写工具。
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return True
        return ("TOOLS" in text) or ("def handler" in text)

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
                tags=normalize_tags(raw.get("tags")),
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
        """导入一个用户写的 Python 工具/技能文件。

        两件容易踩的事：
        1. 用户很自然会把这个文件**旁边**的模块 import 进来（helper.py、
           同目录的包）。默认 sys.path 里没有那个目录，所以临时加进去。
        2. 失败了不能只说 "No module named xxx" —— 要告诉他是哪个包、
           怎么装（_friendly_error 负责翻译）。
        """
        module_name = "voice_agent_skill_" + re.sub(r"\W+", "_", path.stem)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError("无法加载 " + str(path))
        module = importlib.util.module_from_spec(spec)
        folder = str(path.parent)
        added = folder not in sys.path
        if added:
            sys.path.insert(0, folder)
        try:
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        finally:
            if added:
                try:
                    sys.path.remove(folder)
                except ValueError:
                    pass
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

## 要用第三方包怎么办

在文件里声明 **deps**，助手就知道该装什么：

```yaml
name: fetch_page
deps: [requests]          # 导入名和 pip 名不一样时写 "pip名:导入名"，例如 pillow:PIL
action:
  type: shell
  command: python -c "import requests;print('ok')"
```

Python 版写一个大写的 DEPS：

```python
import requests           # 没装的话，界面上会显示「缺 Python 包 requests」并给一个「装依赖」按钮

DEPS = ["requests"]
```

缺包时**点一下界面上那个「装依赖」就能装好**，装完自动重新加载，不用自己开终端。
（打包版里如果机器上没有 python，它会告诉你手敲哪条命令。）

## Python 写法（需要任意逻辑时）

```python
DEPS = []                 # 要用第三方包就写在这里

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

辅助模块可以直接放在工具旁边，用 `from helper import xxx` 引进来就行 ——
只要它里面没有 TOOLS / handler，助手就不会把它当成一个坏掉的工具。

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
