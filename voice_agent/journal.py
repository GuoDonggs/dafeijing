# -*- coding: utf-8 -*-
"""运行日志：**落盘的**详细记录。

为什么要有它：GUI 里那个日志窗口只是个 deque(maxlen=600)，关掉就没了 ——
用户拿着"刚才那一下到底干了什么"来问的时候翻不到，程序闪退更是连一行都不剩
（ui/main_window.py 里那个 AttributeError 就是活例子：栈只打在控制台上，
窗口版连控制台都没有）。

这里把每条日志**同时**写进 <数据目录>/logs/voice-agent-YYYYMMDD.log：
按天分文件、自动清掉旧的、线程安全，外加一个崩溃钩子把 traceback 也写进去。

两级日志：

- info   用户看得懂的过程（唤醒、识别、调了哪个工具、结果、播报、打断）
- detail 排查用的细节（参数全文、耗时、token 用量、每次轮询检查）

**文件里两级都写**；界面默认只显示 info，日志窗口里勾上「详细」就能一起看。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

__all__ = ["LEVELS", "write", "info", "detail", "file_path", "tail", "install_crash_handler",
           "prune", "recent_files", "process_line", "adapt"]

#: 两级：info 给用户看，detail 给排查用
LEVELS = ("info", "detail")
#: 保留多少天的日志
KEEP_DAYS = 14
#: 单个文件超过它就换一个（同一个进程长时间跑也不会长成几个 GB）
MAX_BYTES = 16 * 1024 * 1024
#: 写文件时截断单行的长度：一条日志带上一整篇文件内容对排查没帮助
MAX_LINE = 4000

_lock = threading.Lock()
_installed = False


def _logs_dir(create: bool = False) -> Path:
    from . import paths  # noqa: PLC0415 - 避免循环导入

    return paths.sub("logs", create=create)


def _stamp() -> tuple[str, str]:
    now = time.localtime()
    return time.strftime("%Y%m%d", now), time.strftime("%Y-%m-%d %H:%M:%S", now)


def file_path() -> Path:
    """今天的日志文件（不存在也会返回路径）。"""
    day, _ = _stamp()
    return _logs_dir() / ("voice-agent-" + day + ".log")


def recent_files(limit: int = 14) -> list[Path]:
    folder = _logs_dir()
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.glob("voice-agent-*.log") if p.is_file()),
                   key=lambda p: p.name, reverse=True)
    return files[:limit]


def prune(keep_days: int = KEEP_DAYS) -> int:
    """删掉太旧的日志。返回删了几个文件。"""
    folder = _logs_dir()
    if not folder.is_dir():
        return 0
    cutoff = time.time() - max(1, int(keep_days)) * 86400
    removed = 0
    for item in folder.glob("voice-agent-*.log*"):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def write(level: str, message: str, tag: str = "") -> None:
    """写一条日志。**任何失败都不该影响主流程**，所以这里一路吞异常。"""
    text = str(message or "").replace("\r", " ").strip()
    if not text:
        return
    if len(text) > MAX_LINE:
        text = text[:MAX_LINE] + "…（截断）"
    mark = "·" if str(level) == "detail" else " "
    head = _stamp()[1] + " [" + mark + "] "
    if tag:
        head += "[" + str(tag) + "] "
    # 多行（比如 traceback）整段缩进，读起来才知道是一件事
    body = "\n".join(("    " + part) if index else part
                     for index, part in enumerate(text.splitlines()))
    try:
        with _lock:
            _logs_dir(create=True)
            path = file_path()
            try:
                if path.is_file() and path.stat().st_size > MAX_BYTES:
                    path.replace(path.with_name(path.name + ".1"))
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as handle:
                handle.write(head + body + "\n")
    except Exception:  # noqa: BLE001 - 日志写不下去绝不影响主流程
        pass


def info(message: str, tag: str = "") -> None:
    write("info", message, tag)


def detail(message: str, tag: str = "") -> None:
    write("detail", message, tag)


def tail(limit: int = 40, level: str = "info") -> list[str]:
    """读最近几条（默认只读 info 级）。"""
    path = file_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    picked = lines if str(level) == "detail" else [ln for ln in lines if " [·] " not in ln]
    return picked[-max(1, int(limit)):]


def _format_exc(exc_type, exc, tb) -> str:
    return ("".join(traceback.format_exception(exc_type, exc, tb))).rstrip()


def install_crash_handler() -> None:
    """让未捕获的异常也进日志文件。

    窗口版没有控制台，栈只打在内存里 —— 用户能提供的只有一句"它闪退了"。
    装上这个之后，日志文件里能看到完整栈和当时的时间。
    """
    global _installed
    if _installed:
        return
    _installed = True

    def hook(exc_type, exc, tb) -> None:
        write("info", "【崩溃】未捕获的异常：\n" + _format_exc(exc_type, exc, tb), tag="crash")
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook

    def thread_hook(args) -> None:  # noqa: ANN001 - threading.ExceptHookArgs
        if issubclass(args.exc_type, SystemExit):
            return
        write("info",
              "【崩溃】线程 " + str(getattr(args.thread, "name", "?")) + " 里出错：\n"
              + _format_exc(args.exc_type, args.exc_value, args.exc_traceback),
              tag="crash")

    try:
        threading.excepthook = thread_hook
    except Exception:  # noqa: BLE001 - 老 Python 没有就算了
        pass


def adapt(log) -> Callable[..., None]:  # noqa: ANN001
    """把外部传进来的 log 统一成 (message, level="info") 的签名。

    各个模块都能接一个 log 回调（界面传 Console.log、命令行传 print、
    测试传 lambda）。加了 level 之后，只收一个参数的那些会 TypeError ——
    在一个"记日志"的地方崩掉是最冤枉的。这里按签名判断：

    - 声明了第二个位置参数（Console.log(message, level=...)）→ 原样用；
    - 有 **kwargs → 原样用；
    - 只收一个参数 / 只有 *args（print）→ 丢掉 level 标记。
    """
    import inspect  # noqa: PLC0415

    if log is None:
        return lambda message, level="info": None
    try:
        params = list(inspect.signature(log).parameters.values())
    except (TypeError, ValueError):
        params = []
    if any(p.kind is p.VAR_KEYWORD for p in params) or \
            len([p for p in params if p.kind in (p.POSITIONAL_ONLY,
                                                 p.POSITIONAL_OR_KEYWORD)]) >= 2:
        return log

    def one_arg(message: str, level: str = "info") -> None:
        if str(level) == "detail":
            return          # 这个 sinks 分不出级别，详细日志就不打扰它了
        try:
            log(message)
        except TypeError:
            pass

    return one_arg


def process_line() -> str:
    """日志开头写一行进程信息：排查时先看这行。"""
    return ("— 启动 — 进程 " + str(os.getpid()) + "  Python " + sys.version.split()[0]
            + "  数据目录 " + str(_logs_dir().parent))