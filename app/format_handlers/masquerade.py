"""Masquerade Mode — losslessly embed any file's bytes inside a "host"
container that is itself a valid file in the host format.

Self-defining envelope format placed at a known position inside the host:

    magic      8 bytes   b"UCMSv1\\0"
    ext_len    1 byte    length of original-extension string (incl. leading dot)
    ext_str    variable  utf-8 of original ext, e.g. ".docx"
    payload_len 8 bytes  big-endian uint64 — original payload size in bytes
    payload    variable  the original file bytes verbatim
    pad        variable  zero bytes added so the host's structural rules pass

Round-trip: embed(extract(host)) == host (byte-exact for hosts the engine
itself produced; not necessarily for hosts an external editor mutated).

Hosts implemented in v1:
    .wav  — RIFF/WAVE PCM, envelope sits in the data chunk
    .png  — private ancillary chunk "ucMs" carries the envelope; image is 1x1
    .bmp  — payload appended after a 1x1 pixel block (some viewers truncate
            view at the declared image bounds — file remains valid)
    .txt  — base64 of the envelope (always opens cleanly in a text editor)
    .mkv  — Matroska container with rawvideo rgb24, 1024x1024 @ 42 fps.
            Envelope lives in the frame pixels (hybrid: MKV tags also carry
            the metadata as inspectable hints). 42 fps is intentional — it
            fingerprints Masquerade output for visual identification.
            Requires FFmpeg (the launcher guarantees it is present).
"""
from __future__ import annotations
import base64
import hashlib
import math
import os
import random
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Callable, Iterator, Tuple

from ..utils.cancellation import CancellationToken
from ..utils.paths import bin_dir
from ..core.config import CHUNK_SIZE, streaming_threshold

MAGIC = b"UCMSv1\0"
MAGIC_V2 = b"UCMSv2\0\0"  # 8 bytes — tiered-image-dimensions envelope (PNG/BMP)

# Read-write capable host extensions. Used by the registry + dropdown filter
# when Philosopher's Stone (a.k.a. Masquerade) mode is on.
TARGETS = {".wav", ".png", ".bmp", ".txt", ".mkv", ".py"}

# Lossy source extensions excluded from Stone mode entirely. The bytes of a
# JPG/MP3/MP4 file *can* technically be embedded into a Stone host and
# recovered byte-exact, but the original media data inside them is already
# a lossy compression — treating them as "preserved" is conceptually wrong.
# More importantly, since Stone targets only contain lossless containers,
# the dropdown asymmetry (jpg→txt allowed but txt→jpg not) confuses users.
# Exclude lossy formats from being Stone sources to keep the model symmetric:
# only lossless data goes through the Stone.
LOSSY_EXTS = {
    # Images
    ".jpg", ".jpeg", ".webp", ".heic",
    # Audio
    ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".ac3", ".amr",
    # Video
    ".mp4", ".webm", ".mov", ".wmv", ".flv", ".mpg", ".3gp", ".ts",
    ".vob", ".ogv", ".avi",
}


def is_lossy(ext: str) -> bool:
    e = ext.lower()
    if not e.startswith("."):
        e = "." + e
    return e in LOSSY_EXTS


