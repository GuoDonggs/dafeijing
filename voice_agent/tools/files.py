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

from ._shared import HOME, last_utterance, memory_file
from .windows import _desktop

__all__ = ["list_files", "search_files", "read_file", "write_file", "edit_file",
           "find_files", "grep_files", "open_path", "remember", "recall", "_resolve_path",
           "spoken_location"]

#: 这几个目录里不会有用户要找的文档，但常常占了几十万个文件 ——
#: 「在 D 盘里找一下」最耗时的就是它们（系统目录、回收站、依赖缓存）。
_SKIP_DIRS = frozenset({
    "$recycle.bin", "system volume information", "$winreagent", "recovery",
    "windows", "windows.old", "program files", "program files (x86)", "programdata",
    "appdata", "node_modules", ".git", "__pycache__", ".venv", "venv", ".tox",
    "site-packages", ".cache", ".gradle", ".nuget",
})

#: 找文件的预算。用户说「D盘下」时那是一次**整盘扫描**：不设上限的话，
#: 一个不存在的文件名能让助手沉默好几分钟（用户只会以为它死了）。
SEARCH_SECONDS = 6.0
SEARCH_MAX_ENTRIES = 80000

#: 口语路径里的分隔说法（「D盘**下的**桌面**下的**对焦文件夹」）
_CHAIN_SEP = re.compile(r"(?:下面的|下面|下的|里的|里面的|之中的|中的|上面|里面|之中|中|里)")
#: 每一段末尾常带的类别词（「对焦文件夹」→「对焦」）
_CHAIN_TAIL = re.compile(r"(文件夹|目录|文件|路径)$")
#: 引子（「**我在**文档下的…」）：短、且不带英文数字，才把这段后缀当目录名认。
#: 光看 endsWith 会把 "mymusic" 也认成"音乐"目录，所以带 ASCII 的一律不认。
_LEAD_IN_MAX = 4

#: 口语目录名 → 真实位置（和 _resolve_path 共用一份，别抄成两份）
_NAMED_DIRS = {
    "下载": "Downloads", "downloads": "Downloads",
    "文档": "Documents", "documents": "Documents",
    "图片": "Pictures", "pictures": "Pictures",
    "音乐": "Music", "music": "Music",
    "视频": "Videos", "videos": "Videos",
    "桌面": "Desktop", "desktop": "Desktop",
}


def _is_drive_root(path: Path) -> bool:
    """是不是"一个盘的根"（D:\\）—— 这种起点等于整盘扫描。"""
    text = str(path)
    return bool(re.fullmatch(r"[A-Za-z]:\\?", text))


def _spoken_head(segment: str, base: Path | None = None) -> Path | None:
    """口语路径的第一段解析成真实起点：盘符 / 桌面这类别名 / 应用映射。"""
    if base is not None:
        return base
    text = str(segment or "").strip()
    if not text:
        return None
    drive = re.search(r"([A-Za-z])\s*(?:盘|:)\s*$", text)
    if drive:
        return Path(drive.group(1).upper() + ":\\")
    low = text.lower()
    for name in _NAMED_DIRS:
        if low == name:
            return _resolve_path(name)
        if not low.endswith(name):
            continue
        # 前缀是"我在 / 我的 / 这个"这类中文引子才认（「我在文档下的项目」）。
        # 不能宽松地只看 endsWith：那样 "mymusic" 会被当成"音乐"目录 ——
        # 所以带 ASCII 字母数字的前缀一律不认。
        prefix = low[:-len(name)]
        if not re.search(r"[a-z0-9]", prefix) and len(prefix) <= 4:
            return _resolve_path(name)
    try:
        from .. import screen as screen_mod  # noqa: PLC0415

        found = screen_mod.resolve_app(text)
        if found.get("hit"):
            mapped = Path(str(found.get("target") or ""))
            if mapped.is_dir():
                return mapped
    except Exception:  # noqa: BLE001 - 映射表坏了就当作认不出来
        pass
    return None


def _chain_candidates(tail: list[str]) -> list[list[str]]:
    """每一段都可能带着「文件夹」这种尾巴，也可能不带 —— 两种都试一遍。"""
    variants = [list(tail)]
    for index, part in enumerate(tail):
        stripped = _CHAIN_TAIL.sub("", part).strip()
        if not stripped or stripped == part:
            continue
        # 必须迭代**快照**：直接 for item in variants 的同时又 append 进去，
        # 生成器会跟着变长的列表一直读下去 —— 那不是组合枚举，是无限膨胀
        # （实测直接卡死，一个候选都试不出来）。
        for item in list(variants):
            candidate = item[:index] + [stripped] + item[index + 1:]
            if candidate not in variants:
                variants.append(candidate)
    return variants


