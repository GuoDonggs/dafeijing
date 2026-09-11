# -*- coding: utf-8 -*-
"""七个页面：主页、对话、工具、技能、设置、设备、关于。

每个页面只做两件事：把自己的状态画出来、把用户的操作转成 Console 调用。
业务逻辑一律不在界面里 —— 这样换界面不用碰引擎，换引擎也不用碰界面。
"""

from __future__ import annotations

import json
import time
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..console import Console
from . import components as ui
from . import theme


# ─────────────────────────── 公共小工具 ───────────────────────────


def scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    """返回一个可滚动页面和它的内容布局。长表单都用这个。"""
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    inner = QWidget()
    layout = QVBoxLayout(inner)
    layout.setContentsMargins(4, 4, 12, 24)
    layout.setSpacing(theme.GAP)
    area.setWidget(inner)
    return area, layout


def page_header(title: str, subtitle: str = "") -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 8)
    layout.setSpacing(2)
    label = QLabel(title)
    label.setObjectName("PageTitle")
    layout.addWidget(label)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("PageSubtitle")
        layout.addWidget(sub)
    return box


def setting_row(title: str, subtitle: str = "", control: QWidget | None = None) -> ui.ListRow:
    row = ui.ListRow("", title, subtitle)
    if control is not None:
        row.add_trailing(control)
    return row


