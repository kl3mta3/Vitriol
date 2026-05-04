"""One row in the playlist. 10 elements per the spec."""
from __future__ import annotations
from enum import Enum
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


class Status(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


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

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # 1. Checkbox
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(False)
        layout.addWidget(self.checkbox)

        # 2. Status circle
        self.status_circle = QLabel()
        self._apply_status_style()
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

        # 5. Target dropdown
        self.dst_combo = QComboBox()
        self._populate_targets()
        self.dst_combo.setMinimumWidth(82)
        layout.addWidget(self.dst_combo)

        # 6. Save-over-original checkbox
        self.over_checkbox = QCheckBox("Save over original")
        self.over_checkbox.toggled.connect(self._toggle_save_field)
        layout.addWidget(self.over_checkbox)

        # 7. Save location field
        self.save_field = QLineEdit()
        self.save_field.setReadOnly(True)
        self.save_field.setText(str(self._default_save_dir()))
        self.save_field.setMinimumWidth(180)
        self.save_field.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_field.mousePressEvent = self._pick_dir  # type: ignore[assignment]
        layout.addWidget(self.save_field, 1)

        # 8. Convert / Stop button
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setObjectName("RowConvert")
        self.convert_btn.clicked.connect(self._on_convert_or_stop)
        layout.addWidget(self.convert_btn)

        # 9. Time display
        self.time_label = QLabel("--:-- / --:--")
        self.time_label.setObjectName("Muted")
        self.time_label.setMinimumWidth(80)
        layout.addWidget(self.time_label)

        # 10. Remove button
        self.remove_btn = QPushButton("X")
        self.remove_btn.setObjectName("Danger")
        self.remove_btn.clicked.connect(lambda: self.remove_requested.emit(self))
        layout.addWidget(self.remove_btn)

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
        save_dir = Path(self.save_field.text())
        save_dir.mkdir(parents=True, exist_ok=True)
        return save_dir / (self.path.stem + target_ext)

    def save_over_original(self) -> bool:
        return self.over_checkbox.isChecked()

    def set_status(self, status: Status, error_msg: str | None = None) -> None:
        self._status = status
        self._is_running = status == Status.RUNNING
        self._apply_status_style()
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
    def _apply_status_style(self) -> None:
        names = {
            Status.QUEUED: "StatusCircleQueued",
            Status.RUNNING: "StatusCircleRunning",
            Status.DONE: "StatusCircleDone",
            Status.ERROR: "StatusCircleError",
        }
        self.status_circle.setObjectName(names[self._status])
        # Re-polish so QSS picks up the new objectName
        self.status_circle.style().unpolish(self.status_circle)
        self.status_circle.style().polish(self.status_circle)

    def _refresh_button(self) -> None:
        if self._is_running:
            self.convert_btn.setText("Stop")
            self.convert_btn.setObjectName("RowStop")
        else:
            self.convert_btn.setText("Convert")
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
        Preserves the user's selection if it's still valid."""
        previous = self.dst_combo.currentText()
        self._masquerade = masquerade
        self._populate_targets()
        if previous and self.dst_combo.findText(previous) >= 0:
            self.dst_combo.setCurrentText(previous)

    def _default_save_dir(self) -> Path:
        cat = fh.EXT_CATEGORY.get(self.src_ext, "Text")
        return output_dir(cat)

    def _toggle_save_field(self, checked: bool) -> None:
        self.save_field.setVisible(not checked)

    def _pick_dir(self, _event) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self.save_field.text())
        if d:
            self.save_field.setText(d)

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


def _fmt_secs(s: float) -> str:
    s = int(round(max(0.0, s)))
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"
