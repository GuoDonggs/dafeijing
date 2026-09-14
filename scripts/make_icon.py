#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把仓库根目录的 icon.webp 变成 Windows 要的 .ico，并放到网页版当 favicon。

- packaging/voice-agent.ico：exe 与安装程序的图标（多尺寸，16~256）
- voice_agent/web/icon.webp：网页版控制台的 favicon（静态目录里已经有了它，
  打包时随 web/ 一起进产物）

用法：python scripts/make_icon.py
（build_exe.py / build_installer.py 会在打包前自动调一次，所以正常不用手动跑。）
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "icon.webp"
ICO = ROOT / "packaging" / "voice-agent.ico"
WEB_ICON = ROOT / "voice_agent" / "web" / "icon.webp"
#: Windows 会按显示位置挑尺寸：任务栏 32、开始菜单 48、属性页 256
SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]


def make_icon(force: bool = False) -> bool:
    """生成 .ico 和 favicon。返回是否成功。"""
    if not SOURCE.is_file():
        print("找不到 " + str(SOURCE) + "，跳过图标生成")
        return False
    if ICO.is_file() and not force and ICO.stat().st_mtime >= SOURCE.stat().st_mtime:
        return True
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 没装 Pillow 就别拦着打包
        print("没有 Pillow（" + str(exc)[:50] + "），跳过图标生成")
        return False
    try:
        with Image.open(SOURCE) as image:
            square = image.convert("RGBA")
            # 非正方形就先居中裁成正方形，免得被拉变形
            side = min(square.size)
            left = (square.width - side) // 2
            top = (square.height - side) // 2
            square = square.crop((left, top, left + side, top + side))
            ICO.parent.mkdir(parents=True, exist_ok=True)
            square.save(ICO, format="ICO", sizes=SIZES)
        WEB_ICON.parent.mkdir(parents=True, exist_ok=True)
        if SOURCE.resolve() != WEB_ICON.resolve():
            shutil.copyfile(SOURCE, WEB_ICON)
        print("图标已生成：" + str(ICO) + "（" + str(square.size[0]) + "px 源）")
        return True
    except Exception as exc:  # noqa: BLE001
        print("图标生成失败：" + str(exc)[:120])
        return False


def main() -> int:
    return 0 if make_icon(force=True) else 1


if __name__ == "__main__":
    sys.exit(main())
