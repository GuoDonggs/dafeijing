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

from PyQt6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QSettings,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..agent import VoiceAgent
from ..console import Console
from ..tools import marks as tools_marks
from . import components as ui
from . import pages as pages_mod
from . import theme

APP_NAME = "VoiceAgent"
PANEL_WIDTH = 384
PANEL_MAX_HEIGHT = 700


#: icon.webp 换来的 QIcon（只加载一次）
_APP_ICON: list = [None]


def _icon_from_webp() -> QIcon | None:
    """程序图标：优先用仓库里的 icon.webp（打包后它在 voice_agent/web/ 里）。

    找不到就返回 None，让调用方退回原来画的圆 —— 图标这种事不该让程序起不来。
    """
    if _APP_ICON[0] is not None:
        return _APP_ICON[0]
    for candidate in (Path(__file__).resolve().parent.parent / "web" / "icon.webp",
                      Path(__file__).resolve().parent.parent.parent / "icon.webp"):
        if candidate.is_file():
            icon = QIcon(str(candidate))
            if not icon.isNull():
                _APP_ICON[0] = icon
                return icon
    _APP_ICON[0] = False       # 找过了，没有
    return None


def app_icon(color: str = theme.BLUE) -> QIcon:
    # 有 icon.webp 就用它（任务栏/开始菜单/安装程序里是同一张图）；
    # 状态色只在没有图片时用来画那个圆。
    custom = _icon_from_webp()
    if custom is not None:
        return custom
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


#: 任务栏/开始菜单靠它把进程和图标认成一个独立程序（不设的话 Windows 会
#: 把这些窗口归到 python.exe 名下，任务栏上就是 Python 的图标）
APP_USER_MODEL_ID = "voiceagent.dafeijing.voice-assistant"


def apply_app_identity(app=None) -> None:  # noqa: ANN001
    """给进程和应用设图标与身份：任务栏、Alt+Tab、子窗口都用同一张图。

    两件事都要做：
    1. SetCurrentProcessExplicitAppUserModelID —— 让 Windows 知道"这是一个独立
       程序"，否则任务栏按钮/固定项显示的是 python.exe 的图标；
    2. QApplication.setWindowIcon —— 子窗口（设置页、运行日志、模型向导…）
       自己不设图标，靠应用级图标兜底，不然它们又变回默认图标。
    """
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001 - 非 Windows / 老系统就算了
        pass
    icon = app_icon()
    if icon.isNull():
        return
    if app is None:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
    try:
        if app is not None:
            app.setWindowIcon(icon)
    except Exception:  # noqa: BLE001
        pass


