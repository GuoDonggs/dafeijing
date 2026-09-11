# -*- coding: utf-8 -*-
"""子代理工具：把一件要跑好几步的事丢到后台去做。

真正干活的是 voice_agent/subagent.py，这里只做「参数校验 + 说人话」，
并且让工具层不必反过来 import 主程序（handler 由 Brain 注册进来）。
"""

from __future__ import annotations

__all__ = [
    "set_subagent_handler", "spawn_subagent_tool", "subagent_status_tool",
    "cancel_subagent_tool",
]

# 用列表而不是模块级变量装 handler：赋值语句在函数里必须带 global，
# 少写一次就会变成"设了个局部变量"，症状是工具永远说"还没启动"。
_SUBAGENT_HANDLER: list = [None]


def set_subagent_handler(handler) -> None:
    """注册子代理管理器（Brain 启动时接上）。"""
    _SUBAGENT_HANDLER[0] = handler


def _manager():
    return _SUBAGENT_HANDLER[0]


def _missing() -> str:
    return "现在派不了子代理：后台任务管理器还没启动。"


def spawn_subagent_tool(task: str = "", name: str = "") -> str:
    """把一件要多步才能做完的事交给后台子代理，立刻返回。"""
    text = str(task or "").strip()
    if not text:
        return "没说要让子代理做什么。"
    manager = _manager()
    if manager is None:
        return _missing()
    if not getattr(manager, "enabled", False):
        return "子代理功能现在是关着的，要派的话先去设置里打开。"
    try:
        item = manager.spawn(text, str(name or "").strip())
    except ValueError as exc:
        return str(exc)
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
        return "派子代理失败：" + str(exc)[:80]
    return ("好，我让「" + item.name + "」去做这件事了，做完我告诉你。"
            "这期间你还可以继续吩咐我别的。")


def subagent_status_tool(name: str = "") -> str:
    """查子代理的进度：留空就是所有子代理。"""
    manager = _manager()
    if manager is None:
        return _missing()
    key = str(name or "").strip()
    if not key:
        return manager.describe()
    item = manager.get(key)
    if item is None:
        return "没有叫「" + key + "」的子代理。"
    return "「" + (item.name or item.id) + "」：" + item.summary()


def cancel_subagent_tool(name: str = "") -> str:
    """取消一个还在跑的子代理。"""
    manager = _manager()
    if manager is None:
        return _missing()
    key = str(name or "").strip()
    if not key:
        running = manager.running()
        if not running:
            return "现在没有在跑的子代理。"
        key = running[-1].id
    item = manager.get(key)
    if item is None:
        return "没有叫「" + key + "」的子代理。"
    if not manager.cancel(item.id):
        return "「" + (item.name or item.id) + "」已经不在跑了。"
    return "好，「" + (item.name or item.id) + "」我让它停下了。"
