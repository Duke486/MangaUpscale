"""Synthetic-only tests for the comparison reader and lazy thumbnails."""

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from PIL import Image
from trial_reader import Reader
from storage import signature


class TrialReaderTests(unittest.TestCase):
    def test_reader_reuses_archive_and_caches_only_requested_thumbnails(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            source = directory / "fixture.cbz"
            image = io.BytesIO()
            Image.new("RGB", (1200, 1600), "white").save(image, "JPEG")
            raw = image.getvalue()
            with zipfile.ZipFile(source, "w") as archive:
                for page in range(30):
                    archive.writestr(f"{page:03}.jpg", raw)
            base = dict(source=str(source), signature=signature(source), book=0)
            reader = Reader()
            try:
                with patch("trial_reader.zipfile.ZipFile", wraps=zipfile.ZipFile) as opened:
                    response = reader.handle(
                        dict(
                            base,
                            action="page",
                            page=17,
                            destination=str(directory / "original.page"),
                        )
                    )
                    self.assertEqual(response["total"], 30)
                    self.assertEqual((directory / "original.page").read_bytes(), raw)
                    with patch.object(
                        reader.archive, "read", wraps=reader.archive.read
                    ) as read_page:
                        reader.handle(
                            dict(
                                base,
                                action="page",
                                page=17,
                                destination=str(directory / "original.page"),
                            )
                        )
                        read_page.assert_not_called()
                    response = reader.handle(
                        dict(base, action="thumbs", pages=[12, 13], destination=str(directory))
                    )
                    self.assertEqual(response["pages"], [12, 13])
                    self.assertEqual(len(list(directory.glob("thumb-*.png"))), 2)
                    with Image.open(directory / "thumb-0-12.png") as thumb:
                        self.assertLessEqual(thumb.width, 72)
                        self.assertLessEqual(thumb.height, 96)
                    with patch.object(
                        reader.archive, "open", wraps=reader.archive.open
                    ) as read_member:
                        reader.handle(
                            dict(base, action="thumbs", pages=[12, 13], destination=str(directory))
                        )
                        read_member.assert_not_called()
                    self.assertEqual(opened.call_count, 1)
                source.write_bytes(b"changed")
                with self.assertRaisesRegex(ValueError, "已变化"):
                    reader.handle(
                        dict(
                            base,
                            action="page",
                            page=0,
                            destination=str(directory / "original.page"),
                        )
                    )
            finally:
                reader.close()


if __name__ == "__main__":
    unittest.main()
