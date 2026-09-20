# -*- coding: utf-8 -*-
r"""Windows「已知文件夹」：下载 / 文档 / 图片 / 音乐 / 视频 / 桌面。

**为什么不能写 Path.home() / "Downloads"**：这些文件夹可以被用户搬走
（资源管理器 → 右键 → 属性 → 位置 → 移动）。搬完之后真正的位置记在注册表的
KnownFolders 里，而 C:\Users\<名字>\Downloads 往往还留着（空壳或旧文件）。
用户报了这条：下载目录改到了 D:\download，助手却还是打开
C:\Users\<名字>\Downloads —— 因为代码里就是拿 HOME 拼的。

SHGetKnownFolderPath 是这件事唯一权威的答案（资源管理器自己也查它），这里纯
ctypes 调用，不引 pywin32 / comtypes。取不到（非 Windows、老系统、调用失败）
就退回 Path.home()/<英文名>，任何平台上都不抛异常。
"""

from __future__ import annotations

import ctypes
import os
import threading
import uuid
from pathlib import Path

__all__ = ["KNOWN_FOLDERS", "known_dir", "all_known_dirs", "default_dir", "clear_cache"]

#: 目录名 → FOLDERID（KNOWNFOLDERID 是稳定的系统常量，不是随机 GUID）
_FOLDER_IDS: dict[str, str] = {
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DCDF",
}
#: 可以问的目录名（英文规范名）
KNOWN_FOLDERS: tuple[str, ...] = tuple(_FOLDER_IDS)
_BY_LOWER = {name.lower(): name for name in _FOLDER_IDS}

_lock = threading.Lock()
_cache: dict[str, Path] = {}


def default_dir(name: str) -> Path:
    r"""退路：C:\Users\<你>\Downloads 这一套（非 Windows / 取不到已知文件夹时用）。"""
    return Path.home() / str(name)


def clear_cache() -> None:
    """忘掉缓存（测试用；用户改了目录位置也不会热生效 —— 重启即取最新）。"""
    with _lock:
        _cache.clear()


def _shget(folder_id: str) -> Path | None:
    """问一次 SHGetKnownFolderPath。失败一律返回 None（调用方有退路）。

    单独一个小函数是为了能在测试里被替换掉 —— 断言「搬走之后的目录会生效」
    不能真去改用户注册表。
    """
    if os.name != "nt":
        return None
    try:
        shell32 = ctypes.WinDLL("shell32")
        ole32 = ctypes.WinDLL("ole32")
    except OSError:
        return None
    try:
        # KNOWNFOLDERID 是 16 字节 GUID（内存布局＝little-endian 字节序）
        guid = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        out = ctypes.c_wchar_p()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    except (OSError, AttributeError, ValueError):
        return None
    if hr != 0 or not out.value:
        return None
    try:
        return Path(out.value)
    finally:
        # 这块内存是 shell32 用 CoTaskMemAlloc 分的，必须还给它（否则每次泄漏一条路径）
        try:
            ole32.CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        except OSError:
            pass


def known_dir(name: str, *, use_cache: bool = True) -> Path:
    """问 Windows：这个文件夹**现在**在哪。

    认不出来（不是那六个之一）就当普通相对目录名，接在用户目录下 —— 和以前的
    行为一致，不会因为传了怪名字就抛异常。
    """
    key = _BY_LOWER.get(str(name or "").strip().lower())
    if key is None:
        return default_dir(str(name or "").strip())
    if use_cache:
        with _lock:
            hit = _cache.get(key)
        if hit is not None:
            return hit
    found = _shget(_FOLDER_IDS[key])
    path = found if found is not None else default_dir(key)
    if use_cache:
        with _lock:
            _cache[key] = path
    return path


def all_known_dirs() -> dict[str, Path]:
    """六个目录一次问全（doctor / 排障用）。"""
    return {name: known_dir(name) for name in KNOWN_FOLDERS}
