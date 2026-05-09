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
    """True iff src and dst belong to different media categories.
    Non-media extensions (text/tabular/binary) all count as 'doc'.
    Used to drive the aesthetic encoders in masquerade.convert
    (Mandelbrot for image targets, music for audio targets)."""
    src_cat = fh.MEDIA_CATEGORY_OF.get(src_ext, "doc")
    dst_cat = fh.MEDIA_CATEGORY_OF.get(dst_ext, "doc")
    return src_cat != dst_cat


def _try_trailer_envelope(src: Path, src_ext: str):
    """Dispatch to the per-handler trailer-envelope probe. Returns
    (payload, src_ext) on success or None. Used by the doc -> media
    short-circuit to make PNG -> PDF -> PNG round-trip without Stone."""
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

    Handlers may push human-readable warning strings onto `warnings` (e.g.
    'Limited text extracted — PDF may be scanned'). The queue forwards these
    to the UI's status bar.

    When `masquerade` is True, routes byte-passthrough conversions through
    the masquerade engine instead of the regular handlers.

    When `compiler` is True AND the destination is `.py`, the source is
    embedded into a self-extracting Stone .py script. Independent of the
    global `masquerade` toggle — a per-row option for .py output only.
    """
    src_ext = normalize_ext(src_ext)
    dst_ext = normalize_ext(dst_ext)
    progress = progress or (lambda p: None)
    if warnings is None:
        warnings = []

    # --- Source-type policy enforcement -----------------------------------
    # See `app/format_handlers/__init__.py` STONE_ONLY_SOURCES and
    # AUTO_EXECUTE_EXTS for the rationale. The dropdown filter in
    # `valid_targets_for` enforces these rules at UI time; we re-enforce
    # here as defense-in-depth so a programmatic caller (or a future
    # caller that bypasses the UI) can't bypass the policy.

    # Block auto-execute -> auto-execute conversions (.py/.exe both ends).
    # This is the malware-pipeline guard. Wrapping a Python payload as a
    # self-extracting .exe (or vice versa) produces a single-click
    # auto-runner, which Vitriol must not enable. .zip, image, audio, etc.
    # remain valid sources for .py / .exe targets because their contents
    # don't auto-execute on extraction (the user must take a manual step).
    if src_ext in fh.AUTO_EXECUTE_EXTS and dst_ext in fh.AUTO_EXECUTE_EXTS:
        # ASCII-only message (no unicode arrows or em-dashes) so the error
        # renders correctly when shown in legacy Windows consoles (cp1252)
        # via crash dumps, log files, or `print(exc)` from a CLI invoker.
        raise UnsupportedConversionError(
            f"Vitriol does not allow {src_ext} -> {dst_ext} conversions. "
            ".py and .exe cannot be both the source AND target of the "
            "same conversion -- this would let the tool be used as a "
            "malware-wrapping pipeline. Convert your source to a non-"
            "executable Stone host first (.zip / .png / .wav / etc.), "
            "then convert that to your final target if needed."
        )

    # Auto-engage Stone for sources whose only meaningful operation is
    # Stone-mode (currently .zip and .exe). Without this, a programmatic
    # caller passing masquerade=False with a .zip source would fail
    # downstream — Vitriol has no non-Stone path for these formats.
    if src_ext in fh.STONE_ONLY_SOURCES:
        masquerade = True

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

    # Trailer-envelope round-trip short-circuit. If the source is a doc
    # format (PDF / DOCX / EPUB) that carries a Vitriol trailer envelope
    # AND the target is a media format, recover the original source bytes
    # directly from the trailer. This makes PNG -> PDF -> PNG byte-perfect
    # WITHOUT requiring the Stone toggle, and supersedes both the Stone
    # engagement and the regular reader (which would re-encode lossily).
    media_dst_probe = fh.MEDIA_HANDLERS.get(dst_ext)
    if media_dst_probe is not None and src_ext in (".pdf", ".docx", ".epub"):
        origin = _try_trailer_envelope(src, src_ext)
        if origin is not None:
            payload, recovered_ext = origin
            recovered_ext = normalize_ext(recovered_ext)
            if recovered_ext == dst_ext:
                # Exact match: write payload as-is.
                dst.write_bytes(payload)
                progress(1.0)
                return
            # Different target type. Try to re-encode via the recovered
            # source's media handler — but only if both the recovered ext
            # and the requested ext belong to the SAME media category
            # (e.g. PNG trailer recovered, JPG requested → both image,
            # image_handler can do PNG → JPG). If the categories differ
            # (e.g. PNG trailer recovered, GLB requested), refuse with a
            # clear error rather than silently falling through to a path
            # that would either crash or produce garbage.
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

    # Compiler short-circuit (per-row, .py target only). Embeds the source
    # bytes into a self-extracting Python script via the masquerade engine's
    # .py host. Independent of the global Stone toggle — the user opts into
    # this per-row by ticking "Compiler" when the target is .py. Lossy
    # sources are excluded for the same reason as normal Stone (the round
    # trip would not be meaningful).
    if compiler and dst_ext == ".py":
        from ..format_handlers import masquerade as _msq
        if not _msq.is_lossy(src_ext):
            # .py is always a 'doc' target — cross_category is True iff
            # the source is media. Doesn't change behavior for .py (the
            # self-extracting script is identical either way) but keeps
            # the contract uniform.
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
            return
        # Lossy source + Compiler ON would silently fall through to a
        # placeholder text bundle that bears no resemblance to what the
        # user expected (a runnable .py reconstructing the original).
        # Refuse explicitly instead.
        raise UnsupportedConversionError(
            f"Compiler mode requires a lossless source. {src_ext} is a "
            "lossy format (the round-trip would not reconstruct the "
            "original). Untick Compiler to convert as plain text, or "
            "convert your source to a lossless format first."
        )

    # Philosopher's Stone short-circuit. Two engagement conditions:
    #   1. dst is a Stone host (we want to embed src bytes into it) AND src
    #      is not a lossy format (lossy sources are excluded from Stone).
    #   2. src is a Stone-host extension AND it actually contains a UCMSv2 or
    #      UCMSv1 envelope (we want to extract). A vanilla PNG / WAV / etc.
    #      with no envelope falls through to the regular handler so PNG → JPG,
    #      WAV → MP3, etc. keep working normally even with Stone enabled.
    #
    # Same-category-media exception (PNG→JPG, FBX→GLB, MP3→WAV, etc.):
    # the dropdown UI already advertises these as normal conversions —
    # turning the Stone toggle on shouldn't reroute them through the
    # carrier path. The embed branch is suppressed for same-handler
    # conversions; the extract branch is left alive so dropping a
    # Stone-mode .png back in (with .png target) still unwraps the
    # hidden source. Cross-category conversions (image→pdf, doc→png,
    # anything→.py/.exe/.zip) are unaffected — Stone fires as before.
    if masquerade:
        from ..format_handlers import masquerade as _msq

        # Both src and dst belong to the SAME media handler module —
        # i.e., it's image→image, audio→audio, video→video, or
        # model→model. The handler can do the conversion natively;
        # there's nothing to gain from wrapping the result through Stone.
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

    # Media path: same media module owns the whole conversion.
    # Model handler accepts an extra preserve_animations kwarg; other
    # media handlers don't, so we gate the kwarg on MEDIA_CATEGORY.
    if media_src and media_dst and media_src is media_dst:
        if getattr(media_src, "MEDIA_CATEGORY", "") == "model":
            media_src.convert(src, dst, src_ext, dst_ext, cancel, progress,
                              preserve_animations=preserve_animations)
        else:
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

    # Cross-category: document source → media target. True rasterization
    # (PDF→PNG pixels) or text-to-speech synthesis (PDF→WAV audio) is out
    # of scope, but the user's contract is that any conversion should
    # succeed and be recoverable. So for doc → media we auto-engage the
    # Stone envelope when the destination is a Stone host (PNG/BMP/WAV/MKV):
    # the source bytes are embedded into a valid container of the target
    # type, and converting back through Vitriol extracts the original.
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
                "byte-perfect but only round-trips through Vitriol."
            )
            _msq.convert(src, dst, src_ext, dst_ext, cancel, progress,
                         cross_category=_is_cross_category(src_ext, dst_ext),
                         password=password)
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
