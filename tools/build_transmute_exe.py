"""Rebuild Transmute.exe (the launcher.py wrapper).

Output goes to the repo root: <repo>/Transmute.exe.

Run from anywhere:
    python tools/build_transmute_exe.py

Requires PyInstaller (`pip install pyinstaller`).

This is the small ~11 MB native EXE that finds Python on the user's
system and hands control to launcher.py. It is NOT the full installer
build (that lives in tools/build_installer.py — comes later).
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    stub = repo / "tools" / "transmute_stub.py"
    icon = repo / "resources" / "icons" / "logo.ico"
    final_exe = repo / "Transmute.exe"
    dist = repo / "dist_stub"
    work = repo / "build_stub"

    if not stub.exists():
        print(f"missing stub: {stub}", file=sys.stderr)
        return 1
    if not icon.exists():
        print(f"missing icon: {icon}", file=sys.stderr)
        return 1

    # Clean previous artifacts
    for p in (dist, work):
        if p.exists():
            shutil.rmtree(p)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "Transmute",
        "--icon", str(icon),
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--noconfirm",
        str(stub),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        print("PyInstaller failed.", file=sys.stderr)
        return rc

    built = dist / "Transmute.exe"
    if not built.exists():
        print(f"build did not produce {built}", file=sys.stderr)
        return 2

    shutil.copy2(built, final_exe)
    print(f"\nTransmute.exe -> {final_exe}")
    print(f"Size: {final_exe.stat().st_size / (1024 * 1024):.1f} MB")

    # Clean up build artifacts
    for p in (dist, work):
        if p.exists():
            shutil.rmtree(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