class Page(QWidget):
    """所有页面的基类：统一背景，并提供一个「被切到前台时」的回调。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.console = console

    def on_show(self) -> None:
        """切到这一页时调用（默认什么都不做）。"""

    def on_tick(self, snapshot: dict, state: str) -> None:
        """主循环每 280ms 调一次，页面按需刷新。"""

    def apply_theme(self) -> None:
        """换了主题主色之后重建配色相关的部分。

        图标是"按颜色渲染好再缓存"的，只 setStyleSheet 不会让已有图标变色，
        所以页面自己得把用主色画过的东西重做一遍（默认什么都不用做）。
        """


# ─────────────────────────────── 主页 ───────────────────────────────


class HomePage(Page):
    """主界面：一个会呼吸的圆球 + 状态，别的什么都没有。"""

    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        # 主面板是竖长方形（384 宽），留白要小一点，圆球也相应缩小
        layout.setContentsMargins(8, 4, 8, 8)
        layout.setSpacing(0)

        layout.addStretch(2)
        self.orb = ui.StatusOrb(self, diameter=196)
        self.orb.clicked.connect(self.cancel_requested.emit)
        layout.addWidget(self.orb, 0, Qt.AlignmentFlag.AlignHCenter)

        self._state = "off"
        self.state_label = ui.FadeLabel("未启动")
        font = QFont()
        font.setPointSize(21)
        font.setWeight(QFont.Weight.DemiBold)
        self.state_label.setFont(font)
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(14)
        layout.addWidget(self.state_label)

        self.hint_label = QLabel(theme.STATE_HINTS["off"])
        self.hint_label.setObjectName("Hint")
        self.hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint_label.setWordWrap(True)
        layout.addSpacing(6)
        layout.addWidget(self.hint_label)

        layout.addSpacing(22)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.btn_start = ui.primary_button("启动监听", "play")
        self.btn_start.clicked.connect(self.start_requested.emit)
        self.btn_stop = ui.plain_button("停止", "stop")
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_cancel = ui.plain_button("打断", "hand")
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)
        for button in (self.btn_start, self.btn_stop, self.btn_cancel):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        layout.addStretch(3)

        # 底部速览：竖长方形放不下四列，改成两行两列
        grid = QGridLayout()
        grid.setSpacing(6)
        grid.setContentsMargins(0, 0, 0, 4)
        self.info_labels: dict[str, ui.Pill] = {}
        for index, key in enumerate(("唤醒", "算力", "合成", "声纹")):
            pill = ui.Pill("—")
            pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.info_labels[key] = pill
            grid.addWidget(pill, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

    def apply_theme(self) -> None:
        # 圆球在待命/未启动时没有动画在推，不主动重绘就还是旧颜色；
        # 速览胶囊的颜色每 280ms 会被 on_tick 按实际状态重刷，不用管
        self.orb.refresh_theme()
        self.state_label.setStyleSheet(
            "color: " + theme.STATE_COLORS.get(self._state, theme.TEXT))

    def on_tick(self, snapshot: dict, state: str) -> None:
        status = snapshot.get("status", {})
        starting = bool(snapshot.get("starting"))
        self._state = state
        self.orb.set_state(state)
        self.orb.set_level(float(status.get("mic_level") or 0.0))

        if starting:
            text, hint = "正在加载模型…", "第一次启动要几秒钟，之后就快了"
        else:
            text = theme.STATE_LABELS.get(state, "未启动")
            if state != "off" and status.get("state_text"):
                text = str(status["state_text"])
            hint = self._hint(snapshot, status, state)
        if self.state_label.text() != text:
            self.state_label.setText(text)
        self.state_label.setStyleSheet("color: " + theme.STATE_COLORS.get(state, theme.TEXT))
        if self.hint_label.text() != hint:
            self.hint_label.setText(hint)

        running = bool(status.get("running"))
        self.btn_start.setEnabled(not running and not starting)
        self.btn_stop.setEnabled(running)
        self.btn_cancel.setEnabled(running)

        # 底部速览可以在设置里关掉，小屏或想要更清爽时用
        show_stats = bool(getattr(self.console.cfg.ui, "show_stats", True))
        for pill in self.info_labels.values():
            if pill.isVisible() != show_stats:
                pill.setVisible(show_stats)

        words = status.get("wake_words") or []
        voice = status.get("voiceprint") or {}
        self.info_labels["唤醒"].setText("唤醒 " + (words[0] if words else "—"))
        self.info_labels["算力"].setText("算力 " + str(status.get("provider_text") or "CPU"))
        self.info_labels["合成"].setText("合成 " + str(status.get("tts_engine") or "vits"))
        if voice.get("enabled"):
            self.info_labels["声纹"].setText("声纹 " + ("已启用" if voice.get("ready") else "待录制"))
            self.info_labels["声纹"].set_color(theme.GREEN if voice.get("ready") else theme.ORANGE)
        else:
            self.info_labels["声纹"].setText("声纹 关闭")
            self.info_labels["声纹"].set_color(theme.MUTED)

    @staticmethod
    def _hint(snapshot: dict, status: dict, state: str) -> str:
        missing = snapshot.get("missing_models") or []
        if snapshot.get("last_error"):
            return "启动失败：" + str(snapshot["last_error"])
        if missing:
            return "缺少模型：" + "、".join(missing) + "（运行 python scripts/download_models.py）"
        if state == "off":
            return theme.STATE_HINTS["off"]
        if state == "listen":
            seconds = int((status.get("listen_timeout_ms") or 8000) / 1000)
            return "说指令就好，" + str(seconds) + " 秒内没有提问我会回到待命"
        if state == "think":
            return "正在处理，随时可以喊唤醒词打断"
        if state == "speak":
            return "点一下圆球可以打断播报"
        words = status.get("wake_words") or []
        voice = status.get("voiceprint") or {}
        extra = "（已开启声纹，只认你的声音）" if voice.get("ready") else ""
        return "喊「" + (words[0] if words else "唤醒词") + "」叫我" + extra


# ─────────────────────────── 对话与指令 ───────────────────────────


class ChatPage(Page):
    """一条时间线：语音说的、界面敲的，都在这里，也都在这里发指令。

    以前"对话记录"和"文字指令"是两个菜单项，点开来却是同一个页面 —— 合并掉。
    """

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("对话与指令", "语音说过的、界面上敲过的，都在这里"))

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self.timeline = QVBoxLayout(holder)
        self.timeline.setContentsMargins(2, 2, 10, 12)
        self.timeline.setSpacing(10)
        self.timeline.addStretch(1)
        self.area.setWidget(holder)
        layout.addWidget(self.area, 1)

        self.placeholder = QLabel("还没有对话。在下面输入一句，或者喊一声唤醒词。")
        self.placeholder.setObjectName("Hint")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeline.insertWidget(0, self.placeholder)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("输入指令，回车派发（例如：看看 C 盘还剩多少空间）")
        self.entry.returnPressed.connect(self.send)
        row.addWidget(self.entry, 1)
        self.btn_send = ui.primary_button("派发")
        self.btn_send.clicked.connect(self.send)
        row.addWidget(self.btn_send)
        self.btn_say = ui.plain_button("只播报")
        self.btn_say.clicked.connect(self.say)
        row.addWidget(self.btn_say)
        layout.addLayout(row)

        self._bubbles: list = []
        self._turns_text: list = []
        self._rendered = 0
        self._last_key = ""

    # ── 发送 ──
    def send(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        agent = self.console.ensure_agent()
        if not agent.running:
            from ..tools import CANCEL_REPLY

            reply = agent.ask(text, speak=False, confirm=self.console.web_confirm)
            if reply == CANCEL_REPLY:
                reply = "这条指令属于敏感操作，需要语音确认。先启动监听，再对着麦克风说一次。"
            self.console.log("[ui] " + reply)
        else:
            agent.dispatch(text)
        self._sync()

    def say(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        agent = self.console.ensure_agent()
        if agent.tts is None:
            agent.load()
        agent.speak(text)

    # ── 渲染 ──
    def transcript_text(self) -> str:
        """整段对话的纯文本（测试与复制用）。"""
        return "\n".join(str(t.get("text", "")) for t in self._turns_text)

    def _append(self, turn: dict) -> None:
        bubble = ui.ChatBubble(str(turn.get("role", "system")),
                               str(turn.get("text", "")),
                               str(turn.get("ts", "")))
        self.timeline.insertWidget(self.timeline.count() - 1, bubble)
        if not self._bubbles:
            self.placeholder.setVisible(False)
        self._bubbles.append(bubble)
        ui.fade_in(bubble, 160)

    def _sync(self) -> None:
        turns = (self.console.snapshot().get("status") or {}).get("transcript") or []
        self.on_tick({"status": {"transcript": turns}}, "")

    def on_tick(self, snapshot: dict, state: str) -> None:
        turns = (snapshot.get("status") or {}).get("transcript") or []
        key = self._key_of(turns[-1]) if turns else ""
        if len(turns) == self._rendered and key == self._last_key:
            return
        if len(turns) < self._rendered or (
            self._rendered and self._key_of(turns[self._rendered - 1]) != self._last_key
        ):
            for bubble in self._bubbles:
                bubble.setParent(None)
                bubble.deleteLater()
            self._bubbles.clear()
            self._turns_text.clear()
            self._rendered = 0
            self.placeholder.setVisible(True)
        for turn in turns[self._rendered:]:
            self._append(turn)
            self._turns_text.append(turn)
        self._rendered = len(turns)
        self._last_key = key
        bar = self.area.verticalScrollBar()
        bar.setValue(bar.maximum())

    @staticmethod
    def _key_of(turn: dict) -> str:
        return str(turn.get("ts", "")) + str(turn.get("text", ""))


# ─────────────────────────────── 工具 ───────────────────────────────


class ToolsPage(Page):
    """工具清单 + 试运行。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(page_header("工具", "助手能做的事。双击一行可以试运行"))
        head.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索…")
        self.search.setFixedWidth(200)
        self.search.textChanged.connect(self.render)
        head.addWidget(self.search, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(head)

        self.area, self.list_layout = scroll_page()
        layout.addWidget(self.area, 1)
        self.items: list[dict] = []
        self.reload()

    def reload(self) -> None:
        self.items = self.console.tools_payload()
        self.render()

    def render(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        keyword = self.search.text().strip().lower()
        shown = [t for t in self.items
                 if not keyword or keyword in (t["name"] + t["title"] + t["description"]).lower()]
        builtin = [t for t in shown if t["builtin"]]
        extra = [t for t in shown if not t["builtin"]]
        self.list_layout.addWidget(ui.section_title(
            "内置工具 · " + str(len(builtin)) + " 个"))
        card = ui.Card()
        for tool in builtin:
            card.body.addWidget(self._row(tool))
        self.list_layout.addWidget(card)
        if extra:
            custom = sum(1 for t in extra if t.get("source") == "tool")
            self.list_layout.addWidget(ui.section_title(
                "你自己的工具与技能 · " + str(len(extra)) + " 个"
                + ("（其中自定义工具 " + str(custom) + " 个）" if custom else "")))
            card2 = ui.Card()
            for tool in extra:
                card2.body.addWidget(self._row(tool))
            self.list_layout.addWidget(card2)
        self.list_layout.addStretch(1)

    def _row(self, tool: dict) -> QWidget:
        row = ui.ListRow("tools", tool["title"], tool["description"])
        row.title.setText(tool["title"])
        badges = QWidget()
        badges_layout = QHBoxLayout(badges)
        badges_layout.setContentsMargins(0, 0, 0, 0)
        badges_layout.setSpacing(6)
        if tool["confirm"]:
            badges_layout.addWidget(ui.Pill("需确认", theme.ORANGE))
        if tool.get("source") == "tool":
            badges_layout.addWidget(ui.Pill("自定义", theme.ACCENT))
        elif not tool["builtin"]:
            badges_layout.addWidget(ui.Pill("技能", theme.PURPLE))
        name = QLabel(tool["name"])
        name.setObjectName("Mono")
        badges_layout.addWidget(name)
        run = ui.plain_button("试运行")
        run.clicked.connect(lambda _=False, n=tool["name"]: self.run_tool(n))
        badges_layout.addWidget(run)
        row.add_trailing(badges)
        return row

    def run_tool(self, name: str) -> None:
        tool = next((t for t in self.items if t["name"] == name), None)
        args: dict[str, Any] = {}
        if tool and tool["params"]:
            dialog = JsonDialog(self, name, tool["params"])
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            args = dialog.value
        result = self.console.call_tool(name, args)
        box = QMessageBox(self)
        box.setWindowTitle("试运行结果")
        box.setText(str(result.get("result")) if result.get("ok") else str(result.get("error")))
        box.exec()


class JsonDialog(QDialog):
    """填 JSON 参数用的小窗口。"""

    def __init__(self, parent: QWidget, name: str, params: list[str]) -> None:
        super().__init__(parent)
        self.setWindowTitle("试运行 · " + name)
        self.resize(460, 300)
        self.setStyleSheet(theme.qss())
        self.value: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("参数（JSON，可以留空）："))
        self.editor = QPlainTextEdit()
        self.editor.setPlainText('{\n  "' + params[0] + '": ""\n}')
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self) -> None:
        raw = self.editor.toPlainText().strip()
        if raw:
            try:
                self.value = json.loads(raw)
            except ValueError as exc:
                box = QMessageBox(self)
                box.setWindowTitle("参数不是合法 JSON")
                box.setText(str(exc))
                box.exec()
                return
        super().accept()


