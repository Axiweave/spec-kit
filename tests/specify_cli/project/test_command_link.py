"""Explicit relinking preserves shared identity and changes only machine state."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app


PROJECT_ID = "550e8400-e29b-41d4-a716-446655440000"
FOREIGN_ID = "a60e8400-e29b-41d4-a716-446655440001"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def external_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("APPDATA", str(home / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "data"))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    repo = tmp_path / "original repository"
    workspace = tmp_path / "external workspace"
    (repo / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    (repo / "source.py").write_text("VALUE = 42\n", encoding="utf-8")
    # Noncanonical spacing detects rewrites that preserve parsed JSON values.
    (repo / ".specify/project.json").write_bytes(
        ('{ "storage": "external",\n "project_id": "' + PROJECT_ID
         + '", "schema_version": 1 }\n').encode()
    )
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": PROJECT_ID}), encoding="utf-8"
    )
    (workspace / ".specify/init-options.json").write_text(
        json.dumps({"integration": "omp", "script": "sh", "feature_numbering": "timestamp"}),
        encoding="utf-8",
    )
    feature = workspace / "specs/001-existing"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Existing specification\n", encoding="utf-8")
    config = home / "config/specify/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"storage_root": str(workspace)}), encoding="utf-8")
    record = home / "data/specify/projects" / f"{PROJECT_ID}.json"
    monkeypatch.chdir(repo)
    return repo, workspace, record


def test_link_clone_without_machine_record_preserves_shared_files(external_project, tmp_path, monkeypatch):
    original, workspace, record = external_project
    clone = tmp_path / "cloned repository"
    shutil.copytree(original, clone)
    nested = clone / "src/nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    before = snapshot(tmp_path)

    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "workspace": str(workspace),
        "active_feature": None,
    }
    after = snapshot(tmp_path)
    del after[record.relative_to(tmp_path).as_posix()]
    assert after == before
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert info.exit_code == 0, info.output
    data = json.loads(info.stdout)
    assert data["project_id"] == PROJECT_ID
    assert data["repository_root"] == str(clone)
    assert data["workspace_root"] == str(workspace)
    assert data["active_feature"] is None
    assert data["feature_numbering"] == "timestamp"


def test_repository_rename_retains_identity_mapping_and_active_feature(external_project, tmp_path, monkeypatch):
    repo, workspace, record = external_project
    linked = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert linked.exit_code == 0, linked.output
    state = json.loads(record.read_text(encoding="utf-8"))
    state["active_feature"] = "specs/001-existing"
    record.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    renamed = repo.rename(tmp_path / "renamed repository")
    monkeypatch.chdir(renamed)
    before = snapshot(tmp_path)

    result = CliRunner().invoke(app, ["project", "info", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["repository_root"] == str(renamed)
    assert data["workspace_root"] == str(workspace)
    assert data["project_id"] == PROJECT_ID
    assert data["active_feature"] == "specs/001-existing"
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "fault",
    [
        "missing-locator", "malformed-locator", "invalid-project-id", "locator-version",
        "missing-identity", "malformed-identity", "foreign-identity", "identity-version",
        "missing-workspace", "locator-symlink", "workspace-metadata-escape",
    ],
)
def test_link_rejects_invalid_identity_without_writes(external_project, tmp_path, fault):
    repo, workspace, record = external_project
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({
        "schema_version": 1, "workspace": str(workspace), "active_feature": "specs/001-existing",
    }), encoding="utf-8")
    locator = repo / ".specify/project.json"
    identity = workspace / ".specify/workspace.json"
    locator_bytes, identity_bytes = locator.read_bytes(), identity.read_bytes()
    target = workspace
    if fault == "missing-locator":
        locator.unlink()
    elif fault == "malformed-locator":
        locator.write_text("{", encoding="utf-8")
    elif fault in {"invalid-project-id", "locator-version"}:
        value = json.loads(locator_bytes)
        value["project_id" if fault == "invalid-project-id" else "schema_version"] = (
            "not-a-uuid" if fault == "invalid-project-id" else 999
        )
        locator.write_text(json.dumps(value), encoding="utf-8")
    elif fault == "missing-identity":
        identity.unlink()
    elif fault == "malformed-identity":
        identity.write_text("[]", encoding="utf-8")
    elif fault in {"foreign-identity", "identity-version"}:
        identity.write_text(json.dumps({
            "schema_version": 999 if fault == "identity-version" else 1,
            "project_id": FOREIGN_ID if fault == "foreign-identity" else PROJECT_ID,
        }), encoding="utf-8")
    elif fault == "missing-workspace":
        target = tmp_path / "absent workspace"
    else:
        source = locator if fault == "locator-symlink" else workspace / ".specify"
        outside = source.rename(tmp_path / "outside metadata")
        try:
            source.symlink_to(outside, target_is_directory=outside.is_dir())
        except OSError:
            pytest.skip("The platform does not support test symlinks.")
    before = snapshot(tmp_path)

    result = CliRunner().invoke(app, ["project", "link", str(target)])

    assert result.exit_code != 0
    assert snapshot(tmp_path) == before
    if locator.is_symlink():
        locator.unlink()
    if (workspace / ".specify").is_symlink():
        (workspace / ".specify").unlink()
        (tmp_path / "outside metadata").rename(workspace / ".specify")
    locator.write_bytes(locator_bytes)
    identity.write_bytes(identity_bytes)
    recovered = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert recovered.exit_code == 0, recovered.output
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert info.exit_code == 0, info.output
    assert json.loads(info.stdout)["workspace_root"] == str(workspace)


@pytest.mark.parametrize("fault", ["missing-record", "malformed-record", "missing-workspace", "foreign-workspace"])
def test_invalid_mapping_requires_explicit_link_not_personal_defaults(external_project, tmp_path, fault):
    _, workspace, record = external_project
    if fault != "missing-record":
        record.parent.mkdir(parents=True)
        if fault == "malformed-record":
            record.write_text("{", encoding="utf-8")
        else:
            unavailable = tmp_path / "other workspace"
            if fault == "foreign-workspace":
                (unavailable / ".specify").mkdir(parents=True)
                (unavailable / ".specify/workspace.json").write_text(
                    json.dumps({"schema_version": 1, "project_id": FOREIGN_ID}), encoding="utf-8"
                )
            record.write_text(json.dumps({
                "schema_version": 1, "workspace": str(unavailable), "active_feature": None,
            }), encoding="utf-8")
    before = snapshot(tmp_path)

    info = CliRunner().invoke(app, ["project", "info", "--json"])

    assert info.exit_code != 0
    assert info.stdout == ""
    assert snapshot(tmp_path) == before
    linked = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert linked.exit_code == 0, linked.output
    resolved = CliRunner().invoke(app, ["project", "info", "--json"])
    assert resolved.exit_code == 0, resolved.output
    assert json.loads(resolved.stdout)["workspace_root"] == str(workspace)
    assert json.loads(record.read_text(encoding="utf-8"))["workspace"] == str(workspace)
