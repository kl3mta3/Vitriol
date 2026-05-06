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
MAGIC_V3 = b"UCMSv3\0\0"  # 8 bytes — encrypted Mandelbrot envelope (PNG/BMP)
MAGIC_V3_AUDIO = b"uM03\0\0\0\0"  # 8 bytes — encrypted music envelope (WAV/AIFF/FLAC)
MAGIC_V3_3D = b"UC3Dv3\0\0"  # 8 bytes — encrypted 3D envelope (PLY/OBJ/GLB)
MAGIC_V3_VIDEO = b"UCMv3\0\0\0"  # 8 bytes — encrypted animated-Mandelbrot envelope (MKV)

# Read-write capable host extensions. Used by the registry + dropdown filter
# when Philosopher's Stone (a.k.a. Masquerade) mode is on.
TARGETS = {".wav", ".png", ".bmp", ".txt", ".mkv", ".py",
           ".ply", ".obj", ".glb",
           ".aiff", ".flac",
           ".zip"}
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
    # .zip is a Stone host only when it has exactly one member named
    # `original.*`. Cheap: stdlib zipfile namelist, no decompression.
    if ext == ".zip":
        try:
            import zipfile as _zf
            with _zf.ZipFile(Path(path)) as z:
                names = z.namelist()
                return (len(names) == 1
                        and names[0].startswith(_ZIP_MEMBER_PREFIX + "."))
        except Exception:
            return False
    try:
        with open(path, "rb") as f:
            head = f.read(64 * 1024)
    except OSError:
        return False
    if MAGIC in head or MAGIC_V2 in head:
        return True
    # MKV: legacy plaintext path stamped a `UCMSv1` title tag. v3 MKV files
    # don't write that tag (it leaked the format identity). For v3 we have
    # to do a one-frame FFmpeg decode + bit-unpack probe — more expensive
    # but only fires when Stone is on AND the file's actual ext is .mkv.
    if ext == ".mkv":
        if b"UCMSv1" in head:
            return True
        try:
            return _mkv_v3_envelope_probe(Path(path))
        except Exception:
            return False
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
    """Return True if a Stone envelope is present in the pixel stream:
      - Plain UCMSv2 magic in raw pixel bytes (same-category Stone), OR
      - UCMSv3 magic via k=1 scatter-unpack (cross-category Stone v3), OR
      - UCMSv2 magic via legacy k=4 scatter-unpack (older v2 Stone files).

    For the contiguous-magic case a 64 KB probe suffices. For scatter
    cases the magic's bits are dispersed across the full image, so we
    read ALL pixel bytes once. ~12 MB for 2048² RGB, bounded."""
    scratch = bytearray()
    found_plain = False
    for chunk in pixel_iter:
        scratch.extend(chunk)
        if MAGIC_V2 in scratch:
            found_plain = True
            break
        if len(scratch) >= probe_bytes:
            break
    if found_plain:
        try:
            for _ in pixel_iter:
                pass
        except Exception:
            pass
        return True
    # Drain the rest of the iterator so we have the whole image for scatter probes.
    for chunk in pixel_iter:
        scratch.extend(chunk)
    if not scratch:
        return False
    total = len(scratch)
    if total < 16:
        return False
    # Probe 1: UCMSv3 (k=1) bit-pack. Read the first 64 envelope bytes.
    env_prefix_v3 = _mandelbrot_unpack_envelope_from_pixels(
        bytes(scratch), 64, total_pixel_bytes=total)
    if env_prefix_v3.startswith(MAGIC_V3):
        return True
    # Probe 2: legacy UCMSv2 (k=4) bit-pack for backward-compat.
    env_prefix_v2 = _mandelbrot_unpack_envelope_from_pixels_v2_legacy(
        bytes(scratch), 64, total_pixel_bytes=total)
    return MAGIC_V2 in env_prefix_v2

# MKV host parameters. v3 dropped the 42 fps fingerprint in favor of
# standard 30 fps + a 10-second minimum (300 frames). Each frame carries
# part of the encrypted v3 envelope as 1 bit per pixel byte (k=1, same as
# the image side); the top 7 bits hold the animated Mandelbrot fractal.
# For payloads that don't fill 300 frames, the tail frames are pure
# fractal (zero LSBs), making short videos visually indistinguishable
# from a real Mandelbrot flythrough.
MKV_FRAME_W = 1024
MKV_FRAME_H = 1024
MKV_BYTES_PER_FRAME = MKV_FRAME_W * MKV_FRAME_H * 3        # rgb24, total pixel bytes
MKV_ENVELOPE_BYTES_PER_FRAME = MKV_BYTES_PER_FRAME // 8    # k=1 bit-pack ⇒ 1 byte env per 8 pixel bytes
MKV_FPS = 30
MKV_MIN_FRAMES = 300                                        # 10-second floor at 30 fps

# The Mandelbrot fractal is rendered at this internal resolution per frame
# and bilinear-upscaled to MKV_FRAME_W × MKV_FRAME_H before bit-packing.
# Rendering at full 1024² on every frame would take 2-3s per frame ⇒ ~15
# minutes per 10-sec output. The bit-pack carrier is always at 1024² so
# capacity isn't affected — only the fractal's pixel-perfect detail is.
# 384 keeps recognizably crisp boundary detail while cutting per-frame
# fractal cost ~7×.
MKV_FRACTAL_RENDER_DIM = 384


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


def _wav_embed_music(src_bytes: bytes, src_ext: str,
                      password: bytes = b"") -> bytes:
    """Cross-category Stone audio target. Builds an encrypted v3 audio
    envelope (AES-256-CTR + PBKDF2 under `password`) and bit-packs it into
    music samples (low 4 bits/channel). Empty password → deterministic
    default key shared by all Transmute installs of the same version."""
    from . import _music as _m
    envelope = _v3_audio_envelope(src_bytes, src_ext, password)
    pcm, n_frames = _m.encode_music_envelope(envelope)
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


def _aiff_embed_music(src_bytes: bytes, src_ext: str,
                       password: bytes = b"") -> bytes:
    """Cross-category AIFF: encrypted v3 audio envelope bit-packed into
    big-endian PCM samples. Mirrors _wav_embed_music with BE encoding."""
    from . import _music as _m
    envelope = _v3_audio_envelope(src_bytes, src_ext, password)
    pcm, n_frames = _m.encode_music_envelope_be(envelope)
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


def _aiff_extract(host: bytes, password: bytes = b"") -> Tuple[bytes, str]:
    """Dual-attempt AIFF extract: classic UCMSv1-in-SSND first, then music
    mode (uM01 legacy or v3 encrypted)."""
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
    head = _m.decode_music_bytes_be(ssnd_blob, 16)
    if head.startswith(b"uM01"):
        env = _m.decode_music_payload_be(ssnd_blob)
        return _parse_envelope(env)
    if head.startswith(MAGIC_V3_AUDIO):
        ciphertext_len = struct.unpack(">Q", head[8:16])[0]
        total = V3_AUDIO_HEADER_SIZE + ciphertext_len
        full_env = _m.decode_music_bytes_be(ssnd_blob, total)
        return _parse_v3_audio_envelope(full_env, password)
    raise ValueError("AIFF music mode: no recognized envelope magic.")


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


def _flac_embed_music(src_bytes: bytes, src_ext: str, dst: Path,
                       password: bytes = b"") -> None:
    """Cross-category FLAC: write encrypted v3 music WAV to temp, re-encode
    via FFmpeg. FLAC is bit-exact, so payload bits survive losslessly."""
    import tempfile
    wav_bytes = _wav_embed_music(src_bytes, src_ext, password=password)
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


