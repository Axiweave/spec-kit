from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from specify_cli.workspace_git import initialize_workspace_git, preflight_workspace_git


def test_ten_operation_lifecycle_never_commits_or_transfers(tmp_path, monkeypatch):
    """Invariant: feature and workflow operations leave initial history unchanged."""
    import json
    import shlex
    import sys
    from rich.console import Console
    from typer.testing import CliRunner
    from specify_cli import app
    from specify_cli.shared_infra import install_shared_infra
    from specify_cli.workspace import resolve_project, save_active_feature

    home = tmp_path / "home"
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "data"))
    for variable in ("SPECIFY_FEATURE", "SPECIFY_FEATURE_DIRECTORY", "SPECIFY_FEATURE_NO_PERSIST", "SPECIFY_INIT_DIR"):
        monkeypatch.delenv(variable, raising=False)
    repo = tmp_path / "code"
    root = tmp_path / "workspace"
    (repo / ".specify").mkdir(parents=True)
    (root / ".specify").mkdir(parents=True)
    identity = "550e8400-e29b-41d4-a716-446655440000"
    (repo / ".specify/project.json").write_text(json.dumps({
        "schema_version": 1, "storage": "external", "project_id": identity,
    }))
    (root / ".specify/workspace.json").write_text(json.dumps({
        "schema_version": 1, "project_id": identity,
    }))
    monkeypatch.chdir(repo)
    assert CliRunner().invoke(app, ["project", "link", str(root)]).exit_code == 0
    install_shared_infra(repo, "py", version="test", console=Console(quiet=True))
    (root / ".specify/init-options.json").write_text(json.dumps({"feature_selection": "automatic"}))
    generated = root / ".specify/scripts/python/check_prerequisites.py"
    head = initialize_workspace_git(root)
    project = resolve_project(repo)
    features = {}
    trace = tmp_path / "git-trace.log"
    monkeypatch.setenv("GIT_TRACE", str(trace))

    def helper(name, *args):
        result = subprocess.run(
            [sys.executable, str(root / ".specify/scripts/python" / name), "--json", *args],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)

    def create(label):
        result = helper("create_new_feature.py", "--short-name", label, f"Feature {label}")
        features[label] = Path(result["SPEC_FILE"]).parent

    def workflow(label):
        artifact = features[label] / "workflow-result.md"
        command = [sys.executable, "-c", f"from pathlib import Path; Path({str(artifact)!r}).write_text('Complete\\n')"]
        shell_command = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        fixture = tmp_path / f"{label}.yml"
        fixture.write_text(
            'schema_version: "1.0"\nworkflow:\n'
            f'  id: "lifecycle-{label}"\n  name: "Lifecycle"\n  version: "1.0.0"\n'
            'steps:\n  - id: write\n    type: shell\n'
            f"    run: {json.dumps(shell_command)}\n",
        )
        result = CliRunner().invoke(app, ["workflow", "run", str(fixture), "--json"])
        assert result.exit_code == 0, result.output
        assert artifact.read_text() == "Complete\n"

    operations = [
        lambda: create("alpha"),
        lambda: (features["alpha"] / "spec.md").write_text("# Alpha\n"),
        lambda: helper("setup_plan.py"),
        lambda: (features["alpha"] / "tasks.md").write_text("- [ ] Alpha task\n"),
        lambda: create("beta"),
        lambda: (features["beta"] / "spec.md").write_text("# Beta\n"),
        lambda: save_active_feature(project, features["alpha"]),
        lambda: workflow("alpha"),
        lambda: save_active_feature(project, features["beta"]),
        lambda: workflow("beta"),
    ]
    for number, operation in enumerate(operations, 1):
        operation()
        assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == head, number
        status = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True,
        )
        assert "specs/" in status, (number, status)
        assert "workflows/runs/" not in status
        assert "feature.json" not in status
        log = trace.read_text()
        assert not any(f"built-in: git {command}" in log for command in ("push", "pull", "fetch", "remote", "clone"))
    generated.write_text(generated.read_text() + "\n# User customization\n")
    assert ".specify/scripts/python/check_prerequisites.py" not in subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"], text=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "-f", str(generated)], check=True)
    assert ".specify/scripts/python/check_prerequisites.py" in subprocess.check_output(
        ["git", "-C", str(root), "diff", "--cached", "--name-only"], text=True,
    )
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip() == head


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    for name in tuple(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Workspace Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "workspace@example.invalid")


