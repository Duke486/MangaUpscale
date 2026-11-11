"""Image transport and navigation for the desktop preview."""

import argparse, base64, io, json, os, re, sys, zipfile
from pathlib import Path
from pipeline import read, write, open_generated


def catalog(job):
    from book_actions import choice
    from mode_jobs import existing_outputs

    selection = Path(job) / "state/active-books.json"
    active = set(read(selection)) if selection.is_file() else None
    result = []
    for child in sorted((Path(job) / "jobs").iterdir()) if (Path(job) / "jobs").exists() else []:
        if not child.is_dir() or not re.fullmatch("[a-f0-9]{24}", child.name):
            continue
        if active is not None and child.name not in active:
            continue
        title = child.name[:8]
        mode = choice(child).get("mode")
        try:
            mapping = read(child / "state/map.json")
            title = Path(mapping["archives"][0]["source"]).stem
        except (OSError, ValueError, KeyError, IndexError):
            pass
        try:
            mode = mode or read(child / "plan.json").get("bw_mode", "manga")
        except (OSError, ValueError):
            mode = mode or "manga"
        result.append(
            {"task": child.name, "title": title, "mode": mode, "completed": existing_outputs(child)}
        )
    return result


def navigation(job, target, selection=None):
    books = catalog(job)
    keys = [book["task"] for book in books]
    if target not in keys:
        raise ValueError("invalid_target")
    index = keys.index(target)
    unfinished = [book["task"] for book in books if not book["completed"]]
    first = unfinished[0] if unfinished else None
    following = next((book["task"] for book in books[index + 1 :] if not book["completed"]), None)
    if selection in ("first", "next"):
        destination = first if selection == "first" else following
        if destination is None:
            return {
                "sent": True,
                "action": "preview-completed",
                "target": target,
                "books": books,
                "no_unfinished": True,
            }
        return {"sent": True, "action": "navigate-ready", "target": destination, "books": books}
    return {
        "sent": True,
        "action": "navigate-completed" if books[index]["completed"] else "navigate-ready",
        "target": target,
        "books": books,
        "first_unfinished": first,
        "next_unfinished": following,
    }


def entries_for(child):
    from pipeline import choose, output_size
    from storage import target_for

    try:
        entries = read(child / "state/preview.json")
        if entries:
            return entries
    except (OSError, ValueError):
        pass
    # Completed outputs remain browsable after page caches are cleaned.
    try:
        mapping = read(child / "state/map.json")
        plan = read(child / "plan.json")
        inventory = read(child / "status/inventory.json")
    except (OSError, ValueError):
        return []
    result = []
    for archive in mapping["archives"]:
        aid = archive["id"]
        pages = {p["id"]: p for a in inventory["archives"] if a["id"] == aid for p in a["pages"]}
        target = target_for(archive, plan)
        names = {m["original"].casefold() for m in archive["members"] if not m["image"]}
        for member in archive["members"]:
            if not member["image"]:
                continue
            page = pages[member["id"]]
            name = str(Path(member["original"]).with_suffix(".jpg")).replace("\\", "/")
            if name.casefold() in names:
                name = member["original"] + ".jpg"
            names.add(name.casefold())
            candidates = []
            base = child / "processed" / aid
            if base.exists():
                for pattern in (str(member["order"]) + ".jpg", str(member["order"]) + ".png"):
                    candidates.extend(p for p in base.rglob(pattern) if p.is_file())
            if candidates:
                cache = max(candidates, key=lambda p: p.stat().st_mtime_ns)
                from PIL import Image

                with Image.open(cache) as im:
                    size = im.size
                result.append(
                    {
                        "archive": aid,
                        "member": member["id"],
                        "order": member["order"],
                        "cache": str(cache.relative_to(child)),
                        "size": size,
                    }
                )
            elif target.is_file() and not (child / "status/rebuild-bw.json").exists():
                result.append(
                    {
                        "archive": aid,
                        "member": member["id"],
                        "order": member["order"],
                        "cache": None,
                        "size": output_size(page, plan),
                        "final": str(target),
                        "output_member": name,
                    }
                )
    return result


