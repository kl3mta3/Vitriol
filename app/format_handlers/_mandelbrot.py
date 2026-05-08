"""Mandelbrot-derived XOR keystream for cross-category Stone image outputs.

When a Stone-mode cross-category conversion outputs to an image target
(PNG, BMP), the v2 pixel byte stream is XOR'd with a keystream generated
from this module. Each output image becomes a unique deterministic fractal
portrait of its source — same source always produces the same image,
different sources land in different regions of the Mandelbrot set.

This is a presentation feature, not steganography: the keystream is
derived from public envelope dimensions. Anyone with Vitriol can recover
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

# Lift Pillow's "decompression bomb" guard. k=1 Stone images for big
# sources can exceed Pillow's default 89 MP cap (a 100 MB source maps
# to ~16K x 16K). We produced the image ourselves and trust it.
try:
    from PIL import Image as _PIL_Image_Guard
    _PIL_Image_Guard.MAX_IMAGE_PIXELS = None
except ImportError:
    pass


# Color palette is a sum of three sin-waves on the iteration count.
# Frequencies chosen so they don't synchronize → R, G, B diverge and the
# fractal renders in saturated color cycles instead of grayscale.
_PALETTE_R_FREQ = 0.025
_PALETTE_G_FREQ = 0.018
_PALETTE_B_FREQ = 0.013

_TAU = 2.0 * 3.141592653589793

# Curated set of 64 hand-picked Mandelbrot viewports. Each is
# (center_x, center_y, half_width). All chosen to land squarely on the
# boundary of the set — the only region where iteration counts vary
# enough to look like a fractal. Per-source variety comes from picking
# one of these by hash byte, then jittering position + colors.
_VIEWPORTS = (
    # Whole-set + wide-field views.
    (-0.5, 0.0, 1.5), (-0.7, 0.0, 1.4), (-0.5, 0.5, 0.7),
    (-0.5, -0.5, 0.7),
    # Cardioid edge zooms.
    (0.28, 0.01, 0.06), (0.275, -0.01, 0.05), (-0.235, 0.0, 0.05),
    (-0.4, 0.6, 0.18), (-0.1, 0.65, 0.15), (-0.1, 0.85, 0.2),
    (-0.235, 0.625, 0.04), (0.36, 0.1, 0.04), (0.34, 0.05, 0.06),
    (-0.69, 0.31, 0.06),
    # Period-bulb boundaries.
    (-1.25, 0.0, 0.15), (-1.305, 0.0, 0.04), (-1.255, 0.045, 0.025),
    (-0.125, 0.745, 0.05), (-0.158, 1.033, 0.012), (-0.16, 1.04, 0.04),
    (-1.401155, 0.0, 0.02), (-1.476, 0.0, 0.012), (-0.747, 0.105, 0.018),
    (-1.39, 0.005, 0.025),
    # Filaments + antennas.
    (-1.7689, 0.0, 0.012), (-1.99, 0.0, 0.005), (-1.985, 0.0, 0.008),
    (-1.93, 0.0, 0.014), (-1.85, 0.0, 0.03), (-1.6735, 0.0006, 0.0015),
    (-1.4002, 0.0, 0.005), (-1.4, 0.0, 0.025), (-0.74, 0.21, 0.022),
    (-0.745, 0.186, 0.04),
    # Seahorse / spiral valleys.
    (-0.745, 0.113, 0.012), (-0.7445, 0.1217, 0.005),
    (-0.7440, 0.1245, 0.0014), (-0.7269, 0.1889, 0.025),
    (-0.748, 0.085, 0.05), (-0.748, 0.0975, 0.025),
    (-0.756, 0.07, 0.013), (-0.74, 0.205, 0.018),
    (-0.7475, 0.115, 0.0075), (-0.7269, 0.18, 0.012),
    # Mini-Mandelbrots.
    (-1.7493, 0.000, 0.0015), (-1.62917, 0.0, 0.0025),
    (-0.15891, 1.03244, 0.0035), (-0.10109637, 0.95628651, 0.001),
    (-1.985409, 0.0, 0.0008), (0.359, 0.0865, 0.012),
    (-1.7396, 0.0, 0.005), (-0.16, 1.04, 0.005),
    # Misiurewicz points.
    (-0.77568377, 0.13646737, 0.005), (-0.1011, 0.9563, 0.001),
    (-1.543689, 0.0, 0.0025), (-0.7752, 0.1361, 0.0025),
    (-1.401155, 0.0, 0.0025),
    # Custom / miscellany.
    (-0.7440, 0.1340, 0.005), (-0.6840039, 0.4604141, 0.005),
    (0.3736, 0.0917, 0.012), (-1.0, 0.275, 0.04),
    (-0.95, 0.265, 0.025), (-1.07, 0.265, 0.03),
    (-0.633, 0.4, 0.05), (-0.6905, 0.379, 0.018),
)
assert len(_VIEWPORTS) >= 64, f"viewport pool must be ≥64, got {len(_VIEWPORTS)}"

# Whole-set viewport used as the safety-net fallback when the source-picked
# viewport lands in an all-uniform region (rare).
_FALLBACK_VIEWPORT = (-0.5, 0.0, 1.5)


# Jitter range as fraction of the viewport's half_width. 1.2 means jitter
# can shift the center by up to ±60% of the viewport — two same-viewport
# sources land in clearly different sub-regions instead of nearly identical.
_JITTER_RANGE = 1.2

# Number of palette algorithms (see _palette_dispatch).
_NUM_PALETTES = 6


def derive_seed(magic_bytes: bytes
                 ) -> Tuple[float, float, float, float, float, float, int]:
    """Hash the envelope header into a deterministic seed:
      - viewport + jitter (cx, cy, half_width)
      - per-source palette phases for R, G, B
      - palette algorithm id (0..5)

    Same source → same seed → same fractal + colors. Different sources
    pick different viewports from the curated table, larger jitter shifts
    them within the chosen viewport, and the palette algorithm id selects
    one of six color-cycling schemes.
    """
    h = hashlib.sha256(magic_bytes).digest()
    idx = h[0] % len(_VIEWPORTS)
    cx, cy, hw = _VIEWPORTS[idx]
    jx = (struct.unpack(">Q", h[8:16])[0] / float(1 << 64) - 0.5) * hw * _JITTER_RANGE
    jy = (struct.unpack(">Q", h[16:24])[0] / float(1 << 64) - 0.5) * hw * _JITTER_RANGE
    r_phase = (h[24] / 255.0) * _TAU
    g_phase = (h[25] / 255.0) * _TAU
    b_phase = (h[26] / 255.0) * _TAU
    palette_id = h[27] % _NUM_PALETTES
    return (cx + jx, cy + jy, hw, r_phase, g_phase, b_phase, palette_id)


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


def _palette_three_sin(n: np.ndarray, r_phase: float, g_phase: float,
                        b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Original: three independent sin-waves on iteration count. Saturated
    cycling colors with no synchronization between channels."""
    r = (np.sin(n * _PALETTE_R_FREQ + r_phase) * 127.0 + 128.0)
    g = (np.sin(n * _PALETTE_G_FREQ + g_phase) * 127.0 + 128.0)
    b = (np.sin(n * _PALETTE_B_FREQ + b_phase) * 127.0 + 128.0)
    return r, g, b


