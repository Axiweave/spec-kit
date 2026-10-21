"""Links from a checkout with external storage to the workspace `specs` and `.specify/memory`."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.private_checkout import BEGIN, END, exclude_path
from specify_cli.workspace_links import LINKS, link_workspace
from tests.specify_cli.project.test_command_unlink import external_project, git

RULES = {f"/{name}" for name in LINKS}


def _block(checkout) -> list[str]:
    lines = exclude_path(checkout).read_text(encoding="utf-8").splitlines() if exclude_path(checkout).exists() else []
    return lines[lines.index(BEGIN) + 1 : lines.index(END)] if BEGIN in lines else []


def _status(checkout) -> list[str]:
    return git(checkout, "status", "--porcelain", "--untracked-files=all").splitlines()


def _linked(checkout, workspace) -> bool:
    return all(
        (checkout / name).is_symlink() and (checkout / name).resolve() == (workspace / name).resolve()
        for name in LINKS
    )


@pytest.mark.parametrize("options", [(), ("--private",)], ids=["shared", "private"])
def test_external_init_links_workspace_folders_and_git_does_not_see_them(tmp_path, monkeypatch, options):
    repository, workspace = external_project(tmp_path, monkeypatch, *options)
    assert _linked(repository, workspace)
    assert (repository / "specs/001-alpha/spec.md").read_text(encoding="utf-8") == "# specs/001-alpha\n"
    assert (repository / ".specify/memory/constitution.md").read_bytes() == (
        workspace / ".specify/memory/constitution.md"
    ).read_bytes()
    assert RULES <= set(_block(repository))
    assert _status(repository) == []
    tracked = git(repository, "ls-files").splitlines()
    assert not {"specs", ".specify/memory"} & set(tracked)
    # A shared project commits its locator. The link rules must not hide it.
    assert (".specify/project.json" in tracked) is (not options)

    # A second link run changes nothing.
    exclude = exclude_path(repository).read_bytes()
    assert link_workspace(repository, workspace) == []
    assert exclude_path(repository).read_bytes() == exclude
    assert _linked(repository, workspace)


def test_link_in_a_new_worktree_follows_the_workspace_links_default(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch, "--private")
    on, off = tmp_path / "on", tmp_path / "off"
    git(repository, "worktree", "add", "-q", str(on))
    git(repository, "worktree", "add", "-q", str(off))

    monkeypatch.chdir(on)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    assert CliRunner().invoke(app, ["config", "set", "workspace_links", "false"]).exit_code == 0
    monkeypatch.chdir(off)
    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert _linked(on, workspace)
    for name in LINKS:
        assert not (off / name).exists() and not (off / name).is_symlink()
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
    assert (other / ".specify/memory").is_symlink()
    # The exclude file is shared. The link rule in A must not hide B's real folder.
    assert "?? specs/notes.md" in _status(other)
    assert (other / "specs/notes.md").read_text(encoding="utf-8") == "mine\n"


def test_link_keeps_a_live_foreign_symlink_with_the_same_name(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    other = tmp_path / "B"
    git(repository, "worktree", "add", "-q", str(other))
    docs = tmp_path / "docs/specs"
    docs.mkdir(parents=True)
    (other / "specs").symlink_to(docs, target_is_directory=True)
    monkeypatch.chdir(other)

    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert (other / "specs").resolve() == docs.resolve()
    assert f"Kept {other / 'specs'}" in " ".join(result.output.split())


def test_relink_to_a_moved_workspace_replaces_the_old_links(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch, "--private")
    moved = workspace.rename(tmp_path / "moved")

    result = CliRunner().invoke(app, ["project", "link", str(moved)])

    assert result.exit_code == 0, result.output
    assert _linked(repository, moved)
    assert _status(repository) == []


def test_unlink_removes_the_links_and_their_rules(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch, "--private")

    result = CliRunner().invoke(app, ["project", "unlink"])

    assert result.exit_code == 0, result.output
    for name in LINKS:
        assert not (repository / name).is_symlink()
    assert (workspace / "specs/001-alpha/spec.md").is_file()
    assert (workspace / ".specify/memory/constitution.md").is_file()
    assert not RULES & set(_block(repository))


def test_move_to_local_replaces_the_links_with_real_folders(tmp_path, monkeypatch):
    repository, workspace = external_project(tmp_path, monkeypatch)
    constitution = (workspace / ".specify/memory/constitution.md").read_bytes()

    result = CliRunner().invoke(app, ["project", "move", "--to-local"])

    assert result.exit_code == 0, result.output
    for name in LINKS:
        assert (repository / name).is_dir() and not (repository / name).is_symlink()
    assert (repository / "specs/002-beta/spec.md").read_text(encoding="utf-8") == "# specs/002-beta\n"
    assert (repository / ".specify/memory/constitution.md").read_bytes() == constitution
    assert not RULES & set(_block(repository))
    assert "?? specs/001-alpha/spec.md" in _status(repository)


@pytest.mark.parametrize("value", ["yes", "1", "True"])
def test_workspace_links_accepts_only_true_or_false(tmp_path, monkeypatch, value):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["config", "set", "workspace_links", value])
    assert result.exit_code == 1
    assert not (tmp_path / "specify/config.json").exists()
