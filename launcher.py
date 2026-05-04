"""Universal Converter — launcher.

Designed to be packaged as a small PyInstaller --onedir bundle (~30 MB) with
its own embedded Python, separate from the main app. End users only ever run
launcher.exe; they never need Python installed on their system.

Philosophy: **fully autonomous**. The launcher does NOT prompt the user
about anything routine. On every launch it ensures the following are present
and downloads them silently if missing:

  1. Python packages main.py needs (PySide6, Pillow, striprtf).
     Installed from ./wheels/ first, falling back to PyPI.
  2. ./bin/ffmpeg(.exe) and ./bin/ffprobe(.exe) — fetched from
     gyan.dev's official Windows builds.
  3. ./bin/assimp-vc143-mt.dll — fetched from the Assimp GitHub release.
  4. ./resources/DejaVuSans.ttf — fetched from the DejaVu GitHub release
     so PDF output gets full Latin Extended + Cyrillic + Greek out of the box.
  5. Hardware encoder probe (cached to bin/hw_encoders.json so the audio/video
     handler can silently use NVENC/QSV/AMF/VideoToolbox when available).

After everything is in place it spawns main.py via sys.executable.

Trust model: downloads come from the official upstream HTTPS endpoints. SHA-256
verification is supported (set _*_SHA constants below to lock a release) but
is OFF by default so the launcher is never blocked on a hash mismatch when
upstream re-releases. Logged warning makes the trust assumption explicit.

This module deliberately uses ONLY stdlib (incl. tkinter for the progress
window) so it runs cleanly before PySide6 has been installed.
"""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    _HAS_TK = True
except Exception:
    _HAS_TK = False


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


ROOT = _app_root()
BIN = ROOT / "bin"
WHEELS = ROOT / "wheels"
RESOURCES = ROOT / "resources"
HW_CACHE = BIN / "hw_encoders.json"


def _is_writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


# Read-only install fallback for ./bin/ and ./resources/.
if not _is_writable(BIN):
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    BIN = Path(base) / "UniversalConverter" / "bin"
    BIN.mkdir(parents=True, exist_ok=True)
    HW_CACHE = BIN / "hw_encoders.json"
if not _is_writable(RESOURCES):
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    RESOURCES = Path(base) / "UniversalConverter" / "resources"
    RESOURCES.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Required Python packages
# ---------------------------------------------------------------------------

REQUIRED_PY = [
    ("PySide6", "PySide6>=6.6"),
    ("PIL", "Pillow>=10.0"),
    ("striprtf", "striprtf>=0.0.26"),
]


def _missing_python_packages() -> list[tuple[str, str]]:
    missing = []
    for import_name, pip_spec in REQUIRED_PY:
        try:
            __import__(import_name)
        except ImportError:
            missing.append((import_name, pip_spec))
    return missing


