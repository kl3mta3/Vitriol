"""First-launch dependency check.

Verifies presence of FFmpeg and Assimp DLL. If missing, prompts the user
(via dialogs.ask_install_dependency) and downloads from official sources
with checksum verification. Looks in ./bin/ and ./wheels/ first.

This module is intentionally non-fatal: any uncaught error here lets the
app launch anyway. The relevant handlers will surface clearer errors at
conversion time if their backend is missing.
"""
from __future__ import annotations
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from ..utils.logger import get_logger
from ..utils.paths import bin_dir, wheels_dir
from ..ui import dialogs
from .downloader import download_with_progress, ChecksumError

_log = get_logger()


# Hardcoded URLs and SHA-256 hashes. Update when bumping versions.
_FFMPEG_WIN_URL = "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip"
_FFMPEG_WIN_SHA = ""  # set when locking version; empty = skip verification (logged WARN)

_ASSIMP_WIN_URL = "https://github.com/assimp/assimp/releases/download/v5.4.3/assimp-5.4.3-win64.zip"
_ASSIMP_WIN_SHA = ""


# Both delegate to the canonical helpers in app.utils.paths so launcher,
# dependency_check, and the runtime modules use identical lookup logic.
def find_ffmpeg() -> Optional[Path]:
    from ..utils.paths import find_ffmpeg as _find
    return _find()


def find_assimp() -> Optional[Path]:
    from ..utils.paths import find_assimp as _find
    return _find()


def run_checks(parent=None) -> None:
    """Top-level entry called from main.py."""
    _ensure_ffmpeg(parent)
    _ensure_assimp(parent)


def _ensure_ffmpeg(parent) -> None:
    if find_ffmpeg() is not None:
        _log.info("ffmpeg present")
        return
    if not dialogs.ask_install_dependency(parent, "FFmpeg", "80MB", "audio and video conversion"):
        _log.info("user declined ffmpeg install")
        return
    target_zip = bin_dir() / "ffmpeg-download.zip"
    try:
        download_with_progress(_FFMPEG_WIN_URL, target_zip, expected_sha256=_FFMPEG_WIN_SHA or None)
        _extract_ffmpeg(target_zip)
    except ChecksumError as e:
        _log.error("ffmpeg checksum mismatch: %s", e)
        dialogs.error(parent, "FFmpeg download failed", "Checksum verification failed. Please install FFmpeg manually.")
    except Exception as e:
        _log.exception("ffmpeg install failed")
        dialogs.error(parent, "FFmpeg download failed", str(e))
    finally:
        try:
            if target_zip.exists():
                target_zip.unlink()
        except OSError:
            pass


def _extract_ffmpeg(zip_path: Path) -> None:
    """Pull just ffmpeg.exe and ffprobe.exe out of the gyan.dev zip."""
    target = bin_dir()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
                with z.open(name) as src, open(target / base, "wb") as out:
                    shutil.copyfileobj(src, out)
                try:
                    os.chmod(target / base, 0o755)
                except OSError:
                    pass


def _ensure_assimp(parent) -> None:
    if find_assimp() is not None:
        _log.info("assimp present")
        return
    if not dialogs.ask_install_dependency(parent, "Assimp", "10MB", "3D model conversion"):
        _log.info("user declined assimp install")
        return
    target_zip = bin_dir() / "assimp-download.zip"
    try:
        download_with_progress(_ASSIMP_WIN_URL, target_zip, expected_sha256=_ASSIMP_WIN_SHA or None)
        _extract_assimp(target_zip)
    except ChecksumError as e:
        _log.error("assimp checksum mismatch: %s", e)
        dialogs.error(parent, "Assimp download failed", "Checksum verification failed. Please install Assimp manually.")
    except Exception as e:
        _log.exception("assimp install failed")
        dialogs.error(parent, "Assimp download failed", str(e))
    finally:
        try:
            if target_zip.exists():
                target_zip.unlink()
        except OSError:
            pass


def _extract_assimp(zip_path: Path) -> None:
    target = bin_dir()
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            base = name.rsplit("/", 1)[-1].lower()
            if base.endswith(".dll") and "assimp" in base:
                with z.open(name) as src, open(target / base, "wb") as out:
                    shutil.copyfileobj(src, out)
