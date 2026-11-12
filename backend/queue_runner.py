"""Bounded retries and per-book isolation. Never log source names."""

import os, sys, time, subprocess
from pathlib import Path
from pipeline import read, write
from control import check
from contextlib import contextmanager


@contextmanager
def keep_awake():
    import ctypes

    active = False
    if os.name == "nt":
        active = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
    try:
        yield
    finally:
        if active:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def run_queue(job, runtime, invoke=None, sleep=time.sleep):
    job = Path(job)
    selection = job / "state/active-books.json"
    children = (
        [job / "jobs" / key for key in sorted(read(selection))]
        if selection.is_file()
        else sorted(p.parent for p in (job / "jobs").glob("*/plan.json"))
    )
    from collections import deque

    pending = deque(children)
    completed_keys = set()
    failed = {}
    completed = 0
    attempts = 0
    blocked = False
    skipped_keys = set()

    def succeeded(child):
        completed_keys.add(child.name)
        failed.pop(child.name, None)
        return len(completed_keys)

    def record(state):
        result = {
            "state": state,
            "task_total": len(children),
            "task_completed": completed,
            "task_failed": len(failed),
            "attempts": attempts,
            "failures": list(failed.values()),
            "blocked": blocked,
            "task_skipped": len(skipped_keys),
        }
        write(job / "status/batch-progress.json", result)
        return result

    from default_mode import take_request

    while True:
        request = take_request(job)
        if request:
            affected = set(request.get("applied_tasks", ())) & {child.name for child in children}
            if affected:
                for key in affected:
                    completed_keys.discard(key)
                    failed.pop(key, None)
                    skipped_keys.discard(key)
                order = {child.name: index for index, child in enumerate(children)}
                pending = deque(
                    sorted(
                        {p for p in pending} | {job / "jobs" / key for key in affected},
                        key=lambda child: order.get(child.name, len(children)),
                    )
                )
                completed = len(completed_keys)
            record("processing-batch")
        if not pending:
            if request:
                continue
            break
        from book_actions import take_switch

        switch = take_switch(job)
        if switch:
            target = job / "jobs" / switch["target"]
            if switch.get("mode"):
                from book_actions import restart_bw

                restart_bw(target, switch["mode"])
                completed_keys.discard(target.name)
                completed = len(completed_keys)
            pending = deque(p for p in pending if p != target)
            pending.appendleft(target)
        child = pending.popleft()
        index = children.index(child) + 1
        switched = False
        check()
        skipped_keys.discard(child.name)
        os.environ["MANGA_CHILD_KEY"] = child.name
        write(
            job / "status/active-task.json",
            {
                "task": child.name,
                "index": index,
                "total": len(children),
                "updated_epoch": time.time(),
            },
        )
        from book_actions import choice, apply

        marker = child / "status/rebuild-bw.json"
        if marker.exists():
            rebuild = read(marker)
            if rebuild.get("cleanup_pending"):
                from book_actions import restart_bw

                restart_bw(child, rebuild["mode"])
        decision = choice(child)
        if decision.get("cleanup_pending"):
            apply(child, decision)
        if decision.get("action") == "skip":
            skipped_keys.add(child.name)
            failed.pop(child.name, None)
            write(child / "status/status.json", {"state": "skipped-user"})
            record("processing-batch")
            continue
        from mode_jobs import existing_outputs

        if existing_outputs(child):
            completed = succeeded(child)
            write(child / "status/status.json", {"state": "skipped-existing"})
            record("processing-batch")
            continue
        if decision.get("mode") in ("flat", "manga", "color"):
            plan = read(child / "plan.json")
            if plan.get("bw_mode") != decision["mode"]:
                from mode_jobs import rules_for

                plan["bw_mode"] = decision["mode"]
                for archive in plan["archives"].values():
                    archive["rules"] = rules_for(decision["mode"])
                write(child / "plan.json", plan)
        attempt = 0
        while attempt < 2:
            check()
            attempts += 1
            record("processing-batch")
            if invoke is None:
                from book_actions import invoke as invoke_book

                code = invoke_book(job, child, runtime, index, len(children))
            else:
                code = invoke(child)
            check()
            if isinstance(code, dict) and code.get("switch"):
                target = job / "jobs" / code["switch"]
                if code.get("mode"):
                    from book_actions import restart_bw

                    restart_bw(target, code["mode"])
                    completed_keys.discard(target.name)
                    completed = len(completed_keys)
                pending = deque(p for p in pending if p not in (target, child))
                if child != target:
                    if not existing_outputs(child):
                        pending.append(child)
                    else:
                        completed = succeeded(child)
                pending.appendleft(target)
                switched = True
                break
            if code == -10:
                skipped_keys.add(child.name)
                failed.pop(child.name, None)
                break
            if code == -11:
                continue
            status = (
                read(child / "status/status.json")
                if (child / "status/status.json").exists()
                else {}
            )
            if code == 0 and status.get("state") == "complete":
                completed = succeeded(child)
                break
            error = {k: status.get(k) for k in ("error_type", "code", "errno", "winerror", "page")}
            if error["code"] == "insufficient_disk_space" or error["winerror"] == 112:
                failed[child.name] = {"task": child.name, **error}
                blocked = True
                break
            retryable = (
                error["error_type"]
                in ("PermissionError", "OSError", "TimeoutError", "ConnectionError")
                or not status
            )
            if retryable and attempt == 0:
                write(job / "status/status.json", {"state": "retrying-book", "attempt": 2, **error})
                attempt += 1
                sleep(2)
                continue
            failed[child.name] = {"task": child.name, **error}
            write(
                job / "status/status.json",
                {"state": "book-failed-continue", "failed_tasks": len(failed), **error},
            )
            break
        record("processing-batch")
        if blocked:
            break
    return record("blocked" if blocked else ("finished_with_errors" if failed else "finished"))
