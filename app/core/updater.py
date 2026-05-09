"""Auto-update via GitHub Releases.

Flow:

1. On launch (after splash + main window show), main.py kicks off a
   background QThread that calls `check_for_update()`. We throttle this
   to once per 24h so an app launched many times a day doesn't pound
   the GitHub API.

2. `check_for_update()` GETs `releases/latest` from the API, finds the
   `VitriolSetup-X.Y.Z.exe` asset, parses the SHA-256 out of the release
   body, and compares the version tag to `app.__version__.__version__`.
   Returns an info dict if newer, else None.

3. MainWindow shows a status-bar banner advertising the update. Clicking
   the banner opens UpdateAvailableDialog (in app/ui/dialogs.py).

4. From the dialog, "Update now" calls `download_installer()` (with a
   QProgressDialog) and on success calls `launch_installer_and_exit()`
   to hand off to the Inno installer and quit the app.

Portable builds skip the install step — `is_portable_build()` returns
True when not running from Program Files, and the dialog substitutes a
"Open download page" button that just opens the GitHub release URL.

This module is pure-Python except for `launch_installer_and_exit` which
spawns the installer subprocess. It does NOT import PySide6 — the
network call runs on a worker thread, and Qt-side wiring lives in
main.py / main_window.py / dialogs.py.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..__version__ import __version__
from ..utils import settings
from ..utils.logger import get_logger
from ..utils.net import fetch_json
from .downloader import ChecksumError, download_with_progress

_log = get_logger()


# Update both of these together when forking / re-publishing under a
# different repo. The browser fallback (used when SmartScreen / a
# corporate proxy blocks the API) reaches the human-readable releases
# page rather than the JSON.
GITHUB_REPO = "kl3mta3/Vitriol"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"

# 24 hours between automatic checks. Manual "Check for updates..." in
# the Help menu bypasses this throttle.
_CHECK_INTERVAL_S = 24 * 60 * 60


# Asset name pattern. Accepts both `VitriolSetup-1.2.0.exe` (versioned —
# what older releases shipped) and `VitriolSetup.exe` (un-versioned — used
# so that `releases/latest/download/VitriolSetup.exe` is a stable URL the
# website can link without per-version updates). Either is valid; the
# version comparison uses the release's tag_name field, not the filename.
_INSTALLER_NAME_RE = re.compile(
    r"^VitriolSetup(?:-\d+\.\d+\.\d+)?\.exe$", re.IGNORECASE
)

# SHA-256 in the release body — the RELEASE_NOTES_INSTALLER.md template
# emits the hash as a lone 64-char hex string inside a fenced code block.
# Match any 64-hex run; downstream code picks the first one. False
# positives are extremely unlikely (no other 64-char hex blob lives in
# the release notes).
_SHA256_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_for_update(silent: bool = True) -> Optional[dict[str, Any]]:
    """Return info about a newer release, or None.

    `silent=True` (the default, used at launch) swallows network errors
    and simply returns None — a missing internet connection is not an
    error, the user just doesn't get a banner.

    `silent=False` (used by the manual "Check for updates..." menu) lets
    the caller see the underlying failure mode by re-raising. We only
    raise on hard failures; "no newer version" still returns None.

    The returned dict shape:
      {
        "version":       "1.2.0",       # str, no leading 'v'
        "url":           str,           # https URL to the .exe asset
        "size":          int,           # bytes
        "sha256":        str | None,    # hex digest, or None if not in body
        "notes":         str,           # raw markdown body
        "published_at":  str,           # ISO-8601 timestamp from GitHub
      }
    """
    # Throttle: don't refresh `last_update_check` ourselves — wait until
    # the network call actually succeeds. This way a 24h streak of
    # failed checks doesn't accidentally suppress a real update once
    # connectivity returns.
    raw = fetch_json(GITHUB_API_LATEST)
    if raw is None:
        if not silent:
            raise RuntimeError("Could not reach GitHub. Check your internet connection.")
        return None

    # Mark a successful API hit so the throttle resets even if there's
    # nothing newer.
    settings.set("last_update_check", int(time.time()))

    tag = raw.get("tag_name") or ""
    # Extract the version pattern from anywhere in the tag string.
    # Accepts all common tagging conventions:
    #   "v1.1.1"         -> "1.1.1"
    #   "1.1.1"          -> "1.1.1"
    #   "Vitriol_1.1.1"  -> "1.1.1"
    #   "release-2.0.0"  -> "2.0.0"
    # Rejects tags that contain no version-shaped substring at all
    # (e.g. "Vitriol_Portable_Edition", "nightly", "main") — protects
    # against the case where a non-versioned release gets accidentally
    # clicked as "Set as the latest release" in the GitHub UI. Without
    # this guard the updater would parse the non-version tag as (0,)
    # via _version_tuple's fallback, decide nothing's newer, and
    # silently stop notifying users about real new releases.
    m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", tag)
    if not m:
        if not silent:
            raise RuntimeError(
                f"Latest release tag '{tag}' contains no version number — "
                f"check the 'Set as latest release' flag on the GitHub release page."
            )
        return None
    latest = m.group(1)
    if not latest:
        if not silent:
            raise RuntimeError("GitHub returned a release without a version tag.")
        return None

    if _version_tuple(latest) <= _version_tuple(__version__):
        return None

    skip = settings.get("skip_version") or ""
    if skip and skip == latest:
        return None

    asset = _find_installer_asset(raw.get("assets") or [])
    if asset is None:
        # Newer version exists, but no installer attached yet (release
        # might be tagged before assets uploaded). Treat as no update —
        # next check will pick it up once the asset lands.
        _log.info("update %s exists but no installer asset yet", latest)
        return None

    body = raw.get("body") or ""
    sha = _extract_sha256_from_body(body)

    return {
        "version": latest,
        "url": asset["browser_download_url"],
        "size": int(asset.get("size") or 0),
        "sha256": sha,
        "notes": body,
        "published_at": raw.get("published_at") or "",
    }


def download_installer(
    info: dict[str, Any],
    on_progress: Optional[Callable[[float], None]] = None,
) -> Path:
    """Download the installer to %TEMP%, verify SHA-256, return the path.

    `on_progress(fraction)` is called repeatedly during the download with
    a value between 0.0 and 1.0. Plug a QProgressDialog into this signal
    on the UI side.

    Raises RuntimeError on download failure or hash mismatch — the temp
    file is cleaned up either way, so the caller never has to worry
    about a leftover .part on disk.
    """
    target = _temp_dir() / f"VitriolSetup-{info['version']}.exe"
    try:
        download_with_progress(
            url=info["url"],
            dst=target,
            expected_sha256=info.get("sha256"),
            progress=on_progress,
            timeout=120,
        )
    except ChecksumError as e:
        # Surface a friendlier message — the user sees this in the error
        # dialog and "checksum mismatch" is exactly the wording that
        # hints at tampering or a CDN cache bug, both worth flagging.
        raise RuntimeError(
            "Update download failed integrity check. The file does not match "
            "the SHA-256 published with the release. This usually means the "
            "download was interrupted or corrupted; try again. If it keeps "
            "failing, download manually from the GitHub releases page."
        ) from e
    except Exception as e:
        raise RuntimeError(f"Update download failed: {e}") from e
    return target


def launch_installer_and_exit(installer_path: Path) -> None:
    """Spawn the installer in a detached process and exit this app.

    The detach is critical: on Windows, the running Vitriol.exe holds
    a file lock on its own image. The installer's `CloseApplications=yes`
    directive (added in vitriol.iss) tells Inno to wait for the running
    instance to close before replacing files — by exiting here, we
    release the lock immediately so the install proceeds without the
    user seeing Inno's "close running app" prompt.

    `RestartApplications=yes` (also in vitriol.iss) re-launches the new
    Vitriol.exe after install, completing the loop with no manual step.
    """
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS (0x08) + CREATE_NEW_PROCESS_GROUP (0x200) so
        # the installer survives our exit and doesn't share the parent's
        # console (we have no console anyway, but defense in depth).
        creationflags = 0x00000008 | 0x00000200

    try:
        subprocess.Popen(
            [str(installer_path)],
            creationflags=creationflags,
            close_fds=True,
        )
    except OSError as e:
        # Failed to launch — leave the .exe on disk so the user can
        # double-click it manually. Re-raise so the UI shows the error.
        raise RuntimeError(
            f"Could not launch the installer: {e}\n\n"
            f"You can run it manually from:\n{installer_path}"
        ) from e
    # Brief flush + exit. The installer is fully loaded into memory by
    # Windows before this point, so killing the parent doesn't disturb it.
    sys.exit(0)


def is_portable_build() -> bool:
    """True when running from a folder that is NOT under Program Files.

    Portable-zip users get a "Open download page" button instead of an
    inline installer launch, since they didn't go through Inno Setup
    and overwriting their extracted folder with a fresh installer isn't
    the right UX (it would install a SECOND copy under Program Files).

    Heuristic: compare `sys.executable` path against the standard
    Program Files locations. Imperfect (a user could install Vitriol to
    `C:\\Tools\\Vitriol\\` via the installer's custom path picker), but
    good enough for the 99% case. False positives just route users
    through the manual download path, which is harmless.
    """
    if not getattr(sys, "frozen", False):
        # Running from source — neither portable nor installed. Treat as
        # portable so we don't try to silently install over a dev tree.
        return True
    exe = Path(sys.executable).resolve()
    program_files = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramW6432", r"C:\Program Files"),
    ]
    for pf in program_files:
        if not pf:
            continue
        try:
            if Path(pf).resolve() in exe.parents:
                return False
        except OSError:
            continue
    return True


def should_check_now() -> bool:
    """Has it been at least 24 h since the last successful check?

    Used by main.py to decide whether the post-launch check is allowed
    to fire. The manual menu entry bypasses this and always runs.
    """
    last = settings.get("last_update_check")
    try:
        last = int(last)
    except (TypeError, ValueError):
        last = 0
    return (time.time() - last) >= _CHECK_INTERVAL_S


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse "1.2.3" -> (1, 2, 3). Tolerant of trailing pre-release tags
    like "1.2.3-rc1" — strips the suffix so a release candidate compares
    as equal to its base version (intentional: we don't want users on
    1.2.3 stable to be nagged into 1.2.3-rc1)."""
    base = v.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for part in base.split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _find_installer_asset(assets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for a in assets:
        name = a.get("name") or ""
        if _INSTALLER_NAME_RE.match(name):
            return a
    return None


def _extract_sha256_from_body(body: str) -> Optional[str]:
    """Return the first 64-char hex token found in the release body.

    The release-notes template emits the hash on its own line inside a
    fenced code block, so the very first hex run is reliably the one we
    want. If the body has no hex run (older releases pre-dating the
    template), we return None and the download just skips verification —
    HTTPS still protects against tampering, just not against CDN-cache
    corruption.
    """
    m = _SHA256_RE.search(body or "")
    if not m:
        return None
    return m.group(1).lower()


def _temp_dir() -> Path:
    """Where downloaded installers go. %TEMP% on Windows so Windows'
    own temp-cleanup eventually reclaims the disk, in case our app
    crashes between download and launch."""
    return Path(tempfile.gettempdir())
