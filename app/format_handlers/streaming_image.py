"""Streaming PNG and BMP encoders/decoders sized for the new tiered
image-masquerade format. The point is bounded memory: a 4 GiB envelope
encodes into an 8192×8192 PNG using <2× CHUNK_SIZE of working memory.

PNG layout we emit (matches PNG 1.2 spec):
    8-byte signature  \\x89PNG\\r\\n\\x1a\\n
    IHDR              width, height, bit_depth=8, color_type=2 (RGB),
                      compression=0, filter=0, interlace=0
    pHYs / gAMA / sRGB   neutral image-rendering metadata that real PNG
                          encoders almost always emit. We include them so
                          the file doesn't read as "synthetic, never went
                          through a real encoder" to a forensic eye —
                          bare-bones IHDR+IDAT+IEND is itself a tell.
    IDAT*             zlib-compressed scanlines, split into chunks of
                      ~CHUNK_SIZE so writers can flush incrementally
    IEND              terminator

Each scanline is `[filter byte F] + [width × 3 filtered RGB bytes]`.
PNG defines five filters: 0=None / 1=Sub / 2=Up / 3=Average / 4=Paeth.
Real PNG encoders pick the best filter per row for compression and
typically emit a *mix* across rows — Paeth-heavy on photos, Sub/Up on
artificial images. Always-filter-0 is itself a forensic tell, so we
randomize per-row from a Paeth-heavy distribution **seeded from a caller-
supplied byte string** (so encoding stays deterministic per source).
The decoder un-filters automatically; recovered pixels are byte-exact.

BMP layout (Windows V1, 24-bit RGB, top-down via negative height):
    14-byte BMP file header
    40-byte BITMAPINFOHEADER
    pixel rows (BGR order on disk, padded to 4-byte stride)

We feed the encoders an iterator yielding chunks of RGB bytes (in row-major
order) totalling exactly `width × height × 3`. Decoders return an iterator
yielding RGB chunks the same way.
"""
from __future__ import annotations
import hashlib
import random
import struct
import zlib
from pathlib import Path
from typing import Iterator, Iterable, Optional

from ..utils.cancellation import CancellationToken
from ..core.config import CHUNK_SIZE


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


# Filter selection weights tuned to mimic what real photo-encoders emit.
# Paeth is the most common in real PNGs because it predicts well on natural
# images. Sub and Up are next. None and Average appear less frequently.
# These don't have to match any specific encoder exactly — just be
# *plausibly* like one.
_PNG_FILTER_WEIGHTS = [
    (4, 35),   # Paeth
    (1, 25),   # Sub
    (2, 20),   # Up
    (3, 10),   # Average
    (0, 10),   # None
]


def _pick_filter(rng: random.Random) -> int:
    """Weighted random pick from `_PNG_FILTER_WEIGHTS`."""
    total = sum(w for _f, w in _PNG_FILTER_WEIGHTS)
    r = rng.randint(1, total)
    acc = 0
    for f, w in _PNG_FILTER_WEIGHTS:
        acc += w
        if r <= acc:
            return f
    return 0  # unreachable, defensive


