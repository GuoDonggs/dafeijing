# -*- coding: utf-8 -*-
"""OpenAI 兼容的 LLM 客户端（带工具调用）。

只依赖 requests，不绑任何厂商 SDK：DeepSeek、OpenAI、硅基流动、通义、
本地 Ollama / vLLM / LM Studio 都走同一套 /chat/completions 协议，
换一行 base_url 即可。
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

import requests

from .config import LlmCfg

__all__ = ["Llm", "LlmError"]


class LlmError(RuntimeError):
    """网络、鉴权或返回格式出错；调用方据此降级到离线规则。"""

    def __init__(self, message: str, status: int | None = None,
                 interrupted: bool = False) -> None:
        super().__init__(message)
        self.status = status
        #: 是"用户把它打断了"，不是故障 —— 调用方不该记成错误、更不该降级到规则
        self.interrupted = bool(interrupted)

    @property
    def fatal(self) -> bool:
        """配置类错误（Key 不对、模型不存在）重试多少次都没用，直接放弃 LLM。"""
        return self.status in (401, 403, 404)


def _endpoint(base_url: str) -> str:
    """把各种写法的 base_url 补成完整的 chat/completions 地址。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LlmError("没有配置 llm.base_url")
    if base.endswith("/chat/completions"):
        return base
    if re.search(r"/v\d+$", base) or base.endswith("/api"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class Llm:
    """一次调用 = 一轮对话补全；工具循环留给 brain 去编排。"""

    def __init__(self, cfg: LlmCfg) -> None:
        self.cfg = cfg
        self.url = _endpoint(cfg.base_url)
        self.api_key = cfg.resolved_key()
        if not self.api_key:
            raise LlmError("没有 API Key")
        self._session = requests.Session()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.last_prompt_tokens = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             interrupt: Any = None) -> dict:
        """返回 assistant 消息（含可能的 tool_calls）。

        给了 interrupt（threading.Event）就**随时可以放弃等待**：用户喊一声"停"
        之后不用干等这一轮的超时，见 _post 里的说明。
        """
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        # 思考程度：只发各家公认的取值，其余情况宁可不发。
        # 严格一点的网关收到不认识的字段会直接 400，那比"思考关不掉"更糟。
        effort = (self.cfg.reasoning_effort or "").strip().lower()
        if effort in ("low", "medium", "high"):
            payload["reasoning_effort"] = effort
        elif effort == "max":
            payload["reasoning_effort"] = "high"
        # off / none / 默认：不发这个字段，由模型自己决定
        if self.cfg.extra_body:
            payload.update(self.cfg.extra_body)

        try:
            response = self._post(payload, interrupt)
        except requests.Timeout as exc:
            raise LlmError("模型响应超时") from exc
        except requests.RequestException as exc:
            raise LlmError("连不上模型服务：" + str(exc)[:80]) from exc

        if response.status_code != 200:
            detail = (response.text or "")[:160].replace("\n", " ")
            raise LlmError("模型返回 " + str(response.status_code) + "：" + detail,
                           status=response.status_code)

        try:
            data = response.json()
        except ValueError as exc:
            raise LlmError("模型返回的不是 JSON") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LlmError("模型没有返回结果")
        usage = data.get("usage") or {}
        self.calls += 1
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        # 缓存命中量：前缀（system + 工具声明）稳定时它会显著上升，
        # 既是省钱指标，也是"我有没有把时钟之类的易变内容塞进 system"的体检指标
        details = usage.get("prompt_tokens_details") or {}
        self.cached_tokens += int(details.get("cached_tokens") or 0)
        if usage.get("prompt_tokens"):
            self.last_prompt_tokens = int(usage["prompt_tokens"])
        message = choices[0].get("message") or {}
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": message.get("tool_calls") or [],
        }

    def _post(self, payload: dict, interrupt: Any = None):  # noqa: ANN401
        """发一次请求。给了 interrupt 就把它丢到辅助线程里等，随时能被叫走。

        为什么要这么绕：requests 的 POST 是**同步阻塞**的，一次最长等
        timeout_s（默认 30 秒）。用户喊"停"的时候，这一轮还在读 socket，
        新的任务得排到它后面 —— 日志里看到的就是"它说停下了，可还在跑上一个任务"。
        放在辅助线程里之后，主线程只等 interrupt：被打断就立刻抛
        LlmError(interrupted=True)，而那个请求自己在后台跑完就被丢掉
        （**它的 tool_calls 不会被执行**，所以不会留下任何副作用）。
        """
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        timeout = self.cfg.timeout_s
        if interrupt is None:
            return self._session.post(self.url, headers=headers, data=body, timeout=timeout)
        box: dict = {}

        def work() -> None:
            try:
                box["response"] = self._session.post(self.url, headers=headers,
                                                     data=body, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - 原样带回主线程再分类
                box["error"] = exc

        worker = threading.Thread(target=work, name="llm-post", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.1)
            if interrupt.is_set():
                raise LlmError("这一轮被打断了（请求已放弃）", interrupted=True)
        if "error" in box:
            raise box["error"]
        return box["response"]

    @property
    def stats(self) -> str:
        return "调用 {} 次，输入 {} tokens（缓存命中 {}），输出 {} tokens".format(
            self.calls, self.prompt_tokens, self.cached_tokens, self.completion_tokens
        )
