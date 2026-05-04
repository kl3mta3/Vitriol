"""Format handler registry.

Walks sibling modules at import time. Each handler module exposes:
    SUPPORTED_READ:  set[str]
    SUPPORTED_WRITE: set[str]
    DOC_KIND:        str  ("text" | "tabular" | "binary" | "media")
    read(path, ext, cancel) -> TextDoc | Tabular | bytes
    write(doc, path, ext, cancel) -> None

Media-category handlers (image_handler, audio_video, model_handler) instead expose:
    MEDIA_CATEGORY: str  ("image" | "audio" | "video" | "model")
    SUPPORTED:      set[str]
    convert(src_path, dst_path, src_ext, dst_ext, cancel, progress) -> None

Both registries are assembled defensively: a missing/broken module is logged and
skipped, never crashes the app. This matters because some handlers depend on
optional native binaries (FFmpeg, Assimp).
"""
from __future__ import annotations
import importlib
import pkgutil
from typing import Callable

from ..utils.logger import get_logger

_log = get_logger()

READERS: dict[str, object] = {}
WRITERS: dict[str, object] = {}
MEDIA_HANDLERS: dict[str, object] = {}        # ext -> module (read+write capable)
MEDIA_CATEGORY_OF: dict[str, str] = {}        # ext -> "image"/"audio"/"video"/"model"
MEDIA_WRITE_OK: set[str] = set()              # ext -> writable by its handler

# Categorize an extension into the five high-level groups (used by output dir + dropdown).
EXT_CATEGORY: dict[str, str] = {}


def _register_module(mod) -> None:
    name = mod.__name__.split(".")[-1]
    media_cat = getattr(mod, "MEDIA_CATEGORY", None)
    if media_cat:
        category_of = getattr(mod, "category_of", None)
        write_supported = getattr(mod, "WRITE_SUPPORTED", None)
        for ext in getattr(mod, "SUPPORTED", set()):
            MEDIA_HANDLERS[ext] = mod
            cat = category_of(ext) if callable(category_of) else media_cat
            MEDIA_CATEGORY_OF[ext] = cat
            EXT_CATEGORY[ext] = {"image": "Images", "audio": "Audio", "video": "Video", "model": "Models"}[cat]
            if write_supported is None or ext in write_supported:
                MEDIA_WRITE_OK.add(ext)
        _log.info("registered media handler %s for %s", name, sorted(getattr(mod, "SUPPORTED", set())))
        return
    for ext in getattr(mod, "SUPPORTED_READ", set()):
        READERS.setdefault(ext, mod)
        EXT_CATEGORY.setdefault(ext, "Text")
    for ext in getattr(mod, "SUPPORTED_WRITE", set()):
        WRITERS.setdefault(ext, mod)
        EXT_CATEGORY.setdefault(ext, "Text")
    _log.info(
        "registered handler %s read=%s write=%s",
        name,
        sorted(getattr(mod, "SUPPORTED_READ", set())),
        sorted(getattr(mod, "SUPPORTED_WRITE", set())),
    )


def load_all() -> None:
    """Idempotent: clear and rebuild registries."""
    READERS.clear()
    WRITERS.clear()
    MEDIA_HANDLERS.clear()
    MEDIA_CATEGORY_OF.clear()
    MEDIA_WRITE_OK.clear()
    EXT_CATEGORY.clear()
    pkg = __name__
    for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        if info.name.startswith("_"):
            continue
        full = f"{pkg}.{info.name}"
        try:
            mod = importlib.import_module(full)
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("failed to import handler %s: %s", full, e)
            continue
        try:
            _register_module(mod)
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("failed to register handler %s: %s", full, e)


def all_supported_exts() -> set[str]:
    return set(READERS) | set(WRITERS) | set(MEDIA_HANDLERS)


