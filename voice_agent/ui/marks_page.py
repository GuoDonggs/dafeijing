# -*- coding: utf-8 -*-
"""屏幕标记页：看一眼有哪些框和点，改名字、调坐标、删掉，或者现场再框一个。

为什么值得单独一页：标记是**语音指代**的凭据（"点它"说的是哪个"它"），
看不见就会用错。这里把每个标记摊开 —— 名字、类型、坐标、备注都能改，
改完立刻落盘（build/marks.json），下次开机还在。

右上角还有一个**显示开关**：把范围和点藏起来（不是删除）。框了一堆东西之后
嫌挡视线时用它，藏起来期间名字照样能引用 —— 「点点1」「看看范围1」都还有效，
只是屏幕上不画。语音也能使唤（show_marks 工具），按钮会跟着状态变。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..console import Console
from ..marks import store
from . import components as ui
from . import theme
from .pages import Page, page_header, scroll_page


class MarkRow(QWidget):
    """一个标记：名字 / 坐标 / 备注都可编辑，右边是删除。"""

    def __init__(self, mark, page: "MarksPage") -> None:  # noqa: ANN001
        super().__init__()
        self.mark = mark
        self.page = page
        self._built_name = mark.name
        box = ui.Card()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(box)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.name = QLineEdit(mark.name)
        self.name.setFixedWidth(130)
        self.name.setToolTip("改个更好记的名字也行（不能重名）")
        head.addWidget(self.name)
        kind = ui.Pill("范围" if mark.kind == "region" else "点")
        kind.set_color(theme.ACCENT if mark.kind == "region" else theme.GREEN)
        head.addWidget(kind)
        head.addStretch(1)
        if mark.kind == "region":
            capture = ui.plain_button("截这块")
            capture.setToolTip("把这块存成图片，存到图片文件夹")
            capture.clicked.connect(lambda: self.page.capture(mark))
            head.addWidget(capture)
            locate = ui.plain_button("闪一下")
            locate.setToolTip("在屏幕上闪一下这个位置")
            locate.clicked.connect(lambda: self.page.flash(mark))
            head.addWidget(locate)
        remove = ui.plain_button("删除")
        remove.clicked.connect(lambda: self.page.remove(mark))
        head.addWidget(remove)
        box.body.addLayout(head)

        coords = QHBoxLayout()
        coords.setSpacing(6)
        self.fields: list[QLineEdit] = []
        labels = ("左", "上", "右", "下") if mark.kind == "region" else ("x", "y")
        values = (mark.rect if mark.kind == "region" else (mark.x1, mark.y1))
        for label, value in zip(labels, values):
            caption = QLabel(label)
            caption.setObjectName("RowSubtitle")
            coords.addWidget(caption)
            editor = QLineEdit(str(value))
            editor.setFixedWidth(64)
            coords.addWidget(editor)
            self.fields.append(editor)
        coords.addStretch(1)
        box.body.addLayout(coords)

        note = QLineEdit(mark.note)
        note.setPlaceholderText("备注：这块是什么，例如「下载按钮」")
        box.body.addWidget(note)
        self.note = note

        self.hint = QLabel("")
        self.hint.setObjectName("RowSubtitle")
        self.hint.setWordWrap(True)
        box.body.addWidget(self.hint)

        apply_button = ui.primary_button("保存改动")
        apply_button.clicked.connect(self.apply)
        row = QHBoxLayout()
        row.addWidget(apply_button)
        row.addStretch(1)
        box.body.addLayout(row)
        for editor in [self.name, *self.fields, note]:
            editor.editingFinished.connect(self.apply)

    def apply(self) -> None:
        """把这一行的改动写回仓库（改名 + 坐标 + 备注）。"""
        name = self.name.text().strip()
        if name != self._built_name:
            ok, why = store.rename(self._built_name, name)
            if not ok:
                self.hint.setText(why)
                self.name.setText(self._built_name)
                return
            self._built_name = name
            self.mark = store.get(name) or self.mark
        numbers: list[int] = []
        for editor in self.fields:
            try:
                numbers.append(int(editor.text().strip()))
            except ValueError:
                self.hint.setText("坐标得是整数")
                return
        if self.mark.kind == "region" and len(numbers) == 4:
            store.add_or_update("region", numbers[0], numbers[1], numbers[2], numbers[3],
                                name=self._built_name, note=self.note.text())
        elif self.mark.kind == "point" and len(numbers) == 2:
            store.add_or_update("point", numbers[0], numbers[1],
                                name=self._built_name, note=self.note.text())
        self.hint.setText("已保存")
        self.page.reload()


class MarksPage(Page):
    """屏幕标记管理页。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(page_header(
            "屏幕标记",
            "框一块叫「范围N」、点一下叫「点N」；之后说「看看范围1」「点它」就能引用"))
        head.addStretch(1)
        region = ui.primary_button("框选范围", "plus")
        region.clicked.connect(lambda: self.add("region"))
        point = ui.plain_button("标记点", "plus")
        point.clicked.connect(lambda: self.add("point"))
        # 显示开关：**不是删除**，只是屏幕上画不画。框完一堆东西嫌挡视线时用，
        # 藏起来期间名字照样能引用（「点点1」「看看范围1」照旧有效）。
        self.visible_button = ui.plain_button("隐藏标记", "eyeoff")
        self.visible_button.setToolTip(
            "把屏幕上的范围和点藏起来（不删除，名字照样能用）")
        self.visible_button.clicked.connect(self.toggle_visible)
        wipe = ui.plain_button("全部擦掉")
        wipe.clicked.connect(self.clear)
        for widget in (region, point, self.visible_button, wipe):
            head.addWidget(widget, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(head)

        self.visible_hint = QLabel("")
        self.visible_hint.setObjectName("Hint")
        layout.addWidget(self.visible_hint)

        self.area, self.list_layout = scroll_page()
        layout.addWidget(self.area, 1)
        self.empty = QLabel("还没有标记。点上面的「框选范围」，然后在屏幕上拖一个框。")
        self.empty.setObjectName("Hint")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.list_layout.insertWidget(0, self.empty)
        self._version = -1
        self._visible_shown = None
        self._rows: list[MarkRow] = []
        self.reload()

    # ── 数据 ──
    def on_tick(self, _snapshot: dict, _state: str) -> None:
        self.reload()

    def reload(self) -> None:
        if store.version == self._version:
            return
        self._version = store.version
        self._sync_visible()
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        marks = store.all()
        self.empty.setVisible(not marks)
        for mark in marks:
            row = MarkRow(mark, self)
            self.list_layout.addWidget(row)
            self._rows.append(row)

    def _sync_visible(self) -> None:
        """按钮文字和图标跟着**真实状态**走。

        必须每轮刷新、不能只在点按钮时改：模型也能调这个开关（show_marks 工具），
        语音说一句「把标记藏起来」，这里的按钮就得跟着变成「显示标记」，
        否则界面在说谎（用户点一下反而把它显示回来了）。
        """
        visible = store.visible
        if visible == self._visible_shown:
            return
        self._visible_shown = visible
        self.visible_button.setText("隐藏标记" if visible else "显示标记")
        self.visible_button.setIcon(theme.icon(
            "eyeoff" if visible else "eye", theme.TEXT, 16))
        self.visible_button.setToolTip(
            "把屏幕上的范围和点藏起来（不删除，名字照样能用）" if visible
            else "把藏起来的标记画回屏幕上")
        count = len(store.all())
        if visible:
            self.visible_hint.setText("")
        else:
            self.visible_hint.setText(
                "标记已经藏起来了（" + str(count) + " 个还在，只是屏幕上不画）——"
                "「点点1」「看看范围1」照样有效，想画回来就说「显示标记」或者点上面的按钮。")

    def toggle_visible(self) -> None:
        """切换标记的显示开关（不删任何东西）。"""
        visible = store.toggle_visible()
        self.console.log("[ui] 标记已" + ("显示" if visible else "隐藏")
                         + "（共 " + str(len(store.all())) + " 个，没有删除任何东西）")
        self._sync_visible()
        self.reload()

    # ── 操作 ──
    def main_window(self):
        """往上找主窗口。

        页面是装在独立窗口（PageDialog）里的，所以 self.window() 拿到的是那个
        对话框 —— 它没有 start_marks。得顺着 parent 链一路往上找主面板。
        （以前就是直接拿 self.window()，于是点「框选范围」只会写一行
        「这个窗口打不开框选模式」。）
        """
        node = self.parent()
        while node is not None:
            if hasattr(node, "start_marks"):
                return node
            node = node.parent()
        return None

    def add(self, kind: str) -> None:
        window = self.main_window()
        if window is None:
            self.console.log("[ui] 找不到主窗口，开不了框选模式")
            return
        # 框选的时候把这个窗口藏起来：挡着屏幕就框不准
        host = self.window()
        was_visible = host.isVisible()
        if was_visible:
            host.hide()
        try:
            window.start_marks(kind)
        finally:
            if was_visible:
                host.show()

    def remove(self, mark) -> None:  # noqa: ANN001
        store.remove(mark.name)
        self.console.log("[ui] 删掉了「" + mark.name + "」")
        self.reload()

    def clear(self) -> None:
        count = store.clear()
        self.console.log("[ui] 擦掉了 " + str(count) + " 个标记" if count else "[ui] 本来就没有标记")
        self.reload()

    def flash(self, mark) -> None:  # noqa: ANN001
        """在屏幕上闪一下某个标记（告诉用户"就是这个"）。"""
        window = self.main_window()
        ensure = getattr(window, "_ensure_overlay", None) if window else None
        if ensure is None:
            self.console.log("[ui] " + mark.summary())
            return
        overlay = ensure()
        overlay.show_overlay()
        overlay.flash(mark.name)
        self.console.log("[ui] " + mark.summary())

    def capture(self, mark) -> None:  # noqa: ANN001
        """把这块存成图片 —— 存下来就能当"找图"的模板用。"""
        from .. import tools  # noqa: PLC0415

        answer = tools.call("screenshot", {"region": mark.name, "name": mark.name})
        self.console.log("[ui] " + str(answer))


