"""Persisted user settings.

JSON file at %LOCALAPPDATA%/UniversalConverter/settings.json (Windows) or
~/.local/share/UniversalConverter/settings.json (other). Loaded once, written
on every change.

Keep the schema small — settings here are global app state that survives
across sessions (toggle states, last-used save folders, etc.).
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

from .logger import get_logger

_log = get_logger()


def _settings_path() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / "UniversalConverter"
    p.mkdir(parents=True, exist_ok=True)
    return p / "settings.json"


_DEFAULTS: dict[str, Any] = {
    "masquerade_enabled": False,
    "verify_round_trip": False,
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
