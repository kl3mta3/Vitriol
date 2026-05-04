"""Markdown handler. CommonMark subset + GFM tables.

Scope:
  - ATX headings (#, ##, ...), setext headings (=== / ---).
  - Paragraphs separated by blank lines.
  - Bold (**, __), italic (*, _), code (`...`), links [text](url),
    images ![alt](url), inline HTML passes through as text.
  - Bullet lists (-, *, +) and ordered lists (1. 2.) with nesting via indent.
  - Fenced code blocks (``` and ~~~) with optional info string.
  - Blockquotes (>).
  - Horizontal rules (---, ***, ___).
  - GFM tables (| col | col |\n|---|---|\n| ... |).

Out of scope: HTML passthrough rendering, footnotes, definition lists,
reference-style links, autolinks.
"""
from __future__ import annotations
import re
from html import escape as html_escape
from pathlib import Path
from typing import List, Optional

from ..core.intermediate import (
    Block, Blockquote, CodeBlock, Heading, HorizontalRule, Image, List_,
    Paragraph, Run, Table, TextDoc,
)
from ..utils.cancellation import CancellationToken
from . import charset

SUPPORTED_READ = {".md"}
SUPPORTED_WRITE = {".md"}
DOC_KIND = "text"


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    doc = parse(text)
    # Bundle-aware: if the .md sits next to an images/ folder (either as
    # `<stem>/<stem>.md` + `<stem>/images/` or flat `foo.md` + `images/`),
    # resolve image refs to bytes so downstream writers can embed them.
    _resolve_image_refs(doc, path)
    return doc


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, TextDoc):
        from ..core.intermediate import plain_to_textdoc
        if isinstance(doc, (bytes, bytearray)):
            doc = plain_to_textdoc(bytes(doc).decode("utf-8", errors="replace"))
        else:
            doc = plain_to_textdoc(str(doc))

    # Bundle layout when images are present: <parent>/<stem>/<stem>.md +
    # <parent>/<stem>/images/imageN.<ext>. Flat output otherwise.
    # bundle_writer mutates each Image's href to "images/<name>"; the
    # markdown render reads Image.href when emitting `![alt](href)`.
    from ..core.bundle_writer import write_with_bundle
    write_with_bundle(
        doc, path,
        flat_render=lambda d, p: p.write_text(render(d), encoding="utf-8"),
    )


def _resolve_image_refs(doc: TextDoc, md_path: Path) -> None:
    """Walk Image blocks; if href resolves to a real file under the .md's
    neighborhood (same dir, ./images/, or parent's ./images/), load bytes
    into Image.data so downstream writers can embed them."""
    base = md_path.parent
    def visit(blocks):
        for b in blocks:
            if isinstance(b, Image) and not b.data and b.href:
                href = b.href.strip()
                if not href or href.startswith("http://") or href.startswith("https://"):
                    continue
                for d in (base, base / "images", base.parent / "images"):
                    p = (d / href).resolve() if not Path(href).is_absolute() else Path(href)
                    # If href already points to a sub-path, also try base/href
                    candidates = {p, base / href}
                    for cand in candidates:
                        if cand.exists() and cand.is_file():
                            try:
                                b.data = cand.read_bytes()
                                b.mime = _mime_for_ext(cand.suffix.lower())
                            except OSError:
                                pass
                            break
                    if b.data:
                        break
            elif isinstance(b, List_):
                for item in b.items:
                    visit(item)
            elif isinstance(b, Blockquote):
                visit(b.blocks)
            elif isinstance(b, Table):
                for row in b.rows:
                    for cell in row:
                        visit(cell)
    visit(doc.blocks)


def _mime_for_ext(ext: str) -> str:
    return {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".tiff": "image/tiff", ".svg": "image/svg+xml",
    }.get(ext, "image/png")


# ---- Parsing -------------------------------------------------------------

