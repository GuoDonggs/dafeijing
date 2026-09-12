# -*- coding: utf-8 -*-
"""应用与网页：打开程序、打开网址、搜索。

打开程序时先查用户自己的 apps.yaml 映射表，再走内置别名，最后去开始菜单找。
"""

from __future__ import annotations

import shutil

from ._shared import DEFAULT_SEARCH
from .windows import _find_start_menu_app, _launch

__all__ = ["open_app", "open_url", "web_search"]

def open_app(name: str = "") -> str:
    """打开一个应用 / 网站 / 文件夹。"""
    target_name = (name or "").strip()
    if not target_name:
        return "没说要打开什么"
    lowered = target_name.lower().rstrip("。！")
    # 先查用户自己的映射表（apps.yaml）：它优先级最高，用户说什么就是什么
    try:
        from .. import screen as screen_mod  # noqa: PLC0415

        found = screen_mod.resolve_app(lowered)
        if found.get("hit"):
            entry = found.get("entry") or {}
            mapped = str(entry.get("target") or found.get("target") or "")
            args = [str(a) for a in (entry.get("args") or [])]
            kind = str(entry.get("type") or "")
            if kind == "url" or "://" in mapped:
                return open_url(mapped)
            if kind == "folder":
                from .windows import open_folder  # noqa: PLC0415

                return open_folder(mapped, label=target_name)
            if kind == "command":
                # 命令类（脚本、带参数的调用）走 shell，才认得到参数和管道
                ok = _launch(mapped, args) or _launch("cmd", ["/c", mapped] + args)
            else:
                ok = _launch(mapped, args) or _launch(shutil.which(mapped) or "")
            return ("已经打开" + target_name + "（来自应用映射表）") if ok else (
                "映射表里「" + str(found["key"]) + "」指向 " + mapped + "，但打不开它")
    except Exception:  # noqa: BLE001 - 映射表坏了就按内置规则走
        pass
    alias = _APP_ALIASES.get(lowered)
    if alias is None:
        for key, value in _APP_ALIASES.items():
            if key in lowered:
                alias = value
                break
    target = alias or lowered

    if target.startswith(("ms-settings:", "ms-windows-store:", "ms-screenclip:", "shell:", "microsoft.")):
        return ("已经打开" + target_name) if _launch(target) else ("打不开" + target_name)
    if "://" in target or target.endswith((".com", ".cn", ".net", ".org")):
        return open_url(target)

    found = shutil.which(target)
    if found:
        return ("已经打开" + target_name) if _launch(found) else ("打不开" + target_name)

    shortcut = _find_start_menu_app(target_name)
    if shortcut is not None:
        return ("已经打开" + target_name) if _launch(str(shortcut)) else ("打不开" + target_name)

    if _launch(target):
        return "已经打开" + target_name
    return "没找到叫" + target_name + "的程序"


def open_url(url: str = "") -> str:
    """用默认浏览器打开网址。"""
    value = (url or "").strip()
    if not value:
        return "没说要打开哪个网址"
    if "://" not in value:
        value = "https://" + value
    return "已经打开网页" if _launch(value) else "打不开这个网页"


def web_search(query: str = "") -> str:
    """调用默认浏览器搜索。"""
    value = (query or "").strip()
    if not value:
        return "没说要搜什么"
    from urllib.parse import quote

    return open_url(DEFAULT_SEARCH.format(quote(value)))


_APP_ALIASES = {
    "记事本": "notepad", "笔记本": "notepad", "文本": "notepad",
    "计算器": "calc", "画图": "mspaint", "资源管理器": "explorer", "文件管理器": "explorer",
    "任务管理器": "taskmgr", "命令行": "cmd", "命令提示符": "cmd", "终端": "wt",
    "powershell": "powershell", "设置": "ms-settings:", "系统设置": "ms-settings:",
    "浏览器": "msedge", "edge": "msedge", "谷歌浏览器": "chrome", "chrome": "chrome",
    "微信": "wechat", "weixin": "wechat", "qq": "qq", "钉钉": "dingtalk",
    "网易云音乐": "cloudmusic", "网易云": "cloudmusic", "音乐": "cloudmusic",
    "哔哩哔哩": "bilibili", "b站": "bilibili", "抖音": "douyin",
    "vscode": "code", "代码": "code", "编辑器": "code",
    "相机": "microsoft.windows.camera:", "应用商店": "ms-windows-store:",
    "回收站": "shell:RecycleBinFolder", "此电脑": "shell:MyComputerFolder",
    "截图": "ms-screenclip:", "便签": "notepad",
}

