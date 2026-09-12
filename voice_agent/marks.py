# -*- coding: utf-8 -*-
"""屏幕上的标记：框选的「范围1/范围2」和点过的「点1/点2」。

为什么需要它：跟语音助手说话没法用手指。用户想说"看这一块""点这儿"时，
要么报坐标（人记不住），要么让模型去猜 —— 于是先让他**框一下**，
之后说「看看范围1」「把范围2 截下来」就行了。

这里只存数据（名字 → 几何），画出来是界面的事（ui/overlay.py）：
命令行里没有窗口，标记照样能用（截图裁剪认它），只是看不见而已。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Mark", "MarkStore", "store", "resolve_region",
           "REGION_PREFIX", "POINT_PREFIX"]

REGION_PREFIX = "范围"
POINT_PREFIX = "点"
_NAME_RE = re.compile(r"^(范围|区域|region|点|point)\s*(\d+)?$", re.IGNORECASE)


@dataclass
class Mark:
    """一个标记：矩形区域或一个点。"""

    name: str
    kind: str                 # region / point
    x1: int
    y1: int
    x2: int = 0
    y2: int = 0
    note: str = ""
    created: float = field(default_factory=lambda: __import__("time").time())

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """(left, top, right, bottom)，左上右下都排好序。"""
        if self.kind == "point":
            return self.x1, self.y1, self.x1, self.y1
        left, right = sorted((self.x1, self.x2))
        top, bottom = sorted((self.y1, self.y2))
        return left, top, right, bottom

    @property
    def width(self) -> int:
        left, _, right, _ = self.rect
        return max(1, right - left)

    @property
    def height(self) -> int:
        _, top, _, bottom = self.rect
        return max(1, bottom - top)

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.rect
        return (left + right) // 2, (top + bottom) // 2

    def as_dict(self) -> dict:
        left, top, right, bottom = self.rect
        return {"name": self.name, "kind": self.kind, "x1": left, "y1": top,
                "x2": right, "y2": bottom, "width": self.width, "height": self.height,
                "center": list(self.center), "note": self.note}

    def summary(self) -> str:
        if self.kind == "point":
            return self.name + " 在 " + str(self.x1) + "," + str(self.y1)
        return (self.name + " 是 " + str(self.width) + "×" + str(self.height)
                + " 的一块，左上角 " + str(self.rect[0]) + "," + str(self.rect[1])
                + "，中心 " + str(self.center[0]) + "," + str(self.center[1]))


class MarkStore:
    """标记的仓库。线程安全：工具在工作线程里加，界面在 UI 线程里画。"""

    def __init__(self) -> None:
        self._items: dict[str, Mark] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._version = 0        # 界面靠它判断"要不要重画"

    # ── 读写 ──
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def add_region(self, x1: int, y1: int, x2: int, y2: int,
                   name: str = "", note: str = "") -> Mark:
        return self._add("region", x1, y1, x2, y2, name, note)

    def add_point(self, x: int, y: int, name: str = "", note: str = "") -> Mark:
        return self._add("point", x, y, 0, 0, name, note)

    def add_or_update(self, kind: str, x1: int, y1: int, x2: int = 0, y2: int = 0,
                      name: str = "", note: str = "") -> tuple[Mark, bool]:
        """同名就**改**，不重名就新建。返回（标记, 是不是改的）。

        「把范围1 挪到 100,100 到 400,300」这种调整走这条路 ——
        用户说的是"改那块"，不是"再框一块"。
        """
        with self._lock:
            existing = self._items.get(str(name or "").strip())
        if existing is not None and existing.kind == kind:
            with self._lock:
                existing.x1, existing.y1 = int(x1), int(y1)
                if kind == "region":
                    existing.x2, existing.y2 = int(x2), int(y2)
                if note:
                    existing.note = str(note)[:60]
                self._version += 1
            return existing, True
        return self._add(kind, x1, y1, x2, y2, name, note), False

    def _add(self, kind: str, x1: int, y1: int, x2: int, y2: int,
             name: str, note: str) -> Mark:
        with self._lock:
            final = str(name or "").strip() or self._next_name(kind)
            if final in self._items:
                final = self._next_name(kind)
            mark = Mark(name=final, kind=kind, x1=int(x1), y1=int(y1),
                        x2=int(x2), y2=int(y2), note=str(note or "")[:60])
            self._items[final] = mark
            self._order.append(final)
            self._version += 1
            return mark

    def _next_name(self, kind: str) -> str:
        """范围1、范围2…（点也一样）。不跳号：用户看到的编号是连续的。"""
        prefix = REGION_PREFIX if kind == "region" else POINT_PREFIX
        used = [name for name in self._items if name.startswith(prefix)]
        index = len(used) + 1
        while prefix + str(index) in self._items:
            index += 1
        return prefix + str(index)

    def get(self, name: str) -> Mark | None:
        """按名字取。认「范围1」「范围 1」「region1」，也认不带序号的「范围」。"""
        with self._lock:
            items = dict(self._items)
            order = list(self._order)
        key = str(name or "").strip()
        if not key:
            return None
        if key in items:
            return items[key]
        match = _NAME_RE.match(key)
        if not match:
            return None
        prefix = REGION_PREFIX if match.group(1).lower() in ("范围", "区域", "region") else POINT_PREFIX
        index = match.group(2)
        if index:
            return items.get(prefix + str(int(index)))
        # 没写序号：给最近的那个
        for candidate in reversed(order):
            if candidate.startswith(prefix):
                return items[candidate]
        return None

    def remove(self, name: str) -> Mark | None:
        with self._lock:
            mark = self._items.pop(str(name or "").strip(), None)
            if mark is not None:
                self._order.remove(mark.name)
                self._version += 1
            return mark

    def clear(self, kind: str = "") -> int:
        with self._lock:
            if not kind:
                count = len(self._items)
                self._items.clear()
                self._order.clear()
            else:
                drop = [name for name, mark in self._items.items() if mark.kind == kind]
                for name in drop:
                    self._items.pop(name, None)
                    self._order.remove(name)
                count = len(drop)
            if count:
                self._version += 1
            return count

    def all(self) -> list[Mark]:
        with self._lock:
            return [self._items[name] for name in self._order]

    def regions(self) -> list[Mark]:
        return [mark for mark in self.all() if mark.kind == "region"]

    def describe(self) -> str:
        marks = self.all()
        if not marks:
            return "还没有标记。说「框一下这块」或者报个范围都行。"
        return "；".join(mark.summary() for mark in marks[-6:])


#: 全局一份：工具层和界面层都从这里读写
store = MarkStore()


def resolve_region(text: Any) -> tuple[int, int, int, int] | None:
    """把用户/模型给的东西解析成一个矩形。

    认这几种写法：
    - "范围1" / "点2"        —— 之前框选或点过的标记
    - "100,200,400,500"     —— 左,上,右,下 四个数
    - "100,200,300,200"     —— 也接受"左,上,宽,高"？不：一律按四点理解，
                               宽高那种写法容易和"右、下"混淆，宁可让模型说清楚。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    mark = store.get(raw)
    if mark is not None:
        if mark.kind == "point":
            # 一个点也给一小块，免得"截一个点"变成 1×1 的图
            x, y = mark.x1, mark.y1
            return x - 160, y - 120, x + 160, y + 120
        return mark.rect
    numbers = re.findall(r"-?\d+", raw)
    if len(numbers) >= 4:
        x1, y1, x2, y2 = (int(value) for value in numbers[:4])
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        if right - left >= 4 and bottom - top >= 4:
            return left, top, right, bottom
    return None
