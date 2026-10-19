"""Shared fixtures for the personal config CLI tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("SPECIFY_", "SPECKIT_", "PI_", "OMP_")):
            monkeypatch.delenv(key)
    cwd = tmp_path / "not-a-project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    base = os.environ["APPDATA" if os.name == "nt" else "XDG_CONFIG_HOME"]
    return Path(base) / "specify/config.json"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