def _flac_extract(src: Path, password: bytes = b"") -> Tuple[bytes, str]:
    """Decode the FLAC to WAV via FFmpeg, then route to _wav_extract.
    Note: takes a Path (not bytes) because FFmpeg needs a file. The
    matching _EXTRACT entry adapts via _flac_extract_from_bytes below."""
    import tempfile
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        _flac_to_wav_via_ffmpeg(src, tmp_wav)
        return _wav_extract(tmp_wav.read_bytes(), password=password)
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _flac_extract_from_bytes(host: bytes,
                              password: bytes = b"") -> Tuple[bytes, str]:
    """Bytes-API wrapper for _EXTRACT dispatch."""
    import tempfile
    tmp_flac = Path(tempfile.mkstemp(suffix=".flac")[1])
    try:
        tmp_flac.write_bytes(host)
        return _flac_extract(tmp_flac, password=password)
    finally:
        try: tmp_flac.unlink()
        except OSError: pass


def _wav_extract(host: bytes, password: bytes = b"") -> Tuple[bytes, str]:
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
    # Detect format by reading the first 16 bit-packed bytes (cheap) and
    # checking the magic. Two formats coexist:
    #   uM01  — legacy zlib+UCMSv1 audio Stone (pre-v3)
    #   uM03* — encrypted v3 audio Stone (MAGIC_V3_AUDIO)
    head = _m.decode_music_bytes_le(data_blob, 16)
    if head.startswith(b"uM01"):
        env = _m.decode_music_payload_le(data_blob)
        return _parse_envelope(env)
    if head.startswith(MAGIC_V3_AUDIO):
        ciphertext_len = struct.unpack(">Q", head[8:16])[0]
        total = V3_AUDIO_HEADER_SIZE + ciphertext_len
        full_env = _m.decode_music_bytes_le(data_blob, total)
        return _parse_v3_audio_envelope(full_env, password)
    raise ValueError("WAV music mode: no recognized envelope magic.")


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
    """Legacy plaintext UCMSv1 path: pad envelope to N whole frames.
    Returns (padded_bytes, n_real_frames, n_total_frames, n_padding_frames).
    Kept ONLY for the legacy embed branch — v3 video uses bit-pack and
    different framing math via `_mkv_v3_frame_count`."""
    n_real_frames = max(1, math.ceil(len(env) / MKV_BYTES_PER_FRAME))
    n_total_frames = max(MKV_MIN_FRAMES, n_real_frames)
    n_padding = n_total_frames - n_real_frames
    total_bytes = n_total_frames * MKV_BYTES_PER_FRAME
    padded = env + b"\x00" * (total_bytes - len(env))
    return padded, n_real_frames, n_total_frames, n_padding


def _mkv_v3_envelope_probe(src: Path) -> bool:
    """Cheap detection: FFmpeg-decode just the first frame, bit-unpack the
    first 8 bytes via the same scatter pattern the encoder uses, and check
    for `MAGIC_V3_VIDEO`. Used by `has_envelope` to identify v3 MKVs that
    don't carry the legacy `UCMSv1` title tag."""
    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    args = [
        str(ffmpeg), "-y", "-i", str(src),
        "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-",
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    raw, _ = proc.communicate(timeout=60)
    if proc.returncode != 0 or len(raw) < MKV_BYTES_PER_FRAME:
        return False
    head = _mandelbrot_unpack_envelope_from_pixels(
        raw[:MKV_BYTES_PER_FRAME], len(MAGIC_V3_VIDEO),
        total_pixel_bytes=MKV_BYTES_PER_FRAME)
    return head.startswith(MAGIC_V3_VIDEO)


def _mkv_v3_frame_count(envelope_size: int) -> int:
    """Number of frames needed for a v3 video output. At least
    MKV_MIN_FRAMES (10-sec floor at 30 fps); larger envelopes extend
    naturally past the floor."""
    real = max(1, math.ceil(envelope_size / MKV_ENVELOPE_BYTES_PER_FRAME))
    return max(MKV_MIN_FRAMES, real)


def _mkv_choose_base_viewport(envelope: bytes):
    """Pick a base viewport that's known interesting BEFORE rendering any
    frames. Returns the 7-tuple (cx, cy, half_width, r_phase, g_phase,
    b_phase, palette_id).

    Uses `derive_seed_unjittered` — the curated viewport's exact center
    (no per-source jitter), guaranteed by the curated table to land on
    the boundary at the viewport's native hw. Validates at the most
    zoomed-in extreme of the planned animation as a belt-and-suspenders
    check; if somehow that fails (the zoom is too tight for this
    particular curated viewport), swap once to the universal whole-set
    view so every frame in the clip still shares the same base.
    """
    from . import _mandelbrot as _m
    base_cx, base_cy, base_hw, r_ph, g_ph, b_ph, palette_id = (
        _m.derive_seed_unjittered(envelope))
    # Belt-and-suspenders: test the most zoomed-in frame (smallest hw).
    test_hw = base_hw * _MKV_ZOOM_LO
    if not _m.viewport_is_interesting(128, 128, base_cx, base_cy, test_hw):
        base_cx, base_cy, base_hw = _m._FALLBACK_VIEWPORT
    return (base_cx, base_cy, base_hw, r_ph, g_ph, b_ph, palette_id)


# Zoom range: cur_hw spans base_hw × _MKV_ZOOM_LO at frame 0
# (most zoomed-IN) up to base_hw × _MKV_ZOOM_HI at the final frame
# (most zoomed-OUT). 0.4x → 1.6x = 4× total zoom-out across the clip,
# stays close to the curated viewport's sweet spot so we don't drift
# into uniform regions or shrink the fractal to a dot.
_MKV_ZOOM_LO = 0.4
_MKV_ZOOM_HI = 1.6


def _mkv_frame_viewport(base_seed, frame_idx: int, n_frames: int):
    """Per-frame Mandelbrot seed for the v3 video flythrough. Takes a
    pre-validated base seed (from `_mkv_choose_base_viewport`) and
    animates a smooth zoom-out + slow palette-phase drift around the
    fixed base center. Returns the 7-tuple
    (cx, cy, half_width, r_phase, g_phase, b_phase, palette_id) that
    `_mandelbrot.generate_keystream` accepts.

    Animation rules:
      - Center stays FIXED on the base viewport — no per-frame pan.
        Same fractal, slowly revealing more context.
      - Zoom goes from `_MKV_ZOOM_LO * base_hw` (frame 0, tight)
        smoothly up to `_MKV_ZOOM_HI * base_hw` (last frame, wide).
        Exponential interpolation so each step is a constant
        multiplicative ratio (visually smooth).
      - Palette phases drift sinusoidally over the clip so colors
        cycle gently — gives the fractal's body and arms a lively
        "breathing" feel without changing the underlying shape.
    """
    base_cx, base_cy, base_hw, r_ph, g_ph, b_ph, palette_id = base_seed

    t = (frame_idx / max(1, n_frames - 1)) if n_frames > 1 else 0.0

    # Smooth zoom-out: cur_hw grows from base_hw × LO to base_hw × HI.
    cur_hw = base_hw * (_MKV_ZOOM_LO ** (1.0 - t)) * (_MKV_ZOOM_HI ** t)

    # Palette drift: ±π/3 over the clip, channels offset by 120° / 240°
    # so the color shift moves through hue space rather than just
    # brightening/darkening uniformly.
    drift = math.sin(t * 2.0 * math.pi) * (math.pi / 3.0)
    return (base_cx, base_cy, cur_hw,
            r_ph + drift,
            g_ph + drift * 0.7,
            b_ph + drift * 1.3,
            palette_id)


def _mkv_build_frames_iter(envelope: bytes, n_frames: int):
    """Yield exactly n_frames pixel-byte buffers (each MKV_BYTES_PER_FRAME
    long) ready for FFmpeg's rawvideo stdin. Each frame renders the SAME
    base Mandelbrot viewport (chosen once + validated up-front) at a
    smoothly-shifting zoom factor, with `MKV_ENVELOPE_BYTES_PER_FRAME`
    bytes of the v3 envelope bit-packed into pixel LSBs. Tail frames past
    the envelope use empty bit-packs (pure fractal).

    The fractal is computed at MKV_FRACTAL_RENDER_DIM (default 384) and
    bilinear-upscaled to MKV_FRAME_W × MKV_FRAME_H before bit-packing.
    Bit-pack runs at the full output resolution so carrier capacity is
    unchanged.

    `safety_net=False` is critical here: the per-frame fallback inside
    `generate_keystream` would otherwise swap mid-clip when individual
    frames cross into uniform regions, causing the "different fractals
    flickering" effect the user reported. The base viewport is
    pre-validated by `_mkv_choose_base_viewport` so we don't need a
    per-frame fallback — the chosen base stays interesting throughout
    the zoom-out range."""
    from . import _mandelbrot as _m
    from PIL import Image as _PIL
    env_len = len(envelope)
    base_seed = _mkv_choose_base_viewport(envelope)
    for f in range(n_frames):
        seed = _mkv_frame_viewport(base_seed, f, n_frames)
        # Render at reduced internal dim then bilinear-upscale.
        small = _m.generate_keystream(MKV_FRACTAL_RENDER_DIM,
                                       MKV_FRACTAL_RENDER_DIM, seed,
                                       safety_net=False)
        if MKV_FRACTAL_RENDER_DIM != MKV_FRAME_W:
            img = _PIL.frombuffer(
                "RGB", (MKV_FRACTAL_RENDER_DIM, MKV_FRACTAL_RENDER_DIM),
                small, "raw", "RGB", 0, 1)
            img = img.resize((MKV_FRAME_W, MKV_FRAME_H),
                              _PIL.Resampling.BILINEAR)
            fractal = img.tobytes()
        else:
            fractal = small
        # Slice the envelope chunk for this frame.
        chunk_start = f * MKV_ENVELOPE_BYTES_PER_FRAME
        chunk_end = min(env_len, chunk_start + MKV_ENVELOPE_BYTES_PER_FRAME)
        chunk = envelope[chunk_start:chunk_end] if chunk_start < env_len else b""
        # Bit-pack the chunk into the fractal's LSBs (k=1 scatter pattern).
        # The scatter stride is a function of total_pixel_bytes only, so
        # both encoder and decoder agree on the pattern without sharing
        # any side-channel.
        pixel_bytes = _mandelbrot_pack_envelope_into_fractal(
            chunk, fractal, MKV_BYTES_PER_FRAME)
        yield pixel_bytes


def _mkv_embed_to_file(src_bytes: bytes, src_ext: str, dst: Path,
                        cross_category: bool = False,
                        password: bytes = b"") -> None:
    """Encode the source as a Matroska/FFV1 video. Cross-category outputs
    (image/audio/doc → MKV) build a v3 envelope encrypted under `password`
    and bit-pack it across an animated-Mandelbrot frame sequence (10-sec
    minimum, 30 fps, 1024×1024). Same-category video → MKV (rare) keeps
    the legacy plaintext UCMSv1 path for backward compatibility."""
    if cross_category:
        envelope = _v3_video_envelope(src_bytes, src_ext, password)
        n_frames = _mkv_v3_frame_count(len(envelope))
        frames_iter = _mkv_build_frames_iter(envelope, n_frames)
        # No identifying metadata tags — the v3 magic at the start of the
        # bit-packed pixel stream is identification enough, and the old
        # UC_PAYLOAD_SIZE / UC_ORIG_EXT tags would leak source size and
        # extension in the clear (defeats the v3 "ext is hidden" property).
        metadata_args: list[str] = []
        payload_iter = frames_iter
    else:
        env = _build_envelope(src_bytes, src_ext)
        padded, n_real, n_total, n_pad = _mkv_pad_payload(env)
        n_frames = n_total
        # Legacy plaintext path uses the title tag for has_envelope detection
        # (decoder fall-through path); the metadata is OK here because nothing
        # is encrypted to begin with.
        metadata_args = [
            "-metadata", "title=UCMSv1",
            "-metadata", f"UC_PAYLOAD_SIZE={len(src_bytes)}",
            "-metadata", f"UC_REAL_FRAMES={n_real}",
            "-metadata", f"UC_PADDING_FRAMES={n_pad}",
            "-metadata", f"UC_FRAME_W={MKV_FRAME_W}",
            "-metadata", f"UC_FRAME_H={MKV_FRAME_H}",
            "-metadata", f"UC_ORIG_EXT={src_ext}",
        ]
        # Single-buffer iterator since legacy path holds the full padded
        # blob in memory (was the prior behavior).
        payload_iter = iter([padded])

    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    # FFV1: mathematically lossless intra-only codec. Matroska refuses raw
    # RGB but accepts FFV1, which decodes pixel-for-pixel to the original
    # input — exactly what the bit-packed envelope needs.
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
        *metadata_args,
        str(dst),
    ]
    proc = subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, creationflags=creationflags,
    )
    try:
        for chunk in payload_iter:
            proc.stdin.write(chunk)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    _, stderr = proc.communicate(timeout=1200)
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg MKV embed failed (exit {proc.returncode}): {tail}")


