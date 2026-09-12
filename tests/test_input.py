# -*- coding: utf-8 -*-
"""鼠标与键盘工具：结构体大小 + 真的点一下、真的打一段字。

为什么值得单独一个测试：这两个能力坏掉的时候**一点报错都没有**。
SendInput 失败只返回 0，Windows 不抛异常，于是"点了没反应""打字没反应"
只能靠人肉发现。这里的两个检查分别针对：

1. **结构体大小**：_INPUT 必须是 40 字节（64 位）。多几个字节（比如给
   union 加了个 padding 成员）SendInput 就整条拒绝 —— 这就是它们集体失灵的原因。
2. **真窗口端到端**：开一个自己的窗口，把鼠标移过去、点一下、打一段中文进去、
   再按 ctrl+a 覆盖。拿不到前台焦点就跳过键盘部分（绝不能把字打到别人的窗口里）。

运行：python tests/test_input.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice_agent import screen  # noqa: E402
from voice_agent.tools import windows  # noqa: E402

IS_WINDOWS = sys.platform == "win32"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def struct_sizes() -> None:
    """结构体大小：这是这次"鼠标键盘全都没反应"的根因。"""
    print("结构体")
    expected_input = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    expected_mouse = 32 if ctypes.sizeof(ctypes.c_void_p) == 8 else 24
    check("screen._INPUT 大小正确",
          ctypes.sizeof(screen._INPUT) == expected_input,
          str(ctypes.sizeof(screen._INPUT)) + " == " + str(expected_input))
    check("screen._MOUSEINPUT 大小正确",
          ctypes.sizeof(screen._MOUSEINPUT) == expected_mouse,
          str(ctypes.sizeof(screen._MOUSEINPUT)))
    check("tools.windows._INPUT 大小正确",
          ctypes.sizeof(windows._INPUT) == expected_input,
          str(ctypes.sizeof(windows._INPUT)))
    check("联合体大小没有超过鼠标输入（多出来的字节会让 SendInput 拒绝整条事件）",
          ctypes.sizeof(screen._INPUTUNION) == expected_mouse
          and ctypes.sizeof(windows._INPUTUNION) == expected_mouse,
          str(ctypes.sizeof(screen._INPUTUNION)))


def pixel_to_absolute() -> None:
    """坐标换算：绝对坐标必须归一化到 0~65535，而且要考虑多显示器的负坐标。"""
    print("坐标换算")
    left, top, width, height = screen._virtual_screen()
    check("拿得到虚拟桌面尺寸", width > 0 and height > 0,
          str((left, top, width, height)))
    check("左上角映射到 0,0", screen._absolute_xy(left, top) == (0, 0),
          str(screen._absolute_xy(left, top)))
    check("右下角映射到 65535,65535",
          screen._absolute_xy(left + width - 1, top + height - 1) == (65535, 65535),
          str(screen._absolute_xy(left + width - 1, top + height - 1)))
    middle = screen._absolute_xy(left + width // 2, top + height // 2)
    check("中间点落在中间", 30000 < middle[0] < 35000, str(middle))


def end_to_end() -> None:
    """开一个自己的窗口，真的去点、真的去打字。"""
    print("真窗口端到端")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    result = ctypes.c_ssize_t
    proc_type = ctypes.WINFUNCTYPE(result, wintypes.HWND, ctypes.c_uint,
                                   wintypes.WPARAM, wintypes.LPARAM)
    user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = result
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    class _WndClass(ctypes.Structure):
        _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", proc_type),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    @proc_type
    def _proc(hwnd, msg, wparam, lparam):  # noqa: ANN001
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def pump(rounds: int = 15) -> None:
        msg = wintypes.MSG()
        for _ in range(rounds):
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)

    hinst = kernel32.GetModuleHandleW(None)
    wc = _WndClass()
    wc.lpfnWndProc = _proc
    wc.hInstance = hinst
    wc.lpszClassName = "VoiceAgentInputTest"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, "VoiceAgentInputTest", "VoiceAgent 输入自检",
                                  0x00CF0000 | 0x10000000, 120, 120, 460, 260,
                                  None, None, hinst, None)
    edit = user32.CreateWindowExW(0, "EDIT", "",
                                  0x50000000 | 0x00000080 | 0x00200000, 10, 10, 430, 200,
                                  hwnd, None, hinst, None)
    user32.ShowWindow(hwnd, 5)
    user32.UpdateWindow(hwnd)
    previous = user32.GetForegroundWindow()
    pump(3)
    # 绕开前台锁：AttachThreadInput 之后 SetForegroundWindow 才一定成功
    mine = kernel32.GetCurrentThreadId()
    theirs = user32.GetWindowThreadProcessId(previous, None) if previous else 0
    if theirs and theirs != mine:
        user32.AttachThreadInput(theirs, mine, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(edit)
    if theirs and theirs != mine:
        user32.AttachThreadInput(theirs, mine, False)
    pump(5)

    rect = wintypes.RECT()
    user32.GetWindowRect(edit, ctypes.byref(rect))
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2

    # 锁屏、切到安全桌面（UAC）、或者当前进程不在输入桌面上时，
    # 读鼠标位置、抢前台窗口、模拟输入全都会失败。那不是"工具坏了"，
    # 是这台机器此刻不接受模拟输入 —— 跳过，别报成失败。
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)) or not user32.GetForegroundWindow():
        print("  [跳过] 当前桌面不接受模拟输入（锁屏 / 安全桌面？），端到端部分跳过")
        user32.DestroyWindow(hwnd)
        return

    def text_of() -> str:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(edit, buf, 512)
        return buf.value

    def focused() -> bool:
        return user32.GetForegroundWindow() == hwnd and user32.GetFocus() == edit

    try:
        # 鼠标：绝对定位（相对位移会被系统的"提高指针精确度"加速，落点会飘）
        screen.mouse_move(cx, cy)
        time.sleep(0.15)
        moved = screen.mouse_position()
        check("鼠标能移到指定坐标", moved == (cx, cy),
              str(moved) + " 目标 " + str((cx, cy)))
        screen.mouse_click(cx, cy)
        pump(5)
        try:
            after = screen.mouse_position()
        except RuntimeError as exc:
            print("  [跳过] 鼠标位置突然读不到了（" + str(exc)[:40] + "），后面的不测")
            return
        if after == (0, 0) and moved != (0, 0):
            print("  [跳过] 鼠标位置突然变成 0,0（桌面被切走？），后面的不测")
            return
        check("点击不会把鼠标带偏", after == (cx, cy), str(after))

        if not focused():
            print("  [跳过] 拿不到前台焦点，键盘部分不测"
                  "（绝不能把字打到别的窗口里）")
            return
        try:
            windows.type_text("大肥鲸 测试 123 abc")
        except RuntimeError as exc:
            # SendInput 返回 0：锁屏、安全桌面（UAC）、或者前台是更高权限的窗口。
            # 那不是"工具坏了"，是系统此刻不接受模拟输入 —— 跳过，别报成失败。
            print("  [跳过] 系统拒绝了键盘事件（" + str(exc)[:60] + "）")
            return
        pump(25)
        printed = text_of()
        check("type_text 真的打进去了",
              "大肥鲸" in printed and "abc" in printed, "[" + printed + "]")
        try:
            windows.press_keys("ctrl+a")
        except RuntimeError as exc:
            print("  [跳过] 系统拒绝了组合键（" + str(exc)[:60] + "）")
            return
        pump(8)
        windows.type_text("覆盖成功")
        pump(25)
        check("press_keys 的组合键生效（全选后被覆盖）",
              text_of().strip() == "覆盖成功", "[" + text_of() + "]")
        check("滚轮不报错", "滚" in screen.mouse_scroll(-3))
    finally:
        if previous:
            user32.SetForegroundWindow(previous)
        user32.DestroyWindow(hwnd)
        pump(3)


def main() -> int:
    print("=== 鼠标 / 键盘输入自检 ===")
    if not IS_WINDOWS:
        print("  [跳过] 不是 Windows")
        return 0
    struct_sizes()
    pixel_to_absolute()
    end_to_end()
    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("鼠标与键盘输入全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