_HR_RE = re.compile(r"^[ \t]{0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_ATX_RE = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)[ \t]*([^\s`~]*)[ \t]*$")
_BULLET_RE = re.compile(r"^([ \t]*)([-*+])[ \t]+(.*)$")
_ORDERED_RE = re.compile(r"^([ \t]*)(\d{1,9})[.)][ \t]+(.*)$")
_QUOTE_RE = re.compile(r"^[ \t]{0,3}>[ ]?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def parse(text: str) -> TextDoc:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: List[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Blank
        if not stripped:
            i += 1
            continue
        # Fenced code block
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)[0]  # ` or ~
            fence_len = len(m.group(1))
            info = m.group(2) or None
            code_lines: List[str] = []
            i += 1
            close_re = re.compile(rf"^[ \t]{{0,3}}{re.escape(fence)}{{{fence_len},}}[ \t]*$")
            while i < len(lines) and not close_re.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append(CodeBlock(lang=info, text="\n".join(code_lines)))
            continue
        # ATX heading
        m = _ATX_RE.match(line)
        if m:
            level = len(m.group(1))
            blocks.append(Heading(level=level, runs=_inline(m.group(2))))
            i += 1
            continue
        # Setext heading (line followed by ===/---)
        if i + 1 < len(lines) and stripped:
            nxt = lines[i + 1].strip()
            if nxt and set(nxt) <= {"="} and len(nxt) >= 1:
                blocks.append(Heading(level=1, runs=_inline(stripped)))
                i += 2
                continue
            if nxt and set(nxt) <= {"-"} and len(nxt) >= 1 and not _HR_RE.match(lines[i + 1]):
                # Setext h2 — but only if current line isn't itself a horizontal rule context
                blocks.append(Heading(level=2, runs=_inline(stripped)))
                i += 2
                continue
        # HR
        if _HR_RE.match(line):
            blocks.append(HorizontalRule())
            i += 1
            continue
        # Blockquote
        if _QUOTE_RE.match(line):
            quote_lines: List[str] = []
            while i < len(lines) and (_QUOTE_RE.match(lines[i]) or (lines[i].strip() and quote_lines)):
                m = _QUOTE_RE.match(lines[i])
                if m:
                    quote_lines.append(m.group(1))
                else:
                    quote_lines.append(lines[i])
                i += 1
            inner = parse("\n".join(quote_lines))
            blocks.append(Blockquote(blocks=inner.blocks))
            continue
        # GFM table
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            header = _split_table_row(line)
            i += 2  # skip separator
            rows: List[List[List[Block]]] = [[[Paragraph(runs=_inline(c))] for c in header]]
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row_cells = _split_table_row(lines[i])
                rows.append([[Paragraph(runs=_inline(c))] for c in row_cells])
                i += 1
            blocks.append(Table(rows=rows))
            continue
        # Lists
        m_b = _BULLET_RE.match(line)
        m_o = _ORDERED_RE.match(line)
        if m_b or m_o:
            consumed, lst = _parse_list(lines, i)
            blocks.append(lst)
            i = consumed
            continue
        # Standalone image: a line whose entire content is `![alt](src)`
        m_img = re.match(r"^\s*!\[([^\]]*)\]\(([^)]+)\)\s*$", line)
        if m_img:
            blocks.append(Image(data=b"", mime="", alt=m_img.group(1), href=m_img.group(2)))
            i += 1
            continue
        # Paragraph: collect until blank or block-starting line
        para_lines = [stripped]
        i += 1
        while i < len(lines):
            ln = lines[i]
            s = ln.strip()
            if not s:
                break
            if _ATX_RE.match(ln) or _FENCE_RE.match(ln) or _HR_RE.match(ln) \
               or _BULLET_RE.match(ln) or _ORDERED_RE.match(ln) or _QUOTE_RE.match(ln):
                break
            para_lines.append(s)
            i += 1
        blocks.append(Paragraph(runs=_inline(" ".join(para_lines))))
    return TextDoc(blocks=blocks)


