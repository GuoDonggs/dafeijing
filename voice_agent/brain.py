# -*- coding: utf-8 -*-
"""大脑：把一句「人话」变成一次工具调用，再变成一句「人话」。

两条路径，出口一样 —— 一句可以直接朗读的中文：

- LLM 模式：把工具声明交给模型，由它决定调什么、调几次，再总结成一句话；
- 离线规则模式：正则路由命中常用指令，直接执行；命中不了就明说没听懂。

这个模块还负责三件容易被忽略、但对语音助手特别重要的事：

1. **多模型**：主对话、判定同意与否、看图，可以分别挂不同的模型
   （见 config 的 llm.profiles / llm.routes），语音链路里每一秒沉默都是成本；
2. **前缀稳定**：system 提示里不放时间这类每分钟都变的内容，
   否则整段前缀（含二十多个工具声明）的缓存每轮都会失效；
3. **失败要说出来**：工具失败时先垫一句提醒再念结果，
   用户没有屏幕可看，听不出「命令其实没执行成功」。
"""

from __future__ import annotations

import base64
import platform
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import rules
from . import tools
from .config import Config
from .llm import Llm, LlmError

__all__ = ["Brain"]

_TOOL_HINT = """你可以调用本机工具来完成任务，规则：
- 先说结论再补一句细节，不要罗列步骤；
- 需要真实数据（时间、磁盘、内存、文件、命令输出）时必须调工具，禁止凭空编造；
- 一次能做完就不要分多轮；同一个工具连续失败两次就停下来说明原因；
- 工具返回的是已经整理好的中文结果，直接转述，不要重新格式化；
- 回复会被朗读：不要 Markdown、不要列表、不要念路径和一长串 ID。"""

# 工具结果进入模型上下文前的字符预算。
# 800 字大约对应一两句话的朗读量，再长用户也听不完，却要按最多 6 轮重复付费。
RESULT_BUDGET = 800
HISTORY_BUDGET = 4000


