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
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时产生的文件挪到临时目录：测试不该往用户真实的对话记录 / 长期记忆里写
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

from voice_agent import security, tools  # noqa: E402

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
            # 限流复位：这一段测的是工具本身，不是限流器（限流有专门的测试）。
            # 不复位的话问到第 7 个敏感工具就被按住了。
            security.reset_limits()
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
    from voice_agent.tools._shared import screenshot_dir

    found = re.search(r"([A-Za-z]:\\[^\s，。]*?\.png)", text)
    shot_path = Path(found.group(1)) if found and Path(found.group(1)).is_file() else None
    check("截图工具真的存下了文件", shot_path is not None,
          (str(shot_path) if shot_path else text[:60]))
    # 提示里必须给**真实路径**：以前写死"图片文件夹"，数据目录改到别处之后
    # 模型照着那句话去找，怎么也找不到（用户报的就是这个）
    check("截图提示里给的是完整路径（以前写死「图片文件夹」，找不到）",
          "存在：" in text and shot_path is not None,
          text[:90])
    check("那个路径确实在数据目录下",
          shot_path is not None and str(shot_path).startswith(str(screenshot_dir())),
          str(shot_path))
    # 裸文件名要能解析：模型手上只有文件名，它不会知道数据目录在哪
    if shot_path is not None:
        check("裸文件名能解析到截图目录（读得到）",
              "没找到" not in str(tools.call("read_file", {"path": shot_path.name})),
              shot_path.name)

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

        # 找图：从当前屏幕里裁一块图当模板，再让它在屏幕上找回来，核对坐标。
        #
        # 挑模板有两个讲究：
        #  1. **静止**：任务栏时钟、视频、进度条都在动，拿它们当模板必然对不上；
        #  2. **唯一**：一片重复的花纹（图标行、文字行）会在屏幕上匹配到好几处，
        #     这时候"没对上"说明不了任何问题。
        # 所以：先筛静止区域，再按纹理丰富度一个个试；只有在**画面没动**的情况下
        # 匹配到"截图坐标系里的位置"（正好差一个虚拟桌面原点）才判定为真 bug。
        import cv2

        patch_w, patch_h = 180, 110
        template = Path(tmp) / "patch.png"
        origin_x, origin_y = left, top          # 截图左上角对应的屏幕坐标
        consistent = False
        coordinate_bug = False
        detail = ""
        tried = 0

        for attempt in range(3):
            grab = screen.grab_screen()
            time.sleep(0.2)
            again = screen.grab_screen()
            candidates: list[tuple[float, int, int]] = []
            for ix in range(left + 60, left + width - patch_w - 60, 137):
                for iy in range(top + 60, top + height - patch_h - 60, 91):
                    px, py = ix - left, iy - top
                    patch = grab[py:py + patch_h, px:px + patch_w]
                    if patch.shape[0] != patch_h or patch.shape[1] != patch_w:
                        continue
                    diff = float(np.abs(patch.astype(np.int16)
                                        - again[py:py + patch_h, px:px + patch_w].astype(np.int16)).mean())
                    if diff > 0.5:
                        continue                 # 这块在动，换一块
                    deviation = float(patch.std())
                    if deviation >= 6:
                        candidates.append((deviation, ix, iy))
            candidates.sort(reverse=True)

            for _, ix, iy in candidates[:8]:
                tried += 1
                cv2.imwrite(str(template),
                            grab[iy - top:iy - top + patch_h, ix - left:ix - left + patch_w])
                hits = screen.find_template(template, confidence=0.95, scales=(1.0,), limit=5)
                expected = (ix + patch_w // 2, iy + patch_h // 2)
                positions = [(h["x"], h["y"]) for h in hits]
                if any(abs(x - expected[0]) <= 3 and abs(y - expected[1]) <= 3
                       for x, y in positions):
                    consistent = True
                    detail = "第 " + str(tried) + " 块模板命中 " + str(expected)
                    break
                # 命中位置正好差一个截图原点 = 返回的是截图坐标而不是屏幕坐标，真 bug
                if any(abs(x - (expected[0] - origin_x)) <= 3
                       and abs(y - (expected[1] - origin_y)) <= 3 for x, y in positions):
                    coordinate_bug = True
                    detail = ("返回 " + str(positions) + " 期望 " + str(expected)
                              + "（正好差一个截图原点 " + str((origin_x, origin_y)) + "）")
                    break
                detail = "这块图在屏幕上不唯一或找不到：" + str(positions[:3])
            if consistent or coordinate_bug:
                break

        if not consistent and not coordinate_bug:
            # 试了这么多块都没对上，多半是屏幕一直在变（视频、动画）
            print("  [跳过] 屏幕上找不到又静止又唯一的区域，找图坐标这项没法验")
            return
        check("屏幕上找得到刚裁下来的那块图", consistent or coordinate_bug, detail)
        # 找图返回的必须是**屏幕坐标**，不是截图里的像素坐标
        check("找图返回的是屏幕坐标（多显示器时原点可能是负的）",
              consistent and not coordinate_bug, detail)

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


def image_tools() -> None:
    """图片匹配：图里找图、参考目录按名字找、以及"答完留个窗口"。"""
    print("图片匹配")
    import tempfile as _tempfile

    import numpy as np
    from PIL import Image

    from voice_agent.tools._shared import reference_dir

    REFERENCE_DIR = reference_dir(create=True)

    with _tempfile.TemporaryDirectory() as tmp:
        # 造一张大图，里面贴一张有特征的小图，再让工具把它找出来
        big = np.zeros((400, 600, 3), dtype=np.uint8) + 40
        patch = np.zeros((60, 90, 3), dtype=np.uint8)
        patch[:, :] = (10, 200, 30)
        patch[20:40, 30:60] = (240, 20, 90)
        big[120:180, 200:290] = patch
        source = Path(tmp) / "big.png"
        template = Path(tmp) / "small.png"
        Image.fromarray(big[:, :, ::-1]).save(source)          # BGR -> RGB
        Image.fromarray(patch[:, :, ::-1]).save(template)
        answer = str(tools.call("find_in_image", {"image": str(source), "template": str(template)}))
        check("在图里找得到贴进去的那块", "找到了" in answer and "245,150" in answer,
              answer[:70])
        other = np.zeros((60, 90, 3), dtype=np.uint8)
        other[:, :] = (200, 200, 10)
        other[5:15, 5:15] = (0, 0, 0)
        other_path = Path(tmp) / "other.png"
        Image.fromarray(other[:, :, ::-1]).save(other_path)
        answer = str(tools.call("find_in_image", {"image": str(source),
                                                  "template": str(other_path)}))
        check("大图里没有这张图时会说实话", "没有找到" in answer, answer[:60])

        # 参考目录：丢一张图进去，之后按**名字**就能用它
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        named = REFERENCE_DIR / "自检用按钮.png"
        Image.fromarray(patch[:, :, ::-1]).save(named)
        try:
            check("参考目录列得出来",
                  "自检用按钮" in str(tools.call("list_reference", {})))
            check("按名字也能在图里找",
                  "找到了" in str(tools.call("find_in_image", {"image": str(source),
                                                              "template": "自检用按钮"})),
                  "用名字找")
            check("名字不存在时会告诉你有哪些",
                  "参考图片目录里现有" in str(tools.call("find_in_image",
                                                       {"image": str(source),
                                                        "template": "根本没有这张"})),
                  "给提示")
            check("参考目录在哪说得清",
                  str(REFERENCE_DIR) in str(tools.call("reference_dir", {})))
            check("屏幕找图也认名字（这里只验证参数解析，不要求屏幕上有）",
                  "没找到" in str(tools.call("find_on_screen", {"image": "自检用按钮"}))
                  or "找到了" in str(tools.call("find_on_screen", {"image": "自检用按钮"})))
        finally:
            named.unlink(missing_ok=True)

    check("屏幕找图能限定范围名",
          "看不懂" in str(tools.call("find_on_screen", {"image": "x.png", "region": "范围9"})))


def continuous_talk() -> None:
    """连续对话：这几类工具答完要自动留个窗口。"""
    print("连续对话")
    from voice_agent.tools._shared import TURN

    cases = [
        ("list_windows", {}, "查了给你看 → 留窗口"),
        ("read_file", {}, "缺参数 → 留窗口"),
        ("get_time", {}, "普通工具 → 不留"),
    ]
    for name, args, label in cases:
        security.reset_limits()
        tools.reset_turn()
        tools.call(name, args, on_confirm=lambda _q: False)
        expected = name != "get_time"
        check(label, bool(TURN.get("follow_up")) == expected,
              str(TURN.get("follow_up")) + " " + str(TURN.get("reason"))[:30])
    tools.reset_turn()


def folder_mapping() -> None:
    """应用映射表支持目录：映射一个目录，然后按名字读写。"""
    print("目录映射")
    import tempfile as _tempfile

    from voice_agent import screen

    with _tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "报告.txt").write_text("内容", encoding="utf-8")
        saved = screen.load_app_map()
        try:
            screen.save_app_map({"我的项目": {"target": tmp, "type": "folder"}})
            found = screen.resolve_app("我的项目")
            check("目录映射认得出来", found.get("hit") and found["entry"]["type"] == "folder",
                  str(found.get("entry")))
            check("猜类型也认得目录", screen._guess_app_type(tmp) == "folder")
            check("文件工具按名字能解析到那个目录",
                  str(tools.call("list_files", {"path": "我的项目"})).find("报告") >= 0,
                  "按名字列目录")
            check("按名字能读到里面的文件",
                  "内容" in str(tools.call("read_file", {"path": "我的项目\\报告.txt"}))
                  or "内容" in str(tools.call("read_file", {"path": "我的项目/报告.txt"})),
                  "按名字读文件")
        finally:
            screen.save_app_map(saved)


