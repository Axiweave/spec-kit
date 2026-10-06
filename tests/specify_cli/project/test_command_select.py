"""`specify project select` saves one checkout's active feature inside the project root."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app

_CONTEXT = (
    "Error: Saved selection is ignored in context mode. Pass SPECIFY_FEATURE_DIRECTORY, "
    "or set feature_selection to automatic.\n"
)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _checkout(path: Path, workspace: Path) -> Path:
    _write(path / ".specify/project.json", {"schema_version": 1, "storage": "external", "project_id": "p1"})
    _write(path / ".specify/checkout.json", {"schema_version": 1, "workspace": str(workspace)})
    return path


def _project(tmp_path: Path, storage: str, mode: str = "automatic") -> tuple[Path, Path]:
    """Return (checkout, root) with features specs/a and specs/b under root."""
    tmp_path = tmp_path.resolve()
    repo = tmp_path / "repo"
    root = tmp_path / "ws" if storage == "external" else repo
    if storage == "external":
        _write(root / ".specify/workspace.json", {"schema_version": 1, "project_id": "p1"})
        _checkout(repo, root)
    _write(root / ".specify/init-options.json", {"feature_selection": mode})
    for name in ("a", "b"):
        (root / "specs" / name).mkdir(parents=True)
    return repo, root


def _saved(repo: Path) -> str | None:
    record = repo / ".specify/checkout.json"
    if record.is_file():
        return json.loads(record.read_text()).get("active_feature")
    pointer = repo / ".specify/feature.json"
    return json.loads(pointer.read_text())["feature_directory"] if pointer.is_file() else None


def _select(monkeypatch, cwd: Path, feature: str):
    monkeypatch.chdir(cwd)
    return CliRunner().invoke(app, ["project", "select", feature])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("SPECIFY_INIT_DIR", "SPECIFY_FEATURE_DIRECTORY"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("storage", ["external", "local"])
def test_select_saves_feature_relative_and_absolute(tmp_path, monkeypatch, storage):
    repo, root = _project(tmp_path, storage)

    first = _select(monkeypatch, repo, "specs/a")
    assert (first.exit_code, first.stdout) == (0, "Selected: specs/a\n")
    assert _saved(repo) == "specs/a"

    second = _select(monkeypatch, repo, str(root / "specs/b"))
    assert (second.exit_code, second.stdout) == (0, "Selected: specs/b\n")
    assert _saved(repo) == "specs/b"


@pytest.mark.parametrize("storage", ["external", "local"])
@pytest.mark.parametrize("feature", ["../outside", "OUTSIDE", "specs/missing", "specs/link", "specs/a/file"])
def test_select_rejects_paths_that_are_not_a_feature_directory_in_the_root(tmp_path, monkeypatch, storage, feature):
    repo, root = _project(tmp_path, storage)
    (tmp_path / "outside").mkdir()
    (root / "specs/link").symlink_to(root / "specs/a", target_is_directory=True)
    (root / "specs/a/file").write_text("x", encoding="utf-8")
    feature = str(tmp_path / "outside") if feature == "OUTSIDE" else feature

    result = _select(monkeypatch, repo, feature)

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")
    assert _saved(repo) is None


@pytest.mark.parametrize("storage", ["external", "local"])
def test_select_refuses_context_mode_without_writing(tmp_path, monkeypatch, storage):
    repo, _ = _project(tmp_path, storage, mode="context")

    result = _select(monkeypatch, repo, "specs/a")

    assert result.exit_code == 1
    assert result.stderr == _CONTEXT
    assert _saved(repo) is None


def test_two_checkouts_keep_separate_selections(tmp_path, monkeypatch):
    first, root = _project(tmp_path, "external")
    second = _checkout(tmp_path.resolve() / "worktree", root)

    assert _select(monkeypatch, first, "specs/a").exit_code == 0
    assert _select(monkeypatch, second, "specs/b").exit_code == 0

    assert (_saved(first), _saved(second)) == ("specs/a", "specs/b")
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert json.loads(info.stdout)["active_feature"] == "specs/b"


@pytest.mark.parametrize("override", ["specs/b", "../escape", "/nowhere"])
def test_feature_directory_variable_does_not_affect_select(tmp_path, monkeypatch, override):
    repo, _ = _project(tmp_path, "external")
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", override)

    result = _select(monkeypatch, repo, "specs/a")

    assert (result.exit_code, result.stdout) == (0, "Selected: specs/a\n")
    assert _saved(repo) == "specs/a"
