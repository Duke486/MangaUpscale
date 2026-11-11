"""Local-only quality conversion with verified atomic publication and recovery."""

from pathlib import Path, PurePosixPath
import hashlib, io, os, time, uuid, zipfile
from PIL import Image
from pipeline import read, write, sha, open_generated, IMAGES, error_report
from storage import target_for, job_lock
from control import check, Stopped
from path_identity import batch_output_root, saved_locations


def digest(data):
    return hashlib.sha256(data).hexdigest()


def open_existing(data):
    previous = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = 512_000_000
        im = Image.open(io.BytesIO(data))
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    if im.width * im.height > 512_000_000:
        im.close()
        raise ValueError("repack_image_too_large")
    return im


def encode(im, quality=95):
    buf = io.BytesIO()
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    im.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def cache_for(child, aid, name, data, size, provenance):
    entry = provenance.get(name, {})
    if entry.get("encoded_sha256") == digest(data) and entry.get("cache"):
        candidate = (child / entry["cache"]).resolve()
        if (
            (child / "processed").resolve() in candidate.parents
            and candidate.is_file()
            and sha(candidate) == entry.get("cache_sha256")
        ):
            return candidate
    # Legacy outputs did not record per-page provenance. Accept a cached page only
    # when re-encoding reproduces the exact current JPEG bytes (95 or old 98).
    mapping = (
        read(child / "state/map.json") if (child / "state/map.json").exists() else {"archives": []}
    )
    item = next((v for v in mapping["archives"] if v["id"] == aid), None)
    if item:
        used = {m["original"].casefold() for m in item["members"] if not m["image"]}
        for member in item["members"]:
            if not member["image"]:
                continue
            output = str(PurePosixPath(member["original"]).with_suffix(".jpg"))
            if output.casefold() in used:
                output = member["original"] + ".jpg"
            used.add(output.casefold())
            if output != name:
                continue
            base = child / "processed" / aid
            for candidate in base.rglob(str(member["order"]) + ".png") if base.exists() else []:
                check()
                stamp = candidate.with_suffix(".json")
                if not stamp.exists() or sha(candidate) != read(stamp).get("sha256"):
                    continue
                try:
                    with open_generated(candidate, size) as im:
                        im.load()
                        if any(digest(encode(im, q)) == digest(data) for q in (98, 95)):
                            return candidate
                except (ValueError, OSError):
                    continue
            break
    return None


def replace_verified(partial, target):
    for attempt in range(12):
        try:
            os.replace(partial, target)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.15))


def finish_receipt(child, aid, marker, receipt):
    original = child / "status" / ("receipt-" + aid + ".json")
    if original.exists():
        old = read(original)
        old.update(
            {
                "sha256": receipt["sha256"],
                "bytes": receipt["bytes"],
                "jpeg_quality": 95,
                "repacked": True,
            }
        )
        write(original, old)
    write(marker, receipt)


