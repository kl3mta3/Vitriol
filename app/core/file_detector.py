"""Detect file format. Extension first, magic bytes second.

Recoded from scratch — no python-magic, no filetype lib.
Magic byte signatures cover the ~20 most common formats. For container formats
(MP4, MKV, WebM, etc.) we fall back to extension since FFmpeg will reject
mismatches anyway.
"""
from __future__ import annotations
import io
import zipfile
from pathlib import Path
from typing import Optional


# Map extension (lowercase, with dot) -> normalized extension.
# Aliases collapse to a canonical form so the registry stays small.
_ALIASES = {
    ".jpeg": ".jpg",
    ".tif": ".tiff",
    ".htm": ".html",
    ".yml": ".yaml",
    ".aif": ".aiff",
    ".mts": ".ts",
    # Tar archive single-suffix aliases (.tgz, .tbz2, .txz, .tzst) collapse
    # to the canonical compound forms used by archive_handler.
    ".tgz": ".tar.gz",
    ".tbz2": ".tar.bz2",
    ".tbz": ".tar.bz2",
    ".txz": ".tar.xz",
    ".tzst": ".tar.zst",
}

# Compound suffixes that need to be matched on the full filename rather than
# `path.suffix` (which only returns the last `.foo` segment). Order matters
# only for prefix overlap, which we don't have here.
_COMPOUND_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


def normalize_ext(ext: str) -> str:
    ext = ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    return _ALIASES.get(ext, ext)


def ext_for(path: Path) -> str:
    name = path.name.lower()
    for compound in _COMPOUND_SUFFIXES:
        if name.endswith(compound):
            return compound
    return normalize_ext(path.suffix)


def detect(path: Path) -> str:
    """Return canonical extension for a file. Falls back to ext if magic is ambiguous."""
    ext = ext_for(path)
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return ext
    sniffed = _sniff(head, path)
    return sniffed if sniffed else ext


def _sniff(head: bytes, path: Path) -> Optional[str]:
    if not head:
        return None
    # Images
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:2] == b"BM":
        return ".bmp"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff"
    if head[:4] == b"DDS ":
        return ".dds"
    if head[:6] == b"\x00\x00\x01\x00":
        return ".ico"
    # 3D
    if head[:4] == b"glTF":
        return ".glb"
    if head[:5] == b"solid" or head[:80].count(b"\x00") > 20:
        # ASCII vs binary STL — ambiguous; trust extension if it claims STL
        if path.suffix.lower() == ".stl":
            return ".stl"
    # Documents
    if head[:5] == b"%PDF-":
        return ".pdf"
    if head[:5] == b"{\\rtf" or head[:6] == b"{\\rtf1":
        return ".rtf"
    # ZIP family — peek inside for office/epub
    if head[:4] == b"PK\x03\x04":
        return _zip_subtype(path)
    # XML / SVG
    if head.lstrip()[:5] == b"<?xml":
        rest = head.lstrip().lower()
        if b"<svg" in rest[:200]:
            return ".svg"
        return ".xml"
    if head.lstrip().lower().startswith(b"<svg"):
        return ".svg"
    # JSON
    stripped = head.lstrip()
    if stripped[:1] in (b"{", b"["):
        if b'"asset"' in head and b'"version"' in head:
            return ".gltf"
        return ".json"
    # HTML
    low = head[:512].lower().lstrip()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html"):
        return ".html"
    return None


def _zip_subtype(path: Path) -> str:
    """Inspect a zip for Office / EPUB / OpenDocument signatures."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "[Content_Types].xml" in names:
                joined = " ".join(names)
                if "word/" in joined:
                    return ".docx"
                if "xl/" in joined:
                    return ".xlsx"
                if "ppt/" in joined:
                    return ".pptx"
            if "mimetype" in names:
                try:
                    mt = z.read("mimetype").decode("ascii", errors="ignore").strip()
                except Exception:
                    mt = ""
                if mt == "application/epub+zip":
                    return ".epub"
                if mt.startswith("application/vnd.oasis.opendocument.text"):
                    return ".odt"
    except (zipfile.BadZipFile, OSError):
        pass
    return ".zip"


def is_supported(path: Path, supported: set[str]) -> bool:
    """Quick check used by drop-zone folder traversal."""
    return ext_for(path) in supported
