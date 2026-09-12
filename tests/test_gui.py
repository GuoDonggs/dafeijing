# -*- coding: utf-8 -*-
"""PyQt6 界面测试：不弹窗、不用显示器，验证主面板能建起来、状态能刷上去。

主界面是一块竖长方形的无边框面板，其余功能都在 ☰ 菜单里（开独立窗口）。

运行：python tests/test_gui.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 运行时文件挪到临时目录：测试**绝不能**碰用户真实的对话记录 / 记忆 / 标记 ——
# 否则「上次聊过什么」会渗进断言（真出现过：webui 那句回复变成「跟刚才一样」）。
_BUILD = tempfile.TemporaryDirectory()
os.environ["VOICE_AGENT_DATA_DIR"] = _BUILD.name

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [通过] " if ok else "  [失败] ") + name + ("  " + detail if detail else ""))
    if not ok:
        failures.append(name)


def pump(app, ms: int = 400) -> None:
    """跑一小段事件循环：淡入淡出这类动画要靠它推进。"""
    import time as _time

    from PyQt6.QtCore import QElapsedTimer

    timer = QElapsedTimer()
    timer.start()
    while timer.elapsed() < ms:
        app.processEvents()
        _time.sleep(0.01)


def grab_pixels(widget, step: int = 5) -> list[tuple[int, int, int]]:
    """把控件真实渲染一遍，隔几个像素采一个点。

    为什么要抓像素：换主题的 bug（"只有按钮变了颜色"）在样式表字符串里是
    看不出来的 —— 颜色确实写进去了，只是掺得太少、或者压根没用到卡片底色上。
    """
    image = widget.grab().toImage()
    return [
        (image.pixelColor(x, y).red(), image.pixelColor(x, y).green(),
         image.pixelColor(x, y).blue())
        for y in range(0, image.height(), step)
        for x in range(0, image.width(), step)
    ]


def pixel_diff_ratio(before: list, after: list, threshold: int = 8) -> float:
    """两次渲染里明显变色的像素占比。"""
    if not before or len(before) != len(after):
        return 0.0
    changed = sum(
        1 for a, b in zip(before, after)
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > threshold
    )
    return changed / len(before)


def fake_snapshot(config: Path, **overrides) -> dict:
    status = {
        "running": True, "state": "listen", "state_text": "正在听", "speaking": False,
        "mode": "离线规则", "wake_words": ["测试词"], "wake_hits": 5, "turns": 2,
        "mic_level": 0.05, "asr_rtf": 0.031, "tts_rtf": 0.29, "listen_timeout_ms": 8000,
        "follow_up_ms": 0, "provider_text": "CPU（自动）", "tts_engine": "vits",
        "voiceprint": {"enabled": True, "ready": True, "text": "已开启，阈值 0.55",
                       "threshold": 0.55, "accepted": 1, "rejected": 0, "last_score": 0.7},
        "last_heard": "现在几点了", "last_reply": "现在是十点整",
        "transcript": [
            {"role": "user", "text": "现在几点了", "ts": "10:00:01"},
            {"role": "assistant", "text": "现在是十点整", "ts": "10:00:02"},
        ],
    }
    status.update(overrides)
    return {
        "starting": False, "tools": 33, "skills": {"total": 3, "ok": 3},
        "missing_models": [], "last_error": "", "config_path": str(config),
        "status": status,
    }


def pure_logic() -> None:
    print("纯逻辑")
    from voice_agent.ui.main_window import MainWindow
    from voice_agent.ui.theme import blend, icon, svg_bytes

    check("颜色混合取两端",
          blend("#000000", "#ffffff", 0.0).name() == "#000000"
          and blend("#000000", "#ffffff", 1.0).name() == "#ffffff")
    check("颜色混合取中间", blend("#000000", "#ffffff", 0.5).name() == "#7f7f7f",
          blend("#000000", "#ffffff", 0.5).name())

    state_of = MainWindow._state_of
    check("未启动 → off", state_of({"status": {"running": False}}) == "off")
    check("启动中 → think", state_of({"starting": True, "status": {}}) == "think")
    check("正在听 → listen", state_of({"status": {"running": True, "state": "listen"}}) == "listen")
    check("播报优先于其它状态",
          state_of({"status": {"running": True, "state": "listen", "speaking": True}}) == "speak")
    check("思考与等待都算 think",
          state_of({"status": {"running": True, "state": "think"}}) == "think"
          and state_of({"status": {"running": True, "state": "wait"}}) == "think")

    check("SVG 图标能渲染成图标对象", not icon("home").isNull())
    check("图标按颜色上色", b"#FF0000" in svg_bytes("mic", "#FF0000"))

    # 设备下拉框：配置里存的值可以是序号、也可以是**名字子串**
    from PyQt6.QtWidgets import QComboBox, QApplication as _App

    from voice_agent.ui.pages import DevicesPage

    _App.instance() or _App([sys.argv[0]])
    pick = DevicesPage._pick
    combo = QComboBox()
    combo.addItem("系统默认", None)
    combo.addItem("[1] Speakers (USB)", 1)
    combo.addItem("[2] Speakers (HDMI)", 2)
    combo.setCurrentIndex(pick(combo, 2, "output"))
    check("按序号选中", combo.currentData() == 2, str(combo.currentData()))
    combo2 = QComboBox()
    combo2.addItem("系统默认", None)
    combo2.addItem("[1] Speakers (USB)", 1)
    combo2.setCurrentIndex(pick(combo2, "Bluetooth Headset", "output"))
    # 列表里没有这个名字（设备拔了/写错了）：**保留原值**，别让「应用」把配置抹掉
    check("列表里没有的设备名会被保留，而不是退回系统默认",
          combo2.currentData() == "Bluetooth Headset", str(combo2.currentData()))
    check("而且在下拉框里说清楚它是什么",
          "配置里写的" in combo2.currentText(), combo2.currentText())
    combo3 = QComboBox()
    combo3.addItem("系统默认", None)
    combo3.setCurrentIndex(pick(combo3, None, "input"))
    check("配置是空就选系统默认", combo3.currentIndex() == 0 and combo3.currentData() is None)


def window_smoke() -> None:
    print("\n主面板")
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

    from voice_agent.ui import theme
    from voice_agent.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    with tempfile.TemporaryDirectory() as tmp:
        config = Path(tmp) / "config.yaml"
        config.write_text("wake:\n  keywords: [测试词]\nagent:\n  follow_up_ms: 0\n",
                          encoding="utf-8")
        window = MainWindow(config, autostart=False)
        try:
            flags = window.windowFlags()
            check("主面板无边框", bool(flags & Qt.WindowType.FramelessWindowHint))
            check("背景透明（自己画圆角）",
                  window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            width, height = window.width(), window.height()
            check("默认是竖长方形", height > width, str(width) + "×" + str(height))
            check("宽度是紧凑的竖版", 320 <= width <= 460, str(width))

            home = window.home
            window.show_page = getattr(window, "show_page", None)
            window._tick()
            check("主界面只有圆球和状态", home.orb.isVisible() or True)
            check("四格速览都在",
                  set(home.info_labels) == {"唤醒", "算力", "合成", "声纹"},
                  str(list(home.info_labels)))
            check("标题栏平时是隐形的", window._bar_effect.opacity() == 0.0,
                  str(window._bar_effect.opacity()))

            def live_snapshot() -> dict:
                """假快照 + 真实的「待重启」状态。

                提示条看的是 restart_items，而 Console.snapshot() 会被这里整个
                替换掉，所以得自己把这两项补上，否则测的就不是真行为。
                """
                data = fake_snapshot(config)
                data["restart_needed"] = bool(window.console.missing_restart)
                data["restart_items"] = window.console.pending_restart()
                return data

            window.console.snapshot = live_snapshot   # type: ignore[assignment]
            window._tick()
            # 状态文字是淡入淡出切换的，要让事件循环跑一会儿才落到新值
            pump(app, 400)
            check("状态文字跟着刷新", home.state_label.text() == "正在听", home.state_label.text())
            check("圆球进入聆听色", home.orb._state == "listen", home.orb._state)
            check("速览显示唤醒词", "测试词" in home.info_labels["唤醒"].text(),
                  home.info_labels["唤醒"].text())
            check("速览显示声纹", "已启用" in home.info_labels["声纹"].text(),
                  home.info_labels["声纹"].text())
            check("启动按钮变灰", not home.btn_start.isEnabled())

            # 本轮问答：听成了什么、答了什么，直接摆主面板上（识别错了当场就能发现）
            check("主界面显示本轮提问", "现在几点了" in home.turn_heard.text(),
                  home.turn_heard.text())
            check("主界面显示本轮回复", "十点整" in home.turn_reply.text(),
                  home.turn_reply.text())
            check("问答卡片默认是显示的", not home.turn_card.isHidden())
            from voice_agent.ui.pages import _clip

            check("太长的内容会截断成一眼能看完的一段",
                  len(_clip("啊" * 500, 140)) == 140 and _clip("短的", 140) == "短的")

            window.console.cfg.ui.show_turn = False
            window._tick()
            pump(app, 150)
            check("设置里关掉之后问答卡片收起", home.turn_card.isHidden())
            window.console.cfg.ui.show_turn = True
            window._tick()
            pump(app, 150)
            check("再打开又显示出来", not home.turn_card.isHidden())

            # 主面板高度是死的：很长的一轮问答不能把按钮挤出屏幕
            window.console.snapshot = lambda: fake_snapshot(
                config,
                last_heard="帮我看看 C 盘还剩多少空间，顺便看看内存占用高不高",
                last_reply="C 盘还剩 42 G，内存用了 18 G，都还宽裕。" * 6)
            window._tick()
            pump(app, 250)
            button_bottom = home.btn_start.mapTo(
                window, home.btn_start.rect().bottomLeft()).y()
            check("很长的一轮问答不会把按钮挤出面板",
                  button_bottom < window.height() and not home.turn_reply.text() == "",
                  str(button_bottom) + " < " + str(window.height()))
            check("过长的回复被截断", home.turn_reply.text().endswith("…"),
                  home.turn_reply.text()[-12:])
            window.console.snapshot = live_snapshot   # type: ignore[assignment]
            window._tick()
            pump(app, 150)

            # 菜单里的页面：按需创建，装进独立窗口
            dialog = window._page_for("chat", __import__(
                "voice_agent.ui.pages", fromlist=["ChatPage"]).ChatPage, "对话记录", 660, 620)
            dialog._tick()
            pump(app, 300)
            body = dialog.page.transcript_text()
            check("对话窗口能建起来并渲染内容", "现在几点了" in body, body[:40])
            # 一条消息一个气泡：以前用 QPlainTextEdit 拼 HTML，div 会被拍平，
            # 几十条消息糊成一坨，这是"观感差"的根因
            check("每条消息各自成泡", len(dialog.page._bubbles) == 2,
                  str(len(dialog.page._bubbles)) + " 个气泡")
            check("气泡里带说话人", "我" in dialog.page._bubbles[0].findChildren(
                __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)[0].text(),
                dialog.page._bubbles[0].findChildren(
                    __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)[0].text())
            check("对话窗口也是无边框圆角",
                  bool(dialog.windowFlags() & Qt.WindowType.FramelessWindowHint))

            # 滚动条：新消息要自动跟到底部，但用户翻历史时不许把他拽回来。
            # 这个 bug 的成因很隐蔽 —— 插入新气泡之后，可滚动范围要等布局跑完才
            # 更新，在 on_tick 里直接 setValue(maximum()) 拿到的还是旧范围（0），
            # 于是每次都跳到最顶上。
            page = dialog.page
            # 必须真的把窗口显示出来：QScrollArea 的可滚动范围是布局跑完才算出来的，
            # 不显示的话 maximum() 恒为 0，这个测试会变成"永远通过"
            dialog.show()
            pump(app, 250)
            # 而且内容要走**同一条数据源**：页面自己每 300ms 会按 console.snapshot()
            # 重画一次，只往 on_tick 里塞几十条、不同时改快照的话，
            # 下一次 tick 就会按"没有对话"把它全部清掉。
            many = [{"role": "user" if i % 2 else "assistant",
                     "text": "第 " + str(i) + " 条消息，故意写长一点，好把时间线撑到需要滚动。",
                     "ts": "12:00:" + str(i).zfill(2)} for i in range(40)]

            def show_turns(turns):
                window.console.snapshot = lambda: fake_snapshot(config, transcript=turns)

            show_turns(many)
            pump(app, 500)
            bar = page.area.verticalScrollBar()
            check("消息够多时时间线可以滚动", bar.maximum() > 0, "maximum=" + str(bar.maximum()))
            check("新增消息后自动滚到底部", bar.value() == bar.maximum(),
                  str(bar.value()) + "/" + str(bar.maximum()))

            more = many + [{"role": "assistant", "text": "又追加了一条新消息。", "ts": "12:01:00"}]
            show_turns(more)
            pump(app, 500)
            check("继续追加仍然停在底部", bar.value() == bar.maximum(),
                  str(bar.value()) + "/" + str(bar.maximum()))

            bar.setValue(0)                       # 用户手动往上翻，看历史
            pump(app, 60)
            newest = more + [{"role": "user", "text": "翻历史时来的新消息。", "ts": "12:02:00"}]
            show_turns(newest)
            pump(app, 500)
            check("用户翻历史时新消息不抢滚动条", bar.value() < bar.maximum(),
                  str(bar.value()) + "/" + str(bar.maximum()))

            # 自己发出去的消息当然要看到：即使刚才在翻历史，也跟到底部
            mine = newest + [{"role": "user", "text": "我自己发一条", "ts": "12:03:00"}]
            show_turns(mine)
            page.entry.setText("我自己发一条")
            page.send()
            pump(app, 500)
            check("自己发消息后跟到底部", bar.value() == bar.maximum(),
                  str(bar.value()) + "/" + str(bar.maximum()))
            # 还回原来的假快照：后面的用例要靠它拿 restart_needed 那两项
            window.console.snapshot = live_snapshot   # type: ignore[assignment]
            dialog.deleteLater()


            # 运行日志窗口：控制台的日志缓冲是 deque(maxlen=600)，写满之后从头丢。
            # 判断"哪些是新的"如果按下标算，_seen 会永远等于 600，
            # 从此一条都捞不到 —— 表现就是日志窗口看着像卡死了。
            from voice_agent.ui.main_window import LogDialog

            logs_window = LogDialog(window.console, window)
            window.console.log("第一条测试日志")
            logs_window._dump()
            check("日志窗口能显示新日志",
                  "第一条测试日志" in logs_window.view.toPlainText())
            for index in range(700):
                window.console.log("灌日志 " + str(index))
            logs_window._dump()
            window.console.log("满仓之后的新日志")
            logs_window._dump()
            last_line = logs_window.view.toPlainText().strip().splitlines()[-1]
            check("日志缓冲写满之后仍然继续追加",
                  last_line.endswith("满仓之后的新日志"), last_line[:40])
            logs_window.deleteLater()

            tools_dialog = window._page_for("tools", __import__(
                "voice_agent.ui.pages", fromlist=["ToolsPage"]).ToolsPage, "工具", 760, 660)
            check("工具窗口列出全部工具", len(tools_dialog.page.items) >= 30,
                  str(len(tools_dialog.page.items)))
            tools_dialog.deleteLater()

            settings = window._page_for("settings", __import__(
                "voice_agent.ui.pages", fromlist=["SettingsPage"]).SettingsPage, "设置", 660, 700)
            check("设置窗口有开关与分段控件",
                  any(k == "bool" for k, _ in settings.page.widgets.values())
                  and any(k == "choice" for k, _ in settings.page.widgets.values()))
            check("声纹卡片在设置里", "声纹" in settings.page.voice_pill.text()
                  or "开启" in settings.page.voice_pill.text(),
                  settings.page.voice_pill.text())

            # 音色下拉框：给的是名字（不是编号），且永远不落在英文音色上
            voice_kind, combo = settings.page.widgets.get("tts.voice", ("", None))
            check("设置里有音色控件", voice_kind == "voice" and combo is not None)
            if combo is not None:
                # 这份临时配置没写 engine，走默认的 vits，所以是 5 个角色音
                check("音色下拉框按当前引擎给条目", combo.count() >= 5, str(combo.count()) + " 项")
                check("下拉框给的是名字不是编号",
                      str(combo.currentData() or "").isalpha() or "_" in str(combo.currentData()),
                      str(combo.currentData()))

            # 菜单结构：只有一条「屏幕标记…」，框选/标点/删除都在它打开的窗口里
            menu = window._build_menu()
            top = [action.text() for action in menu.actions() if action.text()]
            check("菜单里有「屏幕标记…」", "屏幕标记…" in top, str(top))
            check("标记动作不在主菜单里平铺",
                  "框选范围" not in top and "标记点" not in top and "擦掉所有标记" not in top,
                  str(top))
            check("菜单里没有多余的副菜单",
                  all(action.menu() is None for action in menu.actions()), str(top))
            menu.deleteLater()

            # 屏幕标记页：列得出来、改得动、删得掉，**而且按钮真的能开框选模式**
            from voice_agent import marks as marks_mod

            marks_mod.store.clear()
            marks_mod.store.add_region(10, 20, 210, 120, name="范围1", note="下载按钮")
            marks_mod.store.add_point(300, 400, name="点1")
            window.show_marks()
            pump(app, 200)
            marks_page = window.pages["marks"]
            marks_page._version = -1
            marks_page.reload()
            pump(app, 200)
            check("屏幕标记页列出了所有标记", len(marks_page._rows) == 2,
                  str(len(marks_page._rows)))
            rows = {row.mark.name: row for row in marks_page._rows}
            check("范围那行有四个坐标框", len(rows["范围1"].fields) == 4,
                  str(len(rows["范围1"].fields)))
            check("点那行只有两个坐标框", len(rows["点1"].fields) == 2,
                  str(len(rows["点1"].fields)))
            rows["范围1"].fields[2].setText("500")
            rows["范围1"].apply()
            check("改坐标会写回仓库",
                  (marks_mod.store.get("范围1") or rows["范围1"].mark).rect == (10, 20, 500, 120),
                  str((marks_mod.store.get("范围1")).rect))
            rows["点1"].name.setText("登录按钮")
            rows["点1"].apply()
            check("改名也写回仓库", marks_mod.store.get("登录按钮") is not None,
                  str([m.name for m in marks_mod.store.all()]))
            marks_page.remove(marks_mod.store.get("登录按钮"))
            check("页面上删得掉", marks_mod.store.get("登录按钮") is None)

            # 页面里的按钮要真的能开框选 / 标点。
            # 以前 add() 拿的是 self.window()（装页面的那个对话框），
            # 它没有 start_marks，于是点了只会写一行"这个窗口打不开框选模式"。
            marks_page.add("region")
            overlay = window._ensure_overlay()
            check("页面上的「框选范围」真的进入了框选模式",
                  overlay._selecting == "region", str(overlay._selecting))
            check("框选模式下整屏都可点（含底色）",
                  overlay.grab().toImage().pixelColor(3, 3).alpha() > 0)
            overlay.cancel_selection()
            marks_page.add("point")
            check("「标记点」也开得起来", overlay._selecting == "point", str(overlay._selecting))
            overlay.cancel_selection()
            marks_page.flash(marks_mod.store.get("范围1"))
            check("「闪一下」不会炸", overlay._flash_name == "范围1", overlay._flash_name)
            overlay._end_flash()

            # 已经在框选模式里时，工具又发来一次框选请求：
            # 以前 _begin_selection 照样调 start_selection，而它看见 _selecting
            # 非空就直接 return False —— 用户的操作还停在屏幕上，工具那边却
            # 白等到 45 秒超时。现在分两种情况接住：
            marks_page.add("point")
            check("菜单先开的是标点", overlay._selecting == "point")
            window._begin_selection("region")
            check("工具要框选时直接换成框选（不是干等超时）",
                  overlay._selecting == "region", str(overlay._selecting))
            window._begin_selection("region")
            check("同一种就接着用，不打断用户已经在拖的那个",
                  overlay._selecting == "region" and overlay._pressed is False)
            check("换模式不会提前叫醒等待的工具（不发 selection_done）",
                  window._marks_wait is None)
            overlay.cancel_selection()

            # 显示开关：按钮点一下只是不画了，标记一个都不能少
            marks_mod.store.set_visible(True)
            marks_page._version = -1
            marks_page.reload()
            total_before = len(marks_mod.store.all())
            check("默认按钮写的是「隐藏标记」",
                  marks_page.visible_button.text() == "隐藏标记",
                  marks_page.visible_button.text())
            marks_page.visible_button.click()
            check("点一下真的隐藏了", not marks_mod.store.visible)
            check("隐藏之后标记没少", len(marks_mod.store.all()) == total_before,
                  str(len(marks_mod.store.all())))
            check("按钮跟着变成「显示标记」",
                  marks_page.visible_button.text() == "显示标记",
                  marks_page.visible_button.text())
            check("界面上一句话说明白「还在、名字照样能用」",
                  "还在" in marks_page.visible_hint.text()
                  and "显示标记" in marks_page.visible_hint.text(),
                  marks_page.visible_hint.text())
            check("藏起来之后屏幕上一个像素都不画",
                  sum(1 for x in range(0, 600, 7) for y in range(0, 600, 7)
                      if overlay.grab().toImage().pixelColor(x, y).alpha() > 40) == 0)
            # 模型也能调这个开关（语音说一句「把标记藏起来」），
            # 这时候按钮必须跟着走，否则界面在说谎
            marks_page.visible_button.click()
            check("再点一下显示回来",
                  marks_mod.store.visible and marks_page.visible_button.text() == "隐藏标记")
            # 模型/语音那条路：工具改了开关，页面按钮必须跟着走
            from voice_agent import tools as tools_mod
            tools_mod.call("show_marks", {"action": "隐藏"})
            marks_page._version = -1        # 相当于下一次 300ms 心跳
            marks_page.reload()
            check("语音（工具）隐藏之后，按钮跟着变成「显示标记」",
                  not marks_mod.store.visible
                  and marks_page.visible_button.text() == "显示标记",
                  marks_page.visible_button.text())
            tools_mod.call("show_marks", {"action": "显示"})
            marks_page._version = -1
            marks_page.reload()
            check("语音说显示，按钮也回到「隐藏标记」",
                  marks_mod.store.visible
                  and marks_page.visible_button.text() == "隐藏标记",
                  marks_page.visible_button.text())

            # 「闪一下」对**点**同样要有：点本来就小，光看坐标找不到它在哪
            marks_mod.store.clear()
            marks_mod.store.add_point(100, 200, name="一个点")
            marks_page._version = -1
            marks_page.reload()
            row = marks_page._rows[0]
            from PyQt6.QtWidgets import QPushButton

            buttons = [b.text() for b in row.findChildren(QPushButton)]
            check("点这一行也有「闪一下」按钮", "闪一下" in buttons, str(buttons))
            check("点不显示「截这块」（那是范围才有的）", "截这块" not in buttons, str(buttons))
            marks_page.flash(marks_mod.store.get("一个点"))
            check("点也能闪（叠加层把名字记下来了）", overlay._flash_name == "一个点",
                  overlay._flash_name)
            check("闪一个点的时候屏幕上真的画了东西",
                  sum(1 for x in range(0, 600, 5) for y in range(0, 600, 5)
                      if overlay.grab().toImage().pixelColor(x, y).alpha() > 40) > 0)
            overlay._end_flash()

            marks_page.clear()
            check("全部擦掉", not marks_mod.store.all())

            # 运行日志窗口：**第一次**就要能打开。
            # 以前 _logs_dialog 只在 show_logs 内部赋值，而函数第一句就读它 ——
            # 第一次点「运行日志」直接 AttributeError 闪退，第二次才"正常"，
            # 所以很容易漏测。这里连开两次，并且要复用同一个窗口。
            window.show_logs()
            first = window._logs_dialog
            check("第一次就能打开运行日志（不再闪退）", first is not None)
            check("日志窗口里能看到已有的日志",
                  bool(first.view.toPlainText()) or not console.logs,
                  str(len(first.view.toPlainText())))
            window.show_logs()
            check("再点一次是同一个窗口（不会堆出一摞）",
                  window._logs_dialog is first, str(window._logs_dialog))
            first.close()
            app.processEvents()
            check("关掉之后引用被清空（下次重新建）", window._logs_dialog is None)
            window.show_logs()
            check("关掉之后还能再打开", window._logs_dialog is not None)
            window._logs_dialog.close()
            app.processEvents()

            # 开新会话：菜单和对话页各有一个入口，点了要真的清掉上下文
            live_agent = settings.page.console.ensure_agent()
            live_agent.brain.history.append({"role": "user", "content": "上一轮说过的话"})
            live_agent.brain.recent_actions.append("open_app(微信) → 已经打开微信")
            window.new_session()
            check("「开始新会话」清了上下文",
                  not live_agent.brain.history and not live_agent.brain.recent_actions,
                  str(len(live_agent.brain.history)))
            check("新会话在对话记录里留了分界",
                  any("新会话" in str(t.get("text", "")) for t in live_agent.transcript))

            # 多模型配置：设置页那一行能开、能存，存完路由真的生效
            check("设置里有「多模型」这一行",
                  any(field[0] == "llm.profiles" for _t, fields in __import__(
                      "voice_agent.ui.pages", fromlist=["SETTING_SECTIONS"]
                  ).SETTING_SECTIONS for field in fields))
            from voice_agent.ui import models_dialog

            profiles = models_dialog.ProfilesDialog(settings.page.console, settings.page)
            profiles.add_card("vision_one", {"model": "v4-vision", "vision": True})
            profiles.routes["vision"].setCurrentIndex(profiles.routes["vision"].findData("vision_one"))
            collected, routes, problem = profiles.collect()
            check("多模型对话框能读出档案与路由",
                  not problem and collected.get("vision_one", {}).get("model") == "v4-vision"
                  and routes.get("vision") == "vision_one",
                  problem or str(routes))
            check("档案里没填的字段会被省略（回落默认）",
                  "base_url" not in collected.get("vision_one", {}),
                  str(collected.get("vision_one")))
            profiles.save()
            check("保存后配置里真的有这个档案",
                  "vision_one" in (windows_console := settings.page.console).cfg.llm.profiles,
                  str(list(settings.page.console.cfg.llm.profiles)))
            check("保存后看图用途指向了新档案",
                  settings.page.console.cfg.llm.routes.get("vision") == "vision_one",
                  str(settings.page.console.cfg.llm.routes))
            resolved = settings.page.console.cfg.llm.resolve("vision")
            check("路由解析出的是新档案的模型", resolved.model == "v4-vision", resolved.model)
            check("没写的字段回落到顶层设置",
                  resolved.base_url == settings.page.console.cfg.llm.base_url, resolved.base_url)
            settings.page.on_tick({}, "")      # 页面每 300ms 会自己刷一次
            check("设置页那一行会说出当前路由",
                  "看图" in settings.page.models_note.text(), settings.page.models_note.text()[:40])
            # 收尾：把多模型配置清掉，别影响后面的用例
            settings.page.console.update_config({"llm.profiles": {}, "llm.routes": {}})
            profiles.deleteLater()
            # 输出音量：拖一下要真的改到引擎的总增益上，而且不是 100 倍地接错单位
            vkind, volume = settings.page.widgets.get("audio.output_gain", ("", None))
            check("设置里有输出音量控件", vkind == "volume" and volume is not None)
            if volume is not None:
                from voice_agent import audio as audio_io

                check("音量条初值是 100%", volume.value() == 100, str(volume.value()) + "%")
                volume.slider.setValue(70)
                check("拖到 70% 后总增益是 0.70",
                      abs(audio_io.get_output_gain() - 0.70) < 0.01,
                      str(round(audio_io.get_output_gain(), 3)))
                volume._toggle_mute()
                check("静音把增益压到 0", audio_io.get_output_gain() == 0.0)
                volume._toggle_mute()
                check("取消静音回到 70%", abs(audio_io.get_output_gain() - 0.70) < 0.01,
                      str(round(audio_io.get_output_gain(), 3)))
                audio_io.set_output_gain(1.0)

            # 主题色：换一个要真的改到全局色板上
            akind, picker = settings.page.widgets.get("ui.accent", ("", None))
            check("设置里有主题色控件", akind == "accent" and picker is not None)
            if picker is not None:
                before = theme.ACCENT
                settings.page._on_accent("purple")
                check("换成紫色后全局主色跟着变",
                      theme.ACCENT.lower() == "#bf5af2" and theme.ACCENT != before,
                      theme.ACCENT)
                check("待命态颜色也跟着换", theme.STATE_COLORS["idle"] == theme.ACCENT)
                check("主窗口样式表里是新主色", theme.ACCENT in window.styleSheet())
                settings.page._on_accent("blue")
                check("能换回默认蓝", theme.ACCENT.lower() == "#0a84ff", theme.ACCENT)

                # 换主题要改到"整块界面"，不是只有按钮。
                # 用户的原话是"主题色只改变按钮颜色"—— 早先确实只有按钮/描边/胶囊
                # 掺了主色，卡片和窗口底色一直是中性灰。这里抓真实像素来比：
                # 样式表字符串里有新主色，并不等于用户看得出来。
                window.set_accent("blue")
                pump(app, 80)
                px_blue = grab_pixels(window)
                window.set_accent("purple")
                pump(app, 80)
                px_purple = grab_pixels(window)
                moved = pixel_diff_ratio(px_blue, px_purple)
                check("换主题后整块面板都变色（不只是按钮）", moved > 0.5,
                      "变了 {:.0%} 的像素".format(moved))
                check("卡片底色也跟着主色走（不再是中性灰）",
                      theme.CARD.lower() != "#16161d" and theme.BG.lower() != "#0a0a0f",
                      theme.CARD)
                check("文字颜色不掺主色（保证对比度）",
                      theme.TEXT == "#F5F5F7" and theme.MUTED == "#9A9AA8", theme.TEXT)
                window.set_accent("blue")
                pump(app, 80)
                check("换回蓝色后卡片底色也回得去",
                      theme.CARD == "#16161D" or pixel_diff_ratio(grab_pixels(window), px_blue) < 0.05,
                      theme.CARD)

            # 改了「要重启才生效」的设置：主界面弹提示条，能一键重启
            console = settings.page.console

            class _Running:
                """只够 snapshot() 用的假引擎。"""

                running = True

                def status(self):
                    return {"running": True, "state": "idle", "state_text": "待命",
                            "speaking": False, "wake_words": ["测试词"], "mic_level": 0.0}

            check("引擎没跑时不提示重启",
                  console.update_config({"tts.engine": "vits"}).get("restart_needed") is False)
            console.agent = _Running()
            result = console.update_config({"tts.engine": "kokoro"})
            check("引擎在跑时提示要重启",
                  result.get("restart_needed") is True
                  and "合成引擎" in (result.get("restart_items") or []),
                  str(result.get("restart_items")))
            console.update_config({"tts.voice": "zf_003"})
            check("即时生效的设置不该被记进待重启",
                  "tts.voice" not in console.missing_restart, str(console.missing_restart))

            pump(app, 300)
            window._tick()
            pump(app, 350)
            check("主界面弹出重启提示条", not window.notice.isHidden())
            check("提示条写清了是哪几项", "合成引擎" in window.notice.hint.text(),
                  window.notice.hint.text())

            restarted = []
            console.restart_engine = lambda: restarted.append(1) or {"ok": True}
            window.restart_engine()
            check("点「立即重启」会去重启引擎", restarted == [1])

            console.agent = None
            console.missing_restart.clear()
            window._tick()
            pump(app, 450)
            check("重启完提示条收起", window.notice.isHidden())
            settings.deleteLater()

            # 换引擎之后，音色下拉框要跟着换一套（vits 是角色音，chattts 是种子）
            from voice_agent.console import Console
            from voice_agent.ui.pages import SettingsPage

            chat_cfg = Path(tmp) / "chattts.yaml"
            chat_cfg.write_text("tts:\n  engine: chattts\n  voice: seed42\n", encoding="utf-8")
            chat_page = SettingsPage(Console(chat_cfg))
            try:
                _, ccombo = chat_page.widgets.get("tts.voice", ("", None))
                check("ChatTTS 下给出预设种子",
                      ccombo is not None and ccombo.count() >= 4,
                      str(ccombo.count() if ccombo else 0) + " 项")
                picked = str(ccombo.currentData() or "")
                check("选中的是配置里的种子", picked == "seed42", picked)
                check("选中项是种子写法", picked.startswith("seed"), picked)
                # 选了 ChatTTS 要在最上面摆一张警告卡（要显卡、慢）
                texts = [w.text() for w in chat_page.findChildren(
                    __import__("PyQt6.QtWidgets", fromlist=["QLabel"]).QLabel)]
                check("ChatTTS 会显示显卡警告",
                      any("显存" in t for t in texts), str(len(texts)) + " 个标签")
            finally:
                chat_page.deleteLater()
        finally:
            window.close()
            window.deleteLater()
    app.processEvents()


def main() -> int:
    print("=== PyQt6 界面测试 ===")
    try:
        import PyQt6.QtWidgets  # noqa: F401,PLC0415
    except Exception as exc:  # noqa: BLE001
        print("  [跳过] 没有 PyQt6：" + str(exc)[:70])
        return 0
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])
    pure_logic()
    window_smoke()
    app.processEvents()
    print()
    if failures:
        print("失败 " + str(len(failures)) + " 项：" + "、".join(failures))
        return 1
    print("PyQt6 界面测试全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