def has_envelope(path: "Path", ext: str) -> bool:
    """Quick check: does this file contain a UCMSv1 envelope? Returns False
    for vanilla files of the same extension (e.g. an ordinary PNG with no
    Stone payload), so the router can fall through to normal conversion
    instead of routing through the masquerade engine.

    Strategy: scan a bounded prefix of the file for the magic bytes. WAV/
    PNG/BMP/TXT envelopes all live in the first few KB of the file. For
    MKV we wrote the title tag "UCMSv1" into the MKV header near the start
    of the file, so the ASCII bytes 'UCMSv1' appear in the first ~32 KB
    even though the binary MAGIC sits inside compressed frame data.
    """
    ext = ext.lower()
    if ext not in TARGETS:
        return False
    # .py is a Stone host only when the file matches the Transmute header.
    if ext == ".py":
        try:
            return _py_is_stone(Path(path))
        except Exception:
            return False
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return False
    if MAGIC in head or MAGIC_V2 in head:
        return True
    # MKV: rely on the title tag we set at embed time. Tag value is stored
    # as UTF-8 in the EBML Tags section, near the file start.
    if ext == ".mkv" and b"UCMSv1" in head:
        return True
    # TXT host: the envelope is base64-encoded, so MAGIC bytes don't appear
    # in raw form. Detect via the literal comment header we always write.
    if ext == ".txt" and b"Transmute Philosopher's Stone envelope" in head:
        return True
    # PNG/BMP v2: magic is buried in pixel data which may be deflate-compressed
    # for PNG. For PNG we can't cheaply scan compressed IDATs; do a small
    # decode of the first IDAT and look for v2 magic in the first ~64 KB of
    # decompressed pixel bytes.
    if ext == ".png":
        try:
            from .streaming_image import stream_png_read
            _w, _h, it = stream_png_read(Path(path))
            scratch = bytearray()
            for chunk in it:
                scratch.extend(chunk)
                if MAGIC_V2 in scratch:
                    return True
                if len(scratch) > 64 * 1024:
                    break
        except Exception:
            return False
    if ext == ".bmp":
        try:
            from .streaming_image import stream_bmp_read
            _w, _h, it = stream_bmp_read(Path(path))
            scratch = bytearray()
            for chunk in it:
                scratch.extend(chunk)
                if MAGIC_V2 in scratch:
                    return True
                if len(scratch) > 64 * 1024:
                    break
        except Exception:
            return False
    return False

# MKV host parameters. 42 fps is intentional — non-standard rate that
# fingerprints Masquerade output: combined with the UCMSv1 magic in the
# first frame's pixels, anyone can identify "this is a Masquerade file"
# just by reading the container header. Minimum 42 frames so the clip is
# always at least 1.0 seconds at 42 fps.
MKV_FRAME_W = 1024
MKV_FRAME_H = 1024
MKV_BYTES_PER_FRAME = MKV_FRAME_W * MKV_FRAME_H * 3  # rgb24
MKV_FPS = 42
MKV_MIN_FRAMES = 42


def is_target(ext: str) -> bool:
    return ext.lower() in TARGETS


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def _build_envelope(payload: bytes, src_ext: str) -> bytes:
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")
    if len(ext_bytes) > 255:
        ext_bytes = ext_bytes[:255]
    out = bytearray()
    out += MAGIC
    out += bytes([len(ext_bytes)])
    out += ext_bytes
    out += struct.pack(">Q", len(payload))
    out += payload
    return bytes(out)


def _parse_envelope(blob: bytes) -> Tuple[bytes, str]:
    """Locate the envelope (must start with MAGIC), return (payload, src_ext)."""
    idx = blob.find(MAGIC)
    if idx < 0:
        raise ValueError("Masquerade envelope not found.")
    p = idx + len(MAGIC)
    ext_len = blob[p]; p += 1
    src_ext = blob[p:p + ext_len].decode("utf-8", errors="replace"); p += ext_len
    payload_len = struct.unpack(">Q", blob[p:p + 8])[0]; p += 8
    payload = blob[p:p + payload_len]
    if len(payload) != payload_len:
        raise ValueError(f"Truncated payload (expected {payload_len}, got {len(payload)}).")
    return payload, src_ext


# ---------------------------------------------------------------------------
# Host: WAV (RIFF/WAVE, PCM 16-bit mono 8kHz — fixed format)
# ---------------------------------------------------------------------------

def _wav_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    if len(env) % 2:
        env += b"\x00"  # 16-bit sample alignment
    sample_rate = 8000
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(env)
    riff_size = 4 + (8 + 16) + (8 + data_size)
    out = bytearray()
    out += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    out += b"fmt " + struct.pack("<I", 16)
    out += struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, bits_per_sample)
    out += b"data" + struct.pack("<I", data_size) + env
    return bytes(out)


def _wav_extract(host: bytes) -> Tuple[bytes, str]:
    # Skip RIFF header + walk chunks looking for 'data'
    if host[:4] != b"RIFF" or host[8:12] != b"WAVE":
        raise ValueError("Not a WAV file.")
    p = 12
    while p + 8 <= len(host):
        ck_id = host[p:p + 4]
        ck_size = struct.unpack("<I", host[p + 4:p + 8])[0]
        if ck_id == b"data":
            blob = host[p + 8:p + 8 + ck_size]
            return _parse_envelope(blob)
        p += 8 + ck_size + (ck_size & 1)  # chunks pad to even
    raise ValueError("WAV has no data chunk.")


