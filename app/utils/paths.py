"""Path utilities. Output directories, app data dir, read-only-install fallback for ./bin/."""
from __future__ import annotations
import os
import sys
from pathlib import Path

CATEGORY_TEXT = "Text"
CATEGORY_AUDIO = "Audio"
CATEGORY_VIDEO = "Video"
CATEGORY_IMAGES = "Images"
CATEGORY_MODELS = "Models"
ALL_CATEGORIES = (CATEGORY_TEXT, CATEGORY_AUDIO, CATEGORY_VIDEO, CATEGORY_IMAGES, CATEGORY_MODELS)


def app_root() -> Path:
    """Directory where the app lives (next to main.py, or alongside the PyInstaller exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def _local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "UniversalConverter"


def _is_writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def bin_dir() -> Path:
    """Where FFmpeg / Assimp DLL live. Falls back to %LOCALAPPDATA% if ./bin is read-only.
    Honors UC_BIN_DIR (set by launcher.py) so the subprocess agrees with where
    the launcher installed the binaries."""
    env = os.environ.get("UC_BIN_DIR")
    if env:
        p = Path(env)
        if _is_writable(p):
            return p
    local = app_root() / "bin"
    if _is_writable(local):
        return local
    fallback = _local_app_data() / "bin"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def hw_encoder_cache() -> Path:
    """Path to the JSON file the launcher wrote with hardware encoder probe results."""
    env = os.environ.get("UC_HW_CACHE")
    if env:
        return Path(env)
    return bin_dir() / "hw_encoders.json"


def wheels_dir() -> Path:
    return app_root() / "wheels"


def resources_dir() -> Path:
    env = os.environ.get("UC_RESOURCES_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p
    return app_root() / "resources"


def output_dir(category: str) -> Path:
    """Default output dir for a category. Created on demand."""
    if category not in ALL_CATEGORIES:
        category = CATEGORY_TEXT
    p = app_root() / "output" / category
    if not _is_writable(p):
        p = _local_app_data() / "output" / category
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file() -> Path:
    base = app_root() / "logs"
    if not _is_writable(base):
        base = _local_app_data() / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "universal-converter.log"


def unique_path(target: Path) -> Path:
    """If target exists, append ' (1)', ' (2)', ... before the suffix until unique."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1
