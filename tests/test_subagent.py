# -*- coding: utf-8 -*-
"""子代理：独立上下文、后台跑、敏感工具一律拒绝、做完主动汇报。

用一个本地假模型服务当后端，不碰真 API：
模型收到任务后按剧本调一次工具，再给一句结论。

运行：python tests/test_subagent.py
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
from voice_agent.agent import VoiceAgent  # noqa: E402
from voice_agent.config import Config  # noqa: E402
from voice_agent.subagent import SubAgent  # noqa: E402
from voice_agent.tools import subagents as subagents_mod  # noqa: E402

SCRIPTED: list[dict] = []
REQUESTS: list[dict] = []
GATE: threading.Event | None = None


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的接口
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        REQUESTS.append(payload)
        if GATE is not None:
            GATE.wait(timeout=10)
        scripted = SCRIPTED.pop(0) if SCRIPTED else {"role": "assistant", "content": "（剧本用完了）"}
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


def wait_state(item: SubAgent, state: str, timeout: float = 10.0) -> bool:
    """等子代理跑到某个状态（子代理在后台线程里）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if item.state == state:
            return True
        time.sleep(0.02)
    return item.state == state


def main() -> int:
    global GATE
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("假模型服务：http://127.0.0.1:{}/v1".format(server.server_address[1]))

    cfg = Config.load()
    cfg.llm.enabled = True
    cfg.llm.base_url = "http://127.0.0.1:{}/v1".format(server.server_address[1])
    cfg.llm.api_key = "test-key"
    cfg.llm.model = "mock-model"
    cfg.agent.subagent_enabled = True
    cfg.agent.subagent_max = 3
    cfg.agent.subagent_rounds = 4

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
        if not ok:
            failures.append(name)

    # 用 VoiceAgent 起步：这样工具处理器的注册路径（Brain → tools）也被覆盖到，
    # 而不是单独 new 一个 Manager —— 那样测不出「模型到底能不能调到这个工具」。
    agent = VoiceAgent(cfg, log=lambda _m: None)
    manager = agent.subagents
    check("Brain 启动时注册了子代理处理器", subagents_mod._SUBAGENT_HANDLER[0] is manager)
    check("工具表里有 spawn_subagent",
          any(t["function"]["name"] == "spawn_subagent" for t in tools_mod.openai_tools()))

    # ── 场景 1：派一个子代理，它自己调工具再给结论 ──────────────────
    SCRIPTED.clear()
    REQUESTS.clear()
    SCRIPTED.append(tool_call("get_time", {}))
    SCRIPTED.append({"role": "assistant", "content": "查到了，现在是下午三点。"})
    done: list[SubAgent] = []
    manager.on_done = done.append

    text = tools_mod.call("spawn_subagent", {"task": "看看现在几点", "name": "查时间"})
    check("派发返回一句人话", "查时间" in text and "做完" in text, text[:50])
    item = manager.all()[0]
    check("子代理立刻返回、状态是 running", item.state in ("running", "done"), item.state)
    check("子代理跑到完成", wait_state(item, "done"), item.state)
    check("结论被带回来了", item.result == "查到了，现在是下午三点。", repr(item.result))
    check("子代理用自己的上下文（2 次请求）", len(REQUESTS) == 2, str(len(REQUESTS)))
    if len(REQUESTS) == 2:
        first = REQUESTS[0]
        names = [t["function"]["name"] for t in (first.get("tools") or [])]
        check("子代理也能看到工具声明", "spawn_subagent" in names, str(len(names)) + " 个")
        check("子代理第一次请求只带自己的任务",
              len([m for m in first["messages"] if m.get("role") == "user"]) == 1)
        tool_msgs = [m for m in REQUESTS[1]["messages"] if m.get("role") == "tool"]
        check("工具结果回灌给了子代理", bool(tool_msgs),
              tool_msgs[0]["content"][:30] if tool_msgs else "")
    check("做完回调了 on_done", len(done) == 1 and done[0] is item, str(len(done)))
    check("on_done 里带着结论", bool(done) and "三点" in done[0].result)

    # ── 场景 2：进度查询 ──────────────────────────────────────────
    status = tools_mod.call("subagent_status", {})
    check("进度查询能报出结论", "三点" in status, status[:50])
    by_name = tools_mod.call("subagent_status", {"name": "查时间"})
    check("按名字也能查到", "三点" in by_name, by_name[:50])
    check("查不到的名字要说人话",
          "没有叫" in tools_mod.call("subagent_status", {"name": "不存在"}))

    # ── 场景 3：敏感工具在后台一律拒绝 ────────────────────────────
    SCRIPTED.clear()
    REQUESTS.clear()
    SCRIPTED.append(tool_call("power", {"action": "shutdown"}))
    SCRIPTED.append({"role": "assistant", "content": "关机这个我在后台做不了。"})
    spy_calls: list[str] = []
    spy_results: list = []
    original_call = tools_mod.call_result

    def spy(name, arguments=None, on_confirm=None, cancel_check=None):  # noqa: ANN001
        spy_calls.append(name)
        outcome = original_call(name, arguments, on_confirm)
        spy_results.append(outcome)
        return outcome

    tools_mod.call_result = spy
    try:
        tools_mod.call("spawn_subagent", {"task": "把电脑关了", "name": "关机"})
        item2 = manager.get("关机")
        assert item2 is not None
        wait_state(item2, "done")
    finally:
        tools_mod.call_result = original_call
    denied = [m for m in REQUESTS[1]["messages"] if m.get("role") == "tool"]
    check("后台调用敏感工具被拒绝（说清是没有确认通道，不是用户取消）",
          bool(denied) and tools_mod.NO_CHANNEL_REPLY in denied[0]["content"],
          denied[0]["content"][:40] if denied else "没有工具结果")
    # 注意 spy_calls 里还有派发本身那一次（spawn_subagent 也是工具）
    check("子代理真的走到工具层去调 power", "power" in spy_calls, str(spy_calls))
    check("拒绝发生在工具层，不是模型自己编的",
          bool(spy_results) and spy_results[-1].code == "denied",
          spy_results[-1].code if spy_results else "没有结果")

    # ── 场景 4：keep_listening / spawn_subagent 在后台不能用 ───────
    SCRIPTED.clear()
    REQUESTS.clear()
    tools_mod.reset_turn()
    SCRIPTED.append(tool_call("keep_listening", {"reason": "我还要说"}))
    SCRIPTED.append({"role": "assistant", "content": "好。"})
    tools_mod.call("spawn_subagent", {"task": "试探一下", "name": "试探"})
    item3 = manager.get("试探")
    assert item3 is not None
    wait_state(item3, "done")
    check("keep_listening 在后台不生效", tools_mod._shared.TURN["follow_up"] is False,
          str(tools_mod._shared.TURN))

    # ── 场景 5：并发上限 ─────────────────────────────────────────
    manager.clear_finished()
    cfg.agent.subagent_max = 1
    GATE = threading.Event()
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "慢慢来。"})
    tools_mod.call("spawn_subagent", {"task": "占住名额", "name": "占位"})
    blocked = tools_mod.call("spawn_subagent", {"task": "第二个", "name": "第二个"})
    check("超过并发上限时说明原因", "最多" in blocked, blocked[:50])
    snap = manager.snapshot()
    check("快照能报出正在跑的子代理",
          snap["running"] == 1 and "占位" in snap["text"], str(snap))
    check("引擎状态里也带着子代理摘要",
          (agent.status().get("subagents") or {}).get("running") == 1,
          str(agent.status().get("subagents")))
    GATE.set()
    GATE = None
    holder = manager.get("占位")
    assert holder is not None
    wait_state(holder, "done")
    cfg.agent.subagent_max = 3

    # ── 场景 6：取消 ────────────────────────────────────────────
    check("取消不存在的子代理要说人话",
          "没有叫" in tools_mod.call("cancel_subagent", {"name": "幽灵"}))
    check("已经没有在跑的了",
          "不在跑" in tools_mod.call("cancel_subagent", {"name": "占位"})
          or "没有在跑" in tools_mod.call("cancel_subagent", {}))

    # ── 场景 6b：用户打断 → 后台的子代理也得停 ──────────────────────
    # 用户的原话："我打断之后，它说停下了，可日志里还在跑上一个任务"。
    # 子代理是独立线程，主对话停它不停，必须显式叫停。
    manager.clear_finished()
    GATE = threading.Event()          # 关着门：请求会一直挂在那儿
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "这一步不该走到"})
    tools_mod.call("spawn_subagent", {"task": "一件要跑很久的事", "name": "长任务"})
    long_task = manager.get("长任务")
    assert long_task is not None
    time.sleep(0.3)
    check("子代理确实在跑（请求挂在假服务上）", long_task.state == "running", long_task.state)
    started = time.time()
    stopped = manager.cancel_all("用户打断")
    done_fast = wait_state(long_task, "cancelled", 3.0)
    spent = time.time() - started
    check("cancel_all 会叫停还在跑的子代理", stopped >= 1, str(stopped))
    check("叫停是**立刻**的：不用等那次请求超时", done_fast and spent < 3.0, "%.2fs" % spent)
    check("被打断的子代理不会给出结论", long_task.result == "" and long_task.state == "cancelled",
          long_task.state)
    GATE.set()                        # 放掉那个孤儿请求
    GATE = None

    # 主对话这边的入口：cancel() / 打断都要顺手叫停子代理
    GATE = threading.Event()
    SCRIPTED.clear()
    SCRIPTED.append({"role": "assistant", "content": "同样不该走到"})
    tools_mod.call("spawn_subagent", {"task": "又一件很久的事", "name": "第二件长任务"})
    second = manager.get("第二件长任务")
    assert second is not None
    time.sleep(0.3)
    agent.cancel()                    # 界面上的「打断」按钮走的就是它
    check("打断主对话会顺手叫停子代理",
          wait_state(second, "cancelled", 3.0), second.state)
    GATE.set()
    GATE = None
    manager.clear_finished()

    # ── 场景 7：关掉之后派不动 ────────────────────────────────────
    cfg.agent.subagent_enabled = False
    off = tools_mod.call("spawn_subagent", {"task": "随便做点什么"})
    check("关掉子代理后拒绝派发", "关着" in off, off[:40])
    cfg.agent.subagent_enabled = True
    check("没说要做什么时要问清楚",
          "没说要让子代理做什么" in tools_mod.call("spawn_subagent", {"task": "   "}))

    # ── 场景 8：主程序择机汇报，不在忙的时候插嘴 ────────────────────
    manager.clear_finished()
    spoken: list[str] = []
    agent.tts = None                      # 没有合成器：_speak 只会记日志
    agent.log = lambda m: spoken.append(m) if m.startswith("[agent] 汇报") else None
    report = SubAgent(id="sub9", task="x", name="查磁盘", state="done",
                      result="C 盘还剩 20 G。", finished=time.time())
    agent._on_subagent_done(report)
    check("汇报进了对话记录",
          any("查磁盘" in t["text"] for t in agent.transcript))
    check("汇报进了待播队列", len(agent._announce) == 1, str(len(agent._announce)))

    agent._state = "listen"
    agent._check_announce()
    check("正在听的时候不插嘴", len(agent._announce) == 1, str(len(agent._announce)))

    agent._state = "idle"
    agent._check_announce()
    deadline = time.time() + 2.0
    while time.time() < deadline and agent._announce:
        time.sleep(0.02)
    check("待命时把汇报念出来", not agent._announce, str(len(agent._announce)))
    check("汇报确实走的是播报路径", any("汇报" in line for line in spoken), str(spoken[:1]))

    # 配置为不播报时只记录不念
    agent.cfg.agent.subagent_announce = False
    agent._on_subagent_done(report)
    check("关掉播报后不再入队", len(agent._announce) == 0, str(len(agent._announce)))

    server.shutdown()
    server.server_close()

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("子代理全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
