"""Pandoc-driven document conversions.

Adds ~50 document/markup formats by shelling out to the bundled `pandoc`
binary (auto-fetched by launcher.py into bin_dir()).

Design:
  - Each call to convert(src → dst) runs pandoc directly: no IR round-trip,
    no quality loss when both ends are pandoc-native.
  - DOC_KIND="pandoc" so this handler's formats only convert among
    themselves by default. Cross-kind bridges (e.g. .rst → .docx where
    docx is owned by docx_handler) go through the `pandoc → text` adapter
    in intermediate.py, which has pandoc emit HTML and parses it via the
    existing HTML→TextDoc reader. Symmetrically, `text → pandoc` re-emits
    the TextDoc as HTML and feeds it to pandoc as the source format.
  - Existing native handlers keep ownership of md, html, docx, epub, odt,
    rtf, txt, pptx — pandoc_handler does NOT register those extensions
    (registry uses setdefault, so even if it did, the alphabetically-
    earlier handlers would win — but being explicit avoids surprises).

Format-name mapping is in `_EXT_TO_PANDOC`. Pandoc's input vs. output
support is asymmetric (e.g. asciidoc is write-only, t2t is read-only) —
that asymmetry is reflected in SUPPORTED_READ vs. SUPPORTED_WRITE.

Cancellation: pandoc subprocess is terminated on cancel.
"""
from __future__ import annotations
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..utils.cancellation import CancellationToken, CancelledError
from ..utils.logger import get_logger

_log = get_logger()


# ---------------------------------------------------------------------------
# Extension → pandoc format-name map.
#
# Each entry is (read_fmt, write_fmt). None means "not supported in this
# direction". A format may have only a reader, only a writer, or both.
# ---------------------------------------------------------------------------

_EXT_TO_PANDOC: dict[str, tuple[Optional[str], Optional[str]]] = {
    # One canonical extension per format. Aliases (.asciidoc, .latex, .bibtex,
    # .docbook, .typst, .wiki, .texinfo, .1-.8 for man-page section numbers)
    # were intentionally dropped — they cluttered the dropdown without
    # adding any conversion the canonical extension doesn't already cover.
    # Same for .bbcode, which pandoc has no real writer for (it would have
    # silently emitted ANSI text under a misleading extension).

    # Lightweight markup (most are read+write)
    ".rst":      ("rst", "rst"),
    ".org":      ("org", "org"),
    ".muse":     ("muse", "muse"),
    ".textile":  ("textile", "textile"),
    ".djot":     ("djot", "djot"),
    ".adoc":     (None, "asciidoc"),     # pandoc writes asciidoc but does not read it
    ".t2t":      ("t2t", None),          # txt2tags: read-only
    ".markua":   (None, "markua"),
    # Notebook
    ".ipynb":    ("ipynb", "ipynb"),
    # Outline
    ".opml":     ("opml", "opml"),
    # TeX family
    ".tex":      ("latex", "latex"),
    ".context":  (None, "context"),
    # Documentation
    ".texi":     (None, "texinfo"),       # GNU TexInfo (canonical extension)
    ".haddock":  ("haddock", "haddock"),
    # Roff / man pages
    ".man":      ("man", "man"),
    ".ms":       (None, "ms"),
    # XML formats
    ".dbk":      ("docbook", "docbook"),  # DocBook (canonical extension)
    ".jats":     ("jats", "jats"),
    ".bits":     ("bits", None),          # BITS: read-only
    ".tei":      (None, "tei"),
    # Wiki markup
    ".mediawiki": ("mediawiki", "mediawiki"),
    ".dokuwiki": ("dokuwiki", "dokuwiki"),
    ".jira":     ("jira", "jira"),
    ".creole":   ("creole", None),       # read-only
    ".vimwiki":  ("vimwiki", None),      # read-only
    ".twiki":    ("twiki", None),        # read-only
    ".tikiwiki": ("tikiwiki", None),     # read-only
    ".xwiki":    (None, "xwiki"),        # write-only
    ".zimwiki":  (None, "zimwiki"),      # write-only
    # Bibliography
    ".bib":      ("bibtex", "bibtex"),
    ".biblatex": ("biblatex", "biblatex"),
    ".csljson":  ("csljson", "csljson"),
    ".ris":      ("ris", None),          # read-only
    ".enl":      ("endnotexml", None),   # read-only (EndNote XML)
    # Page layout
    ".typ":      ("typst", "typst"),
    ".icml":     (None, "icml"),         # InDesign Markup Language
    # Ebook (pandoc handles fb2 fully; epub stays with epub_handler)
    ".fb2":      ("fb2", "fb2"),
    # Slide-deck output formats (write-only HTML/PDF deck flavors)
    ".beamer":   (None, "beamer"),
    ".revealjs": (None, "revealjs"),
    ".slidy":    (None, "slidy"),
    ".slideous": (None, "slideous"),
    ".s5":       (None, "s5"),
    ".dzslides": (None, "dzslides"),
    # Plain ANSI-coloured terminal output
    ".ansi":     (None, "ansi"),
}

