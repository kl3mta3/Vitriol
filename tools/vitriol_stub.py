"""Tiny native-EXE wrapper for Vitriol.

This script is the entry point packaged into Vitriol.exe via PyInstaller.
Its only job: locate a real Python interpreter on the user's system, then
hand control to launcher.py. The launcher itself uses `sys.executable` to
run `pip install` for first-run dependencies, which only works against a
genuine Python — not the PyInstaller-bundled exe (whose sys.executable
points back at itself).

So the design is:
  1. Find Python (py.exe → python.exe → python3.exe in PATH; user-supplied
     UC_PYTHON env var as override).
  2. Find launcher.py next to the .exe (same dir as Vitriol.exe in the
     portable ZIP / git clone) or up one dir (when this script is being
     invoked from `tools/` during development).
  3. Spawn `<found-python> launcher.py <forwarded args>` and exit with
     its return code.
  4. On no-Python: show a friendly Tk dialog telling the user how to
     install Python, with a link to python.org.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _exe_dir() -> Path:
    """Directory containing the EXE (when frozen) or this script (when run
    directly with `python tools/vitriol_stub.py` for development)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _find_launcher() -> Path | None:
    """launcher.py lives next to Vitriol.exe in the distributed layout.
    During development, when running this stub from tools/, it lives one
    directory up."""
    candidates = [
        _exe_dir() / "launcher.py",
        _exe_dir().parent / "launcher.py",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_python() -> Path | None:
    """Locate a system Python interpreter.

    Search order:
      1. UC_PYTHON env var (absolute path override).
      2. py.exe / python.exe in PATH.
      3. Windows registry — Python's installer writes its location to
         HKCU/HKLM Software\\Python\\PythonCore\\<ver>\\InstallPath.
      4. Known install directories — covers users who didn't tick "Add
         to PATH" plus Microsoft Store / pyenv-win style layouts.

    Why all four: shutil.which() alone misses Python whenever the user
    didn't add it to PATH at install time, when a Store install routes
    through the App Execution Alias system, or when the user's PATH at
    Explorer-launch-time is stale relative to a recent install.
    """
    override = os.environ.get("UC_PYTHON")
    if override and Path(override).exists():
        return Path(override)

    # 2. PATH lookup (covers most python.org installs and `py.exe`).
    for name in ("py.exe", "py", "python.exe", "python", "python3.exe", "python3"):
        found = shutil.which(name)
        if found:
            p = Path(found)
            if _looks_like_real_python(p):
                return p

    # 3. Registry (Windows only).
    if os.name == "nt":
        for p in _python_from_registry():
            if _looks_like_real_python(p):
                return p

    # 4. Known install directories.
    for p in _python_from_known_dirs():
        if _looks_like_real_python(p):
            return p

    return None


def _looks_like_real_python(p: Path) -> bool:
    """Reject the Microsoft Store App Execution Alias stub, which is a
    0-byte placeholder at %LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe
    that pops the Store installer instead of running Python."""
    try:
        return p.exists() and p.is_file() and p.stat().st_size > 1024
    except OSError:
        return False


def _python_from_registry():
    """Yield Python paths recorded in the Windows registry by python.org's
    installer. Walks both HKCU (user installs) and HKLM (system installs)."""
    if os.name != "nt":
        return
    try:
        import winreg
    except ImportError:
        return
    company_keys = (
        r"Software\Python\PythonCore",
        r"Software\Python\ContinuumAnalytics",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for ck in company_keys:
            try:
                company = winreg.OpenKey(hive, ck)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        ver = winreg.EnumKey(company, i)
                    except OSError:
                        break
                    i += 1
                    install_path_key = f"{ver}\\InstallPath"
                    try:
                        with winreg.OpenKey(company, install_path_key) as ip:
                            # Prefer the explicit ExecutablePath value if set.
                            try:
                                exe_path, _ = winreg.QueryValueEx(ip, "ExecutablePath")
                                if exe_path:
                                    yield Path(exe_path)
                                    continue
                            except OSError:
                                pass
                            try:
                                base, _ = winreg.QueryValueEx(ip, "")
                                if base:
                                    yield Path(base) / "python.exe"
                            except OSError:
                                pass
                    except OSError:
                        continue
            finally:
                try:
                    company.Close()
                except Exception:
                    pass


def _python_from_known_dirs():
    """Yield python.exe paths in standard install locations on Windows."""
    parents = []
    home = Path.home()
    parents.append(home / "AppData" / "Local" / "Programs" / "Python")
    parents.append(home / ".pyenv" / "pyenv-win" / "versions")
    parents.append(Path("C:/Program Files"))
    parents.append(Path("C:/Program Files (x86)"))
    parents.append(Path("C:/"))
    for parent in parents:
        try:
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if not child.is_dir():
                    continue
                name = child.name.lower()
                # Match "Python3", "Python311", "python-3.12.0", etc.
                if name.startswith("python"):
                    exe = child / "python.exe"
                    if exe.exists():
                        yield exe
        except OSError:
            continue


def _show_python_missing_dialog() -> None:
    """Pop a small Tk dialog explaining how to install Python. Tk ships
    with the standard library, so it's always available in the
    PyInstaller-bundled stub."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "Vitriol — Python required",
            "Vitriol needs Python 3.11 or newer to run.\n\n"
            "Please install it from https://www.python.org/downloads/\n"
            "and check the 'Add Python to PATH' box during install.\n\n"
            "Then double-click Vitriol.exe again.",
        )
        root.destroy()
    except Exception:
        # No display? Fall back to stderr so cmd-line users see something.
        print("Vitriol requires Python 3.11+. Install from "
              "https://www.python.org/downloads/", file=sys.stderr)


def main() -> int:
    launcher = _find_launcher()
    if launcher is None:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Vitriol — install corrupt",
                "Could not find launcher.py next to Vitriol.exe.\n\n"
                "If you got this from a ZIP archive, make sure you "
                "extracted the WHOLE folder (Vitriol.exe and launcher.py "
                "must live side-by-side).",
            )
            root.destroy()
        except Exception:
            print("launcher.py not found next to Vitriol.exe", file=sys.stderr)
        return 2

    python = _find_python()
    if python is None:
        _show_python_missing_dialog()
        return 3

    # py.exe takes a -3 selector; python.exe doesn't. Adapt accordingly.
    name = python.name.lower()
    if name in ("py.exe", "py"):
        cmd = [str(python), "-3", str(launcher), *sys.argv[1:]]
    else:
        cmd = [str(python), str(launcher), *sys.argv[1:]]

    # CREATE_NO_WINDOW so a blip console doesn't flash before the GUI shows.
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        return subprocess.call(cmd, cwd=str(launcher.parent), creationflags=creationflags)
    except OSError as e:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Vitriol — could not start",
                f"Failed to launch Python:\n\n{e}\n\nFound Python at:\n{python}",
            )
            root.destroy()
        except Exception:
            print(f"Failed to launch Python: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
