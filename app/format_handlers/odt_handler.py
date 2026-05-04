"""ODT read-only handler.

Scope: parses content.xml from an OpenDocument Text file. Extracts headings,
paragraphs, lists, tables. Inline formatting (bold/italic) is dropped — we
emit plain Run text per block.

Writing ODT is deferred for v1.

ODT layout:
  /mimetype                       (application/vnd.oasis.opendocument.text)
  /content.xml                    (body content)
  /styles.xml                     (style definitions; we don't read these in v1)
  /META-INF/manifest.xml
"""
from __future__ import annotations
import re
import zipfile
from pathlib import Path
from typing import List
from xml.etree import ElementTree as ET

from ..core.intermediate import (
    Block, Heading, List_, Paragraph, Run, Table, TextDoc,
)
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".odt"}
SUPPORTED_WRITE: set[str] = set()
DOC_KIND = "text"


NS_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
NS_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
NS_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"


def read(path: Path, ext: str, cancel: CancellationToken) -> TextDoc:
    with zipfile.ZipFile(path) as z:
        cancel.check()
        try:
            content = z.read("content.xml")
        except KeyError:
            raise RuntimeError("Not a valid ODT (missing content.xml).")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise RuntimeError(f"ODT content.xml malformed: {e}")
    body = None
    for el in root.iter():
        if el.tag.endswith("}text"):
            body = el
            break
    if body is None:
        return TextDoc(blocks=[])
    blocks = _walk(body)
    return TextDoc(blocks=blocks)


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:  # pragma: no cover
    raise RuntimeError("Writing ODT is not supported in v1.")


def _all_text(el: ET.Element) -> str:
    parts: List[str] = []
    if el.text:
        parts.append(el.text)
    for child in el:
        if child.tag.endswith("}line-break"):
            parts.append("\n")
        elif child.tag.endswith("}tab"):
            parts.append("\t")
        elif child.tag.endswith("}s"):
            try:
                count = int(child.get(f"{{{NS_TEXT}}}c", "1"))
            except ValueError:
                count = 1
            parts.append(" " * count)
        else:
            parts.append(_all_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


_HEADING_LEVEL_RE = re.compile(r"Heading[_\s]?(\d)", re.IGNORECASE)


def _walk(parent: ET.Element) -> List[Block]:
    out: List[Block] = []
    for child in parent:
        local = child.tag.split("}", 1)[-1]
        if local == "h":
            try:
                lvl = int(child.get(f"{{{NS_TEXT}}}outline-level", "1"))
            except ValueError:
                lvl = 1
            lvl = max(1, min(6, lvl))
            out.append(Heading(level=lvl, runs=[Run(text=_all_text(child))]))
        elif local == "p":
            text = _all_text(child)
            if text.strip():
                out.append(Paragraph(runs=[Run(text=text)]))
        elif local == "list":
            ordered = False
            items: List[List[Block]] = []
            for li in child.findall(f"{{{NS_TEXT}}}list-item"):
                inner = _walk(li)
                if not inner:
                    inner = [Paragraph(runs=[Run(text="")])]
                items.append(inner)
            out.append(List_(ordered=ordered, items=items))
        elif local == "table":
            rows: List[List[List[Block]]] = []
            for tr in child.findall(f"{{{NS_TABLE}}}table-row"):
                cells: List[List[Block]] = []
                for tc in tr.findall(f"{{{NS_TABLE}}}table-cell"):
                    cell_blocks = _walk(tc)
                    if not cell_blocks:
                        cell_blocks = [Paragraph(runs=[])]
                    cells.append(cell_blocks)
                if cells:
                    rows.append(cells)
            if rows:
                out.append(Table(rows=rows))
        else:
            # Recurse into office:text-style containers
            out.extend(_walk(child))
    return out
