# -*- coding: utf-8 -*-
"""工具层的体检：一次把所有工具都过一遍。

分两部分：

1. **静态一致性**（不问系统，纯看声明）：工具声明的参数和 handler 的签名必须对得上。
   这一类错误很隐蔽 —— 模型按 schema 传参，handler 却不认，用户只会听到
   "工具参数不对"，然后助手就卡在那儿。之前的 _params 共享字典 bug
   （8 个工具的必填项集体消失）就是这一类。
2. **空参数试运行**：每个工具都用 {} 调一次。好的工具有两种反应 ——
   要么说"你要打开哪个网址"（参数校验有效），要么真的把事做了（无参工具）。
   坏的只有一种：抛异常，或者返回空字符串（静默失效，比如当年
   windows.py 丢了常量、PowerShell 工具全部返回 ""）。

危险的工具（会最小化窗口、调音量、在用户界面上真的点一下）不在这里跑。

运行：python tests/test_tools.py
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时产生的文件挪到临时目录：测试不该往用户真实的对话记录 / 长期记忆里写
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

from voice_agent import tools  # noqa: E402

failures: list[str] = []

#: 空参数跑起来会打扰用户的工具（各自有专门的测试或不需要这么测）
SKIP_RUN = {
    "window": "会把所有窗口最小化",
    "volume": "会把系统音量调大",
    "media_control": "会切换播放 / 暂停",
    "mouse_move": "会把鼠标挪到屏幕左上角",
    "mouse_click": "会在用户当前界面上真的点一下",
    "mouse_drag": "会在用户当前界面上真的拖一下",
    "mouse_scroll": "会滚动用户当前的窗口",
    "look_at_screen": "会截图并真的调用视觉模型（费 token）",
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def static_audit() -> None:
    print("静态一致性（声明 vs 实现）")
    bad_signature: list[str] = []
    bad_required: list[str] = []
    bad_property: list[str] = []
    for name, tool in sorted(tools.REGISTRY.items()):
        schema = tool.parameters or {}
        properties = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        # 1) required 里写的参数必须真的声明过
        for key in required:
            if key not in properties:
                bad_required.append(name + "." + key)
        try:
            signature = inspect.signature(tool.handler)
        except (TypeError, ValueError):
            continue
        for key, parameter in signature.parameters.items():
            if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
                continue
            # 2) handler 认的参数必须在 schema 里，否则模型永远不会传它
            if key not in properties:
                bad_property.append(name + "." + key)
            # 3) 没有默认值的参数必须是 required，否则模型可以合法地不传，
            #    然后 handler 就 TypeError 了
            if parameter.default is parameter.empty and key not in required:
                bad_signature.append(name + "." + key)
    check("required 里的参数都真的声明过", not bad_required, "、".join(bad_required[:6]))
    check("handler 的参数都写进了 schema", not bad_property, "、".join(bad_property[:6]))
    check("没有默认值的参数都标了必填", not bad_signature, "、".join(bad_signature[:6]))
    check("工具都有名字和说明",
          all(t.name and t.description.strip() for t in tools.REGISTRY.values()))
    check("工具表非空", len(tools.REGISTRY) >= 40, str(len(tools.REGISTRY)) + " 个")


def run_audit() -> None:
    print("空参数试运行")
    broken: list[str] = []
    silent: list[str] = []
    denied = 0
    skipped = 0
    for name, tool in sorted(tools.REGISTRY.items()):
        if name in SKIP_RUN:
            skipped += 1
            continue
        try:
            # 确认通道给"拒绝"：敏感工具因此走拒绝分支，既验证了闸门，
            # 又不会真的关机 / 执行命令
            result = tools.call_result(name, {}, on_confirm=lambda _q: False)
        except Exception:  # noqa: BLE001 - 工具层本该自己吞掉异常
            broken.append(name + "（抛异常：" + traceback.format_exc().strip().splitlines()[-1][:60] + "）")
            continue
        if result.code == "denied":
            denied += 1
            if result.text != tools.CANCEL_REPLY:
                broken.append(name + "（拒绝时没说人话）")
            continue
        if not str(result.text or "").strip():
            silent.append(name)
    check("所有工具都不会抛异常", not broken, "、".join(broken[:5]))
    check("所有工具都不会返回空字符串（静默失效）", not silent, "、".join(silent[:8]))
    check("敏感工具在没有确认通道时一律拒绝", denied >= 3, str(denied) + " 个")
    print("    （跳过 " + str(skipped) + " 个会打扰用户的工具，见 SKIP_RUN）")

    # 空参数时应该"问清楚要做什么"，而不是含糊地返回成功
    vague = []
    for name in ("open_app", "open_url", "read_file", "type_text", "press_keys", "web_search"):
        text = str(tools.call(name, {}, on_confirm=lambda _q: False))
        if not any(word in text for word in ("没说", "没听清", "要打开", "要输入", "要按", "要搜")):
            vague.append(name + "：" + text[:24])
    check("必填参数缺失时会说清楚缺什么", not vague, "、".join(vague))


def live_tools() -> None:
    """真的会干活的那几个：截屏、缩放、找图、剪贴板、窗口列表。

    找图这一项特别值得测：截图的原点是**虚拟桌面**的左上角，多显示器时
    它可能是个负数。按 0 算的话，图找得到、坐标却是错的 —— 点下去点到
    另一块屏幕上，而且一点报错都没有。
    """
    print("真的干活的工具")
    import tempfile

    from voice_agent import screen

    # 截屏
    text = str(tools.call("screenshot", {}))
    # 工具只说文件名（"存到图片文件夹里的 xxx.png"），目录得自己补上
    from voice_agent.tools._shared import SCREENSHOT_DIR

    shot_path = None
    for token in text.replace("：", " ").replace("，", " ").split():
        if token.lower().endswith(".png"):
            candidate = SCREENSHOT_DIR / token
            if candidate.is_file():
                shot_path = candidate
    check("截图工具真的存下了文件", shot_path is not None,
          (str(shot_path) if shot_path else text[:60]))

    import numpy as np

    grab = screen.grab_screen()
    left, top, width, height = screen._virtual_screen()
    check("截屏尺寸等于虚拟桌面尺寸",
          grab.shape[1] == width and grab.shape[0] == height,
          str(grab.shape[1]) + "x" + str(grab.shape[0]) + " 期望 " + str(width) + "x" + str(height))

    with tempfile.TemporaryDirectory() as tmp:
        # 缩放
        from PIL import Image

        source = Path(tmp) / "src.png"
        Image.fromarray(np.zeros((200, 400, 3), dtype=np.uint8) + 90).save(source)
        resized = Path(tmp) / "small.png"
        outcome = screen.resize_image(source, width=120, out=resized, quality=80)
        with Image.open(resized) as img:
            check("缩放工具真的产出了小图",
                  img.width == 120 and img.height == 60,
                  str(img.width) + "x" + str(img.height))
        check("缩放结果带了字节数", int(outcome.get("bytes") or 0) > 0, str(outcome.get("bytes")))

        # 找图：从当前屏幕里裁一块纹理最丰富的当作模板，再让它在屏幕上找回来
        patch_w, patch_h = 180, 110
        best = None
        for ix in range(left + 60, left + width - patch_w - 60, 137):
            for iy in range(top + 60, top + height - patch_h - 60, 91):
                px, py = ix - left, iy - top
                patch = grab[py:py + patch_h, px:px + patch_w]
                if patch.shape[0] != patch_h or patch.shape[1] != patch_w:
                    continue
                deviation = float(patch.std())
                if best is None or deviation > best[0]:
                    best = (deviation, ix, iy)
        if best is None or best[0] < 6:
            print("  [跳过] 屏幕太单调（找不到有纹理的区域），找图这项没法验")
            return
        _, ix, iy = best
        template = Path(tmp) / "patch.png"
        import cv2

        cv2.imwrite(str(template), grab[iy - top:iy - top + patch_h, ix - left:ix - left + patch_w])
        hits = screen.find_template(template, confidence=0.92, scales=(1.0,), limit=3)
        expected = (ix + patch_w // 2, iy + patch_h // 2)
        check("屏幕上找得到刚裁下来的那块图", bool(hits),
              str([(h["x"], h["y"]) for h in hits]))
        if hits:
            # 找图返回的必须是**屏幕坐标**，不是截图里的像素坐标
            nearest = min(hits, key=lambda h: abs(h["x"] - expected[0]) + abs(h["y"] - expected[1]))
            check("找图返回的是屏幕坐标（多显示器时原点可能是负的）",
                  abs(nearest["x"] - expected[0]) <= 3 and abs(nearest["y"] - expected[1]) <= 3,
                  "返回 " + str((nearest["x"], nearest["y"])) + " 期望 " + str(expected))

    # 剪贴板与窗口列表走的是 PowerShell，顺手确认它们没被静默搞坏
    check("剪贴板读得回来", isinstance(tools.call("clipboard", {"action": "get"}), str))
    windows_text = str(tools.call("list_windows", {}))
    check("窗口列表非空", len(windows_text) > 4, windows_text[:40])


def vision_path() -> None:
    """看图链路：截图要缩到配置的上限，而且必须告诉模型坐标怎么换算。

    少了缩放和原点这两条信息，模型给的坐标就会错得离谱 ——
    多显示器时截图原点是负的，缩过的图坐标还要乘回去。
    """
    print("看图链路")
    from voice_agent import screen
    from voice_agent.tools import vision as vision_mod

    seen: dict = {}

    def fake_handler(path: str, question: str, meta=None) -> str:  # noqa: ANN001
        seen.update(meta or {})
        seen["question"] = question
        seen["exists"] = Path(path).is_file()
        return "看图完成"

    original = vision_mod._VISION_HANDLER[0]
    try:
        vision_mod.set_vision_handler(fake_handler)

        vision_mod.set_vision_max_side(1280)
        outcome = vision_mod.look_at_screen_tool("屏幕上有什么")
        check("看图工具把图交给了视觉处理器", outcome == "看图完成", outcome[:50])
        if outcome != "看图完成":
            # 截屏本身失败（锁屏、桌面被切走）就没法往下验了，直接跳过
            print("  [跳过] 截屏没成功，看图链路这一节跳过")
            return
        check("图真的存在", bool(seen.get("exists")))
        left, top, width, height = screen._virtual_screen()
        check("带上了原图尺寸（虚拟桌面）",
              tuple(seen.get("original") or ()) == (width, height),
              str(seen.get("original")))
        check("带上了坐标原点（多显示器时可能是负的）",
              tuple(seen.get("origin") or ()) == (left, top), str(seen.get("origin")))
        size = seen.get("size") or (0, 0)
        check("缩放比例自洽",
              abs(seen["original"][0] / size[0] - seen["scale"]) < 0.02,
              str(seen["scale"]))
        check("图片不超过配置的最长边", max(size) <= 1280, str(size))

        vision_mod.set_vision_max_side(640)
        check("改小分辨率后真的缩得更小",
              vision_mod.look_at_screen_tool("再看一眼") == "看图完成"
              and max(seen.get("size") or (9999,)) <= 640,
              str(seen.get("size")))
        vision_mod.set_vision_max_side(1280)
    finally:
        vision_mod.set_vision_handler(original)


def self_control() -> None:
    """控制程序自己：开新会话 / 重启 / 退出。

    重启和退出会**先回答再动手**（延迟一秒多），否则用户听到的是
    "它答应了然后就没了"。这里验证请求确实落到了界面的回调上。
    """
    print("控制程序自己")
    from voice_agent.agent import VoiceAgent
    from voice_agent.config import Config

    config = Path(_BUILD.name) / "config.yaml"
    config.write_text("wake:\n  keywords: [测试词]\n", encoding="utf-8")
    agent = VoiceAgent(Config.load(config), log=lambda _m: None)
    requests: list[str] = []
    agent.app_hook = requests.append

    agent.brain.history.extend([{"role": "user", "content": "之前说的话"},
                                {"role": "assistant", "content": "之前答的话"}])
    agent.brain.recent_actions.append("open_app(微信) → 已经打开微信")
    check("新会话前上下文是有东西的", bool(agent.brain.history))
    message = tools.call("new_session", {}, on_confirm=lambda _q: True)
    check("开新会话会明确回一句", "先放一边" in message or "从头" in message, message[:30])
    check("新会话清空了上下文",
          not agent.brain.history and not agent.brain.recent_actions,
          str(len(agent.brain.history)))
    check("界面上留了一条分界", any("新会话" in str(t.get("text", ""))
                                    for t in agent.transcript))

    # 敏感操作没有确认通道就是拒绝，而且不能真的重启
    denied = tools.call_result("restart_self", {}, on_confirm=lambda _q: False)
    check("重启没有确认通道时被拒绝",
          denied.code == "denied" and not requests, str(denied.code))
    denied_quit = tools.call_result("quit_self", {}, on_confirm=lambda _q: False)
    check("退出没有确认通道时被拒绝",
          denied_quit.code == "denied" and not requests, str(denied_quit.code))

    answer = tools.call_result("restart_self", {}, on_confirm=lambda _q: True)
    check("确认后先回话再动手", "重启" in answer.text and not requests, answer.text[:30])
    deadline = time.time() + 4.0
    while time.time() < deadline and not requests:
        time.sleep(0.1)
    check("延迟之后真的把重启请求交给界面了", requests == ["restart"], str(requests))

    # 退出同理（用一个新的 agent，免得被上一次的"只认第一次"挡住）
    quitter = VoiceAgent(Config.load(config), log=lambda _m: None)
    quit_requests: list[str] = []
    quitter.app_hook = quit_requests.append
    tools.call("quit_self", {}, on_confirm=lambda _q: True)
    deadline = time.time() + 4.0
    while time.time() < deadline and not quit_requests:
        time.sleep(0.1)
    check("退出请求也交给了界面", quit_requests == ["quit"], str(quit_requests))


def main() -> int:
    print("=== 工具层体检 ===")
    static_audit()
    run_audit()
    live_tools()
    vision_path()
    self_control()
    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("工具层全部通过（共 " + str(len(tools.REGISTRY)) + " 个工具）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
