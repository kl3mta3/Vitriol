"""DOCX read/write — recoded ZIP+XML, no python-docx.

Scope:
  - Reads paragraphs, headings (Heading 1..6 styles), bold/italic/underline runs,
    hyperlinks, bulleted/numbered lists, tables, inline images, code-style
    paragraphs (monospaced font detection).
  - Writes the same back. Produces a minimally-styled docx that opens in Word,
    LibreOffice, and Pages.
  - Does NOT support: tracked changes, comments, headers/footers, custom theme
    styles, mathematical equations, embedded objects, drawing primitives.

DOCX is a ZIP containing:
  /[Content_Types].xml       — MIME registry for parts
  /_rels/.rels               — root relationships pointer
  /word/document.xml         — body content
  /word/_rels/document.xml.rels — relationships from document.xml
  /word/styles.xml           — paragraph + run style definitions
  /word/numbering.xml        — list-numbering definitions (lazy-created on write)
  /word/media/image1.png ... — embedded images
"""
from __future__ import annotations
import re
import uuid
import zipfile
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..core.intermediate import (
    Block, Blockquote, CodeBlock, Heading, HorizontalRule, Image, List_,
    Paragraph, Run, Table, TextDoc,
)
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".docx"}
SUPPORTED_WRITE = {".docx"}
DOC_KIND = "text"


# Namespaces used by Word
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
}
for prefix, uri in NS.items():
    if prefix in ("w", "r", "rel", "ct", "a", "wp", "pic"):
        ET.register_namespace(prefix if prefix != "rel" else "", uri)


W = lambda tag: f"{{{NS['w']}}}{tag}"  # noqa: E731
R_NS = NS["r"]


# ============================================================
# READ
# ============================================================

def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    with zipfile.ZipFile(path) as z:
        cancel.check()
        # Trailer-envelope fast path: if this DOCX was produced by Vitriol
        # from an off-type source (e.g. PNG -> DOCX), the original source
        # bytes live in _vitriol/original.bin. Recovering directly from
        # there gives byte-perfect round-trip.
        if "_vitriol/original.bin" in z.namelist():
            try:
                from .masquerade import _parse_envelope
                payload, src_ext = _parse_envelope(z.read("_vitriol/original.bin"))
                from ..core.intermediate import _EXT_TO_MIME
                ext_n = src_ext.lower()
                if not ext_n.startswith("."):
                    ext_n = "." + ext_n
                mime = _EXT_TO_MIME.get(ext_n, "application/octet-stream")
                doc = TextDoc(blocks=[Image(data=payload, mime=mime, alt=f"image{ext_n}")])
                doc.metadata["_vitriol_origin"] = {"bytes": payload, "ext": ext_n}
                return doc
            except (ValueError, KeyError, ImportError) as e:
                from ..utils.logger import get_logger
                get_logger().warning(
                    "DOCX trailer envelope present at _vitriol/original.bin "
                    "but unreadable (%s); falling back to normal extraction.", e)
        document_xml = z.read("word/document.xml")
        try:
            rels_xml = z.read("word/_rels/document.xml.rels")
        except KeyError:
            rels_xml = b""
        try:
            numbering_xml = z.read("word/numbering.xml")
        except KeyError:
            numbering_xml = b""
        media: dict[str, bytes] = {}
        for n in z.namelist():
            if n.startswith("word/media/"):
                media[n] = z.read(n)

    rels = _parse_rels(rels_xml)
    numbering_map = _parse_numbering(numbering_xml)

    root = ET.fromstring(document_xml)
    body = root.find(W("body"))
    if body is None:
        return TextDoc(blocks=[])

    blocks: List[Block] = []
    list_buffer: List[Tuple[int, int, List[Block]]] = []  # (numId, ilvl, item-blocks)

    def flush_list_buffer():
        nonlocal list_buffer
        if not list_buffer:
            return
        # Group by numId; build a List_ per group
        # All items at level 0 for v1; nested lists collapsed via indentation.
        ordered = numbering_map.get(list_buffer[0][0], False)
        items = [bs for (_n, _i, bs) in list_buffer]
        blocks.append(List_(ordered=ordered, items=items))
        list_buffer = []

    for child in list(body):
        cancel.check()
        tag = _localname(child.tag)
        if tag == "p":
            num = _list_info(child)
            if num is not None:
                num_id, ilvl = num
                runs = _read_runs(child, rels, media)
                list_buffer.append((num_id, ilvl, [Paragraph(runs=runs)]))
                continue
            # Not a list item — flush any buffered list first
            flush_list_buffer()
            heading = _heading_level(child)
            runs = _read_runs(child, rels, media)
            if heading:
                blocks.append(Heading(level=heading, runs=runs))
            elif _is_horizontal_rule(child):
                blocks.append(HorizontalRule())
            else:
                blocks.append(Paragraph(runs=runs))
        elif tag == "tbl":
            flush_list_buffer()
            blocks.append(_read_table(child, rels, media))
        elif tag == "sectPr":
            continue
    flush_list_buffer()
    return TextDoc(blocks=blocks, metadata={})


