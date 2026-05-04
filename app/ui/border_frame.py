"""BorderFrame — paint-only widget that draws a manuscript-style inscribed
border around the entire central widget area.

Composition:
  - Outer rectangle line (thin, low opacity) ~18 px in from the widget edge.
  - Inner rectangle line (thin, slightly higher opacity) ~14 px further in.
  - A row of alchemical planetary glyphs marching between the two lines on
    all four edges, recalculated on resize so they stay evenly distributed.
  - A filled diamond glyph at each of the four corners as visual anchors.

All glyphs paint upright — manuscript convention is vertical-edge glyphs are
NOT rotated. Glyph sequence cycles through the seven classical planetary
metals (Sun/gold, Moon/silver, Mercury, Venus/copper, Mars/iron, Jupiter/tin,
Saturn/lead). All Unicode — no SVG assets needed.

Click-through (WA_TransparentForMouseEvents) so it never intercepts input.
Resizes with its parent via an event filter, mirrors VignetteOverlay's
architecture so both can coexist as siblings of the central widget.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QRectF
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QWidget


# Planetary metals sequence (7 glyphs, cycled around the perimeter).
PLANETARY_GLYPHS = "☉☽☿♀♂♃♄"  # ☉ ☽ ☿ ♀ ♂ ♃ ♄
CORNER_GLYPH = "◆"  # ◆

# Layout constants (pixels). Recompute glyph positions on resize.
OUTER_INSET = 4     # outer line offset from widget edge (tight to window)
LINE_GAP = 14       # gap between outer and inner lines
CONTENT_PAD = 8     # breathing room from inner line to UI content (info only)
CORNER_CLEARANCE = 22  # distance from a corner to the first glyph along an edge
TARGET_SPACING = 38  # desired pixel spacing between glyphs (real value rounds)
GLYPH_FONT_PT = 11
CORNER_FONT_PT = 12

# Colors — single hue (#a78bfa light purple) at four opacities.
_HUE = QColor("#a78bfa")


def _purple(alpha_byte: int) -> QColor:
    c = QColor(_HUE)
    c.setAlpha(alpha_byte)
    return c


_OUTER_LINE_COLOR = _purple(int(0.30 * 255))   # 30%
_INNER_LINE_COLOR = _purple(int(0.50 * 255))   # 50%
_GLYPH_COLOR = _purple(int(0.55 * 255))        # 55%
_CORNER_COLOR = _purple(int(0.70 * 255))       # 70%


class BorderFrame(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Sit ABOVE all sibling widgets so the inscribed lines and glyphs
        # paint over the central widget AND the status bar (the border frame
        # is parented to the QMainWindow). raise_() is also called by the
        # creator after the parent's other widgets are constructed.
        self.raise_()
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return super().eventFilter(obj, event)

    # --- paint --------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        rect = self.rect()
        if rect.width() < 2 * (OUTER_INSET + LINE_GAP) + 60:
            return  # too small to bother

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        outer = QRectF(
            rect.left() + OUTER_INSET, rect.top() + OUTER_INSET,
            rect.width() - 2 * OUTER_INSET, rect.height() - 2 * OUTER_INSET,
        )
        inner = QRectF(
            outer.left() + LINE_GAP, outer.top() + LINE_GAP,
            outer.width() - 2 * LINE_GAP, outer.height() - 2 * LINE_GAP,
        )

        # --- the two rectangles ------------------------------------------------
        pen = QPen(_OUTER_LINE_COLOR, 1.0)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(outer)

        pen.setColor(_INNER_LINE_COLOR)
        p.setPen(pen)
        p.drawRect(inner)

        # --- glyph row between the two lines, all four edges ------------------
        self._paint_glyph_row(p, outer, inner)

        # --- corner diamonds ---------------------------------------------------
        self._paint_corners(p, outer)
        p.end()

    # --- helpers ------------------------------------------------------------
    def _paint_glyph_row(self, p: QPainter, outer: QRectF, inner: QRectF) -> None:
        font = QFont()
        font.setPointSize(GLYPH_FONT_PT)
        p.setFont(font)
        p.setPen(_GLYPH_COLOR)
        fm = QFontMetricsF(font)

        # Y midline of the top band (between outer.top and inner.top)
        top_y_mid = (outer.top() + inner.top()) / 2
        bot_y_mid = (outer.bottom() + inner.bottom()) / 2
        left_x_mid = (outer.left() + inner.left()) / 2
        right_x_mid = (outer.right() + inner.right()) / 2

        # Cycle one continuous index across all four edges so the sequence
        # reads correctly when scanning the perimeter.
        glyph_index = 0

        # Top edge: x sweeps left → right
        usable = (outer.right() - outer.left()) - 2 * CORNER_CLEARANCE
        n = max(1, round(usable / TARGET_SPACING))
        spacing = usable / n
        for i in range(n):
            x = outer.left() + CORNER_CLEARANCE + spacing * (i + 0.5)
            self._draw_glyph_centered(p, fm, PLANETARY_GLYPHS[glyph_index % 7], x, top_y_mid)
            glyph_index += 1

        # Right edge: y sweeps top → bottom (continuing sequence)
        usable = (outer.bottom() - outer.top()) - 2 * CORNER_CLEARANCE
        n = max(1, round(usable / TARGET_SPACING))
        spacing = usable / n
        for i in range(n):
            y = outer.top() + CORNER_CLEARANCE + spacing * (i + 0.5)
            self._draw_glyph_centered(p, fm, PLANETARY_GLYPHS[glyph_index % 7], right_x_mid, y)
            glyph_index += 1

        # Bottom edge: x sweeps right → left so glyphs read clockwise
        usable = (outer.right() - outer.left()) - 2 * CORNER_CLEARANCE
        n = max(1, round(usable / TARGET_SPACING))
        spacing = usable / n
        for i in range(n):
            x = outer.right() - CORNER_CLEARANCE - spacing * (i + 0.5)
            self._draw_glyph_centered(p, fm, PLANETARY_GLYPHS[glyph_index % 7], x, bot_y_mid)
            glyph_index += 1

        # Left edge: y sweeps bottom → top
        usable = (outer.bottom() - outer.top()) - 2 * CORNER_CLEARANCE
        n = max(1, round(usable / TARGET_SPACING))
        spacing = usable / n
        for i in range(n):
            y = outer.bottom() - CORNER_CLEARANCE - spacing * (i + 0.5)
            self._draw_glyph_centered(p, fm, PLANETARY_GLYPHS[glyph_index % 7], left_x_mid, y)
            glyph_index += 1

    def _paint_corners(self, p: QPainter, outer: QRectF) -> None:
        font = QFont()
        font.setPointSize(CORNER_FONT_PT)
        p.setFont(font)
        p.setPen(_CORNER_COLOR)
        fm = QFontMetricsF(font)

        gap_mid = LINE_GAP / 2
        # Each corner sits on the diagonal of the band between outer & inner,
        # centered on the glyph row line midpoint.
        positions = [
            (outer.left() + gap_mid, outer.top() + gap_mid),       # top-left
            (outer.right() - gap_mid, outer.top() + gap_mid),      # top-right
            (outer.right() - gap_mid, outer.bottom() - gap_mid),   # bottom-right
            (outer.left() + gap_mid, outer.bottom() - gap_mid),    # bottom-left
        ]
        for x, y in positions:
            self._draw_glyph_centered(p, fm, CORNER_GLYPH, x, y)

    @staticmethod
    def _draw_glyph_centered(p: QPainter, fm: QFontMetricsF, ch: str,
                              cx: float, cy: float) -> None:
        w = fm.horizontalAdvance(ch)
        ascent = fm.ascent()
        descent = fm.descent()
        # Visual center: split ascent vs descent equally around cy.
        baseline_y = cy + (ascent - descent) / 2
        p.drawText(QRectF(cx - w / 2 - 2, baseline_y - ascent, w + 4, ascent + descent),
                   int(Qt.AlignmentFlag.AlignCenter), ch)
