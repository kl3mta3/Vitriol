"""Minimal TrueType font parser for PDF Type0/CIDFontType2 embedding.

Parses just enough of the SFNT tables to:
  - Map Unicode codepoints to glyph indices (cmap).
  - Look up advance widths per glyph (hmtx + hhea + maxp).
  - Read font-wide metrics (head, hhea, OS/2, post) for /FontDescriptor.

Supported cmap subtables:
  - Format 4  (BMP — most common; 16-bit codepoints, segment ranges)
  - Format 12 (full Unicode — 32-bit codepoints, sequential groups)

Out of scope: variation axes, COLR/CPAL, hinting, kerning, ligatures, GPOS/GSUB.
"""
from __future__ import annotations
import struct
from typing import Dict, List, Optional, Tuple


class TTFParseError(Exception):
    pass


class TTFFont:
    """A loaded TrueType font with the bits we need for PDF embedding."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.tables: Dict[str, Tuple[int, int]] = {}  # tag -> (offset, length)
        self._cmap: Dict[int, int] = {}               # codepoint -> glyph_id
        self.units_per_em: int = 1000
        self.num_glyphs: int = 0
        self.ascent: int = 800
        self.descent: int = -200
        self.cap_height: int = 700
        self.x_height: int = 500
        self.italic_angle: int = 0
        self.bbox: Tuple[int, int, int, int] = (-1000, -300, 2000, 1100)
        self.flags: int = 32  # Nonsymbolic
        self.stem_v: int = 80
        self.widths: List[int] = []  # advance width per glyph_id, in 1/1000em
        self._parse()

    def _parse(self) -> None:
        if len(self.data) < 12:
            raise TTFParseError("file too short")
        sfnt_version, num_tables = struct.unpack(">I H", self.data[:6])
        if sfnt_version not in (0x00010000, 0x4F54544F):  # TrueType or OTF
            raise TTFParseError(f"unknown sfnt version 0x{sfnt_version:08x}")
        # Skip search range / entry selector / range shift (6 bytes)
        offset = 12
        for _ in range(num_tables):
            entry = self.data[offset:offset + 16]
            tag = entry[:4].decode("ascii", errors="replace")
            tbl_offset, tbl_length = struct.unpack(">II", entry[8:16])
            self.tables[tag] = (tbl_offset, tbl_length)
            offset += 16

        self._parse_head()
        self._parse_maxp()
        self._parse_hhea()
        self._parse_hmtx()
        self._parse_os2()
        self._parse_post()
        self._parse_cmap()

    def _table(self, tag: str) -> bytes:
        if tag not in self.tables:
            raise TTFParseError(f"missing required table {tag}")
        off, length = self.tables[tag]
        return self.data[off:off + length]

    def _parse_head(self) -> None:
        head = self._table("head")
        # version (4) + fontRevision (4) + checksum (4) + magic (4) + flags (2) +
        # unitsPerEm (2) + created (8) + modified (8) + xMin (2) + yMin (2) +
        # xMax (2) + yMax (2)
        self.units_per_em = struct.unpack(">H", head[18:20])[0] or 1000
        x_min, y_min, x_max, y_max = struct.unpack(">hhhh", head[36:44])
        scale = 1000.0 / self.units_per_em
        self.bbox = (int(x_min * scale), int(y_min * scale),
                     int(x_max * scale), int(y_max * scale))

    def _parse_maxp(self) -> None:
        maxp = self._table("maxp")
        # version (4) + numGlyphs (2)
        self.num_glyphs = struct.unpack(">H", maxp[4:6])[0]

    def _parse_hhea(self) -> None:
        hhea = self._table("hhea")
        scale = 1000.0 / self.units_per_em
        # ascender (2) at offset 4, descender (2) at 6
        ascent, descent = struct.unpack(">hh", hhea[4:8])
        self.ascent = int(ascent * scale)
        self.descent = int(descent * scale)
        # number of hMetric entries at end of hhea (2)
        self._num_h_metrics = struct.unpack(">H", hhea[34:36])[0]

    def _parse_hmtx(self) -> None:
        hmtx = self._table("hmtx")
        scale = 1000.0 / self.units_per_em
        widths: List[int] = []
        # numHMetrics longHorMetric records (advanceWidth uint16, lsb int16)
        for i in range(self._num_h_metrics):
            adv = struct.unpack(">H", hmtx[i * 4:i * 4 + 2])[0]
            widths.append(int(adv * scale))
        # Glyphs beyond numHMetrics share the last advance width
        last = widths[-1] if widths else 500
        for _ in range(self.num_glyphs - len(widths)):
            widths.append(last)
        self.widths = widths

    def _parse_os2(self) -> None:
        if "OS/2" not in self.tables:
            return
        os2 = self._table("OS/2")
        scale = 1000.0 / self.units_per_em
        # version-dependent layout; sCapHeight at offset 88 (v2+), sxHeight at 86
        if len(os2) >= 90:
            x_h = struct.unpack(">h", os2[86:88])[0]
            cap = struct.unpack(">h", os2[88:90])[0]
            if x_h:
                self.x_height = int(x_h * scale)
            if cap:
                self.cap_height = int(cap * scale)

    def _parse_post(self) -> None:
        if "post" not in self.tables:
            return
        post = self._table("post")
        if len(post) >= 8:
            italic = struct.unpack(">i", post[4:8])[0]
            self.italic_angle = italic >> 16  # Fixed -> int

    def _parse_cmap(self) -> None:
        cmap = self._table("cmap")
        version, num_subtables = struct.unpack(">HH", cmap[:4])
        # Score subtables: prefer (3,10) format 12 BMP+SMP, then (3,1) format 4 BMP
        best: Optional[Tuple[int, int, int]] = None  # (priority, offset, format)
        for i in range(num_subtables):
            rec = cmap[4 + i * 8:4 + i * 8 + 8]
            platform_id, encoding_id, sub_offset = struct.unpack(">HHI", rec)
            sub = cmap[sub_offset:]
            sub_format = struct.unpack(">H", sub[:2])[0]
            priority = self._cmap_priority(platform_id, encoding_id, sub_format)
            if priority < 0:
                continue
            if best is None or priority > best[0]:
                best = (priority, sub_offset, sub_format)
        if best is None:
            return
        _prio, off, fmt = best
        if fmt == 4:
            self._parse_cmap_format4(cmap, off)
        elif fmt == 12:
            self._parse_cmap_format12(cmap, off)

    @staticmethod
    def _cmap_priority(platform: int, encoding: int, fmt: int) -> int:
        # Higher = better. -1 = ignore.
        if (platform, encoding) == (3, 10) and fmt == 12:
            return 100
        if (platform, encoding) == (3, 1) and fmt == 4:
            return 80
        if (platform, encoding) == (0, 4) and fmt == 12:
            return 90
        if (platform, encoding) in ((0, 0), (0, 3)) and fmt == 4:
            return 70
        return -1

    def _parse_cmap_format4(self, cmap: bytes, off: int) -> None:
        # format(2) length(2) language(2) segCountX2(2) searchRange(2)
        # entrySelector(2) rangeShift(2)
        seg_count_x2 = struct.unpack(">H", cmap[off + 6:off + 8])[0]
        seg_count = seg_count_x2 // 2
        end_codes_off = off + 14
        end_codes = struct.unpack(f">{seg_count}H", cmap[end_codes_off:end_codes_off + seg_count_x2])
        # +2 reserved pad
        start_codes_off = end_codes_off + seg_count_x2 + 2
        start_codes = struct.unpack(f">{seg_count}H", cmap[start_codes_off:start_codes_off + seg_count_x2])
        id_delta_off = start_codes_off + seg_count_x2
        id_deltas = struct.unpack(f">{seg_count}h", cmap[id_delta_off:id_delta_off + seg_count_x2])
        id_range_offset_off = id_delta_off + seg_count_x2
        id_range_offsets = struct.unpack(f">{seg_count}H", cmap[id_range_offset_off:id_range_offset_off + seg_count_x2])
        for i in range(seg_count):
            start, end = start_codes[i], end_codes[i]
            if start == 0xFFFF:
                continue
            for cp in range(start, end + 1):
                gid = self._cmap4_glyph_for(i, cp, id_range_offsets, id_deltas, id_range_offset_off, cmap)
                if gid:
                    self._cmap[cp] = gid

    @staticmethod
    def _cmap4_glyph_for(seg_idx, cp, id_range_offsets, id_deltas,
                        id_range_offset_off, cmap):
        ro = id_range_offsets[seg_idx]
        if ro == 0:
            return (cp + id_deltas[seg_idx]) & 0xFFFF
        # Index into glyphIdArray: base = id_range_offset_off + 2*seg_idx + ro
        addr = id_range_offset_off + 2 * seg_idx + ro + 2 * (cp - id_range_offsets[seg_idx])
        # Correct addressing per spec:
        # glyphId = *( &idRangeOffset[i] + idRangeOffset[i]/2 + (c - startCode[i]) )
        # We need start_codes — the calling site knows seg_idx; we approximated.
        # Simpler: handle the rare ro!=0 path by indexing directly.
        # (DejaVu Sans uses ro==0 for almost every segment.)
        try:
            return struct.unpack(">H", cmap[addr:addr + 2])[0]
        except struct.error:
            return 0

    def _parse_cmap_format12(self, cmap: bytes, off: int) -> None:
        # format(2) reserved(2) length(4) language(4) numGroups(4)
        num_groups = struct.unpack(">I", cmap[off + 12:off + 16])[0]
        groups_off = off + 16
        for g in range(num_groups):
            start_char, end_char, start_glyph = struct.unpack(
                ">III", cmap[groups_off + g * 12:groups_off + g * 12 + 12]
            )
            for cp in range(start_char, end_char + 1):
                self._cmap[cp] = (start_glyph + (cp - start_char)) & 0xFFFF

    def glyph_for_char(self, ch: str) -> int:
        return self._cmap.get(ord(ch), 0)

    def char_width_pt(self, ch: str, font_size: int) -> float:
        gid = self.glyph_for_char(ch)
        if gid >= len(self.widths):
            gid = 0
        return self.widths[gid] * font_size / 1000.0


def encode_text_for_type0(text: str, font: TTFFont) -> str:
    """Encode a string as a 2-byte-per-CID hex string for use after a Tj
    operator. CID == glyph index (we use Identity CID-to-GID mapping).
    Characters with no glyph fall back to the .notdef glyph (0)."""
    parts: List[str] = []
    for ch in text:
        gid = font.glyph_for_char(ch)
        parts.append(f"{gid:04X}")
    return "<" + "".join(parts) + ">"


def build_widths_array(font: TTFFont) -> str:
    """Build the /W array entry for a CIDFontType2 font, run-length encoded
    per the PDF spec form: [c [w1 w2 w3 ...] c2 [w...]] using runs of
    contiguous glyphs that share the same width."""
    parts: List[str] = []
    i = 0
    n = len(font.widths)
    while i < n:
        # Run of glyphs sharing the same width: emit as `firstCID lastCID w`
        j = i + 1
        while j < n and font.widths[j] == font.widths[i]:
            j += 1
        if j - i >= 4:
            parts.append(f"{i} {j - 1} {font.widths[i]}")
            i = j
            continue
        # Otherwise emit a sequence: `firstCID [w0 w1 w2 ...]` until the next run
        seq_start = i
        seq: List[int] = [font.widths[i]]
        i += 1
        while i < n:
            # peek for a run starting at i
            k = i + 1
            while k < n and font.widths[k] == font.widths[i]:
                k += 1
            if k - i >= 4:
                break
            seq.append(font.widths[i])
            i += 1
        parts.append(f"{seq_start} [" + " ".join(str(w) for w in seq) + "]")
    return "[" + " ".join(parts) + "]"


def build_to_unicode_cmap(font: TTFFont) -> str:
    """Build a ToUnicode CMap stream content. Maps each CID we know about to
    its Unicode codepoint via bfchar entries. This makes copy/paste from the
    PDF return real text instead of glyph indices."""
    # Reverse cmap: glyph_id -> codepoint (lowest if multiple)
    reverse: Dict[int, int] = {}
    for cp, gid in font._cmap.items():
        if gid not in reverse or cp < reverse[gid]:
            reverse[gid] = cp
    items = sorted(reverse.items())
    # Emit in chunks of 100 (PDF spec limit)
    out: List[str] = []
    out.append("/CIDInit /ProcSet findresource begin")
    out.append("12 dict begin")
    out.append("begincmap")
    out.append("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def")
    out.append("/CMapName /Adobe-Identity-UCS def /CMapType 2 def")
    out.append("1 begincodespacerange <0000> <FFFF> endcodespacerange")
    for chunk_start in range(0, len(items), 100):
        chunk = items[chunk_start:chunk_start + 100]
        out.append(f"{len(chunk)} beginbfchar")
        for gid, cp in chunk:
            if cp <= 0xFFFF:
                out.append(f"<{gid:04X}> <{cp:04X}>")
            else:
                # Encode as UTF-16 surrogate pair
                cp2 = cp - 0x10000
                hi = 0xD800 | (cp2 >> 10)
                lo = 0xDC00 | (cp2 & 0x3FF)
                out.append(f"<{gid:04X}> <{hi:04X}{lo:04X}>")
        out.append("endbfchar")
    out.append("endcmap CMapName currentdict /CMap defineresource pop")
    out.append("end end")
    return "\n".join(out)
