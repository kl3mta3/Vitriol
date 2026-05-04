"""Top-level QMainWindow: title, drop zone, playlist, bulk action buttons."""
from __future__ import annotations
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QMainWindow, QPushButton, QStatusBar, QVBoxLayout, QWidget
)

from .drop_zone import DropZone
from .playlist import Playlist
from .playlist_item import PlaylistItemWidget, Status
from . import dialogs
from ..core.conversion_queue import ConversionQueue
from ..utils import settings


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Universal Converter")
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

        # Top bar: title + global toggles (Masquerade Mode, Verify Round-Trip).
        topbar = QHBoxLayout()
        title = QLabel("Universal Converter")
        title.setObjectName("AppTitle")
        topbar.addWidget(title)
        topbar.addStretch(1)
        self.chk_masquerade = QCheckBox("Masquerade Mode")
        self.chk_masquerade.setChecked(bool(settings.get("masquerade_enabled")))
        self.chk_masquerade.setToolTip(
            "Hide any file's bytes inside a lossless container (WAV, PNG, BMP, TXT). "
            "Expands the target dropdowns to include cross-category byte-passthrough."
        )
        self.chk_masquerade.toggled.connect(self._on_masquerade_toggled)
        topbar.addWidget(self.chk_masquerade)
        self.chk_verify = QCheckBox("Verify Round-Trip")
        self.chk_verify.setChecked(bool(settings.get("verify_round_trip")))
        self.chk_verify.setToolTip(
            "After converting, immediately convert back and hash-compare against the source. "
            "Only meaningful with Masquerade Mode on."
        )
        self.chk_verify.toggled.connect(self._on_verify_toggled)
        topbar.addWidget(self.chk_verify)
        self._update_verify_enabled()
        outer.addLayout(topbar)

        self.drop_zone = DropZone()
        self.drop_zone.files_added.connect(self._on_files_added)
        outer.addWidget(self.drop_zone)

        self.playlist = Playlist()
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

    def _on_files_added(self, paths: list) -> None:
        # Filter to known extensions; unknown ones are still added so the user sees the error inline.
        self.playlist.add_paths([Path(p) for p in paths],
                                masquerade=self.chk_masquerade.isChecked())
        # Wire per-item signals (new widgets only)
        for w in self.playlist.items():
            try:
                w.convert_requested.disconnect(self._start_convert)  # type: ignore[arg-type]
            except (TypeError, RuntimeError):
                pass
            try:
                w.stop_requested.disconnect(self._stop_convert)  # type: ignore[arg-type]
            except (TypeError, RuntimeError):
                pass
            w.convert_requested.connect(self._start_convert)
            w.stop_requested.connect(self._stop_convert)
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
        masq = bool(self.chk_masquerade.isChecked())
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

    def _on_masquerade_toggled(self, checked: bool) -> None:
        settings.set("masquerade_enabled", bool(checked))
        self._update_verify_enabled()
        # Refresh every playlist row's target dropdown — masquerade adds/removes
        # cross-category byte-passthrough hosts.
        for w in self.playlist.items():
            w.refresh_targets(masquerade=checked)
        self.statusBar().showMessage(
            "Masquerade Mode " + ("ON — byte-passthrough hosts available." if checked
                                  else "OFF.")
        )

    def _on_verify_toggled(self, checked: bool) -> None:
        settings.set("verify_round_trip", bool(checked))

    def _update_verify_enabled(self) -> None:
        self.chk_verify.setEnabled(self.chk_masquerade.isChecked())
        if not self.chk_masquerade.isChecked():
            self.chk_verify.setToolTip(
                "Verify Round-Trip is only available with Masquerade Mode on, "
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
