# -*- coding: utf-8 -*-
"""一把跑完所有测试，最后给个总账。

单跑某个脚本当然也行（都自带 sys.path 引导，直接 python tests/xxx.py 即可），
这个脚本只是省得挨个敲，并且把 「哪一项挂在哪」一眼讲清楚。

    python scripts/run_tests.py            # 全部
    python scripts/run_tests.py gui webui  # 只跑名字里带 gui / webui 的
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# 顺序即依赖顺序：状态机在前，界面在后，声纹最后（要合成音频，稍慢）
SUITES: list[tuple[str, str]] = [
    ("test_pipeline", "状态机：唤醒→识别→执行→播报→打断→确认→超时回落"),
    ("test_llm_loop", "假模型服务：工具调用循环、降级、断线不重跑"),
    ("test_skills", "技能加载/调用/报错/触发词/覆盖/确认契约"),
    ("test_webui", "网页版接口、鉴权、路径穿越、前后端一致性"),
    ("test_gui", "桌面界面：页面、导航、状态刷新、SVG 图标"),
    ("test_voices", "音色表：英文音色会被换掉、按名字选、下拉框只给中文"),
    ("test_speaker", "声纹：同人放行、异人拦住、关掉就放行"),
]

PASSED = re.compile(r"\[通过\]")


def run_one(name: str, desc: str) -> tuple[bool, int, list[str]]:
    """跑一个脚本，返回（是否通过，通过项数，失败时的末尾几行）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 测试要能在无显示器环境跑，界面测试统一走 offscreen
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        proc = subprocess.run(
            [sys.executable, str(TESTS_DIR / (name + ".py"))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            env=env,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, 0, ["超时"]

    out = (proc.stdout or "") + (proc.stderr or "")
    count = len(PASSED.findall(out))
    if proc.returncode == 0:
        return True, count, []

    # 崩溃时最后一行往往只是被打断的日志，多留几行才看得出真正的原因
    tail = [ln.rstrip() for ln in out.strip().splitlines() if ln.strip()]
    if not tail:
        return False, count, ["退出码 " + str(proc.returncode)]
    return False, count, tail[-8:]


def main(argv: list[str]) -> int:
    picks = [a.lower() for a in argv[1:] if not a.startswith("-")]
    chosen = [s for s in SUITES if not picks or any(p in s[0].lower() for p in picks)]
    if not chosen:
        print("没有匹配的测试：" + ", ".join(picks))
        return 2

    print()
    total = 0
    failed: list[str] = []
    width = max(len(n) for n, _ in chosen)
    for name, desc in chosen:
        ok, count, why = run_one(name, desc)
        total += count
        mark = "通过" if ok else "失败"
        print("  [{}] {}  {:>3} 项  {}".format(mark, name.ljust(width), count, desc))
        if not ok:
            failed.append(name)
            for line in why:
                print("         | " + line[:150])
    print()

    if failed:
        print("失败 {} 个：{}（共 {} 项断言通过）".format(
            len(failed), "、".join(failed), total))
        return 1
    print("全部通过，共 {} 项断言。".format(total))
    print("提示：selftest 另有 10 项，用 python -m voice_agent selftest 跑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
