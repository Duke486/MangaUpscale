"""All fixtures are generated locally; no real input or model is accessed."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
from pipeline import read, write
from event_stream import read_events
from queue_runner import run_queue
from book_actions import choice
from storage import job_lock
import controller


class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def child(self, name="a" * 24):
        child = self.root / "jobs" / name
        write(child / "plan.json", {"archives": {}})
        return child

    def test_conflict_policy_and_explicit_rescan(self):
        import io
        import zipfile
        from PIL import Image
        from demand import prepare
        from mode_jobs import prepare_saved

        source = self.root / "library"
        source.mkdir()
        output = self.root / "output"
        job = self.root / "job"
        image = io.BytesIO()
        Image.new("RGB", (80, 100), "blue").save(image, format="PNG")
        for name in ("A.zip", "A.cbz", "A(1).zip"):
            with zipfile.ZipFile(source / name, "w") as archive:
                archive.writestr("001.png", image.getvalue())

        prepare(source, job, output, 5, report_only=True)
        with self.assertRaisesRegex(ValueError, "output_name_collision"):
            prepare_saved(job, source, output, "manga")
        prepare_saved(job, source, output, "manga", conflicts="skip")
        self.assertEqual(read(job / "status/status.json")["name_conflicts_skipped"], 1)
        self.assertEqual(len(read(job / "state/active-books.json")), 2)

        prepare_saved(job, source, output, "manga", conflicts="number")
        assigned = read(job / "state/output-paths.json")
        self.assertEqual(set(assigned.values()), {"A.cbz", "A(1).cbz", "A(2).cbz"})
        (source / "A.cbz").unlink()
        prepare_saved(job, source, output, "manga")
        self.assertEqual(read(job / "state/output-paths.json"), assigned)
        self.assertEqual(len(read(job / "state/active-books.json")), 3)

        prepare(source, job, output, 5, report_only=True)
        prepare_saved(job, source, output, "manga")
        called = []

        def invoke(child):
            called.append(child.name)
            write(child / "status/status.json", {"state": "complete"})
            return 0

        result = run_queue(job, "unused", invoke=invoke)
        self.assertEqual(result["task_completed"], 2)
        self.assertEqual(set(called), set(read(job / "state/active-books.json")))
        self.assertEqual(len(list((job / "jobs").glob("*/plan.json"))), 3)
        key = called[0]
        write(job / "jobs" / key / "status/book-preference.json", {"mode": "flat"})
        prepare_saved(job, source, output, "manga", book_keys=[key])
        self.assertEqual(read(job / "state/active-books.json"), [key])
        self.assertEqual(read(job / "jobs" / key / "plan.json")["bw_mode"], "flat")
        called.clear()
        run_queue(job, "unused", invoke=invoke)
        self.assertEqual(called, [key])

    def test_blank_guard_reports_page_and_allows_blank_source(self):
        from PIL import Image
        from pipeline import check_blank_result, error_report

        white = Image.new("RGB", (32, 32), "white")
        check_blank_result(white, white, 8)
        source = white.copy()
        source.putpixel((4, 4), (0, 0, 0))
        with self.assertRaisesRegex(ValueError, "unexpected_blank_page") as failure:
            check_blank_result(source, white, 8)
        self.assertEqual(error_report(failure.exception)["page"], 8)

    def test_output_root_cannot_resolve_to_input(self):
        from demand import prepare

        source = self.root / "library"
        source.mkdir()
        with self.assertRaisesRegex(ValueError, "output_overlaps_input"):
            prepare(source, self.root / "job", self.root, 5, report_only=True)

    def test_empty_queue(self):
        result = run_queue(self.root, "unused")
        self.assertEqual(result["state"], "finished")
        self.assertEqual(result["task_total"], 0)

    def test_publish_failure_does_not_write_complete_receipt(self):
        import io
        import zipfile
        from PIL import Image
        from pipeline import scan, run, sha
        from selftest import FixtureEngine

        source = self.root / "source"
        source.mkdir()
        image = io.BytesIO()
        Image.new("RGB", (8, 12), (20, 50, 80)).save(image, format="PNG")
        with zipfile.ZipFile(source / "fixture.cbz", "w") as archive:
            archive.writestr("01.png", image.getvalue())
        job = self.root / "job"
        scan(source, job, output_root=self.root / "out")
        aid = read(job / "status/inventory.json")["archives"][0]["id"]
        weights = self.root / "weights"
        weights.write_bytes(b"fixture")
        runtime = self.root / "runtime.json"
        write(runtime, {"models": {"fixture": {"path": str(weights), "sha256": sha(weights)}}})
        write(job / "plan.json", {"scale": 4, "archives": {aid: {"rules": [{"model": "fixture"}]}}})
        original = Path.rename

        def fail_publish(path, target):
            if path.suffix == ".partial":
                raise PermissionError("synthetic publication failure")
            return original(path, target)

        with patch.object(Path, "rename", fail_publish):
            with self.assertRaises(PermissionError):
                run(job, job / "plan.json", runtime, FixtureEngine())
        self.assertFalse((job / "status" / ("receipt-" + aid + ".json")).exists())
        self.assertTrue(list((self.root / "out").rglob("*.partial")))

    def test_lock_does_not_grow_or_allow_second_owner(self):
        for _ in range(3):
            with job_lock(self.root):
                with self.assertRaises(OSError):
                    with job_lock(self.root):
                        self.fail("duplicate owner")
        self.assertEqual((self.root / "state/session.lock").stat().st_size, 1)

    def test_main_keeps_scan_failures_and_blocked_state(self):
        import demand

        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                write(
                    self.root / "status/demand-summary.json", {"failed": 1, "scan_complete": True}
                )

                def prepare(*args, **kwargs):
                    write(self.root / "status/status.json", {"state": "prepared-reused"})
                    return True

                batch = {
                    "state": "blocked" if blocked else "finished",
                    "blocked": blocked,
                    "task_failed": int(blocked),
                }
                args = [
                    "demand",
                    "--input",
                    str(self.root / "unused"),
                    "--job",
                    str(self.root),
                    "--run",
                ]
                with (
                    patch("parent_guard.start"),
                    patch("mode_jobs.prepare_saved", side_effect=prepare),
                    patch("queue_runner.run_queue", return_value=batch),
                    patch.object(sys, "argv", args),
                    patch("builtins.print"),
                ):
                    with self.assertRaises(SystemExit) as result:
                        demand.main()
                self.assertEqual(result.exception.code, 1)
                self.assertEqual(
                    read(self.root / "status/status.json")["state"],
                    "blocked" if blocked else "finished_with_errors",
                )

    def test_skip_intent_survives_restart(self):
        child = self.child()
        write(child / "status/user-choice.json", {"action": "skip", "cleanup_pending": True})
        self.assertTrue(choice(child)["cleanup_pending"])
        write(child / "status/user-choice.json", {"action": "skip", "cleanup_pending": False})
        invoke = Mock(side_effect=AssertionError("skipped task ran"))
        result = run_queue(self.root, "unused", invoke=invoke)
        self.assertEqual(result["task_skipped"], 1)
        invoke.assert_not_called()

    def test_restart_intent_keeps_pending_cleanup(self):
        child = self.child()
        write(
            child / "status/user-choice.json",
            {"action": "restart", "mode": "manga", "cleanup_pending": True},
        )
        self.assertTrue(choice(child)["cleanup_pending"])

    def test_retry_then_success(self):
        child = self.child()
        calls = []

        def invoke(task):
            calls.append(task)
            write(
                task / "status/status.json",
                {"state": "failed", "error_type": "OSError"}
                if len(calls) == 1
                else {"state": "complete"},
            )
            return 1 if len(calls) == 1 else 0

        result = run_queue(self.root, "unused", invoke=invoke, sleep=lambda _: None)
        self.assertEqual(
            (result["task_completed"], result["task_failed"], result["attempts"]), (1, 0, 2)
        )

    def test_disk_full_blocks_without_retry(self):
        child = self.child()

        def invoke(task):
            write(
                task / "status/status.json", {"state": "failed", "code": "insufficient_disk_space"}
            )
            return 1

        result = run_queue(self.root, "unused", invoke=invoke)
        self.assertEqual((result["state"], result["attempts"]), ("blocked", 1))

    def test_failure_cleared_when_switched_back_and_successful(self):
        a = self.child()
        b = self.child("b" * 24)
        calls = []

        def invoke(task):
            calls.append(task)
            if calls == [a]:
                write(a / "status/status.json", {"state": "failed", "error_type": "ValueError"})
                return 1
            if calls == [a, b]:
                return {"switch": a.name}
            write(task / "status/status.json", {"state": "complete"})
            return 0

        result = run_queue(self.root, "unused", invoke=invoke)
        self.assertEqual(result["task_completed"], 2)
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["state"], "finished")

    def test_events_bounded_and_partial_line_retried(self):
        path = self.root / "events.jsonl"
        lines = [
            json.dumps({"state": "running", "completed": i}).encode() + b"\n" for i in range(600)
        ]
        path.write_bytes(b"".join(lines) + b'{"state":')
        offset = 0
        found = []
        for _ in range(3):
            events, offset = read_events(path, offset)
            self.assertLessEqual(len(events), 256)
            found.extend(events)
        self.assertEqual(len(found), 600)
        self.assertEqual(read_events(path, offset), ([], offset))
        with path.open("ab") as stream:
            stream.write(b'"complete"}\n')
        self.assertEqual(read_events(path, offset)[0], [{"state": "complete"}])
        path.write_bytes(b'[]\ninvalid\n{"state":"new"}\n')
        self.assertEqual(read_events(path, offset)[0], [{"state": "new"}])

    def test_gui_rejects_non_object_json(self):
        path = self.root / "status.json"
        path.write_text("[]")
        self.assertIsNone(controller.read_json(path))

    def test_gui_parent_phase_visible(self):
        app = controller.Controller()
        app.job = self.root
        app.started_at = 1
        app.status = Mock()
        app.summary = Mock()
        app.detail = Mock()
        app.progress = Mock()
        app.process = None
        app.log = Mock()
        app.last_event = None
        write(self.root / "status/status.json", {"state": "scanning", "completed": 2, "total": 3})
        app.poll_progress()
        self.assertIn("检查页面", app.status.set.call_args.args[0])

    def test_tick_does_not_steal_stop_completion(self):
        app = controller.Controller()
        app.closing = False
        app.job = None
        app.process = Mock()
        app.process.poll.return_value = 1
        app.stopping = True
        app.root = Mock()
        app.busy = Mock()
        app.tick()
        self.assertIsNotNone(app.process)
        app.busy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