def _spoken_chain(text: str, base: Path | None = None) -> Path | None:
    """把「D盘下的桌面下的对焦文件夹」这种口语说法拼成**真实存在**的路径。

    为什么需要它：模型很容易把用户的口语原话当成路径传下来，而
    Path("D盘下的桌面下的对焦文件夹") 只会解析成一个不存在的相对目录。
    拼得出来就用，拼不出来返回 None（调用方按原来的规则处理）——
    只认**真的存在**的目录，绝不凭空造一个路径出来。
    base 只给测试用：把第一段当成相对 base 的目录。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [p.strip(" \u3000,，。.、:：;；!！?？") for p in _CHAIN_SEP.split(raw)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    head = _spoken_head(parts[0], base)
    if head is None:
        return None
    tail = parts[1:]
    # 从长到短试：用户那句话后面往往还跟着一整句别的（「…对焦文件夹里写了一份…」），
    # 越长的候选越可能不存在，短的那个才是他要的目录。
    for count in range(len(tail), 0, -1):
        for candidate_tail in _chain_candidates(tail[:count]):
            try:
                candidate = head.joinpath(*candidate_tail)
                if candidate.is_dir() or candidate.is_file():
                    return candidate
            except (OSError, ValueError):
                continue
    return None


def spoken_location() -> Path | None:
    """用户这句话里点明的那个文件夹（已经确认存在）。

    给找文件的工具用：模型把位置吞掉、只给一个盘符时，靠它把位置找回来。
    """
    return _spoken_chain(last_utterance())


def _join_paths(paths: list[Path], limit: int = 3) -> str:
    """列出命中文件的**完整路径**。

    以前只给文件名，模型拿到「介绍.md」之后没法接着读它 —— 还得再问一次在哪。
    """
    shown = [str(p) for p in paths[:limit]]
    text = "、".join(shown)
    if len(paths) > limit:
        text += "…等 " + str(len(paths)) + " 个"
    return text


def _walk_files(base: Path, match, limit: int,
                seconds: float = SEARCH_SECONDS) -> tuple[list[Path], bool]:
    """在 base 下面找文件，返回（命中, 是不是没扫完就停了）。

    广度优先 + 时限 + 条数上限：用户说「D盘下」的时候这是一次整盘遍历，
    必须能停下来。找不到时那句"没扫完"要如实说出来，不能假装"没有"。
    """
    import time as _time  # noqa: PLC0415
    from collections import deque  # noqa: PLC0415

    if base is None:
        # 起点是 None 时 os.scandir 会去扫**当前工作目录**（打包版的 exe 旁边、
        # 或者 System32）—— 那是静默地找错地方，不如干脆什么都不扫。
        return [], False
    base = Path(base)
    if not base.is_dir():
        return [], False
    matches: list[Path] = []
    scanned = 0
    stopped = False
    deadline = _time.monotonic() + max(0.5, float(seconds))
    queue: deque[Path] = deque([base])
    while queue:
        if _time.monotonic() > deadline or scanned > SEARCH_MAX_ENTRIES:
            stopped = True
            break
        current = queue.popleft()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    scanned += 1
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() in _SKIP_DIRS:
                                continue
                            queue.append(Path(entry.path))
                        elif match(entry.name):
                            matches.append(Path(entry.path))
                            if len(matches) >= limit:
                                return matches, False
                    except OSError:
                        continue
        except OSError:
            continue
    return matches, stopped

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
    count = max(1, min(int(limit or 10), 50))
    base = _resolve_path(root) if str(root or "").strip() else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录：" + str(root)
    match = lambda filename: keyword in filename.lower()  # noqa: E731
    # 用户这句话里点明了文件夹、而模型给的是一整个盘（或者没给）→ 先按他说的找，
    # 见 find_files 里的同一段注释
    hint = spoken_location()
    if hint is not None and hint != base and (_is_drive_root(base) or not str(root or "").strip()):
        found, _stopped = _walk_files(hint, match, count)
        if found:
            return "在 " + str(hint) + " 里找到 " + str(len(found)) + " 个：" + _join_paths(found)
    matches, stopped = _walk_files(base, match, count)
    if not matches:
        return ("在 " + str(base) + " 里没找到名字里有「" + str(name) + "」的文件"
                + ("（范围太大，**没扫完**就停了；给一个更具体的文件夹会快得多）" if stopped else ""))
    text = "在 " + str(base) + " 里找到 " + str(len(matches)) + " 个：" + _join_paths(matches, 4)
    if stopped:
        text += "（范围太大，扫了一部分就停下；想找全就给个更具体的文件夹）"
    return text


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
    return target.name + "（" + str(target.parent) + "）的内容是：" + content


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
    # 回**完整路径**：只报文件名的话，用户和模型事后都不知道它到底写到哪去了
    # （相对路径是按进程的工作目录解析的，双击快捷方式启动时可能是 System32）
    return ("已经追加到 " + str(target)) if append else ("已经写入 " + str(target))


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
    try:
        target.write_text(content.replace(needle, str(new or ""), 1), encoding="utf-8")
    except OSError as exc:
        return "改不了这个文件：" + str(exc)[:60]
    return "已经改好 " + str(target)


def find_files(pattern: str = "", root: str = "", limit: int = 20) -> str:
    """按通配符找文件，例如 *.pdf、report?.docx。比 search_files 精确。"""
    import fnmatch  # noqa: PLC0415

    key = (pattern or "").strip()
    if not key:
        return "没说要找什么样的文件"
    if not any(ch in key for ch in "*?["):
        key = "*" + key + "*"
    count = max(1, min(int(limit or 20), 50))
    base = _resolve_path(root) if str(root or "").strip() else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录：" + str(root)
    match = lambda filename: fnmatch.fnmatch(filename.lower(), key.lower())  # noqa: E731
    # ① 用户在这句话里点明了文件夹，而模型给的是一整个盘（或者压根没给）：
    #    **先按他说的那个文件夹找**。他要的是"那里面的东西"，不是"全盘同名文件"：
    #    整盘扫描又慢，还会翻出一堆同名的无关文件把他带偏。
    explicit_root = bool(str(root or "").strip())
    hint = spoken_location()
    if hint is not None and hint != base and (_is_drive_root(base) or not explicit_root):
        found, _stopped = _walk_files(hint, match, count)
        if found:
            return ("在 " + str(hint) + " 里找到 " + str(len(found)) + " 个：" + _join_paths(found)
                    + "（按你话里说的那个文件夹找的）")
    matches, stopped = _walk_files(base, match, count)
    if not matches:
        return ("在 " + str(base) + " 里没找到匹配 " + str(pattern) + " 的文件"
                + ("（范围太大，**没扫完**就停了。给一个更具体的文件夹，比如 "
                   "D:\\桌面\\对焦，会快得多也不会漏）" if stopped else ""))
    text = "在 " + str(base) + " 里找到 " + str(len(matches)) + " 个：" + _join_paths(matches)
    if stopped:
        text += "（范围太大，扫了一部分就停下；想找全就给个更具体的文件夹）"
    return text


def grep_files(pattern: str = "", root: str = "", include: str = "*.txt") -> str:
    """在文件内容里搜一段文字（默认只在文本文件里找）。"""
    import fnmatch  # noqa: PLC0415

    key = (pattern or "").strip()
    if not key:
        return "没说要找什么内容"
    base = _resolve_path(root) if str(root or "").strip() else HOME
    if not base.is_dir():
        return "找不到搜索的起点目录：" + str(root)
    # 和 find_files 一样：用户点明了文件夹就别整盘翻
    hint = spoken_location()
    if hint is not None and hint != base and (_is_drive_root(base) or not str(root or "").strip()):
        if hint.is_dir():
            base = hint
    wanted = (include or "*").lower()
    # 先有上限地收集候选文件（同样是广度优先 + 时限），再逐个读内容。
    # 少了这道上限，一次「在 D 盘里搜 xxx」会读到天荒地老。
    candidates, stopped = _walk_files(
        base, lambda filename: fnmatch.fnmatch(filename.lower(), wanted), 800)
    hits: list[str] = []
    for found in candidates:
        try:
            if found.stat().st_size > 2 * 1024 * 1024:
                continue
            for number, line in enumerate(
                    found.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if key in line:
                    hits.append(str(found) + " 第 " + str(number) + " 行：" + line.strip()[:40])
                    break
        except OSError:
            continue
        if len(hits) >= 8:
            break
    if not hits:
        return ("在 " + str(base) + " 里没找到包含「" + str(pattern) + "」的文件"
                + ("（范围太大，**没扫完**就停了；给一个更具体的文件夹会快得多）" if stopped
                   else ""))
    text = "找到 " + str(len(hits)) + " 处：" + "；".join(hits[:5])
    if stopped:
        text += "（范围太大，只扫了一部分）"
    return text


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
    MEMORY_FILE = memory_file(create=True)
    items: list = []
    if MEMORY_FILE.is_file():
        try:
            items = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                raise ValueError("记忆文件的顶层不是列表")
        except Exception as exc:  # noqa: BLE001
            # **绝不能**把读不出来的文件当成空表再整表覆盖：那样一次解析失败
            # 就会静默抹掉用户全部长期记忆，还回一句"好的，我记住了"。
            # 改名留底 + 明确报错，让人还有机会把它救回来。
            backup = MEMORY_FILE.with_suffix(MEMORY_FILE.suffix + ".bad")
            try:
                MEMORY_FILE.replace(backup)
                note = "，原文件已改名保留为 " + backup.name
            except OSError:
                note = ""
            return ("记忆文件读不出来（" + str(exc)[:60] + "）" + note
                    + "，这次没有写入，免得把已有的记忆冲掉")
    items.append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "key": (key or "").strip(),
        "text": value,
    })
    try:
        MEMORY_FILE.write_text(json.dumps(items[-200:], ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError as exc:
        return "记不下来（写文件失败）：" + str(exc)[:60]
    return "好的，我记住了（存在 " + str(MEMORY_FILE) + "）"


def recall(query: str = "") -> str:
    """回忆之前记下的事。"""
    MEMORY_FILE = memory_file()
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
    if lowered in ("主目录", "用户目录", "home"):
        return HOME
    # 「桌面\对焦」「D盘\对焦」「下载\报告.txt」：**第一段**是口语写法时，
    # 后面接着写子路径是再自然不过的用法。以前只认"整个值恰好是桌面"，
    # 于是「桌面\对焦」这种写法一路落到"相对路径 → 用户目录"上，
    # 得到 C:\Users\xx\桌面\对焦 —— 不存在，模型只好去满盘找。
    head, _, rest = value.replace("/", "\\").partition("\\")
    head_low = head.strip().lower()
    if rest:
        base: Path | None = None
        if head_low in _NAMED_DIRS:
            base = _desktop() if _NAMED_DIRS[head_low] == "Desktop" else HOME / _NAMED_DIRS[head_low]
        elif head_low in ("主目录", "用户目录", "home"):
            base = HOME
        else:
            drive_prefix = re.fullmatch(r"([A-Za-z])\s*(?:盘|:)?", head.strip())
            if drive_prefix:
                base = Path(drive_prefix.group(1).upper() + ":\\")
        if base is not None:
            return base / rest.replace("\\", os.sep)
    if lowered in _NAMED_DIRS:
        return _desktop() if _NAMED_DIRS[lowered] == "Desktop" else HOME / _NAMED_DIRS[lowered]
    drive = re.fullmatch(r"([A-Za-z])\s*(盘|:)?", value)
    if drive:
        return Path(drive.group(1).upper() + ":\\")
    # 裸文件名：截图和参考图片都放在数据目录里，模型/用户只会说文件名
    # （"screen-屏幕一截图-123416.png"、"下载按钮.png"），这里替它们补全路径。
    if not re.search(r"[\\/]", value):
        from ._shared import reference_dir, screenshot_dir  # noqa: PLC0415

        for folder in (screenshot_dir(), reference_dir()):
            try:
                if folder.is_dir():
                    for candidate in folder.rglob(value):
                        if candidate.is_file() or candidate.is_dir():
                            return candidate
            except OSError:
                continue
    # 最后查一次应用映射表：用户可以把目录映射成好记的名字
    # （「我的项目」→ D 盘的 code 目录），之后 list_files / read_file
    # 直接用那个名字就行 —— 这就是"映射目录"最实用的地方。
    try:
        from .. import screen as screen_mod  # noqa: PLC0415

        # 先试整个值（「我的项目」），再试第一段（「我的项目\src\main.py」）——
        # 映射的是目录，用户接着往下写子路径是很自然的用法。
        head, _, tail = value.replace("/", "\\").partition("\\")
        # 先按第一段找（"我的项目\src" → 映射"我的项目"），再按整个值找。
        # 整个值只允许**精确**命中：resolve_app 还会做"互相包含"的模糊匹配，
        # 拿它当路径用会把"我的项目\报告.txt"整体当成映射名。
        for candidate, rest in ((head, tail), (value, "")):
            found = screen_mod.resolve_app(candidate)
            if not found.get("hit"):
                continue
            # "唯一模糊"也算数：用户说「对焦」而映射叫「对焦介绍」时，精确匹配落空，
            # 但只有一个候选 —— 这比"找不到，于是满盘搜"有用得多。
            if not rest and not found.get("exact") and not found.get("unique"):
                continue
            mapped = Path(os.path.expandvars(os.path.expanduser(
                str(found.get("target") or ""))))
            if not mapped.exists():
                continue
            if not rest:
                return mapped
            joined = mapped / rest.replace("\\", os.sep)
            if joined.exists():
                return joined
    except Exception:  # noqa: BLE001 - 映射表坏了就按普通路径走
        pass
    # 剩下的相对路径（"报告.txt"）挂到**用户目录**，不要让它跟着进程的工作目录走：
    # 双击 exe / 快捷方式启动时 CWD 可能是 System32 或别的地方，写进去的文件
    # 谁也找不到 —— 用户说"写到报告.txt"时的直觉是"我的文档/用户目录那里"。
    if not Path(expanded).is_absolute():
        return HOME / expanded
    return Path(expanded)