# ─────────────────────────────── 技能 ───────────────────────────────


class SkillsPage(Page):
    """技能管理：列表 + 新建 / 编辑 / 删除 / 重新加载。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.addWidget(page_header(
            "技能与工具",
            "tools/ 里放自定义工具，skills/ 里放技能 —— 同一个写法，写完就有"))
        head.addStretch(1)
        new_tool = ui.primary_button("新建工具", "plus")
        new_tool.clicked.connect(lambda: self.create("tool"))
        new_skill = ui.plain_button("新建技能", "plus")
        new_skill.clicked.connect(lambda: self.create("skill"))
        reload_button = ui.plain_button("重新加载", "refresh")
        reload_button.clicked.connect(self.reload)
        for widget in (new_tool, new_skill, reload_button):
            head.addWidget(widget, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(head)

        self.area, self.list_layout = scroll_page()
        layout.addWidget(self.area, 1)
        self.items: list[dict] = []
        self.template = ""
        self.tool_template = ""
        self.reload()

    def reload(self) -> None:
        payload = self.console.skills_payload()
        self.items = payload.get("items", [])
        self.template = payload.get("template", "")
        self.tool_template = payload.get("tool_template", "")
        self.render()

    def render(self) -> None:
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not self.items:
            card = ui.Card()
            card.body.addWidget(QLabel("还没有自定义技能。点右上角「新建」写一个。"))
            self.list_layout.addWidget(card)
        else:
            card = ui.Card()
            for skill in self.items:
                card.body.addWidget(self._row(skill))
            self.list_layout.addWidget(card)
        self.list_layout.addStretch(1)

    def _row(self, skill: dict) -> QWidget:
        ok = not skill.get("error")
        title = str(skill.get("title") or skill.get("name") or "")
        subtitle = str(skill.get("error") or skill.get("description") or "（没有写描述）")
        row = ui.ListRow("tools" if skill.get("kind") == "tool" else "skills" if ok else "alert",
                         title, subtitle)
        tools_box = QWidget()
        box = QHBoxLayout(tools_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        if ok:
            box.addWidget(ui.Pill("自定义工具" if skill.get("kind") == "tool" else "技能",
                                  theme.ACCENT if skill.get("kind") == "tool" else theme.PURPLE))
        box.addWidget(ui.Pill("正常" if ok else "加载失败",
                              theme.GREEN if ok else theme.RED))
        edit = ui.plain_button("编辑")
        edit.setEnabled(ok and not str(skill.get("source", "")).endswith(".py"))
        edit.clicked.connect(lambda _=False, s=skill: self.edit(s))
        delete = ui.icon_button("trash", "删除", theme.RED, 30)
        delete.clicked.connect(lambda _=False, s=skill: self.delete(s))
        box.addWidget(edit)
        box.addWidget(delete)
        row.add_trailing(tools_box)
        return row

    def create(self, kind: str = "skill") -> None:
        if kind == "tool":
            SkillDialog(self, "新建工具", "my_tool.yaml", self.tool_template,
                        kind="tool").exec()
        else:
            SkillDialog(self, "新建技能", "my_skill.yaml", self.template).exec()
        self.reload()

    def edit(self, skill: dict) -> None:
        read = self.console.read_skill(str(skill.get("source", "")))
        if not read.get("ok"):
            self._error(str(read.get("error")))
            return
        SkillDialog(self, "编辑技能", str(read["name"]), str(read["content"])).exec()
        self.reload()

    def delete(self, skill: dict) -> None:
        path = str(skill.get("source", ""))
        box = QMessageBox(self)
        box.setWindowTitle("删除技能")
        box.setText("确定删除吗？\n" + path)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        result = self.console.delete_skill(path)
        if not result.get("ok"):
            self._error(str(result.get("error")))
        self.reload()

    def _error(self, text: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("出错了")
        box.setText(text)
        box.exec()


class SkillDialog(QDialog):
    """技能源码编辑器。"""

    def __init__(self, parent: QWidget, title: str, name: str, content: str,
                 kind: str = "skill") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 640)
        self.setStyleSheet(theme.qss())
        self.console = parent.console
        self.name = name
        self.kind = "tool" if str(kind).lower() == "tool" else "skill"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("文件名"))
        self.name_edit = QLineEdit(name)
        name_row.addWidget(self.name_edit, 1)
        layout.addLayout(name_row)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(content)
        font = QFont(theme.FONT_MONO.split(",")[0].strip('"'))
        font.setPointSize(10)
        self.editor.setFont(font)
        layout.addWidget(self.editor, 1)

        hint = QLabel("返回值会被朗读 —— 写成人话，别返回 JSON。保存后立即生效。")
        hint.setObjectName("Hint")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存并加载")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def save(self) -> None:
        result = self.console.save_skill(self.name_edit.text().strip(),
                                         self.editor.toPlainText(),
                                         kind=self.kind)
        if result.get("ok"):
            self.accept()
            return
        box = QMessageBox(self)
        box.setWindowTitle("保存失败")
        box.setText(str(result.get("error")))
        box.exec()


# ─────────────────────────────── 设置 ───────────────────────────────

#: (键, 标题, 说明, 控件类型, 可选项)
SETTING_SECTIONS: list[tuple[str, list[tuple]]] = [
    ("唤醒", [
        ("wake.keywords", "唤醒词", "多个用逗号隔开；建议 3~5 个音节", "text", None),
        ("wake.replies", "应答词", "唤醒后它先说一句，多个用逗号隔开", "text", None),
        ("wake.threshold", "灵敏度", "越低越容易唤醒，环境吵就调高", "choice",
         ["0.15", "0.25", "0.35"]),
        ("agent.listen_timeout_ms", "等待说话的时长", "超时就回到待命", "choice",
         ["5000", "8000", "15000"]),
    ]),
    ("语音与算力", [
        ("audio.output_gain", "输出音量", "拖动即时生效，松手自动记住", "volume", None),
        ("speech.profile", "资源档位", "低占用 / 平衡 / 高质量；改完要重启引擎", "choice",
         ["fast", "balanced", "quality"]),
        ("speech.device", "推理算力", "有可用显卡时可切 cuda；改完要重启引擎", "choice",
         ["auto", "cpu", "cuda"]),
        ("tts.enabled", "语音播报", "关掉之后只显示文字", "bool", None),
        ("tts.engine", "合成引擎",
         "vits：纯 CPU、快，5 个角色音（默认）；chattts：更自然，但要显卡、慢、首次加载十几秒",
         "choice", ["vits", "chattts"]),
        ("tts.voice", "音色", "选中即用，点「试听」当场听；也可以命令行换", "voice", None),
        ("tts.speed", "语速", "", "choice", ["0.9", "1.0", "1.1", "1.2"]),
    ]),
    ("大脑（LLM）", [
        ("llm.enabled", "启用 LLM", "关掉就只能用离线规则", "bool", None),
        ("llm.reasoning_effort", "思考程度", "越深越慢越贵；语音场景建议 low", "choice",
         ["off", "low", "medium", "high", "max"]),
        ("llm.base_url", "服务地址", "任何 OpenAI 兼容服务", "text", None),
        ("llm.model", "模型名", "", "text", None),
        ("llm.max_rounds", "一步最多调几次工具", "多步任务撞上限会只说半句；默认 12", "choice",
         ["6", "12", "20"]),
        ("llm.api_key", "API Key", "留空表示不改动；也可以读环境变量", "password", None),
    ]),
    ("外观", [
        ("ui.accent", "主题色", "换主色，整个界面跟着变；不用重启", "accent", None),
        ("ui.show_stats", "底部速览", "主界面底部的唤醒 / 算力 / 合成 / 声纹四格", "bool", None),
    ]),
    ("交互", [
        ("agent.follow_up_ms", "追问窗口", "答完继续收音的时长，0 = 回待命", "choice",
         ["0", "3000", "5000", "8000"]),
        ("agent.cues", "提示音", "收音 / 确认 / 结束的短提示音", "bool", None),
        ("agent.barge_in_wake", "喊唤醒词可打断", "播报时也保持唤醒词监听", "bool", None),
        ("agent.confirm.enabled", "敏感操作要确认", "关机、执行命令这类先问一句", "bool", None),
    ]),
]

THRESHOLD_CHOICES = ["0.45", "0.55", "0.65"]
THRESHOLD_LABELS = {"0.45": "宽松", "0.55": "标准", "0.65": "严格"}


class SettingsPage(Page):
    """设置：分组卡片 + 开关/分段控件，**改完立即生效**，不用点保存。

    少数几项（引擎、线程、唤醒词、声卡）只能在构造模型时读一次，改完要重启
    引擎 —— 那些会在主界面弹一条提示，带「立即重启」按钮。
    """

    accent_changed = pyqtSignal(str)

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("设置", "改完立即生效；需要重启引擎的会提示"))

        self.area, self.body = scroll_page()
        layout.addWidget(self.area, 1)
        self.widgets: dict[str, Any] = {}
        self._building = True
        self.build()
        self._building = False

    # ── 构建 ──
    def build(self) -> None:
        values = self.console.settings()
        warning = self._engine_warning(values)
        if warning is not None:
            self.body.addWidget(warning)
        self.body.addWidget(self._voiceprint_card(values))
        for title, fields in SETTING_SECTIONS:
            self.body.addWidget(ui.section_title(title))
            card = ui.Card()
            for key, label, hint, kind, options in fields:
                card.body.addWidget(self._row(key, label, hint, kind, options,
                                              values.get(key)))
            self.body.addWidget(card)
        self.body.addStretch(1)

    def _row(self, key: str, label: str, hint: str, kind: str,
             options: list[str] | None, value: Any) -> QWidget:
        if kind == "voice":
            return self._voice_row(label, hint)
        if kind == "volume":
            return self._volume_row(label, hint, value)
        if kind == "accent":
            return self._accent_row(label, hint, value)

        if kind == "bool":
            control = ui.ToggleSwitch(checked=bool(value))
            control.toggled.connect(lambda checked, k=key: self._save(k, checked))
            self.widgets[key] = ("bool", control)
            return setting_row(label, hint, control)

        if kind == "choice":
            labels = [THRESHOLD_LABELS.get(o, o) for o in (options or [])]
            current = "" if value is None else str(value)
            shown = THRESHOLD_LABELS.get(current, current)
            control = ui.SegmentedControl(labels, shown, width=260)
            control.changed.connect(
                lambda shown_label, k=key, opts=options:
                self._save(k, self._choice_value(shown_label, opts)))
            self.widgets[key] = ("choice", control)
            wrap = QWidget()
            box = QVBoxLayout(wrap)
            box.setContentsMargins(0, 4, 0, 4)
            box.setSpacing(6)
            title = QLabel(label)
            title.setObjectName("RowTitle")
            box.addWidget(title)
            if hint:
                sub = QLabel(hint)
                sub.setObjectName("RowSubtitle")
                box.addWidget(sub)
            box.addWidget(control)
            return wrap

        control = QLineEdit("" if value is None else str(value))
        if kind == "password":
            control.setEchoMode(QLineEdit.EchoMode.Password)
            control.setPlaceholderText("留空表示不改动")
        control.setFixedWidth(260)
        control.editingFinished.connect(
            lambda k=key, w=control: self._save_text(k, w))
        self.widgets[key] = ("text", control)
        return setting_row(label, hint, control)

    def _engine_warning(self, values: dict) -> ui.Card | None:
        """选了 ChatTTS 就在最上面摆一张警告卡。

        它和 VITS 不是一个量级的东西：要显卡、吃 2 GB 显存、首次加载十几秒、
        合成速度大约 1 倍实时。这些事不提前说清楚，用户只会觉得"助手变卡了"。
        """
        if str(values.get("tts.engine") or "").strip().lower() != "chattts":
            return None
        card = ui.Card()
        head = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(theme.pixmap("alert", theme.ORANGE, 18))
        head.addWidget(icon)
        title = QLabel("ChatTTS 需要独显")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        card.body.addLayout(head)
        note = QLabel(
            "· 约 2 GB 显存、首次加载十几秒（要联网下 1 GB 权重）\n"
            "· 合成速度大约是 1 倍实时：一句话要等 1~2 秒才开口\n"
            "· 没装 ChatTTS 包或没有 N 卡时，启动会直接报错；"
            "想立刻恢复就把它改回 vits\n"
            "· 改完要重启引擎（主界面会弹提示）"
        )
        note.setObjectName("RowSubtitle")
        note.setWordWrap(True)
        card.body.addWidget(note)
        return card

    def _volume_row(self, label: str, hint: str, value: Any) -> QWidget:
        """输出音量：拖动即时生效，松手才写配置。

        之所以要分开：拖动会连续触发，每动一格写一遍 config.yaml 既慢又吵；
        而只改内存里的总增益，声音是立刻变的。
        """
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(6)
        title = QLabel(label)
        title.setObjectName("RowTitle")
        box.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            box.addWidget(sub)

        # 表单传过来的是原始增益（1.0 = 100%），音量条要的是百分比
        try:
            percent = 100 if value is None else int(round(float(value) * 100))
        except (TypeError, ValueError):
            percent = 100
        self.volume = ui.VolumeSlider(percent)
        self.volume.preview.connect(
            lambda v: self.console.set_volume(v, persist=False))
        self.volume.committed.connect(
            lambda v: self.console.set_volume(v, persist=True))
        box.addWidget(self.volume)
        self.widgets["audio.output_gain"] = ("volume", self.volume)
        return wrap

    def _accent_row(self, label: str, hint: str, value: Any) -> QWidget:
        """主题色：一排色点 + 自定义取色。选中即换，不用重启。"""
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(6)
        title = QLabel(label)
        title.setObjectName("RowTitle")
        box.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            box.addWidget(sub)

        self.accent_picker = ui.AccentPicker(str(value or ""))
        self.accent_picker.picked.connect(self._on_accent)
        box.addWidget(self.accent_picker)
        self.widgets["ui.accent"] = ("accent", self.accent_picker)
        return wrap

    def _on_accent(self, value: str) -> None:
        if self._building:
            return
        if not self.console.set_accent(value).get("ok"):
            return
        # 界面自己刷新（样式表 + 图标），主窗口负责重建主面板
        self.accent_changed.emit(value)

    def apply_theme(self) -> None:
        picker = getattr(self, "accent_picker", None)
        if picker is not None:
            picker.set_current(self.console.cfg.ui.accent)
        volume = getattr(self, "volume", None)
        if volume is not None:
            volume.refresh_theme()

    def _voice_row(self, label: str, hint: str) -> QWidget:
        """音色这一行得自己搭：下拉框是动态填的，旁边还要一个试听按钮。

        为什么要给名字而不是只给编号：vits 只认数字下标，ChatTTS 只认种子，
        0~2 号是英文音色 —— 拿它念中文就是发闷发粗加电流声。列表里只放中文
        音色，选错了也听得出区别，点一下试听就知道。
        """
        data = self.console.voices_payload()
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(6)

        title = QLabel(label)
        title.setObjectName("RowTitle")
        box.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            box.addWidget(sub)

        line = QHBoxLayout()
        line.setSpacing(8)
        combo = QComboBox()
        combo.setFixedWidth(196)
        for row in data.get("rows", []):
            combo.addItem(str(row["label"]), row["name"])
        index = combo.findData(data.get("current_name"))
        if index < 0:
            index = combo.findData(
                next((r["name"] for r in data.get("rows", [])
                      if r["id"] == data.get("current")), None))
        if index >= 0:
            combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda _i, w=combo: self._voice_changed(w))
        line.addWidget(combo)

        audition = ui.plain_button("试听", "play")
        audition.setFixedWidth(74)
        audition.clicked.connect(lambda _c=False, w=combo: self._audition(w))
        line.addWidget(audition)
        line.addStretch(1)
        box.addLayout(line)

        note = str(data.get("note") or "")
        self.voice_note = QLabel(note)
        self.voice_note.setObjectName("RowSubtitle")
        self.voice_note.setWordWrap(True)
        self.voice_note.setVisible(bool(note))
        box.addWidget(self.voice_note)

        # 试听的反馈：以前只往运行日志里写一行，用户点了按钮什么也看不到
        self._audition_hint = QLabel("")
        self._audition_hint.setObjectName("Value")
        self._audition_hint.setWordWrap(True)
        self._audition_hint.setVisible(False)
        box.addWidget(self._audition_hint)

        self.widgets["tts.voice"] = ("voice", combo)
        return wrap

    def _voice_changed(self, combo: QComboBox) -> None:
        if self._building:
            return
        name = combo.currentData()
        if not name:
            return
        self._apply({"tts.voice": str(name)})
        if getattr(self, "voice_note", None) is not None:
            self.voice_note.setVisible(False)

    def _audition(self, combo: QComboBox) -> None:
        """试听。失败必须弹出来 —— 只写运行日志等于没反馈。"""
        name = combo.currentData()
        if not name:
            return
        result = self.console.audition_voice(name)
        if not result.get("ok"):
            box = QMessageBox(self)
            box.setWindowTitle("试听失败")
            box.setText(str(result.get("error")))
            box.setInformativeText("运行日志里有更详细的说明。")
            box.exec()
            self._audition_hint.setVisible(False)
            return
        self._paint_audition(self.console.audition_status())

    def _paint_audition(self, info: dict) -> None:
        """把试听进度画到那一行提示上。

        以前只在点击的瞬间写一句"正在试听 / 首次要加载"，之后没人清 ——
        用户看到的就是"一直显示在加载"。现在 on_tick 每 300ms 轮一次状态。
        """
        state = str(info.get("state") or "idle")
        voice = str(info.get("voice") or "")
        if state == "idle":
            self._audition_hint.setVisible(False)
            return
        if state == "loading":
            text, color = "正在加载合成模型…（首次要十几秒）", theme.ORANGE
        elif state == "playing":
            text, color = "正在试听 " + voice, theme.ACCENT
        elif state == "done":
            text, color = "试听结束：" + voice, theme.GREEN
            # 播完留 5 秒再收起来，让人来得及看一眼
            if time.time() - float(info.get("at") or 0) > 5.0:
                self.console.clear_audition()
                self._audition_hint.setVisible(False)
                return
        else:
            text, color = "试听失败：" + str(info.get("error") or "未知原因"), theme.RED
            self.console.clear_audition()
        self._audition_hint.setText(text)
        self._audition_hint.setStyleSheet("color: " + color + "; font-size: 12px;")
        self._audition_hint.setVisible(True)

    def on_tick(self, snapshot: dict, state: str) -> None:
        # 只关心试听那一行：其余地方由各自的控件自己刷
        self._paint_audition(self.console.audition_status())

    @staticmethod
    def _choice_value(shown: str, options: list[str] | None) -> Any:
        for option in options or []:
            if THRESHOLD_LABELS.get(option, option) == shown:
                return option
        return shown

    # ── 保存 ──
    def _apply(self, updates: dict) -> None:
        if self._building:
            return
        result = self.console.update_config(updates)
        if not result.get("ok"):
            box = QMessageBox(self)
            box.setWindowTitle("保存失败")
            box.setText(str(result.get("error")))
            box.exec()
            return
        if result.get("restart_needed"):
            self._toast("已保存，重启引擎后生效")

    def _save(self, key: str, value: Any) -> None:
        self._apply({key: value})

    def _save_text(self, key: str, widget: QLineEdit) -> None:
        raw = widget.text().strip()
        if key == "llm.api_key" and raw == "":
            return
        if key in ("wake.keywords", "wake.replies"):
            self._apply({key: [p.strip() for p in raw.replace("，", ",").replace("、", ",").split(",")
                               if p.strip()]})
            return
        self._apply({key: raw})

    def _toast(self, text: str) -> None:
        self.console.log("[ui] " + text)

    # ── 声纹卡片 ──
    def _voiceprint_card(self, values: dict) -> ui.Card:
        card = ui.Card()
        head = QHBoxLayout()
        title = QLabel("声纹")
        title.setObjectName("CardTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.voice_pill = ui.Pill(str(values.get("speaker.status", "未开启")))
        head.addWidget(self.voice_pill)
        card.body.addLayout(head)

        note = QLabel("开启后只有你的声音能唤醒。录 3 次更稳；"
                      "没录声纹之前任何人都能唤醒，程序会一直提醒你。")
        note.setObjectName("CardSubtitle")
        note.setWordWrap(True)
        card.body.addWidget(note)

        row = ui.ListRow("usercheck", "开启声纹匹配", "对着麦克风录一段自己的声音作为凭证")
        self.voice_toggle = ui.ToggleSwitch(checked=bool(values.get("speaker.enabled")))
        self.voice_toggle.toggled.connect(self._toggle_voiceprint)
        row.add_trailing(self.voice_toggle)
        card.body.addWidget(row)

        threshold_row = QWidget()
        box = QVBoxLayout(threshold_row)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(6)
        label = QLabel("严格程度")
        label.setObjectName("RowTitle")
        box.addWidget(label)
        sub = QLabel("越严格越不容易被别人的声音唤醒，但也可能认不出你自己")
        sub.setObjectName("RowSubtitle")
        box.addWidget(sub)
        current = str(values.get("speaker.threshold", "0.55"))
        self.threshold = ui.SegmentedControl(
            [THRESHOLD_LABELS[t] for t in THRESHOLD_CHOICES],
            THRESHOLD_LABELS.get(current, "标准"), width=260)
        self.threshold.changed.connect(self._save_threshold)
        box.addWidget(self.threshold)
        card.body.addWidget(threshold_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_enroll = ui.primary_button("录制声纹", "mic")
        self.btn_enroll.clicked.connect(lambda: self._voice_action("enroll"))
        self.btn_test = ui.plain_button("试一次", "check")
        self.btn_test.clicked.connect(lambda: self._voice_action("test"))
        self.btn_clear = ui.plain_button("清除", "trash", "Danger")
        self.btn_clear.clicked.connect(lambda: self._voice_action("clear"))
        for button in (self.btn_enroll, self.btn_test, self.btn_clear):
            buttons.addWidget(button)
        buttons.addStretch(1)
        card.body.addLayout(buttons)

        self.voice_hint = QLabel("")
        self.voice_hint.setObjectName("Hint")
        self.voice_hint.setWordWrap(True)
        card.body.addWidget(self.voice_hint)
        return card

    def _toggle_voiceprint(self, checked: bool) -> None:
        self._apply({"speaker.enabled": checked})
        self.voice_hint.setText(
            "已开启。录一次声纹之后才会真正开始校验。" if checked else "已关闭，任何人都能唤醒。")
        self.refresh_voice_status()

    def _save_threshold(self, shown: str) -> None:
        for key, label in THRESHOLD_LABELS.items():
            if label == shown:
                self._apply({"speaker.threshold": float(key)})
                return

    def _voice_action(self, action: str) -> None:
        """录音要花几秒，放到后台线程里，别把界面卡住。"""
        agent = self.console.ensure_agent()
        voice = agent.voiceprint
        if action == "clear":
            voice.clear()
            self.voice_hint.setText("已清除声纹，现在任何人都能唤醒。")
            self.refresh_voice_status()
            return
        if not voice.enabled:
            self.voice_hint.setText("先打开上面的开关再录。")
            return
        if not voice._ensure_model():
            self.voice_hint.setText("声纹模型用不了：" + voice.status_text())
            return

        self.btn_enroll.setEnabled(False)
        self.btn_test.setEnabled(False)
        seconds = 3.0
        self.voice_hint.setText("请说话……（" + str(int(seconds)) + " 秒）")
        holder: dict = {}

        def work() -> None:
            holder["result"] = (voice.enroll_now(seconds) if action == "enroll"
                                else voice.verify_now(seconds))

        import threading

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                QTimer.singleShot(200, poll)
                return
            self.btn_enroll.setEnabled(True)
            self.btn_test.setEnabled(True)
            result = holder.get("result") or {"ok": False, "error": "没有结果"}
            if action == "enroll":
                if result.get("ok"):
                    self.voice_hint.setText("录好了（第 " + str(result.get("count", 1))
                                            + " 条）。再录一两次会更稳。")
                else:
                    self.voice_hint.setText(str(result.get("error")))
            else:
                if result.get("ok"):
                    verdict = "匹配 ✅" if result.get("allowed") else "不匹配 ❌"
                    self.voice_hint.setText(verdict + "　相似度 " + str(result.get("score"))
                                            + "　" + str(result.get("note")))
                else:
                    self.voice_hint.setText(str(result.get("error")))
            self.refresh_voice_status()

        QTimer.singleShot(200, poll)

    def refresh_voice_status(self) -> None:
        agent = self.console.ensure_agent()
        text = agent.voiceprint.status_text()
        self.voice_pill.setText(text)
        if not agent.voiceprint.enabled:
            self.voice_pill.set_color(theme.MUTED)
        elif agent.voiceprint.enrolled:
            self.voice_pill.set_color(theme.GREEN)
        else:
            self.voice_pill.set_color(theme.ORANGE)

    def on_show(self) -> None:
        self.refresh_voice_status()


# ─────────────────────────────── 设备 ───────────────────────────────


class DevicesPage(Page):
    """麦克风与扬声器。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        self.inputs: list[dict] = []
        self.outputs: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("设备", "换设备会重启监听，建议先停止"))

        card = ui.Card()
        self.combo_in = QComboBox()
        self.combo_out = QComboBox()
        card.body.addWidget(setting_row("麦克风", "说唤醒词和指令的那个", self.combo_in))
        card.body.addWidget(setting_row("扬声器", "播报回复的那个", self.combo_out))
        layout.addWidget(card)

        row = QHBoxLayout()
        row.setSpacing(8)
        apply_button = ui.primary_button("应用并重启引擎")
        apply_button.clicked.connect(self.apply)
        refresh = ui.plain_button("刷新列表", "refresh")
        refresh.clicked.connect(self.load)
        test = ui.plain_button("录音测试", "mic")
        test.clicked.connect(self.record_test)
        for button in (apply_button, refresh, test):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self.hint = QLabel("")
        self.hint.setObjectName("Hint")
        layout.addWidget(self.hint)
        layout.addStretch(1)
        self.load()

    def load(self) -> None:
        data = self.console.devices()
        if not data.get("ok"):
            self.hint.setText(str(data.get("error")))
            return
        self.inputs = data.get("input", [])
        self.outputs = data.get("output", [])
        current = data.get("current", {})
        for combo, items, selected in (
            (self.combo_in, self.inputs, current.get("input")),
            (self.combo_out, self.outputs, current.get("output")),
        ):
            combo.clear()
            combo.addItem("系统默认", None)
            for device in items:
                combo.addItem("[" + str(device["index"]) + "] " + device["name"], device["index"])
            index = combo.findData(selected)
            combo.setCurrentIndex(index if index >= 0 else 0)

    def apply(self) -> None:
        result = self.console.update_config({
            "audio.input_device": self.combo_in.currentData(),
            "audio.output_device": self.combo_out.currentData(),
        })
        if not result.get("ok"):
            self.hint.setText(str(result.get("error")))
            return
        self.console.stop_engine()
        self.console.start_engine()
        self.hint.setText("已保存，正在重启引擎（模型加载要几秒）。")

    def record_test(self) -> None:
        if self.console.agent is not None and self.console.agent.running:
            self.hint.setText("正在监听中，请先停止引擎。")
            return
        self.hint.setText("请说话，最长 12 秒…")
        result = self.console.record_once(12.0)
        self.hint.setText("识别到：" + str(result.get("text")) if result.get("ok")
                          else str(result.get("error")))


