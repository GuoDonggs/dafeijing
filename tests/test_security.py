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
    # 文案要如实：不是"用户取消了"（根本没人被问过），而是"没有确认通道"
    check("没有确认通道时敏感工具被拒绝",
          outcome.code == "denied" and outcome.text == tools.NO_CHANNEL_REPLY,
          outcome.text[:40])
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
    # 改权限必须确认（模型不能自己提权）；**只是查一下**现在几档不用问 ——
    # 用户问"现在什么权限"却被要求先确认一次，那一步毫无意义。
    permission_tool = tools.REGISTRY["permission_mode"]
    check("改权限必须确认（模型不能自己提权）",
          permission_tool.wants_confirm({"mode": "danger-full-access"}))
    check("只查询权限模式不弹确认", not permission_tool.wants_confirm({}))

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

    # 一次拒绝只该留下一行。check() 里已经记过（deny_tools / 只读拒绝），
    # call_result 以前又记一条 —— 同一个动作在日志里出现两行，事后查
    # "它到底被拦了几次"就会看糊涂。
    security.configure(make_config("read-only"))
    denied_args = {"path": str(Path(_BUILD.name) / "nope.txt"), "content": "x"}
    tools.call_result("write_file", denied_args)
    rows = [line for line in audit_lines() if "nope.txt" in line]
    check("只读拒绝只写一行审计（不是两行）", len(rows) == 1, str(len(rows)))
    check("这一行里带着参数和拒绝原因",
          '"reason": "read-only"' in rows[0] and "nope.txt" in rows[0], rows[0][:80])

    security.configure(make_config())
    security.configure(type("C", (), {"security": type("S", (), {
        "mode": "workspace-write", "deny_tools": ["write_file"], "always_confirm": [],
        "floor_tools": None, "allow_insecure": False, "max_prompts_per_minute": 6,
        "max_same_action": 3, "audit": True})(),
        "llm": type("L", (), {"base_url": "https://api.deepseek.com/v1"})()})())
    tools.call_result("write_file", {"path": str(Path(_BUILD.name) / "denied.txt"),
                                     "content": "x"})
    rows = [line for line in audit_lines() if "denied.txt" in line]
    check("禁止名单拒绝也只写一行", len(rows) == 1, str(len(rows)))
    check("记的是 blocked + deny_tools",
          '"event": "blocked"' in rows[0] and '"reason": "deny_tools"' in rows[0], rows[0][:80])
    security.configure(make_config())

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
              security.check(tools.REGISTRY["permission_mode"],
                             {"mode": "danger-full-access"}).needs_confirm)
        check("而查一下权限模式在放开模式下直接放行",
              security.check(tools.REGISTRY["permission_mode"], {}).allowed)
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

    print("\n参数决定档位：只读模式不能借「查询」工具写盘")
    # 用户报过（审查实测复现）：只读模式下调 resize_image(out=…) 真的写出了文件、
    # app_map(add) 真的改了 apps.yaml —— 而只读档正是"明文中转站"自动降级后的落点。
    security.configure(make_config("read-only"))
    art = tools.REGISTRY["resize_image"]
    mapping = tools.REGISTRY["app_map"]
    write_target = str(Path(_BUILD.name) / "sub" / "x.png")
    check("只读模式下 resize_image(out=…) 被拒绝",
          security.check(art, {"image": "a.png", "out": write_target}).code == "denied")
    check("只读模式下 resize_image（不带 out，只进数据目录）放行",
          security.check(art, {"image": "a.png"}).allowed)
    check("只读模式下 app_map(add) 被拒绝",
          security.check(mapping, {"action": "add", "name": "x", "target": "y"}).code == "denied")
    check("只读模式下 app_map(list) 放行",
          security.check(mapping, {"action": "list"}).allowed)
    security.configure(make_config("workspace-write"))
    check("标准模式下写映射表要先确认",
          security.check(mapping, {"action": "add", "name": "x", "target": "y"}).needs_confirm)
    check("而查映射表不用确认",
          not security.check(mapping, {"action": "list"}).needs_confirm)

    # 放开模式：软提醒（参数看着吓人的那种）直接做，只有"这一步本身会执行任意东西"
    # 的硬闸门才继续问。用户的原话是"放开模式下仍然无法进行部分权限操作" ——
    # 以前 app_map 写表、window/clear_marks 空参数、resize_image 另存都会被拦，
    # 拿不到确认通道时更是直接拒绝。
    security.configure(make_config("danger-full-access"))
    check("放开模式：写映射表不再确认",
          security.check(mapping, {"action": "add", "name": "x", "target": "y"}).allowed)
    check("放开模式：window 空参数不再确认",
          security.check(tools.REGISTRY["window"], {}).allowed)
    check("放开模式：clear_marks 空参数不再确认",
          security.check(tools.REGISTRY["clear_marks"], {}).allowed)
    check("放开模式：resize_image 另存到指定路径不再确认",
          security.check(art, {"image": "a.png", "out": write_target}).allowed)

    # 底线名单**以用户写的那份为准**：以前会把内建的六个并上去，于是
    # "我只想让 write_file 要确认"变成了"内建那六个也照样问"。
    custom = make_config("danger-full-access")
    custom.security.floor_tools = ["write_file"]
    security.configure(custom)
    check("自定义底线名单：写的那一个要确认",
          security.check(tools.REGISTRY["write_file"], {}).needs_confirm)
    check("自定义底线名单：没写的（执行命令）不再确认",
          security.check(tools.REGISTRY["run_command"], {}).allowed)
    check("快照里报的就是用户写的那份", security.snapshot()["floor"] == ["write_file"],
          str(security.snapshot()["floor"]))
    loose = make_config("danger-full-access")
    loose.security.floor_tools = []
    loose.security.keep_floor_when_empty = False
    security.configure(loose)
    check("底线清空 + keep=false：执行命令也不问了",
          security.check(tools.REGISTRY["run_command"], {}).allowed)
    kept = make_config("danger-full-access")
    kept.security.floor_tools = []
    kept.security.keep_floor_when_empty = True
    security.configure(kept)
    check("底线清空 + keep=true（默认）：内建那几个仍然问",
          security.check(tools.REGISTRY["run_command"], {}).needs_confirm)
    security.configure(make_config("workspace-write"))

    # 桌面版的确认通道：引擎没启动时弹一个模态框（console.confirm_hook），
    # 点了"执行"就真的能做 —— 以前这条路永远返回 False，等于没有通道。
    from voice_agent.console import Console

    probe_console = Console.__new__(Console)
    probe_console.log = lambda *_a, **_k: None
    probe_console.cfg = make_config("workspace-write")
    probe_console.confirm_hook = None
    check("没有对话框时 confirm_channel() 是 None（工具层会如实说没有通道）",
          probe_console.confirm_channel() is None)
    probe_console.confirm_hook = lambda _q: True
    check("有对话框时 confirm_channel() 可用",
          callable(probe_console.confirm_channel()))
    target = Path(_BUILD.name) / "confirmed.txt"
    outcome = tools.call_result("write_file", {"path": str(target), "content": "ok"},
                                on_confirm=probe_console.confirm_channel())
    check("标准模式下点了「执行」就真的写进去",
          outcome.ok and target.is_file(), outcome.text[:40])
    probe_console.confirm_hook = lambda _q: False
    blocked = tools.call_result("write_file", {"path": str(Path(_BUILD.name) / "no.txt"),
                                               "content": "no"},
                                on_confirm=probe_console.confirm_channel())
    check("点「取消」就不执行", not blocked.ok and blocked.text == tools.CANCEL_REPLY,
          blocked.text[:30])
    security.configure(make_config())

    # open_app 打开"命令类"映射 = 一条免确认的任意命令通道（实测能把 run_command
    # 的底线名单整个绕过去）。所以只要名字在表里指向命令，就必须先问用户。
    from voice_agent import screen as screen_mod

    saved_map = screen_mod.load_app_map()
    try:
        merged = dict(saved_map)
        merged["探针命令项"] = {"type": "command", "target": "cmd /c echo hi"}
        screen_mod.save_app_map(merged)
        opener = tools.REGISTRY["open_app"]
        check("打开「命令类映射」必须确认",
              security.check(opener, {"name": "探针命令项"}).needs_confirm)
        check("打开普通应用不额外问",
              not security.check(opener, {"name": "记事本"}).needs_confirm)
    finally:
        screen_mod.save_app_map(saved_map)

    # 空参数会做出有副作用的默认动作（最小化所有窗口 / 清掉所有标记）→ 要问一句
    check("window 空参数（=最小化全部）要确认",
          security.check(tools.REGISTRY["window"], {}).needs_confirm)
    check("window 给了 action 就不问",
          not security.check(tools.REGISTRY["window"], {"action": "desktop"}).needs_confirm)
    check("clear_marks 空参数（=全清）要确认",
          security.check(tools.REGISTRY["clear_marks"], {}).needs_confirm)

    print("\n盯梢的确认提示必须念得出那条命令")
    # 盯梢是"一次点头、反复执行"，提示里念不出命令等于用户批准了一个未知操作
    watch_question = tools.REGISTRY["start_watch"].confirm_question(
        {"kind": "命令", "target": "Get-Process | Remove-Item C:\\ -Recurse -Force",
         "interval_s": 3})
    check("命令类盯梢的提示里有命令要干什么",
          "命令" in watch_question and "一个文件" not in watch_question, watch_question[:60])

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
