"""Detect local page color while discounting small marginal scanlation marks."""

VERSION = 2


def classify(image):
    import numpy as np
    from PIL import Image

    sample = image.convert("RGBA") if image.mode in ("RGBA", "LA", "P") else image.convert("RGB")
    if sample.mode == "RGBA":
        background = Image.new("RGBA", sample.size, "white")
        sample = Image.alpha_composite(background, sample).convert("RGB")
    sample.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    pixels = np.asarray(sample, dtype=np.int16)
    spread = pixels.max(axis=2) - pixels.min(axis=2)
    # Estimate mild scan tint from near-neutral paper, without neutralizing colored artwork.
    paper = (pixels.min(axis=2) >= 180) & (spread <= 12)
    if paper.mean() >= 0.5:
        offsets = np.median((pixels - pixels[:, :, 1:2])[paper], axis=0)
        pixels = pixels - offsets.astype(np.int16)
        spread = pixels.max(axis=2) - pixels.min(axis=2)
    colored = spread >= 16
    neighbors = np.pad(colored.astype(np.uint8), 1)
    support = sum(
        neighbors[y : y + colored.shape[0], x : x + colored.shape[1]]
        for y in range(3)
        for x in range(3)
    )
    h, w = colored.shape
    colored &= support >= min(3, h * w)
    dy, dx = int(h * 0.08), int(w * 0.08)
    core = colored[dy : h - dy, dx : w - dx]
    ink = pixels.min(axis=2)[dy : h - dy, dx : w - dx] < 245
    area = h * w
    body_count = int(core.sum())
    total_count = int(colored.sum())
    foreground = int(ink.sum())
    minimum = min(12, area * 0.1)
    color = (
        body_count >= max(minimum, area * 0.0008)
        or (body_count >= max(minimum, area * 0.0002) and body_count >= foreground * 0.03)
        or total_count >= max(minimum, area * 0.012)
    )
    return {
        "color": bool(color),
        "color_version": VERSION,
        "chroma": round(float(spread.mean()), 3),
        "color_fraction": round(total_count / area, 6),
        "body_color_fraction": round(body_count / area, 6),
    }
