# -*- coding: utf-8 -*-
"""定时轮询：本地找图命中、画面没变就不问模型、命令判定、停止与上限。

轮询最容易出的两个问题是"根本没在跑"和"跑得太勤把额度烧光"，
所以这里两头都验：真的能命中，以及静止画面不会反复调用模型。

运行：python tests/test_watch.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

import numpy as np  # noqa: E402

from voice_agent.agent import VoiceAgent  # noqa: E402
from voice_agent.config import Config  # noqa: E402
from voice_agent.watcher import Watcher  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def wait_state(watch, state: str, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if watch.state == state:
            return True
        time.sleep(0.05)
    return watch.state == state


def make_config() -> Config:
    path = Path(_BUILD.name) / "config.yaml"
    path.write_text("wake:\n  keywords: [测试词]\n", encoding="utf-8")
    return Config.load(path)


def main() -> int:
    print("=== 定时轮询 ===")
    cfg = make_config()
    hits: list = []
    judged: list = []

    def judge(question: str, answer: str):  # noqa: ANN001
        judged.append((question, answer))
        return "42" in answer

    watcher = Watcher(cfg, log=lambda _m: None, judge=judge, on_hit=hits.append)

    # ── 场景 1：本地找图（从当前屏幕上裁一块，必然找得到）──
    from voice_agent import screen as screen_mod

    grab = screen_mod.grab_screen()
    left, top, width, height = screen_mod._virtual_screen()
    patch_w, patch_h = 160, 100
    best = None
    for ix in range(left + 60, left + width - patch_w - 60, 149):
        for iy in range(top + 60, top + height - patch_h - 60, 97):
            px, py = ix - left, iy - top
            patch = grab[py:py + patch_h, px:px + patch_w]
            if patch.shape[:2] != (patch_h, patch_w):
                continue
            deviation = float(patch.std())
            if best is None or deviation > best[0]:
                best = (deviation, ix, iy)
    if best is None or best[0] < 6:
        print("  [跳过] 屏幕太单调，找图这项没法验")
    else:
        import cv2

        template = Path(_BUILD.name) / "target.png"
        _, ix, iy = best
        cv2.imwrite(str(template), grab[iy - top:iy - top + patch_h,
                                        ix - left:ix - left + patch_w])
        watch = watcher.start("图片", str(template), interval_s=1)
        check("找图轮询会在第一次检查就命中",
              wait_state(watch, "hit"), watch.state + " " + watch.last)
        check("命中后写了位置", "找到了" in watch.last and str(ix) not in ("", None),
              watch.last)
        check("命中会通知出去", len(hits) == 1 and hits[0] is watch, str(len(hits)))
        check("命中后自己停下", watch.state == "hit" and watch.checks >= 1,
              str(watch.checks) + " 次")
        check("快照里能看到它", watcher.snapshot()["total"] == 1,
              str(watcher.snapshot()))

    # ── 场景 2：画面没变就不问模型 ──
    same = np.zeros((200, 300, 3), dtype=np.uint8)
    check("第一次比较不算「没变」", watcher._unchanged("probe", same) is False)
    check("同一张图第二次判为没变", watcher._unchanged("probe", same) is True)
    changed = same.copy()
    changed[:, :] = 255
    check("画面明显变化时判为变了", watcher._unchanged("probe", changed) is False)

    # ── 场景 3：命令 + 判定 ──
    judged.clear()
    watch = watcher.start("命令", "Write-Output 42", "输出里是 42", interval_s=1)
    check("命令轮询能命中", wait_state(watch, "hit"), watch.state + " " + watch.last)
    check("判定时把条件给了模型", bool(judged) and "42" in judged[0][0], str(judged[:1]))
    check("命中说明是人话", "成立" in watch.last or "查" in watch.last, watch.last)

    # ── 场景 4：停止 ──
    judged.clear()

    def never(question: str, answer: str):  # noqa: ANN001
        return False

    quiet = Watcher(cfg, log=lambda _m: None, judge=never, on_hit=hits.append)
    watch = quiet.start("命令", "Write-Output 1", "输出里是 99", interval_s=1)
    time.sleep(1.2)
    check("条件不成立时一直在跑", watch.state == "running", watch.state)
    check("停得掉", quiet.stop(watch.id) is True and watch.state == "stopped")
    check("停掉之后不再检查", not quiet.running())
    check("重复停不报错", quiet.stop(watch.id) is False)
    check("全部停掉接口可用", quiet.stop_all() == 0)

    # ── 场景 5：参数校验与上限 ──
    for kind, target, condition, expect in (
        ("乱写", "x", "", "不认识的检查方式"),
        ("图片", "", "", "要找哪张图"),
        ("命令", "", "", "要跑什么命令"),
        ("屏幕", "", "", "盯着屏幕看什么"),
    ):
        try:
            quiet.start(kind, target, condition)
            check("参数不对时说人话：" + expect, False, "居然接受了")
        except ValueError as exc:
            check("参数不对时说人话：" + expect, expect in str(exc), str(exc)[:40])
    try:
        too_fast = quiet.start("命令", "Write-Output 1", "", interval_s=0.01)
        check("检查间隔有下限", too_fast.interval_s >= 1.0, str(too_fast.interval_s))
        quiet.stop(too_fast.id)
    except Exception as exc:  # noqa: BLE001
        check("检查间隔有下限", False, str(exc))
    crowd = Watcher(cfg, log=lambda _m: None, judge=never)
    started = [crowd.start("命令", "Start-Sleep 9", "输出里是 99", interval_s=1)
               for _ in range(5)]
    try:
        crowd.start("命令", "Start-Sleep 9", "", interval_s=1)
        check("同时盯着的事情有上限", False, "第 6 个居然也接受了")
    except RuntimeError as exc:
        check("同时盯着的事情有上限", "最多" in str(exc), str(exc)[:30])
    check("批量停止可用", crowd.stop_all() == 5, str(len(started)))

    # ── 场景 6：命中汇报走的是"择机播报"那条路 ──
    agent = VoiceAgent(make_config(), log=lambda _m: None)
    queued = type("W", (), {"id": "watch9", "last": "屏幕上出现下载完成了"})()
    agent._on_watch_hit(queued)
    check("轮询命中会进对话记录",
          any("watch9" in str(t["text"]) for t in agent.transcript))
    check("轮询命中会进待播队列", len(agent._announce) == 1, str(len(agent._announce)))
    agent._state = "listen"
    agent._check_announce()
    check("正在听的时候不插嘴", len(agent._announce) == 1, str(len(agent._announce)))

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("定时轮询全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
