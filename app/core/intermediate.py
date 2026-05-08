"""Intermediate representations and IR-to-IR adapters.

Two IRs:
  TextDoc  — prose/document formats (docx, pdf, epub, md, html, rtf, odt, pptx, txt).
  Tabular  — csv, tsv, xlsx.
Pure formats (json, xml, yaml, ini, log) round-trip through plain text.

Block variants are dataclasses, not a class hierarchy. Match by isinstance.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Union, List, Optional


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    code: bool = False
    href: Optional[str] = None


@dataclass
class Heading:
    level: int  # 1..6
    runs: List[Run]


@dataclass
class Paragraph:
    runs: List[Run]


@dataclass
class List_:
    ordered: bool
    items: List[List["Block"]]  # each item is a list of blocks (supports nesting)


@dataclass
class Table:
    rows: List[List[List["Block"]]]  # rows[r][c] = list of blocks for that cell


@dataclass
class Image:
    data: bytes
    mime: str
    alt: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    href: Optional[str] = None  # Source ref (used for bundle-aware md round-trip)


@dataclass
class CodeBlock:
    lang: Optional[str]
    text: str


@dataclass
class HorizontalRule:
    pass


@dataclass
class Blockquote:
    blocks: List["Block"]


Block = Union[Heading, Paragraph, List_, Table, Image, CodeBlock, HorizontalRule, Blockquote]


@dataclass
class TextDoc:
    blocks: List[Block] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)  # title, author, lang


CellValue = Union[str, int, float, bool, datetime, None]


@dataclass
class Cell:
    value: CellValue = None
    formula: Optional[str] = None


@dataclass
class Sheet:
    name: str
    rows: List[List[Cell]] = field(default_factory=list)


@dataclass
class Tabular:
    sheets: List[Sheet] = field(default_factory=list)


# ---- Adapters between IRs ------------------------------------------------

def tabular_to_textdoc(t: Tabular) -> TextDoc:
    """Each sheet becomes a Heading + a Table block."""
    blocks: List[Block] = []
    for sheet in t.sheets:
        blocks.append(Heading(level=1, runs=[Run(text=sheet.name)]))
        if not sheet.rows:
            continue
        rows: List[List[List[Block]]] = []
        for row in sheet.rows:
            cells: List[List[Block]] = []
            for c in row:
                txt = "" if c.value is None else str(c.value)
                cells.append([Paragraph(runs=[Run(text=txt)])])
            rows.append(cells)
        blocks.append(Table(rows=rows))
    return TextDoc(blocks=blocks)


def textdoc_to_tabular(d: TextDoc) -> Tabular:
    """Pull out Table blocks; each becomes a sheet. Prose is dropped (lossy)."""
    sheets: List[Sheet] = []
    current_name = "Sheet1"
    table_idx = 0
    for blk in d.blocks:
        if isinstance(blk, Heading):
            current_name = _runs_to_text(blk.runs).strip() or f"Sheet{len(sheets)+1}"
        elif isinstance(blk, Table):
            rows: List[List[Cell]] = []
            for row in blk.rows:
                rows.append([Cell(value=_blocks_to_text(cell)) for cell in row])
            name = current_name if current_name else f"Sheet{table_idx+1}"
            # Avoid duplicate sheet names
            if any(s.name == name for s in sheets):
                name = f"{name} ({table_idx+1})"
            sheets.append(Sheet(name=name[:31], rows=rows))
            table_idx += 1
            current_name = ""
    if not sheets:
        sheets.append(Sheet(name="Sheet1", rows=[]))
    return Tabular(sheets=sheets)


def textdoc_to_plain(d: TextDoc) -> str:
    """Flatten to a plain-text string. Drops images."""
    parts: List[str] = []
    for blk in d.blocks:
        parts.append(_block_to_plain(blk, depth=0))
    return "\n\n".join(p for p in parts if p)


def plain_to_textdoc(text: str) -> TextDoc:
    """Split blank-line-separated paragraphs into Paragraph blocks."""
    blocks: List[Block] = []
    for chunk in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        blocks.append(Paragraph(runs=[Run(text=chunk)]))
    return TextDoc(blocks=blocks)


def _runs_to_text(runs: List[Run]) -> str:
    return "".join(r.text for r in runs)


def _blocks_to_text(blocks: List[Block]) -> str:
    return " ".join(_block_to_plain(b, depth=0) for b in blocks).strip()


def _block_to_plain(blk: Block, depth: int) -> str:
    if isinstance(blk, Heading):
        return _runs_to_text(blk.runs)
    if isinstance(blk, Paragraph):
        return _runs_to_text(blk.runs)
    if isinstance(blk, List_):
        lines = []
        for i, item in enumerate(blk.items, 1):
            prefix = f"{i}. " if blk.ordered else "- "
            inner = "\n".join(_block_to_plain(b, depth + 1) for b in item)
            lines.append("  " * depth + prefix + inner)
        return "\n".join(lines)
    if isinstance(blk, Table):
        lines = []
        for row in blk.rows:
            lines.append(" | ".join(_blocks_to_text(c) for c in row))
        return "\n".join(lines)
    if isinstance(blk, Image):
        return f"[image: {blk.alt}]" if blk.alt else "[image]"
    if isinstance(blk, CodeBlock):
        return blk.text
    if isinstance(blk, HorizontalRule):
        return "----"
    if isinstance(blk, Blockquote):
        inner = "\n".join(_block_to_plain(b, depth) for b in blk.blocks)
        return "\n".join("> " + ln for ln in inner.splitlines())
    return ""


ADAPTERS = {
    ("tabular", "text"): tabular_to_textdoc,
    ("text", "tabular"): textdoc_to_tabular,
    ("text", "binary"): lambda d: textdoc_to_plain(d).encode("utf-8"),
    ("binary", "text"): lambda b: plain_to_textdoc(
        b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else str(b)
    ),
    ("tabular", "binary"): lambda t: textdoc_to_plain(tabular_to_textdoc(t)).encode("utf-8"),
    ("binary", "tabular"): lambda b: textdoc_to_tabular(
        plain_to_textdoc(b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else str(b))
    ),
}


# ---------------------------------------------------------------------------
# Cross-category adapter: image bytes → TextDoc with a single Image block.
# Lets the router treat image sources as legitimate inputs for document
# writers (pdf_write, markdown_parser, text_plain). Each writer renders the
# Image block however it likes — pdf_write embeds it as an /XObject /Image,
# markdown_parser writes a bundle layout with the image saved alongside,
# text_plain falls back to a "[image]" placeholder.
# ---------------------------------------------------------------------------

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff",
    ".heic": "image/heic", ".heif": "image/heic",
    ".svg": "image/svg+xml",
}


def image_bytes_to_textdoc(src_bytes: bytes, src_ext: str, alt: str = "") -> TextDoc:
    """Wrap a raster image as a single-block TextDoc. Used by the router
    when the source is an image and the target is a document format.

    Sets `_vitriol_origin` in the TextDoc metadata so reversibility-aware
    writers (pdf_write, docx_handler, epub_handler) can stash the original
    source bytes alongside the rendered output (PDF trailer / DOCX ZIP entry).
    Lets PNG -> PDF -> PNG round-trip byte-perfect.
    """
    ext = src_ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    mime = _EXT_TO_MIME.get(ext, "image/png")
    doc = TextDoc(blocks=[Image(data=src_bytes, mime=mime, alt=alt or f"image{ext}")])
    doc.metadata["_vitriol_origin"] = {"bytes": src_bytes, "ext": ext}
    return doc
