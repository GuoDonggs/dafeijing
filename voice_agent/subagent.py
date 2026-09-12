# -*- coding: utf-8 -*-
"""子代理：把一件"要跑好几步"的事丢出去单独做，做完再回来汇报。

为什么语音助手需要它：

- 主对话的上下文很贵。让它去"查三样东西再汇总"，中间的工具结果会一直堆在
  历史里，之后每一轮都要重新付费，而且用户的耳朵在等；
- 语音是**单向**的：助手埋头干两分钟不说话，用户只会以为它死了。
  派个子代理去干，主对话可以立刻回一句"我让人去查了"，然后继续听下一句。

设计上照抄几个成熟 agent（DSH / CodeWhale）的做法：

1. **独立上下文**：子代理有自己的消息历史，跑完只把**结论**交回来，
   中间过程不进主对话；
2. **后台跑**：不阻塞主循环，用户可以接着说话；
3. **有步数上限**：免得它自己绕圈把额度烧光；
4. **默认不能碰敏感工具**：后台任务没人给它按"确认"，所以敏感操作直接拒绝，
   并在提示里告诉它别去碰。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import journal
from . import tools
from .config import Config
from .llm import Llm, LlmError

__all__ = ["SubAgent", "SubAgentManager"]

#: "不限制"时的硬上限。不是给正常任务用的，纯粹防止模型绕圈烧穿额度。
HARD_MAX_RUNNING = 20
HARD_MAX_ROUNDS = 60

# 这些工具在后台没有意义，甚至会污染主对话：
# - keep_listening 改的是「主对话这一轮要不要接着听」的全局状态；
# - spawn_subagent 是子代理再派子代理，没人管得住它们的数量。
_BLOCKED_TOOLS = ("keep_listening", "spawn_subagent")

_SUBAGENT_PROMPT = """你是一个子代理，被主助手派来做一件具体的事。

规则：
- 只做被交代的这件事，不要扩大范围；
- 需要真实数据（时间、磁盘、文件、命令输出、网页）时必须调用工具，禁止编造；
- 敏感操作（关机、执行任意命令等）在后台是**拿不到用户确认**的，会被直接拒绝，
  不要反复尝试，换一条不需要确认的路；
- 做完之后用**一到两句中文**说清结果，这句话会被直接念给用户听：
  不要 Markdown、不要列表、不要念路径和一长串 ID；