def _apply_filter(filter_type: int, row: bytes, prev_row: bytes,
                   bytes_per_pixel: int = 3) -> bytes:
    """Apply a PNG filter to one scanline. Spec: PNG filtering computes
    `filtered[i] = row[i] - predictor[i]` (mod 256). The decoder reverses
    it. `bytes_per_pixel` = 3 for RGB at 8-bit depth.

    Implementation note: this is in the hot path for every PNG row we
    write. Pure-Python per-byte loops were ~2× slower than the encode
    overall on multi-megapixel images, so all filters are NumPy-vectorized
    using int16 arithmetic + cast back to uint8 (the spec's `mod 256` is
    just byte truncation, which `astype(uint8)` does)."""
    if filter_type == 0:  # None
        return row
    import numpy as np
    bpp = bytes_per_pixel
    r = np.frombuffer(row, dtype=np.uint8).astype(np.int16)
    if filter_type == 1:  # Sub: filt[i] = row[i] - row[i-bpp]
        left = np.zeros_like(r)
        left[bpp:] = r[:-bpp]
        out = (r - left).astype(np.uint8)
    elif filter_type == 2:  # Up: filt[i] = row[i] - prev[i]
        p = np.frombuffer(prev_row, dtype=np.uint8).astype(np.int16)
        out = (r - p).astype(np.uint8)
    elif filter_type == 3:  # Average: filt[i] = row[i] - (left + up) // 2
        p = np.frombuffer(prev_row, dtype=np.uint8).astype(np.int16)
        left = np.zeros_like(r)
        left[bpp:] = r[:-bpp]
        out = (r - (left + p) // 2).astype(np.uint8)
    elif filter_type == 4:  # Paeth
        p = np.frombuffer(prev_row, dtype=np.uint8).astype(np.int16)
        left = np.zeros_like(r)
        left[bpp:] = r[:-bpp]
        ul = np.zeros_like(r)
        ul[bpp:] = p[:-bpp]
        pred_p = left + p - ul
        pa = np.abs(pred_p - left)
        pb = np.abs(pred_p - p)
        pc = np.abs(pred_p - ul)
        # Paeth predictor: argmin(pa, pb, pc) → choose left/up/ul
        pred = np.where((pa <= pb) & (pa <= pc), left,
                          np.where(pb <= pc, p, ul))
        out = (r - pred).astype(np.uint8)
    else:
        raise ValueError(f"Unknown PNG filter type {filter_type}")
    return out.tobytes()


def _unapply_filter(filter_type: int, filtered: bytes, prev_row: bytes,
                     bytes_per_pixel: int = 3) -> bytes:
    """Inverse of `_apply_filter`. Recovers original row from filtered row."""
    if filter_type == 0:
        return filtered
    n = len(filtered)
    out = bytearray(n)
    if filter_type == 1:  # Sub
        for i in range(n):
            left = out[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            out[i] = (filtered[i] + left) & 0xFF
    elif filter_type == 2:  # Up
        for i in range(n):
            out[i] = (filtered[i] + prev_row[i]) & 0xFF
    elif filter_type == 3:  # Average
        for i in range(n):
            left = out[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            up = prev_row[i]
            out[i] = (filtered[i] + ((left + up) // 2)) & 0xFF
    elif filter_type == 4:  # Paeth
        for i in range(n):
            left = out[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            up = prev_row[i]
            ul = prev_row[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
            p = left + up - ul
            pa = abs(p - left); pb = abs(p - up); pc = abs(p - ul)
            if pa <= pb and pa <= pc: pred = left
            elif pb <= pc: pred = up
            else: pred = ul
            out[i] = (filtered[i] + pred) & 0xFF
    else:
        raise ValueError(f"Unknown PNG filter type {filter_type}")
    return bytes(out)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png_chunk_to_file(out, tag: bytes, data: bytes) -> None:
    out.write(struct.pack(">I", len(data)))
    out.write(tag)
    out.write(data)
    out.write(struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _write_ancillary_chunks(out) -> None:
    """Write neutral image-rendering metadata that universally appears in
    real PNGs (Photoshop, GIMP, Pillow, browsers). NO attribution chunks
    (`tEXt Software=`) — those would either misrepresent the source or
    leak our identity. The chunks here just describe pixel density and
    color space, which any image carrier could plausibly have."""
    # pHYs: pixels-per-meter X, Y, unit (1=meter). 3779 ≈ 96 DPI (Windows
    # screenshot default) — matches what most "made-from-scratch" PNGs say.
    pHYs = struct.pack(">IIB", 3779, 3779, 1)
    _png_chunk_to_file(out, b"pHYs", pHYs)
    # gAMA: gamma × 100,000. sRGB inverse gamma = 0.45455 → 45455.
    _png_chunk_to_file(out, b"gAMA", struct.pack(">I", 45455))
    # sRGB: rendering intent (0=Perceptual, 1=Relative, 2=Saturation, 3=Absolute).
    _png_chunk_to_file(out, b"sRGB", b"\x00")


def stream_png_write(dst: Path, width: int, height: int,
                      data_iter: Iterable[bytes],
                      cancel: Optional[CancellationToken] = None,
                      progress=None,
                      filter_seed: Optional[bytes] = None) -> None:
    """Write a PNG to `dst`. `data_iter` must yield exactly width*height*3
    bytes total (RGB row-major). Memory usage is bounded by CHUNK_SIZE +
    one scanline + zlib's internal window.

    `filter_seed`, when supplied, is hashed to seed a per-row filter
    picker. Each row gets one of the 5 PNG filters from a Paeth-heavy
    distribution (matches typical real-encoder output). Same seed →
    same filter sequence, so same source produces byte-identical PNG.
    When `filter_seed=None`, all rows use filter 0 (None) — the legacy
    behavior, kept as a fallback path for callers that don't supply
    a seed.

    The decoder un-filters automatically — recovered pixels are byte-
    exact. Ancillary chunks (pHYs/gAMA/sRGB) are always emitted so the
    file doesn't read as bare-bones programmatic output."""
    expected = width * height * 3
    bytes_per_row = width * 3
    compressor = zlib.compressobj(level=zlib.Z_DEFAULT_COMPRESSION)
    pending = bytearray()
    written = 0

    # Seed the filter picker. None → always filter 0 (legacy / no seed).
    rng: Optional[random.Random] = None
    if filter_seed is not None:
        seed_int = int.from_bytes(
            hashlib.sha256(filter_seed + b"png-filter").digest()[:8], "big")
        rng = random.Random(seed_int)

    prev_row = bytes(bytes_per_row)   # all-zero "row -1" for filters 2/3/4

    with open(dst, "wb") as out:
        out.write(PNG_SIG)
        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        _png_chunk_to_file(out, b"IHDR", ihdr)
        # Insert the neutral metadata chunks BEFORE IDAT — that's the order
        # standard PNG encoders emit them.
        _write_ancillary_chunks(out)

        # Keep a small re-assembly buffer so we can split incoming bytes into
        # exact scanlines.
        scratch = bytearray()
        for chunk in data_iter:
            if cancel is not None:
                cancel.check()
            if not chunk:
                continue
            scratch.extend(chunk)
            written += len(chunk)
            # Emit complete scanlines from scratch
            while len(scratch) >= bytes_per_row:
                row = bytes(scratch[:bytes_per_row])
                del scratch[:bytes_per_row]
                # Pick filter (seeded) and apply it to the row.
                f = _pick_filter(rng) if rng is not None else 0
                filtered = _apply_filter(f, row, prev_row)
                pending.extend(compressor.compress(bytes([f]) + filtered))
                prev_row = row
                # Flush a chunk if pending grew enough
                if len(pending) >= CHUNK_SIZE:
                    _png_chunk_to_file(out, b"IDAT", bytes(pending[:CHUNK_SIZE]))
                    del pending[:CHUNK_SIZE]
            if progress is not None and expected:
                progress(min(0.99, written / expected))

        # Source must have ended on a row boundary
        if scratch:
            raise ValueError(
                f"stream_png_write: data_iter delivered {written} bytes; "
                f"final {len(scratch)} bytes don't form a complete scanline "
                f"(width*3 = {bytes_per_row})."
            )
        # Flush compressor
        pending.extend(compressor.flush(zlib.Z_FINISH))
        # Emit remaining IDATs in CHUNK_SIZE pieces
        while len(pending) >= CHUNK_SIZE:
            _png_chunk_to_file(out, b"IDAT", bytes(pending[:CHUNK_SIZE]))
            del pending[:CHUNK_SIZE]
        if pending:
            _png_chunk_to_file(out, b"IDAT", bytes(pending))
        _png_chunk_to_file(out, b"IEND", b"")
    if progress is not None:
        progress(1.0)
    if written != expected:
        raise ValueError(
            f"stream_png_write: data_iter delivered {written} bytes; "
            f"expected {expected} for {width}×{height} RGB."
        )


def stream_png_read(src: Path,
                     cancel: Optional[CancellationToken] = None,
                     progress=None) -> tuple[int, int, Iterator[bytes]]:
    """Open a PNG and return (width, height, rgb_iterator). The iterator
    yields raw RGB bytes (no filter bytes, no padding). Caller is
    responsible for consuming `width*height*3` bytes total."""
    f = open(src, "rb")
    sig = f.read(8)
    if sig != PNG_SIG:
        f.close()
        raise ValueError("Not a PNG file (bad signature).")

    # First chunk must be IHDR
    length, tag = struct.unpack(">I", f.read(4))[0], f.read(4)
    if tag != b"IHDR":
        f.close()
        raise ValueError("PNG missing IHDR chunk.")
    ihdr = f.read(length)
    f.read(4)  # CRC, ignored
    width, height, bit_depth, color_type, _comp, _filt, _intr = struct.unpack(">IIBBBBB", ihdr)
    if bit_depth != 8 or color_type != 2:
        f.close()
        raise ValueError(f"Unsupported PNG: bit_depth={bit_depth} color_type={color_type} "
                         f"(expected 8-bit RGB / type 2).")

    bytes_per_row = width * 3
    expected = bytes_per_row * height

    def _iter():
        try:
            decomp = zlib.decompressobj()
            scratch = bytearray()
            yielded = 0
            prev_row = bytes(bytes_per_row)   # all-zero "row -1"
            while True:
                if cancel is not None:
                    cancel.check()
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                clen, ctag = struct.unpack(">I", hdr[:4])[0], hdr[4:8]
                cdata = f.read(clen)
                f.read(4)  # CRC
                if ctag == b"IEND":
                    break
                if ctag != b"IDAT":
                    continue  # skip ancillary chunks (pHYs, gAMA, sRGB, etc.)
                scratch.extend(decomp.decompress(cdata))
                # Strip filter byte and un-filter each complete scanline.
                while len(scratch) >= 1 + bytes_per_row:
                    f_type = scratch[0]
                    filtered = bytes(scratch[1:1 + bytes_per_row])
                    del scratch[:1 + bytes_per_row]
                    if f_type == 0:
                        row = filtered
                    elif f_type in (1, 2, 3, 4):
                        row = _unapply_filter(f_type, filtered, prev_row)
                    else:
                        raise ValueError(
                            f"stream_png_read: unknown filter type {f_type}"
                        )
                    prev_row = row
                    yielded += len(row)
                    if progress is not None and expected:
                        progress(min(0.99, yielded / expected))
                    yield row
            # Flush any tail
            tail = decomp.flush()
            if tail:
                scratch.extend(tail)
                while len(scratch) >= 1 + bytes_per_row:
                    f_type = scratch[0]
                    filtered = bytes(scratch[1:1 + bytes_per_row])
                    del scratch[:1 + bytes_per_row]
                    if f_type == 0:
                        row = filtered
                    elif f_type in (1, 2, 3, 4):
                        row = _unapply_filter(f_type, filtered, prev_row)
                    else:
                        raise ValueError(
                            f"stream_png_read: unknown filter type {f_type} in tail"
                        )
                    prev_row = row
                    yielded += len(row)
                    yield row
            if yielded != expected:
                raise ValueError(
                    f"stream_png_read: produced {yielded} bytes; "
                    f"expected {expected} for {width}×{height} RGB."
                )
        finally:
            f.close()
            if progress is not None:
                progress(1.0)

    return width, height, _iter()


# ---------------------------------------------------------------------------
# BMP
# ---------------------------------------------------------------------------

def _bmp_row_stride(width: int) -> int:
    return (width * 3 + 3) & ~3   # 4-byte alignment


def stream_bmp_write(dst: Path, width: int, height: int,
                      data_iter: Iterable[bytes],
                      cancel: Optional[CancellationToken] = None,
                      progress=None) -> None:
    """Write a 24-bit BMP with negative height (top-down rows) to dst.
    `data_iter` must yield exactly width*height*3 bytes RGB row-major."""
    expected = width * height * 3
    bytes_per_row = width * 3
    stride = _bmp_row_stride(width)
    pad_per_row = stride - bytes_per_row
    pixel_offset = 14 + 40
    file_size = pixel_offset + stride * height

    with open(dst, "wb") as out:
        # 14-byte BMP file header
        out.write(b"BM")
        out.write(struct.pack("<I", file_size))
        out.write(b"\x00\x00\x00\x00")
        out.write(struct.pack("<I", pixel_offset))
        # 40-byte BITMAPINFOHEADER (negative height = top-down)
        out.write(struct.pack("<IiiHHIIiiII",
                               40, width, -height, 1, 24, 0, stride * height,
                               2835, 2835, 0, 0))

        scratch = bytearray()
        written = 0
        pad = b"\x00" * pad_per_row
        for chunk in data_iter:
            if cancel is not None:
                cancel.check()
            if not chunk:
                continue
            scratch.extend(chunk)
            written += len(chunk)
            while len(scratch) >= bytes_per_row:
                row = bytes(scratch[:bytes_per_row])
                del scratch[:bytes_per_row]
                # BMP stores pixels in BGR order on disk
                bgr = bytearray(bytes_per_row)
                bgr[0::3] = row[2::3]
                bgr[1::3] = row[1::3]
                bgr[2::3] = row[0::3]
                out.write(bytes(bgr))
                if pad_per_row:
                    out.write(pad)
            if progress is not None and expected:
                progress(min(0.99, written / expected))

        if scratch:
            raise ValueError(
                f"stream_bmp_write: data_iter delivered {written} bytes; "
                f"final {len(scratch)} bytes don't form a complete scanline."
            )
    if progress is not None:
        progress(1.0)
    if written != expected:
        raise ValueError(
            f"stream_bmp_write: data_iter delivered {written} bytes; "
            f"expected {expected} for {width}×{height} RGB."
        )


def stream_bmp_read(src: Path,
                     cancel: Optional[CancellationToken] = None,
                     progress=None) -> tuple[int, int, Iterator[bytes]]:
    """Open a 24-bit BMP and return (width, height, rgb_iterator).

    Supports both top-down (negative height) and bottom-up storage. For
    bottom-up images the iterator still yields rows top-to-bottom (we read
    the file end-to-start row by row)."""
    f = open(src, "rb")
    head = f.read(54)
    if head[:2] != b"BM":
        f.close()
        raise ValueError("Not a BMP file (missing BM magic).")
    pixel_offset = struct.unpack("<I", head[10:14])[0]
    width, raw_height = struct.unpack("<ii", head[18:26])
    bit_count = struct.unpack("<H", head[28:30])[0]
    if bit_count != 24:
        f.close()
        raise ValueError(f"Unsupported BMP bit depth {bit_count} (need 24).")
    top_down = raw_height < 0
    height = abs(raw_height)
    bytes_per_row = width * 3
    stride = _bmp_row_stride(width)
    expected = bytes_per_row * height

    def _iter():
        try:
            f.seek(pixel_offset)
            yielded = 0
            if top_down:
                for _ in range(height):
                    if cancel is not None:
                        cancel.check()
                    raw = f.read(stride)
                    bgr = raw[:bytes_per_row]
                    rgb = bytearray(bytes_per_row)
                    rgb[0::3] = bgr[2::3]
                    rgb[1::3] = bgr[1::3]
                    rgb[2::3] = bgr[0::3]
                    yielded += bytes_per_row
                    if progress is not None and expected:
                        progress(min(0.99, yielded / expected))
                    yield bytes(rgb)
            else:
                # Bottom-up: read each row from end to start
                for r in range(height):
                    if cancel is not None:
                        cancel.check()
                    f.seek(pixel_offset + (height - 1 - r) * stride)
                    raw = f.read(stride)
                    bgr = raw[:bytes_per_row]
                    rgb = bytearray(bytes_per_row)
                    rgb[0::3] = bgr[2::3]
                    rgb[1::3] = bgr[1::3]
                    rgb[2::3] = bgr[0::3]
                    yielded += bytes_per_row
                    if progress is not None and expected:
                        progress(min(0.99, yielded / expected))
                    yield bytes(rgb)
        finally:
            f.close()
            if progress is not None:
                progress(1.0)

    return width, height, _iter()
