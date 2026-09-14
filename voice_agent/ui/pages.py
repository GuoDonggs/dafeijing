# -*- coding: utf-8 -*-
"""七个页面：主页、对话、工具、技能、设置、设备、关于。

每个页面只做两件事：把自己的状态画出来、把用户的操作转成 Console 调用。
业务逻辑一律不在界面里 —— 这样换界面不用碰引擎，换引擎也不用碰界面。
"""

from __future__ import annotations

import json
import threading
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
from . import models_dialog
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


def _clip(text: str, limit: int) -> str:
    """截断成一眼能看完的一小段（主面板不是阅读器）。"""
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


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

        # 本轮问答：刚才听成了什么、答了什么。
        # 放在主界面而不是只藏在菜单里 —— 识别错的时候，用户第一眼就能看出来
        # （"我说的是关灯，它听成开灯了"），不用点开对话记录去比对。
        self.turn_card = QWidget()
        self.turn_card.setObjectName("TurnCard")
        turn_box = QVBoxLayout(self.turn_card)
        turn_box.setContentsMargins(12, 10, 12, 10)
        turn_box.setSpacing(4)
        self.turn_heard = QLabel()
        self.turn_heard.setObjectName("TurnHeard")
        self.turn_heard.setWordWrap(True)
        self.turn_reply = QLabel()
        self.turn_reply.setObjectName("TurnReply")
        self.turn_reply.setWordWrap(True)
        turn_box.addWidget(self.turn_heard)
        turn_box.addWidget(self.turn_reply)
        self.turn_card.setVisible(False)
        layout.addSpacing(14)
        layout.addWidget(self.turn_card)

        layout.addSpacing(18)
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
        # 后台子代理还在干活时把进度挂在提示行后面：用户看不见进程，
        # 但至少知道"刚派出去那件事还在跑"，而不是以为助手忘了。
        subs = status.get("subagents") or {}
        watch = status.get("watches") or {}
        running_notes = [str(subs.get("text") or "") if subs.get("running") else "",
                         str(watch.get("text") or "") if watch.get("running") else ""]
        running_notes = [note for note in running_notes if note]
        if running_notes and state in ("idle", "listen"):
            hint = (hint + "　·　" + "　".join(running_notes)).strip("　· ")
        if self.state_label.text() != text:
            self.state_label.setText(text)
        self.state_label.setStyleSheet("color: " + theme.STATE_COLORS.get(state, theme.TEXT))
        if self.hint_label.text() != hint:
            self.hint_label.setText(hint)

        running = bool(status.get("running"))
        self.btn_start.setEnabled(not running and not starting)
        self.btn_stop.setEnabled(running)
        self.btn_cancel.setEnabled(running)

        # 本轮问答：可以在设置里关掉（想要"只有一个圆球"的极简观感时）
        show_turn = bool(getattr(self.console.cfg.ui, "show_turn", True))
        heard = str(status.get("last_heard") or "").strip()
        reply = str(status.get("last_reply") or "").strip()
        if show_turn and (heard or reply):
            # 主面板高度是固定的，太长的回复会把按钮挤出屏幕 —— 截断，
            # 想看全文去「对话与指令」页
            self.turn_heard.setText("我：" + _clip(heard, 64) if heard else "")
            self.turn_heard.setVisible(bool(heard))
            self.turn_reply.setText("大肥鲸：" + _clip(reply, 140) if reply else "")
            self.turn_reply.setVisible(bool(reply))
            self.turn_card.setVisible(True)
        else:
            self.turn_card.setVisible(False)

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
            return ("缺少模型：" + "、".join(missing)
                    + "（菜单 ☰ →「语音模型」可以自动下载，或指到已有的模型目录）")
        if state == "off":
            return theme.STATE_HINTS["off"]
        if state == "listen":
            if status.get("listen_heard"):
                # 已经在说话了：这时候不该再显示"还剩几秒过期"，
                # 用户会以为必须赶时间，越急越说不清
                seconds = int((status.get("listen_ms") or 0) / 1000)
                return ("我在听，说完停一下就行" if seconds < 3
                        else "还在听（已录 " + str(seconds) + " 秒），说完停一下就行")
            seconds = int((status.get("listen_timeout_ms") or 8000) / 1000)
            return "说指令就好，" + str(seconds) + " 秒内没有提问我会回到待命"
        if state == "think":
            return "正在处理，随时可以喊唤醒词打断"
        if state == "speak":
            return "点一下圆球可以打断播报"
        words = status.get("wake_words") or []
        voice = status.get("voiceprint") or {}
        extra = "（已开启声纹，只认你的声音）" if voice.get("ready") else ""
        mode = str((status.get("security") or {}).get("label") or "")
        if mode and mode != "标准":
            # 权限被改过就要一直看得见 —— 忘了自己开过只读，只会觉得"它坏了"
            extra += "（当前「" + mode + "」模式）"
        return "喊「" + (words[0] if words else "唤醒词") + "」叫我" + extra


