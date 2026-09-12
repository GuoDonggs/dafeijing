# -*- coding: utf-8 -*-
"""验证「LLM + 工具调用」这条主链路，用一个本地假模型服务代替真 API。

为什么要有这个测试：真实 LLM 调用需要 API Key 和网络，CI 里不稳定；
但「模型要求调工具 → 我们执行 → 结果回灌 → 模型总结」这套循环是核心逻辑，
必须能离线验证。这里起一个 127.0.0.1 上的最小 /v1/chat/completions 服务，
按剧本依次返回 tool_calls 和最终答复。

运行：python tests/test_llm_loop.py
"""

from __future__ import annotations

import os
import tempfile
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时文件挪到临时目录：测试**绝不能**碰用户真实的对话记录 / 记忆 / 标记 ——
# 否则「上次聊过什么」会渗进断言（真出现过：webui 那句回复变成「跟刚才一样」）。
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name


from voice_agent import tools as tools_mod  # noqa: E402
from voice_agent.brain import Brain  # noqa: E402
from voice_agent.config import Config  # noqa: E402

SCRIPTED: list[dict] = []
REQUESTS: list[dict] = []
#: 假服务也像真接口那样校验消息序列。真实网关对不合法的序列回 400
#: （"An assistant message with 'tool_calls' must be followed by tool messages"），
#: 这里把它复刻出来 —— 这样"收尾总结 400"这类 bug 在离线测试里就会当场暴露。
PROBLEMS: list[str] = []


