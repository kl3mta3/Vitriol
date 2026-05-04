"""Convert one file. Picks the right handler(s) based on registries."""
from __future__ import annotations
from pathlib import Path
from typing import Callable, Optional

from .. import format_handlers as fh
from .intermediate import ADAPTERS
from .file_detector import normalize_ext
from ..utils.cancellation import CancellationToken


class UnsupportedConversionError(Exception):
    pass


def convert_file(
    src: Path,
    dst: Path,
    src_ext: str,
    dst_ext: str,
    cancel: CancellationToken,
    progress: Optional[Callable[[float], None]] = None,
    warnings: Optional[list] = None,
    masquerade: bool = False,
) -> None:
    """Run a single conversion. Raises UnsupportedConversionError on bad pairs.

    Handlers may push human-readable warning strings onto `warnings` (e.g.
    'Limited text extracted — PDF may be scanned'). The queue forwards these
    to the UI's status bar.

    When `masquerade` is True, routes byte-passthrough conversions through
    the masquerade engine instead of the regular handlers.
    """
    src_ext = normalize_ext(src_ext)
    dst_ext = normalize_ext(dst_ext)
    progress = progress or (lambda p: None)
    if warnings is None:
        warnings = []

    # Masquerade short-circuit: any pair where one side is a masquerade host
    # routes through the byte-envelope engine.
    if masquerade:
        from ..format_handlers import masquerade as _msq
        if _msq.can_embed_into(dst_ext) or _msq.can_extract_from(src_ext):
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress)
            return

    media_src = fh.MEDIA_HANDLERS.get(src_ext)
    media_dst = fh.MEDIA_HANDLERS.get(dst_ext)

    # Media path: same media module owns the whole conversion.
    if media_src and media_dst and media_src is media_dst:
        media_src.convert(src, dst, src_ext, dst_ext, cancel, progress)
        return
    # Special case: video -> audio (same module = audio_video).
    if media_src and media_dst and getattr(media_src, "MEDIA_CATEGORY", "") in ("audio", "video") \
            and getattr(media_dst, "MEDIA_CATEGORY", "") in ("audio", "video"):
        media_src.convert(src, dst, src_ext, dst_ext, cancel, progress)
        return
    if media_src or media_dst:
        raise UnsupportedConversionError(
            f"Cannot convert {src_ext} → {dst_ext}: media and document categories don't mix."
        )

    reader = fh.READERS.get(src_ext)
    writer = fh.WRITERS.get(dst_ext)
    if reader is None:
        raise UnsupportedConversionError(f"No reader for {src_ext}.")
    if writer is None:
        raise UnsupportedConversionError(f"No writer for {dst_ext}.")

    cancel.check()
    progress(0.05)
    doc = reader.read(src, src_ext, cancel)
    # Collect warnings handlers stashed on metadata.
    meta = getattr(doc, "metadata", None)
    if isinstance(meta, dict):
        for w in meta.get("warnings", ()) or ():
            warnings.append(str(w))
    progress(0.5)
    cancel.check()

    src_kind = getattr(reader, "DOC_KIND", "text")
    dst_kind = getattr(writer, "DOC_KIND", "text")
    if src_kind != dst_kind:
        adapter = ADAPTERS.get((src_kind, dst_kind))
        if adapter is None:
            raise UnsupportedConversionError(
                f"No adapter from {src_kind} to {dst_kind} for {src_ext} → {dst_ext}."
            )
        doc = adapter(doc)
    progress(0.7)
    cancel.check()
    writer.write(doc, dst, dst_ext, cancel)
    progress(1.0)
