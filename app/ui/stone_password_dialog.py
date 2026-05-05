"""Per-row Philosopher's Stone password dialog.

Modal popup invoked from the playlist row's lock icon. Holds a single
QLineEdit (password input, masked), a show/hide toggle, and Cancel/Set
buttons. The set password is returned as bytes via `selected_password()`
(or None if user cancelled).

Empty-string Set is meaningful — it clears the password and reverts the
row to the default-key behavior. The icon then renders as 🔓 again.
"""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)


class StonePasswordDialog(QDialog):
    """Modal for setting / clearing the password on a single playlist row."""

    def __init__(self, current_password: bytes = b"", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stone Password")
        self.setModal(True)
        self.setMinimumWidth(360)

        self._result: Optional[bytes] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # Password row: field + show/hide checkbox.
        pw_row = QHBoxLayout()
        pw_label = QLabel("Password:")
        pw_label.setMinimumWidth(70)
        pw_row.addWidget(pw_label)
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        # Pre-fill with the row's current password (masked) so the user
        # can edit instead of retype.
        try:
            self.field.setText(current_password.decode("utf-8"))
        except UnicodeDecodeError:
            self.field.setText("")
        self.field.setPlaceholderText("(empty = default key)")
        pw_row.addWidget(self.field, 1)
        self.show_chk = QCheckBox("Show")
        self.show_chk.toggled.connect(self._on_show_toggled)
        pw_row.addWidget(self.show_chk)
        layout.addLayout(pw_row)

        # Help text.
        help_label = QLabel(
            "Files converted with this password round-trip with the same "
            "password.\nWrong password produces unrecoverable garbage. "
            "Forget it = file is gone."
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #9090a0; font-size: 11px;")
        layout.addWidget(help_label)

        # Buttons.
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_btn)
        self.set_btn = QPushButton("Set Password")
        self.set_btn.setDefault(True)
        self.set_btn.clicked.connect(self._on_set)
        btn_row.addWidget(self.set_btn)
        layout.addLayout(btn_row)

        # Enter submits, Esc cancels.
        QShortcut(QKeySequence(Qt.Key.Key_Return), self).activated.connect(self._on_set)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self).activated.connect(self._on_set)

        self.field.setFocus()

    def _on_show_toggled(self, checked: bool) -> None:
        self.field.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_set(self) -> None:
        self._result = self.field.text().encode("utf-8")
        self.accept()

    def selected_password(self) -> Optional[bytes]:
        """Returns the password bytes the user set (may be empty), or None
        if they cancelled."""
        return self._result