# ─────────────────────────────── 关于 ───────────────────────────────


class AboutPage(Page):
    """关于：版本、环境、快捷键。"""

    SHORTCUTS = [
        ("启动监听", "Ctrl+1"),
        ("停止监听", "Ctrl+2"),
        ("打断当前任务", "Esc"),
        ("对话记录", "Ctrl+R"),
        ("文字指令", "Ctrl+K"),
        ("工具", "Ctrl+T"),
        ("设置", "Ctrl+,"),
        ("运行日志", "Ctrl+L"),
        ("退出", "Ctrl+Q"),
    ]

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(page_header("关于", ""))

        head = ui.Card()
        top = QHBoxLayout()
        self.logo = QLabel()
        self.logo.setFixedSize(56, 56)
        self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.logo)
        self._paint_logo()
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel("大肥鲸")
        name.setObjectName("CardTitle")
        text.addWidget(name)
        self.subtitle = QLabel("版本 —")
        self.subtitle.setObjectName("CardSubtitle")
        text.addWidget(self.subtitle)
        top.addLayout(text, 1)
        head.body.addLayout(top)
        layout.addWidget(head)

        self.env_card = ui.Card()
        layout.addWidget(self.env_card)

        layout.addWidget(ui.section_title("快捷键"))
        keys = ui.Card()
        for label, shortcut in self.SHORTCUTS:
            row = ui.ListRow("", label)
            pill = ui.Pill(shortcut, theme.MUTED)
            row.add_trailing(pill)
            keys.body.addWidget(row)
        layout.addWidget(keys)
        layout.addStretch(1)

    def _paint_logo(self) -> None:
        self.logo.setPixmap(theme.pixmap("mic", theme.ACCENT, 34))
        self.logo.setStyleSheet(
            "background: " + theme.rgba(theme.ACCENT, 0.14) + "; border-radius: 16px;")

    def apply_theme(self) -> None:
        self._paint_logo()

    def on_tick(self, snapshot: dict, state: str) -> None:
        from .. import __version__

        self.subtitle.setText("版本 " + __version__ + "　·　本地语音 · 可选 LLM 大脑")
        status = snapshot.get("status", {})
        while self.env_card.body.count():
            item = self.env_card.body.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        from PyQt6.QtWidgets import QLabel as _QLabel

        rows = [
            ("配置", str(snapshot.get("config_path", ""))),
            ("模型", str(snapshot.get("models_dir", ""))),
            ("大脑", str(status.get("mode", ""))),
            ("算力", str(status.get("provider_text", ""))),
            ("合成", str(status.get("tts_engine", ""))),
            ("声纹", str((status.get("voiceprint") or {}).get("text", "未开启"))),
            ("工具", str(snapshot.get("tools", 0)) + " 个　技能 "
             + str((snapshot.get("skills") or {}).get("ok", 0)) + " 个"),
        ]
        for title, value in rows:
            line = QWidget()
            box = QHBoxLayout(line)
            box.setContentsMargins(0, 3, 0, 3)
            label = _QLabel(title)
            label.setObjectName("RowTitle")
            label.setFixedWidth(56)
            box.addWidget(label)
            value_label = _QLabel(value)
            value_label.setObjectName("Mono")
            value_label.setWordWrap(True)
            box.addWidget(value_label, 1)
            self.env_card.body.addWidget(line)
