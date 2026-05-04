"""Runtime configuration for streaming I/O and resource budgeting.

streaming_threshold(ext) returns the source file size at/above which the
router should prefer the streaming code path. The threshold is dynamic per
launch: it scales with available RAM and divides by a per-format multiplier
that estimates how much working memory a whole-file conversion of that
format typically needs (e.g., ZIP-based office formats unpack into 3× their
on-disk size; rasterized images can balloon 8× when decoded; FFmpeg pipes
need ~1× the source).
"""
from __future__ import annotations

try:
    import psutil  # required dep — launcher auto-installs if missing
    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover — defensive for dev runs without launcher
    psutil = None  # type: ignore
    _HAS_PSUTIL = False


CHUNK_SIZE = 1 * 1024 * 1024              # 1 MB read/write chunk
DISK_SPACE_WARN_MULT = 1.5                # warn if free disk < 1.5× expected output
PROGRESS_EMIT_THROTTLE_MS = 500           # max one bytes_progress emit per 500 ms

# Fallback safety floor when psutil isn't available — keep direct-path
# behavior conservative rather than aggressively triggering streaming.
_FALLBACK_THRESHOLD = 500 * 1024 * 1024   # 500 MB

# Per-format expected memory multiplier (whole-file decoded size / on-disk size).
_MULTIPLIERS = {
    "image": 8,        # rgba decode + alpha + intermediate buffers
    "pdf": 4,          # parsed object graph + decompressed streams
    "office_zip": 3,   # zip contents + parsed XML trees
    "video": 1,        # FFmpeg streams; only progress text in Python
    "audio": 1,        # same
    "text": 2,         # source bytes + decoded unicode
    "3d_model": 5,     # vertex/index/material buffers
    "default": 4,
}

_CATEGORIES: dict[str, str] = {
    # image
    ".png": "image", ".bmp": "image", ".jpg": "image", ".jpeg": "image",
    ".webp": "image", ".gif": "image", ".tiff": "image", ".tif": "image",
    ".ico": "image", ".tga": "image", ".ppm": "image", ".pgm": "image",
    ".pbm": "image", ".dds": "image", ".heic": "image", ".heif": "image",
    ".svg": "image",
    # pdf
    ".pdf": "pdf",
    # office zip containers
    ".docx": "office_zip", ".xlsx": "office_zip", ".pptx": "office_zip",
    ".epub": "office_zip", ".odt": "office_zip",
    # video
    ".mp4": "video", ".mkv": "video", ".webm": "video", ".avi": "video",
    ".mov": "video", ".wmv": "video", ".flv": "video", ".mpg": "video",
    ".mpeg": "video", ".3gp": "video", ".ts": "video", ".mts": "video",
    ".vob": "video", ".ogv": "video", ".m4v": "video",
    # audio
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio",
    ".opus": "audio", ".m4a": "audio", ".aac": "audio", ".wma": "audio",
    ".aiff": "audio", ".aif": "audio", ".alac": "audio", ".ac3": "audio",
    ".amr": "audio", ".au": "audio", ".mka": "audio",
    # text
    ".txt": "text", ".md": "text", ".html": "text", ".htm": "text",
    ".json": "text", ".xml": "text", ".yaml": "text", ".yml": "text",
    ".ini": "text", ".log": "text", ".csv": "text", ".tsv": "text",
    ".rtf": "text", ".py": "text",
    # 3d models
    ".glb": "3d_model", ".gltf": "3d_model", ".obj": "3d_model",
    ".stl": "3d_model", ".fbx": "3d_model", ".ply": "3d_model",
    ".dae": "3d_model", ".3ds": "3d_model",
}


def category_for(ext: str) -> str:
    e = ext.lower()
    if not e.startswith("."):
        e = "." + e
    return _CATEGORIES.get(e, "default")


def streaming_threshold(ext: str) -> int:
    """Bytes — files at/above this size should use the streaming path.

    safe_budget = available RAM // 4 (leave 75% headroom for Qt + handlers)
    threshold   = safe_budget // multiplier(category)

    Floors at 64 MB so we don't pathologically stream tiny files on a host
    that's currently very low on free memory.
    """
    cat = category_for(ext)
    mult = _MULTIPLIERS.get(cat, _MULTIPLIERS["default"])
    if not _HAS_PSUTIL:
        return _FALLBACK_THRESHOLD // mult
    safe = psutil.virtual_memory().available // 4
    return max(64 * 1024 * 1024, safe // mult)


def free_disk_for(path) -> int:
    """Return free bytes on the filesystem containing `path` (its parent if
    it doesn't exist yet). Returns a very large number if psutil is missing
    so the disk-space warning doesn't fire spuriously."""
    if not _HAS_PSUTIL:
        return 1 << 62
    from pathlib import Path
    p = Path(path)
    target = p if p.exists() else p.parent
    try:
        return psutil.disk_usage(str(target)).free
    except OSError:
        return 1 << 62