def _split_table_row(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    parts = [p.strip() for p in s.split("|")]
    return parts


def _parse_list(lines: List[str], start: int) -> tuple[int, List_]:
    """Parse a (possibly nested) list starting at lines[start]. Returns (next_index, List_)."""
    first = lines[start]
    m_b = _BULLET_RE.match(first)
    m_o = _ORDERED_RE.match(first)
    ordered = m_o is not None
    base_indent = len((m_b or m_o).group(1))
    items: List[List[Block]] = []
    i = start
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            # Allow a blank line between items if next non-blank is a list item at same indent
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                break
            m = _BULLET_RE.match(lines[j]) or _ORDERED_RE.match(lines[j])
            if not m or len(m.group(1)) != base_indent:
                break
            i = j
            continue
        m = _BULLET_RE.match(ln) if not ordered else _ORDERED_RE.match(ln)
        if not m or len(m.group(1)) != base_indent:
            # Mixed-indent or different marker: stop here
            break
        # Collect this item: the marker line + any continuation/nested lines
        content_indent = base_indent + len(m.group(2)) + 1  # marker + space
        item_lines = [m.group(3)]
        i += 1
        while i < len(lines):
            ln2 = lines[i]
            if not ln2.strip():
                # blank — peek ahead; if next non-blank is more indented than base, continue item
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j >= len(lines):
                    break
                indent = len(lines[j]) - len(lines[j].lstrip())
                if indent <= base_indent:
                    break
                item_lines.append("")
                i += 1
                continue
            indent = len(ln2) - len(ln2.lstrip())
            if indent <= base_indent and (_BULLET_RE.match(ln2) or _ORDERED_RE.match(ln2)):
                # next list item at same level
                break
            if indent < content_indent and not (_BULLET_RE.match(ln2) or _ORDERED_RE.match(ln2)):
                # de-dented prose — end of this item
                break
            item_lines.append(ln2[content_indent:] if indent >= content_indent else ln2.lstrip())
            i += 1
        # Recursively parse this item's body as block-level Markdown
        sub_doc = parse("\n".join(item_lines))
        items.append(sub_doc.blocks if sub_doc.blocks else [Paragraph(runs=[])])
    return i, List_(ordered=ordered, items=items)


# ---- Inline parsing ------------------------------------------------------

_INLINE_TOKEN_RE = re.compile(
    r"(?P<image>!\[(?P<alt>[^\]]*)\]\((?P<isrc>[^)]+)\))"
    r"|(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<lhref>[^)]+)\))"
    r"|(?P<bold>\*\*([^*]+?)\*\*)"
    r"|(?P<bold2>__([^_]+?)__)"
    r"|(?P<italic>\*([^*]+?)\*)"
    r"|(?P<italic2>_([^_]+?)_)"
    r"|(?P<code>`([^`]+?)`)"
)


def _inline(text: str) -> List[Run]:
    runs: List[Run] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            runs.append(Run(text=text[pos:m.start()]))
        if m.group("image"):
            runs.append(Run(text=f"[image: {m.group('alt')}]"))
        elif m.group("link"):
            runs.append(Run(text=m.group("ltext"), href=m.group("lhref")))
        elif m.group("bold"):
            runs.append(Run(text=m.group(0)[2:-2], bold=True))
        elif m.group("bold2"):
            runs.append(Run(text=m.group(0)[2:-2], bold=True))
        elif m.group("italic"):
            runs.append(Run(text=m.group(0)[1:-1], italic=True))
        elif m.group("italic2"):
            runs.append(Run(text=m.group(0)[1:-1], italic=True))
        elif m.group("code"):
            runs.append(Run(text=m.group(0)[1:-1], code=True))
        pos = m.end()
    if pos < len(text):
        runs.append(Run(text=text[pos:]))
    return runs or [Run(text="")]


# ---- Rendering -----------------------------------------------------------

def render(doc: TextDoc, image_hrefs: dict | None = None) -> str:
    """Render TextDoc to Markdown text. `image_hrefs` maps id(Image) → href
    string, used by bundle output to redirect Image blocks to their on-disk
    locations under images/."""
    parts: List[str] = []
    for blk in doc.blocks:
        s = _render_block(blk, depth=0, image_hrefs=image_hrefs)
        if s:
            parts.append(s)
    return "\n\n".join(parts) + "\n"


def _render_runs(runs: List[Run]) -> str:
    out = []
    for r in runs:
        s = r.text
        if r.code:
            s = f"`{s}`"
        if r.bold:
            s = f"**{s}**"
        if r.italic:
            s = f"*{s}*"
        if r.href:
            s = f"[{s}]({r.href})"
        out.append(s)
    return "".join(out)


def _render_block(blk, depth: int, image_hrefs: dict | None = None) -> str:
    if isinstance(blk, Heading):
        lvl = max(1, min(6, blk.level))
        return ("#" * lvl) + " " + _render_runs(blk.runs)
    if isinstance(blk, Paragraph):
        return _render_runs(blk.runs)
    if isinstance(blk, List_):
        lines = []
        for n, item in enumerate(blk.items, 1):
            marker = f"{n}. " if blk.ordered else "- "
            indent = "  " * depth
            inner_blocks = [_render_block(b, depth + 1, image_hrefs) for b in item]
            inner = ("\n" + indent + "  ").join(b for b in inner_blocks if b)
            lines.append(indent + marker + inner)
        return "\n".join(lines)
    if isinstance(blk, Table):
        if not blk.rows:
            return ""
        def row_text(row):
            return "| " + " | ".join(_runs_from_cell(cell).replace("|", "\\|") for cell in row) + " |"
        lines = [row_text(blk.rows[0])]
        ncols = len(blk.rows[0])
        lines.append("|" + "|".join([" --- "] * ncols) + "|")
        for row in blk.rows[1:]:
            row = list(row) + [[Paragraph(runs=[Run(text="")])]] * max(0, ncols - len(row))
            lines.append(row_text(row[:ncols]))
        return "\n".join(lines)
    if isinstance(blk, Image):
        href = (image_hrefs or {}).get(id(blk)) or blk.href or ""
        return f"![{blk.alt}]({href})"
    if isinstance(blk, CodeBlock):
        info = blk.lang or ""
        return f"```{info}\n{blk.text}\n```"
    if isinstance(blk, HorizontalRule):
        return "---"
    if isinstance(blk, Blockquote):
        inner = "\n".join(_render_block(b, depth, image_hrefs) for b in blk.blocks)
        return "\n".join("> " + ln for ln in inner.splitlines())
    return ""


def _runs_from_cell(cell_blocks) -> str:
    parts = []
    for b in cell_blocks:
        if isinstance(b, Paragraph):
            parts.append(_render_runs(b.runs))
        else:
            parts.append(_render_block(b, 0))
    return " ".join(p for p in parts if p)
