"""Top-level QMainWindow: title, drop zone, playlist, bulk action buttons."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar,
    QVBoxLayout, QWidget,
)

from ..utils.paths import resources_dir
from ..utils.logger import get_logger
from ..__version__ import __version__

_log = get_logger()
_CINZEL_FAMILY: str | None = None


def _ensure_cinzel_loaded() -> str | None:
    """Register Cinzel-Regular.ttf with Qt and return the family name, or
    None if the file is missing. Idempotent."""
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
    """Render resources/logo.svg into a QLabel pixmap.

    `boost=True` composites a second pass at 55% alpha on top — thickens
    the stroke for small sizes."""
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
        self.setWindowTitle("Vitriol")
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
        # Margins clear the BorderFrame; small bottom margin because the
        # status bar sits below the central widget.
        outer.setContentsMargins(28, 28, 28, 6)
        outer.setSpacing(10)

        # Top bar: title + global toggles.
        topbar = QHBoxLayout()
        title_box = QHBoxLayout()
        title_box.setContentsMargins(0, 0, 0, 0)
        title_box.setSpacing(8)
        title_icon = _logo_label(38, boost=True)
        if title_icon is not None:
            title_box.addWidget(title_icon)
        title = QLabel("Vitriol")
        title.setObjectName("AppTitle")
        title.setToolTip(f"V.I.T.R.I.O.L-Visita Interiora Terrae Rectificando Invenies Occultum Lapidemn")
        # Cinzel for the title only.
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
        # Sync the playlist's red-watermark cue with the persisted toggle.
        self.playlist.set_stone_active(self.chk_stone.isChecked())
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

        # Discoverable manual-update entry-point. The wordmark right-click
        # context menu still works (kept for power-user habit) but most
        # users never discover hidden right-click gestures, so a visible
        # link above the status bar gives the same affordance with zero
        # discovery cost. Lives in the central widget rather than the
        # QStatusBar so it stays visible when conversion-progress
        # messages are showing in the status bar.
        update_check_row = QHBoxLayout()
        update_check_row.setContentsMargins(0, 0, 0, 0)
        self._update_check_link = QPushButton("Check for Update")
        self._update_check_link.setObjectName("UpdateCheckLink")
        self._update_check_link.setFlat(True)
        self._update_check_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_check_link.setStyleSheet(
            "QPushButton#UpdateCheckLink {"
            " color: #707080;"
            " background: transparent;"
            " border: none;"
            " padding: 2px 6px;"
            " font-size: 10px;"
            " font-weight: 400;"
            " text-align: left;"
            "}"
            "QPushButton#UpdateCheckLink:hover {"
            " color: #a78bfa;"
            " text-decoration: underline;"
            "}"
        )
        self._update_check_link.setToolTip(
            "Check GitHub for a newer version of Vitriol. "
            "(Same as right-clicking the title bar — this link is the "
            "discoverable version of that gesture.)"
        )
        self._update_check_link.clicked.connect(self._check_updates_manual)
        update_check_row.addWidget(self._update_check_link)
        update_check_row.addStretch(1)
        outer.addLayout(update_check_row)

        self.setStatusBar(QStatusBar())
        # Make the status bar tall enough that the "Ready." text sits well
        # above the inscribed BorderFrame's bottom edge. Bottom padding is
        # set in theme.qss; horizontal padding goes via leading-space prefix
        # in _status() because QStatusBar paints showMessage() text in its
        # own paintEvent and ignores QSS padding-left for the message label.
        self.statusBar().setMinimumHeight(64)
        self._status("Ready.")

        # Persistent "Update available" pill on the right side of the
        # status bar. Hidden by default; shown by show_update_banner()
        # when the launch-time check finds a newer release. Clicking it
        # opens the UpdateAvailableDialog. addPermanentWidget() places
        # it at the right edge so transient status messages on the left
        # never overlap it.
        self._update_btn = QPushButton("")
        self._update_btn.setObjectName("UpdateBanner")
        self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_btn.setFlat(True)
        self._update_btn.setStyleSheet(
            "QPushButton#UpdateBanner {"
            " color: #fde68a;"
            " background-color: rgba(146, 64, 14, 0.6);"
            " border: 1px solid rgba(251, 191, 36, 0.5);"
            " border-radius: 10px;"
            " padding: 3px 12px;"
            " margin-right: 8px;"
            " font-weight: bold;"
            "}"
            "QPushButton#UpdateBanner:hover {"
            " background-color: rgba(180, 83, 9, 0.8);"
            "}"
        )
        self._update_btn.clicked.connect(self._open_update_dialog)
        self._update_btn.hide()
        self._pending_update_info: dict | None = None
        self.statusBar().addPermanentWidget(self._update_btn)

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
        """Show a status-bar message. Prefixes 4 spaces because QStatusBar
        ignores QSS padding-left."""
        self.statusBar().showMessage("    " + msg, timeout)

    # --- Auto-update integration ------------------------------------------

    def show_update_banner(self, info: dict) -> None:
        """Show the update-available pill in the status bar."""
        if not info:
            return
        self._pending_update_info = info
        version = info.get("version", "")
        self._update_btn.setText(f"  ↑ Update {version} available  ")
        self._update_btn.setToolTip(
            f"Vitriol {version} is available. Click to view release notes "
            f"and install."
        )
        self._update_btn.show()

    def _open_update_dialog(self) -> None:
        """Open the UpdateAvailableDialog with the last fetched info, or
        fall back to a manual check if nothing was stashed."""
        info = self._pending_update_info
        if info is None:
            self._check_updates_manual()
            return
        self._show_update_dialog(info)

    def _check_updates_manual(self) -> None:
        """Run the update API call on a worker thread and show one of:
        UpdateAvailableDialog, "up to date" toast, or error dialog."""
        from PySide6.QtCore import QThread
        from ..core import updater

        self._status("Checking for updates…")

        class _Worker(QThread):
            done = Signal(object, object)  # (info_or_none, error_or_none)

            def run(self_inner) -> None:  # type: ignore[no-self-argument]
                try:
                    result = updater.check_for_update(silent=False)
                    self_inner.done.emit(result, None)
                except Exception as e:
                    self_inner.done.emit(None, e)

        worker = _Worker(self)
        # Hold a reference on `self` so the QThread isn't GC'd before it
        # finishes. _Worker.deleteLater on done so the slot doesn't leak.
        self._update_check_worker = worker

        def _on_done(result, error):
            self._status("Ready.")
            if error is not None:
                dialogs.error(self, "Update check failed", str(error))
                worker.deleteLater()
                return
            if result is None:
                dialogs.info(
                    self, "Up to date",
                    f"You're running the latest version of Vitriol ({__version__})."
                )
                worker.deleteLater()
                return
            self.show_update_banner(result)
            self._show_update_dialog(result)
            worker.deleteLater()

        worker.done.connect(_on_done)
        worker.start()

    def _show_update_dialog(self, info: dict) -> None:
        """Open the UpdateAvailableDialog and act on the user's choice."""
        from .dialogs import (
            UpdateAvailableDialog, UPDATE_INSTALL, UPDATE_OPEN_PAGE,
            UPDATE_SKIP, UPDATE_LATER,
        )
        from ..core import updater

        portable = updater.is_portable_build()
        dlg = UpdateAvailableDialog(self, info, portable=portable)
        choice = dlg.run()

        if choice == UPDATE_SKIP:
            settings.set("skip_version", info.get("version", ""))
            self._update_btn.hide()
            self._pending_update_info = None
            return

        if choice == UPDATE_OPEN_PAGE:
            import webbrowser
            webbrowser.open(updater.GITHUB_RELEASES_PAGE)
            return

        if choice == UPDATE_INSTALL:
            self._run_install_flow(info)
            return

        # UPDATE_LATER / dismissed — leave banner up; 24h throttle governs.

    def _run_install_flow(self, info: dict) -> None:
        """Download the installer with a progress dialog, then launch it.

        Does not return on success: `launch_installer_and_exit` calls
        sys.exit(0) so Inno can replace Vitriol.exe.
        """
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QProgressDialog
        from ..core import updater

        progress = QProgressDialog(
            f"Downloading Vitriol {info.get('version', '')}…",
            "Cancel", 0, 100, self,
        )
        progress.setWindowTitle("Updating Vitriol")
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)

        class _DLWorker(QThread):
            tick = Signal(int)
            done = Signal(object, object)  # (path_or_none, error_or_none)

            def run(self_inner) -> None:  # type: ignore[no-self-argument]
                def cb(fraction: float) -> None:
                    self_inner.tick.emit(int(fraction * 100))
                try:
                    path = updater.download_installer(info, on_progress=cb)
                    self_inner.done.emit(path, None)
                except Exception as e:
                    self_inner.done.emit(None, e)

        worker = _DLWorker(self)
        self._update_dl_worker = worker

        worker.tick.connect(progress.setValue)

        def _on_done(path, error):
            progress.close()
            if error is not None:
                dialogs.error(self, "Update failed", str(error))
                worker.deleteLater()
                return
            # Exits the app so Inno can replace Vitriol.exe; .iss has
            # RestartApplications=yes for the post-install relaunch.
            try:
                updater.launch_installer_and_exit(path)
            except Exception as e:
                dialogs.error(self, "Update launch failed", str(e))
                worker.deleteLater()

        worker.done.connect(_on_done)
        worker.start()

    def _on_playlist_changed(self) -> None:
        """Fade the drop-zone watermark up (empty) or down (non-empty)."""
        empty = self.playlist.list.count() == 0
        self.drop_zone.fade_watermark(0.13 if empty else 0.0)

    def _on_files_added(self, paths: list) -> None:
        # Unknown extensions still added so the user sees the error inline.
        self.playlist.add_paths([Path(p) for p in paths],
                                masquerade=self.chk_stone.isChecked())
        # Wire per-item signals once. `_signals_wired` marker prevents
        # re-connect warnings.
        for w in self.playlist.items():
            if getattr(w, "_signals_wired", False):
                continue
            w.convert_requested.connect(self._start_convert)
            w.stop_requested.connect(self._stop_convert)
            w._signals_wired = True
        self._status(f"Added {len(paths)} item(s).")

    # --- Per-item ----------------------------------------------------------
    def _start_convert(self, widget: PlaylistItemWidget,
                        skip_preflight: bool = False) -> None:
        """Submit a row's conversion to the queue.

        `skip_preflight=True` is passed by the bulk handlers, which run
        their own batch dialog. Direct row-Convert clicks leave it False
        so the animation prompt re-asks each click.
        """
        if widget.is_running():
            return
        target_ext = widget.target_ext()
        if not target_ext or not target_ext.startswith("."):
            dialogs.error(self, "Cannot convert", f"No valid target format for {widget.path.name}.")
            return
        masq = bool(self.chk_stone.isChecked())
        verify = bool(self.chk_verify.isChecked() and self.chk_verify.isEnabled())
        # Verify Round-Trip warning fires when the estimate exceeds
        # LONG_CONVERSION_SECONDS (10 min). _verify_warned suppresses
        # repeats on retry.
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
        # Single-row animation pre-flight. Skipped from bulk handlers.
        # Reset `_animation_choice_made` so each click re-asks.
        if not skip_preflight:
            widget._animation_choice_made = False
            if self._row_needs_animation_prompt(widget):
                msg = self._animation_prompt_message(
                    widget.path.name, widget.src_ext.lower(), target_ext.lower()
                )
                yes, _ = dialogs.confirm_with_apply_to_all(
                    self, "Preserve 3D animations?", msg,
                    apply_label="Apply to all 3D files in this batch"
                )
                widget.set_preserve_animations(yes)
                widget._animation_choice_made = True
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
            password=widget.stone_password() if masq else b"",
            preserve_animations=widget.preserve_animations(),
        )
        self._job_to_widget[job_id] = widget

    def _on_stone_toggled(self, checked: bool) -> None:
        settings.set("masquerade_enabled", bool(checked))
        self._update_verify_enabled()
        self._refresh_stone_visuals()
        # Stone changes which target extensions appear in each row.
        for w in self.playlist.items():
            w.refresh_targets(masquerade=checked)
        self.playlist.set_stone_active(checked)
        self._status(
            "Philosopher's Stone " + ("ON — lossless byte-passthrough hosts available."
                                      if checked else "OFF.")
        )

    def _refresh_stone_visuals(self) -> None:
        """Sync the Stone toggle's active QSS state."""
        active = self.chk_stone.isChecked()
        self.chk_stone.setProperty("stoneActive", "true" if active else "false")
        if active:
            self.chk_stone.setStyleSheet(
                "QCheckBox#StoneToggle {"
                " color: #c0392b;"
                " font-weight: bold;"
                "}"
            )
        else:
            self.chk_stone.setStyleSheet("")
        # Re-polish so dynamic property selectors take effect.
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
        """Sum the verify estimate for the batch and prompt once if it
        exceeds the long-run threshold. Returns True to proceed."""
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
        # Mark eligible widgets so per-row prompts don't re-fire.
        for w in eligible:
            w._verify_warned = True
        return True

    def _row_needs_animation_prompt(self, widget: PlaylistItemWidget) -> bool:
        """True if this row is a 3D conversion with animation tracks and
        a target format that can carry them, and no choice has been made
        yet."""
        if getattr(widget, "_animation_choice_made", False):
            return False
        from ..format_handlers.model_handler import (
            has_animations, ANIMATION_CAPABLE_TARGETS, SUPPORTED,
        )
        src_ext = widget.src_ext.lower()
        target_ext = (widget.target_ext() or "").lower()
        if src_ext not in SUPPORTED:
            return False
        if target_ext not in ANIMATION_CAPABLE_TARGETS:
            return False
        try:
            return has_animations(widget.path)
        except Exception:
            return False

    def _bulk_animation_preflight(self, items: list[PlaylistItemWidget]) -> bool:
        """Show one batch-level animation-preservation dialog (apply-to-all)
        if any row is eligible, and stamp the answer onto each eligible
        widget. Always returns True (Yes/No both valid)."""
        eligible = [w for w in items
                     if not w.is_running() and w.status() != Status.DONE
                     and self._row_needs_animation_prompt(w)]
        if not eligible:
            return True
        # Lossy bind-pose case: non-FBX source → FBX target.
        any_lossy_fbx_target = any(
            w.target_ext().lower() == ".fbx" and w.src_ext.lower() != ".fbx"
            for w in eligible
        )

        head = (f"{len(eligible)} item(s) in this batch contain animation/rig "
                f"data and have target formats that can carry it.\n\n")

        if any_lossy_fbx_target:
            warning = (
                "Note — at least one row converts to FBX from a non-FBX source.\n"
                "That specific direction (GLB → FBX, DAE → FBX) has a known\n"
                "limitation: animation curves export but the bind-pose link\n"
                "between mesh and skeleton is lost, so the resulting FBX plays\n"
                "the animation with the mesh stuck in T-pose while the skeleton\n"
                "moves separately. Other directions in this batch (FBX → GLB,\n"
                "FBX → DAE, GLB → DAE) preserve animations correctly.\n\n"
                "Workaround for the affected rows: convert to .dae instead and\n"
                "use Blender (or Maya / 3ds Max) to finalize as FBX from there.\n\n"
            )
        else:
            warning = ""

        msg = head + warning + (
            "Try to preserve animations during conversion?\n\n"
            "Yes — animations export. Faithful for FBX → GLB / FBX → DAE / "
            "GLB → DAE; partial for the GLB→FBX / DAE→FBX cases noted above.\n\n"
            "No — strip animations cleanly. Output is deterministically static "
            "geometry, predictable across all formats."
        )
        yes, _ = dialogs.confirm_with_apply_to_all(
            self, "Preserve 3D animations?", msg,
            apply_label=f"Apply to all {len(eligible)} animated items"
        )
        for w in eligible:
            w.set_preserve_animations(yes)
            w._animation_choice_made = True
        return True

    def _animation_prompt_message(self, filename: str,
                                    src_ext: str, target_ext: str) -> str:
        """Build the animation-preservation prompt text. Different copy
        for the lossy non-FBX → FBX case (bind-pose loss) vs. clean paths."""
        head = (f"{filename} contains animation/rig data, and "
                f"{target_ext} can carry it.\n\n")

        if target_ext == ".fbx" and src_ext != ".fbx":
            return head + (
                "⚠ Known limitation: " + src_ext + " → .fbx loses bind-pose data\n"
                "during export (Assimp's FBX exporter doesn't emit the BindPose\n"
                "chunks for skinned meshes from glTF or Collada input). The\n"
                "resulting .fbx plays the animation, but the mesh will stay in\n"
                "T-pose while the skeleton animates separately.\n\n"
                "Workaround: convert to .dae here, then open the .dae in Blender\n"
                "(or Maya / 3ds Max) and export FBX from there. Vitriol's .dae\n"
                "export preserves bind pose correctly; Blender's FBX exporter\n"
                "emits the bind-pose chunks Assimp's doesn't.\n\n"
                "Try to preserve animations during this conversion anyway?\n\n"
                "Yes — animation curves export, but bind pose is lost (T-pose\n"
                "with hopping skeleton when played).\n\n"
                "No — strip animations cleanly. Output is deterministic static\n"
                "geometry."
            )

        # Clean paths: FBX → anything, GLB → DAE, FBX → GLB, etc.
        return head + (
            "Try to preserve animations during conversion?\n\n"
            "Yes — animation preserved. The " + src_ext + " → " + target_ext + "\n"
            "format pair carries skin and animation data faithfully through\n"
            "Vitriol's export.\n\n"
            "No — strip animations cleanly. Output is deterministic static\n"
            "geometry."
        )

    def _on_convert_all(self) -> None:
        items = self.playlist.items()
        if not items:
            return
        if not dialogs.confirm(self, "Convert all?", f"Convert all {len(items)} item(s) in the playlist?"):
            return
        # Reset sticky animation choices so each batch re-asks.
        for w in items:
            w._animation_choice_made = False
        if not self._bulk_verify_preflight(items):
            return
        if not self._bulk_animation_preflight(items):
            return
        for w in items:
            if not w.is_running() and w.status() != Status.DONE:
                self._start_convert(w, skip_preflight=True)

    def _on_convert_selected(self) -> None:
        items = self.playlist.checked_items()
        if not items:
            dialogs.info(self, "Nothing selected", "Tick the checkboxes of items you want to convert.")
            return
        if not dialogs.confirm(self, "Convert selected?", f"Convert {len(items)} selected item(s)?"):
            return
        for w in items:
            w._animation_choice_made = False
        if not self._bulk_verify_preflight(items):
            return
        if not self._bulk_animation_preflight(items):
            return
        for w in items:
            if not w.is_running():
                self._start_convert(w, skip_preflight=True)

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
        if not w:
            return
        # Stale tick after row widget deletion (cancel+remove race) raises
        # shiboken RuntimeError. Harmless — drop the job mapping.
        try:
            w.set_elapsed(secs)
        except RuntimeError:
            self._job_to_widget.pop(job_id, None)

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