def valid_targets_for(src_ext: str, masquerade: bool = False) -> list[str]:
    """Compute the list of extensions the user can convert TO, given src_ext.

    When `masquerade` is True, also include the byte-passthrough host targets
    (WAV, PNG, BMP, TXT) so the user can hide any source inside a lossless
    container.
    """
    src_ext = src_ext.lower()
    if not src_ext.startswith("."):
        src_ext = "." + src_ext

    # Philosopher's Stone extras: any *lossless* source can be embedded into
    # any Stone host. Lossy sources (jpg, mp3, mp4, etc.) are excluded so the
    # round-trip stays meaningful — see masquerade.LOSSY_EXTS for the list.
    extras: set[str] = set()
    if masquerade:
        try:
            from . import masquerade as _msq
            if not _msq.is_lossy(src_ext):
                extras = {e for e in _msq.TARGETS if e != src_ext}
        except ImportError:
            extras = set()

    # Media: stay in the same category (image-to-image, audio-to-audio, video-to-video, model-to-model).
    if src_ext in MEDIA_HANDLERS:
        cat = MEDIA_CATEGORY_OF[src_ext]
        targets = {e for e, c in MEDIA_CATEGORY_OF.items() if c == cat and e != src_ext}
        # Special case: video can be converted to audio (extract audio track).
        if cat == "video":
            targets |= {e for e, c in MEDIA_CATEGORY_OF.items() if c == "audio"}
        targets &= MEDIA_WRITE_OK
        # NEW: image sources can also convert to document targets — the
        # image_bytes_to_textdoc adapter wraps them in a single-block TextDoc
        # which document writers (pdf_write, markdown, txt, html, docx, epub)
        # render as embedded images or bundle layouts.
        if cat == "image":
            for ext, mod in WRITERS.items():
                if ext == src_ext:
                    continue
                # Any text/tabular DOC_KIND writer can accept the image-to-textdoc
                # adapter's output.
                if getattr(mod, "DOC_KIND", "text") in ("text", "tabular"):
                    targets.add(ext)
        return sorted(targets | extras)

    # Document/tabular: any ext that has a writer of compatible kind.
    src_mod = READERS.get(src_ext)
    if not src_mod:
        # Source has no reader, but masquerade can still embed any bytes.
        return sorted(extras) if extras else []
    src_kind = getattr(src_mod, "DOC_KIND", "text")
    targets: set[str] = set()
    for ext, mod in WRITERS.items():
        if ext == src_ext:
            continue
        dst_kind = getattr(mod, "DOC_KIND", "text")
        if dst_kind == src_kind or (src_kind, dst_kind) in _ADAPTER_PAIRS:
            targets.add(ext)
    return sorted(targets | extras)


# Mirror of intermediate.ADAPTERS keys; kept here to avoid a circular import.
_ADAPTER_PAIRS = {
    ("tabular", "text"),
    ("text", "tabular"),
    ("text", "binary"),
    ("binary", "text"),
    ("tabular", "binary"),
    ("binary", "tabular"),
}


# Default target preference per source ext. Falls back to alphabetical first valid.
_DEFAULT_TARGETS = {
    ".txt": ".md", ".md": ".html", ".html": ".md",
    ".csv": ".xlsx", ".tsv": ".xlsx", ".xlsx": ".csv",
    ".json": ".yaml", ".yaml": ".json", ".xml": ".json",
    ".docx": ".pdf", ".pdf": ".txt", ".epub": ".pdf",
    ".rtf": ".docx", ".odt": ".docx", ".pptx": ".pdf",
    ".png": ".jpg", ".jpg": ".png", ".webp": ".png", ".bmp": ".png",
    ".gif": ".png", ".tiff": ".png", ".ico": ".png", ".tga": ".png",
    ".svg": ".png", ".heic": ".jpg",
    ".mp3": ".wav", ".wav": ".mp3", ".flac": ".mp3", ".ogg": ".mp3",
    ".opus": ".mp3", ".m4a": ".mp3", ".aac": ".mp3", ".aiff": ".mp3",
    ".mp4": ".mp3", ".mkv": ".mp4", ".webm": ".mp4", ".mov": ".mp4",
    ".avi": ".mp4", ".wmv": ".mp4", ".flv": ".mp4", ".mpg": ".mp4",
    ".3gp": ".mp4", ".ts": ".mp4", ".vob": ".mp4", ".ogv": ".mp4",
    ".glb": ".obj", ".obj": ".glb", ".fbx": ".glb", ".stl": ".glb",
    ".ply": ".glb", ".dae": ".glb", ".3ds": ".glb", ".gltf": ".glb",
}


def default_target_for(src_ext: str) -> str | None:
    src_ext = src_ext.lower()
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    targets = valid_targets_for(src_ext)
    if not targets:
        return None
    pref = _DEFAULT_TARGETS.get(src_ext)
    if pref and pref in targets:
        return pref
    return targets[0]
