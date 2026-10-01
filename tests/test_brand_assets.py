"""Phase 4: the Cymatix mark as a single source (assets/brand/cymatix-mark.svg)
rendered by scripts/build_brand_icons.py into every icon the launcher and the
desktop app ship. Generated files are committed and must match a fresh
render (never hand-edited)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops

REPO = Path(__file__).resolve().parents[1]
BRAND = REPO / "assets" / "brand"
TRAY_ICONS = REPO / "cymatix_context" / "launcher" / "static" / "icons"
ACCENT = (255, 159, 67)


def _gen():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import build_brand_icons  # type: ignore
        return build_brand_icons
    finally:
        sys.path.remove(str(REPO / "scripts"))


def _pixels(img: Image.Image) -> list:
    # getdata() is deprecated from Pillow 12 in favor of get_flattened_data(),
    # which older Pillow builds (CI) do not have.
    flat = getattr(img, "get_flattened_data", None)
    return list(flat() if flat else img.getdata())


def _max_diff(a: Image.Image, b: Image.Image) -> int:
    assert a.size == b.size and a.mode == b.mode
    return max(hi for _lo, hi in ImageChops.difference(a, b).getextrema())


def test_master_svg_is_the_website_mark():
    svg = (BRAND / "cymatix-mark.svg").read_text(encoding="utf-8")
    assert 'd="M24 12c24 8 24 52 0 60"' in svg and 'd="M60 12c-24 8-24 52 0 60"' in svg
    assert svg.count("<circle") == 8 and "#ff9f43" in svg.lower()


def test_render_is_deterministic():
    gen = _gen()
    assert _max_diff(gen.render_mark(64, ACCENT), gen.render_mark(64, ACCENT)) == 0


def test_render_is_the_accent_mark_on_transparent():
    img = _gen().render_mark(64, ACCENT)
    assert img.mode == "RGBA" and img.size == (64, 64)
    assert img.getpixel((0, 0))[3] == 0                 # transparent corner
    opaque = [p for p in _pixels(img) if p[3] == 255]
    assert opaque and all(p[:3] == ACCENT for p in opaque)


def test_committed_outputs_match_a_fresh_render(tmp_path):
    gen = _gen()
    written = gen.build(tmp_path)
    assert written, "generator wrote nothing"
    for rel in written:
        fresh = tmp_path / rel
        committed = REPO / rel
        assert committed.exists(), f"{rel} not committed; run scripts/build_brand_icons.py"
        if fresh.suffix == ".png":
            a = Image.open(fresh).convert("RGBA")
            b = Image.open(committed).convert("RGBA")
            # Small tolerance: resampling may differ by a level across Pillow builds.
            assert _max_diff(a, b) <= 2, f"{rel} is stale; run scripts/build_brand_icons.py"


@pytest.mark.parametrize("size", [16, 24, 32, 48, 64])
def test_tray_pngs_ship_with_the_launcher(size):
    img = Image.open(TRAY_ICONS / f"tray-{size}.png")
    assert img.size == (size, size) and img.mode == "RGBA"


def test_app_icons():
    gen = BRAND / "generated"
    ico = Image.open(gen / "icon.ico")
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico.info["sizes"])
    assert Image.open(gen / "icon-512.png").size == (512, 512)
    assert Image.open(gen / "icon.icns").size[0] >= 256
    mono = Image.open(gen / "tray-template-32.png").convert("RGBA")
    assert {p[:3] for p in _pixels(mono) if p[3] == 255} == {(0, 0, 0)}
    for state in ("ok", "warn", "error"):
        assert Image.open(gen / f"badge-{state}.png").size == (16, 16)
        assert Image.open(gen / f"tray-{state}-32.png").size == (32, 32)


def test_favicon_is_served_and_linked():
    assert set(Image.open(TRAY_ICONS / "favicon.ico").info["sizes"]) >= {(16, 16), (32, 32)}
    layout = (REPO / "cymatix_context/launcher/templates/layout.html").read_text(encoding="utf-8")
    assert 'href="/static/icons/favicon.ico"' in layout


def test_tray_uses_the_packaged_mark():
    from cymatix_context.launcher.tray import _build_icon_image
    img = _build_icon_image(64).convert("RGBA")
    opaque = [p[:3] for p in _pixels(img) if p[3] == 255]
    assert opaque and all(p == ACCENT for p in opaque)


def test_tray_falls_back_when_icons_are_missing(monkeypatch, tmp_path):
    from cymatix_context.launcher import tray
    monkeypatch.setattr(tray, "_ICON_DIR", tmp_path)
    img = tray._build_icon_image(64)
    assert img.size == (64, 64)  # programmatic fallback, no crash
