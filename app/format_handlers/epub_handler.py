"""EPUB read/write — recoded ZIP+XHTML, no ebooklib.

Scope:
  - EPUB 3 (with EPUB 2 read compatibility).
  - Reads spine order, chapter XHTML, title metadata.
  - Writes a single-file or multi-chapter EPUB. Each top-level Heading 1
    becomes its own chapter; if there are no Heading 1s, the whole document
    becomes one chapter.
  - Generates nav.xhtml (EPUB 3 navigation) and a spine in content.opf.

Does NOT support: DRM, fixed-layout, audio/video embeds, scripted content,
font obfuscation.

EPUB layout:
  /mimetype                       (uncompressed: "application/epub+zip")
  /META-INF/container.xml         (points to OPF)
  /OEBPS/content.opf              (manifest + spine + metadata)
  /OEBPS/nav.xhtml                (TOC)
  /OEBPS/chapter1.xhtml ...
  /OEBPS/images/...
"""
from __future__ import annotations
import re
import uuid
import zipfile
from html import escape as html_escape
from io import BytesIO
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..core.intermediate import (
    Block, Blockquote, CodeBlock, Heading, HorizontalRule, Image, List_,
    Paragraph, Run, Table, TextDoc,
)
from ..utils.cancellation import CancellationToken
from .text_plain import _html_to_textdoc, _block_to_html  # reuse existing HTML logic

SUPPORTED_READ = {".epub"}
SUPPORTED_WRITE = {".epub"}
DOC_KIND = "text"


# ============================================================
# READ
# ============================================================

def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    with zipfile.ZipFile(path) as z:
        cancel.check()
        # Trailer-envelope fast path: same scheme as docx_handler. Lets
        # PNG -> EPUB -> PNG round-trip byte-perfect.
        if "_vitriol/original.bin" in z.namelist():
            try:
                from .masquerade import _parse_envelope
                payload, src_ext = _parse_envelope(z.read("_vitriol/original.bin"))
                from ..core.intermediate import _EXT_TO_MIME, Image
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
                    "EPUB trailer envelope present at _vitriol/original.bin "
                    "but unreadable (%s); falling back to normal extraction.", e)
        # Find OPF via container.xml
        try:
            container = z.read("META-INF/container.xml")
        except KeyError:
            raise RuntimeError("EPUB is missing META-INF/container.xml")
        opf_path = _find_opf_path(container)
        if not opf_path:
            raise RuntimeError("EPUB container.xml has no rootfile")
        opf_xml = z.read(opf_path)
        base = "/".join(opf_path.split("/")[:-1])
        if base:
            base += "/"

        manifest, spine, metadata = _parse_opf(opf_xml)
        # Read each chapter in spine order, concatenate into one TextDoc.
        all_blocks: List[Block] = []
        for itemref in spine:
            cancel.check()
            item = manifest.get(itemref)
            if not item:
                continue
            href = item["href"]
            mediatype = item.get("media-type", "")
            if "html" not in mediatype and not (href.endswith(".xhtml") or href.endswith(".html")):
                continue
            full = base + href
            try:
                chapter_bytes = z.read(full)
            except KeyError:
                continue
            chapter_text = chapter_bytes.decode("utf-8", errors="replace")
            chap_doc = _html_to_textdoc(chapter_text)
            all_blocks.extend(chap_doc.blocks)
        return TextDoc(blocks=all_blocks, metadata=metadata)


def _find_opf_path(container_xml: bytes) -> Optional[str]:
    try:
        root = ET.fromstring(container_xml)
    except ET.ParseError:
        return None
    # rootfile element with full-path attr
    for el in root.iter():
        if el.tag.endswith("rootfile"):
            return el.get("full-path")
    return None


