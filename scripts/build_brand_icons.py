"""Render every Cymatix icon from the master mark.

Single source: ``assets/brand/cymatix-mark.svg``. This script reads its
geometry (two cubic strokes + eight dots), rasterizes it supersampled with
Pillow, and writes:

- ``cymatix_context/launcher/static/icons/``: tray-{16,24,32,48,64}.png
  (the Python tray) and favicon.ico (the launcher dashboard);
- ``assets/brand/generated/``: icon-512.png, icon.ico and icon.icns (the
  desktop app and its installer), tray-template-32.png (macOS template,
  black + alpha), tray-{ok,warn,error}-32.png (tray with a status dot) and
  badge-{ok,warn,error}.png (16px overlay badges).

Outputs are committed; tests/test_brand_assets.py fails if one drifts from
a fresh render. Never hand-edit them; edit the SVG and rerun:

    python scripts/build_brand_icons.py

No dependencies beyond Pillow, and no SVG library: the mark only uses
``M``/``c`` path commands and ``<circle>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
MASTER = REPO / "assets" / "brand" / "cymatix-mark.svg"

ACCENT = (255, 159, 67)      # --color-accent (#ff9f43)
TILE_BG = (9, 12, 16)        # website --color-bg (#090c10)
STATUS = {
    "ok": (63, 185, 80),     # green
    "warn": (210, 153, 34),  # amber
    "error": (248, 81, 73),  # red
}
SUPERSAMPLE = 8
TRAY_SIZES = (16, 24, 32, 48, 64)

Point = Tuple[float, float]


def _mark_geometry() -> Tuple[List[List[Point]], List[Tuple[float, float, float]], float]:
    """(stroke polylines, dots (cx, cy, r), stroke width) in viewBox units."""
    svg = MASTER.read_text(encoding="utf-8")
    width = float(re.search(r'stroke-width="([\d.]+)"', svg).group(1))
    strokes = []
    for d in re.findall(r'<path d="([^"]+)"', svg):
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", d)]
        if not d.startswith("M") or "c" not in d or len(nums) != 8:
            raise ValueError(f"unsupported path in master mark: {d!r}")
        x0, y0, c1x, c1y, c2x, c2y, ex, ey = nums
        p0, p1 = (x0, y0), (x0 + c1x, y0 + c1y)
        p2, p3 = (x0 + c2x, y0 + c2y), (x0 + ex, y0 + ey)
        pts = []
        for i in range(97):
            t = i / 96
            mt = 1 - t
            pts.append(tuple(
                mt ** 3 * a + 3 * mt * mt * t * b + 3 * mt * t * t * c + t ** 3 * e
                for a, b, c, e in zip(p0, p1, p2, p3)
            ))
        strokes.append(pts)
    dots = [tuple(float(v) for v in m) for m in re.findall(
        r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)]
    return strokes, dots, width


def _mask(size: int, fill: float = 0.9) -> Image.Image:
    """Alpha mask of the mark centered in a size x size square."""
    strokes, dots, width = _mark_geometry()
    xs = [p[0] for s in strokes for p in s] + [x for x, _, r in dots for x in (x - r, x + r)]
    ys = [p[1] for s in strokes for p in s] + [y for _, y, r in dots for y in (y - r, y + r)]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    big = size * SUPERSAMPLE
    scale = big * fill / max(maxx - minx, maxy - miny)
    ox = (big - (maxx - minx) * scale) / 2 - minx * scale
    oy = (big - (maxy - miny) * scale) / 2 - miny * scale
    # Thin strokes vanish at tray sizes; keep at least ~1.2 output pixels.
    stroke = max(width * scale, 1.2 * SUPERSAMPLE)
    dot_scale = max(1.0, (1.4 * SUPERSAMPLE) / (4 * scale))

    m = Image.new("L", (big, big), 0)
    draw = ImageDraw.Draw(m)
    for pts in strokes:
        xy = [(x * scale + ox, y * scale + oy) for x, y in pts]
        draw.line(xy, fill=255, width=max(1, round(stroke)), joint="curve")
        for cx, cy in (xy[0], xy[-1]):  # round caps
            r = stroke / 2
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    for cx, cy, r in dots:
        cx, cy, r = cx * scale + ox, cy * scale + oy, r * scale * dot_scale
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return m.resize((size, size), Image.Resampling.BOX)


def render_mark(size: int, color: Tuple[int, int, int], fill: float = 0.9) -> Image.Image:
    """The mark in *color* on transparent, as RGBA."""
    img = Image.new("RGBA", (size, size), color + (0,))
    img.putalpha(_mask(size, fill))
    return img


def _dot(size: int, color: Tuple[int, int, int], ring: Tuple[int, int, int] = TILE_BG) -> Image.Image:
    big = size * SUPERSAMPLE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, big - 1, big - 1), fill=ring + (255,))
    inset = big // 7
    draw.ellipse((inset, inset, big - 1 - inset, big - 1 - inset), fill=color + (255,))
    return img.resize((size, size), Image.Resampling.BOX)


def app_tile(size: int) -> Image.Image:
    """App icon: the mark on a dark rounded tile."""
    big = size * SUPERSAMPLE
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=round(big * 0.22), fill=TILE_BG + (255,))
    tile = tile.resize((size, size), Image.Resampling.BOX)
    tile.alpha_composite(render_mark(size, ACCENT, fill=0.66))
    return tile


def _with_status(size: int, state: str) -> Image.Image:
    img = render_mark(size, ACCENT)
    d = max(6, round(size * 0.42))
    img.alpha_composite(_dot(d, STATUS[state]), (size - d, size - d))
    return img


def build(out_root: Path) -> List[str]:
    """Write every output under *out_root*; return repo-relative paths."""
    written: List[str] = []

    def save(rel: str, img: Image.Image, **kw) -> None:
        path = out_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(path, **kw)
        written.append(rel)

    tray = "cymatix_context/launcher/static/icons"
    for s in TRAY_SIZES:
        save(f"{tray}/tray-{s}.png", render_mark(s, ACCENT), optimize=False)
    save(f"{tray}/favicon.ico", render_mark(48, ACCENT),
         sizes=[(16, 16), (32, 32), (48, 48)])

    gen = "assets/brand/generated"
    tile = app_tile(512)
    save(f"{gen}/icon-512.png", tile, optimize=False)
    save(f"{gen}/icon.ico", tile,
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    save(f"{gen}/icon.icns", tile)
    save(f"{gen}/tray-template-32.png", render_mark(32, (0, 0, 0)), optimize=False)
    for state in STATUS:
        save(f"{gen}/tray-{state}-32.png", _with_status(32, state), optimize=False)
        save(f"{gen}/badge-{state}.png", _dot(16, STATUS[state]), optimize=False)
    return written


def main(argv: Iterable[str] = ()) -> int:
    for rel in build(REPO):
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
