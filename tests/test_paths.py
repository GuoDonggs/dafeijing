# -*- coding: utf-8 -*-
"""数据目录：程序自己产生的东西全部收在一个目录里，而且这个目录可以改。

用户的要求是"临时截图、脚本、设置、缓存等等全部保存在一个目录下，
且目录可以由用户在设置窗口中配置"。这里验证：换一个目录之后，
记忆 / 标记 / 上下文 / 审计 / 截图 / 参考图 / 应用映射表 是不是都跟着走。

运行：python tests/test_paths.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 先用一个临时数据目录，别动用户真实的记忆和截图
_SANDBOX = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _SANDBOX.name

from voice_agent import paths  # noqa: E402
from voice_agent import marks, security  # noqa: E402
from voice_agent.tools import _shared  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    print("=== 数据目录 ===")

    print("默认与解析顺序")
    check("环境变量能指定数据目录",
          str(paths.data_dir()) == _SANDBOX.name, str(paths.data_dir()))
    check("默认是程序目录下的 build",
          paths.default_data_dir().name == "build", str(paths.default_data_dir()))

    print("\n所有运行时文件都在同一个目录下")
    root = paths.data_dir()
    items = {
        "长期记忆": _shared.memory_file(create=True),
        "截图": _shared.screenshot_dir(create=True),
        "参考图片": _shared.reference_dir(create=True),
        "屏幕标记": marks.store_path(),
        "审计日志": security._build_dir() / "audit.jsonl",
        "音译缓存": __import__("voice_agent.translit", fromlist=["cache_path"]).cache_path(),
    }
    from voice_agent.config import Config

    cfg_path = Path(_SANDBOX.name) / "config.yaml"
    cfg_path.write_text("wake:\n  keywords: [测试词]\n", encoding="utf-8")
    cfg = Config.load(cfg_path)
    from voice_agent.speaker import profile_path

    items["声纹档案"] = profile_path()
    from voice_agent.brain import Brain

    brain = Brain(cfg, log=lambda _m: None)
    items["跨轮上下文"] = brain._context_path()
    from voice_agent import screen

    items["应用映射表"] = screen.app_map_path()
    items["视觉缓存"] = screen.vision_cache()
    for label, path in items.items():
        inside = str(path).startswith(str(root))
        check(label + " 在数据目录下", inside, str(path))

    print("\n换一个目录：全都跟着走")
    other = tempfile.mkdtemp()
    paths.set_data_dir(other)
    try:
        check("数据目录立刻切换", str(paths.data_dir()) == other, str(paths.data_dir()))
        check("截图目录跟着换",
              str(_shared.screenshot_dir()).startswith(other), str(_shared.screenshot_dir()))
        check("记忆文件跟着换",
              str(_shared.memory_file()).startswith(other), str(_shared.memory_file()))
        check("标记也跟着换", str(marks.store_path()).startswith(other),
              str(marks.store_path()))
        check("应用映射表也跟着换", str(screen.app_map_path()).startswith(other))
        marks.store.add_region(1, 2, 30, 40, note="测试")
        check("在新目录里真的写得进去",
              (Path(other) / "marks.json").is_file(), str(Path(other) / "marks.json"))
        marks.store.clear()
    finally:
        paths.set_data_dir("")
    check("留空就回到默认", str(paths.data_dir()) == _SANDBOX.name, str(paths.data_dir()))

    print("\n升级迁移：老位置的文件会被搬过来")
    legacy = Path(tempfile.mkdtemp())
    target_root = Path(tempfile.mkdtemp())
    (legacy / "apps.yaml").write_text("我的项目: D:\\code\n", encoding="utf-8")
    paths.set_data_dir(target_root)
    try:
        moved = paths.migrate_legacy([(legacy / "apps.yaml", target_root / "apps.yaml")],
                                     log=lambda _m: None)
        check("老文件搬到新目录", len(moved) == 1 and (target_root / "apps.yaml").is_file(),
              str(moved))
        check("搬完老位置就没了", not (legacy / "apps.yaml").exists())
        moved = paths.migrate_legacy([(legacy / "apps.yaml", target_root / "apps.yaml")])
        check("已经搬过就不再动", not moved)
    finally:
        paths.set_data_dir("")

    info = paths.describe()
    check("describe 里有目录和清单", bool(info.get("dir")) and bool(info.get("items")),
          str(info.get("dir")))
    # 清单要跟 _LAYOUT 对得上：以前漏了 logs / keywords / selftest.wav，
    # 界面上和 doctor 里就少了几项（用户找不到"日志到底写哪了"）
    listed = {row["name"] for row in info["items"]}
    check("清单里有日志目录", "logs" in listed, str(sorted(listed)))
    check("清单里有唤醒词音素表和自检音频",
          {"keywords", "selftest_audio"} <= listed, str(sorted(listed)))

    print("\n运行日志：落盘、分级、崩溃也留痕")
    from voice_agent import journal

    journal.info("测试：一行普通日志")
    journal.detail("测试：一行详细日志")
    log_path = journal.file_path()
    check("日志写在数据目录下的 logs/ 里",
          log_path.is_file() and log_path.parent.name == "logs", str(log_path))
    check("文件名按天分", log_path.name.startswith("voice-agent-"), log_path.name)
    check("info 和 detail 都进了文件",
          len(journal.tail(20, "detail")) >= 2, str(journal.tail(3, "detail")))
    check("默认只看 info（详细那些不打扰用户）",
          all("[·]" not in line for line in journal.tail(20)),
          str(journal.tail(3)))
    check("要详细的时候看得到", any("[·]" in line for line in journal.tail(6, "detail")))

    # 崩溃钩子：窗口版没有控制台，栈只能靠它留下来
    saved_hook = sys.__excepthook__
    sys.__excepthook__ = lambda *a: None      # 别把栈也打到测试输出里
    try:
        try:
            raise ValueError("模拟崩溃：日志里要能看到这一句")
        except ValueError:
            journal.install_crash_handler()
            sys.excepthook(*sys.exc_info())
    finally:
        sys.__excepthook__ = saved_hook
    crashed = journal.tail(30, "detail")
    check("未捕获异常的 traceback 写进了日志",
          any("模拟崩溃" in line for line in crashed), str(crashed[-3:]))
    check("崩溃那一行标了 crash",
          any("[crash]" in line for line in crashed))

    # 外部传进来的 log 可能只收一个参数（命令行是 print、测试是 lambda）
    seen: list = []
    journal.adapt(lambda message: seen.append(message))("一", "detail")
    journal.adapt(lambda message: seen.append(message))("二", "info")
    check("只收一个参数的 log 不会被 level 弄崩", seen == ["二"], str(seen))
    pairs: list = []
    journal.adapt(lambda message, level="info": pairs.append((message, level)))(
        "三", "detail")
    check("收两个参数的 log 会带上 level", pairs == [("三", "detail")], str(pairs))

    print("\n清理呢？太旧的日志会自己走")
    old = log_path.with_name("voice-agent-19700101.log")
    old.write_text("很久以前\n", encoding="utf-8")
    os.utime(old, (1000.0, 1000.0))
    removed = journal.prune(14)
    check("旧日志被清掉", removed >= 1 and not old.exists(), "删了 " + str(removed) + " 个")
    check("今天的日志还在", log_path.is_file())

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("数据目录全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
