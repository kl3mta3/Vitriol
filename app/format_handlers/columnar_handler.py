"""Columnar data formats via pyarrow: parquet, feather, orc.

All three are bridged through Tabular IR by way of an Arrow Table:
  read:  pyarrow.{parquet,feather,orc}.read_table -> python rows -> Tabular
  write: Tabular -> dict-of-columns -> pyarrow.Table -> pyarrow.{...}.write_*

Type mapping:
  - String / large_string         -> str
  - All integer widths            -> int
  - Float / double                -> float
  - Bool                          -> bool
  - Timestamp                     -> datetime
  - Anything else (binary, list)  -> repr(str)

The first row of incoming Tabular data is treated as the column header (so a
CSV→parquet round-trip stays sensible). When converting back the headers
become row 1 again.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from typing import List

from ..core.intermediate import Cell, Sheet, Tabular, TextDoc
from ..utils.cancellation import CancellationToken

SUPPORTED_READ = {".parquet", ".feather", ".orc"}
SUPPORTED_WRITE = {".parquet", ".feather", ".orc"}
DOC_KIND = "tabular"


def read(path: Path, ext: str, cancel: CancellationToken) -> Tabular:
    import pyarrow as pa
    if ext == ".parquet":
        import pyarrow.parquet as pq
        table = pq.read_table(str(path))
    elif ext == ".feather":
        import pyarrow.feather as pf
        table = pf.read_table(str(path))
    elif ext == ".orc":
        import pyarrow.orc as po
        table = po.read_table(str(path))
    else:
        raise RuntimeError(f"Unsupported columnar source: {ext}")
    cancel.check()
    return _arrow_to_tabular(table, path.stem or "Sheet1")


def write(doc, path: Path, ext: str, cancel: CancellationToken) -> None:
    import pyarrow as pa
    if not isinstance(doc, Tabular):
        if isinstance(doc, TextDoc):
            from ..core.intermediate import textdoc_to_tabular
            doc = textdoc_to_tabular(doc)
        else:
            doc = Tabular(sheets=[Sheet(name="Sheet1", rows=[])])
    sheet = doc.sheets[0] if doc.sheets else Sheet(name="Sheet1", rows=[])
    table = _tabular_to_arrow(sheet)

    if ext == ".parquet":
        import pyarrow.parquet as pq
        pq.write_table(table, str(path))
    elif ext == ".feather":
        import pyarrow.feather as pf
        pf.write_feather(table, str(path))
    elif ext == ".orc":
        import pyarrow.orc as po
        po.write_table(table, str(path))
    else:
        raise RuntimeError(f"Unsupported columnar target: {ext}")


def _arrow_to_tabular(table, sheet_name: str) -> Tabular:
    columns = table.column_names
    n_rows = table.num_rows
    rows: List[List[Cell]] = []
    # Header row
    rows.append([Cell(value=str(c)) for c in columns])
    py_cols = [table.column(i).to_pylist() for i in range(len(columns))]
    for r in range(n_rows):
        cells: List[Cell] = []
        for c in range(len(columns)):
            v = py_cols[c][r]
            if v is not None and not isinstance(v, (str, int, float, bool, datetime)):
                v = str(v)
            cells.append(Cell(value=v))
        rows.append(cells)
    return Tabular(sheets=[Sheet(name=sheet_name, rows=rows)])


def _tabular_to_arrow(sheet: Sheet):
    import pyarrow as pa
    if not sheet.rows:
        return pa.table({})
    header = [_str(c.value) for c in sheet.rows[0]]
    # Deduplicate column names (Arrow requires uniqueness).
    seen: dict = {}
    cols = []
    for h in header:
        base = h or "col"
        name = base
        i = 1
        while name in seen:
            i += 1
            name = f"{base}_{i}"
        seen[name] = True
        cols.append(name)
    n_cols = len(cols)
    column_data = [[] for _ in range(n_cols)]
    for row in sheet.rows[1:]:
        for c_idx in range(n_cols):
            v = row[c_idx].value if c_idx < len(row) else None
            column_data[c_idx].append(v)
    return pa.table({cols[i]: column_data[i] for i in range(n_cols)})


def _str(v) -> str:
    if v is None:
        return ""
    return str(v)
