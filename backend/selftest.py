"""Synthetic fixtures only; no user manga or GPU work."""

import io, tempfile, zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from pipeline import scan, run, read, write, sha
from storage import target_for, snapshot, signature
from cleanup_completed import cleanup


class FixtureEngine:
    def __init__(self, fail_at=0):
        self.calls = 0
        self.fail_at = fail_at

    def upscale(self, image, model, rule, color):
        self.calls += 1
        if self.calls == self.fail_at:
            raise RuntimeError("synthetic interruption")
        return image.resize((image.width * 4, image.height * 4))


def main():
    with tempfile.TemporaryDirectory(prefix="janai-test-", dir=Path.cwd()) as temp:
        root = Path(temp).resolve()
        source = root / "PrivateWork"
        source.mkdir()
        job = root / "cache"
        output = root / "output"
        book = source / "Book01.zip"
        with zipfile.ZipFile(book, "w") as z:
            for name in ["Private/001.jpg", "Private/001.png", "Private/010.png"]:
                b = io.BytesIO()
                Image.new("RGB", (8, 12), (20, 50, 80)).save(b, format="PNG")
                z.writestr(name, b.getvalue())
            z.writestr("info.xml", b"fixture")
        scan(source, job, output_root=output)
        inv = read(job / "status/inventory.json")
        aid = inv["archives"][0]["id"]
        item = read(job / "state/map.json")["archives"][0]
        assert aid == "Book01.zip"
        assert inv["archives"][0]["pages"][0]["id"] == "Private/001.jpg"
        scan(source, job, output_root=output)
        assert read(job / "status/inventory.json") == inv
        weights = root / "weights"
        weights.write_bytes(b"fixture")
        runtime = root / "runtime.json"
        write(runtime, {"models": {"fixture": {"path": str(weights), "sha256": sha(weights)}}})
        plan = {"scale": 4, "archives": {aid: {"rules": [{"model": "fixture"}]}}}
        write(job / "plan.json", plan)
        book.rename(source / "offline")
        try:
            run(job, job / "plan.json", runtime, FixtureEngine(2))
        except RuntimeError:
            pass
        else:
            raise AssertionError("interruption not raised")
        next((job / "processed").rglob("*.jpg")).write_bytes(b"broken png")
        target = target_for(item, plan)
        target.parent.mkdir(parents=True, exist_ok=True)
        leftover = target.with_suffix(".cbz.partial")
        leftover.write_bytes(b"interrupted")
        resumed = FixtureEngine()
        run(job, job / "plan.json", runtime, resumed)
        assert resumed.calls == 3
        assert target.suffix == ".cbz" and target.parent == output / source.name
        with zipfile.ZipFile(target) as z:
            assert z.testzip() is None and len(z.namelist()) == len(set(z.namelist()))
            for name in z.namelist():
                if name == "info.xml":
                    continue
                assert name.endswith(".jpg")
                with Image.open(io.BytesIO(z.read(name))) as im:
                    im.load()
                    assert im.format == "JPEG" and im.size == (32, 48)
        assert leftover.read_bytes() == b"interrupted"
        again = FixtureEngine()
        run(job, job / "plan.json", runtime, again)
        assert again.calls == 0
        cleanup(job)
        assert (job / "staged").exists() and (job / "state/map.json").exists()
        scan(source, job, output_root=output)
        run(job, job / "plan.json", runtime, again)
        assert again.calls == 0
        target.write_bytes(b"foreign output")
        run(job, job / "plan.json", runtime, again)
        assert target.read_bytes() == b"foreign output"
        folder = root / "Loose"
        folder.mkdir()
        Image.new("L", (8, 12)).save(folder / "01.png")
        scan(folder, root / "loosejob", output_root=output)
        loose = read(root / "loosejob/state/map.json")["archives"][0]
        assert loose["kind"] == "folder"
        assert target_for(loose, plan) == output / "Loose" / "Loose-JaNai4X.cbz"
        remote = root / "remote"
        remote.write_bytes(b"0123456789" * 1000)
        dest = root / "transfer/cache.cbz"
        dest.parent.mkdir()
        dest.with_suffix(".download.partial").write_bytes(remote.read_bytes()[:500])
        write(dest.with_suffix(".transfer.json"), {"source": signature(remote)})
        snapshot(remote, dest, job)
        assert sha(remote) == sha(dest)
        multi = root / "Multi"
        multi.mkdir()
        for n in ["01.zip", "02.zip"]:
            (multi / n).write_bytes((source / "offline").read_bytes())
        import storage

        original = storage.snapshot
        calls = []

        def fail_second(src, dst, j):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("fixture disconnect")
            return original(src, dst, j)

        with patch("storage.snapshot", fail_second):
            try:
                scan(multi, root / "multijob", output_root=output)
            except OSError:
                pass
        first = read(next((root / "multijob/state/checkpoints").glob("*.json")))["state"]["id"]
        scan(multi, root / "multijob", output_root=output)
        assert first in [a["id"] for a in read(root / "multijob/status/inventory.json")["archives"]]
    print(
        "PASS: CBZ/JPEG, offline snapshot, scan resume, transfer resume, corrupt page repair, output conflicts, partial recovery, cleanup/restart, loose pages, original filenames"
    )


if __name__ == "__main__":
    main()
