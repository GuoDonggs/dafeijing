# -*- coding: utf-8 -*-
"""控制"程序自己"的工具：开新会话、重启、退出。

和别的工具不一样，这三个动的是助手本身，所以真正的实现由 Agent 提供
（它知道怎么清上下文、怎么让界面去重启），这里只负责参数校验和说人话。
"""

from __future__ import annotations

__all__ = ["set_self_handler", "new_session_tool", "restart_self_tool", "quit_self_tool"]

_SELF_HANDLER: list = [None]


def set_self_handler(handler) -> None:
    """注册实现：handler(action, reason) -> 一句给人听的中文。"""
    _SELF_HANDLER[0] = handler


def _run(action: str, reason: str = "") -> str:
    handler = _SELF_HANDLER[0]
    if handler is None:
        return "现在控制不了程序本身（后台还没起来）"
    try:
        return str(handler(action, reason))
    except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
        return "这个操作没做成：" + str(exc)[:80]


def new_session_tool(reason: str = "") -> str:
    """开一个新会话：之前的上下文不再带进后面的对话。"""
    return _run("new_session", reason)


def restart_self_tool() -> str:
    """重启助手程序本身。"""
    return _run("restart")


def quit_self_tool() -> str:
    """退出助手程序。"""
    return _run("quit")
