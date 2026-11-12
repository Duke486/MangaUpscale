"""Synthetic regressions for persisted defaults, queue boundaries and publication."""

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from book_actions import choice
from default_mode import apply, manual_mode, submit, take_request, task_default
from mode_jobs import prepare_saved, rules_for
from pipeline import read, run, sha, write
from queue_runner import run_queue
from storage import job_lock


class DefaultMode(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def child(self, key):
        child = self.job / "jobs" / (key * 24)
        write(child / "plan.json", {"bw_mode": "manga", "archives": {"a1": {"rules": []}}})
        return child

    def test_manual_confirmations_skips_and_inactive_books_are_preserved(self):
        plain, confirmed, skipped, inactive = [self.child(key) for key in "abcd"]
        write(confirmed / "status/book-preference.json", {"mode": "", "trial": {"mode": "flat"}})
        write(skipped / "status/user-choice.json", {"action": "skip"})
        write(self.job / "state/active-books.json", [p.name for p in (plain, confirmed, skipped)])
        result = apply(self.job, "color")
        self.assertEqual((result["applied"], result["kept"]), (1, 2))
        self.assertEqual(choice(confirmed)["mode"], "flat")
        self.assertEqual(choice(skipped)["action"], "skip")
        self.assertEqual(read(inactive / "plan.json")["bw_mode"], "manga")
        self.assertEqual(task_default(self.job, "manga"), "color")
        self.assertIsNone(manual_mode(plain))

        result = apply(self.job, "flat", force=True)
        self.assertEqual(result["applied"], 3)
        self.assertFalse((confirmed / "status/book-preference.json").exists())
        self.assertFalse((skipped / "status/user-choice.json").exists())
        self.assertEqual(choice(skipped), {"action": "restart", "mode": "flat"})
        # A later per-book selection takes precedence over a pending default rebuild.
        write(plain / "status/book-preference.json", {"mode": "manga"})
        self.assertEqual(choice(plain)["mode"], "manga")

    def test_last_book_request_requeues_completed_books_before_finishing(self):
        children = [self.child(key) for key in "ab"]
        calls = []

        def invoke(child):
            calls.append((child.name, read(child / "plan.json")["bw_mode"]))
            if len(calls) == 2:
                result = submit(self.job, {"mode": "color"})
                self.assertTrue(result["queued"])
                self.assertFalse((self.job / "state/default-mode.json").exists())
            (child / "status/rebuild-bw.json").unlink(missing_ok=True)
            write(child / "status/status.json", {"state": "complete"})
            return 0

        with job_lock(self.job / "controller"):
            result = run_queue(self.job, "unused", invoke=invoke)
        self.assertEqual(calls, [(p.name, mode) for mode in ("manga", "color") for p in children])
        self.assertEqual(
            (result["state"], result["task_completed"], result["attempts"]), ("finished", 2, 4)
        )
        self.assertFalse((self.job / "status/default-mode-request.json").exists())

    def test_failed_update_keeps_intent_and_newer_request_for_retry(self):
        child = self.child("a")
        request = self.job / "status/default-mode-request.json"
        processing = self.job / "status/default-mode-processing.json"
        write(request, {"mode": "color"})
        original = __import__("default_mode")._update_plan

        def fail_update(*args):
            write(request, {"mode": "flat"})
            raise PermissionError("synthetic write failure")

        with patch("default_mode._update_plan", side_effect=fail_update):
            with self.assertRaises(PermissionError):
                take_request(self.job)
        self.assertEqual(read(processing)["mode"], "color")
        self.assertEqual(read(child / "status/rebuild-bw.json")["mode"], "color")
        self.assertEqual(read(child / "plan.json")["bw_mode"], "manga")
        self.assertEqual(read(request)["mode"], "flat")
        with patch("default_mode._update_plan", wraps=original):
            self.assertEqual(take_request(self.job)["applied"], 1)
            self.assertEqual(take_request(self.job)["mode"], "flat")
        self.assertFalse(processing.exists())
        self.assertEqual(task_default(self.job), "flat")

    def test_write_error_is_reported_instead_of_queued_success(self):
        self.child("a")
        with patch("default_mode._update_plan", side_effect=PermissionError("fixture")):
            with self.assertRaises(PermissionError):
                submit(self.job, {"mode": "color"})
        self.assertTrue((self.job / "status/default-mode-processing.json").exists())
        self.assertEqual(submit(self.job, {"mode": "flat"})["mode"], "flat")
        with self.assertRaisesRegex(ValueError, "invalid_default_mode"):
            submit(self.job, {"mode": "invalid"})

    def test_switching_models_republishes_correct_sizes_and_reuses_matching_cache(self):
        from PIL import Image
        from demand import prepare

        source = self.job / "library"
        source.mkdir()
        with zipfile.ZipFile(source / "A.cbz", "w") as archive:
            for name, color in (("bw.png", (80, 80, 80)), ("color.png", (20, 50, 80))):
                data = io.BytesIO()
                Image.new("RGB", (8, 12), color).save(data, "PNG")
                archive.writestr(name, data.getvalue())
        job, output = self.job / "task", self.job / "out"
        prepare(source, job, output, 5, report_only=True)
        prepare_saved(job, source, output, "manga", page_height=400)
        child = next((job / "jobs").iterdir())
        weights = self.job / "weights"
        weights.write_bytes(b"fixture")
        runtime = self.job / "runtime.json"
        ids = {r["model"] for mode in ("manga", "flat", "color") for r in rules_for(mode)}
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
        for mode, calls, bw_size in (
            ("manga", 2, (32, 48)),
            ("color", 3, (16, 24)),
            ("flat", 4, (32, 48)),
            ("manga", 4, (32, 48)),
        ):
            if mode != "manga" or engine.calls:
                apply(job, mode)
            run(child, child / "plan.json", runtime, engine)
            self.assertEqual(engine.calls, calls)
            self.assertFalse((child / "status/rebuild-bw.json").exists())
            with zipfile.ZipFile(output / "library/A.cbz") as archive:
                for name, size in (("bw.jpg", bw_size), ("color.jpg", (16, 24))):
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        self.assertEqual(image.size, size)
                        self.assertEqual(image.format, "JPEG")
        prepare_saved(job, source, output, "flat")
        self.assertEqual(read(child / "plan.json")["bw_mode"], "manga")


if __name__ == "__main__":
    unittest.main()
