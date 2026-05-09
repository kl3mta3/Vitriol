"""Masquerade Mode — embed any file's bytes inside a host container that
is itself a valid file in the host format.

Envelope format (v1):
    magic       8 B    b"UCMSv1\\0"
    ext_len     1 B    length of original extension (incl. leading dot)
    ext_str     var    utf-8 of original ext, e.g. ".docx"
    payload_len 8 B    uint64 BE
    payload     var    original file bytes
    pad         var    zero bytes for host structural validity

Round-trip: embed(extract(host)) == host for hosts produced by this engine.

v1 hosts:
    .wav   RIFF/WAVE PCM, envelope in the data chunk.
    .png   Private ancillary chunk "ucMs"; image is 1x1.
    .bmp   Payload appended after a 1x1 pixel block.
    .txt   Base64 of the envelope.
    .mkv   Matroska rawvideo rgb24 1024x1024 @ 42 fps; envelope in frame
           pixels with MKV tags as hints. Requires FFmpeg.
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
from typing import Callable, Iterator, Optional, Tuple

from ..utils.cancellation import CancellationToken
from ..utils.paths import bin_dir
from ..core.config import CHUNK_SIZE, streaming_threshold

MAGIC = b"UCMSv1\0"
MAGIC_V2 = b"UCMSv2\0\0"  # 8 bytes — tiered-image-dimensions envelope (PNG/BMP)
MAGIC_V3 = b"UCMSv3\0\0"  # 8 bytes — encrypted Mandelbrot envelope (PNG/BMP)
MAGIC_V3_AUDIO = b"uM03\0\0\0\0"  # 8 bytes — encrypted music envelope (WAV/AIFF/FLAC)
MAGIC_V3_3D = b"UC3Dv3\0\0"  # 8 bytes — encrypted 3D envelope (PLY/OBJ/GLB)
MAGIC_V3_VIDEO = b"UCMv3\0\0\0"  # 8 bytes — encrypted animated-Mandelbrot envelope (MKV/MP4)


class TamperDetectedError(Exception):
    """Raised by video extract paths when the SHA-256 hash hidden in the
    muxed audio track does not match the recomputed hash of the recovered
    payload. Indicates one of:

      - The file was modified after Stone-encoding (real tamper).
      - The user supplied the wrong password (decryption produced garbage
        payload + garbage hash; comparison fails the same way).

    These two cases produce the IDENTICAL error to preserve the no-oracle
    invariant — the user-facing message is intentionally vague about which
    case applies. Internal callers should not catch and re-raise with a
    more specific message; doing so would leak password-correctness signal."""
    DEFAULT_MESSAGE = "Video file appears corrupted or modified."

    def __init__(self, message: str = DEFAULT_MESSAGE):
        super().__init__(message)

# Read-write capable host extensions. Used by the registry + dropdown filter
# when Philosopher's Stone (a.k.a. Masquerade) mode is on.
TARGETS = {".wav", ".png", ".bmp", ".txt", ".mkv", ".py", ".exe",
           ".ply", ".obj", ".glb",
           ".aiff", ".flac", ".m4a",
           ".zip"}
# .mp4 was prototyped this round but deferred — see the MP4 functions
# below. Lossless H.264 RGB produces unacceptably large files (~370 MB
# for a 5 KB source) because libx264rgb's intra-only output doesn't
# compress complex content well. HEVC/x265 lossless would help (smaller
# files) but requires the Win10/11 HEVC codec extension for WMP playback,
# which is paid in the Microsoft Store. The functions are kept for a
# follow-up round that explores alternate codecs / tile-based encoding.
# .fbx is intentionally excluded as a Stone host (autodesk-proprietary
# binary; readers are notoriously strict, no clean place to drop a payload).
# .flac is a Stone host but only via the music encoder (cross-category) —
# the WAV music output is re-encoded to FLAC via FFmpeg, and the inverse
# decode path uses FFmpeg → WAV → music extract.

# Lossy source extensions excluded from Stone mode entirely. The bytes of a
# JPG/MP3 file *can* technically be embedded into a Stone host and recovered
# byte-exact, but the original media data inside them is already a lossy
# compression — treating them as "preserved" is conceptually wrong.
# More importantly, since Stone targets only contain lossless containers,
# the dropdown asymmetry (jpg→txt allowed but txt→jpg not) confuses users.
# Exclude lossy formats from being Stone sources to keep the model symmetric:
# only lossless data goes through the Stone.
#
# `.m4a` is NOT in this set: Stone-mode produces it with the lossless
# ALAC codec, so a Stone-output .m4a is a valid Stone source for
# round-trip. User-supplied non-Stone .m4a files still fall through to
# the regular media pipeline because `has_envelope()` returns False on them.
# .mp4 IS still in this set this round — see the deferred-MP4 note above.
LOSSY_EXTS = {
    # Images
    ".jpg", ".jpeg", ".webp", ".heic",
    # Audio
    ".mp3", ".ogg", ".opus", ".aac", ".wma", ".ac3", ".amr",
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
    # .py is a Stone host only when the file matches the Vitriol header.
    if ext == ".py":
        try:
            return _py_is_stone(Path(path))
        except Exception:
            return False
    # .exe is a Stone host only when it carries the appended payload magic.
    if ext == ".exe":
        try:
            return _exe_is_stone(Path(path))
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
            return _video_v3_envelope_probe(Path(path))
        except Exception:
            return False
    if ext == ".mp4":
        # MP4 has no legacy UCMSv1 path — only v3 (cross-category).
        try:
            return _video_v3_envelope_probe(Path(path))
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
    # M4A/ALAC host: same approach as FLAC — decode to WAV via FFmpeg
    # and recurse. M4A magic is the `ftyp` box; we trust the extension
    # to gate this path (same as FLAC's fLaC magic check is mostly a
    # sanity rail).
    if ext == ".m4a":
        try:
            import tempfile
            tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
            try:
                _alac_to_wav_via_ffmpeg(Path(path), tmp_wav)
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
    # TXT host: envelope is base64-encoded, no comment header. Detect by
    # base64-decoding the head and looking for MAGIC.
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
#
# Render at HIGHER resolution for math accuracy — the fractal iteration
# loop benefits from more boundary samples, especially at deep zoom
# (10× zoom-in across the clip). Then bilinear-upscale to MKV_FRAME_W
# and apply a Gaussian low-pass filter BEFORE bit-packing. The blur
# strips high-frequency content from the fractal carrier so FFV1
# compresses it well (back to the ~46 MB ballpark of plain 384px),
# while the underlying iteration math was done at higher precision so
# deep-zoom frames still resolve real detail.
#
# The blur ONLY touches the top 7 bits of each pixel (the fractal
# carrier). Bit-pack then sets the bottom 1 bit at scatter positions,
# so the LSB stream is unaffected — round-trip stays byte-perfect.
MKV_FRACTAL_RENDER_DIM_DEFAULT = 512
MKV_FRACTAL_RENDER_DIM_HIGH = 512

# Gaussian blur radius applied to the upscaled-to-MKV_FRAME_W fractal
# right before bit-pack. Tuned to flatten enough high-frequency content
# to keep FFV1 output compact, without making the visual look "out of
# focus." 1.5 is roughly the spatial frequency of the bilinear-upscale
# blur from 384px, so the blurred-768 carrier matches the smoothness
# profile of plain-384 — same compression friendliness, sharper math.
MKV_FRACTAL_LOWPASS_SIGMA = 1.5


def _mkv_pick_render_dim(n_frames: int) -> int:
    """Currently a constant — same render dim for all clips. Kept as
    an adaptive hook for a future round."""
    return MKV_FRACTAL_RENDER_DIM_DEFAULT


def _sha256_bytes(b: bytes) -> bytes:
    """SHA-256 digest (32 bytes raw) of an in-memory byte string. Used by
    the video-output path to bind a tamper-detection hash into the muxed
    audio track."""
    return hashlib.sha256(b).digest()


def _sha256_file(path: Path, chunk: int = CHUNK_SIZE) -> bytes:
    """SHA-256 digest of a file streamed from disk in `chunk`-sized
    pieces. Used by the streamed-embed path so we never need to load
    a multi-GB source into memory just to hash it."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.digest()


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


def _audio_pad_to_inner_for_floor(payload: bytes, src_ext: str) -> "Optional[int]":
    """Compute the `pad_to_inner` value needed to bring the v3 audio
    envelope up to `MUSIC_MIN_FRAMES` bytes (10 seconds at 44.1 kHz).
    Returns None when the natural envelope is already past the floor.

    Padding the inner (rather than just letting the music synth produce
    extra-but-empty frames) ensures EVERY audio frame's LSBs hold
    real ciphertext bytes. Without padding, only the first N frames
    (where N = natural envelope size) hold ciphertext and the rest hold
    the music synth's natural LSBs — a forensic detector running LSB
    statistics over time would see a clear boundary between the two
    regions. With padding, the LSB stream is uniform-random end-to-end."""
    from . import _music as _m
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")[:255]
    natural_inner = 1 + len(ext_bytes) + 8 + len(payload)
    target_inner = _m.MUSIC_MIN_FRAMES - V3_AUDIO_HEADER_SIZE
    return target_inner if natural_inner < target_inner else None