def test_preflight_is_read_only_for_absent_and_empty_destinations(tmp_path: Path):
    """Conservation: validation never creates workspace ownership or files."""
    for existing in (False, True):
        destination = tmp_path / str(existing) / "workspace ü"
        if existing:
            destination.mkdir(parents=True)
        before = set(tmp_path.rglob("*"))
        preflight_workspace_git(destination)
        assert set(tmp_path.rglob("*")) == before


def test_parent_repository_is_rejected_without_changes(tmp_path: Path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="repository"):
        preflight_workspace_git(tmp_path / "workspace")
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_missing_git_does_not_create_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "missing-bin"))
    destination = tmp_path / "workspace"
    with pytest.raises((OSError, ValueError), match="Git|git"):
        preflight_workspace_git(destination)
    assert not destination.exists()


def test_unusable_identity_refuses_before_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "")
    destination = tmp_path / "workspace"
    with pytest.raises((OSError, ValueError), match="identity|ident|name"):
        preflight_workspace_git(destination)
    assert not destination.exists()


def test_redirected_git_environment_cannot_select_code_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    code = tmp_path / "code"
    subprocess.run(["git", "init", str(code)], check=True, capture_output=True)
    before = {p.relative_to(code): p.read_bytes() for p in code.rglob("*") if p.is_file()}
    monkeypatch.setenv("GIT_DIR", str(code / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(code))
    monkeypatch.setenv("GIT_INDEX_FILE", str(code / ".git/index"))
    preflight_workspace_git(tmp_path / "workspace")
    assert {p.relative_to(code): p.read_bytes() for p in code.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("filename", ["notes ü.md", "notes [a]*?.md", "notes #!.md"])
def test_initial_history_tracks_only_durable_content(tmp_path: Path, filename: str):
    """Conservation: custom content survives while private and produced copies stay out."""
    import hashlib
    import json

    root = tmp_path / "workspace"
    files = {
        ".specify/workspace.json": '{"schema_version":1,"project_id":"test"}',
        ".specify/init-options.json": '{"script":"py"}',
        ".specify/memory/constitution.md": "# Constitution",
        "specs/001-a/spec.md": "# Specification",
        f"specs/.cache/{filename}": "Custom durable cache name",
        "specs/.backup/notes.md": "Custom durable backup name",
        ".specify/scripts/python/generated.py": "print('generated')",
        ".specify/scripts/python/recovered.py": "print('custom')",
        ".specify/scripts/python/modified.py": "print('edited')",
        ".specify/feature.json": '{"feature_directory":"specs/001-a"}',
        ".specify/extensions/tool/local-config.yml": "secret: token",
        ".specify/workflows/runs/one/inputs.json": '{"token":"secret"}',
        ".specify/presets/.cache/catalog.json": "{}",
        ".env": "TOKEN=secret",
        ".env.example": "TOKEN=replace",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    manifest = root / ".specify/integrations/speckit.manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "files": {
            name: hashlib.sha256(content.encode()).hexdigest()
            for name, content in files.items() if "/scripts/" in name
        },
        "recovered_files": [".specify/scripts/python/recovered.py", ".specify/scripts/python/modified.py"],
    }), encoding="utf-8")
    # A produced baseline differs from the current modified file.
    data = json.loads(manifest.read_text())
    data["recovered_files"].remove(".specify/scripts/python/modified.py")
    data["files"][".specify/scripts/python/modified.py"] = hashlib.sha256(b"old").hexdigest()
    manifest.write_text(json.dumps(data), encoding="utf-8")
    initial = initialize_workspace_git(root)
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotepath=false", "ls-tree", "-rz", "--name-only", "HEAD"],
        check=True, capture_output=True,
    )
    tracked = set(result.stdout.decode().strip("\0").split("\0"))
    private = {".specify/feature.json", ".specify/extensions/tool/local-config.yml",
               ".specify/workflows/runs/one/inputs.json", ".specify/presets/.cache/catalog.json", ".env"}
    expected = (set(files) - private - {".specify/scripts/python/generated.py"}) | {
        ".gitignore", ".specify/integrations/speckit.manifest.json",
    }
    assert tracked == expected
    assert subprocess.check_output(["git", "-C", str(root), "rev-list", "--count", "HEAD"]).strip() == b"1"
    assert subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip() == initial
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain"]) == b""
    assert all((root / name).read_text(encoding="utf-8") == content for name, content in files.items())


