# -*- coding: utf-8 -*-
"""缺语音模型时的向导窗口：**自动下载** 还是 **指定已有目录**。

为什么要有这个窗口：第一次运行（或者换了台机器、清了缓存）最常见的一步就是
"模型不在"，而以前不管是桌面版还是打包版都只回一句
"运行 python scripts/download_models.py" —— 打包版里既没有 scripts/ 也没有
python，用户就卡在这儿了。现在当场就能下，或者把已有的模型目录指过来。

自动下载装到 <数据目录>/models，下载缓存放 <数据目录>/downloads：
和日志、截图、记忆都在同一个父目录下，备份/搬家只要动一个目录。
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from .. import models_setup
from ..console import Console

_HINT = ("模型装到「自动下载目录」后，下次启动会自动找到它；"
         "指到已有目录的话，这个选择会写进 config.yaml（paths.models_dir）。")


class ModelSetupDialog(QDialog):
    """缺模型时问用户怎么办。"""

    progress = pyqtSignal(str, int, int)   # key, done, total
    finished_download = pyqtSignal(dict)

    def __init__(self, console: Console, reason: str = "", parent=None) -> None:
        super().__init__(parent)
        self.console = console
        self._downloading = False
        self._cancel = False
        self._bytes_total = 0
        self.setWindowTitle("缺少语音模型")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        head = QLabel(reason or "这台机器上还没找到语音模型，先选一种方式把模型准备好。")
        head.setWordWrap(True)
        layout.addWidget(head)

        info = models_setup.status(console.cfg.models_dir if console.cfg else Path("models"))
        self.auto_dir = Path(console.cfg.auto_models_dir) if console.cfg else Path("models")
        lines = []
        for key in info["missing"]:
            spec = models_setup.MODEL_SPECS[key]
            lines.append("· " + spec.label + "（约 " + str(spec.size_mb) + " MB）")
        self.detail = QLabel("缺少 " + str(len(info["missing"])) + " 个模型：\n" + "\n".join(lines)
                             + "\n\n自动下载到：" + str(self.auto_dir)
                             + "\n（和日志、下载缓存在同一个目录下）")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setVisible(False)
        layout.addWidget(self.bar)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(120)
        self.log_view.setVisible(False)
        layout.addWidget(self.log_view)

        self.hint = QLabel(_HINT)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("Hint")
        layout.addWidget(self.hint)

        row = QHBoxLayout()
        self.btn_download = QPushButton("自动下载")
        self.btn_download.setDefault(True)
        self.btn_download.clicked.connect(self.start_download)
        self.btn_pick = QPushButton("选择已有目录…")
        self.btn_pick.clicked.connect(self.pick_dir)
        self.btn_close = QPushButton("稍后")
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_download)
        row.addWidget(self.btn_pick)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        self.progress.connect(self._on_progress)
        self.finished_download.connect(self._on_finished)

    # ── 自动下载 ──
    def start_download(self) -> None:
        if self._downloading:
            self._cancel = True
            self.btn_download.setText("正在停止…")
            return
        self._downloading = True
        self._cancel = False
        self.btn_download.setText("取消下载")
        self.btn_pick.setEnabled(False)
        self.btn_close.setEnabled(False)
        self.log_view.setVisible(True)
        self.bar.setVisible(True)
        self._bytes_total = 0
        self._log("开始下载到 " + str(self.auto_dir))

        def work() -> None:
            result = models_setup.download(
                self.auto_dir,
                include_optional=False,
                cache=Path(self.console.cfg.auto_models_dir).parent / "downloads"
                if self.console.cfg else None,
                log=self._log,
                on_progress=lambda key, done, total: self.progress.emit(key, done, total),
                cancelled=lambda: self._cancel)
            self.finished_download.emit(result)

        threading.Thread(target=work, name="models-download", daemon=True).start()

    def _log(self, line: str) -> None:
        # 下载线程里调过来的：只往控件塞文本，Qt 会自己排队
        try:
            self.log_view.append(str(line))
        except RuntimeError:   # 窗口已经关了
            pass

    def _on_progress(self, key: str, done: int, total: int) -> None:
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(done * 100 / total))
            label = models_setup.MODEL_SPECS[key].label
            self.bar.setFormat(label + "  %p%")
        else:
            self.bar.setRange(0, 0)      # 不知道总长就转圈

    def _on_finished(self, result: dict) -> None:
        self._downloading = False
        self.btn_download.setText("自动下载")
        self.btn_pick.setEnabled(True)
        self.btn_close.setEnabled(True)
        if result.get("failed"):
            self.hint.setText("这些没下成功：" + "、".join(result["failed"])
                              + "。可以再点一次「自动下载」，下好的不会重下。")
            return
        self.bar.setValue(100)
        self.console.log("[ui] 模型已下载到 " + str(result.get("dir")))
        self.console.reload_config()
        self.hint.setText("模型齐了，可以直接启动监听。")
        self.accept()

    # ── 指定已有目录 ──
    def pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择模型目录（里面有 silero_vad.onnx）")
        if not chosen:
            return
        root = models_setup.resolve_models_root(Path(chosen))
        if root is None:
            self.hint.setText("这个目录里没找到模型文件。选那个能看到 silero_vad.onnx、"
                              "sherpa-onnx-* 的文件夹（也可以选它们的上一层）。")
            return
        result = self.console.update_config({"paths.models_dir": str(root)})
        if not result.get("ok"):
            self.hint.setText(str(result.get("error") or "写配置失败"))
            return
        self.console.reload_config()
        missing = list(self.console.cfg.missing_models) if self.console.cfg else []
        if missing:
            self.hint.setText("这个目录里还缺：" + "、".join(missing))
            return
        self.console.log("[ui] 模型目录已设为 " + str(root))
        self.accept()
