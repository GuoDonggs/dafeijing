# -*- coding: utf-8 -*-
"""OpenAI 兼容的 LLM 客户端（带工具调用）。

只依赖 requests，不绑任何厂商 SDK：DeepSeek、OpenAI、硅基流动、通义、
本地 Ollama / vLLM / LM Studio 都走同一套 /chat/completions 协议，
换一行 base_url 即可。
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from .config import LlmCfg

__all__ = ["Llm", "LlmError"]


class LlmError(RuntimeError):
    """网络、鉴权或返回格式出错；调用方据此降级到离线规则。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

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

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """返回 assistant 消息（含可能的 tool_calls）。"""
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
            response = self._session.post(
                self.url,
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=self.cfg.timeout_s,
            )
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

    @property
    def stats(self) -> str:
        return "调用 {} 次，输入 {} tokens（缓存命中 {}），输出 {} tokens".format(
            self.calls, self.prompt_tokens, self.cached_tokens, self.completion_tokens
        )