def _mkv_extract_from_file(src: Path,
                            password: bytes = b"") -> Tuple[bytes, str]:
    """Pipe the MKV through FFmpeg → raw rgb24 frames → bit-unpack the
    v3 envelope (or fall back to legacy plaintext-bytes-in-pixels). v3
    uses the password to decrypt; legacy ignores it.

    Dual-detect by reading the first frame's bit-packed magic. If the
    bit-unpacked magic is `MAGIC_V3_VIDEO`, decrypt under `password`.
    Otherwise, assume the legacy UCMSv1 path (raw envelope bytes packed
    consecutively into pixel bytes) and fall through to `_parse_envelope`.
    """
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
    raw, stderr = proc.communicate(timeout=1200)
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"FFmpeg MKV extract failed (exit {proc.returncode}): {tail}")

    # Probe the first frame for v3 magic via the bit-pack scatter pattern.
    # The scatter stride is a function of total_pixel_bytes only; using
    # MKV_BYTES_PER_FRAME for the per-frame probe matches what the encoder
    # used per frame.
    first_frame = raw[:MKV_BYTES_PER_FRAME]
    if len(first_frame) >= MKV_BYTES_PER_FRAME:
        head_unpacked = _mandelbrot_unpack_envelope_from_pixels(
            first_frame, V3_VIDEO_HEADER_SIZE,
            total_pixel_bytes=MKV_BYTES_PER_FRAME)
        if head_unpacked.startswith(MAGIC_V3_VIDEO):
            ciphertext_len = struct.unpack(">Q", head_unpacked[8:16])[0]
            total_env = V3_VIDEO_HEADER_SIZE + ciphertext_len
            # How many whole frames cover the envelope?
            frames_needed = math.ceil(total_env / MKV_ENVELOPE_BYTES_PER_FRAME)
            # Walk through that many frames, bit-unpack each, concatenate.
            envelope_buf = bytearray()
            for f in range(frames_needed):
                fstart = f * MKV_BYTES_PER_FRAME
                fend = fstart + MKV_BYTES_PER_FRAME
                if fend > len(raw):
                    break
                frame_pixels = raw[fstart:fend]
                # Each frame yields up to MKV_ENVELOPE_BYTES_PER_FRAME envelope bytes.
                want = min(MKV_ENVELOPE_BYTES_PER_FRAME, total_env - len(envelope_buf))
                chunk = _mandelbrot_unpack_envelope_from_pixels(
                    frame_pixels, want, total_pixel_bytes=MKV_BYTES_PER_FRAME)
                envelope_buf.extend(chunk)
                if len(envelope_buf) >= total_env:
                    break
            return _parse_v3_video_envelope(bytes(envelope_buf[:total_env]), password)

    # Legacy plaintext UCMSv1 fall-back.
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

