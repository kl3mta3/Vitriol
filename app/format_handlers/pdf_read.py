"""PDF text + image extraction.

Two-tier strategy:

  PRIMARY  — pdfminer.six. Mature 10+ year project that handles the long
             tail of font-encoding edge cases (standard 14 fonts without
             ToUnicode CMaps, /Differences overrides, CIDFonts, Type1
             named glyphs via Adobe Glyph List, etc.). Also extracts
             embedded image XObjects so PDF→MD/TXT can produce a folder
             bundle with `images/`. Launcher auto-installs alongside the
             other Python deps on first run.

  FALLBACK — the recoded extractor below. Pure-stdlib, parses xref +
             FlateDecode + Tj/TJ operators + ToUnicode CMaps. Covers a
             narrow subset (PDFs with embedded ToUnicode tables work,
             standard-font PDFs typically come out as gibberish). Used
             only when pdfminer.six can't be imported.
"""
from __future__ import annotations
import io
import re
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.intermediate import Image, Paragraph, Run, TextDoc
from ..utils.cancellation import CancellationToken
from ..utils.logger import get_logger

_log = get_logger()

SUPPORTED_READ = {".pdf"}
SUPPORTED_WRITE: set[str] = set()  # write lives in pdf_write.py
DOC_KIND = "text"


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    """Try pdfminer.six first; fall back to the recoded extractor only if
    pdfminer can't be imported (e.g. user ran main.py before the launcher's
    pip install completed).

    Trailer-envelope fast path: if the PDF was produced by Vitriol from
    an off-type source (e.g. PNG -> PDF), the original source bytes are
    appended after %%EOF as a UCMSv1 envelope. Recover those directly so
    the round-trip is byte-perfect with no quality loss from re-extraction.
    """
    origin = _try_read_trailer_envelope(path)
    if origin is not None:
        return _wrap_recovered_origin(*origin)
    try:
        import pdfminer  # noqa: F401  — just probing availability
        doc = _extract_via_pdfminer(path, cancel)
        _annotate_low_extraction_warning(doc, path)
        return doc
    except ImportError:
        _log.warning(
            "pdfminer.six unavailable — falling back to recoded extractor "
            "(reduced quality). Run launcher.py to install dependencies."
        )
        return _read_recoded(path, cancel)
    except Exception as e:
        _log.exception("pdfminer extraction failed; falling back: %s", e)
        doc = _read_recoded(path, cancel)
        doc.metadata.setdefault("warnings", []).append(
            f"Primary PDF extractor failed ({type(e).__name__}); used fallback. "
            "Result may be incomplete."
        )
        return doc


