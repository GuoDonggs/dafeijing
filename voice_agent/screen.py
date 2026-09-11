# -*- coding: utf-8 -*-
"""屏幕与鼠标：截图、找图、缩放、点按拖动，以及用户自定义的应用映射表。

放在单独一个模块里，是因为这些能力都依赖 Pillow / OpenCV，而核心的语音链路
不该被它们拖累 —— 没装也不影响助手说话。

坐标系统一用**屏幕绝对坐标**（左上角为原点），和用户看到的鼠标位置一致。
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import numpy as np

from .config import PROJECT_ROOT

__all__ = [
    "app_map_path", "load_app_map", "save_app_map", "resolve_app",
    "grab_screen", "find_template", "resize_image", "save_for_vision",
    "mouse_position", "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "open_cv_ready",
]

APP_MAP_FILE = PROJECT_ROOT / "apps.yaml"
VISION_CACHE = PROJECT_ROOT / "build" / "vision"

# ── Win32 鼠标事件 ───────────────────────────────────────────────
_MOVE = 0x0001
_LEFT_DOWN, _LEFT_UP = 0x0002, 0x0004
_RIGHT_DOWN, _RIGHT_UP = 0x0008, 0x0010
_MIDDLE_DOWN, _MIDDLE_UP = 0x0020, 0x0040
_WHEEL, _HWHEEL = 0x0800, 0x1000
_INPUT_MOUSE = 0
_KEYEVENTF_KEYUP = 0x0002


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("padding", ctypes.c_byte * 40))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("u", _INPUTUNION))


# 只有 Windows 有 user32；其它平台这些能力直接报「不支持」，不影响语音链路
_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None

_BUTTONS = {
    "left": (_LEFT_DOWN, _LEFT_UP),
    "right": (_RIGHT_DOWN, _RIGHT_UP),
    "middle": (_MIDDLE_DOWN, _MIDDLE_UP),
}


def _send_mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    if _user32 is None:
        raise RuntimeError("当前系统不支持鼠标模拟")
    item = _INPUT()
    item.type = _INPUT_MOUSE
    item.u.mi = _MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, None)
    _user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))


def mouse_position() -> tuple[int, int]:
    """当前鼠标位置。"""
    if _user32 is None:
        raise RuntimeError("当前系统不支持读取鼠标位置")
    point = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def mouse_move(x: int, y: int, duration_ms: int = 200) -> str:
    """把鼠标移到 (x, y)。

    分步移动而不是瞬移：很多界面（拖拽、悬停菜单）需要中间的移动事件才会响应。
    """
    start_x, start_y = mouse_position()
    steps = max(1, min(40, int(duration_ms) // 15))
    for index in range(1, steps + 1):
        nx = int(start_x + (int(x) - start_x) * index / steps)
        ny = int(start_y + (int(y) - start_y) * index / steps)
        _send_mouse(_MOVE, nx, ny)
        time.sleep(max(0.0, duration_ms / 1000.0 / steps))
    return "鼠标已经移到 " + str(int(x)) + "," + str(int(y))


def mouse_click(x: int | None = None, y: int | None = None, button: str = "left",
                count: int = 1, interval_ms: int = 80) -> str:
    """点击鼠标；给了坐标就先移过去。"""
    key = str(button or "left").strip().lower()
    if key not in _BUTTONS:
        return "不支持的鼠标键：" + str(button)
    if x is not None and y is not None:
        mouse_move(int(x), int(y), 160)
    down, up = _BUTTONS[key]
    times = max(1, min(int(count or 1), 5))
    for _ in range(times):
        _send_mouse(down)
        _send_mouse(up)
        time.sleep(max(0.03, int(interval_ms) / 1000.0))
    where = "" if x is None else "在 " + str(int(x)) + "," + str(int(y)) + " "
    names = {"left": "左键", "right": "右键", "middle": "中键"}
    return "已经" + where + names[key] + "点击 " + str(times) + " 次"


def mouse_drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
               duration_ms: int = 500) -> str:
    """按住鼠标从 (x1,y1) 拖到 (x2,y2)。"""
    key = str(button or "left").strip().lower()
    if key not in _BUTTONS:
        return "不支持的鼠标键：" + str(button)
    down, up = _BUTTONS[key]
    mouse_move(int(x1), int(y1), 200)
    _send_mouse(down)
    time.sleep(0.08)
    mouse_move(int(x2), int(y2), max(120, int(duration_ms)))
    time.sleep(0.08)
    _send_mouse(up)
    return "已经从 " + str(int(x1)) + "," + str(int(y1)) + " 拖到 " + str(int(x2)) + "," + str(int(y2))


def mouse_scroll(amount: int, horizontal: bool = False) -> str:
    """滚轮：正数向上/向右，负数向下/向左。一格 = 120。"""
    steps = int(amount or 0)
    if steps == 0:
        return "滚动量是 0，什么都没做"
    notches = max(-20, min(20, steps))
    _send_mouse(_HWHEEL if horizontal else _WHEEL, 0, 0, notches * 120)
    return ("已经向右滚 " if horizontal and notches > 0 else
            "已经向左滚 " if horizontal else
            "已经向上滚 " if notches > 0 else "已经向下滚 ") + str(abs(notches)) + " 格"


# ── 屏幕与图像 ───────────────────────────────────────────────────


def open_cv_ready() -> bool:
    try:
        import cv2  # noqa: F401,PLC0415

        return True
    except Exception:  # noqa: BLE001
        return False


def grab_screen(region: tuple[int, int, int, int] | None = None) -> np.ndarray:
    """截屏成 BGR 数组（OpenCV 习惯的顺序）。region 是 (left, top, right, bottom)。"""
    from PIL import ImageGrab  # noqa: PLC0415

    image = ImageGrab.grab(bbox=region, all_screens=True)
    return np.array(image.convert("RGB"))[:, :, ::-1].copy()


def find_template(image: str | Path, confidence: float = 0.8,
                  region: tuple[int, int, int, int] | None = None,
                  scales: tuple[float, ...] = (1.0, 0.9, 1.1, 0.8, 1.25),
                  limit: int = 5) -> list[dict]:
    """在屏幕上找一张小图，返回 [(中心x, 中心y, 相似度, 缩放)]。

    多尺度匹配是为了容忍界面缩放（125% DPI、浏览器缩放）导致的尺寸差异；
    同一目标会在多个尺度上重复命中，所以最后要按重叠度去重。
    """
    import cv2  # noqa: PLC0415

    target = Path(image)
    if not target.is_file():
        raise FileNotFoundError("找不到图片：" + str(image))
    template = cv2.imread(str(target), cv2.IMREAD_COLOR)
    if template is None:
        raise ValueError("读不出这张图片：" + str(image))

    shot = grab_screen(region)
    offset_x = int(region[0]) if region else 0
    offset_y = int(region[1]) if region else 0
    shot_gray = cv2.cvtColor(shot, cv2.COLOR_BGR2GRAY)
    shot_h, shot_w = shot_gray.shape[:2]

    found: list[dict] = []
    for scale in scales:
        if scale <= 0:
            continue
        height = int(template.shape[0] * scale)
        width = int(template.shape[1] * scale)
        if height < 8 or width < 8 or height > shot_h or width > shot_w:
            continue
        scaled = cv2.resize(template, (width, height), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(shot_gray, cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY),
                                   cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= float(confidence))
        for x, y in zip(xs.tolist(), ys.tolist()):
            found.append({
                "x": int(x + width / 2 + offset_x),
                "y": int(y + height / 2 + offset_y),
                "score": float(result[y, x]),
                "scale": float(scale),
                "w": width, "h": height,
            })

    # 去重：重叠面积大的只留分最高的那个
    found.sort(key=lambda item: item["score"], reverse=True)
    kept: list[dict] = []
    for item in found:
        if any(abs(item["x"] - other["x"]) < max(item["w"], other["w"]) * 0.6
               and abs(item["y"] - other["y"]) < max(item["h"], other["h"]) * 0.6
               for other in kept):
            continue
        kept.append(item)
        if len(kept) >= max(1, int(limit)):
            break
    return kept


def resize_image(image: str | Path, width: int = 0, height: int = 0,
                 out: str | Path | None = None, quality: int = 80,
                 keep_ratio: bool = True) -> dict:
    """缩放图片。

    典型用途：截图动辄 1920×1080，直接丢给视觉模型很费 token；
    先缩到 1024 宽再送，token 能省一大半，识别效果通常几乎不变。
    """
    from PIL import Image  # noqa: PLC0415

    source = Path(image)
    if not source.is_file():
        raise FileNotFoundError("找不到图片：" + str(image))
    with Image.open(source) as img:
        img = img.convert("RGB")
        original = img.size
        target_w = int(width or 0)
        target_h = int(height or 0)
        if target_w <= 0 and target_h <= 0:
            raise ValueError("宽和高至少要给一个")
        if keep_ratio:
            if target_w <= 0:
                target_w = max(1, round(original[0] * target_h / original[1]))
            if target_h <= 0:
                target_h = max(1, round(original[1] * target_w / original[0]))
            ratio = min(target_w / original[0], target_h / original[1])
            target_w = max(1, round(original[0] * ratio))
            target_h = max(1, round(original[1] * ratio))
        resized = img.resize((target_w, target_h), Image.LANCZOS)

    destination = Path(out) if out else source.with_name(
        source.stem + "_" + str(target_w) + "x" + str(target_h) + ".jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        resized.save(destination, "JPEG", quality=max(30, min(int(quality), 95)), optimize=True)
    else:
        resized.save(destination, "PNG", optimize=True)
    return {
        "path": str(destination),
        "original": original,
        "size": (target_w, target_h),
        "bytes": destination.stat().st_size,
    }


def save_for_vision(image: str | Path | None = None, max_width: int = 1280,
                    quality: int = 75) -> Path:
    """把（截屏或指定图片）压成适合送给视觉模型的小图，返回文件路径。"""
    VISION_CACHE.mkdir(parents=True, exist_ok=True)
    if image is None:
        shot = grab_screen()
        from PIL import Image  # noqa: PLC0415

        source = VISION_CACHE / "screen.png"
        Image.fromarray(shot[:, :, ::-1]).save(source)
    else:
        source = Path(image)
    stamp = time.strftime("%H%M%S")
    target = VISION_CACHE / ("vision-" + stamp + ".jpg")
    result = resize_image(source, width=int(max_width), out=target, quality=quality)
    return Path(result["path"])


# ── 本地应用映射表 ───────────────────────────────────────────────


def app_map_path() -> Path:
    return APP_MAP_FILE


def load_app_map() -> dict[str, str]:
    """读用户自定义的应用映射表（apps.yaml）。缺文件时给一份内置默认。"""
    if not APP_MAP_FILE.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(APP_MAP_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if str(key).strip()}


def save_app_map(mapping: dict[str, str]) -> Path:
    import yaml  # noqa: PLC0415

    APP_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = ("# 本地应用映射表：说「打开 XXX」时优先查这里\n"
              "# 左：你对它说的话；右：程序名 / 完整路径 / 网址\n")
    APP_MAP_FILE.write_text(
        header + yaml.safe_dump(dict(sorted(mapping.items())), allow_unicode=True,
                                sort_keys=False),
        encoding="utf-8",
    )
    return APP_MAP_FILE


def resolve_app(name: str) -> dict:
    """在映射表里找这个说法对应的目标。"""
    key = str(name or "").strip().lower()
    mapping = load_app_map()
    if not key:
        return {"hit": False, "mapping": mapping}
    if key in {k.lower() for k in mapping}:
        for original, target in mapping.items():
            if original.lower() == key:
                return {"hit": True, "key": original, "target": target, "exact": True,
                        "mapping": mapping}
    for original, target in mapping.items():
        low = original.lower()
        if low and (low in key or key in low):
            return {"hit": True, "key": original, "target": target, "exact": False,
                    "mapping": mapping}
    return {"hit": False, "mapping": mapping}


def describe_app_map() -> str:
    mapping = load_app_map()
    if not mapping:
        return ("还没有自定义映射。用法：说「添加应用 XXX 指向 C:\\路径\\程序.exe」，"
                "或者直接编辑 " + str(APP_MAP_FILE))
    return "；".join(name + " → " + str(target) for name, target in list(mapping.items())[:12])


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
