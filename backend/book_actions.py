"""Book commands are executed only by the queue after the worker exits."""

import os, shutil, time, uuid, subprocess
from pathlib import Path
from pipeline import read, write
from storage import job_lock


def choice(child):
    child = Path(child)
    rebuilding = {}
    try:
        rebuilding = read(child / "status/rebuild-bw.json")
        if rebuilding.get("mode") in ("flat", "manga", "color") and rebuilding.get(
            "origin"
        ) not in ("default", "color-detection"):
            return {"action": "restart", "mode": rebuilding["mode"]}
    except (OSError, ValueError):
        pass
    try:
        preference = read(child / "status/book-preference.json")
        mode = preference.get("mode") or preference.get("trial", {}).get("mode")
        if mode in ("flat", "manga", "color"):
            return {"action": "restart", "mode": mode}
        if preference.get("mode") == "" and rebuilding.get("mode") not in (
            "flat",
            "manga",
            "color",
        ):
            return {}
    except (OSError, ValueError):
        pass
    try:
        old = read(child / "status/user-choice.json")
        if old.get("action") in ("skip", "restart"):
            return old
    except (OSError, ValueError):
        pass
    if rebuilding.get("mode") in ("flat", "manga", "color"):
        return {"action": "restart", "mode": rebuilding["mode"]}
    return {}


def clear_processed(child):
    child = Path(child).resolve()
    target = child / "processed"
    if target.exists():
        if target.resolve().parent != child or target.is_symlink():
            raise ValueError("unsafe_cache_path")
        for directory, dirs, files in os.walk(target):
            for name in dirs + files:
                p = Path(directory) / name
                if p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 1024:
                    raise ValueError("unsafe_cache_path")
        shutil.rmtree(target)
    (child / "state/preview.json").unlink(missing_ok=True)


def apply(child, command):
    from mode_jobs import rules_for, existing_outputs

    child = Path(child)
    with job_lock(child):
        if existing_outputs(child):
            return "already-complete"
        action = command["action"]
        if action not in ("skip", "restart"):
            raise ValueError("invalid_book_action")
        if action == "restart" and command.get("mode") not in ("flat", "manga", "color"):
            raise ValueError("invalid_book_mode")
        # Persist intent before deletion, so an interrupted cleanup is retried.
        write(
            child / "status/user-choice.json",
            {"action": action, "mode": command.get("mode"), "cleanup_pending": True},
        )
        clear_processed(child)
        if action == "restart":
            plan = read(child / "plan.json")
            plan["bw_mode"] = command["mode"]
            for archive in plan["archives"].values():
                archive["rules"] = rules_for(command["mode"])
            write(child / "plan.json", plan)
        write(
            child / "status/user-choice.json",
            {"action": action, "mode": command.get("mode"), "cleanup_pending": False},
        )
        write(
            child / "status/status.json",
            {"state": "skipped-user" if action == "skip" else "restarting-book"},
        )
        return action


def restart_bw(child, mode):
    child = Path(child).resolve()
    if mode not in ("flat", "manga", "color"):
        raise ValueError("invalid_mode")
    with job_lock(child):
        inventory = read(child / "status/inventory.json")
        root = child / "processed"
        if root.exists() and (root.is_symlink() or root.resolve().parent != child):
            raise ValueError("unsafe_cache_path")
        # Persist rebuild intent first. Completed outputs stay intact until replacement validates.
        write(child / "status/rebuild-bw.json", {"mode": mode, "cleanup_pending": True})
        write(child / "status/book-preference.json", {"mode": mode})
        for archive in inventory["archives"]:
            base = root / archive["id"]
            if not base.exists():
                continue
            if base.resolve().parent != root or base.is_symlink():
                raise ValueError("unsafe_cache_path")
            orders = {
                str(page["order"])
                for page in archive["pages"]
                if mode == "color" or not page["color"]
            }
            for path in base.rglob("*"):
                if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 1024:
                    raise ValueError("unsafe_cache_path")
                if path.is_file() and path.name.split(".")[0] in orders:
                    if root not in path.resolve().parents:
                        raise ValueError("unsafe_cache_path")
                    path.unlink()
        (child / "state/preview.json").unlink(missing_ok=True)
        from mode_jobs import rules_for

        plan = read(child / "plan.json")
        plan["bw_mode"] = mode
        for archive in plan["archives"].values():
            archive["rules"] = rules_for(mode)
        write(child / "plan.json", plan)
        write(child / "status/rebuild-bw.json", {"mode": mode, "cleanup_pending": False})
        write(child / "status/status.json", {"state": "restarting-bw"})


