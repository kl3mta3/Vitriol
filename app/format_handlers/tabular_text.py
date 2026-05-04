"""CSV/TSV handler. Tabular IR via stdlib csv module."""
from __future__ import annotations
import csv
import io
from datetime import datetime
from pathlib import Path
from typing import List

from ..core.intermediate import Cell, Sheet, Tabular
from ..utils.cancellation import CancellationToken
from . import charset

SUPPORTED_READ = {".csv", ".tsv"}
SUPPORTED_WRITE = {".csv", ".tsv"}
DOC_KIND = "tabular"


def read(path: Path, ext: str, cancel: CancellationToken) -> Tabular:
    raw = path.read_bytes()
    cancel.check()
    text, _ = charset.decode_with_encoding(raw)
    delim = "\t" if ext == ".tsv" else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows: List[List[Cell]] = []
    for r in reader:
        rows.append([Cell(value=v) for v in r])
    return Tabular(sheets=[Sheet(name=path.stem or "Sheet1", rows=rows)])


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    if not isinstance(doc, Tabular):
        from ..core.intermediate import textdoc_to_tabular, TextDoc
        if isinstance(doc, TextDoc):
            doc = textdoc_to_tabular(doc)
        else:
            doc = Tabular(sheets=[Sheet(name="Sheet1", rows=[])])
    delim = "\t" if ext == ".tsv" else ","
    sheet = doc.sheets[0] if doc.sheets else Sheet(name="Sheet1", rows=[])
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=delim)
        for row in sheet.rows:
            writer.writerow([_cell_to_str(c) for c in row])


def _cell_to_str(c: Cell) -> str:
    if c.value is None:
        return ""
    if isinstance(c.value, datetime):
        return c.value.isoformat()
    return str(c.value)
