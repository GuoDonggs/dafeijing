# -*- coding: utf-8 -*-
"""屏幕上的标记：框选的「范围1/范围2」和点过的「点1/点2」。

为什么需要它：跟语音助手说话没法用手指。用户想说"看这一块""点这儿"时，
要么报坐标（人记不住），要么让模型去猜 —— 于是先让他**框一下**，
之后说「看看范围1」「把范围2 截下来」就行了。

这里只存数据（名字 → 几何），画出来是界面的事（ui/overlay.py）：
命令行里没有窗口，标记照样能用（截图裁剪认它），只是看不见而已。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

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


def store_path() -> Path:
    """标记存哪（数据目录下）。"""
    return paths.sub("marks", create=True)


class MarkStore:
    """标记的仓库。线程安全：工具在工作线程里加，界面在 UI 线程里画。

    **会落盘**（build/marks.json）：框过的范围、标过的点，重启程序还在 ——
    "范围1 是我上次框的那个下载按钮"，这种记忆不该每次开机都重来一遍。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._items: dict[str, Mark] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        #: 屏幕上画不画这些标记。**只是显示开关**，跟"有没有标记"是两回事：
        #: 藏起来之后名字照样能引用（「点它」「看看范围1」照常有效），
        #: 只是不画在屏幕上 —— 框完一堆东西之后嫌挡视线时就靠它换个清净。
        self._visible = True
        #: 显式给了路径就固定用它；否则每次都现算 —— 用户改了数据目录之后，
        #: 标记要跟着写到新目录去（import 期算死的路径改不动）
        self._fixed_path = Path(path) if path else None
        self._version = 0        # 界面靠它判断"要不要重画"
        self._load()

    @property
    def path(self) -> Path:
        return self._fixed_path or store_path()

    def reload(self) -> None:
        """按当前数据目录重新读一遍（换了目录之后调用）。"""
        with self._lock:
            self._items.clear()
            self._order.clear()
            self._version += 1
        self._load()

    # ── 落盘 ──
    def _load(self) -> None:
        target = self.path
        if not target.is_file():
            return
        try:
            raw_text = target.read_text(encoding="utf-8")
            data = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # **不能静默返回**：手工编辑出错、或者上次写入被中断之后，
            # 用户会看到"标记全没了"，而程序一个字都不说；更糟的是下一次
            # 任何改动都会把这份还能救的文件覆盖掉。改成备份 + 说清楚。
            self._quarantine(target, exc)
            return
        if isinstance(data, dict) and "visible" in data:
            self._visible = bool(data.get("visible"))
        items = data.get("marks") if isinstance(data, dict) else data
        if not isinstance(items, list):
            self._quarantine(target, ValueError("顶层不是 marks 列表"))
            return
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            kind = str(raw.get("kind") or "region")
            if not name or kind not in ("region", "point"):
                continue
            try:
                mark = Mark(name=name, kind=kind, x1=int(raw.get("x1", 0)),
                            y1=int(raw.get("y1", 0)), x2=int(raw.get("x2", 0)),
                            y2=int(raw.get("y2", 0)), note=str(raw.get("note") or "")[:60])
            except (TypeError, ValueError):
                continue
            self._items[name] = mark
            self._order.append(name)

    @staticmethod
    def _quarantine(target: Path, exc: Exception) -> None:
        """把读不出来的文件改名成 .bad 留着，并说清楚发生了什么。"""
        try:
            backup = target.with_suffix(target.suffix + ".bad")
            target.replace(backup)
            where = "，原文件已改名保留为 " + backup.name
        except OSError:
            where = ""
        print("[marks] " + str(target) + " 读不出来（" + str(exc)[:60] + "）"
              + where + "；这次按「还没有标记」处理", file=sys.stderr, flush=True)

    def _save(self) -> None:
        """落盘。**先在锁里拍快照**，再原子替换。

        以前是直接在锁外迭代 self._items 和 self._order：rename() 会在锁内
        先 pop 再插入，正好卡在这个窗口的 _save 就撞 KeyError，而这里只
        except OSError —— 异常逃出去，标记只留在内存里、磁盘没写，工具那边
        还报一句莫名其妙的"出错了：'范围1'"。write_text 也不是原子的
        （先截断再写），两个线程同时写会把文件写坏。
        """
        try:
            with self._lock:
                marks = []
                visible = bool(self._visible)
                for key in list(self._order):
                    mark = self._items.get(key)
                    if mark is None:
                        continue
                    marks.append({"name": mark.name, "kind": mark.kind,
                                  "x1": mark.x1, "y1": mark.y1,
                                  "x2": mark.x2, "y2": mark.y2, "note": mark.note})
            target = self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "visible": visible, "marks": marks}
            temp = target.with_suffix(target.suffix + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
            os.replace(temp, target)      # 原子替换：中途崩了也不会留下半截文件
        except Exception:  # noqa: BLE001 - 存不下不该影响"框一下"这件事本身
            pass

    # ── 读写 ──
    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    # ── 显示开关 ──
    @property
    def visible(self) -> bool:
        """屏幕上画不画这些标记（和"有没有标记"无关）。"""
        with self._lock:
            return bool(self._visible)

    def set_visible(self, value: bool) -> bool:
        """开 / 关显示，返回设置后的状态。会落盘（下次开机保持）。"""
        with self._lock:
            changed = bool(self._visible) != bool(value)
            self._visible = bool(value)
            if changed:
                self._version += 1     # 界面靠它知道该重画了
                result = self._visible
            else:
                result = bool(self._visible)
        if changed:
            self._save()
        return result

    def toggle_visible(self) -> bool:
        """切换显示开关，返回切换后的状态。"""
        return self.set_visible(not self.visible)

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
                # 检查和改写必须在**同一个**临界区里：分两次加锁的话，
                # 中间被 remove() 摘掉时会去改一个已经不在表里的旧对象，
                # 还回一句"改好了" —— 而磁盘上根本没有它。
                existing.x1, existing.y1 = int(x1), int(y1)
                if kind == "region":
                    existing.x2, existing.y2 = int(x2), int(y2)
                if note:
                    existing.note = str(note)[:60]
                self._version += 1
                updated = True
            else:
                updated = False
        if updated:
            self._save()
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
        self._save()
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
        if mark is not None:
            self._save()
        return mark

    def rename(self, old: str, new: str) -> tuple[bool, str]:
        """改名。重名会被拒绝 —— 名字是引用它的唯一凭据，撞了就没法用了。"""
        source = str(old or "").strip()
        target = " ".join(str(new or "").split())
        if not target:
            return False, "名字不能空着"
        with self._lock:
            mark = self._items.get(source)
            if mark is None:
                return False, "没有叫「" + source + "」的标记"
            if target == source:
                return True, ""
            if target in self._items:
                return False, "已经有叫「" + target + "」的标记了"
            self._items.pop(source)
            mark.name = target
            self._items[target] = mark
            self._order[self._order.index(source)] = target
            self._version += 1
        self._save()
        return True, ""

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
        if count:
            self._save()
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
        text = "；".join(mark.summary() for mark in marks[-6:])
        if not self.visible:
            # 说清楚"没画出来 ≠ 没有"：不然模型/用户会以为标记丢了
            text += "（这些标记现在是**隐藏**的，屏幕上不显示，但名字照样能用；"                     "说「显示标记」就画回来）"
        return text


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
