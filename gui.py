"""MangaUpscale desktop interface."""

import sys, os, time, math, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "ui_runtime"))
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QSlider,
    QCheckBox,
    QSpinBox,
    QProgressBar,
    QPlainTextEdit,
    QFileDialog,
    QMessageBox,
    QScrollArea,
    QButtonGroup,
    QSplitter,
)
import controller

controller.ROOT = ROOT


class Dialogs:
    @staticmethod
    def showerror(title, text):
        QMessageBox.warning(QApplication.activeWindow(), title, text)

    @staticmethod
    def output_conflicts():
        dialog = QMessageBox(QApplication.activeWindow())
        dialog.setWindowTitle("成品重名")
        dialog.setText("同一目录内有文件会生成同名成品。")
        dialog.setInformativeText("10 秒内未选择，将跳过每组首个之后的重名文件。")
        number = dialog.addButton("加 (1)、(2) 等编号", QMessageBox.AcceptRole)
        skip = dialog.addButton("跳过其余的同目录重名文件", QMessageBox.RejectRole)
        dialog.setDefaultButton(skip)
        dialog.setEscapeButton(skip)
        timer = QTimer(dialog)
        timer.setSingleShot(True)
        timer.setTimerType(Qt.PreciseTimer)
        timer.timeout.connect(skip.click)
        timer.start(10000)
        dialog.exec()
        timer.stop()
        return "number" if dialog.clickedButton() == number else "skip"


controller.messagebox = Dialogs


class Value:
    def __init__(self, value, callback=None):
        self.value = value
        self.callback = callback

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        if self.callback:
            self.callback(str(value))


class Field:
    def __init__(self, widget):
        self.widget = widget

    def get(self):
        if isinstance(self.widget, QCheckBox):
            return self.widget.isChecked()
        if isinstance(self.widget, QSpinBox):
            return self.widget.value()
        return self.widget.text()

    def set(self, v):
        if isinstance(self.widget, QCheckBox):
            self.widget.setChecked(bool(v))
        elif isinstance(self.widget, QSpinBox):
            self.widget.setValue(int(v))
        else:
            self.widget.setText(str(v))


class Progress:
    def __init__(self, w):
        self.w = w
        self.mode = "indeterminate"

    def configure(self, **kw):
        self.mode = kw.get("mode", self.mode)
        if self.mode == "indeterminate":
            self.w.setRange(0, 0)
        else:
            self.w.setRange(0, 1000)
            if kw.get("maximum"):
                self.w.setValue(round(1000 * kw.get("value", 0) / kw["maximum"]))

    def start(self, *a):
        self.configure(mode="indeterminate")

    def stop(self):
        self.w.setRange(0, 1000)

    def __getitem__(self, key):
        return self.mode


class Root:
    def __init__(self, w):
        self.w = w

    def after(self, ms, fn):
        QTimer.singleShot(ms, self.w, fn)

    def update_idletasks(self):
        QApplication.processEvents()

    def destroy(self):
        self.w.close()


