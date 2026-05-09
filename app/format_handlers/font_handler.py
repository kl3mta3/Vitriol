"""Font conversions among otf, ttf, woff, woff2 via fontTools.

DOC_KIND="font" keeps fonts in their own corner — no accidental font→txt.
The IR is a `FontDoc` carrying the raw bytes of an SFNT-flavored font; the
flavor (None, 'woff', 'woff2') is derived from the source extension.
fontTools handles all four flavors through `TTFont`'s `flavor` attribute.

Notes:
  - WOFF/WOFF2 are SFNT containers with extra compression. Reading either
    yields the underlying SFNT (TTF or OTF). The output flavor is set on
    write based on the destination ext.
  - .otf vs .ttf: both are SFNT; the difference is the outline format
    (CFF for OTF, glyf for TTF). We do NOT convert between outline formats —
    if the source's outlines are CFF and you ask for .ttf, the file is still
    written but the outline tables stay CFF (most renderers will still load
    it but the .ttf extension is misleading). True CFF↔glyf conversion needs
    rasterization and is out of scope.
"""
from __future__ import annotations
import io
from dataclasses import dataclass
from pathlib import Path

from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".otf", ".ttf", ".woff", ".woff2"}
SUPPORTED_WRITE = {".otf", ".ttf", ".woff", ".woff2"}
DOC_KIND = "font"


@dataclass
class FontDoc:
    """Carries raw bytes plus the source flavor so the writer can decide
    whether to recompress (woff/woff2) or pass through (sfnt)."""
    data: bytes
    src_ext: str


def read(path: Path, ext: str, cancel: CancellationToken) -> FontDoc:
    return FontDoc(data=path.read_bytes(), src_ext=ext)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, FontDoc):
        raise RuntimeError("Font writer requires a FontDoc.")
    from fontTools.ttLib import TTFont

    src_flavor = _flavor_for_ext(doc.src_ext)
    dst_flavor = _flavor_for_ext(ext)

    # Same flavor: byte passthrough is correct (and avoids any re-pack risk).
    if src_flavor == dst_flavor:
        path.write_bytes(doc.data)
        return

    font = TTFont(io.BytesIO(doc.data))
    font.flavor = dst_flavor  # None for SFNT, "woff" or "woff2"
    font.save(str(path))


def _flavor_for_ext(ext: str):
    e = ext.lower()
    if e == ".woff":
        return "woff"
    if e == ".woff2":
        return "woff2"
    # .otf and .ttf are both raw SFNT containers from fontTools' perspective.
    return None