def _wav_embed_music(src_bytes: bytes, src_ext: str,
                      password: bytes = b"") -> bytes:
    """Cross-category Stone audio target. Builds an encrypted v3 audio
    envelope (AES-256-CTR + PBKDF2 under `password`) and bit-packs it into
    music samples (low 4 bits/channel). Empty password → deterministic
    default key shared by all Vitriol installs of the same version.

    Tiny payloads are padded to a 10-second floor via `pad_to_inner` so
    the output WAV is always at least 10 seconds long (closes the
    "audio is suspiciously short" forensic tell)."""
    from . import _music as _m
    pad = _audio_pad_to_inner_for_floor(src_bytes, src_ext)
    envelope = _v3_audio_envelope(src_bytes, src_ext, password, pad_to_inner=pad)
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
    big-endian PCM samples. Mirrors _wav_embed_music with BE encoding,
    including the 10-second minimum-duration floor."""
    from . import _music as _m
    pad = _audio_pad_to_inner_for_floor(src_bytes, src_ext)
    envelope = _v3_audio_envelope(src_bytes, src_ext, password, pad_to_inner=pad)
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


# ---------------------------------------------------------------------------
# Host: ALAC in M4A (Apple Lossless in MP4 container)
# ---------------------------------------------------------------------------
# ALAC is the WMP-friendly lossless audio counterpart to FLAC. M4A is the
# audio-only flavor of the MP4 container; the same ALAC stream also gets
# muxed into Stone-mode .mp4 video outputs as the audio track.
# Round-trip parity with FLAC: WAV → ALAC → WAV via FFmpeg is bit-exact at
# 16-bit stereo 44.1 kHz (verified by the round-trip test).

def _alac_via_ffmpeg(wav_path: Path, m4a_path: Path) -> None:
    """Re-encode WAV → ALAC in M4A losslessly via FFmpeg. Uses `-c:a alac`,
    which libavcodec implements as a true lossless encoder."""
    ff = _ffmpeg_path()
    rc = subprocess.call(
        [str(ff), "-y", "-loglevel", "error",
         "-i", str(wav_path), "-c:a", "alac",
         str(m4a_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc != 0 or not m4a_path.exists():
        raise RuntimeError(f"FFmpeg WAV->ALAC failed (exit {rc})")


def _alac_to_wav_via_ffmpeg(m4a_path: Path, wav_path: Path) -> None:
    """Decode M4A/ALAC → WAV losslessly via FFmpeg."""
    ff = _ffmpeg_path()
    rc = subprocess.call(
        [str(ff), "-y", "-loglevel", "error",
         "-i", str(m4a_path), "-c:a", "pcm_s16le",
         "-ar", "44100", "-ac", "2",
         str(wav_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if rc != 0 or not wav_path.exists():
        raise RuntimeError(f"FFmpeg ALAC->WAV failed (exit {rc})")


def _alac_embed_music(src_bytes: bytes, src_ext: str, dst: Path,
                       password: bytes = b"") -> None:
    """Cross-category ALAC/M4A: write encrypted v3 music WAV to temp,
    re-encode to ALAC via FFmpeg. ALAC is bit-exact, so payload bits
    survive losslessly."""
    import tempfile
    wav_bytes = _wav_embed_music(src_bytes, src_ext, password=password)
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        tmp_wav.write_bytes(wav_bytes)
        _alac_via_ffmpeg(tmp_wav, dst)
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _alac_extract(src: Path, password: bytes = b"") -> Tuple[bytes, str]:
    """Decode the M4A/ALAC to WAV via FFmpeg, then route to _wav_extract."""
    import tempfile
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        _alac_to_wav_via_ffmpeg(src, tmp_wav)
        return _wav_extract(tmp_wav.read_bytes(), password=password)
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _alac_extract_from_bytes(host: bytes,
                              password: bytes = b"") -> Tuple[bytes, str]:
    """Bytes-API wrapper for _EXTRACT dispatch."""
    import tempfile
    tmp_m4a = Path(tempfile.mkstemp(suffix=".m4a")[1])
    try:
        tmp_m4a.write_bytes(host)
        return _alac_extract(tmp_m4a, password=password)
    finally:
        try: tmp_m4a.unlink()
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


def _video_v3_envelope_probe(src: Path) -> bool:
    """Cheap detection: FFmpeg-decode just the first frame, bit-unpack the
    first 8 bytes via the same scatter pattern the encoder uses, and check
    for `MAGIC_V3_VIDEO`. Used by `has_envelope` to identify v3 video Stone
    files (.mkv / .mp4) that don't carry a legacy `UCMSv1` title tag.

    Codec-agnostic: FFmpeg auto-detects the input container/codec, so this
    works for both Matroska/FFV1 and MP4/H.264-lossless inputs without
    branching on extension."""
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


# ---------------------------------------------------------------------------
# Audio track for video outputs
# ---------------------------------------------------------------------------
# The muxed audio track is a v3 audio Stone-pack carrying SHA-256 of the
# source bytes; decoder recomputes and compares on extract.
_VIDEO_AUDIO_PLACEHOLDER_EXT = ".bin"


def _video_audio_envelope_target_size(n_video_frames: int) -> int:
    """Target byte length of the v3 audio envelope for the muxed audio
    track of a video with `n_video_frames` frames at MKV_FPS. Matches
    audio duration exactly to video duration:

        audio_seconds = n_video_frames / MKV_FPS
        audio_frames  = audio_seconds * SAMPLE_RATE   = n_video_frames * 1470
        envelope_size = audio_frames                  (1 byte per stereo frame)
    """
    from . import _music as _m
    # 44100 / 30 = 1470 — exact integer ratio at default MKV_FPS, so no
    # rounding drift between video and audio durations.
    audio_frames = (n_video_frames * _m.SAMPLE_RATE) // MKV_FPS
    return audio_frames


def _build_video_audio_pcm(source_hash: bytes, password: bytes,
                             n_video_frames: int) -> bytes:
    """Build the PCM bytes (LE int16 stereo) for a video's muxed audio
    track. The track's v3-audio envelope inner plaintext is:

        ext_len(1) | ".bin" (4) | payload_len(8) | source_hash (32) | random padding

    The decoder reads `payload_len = 32` from the inner and pulls the
    first 32 bytes as the hash. The padding is deterministically random
    (seeded from inner+password by `_v3_audio_envelope`'s `pad_to_inner`)
    so the audio LSB stream is uniform-random end-to-end and looks
    statistically identical to a regular Stone-audio file.

    Returns raw PCM bytes ready to feed into a WAV header by the caller."""
    from . import _music as _m
    if len(source_hash) != 32:
        raise ValueError(f"source_hash must be 32 bytes, got {len(source_hash)}.")
    target_envelope_size = _video_audio_envelope_target_size(n_video_frames)
    target_inner_size = target_envelope_size - V3_AUDIO_HEADER_SIZE
    envelope = _v3_audio_envelope(
        source_hash, _VIDEO_AUDIO_PLACEHOLDER_EXT, password,
        pad_to_inner=target_inner_size)
    pcm, n_audio_frames = _m.encode_music_envelope(envelope)
    return pcm


def _build_video_audio_wav(source_hash: bytes, password: bytes,
                             n_video_frames: int) -> bytes:
    """Convenience wrapper: build the audio track PCM and wrap it in a
    valid RIFF/WAVE header. Used by the embed path which writes this as
    a temp file before letting FFmpeg re-encode to FLAC (MKV) or ALAC (MP4)."""
    from . import _music as _m
    pcm = _build_video_audio_pcm(source_hash, password, n_video_frames)
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


def _extract_hash_from_video_audio_pcm(pcm: bytes, password: bytes) -> bytes:
    """Inverse of `_build_video_audio_pcm`. Returns the 32-byte source-hash
    recovered from the audio track. CRITICAL no-oracle invariant: this
    function must NEVER raise on garbage input — it returns 32 bytes of
    deterministic-but-meaningless data so the caller's hash compare uniformly
    produces `TamperDetectedError`. Catches all internal exceptions and
    falls back to zero-bytes."""
    from . import _music as _m
    try:
        # Mirror the v3-audio decode in `_wav_extract`: peek at the first
        # 16 bit-packed bytes for magic + length, then pull the exact
        # envelope size out. `decode_music_payload_le` is the LEGACY
        # uM01 path (with zlib wrapper); the v3 audio envelope uses the
        # raw bit-unpacked path via `decode_music_bytes_le(n)`.
        head = _m.decode_music_bytes_le(pcm, 16)
        if not head.startswith(MAGIC_V3_AUDIO):
            return b"\x00" * 32
        ciphertext_len = struct.unpack(">Q", head[8:16])[0]
        total = V3_AUDIO_HEADER_SIZE + ciphertext_len
        envelope = _m.decode_music_bytes_le(pcm, total)
        if len(envelope) < total:
            return b"\x00" * 32
        inner_payload, _ = _parse_v3_audio_envelope(envelope, password)
        # `inner_payload` is exactly the source_hash on a clean decode
        # (we set payload_len = 32 in the inner during embed).
        if len(inner_payload) >= 32:
            return inner_payload[:32]
        # Short payload — pad with zeros to keep the comparison uniform.
        return inner_payload + b"\x00" * (32 - len(inner_payload))
    except Exception:
        # Wrong password / truncated / structurally invalid — return defined
        # garbage so the caller still hits the unified TamperDetectedError
        # branch instead of leaking "audio extraction failed" as a separate
        # signal vs "hash mismatch".
        return b"\x00" * 32


def _video_has_audio_stream(src: Path) -> bool:
    """Return True if `src` contains at least one audio stream. Used to
    decide whether to run the hash check on extract — old Stone MKVs from
    before this round have no audio and must extract without error."""
    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    # FFprobe via FFmpeg (`-show_streams` would need ffprobe binary; we
    # don't ship that). Easier: a one-second null-output run with audio
    # mapped — exit code 0 if there's an audio stream, non-zero if not.
    args = [
        str(ffmpeg), "-y", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:a:0",
        "-f", "null",
        "-frames:a", "1",
        "-",
    ]
    try:
        rc = subprocess.call(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        return rc == 0
    except Exception:
        return False


def _extract_audio_hash_from_video(src: Path, password: bytes) -> bytes:
    """Extract the audio track of a Stone-mode video file, decode the
    embedded v3 audio envelope, return the 32-byte source-hash. On any
    failure (no audio stream, decode failure, wrong password, garbage) —
    returns 32 bytes of defined-zero. Caller compares the returned bytes
    to the recomputed payload hash; mismatch → `TamperDetectedError`."""
    import tempfile
    ffmpeg = _ffmpeg_path()
    creationflags = 0x08000000 if os.name == "nt" else 0
    tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
    try:
        # Decode any audio codec (FLAC for MKV, ALAC for MP4) to PCM s16le
        # — same format we packed it as on the embed side.
        args = [
            str(ffmpeg), "-y", "-loglevel", "error",
            "-i", str(src),
            "-vn",
            "-map", "0:a:0",
            "-c:a", "pcm_s16le",
            "-ar", "44100", "-ac", "2",
            str(tmp_wav),
        ]
        rc = subprocess.call(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        if rc != 0 or not tmp_wav.exists() or tmp_wav.stat().st_size < 100:
            return b"\x00" * 32
        wav_bytes = tmp_wav.read_bytes()
        # Strip RIFF/WAVE header to get raw PCM. Use the existing _wav_extract
        # parsing approach inline so we don't have to refactor that function.
        if wav_bytes[:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            return b"\x00" * 32
        p = 12
        pcm = b""
        while p + 8 <= len(wav_bytes):
            ck_id = wav_bytes[p:p + 4]
            ck_size = struct.unpack("<I", wav_bytes[p + 4:p + 8])[0]
            if ck_id == b"data":
                pcm = wav_bytes[p + 8:p + 8 + ck_size]
                break
            p += 8 + ck_size + (ck_size & 1)
        if not pcm:
            return b"\x00" * 32
        return _extract_hash_from_video_audio_pcm(pcm, password)
    except Exception:
        return b"\x00" * 32
    finally:
        try: tmp_wav.unlink()
        except OSError: pass


def _verify_video_audio_hash(src: Path, payload: bytes, password: bytes) -> None:
    """Tamper-detection gate for video extract paths. Computes SHA-256 of
    `payload`, extracts the hash hidden in the muxed audio track, compares.
    Mismatch raises `TamperDetectedError` with a vague user-facing message.

    No-oracle invariant: a wrong password (which decrypts the video to
    garbage payload AND decrypts the audio to garbage hash) produces an
    identical mismatch + identical error message as a real tampered file.
    The user cannot distinguish the two cases from the error alone.

    Skipped silently with a log warning when the source has no audio
    stream (back-compat with pre-this-round Stone MKVs)."""
    if not _video_has_audio_stream(src):
        # Old Stone MKV without an audio track — graceful degradation.
        # Don't raise; the user-visible behavior is "extract works, no
        # tamper check available." A log line records the soft-skip for
        # debugging, but no UI error.
        try:
            from ..utils.logger import get_logger
            get_logger().info(
                "video extract: no audio stream on %s — skipping hash check "
                "(legacy pre-tamper-detection Stone file).", src.name)
        except Exception:
            pass
        return
    expected = _sha256_bytes(payload)
    recovered = _extract_audio_hash_from_video(src, password)
    if expected != recovered:
        raise TamperDetectedError()


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
    # Test the most zoomed-in frame (smallest hw).
    test_hw = base_hw * _MKV_ZOOM_LO
    if not _m.viewport_is_interesting(128, 128, base_cx, base_cy, test_hw):
        base_cx, base_cy, base_hw = _m._FALLBACK_VIEWPORT
    return (base_cx, base_cy, base_hw, r_ph, g_ph, b_ph, palette_id)


# Zoom range: cur_hw spans base_hw × _MKV_ZOOM_HI at frame 0 (wide,
# zoomed-OUT) down to base_hw × _MKV_ZOOM_LO at the final frame (tight,
# zoomed-IN). 2.0x → 0.2x = 10× total zoom-IN across the clip — way
# more dramatic than the prior 4× zoom-OUT, with each successive frame
# revealing genuinely new fractal detail at the boundary.
#
# Why zoom-IN instead of zoom-OUT: zoom-IN feels more "alive" because
# the Mandelbrot reveals deeper structure as you go closer to the
# boundary. Zoom-OUT loses detail (everything pulls back), which felt
# static even at 4×.
#
# Why 10× and not 100×/1000×: the iteration code uses float32 + uint8
# max_iter (255). Beyond ~10²–10³× zoom, float32 precision and
# iteration-cap clipping start producing visible artifacts (uniform
# colored mush instead of fractal detail). 10× stays well inside the
# clean working range and keeps any curated viewport interesting at
# both ends of the zoom.
_MKV_ZOOM_LO = 0.2
_MKV_ZOOM_HI = 2.0


def _mkv_frame_viewport(base_seed, frame_idx: int, n_frames: int):
    """Per-frame Mandelbrot seed for the v3 video flythrough. Takes a
    pre-validated base seed (from `_mkv_choose_base_viewport`) and
    animates a smooth zoom-IN + slow palette-phase drift around the
    fixed base center. Returns the 7-tuple
    (cx, cy, half_width, r_phase, g_phase, b_phase, palette_id) that
    `_mandelbrot.generate_keystream` accepts.

    Animation rules:
      - Center stays FIXED on the base viewport — no per-frame pan.
        Same boundary point, plunging deeper as the clip progresses.
      - Zoom goes from `_MKV_ZOOM_HI * base_hw` (frame 0, wide)
        smoothly down to `_MKV_ZOOM_LO * base_hw` (last frame, tight).
        Exponential interpolation so each step is a constant
        multiplicative ratio (visually smooth — no acceleration jolt).
      - Palette phases drift sinusoidally over the clip so colors
        cycle gently — gives the fractal's body and arms a lively
        "breathing" feel without changing the underlying shape.
    """
    base_cx, base_cy, base_hw, r_ph, g_ph, b_ph, palette_id = base_seed

    t = (frame_idx / max(1, n_frames - 1)) if n_frames > 1 else 0.0

    # Smooth zoom-IN: cur_hw shrinks from base_hw × HI to base_hw × LO.
    cur_hw = base_hw * (_MKV_ZOOM_HI ** (1.0 - t)) * (_MKV_ZOOM_LO ** t)

    # Palette drift: ±π/3 over the clip, channels offset by 120° / 240°
    # so the color shift moves through hue space rather than just
    # brightening/darkening uniformly.
    drift = math.sin(t * 2.0 * math.pi) * (math.pi / 3.0)
    return (base_cx, base_cy, cur_hw,
            r_ph + drift,
            g_ph + drift * 0.7,
            b_ph + drift * 1.3,
            palette_id)


def _mkv_render_single_frame(f: int, base_seed, n_frames: int,
                                envelope: bytes,
                                render_dim: int = MKV_FRACTAL_RENDER_DIM_DEFAULT) -> bytes:
    """Render a single MKV frame: Mandelbrot viewport for index `f`,
    upscale to output dim, apply Gaussian low-pass blur to make the
    carrier codec-friendly, bit-pack the envelope chunk that lands in
    this frame, return raw rgb24 pixel bytes. Pure function — no
    shared mutable state — so it's safe to run concurrently from a
    `ThreadPoolExecutor`. NumPy releases the GIL during the iteration
    loop and PIL releases it during resize/blur, so threads actually
    run in parallel on multi-core systems.

    Pipeline:
      1. Render fractal at `render_dim × render_dim` (high-resolution
         math — supports the 10× clip-wide zoom-in without losing
         boundary detail).
      2. Bilinear upscale to MKV_FRAME_W × MKV_FRAME_H if needed.
      3. Gaussian blur (σ = MKV_FRACTAL_LOWPASS_SIGMA) — strips
         high-frequency content so FFV1 compression stays effective.
         Touches only the carrier (top 7 bits per pixel byte).
      4. Bit-pack envelope chunk into pixel-byte LSBs at scatter
         positions. The blur happens BEFORE this step so payload bits
         are written into the blurred carrier and survive the encode
         bit-perfect."""
    from . import _mandelbrot as _m
    from PIL import Image as _PIL
    from PIL import ImageFilter as _PIF
    env_len = len(envelope)
    seed = _mkv_frame_viewport(base_seed, f, n_frames)
    small = _m.generate_keystream(render_dim, render_dim, seed,
                                   safety_net=False)
    img = _PIL.frombuffer(
        "RGB", (render_dim, render_dim),
        small, "raw", "RGB", 0, 1)
    if render_dim != MKV_FRAME_W:
        img = img.resize((MKV_FRAME_W, MKV_FRAME_H),
                          _PIL.Resampling.BILINEAR)
    # Low-pass filter: flatten high-frequency content in the carrier
    # so FFV1 can compress it efficiently. Sigma ≈ bilinear-blur
    # spatial-frequency of plain-384 upscale, so file size stays in
    # the ~46 MB neighborhood the previous fixed-384 baseline gave us.
    img = img.filter(_PIF.GaussianBlur(radius=MKV_FRACTAL_LOWPASS_SIGMA))
    fractal = img.tobytes()
    chunk_start = f * MKV_ENVELOPE_BYTES_PER_FRAME
    chunk_end = min(env_len, chunk_start + MKV_ENVELOPE_BYTES_PER_FRAME)
    chunk = envelope[chunk_start:chunk_end] if chunk_start < env_len else b""
    return _mandelbrot_pack_envelope_into_fractal(
        chunk, fractal, MKV_BYTES_PER_FRAME)


def _video_frames_iter(envelope: bytes, n_frames: int,
                         render_dim: int = MKV_FRACTAL_RENDER_DIM_DEFAULT,
                         progress=None):
    """Yield exactly n_frames pixel-byte buffers (each MKV_BYTES_PER_FRAME
    long) ready for FFmpeg's rawvideo stdin. Each frame renders the SAME
    base Mandelbrot viewport (chosen once + validated up-front) at a
    smoothly-shifting zoom factor, with `MKV_ENVELOPE_BYTES_PER_FRAME`
    bytes of the v3 envelope bit-packed into pixel LSBs. Tail frames past
    the envelope use empty bit-packs (pure fractal).

    Codec-agnostic: the same frame stream feeds both the FFV1/MKV path
    and the lossless H.264/MP4 path. Only the FFmpeg encoder args downstream
    differ between the two containers.

    Parallel render: frames are produced by a `ThreadPoolExecutor` with
    a sliding submission window, so on multi-core systems frames render
    concurrently while still being **yielded in order** for FFmpeg.
    Typical 3-4× speedup vs sequential on an 8-core system. Window size
    is capped so we never have more than `n_workers + 2` frames in
    flight (peak RAM ≈ 21 MB extra at 1024² rgb24).

    `safety_net=False` is critical here: the per-frame fallback inside
    `generate_keystream` would otherwise swap mid-clip when individual
    frames cross into uniform regions, causing the "different fractals
    flickering" effect the user reported. The base viewport is
    pre-validated by `_mkv_choose_base_viewport` so we don't need a
    per-frame fallback — the chosen base stays interesting throughout
    the zoom-out range."""
    from concurrent.futures import ThreadPoolExecutor
    from collections import deque

    base_seed = _mkv_choose_base_viewport(envelope)

    # Sequential fast path for tiny clips — thread pool spinup costs
    # more than a few frames' rendering.
    if n_frames < 4:
        for f in range(n_frames):
            yield _mkv_render_single_frame(f, base_seed, n_frames, envelope,
                                             render_dim=render_dim)
            if progress is not None:
                progress((f + 1) / n_frames)
        return

    n_workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    # Sliding window: submit `window` ahead, then yield-and-submit one
    # more for each frame we hand to FFmpeg. Bounded RAM regardless of
    # n_frames.
    window = n_workers + 2

    with ThreadPoolExecutor(max_workers=n_workers,
                              thread_name_prefix="video-frame") as ex:
        pending: deque = deque()
        submitted = 0
        # Prime the pipe.
        while submitted < min(n_frames, window):
            pending.append(ex.submit(
                _mkv_render_single_frame, submitted,
                base_seed, n_frames, envelope, render_dim))
            submitted += 1
        while pending:
            # Block on the head future to preserve order.
            yield pending.popleft().result()
            if submitted < n_frames:
                pending.append(ex.submit(
                    _mkv_render_single_frame, submitted,
                    base_seed, n_frames, envelope, render_dim))
                submitted += 1
            if progress is not None:
                completed = submitted - len(pending)
                progress(completed / n_frames)


def _mkv_embed_to_file(src_bytes: bytes, src_ext: str, dst: Path,
                        cross_category: bool = False,
                        password: bytes = b"",
                        progress=None) -> None:
    """Encode the source as a Matroska/FFV1 video. Cross-category outputs
    (image/audio/doc → MKV) build a v3 envelope encrypted under `password`
    and bit-pack it across an animated-Mandelbrot frame sequence (10-sec
    minimum, 30 fps, 1024×1024), plus mux a Stone-music FLAC audio track
    that carries a SHA-256 hash of the source bytes (tamper detection).
    Same-category video → MKV (rare) keeps the legacy plaintext UCMSv1
    path for backward compatibility (no audio track on that path).

    Frames render in parallel via `_video_frames_iter`'s thread pool;
    `progress` (0..1) ticks per-frame as each one is consumed by FFmpeg."""
    import tempfile
    audio_tmp_wav = None
    audio_tmp_flac = None
    try:
        if cross_category:
            # Build the v3 envelope at its natural size (no carrier padding —
            # tail frames past the envelope keep fractal-natural LSBs at
            # non-scatter positions, which is much cheaper than filling every
            # frame with random data and almost as detection-resistant).
            envelope = _v3_video_envelope(src_bytes, src_ext, password)
            n_frames = _mkv_v3_frame_count(len(envelope))
            # Adaptive: bump fractal render dim when we're at the duration
            # floor — small payloads have render-time budget to spare and
            # benefit visibly from a sharper internal fractal.
            render_dim = _mkv_pick_render_dim(n_frames)
            frames_iter = _video_frames_iter(envelope, n_frames,
                                              render_dim=render_dim,
                                              progress=progress)
            # Build the Stone-music audio track carrying SHA-256 of source.
            source_hash = _sha256_bytes(src_bytes)
            audio_wav_bytes = _build_video_audio_wav(
                source_hash, password, n_frames)
            audio_tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
            audio_tmp_wav.write_bytes(audio_wav_bytes)
            audio_tmp_flac = Path(tempfile.mkstemp(suffix=".flac")[1])
            _flac_via_ffmpeg(audio_tmp_wav, audio_tmp_flac)
            # No identifying metadata tags — the v3 magic at the start of the
            # bit-packed pixel stream is identification enough, and the old
            # UC_PAYLOAD_SIZE / UC_ORIG_EXT tags would leak source size and
            # extension in the clear (defeats the v3 "ext is hidden" property).
            metadata_args: list[str] = []
            payload_iter = frames_iter
            audio_input_args = ["-i", str(audio_tmp_flac)]
            map_args = ["-map", "0:v", "-map", "1:a"]
            audio_codec_args = ["-c:a", "copy"]
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
            audio_input_args = []
            map_args = []
            audio_codec_args = []

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
            *audio_input_args,
            *map_args,
            "-c:v", "ffv1",
            "-level", "3",
            "-coder", "1",
            "-context", "1",
            "-g", "1",
            "-slices", "4",
            "-slicecrc", "1",
            "-pix_fmt", "rgb24",
            "-r", str(MKV_FPS),
            *audio_codec_args,
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
    finally:
        for tmp in (audio_tmp_wav, audio_tmp_flac):
            if tmp is not None:
                try: tmp.unlink()
                except OSError: pass


# Streaming MKV embed for sources >= 100 MB. Two-pass: HMAC source for IV,
# then write encrypted envelope to a tempfile, mmap it, parallel-render
# frames from the mmap, pipe to FFmpeg. Peak RAM ~30-50 MB; peak disk ~1x
# source size.
_MKV_STREAMING_THRESHOLD = 100 * 1024 * 1024   # >= 100 MB sources stream


def _mkv_streaming_iv_for_source(src_path: "Path", src_ext: str,
                                   payload_size: int, password: bytes) -> bytes:
    """First-pass IV derivation. Streams source bytes through an
    incremental HMAC that produces the SAME 16-byte IV the non-streaming
    `_v3_audio_envelope` / `_v3_video_envelope` would derive in-memory.

    The HMAC is fed the inner-plaintext byte sequence:
        ext_len(1) || ext_bytes || payload_len(8 BE) || source_bytes
    """
    from . import _stone_crypto as _sc
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")[:255]
    key = _sc.derive_key(password)
    hasher = _sc.StreamingIVHasher(key)
    hasher.update(bytes([len(ext_bytes)]))
    hasher.update(ext_bytes)
    hasher.update(struct.pack(">Q", payload_size))
    with open(src_path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.iv()


def _mkv_streaming_write_envelope(src_path: "Path", src_ext: str,
                                    password: bytes, iv: bytes,
                                    envelope_path: "Path",
                                    progress=None) -> int:
    """Second pass: stream source → AES-CTR encrypt → write envelope to
    tempfile. Returns total envelope size in bytes.

    Envelope layout (same as non-streaming):
        MAGIC_V3_VIDEO (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext
    """
    from . import _stone_crypto as _sc
    if not src_ext.startswith("."):
        src_ext = "." + src_ext
    ext_bytes = src_ext.encode("utf-8")[:255]
    payload_size = src_path.stat().st_size
    inner_size = 1 + len(ext_bytes) + 8 + payload_size
    # Ciphertext is same length as inner (CTR mode). Envelope total
    # known in advance.
    envelope_size = V3_VIDEO_HEADER_SIZE + inner_size
    key = _sc.derive_key(password)
    enc = _sc.StreamingEncryptor(key, iv)
    bytes_written = 0
    with open(envelope_path, "wb") as out:
        # Header
        out.write(MAGIC_V3_VIDEO)
        out.write(struct.pack(">Q", inner_size))
        out.write(iv)
        out.write(b"\x00\x00\x00\x00")
        # Encrypt inner-prefix + streamed source body
        prefix = (bytes([len(ext_bytes)]) + ext_bytes
                   + struct.pack(">Q", payload_size))
        out.write(enc.update(prefix))
        with open(src_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                out.write(enc.update(chunk))
                bytes_written += len(chunk)
                if progress is not None and payload_size > 0:
                    progress(bytes_written / payload_size)
        out.write(enc.finalize())
    return envelope_size


def _mkv_embed_to_file_streamed(src_path: "Path", src_ext: str, dst: "Path",
                                  password: bytes = b"",
                                  progress=None) -> None:
    """Streaming counterpart to `_mkv_embed_to_file` for multi-GB sources.

    Same v3-video envelope format and same audio-track-with-tamper-hash
    feature as the non-streaming path. Differs only in WHERE the bytes
    live during the encrypt step:
      - Non-streaming: plaintext + ciphertext both in RAM
      - Streaming:     plaintext on disk (src_path), ciphertext in a
                       tempfile, frames produced from a mmap of that
                       tempfile.
    Peak RAM: bounded by chunk size + frame buffers (~30–50 MB)
    regardless of source size."""
    import mmap as _mmap
    import tempfile
    payload_size = src_path.stat().st_size
    audio_tmp_wav = None
    audio_tmp_flac = None
    envelope_tmp = None
    envelope_mmap = None
    envelope_fp = None
    try:
        # Pass 1: derive IV by streaming HMAC over the inner-plaintext
        # byte sequence. No large buffer held.
        if progress is not None:
            progress(0.05)
        iv = _mkv_streaming_iv_for_source(src_path, src_ext, payload_size, password)

        # Pass 2: stream-encrypt source to envelope tempfile.
        envelope_tmp = Path(tempfile.mkstemp(suffix=".env")[1])
        encrypt_progress = (lambda p: progress(0.05 + p * 0.30)) if progress else None
        envelope_size = _mkv_streaming_write_envelope(
            src_path, src_ext, password, iv, envelope_tmp,
            progress=encrypt_progress)
        if envelope_size > _MKV_ENVELOPE_HARD_CAP:
            raise ValueError(
                f"v3 video envelope: size {envelope_size} exceeds "
                f"{_MKV_ENVELOPE_HARD_CAP // (1024**3)} GiB cap.")

        # mmap the envelope so the existing parallel frame generator
        # can slice it like bytes without loading it into RAM.
        envelope_fp = open(envelope_tmp, "rb")
        envelope_mmap = _mmap.mmap(envelope_fp.fileno(), 0,
                                     access=_mmap.ACCESS_READ)

        n_frames = _mkv_v3_frame_count(envelope_size)
        render_dim = _mkv_pick_render_dim(n_frames)

        # Audio track: SHA-256 of source via streaming hash + Stone-music WAV.
        source_hash = _sha256_file(src_path)
        audio_wav_bytes = _build_video_audio_wav(
            source_hash, password, n_frames)
        audio_tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        audio_tmp_wav.write_bytes(audio_wav_bytes)
        audio_tmp_flac = Path(tempfile.mkstemp(suffix=".flac")[1])
        _flac_via_ffmpeg(audio_tmp_wav, audio_tmp_flac)

        if progress is not None:
            progress(0.40)
        frames_progress = (lambda p: progress(0.40 + p * 0.55)) if progress else None
        frames_iter = _video_frames_iter(envelope_mmap, n_frames,
                                          render_dim=render_dim,
                                          progress=frames_progress)

        ffmpeg = _ffmpeg_path()
        creationflags = 0x08000000 if os.name == "nt" else 0
        args = [
            str(ffmpeg), "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{MKV_FRAME_W}x{MKV_FRAME_H}",
            "-framerate", str(MKV_FPS),
            "-i", "-",
            "-i", str(audio_tmp_flac),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "ffv1",
            "-level", "3",
            "-coder", "1",
            "-context", "1",
            "-g", "1",
            "-slices", "4",
            "-slicecrc", "1",
            "-pix_fmt", "rgb24",
            "-r", str(MKV_FPS),
            "-c:a", "copy",
            str(dst),
        ]
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=creationflags,
        )
        try:
            for chunk in frames_iter:
                proc.stdin.write(chunk)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        _, stderr = proc.communicate(timeout=3600)
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"FFmpeg MKV streaming embed failed (exit {proc.returncode}): {tail}")
        if progress is not None:
            progress(1.0)
    finally:
        # mmap must be closed before its underlying file on Windows or
        # the file unlink will fail. Same with file handle.
        if envelope_mmap is not None:
            try: envelope_mmap.close()
            except Exception: pass
        if envelope_fp is not None:
            try: envelope_fp.close()
            except Exception: pass
        for tmp in (envelope_tmp, audio_tmp_wav, audio_tmp_flac):
            if tmp is not None:
                try: tmp.unlink()
                except OSError: pass


_MKV_ENVELOPE_HARD_CAP = 25 * 1024 ** 3   # 25 GiB — refuse anything larger early.
                                          # Sized to hold a feature-length 4 K
                                          # video file as a Stone payload. The
                                          # streaming extract path keeps RAM
                                          # bounded regardless of envelope size,
                                          # so this is just a sanity rail.

# At/above this source size, MKV extract uses the streaming path
# (`_mkv_v3_extract_streaming`) which writes the recovered payload to
# a tempfile chunk-by-chunk instead of materializing it in RAM. Below
# this threshold, the in-memory path is fine and slightly faster.
_MKV_EXTRACT_STREAMING_THRESHOLD = 100 * 1024 * 1024   # 100 MB


def _mkv_v3_extract_streaming(src: "Path", password: bytes,
                                dst_payload_path: "Path",
                                progress=None) -> str:
    """Streaming counterpart to `_mkv_extract_from_file` for huge MKV
    sources. Pipes FFmpeg → raw rgb24 frames → bit-unpack envelope chunk
    → AES-CTR streaming decrypt → write plaintext payload chunks to
    `dst_payload_path` on disk. Peak RAM: one frame buffer (~3 MB) +
    chunk-sized buffers (~tens of MB) regardless of payload size.

    Returns the recovered source extension (e.g. ".png", ".wav").

    The caller is responsible for `dst_payload_path` lifecycle (create
    tempfile beforehand, move/rename to final dst on success, delete on
    failure).

    Wrong password produces silent garbage in the output file (matches
    the no-oracle invariant). Tamper detection happens AFTER this returns,
    via `_verify_video_audio_hash_streaming` against the same tempfile.
    """
    from . import _stone_crypto as _sc
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

    def _read_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _stop_ffmpeg_early() -> None:
        try: proc.stdout.close()
        except OSError: pass
        try: proc.terminate()
        except OSError: pass
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except OSError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: pass

    try:
        # Frame 0: must contain the v3 video envelope HEADER
        # (MAGIC + ciphertext_len + IV + salt). Bit-unpack to pull
        # those out, set up the streaming decryptor.
        first_frame = _read_exact(MKV_BYTES_PER_FRAME)
        if progress is not None:
            progress(0.10)
        if len(first_frame) < MKV_BYTES_PER_FRAME:
            raise ValueError("v3 video envelope: stream ended before first frame.")

        head_unpacked = _mandelbrot_unpack_envelope_from_pixels(
            first_frame, V3_VIDEO_HEADER_SIZE,
            total_pixel_bytes=MKV_BYTES_PER_FRAME)
        if not head_unpacked.startswith(MAGIC_V3_VIDEO):
            raise ValueError("v3 video envelope: magic not found.")
        ciphertext_len = struct.unpack(">Q", head_unpacked[8:16])[0]
        if ciphertext_len > _MKV_ENVELOPE_HARD_CAP:
            raise ValueError(
                f"v3 video envelope: ciphertext_len={ciphertext_len} "
                f"exceeds {_MKV_ENVELOPE_HARD_CAP // (1024**3)} GiB cap.")
        iv = head_unpacked[16:32]   # offset 16: after MAGIC(8) + len(8)
        # NOTE: salt(4) at offset 32-36 is currently reserved/unused.

        total_env = V3_VIDEO_HEADER_SIZE + ciphertext_len
        frames_needed = max(1, math.ceil(
            total_env / MKV_ENVELOPE_BYTES_PER_FRAME))

        # Initialize streaming AES-CTR decryptor.
        key = _sc.derive_key(password)
        dec = _sc.StreamingDecryptor(key, iv)

        # Inner-header buffer: holds decrypted bytes until ext_len + ext
        # + payload_len (max 264 B) is parsed. Subsequent decrypted bytes
        # stream straight to disk.
        inner_buf = bytearray()
        inner_header_size: "Optional[int]" = None
        recovered_ext: "Optional[str]" = None
        payload_len: int = 0
        payload_bytes_written: int = 0

        # Counter of envelope bytes fed through `dec.update` so far —
        # used to stop reading frames once the envelope is fully consumed.
        envelope_processed = 0

        def _consume_envelope_chunk(env_chunk: bytes, out_fp) -> None:
            """Feed `env_chunk` through the streaming decryptor; route
            decrypted bytes either into `inner_buf` (until header parsed)
            or to `out_fp` (the payload tempfile)."""
            nonlocal inner_header_size, recovered_ext, payload_len, payload_bytes_written
            # Skip the bytes corresponding to the v3 header (already
            # consumed via `head_unpacked` — those bytes are NOT
            # ciphertext, they're the envelope header in plaintext).
            #
            # The envelope layout is:
            #   [ MAGIC(8) | len(8) | IV(16) | salt(4) ]   <- header, 36 bytes
            #   [ ciphertext (ciphertext_len bytes)       ]   <- decrypt input
            # We only feed the ciphertext portion to the decryptor.
            decrypted = dec.update(env_chunk)
            if not decrypted:
                return
            # Phase 1: inner header. Accumulate until we know the
            # header size, then split.
            if inner_header_size is None:
                inner_buf.extend(decrypted)
                # Need at least 1 byte to know ext_len.
                if len(inner_buf) < 1:
                    return
                ext_len = inner_buf[0]
                # Sanity-clamp on garbage/wrong-password decrypts.
                if ext_len > 64:
                    # Wrong password produced garbage. Treat as a
                    # zero-length-ext payload so we still write something
                    # (matches the in-memory `_parse_v3_inner_clamped`
                    # behavior — no oracle leak).
                    ext_len = 0
                header_size = 1 + ext_len + 8
                if len(inner_buf) < header_size:
                    return  # need more bytes
                inner_header_size = header_size
                if ext_len > 0:
                    recovered_ext = inner_buf[1:1 + ext_len].decode(
                        "utf-8", errors="replace")
                    if not recovered_ext.startswith("."):
                        recovered_ext = "." + recovered_ext
                else:
                    recovered_ext = ".bin"
                payload_len = struct.unpack(
                    ">Q", inner_buf[1 + ext_len:1 + ext_len + 8])[0]
                # Sanity-clamp implausibly huge payload_len (wrong password).
                if payload_len > _MKV_ENVELOPE_HARD_CAP:
                    payload_len = 0
                # Spill any remaining inner_buf bytes (already past header)
                # into the payload file.
                tail = bytes(inner_buf[header_size:])
                inner_buf.clear()
                if tail and payload_bytes_written < payload_len:
                    take = min(len(tail), payload_len - payload_bytes_written)
                    out_fp.write(tail[:take])
                    payload_bytes_written += take
                return
            # Phase 2: payload bytes go straight to disk, capped at
            # payload_len so we don't write the random-padding tail.
            if payload_bytes_written < payload_len:
                take = min(len(decrypted), payload_len - payload_bytes_written)
                out_fp.write(decrypted[:take])
                payload_bytes_written += take

        with open(dst_payload_path, "wb") as out_fp:
            # Process the FIRST frame's envelope content. Skip the
            # 36-byte header (MAGIC + len + IV + salt), then feed the
            # remaining bytes through the decryptor.
            first_env_full = _mandelbrot_unpack_envelope_from_pixels(
                first_frame,
                min(MKV_ENVELOPE_BYTES_PER_FRAME, total_env),
                total_pixel_bytes=MKV_BYTES_PER_FRAME)
            if len(first_env_full) > V3_VIDEO_HEADER_SIZE:
                _consume_envelope_chunk(
                    first_env_full[V3_VIDEO_HEADER_SIZE:], out_fp)
                envelope_processed = len(first_env_full)
            else:
                envelope_processed = len(first_env_full)

            # Subsequent frames.
            for f in range(1, frames_needed):
                if envelope_processed >= total_env:
                    break
                if payload_bytes_written >= payload_len and inner_header_size is not None:
                    # Already wrote everything we need; can stop early.
                    break
                frame_bytes = _read_exact(MKV_BYTES_PER_FRAME)
                if len(frame_bytes) < MKV_BYTES_PER_FRAME:
                    raise ValueError(
                        f"v3 video envelope: stream ended at frame {f} "
                        f"of {frames_needed} (truncated MKV).")
                want = min(MKV_ENVELOPE_BYTES_PER_FRAME,
                            total_env - envelope_processed)
                env_chunk = _mandelbrot_unpack_envelope_from_pixels(
                    frame_bytes, want,
                    total_pixel_bytes=MKV_BYTES_PER_FRAME)
                _consume_envelope_chunk(env_chunk, out_fp)
                envelope_processed += len(env_chunk)
                if progress is not None:
                    progress(0.10 + ((f + 1) / frames_needed) * 0.85)

            # Drain the cipher's finalize (no-op for CTR but required
            # for API correctness — emits any buffered output).
            tail = dec.finalize()
            if tail and inner_header_size is not None and payload_bytes_written < payload_len:
                take = min(len(tail), payload_len - payload_bytes_written)
                out_fp.write(tail[:take])
                payload_bytes_written += take

        _stop_ffmpeg_early()
        if progress is not None:
            progress(0.95)

        # Wrong-password / garbage-decrypt fallback: if we never parsed
        # a sane header, emit empty file and ".bin" ext (matches
        # `_parse_v3_inner_clamped`'s no-oracle behavior).
        if recovered_ext is None:
            recovered_ext = ".bin"
        return recovered_ext
    except Exception:
        _stop_ffmpeg_early()
        raise


def _verify_video_audio_hash_streaming(src: "Path", payload_path: "Path",
                                          password: bytes) -> None:
    """Tamper-detection gate for the streaming extract path. Computes
    SHA-256 of the recovered-payload tempfile via streaming hash, then
    compares against the hash hidden in the muxed audio track.

    Same semantics as `_verify_video_audio_hash` (the in-memory
    counterpart): mismatch raises `TamperDetectedError` with a vague
    user-facing message; backward compat with pre-audio-track Stone
    MKVs preserved (graceful skip when no audio stream)."""
    if not _video_has_audio_stream(src):
        try:
            from ..utils.logger import get_logger
            get_logger().info(
                "video extract: no audio stream on %s — skipping hash check "
                "(legacy pre-tamper-detection Stone file).", src.name)
        except Exception:
            pass
        return
    expected = _sha256_file(payload_path)   # streams payload from disk
    recovered = _extract_audio_hash_from_video(src, password)
    if expected != recovered:
        raise TamperDetectedError()


def _mkv_extract_from_file(src: Path,
                            password: bytes = b"",
                            progress=None) -> Tuple[bytes, str]:
    """Pipe the MKV through FFmpeg → raw rgb24 frames → bit-unpack the
    v3 envelope, OR fall back to legacy plaintext-bytes-in-pixels.

    **Streaming**: reads FFmpeg's stdout one frame at a time and bit-unpacks
    each frame as it arrives. Peak RAM is `(one frame buffer = 3 MB) +
    (envelope-so-far)`, not the whole decoded MKV. Once the envelope is
    complete we terminate FFmpeg early — no waiting for trailing padding
    frames. This is what lets a 700 MB MKV → PNG complete instead of
    OOM-ing or hitting the old 20-min `proc.communicate()` timeout.

    Dual-detect by bit-unpacking the first frame's first 16 bytes via the
    v3 scatter pattern. If `MAGIC_V3_VIDEO` matches: streaming v3 path.
    Otherwise: legacy buffered fall-back (legacy MKVs were only used in
    early plaintext rounds and were small by definition, so the buffered
    read is acceptable for that path).
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

    def _read_exact(n: int) -> bytes:
        """Read exactly n bytes from FFmpeg's stdout, blocking until
        either we have n bytes or the pipe closes (returns short on EOF)."""
        buf = bytearray()
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _stop_ffmpeg_early() -> None:
        """Terminate FFmpeg gracefully once we've drained what we need."""
        try:
            proc.stdout.close()
        except OSError:
            pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    try:
        # Read first frame to detect v3 vs legacy.
        first_frame = _read_exact(MKV_BYTES_PER_FRAME)
        if progress is not None:
            progress(0.30)   # FFmpeg spun up + first frame in hand

        if len(first_frame) >= MKV_BYTES_PER_FRAME:
            head_unpacked = _mandelbrot_unpack_envelope_from_pixels(
                first_frame, V3_VIDEO_HEADER_SIZE,
                total_pixel_bytes=MKV_BYTES_PER_FRAME)
            if head_unpacked.startswith(MAGIC_V3_VIDEO):
                # Streaming v3 path.
                ciphertext_len = struct.unpack(">Q", head_unpacked[8:16])[0]
                if ciphertext_len > _MKV_ENVELOPE_HARD_CAP:
                    raise ValueError(
                        f"v3 video envelope: ciphertext_len={ciphertext_len} "
                        f"exceeds {_MKV_ENVELOPE_HARD_CAP // (1024**3)} GiB cap.")
                total_env = V3_VIDEO_HEADER_SIZE + ciphertext_len
                frames_needed = max(1, math.ceil(
                    total_env / MKV_ENVELOPE_BYTES_PER_FRAME))

                envelope_buf = bytearray()
                # First frame: bit-unpack as much as we need from it.
                want = min(MKV_ENVELOPE_BYTES_PER_FRAME, total_env)
                envelope_buf.extend(_mandelbrot_unpack_envelope_from_pixels(
                    first_frame, want,
                    total_pixel_bytes=MKV_BYTES_PER_FRAME))

                # Subsequent frames: read + bit-unpack one at a time.
                for f in range(1, frames_needed):
                    if len(envelope_buf) >= total_env:
                        break
                    frame_bytes = _read_exact(MKV_BYTES_PER_FRAME)
                    if len(frame_bytes) < MKV_BYTES_PER_FRAME:
                        raise ValueError(
                            f"v3 video envelope: stream ended at frame {f} "
                            f"of {frames_needed} (truncated MKV).")
                    want = min(MKV_ENVELOPE_BYTES_PER_FRAME,
                                total_env - len(envelope_buf))
                    envelope_buf.extend(_mandelbrot_unpack_envelope_from_pixels(
                        frame_bytes, want,
                        total_pixel_bytes=MKV_BYTES_PER_FRAME))
                    if progress is not None:
                        progress(0.30 + ((f + 1) / frames_needed) * 0.65)

                # Got the envelope — stop FFmpeg, decrypt, return.
                _stop_ffmpeg_early()
                if progress is not None:
                    progress(0.95)
                result = _parse_v3_video_envelope(
                    bytes(envelope_buf[:total_env]), password)
                # Tamper-detection gate. Compares SHA-256 of the recovered
                # payload to the hash hidden in the muxed audio track.
                # Wrong password and tampered file produce identical
                # `TamperDetectedError` (no-oracle invariant). Old MKVs
                # without an audio track silently skip the check.
                _verify_video_audio_hash(src, result[0], password)
                if progress is not None:
                    progress(1.0)
                return result

        # Legacy plaintext UCMSv1 fall-back. These files were only produced
        # by the pre-v3 code path and were always small (< 1 frame's worth
        # of envelope data), so the buffered read remains acceptable here.
        rest = proc.stdout.read()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if progress is not None:
            progress(1.0)
        if proc.returncode not in (0, None) and proc.returncode != 0:
            stderr_blob = proc.stderr.read() if proc.stderr else b""
            tail = (stderr_blob or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(
                f"FFmpeg MKV extract failed (exit {proc.returncode}): {tail}")
        return _parse_envelope(first_frame + rest)
    except Exception:
        # Make sure FFmpeg doesn't outlive the failure.
        _stop_ffmpeg_early()
        raise


# ---------------------------------------------------------------------------
# Host: MP4 (lossless H.264 with planar RGB)
# ---------------------------------------------------------------------------
# Identical envelope and frame pipeline to MKV — only the FFmpeg encoder
# args differ. H.264 with `-crf 0 -pix_fmt gbrp` is mathematically lossless
# RGB: no YUV conversion, no rounding, LSBs survive bit-perfect. Plays in
# Windows Media Player on Win10+ without extra codec installs (FFV1 in MKV
# does not). Same `MAGIC_V3_VIDEO` magic, so decoded frames bit-unpack
# the same way; the audio track is ALAC instead of FLAC for MP4 container
# compatibility.
#
# Why `gbrp` and not `yuv444p`: even at `-qp 0` the RGB→YUV→RGB conversion
# rounds values, which corrupts LSBs. `gbrp` (planar RGB) skips the
# colorspace transform entirely.

MP4_VCODEC_ARGS = [
    # libx264rgb is libx264's dedicated RGB-input mode. Unlike libx264 with
    # `-pix_fmt gbrp`, it never converts to YUV internally — guaranteed
    # bit-exact RGB round-trip. WMP-on-Win10+ plays it natively.
    "-c:v", "libx264rgb",
    "-preset", "fast",         # crf=0 dominates output size; preset only changes encode speed
    "-crf", "0",                # mathematically lossless
    "-pix_fmt", "rgb24",        # libx264rgb's required pixel format
]


def _mp4_embed_to_file(src_bytes: bytes, src_ext: str, dst: Path,
                        cross_category: bool = False,
                        password: bytes = b"",
                        progress=None) -> None:
    """Encode the source as a lossless-H.264 MP4. Cross-category outputs
    build a v3 envelope (same `MAGIC_V3_VIDEO` as MKV) and bit-pack it
    across an animated-Mandelbrot frame sequence, plus mux a Stone-music
    ALAC audio track carrying SHA-256 of the source for tamper detection.
    Same-category video → MP4 is not supported (no legacy plaintext path
    for MP4).

    Frames render in parallel via `_video_frames_iter`'s thread pool.
    `gbrp` pix_fmt + `crf=0` libx264 = bit-exact RGB round-trip."""
    if not cross_category:
        # MP4 has no legacy plaintext path (it was never a Stone target
        # before this round). Same-category video → MP4 should be routed
        # through the regular (non-Stone) media pipeline by the caller;
        # if we reach here in same-category mode something is wrong.
        raise RuntimeError(
            "MP4 Stone embed only supports cross-category sources. "
            "Same-type video → MP4 should use the standard media pipeline.")
    import tempfile
    audio_tmp_wav = None
    audio_tmp_m4a = None
    try:
        envelope = _v3_video_envelope(src_bytes, src_ext, password)
        n_frames = _mkv_v3_frame_count(len(envelope))
        render_dim = _mkv_pick_render_dim(n_frames)
        frames_iter = _video_frames_iter(envelope, n_frames,
                                          render_dim=render_dim,
                                          progress=progress)
        # Audio track: identical to the MKV path but encoded to ALAC for
        # MP4-container compatibility. WMP plays ALAC natively on Win10+.
        source_hash = _sha256_bytes(src_bytes)
        audio_wav_bytes = _build_video_audio_wav(
            source_hash, password, n_frames)
        audio_tmp_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        audio_tmp_wav.write_bytes(audio_wav_bytes)
        audio_tmp_m4a = Path(tempfile.mkstemp(suffix=".m4a")[1])
        _alac_via_ffmpeg(audio_tmp_wav, audio_tmp_m4a)

        ffmpeg = _ffmpeg_path()
        creationflags = 0x08000000 if os.name == "nt" else 0
        args = [
            str(ffmpeg), "-y",
            "-f", "rawvideo",
            "-pixel_format", "rgb24",
            "-video_size", f"{MKV_FRAME_W}x{MKV_FRAME_H}",
            "-framerate", str(MKV_FPS),
            "-i", "-",
            "-i", str(audio_tmp_m4a),
            "-map", "0:v", "-map", "1:a",
            *MP4_VCODEC_ARGS,
            "-r", str(MKV_FPS),
            "-c:a", "copy",
            "-movflags", "+faststart",   # MP4 metadata at front for streaming-friendly playback
            str(dst),
        ]
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=creationflags,
        )
        try:
            for chunk in frames_iter:
                proc.stdin.write(chunk)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        _, stderr = proc.communicate(timeout=1800)
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
            raise RuntimeError(f"FFmpeg MP4 embed failed (exit {proc.returncode}): {tail}")
    finally:
        for tmp in (audio_tmp_wav, audio_tmp_m4a):
            if tmp is not None:
                try: tmp.unlink()
                except OSError: pass


def _mp4_extract_from_file(src: Path,
                            password: bytes = b"",
                            progress=None) -> Tuple[bytes, str]:
    """Streaming MP4 extract — mirrors `_mkv_extract_from_file` exactly,
    but for the MP4 container. FFmpeg auto-detects H.264-gbrp on input,
    decodes to rgb24 raw frames; the bit-unpack pattern is identical to
    MKV's. After successful video decrypt, runs the same audio-track
    hash check (`_verify_video_audio_hash`)."""
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

    def _read_exact(n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                break
            buf.extend(chunk)
        return bytes(buf)

    def _stop_ffmpeg_early() -> None:
        try: proc.stdout.close()
        except OSError: pass
        try: proc.terminate()
        except OSError: pass
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except OSError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: pass

    try:
        first_frame = _read_exact(MKV_BYTES_PER_FRAME)
        if progress is not None:
            progress(0.30)
        if len(first_frame) < MKV_BYTES_PER_FRAME:
            raise ValueError("MP4 envelope: stream ended before first frame.")

        head_unpacked = _mandelbrot_unpack_envelope_from_pixels(
            first_frame, V3_VIDEO_HEADER_SIZE,
            total_pixel_bytes=MKV_BYTES_PER_FRAME)
        if not head_unpacked.startswith(MAGIC_V3_VIDEO):
            raise ValueError("MP4 envelope: v3 magic not found.")

        ciphertext_len = struct.unpack(">Q", head_unpacked[8:16])[0]
        if ciphertext_len > _MKV_ENVELOPE_HARD_CAP:
            raise ValueError(
                f"MP4 envelope: ciphertext_len={ciphertext_len} "
                f"exceeds {_MKV_ENVELOPE_HARD_CAP // (1024**3)} GiB cap.")
        total_env = V3_VIDEO_HEADER_SIZE + ciphertext_len
        frames_needed = max(1, math.ceil(
            total_env / MKV_ENVELOPE_BYTES_PER_FRAME))

        envelope_buf = bytearray()
        want = min(MKV_ENVELOPE_BYTES_PER_FRAME, total_env)
        envelope_buf.extend(_mandelbrot_unpack_envelope_from_pixels(
            first_frame, want,
            total_pixel_bytes=MKV_BYTES_PER_FRAME))

        for f in range(1, frames_needed):
            if len(envelope_buf) >= total_env:
                break
            frame_bytes = _read_exact(MKV_BYTES_PER_FRAME)
            if len(frame_bytes) < MKV_BYTES_PER_FRAME:
                raise ValueError(
                    f"MP4 envelope: stream ended at frame {f} of "
                    f"{frames_needed} (truncated MP4).")
            want = min(MKV_ENVELOPE_BYTES_PER_FRAME,
                        total_env - len(envelope_buf))
            envelope_buf.extend(_mandelbrot_unpack_envelope_from_pixels(
                frame_bytes, want,
                total_pixel_bytes=MKV_BYTES_PER_FRAME))
            if progress is not None:
                progress(0.30 + ((f + 1) / frames_needed) * 0.65)

        _stop_ffmpeg_early()
        if progress is not None:
            progress(0.95)
        result = _parse_v3_video_envelope(
            bytes(envelope_buf[:total_env]), password)
        # Same tamper-detection gate as MKV — wrong password and edited
        # file produce identical TamperDetectedError.
        _verify_video_audio_hash(src, result[0], password)
        if progress is not None:
            progress(1.0)
        return result
    except Exception:
        _stop_ffmpeg_early()
        raise


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

# Salt that scopes the keystream to Vitriol. Different tools doing similar
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
    Bottom 1 bit:
      - At scatter positions covered by the envelope: one payload bit
        from the encrypted ciphertext.
      - Everywhere else: zero.

    Non-scatter LSBs are zeroed. Random fill defeats PNG/MKV compression
    (5-7x size). Fractal-natural LSBs over-cluster on byte-0x00 for
    body-heavy fractals (208x vs 7x uniform).
    """
    import numpy as np
    fractal_arr = np.frombuffer(fractal, dtype=np.uint8)[:total_pixel_bytes]
    # Clear LSBs; payload bits OR'd in at scatter positions.
    out = fractal_arr & _FRACTAL_MASK
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
        bits = np.unpackbits(env_arr).reshape(n_env, 8)[:, ::-1].reshape(-1)
        offsets = np.arange(_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE, dtype=np.int64)
        dest = (base[:, None] + offsets[None, :]).reshape(-1)
        # OR in the payload bit at scatter positions (LSBs already cleared).
        out[dest] = out[dest] | bits
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
        # Filter seed: derived from the bit-packed pixel content + password
        # so two different sources/passwords get distinct row-filter
        # sequences (one more variability axis the forensic detector has
        # to chase). Same source + password = same seed = byte-identical
        # PNG output, preserving the determinism property.
        f_seed = hashlib.sha256(pixel_bytes[:65536] + password).digest()
        stream_png_write(dst, width, height, iter([pixel_bytes]), cancel,
                          progress, filter_seed=f_seed)
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
        f_seed = hashlib.sha256(pixel_bytes[:65536] + password).digest()
        stream_png_write(dst, width, height, iter([pixel_bytes]),
                          filter_seed=f_seed)
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
                  password: bytes, pad_to_inner: Optional[int] = None) -> bytes:
    """Build the full UCMSv3 envelope:
        MAGIC_V3 (8) | W (4) | H (4) | IV (16) | salt (4 reserved) | ciphertext

    The inner plaintext (ext+payload) is AES-256-CTR encrypted under a
    PBKDF2-derived key. Wrong password → garbage decryption with no oracle.
    Same source + same password → identical ciphertext (deterministic IV).

    `pad_to_inner`, when supplied, extends the inner plaintext with
    deterministically-derived random bytes BEFORE encryption so the
    resulting ciphertext fills the full carrier capacity. This eliminates
    the "trailing scatter slots have zero LSBs" forensic tell — with
    full-carrier padding, every pixel-byte LSB is part of an AES-CTR
    output, which is uniformly distributed by definition. Decoder reads
    only `inner.payload_len` bytes and discards the random tail.
    """
    import random as _rand
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    if pad_to_inner is not None and pad_to_inner > len(inner):
        # Pad inner with deterministic random bytes. Seed from inner +
        # password so same source + password produces same pad → same
        # ciphertext → same image.
        seed = int.from_bytes(
            hashlib.sha256(inner + password + b"v3-image-pad").digest()[:8],
            "big")
        rng = _rand.Random(seed)
        inner = inner + rng.randbytes(pad_to_inner - len(inner))
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


def _v3_audio_envelope(payload: bytes, src_ext: str, password: bytes,
                         pad_to_inner: Optional[int] = None) -> bytes:
    """Build encrypted audio envelope:
        MAGIC_V3_AUDIO (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext

    Same crypto primitives as the image side (AES-256-CTR, deterministic
    SIV-style IV, PBKDF2-derived key). The clear-text length lets the
    decoder slice the exact ciphertext span out of the bit-packed audio
    stream without scanning past the meaningful content. No image
    dimensions — audio carries no width/height.

    `pad_to_inner`, when supplied, extends the inner plaintext with
    deterministically-derived random bytes BEFORE encryption. Used by
    the muxed-audio-track path on video outputs: the audio carrier needs
    to be a fixed length matching video duration, but the actual payload
    (a 32-byte SHA-256 hash) is much smaller — padding fills the gap so
    the audio LSB stream is uniform-random end-to-end (matches the
    detection profile of a regular Stone-audio file).
    """
    import random as _rand
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    if pad_to_inner is not None and pad_to_inner > len(inner):
        seed = int.from_bytes(
            hashlib.sha256(inner + password + b"v3-audio-pad").digest()[:8],
            "big")
        rng = _rand.Random(seed)
        inner = inner + rng.randbytes(pad_to_inner - len(inner))
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


def _v3_video_envelope(payload: bytes, src_ext: str, password: bytes,
                         pad_to_inner: Optional[int] = None) -> bytes:
    """Build encrypted video envelope:
        MAGIC_V3_VIDEO (8) | ciphertext_len (8 BE) | IV (16) | salt (4) | ciphertext

    Used by MKV cross-category embed. The whole envelope is bit-packed
    (k=1) across the LSBs of an animated Mandelbrot frame sequence.

    `pad_to_inner` extends the inner plaintext with deterministic random
    bytes so the resulting ciphertext fills the full carrier capacity
    (every frame has random LSBs throughout, no trailing zero-LSB tell).
    The clear-text `ciphertext_len` field reflects the padded length."""
    import random as _rand
    from . import _stone_crypto as _sc
    inner = _build_inner_plaintext(payload, src_ext)
    if pad_to_inner is not None and pad_to_inner > len(inner):
        seed = int.from_bytes(
            hashlib.sha256(inner + password + b"v3-video-pad").digest()[:8],
            "big")
        rng = _rand.Random(seed)
        inner = inner + rng.randbytes(pad_to_inner - len(inner))
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


# Threshold above which the Mandelbrot v3 PNG/BMP embed switches from the
# in-memory `_build_mandelbrot_image` path to the streaming `_mandelbrot_pixel_iter`
# path. 50 MB is large enough that the per-strip overhead is fully amortized
# but small enough that any payload above this won't blow out RAM by
# materializing a 32K×32K destination pixel array.
_MANDELBROT_STREAMING_THRESHOLD = 50 * 1024 * 1024


def _mandelbrot_pixel_iter(envelope: bytes, width: int, height: int,
                             content_seed: bytes,
                             progress=None,
                             strip_rows: int = 64) -> "Iterator[bytes]":
    """Generator yielding pixel-byte strips for a Mandelbrot v3 output image.

    Streams the destination image strip-by-strip so peak RAM is bounded
    by `(envelope size) + (one strip = strip_rows × width × 3 bytes) +
    (small fractal cap render)` instead of the full `width × height × 3`
    output array. For a 32K×32K output (a ~3 GB pixel array) the streaming
    path keeps everything under ~500 MB total.

    Bit-pack pattern matches `_mandelbrot_pack_envelope_into_fractal`
    exactly — the scatter stride is a function of `total_pixel_bytes` only,
    so the decoder reads what we write regardless of how we chunk the
    work. Each strip:
      1. Renders or upscales the fractal pixels for that row range.
      2. Clears all LSBs in the strip, then ORs payload bits in at the
         scatter positions that fall inside this strip.
      3. Yields the strip's bytes.
      4. Ticks progress.
    """
    import numpy as np
    from . import _mandelbrot as _m

    total_pixel_bytes = width * height * 3
    num_octets = total_pixel_bytes // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
    env_arr = np.frombuffer(envelope, dtype=np.uint8)
    n_env = min(len(env_arr), num_octets)
    stride, _stride_unused = _mandelbrot_scatter_indices(num_octets)
    full_octet_idx = (np.arange(n_env, dtype=np.int64) * stride) % num_octets

    # Compute the small-cap fractal once; nearest-neighbor upscale per
    # strip for images larger than the cap.
    cap = _m._FRACTAL_CAP
    if max(width, height) > cap:
        if width >= height:
            small_w = cap
            small_h = max(1, int(round(cap * height / width)))
        else:
            small_h = cap
            small_w = max(1, int(round(cap * width / height)))
    else:
        small_w, small_h = width, height

    # Match the in-memory path's seed math so streaming and non-streaming
    # outputs of the same source produce the same fractal.
    seed_bytes = (_MANDELBROT_SALT
                  + struct.pack(">II", width, height)
                  + content_seed)
    seed = _m.derive_seed(seed_bytes)
    fractal_small = np.frombuffer(
        _m.generate_keystream(small_w, small_h, seed),
        dtype=np.uint8).reshape(small_h, small_w, 3)

    bytes_per_row = width * 3
    rows_emitted = 0
    while rows_emitted < height:
        chunk_rows = min(strip_rows, height - rows_emitted)
        strip_start_byte = rows_emitted * bytes_per_row
        strip_end_byte = strip_start_byte + chunk_rows * bytes_per_row

        # Nearest-neighbor upscale of the small fractal into the strip.
        out_row_idx = np.arange(rows_emitted, rows_emitted + chunk_rows,
                                  dtype=np.int64)
        small_row_idx = np.clip(
            out_row_idx * small_h // height, 0, small_h - 1)
        out_col_idx = np.arange(width, dtype=np.int64)
        small_col_idx = np.clip(
            out_col_idx * small_w // width, 0, small_w - 1)
        strip_2d = fractal_small[small_row_idx[:, None],
                                   small_col_idx[None, :], :]
        strip = strip_2d.reshape(-1).copy()
        # Clear all LSBs in the strip. Body-heavy fractals (large interior
        # black regions) have natural LSB=0, so leaving them as-is creates
        # a stronger byte-0x00 anomaly than just zeroing everything — the
        # tested fractal-natural alternative scored 208× over uniform vs.
        # ~7× for blanket-zero. See note in `_mandelbrot_pack_envelope_into_fractal`.
        strip &= _FRACTAL_MASK

        # Find which envelope bytes scatter into this strip's octet range.
        strip_start_octet = strip_start_byte // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
        strip_end_octet = strip_end_byte // _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
        in_strip = (full_octet_idx >= strip_start_octet) & (full_octet_idx < strip_end_octet)
        env_indices = np.where(in_strip)[0]

        if env_indices.size > 0:
            octet_idx_local = full_octet_idx[env_indices] - strip_start_octet
            base_local = octet_idx_local * _MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE
            env_bytes = env_arr[env_indices]
            bits = np.unpackbits(env_bytes).reshape(-1, 8)[:, ::-1].reshape(-1)
            offsets = np.arange(_MANDELBROT_PIXEL_BYTES_PER_ENVELOPE_BYTE,
                                  dtype=np.int64)
            dest = (base_local[:, None] + offsets[None, :]).reshape(-1)
            # OR in payload bits at scatter positions (LSBs already cleared).
            strip[dest] = strip[dest] | bits

        yield bytes(strip)
        rows_emitted += chunk_rows
        if progress is not None:
            progress(rows_emitted / height)


def _png_embed_v2_streaming_from_bytes(src_bytes: bytes, src_ext: str,
                                          dst: Path,
                                          password: bytes = b"",
                                          progress=None) -> None:
    """Streaming Mandelbrot v3 PNG output. Use for large payloads where
    materializing the full destination pixel array would blow out RAM.

    Non-scatter pixel-byte LSBs keep fractal-natural values (not zero) so
    sequential LSB extraction doesn't reveal the obvious zero-tail. Filter
    sequence + ancillary chunks emitted by `stream_png_write`."""
    from .streaming_image import stream_png_write
    inner = _build_inner_plaintext(src_bytes, src_ext)
    ENVELOPE_OVERHEAD = len(MAGIC_V3) + 8 + 16 + 4
    envelope_size = ENVELOPE_OVERHEAD + len(inner)
    width, height = _mandelbrot_calc_image_dims(envelope_size, 0)
    envelope = _v3_envelope(src_bytes, src_ext, width, height, password)
    content_seed = hashlib.sha256(envelope[:64 * 1024]).digest()
    pixel_iter = _mandelbrot_pixel_iter(envelope, width, height,
                                           content_seed=content_seed,
                                           progress=progress)
    stream_png_write(dst, width, height, pixel_iter, filter_seed=content_seed)


def _bmp_embed_v2_streaming_from_bytes(src_bytes: bytes, src_ext: str,
                                          dst: Path,
                                          password: bytes = b"",
                                          progress=None) -> None:
    """Streaming Mandelbrot v3 BMP output. See _png_embed_v2_streaming_from_bytes."""
    from .streaming_image import stream_bmp_write
    inner = _build_inner_plaintext(src_bytes, src_ext)
    ENVELOPE_OVERHEAD = len(MAGIC_V3) + 8 + 16 + 4
    envelope_size = ENVELOPE_OVERHEAD + len(inner)
    width, height = _mandelbrot_calc_image_dims(envelope_size, 0)
    envelope = _v3_envelope(src_bytes, src_ext, width, height, password)
    content_seed = hashlib.sha256(envelope[:64 * 1024]).digest()
    pixel_iter = _mandelbrot_pixel_iter(envelope, width, height,
                                           content_seed=content_seed,
                                           progress=progress)
    stream_bmp_write(dst, width, height, pixel_iter)


def _build_mandelbrot_image(src_bytes: bytes, src_ext: str,
                             password: bytes = b"",
                             cancel: Optional["CancellationToken"] = None
                             ) -> "Tuple[int, int, bytes]":
    """Build the bit-packed Mandelbrot image. Returns (width, height,
    pixel_bytes) ready to stream into stream_png_write / stream_bmp_write.

    The envelope is UCMSv3 — encrypted with a key derived from `password`
    (PBKDF2 + AES-256-CTR). Empty password ⇒ default app key (anyone with
    Vitriol can decode). Non-empty password ⇒ only the same password
    decodes the file.

    Each pixel byte's top 7 bits = colored fractal; bottom 1 bit = one
    payload bit. Pixel bytes at non-scatter positions keep their natural
    fractal-derived LSB (NOT zeroed) — that's what defeats the "trailing
    zero LSBs in extracted byte stream" forensic detection signal at zero
    file-size cost.
    """
    # Pre-build the inner plaintext to size the image.
    inner = _build_inner_plaintext(src_bytes, src_ext)
    ENVELOPE_OVERHEAD = len(MAGIC_V3) + 8 + 16 + 4   # = 36 bytes
    envelope_size = ENVELOPE_OVERHEAD + len(inner)
    width, height = _mandelbrot_calc_image_dims(envelope_size, 0)

    # Encrypt and assemble envelope at its natural size — no carrier
    # padding. The bit-pack will leave non-scatter pixel-byte LSBs alone
    # (= fractal-natural LSBs, not zero), which keeps PNG/BMP file sizes
    # close to the pre-detection-hardening baseline while still hiding
    # the obvious "envelope ends here, then zeros forever" tell.
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

PY_HEADER_FIRST_LINE = "# Generated by Vitriol - Philosopher's Stone Mode"

# ---- Runtime constants embedded into the generated .py scripts ----
#
# These are pure Python code blocks written verbatim into the .py files
# Vitriol generates. They run on the END USER's machine when they
# double-click the .py — so they have to be self-contained: stdlib only
# for the plain variant; the encrypted variant uses the `cryptography`
# library and offers to pip-install it if missing.
#
# The generated script structure is:
#   1. Header comments (filename, size, SHA-256, version tag)
#   2. Variable assignments (_filename, _data, and for encrypted:
#      _visible_hash, _iv_hex)
#   3. Runtime block — one of these constants, written verbatim.
#
# Keep these as raw strings. Backslash-escapes (\r, \\) and braces are
# literal — don't run them through .format() or f-strings.

_PY_PBKDF2_SALT_LITERAL = '"transmute-py-stone-v1"'
_PY_PBKDF2_ITER_LITERAL = "200000"

# Plain (no-password) runtime. Just base64-decodes _data into _filename,
# with a Rebuilding-... animated-dots indicator while it runs.
_PY_RUNTIME_PLAIN = r'''
print("Loading...", flush=True)
import base64, os, sys, time, threading

_animation_stop = threading.Event()


def _animate(msg):
    """Cycle '.', '..', '...', '..', '.' on the same console line until
    _animation_stop is set. Pure stdlib carriage-return; no curses, no
    ANSI codes — works in cmd, PowerShell, Windows Terminal, and most
    POSIX shells."""
    seq = [".", "..", "...", "..", "."]
    i = 0
    while not _animation_stop.wait(0.4):
        sys.stdout.write("\r" + msg + seq[i % len(seq)] + "    ")
        sys.stdout.flush()
        i += 1
    sys.stdout.write("\r" + " " * (len(msg) + 8) + "\r")
    sys.stdout.flush()


def _self_delete_top():
    # On Windows, a running .exe holds an exclusive lock on its own
    # file — direct os.unlink fails. Workaround: write a tiny .bat
    # that waits (via ping, since timeout.exe refuses redirected stdin
    # in detached mode) then deletes both the .exe and itself, and
    # spawn it detached so it survives our process exit.
    try:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            import subprocess, tempfile
            target = __file__
            bat_fd, bat_path = tempfile.mkstemp(suffix=".bat")
            os.close(bat_fd)
            with open(bat_path, "w", encoding="ascii") as _bat:
                _bat.write("@echo off\r\n")
                _bat.write("ping 127.0.0.1 -n 2 >nul\r\n")
                _bat.write('del /q "%s"\r\n' % target)
                _bat.write('(goto) 2>nul & del /q "%s"\r\n' % bat_path)
            subprocess.Popen(
                ["cmd", "/c", "start", "/b", "", "cmd", "/c", bat_path],
                creationflags=0x00000008 | 0x00000200,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            os.unlink(__file__)
    except (OSError, NameError):
        pass


def main():
    msg = "Rebuilding " + _filename + ". This could take a while"
    print(msg + "...")
    t0 = time.time()
    animator = threading.Thread(target=_animate, args=(msg,), daemon=True)
    animator.start()
    try:
        payload = base64.b64decode(_data)
        with open(_filename, "wb") as f:
            f.write(payload)
        _animation_stop.set()
        animator.join(timeout=1)
        elapsed = time.time() - t0
        print("Done in %.1fs." % elapsed)
    finally:
        _animation_stop.set()
    _self_delete_top()


main()
'''

# Inner runtime body — gets AES-CTR-encrypted with the password-derived
# key BEFORE being embedded in the generated .py. End users opening the
# .py in notepad see only the base64'd ciphertext of this code, not the
# code itself. The loader stub at the end of the .py decrypts and execs
# this on a successful password match.
#
# Globals available to this code (provided by the loader stub via exec
# globals): _filename, _visible_hash, _iv_hex, _data, _pw_key, _file_id,
# _registry_clear_attempts (function reference). All standard library
# imports are also available since the stub already imported them.
_PY_VISIBLE_DECOY_FILENAME = "extracted.bin"

_PY_INNER_RUNTIME = r'''
_expected_prefix_hash = "___PREFIX_HASH_PLACEHOLDER___"


def _check_self_integrity():
    import hashlib as _hl
    try:
        with open(__file__, "rb") as _f:
            _src = _f.read()
    except (OSError, NameError):
        sys.exit(1)
    _start_marker = b"_runtime = (\n"
    _end_marker = b"\n)\n"
    _s = _src.find(_start_marker)
    if _s < 0:
        sys.exit(1)
    _e = _src.find(_end_marker, _s)
    if _e < 0:
        sys.exit(1)
    _e += len(_end_marker)
    _h = _hl.sha256()
    _h.update(_src[:_s])
    _h.update(_src[_e:])
    if _h.hexdigest() != _expected_prefix_hash:
        sys.exit(1)


_check_self_integrity()


def _animate(msg):
    seq = [".", "..", "...", "..", "."]
    i = 0
    while not _animation_stop.wait(0.4):
        sys.stdout.write("\r" + msg + seq[i % len(seq)] + "    ")
        sys.stdout.flush()
        i += 1
    sys.stdout.write("\r" + " " * (len(msg) + 8) + "\r")
    sys.stdout.flush()


def _self_delete_inner():
    try:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            import subprocess as _sp, tempfile as _tf
            _target = __file__
            _bat_fd, _bat_path = _tf.mkstemp(suffix=".bat")
            os.close(_bat_fd)
            with open(_bat_path, "w", encoding="ascii") as _bat:
                _bat.write("@echo off\r\n")
                _bat.write("ping 127.0.0.1 -n 2 >nul\r\n")
                _bat.write('del /q "%s"\r\n' % _target)
                _bat.write('(goto) 2>nul & del /q "%s"\r\n' % _bat_path)
            _sp.Popen(
                ["cmd", "/c", "start", "/b", "", "cmd", "/c", _bat_path],
                creationflags=0x00000008 | 0x00000200,
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                close_fds=True,
            )
        else:
            os.unlink(__file__)
    except (OSError, NameError):
        pass


def _inner_main():
    import struct as _st
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    iv = bytes.fromhex(_iv_hex)
    expected_hash = bytes.fromhex(_visible_hash)

    cipher = Cipher(algorithms.AES(_pw_key), modes.CTR(iv),
                     backend=default_backend()).decryptor()

    # Encrypted layout (sequential):
    #   [0..32)        plaintext SHA-256 of payload (= _visible_hash)
    #   [32..34)       big-endian uint16 filename length
    #   [34..34+N)     real filename, UTF-8
    #   [34+N..)       payload bytes
    full_ciphertext = base64.b64decode(_data)

    candidate_hash = cipher.update(full_ciphertext[:32])
    if candidate_hash != expected_hash:
        print("Rebuild failed (data integrity check).")
        sys.exit(1)

    fname_len = _st.unpack(">H", cipher.update(full_ciphertext[32:34]))[0]
    real_filename = cipher.update(full_ciphertext[34:34 + fname_len]).decode(
        "utf-8", errors="replace")

    encrypted_payload = full_ciphertext[34 + fname_len:]
    msg = "Rebuilding " + real_filename + ". This could take a while"
    print(msg + "...")
    t0 = time.time()
    animator = threading.Thread(target=_animate, args=(msg,), daemon=True)
    animator.start()
    try:
        plaintext = cipher.update(encrypted_payload) + cipher.finalize()
        # Re-hash the decrypted payload and compare to the embedded hash.
        # Catches any tamper that slipped past the prefix-hash check at
        # the loader (i.e. modifications to the payload section of _data
        # that left the first 32 bytes intact). If anyone tampered with
        # the file at all, this check fails and the file is never written.
        import hashlib as _hl_check
        if _hl_check.sha256(plaintext).digest() != expected_hash:
            print("Rebuild failed (payload integrity check).")
            sys.exit(1)
        with open(real_filename, "wb") as f:
            f.write(plaintext)
        _animation_stop.set()
        animator.join(timeout=1)
        elapsed = time.time() - t0
        print("Done in %.1fs." % elapsed)
        _registry_clear_attempts(_file_id)
    finally:
        _animation_stop.set()
    _self_delete_inner()


_inner_main()
'''


# Encrypted-loader stub for password-protected .py output. Visible in the
# generated file: password prompt, attempt counter, AES-CTR decryption of
# the embedded `_PY_INNER_RUNTIME` ciphertext. Inner runtime stays opaque
# until the right password unlocks it.
_PY_RUNTIME_ENCRYPTED = r'''
print("Loading...", flush=True)
import base64, os, sys, time, threading, hashlib, subprocess, getpass

# PBKDF2 parameters match Stone-v3 envelope crypto in _stone_crypto.py.
_PBKDF2_SALT = b"transmute-stone-v3"
_PBKDF2_ITER = 200000

# Per-file failed-attempt counter location. Path/basename encoded as
# byte-list literals so a grep over the generated .py for the obvious
# strings finds nothing.
_REGISTRY_KEY = bytes([83,111,102,116,119,97,114,101,92,95,56,55,
                        50,54,55,54,56,56,51]).decode("ascii")
_POSIX_STORE_BASENAME = bytes([46,56,55,50,54,55,54,56,56,51]).decode("ascii")
_MAX_ATTEMPTS = 0x05


def _ensure_cryptography():
    """Install the `cryptography` library if missing — required for AES-CTR
    decrypt at runtime."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        pass
    print("This script needs the 'cryptography' Python library to decrypt.")
    response = input("Install it now via pip? [Y/n]: ").strip().lower()
    if response and response[0] != "y":
        print("Cannot proceed without 'cryptography'. Exiting.")
        return False
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                                "cryptography"])
        return True
    except subprocess.CalledProcessError as exc:
        print("pip install failed: %s" % exc)
        return False


# Cross-platform attempt-counter storage. Stored DWORD is a magic value
# derived as HMAC(file_id, counter_byte)[:4]; values outside the magic
# set are treated as locked-out. Per-file HMAC key prevents cross-file
# reuse.


def _file_id_to_hmac_key(file_id):
    """Return a per-file HMAC key derived from the file_id string."""
    return file_id.encode("ascii", errors="replace")


def _magic_for_count(file_id, count):
    """Compute the 32-bit magic stored in the counter for `count`."""
    import hmac as _hmac
    h = _hmac.new(_file_id_to_hmac_key(file_id), bytes([count & 0xFF]), "sha256")
    return int.from_bytes(h.digest()[:4], "big")


def _count_for_magic(file_id, magic):
    """Inverse of `_magic_for_count`. Returns _MAX_ATTEMPTS for any
    unknown value."""
    for c in range(_MAX_ATTEMPTS + 1):
        if _magic_for_count(file_id, c) == magic:
            return c
    return _MAX_ATTEMPTS


def _attempts_storage_path():
    """Hidden file at user's home dir for POSIX. Name doesn't reference
    Vitriol — looks like a generic cache file."""
    import pathlib
    return pathlib.Path.home() / _POSIX_STORE_BASENAME


def _load_attempts_dict():
    """POSIX read. JSON dict mapping file_id → magic value (NOT the
    raw counter — same obfuscation as the registry path)."""
    import json
    p = _attempts_storage_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, FileNotFoundError, ValueError):
        pass
    return {}


def _save_attempts_dict(data):
    import json
    p = _attempts_storage_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


def _registry_get_attempts(file_id):
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as k:
                v, _ = winreg.QueryValueEx(k, file_id)
                return _count_for_magic(file_id, int(v))
        except (OSError, FileNotFoundError):
            return 0
    raw = _load_attempts_dict().get(file_id)
    if raw is None:
        return 0
    return _count_for_magic(file_id, int(raw))


def _registry_set_attempts(file_id, n):
    magic = _magic_for_count(file_id, int(n))
    if os.name == "nt":
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY) as k:
                winreg.SetValueEx(k, file_id, 0, winreg.REG_DWORD, magic)
        except OSError:
            pass
        return
    data = _load_attempts_dict()
    data[file_id] = magic
    _save_attempts_dict(data)


