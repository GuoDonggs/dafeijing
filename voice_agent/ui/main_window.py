# -*- coding: utf-8 -*-
"""主窗口：一块竖着的、没有系统标题栏的圆角面板。

设计意图很直接 —— **打开就只看到它现在在干什么**。

    ╭──────────────────────────╮
    │ ☰                     — ✕ │   ← 平时隐形，鼠标移上来才浮出
    │                          │
    │           ◉              │
    │         正在听            │
    │   说指令就好，8 秒内没有    │
    │   提问我会回到待命          │
    │                          │
    │   [启动监听] [停止] [打断]  │
    │                          │
    │  唤醒 大肥鲸   算力 CPU     │
    │  合成 vits    声纹 关闭     │
    ╰──────────────────────────╯

对话、工具、技能、设置、设备、日志、关于 —— 全部收进左上角的 ☰ 菜单里，
点开是一个独立的窗口，关掉就回到这块面板。

窗口无边框，所以要自己实现：拖动（按住任意空白处拖）、圆角、描边、
最小化 / 关闭。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QSettings, Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..console import Console
from . import components as ui
from . import pages as pages_mod
from . import theme

APP_NAME = "VoiceAgent"
PANEL_WIDTH = 384
PANEL_MAX_HEIGHT = 700


def app_icon(color: str = theme.BLUE) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(theme.blend(color, "#FFFFFF", 0.15))
    painter.drawEllipse(6, 6, 52, 52)
    painter.setBrush(theme.blend(color, "#000000", 0.28))
    painter.drawEllipse(20, 20, 24, 24)
    painter.end()
    return QIcon(pixmap)


class FramelessDialog(QDialog):
    """无边框的圆角窗口：页面窗口和日志窗口都用它当外壳。

    自己实现拖动和关闭按钮，外观才和主面板一致。
    """

    def __init__(self, parent: QWidget | None, title: str, width: int, height: int) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(False)
        self.resize(width, height)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.panel = ui.RoundedPanel(self, radius=18, color=theme.BG, border=theme.SEPARATOR)
        outer.addWidget(self.panel)

        inner = QVBoxLayout(self.panel)
        inner.setContentsMargins(18, 12, 18, 18)
        inner.setSpacing(10)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("PageTitle")
        bar.addWidget(label)
        bar.addStretch(1)
        close = ui.WindowButton("plus", "关闭", danger=True)
        close.clicked.connect(self.close)
        close.setIcon(QIcon())          # 用旋转的加号当 ✕，省一个图标
        close.paintEvent = _paint_close(close)   # type: ignore[assignment]
        bar.addWidget(close)
        inner.addLayout(bar)
        self.body = inner
        self._drag: QPoint | None = None

    # 无边框窗口要自己实现拖动
    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        self._drag = None
        super().mouseReleaseEvent(event)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802
        super().showEvent(event)
        ui.fade_in(self, 170)


class PageDialog(FramelessDialog):
    """把一个页面装进独立窗口。菜单里点任意一项都是开它。"""

    def __init__(self, page: pages_mod.Page, title: str, parent: QWidget | None,
                 width: int = 720, height: int = 640) -> None:
        super().__init__(parent, title, width, height)
        self.page = page
        self.body.addWidget(page, 1)
        page.on_show()
        if parent is not None:
            center = parent.frameGeometry().center()
            self.move(center.x() - width // 2, max(20, center.y() - height // 2))
        # 页面要跟着主循环刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(300)

    def _tick(self) -> None:
        if self.parent() is None:
            return
        window = self.parent()
        if isinstance(window, MainWindow):
            self.page.on_tick(window.latest, window.current_state)


def _paint_close(button: QWidget):
    """给关闭按钮换个画法：两条交叉的线。"""

    def paint(event) -> None:  # noqa: ANN001
        painter = QPainter(button)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if button._hover:  # type: ignore[attr-defined]
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 69, 58, 210))
            painter.drawEllipse(0, 0, button.width(), button.height())
        pen = QColor("#FFFFFF" if button._hover else theme.MUTED)  # type: ignore[attr-defined]
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        offset = 8
        painter.drawLine(offset, offset, button.width() - offset, button.height() - offset)
        painter.drawLine(button.width() - offset, offset, offset, button.height() - offset)
        painter.end()

    return paint


class MainWindow(QWidget):
    """竖长方形的主面板。"""

    def __init__(self, config_path: Path | None = None, autostart: bool = True) -> None:
        super().__init__()
        self.console = Console(config_path)
        self.settings = QSettings(APP_NAME, "Console")
        self._last_state = ""
        self.latest: dict = {}
        self.current_state = "off"
        self._drag: QPoint | None = None
        self._bar_visible = False

        # 主题色来自配置：必须在建界面之前设好，图标是按颜色渲染的
        theme.set_accent(self.console.cfg.ui.accent_hex())
        self.setWindowTitle("语音 Agent")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(theme.qss())
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._size_to_screen()
        self._restore_position()

        self._build()
        self._bind_shortcuts()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(280)
        if autostart:
            QTimer.singleShot(600, self.start_engine)

    # ───────────────── 构建 ─────────────────

    def _size_to_screen(self) -> None:
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        height = PANEL_MAX_HEIGHT
        if available is not None:
            height = min(PANEL_MAX_HEIGHT, int(available.height() * 0.86))
        self.setFixedSize(PANEL_WIDTH, max(520, height))

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)     # 留白给圆角与描边
        self.panel = ui.RoundedPanel(self, radius=20, color=theme.BG, border=theme.SEPARATOR)
        outer.addWidget(self.panel)

        inner = QVBoxLayout(self.panel)
        inner.setContentsMargins(16, 10, 16, 18)
        inner.setSpacing(0)

        # ① 顶部工具条：平时透明，鼠标移到窗口上才浮现
        self.titlebar = QWidget(self.panel)
        self.titlebar.setFixedHeight(30)
        bar = QHBoxLayout(self.titlebar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        self.btn_menu = ui.WindowButton("activity", "菜单")
        self.btn_menu.clicked.connect(self.open_menu)
        bar.addWidget(self.btn_menu)
        bar.addStretch(1)
        self.btn_min = ui.WindowButton("chevrondown", "最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        self.btn_close = ui.WindowButton("plus", "退出", danger=True)
        self.btn_close.paintEvent = _paint_close(self.btn_close)  # type: ignore[assignment]
        self.btn_close.clicked.connect(self.close)
        bar.addWidget(self.btn_min)
        bar.addWidget(self.btn_close)
        inner.addWidget(self.titlebar)

        self._bar_effect = QGraphicsOpacityEffect(self.titlebar)
        self._bar_effect.setOpacity(0.0)
        self.titlebar.setGraphicsEffect(self._bar_effect)
        self._bar_anim = QPropertyAnimation(self._bar_effect, b"opacity", self)
        self._bar_anim.setDuration(220)
        self._bar_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # ② 需要重启才生效的设置：一条提示条，平时收起来不占位置
        self.notice = ui.NoticeBar(self.panel)
        self.notice.action.connect(self.restart_engine)
        inner.addWidget(self.notice)

        # ③ 中间：圆球 + 状态
        self.home = pages_mod.HomePage(self.console, self.panel)
        inner.addWidget(self.home, 1)
        self.pages = {"home": self.home}
        self.home.start_requested.connect(self.start_engine)    # type: ignore[attr-defined]
        self.home.stop_requested.connect(self.stop_engine)      # type: ignore[attr-defined]
        self.home.cancel_requested.connect(self.cancel_task)    # type: ignore[attr-defined]

    def _page_for(self, key: str, factory, title: str, width: int = 720,
                  height: int = 640) -> PageDialog:
        """按需创建页面 —— 没打开过的页面不占内存，启动也更快。"""
        if key not in self.pages:
            page = factory(self.console)
            # 设置页里换了主题色，主窗口得跟着变色（图标是渲染后缓存的，
            # 只重设样式表不够）
            signal = getattr(page, "accent_changed", None)
            if signal is not None:
                signal.connect(self.set_accent)
            self.pages[key] = page
        return PageDialog(self.pages[key], title, self, width, height)

    def _bind_shortcuts(self) -> None:
        pairs = [
            ("Ctrl+1", self.start_engine),
            ("Ctrl+2", self.stop_engine),
            ("Esc", self.cancel_task),
            ("Ctrl+R", lambda: self.show_chat()),
            ("Ctrl+K", lambda: self.show_chat()),
            ("Ctrl+T", lambda: self.show_tools()),
            ("Ctrl+,", lambda: self.show_settings()),
            ("Ctrl+L", self.show_logs),
            ("Ctrl+Q", self.close),
        ]
        for sequence, slot in pairs:
            QShortcut(QKeySequence(sequence), self, activated=slot)

    # ───────────────── 菜单 ─────────────────

    def open_menu(self) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(theme.qss())
        entries = [
            ("chat", "对话记录", "Ctrl+R", self.show_chat),
            ("edit", "文字指令", "Ctrl+K", self.show_command),
            (None, None, None, None),
            ("tools", "工具", "Ctrl+T", self.show_tools),
            ("skills", "技能", "", self.show_skills),
            (None, None, None, None),
            ("settings", "设置", "Ctrl+,", self.show_settings),
            ("mic", "设备", "", self.show_devices),
            (None, None, None, None),
            ("activity", "运行日志", "Ctrl+L", self.show_logs),
            ("info", "关于", "", self.show_about),
            (None, None, None, None),
            ("power", "退出", "Ctrl+Q", self.close),
        ]
        for icon_name, label, shortcut, slot in entries:
            if label is None:
                menu.addSeparator()
                continue
            action = menu.addAction(theme.icon(icon_name, theme.TEXT, 16), label)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
        menu.exec(self.btn_menu.mapToGlobal(QPoint(0, self.btn_menu.height() + 6)))

    # ───────────────── 各页面 ─────────────────

    def show_chat(self) -> None:
        self._page_for("chat", pages_mod.ChatPage, "对话记录", 660, 620).show()

    def show_command(self) -> None:
        self.show_chat()

    def show_tools(self) -> None:
        self._page_for("tools", pages_mod.ToolsPage, "工具", 760, 660).show()

    def show_skills(self) -> None:
        self._page_for("skills", pages_mod.SkillsPage, "技能", 760, 660).show()

    def show_settings(self) -> None:
        self._page_for("settings", pages_mod.SettingsPage, "设置", 660, 700).show()

    def show_devices(self) -> None:
        self._page_for("devices", pages_mod.DevicesPage, "设备", 620, 460).show()

    def show_about(self) -> None:
        self._page_for("about", pages_mod.AboutPage, "关于", 620, 620).show()

    def show_logs(self) -> None:
        dialog = LogDialog(self.console, self)
        dialog.show()
        self._logs_dialog = dialog

    # ───────────────── 控制 ─────────────────

    def start_engine(self) -> None:
        if self.console.agent is not None and self.console.agent.running:
            return
        self.console.start_engine()

    def stop_engine(self) -> None:
        self.console.stop_engine()

    def cancel_task(self) -> None:
        if self.console.agent is not None:
            self.console.agent.cancel()

    def restart_engine(self) -> None:
        """重启引擎：让「改了但要重启才生效」的那些设置落地。

        引擎起来之前按钮是灰的，免得连点几次叠出好几个启动线程；
        进度通过 _tick 里的 starting 状态反馈（提示条上写「正在重启…」）。
        """
        self.notice.set_busy(True)
        self.console.restart_engine()

    def set_accent(self, value: str) -> None:
        """换主题主色：换样式表 + 重建主面板上按颜色渲染过的图标。

        图标是渲染成位图再缓存的（theme.icon），只改样式表不会让它们变色，
        所以这里显式重做一遍。菜单里的页面按需创建，清掉缓存即可 —— 下次
        打开就是新配色。
        """
        color = theme.set_accent(value)
        self.setStyleSheet(theme.qss())
        # 已经建过的页面也刷一遍（设置页就是发信号的那个）。
        # 这里刻意不关窗口：用户点一下色点，设置窗口就自己消失，很突兀。
        for page in self.pages.values():
            try:
                page.apply_theme()
            except Exception:  # noqa: BLE001 - 换主题失败不该影响主流程
                pass
        for button in (self.btn_menu, self.btn_min, self.btn_close):
            button.refresh_theme()
        self.notice.refresh_theme()
        self.setWindowIcon(app_icon(theme.STATE_COLORS.get(self.current_state, theme.ACCENT)))
        self.console.log("[ui] 主题色换成了 " + color)

    # ───────────────── 刷新 ─────────────────

    @staticmethod
    def _state_of(data: dict) -> str:
        status = data.get("status", {})
        if data.get("starting"):
            return "think"
        if not status.get("running"):
            return "off"
        if status.get("speaking"):
            return "speak"
        if status.get("state") == "listen":
            return "listen"
        if status.get("state") in ("think", "wait"):
            return "think"
        return "idle"

    def _tick(self) -> None:
        try:
            data = self.console.snapshot()
        except Exception:  # noqa: BLE001 - 取状态失败不能让窗口崩掉
            return
        self.latest = data
        state = self._state_of(data)
        self.current_state = state
        self.home.on_tick(data, state)
        self._refresh_notice(data)
        if state != self._last_state:
            self._last_state = state
            self.setWindowIcon(app_icon(theme.STATE_COLORS.get(state, theme.BLUE)))
        for key, page in self.pages.items():
            if key != "home" and page.isVisible():
                page.on_tick(data, state)

    def _refresh_notice(self, data: dict) -> None:
        """该不该弹「要重启」：有几项设置改完没重启，而且引擎正在跑。"""
        items = list(data.get("restart_items") or [])
        busy = bool(data.get("starting"))
        if not items:
            self.notice.hide_bar()
            return
        self.notice.show_message(
            "有 " + str(len(items)) + " 项设置要重启引擎才生效",
            "、".join(items),
        )
        self.notice.set_busy(busy)

    # ───────────────── 无边框窗口的杂活 ─────────────────

    def enterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._show_bar(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._show_bar(False)
        super().leaveEvent(event)

    def _show_bar(self, visible: bool) -> None:
        if visible == self._bar_visible:
            return
        self._bar_visible = visible
        self._bar_anim.stop()
        self._bar_anim.setStartValue(self._bar_effect.opacity())
        self._bar_anim.setEndValue(1.0 if visible else 0.0)
        self._bar_anim.start()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        self._drag = None
        super().mouseReleaseEvent(event)

    def _restore_position(self) -> None:
        saved = self.settings.value("position")
        if saved is not None:
            self.move(saved)
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.right() - self.width() - 40, area.top() + 60)

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        self.settings.setValue("position", self.pos())
        try:
            self.console.stop_engine()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)


class LogDialog(FramelessDialog):
    """运行日志。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(parent, "运行日志", 820, 560)
        self.console = console
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(600)
        self.body.addWidget(self.view, 1)
        self._seen = 0
        self._dump()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._dump)
        self._timer.start(400)

    def _dump(self) -> None:
        logs = list(self.console.logs)
        for item in logs[self._seen:]:
            self.view.appendPlainText(str(item.get("text", "")))
        self._seen = len(logs)


def run(config_path: Path | None = None, autostart: bool = True) -> int:
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("语音 Agent")
    app.setStyleSheet(theme.qss())
    window = MainWindow(config_path, autostart=autostart)
    window.show()
    ui.fade_in(window, 200)
    return app.exec()


def main(config_path: Path | None = None, autostart: bool = True) -> int:
    try:
        return run(config_path, autostart)
    except ImportError as exc:
        print("没有安装 PyQt6：" + str(exc))
        print("安装：python -m pip install PyQt6")
        print("或者用网页版：python -m voice_agent ui --web")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
