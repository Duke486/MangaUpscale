import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend"), str(ROOT / "ui_runtime")]
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from preview_panel import DecodePair, ImageBox
from backend.runtime_config import load_runtime
from pipeline import validate_member


class ReleaseTests(unittest.TestCase):
    def test_original_names_and_path_validation(self):
        self.assertEqual(validate_member("章节/第001页.png"), "章节/第001页.png")
        for name in ("../escape.png", "/absolute.png", "C:/escape.png", "bad\\page.png"):
            with self.assertRaises(ValueError):
                validate_member(name)

    def test_portable_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / "runtime.json"
            path.write_text(
                json.dumps(
                    {
                        "python": "runtime/python/python.exe",
                        "resource_root": ".",
                        "models": {"m": {"path": "runtime/models/m.pth"}},
                    }
                )
            )
            config = load_runtime(path)
            self.assertEqual(config["resource_root"], str(Path(temp).resolve()))
            self.assertEqual(
                Path(config["python"]), Path(temp).resolve() / "runtime/python/python.exe"
            )
            self.assertEqual(
                Path(config["models"]["m"]["path"]), Path(temp).resolve() / "runtime/models/m.pth"
            )

    def test_metadata_is_native_and_cleared(self):
        app = QApplication.instance() or QApplication([])
        box = ImageBox()
        image = QImage(300, 450, QImage.Format_RGB32)
        image.fill(0x808080)
        box.set_levels([image], 4096)
        self.assertEqual(box.info.text(), "300 × 450 px · 4.0 KiB")
        box.set_view(2, 0.3, 0.4)
        self.assertEqual(box.info.text(), "300 × 450 px · 4.0 KiB")
        box.reset()
        self.assertEqual(box.info.text(), "—")
        self.assertIsNone(box.byte_count)
        box.info.deleteLater()
        box.deleteLater()
        app.processEvents()

    def test_transport_counts_encoded_bytes(self):
        import base64

        app = QApplication.instance() or QApplication([])
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image = QImage(30, 45, QImage.Format_RGB32)
        image.fill(0x808080)
        image.save(buffer, "PNG")
        raw = bytes(buffer.data())
        task = DecodePair(
            json.dumps(
                {
                    "original": base64.b64encode(raw).decode(),
                    "processed": base64.b64encode(raw).decode(),
                }
            ),
            7,
        )
        result = []
        task.signals.ready.connect(lambda generation, data: result.append((generation, data)))
        task.run()
        self.assertEqual(result[0][0], 7)
        self.assertEqual(result[0][1]["original_bytes"], len(raw))
        self.assertEqual(result[0][1]["processed_levels"][0].width(), 30)
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
