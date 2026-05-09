"""FFmpeg subprocess wrapper for ALL audio and video conversion.

Scope:
  - Reads & writes every audio/video format the bundled FFmpeg supports.
  - Picks sensible default codecs per output container; no quality knobs in v1.
  - Reports progress by parsing FFmpeg's `-progress pipe:1` stream.
  - Cancellation via CancellationToken: terminates the FFmpeg subprocess.

Does not implement: probing for stream info beyond what FFmpeg infers from
the input. If FFmpeg can't read a file, the failure surfaces as a stderr-tail
error message in the playlist.
"""
from __future__ import annotations
import functools
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

import json

from ..utils.cancellation import CancellationToken, CancelledError
from ..utils.logger import get_logger
from ..utils.paths import bin_dir, hw_encoder_cache

_log = get_logger()

MEDIA_CATEGORY = "audio"  # overridden per-ext below by registry split

AUDIO_EXTS = {
    ".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma",
    ".aiff", ".alac", ".ac3", ".amr", ".au", ".mka", ".oga", ".mp2",
}
VIDEO_EXTS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv",
    ".mpg", ".mpeg", ".3gp", ".ts", ".vob", ".ogv", ".asf", ".f4v", ".m4v",
}
SUPPORTED = AUDIO_EXTS | VIDEO_EXTS

# Per-extension category lookup. Used by the registry to set MEDIA_CATEGORY_OF.
_CAT = {**{e: "audio" for e in AUDIO_EXTS}, **{e: "video" for e in VIDEO_EXTS}}


# Default codec map per output container. Conservative choices that play
# everywhere by default; can be tuned later.
_AUDIO_CODECS = {
    ".mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".flac": ["-c:a", "flac"],
    ".ogg": ["-c:a", "libvorbis", "-q:a", "5"],
    ".opus": ["-c:a", "libopus", "-b:a", "128k"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".aac": ["-c:a", "aac", "-b:a", "192k"],
    ".wma": ["-c:a", "wmav2", "-b:a", "192k"],
    ".aiff": ["-c:a", "pcm_s16be"],
    ".alac": ["-c:a", "alac"],
    ".ac3": ["-c:a", "ac3", "-b:a", "192k"],
    ".amr": ["-c:a", "libopencore_amrnb", "-ar", "8000", "-ac", "1"],
    ".au": ["-c:a", "pcm_s16be"],
    ".mka": ["-c:a", "libvorbis", "-q:a", "5"],
    ".oga": ["-c:a", "libvorbis", "-q:a", "5"],
    ".mp2": ["-c:a", "mp2", "-b:a", "192k"],
}
_VIDEO_CODECS = {
    ".mp4": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
    ".mkv": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
    ".webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-c:a", "libopus", "-b:a", "128k"],
    ".avi": ["-c:v", "mpeg4", "-q:v", "5", "-c:a", "libmp3lame", "-q:a", "2"],
    ".mov": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
    ".wmv": ["-c:v", "wmv2", "-b:v", "2M", "-c:a", "wmav2"],
    ".flv": ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"],
    ".mpg": ["-c:v", "mpeg2video", "-q:v", "5", "-c:a", "mp2"],
    ".mpeg": ["-c:v", "mpeg2video", "-q:v", "5", "-c:a", "mp2"],
    ".3gp": ["-c:v", "libx264", "-preset", "medium", "-crf", "26", "-c:a", "aac", "-b:a", "96k"],
    ".ts": ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"],
    ".vob": ["-c:v", "mpeg2video", "-q:v", "5", "-c:a", "ac3"],
    ".ogv": ["-c:v", "libtheora", "-q:v", "7", "-c:a", "libvorbis", "-q:a", "5"],
    # ASF and WMV share the wmv2/wmav2 codecs.
    ".asf": ["-c:v", "wmv2", "-b:v", "2M", "-c:a", "wmav2"],
    # Flash variants
    ".f4v": ["-c:v", "libx264", "-preset", "medium", "-crf", "23", "-c:a", "aac", "-b:a", "128k"],
    # M4V is just MP4 with H.264/AAC, often Apple-style.
    ".m4v": ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-b:a", "192k"],
}


# Software encoder name -> (target_codec_label, what to swap to under HW path).
# The "args under HW" overrides the -preset/-crf style flags since each HW
# encoder uses different rate-control flags.
_SW_TO_HW_ARGS = {
    "libx264": {
        "h264_nvenc": ["-preset", "p5", "-rc", "vbr", "-cq", "20", "-b:v", "0"],
        "h264_qsv":   ["-preset", "medium", "-global_quality", "20"],
        "h264_amf":   ["-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22"],
        "h264_videotoolbox": ["-q:v", "55"],
    },
    "libvpx-vp9": {
        "vp9_qsv": ["-global_quality", "32"],
    },
}


