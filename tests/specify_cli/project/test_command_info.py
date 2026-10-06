"""Project inspection is read-only and reports the invoking project."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app

_LINK_HINT = "If this checkout belongs to a project with external storage, run specify project link PATH."


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config", "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
        "GIT_AUTHOR_NAME": "Team", "GIT_COMMITTER_NAME": "Team",
        "GIT_AUTHOR_EMAIL": "team@example.com", "GIT_COMMITTER_EMAIL": "team@example.com",
    }.items():
        monkeypatch.setenv(key, str(value))
    for key in ("SPECIFY_INIT_DIR", "SPECIFY_FEATURE_DIRECTORY"):
        monkeypatch.delenv(key, raising=False)
    return home


def test_info_in_unattached_worktree_of_private_project_hints_link(tmp_path, monkeypatch, isolated_home):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_text("team\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team")
    monkeypatch.chdir(main)
    init = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(tmp_path / "ws"), "--private", "--script", "sh",
        "--no-workspace-git",
    ])
    assert init.exit_code == 0, init.output
    worktree = tmp_path / "worktree"
    _git(main, "worktree", "add", "-q", str(worktree))
    assert not (worktree / ".specify").exists()
    monkeypatch.chdir(worktree)

    result = CliRunner().invoke(app, ["project", "info"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: No Spec Kit project found from {Path.cwd()}. {_LINK_HINT}\n"


def test_info_outside_git_has_no_link_hint(tmp_path, monkeypatch, isolated_home):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)

    result = CliRunner().invoke(app, ["project", "info"])

    assert result.exit_code == 1
    assert result.stderr == f"Error: No Spec Kit project found from {Path.cwd()}\n"


_DETACH_HINT = ". Run specify project unlink to detach this checkout, or specify project link <backup>.\n"


@pytest.mark.parametrize("loss", ["workspace", "identity", "record"])
def test_info_names_unlink_only_when_the_workspace_is_lost_or_broken(tmp_path, monkeypatch, isolated_home, loss):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.chdir(repository)
    init = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(tmp_path / "ws"), "--script", "sh", "--no-workspace-git",
    ])
    assert init.exit_code == 0, init.output
    assert CliRunner().invoke(app, ["project", "info"]).exit_code == 0
    if loss == "workspace":
        shutil.rmtree(tmp_path / "ws")
    elif loss == "identity":
        (tmp_path / "ws/.specify/workspace.json").unlink()
    else:  # A new worktree or clone has no record yet: link it.
        (repository / ".specify/checkout.json").unlink()

    result = CliRunner().invoke(app, ["project", "info"])

    assert result.exit_code == 1
    assert result.stdout == ""
    if loss == "record":
        assert result.stderr == f"Error: Missing workspace mapping for {repository}. Use specify project link PATH.\n"
    else:
        assert result.stderr.startswith("Error: ") and result.stderr.endswith(_DETACH_HINT)


def _features(root: Path, *names: str) -> None:
    for name in names:
        (root / "specs" / name).mkdir(parents=True)
        (root / "specs" / name / "spec.md").write_text("# spec\n", encoding="utf-8")


def test_info_reports_duplicate_prefixes_with_the_rename_command(tmp_path, monkeypatch):
    _local_project(tmp_path, monkeypatch, {"feature_numbering": "sequential"})
    _features(tmp_path, "008-login", "008-billing", "009-unique", "custom")

    data = json.loads(CliRunner().invoke(app, ["project", "info", "--json"]).stdout)
    text = CliRunner().invoke(app, ["project", "info"])

    assert data["duplicate_prefixes"] == [["specs/008-billing", "specs/008-login"]]
    assert text.exit_code == 0, text.output
    assert text.stdout.endswith(
        "Duplicate feature prefixes:\n"
        "  specs/008-billing, specs/008-login\n"
        "Rename with: specify project migrate-naming --feature-numbering timestamp --dry-run\n"
    )
    assert "duplicate_prefixes" not in text.stdout


def test_info_tells_a_timestamp_project_to_rename_timestamp_duplicates_by_hand(tmp_path, monkeypatch):
    _local_project(tmp_path, monkeypatch, {"feature_numbering": "timestamp"})
    _features(tmp_path, "20260101-000000-a", "20260101-000000-b")

    text = CliRunner().invoke(app, ["project", "info"])

    assert text.exit_code == 0, text.output
    assert "  specs/20260101-000000-a, specs/20260101-000000-b\n" in text.stdout
    assert "by hand" in text.stdout
    assert "migrate-naming" not in text.stdout  # migrate-naming skips names already in the target scheme


def test_info_without_duplicates_prints_no_duplicate_block(tmp_path, monkeypatch):
    _local_project(tmp_path, monkeypatch, {"feature_numbering": "sequential"})
    _features(tmp_path, "001-a", "002-a", "20260101-000000-a")

    data = json.loads(CliRunner().invoke(app, ["project", "info", "--json"]).stdout)
    text = CliRunner().invoke(app, ["project", "info"])

    assert data["duplicate_prefixes"] == []
    assert "Duplicate" not in text.stdout and "Rename" not in text.stdout


def test_info_reports_local_project_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/one"}')
    (tmp_path / ".specify/init-options.json").write_text('{"feature_selection":"automatic"}')
    monkeypatch.chdir(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["storage"] == "local"
    assert data["repository_root"] == data["workspace_root"] == str(tmp_path)
    assert data["active_feature"] == "specs/one"
    assert data["feature_selection"] == "automatic"
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_info_errors_do_not_corrupt_json_stream(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "Spec Kit project" in result.stderr


def test_extension_listing_does_not_depend_on_feature_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".specify").mkdir()
    pointer = tmp_path / ".specify/feature.json"
    pointer.write_text("malformed feature state")
    result = CliRunner().invoke(app, ["extension", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert pointer.read_text() == "malformed feature state"


def _local_project(tmp_path, monkeypatch, options):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/one"}')
    if options is not None:
        (tmp_path / ".specify/init-options.json").write_text(json.dumps(options))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("options, saved_is_used", [
    (None, False),
    ({}, False),
    ({"feature_selection": "context"}, False),
    ({"feature_selection": "automatic", "future_setting": {"kept": True}}, True),
], ids=["no-file", "no-field", "context", "automatic"])
@pytest.mark.parametrize("override", [None, "specs/two"])
def test_info_reports_saved_feature_only_in_automatic_mode(tmp_path, monkeypatch, options, saved_is_used, override):
    _local_project(tmp_path, monkeypatch, options)
    if override:
        monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", override)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["feature_selection"] == ("automatic" if saved_is_used else "context")
    assert data["active_feature"] == (override or ("specs/one" if saved_is_used else None))
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("value", ["random", "AUTOMATIC", None, 1])
def test_info_rejects_unknown_selection_mode_without_json_output(tmp_path, monkeypatch, value):
    _local_project(tmp_path, monkeypatch, {"feature_selection": value})
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "feature_selection" in result.stderr
