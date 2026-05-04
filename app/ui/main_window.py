"""Top-level QMainWindow: title, drop zone, playlist, bulk action buttons."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QVBoxLayout, QWidget
)

from ..utils.paths import resources_dir
from ..utils.logger import get_logger

_log = get_logger()
_CINZEL_FAMILY: str | None = None


def _ensure_cinzel_loaded() -> str | None:
    """Register Cinzel-Regular.ttf with Qt's font database on first use.
    Returns the family name to pass to QFont, or None if the file is missing.
    Idempotent — safe to call repeatedly."""
    global _CINZEL_FAMILY
    if _CINZEL_FAMILY is not None:
        return _CINZEL_FAMILY
    ttf = resources_dir() / "fonts" / "Cinzel-Regular.ttf"
    if not ttf.exists():
        return None
    font_id = QFontDatabase.addApplicationFont(str(ttf))
    if font_id < 0:
        _log.warning("Failed to register Cinzel font from %s", ttf)
        return None
    families = QFontDatabase.applicationFontFamilies(font_id)
    _CINZEL_FAMILY = families[0] if families else None
    return _CINZEL_FAMILY

from .drop_zone import DropZone
from .playlist import Playlist
from .playlist_item import PlaylistItemWidget, Status
from . import dialogs
from ..core.conversion_queue import ConversionQueue
from ..utils import settings


def _gem_icon_label(size_px: int = 16) -> QLabel:
    """Render resources/gem.svg into a small QLabel pixmap. Returned label
    starts hidden — caller toggles visibility via setVisible()."""
    lbl = QLabel()
    lbl.setFixedSize(size_px, size_px)
    lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    svg_path = resources_dir() / "gem.svg"
    if svg_path.exists():
        from PySide6.QtGui import QPixmap, QPainter
        from PySide6.QtCore import QRectF
        pm = QPixmap(size_px, size_px)
        pm.fill(Qt.GlobalColor.transparent)
        renderer = QSvgRenderer(str(svg_path))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, size_px, size_px))
        painter.end()
        lbl.setPixmap(pm)
    lbl.setVisible(False)
    return lbl


def _help_icon(tooltip_text: str) -> QLabel:
    """A small "?" pill that shows `tooltip_text` on hover.

    Styled inline so we don't have to extend theme.qss for one-off elements.
    Width/height are forced to 16px so it stays compact next to a checkbox.
    """
    lbl = QLabel("?")
    lbl.setObjectName("HelpIcon")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setFixedSize(16, 16)
    lbl.setCursor(Qt.CursorShape.WhatsThisCursor)
    lbl.setStyleSheet(
        "QLabel#HelpIcon {"
        " background-color: #2a2a3a;"
        " color: #b0b0c0;"
        " border: 1px solid #3a3a4a;"
        " border-radius: 8px;"
        " font-size: 10px;"
        " font-weight: bold;"
        "}"
        "QLabel#HelpIcon:hover {"
        " background-color: #3b82f6;"
        " color: #ffffff;"
        " border-color: #3b82f6;"
        "}"
    )
    lbl.setToolTip(tooltip_text)
    return lbl


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Transmute")
        self.resize(1180, 720)

        self._queue = ConversionQueue(max_workers=3, parent=self)
        self._job_to_widget: dict[int, PlaylistItemWidget] = {}

        self._build_ui()
        self._wire_queue()

    # --- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(10)

        # Top bar: title + global toggles (Philosopher's Stone, Verify Round-Trip).
        topbar = QHBoxLayout()
        title = QLabel("Transmute")
        title.setObjectName("AppTitle")
        # Apply Cinzel (engraved-cap serif) to the title only — the rest of
        # the UI keeps its sans-serif. Slight letter-spacing for the classical
        # carved-capital feel.
        cinzel_family = _ensure_cinzel_loaded()
        if cinzel_family:
            f = QFont(cinzel_family, 22)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            title.setFont(f)
            title.setStyleSheet(
                "color: #e8e8f0; padding: 6px 4px 6px 4px;"
            )
        topbar.addWidget(title)
        topbar.addStretch(1)

        # Wrapped in <p style="width: 280px"> so Qt treats them as rich text and
        # soft-wraps to a sensible width instead of showing one long line.
        stone_tip = (
            '<p style="width:280px; margin:0;">'
            "Enables cross-format byte-preserving conversions (text→audio, image→text, etc.). "
            "Files keep their original bytes intact while wearing another format's container. "
            "Round-trip safe with lossless source formats only — lossy formats "
            "(jpg, mp3, mp4, etc.) are excluded."
            "</p>"
        )
        verify_tip = (
            '<p style="width:280px; margin:0;">'
            "After conversion, immediately reverses it and compares hashes to confirm "
            "bit-perfect preservation. Doubles conversion time. Output saves only if "
            "verification passes."
            "</p>"
        )

        # Gem icon sits to the LEFT of the Stone label, visible only when active.
        self.gem_icon = _gem_icon_label(16)
        topbar.addWidget(self.gem_icon)

        self.chk_stone = QCheckBox("Philosopher's Stone")
        self.chk_stone.setObjectName("StoneToggle")
        self.chk_stone.setChecked(bool(settings.get("masquerade_enabled")))
        self.chk_stone.setToolTip(stone_tip)
        self.chk_stone.toggled.connect(self._on_stone_toggled)
        topbar.addWidget(self.chk_stone)
        topbar.addWidget(_help_icon(stone_tip))

        self.chk_verify = QCheckBox("Verify Round-Trip")
        self.chk_verify.setChecked(bool(settings.get("verify_round_trip")))
        self.chk_verify.setToolTip(verify_tip)
        self.chk_verify.toggled.connect(self._on_verify_toggled)
        topbar.addWidget(self.chk_verify)
        topbar.addWidget(_help_icon(verify_tip))

        self._update_verify_enabled()
        self._refresh_stone_visuals()
        outer.addLayout(topbar)

        self.drop_zone = DropZone()
        self.drop_zone.files_added.connect(self._on_files_added)
        outer.addWidget(self.drop_zone)

        self.playlist = Playlist()
        self.playlist.items_changed.connect(self._on_playlist_changed)
        outer.addWidget(self.playlist, 1)

        bulk = QHBoxLayout()
        self.btn_convert_all = QPushButton("Convert All")
        self.btn_convert_all.setObjectName("Primary")
        self.btn_convert_sel = QPushButton("Convert Selected")
        self.btn_convert_sel.setObjectName("Secondary")
        self.btn_remove_sel = QPushButton("Remove Selected")
        self.btn_clear = QPushButton("Clear Playlist")
        bulk.addWidget(self.btn_convert_all)
        bulk.addWidget(self.btn_convert_sel)
        bulk.addStretch(1)
        bulk.addWidget(self.btn_remove_sel)
        bulk.addWidget(self.btn_clear)
        outer.addLayout(bulk)

        self.btn_convert_all.clicked.connect(self._on_convert_all)
        self.btn_convert_sel.clicked.connect(self._on_convert_selected)
        self.btn_remove_sel.clicked.connect(self._on_remove_selected)
        self.btn_clear.clicked.connect(self._on_clear)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready.")

    # --- Wiring ------------------------------------------------------------
    def _wire_queue(self) -> None:
        self._queue.job_started.connect(self._on_job_started)
        self._queue.job_progress.connect(self._on_job_progress)
        self._queue.job_elapsed.connect(self._on_job_elapsed)
        self._queue.job_finished.connect(self._on_job_finished)
        self._queue.job_failed.connect(self._on_job_failed)
        self._queue.job_cancelled.connect(self._on_job_cancelled)
        self._queue.job_warning.connect(self._on_job_warning)

    def _on_playlist_changed(self) -> None:
        """Fade the drop-zone watermark up (empty) or down (non-empty)."""
        empty = self.playlist.list.count() == 0
        self.drop_zone.fade_watermark(0.13 if empty else 0.0)

    def _on_files_added(self, paths: list) -> None:
        # Filter to known extensions; unknown ones are still added so the user sees the error inline.
        self.playlist.add_paths([Path(p) for p in paths],
                                masquerade=self.chk_stone.isChecked())
        # Wire per-item signals exactly once. Marker on the widget itself
        # avoids the disconnect/reconnect dance (which prints harmless but
        # noisy RuntimeWarnings for newly-created widgets that have nothing
        # to disconnect from).
        for w in self.playlist.items():
            if getattr(w, "_signals_wired", False):
                continue
            w.convert_requested.connect(self._start_convert)
            w.stop_requested.connect(self._stop_convert)
            w._signals_wired = True
        self.statusBar().showMessage(f"Added {len(paths)} item(s).")

    # --- Per-item ----------------------------------------------------------
    def _start_convert(self, widget: PlaylistItemWidget) -> None:
        if widget.is_running():
            return
        target_ext = widget.target_ext()
        if not target_ext or not target_ext.startswith("."):
            dialogs.error(self, "Cannot convert", f"No valid target format for {widget.path.name}.")
            return
        widget.reset_for_rerun()
        widget.set_status(Status.RUNNING)
        out = widget.output_path()
        masq = bool(self.chk_stone.isChecked())
        verify = bool(self.chk_verify.isChecked() and self.chk_verify.isEnabled())
        job_id = self._queue.submit(
            src=widget.path,
            dst=out,
            src_ext=widget.src_ext,
            dst_ext=target_ext,
            save_over_original=widget.save_over_original(),
            masquerade=masq,
            verify_round_trip=verify,
        )
        self._job_to_widget[job_id] = widget

    def _on_stone_toggled(self, checked: bool) -> None:
        settings.set("masquerade_enabled", bool(checked))
        self._update_verify_enabled()
        self._refresh_stone_visuals()
        # Refresh every playlist row's target dropdown — Stone adds/removes
        # cross-category byte-passthrough hosts.
        for w in self.playlist.items():
            w.refresh_targets(masquerade=checked)
        self.statusBar().showMessage(
            "Philosopher's Stone " + ("ON — lossless byte-passthrough hosts available."
                                      if checked else "OFF.")
        )

    def _refresh_stone_visuals(self) -> None:
        """Sync the gem icon visibility + the active QSS state of the toggle."""
        active = self.chk_stone.isChecked()
        self.gem_icon.setVisible(active)
        self.chk_stone.setProperty("stoneActive", "true" if active else "false")
        # Re-polish so the dynamic property selector kicks in.
        self.chk_stone.style().unpolish(self.chk_stone)
        self.chk_stone.style().polish(self.chk_stone)

    def _on_verify_toggled(self, checked: bool) -> None:
        settings.set("verify_round_trip", bool(checked))

    def _update_verify_enabled(self) -> None:
        self.chk_verify.setEnabled(self.chk_stone.isChecked())
        if not self.chk_stone.isChecked():
            self.chk_verify.setToolTip(
                "Verify Round-Trip is only available with Philosopher's Stone on, "
                "since lossy conversions can't round-trip exactly."
            )
        else:
            self.chk_verify.setToolTip(
                "After converting, immediately convert back and hash-compare against the source."
            )

    def _stop_convert(self, widget: PlaylistItemWidget) -> None:
        # Find the job id for this widget
        for jid, w in list(self._job_to_widget.items()):
            if w is widget:
                self._queue.cancel(jid)
                break

    # --- Bulk --------------------------------------------------------------
    def _on_convert_all(self) -> None:
        items = self.playlist.items()
        if not items:
            return
        if not dialogs.confirm(self, "Convert all?", f"Convert all {len(items)} item(s) in the playlist?"):
            return
        for w in items:
            if not w.is_running() and w.status() != Status.DONE:
                self._start_convert(w)

    def _on_convert_selected(self) -> None:
        items = self.playlist.checked_items()
        if not items:
            dialogs.info(self, "Nothing selected", "Tick the checkboxes of items you want to convert.")
            return
        if not dialogs.confirm(self, "Convert selected?", f"Convert {len(items)} selected item(s)?"):
            return
        for w in items:
            if not w.is_running():
                self._start_convert(w)

    def _on_remove_selected(self) -> None:
        items = self.playlist.checked_items()
        if not items:
            dialogs.info(self, "Nothing selected", "Tick the checkboxes of items you want to remove.")
            return
        if not dialogs.confirm(self, "Remove selected?", f"Remove {len(items)} selected item(s) from the playlist?"):
            return
        for w in items:
            if w.is_running():
                self._stop_convert(w)
            self.playlist.remove_widget(w)

    def _on_clear(self) -> None:
        if self.playlist.list.count() == 0:
            return
        if not dialogs.confirm(self, "Clear playlist?", "Remove all items from the playlist?"):
            return
        # Cancel any running jobs
        self._queue.cancel_all()
        self.playlist.clear()

    # --- Queue -> widget ---------------------------------------------------
    def _on_job_started(self, job_id: int) -> None:
        w = self._job_to_widget.get(job_id)
        if w:
            w.set_status(Status.RUNNING)
            w.set_progress(0.01)

    def _on_job_progress(self, job_id: int, p: float) -> None:
        w = self._job_to_widget.get(job_id)
        if w:
            w.set_progress(p)

    def _on_job_elapsed(self, job_id: int, secs: float) -> None:
        w = self._job_to_widget.get(job_id)
        if w:
            w.set_elapsed(secs)

    def _on_job_finished(self, job_id: int, path: str) -> None:
        w = self._job_to_widget.pop(job_id, None)
        if w:
            w.set_progress(1.0)
            w.set_status(Status.DONE)
        self.statusBar().showMessage(f"Saved: {path}")

    def _on_job_failed(self, job_id: int, msg: str) -> None:
        w = self._job_to_widget.pop(job_id, None)
        if w:
            w.set_status(Status.ERROR, msg)
        self.statusBar().showMessage(f"Error: {msg}")

    def _on_job_warning(self, job_id: int, msg: str) -> None:
        w = self._job_to_widget.get(job_id)
        if w:
            existing = w.title.toolTip()
            w.title.setToolTip(existing + ("\n" if existing else "") + "Warning: " + msg)
        self.statusBar().showMessage("Warning: " + msg, 8000)

    def _on_job_cancelled(self, job_id: int) -> None:
        w = self._job_to_widget.pop(job_id, None)
        if w:
            w.set_status(Status.QUEUED)
            w.set_progress(0.0)
        self.statusBar().showMessage("Cancelled.")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._queue.cancel_all()
        super().closeEvent(event)
