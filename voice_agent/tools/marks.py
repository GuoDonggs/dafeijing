# -*- coding: utf-8 -*-
"""标记工具：框一块、点一个、列出来、删掉。

语音场景里"用手指一下"这件事只能靠它：用户框一块，之后说「看看范围1」
「把范围2 截下来」，助手就知道说的是哪儿。

真正的存储和绘制在 voice_agent/marks.py（数据）和 ui/overlay.py（画面）。
没有界面时（命令行、后台）标记照样能用，只是看不见。
"""

from __future__ import annotations

__all__ = [
    "set_marks_handler", "mark_region_tool", "mark_point_tool",
    "list_marks_tool", "remove_mark_tool", "clear_marks_tool",
]

#: 界面注册的回调：("select_region"|"select_point", timeout) -> 用户选出来的几何
_MARKS_HANDLER: list = [None]


def set_marks_handler(handler) -> None:
    """注册「让用户现场框选 / 标点」的实现（PyQt 界面才有）。"""
    _MARKS_HANDLER[0] = handler


def _interactive(kind: str) -> dict | None:
    handler = _MARKS_HANDLER[0]
    if handler is None:
        return None
    try:
        return handler(kind)
    except Exception:  # noqa: BLE001 - 交互失败就当没框
        return None


def mark_region_tool(x1: int = 0, y1: int = 0, x2: int = 0, y2: int = 0,
                     name: str = "", note: str = "") -> str:
    """框一块区域。不给坐标就让用户在屏幕上拖一个框。"""
    from .. import marks as marks_mod

    # 任意一个数非 0 就当作"给了坐标"：只说「框住 100,200」时 x2/y2 是 0，
    # 以前这里会把整对坐标丢掉，转而去要求用户在屏幕上手动框 —— 明明报了坐标。
    if x1 or y1 or x2 or y2:
        mark, updated = marks_mod.store.add_or_update(
            "region", int(x1), int(y1), int(x2 or x1), int(y2 or y1),
            name=name, note=note)
        if updated:
            return "好，「" + mark.name + "」改成 " + mark.summary().split(" 是 ", 1)[-1] + "。"
        return ("好，这块记成「" + mark.name + "」了（" + mark.summary().split(" 是 ", 1)[-1]
                + "）。之后说「看看" + mark.name + "」就行。")
    picked = _interactive("region")
    if not picked:
        return ("要框哪一块？你可以直接说坐标（比如「框住 100,200 到 600,500」），"
                "或者在界面上用菜单里的「框选范围」拖一个框出来。")
    mark = marks_mod.store.add_region(picked["x1"], picked["y1"], picked["x2"], picked["y2"],
                                      name=name, note=note or "用户框选")
    return ("好，框好了，这块叫「" + mark.name + "」（"
            + str(mark.width) + "×" + str(mark.height) + "）。")


def mark_point_tool(x: int = 0, y: int = 0, name: str = "", note: str = "") -> str:
    """标一个点。不给坐标就让用户在屏幕上点一下。"""
    from .. import marks as marks_mod

    if x or y:
        mark, updated = marks_mod.store.add_or_update("point", int(x), int(y),
                                                      name=name, note=note)
        if updated:
            return "好，「" + mark.name + "」挪到 " + str(mark.x1) + "," + str(mark.y1) + " 了。"
        return "好，" + mark.summary() + "，我记成「" + mark.name + "」了。"
    picked = _interactive("point")
    if not picked:
        return ("要标哪个点？说坐标也行（比如「标在 800,450」），"
                "或者在界面上用菜单里的「标记点」点一下。")
    mark = marks_mod.store.add_point(picked["x"], picked["y"], name=name,
                                     note=note or "用户标点")
    return "好，" + mark.summary() + "，我记成「" + mark.name + "」了。"


def list_marks_tool() -> str:
    """列出所有标记。"""
    from .. import marks as marks_mod

    marks = marks_mod.store.all()
    if not marks:
        return "现在屏幕上没有任何标记。"
    return "现在有 " + str(len(marks)) + " 个标记：" + marks_mod.store.describe()


def remove_mark_tool(name: str = "") -> str:
    """删掉一个标记（留空 = 删掉最后一个）。"""
    from .. import marks as marks_mod

    key = str(name or "").strip()
    if not key:
        marks = marks_mod.store.all()
        if not marks:
            return "现在没有标记可删。"
        key = marks[-1].name
    mark = marks_mod.store.get(key)
    if mark is None:
        return "没有叫「" + key + "」的标记。"
    marks_mod.store.remove(mark.name)
    return "好，「" + mark.name + "」擦掉了。"


def clear_marks_tool(kind: str = "") -> str:
    """清掉所有标记（kind=区域 只清框，kind=点 只清点）。"""
    from .. import marks as marks_mod

    what = str(kind or "").strip().lower()
    # 只认得出这几种说法。**认不出来就说认不出来** —— 以前任何不认识的写法
    # （"全部"、"标记"、"范围1"）都会落到空串，而空串是"全清"：
    # 用户只想删一个标记，结果所有框和点都没了，还回一句"擦掉了 N 个"。
    if not what or what in ("全部", "所有", "all", "标记", "全部清掉", "都清掉"):
        target = ""
    elif what in ("区域", "范围", "region", "框", "框选", "框选范围"):
        target = "region"
    elif what in ("点", "point", "圆点", "标记点"):
        target = "point"
    else:
        got = marks_mod.store.describe()
        return ("分不清要清哪一种（" + str(kind) + "）。可以说「清掉所有标记」、"
                "「只清框」或者「只清点」。现在有的是：" + got)
    count = marks_mod.store.clear(target)
    if not count:
        return "本来就没有标记。"
    return "好，擦掉了 " + str(count) + " 个标记。"
