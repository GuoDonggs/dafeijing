# -*- coding: utf-8 -*-
"""多模型配置：给每个用途（主对话 / 判定 / 看图 / 子代理）挂不同的模型档案。

为什么要做成界面而不是让人手写 YAML：这套东西的默认值是「什么都不写也能跑」，
一旦要动它，通常是"主对话想用便宜快的、看图要挂个带视觉的" —— 属于典型
的边试边改，手写 YAML 很容易把 key 写错、把路由指向一个不存在的档案，
而错法的表现是"它突然不说话了"，很难查。所以这里把能填的都摊开：

- 上面一排是**路由**：哪个用途用哪个档案（留空 = 用默认那一组）；
- 下面是**档案列表**：每个档案自己一套地址 / 模型 / 密钥 / 思考程度 / 是否识图。

档案里没写的字段会回落到顶层的 llm.* 设置，所以"只想给看图换个模型"时，
新建一个档案、只填模型名和 base_url 就够了。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..console import Console
from . import components as ui
from . import theme

#: 用途 → 界面上的一句话说明
PURPOSES = [
    ("chat", "主对话", "平时说话、调工具都走它"),
    ("judge", "同意判定", "只回答「是 / 否」，用最便宜的模型就行"),
    ("vision", "看图", "必须支持图片输入，否则 look_at_screen 用不了"),
    ("subagent", "子代理", "后台跑多步任务用的模型"),
]

#: 档案里可以填的字段（和 config.py 的 LlmCfg.resolve 一一对应）
EFFORTS = ["", "off", "low", "medium", "high", "max"]
EFFORT_LABELS = {"": "跟随默认", "off": "不思考", "low": "浅", "medium": "中",
                 "high": "深", "max": "最深"}


class ProfileCard(QFrame):
    """一个模型档案的编辑卡片。"""

    def __init__(self, name: str, data: dict, on_remove) -> None:  # noqa: ANN001
        super().__init__()
        self.setObjectName("Card")
        self.removed = False
        body = QVBoxLayout(self)
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.name = QLineEdit(name)
        self.name.setPlaceholderText("档案名，例如 fast")
        head.addWidget(self.name, 1)
        remove = ui.plain_button("删除")
        remove.clicked.connect(lambda: on_remove(self))
        head.addWidget(remove)
        body.addLayout(head)

        self.fields: dict[str, Any] = {}
        for key, label, placeholder in (
            ("model", "模型名", "deepseek-chat"),
            ("base_url", "服务地址", "https://api.deepseek.com/v1"),
            ("api_key", "API Key", "留空 = 用顶层那把密钥"),
        ):
            row = QHBoxLayout()
            row.setSpacing(8)
            caption = QLabel(label)
            caption.setObjectName("CardSubtitle")
            caption.setFixedWidth(64)
            row.addWidget(caption)
            editor = QLineEdit(str(data.get(key) or ""))
            editor.setPlaceholderText(placeholder)
            if key == "api_key":
                editor.setEchoMode(QLineEdit.EchoMode.Password)
            row.addWidget(editor, 1)
            body.addLayout(row)
            self.fields[key] = editor

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        effort_label = QLabel("思考程度")
        effort_label.setObjectName("CardSubtitle")
        effort_label.setFixedWidth(64)
        bottom.addWidget(effort_label)
        self.effort = QComboBox()
        for value in EFFORTS:
            self.effort.addItem(EFFORT_LABELS[value], value)
        current = str(data.get("reasoning_effort") or "")
        index = self.effort.findData(current)
        self.effort.setCurrentIndex(index if index >= 0 else 0)
        bottom.addWidget(self.effort, 1)
        bottom.addWidget(QLabel("温度"))
        self.temperature = QLineEdit(str(data.get("temperature", "")))
        self.temperature.setFixedWidth(60)
        self.temperature.setPlaceholderText("0.3")
        bottom.addWidget(self.temperature)
        self.vision = ui.ToggleSwitch(checked=bool(data.get("vision")))
        bottom.addWidget(QLabel("会看图"))
        bottom.addWidget(self.vision)
        body.addLayout(bottom)

    def collect(self) -> tuple[str, dict] | None:
        """读出这一行的内容；名字为空表示这行作废（返回 None）。"""
        name = self.name.text().strip()
        if not name:
            return None
        data: dict = {}
        for key, editor in self.fields.items():
            value = editor.text().strip()
            if value:
                data[key] = value
        effort = self.effort.currentData()
        if effort:
            data["reasoning_effort"] = effort
        temperature = self.temperature.text().strip()
        if temperature:
            try:
                data["temperature"] = float(temperature)
            except ValueError:
                data["temperature"] = 0.3
        if self.vision.isChecked():
            data["vision"] = True
        return name, data


class ProfilesDialog(QDialog):
    """多模型配置窗口。"""

    def __init__(self, console: Console, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.console = console
        self.setWindowTitle("多模型配置")
        self.setStyleSheet(theme.qss())
        self.setMinimumWidth(620)
        self.cards: list[ProfileCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(self._header())

        self.routes: dict[str, QComboBox] = {}
        layout.addWidget(self._routes_card())

        row = QHBoxLayout()
        title = QLabel("模型档案")
        title.setObjectName("CardTitle")
        row.addWidget(title)
        row.addStretch(1)
        add = ui.plain_button("添加档案", "plus")
        add.clicked.connect(lambda: self.add_card("", {}))
        row.addWidget(add)
        layout.addLayout(row)

        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(8)
        layout.addLayout(self.list_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.load()

    # ── 构建 ──
    def _header(self) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        title = QLabel("多模型配置")
        title.setObjectName("CardTitle")
        column.addWidget(title)
        note = QLabel(
            "给不同用途挂不同的模型：平时说话用便宜的，看图必须挂带视觉的。\n"
            "档案里没填的字段会跟随上面的默认设置；一个档案都不建，就等于全部用默认。")
        note.setObjectName("RowSubtitle")
        note.setWordWrap(True)
        column.addWidget(note)
        return box

    def _routes_card(self) -> QWidget:
        card = ui.Card()
        for purpose, label, hint in PURPOSES:
            row = QHBoxLayout()
            row.setSpacing(8)
            caption = QLabel(label)
            caption.setFixedWidth(64)
            row.addWidget(caption)
            combo = QComboBox()
            row.addWidget(combo, 1)
            sub = QLabel(hint)
            sub.setObjectName("RowSubtitle")
            row.addWidget(sub, 2)
            card.body.addLayout(row)
            self.routes[purpose] = combo
        return card

    def add_card(self, name: str, data: dict) -> None:
        card = ProfileCard(name, data, self.remove_card)
        self.cards.append(card)
        self.list_box.addWidget(card)
        self.refresh_routes()

    def remove_card(self, card: ProfileCard) -> None:
        card.removed = True
        self.cards.remove(card)
        card.setParent(None)
        card.deleteLater()
        self.refresh_routes()

    def refresh_routes(self) -> None:
        """路由下拉框跟着档案列表走（档案改名、删掉都要重新填）。"""
        names = [card.name.text().strip() for card in self.cards if card.name.text().strip()]
        for purpose, combo in self.routes.items():
            previous = combo.currentData() or ""
            combo.clear()
            combo.addItem("跟随默认", "")
            for name in names:
                combo.addItem(name, name)
            index = combo.findData(previous)
            combo.setCurrentIndex(index if index >= 0 else 0)

    # ── 读 / 写 ──
    def load(self) -> None:
        llm = self.console.cfg.llm
        routes = dict(getattr(llm, "routes", None) or {})
        for name, data in sorted((getattr(llm, "profiles", None) or {}).items()):
            self.add_card(str(name), dict(data or {}))
        if not self.cards:
            self.add_card("", {})
        self.refresh_routes()
        for purpose, combo in self.routes.items():
            index = combo.findData(str(routes.get(purpose) or ""))
            combo.setCurrentIndex(index if index >= 0 else 0)

    def collect(self) -> tuple[dict, dict, str]:
        profiles: dict = {}
        for card in self.cards:
            if card.removed:
                continue
            item = card.collect()
            if item is None:
                continue
            name, data = item
            if name in profiles:
                return {}, {}, "档案名重复了：" + name
            profiles[name] = data
        routes: dict = {}
        for purpose, combo in self.routes.items():
            picked = combo.currentData()
            if picked:
                routes[purpose] = picked
        for name in routes.values():
            if name not in profiles:
                return {}, {}, "路由指向了不存在的档案：" + str(name)
        return profiles, routes, ""

    def save(self) -> None:
        profiles, routes, problem = self.collect()
        if problem:
            box = QMessageBox(self)
            box.setWindowTitle("配置不完整")
            box.setText(problem)
            box.exec()
            return
        result = self.console.update_config({"llm.profiles": profiles, "llm.routes": routes})
        if not result.get("ok"):
            box = QMessageBox(self)
            box.setWindowTitle("保存失败")
            box.setText(str(result.get("error")))
            box.exec()
            return
        self.console.log("[ui] 多模型配置已保存：" + str(len(profiles)) + " 个档案，"
                         + str(len(routes)) + " 条路由")
        self.accept()


def describe_routes(llm) -> str:  # noqa: ANN001
    """一句话说明当前的路由，给设置页那行提示用。"""
    routes = dict(getattr(llm, "routes", None) or {})
    profiles = dict(getattr(llm, "profiles", None) or {})
    if not profiles:
        return "只有一个模型（" + str(getattr(llm, "model", "") or "默认") + "）"
    parts = []
    for purpose, label, _hint in PURPOSES:
        name = str(routes.get(purpose) or "")
        if name:
            parts.append(label + " → " + name)
    return "；".join(parts) if parts else "已建 " + str(len(profiles)) + " 个档案，都没挂上用途"