def repack_one(child, aid, target):
    child = Path(child).resolve()
    target = Path(target)
    marker = child / "status" / ("repack95-" + aid + ".json")
    pending = child / "state" / ("repack95-" + aid + ".json")
    if not target.is_file():
        return {"state": "missing"}
    if target.is_symlink() or getattr(target.lstat(), "st_file_attributes", 0) & 1024:
        raise ValueError("output_is_link")
    before = sha(target)
    if pending.exists():
        transaction = read(pending)
        if transaction["receipt"]["sha256"] == before:
            write(child / "state" / ("encoded-pages-" + aid + ".json"), transaction["provenance"])
            finish_receipt(child, aid, marker, transaction["receipt"])
            pending.unlink()
            return {"state": "repack95-skipped"}
    if marker.exists() and read(marker).get("sha256") == before:
        return {"state": "repack95-skipped"}
    provenance_path = child / "state" / ("encoded-pages-" + aid + ".json")
    provenance = read(provenance_path) if provenance_path.exists() else {}
    partial = target.with_name(target.name + ".q95-" + uuid.uuid4().hex + ".partial")
    expected = []
    hashes = {}
    sizes = {}
    cache_pages = 0
    jpeg_pages = 0
    new_provenance = {}
    with (
        zipfile.ZipFile(target) as source,
        zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as out,
    ):
        entries = source.infolist()
        if len({e.filename for e in entries}) != len(entries):
            raise ValueError("duplicate_zip_names")
        reserved = {
            e.filename.casefold()
            for e in entries
            if e.is_dir() or PurePosixPath(e.filename).suffix.lower() not in IMAGES
        }
        total = sum(
            not e.is_dir() and PurePosixPath(e.filename).suffix.lower() in IMAGES for e in entries
        )
        for entry in entries:
            check()
            data = source.read(entry)
            name = entry.filename
            if not entry.is_dir() and PurePosixPath(name).suffix.lower() in IMAGES:
                with open_existing(data) as current:
                    size = current.size
                    current.load()
                    cache = cache_for(child, aid, name, data, size, provenance)
                    if cache:
                        with open_generated(cache, size) as pixels:
                            pixels.load()
                            encoded = encode(pixels)
                        cache_pages += 1
                    else:
                        encoded = encode(current)
                name = str(PurePosixPath(name).with_suffix(".jpg"))
                if name.casefold() in reserved:
                    name = entry.filename + ".jpg"
                if name.casefold() in reserved:
                    raise ValueError("output_name_collision")
                reserved.add(name.casefold())
                data = encoded
                sizes[name] = size
                jpeg_pages += 1
                new_provenance[name] = {
                    "encoded_sha256": digest(data),
                    "size": size,
                    "cache": str(cache.relative_to(child)) if cache else None,
                    "cache_sha256": sha(cache) if cache else None,
                    "preserved_original": False,
                }
                write(
                    child / "status/status.json",
                    {"state": "repack95-encoding", "completed": jpeg_pages, "total": total},
                )
            out.writestr(name, data)
            expected.append(name)
            hashes[name] = digest(data)
    if not jpeg_pages:
        raise ValueError("archive_has_no_pages")
    write(
        child / "status/status.json",
        {"state": "repack95-verifying", "completed": jpeg_pages, "total": jpeg_pages},
    )
    with zipfile.ZipFile(partial) as verified:
        if verified.namelist() != expected or verified.testzip() is not None:
            raise ValueError("archive_verification_failed")
        for name in expected:
            check()
            data = verified.read(name)
            if digest(data) != hashes[name]:
                raise ValueError("packed_page_changed")
            if name in sizes:
                with open_generated(io.BytesIO(data), sizes[name]) as im:
                    im.load()
                    if im.format != "JPEG":
                        raise ValueError("jpeg_verification_failed")
    if sha(target) != before:
        raise ValueError("target_changed_during_repack")
    receipt = {
        "state": "complete",
        "sha256": sha(partial),
        "bytes": partial.stat().st_size,
        "previous_bytes": target.stat().st_size,
        "quality": 95,
        "subsampling": 0,
        "pages": jpeg_pages,
        "lossless_cache_pages": cache_pages,
        "crc": "passed",
    }
    write(pending, {"receipt": receipt, "provenance": new_provenance})
    check()
    replace_verified(partial, target)
    write(provenance_path, new_provenance)
    finish_receipt(child, aid, marker, receipt)
    pending.unlink()
    return receipt


def repack_task(job, source, output):
    job = Path(job)
    manifest = job / "state/demand.json"
    if not manifest.exists():
        raise ValueError("repack_requires_existing_job")
    saved = read(manifest)
    norm = lambda p: os.path.normcase(os.path.abspath(p))
    source, output = saved_locations(saved["config"], source, output)
    root = batch_output_root(source, output)
    results = []
    seen = set()
    for key, item in saved["items"].items():
        if not item.get("needs_upscale"):
            continue
        check()
        child = job / "jobs" / key
        candidates = []
        if (child / "state/map.json").exists() and (child / "plan.json").exists():
            plan = read(child / "plan.json")
            candidates = [
                (v["id"], target_for(v, plan)) for v in read(child / "state/map.json")["archives"]
            ]
        else:
            relative = Path(item["source"]).relative_to(Path(saved["config"]["source"]))
            candidates = [("b" + key + ".cbz", root / relative.with_suffix(".cbz"))]
        for aid, target in candidates:
            if not target.is_file():
                continue
            if root not in target.resolve().parents:
                raise ValueError("output_outside_task")
            if norm(target) in seen:
                continue
            seen.add(norm(target))
            os.environ["MANGA_CHILD_KEY"] = key
            write(job / "status/active-task.json", {"task": key, "updated_epoch": time.time()})
            try:
                with job_lock(child):
                    result = repack_one(child, aid, target)
                write(
                    child / "status/status.json",
                    {
                        "state": "repack95-complete"
                        if result["state"] == "complete"
                        else result["state"]
                    },
                )
                results.append({"task": key, "state": result["state"]})
            except Stopped:
                raise
            except Exception as e:
                write(child / "status/status.json", error_report(e))
                results.append({"task": key, "state": "failed", "error_type": type(e).__name__})
    report = {
        "completed": sum(v["state"] == "complete" for v in results),
        "skipped": sum(v["state"] == "repack95-skipped" for v in results),
        "failed": sum(v["state"] == "failed" for v in results),
        "results": results,
    }
    write(job / "status/repack95-summary.json", report)
    return report
