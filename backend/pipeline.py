"""Resumable image processing and verified CBZ publication."""

from pathlib import Path, PurePosixPath
import argparse, hashlib, io, json, os, re, shutil, sys, time, uuid, zipfile
from storage import target_for, job_lock
from control import check as check_stop, Stopped
from fileio import atomic_json, read_text_shared
from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from archive_pages import IMAGES


def error_report(error):
    import traceback

    codes = {
        "source_directory_changed",
        "output_directory_changed",
        "output_overlaps_input",
        "output_name_collision",
        "insufficient_disk_space",
        "source_prefix_changed",
        "source_changed_during_copy",
        "input_unavailable",
        "job_input_changed",
        "local_paths_inside_input",
        "no_supported_pages_or_archives",
        "archive_has_no_pages",
        "invalid_epub",
        "unsupported_epub_image",
        "duplicate_zip_names",
        "unsafe_member",
        "source_or_stage_changed",
        "model_changed",
        "target_exists",
        "invalid_archive_selection",
        "another_janai_task_is_running",
        "no_matching_rule",
        "unexpected_blank_page",
    }
    return {
        "state": "stopped" if isinstance(error, Stopped) else "failed",
        "error_type": type(error).__name__,
        "code": str(error) if str(error) in codes else "processing_failed",
        "errno": getattr(error, "errno", None),
        "winerror": getattr(error, "winerror", None),
        "page": getattr(error, "page", None),
        "frames": [
            {"module": Path(f.filename).name, "function": f.name, "line": f.lineno}
            for f in traceback.extract_tb(error.__traceback__)
        ],
        "pid": os.getpid(),
    }


def read(p):
    return json.loads(read_text_shared(p))


def write(p, v):
    if Path(p).name == "status.json":
        v = {**v, "updated_epoch": time.time()}
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(p, v, optional=p.name == "status.json")
    event_file = os.environ.get("MANGA_EVENT_FILE")
    if p.name == "status.json" and event_file:
        safe = {
            k: v[k]
            for k in (
                "state",
                "bytes",
                "total_bytes",
                "completed",
                "total",
                "archives",
                "pages",
                "code",
                "error_type",
                "pid",
                "updated_epoch",
                "model",
                "archive",
                "decode_s",
                "inference_s",
                "save_s",
                "page",
            )
            if k in v
        }
        safe["task"] = os.environ.get("MANGA_CHILD_KEY", "")
        try:
            with Path(event_file).open("a", encoding="utf-8") as log:
                log.write(json.dumps(safe) + "\n")
        except OSError:
            pass


def sha(p):
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda: f.read(1048576), b""):
            check_stop()
            h.update(b)
    return h.hexdigest()


def validate_member(name):
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        raise ValueError("unsafe_member")
    return name


def natural(s):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", s)]


def metadata(data):
    from color_detection import classify
    from PIL import Image
    import numpy as np

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if getattr(im, "n_frames", 1) != 1:
            raise ValueError("animated_image")
        color = classify(im)
        # Scalar-only proxies. No thumbnails, pixel arrays, OCR, EXIF text or spatial maps escape.
        gray = im.convert("L")
        gray.thumbnail((1024, 1024))
        g = np.asarray(gray, dtype=np.float32)
        hist = np.bincount(g.astype(np.uint8).ravel(), minlength=256).astype(float)
        hist /= hist.sum()
        nonzero = hist[hist > 0]
        entropy = float(-(nonzero * np.log2(nonzero)).sum())
        stats = {
            "analysis_width": int(g.shape[1]),
            "analysis_height": int(g.shape[0]),
            "luma_mean": round(float(g.mean()), 3),
            "luma_std": round(float(g.std()), 3),
            "entropy_bits": round(entropy, 4),
            "black_fraction": round(float((g < 16).mean()), 5),
            "white_fraction": round(float((g > 239).mean()), 5),
        }
        if min(g.shape) >= 3:
            center = g[1:-1, 1:-1]
            neighbors = (g[:-2, 1:-1] + g[2:, 1:-1] + g[1:-1, :-2] + g[1:-1, 2:]) / 4
            residual = center - neighbors
            grad = np.maximum(abs(g[1:-1, 2:] - g[1:-1, :-2]), abs(g[2:, 1:-1] - g[:-2, 1:-1])) / 2
            flat = residual[grad < 5]
            noise = (
                float(np.median(abs(flat - np.median(flat))) / 0.67449)
                if flat.size >= 128
                else None
            )
            stats.update(
                {
                    "edge_fraction": round(float((grad > 20).mean()), 5),
                    "laplacian_variance": round(float((residual * 4).var()), 3),
                    "high_frequency_mad": round(
                        float(np.median(abs(residual - np.median(residual)))), 3
                    ),
                    "flat_noise_proxy": round(noise, 3) if noise is not None else None,
                    "flat_sample_fraction": round(float((grad < 5).mean()), 5),
                }
            )
        return {
            "width": w,
            "height": h,
            "mode": im.mode,
            "format": im.format,
            **color,
            "traits": stats,
        }


