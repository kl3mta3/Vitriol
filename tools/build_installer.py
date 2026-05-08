"""Build the Inno Setup installer for Vitriol.

Produces `dist/VitriolSetup-<version>.exe` from the PyInstaller dist
folder at `dist/Vitriol/`.

Usage:
    python tools/build_installer.py

Prerequisites:
  - Inno Setup 6+ installed. Download: https://jrsoftware.org/isdl.php
    Default install path: `C:\\Program Files (x86)\\Inno Setup 6\\iscc.exe`.
  - `dist/Vitriol/` must already exist (built by
    `tools/build_vitriol_dist.py`). The dist-builder signs Vitriol.exe
    via Azure Trusted Signing before this script bundles it; this
    script then signs the produced VitriolSetup-*.exe at the end.
  - Azure Trusted Signing env vars (VITRIOL_AZTS_*) — see tools/sign.py.
    If unset, both this script and the dist-builder skip signing with
    a printed warning, leaving unsigned artifacts.
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


def _check_inner_exe_signed(repo: Path, signtool: Path | None) -> None:
    """Warn if the bundled Vitriol.exe isn't signed.

    We do NOT abort — the user might be intentionally producing an
    unsigned dev installer. But the warning is loud because shipping
    a signed installer that bundles an UNSIGNED inner exe is a
    confusing user experience: the installer prompts a verified
    publisher, then the installed app prompts "Unknown publisher" on
    first launch. The fix is always to re-run the dist build after
    setting up AzTS, then re-run this script."""
    if signtool is None:
        return
    inner = repo / "dist" / "Vitriol" / "Vitriol.exe"
    rc = subprocess.call(
        [str(signtool), "verify", "/pa", "/q", str(inner)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if rc != 0:
        print()
        print("=" * 64)
        print("WARNING: dist/Vitriol/Vitriol.exe is NOT signed.")
        print()
        print("The installer will be signed (if AzTS is configured), but")
        print("the bundled app inside will not. End users will see the")
        print("verified publisher on the installer prompt, then 'Unknown")
        print("publisher' the first time they launch the installed app.")
        print()
        print("To fix: re-run `python tools/build_vitriol_dist.py` with")
        print("the VITRIOL_AZTS_* env vars set, then re-run this script.")
        print("=" * 64)
        print()


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

    # Sign the produced installer via Azure Trusted Signing. This
    # MUST happen after iscc finishes — Inno's compressor would
    # blow away any signature applied earlier. Conversely, the inner
    # Vitriol.exe needs to be signed BEFORE Inno bundles it (handled
    # by tools/build_vitriol_dist.py). If AzTS isn't configured,
    # sign_file prints a clear notice and returns False; we fall
    # through to the unsigned-install path below.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sign import sign_file  # noqa: E402

    # Pre-flight sanity check: warn loudly if the inner exe isn't
    # signed, because the installer-only-signed case produces a
    # confusing UAC vs. first-launch publisher mismatch.
    _check_inner_exe_signed(repo, _find_signtool_for_check())

    signed = sign_file(out)

    # Re-stat AFTER signing — Authenticode appends ~10 KB so the
    # reported size matches the file users will download.
    size_mb = out.stat().st_size / (1024 * 1024)
    print()
    print(f"OK → {out}  ({size_mb:.1f} MB)")
    if signed:
        print(f"   Signed via Azure Trusted Signing.")
        print(f"   SHA-256 in hand: run `Get-FileHash` on the file above")
        print(f"   and paste the result into RELEASE_NOTES_INSTALLER.md.")
    else:
        print(f"   UNSIGNED. SmartScreen will warn end users on first run.")
        print(f"   Set VITRIOL_AZTS_* env vars to enable signing.")
    return 0


def _find_signtool_for_check() -> Path | None:
    """Re-use sign.py's signtool discovery for the pre-flight sanity
    check. Lives here as a thin pass-through so the import stays
    tucked inside main() rather than at module-import time (sign.py
    imports tempfile/json which we don't want to pay for if iscc
    fails early)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sign import _find_signtool  # noqa: E402
    return _find_signtool()


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
