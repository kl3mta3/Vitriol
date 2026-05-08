"""Tiny stdlib-only HTTPS helper for fetching small JSON manifests.

For larger downloads with progress + cancellation + SHA-256 verification,
use `app.core.downloader.download_with_progress` instead — that's the
canonical download path used by the launcher and the auto-updater.

This module exists for the one shape `downloader` doesn't cover: a small
GET that returns a parsed JSON object (used to fetch the GitHub Releases
API manifest at app launch).
"""
from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any, Optional

from .logger import get_logger
from ..__version__ import __version__

_log = get_logger()


# Conservative timeout for one request — GitHub's API responds in <1 s
# normally, and a 10 s ceiling means a bad network never blocks the UI
# worker thread for long.
_API_TIMEOUT = 10

_USER_AGENT = f"Vitriol/{__version__}"


def fetch_json(url: str, timeout: float = _API_TIMEOUT) -> Optional[dict[str, Any]]:
    """GET `url`, parse the response as JSON, return the parsed dict.

    Returns None on any network or parse failure — callers treat this as
    "couldn't reach the server, try again later" rather than crashing the
    UI. All failures are logged at WARNING level.

    Used to fetch the GitHub Releases API manifest. We send a User-Agent
    matching our app+version because GitHub rejects unauthenticated API
    requests without one with a 403.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        _log.warning("fetch_json(%s) failed: %s", url, e)
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        _log.warning("fetch_json(%s) parse failed: %s", url, e)
        return None
    if not isinstance(parsed, dict):
        # GitHub's /releases/latest returns a single object; if we ever see
        # an array (e.g. /releases without /latest) we don't try to interpret it.
        _log.warning("fetch_json(%s) unexpected shape: %s", url, type(parsed).__name__)
        return None
    return parsed
