"""The scrollable playlist of files to convert. QListWidget with InternalMove."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal, QSize, QRectF
from PySide6.QtGui import QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractItemView, QFrame, QLabel, QListWidget, QListWidgetItem, QStackedLayout, QVBoxLayout, QWidget

from .playlist_item import PlaylistItemWidget, Status
from ..utils.paths import resources_dir


class Playlist(QFrame):
    items_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("PlaylistFrame")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)

        self._stack = QStackedLayout()
        layout.addLayout(self._stack)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setMovement(QListWidget.Movement.Snap)
        self.list.setUniformItemSizes(False)
        self.list.setSpacing(2)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        # Make the list transparent so the parent's watermark shows through.
        self.list.setStyleSheet("QListWidget { background: transparent; }")
        self.list.viewport().setStyleSheet("background: transparent;")

        self.empty = QLabel("No files added yet.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setObjectName("Muted")

        self._stack.addWidget(self.empty)
        self._stack.addWidget(self.list)
        self._sync_empty()

        # Logo watermark painted in this frame's background — fills the empty
        # space behind playlist items so the area never reads as a void.
        svg_path = resources_dir() / "logo.svg"
        self._svg = QSvgRenderer(str(svg_path)) if svg_path.exists() else None
        self._wm_opacity = 0.07  # quiet — items overlay this without losing legibility

    def add_paths(self, paths: Iterable[Path], masquerade: bool = False) -> None:
        added = 0
        for p in paths:
            self._add_one(Path(p), masquerade=masquerade)
            added += 1
        if added:
            self._sync_empty()
            self.items_changed.emit()

    def _add_one(self, path: Path, masquerade: bool = False) -> PlaylistItemWidget:
        widget = PlaylistItemWidget(path, masquerade=masquerade)
        widget.remove_requested.connect(self._on_remove)
        item = QListWidgetItem()
        item.setSizeHint(QSize(widget.sizeHint().width(), 50))
        # Disable per-item drag handle interaction with checkbox/buttons
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)
        self.list.addItem(item)
        self.list.setItemWidget(item, widget)
        return widget

    def items(self) -> list[PlaylistItemWidget]:
        out: list[PlaylistItemWidget] = []
        for i in range(self.list.count()):
            w = self.list.itemWidget(self.list.item(i))
            if isinstance(w, PlaylistItemWidget):
                out.append(w)
        return out

    def checked_items(self) -> list[PlaylistItemWidget]:
        return [w for w in self.items() if w.is_checked()]

    def clear(self) -> None:
        self.list.clear()
        self._sync_empty()
        self.items_changed.emit()

    def remove_widget(self, widget: PlaylistItemWidget) -> None:
        for i in range(self.list.count()):
            if self.list.itemWidget(self.list.item(i)) is widget:
                self.list.takeItem(i)
                break
        self._sync_empty()
        self.items_changed.emit()

    def paintEvent(self, event) -> None:  # noqa: N802
        # QFrame paints its QSS background first.
        super().paintEvent(event)
        if self._svg is None or self._wm_opacity <= 0.001:
            return
        # Center a square watermark sized to fit the smaller dimension with
        # ~20px breathing room. Capped at 360 so it stays elegant on huge
        # windows.
        rect = self.rect()
        side = max(120, min(rect.width() - 40, rect.height() - 40, 360))
        x = (rect.width() - side) / 2
        y = (rect.height() - side) / 2
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setOpacity(self._wm_opacity)
        self._svg.render(p, QRectF(x, y, side, side))
        p.end()

    def _on_remove(self, widget: PlaylistItemWidget) -> None:
        if widget.is_running():
            widget.stop_requested.emit(widget)
        self.remove_widget(widget)

    def _sync_empty(self) -> None:
        self._stack.setCurrentIndex(0 if self.list.count() == 0 else 1)