def _palette_hsv_cycle(n: np.ndarray, r_phase: float, g_phase: float,
                        b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """HSV cycle: hue runs through the spectrum with iteration count;
    saturation + value pulse based on phase shifts. Vivid rainbow look."""
    hue = (n * 0.012 + r_phase / _TAU) % 1.0
    sat = 0.7 + 0.3 * np.sin(n * 0.04 + g_phase)
    val = 0.7 + 0.3 * np.cos(n * 0.025 + b_phase)
    np.clip(sat, 0.0, 1.0, out=sat)
    np.clip(val, 0.0, 1.0, out=val)
    # HSV → RGB (vectorized)
    h6 = hue * 6.0
    i = np.floor(h6).astype(np.int32) % 6
    f = h6 - np.floor(h6)
    p = val * (1.0 - sat)
    q = val * (1.0 - sat * f)
    t = val * (1.0 - sat * (1.0 - f))
    r = np.where(i == 0, val, np.where(i == 1, q, np.where(i == 2, p,
            np.where(i == 3, p, np.where(i == 4, t, val)))))
    g = np.where(i == 0, t, np.where(i == 1, val, np.where(i == 2, val,
            np.where(i == 3, q, np.where(i == 4, p, p)))))
    b = np.where(i == 0, p, np.where(i == 1, p, np.where(i == 2, t,
            np.where(i == 3, val, np.where(i == 4, val, q)))))
    return r * 255.0, g * 255.0, b * 255.0


def _palette_two_color(n: np.ndarray, r_phase: float, g_phase: float,
                        b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Smooth gradient between two complementary anchor colors derived
    from phase. Iteration count drives the interpolation parameter."""
    # Anchor 1 from r_phase, g_phase; anchor 2 is its complement.
    a1_r = 64 + 191 * (np.sin(r_phase) * 0.5 + 0.5)
    a1_g = 64 + 191 * (np.sin(g_phase) * 0.5 + 0.5)
    a1_b = 64 + 191 * (np.sin(b_phase) * 0.5 + 0.5)
    a2_r = 255.0 - a1_r
    a2_g = 255.0 - a1_g
    a2_b = 255.0 - a1_b
    t = (np.sin(n * 0.03 + r_phase) * 0.5 + 0.5)
    r = a1_r * (1.0 - t) + a2_r * t
    g = a1_g * (1.0 - t) + a2_g * t
    b = a1_b * (1.0 - t) + a2_b * t
    return r, g, b


def _palette_three_anchor(n: np.ndarray, r_phase: float, g_phase: float,
                           b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Triangular interpolation between three hash-derived anchor colors.
    Produces a banded, painterly look."""
    a1 = (96 + 159 * np.sin(r_phase),
          96 + 159 * np.sin(g_phase + 1.0),
          96 + 159 * np.sin(b_phase + 2.0))
    a2 = (96 + 159 * np.sin(r_phase + 2.0),
          96 + 159 * np.sin(g_phase),
          96 + 159 * np.sin(b_phase + 4.0))
    a3 = (96 + 159 * np.sin(r_phase + 4.0),
          96 + 159 * np.sin(g_phase + 2.0),
          96 + 159 * np.sin(b_phase))
    # Three-way blend driven by two phase-shifted sine waves.
    s1 = (np.sin(n * 0.025 + r_phase) * 0.5 + 0.5)
    s2 = (np.sin(n * 0.018 + g_phase + 1.5) * 0.5 + 0.5)
    w1 = s1
    w2 = (1.0 - s1) * s2
    w3 = (1.0 - s1) * (1.0 - s2)
    r = w1 * a1[0] + w2 * a2[0] + w3 * a3[0]
    g = w1 * a1[1] + w2 * a2[1] + w3 * a3[1]
    b = w1 * a1[2] + w2 * a2[2] + w3 * a3[2]
    return r, g, b


def _palette_log_ramp(n: np.ndarray, r_phase: float, g_phase: float,
                       b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Logarithmic ramp through one anchor color, with brighter highlights
    near the boundary. Fire-style or ice-style depending on phase."""
    safe = np.maximum(n, 1.0)
    t = np.log(safe) / np.log(256.0)   # in [0, 1]
    anchor_r = 128 + 127 * np.sin(r_phase)
    anchor_g = 128 + 127 * np.sin(g_phase)
    anchor_b = 128 + 127 * np.sin(b_phase)
    r = t * anchor_r + (1.0 - t) * 16.0
    g = t * anchor_g + (1.0 - t) * 16.0
    b = t * anchor_b + (1.0 - t) * 16.0
    return r, g, b


def _palette_inverted(n: np.ndarray, r_phase: float, g_phase: float,
                       b_phase: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Inverted three-sin: light fractal exterior, dark anchor in the body
    region. Looks like an X-ray or photonegative of the standard view."""
    r = 255.0 - (np.sin(n * _PALETTE_R_FREQ + r_phase) * 127.0 + 128.0)
    g = 255.0 - (np.sin(n * _PALETTE_G_FREQ + g_phase) * 127.0 + 128.0)
    b = 255.0 - (np.sin(n * _PALETTE_B_FREQ + b_phase) * 127.0 + 128.0)
    return r, g, b


_PALETTES = (
    _palette_three_sin,
    _palette_hsv_cycle,
    _palette_two_color,
    _palette_three_anchor,
    _palette_log_ramp,
    _palette_inverted,
)


def derive_seed_unjittered(magic_bytes: bytes
                            ) -> Tuple[float, float, float, float, float, float, int]:
    """Like `derive_seed` but skips the per-source viewport jitter — the
    curated viewport's exact pre-tuned center is used. The jitter is
    great for one-shot images (it varies same-viewport sources) but is
    a liability for video, where it can push the centre off the
    boundary into a uniform region. The video pipeline picks a base
    viewport once via this function so every frame in the clip uses the
    exact same pre-curated centre."""
    h = hashlib.sha256(magic_bytes).digest()
    idx = h[0] % len(_VIEWPORTS)
    cx, cy, hw = _VIEWPORTS[idx]
    r_phase = (h[24] / 255.0) * _TAU
    g_phase = (h[25] / 255.0) * _TAU
    b_phase = (h[26] / 255.0) * _TAU
    palette_id = h[27] % _NUM_PALETTES
    return (cx, cy, hw, r_phase, g_phase, b_phase, palette_id)


def viewport_is_interesting(width: int, height: int,
                             cx: float, cy: float, hw: float) -> bool:
    """Quick boundary check: does this viewport land on the Mandelbrot
    boundary (a healthy mix of inside-set + escape-fast pixels)?

    Used by the video pipeline to validate the base viewport ONCE
    before rendering 300+ frames at it — if the curated seed jittered
    off into a uniform region, the video would otherwise be a solid
    color throughout. Cheap (one tiny Mandelbrot iter at e.g. 128×128)."""
    iter_count = _mandelbrot_iter_count(width, height, (cx, cy, hw))
    inside_fraction = float((iter_count >= 255).sum()) / iter_count.size
    return 0.001 <= inside_fraction <= 0.92


def generate_keystream(width: int, height: int, seed,
                       safety_net: bool = True) -> bytes:
    """Generate a width*height*3 byte RGB keystream rendering a colored
    Mandelbrot fractal. The fractal occupies the full image (no tiling).

    `seed` is the 7-tuple (cx, cy, half_width, r_phase, g_phase, b_phase,
    palette_id) returned by `derive_seed`. The first three drive the
    Mandelbrot iteration; the next three set palette colors; the last
    selects which palette algorithm to use.

    `safety_net=True` (default, for image use): if the rendered fractal
    is too uniform (all-inside or all-outside the set), silently swap to
    a fallback whole-set view so we never produce a solid-color image.
    The fallback's offsets depend on the input center, which is fine
    for one-shot images but causes per-frame viewport jumps when
    rendering animated video. Video should pass `safety_net=False` and
    pre-validate the base viewport via `viewport_is_interesting`.

    For images larger than _FRACTAL_CAP, the fractal is computed at the
    capped resolution and Pillow-resized up.
    """
    if len(seed) >= 7:
        center_x, center_y, half_width, r_phase, g_phase, b_phase, palette_id = seed
    elif len(seed) >= 6:
        center_x, center_y, half_width, r_phase, g_phase, b_phase = seed
        palette_id = 0
    else:
        # Backward compat: 3-tuple seed.
        center_x, center_y, half_width = seed[:3]
        r_phase, g_phase, b_phase = 0.0, 1.7, 3.3
        palette_id = 0

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

    # Safety net: regenerate at fallback whole-set view if the source-
    # picked viewport landed in an all-uniform region. Disabled by the
    # video path (see docstring): the fallback's per-call offsets cause
    # mid-clip viewport jumps when called per-frame.
    if safety_net:
        inside_fraction = float((iter_count >= 255).sum()) / iter_count.size
        if inside_fraction > 0.92 or inside_fraction < 0.001:
            fb_cx, fb_cy, fb_hw = _FALLBACK_VIEWPORT
            iter_count = _mandelbrot_iter_count(
                comp_w, comp_h, (fb_cx + (center_x % 0.3) - 0.15,
                                  fb_cy + (center_y % 0.2) - 0.1,
                                  fb_hw))

    n = iter_count.astype(np.float64)
    palette_fn = _PALETTES[palette_id % len(_PALETTES)]
    r, g, b = palette_fn(n, r_phase, g_phase, b_phase)

    # Pixels INSIDE the set get black so the fractal body silhouette is
    # always recognizable, regardless of which palette was used.
    inside = (iter_count >= 255)
    r = np.where(inside, 0.0, r)
    g = np.where(inside, 0.0, g)
    b = np.where(inside, 0.0, b)

    rgb = np.empty((comp_h, comp_w, 3), dtype=np.uint8)
    rgb[..., 0] = np.clip(r, 0, 255).astype(np.uint8)
    rgb[..., 1] = np.clip(g, 0, 255).astype(np.uint8)
    rgb[..., 2] = np.clip(b, 0, 255).astype(np.uint8)

    if (comp_w, comp_h) != (width, height):
        from PIL import Image as _PIL
        img = _PIL.frombuffer("RGB", (comp_w, comp_h), rgb.tobytes(),
                                "raw", "RGB", 0, 1)
        img = img.resize((width, height), _PIL.Resampling.BILINEAR)
        return img.tobytes()
    return rgb.tobytes()
