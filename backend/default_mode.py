"""Task-wide default style with explicit per-book choices preserved."""

from contextlib import ExitStack
from pathlib import Path

from pipeline import read, write

MODES = ("flat", "manga", "color")
MANUAL_FILES = ("book-preference.json", "user-choice.json", "rebuild-bw.json")


def mode_path(job):
    return Path(job) / "state/default-mode.json"


def save(job, mode):
    if mode not in MODES:
        raise ValueError("invalid_default_mode")
    write(mode_path(job), {"mode": mode})


def task_default(job, fallback="manga"):
    try:
        mode = read(mode_path(job)).get("mode")
    except (OSError, ValueError):
        mode = None
    if mode in MODES:
        return mode
    save(job, fallback)
    return fallback


def manual_mode(child):
    child = Path(child)
    for name in MANUAL_FILES:
        try:
            data = read(child / "status" / name)
        except (OSError, ValueError):
            continue
        # A book marked by a previous default-style change is not a manual choice.
        if name == "rebuild-bw.json" and data.get("origin") == "default":
            continue
        mode = data.get("mode") or data.get("trial", {}).get("mode")
        if mode in MODES:
            return mode
    return None


def _update_plan(child, mode):
    from mode_jobs import rules_for

    plan = read(child / "plan.json")
    plan["bw_mode"] = mode
    for archive in plan["archives"].values():
        archive["rules"] = rules_for(mode)
    write(child / "plan.json", plan)


def _mark_rebuild(child, mode):
    # Persist intent before changing plans so existing outputs cannot hide an interrupted update.
    write(
        child / "status/rebuild-bw.json",
        {"mode": mode, "cleanup_pending": False, "origin": "default"},
    )
    _update_plan(child, mode)
    (child / "state/preview.json").unlink(missing_ok=True)


def apply(job, mode, force=False):
    job = Path(job)
    if mode not in MODES:
        raise ValueError("invalid_default_mode")
    selection = job / "state/active-books.json"
    keys = set(read(selection)) if selection.is_file() else None
    children = sorted(path.parent for path in (job / "jobs").glob("*/plan.json"))
    if keys is not None:
        children = [child for child in children if child.name in keys]
    applied = []
    kept = 0
    for child in children:
        manual = manual_mode(child)
        skipped = child / "status/user-choice.json"
        skip = skipped.is_file() and read(skipped).get("action") == "skip"
        if (manual or skip) and not force:
            kept += 1
            continue
        _mark_rebuild(child, mode)
        if force:
            for name in ("book-preference.json", "user-choice.json"):
                (child / "status" / name).unlink(missing_ok=True)
        applied.append(child.name)
    save(job, mode)
    result = {
        "mode": mode,
        "force": bool(force),
        "applied": len(applied),
        "kept": kept,
        "total": len(children),
        "applied_tasks": applied,
    }
    write(job / "status/default-mode-result.json", result)
    return result


def submit(job, request):
    from storage import JobBusy, job_lock

    job = Path(job)
    if request.get("mode") not in MODES:
        raise ValueError("invalid_default_mode")
    write(job / "status/default-mode-request.json", request)
    with ExitStack() as stack:
        try:
            stack.enter_context(job_lock(job / "controller"))
        except JobBusy:
            return {"queued": True, "applied": 0, "kept": 0}
        result = None
        while applied := take_request(job):
            result = applied
        return {"queued": False, **(result or {"applied": 0, "kept": 0})}


def take_request(job):
    job = Path(job)
    path = job / "status/default-mode-request.json"
    processing = job / "status/default-mode-processing.json"
    if not processing.exists():
        try:
            path.replace(processing)
        except FileNotFoundError:
            return None
    request = read(processing)
    if request.get("mode") not in MODES:
        processing.unlink()
        return None
    result = apply(job, request["mode"], bool(request.get("force")))
    processing.unlink()
    return result
