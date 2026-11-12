"""Persistent readers for the comparison UI; never run inference or write task state."""

import json
import sys
import zipfile
from collections import OrderedDict
from pathlib import Path

from PIL import Image
from archive_pages import IMAGES, archive_entries
from storage import signature


class Reader:
    def __init__(self):
        self.archive = None
        self.identity = None
        self.entries = []
        self.pages = OrderedDict()

    def close(self):
        if self.archive:
            self.archive.close()
            self.archive = None
        self.identity = None
        self.pages.clear()

    def handle(self, request):
        source = Path(request["source"])
        if signature(source) != request["signature"]:
            raise ValueError("源漫画已变化，请重新检测")
        identity = (str(source), request["signature"]["size"], request["signature"]["mtime_ns"])
        if identity != self.identity:
            self.close()
            self.archive = zipfile.ZipFile(source)
            self.entries = [
                entry
                for entry in archive_entries(self.archive, source.suffix.lower() == ".epub")
                if Path(entry.filename).suffix.lower() in IMAGES
            ]
            self.identity = identity
        destination = Path(request["destination"])
        if request["action"] == "page":
            page = min(max(0, request.get("page", 0)), max(0, len(self.entries) - 1))
            if not self.entries:
                raise ValueError("漫画中没有可预览的图片")
            data = self.pages.get(page)
            if data is None:
                data = self.archive.read(self.entries[page])
                # Bound the compressed-original cache; decoded pages are not retained here.
                if len(data) <= 32 * 1024**2:
                    self.pages[page] = data
                    while sum(map(len, self.pages.values())) > 32 * 1024**2:
                        self.pages.popitem(last=False)
            else:
                self.pages.move_to_end(page)
            destination.write_bytes(data)
            return {"total": len(self.entries), "page": page, "bytes": len(data)}
        ready = []
        for page in request["pages"]:
            if not 0 <= page < len(self.entries):
                continue
            path = destination / f"thumb-{request['book']}-{page}.png"
            try:
                if not path.exists():
                    with (
                        self.archive.open(self.entries[page]) as stream,
                        Image.open(stream) as image,
                    ):
                        image.draft("RGB", (96, 120))
                        image.thumbnail((72, 96), Image.Resampling.BILINEAR, reducing_gap=2.0)
                        image.convert("RGB").save(path)
                ready.append(page)
            except (OSError, ValueError, zipfile.BadZipFile):
                continue
        return {"pages": ready}


def main():
    from parent_guard import start

    start()
    reader = Reader()
    try:
        for line in sys.stdin:
            request = json.loads(line)
            try:
                result = reader.handle(request)
            except Exception as error:
                result = {"error": str(error)}
            print(json.dumps({**result, "book": request["book"], "id": request["id"]}), flush=True)
    finally:
        reader.close()


if __name__ == "__main__":
    main()