def _registry_clear_attempts(file_id):
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REGISTRY_KEY, 0,
                                  winreg.KEY_ALL_ACCESS) as k:
                try:
                    winreg.DeleteValue(k, file_id)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        return
    data = _load_attempts_dict()
    data.pop(file_id, None)
    _save_attempts_dict(data)


_animation_stop = threading.Event()


def _self_delete():
    # Frozen .exe on Windows holds an exclusive file lock. Spawn a
    # detached .bat that waits via ping (timeout.exe refuses redirected
    # stdin) then deletes both the .exe and itself.
    try:
        if sys.platform == "win32" and getattr(sys, "frozen", False):
            import subprocess as _sp, tempfile as _tf
            _target = __file__
            _bat_fd, _bat_path = _tf.mkstemp(suffix=".bat")
            os.close(_bat_fd)
            with open(_bat_path, "w", encoding="ascii") as _bat:
                _bat.write("@echo off\r\n")
                _bat.write("ping 127.0.0.1 -n 2 >nul\r\n")
                _bat.write('del /q "%s"\r\n' % _target)
                _bat.write('(goto) 2>nul & del /q "%s"\r\n' % _bat_path)
            _sp.Popen(
                ["cmd", "/c", "start", "/b", "", "cmd", "/c", _bat_path],
                creationflags=0x00000008 | 0x00000200,
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                close_fds=True,
            )
        else:
            os.unlink(__file__)
    except (OSError, NameError):
        pass


