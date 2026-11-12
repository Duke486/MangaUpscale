"""Reuse the saved selection, skip existing outputs, rebuild only missing books."""

from pathlib import Path
import os
from pipeline import read, write, scan, sha
from storage import target_for
from control import check
from path_identity import batch_output_root, saved_locations


def rules_for(mode):
    color = {
        "color": True,
        "engine": "tensorrt",
        "model": "illustration-detail-fdat-m-2x",
        "tile": 256,
        "padding": 64,
    }
    if mode == "color":
        return [color, {**color, "color": False}]
    if mode == "flat":
        return [
            color,
            {
                "color": False,
                "engine": "waifu2x",
                "model": "flat-art-denoise2",
                "tile": 256,
                "batch_size": 1,
                "tta": False,
                "noise_level": 2,
            },
        ]
    heights = [1200, 1300, 1400, 1500, 1600, 1920, 2048]
    return [color] + [
        {
            "color": False,
            "engine": "spandrel",
            "height_min": 0 if i == 0 else (heights[i - 1] + h) // 2,
            "height_max": 999999 if i == len(heights) - 1 else (h + heights[i + 1]) // 2 - 1,
            "model": "manga-" + str(h),
            "tile": 384,
            "padding": 96,
        }
        for i, h in enumerate(heights)
    ]


def output_paths(job, source, root, selected, conflicts):
    path = job / "state/output-paths.json"
    assigned = read(path) if path.exists() else {}
    selected_keys = {key for key, _ in selected}
    assigned = {
        key: name
        for key, name in assigned.items()
        if key in selected_keys or (root / name).exists()
    }
    occupied = {name.casefold() for name in assigned.values()}
    originals = {
        key: Path(item["source"]).relative_to(source).with_suffix(".cbz") for key, item in selected
    }
    reserved = {str(name).casefold() for name in originals.values()}
    targets = {}
    # A pre-upgrade job may already own the unnumbered output.
    ordered = sorted(
        selected,
        key=lambda pair: (
            not any((job / "jobs" / pair[0] / "status").glob("receipt-*.json")),
            pair[1]["source"].casefold(),
        ),
    )
    for key, _ in ordered:
        name = originals[key]
        if key in assigned:
            targets[key] = root / assigned[key]
            continue
        candidate = name
        if str(candidate).casefold() in occupied:
            if conflicts is None:
                raise ValueError("output_name_collision")
            if conflicts == "skip":
                continue
            number = 1
            while True:
                candidate = name.with_name(f"{name.stem}({number}).cbz")
                folded = str(candidate).casefold()
                if (
                    folded not in occupied
                    and folded not in reserved
                    and not (root / candidate).exists()
                ):
                    break
                number += 1
        assigned[key] = str(candidate)
        occupied.add(str(candidate).casefold())
        targets[key] = root / candidate
    for target in targets.values():
        resolved = target.resolve()
        if resolved == source or source in resolved.parents:
            raise ValueError("output_overlaps_input")
    write(path, assigned)
    return targets


def prepare_saved(
    job,
    source,
    output,
    mode,
    skip_above_recommended=True,
    page_height=None,
    conflicts=None,
    book_keys=None,
):
    source = Path(source).absolute()
    output = Path(output).absolute()
    job = Path(job)
    cp = job / "state/demand.json"
    summary = job / "status/demand-summary.json"
    if not cp.exists() or not summary.exists() or not read(summary).get("scan_complete"):
        return False
    data = read(cp)
    config = data["config"]
    source, output = saved_locations(config, source, output)
    from default_mode import task_default

    default_mode = task_default(job, mode)
    selected = [(key, item) for key, item in data["items"].items() if item.get("needs_upscale")]
    root = batch_output_root(source, output)
    targets = output_paths(job, source, root, selected, conflicts)
    if book_keys is not None:
        keys = set(book_keys)
        if not keys <= {key for key, _ in selected}:
            raise ValueError("invalid_book_selection")
        selected = [(key, item) for key, item in selected if key in keys]
        targets = {key: path for key, path in targets.items() if key in keys}
    active = set()
    for index, (key, item) in enumerate(selected, 1):
        if key not in targets:
            continue
        check()
        child = job / "jobs" / key
        from book_actions import choice, apply

        expected = targets[key]
        if (child / "plan.json").is_file():
            active.add(key)
        decision = choice(child)
        if decision.get("cleanup_pending"):
            apply(child, decision)
        if decision.get("action") == "skip":
            continue
        effective_mode = (
            decision.get("mode") if decision.get("action") == "restart" else default_mode
        )
        os.environ["MANGA_CHILD_KEY"] = key
        write(
            job / "status/active-task.json",
            {
                "task": key,
                "index": index,
                "total": len(selected),
                "updated_epoch": __import__("time").time(),
            },
        )
        if expected.is_file() and not (child / "state/map.json").exists():
            write(child / "status/status.json", {"state": "skipped-existing"})
            continue
        ready = False
        if (child / "state/map.json").exists() and (child / "plan.json").exists():
            mapping = read(child / "state/map.json")
            plan = read(child / "plan.json")
            if (
                all(
                    target_for(v, plan) == expected and expected.is_file()
                    for v in mapping["archives"]
                )
                and not (child / "status/rebuild-bw.json").exists()
            ):
                continue
            ready = all(
                (child / "staged" / v["id"]).exists()
                and sha(child / "staged" / v["id"]) == v["stage_sha256"]
                for v in mapping["archives"]
            )
        # Reuses snapshots and checkpoints; old versions may have removed completed caches.
        if not ready:
            scan(item["source"], child, output_root=output)
        mapping = read(child / "state/map.json")
        inventory = read(child / "status/inventory.json")
        for record in mapping["archives"]:
            record["output_base"] = str(expected.with_suffix(""))
        write(child / "state/map.json", mapping)
        plan = {
            "scale": 4,
            "color_scale": 2,
            "output_suffix": "",
            "output_format": "jpeg",
            "page_threshold": config["threshold"],
            "bw_mode": effective_mode,
            "skip_above_recommended": skip_above_recommended,
            "height_metric": config.get("height_metric", "fit-page"),
            "archives": {
                v["id"]: {"rules": rules_for(effective_mode)} for v in inventory["archives"]
            },
        }
        if page_height is not None:
            plan["page_height"] = page_height
        write(child / "plan.json", plan)
        for record in mapping["archives"]:
            checkpoint = child / "state/checkpoints" / (record["id"] + ".json")
            saved = read(checkpoint)
            saved["state"] = record
            write(checkpoint, saved)
        active.add(key)
    write(job / "state/active-books.json", sorted(active))
    write(
        job / "status/status.json",
        {
            "state": "prepared-reused",
            "jobs": len(targets),
            "name_conflicts_skipped": len(selected) - len(targets),
        },
    )
    return True


def existing_outputs(child):
    child = Path(child)
    if (child / "status/rebuild-bw.json").exists():
        return False
    if not (child / "plan.json").exists() or not (child / "state/map.json").exists():
        return False
    plan = read(child / "plan.json")
    items = read(child / "state/map.json")["archives"]
    return bool(items) and all(target_for(v, plan).is_file() for v in items)