def scan(source, job, recursive=False, only_name=None, output_root="./output"):
    from storage import scan_local

    return scan_local(source, job, recursive, only_name, output_root)


def choose(page, rules):
    for r in rules:
        if r.get("color", page["color"]) != page["color"]:
            continue
        if (
            not r.get("height_min", 0)
            <= max(page["width"], page["height"])
            <= r.get("height_max", 10**9)
        ):
            continue
        return r
    raise ValueError("no_matching_rule")


def page_scale(page, plan):
    desired = (
        plan.get("color_scale", plan["scale"])
        if page["color"] or plan.get("bw_mode") == "color"
        else plan["scale"]
    )
    if plan.get("skip_above_recommended", False) and max(page["width"], page["height"]) > 2048:
        return 1
    if plan.get("page_height") is not None:
        return desired if max(page["width"], page["height"]) < plan["page_height"] else 1
    threshold = plan.get("page_threshold")
    if threshold is not None:
        w, h = page["width"], page["height"]
        ratio = (
            2752 / max(w, h)
            if plan.get("height_metric") == "long-edge"
            else round(min((2752 if w > h else 2064) / w, (2064 if w > h else 2752) / h), 3)
        )
        if ratio <= threshold:
            return 1
    return desired


def check_blank_result(original, result, order):
    extrema = result.getextrema()
    channels = (extrema,) if isinstance(extrema[0], int) else extrema
    if all(low >= 254 for low, _ in channels) and original.convert("L").getextrema()[0] < 224:
        error = ValueError("unexpected_blank_page")
        error.page = order
        raise error


def output_size(page, plan):
    multiplier = page_scale(page, plan)
    return page["width"] * multiplier, page["height"] * multiplier


def open_generated(path, size):
    """Allow only the expected generated dimensions, never unbound input images."""
    from PIL import Image

    old = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = max(old or 0, size[0] * size[1])
        im = Image.open(path)
    finally:
        Image.MAX_IMAGE_PIXELS = old
    if im.size != size:
        im.close()
        raise ValueError("wrong_output_size")
    return im


def verify_png(path, size):
    with open_generated(path, size) as im:
        if im.format != "PNG":
            raise ValueError("wrong_cache_format")
        im.load()


def encode_original(path, original, size):
    """Preserve exact member names AND truthful format signatures."""
    from PIL import Image

    suffix = PurePosixPath(original).suffix.lower()
    formats = {
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".png": "PNG",
        ".webp": "WEBP",
        ".bmp": "BMP",
        ".tif": "TIFF",
        ".tiff": "TIFF",
    }
    fmt = formats[suffix]
    options = {}
    if fmt == "JPEG":
        options = {"quality": 95, "subsampling": 0}
    elif fmt == "WEBP":
        options = {"lossless": True, "quality": 100, "method": 4}
    elif fmt == "PNG":
        options = {"compress_level": 6}
    elif fmt == "TIFF":
        options = {"compression": "tiff_deflate"}
    data = io.BytesIO()
    with open_generated(path, size) as im:
        im.load()
        if fmt == "JPEG" and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(data, format=fmt, **options)
    return data.getvalue()


def save_jpeg_cache(result, out, fingerprint, size):
    started = time.perf_counter()
    try:
        if result.mode not in ("RGB", "L"):
            result = result.convert("RGB")
        buffer = io.BytesIO()
        result.save(buffer, format="JPEG", quality=95, subsampling=0)
        data = buffer.getvalue()
        with open_generated(io.BytesIO(data), size) as image:
            image.load()
            if image.format != "JPEG":
                raise ValueError("wrong_cache_format")
        temp = out.with_suffix(".jpg.partial")
        temp.write_bytes(data)
        temp.replace(out)
        write(
            out.with_suffix(".jpg.json"),
            {
                "fingerprint": fingerprint,
                "sha256": hashlib.sha256(data).hexdigest(),
                "jpeg_quality": 95,
                "subsampling": 0,
            },
        )
        return round(time.perf_counter() - started, 3)
    finally:
        result.close()


