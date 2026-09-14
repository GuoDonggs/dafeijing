#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 voice-agent 打成 Windows 绿色版（PyInstaller onedir）。

用法：
    python scripts/build_exe.py                  # 桌面版 VoiceAgent.exe + 命令行版 VoiceAgentCLI.exe
    python scripts/build_exe.py --console        # 只出命令行版，构建快一些
    python scripts/build_exe.py --slim           # 去掉 onnxruntime 的 CUDA/TensorRT DLL（省约 170 MB）
    python scripts/build_exe.py --clean          # 连 PyInstaller 缓存一起清掉（改了 spec 却"没生效"时用）
    python scripts/build_exe.py --with-config    # 把项目的 config.yaml 也复制过去（注意：里面有 API Key）
    python scripts/build_exe.py --dry-run        # 只打印要执行的命令

产物是 dist/VoiceAgent/ 这个文件夹：拷到别的 Windows 机器上就能跑，那台机器不用装 Python。
模型（几百 MB）和配置不进包，放在 exe 旁边 —— 见 packaging/README.md。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = PROJECT_ROOT / "packaging" / "voice-agent.spec"
APP_NAME = "VoiceAgent"                                  # 与 spec 里 COLLECT(name=...) 一致
DEFAULT_DIST = PROJECT_ROOT / "dist"
# workpath 特意放在 build/pyinstaller 下：build/ 本身是程序运行期放
# memory.json、keywords.generated.txt、截图的地方，直接拿它当 PyInstaller 工作目录的话，
# 一次 --clean 就会把用户的记忆文件一起删掉。
DEFAULT_WORK = PROJECT_ROOT / "build" / "pyinstaller"

WINDOWED_EXE = "VoiceAgent.exe"
CONSOLE_EXE = "VoiceAgentCLI.exe"
#: exe 的「属性 → 详细信息」里那份版本资源。由版本号现场生成，
#: 不手写第二份 —— 两份版本号迟早会对不上。
VERSION_FILE = PROJECT_ROOT / "packaging" / "version_info.txt"


def app_version() -> str | None:
    """从 voice_agent/__init__.py 里读版本号（全项目唯一真源）。

    读不到就返回 None —— **绝不退化成 "0.0"**：那会打出一个属性里写着
    0.0.0.0 的 exe，构建还照样返回成功，用户装了之后谁也说不清是哪个版本。
    """
    source = PROJECT_ROOT / "voice_agent" / "__init__.py"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return None
    # 单引号 / 类型注解 / 前后空格都认（以前只认双引号，改个写法就静默变 0.0）
    match = re.search(r"^__version__\s*(?::\s*str\s*)?=\s*['\"]([^'\"]+)['\"]",
                      text, re.M)
    return match.group(1) if match else None


