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


def tracked(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", "HEAD"],
        capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    return set(result.stdout.decode().splitlines())


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
            "feature_numbering": "timestamp", "feature_selection": "automatic", "command_scope": "project",
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
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Workspace Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "workspace@example.invalid")
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


def assert_moved(
    repository: Path, workspace: Path, before: dict[str, bytes], history: tuple,
    *, no_workspace_git: bool = False,
) -> dict:
    for relative, content in before.items():
        if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
            assert (workspace / relative).read_bytes() == content
        elif not relative.startswith((".specify/", "specs/")):
            assert (repository / relative).read_bytes() == content
    assert not (repository / "specs").exists()
    assert set(snapshot(repository / ".specify")) == {"project.json"}
    assert not (workspace / ".specify/project.json").exists()
    assert not (workspace / ".specify/feature.json").exists()
    assert git_state(repository) == history
    if no_workspace_git:
        assert not (workspace / ".git").exists()
    else:
        assert (workspace / ".git").is_dir()
        assert git(workspace, "rev-list", "--count", "HEAD").strip() == b"1"
        assert git(workspace, "status", "--porcelain") == b""
    info = project_info(repository)
    assert info["storage"] == "external"
    assert info["repository_root"] == str(repository)
    assert info["workspace_root"] == str(workspace)
    assert info["active_feature"] == ACTIVE_FEATURE
    assert info["feature_numbering"] == "timestamp"
    assert info["feature_selection"] == "automatic"
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
        assert not (workspace / ".git").exists()


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
        assert not (workspace / ".git").exists()


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
    assert not (workspace / ".git").exists()


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
    assert re.search(r"\b[0-9a-f]{7,40}\b", result.output), result.output
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


@pytest.mark.parametrize("no_workspace_git", [False, True], ids=["default", "no-workspace-git"])
@pytest.mark.parametrize(
    "occupied", ["file", "unrelated-directory", "foreign-workspace", "git-repo", "git-worktree-file"],
)
def test_occupied_destination_never_changes_source_or_destination(local_project, occupied, no_workspace_git):
    repository, workspace, home = local_project
    if occupied == "file":
        workspace.write_bytes(b"Unrelated destination file\n")
    else:
        workspace.mkdir()
        if occupied == "git-repo":
            subprocess.run(["git", "init", "-q", str(workspace)], check=True)
        elif occupied == "git-worktree-file":
            (workspace / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        else:
            (workspace / "notes.txt").write_bytes(b"Someone else's notes\n")
            if occupied == "foreign-workspace":
                (workspace / ".specify").mkdir()
                (workspace / ".specify/workspace.json").write_text(json.dumps({
                    "schema_version": 1, "project_id": "550e8400-e29b-41d4-a716-446655440000",
                }), encoding="utf-8")
    destination_before = (
        workspace.read_bytes() if occupied == "file"
        else {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    )
    before, history = snapshot(repository), git_state(repository)

    arguments = ["project", "move", str(workspace), "--confirm-remove-local"]
    if no_workspace_git:
        arguments.append("--no-workspace-git")
    result = CliRunner().invoke(app, arguments)

    assert result.exit_code != 0
    assert str(workspace) in result.output
    assert any(word in result.output.lower() for word in ("empty", "occupied", "exist", "foreign", "owned"))
    assert_local(repository, home, before, history)
    destination_after = (
        workspace.read_bytes() if occupied == "file"
        else {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
    )
    assert destination_after == destination_before


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


def test_default_migration_creates_one_commit_with_durable_history(local_project):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code == 0, result.output
    assert_moved(repository, workspace, before, history)
    present = tracked(workspace)
    for relative in (
        ".specify/workspace.json", ".specify/init-options.json",
        ".specify/memory/constitution.md",
        ".specify/templates/plan-template.md", ".specify/templates/commands/plan.md",
        ".specify/custom/edited.bin",
        "specs/001-complete/spec.md", "specs/001-complete/tasks.md",
        f"{ACTIVE_FEATURE}/spec.md",
    ):
        assert relative in present, f"{relative} missing from initial commit: {sorted(present)}"
    assert ".specify/feature.json" not in present
    commit_id = git(workspace, "rev-parse", "HEAD").decode().strip()
    assert re.fullmatch(r"[0-9a-f]{40}", commit_id)
    assert commit_id in result.output
    lower = result.output.lower()
    assert "code repository" in lower and "unchanged" in lower
    assert "workspace" in lower and "git" in lower and "history" in lower


def test_direct_library_call_defaults_to_workspace_history(local_project):
    from specify_cli.project.move import commit_move, prepare_move

    repository, workspace, home = local_project
    prepared = prepare_move(repository, workspace)

    assert prepared.no_workspace_git is False

    report: dict[str, str] = {}
    project = commit_move(prepared, report=report)

    assert project.storage == "external"
    assert (workspace / ".git").is_dir()
    assert report["commit_id"] == git(workspace, "rev-parse", "HEAD").decode().strip()


def test_no_workspace_git_skips_all_probes_and_writes(local_project, monkeypatch, tmp_path):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)

    with monkeypatch.context() as blocked:
        blocked.setenv("PATH", str(tmp_path / "no-git-on-path"))
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            blocked.delenv(name, raising=False)
        result = CliRunner().invoke(app, [
            "project", "move", str(workspace), "--confirm-remove-local", "--no-workspace-git",
        ])

    assert result.exit_code == 0, result.output
    assert "skip" in result.output.lower()
    assert_moved(repository, workspace, before, history, no_workspace_git=True)


def test_parent_git_repository_refuses_default_but_not_opt_out(local_project):
    repository, workspace, home = local_project
    controlled = workspace.parent / "git-controlled-area"
    subprocess.run(["git", "init", "-q", str(controlled)], check=True)
    nested = controlled / "nested workspace"
    before, history = snapshot(repository), git_state(repository)

    refused = CliRunner().invoke(app, ["project", "move", str(nested), "--confirm-remove-local"])

    assert refused.exit_code != 0
    assert "repository" in refused.output.lower() or "git" in refused.output.lower()
    assert not nested.exists()
    assert_local(repository, home, before, history)

    result = CliRunner().invoke(app, [
        "project", "move", str(nested), "--confirm-remove-local", "--no-workspace-git",
    ])

    assert result.exit_code == 0, result.output
    assert_moved(repository, nested, before, history, no_workspace_git=True)


def test_missing_identity_refuses_before_any_write(local_project, monkeypatch):
    repository, workspace, home = local_project
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert any(word in result.output.lower() for word in ("identity", "ident", "name"))
    assert "--no-workspace-git" in result.output
    assert not workspace.exists()
    assert_local(repository, home, before, history)


def test_commit_hook_refusal_restores_local_project_before_commit(local_project, monkeypatch, tmp_path):
    repository, workspace, home = local_project
    template = tmp_path / "git-hook-template/hooks"
    template.mkdir(parents=True)
    hook = template / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'Blocked by test hook' >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template.parent))
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert_local(repository, home, before, history)
    for relative, content in before.items():
        if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
            assert (workspace / relative).read_bytes() == content
    assert not (workspace / ".git").exists()
    assert_recovery(result.output, repository, workspace)


