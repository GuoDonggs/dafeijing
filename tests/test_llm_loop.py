# -*- coding: utf-8 -*-
"""验证「LLM + 工具调用」这条主链路，用一个本地假模型服务代替真 API。

为什么要有这个测试：真实 LLM 调用需要 API Key 和网络，CI 里不稳定；
但「模型要求调工具 → 我们执行 → 结果回灌 → 模型总结」这套循环是核心逻辑，
必须能离线验证。这里起一个 127.0.0.1 上的最小 /v1/chat/completions 服务，
按剧本依次返回 tool_calls 和最终答复。

运行：python tests/test_llm_loop.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_agent import tools as tools_mod  # noqa: E402
from voice_agent.brain import Brain  # noqa: E402
from voice_agent.config import Config  # noqa: E402

SCRIPTED: list[dict] = []
REQUESTS: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(payload)
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

    def spy(name, arguments=None, on_confirm=None):  # noqa: ANN001
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

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("LLM 工具调用链路全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