# ---------------------------------------------------------------------------
# Host: PNG (private "ucMs" ancillary chunk)
# ---------------------------------------------------------------------------

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    out = bytearray()
    out += PNG_SIG
    # IHDR: 1×1 RGBA, bit depth 8, color type 6, no compression/filter/interlace
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    out += _png_chunk(b"IHDR", ihdr)
    # IDAT: one transparent pixel (filter byte 0 + RGBA 00 00 00 00), zlib-compressed
    pixel = b"\x00" + b"\x00\x00\x00\x00"
    out += _png_chunk(b"IDAT", zlib.compress(pixel))
    # Private chunk carrying the envelope. PNG chunk tag rules:
    #   1st letter case = critical/ancillary  (lowercase = ancillary)
    #   2nd letter case = public/private      (lowercase = private)
    #   3rd letter case = reserved (must be uppercase)
    #   4th letter case = safe-to-copy        (lowercase = safe to copy)
    out += _png_chunk(b"ucMs", env)
    out += _png_chunk(b"IEND", b"")
    return bytes(out)


def _png_extract(host: bytes) -> Tuple[bytes, str]:
    if host[:8] != PNG_SIG:
        raise ValueError("Not a PNG file.")
    p = 8
    while p + 12 <= len(host):
        size = struct.unpack(">I", host[p:p + 4])[0]
        tag = host[p + 4:p + 8]
        body = host[p + 8:p + 8 + size]
        if tag == b"ucMs":
            return _parse_envelope(body)
        p += 12 + size
    raise ValueError("PNG has no ucMs chunk.")


# ---------------------------------------------------------------------------
# Host: BMP (1×1 24-bit, envelope appended after the pixel array)
# ---------------------------------------------------------------------------

def _bmp_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    # BITMAPINFOHEADER size 40, 1×1 24-bit pixel = 3 bytes + 1 byte padding (rows pad to 4)
    pixel_row = b"\x00\x00\x00\x00"  # one pixel + pad
    pixel_offset = 14 + 40           # file header + DIB header
    file_size = pixel_offset + len(pixel_row) + len(env)
    file_hdr = b"BM" + struct.pack("<I", file_size) + b"\x00\x00\x00\x00" + struct.pack("<I", pixel_offset)
    dib = struct.pack("<IiiHHIIiiII",
                      40, 1, 1, 1, 24, 0, len(pixel_row),
                      2835, 2835, 0, 0)
    return file_hdr + dib + pixel_row + env


def _bmp_extract(host: bytes) -> Tuple[bytes, str]:
    if host[:2] != b"BM":
        raise ValueError("Not a BMP file.")
    pixel_offset = struct.unpack("<I", host[10:14])[0]
    # The pixel array length = 4 bytes (1x1 24-bit padded). Envelope follows.
    blob = host[pixel_offset + 4:]
    return _parse_envelope(blob)


# ---------------------------------------------------------------------------
# Host: TXT (base64-wrapped envelope)
# ---------------------------------------------------------------------------

def _txt_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    body = base64.b64encode(env).decode("ascii")
    # Wrap to 76 cols + a tiny header so a casual viewer sees what it is
    chunks = [body[i:i + 76] for i in range(0, len(body), 76)]
    header = (
        f"# Transmute Philosopher's Stone envelope ({src_ext})\n"
        f"# This file holds the original payload base64-encoded inside a UCMSv1 envelope.\n"
        f"# Convert it back through Transmute (Philosopher's Stone on) to recover the source.\n"
    )
    return (header + "\n".join(chunks) + "\n").encode("utf-8")


def _txt_extract(host: bytes) -> Tuple[bytes, str]:
    text = host.decode("utf-8", errors="replace")
    # Concatenate all non-comment, non-blank lines and base64-decode
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    body = "".join(lines)
    try:
        env = base64.b64decode(body, validate=True)
    except Exception as e:
        raise ValueError(f"TXT host is not a valid masquerade envelope: {e}")
    return _parse_envelope(env)


# ---------------------------------------------------------------------------
# Host: MKV (Matroska + rawvideo rgb24, 1024x1024 @ 42 fps)
# ---------------------------------------------------------------------------
# These two work with Path objects, not bytes — the others all live in
# memory but a multi-MB MKV pipe is wasteful. The convert() entrypoint
# branches on dst_ext to pick the right API.

