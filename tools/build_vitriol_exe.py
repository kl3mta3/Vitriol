"""Rebuild Vitriol.exe (the launcher.py wrapper).

Output goes to the repo root: <repo>/Vitriol.exe.

Run from anywhere:
    python tools/build_vitriol_exe.py

Requires PyInstaller (`pip install pyinstaller`).

This is the small ~11 MB native EXE that finds Python on the user's
system and hands control to launcher.py. It is NOT the full installer
build (that lives in tools/build_installer.py — comes later).

After PyInstaller produces the binary, this script invokes the same
Azure Trusted Signing pipeline used by build_vitriol_dist.py and
build_installer.py, so the repo-root Vitriol.exe gets the same
"Equivalent Exchange" publisher signature as the shipped installer.
That keeps the trust signal consistent between users who clone the
repo and double-click vs. users who download the installer.

If the AzTS env vars (VITRIOL_AZTS_*) aren't set, signing is skipped
with a clear notice and the build still produces an (unsigned) exe —
so dev rebuilds without AzTS access still work.
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    stub = repo / "tools" / "vitriol_stub.py"
    icon = repo / "resources" / "icons" / "logo.ico"
    final_exe = repo / "Vitriol.exe"
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
        "--name", "Vitriol",
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

    built = dist / "Vitriol.exe"
    if not built.exists():
        print(f"build did not produce {built}", file=sys.stderr)
        return 2

    shutil.copy2(built, final_exe)

    # Sign the final exe in place via Azure Trusted Signing. Same
    # sign.py that drives build_vitriol_dist.py + build_installer.py —
    # one config, three artifacts. If AzTS isn't configured, sign_file
    # prints a notice and returns False; we continue with the unsigned
    # exe so dev rebuilds without AzTS access still work.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sign import sign_file  # noqa: E402

    signed = sign_file(final_exe)

    # Re-stat AFTER signing — Authenticode appends ~10 KB so the
    # reported size matches what ends up committed.
    print(f"\nVitriol.exe -> {final_exe}")
    print(f"Size: {final_exe.stat().st_size / (1024 * 1024):.1f} MB")
    if signed:
        print("   Signed via Azure Trusted Signing.")
    else:
        print("   UNSIGNED. Cloned-repo users will see SmartScreen warnings.")
        print("   Set VITRIOL_AZTS_* env vars to enable signing.")

    # Clean up build artifacts
    for p in (dist, work):
        if p.exists():
            shutil.rmtree(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
