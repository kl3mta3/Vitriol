"""Drag-and-drop / click-to-browse area."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QMouseEvent, QPainter, QPen, QColor
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QWidget, QVBoxLayout


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
        self.label.setStyleSheet("color: #b0b0c0; font-size: 14px;")
        layout.addWidget(self.label, 1)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setObjectName("Secondary")
        self.browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.browse_btn.clicked.connect(self._browse)
        bottom.addWidget(self.browse_btn)
        layout.addLayout(bottom)

        self._hover = False

    def sizeHint(self) -> QSize:
        return QSize(800, 130)

    # --- painting (dashed border) -------------------------------------------------
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
