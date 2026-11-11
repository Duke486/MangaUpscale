"""Bounded JSONL polling for the GUI; an unfinished final line is retried."""

import json
from pathlib import Path

MAX_BYTES = 256 * 1024
MAX_LINES = 256


def read_events(path, offset, max_bytes=MAX_BYTES, max_lines=MAX_LINES):
    with Path(path).open("rb") as stream:
        size = stream.seek(0, 2)
        if offset > size:
            offset = 0
        stream.seek(offset)
        data = stream.read(max_bytes)
    boundary = data.rfind(b"\n")
    if boundary < 0:
        # A producer must keep each event below the polling budget. Drop a
        # corrupt oversized fragment so it cannot stall the reader forever.
        return [], offset + len(data) if len(data) == max_bytes else offset
    lines = data[: boundary + 1].splitlines(keepends=True)[:max_lines]
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events, offset + sum(map(len, lines))
