"""Integration status and catalogs use the external workspace, not the repository."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from specify_cli import app
from tests.specify_cli.integrations._helpers import _run_in_project, runner


@pytest.fixture
def external_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for variable, directory in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
    }.items():
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.delenv("SPECKIT_INTEGRATION_CATALOG_URL", raising=False)
    repo, workspace = tmp_path / "repo", tmp_path / "ws"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    old_cwd = os.getcwd()
    try:
        os.chdir(repo)
        result = runner.invoke(app, [
            "init", "--here", "--force", "--storage", "external", "--workspace", str(workspace),
            "--integration", "claude", "--script", "sh", "--non-interactive",
            "--ignore-agent-tools", "--no-workspace-git", "--offline",
        ], catch_exceptions=False)
    finally:
        os.chdir(old_cwd)
    assert result.exit_code == 0, result.output
    return repo, workspace.resolve()


def _status(repo):
    result = _run_in_project(repo, ["integration", "status", "--json"])
    return result.exit_code, json.loads(result.output)


def test_fresh_external_project_status_is_ok(external_project):
    repo, workspace = external_project
    code, payload = _status(repo)
    assert payload["status"] == "ok", payload["findings"]
    assert code == 0
    for key, summary in payload["manifests"].items():
        assert summary["missing_files"] == []
        assert summary["manifest"] == (workspace / f".specify/integrations/{key}.manifest.json").as_posix()

    text = _run_in_project(repo, ["integration", "status"])
    assert text.exit_code == 0, text.output
    assert "Missing managed files: 0" in text.output


def test_external_status_reports_workspace_file_missing_and_modified(external_project):
    repo, workspace = external_project
    manifest = json.loads(
        (workspace / ".specify/integrations/speckit.manifest.json").read_text(encoding="utf-8")
    )
    deleted, edited = sorted(rel for rel in manifest["files"] if rel.startswith(".specify/"))[:2]
    (workspace / deleted).unlink()
    (workspace / edited).write_text("edited", encoding="utf-8")

    code, payload = _status(repo)

    assert code != 0
    speckit = payload["manifests"]["speckit"]
    assert speckit["missing_files"] == [deleted]
    assert speckit["modified_files"] == [edited]
    assert speckit["invalid_files"] == []


def test_external_catalog_add_writes_workspace_config(external_project):
    repo, workspace = external_project
    result = _run_in_project(
        repo,
        ["integration", "catalog", "add", "https://new.example.com/catalog.json", "--name", "mine"],
    )
    assert result.exit_code == 0, result.output

    assert (workspace / ".specify/integration-catalogs.yml").is_file()
    assert sorted(p.name for p in (repo / ".specify").iterdir()) == ["project.json"]

    listed = _run_in_project(repo, ["integration", "catalog", "list"])
    assert listed.exit_code == 0, listed.output
    assert "mine" in listed.output
