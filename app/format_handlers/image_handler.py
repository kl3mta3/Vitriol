"""Image conversion via Pillow, with a QtSvg bridge for reading SVGs.

Scope:
  - Reads & writes Pillow's standard formats: png, jpg, webp, bmp, tiff, gif,
    ico, tga, ppm, pgm, pbm, dds, plus heic if Pillow's HEIF plugin is present.
  - SVG is *read-only*: rendered by QSvgRenderer at the source's natural size
    into an offscreen QImage, then handed to Pillow for export. This uses
    QtSvg's SVG Tiny 1.2 implementation — most logos/icons render correctly,
    advanced filter effects may not. We do NOT write SVG.

Cancellation: pure-Python — checks the token once before save.
"""
from __future__ import annotations
import io
from pathlib import Path
from typing import Callable

from ..utils.cancellation import CancellationToken
from ..utils.logger import get_logger

_log = get_logger()

MEDIA_CATEGORY = "image"

# Standard Pillow extensions. SVG is read-only — present in SUPPORTED but
# rejected when used as a target inside `convert`.
_PIL_EXTS = {
    ".png", ".jpg", ".webp", ".bmp", ".tiff", ".gif", ".ico", ".tga",
    ".ppm", ".pgm", ".pbm", ".dds",
}
_HEIC_EXTS = {".heic"}
_SVG_EXTS = {".svg"}

SUPPORTED = _PIL_EXTS | _HEIC_EXTS | _SVG_EXTS
# SVG and HEIC are read-only in some configurations; only writeable formats here.
WRITE_SUPPORTED = _PIL_EXTS | _HEIC_EXTS


def _ensure_pillow():
    from PIL import Image  # noqa: F401
    return Image


def _ensure_heif() -> bool:
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


def _open_image(src: Path, src_ext: str):
    Image = _ensure_pillow()
    if src_ext in _SVG_EXTS:
        return _open_svg_via_qt(src)
    if src_ext in _HEIC_EXTS:
        if not _ensure_heif():
            raise RuntimeError(
                "HEIC/HEIF support requires the optional 'pillow-heif' package. "
                "Install it from ./wheels/ or pip install pillow-heif."
            )
    img = Image.open(src)
    img.load()
    return img


def _open_svg_via_qt(src: Path):
    """Render SVG to a QImage, then convert to a Pillow Image."""
    from PySide6.QtCore import QSize, Qt
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

    # Convert QImage -> raw RGBA bytes -> Pillow.
    qimg = qimg.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimg.width(), qimg.height()
    ptr = qimg.constBits().tobytes()
    return Image.frombuffer("RGBA", (w, h), ptr, "raw", "RGBA", 0, 1)


def _save_image(img, dst: Path, dst_ext: str) -> None:
    """Save with format-appropriate options. Handles JPEG-no-alpha, etc."""
    Image = _ensure_pillow()
    fmt_for_ext = {
        ".png": "PNG", ".jpg": "JPEG", ".webp": "WEBP", ".bmp": "BMP",
        ".tiff": "TIFF", ".gif": "GIF", ".ico": "ICO", ".tga": "TGA",
        ".ppm": "PPM", ".pgm": "PPM", ".pbm": "PPM", ".dds": "DDS",
        ".heic": "HEIF",
    }
    fmt = fmt_for_ext.get(dst_ext)
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
        # Generate common sizes
        save_kwargs["sizes"] = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    elif fmt == "GIF":
        if img.mode != "P":
            img = img.convert("P", palette=Image.Palette.ADAPTIVE)
    elif fmt == "HEIF":
        if not _ensure_heif():
            raise RuntimeError("Writing HEIC requires pillow-heif (not installed).")

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
        raise RuntimeError("Writing SVG is not supported in v1.")
    progress(0.05)
    img = _open_image(src, src_ext)
    progress(0.5)
    cancel.check()
    _save_image(img, dst, dst_ext)
    progress(1.0)
