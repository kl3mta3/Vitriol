"""RTF read handler.

Strategy:
  1. Try the `striprtf` package first (battle-tested ~300 lines, BSD-3).
     The launcher installs it via requirements.txt.
  2. If unavailable, fall back to a minimal from-scratch RTF 1.5 extractor.
     This covers common cases (Unicode escapes, group nesting, paragraph
     breaks, skipped destinations) but is known to break on cursed RTF
     such as Outlook-clipboard exports, embedded objects, and old Mac Word.

RTF write is deferred for v1.
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import List

from ..core.intermediate import Paragraph, Run, TextDoc
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".rtf"}
SUPPORTED_WRITE: set[str] = set()  # deferred per v1 plan
DOC_KIND = "text"


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    raw = path.read_bytes()
    cancel.check()
    text = _try_striprtf(raw)
    if text is None:
        text = _extract_text(raw)
    blocks = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip("\n").strip()
        if chunk:
            blocks.append(Paragraph(runs=[Run(text=chunk)]))
    return TextDoc(blocks=blocks)


def _try_striprtf(raw: bytes) -> str | None:
    """Use striprtf if installed (battle-tested for cursed RTF in the wild)."""
    try:
        from striprtf.striprtf import rtf_to_text  # type: ignore
    except ImportError:
        return None
    try:
        return rtf_to_text(raw.decode("latin-1", errors="replace"), errors="ignore")
    except Exception:
        return None


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:  # pragma: no cover
    raise RuntimeError("Writing RTF is not supported in v1.")


# Skipped destinations (control words whose group content we ignore entirely)
_SKIP_DEST = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict",
    "object", "header", "footer", "footnote", "annotation",
    "themedata", "colorschememapping", "datastore", "operator",
    "panose", "filetbl", "rsidtbl", "generator", "listtable",
    "listoverridetable",
}


def _extract_text(data: bytes) -> str:
    """RTF parser → plain text. Uses bytes-then-decode where unicode escapes drive it."""
    # Decode as latin-1 first (1:1 byte mapping), then handle escapes.
    src = data.decode("latin-1", errors="replace")

    out: List[str] = []
    i = 0
    n = len(src)
    # Stack of (skip_group: bool, ucskip: int, char_codepage: str)
    stack: List[dict] = [{"skip": False, "ucskip": 1, "cpg": "cp1252"}]

    while i < n:
        ch = src[i]
        state = stack[-1]
        if ch == "\\":
            i += 1
            if i >= n:
                break
            c = src[i]
            if c == "\\" or c == "{" or c == "}":
                if not state["skip"]:
                    out.append(c)
                i += 1
                continue
            if c == "*":
                # \*\controlword → mark next group as skip
                i += 1
                # Push a flag so the next time we open a group, mark it skip
                stack[-1]["skip_next_group_with_dest"] = True
                continue
            if c == "'":
                # Hex byte: \'xx
                if i + 2 < n:
                    try:
                        b = int(src[i + 1:i + 3], 16)
                        if not state["skip"]:
                            try:
                                out.append(bytes([b]).decode(state["cpg"]))
                            except UnicodeDecodeError:
                                out.append("?")
                        i += 3
                        continue
                    except ValueError:
                        pass
                i += 1
                continue
            if c == "u":
                # Unicode escape: \uNNNN[?]
                m = re.match(r"u(-?\d+)", src[i:])
                if m:
                    code = int(m.group(1))
                    if code < 0:
                        code += 65536
                    if not state["skip"]:
                        try:
                            out.append(chr(code))
                        except ValueError:
                            out.append("?")
                    i += len(m.group(0))
                    # Skip ucskip following bytes/control
                    skip_n = state["ucskip"]
                    while skip_n > 0 and i < n:
                        if src[i] == "\\":
                            i += 1
                            # Skip whole control word
                            m2 = re.match(r"[a-zA-Z]+(-?\d+)?", src[i:])
                            if m2:
                                i += len(m2.group(0))
                                if i < n and src[i] == " ":
                                    i += 1
                            elif i < n:
                                i += 1
                        else:
                            i += 1
                        skip_n -= 1
                    continue
            # Generic control word: \word123 [optional space]
            m = re.match(r"([a-zA-Z]+)(-?\d+)?", src[i:])
            if m:
                word = m.group(1)
                arg = m.group(2)
                i += len(m.group(0))
                if i < n and src[i] == " ":
                    i += 1
                _apply_control(word, arg, state, out, stack)
                continue
            # Unknown escape — skip the next char
            i += 1
            continue
        elif ch == "{":
            # New group: copy state
            new_state = dict(state)
            new_state.pop("skip_next_group_with_dest", None)
            if state.get("skip_next_group_with_dest"):
                new_state["skip"] = True
                state.pop("skip_next_group_with_dest", None)
            stack.append(new_state)
            i += 1
            continue
        elif ch == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        elif ch in ("\r", "\n"):
            # Bare CR/LF in RTF source is whitespace, ignore
            i += 1
            continue
        else:
            if not state["skip"]:
                out.append(ch)
            i += 1
    return "".join(out)


def _apply_control(word: str, arg, state: dict, out: List[str], stack: List[dict]) -> None:
    if word in _SKIP_DEST:
        state["skip"] = True
        return
    if word == "par" or word == "line" or word == "sect":
        if not state["skip"]:
            out.append("\n")
        return
    if word == "tab":
        if not state["skip"]:
            out.append("\t")
        return
    if word == "uc":
        try:
            state["ucskip"] = int(arg) if arg else 1
        except ValueError:
            state["ucskip"] = 1
        return
    if word == "ansicpg":
        if arg:
            try:
                state["cpg"] = f"cp{int(arg)}"
            except ValueError:
                pass
        return
    if word == "pard":
        # Reset paragraph properties — separate paragraphs with a blank line for our use
        if not state["skip"]:
            out.append("\n")
        return
    # No-op for everything else
