"""Mandelbrot-derived XOR keystream for cross-category Stone image outputs.

When a Stone-mode cross-category conversion outputs to an image target
(PNG, BMP), the v2 pixel byte stream is XOR'd with a keystream generated
from this module. Each output image becomes a unique deterministic fractal
portrait of its source — same source always produces the same image,
different sources land in different regions of the Mandelbrot set.

This is a presentation feature, not steganography: the keystream is
derived from public envelope dimensions. Anyone with Transmute can recover
the original source.

Implementation uses NumPy for vectorized iteration, generating the full
image-size keystream in ~0.3-0.6 sec at 1080². Output is RGB (3 bytes
per pixel) with smooth color cycling driven by iteration count, so the
fractal is visible even when XOR'd with dense payload data.
"""
from __future__ import annotations
import hashlib
import struct
from typing import Tuple

import numpy as np


# A small hand-curated set of viewports. Each is (center_x, center_y,
# half_width). All chosen to land squarely on the boundary of the
# Mandelbrot set — the only region where iteration count varies enough
# to look like a fractal instead of a uniform field.
_VIEWPORTS = (
    # Whole-set view (the classic apple-and-bug shape).
    (-0.5, 0.0, 1.5),
    # Seahorse Valley.
    (-0.745, 0.113, 0.012),
    # Triple Spiral Valley.
    (-0.7269, 0.1889, 0.025),
    # Elephant Valley boundary.
    (0.28, 0.01, 0.06),
    # Mini-Mandelbrot island chain.
    (-1.7689, 0.0, 0.012),
    # Period-2 bulb boundary.
    (-1.25, 0.0, 0.15),
    # Spiral arm filament.
    (-0.745, 0.186, 0.04),
    # Top antenna boundary region.
    (-0.16, 1.04, 0.04),
    # Boundary near satellite bulb.
    (-1.401155, 0.0, 0.02),
    # Curl on boundary above seahorse.
    (-0.748, 0.085, 0.05),
    # Wide-field upper boundary.
    (-0.5, 0.5, 0.7),
    # Wide-field lower boundary.
    (-0.5, -0.5, 0.7),
    # Deep zoom near misiurewicz point.
    (-0.77568377, 0.13646737, 0.005),
    # Boundary above main cardioid.
    (-0.1, 0.85, 0.2),
    # Period-3 bulb.
    (-0.125, 0.745, 0.05),
    # Filament near top.
    (-0.16070, 1.0375, 0.003),
)

# Color palette phases. Three independent sin-wave phases over the
# iteration count produce smooth color cycling that contrasts strongly
# enough to remain visible after XOR with payload data. Phase offsets
# chosen so the three channels don't synchronize (otherwise the fractal
# would render as a grayscale gradient).
_PALETTE_R_FREQ = 0.025
_PALETTE_G_FREQ = 0.018
_PALETTE_B_FREQ = 0.013
_PALETTE_R_PHASE = 0.0
_PALETTE_G_PHASE = 1.7
_PALETTE_B_PHASE = 3.3


def derive_seed(magic_bytes: bytes) -> Tuple[float, float, float]:
    """Hash the envelope header to deterministically pick one of the
    curated Mandelbrot viewports plus a small jitter.

    Returns (center_x, center_y, half_width).
    """
    h = hashlib.sha256(magic_bytes).digest()
    idx = h[0] % len(_VIEWPORTS)
    cx, cy, hw = _VIEWPORTS[idx]
    jx = (struct.unpack(">Q", h[8:16])[0] / float(1 << 64) - 0.5) * hw * 0.3
    jy = (struct.unpack(">Q", h[16:24])[0] / float(1 << 64) - 0.5) * hw * 0.3
    return cx + jx, cy + jy, hw


