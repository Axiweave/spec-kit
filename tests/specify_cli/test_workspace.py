"""Workspace invariants: identity agreement, confinement, and selection precedence."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from specify_cli.workspace import resolve_project, save_active_feature


def external(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    (repo / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    identity = str(uuid4())
    (repo / ".specify/project.json").write_text(json.dumps({"schema_version": 1, "project_id": identity, "storage": "external"}))
    (workspace / ".specify/workspace.json").write_text(json.dumps({"schema_version": 1, "project_id": identity}))
    record = tmp_path / "data/specify/projects" / f"{identity}.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None}))
    return repo, workspace, record


@pytest.mark.parametrize("name", ["specs/001-first", "specs/002-next", "custom/path with spaces"])
def test_selection_round_trip_is_external_and_repository_stays_fixed(tmp_path, monkeypatch, name):
    repo, workspace, _ = external(tmp_path, monkeypatch)
    feature = workspace / name
    feature.mkdir(parents=True)
    project = resolve_project(repo)
    assert project.feature_dir is None
    save_active_feature(project, feature)
    selected = resolve_project(repo)
    assert selected.repository_root == repo
    assert selected.workspace_root == workspace
    assert selected.feature_dir == feature
    assert sorted(p.name for p in (repo / ".specify").iterdir()) == ["project.json"]


@pytest.mark.parametrize("absolute", [False, True])
def test_explicit_selection_wins_without_mutating_saved_selection(tmp_path, monkeypatch, absolute):
    repo, workspace, record = external(tmp_path, monkeypatch)
    first, second = workspace / "specs/first", workspace / "specs/second"
    first.mkdir(parents=True)
    second.mkdir()
    save_active_feature(resolve_project(repo), first)
    before = record.read_bytes()
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(second if absolute else Path("specs/second")))
    assert resolve_project(repo).feature_dir == second
    assert record.read_bytes() == before


@pytest.mark.parametrize("fault", ["mapping", "foreign", "version", "traversal", "missing", "locator_symlink"])
def test_invalid_external_state_never_falls_back_or_writes(tmp_path, monkeypatch, fault):
    repo, workspace, record = external(tmp_path, monkeypatch)
    if fault == "mapping":
        record.unlink()
    elif fault == "foreign":
        (workspace / ".specify/workspace.json").write_text(json.dumps({"schema_version": 1, "project_id": str(uuid4())}))
    elif fault == "version":
        data = json.loads(record.read_text())
        data["schema_version"] = 999
        record.write_text(json.dumps(data))
    elif fault == "traversal":
        monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "../outside")
    elif fault == "missing":
        data = json.loads(record.read_text())
        data["workspace"] = str(tmp_path / "absent")
        record.write_text(json.dumps(data))
    else:
        locator = repo / ".specify/project.json"
        target = tmp_path / "locator.json"
        locator.rename(target)
        locator.symlink_to(target)
    before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        resolve_project(repo)
    assert {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*") if p.is_file()} == before


def test_symlink_feature_escape_is_rejected(tmp_path, monkeypatch):
    repo, workspace, _ = external(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "escape")
    with pytest.raises(ValueError):
        resolve_project(repo)


def test_local_pointer_and_absolute_override_keep_existing_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/old"}')
    assert resolve_project(tmp_path).feature_dir == tmp_path / "specs/old"
    outside = tmp_path.parent / "external-feature"
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(outside))
    assert resolve_project(tmp_path).feature_dir == outside


def test_strict_init_dir_does_not_search_parent(tmp_path, monkeypatch):
    (tmp_path / ".specify").mkdir()
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.setenv("SPECIFY_INIT_DIR", str(child))
    with pytest.raises(ValueError):
        resolve_project(tmp_path)


def test_unreadable_workspace_reports_location_without_fallback(tmp_path, monkeypatch):
    repo, workspace, _ = external(tmp_path, monkeypatch)
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path == workspace / ".specify/workspace.json":
            raise PermissionError("Permission denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(ValueError, match=str(workspace)):
        resolve_project(repo)
    assert not (repo / "specs").exists()


@pytest.mark.parametrize("discovery", ["cwd", "nested-cwd", "override"])
def test_workspace_is_not_a_user_repository_but_internal_reads_work(tmp_path, monkeypatch, discovery):
    """Invariant: user discovery rejects workspace roots without changing their internal read contract."""
    import os
    import shlex
    import subprocess
    import sys

    from typer.testing import CliRunner

    from specify_cli import app
    from specify_cli.integration_state import try_read_integration_json
    from specify_cli.workspace import workspace_root_for

    repo, workspace, _ = external(tmp_path, monkeypatch)
    state = {"default_integration": "omp"}
    (workspace / ".specify/integration.json").write_text(json.dumps(state), encoding="utf-8")
    nested = workspace / "specs/001-feature"
    nested.mkdir(parents=True)
    if discovery == "override":
        monkeypatch.chdir(repo)
        monkeypatch.setenv("SPECIFY_INIT_DIR", str(workspace))
    else:
        monkeypatch.chdir(nested if discovery == "nested-cwd" else workspace)
    argv = [sys.executable, "-c", "from pathlib import Path; Path('unexpected.txt').write_text('ran')"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    workflow = tmp_path / "standalone.yml"
    workflow.write_text(json.dumps({
        "schema_version": "1.0",
        "workflow": {"id": "storage-probe", "name": "Storage probe", "version": "1.0.0"},
        "steps": [{"id": "write", "type": "shell", "run": command}],
    }), encoding="utf-8")
    before = {p: p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")}

    with pytest.raises(ValueError) as error:
        resolve_project()
    assert str(workspace) in str(error.value)
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 1
    assert str(workspace) in result.stderr
    result = CliRunner().invoke(app, ["workflow", "run", str(workflow)])
    assert result.exit_code == 1, result.output
    assert workspace_root_for(workspace) == workspace
    data, diagnostic = try_read_integration_json(workspace)
    assert diagnostic is None
    assert data["default_integration"] == state["default_integration"]
    assert {p: p.read_bytes() if p.is_file() else None for p in tmp_path.rglob("*")} == before

    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    assert resolve_project(repo).workspace_root == workspace