def main():
    # Immediate visible feedback so the console isn't blank during the
    # cryptography import (~1-2s normally, longer with AV scanning).
    # These two lines appear within ms of launch — user sees activity
    # before the password prompt arrives.
    print("Rebuilding ...", flush=True)
    print("... This could take a while", flush=True)
    if not _ensure_cryptography():
        sys.exit(1)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    # File identifier: middle 16 hex chars of the visible SHA-256.
    file_id = _visible_hash[24:40]
    attempts = _registry_get_attempts(file_id)
    if attempts >= _MAX_ATTEMPTS:
        print("Too many failed attempts. This file has been invalidated.")
        _self_delete()
        sys.exit(1)

    # Write the prompt to stdout EXPLICITLY before reading the password.
    #
    # Why: getpass.getpass() on Windows uses msvcrt.putwch() to write the
    # prompt directly to the console screen buffer via Win32 APIs — NOT
    # through stdout. On some Windows console configurations (ConPTY,
    # newer Windows Terminal, certain conhost setups, PyInstaller-bundled
    # consoles) those msvcrt writes don't render visibly, so the user
    # sees a blank console with no prompt and the script appears hung.
    #
    # By writing "Password: " to stdout ourselves first, the prompt is
    # guaranteed visible regardless of msvcrt's behavior. We then read
    # the password — using getpass with an empty prompt for echo
    # suppression on a real TTY, or plain input() when stdin is
    # redirected (where msvcrt would hang waiting for a console).
    sys.stdout.write("Password: ")
    sys.stdout.flush()
    try:
        is_tty = sys.stdin.isatty()
    except Exception:
        is_tty = True
    if is_tty:
        try:
            password = getpass.getpass("")
        except Exception:
            # Last-resort fallback if getpass fails in this environment.
            password = input("")
    else:
        password = input("")
    pw_bytes = password.encode("utf-8")
    pw_key = hashlib.pbkdf2_hmac("sha256", pw_bytes, _PBKDF2_SALT, _PBKDF2_ITER)

    # Password verification via hash check.
    #
    # The first 32 bytes of _data are the SHA-256 of the original payload,
    # encrypted with the password-derived key. If the password is correct,
    # decrypting those 32 bytes yields the same hash that's already
    # plaintext-visible as _visible_hash. If the password is wrong,
    # decryption produces garbage that won't match. No separate sentinel
    # constant needed.
    iv = bytes.fromhex(_iv_hex)
    expected_hash = bytes.fromhex(_visible_hash)
    data_ct = base64.b64decode(_data)
    data_cipher = Cipher(algorithms.AES(pw_key), modes.CTR(iv),
                          backend=default_backend()).decryptor()
    candidate_hash = data_cipher.update(data_ct[:32])

    if candidate_hash != expected_hash:
        # Wrong password (or tampered _data prefix). Counter increments;
        # generic message; on the 5th wrong, .py self-deletes.
        attempts += 1
        _registry_set_attempts(file_id, attempts)
        if attempts >= _MAX_ATTEMPTS:
            print("Wrong password. %d/%d attempts used. File deleted."
                   % (attempts, _MAX_ATTEMPTS))
            _self_delete()
        else:
            print("Wrong password. %d/%d attempts used."
                   % (attempts, _MAX_ATTEMPTS))
        sys.exit(1)

    # Password correct. Decrypt the inner runtime and exec it. The inner
    # runtime constructs its own fresh cipher to re-decrypt _data from
    # the start (microsecond perf cost — simpler than carrying cipher
    # state across the exec boundary).
    rt_iv = bytes.fromhex(_runtime_iv_hex)
    rt_ct = base64.b64decode(_runtime)
    rt_cipher = Cipher(algorithms.AES(pw_key), modes.CTR(rt_iv),
                        backend=default_backend()).decryptor()
    rt_src = rt_cipher.update(rt_ct) + rt_cipher.finalize()

    inner_globals = dict(globals())
    inner_globals["_pw_key"] = pw_key
    inner_globals["_file_id"] = file_id
    exec(compile(rt_src, "<runtime>", "exec"), inner_globals)


