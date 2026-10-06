"""Workflow definitions and state use the workspace, but shell steps use the repository."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app


PROJECT_ID = "550e8400-e29b-41d4-a716-446655440000"
FOREIGN_PROJECT_ID = "550e8400-e29b-41d4-a716-446655440001"


@pytest.fixture(autouse=True)
def isolated_user_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for name, value in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "roaming",
        "LOCALAPPDATA": home / "local",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("SPECKIT_WORKFLOW_RUN_ID", raising=False)


@pytest.fixture
def external_project(tmp_path, monkeypatch):
    repository = tmp_path / "code repository"
    workspace = tmp_path / "external workspace"
    for root in (repository, workspace):
        (root / ".specify").mkdir(parents=True)
    (repository / ".specify/project.json").write_text(
        json.dumps({"schema_version": 1, "project_id": PROJECT_ID, "storage": "external"}),
        encoding="utf-8",
    )
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": PROJECT_ID}), encoding="utf-8"
    )
    record = repository / ".specify/checkout.json"
    record.write_text(
        json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None}),
        encoding="utf-8",
    )
    nested = repository / "src/package"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    return repository, workspace


def write_workflow(path: Path, *, gated: bool = False) -> Path:
    script = "from pathlib import Path; Path('shell-cwd.txt').write_text(str(Path.cwd()), encoding='utf-8')"
    argv = [sys.executable, "-c", script]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    steps = []
    if gated:
        steps.append({
            "id": "review",
            "type": "gate",
            "message": "Approve the workflow",
            "options": ["approve", "reject"],
            "verdict_input": "verdict",
        })
    steps.append({"id": "record-cwd", "type": "shell", "run": command})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "workflow": {"id": "external-check", "name": "External Check", "version": "1.0.0"},
        "inputs": {"verdict": {"type": "string", "default": ""}},
        "steps": steps,
    }), encoding="utf-8")
    return path


def invoke_json(*args: str) -> dict:
    result = CliRunner().invoke(app, ["workflow", *args, "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_workflow_install_and_enablement_belong_to_workspace(external_project, tmp_path):
    repository, workspace = external_project
    source = write_workflow(tmp_path / "incoming/workflow.yml")
    runner = CliRunner()

    added = runner.invoke(app, ["workflow", "add", str(source), "--dev"], catch_exceptions=False)

    assert added.exit_code == 0, added.output
    assert (workspace / ".specify/workflows/external-check/workflow.yml").is_file()
    assert not (repository / ".specify/workflows").exists()
    disabled = runner.invoke(app, ["workflow", "disable", "external-check"], catch_exceptions=False)
    assert disabled.exit_code == 0, disabled.output
    blocked = runner.invoke(app, ["workflow", "run", "external-check", "--json"])
    assert blocked.exit_code != 0
    assert "disabled" in blocked.stderr.lower()
    assert not (repository / "shell-cwd.txt").exists()
    enabled = runner.invoke(app, ["workflow", "enable", "external-check"], catch_exceptions=False)
    assert enabled.exit_code == 0, enabled.output
    assert invoke_json("run", "external-check")["status"] == "completed"
    assert not (repository / ".specify/workflows").exists()


@pytest.mark.parametrize("source_kind", ["installed-id", "direct-file"])
def test_external_run_status_and_resume_keep_shell_cwd_in_repository(
    external_project, source_kind
):
    repository, workspace = external_project
    workflow = write_workflow(
        workspace / ".specify/workflows/external-check/workflow.yml", gated=True
    )
    source = "external-check" if source_kind == "installed-id" else str(workflow)

    started = invoke_json("run", source)

    assert started["status"] == "paused"
    run_id = started["run_id"]
    run_dir = workspace / ".specify/workflows/runs" / run_id
    assert (run_dir / "state.json").is_file()
    assert not (repository / ".specify/workflows").exists()
    assert not (repository / "shell-cwd.txt").exists()
    status = invoke_json("status", run_id)
    assert status["status"] == "paused"
    assert status["steps"]["review"] == "paused"
    listing = invoke_json("status")
    assert [run["run_id"] for run in listing["runs"]] == [run_id]

    resumed = invoke_json("resume", run_id, "--input", "verdict=approve")

    assert resumed["run_id"] == run_id
    assert resumed["status"] == "completed"
    assert Path((repository / "shell-cwd.txt").read_text(encoding="utf-8")) == repository.resolve()
    assert not (workspace / "shell-cwd.txt").exists()
    assert not (repository / ".specify/workflows").exists()
    status = invoke_json("status", run_id)
    assert status["steps"] == {"review": "completed", "record-cwd": "completed"}
    assert json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["status"] == "completed"


def test_direct_file_outside_project_keeps_cwd_and_local_state(tmp_path, monkeypatch):
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    source = write_workflow(tmp_path / "definitions/workflow.yml")
    monkeypatch.chdir(standalone)

    completed = invoke_json("run", str(source))

    assert completed["status"] == "completed"
    assert Path((standalone / "shell-cwd.txt").read_text(encoding="utf-8")) == standalone.resolve()
    assert (standalone / ".specify/workflows/runs" / completed["run_id"] / "state.json").is_file()
    assert not (source.parent / ".specify").exists()
    assert not (source.parent / "shell-cwd.txt").exists()
    assert invoke_json("status", completed["run_id"])["status"] == "completed"


@pytest.mark.parametrize("operation", ["add", "run-id", "run-file"])
def test_foreign_workspace_is_rejected_before_workflow_access(
    external_project, tmp_path, monkeypatch, operation
):
    repository, workspace = external_project
    source = write_workflow(workspace / ".specify/workflows/external-check/workflow.yml")
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": FOREIGN_PROJECT_ID}), encoding="utf-8"
    )
    if operation == "add":
        incoming = write_workflow(tmp_path / "incoming/workflow.yml")
        args = ["add", str(incoming), "--dev"]
    elif operation == "run-id":
        args = ["run", "external-check", "--json"]
    else:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("SPECIFY_INIT_DIR", str(repository))
        args = ["run", str(source), "--json"]
    before = {
        path: path.read_bytes()
        for root in (repository, workspace)
        for path in root.rglob("*") if path.is_file()
    }

    result = CliRunner().invoke(app, ["workflow", *args])

    assert result.exit_code != 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    diagnostic = result.output.lower()
    assert "workspace" in diagnostic
    assert any(word in diagnostic for word in ("another project", "foreign", "mismatch"))
    if "--json" in args:
        assert result.stdout == ""
    after = {
        path: path.read_bytes()
        for root in (repository, workspace)
        for path in root.rglob("*") if path.is_file()
    }
    assert after == before
    assert not (repository / ".specify/workflows").exists()
