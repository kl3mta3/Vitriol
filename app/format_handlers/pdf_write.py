"""Minimal PDF writer — recoded against the ISO 32000-1 spec.

Scope (per v1 plan, locked tight):
  - Text-only output. Uses one of the 14 standard PDF fonts (Helvetica family)
    by default for reliability; if resources/DejaVuSans.ttf is present, the
    writer embeds it as a TrueType font so the output supports Latin Extended,
    Greek, Cyrillic, and other Unicode coverage DejaVu provides.
  - Auto line wrap, automatic page breaks. Headings render as larger bold text.
  - Lists: bullet/number prefix + indent.
  - Tables: rendered as a fixed-width monospaced grid (no border art in v1).
  - Images: rendered as `[image: alt]` placeholder text.
  - Code blocks: monospaced, preserved whitespace.

Not supported in v1: hyperlinks, bookmarks, page numbers, header/footer,
form fields, embedded images.

The writer assembles the PDF as a sequence of indirect objects, builds the
xref table by tracking byte offsets, and emits a classic xref + trailer.
"""
from __future__ import annotations
import re
import zlib
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple

from ..core.intermediate import (
    Block, Blockquote, CodeBlock, Heading, HorizontalRule, Image, List_,
    Paragraph, Run, Table, TextDoc,
)
from ..utils.cancellation import CancellationToken
from ..utils.paths import resources_dir
from ._pdf_ttf import (
    TTFFont, TTFParseError, build_to_unicode_cmap, build_widths_array,
    encode_text_for_type0,
)

SUPPORTED_READ: set[str] = set()
SUPPORTED_WRITE = {".pdf"}
DOC_KIND = "text"


# Page geometry (US Letter, in points: 1pt = 1/72 inch).
PAGE_W = 612
PAGE_H = 792
MARGIN_L = 72
MARGIN_R = 72
MARGIN_T = 72
MARGIN_B = 72

# Body font defaults
BODY_SIZE = 11
LEADING = 14
HEADING_SIZES = {1: 22, 2: 18, 3: 15, 4: 13, 5: 12, 6: 11}


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, TextDoc):
        from ..core.intermediate import plain_to_textdoc
        if isinstance(doc, (bytes, bytearray)):
            doc = plain_to_textdoc(bytes(doc).decode("utf-8", errors="replace"))
        else:
            doc = plain_to_textdoc(str(doc))

    writer = _PdfWriter()
    writer.render(doc, cancel)
    writer.save(path)


# ============================================================
# Writer
# ============================================================

