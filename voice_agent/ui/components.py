# -*- coding: utf-8 -*-
"""可复用的控件：卡片、开关、分段控件、侧边栏项、状态圆球。

这些都是「画」出来的，不是 Qt 自带控件的皮肤 —— 自带控件做不出 iOS 那种
圆角滑块和弹性动画。每个控件只暴露少量属性，界面代码只负责摆位置。
"""

from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QRadialGradient
from PyQt6.QtWidgets import (
    QAbstractButton,
    QColorDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme


class Card(QFrame):
    """圆角卡片。所有内容都装在卡片里，靠留白分组，不画分割线。"""

    def __init__(self, parent: QWidget | None = None, padding: int = theme.PAD,
                 spacing: int = theme.GAP) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding, padding, padding)
        self.body.setSpacing(spacing)


class Pill(QLabel):
    """小圆角标签：状态、来源之类。"""

    def __init__(self, text: str = "", color: str = theme.MUTED,
                 parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("_pillColor", color)
        self._apply(color)

    def _apply(self, color: str) -> None:
        # 底色掺一点主色：一屏里十几个胶囊，换主题时到处都能看到变化
        self.setStyleSheet(
            "QLabel { background: " + theme.PILL_BG + "; color: " + color
            + "; border-radius: 9px; padding: 2px 10px; font-size: 11px; }"
        )

    def refresh_theme(self) -> None:
        """换主色后重新上色（颜色本身是外面给的，这里只需要重刷底色）。"""
        self._apply(self.property("_pillColor") or theme.MUTED)

    def set_color(self, color: str) -> None:
        self._apply(color)


class ToggleSwitch(QAbstractButton):
    """iOS 风格的开关：圆角轨道 + 会滑动的小球。"""

    def __init__(self, parent: QWidget | None = None, checked: bool = False) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(51, 31)
        self._pos = 1.0 if checked else 0.0
        self._anim = QPropertyAnimation(self, b"position", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def get_position(self) -> float:
        return self._pos

    def set_position(self, value: float) -> None:
        self._pos = float(value)
        self.update()

    position = pyqtProperty(float, fget=get_position, fset=set_position)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        track_off = QColor("#39393F")
        track_on = QColor(theme.GREEN)
        track = QColor(
            int(track_off.red() + (track_on.red() - track_off.red()) * self._pos),
            int(track_off.green() + (track_on.green() - track_off.green()) * self._pos),
            int(track_off.blue() + (track_on.blue() - track_off.blue()) * self._pos),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 2, 51, 27), 13.5, 13.5)

        painter.setBrush(QColor("#FFFFFF"))
        x = 3 + self._pos * (51 - 27 - 6)
        painter.drawEllipse(QRectF(x, 4, 23, 23))
        painter.end()


class SegmentedControl(QWidget):
    """iOS 风格的分段控件：一整条轨道，选中的那格有个会滑动的白底。"""

    changed = pyqtSignal(str)

    def __init__(self, options: list[str], current: str = "",
                 parent: QWidget | None = None, width: int = 260) -> None:
        super().__init__(parent)
        self._options = list(options)
        self._labels = [str(o) for o in options]
        self._index = max(0, self._labels.index(current)) if current in self._labels else 0
        self._pill = float(self._index)
        self.setFixedHeight(32)
        self.setMinimumWidth(width)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"pill", self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def get_pill(self) -> float:
        return self._pill

    def set_pill(self, value: float) -> None:
        self._pill = float(value)
        self.update()

    pill = pyqtProperty(float, fget=get_pill, fset=set_pill)

    def value(self) -> str:
        return self._labels[self._index]

    def set_value(self, value: str, animate: bool = True) -> None:
        if value not in self._labels:
            return
        target = self._labels.index(value)
        if target == self._index:
            return
        self._index = target
        if animate:
            self._anim.stop()
            self._anim.setStartValue(self._pill)
            self._anim.setEndValue(float(target))
            self._anim.start()
        else:
            self.set_pill(float(target))
        self.changed.emit(value)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        width = self.width() / max(1, len(self._labels))
        index = int(event.position().x() // width)
        index = max(0, min(len(self._labels) - 1, index))
        self.set_value(self._labels[index])
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#1F1F28"))
        painter.drawRoundedRect(rect, 9, 9)

        segment = self.width() / max(1, len(self._labels))
        painter.setBrush(QColor("#3A3A47"))
        painter.drawRoundedRect(
            QRectF(self._pill * segment + 2, 2, segment - 4, self.height() - 4), 7.5, 7.5)

        font = QFont(self.font())
        font.setPointSize(9)
        painter.setFont(font)
        for index, label in enumerate(self._labels):
            painter.setPen(QColor(theme.TEXT if index == self._index else theme.MUTED))
            painter.drawText(
                QRectF(index * segment, 0, segment, self.height()),
                Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class NavButton(QAbstractButton):
    """侧边栏的一项：图标 + 文字，选中时左侧有一条高亮。"""

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hover = False
        self._selected_color = theme.BLUE

    def set_accent(self, color: str) -> None:
        self._selected_color = color
        self.update()

    def enterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        selected = self.isChecked()
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 16))
            painter.drawRoundedRect(QRectF(8, 2, self.width() - 16, self.height() - 4), 10, 10)
            painter.setBrush(QColor(self._selected_color))
            painter.drawRoundedRect(QRectF(0, 12, 3, self.height() - 24), 1.5, 1.5)
        elif self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 8))
            painter.drawRoundedRect(QRectF(8, 2, self.width() - 16, self.height() - 4), 10, 10)

        color = theme.TEXT if selected else theme.MUTED
        painter.drawPixmap(20, (self.height() - 18) // 2, theme.pixmap(self._icon_name, color, 18))
        font = QFont(self.font())
        font.setPointSize(10)
        font.setBold(selected)
        painter.setFont(font)
        painter.setPen(QColor(color))
        painter.drawText(QRectF(48, 0, self.width() - 56, self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        painter.end()


class StatusOrb(QWidget):
    """会呼吸的状态圆球 —— 主页上唯一的主角。

    三层叠出来：最外是缓慢扩散的光环，中间是常驻的柔光，里面是渐变实体球。
    颜色、脉动速度、光环层数都跟着状态走，不用文字也知道它在干什么。
    """

    clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, diameter: int = 240) -> None:
        super().__init__(parent)
        self._diameter = diameter
        self._state = "off"
        self._phase = 0.0
        self._level = 0.0
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点一下可以打断当前任务")
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state if state in theme.STATE_COLORS else "off"
            self.update()

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level) * 6.0))

    def refresh_theme(self) -> None:
        """换主色后重画 —— 待命态没有动画在推，不主动重绘就一直是旧颜色。"""
        self.update()

    def _tick(self) -> None:
        speed = {"listen": 1.5, "think": 2.4, "speak": 1.9, "idle": 0.45}.get(self._state, 0.0)
        if speed:
            self._phase = (self._phase + speed) % 100.0
            self.update()
        elif self._level > 0.01:
            self.update()

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = min(self.width(), self.height())
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        base = theme.STATE_COLORS.get(self._state, theme.DIM)
        core = size * 0.175

        # 1) 常驻柔光：让球体"发光"而不是贴上去
        glow_radius = size * 0.46
        glow = QRadialGradient(center, glow_radius)
        glow.setColorAt(0.0, theme.blend(base, theme.BG, 0.55))
        glow.setColorAt(0.55, theme.blend(base, theme.BG, 0.86))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, glow_radius, glow_radius)

        # 2) 扩散的光环
        rings = {"listen": 3, "think": 3, "speak": 2, "idle": 1}.get(self._state, 0)
        for index in range(rings):
            progress = ((self._phase / 100.0) + index / max(1, rings)) % 1.0
            radius = core * 1.25 + progress * (size * 0.30)
            alpha = int(120 * (1.0 - progress) ** 1.7)
            if alpha <= 3:
                continue
            pen = QColor(base)
            pen.setAlpha(alpha)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(center, radius, radius)

        # 3) 球体本体
        radius = core * (1.0 + self._level * 0.30)
        if self._state == "idle":
            radius *= 1.0 + 0.012 * (1 if int(self._phase * 2) % 2 == 0 else -1)
        body = QRadialGradient(QPointF(center.x(), center.y() - radius * 0.35), radius * 1.5)
        body.setColorAt(0.0, theme.blend(base, "#FFFFFF", 0.45))
        body.setColorAt(0.5, QColor(base))
        body.setColorAt(1.0, theme.blend(base, "#000000", 0.5))
        painter.setBrush(body)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, radius, radius)
        painter.end()


