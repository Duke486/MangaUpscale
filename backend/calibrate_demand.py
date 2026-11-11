"""Estimate a page-height threshold from a random sample."""

import argparse, os, random, secrets, sys, contextlib, math
from pathlib import Path
from demand import sample
from pipeline import read, write, error_report
from storage import signature, job_lock
from archive_pages import ARCHIVES


def cutoff(values):
    # Strict greater-than at the boundary keeps ties together and stays below cap.
    ordered = sorted(values, reverse=True)
    cap = 2 * len(ordered) // 3
    return max(1.10, ordered[cap]) if ordered else None


def calibrate(source, job, size, reuse=None):
    source = Path(source).resolve()
    job = Path(job).resolve()
    if not source.is_dir():
        raise ValueError("input_unavailable")
    if source == job or source in job.parents:
        raise ValueError("local_paths_inside_input")
    with job_lock(job):
        state = job / "state/random-sample.json"
        if state.exists():
            data = read(state)
            if data["source"] != str(source) or data["size"] != size:
                raise ValueError("job_input_changed")
        else:
            seed = secrets.randbits(128)
            rng = random.Random(seed)
            chosen = []
            total = 0

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
                    path = Path(directory) / name
                    if path.is_symlink() or path.suffix.lower() not in ARCHIVES:
                        continue
                    total += 1
                    if len(chosen) < size:
                        chosen.append(str(path))
                    else:
                        index = rng.randrange(total)
                        if index < size:
                            chosen[index] = str(path)
            data = {
                "source": str(source),
                "size": size,
                "seed": seed,
                "population": total,
                "chosen": chosen,
                "results": {},
            }
            write(state, data)
        cached = {}
        if reuse and Path(reuse).exists():
            cached = {v["source"]: v for v in read(reuse)["items"].values()}
        for i, name in enumerate(data["chosen"]):
            if str(i) in data["results"]:
                continue
            try:
                path = Path(name)
                stamp = signature(path)
                old = cached.get(name, {})
                if old.get("state") == "sampled" and old.get("signature") == stamp:
                    result = {"max_display_scale": old["max_display_scale"]}
                else:
                    result = sample(path, 5)
                if signature(path) != stamp:
                    raise ValueError("source_changed_during_copy")
                data["results"][str(i)] = {"state": "sampled", "scale": result["max_display_scale"]}
            except Exception as e:
                data["results"][str(i)] = {"state": "failed", "error_type": type(e).__name__}
            write(state, data)
            write(
                job / "status/status.json",
                {
                    "state": "sampling",
                    "completed": len(data["results"]),
                    "total": len(data["chosen"]),
                },
            )
        values = [v["scale"] for v in data["results"].values() if v["state"] == "sampled"]
        threshold = cutoff(values)
        selected = sum(v > threshold for v in values) if values else 0
        report = {
            "state": "calibrated" if values else "failed",
            "population_archives": data["population"],
            "random_sample_size": len(data["chosen"]),
            "evaluated": len(values),
            "failed": len(data["chosen"]) - len(values),
            "old_selected": sum(v > 1.10 for v in values),
            "old_percent": round(100 * sum(v > 1.10 for v in values) / len(values), 2)
            if values
            else None,
            "threshold": threshold,
            "selected": selected,
            "needs_upscale_percent": round(100 * selected / len(values), 2) if values else None,
            "cap_fraction": "2/3",
            "samples_per_archive": 5,
        }
        write(job / "status/calibration.json", report)
        write(job / "status/status.json", report)
        return report


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--size", type=int, default=60)
    p.add_argument("--reuse")
    a = p.parse_args()
    try:
        if a.size < 3:
            raise ValueError("invalid_sample_count")
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            report = calibrate(a.input, a.job, a.size, a.reuse)
        print(__import__("json").dumps(report))
        sys.exit(0 if report["threshold"] else 1)
    except Exception as e:
        report = error_report(e)
        try:
            write(Path(a.job) / "status/status.json", report)
        except OSError:
            pass
        print(__import__("json").dumps(report))
        sys.exit(1)


if __name__ == "__main__":
    main()
