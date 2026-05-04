"""Universal Converter — entry point."""
from __future__ import annotations
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from app.utils.logger import get_logger
from app.utils.paths import app_root
from app import format_handlers


def _load_stylesheet(app: QApplication) -> None:
    qss = app_root() / "theme.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))


def main() -> int:
    log = get_logger()
    log.info("starting Universal Converter")

    app = QApplication(sys.argv)
    app.setApplicationName("Universal Converter")
    app.setOrganizationName("UniversalConverter")
    _load_stylesheet(app)

    # Build the format-handler registry now that PySide6 is up.
    format_handlers.load_all()

    # Dependency check (FFmpeg / Assimp / Python packages) is owned by
    # launcher.py — it runs before this process starts. We do not re-check here.

    from app.ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
