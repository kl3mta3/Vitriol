"""Plain-text family: txt, md, html, json, xml, yaml, ini, log, csv, tsv.

Most of these round-trip as text via the binary IR (raw bytes). Two exceptions:
  - csv/tsv use the Tabular IR via stdlib csv module.
  - md/html use TextDoc; their parsers live in markdown_parser.py and below.

This module is a single registration surface — different DOC_KIND per ext is
handled by splitting the handlers into multiple sub-handlers via subclasses
of a tiny dispatch helper. We pick which IR the call uses based on `ext`.
"""
from __future__ import annotations
import json
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
from typing import List

from ..core.intermediate import (
    Block, CodeBlock, Heading, HorizontalRule, Image, List_, Paragraph,
    Run, Table, Tabular, TextDoc, Blockquote,
)
from ..utils.cancellation import CancellationToken
from . import charset

# This module exposes multiple DOC_KINDs. The registry currently keys handlers
# by ext on a single DOC_KIND, so we split into per-ext "shim" attributes by
# listing both reads/writes here and dispatching internally.
# Note: json/yaml/ini/toml/jsonl/env are reserved for `config_handler.py`
# which does real semantic conversion among them. text_plain still owns the
# *prose* family below and the catch-all xml passthrough.
SUPPORTED_READ = {".txt", ".log", ".xml", ".html", ".py"}
SUPPORTED_WRITE = {".txt", ".log", ".xml", ".html", ".py"}
DOC_KIND = "text"

# Streaming pass-through: plain-text → plain-text conversion above the
# streaming threshold can be a chunked file copy with optional encoding
# normalization. Rich-text round-trips via TextDoc (HTML, etc.) stay on the
# whole-file path because the IR tree must be in memory.
STREAMABLE = True
_STREAM_PASSTHROUGH_EXTS = {".txt", ".log", ".xml", ".py"}


def can_stream(src_ext: str, dst_ext: str) -> bool:
    """Streaming pass-through is only safe when both ends are bytes-compatible
    plain text (no HTML→TextDoc rebuild, no CSV/TSV tabular structure)."""
    s = src_ext.lower(); d = dst_ext.lower()
    return s in _STREAM_PASSTHROUGH_EXTS and d in _STREAM_PASSTHROUGH_EXTS


def stream_convert(src: Path, dst: Path, src_ext: str, dst_ext: str,
                    cancel, progress) -> None:
    """Chunked file copy. Used by router when src is large AND both ends
    are plain-text-compatible. Bytes are preserved exactly."""
    import shutil
    from ..core.config import CHUNK_SIZE
    total = src.stat().st_size
    written = 0
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            cancel.check()
            buf = fi.read(CHUNK_SIZE)
            if not buf:
                break
            fo.write(buf)
            written += len(buf)
            if total:
                progress(min(0.99, written / total))
    progress(1.0)


def read(path: Path, ext: str, cancel: CancellationToken):
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    if ext == ".html":
        return _html_to_textdoc(text)
    return _plain_to_textdoc(text)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if isinstance(doc, (bytes, bytearray)):
        path.write_bytes(bytes(doc))
        return
    if isinstance(doc, Tabular):
        from ..core.intermediate import tabular_to_textdoc
        doc = tabular_to_textdoc(doc)
    if not isinstance(doc, TextDoc):
        path.write_bytes(str(doc).encode("utf-8"))
        return
    if ext == ".json":
        path.write_text(_textdoc_to_json(doc), encoding="utf-8")
        return
    if ext == ".html":
        from ..core.bundle_writer import write_with_bundle
        write_with_bundle(
            doc, path,
            flat_render=lambda d, p: p.write_text(_textdoc_to_html(d), encoding="utf-8"),
        )
        return
    # Plain-text default for txt/log/ini/xml/yaml/py: flatten with bundle when images present.
    from ..core.bundle_writer import write_with_bundle
    write_with_bundle(
        doc, path,
        flat_render=lambda d, p: p.write_text(_textdoc_to_plain_with_hrefs(d), encoding="utf-8"),
    )


