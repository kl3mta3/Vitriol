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
TARGETS = {".wav", ".png", ".bmp", ".txt", ".mkv", ".py",
           ".ply", ".obj", ".glb",
           ".aiff", ".flac"}
# .fbx is intentionally excluded as a Stone host (autodesk-proprietary
# binary; readers are notoriously strict, no clean place to drop a payload).
# .flac is a Stone host but only via the music encoder (cross-category) —
# the WAV music output is re-encoded to FLAC via FFmpeg, and the inverse
# decode path uses FFmpeg → WAV → music extract.

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
    # PLY / OBJ hosts: envelope is base64'd inside `comment` / `#` lines.
    # Look for the tagged comment prefix.
    if ext == ".ply" and b"comment uc " in head:
        return True
    if ext == ".obj" and b"# uc " in head:
        return True
    # FLAC host: detection requires FFmpeg-decoding to WAV first (FLAC
    # stream format is too complex to inspect cheaply). We only do this
    # when the file's actual magic is fLaC AND the caller has Stone on
    # (the calling site, not has_envelope itself, gates this).
    if ext == ".flac" and head[:4] == b"fLaC":
        try:
            import tempfile
            tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
            try:
                _flac_to_wav_via_ffmpeg(Path(path), tmp_wav)
                # Recursively check the temp WAV. The has_envelope call
                # for .wav covers both classic and music modes.
                return has_envelope(tmp_wav, ".wav")
            finally:
                try: tmp_wav.unlink()
                except OSError: pass
        except Exception:
            return False
    # AIFF music host: same scheme as WAV music but big-endian PCM in
    # FORM/AIFF/SSND container. Detect via SSND-data music header probe.
    if ext == ".aiff":
        try:
            full = open(path, "rb").read()
            if full[:4] == b"FORM" and full[8:12] == b"AIFF":
                p = 12
                num_channels = bits_per_sample = sample_rate = None
                ssnd_blob = None
                while p + 8 <= len(full):
                    ck_id = full[p:p + 4]
                    ck_size = struct.unpack(">I", full[p + 4:p + 8])[0]
                    if ck_id == b"COMM" and ck_size >= 18:
                        comm = full[p + 8:p + 8 + ck_size]
                        num_channels, _nf, bits_per_sample = struct.unpack(
                            ">hI h", comm[:8])
                        sample_rate = _aiff_parse_extended_float(comm[8:18])
                    elif ck_id == b"SSND":
                        ssnd_blob = full[p + 8:p + 8 + ck_size][8:]  # strip offset+blockSize
                        break
                    p += 8 + ck_size + (ck_size & 1)
                # Plain UCMSv1 envelope check first (same-category AIFF).
                if ssnd_blob and (MAGIC in ssnd_blob[:64*1024]):
                    return True
                # Music mode probe.
                if ssnd_blob and (sample_rate, num_channels, bits_per_sample) == (44100, 2, 16):
                    if len(ssnd_blob) >= 12 * 4:
                        header_bytes = bytearray()
                        for i in range(12):
                            off = i * 4
                            left, right = struct.unpack(">hh", ssnd_blob[off:off + 4])
                            header_bytes.append(((right & 0x0F) << 4) | (left & 0x0F))
                        if bytes(header_bytes[:4]) == b"uM01":
                            return True
        except (OSError, struct.error):
            pass
    # WAV music host: bottom 4 bits of stereo samples carry payload bytes;
    # MAGIC doesn't appear in raw bytes. Detect by reading the WAV format
    # chunk + first ~16 frames and checking for the music-payload magic.
    if ext == ".wav":
        try:
            full = open(path, "rb").read()
            # Need WAV format chunk to know endianness/channel layout.
            if full[:4] == b"RIFF" and full[8:12] == b"WAVE":
                p = 12
                sample_rate = num_channels = bits_per_sample = None
                data_blob = None
                while p + 8 <= len(full):
                    ck_id = full[p:p + 4]
                    ck_size = struct.unpack("<I", full[p + 4:p + 8])[0]
                    if ck_id == b"fmt ":
                        fmt = full[p + 8:p + 8 + ck_size]
                        if len(fmt) >= 16:
                            _, num_channels, sample_rate, _, _, bits_per_sample = (
                                struct.unpack("<HHIIHH", fmt[:16]))
                    elif ck_id == b"data":
                        data_blob = full[p + 8:p + 8 + ck_size]
                        break
                    p += 8 + ck_size + (ck_size & 1)
                if data_blob and (sample_rate, num_channels, bits_per_sample) == (44100, 2, 16):
                    # Probe the music-mode header
                    if len(data_blob) >= 12 * 4:
                        from . import _music as _m
                        header_bytes = bytearray()
                        for i in range(12):
                            off = i * 4
                            left, right = struct.unpack("<hh", data_blob[off:off + 4])
                            header_bytes.append(((right & 0x0F) << 4) | (left & 0x0F))
                        if bytes(header_bytes[:4]) == b"uM01":
                            return True
        except (OSError, struct.error):
            pass
    # TXT host: the envelope is base64-encoded, so MAGIC bytes don't appear
    # in raw form. The file carries no comment header (deliberately — the
    # output should look like an unremarkable base64 dump). Detect by
    # attempting a base64 decode of the head and checking for MAGIC.
    if ext == ".txt":
        try:
            text = head.decode("ascii", errors="strict")
            stitched = "".join(ln.strip() for ln in text.splitlines()
                               if ln.strip() and not ln.startswith("#"))
            # Decode just enough to inspect the prefix; pad to a multiple of 4.
            probe = stitched[: (len(stitched) // 4) * 4]
            if probe:
                decoded = base64.b64decode(probe, validate=False)
                if decoded.startswith(MAGIC) or decoded.startswith(MAGIC_V2):
                    return True
        except (UnicodeDecodeError, ValueError):
            pass
    # PNG/BMP v2: magic is buried in pixel data which may be deflate-compressed
    # for PNG. For PNG we can't cheaply scan compressed IDATs; do a small
    # decode of the first IDAT and look for v2 magic in the first ~64 KB of
    # decompressed pixel bytes.
    #
    # Dual-attempt: a Mandelbrot-XOR'd PNG (cross-category Stone) won't show
    # MAGIC_V2 in raw pixel bytes. We need to also scan the same buffer with
    # the Mandelbrot inverse keystream applied, in case this is a cross-
    # category Stone host.
    if ext == ".png":
        try:
            from .streaming_image import stream_png_read
            w, h, it = stream_png_read(Path(path))
            return _v2_envelope_present_in_pixels(it, w, h)
        except Exception:
            return False
    if ext == ".bmp":
        try:
            from .streaming_image import stream_bmp_read
            w, h, it = stream_bmp_read(Path(path))
            return _v2_envelope_present_in_pixels(it, w, h)
        except Exception:
            return False
    return False


def _v2_envelope_present_in_pixels(pixel_iter, width: int, height: int,
                                    probe_bytes: int = 64 * 1024) -> bool:
    """Read up to `probe_bytes` of pixel data; return True if MAGIC_V2 is
    present either in the raw stream (same-category Stone) OR in the
    Mandelbrot-XOR'd stream (cross-category Stone aesthetic)."""
    scratch = bytearray()
    for chunk in pixel_iter:
        scratch.extend(chunk)
        if MAGIC_V2 in scratch:
            return True
        if len(scratch) >= probe_bytes:
            break
    if not scratch:
        return False
    # Mandelbrot bit-packed envelope: read the bottom 4 bits of each pixel
    # byte and reassemble into a byte stream; check for MAGIC_V2.
    max_env = len(scratch) // 2
    if max_env >= 8:
        env_prefix = _mandelbrot_unpack_envelope_from_pixels(
            bytes(scratch), min(max_env, 64 * 1024))
        if MAGIC_V2 in env_prefix:
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


def _wav_embed_music(src_bytes: bytes, src_ext: str) -> bytes:
    """Cross-category Stone audio target. Generate music samples with the
    source bytes packed in the bottom 4 bits per sample."""
    from . import _music as _m
    env = _build_envelope(src_bytes, src_ext)
    pcm, n_frames = _m.encode_music_payload(env)
    sample_rate = _m.SAMPLE_RATE
    num_channels = _m.CHANNELS
    bits_per_sample = _m.BITS_PER_SAMPLE
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm)
    riff_size = 4 + (8 + 16) + (8 + data_size)
    out = bytearray()
    out += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    out += b"fmt " + struct.pack("<I", 16)
    out += struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate,
                        block_align, bits_per_sample)
    out += b"data" + struct.pack("<I", data_size) + pcm
    return bytes(out)


