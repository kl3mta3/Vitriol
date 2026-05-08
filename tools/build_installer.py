"""Build the Inno Setup installer for Vitriol.

Produces `dist/VitriolSetup-<version>.exe` from the PyInstaller dist
folder at `dist/Vitriol/`.

Usage:
    python tools/build_installer.py

Prerequisites:
  - Inno Setup 6+ installed. Download: https://jrsoftware.org/isdl.php
    Default install path: `C:\\Program Files (x86)\\Inno Setup 6\\iscc.exe`.
  - `dist/Vitriol/` must already exist (built by
    `tools/build_vitriol_dist.py`). If missing, this script offers to
    run the dist-build first.

The installer is unsigned. Windows SmartScreen will warn end users on
first run; signing with an EV certificate is a separate post-step.
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys
from pathlib import Path


# Common locations Inno Setup's compiler installs to.
_ISCC_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Inno Setup 6\iscc.exe"),
    Path(r"C:\Program Files\Inno Setup 6\iscc.exe"),
    Path(r"C:\Program Files (x86)\Inno Setup 5\iscc.exe"),
    Path(r"C:\Program Files\Inno Setup 5\iscc.exe"),
]


def _find_iscc() -> Path | None:
    """Locate iscc.exe. Returns None if Inno Setup isn't installed."""
    # Try the standard install paths first.
    for p in _ISCC_CANDIDATES:
        if p.exists():
            return p
    # Try PATH (some users add Inno Setup to their PATH).
    on_path = shutil.which("iscc")
    if on_path:
        return Path(on_path)
    return None


def _read_version(repo: Path) -> str:
    """Read __version__ from app/__version__.py without importing the
    rest of the app (which would pull in PySide6 and the world)."""
    version_file = repo / "app" / "__version__.py"
    if not version_file.exists():
        print(f"missing version file: {version_file}", file=sys.stderr)
        sys.exit(2)
    text = version_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("__version__"):
            eq = line.find("=")
            if eq > 0:
                rhs = line[eq + 1:].strip().strip("'\"")
                if rhs:
                    return rhs
    print(f"could not parse __version__ from {version_file}", file=sys.stderr)
    sys.exit(2)


def _ensure_dist(repo: Path) -> None:
    """Verify dist/Vitriol/Vitriol.exe exists. Offer to build it if
    not, or refuse if the user can't approve."""
    dist_exe = repo / "dist" / "Vitriol" / "Vitriol.exe"
    if dist_exe.exists():
        return
    print("dist/Vitriol/Vitriol.exe not found.", file=sys.stderr)
    print(f"  Expected at: {dist_exe}", file=sys.stderr)
    print(f"  Run `python tools/build_vitriol_dist.py` first.", file=sys.stderr)
    sys.exit(3)


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    iscc = _find_iscc()
    if iscc is None:
        print("Inno Setup compiler (iscc.exe) not found.", file=sys.stderr)
        print("  Install Inno Setup 6+ from https://jrsoftware.org/isdl.php",
              file=sys.stderr)
        print("  Default install path is:", file=sys.stderr)
        print(r"    C:\Program Files (x86)\Inno Setup 6\iscc.exe", file=sys.stderr)
        return 4
    print(f"Using iscc: {iscc}")

    _ensure_dist(repo)

    version = _read_version(repo)
    print(f"Building installer for Vitriol {version}")

    iss = repo / "tools" / "vitriol.iss"
    if not iss.exists():
        print(f"missing Inno Setup script: {iss}", file=sys.stderr)
        return 5

    # Pass version into the .iss via /D. iscc reads stdout/stderr as
    # bytes — decode here for nicer console output.
    cmd = [
        str(iscc),
        f"/DAppVersion={version}",
        str(iss),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(repo / "tools"))
    if rc != 0:
        print(f"iscc failed with exit code {rc}", file=sys.stderr)
        return rc

    out = repo / "dist" / f"VitriolSetup-{version}.exe"
    if not out.exists():
        print(f"build succeeded but output missing at {out}", file=sys.stderr)
        return 6
    size_mb = out.stat().st_size / (1024 * 1024)
    print()
    print(f"OK → {out}  ({size_mb:.1f} MB)")
    print(f"   Unsigned. Windows SmartScreen will warn on first run.")
    return 0


if __name__ == "__main__":
    # Wrap main() so the console window stays open whether the script
    # was launched from a cmd shell, by double-click in Explorer, or
    # from a build pipeline. Without this, double-click users see a
    # console flash + close in <1 second and can't read output.
    try:
        rc = main()
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        rc = 1
    print()
    try:
        if sys.stdin.isatty():
            input("Press Enter to close...")
    except Exception:
        pass
    sys.exit(rc)