def _try_read_trailer_envelope(path: Path) -> Optional[tuple[bytes, str]]:
    """Scan the tail of the PDF for a UCMSv1 envelope appended after %%EOF.
    Returns (payload, src_ext) on success; None if no trailer or invalid.

    Strategy: scan the last ~64 KB for the envelope's MAGIC bytes
    (`UCMSv1\\0`) rather than anchoring on `%%EOF`. The MAGIC is much more
    distinctive — the chance of a coincidental occurrence in a normal PDF
    body is negligible. Anchoring on `%%EOF` could be fooled by foreign
    PDFs whose content streams contain that literal substring.

    Reads at most 64 MB. Envelopes larger than that skip this fast path
    (the regular pdfminer extraction still works on the document's visible
    content; the trailer would still be valid but won't be detected via
    the fast path).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < 32:
        return None
    read_limit = min(size, 64 * 1024 * 1024)
    try:
        with open(path, "rb") as f:
            if size <= read_limit:
                blob = f.read()
            else:
                f.seek(-read_limit, 2)
                blob = f.read()
    except OSError:
        return None
    try:
        from .masquerade import MAGIC, _parse_envelope
    except ImportError:
        return None
    # Find the LAST occurrence of the envelope MAGIC. Trailers we write
    # are always at the very end, so the last match is ours; any earlier
    # match (extremely unlikely in a normal PDF) is ignored.
    pos = blob.rfind(MAGIC)
    if pos < 0:
        return None
    try:
        return _parse_envelope(blob[pos:])
    except ValueError:
        return None


def _wrap_recovered_origin(payload: bytes, src_ext: str) -> TextDoc:
    """Wrap recovered original-source bytes as a TextDoc the router can
    forward to any image writer (or onward Stone host) without losing
    bytes. The metadata's `_vitriol_origin` lets a downstream PDF write
    re-emit the trailer if the user does PDF -> PDF conversion."""
    from ..core.intermediate import _EXT_TO_MIME
    ext = src_ext.lower()
    if not ext.startswith("."):
        ext = "." + ext
    mime = _EXT_TO_MIME.get(ext, "application/octet-stream")
    doc = TextDoc(blocks=[Image(data=payload, mime=mime, alt=f"image{ext}")])
    doc.metadata["_vitriol_origin"] = {"bytes": payload, "ext": ext}
    return doc


def _annotate_low_extraction_warning(doc: TextDoc, path: Path) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    total_chars = sum(len(r.text) for blk in doc.blocks
                      if isinstance(blk, Paragraph) for r in blk.runs)
    if size > 100_000 and total_chars < 50:
        doc.metadata.setdefault("warnings", []).append(
            "Limited text extracted — this PDF may be scanned, encrypted, "
            "or contain text only as outlined shapes. Try opening it in a "
            "PDF viewer first."
        )


# ============================================================
# Primary extractor — pdfminer.six
# ============================================================

def _extract_via_pdfminer(path: Path, cancel: CancellationToken) -> TextDoc:
    """Walk pages with pdfminer.high_level.extract_pages, emit Paragraph
    blocks for each LTTextContainer and Image blocks for each LTImage.

    Memory: pdfminer's extract_pages is a generator — pages stream one at a
    time. We don't hold the whole document at any point.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer, LTImage, LTFigure

    blocks: List = []
    images_seen = 0

    def _walk_for_images(container) -> None:
        """LTImages can be nested under LTFigure (or directly on the page).
        Walk recursively, emit Image blocks for each LTImage we find."""
        nonlocal images_seen
        for child in container:
            if isinstance(child, LTImage):
                img_block = _ltimage_to_block(child, images_seen + 1)
                if img_block is not None:
                    blocks.append(img_block)
                    images_seen += 1
            elif isinstance(child, LTFigure):
                _walk_for_images(child)

    page_num = 0
    for page_layout in extract_pages(str(path)):
        cancel.check()
        page_num += 1
        for elem in page_layout:
            if isinstance(elem, LTTextContainer):
                text = elem.get_text()
                # pdfminer returns text with line breaks intact; collapse runs
                # of whitespace at line boundaries but preserve paragraph
                # separation by splitting on blank lines.
                for chunk in text.split("\n\n"):
                    chunk = chunk.strip()
                    if chunk:
                        # Normalize internal newlines to spaces — paragraphs
                        # usually wrap mid-line in PDFs.
                        chunk = re.sub(r"\s+", " ", chunk)
                        blocks.append(Paragraph(runs=[Run(text=chunk)]))
            elif isinstance(elem, LTFigure):
                _walk_for_images(elem)
            elif isinstance(elem, LTImage):
                img_block = _ltimage_to_block(elem, images_seen + 1)
                if img_block is not None:
                    blocks.append(img_block)
                    images_seen += 1

    return TextDoc(blocks=blocks, metadata={"page_count": page_num})


def _ltimage_to_block(ltimage, index: int) -> Optional[Image]:
    """Convert a pdfminer LTImage to our Image IR block. Tries to keep the
    encoded bytes as-is for JPEG (DCTDecode) and re-encodes everything else
    to PNG via Pillow. Returns None for image types we can't decode."""
    try:
        stream = ltimage.stream
        if stream is None:
            return None
        # pdfminer exposes the raw stream filters via stream.attrs.get('Filter')
        # and the decoded bytes via stream.get_data(). For JPEGs we want the
        # original DCT bytes (no re-encode); for others we re-encode to PNG.
        filt = stream.attrs.get("Filter")
        filt_name = ""
        if filt is not None:
            try:
                # Filter can be a single PSLiteral or a list; pdfminer.psparser
                # doesn't import cleanly here, so duck-type via repr.
                if isinstance(filt, list):
                    filt_name = str(filt[0]) if filt else ""
                else:
                    filt_name = str(filt)
            except Exception:
                filt_name = ""
        # rawdata holds the pre-filter bytes; bytes the post-filter pixel data.
        if "DCTDecode" in filt_name:
            # JPEG — keep encoded bytes verbatim
            data = stream.get_rawdata() or stream.get_data()
            return Image(data=data, mime="image/jpeg",
                          alt=f"image{index}.jpg")
        # Decode then re-encode as PNG
        try:
            from PIL import Image as PILImage
        except ImportError:
            return None
        raw = stream.get_data()
        width = stream.attrs.get("Width") or 0
        height = stream.attrs.get("Height") or 0
        bits = stream.attrs.get("BitsPerComponent", 8)
        cs = stream.attrs.get("ColorSpace")
        # Best-effort color space → Pillow mode
        mode = "RGB"
        cs_str = str(cs) if cs is not None else ""
        if "DeviceGray" in cs_str:
            mode = "L"
        elif "DeviceRGB" in cs_str:
            mode = "RGB"
        elif "DeviceCMYK" in cs_str:
            mode = "CMYK"
        if width <= 0 or height <= 0:
            return None
        try:
            pil = PILImage.frombytes(mode, (int(width), int(height)), raw)
        except Exception:
            return None
        if mode == "CMYK":
            pil = pil.convert("RGB")
        out = io.BytesIO()
        pil.save(out, format="PNG", optimize=False)
        return Image(data=out.getvalue(), mime="image/png",
                      alt=f"image{index}.png")
    except Exception:
        # Anything goes wrong — skip this image rather than failing the whole
        # extraction. The text still comes through.
        return None


