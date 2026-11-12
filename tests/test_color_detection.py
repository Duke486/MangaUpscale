"""Synthetic local color, page-margin marks and scan-tint regressions."""

import io
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zipfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from PIL import Image, ImageDraw
from pipeline import metadata, read, run, sha, write, scan
from mode_jobs import prepare_saved, rules_for
from demand import prepare
from color_detection import VERSION


def fixture(kind):
    image = Image.new("RGB", (800, 1200), "white")
    draw = ImageDraw.Draw(image)
    if kind not in (
        "title",
        "character",
        "pastel",
        "white-watermark",
        "tint",
        "edge-art",
        "faint-title",
    ):
        for y in range(140, 1100, 55):
            draw.rectangle((110, y, 690, y + 30), outline=(60, 60, 60), width=2)
            draw.line((140, y + 15, 660, y + 15), fill=(100, 100, 100), width=2)
    if kind == "title":
        for x in range(290, 510, 25):
            draw.line((x, 350, x + 12, 380), fill=(210, 60, 90), width=4)
    elif kind == "faint-title":
        draw.rectangle((300, 400, 500, 440), fill=(232, 244, 248))
    elif kind == "character":
        draw.ellipse((355, 500, 445, 630), fill=(240, 180, 130))
    elif kind == "pastel":
        draw.rectangle((310, 410, 490, 450), fill=(220, 240, 250))
    elif kind == "lines":
        for y in range(300, 900, 60):
            draw.line((220, y, 570, y + 35), fill=(60, 130, 200), width=2)
    elif kind == "small-sticker":
        draw.ellipse((370, 500, 405, 535), fill=(230, 90, 120))
    elif kind == "sticker":
        draw.ellipse((360, 500, 425, 565), fill=(230, 90, 120))
    elif kind in ("watermark", "white-watermark"):
        for x, y in ((20, 45), (640, 45), (20, 1130), (640, 1130)):
            for i in range(0, 120, 12):
                draw.line((x + i, y, x + i + 5, y + 15), fill=(30, 70, 200), width=2)
    elif kind == "tint":
        image = Image.new("RGB", image.size, (250, 246, 240))
    elif kind == "edge-art":
        draw.rectangle((0, 300, 55, 850), fill=(80, 130, 210))
    return image


