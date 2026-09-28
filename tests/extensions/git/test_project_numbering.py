"""Project numbering and Git branches share a mode, not feature selection."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from specify_cli.extensions import ExtensionManager
from tests.conftest import requires_bash
from tests.extensions.git.test_git_extension import EXT_DIR, _init_git, _write_config
from tests.parity_helpers import clean_env, run
from tests.specify_cli.test_external_feature_scripts import (
    _make_project,
    _select,
    _snapshot,
    _write_json,
)

PWSH = shutil.which("pwsh")
PS_OPTIONS = {
    "--json": "-Json",
    "--short-name": "-ShortName",
    "--number": "-Number",
    "--timestamp": "-Timestamp",
    "--paths-only": "-PathsOnly",
}


@pytest.fixture(params=[
    pytest.param("bash", marks=requires_bash),
    "python",
    pytest.param("powershell", marks=pytest.mark.skipif(not PWSH, reason="no PowerShell available")),
])
def variant(request):
    return request.param


@pytest.fixture
def script_env(tmp_path, monkeypatch):
    env = clean_env()
    for key in list(env):
        if key.startswith(("SPECKIT_", "GIT_", "OMP_", "PI_", "PYTHON")):
            env.pop(key)
    home = tmp_path / "home"
    home.mkdir()
    env.update({
        "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "APPDATA": str(tmp_path / "config"),
        "LOCALAPPDATA": str(tmp_path / "data"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    for key in set(os.environ) - set(env):
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture(params=[False, True], ids=["local", "external"])
def project(tmp_path, request, script_env):
    project = _make_project(tmp_path, request.param)
    repo, workspace, _ = project
    _init_git(repo)
    _select(project, "specs/099-saved-feature")
    (workspace / "specs/20990101-120000-timestamp-feature").mkdir()
    result = run(["git", "branch", "012-existing-branch"], repo, env=script_env)
    assert result.returncode == 0, result.stderr
    manager = ExtensionManager(repo)
    manager.install_from_directory(EXT_DIR, "0.5.0", register_commands=False)
    return project


def _invoke(variant, workspace, repo, env, script, *args, extension=True):
    base = workspace / ".specify"
    if extension:
        base = base / "extensions/git"
    base = base / "scripts" / variant
    if variant == "bash":
        command = ["bash", str(base / f"{script}.sh")]
    elif variant == "python":
        command = [sys.executable, str(base / f"{script.replace('-', '_')}.py")]
    else:
        command = [PWSH, "-NoProfile", "-File", str(base / f"{script}.ps1")]
        args = tuple(PS_OPTIONS.get(arg, arg) for arg in args)
    result = run([*command, *args], repo, env=env, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _set_modes(project, project_mode, branch_mode=None):
    _, workspace, _ = project
    _write_json(workspace / ".specify/init-options.json", {
        "feature_numbering": project_mode,
        "integration": "copilot",
        "script": "sh",
    })
    if branch_mode is not None:
        _write_config(workspace, f"branch_numbering: {branch_mode}\n")


def _create_branch(project, variant, env, *args, exact_name=None):
    repo, workspace, record = project
    before = {
        "assets": _snapshot(workspace / ".specify"),
        "features": _snapshot(workspace / "specs"),
        "locator": _snapshot(repo / ".specify"),
        "record": record.read_bytes() if record else None,
    }
    head = run(["git", "rev-parse", "HEAD"], repo, env=env)
    assert head.returncode == 0, head.stderr
    branch_env = {**env, **({"GIT_BRANCH_NAME": exact_name} if exact_name else {})}
    earliest = datetime.now().replace(microsecond=0) - timedelta(seconds=1)
    output = _invoke(
        variant, workspace, repo, branch_env, "create-new-feature-branch",
        "--json", "--short-name", "new-feature", *args, "Add project numbering",
    )
    latest = datetime.now().replace(microsecond=0) + timedelta(seconds=1)
    branch = run(["git", "branch", "--show-current"], repo, env=env)
    assert branch.returncode == 0, branch.stderr
    assert branch.stdout.strip() == output["BRANCH_NAME"]
    after_head = run(["git", "rev-parse", "HEAD"], repo, env=env)
    assert after_head.returncode == 0, after_head.stderr
    assert after_head.stdout == head.stdout
    assert _snapshot(workspace / ".specify") == before["assets"]
    assert _snapshot(workspace / "specs") == before["features"]
    assert _snapshot(repo / ".specify") == before["locator"]
    if record:
        assert record.read_bytes() == before["record"]
        assert not (repo / "specs").exists()
        assert not (workspace / ".git").exists()
    paths = _invoke(
        variant, workspace, repo, env, "check-prerequisites",
        "--json", "--paths-only", extension=False,
    )
    assert Path(paths["REPO_ROOT"]) == repo
    assert Path(paths["FEATURE_DIR"]) == workspace / "specs/099-saved-feature"
    return output, earliest, latest


def _assert_number(result, expected, exact_name=None):
    output, earliest, latest = result
    if expected == "timestamp":
        timestamp = datetime.strptime(output["FEATURE_NUM"], "%Y%m%d-%H%M%S")
        assert earliest <= timestamp <= latest
        assert output["FEATURE_NUM"] == timestamp.strftime("%Y%m%d-%H%M%S")
    else:
        assert output["FEATURE_NUM"] == expected
    assert output["BRANCH_NAME"] == (exact_name or f"{output['FEATURE_NUM']}-new-feature")


@pytest.mark.parametrize("mode", ["sequential", "timestamp"])
@pytest.mark.parametrize("generated_config", [False, True], ids=["no-config", "generated-config"])
def test_project_mode_drives_default_branch_without_changing_selection(
    project, variant, script_env, mode, generated_config,
):
    repo, workspace, _ = project
    (workspace / ".specify/extensions/git/git-config.yml").unlink()
    _set_modes(project, mode)
    if generated_config:
        deployed, _, failed = ExtensionManager(repo).scaffold_config("git")
        assert "git-config.yml" in deployed
        assert not failed
    defaults = Path(script_env["XDG_CONFIG_HOME"]) / "specify/config.json"
    _write_json(defaults, {
        "feature_numbering": "timestamp" if mode == "sequential" else "sequential",
        "storage_root": str(repo.parent / "another workspace"),
    })
    before_defaults = defaults.read_bytes()
    result = _create_branch(project, variant, script_env)
    _assert_number(result, "100" if mode == "sequential" else "timestamp")
    assert defaults.read_bytes() == before_defaults


@pytest.mark.parametrize("project_mode,branch_mode,expected", [
    ("timestamp", "sequential", "100"),
    ("sequential", "timestamp", "timestamp"),
])
def test_intentional_branch_mode_overrides_project_mode(
    project, variant, script_env, project_mode, branch_mode, expected,
):
    _set_modes(project, project_mode, branch_mode)
    _assert_number(_create_branch(project, variant, script_env), expected)


@pytest.mark.parametrize("project_mode,branch_mode,args,exact_name,expected", [
    pytest.param("timestamp", None, ("--number", "0"), None, "000", id="zero-over-project"),
    pytest.param("timestamp", "timestamp", ("--number", "42"), None, "042", id="number-over-modes"),
    pytest.param("sequential", "sequential", ("--timestamp",), None, "timestamp", id="timestamp-over-modes"),
    pytest.param("timestamp", "timestamp", ("--timestamp", "--number", "42"), None, "timestamp", id="timestamp-over-number"),
    pytest.param("timestamp", "timestamp", ("--timestamp", "--number", "42"), "team/007-exact-name", "007", id="exact-name-over-all"),
    pytest.param("sequential", "sequential", (), "team/20260927-120000-exact-name", "20260927-120000", id="exact-timestamp-name"),
])
def test_per_feature_choices_override_modes_without_changing_saved_selection(
    project, variant, script_env, project_mode, branch_mode, args, exact_name, expected,
):
    _set_modes(project, project_mode, branch_mode)
    result = _create_branch(project, variant, script_env, *args, exact_name=exact_name)
    _assert_number(result, expected, exact_name)


def test_sequential_number_uses_repository_branches_after_workspace_features(
    project, variant, script_env,
):
    _set_modes(project, "sequential")
    _assert_number(_create_branch(project, variant, script_env), "100")
    _assert_number(_create_branch(project, variant, script_env), "101")


@pytest.mark.parametrize("invalid_source", ["project", "extension"])
def test_invalid_numbering_preserves_repository_and_workspace(
    project, variant, script_env, invalid_source,
):
    repo, workspace, record = project
    _set_modes(
        project,
        "random" if invalid_source == "project" else "sequential",
        "random" if invalid_source == "extension" else None,
    )
    before = (_snapshot(repo), _snapshot(workspace), record.read_bytes() if record else None)
    scripts = workspace / ".specify/extensions/git/scripts"
    commands = {
        "bash": ["bash", str(scripts / "bash/create-new-feature-branch.sh")],
        "python": [sys.executable, str(scripts / "python/create_new_feature_branch.py")],
        "powershell": [PWSH, "-NoProfile", "-File", str(scripts / "powershell/create-new-feature-branch.ps1")],
    }
    result = run([*commands[variant], "Invalid numbering"], repo, env=script_env, timeout=30)
    assert result.returncode != 0
    assert (_snapshot(repo), _snapshot(workspace), record.read_bytes() if record else None) == before