def _version_parts(text: str) -> tuple[int, int, int, int]:
    """把 "1.2.0.0" 解析成四段整数（按段比，别用字符串 rstrip —— 那会把
    1.20.0.0 和 1.2.0.0 判成一样、10.0.0.0 和 1.0.0.0 判成一样）。"""
    parts = [int(p) for p in re.findall(r"\d+", str(text))][:4]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def exe_version(exe: Path) -> str | None:
    """回读 exe 里的版本资源（"1.2.0.0"）；读不到返回 None。

    用 PowerShell 的 VersionInfo：PyInstaller 自带的 versioninfo 模块是**生成**用的，
    拿它读一个二进制 exe 读不出来（第一版就踩了这个坑，白报了一次"没读到"）。
    """
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-Item -LiteralPath '" + str(exe).replace("'", "''")
             + "').VersionInfo.FileVersion"],
            capture_output=True, text=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = (completed.stdout or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 - 读不到就当没带版本资源
        return None


def version_tuple(value: str) -> tuple[int, int, int, int] | None:
    """把 1.1 这类写法补成 Windows 版本资源要的四段数字。

    一个数字都没有（"beta"）时返回 None —— 否则会写出 0.0.0.0 的 exe，
    而"回读比对"用的也是这个函数，两边一起变成 0 就"校验通过"了。
    """
    parts = [int(p) for p in re.findall(r"\d+", str(value))][:4]
    if not parts:
        return None
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def write_version_info(version: str) -> Path:
    """生成 PyInstaller 的版本资源文件（exe 属性里看到的那份）。"""
    nums = version_tuple(version)
    if nums is None:
        raise ValueError("版本号里连一个数字都没有：" + repr(version))
    dotted = ".".join(str(n) for n in nums)
    # 只能有"一行注释 + 一个表达式"：PyInstaller 是拿 eval() 读这个文件的，
    # 中间夹一句 docstring 会被当成语句 → SyntaxError: invalid syntax。
    text = ('# -*- coding: utf-8 -*-  （由 scripts/build_exe.py 生成，不要手改；'
            "改版本号请改 voice_agent/__init__.py）\n"
            "VSVersionInfo(\n"
            "  ffi=FixedFileInfo(\n"
            "    filevers=" + str(nums) + ",\n"
            "    prodvers=" + str(nums) + ",\n"
            "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)\n"
            "  ),\n"
            "  kids=[\n"
            "    StringFileInfo([\n"
            "      StringTable(\n"
            "        '080404B0',\n"
            "        [StringStruct('CompanyName', 'voice-agent'),\n"
            "         StringStruct('FileDescription', '大肥鲸 · 语音控制电脑助手'),\n"
            "         StringStruct('FileVersion', '" + dotted + "'),\n"
            "         StringStruct('InternalName', 'VoiceAgent'),\n"
            "         StringStruct('OriginalFilename', 'VoiceAgent.exe'),\n"
            "         StringStruct('ProductName', '大肥鲸 VoiceAgent'),\n"
            "         StringStruct('ProductVersion', '" + str(version) + "'),\n"
            "         StringStruct('LegalCopyright', '')])\n"
            "    ]),\n"
            "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
            "  ]\n"
            ")\n")
    VERSION_FILE.write_text(text, encoding="utf-8")
    return VERSION_FILE


def _fix_console() -> None:
    """Windows 控制台编码千奇百怪，中文提示不能因为编不出来就崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def human_size(num_bytes: float) -> str:
    """把字节数写成 12.1 MB 这种给人看的形式。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            digits = 0 if unit in ("B", "KB") else 1
            return "{0:.{1}f} {2}".format(value, digits, unit)
        value /= 1024.0
    return str(value)


def dir_stats(path: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节数)。

    **不跟着目录联接走**：models/ 通常是指向几 GB 真模型的联接
    （见 packaging/README.md），跟进去会把模型算成"产物体积"，
    数字虚高，dry-run 预览还会白扫一遍整个模型目录。
    """
    files = 0
    total = 0
    for root, dirs, names in os.walk(path):
        try:
            dirs[:] = [name for name in dirs
                       if not _link_target(Path(root) / name)]
        except OSError:
            pass
        for name in names:
            try:
                total += (Path(root) / name).stat().st_size
                files += 1
            except OSError:   # 打包过程中被占用的文件不该让统计失败
                continue
    return files, total


def _link_target(path: Path) -> str:
    """目录联接 / 符号链接指向哪里；不是链接就返回空串。

    刻意不看"目标还在不在"：联接坏了（目标被删了）同样要认得出来，
    否则既删不掉、也改不了指向。os.path.isjunction 走 lstat，不受目标影响。
    """
    try:
        if os.path.isjunction(path):
            try:
                return str(path.resolve())
            except OSError:
                return os.readlink(path)
    except (OSError, AttributeError):
        pass
    if path.is_symlink():
        try:
            return str(path.resolve())
        except OSError:
            return os.readlink(path)
    return ""


def remove_path(path: Path) -> bool:
    """删文件 / 目录 / 目录联接。

    目录联接（dist/models 就是，见 packaging/README.md）要特别处理：
    Python 3.12 的 shutil.rmtree **拒绝**作用在联接上，直接抛
    "Cannot call rmtree on a symbolic link"。它不会顺着删掉真模型（这点是好
    消息），但也删不掉联接本身 —— 早先这里写着"rmtree 是安全的"，实际效果是
    "删不掉还当成功"，换模型目录时会留下一个指向旧位置的联接。
    摘联接用 os.rmdir，只动链接，目标目录一个字节都不碰。
    """
    is_link = bool(_link_target(path))
    if not is_link and not path.exists():
        return True
    try:
        if is_link:
            try:
                os.rmdir(path)
            except OSError:
                path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True
    except OSError as exc:
        print("  ! 删不掉 " + str(path) + "：" + str(exc))
        print("    （多半是程序还在运行或有杀软在扫，关掉相关进程后重试）")
        return False


def pyinstaller_version() -> str | None:
    try:
        import PyInstaller  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return str(getattr(PyInstaller, "__version__", "?"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_exe.py",
        description="用 PyInstaller 把 voice-agent 打成 Windows 绿色版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--console", action="store_true",
                        help="只出命令行版 VoiceAgentCLI.exe（不要桌面窗口）")
    parser.add_argument("--clean", action="store_true",
                        help="连 PyInstaller 自己的缓存也清掉（比默认的清理旧产物更彻底，改了 spec 却没生效时用）")
    parser.add_argument("--no-clean", action="store_true",
                        help="不清理旧产物，增量重建（排错时反而更慢，谨慎用）")
    parser.add_argument("--slim", action="store_true",
                        help="去掉 onnxruntime 的 CUDA/TensorRT provider DLL，产物小约 170 MB")
    parser.add_argument("--with-config", action="store_true",
                        help="把项目的 config.yaml 复制到产物里（里面有 API Key，别外发）")
    parser.add_argument("--distpath", default=str(DEFAULT_DIST), help="产物目录，默认 dist/")
    parser.add_argument("--workpath", default=str(DEFAULT_WORK), help="中间目录，默认 build/pyinstaller/")
    parser.add_argument("--dry-run", action="store_true", help="只打印命令，不真的构建")
    return parser.parse_args(argv)


# 打包前会被清空的产物目录里，有几样是「用户的」而不是「构建的」：
# config.yaml（含 API Key）、声纹档、应用映射表、运行期生成的 build/，
# 以及用户自己往 skills/ 里加的技能。models 通常是目录联接（几百 MB），
# 拷不动，只记下指向、打完重建。
KEEP_FILES = ("config.yaml", "config.yaml.bak", "apps.yaml", ".env")
#: build/ 是运行期数据；**models 也要在里面** —— README 教的正是"把 models 整个
#: 拷到 exe 旁边"，而它在白名单外时会被 remove_path(out_dir) 连模型一起删掉
#: （实测：追不回来，程序随后"缺模型"起不来）。联接由 plan["models"] 单独处理，
#: 这里只收真实目录。
KEEP_DIRS = ("build", "models")


def _same_file(left: Path, right: Path) -> bool:
    """两个文件内容是不是一样（大小 + 逐块比较，不用读进内存）。"""
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as a, right.open("rb") as b:
            while True:
                chunk_a = a.read(65536)
                chunk_b = b.read(65536)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except OSError:
        return False


def _stash_user_data(out_dir: Path, stash: Path) -> dict:
    """把产物目录里的用户数据挪到一边，返回一份「怎么放回去」的说明。"""
    plan: dict = {"files": [], "dirs": [], "skills": [], "models": ""}
    if not out_dir.is_dir():
        return plan
    plan["models"] = _link_target(out_dir / "models")   # 联接：只记指向，重建
    stash.mkdir(parents=True, exist_ok=True)
    for name in KEEP_FILES:
        source = out_dir / name
        if source.is_file():
            shutil.copyfile(source, stash / name)
            plan["files"].append(name)
    for name in KEEP_DIRS:
        source = out_dir / name
        if source.is_dir() and not _link_target(source):
            shutil.copytree(source, stash / name, dirs_exist_ok=True)
            plan["dirs"].append(name)
    # skills/：只收「源仓库里没有」的那些，也就是用户自己加的
    user_skills = out_dir / "skills"
    # "是不是自带的"不能只比文件名：用户在产物目录里**改过**的 greet.yaml
    # 也是他的东西，只看名字会把它当成自带文件、重建后被仓库版本静默覆盖。
    # 所以再加一条：内容和仓库那份不一样，就算用户改过的。
    shipped: dict[str, Path] = {}
    if (PROJECT_ROOT / "skills").is_dir():
        for item in (PROJECT_ROOT / "skills").rglob("*"):
            if item.is_file():
                shipped[item.name] = item
    if user_skills.is_dir():
        for path in user_skills.rglob("*"):
            keep = path.is_file() and (path.name not in shipped
                                       or not _same_file(path, shipped[path.name]))
            if keep:
                rel = path.relative_to(user_skills)
                target = stash / "skills" / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                plan["skills"].append(str(rel))
    return plan


def _restore_user_data(out_dir: Path, stash: Path, plan: dict) -> None:
    """把 _stash_user_data 收起来的东西放回去。"""
    restored: list[str] = []
    for name in plan["files"]:
        source = stash / name
        if source.is_file():
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, out_dir / name)
            restored.append(name)
    for name in plan["dirs"]:
        source = stash / name
        if source.is_dir():
            shutil.copytree(source, out_dir / name, dirs_exist_ok=True)
            restored.append(name + "/")
    for rel in plan["skills"]:
        source = stash / "skills" / rel
        target = out_dir / "skills" / rel
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if plan["skills"]:
        restored.append("skills/ 里 " + str(len(plan["skills"])) + " 个自定义文件")
    if plan["models"]:
        link = out_dir / "models"
        remove_path(link)
        try:
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), plan["models"]],
                capture_output=True, text=True, check=False,
            )
            if link.is_dir():
                restored.append("models 联接 -> " + plan["models"])
            else:
                print("  ！models 联接没建起来：" + (completed.stderr or "").strip()[:120])
        except OSError as exc:
            print("  ！models 联接没建起来：" + str(exc)[:120])
    if restored:
        print("  已把上一次的用户数据放回产物目录：" + "、".join(restored))
    elif (stash / "config.yaml").is_file():
        print("  ! 暂存里有 config.yaml 但没放回去，这不该发生，请检查")
    shutil.rmtree(stash, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dist_dir = Path(args.distpath).expanduser().resolve()
    work_dir = Path(args.workpath).expanduser().resolve()
    out_dir = dist_dir / APP_NAME
    work_sub = work_dir / SPEC_PATH.stem      # PyInstaller 会在 workpath 后面再接一层 spec 名

    print("=" * 68)
    print("  voice-agent 打包（PyInstaller onedir）")
    print("=" * 68)
    version = app_version()
    if not version:
        print()
        print("  [中止] 读不出 voice_agent/__init__.py 里的 __version__ ——")
        print("  宁可不打包，也不打一个属性里写着 0.0.0.0 的 exe。")
        print("  请检查那一行长这样：__version__ = \"1.2\"")
        return 2
    print("  版本        : " + version)
    print("  Python      : " + sys.version.split()[0] + "  " + sys.executable)
    # 图标从仓库根目录的 icon.webp 现生成（和安装程序、任务栏用同一张图）
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import make_icon  # noqa: PLC0415

        make_icon.make_icon()
    except Exception as exc:  # noqa: BLE001 - 图标失败不该拦住打包
        print("  ！图标没生成：" + str(exc)[:80])
    print("  项目目录    : " + str(PROJECT_ROOT))
    print("  产物目录    : " + str(out_dir))
    print("  中间目录    : " + str(work_sub))
    print("  模式        : " + ("只出命令行版" if args.console else "桌面版 + 命令行版")
          + ("，精简 CUDA/TensorRT" if args.slim else ""))

    pyinstaller = pyinstaller_version()
    if pyinstaller is None:
        print()
        print("  [缺少依赖] 没找到 PyInstaller，先装它：")
        print()
        print("      pip install pyinstaller")
        print()
        return 2
    print("  PyInstaller : " + pyinstaller)

    if not SPEC_PATH.is_file():
        print()
        print("  [缺少文件] 找不到 spec：" + str(SPEC_PATH))
        return 2

    # 0) --dry-run 必须**什么都不动**。以前它排在清理后面，于是"只打印命令"的
    #    那一次照样把 dist/VoiceAgent 删了、把 exe 旁边的 config.yaml（含 API Key）
    #    和运行期 build/ 挪进暂存区 —— 而 dry-run 走到最后直接 return，
    #    连"放回去"都不会执行。用户只是想看看命令，产物却没了。
    if args.dry_run:
        cmd_preview = [sys.executable, "-m", "PyInstaller", "--noconfirm",
                       "--distpath", str(dist_dir), "--workpath", str(work_dir)]
        if args.clean:
            cmd_preview.append("--clean")
        cmd_preview.append(str(SPEC_PATH))
        print("")
        print("[--dry-run] 下面这些**都不会真的执行**：")
        if args.no_clean:
            print("  · 跳过清理旧产物（--no-clean）")
        else:
            print("  · 清理 " + str(out_dir) + ("" if not out_dir.is_dir()
                                              else "（现在存在，里面有 "
                                                   + str(dir_stats(out_dir)[0]) + " 个文件）"))
            if out_dir.is_dir():
                print("    用户的 config.yaml / build/ / skills/ 会先暂存到 "
                      + str(work_dir.parent / "dist-user-data"))
        print("  · 生成版本资源 " + str(VERSION_FILE) + "（版本 " + version + "）")
        print("  · 构建命令：")
        print("      " + " ".join('"' + p + '"' if " " in p else p for p in cmd_preview))
        print("")
        print("--dry-run：到此为止，没有改动任何文件。")
        return 0

    # 1) 清理旧产物。默认清，--clean 额外让 PyInstaller 丢掉自己的缓存。
    #    清理会把「放在 exe 旁边的用户数据」一起带走，所以先收起来。
    # 暂存目录刻意放在 workpath 外面：PyInstaller 会整理 --workpath 下的东西，
    # 用户的配置不能被它顺手带走
    stash = work_dir.parent / "dist-user-data"
    # 上一次构建要是被 Ctrl+C / 杀死在半路（那时候还没修 finally），用户数据会
    # 只剩暂存区这一份 —— 而下面一句就是把它删掉。所以先判断"暂存区里有配置、
    # 产物目录里却没有"，是就先放回去再继续，别把唯一一份删了。
    stash_has_data = (any((stash / name).is_file() for name in KEEP_FILES)
                      or any((stash / name).is_dir() for name in KEEP_DIRS)
                      or (stash / "skills").is_dir())
    if stash_has_data and not (out_dir / "config.yaml").is_file():
        print("\n[0/4] 发现上一次构建留下的暂存数据，先放回产物目录")
        # 注意源路径就是 stash 本身（第一次写这段时手滑写成 stash+"-recover"，
        # 结果什么都没搬回来，紧接着 remove_path(stash) 把唯一一份删了 ——
        # 打包脚本自己把用户数据弄丢过一次，这里必须是真的 stash）
        skills_from_stash = [str(item.relative_to(stash / "skills"))
                             for item in (stash / "skills").rglob("*")
                             if item.is_file()] if (stash / "skills").is_dir() else []
        _restore_user_data(out_dir, stash,
                           {"files": [name for name in KEEP_FILES
                                      if (stash / name).is_file()],
                            "dirs": [name for name in KEEP_DIRS
                                     if (stash / name).is_dir()],
                            "skills": skills_from_stash, "models": ""})
        print("  已放回：" + str(out_dir))
    remove_path(stash)
    plan = _stash_user_data(out_dir, stash)
    kept = len(plan["files"]) + len(plan["dirs"]) + len(plan["skills"])
    if args.no_clean:
        print("\n[1/4] 跳过清理（--no-clean）")
    else:
        print("\n[1/4] 清理旧产物")
        # 删不干净就直接停：以前只是打印一句警告继续往下走，
        # 结果 PyInstaller 在一个没清空的目录上构建失败，报的错和真正的原因
        # （有程序占着文件）八竿子打不着，排查很费劲。
        if not remove_path(out_dir):
            print()
            print("  [中止] 产物目录删不掉，多半是程序还在运行。")
            print("  先关掉 " + str(out_dir / WINDOWED_EXE) + " 和 VoiceAgentCLI.exe，再重新打包。")
            return 1
        remove_path(work_sub)
        print("  已清理 " + str(out_dir))
    if kept or plan["models"]:
        print("  已暂存用户数据：" + str(kept) + " 项"
              + ("，另有 models 联接" if plan["models"] else "")
              + "  -> " + str(stash))
    elif (out_dir / "config.yaml").is_file():
        print("  ! 产物目录里有 config.yaml 但没能暂存，构建会把它冲掉，请检查权限")

    # 2) 组装命令。环境变量是 spec 的开关（.spec 里读不到命令行参数）。
    env = dict(os.environ)
    env["VOICE_AGENT_CONSOLE_ONLY"] = "1" if args.console else ""
    env["VOICE_AGENT_SLIM"] = "1" if args.slim else ""
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        env["VOICE_AGENT_VERSION_FILE"] = str(write_version_info(version))
    except OSError as exc:
        print("  ! 版本资源没写出来（" + str(exc)[:80] + "），exe 属性里不会带版本号")
        env.pop("VOICE_AGENT_VERSION_FILE", None)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--distpath", str(dist_dir),
        "--workpath", str(work_dir),
    ]
    if args.clean:
        cmd.append("--clean")
    cmd.append(str(SPEC_PATH))

    print("\n[2/4] 开始构建（第一次 3~10 分钟，之后有缓存会快不少）")
    print("  " + " ".join('"' + part + '"' if " " in part else part for part in cmd))
    started = time.perf_counter()
    # **所有退出路径都要把用户数据放回去**。以前只有"构建失败"和"产物缺文件"
    # 两个分支调了 _restore_user_data，Ctrl+C（或任何异常）直接 return ——
    # 而暂存区在下一次构建开头会被整个删掉，用户的 config.yaml（含 API Key）、
    # 声纹档、运行期 build/ 就此永久消失。这里改成 try/finally 兜住。
    restored = False
    try:
        try:
            code = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env)
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130
        elapsed = time.perf_counter() - started
        if code != 0:
            print("\n  [构建失败] PyInstaller 退出码 " + str(code))
            _restore_user_data(out_dir, stash, plan)
            restored = True
            print("  排查顺序：先看上面的报错；再看 "
                  + str(work_sub / ("warn-" + SPEC_PATH.stem + ".txt"))
                  + " 里的 missing module 清单。")
            print("  改了 spec 却像没生效时加 --clean 再试。")
            return 1
    finally:
        if not restored:
            # 中断、异常、正常结束都在这里兜底（_restore_user_data 自己幂等）
            _restore_user_data(out_dir, stash, plan)

    # 3) 检查产物
    print("\n[3/4] 构建完成，用时 {:.0f}s".format(elapsed))
    expected = [CONSOLE_EXE] + ([] if args.console else [WINDOWED_EXE])
    missing = [name for name in expected if not (out_dir / name).is_file()]
    if missing:
        print("  [异常] 产物里少了 " + "、".join(missing) + "，请检查上面的构建日志。")
        _restore_user_data(out_dir, stash, plan)
        return 1

    for name in expected:
        exe = out_dir / name
        print("  " + name.ljust(18) + human_size(exe.stat().st_size).rjust(9))
    # 回读 exe 里的版本资源：**构建成功不等于版本写对了**（以前版本号取不到
    # 会静默变 0.0，构建照样返回 0）。这里当场核对，错了就明说。
    want = version_tuple(version) or (0, 0, 0, 0)
    got = exe_version(out_dir / expected[-1])
    want_text = ".".join(str(part) for part in want)
    if got is None:
        print("  ！exe 里没读到版本资源（属性 → 详细信息 会是空的）")
    elif _version_parts(got) != want:
        # **不一致就失败**：以前只多打一行"！"，脚本照样返回 0，
        # 于是"版本号写错了"这件事根本拦不住。
        print("  [异常] exe 里的版本是 " + got + "，和 __version__（" + want_text + "）不一致")
        _restore_user_data(out_dir, stash, plan)
        return 1
    else:
        print("  版本资源     " + got)
    files, total = dir_stats(out_dir)
    print("  整个目录           " + human_size(total).rjust(9) + "（" + str(files) + " 个文件）")

    # 4) 配置与模型：它们不在包里，放在 exe 旁边就能被找到
    print("\n[4/4] 收尾")
    _restore_user_data(out_dir, stash, plan)
    config_next_to_exe = out_dir / "config.yaml"
    if args.with_config:
        source = PROJECT_ROOT / "config.yaml"
        if source.is_file():
            shutil.copyfile(source, config_next_to_exe)
            print("  已复制 " + str(source) + " -> " + str(config_next_to_exe))
            print("  ！这份配置里有 API Key，别把整个目录发给别人。")
        else:
            print("  项目里没有 config.yaml，跳过。")
    elif not config_next_to_exe.is_file() and (out_dir / "config.example.yaml").is_file():
        shutil.copyfile(out_dir / "config.example.yaml", config_next_to_exe)
        print("  已生成 " + str(config_next_to_exe) + "（示例配置，api_key 为空；")
        print("    想用项目里那份就加 --with-config，或者自己复制过去）")

    models_near_exe = (out_dir / "models").is_dir()
    models_sibling = (dist_dir / "voice-assistant" / "models").is_dir()
    if not models_near_exe and not models_sibling:
        print("  ！还没找到模型目录。程序不在包里带模型（几百 MB），")
        print("    把它们放到 " + str(out_dir / "models") + " 或")
        print("    " + str(dist_dir / "voice-assistant" / "models") + " 即可；")
        print("    也可以在本机做一次目录联接，不复制文件：")
        print("      mklink /J \"" + str(out_dir / "models") + "\" \"D:\\path\\to\\models\"")

    print("\n下一步，验证一下（应该打印出自检表 / 工具清单）：")
    print("  \"" + str(out_dir / CONSOLE_EXE) + "\" doctor")
    print("  \"" + str(out_dir / CONSOLE_EXE) + "\" selftest")
    if not args.console:
        print("双击运行的桌面版：" + str(out_dir / WINDOWED_EXE))
    print("详细说明（要一起发什么、多少体积、常见坑）：packaging/README.md")
    return 0


if __name__ == "__main__":
    _fix_console()
    raise SystemExit(main())
