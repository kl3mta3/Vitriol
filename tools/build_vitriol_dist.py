"""Build the self-contained Vitriol distribution folder.

Output: dist/Vitriol/  (contains Vitriol.exe + _internal/ + bundled assets)

This is the build used for:
  - The portable ZIP (zip up dist/Vitriol/, ship it).
  - The Inno Setup installer (point its [Files] section at dist/Vitriol/*).

End users running the resulting Vitriol.exe do NOT need Python on
their system — the interpreter and every required library are bundled
inside _internal/. Only FFmpeg, Assimp, and TTF fonts are still fetched
on first run (too big to bundle).

Run from anywhere:
    python tools/build_vitriol_dist.py

Requires PyInstaller (`pip install pyinstaller`).
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    spec = repo / "tools" / "Vitriol.spec"
    dist = repo / "dist"
    work = repo / "build"

    if not spec.exists():
        print(f"missing spec: {spec}", file=sys.stderr)
        return 1

    # Clean previous build artifacts so a stale layout can't sneak in.
    for p in (dist / "Vitriol", work / "Vitriol"):
        if p.exists():
            shutil.rmtree(p)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", str(dist),
        "--workpath", str(work),
        str(spec),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(repo))
    if rc != 0:
        print("PyInstaller build failed.", file=sys.stderr)
        return rc

    out = dist / "Vitriol"
    exe = out / "Vitriol.exe"
    if not exe.exists():
        print(f"build did not produce {exe}", file=sys.stderr)
        return 2

    # Sign Vitriol.exe BEFORE reporting size or chaining into the
    # installer build. The Authenticode block changes the file's bytes
    # and therefore its size + SHA-256 — the installer must bundle the
    # signed copy, not the unsigned one. If AzTS isn't configured,
    # sign.sign_file prints a clear notice and returns False; we
    # continue with an unsigned exe so dev builds still complete.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sign import sign_file  # noqa: E402

    sign_file(exe)

    # Report the total size so we have a sense of the ZIP / installer payload.
    # Re-stat AFTER signing so the reported size matches the artifact
    # that ships (Authenticode adds ~10 KB).
    total_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"\nBuild complete: {out}")
    print(f"  Vitriol.exe: {exe.stat().st_size / (1024 * 1024):.1f} MB")
    print(f"  Total folder:  {total_bytes / (1024 * 1024):.1f} MB")
    print(f"\nNext steps:")
    print(f"  Portable ZIP:  zip up the Vitriol/ folder.")
    print(f"  Installer:     run tools/build_installer.py (later).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
