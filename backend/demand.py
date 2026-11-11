"""Sample archives and prepare a resumable processing queue."""

import argparse, contextlib, os, sys, zipfile, hashlib, subprocess
from pathlib import Path
from PIL import ImageFile
from control import check, Stopped
from queue_runner import keep_awake
from path_identity import batch_output_root, same_location
from pipeline import read, write, scan, IMAGES, error_report
from storage import default_job, signature, job_lock
from archive_pages import ARCHIVES, archive_entries


def default_threshold():
    settings = Path(__file__).resolve().parents[1] / "demand-settings.json"
    return float(read(settings)["threshold"]) if settings.exists() else 1.10


def sample(path, count=5, threshold=None, height_metric="long-edge"):
    threshold = default_threshold() if threshold is None else threshold
    sizes = []
    with zipfile.ZipFile(path) as z:
        entries = [
            i
            for i in archive_entries(z, Path(path).suffix.lower() == ".epub")
            if Path(i.filename).suffix.lower() in IMAGES
        ][:count]
        if not entries:
            raise ValueError("archive_has_no_pages")
        for entry in entries:
            check()
            parser = ImageFile.Parser()
            with z.open(entry) as f:
                for _ in range(256):
                    data = f.read(4096)
                    if not data:
                        break
                    parser.feed(data)
                    if parser.image:
                        break
            if not parser.image:
                raise ValueError("header_limit_exceeded")
            sizes.append(parser.image.size)
    # Fit whole page; landscape pages use landscape screen orientation.
    ratios = [min((2752 if w > h else 2064) / w, (2064 if w > h else 2752) / h) for w, h in sizes]
    scale = (
        max(2752 / max(w, h) for w, h in sizes)
        if height_metric == "long-edge"
        else round(max(ratios), 3)
    )
    return {
        "sample_count": len(sizes),
        "needs_upscale": scale > threshold,
        "max_display_scale": scale,
    }


def summarize(items, complete=False):
    values = list(items.values())
    checked = len(values)
    failed = sum(v["state"] == "failed" for v in values)
    evaluated = checked - failed
    selected = sum(v["state"] == "sampled" and v.get("needs_upscale", False) for v in values)
    return {
        "checked": checked,
        "evaluated": evaluated,
        "selected": selected,
        "not_needed": evaluated - selected,
        "failed": failed,
        "needs_upscale_percent": round(100 * selected / evaluated, 2) if evaluated else None,
        "percent_denominator": "evaluated_archives",
        "scan_complete": complete,
    }