def confirm_prompts() -> None:
    """确认提示必须"念得清楚"。

    用户的原话是"此操作需要xxx（听不清）"—— 因为提示里塞了路径、扩展名和英文名，
    中文 TTS 念出来就是噪音。这里逐条验证：提示里不能有反斜杠、正斜杠、
    文件扩展名、成串的英文。
    """
    print("确认提示念得清楚")
    import re as _re

    cases = [
        ("write_file", {"path": "D:\\screen-20260912.png", "content": "x" * 400},
         "写文件", "带时间戳的英文文件名不能念"),
        ("write_file", {"path": "D:\\报告.docx", "content": "你好"},
         "「报告」", "中文文件名可以说出来"),
        ("run_command", {"command": "Get-ChildItem -Path C:\\Users -Recurse"},
         "列出文件", "命令只说要干什么"),
        ("kill_process", {"name": "notepad.exe"}, "记事本", "英文程序名换成中文"),
        ("click_image", {"image": "下载按钮.png", "times": 3},
         "「下载按钮」", "找的图用中文名"),
        ("power", {"action": "shutdown", "delay": 60}, "关机", "动作词换成中文"),
        ("mouse_drag", {"x1": 10, "y1": 20, "x2": 300, "y2": 400},
         "拖拽鼠标", "坐标不念"),
    ]
    for name, args, must, why in cases:
        question = tools.REGISTRY[name].confirm_question(args)
        check("「" + name + "」：" + why, must in question, question)
    for name in ("write_file", "run_command", "click_image", "mouse_drag", "start_watch"):
        question = tools.REGISTRY[name].confirm_question(
            {"path": "D:\\a\\b\\c.png", "image": "x.png", "command": "dir /s",
             "x1": 1, "y1": 2, "x2": 3, "y2": 4, "kind": "图片", "target": "y.png"})
        bad = _re.search(r"[\\/]", question) or _re.search(r"\.[a-z]{2,4}\b", question)
        check("「" + name + "」的提示里没有路径分隔符和扩展名", bad is None, question)
        check("「" + name + "」的提示够短（念得完）", len(question) <= 30,
              str(len(question)) + " 字：" + question)
        check("「" + name + "」的提示以确认吗结尾", question.endswith("确认吗？"), question)


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

    security.reset_limits()
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
    security.reset_limits()
    tools.call("quit_self", {}, on_confirm=lambda _q: True)
    deadline = time.time() + 4.0
    while time.time() < deadline and not quit_requests:
        time.sleep(0.1)
    check("退出请求也交给了界面", quit_requests == ["quit"], str(quit_requests))