# ============================================================
# Fallback extractor — recoded (kept from earlier)
# ============================================================

def _read_recoded(path: Path, cancel: CancellationToken) -> TextDoc:
    raw = path.read_bytes()
    cancel.check()
    try:
        doc = _extract_pdf(raw, cancel)
    except _PdfError as e:
        _log.warning("recoded pdf parse failed: %s", e)
        return TextDoc(
            blocks=[Paragraph(runs=[Run(text=f"(Could not extract text: {e})")])],
            metadata={"warnings": [f"PDF extraction failed: {e}"]},
        )
    _annotate_low_extraction_warning(doc, path)
    return doc


# ============================================================
# Errors
# ============================================================

class _PdfError(Exception):
    pass


# ============================================================
# Tokenizer
# ============================================================

_WHITESPACE = b"\x00\t\n\f\r "
_DELIMS = b"()<>[]{}/%"


class _Lexer:
    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def skip_ws_and_comments(self) -> None:
        while self.pos < len(self.data):
            c = self.data[self.pos:self.pos + 1]
            if c in _WHITESPACE:
                self.pos += 1
            elif c == b"%":
                # Comment until newline
                nl = self.data.find(b"\n", self.pos)
                self.pos = nl + 1 if nl != -1 else len(self.data)
            else:
                break

    def peek(self, n: int = 1) -> bytes:
        return self.data[self.pos:self.pos + n]

    def next_token(self) -> Optional[bytes]:
        self.skip_ws_and_comments()
        if self.pos >= len(self.data):
            return None
        c = self.data[self.pos:self.pos + 1]
        if c in b"<>[]{}":
            if c == b"<" and self.peek(2) == b"<<":
                self.pos += 2
                return b"<<"
            if c == b">" and self.peek(2) == b">>":
                self.pos += 2
                return b">>"
            self.pos += 1
            return c
        if c == b"/":
            return self._read_name()
        if c == b"(":
            return self._read_string()
        if c == b"<":
            return self._read_hex_string()
        # number, keyword, operator
        return self._read_word()

    def _read_word(self) -> bytes:
        start = self.pos
        while self.pos < len(self.data):
            c = self.data[self.pos:self.pos + 1]
            if c in _WHITESPACE or c in _DELIMS:
                break
            self.pos += 1
        return self.data[start:self.pos]

    def _read_name(self) -> bytes:
        # Includes the leading slash
        start = self.pos
        self.pos += 1  # past /
        while self.pos < len(self.data):
            c = self.data[self.pos:self.pos + 1]
            if c in _WHITESPACE or c in _DELIMS:
                break
            self.pos += 1
        return self.data[start:self.pos]

    def _read_string(self) -> bytes:
        start = self.pos
        self.pos += 1  # past (
        depth = 1
        out = bytearray(b"(")
        while self.pos < len(self.data) and depth > 0:
            c = self.data[self.pos:self.pos + 1]
            if c == b"\\":
                out.append(self.data[self.pos])
                self.pos += 1
                if self.pos < len(self.data):
                    out.append(self.data[self.pos])
                    self.pos += 1
            elif c == b"(":
                depth += 1
                out.append(self.data[self.pos])
                self.pos += 1
            elif c == b")":
                depth -= 1
                out.append(self.data[self.pos])
                self.pos += 1
            else:
                out.append(self.data[self.pos])
                self.pos += 1
        return bytes(out)

    def _read_hex_string(self) -> bytes:
        start = self.pos
        self.pos += 1  # past <
        end = self.data.find(b">", self.pos)
        if end == -1:
            raise _PdfError("unterminated hex string")
        s = self.data[start:end + 1]
        self.pos = end + 1
        return s


