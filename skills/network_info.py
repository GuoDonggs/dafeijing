# -*- coding: utf-8 -*-
"""Python 技能示例：主机名与局域网 IP。

需要任意逻辑（循环、异常处理、调库）时就写 .py，把工具声明放进 TOOLS 列表。
返回的字符串会被直接朗读，所以写成人话、别返回 JSON。
"""

from __future__ import annotations

import socket

TITLE = "网络信息"
DESCRIPTION = "查询本机的主机名和局域网 IP。"


def handler() -> str:
    try:
        hostname = socket.gethostname()
    except Exception as exc:  # noqa: BLE001
        return "查不到主机名：" + str(exc)[:60]
    ip = "未知"
    try:
        # UDP connect 不会真的发包，只是让系统挑一张出网网卡，从而拿到本机内网地址
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
    except Exception:  # noqa: BLE001 - 完全离线时拿不到，不影响主机名
        pass
    return "这台电脑叫 " + hostname + "，局域网地址是 " + ip


TOOLS = [
    {
        "name": "network_info",
        "title": "查网络信息",
        "description": "查询本机的主机名和局域网 IP 地址。用户问「我的 IP 是多少」「电脑叫什么」时调用。",
        "parameters": {"type": "object", "properties": {}},
        "handler": handler,
        "confirm": False,
        # 没有 API Key 时的触发词
        "triggers": ["网络信息", "局域网地址", "我的ip", "电脑叫什么", "主机名"],
    }
]