# Stone v3 bit-pack: 1 bit of payload per pixel byte (k=1).
# Eight pixel bytes carry one envelope byte. Top 7 bits per pixel byte
# hold the fractal at 128 levels per channel (perceptually pristine).
_PAYLOAD_BITS_PER_CHANNEL = 1
_PAYLOAD_MASK = (1 << _PAYLOAD_BITS_PER_CHANNEL) - 1   # = 0b1
_FRACTAL_MASK = (~_PAYLOAD_MASK) & 0xFF                 # = 0b11111110
_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE = 8 // _PAYLOAD_BITS_PER_CHANNEL  # = 8


def _mandelbrot_scatter_indices(num_pairs: int) -> "Tuple[int, int]":
    """Pick a stride coprime to num_pairs so envelope bytes scatter
    uniformly across the image when written at positions
    `(i * stride) mod num_pairs`. Returns (stride, _unused).

    Stride is derived deterministically from num_pairs alone, so the
    decoder uses identical scattering. ~62% (golden-ratio fraction) of
    num_pairs gives well-distributed coverage."""
    import math as _math
    if num_pairs <= 1:
        return 1, 0
    target = max(7, int(num_pairs * 0.6180339887498949))
    if target & 1 == 0:
        target += 1
    # Bump until coprime with num_pairs.
    while _math.gcd(target, num_pairs) != 1:
        target += 2
        if target >= num_pairs:
            return 1, 0  # degenerate fallback (always coprime to 1)
    return target, 0


def _mandelbrot_pack_envelope_into_fractal(envelope: bytes, fractal: bytes,
                                             total_pixel_bytes: int) -> bytes:
    """NumPy-vectorized bit-pack with scatter, k=1. Each envelope byte's
    8 bits land at scattered pixel-octet positions derived from image
    dimensions, so the data-noise (1 bit per channel) spreads uniformly.

    Top 7 bits of every pixel byte = fractal color (128 levels — pristine).
    Bottom 1 bit = one payload bit at scattered position
    `(i * stride) mod num_octets` for envelope byte index i, OR untouched
    fractal LSB (≡ zero — see fractal palette code) for octets past the
    envelope.
    """
    import numpy as np
    fractal_arr = np.frombuffer(fractal, dtype=np.uint8)[:total_pixel_bytes]
    # Start with fractal top bits, payload bit cleared
    out = (fractal_arr & _FRACTAL_MASK).copy()
    env_len = len(envelope)
    if env_len > 0:
        num_octets = total_pixel_bytes // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
        n_env = min(env_len, num_octets)
        stride, _ = _mandelbrot_scatter_indices(num_octets)
        env_arr = np.frombuffer(envelope, dtype=np.uint8)[:n_env]
        # Scattered octet index for each envelope byte.
        octet_idx = (np.arange(n_env, dtype=np.int64) * stride) % num_octets
        # Each octet starts at pixel byte position octet_idx * 8.
        base = octet_idx * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
        # For each envelope byte, write 8 bits at positions base+0..base+7.
        # np.unpackbits explodes [n] uint8 → [n*8] uint8 of {0,1}, big-endian
        # bit order (MSB first). We want LSB-first so bit i goes to byte i:
        # use ">u1" view + bit-reverse, OR just: bits[i*8 + b] = (env[i] >> b) & 1.
        bits = np.unpackbits(env_arr).reshape(n_env, 8)[:, ::-1].reshape(-1)
        # Compute the 8 destination indices per envelope byte.
        offsets = np.arange(_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE, dtype=np.int64)
        dest = (base[:, None] + offsets[None, :]).reshape(-1)
        out[dest] = (fractal_arr[dest] & _FRACTAL_MASK) | bits
    return out.tobytes()


def _mandelbrot_unpack_envelope_from_pixels(pixel_bytes: bytes,
                                              max_envelope_bytes: int,
                                              total_pixel_bytes: int = -1) -> bytes:
    """NumPy-vectorized bit-unpack matching the scatter pattern from
    _mandelbrot_pack_envelope_into_fractal at k=1. Reassembles envelope
    bytes from 8 scattered pixel-byte LSBs each.

    `total_pixel_bytes` is the FULL image size used to compute the same
    scatter stride as the encoder. Defaults to len(pixel_bytes) when -1
    (caller provided the whole image). Must be passed explicitly when
    `pixel_bytes` is a partial buffer (e.g. the 64 KB has_envelope probe)."""
    import numpy as np
    if total_pixel_bytes < 0:
        total_pixel_bytes = len(pixel_bytes)
    num_octets_full = total_pixel_bytes // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    available_octets = len(pixel_bytes) // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    if num_octets_full == 0:
        return b""
    n_env = min(max_envelope_bytes, num_octets_full)
    if n_env == 0:
        return b""
    stride, _ = _mandelbrot_scatter_indices(num_octets_full)
    px = np.frombuffer(pixel_bytes, dtype=np.uint8)
    octet_idx = (np.arange(n_env, dtype=np.int64) * stride) % num_octets_full
    base = octet_idx * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    in_range = base + (_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE - 1) < len(pixel_bytes)
    out = np.zeros(n_env, dtype=np.uint8)
    valid_base = base[in_range]
    if valid_base.size > 0:
        offsets = np.arange(_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE, dtype=np.int64)
        # Shape: (n_valid, 8). Each row holds 8 pixel-byte LSBs in scatter order.
        gathered = px[(valid_base[:, None] + offsets[None, :])] & _PAYLOAD_MASK
        # Reverse to MSB-first so np.packbits assembles correctly.
        bits = gathered[:, ::-1]
        # packbits along axis -1 with bitorder='big' — assemble each 8-bit row to a byte.
        bytes_recovered = np.packbits(bits.astype(np.uint8), axis=-1).reshape(-1)
        out[in_range] = bytes_recovered
    return out.tobytes()


def _mandelbrot_calc_image_dims(payload_size: int, ext_len: int = 0) -> "Tuple[int, int]":
    """Square (W, H) sized so the bit-packed UCMSv3 envelope fits with
    the Mandelbrot fractal showing across the whole image.

    `payload_size` here is interpreted as the FULL ENVELOPE byte count
    (caller already added v3 overhead). The legacy `ext_len` parameter
    is ignored — kept for backward-compat call signatures.

    Pixel bytes needed = envelope_bytes × 8 (k=1: one bit per pixel byte).
    Pixels needed = pixel_bytes / 3 (3 channels per pixel).
    """
    envelope_bytes = payload_size
    pixel_bytes_needed = envelope_bytes * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    pixels_needed = (pixel_bytes_needed + 2) // 3
    for cap, dim in _IMAGE_TIERS:
        if pixels_needed <= dim * dim:
            return dim, dim
    side = math.ceil(math.sqrt(pixels_needed))
    side = max(_MIN_DIM, ((side + 1023) // 1024) * 1024)
    return side, side


def _mandelbrot_keystream(width: int, height: int,
                           content_seed: bytes = b"") -> bytes:
    """Generate a deterministic full-size colored Mandelbrot keystream
    of length `width * height * 3` bytes (RGB-interleaved row-major).

    Seed material:
      salt + width + height + content_seed

    `content_seed` is optional source-dependent bytes (e.g. SHA-256 of
    the envelope or a prefix of it). When supplied, two different sources
    of the same dimensions produce visually distinct fractals. This is
    purely cosmetic — the bit-pack decoder reads the bottom 4 bits of
    each pixel byte directly and never needs the keystream, so the seed
    can include arbitrary source-derived material without affecting
    decoder logic.
    """
    from . import _mandelbrot as _m
    seed_bytes = _MANDELBROT_SALT + struct.pack(">II", width, height) + content_seed
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
                           mandelbrot: bool = False,
                           password: bytes = b"") -> None:
    from .streaming_image import stream_png_write
    payload_size = src_path.stat().st_size
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_path.read_bytes(), src_ext, password=password, cancel=cancel)
        stream_png_write(dst, width, height, iter([pixel_bytes]), cancel, progress)
        return
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_png_write(dst, width, height, pixel_iter, cancel, progress)