# ---------------------------------------------------------------------------
# Host: AIFF (Apple/IFF audio container)
# ---------------------------------------------------------------------------
# Layout: FORM <size> AIFF [chunks]
# Required chunks: COMM (parameters) and SSND (sample data).
# Sample data is big-endian PCM. Chunks 4-byte aligned (pad with one zero
# byte if odd-sized payload).

def _aiff_pad(n: int) -> int:
    return n & 1


def _aiff_embed(src_bytes: bytes, src_ext: str) -> bytes:
    """Same-category AIFF: stash the UCMSv1 envelope verbatim into the
    SSND chunk. Mirrors the classic _wav_embed approach.

    The format is 8 kHz mono 16-bit (matches our WAV defaults — keeps the
    payload-to-frames math identical for parity with WAV)."""
    env = _build_envelope(src_bytes, src_ext)
    if len(env) % 2:
        env += b"\x00"  # 16-bit alignment
    sample_rate = 8000
    num_channels = 1
    bits_per_sample = 16
    n_frames = len(env) // (num_channels * (bits_per_sample // 8))
    # COMM chunk: numChannels(2) numSampleFrames(4) sampleSize(2) sampleRate(10 IEEE 754 80-bit)
    comm_data = (struct.pack(">hI h", num_channels, n_frames, bits_per_sample)
                 + _aiff_extended_float(sample_rate))
    if _aiff_pad(len(comm_data)):
        comm_data += b"\x00"
    # SSND chunk: offset(4) blockSize(4) sampleData(...)
    ssnd_data = struct.pack(">II", 0, 0) + env
    if _aiff_pad(len(ssnd_data)):
        ssnd_data += b"\x00"
    body = (b"AIFF"
            + b"COMM" + struct.pack(">I", len(comm_data)) + comm_data
            + b"SSND" + struct.pack(">I", len(ssnd_data)) + ssnd_data)
    return b"FORM" + struct.pack(">I", len(body)) + body


def _aiff_embed_music(src_bytes: bytes, src_ext: str) -> bytes:
    """Cross-category AIFF: music samples (big-endian PCM) carry payload
    in bottom 4 bits per channel. Same encoder as _wav_embed_music but
    big-endian samples wrapped in IFF chunks."""
    from . import _music as _m
    env = _build_envelope(src_bytes, src_ext)
    pcm, n_frames = _m.encode_music_payload_be(env)
    sample_rate = _m.SAMPLE_RATE
    num_channels = _m.CHANNELS
    bits_per_sample = _m.BITS_PER_SAMPLE
    comm_data = (struct.pack(">hI h", num_channels, n_frames, bits_per_sample)
                 + _aiff_extended_float(sample_rate))
    if _aiff_pad(len(comm_data)):
        comm_data += b"\x00"
    ssnd_data = struct.pack(">II", 0, 0) + pcm
    if _aiff_pad(len(ssnd_data)):
        ssnd_data += b"\x00"
    body = (b"AIFF"
            + b"COMM" + struct.pack(">I", len(comm_data)) + comm_data
            + b"SSND" + struct.pack(">I", len(ssnd_data)) + ssnd_data)
    return b"FORM" + struct.pack(">I", len(body)) + body


