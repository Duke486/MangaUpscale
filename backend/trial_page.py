"""Read or render a page without touching the production task cache."""

import argparse
import io
import json
import zipfile
from pathlib import Path

from PIL import Image
from pipeline import IMAGES, check_blank_result, choose, metadata, page_scale, read, sha
from storage import signature
from archive_pages import archive_entries


def main():
    from parent_guard import start

    start()
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    args = parser.parse_args()
    request = read(args.request)
    source = Path(request["source"])
    if signature(source) != request["signature"]:
        raise ValueError("源漫画已变化，请重新检测")
    with zipfile.ZipFile(source) as archive:
        entries = [
            entry
            for entry in archive_entries(archive, source.suffix.lower() == ".epub")
            if Path(entry.filename).suffix.lower() in IMAGES
        ]
        page = request.get("page")
        if page is None:
            print(json.dumps(len(entries)))
            return
        data = archive.read(entries[page])
        if not request.get("mode"):
            thumbs = range(max(0, page - 2), min(len(entries), page + 3))
            for index in thumbs:
                path = args.request.with_name(f"thumb-{request['book']}-{index}.png")
                if path.exists():
                    continue
                try:
                    with archive.open(entries[index]) as stream, Image.open(stream) as thumb:
                        thumb.thumbnail((64, 80))
                        thumb.convert("RGB").save(path)
                except (OSError, ValueError, zipfile.BadZipFile):
                    continue
    destination = args.request.with_name("image.png")
    args.request.with_name("image-info.json").write_text(
        json.dumps({"original_bytes": len(data)}), encoding="utf-8"
    )
    with Image.open(io.BytesIO(data)) as image:
        if not request.get("mode"):
            image.save(destination, format="PNG")
            return
        render(image, metadata(data), request, destination)


def render(image, page, request, destination):
    import ctypes
    from inference import Engine
    from mode_jobs import rules_for

    mode = request["mode"]
    plan = {**request["plan"], "bw_mode": mode, "scale": 4, "color_scale": 2}
    if page_scale(page, plan) == 1:
        image.save(destination, format="PNG")
        print("passthrough")
        return
    rule = choose(page, rules_for(mode))
    from runtime_config import load_runtime

    runtime = load_runtime(Path(__file__).resolve().parents[1] / "runtime.json")
    model = runtime["models"][rule["model"]]
    if model.get("sha256") and sha(Path(model["path"])) != model["sha256"]:
        raise ValueError("模型文件已变化")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    lock = kernel.CreateMutexW(None, False, "Local\\MangaUpscaleSingleGPU")
    if not lock:
        raise OSError("无法取得 GPU 锁")
    try:
        if kernel.WaitForSingleObject(lock, 0) not in (0, 128):
            raise RuntimeError("GPU 正在处理另一个漫画任务")
        try:
            engine = Engine(resource_root=runtime.get("resource_root"))
            result = engine.upscale(image, model, rule, page["color"] or mode == "color")
            check_blank_result(image, result, request["page"] + 1)
            result.save(destination)
            print("color" if page["color"] else "bw")
        finally:
            kernel.ReleaseMutex(lock)
    finally:
        kernel.CloseHandle(lock)


if __name__ == "__main__":
    main()