SUPPORTED_READ = {ext for ext, (r, _) in _EXT_TO_PANDOC.items() if r is not None}
SUPPORTED_WRITE = {ext for ext, (_, w) in _EXT_TO_PANDOC.items() if w is not None}
DOC_KIND = "pandoc"


@dataclass
class PandocDoc:
    """Pointer to a source document plus its pandoc format name.

    The router's same-handler fast path passes this object straight from
    read() to write() so we can do a single high-fidelity pandoc call
    without parsing through HTML twice.
    """
    src_path: Path
    src_fmt: str  # pandoc format name (e.g. "rst", "ipynb")


# ---------------------------------------------------------------------------
# Public read/write
# ---------------------------------------------------------------------------

def read(path: Path, ext: str, cancel: CancellationToken) -> PandocDoc:
    src_fmt = _read_fmt_for(ext)
    if src_fmt is None:
        raise RuntimeError(f"Pandoc cannot read {ext}.")
    return PandocDoc(src_path=path, src_fmt=src_fmt)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    dst_fmt = _write_fmt_for(ext)
    if dst_fmt is None:
        raise RuntimeError(f"Pandoc cannot write {ext}.")

    if isinstance(doc, PandocDoc):
        # High-fidelity path: pandoc src_fmt -> dst_fmt directly.
        _run_pandoc_file(doc.src_path, doc.src_fmt, path, dst_fmt, cancel)
        return

    # Cross-kind bridge: doc came from a non-pandoc reader (e.g. md,
    # html, docx). Re-emit it as HTML and feed that to pandoc.
    from ..core.intermediate import TextDoc
    if isinstance(doc, TextDoc):
        from ..format_handlers.text_plain import _textdoc_to_html
        html = _textdoc_to_html(doc)
        _run_pandoc_bytes(html.encode("utf-8"), "html", path, dst_fmt, cancel)
        return
    if isinstance(doc, (bytes, bytearray)):
        # Treat as a raw markdown bytes blob — best fallback.
        _run_pandoc_bytes(bytes(doc), "markdown", path, dst_fmt, cancel)
        return
    raise RuntimeError(f"Pandoc writer received unsupported IR type: {type(doc)!r}")


def _read_fmt_for(ext: str) -> Optional[str]:
    pair = _EXT_TO_PANDOC.get(ext.lower())
    return pair[0] if pair else None


def _write_fmt_for(ext: str) -> Optional[str]:
    pair = _EXT_TO_PANDOC.get(ext.lower())
    return pair[1] if pair else None


# ---------------------------------------------------------------------------
# Cross-handler adapter helpers (called by intermediate.ADAPTERS)
# ---------------------------------------------------------------------------

def pandoc_to_textdoc(pd: PandocDoc):
    """Convert a PandocDoc to a TextDoc by routing through HTML.

    Used when the destination handler is text-IR-based (e.g. md, docx via
    docx_handler). Quality is bounded by Vitriol's HTML→TextDoc parser —
    pandoc's HTML output is clean enough that this round-trip preserves
    most structure (headings, lists, tables, links, code, blockquotes).
    """
    from ..format_handlers.text_plain import _html_to_textdoc
    html = _run_pandoc_to_string(pd.src_path, pd.src_fmt, "html")
    return _html_to_textdoc(html)


def textdoc_to_pandoc(doc) -> PandocDoc:
    """Convert a TextDoc to a PandocDoc by emitting HTML and pointing
    pandoc at it. Caller's writer then converts html → target format."""
    from ..core.intermediate import TextDoc
    from ..format_handlers.text_plain import _textdoc_to_html
    import tempfile
    if not isinstance(doc, TextDoc):
        raise RuntimeError(f"textdoc_to_pandoc expected TextDoc, got {type(doc)!r}")
    html = _textdoc_to_html(doc)
    tmp = Path(tempfile.mkstemp(suffix=".html")[1])
    tmp.write_text(html, encoding="utf-8")
    return PandocDoc(src_path=tmp, src_fmt="html")


# ---------------------------------------------------------------------------
# pandoc subprocess plumbing
# ---------------------------------------------------------------------------

_PANDOC_PATH: Optional[Path] = None


def _pandoc_path() -> Path:
    global _PANDOC_PATH
    if _PANDOC_PATH is not None and _PANDOC_PATH.exists():
        return _PANDOC_PATH
    from ..utils.paths import find_pandoc
    found = find_pandoc()
    if found is None:
        raise RuntimeError(
            "pandoc not found. Install via the launcher (auto-fetch on next "
            "launch) or place pandoc(.exe) on PATH or in ./bin/."
        )
    _PANDOC_PATH = found
    return _PANDOC_PATH


