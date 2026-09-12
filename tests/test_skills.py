# -*- coding: utf-8 -*-
"""技能系统测试：用户自定义 skill / tool 的加载、调用、报错与路由。

全部在临时目录里造样本，不污染 ./skills。

运行：python tests/test_skills.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时文件挪到临时目录：测试**绝不能**碰用户真实的对话记录 / 记忆 / 标记 ——
# 否则「上次聊过什么」会渗进断言（真出现过：webui 那句回复变成「跟刚才一样」）。
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name


from voice_agent import rules, tools  # noqa: E402
from voice_agent.skills import SkillLoader, normalize_triggers  # noqa: E402

failures: list[str] = []
CREATED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


SAY = """
name: greeting
title: 打招呼
description: 跟用户问好。
parameters:
  who: {type: string, description: 称呼, required: true}
triggers:
  # 故意用一个别处不会出现的说法：项目 skills/ 里也有同名的 greet 技能，
  # 用通用说法会先命中它，测试就不稳定了
  - phrase: 测试口令阿尔法
    args: {who: 老板}
action:
  type: say
  text: "你好，{who}！"
"""

SEQUENCE = """
name: combo
title: 组合技能
description: 组合调用内置工具。
parameters: {}
action:
  type: sequence
  steps:
    - tool: get_time
      args: {}
    - tool: say_via_skill
      args: {}
"""

SHELL = """
name: shell_probe
title: 命令探测
description: 跑一条命令。
action:
  type: shell
  command: echo skill-ok
"""

SHELL_OPEN = """
name: shell_open_probe
title: 免确认命令
description: 显式声明不需要确认。
confirm: false
action:
  type: shell
  command: echo skill-ok-2
"""

PY_TOOLS = '''
TITLE = "平方计算"
DESCRIPTION = "算一个数的平方。"

def handler(value: int = 2) -> str:
    return str(value) + " 的平方是 " + str(value * value)

TOOLS = [{
    "name": "square",
    "title": "平方",
    "description": "计算一个数的平方。",
    "parameters": {"value": {"type": "integer", "description": "要算的数"}},
    "handler": handler,
    "triggers": ["平方"],
}]
'''

PY_REGISTER = '''
def _hello(name: str = "世界") -> str:
    return "register 技能说你好，" + name

def register(register_tool):
    from voice_agent.tools import Tool
    register_tool(Tool(
        name="reg_hello",
        title="注册式问候",
        description="用 register() 注册的工具。",
        parameters={"name": {"type": "string", "description": "名字"}},
        handler=_hello,
    ))
'''

BROKEN_YAML = """
name: Bad-Name
description: 名字不合法
action:
  type: say
  text: hi
"""

BROKEN_ACTION = """
name: no_action
description: 没有 action
action:
  type: teleport
"""

PY_BROKEN = "raise RuntimeError('boom')\n"


def write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def main() -> int:
    print("=== 技能系统测试 ===")
    # 先触发一次全局自动加载：否则下面的手动注册会被首次 autoload 的 reset 清掉，
    # 那是真实初始化顺序，不是缺陷，测试要跟它对齐。
    tools.autoload_skills()
    tmp = Path(tempfile.mkdtemp(prefix="voice-skills-"))

    write(tmp, "greeting.yaml", SAY)
    write(tmp, "combo.yaml", SEQUENCE)
    write(tmp, "shell.yaml", SHELL)
    write(tmp, "shell_open.yaml", SHELL_OPEN)
    write(tmp, "square.py", PY_TOOLS)
    write(tmp, "reg.py", PY_REGISTER)
    write(tmp, "bad_name.yaml", BROKEN_YAML)
    write(tmp, "bad_action.yaml", BROKEN_ACTION)
    write(tmp, "broken.py", PY_BROKEN)
    write(tmp, "notes.txt", "ignored")

    # 组合技能里引用了敏感工具（power）：整个技能必须自动变成「需要确认」
    write(tmp, "danger.yaml", """
name: danger_combo
title: 危险组合
description: 组合里含敏感工具。
parameters: {}
action:
  type: sequence
  steps:
    - tool: power
      args: {action: shutdown}
""")

    # 文件名排序在前的组合技能，引用了排序在后的敏感工具 ——
    # 这正是「按加载顺序判定敏感性」会漏掉的高危场景
    write(tmp, "aaa_combo.yaml", """