def repack_jpeg(job):
    # Same fingerprints, receipt validation and publication path as normal resume.
    return run(job, Path(job) / "plan.json", Path(__file__).resolve().parents[1] / "runtime.json")


def run(job, planfile, runtimefile, engine=None):
    from PIL import Image

    job = Path(job).resolve()
    from runtime_config import load_runtime

    runtime = load_runtime(runtimefile)
    if engine is None:
        from inference import Engine

        engine = Engine(resource_root=runtime.get("resource_root"))
        engine.emit = lambda event: write(job / "status/status.json", event)
    inv = read(job / "status/inventory.json")
    mapping = read(job / "state/map.json")
    plan = read(planfile)
    scale = plan.get("scale")
    if scale not in (2, 4):
        raise ValueError("scale_must_be_2_or_4")
    selected = plan["archives"]
    total = sum(a["page_count"] for a in inv["archives"] if a["id"] in selected)
    done = 0
    model_hashes = {}
    if not selected or set(selected) - {a["id"] for a in inv["archives"]}:
        raise ValueError("invalid_archive_selection")
    with job_lock(job), ThreadPoolExecutor(max_workers=1) as saver:
        from storage import refresh_colors

        if refresh_colors(job, inv):
            write(job / "status/inventory.json", inv)
        pending = None
        preview = []
        write(job / "state/preview.json", preview)

        def publish_preview(entry):
            preview.append(entry)
            write(job / "state/preview.json", preview)

        def finish_pending():
            nonlocal pending
            if pending is not None:
                future, event = pending
                event["save_s"] = future.result()
                publish_preview(event.pop("_preview"))
                write(job / "status/status.json", event)
                pending = None

        for a in mapping["archives"]:
            check_stop()
            aid = a["id"]
            if aid not in selected:
                continue
            source = Path(a["source"])
            stage = job / "staged" / aid
            suffix = plan.get("output_suffix", "-JaNai4X")
            if suffix and not re.fullmatch(r"-[A-Za-z0-9_-]+", suffix):
                raise ValueError("invalid_output_suffix")
            target = target_for(a, plan)
            target.parent.mkdir(parents=True, exist_ok=True)
            status = next(v for v in inv["archives"] if v["id"] == aid)
            recipe = hashlib.sha256(
                json.dumps(
                    {
                        "selection": selected[aid],
                        "runtime": runtime,
                        "source": a["source_sha256"],
                        "scale": scale,
                        "output_format": plan.get("output_format", "jpeg"),
                        "output_suffix": plan.get("output_suffix", "-JaNai4X"),
                        "jpeg_quality": 95,
                        "preserve_original_jpeg": True,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if "page_threshold" in plan:
                recipe = hashlib.sha256(
                    (recipe + "|page-threshold|" + str(plan["page_threshold"])).encode()
                ).hexdigest()
            receipt_path = job / "status" / ("receipt-" + aid + ".json")
            if target.is_file() and not (job / "status/rebuild-bw.json").exists():
                done += status["page_count"]
                continue
            if (not a.get("local_snapshot") and sha(source) != a["source_sha256"]) or sha(
                stage
            ) != a["stage_sha256"]:
                raise ValueError("source_or_stage_changed")
            pages = {p["id"]: p for p in status["pages"]}
            outputs = {}
            output_names = {m["original"].casefold() for m in a["members"] if not m["image"]}
            required = (
                max(output_size(p, plan)[0] * output_size(p, plan)[1] * 4 for p in status["pages"])
                + 64 * 1024**2
            )
            if shutil.disk_usage(job).free < required:
                raise ValueError("insufficient_disk_space")
            # Resolve output names in original order, independently of inference scheduling.
            page_output_names = {}
            for member in a["members"]:
                if not member["image"]:
                    continue
                name = str(PurePosixPath(member["original"]).with_suffix(".jpg"))
                if name.casefold() in output_names:
                    name = member["original"] + ".jpg"
                if name.casefold() in output_names:
                    raise ValueError("output_name_collision")
                output_names.add(name.casefold())
                page_output_names[member["id"]] = name
            scheduled = sorted(
                (m for m in a["members"] if m["image"]),
                key=lambda m: (bool(pages[m["id"]]["color"]), m["order"]),
            )
            last_was_bw = False
            with zipfile.ZipFile(stage) as src:
                for member in scheduled:
                    check_stop()
                    if not member["image"]:
                        continue
                    pid = member["id"]
                    p = pages[pid]
                    rule = choose(p, selected[aid]["rules"])
                    model_id = rule["model"]
                    if p["color"] and last_was_bw:
                        finish_pending()
                    last_was_bw = not p["color"]
                    multiplier = page_scale(p, plan)
                    passthrough = multiplier == 1 and p.get("format") == "JPEG"
                    out = None
                    if not passthrough:
                        model = runtime["models"][model_id]
                        modelpath = Path(model["path"])
                        if model_id not in model_hashes:
                            model_hashes[model_id] = sha(modelpath)
                        if model.get("sha256") and model_hashes[model_id] != model["sha256"]:
                            raise ValueError("model_changed")
                        fingerprint = hashlib.sha256(
                            json.dumps(
                                {
                                    "source": a["source_sha256"],
                                    "page": pid,
                                    "model_sha": model_hashes[model_id],
                                    "rule": rule,
                                    "scale": scale,
                                },
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()
                        if multiplier == 1:
                            fingerprint = hashlib.sha256(
                                (a["source_sha256"] + "|" + pid + "|keep-original").encode()
                            ).hexdigest()
                        legacy = job / "processed" / aid / (str(member["order"]) + ".png")
                        # Page identity stays in the stamp, never in the directory key.
                        group = (
                            "original"
                            if multiplier == 1
                            else model_id
                            + "-"
                            + hashlib.sha256(
                                json.dumps(
                                    {
                                        "model_sha": model_hashes[model_id],
                                        "rule": rule,
                                        "scale": scale,
                                    },
                                    sort_keys=True,
                                ).encode()
                            ).hexdigest()[:16]
                        )
                        out = job / "processed" / aid / group / (str(member["order"]) + ".jpg")
                        out.parent.mkdir(parents=True, exist_ok=True)
                        size = output_size(p, plan)
                        valid = False
                        legacy_image = None
                        candidates = (
                            out,
                            out.with_suffix(".png"),
                            job / "processed" / aid / fingerprint / (str(member["order"]) + ".png"),
                            legacy,
                        )
                        for candidate in candidates:
                            try:
                                stamp_path = (
                                    candidate.with_suffix(".jpg.json")
                                    if candidate.suffix == ".jpg"
                                    else candidate.with_suffix(".json")
                                )
                                stamp_data = read(stamp_path)
                                if stamp_data.get("fingerprint") != fingerprint:
                                    continue
                                if candidate.suffix == ".jpg" and (
                                    stamp_data.get("jpeg_quality") != 95
                                    or stamp_data.get("subsampling") != 0
                                ):
                                    continue
                                data = candidate.read_bytes()
                                if hashlib.sha256(data).hexdigest() != stamp_data.get("sha256"):
                                    continue
                                with open_generated(io.BytesIO(data), size) as cached:
                                    cached.load()
                                    if cached.format != (
                                        "JPEG" if candidate.suffix == ".jpg" else "PNG"
                                    ):
                                        continue
                                    if candidate.suffix == ".png":
                                        legacy_image = cached.copy()
                                valid = candidate.suffix == ".jpg"
                                break
                            except (OSError, ValueError, KeyError):
                                continue
                        if not valid:
                            write(
                                job / "status/status.json",
                                {
                                    "state": "keep-original"
                                    if multiplier == 1
                                    else "page-inference",
                                    "archive": aid,
                                    "completed": done,
                                    "total": total,
                                    "model": "original" if multiplier == 1 else model_id,
                                    "pid": os.getpid(),
                                },
                            )
                            decode_s = 0.0
                            inference_s = 0.0
                            if legacy_image is not None:
                                result = legacy_image
                            else:
                                started = time.perf_counter()
                                with Image.open(io.BytesIO(src.read(pid))) as im:
                                    im.load()
                                    decode_s = time.perf_counter() - started
                                    started = time.perf_counter()
                                    result = (
                                        im.copy()
                                        if multiplier == 1
                                        else engine.upscale(
                                            im,
                                            model,
                                            rule,
                                            p["color"] or plan.get("bw_mode") == "color",
                                        )
                                    )
                                    if multiplier != 1:
                                        check_blank_result(im, result, member["order"])
                                    inference_s = time.perf_counter() - started
                            if result.size != size:
                                if scale != 2 or result.size != (p["width"] * 4, p["height"] * 4):
                                    raise ValueError("unexpected_model_size")
                                import numpy as np

                                pixels = np.asarray(result, dtype=np.float32) / 255
                                linear = np.where(
                                    pixels <= 0.04045,
                                    pixels / 12.92,
                                    ((pixels + 0.055) / 1.055) ** 2.4,
                                )
                                if linear.ndim == 2:
                                    resized = np.asarray(
                                        Image.fromarray(linear).resize(
                                            size, Image.Resampling.LANCZOS
                                        )
                                    )
                                else:
                                    resized = np.stack(
                                        [
                                            np.asarray(
                                                Image.fromarray(linear[:, :, c]).resize(
                                                    size, Image.Resampling.LANCZOS
                                                )
                                            )
                                            for c in range(3)
                                        ],
                                        axis=2,
                                    )
                                resized = np.clip(resized, 0, 1)
                                srgb = np.where(
                                    resized <= 0.0031308,
                                    resized * 12.92,
                                    1.055 * resized ** (1 / 2.4) - 0.055,
                                )
                                result = Image.fromarray(np.round(srgb * 255).astype(np.uint8))
                            finish_pending()
                            pending = (
                                saver.submit(save_jpeg_cache, result, out, fingerprint, size),
                                {
                                    "state": "page-complete",
                                    "archive": aid,
                                    "completed": done + 1,
                                    "total": total,
                                    "model": model_id,
                                    "decode_s": round(decode_s, 3),
                                    "inference_s": round(inference_s, 3),
                                    "_preview": {
                                        "archive": aid,
                                        "member": pid,
                                        "order": member["order"],
                                        "color": p["color"],
                                        "cache": str(out.relative_to(job)),
                                        "size": size,
                                    },
                                },
                            )
                    name = page_output_names[pid]
                    outputs[pid] = (out, name)
                    done += 1
                    if passthrough or valid:
                        finish_pending()
                        publish_preview(
                            {
                                "archive": aid,
                                "member": pid,
                                "order": member["order"],
                                "color": p["color"],
                                "cache": str(out.relative_to(job)) if out else None,
                                "size": output_size(p, plan),
                            }
                        )
                        write(
                            job / "status/status.json",
                            {
                                "state": "page-complete",
                                "archive": aid,
                                "completed": done,
                                "total": total,
                                "model": "original" if multiplier == 1 else model_id,
                            },
                        )
                finish_pending()
                partial = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
                expected = []
                encoded_hashes = {}
                encoded_pages = 0
                provenance = {}
                write(
                    job / "status/status.json",
                    {
                        "state": "packaging-jpeg",
                        "archive": aid,
                        "completed": 0,
                        "total": len(pages),
                        "pid": os.getpid(),
                    },
                )
                try:
                    with zipfile.ZipFile(
                        partial, "x", compression=zipfile.ZIP_STORED, allowZip64=True
                    ) as dst:
                        for m in a["members"]:
                            check_stop()
                            if m["image"]:
                                path, name = outputs[m["id"]]
                                page = pages[m["id"]]
                                data = src.read(m["id"]) if path is None else path.read_bytes()
                                dst.writestr(name, data)
                                provenance[name] = {
                                    "encoded_sha256": hashlib.sha256(data).hexdigest(),
                                    "size": output_size(page, plan),
                                    "preserved_original": path is None,
                                    "cache": str(path.relative_to(job))
                                    if path is not None
                                    else None,
                                    "cache_sha256": hashlib.sha256(data).hexdigest()
                                    if path is not None
                                    else None,
                                }
                                encoded_hashes[name] = hashlib.sha256(data).hexdigest()
                                encoded_pages += 1
                                write(
                                    job / "status/status.json",
                                    {
                                        "state": "packaging-jpeg",
                                        "archive": aid,
                                        "completed": encoded_pages,
                                        "total": len(pages),
                                        "pid": os.getpid(),
                                    },
                                )
                            else:
                                name = m["original"]
                                dst.writestr(name, src.read(m["id"]))
                            expected.append(name)
                    write(
                        job / "status/status.json",
                        {
                            "state": "verifying",
                            "archive": aid,
                            "completed": encoded_pages,
                            "total": len(pages),
                            "pid": os.getpid(),
                        },
                    )
                    with zipfile.ZipFile(partial) as check:
                        if check.namelist() != expected:
                            raise ValueError("archive_verification_failed")
                        for m in a["members"]:
                            check_stop()
                            if not m["image"]:
                                check.read(m["original"])
                                continue
                            if m["image"]:
                                path, name = outputs[m["id"]]
                                data = check.read(name)
                                if hashlib.sha256(data).hexdigest() != encoded_hashes[name]:
                                    raise ValueError("packed_page_changed")
                                expected_page = pages[m["id"]]
                                with open_generated(
                                    io.BytesIO(data), output_size(expected_page, plan)
                                ) as im:
                                    im.load()
                                    p = pages[m["id"]]
                                    if im.format != "JPEG" or im.size != output_size(p, plan):
                                        raise ValueError("packed_size_changed")
                    if not a.get("local_snapshot") and sha(source) != a["source_sha256"]:
                        raise ValueError("source_changed")
                    # rename refuses to replace an existing destination on Windows.
                    rebuilding = (job / "status/rebuild-bw.json").exists()
                    if target.exists() and not rebuilding:
                        raise FileExistsError("target_exists")
                    write(job / "state" / ("encoded-pages-" + aid + ".json"), provenance)
                    receipt = {
                        "jpeg_quality": 95,
                        "preserve_original_jpeg": True,
                        "archive": aid,
                        "state": "complete",
                        "pages": len(pages),
                        "bytes": partial.stat().st_size,
                        "sha256": sha(partial),
                        "recipe_sha256": recipe,
                        "crc": "passed",
                        "source_snapshot_verified": True,
                        "format": "JPEG",
                        "restored_names": True,
                    }
                    if rebuilding:
                        os.replace(partial, target)
                    else:
                        partial.rename(target)
                    # A complete receipt must refer to a published final file,
                    # never a .partial whose rename may still fail.
                    write(receipt_path, receipt)
                except BaseException:
                    # Retain .partial for diagnosis; never present it as complete.
                    raise
        (job / "status/rebuild-bw.json").unlink(missing_ok=True)
        write(
            job / "status/status.json",
            {"state": "complete", "completed": done, "total": total, "pid": os.getpid()},
        )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--input", required=True)
    s.add_argument("--job")
    s.add_argument("--output-root", default="./output")
    s.add_argument("--cache-root", default="./cache")
    s.add_argument("--recursive", action="store_true")
    s.add_argument("--only-name")
    r = sub.add_parser("run")
    r.add_argument("--job", required=True)
    r.add_argument("--plan", required=True)
    r.add_argument("--runtime", required=True)
    probe = sub.add_parser("probe")
    probe.add_argument("--job", required=True)
    repack = sub.add_parser("repack-jpeg")
    repack.add_argument("--job", required=True)
    a = parser.parse_args()
    from storage import default_job

    job = Path(a.job) if a.job else default_job(a.input, a.cache_root, a.recursive, a.only_name)
    try:
        # Suppress third-party exceptions, warnings and logs which could disclose names.
        with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
            if a.action == "scan":
                scan(a.input, job, a.recursive, a.only_name, a.output_root)
            elif a.action == "repack-jpeg":
                repack_jpeg(job)
            elif a.action == "probe":
                plan = read(job / "plan.json")
                reports = []
                for item in read(job / "state/map.json")["archives"]:
                    if item["id"] not in plan["archives"]:
                        continue
                    source = Path(item["source"])
                    target = target_for(item, plan)
                    partial = target.with_name(target.name + ".partial")
                    path = target if target.exists() else partial
                    reports.append(
                        {
                            "archive": item["id"],
                            "final_exists": target.exists(),
                            "bytes": path.stat().st_size if path.exists() else 0,
                            "modified_epoch": path.stat().st_mtime if path.exists() else None,
                        }
                    )
                write(job / "status/package-probe.json", {"packages": reports})
            else:
                run(job, a.plan, a.runtime)
        print(
            json.dumps(
                {
                    "state": read(job / "status/status.json")["state"],
                    "job": str(job),
                    "public_manifest": str(job / "status/inventory.json"),
                }
            )
        )
    except BaseException as e:
        report = error_report(e)
        write(job / "status/status.json", report)
        print(json.dumps(report))
        sys.exit(1)


if __name__ == "__main__":
    main()
