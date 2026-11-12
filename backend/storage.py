"""Local snapshots and durable scan checkpoints."""

from pathlib import Path
from contextlib import contextmanager
import errno, hashlib, os, shutil, time, zipfile
from control import check
from path_identity import same_location
from archive_pages import ARCHIVES, archive_entries


class JobBusy(OSError):
    """Another process owns the task lock."""


@contextmanager
def job_lock(job):
    path = Path(job) / "state/session.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not append to or write a byte locked by another process. Windows
    # byte-range locking also supports an initially empty file.
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b") as f:
        f.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if (
                error.errno in (errno.EACCES, errno.EAGAIN)
                or getattr(error, "winerror", None) == 33
            ):
                raise JobBusy("job_busy") from error
            raise
        try:
            f.write(b"0")
            f.flush()
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def signature(path):
    s = path.stat()
    return {"size": s.st_size, "mtime_ns": s.st_mtime_ns}


def snapshot(source, dest, job):
    from pipeline import read, write, sha

    state = dest.with_suffix(".transfer.json")
    partial = dest.with_suffix(".download.partial")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(4):
        try:
            check()
            write(job / "status/status.json", {"state": "checking-source", "pid": os.getpid()})
            before = signature(source)
            old = read(state) if state.exists() else {}
            if old.get("source") == before and dest.exists() and old.get("sha256") == sha(dest):
                write(
                    job / "status/status.json",
                    {
                        "state": "download-cache-verified",
                        "bytes": dest.stat().st_size,
                        "total_bytes": before["size"],
                        "pid": os.getpid(),
                    },
                )
                return old["sha256"]
            if old.get("source") != before:
                partial.unlink(missing_ok=True)
            write(state, {"source": before})
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > before["size"]:
                partial.unlink()
                offset = 0
            if shutil.disk_usage(dest.parent).free < before["size"] - offset + 64 * 1024**2:
                raise ValueError("insufficient_disk_space")
            # Verify the saved prefix before appending; size/mtime alone are not identity.
            digest_builder = hashlib.sha256()
            with source.open("rb") as src:
                if offset:
                    write(
                        job / "status/status.json",
                        {
                            "state": "checking-prefix",
                            "bytes": 0,
                            "total_bytes": offset,
                            "pid": os.getpid(),
                        },
                    )
                    with partial.open("rb") as cached:
                        while True:
                            check()
                            block = cached.read(4 * 1024**2)
                            if not block:
                                break
                            if src.read(len(block)) != block:
                                raise ValueError("source_prefix_changed")
                            digest_builder.update(block)
                with partial.open("ab") as dst:
                    while True:
                        check()
                        block = src.read(4 * 1024**2)
                        if not block:
                            break
                        dst.write(block)
                        dst.flush()
                        digest_builder.update(block)
                        write(
                            job / "status/status.json",
                            {
                                "state": "copying",
                                "bytes": dst.tell(),
                                "total_bytes": before["size"],
                                "attempt": attempt + 1,
                                "pid": os.getpid(),
                            },
                        )
                    os.fsync(dst.fileno())
            write(job / "status/status.json", {"state": "checking-source", "pid": os.getpid()})
            if signature(source) != before or partial.stat().st_size != before["size"]:
                raise ValueError("source_changed_during_copy")
            write(job / "status/status.json", {"state": "hashing-local", "pid": os.getpid()})
            digest = digest_builder.hexdigest()
            partial.replace(dest)
            write(state, {"source": before, "sha256": digest})
            write(
                job / "status/status.json",
                {
                    "state": "download-verified",
                    "bytes": dest.stat().st_size,
                    "total_bytes": before["size"],
                    "pid": os.getpid(),
                },
            )
            return digest
        except OSError:
            if attempt == 3:
                raise
            write(
                job / "status/status.json",
                {"state": "retrying_source", "attempt": attempt + 1, "pid": os.getpid()},
            )
            time.sleep(2**attempt)


def default_job(source, cache_root, recursive=False, only_name=None):
    key = str(Path(source).absolute()).casefold() + "|" + str(recursive) + "|" + str(only_name)
    return Path(cache_root) / ("job-" + hashlib.sha256(key.encode()).hexdigest()[:20])


def target_for(item, plan):
    from pathlib import PurePosixPath
    import re

    suffix = plan.get("output_suffix", "-JaNai4X")
    if suffix and not re.fullmatch(r"-[A-Za-z0-9_-]+", suffix):
        raise ValueError("invalid_output_suffix")
    if item.get("output_base"):
        base = Path(item["output_base"])
        return base.with_name(base.name + suffix + ".cbz")
    source = Path(item["source"])
    return source.with_name(source.stem + suffix + ".cbz")


