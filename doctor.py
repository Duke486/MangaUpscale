"""Check installed dependencies and model files."""

import importlib.metadata
import json
from pathlib import Path
import sys


def main():
    root = Path(__file__).resolve().parent
    from backend.runtime_config import load_runtime

    runtime = load_runtime(root / "runtime.json")
    dependencies = {}
    for package in ("Pillow", "numpy", "torch", "spandrel", "tensorrt_cu12"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    models = {key: Path(value["path"]).is_file() for key, value in runtime["models"].items()}
    report = {
        "python": sys.version.split()[0],
        "configured_python_exists": Path(runtime["python"]).is_file(),
        "qt_runtime_exists": (root / "ui_runtime/PySide6/__init__.py").is_file(),
        "resource_root_exists": Path(runtime.get("resource_root", root)).is_dir(),
        "dependencies": dependencies,
        "model_files_present": models,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return (
        0
        if all(models.values())
        and all(dependencies.values())
        and report["qt_runtime_exists"]
        and report["configured_python_exists"]
        and report["resource_root_exists"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