def test_required_content_hidden_by_user_ignore_refuses_without_force_add(tmp_path: Path):
    root = tmp_path / "workspace"
    (root / "specs/001").mkdir(parents=True)
    (root / "specs/001/spec.md").write_text("# Required")
    (root / ".gitignore").write_bytes(b"specs/\r\n")
    with pytest.raises(ValueError, match="ignored|required"):
        initialize_workspace_git(root)
    assert (root / ".gitignore").read_bytes() == b"specs/\r\n"
    assert not (root / ".git").exists()
    assert (root / "specs/001/spec.md").read_text() == "# Required"


@pytest.mark.parametrize("path", ["../outside", "/outside", "scripts/../../outside"])
def test_malformed_provenance_never_excludes_arbitrary_content(tmp_path: Path, path: str):
    import json

    root = tmp_path / "workspace"
    (root / ".specify/integrations").mkdir(parents=True)
    (root / ".specify/integrations/speckit.manifest.json").write_text(json.dumps({
        "files": {path: "0" * 64},
    }))
    with pytest.raises(ValueError, match="provenance|path"):
        initialize_workspace_git(root)
    assert not (root / ".git").exists()


@pytest.mark.parametrize("recovered", ["scripts/helper.py", {"scripts/helper.py": True}, [42]])
def test_malformed_recovered_provenance_refuses_before_git(tmp_path, recovered):
    import json

    root = tmp_path / "workspace"
    manifests = root / ".specify/integrations"
    manifests.mkdir(parents=True)
    (manifests / "speckit.manifest.json").write_text(json.dumps({
        "files": {"scripts/helper.py": "0" * 64},
        "recovered_files": recovered,
    }))
    with pytest.raises(ValueError, match="provenance"):
        initialize_workspace_git(root)
    assert not (root / ".git").exists()


def test_check_ignore_operational_failure_does_not_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("Keep user bytes")
    real_run = subprocess.run

    def fail_ignore(args, **kwargs):
        if "check-ignore" in args:
            return subprocess.CompletedProcess(args, 128, "", "broken Git configuration")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_ignore)
    with pytest.raises(ValueError, match="broken Git configuration"):
        initialize_workspace_git(root)
    assert not (root / ".git").exists()
    assert not (root / ".gitignore").exists()
    assert (root / "notes.md").read_text() == "Keep user bytes"


def test_nested_bare_repository_is_rejected(tmp_path: Path):
    subprocess.run(["git", "init", "--bare", str(tmp_path / "bare")], check=True, capture_output=True)
    with pytest.raises(ValueError, match="repository"):
        preflight_workspace_git(tmp_path / "bare/workspace")
    assert not (tmp_path / "bare/workspace").exists()


