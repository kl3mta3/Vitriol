"""Confirmation popups and dependency-install prompts."""
from __future__ import annotations
from PySide6.QtWidgets import QMessageBox, QWidget


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