class ListRow(QFrame):
    """卡片里的一行：左边图标 + 标题/副标题，右边放控件。"""

    def __init__(self, icon_name: str = "", title: str = "", subtitle: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 背景交给全局 QSS 的 #ListRow:hover（带一点主色），这里只留内边距
        self.setObjectName("ListRow")
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(12)

        if icon_name:
            badge = QLabel(self)
            badge.setFixedSize(30, 30)
            badge.setPixmap(theme.pixmap(icon_name, theme.TEXT, 17))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            # 交给全局 QSS 的 #Badge —— 那样换主色时它会自己跟着变，
            # 不用挨个去改内联样式
            badge.setObjectName("Badge")
            row.addWidget(badge)
        else:
            row.addSpacing(0)

        text = QVBoxLayout()
        text.setSpacing(1)
        self.title = QLabel(title, self)
        self.title.setObjectName("RowTitle")
        text.addWidget(self.title)
        self.subtitle = QLabel(subtitle, self)
        self.subtitle.setObjectName("RowSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        text.addWidget(self.subtitle)
        row.addLayout(text, 1)
        self.row = row

    def add_trailing(self, widget: QWidget) -> None:
        self.row.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


def section_title(text: str) -> QWidget:
    """小节标题：左边一小段主色竖条 + 文字。

    竖条用主色，换主题时整页的"分节感"一起变色 —— 比只换按钮显眼得多。
    """
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 10, 0, 2)
    layout.setSpacing(8)
    bar = QFrame()
    bar.setObjectName("SectionBar")
    bar.setFixedSize(3, 12)
    layout.addWidget(bar)
    label = QLabel(text)
    label.setObjectName("SectionTitle")
    layout.addWidget(label)
    layout.addStretch(1)
    return box


def icon_button(icon_name: str, tooltip: str = "", color: str = theme.MUTED,
                size: int = 32) -> QPushButton:
    """只有图标的圆形按钮。"""
    button = QPushButton()
    button.setIcon(theme.icon(icon_name, color, 16))
    button.setIconSize(QSize(16, 16))
    button.setFixedSize(size, size)
    button.setToolTip(tooltip)
    button.setStyleSheet(
        "QPushButton { background: rgba(255,255,255,0.07); border-radius: "
        + str(size // 2) + "px; } QPushButton:hover { background: rgba(255,255,255,0.14); }"
    )
    return button


def primary_button(text: str, icon_name: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("Primary")
    if icon_name:
        button.setIcon(theme.icon(icon_name, "#FFFFFF", 16))
        button.setIconSize(QSize(16, 16))
    button.setMinimumHeight(34)
    return button


def plain_button(text: str, icon_name: str = "", kind: str = "") -> QPushButton:
    button = QPushButton(text)
    if kind:
        button.setObjectName(kind)
    if icon_name:
        button.setIcon(theme.icon(icon_name, theme.BLUE if kind == "Ghost" else theme.TEXT, 16))
        button.setIconSize(QSize(16, 16))
    button.setMinimumHeight(32)
    return button


# ─────────────────────── 无边框窗口用的零件 ───────────────────────


class RoundedPanel(QWidget):
    """圆角面板：无边框窗口的"脸"。

    系统标题栏去掉之后，圆角、描边、底色都得自己画 —— 这样窗口才像一张
    浮在桌面上的卡片，而不是一个方方正正的框。
    """

    def __init__(self, parent: QWidget | None = None, radius: int = 18,
                 color: str = theme.BG, border: str = theme.SEPARATOR,
                 top: str = "") -> None:
        super().__init__(parent)
        self._radius = radius
        self._color = QColor(color)
        self._border = QColor(border)
        # 顶部颜色：不是纯色而是从上到下的渐变。掺一点主色，
        # 换主题时整块窗口的色调都会变 —— 光靠描边那一条线太不明显了。
        self._top = QColor(top or color)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

    def set_colors(self, color: str, border: str, top: str = "") -> None:
        self._color = QColor(color)
        self._border = QColor(border)
        self._top = QColor(top or color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        brush = self._color
        if self._top != self._color:
            gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            gradient.setColorAt(0.0, self._top)
            gradient.setColorAt(0.45, self._color)
            gradient.setColorAt(1.0, self._color)
            brush = gradient
        painter.setPen(self._border)
        painter.setBrush(brush)
        painter.drawRoundedRect(rect, self._radius, self._radius)
        painter.end()


class WindowButton(QAbstractButton):
    """标题栏上的小圆点按钮（最小化 / 关闭）。

    平时几乎隐形，鼠标移到窗口上才浮出来 —— 这样默认界面上就只剩圆球和状态。
    """

    def __init__(self, icon_name: str, tooltip: str = "", danger: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._icon_name = icon_name
        self._danger = danger
        self._hover = False
        self.setFixedSize(26, 26)
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001, N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def refresh_theme(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._hover:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 69, 58, 200) if self._danger
                             else QColor(255, 255, 255, 26))
            painter.drawEllipse(QRectF(0, 0, self.width(), self.height()))
        color = "#FFFFFF" if (self._hover and self._danger) else theme.MUTED
        size = 13
        painter.drawPixmap((self.width() - size) // 2, (self.height() - size) // 2,
                           theme.pixmap(self._icon_name, color, size))
        painter.end()


def fade_in(widget: QWidget, duration: int = 180) -> None:
    """淡入。Qt 没有内建的过渡，窗口类自己做一个。"""
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    animation = QPropertyAnimation(effect, b"opacity", widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)
    animation.finished.connect(lambda: widget.setGraphicsEffect(None))
    animation.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
    widget._fade_animation = animation  # 防止被 GC 掉


class FadeLabel(QLabel):
    """文字切换时淡入淡出的标签（状态文字用它，换字不会"啪"地跳一下）。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(1.0)
        self.setGraphicsEffect(self._effect)
        self._animation = QPropertyAnimation(self._effect, b"opacity", self)
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pending = ""

    def setText(self, text: str) -> None:  # noqa: N802
        if text == self.text():
            return
        self._pending = text
        self._animation.stop()
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        try:
            self._animation.finished.disconnect()
        except TypeError:
            pass
        self._animation.finished.connect(self._swap)
        self._animation.start()

    def _swap(self) -> None:
        super().setText(self._pending)
        self._animation.stop()
        try:
            self._animation.finished.disconnect()
        except TypeError:
            pass
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()


# ─────────────────────── 聊天气泡 ───────────────────────


class ChatBubble(QFrame):
    """一条对话气泡。

    为什么不用 QPlainTextEdit 拼 HTML：它本质是纯文本编辑器，insertHtml 只能
    尽力而为 —— <div> 会被拍平，于是几十条消息糊成一坨，读起来非常糟。
    这里改成"一个气泡一个控件"，圆角、换行、对齐全由 QSS 控制。
    """

    def __init__(self, role: str, text: str, stamp: str = "",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = str(role)
        mine = self.role == "user"
        system = self.role == "system"

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        card = QFrame()
        card.setObjectName("BubbleSystem" if system else
                           ("BubbleMine" if mine else "BubbleTheirs"))
        card.setMaximumWidth(430)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(12, 8, 12, 10)
        inner.setSpacing(3)

        who = {"user": "我", "assistant": "大肥鲸"}.get(self.role, "系统")
        head = QLabel(who + ("　" + stamp if stamp else ""))
        head.setObjectName("BubbleWho")
        inner.addWidget(head)

        body = QLabel(str(text))
        body.setObjectName("BubbleText")
        body.setWordWrap(True)
        # 纯文本：指令里的 < & 原样显示，换行也不会被吃掉
        body.setTextFormat(Qt.TextFormat.PlainText)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        inner.addWidget(body)

        if system:
            outer.addStretch(1)
            outer.addWidget(card)
            outer.addStretch(1)
        elif mine:
            outer.addStretch(1)
            outer.addWidget(card)
        else:
            outer.addWidget(card)
            outer.addStretch(1)


# ─────────────────────── 音量条 ───────────────────────


class VolumeSlider(QWidget):
    """输出音量：一条滑杆 + 百分比 + 静音。

    交互上分两步：拖动过程中只发 preview（引擎里立刻改总增益，声音跟着变），
    松手才发 committed（这时才写配置文件）—— 否则拖一次要重写几十遍 config.yaml。
    """

    preview = pyqtSignal(int)
    committed = pyqtSignal(int)

    def __init__(self, percent: int = 100, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._last_positive = max(1, int(percent))
        self._muted = int(percent) <= 0

        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        self.btn_mute = icon_button("volume", "静音", theme.MUTED, 30)
        self.btn_mute.clicked.connect(self._toggle_mute)
        box.addWidget(self.btn_mute)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 150)
        self.slider.setValue(max(0, min(int(percent), 150)))
        self.slider.setFixedWidth(150)
        self.slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.slider.valueChanged.connect(self._on_change)
        self.slider.sliderReleased.connect(self._on_release)
        box.addWidget(self.slider)

        self.value_label = QLabel(str(self.slider.value()) + "%")
        self.value_label.setObjectName("Value")
        self.value_label.setFixedWidth(42)
        box.addWidget(self.value_label)
        box.addStretch(1)
        self._refresh()

    # ── 对外 ──
    def value(self) -> int:
        return int(self.slider.value())

    def set_value(self, percent: int, notify: bool = False) -> None:
        """从配置同步过来时不发信号，免得又写回配置文件。"""
        value = max(0, min(int(percent), 150))
        if value == self.slider.value():
            self._refresh()
            return
        blocked = self.slider.blockSignals(not notify)
        self.slider.setValue(value)
        self.slider.blockSignals(blocked)
        if value > 0:
            self._last_positive = value
        self._muted = value <= 0
        self._refresh()

    def refresh_theme(self) -> None:
        self.btn_mute.setIcon(theme.icon(self._icon_name(), theme.MUTED, 16))
        self._refresh()

    # ── 内部 ──
    def _icon_name(self) -> str:
        if self._muted or self.slider.value() <= 0:
            return "mute"
        return "volume" if self.slider.value() <= 90 else "volumehigh"

    def _refresh(self) -> None:
        self.value_label.setText("静音" if self.slider.value() <= 0
                                 else str(self.slider.value()) + "%")
        self.btn_mute.setIcon(theme.icon(self._icon_name(), theme.MUTED, 16))
        self.btn_mute.setToolTip("取消静音" if self._muted else "静音")

    def _on_change(self, value: int) -> None:
        if value > 0:
            self._last_positive = value
            self._muted = False
        self._refresh()
        self.preview.emit(int(value))
        if not self.slider.isSliderDown():
            self.committed.emit(int(value))

    def _on_release(self) -> None:
        self.committed.emit(self.value())

    def _toggle_mute(self) -> None:
        if self.slider.value() > 0:
            self._last_positive = self.slider.value()
            self.slider.setValue(0)
        else:
            self.slider.setValue(self._last_positive or 100)


# ─────────────────────── 主题色 ───────────────────────


class AccentDot(QAbstractButton):
    """一个圆形色块，选中的那个外面套一圈。"""

    def __init__(self, color: str, parent: QWidget | None = None, size: int = 26) -> None:
        super().__init__(parent)
        self.color = color
        self._selected = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, value: bool) -> None:
        if self._selected != value:
            self._selected = value
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self._selected:
            pen = painter.pen()
            pen.setColor(QColor(theme.TEXT))
            pen.setWidthF(2.0)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
            rect = rect.adjusted(4.0, 4.0, -4.0, -4.0)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self.color))
        painter.drawEllipse(rect)


class AccentPicker(QWidget):
    """一排预设色 + 一个自定义（打开取色盘）。选中即发 picked。"""

    picked = pyqtSignal(str)

    def __init__(self, current: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from ..config import ACCENT_PRESETS  # noqa: PLC0415

        self._current = (current or "").lower()
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._dots: list[AccentDot] = []
        for name, color in ACCENT_PRESETS.items():
            dot = AccentDot(color)
            dot.setToolTip(name)
            dot.clicked.connect(lambda _c=False, n=name: self.picked.emit(n))
            box.addWidget(dot)
            self._dots.append(dot)

        self._custom = AccentDot(theme.CARD_ACTIVE)
        self._custom.setToolTip("自定义颜色…")
        self._custom.clicked.connect(self._pick_custom)
        box.addWidget(self._custom)
        box.addStretch(1)
        self.set_current(current)

    def set_current(self, value: str) -> None:
        """把选中的圈套在对应的点上；自定义色就用最后一个点显示。"""
        from ..config import ACCENT_PRESETS, resolve_accent  # noqa: PLC0415

        raw = (value or "").lower()
        self._current = raw
        hexed = resolve_accent(raw).lower()
        # 先定「谁被选中」：预设名 > 颜色值撞上某个预设 > 自定义
        if not raw:
            chosen = "blue"
        elif raw in ACCENT_PRESETS:
            chosen = raw
        else:
            chosen = next((name for name, color in ACCENT_PRESETS.items()
                           if _same_hex(color, hexed)), "")
        for name, dot in zip(ACCENT_PRESETS.keys(), self._dots):
            dot.set_selected(name == chosen)
        if not chosen:
            self._custom.color = hexed
        self._custom.set_selected(not chosen)
        self._custom.update()

    def _pick_custom(self) -> None:
        from ..config import resolve_accent  # noqa: PLC0415

        color = QColorDialog.getColor(QColor(resolve_accent(self._current)), self, "选一个主色")
        if color.isValid():
            self.picked.emit(color.name())


def _same_hex(a: str, b: str) -> bool:
    return str(a).lower().lstrip("#") == str(b).lower().lstrip("#")


# ─────────────────────── 提示条 ───────────────────────


class NoticeBar(QFrame):
    """一条能收能放的提示（主界面用它提醒"有设置要重启才生效"）。"""

    action = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setVisible(False)

        box = QHBoxLayout(self)
        box.setContentsMargins(12, 8, 8, 8)
        box.setSpacing(10)

        self.icon = QLabel()
        self.icon.setPixmap(theme.pixmap("refresh", theme.ACCENT, 18))
        box.addWidget(self.icon)

        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(1)
        self.title = QLabel("")
        self.title.setObjectName("BannerText")
        self.hint = QLabel("")
        self.hint.setObjectName("BannerHint")
        self.hint.setWordWrap(True)
        texts.addWidget(self.title)
        texts.addWidget(self.hint)
        box.addLayout(texts, 1)

        self.button = plain_button("立即重启", "refresh", "Banner")
        self.button.clicked.connect(self.action.emit)
        box.addWidget(self.button)

        # 淡入淡出 + 高度一起动：只淡入的话，下面的圆球会"啪"地跳一下
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._grow = QPropertyAnimation(self, b"maximumHeight", self)
        self._grow.setDuration(220)
        self._grow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._grow.finished.connect(self._after_grow)
        self.setMaximumHeight(0)

    def refresh_theme(self) -> None:
        self.icon.setPixmap(theme.pixmap("refresh", theme.ACCENT, 18))

    def show_message(self, title: str, hint: str = "") -> None:
        if self.title.text() != title:
            self.title.setText(title)
        if self.hint.text() != hint:
            self.hint.setText(hint)
        if not self.isHidden() and self._effect.opacity() > 0.9:
            return
        # hide_bar 会把 _after_hide 挂到动画的 finished 上；那次连接如果没断，
        # 这里刚淡入进来、200ms 后就被它一句 setVisible(False) 藏掉，
        # 而每 280ms 一次的刷新又把它显示出来 —— 提示条于是不停地闪，
        # 字根本读不完。显示之前先摘掉那个连接。
        try:
            self._anim.finished.disconnect(self._after_hide)
        except TypeError:
            pass
        if self.isHidden():
            self.setMaximumHeight(0)
            self.setVisible(True)
            self._grow.stop()
            self._grow.setStartValue(0)
            self._grow.setEndValue(max(44, self.sizeHint().height()))
            self._grow.start()
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _after_grow(self) -> None:
        # 动画结束后放开限制，窗口变窄导致文字换行时还能自己长高
        if not self.isHidden():
            self.setMaximumHeight(16777215)

    def _after_hide(self) -> None:
        """淡出结束才真的藏起来（提前藏会让动画看不见）。"""
        self.setVisible(False)

    def hide_bar(self) -> None:
        if self.isHidden():
            return
        self._grow.stop()
        self.setMaximumHeight(16777215)
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        try:
            self._anim.finished.disconnect()
        except TypeError:
            pass
        self._anim.finished.connect(self._after_hide)
        self._anim.start()

    def set_busy(self, busy: bool) -> None:
        self.button.setEnabled(not busy)
        self.button.setText("正在重启…" if busy else "立即重启")