class _PdfWriter:
    def __init__(self) -> None:
        self._objects: List[Optional[bytes]] = [None]  # 1-indexed
        self._pages: List[int] = []
        self._page_streams: List[bytearray] = [bytearray()]
        self._cursor_y = PAGE_H - MARGIN_T
        self._dejavu_ttf, self._dejavu_font = self._load_dejavu()
        # F1=Helvetica, F2=Helvetica-Bold, F3=Helvetica-Oblique, F4=Courier,
        # F5=DejaVu Type0 (if loaded; full Latin Extended + Cyrillic + Greek).
        self._has_dejavu = self._dejavu_font is not None

    def _load_dejavu(self) -> tuple[Optional[bytes], Optional[TTFFont]]:
        ttf = resources_dir() / "DejaVuSans.ttf"
        if not ttf.exists():
            return None, None
        try:
            data = ttf.read_bytes()
            font = TTFFont(data)
            return data, font
        except (OSError, TTFParseError):
            return None, None

    # --- Public render -----------------------------------------------------
    def render(self, doc: TextDoc, cancel: CancellationToken) -> None:
        for blk in doc.blocks:
            cancel.check()
            self._render_block(blk, indent=0)

    def _render_block(self, blk: Block, indent: int) -> None:
        if isinstance(blk, Heading):
            size = HEADING_SIZES.get(blk.level, BODY_SIZE)
            self._wrap_text("".join(r.text for r in blk.runs), size=size, bold=True, indent=indent, space_after=8)
        elif isinstance(blk, Paragraph):
            text = self._runs_to_inline(blk.runs)
            self._wrap_text(text, size=BODY_SIZE, indent=indent, space_after=6)
        elif isinstance(blk, List_):
            for i, item in enumerate(blk.items, 1):
                marker = f"{i}. " if blk.ordered else "• "
                first = True
                for sub in item:
                    if isinstance(sub, Paragraph):
                        text = (marker if first else "  ") + self._runs_to_inline(sub.runs)
                        first = False
                        self._wrap_text(text, size=BODY_SIZE, indent=indent + 18, space_after=2)
                    else:
                        self._render_block(sub, indent=indent + 18)
        elif isinstance(blk, Table):
            self._render_table(blk, indent)
        elif isinstance(blk, Image):
            self._wrap_text(f"[image: {blk.alt or 'image'}]", size=BODY_SIZE, italic=True,
                            indent=indent, space_after=6)
        elif isinstance(blk, CodeBlock):
            for line in blk.text.splitlines() or [""]:
                self._wrap_text(line, size=BODY_SIZE - 1, mono=True, indent=indent + 12, space_after=0, no_wrap=True)
            self._cursor_y -= 4
        elif isinstance(blk, HorizontalRule):
            self._cursor_y -= 6
            self._add_horizontal_line(indent)
            self._cursor_y -= 8
        elif isinstance(blk, Blockquote):
            for sub in blk.blocks:
                self._render_block(sub, indent=indent + 24)

    def _runs_to_inline(self, runs: List[Run]) -> str:
        return "".join(r.text for r in runs)

    def _render_table(self, tbl: Table, indent: int) -> None:
        # Compute column widths from text content; render each row as fixed-width.
        if not tbl.rows:
            return
        col_count = max(len(r) for r in tbl.rows)
        col_text: List[List[str]] = []
        for row in tbl.rows:
            row_text = []
            for c_idx in range(col_count):
                if c_idx < len(row):
                    cell = row[c_idx]
                    text_parts = []
                    for b in cell:
                        if isinstance(b, Paragraph):
                            text_parts.append(self._runs_to_inline(b.runs))
                        else:
                            text_parts.append("")
                    row_text.append(" ".join(text_parts).replace("\n", " ")[:60])
                else:
                    row_text.append("")
            col_text.append(row_text)
        col_widths = [max(len(row[c]) for row in col_text) for c in range(col_count)]
        for r_idx, row in enumerate(col_text):
            line = "  ".join(row[c].ljust(col_widths[c]) for c in range(col_count))
            self._wrap_text(line, size=BODY_SIZE - 1, mono=True, indent=indent, space_after=0, no_wrap=True)
            if r_idx == 0:
                sep = "  ".join("-" * col_widths[c] for c in range(col_count))
                self._wrap_text(sep, size=BODY_SIZE - 1, mono=True, indent=indent, space_after=0, no_wrap=True)
        self._cursor_y -= 6

    # --- Layout ------------------------------------------------------------
    def _font_for(self, bold: bool, italic: bool, mono: bool) -> Tuple[str, int]:
        # Returns (font_label, ascent_units). Ascent is approximate for use in
        # vertical positioning — we keep a fixed leading per size.
        if mono:
            return "F4", 0
        if self._has_dejavu and not (bold and italic):
            # Use DejaVu only for plain runs (we ship one face). Bold/italic
            # fall back to Helvetica variants for now.
            return "F5", 0
        if bold:
            return "F2", 0
        if italic:
            return "F3", 0
        return "F1", 0

    def _wrap_text(self, text: str, size: int, indent: int = 0,
                   bold: bool = False, italic: bool = False, mono: bool = False,
                   space_after: int = 0, no_wrap: bool = False) -> None:
        font_label, _ = self._font_for(bold, italic, mono)
        max_width = PAGE_W - MARGIN_L - MARGIN_R - indent
        if no_wrap:
            lines = [text]
        elif font_label == "F5" and self._dejavu_font is not None:
            # Use real glyph widths from the embedded TrueType for accurate wrap
            lines = _wrap_with_font(text, max_width, size, self._dejavu_font)
        else:
            char_w = (size * 0.60) if mono or font_label == "F4" else (size * 0.50)
            lines = _wrap_to_width(text, max_width, char_w)
        for line in lines:
            self._ensure_room(LEADING)
            self._draw_line(line, indent, size, font_label)
            self._cursor_y -= LEADING
        self._cursor_y -= space_after

    def _draw_line(self, text: str, indent: int, size: int, font_label: str) -> None:
        x = MARGIN_L + indent
        y = self._cursor_y
        if font_label == "F5" and self._dejavu_font is not None:
            # Type0 / Identity-H: emit 2-byte hex CIDs
            encoded = encode_text_for_type0(text, self._dejavu_font)
        else:
            encoded = _encode_text_for_pdf(text)
        op = f"BT /{font_label} {size} Tf {x} {y} Td {encoded} Tj ET\n".encode("latin-1")
        self._page_streams[-1].extend(op)

    def _add_horizontal_line(self, indent: int) -> None:
        x1 = MARGIN_L + indent
        x2 = PAGE_W - MARGIN_R
        y = self._cursor_y
        op = f"{x1} {y} m {x2} {y} l 0.5 w S\n".encode("latin-1")
        self._page_streams[-1].extend(op)

    def _ensure_room(self, needed: int) -> None:
        if self._cursor_y - needed < MARGIN_B:
            self._new_page()

    def _new_page(self) -> None:
        self._page_streams.append(bytearray())
        self._cursor_y = PAGE_H - MARGIN_T

    # --- Save --------------------------------------------------------------
    def save(self, path: Path) -> None:
        # Plan object numbers:
        # 1: Catalog
        # 2: Pages
        # 3: Font F1 (Helvetica), 4: F2 Helv-Bold, 5: F3 Helv-Oblique, 6: F4 Courier
        # 7+: optional DejaVu font + descriptor + file (3 objects), then page contents + page objects

        catalog_no = self._reserve()  # 1
        pages_no = self._reserve()    # 2
        f1 = self._reserve(); f2 = self._reserve(); f3 = self._reserve(); f4 = self._reserve()
        font_dict_extra = ""

        if self._has_dejavu and self._dejavu_font is not None:
            # Type0 → CIDFontType2 → FontDescriptor → FontFile2,
            # plus a ToUnicode CMap so copy/paste returns real text.
            f5 = self._reserve()
            cid_font_no = self._reserve()
            descriptor_no = self._reserve()
            file_no = self._reserve()
            tounicode_no = self._reserve()
            self._set(f5, _type0_font_obj(f5, cid_font_no, tounicode_no, "DejaVuSans"))
            self._set(cid_font_no, _cid_font_obj(cid_font_no, descriptor_no, "DejaVuSans", self._dejavu_font))
            self._set(descriptor_no, _font_descriptor_obj_real(descriptor_no, file_no, self._dejavu_font))
            self._set(file_no, _font_file_obj(file_no, self._dejavu_ttf or b""))
            self._set(tounicode_no, _to_unicode_obj(tounicode_no, self._dejavu_font))
            font_dict_extra = f" /F5 {f5} 0 R"

        self._set(f1, _standard_font_obj(f1, "Helvetica"))
        self._set(f2, _standard_font_obj(f2, "Helvetica-Bold"))
        self._set(f3, _standard_font_obj(f3, "Helvetica-Oblique"))
        self._set(f4, _standard_font_obj(f4, "Courier"))

        page_obj_nos: List[int] = []
        content_obj_nos: List[int] = []
        for stream in self._page_streams:
            content_no = self._reserve()
            page_no = self._reserve()
            self._set(content_no, _content_obj(content_no, bytes(stream)))
            self._set(page_no, _page_obj(page_no, pages_no, content_no, f1, f2, f3, f4, font_dict_extra))
            page_obj_nos.append(page_no)
            content_obj_nos.append(content_no)

        kids = " ".join(f"{n} 0 R" for n in page_obj_nos)
        self._set(pages_no,
                  f"{pages_no} 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_obj_nos)} "
                  f"/MediaBox [0 0 {PAGE_W} {PAGE_H}] >>\nendobj\n".encode("latin-1"))
        self._set(catalog_no,
                  f"{catalog_no} 0 obj\n<< /Type /Catalog /Pages {pages_no} 0 R >>\nendobj\n".encode("latin-1"))

        path.write_bytes(self._serialize(catalog_no))

    # --- Object table helpers ---------------------------------------------
    def _reserve(self) -> int:
        self._objects.append(None)
        return len(self._objects) - 1

    def _set(self, num: int, data: bytes) -> None:
        self._objects[num] = data

    def _serialize(self, root_no: int) -> bytes:
        out = BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = [0] * len(self._objects)
        for n in range(1, len(self._objects)):
            data = self._objects[n] or b""
            offsets[n] = out.tell()
            out.write(data)
        xref_pos = out.tell()
        out.write(f"xref\n0 {len(self._objects)}\n".encode("latin-1"))
        out.write(b"0000000000 65535 f \n")
        for n in range(1, len(self._objects)):
            out.write(f"{offsets[n]:010d} 00000 n \n".encode("latin-1"))
        out.write(b"trailer\n")
        out.write(f"<< /Size {len(self._objects)} /Root {root_no} 0 R >>\n".encode("latin-1"))
        out.write(f"startxref\n{xref_pos}\n%%EOF\n".encode("latin-1"))
        return out.getvalue()