main()
'''


def _strip_py_for_minimal_output(src: str) -> str:
    """Remove every comment and docstring from a Python source string,
    preserving execution semantics. Used to strip the runtime templates
    before embedding them in a generated .py — so the resulting script
    has zero developer-comments / docstrings to help anyone reading it
    in notepad understand the structure or attack surface.

    Implementation: parse to AST, walk the tree removing docstring nodes
    (the first stmt of a module/function/class body when it's a string
    literal Expr), then `ast.unparse` to regenerate source. Comments
    are discarded by the parser; docstrings are removed explicitly. The
    output is normalized Python with no developer-facing prose."""
    import ast
    tree = ast.parse(src)
    # Drop docstrings from every block-level scope.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef, ast.Module)):
            body = getattr(node, "body", None)
            if (body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:]
                if not node.body:
                    # Empty body would be a SyntaxError on unparse.
                    node.body = [ast.Pass()]
    return ast.unparse(tree) + "\n"


def _py_embed_to_file(src_path: Path, src_ext: str, dst: Path,
                       cancel: Optional["CancellationToken"] = None,
                       src_filename: Optional[str] = None,
                       password: bytes = b"") -> None:
    """Generate a self-extracting Python script. Streams source bytes through
    base64 in 3-byte input chunks (4 base64 chars out) so memory stays bounded
    even for huge sources. Output script splits the base64 into 4096-char
    string literals concatenated by Python's adjacent-literal joining.

    `src_filename` (when provided) overrides what name appears in the script
    header AND what the script writes when it runs.

    `password` (when non-empty) switches to the ENCRYPTED variant: source
    bytes are AES-256-CTR encrypted under a PBKDF2-derived key from the
    password. The generated .py prompts the user for a password at run
    time, verifies it via an embedded encrypted-hash field, then decrypts
    + writes the file. After 5 failed attempts (tracked in the Windows
    registry) the .py self-deletes without producing the file.

    The generated script always ends with a self-delete: once the
    original file is reconstructed (or after 5 failed unlocks), the .py
    `os.unlink(__file__)`s itself so it doesn't litter the filesystem.
    """
    if password:
        _py_embed_to_file_encrypted(src_path, src_ext, dst, password,
                                      cancel=cancel, src_filename=src_filename)
    else:
        _py_embed_to_file_plain(src_path, src_ext, dst,
                                  cancel=cancel, src_filename=src_filename)


# Stone .py v4 — opaque-bootstrap format for encrypted .py outputs.
#
# Layout (Python source):
#   _v4 = "<single base64 blob>"      # parses as one string, fast
#   ... # == VITRIOL STONE v4 ==
#   <visible bootstrap ~120 lines>
#
# Decoded blob:
#   magic        b"VTSv4\0"  6 B
#   file_id      8 B          counter HMAC key
#   iv_outer    16 B          AES-CTR IV for outer runtime
#   iv_payload  16 B          AES-CTR IV for payload
#   visible_hash 32 B         SHA-256 of plaintext payload
#   outer_hash   32 B         SHA-256 of plaintext outer source (password verify)
#   outer_size    4 B uint32 LE
#   payload_size  8 B uint64 LE
#   outer_ct    <outer_size> B
#   payload_ct  <payload_size> B   (filename header + raw bytes)
#
# Visible bootstrap: imports, counter, password prompt, outer-runtime
# decrypt+hash-verify, exec. Everything else (payload decrypt, SHA-256
# tamper check, tamper response, filename parse, file write, success
# clear) is opaque inside outer_ct.
#
# .exe path uses v3 inside the PyInstaller stub — bundle opacity makes
# v4 redundant there.

_V4_MAGIC = b"VTSv4\0"
_V4_HEADER_FMT = "<6s8s16s16s32s32sIQ"
_V4_HEADER_LEN = 6 + 8 + 16 + 16 + 32 + 32 + 4 + 8   # = 122
_V4_SOURCE_MARKER = "# == VITRIOL STONE v4 =="
_V4_VARIABLE_NAME = "_v4"


# Outer runtime — AES-CTR-encrypted before embedding. Bootstrap decrypts
# and execs on password match.
#
# Globals provided by bootstrap exec:
#   _key, _iv_payload, _visible_hash, _payload_ct, _file_id,
#   _attempts_set(file_id, n), _self_delete(), _MAX
_PY_OUTER_RUNTIME_V4 = r'''
import struct as _st
import hashlib as _hl
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Decrypt the payload blob: [filename_len 2B BE][filename UTF-8][payload]
_dec = Cipher(algorithms.AES(_key), modes.CTR(_iv_payload),
              backend=default_backend()).decryptor()