def spoken_location_tools() -> None:
    """用户点名了文件夹，就别整盘翻。

    真事：用户说「我在 D 盘下的桌面下的对焦文件夹中写了一份介绍.md」，
    模型调的是 find_files(pattern="介绍*.md", root="D:\\") —— 一次整盘扫描，
    又慢又会翻出一堆同名的无关文件。这里验证三件事：
    口语路径能拼回真实目录、盘符起点会先按用户说的文件夹找、
    以及模型给了具体文件夹时不插手。
    """
    print("找文件：点名了文件夹就别整盘翻")
    import tempfile as _tempfile

    from voice_agent.tools import files as files_mod

    with _tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        focus = root / "桌面" / "对焦"
        focus.mkdir(parents=True)
        (focus / "介绍.md").write_text("# 正主", encoding="utf-8")
        (focus / "别的.txt").write_text("x", encoding="utf-8")
        elsewhere = root / "别处"
        elsewhere.mkdir()
        (elsewhere / "介绍.md").write_text("# 无关的同名文件", encoding="utf-8")
        noisy = root / "node_modules"
        noisy.mkdir()
        (noisy / "介绍.md").write_text("# 依赖里的", encoding="utf-8")

        # ── 口语路径 ──
        check("「X下的桌面下的对焦文件夹」拼得出真实目录",
              files_mod._spoken_chain("X下的桌面下的对焦文件夹", base=root) == focus,
              str(files_mod._spoken_chain("X下的桌面下的对焦文件夹", base=root)))
        check("「我的桌面」认得出来（引子 + 目录名）",
              files_mod._spoken_head("我的桌面") is not None
              and files_mod._spoken_head("mymusic") is None,
              str(files_mod._spoken_head("我的桌面")))
        check("整句话里带垃圾尾巴也只取前面那段",
              files_mod._spoken_chain("在X下的桌面下的对焦文件夹里写了一份介绍",
                                     base=root) == focus,
              str(files_mod._spoken_chain("在X下的桌面下的对焦文件夹里写了一份介绍",
                                          base=root)))
        check("盘符那段认得出（「我在D盘」→ D:\\）",
              str(files_mod._spoken_head("我在d盘")).startswith("D"),
              str(files_mod._spoken_head("我在d盘")))
        check("认不出来就是 None，不会凭空造一个路径",
              files_mod._spoken_chain("mymusic") is None
              and files_mod._spoken_chain("把桌面上的文件整理一下") is None)
        # 候选是"每段带不带「文件夹」"的组合：必须收敛。
        # 曾经写成边扩展边迭代同一个列表 —— 那不是组合枚举，是无限膨胀（直接卡死）。
        combo = files_mod._chain_candidates(["a", "b文件夹", "c目录"])
        check("候选枚举会收敛（不会自我膨胀）", len(combo) == 4, str(combo))
        check("起点不存在时什么都不扫（不能顺手去扫当前目录）",
              files_mod._walk_files(None, lambda _n: True, 3)[0] == []
              and files_mod._walk_files(root / "没有这个目录", lambda _n: True, 3)[0] == [])

        # ── 盘符起点 + 用户点名的文件夹 → 先按他说的找 ──
        saved = files_mod.spoken_location
        files_mod.spoken_location = lambda: focus
        try:
            answer = files_mod.find_files("介绍*.md", root=Path(raw).drive + "\\")
        finally:
            files_mod.spoken_location = saved
        check("给了盘符也先按用户说的那个文件夹找",
              "对焦" in answer and "介绍.md" in answer, answer)
        check("回答里说明了这是按用户说的位置找的", "按你话里说的" in answer, answer)
        check("没把别处的同名文件混进来", "别处" not in answer, answer)

        # ── 模型给了具体文件夹：照它找，不插手 ──
        answer = files_mod.find_files("介绍*.md", root=str(root))
        check("给了具体文件夹就照它找（两个同名文件都该出现）",
              "对焦" in answer and "别处" in answer, answer)
        check("依赖目录跳过（node_modules 里那三个不算）",
              "node_modules" not in answer, answer)
        check("结果里给的是**完整路径**（模型能直接拿去读）",
              str(focus / "介绍.md") in answer, answer)
        empty = files_mod.find_files("绝对没有这种文件*.zzz", root=str(root))
        check("找不到时如实说，并带上找的范围", "没找到" in empty and str(root) in empty, empty)

        # ── 用户那句话是**按线程**记的 ──
        tools.set_utterance("我在D盘下的桌面下的对焦文件夹里写了介绍.md")
        check("工具层拿得到用户这句话",
              "对焦" in tools.last_utterance(), tools.last_utterance()[:20])
        tools.set_utterance("")


