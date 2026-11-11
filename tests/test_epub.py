"""Synthetic EPUBs only: publication order, preview, staging and CBZ publication."""

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from archive_pages import archive_entries, member_path
from demand import prepare, sample
from mode_jobs import prepare_saved
from pipeline import read, run, sha, write
from selftest import FixtureEngine
from storage import signature, target_for


def fixture(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            """
            <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
              <rootfiles><rootfile full-path="OPS/package.opf"/></rootfiles>
            </container>""",
        )
        archive.writestr(
            "OPS/package.opf",
            """
            <package xmlns="http://www.idpf.org/2007/opf"><manifest>
              <item id="c" href="Text/cover.xhtml" media-type="application/xhtml+xml"/>
              <item id="p1" href="Text/001.xhtml" media-type="application/xhtml+xml"/>
              <item id="p2" href="Text/002.svg" media-type="image/svg+xml"/>
              <item id="nav" href="Text/nav.xhtml" media-type="application/xhtml+xml"/>
            </manifest><spine>
              <itemref idref="c"/><itemref idref="p1"/><itemref idref="p2"/>
              <itemref idref="p1"/><itemref idref="nav" linear="no"/>
            </spine></package>""",
        )
        archive.writestr(
            "OPS/Text/cover.xhtml",
            """
            <html xmlns="http://www.w3.org/1999/xhtml"><body>
              <svg xmlns="http://www.w3.org/2000/svg"
                   xmlns:xlink="http://www.w3.org/1999/xlink">
                <image xlink:href="../Images/003%20cover.PNG#view"/>
              </svg>
            </body></html>""",
        )
        archive.writestr(
            "OPS/Text/001.xhtml",
            """
            <html xmlns="http://www.w3.org/1999/xhtml"><body>
              <img src="../Images/001.png"/>
            </body></html>""",
        )
        archive.writestr(
            "OPS/Text/002.svg",
            """
            <svg xmlns="http://www.w3.org/2000/svg"><image href="../Images/002.png"/></svg>""",
        )
        for name, width in (
            ("001.png", 81),
            ("002.png", 82),
            ("003 cover.PNG", 83),
            ("unused.png", 90),
        ):
            data = io.BytesIO()
            Image.new("L", (width, 100), 100).save(data, "PNG")
            archive.writestr("OPS/Images/" + name, data.getvalue())
        archive.writestr("OPS/stylesheet.css", "body { margin: 0; }")


class EpubSupport(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "library"
        self.source.mkdir()
        self.book = self.source / "book.EPUB"
        fixture(self.book)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_order_index_does_not_read_image_payloads(self):
        with zipfile.ZipFile(self.book) as archive:
            original_read = archive.read

            def metadata_only(name, *args, **kwargs):
                self.assertNotIn(Path(name).suffix.lower(), (".png", ".jpg"))
                return original_read(name, *args, **kwargs)

            with patch.object(archive, "read", side_effect=metadata_only):
                pages = archive_entries(archive, True)
            self.assertEqual(
                [Path(p.filename).name for p in pages],
                ["003 cover.PNG", "001.png", "002.png", "001.png"],
            )
        self.assertEqual(sample(self.book, 2)["sample_count"], 2)

    def test_detection_staging_publication_and_offline_resume(self):
        job = self.root / "job"
        output = self.root / "output"
        before = self.book.read_bytes()
        prepare(self.source, job, output, 2, report_only=True)
        self.assertEqual(read(job / "status/demand-summary.json")["selected"], 1)
        self.assertTrue(prepare_saved(job, self.source, output, "manga"))
        key = read(job / "state/active-books.json")[0]
        child = job / "jobs" / key
        record = read(child / "state/map.json")["archives"][0]
        inventory = read(child / "status/inventory.json")["archives"][0]
        self.assertEqual([p["width"] for p in inventory["pages"]], [83, 81, 82, 81])
        self.assertEqual(inventory["other_files"], 0)
        self.assertNotIn("cover", (child / "status/inventory.json").read_text())
        plan = read(child / "plan.json")
        plan["archives"][record["id"]]["rules"] = [{"model": "fixture"}]
        write(child / "plan.json", plan)
        weights = self.root / "weights"
        weights.write_bytes(b"fixture")
        runtime = self.root / "runtime.json"
        write(runtime, {"models": {"fixture": {"path": str(weights), "sha256": sha(weights)}}})
        self.assertEqual(self.book.read_bytes(), before)
        self.book.rename(self.book.with_suffix(".offline"))
        run(child, child / "plan.json", runtime, FixtureEngine())
        target = target_for(record, plan)
        self.assertEqual(target, output / "library/book.cbz")
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), [f"{i:06d}.jpg" for i in range(1, 5)])
            self.assertIsNone(archive.testzip())
            for name, width in zip(archive.namelist(), (83, 81, 82, 81)):
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    image.load()
                    self.assertEqual(image.size, (width * 4, 400))
        engine = FixtureEngine()
        run(child, child / "plan.json", runtime, engine)
        self.assertEqual(engine.calls, 0)

    def test_trial_count_original_and_passthrough_use_publication_order(self):
        import trial_page

        request = self.root / "trial/request.json"
        content = {"source": str(self.book), "signature": signature(self.book), "book": "fixture"}
        write(request, content)
        with patch("parent_guard.start"), patch.object(sys, "argv", ["trial_page", str(request)]):
            with patch("builtins.print") as printed:
                trial_page.main()
            printed.assert_called_once_with("4")
            write(request, {**content, "page": 0})
            trial_page.main()
            with Image.open(request.with_name("image.png")) as image:
                self.assertEqual(image.size, (83, 100))
            write(request, {**content, "page": 2, "mode": "manga", "plan": {"page_height": 1}})
            with patch("builtins.print"):
                trial_page.main()
            with Image.open(request.with_name("image.png")) as image:
                self.assertEqual(image.size, (82, 100))

    def test_unsafe_urls_and_malformed_epub_fail(self):
        for href in (
            "https://example.com/page.png",
            "//host/page.png",
            "../../page.png",
            "/page.png",
            "C:/page.png",
            "%2e%2e/%2e%2e/page.png",
            "a%5cb.png",
        ):
            with self.subTest(href=href), self.assertRaisesRegex(ValueError, "unsafe_member"):
                member_path("OPS/package.opf", href)
        self.assertEqual(
            member_path("OPS/Text/page.xhtml", "../Images/001.png"), "OPS/Images/001.png"
        )
        for container in (
            "<invalid",
            "<container/>",
            '<container><rootfiles><rootfile full-path="missing.opf"/></rootfiles></container>',
        ):
            with self.subTest(container=container):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr("META-INF/container.xml", container)
                with (
                    zipfile.ZipFile(stream) as archive,
                    self.assertRaisesRegex(ValueError, "invalid_epub"),
                ):
                    archive_entries(archive, True)


if __name__ == "__main__":
    unittest.main()
