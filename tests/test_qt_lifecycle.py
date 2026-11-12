"""Real Qt event-loop tests with synthetic process handles and no previews."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "ui_runtime")]
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
import gui


class QtLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.window = gui.App()
        self.window.preview.shutdown()
        self.window.job = Path(self.temp.name).resolve()
        self.window.settings_path = self.window.job / "gui-settings.json"
        (self.window.job / "status").mkdir()
        self.window.process = Mock()
        self.window.process.poll.return_value = None

    def tearDown(self):
        self.window.process = None
        self.window.closing = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_size_editor_preserves_processing_parameter_and_busy_lock(self):
        self.window.threshold_input.setValue(1600)
        self.assertEqual(self.window.threshold_scale.value(), 1600)
        self.assertEqual(self.window.threshold.get(), 2752 / 1600)
        self.window.busy(True)
        self.assertFalse(self.window.threshold_input.isEnabled())
        self.window.busy(False)
        self.assertTrue(self.window.threshold_input.isEnabled())

    def test_direct_rework_buttons_preserve_existing_commands(self):
        for action, mode in zip(self.window.preview.actions, ("manga", "flat", "color")):
            action.setEnabled(True)
            with patch.object(self.window.preview, "command") as command:
                action.click()
                command.assert_called_once_with("preference", mode)

    def test_default_style_update_selects_card_by_value(self):
        import json

        panel = self.window.preview
        sender = Mock()
        sender.readAllStandardOutput.return_value = json.dumps(
            {
                "sent": True,
                "action": "set-default-mode",
                "mode": "color",
                "applied": 1,
                "kept": 0,
            }
        ).encode()
        with patch.object(panel, "sender", sender):
            panel.sent()
        self.assertEqual(self.window.bw_mode.get(), "强制全局彩页")
        self.assertTrue(self.window.mode_buttons[2].isChecked())

    def test_paths_remembered_without_starting_a_task(self):
        from backend.fileio import atomic_json
        from PySide6.QtWidgets import QPushButton

        saved = self.window.settings_path
        atomic_json(saved, {"page_height": 1600, "bw_mode": "平涂"})
        for key in ("source", "output", "cache"):
            field = getattr(self.window, key)
            field.set(str(self.window.job / key))
            field.widget.editingFinished.emit()
        stored = gui.controller.read_json(saved)
        self.assertEqual(stored["cache"], str(self.window.job / "cache"))
        self.assertEqual(stored["page_height"], 1600)
        self.assertEqual(stored["bw_mode"], "平涂")
        self.window.show()
        self.app.processEvents()
        self.assertTrue(self.window.cache.widget.isVisible())
        self.assertFalse(
            any(button.text() == "查看日志" for button in self.window.findChildren(QPushButton))
        )
        # Reopening uses the same saved path preferences, without invoking the controller.
        real_read = gui.controller.read_json
        with patch(
            "gui.controller.read_json",
            side_effect=lambda path: (
                real_read(saved) if Path(path).name == "gui-settings.json" else real_read(path)
            ),
        ):
            reopened = gui.App()
        reopened.settings_path = self.window.job / "reopened-settings.json"
        try:
            self.assertEqual(reopened.source.get(), stored["source"])
            self.assertEqual(reopened.output.get(), stored["output"])
            self.assertEqual(reopened.cache.get(), stored["cache"])
        finally:
            reopened.closing = True
            reopened.preview.shutdown()
            reopened.close()

    def test_history_restores_paths_and_default_style(self):
        from backend.fileio import atomic_json

        job = self.window.job
        atomic_json(
            job / "state/demand.json",
            {
                "config": {
                    "source": str(job / "source"),
                    "output": str(job / "output"),
                    "samples": 3,
                    "threshold": 2752 / 1600,
                },
                "items": {},
            },
        )
        atomic_json(job / "state/default-mode.json", {"mode": "flat"})
        self.window.existing.set(str(job))
        self.window.reload_tasks()
        self.window.choose_task(self.window.task_picker.currentIndex())
        self.assertEqual(self.window.source.get(), str(job / "source"))
        self.assertEqual(self.window.output.get(), str(job / "output"))
        self.assertEqual(self.window.samples.get(), 3)
        self.assertEqual(self.window.threshold_scale.value(), 1600)
        self.assertEqual(self.window.bw_mode.get(), "平涂")
        self.assertTrue(self.window.mode_buttons[1].isChecked())

    def test_full_width_thumbnails_lazy_load_and_scroll_without_inference(self):
        import io
        import zipfile
        from PIL import Image
        from backend.fileio import atomic_json
        from trial_dialog import TrialDialog
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        source = self.window.job / "fixture.cbz"
        image = io.BytesIO()
        Image.new("RGB", (500, 600), "white").save(image, "JPEG")
        with zipfile.ZipFile(source, "w") as archive:
            for page in range(40):
                archive.writestr(f"{page:03}.jpg", image.getvalue())
        atomic_json(
            self.window.job / "state/demand.json",
            {
                "config": {
                    "source": str(source.parent),
                    "output": str(source.parent),
                    "threshold": 1.5,
                },
                "items": {
                    "book": {
                        "source": str(source),
                        "needs_upscale": True,
                        "signature": {
                            "size": source.stat().st_size,
                            "mtime_ns": source.stat().st_mtime_ns,
                        },
                    }
                },
            },
        )
        with patch("backend.runtime_config.load_runtime", return_value={"python": sys.executable}):
            dialog = TrialDialog(self.window)
        try:
            dialog.resize(1000, 760)
            dialog.show()
            strip = dialog.thumbnail_strip
            for _ in range(500):
                if strip.count() and not strip.item(0).icon().isNull():
                    break
                QTest.qWait(10)
            self.assertEqual(strip.count(), 40)
            self.assertLess(len(list(Path(dialog.temp.name).glob("thumb-*.png"))), 40)
            self.assertGreater(strip.width(), dialog.width() - 60)
            bar = strip.horizontalScrollBar()
            event = QWheelEvent(
                QPointF(50, 50),
                QPointF(50, 50),
                QPoint(),
                QPoint(0, -120),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
            self.app.sendEvent(strip.viewport(), event)
            self.assertGreater(bar.value(), 0)
            bar.setValue(bar.maximum())
            for _ in range(500):
                if not strip.item(39).icon().isNull():
                    break
                QTest.qWait(10)
            self.assertFalse(strip.item(39).icon().isNull())
            with patch.object(dialog, "launch") as generation:
                strip.setCurrentRow(39)
                for _ in range(500):
                    if dialog.read_request is None:
                        break
                    QTest.qWait(10)
                generation.assert_not_called()
                dialog.mode.setCurrentIndex(dialog.mode.findData("flat"))
                generation.assert_not_called()
                dialog.generate_trial()
                generation.assert_called_once_with(39, "flat")
        finally:
            dialog.reject()

    def test_conflict_dialog_defaults_to_skip_after_ten_seconds(self):
        class FastTimer(QTimer):
            def start(self, duration):
                self.requested_duration = duration
                super().start(1)

        timer = FastTimer()
        with patch("gui.QTimer", return_value=timer):
            self.assertEqual(gui.Dialogs.output_conflicts(), "skip")
        self.assertEqual(timer.requested_duration, 10000)

        def choose_number():
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QMessageBox)
            next(button for button in dialog.buttons() if "(1)" in button.text()).click()

        QTimer.singleShot(0, choose_number)
        self.assertEqual(gui.Dialogs.output_conflicts(), "number")

    def test_trial_loads_original_and_keeps_zoom_when_result_arrives(self):
        import io
        import zipfile
        from PIL import Image
        from backend.fileio import atomic_json
        from trial_dialog import TrialDialog

        source = self.window.job / "fixture.cbz"
        data = io.BytesIO()
        Image.new("L", (500, 600), 180).save(data, "PNG")
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("002.png", data.getvalue())
            archive.writestr("001.png", data.getvalue())
        stat = source.stat()
        stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        atomic_json(
            self.window.job / "state/demand.json",
            {
                "config": {
                    "source": str(source.parent),
                    "output": str(source.parent),
                    "threshold": 1.1,
                },
                "items": {
                    "book": {"source": str(source), "signature": stamp, "needs_upscale": True}
                },
            },
        )
        with patch("backend.runtime_config.load_runtime", return_value={"python": sys.executable}):
            dialog = TrialDialog(self.window)
        try:
            for _ in range(500):
                if dialog.original.picture:
                    break
                QTest.qWait(10)
            self.assertEqual(dialog.page.maximum(), 2)
            self.assertTrue(dialog.original.picture)
            self.assertEqual(dialog.thumbnail_strip.count(), 2)
            with patch.object(dialog, "launch") as generation:
                dialog.mode.setCurrentIndex(dialog.mode.findData("flat"))
                generation.assert_not_called()
                dialog.generate_trial()
                generation.assert_called_once_with(0, "flat")
            dialog.mode.setCurrentIndex(dialog.mode.findData(""))
            dialog.plan["page_height"] = 400
            dialog.generate_trial()
            dialog.original.set_view(3, 0.2, 0.6)
            self.assertTrue(dialog.worker.waitForFinished(5000))
            self.assertEqual(
                (dialog.result.zoom, dialog.result.cx, dialog.result.cy), (3, 0.2, 0.6)
            )
            self.assertTrue(dialog.confirm.isEnabled())
            dialog.confirm.click()
            self.assertTrue(dialog.confirmed(0))
            dialog.default = "flat" if dialog.default == "manga" else "manga"
            self.assertTrue(dialog.confirmed(0))
        finally:
            dialog.reject()

    def test_stop_keeps_event_loop_responsive(self):
        pulses = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: pulses.append(1))
        timer.start()
        self.assertFalse(self.window.stop())
        QTest.qWait(160)
        timer.stop()
        self.assertGreater(len(pulses), 2)
        self.assertTrue((self.window.job / "status/stop.request").exists())
        self.window.process.poll.return_value = 0
        self.window.tick()
        self.assertIsNotNone(self.window.process)
        self.window.poll_stop()
        self.assertFalse(self.window.stopping)
        self.assertIsNone(self.window.process)
        self.assertEqual(self.window.status.get(), "已停止，可继续")

    def test_failed_killer_allows_retry(self):
        self.window.stop()
        with (
            patch("gui.time.monotonic", return_value=self.window.stop_started + 4),
            patch("subprocess.Popen", side_effect=OSError(5, "fixture")),
        ):
            self.window.poll_stop()
        self.assertFalse(self.window.stopping)
        self.assertTrue(self.window.stop_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