def _aiff_extract(host: bytes) -> Tuple[bytes, str]:
    """Dual-attempt AIFF extract: classic UCMSv1-in-SSND first, then music."""
    if host[:4] != b"FORM" or host[8:12] != b"AIFF":
        raise ValueError("Not an AIFF file.")
    p = 12
    sample_rate = num_channels = bits_per_sample = None
    ssnd_blob = None
    while p + 8 <= len(host):
        ck_id = host[p:p + 4]
        ck_size = struct.unpack(">I", host[p + 4:p + 8])[0]
        if ck_id == b"COMM" and ck_size >= 18:
            comm = host[p + 8:p + 8 + ck_size]
            num_channels, _nframes, bits_per_sample = struct.unpack(
                ">hI h", comm[:8])
            sample_rate = _aiff_parse_extended_float(comm[8:18])
        elif ck_id == b"SSND":
            ssnd = host[p + 8:p + 8 + ck_size]
            # Strip 8-byte offset + blockSize prefix
            ssnd_blob = ssnd[8:] if len(ssnd) >= 8 else b""
        p += 8 + ck_size + _aiff_pad(ck_size)
    if ssnd_blob is None:
        raise ValueError("AIFF: no SSND chunk.")
    # Attempt 1: classic UCMSv1 envelope verbatim in SSND.
    try:
        return _parse_envelope(ssnd_blob)
    except ValueError:
        pass
    # Attempt 2: music mode — big-endian PCM at 44.1 kHz / 16-bit / stereo.
    if (sample_rate, num_channels, bits_per_sample) != (44100, 2, 16):
        raise ValueError("AIFF: SSND has neither classic envelope nor "
                         "music-mode parameters (44.1 kHz / 16-bit / stereo).")
    from . import _music as _m
    env = _m.decode_music_payload_be(ssnd_blob)
    return _parse_envelope(env)


def _aiff_extended_float(value: int) -> bytes:
    """Encode a positive integer as IEEE 754 80-bit extended-precision
    big-endian (used by AIFF for sample rate). Sufficient for typical
    sample rates (8 kHz to 192 kHz). No fractional support needed."""
    if value == 0:
        return b"\x00" * 10
    sign = 0
    if value < 0:
        sign = 0x8000
        value = -value
    # Find power of 2 such that value normalizes to [1, 2)
    exp = value.bit_length() - 1
    mantissa = value << (63 - exp)
    biased_exp = exp + 16383
    return struct.pack(">HQ", sign | biased_exp, mantissa)


def _aiff_parse_extended_float(b: bytes) -> int:
    """Inverse of _aiff_extended_float — returns positive integer rate."""
    if len(b) != 10:
        raise ValueError("AIFF: extended float must be 10 bytes")
    if b == b"\x00" * 10:
        return 0
    biased_exp_word, mantissa = struct.unpack(">HQ", b)
    exp = (biased_exp_word & 0x7FFF) - 16383
    if exp < 0 or exp > 63:
        return 0
    return mantissa >> (63 - exp)


# ---------------------------------------------------------------------------
# Host: FLAC (lossless audio compression via FFmpeg)
# ---------------------------------------------------------------------------
# We don't ship a FLAC encoder/decoder. Instead the music WAV is generated
# in memory, written to a temp file, then re-encoded to FLAC via FFmpeg
# (`ffmpeg -i tmp.wav -c:a flac out.flac`). FLAC is bit-exact lossless,
# so the bottom-4-bit payload survives the encode/decode round-trip.
#
# Read direction: FFmpeg decodes the FLAC to a temp WAV, then we extract
# from that WAV using the same music decoder. has_envelope cost is one
# FFmpeg invocation — only fires when masquerade=True is set, so the cost
# is amortized against the conversion itself.