def _run_pandoc_file(
    src: Path, src_fmt: str,
    dst: Path, dst_fmt: str,
    cancel: CancellationToken,
) -> None:
    args = [
        str(_pandoc_path()),
        "-f", src_fmt,
        "-t", dst_fmt,
        "-o", str(dst),
        str(src),
    ]
    # Containerized formats (docx/epub/odt/pdf) need pandoc's standalone
    # mode implicitly — pandoc enables it for these. But asking for a
    # standalone document by default for plain text outputs gives nicer
    # results (proper HTML head, proper LaTeX preamble, etc.) without
    # changing semantics for partial fragments.
    if dst_fmt in ("html", "html4", "html5", "latex", "context",
                   "beamer", "revealjs", "s5", "slidy", "slideous", "dzslides",
                   "man", "texinfo", "ms"):
        args.insert(1, "--standalone")
    _run(args, cancel)
    _strip_pandoc_generator(dst, dst_fmt)


def _run_pandoc_bytes(
    src_bytes: bytes, src_fmt: str,
    dst: Path, dst_fmt: str,
    cancel: CancellationToken,
) -> None:
    args = [
        str(_pandoc_path()),
        "-f", src_fmt,
        "-t", dst_fmt,
        "-o", str(dst),
    ]
    if dst_fmt in ("html", "html4", "html5", "latex", "context",
                   "beamer", "revealjs", "s5", "slidy", "slideous", "dzslides",
                   "man", "texinfo", "ms"):
        args.insert(1, "--standalone")
    _run(args, cancel, stdin_bytes=src_bytes)
    _strip_pandoc_generator(dst, dst_fmt)


# ---------------------------------------------------------------------------
# Output post-process
# ---------------------------------------------------------------------------

# Pandoc stamps "Automatically generated by Pandoc X.Y" into the header of
# several output formats. The user didn't ask for our converter to brand
# their files — strip the leakage on the way out.
#
# Patterns by format:
#   roff (man, ms): leading `.\" Automatically generated by Pandoc ...` and
#       the `.\"` blank-comment line that follows.
#   HTML family (html, html4, html5, revealjs, slidy, slideous, s5, dzslides,
#       chunkedhtml): a `<meta name="generator" content="pandoc ..." />` tag
#       in the document head.
# Other formats either don't add identifying metadata, or do so in places
# that aren't trivially strippable (LaTeX preamble, ipynb language_info).
# We can extend this if/when more formats need it.

_HTML_LIKE_FORMATS = {
    "html", "html4", "html5",
    "revealjs", "slidy", "slideous", "s5", "dzslides", "chunkedhtml",
}
_ROFF_FORMATS = {"man", "ms"}
_GENERATOR_META_RE = re.compile(
    r"\s*<meta\s+name=[\"']generator[\"']\s+content=[\"']pandoc[^\"']*[\"']\s*/?>\s*",
    re.IGNORECASE,
)


def _strip_pandoc_generator(path: Path, dst_fmt: str) -> None:
    """Remove pandoc's generator stamp from the output file in place.

    Best-effort — read errors and write errors are silently swallowed
    so a stripping issue never fails an otherwise-successful conversion.
    """
    try:
        if dst_fmt in _ROFF_FORMATS:
            text = path.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            i = 0
            # Roff comment lines start with `.\"`. Pandoc emits one with
            # "Pandoc" in it, then a blank comment, then the real content.
            while i < len(lines) and i < 5:
                stripped = lines[i].lstrip()
                if stripped.startswith('.\\"') and (
                    "Pandoc" in stripped or stripped.rstrip() == '.\\"'
                ):
                    i += 1
                    continue
                break
            if i > 0:
                path.write_text("".join(lines[i:]), encoding="utf-8")
        elif dst_fmt in _HTML_LIKE_FORMATS:
            text = path.read_text(encoding="utf-8")
            new = _GENERATOR_META_RE.sub("\n", text)
            if new != text:
                path.write_text(new, encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # File may be a binary format or otherwise not text-strippable.
        # Leave it alone rather than risking corruption.
        return


def _run_pandoc_to_string(src: Path, src_fmt: str, dst_fmt: str) -> str:
    """Synchronous capture for the cross-kind adapter. No cancellation
    plumbing because adapters are fast and run inside the IR conversion
    step, not the long-running write."""
    creationflags = 0x08000000 if os.name == "nt" else 0
    out = subprocess.check_output(
        [str(_pandoc_path()), "-f", src_fmt, "-t", dst_fmt, str(src)],
        timeout=120,
        creationflags=creationflags,
    )
    return out.decode("utf-8", errors="replace")


def _run(args: list[str], cancel: CancellationToken,
         stdin_bytes: Optional[bytes] = None) -> None:
    creationflags = 0x08000000 if os.name == "nt" else 0
    _log.info("pandoc: %s", " ".join(args))
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE if stdin_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
    )
    cancel.on_cancel(lambda: _terminate(proc))
    try:
        out, err = proc.communicate(input=stdin_bytes, timeout=300)
    except subprocess.TimeoutExpired:
        _terminate(proc)
        raise RuntimeError("pandoc timed out after 5 minutes")
    if cancel.is_set():
        raise CancelledError()
    if proc.returncode != 0:
        tail = (err or b"").decode("utf-8", errors="replace").strip().splitlines()[-10:]
        raise RuntimeError(
            f"pandoc exit {proc.returncode}: {' | '.join(tail) or 'no error message'}"
        )


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass
