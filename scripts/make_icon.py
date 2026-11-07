#!/usr/bin/env python
"""Render the LocalScript app icon -> localscript/static/icon.png and LocalScript.icns.

Developer tool: the outputs are committed, so you only need this if you change the design.
Needs numpy + scipy (both already dependencies) and macOS's `sips` / `iconutil`.
"""
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

OUT = Path(__file__).resolve().parent.parent / "localscript" / "static"
S, SS = 1024, 2          # final size, supersampling factor (anti-aliasing)
N = S * SS


def coverage(dist, aa=1.4):
    """Signed distance (pixels, negative inside) -> anti-aliased 0..1 coverage."""
    return np.clip(0.5 - dist / aa, 0.0, 1.0).astype(np.float32)


def rounded_rect(x, y, cx, cy, hw, hh, r):
    qx = np.abs(x - cx) - (hw - r)
    qy = np.abs(y - cy) - (hh - r)
    return np.hypot(np.maximum(qx, 0), np.maximum(qy, 0)) + np.minimum(np.maximum(qx, qy), 0) - r


def capsule(x, y, x0, x1, yc, r):
    px = np.clip(x, x0, x1)
    return np.hypot(x - px, y - yc) - r


def render() -> np.ndarray:
    k = SS
    y, x = (a.astype(np.float32) + 0.5 for a in np.mgrid[0:N, 0:N])
    c = N / 2
    half = 412 * k                                  # Apple's icon grid: an 824 px tile centred in 1024
    tile = rounded_rect(x, y, c, c, half, half, 185 * k)
    a_tile = coverage(tile)

    # soft drop shadow
    shadow = gaussian_filter(coverage(rounded_rect(x, y + 10 * k, c, c, half, half, 185 * k)), 14 * k) * 0.30

    # vertical gradient on the tile
    t = np.clip((y - (c - half)) / (2 * half), 0, 1)[..., None]
    top, bottom = np.array([255, 110, 94], np.float32) / 255, np.array([214, 55, 47], np.float32) / 255
    bg = top * (1 - t) + bottom * t

    # premultiplied compositing: shadow, tile, then the three text lines
    alpha = a_tile + shadow * (1 - a_tile)
    rgb = bg * a_tile[..., None]
    lines = np.zeros_like(a_tile)
    for yc, x1 in ((400, 734), (540, 734), (680, 590)):
        lines = np.maximum(lines, coverage(capsule(x, y, 290 * k, x1 * k, yc * k, 38 * k)))
    rgb = rgb * (1 - lines[..., None]) + lines[..., None]
    alpha = lines + alpha * (1 - lines)

    pm = np.concatenate([rgb, alpha[..., None]], axis=-1)            # premultiplied RGBA at N x N
    pm = pm.reshape(S, SS, S, SS, 4).mean(axis=(1, 3))               # box downsample
    a = pm[..., 3:4]
    straight = np.where(a > 1e-6, pm[..., :3] / np.maximum(a, 1e-6), 0)
    return (np.concatenate([straight, a], axis=-1).clip(0, 1) * 255 + 0.5).astype(np.uint8)


def write_png(path: Path, rgba: np.ndarray) -> None:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        master = tmp / "master.png"
        write_png(master, render())

        iconset = tmp / "LocalScript.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(master), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(OUT / "LocalScript.icns")], check=True)
        subprocess.run(["sips", "-z", "512", "512", str(master), "--out", str(OUT / "icon.png")],
                       check=True, capture_output=True)
        shutil.copy(master, tmp / "keep.png")
    print("wrote", OUT / "icon.png", "and", OUT / "LocalScript.icns")


if __name__ == "__main__":
    main()