# ============================================================
# Helpers
# ============================================================

def _wrap_with_font(text: str, max_width: float, font_size: int, font: TTFFont) -> List[str]:
    """Greedy word-wrap using real glyph widths from the embedded TTF."""
    if not text:
        return [""]
    words = re.split(r"(\s+)", text)
    lines: List[str] = []
    current = ""
    current_w = 0.0
    for tok in words:
        if not tok:
            continue
        tok_w = sum(font.char_width_pt(c, font_size) for c in tok)
        if current_w + tok_w <= max_width:
            current += tok
            current_w += tok_w
        else:
            if current.strip():
                lines.append(current.rstrip())
            current = tok.lstrip()
            current_w = sum(font.char_width_pt(c, font_size) for c in current)
            # Hard-break a single word that's wider than the line
            while current_w > max_width and len(current) > 1:
                # Find break position
                accum = 0.0
                k = 0
                for k in range(len(current)):
                    accum += font.char_width_pt(current[k], font_size)
                    if accum > max_width:
                        break
                lines.append(current[:max(1, k)])
                current = current[max(1, k):]
                current_w = sum(font.char_width_pt(c, font_size) for c in current)
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _wrap_to_width(text: str, max_width: float, char_w: float) -> List[str]:
    """Greedy word-wrap by approximate character width."""
    if not text:
        return [""]
    max_chars = max(10, int(max_width / char_w))
    words = re.split(r"(\s+)", text)
    lines: List[str] = []
    current = ""
    for tok in words:
        if not tok:
            continue
        if len(current) + len(tok) <= max_chars:
            current += tok
        else:
            if current.strip():
                lines.append(current.rstrip())
            current = tok.lstrip()
            # If a single token is longer than max_chars, hard-break it.
            while len(current) > max_chars:
                lines.append(current[:max_chars])
                current = current[max_chars:]
    if current.strip():
        lines.append(current.rstrip())
    return lines or [""]