STYLE = """
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #253249; }
QMainWindow, QWidget#surface { background: #f4f6fa; }
QFrame#card { background: #ffffff; border: 1px solid #e4e8f0; border-radius: 14px; }
QLabel { background: transparent; border: none; }
QLabel#title { font-size: 25px; font-weight: 700; color: #17243a; }
QLabel#section { font-size: 15px; font-weight: 600; color: #17243a; }
QLabel#muted { color: #8390a3; font-size: 12px; }
QLabel#badge { color: #4c6fbd; background: #eaf0ff; border-radius: 6px; padding: 5px 10px; font-size: 12px; }
QLabel#number { color: #3469e8; font-size: 24px; font-weight: 700; }
QLabel#logo { color: white; background: #376cf0; border-radius: 11px; font-size: 22px; font-weight: 700; }
QLineEdit { background: #f8f9fc; border: 1px solid #e5e9f1; border-radius: 7px; padding: 8px 10px; selection-background-color: #d8e4ff; }
QLineEdit:focus { border-color: #6e95f3; background: white; }
QLineEdit:disabled { color: #a0a9b8; }
QPushButton { background: white; border: 1px solid #e1e6ef; border-radius: 7px; padding: 8px 15px; font-weight: 500; }
QPushButton:hover { border-color: #96b1ee; background: #f4f7ff; }
QPushButton:pressed { background: #e8efff; }
QPushButton:checked { background: #edf3ff; color: #3268dc; border-color: #8fabed; }
QPushButton#primary { background: #376cf0; color: white; border-color: #376cf0; font-weight: 600; padding: 11px 22px; }
QPushButton#primary:hover { background: #285cda; }
QPushButton:disabled { color: #adb5c3; background: #f6f7fa; border-color: #edf0f5; }
QPushButton#primary:disabled { background: #b7c8ee; color: white; border-color: #b7c8ee; }
QPushButton#browse { background: #f0f3f9; border: none; padding: 8px 12px; color: #60708d; }
QPushButton#accent { background: #ef8a1b; color: white; border-color: #ef8a1b; font-weight: 600; }
QPushButton#accent:hover { background: #d9780c; border-color: #d9780c; }
QPushButton#accent:disabled { background: #f2c99c; color: white; border-color: #f2c99c; }
QCheckBox { spacing: 9px; padding: 3px 0; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 5px; border: 1px solid #cbd4e3; background: white; }
QCheckBox::indicator:checked { background: #376cf0; border-color: #376cf0; image: url(CHECK_SVG); }
QCheckBox::indicator:disabled { background: #e2e7f0; }
QSlider::groove:horizontal { height: 5px; border-radius: 2px; background: #e9edf5; }
QSlider::sub-page:horizontal { background: #7097f4; border-radius: 2px; }
QSlider::handle:horizontal { width: 16px; margin: -6px 0; border-radius: 9px; background: white; border: 2px solid #376cf0; }
QSpinBox { padding: 6px 8px; background: #f8f9fc; border: 1px solid #e5e9f1; border-radius: 6px; }
QProgressBar { border: none; border-radius: 3px; background: #eaf0f8; height: 6px; max-height: 6px; }
QProgressBar::chunk { border-radius: 3px; background: #376cf0; }
QPlainTextEdit { background: #f8f9fc; border: none; border-radius: 8px; padding: 8px; color: #758297; font-size: 11px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { width: 6px; background: transparent; margin: 0; }
QScrollBar::handle:vertical { background: #d5ddea; border-radius: 3px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""


def label(text="", kind=None, wrap=False):
    w = QLabel(text)
    if kind:
        w.setObjectName(kind)
    w.setWordWrap(wrap)
    return w


def card(parent, title):
    w = QFrame()
    w.setObjectName("card")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(16, 13, 16, 13)
    lay.setSpacing(9)
    if title:
        lay.addWidget(label(title, "section"))
    parent.addWidget(w)
    return lay


class App(QMainWindow, controller.Controller):
    def __init__(self):
        super().__init__()
        self.root = Root(self)
        self.process = None
        self.job = None
        self.log_stream = None
        self.last_event = None
        self.closing = False
        self.started_at = 0
        self.event_offset = 0
        self.event_previous = None
        self.setWindowTitle("漫画超分 · 5.0")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1180, screen.width() - 24), min(1150, screen.height() - 40))
        self.setMinimumSize(min(880, screen.width() - 24), min(560, screen.height() - 40))
        settings = controller.read_json(ROOT / "gui-settings.json") or {}
        surface = QWidget()
        surface.setObjectName("surface")
        self.setCentralWidget(surface)
        outer = QVBoxLayout(surface)
        outer.setContentsMargins(26, 22, 26, 22)
        outer.setSpacing(18)
        head = QHBoxLayout()
        logo = label("漫", "logo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(44, 44)
        head.addWidget(logo)
        head.addSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("漫画超分", "title"))
        titles.addWidget(label("按需处理，继续上次进度", "muted"))
        head.addLayout(titles)
        head.addStretch()
        head.addWidget(label("Windows · NVIDIA CUDA", "badge"))
        outer.addLayout(head)
        self.sections = QSplitter(Qt.Vertical)
        self.sections.setChildrenCollapsible(False)
        outer.addWidget(self.sections, 1)
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setMinimumHeight(80)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        topwidget = QWidget()
        body = QHBoxLayout(topwidget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        top_scroll.setWidget(topwidget)
        self.sections.addWidget(top_scroll)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        leftwidget = QWidget()
        leftwidget.setObjectName("surface")
        left = QVBoxLayout(leftwidget)
        left.setContentsMargins(0, 0, 4, 0)
        left.setSpacing(14)
        scroller.setWidget(leftwidget)
        body.addWidget(scroller, 3)
        paths = card(left, "文件与任务")
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(1, 1)
        paths.addLayout(grid)
        self.editors = []
        self.browse_buttons = []
        for row, key, title, default in [
            (0, "source", "漫画目录", ""),
            (1, "output", "保存位置", str(Path.home() / "Pictures" / "MangaUpscale")),
            (
                2,
                "cache",
                "缓存位置",
                str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MangaUpscale" / "cache"),
            ),
            (3, "existing", "继续旧任务", ""),
        ]:
            edit = QLineEdit(str(settings.get(key, default)))
            edit.setMinimumWidth(100)
            edit.setPlaceholderText("可选，选择原任务文件夹" if key == "existing" else "选择文件夹")
            self.editors.append(edit)
            setattr(self, key, Field(edit))
            grid.addWidget(label(title, "muted"), row, 0)
            grid.addWidget(edit, row, 1)
            btn = QPushButton("浏览")
            self.browse_buttons.append(btn)
            btn.setObjectName("browse")
            btn.clicked.connect(lambda checked=False, e=edit: self.browse(e))
            grid.addWidget(btn, row, 2)
        options = card(left, "处理设置")
        line = QHBoxLayout()
        line.addWidget(label("页高低于"))
        line.addStretch()
        self.number = label("", "number")
        line.addWidget(self.number)
        line.addWidget(label("px 时超分", "muted"))
        options.addLayout(line)
        self.threshold = Value(float(settings.get("threshold", 1.5)))
        self.threshold_scale = QSlider(Qt.Horizontal)
        self.threshold_scale.setRange(400, 2732)
        self.threshold_scale.setValue(
            int(settings.get("page_height", math.ceil(2752 / float(self.threshold.get()))))
        )
        options.addWidget(self.threshold_scale)
        self.height = label("", "muted", True)
        options.addWidget(self.height)
        mode = QHBoxLayout()
        mode.addWidget(label("默认风格", "muted"))
        mode.addStretch()
        self.bw_mode = Value(settings.get("bw_mode", "传统日漫"))
        self.mode_buttons = []
        self.mode_group = QButtonGroup(self)
        for text in ("传统日漫", "平涂", "全彩"):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(text == self.bw_mode.get())
            self.mode_group.addButton(b)
            self.mode_buttons.append(b)
            b.clicked.connect(lambda checked=False, t=text: self.bw_mode.set(t))
            mode.addWidget(b)
        options.addLayout(mode)
        self.limit_check = QCheckBox("超过模型最大推荐输入页高的图片不超分")
        self.limit_check.setChecked(settings.get("skip_large", True))
        self.skip_large = Field(self.limit_check)
        options.addWidget(self.limit_check)
        self.repack_check = QCheckBox("本次启动以95质量重新打包CBZ并替换成品")
        self.repack95 = Field(self.repack_check)
        self.repack_check.hide()
        reference = card(left, None)
        reference.setSpacing(5)
        reference.addWidget(label("模型输入页高参考", "muted"))
        reference.addWidget(
            label(
                "MangaJaNai 黑白4×    1200 · 1300 · 1400 · 1500 · 1600 · 1920 · 2048 px", None, True
            )
        )
        reference.addWidget(
            label(
                "waifu2x 平涂（降噪2 · 4×） / FDAT M 原生2× 彩页：软件建议上限 2048 px",
                "muted",
                True,
            )
        )
        reference.addWidget(
            label("全彩：所有页（含黑白页）都按彩页处理，FDAT M 原生 2×", "muted", True)
        )
        reference.addWidget(label("日漫按长边匹配；其余两款未指定固定页高。", "muted", True))
        inspect = QHBoxLayout()
        inspect.addWidget(label("每本抽检", "muted"))
        samples = QSpinBox()
        samples.setRange(1, 10)
        samples.setValue(int(settings.get("samples", 5)))
        self.samples = Field(samples)
        inspect.addWidget(samples)
        inspect.addWidget(label("页", "muted"))
        inspect.addStretch()
        inspect.addWidget(label("已有成品自动跳过", "muted"))
        options.addLayout(inspect)
        left.addStretch()
        rightwidget = QWidget()
        right = QVBoxLayout(rightwidget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        rightwidget.setMinimumWidth(290)
        body.addWidget(rightwidget, 2)
        activity = card(right, "任务进度")
        activity.addWidget(label("●  本地处理 · 自动保留进度", "badge"))
        state = label("准备就绪", "section", True)
        activity.addWidget(state)
        self.status = Value("准备就绪", state.setText)
        detail = label("", "muted", True)
        self.detail = Value("", detail.setText)
        activity.addWidget(detail)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 1000)
        bar.setValue(0)
        activity.addWidget(bar)
        self.progress = Progress(bar)
        summary = label("尚未检测", "muted", True)
        activity.addWidget(summary)
        self.summary = Value("尚未检测", summary.setText)
        loglayout = card(right, "运行日志")
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)
        loglayout.addWidget(self.logs, 1)
        right.setStretch(1, 1)
        from preview_panel import PreviewPanel

        self.preview = PreviewPanel(self)
        self.preview.setMinimumHeight(260)
        self.sections.addWidget(self.preview)
        self.sections.setStretchFactor(0, 1)
        self.sections.setStretchFactor(1, 2)
        self.sections.setSizes([350, 560])
        footer = QHBoxLayout()
        footer.addWidget(label("上一本／下一本立即切换处理，保留各本页面缓存。", "muted"))
        footer.addStretch()
        self.paused = False
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        footer.addWidget(self.pause_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        footer.addWidget(self.stop_button)
        self.trial_button = QPushButton("逐包选模型")
        self.trial_button.clicked.connect(lambda: self.start(False, trial=True))
        footer.addWidget(self.trial_button)
        self.report_button = QPushButton("仅检测")
        self.report_button.clicked.connect(lambda: self.start(False))
        footer.addWidget(self.report_button)
        self.run_button = QPushButton("开始 / 继续")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(lambda: self.start(True))
        footer.addWidget(self.run_button)
        outer.addLayout(footer)
        self.threshold_scale.valueChanged.connect(self.update_threshold)
        self.limit_check.toggled.connect(self.update_threshold)
        self.existing.widget.textChanged.connect(self.update_threshold)
        self.update_threshold()
        self.root.after(1000, self.tick)

    def open_trial(self):
        from trial_dialog import TrialDialog

        dialog = TrialDialog(self)
        if dialog.exec() and dialog.run_keys:
            self.start(True, book_keys=dialog.run_keys)
        dialog.deleteLater()

    def update_threshold(self, *a):
        px = self.threshold_scale.value()
        self.threshold.set(2752 / px)
        self.number.setText(str(px))
        self.height.setText(f"以图片长边判断：低于 {px} px 的页面参与超分。")

    def browse(self, edit):
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if path:
            edit.setText(path)

    def log(self, text, epoch=None):
        self.logs.appendPlainText(time.strftime("%H:%M:%S", time.localtime(epoch)) + "   " + text)
        self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def busy(self, value):
        for w in [
            *self.mode_buttons,
            *self.editors,
            *self.browse_buttons,
            self.limit_check,
            self.repack_check,
            self.threshold_scale,
            self.samples.widget,
            self.report_button,
            self.trial_button,
            self.run_button,
        ]:
            w.setEnabled(not value)
        self.stop_button.setEnabled(value)
        self.pause_button.setEnabled(value and not getattr(self, "stopping", False))
        if not value:
            self.paused = False
            self.pause_button.setText("暂停")
        if value:
            self.progress.start()
        else:
            self.progress.stop()

    def toggle_pause(self):
        if not self.process or self.process.poll() is not None or getattr(self, "stopping", False):
            return
        marker = self.job / "status/pause.request"
        try:
            if self.paused:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text("pause", encoding="ascii")
                self.pause_started = time.time()
        except OSError:
            self.log("暂停状态更新失败，请重试。")
            return
        self.paused = not self.paused
        self.pause_button.setText("继续" if self.paused else "暂停")
        self.log("已请求暂停，将在当前计算步骤结束后暂停。" if self.paused else "继续处理。")
        if not self.paused:
            self.status.set("继续处理…")
        self.sync_pause()

    def sync_pause(self):
        if not getattr(self, "paused", False) or getattr(self, "stopping", False):
            return
        ack = controller.read_json(self.job / "status/pause-state.json") or {}
        confirmed = ack.get("state") == "paused" and ack.get("updated_epoch", 0) >= getattr(
            self, "pause_started", 0
        )
        self.status.set("已暂停" if confirmed else "正在暂停，等待当前计算步骤结束…")
        self.progress.stop()

    def stop(self):
        if getattr(self, "stopping", False):
            return False
        if not self.process or self.process.poll() is not None:
            self.process = None
            self.busy(False)
            return True
        self.stopping = True
        self.stop_started = time.monotonic()
        self.stop_killer = None
        self.status.set("正在停止，保留已有断点…")
        self.stop_button.setEnabled(False)
        self.pause_button.setEnabled(False)
        try:
            (self.job / "status/stop.request").write_text("stop", encoding="ascii")
        except OSError:
            pass
        QTimer.singleShot(100, self.poll_stop)
        return False

    def poll_stop(self):
        import subprocess

        if not self.process or self.process.poll() is not None:
            self.process = None
            self.stopping = False
            self.busy(False)
            self.status.set("已停止，可继续")
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None
            if getattr(self, "close_after_stop", False):
                self.closing = True
                self.preview.shutdown()
                self.close()
            return
        elapsed = time.monotonic() - self.stop_started
        if elapsed > 3 and self.stop_killer is None:
            try:
                self.stop_killer = subprocess.Popen(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=controller.CREATE_NO_WINDOW,
                )
            except OSError:
                self.stopping = False
                self.close_after_stop = False
                self.stop_button.setEnabled(True)
                self.status.set("停止失败，请重试")
                return
        if elapsed > 12:
            self.stopping = False
            self.close_after_stop = False
            self.stop_button.setEnabled(True)
            self.status.set("停止尚未完成，请重试")
            return
        QTimer.singleShot(100, self.poll_stop)

    def closeEvent(self, event):
        if self.closing:
            self.preview.shutdown()
            event.accept()
            return
        self.close_after_stop = True
        if self.stop():
            self.closing = True
            self.preview.shutdown()
            event.accept()
        else:
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE.replace("CHECK_SVG", (ROOT / "check.svg").as_posix()))
    window = App()
    window.show()
    window.raise_()
    window.activateWindow()

    def ready():
        (ROOT / "startup-status.json").write_text(
            json.dumps(
                {
                    "state": "ready",
                    "pid": os.getpid(),
                    "visible": window.isVisible(),
                    "width": window.width(),
                    "height": window.size().height(),
                }
            ),
            encoding="utf-8",
        )
        if "--startup-test" in sys.argv:
            window.close()

    QTimer.singleShot(300, ready)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
