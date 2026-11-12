"""Per-book trial and selection, before starting the production queue."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QProcess, QProcessEnvironment, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QImage
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
)

from controller import ROOT, read_json
from backend.fileio import atomic_json
from preview_panel import ImageBox


class TrialDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("选页与对比")
        self.resize(1200, 800)
        demand = read_json(owner.job / "state/demand.json")
        self.books = [
            (key, book) for key, book in demand["items"].items() if book.get("needs_upscale")
        ]
        self.assigned = read_json(owner.job / "state/output-paths.json") or {}
        self.source = Path(demand["config"]["source"])
        self.output = Path(demand["config"]["output"]) / self.source.name
        stored = read_json(owner.job / "state/default-mode.json") or {}
        stored_mode = stored.get("mode")
        self.default = (
            stored_mode
            if stored_mode in ("flat", "manga", "color")
            else {"平涂": "flat", "全彩": "color"}.get(owner.bw_mode.get(), "manga")
        )
        self.plan = {
            "page_threshold": demand["config"]["threshold"],
            "height_metric": demand["config"].get("height_metric", "fit-page"),
            "page_height": owner.threshold_scale.value(),
            "skip_above_recommended": owner.skip_large.get(),
        }
        self.temp = TemporaryDirectory(prefix="manga-trial-")
        from backend.runtime_config import load_runtime

        self.runtime = load_runtime(ROOT / "runtime.json")
        self.cache = {}
        self.request = None
        self.worker = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("MANGA_OWNER_PID", str(os.getpid()))
        env.insert("PYTHONIOENCODING", "utf-8")
        self.worker.setProcessEnvironment(env)
        self.worker.finished.connect(self.finished_trial)
        self.worker.errorOccurred.connect(self.process_error)
        self.run_keys = []
        self.active_book = -1

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        self.queue = QListWidget()
        self.queue.setFixedWidth(260)
        body.addWidget(self.queue)
        self.original, self.result = ImageBox(), ImageBox()
        self.original.changed.connect(self.result.set_view)
        self.result.changed.connect(self.original.set_view)
        for title, box in (("原图", self.original), ("预览结果", self.result)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            column.addWidget(box, 1)
            column.addWidget(box.info)
            body.addLayout(column, 1)
        layout.addLayout(body, 1)
        strip = QHBoxLayout()
        self.thumbnails = []
        for _ in range(5):
            button = QToolButton()
            button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(64, 80))
            button.clicked.connect(
                lambda checked=False, b=button: self.page.setValue(b.property("page"))
            )
            button.hide()
            self.thumbnails.append(button)
            strip.addWidget(button)
        strip.addStretch()
        layout.addLayout(strip)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("页码"))
        self.page = QSpinBox()
        self.page.setRange(1, 1)
        toolbar.addWidget(self.page)
        self.mode = QComboBox()
        for title, mode in (
            ("使用批次默认", ""),
            ("传统日漫", "manga"),
            ("平涂", "flat"),
            ("全彩", "color"),
        ):
            self.mode.addItem(title, mode)
        toolbar.addWidget(self.mode)
        self.generate = QPushButton("生成预览")
        toolbar.addWidget(self.generate)
        self.native = QPushButton("结果原尺寸")
        toolbar.addWidget(self.native)
        self.hold = QPushButton("按住看原图")
        self.hold.pressed.connect(self.hold_original)
        self.hold.released.connect(self.show_result)
        toolbar.addWidget(self.hold)
        self.open_output = QPushButton("打开成品")
        self.open_output.clicked.connect(self.open_current_output)
        toolbar.addWidget(self.open_output)
        toolbar.addStretch()
        self.confirm = QPushButton("确认并下一包")
        toolbar.addWidget(self.confirm)
        self.skip = QPushButton("暂跳过")
        self.skip.clicked.connect(lambda: self.queue.setCurrentRow(self.active_book + 1))
        toolbar.addWidget(self.skip)
        self.start_batch = QPushButton()
        toolbar.addWidget(self.start_batch)
        layout.addLayout(toolbar)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.destination = QLabel()
        self.destination.setWordWrap(True)
        layout.addWidget(self.destination)

        for _ in self.books:
            self.queue.addItem("")
        self.refresh_queue()
        self.queue.currentRowChanged.connect(self.select_book)
        self.page.valueChanged.connect(self.select_page)
        self.mode.currentIndexChanged.connect(self.show_result)
        self.generate.clicked.connect(self.generate_trial)
        self.confirm.clicked.connect(self.confirm_book)
        self.start_batch.clicked.connect(self.accept_batch)
        self.native.clicked.connect(self.native_size)
        self.queue.setCurrentRow(
            next((i for i in range(len(self.books)) if not self.confirmed(i)), 0)
        )
        self.update_buttons()

    def preference_path(self, index):
        return self.owner.job / "jobs" / self.books[index][0] / "status/book-preference.json"

    def preference(self, index):
        preference = read_json(self.preference_path(index)) or {}
        if not preference.get("mode"):
            confirmed_mode = preference.get("trial", {}).get("mode")
            if confirmed_mode in ("flat", "manga", "color"):
                preference["mode"] = confirmed_mode
        return preference

    def effective(self, override):
        return override or self.default

    def confirmed(self, index):
        preference = self.preference(index)
        return preference.get("trial") == {
            "signature": self.books[index][1]["signature"],
            "plan": self.plan,
            "mode": self.effective(preference.get("mode")),
        }

    def refresh_queue(self):
        for i, (_, book) in enumerate(self.books):
            mode = self.preference(i).get("mode")
            effective = self.effective(mode)
            name = {"flat": "平涂", "color": "全彩"}.get(effective, "传统日漫")
            status = "已确认" if self.confirmed(i) else "待确认"
            failure = read_json(self.preference_path(i).with_name("status.json")) or {}
            if failure.get("state") == "failed":
                page = failure.get("page")
                status += (
                    f" · 第 {page} 页疑似白页"
                    if page and failure.get("code") == "unexpected_blank_page"
                    else " · 上次处理失败"
                )
            scope = "单包设置" if mode else "批次默认"
            item = self.queue.item(i)
            item.setText(f"{Path(book['source']).name}\n{status} · {name}（{scope}）")
            item.setToolTip(book["source"])

    def output_path(self, index):
        key, book = self.books[index]
        name = self.assigned.get(key)
        return self.output / (
            name or Path(book["source"]).relative_to(self.source).with_suffix(".cbz")
        )

    def open_current_output(self):
        path = self.output_path(self.active_book)
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def hold_original(self):
        if self.original.picture and self.result.picture:
            self.result.set_levels(self.original.levels, self.original.byte_count)

    def select_book(self, index):
        self.active_book = index
        self.original.reset()
        self.result.reset()
        for button in self.thumbnails:
            button.hide()
        if index < 0:
            self.update_buttons()
            return
        path = self.output_path(index)
        self.destination.setText(str(path))
        self.open_output.setEnabled(path.is_file())
        mode = self.preference(index).get("mode") or ""
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.page.blockSignals(True)
        self.page.setRange(1, 1)
        self.page.blockSignals(False)
        self.launch(None, None)

    def select_page(self):
        self.original.reset()
        self.result.reset()
        self.launch(self.page.value() - 1, None)

    def generate_trial(self):
        self.launch(self.page.value() - 1, self.effective(self.mode.currentData()))

    def launch(self, page, mode):
        if self.request is not None or self.active_book < 0:
            return
        index = self.active_book
        book = self.books[index][1]
        self.request = (index, page, mode)
        request_path = Path(self.temp.name) / "request.json"
        atomic_json(
            request_path,
            {
                "source": book["source"],
                "signature": book["signature"],
                "page": page,
                "mode": mode,
                "plan": self.plan,
                "book": index,
            },
        )
        self.status.setText("正在生成预览，可以缩放和拖动原图。" if mode else "正在读取漫画…")
        self.update_buttons()
        self.worker.start(
            self.runtime["python"],
            [
                "-B",
                str(ROOT / "backend/trial_page.py"),
                str(request_path),
            ],
        )

    def finished_trial(self, code, _):
        index, page, mode = self.request
        self.request = None
        stdout = bytes(self.worker.readAllStandardOutput()).decode("utf-8", "replace").strip()
        stderr = bytes(self.worker.readAllStandardError()).decode("utf-8", "replace").strip()
        if code:
            lines = stderr.splitlines()
            self.status.setText("预览失败 · " + (lines[-1] if lines else str(code)))
        elif page is None:
            total = json.loads(stdout)
            if total:
                self.page.setMaximum(total)
                self.select_page()
                return
        else:
            path = Path(self.temp.name) / "image.png"
            if mode:
                candidate = path.with_name(f"{index}-{page}-{mode}.png")
                path.replace(candidate)
                self.cache[index, page, mode] = (candidate, stdout.splitlines()[-1])
            else:
                info = read_json(path.with_name("image-info.json"))
                self.original.set_levels([QImage(str(path))], info["original_bytes"])
                pages = range(max(0, page - 2), min(self.page.maximum(), page + 3))
                for offset, button in enumerate(self.thumbnails):
                    button.setVisible(offset < len(pages))
                    if offset < len(pages):
                        nearby = pages[offset]
                        icon = path.with_name(f"thumb-{index}-{nearby}.png")
                        button.setIcon(QIcon(str(icon)))
                        button.setText(str(nearby + 1))
                        button.setProperty("page", nearby + 1)
            self.show_result()
        self.update_buttons()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.request = None
            self.status.setText("无法启动预览进程。")
            self.update_buttons()

    def candidate(self):
        return self.cache.get(
            (self.active_book, self.page.value() - 1, self.effective(self.mode.currentData()))
        )

    def show_result(self):
        self.result.reset()
        candidate = self.candidate()
        if candidate and self.original.picture:
            self.result.set_levels([QImage(str(candidate[0]))], candidate[0].stat().st_size)
            self.result.info.setText(self.result.info.text() + " · PNG 预览")
            self.result.set_view(self.original.zoom, self.original.cx, self.original.cy)
            self.status.setText(
                {
                    "bw": "预览已生成。",
                    "color": "此页走彩页模型，预览已生成。",
                    "passthrough": "此页按当前设置保留原尺寸，预览已生成。",
                }[candidate[1]]
            )
        elif self.request is None:
            self.status.setText("选择页面和风格，再生成预览。")
        self.update_buttons()

    def update_buttons(self):
        idle = self.request is None
        self.queue.setEnabled(idle)
        self.skip.setEnabled(idle and 0 <= self.active_book < len(self.books) - 1)
        for button in self.thumbnails:
            button.setEnabled(idle)
        self.page.setEnabled(idle and self.active_book >= 0)
        self.mode.setEnabled(idle and self.active_book >= 0)
        self.generate.setEnabled(idle and bool(self.original.picture) and not self.candidate())
        candidate = self.candidate()
        self.confirm.setEnabled(idle and bool(self.result.picture) and bool(candidate))
        self.native.setEnabled(bool(self.result.picture))
        self.hold.setEnabled(bool(self.result.picture))
        if self.active_book < 0:
            self.open_output.setEnabled(False)
        count = sum(self.confirmed(i) for i in range(len(self.books)))
        self.start_batch.setText(f"开始 / 继续处理已确认的 {count} 包")
        self.start_batch.setEnabled(idle and count > 0)

    def native_size(self):
        rect = self.result.geometry_for_image()
        if rect.width():
            zoom = (
                self.result.zoom
                * self.result.picture.width()
                / (rect.width() * self.result.devicePixelRatioF())
            )
            for box in (self.original, self.result):
                box.set_view(zoom, self.original.cx, self.original.cy)

    def confirm_book(self):
        index = self.active_book
        mode = self.effective(self.mode.currentData())
        path = self.preference_path(index)
        pending = read_json(path.with_name("rebuild-bw.json")) or {}
        if pending.get("mode") and pending["mode"] != self.effective(mode):
            self.status.setText("此包有尚未完成的重做任务，请先处理该任务。")
            return
        try:
            atomic_json(
                path,
                {
                    "mode": mode,
                    "trial": {
                        "signature": self.books[index][1]["signature"],
                        "plan": self.plan,
                        "mode": self.effective(mode),
                    },
                },
            )
        except OSError as error:
            self.status.setText(f"保存单包设置失败 · {error}")
            return
        self.refresh_queue()
        next_book = next(
            (i for i in range(index + 1, len(self.books)) if not self.confirmed(i)), None
        )
        if next_book is not None:
            self.queue.setCurrentRow(next_book)
        self.update_buttons()

    def accept_batch(self):
        self.run_keys = [key for i, (key, _) in enumerate(self.books) if self.confirmed(i)]
        self.accept()

    def done(self, result):
        if self.worker.state() != QProcess.ProcessState.NotRunning:
            self.worker.kill()
            if not self.worker.waitForFinished(3000):
                return
        self.temp.cleanup()
        super().done(result)
