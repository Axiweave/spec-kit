"""Shared fixtures for the personal config CLI tests."""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("SPECIFY_", "SPECKIT_", "PI_", "OMP_")):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cwd = tmp_path / "not-a-project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    base = tmp_path / ("roaming" if os.name == "nt" else "config")
    return base / "specify/config.json"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
