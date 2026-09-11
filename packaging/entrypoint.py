# -*- coding: utf-8 -*-
"""打包后的启动器（PyInstaller 的入口脚本）。

为什么不能直接把 voice_agent/__main__.py 当入口：PyInstaller 会把入口脚本
当成顶层的 __main__ 模块来执行，而 __main__.py 里用的是 'from . import ...'
这种包内相对导入 —— 没有包上下文，一启动就 ImportError。

所以这个文件只做三件事：

1. 从包外导入 voice_agent.__main__.main（相对导入留在包里，不受影响）；
2. 双击（不带参数）时默认打开桌面界面：VoiceAgent.exe 是窗口程序，
   不带参数只会打印帮助然后退出，用户双击后什么都看不到；
3. multiprocessing.freeze_support()：冻结后的程序一旦再起子进程，
   少了这句会把整个程序重新执行一遍。

命令行版 VoiceAgentCLI.exe 不带参数时保持原样（打印帮助），
这样 VoiceAgentCLI.exe 和 python -m voice_agent 的行为完全一致。
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

#: 窗口版 exe 的名字（去掉分隔符、转小写后比较）。改名了也不会崩，
#: 只是退回「打印帮助」的默认行为。
WINDOWED_EXE_STEM = "voiceagent"


def rewrite_argv(argv: list[str]) -> list[str]:
    """窗口版 exe：双击（无参数）或只带选项时，都当成「打开桌面界面」。

    双击的场景不用多说；只带选项的情况也要照顾：
    VoiceAgent.exe --no-autostart 里的 --no-autostart 是 ui 子命令的参数，
    直接丢给 argparse 会被当成未知子命令，程序以退出码 2 静默退出 ——
    窗口版没有控制台，用户双击后只会觉得「什么都没发生」。
    """
    try:
        stem = Path(sys.executable).stem.lower().replace("-", "").replace("_", "")
    except Exception:  # noqa: BLE001 - 取不到名字就按原样跑
        return argv
    if stem != WINDOWED_EXE_STEM:
        return argv

    known = {"ui", "gui", "run", "ask", "say", "listen", "wake", "skills",
             "tools", "devices", "doctor", "selftest"}
    if any(arg in known for arg in argv):
        return argv
    return ["ui"] + list(argv)


def main() -> int:
    from voice_agent.__main__ import main as cli_main

    return int(cli_main(rewrite_argv(sys.argv[1:])) or 0)


if __name__ == "__main__":
    # 必须先于任何业务代码：窗口版下的多进程/子进程要靠它复用当前进程
    multiprocessing.freeze_support()
    sys.exit(main())
