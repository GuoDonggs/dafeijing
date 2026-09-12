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


def app_version() -> str:
    """从 voice_agent/__init__.py 里读版本号（全项目唯一真源）。"""
    source = PROJECT_ROOT / "voice_agent" / "__init__.py"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return "0.0"
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else "0.0"


def version_tuple(value: str) -> tuple[int, int, int, int]:
    """把 1.1 这类写法补成 Windows 版本资源要的四段数字。"""
    parts = [int(p) for p in re.findall(r"\d+", str(value))][:4]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def write_version_info(version: str) -> Path:
    """生成 PyInstaller 的版本资源文件（exe 属性里看到的那份）。"""
    nums = version_tuple(version)
    dotted = ".".join(str(n) for n in nums)
    text = ('# -*- coding: utf-8 -*-\n'
            '"""由 scripts/build_exe.py 生成，不要手改（改 voice_agent/__init__.py 的版本号）。"""\n'
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
    """返回 (文件数, 总字节数)。"""
    files = 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                files += 1
                total += item.stat().st_size
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
KEEP_DIRS = ("build",)


def _stash_user_data(out_dir: Path, stash: Path) -> dict:
    """把产物目录里的用户数据挪到一边，返回一份「怎么放回去」的说明。"""
    plan: dict = {"files": [], "dirs": [], "skills": [], "models": ""}
    if not out_dir.is_dir():
        return plan
    plan["models"] = _link_target(out_dir / "models")
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
    shipped = {p.name for p in (PROJECT_ROOT / "skills").glob("*")} if (PROJECT_ROOT / "skills").is_dir() else set()
    if user_skills.is_dir():
        for path in user_skills.rglob("*"):
            if path.is_file() and path.name not in shipped:
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
    print("  版本        : " + version)
    print("  Python      : " + sys.version.split()[0] + "  " + sys.executable)
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

    # 1) 清理旧产物。默认清，--clean 额外让 PyInstaller 丢掉自己的缓存。
    #    清理会把「放在 exe 旁边的用户数据」一起带走，所以先收起来。
    # 暂存目录刻意放在 workpath 外面：PyInstaller 会整理 --workpath 下的东西，
    # 用户的配置不能被它顺手带走
    stash = work_dir.parent / "dist-user-data"
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
    if args.dry_run:
        print("\n--dry-run：到此为止。")
        return 0

    started = time.perf_counter()
    try:
        code = subprocess.call(cmd, cwd=str(PROJECT_ROOT), env=env)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    elapsed = time.perf_counter() - started
    if code != 0:
        print("\n  [构建失败] PyInstaller 退出码 " + str(code))
        print("  排查顺序：先看上面的报错；再看 " + str(work_sub / ("warn-" + SPEC_PATH.stem + ".txt"))
              + " 里的 missing module 清单。")
        print("  改了 spec 却像没生效时加 --clean 再试。")
        return 1

    # 3) 检查产物
    print("\n[3/4] 构建完成，用时 {:.0f}s".format(elapsed))
    expected = [CONSOLE_EXE] + ([] if args.console else [WINDOWED_EXE])
    missing = [name for name in expected if not (out_dir / name).is_file()]
    if missing:
        print("  [异常] 产物里少了 " + "、".join(missing) + "，请检查上面的构建日志。")
        return 1

    for name in expected:
        exe = out_dir / name
        print("  " + name.ljust(18) + human_size(exe.stat().st_size).rjust(9))
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