def test_post_commit_cleanup_failure_restores_local_and_keeps_workspace_history(local_project, monkeypatch):
    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)
    real_rmtree = shutil.rmtree

    def flaky_rmtree(path, *args, **kwargs):
        root = Path(path)
        if root.name.startswith(".specify-move-"):
            victim = next(candidate for candidate in root.rglob("*") if candidate.is_file())
            victim.unlink()
            raise OSError("Injected cleanup failure after the workspace commit.")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert_local(repository, home, before, history)
    for relative, content in before.items():
        if relative.startswith((".specify/", "specs/")) and relative != ".specify/feature.json":
            assert (workspace / relative).read_bytes() == content
    assert (workspace / ".git").is_dir()
    assert git(workspace, "rev-list", "--count", "HEAD").strip() == b"1"
    assert git(workspace, "status", "--porcelain") == b""
    assert str(workspace) in result.output
    assert "commit" in result.output.lower()


def test_workspace_git_error_marked_committed_preserves_identity_and_skips_metadata_restore(local_project, monkeypatch):
    from specify_cli.project import move

    repository, workspace, home = local_project
    before, history = snapshot(repository), git_state(repository)
    committed_constitution = b"# Committed constitution\nThis reflects the durable commit.\n"

    def fake_initialize(target: Path) -> str:
        (target / ".specify/memory/constitution.md").write_bytes(committed_constitution)
        raise move.WorkspaceGitError("Simulated post-commit Git failure.", committed=True)

    monkeypatch.setattr(move, "initialize_workspace_git", fake_initialize)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert (workspace / ".specify/memory/constitution.md").read_bytes() == committed_constitution
    identity = json.loads((workspace / ".specify/workspace.json").read_text(encoding="utf-8"))
    assert identity["schema_version"] == 1
    assert identity["project_id"]
    assert_local(repository, home, before, history)


def test_recovery_failure_after_refresh_retains_evidence_without_deleting_it(local_project, monkeypatch):
    from specify_cli.project import _command_move_managed

    repository, workspace, home = local_project
    before = snapshot(repository)
    real_refresh = _command_move_managed.refresh_commands

    def blocked_refresh(repo: Path) -> None:
        real_refresh(repo)
        (repo / "specs").write_bytes(b"A concurrent process claimed this path.\n")
        raise ValueError("Injected failure after refresh, before the Git commit.")

    monkeypatch.setattr(_command_move_managed, "refresh_commands", blocked_refresh)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert any(word in result.output.lower() for word in ("recovery", "attention"))
    assert str(workspace) in result.output
    assert (repository / ".specify/memory/constitution.md").read_bytes() == before[".specify/memory/constitution.md"]
    assert (repository / ".specify/init-options.json").read_bytes() == before[".specify/init-options.json"]
    assert (repository / "specs").read_bytes() == b"A concurrent process claimed this path.\n"
    recovery_dirs = [p for p in repository.glob(".specify-move-*") if p.is_dir()]
    assert recovery_dirs, "The recovery directory must remain for manual inspection."
    assert (recovery_dirs[0] / "specs/001-complete/spec.md").read_bytes() == before["specs/001-complete/spec.md"]
