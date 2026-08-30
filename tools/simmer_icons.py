#!/usr/bin/env python3
"""Regenerate every Simmer icon from the one master logo.

Source of truth : simmer/ui/assets/facades_simmer_logo_mark.png  (1024x1024)
Outputs         : simmer/ui/static/assets/{simmer-logo.png, favicon*.png,
                  favicon.ico, favicon.svg}

Run this after replacing the master logo; never hand-edit the outputs.

    python3 tools/simmer_icons.py

Two things it does that a plain resize does not:

  1. Crops to the mark. The master has ~25% dead padding on every side. Scaled
     straight down, the mark occupies ~9px of a 16px favicon and reads as a
     smudge. We find the mark's bounding box against the background colour and
     crop a square around it.
  2. Rounds the corners, matching the rx=10/64 of the mark this replaced, with a
     supersampled mask so the curve stays clean at 16px.
"""
from __future__ import annotations

import base64
import io
import os
import sys

from PIL import Image, ImageDraw

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships with the toolchain
    sys.exit("needs numpy: pip install numpy")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "simmer/ui/assets/facades_simmer_logo_mark.png")
OUT = os.path.join(ROOT, "simmer/ui/static/assets")

# Header lockup renders at 3rem (48px); 192 covers a 4x display and keeps the
# file small. The glow gradients compress poorly, so oversizing is expensive.
LOGO_PX = 192
RADIUS_FRAC = 0.16  # matches the previous mark's rx=10 on a 64 viewBox
# Square crop, as a multiple of the mark's long edge. Optical sizing: small
# icons crop tight so the thin neon strokes fill the tile and stay legible at
# 16px, while the large mark keeps room for its glow to breathe.
PAD_SMALL = 1.04    # favicons <= 32px
PAD_LARGE = 1.20    # everything above
SVG_EMBED_PX = 64   # favicon.svg embeds a PNG this size; it loads on every page


def mark_bbox(im: Image.Image, tol: float = 12.0) -> tuple[int, int, int, int]:
    """Bounding box of the artwork, measured against the corner background."""
    a = np.asarray(im.convert("RGB")).astype(int)
    bg = a[0:40, 0:40].reshape(-1, 3).mean(0)
    ys, xs = np.where(np.sqrt(((a - bg) ** 2).sum(2)) > tol)
    if not len(xs):
        raise SystemExit("no artwork found — is the logo a solid colour?")
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def square_crop(im: Image.Image, bbox: tuple[int, int, int, int], pad: float) -> Image.Image:
    """Square crop of `pad` x the mark's long edge, centred on it and clamped
    inside the canvas (Image.crop would otherwise pad with black)."""
    x0, y0, x1, y1 = bbox
    side = min(int(max(x1 - x0, y1 - y0) * pad), im.width, im.height)
    cx = min(max((x0 + x1) // 2, side // 2), im.width - side // 2)
    cy = min(max((y0 + y1) // 2, side // 2), im.height - side // 2)
    return im.crop((cx - side // 2, cy - side // 2, cx + side // 2, cy + side // 2))


def rounded(img: Image.Image, size: int) -> Image.Image:
    """Downscale to `size` and mask to a rounded square."""
    out = img.resize((size, size), Image.LANCZOS).convert("RGBA")
    r, ss = max(1, int(size * RADIUS_FRAC)), 8
    mask = Image.new("L", (size * ss, size * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size * ss - 1, size * ss - 1], radius=r * ss, fill=255
    )
    out.putalpha(mask.resize((size, size), Image.LANCZOS))
    return out


def main() -> None:
    if not os.path.exists(SRC):
        raise SystemExit(f"missing master logo: {SRC}")
    im = Image.open(SRC).convert("RGB")

    bbox = mark_bbox(im)
    large = square_crop(im, bbox, PAD_LARGE)
    small = square_crop(im, bbox, PAD_SMALL)
    print(f"mark bbox {bbox} -> large {large.size}, small {small.size}")

    def base_for(size: int) -> Image.Image:
        return small if size <= 32 else large

    def report(name: str) -> None:
        print(f"  {name:<18} {os.path.getsize(os.path.join(OUT, name)):>8,} B")

    for name, size in [
        ("simmer-logo.png", LOGO_PX),
        ("favicon-192.png", 192),
        ("favicon-32.png", 32),
        ("favicon-16.png", 16),
    ]:
        rounded(base_for(size), size).save(os.path.join(OUT, name), optimize=True)
        report(name)

    # .ico carries 16/32/48 so each context picks its own size.
    # Every embedded size here is <= 48, so use the tight crop throughout.
    rounded(small, 64).save(
        os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)]
    )
    report("favicon.ico")

    # The mark is raster, so favicon.svg wraps a small PNG to keep the
    # image/svg+xml <link> in app.html working and pixel-identical to the rest.
    # Tight crop: this renders in the browser tab at 16-32px, not as a big mark.
    buf = io.BytesIO()
    rounded(small, SVG_EMBED_PX).save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode()
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        'role="img" aria-label="Simmer">\n'
        "  <!-- Facades Simmer logomark. Generated — do not hand-edit.\n"
        "       Source: simmer/ui/assets/facades_simmer_logo_mark.png\n"
        "       Regenerate: python3 tools/simmer_icons.py -->\n"
        f'  <image width="64" height="64" href="data:image/png;base64,{b64}"/>\n'
        "</svg>\n"
    )
    with open(os.path.join(OUT, "favicon.svg"), "w", encoding="utf-8") as fh:
        fh.write(svg)
    report("favicon.svg")


if __name__ == "__main__":
    main()