def _bmp_embed_v2_to_file(src_path: Path, src_ext: str, dst: Path,
                           cancel: Optional["CancellationToken"] = None,
                           progress: Optional[Callable[[float], None]] = None,
                           mandelbrot: bool = False,
                           password: bytes = b"") -> None:
    from .streaming_image import stream_bmp_write
    payload_size = src_path.stat().st_size
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_path.read_bytes(), src_ext, password=password, cancel=cancel)
        stream_bmp_write(dst, width, height, iter([pixel_bytes]), cancel, progress)
        return
    width, height = _calc_image_dims(payload_size, len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_path(src_path, src_ext, width, height, cancel)
    stream_bmp_write(dst, width, height, pixel_iter, cancel, progress)


def _png_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path,
                              mandelbrot: bool = False,
                              password: bytes = b"") -> None:
    from .streaming_image import stream_png_write
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_bytes, src_ext, password=password)
        stream_png_write(dst, width, height, iter([pixel_bytes]))
        return
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height)
    stream_png_write(dst, width, height, pixel_iter)


def _bmp_embed_v2_from_bytes(src_bytes: bytes, src_ext: str, dst: Path,
                              mandelbrot: bool = False,
                              password: bytes = b"") -> None:
    from .streaming_image import stream_bmp_write
    if mandelbrot:
        width, height, pixel_bytes = _build_mandelbrot_image(
            src_bytes, src_ext, password=password)
        stream_bmp_write(dst, width, height, iter([pixel_bytes]))
        return
    width, height = _calc_image_dims(len(src_bytes), len(src_ext.encode("utf-8")))
    pixel_iter = _v2_pixel_iter_from_bytes(src_bytes, src_ext, width, height)
    stream_bmp_write(dst, width, height, pixel_iter)


def _build_inner_plaintext(payload: bytes, src_ext: str) -> bytes:
    """The encrypted-payload-side blob: ext_len | ext | payload_len | payload.
    Wrapped by AES-CTR in v3; no magic at this level (the v3 outer header
    holds the magic)."""
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")
    if len(ext_bytes) > 255:
        ext_bytes = ext_bytes[:255]
    return (bytes([len(ext_bytes)]) + ext_bytes
            + struct.pack(">Q", len(payload)) + payload)


def _v3_envelope(payload: bytes, src_ext: str, width: int, height: int,
                  password: bytes) -> bytes:
    """Build the full UCMSv3 envelope:
        MAGIC_V3 (8) | W (4) | H (4) | IV (16) | salt (4 reserved) | ciphertext

    The inner plaintext (ext+payload) is AES-256-CTR encrypted under a
    PBKDF2-derived key. Wrong password → garbage decryption with no oracle.
    Same source + same password → identical ciphertext (deterministic IV).
    """
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    iv, ciphertext = _sc.encrypt(inner, password)
    salt_field = b"\x00\x00\x00\x00"  # reserved for future per-file salting
    return (MAGIC_V3
            + struct.pack(">II", width, height)
            + iv + salt_field
            + ciphertext)


def _parse_v3_envelope(blob: bytes, password: bytes) -> "Tuple[bytes, str]":
    """Parse v3 envelope. Returns (payload, src_ext).

    Wrong password produces garbage values for ext_len/ext/payload_len.
    We CLAMP these to plausible ranges (don't error) so the no-oracle
    invariant holds — the function returns *something* either way and
    the user discovers correctness by whether the output file opens
    normally."""
    from . import _stone_crypto as _sc
    if len(blob) < len(MAGIC_V3) + 8 + 16 + 4:
        raise ValueError("v3 envelope: too short.")
    if not blob.startswith(MAGIC_V3):
        raise ValueError("v3 envelope: magic not found.")
    p = len(MAGIC_V3)
    width, height = struct.unpack(">II", blob[p:p + 8]); p += 8
    iv = blob[p:p + 16]; p += 16
    _salt = blob[p:p + 4]; p += 4   # reserved
    ciphertext = blob[p:]
    inner = _sc.decrypt(iv, ciphertext, password)
    if len(inner) < 1:
        # Truly empty ciphertext — give up rather than guess.
        return b"", ".bin"
    # Parse inner. Wrong password → these fields are random, but we
    # CLAMP to plausible ranges to avoid raising, preserving no-oracle.
    ext_len = inner[0]
    if ext_len > 64 or 1 + ext_len + 8 > len(inner):
        # Garbage. Treat the entire decrypted blob as raw payload with
        # an unknown extension. User will see a file that doesn't open.
        return inner[1:], ".bin"
    src_ext = inner[1:1 + ext_len].decode("utf-8", errors="replace")
    if not src_ext.startswith("."):
        src_ext = "." + src_ext if src_ext else ".bin"
    p = 1 + ext_len
    payload_len = struct.unpack(">Q", inner[p:p + 8])[0]
    p += 8
    payload = inner[p:p + payload_len]
    # If wrong password makes payload_len wildly wrong, just return what
    # we have. No error — user's output file just won't open as expected.
    if len(payload) != payload_len:
        return payload, src_ext
    return payload, src_ext


def _parse_v3_inner_clamped(inner: bytes) -> "Tuple[bytes, str]":
    """Common graceful-clamp parser for v3 inner plaintext (audio + 3D).

    Same no-oracle invariant as image: wrong password produces garbage
    bytes; we clamp implausible values rather than raising so the caller
    always gets *something* and never learns 'wrong password' from an
    exception."""
    if len(inner) < 1:
        return b"", ".bin"
    ext_len = inner[0]
    if ext_len > 64 or 1 + ext_len + 8 > len(inner):
        return inner[1:], ".bin"
    src_ext = inner[1:1 + ext_len].decode("utf-8", errors="replace")
    if not src_ext.startswith("."):
        src_ext = "." + src_ext if src_ext else ".bin"
    p = 1 + ext_len
    payload_len = struct.unpack(">Q", inner[p:p + 8])[0]
    p += 8
    payload = inner[p:p + payload_len]
    if len(payload) != payload_len:
        return payload, src_ext
    return payload, src_ext


def _v3_audio_envelope(payload: bytes, src_ext: str, password: bytes) -> bytes:
    """Build encrypted audio envelope:
        MAGIC_V3_AUDIO (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext

    Same crypto primitives as the image side (AES-256-CTR, deterministic
    SIV-style IV, PBKDF2-derived key). The clear-text length lets the
    decoder slice the exact ciphertext span out of the bit-packed audio
    stream without scanning past the meaningful content. No image
    dimensions — audio carries no width/height.
    """
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    iv, ciphertext = _sc.encrypt(inner, password)
    salt_field = b"\x00\x00\x00\x00"   # reserved for future per-file salting
    return (MAGIC_V3_AUDIO
            + struct.pack(">Q", len(ciphertext))
            + iv + salt_field
            + ciphertext)


V3_AUDIO_HEADER_SIZE = len(MAGIC_V3_AUDIO) + 8 + 16 + 4   # = 36


def _parse_v3_audio_envelope(blob: bytes, password: bytes) -> "Tuple[bytes, str]":
    """Parse encrypted audio envelope. Wrong password silently produces
    garbage (no oracle). Truncation is a real error (non-content-related)
    and may raise."""
    from . import _stone_crypto as _sc
    if len(blob) < V3_AUDIO_HEADER_SIZE:
        raise ValueError("v3 audio envelope: too short.")
    if not blob.startswith(MAGIC_V3_AUDIO):
        raise ValueError("v3 audio envelope: magic not found.")
    p = len(MAGIC_V3_AUDIO)
    ciphertext_len = struct.unpack(">Q", blob[p:p + 8])[0]; p += 8
    iv = blob[p:p + 16]; p += 16
    _salt = blob[p:p + 4]; p += 4
    ciphertext = blob[p:p + ciphertext_len]
    if len(ciphertext) != ciphertext_len:
        raise ValueError(
            f"v3 audio envelope: truncated (need {ciphertext_len}, got {len(ciphertext)}).")
    inner = _sc.decrypt(iv, ciphertext, password)
    return _parse_v3_inner_clamped(inner)


