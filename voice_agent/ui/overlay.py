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
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QGuiApplication
from PyQt6.QtWidgets import QWidget

from .. import marks as marks_mod
from . import theme

__all__ = ["MarksOverlay"]


class MarksOverlay(QWidget):
    """画标记的全屏透明层。"""

    #: 框选完成（物理像素矩形）或取消（None），工作线程等这个信号
    selection_done = pyqtSignal(object)

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
        self.selection_done.connect(self._finish_selection)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._sync)
        self._timer.start(250)

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
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        for mark in marks_mod.store.all():
            self._paint_mark(painter, mark, dpr)
        if self._selecting and not self._current.isNull():
            pen = QPen(QColor(theme.ACCENT), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(theme.rgba(theme.ACCENT, 0.12)))
            painter.drawRect(self._current)
        painter.end()

    def _paint_mark(self, painter: QPainter, mark, dpr: float) -> None:  # noqa: ANN001
        left, top, right, bottom = mark.rect
        x1, y1 = int(left / dpr) - self.x(), int(top / dpr) - self.y()
        x2, y2 = int(right / dpr) - self.x(), int(bottom / dpr) - self.y()
        accent = QColor(theme.ACCENT)
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
        painter.setBrush(QColor(theme.rgba(theme.ACCENT, 0.82)))
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
        self.show_overlay()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, False)
        self.show()                     # 改了 flag 要重新 show 才生效
        self.raise_()
        self.activateWindow()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)
        self.grabKeyboard()
        self.update()
        return True

    def _finish_selection(self, rect) -> None:  # noqa: ANN001
        """选择结束（正常结束或取消）。"""
        self._selecting = ""
        self._current = QRect()
        self.unsetCursor()
        self.releaseKeyboard()
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
        self._start = event.position().toPoint()
        self._current = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._selecting:
            return
        if self._selecting == "point":
            self._current = QRect(event.position().toPoint(), event.position().toPoint())
        else:
            self._current = QRect(self._start, event.position().toPoint()).normalized()
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._selecting or event.button() != Qt.MouseButton.LeftButton:
            return
        dpr = self._dpr()
        point = event.position().toPoint()
        result: dict
        if self._selecting == "point":
            result = {"kind": "point",
                      "x": int((point.x() + self.x()) * dpr),
                      "y": int((point.y() + self.y()) * dpr)}
        else:
            rect = QRect(self._start, point).normalized()
            if rect.width() < 8 or rect.height() < 8:
                self.selection_done.emit(None)      # 手抖点了一下，当取消
                return
            result = {"kind": "region",
                      "x1": int((rect.left() + self.x()) * dpr),
                      "y1": int((rect.top() + self.y()) * dpr),
                      "x2": int((rect.right() + self.x()) * dpr),
                      "y2": int((rect.bottom() + self.y()) * dpr)}
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
