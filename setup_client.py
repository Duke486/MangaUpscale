"""Install dependencies and fetch models from their upstream releases."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


def fetch(url, destination):
    if destination.is_file():
        return
    print("Downloading:", url, flush=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    with urllib.request.urlopen(url, timeout=120) as source, temporary.open("wb") as target:
        shutil.copyfileobj(source, target)
    temporary.replace(destination)


def main():
    cache = ROOT / "downloads"
    cache.mkdir(exist_ok=True)
    if not (ROOT / "dependencies.ok").exists():
        try:
            import pip
        except ImportError:
            script = cache / "get-pip.py"
            fetch("https://bootstrap.pypa.io/get-pip.py", script)
            subprocess.run([sys.executable, str(script)], check=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "torch==2.11.0",
                "torchvision==0.26.0",
                "--index-url",
                "https://download.pytorch.org/whl/cu128",
            ],
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "PySide6==6.12.0",
                "Pillow==13.0.0",
                "numpy==2.4.6",
                "spandrel==0.4.2",
                "tensorrt-cu12==10.9.0.34",
                "timm",
                "einops",
                "packaging",
                "pyyaml",
                "requests",
                "tqdm",
            ],
            check=True,
        )
        (ROOT / "dependencies.ok").write_text("ready")
    config = json.loads((ROOT / "runtime.json").read_text(encoding="utf-8"))
    archives = [
        (
            "manga",
            "https://github.com/the-database/MangaJaNai/releases/download/1.0.0/MangaJaNai_V1_ModelsOnly.zip",
        ),
        (
            "color",
            "https://github.com/the-database/MangaJaNai/releases/download/3.0.0/IllustrationJaNai_V3_ONNX_bf16.7z",
        ),
        (
            "flat",
            "https://github.com/nagadomi/nunif/releases/download/0.0.0/waifu2x_pretrained_models_20250502.zip",
        ),
    ]
    for group, url in archives:
        models = [
            (key, model)
            for key, model in config["models"].items()
            if (
                key.startswith("manga-")
                if group == "manga"
                else key
                == ("flat-art-denoise2" if group == "flat" else "illustration-detail-fdat-m-2x")
            )
        ]
        if all((ROOT / model["path"]).is_file() for _, model in models):
            continue
        archive = cache / url.rsplit("/", 1)[-1]
        fetch(url, archive)
        if group == "color":
            seven = ROOT / "runtime/tools/7z.exe"
            listing = subprocess.check_output(
                [str(seven), "l", "-slt", str(archive)], text=True, encoding="utf-8"
            )
            names = [line[7:] for line in listing.splitlines() if line.startswith("Path = ")]
            candidates = [
                name
                for name in names
                if "2x_IllustrationJaNai_V3detail_FDAT_M" in Path(name).name
                and name.endswith(".onnx")
            ]
            if not candidates:
                raise RuntimeError("官方模型包中没有找到 FDAT M 2× ONNX 模型")
            name = sorted(candidates)[0]
            target = ROOT / models[0][1]["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.with_suffix(".partial").open("wb") as output:
                subprocess.run(
                    [str(seven), "x", "-so", str(archive), name], stdout=output, check=True
                )
            target.with_suffix(".partial").replace(target)
            models[0][1]["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            with zipfile.ZipFile(archive) as source:
                for _, model in models:
                    target = ROOT / model["path"]
                    candidates = [
                        name for name in source.namelist() if Path(name).name == target.name
                    ]
                    if group == "flat":
                        candidates = [
                            name
                            for name in candidates
                            if "swin_unet/art/" in name.replace("\\", "/")
                        ]
                    if len(candidates) != 1:
                        raise RuntimeError("无法唯一定位官方模型：" + target.name)
                    data = source.read(candidates[0])
                    if hashlib.sha256(data).hexdigest() != model["sha256"]:
                        raise RuntimeError("模型校验失败：" + target.name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
        (ROOT / "runtime.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (ROOT / "client-ready.ok").write_text("ready")


if __name__ == "__main__":
    main()
