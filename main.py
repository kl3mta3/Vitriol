"""Transmute — entry point."""
from __future__ import annotations
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.utils.logger import get_logger
from app.utils.paths import app_root, log_file, resources_dir
from app import format_handlers


def _load_stylesheet(app: QApplication) -> None:
    qss = app_root() / "theme.qss"
    if not qss.exists():
        return
    text = qss.read_text(encoding="utf-8")
    # Substitute {RES} with the resources directory so QSS image: url() rules
    # can reference SVG assets we ship in resources/. QSS only accepts absolute
    # paths or paths inside Qt resource files; this lets us avoid Qt resources.
    res = str(resources_dir()).replace("\\", "/")
    text = text.replace("{RES}", res)
    app.setStyleSheet(text)


def _load_app_icon(app: QApplication) -> None:
    """Set the app/window icon. Prefers the original logo.ico (discs filled
    with dark on transparent canvas) — at 16/32 px the dark discs anchor the
    composition visually, and on dark host surfaces (Windows dark-theme
    title bar / taskbar) they blend into the chrome. Falls back through the
    other variants if logo.ico isn't present."""
    res = resources_dir()
    for candidate in ("icons/logo.ico",
                       "icons/logo-outline.ico",
                       "icons/logo-bg.ico",
                       "logo.svg"):
        p = res / candidate
        if p.exists():
            app.setWindowIcon(QIcon(str(p)))
            return


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
                    f"Transmute hit an unexpected error:\n\n{short}\n\n"
                    f"Full details written to:\n{log_file()}"
                )
                QTimer.singleShot(0, lambda: QMessageBox.critical(None, "Error", msg))
        except Exception:
            pass
    sys.excepthook = hook


def main() -> int:
    log = get_logger()
    log.info("starting Transmute")
    _install_exception_logging(log)

    app = QApplication(sys.argv)
    app.setApplicationName("Transmute")
    app.setOrganizationName("Transmute")
    _load_app_icon(app)
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
