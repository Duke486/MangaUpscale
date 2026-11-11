"""Resolve the portable runtime relative to its configuration file."""

import json
from pathlib import Path


def load_runtime(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))

    def resolve(value):
        return str((path.parent / value).resolve())

    for key in ("python", "resource_root"):
        if key in config:
            config[key] = resolve(config[key])
    for model in config.get("models", {}).values():
        for key in ("path", "onnx", "nunif_root"):
            if key in model:
                model[key] = resolve(model[key])
    return config
