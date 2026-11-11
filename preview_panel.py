"""Independent, cancellable preview and book controls."""

import base64, json, os, sys, uuid, time
from pathlib import Path
from PySide6.QtCore import (
    Qt,
    QTimer,
    QProcess,
    QProcessEnvironment,
    QRectF,
    Signal,
    QObject,
    QRunnable,
    QThreadPool,
    QEvent,
)
from PySide6.QtGui import QPixmap, QPainter, QColor, QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QDialog,
)


class DecodedSignals(QObject):
    ready = Signal(int, object)


class DecodePair(QRunnable):
    def __init__(self, raw, generation):
        super().__init__()
        self.raw = raw
        self.generation = generation
        self.signals = DecodedSignals()

    def run(self):
        try:
            data = json.loads(self.raw)
            for key in ("original", "processed"):
                if key not in data:
                    continue
                encoded = base64.b64decode(data.pop(key))
                data[key + "_bytes"] = len(encoded)
                image = QImage.fromData(encoded)
                if image.isNull():
                    raise ValueError("image_decode_failed")
                levels = [image]
                while max(image.width(), image.height()) > 512:
                    image = image.scaled(
                        max(1, image.width() // 2),
                        max(1, image.height() // 2),
                        Qt.IgnoreAspectRatio,
                        Qt.SmoothTransformation,
                    )
                    levels.append(image)
                data[key + "_levels"] = levels
        except Exception:
            data = {"error": "preview_unavailable"}
        self.signals.ready.emit(self.generation, data)


class ImageBox(QLabel):
    changed = Signal(float, float, float)

    def __init__(self):
        super().__init__()
        self.picture = None
        self.levels = []
        self.detail = None
        self.detail_rect = None
        self.native_size = None
        self.info = QLabel("—")
        self.info.setAlignment(Qt.AlignCenter)
        self.byte_count = None
        self.zoom = 1.0
        self.cx = 0.5
        self.cy = 0.5
        self.drag = None
        self.setMinimumSize(120, 100)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.setCursor(Qt.OpenHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def set_levels(self, levels, byte_count=None):
        self.levels = levels
        self.picture = levels[0]
        self.native_size = (self.picture.width(), self.picture.height())
        self.byte_count = byte_count
        size = ""
        if byte_count is not None:
            value = float(byte_count)
            unit = "B"
            for unit in ("B", "KiB", "MiB", "GiB"):
                if value < 1024 or unit == "GiB":
                    break
                value /= 1024
            size = f" · {value:.1f} {unit}"
        self.info.setText(f"{self.native_size[0]} × {self.native_size[1]} px{size}")
        self.update()

    def reset(self):
        self.info.setText("—")
        self.byte_count = None
        self.picture = None
        self.levels = []
        self.detail = None
        self.native_size = None
        self.zoom = 1.0
        self.cx = self.cy = 0.5
        self.update()

    def geometry_for_image(self):
        if not self.picture:
            return QRectF()
        w, h = self.native_size or (self.picture.width(), self.picture.height())
        fit = min(self.width() / w, self.height() / h)
        w *= fit * self.zoom
        h *= fit * self.zoom
        return QRectF(self.width() / 2 - self.cx * w, self.height() / 2 - self.cy * h, w, h)

    def visible_region(self):
        rect = self.geometry_for_image()
        if rect.isEmpty():
            return None
        return [
            max(0.0, -rect.x() / rect.width()),
            max(0.0, -rect.y() / rect.height()),
            min(1.0, (self.width() - rect.x()) / rect.width()),
            min(1.0, (self.height() - rect.y()) / rect.height()),
        ]

    def set_view(self, zoom, cx, cy):
        self.zoom = zoom
        self.cx = max(0.0, min(1.0, cx))
        self.cy = max(0.0, min(1.0, cy))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setClipRect(self.rect())
        painter.fillRect(self.rect(), QColor("#eef1f6"))
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        if not self.picture:
            painter.setPen(QColor("#8390a3"))
            painter.drawText(self.rect(), Qt.AlignCenter, "等待已完成页面")
            return
        rect = self.geometry_for_image()
        # Pick a precomputed level with enough pixels for the current display scale.
        image = self.levels[0]
        needed = rect.width() * self.devicePixelRatioF()
        for level in self.levels[1:]:
            if level.width() < needed:
                break
            image = level
        target = rect.intersected(QRectF(self.rect()))
        source = QRectF(
            (target.x() - rect.x()) / rect.width() * image.width(),
            (target.y() - rect.y()) / rect.height() * image.height(),
            target.width() / rect.width() * image.width(),
            target.height() / rect.height() * image.height(),
        )
        painter.drawImage(target, image, source)

    def wheelEvent(self, event):
        if not self.picture:
            return
        self.setFocus(Qt.MouseFocusReason)
        delta = event.angleDelta().y() or event.pixelDelta().y()
        if not delta:
            return
        old = self.geometry_for_image()
        zoom = max(0.1, min(128.0, self.zoom * 1.2 ** (delta / 120)))
        ratio = zoom / self.zoom
        dx = event.position().x() - self.width() / 2
        dy = event.position().y() - self.height() / 2
        cx = self.cx + dx / old.width() - dx / (old.width() * ratio)
        cy = self.cy + dy / old.height() - dy / (old.height() * ratio)
        self.set_view(zoom, cx, cy)
        self.changed.emit(self.zoom, self.cx, self.cy)
        event.accept()

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        if event.button() == Qt.LeftButton and self.picture:
            self.drag = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag is None:
            return
        rect = self.geometry_for_image()
        delta = event.position() - self.drag
        self.drag = event.position()
        self.set_view(
            self.zoom, self.cx - delta.x() / rect.width(), self.cy - delta.y() / rect.height()
        )
        self.changed.emit(self.zoom, self.cx, self.cy)
        event.accept()

    def mouseReleaseEvent(self, event):
        self.drag = None
        self.setCursor(Qt.OpenHandCursor)
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.set_view(1.0, 0.5, 0.5)
        self.changed.emit(1.0, 0.5, 0.5)
        event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update()


class PreviewPanel(QWidget):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.index = -1
        self.count = 0
        self.active = {}
        self.signature = None
        self.request_id = None
        self.generation = 0
        self.job = None
        self.running_generation = 0
        self.decode_busy = False
        self.decode_task = None
        self.closed = False
        self.books = []
        self.selected_book = None
        self.follow_identity = None
        self.preview_only = False
        self.preview_session = None
        self.completed_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.book_previous = QPushButton("上一本")
        self.book_next = QPushButton("下一本")
        self.book_previous.clicked.connect(lambda: self.move_book(-1))
        self.book_next.clicked.connect(lambda: self.move_book(1))
        controls.addWidget(self.book_previous)
        controls.addWidget(self.book_next)
        controls.addWidget(QLabel("第"))
        self.book_number = QSpinBox()
        self.book_number.setRange(1, 1)
        self.book_number.setKeyboardTracking(False)
        self.book_number.setFixedWidth(85)
        self.book_number.editingFinished.connect(self.jump_book)
        controls.addWidget(self.book_number)
        self.book_total = QLabel("/ 0 本")
        controls.addWidget(self.book_total)
        self.title = QLabel("选择已有任务后浏览漫画")
        self.title.setMinimumWidth(80)
        controls.addWidget(self.title, 1)
        layout.addLayout(controls)
        controls = QHBoxLayout()
        controls.addStretch()
        self.actions = []
        for text, mode in [
            ("传统并重做黑白", "manga"),
            ("平涂2并重做黑白", "flat"),
            ("全彩并重做整本", "color"),
        ]:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setToolTip(
                "将整本作为彩页使用原生2×模型，清除本漫画全部页缓存并立即重做"
                if mode == "color"
                else "立即重做本漫画的黑白页，保留彩页缓存"
            )
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, m=mode: self.command("preference", m))
            controls.addWidget(button)
            self.actions.append(button)
        layout.addLayout(controls)
        pair = QHBoxLayout()
        self.boxes = []
        for title in ["原始页", "超分结果"]:
            col = QVBoxLayout()
            label = QLabel(title)
            label.setAlignment(Qt.AlignCenter)
            col.addWidget(label)
            box = ImageBox()
            self.boxes.append(box)
            col.addWidget(box, 1)
            col.addWidget(box.info)
            pair.addLayout(col, 1)
        layout.addLayout(pair, 1)
        nav = QHBoxLayout()
        nav.addWidget(QLabel("滚轮缩放 · 拖拽 · 双击适屏"))
        nav.addStretch()
        self.previous = QPushButton("← 上一页")
        self.next = QPushButton("下一页 →")
        self.position = QLabel("尚无已完成页面")
        self.previous.clicked.connect(lambda: self.move(-1))
        self.next.clicked.connect(lambda: self.move(1))
        nav.addWidget(self.previous)
        nav.addWidget(self.position)
        nav.addWidget(self.next)
        nav.addStretch()
        layout.addLayout(nav)
        from PySide6.QtWidgets import QApplication

        QApplication.instance().installEventFilter(self)
        self.shortcuts = []
        for key, delta in [("Left", -1), ("Right", 1)]:
            shortcut = QShortcut(QKeySequence(key), owner)
            shortcut.setContext(Qt.WindowShortcut)
            shortcut.activated.connect(lambda d=delta: self.keyboard_move(d))
            self.shortcuts.append(shortcut)
        self.reader = QProcess(self)
        self.reader.finished.connect(self.loaded)
        self.reader.errorOccurred.connect(lambda e: self.process_error())
        self.sender = QProcess(self)
        self.sender.finished.connect(self.sent)
        self.sender.errorOccurred.connect(lambda e: self.send_error())
        self.timeout = QTimer(self)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self.reader.kill)
        for box in self.boxes:
            box.changed.connect(self.sync_view)
        self.follow_timer = QTimer(self)
        self.follow_timer.timeout.connect(self.follow_processing)
        self.follow_timer.start(200)
        self.refresh = QTimer(self)
        self.refresh.timeout.connect(self.poll)
        self.refresh.start(1500)
        self.update_buttons()

    def launch(self, process, args):
        from controller import ROOT, read_json

        from backend.runtime_config import load_runtime

        runtime = load_runtime(ROOT / "runtime.json")
        env = QProcessEnvironment.systemEnvironment()
        env.insert("MANGA_OWNER_PID", str(os.getpid()))
        process.setProcessEnvironment(env)
        process.start(
            runtime["python"],
            ["-B", str(ROOT / "backend/preview_service.py"), "--job", str(self.job), *args],
        )

    def poll(self):
        job = self.owner.job
        if not self.owner.process or self.owner.process.poll() is not None:
            saved = self.owner.existing.get().strip()
            if saved:
                job = Path(saved)
        if not job or self.closed:
            return
        if self.job != job:
            self.job = job
            self.index = -1
            self.signature = None
            self.active = {}
            self.generation += 1
            self.selected_book = None
            self.books = []
            self.preview_only = False
            for box in self.boxes:
                box.reset()
        if self.reader.state() != QProcess.NotRunning or self.decode_busy:
            return
        self.running_generation = self.generation
        args = ["--index", str(self.index)]
        if self.owner.process and self.owner.process.poll() is None and not self.preview_only:
            args += ["--follow", "--since", str(getattr(self.owner, "started_at", 0))]
        if self.selected_book:
            args += ["--book", self.selected_book]
        if self.signature:
            args += ["--known", json.dumps(self.signature)]
        self.launch(self.reader, args)
        self.timeout.start(15000)

    def follow_processing(self):
        if self.closed:
            return
        if self.preview_only:
            running = bool(self.owner.process and self.owner.process.poll() is None)
            if not running or (
                getattr(self.owner, "paused", False)
                and self.preview_session
                == (str(self.owner.job), getattr(self.owner, "started_at", 0))
            ):
                return
            self.preview_only = False
            self.follow_identity = None
        if not self.owner.process or self.owner.process.poll() is not None:
            self.follow_identity = None
            return
        from controller import read_json

        job = self.owner.job
        if not job:
            return
        live = read_json(job / "status/active-task.json") or {}
        fresh = live.get("updated_epoch", 0) >= getattr(self.owner, "started_at", 0)
        key = live.get("task") if fresh else None
        identity = (
            str(job),
            getattr(self.owner, "started_at", 0),
            key,
            live.get("run_id") if fresh else None,
        )
        if identity == self.follow_identity:
            return
        self.follow_identity = identity
        self.generation += 1
        self.signature = None
        self.index = -1
        self.count = 0
        self.selected_book = key
        for box in self.boxes:
            box.reset()
        self.position.setText("等待当前漫画已完成页面" if key else "等待后台开始处理…")
        if key is None:
            self.title.setText("正在准备任务…")
        self.update_buttons()
        if self.reader.state() != QProcess.NotRunning:
            self.reader.kill()
        else:
            self.poll()

    def sync_view(self, zoom, cx, cy):
        # Interaction never requests, decodes or waits for image data.
        for box in self.boxes:
            box.set_view(zoom, cx, cy)

    def eventFilter(self, obj, event):
        from PySide6.QtWidgets import (
            QLineEdit,
            QAbstractSpinBox,
            QPlainTextEdit,
            QTextEdit,
            QSlider,
        )

        if (
            event.type() == QEvent.ShortcutOverride
            and event.key() in (Qt.Key_Left, Qt.Key_Right)
            and isinstance(obj, (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit, QSlider))
        ):
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def keyboard_move(self, delta):
        from PySide6.QtWidgets import (
            QApplication,
            QLineEdit,
            QAbstractSpinBox,
            QPlainTextEdit,
            QTextEdit,
        )

        if isinstance(
            QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox, QPlainTextEdit, QTextEdit)
        ):
            return
        self.move(delta)

    def move_book(self, delta):
        if self.request_id or self.completed_dialog:
            return
        keys = [book["task"] for book in self.books]
        if self.selected_book not in keys:
            return
        index = max(0, min(len(keys) - 1, keys.index(self.selected_book) + delta))
        if keys[index] == self.selected_book:
            return
        self.switch_book(keys[index])

    def jump_book(self):
        if self.request_id or self.completed_dialog:
            return
        keys = [book["task"] for book in self.books]
        index = self.book_number.value() - 1
        if 0 <= index < len(keys) and keys[index] != self.selected_book:
            self.switch_book(keys[index])

    def switch_book(self, target):
        if self.request_id or self.completed_dialog:
            return
        self.request_id = uuid.uuid4().hex
        self.update_buttons()
        self.launch(
            self.sender, ["--command", json.dumps({"action": "navigate", "target": target})]
        )

    def execute_switch(self, target):
        if self.request_id:
            return
        self.preview_only = False
        self.follow_identity = None
        if getattr(self.owner, "paused", False):
            self.owner.toggle_pause()
        self.request_id = uuid.uuid4().hex
        self.launch(
            self.sender,
            [
                "--command",
                json.dumps({"id": self.request_id, "action": "switch", "target": target}),
            ],
        )
        running = bool(self.owner.process and self.owner.process.poll() is None)
        if not running:
            self.selected_book = target
            self.index = 0
            self.signature = None
            self.generation += 1
            self.count = 0
            for box in self.boxes:
                box.reset()
        self.update_buttons()
        if self.reader.state() != QProcess.NotRunning:
            self.reader.kill()
        else:
            self.poll()

    def move(self, delta):
        if not self.count:
            return
        current = self.count - 1 if self.index < 0 else self.index
        self.index = max(0, min(self.count - 1, current + delta))
        self.generation += 1
        self.signature = None
        self.page_signature = None
        for box in self.boxes:
            box.picture = None
            box.levels = []
            box.detail = None
            box.update()
        if self.reader.state() != QProcess.NotRunning:
            self.reader.kill()
        else:
            self.poll()

    def loaded(self, *args):
        self.timeout.stop()
        raw = bytes(self.reader.readAllStandardOutput())
        if self.running_generation != self.generation:
            QTimer.singleShot(0, self.poll)
            return
        self.decode_busy = True
        self.decode_task = DecodePair(raw, self.running_generation)
        self.decode_task.signals.ready.connect(self.decoded)
        QThreadPool.globalInstance().start(self.decode_task)

    def decoded(self, generation, data):
        self.decode_busy = False
        self.decode_task = None
        if self.closed:
            return
        if generation != self.generation:
            QTimer.singleShot(0, self.poll)
            return
        if self.owner.process and self.owner.process.poll() is None and not self.preview_only:
            from controller import read_json

            live = read_json(self.owner.job / "status/active-task.json") or {}
            if (
                live.get("updated_epoch", 0) < getattr(self.owner, "started_at", 0)
                or data.get("book") != live.get("task")
                or data.get("live", {}).get("run_id") != live.get("run_id")
            ):
                QTimer.singleShot(0, self.follow_processing)
                return
        if "books" in data:
            self.books = data["books"]
            self.selected_book = data.get("book")
        active = data.get("active", {})
        if (active.get("task"), active.get("run_id")) != (
            self.active.get("task"),
            self.active.get("run_id"),
        ):
            self.index = -1
            self.signature = None
            for box in self.boxes:
                box.reset()
        self.active = active
        self.count = data.get("count", 0)
        if not self.count:
            self.signature = None
            for box in self.boxes:
                box.reset()
        if "original_levels" in data:
            for box, key in zip(self.boxes, ["original", "processed"]):
                box.set_levels(data[key + "_levels"], data[key + "_bytes"])
            self.index = data["index"]
            self.signature = data["signature"]
        if "signature" in data:
            self.signature = data["signature"]
        self.position.setText(
            f"{self.index + 1} / {self.count} 已完成页"
            if self.count and self.index >= 0
            else "等待已完成页面"
        )
        self.update_buttons()

    def update_buttons(self):
        selected = next((book for book in self.books if book["task"] == self.selected_book), None)
        keys = [book["task"] for book in self.books]
        index = keys.index(self.selected_book) if selected else -1
        self.book_previous.setEnabled(
            index > 0 and not self.request_id and not self.completed_dialog
        )
        self.book_next.setEnabled(
            index >= 0
            and index < len(keys) - 1
            and not self.request_id
            and not self.completed_dialog
        )
        self.book_number.setEnabled(
            bool(keys) and not self.request_id and not self.completed_dialog
        )
        self.book_total.setText(f"/ {len(keys)} 本")
        if not self.book_number.hasFocus() and not self.book_number.lineEdit().hasFocus():
            self.book_number.blockSignals(True)
            self.book_number.setRange(1, max(1, len(keys)))
            self.book_number.setValue(max(1, index + 1))
            self.book_number.blockSignals(False)
        if selected:
            self.title.setText(f"{selected['title'][:28]}")
            self.title.setToolTip(selected["title"])
        for button, mode in zip(self.actions, ["manga", "flat", "color"]):
            button.setEnabled(bool(selected) and not self.request_id and not self.completed_dialog)
            button.setChecked(bool(selected) and selected["mode"] == mode)
        self.previous.setEnabled(self.count > 0 and self.index > 0)
        self.next.setEnabled(self.count > 0 and self.index < self.count - 1)

    def command(self, action, mode):
        if self.request_id or not self.selected_book:
            return
        self.preview_only = False
        self.follow_identity = None
        self.request_id = uuid.uuid4().hex
        request = {
            "id": self.request_id,
            "action": "restart-bw",
            "target": self.selected_book,
            "mode": mode,
        }
        if getattr(self.owner, "paused", False):
            self.owner.toggle_pause()
        self.update_buttons()
        self.launch(self.sender, ["--command", json.dumps(request)])

    def sent(self, *args):
        try:
            data = json.loads(bytes(self.sender.readAllStandardOutput()))
        except ValueError:
            data = {}
        self.request_id = None
        if not data.get("sent"):
            self.send_error()
            return
        if data.get("action") in ("navigate-ready", "navigate-completed", "preview-completed"):
            self.books = data["books"]
            if data["action"] == "navigate-ready":
                self.execute_switch(data["target"])
            elif data["action"] == "navigate-completed":
                self.show_completed(data)
            else:
                self.owner.log("没有可跳转的未完成漫画，已停留预览该成品。")
                self.preview_completed(data["target"])
            return
        if data.get("action") in ("switch", "restart-bw"):
            self.owner.log(
                (
                    "正在清除本漫画全部页缓存，并以全彩2×从头重做。"
                    if data.get("mode") == "color"
                    else "正在停止并重做本漫画的黑白页；彩页缓存保留。"
                )
                if data["action"] == "restart-bw"
                else "正在切换漫画：停止当前本并保留缓存，续做所选漫画。"
            )
            if not self.owner.process or self.owner.process.poll() is not None:
                self.owner.existing.set(str(self.job))
                self.owner.start(True)
            self.update_buttons()
            return
        for book in self.books:
            if book["task"] == data.get("task"):
                book["mode"] = data["mode"]
        self.owner.log(
            "已保存本漫画画风："
            + ("平涂（降噪2）" if data["mode"] == "flat" else "传统日漫")
            + "；再次处理本漫画时使用，已有缓存保留。"
        )
        self.update_buttons()

    def show_completed(self, data):
        if self.completed_dialog:
            return
        dialog = QDialog(self)
        self.completed_dialog = dialog
        dialog.setWindowTitle("该漫画已完成")
        dialog.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(dialog)
        label = QLabel("该漫画已完成，请选择：")
        layout.addWidget(label)
        first = QPushButton("跳到首个未完成的本（5 秒）")
        preview = QPushButton("不执行处理任务，预览该成品")
        following = QPushButton("跳到下个未完成的本")
        for button in (first, preview, following):
            layout.addWidget(button)
        if not data.get("first_unfinished"):
            label.setText("所有漫画均已完成；没有未完成本时将停留预览。")
        following.setEnabled(bool(data.get("next_unfinished")))
        following.setToolTip("从所选本之后向后查找；后方没有未完成本时不可选")
        timer = QTimer(dialog)
        timer.setInterval(100)
        deadline = time.monotonic() + 5
        resolved = [False]

        def choose(selection):
            if resolved[0] or self.closed:
                return
            resolved[0] = True
            timer.stop()
            self.completed_dialog = None
            dialog.close()
            dialog.deleteLater()
            if selection == "preview":
                self.preview_completed(data["target"])
            else:
                self.request_id = uuid.uuid4().hex
                self.update_buttons()
                self.launch(
                    self.sender,
                    [
                        "--command",
                        json.dumps(
                            {"action": "navigate", "target": data["target"], "selection": selection}
                        ),
                    ],
                )

        def tick():
            remaining = max(0, int(__import__("math").ceil(deadline - time.monotonic())))
            first.setText(f"跳到首个未完成的本（{remaining} 秒）")
            if remaining == 0:
                choose("first")

        first.clicked.connect(lambda: choose("first"))
        preview.clicked.connect(lambda: choose("preview"))
        following.clicked.connect(lambda: choose("next"))
        dialog.rejected.connect(lambda: choose("first"))
        timer.timeout.connect(tick)
        timer.start()
        self.update_buttons()
        dialog.open()

    def preview_completed(self, target):
        running = bool(self.owner.process and self.owner.process.poll() is None)
        if running and not getattr(self.owner, "paused", False):
            self.owner.toggle_pause()
        if running and not getattr(self.owner, "paused", False):
            self.owner.log("暂停失败，未进入成品预览。")
            self.update_buttons()
            return
        self.preview_only = True
        self.preview_session = (str(self.owner.job), getattr(self.owner, "started_at", 0))
        self.selected_book = target
        self.index = 0
        self.signature = None
        self.generation += 1
        self.count = 0
        for box in self.boxes:
            box.reset()
        self.update_buttons()
        if self.reader.state() != QProcess.NotRunning:
            self.reader.kill()
        else:
            self.poll()

    def send_error(self):
        self.request_id = None
        self.owner.log("当前漫画操作未发送，请重试。")
        self.update_buttons()

    def process_error(self):
        self.timeout.stop()

    def shutdown(self):
        self.closed = True
        if self.completed_dialog:
            self.completed_dialog.close()
            self.completed_dialog = None
        self.refresh.stop()
        self.follow_timer.stop()
        self.timeout.stop()
        for process in (self.reader, self.sender):
            if process.state() != QProcess.NotRunning:
                process.kill()
