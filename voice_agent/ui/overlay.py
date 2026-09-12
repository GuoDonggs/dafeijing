# -*- coding: utf-8 -*-
"""屏幕上的标记层：框选的虚线框和点过的圆点。

一个**透明、置顶、平时点不中**的全屏窗口，专门用来画标记：

- 范围：主题色虚线框 + 左上角的名字（范围1、范围2…）；
- 点  ：半透明圆点 + 名字（点1、点2…）。

它平时对鼠标完全透明（WindowTransparentForInput），只有进入"框选/标点"模式时
才接收鼠标 —— 否则用户就点不到自己桌面上的东西了。

坐标：标记存的是**物理像素**（和截图、鼠标同一套坐标），Qt 的窗口几何是
逻辑像素，所以画的时候除以 dpr、框选的时候乘回去。
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QWidget

from .. import marks as marks_mod
from . import theme

__all__ = ["MarksOverlay"]


class MarksOverlay(QWidget):
    """画标记的全屏透明层。"""

    #: 框选完成（物理像素矩形）或取消（None），工作线程等这个信号
    selection_done = pyqtSignal(object)
    #: 给用户的一句提示（"框太小了，再拖一次"），界面拿去写日志
    selection_notice = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput      # 平时点不中
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._version = -1
        self._selecting = ""
        self._start = QPoint()
        self._current = QRect()
        #: 鼠标真的按下了没有。**没有它就会出现"还没按就已经定了一个角"**：
        #: _start 默认是 (0,0)，一移动鼠标就画出一条从屏幕左上角拉过来的虚线。
        self._pressed = False
        #: 正在"闪一下"的标记名 + 什么时候结束（"闪一下"按钮用）
        self._flash_name = ""
        self._flash_until = 0.0
        self.selection_done.connect(self._finish_selection)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(250)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self._end_flash)

    # ── 几何 ──
    @staticmethod
    def _union_rect() -> QRect:
        """所有显示器合起来的矩形（逻辑像素）。"""
        rect = QRect()
        for screen in QGuiApplication.screens():
            rect = rect.united(screen.geometry())
        return rect

    @staticmethod
    def _dpr() -> float:
        screen = QGuiApplication.primaryScreen()
        try:
            return float(screen.devicePixelRatio()) if screen else 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    @staticmethod
    def _logical_dpr_at(x: int, y: int) -> float:
        """按**逻辑坐标**找这块屏的缩放比（Qt 的 screen 几何是逻辑坐标）。"""
        try:
            for screen in QGuiApplication.screens():
                if screen.geometry().contains(int(x), int(y)):
                    return float(screen.devicePixelRatio() or 1.0)
        except Exception:  # noqa: BLE001
            pass
        return MarksOverlay._dpr()

    @staticmethod
    def _dpr_at(x: int, y: int) -> float:
        """**这一点所在那块屏幕**的缩放比（找不到就退回主屏的）。

        覆盖层铺满所有显示器，而各显示器的缩放比可以不一样（主屏 150% +
        副屏 100% 很常见）。统一拿主屏的 dpr 换算，副屏上的标记和点击就会
        整体偏一截。两块屏 dpr 相同时这个函数与 _dpr() 完全等价
        （本机就是 1.0 / 1.0），所以不改变现有行为。
        """
        try:
            for screen in QGuiApplication.screens():
                ratio = float(screen.devicePixelRatio() or 1.0)
                geo = screen.geometry()
                left = int(geo.x() * ratio)
                top = int(geo.y() * ratio)
                if (left <= x <= left + int(geo.width() * ratio)
                        and top <= y <= top + int(geo.height() * ratio)):
                    return ratio
        except Exception:  # noqa: BLE001
            pass
        return MarksOverlay._dpr()

    def show_overlay(self) -> None:
        rect = self._union_rect()
        if rect.isValid():
            self.setGeometry(rect)
        self.show()
        self.raise_()

    # ── 刷新 ──
    def _sync(self) -> None:
        if not self.isVisible():
            return
        if marks_mod.store.version != self._version:
            self._version = marks_mod.store.version
            self.update()

    # ── 画 ──
    def paintEvent(self, _event) -> None:  # noqa: ANN001, N802
        dpr = self._dpr()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._selecting:
            # 选择模式下把整块铺一层几乎看不见的底色。两个作用：
            # ① Windows 的分层窗口在**全透明**的像素上不接收鼠标 ——
            #    不铺这一层就会出现"只有鼠标停在已有标记上时才点得动"；
            # ② 顺便让用户看出来"现在是在框选"。
            painter.fillRect(self.rect(), theme.qcolor(theme.ACCENT, 0.05))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        # 「隐藏标记」只是**不画**，不是删掉：名字照样能引用（「点点1」还有效），
        # 用户框完一堆东西嫌挡视线时就靠它换个清净。
        # 例外是「闪一下」的那一个：那是用户明确说「给我看这个」，得画出来。
        visible = marks_mod.store.visible
        for mark in marks_mod.store.all():
            if not visible and mark.name != self._flash_name:
                continue
            self._paint_mark(painter, mark, dpr)
        if self._selecting and self._pressed and not self._current.isNull():
            pen = QPen(QColor(theme.ACCENT), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            # 半透明填充：QColor 得用数值构造，rgba(...) 是给 QSS 用的字符串
            painter.setBrush(theme.qcolor(theme.ACCENT, 0.12))
            painter.drawRect(self._current)
        painter.end()

    def flash(self, name: str, milliseconds: int = 1400) -> None:
        """把某个标记闪一下（粗边框 + 亮一点），用来回答"是哪一个"。

        「隐藏」状态下也照闪：用户点「闪一下」的意图就是"让我看看它在哪"，
        这时候不画反而是不听话（paintEvent 里对正在闪的那个开了例外）。
        """
        self._flash_name = str(name or "")
        self.show_overlay()
        self.update()
        self._flash_timer.start(max(200, int(milliseconds)))

    def _end_flash(self) -> None:
        self._flash_name = ""
        self.update()

    def _paint_mark(self, painter: QPainter, mark, dpr: float) -> None:  # noqa: ANN001
        left, top, right, bottom = mark.rect
        # 两个角各按自己所在屏幕的缩放比换算（同一块屏上结果与以前一样）
        ratio1 = self._dpr_at(left, top)
        ratio2 = self._dpr_at(right, bottom)
        x1, y1 = int(left / ratio1) - self.x(), int(top / ratio1) - self.y()
        x2, y2 = int(right / ratio2) - self.x(), int(bottom / ratio2) - self.y()
        accent = QColor(theme.ACCENT)
        flashing = bool(self._flash_name) and mark.name == self._flash_name
        if flashing:
            # 闪的时候加粗并提亮，扫一眼就能看到是哪一块
            painter.setPen(QPen(theme.qcolor(theme.ACCENT, 1.0), 4))
            painter.setBrush(theme.qcolor(theme.ACCENT, 0.20))
            if mark.kind == "point":
                # 点没有面积：画一个零大小的矩形等于什么都没画（以前就是这样，
                # 所以"闪一下"对点不起作用）。改成一个明显的圆环。
                painter.drawEllipse(QPoint(x1, y1), 22, 22)
                painter.setPen(QPen(theme.qcolor(theme.ACCENT, 0.6), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPoint(x1, y1), 34, 34)
            else:
                painter.drawRect(QRect(QPoint(x1, y1), QPoint(x2, y2)))
            self._paint_label(painter, mark.name, x1 + 4, y1 - 6)
            return
        if mark.kind == "point":
            painter.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 200), 2))
            painter.setBrush(QColor(accent.red(), accent.green(), accent.blue(), 90))
            painter.drawEllipse(QPoint(x1, y1), 9, 9)
            label_at = (x1 + 12, y1 - 12)
        else:
            pen = QPen(accent, 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRect(QPoint(x1, y1), QPoint(x2, y2)))
            label_at = (x1 + 4, y1 - 6)
        self._paint_label(painter, mark.name, label_at[0], label_at[1])

    @staticmethod
    def _paint_label(painter: QPainter, text: str, x: int, y: int) -> None:
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 10
        height = metrics.height() + 2
        box = QRect(x, y - height, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor(theme.ACCENT, 0.82))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # ── 交互：框选 / 标点 ──
    def start_selection(self, kind: str = "region") -> bool:
        """进入选择模式；用户拖完（或按 Esc）后发 selection_done。"""
        if self._selecting:
            return False
        self._selecting = "point" if kind == "point" else "region"
        self._current = QRect()
        self._pressed = False
        self.show_overlay()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, False)
        self.show()                     # 改了 flag 要重新 show 才生效
        # 再把整块显式标成"窗口区域"：分层窗口在完全透明的像素上会把鼠标放过去，
        # 光靠"关掉 WindowTransparentForInput"不够 —— 这是"只有停在已有标记上
        # 才点得动"的另一半原因。
        self.setMask(QRegion(self.rect()))
        self.raise_()
        self.activateWindow()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.grabKeyboard()
        self.update()
        return True

    def restart_selection(self, kind: str) -> None:
        """把正在进行的框选**换成另一种**，但不发 selection_done。

        为什么不能先 cancel 再 start：cancel_selection 会立刻发一个 None，
        把还在等工作线程的工具提前叫醒（它会以为用户取消了）。
        """
        self._selecting = "point" if kind == "point" else "region"
        self._current = QRect()
        self._pressed = False
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def _finish_selection(self, rect) -> None:  # noqa: ANN001
        """选择结束（正常结束或取消）。"""
        self._selecting = ""
        self._current = QRect()
        self._pressed = False
        self.unsetCursor()
        self.releaseKeyboard()
        self.clearMask()            # 松开之后恢复"点得穿"，别挡住桌面
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, True)
        self.show()
        self.update()

    def cancel_selection(self) -> bool:
        if not self._selecting:
            return False
        self.selection_done.emit(None)
        return True

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._selecting or event.button() != Qt.MouseButton.LeftButton:
            return
        self._pressed = True
        self._start = event.position().toPoint()          # 第一个角从**按下**这里算
        self._current = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._selecting:
            return
        point = event.position().toPoint()
        if self._selecting == "point":
            # 标点：还没按下也画一个小圆点跟着走，用户才知道会标在哪
            self._current = QRect(point, point)
        elif self._pressed:
            # 框选：**只有按住时**才拉框。没按就画的话，第一个角会默认落在
            # 屏幕左上角（_start 的初始值），看起来像"还没按就定了一个角"。
            self._current = QRect(self._start, point).normalized()
        else:
            return
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._selecting or event.button() != Qt.MouseButton.LeftButton:
            return
        self._pressed = False
        point = event.position().toPoint()

        def to_screen(px: int, py: int) -> tuple[int, int]:
            """窗口坐标 → 屏幕物理坐标（按这一点所在那块屏的缩放比）。

            注意顺序：先按**逻辑**坐标找到那块屏（Qt 的 screenAt 吃逻辑坐标），
            再拿它的缩放比换算成物理像素。混用（把逻辑坐标喂给按物理坐标写的
            _dpr_at）在混合 DPI 的多屏上会选错屏、坐标整体偏一截。
            """
            fx, fy = px + self.x(), py + self.y()
            ratio = MarksOverlay._logical_dpr_at(fx, fy)
            return int(fx * ratio), int(fy * ratio)

        result: dict
        if self._selecting == "point":
            # 标点：点一下就够了，不用拖
            sx, sy = to_screen(point.x(), point.y())
            result = {"kind": "point", "x": sx, "y": sy}
        else:
            rect = QRect(self._start, point).normalized()
            if rect.width() < 8 or rect.height() < 8:
                # 只是点了一下（没拖动）：不当作"取消"，而是留在框选模式里
                # 让他再拖一次 —— 以前这里直接把模式关掉了，用户得重新点按钮。
                self._current = QRect()
                self.update()
                self.selection_notice.emit("框太小了，按住鼠标拖一个框出来（按 Esc 取消）")
                return
            sx1, sy1 = to_screen(rect.left(), rect.top())
            sx2, sy2 = to_screen(rect.right(), rect.bottom())
            result = {"kind": "region", "x1": sx1, "y1": sy1, "x2": sx2, "y2": sy2}
        self.selection_done.emit(result)

    def keyPressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.key() == Qt.Key.Key_Escape:
            self.selection_done.emit(None)
            return
        super().keyPressEvent(event)


def wait_for_selection(overlay: MarksOverlay, kind: str, timeout: float = 45.0,
                       emit: Callable[[str], None] | None = None) -> dict | None:
    """在工作线程里等界面选完（供工具调用使用）。

    工具跑在工作线程，Qt 的东西只能在界面线程碰 —— 所以这里只发信号 + 等事件。
    """
    import threading

    done = threading.Event()
    holder: dict = {}

    def _on_done(result) -> None:  # noqa: ANN001
        holder["result"] = result
        done.set()

    overlay.selection_done.connect(_on_done)
    try:
        if emit is not None:
            emit(kind)          # 让界面线程去调 start_selection
        if not done.wait(timeout):
            return None
        return holder.get("result")
    finally:
        try:
            overlay.selection_done.disconnect(_on_done)
        except TypeError:
            pass
