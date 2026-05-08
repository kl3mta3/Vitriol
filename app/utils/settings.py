"""Persisted user settings.

JSON file at user_data_dir() / settings.json. Path delegated to paths.py
so all per-user data lives under a single root (AppData on Windows,
~/.local/share on other OSes). Loaded once, written on every change.

Keep the schema small — settings here are global app state that survives
across sessions (toggle states, last-used save folders, etc.).
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from .logger import get_logger
from .paths import settings_file

_log = get_logger()


def _settings_path() -> Path:
    return settings_file()


_DEFAULTS: dict[str, Any] = {
    "masquerade_enabled": False,
    "verify_round_trip": False,
    # --- auto-update (added v1.1.1) ---
    # Whether the app checks GitHub Releases for newer versions on launch.
    # User-toggleable in Preferences. Manual "Check for updates..." in the
    # Help menu still works regardless of this flag.
    "auto_check_updates": True,
    # Unix timestamp of the last successful check (any outcome — newer,
    # not-newer, error). Used to throttle to once per 24h so we don't
    # hammer the GitHub API on apps that get launched dozens of times a
    # day.
    "last_update_check": 0,
    # Version string the user clicked "Skip this version" on. While this
    # equals the latest available version, the banner stays hidden. Once
    # a newer version comes out, the comparison naturally re-enables it.
    "skip_version": "",
}


_loaded: dict[str, Any] | None = None


def load() -> dict[str, Any]:
    global _loaded
    if _loaded is not None:
        return _loaded
    out = dict(_DEFAULTS)
    p = _settings_path()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Merge: known keys only, preserve type
                for k, default_v in _DEFAULTS.items():
                    v = data.get(k, default_v)
                    if isinstance(default_v, bool) and not isinstance(v, bool):
                        v = bool(v)
                    out[k] = v
        except (OSError, ValueError) as e:
            _log.warning("could not read settings: %s", e)
    _loaded = out
    return out


def save() -> None:
    if _loaded is None:
        return
    p = _settings_path()
    try:
        p.write_text(json.dumps(_loaded, indent=2), encoding="utf-8")
    except OSError as e:
        _log.warning("could not write settings: %s", e)


def get(key: str) -> Any:
    return load().get(key, _DEFAULTS.get(key))


def set(key: str, value: Any) -> None:
    s = load()
    s[key] = value
    save()
