# -*- coding: utf-8 -*-
"""Windows 系统层：窗口、键鼠输入、音量媒体、电源、剪贴板、命令执行。

都是薄薄一层 Win32 / PowerShell 调用。约定不变：**永不抛异常**，
失败也返回一句给人听的中文。
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from ._shared import HOME, screenshot_dir

_CMD_SYNTAX = re.compile(r'[&|<>^"\']')


__all__ = [
    "type_text", "press_keys", "volume", "media_control", "window",
    "lock_screen", "power", "run_command", "clipboard", "screenshot",
    "list_windows", "focus_window", "list_processes", "kill_process", "wait",
]

def _decode(raw: bytes) -> str:
    """把子进程的字节解成文字。

    PowerShell 的 6 个流编码并不一致：我们设了 [Console]::OutputEncoding，
    所以**正常输出**是 UTF-8，但**错误记录**（Copy-Item 找不到文件那种）
    经常还是系统的 ANSI 代码页。一律按 UTF-8 解就会变成
    "�Ҳ���·��" 这种乱码 —— 模型读不懂，只能瞎试。
    这里先试 UTF-8，出现替换字符就改用 ANSI（mbcs）再解一次。
    """
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if "\ufffd" in text:
        try:
            return raw.decode("mbcs", errors="replace")
        except (LookupError, ValueError):
            return text
    return text


def _ps(script: str, timeout: float = 20.0, stdin_text: str | None = None) -> str:
    """跑一段 PowerShell，返回 stdout（失败返回空串）。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_UTF8 + script],
            input=stdin_text,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return _decode(proc.stdout or b"").strip()
    except Exception:
        return ""


def _launch(target: str, args: list[str] | None = None) -> bool:
    """用系统默认方式打开一个 exe / .lnk / 文档 / URL，不阻塞。"""
    extra = " ".join(str(a) for a in (args or []) if str(a).strip())
    try:
        if extra:
            os.startfile(target, arguments=extra)  # noqa: S606 - Windows 专用
        else:
            os.startfile(target)  # noqa: S606 - Windows 专用，正是我们要的
        return True
    except Exception:
        pass
    if _CMD_SYNTAX.search(target):
        return False   # 宁可打不开，也不能让名字里的 & 变成第二条命令
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", target],
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:
        return False


def _desktop() -> Path:
    value = _ps("[Environment]::GetFolderPath('Desktop')", timeout=8.0)
    path = Path(value) if value else HOME / "Desktop"
    return path if path.is_dir() else HOME / "Desktop"


def _send_input(*inputs: "_INPUT") -> None:
    if _user32 is None:
        return
    array = (_INPUT * len(inputs))(*inputs)
    sent = _user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT))
    # 必须检查返回值。SendInput 失败只返回 0（例如结构体大小不对、目标窗口
    # 权限更高被 UIPI 拦下），静默忽略的话用户只会看到"打了字但屏幕上没有"。
    if sent != len(inputs):
        raise RuntimeError("系统拒绝了这次键盘事件（SendInput=" + str(sent) + "/"
                           + str(len(inputs)) + "，错误码 "
                           + str(ctypes.get_last_error()) + "）")


def _key_input(vk: int, up: bool = False) -> "_INPUT":
    item = _INPUT()
    item.type = _INPUT_KEYBOARD
    item.u.ki = _KEYBDINPUT(vk, 0, _KEYEVENTF_KEYUP if up else 0, 0, None)
    return item


def _unicode_input(char: str, up: bool = False) -> "_INPUT":
    item = _INPUT()
    item.type = _INPUT_KEYBOARD
    flags = _KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0)
    item.u.ki = _KEYBDINPUT(0, ord(char), flags, 0, None)
    return item


def _tap_media(action: str, times: int = 1) -> bool:
    vk = _VK_MEDIA.get(action)
    if vk is None or _user32 is None:
        return False
    for _ in range(max(1, times)):
        _user32.keybd_event(vk, 0, 0, 0)
        _user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
    return True


