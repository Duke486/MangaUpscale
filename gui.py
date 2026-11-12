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
    QTabWidget,
    QComboBox,
    QSplitter,
    QToolButton,
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
QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; color: #392f34; }
QMainWindow, QWidget#surface, QDialog { background: #faf6f7; }
QFrame#card { background: #ffffff; border: 1px solid #eadfe3; border-radius: 12px; }
QLabel { background: transparent; border: none; }
QLabel#title { font-size: 21px; font-weight: 700; color: #392f34; }
QLabel#section { font-size: 16px; font-weight: 600; }
QLabel#muted { color: #796970; font-size: 12px; }
QLabel#badge { color: #875367; background: #f8e8ee; border-radius: 6px; padding: 5px 10px; font-size: 12px; }
QLabel#number { color: #96546d; font-size: 22px; font-weight: 700; }
QLabel#logo { color: #875367; background: #f5dce5; border-radius: 10px; font-size: 21px; font-weight: 700; }
QLineEdit, QSpinBox, QComboBox { background: #fffdfd; border: 1px solid #dfd3d8; border-radius: 7px; padding: 5px 8px; selection-background-color: #f4d6e1; selection-color: #392f34; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border: 1px solid #be7e96; }
QLineEdit:disabled, QSpinBox:disabled { color: #a3949a; background: #f6f2f4; }
QPushButton, QToolButton { background: white; border: 1px solid #e3d8dd; border-radius: 7px; padding: 6px 10px; }
QPushButton:hover, QToolButton:hover { border-color: #c997aa; background: #fff1f6; }
QPushButton:pressed, QToolButton:pressed { background: #f5dce5; }
QPushButton:checked, QToolButton:checked { background: #fbe8ef; color: #784157; border-color: #c58b9f; }
QPushButton:focus, QToolButton:focus { border: 1px solid #a75f7a; }
QPushButton#primary { background: #f2cbd9; color: #64374a; border-color: #dfadbf; font-weight: 600; padding: 7px 18px; }
QPushButton#primary:hover { background: #edbacd; border-color: #ca91a6; }
QPushButton:disabled, QToolButton:disabled { color: #a99ba1; background: #f6f2f4; border-color: #eee6e9; }
QPushButton#primary:disabled { background: #f4e3ea; color: #a28b94; border-color: #ebd9e0; }
QPushButton#browse, QToolButton#disclosure { background: #f8eff3; border: 1px solid #efdee5; color: #825367; }
QPushButton#accent { background: #f8eff3; color: #825367; border-color: #e7cdd8; }
QCheckBox { spacing: 9px; padding: 4px 0; }
QCheckBox::indicator { width: 17px; height: 17px; border-radius: 4px; border: 1px solid #cdbbc3; background: white; }
QCheckBox::indicator:checked { background: #ad6984; border-color: #ad6984; image: url(CHECK_SVG); }
QCheckBox::indicator:disabled { background: #e8dee2; }
QSlider::groove:horizontal { height: 5px; border-radius: 2px; background: #f1e7eb; }
QSlider::sub-page:horizontal { background: #e5b3c5; border-radius: 2px; }
QSlider::handle:horizontal { width: 16px; margin: -6px 0; border-radius: 9px; background: white; border: 2px solid #b97590; }
QProgressBar { border: none; border-radius: 3px; background: #f1e7eb; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { border-radius: 3px; background: #d9a0b6; }
QPlainTextEdit { background: #fffdfd; border: 1px solid #eadfe3; border-radius: 8px; padding: 8px; color: #73636b; font-size: 12px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { width: 8px; background: transparent; margin: 0; }
QScrollBar::handle:vertical { background: #dfcbd4; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { padding: 8px 24px; margin: 0 6px 8px 0; color: #796970; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #875367; border-bottom: 2px solid #cb8fa6; font-weight: 600; }
QTabBar::tab:hover { background: #f8eaf0; border-radius: 6px; }
QListWidget { background: #fffdfd; border: 1px solid #eadfe3; border-radius: 10px; padding: 5px; outline: none; }
QListWidget::item { padding: 12px 8px; border-radius: 6px; margin: 2px; }
QListWidget::item:selected { background: #f7e0e9; color: #64374a; }
QListWidget::item:hover { background: #fbf0f4; }
QMenu { background: #fffdfd; border: 1px solid #e3d8dd; padding: 6px; }
QMenu::item { padding: 9px 18px; border-radius: 4px; }
QMenu::item:selected { background: #f7e0e9; }
QToolTip { background: #fffdfd; color: #392f34; border: 1px solid #dfcbd4; padding: 6px; }
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
        import tomllib

        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ]
        self.setWindowTitle(f"漫画超分 · {version}")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1280, screen.width() - 24), min(800, screen.height() - 40))
        self.setMinimumSize(min(780, screen.width() - 24), min(540, screen.height() - 40))
        self.settings_path = ROOT / "gui-settings.json"
        settings = controller.read_json(self.settings_path) or {}
        surface = QWidget()
        surface.setObjectName("surface")
        self.setCentralWidget(surface)
        outer = QVBoxLayout(surface)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(12)
        head = QHBoxLayout()
        logo = label("漫", "logo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(34, 34)
        head.addWidget(logo)
        head.addSpacing(6)
        head.addWidget(label("漫画超分", "section"))
        head.addStretch()
        info = label(f"{version}  ·  本地处理", "muted")
        info.setToolTip("Windows · NVIDIA CUDA\n进度自动保存在任务缓存中")
        head.addWidget(info)
        outer.addLayout(head)

        body = QSplitter(Qt.Horizontal)
        outer.addWidget(body, 1)
        self.workspace = QTabWidget()
        body.addWidget(self.workspace)
        right_column = QWidget()
        right = QVBoxLayout(right_column)
        right.setContentsMargins(10, 40, 0, 0)
        right.setSpacing(10)
        body.addWidget(right_column)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([720, 480])
        body.setChildrenCollapsible(False)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        settings_column = QWidget()
        settings_column.setObjectName("surface")
        left = QVBoxLayout(settings_column)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        settings_scroll.setWidget(settings_column)
        self.workspace.addTab(settings_scroll, "任务设置")
        rules = QVBoxLayout()
        rules.setSpacing(10)
        paths = card(left, "漫画与保存位置")
        paths.addWidget(label("支持 ZIP、CBZ 和图片型 EPUB，包含子文件夹。", "muted", True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)
        paths.addLayout(grid)
        _, advanced_layout = self.foldout(rules, "更多设置")
        self.editors = []
        self.browse_buttons = []
        for row, key, title, default in [
            (0, "source", "漫画文件夹", ""),
            (1, "output", "保存到", str(Path.home() / "Pictures" / "MangaUpscale")),
            (
                2,
                "cache",
                "缓存位置",
                str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MangaUpscale" / "cache"),
            ),
        ]:
            edit = QLineEdit(str(settings.get(key, default)))
            edit.setMinimumWidth(80)
            edit.setPlaceholderText("选择文件夹")
            edit.setToolTip(edit.text())
            edit.textChanged.connect(edit.setToolTip)
            self.editors.append(edit)
            setattr(self, key, Field(edit))
            target = grid
            target.addWidget(label(title), row, 0)
            target.addWidget(edit, row, 1)
            btn = QPushButton("选择文件夹")
            btn.setObjectName("browse")
            self.browse_buttons.append(btn)
            btn.clicked.connect(lambda checked=False, e=edit: self.browse(e))
            target.addWidget(btn, row, 2)

        self.existing = Field(QLineEdit(str(settings.get("existing", ""))))
        self.editors.append(self.existing.widget)
        history = card(left, "继续之前的任务")
        history_line = QHBoxLayout()
        self.task_picker = QComboBox()
        self.task_picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.task_picker.setMinimumContentsLength(12)
        history_line.addWidget(self.task_picker, 1)
        refresh_tasks = QPushButton("刷新")
        refresh_tasks.clicked.connect(self.reload_tasks)
        history_line.addWidget(refresh_tasks)
        import_task = QPushButton("导入…")
        import_task.clicked.connect(self.import_task)
        history_line.addWidget(import_task)
        history.addLayout(history_line)
        history.addWidget(label("按漫画文件夹名选择，自动恢复路径和检测参数。", "muted", True))
        self.browse_buttons.extend([refresh_tasks, import_task])
        self.task_picker.currentIndexChanged.connect(self.choose_task)
        self.cache.widget.editingFinished.connect(self.reload_tasks)

        options_widget = QFrame()
        options_widget.setObjectName("card")
        options = QVBoxLayout(options_widget)
        options.setContentsMargins(16, 14, 16, 14)
        options.setSpacing(8)
        rules.insertWidget(0, options_widget)
        options.addWidget(label("处理规则", "section"))
        line = QHBoxLayout()
        line.addWidget(label("图片长边低于"))
        self.threshold_input = QSpinBox()
        self.threshold_input.setRange(400, 2732)
        self.threshold_input.setSuffix(" px")
        self.threshold_input.setFixedWidth(124)
        line.addWidget(self.threshold_input)
        line.addWidget(label("时超分"))
        line.addStretch()
        options.addLayout(line)
        self.threshold = Value(float(settings.get("threshold", 1.5)))
        self.threshold_scale = QSlider(Qt.Horizontal)
        self.threshold_scale.setRange(400, 2732)
        self.threshold_scale.setValue(
            int(settings.get("page_height", math.ceil(2752 / float(self.threshold.get()))))
        )
        self.threshold_input.setValue(self.threshold_scale.value())
        self.threshold_input.valueChanged.connect(self.threshold_scale.setValue)
        self.threshold_scale.valueChanged.connect(self.threshold_input.setValue)
        options.addWidget(self.threshold_scale)
        self.height = label("", "muted", True)
        self.height.hide()
        options.addWidget(label("默认风格"))
        mode = QHBoxLayout()
        saved_mode = settings.get("bw_mode", "传统日漫")
        self.bw_mode = Value("强制全局彩页" if saved_mode == "全彩" else saved_mode)
        self.mode_buttons = []
        self.mode_group = QButtonGroup(self)
        for text, title, hint in (
            ("传统日漫", "传统日漫", "黑白线稿与网点"),
            ("平涂", "平涂", "平整色块与简洁线条"),
            ("强制全局彩页", "全页彩色", "所有页使用彩页模型"),
        ):
            b = QPushButton(title)
            b.setToolTip(hint)
            b.setProperty("mode_name", text)
            b.setCheckable(True)
            b.setChecked(text == self.bw_mode.get())
            b.setMinimumHeight(30)
            self.mode_group.addButton(b)
            self.mode_buttons.append(b)
            b.clicked.connect(lambda checked=False, t=text: self.bw_mode.set(t))
            mode.addWidget(b, 1)
        options.addLayout(mode)
        options.addWidget(
            label("传统日漫和平涂会自动识别彩页，并使用彩页模型处理。", "muted", True)
        )
        self.limit_check = QCheckBox("跳过超出模型建议尺寸的页面")
        self.limit_check.setChecked(settings.get("skip_large", True))
        self.skip_large = Field(self.limit_check)
        advanced_layout.addWidget(self.limit_check)
        self.repack_check = QCheckBox("本次启动以95质量重新打包CBZ并替换成品")
        self.repack95 = Field(self.repack_check)
        self.repack_check.hide()
        inspect = QHBoxLayout()
        inspect.addWidget(label("检测每本前"))
        samples = QSpinBox()
        samples.setRange(1, 10)
        samples.setValue(int(settings.get("samples", 5)))
        self.samples = Field(samples)
        inspect.addWidget(samples)
        inspect.addWidget(label("页"))
        inspect.addStretch()
        advanced_layout.addLayout(inspect)
        reference = card(advanced_layout, "模型说明")
        reference.addWidget(
            label(
                "传统日漫：MangaJaNai 黑白 4×，按长边匹配 1200 / 1300 / 1400 / 1500 / 1600 / 1920 / 2048 px 模型。",
                "muted",
                True,
            )
        )
        reference.addWidget(
            label(
                "平涂：waifu2x art，降噪 2，4×。彩页：FDAT M 原生 2×。软件建议输入尺寸上限为 2048 px。",
                "muted",
                True,
            )
        )
        reference.addWidget(
            label("全页彩色处理包含黑白页。平涂与彩页模型未指定固定输入页高。", "muted", True)
        )
        left.addLayout(rules)
        left.addWidget(label("已有成品自动跳过 · 输出 CBZ · 原始文件保持不变", "muted", True))
        left.addStretch()
        from preview_panel import PreviewPanel

        self.preview = PreviewPanel(self)
        self.workspace.addTab(self.preview, "处理预览")

        activity = card(right, "开始控制、状态与进度")
        state = label("准备就绪", "section", True)
        state.setMaximumHeight(52)
        activity.addWidget(state)
        self.status = Value("准备就绪", state.setText)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setRange(0, 1000)
        bar.setValue(0)
        activity.addWidget(bar)
        self.progress = Progress(bar)
        detail = label("", "muted", True)
        detail.hide()
        self.detail = Value("", lambda text: (detail.setText(text), detail.setVisible(bool(text))))
        activity.addWidget(detail)
        summary = label("尚未检测", "muted", True)
        activity.addWidget(summary)
        self.summary = Value("尚未检测", summary.setText)
        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.paused = False
        self.pause_button = QPushButton("暂停")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.toggle_pause)
        footer.addWidget(self.pause_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        footer.addWidget(self.stop_button)
        self.trial_button = QPushButton("逐本对比")
        self.trial_button.clicked.connect(lambda: self.start(False, trial=True))
        footer.addWidget(self.trial_button)
        self.report_button = QPushButton("检测漫画")
        self.report_button.clicked.connect(lambda: self.start(False))
        footer.addWidget(self.report_button)
        self.run_button = QPushButton("开始处理")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(lambda: self.start(True))
        footer.addWidget(self.run_button)
        activity.insertLayout(1, footer)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)
        self.logs.setMinimumHeight(80)
        self.logs.show()
        log_card = card(right, "运行日志")
        log_card.addWidget(self.logs, 1)
        right.setStretch(right.count() - 1, 1)
        self.reload_tasks()
        self.threshold_scale.valueChanged.connect(self.update_threshold)
        self.limit_check.toggled.connect(self.update_threshold)
        self.existing.widget.textChanged.connect(self.update_threshold)
        self.pause_button.hide()
        self.stop_button.hide()
        self.update_threshold()
        for editor in (self.source.widget, self.output.widget, self.cache.widget):
            editor.editingFinished.connect(self.save_paths)
        self.root.after(1000, self.tick)

    def reload_tasks(self):
        current = self.existing.get().strip()
        cache = Path(self.cache.get())
        candidates = list(cache.glob("job-gui-*/state/demand.json")) if cache.is_dir() else []
        if current and (Path(current) / "state/demand.json").is_file():
            candidates.append(Path(current) / "state/demand.json")
        self.task_picker.blockSignals(True)
        self.task_picker.clear()
        self.task_picker.addItem("使用当前设置（自动保存进度）", "")
        for path in sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True):
            manifest = controller.read_json(path) or {}
            config = manifest.get("config", {})
            if not config.get("source"):
                continue
            import datetime

            date = datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
            task = str(path.parent.parent)
            self.task_picker.addItem(f"{Path(config['source']).name}  ·  {date}", task)
            self.task_picker.setItemData(
                self.task_picker.count() - 1,
                f"来源：{config['source']}\n保存到：{config.get('output', '')}\n断点：{task}",
                Qt.ToolTipRole,
            )
        index = self.task_picker.findData(current)
        if current and index < 0:
            self.task_picker.addItem(f"已指定任务 · {Path(current).name}", current)
            index = self.task_picker.count() - 1
        self.task_picker.setCurrentIndex(max(0, index))
        self.task_picker.blockSignals(False)

    def choose_task(self, index):
        task = self.task_picker.itemData(index) or ""
        self.existing.set(task)
        if not task:
            self.save_paths()
            return
        manifest = controller.read_json(Path(task) / "state/demand.json") or {}
        config = manifest.get("config", {})
        if config.get("source"):
            self.source.set(config["source"])
        if config.get("output"):
            self.output.set(config["output"])
        if config.get("samples"):
            self.samples.set(config["samples"])
        stored = controller.read_json(Path(task) / "state/default-mode.json") or {}
        mode_name = {"manga": "传统日漫", "flat": "平涂", "color": "强制全局彩页"}.get(
            stored.get("mode")
        )
        if mode_name:
            self.bw_mode.set(mode_name)
            for button in self.mode_buttons:
                button.setChecked(button.property("mode_name") == mode_name)
        if config.get("threshold"):
            self.threshold_scale.setValue(round(2752 / config["threshold"]))
        self.save_paths()

    def import_task(self):
        task = QFileDialog.getExistingDirectory(
            self, "选择包含 state 子目录的任务断点目录", self.cache.get()
        )
        if not task:
            return
        manifest = controller.read_json(Path(task) / "state/demand.json") or {}
        if not manifest.get("config", {}).get("source"):
            QMessageBox.warning(
                self, "未找到任务", "请选择缓存中的任务断点目录，其中应包含 state/demand.json。"
            )
            return
        self.existing.set(task)
        self.reload_tasks()
        self.choose_task(self.task_picker.currentIndex())

    def foldout(self, parent, title):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 10, 14, 10)
        toggle = QToolButton()
        toggle.setObjectName("disclosure")
        toggle.setText(title)
        toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.RightArrow)
        toggle.setCheckable(True)
        layout.addWidget(toggle, 0, Qt.AlignLeft)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 8, 0, 0)
        content_layout.setSpacing(10)
        content.hide()
        toggle.toggled.connect(content.setVisible)
        toggle.toggled.connect(
            lambda checked: toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        )
        layout.addWidget(content)
        parent.addWidget(frame)
        return toggle, content_layout

    def start(self, run, output_conflicts=None, *, trial=False, book_keys=()):
        previous_start = self.started_at
        controller.Controller.start(self, run, output_conflicts, trial=trial, book_keys=book_keys)
        if run and self.started_at != previous_start:
            self.workspace.setCurrentIndex(1)

    def open_trial(self):
        from trial_dialog import TrialDialog

        dialog = TrialDialog(self)
        if dialog.exec() and dialog.run_keys:
            self.start(True, book_keys=dialog.run_keys)
        dialog.deleteLater()

    def update_threshold(self, *a):
        px = self.threshold_scale.value()
        self.threshold.set(2752 / px)
        if hasattr(self, "run_button"):
            self.run_button.setText(
                "继续处理" if self.existing.get().strip() or self.job else "开始处理"
            )
        self.height.setText(f"长边达到 {px} px 的页面保留原尺寸。")

    def browse(self, edit):
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if path:
            edit.setText(path)
            self.save_paths()
            if edit is self.cache.widget:
                self.reload_tasks()

    def save_paths(self):
        settings = controller.read_json(self.settings_path) or {}
        settings.update(
            {key: getattr(self, key).get() for key in ("source", "output", "cache", "existing")}
        )
        try:
            controller.atomic_json(self.settings_path, settings)
        except OSError as error:
            self.log(f"路径保存失败：{error}")

    def log(self, text, epoch=None):
        scrollbar = self.logs.verticalScrollBar()
        position = scrollbar.value()
        follow = position >= scrollbar.maximum() - 2
        self.logs.appendPlainText(time.strftime("%H:%M:%S", time.localtime(epoch)) + "   " + text)
        scrollbar.setValue(scrollbar.maximum() if follow else position)

    def busy(self, value):
        for w in [
            *self.mode_buttons,
            *self.editors,
            *self.browse_buttons,
            self.limit_check,
            self.repack_check,
            self.threshold_scale,
            self.threshold_input,
            self.task_picker,
            self.samples.widget,
            self.report_button,
            self.trial_button,
            self.run_button,
        ]:
            w.setEnabled(not value)
        for button in (self.run_button, self.report_button, self.trial_button):
            button.setVisible(not value)
        self.pause_button.setVisible(value)
        self.stop_button.setVisible(value)
        if not value:
            self.update_threshold()
            self.reload_tasks()
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
        self.save_paths()
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