def _textdoc_to_plain_with_hrefs(doc: TextDoc) -> str:
    """Like intermediate.textdoc_to_plain but renders Image blocks using
    `href` when set (bundle mode) so the text references the saved file."""
    parts: List[str] = []
    for blk in doc.blocks:
        parts.append(_block_to_plain_with_hrefs(blk, depth=0))
    return "\n\n".join(p for p in parts if p)


def _block_to_plain_with_hrefs(blk, depth: int) -> str:
    if isinstance(blk, Image):
        ref = blk.href or blk.alt or ""
        return f"[image: {ref}]" if ref else "[image]"
    if isinstance(blk, List_):
        lines = []
        for i, item in enumerate(blk.items, 1):
            prefix = f"{i}. " if blk.ordered else "- "
            inner = "\n".join(_block_to_plain_with_hrefs(b, depth + 1) for b in item)
            lines.append("  " * depth + prefix + inner)
        return "\n".join(lines)
    if isinstance(blk, Table):
        from ..core.intermediate import _block_to_plain  # reuse for cells
        lines = []
        for row in blk.rows:
            cells = [" ".join(_block_to_plain_with_hrefs(b, 0) for b in cell).strip() for cell in row]
            lines.append(" | ".join(cells))
        return "\n".join(lines)
    if isinstance(blk, Blockquote):
        inner = "\n".join(_block_to_plain_with_hrefs(b, depth) for b in blk.blocks)
        return "\n".join("> " + ln for ln in inner.splitlines())
    # Fall through to the standard renderer for non-image blocks.
    from ..core.intermediate import _block_to_plain
    return _block_to_plain(blk, depth)


# ---- Plain text ----------------------------------------------------------

def _plain_to_textdoc(text: str) -> TextDoc:
    blocks: List[Block] = []
    for chunk in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        chunk = chunk.strip("\n")
        if chunk:
            blocks.append(Paragraph(runs=[Run(text=chunk)]))
    return TextDoc(blocks=blocks)


def _textdoc_to_json(doc: TextDoc) -> str:
    """Best-effort JSON dump of the IR for debugging / lossless export."""
    def serialize_runs(rs):
        return [{"text": r.text, "bold": r.bold, "italic": r.italic, "underline": r.underline,
                 "code": r.code, "href": r.href} for r in rs]

    def serialize(b):
        if isinstance(b, Heading):
            return {"type": "heading", "level": b.level, "runs": serialize_runs(b.runs)}
        if isinstance(b, Paragraph):
            return {"type": "paragraph", "runs": serialize_runs(b.runs)}
        if isinstance(b, List_):
            return {"type": "list", "ordered": b.ordered,
                    "items": [[serialize(x) for x in item] for item in b.items]}
        if isinstance(b, Table):
            return {"type": "table",
                    "rows": [[[serialize(x) for x in cell] for cell in row] for row in b.rows]}
        if isinstance(b, Image):
            return {"type": "image", "alt": b.alt, "mime": b.mime, "size": len(b.data),
                    "width": b.width, "height": b.height}
        if isinstance(b, CodeBlock):
            return {"type": "code", "lang": b.lang, "text": b.text}
        if isinstance(b, HorizontalRule):
            return {"type": "hr"}
        if isinstance(b, Blockquote):
            return {"type": "blockquote", "blocks": [serialize(x) for x in b.blocks]}
        return {"type": "unknown"}

    out = {"metadata": doc.metadata, "blocks": [serialize(b) for b in doc.blocks]}
    return json.dumps(out, ensure_ascii=False, indent=2)


# ---- HTML ----------------------------------------------------------------

