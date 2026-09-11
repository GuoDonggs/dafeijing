# -*- coding: utf-8 -*-
"""鼠标、屏幕找图、图像缩放、应用映射表 —— 需要 Pillow / OpenCV 的那一批。

真正干活的是 voice_agent/screen.py，这里只做「参数校验 + 说人话」的包装。
缺依赖时不影响说话，只是调用它们会返回一句「缺什么」。
"""

from __future__ import annotations

import time

__all__ = [
    "mouse_position_tool", "mouse_move_tool", "mouse_click_tool", "mouse_drag_tool",
    "mouse_scroll_tool", "find_on_screen_tool", "click_image_tool",
    "resize_image_tool", "look_at_screen_tool", "app_map_tool", "set_vision_handler",
]

_VISION_HANDLER: list = [None]


def set_vision_handler(handler) -> None:
    """注册「看图回答问题」的实现（brain 负责接上多模型里的 vision 档案）。"""
    _VISION_HANDLER[0] = handler


def set_vision_handler(handler) -> None:
    """注册「看图回答问题」的实现（brain 负责接上多模型里的 vision 档案）。"""
    _VISION_HANDLER[0] = handler


def _screen():
    from .. import screen as screen_mod  # noqa: PLC0415

    return screen_mod


def mouse_position_tool() -> str:
    """报告鼠标当前坐标。"""
    try:
        x, y = _screen().mouse_position()
    except Exception as exc:  # noqa: BLE001
        return "读不到鼠标位置：" + str(exc)[:60]
    return "鼠标现在在 " + str(x) + "," + str(y)


def mouse_move_tool(x: int = 0, y: int = 0, duration_ms: int = 200) -> str:
    """把鼠标移到指定坐标。"""
    try:
        return _screen().mouse_move(int(x), int(y), int(duration_ms))
    except Exception as exc:  # noqa: BLE001
        return "移动鼠标失败：" + str(exc)[:60]


def mouse_click_tool(x: int | None = None, y: int | None = None, button: str = "left",
                     count: int = 1) -> str:
    """点击鼠标（可指定坐标与按键）。"""
    try:
        return _screen().mouse_click(x, y, button, count)
    except Exception as exc:  # noqa: BLE001
        return "点击失败：" + str(exc)[:60]


def mouse_drag_tool(x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                    button: str = "left") -> str:
    """按住鼠标拖拽。"""
    try:
        return _screen().mouse_drag(int(x1), int(y1), int(x2), int(y2), button)
    except Exception as exc:  # noqa: BLE001
        return "拖拽失败：" + str(exc)[:60]


def mouse_scroll_tool(amount: int = 3, horizontal: bool = False) -> str:
    """滚动鼠标滚轮。"""
    try:
        return _screen().mouse_scroll(int(amount), bool(horizontal))
    except Exception as exc:  # noqa: BLE001
        return "滚动失败：" + str(exc)[:60]


def find_on_screen_tool(image: str = "", confidence: float = 0.8) -> str:
    """在屏幕上找一张图，返回它的位置坐标。"""
    if not str(image).strip():
        return "没说要找哪张图片"
    try:
        hits = _screen().find_template(image, confidence=float(confidence))
    except FileNotFoundError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        return "找图失败：" + str(exc)[:80]
    if not hits:
        return "屏幕上没找到这张图（阈值 " + str(round(float(confidence), 2)) + "）"
    best = hits[0]
    extra = ("，另外还有 " + str(len(hits) - 1) + " 处相似位置") if len(hits) > 1 else ""
    return ("找到了，在屏幕 " + str(best["x"]) + "," + str(best["y"])
            + " 位置，相似度 " + str(round(best["score"] * 100)) + "%" + extra)


def click_image_tool(image: str = "", times: int = 1, interval_ms: int = 200,
                     confidence: float = 0.8, button: str = "left") -> str:
    """找到屏幕上的图片并点击它（连点器）。"""
    if not str(image).strip():
        return "没说要点击哪张图片"
    count = max(1, min(int(times or 1), 30))
    gap = max(60, int(interval_ms or 200))
    try:
        module = _screen()
        hits = module.find_template(image, confidence=float(confidence), limit=1)
        if not hits:
            return "屏幕上没找到这张图，没有点击"
        spot = hits[0]
        module.mouse_click(spot["x"], spot["y"], button, 1)
        for _ in range(count - 1):
            # 只定位一次：找图比点击慢得多，连点时重复找图纯属浪费
            time.sleep(gap / 1000.0)
            module.mouse_click(spot["x"], spot["y"], button, 1)
        return ("已经在 " + str(spot["x"]) + "," + str(spot["y"]) + " 点击 " + str(count)
                + " 次，间隔 " + str(gap) + " 毫秒（相似度 "
                + str(round(spot["score"] * 100)) + "%）")
    except Exception as exc:  # noqa: BLE001
        return "连点失败：" + str(exc)[:80]


def resize_image_tool(image: str = "", width: int = 0, height: int = 0,
                      out: str = "", quality: int = 80) -> str:
    """缩放图片到指定尺寸。"""
    if not str(image).strip():
        return "没说要缩放哪张图片"
    try:
        result = _screen().resize_image(image, width=int(width or 0), height=int(height or 0),
                                        out=out or None, quality=int(quality or 80))
    except Exception as exc:  # noqa: BLE001
        return "缩放失败：" + str(exc)[:80]
    saved = round(result["bytes"] / 1024, 1)
    return ("已经从 " + str(result["original"][0]) + "×" + str(result["original"][1])
            + " 缩到 " + str(result["size"][0]) + "×" + str(result["size"][1])
            + "，文件 " + str(saved) + " KB，存在 " + result["path"])


def look_at_screen_tool(question: str = "") -> str:
    """看屏幕：截图后交给视觉模型回答。"""
    handler = _VISION_HANDLER[0]
    if handler is None:
        return ("还没配置视觉模型。在 config.yaml 的 llm.profiles 里加一个 vision: true 的档案，"
                "再把 llm.routes.vision 指过去")
    try:
        path = _screen().save_for_vision(None, max_width=1280)
    except Exception as exc:  # noqa: BLE001
        return "截屏失败：" + str(exc)[:80]
    try:
        return handler(str(path), question or "屏幕上有什么？用一两句话说明关键内容")
    except Exception as exc:  # noqa: BLE001
        return "视觉模型调用失败：" + str(exc)[:100]


def app_map_tool(action: str = "list", name: str = "", target: str = "") -> str:
    """查看 / 添加 / 删除本地应用映射。"""
    module = _screen()
    what = (action or "list").strip().lower()
    if what in ("list", "列出", "查看"):
        return "当前的本地应用映射：" + module.describe_app_map()
    if what in ("add", "添加", "新增", "设置"):
        key = str(name or "").strip()
        value = str(target or "").strip()
        if not key or not value:
            return "要同时给出名字和目标，例如：把「我的项目」指向 D:\\code"
        mapping = module.load_app_map()
        mapping[key] = value
        module.save_app_map(mapping)
        return "好的，以后说「打开" + key + "」就打开 " + value
    if what in ("remove", "delete", "删除"):
        key = str(name or "").strip()
        mapping = module.load_app_map()
        for existing in list(mapping):
            if existing.lower() == key.lower():
                mapping.pop(existing)
                module.save_app_map(mapping)
                return "已经从映射表里删掉「" + existing + "」"
        return "映射表里没有「" + key + "」"
    return "不支持的 action：" + what + "（只支持 list / add / remove）"


_VISION_HANDLER: list = [None]

