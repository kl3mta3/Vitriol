"""Subtitle conversions: srt, vtt, ass, ssa, sub (MicroDVD), mpl.

Uses a small per-cue IR (list of `Cue` records) that all parsers/emitters
share. DOC_KIND is "subtitle" so the registry only exposes subtitle→subtitle
pairs (no accidental subtitle→txt etc. — those would lose all timing).

Limitations:
  - ASS/SSA: only the dialogue lines are preserved. Style blocks and complex
    karaoke effects in the input are discarded.
  - SUB (MicroDVD): frame-based; we assume 25 fps when reading and writing
    unless the source has an `{DEFAULT}{}{!Postproc...}` framerate hint.
  - MPL2: format is `[start_decisec][end_decisec]text` — converted to seconds.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from ..utils.cancellation import CancellationToken
from . import charset

SUPPORTED_READ = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".mpl"}
SUPPORTED_WRITE = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".mpl"}
DOC_KIND = "subtitle"

_DEFAULT_FPS = 25.0


@dataclass
class Cue:
    start: float  # seconds
    end: float    # seconds
    text: str     # plain text; \n for newlines


@dataclass
class SubtitleDoc:
    cues: List[Cue] = field(default_factory=list)
    fps: float = _DEFAULT_FPS
    metadata: dict = field(default_factory=dict)


def read(path: Path, ext: str, cancel: CancellationToken) -> SubtitleDoc:
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if ext == ".srt":
        return _read_srt(text)
    if ext == ".vtt":
        return _read_vtt(text)
    if ext in (".ass", ".ssa"):
        return _read_ass(text)
    if ext == ".sub":
        return _read_microdvd(text)
    if ext == ".mpl":
        return _read_mpl(text)
    raise RuntimeError(f"Unsupported subtitle source: {ext}")


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, SubtitleDoc):
        raise RuntimeError("Subtitle writer requires a SubtitleDoc.")
    if ext == ".srt":
        path.write_text(_write_srt(doc), encoding="utf-8")
    elif ext == ".vtt":
        path.write_text(_write_vtt(doc), encoding="utf-8")
    elif ext in (".ass", ".ssa"):
        path.write_text(_write_ass(doc, advanced=(ext == ".ass")), encoding="utf-8")
    elif ext == ".sub":
        path.write_text(_write_microdvd(doc), encoding="utf-8")
    elif ext == ".mpl":
        path.write_text(_write_mpl(doc), encoding="utf-8")
    else:
        raise RuntimeError(f"Unsupported subtitle target: {ext}")


# ---- timing helpers ------------------------------------------------------

def _parse_srt_time(s: str) -> float:
    # 00:00:01,500
    m = re.match(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})", s.strip())
    if not m:
        raise ValueError(f"bad srt timestamp: {s!r}")
    h, mn, sec, ms = (int(x) for x in m.groups())
    return h * 3600 + mn * 60 + sec + ms / 1000.0


def _fmt_srt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600); t -= h * 3600
    mn = int(t // 60); t -= mn * 60
    sec = int(t); ms = int(round((t - sec) * 1000))
    if ms == 1000:
        ms = 0; sec += 1
    return f"{h:02d}:{mn:02d}:{sec:02d},{ms:03d}"


def _fmt_vtt_time(t: float) -> str:
    return _fmt_srt_time(t).replace(",", ".")


def _parse_ass_time(s: str) -> float:
    # 0:00:01.50  (centiseconds)
    m = re.match(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})", s.strip())
    if not m:
        raise ValueError(f"bad ass timestamp: {s!r}")
    h, mn, sec, frac = m.groups()
    # ASS uses centiseconds (2 digits). Pad/truncate to 3 digits = ms.
    ms_str = (frac + "00")[:3]
    return int(h) * 3600 + int(mn) * 60 + int(sec) + int(ms_str) / 1000.0


def _fmt_ass_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600); t -= h * 3600
    mn = int(t // 60); t -= mn * 60
    sec = int(t); cs = int(round((t - sec) * 100))
    if cs == 100:
        cs = 0; sec += 1
    return f"{h:01d}:{mn:02d}:{sec:02d}.{cs:02d}"


# ---- SRT -----------------------------------------------------------------

_SRT_TIMING = re.compile(r"(\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{1,3})")


def _read_srt(text: str) -> SubtitleDoc:
    cues: List[Cue] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for blk in blocks:
        lines = [l for l in blk.split("\n") if l.strip() != ""]
        if not lines:
            continue
        # Optional numeric index on the first line.
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        m = _SRT_TIMING.search(lines[0])
        if not m:
            continue
        start = _parse_srt_time(m.group(1))
        end = _parse_srt_time(m.group(2))
        body = "\n".join(lines[1:]).strip()
        cues.append(Cue(start=start, end=end, text=body))
    return SubtitleDoc(cues=cues)


def _write_srt(doc: SubtitleDoc) -> str:
    out: List[str] = []
    for i, c in enumerate(doc.cues, 1):
        out.append(str(i))
        out.append(f"{_fmt_srt_time(c.start)} --> {_fmt_srt_time(c.end)}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


# ---- VTT -----------------------------------------------------------------

def _read_vtt(text: str) -> SubtitleDoc:
    cues: List[Cue] = []
    if text.lstrip().startswith("WEBVTT"):
        # Drop the WEBVTT header (and any STYLE/REGION/NOTE blocks).
        text = re.sub(r"^WEBVTT[^\n]*\n", "", text.lstrip(), count=1)
    blocks = re.split(r"\n\s*\n", text.strip())
    for blk in blocks:
        lines = [l for l in blk.split("\n") if l.strip() != ""]
        if not lines:
            continue
        # Skip NOTE/STYLE/REGION blocks.
        if lines[0].lstrip().startswith(("NOTE", "STYLE", "REGION")):
            continue
        timing_idx = None
        for i, l in enumerate(lines):
            if "-->" in l:
                timing_idx = i
                break
        if timing_idx is None:
            continue
        m = _SRT_TIMING.search(lines[timing_idx])
        if not m:
            continue
        start = _parse_srt_time(m.group(1))
        end = _parse_srt_time(m.group(2))
        body = "\n".join(lines[timing_idx + 1:]).strip()
        # Strip simple inline tags like <c.classname> or <i>.
        body = re.sub(r"</?[^>]+>", "", body)
        cues.append(Cue(start=start, end=end, text=body))
    return SubtitleDoc(cues=cues)


def _write_vtt(doc: SubtitleDoc) -> str:
    out = ["WEBVTT", ""]
    for c in doc.cues:
        out.append(f"{_fmt_vtt_time(c.start)} --> {_fmt_vtt_time(c.end)}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


# ---- ASS / SSA -----------------------------------------------------------

_ASS_DIALOGUE_RE = re.compile(
    # Both ASS (Layer = int) and SSA (Marked=N) start the line with a single
    # field that contains no commas, then start, end, and the rest.
    r"^Dialogue:\s*(?P<layer>[^,]+),(?P<start>[^,]+),(?P<end>[^,]+),(?P<rest>.*)$",
    re.IGNORECASE,
)


def _read_ass(text: str) -> SubtitleDoc:
    cues: List[Cue] = []
    for line in text.splitlines():
        m = _ASS_DIALOGUE_RE.match(line)
        if not m:
            continue
        try:
            start = _parse_ass_time(m.group("start"))
            end = _parse_ass_time(m.group("end"))
        except ValueError:
            continue
        rest = m.group("rest")
        # Format is: Style, Name, MarginL, MarginR, MarginV, Effect, Text
        # The text is the last field but may itself contain commas — we want
        # everything from the 8th field onward (= 7 leading commas after
        # "Style").
        parts = rest.split(",", 8)
        body = parts[-1] if len(parts) >= 1 else ""
        # Strip {\override} ASS markup.
        body = re.sub(r"\{[^}]*\}", "", body)
        # ASS uses \N for hard newline.
        body = body.replace("\\N", "\n").replace("\\n", "\n")
        cues.append(Cue(start=start, end=end, text=body.strip()))
    return SubtitleDoc(cues=cues)


_ASS_HEADER_V4PLUS = (
    "[Script Info]\n"
    "ScriptType: v4.00+\n"
    "WrapStyle: 0\n"
    "ScaledBorderAndShadow: yes\n"
    "PlayResX: 1920\n"
    "PlayResY: 1080\n"
    "\n"
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n"
    "\n"
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)

_ASS_HEADER_V4 = (
    "[Script Info]\n"
    "ScriptType: v4.00\n"
    "\n"
    "[V4 Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, TertiaryColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
    "Style: Default,Arial,48,16777215,255,16777215,0,0,0,1,2,2,2,10,10,10,0,1\n"
    "\n"
    "[Events]\n"
    "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)


def _write_ass(doc: SubtitleDoc, advanced: bool = True) -> str:
    out = [_ASS_HEADER_V4PLUS if advanced else _ASS_HEADER_V4]
    layer_or_marked = "0" if advanced else "Marked=0"
    for c in doc.cues:
        body = c.text.replace("\n", "\\N")
        out.append(
            f"Dialogue: {layer_or_marked},{_fmt_ass_time(c.start)},"
            f"{_fmt_ass_time(c.end)},Default,,0,0,0,,{body}"
        )
    return "\n".join(out) + "\n"


# ---- SUB (MicroDVD) ------------------------------------------------------

_SUB_LINE_RE = re.compile(r"\{(\d+)\}\{(\d+)\}(.*)")


def _read_microdvd(text: str) -> SubtitleDoc:
    cues: List[Cue] = []
    fps = _DEFAULT_FPS
    for line in text.splitlines():
        m = _SUB_LINE_RE.match(line.strip())
        if not m:
            continue
        sf, ef, body = m.group(1), m.group(2), m.group(3)
        # First line {1}{1}<fps> is a framerate hint in some files.
        if sf == "1" and ef == "1":
            try:
                fps = float(body.strip())
                continue
            except ValueError:
                pass
        body = body.replace("|", "\n")
        # Strip MicroDVD style codes like {y:i} or {Y:i}.
        body = re.sub(r"\{[^}]*\}", "", body)
        cues.append(Cue(start=int(sf) / fps, end=int(ef) / fps, text=body))
    doc = SubtitleDoc(cues=cues, fps=fps)
    return doc


def _write_microdvd(doc: SubtitleDoc) -> str:
    fps = doc.fps if doc.fps and doc.fps > 0 else _DEFAULT_FPS
    lines: List[str] = [f"{{1}}{{1}}{fps:g}"]
    for c in doc.cues:
        body = c.text.replace("\n", "|")
        sf = int(round(c.start * fps))
        ef = int(round(c.end * fps))
        lines.append(f"{{{sf}}}{{{ef}}}{body}")
    return "\n".join(lines) + "\n"


# ---- MPL2 ----------------------------------------------------------------

_MPL_LINE_RE = re.compile(r"\[(\d+)\]\[(\d+)\](.*)")


def _read_mpl(text: str) -> SubtitleDoc:
    cues: List[Cue] = []
    for line in text.splitlines():
        m = _MPL_LINE_RE.match(line.strip())
        if not m:
            continue
        # MPL2 timestamps are deciseconds.
        start = int(m.group(1)) / 10.0
        end = int(m.group(2)) / 10.0
        body = m.group(3).replace("|", "\n")
        cues.append(Cue(start=start, end=end, text=body))
    return SubtitleDoc(cues=cues)


def _write_mpl(doc: SubtitleDoc) -> str:
    lines: List[str] = []
    for c in doc.cues:
        body = c.text.replace("\n", "|")
        ds = int(round(c.start * 10))
        de = int(round(c.end * 10))
        lines.append(f"[{ds}][{de}]{body}")
    return "\n".join(lines) + "\n"