class Brain:
    def __init__(self, cfg: Config, log: Callable[[str], None] = print) -> None:
        self.cfg = cfg
        self.log = log
        self.history: list[dict] = []
        self.last_error = ""
        self.llm: Llm | None = None
        # 本轮是否已经真的动过工具。模型中途断线时靠它决定「能不能退回规则重试」：
        # 已经执行过的指令再走一遍规则，等于把关机、执行命令这类操作做两次。
        self.used_tools = False
        self._llm_backoff_until = 0.0
        self._clients: dict[str, Llm | None] = {}
        self._prompt_cache: str | None = None
        self._clock_stamp = ""

        tools.set_vision_handler(self._answer_with_vision)
        self._clients["chat"] = self._build_client("chat")
        self.llm = self._clients["chat"]

    # -- 客户端 -----------------------------------------------------------
    def _build_client(self, purpose: str) -> Llm | None:
        resolved = self.cfg.llm.resolve(purpose)
        if not resolved.available:
            if purpose == "chat":
                self.last_error = "没有配置 API Key"
            return None
        try:
            return Llm(resolved)
        except LlmError as exc:
            self.last_error = str(exc)
            return None

    def _client(self, purpose: str) -> Llm | None:
        """取某个用途的客户端；没配就回落到主对话模型，再不行返回 None。

        「判定同意与否」这种小问题单独走一个便宜/不开思考的模型，
        否则它会在用户已经说了「行」之后，再插进一段无谓的沉默。
        """
        if purpose not in self._clients:
            self._clients[purpose] = self._build_client(purpose) or self._clients.get("chat")
        return self._clients[purpose]

    # -- 状态 -------------------------------------------------------------
    @property
    def mode(self) -> str:
        if self.llm is None:
            return "离线规则"
        chat = self.cfg.llm.resolve("chat")
        effort = chat.reasoning_effort or "默认"
        extras = [name for name in ("judge", "vision") if self._client_is_separate(name)]
        text = "LLM " + chat.model + "（思考 " + effort + "）"
        if extras:
            text += " +" + "/".join(extras)
        return text

    def _client_is_separate(self, purpose: str) -> bool:
        """这个用途是不是真的挂了另一个模型（而不是回落到主模型）。"""
        name = str((self.cfg.llm.routes or {}).get(purpose) or "")
        if not name or name == str((self.cfg.llm.routes or {}).get("chat") or ""):
            return False
        profile = (self.cfg.llm.profiles or {}).get(name)
        return bool(isinstance(profile, dict) and (profile.get("model") or profile.get("base_url")))

    def _stable_prompt(self) -> str:
        """system 提示里只放不变的内容，让服务端的前缀缓存能一直命中。

        以前这里拼了 datetime.now()，于是每分钟整个前缀（包括二十多个工具声明）
        都会失效一次，而一轮对话可能调用模型 6 次。时间改成跟在用户消息后面单独发。
        """
        if self._prompt_cache is None:
            self._prompt_cache = (
                self.cfg.agent.persona.strip()
                + "\n\n"
                + _TOOL_HINT
                + "\n\n当前环境："
                + platform.system() + " " + platform.release()
                + "，主机名 " + platform.node()
                + "，用户目录 " + str(Path.home())
                + "。需要知道当前时间就调用 get_time 工具，不要猜。"
            )
        return self._prompt_cache

    def _clock_line(self) -> str:
        """时间另发一条，而且只有跨分钟才重新发。"""
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        if stamp == self._clock_stamp:
            return ""
        self._clock_stamp = stamp
        return ("当前时间 " + stamp + "（周" + "一二三四五六日"[now.weekday()] + "）。")

    def system_prompt(self) -> str:
        """给外部看的完整提示（含时间）。"""
        line = self._clock_line()
        return self._stable_prompt() + (("\n" + line) if line else "")

    # -- 对外接口 ---------------------------------------------------------
    def respond(
        self,
        text: str,
        confirm: Callable[[str], bool] | None = None,
        interrupt: threading.Event | None = None,
    ) -> str:
        """处理一句用户指令，返回要朗读的回复（可能为空字符串）。"""
        user_text = (text or "").strip()
        if not user_text:
            return "我没听清，再说一遍好吗？"
        self.used_tools = False
        if self.llm is not None and time.monotonic() >= self._llm_backoff_until:
            try:
                return self._respond_llm(user_text, confirm, interrupt)
            except LlmError as exc:
                self.last_error = str(exc)
                if exc.fatal:
                    # Key 不对、模型不存在这类问题，重试也不会好
                    self.llm = None
                    self._clients["chat"] = None
                    self.log("[brain] 模型配置有问题（" + str(exc) + "），已切到离线规则模式")
                else:
                    # 网络抖动：只退避一段时间，别把整场对话都变成离线的
                    self._llm_backoff_until = time.monotonic() + max(10.0, self.cfg.llm.timeout_s)
                    self.log("[brain] 模型暂时不可用（" + str(exc) + "），"
                             + str(int(max(10.0, self.cfg.llm.timeout_s))) + " 秒内先用离线规则")
                if self.used_tools:
                    # 关键：已经调用过工具了，绝不能再拿规则跑一遍
                    self.log("[brain] 本轮已经执行过工具，不重试，避免重复执行")
                    return "模型连接中断了，刚才可能已经执行了一半，我先停下不再重试。"
        return self._respond_rules(user_text, confirm)

    def judge(self, question: str, answer: str) -> bool | None:
        """判断用户对确认提问的回答是同意还是拒绝；模型不可用时返回 None。

        这一步发生在「用户已经说了『行』」之后、真正执行之前，
        所以走 judge 专用模型（默认不开思考），别让它变成一段尴尬的沉默。
        """
        client = self._client("judge")
        if client is None or not (answer or "").strip():
            return None
        try:
            message = client.chat(
                [
                    {
                        "role": "system",
                        "content": "用户被问到「" + question + "」，他的回答是「" + answer + "」。"
                        "请只回答一个字：同意就回「是」，拒绝或含糊就回「否」。不要解释。",
                    },
                    {"role": "user", "content": answer},
                ]
            )
        except LlmError:
            return None
        content = (message.get("content") or "").strip()
        if not content:
            return None
        return content.startswith("是") or content.lower().startswith("yes")

    # -- 看图 -------------------------------------------------------------
    def _answer_with_vision(self, image_path: str, question: str) -> str:
        """把一张图交给 vision 档案的模型，返回一句可以直接朗读的回答。"""
        client = self._client("vision")
        if client is None:
            return "没有配置可用的视觉模型"
        try:
            data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        except OSError as exc:
            return "读不到刚截的图：" + str(exc)[:60]
        try:
            message = client.chat([
                {
                    "role": "system",
                    "content": "你在看用户电脑屏幕的截图。用一到两句中文说清楚要点，"
                               "不要罗列、不要念坐标和文件名。",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url",
                         "image_url": {"url": "data:image/jpeg;base64," + data}},
                    ],
                },
            ])
        except LlmError as exc:
            return "视觉模型调用失败：" + str(exc)[:80]
        return (message.get("content") or "").strip() or "我看不出什么特别的。"

    # -- LLM 路径 ---------------------------------------------------------
    def _history_for_prompt(self) -> list[dict]:
        """按字符预算裁剪历史。

        比固定条数更贴近真实开销，又不必每轮跑分词器。语音助手的上下文很短，
        真正需要的是「上一两轮说了什么」，不是二十条记录。
        """
        picked: list[dict] = []
        used = 0
        for item in reversed(self.history):
            size = len(str(item.get("content") or ""))
            if picked and used + size > HISTORY_BUDGET:
                break
            used += size
            picked.append(item)
        picked.reverse()
        return picked

    @staticmethod
    def _cap_result(text: str, budget: int = RESULT_BUDGET) -> str:
        """把过长的工具结果压进预算，并**说清楚**丢了多少。

        含糊的「……后面还有」既骗模型也骗用户：用户听到一句笃定的回答，
        却不知道助手其实只看到了开头。压缩后反而更长时就保留原文
        （和成熟 agent 的溢出策略一致：能放进去的替换一定比原文短）。
        """
        value = str(text)
        if len(value) <= budget:
            return value
        head = int(budget * 0.7)
        tail = max(0, budget - head)
        dropped = len(value) - head - tail
        capped = (value[:head] + "……（这里略过 " + str(dropped) + " 个字）……"
                  + (value[-tail:] if tail else ""))
        return capped if len(capped) < len(value) else value

    def _respond_llm(
        self,
        user_text: str,
        confirm: Callable[[str], bool] | None,
        interrupt: threading.Event | None,
    ) -> str:
        client = self.llm
        assert client is not None
        messages: list[dict] = [{"role": "system", "content": self._stable_prompt()}]
        messages.extend(self._history_for_prompt())
        messages.append({"role": "user", "content": user_text})
        clock = self._clock_line()
        if clock:
            # 放在用户消息之后：前面整段（系统提示 + 工具声明 + 历史）保持字节稳定，
            # 服务端的前缀缓存才有机会命中
            messages.append({"role": "system", "content": clock})

        failures = 0
        for _round in range(max(1, self.cfg.llm.max_rounds)):
            if interrupt is not None and interrupt.is_set():
                return ""
            message = client.chat(messages, tools=tools.openai_tools())
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                if failures and reply:
                    # 用户没有屏幕可看：先把「没成功」说出来，再念结果
                    reply = "刚才有一步没成功，结果可能不准。" + reply
                self._remember(user_text, reply)
                return reply or "我做完了，但没什么要说的。"

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            })
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                arguments: Any = function.get("arguments") or "{}"
                self.log("[brain] 调用工具 " + name + " " + str(arguments))
                if interrupt is not None and interrupt.is_set():
                    return ""
                self.used_tools = True
                outcome = tools.call_result(name, arguments, on_confirm=confirm)
                if outcome.ok:
                    self.log("[brain] 工具返回 " + outcome.text[:120])
                else:
                    failures += 1
                    self.log("[brain] 工具失败(" + outcome.code + ")：" + outcome.text[:120])
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "name": name,
                    "content": self._cap_result(outcome.text),
                })

        self._remember(user_text, "（步骤太多，已停止）")
        return "这件事分了好几步还没做完，我先停下来。"

    def _remember(self, user_text: str, reply: str) -> None:
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self.history = self.history[-24:]

    def reset(self) -> None:
        self.history.clear()

    # -- 离线规则路径 -----------------------------------------------------
    def _respond_rules(self, user_text: str, confirm: Callable[[str], bool] | None) -> str:
        chit = rules.chitchat(user_text)
        if chit:
            return chit
        routed = rules.route(user_text)
        if routed is None:
            return "这个我还没学会。" + rules.help_text()
        name, arguments = routed
        self.log("[brain] 规则命中 " + name + " " + str(arguments))
        self.used_tools = True
        outcome = tools.call_result(name, arguments, on_confirm=confirm)
        if outcome.ok:
            self.log("[brain] 工具返回 " + outcome.text[:120])
            return outcome.text
        self.log("[brain] 工具失败(" + outcome.code + ")：" + outcome.text[:120])
        return outcome.text
