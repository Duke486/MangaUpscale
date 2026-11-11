"""Retain startup errors even when launched without a console."""

import os, sys, traceback, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
log = (ROOT / "启动日志.txt").open("w", encoding="utf-8", buffering=1)
sys.stdout = log
sys.stderr = log
try:
    (ROOT / "startup-status.json").write_text(
        json.dumps({"state": "starting", "pid": os.getpid()}), encoding="utf-8"
    )
    import gui

    gui.main()
except Exception:
    traceback.print_exc()
    (ROOT / "startup-status.json").write_text(
        json.dumps({"state": "failed", "pid": os.getpid()}), encoding="utf-8"
    )
    import ctypes

    ctypes.windll.user32.MessageBoxW(
        None, "启动失败，详细原因已保存到程序目录的“启动日志.txt”。", "漫画超分", 16
    )
    sys.exit(1)