class ColorDetection(unittest.TestCase):
    def test_local_art_is_color_and_small_margin_marks_are_not(self):
        for kind in (
            "title",
            "faint-title",
            "character",
            "pastel",
            "lines",
            "sticker",
            "small-sticker",
            "edge-art",
            "watermark",
            "white-watermark",
            "tint",
            "gray",
        ):
            image = fixture(kind)
            expected = kind not in ("watermark", "white-watermark", "tint", "gray")
            for size in ((400, 600), (800, 1200), (1600, 2400)):
                for encoding in ("PNG", "JPEG"):
                    with self.subTest(kind=kind, size=size, encoding=encoding):
                        data = io.BytesIO()
                        image.resize(size).save(data, encoding, quality=80)
                        result = metadata(data.getvalue())
                        self.assertEqual(result["color"], expected, result)

    def test_scan_checkpoint_upgrade_does_not_need_original_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, job = root / "source", root / "task"
            source.mkdir()
            book = source / "A.cbz"
            data = io.BytesIO()
            fixture("title").resize((400, 600)).save(data, "PNG")
            with zipfile.ZipFile(book, "w") as archive:
                archive.writestr("page.png", data.getvalue())
            scan(source, job, output_root=root / "out")
            checkpoint = next((job / "state/checkpoints").glob("*.json"))
            saved = read(checkpoint)
            page = saved["status"]["pages"][0]
            page.pop("color_version")
            page["color"] = False
            write(checkpoint, saved)
            book.rename(book.with_suffix(".offline"))
            with patch("storage.snapshot", side_effect=AssertionError("source accessed")):
                scan(source, job, output_root=root / "out")
            page = read(job / "status/inventory.json")["archives"][0]["pages"][0]
            self.assertTrue(page["color"])
            self.assertEqual(page["color_version"], VERSION)
            with patch("color_detection.classify", side_effect=AssertionError("already upgraded")):
                scan(source, job, output_root=root / "out")

    def test_neutral_noise_and_hidden_transparent_rgb_are_not_art(self):
        import numpy as np

        rng = np.random.default_rng(24)
        gray = rng.integers(30, 255, (800, 600, 1), dtype=np.int16)
        pixels = np.clip(gray + rng.integers(-2, 3, (800, 600, 3)), 0, 255).astype("uint8")
        for image in (Image.fromarray(pixels), Image.new("RGBA", (400, 600), (255, 0, 0, 0))):
            data = io.BytesIO()
            image.save(data, "PNG")
            self.assertFalse(metadata(data.getvalue())["color"])

    def test_legacy_completed_pages_refresh_offline_and_rebuild_only_changed_pages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source, job, output = root / "library", root / "task", root / "out"
            source.mkdir()
            book = source / "A.cbz"
            with zipfile.ZipFile(book, "w") as archive:
                for kind in ("title", "watermark", "gray"):
                    data = io.BytesIO()
                    fixture(kind).resize((400, 600)).save(data, "PNG")
                    archive.writestr(kind + ".png", data.getvalue())
            prepare(source, job, output, 5, report_only=True)
            prepare_saved(job, source, output, "manga", page_height=800)
            child = next((job / "jobs").iterdir())
            inventory = read(child / "status/inventory.json")
            archive = inventory["archives"][0]
            old_colors = {"title.png": False, "watermark.png": True, "gray.png": False}
            for page in archive["pages"]:
                page["color"] = old_colors[page["id"]]
                page.pop("color_version")
            write(child / "status/inventory.json", inventory)
            checkpoint = child / "state/checkpoints" / (archive["id"] + ".json")
            saved = read(checkpoint)
            saved["status"] = archive
            write(checkpoint, saved)
            weights = root / "weights"
            weights.write_bytes(b"fixture")
            runtime = root / "runtime.json"
            ids = {rule["model"] for rule in rules_for("manga")}
            write(
                runtime,
                {"models": {key: {"path": str(weights), "sha256": sha(weights)} for key in ids}},
            )

            class Engine:
                calls = 0

                def upscale(self, image, model, rule, color):
                    self.calls += 1
                    scale = 2 if color else 4
                    return image.resize((image.width * scale, image.height * scale))

            engine = Engine()
            with patch("storage.refresh_colors", return_value=False):
                run(child, child / "plan.json", runtime, engine)
            target = output / "library/A.cbz"
            old_output = sha(target)
            self.assertEqual(engine.calls, 3)
            book.rename(book.with_suffix(".offline"))
            prepare_saved(job, source, output, "manga", page_height=800)
            self.assertEqual(sha(target), old_output)
            pages = read(child / "status/inventory.json")["archives"][0]["pages"]
            self.assertEqual(
                {p["id"]: p["color"] for p in pages},
                {"title.png": True, "watermark.png": False, "gray.png": False},
            )
            self.assertTrue(all(p["color_version"] == VERSION for p in pages))
            self.assertTrue((child / "status/rebuild-bw.json").exists())
            run(child, child / "plan.json", runtime, engine)
            self.assertEqual(engine.calls, 5)
            with zipfile.ZipFile(target) as archive:
                for name, size in (
                    ("title.jpg", (800, 1200)),
                    ("watermark.jpg", (1600, 2400)),
                    ("gray.jpg", (1600, 2400)),
                ):
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        self.assertEqual(image.size, size)
            with patch("color_detection.classify", side_effect=AssertionError("already upgraded")):
                prepare_saved(job, source, output, "manga", page_height=800)
            self.assertFalse((child / "status/rebuild-bw.json").exists())


if __name__ == "__main__":
    unittest.main()