def _encode_text_for_pdf(text: str) -> str:
    """Encode a string as a PDF literal string. Escapes (, ), \\."""
    encoded_chars = []
    for ch in text:
        cp = ord(ch)
        if ch == "(":
            encoded_chars.append("\\(")
        elif ch == ")":
            encoded_chars.append("\\)")
        elif ch == "\\":
            encoded_chars.append("\\\\")
        elif 32 <= cp < 127:
            encoded_chars.append(ch)
        elif cp <= 255:
            # WinAnsi range
            encoded_chars.append(f"\\{cp:03o}")
        else:
            # Outside WinAnsi — replace with '?' for the standard Helvetica fallback.
            # When using the embedded DejaVu font (CID) this would need different handling;
            # in v1 we transcribe non-WinAnsi text via WinAnsi-best-effort.
            try:
                b = ch.encode("cp1252")
                for byte in b:
                    if 32 <= byte < 127:
                        encoded_chars.append(chr(byte))
                    else:
                        encoded_chars.append(f"\\{byte:03o}")
            except UnicodeEncodeError:
                encoded_chars.append("?")
    return "(" + "".join(encoded_chars) + ")"


def _standard_font_obj(num: int, name: str) -> bytes:
    return (
        f"{num} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /{name} "
        f"/Encoding /WinAnsiEncoding >>\nendobj\n"
    ).encode("latin-1")


