"""Conversion-time estimator used by the Verify Round-Trip warning.

Numbers are rough — refine after observing real runs. The point isn't
accuracy, it's deciding whether to interrupt the user with a confirmation
popup before kicking off a conversion that could take many minutes.
"""
from __future__ import annotations

# Throughput baselines (bytes per second, single conversion direction).
# Verify doubles the time because reverse direction must run too.
_BYTES_PER_SEC = {
    "video":      50_000_000,
    "audio":     100_000_000,
    "image_masq": 100_000_000,   # streaming PNG/BMP encode
    "text_masq":  200_000_000,   # base64 wrap
    "wav_masq":   200_000_000,   # raw PCM copy
    "mkv_masq":    25_000_000,   # FFV1 encode is the slow one
    "py_masq":     80_000_000,   # base64 + script wrapping
    "text":        50_000_000,
    "office_zip":  30_000_000,
    "pdf":         20_000_000,
    "default":     40_000_000,
}

# 10-minute threshold for showing the warning (matches the spec).
LONG_CONVERSION_SECONDS = 600


def _category(src_ext: str, dst_ext: str, masquerade: bool) -> str:
    """Pick the throughput bucket. Masquerade paths use specialized buckets
    because their work profile is dominated by the host encoder, not the
    source format."""
    s = src_ext.lower()
    d = dst_ext.lower()
    if not s.startswith("."): s = "." + s
    if not d.startswith("."): d = "." + d

    if masquerade or d in {".wav", ".png", ".bmp", ".txt", ".mkv", ".py"}:
        if d == ".png" or d == ".bmp":
            return "image_masq"
        if d == ".txt":
            return "text_masq"
        if d == ".wav":
            return "wav_masq"
        if d == ".mkv":
            return "mkv_masq"
        if d == ".py":
            return "py_masq"

    # Non-masquerade routing — pick by destination since that's usually the
    # bottleneck (encoder side).
    from .config import category_for
    return category_for(d)


def estimate_seconds(src_ext: str, dst_ext: str, src_size: int,
                      masquerade: bool = False) -> float:
    """Return rough seconds for a single forward conversion. Caller doubles
    for Verify Round-Trip mode."""
    if src_size <= 0:
        return 0.0
    cat = _category(src_ext, dst_ext, masquerade)
    bps = _BYTES_PER_SEC.get(cat, _BYTES_PER_SEC["default"])
    return src_size / bps


def estimate_verify_seconds(src_ext: str, dst_ext: str, src_size: int,
                             masquerade: bool = False) -> float:
    """Total wall-clock for a verified conversion (forward + reverse)."""
    return estimate_seconds(src_ext, dst_ext, src_size, masquerade) * 2