- 如果确实做不到，就直接说做不到以及卡在哪，不要含糊其辞。
"""


@dataclass
class SubAgent:
    """一个后台子任务的状态。"""

    id: str
    task: str
    name: str = ""
    state: str = "running"          # running / done / error / cancelled
    result: str = ""
    error: str = ""
    rounds: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    #: 叫停信号：用户打断时置位。它会传进模型调用里 —— 不然子代理正卡在
    #: 一次几十秒的 POST 上，喊停之后还要等那一轮跑完才理你。
    stop_event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def seconds(self) -> float:
        end = self.finished or time.time()
        return round(end - self.started, 1)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "task": self.task, "name": self.name or self.id,
            "state": self.state, "result": self.result, "error": self.error,
            "rounds": self.rounds, "seconds": self.seconds,
        }

    def summary(self) -> str:
        """一句话状态，给工具返回值和播报用。"""
        if self.state == "running":
            return "「" + (self.name or self.id) + "」还在做（已 " + str(self.seconds) + " 秒）"
        if self.state == "done":
            return self.result or "做完了，但没什么要说的"
        if self.state == "cancelled":
            return "「" + (self.name or self.id) + "」已经取消"
        return "「" + (self.name or self.id) + "」没做成：" + (self.error or "未知原因")


class SubAgentManager:
    """管理所有后台子代理。线程安全，最多同时跑 N 个。"""

    def __init__(self, cfg: Config, log: Callable[[str], None] = print,
                 on_done: Callable[[SubAgent], None] | None = None) -> None:
        self.cfg = cfg
        self.log = journal.adapt(log)
        self.on_done = on_done
        self._items: dict[str, SubAgent] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._counter = 0

    # ── 对外 ──
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.agent.subagent_enabled)

    def max_running(self) -> int:
        """同时最多几个子代理。配 0 = 不限制，但仍然有个硬上限兜底 ——
        模型一旦绕起圈来，"不限制"就是把自己拖死的开关。"""
        try:
            want = int(self.cfg.agent.subagent_max)
        except (TypeError, ValueError):
            want = HARD_MAX_RUNNING
        return want if want > 0 else HARD_MAX_RUNNING

    def max_rounds(self) -> int:
        """每个子代理最多几步。0 = 不限制（同样有硬上限）。"""
        try:
            want = int(self.cfg.agent.subagent_rounds)
        except (TypeError, ValueError):
            want = HARD_MAX_ROUNDS
        return want if want > 0 else HARD_MAX_ROUNDS

    def spawn(self, task: str, name: str = "") -> SubAgent:
        """派一个子代理。返回它的状态对象（state=running）。"""
        text = (task or "").strip()
        if not text:
            raise ValueError("没说要让子代理做什么")
        with self._lock:
            running = sum(1 for item in self._items.values() if item.state == "running")
            limit = self.max_running()
            if running >= limit:
                raise RuntimeError(
                    ("同时最多 " + str(limit) + " 个子代理在跑，等一个做完再派")
                    if limit < HARD_MAX_RUNNING else
                    ("同时在跑的子代理太多了（" + str(limit) + " 个），先等几个做完"))
            self._counter += 1
            item = SubAgent(id="sub" + str(self._counter), task=text,
                            name=(name or "").strip() or ("子代理" + str(self._counter)))
            self._items[item.id] = item
            self._order.append(item.id)
        self.log("[subagent] 派出 " + item.name + "：" + text[:60])
        threading.Thread(target=self._run, args=(item,),
                         name="subagent-" + item.id, daemon=True).start()
        return item

    def get(self, key: str) -> SubAgent | None:
        with self._lock:
            item = self._items.get(str(key or "").strip())
            if item is not None:
                return item
            for candidate in self._items.values():
                if candidate.name == key:
                    return candidate
        return None

    def all(self) -> list[SubAgent]:
        with self._lock:
            return [self._items[key] for key in self._order]

    def running(self) -> list[SubAgent]:
        return [item for item in self.all() if item.state == "running"]

    def finished_since(self, stamp: float) -> list[SubAgent]:
        return [item for item in self.all()
                if item.finished and item.finished > stamp and item.state == "done"]

    def cancel(self, key: str) -> bool:
        item = self.get(key)
        if item is None or item.state != "running":
            return False
        item.stop_event.set()
        item.state = "cancelled"
        item.finished = time.time()
        self.log("[subagent] 取消了 " + item.name)
        return True

    def cancel_all(self, reason: str = "") -> int:
        """叫停**所有**还在跑的子代理，返回停了几个。

        用户打断的时候用它：子代理是独立线程，主对话"停"了它照样一步接一步地
        调工具、烧 token、占着模型 —— 用户看到的正是"它说停下了，可日志里
        还在跑上一个任务"。定时盯梢（watch）不在这里面：那是用户明确让它长期
        盯着的事，要停得说「别盯了」。
        """
        stopped = 0
        for item in self.all():
            if item.state != "running":
                continue
            item.stop_event.set()
            item.state = "cancelled"
            item.finished = time.time()
            stopped += 1
        if stopped:
            self.log("[subagent] 叫停了 " + str(stopped) + " 个还在跑的子代理"
                     + ("（" + str(reason) + "）" if reason else ""))
        return stopped

    def clear_finished(self) -> int:
        with self._lock:
            done = [key for key, item in self._items.items() if item.state != "running"]
            for key in done:
                self._items.pop(key, None)
                if key in self._order:
                    self._order.remove(key)
        return len(done)

    def snapshot(self) -> dict:
        """给界面看的一行摘要（每几百毫秒被调一次，必须很轻）。"""
        items = self.all()
        running = [item for item in items if item.state == "running"]
        return {
            "enabled": self.enabled,
            "total": len(items),
            "running": len(running),
            "text": running[-1].summary() if running else "",
        }

    def describe(self) -> str:
        items = self.all()
        if not items:
            return "现在没有子代理"
        return "；".join(item.name + "：" + item.summary() for item in items[-5:])

    # ── 内部 ──
    def _client(self) -> Llm | None:
        """子代理用哪个模型：配了 subagent 路由就用它，否则跟主对话同一个。"""
        for purpose in ("subagent", "chat"):
            resolved = self.cfg.llm.resolve(purpose)
            if resolved.available:
                try:
                    return Llm(resolved)
                except LlmError as exc:
                    self.log("[subagent] 建客户端失败(" + purpose + ")：" + str(exc)[:80])
        return None

    def _run(self, item: SubAgent) -> None:
        """子代理的主循环：自己的消息历史，自己的工具循环。"""
        try:
            client = self._client()
            if client is None:
                raise RuntimeError("没有可用的模型（先配 llm.api_key）")
            rounds = self.max_rounds()
            messages: list[dict] = [
                {"role": "system", "content": _SUBAGENT_PROMPT},
                {"role": "user", "content": item.task},
            ]
            tool_schema = tools.openai_tools()
            for _round in range(rounds):
                if item.stop_event.is_set():
                    return
                item.rounds += 1
                started = time.time()
                message = client.chat(messages, tools=tool_schema,
                                      interrupt=item.stop_event)
                self.log("[subagent] " + item.name + " 第 " + str(item.rounds) + " 轮："
                         + "%.1fs" % (time.time() - started), "detail")
                calls = message.get("tool_calls") or []
                if not calls:
                    item.result = (message.get("content") or "").strip()
                    break
                messages.append({"role": "assistant",
                                 "content": message.get("content") or "",
                                 "tool_calls": calls})
                for call in calls:
                    if item.stop_event.is_set():
                        return
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    arguments = function.get("arguments") or "{}"
                    if name in _BLOCKED_TOOLS:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call.get("id") or name,
                            "name": name,
                            "content": "这个工具在后台子代理里不能用，请换一种做法。",
                        })
                        continue
                    self.log("[subagent] " + item.name + " 调用工具 " + name)
                    started = time.time()
                    # 后台没人给它按确认 —— 敏感工具一律拒绝，这是有意的
                    outcome = tools.call_result(name, arguments,
                                                on_confirm=lambda _q: False)
                    self.log("[subagent] " + item.name + " ← " + name + " "
                             + ("成功" if outcome.ok else "失败(" + str(outcome.code) + ")")
                             + " %.0fms｜%s" % ((time.time() - started) * 1000,
                                                str(outcome.text)[:160]), "detail")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "name": name,
                        "content": str(outcome.text)[:800],
                    })
            else:
                item.result = ("这件事步骤太多（已经做了 " + str(item.rounds)
                               + " 步），我先停下来，把已经查到的说一下。")
            if item.stop_event.is_set():
                return
            item.state = "done"
        except LlmError as exc:
            if getattr(exc, "interrupted", False):
                item.state = "cancelled"
                return
            item.state, item.error = "error", str(exc)[:160]
        except Exception as exc:  # noqa: BLE001 - 子代理崩了不能影响主循环
            item.state, item.error = "error", str(exc)[:160]
        finally:
            if item.state != "cancelled":
                item.finished = time.time()
            self.log("[subagent] " + item.name + " " + item.state
                     + "（" + str(round(item.seconds, 1)) + "s，"
                     + str(item.rounds) + " 轮）：" + (item.result or item.error)[:80])
            if item.state == "done" and self.on_done is not None:
                try:
                    self.on_done(item)
                except Exception as exc:  # noqa: BLE001
                    self.log("[subagent] 汇报失败：" + str(exc)[:80])
