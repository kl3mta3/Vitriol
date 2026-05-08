"""One row in the playlist. 10 elements per the spec."""
from __future__ import annotations
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPainter, QColor, QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QWidget, QSizePolicy
)

from .. import format_handlers as fh
from ..core.file_detector import detect, normalize_ext
from ..utils.paths import output_dir
from ._status import Status
from .status_glyph import StatusGlyph


class ProgressLabel(QLabel):
    """A label with a translucent green fill from left to right showing progress."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._progress = 0.0
        self.setMinimumWidth(220)
        self.setMaximumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

    def set_progress(self, p: float) -> None:
        self._progress = max(0.0, min(1.0, p))
        self.update()

    def progress(self) -> float:
        return self._progress

    def set_text_with_ellipsis(self, text: str) -> None:
        self.setToolTip(text)
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(text, Qt.TextElideMode.ElideMiddle, max(60, self.width() - 8))
        self.setText(elided)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # Re-elide on resize so the title stays readable.
        if self.toolTip():
            self.set_text_with_ellipsis(self.toolTip())
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        # Paint the green overlay first, then the text on top.
        if self._progress > 0:
            p = QPainter(self)
            color = QColor(46, 204, 113, 90)  # translucent green
            w = int(self.width() * self._progress)
            p.fillRect(0, 0, w, self.height(), color)
            p.end()
        super().paintEvent(event)


class PlaylistItemWidget(QWidget):
    """Emits signals for the controlling MainWindow to wire up to the queue."""

    convert_requested = Signal(object)   # PlaylistItemWidget
    stop_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, path: Path, parent=None, masquerade: bool = False) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.src_ext = detect(self.path)
        self._masquerade = masquerade
        self._status = Status.QUEUED
        self._is_running = False
        self._estimated_total_sec: float | None = None
        self._elapsed_sec: float = 0.0
        # When False, the save folder auto-derives from the *target* ext's
        # category and refreshes whenever the target dropdown changes. Once
        # the user picks a folder via _pick_dir, we stop overriding their
        # choice on subsequent target changes.
        self._save_dir_overridden = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # 1. Checkbox
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(False)
        layout.addWidget(self.checkbox)

        # 2. Status glyph (custom-painted: ring / rotor / disc-with-mark)
        self.status_circle = StatusGlyph()
        layout.addWidget(self.status_circle)

        # 3. Title with progress overlay
        self.title = ProgressLabel()
        self.title.set_text_with_ellipsis(self.path.name)
        layout.addWidget(self.title)

        # 4. Source dropdown (locked)
        self.src_combo = QComboBox()
        self.src_combo.addItem(self.src_ext)
        self.src_combo.setEnabled(False)
        self.src_combo.setMinimumWidth(72)
        layout.addWidget(self.src_combo)

        # 5. Target dropdown (populated AFTER convert_btn is created — see below)
        self.dst_combo = QComboBox()
        self.dst_combo.setMinimumWidth(82)
        layout.addWidget(self.dst_combo)

        # 6. Overwrite checkbox (saves over original; tooltip explains)
        self.over_checkbox = QCheckBox("Overwrite")
        self.over_checkbox.setToolTip("save over original")
        self.over_checkbox.toggled.connect(self._toggle_save_field)
        layout.addWidget(self.over_checkbox)

        # 6b. Compiler checkbox — only visible when the target is .py.
        # When ON, the .py output is a self-extracting Stone script that
        # reconstructs the source when run. When OFF, the .py is just the
        # source's text content saved with a .py extension (will likely
        # not be runnable Python for non-text sources, but matches what
        # the user picked). Independent of the global Stone toggle.
        self.compiler_checkbox = QCheckBox("Compiler")
        self.compiler_checkbox.setToolTip(
            "Output a self-extracting .py script that reconstructs the "
            "original file when run with Python."
        )
        self.compiler_checkbox.setVisible(False)
        layout.addWidget(self.compiler_checkbox)
        # Toggling Compiler flips the row from "plain .py text dump" to
        # "Stone-mode self-extracting script" — which means the password
        # lock becomes available even for sources that share .py's
        # category (text → .py). Re-evaluate the lock visibility on
        # every toggle so the 🔓 appears/disappears immediately rather
        # than waiting for the user to re-pick the target.
        self.compiler_checkbox.toggled.connect(
            lambda _checked: self._refresh_stone_lock_visibility()
        )

        # 6c. Stone password toggle — only visible when global Stone is on.
        # Renders as 🔓 (no password set) or 🔒 (password set). Click opens
        # a modal dialog. Per-row password is held in widget memory only —
        # never written to disk. The byte string is forwarded to the queue
        # via Job.password.
        self._stone_password: bytes = b""

        # Per-row choice for 3D animation/rig preservation. Set by
        # MainWindow's pre-flight prompt when the source has animations
        # AND the target format can carry them (.fbx/.dae/.glb/.gltf).
        # Forwarded to Job.preserve_animations. Default False = drop
        # animations (the safe, deterministic outcome). MainWindow only
        # prompts the user when both conditions above are met; for all
        # other rows this stays False and is harmless.
        self._preserve_animations: bool = False
        self.stone_lock_btn = QPushButton("\U0001F513")  # 🔓
        self.stone_lock_btn.setObjectName("StoneLock")
        self.stone_lock_btn.setFlat(True)
        self.stone_lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        # Size the emoji glyph proportional to other row controls. Default
        # Qt button rendering would clip the lock at ~10pt with no padding;
        # bump font-size + give 4px horizontal breathing room.
        self.stone_lock_btn.setFixedSize(34, 28)
        self.stone_lock_btn.setStyleSheet(
            "QPushButton#StoneLock {"
            " font-size: 16px;"
            " padding: 0 4px;"
            " border: none;"
            " background: transparent;"
            "}"
            "QPushButton#StoneLock:hover {"
            " background: rgba(255, 255, 255, 0.08);"
            " border-radius: 4px;"
            "}"
        )
        self.stone_lock_btn.setToolTip(
            "Set Stone password (files round-trip with this password)")
        self.stone_lock_btn.clicked.connect(self._on_stone_lock_clicked)
        # Initial visibility: gated below in _refresh_stone_lock_visibility
        # once dst_combo is populated (visibility depends on cross-type).
        layout.addWidget(self.stone_lock_btn)

        # 7. Save location field — stretches to fill remaining width.
        # Path text is ellipsized when the field is too narrow; full path
        # appears as tooltip on hover. Stretch factor 1 keeps it taking
        # the bulk of the row's free space. Min width drops a bit to make
        # room for the optional Compiler toggle without overflowing.
        self.save_field = QLineEdit()
        self.save_field.setReadOnly(True)
        self.save_field.setMinimumWidth(140)
        self.save_field.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_field.mousePressEvent = self._pick_dir  # type: ignore[assignment]
        self._set_save_field_path(str(self._default_save_dir()))
        layout.addWidget(self.save_field, 1)

        # 8. Transmute / Stop button. "Transmute" is the verb (the action
        # the button performs) — Vitriol the product transmutes the file.
        self.convert_btn = QPushButton("Transmute")
        self.convert_btn.setObjectName("RowConvert")
        self.convert_btn.clicked.connect(self._on_convert_or_stop)
        layout.addWidget(self.convert_btn)

        # 9. Time display — simple "elapsed / estimated" format.
        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("Muted")
        self.time_label.setMinimumWidth(80)
        layout.addWidget(self.time_label)

        # 10. Remove button
        self.remove_btn = QPushButton("X")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.remove_btn)

        # Now that convert_btn exists, populate the target dropdown.
        # Doing this after every widget is created lets _populate_targets
        # enable/disable convert_btn based on whether any targets exist.
        self._populate_targets()
        # Save folder follows the *target* extension's category, not the
        # source's — so .txt → .wav saves to output/Audio/, not output/Text/.
        self._refresh_save_field()
        self.compiler_checkbox.setVisible(self.target_ext() == ".py")
        self._refresh_stone_lock_visibility()
        self.dst_combo.currentTextChanged.connect(self._on_target_changed)

    # --- public API ---------------------------------------------------------
    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def status(self) -> Status:
        return self._status

    def is_running(self) -> bool:
        return self._is_running

    def target_ext(self) -> str | None:
        t = self.dst_combo.currentText()
        return t if t else None

    def output_path(self) -> Path:
        target_ext = self.target_ext() or ".out"
        if self.over_checkbox.isChecked():
            return self.path.with_suffix(target_ext)
        # Use the FULL path stashed by _set_save_field_path — save_field.text()
        # may be elided (contains '…' for long paths) which would create a
        # literal '…'-named directory and silently misroute the output.
        full = getattr(self, "_full_save_path", None) or self.save_field.text()
        save_dir = Path(full)
        save_dir.mkdir(parents=True, exist_ok=True)
        return save_dir / (self.path.stem + target_ext)

    def save_over_original(self) -> bool:
        return self.over_checkbox.isChecked()

    def compiler_enabled(self) -> bool:
        """True only when the row is targeting .py and the per-row Compiler
        toggle is on. Otherwise the queue treats this as a normal conversion."""
        if self.target_ext() != ".py":
            return False
        return self.compiler_checkbox.isChecked()

    def stone_password(self) -> bytes:
        """The user-set per-row Stone password. Empty bytes means use the
        default app key (still encrypted, anyone with Vitriol can decode).
        Held in widget memory only; never persisted."""
        return self._stone_password

    def preserve_animations(self) -> bool:
        """Per-row 3D animation-preservation choice. Set by MainWindow's
        pre-flight prompt; default False = drop animations on export."""
        return self._preserve_animations

    def set_preserve_animations(self, value: bool) -> None:
        self._preserve_animations = bool(value)

    def _on_stone_lock_clicked(self) -> None:
        from .stone_password_dialog import StonePasswordDialog
        dlg = StonePasswordDialog(current_password=self._stone_password, parent=self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            chosen = dlg.selected_password()
            if chosen is None:
                return
            self._stone_password = chosen
            self._refresh_stone_lock_icon()

    def _refresh_stone_lock_icon(self) -> None:
        if self._stone_password:
            self.stone_lock_btn.setText("\U0001F512")  # 🔒
            self.stone_lock_btn.setToolTip(
                "Stone password set — click to change or clear")
        else:
            self.stone_lock_btn.setText("\U0001F513")  # 🔓
            self.stone_lock_btn.setToolTip(
                "Set Stone password (files round-trip with this password)")

    def _is_cross_type_row(self) -> bool:
        """True iff this row would actually engage encryption. Cross-category
        rows (text→audio, image→video, etc.) trigger v3 encryption; same-type
        rows do not. Zip targets are special-cased to ALWAYS return False
        regardless of cross-type — zip never encrypts, since encryption would
        corrupt the archive structure and defeat the "real zip" property."""
        src = self.src_ext.lower()
        dst = (self.target_ext() or "").lower()
        if not dst or not src:
            return False
        # Zip targets never encrypt — hide the icon regardless of category.
        if dst == ".zip":
            return False
        # `.py` with Compiler ON, and `.exe` always, are Stone-mode
        # wrapping operations regardless of source category. They support
        # password protection independent of whether the source is image,
        # audio, doc, etc. Without these special-cases, doc→.py and
        # doc→.exe (e.g. .pdf → .py) would collapse to same-category
        # ("doc") and silently hide the password icon.
        if dst == ".py" and self.compiler_checkbox.isChecked():
            return True
        if dst == ".exe":
            return True
        # STONE_ONLY_SOURCES (.zip, .exe) have no "same-type" doc category
        # in any meaningful sense — every conversion FROM them is inherently
        # transformative and engages encryption. Treat them as cross-type
        # whenever the destination differs (which it always will at this
        # point — same-ext .zip→.zip / .exe→.exe is filtered out elsewhere).
        if src in fh.STONE_ONLY_SOURCES and src != dst:
            return True
        # Non-media exts collapse to "doc"; matches the router's
        # _is_cross_category logic so the UI gate stays in sync.
        src_cat = fh.MEDIA_CATEGORY_OF.get(src, "doc")
        dst_cat = fh.MEDIA_CATEGORY_OF.get(dst, "doc")
        return src_cat != dst_cat

    def _refresh_stone_lock_visibility(self) -> None:
        """Show the lock icon only when Stone is engaged AND this row is a
        cross-type conversion. Clears any stored password when the row
        turns same-type — a leftover password would never apply, since
        same-type Stone uses plaintext envelopes.

        "Stone is engaged" means either the global toggle is on OR the
        source extension is in STONE_ONLY_SOURCES (.zip, .exe), since
        those auto-engage Stone in `valid_targets_for` and the router.
        Without this OR-clause, the lock icon would stay hidden when the
        user adds a .zip with the global Stone toggle OFF — even though
        the actual conversion runs through Stone and supports a password."""
        src = self.src_ext.lower()
        stone_engaged = self._masquerade or src in fh.STONE_ONLY_SOURCES
        visible = stone_engaged and self._is_cross_type_row()
        self.stone_lock_btn.setVisible(visible)
        if not visible and self._stone_password:
            self._stone_password = b""
            self._refresh_stone_lock_icon()

    def set_status(self, status: Status, error_msg: str | None = None) -> None:
        self._status = status
        self._is_running = status == Status.RUNNING
        self.status_circle.set_status(status)
        if status == Status.ERROR and error_msg:
            self.status_circle.setToolTip(error_msg)
        else:
            self.status_circle.setToolTip("")
        self._refresh_button()

    def set_progress(self, p: float) -> None:
        self.title.set_progress(p)
        # Update time estimate based on elapsed
        self._update_time_label(p)

    def set_elapsed(self, seconds: float) -> None:
        self._elapsed_sec = seconds
        self._update_time_label(self.title.progress())

    def reset_for_rerun(self) -> None:
        self.title.set_progress(0.0)
        self._estimated_total_sec = None
        self._elapsed_sec = 0.0
        self.time_label.setText("--:-- / --:--")
        self.set_status(Status.QUEUED)

    # --- internals ----------------------------------------------------------
    def _refresh_button(self) -> None:
        if self._is_running:
            self.convert_btn.setText("Stop")
            self.convert_btn.setObjectName("RowStop")
        else:
            self.convert_btn.setText("Transmute")
            self.convert_btn.setObjectName("RowConvert")
        self.convert_btn.style().unpolish(self.convert_btn)
        self.convert_btn.style().polish(self.convert_btn)

    def _populate_targets(self) -> None:
        self.dst_combo.clear()
        targets = fh.valid_targets_for(self.src_ext, masquerade=self._masquerade)
        for t in targets:
            self.dst_combo.addItem(t)
        default = fh.default_target_for(self.src_ext)
        if default and default in targets:
            self.dst_combo.setCurrentText(default)
        if not targets:
            self.dst_combo.addItem("(no targets)")
            self.dst_combo.setEnabled(False)
            self.convert_btn.setEnabled(False)
            self.set_status(Status.ERROR, f"No conversion targets registered for {self.src_ext}.")
        else:
            self.dst_combo.setEnabled(True)
            self.convert_btn.setEnabled(True)

    def refresh_targets(self, masquerade: bool) -> None:
        """Re-populate dropdown when the global Masquerade toggle changes.
        Preserves the user's selection if it's still valid. Also toggles
        the Stone-password lock icon's visibility (cross-type rows only)
        and clears any stored per-row password when Stone goes OFF or
        when the row drops out of cross-type."""
        previous = self.dst_combo.currentText()
        self._masquerade = masquerade
        self._populate_targets()
        if previous and self.dst_combo.findText(previous) >= 0:
            self.dst_combo.setCurrentText(previous)
        # Lock icon visibility (cross-type-gated) + clear-on-hide.
        self._refresh_stone_lock_visibility()

    def _default_save_dir(self) -> Path:
        """Pick the output folder based on the *target* extension's category.
        Falls back to the source ext if no target is chosen yet."""
        ext = self.target_ext() or self.src_ext
        cat = fh.EXT_CATEGORY.get(ext, "Text")
        return output_dir(cat)

    def _refresh_save_field(self) -> None:
        """Update the displayed save folder to match the current target ext.
        Skipped if the user has manually picked a folder via _pick_dir."""
        if self._save_dir_overridden:
            return
        self._set_save_field_path(str(self._default_save_dir()))

    def _set_save_field_path(self, path: str) -> None:
        """Store the full path on the widget + tooltip; show an elided form
        in the field text since save_field's display width is narrow now."""
        self._full_save_path = path
        self.save_field.setToolTip(path)
        fm = QFontMetrics(self.save_field.font())
        elided = fm.elidedText(path, Qt.TextElideMode.ElideMiddle,
                                max(60, self.save_field.width() - 16))
        self.save_field.setText(elided)

    def _on_target_changed(self, _new_text: str) -> None:
        self._refresh_save_field()
        # Show the Compiler toggle only when .py is the target.
        is_py = (self.target_ext() == ".py")
        self.compiler_checkbox.setVisible(is_py)
        if not is_py:
            self.compiler_checkbox.setChecked(False)
        # Lock icon is gated on cross-type — re-evaluate when target changes.
        self._refresh_stone_lock_visibility()

    def _toggle_save_field(self, checked: bool) -> None:
        self.save_field.setVisible(not checked)

    def _pick_dir(self, _event) -> None:
        current = getattr(self, "_full_save_path", self.save_field.text())
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", current)
        if d:
            self._set_save_field_path(d)
            # User has chosen explicitly — stop auto-deriving on target changes.
            self._save_dir_overridden = True

    def _on_convert_or_stop(self) -> None:
        if self._is_running:
            self.stop_requested.emit(self)
        else:
            self.convert_requested.emit(self)

    def _update_time_label(self, progress: float) -> None:
        elapsed = _fmt_secs(self._elapsed_sec)
        if progress > 0.02 and self._is_running:
            est_total = self._elapsed_sec / max(progress, 0.02)
            total = _fmt_secs(est_total)
        elif self._status == Status.DONE:
            total = elapsed
        else:
            total = "--:--"
        self.time_label.setText(f"{elapsed} / {total}")

    def update_bytes_progress(self, processed: int, total: int) -> None:
        """No-op stub. The bytes_progress signal is still emitted by the
        queue (kept for any future use), but the time_label stays in the
        simple `elapsed / estimated` format produced by _update_time_label.
        Left as a stub so MainWindow's wire-up doesn't have to change."""
        return


def _fmt_secs(s: float) -> str:
    s = int(round(max(0.0, s)))
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"
