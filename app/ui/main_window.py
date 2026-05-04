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
    from ..utils.paths import find_font
    ttf = find_font("Cinzel-Regular.ttf")
    if ttf is None:
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
from .vignette import VignetteOverlay
from .border_frame import BorderFrame
from . import dialogs
from ..core.conversion_queue import ConversionQueue
from ..utils import settings


def _logo_label(size_px: int = 28, boost: bool = False) -> QLabel | None:
    """Render resources/logo.svg into a small QLabel pixmap. Returns None
    if the SVG is missing.

    `boost=True` paints the SVG twice — once at full opacity and again at
    half opacity over the top — which thickens the perceived stroke weight
    and makes the icon read brighter at small sizes. Used for the title-bar
    icon so it has visual presence next to the Cinzel header."""
    svg_path = resources_dir() / "logo.svg"
    if not svg_path.exists():
        return None
    from PySide6.QtGui import QPixmap, QPainter
    from PySide6.QtCore import QRectF
    pm = QPixmap(size_px, size_px)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(str(svg_path))
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size_px, size_px))
    if boost:
        # Composite a second pass at reduced alpha to thicken/brighten the
        # lines without altering the source SVG.
        painter.setOpacity(0.55)
        renderer.render(painter, QRectF(0, 0, size_px, size_px))
    painter.end()
    lbl = QLabel()
    lbl.setFixedSize(size_px, size_px)
    lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    lbl.setPixmap(pm)
    return lbl


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
        # Margins clear the inscribed BorderFrame on the sides + top. The
        # bottom margin is small because the QStatusBar lives below the
        # central widget; the BorderFrame is parented to the QMainWindow
        # itself so it wraps the status bar too — bottom border sits BELOW
        # the "Ready." text, giving the bulk buttons a natural buffer.
        outer.setContentsMargins(28, 28, 28, 6)
        outer.setSpacing(10)

        # Top bar: title + global toggles (Philosopher's Stone, Verify Round-Trip).
        topbar = QHBoxLayout()
        # Title cluster (icon + label) gets its own tight sub-layout so the
        # icon sits close to the text without affecting the topbar's wider
        # spacing between the title cluster and the toggles on the right.
        title_box = QHBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(8)
        title_icon = _logo_label(38, boost=True)
        if title_icon is not None:
            title_box.addWidget(title_icon)
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
        title_box.addWidget(title)
        topbar.addLayout(title_box)
        topbar.addStretch(1)

        # Wrapped in <p style="width: 280px"> so Qt treats them as rich text and
        # soft-wraps to a sensible width instead of showing one long line.
        stone_tip = (
            '<p style="width:280px; margin:0;">'
            "Enables cross-format byte-preserving conversions (text→audio, image→text, etc.). "
            "Files keep their original bytes intact while wearing another format's container. "
            "Cross-category outputs get aesthetic treatment — fractal patterns for images, "
            "generated music for audio. Round-trip integrity preserved. "
            "Lossless source formats only — lossy formats (jpg, mp3, mp4, etc.) are excluded."
            "</p>"
        )
        verify_tip = (
            '<p style="width:280px; margin:0;">'
            "After conversion, immediately reverses it and compares hashes to confirm "
            "bit-perfect preservation. Doubles conversion time. Output saves only if "
            "verification passes."
            "</p>"
        )

        self.chk_stone = QCheckBox("Philosopher's Stone")
        self.chk_stone.setObjectName("StoneToggle")
        self.chk_stone.setChecked(bool(settings.get("masquerade_enabled")))
        self.chk_stone.setToolTip(stone_tip)
        self.chk_stone.toggled.connect(self._on_stone_toggled)
        topbar.addWidget(self.chk_stone)
        topbar.addWidget(_help_icon(stone_tip))

        self.chk_verify = QCheckBox("Verify Round-Trip")
        self.chk_verify.setObjectName("VerifyToggle")
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
        # Make the status bar tall enough that the "Ready." text sits well
        # above the inscribed BorderFrame's bottom edge. Bottom padding is
        # set in theme.qss; horizontal padding goes via leading-space prefix
        # in _status() because QStatusBar paints showMessage() text in its
        # own paintEvent and ignores QSS padding-left for the message label.
        self.statusBar().setMinimumHeight(64)
        self._status("Ready.")

        # Inscribed manuscript border + vignette parented to the QMainWindow
        # itself so they wrap the entire window including the status bar.
        # The bottom border line ends up BELOW the "Ready." text, which gives
        # the bulk buttons a natural buffer above the bottom glyph row.
        # raise_() keeps them above siblings so they paint over the status
        # bar's QSS background.
        self._border = BorderFrame(self)
        self._vignette = VignetteOverlay(self)
        self._border.raise_()
        self._vignette.raise_()

    # --- Wiring ------------------------------------------------------------
    def _wire_queue(self) -> None:
        self._queue.job_started.connect(self._on_job_started)
        self._queue.job_progress.connect(self._on_job_progress)
        self._queue.job_elapsed.connect(self._on_job_elapsed)
        self._queue.job_finished.connect(self._on_job_finished)
        self._queue.job_failed.connect(self._on_job_failed)
        self._queue.job_cancelled.connect(self._on_job_cancelled)
        self._queue.job_warning.connect(self._on_job_warning)
        self._queue.job_bytes_progress.connect(self._on_job_bytes_progress)

    def _on_job_bytes_progress(self, job_id: int, processed: int, total: int) -> None:
        w = self._job_to_widget.get(job_id)
        if w is not None:
            w.update_bytes_progress(processed, total)

    def _status(self, msg: str, timeout: int = 0) -> None:
        """Show a status-bar message with leading-space indent so the text
        clears the inscribed left border. Qt's QStatusBar paints showMessage
        text in its own paintEvent and ignores QSS padding-left, so prefix
        is the reliable way to push the text right."""
        # 4 spaces ≈ 20 px at the default UI font — matches what padding-left
        # would have done if QStatusBar honored it.
        self.statusBar().showMessage("    " + msg, timeout)

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
        self._status(f"Added {len(paths)} item(s).")

    # --- Per-item ----------------------------------------------------------
    def _start_convert(self, widget: PlaylistItemWidget) -> None:
        if widget.is_running():
            return
        target_ext = widget.target_ext()
        if not target_ext or not target_ext.startswith("."):
            dialogs.error(self, "Cannot convert", f"No valid target format for {widget.path.name}.")
            return
        masq = bool(self.chk_stone.isChecked())
        verify = bool(self.chk_verify.isChecked() and self.chk_verify.isEnabled())
        # Verify Round-Trip warning — only fires when estimated wall-clock
        # exceeds the LONG_CONVERSION_SECONDS threshold (10 min). Per-item
        # _verify_warned flag prevents re-prompting on retries.
        if verify and not getattr(widget, "_verify_warned", False):
            from ..core.estimator import estimate_verify_seconds, LONG_CONVERSION_SECONDS
            try:
                src_size = widget.path.stat().st_size
            except OSError:
                src_size = 0
            secs = estimate_verify_seconds(widget.src_ext, target_ext, src_size, masq)
            if secs > LONG_CONVERSION_SECONDS:
                mins = int(round(secs / 60))
                if not dialogs.confirm(
                    self,
                    "Verify Round-Trip — long conversion",
                    f"Verifying this conversion will take ~{mins} extra minutes "
                    "(round-trip doubles conversion time). Continue with verification?",
                ):
                    return
            widget._verify_warned = True
        widget.reset_for_rerun()
        widget.set_status(Status.RUNNING)
        out = widget.output_path()
        job_id = self._queue.submit(
            src=widget.path,
            dst=out,
            src_ext=widget.src_ext,
            dst_ext=target_ext,
            save_over_original=widget.save_over_original(),
            masquerade=masq,
            verify_round_trip=verify,
            compiler=widget.compiler_enabled(),
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
        self._status(
            "Philosopher's Stone " + ("ON — lossless byte-passthrough hosts available."
                                      if checked else "OFF.")
        )

    def _refresh_stone_visuals(self) -> None:
        """Sync the active QSS state of the Stone toggle. The hex indicator
        carries the visual cue now — no separate icon to manage."""
        active = self.chk_stone.isChecked()
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
    def _bulk_verify_preflight(self, items: list[PlaylistItemWidget]) -> bool:
        """Aggregate the Verify Round-Trip estimate across all queued items
        and prompt once. Returns True if conversion should proceed (verify
        either off, total under threshold, or user confirmed). Marks each
        widget so the per-item warning won't fire again for this batch."""
        verify = bool(self.chk_verify.isChecked() and self.chk_verify.isEnabled())
        if not verify:
            return True
        from ..core.estimator import estimate_verify_seconds, LONG_CONVERSION_SECONDS
        masq = bool(self.chk_stone.isChecked())
        total = 0.0
        eligible = []
        for w in items:
            if w.is_running() or w.status() == Status.DONE:
                continue
            ext = w.target_ext()
            if not ext:
                continue
            try:
                size = w.path.stat().st_size
            except OSError:
                size = 0
            total += estimate_verify_seconds(w.src_ext, ext, size, masq)
            eligible.append(w)
        if total > LONG_CONVERSION_SECONDS and eligible:
            mins = int(round(total / 60))
            if not dialogs.confirm(
                self, "Verify Round-Trip — long batch",
                f"Verifying {len(eligible)} item(s) will take ~{mins} extra minutes total "
                "(round-trip doubles each conversion). Continue with verification?",
            ):
                return False
        # Stamp _verify_warned on every eligible widget so per-row prompts
        # don't fire again inside _start_convert.
        for w in eligible:
            w._verify_warned = True
        return True

    def _on_convert_all(self) -> None:
        items = self.playlist.items()
        if not items:
            return
        if not dialogs.confirm(self, "Convert all?", f"Convert all {len(items)} item(s) in the playlist?"):
            return
        if not self._bulk_verify_preflight(items):
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
        if not self._bulk_verify_preflight(items):
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
        self._status(f"Saved: {path}")

    def _on_job_failed(self, job_id: int, msg: str) -> None:
        w = self._job_to_widget.pop(job_id, None)
        if w:
            w.set_status(Status.ERROR, msg)
        self._status(f"Error: {msg}")

    def _on_job_warning(self, job_id: int, msg: str) -> None:
        w = self._job_to_widget.get(job_id)
        if w:
            existing = w.title.toolTip()
            w.title.setToolTip(existing + ("\n" if existing else "") + "Warning: " + msg)
        self._status("Warning: " + msg, 8000)

    def _on_job_cancelled(self, job_id: int) -> None:
        w = self._job_to_widget.pop(job_id, None)
        if w:
            w.set_status(Status.QUEUED)
            w.set_progress(0.0)
        self._status("Cancelled.")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._queue.cancel_all()
        super().closeEvent(event)
