# -*- coding: utf-8 -*-
"""轮询工具：让助手盯着某件事，条件成立就主动汇报。

真正干活的是 voice_agent/watcher.py，这里只做参数校验和说人话。
"""

from __future__ import annotations

__all__ = ["set_watch_handler", "start_watch_tool", "list_watches_tool", "stop_watch_tool"]

_WATCH_HANDLER: list = [None]


def set_watch_handler(handler) -> None:
    """注册轮询管理器（Brain 启动时接上）。"""
    _WATCH_HANDLER[0] = handler


def _manager():
    return _WATCH_HANDLER[0]


def _missing() -> str:
    return "现在盯不了：轮询管理器还没启动。"


def start_watch_tool(kind: str = "", target: str = "", condition: str = "",
                     interval_s: float = 5.0, once: bool = True,
                     region: str = "", expect: str = "出现") -> str:
    """开始一项定时轮询。"""
    manager = _manager()
    if manager is None:
        return _missing()
    try:
        item = manager.start(kind, target, condition, interval_s=interval_s, once=once,
                             region=region, expect=expect)
    except ValueError as exc:
        return str(exc)
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
        return "开始轮询失败：" + str(exc)[:80]
    what = {"找图": ("在 " + item.region + " 里") if item.region else "屏幕上的那张图",
            "命令": "你交代的检查",
            "屏幕": "屏幕上的情况"}.get(item.kind, "这件事")
    return ("好，我每 " + str(item.interval_s) + " 秒看一眼" + what
            + "，一有结果就告诉你。这期间你还可以吩咐我别的。")


def list_watches_tool(watch_id: str = "") -> str:
    """查看轮询任务的进度。"""
    manager = _manager()
    if manager is None:
        return _missing()
    key = str(watch_id or "").strip()
    if not key:
        return manager.describe()
    item = manager.get(key)
    if item is None:
        return "没有叫「" + key + "」的轮询任务。"
    return item.id + "：" + item.summary()


def stop_watch_tool(watch_id: str = "") -> str:
    """停掉一个轮询任务（留空 = 全部停掉）。"""
    manager = _manager()
    if manager is None:
        return _missing()
    key = str(watch_id or "").strip()
    if not key:
        count = manager.stop_all()
        return "好，全停下了（" + str(count) + " 个）。" if count else "现在没有在盯的事情。"
    item = manager.get(key)
    if item is None:
        return "没有叫「" + key + "」的轮询任务。"
    if not manager.stop(item.id):
        return "「" + item.id + "」已经不在盯了。"
    return "好，「" + item.id + "」我停下了。"