def _find_start_menu_app(name: str) -> Path | None:
    """在开始菜单里按名字找快捷方式（中文名大多走这条路）。"""
    roots = [
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("ProgramData", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    key = name.strip().lower()
    if not key:
        return None
    candidates: list[tuple[int, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.lnk"):
            stem = path.stem.lower()
            if stem == key:
                return path
            if key in stem or stem in key:
                candidates.append((abs(len(stem) - len(key)), path))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]
    return None


def volume(action: str = "up", steps: int = 0) -> str:
    """调节音量（按媒体键，每次约 2%）。"""
    what = (action or "up").strip().lower()
    mapping = {
        "up": ("volume_up", 5), "大": ("volume_up", 5), "增大": ("volume_up", 5), "调大": ("volume_up", 5),
        "down": ("volume_down", 5), "小": ("volume_down", 5), "减小": ("volume_down", 5), "调小": ("volume_down", 5),
        "mute": ("mute", 1), "静音": ("mute", 1), "unmute": ("mute", 1),
        "max": ("volume_up", 25), "最大": ("volume_up", 25),
        "min": ("volume_down", 25),
    }
    key, default_steps = mapping.get(what, ("volume_up", 5))
    try:
        # 每按一次要 sleep，不封顶的话 steps=100000 会把这一轮卡住将近一小时
        count = max(1, min(int(steps) if steps else default_steps, 60))
    except (TypeError, ValueError):
        count = default_steps
    if not _tap_media(key, count):
        return "调不了音量，这台系统不支持媒体键"
    if key == "mute":
        # unmute 按的是同一个静音键（Windows 的静音是个开关），文案不能写死"静音了"
        return "已经切换了静音开关（原本静音的话现在就有声音了）" if what == "unmute" else "已经把声音静音了"
    return "音量已经" + ("调到最大" if what in ("max", "最大") else "调小了" if key == "volume_down" else "调大了")


def media_control(action: str = "play_pause") -> str:
    """播放 / 暂停 / 上一首 / 下一首 / 停止。"""
    what = (action or "play_pause").strip().lower()
    mapping = {
        "play": "play_pause", "pause": "play_pause", "播放": "play_pause", "暂停": "play_pause",
        "play_pause": "play_pause", "toggle": "play_pause",
        "next": "next", "下一首": "next", "下一个": "next",
        "prev": "prev", "previous": "prev", "上一首": "prev", "上一个": "prev",
        "stop": "stop", "停止": "stop",
    }
    key = mapping.get(what, "play_pause")
    if not _tap_media(key):
        return "控制不了播放器"
    words = {"play_pause": "已经切换播放和暂停", "next": "已经切到下一首", "prev": "已经切回上一首", "stop": "已经停止播放"}
    return words.get(key, "已经发送了媒体控制键")


def screenshot(monitor: int = 0, region: str = "", name: str = "") -> str:
    """截屏并存到「图片」目录。

    monitor：1 = 主屏，2、3… 其它屏（从左到右），0 = 全部（整个虚拟桌面）。
    region ：框选过的「范围1」「点2」，或者 "左,上,右,下" 四个数。
    name   ：存成 screen-<name>-时间.png，方便回头找。
    """
    from .. import marks as marks_mod
    from .. import screen as screen_mod
    from PIL import Image  # noqa: PLC0415

    rect = marks_mod.resolve_region(region) if str(region or "").strip() else None
    if str(region or "").strip() and rect is None:
        return "看不懂这个范围：" + str(region) + "（可以先用「框选」框一块，或写成 左,上,右,下）"
    try:
        shot = screen_mod.grab_screen(region=rect, monitor=int(monitor or 0))
    except Exception as exc:  # noqa: BLE001
        return "截屏失败：" + str(exc)[:80]
    if shot.size == 0:
        return "截屏失败：没有拿到画面"
    folder = screenshot_dir(create=True)
    tag = "-" + re.sub(r"\W+", "", str(name))[:16] if str(name or "").strip() else ""
    path = folder / ("screen" + tag + "-"
                             + datetime.now().strftime("%Y%m%d-%H%M%S") + ".png")
    try:
        Image.fromarray(shot[:, :, ::-1]).save(path)
    except Exception as exc:  # noqa: BLE001
        return "截屏存不下来：" + str(exc)[:80]
    where = ("范围 " + str(region)) if rect else (
        ("第 " + str(int(monitor)) + " 块屏幕") if int(monitor or 0) > 0 else "整个桌面")
    # **必须给完整路径**：以前这里写死"存到图片文件夹里"，而数据目录已经改成
    # 可配置的了 —— 模型照着"图片文件夹"去找，当然找不到，于是一路瞎试。
    return ("已经截屏（" + where + "，" + str(shot.shape[1]) + "×" + str(shot.shape[0])
            + "），存在：" + str(path))


def open_folder(path: str = "", label: str = "") -> str:
    """在资源管理器里打开一个文件夹。"""
    raw = str(path or "").strip()
    if not raw:
        return "没说要打开哪个文件夹"
    expanded = os.path.expandvars(os.path.expanduser(raw))
    from pathlib import Path  # noqa: PLC0415

    target = Path(expanded)
    if not target.is_dir():
        return "找不到这个文件夹：" + raw
    opened = _launch(str(target))
    if not opened:
        # 个别机器上 startfile 对目录不灵，退回 explorer
        try:
            subprocess.Popen(["explorer", str(target)],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            opened = True
        except Exception:  # noqa: BLE001
            opened = False
    return ("已经打开文件夹 " + (label or target.name)) if opened else ("打不开 " + raw)


def clipboard(action: str = "get", text: str = "") -> str:
    """读 / 写系统剪贴板。"""
    what = (action or "get").strip().lower()
    if what in ("set", "写", "复制", "copy"):
        if not text:
            return "没说要复制什么"
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 _PS_UTF8 + "Set-Clipboard -Value ([Console]::In.ReadToEnd())"],
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:  # noqa: BLE001
            return "复制到剪贴板失败：" + str(exc)[:60]
        if proc.returncode != 0:
            return "复制到剪贴板失败：" + (_decode(proc.stderr or b"").strip()[:80] or "返回码 " + str(proc.returncode))
        return "已经复制到剪贴板"
    value = _ps("Get-Clipboard -Raw", timeout=10.0)
    if not value:
        # "读不到"和"空"是两回事，别把失败说成空
        return "剪贴板里没有文字（或者读不出来）"
    return "剪贴板里是：" + re.sub(r"\s+", " ", value)[:200]


def type_text(text: str = "") -> str:
    """把一段文字打到当前光标所在的位置。"""
    value = (text or "").strip()
    if not value:
        return "没说要输入什么"
    if _user32 is None:
        return "这个系统不支持模拟键盘"
    limit = 2000
    if len(value) > limit:
        value = value[:limit]
    for char in value:
        _send_input(_unicode_input(char), _unicode_input(char, up=True))
        time.sleep(0.005)
    return "已经输入了 " + str(len(value)) + " 个字" + ("（超出部分已省略）" if len(text.strip()) > limit else "")


def press_keys(keys: str = "") -> str:
    """按快捷键，例如 win+d、ctrl+shift+s、alt+f4。"""
    combo = [part.strip().lower() for part in re.split(r"[+\s]+", (keys or "").strip()) if part.strip()]
    if not combo or _user32 is None:
        return "没说清楚按什么键"
    vks: list[int] = []
    for part in combo:
        if part in _KEY_NAMES:
            vks.append(_KEY_NAMES[part])
        elif len(part) == 1:
            vks.append(ord(part.upper()))
        else:
            return "不认识这个按键：" + part
    for vk in vks:
        _send_input(_key_input(vk))
    for vk in reversed(vks):
        _send_input(_key_input(vk, up=True))
    return "已经按下 " + "+".join(combo)


def window(action: str = "minimize_all") -> str:
    """窗口操作：显示桌面 / 关闭当前窗口 / 切换窗口。"""
    what = (action or "").strip().lower()
    if any(key in what for key in ("desktop", "桌面", "最小化", "minimize")):
        return press_keys("win+d") + "，已经最小化所有窗口"
    if any(key in what for key in ("close", "关闭")):
        return press_keys("alt+f4") + "，已经关闭当前窗口"
    if any(key in what for key in ("switch", "切换", "next")):
        return press_keys("alt+tab") + "，已经切换窗口"
    return "不知道该做什么窗口操作"


def list_windows(filter: str = "") -> str:
    """列出当前打开的窗口标题。

    只列"有标题、且看得见"的顶层窗口 —— 一棵窗口树里绝大多数是隐藏的辅助窗口，
    念给用户听毫无意义。
    """
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
        "Select-Object -ExpandProperty MainWindowTitle"
    )
    try:
        out = _ps(script, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        return "读不到窗口列表：" + str(exc)[:60]
    titles = [line.strip() for line in str(out or "").splitlines() if line.strip()]
    key = (filter or "").strip().lower()
    if key:
        titles = [t for t in titles if key in t.lower()]
    if not titles:
        probe = str(_ps("'ok'", timeout=10.0) or "").strip()
        if "ok" not in probe:
            return "读不到窗口列表（PowerShell 没能执行），这不代表没有窗口"
        return ("没有匹配的窗口" if key else "现在没有打开的窗口")
    head = titles[:8]
    more = ("，还有 " + str(len(titles) - len(head)) + " 个") if len(titles) > len(head) else ""
    return "打开着 " + str(len(titles)) + " 个窗口：" + "、".join(head) + more


def focus_window(title: str = "") -> str:
    """把某个窗口切到最前面。"""
    key = (title or "").strip()
    if not key:
        return "没说要切到哪个窗口"
    script = (
        "$w = Get-Process | Where-Object { $_.MainWindowTitle -like '*" + key.replace("'", "''") + "*' } | "
        "Select-Object -First 1; "
        "if ($w) { "
        "$sig = '[DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);"
        "[DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int c);'; "
        "$t = Add-Type -MemberDefinition $sig -Name W -Namespace N -PassThru; "
        "$t::ShowWindow($w.MainWindowHandle, 9) | Out-Null; "
        "$t::SetForegroundWindow($w.MainWindowHandle) | Out-Null; $w.MainWindowTitle }"
    )
    try:
        out = _ps(script, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        return "切换窗口失败了：" + str(exc)[:60]
    found = str(out or "").strip().splitlines()
    found = found[-1].strip() if found else ""
    if found:
        return "已经把「" + found + "」切到前面了"
    return "没找到标题里有「" + key + "」的窗口"


def list_processes(filter: str = "", top: int = 5) -> str:
    """列出吃资源最多的进程。"""
    key = (filter or "").strip()
    count = max(1, min(int(top or 5), 15))
    where = ("$_.ProcessName -like '*" + key.replace("'", "''") + "*'") if key else "$true"
    script = (
        "Get-Process | Where-Object { " + where + " } | "
        "Sort-Object -Property WorkingSet64 -Descending | Select-Object -First " + str(count) + " | "
        "ForEach-Object { '{0}|{1:N0}' -f $_.ProcessName, ($_.WorkingSet64/1MB) }"
    )
    try:
        out = _ps(script, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        return "读不到进程列表：" + str(exc)[:60]
    rows = [line.strip() for line in str(out or "").splitlines() if "|" in line]
    if not rows:
        # 空结果有两种可能：真的没有，和**根本没读出来**（PowerShell 起不来）。
        # 一律说成"没有"就是在撒谎，用户会以为自己机器上真的没有这个进程。
        probe = str(_ps("'ok'", timeout=10.0) or "").strip()
        if "ok" not in probe:
            return "读不到进程列表（PowerShell 没能执行），这不代表没有这个进程"
        return ("没有名字里带「" + key + "」的进程" if key else "现在没有能列出来的进程")
    parts = []
    for row in rows:
        name, _, size = row.partition("|")
        parts.append(name.strip() + " 占 " + size.strip() + " MB")
    return ("匹配到的进程：" if key else "最占内存的进程：") + "，".join(parts)


def kill_process(name: str = "", force: bool = True) -> str:
    """结束一个进程（敏感操作，需要确认）。"""
    key = (name or "").strip()
    if not key:
        return "没说要结束哪个进程"
    if any(ch in key for ch in "*?[]"):
        # Get-Process -Name '*' 会命中**所有**进程，一条 Stop-Process -Force
        # 就能把整台机器上的程序全杀掉。通配符在这里没有任何正当用途。
        return "进程名里不能带通配符（* ? [ ]），请给出确切的程序名"
    safe = key.replace("'", "''")
    script = (
        "$p = Get-Process -Name '" + safe + "' -ErrorAction SilentlyContinue; "
        "if (-not $p) { $p = Get-Process | Where-Object { $_.MainWindowTitle -like '*" + safe + "*' } }; "
        "if (-not $p) { 'NONE' } else { "
        # 逐个杀并统计**真正成功**的：以前 $n 数的是"匹配到几个"，
        # 杀失败（权限不够、进程已退出）也照样报"已经结束 N 个"。
        "$ok = 0; $fail = 0; "
        "foreach ($x in $p) { try { Stop-Process -Id $x.Id" + (" -Force" if force else "")
        + " -ErrorAction Stop; $ok++ } catch { $fail++ } }; "
        "'OK ' + $ok + ' ' + $fail }"
    )
    try:
        out = str(_ps(script, timeout=20.0) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return "结束进程失败了：" + str(exc)[:60]
    if out.startswith("NONE"):
        return "没有找到叫「" + key + "」的进程"
    if out.startswith("OK"):
        parts = out.split()
        done = parts[1] if len(parts) > 1 else "0"
        failed = parts[2] if len(parts) > 2 else "0"
        if done == "0":
            return "没能结束「" + key + "」：可能是权限不够（试试用管理员运行）"
        text = "已经结束 " + key + ("（" + done + " 个进程）" if done != "1" else "")
        return (text + "；另有 " + failed + " 个没杀掉") if failed != "0" else text
    return "结束进程的结果看不懂：" + out[:60]


def wait(seconds: int = 1) -> str:
    """等一会儿再做下一步。"""
    try:
        span = max(0.1, min(float(seconds or 1), 30.0))
    except (TypeError, ValueError):
        span = 1.0
    time.sleep(span)
    return "等了 " + str(round(span, 1)) + " 秒"


def lock_screen() -> str:
    """锁屏。"""
    if os.name == "nt":
        ctypes.windll.user32.LockWorkStation()
        return "已经锁屏了"
    return "这个系统不支持锁屏"


def power(action: str = "", delay: int = 0) -> str:
    """关机 / 重启 / 睡眠 / 注销（敏感操作，需要确认）。

    action 默认**留空而不是 shutdown**：模型把参数拼坏、网关回了个空对象时，
    "默认关机"是能想象到的最坏兜底。没说要做什么就什么都不做。
    """
    what = (action or "").strip().lower()
    if not what:
        return "没说清楚要关机、重启、睡眠还是注销，这次先不动"
    try:
        # delay 以前**根本没被用**：确认提示念的是"延迟 60 秒，确认吗"，
        # 用户点头之后却是立刻关机 —— 提示撒谎比不做延迟更糟。
        wait = max(0, min(int(delay or 0), 3600))
    except (TypeError, ValueError):
        wait = 0
    after = ("，" + str(wait) + " 秒后执行") if wait else ""
    if any(key in what for key in ("shutdown", "关机")):
        subprocess.Popen(["shutdown", "/s", "/t", str(wait), "/f"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "好，正在关机" + after + "（想取消就说「取消关机」）" if wait else "正在关机"
    if any(key in what for key in ("restart", "reboot", "重启")):
        subprocess.Popen(["shutdown", "/r", "/t", str(wait), "/f"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "好，正在重启" + after if wait else "正在重启"
    if any(key in what for key in ("cancel", "取消", "别关", "不关")):
        try:
            proc = subprocess.run(["shutdown", "/a"], capture_output=True, timeout=10,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if proc.returncode == 0:
                return "已经取消了计划中的关机"
        except Exception:  # noqa: BLE001
            pass
        return "没有可取消的关机计划（或者已经来不及了）"
    if any(key in what for key in ("sleep", "睡眠", "休眠")):
        if wait:
            # 睡眠/注销的接口没有延迟参数，只能自己等
            threading.Timer(wait, lambda: subprocess.Popen(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))).start()
            return "好，" + str(wait) + " 秒后进入睡眠"
        subprocess.Popen(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "进入睡眠了"
    if any(key in what for key in ("logoff", "注销")):
        if wait:
            threading.Timer(wait, lambda: _ps("logoff")).start()
            return "好，" + str(wait) + " 秒后注销"
        _ps("logoff")
        return "正在注销"
    return "不支持的电源操作"


def run_command(command: str = "", timeout: int = 30) -> str:
    """执行一条系统命令并返回输出（敏感操作，需要确认）。"""
    line = (command or "").strip()
    if not line:
        return "没说要执行什么命令"
    try:
        proc = subprocess.Popen(
            line,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001
        return "命令执行失败：" + str(exc)[:80]
    try:
        raw_out, raw_err = proc.communicate(timeout=min(max(int(timeout), 3), 120))
    except subprocess.TimeoutExpired:
        # 光 kill 掉 cmd.exe 是不够的：它派生的孙进程还活着（继续吃 CPU、
        # 继续攥着 stdout 管道），而随后的 communicate() 会一直等管道关闭 ——
        # 这条工具就再也不返回了，"命令执行超时了"根本说不出口。
        # taskkill /T 连整棵进程树一起收，communicate 再带一次超时兜底。
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass
        proc.kill()
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return "命令执行超时了（超过 " + str(timeout) + " 秒，已经强制结束）"
    # 用同一个"先 UTF-8 再 ANSI"的解码：命令的错误输出经常是系统代码页，
    # 一律按 UTF-8 解会变成乱码，模型看不懂就会开始瞎试别的办法。
    output = (_decode(raw_out or b"") + " " + _decode(raw_err or b"")).strip()
    output = re.sub(r"\s+", " ", output)
    code = int(proc.returncode or 0)
    if not output:
        return ("命令执行完了，没有输出，返回码 " + str(code)) if code == 0 else (
            "命令失败，返回码 " + str(code) + "，没有输出")
    if len(output) > 400:
        output = output[:400] + "……后面还有"
    # 返回码非 0 就是**失败**：以前只要有输出就当成成功上报，
    # 模型会把一条报错的命令当成做成了。
    if code != 0:
        return "命令失败（返回码 " + str(code) + "）：" + output
    return "命令输出：" + output


_user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None


_VK_MEDIA = {
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
    "next": 0xB0,
    "prev": 0xB1,
    "stop": 0xB2,
    "play_pause": 0xB3,
}


_KEY_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


# ── Win32 常量与结构体 ──
# 这些都放在文件末尾（函数后面）：Python 是调用时解析全局名，所以没问题，
# 而这样上半部分读起来就是纯粹的业务逻辑。
#
# 提醒：这一段当初在 tools.py 拆成 tools/ 包时被漏掉了，后果是 _ps() 里
# NameError 被 except 吞掉、永远返回空串（音量、截屏、系统信息全部静默失效），
# type_text / press_keys 则直接抛异常。加回来之后才真正能用。

# PowerShell 的输出编码：Windows 控制台默认不是 UTF-8，不先设一下，
# 中文路径、窗口标题回来就是乱码
_PS_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    )


class _INPUTUNION(ctypes.Union):
    # 没有 padding 成员：_INPUT 必须是正好 40 字节（64 位），否则 SendInput
    # 会整条拒绝并返回 0 —— 不抛异常、不报错，表现就是"打字、快捷键都没反应"。
    _fields_ = (("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.c_ulong), ("u", _INPUTUNION))


_KEYEVENTF_KEYUP = 0x0002


_KEYEVENTF_UNICODE = 0x0004


_INPUT_KEYBOARD = 1