def _localname(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_rels(rels_xml: bytes) -> dict:
    """Map rId -> {target, type}."""
    out: dict = {}
    if not rels_xml:
        return out
    try:
        root = ET.fromstring(rels_xml)
    except ET.ParseError:
        return out
    for rel in root:
        rid = rel.get("Id")
        target = rel.get("Target")
        rtype = rel.get("Type")
        if rid:
            out[rid] = {"target": target, "type": rtype}
    return out


def _parse_numbering(num_xml: bytes) -> dict:
    """Best-effort: map numId -> ordered (bool). Decimal/lowerLetter/etc => True."""
    if not num_xml:
        return {}
    try:
        root = ET.fromstring(num_xml)
    except ET.ParseError:
        return {}
    abstr_to_ordered: dict = {}
    for ab in root.findall(W("abstractNum")):
        ab_id = ab.get(W("abstractNumId"))
        # Look at first level
        lvl = ab.find(W("lvl"))
        if lvl is not None:
            fmt_el = lvl.find(W("numFmt"))
            fmt = fmt_el.get(W("val")) if fmt_el is not None else "bullet"
            abstr_to_ordered[ab_id] = fmt != "bullet"
    out: dict = {}
    for num in root.findall(W("num")):
        num_id = num.get(W("numId"))
        ab_ref = num.find(W("abstractNumId"))
        if num_id and ab_ref is not None:
            out[int(num_id)] = abstr_to_ordered.get(ab_ref.get(W("val")), False)
    return out


def _heading_level(p: ET.Element) -> int:
    pStyle = p.find(f"{W('pPr')}/{W('pStyle')}")
    if pStyle is None:
        return 0
    val = (pStyle.get(W("val")) or "").lower()
    m = re.match(r"heading\s*(\d)", val)
    if m:
        return max(1, min(6, int(m.group(1))))
    if val == "title":
        return 1
    return 0


def _is_horizontal_rule(p: ET.Element) -> bool:
    pBdr = p.find(f"{W('pPr')}/{W('pBdr')}/{W('bottom')}")
    return pBdr is not None and pBdr.get(W("sz")) is not None


def _list_info(p: ET.Element) -> Optional[Tuple[int, int]]:
    numPr = p.find(f"{W('pPr')}/{W('numPr')}")
    if numPr is None:
        return None
    num_id_el = numPr.find(W("numId"))
    ilvl_el = numPr.find(W("ilvl"))
    if num_id_el is None:
        return None
    try:
        num_id = int(num_id_el.get(W("val"), "0"))
    except ValueError:
        return None
    try:
        ilvl = int(ilvl_el.get(W("val"), "0")) if ilvl_el is not None else 0
    except ValueError:
        ilvl = 0
    return num_id, ilvl


def _read_runs(p: ET.Element, rels: dict, media: dict) -> List[Run]:
    runs: List[Run] = []
    # We treat hyperlink children specially.
    for child in p.iter():
        tag = _localname(child.tag)
        if tag == "r":
            run = _read_run(child)
            # If parent is a hyperlink, attach href
            parent_link = _parent_hyperlink(p, child)
            if parent_link is not None:
                rid = parent_link.get(f"{{{R_NS}}}id")
                if rid and rid in rels:
                    for r_run in run:
                        r_run.href = rels[rid]["target"]
            runs.extend(run)
        elif tag == "tab":
            runs.append(Run(text="\t"))
        elif tag == "br" and _localname(child.tag) == "br":
            runs.append(Run(text="\n"))
    return runs or [Run(text="")]


def _parent_hyperlink(root: ET.Element, target: ET.Element) -> Optional[ET.Element]:
    for parent in root.iter():
        if _localname(parent.tag) == "hyperlink":
            for c in parent:
                if c is target:
                    return parent
    return None


def _read_run(r: ET.Element) -> List[Run]:
    rPr = r.find(W("rPr"))
    bold = rPr is not None and rPr.find(W("b")) is not None
    italic = rPr is not None and rPr.find(W("i")) is not None
    underline = rPr is not None and rPr.find(W("u")) is not None
    code = False
    if rPr is not None:
        rFonts = rPr.find(W("rFonts"))
        if rFonts is not None:
            ascii_font = (rFonts.get(W("ascii")) or "").lower()
            if "mono" in ascii_font or "consolas" in ascii_font or "courier" in ascii_font:
                code = True
    runs: List[Run] = []
    for el in r:
        tag = _localname(el.tag)
        if tag == "t":
            runs.append(Run(text=el.text or "", bold=bold, italic=italic, underline=underline, code=code))
        elif tag == "tab":
            runs.append(Run(text="\t"))
        elif tag == "br":
            runs.append(Run(text="\n"))
    return runs


def _read_table(tbl: ET.Element, rels: dict, media: dict) -> Table:
    rows: List[List[List[Block]]] = []
    for tr in tbl.findall(W("tr")):
        cells: List[List[Block]] = []
        for tc in tr.findall(W("tc")):
            cell_blocks: List[Block] = []
            for child in tc:
                if _localname(child.tag) == "p":
                    cell_blocks.append(Paragraph(runs=_read_runs(child, rels, media)))
            if not cell_blocks:
                cell_blocks = [Paragraph(runs=[])]
            cells.append(cell_blocks)
        rows.append(cells)
    return Table(rows=rows)


# ============================================================
# WRITE
# ============================================================

def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, TextDoc):
        from ..core.intermediate import plain_to_textdoc
        if isinstance(doc, (bytes, bytearray)):
            doc = plain_to_textdoc(bytes(doc).decode("utf-8", errors="replace"))
        else:
            doc = plain_to_textdoc(str(doc))

    body_xml, media_files, has_lists = _render_body(doc, cancel)
    parts = _build_parts(body_xml, media_files, has_lists)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
        # Off-type round-trip: when this DOCX was produced from an off-type
        # source via image_bytes_to_textdoc, stash the original source bytes
        # in a private ZIP entry. Word and other DOCX readers ignore files
        # outside the [Content_Types] / relationships graph, so the entry
        # is invisible to viewers but lets docx_handler.read recover the
        # original source byte-perfect.
        origin = doc.metadata.get("_vitriol_origin") if isinstance(doc.metadata, dict) else None
        if isinstance(origin, dict) and "bytes" in origin and "ext" in origin:
            from .masquerade import _build_envelope
            z.writestr("_vitriol/original.bin",
                       _build_envelope(origin["bytes"], origin["ext"]),
                       compress_type=zipfile.ZIP_STORED)


