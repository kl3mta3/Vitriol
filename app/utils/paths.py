"""Path utilities for an installer-friendly layout.

Three logical roots:

  app_root()        — where the executable / main.py lives. READ-ONLY when
                      the app is installed under Program Files. Holds the
                      bundled, immutable assets (SVGs, icons, theme.qss).

  user_data_dir()   — %LOCALAPPDATA%/Vitriol on Windows; equivalent
                      XDG_DATA_HOME path on Linux/macOS. Holds writable,
                      auto-fetched assets (FFmpeg, Assimp, fonts, hw cache)
                      plus settings + logs. Survives app updates cleanly.

  docs_dir()        — %USERPROFILE%/Documents/Vitriol on Windows; ~/Documents
                      equivalent elsewhere. Holds conversion OUTPUT — the
                      one place the end-user expects to find their files.

Environment overrides (set by launcher.py so its subprocess agrees):
  UC_BIN_DIR, UC_HW_CACHE, UC_RESOURCES_DIR, UC_USER_DATA_DIR, UC_DOCS_DIR.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

CATEGORY_TEXT = "Text"
CATEGORY_AUDIO = "Audio"
CATEGORY_VIDEO = "Video"
CATEGORY_IMAGES = "Images"
CATEGORY_MODELS = "Models"
ALL_CATEGORIES = (CATEGORY_TEXT, CATEGORY_AUDIO, CATEGORY_VIDEO, CATEGORY_IMAGES, CATEGORY_MODELS)


# ---------------------------------------------------------------------------
# Roots
# ---------------------------------------------------------------------------

def app_root() -> Path:
    """Directory where the app's bundled, read-only assets live.

    Resolution by mode:
      - Dev (running `python launcher.py` from the repo): the repo root.
      - PyInstaller onefile (sys.frozen + sys._MEIPASS = temp extract dir):
        the temp extract dir, which holds main.py / theme.qss / resources/.
      - PyInstaller onedir (sys.frozen + sys._MEIPASS = `<exe-dir>/_internal`):
        the `_internal` folder next to Vitriol.exe, where datas are bundled.

    Always read-only — never write here.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller exposes the bundle's data root as sys._MEIPASS for
        # both onefile (temp extract) and onedir (_internal/) modes. This
        # is where the spec's `datas = [...]` entries land — main.py,
        # theme.qss, and the resources/ tree all live here, NOT next to
        # sys.executable.
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        # Fallback if _MEIPASS isn't set for some reason — older PyInstaller,
        # or a non-PyInstaller freezer that sets sys.frozen.
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def user_data_dir() -> Path:
    """Per-user writable dir for everything the app generates or downloads.
    Auto-created if missing. Honors UC_USER_DATA_DIR override."""
    env = os.environ.get("UC_USER_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / "Vitriol"
    p.mkdir(parents=True, exist_ok=True)
    return p


def docs_dir() -> Path:
    """User's Documents/Vitriol folder — default destination for converted
    output. Auto-created. Honors UC_DOCS_DIR override (used by tests)."""
    env = os.environ.get("UC_DOCS_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if os.name == "nt":
        # Resolve "My Documents" via USERPROFILE — works for OneDrive-redirected
        # Documents too because OneDrive updates the path Windows uses.
        userprofile = os.environ.get("USERPROFILE") or str(Path.home())
        base = Path(userprofile) / "Documents"
    else:
        base = Path.home() / "Documents"
    p = base / "Vitriol"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Per-purpose directories
# ---------------------------------------------------------------------------

def bin_dir() -> Path:
    """Where FFmpeg / Assimp DLL live. Always under user_data_dir() so the
    same code path works for portable (zip) AND installed (Program Files)
    deployments. Honors UC_BIN_DIR override for cases where someone wants
    to point at a system-wide install."""
    env = os.environ.get("UC_BIN_DIR")
    if env:
        return Path(env)
    p = user_data_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def hw_encoder_cache() -> Path:
    env = os.environ.get("UC_HW_CACHE")
    if env:
        return Path(env)
    return bin_dir() / "hw_encoders.json"


# ---------------------------------------------------------------------------
# Binary discovery — single source of truth for FFmpeg/Assimp lookup.
# Used by both launcher.py (early, before app starts) and runtime modules
# (audio_video, masquerade, model_handler).
# ---------------------------------------------------------------------------

def _repo_bin_dir() -> "Path | None":
    """Repo-relative bin/ folder, or None when running from a PyInstaller
    bundle (where __file__ resolves into _internal/ and the lookup would
    be useless)."""
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        return None
    return Path(__file__).resolve().parents[2] / "bin"


def find_ffmpeg() -> "Path | None":
    """Locate ffmpeg in this order:
      1. UC_BIN_DIR (set by launcher to the actual found-ffmpeg parent)
      2. bin_dir() (= UC_BIN_DIR or %LOCALAPPDATA%/Vitriol/bin)
      3. Repo-relative bin/ (covers `python -m app.main` without launcher)
      4. PATH (system-wide install)
    Returns None if nothing found.
    """
    import shutil
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    dirs = [bin_dir()]
    repo = _repo_bin_dir()
    if repo is not None:
        dirs.append(repo)
    for d in dirs:
        c = d / name
        if c.exists():
            return c
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_ffprobe() -> "Path | None":
    import shutil
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    dirs = [bin_dir()]
    repo = _repo_bin_dir()
    if repo is not None:
        dirs.append(repo)
    for d in dirs:
        c = d / name
        if c.exists():
            return c
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def find_pandoc() -> "Path | None":
    """Locate the pandoc binary. Same lookup order as ffmpeg/assimp.
    Pandoc is auto-fetched by launcher.py into bin_dir() on first launch."""
    import shutil
    name = "pandoc.exe" if os.name == "nt" else "pandoc"
    dirs = [bin_dir()]
    repo = _repo_bin_dir()
    if repo is not None:
        dirs.append(repo)
    for d in dirs:
        c = d / name
        if c.exists():
            return c
    found = shutil.which("pandoc")
    return Path(found) if found else None


def find_assimp() -> "Path | None":
    """Locate the Assimp DLL/SO. Honors UC_ASSIMP_DIR (set by launcher)
    plus the legacy ASSIMP_DLL / ASSIMP_PATH env overrides."""
    import shutil
    names = ("assimp-vc143-mt.dll", "assimp.dll", "libassimp.dll",
             "libassimp.so", "libassimp.dylib")
    dirs = []
    assimp_dir = os.environ.get("UC_ASSIMP_DIR")
    if assimp_dir:
        dirs.append(Path(assimp_dir))
    dirs.append(bin_dir())
    repo = _repo_bin_dir()
    if repo is not None:
        dirs.append(repo)
    for d in dirs:
        for name in names:
            c = d / name
            if c.exists():
                return c
    env = os.environ.get("ASSIMP_DLL") or os.environ.get("ASSIMP_PATH")
    if env and Path(env).exists():
        return Path(env)
    found = shutil.which("assimp-vc143-mt") or shutil.which("assimp")
    return Path(found) if found else None


def wheels_dir() -> Path:
    """Optional offline pip wheel cache. Read-only at runtime — beside the
    app for portable installs, ignored if absent."""
    return app_root() / "wheels"


def resources_dir() -> Path:
    """Bundled, immutable assets (SVGs, icons, theme.qss). Read-only.
    UC_RESOURCES_DIR override is honored for tests / unusual layouts."""
    env = os.environ.get("UC_RESOURCES_DIR")
    if env:
        p = Path(env)
        if p.exists():
            return p
    return app_root() / "resources"


def font_dir() -> Path:
    """Auto-fetched fonts (DejaVu, Cinzel). Lives under user_data_dir() so
    the launcher can write here regardless of where the app is installed."""
    p = user_data_dir() / "fonts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def find_font(filename: str) -> Path | None:
    """Return the on-disk path for a bundled font file. Checks user font_dir
    first (where the launcher writes auto-fetched files), then the bundled
    resources/ tree as a fallback. Returns None if not found anywhere."""
    candidates = (
        font_dir() / filename,
        resources_dir() / "fonts" / filename,
        resources_dir() / filename,    # legacy: DejaVu was at resources root
    )
    for c in candidates:
        if c.exists():
            return c
    return None


def output_dir(category: str) -> Path:
    """Default output dir for a category. Documents/Vitriol/<Category>.
    Auto-created. Falls back to user_data_dir()/output/<Category> only if
    Documents is somehow not writable (extremely rare — locked-down kiosk)."""
    if category not in ALL_CATEGORIES:
        category = CATEGORY_TEXT
    primary = docs_dir() / category
    if _is_writable(primary):
        return primary
    fallback = user_data_dir() / "output" / category
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def log_file() -> Path:
    base = user_data_dir() / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "vitriol.log"


def settings_file() -> Path:
    """Persisted user settings — under user_data_dir() so it survives reinstalls."""
    return user_data_dir() / "settings.json"


# ---------------------------------------------------------------------------
# First-run scaffold
# ---------------------------------------------------------------------------

def ensure_user_dirs() -> None:
    """Create every directory the app needs to write to. Idempotent — safe
    to call on every launch. Called by launcher.py before main.py spawns."""
    user_data_dir()             # AppData root
    bin_dir()
    font_dir()
    log_file().parent
    docs_dir()
    for cat in ALL_CATEGORIES:
        try:
            (docs_dir() / cat).mkdir(parents=True, exist_ok=True)
        except OSError:
            # Documents not writable — fall back will kick in lazily.
            pass


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def unique_path(target: Path) -> Path:
    """If target exists, append ' (1)', ' (2)', ... before the suffix until unique."""
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    i = 1
    while True:
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
        i += 1