# ─────────────────────────── 对话与指令 ───────────────────────────


class ChatPage(Page):
    """一条时间线：语音说的、界面敲的，都在这里，也都在这里发指令。

    以前"对话记录"和"文字指令"是两个菜单项，点开来却是同一个页面 —— 合并掉。
    """

    #: 后台问完一句了（Qt 的东西只能在界面线程碰，所以用信号回来）
    asked = pyqtSignal()
    #: 后台播报结束（回到界面线程收拾状态）
    spoken = pyqtSignal()
    #: 正在后台播报（避免连点两次叠着念）
    _speaking = False

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
        # 开新会话：清空上下文但保留这份记录（记录是历史，上下文是包袱）
        self.btn_new = ui.plain_button("新会话")
        self.btn_new.setToolTip("清空上下文，之后说的话不再参考之前的对话")
        self.btn_new.clicked.connect(self.new_session)
        row.addWidget(self.btn_new)
        layout.addLayout(row)

        self._bubbles: list = []
        self._turns_text: list = []
        self._rendered = 0
        self._last_key = ""
        # 「跟着新消息走到底部」是一个**用户意图**，不是每次刷新都做的事：
        # 用户滚上去看历史时不能把他拽回来，而自己发消息时又必须跟到底。
        self._stick = True
        self._settling = False
        bar = self.area.verticalScrollBar()
        # 关键：新气泡插进来之后，可滚动范围是**布局跑完**才更新的。
        # 在 on_tick 里直接 setValue(bar.maximum()) 拿到的还是插之前的旧范围，
        # 于是每次刷新都跳回最顶上 —— 这正是"每次都要手动滚到底"的原因。
        bar.rangeChanged.connect(self._on_range_changed)
        bar.valueChanged.connect(self._on_scrolled)
        self._asking = False
        self._speaking = False
        self.asked.connect(self._on_asked)
        self.spoken.connect(self._on_spoken)

    # ── 发送 ──
    def send(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        # 自己发出去的消息当然要看到：即使刚才在翻历史，也跟到底部
        self._stick = True
        agent = self.console.ensure_agent()
        if agent.running:
            agent.dispatch(text)
            self._sync()
            return
        if self._asking:
            self.console.log("[ui] 上一条还在处理，等它完了再说")
            return
        # 引擎没跑（只启用了大脑）时，这一步要调模型，可能好几秒甚至更久。
        # **绝不能**在 Qt 主线程里同步跑：以前界面会整个假死，Windows 直接
        # 标记"无响应"，用户只能等或者强杀。丢到后台线程，结果照旧通过
        # 对话记录回到时间线上（on_tick 会把它渲染出来）。
        from ..tools import CANCEL_REPLY

        self._asking = True
        self.btn_send.setEnabled(False)
        self.btn_send.setText("处理中…")
        agent.note("user", text)
        self._sync()

        def work() -> None:
            try:
                # 桌面版：引擎没启动时走界面里的模态确认框（console.confirm_hook）
                reply = agent.ask(text, speak=False, confirm=self.console.confirm_channel())
                if reply == CANCEL_REPLY:
                    reply = ("这条指令属于敏感操作，需要语音确认。"
                             "先启动监听，再对着麦克风说一次。")
            except Exception as exc:  # noqa: BLE001 - 出错也要有一句话，不能没反应
                reply = "处理出错了：" + str(exc)[:120]
            agent.note("assistant", reply)
            self.console.log("[ui] " + reply)
            self.asked.emit()

        threading.Thread(target=work, name="ui-ask", daemon=True).start()

    def _on_spoken(self) -> None:
        """后台那句念完了（或失败了）：把状态收回来。"""
        self._speaking = False

    def _on_asked(self) -> None:
        self._asking = False
        self._sync()
        try:
            self.btn_send.setEnabled(True)
            self.btn_send.setText("派发")
        except RuntimeError:   # 窗口已经关了
            pass

    def new_session(self) -> None:
        """清空上下文开一个新的（语音说「换个话题」也会走到这里）。"""
        agent = self.console.ensure_agent()
        message = agent.new_session()
        self.console.log("[ui] " + message)
        self._sync()

    def say(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        self.entry.clear()
        agent = self.console.ensure_agent()
        # 合成引擎会在需要时按需建起来（Agent._ensure_tts），这里不再裸调
        # agent.load()：模型不齐时它会抛 ConfigError，而 Qt 槽里的未捕获异常
        # 会让整个进程直接 abort（连日志都没有，用户看到的是"点一下程序就没了"）。
        #
        # **必须丢到后台线程**：Tts.speak 是"合成 + 阻塞播放"，一句话要占住
        # 好几秒；放在槽里同步跑，窗口整段话都不重绘（Windows 会标"无响应"，
        # 「打断」按钮也点不动）—— 和上面 send() 里那条规矩一样。
        if self._speaking:
            self.console.log("[ui] 上一句还在播，等它念完")
            return
        self._speaking = True
        if agent.tts is None:
            # 先探一下引擎能不能建起来：这样"没有播报"的提示还留在主线程里弹
            try:
                agent._ensure_tts()  # noqa: SLF001 - 界面这边就用它按需建
            except Exception as exc:  # noqa: BLE001
                self.console.log("[ui] 语音合成没起来：" + str(exc)[:100])
        if agent.tts is None:
            self._speaking = False
        else:
            def speak_later() -> None:
                try:
                    agent.speak(text)
                except Exception as exc:  # noqa: BLE001 - 后台线程不能把异常吞了
                    self.console.log("[ui] 播报失败：" + str(exc)[:100])
                self.spoken.emit()

            threading.Thread(target=speak_later, name="ui-say", daemon=True).start()
            return
        if agent.tts is None:
            hint = ("语音播报被关掉了（设置 → 语音与算力 → 语音播报）"
                    if not self.console.cfg.tts.enabled else
                    "语音合成没加载起来，运行日志里的 [tts] 那一行会说明原因")
            box = QMessageBox(self)
            box.setWindowTitle("没有播报")
            box.setText(hint)
            box.exec()

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
        # 先把"要不要跟到底"定下来再插入：插入之后范围会变，
        # 那时候再判断就已经不是用户当初的位置了
        stick = self._at_bottom() or not self._bubbles
        for turn in turns[self._rendered:]:
            self._append(turn)
            self._turns_text.append(turn)
        self._rendered = len(turns)
        self._last_key = key
        self._stick = stick

    def _at_bottom(self, slack: int = 48) -> bool:
        """现在是不是贴着底部（留一点余量，免得差几像素就判定"用户滚上去了"）。"""
        bar = self.area.verticalScrollBar()
        return bar.maximum() - bar.value() <= slack

    def _on_scrolled(self, _value: int) -> None:
        """用户自己拖动滚动条 → 他要看历史，别再把他拽回底部。"""
        if not self._settling:
            self._stick = self._at_bottom()

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容变高（范围变大）时，如果该跟着走就补一次到底。"""
        if self._stick:
            self._settling = True
            self.area.verticalScrollBar().setValue(maximum)
            self._settling = False

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
        # 用途标签：和给模型看的是同一份 —— 用户能一眼看出"哪个工具适合干什么"
        for tag in (tool.get("tags") or [])[:3]:
            badges_layout.addWidget(ui.Pill(str(tag), theme.BLUE))
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
        # 用户亲手点了「试运行」并填了参数 = 明确意图：allow_sensitive 让它不要
        # 再被"没有确认通道"挡下（权限模式、deny_tools、只读档仍然照旧生效）
        result = self.console.call_tool(name, args, allow_sensitive=True)
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
        # 只差装包的话给一个按钮 —— 不然用户只能自己开终端敲 pip，
        # 而"自定义工具跑不起来"十有八九就是卡在这一步
        if skill.get("needs_install"):
            install = ui.primary_button("装依赖")
            install.setToolTip("要装：" + "、".join(skill.get("missing") or []))
            install.clicked.connect(lambda _=False, s=skill: self.install(s))
            box.addWidget(install)
        edit = ui.plain_button("编辑")
        edit.setEnabled(ok and not str(skill.get("source", "")).endswith(".py"))
        edit.clicked.connect(lambda _=False, s=skill: self.edit(s))
        delete = ui.icon_button("trash", "删除", theme.RED, 30)
        delete.clicked.connect(lambda _=False, s=skill: self.delete(s))
        box.addWidget(edit)
        box.addWidget(delete)
        row.add_trailing(tools_box)
        return row

    def install(self, skill: dict) -> None:
        """装这个文件声明的依赖。pip 可能要跑一阵子，别卡住界面。"""
        path = str(skill.get("source", ""))
        self._error_box("正在安装依赖",
                        "要装：" + "、".join(skill.get("missing") or []) + "\n\n"
                        "点确定后开始装，装完会自动重新加载。网络慢的话可能要等一会儿。")
        holder: dict = {}

        def work() -> None:
            holder["result"] = self.console.install_skill_deps(path)

        import threading

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def poll() -> None:
            if thread.is_alive():
                QTimer.singleShot(300, poll)
                return
            result = holder.get("result") or {"ok": False, "error": "没有结果"}
            self.reload()
            if result.get("ok"):
                self._error_box("装好了",
                                "依赖已安装。"
                                + ("工具已经能用了。" if result.get("loaded")
                                   else "但工具还是没加载起来，看看它的报错。"))
            else:
                self._error_box("装依赖失败", str(result.get("error")))

        QTimer.singleShot(300, poll)

    def _error_box(self, title: str, text: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(text)
        box.exec()

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
        # 必须带上 kind：编辑的是 tools/ 里的自定义工具时，少了它就会当成技能
        # 存到 skills/ 目录下 —— 原文件一个字没动，反而多出一个同名工具，
        # 加载时报"工具名重复，实际生效的是后加载的那个"，用户改了等于没改。
        kind = "tool" if str(skill.get("kind") or "") == "tool" else "skill"
        SkillDialog(self, "编辑技能", str(read["name"]), str(read["content"]),
                    kind=kind).exec()
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
    # 大脑这一块按"先开关、再默认模型、最后按用途分别挂"排：
    # 服务地址 / 模型名 / 密钥是**默认模型**（也就是主对话）的参数，
    # 多模型里的档案会覆盖它们 —— 所以这两组标题必须写清楚谁管谁。
    ("大脑（LLM）", [
        ("llm.enabled", "启用 LLM", "关掉就只能用离线规则", "bool", None),
        ("llm.reasoning_effort", "思考程度", "越深越慢越贵；语音场景建议 low", "choice",
         ["off", "low", "medium", "high", "max"]),
        ("llm.max_rounds", "一步最多调几次工具", "多步任务撞上限会只说半句；不限就不收尾", "choice",
         ["6", "12", "20", "0"], {"0": "不限制"}),
        ("llm.vision_max_side", "看图分辨率", "截图送给视觉模型前的最长边；越小越省 token，字小就看不清",
         "choice", ["768", "1024", "1280", "1600", "1920"]),
    ]),
    ("默认模型（主对话）", [
        ("llm.model", "模型名", "下面这三项是**兜底**：哪个用途没单独挂档案，用的就是它们",
         "text", None),
        ("llm.base_url", "服务地址", "任何 OpenAI 兼容服务；非本机的 http 地址会自动降为只读模式",
         "text", None),
        ("llm.api_key", "API Key", "留空表示不改动；也可以读环境变量", "password", None),
    ]),
    ("按用途挂模型（可选）", [
        ("llm.profiles", "多模型",
         "主对话 / 同意判定 / 看图 / 子代理 可以各挂一个模型（例如看图用一个带视觉的）",
         "models", None),
    ]),
    ("数据与文件", [
        ("paths.data_dir", "数据目录",
         "记忆、截图、标记、缓存、审计日志都收在这一个目录里；留空 = 程序目录下的 build",
         "datadir", None),
    ]),
    ("权限", [
        ("security.mode", "权限模式",
         "只读：只能查；标准：敏感操作先问你；放开：敏感操作直接做（关机和执行命令仍要确认）",
         "choice", ["read-only", "workspace-write", "danger-full-access"],
         {"read-only": "只读", "workspace-write": "标准", "danger-full-access": "放开"}),
        ("security.max_prompts_per_minute", "每分钟最多问几次",
         "防「反复弹确认把你问烦」：超过就一律拒绝，0 = 不限", "choice",
         ["3", "6", "10", "0"], {"0": "不限"}),
        ("security.max_same_action", "同一操作最多连着问几次",
         "防「盯着一个危险操作反复问，直到你手滑同意」", "choice",
         ["1", "3", "5", "0"], {"0": "不限"}),
        ("security.deny_tools", "禁止使用的工具",
         "填工具名（逗号隔开，例如 run_command、write_file）：这些工具任何模式下都直接拒绝",
         "text", None),
        ("security.always_confirm", "必须确认的工具",
         "在这些工具上额外要求确认（逗号隔开）。关机和执行命令本来就必须确认", "text", None),
        ("security.floor_tools", "放开模式下的底线",
         "「放开」模式下**仍然要确认**的工具。**写了就以你写的为准**（不再自动并上"
         "内建那几个）；想真的完全不问，就把这里清空并把下一项关掉 —— 不建议："
         "执行命令等于把电脑交出去", "text", None),
        ("security.keep_floor_when_empty", "清空底线时保留内置的那几个",
         "只在上面**留空**时起作用：开着（默认）= 仍然问执行命令/关机那几个；"
         "关掉 = 连它们也不再确认", "bool", None),
        ("security.allow_insecure", "允许明文 HTTP 模型地址",
         "关着时：非本机的 http 地址会自动降到只读（那种链路上任何人都能改写模型的回答）",
         "bool", None),
        ("security.audit", "记录审计日志", "build/audit.jsonl，记下每次敏感操作的参数与批没批",
         "bool", None),
    ]),
    ("外观", [
        ("ui.accent", "主题色", "换主色，整个界面跟着变；不用重启", "accent", None),
        ("ui.show_stats", "底部速览", "主界面底部的唤醒 / 算力 / 合成 / 声纹四格", "bool", None),
        ("ui.show_turn", "显示本轮问答", "主界面上显示刚才听到的提问和它的回复", "bool", None),
    ]),
    ("子代理", [
        ("agent.subagent_enabled", "启用子代理",
         "把「要好几步才做得完」的事丢到后台去做，你可以接着说别的", "bool", None),
        ("agent.subagent_max", "同时最多几个", "同时跑太多会一起变慢；不限制时内部仍留 20 个的硬上限",
         "choice", ["1", "2", "3", "5", "0"], {"0": "不限制"}),
        ("agent.subagent_rounds", "每个最多做几步", "步数用完它会先停下来说说查到什么；不限制时最多 60 步",
         "choice", ["4", "8", "12", "24", "0"], {"0": "不限制"}),
        ("agent.subagent_announce", "做完主动汇报", "闲下来时它会念一句结果；关掉就只记在对话里",
         "bool", None),
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
        # 警告卡**一直在布局里**，只切换显示与否。以前是"不是 chattts 就不创建"，
        # 而页面是建一次就缓存下来的 —— 用户把引擎换回 vits，那张卡还赖在顶上。
        self._engine_warning_card = self._engine_warning(values)
        self.body.addWidget(self._engine_warning_card)
        self.body.addWidget(self._voiceprint_card(values))
        for title, fields in SETTING_SECTIONS:
            self.body.addWidget(ui.section_title(title))
            card = ui.Card()
            for field in fields:
                key, label, hint, kind, options = field[:5]
                labels = field[5] if len(field) > 5 else None
                card.body.addWidget(self._row(key, label, hint, kind, options,
                                              values.get(key), labels))
            self.body.addWidget(card)
        self.body.addStretch(1)

    def _row(self, key: str, label: str, hint: str, kind: str,
             options: list[str] | None, value: Any,
             labels: dict[str, str] | None = None) -> QWidget:
        if kind == "voice":
            return self._voice_row(label, hint)
        if kind == "volume":
            return self._volume_row(label, hint, value)
        if kind == "accent":
            return self._accent_row(label, hint, value)
        if kind == "models":
            return self._models_row(label, hint)
        if kind == "datadir":
            return self._datadir_row(label, hint, value)

        if kind == "bool":
            control = ui.ToggleSwitch(checked=bool(value))
            control.toggled.connect(lambda checked, k=key: self._save(k, checked))
            self.widgets[key] = ("bool", control)
            return setting_row(label, hint, control)

        if kind == "choice":
            # 每一行可以自带"值 -> 人话"的映射（例如 max_rounds 的 0 显示成「不限制」）
            table = dict(THRESHOLD_LABELS)
            table.update(labels or {})
            shown_options = [table.get(o, o) for o in (options or [])]
            current = "" if value is None else str(value)
            shown = table.get(current, current)
            # **真值可能不在预设选项里**（追问窗口默认 6000ms、quality 档语速 1.06…）。
            # 不在的话控件会落到第 0 项 —— 界面显示的是假值，用户点一下还会把真配置
            # 改成那个假值（"追问窗口 0（回待命）"就是这么来的）。补进去即可。
            if shown and shown not in shown_options:
                shown_options = shown_options + [shown]
            control = ui.SegmentedControl(shown_options, shown, width=260)
            control.changed.connect(
                lambda shown_label, k=key, opts=options, tb=table:
                self._save(k, self._choice_value(shown_label, opts, tb)))
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

    @staticmethod
    def _using_chattts(values: dict) -> bool:
        return str(values.get("tts.engine") or "").strip().lower() == "chattts"

    def _refresh_engine_warning(self) -> None:
        card = getattr(self, "_engine_warning_card", None)
        if card is not None:
            card.setVisible(self._using_chattts(self.console.settings()))

    def refresh_engine_rows(self) -> None:
        """按最新配置把"引擎 / 算力 / 档位"这几行重画一遍。

        改了「资源档位」会连带把 tts.engine 换成 chattts（quality 档），
        只盯 tts.engine 直接变化的话，下拉框和提示行还停在旧值上 ——
        用户重启之后才发现引擎变了，事前一点提示都没有。
        """
        values = self.console.settings()
        for key in ("tts.engine", "speech.profile", "speech.device",
                    "speech.threads", "llm.reasoning_effort"):
            entry = self.widgets.get(key)
            if not entry:
                continue
            kind, control = entry
            value = values.get(key)
            if kind == "choice" and hasattr(control, "show_value"):
                # 分段控件（SegmentedControl）：**只挪显示、不发信号**，
                # 而且先把真值补成选项 —— 以前这里按 QComboBox 的 API 写
                # （count/itemData/setCurrentIndex），每次都 AttributeError 被吞掉，
                # 于是"合成引擎"那一行永远停在旧值，和弹出的 ChatTTS 警告卡自相矛盾。
                control.ensure_option(str(value))
                control.show_value(str(value))
            elif kind == "bool":
                control.setChecked(bool(value))

    def _engine_warning(self, values: dict) -> ui.Card:
        """ChatTTS 的警告卡：只在真的用它时才显示。

        它和 VITS 不是一个量级的东西：要显卡、吃 2 GB 显存、首次加载十几秒、
        合成速度大约 1 倍实时。这些事不提前说清楚，用户只会觉得"助手变卡了"。
        """
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
        card.setVisible(self._using_chattts(values))
        return card

    def _datadir_row(self, label: str, hint: str, value: Any) -> QWidget:
        """数据目录：能改、能打开，还有一句"现在到底用哪个"。"""
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(4)
        title = QLabel(label)
        title.setObjectName("RowTitle")
        box.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            sub.setWordWrap(True)
            box.addWidget(sub)

        row = QHBoxLayout()
        row.setSpacing(8)
        editor = QLineEdit(str(value or ""))
        editor.setPlaceholderText("留空 = " + str(self.console.snapshot().get(
            "paths", {}).get("default", "")))
        row.addWidget(editor, 1)
        save = ui.plain_button("保存")
        save.clicked.connect(lambda: self._save("paths.data_dir", editor.text().strip()))
        row.addWidget(save)
        open_button = ui.plain_button("打开目录")
        open_button.clicked.connect(self._open_data_dir)
        row.addWidget(open_button)
        # 出问题时要翻的就是这个文件；界面上的日志窗口一关就没了，
        # 所以这里给一个直接打开它的入口（省得用户自己去 logs/ 里找）。
        log_button = ui.plain_button("看运行日志", "activity")
        log_button.setToolTip("打开日志文件（里面有完整的操作过程和崩溃栈）")
        log_button.clicked.connect(self._open_log_file)
        row.addWidget(log_button)
        box.addLayout(row)

        self.data_note = QLabel()
        self.data_note.setObjectName("RowSubtitle")
        self.data_note.setWordWrap(True)
        self.data_note.setText(self._data_dir_note())
        box.addWidget(self.data_note)
        return wrap

    def _data_dir_note(self) -> str:
        info = self.console.snapshot().get("paths") or {}
        rows = [row for row in (info.get("items") or []) if row.get("exists")]
        here = "现在用：" + str(info.get("dir") or "")
        if rows:
            here += "（已有 " + "、".join(str(row["name"]) for row in rows[:6]) + "）"
        return here

    def _open_log_file(self) -> None:
        """打开今天的日志文件（用系统默认程序）。"""
        from .. import journal  # noqa: PLC0415
        from ..tools.windows import _launch  # noqa: PLC0415

        path = journal.file_path()
        if not path.is_file():
            self.console.log("[ui] 今天还没有日志：" + str(path))
            return
        if not _launch(str(path)):
            self.console.log("[ui] 打不开日志文件，路径是 " + str(path))
            return
        self.console.log("[ui] 已打开日志：" + str(path))

    def _open_data_dir(self) -> None:
        from .. import paths  # noqa: PLC0415

        target = paths.set_data_dir(self.console.cfg.paths.data_dir)
        result = self.console.open_path_in_shell(target)
        self.console.log("[ui] " + result)
        if hasattr(self, "data_note"):
            self.data_note.setText(self._data_dir_note())

    def _models_row(self, label: str, hint: str) -> QWidget:
        """多模型：一行按钮 + 一句「现在是怎么挂的」。"""
        wrap = QWidget()
        box = QVBoxLayout(wrap)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(4)
        title = QLabel(label)
        title.setObjectName("RowTitle")
        box.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            sub.setWordWrap(True)
            box.addWidget(sub)
        self.models_note = QLabel()
        self.models_note.setObjectName("RowSubtitle")
        self.models_note.setWordWrap(True)
        self.models_note.setText(models_dialog.describe_routes(self.console.cfg.llm))
        box.addWidget(self.models_note)
        button = ui.plain_button("配置多模型…", "settings")
        button.clicked.connect(self.open_models)
        row = QHBoxLayout()
        row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)
        return wrap

    def open_models(self) -> None:
        dialog = models_dialog.ProfilesDialog(self.console, self)
        if dialog.exec():
            self.refresh_models_note()

    def refresh_models_note(self) -> None:
        note = getattr(self, "models_note", None)
        if note is None:
            return
        text = models_dialog.describe_routes(self.console.cfg.llm)
        if note.text() != text:
            note.setText(text)

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
        # 只关心试听、录音、多模型说明这三处：其余地方由各自的控件自己刷
        self._paint_audition(self.console.audition_status())
        self._paint_voice_progress()
        # 多模型对话框保存时是直接写配置的，页面收不到通知；
        # 与其加一套信号，不如每次 tick 对一下文本（一行字符串比较，开销可忽略）
        self.refresh_models_note()

    def _paint_voice_progress(self) -> None:
        """录音时显示「还剩几秒 / 有没有听到人声」。"""
        live = getattr(self, "voice_live", None)
        if live is None or not live.isVisible():
            return
        agent = self.console.agent
        voice = getattr(agent, "voiceprint", None)
        if voice is None:
            return
        info = voice.progress()
        if info.get("state") != "recording":
            self.voice_progress.setText("")
            return
        level = float(info.get("level") or 0.0)
        self.voice_progress.setText(
            "还剩 " + str(info.get("left", 0)) + " 秒　已听到 "
            + str(info.get("speech", 0)) + " 秒说话声")
        if level > 0.06:
            live.setText("有声音")
            live.set_color(theme.GREEN)
        else:
            live.setText("听…")
            live.set_color(theme.DIM)

    @staticmethod
    def _choice_value(shown: str, options: list[str] | None,
                      table: dict[str, str] | None = None) -> Any:
        lookup = table if table is not None else THRESHOLD_LABELS
        for option in options or []:
            if lookup.get(option, option) == shown:
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
        # 换了「资源档位」也会连带换掉 tts.engine（quality 档 → chattts），
        # 只盯 tts.engine 的话那张 ChatTTS 警告卡不会弹、"合成引擎"那一行还
        # 显示着旧引擎，用户重启后才发现起不来，事前毫无提示。
        if {"tts.engine", "speech.profile"} & set(updates or {}):
            self._refresh_engine_warning()
            try:
                self.refresh_engine_rows()
            except Exception as exc:  # noqa: BLE001 - 刷新失败不该影响保存
                self.console.log("[ui] 刷新引擎显示失败：" + str(exc)[:80])
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

        # 录音引导：写清楚"说什么、说多久、录完会检查什么"。
        # 不写的话用户往往对着麦克风愣一下、或者只说一个"喂"，录进一段静音。
        guide = QLabel(
            "录的时候请用平常的音量和语速说一句完整的话，例如：\n"
            "　　「今天天气不错，我想听点音乐」\n"
            "说够 1.2 秒就算数（最长录 4 秒）。录完会先体检："
            "没声音、太短、离麦太近爆音，都会让你重录，不会把废录音存进档案。"
        )
        guide.setObjectName("RowSubtitle")
        guide.setWordWrap(True)
        card.body.addWidget(guide)

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

        # 录音时的实时反馈：还剩几秒、有没有听到人声
        live = QHBoxLayout()
        live.setSpacing(8)
        self.voice_progress = QLabel("")
        self.voice_progress.setObjectName("Value")
        live.addWidget(self.voice_progress)
        self.voice_live = ui.Pill("待机", theme.DIM)
        self.voice_live.setVisible(False)
        live.addWidget(self.voice_live)
        live.addStretch(1)
        card.body.addLayout(live)

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
        seconds = 4.0
        self.voice_hint.setText("现在开始说 —— 用平常的音量和语速，说一句完整的话。")
        self.voice_live.setVisible(True)
        self.voice_live.setText("听…")
        self.voice_live.set_color(theme.DIM)
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
            self.voice_progress.setText("")
            self.voice_live.setVisible(False)
            if action == "enroll":
                if result.get("ok"):
                    self.voice_hint.setText(
                        "录好了（第 " + str(result.get("count", 1)) + " 条，"
                        + str(result.get("speech_seconds", 0)) + " 秒说话声）。"
                        + "再录一两次会更稳。")
                else:
                    self.voice_hint.setText("这次没录成：" + str(result.get("error")))
            else:
                if result.get("ok"):
                    verdict = "匹配" if result.get("allowed") else "不匹配"
                    self.voice_hint.setText(verdict + "　相似度 " + str(result.get("score"))
                                            + "　" + str(result.get("note")))
                else:
                    self.voice_hint.setText("这次没测成：" + str(result.get("error")))
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

    #: 后台录音结束（结果回界面线程）
    recorded = pyqtSignal(dict)

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(console, parent)
        self.inputs: list[dict] = []
        self.outputs: list[dict] = []
        self._recording = False
        self._record_left = 0
        self._record_timer = QTimer(self)
        self._record_timer.setInterval(1000)
        self._record_timer.timeout.connect(self._on_record_tick)
        self.recorded.connect(self._on_recorded)
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
        for combo, items, selected, kind in (
            (self.combo_in, self.inputs, current.get("input"), "input"),
            (self.combo_out, self.outputs, current.get("output"), "output"),
        ):
            combo.clear()
            combo.addItem("系统默认", None)
            for device in items:
                combo.addItem("[" + str(device["index"]) + "] " + device["name"], device["index"])
            combo.setCurrentIndex(self._pick(combo, selected, kind))

    @staticmethod
    def _pick(combo, selected, kind: str) -> int:
        """按配置里写的设备选中下拉框里的那一项。

        配置里的值有三种写法：序号、**名字子串**、null（resolve_device 都认）。
        以前只按序号 findData：配置里写的是名字时找不到，下拉框悄悄退回
        「系统默认」，用户再点一下「应用」就把名字冲成了 null —— 麦克风被
        改成系统默认，而界面上从头到尾没提过一句。
        """
        index = combo.findData(selected)
        if index < 0 and isinstance(selected, str) and selected.strip():
            try:
                from ..audio import resolve_device

                number = resolve_device(selected, kind)
            except Exception:  # noqa: BLE001 - 解析不出来就当"列表里没有"
                number = None
            if number is not None:
                index = combo.findData(number)
        if index < 0 and selected not in (None, ""):
            # 列表里确实没有（设备拔了 / 名字写错了）：也**保留**它，
            # 别让"应用"把配置抹掉 —— 插回去还能用。
            combo.addItem("（配置里写的：" + str(selected) + "）", selected)
            index = combo.count() - 1
        return index if index >= 0 else 0

    def apply(self) -> None:
        result = self.console.update_config({
            "audio.input_device": self.combo_in.currentData(),
            "audio.output_device": self.combo_out.currentData(),
        })
        if not result.get("ok"):
            self.hint.setText(str(result.get("error")))
            return
        was_running = bool(self.console.agent is not None and self.console.agent.running)
        if not was_running:
            # 引擎本来就没在跑：只保存，**不要**顺手把麦克风打开 ——
            # 用户点的是"应用并重启引擎"，不是"开始监听"。以前这里会
            # 静悄悄启动整套监听，等于没问过就开麦。
            self.hint.setText("已保存，下次启动监听时生效。")
            return
        self.console.stop_engine()
        self.console.start_engine()
        self.hint.setText("已保存，正在重启引擎（模型加载要几秒）。")

    def record_test(self) -> None:
        if self.console.agent is not None and self.console.agent.running:
            self.hint.setText("正在监听中，请先停止引擎。")
            return
        if self._recording:
            self.hint.setText("正在录音，等这一遍说完。")
            return
        # **录音要占住最长 12 秒**（外加可能几秒的模型加载）：同步跑的话事件
        # 循环被占死，连"请说话"这句提示都画不出来，用户对着一个卡住的窗口说话。
        # 丢到后台线程，期间用定时器把提示语往前推。
        self._recording = True
        self._record_left = 12
        self.hint.setText("请说话，最长 12 秒…")
        self._record_timer.start(1000)

        def work() -> None:
            result = self.console.record_once(12.0)
            self.recorded.emit(result)

        threading.Thread(target=work, name="ui-record", daemon=True).start()

    def _on_record_tick(self) -> None:
        if self._record_left <= 0:
            return
        self._record_left -= 1
        if self._recording:
            self.hint.setText("正在录音…还剩 " + str(self._record_left) + " 秒")

    def _on_recorded(self, result: dict) -> None:
        self._recording = False
        self._record_timer.stop()
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
        #: 环境卡每行的值标签（标题 → 标签）。行只建一次，on_tick 只改文字。
        self._env_labels: dict[str, QLabel] = {}
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
        """刷新环境卡。

        这里**只改文字**，控件只在第一次建起来：以前每次 tick（300ms）都把整张卡
        拆掉重建 —— 一秒钟三次析构 + 构造十来个 QLabel，还顺手把滚动位置和
        选中状态一起丢掉，鼠标停在"复制路径"那种交互上就更明显。
        """
        from .. import __version__

        self.subtitle.setText("版本 " + __version__ + "　·　本地语音 · 可选 LLM 大脑")
        status = snapshot.get("status", {})
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
        if not self._env_labels:
            self._build_env_rows([title for title, _ in rows])
        for title, value in rows:
            label = self._env_labels[title]
            if label.text() != value:
                label.setText(value)

    def _build_env_rows(self, titles: list[str]) -> None:
        """环境卡的行只建一次，之后 on_tick 只改文字。"""
        from PyQt6.QtWidgets import QLabel as _QLabel

        for title in titles:
            line = QWidget()
            box = QHBoxLayout(line)
            box.setContentsMargins(0, 3, 0, 3)
            label = _QLabel(title)
            label.setObjectName("RowTitle")
            label.setFixedWidth(56)
            box.addWidget(label)
            value_label = _QLabel("")
            value_label.setObjectName("Mono")
            value_label.setWordWrap(True)
            box.addWidget(value_label, 1)
            self._env_labels[title] = value_label
            self.env_card.body.addWidget(line)
