"""The `specs` link from a checkout with external storage to its workspace specs."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.private_checkout import BEGIN, END, exclude_path
from specify_cli.specs_link import link_specs
from tests.specify_cli.project.test_command_unlink import external_project, git


def _block(checkout) -> list[str]:
    lines = exclude_path(checkout).read_text(encoding="utf-8").splitlines() if exclude_path(checkout).exists() else []
    return lines[lines.index(BEGIN) + 1 : lines.index(END)] if BEGIN in lines else []


def _status(checkout) -> list[str]:
    return git(checkout, "status", "--porcelain", "--untracked-files=all").splitlines()


@pytest.mark.parametrize("options", [(), ("--private",)], ids=["shared", "private"])
def test_external_init_links_specs_and_git_does_not_see_the_link(tmp_path, monkeypatch, options):
    repository, workspace = external_project(tmp_path, monkeypatch, *options)
    link = repository / "specs"
    assert link.is_symlink() and link.resolve() == (workspace / "specs").resolve()
    assert (link / "001-alpha/spec.md").read_text(encoding="utf-8") == "# specs/001-alpha\n"
    assert "/specs" in _block(repository)
    assert _status(repository) == []
    tracked = git(repository, "ls-files").splitlines()
    assert "specs" not in tracked
    # A shared project commits its locator. The specs rule must not hide it.
    assert (".specify/project.json" in tracked) is (not options)

    # A second link run changes nothing.
    exclude = exclude_path(repository).read_bytes()
    assert link_specs(repository, workspace) is None
    assert exclude_path(repository).read_bytes() == exclude
    assert link.resolve() == (workspace / "specs").resolve()


def test_link_in_a_new_worktree_follows_the_specs_link_default(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch, "--private")
    on, off = tmp_path / "on", tmp_path / "off"
    git(repository, "worktree", "add", "-q", str(on))
    git(repository, "worktree", "add", "-q", str(off))

    monkeypatch.chdir(on)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    assert CliRunner().invoke(app, ["config", "set", "specs_link", "false"]).exit_code == 0
    monkeypatch.chdir(off)
    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert (on / "specs").resolve() == (workspace / "specs").resolve()
    assert not (off / "specs").exists() and not (off / "specs").is_symlink()
    assert _status(on) == [] and _status(off) == []


def test_link_keeps_an_existing_specs_folder(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    other = tmp_path / "B"
    git(repository, "worktree", "add", "-q", str(other))
    (other / "specs").mkdir()
    (other / "specs/notes.md").write_text("mine\n", encoding="utf-8")
    monkeypatch.chdir(other)

    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert f"Kept {other / 'specs'}" in " ".join(result.output.split())
    assert not (other / "specs").is_symlink()
    assert (other / "specs/notes.md").read_text(encoding="utf-8") == "mine\n"


def test_unlink_removes_the_specs_link_and_its_rule(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch, "--private")

    result = CliRunner().invoke(app, ["project", "unlink"])

    assert result.exit_code == 0, result.output
    assert not (repository / "specs").is_symlink()
    assert (workspace / "specs/001-alpha/spec.md").is_file()
    assert "/specs" not in _block(repository)


def test_move_to_local_replaces_the_link_with_a_real_specs_folder(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)

    result = CliRunner().invoke(app, ["project", "move", "--to-local"])

    assert result.exit_code == 0, result.output
    specs = repository / "specs"
    assert specs.is_dir() and not specs.is_symlink()
    assert (specs / "002-beta/spec.md").read_text(encoding="utf-8") == "# specs/002-beta\n"
    assert "/specs" not in _block(repository)
    assert "?? specs/001-alpha/spec.md" in _status(repository)


@pytest.mark.parametrize("value", ["yes", "1", "True"])
def test_specs_link_accepts_only_true_or_false(tmp_path, monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["config", "set", "specs_link", value])
    assert result.exit_code == 1
    assert not (tmp_path / "specify/config.json").exists()
