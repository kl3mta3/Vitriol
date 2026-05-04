"""Universal Converter — entry point."""
from __future__ import annotations
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from app.utils.logger import get_logger
from app.utils.paths import app_root, log_file
from app import format_handlers


def _load_stylesheet(app: QApplication) -> None:
    qss = app_root() / "theme.qss"
    if qss.exists():
        app.setStyleSheet(qss.read_text(encoding="utf-8"))


def _install_exception_logging(log) -> None:
    """Route uncaught Python exceptions through the file logger so future bugs
    leave a trace even when they bubble up out of Qt slots. Without this, Qt
    just prints to stderr and nothing lands on disk."""
    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        log.error("uncaught exception:\n%s", tb_text)
        # Also try to surface a non-blocking dialog so the user knows something
        # went wrong (instead of "nothing happened").
        try:
            app = QApplication.instance()
            if app is not None:
                short = f"{exc_type.__name__}: {exc_value}"
                msg = (
                    f"Universal Converter hit an unexpected error:\n\n{short}\n\n"
                    f"Full details written to:\n{log_file()}"
                )
                QTimer.singleShot(0, lambda: QMessageBox.critical(None, "Error", msg))
        except Exception:
            pass
    sys.excepthook = hook


def main() -> int:
    log = get_logger()
    log.info("starting Universal Converter")
    _install_exception_logging(log)

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