def _v3_3d_envelope(payload: bytes, src_ext: str, password: bytes) -> bytes:
    """Build encrypted 3D envelope:
        MAGIC_V3_3D (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext

    Used by PLY/OBJ/GLB cross-category embed. PLY/OBJ wrap this in base64
    inside comment lines; GLB stores it verbatim in the ucMs chunk."""
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    iv, ciphertext = _sc.encrypt(inner, password)
    salt_field = b"\x00\x00\x00\x00"
    return (MAGIC_V3_3D
            + struct.pack(">Q", len(ciphertext))
            + iv + salt_field
            + ciphertext)


V3_3D_HEADER_SIZE = len(MAGIC_V3_3D) + 8 + 16 + 4   # = 36


def _parse_v3_3d_envelope(blob: bytes, password: bytes) -> "Tuple[bytes, str]":
    """Parse encrypted 3D envelope. Wrong password → silent garbage."""
    from . import _stone_crypto as _sc
    if len(blob) < V3_3D_HEADER_SIZE:
        raise ValueError("v3 3D envelope: too short.")
    if not blob.startswith(MAGIC_V3_3D):
        raise ValueError("v3 3D envelope: magic not found.")
    p = len(MAGIC_V3_3D)
    ciphertext_len = struct.unpack(">Q", blob[p:p + 8])[0]; p += 8
    iv = blob[p:p + 16]; p += 16
    _salt = blob[p:p + 4]; p += 4
    ciphertext = blob[p:p + ciphertext_len]
    if len(ciphertext) != ciphertext_len:
        raise ValueError(
            f"v3 3D envelope: truncated (need {ciphertext_len}, got {len(ciphertext)}).")
    inner = _sc.decrypt(iv, ciphertext, password)
    return _parse_v3_inner_clamped(inner)


def _v3_video_envelope(payload: bytes, src_ext: str, password: bytes) -> bytes:
    """Build encrypted video envelope:
        MAGIC_V3_VIDEO (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext

    Used by MKV cross-category embed. The whole envelope is bit-packed
    (k=1) across the LSBs of an animated Mandelbrot frame sequence."""
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    iv, ciphertext = _sc.encrypt(inner, password)
    salt_field = b"\x00\x00\x00\x00"
    return (MAGIC_V3_VIDEO
            + struct.pack(">Q", len(ciphertext))
            + iv + salt_field
            + ciphertext)


V3_VIDEO_HEADER_SIZE = len(MAGIC_V3_VIDEO) + 8 + 16 + 4   # = 36


def _parse_v3_video_envelope(blob: bytes, password: bytes) -> "Tuple[bytes, str]":
    """Parse encrypted video envelope. Wrong password → silent garbage."""
    from . import _stone_crypto as _sc
    if len(blob) < V3_VIDEO_HEADER_SIZE:
        raise ValueError("v3 video envelope: too short.")
    if not blob.startswith(MAGIC_V3_VIDEO):
        raise ValueError("v3 video envelope: magic not found.")
    p = len(MAGIC_V3_VIDEO)
    ciphertext_len = struct.unpack(">Q", blob[p:p + 8])[0]; p += 8
    iv = blob[p:p + 16]; p += 16
    _salt = blob[p:p + 4]; p += 4
    ciphertext = blob[p:p + ciphertext_len]
    if len(ciphertext) != ciphertext_len:
        raise ValueError(
            f"v3 video envelope: truncated (need {ciphertext_len}, got {len(ciphertext)}).")
    inner = _sc.decrypt(iv, ciphertext, password)
    return _parse_v3_inner_clamped(inner)


def _build_mandelbrot_image(src_bytes: bytes, src_ext: str,
                             password: bytes = b"",
                             cancel: Optional["CancellationToken"] = None
                             ) -> "Tuple[int, int, bytes]":
    """Build the bit-packed Mandelbrot image. Returns (width, height,
    pixel_bytes) ready to stream into stream_png_write / stream_bmp_write.

    The envelope is UCMSv3 — encrypted with a key derived from `password`
    (PBKDF2 + AES-256-CTR). Empty password ⇒ default app key (anyone with
    Transmute can decode). Non-empty password ⇒ only the same password
    decodes the file.

    Each pixel byte's top 7 bits = colored fractal; bottom 1 bit = one
    payload bit (or zero past the envelope, leaving the fractal pure).
    """
    # Pre-build the inner plaintext to know the encrypted envelope size
    # exactly (CTR mode preserves length; ciphertext is same size as
    # inner plaintext + the v3 header overhead).
    inner = _build_inner_plaintext(src_bytes, src_ext)
    ENVELOPE_OVERHEAD = len(MAGIC_V3) + 8 + 16 + 4   # = 36 bytes
    envelope_size = ENVELOPE_OVERHEAD + len(inner)
    width, height = _mandelbrot_calc_image_dims(envelope_size, 0)

    # Encrypt and assemble envelope
    envelope = _v3_envelope(src_bytes, src_ext, width, height, password)
    if cancel is not None:
        cancel.check()

    # Source+password-dependent fractal seed. The encrypted envelope
    # already incorporates the password; SHA-256 of its first 64 KB
    # gives a unique seed per (source, password) pair. Decoder doesn't
    # need this seed (it just reads the bottom bits of pixels).
    content_seed = hashlib.sha256(envelope[:64 * 1024]).digest()
    fractal = _mandelbrot_keystream(width, height, content_seed=content_seed)
    if cancel is not None:
        cancel.check()
    total_pixel_bytes = width * height * 3
    if len(envelope) * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE > total_pixel_bytes:
        raise RuntimeError(
            f"Mandelbrot dim calc bug: envelope={len(envelope)} bytes needs "
            f"{len(envelope) * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE} "
            f"pixel bytes but image holds {total_pixel_bytes}.")
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
                             cancel: Optional["CancellationToken"] = None,
                             password: bytes = b"") -> str:
    from .streaming_image import stream_png_read
    return _extract_v2_dual_attempt(src, dst_path, cancel, stream_png_read, password)


def _bmp_extract_v2_to_file(src: Path, dst_path: Path,
                             cancel: Optional["CancellationToken"] = None,
                             password: bytes = b"") -> str:
    from .streaming_image import stream_bmp_read
    return _extract_v2_dual_attempt(src, dst_path, cancel, stream_bmp_read, password)


def _extract_v2_dual_attempt(src: Path, dst_path: Path,
                              cancel: Optional["CancellationToken"],
                              stream_reader,
                              password: bytes = b"") -> str:
    """Try three extraction paths in order:
      1. Plain UCMSv2 byte-passthrough (raw envelope in pixel bytes).
      2. Bit-packed UCMSv3 envelope (k=1, encrypted Mandelbrot Stone).
      3. Bit-packed UCMSv2 envelope (k=4, legacy Mandelbrot Stone) for
         backward-compat reading of older Stone files.

    Buffers the entire pixel byte stream into memory once. For 4096^2 RGB
    that's 48 MB; for huge cross-category Stone images at k=1, can be 200+
    MB. Acceptable for the typical use case.
    """
    width, height, it = stream_reader(src, cancel)
    pixel_bytes = bytearray()
    for chunk in it:
        if cancel is not None:
            cancel.check()
        pixel_bytes.extend(chunk)

    # Attempt 1: plain UCMSv2 raw byte stream with magic at offset 0.
    if len(pixel_bytes) >= 8 and bytes(pixel_bytes[:8]) == MAGIC_V2:
        return _extract_v2_from_pixel_iter(iter([bytes(pixel_bytes)]), dst_path, cancel)

    # Attempt 2: bit-packed UCMSv3 envelope (k=1, encrypted).
    total_bytes = len(pixel_bytes)
    max_env_v3 = total_bytes // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    if max_env_v3 >= len(MAGIC_V3) + 8 + 16 + 4:
        envelope = _mandelbrot_unpack_envelope_from_pixels(
            bytes(pixel_bytes), max_env_v3, total_pixel_bytes=total_bytes)
        if envelope[:len(MAGIC_V3)] == MAGIC_V3:
            payload, src_ext = _parse_v3_envelope(bytes(envelope), password)
            with open(dst_path, "wb") as out:
                out.write(payload)
            return src_ext

    # Attempt 3: legacy bit-packed UCMSv2 envelope (k=4, pre-encryption).
    legacy_pixel_bytes_per_byte = 2  # k=4 used 2 pixel bytes per envelope byte
    max_env_v2 = total_bytes // legacy_pixel_bytes_per_byte
    if max_env_v2 >= 8:
        envelope = _mandelbrot_unpack_envelope_from_pixels_v2_legacy(
            bytes(pixel_bytes), max_env_v2, total_pixel_bytes=total_bytes)
        if envelope[:8] == MAGIC_V2:
            return _extract_v2_from_pixel_iter(iter([envelope]), dst_path, cancel)

    raise ValueError("Stone envelope: no v2-plain, v3-bit-packed, or "
                     "v2-bit-packed (legacy) magic found in pixel data.")


