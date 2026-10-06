"""Unlink detaches one checkout from external storage and never changes the workspace."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.workspace import atomic_json, checkout_record_path, resolve_project

FEATURES = ("specs/001-alpha", "specs/002-beta")
NEXT = "Next: specify init --here --storage local, or specify project link <backup>\n"
WARNING = "Warning: Git tracks .specify/project.json. Committing this deletion detaches every teammate's checkout.\n"


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def external_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *options: str) -> tuple[Path, Path]:
    """Commit checkout A of an external project. Its workspace holds two committed features."""
    if shutil.which("git") is None:
        pytest.skip("Git is required for checkout tests.")
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config", "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(key, str(value))
    for name in tuple(os.environ):
        if name.startswith(("SPECIFY_", "GIT_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Team")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "team@example.invalid")
    repository, workspace = tmp_path / "A", tmp_path / "ws"
    repository.mkdir()
    git(repository, "init", "-q")
    monkeypatch.chdir(repository)
    result = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--script", "sh", "--ignore-agent-tools",
        "--offline", "--non-interactive", "--feature-selection", "automatic", "--workspace", str(workspace), *options,
    ])
    assert result.exit_code == 0, result.output
    for feature in FEATURES:
        (workspace / feature).mkdir(parents=True)
        (workspace / feature / "spec.md").write_text(f"# {feature}\n", encoding="utf-8")
    git(repository, "add", "-A")
    git(repository, "commit", "-qm", "init", "--allow-empty")  # A private checkout hides every Spec Kit file.
    if (workspace / ".git").is_dir():
        git(workspace, "add", "-A")
        git(workspace, "commit", "-qm", "features")
    return repository, workspace


def second_worktree(repository: Path, workspace: Path, feature: str) -> Path:
    """Add worktree B of the same project, attached to the same workspace with its own feature."""
    other = repository.parent / "B"
    git(repository, "worktree", "add", "-q", str(other))
    atomic_json(checkout_record_path(other), {"schema_version": 1, "workspace": str(workspace), "active_feature": feature})
    return other


def checkout_state(checkout: Path) -> tuple[bytes, Path, Path | None]:
    project = resolve_project(checkout)
    return checkout_record_path(checkout).read_bytes(), project.workspace_root, project.feature_dir


@pytest.mark.parametrize("tracked", [True, False], ids=["tracked", "untracked"])
def test_unlink_removes_both_checkout_files_and_keeps_the_workspace(tmp_path, monkeypatch, tracked):
    repository, workspace = external_project(tmp_path, monkeypatch)
    if not tracked:
        git(repository, "rm", "-q", "--cached", ".specify/project.json")
    before = snapshot(workspace)

    result = CliRunner().invoke(app, ["project", "unlink"])

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "Removed: .specify/checkout.json\nRemoved: .specify/project.json\n" + (WARNING if tracked else "") + NEXT
    )
    assert not (repository / ".specify/checkout.json").exists()
    assert not (repository / ".specify/project.json").exists()
    assert snapshot(workspace) == before


def test_unlink_works_when_the_workspace_is_lost(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    shutil.rmtree(workspace)

    info = CliRunner().invoke(app, ["project", "info"])
    result = CliRunner().invoke(app, ["project", "unlink"])

    assert info.exit_code == 1
    assert "Run specify project unlink to detach this checkout, or specify project link <backup>." in info.stderr
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("Removed: .specify/checkout.json\nRemoved: .specify/project.json\n")
    assert not workspace.exists()
    assert resolve_project(repository).storage == "local"


@pytest.mark.parametrize("locator", [None, {"schema_version": 1, "project_id": "x", "storage": "local"}],
                         ids=["no-locator", "not-external"])
def test_unlink_refuses_a_checkout_without_an_external_locator(tmp_path, monkeypatch, locator):
    repository, workspace = external_project(tmp_path, monkeypatch)
    (repository / ".specify/project.json").unlink()
    if locator:
        (repository / ".specify/project.json").write_text(json.dumps(locator), encoding="utf-8")
    before = snapshot(repository)

    result = CliRunner().invoke(app, ["project", "unlink"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")
    assert snapshot(repository) == before


def test_local_init_after_unlink_ignores_the_feature_pointer(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    assert CliRunner().invoke(app, ["project", "unlink"]).exit_code == 0

    result = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--script", "sh", "--ignore-agent-tools",
        "--offline", "--non-interactive", "--storage", "local",
    ])

    assert result.exit_code == 0, result.output
    project = resolve_project(repository)
    assert project.storage == "local" and project.workspace_root == repository
    assert subprocess.run(["git", "check-ignore", "-q", ".specify/feature.json"], cwd=repository).returncode == 0


def test_unlink_in_one_worktree_keeps_the_other_worktree_attached(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    atomic_json(checkout_record_path(repository), {
        "schema_version": 1, "workspace": str(workspace), "active_feature": FEATURES[0],
    })
    other = second_worktree(repository, workspace, FEATURES[1])
    state, before = checkout_state(other), snapshot(workspace)
    assert state[1:] == (workspace, workspace / FEATURES[1])

    result = CliRunner().invoke(app, ["project", "unlink"])

    assert result.exit_code == 0, result.output
    assert not checkout_record_path(repository).exists()
    assert checkout_state(other) == state
    assert snapshot(workspace) == before