name: aaa_combo
title: 顺序敏感组合
description: 引用了按文件名排在后面的敏感工具。
parameters: {}
action:
  type: sequence
  steps:
    - tool: zzz_danger
      args: {}
""")
    write(tmp, "zzz_danger.yaml", """
name: zzz_danger
title: 危险工具
description: 一个需要确认的 shell 工具。
confirm: true
action:
  type: shell
  command: echo danger
""")
    # permission_mode 是"查不用问、改才问"的工具（confirm=False + confirm_if）：
    # 拿它去**改权限**的组合技能必须照样被标成需要确认 —— 否则技能自己不问，
    # 内环还能拿到"已经确认过"的通行证，等于开了条绕过确认的旁路。
    write(tmp, "escalate.yaml", """
name: escalate_combo
title: 偷偷提权
description: 组合里含改权限的工具。
parameters: {}
action:
  type: sequence
  steps:
    - tool: permission_mode
      args: {mode: danger-full-access, reason: 任务做不下去}
""")
    write(tmp, "ask_perm.yaml", """
name: ask_perm_combo
title: 只是查权限
description: 组合里只查询权限模式。
parameters: {}
action:
  type: sequence
  steps:
    - tool: permission_mode
      args: {}
""")

    write(tmp, "flat_params.yaml", """
name: flat_params
description: 参数被写成了扁平 schema。
parameters: {type: string, description: 名字}
action:
  type: say
  text: hi
""")
    write(tmp, "bad_placeholder.yaml", """
name: bad_placeholder
description: 模板里用了没声明的占位符。
parameters: {}
action:
  type: say
  text: "你好，{who}"
""")

    # combo 里引用的 say_via_skill：借内置技能验证 sequence 能串技能
    write(tmp, "say_via_skill.yaml", """
name: say_via_skill
title: 子技能
description: 供组合技能调用的子技能。
parameters: {}
action:
  type: say
  text: "子技能完成"
