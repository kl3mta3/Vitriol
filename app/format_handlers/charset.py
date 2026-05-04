"""Recoded charset detection.

Strategy (in order):
  1. BOM sniff: UTF-8, UTF-16-LE, UTF-16-BE, UTF-32-LE, UTF-32-BE.
  2. Try strict UTF-8 decode (the modern default).
  3. Heuristic: count zero bytes in even/odd positions to detect UTF-16
     without BOM. If >30% of bytes in alternating positions are zero, treat
     as UTF-16.
  4. Try common single-byte encodings in order: cp1252, latin-1.

Returns the decoded text. Never raises — falls back to latin-1 with errors
replaced as a last resort, since latin-1 covers all 256 byte values.
"""
from __future__ import annotations
from typing import Tuple

_BOMS: list[Tuple[bytes, str]] = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]


def decode(data: bytes) -> str:
    text, _enc = decode_with_encoding(data)
    return text


def decode_with_encoding(data: bytes) -> Tuple[str, str]:
    if not data:
        return "", "utf-8"

    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                pass

    # Strict UTF-8
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    # UTF-16 heuristic (text mostly within Latin range produces lots of \x00s)
    sample = data[:4096]
    if len(sample) >= 32:
        even_zero = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
        odd_zero = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
        even_total = (len(sample) + 1) // 2
        odd_total = len(sample) // 2
        if odd_total and odd_zero / odd_total > 0.30 and even_zero < odd_zero // 2:
            try:
                return data.decode("utf-16-le"), "utf-16-le"
            except UnicodeDecodeError:
                pass
        if even_total and even_zero / even_total > 0.30 and odd_zero < even_zero // 2:
            try:
                return data.decode("utf-16-be"), "utf-16-be"
            except UnicodeDecodeError:
                pass

    # cp1252 — superset of ASCII, common on Windows
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        pass

    return data.decode("latin-1", errors="replace"), "latin-1"


def encode(text: str, encoding: str = "utf-8") -> bytes:
    """Encode with BOM for UTF-16 variants if requested explicitly."""
    if encoding == "utf-8-sig":
        return b"\xef\xbb\xbf" + text.encode("utf-8")
    return text.encode(encoding, errors="strict")
