# -*- coding: utf-8 -*-
"""时间与本机状态：时间、CPU、内存、磁盘、电池、开机时长。"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

__all__ = ["get_time", "system_info"]

def get_time() -> str:
    """现在的时间与日期。"""
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return (
        "现在是" + now.strftime("%H:%M") + "，"
        + now.strftime("%Y年%m月%d日") + "，" + weekdays[now.weekday()]
    )


def system_info(kind: str = "全部") -> str:
    """CPU / 内存 / 磁盘 / 电量 / 开机时长。"""
    import psutil  # noqa: PLC0415

    what = (kind or "全部").strip().lower()
    parts: list[str] = []

    failed: list[str] = []
    if any(key in what for key in ("全部", "all", "cpu", "处理器", "负载")):
        try:
            parts.append("CPU 占用百分之" + str(int(psutil.cpu_percent(interval=0.3))))
        except Exception as exc:  # noqa: BLE001
            # 读不到 ≠ 正常：以前这里 pass 掉，最后统一回一句"系统正常"。
            failed.append("CPU 占用（" + str(exc)[:40] + "）")
    if any(key in what for key in ("全部", "all", "内存", "memory", "ram")):
        try:
            mem = psutil.virtual_memory()
            parts.append(
                "内存用了百分之" + str(int(mem.percent))
                + "，还剩 " + str(round(mem.available / 1024 ** 3, 1)) + " G"
            )
        except Exception as exc:  # noqa: BLE001
            failed.append("内存（" + str(exc)[:40] + "）")
    if any(key in what for key in ("全部", "all", "磁盘", "硬盘", "空间", "disk", "盘")):
        match = re.search(r"([A-Za-z])", kind or "")
        letters = [match.group(1).upper()] if match else []
        if not letters:
            letters = [
                p.device[0].upper()
                for p in psutil.disk_partitions(all=False)
                if p.device and p.fstype
            ][:3]
        for letter in letters:
            try:
                usage = psutil.disk_usage(letter + ":\\")
                parts.append(
                    letter + "盘还剩 " + str(round(usage.free / 1024 ** 3)) + " G，用了百分之"
                    + str(int(usage.percent))
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(letter + "盘（" + str(exc)[:40] + "）")
                continue
    if any(key in what for key in ("全部", "all", "电池", "电量", "battery")):
        try:
            battery = psutil.sensors_battery()
            if battery is not None:
                state = "正在充电" if battery.power_plugged else "没插电"
                parts.append("电量百分之" + str(int(battery.percent)) + "，" + state)
            elif "电池" in what or "电量" in what:
                # 明确问电池却读不到：台式机没有电池是正常的，说清楚就行
                parts.append("读不到电池（这台机器可能没有电池）")
        except Exception as exc:  # noqa: BLE001
            failed.append("电池（" + str(exc)[:40] + "）")
    if any(key in what for key in ("全部", "all", "开机", "运行", "uptime")):
        try:
            hours = (time.time() - psutil.boot_time()) / 3600.0
            parts.append("已经开机 " + str(round(hours, 1)) + " 小时")
        except Exception as exc:  # noqa: BLE001
            failed.append("开机时长（" + str(exc)[:40] + "）")

    if not parts:
        parts.append("这台电脑是 " + (os.environ.get("COMPUTERNAME") or "当前主机"))
    if failed:
        # 读不到就说读不到：以前一律回"系统正常"，用户会以为机器真的没问题
        parts.append("这几项没读到：" + "、".join(failed[:3]))
    return "；".join(parts)

