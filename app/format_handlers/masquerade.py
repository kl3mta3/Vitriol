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
import math
import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Callable, Tuple

from ..utils.cancellation import CancellationToken
from ..utils.paths import bin_dir

MAGIC = b"UCMSv1\0"

# Read-write capable host extensions. Used by the registry + dropdown filter
# when Philosopher's Stone (a.k.a. Masquerade) mode is on.
TARGETS = {".wav", ".png", ".bmp", ".txt", ".mkv"}

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
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return False
    if MAGIC in head:
        return True
    # MKV: rely on the title tag we set at embed time. Tag value is stored
    # as UTF-8 in the EBML Tags section, near the file start.
    if ext == ".mkv" and b"UCMSv1" in head:
        return True
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
    args = [
        str(ffmpeg), "-y",
        "-f", "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size", f"{MKV_FRAME_W}x{MKV_FRAME_H}",
        "-framerate", str(MKV_FPS),
        "-i", "-",
        "-c:v", "rawvideo",
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
# Public API
# ---------------------------------------------------------------------------

_EMBED = {".wav": _wav_embed, ".png": _png_embed, ".bmp": _bmp_embed, ".txt": _txt_embed}
_EXTRACT = {".wav": _wav_extract, ".png": _png_extract, ".bmp": _bmp_extract, ".txt": _txt_extract}


def can_embed_into(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EMBED or ext == ".mkv"


def can_extract_from(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EXTRACT or ext == ".mkv"


def _try_extract(src: Path, src_ext: str) -> Tuple[bytes, str] | None:
    """Returns (payload, recovered_ext) if src is a Masquerade host with a
    valid envelope; None if no envelope or unsupported source ext."""
    src_ext = src_ext.lower()
    try:
        if src_ext == ".mkv":
            return _mkv_extract_from_file(src)
        if src_ext in _EXTRACT:
            return _EXTRACT[src_ext](src.read_bytes())
    except ValueError:
        return None
    return None


def _embed_to(dst: Path, payload: bytes, src_ext: str, dst_ext: str) -> None:
    dst_ext = dst_ext.lower()
    if dst_ext == ".mkv":
        _mkv_embed_to_file(payload, src_ext, dst)
        return
    if dst_ext not in _EMBED:
        raise RuntimeError(f"Masquerade target {dst_ext} is not supported.")
    dst.write_bytes(_EMBED[dst_ext](payload, src_ext))


def convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
            cancel: CancellationToken, progress: Callable[[float], None]) -> None:
    """Top-level entry the conversion queue calls when Masquerade Mode is on.

    Two cases:
      1. Source is itself a masquerade host AND contains a valid envelope:
         EXTRACT to recover the original payload. If dst_ext matches the
         recovered ext (or isn't a host itself), write payload as-is. If
         dst_ext is a different host, re-embed.
      2. Otherwise: EMBED the source bytes into a fresh host of dst_ext.
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
        _embed_to(dst, payload, recovered_ext, dst_ext)
        progress(1.0)
        return

    src_bytes = src.read_bytes()
    cancel.check()
    progress(0.3)
    _embed_to(dst, src_bytes, src_ext, dst_ext)
    progress(1.0)