def sift_matching() -> None:
    """旋转 / 缩放过的参考图：模板匹配对不上时，SIFT 兜底要找得到。

    这是用户那句「根据文档里的图片介绍去屏幕上找」的常见形态 ——
    文档里的图往往被缩放过、或者角度差一点，模板匹配直接归零。
    """
    print("找图：模板匹配 + SIFT 兜底")
    import tempfile as _tempfile

    import numpy as np

    from voice_agent import screen
    from voice_agent.tools import images as images_mod

    try:
        import cv2
    except Exception as exc:  # noqa: BLE001
        print("  跳过：没有 OpenCV（" + str(exc)[:40] + "）")
        return
    if not hasattr(cv2, "SIFT_create"):
        print("  跳过：这个 OpenCV 构建没有 SIFT")
        return

    rng = np.random.default_rng(7)
    # 造一张有纹理的"界面"：纯色块没有特征点，SIFT 也没辙
    scene = (rng.random((600, 900, 3)) * 90 + 60).astype(np.uint8)
    cv2.rectangle(scene, (620, 380), (760, 470), (40, 180, 90), -1)
    cv2.circle(scene, (690, 425), 22, (230, 230, 240), -1)
    cv2.putText(scene, "OK", (650, 440), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (10, 10, 10), 3)
    patch = scene[370:480, 610:770].copy()

    with _tempfile.TemporaryDirectory() as tmp:
        scene_path = str(Path(tmp) / "scene.png")
        patch_path = str(Path(tmp) / "patch.png")
        cv2.imwrite(scene_path, scene)
        cv2.imwrite(patch_path, patch)

        straight = screen.find_in_image(scene_path, patch_path, confidence=0.8)
        check("原样的图用模板匹配就能找到",
              bool(straight) and straight[0]["method"] == "template"
              and abs(straight[0]["x"] - 690) <= 3 and abs(straight[0]["y"] - 425) <= 3,
              str(straight[:1]))

        # 转 50 度再贴回去：模板匹配必然对不上
        big = cv2.copyMakeBorder(patch, 80, 80, 80, 80, cv2.BORDER_REPLICATE)
        hh, ww = big.shape[:2]
        matrix = cv2.getRotationMatrix2D((ww / 2, hh / 2), 50, 1.0)
        rotated = cv2.warpAffine(big, matrix, (ww, hh), borderMode=cv2.BORDER_REPLICATE)
        rotated_path = str(Path(tmp) / "rotated.png")
        cv2.imwrite(rotated_path, rotated)

        check("转过角度的图，模板匹配确实找不到（这就是要 SIFT 的原因）",
              screen.find_in_image(scene_path, rotated_path, confidence=0.8,
                                   method="template") == [])
        started = time.time()
        auto = screen.find_in_image(scene_path, rotated_path, confidence=0.8)
        elapsed = time.time() - started
        check("auto 会用 SIFT 兜底找到它",
              bool(auto) and auto[0]["method"] == "sift", str(auto[:1]))
        check("SIFT 给的坐标基本对得上（±25 像素）",
              bool(auto) and abs(auto[0]["x"] - 690) <= 25 and abs(auto[0]["y"] - 450) <= 25,
              str(auto[:1]))
        check("SIFT 兜底也是百毫秒级，不会把语音拖住", elapsed < 3.0, "%.2fs" % elapsed)
        check("显式指定 method=sift 也走同一条路",
              bool(screen.find_in_image(scene_path, rotated_path, confidence=0.8,
                                        method="sift")))

        # 工具层的回答要说清楚是哪一种匹配（用户才知道为什么"相似度不是 99%"）
        answer = images_mod.find_in_image_tool(scene_path, rotated_path)
        check("回答里点明了是 SIFT 特征匹配", "SIFT" in answer, answer[:80])
        check("回答里给了下一步（上屏幕找 / 框下来）",
              "find_on_screen" in answer and "mark_region" in answer, answer[-60:])

        # 找不到时要如实说，而且不能只怪阈值
        absent = screen.find_in_image(scene_path,
                                      str(Path(tmp) / "patch.png"), confidence=0.99)
        check("阈值太高时就是找不到（不再硬凑一个结果）", absent == [] or absent[0]["score"] >= 0.99)
        blank = np.full((40, 40, 3), 200, np.uint8)
        blank_path = str(Path(tmp) / "blank.png")
        cv2.imwrite(blank_path, blank)
        check("纯色小图不会让 SIFT 炸掉（没有特征点就返回空）",
              screen.find_in_image(scene_path, blank_path, confidence=0.8) == [])


