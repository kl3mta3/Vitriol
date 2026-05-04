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

from PySide6.QtCore import Qt, QPointF, QEvent
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QWidget


# Tune these once and forget. Goal: vignette barely perceptible, atmosphere
# only — the inscribed BorderFrame now carries the visible ornamentation.
_VIGNETTE_ALPHA = 15       # 0..255 — final alpha at the corner (was 22)
_VIGNETTE_INNER = 0.70     # fraction of radius that stays fully transparent (was 0.55)
_VIGNETTE_MID_FRAC = 0.85  # extra gradient stop for a smoother roll-off
_VIGNETTE_MID_ALPHA = 8    # alpha at the mid stop


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
        cx, cy = rect.center().x(), rect.center().y()
        radius = math.hypot(rect.width() / 2, rect.height() / 2)
        grad = QRadialGradient(QPointF(cx, cy), radius)
        grad.setColorAt(0.0, QColor(0, 0, 0, 0))
        grad.setColorAt(_VIGNETTE_INNER, QColor(0, 0, 0, 0))
        grad.setColorAt(_VIGNETTE_MID_FRAC, QColor(0, 0, 0, _VIGNETTE_MID_ALPHA))
        grad.setColorAt(1.0, QColor(0, 0, 0, _VIGNETTE_ALPHA))
        p.fillRect(rect, grad)
        p.end()
