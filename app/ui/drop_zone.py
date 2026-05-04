"""Drag-and-drop / click-to-browse area."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal, QSize, QPropertyAnimation, Property
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent, QPainter, QPen, QColor, QIcon, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QWidget, QVBoxLayout

from ..utils.paths import resources_dir


class DropZone(QWidget):
    files_added = Signal(list)  # list[Path]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.label = QLabel("Drag & drop files or folders here, or click to browse.")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("color: #b0b0c0; font-size: 14px; background: transparent;")
        layout.addWidget(self.label, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.browse_btn = QPushButton()
        self.browse_btn.setObjectName("BrowseFolder")
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.setToolTip("Browse for files or folders")
        self.browse_btn.setIcon(_render_folder_icon(28))
        self.browse_btn.setIconSize(QSize(28, 28))
        # Compact icon-only button: square footprint, no text padding.
        self.browse_btn.setFixedSize(QSize(40, 40))
        self.browse_btn.clicked.connect(self._browse)
        bottom.addWidget(self.browse_btn)
        layout.addLayout(bottom)

        self._hover = False

        # Watermark: render logo.svg behind the text when the playlist is
        # empty. Opacity is animated by main_window via set_watermark_opacity()
        # so the mark fades out smoothly when files are added.
        svg_path = resources_dir() / "logo.svg"
        self._svg = QSvgRenderer(str(svg_path)) if svg_path.exists() else None
        self._wm_opacity = 0.13  # base resting opacity
        self._wm_anim = QPropertyAnimation(self, b"watermarkOpacity")
        self._wm_anim.setDuration(450)

    def get_watermark_opacity(self) -> float:
        return self._wm_opacity

    def set_watermark_opacity(self, val: float) -> None:
        self._wm_opacity = max(0.0, min(1.0, float(val)))
        self.update()

    watermarkOpacity = Property(float, get_watermark_opacity, set_watermark_opacity)

    def fade_watermark(self, target: float) -> None:
        """Animate the watermark to `target` opacity. Called by main_window
        when the playlist transitions empty<->non-empty."""
        self._wm_anim.stop()
        self._wm_anim.setStartValue(self._wm_opacity)
        self._wm_anim.setEndValue(target)
        self._wm_anim.start()

    def sizeHint(self) -> QSize:
        return QSize(800, 130)

    # --- painting (dashed border + watermark) ------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#8b5cf6") if self._hover else QColor("#3a3a4a")
        pen = QPen(color, 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        bg = QColor("#15151f") if not self._hover else QColor("#1a1730")
        p.setBrush(bg)
        rect = self.rect().adjusted(2, 2, -2, -2)
        p.drawRoundedRect(rect, 10, 10)

        # Watermark: low-opacity logo centered behind the prompt text
        if self._svg is not None and self._wm_opacity > 0.001:
            p.save()
            p.setOpacity(self._wm_opacity)
            # Square target sized to fit the zone height with 12px margin
            side = max(40, min(self.height() - 24, self.width() - 24))
            x = (self.width() - side) / 2
            y = (self.height() - side) / 2
            from PySide6.QtCore import QRectF
            self._svg.render(p, QRectF(x, y, side, side))
            p.restore()
        super().paintEvent(event)

    # --- drag/drop ---------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._hover = True
            self.update()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._hover = False
        self.update()
        urls = event.mimeData().urls()
        paths: list[Path] = []
        for u in urls:
            if not u.isLocalFile():
                continue
            p = Path(u.toLocalFile())
            paths.extend(self._expand(p))
        if paths:
            self.files_added.emit(paths)
        event.acceptProposedAction()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        # Click anywhere on the zone (except the Browse button) opens a file picker.
        if event.button() == Qt.MouseButton.LeftButton:
            self._browse()
        super().mousePressEvent(event)

    # --- browse ------------------------------------------------------------------
    def _browse(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select files to convert", "", "All files (*.*)")
        if files:
            self.files_added.emit([Path(f) for f in files])

    @staticmethod
    def _expand(p: Path) -> Iterable[Path]:
        if p.is_dir():
            return [child for child in p.rglob("*") if child.is_file()]
        if p.is_file():
            return [p]
        return []


def _render_folder_icon(size: int) -> QIcon:
    """Render the muted folder.svg into a QIcon at the requested size."""
    svg_path = resources_dir() / "folder.svg"
    if not svg_path.exists():
        return QIcon()
    renderer = QSvgRenderer(str(svg_path))
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(p)
    p.end()
    return QIcon(pix)
