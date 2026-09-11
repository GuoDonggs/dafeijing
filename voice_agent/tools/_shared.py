# -*- coding: utf-8 -*-
"""子模块共用的常量。

单独放一个文件，是为了让 windows / files / apps 之间不必为了一个路径常量
互相 import —— 那样很容易绕成循环导入。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import PROJECT_ROOT

__all__ = ["PROJECT_ROOT", "HOME", "SCREENSHOT_DIR", "MEMORY_FILE", "DEFAULT_SEARCH",
           "TURN", "reset_turn", "keep_listening"]

HOME = Path.home()
SCREENSHOT_DIR = HOME / "Pictures" / "voice-agent"
# 长期记忆存哪。默认在项目目录下；测试（或只读安装）可以用
# VOICE_AGENT_BUILD_DIR 把它挪到别处，别写进用户真实的记忆里。
BUILD_DIR = Path(os.environ["VOICE_AGENT_BUILD_DIR"]) if os.environ.get(
    "VOICE_AGENT_BUILD_DIR") else PROJECT_ROOT / "build"
MEMORY_FILE = BUILD_DIR / "memory.json"
DEFAULT_SEARCH = "https://www.bing.com/search?q={}"

# ── 「这一轮要不要接着听」 ──
# 工具本身是无状态的纯函数，但"答完这句要不要继续收音"是**这一轮对话**的状态。
# 放这里由 Brain 每轮开头重置、结束时读取。
TURN: dict = {"follow_up": False, "reason": ""}


def reset_turn() -> None:
    TURN["follow_up"] = False
    TURN["reason"] = ""


def keep_listening(reason: str = "") -> str:
    """模型主动要求「别走，我还要接着说」时调用的工具。

    为什么做成工具而不是让模型输出一个字段：语音链路上只有一条纯文本通道，
    让模型"顺便吐个 JSON"很不可靠。做成工具就是一次明确的动作，
    模型要么调了、要么没调，没有中间状态。
    """
    TURN["follow_up"] = True
    TURN["reason"] = str(reason or "").strip()
    return "好，我等着。"
