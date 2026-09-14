#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 dist/VoiceAgent 打成一个 Windows 安装包（Inno Setup）。

用法：
    python scripts/build_exe.py                 # 先出 dist/VoiceAgent（PyInstaller）
    python scripts/build_installer.py           # 再打成安装包
    python scripts/build_installer.py --with-config   # 连你的 config.yaml 一起装（含 API Key）
    python scripts/build_installer.py --no-models     # 不带模型（装完自己把 models 放进去）
    python scripts/build_installer.py --dry-run       # 只打印会做什么

产物在 dist/installer/：
    VoiceAgent-Setup-<版本>.exe          安装程序（双击即装）
    VoiceAgent-Setup-<版本>-1.bin        payload 超过 2GB 时的分卷（一起发给对方）

为什么走 Inno Setup：Windows 上"像样的安装程序"得处理快捷方式、控制面板卸载项、
升级替换、无管理员权限时退到用户目录、卸载时别把用户数据删掉 —— 这些手写容易漏。
ISCC.exe 找不到时会给出下载地址，不会静默失败。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_exe  # noqa: E402 - 复用它的版本号与人类可读体积

DIST_APP = PROJECT_ROOT / "dist" / build_exe.APP_NAME
WORK = PROJECT_ROOT / "build" / "installer"
PAYLOAD = WORK / "payload"
ISS = PROJECT_ROOT / "packaging" / "installer.iss"
OUT_DIR = PROJECT_ROOT / "dist" / "installer"
ICON_FILE = WORK / "voice-agent.ico"
LANG_FILE = WORK / "ChineseSimplified.isl"

#: 简体中文语言文件（Inno 官方只带英文，这是社区维护的那份）
LANG_URLS = (
    "https://gh-proxy.com/https://raw.githubusercontent.com/kira-96/"
    "Inno-Setup-Chinese-Simplified-Translation/main/ChineseSimplified.isl",
    "https://raw.githubusercontent.com/kira-96/"
    "Inno-Setup-Chinese-Simplified-Translation/main/ChineseSimplified.isl",
)
ISCC_CANDIDATES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 7" / "ISCC.exe",
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 7\ISCC.exe"),
)


def find_iscc() -> Path | None:
    for path in ISCC_CANDIDATES:
        if path.is_file():
            return path
    found = shutil.which("ISCC") or shutil.which("iscc")
    return Path(found) if found else None


def download(urls: tuple[str, ...], target: Path, what: str) -> bool:
    """下载一个小文件（语言文件）。失败不致命，只是界面变英文。"""
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
                data = response.read()
            if len(data) > 1024:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                print("  已下载" + what + "：" + target.name)
                return True
        except Exception as exc:  # noqa: BLE001 - 网络问题不该让打包失败
            print("  ！下载失败（" + url.split("/")[2] + "）：" + str(exc)[:60])
    print("  ！拿不到" + what + "，安装界面就用英文")
    return False