def _mandelbrot_unpack_envelope_from_pixels_v2_legacy(
        pixel_bytes: bytes, max_envelope_bytes: int,
        total_pixel_bytes: int = -1) -> bytes:
    """Backward-compat: unpack the OLD k=4 bit-pack format used before v3.
    Each envelope byte = 4 bits (low) at pixel byte 2i + 4 bits (high) at
    pixel byte 2i+1, scattered via the same golden-ratio stride logic."""
    import numpy as np
    if total_pixel_bytes < 0:
        total_pixel_bytes = len(pixel_bytes)
    num_pairs_full = total_pixel_bytes // 2
    available_pairs = len(pixel_bytes) // 2
    if num_pairs_full == 0:
        return b""
    n_env = min(max_envelope_bytes, num_pairs_full)
    if n_env == 0:
        return b""
    stride, _ = _mandelbrot_scatter_indices(num_pairs_full)
    px = np.frombuffer(pixel_bytes, dtype=np.uint8)
    idx = (np.arange(n_env, dtype=np.int64) * stride) % num_pairs_full
    in_range = idx < available_pairs
    out = np.zeros(n_env, dtype=np.uint8)
    valid_idx = idx[in_range]
    low = px[2 * valid_idx] & 0x0F
    high = px[2 * valid_idx + 1] & 0x0F
    out[in_range] = (high << 4) | low
    return out.tobytes()


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


def _ply_embed(src_bytes: bytes, src_ext: str,
                cross_category: bool = False,
                password: bytes = b"") -> bytes:
    """Build a PLY host. Same-type (model→model) keeps the plaintext
    UCMSv1 envelope. Cross-type (e.g. .png→.ply) wraps the v3 encrypted
    envelope so the source extension is hidden in the comment bytes."""
    if cross_category:
        env = _v3_3d_envelope(src_bytes, src_ext, password)
    else:
        env = _build_envelope(src_bytes, src_ext)
    body = base64.b64encode(env).decode("ascii")
    chunks = [body[i:i + 72] for i in range(0, len(body), 72)]
    lines = [f"comment {_PLY_COMMENT_TAG} {c}\n" for c in chunks]
    return (_PLY_HEADER + "".join(lines) + _PLY_FOOTER).encode("utf-8")


def _ply_extract(host: bytes, password: bytes = b"") -> Tuple[bytes, str]:
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
    if env.startswith(MAGIC_V3_3D):
        return _parse_v3_3d_envelope(env, password)
    return _parse_envelope(env)


# ---------------------------------------------------------------------------
# Host: OBJ (Wavefront) — envelope rides in `#` comment lines
# ---------------------------------------------------------------------------
# OBJ readers ignore any line beginning with `#`. Same scheme as PLY: tagged
# comments carrying base64 chunks, then a single vertex so the file is
# structurally valid as a (degenerate) mesh.

_OBJ_COMMENT_TAG = "uc"


def _obj_embed(src_bytes: bytes, src_ext: str,
                cross_category: bool = False,
                password: bytes = b"") -> bytes:
    """Build an OBJ host. See _ply_embed for the same-type/cross-type rule."""
    if cross_category:
        env = _v3_3d_envelope(src_bytes, src_ext, password)
    else:
        env = _build_envelope(src_bytes, src_ext)
    body = base64.b64encode(env).decode("ascii")
    chunks = [body[i:i + 72] for i in range(0, len(body), 72)]
    lines = [f"# {_OBJ_COMMENT_TAG} {c}\n" for c in chunks]
    return ("".join(lines) + "v 0 0 0\n").encode("utf-8")


def _obj_extract(host: bytes, password: bytes = b"") -> Tuple[bytes, str]:
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
    if env.startswith(MAGIC_V3_3D):
        return _parse_v3_3d_envelope(env, password)
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


def _glb_embed(src_bytes: bytes, src_ext: str,
                cross_category: bool = False,
                password: bytes = b"") -> bytes:
    """Build a GLB host. See _ply_embed for the same-type/cross-type rule."""
    if cross_category:
        env = _v3_3d_envelope(src_bytes, src_ext, password)
    else:
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


def _glb_extract(host: bytes, password: bytes = b"") -> Tuple[bytes, str]:
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
            env = data.rstrip(b"\x00")
            if env.startswith(MAGIC_V3_3D):
                return _parse_v3_3d_envelope(env, password)
            return _parse_envelope(env)
    raise ValueError("GLB host: no Stone envelope chunk (ucMs) found.")


# ---------------------------------------------------------------------------
# Host: ZIP (transparent archive — single STORED member named original{ext})
# ---------------------------------------------------------------------------
# The output is a real, valid ZIP file. Opening it with Windows Explorer or
# any zip tool extracts a single member that IS the original source file,
# byte-for-byte. Round-trip via Transmute also works (zip → png recovers the
# PNG). Always plaintext — encryption would corrupt the archive structure
# and defeat the "real zip" property, so the password parameter is
# intentionally not threaded into this path.
#
# Decoder rule for round-trip: only "Stone-built" zips (exactly one member
# whose name starts with `original.`) are auto-extracted. Any other zip is
# treated as opaque bytes by `_zip_extract` (raises ValueError, which
# `_try_extract` catches and translates to None). That lets the user wrap
# a regular multi-file zip *inside* a Transmute zip without having the
# inner zip silently unpacked.

_ZIP_MEMBER_PREFIX = "original"


def _zip_embed(src_bytes: bytes, src_ext: str) -> bytes:
    """Build a real STORED-method zip with one member named `original{ext}`."""
    import io
    import zipfile as _zf
    if not src_ext.startswith("."):
        src_ext = "." + src_ext if src_ext else ""
    member_name = _ZIP_MEMBER_PREFIX + src_ext
    buf = io.BytesIO()
    with _zf.ZipFile(buf, mode="w", compression=_zf.ZIP_STORED) as z:
        z.writestr(member_name, src_bytes)
    return buf.getvalue()


