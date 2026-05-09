"""vCard (.vcf) read+write via Tabular IR.

Reads a multi-card .vcf file into a single sheet where:
  - row 0 is a header (FN, N, TEL, EMAIL, ORG, TITLE, ADR, NOTE)
  - each remaining row is one card

Reads VERSION 3.0 cleanly. 4.0 mostly works (we ignore the JSON address
encoding). 2.1 we can read but the QP/UTF-7 escaping isn't decoded —
that format is rare today.

Writes VERSION 3.0 cards. Each Tabular row becomes a card; the column
header dictates the property names.
"""
from __future__ import annotations
from pathlib import Path
from typing import List

from ..core.intermediate import Cell, Sheet, Tabular, TextDoc
from ..utils.cancellation import CancellationToken
from . import charset

SUPPORTED_READ = {".vcf"}
SUPPORTED_WRITE = {".vcf"}
DOC_KIND = "tabular"

# Properties exposed as columns (in this order).
_DEFAULT_COLS = ["FN", "N", "TEL", "EMAIL", "ORG", "TITLE", "ADR", "NOTE"]


def read(path: Path, ext: str, cancel: CancellationToken) -> Tabular:
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cards = _split_cards(text)

    # Discover all properties present, biased toward the canonical list.
    seen: dict = {p: True for p in _DEFAULT_COLS}
    parsed_cards: List[dict] = []
    for c in cards:
        d = _parse_card(c)
        parsed_cards.append(d)
        for k in d:
            seen.setdefault(k, True)

    cols = list(seen.keys())
    rows: List[List[Cell]] = [[Cell(value=c) for c in cols]]
    for d in parsed_cards:
        rows.append([Cell(value=d.get(c, "")) for c in cols])
    return Tabular(sheets=[Sheet(name="Contacts", rows=rows)])


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, Tabular):
        if isinstance(doc, TextDoc):
            from ..core.intermediate import textdoc_to_tabular
            doc = textdoc_to_tabular(doc)
        else:
            raise RuntimeError("VCF writer requires a Tabular doc.")

    if not doc.sheets or not doc.sheets[0].rows:
        path.write_text("", encoding="utf-8")
        return
    sheet = doc.sheets[0]
    header = [str(c.value) if c.value is not None else "" for c in sheet.rows[0]]
    out: List[str] = []
    for row in sheet.rows[1:]:
        out.append("BEGIN:VCARD")
        out.append("VERSION:3.0")
        for i, col in enumerate(header):
            v = row[i].value if i < len(row) else None
            if v in (None, ""):
                continue
            prop = col.upper().strip() or f"X-COL-{i}"
            out.append(f"{prop}:{_escape_value(str(v))}")
        out.append("END:VCARD")
    path.write_text("\r\n".join(out) + "\r\n", encoding="utf-8")


# ---- parsing -------------------------------------------------------------

def _split_cards(text: str) -> List[List[str]]:
    """Yield each BEGIN/END:VCARD block as a list of unfolded lines."""
    cards: List[List[str]] = []
    cur: List[str] = []
    in_card = False
    for line in _unfold(text.split("\n")):
        s = line.strip()
        if s.upper() == "BEGIN:VCARD":
            in_card = True
            cur = []
            continue
        if s.upper() == "END:VCARD":
            in_card = False
            if cur:
                cards.append(cur)
            cur = []
            continue
        if in_card and s:
            cur.append(s)
    return cards


def _unfold(lines):
    """RFC 6350 line folding: continuation lines start with space/tab."""
    buf: List[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and buf:
            buf[-1] += line[1:]
        else:
            buf.append(line)
    return buf


def _parse_card(lines: List[str]) -> dict:
    out: dict = {}
    for line in lines:
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        # head is like "TEL;TYPE=CELL" or just "FN".
        prop = head.split(";", 1)[0].upper().strip()
        if prop in ("VERSION", "REV", "PRODID"):
            continue
        decoded = _unescape_value(value)
        # Multi-occurrence (e.g., multiple TEL or EMAIL) — concatenate.
        if prop in out:
            out[prop] = f"{out[prop]}; {decoded}"
        else:
            out[prop] = decoded
    return out


def _unescape_value(s: str) -> str:
    out_chars = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "n" or nxt == "N":
                out_chars.append("\n")
            elif nxt == ",":
                out_chars.append(",")
            elif nxt == ";":
                out_chars.append(";")
            elif nxt == "\\":
                out_chars.append("\\")
            else:
                out_chars.append(nxt)
            i += 2
            continue
        out_chars.append(c)
        i += 1
    return "".join(out_chars)


def _escape_value(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace("\n", "\\n")
             .replace(";", "\\;")
             .replace(",", "\\,"))