def _ffmpeg_path() -> Path:
    local = bin_dir() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return local
    found = shutil.which("ffmpeg")
    if not found:
        raise RuntimeError(
            "MKV masquerade requires FFmpeg, which the launcher should have "
            "installed. Re-run launcher.py to repair the install."
        )
    return Path(found)


def _mkv_pad_payload(env: bytes) -> tuple[bytes, int, int, int]:
    """Pad envelope to N whole frames. Returns (padded_bytes, n_real_frames,
    n_total_frames, n_padding_frames)."""
    n_real_frames = max(1, math.ceil(len(env) / MKV_BYTES_PER_FRAME))
    n_total_frames = max(MKV_MIN_FRAMES, n_real_frames)
    n_padding = n_total_frames - n_real_frames
    total_bytes = n_total_frames * MKV_BYTES_PER_FRAME
    padded = env + b"\x00" * (total_bytes - len(env))
    return padded, n_real_frames, n_total_frames, n_padding


def _mkv_embed_to_file(src_bytes: bytes, src_ext: str, dst: Path) -> None:
    env = _build_envelope(src_bytes, src_ext)
    padded, n_real, n_total, n_pad = _mkv_pad_payload(env)
    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    # FFV1 (lossless intra-only video codec) instead of rawvideo. Matroska
    # doesn't natively support raw RGB — FFmpeg errors with "Raw RGB is not
    # supported Natively in Matroska". FFV1 is mathematically lossless: every
    # input RGB pixel decodes back to the exact same bytes, which is all our
    # envelope needs. Bonus: it compresses (modestly, since payload bytes
    # interpreted as pixels are essentially random), so the .mkv is smaller.
    args = [
        str(ffmpeg), "-y",
        "-f", "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size", f"{MKV_FRAME_W}x{MKV_FRAME_H}",
        "-framerate", str(MKV_FPS),
        "-i", "-",
        "-c:v", "ffv1",
        "-level", "3",
        "-coder", "1",
        "-context", "1",
        "-g", "1",
        "-slices", "4",
        "-slicecrc", "1",
        "-pix_fmt", "rgb24",
        "-r", str(MKV_FPS),
        "-metadata", "title=UCMSv1",
        "-metadata", f"UC_PAYLOAD_SIZE={len(src_bytes)}",
        "-metadata", f"UC_REAL_FRAMES={n_real}",
        "-metadata", f"UC_PADDING_FRAMES={n_pad}",
        "-metadata", f"UC_FRAME_W={MKV_FRAME_W}",
        "-metadata", f"UC_FRAME_H={MKV_FRAME_H}",
        "-metadata", f"UC_ORIG_EXT={src_ext}",
        str(dst),
    ]
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=creationflags,
    )
    try:
        proc.stdin.write(padded)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    _, stderr = proc.communicate(timeout=600)
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg MKV embed failed (exit {proc.returncode}): {tail}")