def prepare(source, job, output, count, report_only=False, threshold=None):
    threshold = default_threshold() if threshold is None else threshold
    source = Path(source).resolve()
    job = Path(job).resolve()
    output = Path(output).resolve()
    batch_output_root(source, output)
    if source == job or source in job.parents or source == output or source in output.parents:
        raise ValueError("local_paths_inside_input")
    with job_lock(job):
        config = {
            "source": str(source),
            "output": str(output),
            "samples": count,
            "threshold": threshold,
            "height_metric": "long-edge",
        }
        cp = job / "state/demand.json"
        checkpoint = job / "state/demand-scan.json"
        data = (
            read(checkpoint)
            if checkpoint.exists()
            else (read(cp) if cp.exists() else {"config": config, "items": {}})
        )
        saved = data["config"]
        if (
            not same_location(saved["source"], source)
            or not same_location(saved["output"], output)
            or saved["samples"] != count
            or saved["threshold"] != threshold
        ):
            raise ValueError("job_input_changed")
        config = saved
        if not source.is_dir():
            raise ValueError("input_unavailable")
        current = {}

        def walk_error(error):
            raise error

        for directory, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
            dirs[:] = [
                d
                for d in dirs
                if not (Path(directory) / d).is_symlink()
                and not getattr((Path(directory) / d).lstat(), "st_file_attributes", 0) & 1024
            ]
            for name in files:
                check()
                path = Path(directory) / name
                if path.is_symlink() or path.suffix.lower() not in ARCHIVES:
                    continue
                key = hashlib.sha256(str(path.relative_to(source)).encode()).hexdigest()[:24]
                stamp = signature(path)
                old = data["items"].get(key, {})
                if (
                    old.get("signature")
                    and old["signature"] != stamp
                    and (job / "jobs" / key).exists()
                ):
                    raise ValueError("job_input_changed")
                if old.get("signature") == stamp and old.get("state") == "sampled":
                    current[key] = old
                    continue
                try:
                    result = sample(path, count, threshold, config.get("height_metric", "fit-page"))
                    if signature(path) != stamp:
                        raise ValueError("source_changed_during_copy")
                    data["items"][key] = {
                        "source": str(path),
                        "signature": stamp,
                        "state": "sampled",
                        **result,
                    }
                except Stopped:
                    raise
                except Exception as e:
                    data["items"][key] = {
                        "source": str(path),
                        "signature": stamp,
                        "state": "failed",
                        "error_type": type(e).__name__,
                    }
                write(checkpoint, data)
                current[key] = data["items"][key]
                write(job / "status/status.json", {"state": "scanning", **summarize(current)})
        data["items"] = current
        write(cp, data)
        checkpoint.unlink(missing_ok=True)
        summary = summarize(current, complete=True)
        write(job / "status/demand-summary.json", summary)
        if report_only:
            write(job / "status/status.json", {"state": "reported", **summary})
            return
        targets = set()
        for key, item in data["items"].items():
            if not item.get("needs_upscale"):
                continue
            path = Path(item["source"])
            relative = path.relative_to(source)
            base = output / source.name / relative.with_suffix("")
            if str(base).casefold() in targets:
                raise ValueError("output_name_collision")
            targets.add(str(base).casefold())
        selected_index = 0
        for key, item in data["items"].items():
            if not item.get("needs_upscale"):
                continue
            selected_index += 1
            os.environ["MANGA_CHILD_KEY"] = key
            write(
                job / "status/active-task.json",
                {
                    "task": key,
                    "index": selected_index,
                    "total": len(targets),
                    "updated_epoch": __import__("time").time(),
                },
            )
            child = job / "jobs" / key
            path = Path(item["source"])
            scan(path, child, output_root=output)
            mapping = read(child / "state/map.json")
            for record in mapping["archives"]:
                record["output_base"] = str(
                    output / source.name / path.relative_to(source).with_suffix("")
                )
            write(child / "state/map.json", mapping)
            if not (child / "plan.json").exists():
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("make_default_plan.py")),
                        "--job",
                        str(child),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            plan = read(child / "plan.json")
            plan["output_suffix"] = ""
            receipts = [
                child / "status" / ("receipt-" + v["id"] + ".json") for v in mapping["archives"]
            ]
            already_completed = bool(receipts) and all(
                v.exists() and read(v).get("state") == "complete" for v in receipts
            )
            if not already_completed:
                plan["page_threshold"] = threshold
            write(child / "plan.json", plan)
            # Keep scan checkpoints consistent with the destination override.
            for record in mapping["archives"]:
                checkpoint = child / "state/checkpoints" / (record["id"] + ".json")
                saved = read(checkpoint)
                saved["state"] = record
                write(checkpoint, saved)
        write(
            job / "status/status.json",
            {
                "state": "prepared",
                "jobs": len(targets),
                "failed": sum(v["state"] == "failed" for v in data["items"].values()),
            },
        )


def resume_prepared(job, source, output, count, threshold):
    cp = job / "state/demand.json"
    summary_path = job / "status/demand-summary.json"
    if not cp.exists() or not summary_path.exists():
        return False
    data = read(cp)
    config = data["config"]
    summary = read(summary_path)
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    if (
        not same_location(config["source"], source)
        or not same_location(config["output"], output)
        or config["samples"] != count
        or config.get("threshold") != threshold
    ):
        return False
    selected = [key for key, item in data["items"].items() if item.get("needs_upscale")]
    if not summary.get("scan_complete") or len(selected) != summary["selected"]:
        return False
    for key in selected:
        child = job / "jobs" / key
        if not all(
            (child / name).exists()
            for name in ("plan.json", "state/map.json", "status/inventory.json")
        ):
            return False
    for key in selected:
        child = job / "jobs" / key
        mapping = read(child / "state/map.json")
        plan = read(child / "plan.json")
        receipts = [
            child / "status" / ("receipt-" + v["id"] + ".json") for v in mapping["archives"]
        ]
        if not (
            bool(receipts)
            and all(v.exists() and read(v).get("state") == "complete" for v in receipts)
        ):
            plan["page_threshold"] = threshold
            write(child / "plan.json", plan)
    write(job / "status/status.json", {"state": "prepared-reused", "jobs": len(selected)})
    return True


