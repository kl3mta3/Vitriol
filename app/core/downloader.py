"""urllib-based downloader with progress, checksum verification, and cancellation.

Pure stdlib — no requests, no httpx.
"""
from __future__ import annotations
import hashlib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from ..utils.cancellation import CancellationToken, CancelledError
from ..utils.logger import get_logger

_log = get_logger()


class ChecksumError(Exception):
    pass


def download_with_progress(
    url: str,
    dst: Path,
    expected_sha256: Optional[str] = None,
    cancel: Optional[CancellationToken] = None,
    progress: Optional[Callable[[float], None]] = None,
    chunk: int = 64 * 1024,
    timeout: int = 30,
) -> Path:
    """Download `url` to `dst`. Verifies SHA-256 if provided. Returns dst."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    progress = progress or (lambda p: None)
    _log.info("downloading %s -> %s", url, dst)

    req = urllib.request.Request(url, headers={"User-Agent": "Vitriol/0.1"})
    hasher = hashlib.sha256()
    total = 0
    expected_total = 0
    tmp = dst.with_suffix(dst.suffix + ".part")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                expected_total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                expected_total = 0
            with open(tmp, "wb") as out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError()
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    out.write(buf)
                    hasher.update(buf)
                    total += len(buf)
                    if expected_total > 0:
                        progress(min(1.0, total / expected_total))
        if expected_sha256:
            digest = hasher.hexdigest().lower()
            if digest != expected_sha256.lower():
                raise ChecksumError(f"sha256 {digest} != expected {expected_sha256}")
        else:
            _log.warning("download %s has no expected checksum (skipped verification)", url)
        tmp.replace(dst)
        return dst
    except urllib.error.URLError as e:
        raise RuntimeError(f"download failed: {e}") from e
    finally:
        if tmp.exists() and not dst.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