def _mkv_extract_from_file(src: Path) -> Tuple[bytes, str]:
    """Pipe the MKV through FFmpeg → raw rgb24 frames → parse envelope."""
    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    args = [
        str(ffmpeg), "-y", "-i", str(src),
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-",
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    raw, stderr = proc.communicate(timeout=600)
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg MKV extract failed (exit {proc.returncode}): {tail}")
    return _parse_envelope(raw)


# ---------------------------------------------------------------------------
# UCMSv2 — tiered image dimensions, envelope-in-pixels, streaming writer/reader
# ---------------------------------------------------------------------------

# v2 envelope (in image pixels):
#   magic(8) + ext_len(1) + ext_str(var) + payload_len(8 BE)
#                                        + width(4 BE) + height(4 BE)
#                                        + payload(payload_len)
#                                        + pseudo_random_padding(rest)

# Tiered image dimensions. Always RGB. Always min 1080×1080.
_IMAGE_TIERS = [
    (3_300_000,  1080),    # ≤ 3.3 MB → 1080×1080
    (12_000_000, 2048),
    (48_000_000, 4096),
    (192_000_000, 8192),
]
_MIN_DIM = 1080


def _calc_image_dims(payload_size: int, ext_len: int) -> Tuple[int, int]:
    """Pick a square (W, H) sized to fit the v2 envelope around `payload_size`.

    Total pixels (in bytes) must be >= header_size + payload_size.
    For payloads above 192MB, side = next-1024-multiple of sqrt(needed/3).
    """
    header_size = 8 + 1 + ext_len + 8 + 4 + 4   # 25 + ext_len
    needed = payload_size + header_size
    for cap, dim in _IMAGE_TIERS:
        if needed <= dim * dim * 3:
            return dim, dim
    # Above the largest preset — grow naturally
    side = math.ceil(math.sqrt(needed / 3))
    side = max(_MIN_DIM, ((side + 1023) // 1024) * 1024)
    return side, side


def _v2_header_bytes(payload_size: int, src_ext: str, width: int, height: int) -> bytes:
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")
    if len(ext_bytes) > 255:
        ext_bytes = ext_bytes[:255]
    return (MAGIC_V2 + bytes([len(ext_bytes)]) + ext_bytes
            + struct.pack(">Q", payload_size)
            + struct.pack(">II", width, height))


def _padding_seed(magic: bytes, src_ext: str, payload_size: int) -> int:
    """Deterministic seed for the pseudo-random pad — anyone with the magic +
    ext + payload_len can reproduce the exact pad. Round-trip just ignores
    the pad (reads exactly payload_size bytes); the determinism is for
    reproducibility / debugging, not for round-trip correctness."""
    h = hashlib.sha256(magic + src_ext.encode("utf-8")
                        + struct.pack(">Q", payload_size)).digest()
    return int.from_bytes(h[:8], "big")


def _padding_iter(seed: int, total_bytes: int, chunk: int = CHUNK_SIZE):
    """Yield `total_bytes` of pseudo-random data in chunks. Never materialize
    the whole pad in memory."""
    rng = random.Random(seed)
    remaining = total_bytes
    while remaining > 0:
        n = min(chunk, remaining)
        yield rng.randbytes(n)
        remaining -= n


def _v2_pixel_iter_from_path(src_path: Path, src_ext: str, width: int, height: int,
                              cancel: Optional["CancellationToken"] = None):
    """Yield exactly width*height*3 bytes total: v2 header + payload bytes
    (streamed from src_path) + pseudo-random pad. Bounded memory."""
    payload_size = src_path.stat().st_size
    header = _v2_header_bytes(payload_size, src_ext, width, height)
    yield header
    written = len(header)
    target_total = width * height * 3
    # Stream payload from disk
    with open(src_path, "rb") as f:
        while True:
            if cancel is not None:
                cancel.check()
            buf = f.read(CHUNK_SIZE)
            if not buf:
                break
            yield buf
            written += len(buf)
    # Pseudo-random pad fills remainder
    remaining = target_total - written
    if remaining < 0:
        raise RuntimeError(
            f"v2 envelope overflowed image ({-remaining} extra bytes). "
            "Dimension calc bug?"
        )
    seed = _padding_seed(MAGIC_V2, src_ext, payload_size)
    for chunk in _padding_iter(seed, remaining):
        if cancel is not None:
            cancel.check()
        yield chunk


def _v2_pixel_iter_from_bytes(src_bytes: bytes, src_ext: str, width: int, height: int):
    """Same as above but for whole-file in-memory paths (small files).
    Used by the legacy bytes API for back-compat."""
    payload_size = len(src_bytes)
    header = _v2_header_bytes(payload_size, src_ext, width, height)
    yield header
    yield src_bytes
    target_total = width * height * 3
    remaining = target_total - len(header) - payload_size
    if remaining < 0:
        raise RuntimeError(f"v2 envelope overflow ({-remaining} extra bytes).")
    seed = _padding_seed(MAGIC_V2, src_ext, payload_size)
    for chunk in _padding_iter(seed, remaining):
        yield chunk


def _png_embed_v2_to_file(src_path: Path, src_ext: str, dst: Path,
                           cancel: Optional["CancellationToken"] = None,
                           progress: Optional[Callable[[float], None]] = None) -> None:
    from .streaming_image import stream_png_write
    payload_size = src_path.stat().st_size
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_png_write(dst, width, height, pixel_iter, cancel, progress)


def _bmp_embed_v2_to_file(src_path: Path, src_ext: str, dst: Path,
                           cancel: Optional["CancellationToken"] = None,
                           progress: Optional[Callable[[float], None]] = None) -> None:
    from .streaming_image import stream_bmp_write
    payload_size = src_path.stat().st_size
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_bmp_write(dst, width, height, pixel_iter, cancel, progress)


def _png_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path) -> None:
    from .streaming_image import stream_png_write
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    stream_png_write(dst, width, height,
                      _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height))


def _bmp_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path) -> None:
    from .streaming_image import stream_bmp_write
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    stream_bmp_write(dst, width, height,
                      _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height))


