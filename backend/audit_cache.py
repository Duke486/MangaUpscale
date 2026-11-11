"""Verify cached archives and report damaged files."""

import argparse, contextlib, hashlib, json, os, zipfile
from pathlib import Path
from pipeline import read, sha, error_report


def audit(job, recent):
    job = Path(job)
    children = job / "jobs"
    rank = []
    for child in children.iterdir():
        status = child / "status/status.json"
        if status.exists():
            rank.append((status.stat().st_mtime, child))
    reports = []
    for _, child in sorted(rank, reverse=True)[:recent]:
        report = {"task": child.name, "latest_status": read(child / "status/status.json")}
        details = []
        manifest = child / "state/scan.json"
        if manifest.exists():
            for item in read(manifest)["items"]:
                aid = item["id"]
                raw = child / "downloads" / aid
                transfer = raw.with_suffix(".transfer.json")
                partial = raw.with_suffix(".download.partial")
                stage = child / "staged" / aid
                cp = child / "state/checkpoints" / (aid + ".json")
                entry = {
                    "archive": aid,
                    "snapshot_exists": raw.exists(),
                    "partial_bytes": partial.stat().st_size if partial.exists() else 0,
                }
                if raw.exists() and transfer.exists():
                    receipt = read(transfer)
                    entry.update(
                        {
                            "snapshot_bytes": raw.stat().st_size,
                            "expected_bytes": receipt.get("source", {}).get("size"),
                            "snapshot_hash_matches": sha(raw) == receipt.get("sha256"),
                        }
                    )
                    with zipfile.ZipFile(raw) as z:
                        entry.update(
                            {
                                "snapshot_crc_passed": z.testzip() is None,
                                "snapshot_members": len(
                                    [v for v in z.infolist() if not v.is_dir()]
                                ),
                            }
                        )
                if stage.exists() and cp.exists():
                    checkpoint = read(cp)
                    entry["stage_hash_matches"] = sha(stage) == checkpoint["state"]["stage_sha256"]
                    with zipfile.ZipFile(stage) as z:
                        entry.update(
                            {
                                "stage_crc_passed": z.testzip() is None,
                                "stage_members": len([v for v in z.infolist() if not v.is_dir()]),
                            }
                        )
                    entry["analysed_pages"] = checkpoint["status"]["page_count"]
                details.append(entry)
        report["cache"] = details
        reports.append(report)
    return {"read_only": True, "network_source_read": False, "tasks": reports}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True)
    p.add_argument("--recent", type=int, default=3)
    a = p.parse_args()
    try:
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            result = audit(a.job, a.recent)
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps(error_report(e)))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