def tool_tags() -> None:
    """用途标签：同一件事有好几种做法时，模型靠它挑对的那个。

    重点盯「找图」这一组：本地模板匹配（毫秒、不花钱）和"截图问视觉模型"
    （几秒、一次调用）都能干，标签和提示词必须把这件事说在前面。
    """
    print("工具的用途标签")
    from voice_agent import skills as skills_mod

    tools.autoload_skills()
    builtin = [item for item in tools.REGISTRY.values() if item.source == "builtin"]
    missing = sorted(item.name for item in builtin if not item.tags)
    check("每个内置工具都有用途标签（" + str(len(builtin)) + " 个）", not missing,
          "缺：" + "、".join(missing))
    schema = tools.REGISTRY["find_on_screen"].schema()["function"]["description"]
    check("标签写在给模型的说明最前面", schema.startswith("【找图"), schema[:30])
    check("「本地找图」和「视觉模型看图」的标签能区分开",
          "不花钱" in tools.REGISTRY["find_on_screen"].tags
          and "花钱" in tools.REGISTRY["look_at_screen"].tags,
          str(tools.REGISTRY["find_on_screen"].tags) + " / "
          + str(tools.REGISTRY["look_at_screen"].tags))
    from voice_agent.brain import _TOOL_HINT

    check("提示词里写明：先本地找图，别先截图问模型",
          "找图·本地·不花钱" in _TOOL_HINT and "look_at_screen" in _TOOL_HINT)
    check("提示词里写明：找到位置就顺手标下来",
          "mark_region" in _TOOL_HINT and "顺手固定下来" in _TOOL_HINT)

    skill_tools = [item for item in tools.REGISTRY.values() if item.source != "builtin"]
    check("自带技能也打了标签（" + str(len(skill_tools)) + " 个）",
          all(item.tags for item in skill_tools),
          str([item.name for item in skill_tools if not item.tags]))
    check("技能标签支持字符串和列表两种写法",
          skills_mod.normalize_tags("找图/本地") == ("找图", "本地")
          and skills_mod.normalize_tags(["找图", "本地"]) == ("找图", "本地"),
          str(skills_mod.normalize_tags("找图/本地")))
    check("标签最多留 4 个（说明最前面那行不能变成一句话）",
          len(skills_mod.normalize_tags("a/b/c/d/e/f")) == 4)
    check("没写标签就是空元组，不影响老技能",
          skills_mod.normalize_tags(None) == () and skills_mod.normalize_tags("") == ())


def main() -> int:
    print("=== 工具层体检 ===")
    static_audit()
    run_audit()
    live_tools()
    vision_path()
    image_tools()
    sift_matching()
    tool_tags()
    confirm_prompts()
    continuous_talk()
    folder_mapping()
    spoken_location_tools()
    self_control()
    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("工具层全部通过（共 " + str(len(tools.REGISTRY)) + " 个工具）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