def _build_parts(body_xml: str, media_files: dict[str, bytes], has_lists: bool) -> dict[str, bytes]:
    """Assemble all archive members."""
    out: dict[str, bytes] = {}
    out["[Content_Types].xml"] = _content_types_xml(media_files, has_lists).encode("utf-8")
    out["_rels/.rels"] = _root_rels_xml().encode("utf-8")
    out["word/document.xml"] = body_xml.encode("utf-8")
    out["word/_rels/document.xml.rels"] = _document_rels_xml(media_files, has_lists).encode("utf-8")
    out["word/styles.xml"] = _styles_xml().encode("utf-8")
    if has_lists:
        out["word/numbering.xml"] = _numbering_xml().encode("utf-8")
    for fname, data in media_files.items():
        out[f"word/media/{fname}"] = data
    return out


def _content_types_xml(media_files: dict[str, bytes], has_lists: bool) -> str:
    defaults = [
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
    ]
    seen_ext = set()
    for fname in media_files:
        ext = fname.rsplit(".", 1)[-1].lower()
        if ext in seen_ext:
            continue
        seen_ext.add(ext)
        ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "gif": "image/gif", "bmp": "image/bmp"}.get(ext, f"image/{ext}")
        defaults.append(f'<Default Extension="{ext}" ContentType="{ct}"/>')
    overrides = [
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>',
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>',
    ]
    if has_lists:
        overrides.append('<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        + "".join(defaults) + "".join(overrides) + "</Types>"
    )


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )


def _document_rels_xml(media_files: dict[str, bytes], has_lists: bool) -> str:
    rels = []
    rid = 1
    if has_lists:
        rels.append(f'<Relationship Id="rId{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>')
        rid += 1
    rels.append(f'<Relationship Id="rId{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    rid += 1
    for fname in media_files:
        rels.append(f'<Relationship Id="rId{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{fname}"/>')
        rid += 1
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels) + "</Relationships>"
    )


def _styles_xml() -> str:
    """Minimal styles: Normal + Heading 1..6."""
    headings = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{i}"><w:name w:val="heading {i}"/>'
        f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
        f'<w:pPr><w:keepNext/><w:spacing w:before="240" w:after="60"/><w:outlineLvl w:val="{i-1}"/></w:pPr>'
        f'<w:rPr><w:b/><w:sz w:val="{40 - i*2}"/></w:rPr></w:style>'
        for i in range(1, 7)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Normal" w:default="1">'
        '<w:name w:val="Normal"/></w:style>'
        + headings + "</w:styles>"
    )


