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
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# 顺序即依赖顺序：状态机在前，界面在后，声纹最后（要合成音频，稍慢）
SUITES: list[tuple[str, str]] = [
    ("test_pipeline", "状态机：唤醒→识别→执行→播报→打断→确认→超时回落"),
    ("test_llm_loop", "假模型服务：工具调用循环、降级、断线不重跑"),
    ("test_skills", "技能加载/调用/报错/触发词/覆盖/确认契约"),
    ("test_subagent", "子代理：后台跑、独立上下文、敏感工具拒绝、择机汇报"),
    ("test_watch", "定时轮询：找图命中、静止画面不问模型、命令判定、停止与上限"),
    ("test_tools", "工具层体检：声明与实现一致、空参数试运行、截屏找图看图链路"),
    ("test_security", "权限闸门：模式阶梯、fail closed、限流、明文链路降级、审计"),
    ("test_marks", "屏幕标记：框选范围、标记点、解析成截图/看图用的矩形"),
    ("test_translit", "英文音译：内置表、学习缓存、问模型、失败兜底"),
    ("test_paths", "数据目录：运行时文件集中在一处、可切换、老文件自动迁移"),
    ("test_input", "鼠标键盘：结构体大小、绝对坐标、真窗口打字点击"),
    ("test_webui", "网页版接口、鉴权、路径穿越、前后端一致性"),
    ("test_gui", "桌面界面：页面、导航、状态刷新、SVG 图标"),
    ("test_voices", "音色表：按名字/编号选、种子写法、下拉框只给中文"),
    ("test_speaker", "声纹：同人放行、异人拦住、关掉就放行"),
]

PASSED = re.compile(r"\[通过\]")


def run_one(name: str, desc: str) -> tuple[bool, int, list[str]]:
    """跑一个脚本，返回（是否通过，通过项数，失败时的末尾几行）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    # 测试要能在无显示器环境跑，界面测试统一走 offscreen
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 运行期文件一律丢进临时目录：测试**绝不能**碰用户真实的 build/ ——
    # 他的对话记录、长期记忆、屏幕标记都在那儿，而"上次聊过什么"渗进断言
    # 会让测试时红时绿（真出现过：webui 那句回复变成"跟刚才一样"）。
    # 各个测试文件自己也设了一遍，这里是给以后新写的测试兜底。
    env["VOICE_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="voice-agent-test-")
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
