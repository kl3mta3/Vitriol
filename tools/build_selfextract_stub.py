"""Build resources/stubs/selfextract_stub.exe via PyInstaller.

This stub is the basis for every Stone-mode .exe Vitriol generates.
At conversion time, Vitriol copies the bytes of this .exe + appends a
magic marker + the .py runtime source. At run time, the stub finds the
magic, decodes the appended source, and exec's it.

Run once during Vitriol development / build pipeline:
    python tools/build_selfextract_stub.py

Output: resources/stubs/selfextract_stub.exe (~12 MB; PyInstaller
includes the Python interpreter so end users don't need Python).

Re-run when:
  - the stub source (`tools/selfextract_stub.py`) changes
  - you bump Python versions and want to rebuild against the new one
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    stub_src = repo / "tools" / "selfextract_stub.py"
    if not stub_src.exists():
        print(f"missing stub source: {stub_src}", file=sys.stderr)
        return 2

    output_dir = repo / "resources" / "stubs"
    output_dir.mkdir(parents=True, exist_ok=True)

    work = repo / "build" / "selfextract_stub"
    spec_dir = work
    work.mkdir(parents=True, exist_ok=True)

    # --onefile: produce a single self-contained .exe.
    # --console: keep stdout/stdin attached so password prompt + animation work.
    # --name selfextract_stub: produces selfextract_stub.exe.
    # --noconfirm: don't prompt before overwriting existing dist.
    # `--collect-all cryptography` bundles the `cryptography` package +
    # its native dependencies (cffi, OpenSSL backend libs) into the .exe
    # so the encrypted-mode runtime — which lives in the appended
    # payload, NOT this stub — can `import cryptography` at execution
    # time. Without this, encrypted .exe outputs fail at runtime when
    # the inner runtime tries to AES-CTR-decrypt the payload.
    # `--hidden-import` lines cover the specific submodules that the
    # appended payload references; PyInstaller's static analysis can't
    # see imports inside an exec'd string, so we list them explicitly.
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", "selfextract_stub",
        "--distpath", str(output_dir),
        "--workpath", str(work),
        "--specpath", str(spec_dir),
        "--noconfirm",
        "--collect-all", "cryptography",
        "--hidden-import", "cryptography.hazmat.primitives.ciphers",
        "--hidden-import", "cryptography.hazmat.primitives.ciphers.algorithms",
        "--hidden-import", "cryptography.hazmat.primitives.ciphers.modes",
        "--hidden-import", "cryptography.hazmat.backends",
        "--hidden-import", "cryptography.hazmat.backends.openssl",
        str(stub_src),
    ]
    print("Running:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(repo))
    if rc != 0:
        print(f"PyInstaller failed (exit {rc}).", file=sys.stderr)
        return rc

    out = output_dir / "selfextract_stub.exe"
    if not out.exists():
        print(f"Build succeeded but output missing at {out}", file=sys.stderr)
        return 3
    size_mb = out.stat().st_size / (1024 * 1024)
    print()
    print(f"OK -> {out}  ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