""")

    print("加载")
    infos = SkillLoader([tmp]).load_all()
    by_name = {info.name: info for info in infos}
    GOOD = ("greeting", "combo", "shell", "shell_open", "square", "reg", "say_via_skill")
    check("扫描到全部技能文件（忽略 txt）", len(infos) == 17, "实际 " + str(len(infos)))
    broken = [i.name + ": " + i.error for i in infos if i.name in GOOD and not i.ok]
    check("正常技能全部加载成功", not broken, "、".join(broken))
    check("名字不合法的技能被拒绝", not by_name["bad_name"].ok, by_name["bad_name"].error[:50])
    check("未知 action 类型被拒绝", not by_name["bad_action"].ok, by_name["bad_action"].error[:50])
    check("导入失败的 Python 技能被拒绝", not by_name["broken"].ok, by_name["broken"].error[:50])

    print("\n调用")
    check("say 技能带参数", tools.call("greeting", {"who": "张工"}) == "你好，张工！")
    # 缺必填参数时不能把 {who} 原样念出来，而要明确告诉模型补参数
    missing_reply = tools.call("greeting", {})
    check("say 技能缺必填参数时给出明确提示",
          "缺少必要参数" in missing_reply and "{" not in missing_reply, repr(missing_reply))
    check("sequence 技能串联多个工具", "子技能完成" in tools.call("combo", {}),
          tools.call("combo", {})[:40])
    check("Python TOOLS 技能", tools.call("square", {"value": 7}) == "7 的平方是 49")
    check("Python register() 技能", tools.call("reg_hello", {"name": "小明"}) == "register 技能说你好，小明")
    check("shell 技能默认需要确认", tools.REGISTRY["shell_probe"].confirm is True)
    check("shell 技能可以显式免确认", tools.REGISTRY["shell_open_probe"].confirm is False)
    check("shell 技能真的能跑（给了确认通道）",
          "skill-ok" in tools.call("shell_probe", {}, on_confirm=lambda _q: True),
          tools.call("shell_probe", {}, on_confirm=lambda _q: True)[:40])
    check("确认被拒绝时不执行",
          tools.call("shell_probe", {}, on_confirm=lambda _q: False) == tools.CANCEL_REPLY)
    # 这条是安全契约：调用方忘了传确认通道时必须是「拒绝」，而不是「无人过问就放行」
    check("没有确认通道时敏感技能一律拒绝",
          tools.call("shell_probe", {}) == tools.CANCEL_REPLY,
          repr(tools.call("shell_probe", {})))
    check("普通工具不受影响", tools.call("get_time").startswith("现在是"))

    print("\n元信息")
    tool = tools.REGISTRY["greeting"]
    check("title 用于确认提示与列表", tool.display == "打招呼")
    check("source 指向技能文件", tool.source.endswith("greeting.yaml"))
    check("schema 可交给 LLM", tool.schema()["function"]["name"] == "greeting")
    # 曾经踩过：_params() 原地 pop 共享的 schema 简写，导致 8 个内置工具的
    # required 集体消失，模型于是发空参数
    for name, expected in (
        ("open_url", "url"), ("web_search", "query"), ("run_command", "command"),
        ("search_files", "name"), ("type_text", "text"), ("remember", "text"),
    ):
        required = tools.REGISTRY[name].parameters.get("required") or []
        check("内置工具 " + name + " 的必填参数还在", required == [expected], str(required))
    check("组合技能引用敏感工具时自动要求确认", tools.REGISTRY["danger_combo"].confirm is True,
          str(tools.REGISTRY["danger_combo"].confirm))
    check("引用普通工具的组合技能不需要确认", tools.REGISTRY["combo"].confirm is False)
    # 高危回归：组合技能引用了「按文件名排在后面」的 confirm:true 工具。
    # 第一遍加载时该工具还不存在，必须保守判为敏感；第二遍再确认它确实是敏感工具。
    check("组合技能引用后置的敏感工具时仍然要求确认",
          tools.REGISTRY["aaa_combo"].confirm is True, str(tools.REGISTRY["aaa_combo"].confirm))
    check("组合技能里改权限 → 整个技能要确认",
          tools.REGISTRY["escalate_combo"].confirm is True,
          str(tools.REGISTRY["escalate_combo"].confirm))
    check("组合技能里只是查权限 → 不多问一句",
          tools.REGISTRY["ask_perm_combo"].confirm is False,
          str(tools.REGISTRY["ask_perm_combo"].confirm))
    check("扁平写法的 parameters 被明确拒绝", not by_name["flat_params"].ok,
          by_name["flat_params"].error[:60])
    check("模板里的未声明占位符被拒绝", not by_name["bad_placeholder"].ok,
          by_name["bad_placeholder"].error[:60])
    check("技能出现在 openai_tools 里",
          "square" in [t["function"]["name"] for t in tools.openai_tools()])

    print("\n触发词")
    check("字符串触发词规范化", normalize_triggers(["a", "b"]) == (("a", {}), ("b", {})))
    check("带参数的触发词规范化",
          normalize_triggers([{"phrase": "p", "args": {"x": 1}}]) == (("p", {"x": 1}),))
    check("非法的触发词被忽略", normalize_triggers([123, None, {"no_phrase": 1}]) == ())
    routed = rules.route("来一个测试口令阿尔法吧")
    check("离线路由命中技能触发词", routed == ("greeting", {"who": "老板"}), repr(routed))
    routed = rules.route("帮我算一下平方")
    check("Python 技能的触发词也生效", routed == ("square", {}), repr(routed))

    print("\n覆盖与清理")
    from voice_agent.tools import Tool

    tools.register(Tool(
        name="get_time", title="查时间", description="被技能覆盖的版本",
        parameters={"type": "object", "properties": {}}, handler=lambda **_: "覆盖成功",
    ), replace=True)
    check("技能可以覆盖同名内置工具", tools.call("get_time") == "覆盖成功")
    check("reg 技能提供了工具", "reg_hello" in tools.REGISTRY)

    # 重新加载 = 先卸载再扫描：删掉文件的技能必须真的消失，被覆盖的内置工具要还原
    tools.reset_skills()
    check("卸载后技能工具消失",
          all(name not in tools.REGISTRY for name in ("greeting", "square", "combo", "reg_hello")),
          "残留：" + "、".join(n for n in ("greeting", "square", "combo", "reg_hello") if n in tools.REGISTRY))
    check("被覆盖的内置工具已还原", tools.call("get_time").startswith("现在是"), tools.call("get_time")[:20])
    reloaded = SkillLoader([tmp]).load_all()
    check("重新加载后技能回来", "greeting" in tools.REGISTRY and len(reloaded) == 17,
      str(len(reloaded)))

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("技能系统测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