# ============================================================
# Object parser (minimal — handles dicts, arrays, refs, prims)
# ============================================================

def _parse_object(lex: _Lexer):
    """Parses the object at the current lex position. Returns Python value."""
    tok = lex.next_token()
    if tok is None:
        raise _PdfError("unexpected EOF parsing object")
    return _parse_from_token(tok, lex)


def _parse_from_token(tok: bytes, lex: _Lexer):
    if tok == b"<<":
        return _parse_dict(lex)
    if tok == b"[":
        return _parse_array(lex)
    if tok.startswith(b"/"):
        return _Name(tok[1:].decode("latin-1"))
    if tok.startswith(b"("):
        return _decode_pdf_string(tok[1:-1])
    if tok.startswith(b"<") and tok.endswith(b">"):
        return _decode_hex_string(tok[1:-1])
    if tok in (b"true", b"false"):
        return tok == b"true"
    if tok == b"null":
        return None
    # Number, ref, or keyword
    try:
        if b"." in tok:
            return float(tok)
        n = int(tok)
        # Possibly a reference: "n m R"
        save = lex.pos
        t2 = lex.next_token()
        t3 = lex.next_token() if t2 is not None else None
        if t2 is not None and t3 == b"R":
            try:
                gen = int(t2)
                return _Ref(n, gen)
            except ValueError:
                pass
        lex.pos = save
        return n
    except ValueError:
        # keyword like "stream", "endobj", etc. — return raw
        return _Keyword(tok)


def _parse_dict(lex: _Lexer) -> dict:
    out: dict = {}
    while True:
        tok = lex.next_token()
        if tok is None:
            raise _PdfError("unterminated dict")
        if tok == b">>":
            return out
        if not tok.startswith(b"/"):
            raise _PdfError(f"expected /name in dict, got {tok!r}")
        key = tok[1:].decode("latin-1")
        val = _parse_object(lex)
        out[key] = val


def _parse_array(lex: _Lexer) -> list:
    out: list = []
    while True:
        tok = lex.next_token()
        if tok is None:
            raise _PdfError("unterminated array")
        if tok == b"]":
            return out
        out.append(_parse_from_token(tok, lex))


class _Ref:
    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int) -> None:
        self.num = num
        self.gen = gen

    def __repr__(self) -> str:
        return f"Ref({self.num},{self.gen})"


class _Name(str):
    pass


class _Keyword(bytes):
    pass


def _decode_pdf_string(b: bytes) -> bytes:
    """Decode escape sequences inside a literal string. Returns bytes."""
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i:i + 1]
        if c == b"\\" and i + 1 < len(b):
            nxt = b[i + 1:i + 2]
            if nxt == b"n":
                out.append(0x0a); i += 2
            elif nxt == b"r":
                out.append(0x0d); i += 2
            elif nxt == b"t":
                out.append(0x09); i += 2
            elif nxt == b"b":
                out.append(0x08); i += 2
            elif nxt == b"f":
                out.append(0x0c); i += 2
            elif nxt in (b"(", b")", b"\\"):
                out.append(b[i + 1]); i += 2
            elif nxt.isdigit():
                # Octal up to 3 digits
                j = i + 1
                end = min(i + 4, len(b))
                while j < end and b[j:j + 1].isdigit():
                    j += 1
                try:
                    out.append(int(b[i + 1:j], 8) & 0xff)
                except ValueError:
                    pass
                i = j
            elif nxt == b"\n":
                i += 2  # line continuation
            else:
                i += 2
        else:
            out.append(b[i])
            i += 1
    return bytes(out)


def _decode_hex_string(b: bytes) -> bytes:
    s = bytes(c for c in b if c not in _WHITESPACE)
    if len(s) % 2 == 1:
        s += b"0"
    try:
        return bytes.fromhex(s.decode("latin-1"))
    except ValueError:
        return b""


# ============================================================
# xref + document
# ============================================================