def _extract_v2_from_pixel_iter(pixel_iter: Iterator[bytes], dst_path: Path,
                                 cancel: Optional["CancellationToken"] = None) -> str:
    """Read v2 envelope from a pixel-byte iterator. Streams the recovered
    payload directly to dst_path. Returns the recovered source extension."""
    # Buffer just enough to parse the variable-length header
    scratch = bytearray()
    while len(scratch) < 9:
        try:
            scratch.extend(next(pixel_iter))
        except StopIteration:
            raise ValueError("v2 envelope: stream ended before header.")
    if bytes(scratch[:8]) != MAGIC_V2:
        raise ValueError("v2 envelope: magic not found at start of pixel data.")
    ext_len = scratch[8]
    needed_header = 8 + 1 + ext_len + 8 + 4 + 4
    while len(scratch) < needed_header:
        try:
            scratch.extend(next(pixel_iter))
        except StopIteration:
            raise ValueError("v2 envelope: stream ended mid-header.")
    p = 9
    src_ext = bytes(scratch[p:p + ext_len]).decode("utf-8", errors="replace")
    p += ext_len
    payload_size = struct.unpack(">Q", scratch[p:p + 8])[0]
    p += 8
    width, height = struct.unpack(">II", scratch[p:p + 8])
    p += 8
    # Anything past the header in scratch is the start of the payload
    payload_so_far = bytes(scratch[p:])
    written = 0
    with open(dst_path, "wb") as out:
        if payload_so_far:
            take = min(payload_size, len(payload_so_far))
            out.write(payload_so_far[:take])
            written += take
        while written < payload_size:
            if cancel is not None:
                cancel.check()
            try:
                chunk = next(pixel_iter)
            except StopIteration:
                raise ValueError(f"v2 envelope: stream ended; got {written}/{payload_size}.")
            need = payload_size - written
            if len(chunk) <= need:
                out.write(chunk)
                written += len(chunk)
            else:
                out.write(chunk[:need])
                written = payload_size
    # Drain remaining pad chunks so any underlying file handle closes cleanly
    try:
        for _ in pixel_iter:
            pass
    except Exception:
        pass
    return src_ext


def _png_extract_v2_to_file(src: Path, dst_path: Path,
                             cancel: Optional["CancellationToken"] = None) -> str:
    from .streaming_image import stream_png_read
    _w, _h, it = stream_png_read(src, cancel)
    return _extract_v2_from_pixel_iter(it, dst_path, cancel)


def _bmp_extract_v2_to_file(src: Path, dst_path: Path,
                             cancel: Optional["CancellationToken"] = None) -> str:
    from .streaming_image import stream_bmp_read
    _w, _h, it = stream_bmp_read(src, cancel)
    return _extract_v2_from_pixel_iter(it, dst_path, cancel)


# ---------------------------------------------------------------------------
# Host: .py — Philosopher's Stone self-extracting Python script
# ---------------------------------------------------------------------------

PY_HEADER_FIRST_LINE = "# Generated by Transmute - Philosopher's Stone Mode"


