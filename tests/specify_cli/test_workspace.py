"""Workspace invariants: identity agreement, confinement, and selection precedence."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from specify_cli.workspace import feature_selection_mode, resolve_project, save_active_feature


def external(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    (repo / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    identity = str(uuid4())
    (repo / ".specify/project.json").write_text(json.dumps({"schema_version": 1, "project_id": identity, "storage": "external"}))
    (workspace / ".specify/workspace.json").write_text(json.dumps({"schema_version": 1, "project_id": identity}))
    record = repo / ".specify/checkout.json"
    record.write_text(json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None}))
    return repo, workspace, record


def local(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    return tmp_path, tmp_path, None


def policy(workspace, mode):
    (workspace / ".specify/init-options.json").write_text(json.dumps({"feature_selection": mode}))


STORAGE = {"local": local, "external": external}


@pytest.mark.parametrize("name", ["specs/001-first", "specs/002-next", "custom/path with spaces"])
def test_selection_round_trip_is_external_and_repository_stays_fixed(tmp_path, monkeypatch, name):
    repo, workspace, _ = external(tmp_path, monkeypatch)
    policy(workspace, "automatic")
    feature = workspace / name
    feature.mkdir(parents=True)
    project = resolve_project(repo)
    assert project.feature_dir is None
    save_active_feature(project, feature)
    selected = resolve_project(repo)
    assert selected.repository_root == repo
    assert selected.workspace_root == workspace
    assert selected.feature_dir == feature
    assert sorted(p.name for p in (repo / ".specify").iterdir()) == ["checkout.json", "project.json"]


@pytest.mark.parametrize("mode", ["context", "automatic"])
@pytest.mark.parametrize("absolute", [False, True])
def test_explicit_selection_wins_without_mutating_saved_selection(tmp_path, monkeypatch, absolute, mode):
    repo, workspace, record = external(tmp_path, monkeypatch)
    policy(workspace, mode)
    first, second = workspace / "specs/first", workspace / "specs/second"
    first.mkdir(parents=True)
    second.mkdir()
    save_active_feature(resolve_project(repo), first)
    before = record.read_bytes()
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(second if absolute else Path("specs/second")))
    assert resolve_project(repo).feature_dir == second
    assert record.read_bytes() == before


@pytest.mark.parametrize("mode", ["context", "automatic"])
@pytest.mark.parametrize("fault", ["mapping", "foreign", "version", "traversal", "missing", "locator_symlink"])
def test_invalid_external_state_never_falls_back_or_writes(tmp_path, monkeypatch, fault, mode):
    repo, workspace, record = external(tmp_path, monkeypatch)
    policy(workspace, mode)
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


@pytest.mark.parametrize("mode", ["context", "automatic"])
def test_symlink_feature_escape_is_rejected(tmp_path, monkeypatch, mode):
    repo, workspace, _ = external(tmp_path, monkeypatch)
    policy(workspace, mode)
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "escape")
    with pytest.raises(ValueError):
        resolve_project(repo)


@pytest.mark.parametrize("mode", ["context", "automatic"])
def test_local_pointer_and_absolute_override_keep_existing_behavior(tmp_path, monkeypatch, mode):
    local(tmp_path, monkeypatch)
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/old"}')
    policy(tmp_path, mode)
    assert resolve_project(tmp_path).feature_dir == (tmp_path / "specs/old" if mode == "automatic" else None)
    outside = tmp_path.parent / "external-feature"
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(outside))
    assert resolve_project(tmp_path).feature_dir == outside


@pytest.mark.parametrize("storage", ["local", "external"])
@pytest.mark.parametrize("options, saved_is_used", [
    (None, False),
    ({}, False),
    ({"feature_selection": "context", "future_setting": [1]}, False),
    ({"feature_selection": "automatic", "future_setting": [1]}, True),
], ids=["no-file", "no-field", "context", "automatic"])
def test_saved_feature_is_used_only_in_automatic_mode(tmp_path, monkeypatch, storage, options, saved_is_used):
    repo, workspace, record = STORAGE[storage](tmp_path, monkeypatch)
    feature = workspace / "specs/saved"
    feature.mkdir(parents=True)
    save_active_feature(resolve_project(repo), feature)
    if options is not None:
        (workspace / ".specify/init-options.json").write_text(json.dumps(options))
    state = record or repo / ".specify/feature.json"
    before = state.read_bytes()
    assert resolve_project(repo).feature_dir == (feature if saved_is_used else None)
    assert state.read_bytes() == before


@pytest.mark.parametrize("storage", ["local", "external"])
def test_malformed_saved_feature_breaks_only_automatic_mode(tmp_path, monkeypatch, storage):
    repo, workspace, record = STORAGE[storage](tmp_path, monkeypatch)
    if record:
        record.write_text(json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": ""}))
    else:
        (repo / ".specify/feature.json").write_text("{}")
    policy(workspace, "context")
    assert resolve_project(repo).feature_dir is None
    policy(workspace, "automatic")
    with pytest.raises(ValueError):
        resolve_project(repo)


def test_checkouts_of_one_project_resolve_their_own_record(tmp_path, monkeypatch):
    """FR-009/FR-010: a record or selection in one checkout never reaches another checkout."""
    first, workspace, _ = external(tmp_path, monkeypatch)
    clone = tmp_path / "workspace clone"
    shutil.copytree(workspace / ".specify", clone / ".specify")
    second = tmp_path / "second worktree"
    (second / ".specify").mkdir(parents=True)
    (second / ".specify/project.json").write_bytes((first / ".specify/project.json").read_bytes())
    (second / ".specify/checkout.json").write_text(json.dumps({
        "schema_version": 1, "workspace": str(clone), "active_feature": None,
    }))
    for root in (workspace, clone):
        policy(root, "automatic")
        for name in ("one", "two"):
            (root / "specs" / name).mkdir(parents=True)
    save_active_feature(resolve_project(first), workspace / "specs/one")
    save_active_feature(resolve_project(second), clone / "specs/two")
    one, two = resolve_project(first), resolve_project(second)
    assert (one.workspace_root, one.feature_dir) == (workspace, workspace / "specs/one")
    assert (two.workspace_root, two.feature_dir) == (clone, clone / "specs/two")


def test_symlinked_checkout_record_is_refused_for_read_and_write(tmp_path, monkeypatch):
    repo, workspace, record = external(tmp_path, monkeypatch)
    policy(workspace, "automatic")
    (workspace / "specs/one").mkdir(parents=True)
    project = resolve_project(repo)
    target = tmp_path / "elsewhere.json"
    record.rename(target)
    record.symlink_to(target)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="symlink"):
        resolve_project(repo)
    with pytest.raises(ValueError, match="symlink"):
        save_active_feature(project, workspace / "specs/one")
    assert record.is_symlink() and target.read_bytes() == before


def test_workspace_lookup_ignores_selection_policy_state_and_override(tmp_path, monkeypatch):
    from specify_cli.workspace import workspace_root_for

    repo, workspace, record = external(tmp_path, monkeypatch)
    (workspace / ".specify/init-options.json").write_text('{"feature_selection": "sometimes"}')
    record.write_text(json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": ""}))
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "../outside")
    assert workspace_root_for(repo) == workspace
    with pytest.raises(ValueError):
        resolve_project(repo)


@pytest.mark.parametrize("override", [None, "specs/one"])
@pytest.mark.parametrize("text", ['{"feature_selection": "sometimes"}', '{"feature_selection": null}', "{", "[]"])
@pytest.mark.parametrize("storage", ["local", "external"])
def test_invalid_selection_policy_is_rejected_even_with_an_explicit_override(
    tmp_path, monkeypatch, storage, text, override,
):
    repo, workspace, _ = STORAGE[storage](tmp_path, monkeypatch)
    (workspace / ".specify/init-options.json").write_text(text)
    if override:
        monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", override)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError):
        resolve_project(repo)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_feature_selection_mode_rejects_a_symlinked_policy(tmp_path):
    (tmp_path / ".specify").mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text('{"feature_selection": "automatic"}')
    (tmp_path / ".specify/init-options.json").symlink_to(target)
    with pytest.raises(ValueError):
        feature_selection_mode(tmp_path)


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
