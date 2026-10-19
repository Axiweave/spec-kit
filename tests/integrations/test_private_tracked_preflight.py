"""Private mode: install and switch refuse to write a Git-tracked file before any change."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True, text=True).stdout


def _run(monkeypatch, directory: Path, *args: str):
    monkeypatch.chdir(directory)
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, " ".join(result.output.split())


def _files(*roots: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for root in roots for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


@pytest.fixture
def private_project(tmp_path, monkeypatch):
    """A private claude project in a code repository."""
    repository, workspace = tmp_path / "repo", tmp_path / "ws"
    repository.mkdir()
    _git(repository, "init", "-q")
    (repository / "README.md").write_text("team\n", encoding="utf-8")
    code, output = _run(
        monkeypatch, repository, "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools",
        "--offline", "--non-interactive", "--workspace", str(workspace), "--private", "--script", "sh",
        "--no-workspace-git",
    )
    assert code == 0, output
    return repository, workspace


def _track(repository: Path, relative: str) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"team": true}\n', encoding="utf-8")
    _git(repository, "add", "-f", relative)
    _git(repository, "commit", "-q", "-m", "team file")


def test_switch_refuses_tracked_target_file_before_removing_current(private_project, monkeypatch):
    repository, workspace = private_project
    _track(repository, ".gemini/commands/speckit.plan.toml")
    before = _files(repository, workspace)

    code, output = _run(monkeypatch, repository, "integration", "switch", "gemini")

    assert code == 1
    assert "tracked file .gemini/commands/speckit.plan.toml" in output
    assert _files(repository, workspace) == before


def test_install_preflight_uses_the_workspace_event_settings(private_project, monkeypatch):
    repository, workspace = private_project
    (workspace / ".specify/integration-events.yml").write_text(
        "integrations:\n  copilot:\n    events:\n      stop:\n        command: speckit.plan\n", encoding="utf-8",
    )
    _track(repository, ".github/hooks/speckit.json")
    before = _files(repository, workspace)

    code, output = _run(monkeypatch, repository, "integration", "install", "copilot", "--force")

    assert code == 1
    assert "tracked file .github/hooks/speckit.json" in output
    assert _files(repository, workspace) == before
