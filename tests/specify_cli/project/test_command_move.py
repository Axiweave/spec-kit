"""A local project moves only after a verified copy and explicit cleanup consent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

from specify_cli import app
from tests.parity_helpers import install_scripts, py_cmd


ACTIVE_FEATURE = "specs/007-current"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def git(repository: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return result.stdout


def git_state(repository: Path) -> tuple[bytes, bytes, bytes, bytes]:
    return (
        git(repository, "rev-parse", "HEAD"),
        git(repository, "symbolic-ref", "HEAD"),
        git(repository, "log", "--all", "--format=%H %P %s"),
        (repository / ".git/index").read_bytes(),
    )


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("Git is required for the migration history checks.")
    home = tmp_path / "home"
    home.mkdir()
    for name, path in {
        "HOME": home, "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / "config", "XDG_DATA_HOME": home / "data",
        "XDG_CACHE_HOME": home / "cache", "APPDATA": home / "config",
        "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(name, str(path))
    for name in tuple(os.environ):
        if name.startswith(("SPECIFY_", "SPECKIT_", "GIT_", "OMP_", "PI_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / "agent"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("COLUMNS", "240")
    source = str(Path(__file__).resolve().parents[3] / "src")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH")))))
    repository = tmp_path / "code project"
    repository.mkdir()
    files = {
        "source.py": b"VALUE = 42\n",
        ".specify/feature.json": b'{ "feature_directory": "specs/007-current" }\n',
        ".specify/init-options.json": json.dumps({
            "integration": "omp", "ai": "omp", "script": "sh",
            "feature_numbering": "timestamp", "command_scope": "project",
        }).encode(),
        ".specify/memory/constitution.md": b"# User constitution\nKeep this edit.\n",
        ".specify/templates/plan-template.md": b"# Project plan template\n",
        ".specify/templates/commands/plan.md": (
            b"---\nscripts:\n  sh: scripts/bash/setup-plan.sh --json\n---\n"
            b"User plan instructions. Run {SCRIPT}.\n"
        ),
        ".specify/custom/edited.bin": bytes(range(256)),
        "specs/001-complete/spec.md": b"# Completed feature\n",
        "specs/001-complete/tasks.md": b"- [x] Keep completed work\n",
        f"{ACTIVE_FEATURE}/spec.md": b"# Active feature\n",
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    install_scripts(repository, "setup-plan")
    install_scripts(repository, "check-prerequisites")
    git(repository, "init", "--initial-branch=unrelated-development")
    git(repository, "config", "user.name", "Migration Test")
    git(repository, "config", "user.email", "migration@example.invalid")
    git(repository, "add", ".")
    git(repository, "-c", "commit.gpgsign=false", "commit", "-m", "Seed local project")
    (repository / "source.py").write_bytes(b"VALUE = 43\n")
    git(repository, "add", "source.py")
    (repository / "notes.txt").write_bytes(b"Untracked repository notes\n")
    monkeypatch.chdir(repository)
    return repository, tmp_path / "external workspace", home


def project_info(repository: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-c", "from specify_cli import app; app()", "project", "info", "--json"],
        cwd=repository, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def assert_local(repository: Path, home: Path, before: dict[str, bytes], history: tuple):
    assert snapshot(repository) == before
    assert git_state(repository) == history
    assert not list((home / "data/specify/projects").glob("*.json"))
    info = project_info(repository)
    assert info["storage"] == "local"
    assert info["workspace_root"] == info["repository_root"] == str(repository)
    assert info["active_feature"] == ACTIVE_FEATURE


def assert_moved(repository: Path, workspace: Path, before: dict[str, bytes], history: tuple):
    for relative, content in before.items():
        if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
            assert (workspace / relative).read_bytes() == content
        elif not relative.startswith((".specify/", "specs/")):
            assert (repository / relative).read_bytes() == content
    assert not (repository / "specs").exists()
    assert set(snapshot(repository / ".specify")) == {"project.json"}
    assert not (workspace / ".specify/project.json").exists()
    assert git_state(repository) == history
    info = project_info(repository)
    assert info["storage"] == "external"
    assert info["repository_root"] == str(repository)
    assert info["workspace_root"] == str(workspace)
    assert info["active_feature"] == ACTIVE_FEATURE
    assert info["feature_numbering"] == "timestamp"
    assert info["integration"] == "omp"
    assert info["script"] == "sh"
    return info


def assert_recovery(output: str, repository: Path, workspace: Path):
    assert str(repository) in output
    assert str(workspace) in output
    lower = output.lower()
    assert "stag" in lower or "cop" in lower
    assert any(word in lower for word in ("retained", "unchanged", "intact", "usable", "preserved"))
    assert any(word in lower for word in ("retry", "remove", "choose", "inspect", "recover"))


@pytest.mark.parametrize("consent", [False, True], ids=["decline", "accept"])
def test_confirmation_observes_verified_stage_and_untouched_source(local_project, monkeypatch, consent):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)
    observations = []

    def confirm_after_verification(*args, **kwargs):
        for relative, content in before.items():
            if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
                assert (workspace / relative).read_bytes() == content
        assert snapshot(repository) == before
        assert git_state(repository) == history
        assert not list((home / "data/specify/projects").glob("*.json"))
        assert not (workspace / ".specify/workspace.json").exists()
        sys.stdout.flush()
        sys.stderr.flush()
        preview = sys.stdout.buffer.getvalue().decode() + sys.stderr.buffer.getvalue().decode()
        assert str(repository) in preview
        assert str(workspace) in preview
        assert "verif" in preview.lower()
        assert f"{ACTIVE_FEATURE}/spec.md" in preview
        assert ".specify/memory/constitution.md" in preview
        observations.append(preview)
        return consent

    monkeypatch.setattr(typer, "confirm", confirm_after_verification)
    result = CliRunner().invoke(app, ["project", "move", str(workspace)])

    assert observations, result.output
    if consent:
        assert result.exit_code == 0, result.output
        assert_moved(repository, workspace, before, history)
    else:
        assert result.exit_code != 0
        assert_local(repository, home, before, history)
        assert_recovery(result.output, repository, workspace)


@pytest.mark.parametrize("answer", ["y\n", "n\n", ""], ids=["yes", "no", "eof"])
def test_move_requires_explicit_answer_and_preserves_git(local_project, answer):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace)], input=answer)

    if answer == "y\n":
        assert result.exit_code == 0, result.output
        assert_moved(repository, workspace, before, history)
    else:
        assert result.exit_code != 0
        assert_local(repository, home, before, history)
        assert (workspace / ACTIVE_FEATURE / "spec.md").read_bytes() == before[f"{ACTIVE_FEATURE}/spec.md"]
        assert_recovery(result.output, repository, workspace)


def test_noninteractive_move_without_flag_retains_stage_and_source(local_project):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)

    result = subprocess.run(
        [sys.executable, "-c", "from specify_cli import app; app()", "project", "move", str(workspace)],
        cwd=repository, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode != 0
    assert_local(repository, home, before, history)
    assert (workspace / ACTIVE_FEATURE / "spec.md").read_bytes() == before[f"{ACTIVE_FEATURE}/spec.md"]
    assert_recovery(result.stdout + result.stderr, repository, workspace)


@pytest.mark.parametrize("scope", ["project", "global"])
def test_cleanup_flag_moves_project_and_native_commands_continue(local_project, monkeypatch, scope):
    repository, workspace, home = local_project
    shared = home / "agent/commands"
    ownership = home / "data/specify/integrations/omp"
    if scope == "global":
        options_path = repository / ".specify/init-options.json"
        options = json.loads(options_path.read_text(encoding="utf-8"))
        options["command_scope"] = "global"
        options_path.write_text(json.dumps(options), encoding="utf-8")
        installed = CliRunner().invoke(app, ["integration", "install", "omp", "--global"])
        assert installed.exit_code == 0, installed.output
        launcher = shared / "speckit.plan.md"
        launcher.write_text(launcher.read_text(encoding="utf-8") + "\nKeep my global edit.\n", encoding="utf-8")
    shared_before, ownership_before = snapshot(shared), snapshot(ownership)
    before, history = snapshot(repository), git_state(repository)

    def unexpected_prompt(*args, **kwargs):
        pytest.fail("The explicit cleanup flag must not request more consent.")

    monkeypatch.setattr(typer, "confirm", unexpected_prompt)
    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code == 0, result.output
    info = assert_moved(repository, workspace, before, history)
    assert info["command_scope"] == scope
    assert "git" in result.output.lower() and "history" in result.output.lower()
    arguments = ["project", "command", "speckit.plan", "--json"]
    if scope == "global":
        invocation = re.search(r"`specify ([^`]+)`", launcher.read_text(encoding="utf-8")).group(1)
        arguments = shlex.split(invocation)
    command = CliRunner().invoke(app, arguments)
    assert command.exit_code == 0, command.output
    data = json.loads(command.stdout)
    assert data["feature_dir"] == str(workspace / ACTIVE_FEATURE)
    assert "User plan instructions." in data["content"]
    assert str(workspace / ".specify/scripts/bash/setup-plan.sh") in data["content"]
    plan = subprocess.run(
        py_cmd(workspace, "setup-plan", "--json"), cwd=repository,
        capture_output=True, text=True, timeout=30,
    )
    assert plan.returncode == 0, plan.stderr
    assert Path(json.loads(plan.stdout)["IMPL_PLAN"]) == workspace / ACTIVE_FEATURE / "plan.md"
    assert (workspace / ACTIVE_FEATURE / "plan.md").read_bytes() == b"# Project plan template\n"
    assert not (repository / "specs").exists()
    assert git_state(repository) == history
    assert snapshot(shared) == shared_before
    assert snapshot(ownership) == ownership_before
    assert not (repository / ".omp/commands").exists()
    assert not (workspace / ".omp/commands").exists()


@pytest.mark.parametrize("occupied", ["file", "unrelated-directory", "foreign-workspace"])
def test_occupied_destination_never_changes_source_or_destination(local_project, occupied):
    repository, workspace, home = local_project
    if occupied == "file":
        workspace.write_bytes(b"Unrelated destination file\n")
        destination_before = workspace.read_bytes()
    else:
        workspace.mkdir()
        (workspace / "notes.txt").write_bytes(b"Someone else's notes\n")
        if occupied == "foreign-workspace":
            (workspace / ".specify").mkdir()
            (workspace / ".specify/workspace.json").write_text(json.dumps({
                "schema_version": 1, "project_id": "550e8400-e29b-41d4-a716-446655440000",
            }), encoding="utf-8")
        destination_before = snapshot(workspace)
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert str(workspace) in result.output
    assert any(word in result.output.lower() for word in ("empty", "occupied", "exist", "foreign", "owned"))
    assert_local(repository, home, before, history)
    assert (workspace.read_bytes() if occupied == "file" else snapshot(workspace)) == destination_before


def test_local_commands_without_move_keep_feature_json_and_local_paths(local_project):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)

    command = CliRunner().invoke(app, ["project", "command", "speckit.plan", "--json"])
    assert command.exit_code == 0, command.output
    data = json.loads(command.stdout)
    assert data["workspace_root"] == str(repository)
    assert data["feature_dir"] == str(repository / ACTIVE_FEATURE)
    assert str(repository / ".specify/scripts/bash/setup-plan.sh") in data["content"]
    paths = subprocess.run(
        py_cmd(repository, "check-prerequisites", "--json", "--paths-only"),
        cwd=repository, capture_output=True, text=True, timeout=30,
    )
    assert paths.returncode == 0, paths.stderr
    assert Path(json.loads(paths.stdout)["FEATURE_DIR"]) == repository / ACTIVE_FEATURE
    assert_local(repository, home, before, history)
    assert not workspace.exists()


def test_failed_locator_write_restores_local_project_and_reports_stage(local_project, monkeypatch):
    from specify_cli.project import move

    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)
    atomic_json = move.atomic_json

    def refuse_locator(path, data):
        if Path(path) == repository / ".specify/project.json":
            raise OSError("Injected locator write failure")
        return atomic_json(path, data)

    monkeypatch.setattr(move, "atomic_json", refuse_locator)
    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert "Injected locator write failure" in result.output
    assert_local(repository, home, before, history)
    for relative, content in before.items():
        if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
            assert (workspace / relative).read_bytes() == content
    assert_recovery(result.output, repository, workspace)
