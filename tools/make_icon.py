#!/usr/bin/env python3
"""Draw the Parity app icon and pack it into an .icns.

No image libraries: the artwork is computed per pixel with analytic
anti-aliasing and written as a PNG by hand, then macOS's own ``sips`` and
``iconutil`` produce the multi-resolution ``.icns``.

    python3 tools/make_icon.py --out build/icon
    python3 tools/make_icon.py --preview        # the mark as text, to eyeball it

The motif is the name: a ring cut into two equal halves, counter-rotated a few
degrees apart and coloured as a pair -- cyan left, coral right. Two halves that
are equal and only just out of alignment is what a harmonic mix is, and it is
what "parity" means.
"""

from __future__ import annotations

import argparse
import math
import struct
import subprocess
import sys
import zlib
from pathlib import Path

CYAN = (0x4C, 0xC6, 0xF0)
CYAN_LIGHT = (0xA8, 0xE6, 0xFF)
CORAL = (0xFF, 0x8A, 0x65)
CORAL_LIGHT = (0xFF, 0xC4, 0xAC)
BG_TOP = (0x1B, 0x23, 0x34)
BG_BOTTOM = (0x08, 0x0A, 0x0F)

# The mark, as fractions of the icon's width.
RING_MID = 0.300   # centreline radius of the ring
RING_THICK = 0.098  # stroke thickness
SEAM_GAP = 0.030    # half the gap between the two halves, across the cut
SEAM_TILT = 22.0    # degrees the cut is turned away from vertical

ICONSET_SIZES = (
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
)


def clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def coverage(distance: float, aa: float = 1.0) -> float:
    """Anti-aliased coverage from a signed distance (negative = inside)."""
    return clamp01(0.5 - distance / aa)


def mix(a, b, t: float) -> tuple[float, float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def over(dst, src, alpha: float):
    if alpha <= 0.0:
        return dst
    if alpha >= 1.0:
        return src
    return (
        dst[0] + (src[0] - dst[0]) * alpha,
        dst[1] + (src[1] - dst[1]) * alpha,
        dst[2] + (src[2] - dst[2]) * alpha,
    )


def sd_round_rect(px: float, py: float, cx: float, cy: float, hw: float, hh: float, r: float) -> float:
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    ax, ay = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ax, ay) + min(max(qx, qy), 0.0) - r


def rotate(dx: float, dy: float, degrees: float) -> tuple[float, float]:
    """``(dx, dy)`` turned by ``degrees`` clockwise on screen."""
    angle = math.radians(degrees)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a


def render(size: int) -> bytes:
    """Return RGBA bytes for a ``size`` x ``size`` icon."""
    cx = cy = size / 2.0
    pad = size * 0.055
    hw = hh = size / 2.0 - pad
    radius = size * 0.225

    ring_mid = size * RING_MID
    ring_inner = ring_mid - size * RING_THICK / 2.0
    ring_outer = ring_mid + size * RING_THICK / 2.0
    seam = size * SEAM_GAP

    # One cut, tilted off square, leaving the same amount of ring on each side:
    # two equal halves. (fill, stroke colour, its lighter edge)
    halves = (
        (True, CYAN, CYAN_LIGHT),
        (False, CORAL, CORAL_LIGHT),
    )

    out = bytearray(size * size * 4)
    offset = 0
    for y in range(size):
        py = y + 0.5
        shade = y / max(1, size - 1)
        bg = mix(BG_TOP, BG_BOTTOM, shade)
        for x in range(size):
            px = x + 0.5

            shape = sd_round_rect(px, py, cx, cy, hw, hh, radius)
            bg_cov = coverage(shape)
            if bg_cov <= 0.0:
                offset += 4
                continue

            # A soft light from the top-left, so the tile is not flat.
            lifted = mix(bg, (0x2C, 0x3A, 0x55), clamp01((1.0 - (px + py) / size) * 0.35))
            colour = lifted

            dx, dy = px - cx, py - cy
            lx, ly = rotate(dx, dy, SEAM_TILT)
            dist = math.hypot(lx, ly)
            radial = max(ring_inner - dist, dist - ring_outer)
            if radial < 1.5:
                for is_left, stroke, light in halves:
                    # Both halves stop ``seam`` short of the same cut, so the gap
                    # between them is even all the way along it.
                    along = lx + seam if is_left else seam - lx
                    cov = coverage(max(radial, along))
                    if cov <= 0.0:
                        continue
                    depth = clamp01((dist - ring_inner) / max(1.0, ring_outer - ring_inner))
                    colour = over(colour, mix(stroke, light, depth * 0.55), cov)

            out[offset] = int(colour[0] + 0.5)
            out[offset + 1] = int(colour[1] + 0.5)
            out[offset + 2] = int(colour[2] + 0.5)
            out[offset + 3] = int(bg_cov * 255.0 + 0.5)
            offset += 4
    return bytes(out)


def preview(size: int = 44) -> None:
    """Print the mark as text, so the shape can be checked without looking at it."""
    rgba = render(size)
    for y in range(size):
        row = []
        for x in range(size):
            i = (y * size + x) * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            if a < 24:
                row.append(" ")
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            if mx - mn < 34:
                row.append("·" if mx > 90 else ".")   # the tile's own shading
            elif b > r:
                row.append("C" if mx > 150 else "c")   # the cyan half
            else:
                row.append("O" if mx > 150 else "o")   # the coral half
        print("".join(row))
    print(f"-- {(1 / size):.3%} per cell, {size}x{size}")


def write_png(path: Path, size: int, rgba: bytes) -> None:
    stride = size * 4
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter: none
        raw.extend(rgba[y * stride : (y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def run(*command: str) -> None:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"{command[0]} failed: {result.stderr.strip()}")


def build_icns(out_dir: Path, master: int = 1024) -> tuple[Path, Path]:
    """Render the icon, expand it to every size, and pack an .icns."""
    out_dir.mkdir(parents=True, exist_ok=True)
    master_png = out_dir / "Parity.png"
    write_png(master_png, master, render(master))

    iconset = out_dir / "Parity.iconset"
    iconset.mkdir(exist_ok=True)
    for name, size in ICONSET_SIZES:
        target = iconset / name
        if size == master:
            target.write_bytes(master_png.read_bytes())
        else:
            run("sips", "-z", str(size), str(size), str(master_png), "--out", str(target))

    icns = out_dir / "Parity.icns"
    run("iconutil", "-c", "icns", str(iconset), "-o", str(icns))
    return icns, master_png


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the Parity .icns icon.")
    parser.add_argument("--out", default="build/icon", help="output directory")
    parser.add_argument("--size", type=int, default=1024, help="master render size")
    parser.add_argument("--preview", action="store_true", help="print the mark as text and stop")
    args = parser.parse_args()

    if args.preview:
        preview()
        return 0

    if sys.platform != "darwin":
        print("This icon packer uses sips/iconutil, so it needs macOS.", file=sys.stderr)
        return 1

    icns, png = build_icns(Path(args.out).expanduser(), args.size)
    print(f"wrote {icns} ({icns.stat().st_size / 1024:.0f} KB) and {png}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