def messages_problem(messages: list[dict]) -> str:
    """assistant 声明了几个 tool_calls，后面就得跟几条 tool 回复。"""
    pending: list[str] = []
    for message in messages or []:
        role = message.get("role")
        if role == "assistant" and message.get("tool_calls"):
            if pending:
                return "上一批 tool_calls 没答复完：" + str(pending)
            pending = [str(c.get("id") or (c.get("function") or {}).get("name"))
                       for c in message["tool_calls"]]
        elif role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id in pending:
                pending.remove(call_id)
            elif not message.get("name"):
                return "tool 消息缺少 tool_call_id 和 name"
        elif role in ("user", "system") and pending:
            return "assistant 的 tool_calls 后面少了 tool 回复：" + str(pending)
    return ("末尾还有没答复的 tool_calls：" + str(pending)) if pending else ""


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(payload)
        problem = messages_problem(payload.get("messages") or [])
        if problem:
            body = json.dumps({"error": {"message":
                               "An assistant message with 'tool_calls' must be followed by "
                               "tool messages responding to each tool_call_id. " + problem}},
                              ensure_ascii=False).encode("utf-8")
            PROBLEMS.append(problem)
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        scripted = SCRIPTED.pop(0) if SCRIPTED else {"content": "（剧本用完了）"}
        body = json.dumps(
            {"choices": [{"index": 0, "message": scripted}], "usage": {}}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静音
        return


def tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("假模型服务：http://127.0.0.1:{}/v1".format(port))

    cfg = Config.load()
    cfg.llm.enabled = True
    cfg.llm.base_url = "http://127.0.0.1:{}/v1".format(port)
    cfg.llm.api_key = "test-key"
    cfg.llm.model = "mock-model"

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
        if not ok:
            failures.append(name)

    # ── 场景 1：模型先要工具，再总结 ────────────────────────────────
    REQUESTS.clear()
    SCRIPTED.clear()
    SCRIPTED.append(tool_call("system_info", {"kind": "D盘"}))
    SCRIPTED.append({"role": "assistant", "content": "D盘还剩 144 G，用了八成。"})

    brain = Brain(cfg, log=lambda _m: None)
    check("LLM 客户端初始化", brain.llm is not None, brain.mode)
    reply = brain.respond("D盘还剩多少空间")
    check("工具调用循环得到最终回复", reply == "D盘还剩 144 G，用了八成。", repr(reply))
    check("一共请求了模型两次", len(REQUESTS) == 2, "实际 " + str(len(REQUESTS)))

    if len(REQUESTS) == 2:
        with_tools = REQUESTS[0]
        check("请求里带了工具声明", bool(with_tools.get("tools")), str(len(with_tools.get("tools") or [])) + " 个工具")
        names = [t["function"]["name"] for t in (with_tools.get("tools") or [])]
        check("工具声明包含 system_info", "system_info" in names)
        second = REQUESTS[1]["messages"]
        tool_messages = [m for m in second if m.get("role") == "tool"]
        check("第二次请求带回了工具结果", bool(tool_messages), (tool_messages[0]["content"][:40] if tool_messages else ""))
        check(
            "工具结果真的执行了",
            bool(tool_messages) and "盘" in tool_messages[0]["content"],
            tool_messages[0]["content"][:60] if tool_messages else "",
        )

    # ── 场景 2：确认提问的语义判定 ─────────────────────────────────
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "是"})
    check("确认语义判定＝同意", brain.judge("要关机，确认吗？", "行，你弄吧") is True)
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "否"})
    check("确认语义判定＝拒绝", brain.judge("要关机，确认吗？", "先别动") is False)

    # ── 场景 3：模型服务挂掉 → 自动降级到离线规则 ────────────────────
    server.shutdown()
    server.server_close()
    degraded = Brain(cfg, log=lambda _m: None)
    reply = degraded.respond("现在几点了")
    check("模型不可用时降级到离线规则", "现在" in reply, repr(reply[:40]))

    # ── 场景 4：已经调过工具之后模型断线 → 不允许再跑一遍 ──────────────
    # 这是安全要求：否则「关机 / 执行命令」这类指令会被执行两次。
    SCRIPTED.clear()
    REQUESTS.clear()
    counter = {"n": 0}

    class FlakyHandler(Handler):
        def do_POST(self) -> None:  # noqa: N802
            counter["n"] += 1
            if counter["n"] == 1:
                return super().do_POST()          # 第一次：正常返回 tool_calls
            self.send_response(500)               # 第二次：模型挂了
            self.send_header("Content-Length", "0")
            self.end_headers()

    flaky = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
    threading.Thread(target=flaky.serve_forever, daemon=True).start()
    cfg.llm.base_url = "http://127.0.0.1:" + str(flaky.server_address[1]) + "/v1"
    tools_calls: list[str] = []

    # 大脑走的是 call_result（带 ok 标记的那个），所以要拦它而不是 call
    original_call = tools_mod.call_result

    # 签名要和真的 call_result 一致（多出来的 cancel_check 是长工具"半路收手"用的）
    def spy(name, arguments=None, on_confirm=None, cancel_check=None):  # noqa: ANN001
        tools_calls.append(name)
        return tools_mod.ToolResult("工具已执行")

    tools_mod.call_result = spy
    try:
        SCRIPTED.append(tool_call("get_time", {}))
        SCRIPTED.append({"role": "assistant", "content": "不该走到这里"})
        retry_brain = Brain(cfg, log=lambda _m: None)
        reply = retry_brain.respond("现在几点了")
    finally:
        tools_mod.call_result = original_call
        flaky.shutdown()
        flaky.server_close()

    check("工具执行后断线：只调用一次工具", tools_calls == ["get_time"], str(tools_calls))
    check("工具执行后断线：不回退重跑", "不再重试" in reply, repr(reply[:40]))

    # ── 场景 5：模型绕圈（同一套参数连调三次）→ 收尾那次请求必须合法 ──────
    # 用户真遇到过：绕圈收尾时发出去的消息里，assistant 声明了 tool_calls
    # 却没有对应的 tool 回复，网关直接回 400，日志里是
    # "收尾总结也失败了：模型返回 400：…must be followed…"。
    PROBLEMS.clear()
    SCRIPTED.clear()
    REQUESTS.clear()
    loop_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=loop_server.serve_forever, daemon=True).start()
    cfg.llm.base_url = "http://127.0.0.1:" + str(loop_server.server_address[1]) + "/v1"
    for _ in range(3):
        SCRIPTED.append(tool_call("get_time", {}))
    SCRIPTED.append({"role": "assistant", "content": "我先说这些，剩下的还没做。"})
    try:
        loop_brain = Brain(cfg, log=lambda _m: None)
        reply = loop_brain.respond("现在几点了")
    finally:
        loop_server.shutdown()
        loop_server.server_close()
    check("同样参数连调三次会判定绕圈并收尾", "卡" in reply, repr(reply[:50]))
    check("收尾那次请求是合法的（每个 tool_call 都有 tool 回复）",
          PROBLEMS == [], str(PROBLEMS))
    check("收尾拿到了模型的话，而不是那句兜底",
          "我先说这些" in reply, repr(reply[:50]))

    # ── 场景 6：用户打断 → 不用等模型把这一轮跑完 ────────────────────
    # 以前喊一声"停"，这一轮还得干等 timeout_s（默认 30 秒）才轮到新任务 ——
    # 用户看到的是"它说停下了，可还在跑上一个任务"。
    class SlowHandler(Handler):
        def do_POST(self) -> None:  # noqa: N802
            time.sleep(3.0)
            return super().do_POST()

    slow = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=slow.serve_forever, daemon=True).start()
    cfg.llm.base_url = "http://127.0.0.1:" + str(slow.server_address[1]) + "/v1"
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "不该等到我"})
    PROBLEMS.clear()
    interrupt = threading.Event()
    threading.Timer(0.4, interrupt.set).start()
    started = time.time()
    try:
        slow_brain = Brain(cfg, log=lambda _m: None)
        reply = slow_brain.respond("现在几点了", interrupt=interrupt)
    finally:
        slow.shutdown()
        slow.server_close()
    waited = time.time() - started
    check("打断之后立刻返回（不用等这一轮请求跑完）", waited < 2.0, "%.2fs" % waited)
    check("打断返回空回复，交给新的任务", reply == "", repr(reply))

    check("整个过程里没有任何一次请求是不合法的",
          PROBLEMS == [], str(PROBLEMS))

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("LLM 工具调用链路全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
