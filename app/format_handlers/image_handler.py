"""Image conversion via Pillow, with a QtSvg bridge for reading SVGs.

Scope:
  - Reads & writes Pillow's standard formats (png, jpg, webp, bmp, tiff, gif,
    ico, tga, ppm, pgm, pbm, dds, dib, msp, pcx, pfm, sgi, xbm, icns, jp2, qoi,
    apng) plus avif/heic/heif/jxl when the optional plugins are installed.
  - Read-only Pillow formats (cur, dcx, fli, flc, mpo, psd, xpm, blp, eps):
    decoded but not written. Listed in SUPPORTED but excluded from
    WRITE_SUPPORTED so the registry strips them from target dropdowns.
  - SVG is *read-only*: rendered by QSvgRenderer at the source's natural size
    into an offscreen QImage, then handed to Pillow for export. This uses
    QtSvg's SVG Tiny 1.2 implementation — most logos/icons render correctly,
    advanced filter effects may not. We do NOT write SVG.

Cancellation: pure-Python — checks the token once before save.
"""
from __future__ import annotations
from pathlib import Path
from typing import Callable

from ..utils.cancellation import CancellationToken
from ..utils.logger import get_logger

_log = get_logger()

MEDIA_CATEGORY = "image"

# Standard Pillow formats: read AND write are supported.
_PIL_RW_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif", ".ico", ".tga",
    ".ppm", ".pgm", ".pbm", ".pnm", ".dds", ".dib", ".msp", ".pcx", ".pfm",
    ".sgi", ".xbm", ".icns", ".jp2", ".qoi", ".apng",
}
# Read-only Pillow formats. Pillow can decode these but writes are missing
# or unreliable (PSD/MPO/CUR write don't exist; BLP write is incomplete; FLI
# is animation-only; XPM/DCX/EPS write either don't exist or need Ghostscript).
_PIL_RO_EXTS = {
    ".cur", ".dcx", ".fli", ".flc", ".mpo", ".psd", ".xpm", ".blp", ".eps",
}
# Optional plugin formats — read+write iff the plugin imported successfully.
_AVIF_EXTS = {".avif"}
_HEIC_EXTS = {".heic", ".heif"}
_JXL_EXTS = {".jxl"}
_SVG_EXTS = {".svg"}

SUPPORTED = (
    _PIL_RW_EXTS | _PIL_RO_EXTS
    | _AVIF_EXTS | _HEIC_EXTS | _JXL_EXTS | _SVG_EXTS
)
# Read-only formats and SVG are excluded from writes. AVIF/HEIC/JXL are added
# in `_writable_set()` only when their plugin is importable.
WRITE_SUPPORTED = _PIL_RW_EXTS  # extended below at import time


def _ensure_pillow():
    from PIL import Image  # noqa: F401
    return Image


def _ensure_avif() -> bool:
    try:
        import pillow_avif  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_heif() -> bool:
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def _ensure_jxl() -> bool:
    try:
        import pillow_jxl  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


# Eagerly attempt plugin registration so Image.EXTENSION knows the formats
# at startup. Failures are quietly tolerated (the plugin may be missing on
# some installs).
_HAS_AVIF = _ensure_avif()
_HAS_HEIF = _ensure_heif()
_HAS_JXL = _ensure_jxl()
if _HAS_AVIF:
    WRITE_SUPPORTED |= _AVIF_EXTS
if _HAS_HEIF:
    WRITE_SUPPORTED |= _HEIC_EXTS
if _HAS_JXL:
    WRITE_SUPPORTED |= _JXL_EXTS


def _open_image(src: Path, src_ext: str):
    Image = _ensure_pillow()
    if src_ext in _SVG_EXTS:
        return _open_svg_via_qt(src)
    if src_ext in _HEIC_EXTS and not _ensure_heif():
        raise RuntimeError(
            "HEIC/HEIF support requires the optional 'pillow-heif' package. "
            "pip install pillow-heif"
        )
    if src_ext in _AVIF_EXTS and not _ensure_avif():
        raise RuntimeError(
            "AVIF support requires the optional 'pillow-avif-plugin' package. "
            "pip install pillow-avif-plugin"
        )
    if src_ext in _JXL_EXTS and not _ensure_jxl():
        raise RuntimeError(
            "JPEG XL support requires the optional 'pillow-jxl-plugin' package. "
            "pip install pillow-jxl-plugin"
        )
    img = Image.open(src)
    img.load()
    return img


