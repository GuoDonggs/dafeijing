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