def _py_embed_to_file(src_path: Path, src_ext: str, dst: Path,
                       cancel: Optional["CancellationToken"] = None) -> None:
    """Generate a self-extracting Python script. Streams source bytes through
    base64 in 3-byte input chunks (4 base64 chars out) so memory stays bounded
    even for huge sources. Output script splits the base64 into 4096-char
    string literals concatenated by Python's adjacent-literal joining."""
    payload_size = src_path.stat().st_size
    src_filename = src_path.name
    sha256 = hashlib.sha256()
    chunk_chars = 4096           # base64 chars per literal
    chunk_bytes_in = (chunk_chars // 4) * 3   # source bytes producing exactly chunk_chars b64 chars
    with open(src_path, "rb") as f, open(dst, "w", encoding="utf-8") as out:
        # First pass for sha256 — quick second open keeps script header stable
        for buf in iter(lambda: f.read(CHUNK_SIZE), b""):
            sha256.update(buf)
            if cancel is not None:
                cancel.check()
        digest = sha256.hexdigest()
        # Header
        out.write(PY_HEADER_FIRST_LINE + "\n")
        out.write(f"# Source: {src_filename}\n")
        out.write(f"# Original size: {payload_size}\n")
        out.write(f"# SHA-256: {digest}\n")
        out.write(f"# UCMSv2-py\n")
        out.write("import base64\n")
        out.write("data = (\n")
        f.seek(0)
        first = True
        while True:
            if cancel is not None:
                cancel.check()
            raw = f.read(chunk_bytes_in)
            if not raw:
                break
            b64 = base64.b64encode(raw).decode("ascii")
            sep = "" if first else "\n"
            out.write(f'{sep}    "{b64}"')
            first = False
        out.write("\n)\n")
        out.write(f'with open({src_filename!r}, "wb") as f:\n')
        out.write("    f.write(base64.b64decode(data))\n")
        out.write(f'print("Reconstructed: {src_filename}")\n')


def _py_extract_to_file(src: Path, dst_path: Path,
                         cancel: Optional["CancellationToken"] = None) -> str:
    """If `src` is a Transmute-generated Stone .py, decode the embedded base64
    and write it to dst_path. Returns the recovered source extension."""
    sha_expected = ""
    src_filename = ""
    in_data = False
    b64_chunks: list[str] = []
    closed = False
    with open(src, "r", encoding="utf-8") as f:
        for line in f:
            if cancel is not None:
                cancel.check()
            stripped = line.strip()
            if not stripped:
                continue
            if not in_data:
                if stripped.startswith("# Source:"):
                    src_filename = stripped[len("# Source:"):].strip()
                elif stripped.startswith("# SHA-256:"):
                    sha_expected = stripped[len("# SHA-256:"):].strip()
                elif stripped.startswith("data = ("):
                    in_data = True
                continue
            # in_data
            if stripped == ")":
                closed = True
                break
            # Lines look like "    \"...\""
            s = stripped
            if s.startswith('"'):
                s = s[1:]
            if s.endswith('",'):
                s = s[:-2]
            elif s.endswith('"'):
                s = s[:-1]
            b64_chunks.append(s)
    if not closed or not src_filename:
        raise ValueError("Stone .py: malformed header or data block.")
    # Recover extension from filename
    src_ext = Path(src_filename).suffix.lower() or ".bin"
    # Decode + stream to dst
    body = "".join(b64_chunks)
    payload = base64.b64decode(body)
    if sha_expected:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != sha_expected:
            raise ValueError(
                f"Stone .py: SHA-256 mismatch (expected {sha_expected}, got {actual})."
            )
    with open(dst_path, "wb") as out:
        out.write(payload)
    return src_ext


def _py_is_stone(src: Path) -> bool:
    """Quick check: is this .py a Transmute Stone-generated script?"""
    try:
        with open(src, "r", encoding="utf-8") as f:
            for _ in range(3):
                line = f.readline()
                if not line:
                    break
                if line.startswith("#!"):
                    continue   # allow shebang
                return line.rstrip("\r\n") == PY_HEADER_FIRST_LINE
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EMBED = {".wav": _wav_embed, ".png": _png_embed, ".bmp": _bmp_embed, ".txt": _txt_embed}
_EXTRACT = {".wav": _wav_extract, ".png": _png_extract, ".bmp": _bmp_extract, ".txt": _txt_extract}


def can_embed_into(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EMBED or ext in (".mkv", ".py")


def can_extract_from(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EXTRACT or ext in (".mkv", ".py")


def _try_extract(src: Path, src_ext: str) -> Tuple[bytes, str] | None:
    """Returns (payload, recovered_ext) if src is a Masquerade host with a
    valid envelope; None if no envelope or unsupported source ext.

    NOTE: For path-based extractors (PNG v2, BMP v2, MKV, .py), this still
    returns whole-payload bytes for back-compat with the existing convert()
    flow. For huge payloads the new streaming convert path bypasses this and
    extracts directly to dst.
    """
    src_ext = src_ext.lower()
    try:
        if src_ext == ".mkv":
            return _mkv_extract_from_file(src)
        if src_ext == ".py":
            if not _py_is_stone(src):
                return None
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
            try:
                ext = _py_extract_to_file(src, tmp)
                return tmp.read_bytes(), ext
            finally:
                try: tmp.unlink()
                except OSError: pass
        if src_ext == ".png":
            # Try v2 first (envelope in pixel data), fall back to v1 (private chunk)
            try:
                import tempfile
                tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
                try:
                    ext = _png_extract_v2_to_file(src, tmp)
                    return tmp.read_bytes(), ext
                finally:
                    try: tmp.unlink()
                    except OSError: pass
            except (ValueError, RuntimeError):
                return _png_extract(src.read_bytes())
        if src_ext == ".bmp":
            try:
                import tempfile
                tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
                try:
                    ext = _bmp_extract_v2_to_file(src, tmp)
                    return tmp.read_bytes(), ext
                finally:
                    try: tmp.unlink()
                    except OSError: pass
            except (ValueError, RuntimeError):
                return _bmp_extract(src.read_bytes())
        if src_ext in _EXTRACT:
            return _EXTRACT[src_ext](src.read_bytes())
    except ValueError:
        return None
    return None


def _embed_to(dst: Path, payload: bytes, src_ext: str, dst_ext: str,
               cancel: Optional["CancellationToken"] = None) -> None:
    """Whole-bytes-in-memory embed. Used for small files."""
    dst_ext = dst_ext.lower()
    if dst_ext == ".mkv":
        _mkv_embed_to_file(payload, src_ext, dst)
        return
    if dst_ext == ".png":
        _png_embed_v2_from_bytes(payload, src_ext, dst)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_from_bytes(payload, src_ext, dst)
        return
    if dst_ext == ".py":
        # Write payload to a temp file so _py_embed_to_file can stream it
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=src_ext or ".bin")[1])
        try:
            tmp.write_bytes(payload)
            # Use the original filename (best guess)
            tmp_renamed = tmp.with_name("payload" + (src_ext or ".bin"))
            try: tmp.rename(tmp_renamed); tmp = tmp_renamed
            except OSError: pass
            _py_embed_to_file(tmp, src_ext, dst, cancel)
        finally:
            try: tmp.unlink()
            except OSError: pass
        return
    if dst_ext not in _EMBED:
        raise RuntimeError(f"Masquerade target {dst_ext} is not supported.")
    dst.write_bytes(_EMBED[dst_ext](payload, src_ext))


def _embed_streamed_to(dst: Path, src_path: Path, src_ext: str, dst_ext: str,
                        cancel: Optional["CancellationToken"] = None,
                        progress: Optional[Callable[[float], None]] = None) -> None:
    """Path-based streaming embed. Used for files above the streaming threshold."""
    dst_ext = dst_ext.lower()
    if dst_ext == ".png":
        _png_embed_v2_to_file(src_path, src_ext, dst, cancel, progress)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_to_file(src_path, src_ext, dst, cancel, progress)
        return
    if dst_ext == ".py":
        _py_embed_to_file(src_path, src_ext, dst, cancel)
        return
    # WAV / TXT / MKV: whole-file bytes API still used; fall through.
    src_bytes = src_path.read_bytes()
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel)


def convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
            cancel: CancellationToken, progress: Callable[[float], None]) -> None:
    """Top-level entry the conversion queue calls when Masquerade Mode is on.

    Two cases:
      1. Source is itself a masquerade host AND contains a valid envelope:
         EXTRACT to recover the original payload. If dst_ext matches the
         recovered ext (or isn't a host itself), write payload as-is. If
         dst_ext is a different host, re-embed.
      2. Otherwise: EMBED the source bytes into a fresh host of dst_ext.

    For files at/above streaming_threshold, uses path-based streaming
    helpers — never materializes the whole payload in memory.
    """
    progress(0.05)
    src_ext = src_ext.lower()
    dst_ext = dst_ext.lower()

    extracted = _try_extract(src, src_ext) if can_extract_from(src_ext) else None
    if extracted is not None:
        payload, recovered_ext = extracted
        cancel.check()
        progress(0.55)
        if dst_ext == recovered_ext or not can_embed_into(dst_ext):
            if dst.suffix.lower() != recovered_ext:
                dst = dst.with_suffix(recovered_ext)
            dst.write_bytes(payload)
            progress(1.0)
            return
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel)
        progress(1.0)
        return

    # Decide streamed vs whole-file path based on source size
    src_size = src.stat().st_size
    threshold = streaming_threshold(dst_ext)
    if src_size >= threshold and dst_ext in (".png", ".bmp", ".py"):
        cancel.check()
        progress(0.1)
        _embed_streamed_to(dst, src, src_ext, dst_ext, cancel, progress)
        progress(1.0)
        return

    src_bytes = src.read_bytes()
    cancel.check()
    progress(0.3)
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel)
    progress(1.0)