def take_switch(job):
    job = Path(job)
    path = job / "status/switch-request.json"
    if not path.exists():
        return None
    request = read(path)
    path.unlink(missing_ok=True)
    target = request.get("target", "")
    selection = job / "state/active-books.json"
    if (
        not __import__("re").fullmatch("[a-f0-9]{24}", target)
        or not (job / "jobs" / target / "plan.json").is_file()
        or (selection.is_file() and target not in read(selection))
    ):
        write(
            job / "status/switch-result.json", {"id": request.get("id"), "state": "invalid-target"}
        )
        return None
    if request.get("mode") is not None and request["mode"] not in ("flat", "manga", "color"):
        raise ValueError("invalid_mode")
    return request


def invoke(job, child, runtime, index, total, worker_script=None):
    from control import check

    job = Path(job)
    child = Path(child)
    token = uuid.uuid4().hex
    marker = child / "status/book-stop.request"
    marker.unlink(missing_ok=True)
    active = {
        "task": child.name,
        "run_id": token,
        "index": index,
        "total": total,
        "controllable": True,
        "updated_epoch": time.time(),
    }
    write(job / "status/active-task.json", active)
    proc = subprocess.Popen(
        [
            __import__("sys").executable,
            str(worker_script or Path(__file__).with_name("worker.py")),
            "--job",
            str(child),
            "--plan",
            str(child / "plan.json"),
            "--runtime",
            str(runtime),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={
            **os.environ,
            "MANGA_BOOK_STOP_FILE": str(marker),
            "MANGA_OWNER_PID": str(os.getpid()),
        },
    )
    command = None
    deadline = None
    try:
        while proc.poll() is None:
            check(pause=False)
            if command is None:
                switch = take_switch(job)
                if switch:
                    command = {**switch, "action": "switch"}
                    marker.write_text("stop")
                    deadline = time.monotonic() + 0.5
                    write(
                        job / "status/switch-result.json",
                        {
                            "id": switch.get("id"),
                            "state": "stopping-book",
                            "target": switch["target"],
                        },
                    )
            path = job / "status/book-command.json"
            if command is None and path.exists():
                request = read(path)
                path.unlink(missing_ok=True)
                if (
                    request.get("task") == child.name
                    and request.get("run_id") == token
                    and request.get("action") in ("skip", "restart")
                    and (
                        request["action"] == "skip"
                        or request.get("mode") in ("flat", "manga", "color")
                    )
                ):
                    command = request
                    marker.write_text("stop")
                    deadline = time.monotonic() + 1
                    write(
                        job / "status/book-command-result.json",
                        {"id": request.get("id"), "state": "stopping-book"},
                    )
                else:
                    write(
                        job / "status/book-command-result.json",
                        {"id": request.get("id"), "state": "stale-command"},
                    )
            if deadline is not None and time.monotonic() >= deadline:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                proc.wait(timeout=10)
            time.sleep(0.1)
        if command and command["action"] == "switch":
            write(
                job / "status/switch-result.json",
                {"id": command.get("id"), "state": "switching", "target": command["target"]},
            )
            return {"switch": command["target"], "mode": command.get("mode")}
        if command:
            try:
                result = apply(child, command)
            except Exception:
                write(
                    job / "status/book-command-result.json",
                    {"id": command.get("id"), "state": "action-failed"},
                )
                raise
            write(
                job / "status/book-command-result.json", {"id": command.get("id"), "state": result}
            )
            return -10 if result == "skip" else (-11 if result == "restart" else proc.returncode)
        return proc.returncode
    finally:
        if proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            proc.wait(timeout=10)
        active["controllable"] = False
        write(job / "status/active-task.json", active)
        path = job / "status/book-command.json"
        if path.exists():
            request = read(path)
            if request.get("run_id") == token:
                path.unlink(missing_ok=True)
                write(
                    job / "status/book-command-result.json",
                    {"id": request.get("id"), "state": "stale-command"},
                )
        marker.unlink(missing_ok=True)