def _type0_font_obj(num: int, cid_font_no: int, tounicode_no: int, base_font: str) -> bytes:
    return (
        f"{num} 0 obj\n<< /Type /Font /Subtype /Type0 /BaseFont /{base_font} "
        f"/Encoding /Identity-H /DescendantFonts [{cid_font_no} 0 R] "
        f"/ToUnicode {tounicode_no} 0 R >>\nendobj\n"
    ).encode("latin-1")


def _cid_font_obj(num: int, descriptor_no: int, base_font: str, font: TTFFont) -> bytes:
    widths = build_widths_array(font)
    return (
        f"{num} 0 obj\n<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base_font} "
        f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        f"/FontDescriptor {descriptor_no} 0 R /CIDToGIDMap /Identity "
        f"/W {widths} >>\nendobj\n"
    ).encode("latin-1")


def _font_descriptor_obj_real(num: int, file_no: int, font: TTFFont) -> bytes:
    bbox = "[" + " ".join(str(v) for v in font.bbox) + "]"
    return (
        f"{num} 0 obj\n<< /Type /FontDescriptor /FontName /DejaVuSans "
        f"/Flags {font.flags} /FontBBox {bbox} /ItalicAngle {font.italic_angle} "
        f"/Ascent {font.ascent} /Descent {font.descent} /CapHeight {font.cap_height} "
        f"/StemV {font.stem_v} /FontFile2 {file_no} 0 R >>\nendobj\n"
    ).encode("latin-1")


def _to_unicode_obj(num: int, font: TTFFont) -> bytes:
    cmap_text = build_to_unicode_cmap(font).encode("latin-1")
    compressed = zlib.compress(cmap_text)
    header = f"{num} 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("latin-1")
    return header + compressed + b"\nendstream\nendobj\n"


def _font_file_obj(num: int, ttf_data: bytes) -> bytes:
    compressed = zlib.compress(ttf_data)
    header = (
        f"{num} 0 obj\n<< /Length {len(compressed)} /Length1 {len(ttf_data)} "
        f"/Filter /FlateDecode >>\nstream\n"
    ).encode("latin-1")
    return header + compressed + b"\nendstream\nendobj\n"


def _content_obj(num: int, stream: bytes) -> bytes:
    compressed = zlib.compress(stream)
    header = f"{num} 0 obj\n<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode("latin-1")
    return header + compressed + b"\nendstream\nendobj\n"


def _page_obj(num: int, parent: int, content: int,
              f1: int, f2: int, f3: int, f4: int, extra_fonts: str) -> bytes:
    return (
        f"{num} 0 obj\n<< /Type /Page /Parent {parent} 0 R /Contents {content} 0 R "
        f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R /F3 {f3} 0 R /F4 {f4} 0 R{extra_fonts} >> >> "
        f">>\nendobj\n"
    ).encode("latin-1")
