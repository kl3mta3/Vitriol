"""Convert one file. Picks the right handler(s) based on registries."""
from __future__ import annotations
from pathlib import Path
from typing import Callable, Optional

from .. import format_handlers as fh
from .intermediate import ADAPTERS
from .file_detector import normalize_ext
from .config import streaming_threshold, free_disk_for, DISK_SPACE_WARN_MULT
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

    # Disk-space sanity check (cheap, no popup — just a status-bar hint).
    try:
        src_size = src.stat().st_size
        free = free_disk_for(dst.parent)
        # Worst-case output size estimate: input × 2 (covers compressed → raw,
        # base64 expansion, image rasterization, etc).
        if src_size * 2 * DISK_SPACE_WARN_MULT > free and src_size > 100 * 1024 * 1024:
            warnings.append(
                f"Low disk space at output target: {free // (1024*1024)} MB free; "
                f"conversion may need up to {(src_size * 2) // (1024*1024)} MB."
            )
    except OSError:
        src_size = 0

    # Philosopher's Stone short-circuit. Two engagement conditions:
    #   1. dst is a Stone host (we want to embed src bytes into it) AND src
    #      is not a lossy format (lossy sources are excluded from Stone).
    #   2. src is a Stone-host extension AND it actually contains a UCMSv2 or
    #      UCMSv1 envelope (we want to extract). A vanilla PNG / WAV / etc.
    #      with no envelope falls through to the regular handler so PNG → JPG,
    #      WAV → MP3, etc. keep working normally even with Stone enabled.
    if masquerade:
        from ..format_handlers import masquerade as _msq
        engage = False
        if _msq.can_embed_into(dst_ext) and not _msq.is_lossy(src_ext):
            engage = True
        elif _msq.can_extract_from(src_ext) and _msq.has_envelope(src, src_ext):
            engage = True
        if engage:
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

    # Cross-category: image source → document target.
    # Wrap the image bytes in a single-block TextDoc and hand off to the
    # document writer (pdf_write embeds it as XObject, markdown/html bundle
    # it via images/ folder, txt falls back to "[image]" placeholder).
    if media_src and not media_dst:
        cat = getattr(media_src, "MEDIA_CATEGORY", "")
        if cat == "image":
            writer = fh.WRITERS.get(dst_ext)
            if writer is not None:
                from .intermediate import image_bytes_to_textdoc
                progress(0.05)
                doc = image_bytes_to_textdoc(src.read_bytes(), src_ext,
                                              alt=src.stem)
                cancel.check()
                progress(0.5)
                writer.write(doc, dst, dst_ext, cancel)
                progress(1.0)
                return
        raise UnsupportedConversionError(
            f"Cannot convert {src_ext} → {dst_ext}: no cross-category "
            f"adapter for {cat} → {dst_ext}."
        )

    # Cross-category: document source → image target requires rasterization
    # (rendering a PDF/HTML page to pixels). Out of scope this round —
    # suggest Stone byte-passthrough as the workaround.
    if not media_src and media_dst:
        if getattr(media_dst, "MEDIA_CATEGORY", "") == "image":
            raise UnsupportedConversionError(
                f"Rendering {src_ext} → {dst_ext} requires document "
                "rasterization, not supported in v1. Try Philosopher's Stone "
                "for byte-passthrough instead."
            )
        raise UnsupportedConversionError(
            f"Cannot convert {src_ext} → {dst_ext}: media and document "
            "categories don't mix for this pair."
        )

    reader = fh.READERS.get(src_ext)
    writer = fh.WRITERS.get(dst_ext)
    if reader is None:
        raise UnsupportedConversionError(f"No reader for {src_ext}.")
    if writer is None:
        raise UnsupportedConversionError(f"No writer for {dst_ext}.")

    # Same-handler plain-text pass-through fast path: e.g. txt → py / log
    # is just a file copy that preserves bytes exactly. The whole-file IR
    # path mangles whitespace by joining paragraphs with `\n\n`, so we
    # short-circuit if the handler tells us this pair can stream.
    if reader is writer:
        can_stream = getattr(reader, "can_stream", None)
        sc = getattr(reader, "stream_convert", None)
        if callable(can_stream) and callable(sc) and can_stream(src_ext, dst_ext):
            sc(src, dst, src_ext, dst_ext, cancel, progress)
            return

    # Streaming branch: above the per-format threshold, prefer streaming.
    threshold = min(streaming_threshold(src_ext), streaming_threshold(dst_ext))
    if src_size > threshold:
        same_handler = reader is writer
        # Both sides streamable? Use stream_convert if same module exposes it,
        # else fall through to whole-file (cross-handler chained streaming is
        # out of scope for this round).
        r_streamable = getattr(reader, "STREAMABLE", False)
        w_streamable = getattr(writer, "STREAMABLE", False)
        same_can_stream = (same_handler and r_streamable
                            and hasattr(reader, "stream_convert"))
        if same_can_stream:
            # Optionally check the handler's own can_stream() guard
            ok = True
            cs = getattr(reader, "can_stream", None)
            if callable(cs):
                ok = bool(cs(src_ext, dst_ext))
            if ok:
                reader.stream_convert(src, dst, src_ext, dst_ext, cancel, progress)
                return
        # Auto-fallback to byte-masquerade. Status-bar message explains it.
        from ..format_handlers import masquerade as _msq
        if _msq.can_embed_into(dst_ext) and not _msq.is_lossy(src_ext):
            warnings.append(
                "File too large for direct conversion and handler cannot "
                "stream — used byte-masquerade as fallback. Output is "
                "byte-perfect but only round-trips through Transmute."
            )
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress)
            return
        # No fallback available — let the whole-file path try and probably OOM
        # with a clear traceback in the log.

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
