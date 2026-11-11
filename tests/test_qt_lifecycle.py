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
        self.window.job = Path(self.temp.name)
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
        dialog = TrialDialog(self.window)
        try:
            for _ in range(2):
                self.assertTrue(dialog.worker.waitForFinished(5000))
            self.assertEqual(dialog.page.maximum(), 2)
            self.assertTrue(dialog.original.picture)
            dialog.plan["page_height"] = 400
            dialog.generate_trial()
            dialog.original.set_view(3, 0.2, 0.6)
            self.assertTrue(dialog.worker.waitForFinished(5000))
            self.assertEqual(
                (dialog.result.zoom, dialog.result.cx, dialog.result.cy), (3, 0.2, 0.6)
            )
            self.assertFalse(dialog.confirm.isEnabled())
            candidate, _ = dialog.candidate()
            dialog.cache[0, 0, dialog.default] = candidate, "bw"
            dialog.show_result()
            dialog.confirm.click()
            self.assertTrue(dialog.confirmed(0))
            dialog.default = "flat" if dialog.default == "manga" else "manga"
            self.assertFalse(dialog.confirmed(0))
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