def _parse_opf(opf_xml: bytes) -> Tuple[dict, List[str], dict]:
    """Returns (manifest{id->{href,media-type}}, spine[itemref ids], metadata{title,author,...})."""
    try:
        root = ET.fromstring(opf_xml)
    except ET.ParseError:
        return {}, [], {}
    manifest: dict = {}
    spine: List[str] = []
    metadata: dict = {}
    for el in root.iter():
        local = el.tag.split("}", 1)[-1]
        if local == "item":
            iid = el.get("id")
            href = el.get("href")
            mt = el.get("media-type") or ""
            if iid and href:
                manifest[iid] = {"href": href, "media-type": mt}
        elif local == "itemref":
            idref = el.get("idref")
            if idref:
                spine.append(idref)
        elif local == "title" and el.text:
            metadata.setdefault("title", el.text)
        elif local == "creator" and el.text:
            metadata.setdefault("author", el.text)
        elif local == "language" and el.text:
            metadata.setdefault("lang", el.text)
    return manifest, spine, metadata


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

    title = doc.metadata.get("title") or "Untitled"
    author = doc.metadata.get("author") or "Unknown"
    lang = doc.metadata.get("lang") or "en"
    book_id = f"urn:uuid:{uuid.uuid4()}"

    chapters = _split_into_chapters(doc.blocks)

    # Render each chapter to XHTML
    chapter_files: List[Tuple[str, str, str]] = []  # (filename, title, xhtml)
    for i, (chap_title, blocks) in enumerate(chapters, 1):
        cancel.check()
        body_html = "".join(_block_to_html(b) for b in blocks)
        xhtml = _wrap_xhtml(chap_title or title, body_html, lang)
        chapter_files.append((f"chapter{i}.xhtml", chap_title or f"Chapter {i}", xhtml))

    nav_xhtml = _build_nav(chapter_files, title, lang)
    opf = _build_opf(title, author, lang, book_id, chapter_files)

    with zipfile.ZipFile(path, "w") as z:
        # mimetype must be FIRST and STORED (uncompressed)
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" '
                   'media-type="application/oebps-package+xml"/></rootfiles></container>',
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav_xhtml, zipfile.ZIP_DEFLATED)
        for fname, _t, xhtml in chapter_files:
            z.writestr(f"OEBPS/{fname}", xhtml, zipfile.ZIP_DEFLATED)
        # Off-type round-trip trailer envelope (same scheme as docx_handler).
        # Stash original source bytes in a private ZIP entry outside the OPF
        # manifest so EPUB readers ignore it but Vitriol can recover it.
        origin = doc.metadata.get("_vitriol_origin") if isinstance(doc.metadata, dict) else None
        if isinstance(origin, dict) and "bytes" in origin and "ext" in origin:
            from .masquerade import _build_envelope
            z.writestr("_vitriol/original.bin",
                       _build_envelope(origin["bytes"], origin["ext"]),
                       compress_type=zipfile.ZIP_STORED)


def _split_into_chapters(blocks: List[Block]) -> List[Tuple[str, List[Block]]]:
    """Each Heading 1 starts a new chapter. If there are none, return one chapter."""
    chapters: List[Tuple[str, List[Block]]] = []
    current_title = ""
    current: List[Block] = []
    for b in blocks:
        if isinstance(b, Heading) and b.level == 1:
            if current:
                chapters.append((current_title, current))
            current_title = "".join(r.text for r in b.runs).strip()
            current = [b]
        else:
            current.append(b)
    if current:
        chapters.append((current_title, current))
    if not chapters:
        chapters = [("", [])]
    return chapters


def _wrap_xhtml(title: str, body_html: str, lang: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{html_escape(lang)}" lang="{html_escape(lang)}">\n'
        f'<head><meta charset="utf-8"/><title>{html_escape(title)}</title></head>\n'
        f'<body>\n{body_html}\n</body>\n</html>\n'
    )


def _build_nav(chapters: List[Tuple[str, str, str]], title: str, lang: str) -> str:
    items = "\n".join(
        f'<li><a href="{fname}">{html_escape(t)}</a></li>'
        for fname, t, _ in chapters
    )
    body = (
        f'<nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops">'
        f'<h1>{html_escape(title)}</h1><ol>{items}</ol></nav>'
    )
    return _wrap_xhtml("Contents", body, lang)


def _build_opf(title: str, author: str, lang: str, book_id: str,
               chapters: List[Tuple[str, str, str]]) -> str:
    manifest_items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine_items = []
    for i, (fname, _t, _) in enumerate(chapters, 1):
        manifest_items.append(f'<item id="ch{i}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="ch{i}"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="bookid">{html_escape(book_id)}</dc:identifier>\n'
        f'    <dc:title>{html_escape(title)}</dc:title>\n'
        f'    <dc:creator>{html_escape(author)}</dc:creator>\n'
        f'    <dc:language>{html_escape(lang)}</dc:language>\n'
        '    <meta property="dcterms:modified">2025-01-01T00:00:00Z</meta>\n'
        '  </metadata>\n'
        '  <manifest>\n    ' + "\n    ".join(manifest_items) + "\n  </manifest>\n"
        '  <spine>\n    ' + "\n    ".join(spine_items) + "\n  </spine>\n"
        '</package>\n'
    )