def _install_packages(packages: list[str], log) -> bool:
    if not packages:
        return True
    py = sys.executable
    log(f"Installing {len(packages)} Python package(s)…")
    if WHEELS.exists() and any(WHEELS.glob("*.whl")):
        log("  trying ./wheels/ first")
        rc = subprocess.call(
            [py, "-m", "pip", "install", "--no-index", "--find-links", str(WHEELS), *packages],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if rc == 0:
            log("  installed from local wheels")
            return True
        log("  local wheels insufficient; falling back to PyPI")
    log("  installing from PyPI")
    rc = subprocess.call([py, "-m", "pip", "install", *packages])
    return rc == 0


# ---------------------------------------------------------------------------
# Native binaries + bundled font
# ---------------------------------------------------------------------------

# Optional integrity locks. Set when you cut a release; left empty = trust HTTPS.
FFMPEG_URL = "https://github.com/GyanD/codexffmpeg/releases/download/7.1/ffmpeg-7.1-essentials_build.zip"
FFMPEG_SHA = ""

# Assimp v5.x release archives don't include prebuilt Windows binaries.
# v6.0.0+ ships windows-x64-v{ver}.zip with the DLL inside.
ASSIMP_URL = "https://github.com/assimp/assimp/releases/download/v6.0.5/windows-x64-v6.0.5.zip"
ASSIMP_SHA = ""

DEJAVU_URL = "https://github.com/dejavu-fonts/dejavu-fonts/releases/download/version_2_37/dejavu-fonts-ttf-2.37.zip"
DEJAVU_SHA = ""


def _find_ffmpeg() -> Optional[Path]:
    local = BIN / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return local
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def _find_assimp() -> Optional[Path]:
    for name in ("assimp-vc143-mt.dll", "assimp.dll", "libassimp.dll", "libassimp.so", "libassimp.dylib"):
        c = BIN / name
        if c.exists():
            return c
    env = os.environ.get("ASSIMP_DLL") or os.environ.get("ASSIMP_PATH")
    if env and Path(env).exists():
        return Path(env)
    found = shutil.which("assimp-vc143-mt") or shutil.which("assimp")
    return Path(found) if found else None


def _find_dejavu() -> Optional[Path]:
    p = RESOURCES / "DejaVuSans.ttf"
    return p if p.exists() else None


def _download(url: str, dst: Path, expected_sha: str, log) -> bool:
    log(f"  downloading {url}")
    tmp = dst.with_suffix(dst.suffix + ".part")
    hasher = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "UniversalConverter/0.1"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            seen = 0
            last_pct = -1
            while True:
                buf = resp.read(64 * 1024)
                if not buf:
                    break
                out.write(buf)
                hasher.update(buf)
                seen += len(buf)
                if total > 0:
                    pct = int(seen * 100 / total)
                    if pct != last_pct and pct % 10 == 0:
                        log(f"    {pct}% ({seen // 1024} KB / {total // 1024} KB)")
                        last_pct = pct
    except urllib.error.URLError as e:
        log(f"  download failed: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    if expected_sha:
        digest = hasher.hexdigest().lower()
        if digest != expected_sha.lower():
            log(f"  checksum mismatch: got {digest}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
    else:
        log("  (no SHA-256 lock set — trusting HTTPS to upstream)")
    tmp.replace(dst)
    return True


def _install_ffmpeg(log) -> bool:
    log("FFmpeg missing — fetching")
    zip_path = BIN / "ffmpeg-download.zip"
    try:
        if not _download(FFMPEG_URL, zip_path, FFMPEG_SHA, log):
            return False
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                base = n.rsplit("/", 1)[-1].lower()
                if base in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
                    with z.open(n) as src, open(BIN / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    try:
                        os.chmod(BIN / base, 0o755)
                    except OSError:
                        pass
        log("  FFmpeg installed")
        return True
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass


def _install_assimp(log) -> bool:
    log("Assimp missing — fetching")
    zip_path = BIN / "assimp-download.zip"
    try:
        if not _download(ASSIMP_URL, zip_path, ASSIMP_SHA, log):
            return False
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                base = n.rsplit("/", 1)[-1].lower()
                if base.endswith(".dll") and "assimp" in base:
                    with z.open(n) as src, open(BIN / base, "wb") as out:
                        shutil.copyfileobj(src, out)
        log("  Assimp installed")
        return True
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass


def _install_dejavu(log) -> bool:
    log("DejaVuSans.ttf missing — fetching (gives PDF output Unicode coverage)")
    zip_path = RESOURCES / "dejavu-download.zip"
    try:
        if not _download(DEJAVU_URL, zip_path, DEJAVU_SHA, log):
            return False
        target = RESOURCES / "DejaVuSans.ttf"
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                # The zip nests as dejavu-fonts-ttf-2.37/ttf/DejaVuSans.ttf
                if n.lower().endswith("/ttf/dejavusans.ttf") or n.lower().endswith("dejavusans.ttf"):
                    with z.open(n) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                    log("  DejaVuSans.ttf installed")
                    return True
        log("  DejaVuSans.ttf not found inside downloaded archive")
        return False
    finally:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Hardware encoder probe
# ---------------------------------------------------------------------------

HW_VIDEO_CANDIDATES = {
    "h264": [
        ("h264_nvenc", "NVIDIA NVENC"),
        ("h264_qsv", "Intel QuickSync"),
        ("h264_amf", "AMD AMF"),
        ("h264_videotoolbox", "Apple VideoToolbox"),
    ],
    "hevc": [
        ("hevc_nvenc", "NVIDIA NVENC"),
        ("hevc_qsv", "Intel QuickSync"),
        ("hevc_amf", "AMD AMF"),
        ("hevc_videotoolbox", "Apple VideoToolbox"),
    ],
    "vp9": [
        ("vp9_qsv", "Intel QuickSync"),
    ],
    "av1": [
        ("av1_nvenc", "NVIDIA NVENC"),
        ("av1_qsv", "Intel QuickSync"),
        ("av1_amf", "AMD AMF"),
    ],
}


def _probe_hw_encoders(ffmpeg: Path, log) -> dict:
    log("Probing hardware encoders…")
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        out = subprocess.check_output(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            stderr=subprocess.STDOUT, text=True, timeout=30,
            creationflags=creationflags,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"  probe failed: {e}")
        return {}
    available = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            available.add(parts[1])
    chosen: dict = {}
    for target, candidates in HW_VIDEO_CANDIDATES.items():
        for enc, label in candidates:
            if enc in available:
                chosen[target] = enc
                log(f"  {target} → {enc} ({label})")
                break
    if not chosen:
        log("  no hardware encoders detected — using software fallbacks")
    return chosen


def _refresh_hw_cache(ffmpeg: Path, log) -> None:
    needs_refresh = True
    if HW_CACHE.exists():
        try:
            cache_mtime = HW_CACHE.stat().st_mtime
            ffmpeg_mtime = ffmpeg.stat().st_mtime
            if cache_mtime > ffmpeg_mtime:
                needs_refresh = False
        except OSError:
            pass
    if not needs_refresh:
        return
    encoders = _probe_hw_encoders(ffmpeg, log)
    try:
        HW_CACHE.write_text(json.dumps(encoders, indent=2), encoding="utf-8")
    except OSError as e:
        log(f"  could not write hw cache: {e}")


# ---------------------------------------------------------------------------
# Progress window (stdlib Tkinter — works before PySide6 is installed)
# ---------------------------------------------------------------------------

class _ProgressUI:
    def __init__(self) -> None:
        if not _HAS_TK:
            self.root = None
            return
        try:
            self.root = tk.Tk()
        except tk.TclError:
            self.root = None
            return
        self.root.title("Universal Converter")
        self.root.geometry("620x320")
        self.root.configure(bg="#15151f")
        title = tk.Label(self.root, text="Universal Converter — Starting up",
                         fg="#e8e8f0", bg="#15151f", font=("Segoe UI", 13, "bold"))
        title.pack(pady=(14, 8), padx=14, anchor="w")
        sub = tk.Label(self.root,
                       text="Checking dependencies and installing anything missing.",
                       fg="#9090a0", bg="#15151f", font=("Segoe UI", 9))
        sub.pack(pady=(0, 8), padx=14, anchor="w")
        self.text = tk.Text(self.root, bg="#0a0a0f", fg="#cccccc",
                            font=("Consolas", 9), bd=0, highlightthickness=0)
        self.text.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.text.configure(state="disabled")
        self.root.update()

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr)
        if self.root is None:
            return
        try:
            self.text.configure(state="normal")
            self.text.insert("end", msg + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")
            self.root.update()
        except tk.TclError:
            pass

    def close(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Top-level launch
# ---------------------------------------------------------------------------

def main() -> int:
    ui = _ProgressUI()
    try:
        # 1. Python packages — auto-install, no prompt
        missing = _missing_python_packages()
        if missing:
            specs = [spec for _, spec in missing]
            ui.log(f"Missing Python packages: {', '.join(name for name, _ in missing)}")
            _install_packages(specs, ui.log)

        # 2. FFmpeg — auto-fetch
        if _find_ffmpeg() is None:
            _install_ffmpeg(ui.log)

        # 3. Assimp — auto-fetch
        if _find_assimp() is None:
            _install_assimp(ui.log)

        # 4. DejaVuSans.ttf — auto-fetch (PDF Unicode coverage)
        if _find_dejavu() is None:
            _install_dejavu(ui.log)

        # 5. Hardware encoder probe
        ffmpeg = _find_ffmpeg()
        if ffmpeg is not None:
            _refresh_hw_cache(ffmpeg, ui.log)

        ui.log("Launching app…")
    finally:
        ui.close()

    main_py = ROOT / "main.py"
    if not main_py.exists():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            alt = Path(meipass) / "main.py"
            if alt.exists():
                main_py = alt
        if not main_py.exists():
            print(f"main.py not found at {ROOT}", file=sys.stderr)
            return 1

    env = os.environ.copy()
    env["UC_BIN_DIR"] = str(BIN)
    env["UC_HW_CACHE"] = str(HW_CACHE)
    env["UC_RESOURCES_DIR"] = str(RESOURCES)
    rc = subprocess.call([sys.executable, str(main_py)], env=env)
    return rc


if __name__ == "__main__":
    sys.exit(main())
