# -*- coding: utf-8 -*-
"""权限闸门：模式、升级、限流、明文链路降级、审计与"外部内容"标记。

这套东西防的是**中转站攻击**：模型（或它背后的代理）可以随便在回答里塞工具
调用，所以"能不能动本机"不能由它说了算。这里逐条验证闸门确实拦得住，
而且拦不住的时候也要吵一声（写进审计）。

运行：python tests/test_security.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name
# 旧名字也指到同一个沙箱：两个都设，谁优先都落在同一个临时目录
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

from voice_agent import security, tools  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def make_config(mode: str = "workspace-write", base_url: str = "https://api.deepseek.com/v1",
                allow_insecure: bool = False, max_prompts: int = 6, max_same: int = 3):
    return SimpleNamespace(
        security=SimpleNamespace(mode=mode, allow_insecure=allow_insecure,
                                 max_prompts_per_minute=max_prompts,
                                 max_same_action=max_same, audit=True),
        llm=SimpleNamespace(base_url=base_url),
    )


def call(name: str, args: dict | None = None, answer: bool = True,
         questions: list | None = None) -> tools.ToolResult:
    def confirm(question: str) -> bool:
        if questions is not None:
            questions.append(question)
        return answer

    return tools.call_result(name, args or {}, on_confirm=confirm)


def audit_lines() -> list[str]:
    path = Path(_BUILD.name) / "audit.jsonl"
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").strip().splitlines()


def main() -> int:
    print("=== 权限闸门 ===")
    security.configure(make_config())
    security.reset_turn()

    print("模式阶梯")
    check("默认是标准模式", security.mode() == "workspace-write", security.mode())
    check("只读工具任何模式都放行",
          security.check(tools.REGISTRY["get_time"]).allowed)
    check("标准模式下敏感工具要确认",
          security.check(tools.REGISTRY["run_command"]).needs_confirm)
    check("标准模式下普通操作直接放行",
          security.check(tools.REGISTRY["open_app"]).allowed)

    security.configure(make_config("read-only"))
    denied = security.check(tools.REGISTRY["open_app"])
    check("只读模式拒绝常规操作", not denied.allowed and denied.code == "denied", denied.code)
    check("拒绝时给的是固定标记（模型认得出来）",
          denied.text.startswith("[permission: "), denied.text[:40])
    check("拒绝里带上了让用户放开的提示",
          "让用户" in denied.text or "本人" in denied.text, denied.text[-30:])
    check("只读模式仍然放行只读工具",
          security.check(tools.REGISTRY["system_info"]).allowed)
    check("只读模式连确认的机会都不给（不能靠说服用户绕过）",
          not security.check(tools.REGISTRY["run_command"]).needs_confirm)

    security.configure(make_config("danger-full-access"))
    check("放开模式下常规操作免确认",
          security.check(tools.REGISTRY["open_app"]).allowed)
    check("放开模式下写文件免确认",
          security.check(tools.REGISTRY["write_file"]).allowed)
    for name in ("run_command", "power", "kill_process", "quit_self", "restart_self"):
        decision = security.check(tools.REGISTRY[name])
        check("放开模式下「" + name + "」仍然要确认",
              decision.needs_confirm and decision.code == "needs_confirm", decision.code)

    print("\n确认通道：拿不到就是拒绝（fail closed）")
    security.configure(make_config())
    security.reset_limits()
    outcome = tools.call_result("run_command", {"command": "calc"})
    check("没有确认通道时敏感工具被拒绝",
          outcome.code == "denied" and outcome.text == tools.CANCEL_REPLY, outcome.code)
    check("没有确认通道时普通工具照常",
          tools.call_result("get_time", {}).ok)

    print("\n一次确认只够一次调用")
    questions: list[str] = []
    call("run_command", {"command": "echo 1"}, questions=questions)
    call("run_command", {"command": "echo 2"}, questions=questions)
    check("每次调用都重新问一遍", len(questions) == 2, str(len(questions)))
    check("用户拒绝时不执行", call("run_command", {"command": "echo 3"}, answer=False).code == "denied")

    print("\n升级只能由用户放开")
    security.configure(make_config("workspace-write"))
    ok, message = security.set_mode("read-only")
    check("收窄随时可以", ok and security.mode() == "read-only", message)
    ok, message = security.set_mode("danger-full-access")
    check("放宽走的是同一张阶梯表", ok and security.mode() == "danger-full-access", message)
    ok, message = security.set_mode("root")
    check("不存在的模式会被拒绝", not ok, message)
    check("权限工具本身必须确认（模型不能自己提权）",
          bool(tools.REGISTRY["permission_mode"].confirm))

    print("\n限流：防止反复弹确认把人问烦")
    target = str(Path(_BUILD.name) / "probe.txt")
    args = {"path": target, "content": "hi"}

    def asked_times(tool_name: str, times: int) -> int:
        """真去调用，数一共问了几次 —— 限流是在"要问用户"那一刻记的。"""
        count = 0
        for _ in range(times):
            questions = []
            call(tool_name, args, questions=questions)
            count += len(questions)
        return count

    security.configure(make_config(max_prompts=3))
    security.reset_limits()
    check("超过上限后不再弹确认", asked_times("write_file", 6) == 3,
          str(asked_times("write_file", 1)))
    questions = []
    limited = call("write_file", args, questions=questions)
    check("超限时给的是权限标记而不是默默拒绝",
          "permission" in limited.text and not questions, limited.text[:46])

    security.configure(make_config(max_prompts=0, max_same=2))
    security.reset_limits()
    check("同一个操作连着要多次也会被拦下", asked_times("write_file", 5) == 2)
    security.configure(make_config(max_prompts=0, max_same=0))
    security.reset_limits()
    questions = []
    call("write_file", args, questions=questions)
    check("把限流关掉之后不拦", len(questions) == 1, str(len(questions)))
    security.reset_limits()

    print("\n明文 HTTP 中转站：自动降级")
    ok, note = security.transport_trusted("https://api.deepseek.com/v1")
    check("https 可信", ok)
    ok, _note = security.transport_trusted("http://127.0.0.1:11434/v1")
    check("本机地址可信（ollama 这类）", ok)
    ok, note = security.transport_trusted("http://api.some-relay.example/v1")
    check("明文外网地址不可信", not ok and "明文" in note, note)
    security.configure(make_config("danger-full-access", base_url="http://relay.example/v1"))
    check("不可信链路把模式压到只读",
          security.mode() == "read-only", security.mode())
    check("压到只读之后写操作被拒",
          not security.check(tools.REGISTRY["write_file"]).allowed)
    security.configure(make_config("danger-full-access", base_url="http://relay.example/v1",
                                   allow_insecure=True))
    check("用户明确接受明文时不再强制降级",
          security.mode() == "danger-full-access", security.mode())
    security.configure(make_config())

    print("\n外部内容标记（taint）")
    security.reset_turn()
    check("新一轮开始时没有标记", not security.tainted())
    security.taint("web_search")
    check("碰过外部内容就有标记", security.tainted())
    security.reset_limits()
    questions = []
    call("run_command", {"command": "echo 4"}, questions=questions)
    check("确认提示里会提醒这是外部内容引发的",
          bool(questions) and "外部内容" in questions[0], questions[0][:30] if questions else "")
    security.reset_turn()
    check("换一轮就清掉", not security.tainted())

    print("\n审计日志")
    lines = audit_lines()
    text = "\n".join(lines)
    check("写下了审计记录", bool(lines), str(len(lines)) + " 条")
    check("记录了放行的那一次", "allowed-once" in text)
    check("记录了被拒绝的那一次", "rejected" in text)
    check("记录了模式切换", '"event": "mode"' in text)
    check("记录了限流", "rate_limited" in text)
    check("记录了明文链路降级", "transport_downgrade" in text)

    print("\n放开模式：声明了 confirm 的工具也不再问")
    # 用户的抱怨："我都设成完全开放了，拖个鼠标还要确认"。根因是工具层那句
    # `decision.needs_confirm or tool.confirm` —— 后半句让"模式说了算"永远不生效。
    from voice_agent.tools import Tool as _Tool

    original_drag = tools.REGISTRY["mouse_drag"]
    tools.register(_Tool(
        name="mouse_drag", title="拖拽鼠标", description="测试用：不会真的拖",
        parameters={"type": "object", "properties": {}},
        handler=lambda **_kw: "拖好了", confirm=True), replace=True)
    try:
        drag = tools.REGISTRY["mouse_drag"]
        check("鼠标工具带了操作类别（同类只问一次靠它）",
              drag.group == "鼠标操作", drag.group)
        security.configure(make_config("danger-full-access"))
        security.reset_limits()
        check("放开模式下拖拽不需要确认", not security.check(drag).needs_confirm)
        asked: list = []
        outcome = tools.call("mouse_drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
                             on_confirm=lambda *a: (asked.append(a), True)[1])
        check("真的调用一次也没问", asked == [], str(asked))
        check("而且确实执行了", str(outcome) == "拖好了", str(outcome))
        check("底线工具不受影响：放开模式下执行命令仍要确认",
              security.check(tools.REGISTRY["run_command"]).needs_confirm)
        # 这一条是安全回归：permission_mode 是"用户本人授权"的唯一通道，
        # 它必须任何模式下都问 —— 不能因为"放开模式不问"就被顺手放过。
        check("权限工具在放开模式下仍然要确认（不能靠模式悄悄提权）",
              security.check(tools.REGISTRY["permission_mode"]).needs_confirm)
        security.configure(make_config("workspace-write"))
        security.reset_limits()
        check("标准模式下拖拽仍然要先确认", security.check(drag).needs_confirm)
        security.configure(make_config("read-only"))
        check("只读模式下鼠标操作直接拒绝（连问都不问）",
              security.check(drag).code == "denied")
    finally:
        tools.REGISTRY["mouse_drag"] = original_drag
        security.configure(make_config("workspace-write"))

    print("\n参数坏掉时绝不拿默认值去调工具")
    # 模型的 arguments 被截断（max_tokens、网关只回半截 JSON）时，以前会
    # 当成 {} 直接调 handler —— 而 power 的默认动作是**关机**。这条断言守住它。
    security.configure(make_config("danger-full-access"))
    security.reset_limits()
    broken = tools.call_result("power", '{"action": "shut', on_confirm=lambda *_: True)
    check("参数不是合法 JSON 时不执行（哪怕放开了权限）",
          not broken.ok and broken.code == "bad_arguments", broken.code)
    check("空参数仍然照常（有些工具就是没有参数）", tools.call_result("get_time", None).ok)

    print("\n提权提示必须说清切到哪一档")
    # 只读模式下被问一句光秃秃的「要调整权限，确认吗？」然后答「确认」，
    # 用户根本不知道自己批准了什么 —— 提示里必须念出目标模式。
    question = tools.REGISTRY["permission_mode"].confirm_question(
        {"mode": "danger-full-access", "reason": "任务做不下去"})
    check("提权确认里念得出目标模式", "放开权限" in question, question)
    readonly = tools.REGISTRY["permission_mode"].confirm_question({"mode": "read-only"})
    check("切回只读也说得出", "只读模式" in readonly, readonly)

    print("\n放开模式：底线名单可以关掉")
    security.configure(make_config("danger-full-access"))
    security.reset_limits()
    check("默认底线里执行命令仍然要确认",
          security.check(tools.REGISTRY["run_command"]).needs_confirm)
    loose = make_config("danger-full-access")
    loose.security.floor_tools = []
    loose.security.keep_floor_when_empty = False
    security.configure(loose)
    check("清空底线 + 关掉保留 → 执行命令也不问了",
          security.check(tools.REGISTRY["run_command"]).allowed,
          str(security.snapshot()["floor"]))
    security.configure(make_config("danger-full-access"))
    check("默认情况下快照里能看到底线名单",
          "run_command" in security.snapshot()["floor"], str(security.snapshot()["floor"]))

    print("\n同一句提示 ≠ 同一个操作（身份按参数算）")
    a = tools.tool_fingerprint("run_command", {"command": "Get-ChildItem C:/x"})
    b = tools.tool_fingerprint("run_command", {"command": "Get-ChildItem C:/x | Remove-Item"})
    check("两条不同的命令指纹不同", a != b, a + " vs " + b)
    check("参数的顺序不影响指纹",
          tools.tool_fingerprint("x", {"a": 1, "b": 2})
          == tools.tool_fingerprint("x", {"b": 2, "a": 1}))
    # 提示是给人听的（会被"说人话"），所以两条命令的提示**可能碰巧一样**；
    # 身份必须靠指纹区分开 —— 否则上一句的同意会被当成这一句的同意。
    q1 = tools.REGISTRY["run_command"].confirm_question({"command": "Get-ChildItem C:/x"})
    q2 = tools.REGISTRY["run_command"].confirm_question(
        {"command": "Get-ChildItem C:/x | Remove-Item"})
    check("管道 + 删除会按**最危险**的动作报", "删除" in q2 and "删除" not in q1,
          q1 + " / " + q2)
    same_hint = tools.REGISTRY["run_command"].confirm_question({"command": "Get-ChildItem C:/x"})
    other_hint = tools.REGISTRY["run_command"].confirm_question({"command": "Get-ChildItem D:/y"})
    check("提示可能一样，但指纹一定不一样",
          same_hint == other_hint
          and tools.tool_fingerprint("run_command", {"command": "Get-ChildItem C:/x"})
          != tools.tool_fingerprint("run_command", {"command": "Get-ChildItem D:/y"}),
          same_hint)

    print("\n用户自己定的名单")
    security.configure(make_config())
    security.reset_limits()
    cfg = make_config()
    cfg.security.deny_tools = ["write_file"]
    cfg.security.always_confirm = ["open_app"]
    security.configure(cfg)
    banned = security.check(tools.REGISTRY["write_file"])
    check("禁止名单里的工具直接拒绝（连确认都不给）",
          not banned.allowed and banned.reason == "deny_tools", banned.reason)
    check("拒绝理由说清了是被禁止使用", "禁止使用" in banned.text, banned.text[:36])
    check("禁止名单不影响别的工具",
          security.check(tools.REGISTRY["read_file"]).allowed)
    extra = security.check(tools.REGISTRY["open_app"])
    check("额外要求确认的工具会弹确认", extra.needs_confirm, extra.code)
    check("快照里能看到这两张名单",
          security.snapshot()["deny"] == ["write_file"]
          and security.snapshot()["confirm"] == ["open_app"],
          str(security.snapshot()["deny"]) + str(security.snapshot()["confirm"]))
    security.configure(make_config())
    security.reset_limits()

    print("\n工具层的整体行为")
    security.configure(make_config("read-only"))
    security.reset_turn()
    outcome = tools.call("open_app", {"name": "记事本"}, on_confirm=lambda _q: True)
    check("只读模式下真有工具被拦下（不是只写了个函数）",
          "permission" in str(outcome), str(outcome)[:50])
    security.configure(make_config())
    check("恢复标准模式后放行",
          security.check(tools.REGISTRY["open_app"]).allowed)

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("权限闸门全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