def _flac_via_ffmpeg(wav_path: Path, flac_path: Path) -> None:
    """Re-encode WAV → FLAC losslessly via FFmpeg."""
    ff = _ffmpeg_path()
    rc = subprocess.call(
        [str(ff), "-y", "-loglevel", "error",
         "-i", str(wav_path), "-c:a", "flac",
         "-compression_level", "5",
         str(flac_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc != 0 or not flac_path.exists():
        raise RuntimeError(f"FFmpeg WAV->FLAC failed (exit {rc})")


def _flac_to_wav_via_ffmpeg(flac_path: Path, wav_path: Path) -> None:
    """Decode FLAC → WAV losslessly via FFmpeg."""
    ff = _ffmpeg_path()
    rc = subprocess.call(
        [str(ff), "-y", "-loglevel", "error",
         "-i", str(flac_path), "-c:a", "pcm_s16le",
         "-ar", "44100", "-ac", "2",
         str(wav_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc != 0 or not wav_path.exists():
        raise RuntimeError(f"FFmpeg FLAC->WAV failed (exit {rc})")


def _flac_embed_music(src_bytes: bytes, src_ext: str, dst: Path) -> None:
    """Cross-category FLAC: write music WAV to temp, re-encode via FFmpeg."""
    import tempfile
    wav_bytes = _wav_embed_music(src_bytes, src_ext)
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        tmp_wav.write_bytes(wav_bytes)
        _flac_via_ffmpeg(tmp_wav, dst)
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _flac_embed(src_bytes: bytes, src_ext: str, dst: Path) -> None:
    """Same-category FLAC: classic UCMSv1 envelope in a tiny PCM WAV,
    re-encoded to FLAC via FFmpeg."""
    import tempfile
    wav_bytes = _wav_embed(src_bytes, src_ext)
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        tmp_wav.write_bytes(wav_bytes)
        _flac_via_ffmpeg(tmp_wav, dst)
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _flac_extract(src: Path) -> Tuple[bytes, str]:
    """Decode the FLAC to WAV via FFmpeg, then dual-attempt _wav_extract.
    Note: takes a Path (not bytes) because FFmpeg needs a file. The
    matching _EXTRACT entry adapts via _flac_extract_from_bytes below."""
    import tempfile
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        _flac_to_wav_via_ffmpeg(src, tmp_wav)
        return _wav_extract(tmp_wav.read_bytes())
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _flac_extract_from_bytes(host: bytes) -> Tuple[bytes, str]:
    """Bytes-API wrapper for _EXTRACT dispatch."""
    import tempfile
    tmp_flac = Path(tempfile.mkstemp(suffix=".flac")[1])
    try:
        tmp_flac.write_bytes(host)
        return _flac_extract(tmp_flac)
    finally:
        try: tmp_flac.unlink()
        except OSError: pass


def _wav_extract(host: bytes) -> Tuple[bytes, str]:
    # Skip RIFF header + walk chunks looking for 'data'
    if host[:4] != b"RIFF" or host[8:12] != b"WAVE":
        raise ValueError("Not a WAV file.")
    p = 12
    data_blob = None
    sample_rate = None
    num_channels = None
    bits_per_sample = None
    while p + 8 <= len(host):
        ck_id = host[p:p + 4]
        ck_size = struct.unpack("<I", host[p + 4:p + 8])[0]
        if ck_id == b"fmt ":
            fmt = host[p + 8:p + 8 + ck_size]
            if len(fmt) >= 16:
                _, num_channels, sample_rate, _, _, bits_per_sample = struct.unpack(
                    "<HHIIHH", fmt[:16])
        elif ck_id == b"data":
            data_blob = host[p + 8:p + 8 + ck_size]
        p += 8 + ck_size + (ck_size & 1)  # chunks pad to even
    if data_blob is None:
        raise ValueError("WAV: no data chunk found.")
    # Dual-attempt: classic UCMSv1-in-data-chunk first.
    try:
        return _parse_envelope(data_blob)
    except ValueError:
        pass
    # Music mode: 44.1 kHz / 16-bit / stereo with payload in bottom 4 bits.
    if (sample_rate, num_channels, bits_per_sample) != (44100, 2, 16):
        raise ValueError("WAV: data chunk has neither classic envelope nor "
                         "music-mode parameters (44.1 kHz / 16-bit / stereo).")
    from . import _music as _m
    env = _m.decode_music_payload_le(data_blob)
    return _parse_envelope(env)
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
    # Wrap to 76 cols. No header — the file looks like an unremarkable
    # base64 dump (PEM, key material, etc.). Detection on the read side
    # decodes the body and checks for the envelope MAGIC; if the bytes
    # don't decode cleanly or don't begin with MAGIC, the file falls
    # through to the regular .txt handler.
    chunks = [body[i:i + 76] for i in range(0, len(body), 76)]
    return ("\n".join(chunks) + "\n").encode("utf-8")


def _txt_extract(host: bytes) -> Tuple[bytes, str]:
    text = host.decode("utf-8", errors="replace")
    # Stitch every non-blank line; ignore stray comment-style lines so a user
    # who accidentally pasted an envelope under a header still recovers.
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

# Module-level cache so we don't re-walk the filesystem per MKV embed/extract.
# Auto-invalidates when the cached binary disappears mid-session.
_FFMPEG_RESOLVED: "Path | None" = None


def _ffmpeg_path() -> Path:
    global _FFMPEG_RESOLVED
    if _FFMPEG_RESOLVED is not None and _FFMPEG_RESOLVED.exists():
        return _FFMPEG_RESOLVED
    from ..utils.paths import find_ffmpeg
    found = find_ffmpeg()
    if found is None:
        raise RuntimeError(
            "MKV masquerade requires FFmpeg, which the launcher should have "
            "installed. Re-run launcher.py to repair the install."
        )
    _FFMPEG_RESOLVED = found
    return _FFMPEG_RESOLVED


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
                              cancel: Optional["CancellationToken"] = None,
                              pad_zero: bool = False):
    """Yield exactly width*height*3 bytes total: v2 header + payload bytes
    (streamed from src_path) + pad. Bounded memory.

    `pad_zero`: when True, the pad section is all zero bytes instead of
    pseudo-random. Used by the Mandelbrot mode so the visible image
    becomes the fractal pattern (zero XOR keystream = keystream itself).
    Same-category mode keeps the pseudo-random pad."""
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
    # Pad fills remainder. Zeros for Mandelbrot mode (so the visible image
    # is the keystream after XOR), pseudo-random otherwise.
    remaining = target_total - written
    if remaining < 0:
        raise RuntimeError(
            f"v2 envelope overflowed image ({-remaining} extra bytes). "
            "Dimension calc bug?"
        )
    if pad_zero:
        for chunk in _zero_pad_iter(remaining):
            if cancel is not None:
                cancel.check()
            yield chunk
    else:
        seed = _padding_seed(MAGIC_V2, src_ext, payload_size)
        for chunk in _padding_iter(seed, remaining):
            if cancel is not None:
                cancel.check()
            yield chunk


def _v2_pixel_iter_from_bytes(src_bytes: bytes, src_ext: str, width: int, height: int,
                               pad_zero: bool = False):
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
    if pad_zero:
        for chunk in _zero_pad_iter(remaining):
            yield chunk
    else:
        seed = _padding_seed(MAGIC_V2, src_ext, payload_size)
        for chunk in _padding_iter(seed, remaining):
            yield chunk


def _zero_pad_iter(total_bytes: int, chunk: int = CHUNK_SIZE):
    remaining = total_bytes
    while remaining > 0:
        n = min(chunk, remaining)
        yield bytes(n)
        remaining -= n


# ---------------------------------------------------------------------------
# Mandelbrot keystream (cross-category Stone aesthetic, image targets only)
# ---------------------------------------------------------------------------

# Salt that scopes the keystream to Transmute. Different tools doing similar
# fractal tricks won't accidentally collide with our seed.
_MANDELBROT_SALT = b"transmute-mandelbrot-v1"

# NumPy-vectorized full-image Mandelbrot keystream. RGB-interleaved
# (3 bytes per pixel) so the fractal renders in color directly, without
# tiling. Generation cost is ~0.3-0.6 sec for a 1080² image.
#
# Cross-category embed scheme: each pixel byte's TOP 4 bits hold the
# fractal color, BOTTOM 4 bits hold a nibble of the source envelope.
# The whole image displays the colored fractal everywhere (just at 4-bit
# color depth per channel = 16 levels = 4096 total colors), and the
# envelope can be reassembled by reading the bottom 4 bits across the
# pixel stream in order.

# Two pixel bytes carry one envelope byte (low nibble first, then high).
_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE = 2


def _mandelbrot_pack_envelope_into_fractal(envelope: bytes, fractal: bytes,
                                             total_pixel_bytes: int) -> bytes:
    """Combine fractal pixel bytes (top 4 bits) with envelope nibbles
    (bottom 4 bits). Returns a bytes of length `total_pixel_bytes`.

    `fractal` must be at least `total_pixel_bytes` long.
    `envelope` may be shorter than total_pixel_bytes / 2 — bytes past
    the envelope end get a zero nibble (so those pixels show the pure
    fractal at full top-4-bit depth)."""
    out = bytearray(total_pixel_bytes)
    env_len = len(envelope)
    for i in range(total_pixel_bytes):
        env_idx = i >> 1   # i // 2
        if env_idx < env_len:
            nibble_high = i & 1
            byte = envelope[env_idx]
            nib = (byte >> 4) if nibble_high else (byte & 0x0F)
        else:
            nib = 0
        out[i] = (fractal[i] & 0xF0) | nib
    return bytes(out)


def _mandelbrot_unpack_envelope_from_pixels(pixel_bytes: bytes,
                                              max_envelope_bytes: int) -> bytes:
    """Reassemble envelope bytes from the bottom 4 bits of pixel_bytes.
    Reads up to `max_envelope_bytes` bytes (or as many as fit). Returns
    the recovered envelope prefix."""
    n_env = min(max_envelope_bytes, len(pixel_bytes) // 2)
    out = bytearray(n_env)
    for b in range(n_env):
        low = pixel_bytes[2 * b] & 0x0F
        high = pixel_bytes[2 * b + 1] & 0x0F
        out[b] = (high << 4) | low
    return bytes(out)


def _mandelbrot_calc_image_dims(payload_size: int, ext_len: int) -> "Tuple[int, int]":
    """Square (W, H) sized so the bit-packed envelope fits with the
    Mandelbrot fractal showing across the whole image.

    Total envelope bytes = header + payload.
    Pixel bytes needed = 2 * envelope_bytes (low nibble + high nibble per byte).
    Pixels needed = pixel_bytes / 3 (3 channels per pixel).
    """
    header_size = 8 + 1 + ext_len + 8 + 4 + 4   # 25 + ext_len
    envelope_bytes = payload_size + header_size
    pixel_bytes_needed = envelope_bytes * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    pixels_needed = (pixel_bytes_needed + 2) // 3
    for cap, dim in _IMAGE_TIERS:
        if pixels_needed <= dim * dim:
            return dim, dim
    side = math.ceil(math.sqrt(pixels_needed))
    side = max(_MIN_DIM, ((side + 1023) // 1024) * 1024)
    return side, side


def _mandelbrot_keystream(width: int, height: int) -> bytes:
    """Generate a deterministic full-size colored Mandelbrot keystream
    of length `width * height * 3` bytes (RGB-interleaved row-major).

    Seed is derived from the image dimensions plus a fixed salt, so the
    keystream is recoverable on the read side from the PNG/BMP container's
    declared dimensions alone.
    """
    from . import _mandelbrot as _m
    seed_bytes = _MANDELBROT_SALT + struct.pack(">II", width, height)
    seed = _m.derive_seed(seed_bytes)
    return _m.generate_keystream(width, height, seed)


def _xor_pixel_iter(pixel_iter: "Iterator[bytes]", keystream: bytes):
    """Wrap a pixel-byte iterator, XOR'ing each chunk byte-for-byte with
    successive bytes of the keystream. The keystream length equals the
    total bytes the iterator will yield (width*height*3 for RGB), so no
    modulo wrapping is needed — straight 1:1 XOR."""
    pos = 0
    klen = len(keystream)
    for chunk in pixel_iter:
        n = len(chunk)
        out = bytearray(n)
        for i in range(n):
            out[i] = chunk[i] ^ keystream[pos + i]
        pos += n
        if pos > klen:
            raise RuntimeError("Mandelbrot keystream exhausted: "
                                f"image bytes ({pos}) exceed keystream ({klen}).")
        yield bytes(out)


def _png_embed_v2_to_file(src_path: Path, src_ext: str, dst: Path,
                           cancel: Optional["CancellationToken"] = None,
                           progress: Optional[Callable[[float], None]] = None,
                           mandelbrot: bool = False) -> None:
    from .streaming_image import stream_png_write
    payload_size = src_path.stat().st_size
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_path.read_bytes(), src_ext, cancel)
        stream_png_write(dst, width, height, iter([pixel_bytes]), cancel, progress)
        return
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_png_write(dst, width, height, pixel_iter, cancel, progress)


def _bmp_embed_v2_to_file(src_path: Path, src_ext: str, dst: Path,
                           cancel: Optional["CancellationToken"] = None,
                           progress: Optional[Callable[[float], None]] = None,
                           mandelbrot: bool = False) -> None:
    from .streaming_image import stream_bmp_write
    payload_size = src_path.stat().st_size
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_path.read_bytes(), src_ext, cancel)
        stream_bmp_write(dst, width, height, iter([pixel_bytes]), cancel, progress)
        return
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_bmp_write(dst, width, height, pixel_iter, cancel, progress)


def _png_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path,
                              mandelbrot: bool = False) -> None:
    from .streaming_image import stream_png_write
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(src_bytes, src_ext)
        stream_png_write(dst, width, height, iter([pixel_bytes]))
        return
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height)
    stream_png_write(dst, width, height, pixel_iter)


def _bmp_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path,
                              mandelbrot: bool = False) -> None:
    from .streaming_image import stream_bmp_write
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(src_bytes, src_ext)
        stream_bmp_write(dst, width, height, iter([pixel_bytes]))
        return
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height)
    stream_bmp_write(dst, width, height, pixel_iter)


def _build_mandelbrot_image(src_bytes: bytes, src_ext: str,
                             cancel: Optional["CancellationToken"] = None
                             ) -> "Tuple[int, int, bytes]":
    """Build the bit-packed Mandelbrot image. Returns (width, height,
    pixel_bytes) ready to stream into stream_png_write / stream_bmp_write.

    Image dims are sized via _mandelbrot_calc_image_dims so the bit-packed
    envelope fits with the fractal occupying the full image. Each pixel
    byte's top 4 bits = colored fractal; bottom 4 bits = envelope nibble
    (or zero past the envelope, leaving the fractal undisturbed there).
    """
    payload_size = len(src_bytes)
    width, height = _mandelbrot_calc_image_dims(
        payload_size, len(src_ext.encode("utf-8")))
    if cancel is not None:
        cancel.check()
    fractal = _mandelbrot_keystream(width, height)
    if cancel is not None:
        cancel.check()
    # Build the envelope verbatim (header + payload, no pad — pad is
    # implicit in the "envelope ends before pixel stream ends" gap, where
    # the bottom-4-bit nibble defaults to zero leaving the pure fractal).
    header = _v2_header_bytes(payload_size, src_ext, width, height)
    envelope = header + src_bytes
    total_pixel_bytes = width * height * 3
    # Sanity: dims should always be large enough.
    if len(envelope) * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE > total_pixel_bytes:
        raise RuntimeError(
            f"Mandelbrot dim calc bug: envelope={len(envelope)} bytes needs "
            f"{len(envelope) * 2} pixel bytes but image holds {total_pixel_bytes}.")
    pixel_bytes = _mandelbrot_pack_envelope_into_fractal(
        envelope, fractal, total_pixel_bytes)
    return width, height, pixel_bytes


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
    return _extract_v2_dual_attempt(src, dst_path, cancel, stream_png_read)


def _bmp_extract_v2_to_file(src: Path, dst_path: Path,
                             cancel: Optional["CancellationToken"] = None) -> str:
    from .streaming_image import stream_bmp_read
    return _extract_v2_dual_attempt(src, dst_path, cancel, stream_bmp_read)


def _extract_v2_dual_attempt(src: Path, dst_path: Path,
                              cancel: Optional["CancellationToken"],
                              stream_reader) -> str:
    """Try plain v2 extraction first (same-category Stone — raw bytes in
    pixels, MAGIC_V2 at start). If that fails, try the Mandelbrot bit-pack
    extraction (cross-category Stone aesthetic — envelope nibbles in the
    bottom 4 bits of each pixel byte).

    Buffers the entire pixel byte stream into memory once. For 4096^2 RGB
    that's 48 MB temporarily — acceptable for the size range Stone uses.
    """
    width, height, it = stream_reader(src, cancel)
    pixel_bytes = bytearray()
    for chunk in it:
        if cancel is not None:
            cancel.check()
        pixel_bytes.extend(chunk)

    # Attempt 1: plain — raw byte stream with MAGIC_V2 at offset 0
    # (same-category Stone byte-passthrough).
    if len(pixel_bytes) >= 8 and bytes(pixel_bytes[:8]) == MAGIC_V2:
        return _extract_v2_from_pixel_iter(iter([bytes(pixel_bytes)]), dst_path, cancel)

    # Attempt 2: Mandelbrot bit-packed envelope — read low 4 bits of each
    # pixel byte and reassemble into envelope bytes.
    max_envelope_bytes = len(pixel_bytes) // 2
    if max_envelope_bytes >= 8:
        envelope = _mandelbrot_unpack_envelope_from_pixels(
            bytes(pixel_bytes), max_envelope_bytes)
        if envelope[:8] == MAGIC_V2:
            return _extract_v2_from_pixel_iter(iter([envelope]), dst_path, cancel)

    raise ValueError("v2 envelope: magic not found in plain pixel bytes "
                     "or in the bottom-4-bit Mandelbrot bit-packed stream.")


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
# Host: PLY (ASCII Polygon File Format) — envelope rides in `comment` lines
# ---------------------------------------------------------------------------
# PLY's header allows free-form `comment ...` lines that any conformant reader
# ignores. We base64 the envelope, split into 76-char chunks, and write one
# chunk per `comment` line. The geometry block is a single vertex at origin
# so the file loads in MeshLab / Blender / Open3D without complaint.

_PLY_HEADER = "ply\nformat ascii 1.0\n"
_PLY_FOOTER = ("element vertex 1\nproperty float x\nproperty float y\n"
               "property float z\nend_header\n0 0 0\n")
_PLY_COMMENT_TAG = "uc"   # short prefix on each comment line so extraction can
                          # ignore unrelated comments a user might paste in.


def _ply_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    body = base64.b64encode(env).decode("ascii")
    chunks = [body[i:i + 72] for i in range(0, len(body), 72)]
    lines = [f"comment {_PLY_COMMENT_TAG} {c}\n" for c in chunks]
    return (_PLY_HEADER + "".join(lines) + _PLY_FOOTER).encode("utf-8")


def _ply_extract(host: bytes) -> Tuple[bytes, str]:
    text = host.decode("utf-8", errors="replace")
    pieces: list[str] = []
    in_header = False
    for line in text.splitlines():
        s = line.strip()
        if s == "ply":
            in_header = True
            continue
        if not in_header:
            continue
        if s == "end_header":
            break
        if s.startswith("comment "):
            rest = s[len("comment "):].strip()
            if rest.startswith(_PLY_COMMENT_TAG + " "):
                pieces.append(rest[len(_PLY_COMMENT_TAG) + 1:])
    if not pieces:
        raise ValueError("PLY host: no Stone envelope comments found.")
    body = "".join(pieces)
    try:
        env = base64.b64decode(body, validate=True)
    except Exception as e:
        raise ValueError(f"PLY host: malformed base64 envelope: {e}")
    return _parse_envelope(env)


# ---------------------------------------------------------------------------
# Host: OBJ (Wavefront) — envelope rides in `#` comment lines
# ---------------------------------------------------------------------------
# OBJ readers ignore any line beginning with `#`. Same scheme as PLY: tagged
# comments carrying base64 chunks, then a single vertex so the file is
# structurally valid as a (degenerate) mesh.

_OBJ_COMMENT_TAG = "uc"


def _obj_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)
    body = base64.b64encode(env).decode("ascii")
    chunks = [body[i:i + 72] for i in range(0, len(body), 72)]
    lines = [f"# {_OBJ_COMMENT_TAG} {c}\n" for c in chunks]
    return ("".join(lines) + "v 0 0 0\n").encode("utf-8")


def _obj_extract(host: bytes) -> Tuple[bytes, str]:
    text = host.decode("utf-8", errors="replace")
    pieces: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            rest = s[1:].strip()
            if rest.startswith(_OBJ_COMMENT_TAG + " "):
                pieces.append(rest[len(_OBJ_COMMENT_TAG) + 1:])
    if not pieces:
        raise ValueError("OBJ host: no Stone envelope comments found.")
    body = "".join(pieces)
    try:
        env = base64.b64decode(body, validate=True)
    except Exception as e:
        raise ValueError(f"OBJ host: malformed base64 envelope: {e}")
    return _parse_envelope(env)


# ---------------------------------------------------------------------------
# Host: GLB (binary glTF) — envelope in a custom chunk after JSON+BIN
# ---------------------------------------------------------------------------
# GLB layout: 12-byte header (magic "glTF", version, total length) followed
# by a sequence of chunks. Each chunk: 4-byte length, 4-byte type, payload.
# Standard chunk types are JSON (0x4E4F534A) and BIN (0x004E4942). The spec
# says readers MUST ignore unknown chunk types, so we append a chunk with
# type b"ucMs" carrying the envelope. The JSON/BIN chunks describe a single
# degenerate vertex so any glTF viewer loads the file cleanly.
#
# Chunks must be 4-byte aligned. JSON chunks pad with 0x20 (space), BIN
# chunks pad with 0x00. The custom envelope chunk pads with 0x00.

_GLB_MAGIC = b"glTF"
_GLB_VERSION = 2
_GLB_CHUNK_JSON = b"JSON"
_GLB_CHUNK_BIN = b"BIN\x00"
_GLB_CHUNK_UCMS = b"ucMs"

# Minimal valid glTF 2.0 JSON: one node, one mesh, one degenerate triangle
# referencing a 36-byte BIN buffer (3 vertices × 3 floats × 4 bytes). The
# triangle is degenerate (all three vertices at origin) so it has zero area
# and renders nothing — but the file is well-formed.
_GLB_MIN_JSON = (
    b'{"asset":{"version":"2.0"},"scenes":[{"nodes":[0]}],'
    b'"nodes":[{"mesh":0}],"meshes":[{"primitives":[{"attributes":{"POSITION":0}}]}],'
    b'"accessors":[{"bufferView":0,"componentType":5126,"count":3,"type":"VEC3",'
    b'"min":[0,0,0],"max":[0,0,0]}],'
    b'"bufferViews":[{"buffer":0,"byteLength":36,"byteOffset":0}],'
    b'"buffers":[{"byteLength":36}]}'
)
_GLB_MIN_BIN = b"\x00" * 36


def _pad4(n: int) -> int:
    """Bytes needed to round n up to a multiple of 4."""
    return (-n) & 3


def _glb_embed(src_bytes: bytes, src_ext: str) -> bytes:
    env = _build_envelope(src_bytes, src_ext)

    json_pad = b" " * _pad4(len(_GLB_MIN_JSON))
    json_chunk_data = _GLB_MIN_JSON + json_pad
    bin_pad = b"\x00" * _pad4(len(_GLB_MIN_BIN))
    bin_chunk_data = _GLB_MIN_BIN + bin_pad
    env_pad = b"\x00" * _pad4(len(env))
    env_chunk_data = env + env_pad

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack("<I", len(data)) + tag + data

    body = (chunk(_GLB_CHUNK_JSON, json_chunk_data)
            + chunk(_GLB_CHUNK_BIN, bin_chunk_data)
            + chunk(_GLB_CHUNK_UCMS, env_chunk_data))
    total_len = 12 + len(body)
    header = _GLB_MAGIC + struct.pack("<II", _GLB_VERSION, total_len)
    return header + body


def _glb_extract(host: bytes) -> Tuple[bytes, str]:
    if len(host) < 12 or host[:4] != _GLB_MAGIC:
        raise ValueError("GLB host: missing glTF magic.")
    version, total_len = struct.unpack("<II", host[4:12])
    if version != _GLB_VERSION:
        raise ValueError(f"GLB host: unsupported glTF version {version}.")
    p = 12
    while p + 8 <= len(host):
        chunk_len, = struct.unpack("<I", host[p:p + 4])
        tag = host[p + 4:p + 8]
        data = host[p + 8:p + 8 + chunk_len]
        p += 8 + chunk_len
        if tag == _GLB_CHUNK_UCMS:
            # Strip trailing zero padding before parsing
            return _parse_envelope(data.rstrip(b"\x00"))
    raise ValueError("GLB host: no Stone envelope chunk (ucMs) found.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EMBED = {
    ".wav": _wav_embed, ".png": _png_embed, ".bmp": _bmp_embed,
    ".txt": _txt_embed,
    ".ply": _ply_embed, ".obj": _obj_embed, ".glb": _glb_embed,
    ".aiff": _aiff_embed,
    # .flac is dispatched specially below (needs Path target for FFmpeg).
}
_EXTRACT = {
    ".wav": _wav_extract, ".png": _png_extract, ".bmp": _bmp_extract,
    ".txt": _txt_extract,
    ".ply": _ply_extract, ".obj": _obj_extract, ".glb": _glb_extract,
    ".aiff": _aiff_extract,
    ".flac": _flac_extract_from_bytes,
}


def can_embed_into(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EMBED or ext in (".mkv", ".py", ".flac")


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
               cancel: Optional["CancellationToken"] = None,
               cross_category: bool = False) -> None:
    """Whole-bytes-in-memory embed. Used for small files.

    `cross_category` triggers the aesthetic encoder: Mandelbrot XOR for
    PNG/BMP image targets. Audio targets (WAV/AIFF/FLAC) get the music
    encoder via separate dispatch (see _embed_audio_music below).
    """
    dst_ext = dst_ext.lower()
    if dst_ext == ".mkv":
        _mkv_embed_to_file(payload, src_ext, dst)
        return
    if dst_ext == ".png":
        _png_embed_v2_from_bytes(payload, src_ext, dst, mandelbrot=cross_category)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_from_bytes(payload, src_ext, dst, mandelbrot=cross_category)
        return
    if dst_ext == ".wav" and cross_category:
        # Cross-category audio target: render music samples with payload
        # in low 4 bits per channel. Same-category WAV (audio source)
        # falls through to the classic _wav_embed via _EMBED dispatch.
        dst.write_bytes(_wav_embed_music(payload, src_ext))
        return
    if dst_ext == ".aiff" and cross_category:
        dst.write_bytes(_aiff_embed_music(payload, src_ext))
        return
    if dst_ext == ".flac":
        # FLAC always goes through FFmpeg. Cross-category uses the music
        # encoder; same-category uses the classic 8 kHz mono envelope WAV
        # (re-encoded to FLAC for storage). Both round-trip losslessly.
        if cross_category:
            _flac_embed_music(payload, src_ext, dst)
        else:
            _flac_embed(payload, src_ext, dst)
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
                        progress: Optional[Callable[[float], None]] = None,
                        cross_category: bool = False) -> None:
    """Path-based streaming embed. Used for files above the streaming threshold."""
    dst_ext = dst_ext.lower()
    if dst_ext == ".png":
        _png_embed_v2_to_file(src_path, src_ext, dst, cancel, progress,
                              mandelbrot=cross_category)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_to_file(src_path, src_ext, dst, cancel, progress,
                              mandelbrot=cross_category)
        return
    if dst_ext == ".py":
        _py_embed_to_file(src_path, src_ext, dst, cancel)
        return
    # WAV / TXT / MKV: whole-file bytes API still used; fall through.
    src_bytes = src_path.read_bytes()
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel, cross_category=cross_category)


def convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
            cancel: CancellationToken, progress: Callable[[float], None],
            *, cross_category: bool = False) -> None:
    """Top-level entry the conversion queue calls when Masquerade Mode is on.

    Two cases:
      1. Source is itself a masquerade host AND contains a valid envelope:
         EXTRACT to recover the original payload. If dst_ext matches the
         recovered ext (or isn't a host itself), write payload as-is. If
         dst_ext is a different host, re-embed.
      2. Otherwise: EMBED the source bytes into a fresh host of dst_ext.

    For files at/above streaming_threshold, uses path-based streaming
    helpers — never materializes the whole payload in memory.

    `cross_category` indicates the source and target are in different media
    categories (image vs audio vs doc vs model). When True AND the dst is
    an image (PNG/BMP) or audio (WAV/AIFF/FLAC) host, the embed applies
    the corresponding aesthetic encoder (Mandelbrot keystream / music
    bit-pack). Same-category Stone keeps byte-passthrough behavior.
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
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel,
                  cross_category=cross_category)
        progress(1.0)
        return

    # Decide streamed vs whole-file path based on source size.
    # .py is always routed through the path-based variant (regardless of
    # size) so the embedded reconstruction script preserves the original
    # filename instead of a "payload.<ext>" placeholder.
    src_size = src.stat().st_size
    threshold = streaming_threshold(dst_ext)
    if dst_ext == ".py" or (src_size >= threshold and dst_ext in (".png", ".bmp")):
        cancel.check()
        progress(0.1)
        _embed_streamed_to(dst, src, src_ext, dst_ext, cancel, progress,
                           cross_category=cross_category)
        progress(1.0)
        return

    src_bytes = src.read_bytes()
    cancel.check()
    progress(0.3)
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel,
              cross_category=cross_category)
    progress(1.0)
