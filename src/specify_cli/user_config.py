"""Personal defaults used only when setting up a new project."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .workspace import atomic_json, confined, read_json

_KEYS = frozenset({"storage_root", "feature_numbering", "feature_selection", "integration", "script"})


def config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    root = root.expanduser().resolve()
    path = root / "specify/config.json"
    confined(root, path)
    for parent in (root, path.parent):
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Configuration path is not a directory: {parent}")
    return path


def _read() -> dict[str, Any]:
    path = config_path()
    if not path.exists() and not path.is_symlink():
        return {}
    return read_json(path)


def _validate_key(key: str) -> None:
    if not isinstance(key, str) or key not in _KEYS:
        raise ValueError(f"Unknown default: {key}. Choose {', '.join(sorted(_KEYS))}.")


def _validate_value(key: str, value: str) -> None:
    _validate_key(key)
    if not isinstance(value, str):
        raise ValueError(f"Default {key} must be a string.")
    if not value:
        return
    if key == "feature_numbering" and value not in ("sequential", "timestamp"):
        raise ValueError("Default feature_numbering must be sequential or timestamp.")
    if key == "feature_selection" and value not in ("context", "automatic"):
        raise ValueError("Default feature_selection must be context or automatic.")
    if key == "script" and value not in ("sh", "ps", "py"):
        raise ValueError("Default script must be sh, ps, or py.")
    if key == "integration":
        from .integrations import get_integration

        if get_integration(value) is None:
            raise ValueError(f"Default integration is not registered: {value}")
    if key == "storage_root" and "\0" in value:
        raise ValueError("Default storage_root must not contain a null character.")


def load_defaults() -> dict[str, Any]:
    """Read raw defaults and validate known fields without changing the file."""
    data = _read()
    for key in _KEYS & data.keys():
        _validate_value(key, data[key])
    return data


def get_default(key: str) -> str:
    _validate_key(key)
    return load_defaults().get(key, "")


def set_default(key: str, value: str) -> None:
    _validate_value(key, value)
    data = _read()
    data[key] = value
    atomic_json(config_path(), data)


def clear_default(key: str) -> None:
    _validate_key(key)
    data = _read()
    if key in data:
        del data[key]
        atomic_json(config_path(), data)