_pt = _dec.update(_payload_ct) + _dec.finalize()
if len(_pt) < 2:
    print("File integrity check failed. File deleted.")
    _attempts_set(_file_id, _MAX)
    _self_delete()
    raise SystemExit(1)
_fl = _st.unpack(">H", _pt[:2])[0]
if 2 + _fl > len(_pt) or _fl > 4096:
    print("File integrity check failed. File deleted.")
    _attempts_set(_file_id, _MAX)
    _self_delete()
    raise SystemExit(1)
_real_filename = _pt[2:2 + _fl].decode("utf-8", errors="replace")
_payload = _pt[2 + _fl:]

# Hash check — REQUIRED. If the decrypted payload doesn't match the
# embedded SHA-256, the file was tampered with. Tamper response: lock
# the counter to MAX (so any preserved copies are invalidated) and
# self-delete this copy. Generic message — doesn't leak which check
# failed (matches the no-oracle property of the rest of Stone mode).
if _hl.sha256(_payload).digest() != _visible_hash:
    print("File integrity check failed. File deleted.")
    _attempts_set(_file_id, _MAX)
    _self_delete()
    raise SystemExit(1)

# Right password AND intact payload — write the file.
import time as _time
_t0 = _time.time()
print("Rebuilding " + _real_filename + "...", flush=True)
with open(_real_filename, "wb") as _f:
    _f.write(_payload)
print("Done in %.1fs." % (_time.time() - _t0))

# Success: clear the failed-attempt counter for this file (so a future
# legitimate run after some prior wrong-password attempts doesn't carry
# the strikes forward).
_attempts_set(_file_id, 0)

# Self-delete the .py — same one-shot pattern as v3.
_self_delete()
'''


# Bootstrap — the visible portion of an encrypted v4 .py. Compact (~120
# lines), reveals only that it's a self-extracting password-protected
# script with a counter mechanism. Does NOT reveal the actual decrypt
# logic, the tamper response, or the payload structure (those live
# encrypted in `_v4`'s outer_ct).
#
# Two values get .format()-substituted at build time:
#   {V4VAR}     name of the source variable that holds the base64 blob
#   {SENTINEL}  human-visible "# == VITRIOL STONE v4 ==" marker
# Everything else is literal — adjacent {{ and }} are escaped braces
# (Python format placeholder mechanism).
_PY_BOOTSTRAP_V4 = r'''
import sys, os, hashlib, getpass, base64, struct

# Decode the embedded blob. The Python parser sees `{V4VAR}` as ONE
# string token regardless of payload size — drops parse-compile from
# ~10s to <100ms compared to the v3 tuple-of-thousands-of-strings format.
_blob = base64.b64decode({V4VAR})
if _blob[:6] != b"VTSv4\0":
    sys.exit(1)
_file_id = _blob[6:14].hex()
_iv_outer    = _blob[14:30]
_iv_payload  = _blob[30:46]
_visible_hash= _blob[46:78]
_outer_hash  = _blob[78:110]
_outer_sz    = struct.unpack("<I", _blob[110:114])[0]
_payload_sz  = struct.unpack("<Q", _blob[114:122])[0]
_off         = 122
_outer_ct    = _blob[_off:_off + _outer_sz]; _off += _outer_sz
_payload_ct  = _blob[_off:_off + _payload_sz]

# --- Counter mechanism (visible — has to gate before any decrypt) ----
# Storage location is NOT named after Vitriol — looks like a generic
# system entry. Value is HMAC-magic-encoded so a manual regedit or JSON
# inspection doesn't reveal the raw counter (and editing it to 0 locks
# out instead of resetting). Same design as v3.
_REG_KEY = bytes([83,111,102,116,119,97,114,101,92,95,56,55,
                  50,54,55,54,56,56,51]).decode("ascii")
_POSIX_NAME = bytes([46,56,55,50,54,55,54,56,56,51]).decode("ascii")
_MAX = 5

def _hkey(fid): return fid.encode("ascii", errors="replace")

def _magic(fid, n):
    import hmac as _h
    return int.from_bytes(_h.new(_hkey(fid), bytes([n & 0xFF]),
                                 "sha256").digest()[:4], "big")

def _count(fid, m):
    for c in range(_MAX + 1):
        if _magic(fid, c) == m:
            return c
    return _MAX

def _attempts_get(fid):
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
                v, _ = winreg.QueryValueEx(k, fid)
                return _count(fid, int(v))
        except OSError:
            return 0
    import json, pathlib
    try:
        d = json.loads((pathlib.Path.home() / _POSIX_NAME).read_text("utf-8"))
        return _count(fid, int(d.get(fid, 0)))
    except (OSError, ValueError):
        return 0

def _attempts_set(fid, n):
    m = _magic(fid, int(n))
    if os.name == "nt":
        try:
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
                winreg.SetValueEx(k, fid, 0, winreg.REG_DWORD, m)
        except OSError:
            pass
        return
    import json, pathlib
    p = pathlib.Path.home() / _POSIX_NAME
    try:
        d = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        d = {{}}
    if not isinstance(d, dict):
        d = {{}}
    d[fid] = m
    try:
        p.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass

def _self_delete():
    try:
        if sys.platform == "win32":
            import subprocess as _sp, tempfile as _tf
            _bf, _bp = _tf.mkstemp(suffix=".bat"); os.close(_bf)
            with open(_bp, "w", encoding="ascii") as _b:
                _b.write("@echo off\r\nping 127.0.0.1 -n 2 >nul\r\n")
                _b.write('del /q "%s"\r\n' % __file__)
                _b.write('(goto) 2>nul & del /q "%s"\r\n' % _bp)
            _sp.Popen(["cmd", "/c", "start", "/b", "", "cmd", "/c", _bp],
                      creationflags=0x08 | 0x200, stdin=_sp.DEVNULL,
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, close_fds=True)
        else:
            os.unlink(__file__)
    except OSError:
        pass

# --- Pre-flight: locked out? ----------------------------------------
_attempts = _attempts_get(_file_id)
if _attempts >= _MAX:
    print("Too many failed attempts. This file has been invalidated.")
    _self_delete()
    sys.exit(1)

# --- Prompt ----------------------------------------------------------
print("Loading...", flush=True)
print("Rebuilding ...", flush=True)
print("... This could take a while", flush=True)
sys.stdout.write("Password: "); sys.stdout.flush()
try:
    _is_tty = sys.stdin.isatty()
except Exception:
    _is_tty = True
try:
    _pw = getpass.getpass("") if _is_tty else input("")
except Exception:
    _pw = input("")

# --- Crypto: derive key, decrypt outer runtime, hash-check -----------
try:
    import cryptography  # noqa: F401
except ImportError:
    print("This script needs the 'cryptography' Python library to decrypt.")
    _resp = input("Install it now via pip? [Y/n]: ").strip().lower()
    if _resp and _resp[0] != "y":
        print("Cannot proceed without 'cryptography'. Exiting.")
        sys.exit(1)
    import subprocess as _sp
    try:
        _sp.check_call([sys.executable, "-m", "pip", "install", "cryptography"])
    except Exception:
        print("pip install failed. Cannot proceed.")
        sys.exit(1)
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

_key = hashlib.pbkdf2_hmac("sha256", _pw.encode("utf-8"),
                           b"transmute-stone-v3", 200000)
_dec = Cipher(algorithms.AES(_key), modes.CTR(_iv_outer),
              backend=default_backend()).decryptor()
_outer_pt = _dec.update(_outer_ct) + _dec.finalize()

if hashlib.sha256(_outer_pt).digest() != _outer_hash:
    # Wrong password (or tampered outer_ct). Generic message; counter
    # increments; on the 5th failed attempt the .py self-deletes.
    _attempts_set(_file_id, _attempts + 1)
    print("Wrong password. %d/%d attempts used." % (_attempts + 1, _MAX))
    if _attempts + 1 >= _MAX:
        print("File deleted.")
        _self_delete()
    sys.exit(1)

