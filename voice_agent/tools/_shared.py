# -*- coding: utf-8 -*-
"""子模块共用的常量。

单独放一个文件，是为了让 windows / files / apps 之间不必为了一个路径常量
互相 import —— 那样很容易绕成循环导入。
"""

from __future__ import annotations

from pathlib import Path

from .. import paths
from ..config import PROJECT_ROOT

__all__ = ["PROJECT_ROOT", "HOME", "DEFAULT_SEARCH", "TURN", "reset_turn", "keep_listening",
           "screenshot_dir", "reference_dir", "memory_file", "build_dir"]

HOME = Path.home()
DEFAULT_SEARCH = "https://www.bing.com/search?q={}"

# ── 运行时文件都收在「数据目录」下（见 voice_agent/paths.py）──
# 以前截图丢在图片文件夹、记忆丢在 build/、映射表丢在项目根，三处分散。
# 现在统一，而且目录可以在设置窗口里改。这些必须是**函数**而不是常量：
# 用户改了数据目录要立刻生效，import 期算好的常量改不动。


def screenshot_dir(create: bool = False) -> Path:
    """截图存哪（参考图片在它下面的 reference/）。"""
    return paths.sub("screenshots", create=create)


def reference_dir(create: bool = False) -> Path:
    """参考图片目录：把「下载按钮.png」丢进去，就能说「找一下下载按钮」。"""
    return paths.sub("reference", create=create)


def memory_file(create: bool = False) -> Path:
    """长期记忆存哪。"""
    return paths.sub("memory", create=create)


#: 兼容旧名字（有些地方直接 import 了它）—— 指向数据目录本身
def build_dir() -> Path:
    return paths.data_dir()

# ── 「这一轮要不要接着听」 ──
# 工具本身是无状态的纯函数，但"答完这句要不要继续收音"是**这一轮对话**的状态。
# 放这里由 Brain 每轮开头重置、结束时读取。
TURN: dict = {"follow_up": False, "reason": ""}


def reset_turn() -> None:
    TURN["follow_up"] = False
    TURN["reason"] = ""
    # 新一轮对话：上一轮碰过外部内容的标记一起清掉（权限模式不清）
    from .. import security  # noqa: PLC0415 - 避免包初始化期的循环导入

    security.reset_turn()


def keep_listening(reason: str = "") -> str:
    """模型主动要求「别走，我还要接着说」时调用的工具。

    为什么做成工具而不是让模型输出一个字段：语音链路上只有一条纯文本通道，
    让模型"顺便吐个 JSON"很不可靠。做成工具就是一次明确的动作，
    模型要么调了、要么没调，没有中间状态。
    """
    TURN["follow_up"] = True
    TURN["reason"] = str(reason or "").strip()
    return "好，我等着。"
