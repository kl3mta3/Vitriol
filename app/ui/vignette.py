"""VignetteOverlay — paint-only overlay that sits above the central widget
and adds a soft radial darkening + low-opacity edge rune inscriptions.

The widget is fully click-through (WA_TransparentForMouseEvents) so it
doesn't intercept any input. It resizes with its parent via a simple
event filter installed on the parent widget.

Goals:
  - Atmosphere, not visible darkening — keep alpha low (~5–8% at corners).
  - Cheap to repaint: one QRadialGradient + a handful of small rune marks.
  - Never reduce text legibility — the center stays fully transparent.
"""
from __future__ import annotations
import math

from PySide6.QtCore import Qt, QRectF, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QWidget


# Tune these once and forget. Values picked to be barely perceptible on a
# calibrated monitor — bump alpha if the room is too bright, drop it if the
# corners look smudgy.
_VIGNETTE_ALPHA = 22       # 0..255 — final alpha at the corner
_VIGNETTE_INNER = 0.55     # fraction of radius that stays fully transparent
_INSCRIPTION_ALPHA = 28    # 0..255 — alpha for the edge rune marks
_INSCRIPTION_COLOR = "#a78bfa"  # tint matches the logo's purple
_INSCRIPTION_INSET = 8     # pixels from the inner window edge
_INSCRIPTION_SPACING = 36  # pixels between rune marks along each edge


class VignetteOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # Make sure we sit on top of the parent's other children.
        self.raise_()
        # Track parent geometry — resize with it.
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    # --- parent resize tracking -------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            self.raise_()
        return super().eventFilter(obj, event)

    # --- paint ------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        self._paint_vignette(p, rect)
        self._paint_inscriptions(p, rect)
        p.end()

    @staticmethod
    def _paint_vignette(p: QPainter, rect) -> None:
        cx, cy = rect.center().x(), rect.center().y()
        radius = math.hypot(rect.width() / 2, rect.height() / 2)
        grad = QRadialGradient(QPointF(cx, cy), radius)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(_VIGNETTE_INNER, QColor(0, 0, 0, 0))
        grad.setColorAt(1.0, QColor(0, 0, 0, _VIGNETTE_ALPHA))
        p.fillRect(rect, grad)

    @staticmethod
    def _paint_inscriptions(p: QPainter, rect) -> None:
        """Tiny rune-style tick marks running along the four inner edges.
        Mostly a texture cue — at this opacity they read as fine ornament,
        not as content."""
        pen = QPen(QColor(_INSCRIPTION_COLOR))
        pen.setWidthF(0.9)
        c = QColor(_INSCRIPTION_COLOR)
        c.setAlpha(_INSCRIPTION_ALPHA)
        pen.setColor(c)
        p.setPen(pen)
        inset = _INSCRIPTION_INSET
        sp = _INSCRIPTION_SPACING
        x0, y0 = rect.left() + inset, rect.top() + inset
        x1, y1 = rect.right() - inset, rect.bottom() - inset
        # Top + bottom: small ticks pointing inward
        x = x0 + sp
        while x < x1 - sp / 2:
            p.drawLine(x, y0, x, y0 + 4)
            p.drawLine(x, y1, x, y1 - 4)
            # Every third tick gets a tiny diamond above for variety
            if int((x - x0) / sp) % 3 == 0:
                p.drawLine(x - 2, y0 + 2, x, y0)
                p.drawLine(x + 2, y0 + 2, x, y0)
                p.drawLine(x - 2, y1 - 2, x, y1)
                p.drawLine(x + 2, y1 - 2, x, y1)
            x += sp
        # Left + right
        y = y0 + sp
        while y < y1 - sp / 2:
            p.drawLine(x0, y, x0 + 4, y)
            p.drawLine(x1, y, x1 - 4, y)
            if int((y - y0) / sp) % 3 == 0:
                p.drawLine(x0 + 2, y - 2, x0, y)
                p.drawLine(x0 + 2, y + 2, x0, y)
                p.drawLine(x1 - 2, y - 2, x1, y)
                p.drawLine(x1 - 2, y + 2, x1, y)
            y += sp