def make_icon() -> bool:
    """生成安装程序/快捷方式用的图标。

    优先用 scripts/make_icon.py 从仓库根目录的 icon.webp 转出来的那份
    （和 exe、任务栏是同一张图）；转不出来再退回"用界面里那颗麦克风画一个"。
    """
    try:
        import make_icon as icon_maker  # noqa: PLC0415

        if icon_maker.make_icon(force=True):
            return True
    except Exception as exc:  # noqa: BLE001
        print("  ！icon.webp 转 ico 失败（" + str(exc)[:60] + "），改用画的图标")
    if ICON_FILE.is_file():
        return True
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtCore import QByteArray, Qt
        from PyQt6.QtGui import QColor, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
        from PyQt6.QtWidgets import QApplication
        from PIL import Image

        _app = QApplication.instance() or QApplication([])  # noqa: F841 - 不持有会闪退
        from voice_agent.ui import theme

        size = 256
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QColor("#1E88E5"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(6, 6, size - 12, size - 12, 48, 48)
        QSvgRenderer(QByteArray(theme.svg_bytes("mic", "#FFFFFF"))).render(painter)
        painter.end()
        ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
        png = ICON_FILE.with_suffix(".png")
        image.save(str(png))
        with Image.open(png) as handle:
            handle.save(ICON_FILE,
                        sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        png.unlink(missing_ok=True)
        print("  已生成图标：" + ICON_FILE.name)
        return True
    except Exception as exc:  # noqa: BLE001
        print("  ！图标没生成（" + str(exc)[:60] + "），安装程序会用默认图标")
        return False


def stage_payload(with_config: bool, with_models: bool,
                  source_app: Path | None = None) -> int:
    """把 dist/VoiceAgent 复制成一份干净的 payload。

    故意**不带**运行期数据：build/（记忆、声纹、日志、截图）和 config.yaml
    （里面有 API Key）都不进安装包 —— 装到别人机器上不该带上你的密钥。
    模型默认带上（装完就能用），--no-models 可以不带。
    """
    dist_app = source_app or DIST_APP
    if not dist_app.is_dir():
        print("找不到 " + str(dist_app) + "，先跑：python scripts/build_exe.py")
        return 0
    if PAYLOAD.exists():
        build_exe.remove_path(PAYLOAD)
    PAYLOAD.mkdir(parents=True, exist_ok=True)

    skip_dirs = {"build"}
    skip_files = set() if with_config else {"config.yaml", "config.yaml.bak"}
    if not with_models:
        skip_dirs.add("models")
    count = 0
    for item in dist_app.iterdir():
        if item.name in skip_files:
            continue
        if item.is_dir():
            if item.name in skip_dirs:
                continue
            # models 在 dist 里通常是目录联接：要复制**真的内容**，
            # 安装包里不能是一个指向本机的联接
            # copytree 作用在联接**路径**上取到的就是真实内容，所以统一这么写：
            # 以前联接分支把源硬编码成 models，别的联接目录会被 models 的内容填满
            shutil.copytree(str(item), str(PAYLOAD / item.name), dirs_exist_ok=True)
            count += 1
        else:
            shutil.copyfile(item, PAYLOAD / item.name)
            count += 1
    if with_models and not (PAYLOAD / "models").is_dir() and (PROJECT_ROOT / "models").is_dir():
        shutil.copytree(str(PROJECT_ROOT / "models"), str(PAYLOAD / "models"),
                        dirs_exist_ok=True)
        count += 1
    files, total = build_exe.dir_stats(PAYLOAD)
    print("  payload：" + str(files) + " 个文件，" + build_exe.human_size(total))
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_installer.py",
        description="把 dist/VoiceAgent 打成 Windows 安装包（Inno Setup）",
    )
    parser.add_argument("--with-config", action="store_true",
                        help="连项目里的 config.yaml 一起装（里面有 API Key，别外发）")
    parser.add_argument("--no-models", action="store_true", help="不带 models 目录")
    parser.add_argument("--dry-run", action="store_true", help="只打印会做什么")
    parser.add_argument("--dist", default=str(DIST_APP),
                        help="源产物目录，默认 dist/VoiceAgent"
                             "（正在运行的那个 exe 会锁住 dist/，可以换 dist-new/）")
    args = parser.parse_args(argv)
    build_exe._fix_console()  # noqa: SLF001 - 同一个脚本目录里的工具函数

    version = build_exe.app_version()
    if not version:
        print("[中止] 读不出 voice_agent/__init__.py 里的 __version__")
        return 2
    print("=" * 68)
    print("  大肥鲸 VoiceAgent 安装包（Inno Setup）")
    print("=" * 68)
    print("  版本      : " + version)
    source_app = Path(args.dist).resolve()
    print("  源产物    : " + str(source_app))
    print("  输出目录  : " + str(OUT_DIR))
    print("  模型      : " + ("不带" if args.no_models else "带上（装完即用）"))
    # 安装包名里的版本号来自 __version__，里面装的 exe 却可能是上一次构建的 ——
    # 对一下，不一致就别打（否则用户装完发现"版本对不上"）。
    exe_got = build_exe.exe_version(source_app / build_exe.WINDOWED_EXE)
    if exe_got is not None and build_exe._version_parts(exe_got) != (build_exe.version_tuple(version) or (0, 0, 0, 0)):
        print()
        print("  [中止] dist 里的 exe 版本是 " + exe_got + "，和 __version__（"
              + version + "）不一致 —— 先重跑 python scripts/build_exe.py")
        return 2
    print("  配置      : " + ("带上项目里的 config.yaml" if args.with_config
                             else "不带（安装包不该含你的 API Key）"))

    iscc = find_iscc()
    if iscc is None:
        print()
        print("  [缺少依赖] 没找到 Inno Setup 的 ISCC.exe。装一个（不用管理员权限）：")
        print("      https://jrsoftware.org/isdl.php   或者：")
        print("      winget install JRSoftware.InnoSetup")
        print("  装完再跑一次本脚本即可。")
        return 2
    print("  ISCC      : " + str(iscc))
    if not ISS.is_file():
        print("  [缺少文件] 找不到 " + str(ISS))
        return 2

    if args.dry_run:
        print()
        print("[--dry-run] 会做这些事（都不会真的执行）：")
        print("  · 清空并重建 " + str(PAYLOAD))
        print("  · 把 " + str(DIST_APP) + " 复制进去（不含 build/、config.yaml"
              + ("" if args.no_models else "，含 models/"))
        print("  · 生成图标 " + str(ICON_FILE))
        print("  · 下载简体中文语言文件 " + str(LANG_FILE) + "（失败就用英文界面）")
        print("  · 调用 " + str(iscc) + " 编译 " + str(ISS))
        print("  · 产物写到 " + str(OUT_DIR / ("VoiceAgent-Setup-" + version + ".exe")))
        return 0

    print("\n[1/4] 准备 payload")
    stage_payload(args.with_config, not args.no_models, source_app)

    print("\n[2/4] 图标与语言文件")
    has_icon = make_icon()
    has_lang = download(LANG_URLS, LANG_FILE, "简体中文语言文件")

    print("\n[3/4] 编译安装程序（几 GB 的 payload 要压一会儿）")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [str(iscc),
           "/DAppVersion=" + version,
           "/DPayloadDir=" + str(PAYLOAD),
           "/DOutputDir=" + str(OUT_DIR),
           "/DLangFile=" + (str(LANG_FILE) if has_lang else ""),
           "/DIconFile=" + (str(ICON_FILE) if has_icon else ""),
           str(ISS)]
    print("  " + " ".join('"' + part + '"' if " " in part else part for part in cmd))
    code = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    if code != 0:
        print("\n  [构建失败] ISCC 退出码 " + str(code) + "（上面的日志里有原因）")
        return 1

    print("\n[4/4] 产物")
    produced = sorted(OUT_DIR.glob("VoiceAgent-Setup-" + version + "*"))
    if not produced:
        print("  [异常] " + str(OUT_DIR) + " 里没找到产物")
        return 1
    for item in produced:
        # 分块算 sha256：分卷可能有近 2GB，整块 read_bytes() 会吃掉同样多的内存
        with item.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()[:16]
        print("  " + item.name.ljust(34) + build_exe.human_size(item.stat().st_size).rjust(11)
              + "  sha256:" + digest)
    setup_exe = next((p for p in produced if p.suffix.lower() == ".exe"), produced[0])
    if len(produced) > 1:
        print("  （安装程序总是分卷：这些文件要一起发给对方，放在同一个目录）")
    print("\n安装：双击 " + setup_exe.name + "。卸载在「设置 → 应用」里，"
          "卸载时会问要不要连数据一起删。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