def refresh_colors(job, inventory):
    """Upgrade cached classifications under the caller's task lock, using local snapshots."""
    from color_detection import VERSION, classify
    from PIL import Image
    from pipeline import read, write, sha

    job = Path(job)
    refreshed = False
    for archive in inventory["archives"]:
        stale = [p for p in archive["pages"] if p.get("color_version") != VERSION]
        if not stale:
            continue
        checkpoint = job / "state/checkpoints" / (archive["id"] + ".json")
        saved = read(checkpoint)
        stage = job / "staged" / archive["id"]
        if sha(stage) != saved["state"]["stage_sha256"]:
            raise ValueError("source_or_stage_changed")
        updated = []
        with zipfile.ZipFile(stage) as source:
            for page in stale:
                check()
                with source.open(page["id"]) as stream, Image.open(stream) as image:
                    updated.append((page, classify(image)))
        if any(page["color"] != result["color"] for page, result in updated):
            plan_path = job / "plan.json"
            marker = job / "status/rebuild-bw.json"
            if plan_path.is_file() and not marker.exists():
                plan = read(plan_path)
                if plan.get("bw_mode") != "color":
                    write(
                        marker,
                        {
                            "mode": plan.get("bw_mode", "manga"),
                            "cleanup_pending": False,
                            "origin": "color-detection",
                        },
                    )
            (job / "state/preview.json").unlink(missing_ok=True)
        for page, result in updated:
            page.update(result)
        saved["status"] = archive
        write(checkpoint, saved)
        refreshed = True
    return refreshed


