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

__all__ = ["list_files", "search_files", "read_file", "write_file", "edit_file",
           "find_files", "grep_files", "open_path", "remember", "recall", "_resolve_path"]

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
    raw = (path or "").strip()
    if not raw:
        # 空路径以前会被解析成"桌面"，于是用户听到的是"找不到这个文件" ——
        # 听起来像文件真的没了。缺参数就直说缺参数。
        return "没说要读哪个文件，可以说「读一下桌面的报告.txt」"
    target = _resolve_path(raw)
    if not target.is_file():
        return "找不到这个文件：" + raw
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return "读不了这个文件：" + str(exc)[:60]
    content = re.sub(r"\s+", " ", content).strip()
    if len(content) > int(max_chars):
        content = content[: int(max_chars)] + "……后面还有"
    return target.name + " 的内容是：" + content


def write_file(path: str = "", content: str = "", mode: str = "overwrite") -> str:
    """写文本文件。mode=append 时追加到末尾。"""
    target = _resolve_path((path or "").strip())
    body = str(content or "")
    if not str(path or "").strip():
        return "没说要写到哪个文件"
    if not body:
        return "内容是空的，没写"
    append = str(mode or "").strip().lower() in ("append", "追加", "a")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError as exc:
        return "写不了这个文件：" + str(exc)[:60]
    return ("已经追加到 " + target.name) if append else ("已经写入 " + target.name)


def edit_file(path: str = "", old: str = "", new: str = "") -> str:
    """把文件里的一段文字换成另一段（只换第一处）。"""
    target = _resolve_path((path or "").strip())
    needle = str(old or "")
    if not needle:
        return "没说要改哪一段"
    if not target.is_file():
        return "找不到这个文件"
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return "读不了这个文件：" + str(exc)[:60]
    if needle not in content:
        return "文件里没有这段文字，没改动"
    target.write_text(content.replace(needle, str(new or ""), 1), encoding="utf-8")
    return "已经改好 " + target.name


def find_files(pattern: str = "", root: str = "", limit: int = 20) -> str:
    """按通配符找文件，例如 *.pdf、report?.docx。比 search_files 精确。"""
    key = (pattern or "").strip()
    if not key:
        return "没说要找什么样的文件"
    if not any(ch in key for ch in "*?["):
        key = "*" + key + "*"
    base = _resolve_path(root) if root else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录"
    matches: list[Path] = []
    try:
        for found in base.rglob(key):
            if found.is_file():
                matches.append(found)
                if len(matches) >= int(limit):
                    break
    except Exception as exc:  # noqa: BLE001
        return "搜索出错了：" + str(exc)[:60]
    if not matches:
        return "没找到匹配 " + pattern + " 的文件"
    return "找到 " + str(len(matches)) + " 个：" + "、".join(p.name for p in matches[:6])


def grep_files(pattern: str = "", root: str = "", include: str = "*.txt") -> str:
    """在文件内容里搜一段文字（默认只在文本文件里找）。"""
    key = (pattern or "").strip()
    if not key:
        return "没说要找什么内容"
    base = _resolve_path(root) if root else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录"
    hits: list[str] = []
    scanned = 0
    try:
        for found in base.rglob(include or "*"):
            if not found.is_file() or found.stat().st_size > 2 * 1024 * 1024:
                continue
            scanned += 1
            if scanned > 800:
                break
            try:
                for number, line in enumerate(
                        found.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if key in line:
                        hits.append(found.name + " 第 " + str(number) + " 行：" + line.strip()[:40])
                        break
            except OSError:
                continue
            if len(hits) >= 8:
                break
    except Exception as exc:  # noqa: BLE001
        return "搜索出错了：" + str(exc)[:60]
    if not hits:
        return "没找到包含「" + pattern + "」的文件"
    return "找到 " + str(len(hits)) + " 处：" + "；".join(hits[:5])


def open_path(path: str = "") -> str:
    """用系统默认程序打开一个文件或文件夹。"""
    raw = (path or "").strip()
    if not raw:
        return "没说要打开什么"
    target = _resolve_path(raw)
    if not target.exists():
        return "找不到 " + raw
    from .windows import _launch  # noqa: PLC0415

    if _launch(str(target)):
        return "已经打开 " + target.name
    return "打不开 " + str(target)


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
    """把「桌面」「D盘」「下载」这类口语路径解析成真实路径。

    顺序有讲究：先看是不是绝对路径，再看口语别名。以前不分青红皂白先建一张
    别名表 —— 里面那个"桌面"要跑一次 PowerShell，于是**每次读写绝对路径都要
    起一个 powershell 进程**（几百毫秒），还可能在受限环境下直接抛异常。
    """
    value = (raw or "").strip().strip('"').strip("'")
    if not value:
        return HOME
    expanded = os.path.expandvars(os.path.expanduser(value))
    # 已经是完整路径就别查别名了（C:\x、\\\\server\\share、/tmp 都算）
    if Path(expanded).is_absolute() or re.match(r"^[A-Za-z]:", expanded):
        return Path(expanded)
    lowered = value.lower()
    named = {
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
    if lowered in ("桌面", "desktop"):
        return _desktop()
    drive = re.fullmatch(r"([A-Za-z])\s*(盘|:)?", value)
    if drive:
        return Path(drive.group(1).upper() + ":\\")
    return Path(expanded)