def test_bare_repository_refused_by_safe_bare_repository_setting_is_rejected(tmp_path: Path, monkeypatch):
    subprocess.run(["git", "init", "--bare", str(tmp_path / "bare")], check=True, capture_output=True)
    config = tmp_path / "gitconfig"
    config.write_text("[safe]\n\tbareRepository = explicit\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    with pytest.raises(ValueError, match="repository"):
        preflight_workspace_git(tmp_path / "bare/workspace")
    assert not (tmp_path / "bare/workspace").exists()


def test_custom_feature_files_are_committed_and_generated_transients_ignored(tmp_path: Path):
    """Conservation: any user file under specs/ is durable; only documented producer outputs stay out."""
    root = tmp_path / "workspace"
    custom = ["specs/001-x/spec.md", "specs/001-x/draft.tmp", "specs/001-x/notes.pyc",
              "specs/001-x/__pycache__/y.txt", "specs/001-x/a/b/__pycache__/x.txt"]
    generated = [".specify/scripts/python/__pycache__/common.cpython-312.pyc",
                 ".specify/extensions/git/scripts/python/__pycache__/git_common.cpython-312.pyc",
                 ".specify/workflows/.workflow-registry.json.abc123.tmp"]
    for name in custom + generated:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    initialize_workspace_git(root)
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-rz", "--name-only", "HEAD"], check=True, capture_output=True,
    ).stdout.decode().strip("\0").split("\0")
    assert set(tracked) == {*custom, ".gitignore"}
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"]) == b""