# Right password — hand off to the (encrypted-until-now) outer runtime.
exec(compile(_outer_pt, "<vitriol-outer>", "exec"), {{
    "_key": _key,
    "_iv_payload": _iv_payload,
    "_visible_hash": _visible_hash,
    "_payload_ct": _payload_ct,
    "_file_id": _file_id,
    "_attempts_set": _attempts_set,
    "_self_delete": _self_delete,
    "_MAX": _MAX,
    "__file__": __file__,
}})
'''


def _build_py_source_encrypted_v4(src_path: Path, src_ext: str,
                                    password: bytes,
                                    cancel: Optional["CancellationToken"] = None,
                                    src_filename: Optional[str] = None) -> bytes:
    """Build a v4-format encrypted self-extracting .py and return its bytes.

    Wire format on disk (UTF-8 source):

        _v4 = "<single base64 string of binary blob>"
        # == VITRIOL STONE v4 ==
        <bootstrap Python — visible, ~120 lines>

    The binary blob (post-base64-decode) holds magic + IVs + hashes +
    sizes + outer-runtime ciphertext + payload ciphertext. See
    `_PY_OUTER_RUNTIME_V4` and `_PY_BOOTSTRAP_V4` for the field layout
    that's parsed at runtime.

    Used by `_py_embed_to_file_encrypted` for .py outputs. The .exe
    path uses the older `_build_py_source_encrypted` because the
    PyInstaller stub already provides opacity via its binary wrapper —
    no benefit from re-doing the work in v4 format inside the stub's
    embedded payload.
    """
    from . import _stone_crypto as _sc

    if src_filename is None:
        src_filename = src_path.name

    # Pass 1 — hash plaintext for visible_hash (32 bytes) and load the
    # full plaintext into a buffer so we can encrypt it as one CTR stream
    # below. For very large payloads this could be streamed instead;
    # current implementation loads into memory which matches v3's pattern.
    sha = hashlib.sha256()
    with open(src_path, "rb") as f:
        plaintext = f.read()
    if cancel is not None:
        cancel.check()
    sha.update(plaintext)
    visible_hash = sha.digest()

    # Build the payload plaintext: [filename_len 2B BE][filename UTF-8][raw payload]
    real_fname_bytes = src_filename.encode("utf-8")[:65535]
    payload_pt = (struct.pack(">H", len(real_fname_bytes))
                  + real_fname_bytes
                  + plaintext)

    # Generate cryptographically random per-file IDs / IVs. file_id
    # functions as the HMAC key for the counter magic — different per
    # file means cross-file counter inspection reveals nothing.
    import secrets
    file_id = secrets.token_bytes(8)
    iv_outer = secrets.token_bytes(16)
    iv_payload = secrets.token_bytes(16)

    key = _sc.derive_key(password)

    # Encrypt the payload (filename + raw bytes).
    enc_pay = _sc.StreamingEncryptor(key, iv_payload)
    payload_ct = enc_pay.update(payload_pt) + enc_pay.finalize()

    # Encrypt the outer runtime — strip comments/docstrings first so the
    # encrypted form is the minimal bytecode-equivalent source. Hash the
    # plaintext source for the password-verify token.
    outer_pt_str = _strip_py_for_minimal_output(_PY_OUTER_RUNTIME_V4)
    outer_pt = outer_pt_str.encode("utf-8")
    outer_hash = hashlib.sha256(outer_pt).digest()
    enc_outer = _sc.StreamingEncryptor(key, iv_outer)
    outer_ct = enc_outer.update(outer_pt) + enc_outer.finalize()

    # Pack the binary blob. Format must match what `_PY_BOOTSTRAP_V4`
    # parses at runtime (and what `_py_extract_to_file` parses for
    # Vitriol-side drop-in extraction).
    header = struct.pack(_V4_HEADER_FMT,
                          _V4_MAGIC, file_id, iv_outer, iv_payload,
                          visible_hash, outer_hash,
                          len(outer_ct), len(payload_ct))
    blob = header + outer_ct + payload_ct
    blob_b64 = base64.b64encode(blob).decode("ascii")

    # Compose the source. The base64 string sits on a single line as
    # one Python literal — Python's lexer reads it as ONE STRING token
    # regardless of size, instead of v3's tuple of thousands of literals
    # which forced the parser to do thousands of token-concat operations.
    bootstrap_src = _PY_BOOTSTRAP_V4.format(
        V4VAR=_V4_VARIABLE_NAME,
        SENTINEL=_V4_SOURCE_MARKER,
    )
    bootstrap_src = _strip_py_for_minimal_output(bootstrap_src)

    out_lines = [
        f'{_V4_VARIABLE_NAME} = "{blob_b64}"',
        _V4_SOURCE_MARKER,
        bootstrap_src.rstrip() + "\n",
    ]
    return ("\n".join(out_lines)).encode("utf-8")


def _build_py_source_plain(src_path: Path,
                             cancel: Optional["CancellationToken"] = None,
                             src_filename: Optional[str] = None) -> bytes:
    """Build the bytes of a plain (no-password) self-extracting .py and
    return them. Used by `_py_embed_to_file_plain` (which writes the
    bytes to a .py file on disk) and by `_exe_embed_to_file` (which
    appends the bytes after a stub.exe + magic marker so the same
    runtime executes inside a frozen .exe).
    """
    if src_filename is None:
        src_filename = src_path.name
    chunk_chars = 4096
    chunk_bytes_in = (chunk_chars // 4) * 3
    import io as _io
    out = _io.StringIO()
    out.write(f"_filename = {src_filename!r}\n")
    out.write("_data = (\n")
    first = True
    with open(src_path, "rb") as f:
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
    out.write(_strip_py_for_minimal_output(_PY_RUNTIME_PLAIN))
    return out.getvalue().encode("utf-8")


def _py_embed_to_file_plain(src_path: Path, src_ext: str, dst: Path,
                              cancel: Optional["CancellationToken"] = None,
                              src_filename: Optional[str] = None) -> None:
    """Plain (no-password) self-extracting .py. Source bytes are base64'd
    in 4096-char literal chunks. At runtime the script base64-decodes
    `_data` into `_filename`, prints a "Rebuilding ..." message with
    animated dots, and self-deletes."""
    src_bytes = _build_py_source_plain(src_path, cancel=cancel,
                                          src_filename=src_filename)
    with open(dst, "wb") as out:
        out.write(src_bytes)


def _build_py_source_encrypted(src_path: Path, src_ext: str, password: bytes,
                                  cancel: Optional["CancellationToken"] = None,
                                  src_filename: Optional[str] = None,
                                  prefix_extra_bytes: bytes = b"") -> bytes:
    """Build the bytes of an encrypted self-extracting .py and return them.

    `prefix_extra_bytes`: bytes that will appear in the file BEFORE this
    .py source at runtime — relevant for the `.exe` self-extractor, where
    the file on disk is `[stub.exe bytes][magic][.py source]`. The runtime
    self-hash check hashes everything in the file except the `_runtime =
    (...)` block, so the hash baked into the inner runtime must include
    this prefix. For a normal .py target, prefix_extra_bytes is empty.

    Used by `_py_embed_to_file_encrypted` (writes bytes to .py on disk)
    and `_exe_embed_to_file` (writes prefix + bytes to .exe on disk).
    """
    from . import _stone_crypto as _sc
    if src_filename is None:
        src_filename = src_path.name
    # Pass 1: hash plaintext for the visible-hash field AND assemble the
    # IV input. We need the plaintext SHA-256 to prepend to the cipher
    # input, which means we have to compute it before encrypting.
    sha = hashlib.sha256()
    with open(src_path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK_SIZE), b""):
            sha.update(buf)
            if cancel is not None:
                cancel.check()
    plaintext_hash = sha.digest()
    visible_hash_hex = plaintext_hash.hex()

    # Derive key + IV. Use the same StreamingIVHasher trick from the
    # MKV path so we don't need to load plaintext into RAM: feed
    # plaintext_hash || (streamed source bytes) to the HMAC.
    key = _sc.derive_key(password)
    iv_hasher = _sc.StreamingIVHasher(key)
    iv_hasher.update(plaintext_hash)
    with open(src_path, "rb") as f:
        for buf in iter(lambda: f.read(CHUNK_SIZE), b""):
            iv_hasher.update(buf)
            if cancel is not None:
                cancel.check()
    iv = iv_hasher.iv()

    # Pass 2: stream-encrypt the data into an in-memory buffer so we
    # can compose the .py contents in pieces (needed for the self-hash
    # check in the inner runtime — see below).
    enc = _sc.StreamingEncryptor(key, iv)
    chunk_chars = 4096
    chunk_bytes_in = (chunk_chars // 4) * 3
    import io as _io
    data_buf = _io.StringIO()
    cipher_buf = bytearray()
    # Encrypt the prefix: plaintext_hash || filename_len(2 BE) || filename
    # The visible `_filename` in the .py is a decoy; the real filename
    # only emerges after decryption + the inner runtime parses this prefix.
    real_fname_bytes = src_filename.encode("utf-8")[:65535]
    fname_prefix = (plaintext_hash
                     + struct.pack(">H", len(real_fname_bytes))
                     + real_fname_bytes)
    cipher_buf.extend(enc.update(fname_prefix))
    first = True

    def _flush_complete_lines() -> None:
        nonlocal first
        while len(cipher_buf) >= chunk_bytes_in:
            raw = bytes(cipher_buf[:chunk_bytes_in])
            del cipher_buf[:chunk_bytes_in]
            b64 = base64.b64encode(raw).decode("ascii")
            sep = "" if first else "\n"
            data_buf.write(f'{sep}    "{b64}"')
            first = False

    _flush_complete_lines()
    with open(src_path, "rb") as f:
        while True:
            if cancel is not None:
                cancel.check()
            raw = f.read(CHUNK_SIZE)
            if not raw:
                break
            cipher_buf.extend(enc.update(raw))
            _flush_complete_lines()
    cipher_buf.extend(enc.finalize())
    if cipher_buf:
        b64 = base64.b64encode(bytes(cipher_buf)).decode("ascii")
        sep = "" if first else "\n"
        data_buf.write(f'{sep}    "{b64}"')

    # Compute the runtime IV — derived from the AES key + a fixed
    # string + the visible-hash so different files get different IVs
    # even under the same password.
    rt_iv_hasher = _sc.StreamingIVHasher(key)
    rt_iv_hasher.update(b"transmute-py-runtime-iv:")
    rt_iv_hasher.update(visible_hash_hex.encode("ascii"))
    rt_iv = rt_iv_hasher.iv()

    # Compose Piece 1 (everything written BEFORE the `_runtime = (`
    # line) and Piece 2 (everything written AFTER `\n)\n` that closes
    # the runtime block) as strings. The self-hash check in the inner
    # runtime hashes (Piece 1 + Piece 2) and aborts if the file on
    # disk doesn't match — so any tamper to the visible stub voids the
    # extraction.
    # The visible `_filename` in the .py is a DECOY. The real filename
    # is encrypted at the start of `_data` (see fname_prefix above) and
    # is only revealed at runtime after a successful decrypt.
    piece1 = (
        f"_filename = {_PY_VISIBLE_DECOY_FILENAME!r}\n"
        + f"_visible_hash = {visible_hash_hex!r}\n"
        + f"_iv_hex = {iv.hex()!r}\n"
        + "_data = (\n"
        + data_buf.getvalue()
        + "\n)\n"
        + f"_runtime_iv_hex = {rt_iv.hex()!r}\n"
    )
    piece2 = _strip_py_for_minimal_output(_PY_RUNTIME_ENCRYPTED)

    # Compute the prefix hash that the inner runtime will check
    # against. Hash bytes are `prefix_extra_bytes + piece1 + piece2` —
    # the file on disk will have `_runtime = (...)\n)\n` between piece1
    # and piece2, which the runtime explicitly skips when re-hashing at
    # execution time. For .exe targets, `prefix_extra_bytes` is the
    # stub.exe binary plus the payload-magic marker — those bytes
    # naturally appear before piece1 in the on-disk file, so the
    # runtime sees and hashes them too.
    prefix_bytes = (prefix_extra_bytes
                     + piece1.encode("utf-8")
                     + piece2.encode("utf-8"))
    expected_prefix_hash = hashlib.sha256(prefix_bytes).hexdigest()

    # Inject the hash into the inner runtime template, strip + encrypt.
    # No sentinel prefix — password verification happens via the
    # `_data[:32]` hash check in the loader's main(), so the inner
    # runtime is just plain encrypted Python source with no marker.
    inner_with_hash = _PY_INNER_RUNTIME.replace(
        "___PREFIX_HASH_PLACEHOLDER___", expected_prefix_hash)
    inner_stripped = _strip_py_for_minimal_output(inner_with_hash)
    inner_plaintext = inner_stripped.encode("utf-8")
    rt_enc = _sc.StreamingEncryptor(key, rt_iv)
    rt_ciphertext = rt_enc.update(inner_plaintext) + rt_enc.finalize()
    rt_b64 = base64.b64encode(rt_ciphertext).decode("ascii")

    runtime_block = "_runtime = (\n"
    for i in range(0, len(rt_b64), chunk_chars):
        chunk = rt_b64[i:i + chunk_chars]
        sep = "" if i == 0 else "\n"
        runtime_block += f'{sep}    "{chunk}"'
    runtime_block += "\n)\n"

    # Return the .py source bytes in LF-only form. Caller writes them
    # to disk in BINARY mode (no CRLF translation) — consistent newlines
    # are required for the embed-time hash and runtime-time hash to agree.
    return (piece1.encode("utf-8")
             + runtime_block.encode("utf-8")
             + piece2.encode("utf-8"))


def _py_embed_to_file_encrypted(src_path: Path, src_ext: str, dst: Path,
                                  password: bytes,
                                  cancel: Optional["CancellationToken"] = None,
                                  src_filename: Optional[str] = None) -> None:
    """Encrypted (password-protected) self-extracting .py.

    As of v4, .py outputs use the bootstrap-+-blob format defined by
    `_build_py_source_encrypted_v4`: ~120 lines of visible bootstrap
    Python (counter mechanism, password prompt, decrypt outer runtime,
    hash-check, exec) plus a single base64 string holding the entire
    encrypted blob (outer runtime ciphertext + payload ciphertext +
    headers). Parse-compile time stays sub-100ms regardless of payload
    size; the actual decrypt logic, tamper response, and file-write
    code live encrypted inside the blob and are never visible in a
    text editor.

    The .exe path keeps using the older v3 source (`_build_py_source_encrypted`)
    because PyInstaller's stub already provides full opacity for that
    output type — no win from re-doing it in v4 inside the stub.

    Encryption primitives are unchanged: AES-256-CTR with PBKDF2-200k
    key derivation (same as Stone v3 envelopes).
    """
    src_bytes = _build_py_source_encrypted_v4(src_path, src_ext, password,
                                                cancel=cancel,
                                                src_filename=src_filename)
    with open(dst, "wb") as out:
        out.write(src_bytes)


def _v4_extract(src: Path, dst_path: Path,
                 password: bytes = b"",
                 cancel: Optional["CancellationToken"] = None) -> Optional[str]:
    """Try to parse `src` as a v4-format encrypted Stone .py. Returns
    the recovered source extension on success, or None if the file
    isn't v4 (caller falls back to the legacy parser).

    Detection: look for `_v4 = "` as the first non-blank, non-comment
    line. v4 files always emit the assignment as the first statement.

    Failure modes return None (let legacy parser try) only when the
    detection fails. Once v4 detection succeeds, decryption errors
    raise ValueError so the caller surfaces them instead of silently
    falling back.
    """
    # Detection — read just enough of the first line to confirm.
    try:
        with open(src, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if not head.startswith(b"_v4 = \""):
        return None

    # Confirmed v4. Read the full first line — it's the entire base64
    # blob on one line, terminated by closing quote + newline.
    with open(src, "rb") as f:
        # The first line can be arbitrarily long (multi-MB for big
        # payloads). readline() handles that; Python's stdio buffers in
        # 8K chunks under the hood so memory stays bounded by the line
        # length itself, not by some larger window.
        first_line = f.readline().decode("utf-8", errors="replace")

    # Strip trailing newline, leading/trailing whitespace, and the
    # `_v4 = "` ... `"` wrapper. Validate strictly so a malformed file
    # raises rather than silently producing garbage.
    line = first_line.rstrip("\r\n").strip()
    prefix = '_v4 = "'
    if not line.startswith(prefix) or not line.endswith('"'):
        raise ValueError("Stone .py v4: malformed _v4 assignment line.")
    blob_b64 = line[len(prefix):-1]
    try:
        # binascii.Error (raised by b64decode for invalid input) is a
        # subclass of ValueError — single catch covers both.
        blob = base64.b64decode(blob_b64, validate=True)
    except ValueError as e:
        raise ValueError(f"Stone .py v4: malformed base64 in _v4: {e}") from e

    if len(blob) < _V4_HEADER_LEN:
        raise ValueError("Stone .py v4: blob shorter than header.")
    if blob[:6] != _V4_MAGIC:
        raise ValueError(
            f"Stone .py v4: wrong magic ({blob[:6]!r}, expected {_V4_MAGIC!r})."
        )
    (magic, _file_id_bytes, iv_outer, iv_payload, visible_hash,
     outer_hash, outer_sz, payload_sz) = struct.unpack(_V4_HEADER_FMT,
                                                          blob[:_V4_HEADER_LEN])

    expected_total = _V4_HEADER_LEN + outer_sz + payload_sz
    if len(blob) < expected_total:
        raise ValueError(
            f"Stone .py v4: blob truncated (have {len(blob)} B, "
            f"need {expected_total} B)."
        )

    # We don't need the outer runtime to extract — we re-implement its
    # 25-line decrypt-and-write logic here in Python directly. (Vitriol
    # never has to *run* the outer runtime to recover the payload; that's
    # only needed when running the .py directly.) Skipping the outer
    # decrypt also means a wrong password just produces garbage at the
    # SHA-256 step below, matching the no-oracle invariant.
    payload_off = _V4_HEADER_LEN + outer_sz
    payload_ct = blob[payload_off:payload_off + payload_sz]

    # Cancellation hook — for huge payloads, give the queue a chance to
    # bail out after the header parse and before AES does its work.
    if cancel is not None:
        cancel.check()

    from . import _stone_crypto as _sc
    try:
        plaintext = _sc.decrypt(iv_payload, payload_ct, password)
    except Exception as e:
        raise ValueError(f"Stone .py v4: decrypt failed: {e}") from e

    if len(plaintext) < 2:
        raise ValueError("Stone .py v4: decrypted payload too short.")
    fname_len = struct.unpack(">H", plaintext[:2])[0]
    if 2 + fname_len > len(plaintext) or fname_len > 4096:
        # Wrong-password decrypt produces garbage that won't have a
        # plausible filename header. Mirror the v3 behavior: write
        # whatever bytes follow the header and let downstream code
        # discover the SHA-256 mismatch.
        payload = plaintext[2:]
        src_filename = ""
    else:
        src_filename = plaintext[2:2 + fname_len].decode("utf-8",
                                                            errors="replace")
        payload = plaintext[2 + fname_len:]

    # Hash check — same protection as the runtime path's tamper response.
    # Vitriol's drop-in extractor doesn't self-delete (we don't own the
    # file the way a runtime invocation does), but we DO refuse to write
    # garbage to disk on tamper or wrong password.
    if visible_hash != hashlib.sha256(payload).digest():
        raise ValueError(
            "Stone .py v4: payload hash mismatch (wrong password or tampered file)."
        )

    src_ext = Path(src_filename).suffix.lower() or ".bin"
    with open(dst_path, "wb") as out:
        out.write(payload)
    return src_ext


def _py_extract_to_file(src: Path, dst_path: Path,
                         cancel: Optional["CancellationToken"] = None,
                         password: bytes = b"") -> str:
    """If `src` is a Vitriol-generated Stone .py, decode the embedded
    payload and write it to dst_path. Returns the recovered source
    extension.

    Handles three on-disk variants:
      - v4 (encrypted, current format): single `_v4 = "<base64>"` line
        followed by the visible bootstrap. The base64 decodes to a
        binary blob with magic + IVs + hashes + sizes + outer-runtime
        ciphertext + payload ciphertext. Parsed by `_v4_extract` below.
      - UCMSv2-py     : legacy plain base64-encoded payload. `_data` is
                        the base64 of the original bytes.
      - UCMSv2-py-enc : legacy encrypted (AES-256-CTR). `_data` is base64
                        of the combined ciphertext (encrypted_hash ||
                        encrypted_payload). `_iv_hex` and `_visible_hash`
                        appear above `_data`. Caller must provide the
                        correct `password` or this raises ValueError.
                        (Wrong password produces silent garbage and the
                        SHA-256 check below catches it — not a leak;
                        same behavior the runtime script would show.)
    """
    # Try the v4 path first. Cheap detection: look for `_v4 = "` near
    # the top of the file. If absent, fall through to the legacy
    # line-by-line parser below.
    v4_result = _v4_extract(src, dst_path, password=password, cancel=cancel)
    if v4_result is not None:
        return v4_result
    sha_expected = ""
    src_filename = ""
    iv_hex = ""
    visible_hash = ""
    variant = "plain"
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
                # Legacy comment-based metadata (older Vitriol versions
                # wrote # Source: / # SHA-256: / # UCMSv2-py-enc lines).
                if stripped.startswith("# Source:"):
                    if not src_filename:
                        src_filename = stripped[len("# Source:"):].strip()
                elif stripped.startswith("# SHA-256:"):
                    sha_expected = stripped[len("# SHA-256:"):].strip()
                elif stripped.startswith("# UCMSv2-py-enc"):
                    variant = "encrypted"
                # Current variable-assignment metadata (no comments needed).
                elif stripped.startswith("_filename"):
                    eq = stripped.find("=")
                    if eq > 0:
                        rhs = stripped[eq + 1:].strip().strip("'\"")
                        if rhs and not src_filename:
                            src_filename = rhs
                elif stripped.startswith("_visible_hash"):
                    eq = stripped.find("=")
                    if eq > 0:
                        rhs = stripped[eq + 1:].strip().strip("'\"")
                        visible_hash = rhs
                        # Presence of _visible_hash means encrypted variant.
                        variant = "encrypted"
                elif stripped.startswith("_iv_hex"):
                    eq = stripped.find("=")
                    if eq > 0:
                        rhs = stripped[eq + 1:].strip().strip("'\"")
                        iv_hex = rhs
                        variant = "encrypted"
                elif stripped.startswith("_data = ("):
                    in_data = True
                elif stripped.startswith("data = ("):
                    # Older plain variant (no underscore prefix)
                    in_data = True
                continue
            # in_data
            if stripped == ")":
                closed = True
                break
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
    # Encrypted variant: prefer the visible_hash variable as the SHA-256
    # anchor when no `# SHA-256:` comment was found.
    if visible_hash and not sha_expected:
        sha_expected = visible_hash
    body = "".join(b64_chunks)
    raw = base64.b64decode(body)
    # NOTE: don't compute src_ext yet — the encrypted variant may
    # override src_filename with the real filename recovered from the
    # decrypted prefix below. src_ext is computed at the end.

    if variant == "encrypted":
        if not iv_hex:
            raise ValueError("Stone .py (encrypted): missing _iv_hex.")
        try:
            iv = bytes.fromhex(iv_hex)
        except ValueError as e:
            raise ValueError(f"Stone .py (encrypted): malformed _iv_hex: {e}")
        from . import _stone_crypto as _sc
        # Decrypt the WHOLE ciphertext as one stream. Plaintext layout:
        #   [0..32)        SHA-256 of payload
        #   [32..34)       big-endian uint16 filename length
        #   [34..34+N)     real filename, UTF-8
        #   [34+N..)       payload bytes
        plaintext = _sc.decrypt(iv, raw, password)
        if len(plaintext) < 34:
            raise ValueError("Stone .py (encrypted): truncated ciphertext.")
        # NO hash check here — preserves the no-oracle invariant. Wrong
        # password produces silent garbage. The runtime .py script DOES
        # check the hash and refuses to write garbage on wrong password.
        fname_len = struct.unpack(">H", plaintext[32:34])[0]
        # Sanity-clamp on garbage from wrong-password decrypts. A real
        # filename is ≤ 4096 bytes; anything larger is implausible.
        if fname_len > 4096 or 34 + fname_len > len(plaintext):
            # Treat as wrong-password garbage. The visible `_filename`
            # decoy stays as-is (will be ignored downstream).
            payload = plaintext[34:]
        else:
            real_filename = plaintext[34:34 + fname_len].decode(
                "utf-8", errors="replace")
            payload = plaintext[34 + fname_len:]
            # Override the visible (decoy) filename with the recovered
            # real one so the caller sees the correct extension.
            if real_filename:
                src_filename = real_filename
    else:
        payload = raw
        if sha_expected:
            actual = hashlib.sha256(payload).hexdigest()
            if actual != sha_expected:
                raise ValueError(
                    f"Stone .py: SHA-256 mismatch (expected {sha_expected}, got {actual}).")

    src_ext = Path(src_filename).suffix.lower() or ".bin"
    with open(dst_path, "wb") as out:
        out.write(payload)
    return src_ext


def _py_extract_to_file_in_memory(src: Path,
                                    password: bytes = b"") -> "Tuple[bytes, str]":
    """Convenience wrapper around `_py_extract_to_file` that returns
    `(payload_bytes, recovered_ext)` instead of writing to a temp file
    and reading it back. Used by `convert()`'s Stone-.py guard so we can
    propagate a clean ValueError without the silent return-None behavior
    that `_try_extract` does."""
    import tempfile
    tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
    try:
        ext = _py_extract_to_file(src, tmp, password=password)
        return tmp.read_bytes(), ext
    finally:
        try: tmp.unlink()
        except OSError: pass


def _py_is_stone(src: Path) -> bool:
    """Quick check: is this .py a Vitriol Stone-generated script?

    Detects via structural markers (the `_filename = '...'` and
    `_data = (` line pair near the start of the file) rather than a
    magic comment, since current Vitriol versions don't write any
    identifying header comments. Also recognizes legacy files that
    have the `# Generated by Vitriol - Philosopher's Stone Mode`
    comment for backward compat with older outputs."""
    try:
        with open(src, "r", encoding="utf-8") as f:
            head = f.read(8192)
    except OSError:
        return False
    # Legacy header comment.
    if "# Generated by Vitriol - Philosopher's Stone Mode" in head:
        return True
    # Current structural marker: `_filename = '...'` followed (on a
    # later line) by `_data = (`. Both must be present near the top.
    has_filename = False
    has_data = False
    for line in head.splitlines():
        s = line.strip()
        if s.startswith("_filename") and "=" in s:
            has_filename = True
        elif s.startswith("_data = ("):
            has_data = True
        if has_filename and has_data:
            return True
    return False


# Host: EXE (self-extracting Windows .exe). Layout: stub.exe + magic +
# appended Python source (same runtime as the .py target — plain or
# encrypted). Encrypted variant's self-hash check includes stub bytes +
# magic in the prefix.

# Magic marker between stub binary and appended Python source. MUST match
# the constant in tools/selfextract_stub.py. Trailing NUL prevents the
# literal from matching when it appears in source.
_EXE_PAYLOAD_MAGIC = b"TMUTSTUB-PAYLOAD\x00"


def _stub_exe_bytes() -> bytes:
    """Read the bundled selfextract_stub.exe. Cached after first call."""
    global _STUB_EXE_BYTES_CACHE
    cached = globals().get("_STUB_EXE_BYTES_CACHE")
    if cached is not None:
        return cached
    # Lazy import to avoid pulling app.utils.paths at module load.
    from ..utils.paths import resources_dir
    stub_path = resources_dir() / "stubs" / "selfextract_stub.exe"
    if not stub_path.exists():
        raise RuntimeError(
            "selfextract_stub.exe is not present. Run "
            "`python tools/build_selfextract_stub.py` to produce "
            f"{stub_path}.")
    with open(stub_path, "rb") as f:
        data = f.read()
    globals()["_STUB_EXE_BYTES_CACHE"] = data
    return data


def _exe_embed_to_file(src_path: Path, src_ext: str, dst: Path,
                        cancel: Optional["CancellationToken"] = None,
                        src_filename: Optional[str] = None,
                        password: bytes = b"") -> None:
    """Generate a self-extracting Windows .exe.

    Layout: `[selfextract_stub.exe binary] [magic] [Python source bytes]`

    The Python source is the SAME runtime the .py target produces (plain
    or encrypted variant, password-gated identically). The stub.exe at
    runtime locates the magic, exec's the appended source, and the
    runtime then writes the recovered file + clears its registry counter
    + self-deletes (the .exe defers its own deletion via cmd since
    Windows holds an exclusive lock on a running .exe).

    For the encrypted variant the inner runtime's self-hash check
    includes the stub.exe + magic bytes — that's why we pass them as
    `prefix_extra_bytes` into the source builder.
    """
    stub_bytes = _stub_exe_bytes()
    prefix = stub_bytes + _EXE_PAYLOAD_MAGIC
    if password:
        py_src = _build_py_source_encrypted(src_path, src_ext, password,
                                              cancel=cancel,
                                              src_filename=src_filename,
                                              prefix_extra_bytes=prefix)
    else:
        py_src = _build_py_source_plain(src_path, cancel=cancel,
                                          src_filename=src_filename)
    with open(dst, "wb") as out:
        out.write(prefix)
        out.write(py_src)


def _exe_extract_to_file(src: Path, dst_path: Path,
                          cancel: Optional["CancellationToken"] = None,
                          password: bytes = b"") -> str:
    """Inverse of `_exe_embed_to_file`: locate the magic in the .exe,
    decode the appended Python source, and route through the same
    `_py_extract_to_file` machinery to recover the original payload.

    Returns the recovered source extension."""
    # mmap so we can rfind the magic without loading the whole .exe
    # into memory (could be many GB if the embedded payload is huge).
    import mmap
    with open(src, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            idx = mm.rfind(_EXE_PAYLOAD_MAGIC)
            if idx < 0:
                raise ValueError("Stone .exe: no payload magic found.")
            payload_start = idx + len(_EXE_PAYLOAD_MAGIC)
            py_src_bytes = bytes(mm[payload_start:])
    # Hand the appended Python source to the existing .py extract
    # path. We materialize it to a tempfile so the .py extractor can
    # parse it line-by-line in text mode the way it normally does.
    import tempfile
    tmp = Path(tempfile.mkstemp(suffix=".py")[1])
    try:
        tmp.write_bytes(py_src_bytes)
        return _py_extract_to_file(tmp, dst_path, cancel=cancel,
                                    password=password)
    finally:
        try: tmp.unlink()
        except OSError: pass


def _exe_is_stone(src: Path) -> bool:
    """Quick check: is this .exe a Vitriol Stone-generated self-extractor?
    Detects by mmap-rfind of the payload magic. Cheap on any size file."""
    try:
        import mmap
        with open(src, "rb") as f:
            # mmap.mmap requires non-empty file.
            size = os.fstat(f.fileno()).st_size
            if size <= len(_EXE_PAYLOAD_MAGIC):
                return False
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                return mm.rfind(_EXE_PAYLOAD_MAGIC) >= 0
    except (OSError, ValueError):
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
# byte-for-byte. Round-trip via Vitriol also works (zip → png recovers the
# PNG). Always plaintext — encryption would corrupt the archive structure
# and defeat the "real zip" property, so the password parameter is
# intentionally not threaded into this path.
#
# Decoder rule for round-trip: only "Stone-built" zips (exactly one member
# whose name starts with `original.`) are auto-extracted. Any other zip is
# treated as opaque bytes by `_zip_extract` (raises ValueError, which
# `_try_extract` catches and translates to None). That lets the user wrap
# a regular multi-file zip *inside* a Vitriol zip without having the
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
    ".m4a": _alac_extract_from_bytes,
    ".zip": _zip_extract,
}


def can_embed_into(ext: str) -> bool:
    ext = ext.lower()
    # Targets handled outside the _EMBED dict (each has its own dispatch
    # path because they need extra parameters or use specialized embed
    # functions): MKV via _mkv_embed_to_file, PY via _py_embed_to_file,
    # EXE via _exe_embed_to_file (self-extracting binary), FLAC + M4A
    # via FFmpeg-routed encoders.
    return ext in _EMBED or ext in (".mkv", ".py", ".exe", ".flac", ".m4a")


def can_extract_from(ext: str) -> bool:
    ext = ext.lower()
    return ext in _EXTRACT or ext in (".mkv", ".py", ".exe")


def _try_extract(src: Path, src_ext: str,
                  password: bytes = b"",
                  progress=None) -> Tuple[bytes, str] | None:
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
            return _mkv_extract_from_file(src, password=password, progress=progress)
        if src_ext == ".py":
            if not _py_is_stone(src):
                return None
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
            try:
                ext = _py_extract_to_file(src, tmp, password=password)
                return tmp.read_bytes(), ext
            finally:
                try: tmp.unlink()
                except OSError: pass
        if src_ext == ".exe":
            if not _exe_is_stone(src):
                return None
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
            try:
                ext = _exe_extract_to_file(src, tmp, password=password)
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
        if src_ext == ".m4a":
            return _alac_extract_from_bytes(src.read_bytes(), password=password)
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
               password: bytes = b"",
               progress=None,
               src_filename: Optional[str] = None) -> None:
    """Whole-bytes-in-memory embed. Used for small files.

    `cross_category` triggers the aesthetic encoder: Mandelbrot Stone for
    PNG/BMP image targets, music encoder for WAV/AIFF/FLAC audio targets.
    `password` is forwarded to image targets for the v3 envelope encryption.
    For Mandelbrot v3 PNG/BMP, payloads above _MANDELBROT_STREAMING_THRESHOLD
    auto-route to the streaming output path so peak RAM stays bounded.
    """
    dst_ext = dst_ext.lower()
    if dst_ext == ".mkv":
        _mkv_embed_to_file(payload, src_ext, dst,
                           cross_category=cross_category, password=password,
                           progress=progress)
        return
    if dst_ext == ".png":
        if cross_category and len(payload) > _MANDELBROT_STREAMING_THRESHOLD:
            # Streaming path: bounded RAM regardless of output image size.
            _png_embed_v2_streaming_from_bytes(payload, src_ext, dst,
                                                  password=password,
                                                  progress=progress)
        else:
            _png_embed_v2_from_bytes(payload, src_ext, dst,
                                      mandelbrot=cross_category,
                                      password=password)
        return
    if dst_ext == ".bmp":
        if cross_category and len(payload) > _MANDELBROT_STREAMING_THRESHOLD:
            _bmp_embed_v2_streaming_from_bytes(payload, src_ext, dst,
                                                  password=password,
                                                  progress=progress)
        else:
            _bmp_embed_v2_from_bytes(payload, src_ext, dst,
                                      mandelbrot=cross_category,
                                      password=password)
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
    if dst_ext == ".m4a":
        # M4A/ALAC: cross-category uses the music encoder (encrypted v3
        # envelope bit-packed into ALAC PCM). Same-category audio→m4a is
        # not supported as Stone — there is no plaintext UCMSv1 path for
        # ALAC. Caller should route same-category audio→m4a through the
        # regular media pipeline.
        if not cross_category:
            raise RuntimeError(
                "M4A Stone embed only supports cross-category sources. "
                "Same-type audio → M4A should use the standard media pipeline.")
        _alac_embed_music(payload, src_ext, dst, password=password)
        return
    if dst_ext == ".py":
        # Write payload to a temp file so _py_embed_to_file can stream it.
        # `src_filename` (when present) is used for the script's output
        # filename — so the script reconstructs as e.g. `Music.wav` instead
        # of `tmp12345.wav`. Falls back to "extracted{ext}" when we don't
        # have an original filename (e.g., re-embed-after-extract path).
        # Password (when set) triggers the encrypted variant: the .py
        # prompts for a password at runtime and AES-CTR-decrypts before
        # writing the original file.
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=src_ext or ".bin")[1])
        try:
            tmp.write_bytes(payload)
            effective_filename = src_filename or ("extracted" + (src_ext or ".bin"))
            _py_embed_to_file(tmp, src_ext, dst, cancel,
                               src_filename=effective_filename,
                               password=password)
        finally:
            try: tmp.unlink()
            except OSError: pass
        return
    if dst_ext == ".exe":
        # Self-extracting Windows .exe. Same runtime as the .py target
        # (plain or encrypted), but appended after a pre-compiled
        # selfextract_stub.exe binary so end users don't need Python.
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=src_ext or ".bin")[1])
        try:
            tmp.write_bytes(payload)
            effective_filename = src_filename or ("extracted" + (src_ext or ".bin"))
            _exe_embed_to_file(tmp, src_ext, dst, cancel=cancel,
                                src_filename=effective_filename,
                                password=password)
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
                        password: bytes = b"",
                        src_filename: Optional[str] = None) -> None:
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
        # Use the user's actual filename if we have it, else fall back to
        # the source path's name. Both reach `_py_embed_to_file` as the
        # name baked into the script header AND the file the script writes
        # at run time. Password (when set) triggers the encrypted variant.
        _py_embed_to_file(src_path, src_ext, dst, cancel,
                           src_filename=src_filename or src_path.name,
                           password=password)
        return
    if dst_ext == ".exe":
        # Streaming counterpart: same as .py target but wraps the source
        # in a self-extracting Windows .exe.
        _exe_embed_to_file(src_path, src_ext, dst, cancel=cancel,
                            src_filename=src_filename or src_path.name,
                            password=password)
        return
    if dst_ext == ".mkv" and cross_category:
        # Streaming MKV embed: encrypts source on disk through a
        # tempfile + mmap so peak RAM stays bounded for multi-GB
        # sources. Same envelope format and same audio-track-with-
        # tamper-hash as the in-memory `_mkv_embed_to_file` — only
        # the byte-flow differs. Falls through to the in-memory path
        # for same-category MKV (legacy plaintext UCMSv1, never large
        # by definition).
        _mkv_embed_to_file_streamed(src_path, src_ext, dst,
                                      password=password, progress=progress)
        return
    # WAV / TXT / same-cat MKV: whole-file bytes API still used; fall through.
    src_bytes = src_path.read_bytes()
    _embed_to(dst, src_bytes, src_ext, dst_ext, cancel,
              cross_category=cross_category, password=password,
              src_filename=src_filename)


def convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
            cancel: CancellationToken, progress: Callable[[float], None],
            *, cross_category: bool = False,
            password: bytes = b"",
            src_filename: Optional[str] = None) -> None:
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

    # Default filename for .py output: the user's actual src filename.
    # Caller (router) can override via the `src_filename` kwarg if it has
    # better info. The extracted-then-re-embedded path doesn't have the
    # original filename — `_embed_to` will fall back to "extracted{ext}"
    # in that case so the .py script doesn't bake in a tempfile name.
    if src_filename is None:
        src_filename = src.name

    # Scoped progress lambdas: each phase gets its own 0..1 callback that
    # maps onto a slice of the outer 0..1 budget. Inner functions don't need
    # to know about phase ratios — they tick within their own 0..1 space.
    extract_progress = (lambda p: progress(0.05 + p * 0.50)) if progress else None
    embed_progress_after_extract = (lambda p: progress(0.55 + p * 0.45)) if progress else None
    embed_progress_streamed = (lambda p: progress(0.10 + p * 0.90)) if progress else None
    embed_progress_inmem = (lambda p: progress(0.30 + p * 0.70)) if progress else None

    # Stone .py guard: if the source IS clearly a Stone-generated .py
    # (matches `_py_is_stone`), extract failure must surface as a clear
    # error rather than falling through to the embed branch (which would
    # silently treat the .py source bytes as a fresh payload — producing
    # a bogus output file with a misleading name like "extracted.bin").
    if src_ext == ".py" and _py_is_stone(src):
        try:
            payload, recovered_ext = _py_extract_to_file_in_memory(
                src, password=password)
        except ValueError as e:
            raise RuntimeError(
                f"Stone .py extract failed: {e} The file may be corrupted "
                f"or generated by an incompatible Vitriol version.")
        cancel.check()
        progress(0.55)
        # Stone .py source is always cross-category from its dst's
        # perspective. Honor the user's chosen dst_ext: write payload
        # directly when ext matches OR dst is non-host; otherwise re-embed.
        if dst_ext == recovered_ext or not can_embed_into(dst_ext):
            dst.write_bytes(payload)
            progress(1.0)
            return
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel,
                  cross_category=cross_category, password=password,
                  progress=embed_progress_after_extract,
                  src_filename=None)
        progress(1.0)
        return

    # Stone .exe guard: same shape as the .py guard above. If the source
    # is a Vitriol self-extracting .exe, surface extract failure as a
    # clean error rather than treating the .exe binary as a fresh payload.
    if src_ext == ".exe" and _exe_is_stone(src):
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
        try:
            try:
                recovered_ext = _exe_extract_to_file(src, tmp, password=password)
            except ValueError as e:
                raise RuntimeError(
                    f"Stone .exe extract failed: {e} The file may be corrupted "
                    f"or generated by an incompatible Vitriol version.")
            payload = tmp.read_bytes()
        finally:
            try: tmp.unlink()
            except OSError: pass
        cancel.check()
        progress(0.55)
        if dst_ext == recovered_ext or not can_embed_into(dst_ext):
            dst.write_bytes(payload)
            progress(1.0)
            return
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel,
                  cross_category=cross_category, password=password,
                  progress=embed_progress_after_extract,
                  src_filename=None)
        progress(1.0)
        return

    # Streaming MKV extract dispatch: for sources past the streaming
    # threshold, route through the payload-on-disk path so multi-GB
    # MKV → file conversions don't OOM. Below the threshold, use the
    # in-memory `_try_extract` (slightly faster for small files).
    if (src_ext == ".mkv"
            and src.stat().st_size >= _MKV_EXTRACT_STREAMING_THRESHOLD
            and _video_v3_envelope_probe(src)):
        import tempfile, shutil
        cancel.check()
        payload_tmp = Path(tempfile.mkstemp(suffix=".tmp")[1])
        try:
            recovered_ext = _mkv_v3_extract_streaming(
                src, password, payload_tmp, progress=extract_progress)
            cancel.check()
            # Tamper-detection runs against the recovered tempfile via
            # streaming SHA-256 (so we never load the payload into RAM).
            _verify_video_audio_hash_streaming(src, payload_tmp, password)
            cancel.check()
            progress(0.55)
            if dst_ext == recovered_ext or not can_embed_into(dst_ext):
                # Copy tempfile to dst (NOT shutil.move — on Windows the
                # tempfile can briefly stay locked by AV scanning, which
                # makes shutil.move's rename-then-unlink racy. The copy
                # path leaves the tempfile alone; the `finally` block
                # below cleans it up best-effort.).
                shutil.copyfile(str(payload_tmp), str(dst))
                progress(1.0)
                return
            # Re-embed via the streamed embed path so we don't load the
            # recovered payload into RAM here either.
            _embed_streamed_to(dst, payload_tmp, recovered_ext, dst_ext,
                                cancel=cancel,
                                progress=embed_progress_after_extract,
                                cross_category=cross_category,
                                password=password,
                                src_filename=None)
            progress(1.0)
            return
        finally:
            if payload_tmp is not None:
                try: payload_tmp.unlink()
                except OSError: pass

    # When dst is .py, treat the source as raw bytes — DON'T auto-
    # extract a Stone host first. The .py target is a self-extracting
    # wrapper for "this file" — the user expects running the .py to
    # reproduce the EXACT input file (including any Stone payload it
    # may itself contain). Auto-extracting would silently swap in the
    # inner payload + lose the original filename, producing the
    # `extracted.bin` confusion. (For Stone-host conversions to other
    # targets — .png → .wav, .mkv → .png, etc. — auto-extract still
    # fires.)
    skip_auto_extract = (dst_ext == ".py")
    extracted = (_try_extract(src, src_ext, password=password,
                               progress=extract_progress)
                 if (can_extract_from(src_ext) and not skip_auto_extract)
                 else None)
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
        # Re-embed: we don't have the original filename here (only the
        # recovered ext), so let `_embed_to` fall back to "extracted{ext}".
        _embed_to(dst, payload, recovered_ext, dst_ext, cancel,
                  cross_category=cross_category, password=password,
                  progress=embed_progress_after_extract,
                  src_filename=None)
        progress(1.0)
        return

    # Decide streamed vs whole-file path based on source size.
    src_size = src.stat().st_size
    threshold = streaming_threshold(dst_ext)
    use_streaming = (
        dst_ext == ".py"
        or (src_size >= threshold and dst_ext in (".png", ".bmp"))
        # MKV gets its own streaming threshold — at 100 MB+ the
        # in-memory encrypt holds ~3× source size in RAM, which OOMs
        # on most systems for 1 GB+ payloads. Streaming MKV uses a
        # tempfile + mmap and stays bounded ~30–50 MB regardless of
        # source size.
        or (src_size >= _MKV_STREAMING_THRESHOLD
             and dst_ext == ".mkv" and cross_category)
    )
    if use_streaming:
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
              cross_category=cross_category, password=password,
              progress=embed_progress_inmem)
    progress(1.0)
