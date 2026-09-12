# -*- coding: utf-8 -*-
"""屏幕标记：框选范围（范围1、范围2…）、标记点（点1、点2…），以及它们怎么被用起来。

这套东西的价值在于"语音指代"：跟助手说话没法用手指，于是先框一下，
之后说「看看范围1」「截一下范围2」它就知道说的是哪儿。

运行：python tests/test_marks.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_BUILD_DIR"] = _BUILD.name

from voice_agent import marks as marks_mod  # noqa: E402
from voice_agent import screen, tools  # noqa: E402

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    print("=== 屏幕标记 ===")
    store = marks_mod.store

    print("命名与增删")
    store.clear()
    first = store.add_region(100, 200, 500, 400)
    second = store.add_region(-900, 50, -400, 300)
    check("框选自动编号", (first.name, second.name) == ("范围1", "范围2"),
          first.name + "/" + second.name)
    check("尺寸和中心算得对",
          (first.width, first.height, first.center) == (400, 200, (300, 300)),
          str(first.as_dict()))
    check("坐标反着给也能用", store.add_region(900, 500, 300, 100).rect == (300, 100, 900, 500))
    point = store.add_point(800, 450)
    check("点自动编号", point.name == "点1", point.name)
    check("点也能算中心", point.center == (800, 450))
    check("列得出来", "范围1" in store.describe() and "点1" in store.describe(),
          store.describe()[:60])

    print("\n按名字找（认各种写法）")
    for alias in ("范围1", "范围 1", "region1", "REGION 1"):
        check("认「" + alias + "」", (store.get(alias) or type("x", (), {"name": ""})()).name == "范围1",
              alias)
    check("只写「范围」给最近的那个",
          (store.get("范围") or first).name == "范围3", (store.get("范围") or first).name)
    check("认「点1」", (store.get("点1") or point).name == "点1")
    check("不存在的名字返回空", store.get("范围9") is None)

    print("\n解析成矩形（给截图 / 看图用）")
    check("名字能解析", marks_mod.resolve_region("范围1") == (100, 200, 500, 400),
          str(marks_mod.resolve_region("范围1")))
    check("点会解析成一小块（不是 1×1 的图）",
          marks_mod.resolve_region("点1") == (640, 330, 960, 570),
          str(marks_mod.resolve_region("点1")))
    check("四个数也能解析", marks_mod.resolve_region("10,20,300,400") == (10, 20, 300, 400),
          str(marks_mod.resolve_region("10,20,300,400")))
    check("数不够就不认", marks_mod.resolve_region("10,20") is None)
    check("乱七八糟的不认", marks_mod.resolve_region("那块地方") is None)

    print("\n工具层")
    check("列标记工具", "范围1" in tools.call("list_marks", {}))
    answer = tools.call("mark_region", {"x1": 10, "y1": 10, "x2": 210, "y2": 110,
                                        "note": "下载按钮"})
    check("新增范围会回一句人话", "范围4" in answer and "200×100" in answer, answer[:60])
    check("note 存下来了", (store.get("范围4") or first).note == "下载按钮")
    # 同名再框一次 = 调整那块，而不是又建一块
    before = len(store.all())
    answer = tools.call("mark_region", {"x1": 0, "y1": 0, "x2": 500, "y2": 400,
                                        "name": "范围4"})
    check("同名是「改」不是「再建一块」",
          len(store.all()) == before and (store.get("范围4") or first).rect == (0, 0, 500, 400),
          answer[:50])
    check("改动也会说清楚", "改成" in answer, answer[:40])
    answer = tools.call("remove_mark", {"name": "范围4"})
    check("删得掉", "擦掉" in answer and store.get("范围4") is None, answer[:40])
    check("删不存在的会说清楚", "没有叫" in tools.call("remove_mark", {"name": "范围9"}))
    check("留空删最近一个", "擦掉" in tools.call("remove_mark", {}))
    check("按类型清：只清点", tools.call("clear_marks", {"kind": "点"}) and store.get("点1") is None)
    check("框还在", store.get("范围1") is not None)
    check("全清", "擦掉" in tools.call("clear_marks", {}) and not store.all())
    check("没标记时清会说实话", "本来就没有" in tools.call("clear_marks", {}))

    print("\n落盘：重启之后标记还在")
    store.clear()
    store.add_region(11, 22, 333, 444, note="下载按钮")
    store.add_point(555, 666, note="登录")
    saved = marks_mod.store_path()
    check("标记写进了 build/marks.json", saved.is_file(), str(saved))
    reloaded = marks_mod.MarkStore(saved)
    check("重新读一遍，框还在",
          (reloaded.get("范围1") or first).rect == (11, 22, 333, 444),
          str(reloaded.get("范围1")))
    check("备注也一起存下来了", (reloaded.get("范围1") or first).note == "下载按钮")
    check("点也还在", (reloaded.get("点1") or first).center == (555, 666))
    reloaded.remove("范围1")
    check("删掉之后落盘也更新",
          marks_mod.MarkStore(saved).get("范围1") is None)
    reloaded.clear()
    check("清空之后文件里也没了",
          not marks_mod.MarkStore(saved).all())

    print("\n显示开关：只是不画，不是删除")
    store.clear()
    store.set_visible(True)
    store.add_region(1, 2, 300, 400, name="范围1")
    store.add_point(700, 800, name="点1")
    check("默认是显示着的", store.visible)
    check("隐藏返回新状态", store.set_visible(False) is False and not store.visible)
    check("隐藏之后标记一个都没少", len(store.all()) == 2, str(store.all()))
    check("隐藏之后名字照样能用（这才是重点）",
          (store.get("范围1") or first).rect == (1, 2, 300, 400))
    check("隐藏之后照样能解析成范围（截图/看图不受影响）",
          marks_mod.resolve_region("范围1") == (1, 2, 300, 400))
    check("隐藏状态会落盘", marks_mod.MarkStore(saved).visible is False)
    shown = marks_mod.MarkStore(saved)
    shown.set_visible(True)
    check("显示状态也落盘", marks_mod.MarkStore(saved).visible is True)
    check("切一下能翻转", (store.set_visible(True), store.toggle_visible())[1] is False)
    store.set_visible(True)

    print("\n加标记会自动显示出来（不然看起来就像没成功）")
    store.set_visible(False)
    answer = tools.call("mark_point", {"x": 12, "y": 34, "name": "自动显示的点"})
    check("藏起来的状态下加一个点 → 自动显示", store.visible, answer)
    check("回答里说明了这件事", "显示" in answer and "藏" in answer, answer)
    store.set_visible(False)
    answer = tools.call("mark_region", {"x1": 1, "y1": 2, "x2": 60, "y2": 80, "name": "自动显示的范围"})
    check("范围也一样会自动显示", store.visible, answer)
    store.set_visible(False)
    answer = tools.call("mark_point", {"x": 15, "y": 16, "name": "自动显示的点"})
    check("改一个已有的标记也会显示出来", store.visible and "挪到" in answer, answer)
    store.set_visible(True)
    # 收拾干净：后面那一段假设"标记就是最开始那两个"
    store.remove("自动显示的点")
    store.remove("自动显示的范围")

    print("\n工具层：语音也能藏 / 显示")
    hidden = tools.call_result("show_marks", {"action": "隐藏"})
    check("说「隐藏」就藏起来", hidden.ok and not store.visible, hidden.text)
    check("回答里说清了「没删」和「怎么找回」",
          "还在" in hidden.text and "显示标记" in hidden.text, hidden.text)
    check("藏起来之后 list_marks 仍列得出来，并说明是隐藏的",
          "隐藏" in tools.call("list_marks") and "范围1" in tools.call("list_marks"),
          tools.call("list_marks"))
    check("留空 = 只查询", "隐藏" in tools.call("show_marks", {}), tools.call("show_marks", {}))
    check("说「显示」就画回来",
          tools.call_result("show_marks", {"action": "显示"}).ok and store.visible)
    check("切换也认", "藏起来" in tools.call("show_marks", {"action": "切换"}) and not store.visible,
          tools.call("show_marks", {}))
    check("认不出的说法不会乱动",
          "没听懂" in tools.call("show_marks", {"action": "翻个面"}) and not store.visible)
    store.set_visible(True)
    check("标记数量自始至终没变过", len(store.all()) == 2)

    print("\n改名：名字是引用它的唯一凭据")
    store.clear()
    store.add_region(0, 0, 100, 100, name="范围1")
    ok, why = store.rename("范围1", "下载区")
    check("改得动", ok and (store.get("下载区") or first).rect == (0, 0, 100, 100), why)
    check("老名字查不到了", store.get("范围1") is None)
    store.add_point(1, 2, name="另一个")
    ok, why = store.rename("下载区", "另一个")
    check("重名会被拒绝", not ok and "已经有" in why, why)
    check("空名字会被拒绝", not store.rename("下载区", "   ")[0])

    print("\n截图真的只截那一块")
    store.add_region(0, 0, 400, 300, name="左上角")
    out = tools.call("screenshot", {"region": "左上角", "name": "crop"})
    check("截图工具认范围名", "左上角" in out and "crop" in out and "400×300" in out,
          out[:70])
    import re as _re

    found = _re.search(r"([A-Za-z]:\\[^\s，。]*?\.png)", out)
    saved = Path(found.group(1)) if found and Path(found.group(1)).is_file() else None
    check("文件真的存下来了", saved is not None, str(saved))
    if saved is not None:
        from PIL import Image

        with Image.open(saved) as img:
            check("截出来的就是框里那一块（400×300）", img.size == (400, 300), str(img.size))
    bad = tools.call("screenshot", {"region": "不存在的地方"})
    check("看不懂的范围会说人话", "看不懂" in bad, bad[:40])

    print("\n看图也只看那一块")
    from voice_agent.tools import vision as vision_mod

    seen: dict = {}
    original = vision_mod._VISION_HANDLER[0]
    try:
        vision_mod.set_vision_handler(
            lambda path, question, meta=None: (seen.update(meta or {}), "看图完成")[1])
        store.clear()
        store.add_region(500, 400, 900, 700, name="范围1")
        answer = vision_mod.look_at_screen_tool("这儿写了什么", region="范围1")
        check("看图工具认范围名", answer == "看图完成", answer[:40])
        check("送出去的是裁剪后的小图", tuple(seen.get("original") or ()) == (400, 300),
              str(seen.get("original")))
        check("坐标原点跟着变成框的左上角", tuple(seen.get("origin") or ()) == (500, 400),
              str(seen.get("origin")))
        check("按屏幕截：主屏和二号屏尺寸对",
              screen.grab_screen(monitor=1).shape[1] == 1920
              and screen.grab_screen(monitor=2).shape[1] == 1920,
              str(screen.list_monitors())[:40])
        check("整屏比单屏大", screen.grab_screen().shape[1]
              > screen.grab_screen(monitor=1).shape[1])
    finally:
        vision_mod.set_vision_handler(original)

    print("\n界面层（offscreen 下也要能画、能选）")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PyQt6.QtCore import QPointF, QRect, Qt
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtWidgets import QApplication

        from voice_agent.ui.overlay import MarksOverlay

        app = QApplication.instance() or QApplication([])
        store.clear()
        store.add_region(100, 100, 500, 400, name="范围1")
        store.add_point(300, 500, name="点1")
        overlay = MarksOverlay()
        overlay.show_overlay()
        overlay._version = -1
        overlay._sync()
        check("标记层的范围覆盖所有显示器", overlay.width() > 0 and overlay.height() > 0,
              str(overlay.width()) + "x" + str(overlay.height()))
        overlay.grab()          # 真的画一遍：画错会抛异常
        check("画得出来（范围 + 点）", True)

        # 隐藏开关：只是不画，不是删。这一步用真实像素验，别只看代码
        def painted_pixels() -> int:
            overlay._sync()
            app.processEvents()
            image = overlay.grab().toImage()
            return sum(1 for x in range(0, 600, 5) for y in range(0, 600, 5)
                       if image.pixelColor(x, y).alpha() > 40)

        store.set_visible(True)
        before = painted_pixels()
        store.set_visible(False)
        after = painted_pixels()
        check("显示时屏幕上画得出东西", before > 0, str(before))
        check("隐藏之后一个像素都不画", after == 0, str(after))
        check("隐藏不会删标记", len(store.all()) == 2)
        store.set_visible(True)
        check("再显示能画回来", painted_pixels() == before, str(painted_pixels()))

        overlay.start_selection("region")
        check("进入框选模式后能接收鼠标", overlay._selecting == "region", overlay._selecting)

        def _event(kind, pos, button, buttons):  # noqa: ANN001
            return QMouseEvent(kind, pos, QPointF(overlay.mapToGlobal(pos.toPoint())),
                               button, buttons, Qt.KeyboardModifier.NoModifier)

        # 还没按下时移动鼠标：**不能**画出一个从屏幕左上角拉过来的框。
        # （_start 的初始值是 (0,0)，老代码一移动就画，看起来像"没按就定了一个角"）
        overlay.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, QPointF(300, 200),
                                      Qt.MouseButton.NoButton, Qt.MouseButton.NoButton))
        empty = overlay.grab().toImage()
        edge = any(empty.pixelColor(x, 1).alpha() > 60 for x in range(0, 400, 10))
        check("没按下鼠标时不会冒出框（旧代码从左上角拉虚线）", not edge, "上边缘没有框线")
        check("没按下时也不记第一个角", not overlay._pressed)

        # 原地点一下（不拖动）：不结束选择，而是留在框选模式里等重拖
        notices: list = []
        overlay.selection_notice.connect(notices.append)
        picks: list = []
        overlay.selection_done.connect(picks.append)
        spot = QPointF(150, 160)
        overlay.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, spot,
                                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        overlay.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, spot,
                                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        app.processEvents()
        check("原地点一下不会创建选取", not picks, str(picks))
        check("点一下之后还留在框选模式（要求重拖）",
              overlay._selecting == "region" and not overlay._pressed, overlay._selecting)
        check("会提示一句人话", bool(notices) and "太小" in notices[0],
              notices[0] if notices else "")
        overlay.selection_notice.disconnect(notices.append)
        overlay.selection_done.disconnect(picks.append)

        start = QPointF(20, 30)
        end = QPointF(220, 130)
        overlay.mousePressEvent(_event(QMouseEvent.Type.MouseButtonPress, start,
                                       Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        overlay.mouseMoveEvent(_event(QMouseEvent.Type.MouseMove, end,
                                      Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton))
        got: list = []
        overlay.selection_done.connect(got.append)
        overlay.mouseReleaseEvent(_event(QMouseEvent.Type.MouseButtonRelease, end,
                                         Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        app.processEvents()
        check("拖完会交出一个矩形", bool(got) and got[0]["kind"] == "region", str(got))
        check("拖完之后不再拦鼠标", overlay._selecting == "", overlay._selecting)
        overlay.cancel_selection()

        # 选择模式必须**整屏都能接收鼠标**：Windows 的分层窗口在完全透明的
        # 像素上会把鼠标放过去，所以这一层必须铺满（否则"只有停在已有标记上
        # 才点得动"—— 用户报的正是这个）。
        overlay.start_selection("region")
        band = overlay.grab().toImage()
        corners = [(2, 2), (overlay.width() - 3, 2), (2, overlay.height() - 3),
                   (overlay.width() - 3, overlay.height() - 3)]
        alphas = [band.pixelColor(x, y).alpha() for x, y in corners]
        check("框选模式下整屏都有底色（不然点不动）", all(a > 0 for a in alphas),
              str(alphas))

        # 拖拽中的那块填充必须是**半透明**的：以前用 QColor(rgba(...)) 构造，
        # QColor 不认那个字符串 → 无效颜色 → 画成不透明的纯色。
        # （橡皮筋只在按住时画，所以这里要模拟"正在拖"）
        overlay._pressed = True
        overlay._start = QPointF(10, 10).toPoint()
        overlay._current = QRect(10, 10, 200, 120)
        shot = overlay.grab().toImage()
        inside = shot.pixelColor(100, 60)
        check("框选填充是半透明的（不是一块实心色）",
              0 < inside.alpha() < 90, "alpha=" + str(inside.alpha()))
        check("填充用的是主题色而不是黑色", inside.red() + inside.green() + inside.blue() > 60,
              str((inside.red(), inside.green(), inside.blue())))
        check("框线还在（虚线边框）",
              any(shot.pixelColor(x, 10).alpha() > 120 for x in range(12, 200, 8)),
              "上边框")
        overlay.cancel_selection()
        overlay.deleteLater()
    except ImportError as exc:
        print("  [跳过] 没有 PyQt6：" + str(exc)[:60])

    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("屏幕标记全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
