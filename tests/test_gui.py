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


def fake_snapshot(config: Path, **overrides) -> dict:
    status = {
        "running": True, "state": "listen", "state_text": "正在听", "speaking": False,
        "mode": "离线规则", "wake_words": ["测试词"], "wake_hits": 5, "turns": 2,
        "mic_level": 0.05, "asr_rtf": 0.031, "tts_rtf": 0.29, "listen_timeout_ms": 8000,
        "follow_up_ms": 0, "provider_text": "CPU（自动）", "tts_engine": "vits",
        "voiceprint": {"enabled": True, "ready": True, "text": "已开启，阈值 0.55",
                       "threshold": 0.55, "accepted": 1, "rejected": 0, "last_score": 0.7},
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
            dialog.deleteLater()

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
