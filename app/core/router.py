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


def _is_cross_category(src_ext: str, dst_ext: str) -> bool:
    """True if src and dst are different media categories. Non-media
    extensions all count as 'doc'."""
    src_cat = fh.MEDIA_CATEGORY_OF.get(src_ext, "doc")
    dst_cat = fh.MEDIA_CATEGORY_OF.get(dst_ext, "doc")
    return src_cat != dst_cat


def _try_trailer_envelope(src: Path, src_ext: str):
    """Probe a doc source for a Vitriol trailer envelope. Returns
    (payload, src_ext) or None."""
    if src_ext == ".pdf":
        from ..format_handlers.pdf_read import _try_read_trailer_envelope
        return _try_read_trailer_envelope(src)
    if src_ext in (".docx", ".epub"):
        try:
            import zipfile
            with zipfile.ZipFile(src) as z:
                if "_vitriol/original.bin" in z.namelist():
                    from ..format_handlers.masquerade import _parse_envelope
                    return _parse_envelope(z.read("_vitriol/original.bin"))
        except (zipfile.BadZipFile, KeyError, ValueError, OSError):
            return None
    return None


def convert_file(
    src: Path,
    dst: Path,
    src_ext: str,
    dst_ext: str,
    cancel: CancellationToken,
    progress: Optional[Callable[[float], None]] = None,
    warnings: Optional[list] = None,
    masquerade: bool = False,
    compiler: bool = False,
    password: bytes = b"",
    preserve_animations: bool = False,
) -> None:
    """Run a single conversion. Raises UnsupportedConversionError on bad pairs.

    Handler warnings are appended to `warnings` for the UI status bar.

    `masquerade=True` routes byte-passthrough conversions through the
    masquerade engine. `compiler=True` with `dst_ext == ".py"` embeds the
    source in a self-extracting Stone .py.
    """
    src_ext = normalize_ext(src_ext)
    dst_ext = normalize_ext(dst_ext)
    progress = progress or (lambda p: None)
    if warnings is None:
        warnings = []

    # --- Source-type policy enforcement -----------------------------------
    # Re-enforces the dropdown filter from `format_handlers.__init__` so a
    # programmatic caller can't bypass it.

    # Block .py↔.exe both-ends to prevent malware-pipeline use.
    if src_ext in fh.AUTO_EXECUTE_EXTS and dst_ext in fh.AUTO_EXECUTE_EXTS:
        # ASCII-only for legacy Windows console rendering.
        raise UnsupportedConversionError(
            f"Vitriol does not allow {src_ext} -> {dst_ext} conversions. "
            ".py and .exe cannot be both the source AND target of the "
            "same conversion -- this would let the tool be used as a "
            "malware-wrapping pipeline. Convert your source to a non-"
            "executable Stone host first (.zip / .png / .wav / etc.), "
            "then convert that to your final target if needed."
        )

    # Stone-only sources (.zip, .exe) have no non-Stone path.
    if src_ext in fh.STONE_ONLY_SOURCES:
        masquerade = True

    # Disk-space hint (status bar only, no popup).
    try:
        src_size = src.stat().st_size
        free = free_disk_for(dst.parent)
        # Worst-case output ≈ 2x input.
        if src_size * 2 * DISK_SPACE_WARN_MULT > free and src_size > 100 * 1024 * 1024:
            warnings.append(
                f"Low disk space at output target: {free // (1024*1024)} MB free; "
                f"conversion may need up to {(src_size * 2) // (1024*1024)} MB."
            )
    except OSError:
        src_size = 0

    # Trailer-envelope short-circuit: PDF/DOCX/EPUB carrying a Vitriol
    # trailer envelope, with a media target, recovers the original source
    # bytes directly. Makes PNG → PDF → PNG byte-perfect without Stone.
    media_dst_probe = fh.MEDIA_HANDLERS.get(dst_ext)
    if media_dst_probe is not None and src_ext in (".pdf", ".docx", ".epub"):
        origin = _try_trailer_envelope(src, src_ext)
        if origin is not None:
            payload, recovered_ext = origin
            recovered_ext = normalize_ext(recovered_ext)
            if recovered_ext == dst_ext:
                dst.write_bytes(payload)
                progress(1.0)
                return
            # Different target type — re-encode via the recovered source's
            # media handler, but only if both extensions belong to the
            # same handler.
            recovered_handler = fh.MEDIA_HANDLERS.get(recovered_ext)
            if (recovered_handler is not None
                    and recovered_handler is media_dst_probe
                    and recovered_ext in getattr(recovered_handler, "SUPPORTED", set())
                    and dst_ext in getattr(recovered_handler, "SUPPORTED", set())):
                import tempfile
                tmp_path = Path(tempfile.mkstemp(suffix=recovered_ext)[1])
                try:
                    tmp_path.write_bytes(payload)
                    recovered_handler.convert(
                        tmp_path, dst, recovered_ext, dst_ext, cancel, progress)
                    return
                finally:
                    try: tmp_path.unlink()
                    except OSError: pass
            raise UnsupportedConversionError(
                f"Cannot convert {src_ext} → {dst_ext}: this {src_ext} carries "
                f"a recoverable {recovered_ext} payload, but {recovered_ext} → "
                f"{dst_ext} crosses media categories (or the handler doesn't "
                "support both ends). Convert to "
                f"{recovered_ext} first, then to {dst_ext}."
            )

    # Compiler short-circuit (.py target only). Embeds source bytes into
    # a self-extracting Python script via masquerade. Lossy sources excluded.
    if compiler and dst_ext == ".py":
        from ..format_handlers import masquerade as _msq
        if not _msq.is_lossy(src_ext):
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
            return
        raise UnsupportedConversionError(
            f"Compiler mode requires a lossless source. {src_ext} is a "
            "lossy format (the round-trip would not reconstruct the "
            "original). Untick Compiler to convert as plain text, or "
            "convert your source to a lossless format first."
        )

    # Philosopher's Stone short-circuit. Engages on:
    #   - embed: dst is a Stone host, src is non-lossy, NOT same-handler
    #     same-category (PNG→JPG, FBX→GLB, etc. are normal conversions).
    #   - extract: src is a Stone host that actually carries a UCMSv*
    #     envelope.
    if masquerade:
        from ..format_handlers import masquerade as _msq

        same_media_handler = (
            src_ext in fh.MEDIA_HANDLERS
            and dst_ext in fh.MEDIA_HANDLERS
            and fh.MEDIA_HANDLERS[src_ext] is fh.MEDIA_HANDLERS[dst_ext]
        )

        engage = False
        if (not same_media_handler
                and _msq.can_embed_into(dst_ext)
                and not _msq.is_lossy(src_ext)):
            engage = True
        elif _msq.can_extract_from(src_ext) and _msq.has_envelope(src, src_ext):
            engage = True
        if engage:
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
            return

    media_src = fh.MEDIA_HANDLERS.get(src_ext)
    media_dst = fh.MEDIA_HANDLERS.get(dst_ext)

    # Media path: same module handles the conversion. Model handler is
    # the only one that takes preserve_animations.
    if media_src and media_dst and media_src is media_dst:
        if getattr(media_src, "MEDIA_CATEGORY", "") == "model":
            media_src.convert(src, dst, src_ext, dst_ext, cancel, progress,
                              preserve_animations=preserve_animations)
        else:
            media_src.convert(src, dst, src_ext, dst_ext, cancel, progress)
        return
    # video → audio (same module = audio_video).
    if media_src and media_dst and getattr(media_src, "MEDIA_CATEGORY", "") in ("audio", "video") \
            and getattr(media_dst, "MEDIA_CATEGORY", "") in ("audio", "video"):
        media_src.convert(src, dst, src_ext, dst_ext, cancel, progress)
        return

    # Cross-category: image source → document target. Wrap bytes in a
    # single-block TextDoc and hand off to the document writer.
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

    # Cross-category: document source → media target. Auto-engage Stone
    # for Stone-host destinations so the conversion is recoverable.
    if not media_src and media_dst:
        from ..format_handlers import masquerade as _msq
        if _msq.can_embed_into(dst_ext) and not _msq.is_lossy(src_ext):
            warnings.append(
                f"{src_ext} → {dst_ext} has no semantic conversion path; "
                "embedded the source via Philosopher's Stone. Convert the "
                "output back through Vitriol to recover the original."
            )
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
            return
        raise UnsupportedConversionError(
            f"Cannot convert {src_ext} → {dst_ext}: no semantic path and "
            f"{dst_ext} is not a Philosopher's Stone host."
        )

    reader = fh.READERS.get(src_ext)
    writer = fh.WRITERS.get(dst_ext)
    if reader is None:
        raise UnsupportedConversionError(f"No reader for {src_ext}.")
    if writer is None:
        raise UnsupportedConversionError(f"No writer for {dst_ext}.")

    # Same-handler plain-text pass-through (txt → py / log etc.).
    # Whole-file IR path mangles whitespace; stream_convert preserves bytes.
    if reader is writer:
        can_stream = getattr(reader, "can_stream", None)
        sc = getattr(reader, "stream_convert", None)
        if callable(can_stream) and callable(sc) and can_stream(src_ext, dst_ext):
            sc(src, dst, src_ext, dst_ext, cancel, progress)
            return

    # Streaming branch for sources above the per-format threshold.
    threshold = min(streaming_threshold(src_ext), streaming_threshold(dst_ext))
    if src_size > threshold:
        same_handler = reader is writer
        r_streamable = getattr(reader, "STREAMABLE", False)
        w_streamable = getattr(writer, "STREAMABLE", False)
        same_can_stream = (same_handler and r_streamable
                            and hasattr(reader, "stream_convert"))
        if same_can_stream:
            ok = True
            cs = getattr(reader, "can_stream", None)
            if callable(cs):
                ok = bool(cs(src_ext, dst_ext))
            if ok:
                reader.stream_convert(src, dst, src_ext, dst_ext, cancel, progress)
                return
        # Fallback to byte-masquerade with a status-bar warning.
        from ..format_handlers import masquerade as _msq
        if _msq.can_embed_into(dst_ext) and not _msq.is_lossy(src_ext):
            warnings.append(
                "File too large for direct conversion and handler cannot "
                "stream — used byte-masquerade as fallback. Output is "
                "byte-perfect but only round-trips through Vitriol."
            )
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
            return
        # No fallback — fall through to whole-file path.

    cancel.check()
    progress(0.05)
    doc = reader.read(src, src_ext, cancel)
    # Forward handler-stashed metadata warnings.
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