class _HtmlReader(HTMLParser):
    """Minimal HTML→TextDoc. Recognizes the structural tags we care about."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[Block] = []
        self._stack_runs: List[Run] = []
        self._cur_runs: List[Run] = []
        self._fmt: dict = {"bold": False, "italic": False, "underline": False, "code": False, "href": None}
        self._in_block = None  # "p" | "h1".."h6" | "li" | "code" | "pre" | "blockquote" | None
        self._heading_level = 0
        self._list_stack: List[List_] = []
        self._li_blocks: List[List[Block]] = []  # parallel to list_stack: collected items
        self._pre_text: List[str] = []
        self._table_rows: List[List[List[Block]]] = []
        self._tr: List[List[Block]] | None = None
        self._td_runs: List[Run] | None = None
        self._in_td = False
        self._title = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        t = tag.lower()
        if t in ("b", "strong"):
            self._fmt["bold"] = True
        elif t in ("i", "em"):
            self._fmt["italic"] = True
        elif t == "u":
            self._fmt["underline"] = True
        elif t == "code":
            self._fmt["code"] = True
        elif t == "a":
            self._fmt["href"] = a.get("href")
        elif t == "br":
            self._cur_runs.append(Run(text="\n"))
        elif t == "p":
            self._flush_paragraph()
            self._in_block = "p"
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_paragraph()
            self._in_block = t
            self._heading_level = int(t[1])
        elif t in ("ul", "ol"):
            self._flush_paragraph()
            self._list_stack.append(List_(ordered=(t == "ol"), items=[]))
            self._li_blocks.append([])
        elif t == "li":
            self._flush_paragraph()
            # Start a fresh paragraph for this li
            self._in_block = "li"
        elif t == "blockquote":
            self._flush_paragraph()
            self._in_block = "blockquote"
        elif t == "hr":
            self._flush_paragraph()
            self.blocks.append(HorizontalRule())
        elif t == "pre":
            self._in_block = "pre"
            self._pre_text = []
        elif t == "img":
            alt = a.get("alt") or ""
            self._cur_runs.append(Run(text=f"[image: {alt}]"))
        elif t == "table":
            self._flush_paragraph()
            self._table_rows = []
        elif t == "tr":
            self._tr = []
        elif t in ("td", "th"):
            self._in_td = True
            self._td_runs = []
        elif t == "title":
            self._in_block = "title"

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("b", "strong"):
            self._fmt["bold"] = False
        elif t in ("i", "em"):
            self._fmt["italic"] = False
        elif t == "u":
            self._fmt["underline"] = False
        elif t == "code":
            self._fmt["code"] = False
        elif t == "a":
            self._fmt["href"] = None
        elif t == "p":
            self._flush_paragraph()
            self._in_block = None
        elif t in ("h1", "h2", "h3", "h4", "h5", "h6"):
            if self._cur_runs:
                self.blocks.append(Heading(level=self._heading_level, runs=self._cur_runs))
                self._cur_runs = []
            self._in_block = None
        elif t == "li":
            if self._list_stack and self._cur_runs:
                self._li_blocks[-1].append(Paragraph(runs=self._cur_runs))
                self._cur_runs = []
            self._in_block = None
        elif t in ("ul", "ol"):
            if self._list_stack:
                lst = self._list_stack.pop()
                items = self._li_blocks.pop()
                # `items` is a flat list of paragraphs from each li; group as separate items
                lst.items = [[p] for p in items]
                if self._list_stack:
                    self._li_blocks[-1].append(lst)
                else:
                    self.blocks.append(lst)
        elif t == "blockquote":
            if self._cur_runs:
                self.blocks.append(Blockquote(blocks=[Paragraph(runs=self._cur_runs)]))
                self._cur_runs = []
            self._in_block = None
        elif t == "pre":
            self.blocks.append(CodeBlock(lang=None, text="".join(self._pre_text)))
            self._pre_text = []
            self._in_block = None
        elif t in ("td", "th"):
            self._in_td = False
            if self._tr is not None and self._td_runs is not None:
                self._tr.append([Paragraph(runs=self._td_runs)])
            self._td_runs = None
        elif t == "tr":
            if self._tr is not None:
                self._table_rows.append(self._tr)
                self._tr = None
        elif t == "table":
            if self._table_rows:
                self.blocks.append(Table(rows=self._table_rows))
            self._table_rows = []
        elif t == "title":
            self._in_block = None

    def handle_data(self, data):
        if not data:
            return
        if self._in_block == "pre":
            self._pre_text.append(data)
            return
        if self._in_block == "title":
            self._title = (self._title or "") + data
            return
        if self._in_td and self._td_runs is not None:
            self._td_runs.append(Run(text=data, **self._run_kwargs()))
            return
        self._cur_runs.append(Run(text=data, **self._run_kwargs()))

    def _run_kwargs(self) -> dict:
        return dict(bold=self._fmt["bold"], italic=self._fmt["italic"],
                    underline=self._fmt["underline"], code=self._fmt["code"],
                    href=self._fmt["href"])

    def _flush_paragraph(self) -> None:
        if self._cur_runs and self._in_block in (None, "p"):
            self.blocks.append(Paragraph(runs=self._cur_runs))
            self._cur_runs = []


def _html_to_textdoc(text: str) -> TextDoc:
    parser = _HtmlReader()
    parser.feed(text)
    parser._flush_paragraph()
    doc = TextDoc(blocks=parser.blocks)
    if parser._title:
        doc.metadata["title"] = parser._title.strip()
    return doc


def _runs_to_html(runs: List[Run]) -> str:
    out = []
    for r in runs:
        s = html_escape(r.text)
        if r.code:
            s = f"<code>{s}</code>"
        if r.bold:
            s = f"<strong>{s}</strong>"
        if r.italic:
            s = f"<em>{s}</em>"
        if r.underline:
            s = f"<u>{s}</u>"
        if r.href:
            s = f'<a href="{html_escape(r.href, quote=True)}">{s}</a>'
        out.append(s)
    return "".join(out)


def _block_to_html(blk, depth: int = 0) -> str:
    if isinstance(blk, Heading):
        lvl = max(1, min(6, blk.level))
        return f"<h{lvl}>{_runs_to_html(blk.runs)}</h{lvl}>"
    if isinstance(blk, Paragraph):
        return f"<p>{_runs_to_html(blk.runs)}</p>"
    if isinstance(blk, List_):
        tag = "ol" if blk.ordered else "ul"
        items = []
        for item in blk.items:
            inner = "".join(_block_to_html(b, depth + 1) for b in item)
            items.append(f"<li>{inner}</li>")
        return f"<{tag}>{''.join(items)}</{tag}>"
    if isinstance(blk, Table):
        rows_html = []
        for row in blk.rows:
            cells = "".join(f"<td>{''.join(_block_to_html(b) for b in cell)}</td>" for cell in row)
            rows_html.append(f"<tr>{cells}</tr>")
        return f"<table>{''.join(rows_html)}</table>"
    if isinstance(blk, Image):
        alt = html_escape(blk.alt or "")
        # Bundle mode: href set by bundle_writer to a relative path like
        # "images/image1.png". Otherwise embed as data URI for self-contained HTML.
        if blk.href:
            return f'<img src="{html_escape(blk.href, quote=True)}" alt="{alt}"/>'
        if blk.data:
            import base64
            b64 = base64.b64encode(blk.data).decode("ascii")
            return f'<img src="data:{blk.mime};base64,{b64}" alt="{alt}"/>'
        return f'<img alt="{alt}"/>'
    if isinstance(blk, CodeBlock):
        cls = f' class="language-{html_escape(blk.lang)}"' if blk.lang else ""
        return f"<pre><code{cls}>{html_escape(blk.text)}</code></pre>"
    if isinstance(blk, HorizontalRule):
        return "<hr/>"
    if isinstance(blk, Blockquote):
        inner = "".join(_block_to_html(b) for b in blk.blocks)
        return f"<blockquote>{inner}</blockquote>"
    return ""


def _textdoc_to_html(doc: TextDoc) -> str:
    title = html_escape(doc.metadata.get("title", "Document"))
    body = "\n".join(_block_to_html(b) for b in doc.blocks)
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"/>'
        f"<title>{title}</title></head><body>\n{body}\n</body></html>\n"
    )


