"""Cooperative pause and cancellation; stop always takes priority."""

import os, time
from pathlib import Path


class Stopped(Exception):
    pass


def check(pause=True):
    def stopped():
        for name in ("MANGA_STOP_FILE", "MANGA_BOOK_STOP_FILE"):
            marker = os.environ.get(name)
            if marker and Path(marker).exists():
                raise Stopped("stopped_by_user")

    stopped()
    marker = os.environ.get("MANGA_PAUSE_FILE")
    if not pause or not marker:
        return
    marker = Path(marker)
    acknowledged = False
    while marker.exists():
        stopped()
        if not acknowledged:
            from fileio import atomic_json

            atomic_json(
                marker.with_name("pause-state.json"),
                {"state": "paused", "pid": os.getpid(), "updated_epoch": time.time()},
                optional=True,
            )
            acknowledged = True
        time.sleep(0.1)
    if acknowledged:
        from fileio import atomic_json

        atomic_json(
            marker.with_name("pause-state.json"),
            {"state": "running", "pid": os.getpid(), "updated_epoch": time.time()},
            optional=True,
        )
    stopped()
