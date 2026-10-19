"""Regression tests for test-environment isolation from the real user home."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Module import runs at collection, before the autouse fixtures change HOME.
REAL_HOME = Path.home().resolve()
USER_DIRS = (
    "HOME", "USERPROFILE", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
    "XDG_DATA_HOME", "XDG_STATE_HOME", "APPDATA", "LOCALAPPDATA",
)


def _outside_real_home(path: Path) -> bool:
    return not path.resolve().is_relative_to(REAL_HOME)


def test_user_dirs_and_git_config_are_isolated(tmp_path: Path) -> None:
    for name in USER_DIRS:
        path = Path(os.environ[name])
        assert path.is_dir(), name
        assert _outside_real_home(path), name
    # Integrations resolve the home through Path.home(), not only the env vars.
    assert _outside_real_home(Path.home())

    # `git commit` works without the developer's global identity.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"], cwd=tmp_path, check=True
    )
    origin = subprocess.run(
        ["git", "config", "--show-origin", "user.email"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout
    assert _outside_real_home(Path(origin.split("\t")[0].removeprefix("file:")))


@pytest.mark.parametrize("run", [1, 2])
def test_each_test_gets_a_fresh_home(run: int) -> None:
    marker = Path.home() / "leak-marker"
    assert not marker.exists()
    marker.write_text("x", encoding="utf-8")