class _PdfDoc:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.xref: Dict[Tuple[int, int], int] = {}  # (num, gen) -> offset
        self.trailer: dict = {}
        self._parse_xref()

    def _parse_xref(self) -> None:
        # Find startxref near the end
        tail = self.data[-2048:]
        idx = tail.rfind(b"startxref")
        if idx == -1:
            raise _PdfError("startxref not found")
        # The offset follows
        m = re.search(rb"startxref\s*(\d+)", tail)
        if not m:
            raise _PdfError("malformed startxref")
        offset = int(m.group(1))
        self._read_xref_at(offset)
        if self.trailer.get("Encrypt"):
            raise _PdfError("Encrypted PDFs are not supported in v1.")

    def _read_xref_at(self, offset: int) -> None:
        if self.data[offset:offset + 4] == b"xref":
            self._read_classic_xref(offset)
        else:
            # xref stream
            lex = _Lexer(self.data, offset)
            obj_num = int(lex.next_token() or b"0")
            _gen = int(lex.next_token() or b"0")
            kw = lex.next_token()  # "obj"
            if kw != b"obj":
                raise _PdfError("expected obj at xref-stream start")
            d = _parse_object(lex)  # the dict
            if not isinstance(d, dict):
                raise _PdfError("xref stream missing dict")
            # Read 'stream' keyword + EOL
            lex.skip_ws_and_comments()
            assert self.data[lex.pos:lex.pos + 6] == b"stream"
            lex.pos += 6
            if self.data[lex.pos:lex.pos + 2] == b"\r\n":
                lex.pos += 2
            elif self.data[lex.pos:lex.pos + 1] == b"\n":
                lex.pos += 1
            length = self._resolve_int(d.get("Length", 0))
            stream = self.data[lex.pos:lex.pos + length]
            stream = self._apply_filters(stream, d)
            self._read_xref_stream(stream, d)
            self.trailer = d
            prev = d.get("Prev")
            if prev is not None:
                self._read_xref_at(int(prev))
            return

    def _read_classic_xref(self, offset: int) -> None:
        i = offset + 4  # past "xref"
        while i < len(self.data) and self.data[i:i + 1] in _WHITESPACE:
            i += 1
        # subsections: "first count\n" then count lines of "ooooooooo ggggg n|f"
        while True:
            line_end = self.data.find(b"\n", i)
            if line_end == -1:
                break
            header = self.data[i:line_end].strip()
            if header.startswith(b"trailer"):
                break
            parts = header.split()
            if len(parts) != 2:
                break
            try:
                first = int(parts[0])
                count = int(parts[1])
            except ValueError:
                break
            i = line_end + 1
            for k in range(count):
                entry = self.data[i:i + 20]
                # 10-digit offset, 5-digit gen, n/f
                m = re.match(rb"(\d{10})\s(\d{5})\s([nf])", entry)
                if m and m.group(3) == b"n":
                    self.xref[(first + k, int(m.group(2)))] = int(m.group(1))
                i += 20
        # trailer dict
        t_idx = self.data.find(b"trailer", offset)
        if t_idx != -1:
            lex = _Lexer(self.data, t_idx + len(b"trailer"))
            tok = lex.next_token()
            if tok == b"<<":
                d = _parse_dict(lex)
                # Merge — earlier (closer to top) wins
                for k, v in d.items():
                    self.trailer.setdefault(k, v)
                prev = d.get("Prev")
                if prev is not None:
                    try:
                        self._read_xref_at(int(prev))
                    except _PdfError:
                        pass

    def _read_xref_stream(self, stream: bytes, d: dict) -> None:
        w = d.get("W")
        if not isinstance(w, list) or len(w) != 3:
            return
        w1, w2, w3 = int(w[0]), int(w[1]), int(w[2])
        index = d.get("Index", [0, int(d.get("Size", 0))])
        idx_pairs = list(zip(index[0::2], index[1::2]))
        pos = 0
        for first, count in idx_pairs:
            for k in range(int(count)):
                rec = stream[pos:pos + w1 + w2 + w3]
                pos += w1 + w2 + w3
                if not rec:
                    continue
                t = int.from_bytes(rec[:w1], "big") if w1 > 0 else 1
                f2 = int.from_bytes(rec[w1:w1 + w2], "big") if w2 > 0 else 0
                f3 = int.from_bytes(rec[w1 + w2:w1 + w2 + w3], "big") if w3 > 0 else 0
                if t == 1:
                    self.xref[(first + k, f3)] = f2

    def _resolve_int(self, x) -> int:
        if isinstance(x, _Ref):
            obj = self.get_object(x)
            return int(obj) if isinstance(obj, (int, float)) else 0
        try:
            return int(x)
        except (TypeError, ValueError):
            return 0

    def get_object(self, ref):
        """Return the resolved object for a Ref (or pass through if not a Ref)."""
        if not isinstance(ref, _Ref):
            return ref
        offset = self.xref.get((ref.num, ref.gen))
        if offset is None:
            return None
        lex = _Lexer(self.data, offset)
        n1 = lex.next_token()
        n2 = lex.next_token()
        kw = lex.next_token()
        if kw != b"obj":
            return None
        obj = _parse_object(lex)
        # If followed by 'stream', attach payload
        if isinstance(obj, dict):
            lex.skip_ws_and_comments()
            if self.data[lex.pos:lex.pos + 6] == b"stream":
                lex.pos += 6
                if self.data[lex.pos:lex.pos + 2] == b"\r\n":
                    lex.pos += 2
                elif self.data[lex.pos:lex.pos + 1] == b"\n":
                    lex.pos += 1
                length = self._resolve_int(obj.get("Length", 0))
                payload = self.data[lex.pos:lex.pos + length]
                payload = self._apply_filters(payload, obj)
                return _Stream(obj, payload)
        return obj

    def _apply_filters(self, data: bytes, d: dict) -> bytes:
        f = d.get("Filter")
        if f is None:
            return data
        if isinstance(f, _Name):
            filters = [f]
        elif isinstance(f, list):
            filters = f
        else:
            return data
        for filt in filters:
            name = str(filt)
            if name == "FlateDecode":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    return data
                # PNG/TIFF predictor would be applied here for xref streams; skipped.
            elif name == "ASCII85Decode":
                data = _ascii85_decode(data)
            elif name == "ASCIIHexDecode":
                hex_data = bytes(c for c in data if c not in _WHITESPACE and c != ord(">"))
                if len(hex_data) % 2 == 1:
                    hex_data += b"0"
                try:
                    data = bytes.fromhex(hex_data.decode("latin-1"))
                except ValueError:
                    return data
            else:
                # Unsupported filter — give up
                raise _PdfError(f"unsupported filter: {name}")
        return data


