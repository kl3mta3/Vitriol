"""Archive conversions: zip, 7z, tar, tar.gz, tar.bz2, tar.xz, tar.zst.

Comic-book extensions (cbz=zip, cb7=7z) are aliased onto the same code
paths so a CBZ source can convert to ZIP, CB7, etc.

CBR (RAR) is **read-only** when the `unrar` binary is available on PATH;
otherwise it raises a clear error. Writing RAR is never supported (the RAR
spec is closed and the unrar tool is decode-only).

Conversion strategy: extract to a temp directory, repack into the new
container. This loses any compression metadata that doesn't translate
between formats (e.g. 7z's LZMA2 filter chain becomes plain DEFLATE in
ZIP), but the file tree itself is preserved.

DOC_KIND="archive" keeps archives in their own group — they only convert
among themselves. Stone-mode (.zip as host) still works because the router
checks for a Stone envelope before falling through to the archive handler.
"""
from __future__ import annotations
import io
import os
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..utils.cancellation import CancellationToken
from ..utils.logger import get_logger

_log = get_logger()

# Extension → handler kind. Comic books reuse the underlying archive code.
_KIND_FOR_EXT = {
    ".zip": "zip",
    ".cbz": "zip",
    ".7z": "7z",
    ".cb7": "7z",
    ".tar": "tar",
    ".tar.gz": "tar.gz",
    ".tar.bz2": "tar.bz2",
    ".tar.xz": "tar.xz",
    ".tar.zst": "tar.zst",
    # Read-only:
    ".cbr": "rar",
    ".rar": "rar",
}

# Extensions we can READ.
SUPPORTED_READ = set(_KIND_FOR_EXT.keys())
# Extensions we can WRITE — RAR is excluded.
SUPPORTED_WRITE = {e for e, k in _KIND_FOR_EXT.items() if k != "rar"}
DOC_KIND = "archive"


@dataclass
class ArchiveDoc:
    """Pointer to the source archive on disk plus its detected format.

    We never load the contents into RAM up front — extraction happens during
    `write` into a temp dir, then the temp dir is repacked. This keeps memory
    bounded for multi-gigabyte archives.
    """
    src_path: Path
    src_kind: str  # one of "zip", "7z", "tar", "tar.gz", "tar.bz2", "tar.xz", "tar.zst", "rar"


def read(path: Path, ext: str, cancel: CancellationToken) -> ArchiveDoc:
    kind = _KIND_FOR_EXT.get(ext)
    if kind is None:
        raise RuntimeError(f"Unsupported archive source: {ext}")
    return ArchiveDoc(src_path=path, src_kind=kind)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, ArchiveDoc):
        raise RuntimeError("Archive writer requires an ArchiveDoc.")
    dst_kind = _KIND_FOR_EXT.get(ext)
    if dst_kind is None:
        raise RuntimeError(f"Unsupported archive target: {ext}")
    if dst_kind == "rar":
        raise RuntimeError(
            "Writing RAR is not supported (the RAR format is closed-source; "
            "the unrar tool only decodes)."
        )

    # Same kind: byte passthrough — preserves all original metadata.
    if doc.src_kind == dst_kind:
        shutil.copyfile(doc.src_path, path)
        return

    with tempfile.TemporaryDirectory(prefix="vitriol-archive-") as tmp:
        tmp_path = Path(tmp)
        cancel.check()
        _extract(doc.src_path, doc.src_kind, tmp_path)
        cancel.check()
        _repack(tmp_path, path, dst_kind)


# ---- extract -------------------------------------------------------------

def _extract(src: Path, kind: str, dst_dir: Path) -> None:
    if kind == "zip":
        with zipfile.ZipFile(src) as z:
            z.extractall(dst_dir)
        return
    if kind == "7z":
        import py7zr
        with py7zr.SevenZipFile(src, mode="r") as z:
            z.extractall(path=str(dst_dir))
        return
    if kind in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
        # tarfile auto-detects the compression on read.
        with tarfile.open(src, "r:*") as t:
            _safe_extract_tar(t, dst_dir)
        return
    if kind == "tar.zst":
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        with open(src, "rb") as fi, dctx.stream_reader(fi) as reader:
            # tarfile needs a seekable stream OR a known mode. Use streaming
            # mode "r|" which reads sequentially.
            with tarfile.open(fileobj=reader, mode="r|") as t:
                _safe_extract_tar(t, dst_dir)
        return
    if kind == "rar":
        _extract_rar(src, dst_dir)
        return
    raise RuntimeError(f"Unknown extract kind: {kind}")


def _safe_extract_tar(t: tarfile.TarFile, dst_dir: Path) -> None:
    """tarfile extraction with a path-traversal guard. Python 3.12+ has
    `filter='data'` for this; we use it when available, otherwise we filter
    members ourselves before extracting."""
    if hasattr(tarfile, "data_filter"):
        # Python 3.12+: explicit safe filter rejects absolute / traversing
        # paths and unsafe device nodes. Streaming mode is fine here.
        try:
            t.extractall(dst_dir, filter="data")
            return
        except TypeError:
            pass
    # Fallback: enumerate members and skip any whose resolved path escapes
    # dst_dir. Only safe in seekable mode (random-access getmembers()).
    dst_resolved = dst_dir.resolve()
    members = []
    for m in t.getmembers():
        target = (dst_dir / m.name).resolve()
        try:
            target.relative_to(dst_resolved)
        except ValueError:
            _log.warning("skipping tar member outside dest: %s", m.name)
            continue
        members.append(m)
    t.extractall(dst_dir, members=members)


def _extract_rar(src: Path, dst_dir: Path) -> None:
    try:
        import rarfile  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Reading RAR/CBR requires the 'rarfile' package and an external "
            "'unrar' binary. pip install rarfile and ensure unrar is on PATH."
        ) from e
    try:
        with rarfile.RarFile(src) as r:
            r.extractall(dst_dir)
    except rarfile.RarCannotExec as e:
        raise RuntimeError(
            f"unrar binary not found on PATH. Install unrar to read RAR/CBR files. ({e})"
        ) from e


# ---- repack --------------------------------------------------------------

def _walk_files(root: Path):
    """Yield (absolute_path, posix_relative_name) for every file under root."""
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            yield full, rel


def _repack(src_dir: Path, dst: Path, kind: str) -> None:
    if kind == "zip":
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for full, rel in _walk_files(src_dir):
                z.write(full, rel)
        return
    if kind == "7z":
        import py7zr
        with py7zr.SevenZipFile(dst, mode="w") as z:
            for full, rel in _walk_files(src_dir):
                z.write(full, rel)
        return
    if kind in ("tar", "tar.gz", "tar.bz2", "tar.xz"):
        mode = {"tar": "w", "tar.gz": "w:gz", "tar.bz2": "w:bz2", "tar.xz": "w:xz"}[kind]
        with tarfile.open(dst, mode) as t:
            for full, rel in _walk_files(src_dir):
                t.add(str(full), arcname=rel)
        return
    if kind == "tar.zst":
        import zstandard as zstd
        cctx = zstd.ZstdCompressor(level=10)
        with open(dst, "wb") as fo, cctx.stream_writer(fo) as writer:
            with tarfile.open(fileobj=writer, mode="w|") as t:
                for full, rel in _walk_files(src_dir):
                    t.add(str(full), arcname=rel)
        return
    raise RuntimeError(f"Unknown repack kind: {kind}")
