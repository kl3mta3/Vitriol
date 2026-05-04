"""StatusGlyph — custom-paint widget that replaces the per-row status QLabel.

Four states drive the paint:
  QUEUED   — empty thin-stroke ring (#3b82f6, blue)
  RUNNING  — rotating mini transmutation glyph (1 turn / ~3s, #f1c40f → flashes)
  DONE     — filled disc with inscribed checkmark (#2ecc71, with brief #d4a574 gold flash)
  ERROR    — filled disc with inscribed X (#e74c3c, with brief flash)

Same 14×14 fixed footprint as the old QLabel so the playlist row layout
doesn't shift. Painting is QPainter-based; the rotating animation uses a
QVariantAnimation driving a single integer property (degrees), repainting
on change. GPU compositing is left to Qt.
"""
from __future__ import annotations
import math

from PySide6.QtCore import Qt, QSize, QVariantAnimation, QEasingCurve, Signal, Property
from PySide6.QtGui import QPainter, QPen, QColor, QBrush
from PySide6.QtWidgets import QWidget

from ._status import Status


_GOLD = QColor("#d4a574")
_RED_FLASH = QColor("#e74c3c")
_BLUE = QColor("#3b82f6")
_YELLOW = QColor("#f1c40f")
_GREEN = QColor("#2ecc71")
_RED = QColor("#e74c3c")


class StatusGlyph(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._status: Status = Status.QUEUED
        self._rot_deg: int = 0
        self._flash_t: float = 0.0  # 0..1 flash interpolation, fades to 0
        self._flash_color: QColor | None = None

        # Rotation animation (looped while RUNNING).
        self._rot_anim = QVariantAnimation(self)
        self._rot_anim.setStartValue(0)
        self._rot_anim.setEndValue(360)
        self._rot_anim.setDuration(3000)
        self._rot_anim.setLoopCount(-1)
        self._rot_anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._rot_anim.valueChanged.connect(self._on_rot)

        # Flash animation (one-shot on DONE / ERROR).
        self._flash_anim = QVariantAnimation(self)
        self._flash_anim.setStartValue(1.0)
        self._flash_anim.setEndValue(0.0)
        self._flash_anim.setDuration(450)
        self._flash_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._flash_anim.valueChanged.connect(self._on_flash)

    def sizeHint(self) -> QSize:
        return QSize(14, 14)

    def status(self) -> Status:
        return self._status

    def set_status(self, status: Status) -> None:
        prev = self._status
        self._status = status
        if status == Status.RUNNING:
            if self._rot_anim.state() != QVariantAnimation.State.Running:
                self._rot_anim.start()
        else:
            self._rot_anim.stop()

        # Trigger a flash on terminal-state transitions
        if status == Status.DONE and prev != Status.DONE:
            self._fire_flash(_GOLD)
        elif status == Status.ERROR and prev != Status.ERROR:
            self._fire_flash(_RED_FLASH)
        else:
            self._flash_t = 0.0
            self._flash_color = None
        self.update()

    def _fire_flash(self, color: QColor) -> None:
        self._flash_color = color
        self._flash_t = 1.0
        self._flash_anim.stop()
        self._flash_anim.start()

    def _on_rot(self, val) -> None:
        self._rot_deg = int(val)
        self.update()

    def _on_flash(self, val) -> None:
        self._flash_t = float(val)
        if self._flash_t <= 0.0:
            self._flash_color = None
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        cx, cy, r = 7, 7, 6
        if self._status == Status.QUEUED:
            self._paint_ring(p, cx, cy, r, _BLUE)
        elif self._status == Status.RUNNING:
            self._paint_rotor(p, cx, cy, r, _YELLOW)
        elif self._status == Status.DONE:
            base = self._mix(_GREEN, self._flash_color, self._flash_t) if self._flash_color else _GREEN
            self._paint_disc(p, cx, cy, r, base)
            self._paint_check(p, cx, cy, r)
        elif self._status == Status.ERROR:
            base = self._mix(_RED, self._flash_color, self._flash_t) if self._flash_color else _RED
            self._paint_disc(p, cx, cy, r, base)
            self._paint_x(p, cx, cy, r)
        p.end()

    @staticmethod
    def _mix(base: QColor, flash: QColor | None, t: float) -> QColor:
        if not flash or t <= 0:
            return base
        # Lerp from base toward flash by t.
        r = int(base.red()   * (1 - t) + flash.red()   * t)
        g = int(base.green() * (1 - t) + flash.green() * t)
        b = int(base.blue()  * (1 - t) + flash.blue()  * t)
        return QColor(r, g, b)

    @staticmethod
    def _paint_ring(p: QPainter, cx: int, cy: int, r: int, color: QColor) -> None:
        pen = QPen(color, 1.6)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

    @staticmethod
    def _paint_disc(p: QPainter, cx: int, cy: int, r: int, color: QColor) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)

    @staticmethod
    def _paint_check(p: QPainter, cx: int, cy: int, r: int) -> None:
        pen = QPen(QColor("#0a0a0f"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(cx - 3, cy, cx - 1, cy + 2)
        p.drawLine(cx - 1, cy + 2, cx + 3, cy - 2)

    @staticmethod
    def _paint_x(p: QPainter, cx: int, cy: int, r: int) -> None:
        pen = QPen(QColor("#0a0a0f"), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(cx - 2, cy - 2, cx + 2, cy + 2)
        p.drawLine(cx - 2, cy + 2, cx + 2, cy - 2)

    def _paint_rotor(self, p: QPainter, cx: int, cy: int, r: int, color: QColor) -> None:
        """Rotating mini transmutation glyph: thin ring + inscribed triangle
        that spins around the center. Suggests live alchemical work without
        being so busy that it competes with the title-bar progress overlay."""
        pen = QPen(color, 1.3)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Outer ring (static)
        p.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        # Inscribed triangle (rotating). Vertices on a circle of radius r-1.
        rr = r - 1
        verts = []
        for i in range(3):
            ang_deg = self._rot_deg + i * 120 - 90
            ang = math.radians(ang_deg)
            x = cx + rr * math.cos(ang)
            y = cy + rr * math.sin(ang)
            verts.append((x, y))
        p.drawLine(int(verts[0][0]), int(verts[0][1]), int(verts[1][0]), int(verts[1][1]))
        p.drawLine(int(verts[1][0]), int(verts[1][1]), int(verts[2][0]), int(verts[2][1]))
        p.drawLine(int(verts[2][0]), int(verts[2][1]), int(verts[0][0]), int(verts[0][1]))
        # Center dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(color))
        p.drawEllipse(cx - 1, cy - 1, 2, 2)