class _Stream:
    __slots__ = ("dict_", "data")

    def __init__(self, d: dict, data: bytes) -> None:
        self.dict_ = d
        self.data = data


def _ascii85_decode(data: bytes) -> bytes:
    s = bytes(c for c in data if c not in _WHITESPACE)
    if s.startswith(b"<~"):
        s = s[2:]
    if s.endswith(b"~>"):
        s = s[:-2]
    out = bytearray()
    n = 0
    accum = 0
    for c in s:
        if c == ord("z") and n == 0:
            out.extend(b"\x00\x00\x00\x00")
            continue
        accum = accum * 85 + (c - 33)
        n += 1
        if n == 5:
            out.extend(accum.to_bytes(4, "big"))
            n = 0
            accum = 0
    if n:
        for _ in range(5 - n):
            accum = accum * 85 + 84
        out.extend(accum.to_bytes(4, "big")[: n - 1])
    return bytes(out)


# ============================================================
# Content stream → text
# ============================================================

_OP_TJ_STRING = re.compile(rb"")  # placeholder; we use the lexer for content streams too


def _extract_pdf(data: bytes, cancel: CancellationToken) -> TextDoc:
    if not data.startswith(b"%PDF-"):
        raise _PdfError("not a PDF file")
    doc = _PdfDoc(data)
    root_ref = doc.trailer.get("Root")
    if root_ref is None:
        raise _PdfError("trailer has no /Root")
    catalog = doc.get_object(root_ref)
    if not isinstance(catalog, dict):
        raise _PdfError("catalog is not a dict")
    pages_ref = catalog.get("Pages")
    if pages_ref is None:
        raise _PdfError("catalog has no /Pages")
    pages = _flatten_pages(doc, doc.get_object(pages_ref))

    blocks = []
    for page in pages:
        cancel.check()
        text = _page_text(doc, page)
        if text.strip():
            for para in text.split("\n\n"):
                para = para.strip()
                if para:
                    blocks.append(Paragraph(runs=[Run(text=para)]))
    return TextDoc(blocks=blocks)


def _flatten_pages(doc: _PdfDoc, node) -> List[dict]:
    if not isinstance(node, dict):
        return []
    t = node.get("Type")
    if t and str(t) == "Page":
        return [node]
    out = []
    kids = node.get("Kids", [])
    if isinstance(kids, list):
        for k in kids:
            child = doc.get_object(k)
            out.extend(_flatten_pages(doc, child))
    return out