def _zip_extract(host: bytes) -> Tuple[bytes, str]:
    """If `host` is a Stone-built zip (exactly one member named original.*),
    return that member's bytes + extension. Any other zip raises ValueError
    so `_try_extract` falls through to opaque-bytes wrapping."""
    import io
    import zipfile as _zf
    try:
        z = _zf.ZipFile(io.BytesIO(host))
    except _zf.BadZipFile as e:
        raise ValueError(f"ZIP host: not a valid zip ({e}).")
    try:
        names = z.namelist()
        if len(names) != 1:
            raise ValueError(
                f"ZIP host: expected one member, got {len(names)}; "
                "treating as opaque bytes.")
        name = names[0]
        if not name.startswith(_ZIP_MEMBER_PREFIX + "."):
            raise ValueError(
                f"ZIP host: member name {name!r} doesn't match "
                "Stone-built `original.*` pattern.")
        body = z.read(name)
        # Recover ext from the member name (strip the "original" prefix).
        ext = name[len(_ZIP_MEMBER_PREFIX):]
        if not ext.startswith("."):
            ext = "." + ext if ext else ".bin"
        return body, ext
    finally:
        z.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EMBED = {
    ".wav": _wav_embed, ".png": _png_embed, ".bmp": _bmp_embed,
    ".txt": _txt_embed,
    ".ply": _ply_embed, ".obj": _obj_embed, ".glb": _glb_embed,
    ".aiff": _aiff_embed,
    ".zip": _zip_embed,
    # .flac is dispatched specially below (needs Path target for FFmpeg).
}
_EXTRACT = {
    ".wav": _wav_extract, ".png": _png_extract, ".bmp": _bmp_extract,
    ".txt": _txt_extract,
    ".ply": _ply_extract, ".obj": _obj_extract, ".glb": _glb_extract,
    ".aiff": _aiff_extract,
    ".flac": _flac_extract_from_bytes,
    ".zip": _zip_extract,
}


def can_embed_into(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EMBED or ext in (".mkv", ".py", ".flac")


def can_extract_from(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EXTRACT or ext in (".mkv", ".py")


def _try_extract(src: Path, src_ext: str,
                  password: bytes = b"") -> Tuple[bytes, str] | None:
    """Returns (payload, recovered_ext) if src is a Masquerade host with a
    valid envelope; None if no envelope or unsupported source ext.

    `password` is forwarded to all extractors that use encrypted v3
    envelopes (image PNG/BMP, audio WAV/AIFF/FLAC, 3D PLY/OBJ/GLB).
    TXT, MKV, PY hosts currently use the unencrypted v1/v2 envelope and
    ignore the password.
    """
    src_ext = src_ext.lower()
    try:
        if src_ext == ".mkv":
            return _mkv_extract_from_file(src, password=password)
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
            # Try v2/v3 first (envelope in pixel data), fall back to v1 (private chunk)
            try:
                import tempfile
                tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
                try:
                    ext = _png_extract_v2_to_file(src, tmp, password=password)
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
                    ext = _bmp_extract_v2_to_file(src, tmp, password=password)
                    return tmp.read_bytes(), ext
                finally:
                    try: tmp.unlink()
                    except OSError: pass
            except (ValueError, RuntimeError):
                return _bmp_extract(src.read_bytes())
        # Audio + 3D extractors take a password for v3 decryption.
        if src_ext in (".wav", ".aiff"):
            return _EXTRACT[src_ext](src.read_bytes(), password=password)
        if src_ext == ".flac":
            return _flac_extract_from_bytes(src.read_bytes(), password=password)
        if src_ext in (".ply", ".obj", ".glb"):
            return _EXTRACT[src_ext](src.read_bytes(), password=password)
        if src_ext in _EXTRACT:
            return _EXTRACT[src_ext](src.read_bytes())
    except ValueError:
        return None
    return None


def _embed_to(dst: Path, payload: bytes, src_ext: str, dst_ext: str,
               cancel: Optional["CancellationToken"] = None,
               cross_category: bool = False,
               password: bytes = b"") -> None:
    """Whole-bytes-in-memory embed. Used for small files.

    `cross_category` triggers the aesthetic encoder: Mandelbrot Stone for
    PNG/BMP image targets, music encoder for WAV/AIFF/FLAC audio targets.
    `password` is forwarded to image targets for the v3 envelope encryption.
    Audio music encoder doesn't yet use the password (out of scope this round).
    """
    dst_ext = dst_ext.lower()
    if dst_ext == ".mkv":
        _mkv_embed_to_file(payload, src_ext, dst,
                           cross_category=cross_category, password=password)
        return
    if dst_ext == ".png":
        _png_embed_v2_from_bytes(payload, src_ext, dst,
                                  mandelbrot=cross_category, password=password)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_from_bytes(payload, src_ext, dst,
                                  mandelbrot=cross_category, password=password)
        return
    if dst_ext == ".wav" and cross_category:
        # Cross-category audio target: encrypted v3 envelope bit-packed
        # into music samples. Same-category WAV (audio→audio) falls
        # through to the plaintext _wav_embed via _EMBED dispatch.
        dst.write_bytes(_wav_embed_music(payload, src_ext, password=password))
        return
    if dst_ext == ".aiff" and cross_category:
        dst.write_bytes(_aiff_embed_music(payload, src_ext, password=password))
        return
    if dst_ext == ".flac":
        # FLAC always goes through FFmpeg. Cross-category uses the music
        # encoder (encrypted v3); same-category uses the classic 8 kHz
        # mono plaintext envelope WAV. Both round-trip losslessly.
        if cross_category:
            _flac_embed_music(payload, src_ext, dst, password=password)
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
    if dst_ext in (".ply", ".obj", ".glb"):
        # 3D hosts: cross-type wraps an encrypted v3 envelope (hides source
        # ext in the comment/chunk bytes); same-type stays plaintext UCMSv1.
        dst.write_bytes(_EMBED[dst_ext](payload, src_ext,
                                         cross_category=cross_category,
                                         password=password))
        return
    if dst_ext not in _EMBED:
        raise RuntimeError(f"Masquerade target {dst_ext} is not supported.")
    dst.write_bytes(_EMBED[dst_ext](payload, src_ext))


def _embed_streamed_to(dst: Path, src_path: Path, src_ext: str, dst_ext: str,
                        cancel: Optional["CancellationToken"] = None,
                        progress: Optional[Callable[[float], None]] = None,
                        cross_category: bool = False,
                        password: bytes = b"") -> None:
    """Path-based streaming embed. Used for files above the streaming threshold."""
    dst_ext = dst_ext.lower()
    if dst_ext == ".png":
        _png_embed_v2_to_file(src_path, src_ext, dst, cancel, progress,
                              mandelbrot=cross_category, password=password)
        return
    if dst_ext == ".bmp":
        _bmp_embed_v2_to_file(src_path, src_ext, dst, cancel, progress,
                              mandelbrot=cross_category, password=password)
        return
    if dst_ext == ".py":
        _py_embed_to_file(src_path, src_ext, dst, cancel)
        return
    # WAV / TXT / MKV: whole-file bytes API still used; fall through.
    src_bytes = src_path.read_bytes()
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel,
              cross_category=cross_category, password=password)


def convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
            cancel: CancellationToken, progress: Callable[[float], None],
            *, cross_category: bool = False,
            password: bytes = b"") -> None:
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

    extracted = (_try_extract(src, src_ext, password=password)
                 if can_extract_from(src_ext) else None)
    if extracted is not None:
        payload, recovered_ext = extracted
        cancel.check()
        progress(0.55)
        # The output file ALWAYS lands at the user-chosen dst path — never
        # renamed to recovered_ext. With v3 encryption, the recovered_ext
        # from a wrong-password decrypt is garbage that would otherwise
        # leak the password-correctness via filename extension. Honoring
        # dst_ext keeps the no-oracle invariant: wrong password produces
        # a file at the user's chosen extension whose contents simply
        # don't open in the target app.
        if dst_ext == recovered_ext or not can_embed_into(dst_ext):
            dst.write_bytes(payload)
            progress(1.0)
            return
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel,
                  cross_category=cross_category, password=password)
        progress(1.0)
        return

    # Decide streamed vs whole-file path based on source size.
    src_size = src.stat().st_size
    threshold = streaming_threshold(dst_ext)
    if dst_ext == ".py" or (src_size >= threshold and dst_ext in (".png", ".bmp")):
        cancel.check()
        progress(0.1)
        _embed_streamed_to(dst, src, src_ext, dst_ext, cancel, progress,
                           cross_category=cross_category, password=password)
        progress(1.0)
        return

    src_bytes = src.read_bytes()
    cancel.check()
    progress(0.3)
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel,
              cross_category=cross_category, password=password)
    progress(1.0)
