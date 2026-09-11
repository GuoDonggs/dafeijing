# -*- coding: utf-8 -*-
"""子模块共用的常量。

单独放一个文件，是为了让 windows / files / apps 之间不必为了一个路径常量
互相 import —— 那样很容易绕成循环导入。
"""

from __future__ import annotations

from pathlib import Path

from ..config import PROJECT_ROOT

__all__ = ["PROJECT_ROOT", "HOME", "SCREENSHOT_DIR", "MEMORY_FILE", "DEFAULT_SEARCH"]

HOME = Path.home()
SCREENSHOT_DIR = HOME / "Pictures" / "voice-agent"
MEMORY_FILE = PROJECT_ROOT / "build" / "memory.json"
DEFAULT_SEARCH = "https://www.bing.com/search?q={}"
