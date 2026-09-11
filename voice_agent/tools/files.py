# -*- coding: utf-8 -*-
"""文件与记忆：列目录、找文件、读文件、长期记忆。

路径支持「桌面」「下载」「D盘」这类口语说法 —— 用户不会念完整路径。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ._shared import HOME, MEMORY_FILE
from .windows import _desktop

__all__ = ["list_files", "search_files", "read_file", "remember", "recall", "_resolve_path"]

def list_files(path: str = "") -> str:
    """列出一个目录里的文件。"""
    raw = (path or "").strip()
    target = _resolve_path(raw) if raw else _desktop()
    if not target.is_dir():
        return "找不到这个文件夹"
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except PermissionError:
        return "没有权限看这个文件夹"
    if not entries:
        return "这个文件夹是空的"
    folders = [p.name for p in entries if p.is_dir()][:8]
    files = [p.name for p in entries if p.is_file()][:8]
    parts = []
    if folders:
        parts.append("文件夹有 " + "、".join(folders))
    if files:
        parts.append("文件有 " + "、".join(files))
    return target.name + " 里一共 " + str(len(entries)) + " 项；" + "；".join(parts)


def search_files(name: str = "", root: str = "", limit: int = 10) -> str:
    """按文件名找文件（默认在用户目录下找）。"""
    keyword = (name or "").strip().lower()
    if not keyword:
        return "没说要找什么文件"
    base = _resolve_path(root) if root else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录"
    matches: list[Path] = []
    scanned = 0
    try:
        for current, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith((".", "$"))][:40]
            scanned += 1
            if scanned > 4000:
                break
            for filename in files:
                if keyword in filename.lower():
                    matches.append(Path(current) / filename)
                    if len(matches) >= int(limit):
                        break
            if len(matches) >= int(limit):
                break
    except Exception as exc:
        return "搜索出错了：" + str(exc)[:60]
    if not matches:
        return "没找到名字里有" + name + "的文件"
    return "找到 " + str(len(matches)) + " 个：" + "、".join(p.name for p in matches[:5])


def read_file(path: str = "", max_chars: int = 800) -> str:
    """读一个文本文件的内容。"""
    target = _resolve_path((path or "").strip())
    if not target.is_file():
        return "找不到这个文件"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return "读不了这个文件：" + str(exc)[:60]
    content = re.sub(r"\s+", " ", content).strip()
    if len(content) > int(max_chars):
        content = content[: int(max_chars)] + "……后面还有"
    return target.name + " 的内容是：" + content


def remember(text: str = "", key: str = "") -> str:
    """记一件事到长期记忆里。"""
    value = (text or "").strip()
    if not value:
        return "没说要记什么"
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        items = json.loads(MEMORY_FILE.read_text(encoding="utf-8")) if MEMORY_FILE.is_file() else []
    except Exception:
        items = []
    items.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "key": (key or "").strip(),
        "text": value,
    })
    MEMORY_FILE.write_text(json.dumps(items[-200:], ensure_ascii=False, indent=2), encoding="utf-8")
    return "好的，我记住了"


def recall(query: str = "") -> str:
    """回忆之前记下的事。"""
    if not MEMORY_FILE.is_file():
        return "我还没记过什么事"
    try:
        items = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return "记忆文件读不出来"
    keyword = (query or "").strip()
    if keyword:
        items = [it for it in items if keyword in it.get("text", "") or keyword in it.get("key", "")]
    if not items:
        return "没想起来相关的事"
    recent = items[-3:]
    return "我记得：" + "；".join(it.get("text", "") for it in recent)


def _resolve_path(raw: str) -> Path:
    """把「桌面」「D盘」「下载」这类口语路径解析成真实路径。"""
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return HOME
    lowered = value.lower()
    named = {
        "桌面": _desktop(), "desktop": _desktop(),
        "下载": HOME / "Downloads", "downloads": HOME / "Downloads",
        "文档": HOME / "Documents", "documents": HOME / "Documents",
        "图片": HOME / "Pictures", "pictures": HOME / "Pictures",
        "音乐": HOME / "Music", "music": HOME / "Music",
        "视频": HOME / "Videos", "videos": HOME / "Videos",
        "主目录": HOME, "用户目录": HOME, "home": HOME,
    }
    for key, path in named.items():
        if lowered == key.lower():
            return path
    drive = re.fullmatch(r"([A-Za-z])\s*(盘|:)?", value)
    if drive:
        return Path(drive.group(1).upper() + ":\\")
    expanded = os.path.expandvars(os.path.expanduser(value))
    return Path(expanded)







