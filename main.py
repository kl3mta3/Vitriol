"""Vitriol — entry point."""
from __future__ import annotations
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from app.utils.logger import get_logger
from app.utils.paths import app_root, log_file, resources_dir
from app import format_handlers


def _splash_enabled() -> bool:
    """Decide whether to show the splash screen. CLI flag takes priority,
    then the persisted QSettings preference, then default ON.

    CLI:           --no-splash → splash skipped this launch
    QSettings:     "appearance/show_splash" (bool, default True) →
                    persisted user preference, set via Preferences UI.
    """
    if "--no-splash" in sys.argv:
        return False
    s = QSettings("Vitriol", "Vitriol")
    return s.value("appearance/show_splash", True, type=bool)


# Stable AppUserModelID for Vitriol. Without this, the Windows taskbar
# groups our window under python.exe and shows the Python feather icon —
# even though `app.setWindowIcon(...)` is called. Setting an explicit
# AUMID tells Windows "this is its own app" so the taskbar icon falls
# back to QApplication.windowIcon() instead of the launching exe's.
# Format: <CompanyName>.<ProductName>.<SubProduct>.<Version>
_WIN_AUMID = "Vitriol.Vitriol.App.1"


def _set_windows_aumid() -> None:
    """Tag this process with our AppUserModelID on Windows so the taskbar
    uses our QIcon instead of python.exe's feather. No-op on non-Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_WIN_AUMID)
    except (OSError, AttributeError):
        # ctypes can fail in unusual environments (some sandboxes, MSYS
        # shells); not worth crashing the app over a taskbar icon nicety.
        pass


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
                    f"Vitriol hit an unexpected error:\n\n{short}\n\n"
                    f"Full details written to:\n{log_file()}"
                )
                QTimer.singleShot(0, lambda: QMessageBox.critical(None, "Error", msg))
        except Exception:
            pass
    sys.excepthook = hook


def main() -> int:
    log = get_logger()
    log.info("starting Vitriol")
    _install_exception_logging(log)
    # Must run BEFORE QApplication is created — Windows reads the AUMID
    # of the calling process at QApplication construction time, and once
    # the taskbar entry exists it can't be re-tagged.
    _set_windows_aumid()

    app = QApplication(sys.argv)
    app.setApplicationName("Vitriol")
    app.setOrganizationName("Vitriol")
    _load_app_icon(app)
    _load_stylesheet(app)

    # Splash screen — shown immediately so it covers the heavyweight
    # startup work (format-handler discovery, MainWindow construction).
    # Disable via --no-splash CLI flag or QSettings preference.
    splash = None
    if _splash_enabled():
        from app.ui.splash import VitriolSplash
        splash = VitriolSplash()
        splash.show()
        # Pump events once so the splash is actually painted before we
        # block on heavyweight imports.
        app.processEvents()

    # Build the format-handler registry now that PySide6 is up.
    format_handlers.load_all()

    # Dependency check (FFmpeg / Assimp / Python packages) is owned by
    # launcher.py — it runs before this process starts. We do not re-check here.

    from app.ui.main_window import MainWindow
    win = MainWindow()

    # Splash gating: defer `win.show()` until the splash has fully
    # finished its 3 s ceremony (or the user clicked to skip and the
    # 150 ms fade-out completed). `animation_finished` fires exactly
    # once for either path. Without this the splash would close as
    # soon as the main window is constructed (often ~1.5 s in), cutting
    # the ceremony short and dropping the motto reveal mid-animation.
    #
    # Heavy startup (format_handlers + MainWindow construction) finishes
    # well before the 3 s splash on a normal machine, so this gate is
    # the splash-finishing path. On a slow machine where startup runs
    # past 3 s, the splash will have emitted its signal before we
    # connect — so we also check `_dismissed` and short-circuit.
    if splash is not None:
        def _show_main_window() -> None:
            splash.close()
            win.show()
        if splash._dismissed:
            # Splash already finished while we were building the window.
            # Just show — no need to wait for an event that's already past.
            _show_main_window()
        else:
            splash.animation_finished.connect(_show_main_window)
    else:
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