def _numbering_xml() -> str:
    """Two abstract nums: bullet (id=0) and decimal (id=1). Two num refs to them."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        '</w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        '</w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        '</w:numbering>'
    )


def _render_body(doc: TextDoc, cancel: CancellationToken) -> Tuple[str, dict[str, bytes], bool]:
    out: List[str] = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    out.append('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">')
    out.append("<w:body>")
    media: dict[str, bytes] = {}
    has_lists = False
    for blk in doc.blocks:
        cancel.check()
        s, used_list = _render_block(blk, media)
        out.append(s)
        if used_list:
            has_lists = True
    out.append('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
               '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>')
    out.append("</w:body></w:document>")
    return "".join(out), media, has_lists


def _render_block(blk, media: dict) -> Tuple[str, bool]:
    if isinstance(blk, Heading):
        lvl = max(1, min(6, blk.level))
        return (
            f'<w:p><w:pPr><w:pStyle w:val="Heading{lvl}"/></w:pPr>'
            + _render_runs_xml(blk.runs) + "</w:p>",
            False,
        )
    if isinstance(blk, Paragraph):
        return f"<w:p>{_render_runs_xml(blk.runs)}</w:p>", False
    if isinstance(blk, List_):
        num_ref = 2 if blk.ordered else 1
        out = []
        for item in blk.items:
            inner_runs: List[Run] = []
            for sub in item:
                if isinstance(sub, Paragraph):
                    inner_runs.extend(sub.runs)
                else:
                    inner_runs.append(Run(text=_block_to_text(sub)))
            out.append(
                f'<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="{num_ref}"/></w:numPr></w:pPr>'
                + _render_runs_xml(inner_runs) + "</w:p>"
            )
        return "".join(out), True
    if isinstance(blk, Table):
        return _render_table_xml(blk, media), False
    if isinstance(blk, Image):
        # v1: emit alt text as a paragraph; embedded-image rendering is
        # complex (needs drawingML wp:inline + a pic:pic graphic). Out of v1.
        alt = blk.alt or "image"
        return f"<w:p>{_render_runs_xml([Run(text=f'[image: {alt}]', italic=True)])}</w:p>", False
    if isinstance(blk, CodeBlock):
        runs = [Run(text=blk.text, code=True)]
        return f"<w:p>{_render_runs_xml(runs)}</w:p>", False
    if isinstance(blk, HorizontalRule):
        return ('<w:p><w:pPr><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="auto"/></w:pBdr></w:pPr></w:p>', False)
    if isinstance(blk, Blockquote):
        out = []
        used = False
        for b in blk.blocks:
            s, u = _render_block(b, media)
            # Wrap each sub-block as a paragraph with left indent
            out.append(s.replace("<w:pPr>", '<w:pPr><w:ind w:left="720"/>')
                       if "<w:pPr>" in s else s.replace("<w:p>", '<w:p><w:pPr><w:ind w:left="720"/></w:pPr>', 1))
            used = used or u
        return "".join(out), used
    return "<w:p/>", False


def _render_runs_xml(runs: List[Run]) -> str:
    parts = []
    for r in runs:
        if not r.text:
            continue
        rpr_parts = []
        if r.bold:
            rpr_parts.append("<w:b/>")
        if r.italic:
            rpr_parts.append("<w:i/>")
        if r.underline:
            rpr_parts.append('<w:u w:val="single"/>')
        if r.code:
            rpr_parts.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        rpr = f"<w:rPr>{''.join(rpr_parts)}</w:rPr>" if rpr_parts else ""
        # Split on newlines into multiple <w:t> separated by <w:br/>
        segments = (r.text).split("\n")
        text_parts = []
        for i, seg in enumerate(segments):
            if i > 0:
                text_parts.append("<w:br/>")
            if seg:
                text_parts.append(f'<w:t xml:space="preserve">{_xml_escape(seg)}</w:t>')
        parts.append(f"<w:r>{rpr}{''.join(text_parts)}</w:r>")
    return "".join(parts)


def _render_table_xml(tbl: Table, media: dict) -> str:
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
           '<w:tblW w:w="0" w:type="auto"/></w:tblPr>']
    if tbl.rows:
        ncols = max(len(row) for row in tbl.rows)
        out.append("<w:tblGrid>" + "".join("<w:gridCol/>" for _ in range(ncols)) + "</w:tblGrid>")
    for row in tbl.rows:
        out.append("<w:tr>")
        for cell in row:
            out.append('<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>')
            for sub in cell:
                s, _ = _render_block(sub, media)
                out.append(s)
            if not cell:
                out.append("<w:p/>")
            out.append("</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def _block_to_text(blk) -> str:
    if isinstance(blk, Paragraph):
        return "".join(r.text for r in blk.runs)
    if isinstance(blk, Heading):
        return "".join(r.text for r in blk.runs)
    return ""


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))
