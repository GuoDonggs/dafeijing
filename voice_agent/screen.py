# -*- coding: utf-8 -*-
"""屏幕与鼠标：截图、找图、缩放、点按拖动，以及用户自定义的应用映射表。

放在单独一个模块里，是因为这些能力都依赖 Pillow / OpenCV，而核心的语音链路
不该被它们拖累 —— 没装也不影响助手说话。

坐标系统一用**屏幕绝对坐标**（左上角为原点），和用户看到的鼠标位置一致。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import numpy as np

from . import paths
from .config import PROJECT_ROOT

__all__ = [
    "app_map_path", "load_app_map", "save_app_map", "resolve_app",
    "grab_screen", "find_template", "resize_image", "save_for_vision",
    "mouse_position", "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "open_cv_ready",
]

def app_map_path() -> Path:
    """应用映射表放哪（数据目录下，用户可以改数据目录）。"""
    return paths.sub("apps", create=False)


def vision_cache() -> Path:
    """送给视觉模型的压缩图缓存。"""
    return paths.sub("vision")

# ── Win32 鼠标事件 ───────────────────────────────────────────────
_MOVE = 0x0001
_LEFT_DOWN, _LEFT_UP = 0x0002, 0x0004
_RIGHT_DOWN, _RIGHT_UP = 0x0008, 0x0010
_MIDDLE_DOWN, _MIDDLE_UP = 0x0020, 0x0040
_WHEEL, _HWHEEL = 0x0800, 0x1000
# SendInput 的 dx/dy 默认是**相对位移**。要送绝对坐标必须带 ABSOLUTE，
# 并且用 VIRTUALDESK 说明"坐标是相对整个虚拟桌面的"（多显示器时少一个都不行）。
_ABSOLUTE, _VIRTUALDESK = 0x8000, 0x4000
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
    # 这里**不能**再塞一个 padding 成员：_INPUT 的大小必须正好是 40 字节
    # （64 位下 4 + 4 对齐 + 32）。多了几个字节，SendInput 会整条拒绝并返回 0，
    # 而且不报任何错 —— 症状就是"鼠标、键盘点了没反应"。
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


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
    # 一定要检查返回值：SendInput 失败时只返回 0，不会抛异常，
    # 于是"点了没反应"这种问题会一直查不出来（这次就是）。
    sent = _user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(_INPUT))
    if sent != 1:
        raise RuntimeError("系统拒绝了这次鼠标事件（SendInput=" + str(sent)
                           + "，错误码 " + str(ctypes.get_last_error()) + "）")


def _virtual_screen() -> tuple[int, int, int, int]:
    """虚拟桌面（所有显示器合起来）的左、上、宽、高。"""
    return (int(_user32.GetSystemMetrics(76)), int(_user32.GetSystemMetrics(77)),
            int(_user32.GetSystemMetrics(78)), int(_user32.GetSystemMetrics(79)))


def _absolute_xy(x: int, y: int) -> tuple[int, int]:
    """屏幕像素 → SendInput 要的 0~65535 归一化绝对坐标。

    注意是**虚拟桌面**（所有显示器合起来）：第二块屏幕在主屏左边时，
    虚拟桌面的左边界是负数，直接用主屏尺寸算会整体偏掉一屏。
    """
    left, top, width, height = _virtual_screen()
    nx = int(round((int(x) - left) * 65535 / max(1, width - 1)))
    ny = int(round((int(y) - top) * 65535 / max(1, height - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _move_to(x: int, y: int) -> None:
    """把鼠标**绝对**移动到屏幕像素 (x, y)。

    绝对坐标必须归一化：直接把手像素当 dx/dy 发出去，系统会当成"相对位移"，
    鼠标于是飞到屏幕角落，点哪儿都不对。
    """
    nx, ny = _absolute_xy(int(x), int(y))
    _send_mouse(_MOVE | _ABSOLUTE | _VIRTUALDESK, nx, ny)


def mouse_position() -> tuple[int, int]:
    """当前鼠标位置。

    必须检查返回值：GetCursorPos 在锁屏、切到安全桌面（UAC）、
    或者当前进程不在输入桌面上时会失败，而失败时 POINT 还是全 0 ——
    不检查的话会一本正经地返回 (0,0)，调用方根本看不出这是"读不到"。
    """
    if _user32 is None:
        raise RuntimeError("当前系统不支持读取鼠标位置")
    point = wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        raise RuntimeError("读不到鼠标位置（屏幕可能锁了，或者桌面被切走了）")
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
        _move_to(nx, ny)
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


# ── 显示器 ──────────────────────────────────────────────────────
class _MONITORINFO(ctypes.Structure):
    _fields_ = (("cbSize", ctypes.c_ulong), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", ctypes.c_ulong))


def list_monitors() -> list[dict]:
    """所有显示器，**主屏排第一**，其余按从左到右。

    多屏时"截屏"到底截哪一块，用户和模型都说不清 —— 给它们一个稳定的编号：
    1 = 主屏，2、3… = 其它屏（从左到右）。
    """
    if _user32 is None:
        return []
    found: list[dict] = []

    def _callback(hmonitor, _hdc, _rect, _data):  # noqa: ANN001
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        primary = False
        if _user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            primary = bool(info.dwFlags & 1)
            box = info.rcMonitor
        else:
            box = _rect.contents
        found.append({
            "left": int(box.left), "top": int(box.top),
            "right": int(box.right), "bottom": int(box.bottom),
            "width": int(box.right - box.left), "height": int(box.bottom - box.top),
            "primary": primary,
        })
        return 1

    try:
        callback = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(wintypes.RECT), ctypes.c_double)(_callback)
        _user32.EnumDisplayMonitors(None, None, callback, 0)
    except Exception:  # noqa: BLE001 - 枚举失败就当只有一块屏
        return []
    found.sort(key=lambda item: (not item["primary"], item["left"]))
    return found


def monitor_rect(index: int) -> tuple[int, int, int, int] | None:
    """第 index 块显示器的矩形（1 = 主屏，0/负数 = 整个虚拟桌面 → None）。"""
    try:
        want = int(index)
    except (TypeError, ValueError):
        return None
    if want <= 0:
        return None
    monitors = list_monitors()
    if not monitors:
        return None
    if want > len(monitors):
        return None
    item = monitors[want - 1]
    return item["left"], item["top"], item["right"], item["bottom"]


def grab_screen(region: tuple[int, int, int, int] | None = None,
                monitor: int = 0) -> np.ndarray:
    """截屏成 BGR 数组（OpenCV 习惯的顺序）。

    region 是 (left, top, right, bottom)。也可以只给 monitor（1 = 主屏），
    多显示器时按屏截比"截全屏再裁"省一大半像素 —— 送视觉模型前尤其值钱。
    """
    from PIL import ImageGrab  # noqa: PLC0415

    if region is None and monitor:
        region = monitor_rect(int(monitor))
    image = ImageGrab.grab(bbox=region, all_screens=True)
    return np.array(image.convert("RGB"))[:, :, ::-1].copy()


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".webp")


def _imread(path: Path):
    """读一张图（BGR 数组），读不出来返回 None。

    不能用 cv2.imread：它在 Windows 上**读不了中文路径**（内部用窄字符 API），
    返回 None 而不报错。而中文文件名恰恰是中文用户最自然的写法
    （「下载按钮.png」），所以这里自己读字节再解码。
    """
    import cv2  # noqa: PLC0415

    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def reference_dir() -> Path:
    """参考图片目录：里面放"要找的东西"的小图。"""
    from .tools._shared import reference_dir  # noqa: PLC0415 - 避免循环导入

    return reference_dir()


def list_reference_images(limit: int = 20) -> list[str]:
    """参考目录里现成的图片名（不带扩展名），给"有哪些能找"用。"""
    folder = reference_dir()
    if not folder.is_dir():
        return []
    names = []
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in _IMAGE_SUFFIXES:
            names.append(item.stem)
        if len(names) >= limit:
            break
    return names


def resolve_template(image: str | Path) -> Path:
    """把"要找的那张图"解析成文件路径。

    可以直接给路径；也可以只给**名字**（下载按钮），此时去参考图片目录里找 ——
    语音场景里没人念得出一长串路径，说名字才是自然的。
    """
    raw = str(image or "").strip().strip('"').strip("'")
    if not raw:
        raise FileNotFoundError("没说要找哪张图片")
    direct = Path(os.path.expandvars(os.path.expanduser(raw)))
    if direct.is_file():
        return direct
    folder = reference_dir()
    candidates = [raw] if Path(raw).suffix else [raw + suffix for suffix in _IMAGE_SUFFIXES]
    for name in candidates:
        target = folder / name
        if target.is_file():
            return target
    if folder.is_dir():
        lowered = raw.lower()
        for item in folder.iterdir():
            if item.is_file() and item.stem.lower() == lowered:
                return item
    existing = list_reference_images(8)
    hint = ("参考图片目录里现有：" + "、".join(existing)) if existing else (
        "把要找的小图放进 " + str(folder) + " 就能直接用名字找")
    raise FileNotFoundError("找不到图片「" + raw + "」。" + hint)


def find_in_image(image: str | Path, template: str | Path, confidence: float = 0.8,
                  scales: tuple[float, ...] = (1.0,), limit: int = 5) -> list[dict]:
    """在一张图片里找另一张图（不碰屏幕）。

    用来回答"这张截图里有没有那个图标""参考图 A 里有没有 B"，
    也用来在把图送进视觉模型之前先自己比对一遍 —— 本地比对不要钱。
    """
    import cv2  # noqa: PLC0415

    source = Path(image)
    if not source.is_file():
        raise FileNotFoundError("找不到图片：" + str(image))
    shot = _imread(source)
    if shot is None:
        raise ValueError("读不出这张图片：" + str(image))
    target_path = resolve_template(template)
    target = _imread(target_path)
    if target is None:
        raise ValueError("读不出要找的那张图：" + str(target_path))
    shot_gray = cv2.cvtColor(shot, cv2.COLOR_BGR2GRAY)
    shot_h, shot_w = shot_gray.shape[:2]
    found: list[dict] = []
    for scale in scales:
        if scale <= 0:
            continue
        height = int(target.shape[0] * scale)
        width = int(target.shape[1] * scale)
        if height < 6 or width < 6 or height > shot_h or width > shot_w:
            continue
        scaled = cv2.resize(target, (width, height), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(shot_gray, cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY),
                                   cv2.TM_CCOEFF_NORMED)
        ys, xs = np.where(result >= float(confidence))
        for x, y in zip(xs.tolist(), ys.tolist()):
            found.append({"x": int(x + width / 2), "y": int(y + height / 2),
                          "score": float(result[y, x]), "scale": float(scale),
                          "w": width, "h": height})
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


def find_template(image: str | Path, confidence: float = 0.8,
                  region: tuple[int, int, int, int] | None = None,
                  scales: tuple[float, ...] = (1.0, 0.9, 1.1, 0.8, 1.25),
                  limit: int = 5) -> list[dict]:
    """在屏幕上找一张小图，返回 [(中心x, 中心y, 相似度, 缩放)]。

    多尺度匹配是为了容忍界面缩放（125% DPI、浏览器缩放）导致的尺寸差异；
    同一目标会在多个尺度上重复命中，所以最后要按重叠度去重。
    """
    import cv2  # noqa: PLC0415

    target = resolve_template(image)
    template = _imread(target)
    if template is None:
        raise ValueError("读不出这张图片：" + str(target))

    shot = grab_screen(region)
    # 截图的原点是**虚拟桌面**的左上角，不是主屏的左上角。
    # 第二块屏放在主屏左边时虚拟桌面原点是个负数（比如 -1920），
    # 这里要是按 0 算，返回的坐标会整体偏掉整整一屏 —— 找图明明找到了，
    # 点下去却点到另一块屏幕上。
    if region:
        offset_x, offset_y = int(region[0]), int(region[1])
    else:
        offset_x, offset_y = _virtual_screen()[:2]
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


def save_for_vision(image: str | Path | None = None, max_side: int = 1280,
                    quality: int = 75,
                    region: tuple[int, int, int, int] | None = None,
                    monitor: int = 0) -> dict:
    """把（截屏或指定图片）压成适合送给视觉模型的小图。

    返回一个字典而不是光一个路径：**模型给出的坐标要能换算回屏幕像素**。
    图片被缩小过、截图的坐标原点又可能是负数（多显示器），这两件事不告诉
    模型，它说"点这个按钮"时给的坐标就是错的 —— 而且看不出来错在哪。

    返回 {path, size, original, scale, origin, screen}
      size      送给模型的图片尺寸
      original  原图尺寸（= 虚拟桌面尺寸）
      scale     原图 / 送出去的图（比如 1.5 表示屏幕上 1 像素 = 图里 0.67 像素）
      origin    截图左上角对应的屏幕坐标（多显示器时可能是负的）
      screen    虚拟桌面宽高
    """
    VISION_CACHE = vision_cache()
    VISION_CACHE.mkdir(parents=True, exist_ok=True)
    origin = (0, 0)
    if image is None:
        # 只截需要的那一块：整屏 3840×1080 缩到 1280 宽后，很多细节就糊了；
        # 裁成用户框的那一块再送，等于把同样的 token 花在真正要看的地方。
        if region is None and monitor:
            region = monitor_rect(int(monitor))
        shot = grab_screen(region)
        from PIL import Image  # noqa: PLC0415

        source = VISION_CACHE / "screen.png"
        Image.fromarray(shot[:, :, ::-1]).save(source)
        origin = (int(region[0]), int(region[1])) if region else _virtual_screen()[:2]
    else:
        source = Path(image)
    stamp = time.strftime("%H%M%S")
    target = VISION_CACHE / ("vision-" + stamp + ".jpg")
    # max_side 是"最长边"的上限：竖屏截图按宽度缩会越缩越大
    from PIL import Image  # noqa: PLC0415

    with Image.open(source) as probe:
        width, height = probe.size
    limit = max(64, int(max_side or 1280))
    if width >= height:
        result = resize_image(source, width=min(width, limit), out=target, quality=quality)
    else:
        result = resize_image(source, height=min(height, limit), out=target, quality=quality)
    original = result["original"]
    scale = round(original[0] / max(1, result["size"][0]), 4)
    return {
        "path": str(result["path"]),
        "size": result["size"],
        "original": original,
        "scale": scale,
        "origin": origin,
        "screen": _virtual_screen()[2:],
    }


# ── 本地应用映射表 ───────────────────────────────────────────────
#
# 这张表只干一件事：把「一个程序 / 脚本 / 启动方式」映射成一个**名字**，
# 于是用户说「打开 XXX」时能对上号。写法两种都认：
#
#   微信: C:\Program Files\Tencent\WeChat\WeChat.exe     # 简写：名字 -> 目标
#
#   我的备份:                                              # 完整写法
#     target: D:\scripts\backup.bat
#     aliases: [备份脚本, backup]                          # 同一个目标可以有多个叫法
#     type: command                                       # exe / path / url / command
#     args: ["--fast"]                                    # 可选启动参数

_APP_TYPES = ("exe", "path", "url", "command", "folder")





def _guess_app_type(target: str) -> str:
    """猜这条映射是什么类型。

    **目录要单独认出来**：用户说「把我的项目指到 D 盘的 code 目录」时，
    那是要"打开文件夹"，不是"执行程序"。
    判据：已经存在的目录，或者写法上就以斜杠结尾。
    """
    low = target.lower()
    if "://" in target:
        return "url"
    if low.endswith((".exe", ".lnk", ".bat", ".cmd", ".com")):
        return "path"
    if low.endswith((".com", ".cn", ".net", ".org")) and " " not in target:
        return "url"
    stripped = target.rstrip("\\/")
    if target.endswith(("\\", "/")) and stripped:
        return "folder"
    try:
        if stripped and Path(stripped).is_dir():
            return "folder"
    except OSError:
        pass
    return "command"


def _normalize_entry(value: Any) -> dict:
    """把一条映射统一成 {target, aliases, type, args}。"""
    if isinstance(value, dict):
        target = str(value.get("target") or value.get("path")
                     or value.get("command") or "").strip()
        raw_aliases = value.get("aliases") or value.get("alias") or []
        raw_args = value.get("args") or []
        kind = str(value.get("type") or "").strip().lower()
    else:
        target = str(value or "").strip()
        raw_aliases, raw_args, kind = [], [], ""
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    if isinstance(raw_args, str):
        raw_args = [raw_args]
    aliases = [str(a).strip() for a in raw_aliases if str(a).strip()]
    args = [str(a) for a in raw_args if str(a).strip()]
    if kind not in _APP_TYPES:
        kind = _guess_app_type(target)
    return {"target": target, "aliases": aliases, "type": kind, "args": args}


def load_app_map() -> dict[str, dict]:
    """读用户自定义的应用映射表（apps.yaml）。缺文件时就是空的。"""
    APP_MAP_FILE = app_map_path()
    if not APP_MAP_FILE.is_file():
        return {}
    try:
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(app_map_path().read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    mapping: dict[str, dict] = {}
    for key, value in data.items():
        name = str(key).strip()
        if not name or name.startswith("#"):
            continue
        entry = _normalize_entry(value)
        if entry["target"]:
            mapping[name] = entry
    return mapping


def save_app_map(mapping: dict[str, Any]) -> Path:
    """写回映射表。只给 target 的条目写成简写，有别名/参数的写完整形式。"""
    import yaml  # noqa: PLC0415

    APP_MAP_FILE = app_map_path()
    APP_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    header = ("# 本地应用映射表：说「打开 XXX」时优先查这里\n"
              "# 左边是名字（可以有多个叫法），右边是程序名 / 完整路径 / 网址。\n"
              "# 也可以写成完整形式：\n"
              "#   我的备份:\n"
              "#     target: D:\\scripts\\backup.bat\n"
              "#     aliases: [备份脚本, backup]\n"
              "#     type: command\n")
    clean: dict[str, Any] = {}
    for key, value in sorted(mapping.items()):
        name = str(key).strip()
        if not name:
            continue
        entry = _normalize_entry(value)
        if not entry["target"]:
            continue
        if entry["aliases"] or entry["args"]:
            item = {"target": entry["target"], "type": entry["type"]}
            if entry["aliases"]:
                item["aliases"] = entry["aliases"]
            if entry["args"]:
                item["args"] = entry["args"]
            clean[name] = item
        else:
            clean[name] = entry["target"]
    APP_MAP_FILE.write_text(
        header + yaml.safe_dump(clean, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return APP_MAP_FILE


def resolve_app(name: str) -> dict:
    """在映射表里找这个说法对应的目标。

    匹配顺序：名字完全一致 > 别名完全一致 > 互相包含（「打开微信」能对上「微信」）。
    """
    key = str(name or "").strip().lower()
    mapping = load_app_map()
    if not key:
        return {"hit": False, "mapping": mapping}

    def matched(entry_key: str, entry: dict) -> bool:
        return entry_key.lower() == key or any(a.lower() == key for a in entry["aliases"])

    for entry_key, entry in mapping.items():
        if matched(entry_key, entry):
            return {"hit": True, "key": entry_key, "entry": entry,
                    "target": entry["target"], "exact": True, "mapping": mapping}
    for entry_key, entry in mapping.items():
        names = [entry_key.lower()] + [a.lower() for a in entry["aliases"]]
        if any(n and (n in key or key in n) for n in names):
            return {"hit": True, "key": entry_key, "entry": entry,
                    "target": entry["target"], "exact": False, "mapping": mapping}
    return {"hit": False, "mapping": mapping}


def describe_app_map() -> str:
    mapping = load_app_map()
    if not mapping:
        return ("还没有自定义映射。可以直接说「添加应用 我的项目 指向 D:\\code」，"
                "或者编辑 " + str(app_map_path()))
    parts = []
    for name, entry in list(mapping.items())[:12]:
        text = name
        if entry["aliases"]:
            text += "（" + "/".join(entry["aliases"][:3]) + "）"
        parts.append(text + " → " + entry["target"])
    return "；".join(parts)



def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
