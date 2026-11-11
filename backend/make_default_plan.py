"""Default per-page model routing."""

import argparse, json
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--job", required=True)
    a = p.parse_args()
    job = Path(a.job)
    inv = json.loads((job / "status/inventory.json").read_text(encoding="utf-8-sig"))
    heights = [1200, 1300, 1400, 1500, 1600, 1920, 2048]
    rules = [
        {
            "color": True,
            "engine": "tensorrt",
            "model": "illustration-detail-fdat-m-2x",
            "tile": 256,
            "padding": 64,
        }
    ]
    for i, h in enumerate(heights):
        rules.append(
            {
                "color": False,
                "engine": "spandrel",
                "height_min": 0 if i == 0 else (heights[i - 1] + h) // 2,
                "height_max": 999999 if i == len(heights) - 1 else (h + heights[i + 1]) // 2 - 1,
                "model": "manga-" + str(h),
                "tile": 384,
                "padding": 96,
            }
        )
    plan = {
        "scale": 4,
        "color_scale": 2,
        "output_suffix": "-JaNai4X",
        "output_format": "jpeg",
        "archives": {v["id"]: {"rules": rules} for v in inv["archives"]},
    }
    (job / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "state": "planned",
                "archives": len(plan["archives"]),
                "backend": "PyTorch/spandrel",
                "scale": 4,
                "jpeg_quality": 98,
            }
        )
    )


if __name__ == "__main__":
    main()
