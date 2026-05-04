"""XLSX read/write — recoded ZIP+XML, no openpyxl.

Scope:
  - Reads multiple sheets, cell values (string/number/bool/date), formulas
    (preserved as strings), shared-strings table.
  - Writes the same. Uses inline strings for output (simpler than maintaining
    a sharedStrings.xml). Excel and LibreOffice handle inline strings fine.
  - Does NOT support: cell styles, formatting, charts, merged cells, named
    ranges, pivot tables, conditional formatting.

XLSX layout:
  /[Content_Types].xml
  /_rels/.rels
  /xl/workbook.xml             — sheet directory + names
  /xl/_rels/workbook.xml.rels  — rels: sheets + sharedStrings
  /xl/sharedStrings.xml        — optional; we read it, don't write it
  /xl/worksheets/sheet{N}.xml  — actual cell data
"""
from __future__ import annotations
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

from ..core.intermediate import Cell, Sheet, Tabular, TextDoc
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".xlsx"}
SUPPORTED_WRITE = {".xlsx"}
DOC_KIND = "tabular"

NS_SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

S = lambda tag: f"{{{NS_SS}}}{tag}"  # noqa: E731

# Excel epoch: 1900-01-01 = 1, with the famous leap-year bug treating 1900 as a leap year.
_EXCEL_EPOCH = datetime(1899, 12, 30)


# ============================================================
# READ
# ============================================================

def read(path: Path, ext: str, cancel: CancellationToken) -> Tabular:
    with zipfile.ZipFile(path) as z:
        cancel.check()
        wb_xml = z.read("xl/workbook.xml")
        try:
            wb_rels = z.read("xl/_rels/workbook.xml.rels")
        except KeyError:
            wb_rels = b""
        try:
            shared_xml = z.read("xl/sharedStrings.xml")
        except KeyError:
            shared_xml = b""
        sheets_xml: dict[str, bytes] = {}
        for n in z.namelist():
            if n.startswith("xl/worksheets/") and n.endswith(".xml"):
                sheets_xml[n] = z.read(n)

    shared = _parse_shared_strings(shared_xml)
    sheet_refs = _parse_workbook(wb_xml)  # [(name, rId), ...]
    rels = _parse_workbook_rels(wb_rels)  # rId -> target

    sheets: List[Sheet] = []
    for name, rid in sheet_refs:
        cancel.check()
        target = rels.get(rid)
        if not target:
            continue
        # target like "worksheets/sheet1.xml" — prepend "xl/"
        full = f"xl/{target}" if not target.startswith("xl/") else target
        # Some xlsx use absolute paths starting with /
        if full.startswith("/"):
            full = full[1:]
        data = sheets_xml.get(full)
        if data is None:
            # Try common variants
            base = target.rsplit("/", 1)[-1]
            for k, v in sheets_xml.items():
                if k.endswith(base):
                    data = v
                    break
        if data is None:
            continue
        rows = _parse_sheet(data, shared)
        sheets.append(Sheet(name=name, rows=rows))
    if not sheets:
        sheets = [Sheet(name="Sheet1", rows=[])]
    return Tabular(sheets=sheets)


def _parse_shared_strings(data: bytes) -> List[str]:
    if not data:
        return []
    out: List[str] = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for si in root.findall(S("si")):
        # Either a single <t> or a series of <r><t>...</t></r>
        t = si.find(S("t"))
        if t is not None and t.text is not None:
            out.append(t.text)
            continue
        parts = []
        for r in si.findall(S("r")):
            t2 = r.find(S("t"))
            if t2 is not None and t2.text:
                parts.append(t2.text)
        out.append("".join(parts))
    return out


def _parse_workbook(data: bytes) -> List[Tuple[str, str]]:
    refs: List[Tuple[str, str]] = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return refs
    sheets_el = root.find(S("sheets"))
    if sheets_el is None:
        return refs
    for sh in sheets_el.findall(S("sheet")):
        name = sh.get("name") or "Sheet"
        rid = sh.get(f"{{{NS_REL}}}id") or sh.get("r:id") or ""
        refs.append((name, rid))
    return refs