@functools.lru_cache(maxsize=1)
def _hw_encoders() -> dict:
    """Load the launcher's hw-encoder cache. {target_codec -> ffmpeg encoder name}."""
    cache = hw_encoder_cache()
    if not cache.exists():
        return {}
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _maybe_swap_to_hw(codec_args: list[str]) -> list[str]:
    """If the args use a software encoder we have a HW path for, swap silently."""
    if "-c:v" not in codec_args:
        return codec_args
    idx = codec_args.index("-c:v")
    if idx + 1 >= len(codec_args):
        return codec_args
    sw_name = codec_args[idx + 1]
    hw_table = _SW_TO_HW_ARGS.get(sw_name)
    if not hw_table:
        return codec_args
    available = _hw_encoders()
    # Map sw name to target codec label
    target = "h264" if sw_name == "libx264" else "hevc" if sw_name == "libx265" else "vp9" if sw_name == "libvpx-vp9" else None
    if not target:
        return codec_args
    chosen_hw = available.get(target)
    if not chosen_hw or chosen_hw not in hw_table:
        return codec_args
    # Build new args: replace -c:v sw -preset X -crf Y with -c:v hw <hw-flags>
    out: list[str] = []
    i = 0
    while i < len(codec_args):
        a = codec_args[i]
        if a == "-c:v" and i + 1 < len(codec_args) and codec_args[i + 1] == sw_name:
            out += ["-c:v", chosen_hw]
            i += 2
            continue
        # Strip software-encoder-specific flags that wouldn't apply
        if a in ("-preset", "-crf", "-b:v") and i + 1 < len(codec_args):
            i += 2
            continue
        out.append(a)
        i += 1
    out += hw_table[chosen_hw]
    _log.info("using hardware encoder %s in place of %s", chosen_hw, sw_name)
    return out


# Module-level resolved paths. Populated lazily by _ffmpeg_path / _ffprobe_path
# on first call so we don't re-walk filesystem + PATH per conversion. Cleared
# automatically when the cached binary disappears between conversions.
_FFMPEG_RESOLVED: Optional[Path] = None
_FFPROBE_RESOLVED: Optional[Path] = None


def _ffmpeg_path() -> Path:
    global _FFMPEG_RESOLVED
    if _FFMPEG_RESOLVED is not None and _FFMPEG_RESOLVED.exists():
        return _FFMPEG_RESOLVED
    from ..utils.paths import find_ffmpeg
    found = find_ffmpeg()
    if found is None:
        raise RuntimeError("FFmpeg not found. Install it via the launch prompt or place ffmpeg.exe in ./bin/.")
    _FFMPEG_RESOLVED = found
    return _FFMPEG_RESOLVED


def _ffprobe_path() -> Optional[Path]:
    global _FFPROBE_RESOLVED
    if _FFPROBE_RESOLVED is not None and _FFPROBE_RESOLVED.exists():
        return _FFPROBE_RESOLVED
    from ..utils.paths import find_ffprobe
    _FFPROBE_RESOLVED = find_ffprobe()
    return _FFPROBE_RESOLVED


def _probe_duration_seconds(src: Path) -> Optional[float]:
    """Return media duration in seconds, or None if unknown."""
    probe = _ffprobe_path()
    if probe is None:
        return None
    try:
        out = subprocess.check_output(
            [str(probe), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(src)],
            stderr=subprocess.STDOUT, text=True, timeout=15,
        ).strip()
        return float(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _build_args(src: Path, dst: Path, src_ext: str, dst_ext: str) -> list[str]:
    """Build the FFmpeg arg list for src → dst."""
    src_is_video = src_ext in VIDEO_EXTS
    dst_is_audio = dst_ext in AUDIO_EXTS
    dst_is_video = dst_ext in VIDEO_EXTS

    args: list[str] = ["-y", "-i", str(src)]

    if dst_is_audio:
        # Audio-only output: drop video stream(s).
        args += ["-vn"]
        codec = _AUDIO_CODECS.get(dst_ext, ["-c:a", "copy"])
        args += codec
    elif dst_is_video:
        codec = _VIDEO_CODECS.get(dst_ext, [])
        codec = _maybe_swap_to_hw(codec)
        args += codec
        # If the source is audio-only and target is video, FFmpeg will fail —
        # let the user see that error directly.
    else:
        # Shouldn't reach here; router gate should catch it.
        raise RuntimeError(f"Unsupported audio/video target: {dst_ext}")

    args += ["-progress", "pipe:1", "-nostats", str(dst)]
    return args


def convert(
    src: Path,
    dst: Path,
    src_ext: str,
    dst_ext: str,
    cancel: CancellationToken,
    progress: Callable[[float], None],
) -> None:
    ff = _ffmpeg_path()
    duration = _probe_duration_seconds(src)
    args = [str(ff)] + _build_args(src, dst, src_ext, dst_ext)
    _log.info("ffmpeg: %s", " ".join(args))

    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=creationflags,
    )
    cancel.on_cancel(lambda: _terminate(proc))

    stderr_tail: list[str] = []

    def drain_stderr() -> None:
        try:
            for line in proc.stderr or []:
                stderr_tail.append(line)
                if len(stderr_tail) > 120:
                    del stderr_tail[:60]
        except (ValueError, OSError):
            pass

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    out_time_us = 0
    try:
        for line in proc.stdout or []:
            if cancel.is_set():
                _terminate(proc)
                break
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    out_time_us = int(line.split("=", 1)[1])
                except ValueError:
                    pass
                if duration and duration > 0:
                    progress(min(0.99, out_time_us / 1_000_000 / duration))
            elif line.startswith("out_time_ms="):
                try:
                    out_time_us = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line == "progress=end":
                progress(1.0)
                break
    finally:
        proc.wait()
        t.join(timeout=1.0)

    if cancel.is_set():
        raise CancelledError()
    if proc.returncode != 0:
        tail = "".join(stderr_tail[-30:]).strip()
        raise RuntimeError(f"FFmpeg exit {proc.returncode}: {tail or 'no error message'}")


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass


# Convenience for the registry: per-ext category map.
def category_of(ext: str) -> str:
    return _CAT.get(ext, "audio")