def _open_svg_via_qt(src: Path):
    """Render SVG to a QImage, then convert to a Pillow Image."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtSvg import QSvgRenderer
    from PIL import Image

    renderer = QSvgRenderer(str(src))
    if not renderer.isValid():
        raise RuntimeError("SVG file could not be parsed (QtSvg supports SVG Tiny 1.2 only).")

    default_size = renderer.defaultSize()
    if default_size.width() <= 0 or default_size.height() <= 0:
        default_size = QSize(1024, 1024)

    qimg = QImage(default_size, QImage.Format.Format_ARGB32)
    qimg.fill(QColor(0, 0, 0, 0))
    painter = QPainter(qimg)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()

    qimg = qimg.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.constBits().tobytes()
    return Image.frombuffer("RGBA", (w, h), ptr, "raw", "RGBA", 0, 1)


# Map output extension → Pillow format identifier. Anything not in here is
# rejected as an unwritable target.
_FMT_FOR_EXT = {
    ".png": "PNG", ".apng": "PNG",
    ".jpg": "JPEG", ".jpeg": "JPEG",
    ".webp": "WEBP", ".bmp": "BMP", ".dib": "DIB",
    ".tiff": "TIFF",
    ".gif": "GIF", ".ico": "ICO", ".tga": "TGA",
    ".ppm": "PPM", ".pgm": "PPM", ".pbm": "PPM", ".pnm": "PPM",
    ".dds": "DDS", ".msp": "MSP", ".pcx": "PCX",
    ".pfm": "PPM", ".sgi": "SGI", ".xbm": "XBM",
    ".icns": "ICNS", ".jp2": "JPEG2000", ".qoi": "QOI",
    ".heic": "HEIF", ".heif": "HEIF",
    ".avif": "AVIF",
    ".jxl": "JXL",
}


def _save_image(img, dst: Path, dst_ext: str) -> None:
    """Save with format-appropriate options. Handles JPEG-no-alpha, etc."""
    Image = _ensure_pillow()
    fmt = _FMT_FOR_EXT.get(dst_ext)
    if fmt is None:
        raise RuntimeError(f"Cannot write image format {dst_ext}.")
    save_kwargs: dict = {"format": fmt}

    if fmt == "JPEG":
        # JPEG can't hold alpha. Composite RGBA onto white.
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            mask = img.split()[-1]
            bg.paste(img.convert("RGB"), mask=mask)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        save_kwargs["quality"] = 92
        save_kwargs["optimize"] = True
    elif fmt == "PNG":
        save_kwargs["optimize"] = True
    elif fmt == "WEBP":
        save_kwargs["quality"] = 92
    elif fmt == "ICO":
        save_kwargs["sizes"] = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    elif fmt == "GIF":
        if img.mode != "P":
            img = img.convert("P", palette=Image.Palette.ADAPTIVE)
    elif fmt == "ICNS":
        # ICNS expects square RGBA; resize to a supported size if needed.
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        # Pick a sensible default that ICNS supports.
        target = max(img.size)
        for sz in (1024, 512, 256, 128, 64, 32, 16):
            if target >= sz:
                if img.size != (sz, sz):
                    img = img.resize((sz, sz))
                break
    elif fmt == "JPEG2000":
        save_kwargs["quality_mode"] = "rates"
        save_kwargs["quality_layers"] = [10]
    elif fmt == "AVIF":
        save_kwargs["quality"] = 75
    elif fmt == "HEIF":
        if not _ensure_heif():
            raise RuntimeError("Writing HEIC/HEIF requires pillow-heif (not installed).")
        save_kwargs["quality"] = 75
    elif fmt == "JXL":
        if not _ensure_jxl():
            raise RuntimeError("Writing JXL requires pillow-jxl-plugin (not installed).")
    elif fmt in ("PPM", "PCX", "TGA", "BMP", "DIB", "SGI", "MSP", "XBM"):
        # These need a non-alpha mode for many formats.
        if fmt in ("PPM", "PCX") and img.mode in ("RGBA", "LA", "PA"):
            img = img.convert("RGB")
        if fmt == "MSP" and img.mode != "1":
            # MSP is monochrome only.
            img = img.convert("L").convert("1")
        if fmt == "XBM" and img.mode != "1":
            img = img.convert("L").convert("1")

    # PFM is a floating-point format but Pillow's PPM plugin will write it
    # using the .pfm extension if format="PPM" — actually PFM needs format
    # "PPM" too. Verified by Pillow docs.
    img.save(dst, **save_kwargs)


def convert(
    src: Path,
    dst: Path,
    src_ext: str,
    dst_ext: str,
    cancel: CancellationToken,
    progress: Callable[[float], None],
) -> None:
    if dst_ext in _SVG_EXTS:
        raise RuntimeError("Writing SVG is not supported.")
    if dst_ext in _PIL_RO_EXTS:
        raise RuntimeError(f"Writing {dst_ext} is not supported (Pillow read-only).")
    progress(0.05)
    img = _open_image(src, src_ext)
    progress(0.5)
    cancel.check()
    _save_image(img, dst, dst_ext)
    progress(1.0)