def _parse_workbook_rels(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    if not data:
        return out
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return out
    for rel in root:
        rid = rel.get("Id")
        target = rel.get("Target")
        if rid and target:
            out[rid] = target
    return out


_COL_LETTERS_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _col_letters_to_index(letters: str) -> int:
    n = 0
    for c in letters:
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def _parse_sheet(data: bytes, shared: List[str]) -> List[List[Cell]]:
    rows: List[List[Cell]] = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return rows
    sheet_data = root.find(S("sheetData"))
    if sheet_data is None:
        return rows
    for row_el in sheet_data.findall(S("row")):
        cells: List[Cell] = []
        for c_el in row_el.findall(S("c")):
            ref = c_el.get("r")
            col_idx = 0
            if ref:
                m = _COL_LETTERS_RE.match(ref)
                if m:
                    col_idx = _col_letters_to_index(m.group(1))
            # Pad with empty cells up to col_idx
            while len(cells) < col_idx:
                cells.append(Cell())
            ctype = c_el.get("t", "n")
            v_el = c_el.find(S("v"))
            f_el = c_el.find(S("f"))
            inline_t = c_el.find(f"{S('is')}/{S('t')}")
            value = None
            if inline_t is not None:
                value = inline_t.text or ""
            elif v_el is not None and v_el.text is not None:
                raw = v_el.text
                if ctype == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = raw
                elif ctype == "b":
                    value = raw == "1"
                elif ctype == "str" or ctype == "e":
                    value = raw
                else:
                    # number — could be a date, but we lack style info to decide
                    try:
                        if "." in raw or "e" in raw or "E" in raw:
                            value = float(raw)
                        else:
                            value = int(raw)
                    except ValueError:
                        value = raw
            formula = (f_el.text if f_el is not None else None)
            cells.append(Cell(value=value, formula=formula))
        rows.append(cells)
    return rows


# ============================================================
# WRITE
# ============================================================

def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, Tabular):
        if isinstance(doc, TextDoc):
            from ..core.intermediate import textdoc_to_tabular
            doc = textdoc_to_tabular(doc)
        else:
            doc = Tabular(sheets=[Sheet(name="Sheet1", rows=[])])

    sheets = doc.sheets or [Sheet(name="Sheet1", rows=[])]
    parts: dict[str, bytes] = {}
    parts["[Content_Types].xml"] = _xlsx_content_types(len(sheets)).encode("utf-8")
    parts["_rels/.rels"] = _xlsx_root_rels().encode("utf-8")
    parts["xl/workbook.xml"] = _xlsx_workbook(sheets).encode("utf-8")
    parts["xl/_rels/workbook.xml.rels"] = _xlsx_workbook_rels(sheets).encode("utf-8")
    for i, sh in enumerate(sheets, 1):
        cancel.check()
        parts[f"xl/worksheets/sheet{i}.xml"] = _xlsx_sheet(sh).encode("utf-8")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)


def _xlsx_content_types(n_sheets: int) -> str:
    sheets = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, n_sheets + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + sheets + "</Types>"
    )


def _xlsx_root_rels() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )


def _xlsx_workbook(sheets: List[Sheet]) -> str:
    sheet_xml = "".join(
        f'<sheet name="{_xml_attr(s.name[:31])}" sheetId="{i}" r:id="rId{i}"/>'
        for i, s in enumerate(sheets, 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheet_xml}</sheets></workbook>"
    )


def _xlsx_workbook_rels(sheets: List[Sheet]) -> str:
    rels = "".join(
        f'<Relationship Id="rId{i}" '
        f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rels + "</Relationships>"
    )


def _col_letter(idx: int) -> str:
    """0-based -> A, B, ... AA, AB, ..."""
    s = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        s = chr(ord("A") + rem) + s
    return s


def _xlsx_sheet(sheet: Sheet) -> str:
    rows_xml: List[str] = []
    for r_idx, row in enumerate(sheet.rows, 1):
        cells_xml = []
        for c_idx, cell in enumerate(row):
            ref = f"{_col_letter(c_idx)}{r_idx}"
            cells_xml.append(_render_cell(cell, ref))
        rows_xml.append(f'<row r="{r_idx}">' + "".join(cells_xml) + "</row>")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>" + "".join(rows_xml) + "</sheetData></worksheet>"
    )


def _render_cell(cell: Cell, ref: str) -> str:
    v = cell.value
    if v is None and cell.formula is None:
        return f'<c r="{ref}"/>'
    if cell.formula is not None:
        # Formula with optional cached value (always a number for safety; LibreOffice recalcs anyway)
        cached = ""
        if isinstance(v, (int, float)):
            cached = f"<v>{v}</v>"
        return f'<c r="{ref}"><f>{_xml_escape(cell.formula)}</f>{cached}</c>'
    if isinstance(v, bool):
        return f'<c r="{ref}" t="b"><v>{1 if v else 0}</v></c>'
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"><v>{v}</v></c>'
    if isinstance(v, datetime):
        # Excel serial date
        delta = v - _EXCEL_EPOCH
        serial = delta.days + delta.seconds / 86400
        return f'<c r="{ref}"><v>{serial}</v></c>'
    # String — use inline string to avoid maintaining sharedStrings
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{_xml_escape(str(v))}</t></is></c>'


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def _xml_attr(s: str) -> str:
    return _xml_escape(s)