def preview_order(child):
    entries = entries_for(child)
    if any("color" not in entry for entry in entries):
        try:
            inventory = read(child / "status/inventory.json")
            colors = {
                (a["id"], page["id"]): page["color"]
                for a in inventory["archives"]
                for page in a["pages"]
            }
        except (OSError, ValueError):
            colors = {}
        entries = [
            {
                **entry,
                "color": entry.get("color", colors.get((entry["archive"], entry["member"]), True)),
            }
            for entry in entries
        ]
    return sorted(
        entries, key=lambda entry: (bool(entry["color"]), entry["archive"], entry["order"])
    )


def load(job, index, known=None, book=None, follow=False, since=0):
    job = Path(job)
    books = catalog(job)
    try:
        live = read(job / "status/active-task.json")
    except (OSError, ValueError):
        live = {}
    keys = [b["task"] for b in books]
    if follow:
        key = (
            live.get("task")
            if live.get("updated_epoch", 0) >= since and live.get("task") in keys
            else ""
        )
        if key and book is not None and book != key:
            index = -1
    else:
        key = (
            book
            if book in keys
            else (live.get("task") if live.get("task") in keys else (keys[0] if keys else ""))
        )
    result = {"active": {"task": key}, "live": live, "count": 0, "books": books, "book": key}
    try:
        result["switch_result"] = read(job / "status/switch-result.json")
    except (OSError, ValueError):
        pass
    if not key:
        return result
    child = job / "jobs" / key
    try:
        entries = preview_order(child)
        result["count"] = len(entries)
        if not entries:
            return result
        index = 0 if index < 0 else min(index, len(entries) - 1)
        entry = entries[index]
        result["index"] = index
        page_signature = [key, None, index, entry["cache"], entry["size"]]
        signature = page_signature
        result["signature"] = signature
        if signature == known:
            return result
        stage = child / "staged" / entry["archive"]
        if stage.resolve().parent != (child / "staged").resolve():
            raise ValueError()
        with zipfile.ZipFile(stage) as z:
            original = z.read(entry["member"])
        cache = entry["cache"]
        if entry.get("final"):
            with zipfile.ZipFile(entry["final"]) as z:
                processed = z.read(entry["output_member"])
        elif cache:
            cache = (child / cache).resolve()
            if (child / "processed").resolve() not in cache.parents:
                raise ValueError()
            processed = cache.read_bytes()
        else:
            processed = original
        # Transfer the original encoded bytes once per page; no viewport-dependent reload.
        result["original"] = base64.b64encode(original).decode("ascii")
        result["processed"] = base64.b64encode(processed).decode("ascii")
        result["native"] = True
        return result
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        result["pending"] = True
        return result


def main():
    from parent_guard import start

    start()
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    parser.add_argument("--index", type=int, default=-1)
    parser.add_argument("--known")
    parser.add_argument("--book")
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--since", type=float, default=0)
    parser.add_argument("--command")
    args = parser.parse_args()
    try:
        if args.command:
            request = json.loads(args.command)
            keys = {b["task"] for b in catalog(Path(args.job))}
            if request.get("action") == "navigate":
                result = navigation(Path(args.job), request.get("target"), request.get("selection"))
            elif request.get("action") in ("switch", "restart-bw"):
                if request.get("target") not in keys:
                    raise ValueError("invalid_target")
                if request["action"] == "restart-bw" and request.get("mode") not in (
                    "flat",
                    "manga",
                    "color",
                ):
                    raise ValueError("invalid_mode")
                write(Path(args.job) / "status/switch-request.json", request)
                result = {
                    "sent": True,
                    "action": request["action"],
                    "target": request["target"],
                    "mode": request.get("mode"),
                }
            else:
                if request.get("task") not in keys or request.get("mode") not in (
                    "flat",
                    "manga",
                    "color",
                ):
                    raise ValueError("invalid_preference")
                write(
                    Path(args.job) / "jobs" / request["task"] / "status/book-preference.json",
                    {"mode": request["mode"]},
                )
                result = {"sent": True, "task": request["task"], "mode": request["mode"]}
        else:
            result = load(
                args.job,
                args.index,
                json.loads(args.known) if args.known else None,
                args.book,
                args.follow,
                args.since,
            )
        print(json.dumps(result))
    except Exception:
        print(json.dumps({"error": "preview_unavailable"}))


if __name__ == "__main__":
    main()
