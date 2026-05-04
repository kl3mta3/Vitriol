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


# Color palette is a sum of three sin-waves on the iteration count.
# Frequencies chosen so they don't synchronize → R, G, B diverge and the
# fractal renders in saturated color cycles instead of grayscale.
_PALETTE_R_FREQ = 0.025
_PALETTE_G_FREQ = 0.018
_PALETTE_B_FREQ = 0.013

_TAU = 2.0 * 3.141592653589793

# Curated set of hand-picked Mandelbrot viewports. Each is
# (center_x, center_y, half_width). All chosen to land squarely on the
# boundary of the set — the only region where iteration counts vary
# enough to look like a fractal. Per-source variety comes from picking
# one of these by hash byte, then jittering position + colors.
_VIEWPORTS = (
    (-0.5, 0.0, 1.5),                                  # Whole-set view
    (-0.745, 0.113, 0.012),                            # Seahorse Valley
    (-0.7269, 0.1889, 0.025),                          # Triple Spiral Valley
    (0.28, 0.01, 0.06),                                # Elephant Valley
    (-1.7689, 0.0, 0.012),                             # Mini-Mandelbrot island chain
    (-1.25, 0.0, 0.15),                                # Period-2 bulb boundary
    (-0.745, 0.186, 0.04),                             # Spiral arm filament
    (-0.16, 1.04, 0.04),                               # Top antenna
    (-1.401155, 0.0, 0.02),                            # Boundary near satellite bulb
    (-0.748, 0.085, 0.05),                             # Curl above seahorse
    (-0.5, 0.5, 0.7),                                  # Wide-field upper boundary
    (-0.5, -0.5, 0.7),                                 # Wide-field lower boundary
    (-0.77568377, 0.13646737, 0.005),                  # Misiurewicz point neighborhood
    (-0.1, 0.85, 0.2),                                 # Boundary above main cardioid
    (-0.125, 0.745, 0.05),                             # Period-3 bulb
    (-0.16070, 1.0375, 0.003),                         # Filament near top
)

# Whole-set viewport used as the safety-net fallback when the source-picked
# viewport lands in an all-uniform region (rare).
_FALLBACK_VIEWPORT = (-0.5, 0.0, 1.5)


def derive_seed(magic_bytes: bytes) -> Tuple[float, float, float, float, float, float]:
    """Hash the envelope header into a deterministic seed:
      - viewport + jitter (cx, cy, half_width)
      - per-source palette phases for R, G, B

    Same source → same seed → same fractal + colors. Different sources
    pick different viewports from the curated table, with extra jitter
    on position and a unique palette phase per channel.
    """
    h = hashlib.sha256(magic_bytes).digest()
    idx = h[0] % len(_VIEWPORTS)
    cx, cy, hw = _VIEWPORTS[idx]
    jx = (struct.unpack(">Q", h[8:16])[0] / float(1 << 64) - 0.5) * hw * 0.4
    jy = (struct.unpack(">Q", h[16:24])[0] / float(1 << 64) - 0.5) * hw * 0.4
    r_phase = (h[24] / 255.0) * _TAU
    g_phase = (h[25] / 255.0) * _TAU
    b_phase = (h[26] / 255.0) * _TAU
    return (cx + jx, cy + jy, hw, r_phase, g_phase, b_phase)


# Cap the actual Mandelbrot computation at this dim. For larger output
# images, generate at this size and Pillow-resize. The fractal is purely
# decorative — pixel-perfect detail at multi-megapixel resolutions isn't
# worth the 30+ second cost.
_FRACTAL_CAP = 1080


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
                       seed) -> bytes:
    """Generate a width*height*3 byte RGB keystream rendering a colored
    Mandelbrot fractal. The fractal occupies the full image (no tiling).

    `seed` is the 6-tuple (cx, cy, half_width, r_phase, g_phase, b_phase)
    returned by `derive_seed`. The first three drive the Mandelbrot
    iteration; the last three set per-source palette colors.

    For images larger than _FRACTAL_CAP, the fractal is computed at the
    capped resolution and Pillow-resized up. ~50× speedup on huge images;
    the only loss is sub-pixel fractal detail, which doesn't matter for
    a decorative cover image.
    """
    if len(seed) >= 6:
        center_x, center_y, half_width, r_phase, g_phase, b_phase = seed
    else:
        # Backward compat: 3-tuple seed (no palette phases).
        center_x, center_y, half_width = seed[:3]
        r_phase, g_phase, b_phase = 0.0, 1.7, 3.3

    # Compute at capped resolution, then resize.
    if max(width, height) > _FRACTAL_CAP:
        if width >= height:
            comp_w = _FRACTAL_CAP
            comp_h = max(1, int(round(_FRACTAL_CAP * height / width)))
        else:
            comp_h = _FRACTAL_CAP
            comp_w = max(1, int(round(_FRACTAL_CAP * width / height)))
    else:
        comp_w, comp_h = width, height

    iter_count = _mandelbrot_iter_count(
        comp_w, comp_h, (center_x, center_y, half_width))

    # Safety net: if the source-picked viewport landed in an all-uniform
    # region (entirely inside or entirely outside the set, e.g. all-black
    # or all-flat-color), regenerate at the fallback whole-set view so
    # we always emit a recognizable fractal. Cheap to detect: the
    # iter_count is tiny (~1080² uint8 = 1.1 MB) and unique() is fast.
    inside_fraction = float((iter_count >= 255).sum()) / iter_count.size
    if inside_fraction > 0.92 or inside_fraction < 0.001:
        # The chosen viewport is bad — fall back to the always-good
        # whole-set view, but keep the per-source palette phases so two
        # bad-viewport sources still produce different-colored outputs.
        # Apply a tiny per-source position jitter (derived from the
        # original bad center) so multiple fallbacks aren't identical.
        fb_cx, fb_cy, fb_hw = _FALLBACK_VIEWPORT
        iter_count = _mandelbrot_iter_count(
            comp_w, comp_h, (fb_cx + (center_x % 0.3) - 0.15,
                              fb_cy + (center_y % 0.2) - 0.1,
                              fb_hw))
    n = iter_count.astype(np.float64)
    r = (np.sin(n * _PALETTE_R_FREQ + r_phase) * 127.0 + 128.0)
    g = (np.sin(n * _PALETTE_G_FREQ + g_phase) * 127.0 + 128.0)
    b = (np.sin(n * _PALETTE_B_FREQ + b_phase) * 127.0 + 128.0)
    inside = (iter_count >= 255)
    r[inside] = 0.0
    g[inside] = 0.0
    b[inside] = 0.0
    rgb = np.empty((comp_h, comp_w, 3), dtype=np.uint8)
    rgb[..., 0] = np.clip(r, 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(g, 0, 255).astype(np.uint8)
    rgb[..., 2] = np.clip(b, 0, 255).astype(np.uint8)

    # If we computed at a capped dim, scale up to the requested size.
    # BILINEAR keeps the fractal silhouette smooth without producing
    # blocky stair-steps; cost ~50ms even for 4500x4500.
    if (comp_w, comp_h) != (width, height):
        from PIL import Image as _PIL
        img = _PIL.frombuffer("RGB", (comp_w, comp_h), rgb.tobytes(),
                                "raw", "RGB", 0, 1)
        img = img.resize((width, height), _PIL.Resampling.BILINEAR)
        return img.tobytes()
    return rgb.tobytes()
