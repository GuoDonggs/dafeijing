# -*- coding: utf-8 -*-
"""控制"程序自己"的工具：开新会话、重启、退出。

和别的工具不一样，这三个动的是助手本身，所以真正的实现由 Agent 提供
（它知道怎么清上下文、怎么让界面去重启），这里只负责参数校验和说人话。
"""

from __future__ import annotations

__all__ = ["set_self_handler", "new_session_tool", "restart_self_tool", "quit_self_tool",
           "permission_mode_tool"]

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


def permission_mode_tool(mode: str = "", reason: str = "") -> str:
    """查看或切换权限模式（只读 / 标准 / 放开）。

    放宽必须由用户本人确认 —— 工具本身带 confirm，确认提示里会原样带上
    模型给的理由（和 DSH 的 escalation 审计理由一个意思）。
    """
    from .. import security

    target = str(mode or "").strip().lower()
    snapshot = security.snapshot()
    if not target:
        return ("现在是「" + str(snapshot["label"]) + "」模式："
                + {"read-only": "只能查，写和操作都会被拒绝",
                   "workspace-write": "敏感操作会先问你一句",
                   "danger-full-access": "敏感操作直接做，但关机和执行命令仍要确认",
                   }.get(str(snapshot["mode"]), ""))
    was = security.mode()
    ok, message = security.set_mode(target, reason)
    if not ok:
        return message
    if was != security.mode() and str(reason or "").strip():
        return message + "（原因：" + str(reason)[:40] + "）"
    return message