def main():
    from parent_guard import start as guard_parent

    guard_parent()
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--job")
    p.add_argument("--output-root", default="./output")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--threshold", type=float, default=default_threshold())
    p.add_argument("--page-height", type=int)
    p.add_argument("--bw-mode", choices=["manga", "flat"], default="manga")
    p.add_argument("--allow-above-recommended", action="store_true")
    p.add_argument("--repack95", action="store_true")
    p.add_argument("--output-conflicts", choices=["number", "skip"])
    p.add_argument("--book-key", action="append")
    p.add_argument("--run", action="store_true")
    p.add_argument("--report-only", action="store_true")
    a = p.parse_args()
    job = (
        Path(a.job)
        if a.job
        else default_job(a.input, "./cache", True, "demand-" + str(a.threshold))
    )
    os.environ.setdefault("MANGA_STOP_FILE", str(job / "status/stop.request"))
    os.environ["MANGA_PAUSE_FILE"] = str(job / "status/pause.request")
    os.environ["MANGA_EVENT_FILE"] = str(job / "status/events.jsonl")
    try:
        if not 1 <= a.threshold <= 100:
            raise ValueError("invalid_threshold")
        if a.repack95 and not a.run:
            raise ValueError("repack_requires_run")
        if a.run and a.report_only:
            raise ValueError("report_only_conflicts_with_run")
        if not 1 <= a.samples <= 10:
            raise ValueError("invalid_sample_count")
        if a.page_height is not None and not 400 <= a.page_height <= 2732:
            raise ValueError("invalid_page_height")
        with (
            keep_awake(),
            job_lock(job / "controller"),
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            repack_failed = 0
            if a.repack95:
                from repack95 import repack_task

                repack_failed = repack_task(job, a.input, a.output_root)["failed"]
            from mode_jobs import prepare_saved

            if a.report_only:
                prepare(a.input, job, a.output_root, a.samples, True, a.threshold)
            elif not prepare_saved(
                job,
                a.input,
                a.output_root,
                a.bw_mode,
                not a.allow_above_recommended,
                a.page_height,
                a.output_conflicts,
                a.book_key,
            ):
                prepare(a.input, job, a.output_root, a.samples, True, a.threshold)
                prepare_saved(
                    job,
                    a.input,
                    a.output_root,
                    a.bw_mode,
                    not a.allow_above_recommended,
                    a.page_height,
                    a.output_conflicts,
                    a.book_key,
                )
            skipped_names = 0
            if a.run:
                skipped_names = read(job / "status/status.json").get("name_conflicts_skipped", 0)
                from queue_runner import run_queue

                batch = run_queue(job, Path(__file__).resolve().parents[1] / "runtime.json")
                write(job / "status/status.json", batch)
        summary = read(job / "status/demand-summary.json")
        final_status = read(job / "status/status.json")
        failed = summary.get("failed", 0) + final_status.get("task_failed", 0) + repack_failed
        state = (
            "blocked"
            if final_status.get("blocked")
            else "finished_with_errors"
            if failed
            else ("reported" if a.report_only else ("finished" if a.run else "prepared"))
        )
        summary = read(job / "status/demand-summary.json")
        batch_summary = (
            read(job / "status/batch-progress.json")
            if a.run and (job / "status/batch-progress.json").exists()
            else {}
        )
        write(
            job / "status/status.json",
            {
                **summary,
                **batch_summary,
                "state": state,
                "repack_failed": repack_failed,
                "name_conflicts_skipped": skipped_names,
            },
        )
        print(__import__("json").dumps({"state": state, "job": str(job), **summary}))
        if failed:
            sys.exit(1)
    except Exception as e:
        if os.environ.get("MANGA_STOP_FILE") and Path(os.environ["MANGA_STOP_FILE"]).exists():
            e = Stopped("stopped_by_user")
        report = error_report(e)
        try:
            write(job / "status/status.json", report)
        except OSError:
            pass
        print(__import__("json").dumps(report))
        sys.exit(1)


if __name__ == "__main__":
    main()
