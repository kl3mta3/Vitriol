"""The scrollable playlist of files to convert. QListWidget with InternalMove."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal, QSize, QRectF
from PySide6.QtGui import QPainter, QPixmap, QImage, QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractItemView, QFrame, QLabel, QListWidget, QListWidgetItem, QStackedLayout, QVBoxLayout, QWidget

from .playlist_item import PlaylistItemWidget, Status
from ..utils.paths import resources_dir


# Stone-active watermark glow color — muted alchemical red. The historical
# Philosopher's Stone is depicted as deep red in alchemical literature,
# and this matches the tint on the Stone toggle's checkbox text when
# active (see main_window._refresh_stone_visuals). Note: the LOGO itself
# always renders in its native SVG colors (purple/blue gradient). Only
# the glow halo behind the logo is red.
_STONE_RED = QColor("#c0392b")
# Watermark opacity scheme. The logo is ALWAYS visible at a SINGLE
# fixed opacity — toggling Stone mode does NOT change the logo's
# brightness. The red glow halo BEHIND the logo is the only thing
# that appears/disappears with Stone state. This way the logo reads
# as decoration regardless of mode, and the glow alone signals
# "alchemy is engaged."
_WM_OPACITY_LOGO     = 0.18    # constant logo opacity, never changes
_WM_OPACITY_GLOW_ON  = 0.79    # bright halo so it clearly reads behind the logo
# Glow blur radius (px). Controls how far the halo extends past the
# logo's bounding box. The glow pixmap canvas is sized at side + 2*radius
# so the blur doesn't clip at the boundary. 40 px gives a wide soft halo
# that reads as a glow rather than a tight outline.
_WM_GLOW_RADIUS = 40


def _gaussian_blur_qimage(src: QImage, radius: int) -> QPixmap:
    """Run a Gaussian blur on a QImage and return the result as a QPixmap.

    Implementation routes through PIL (already a project dependency for
    the MKV pipeline). PySide6 has no QImage-level blur primitive
    (QGraphicsBlurEffect operates on QGraphicsItem, not free QImages),
    so PIL is the cleanest path. Performance: ~5 ms on a ~400×400 RGBA
    canvas — runs once per resize, never per-paint."""
    from PIL import Image, ImageFilter
    src = src.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = src.width(), src.height()
    # constBits() returns a memoryview-like; tobytes() gives a raw copy
    # PIL can wrap. The bytes-per-line may include row padding on some
    # platforms, so handle that by passing the explicit stride to PIL.
    bpl = src.bytesPerLine()
    raw = bytes(src.constBits())
    if bpl == w * 4:
        pil = Image.frombytes("RGBA", (w, h), raw)
    else:
        # Strip per-row padding into a tightly packed buffer.
        rows = [raw[i * bpl:i * bpl + w * 4] for i in range(h)]
        pil = Image.frombytes("RGBA", (w, h), b"".join(rows))
    blurred = pil.filter(ImageFilter.GaussianBlur(radius=radius))
    out = QImage(blurred.tobytes(), w, h, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(out)


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
        # Logo is always visible (in its native SVG colors); when Stone
        # mode is active a red Gaussian-blurred halo appears BEHIND the
        # logo so only the glow extending past the logo's edges reads as
        # the alchemical cue.
        svg_path = resources_dir() / "logo.svg"
        self._svg = QSvgRenderer(str(svg_path)) if svg_path.exists() else None
        self._stone_active: bool = False
        # Two cached pixmaps:
        #   _wm_pixmap_logo — SVG rendered as-is (native colors), no tint
        #   _wm_pixmap_glow — red silhouette + Gaussian blur, on a larger
        #                     canvas so the halo doesn't clip
        # Rebuilt only when the size changes.
        self._wm_pixmap_logo: QPixmap | None = None
        self._wm_pixmap_glow: QPixmap | None = None
        self._wm_pixmap_side: int = 0

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

    def set_stone_active(self, active: bool) -> None:
        """Show or hide the alchemical-circle watermark. Called from
        MainWindow whenever the Philosopher's Stone toggle changes state."""
        if self._stone_active == bool(active):
            return
        self._stone_active = bool(active)
        # Force a repaint to show/hide the watermark immediately.
        self.update()

    def _build_watermark_pixmaps(self, side: int) -> tuple[QPixmap, QPixmap] | None:
        """Build the (logo, glow) pixmap pair for the watermark.

        - logo: SVG rendered in its native colors at `side × side`, no tint.
        - glow: SVG silhouette tinted Stone-red and Gaussian-blurred, on a
          slightly larger canvas (`side + 2 * _WM_GLOW_RADIUS`) so the
          blurred halo doesn't clip at the pixmap boundary.

        Cached by `paintEvent` and rebuilt only when `side` changes."""
        if self._svg is None:
            return None
        # 1. Front logo: SVG as-is, no tint, no compositing.
        logo = QPixmap(side, side)
        logo.fill(Qt.GlobalColor.transparent)
        sp = QPainter(logo)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._svg.render(sp, QRectF(0, 0, side, side))
        sp.end()

        # 2. Glow silhouette: render SVG into a larger canvas, recolor
        # all visible pixels red via SourceIn (alpha-preserving), then
        # Gaussian-blur via PIL.
        glow_canvas = side + 2 * _WM_GLOW_RADIUS
        sil = QImage(glow_canvas, glow_canvas, QImage.Format.Format_ARGB32_Premultiplied)
        sil.fill(Qt.GlobalColor.transparent)
        sp = QPainter(sil)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Center the SVG inside the larger canvas — leaves _WM_GLOW_RADIUS
        # of transparent margin on every side for the blur to bleed into.
        self._svg.render(sp, QRectF(_WM_GLOW_RADIUS, _WM_GLOW_RADIUS, side, side))
        sp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        sp.fillRect(0, 0, glow_canvas, glow_canvas, _STONE_RED)
        sp.end()
        glow = _gaussian_blur_qimage(sil, _WM_GLOW_RADIUS)
        return logo, glow

    def paintEvent(self, event) -> None:  # noqa: N802
        # QFrame paints its QSS background first.
        super().paintEvent(event)
        if self._svg is None:
            return
        # Center a square watermark sized to fit the smaller dimension with
        # ~20px breathing room. Capped at 360 so it stays elegant on huge
        # windows.
        rect = self.rect()
        side = max(120, min(rect.width() - 40, rect.height() - 40, 360))
        x = (rect.width() - side) // 2
        y = (rect.height() - side) // 2
        # Build/refresh both pixmaps if the size changed.
        if self._wm_pixmap_logo is None or self._wm_pixmap_side != side:
            built = self._build_watermark_pixmaps(side)
            if built is None:
                return
            self._wm_pixmap_logo, self._wm_pixmap_glow = built
            self._wm_pixmap_side = side
        if self._wm_pixmap_logo is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._stone_active and self._wm_pixmap_glow is not None:
            # Glow goes BEHIND the logo. Its canvas is _WM_GLOW_RADIUS
            # bigger on each side, so offset the draw point accordingly
            # to keep the logo's center aligned with the glow's center.
            p.setOpacity(_WM_OPACITY_GLOW_ON)
            p.drawPixmap(x - _WM_GLOW_RADIUS, y - _WM_GLOW_RADIUS,
                          self._wm_pixmap_glow)
        # Logo always renders at the SAME opacity — Stone state never
        # changes the logo's brightness. Toggle is glow-only.
        p.setOpacity(_WM_OPACITY_LOGO)
        p.drawPixmap(x, y, self._wm_pixmap_logo)
        p.end()

    def _on_remove(self, widget: PlaylistItemWidget) -> None:
        if widget.is_running():
            widget.stop_requested.emit(widget)
        self.remove_widget(widget)

    def _sync_empty(self) -> None:
        self._stack.setCurrentIndex(0 if self.list.count() == 0 else 1)