def _page_text(doc: _PdfDoc, page: dict) -> str:
    contents = page.get("Contents")
    if contents is None:
        return ""
    streams = []
    if isinstance(contents, list):
        for c in contents:
            obj = doc.get_object(c)
            if isinstance(obj, _Stream):
                streams.append(obj.data)
    else:
        obj = doc.get_object(contents)
        if isinstance(obj, _Stream):
            streams.append(obj.data)
    full = b"\n".join(streams)
    fonts = _gather_fonts(doc, page)
    return _decode_content_stream(full, fonts)


def _gather_fonts(doc: _PdfDoc, page: dict) -> Dict[str, dict]:
    """Returns {font-name -> {to_unicode_map: dict[int->str], encoding: str}}."""
    out: Dict[str, dict] = {}
    res = page.get("Resources")
    if isinstance(res, _Ref):
        res = doc.get_object(res)
    if not isinstance(res, dict):
        return out
    font_dict = res.get("Font")
    if isinstance(font_dict, _Ref):
        font_dict = doc.get_object(font_dict)
    if not isinstance(font_dict, dict):
        return out
    for name, ref in font_dict.items():
        font_obj = doc.get_object(ref)
        if not isinstance(font_obj, dict):
            continue
        info = {"to_unicode_map": {}, "encoding": "WinAnsi"}
        tu = font_obj.get("ToUnicode")
        if tu is not None:
            tu_obj = doc.get_object(tu)
            if isinstance(tu_obj, _Stream):
                info["to_unicode_map"] = _parse_to_unicode_cmap(tu_obj.data)
        enc = font_obj.get("Encoding")
        if isinstance(enc, _Name):
            info["encoding"] = str(enc)
        out[name] = info
    return out


# Parse a /ToUnicode CMap stream → dict mapping int code → Unicode str
_BFCHAR_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_RE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]+)\])")


def _parse_to_unicode_cmap(data: bytes) -> Dict[int, str]:
    out: Dict[int, str] = {}
    # bfchar
    in_bfchar = False
    for chunk in re.split(rb"endbfchar|endbfrange", data):
        if b"beginbfchar" in chunk:
            body = chunk.split(b"beginbfchar", 1)[1]
            for m in _BFCHAR_RE.finditer(body):
                src = int(m.group(1), 16)
                tgt_hex = m.group(2)
                out[src] = _hex_to_unicode(tgt_hex)
        if b"beginbfrange" in chunk:
            body = chunk.split(b"beginbfrange", 1)[1]
            for m in _BFRANGE_RE.finditer(body):
                lo = int(m.group(1), 16)
                hi = int(m.group(2), 16)
                if m.group(3):
                    base = _hex_to_unicode(m.group(3))
                    if base:
                        for i in range(hi - lo + 1):
                            # Increment last codepoint
                            if base:
                                out[lo + i] = base[:-1] + chr(ord(base[-1]) + i)
                elif m.group(4):
                    items = re.findall(rb"<([0-9A-Fa-f]+)>", m.group(4))
                    for i, it in enumerate(items):
                        out[lo + i] = _hex_to_unicode(it)
    return out


