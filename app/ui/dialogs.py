"""Confirmation popups and dependency-install prompts."""
from __future__ import annotations
from typing import Any
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QSizePolicy, QTextBrowser, QVBoxLayout,
    QWidget,
)


def confirm(parent: QWidget | None, title: str, message: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def info(parent: QWidget | None, title: str, message: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Information)
    box.exec()


def error(parent: QWidget | None, title: str, message: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Critical)
    box.exec()


def ask_install_dependency(parent: QWidget | None, name: str, size_label: str, why: str) -> bool:
    msg = (
        f"{name} is required for {why}.\n\n"
        f"Download and install it locally? (~{size_label})\n\n"
        "If you decline, the app will still launch but conversions in this category will be unavailable."
    )
    return confirm(parent, f"Install {name}?", msg)


def confirm_with_apply_to_all(parent: QWidget | None, title: str, message: str,
                               apply_label: str = "Apply to all in this batch") -> tuple[bool, bool]:
    """Yes/No prompt with an additional 'apply to all' checkbox.

    Returns (yes_chosen, apply_to_all_checked). Used for batch
    conversions where one decision can sanely apply to multiple rows
    (e.g. 'preserve animations on all 3D files in this batch?')."""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(message)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    cb = QCheckBox(apply_label)
    box.setCheckBox(cb)
    result = box.exec()
    return (result == QMessageBox.StandardButton.Yes, cb.isChecked())


# ---------------------------------------------------------------------------
# Update prompt
# ---------------------------------------------------------------------------


# Result codes returned by UpdateAvailableDialog.run() so the caller
# can branch on what the user picked. Plain ints (not an enum) so the
# caller doesn't need to import the dialog class just to read the
# return value.
UPDATE_INSTALL = 1       # "Update now" — download + launch installer
UPDATE_OPEN_PAGE = 2     # "Open download page" — portable build path
UPDATE_SKIP = 3          # "Skip this version"
UPDATE_LATER = 0         # "Remind me later" / dialog closed


class UpdateAvailableDialog(QDialog):
    """Modal dialog announcing a new release with collapsible release notes.

    Three actions, returned as plain integer codes so callers can branch
    without importing this class:

      UPDATE_INSTALL    — user clicked "Update now"
      UPDATE_OPEN_PAGE  — user clicked "Open download page" (portable build)
      UPDATE_SKIP       — user clicked "Skip this version"
      UPDATE_LATER      — user closed the dialog or clicked "Remind me later"

    Construct with:
      dlg = UpdateAvailableDialog(parent, info, portable=is_portable_build())
      result = dlg.run()
    """

    def __init__(self, parent: QWidget | None, info: dict[str, Any],
                 portable: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Vitriol — update available")
        self.setModal(True)
        self.resize(540, 420)
        self._result_code = UPDATE_LATER

        version = info.get("version", "")
        published = _format_published(info.get("published_at", ""))
        size = _format_size(info.get("size", 0))
        notes = info.get("notes", "") or "(No release notes provided.)"

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Header — large version line + smaller metadata.
        header = QLabel(f"<h2>Vitriol {version} is available</h2>")
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        meta_bits = []
        if published:
            meta_bits.append(f"Released {published}")
        if size:
            meta_bits.append(f"{size} download")
        if meta_bits:
            meta = QLabel(" · ".join(meta_bits))
            meta.setStyleSheet("color: #999;")
            layout.addWidget(meta)

        notes_label = QLabel("<b>What's new</b>")
        notes_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(notes_label)

        # Release notes — render as markdown via QTextBrowser. setMarkdown
        # is Qt 5.14+; PySide6 always has it. External links in the
        # notes open in the user's browser instead of inside the dialog.
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMarkdown(notes)
        browser.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(browser, stretch=1)

        # Three button row — primary action depends on portable vs.
        # installed. The button labels are spelled out in full so a user
        # who tabs to one and reads the tooltip-less label still knows
        # what each does.
        buttons = QDialogButtonBox()

        if portable:
            primary = QPushButton("Open download page")
            primary.setDefault(True)
            primary.clicked.connect(lambda: self._finish(UPDATE_OPEN_PAGE))
            buttons.addButton(primary, QDialogButtonBox.ButtonRole.AcceptRole)
        else:
            primary = QPushButton("Update now")
            primary.setDefault(True)
            primary.clicked.connect(lambda: self._finish(UPDATE_INSTALL))
            buttons.addButton(primary, QDialogButtonBox.ButtonRole.AcceptRole)

        skip_btn = QPushButton("Skip this version")
        skip_btn.clicked.connect(lambda: self._finish(UPDATE_SKIP))
        buttons.addButton(skip_btn, QDialogButtonBox.ButtonRole.DestructiveRole)

        later_btn = QPushButton("Remind me later")
        later_btn.clicked.connect(lambda: self._finish(UPDATE_LATER))
        buttons.addButton(later_btn, QDialogButtonBox.ButtonRole.RejectRole)

        layout.addWidget(buttons)

    def _finish(self, code: int) -> None:
        self._result_code = code
        self.accept()

    def run(self) -> int:
        """Show modally, return one of the UPDATE_* result codes."""
        self.exec()
        return self._result_code


def _format_published(iso: str) -> str:
    """Pretty-print a GitHub-style ISO-8601 timestamp.

    "2026-05-08T14:23:11Z" -> "May 8, 2026". Short month + day so the
    line stays readable; the year is included because re-discovering an
    update banner months later shouldn't require math to figure out
    when it was published.

    Returns the empty string on any parse failure — the meta line just
    omits the released-date bit if we can't read it.
    """
    if not iso:
        return ""
    try:
        from datetime import datetime
        # GitHub uses 'Z' for UTC; fromisoformat needs +00:00 in 3.10.
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        # %-d / %#d are non-portable across platforms (POSIX uses %-d,
        # Windows uses %#d). Format with %d (zero-padded) and strip the
        # leading zero ourselves so the same code works everywhere.
        return dt.strftime("%b %d, %Y").replace(" 0", " ")
    except (ValueError, TypeError):
        return iso


def _format_size(n: int) -> str:
    """Render a byte count as e.g. "47.3 MB" / "612 KB". Returns "" on 0."""
    if not n:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