def scan_local(source, job, recursive=False, only_name=None, output_root="./output"):
    from pipeline import read, write, sha, validate_member, natural, metadata, IMAGES
    from color_detection import VERSION

    source = Path(source).resolve()
    job = Path(job).resolve()
    output_root = Path(output_root).resolve()
    if not source.exists():
        raise ValueError("input_unavailable")
    base = source.parent if source.is_file() else source
    if job == base or base in job.parents or output_root == base or base in output_root.parents:
        raise ValueError("local_paths_inside_input")
    with job_lock(job):
        config = {
            "source": str(source),
            "recursive": recursive,
            "only_name": only_name,
            "output_root": str(output_root),
        }
        config_path = job / "state/input.json"
        if config_path.exists():
            previous = read(config_path)
            if (
                previous.get("recursive") != recursive
                or previous.get("only_name") != only_name
                or not same_location(previous["source"], source)
                or not same_location(previous["output_root"], output_root)
            ):
                raise ValueError("job_input_changed")
        write(config_path, config)
        journal_path = job / "state/scan.json"
        journal = read(journal_path) if journal_path.exists() else {"items": []}
        if not journal.get("enumerated"):
            if source.is_file():
                files = [source]
            elif only_name:
                if Path(only_name).name != only_name or "/" in only_name or "\\" in only_name:
                    raise ValueError("invalid_only_name")
                files = [source / only_name]
            else:
                files = sorted(
                    (source.rglob("*") if recursive else source.iterdir()),
                    key=lambda p: natural(str(p)),
                )
            archives = []
            folders = {}
            for p in files:
                if p.is_symlink() or not p.is_file():
                    continue
                if p.suffix.lower() in ARCHIVES and not p.stem.endswith("-JaNai4X"):
                    archives.append(p)
                elif p.suffix.lower() in IMAGES:
                    folders.setdefault(p.parent, []).append(p)
            items = []
            used = set()
            used_ids = set()
            work_name = source.stem if source.is_file() else source.name
            for kind, path, members in [("archive", p, []) for p in archives] + [
                ("folder", p, v) for p, v in folders.items()
            ]:
                relative = path.relative_to(base)
                relbase = relative.with_suffix("") if kind == "archive" else relative / path.name
                output = output_root / work_name / relbase
                if str(output).casefold() in used:
                    output = output.with_name(
                        output.name + "-" + hashlib.sha256(str(relative).encode()).hexdigest()[:8]
                    )
                used.add(str(output).casefold())
                archive_id = path.name
                if archive_id.casefold() in used_ids:
                    archive_id = (
                        path.stem
                        + "-"
                        + hashlib.sha256(str(relative).encode()).hexdigest()[:8]
                        + path.suffix
                    )
                used_ids.add(archive_id.casefold())
                items.append(
                    {
                        "id": archive_id,
                        "kind": kind,
                        "source": str(path),
                        "files": [str(v) for v in members],
                        "output_base": str(output),
                    }
                )
            if not items:
                raise ValueError("no_supported_pages_or_archives")
            journal = {"enumerated": True, "items": items}
            write(journal_path, journal)
        state = []
        status = []
        for item in journal["items"]:
            check()
            aid = item["id"]
            checkpoint = job / "state/checkpoints" / (aid + ".json")
            stage = job / "staged" / aid
            if checkpoint.exists():
                saved = read(checkpoint)
                receipt_path = job / "status" / ("receipt-" + aid + ".json")
                plan_path = job / "plan.json"
                completed = False
                if receipt_path.exists() and plan_path.exists():
                    target = target_for(saved["state"], read(plan_path))
                    receipt = read(receipt_path)
                    completed = (
                        receipt.get("state") == "complete"
                        and target.exists()
                        and sha(target) == receipt.get("sha256")
                    )
                current = all(p.get("color_version") == VERSION for p in saved["status"]["pages"])
                if (completed and current) or (
                    stage.exists() and sha(stage) == saved["state"]["stage_sha256"]
                ):
                    refresh_colors(job, {"archives": [saved["status"]]})
                    write(
                        job / "status/status.json",
                        {
                            "state": "cache-verified",
                            "pages": saved["status"]["page_count"],
                            "pid": os.getpid(),
                        },
                    )
                    state.append(saved["state"])
                    status.append(saved["status"])
                    continue
            raw = job / "downloads" / aid
            if item["kind"] == "archive":
                digest = snapshot(Path(item["source"]), raw, job)
            else:
                raw.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as z:
                    for index, name in enumerate(item["files"]):
                        p = Path(name)
                        cached = job / "downloads" / (aid + "-" + str(index))
                        snapshot(p, cached, job)
                        z.write(cached, p.name)
                digest = sha(raw)
            write(
                job / "status/status.json",
                {"state": "analysing-pages", "completed": 0, "pid": os.getpid()},
            )
            analysis_path = job / "state/analysis" / (aid + ".json")
            analysis = read(analysis_path) if analysis_path.exists() else {}
            if analysis.get("snapshot_sha256") != digest:
                analysis = {"snapshot_sha256": digest, "entries": {}}
            if checkpoint.exists() and not analysis["entries"]:
                old_checkpoint = read(checkpoint)
                if old_checkpoint["state"].get("source_sha256") == digest:
                    old_pages = {p["id"]: p for p in old_checkpoint["status"]["pages"]}
                    for member in old_checkpoint["state"]["members"]:
                        cached = {"token": member["id"]}
                        if member["image"]:
                            cached["metadata"] = {
                                k: v
                                for k, v in old_pages[member["id"]].items()
                                if k not in ("id", "order")
                            }
                        analysis["entries"][str(member["order"])] = cached
            record = {**item, "source_sha256": digest, "local_snapshot": True, "members": []}
            pages = []
            stage.parent.mkdir(parents=True, exist_ok=True)
            temporary = stage.with_suffix(".staging.partial")
            with (
                zipfile.ZipFile(raw) as src,
                zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as dst,
            ):
                epub = item["kind"] == "archive" and Path(item["source"]).suffix.lower() == ".epub"
                entries = archive_entries(src, epub)
                for order, entry in enumerate(entries, 1):
                    check()
                    write(
                        job / "status/status.json",
                        {
                            "state": "analysing-pages",
                            "completed": order - 1,
                            "total": len(entries),
                            "pid": os.getpid(),
                        },
                    )
                    previous = analysis["entries"].get(str(order), {})
                    # CBZ readers sort filenames; EPUB filenames need not follow its spine.
                    original = (
                        f"{order:06d}{Path(entry.filename).suffix.lower()}"
                        if epub
                        else entry.filename
                    )
                    validate_member(entry.filename)
                    token = validate_member(original)
                    data = src.read(entry)
                    dst.writestr(token, data)
                    is_image = Path(entry.filename).suffix.lower() in IMAGES
                    record["members"].append(
                        {"id": token, "original": original, "image": is_image, "order": order}
                    )
                    if is_image:
                        info = previous.get("metadata")
                        if info is None or info.get("color_version") != VERSION:
                            info = metadata(data)
                        pages.append({"id": token, "order": order, **info})
                        analysis["entries"][str(order)] = {"token": token, "metadata": info}
                    else:
                        analysis["entries"][str(order)] = {"token": token}
                    write(analysis_path, analysis)
                    write(
                        job / "status/status.json",
                        {
                            "state": "analysing-pages",
                            "completed": order,
                            "total": len(entries),
                            "pid": os.getpid(),
                        },
                    )
            if not pages:
                raise ValueError("archive_has_no_pages")
            temporary.replace(stage)
            write(
                job / "status/status.json",
                {
                    "state": "hashing-stage",
                    "completed": len(entries),
                    "total": len(entries),
                    "pid": os.getpid(),
                },
            )
            record["stage_sha256"] = sha(stage)
            summary = {
                "id": aid,
                "source": item["source"],
                "pages": pages,
                "page_count": len(pages),
                "other_files": len(entries) - len(pages),
            }
            write(checkpoint, {"state": record, "status": summary})
            state.append(record)
            status.append(summary)
            write(
                job / "status/status.json",
                {
                    "state": "scanning",
                    "archives_completed": len(status),
                    "archives_total": len(journal["items"]),
                    "pid": os.getpid(),
                },
            )
        write(job / "state/map.json", {"archives": state})
        write(job / "status/inventory.json", {"schema": 2, "archives": status})
        write(
            job / "status/status.json",
            {
                "state": "scanned",
                "archives": len(status),
                "pages": sum(v["page_count"] for v in status),
            },
        )