def _hex_to_unicode(h: bytes) -> str:
    s = h.decode("latin-1")
    if len(s) % 2 == 1:
        s += "0"
    raw = bytes.fromhex(s)
    # UTF-16 BE in CMap
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _decode_content_stream(data: bytes, fonts: Dict[str, dict]) -> str:
    """Walk the content-stream operators, accumulating text. Cluster lines by Y."""
    lex = _Lexer(data)
    stack: list = []
    current_font_name: Optional[str] = None
    in_text = False
    # (y, x, text)
    fragments: List[Tuple[float, float, str]] = []
    text_y = 0.0
    text_x = 0.0
    leading = 12.0
    # text matrix tracking is approximate — we use only Td/TD/Tm/T* y-deltas

    while True:
        tok = lex.next_token()
        if tok is None:
            break
        if isinstance(tok, _Keyword) or (
            isinstance(tok, bytes) and tok and not tok.startswith(b"/")
            and not tok.startswith(b"(") and not tok.startswith(b"<")
            and not tok in (b"<<", b">>", b"[", b"]")
        ):
            # numeric vs operator
            try:
                if b"." in tok:
                    stack.append(float(tok))
                else:
                    stack.append(int(tok))
                continue
            except ValueError:
                pass
            op = tok.decode("latin-1", errors="replace")
            if op == "BT":
                in_text = True
                text_x = 0.0; text_y = 0.0
                stack = []
                continue
            if op == "ET":
                in_text = False
                stack = []
                continue
            if op == "Tf" and len(stack) >= 2:
                font_name = stack[-2]
                if isinstance(font_name, _Name):
                    current_font_name = str(font_name)
                stack = []
                continue
            if op == "TL" and stack:
                leading = float(stack[-1])
                stack = []
                continue
            if op == "Td" and len(stack) >= 2:
                text_x += float(stack[-2])
                text_y += float(stack[-1])
                stack = []
                continue
            if op == "TD" and len(stack) >= 2:
                tx, ty = float(stack[-2]), float(stack[-1])
                text_x += tx
                text_y += ty
                leading = -ty
                stack = []
                continue
            if op == "Tm" and len(stack) >= 6:
                text_x = float(stack[-2])
                text_y = float(stack[-1])
                stack = []
                continue
            if op == "T*":
                text_y -= leading
                stack = []
                continue
            if op in ("Tj", "'", '"'):
                if stack and isinstance(stack[-1], (bytes, str)):
                    s = stack[-1]
                    if isinstance(s, str):
                        s = s.encode("latin-1", errors="replace")
                    text = _decode_show_string(s, fonts.get(current_font_name or "", {}))
                    if op == "'":
                        text_y -= leading
                    if op == '"':
                        text_y -= leading
                    fragments.append((text_y, text_x, text))
                stack = []
                continue
            if op == "TJ" and stack and isinstance(stack[-1], list):
                arr = stack[-1]
                parts = []
                for el in arr:
                    if isinstance(el, (bytes, str)):
                        b = el if isinstance(el, bytes) else el.encode("latin-1", errors="replace")
                        parts.append(_decode_show_string(b, fonts.get(current_font_name or "", {})))
                    elif isinstance(el, (int, float)) and el < -100:
                        # large negative kerning = word break
                        parts.append(" ")
                fragments.append((text_y, text_x, "".join(parts)))
                stack = []
                continue
            # Other operator — drop accumulated stack
            stack = []
            continue
        # Operand
        if isinstance(tok, bytes) and tok in (b"<<", b">>"):
            # parse a dict and discard (no text from dicts at top level here)
            if tok == b"<<":
                _parse_dict(lex)
            continue
        if tok == b"[":
            stack.append(_parse_array(lex))
            continue
        if isinstance(tok, bytes) and tok.startswith(b"/"):
            stack.append(_Name(tok[1:].decode("latin-1")))
            continue
        if isinstance(tok, bytes) and tok.startswith(b"("):
            stack.append(_decode_pdf_string(tok[1:-1]))
            continue
        if isinstance(tok, bytes) and tok.startswith(b"<") and tok.endswith(b">"):
            stack.append(_decode_hex_string(tok[1:-1]))
            continue

    # Cluster fragments into lines by Y (descending), then sort each line by X (ascending).
    if not fragments:
        return ""
    # Quantize Y to nearest integer to bucket fragments on the same line
    by_line: Dict[int, List[Tuple[float, str]]] = {}
    for y, x, t in fragments:
        bucket = round(y)
        by_line.setdefault(bucket, []).append((x, t))
    lines = []
    for y in sorted(by_line.keys(), reverse=True):
        line_parts = sorted(by_line[y], key=lambda p: p[0])
        lines.append("".join(t for _x, t in line_parts).rstrip())
    # Heuristic paragraphing: two consecutive blank-ish lines => paragraph
    out_lines: List[str] = []
    for line in lines:
        out_lines.append(line)
    return "\n".join(out_lines)


_STD_ENC = bytes(range(256)).decode("cp1252", errors="replace")


def _decode_show_string(b: bytes, font_info: dict) -> str:
    """Map a raw byte string from a Tj/TJ operand into Unicode."""
    cmap = font_info.get("to_unicode_map") or {}
    if cmap:
        # Try 2-byte codes first if any keys are >255
        if any(k > 255 for k in cmap):
            # 2-byte CID
            out = []
            for i in range(0, len(b) - 1, 2):
                code = (b[i] << 8) | b[i + 1]
                out.append(cmap.get(code, ""))
            return "".join(out)
        else:
            return "".join(cmap.get(c, _STD_ENC[c]) for c in b)
    # Default: WinAnsi
    return b.decode("cp1252", errors="replace")