def _mandelbrot_iter_count(width: int, height: int,
                            seed: Tuple[float, float, float],
                            max_iter: int = 255) -> np.ndarray:
    """Vectorized Mandelbrot iteration via NumPy. Returns a uint8 array
    of shape (height, width) with iteration count per pixel (0..max_iter).
    Pixels inside the set get max_iter.

    Uses split real/imaginary float32 arithmetic + squared-magnitude
    comparison (no sqrt, no complex dtype). Skips boolean-index copies
    by computing every pixel every iteration and recording only the
    first divergence step in `out`. Float32 is sufficient for the
    iteration counts we care about and roughly halves memory bandwidth
    vs. float64.
    """
    center_x, center_y, half_width = seed
    aspect = height / float(width) if width > 0 else 1.0
    half_height = half_width * aspect
    cr_axis = np.linspace(center_x - half_width, center_x + half_width,
                           width, dtype=np.float32)
    ci_axis = np.linspace(center_y - half_height, center_y + half_height,
                           height, dtype=np.float32)
    cr = np.broadcast_to(cr_axis[None, :], (height, width)).copy()
    ci = np.broadcast_to(ci_axis[:, None], (height, width)).copy()
    zr = np.zeros((height, width), dtype=np.float32)
    zi = np.zeros((height, width), dtype=np.float32)
    out = np.full((height, width), max_iter, dtype=np.uint8)
    not_done = np.ones((height, width), dtype=bool)
    # Diverged pixels (escape iter > 4) keep iterating in this loop and
    # eventually overflow float32. We don't care about their final z
    # values (we already recorded their iteration count) but the overflow
    # warnings are noisy. Suppress them.
    with np.errstate(over="ignore", invalid="ignore"):
        for n in range(max_iter):
            zr2 = zr * zr
            zi2 = zi * zi
            diverged = (zr2 + zi2 > 4.0) & not_done
            if diverged.any():
                out[diverged] = n
                not_done &= ~diverged
            # Always update every pixel; diverged-and-recorded pixels just
            # keep iterating harmlessly — we won't read them again. Skipping
            # the boolean-index copy is the speedup vs. masking.
            new_zi = zr * zi
            new_zi += new_zi  # 2*zr*zi
            new_zi += ci
            zr_new = zr2 - zi2 + cr
            zr = zr_new
            zi = new_zi
            # Cheap early exit: every 32 iterations check if anything is
            # still active (not_done is small, .any() is fast).
            if n & 31 == 31 and not not_done.any():
                break
    return out


def generate_keystream(width: int, height: int,
                       seed: Tuple[float, float, float]) -> bytes:
    """Generate a width*height*3 byte RGB keystream rendering a colored
    Mandelbrot fractal. The fractal occupies the full image (no tiling).
    Pixels inside the set are dark; pixels outside cycle through hue
    based on iteration count.

    The output is a flat bytes object, row-major, RGB-interleaved
    (R0,G0,B0, R1,G1,B1, ...) — directly compatible with the v2 pixel
    byte stream consumed by stream_png_write / stream_bmp_write.
    """
    iter_count = _mandelbrot_iter_count(width, height, seed)
    n = iter_count.astype(np.float64)
    # Three-phase sine palette. Frequencies and phases chosen so the
    # three channels diverge — fractal renders in saturated color cycles
    # rather than grayscale.
    r = (np.sin(n * _PALETTE_R_FREQ + _PALETTE_R_PHASE) * 127.0 + 128.0)
    g = (np.sin(n * _PALETTE_G_FREQ + _PALETTE_G_PHASE) * 127.0 + 128.0)
    b = (np.sin(n * _PALETTE_B_FREQ + _PALETTE_B_PHASE) * 127.0 + 128.0)
    # Pixels INSIDE the set (max iteration) get black so the fractal
    # body is recognizable as a silhouette.
    inside = (iter_count >= 255)
    r[inside] = 0.0
    g[inside] = 0.0
    b[inside] = 0.0
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[..., 0] = np.clip(r, 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(g, 0, 255).astype(np.uint8)
    rgb[..., 2] = np.clip(b, 0, 255).astype(np.uint8)
    return rgb.tobytes()
