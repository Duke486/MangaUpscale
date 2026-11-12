"""Per-book trial and selection, before starting the production queue."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QProcess, QProcessEnvironment, QSize, Qt, QUrl, QTimer, QEvent, QPoint
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QListView,
    QListWidgetItem,
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QApplication,
    QSplitter,
    QWidget,
)

from controller import ROOT, read_json
from backend.fileio import atomic_json
from preview_panel import ImageBox


class ModeButtons(QWidget):
    """Direct model choices with stable values for the existing preview actions."""

    def __init__(self):
        super().__init__()
        self.group = QButtonGroup(self)
        self.items = []
        self.changed = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)

    def addItem(self, title, value):
        button = QPushButton(title)
        button.setCheckable(True)
        self.group.addButton(button, len(self.items))
        self.items.append((button, value))
        self.layout().addWidget(button)
        if len(self.items) == 1:
            button.setChecked(True)
        button.clicked.connect(lambda: self.changed() if self.changed else None)

    def currentData(self):
        index = self.group.checkedId()
        return self.items[max(0, index)][1]

    def findData(self, value):
        return next((i for i, (_, data) in enumerate(self.items) if data == value), -1)

    def setCurrentIndex(self, index):
        self.items[index][0].setChecked(True)
        if self.changed:
            self.changed()


class TrialDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("逐本对比")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1320, screen.width() - 32), min(860, screen.height() - 48))
        self.setMinimumSize(min(760, screen.width() - 32), min(520, screen.height() - 48))
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
            else {"平涂": "flat", "强制全局彩页": "color"}.get(owner.bw_mode.get(), "manga")
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
        self.read_id = 0
        self.read_request = None
        self.thumb_request = None
        self.thumb_failed = set()
        self.read_process = QProcess(self)
        self.thumb_process = QProcess(self)
        for process in (self.read_process, self.thumb_process):
            process.setProcessEnvironment(env)
            process.setProgram(self.runtime["python"])
            process.setArguments(["-B", str(ROOT / "backend/trial_reader.py")])
        self.read_process.readyReadStandardOutput.connect(self.read_ready)
        self.thumb_process.readyReadStandardOutput.connect(self.thumbs_ready)
        self.read_process.errorOccurred.connect(self.read_error)
        self.read_process.finished.connect(lambda *_: self.read_error())
        self.thumb_process.errorOccurred.connect(self.thumb_error)
        self.thumb_process.finished.connect(lambda *_: self.thumb_error())
        self.thumb_timer = QTimer(self)
        self.thumb_timer.setSingleShot(True)
        self.thumb_timer.setInterval(80)
        self.thumb_timer.timeout.connect(self.load_visible_thumbnails)
        self.run_keys = []
        self.active_book = -1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        body = QSplitter(Qt.Horizontal)
        self.queue = QListWidget()
        self.queue.setMinimumWidth(160)
        self.queue.setMaximumWidth(300)
        body.addWidget(self.queue)
        comparison = QWidget()
        comparison_layout = QVBoxLayout(comparison)
        comparison_layout.setContentsMargins(8, 0, 0, 0)
        pair = QHBoxLayout()
        comparison_layout.addLayout(pair, 1)
        self.original, self.result = ImageBox(), ImageBox()
        self.original.empty_text = "选择漫画后显示原图"
        self.result.empty_text = "生成预览后显示对比结果"
        self.original.changed.connect(self.result.set_view)
        self.result.changed.connect(self.original.set_view)
        for title, box in (("原图", self.original), ("预览结果", self.result)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            column.addWidget(box, 1)
            column.addWidget(box.info)
            pair.addLayout(column, 1)
        body.addWidget(comparison)
        body.setStretchFactor(1, 1)
        body.setSizes([230, 1000])
        body.setChildrenCollapsible(False)
        layout.addWidget(body, 1)
        self.thumbnail_strip = QListWidget()
        self.thumbnail_strip.setViewMode(QListView.IconMode)
        self.thumbnail_strip.setFlow(QListView.LeftToRight)
        self.thumbnail_strip.setWrapping(False)
        self.thumbnail_strip.setMovement(QListView.Static)
        self.thumbnail_strip.setResizeMode(QListView.Adjust)
        self.thumbnail_strip.setIconSize(QSize(72, 96))
        self.thumbnail_strip.setGridSize(QSize(92, 142))
        self.thumbnail_strip.setFixedHeight(166)
        self.thumbnail_strip.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.thumbnail_strip.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.thumbnail_strip.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.thumbnail_strip.setToolTip("滚轮横向浏览全部页面；点击缩略图选择当前预览页")
        self.thumbnail_strip.viewport().installEventFilter(self)
        self.thumbnail_strip.horizontalScrollBar().valueChanged.connect(
            lambda *_: self.thumb_timer.start()
        )
        self.thumbnail_strip.currentRowChanged.connect(
            lambda row: self.page.setValue(row + 1) if row >= 0 else None
        )
        layout.addWidget(self.thumbnail_strip)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("页码"))
        self.page = QSpinBox()
        self.page.setRange(1, 1)
        toolbar.addWidget(self.page)
        self.mode = ModeButtons()
        for title, mode in (
            ("使用任务默认", ""),
            ("传统日漫", "manga"),
            ("平涂", "flat"),
            ("全页彩色", "color"),
        ):
            self.mode.addItem(title, mode)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("本漫画模型"))
        model_row.addWidget(self.mode)
        model_row.addStretch()
        layout.addLayout(model_row)
        self.generate = QPushButton("生成预览")
        toolbar.addWidget(self.generate)
        self.native = QPushButton("100% 显示")
        toolbar.addWidget(self.native)
        self.hold = QPushButton("按住看原图")
        self.hold.pressed.connect(self.hold_original)
        self.hold.released.connect(self.show_result)
        toolbar.addWidget(self.hold)
        self.open_output = QPushButton("打开成品")
        self.open_output.clicked.connect(self.open_current_output)
        self.open_output.setToolTip("打开当前漫画已有的成品文件")
        toolbar.addStretch()
        layout.addLayout(toolbar)
        decisions = QHBoxLayout()
        decisions.addWidget(self.open_output)
        decisions.addStretch()
        self.confirm = QPushButton("确认并下一本")
        self.confirm.setToolTip("保存当前实际风格；之后修改任务默认风格不会改变本次确认。")
        decisions.addWidget(self.confirm)
        self.skip = QPushButton("下一本")
        self.skip.clicked.connect(lambda: self.queue.setCurrentRow(self.active_book + 1))
        decisions.addWidget(self.skip)
        self.start_batch = QPushButton()
        self.start_batch.setObjectName("primary")
        decisions.addWidget(self.start_batch)
        layout.addLayout(decisions)
        self.status = QLabel()
        self.status.setObjectName("badge")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.destination = QLabel()
        self.destination.setWordWrap(True)
        self.destination.setObjectName("muted")
        self.destination.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.destination)

        for _ in self.books:
            self.queue.addItem("")
        self.refresh_queue()
        self.queue.currentRowChanged.connect(self.select_book)
        self.page.valueChanged.connect(self.select_page)
        self.mode.changed = self.show_result
        self.generate.clicked.connect(self.generate_trial)
        self.confirm.clicked.connect(self.confirm_book)
        self.start_batch.clicked.connect(self.accept_batch)
        self.native.clicked.connect(self.native_size)
        self.queue.setCurrentRow(
            next((i for i in range(len(self.books)) if not self.confirmed(i)), 0)
        )
        self.update_buttons()

    def reader_request(self, process, action, **values):
        index = self.active_book
        book = self.books[index][1]
        self.read_id += 1
        request = {
            "id": self.read_id,
            "book": index,
            "source": book["source"],
            "signature": book["signature"],
            "action": action,
            **values,
        }
        if process.state() == QProcess.NotRunning:
            process.start()
        process.write((json.dumps(request) + "\n").encode())
        return self.read_id

    def read_page(self, page):
        if self.active_book < 0 or self.request is not None:
            return
        # Collapse rapid page clicks to the latest request instead of queuing every intermediate page.
        self.pending_page = page
        if self.read_request is not None:
            return
        self.read_request = self.reader_request(
            self.read_process,
            "page",
            page=page,
            destination=str(Path(self.temp.name) / "original.page"),
        )
        self.status.setText("正在读取当前页…")
        self.update_buttons()

    def read_ready(self):
        while self.read_process.canReadLine():
            data = json.loads(bytes(self.read_process.readLine()))
            if data.get("id") != self.read_request:
                continue
            self.read_request = None
            if data.get("error"):
                self.status.setText("读取失败 · " + data["error"])
                self.update_buttons()
                continue
            if data["page"] != self.pending_page:
                self.read_page(self.pending_page)
                continue
            self.page.blockSignals(True)
            self.page.setRange(1, data["total"])
            self.page.setValue(data["page"] + 1)
            self.page.blockSignals(False)
            if self.thumbnail_strip.count() != data["total"]:
                self.thumbnail_strip.blockSignals(True)
                self.thumbnail_strip.clear()
                for page in range(data["total"]):
                    item = QListWidgetItem(str(page + 1))
                    item.setTextAlignment(Qt.AlignHCenter)
                    self.thumbnail_strip.addItem(item)
                self.thumbnail_strip.blockSignals(False)
            self.thumbnail_strip.blockSignals(True)
            self.thumbnail_strip.setCurrentRow(data["page"])
            self.thumbnail_strip.blockSignals(False)
            image = QImage(str(Path(self.temp.name) / "original.page"))
            if image.isNull():
                self.status.setText("当前页无法解码，请选择其他页面。")
                self.update_buttons()
                continue
            self.original.set_levels([image], data["bytes"])
            self.result.reset()
            self.show_result()
            self.thumb_timer.start()

    def load_visible_thumbnails(self):
        if (
            self.thumb_request is not None
            or self.active_book < 0
            or not self.thumbnail_strip.count()
        ):
            return
        view = self.thumbnail_strip
        first = max(0, view.horizontalScrollBar().value() // view.gridSize().width())
        count = view.viewport().width() // 92 + 3
        pages = [
            page
            for page in range(max(0, first - 2), min(view.count(), first + count))
            if view.item(page).icon().isNull() and page not in self.thumb_failed
        ]
        if not pages:
            return
        self.thumb_pages = pages
        self.thumb_request = self.reader_request(
            self.thumb_process, "thumbs", pages=pages, destination=self.temp.name
        )

    def thumbs_ready(self):
        while self.thumb_process.canReadLine():
            data = json.loads(bytes(self.thumb_process.readLine()))
            if data.get("id") != self.thumb_request:
                continue
            self.thumb_request = None
            if data["book"] != self.active_book:
                self.thumb_timer.start()
                continue
            ready = data.get("pages", [])
            self.thumb_failed.update(set(self.thumb_pages) - set(ready))
            for page in ready:
                item = self.thumbnail_strip.item(page)
                if item is not None:
                    pixmap = QPixmap(
                        str(Path(self.temp.name) / f"thumb-{self.active_book}-{page}.png")
                    )
                    icon = QIcon(pixmap)
                    icon.addPixmap(pixmap, QIcon.Selected)
                    item.setIcon(icon)
            self.thumb_timer.start()

    def read_error(self, *_):
        if self.read_request is not None:
            self.read_request = None
            self.status.setText("原图读取进程已退出，请重新选择页面。")
            self.update_buttons()

    def thumb_error(self, *_):
        self.thumb_request = None

    def eventFilter(self, obj, event):
        if obj is self.thumbnail_strip.viewport() and event.type() == QEvent.Wheel:
            delta = event.pixelDelta().x() or event.pixelDelta().y()
            if not delta:
                delta = event.angleDelta().x() or event.angleDelta().y()
            bar = self.thumbnail_strip.horizontalScrollBar()
            bar.setValue(bar.value() - delta)
            event.accept()
            return True
        if obj is self.thumbnail_strip.viewport() and event.type() == QEvent.Resize:
            self.thumb_timer.start()
        return super().eventFilter(obj, event)

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
            name = {"flat": "平涂", "color": "强制全局彩页"}.get(effective, "传统日漫")
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
        self.thumb_timer.stop()
        self.thumbnail_strip.clear()
        self.thumb_failed.clear()
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
        self.read_page(0)

    def select_page(self):
        self.original.reset()
        self.result.reset()
        self.read_page(self.page.value() - 1)

    def generate_trial(self):
        self.launch(self.page.value() - 1, self.effective(self.mode.currentData()))

    def launch(self, page, mode):
        if self.request is not None or self.read_request is not None or self.active_book < 0:
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
            self.status.setText("选择页面和风格，点击「生成预览」后即可确认。")
        self.update_buttons()

    def update_buttons(self):
        idle = self.request is None and self.read_request is None
        self.queue.setEnabled(idle)
        self.skip.setEnabled(idle and 0 <= self.active_book < len(self.books) - 1)
        self.thumbnail_strip.setEnabled(self.request is None)
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
        self.start_batch.setText(f"处理已确认的 {count} 本")
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
        self.thumb_timer.stop()
        for process in (self.read_process, self.thumb_process):
            process.blockSignals(True)
            if process.state() != QProcess.NotRunning:
                process.kill()
                process.waitForFinished(3000)
        if self.worker.state() != QProcess.ProcessState.NotRunning:
            self.worker.kill()
            if not self.worker.waitForFinished(3000):
                return
        self.temp.cleanup()
        super().done(result)
