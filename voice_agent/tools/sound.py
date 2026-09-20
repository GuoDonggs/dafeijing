# -*- coding: utf-8 -*-
"""声音输出相关工具：系统音量（0-100）与切换当前输出设备。

和已有的 volume 工具的区别：那个是"按媒体键调大调小"，只知道相对量、也读不出
当前值；这两个工具管的是 **Windows 当前默认输出设备**本身：

- output_volume：读 / 设成 0-100（还能静音），是设备的主音量；
- output_device：列出所有输出设备、把默认设备切过去（三个角色一起切）。

实现在 voice_agent/output_device.py（纯 ctypes 调 Core Audio）。
"""

from __future__ import annotations

from .. import output_device as od

__all__ = ["output_volume_tool", "output_device_tool", "resolve_device_arg"]

_GET_WORDS = ("", "get", "查看", "查询", "看看", "读", "现在多少")
_MUTE_WORDS = ("mute", "静音", "关声", "静音吧")
_UNMUTE_WORDS = ("unmute", "取消静音", "恢复声音", "开声")


def _current_line() -> str:
    """一句"现在的状态"，读不到就说读不到。"""
    name = (od.default_device() or {}).get("name") or "默认设备"
    volume = od.get_volume()
    if volume is None:
        return "读不到当前音量（" + (od.available() or "系统没给接口") + "）"
    muted = od.get_mute()
    tail = "，现在是静音" if muted else ""
    return "当前从「" + name + "」出声，音量 " + str(volume) + "%" + tail + "。"


def output_volume_tool(action: str = "get", percent: int = -1) -> str:
    """读或设置**当前输出设备**的音量（0-100），也能静音 / 取消静音。"""
    what = str(action or "").strip().lower()
    problem = od.available()
    if problem:
        return "调不了系统音量：" + problem
    if what in _GET_WORDS:
        return _current_line()
    if what in _MUTE_WORDS:
        if od.set_mute(True) is None:
            return "静音没成功（系统没接受这个请求）"
        return "已经静音了（" + _current_line() + "）"
    if what in _UNMUTE_WORDS:
        if od.set_mute(False) is None:
            return "取消静音没成功（系统没接受这个请求）"
        return "声音回来了（" + _current_line() + "）"
    if what in ("up", "调大", "大一点", "大声点"):
        current = od.get_volume()
        if current is None:
            return "读不到当前音量，没法调大"
        done = od.set_volume(min(100, current + 10))
        return ("音量调到 " + str(done) + "%" if done is not None
                else "调大没成功")
    if what in ("down", "调小", "小一点", "小声点"):
        current = od.get_volume()
        if current is None:
            return "读不到当前音量，没法调小"
        done = od.set_volume(max(0, current - 10))
        return ("音量调到 " + str(done) + "%" if done is not None
                else "调小没成功")
    # 剩下的按"设成多少"处理
    if percent is None or int(percent) < 0:
        return ("要设多少？说个 0 到 100 的数，比如「音量调到 30」"
                "（现在" + _current_line().rstrip("。") + "）")
    done = od.set_volume(int(percent))
    if done is None:
        return "设置没成功（系统没接受这个请求，音量可能被驱动锁住了）"
    if od.get_mute():
        od.set_mute(False)   # 设音量时顺手取消静音，否则用户以为没生效
        return "音量设成 " + str(done) + "%，顺便把静音取消了"
    return "音量设成 " + str(done) + "%"


def resolve_device_arg(value: str) -> tuple[dict | None, str]:
    """把用户说的"第几个 / 名字 / 设备 id"解析成一个设备。

    返回（设备, 错误说明）。名字只做**唯一子串**匹配：不唯一就让他说清楚，
    绝不猜 —— 猜错就是把声音切到别的设备上，用户还以为是坏了。
    """
    devices = od.list_output_devices()
    if not devices:
        return None, "列不出输出设备（" + (od.available() or "系统没给接口") + "）"
    text = str(value or "").strip()
    if not text:
        return None, ""
    if text.isdigit():
        index = int(text)
        # 用户看到的编号是 1 起，这里两种都认：先按 0 起的下标，再按 1 起的序号
        for item in devices:
            if item["index"] == index:
                return item, ""
        if 1 <= index <= len(devices):
            return devices[index - 1], ""
        return None, "没有第 " + str(index) + " 个输出设备（一共 " + str(len(devices)) + " 个）"
    if text.startswith("{") and len(text) > 20:
        for item in devices:
            if item["id"] == text:
                return item, ""
        return None, "这个设备 id 现在不在列表里"
    low = text.lower()
    hits = [item for item in devices if low in (item["name"] or "").lower()]
    if len(hits) == 1:
        return hits[0], ""
    if not hits:
        names = "、".join((item["name"] or "?") for item in devices[:6])
        return None, "没有名字里有「" + text + "」的输出设备。现有：" + names
    names = "、".join(item["name"] for item in hits[:4])
    return None, ("有 " + str(len(hits)) + " 个设备都叫这个，说清楚一点："
                  + names + "（也可以说第几个）")


def output_device_tool(action: str = "list", name: str = "") -> str:
    """列出输出设备 / 把声音切换到另一个设备。"""
    what = str(action or "list").strip().lower()
    if what in ("list", "列出", "查看", "有哪些", "看看"):
        devices = od.list_output_devices()
        if not devices:
            return "列不出输出设备（" + (od.available() or "系统没给接口") + "）"
        parts = []
        for order, item in enumerate(devices, 1):
            mark = " ←现在在用" if item["default"] else ""
            parts.append(str(order) + ") " + (item["name"] or "?") + mark)
        return ("现在从「" + ((od.default_device() or {}).get("name") or "?")
                + "」出声。" + "；".join(parts) + "。要换就说「切换到第几个」或「换成它的名字」。")
    if what in ("switch", "切换", "换成", "换到", "set"):
        target, why = resolve_device_arg(name)
        if target is None:
            return why or "要切换到哪一个设备？先说「有哪些输出设备」看看"
        if target["default"]:
            return "现在用的就是「" + target["name"] + "」，不用换。"
        ok, reason = od.set_default(target)
        if not ok:
            return reason
        # 助手自己的播放设备是**另一件事**：配置里把 audio.output_device 写死了的话，
        # 它还是会从那个设备出声（那是用户自己选的，不该被这里悄悄改掉）。
        return ("好，声音已经从「" + target["name"] + "」出来了。"
                "（如果助手自己没跟着换，去「设置 → 设备」把扬声器选成「系统默认」）")
    return "看不懂这个动作（list / switch）"