class FramelessDialog(QDialog):
    """无边框的圆角窗口：页面窗口和日志窗口都用它当外壳。

    自己实现拖动和关闭按钮，外观才和主面板一致。
    """

    def __init__(self, parent: QWidget | None, title: str, width: int, height: int) -> None:
        super().__init__(parent)
        # 独立顶层窗口：不自己设一次，任务栏上就是默认（Python）图标
        self.setWindowIcon(app_icon())
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setModal(False)
        self.resize(width, height)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        self.panel = ui.RoundedPanel(self, radius=18, color=theme.BG,
                                     border=theme.PANEL_BORDER, top=theme.SURFACE_TOP)
        outer.addWidget(self.panel)
        self.apply_theme()

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

    def apply_theme(self) -> None:
        """把当前主题刷到这个窗口上（换主色时主窗口会挨个调用）。

        页面窗口是独立顶层窗口，主窗口 setStyleSheet 影响不到它们 ——
        以前漏了这一步，所以"换了主题色，弹出来的设置页还是旧配色"。
        """
        self.setStyleSheet(theme.qss())
        self.panel.set_colors(theme.BG, theme.PANEL_BORDER, theme.SURFACE_TOP)

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

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        """关窗时把页面摘出来，别让它跟着窗口一起销毁。

        页面是**缓存的**（同一个 Page 实例会被反复塞进新窗口）。窗口一关，
        它的子控件会被 Qt 一起销毁，而缓存里还留着那个已经死掉的指针 ——
        下次点「设置」就会出现一个只有标题、内容全空的窗口。
        这里先把页面从窗口里摘下来（setParent(None)），它就活得下来。
        """
        self._timer.stop()
        self.page.setParent(None)
        super().closeEvent(event)
        # 关掉就回收：只"藏起来"的话，反复开关会一直堆窗口对象，
        # 每个还带着一个已经停掉但没释放的定时器。
        self.deleteLater()

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

    #: 助手要求「重启 / 退出程序本身」时发出（带着 "restart" / "quit"）
    self_control_requested = pyqtSignal(str)
    #: 工具要求"让用户在屏幕上框一下 / 点一下"（工作线程 → 界面线程）
    marks_select_requested = pyqtSignal(str)
    #: 框选超时/作废：让界面线程把叠加层退出选择模式
    marks_cancel_requested = pyqtSignal()
    #: 工具线程要弹一个"确认吗"（Qt 的东西只能在界面线程碰）
    confirm_requested = pyqtSignal(str)

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
        self.setWindowTitle("大肥鲸")
        self.setWindowIcon(app_icon())
        self.setStyleSheet(theme.qss())
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._size_to_screen()
        self._restore_position()

        # 没有语音通道时（引擎没启动、界面里点"试运行"）也要能确认：
        # 以前这条路直接返回 False，于是放开模式下"执行命令"这类操作
        # 全被拒，用户看到的是"明明放开了却什么都干不了"。
        # 托盘图标：窗口图标、任务栏、托盘用同一张图（icon.webp）
        apply_app_identity()
        self._tray: QSystemTrayIcon | None = None

        self._confirm_pending: dict | None = None
        self.confirm_requested.connect(self._show_confirm_box)
        self.console.confirm_hook = self._ask_confirm_from_worker

        self._build()
        self._build_tray()
        self._bind_shortcuts()

        # 助手要「重启 / 退出程序本身」时，请求会从这里转回界面线程 ——
        # 工具是在工作线程里跑的，直接动 Qt 的窗口会崩。
        self.self_control_requested.connect(self._on_self_control)
        self.console.app_hook = self.self_control_requested.emit
        # 标记层：助手说「框一下这块」时，由它出面让用户拖一个框出来
        self.marks_select_requested.connect(self._begin_selection)
        self.marks_cancel_requested.connect(self._cancel_selection_mode)
        self._marks_overlay = None
        self._marks_wait: dict | None = None
        tools_marks.set_marks_handler(self.request_selection)

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
        self.panel = ui.RoundedPanel(self, radius=20, color=theme.BG,
                                     border=theme.PANEL_BORDER, top=theme.SURFACE_TOP)
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
        self.btn_min = ui.WindowButton("chevrondown", "最小化到托盘")
        self.btn_min.clicked.connect(self.hide_to_tray)
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
        # 每个页面最多一个窗口；重复点菜单时把它提到前面就行
        self._dialogs: dict[str, PageDialog] = {}
        # 运行日志窗口。**必须在这里先置空**：show_logs 第一句就要读它，
        # 而以前它只在 show_logs 内部被赋值 —— 于是第一次打开日志直接
        # AttributeError 闪退（第二次才"正常"，所以很容易漏测）。
        self._logs_dialog: LogDialog | None = None
        self.home.start_requested.connect(self.start_engine)    # type: ignore[attr-defined]
        self.home.stop_requested.connect(self.stop_engine)      # type: ignore[attr-defined]
        self.home.cancel_requested.connect(self.cancel_task)    # type: ignore[attr-defined]

    def _page_for(self, key: str, factory, title: str, width: int = 720,
                  height: int = 640) -> PageDialog:
        """按需创建页面 —— 没打开过的页面不占内存，启动也更快。

        窗口已经开着就直接提到前面，不再开第二个：以前重复点「设置」会新开一个
        窗口，把页面控件从旧窗口里抢走，两个窗口都变得半死不活。
        """
        existing = self._dialogs.get(key)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return existing
        if key not in self.pages:
            page = factory(self.console)
            # 设置页里换了主题色，主窗口得跟着变色（图标是渲染后缓存的，
            # 只重设样式表不够）
            signal = getattr(page, "accent_changed", None)
            if signal is not None:
                signal.connect(self.set_accent)
            self.pages[key] = page
        dialog = PageDialog(self.pages[key], title, self, width, height)
        # 销毁时把缓存里的引用一起清掉（不然那个 key 永远指着一个死对象）
        dialog.destroyed.connect(lambda _=None, k=key: self._dialogs.pop(k, None))
        self._dialogs[key] = dialog
        return dialog

    # ───────────────── 屏幕上的标记（框选 / 标点）─────────────────

    def _ensure_overlay(self):
        """标记层按需创建 —— 没框过东西就不该多一个全屏窗口。"""
        if self._marks_overlay is None:
            from .overlay import MarksOverlay

            self._marks_overlay = MarksOverlay(self)
            self._marks_overlay.selection_done.connect(self._on_selection_done)
            # "框太小了，再拖一次"这类提示直接进运行日志
            self._marks_overlay.selection_notice.connect(self.console.log)
        return self._marks_overlay

    def start_marks(self, kind: str = "region") -> None:
        """菜单入口：让用户框一块 / 点一下。"""
        if self._marks_wait is not None:
            # 有工具正在等用户框选：这时候不能把等待作废（以前的写法是
            # 无条件清掉 _marks_wait），否则那次框选的结果会被当成"界面标记"
            # 存下来，而工具还在原地白等到 45 秒超时。
            self.console.log("[ui] 现在正在等你在屏幕上选（那是某个工具要的），"
                             "先选完它，或者按 Esc 取消")
            return
        overlay = self._marks_overlay
        if overlay is not None and getattr(overlay, "_selecting", ""):
            self.console.log("[ui] 已经在框选模式里了，直接在屏幕上操作就行")
            return
        self._begin_selection(kind)

    def request_selection(self, kind: str, timeout: float = 45.0) -> dict | None:
        """工具（工作线程）调过来的：等用户在屏幕上选完。

        Qt 的东西只能在界面线程碰，所以这里发个信号过去，自己在这儿等。
        """
        import threading

        wait = {"done": threading.Event(), "result": None}
        self._marks_wait = wait
        self.marks_select_requested.emit(kind)
        if not wait["done"].wait(timeout):
            self.console.log("[ui] 框选超时了（没等到你在屏幕上选）")
            # **界面也要跟着退出选择模式**：以前只把等待位清掉，overlay 还停在全屏
            # 框选态（鼠标不穿透、键盘被抓着），用户之后随手一拖就被当成"界面框选"
            # 存下来 —— 一个他完全不知道的幽灵标记，而且不按 Esc 出不去。
            self.marks_cancel_requested.emit()
            if self._marks_wait is wait:
                self._marks_wait = None
            return None
        result = wait["result"]
        if self._marks_wait is wait:
            self._marks_wait = None
        return result

    def _begin_selection(self, kind: str) -> None:
        overlay = self._ensure_overlay()
        overlay.show_overlay()
        want = "point" if kind == "point" else "region"
        current = getattr(overlay, "_selecting", "")
        if current:
            # 已经在框选模式里了。以前这里照样调 start_selection —— 它看见
            # _selecting 非空就直接 return False（什么都不做），于是工具在那边
            # 白等到 45 秒超时。
            #   · 类型一样：用户的操作还在进行，接着选完就会交给工具；
            #   · 类型不一样（多半是菜单先开的那种）：直接换成工具要的那种。
            if current != want:
                overlay.restart_selection(want)
            self.console.log("[ui] 现在是" + ("标点" if want == "point" else "框选")
                             + "模式，直接在屏幕上操作就行（按 Esc 取消）")
            return
        self.console.log("[ui] 请在屏幕上"
                         + ("点一下" if want == "point" else "拖一个框")
                         + "（按 Esc 取消）")
        if not overlay.start_selection(want):
            self.console.log("[ui] 框选没能打开，你再试一次")

    def _cancel_selection_mode(self) -> None:
        """把叠加层从选择模式里退出来（工具已经不等了，界面不能还停在那儿）。"""
        overlay = self._marks_overlay
        if overlay is not None and getattr(overlay, "_selecting", ""):
            overlay.cancel_selection()

    def _on_selection_done(self, result) -> None:
        """用户选完了：要么交给等着的工具，要么直接存成标记（菜单进来的）。"""
        wait, self._marks_wait = self._marks_wait, None
        if wait is not None:
            wait["result"] = result
            wait["done"].set()
            return
        if not result:
            self.console.log("[ui] 已取消")
            return
        from .. import marks as marks_mod

        if result.get("kind") == "point":
            mark = marks_mod.store.add_point(result["x"], result["y"], note="界面标点")
        else:
            mark = marks_mod.store.add_region(result["x1"], result["y1"],
                                              result["x2"], result["y2"], note="界面框选")
        self.console.log("[ui] 记好了：" + mark.summary())

    # ───────────────── 控制程序自己 ─────────────────

    def _on_self_control(self, action: str) -> None:
        """语音让助手重启 / 退出（信号从工作线程发过来，这里是界面线程）。"""
        what = str(action or "").strip().lower()
        if what == "restart":
            self.console.log("[ui] 收到重启指令，正在重新启动……")
            # 先松开麦克风再拉新进程：反过来的话新实例起来时设备还被占着，
            # 用户看到的是"重启完就不听使唤了"
            self.console.stop_engine()
            if not self.relaunch_program():
                self.console.log("[ui] 重启失败，改为直接退出")
        else:
            self.console.log("[ui] 收到退出指令，正在关闭……")
        # 留一点时间把最后那句话念完、把日志落下去
        QTimer.singleShot(700, self._quit_now)

    def _quit_now(self) -> None:
        # 位置存在 QSettings 里（closeEvent 也做一次）。以前这里调了一个
        # **根本不存在**的 self._save_position() —— 语音说「退下」走的就是这条路，
        # AttributeError 直接把进程 abort 掉（窗口"啪"地消失）。
        self._remember_position()
        self.console.stop_engine()
        app = QApplication.instance()
        if app is not None:
            app.quit()
        else:
            sys.exit(0)

    @staticmethod
    def relaunch_program() -> bool:
        """重新拉起自己（新进程），旧进程随后退出。"""
        from PyQt6.QtCore import QProcess

        command = VoiceAgent.relaunch_command()
        try:
            started, _pid = QProcess.startDetached(command[0], command[1:])
        except Exception as exc:  # noqa: BLE001
            print("[ui] 重启失败：" + str(exc), file=sys.stderr)
            return False
        return bool(started)

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
        self._build_menu().exec(
            self.btn_menu.mapToGlobal(QPoint(0, self.btn_menu.height() + 6)))

    def _build_menu(self) -> QMenu:
        """拼出 ☰ 菜单（单独一个方法，方便测试里检查结构）。"""
        menu = QMenu(self)
        menu.setStyleSheet(theme.qss())
        entries = [
            # 「对话记录」和「文字指令」本来就是同一个页面，合并成一条
            ("chat", "对话与指令", "Ctrl+K", self.show_chat),
            ("plus", "开始新会话", "", self.new_session),
            # 标记只有这一个入口：框选/标点/删除都在它打开的窗口里
            # （几个动作是同一件事的不同步骤，拆在菜单上反而找不到）
            ("search", "屏幕标记…", "", self.show_marks),
            (None, None, None, None),
            ("tools", "工具", "Ctrl+T", self.show_tools),
            ("skills", "技能", "", self.show_skills),
            (None, None, None, None),
            ("settings", "设置", "Ctrl+,", self.show_settings),
            ("mic", "设备", "", self.show_devices),
            (None, None, None, None),
            ("activity", "运行日志", "Ctrl+L", self.show_logs),
            ("disk", "语音模型", "", self.show_model_setup),
            ("info", "关于", "", self.show_about),
            (None, None, None, None),
            ("power", "退出", "Ctrl+Q", self.close),
        ]
        for icon_name, label, shortcut, slot in entries:
            if label is None:
                menu.addSeparator()
                continue
            icon = theme.icon(icon_name, theme.TEXT, 16)
            action = menu.addAction(icon, label)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
        return menu

    # ───────────────── 各页面 ─────────────────

    def show_chat(self) -> None:
        self._page_for("chat", pages_mod.ChatPage, "对话与指令", 660, 640).show()

    def show_marks(self) -> None:
        """打开「屏幕标记」页：看列表、改名字坐标、删、再框一个。"""
        from .marks_page import MarksPage

        self._page_for("marks", MarksPage, "屏幕标记", 660, 640).show()

    def clear_marks(self) -> None:
        """擦掉屏幕上所有标记。"""
        from .. import marks as marks_mod

        count = marks_mod.store.clear()
        self.console.log("[ui] 擦掉了 " + str(count) + " 个标记"
                         if count else "[ui] 本来就没有标记")

    def new_session(self) -> None:
        """开一个新会话：清掉上下文，界面上的历史留着。"""
        agent = self.console.ensure_agent()
        message = agent.new_session()
        self.console.log("[ui] " + message)
        self._tick()          # 立刻刷一次主面板：问答卡片会清空，用户看得出换了会话

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
        """日志窗口只留一个：反复按 Ctrl+L 不该堆出一摞窗口（各带一个定时器）。"""
        existing = self._logs_dialog
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:      # 底层对象已经销毁
                self._logs_dialog = None
        dialog = LogDialog(self.console, self)
        dialog.destroyed.connect(lambda _=None: setattr(self, "_logs_dialog", None))
        dialog.show()
        self._logs_dialog = dialog

    # ───────────────── 控制 ─────────────────

    # ───────────────── 托盘 ─────────────────
    def _build_tray(self) -> None:
        """系统托盘：最小化之后还能操作（以前最小化就找不着了）。"""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("大肥鲸 · 语音助手")
        menu = QMenu()
        menu.addAction("显示主界面", self.show_from_tray)
        menu.addSeparator()
        menu.addAction("启动监听", self.start_engine)
        menu.addAction("停止监听", self.stop_engine)
        menu.addAction("打断当前任务", self.cancel_task)
        menu.addSeparator()
        menu.addAction("退出", self.close)
        tray.setContextMenu(menu)
        # 左键单击/双击都拉回主界面（两种习惯都有）
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason) -> None:  # noqa: ANN001
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_from_tray()

    def hide_to_tray(self) -> None:
        """收起主界面但**继续在后台听**（再点托盘图标就回来）。"""
        if self._tray is None:
            self.showMinimized()      # 没有托盘（少数桌面环境）时退回普通最小化
            return
        self.hide()
        self.console.log("[ui] 已收进托盘，还在后台听着；点托盘图标回来")
        try:
            self._tray.showMessage("大肥鲸还在后台", "点托盘图标可以把我叫回来。",
                                   app_icon(), 3000)
        except Exception:  # noqa: BLE001 - 通知失败无所谓
            pass

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    # ───────────────── 确认通道（无语音时用对话框）─────────────────
    def _ask_confirm_from_worker(self, question: str) -> bool:
        """接口给 Console 用：工作线程里问一句"确认吗"，在界面线程弹模态框。

        超时按**拒绝**处理（fail closed），和语音确认一个口径。
        """
        import threading

        if threading.current_thread() is threading.main_thread():
            return self._confirm_box(question)
        holder: dict = {"answer": False, "done": threading.Event()}
        self._confirm_pending = holder
        self.confirm_requested.emit(question)
        limit = float(getattr(self.console.cfg.agent.confirm, "timeout_ms", 10000)) / 1000.0
        if not holder["done"].wait(max(5.0, limit + 5.0)):
            self.console.log("[ui] 确认超时（没等到你回答），这一步没执行")
            return False
        return bool(holder["answer"])

    def _show_confirm_box(self, question: str) -> None:
        """界面线程：弹框并把答案交回等待中的工作线程。"""
        holder, self._confirm_pending = self._confirm_pending, None
        try:
            answer = self._confirm_box(question)
        except Exception as exc:  # noqa: BLE001 - 弹框失败也不能把工具线程卡死
            self.console.log("[ui] 确认框出错：" + str(exc)[:80])
            answer = False
        if holder is not None:
            holder["answer"] = answer
            holder["done"].set()

    def _confirm_box(self, question: str) -> bool:
        """真的问一句（默认按钮是「否」：手滑回车不会执行危险操作）。"""
        box = QMessageBox(self)
        box.setWindowTitle("需要确认")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("执行")
        box.button(QMessageBox.StandardButton.No).setText("取消")
        limit = int(getattr(self.console.cfg.agent.confirm, "timeout_ms", 10000))
        QTimer.singleShot(max(3000, limit), box.reject)
        return box.exec() == QMessageBox.StandardButton.Yes

    def start_engine(self) -> None:
        if self.console.agent is not None and self.console.agent.running:
            return
        cfg = getattr(self.console, "cfg", None)
        if cfg is not None and getattr(cfg, "missing_models", None):
            # 缺模型就直接把向导弹出来：以前这里会打印一句"运行
            # python scripts/download_models.py"，打包版里那条路根本不存在。
            self.show_model_setup()
            return
        self.console.start_engine()

    def show_model_setup(self) -> None:
        """「语音模型」向导：缺了就下/或指到已有目录，齐了就当状态页看。"""
        from .model_setup_dialog import ModelSetupDialog

        cfg = getattr(self.console, "cfg", None)
        missing = list(getattr(cfg, "missing_models", []) or [])
        if not missing:
            self.console.log("[ui] 模型目录：" + str(getattr(cfg, "models_dir", "")))
            box = QMessageBox(self)
            box.setWindowTitle("语音模型")
            box.setText("需要的模型都在：" + str(getattr(cfg, "models_dir", "")))
            box.exec()
            return
        dialog = ModelSetupDialog(self.console, parent=self)
        if dialog.exec():
            self._refresh_notice(self.latest or {})
            cfg = getattr(self.console, "cfg", None)
            if cfg is not None and not getattr(cfg, "missing_models", None):
                self.console.log("[ui] 模型就绪，可以启动监听了")
                self.start_engine()

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
        # 面板描边 + 顶部渐变也带主色：换色时整块窗口一起变，而不是只有按钮变
        self.panel.set_colors(theme.BG, theme.PANEL_BORDER, theme.SURFACE_TOP)
        # 页面窗口是独立顶层窗口，得挨个刷；它们里面还有胶囊、图标要重上色
        for dialog in self.findChildren(FramelessDialog):
            try:
                dialog.apply_theme()
            except Exception:  # noqa: BLE001 - 换主题失败不该影响主流程
                pass
        # 胶囊的底色掺了主色，但它用的是内联样式表，得主动刷一遍
        for pill in self.findChildren(ui.Pill):
            try:
                pill.refresh_theme()
            except Exception:  # noqa: BLE001
                pass
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
        # **每一处刷新都要自己兜住异常**：Qt 槽里逃出去的异常会走 PyQt 的 qFatal，
        # 表现是"点一下程序就没了"（没有报错框、日志里也未必有）。以前只包住了
        # snapshot() 那一句，页面里的一个意外键就能把整个进程带走。
        self._safe_tick("主页", self.home.on_tick, data, state)
        self._safe_tick("提示条", self._refresh_notice, data)
        if state != self._last_state:
            self._last_state = state
            self.setWindowIcon(app_icon(theme.STATE_COLORS.get(state, theme.BLUE)))
        for key, page in self.pages.items():
            if key != "home" and page.isVisible():
                self._safe_tick(key, page.on_tick, data, state)

    def _safe_tick(self, what: str, func, *args) -> None:  # noqa: ANN001
        """跑一次界面刷新；出错就记日志，绝不让异常逃回 Qt 的事件循环。"""
        try:
            func(*args)
        except Exception as exc:  # noqa: BLE001
            self.console.log("[ui] " + str(what) + "刷新失败：" + str(exc)[:120])

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

    def _remember_position(self) -> None:
        """把窗口位置记下来（下次打开还在原地）。"""
        try:
            self.settings.setValue("position", self.pos())
        except Exception:  # noqa: BLE001 - 记不住位置不该拦住退出
            pass

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        self._remember_position()
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
        # 「详细」：参数全文、耗时、token 用量这些排查用的行默认不显示，
        # 勾上就一起看（文件里本来就是全的）
        from PyQt6.QtWidgets import QCheckBox

        self.detail = QCheckBox("详细（工具参数、耗时、token 用量）")
        self.detail.setToolTip("这些行在日志文件里一直都有，这里只是显示不显示")
        self.detail.stateChanged.connect(self._redump)
        self.body.addWidget(self.detail)
        self._seen = 0
        self._dump()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._dump)
        self._timer.start(400)

    def _dump(self) -> None:
        """把还没显示过的日志追加进去。

        按**序号**而不是下标来算"新的"：日志缓冲区是个 deque(maxlen=600)，
        写满之后从头丢，长度永远是 600 —— 用下标的话 _seen 会一直等于 600，
        从此再也捞不到新内容，日志窗口看起来就像"卡住了"。
        """
        logs = list(self.console.logs)
        if not logs:
            return
        new = [item for item in logs if int(item.get("seq") or 0) > self._seen]
        if not new:
            return
        want_detail = bool(self.detail.isChecked())
        for item in new:
            if not want_detail and item.get("level") == "detail":
                continue
            self.view.appendPlainText(str(item.get("text", "")))
        self._seen = int(new[-1].get("seq") or self._seen)

    def _redump(self) -> None:
        """勾/取消「详细」之后重画一遍。"""
        self.view.clear()
        self._seen = 0
        self._dump()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802
        """关窗要停掉定时器并回收，否则每按一次 Ctrl+L 就多一个常驻窗口。"""
        self._timer.stop()
        super().closeEvent(event)
        self.deleteLater()


def run(config_path: Path | None = None, autostart: bool = True) -> int:
    # 窗口版没有控制台：崩了只有这个钩子能把栈写进日志文件
    from .. import journal

    journal.install_crash_handler()
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("大肥鲸")
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