def test_symlink_destination_is_rejected_before_writes(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        preflight_workspace_git(alias / "workspace")
    assert list(target.iterdir()) == []


def test_later_private_outputs_stay_out_of_default_status(tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    initialize_workspace_git(root)
    for name in (".specify/workflows/runs/one/state.json", ".specify/workflows/.workflow-registry.json.x.tmp",
                 ".specify/extensions/.reinstall-staging-one/private.md",
                 ".specify/presets/.reinstall-staging-two/private.md",
                 ".specify/extensions/tool/tool-config.local.yml", "specs/.cache/custom.md"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("new content")
    assert subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"]) == (
        b"?? specs/.cache/custom.md\n"
    )


@pytest.mark.parametrize("phase", ["init", "add", "commit"])
def test_git_write_failure_preserves_user_bytes(tmp_path, monkeypatch, phase):
    """Conservation: an unsuccessful initial commit does not remove durable content."""
    root = tmp_path / "workspace"
    root.mkdir()
    note = root / "notes.md"
    note.write_bytes(b"User content\n")
    ignore = root / ".gitignore"
    ignore.write_bytes(b"# User rules\r\n*.log\r\n")
    real_run = subprocess.run

    def refuse(command, **kwargs):
        if phase in command[1:3]:
            return subprocess.CompletedProcess(command, 1, "", f"refused {phase}")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", refuse)
    with pytest.raises(ValueError, match=f"refused {phase}"):
        initialize_workspace_git(root)
    assert note.read_bytes() == b"User content\n"
    assert ignore.read_bytes() == b"# User rules\r\n*.log\r\n"
    assert not (root / ".git").exists()


def test_real_commit_hook_refusal_does_not_bypass_hook(tmp_path, monkeypatch):
    """Conservation: a rejecting hook prevents history and preserves user content."""
    if os.name == "nt":
        pytest.skip("This hook requires a native POSIX shell.")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_bytes(b"Keep this file\n")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\nprintf 'called' > '{marker}'\nexit 1\n")
    hook.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f'[core]\n\thooksPath = "{hooks}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    with pytest.raises(ValueError):
        initialize_workspace_git(root)
    assert marker.read_text() == "called"
    assert (root / "notes.md").read_bytes() == b"Keep this file\n"
    assert not (root / ".git").exists()


def test_write_timeout_preserves_content_and_removes_owned_partial_git(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_bytes(b"Keep this file\n")
    real_run = subprocess.run

    def timeout_commit(command, **kwargs):
        if "commit" in command[1:3]:
            assert kwargs["timeout"] == 120
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", timeout_commit)
    with pytest.raises(ValueError, match="Workspace Git failed"):
        initialize_workspace_git(root)
    assert (root / "notes.md").read_bytes() == b"Keep this file\n"
    assert not (root / ".git").exists()


def test_signing_refusal_keeps_normal_git_policy(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("This signing stub requires a native POSIX shell.")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_bytes(b"Keep this file\n")
    marker = tmp_path / "signer-ran"
    signer = tmp_path / "refuse-signing"
    signer.write_text(f"#!/bin/sh\nprintf 'called' > '{marker}'\nexit 1\n")
    signer.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f'[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = "{signer}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    with pytest.raises(ValueError):
        initialize_workspace_git(root)
    assert marker.read_text() == "called"
    assert (root / "notes.md").read_bytes() == b"Keep this file\n"
    assert not (root / ".git").exists()


def test_post_commit_probe_failure_retains_exact_history(tmp_path, monkeypatch):
    from specify_cli.workspace_git import WorkspaceGitError

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_bytes(b"Committed content\n")
    real_run = subprocess.run

    def fail_report(command, **kwargs):
        if command[1:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(command, 128, "", "report failed")
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_report)
    with pytest.raises(WorkspaceGitError) as failure:
        initialize_workspace_git(root)
    assert failure.value.committed
    assert real_run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip() == "1"
    assert real_run(
        ["git", "-C", str(root), "show", "HEAD:notes.md"],
        check=True, capture_output=True,
    ).stdout == b"Committed content\n"


def test_symlink_race_before_staging_preserves_unrelated_target(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    note = root / "notes.md"
    note.write_bytes(b"Original content\n")
    target = tmp_path / "unrelated.md"
    target.write_bytes(b"Do not stage or change\n")
    real_run = subprocess.run
    staged = []

    def replace_before_staging(command, **kwargs):
        if "add" in command[1:3]:
            staged.append(command)
        result = real_run(command, **kwargs)
        if "check-ignore" in command[1:3]:
            note.unlink()
            note.symlink_to(target)
        return result

    monkeypatch.setattr(subprocess, "run", replace_before_staging)
    with pytest.raises(ValueError, match="changed before staging"):
        initialize_workspace_git(root)
    assert not staged
    assert note.is_symlink()
    assert target.read_bytes() == b"Do not stage or change\n"
    assert not (root / ".git").exists()


def test_global_worktree_setting_cannot_stage_foreign_content(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (root / "notes.md").write_bytes(b"Workspace bytes\n")
    (foreign / "notes.md").write_bytes(b"Private foreign bytes\n")
    config = tmp_path / "gitconfig"
    config.write_text(f'[core]\n\tworktree = "{foreign}"\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    initialize_workspace_git(root)
    assert subprocess.check_output(["git", "-C", str(root), "show", "HEAD:notes.md"]) == b"Workspace bytes\n"
    assert (foreign / "notes.md").read_bytes() == b"Private foreign bytes\n"
    assert not (foreign / ".git").exists()


def test_changed_worktree_root_refuses_before_staging(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (root / "notes.md").write_bytes(b"Workspace bytes\n")
    (foreign / "notes.md").write_bytes(b"Private foreign bytes\n")
    real_run = subprocess.run

    def change_worktree(command, **kwargs):
        result = real_run(command, **kwargs)
        if command[1:] == ["init"]:
            real_run(
                ["git", "-C", str(root), "config", "core.worktree", str(foreign)],
                check=True, capture_output=True,
            )
        return result

    monkeypatch.setattr(subprocess, "run", change_worktree)
    with pytest.raises(ValueError, match="worktree root"):
        initialize_workspace_git(root)
    assert (foreign / "notes.md").read_bytes() == b"Private foreign bytes\n"
    assert not (foreign / ".git").exists()
    assert not (root / ".git").exists()
