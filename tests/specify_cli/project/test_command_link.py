"""Explicit relinking preserves shared identity and changes only machine state."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.workspace_git import initialize_workspace_git

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
        json.dumps({
            "integration": "omp", "script": "sh", "feature_numbering": "timestamp",
            "feature_selection": "automatic",
        }),
        encoding="utf-8",
    )
    feature = workspace / "specs/001-existing"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Existing specification\n", encoding="utf-8")
    config = home / "config/specify/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"storage_root": str(workspace)}), encoding="utf-8")
    record = repo / ".specify/checkout.json"
    monkeypatch.chdir(repo)
    return repo, workspace, record


def test_real_workspace_clone_relinks_without_changing_history(external_project, tmp_path, monkeypatch):
    """Invariant: relinking changes machine state, not shared identity or committed data."""
    repo, workspace, record = external_project
    for name, value in {
        "GIT_AUTHOR_NAME": "Workspace Test",
        "GIT_AUTHOR_EMAIL": "workspace@example.test",
        "GIT_COMMITTER_NAME": "Workspace Test",
        "GIT_COMMITTER_EMAIL": "workspace@example.test",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "absent-git-config"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)
    # The first link completes the workspace, so the commit ignores its generated files.
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    head = initialize_workspace_git(workspace)
    remote = tmp_path / "shared workspace remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(workspace), "push", str(remote), "HEAD:refs/heads/shared"],
        check=True, capture_output=True,
    )
    clone = tmp_path / "shared workspace clone"
    subprocess.run(
        ["git", "clone", "--branch", "shared", str(remote), str(clone)],
        check=True, capture_output=True,
    )
    durable = {
        path.relative_to(clone).as_posix(): path.read_bytes()
        for path in clone.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(clone).parts
    }
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["project", "link", str(clone)])
    assert result.exit_code == 0, result.output
    state = json.loads(record.read_text(encoding="utf-8"))
    assert state["workspace"] == str(clone)
    assert state["active_feature"] is None
    assert json.loads((clone / ".specify/workspace.json").read_text())["project_id"] == PROJECT_ID
    assert subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip() == head
    assert subprocess.check_output(
        ["git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all"], text=True,
    ) == ""
    after = {
        path.relative_to(clone).as_posix(): path.read_bytes()
        for path in clone.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(clone).parts
    }
    assert {name: after[name] for name in durable} == durable


def test_cloned_workspace_restores_helpers_without_losing_custom_content(
    external_project, tmp_path, monkeypatch,
):
    """Invariant: explicit restoration preserves custom bytes and the initial HEAD."""
    import sys
    from rich.console import Console
    from specify_cli.shared_infra import install_shared_infra
    from specify_cli.workspace import resolve_project, save_active_feature

    repo, workspace, _ = external_project
    for name, value in {
        "GIT_AUTHOR_NAME": "Workspace Test",
        "GIT_AUTHOR_EMAIL": "workspace@example.test",
        "GIT_COMMITTER_NAME": "Workspace Test",
        "GIT_COMMITTER_EMAIL": "workspace@example.test",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "absent-git-config"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(name, value)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    console = Console(quiet=True)
    install_shared_infra(repo, "py", version="test", console=console)
    custom = workspace / ".specify/templates/spec-template.md"
    custom.write_text("# Custom template retained after sharing\n", encoding="utf-8")
    context = workspace / ".specify/memory/constitution.md"
    context.parent.mkdir(parents=True, exist_ok=True)
    context.write_text("# Shared project principles\n", encoding="utf-8")
    head = initialize_workspace_git(workspace)
    clone = tmp_path / "helper workspace clone"
    subprocess.run(["git", "clone", str(workspace), str(clone)], check=True, capture_output=True)
    helper = clone / ".specify/scripts/python/check_prerequisites.py"
    assert not helper.exists()
    assert CliRunner().invoke(app, ["project", "link", str(clone)]).exit_code == 0
    assert helper.is_file()
    assert (clone / ".specify/templates/plan-template.md").is_file()
    assert subprocess.check_output(
        ["git", "-C", str(clone), "status", "--porcelain", "--untracked-files=all"], text=True,
    ) == ""
    project = resolve_project(repo)
    save_active_feature(project, clone / "specs/001-existing")
    result = subprocess.run(
        [sys.executable, str(helper), "--json", "--paths-only"],
        cwd=repo, check=True, capture_output=True, text=True,
    )
    paths = json.loads(result.stdout)
    assert Path(paths["FEATURE_DIR"]) == clone / "specs/001-existing"
    assert (clone / custom.relative_to(workspace)).read_bytes() == custom.read_bytes()
    assert (clone / context.relative_to(workspace)).read_bytes() == context.read_bytes()
    assert subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip() == head
    assert subprocess.check_output(["git", "-C", str(clone), "diff", "--cached", "--name-only"], text=True) == ""


def test_clone_restores_all_package_types_and_preserves_custom_overrides(
    external_project, tmp_path, monkeypatch,
):
    """Conservation: explicit package restoration preserves shared edits and history."""
    import socket
    import sys
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    repo, workspace, _ = external_project
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Clone Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "clone@example.test")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-git-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    home = tmp_path / "package-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "config"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    runner = CliRunner()
    assert runner.invoke(app, ["project", "link", str(workspace)]).exit_code == 0

    sources = tmp_path / "package-sources"
    extension, preset, workflow, step = [sources / name for name in ("extension", "preset", "workflow", "step")]

    def put(root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    put(extension, "extension.yml", json.dumps({
        "schema_version": "1.0",
        "extension": {"id": "clone-extension", "name": "Clone Extension", "version": "1.0.0", "description": "Clone fixture"},
        "requires": {"speckit_version": ">=0.1.0"},
        "provides": {"commands": [{"name": "speckit.clone-extension.hello", "file": "commands/hello.md"}]},
    }))
    put(extension, "commands/hello.md", "# Original command\n")
    put(extension, "scripts/tool.py", "print('extension restored')\n")
    put(preset, "preset.yml", json.dumps({
        "schema_version": "1.0",
        "preset": {"id": "clone-preset", "name": "Clone Preset", "version": "1.0.0", "description": "Clone fixture"},
        "requires": {"speckit_version": ">=0.1.0"},
        "provides": {"templates": [{"type": "template", "name": "clone-note", "file": "templates/clone-note.md"}]},
    }))
    put(preset, "templates/clone-note.md", "# Original template\n")
    put(preset, "scripts/tool.py", "print('preset restored')\n")
    put(workflow, "workflow.yml", (
        'schema_version: "1.0"\nworkflow:\n  id: clone-workflow\n'
        '  name: Clone Workflow\n  version: "1.0.0"\nsteps:\n'
        '  - id: hello\n    type: shell\n    run: "echo restored"\n'
    ))
    put(workflow, "helper.py", "print('original workflow helper')\n")
    put(step, "step.yml", "step:\n  type_key: clone-step\n")
    put(step, "__init__.py", "def restored():\n    return 'step restored'\n")
    put(step, "helper.py", "print('original step helper')\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(sources)))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    put(sources, "catalog.json", json.dumps({"steps": {"clone-step": {
        "name": "Clone Step", "version": "1.0.0",
        "url": f"{base}/step/step.yml", "init_url": f"{base}/step/__init__.py",
        "extra_files": {"helper.py": f"{base}/step/helper.py"},
    }}}))
    monkeypatch.setenv("SPECKIT_STEP_CATALOG_URL", f"{base}/catalog.json")
    connect, connections = socket.socket.connect, []

    def loopback_only(sock, address):
        connections.append(address)
        if address[0] != "127.0.0.1":
            raise OSError("Outbound access is blocked in the clone check.")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    try:
        for command in (
            ["extension", "add", str(extension), "--dev"],
            ["preset", "add", "clone-preset", "--dev", str(preset)],
            ["workflow", "add", str(workflow), "--dev"],
            ["workflow", "step", "add", "clone-step"],
        ):
            result = runner.invoke(app, command, catch_exceptions=False)
            assert result.exit_code == 0, result.output
        edits = {
            ".specify/extensions/clone-extension/commands/hello.md": "# Edited shared command\n",
            ".specify/presets/clone-preset/templates/clone-note.md": "# Edited shared template\n",
            ".specify/workflows/clone-workflow/helper.py": "print('edited workflow helper')\n",
            ".specify/workflows/steps/clone-step/helper.py": "print('edited step helper')\n",
        }
        for name, content in edits.items():
            (workspace / name).write_text(content, encoding="utf-8")
        head = initialize_workspace_git(workspace)
        clone = tmp_path / "package workspace clone"
        subprocess.run(["git", "clone", str(workspace), str(clone)], check=True, capture_output=True)
        missing = [
            ".specify/extensions/clone-extension/scripts/tool.py",
            ".specify/presets/clone-preset/scripts/tool.py",
            ".specify/workflows/clone-workflow/workflow.yml",
            ".specify/workflows/steps/clone-step/__init__.py",
        ]
        assert all(not (clone / name).exists() for name in missing)
        connections.clear()
        linked = runner.invoke(app, ["project", "link", str(clone)])
        assert linked.exit_code == 0, linked.output
        assert connections == []
        for package, repair in (
            ("clone-extension", "specify extension add clone-extension --force"),
            ("clone-preset", "specify preset update clone-preset"),
            ("clone-workflow", "specify workflow add clone-workflow"),
            ("clone-step", "specify workflow step add clone-step --force"),
        ):
            assert f"Missing package files: {package}\nRepair: {repair}\n" in linked.output
        assert all(not (clone / name).exists() for name in missing)
        assert (clone / ".specify/scripts/bash/check-prerequisites.sh").is_file()
        assert (clone / ".specify/templates/spec-template.md").is_file()
        for command in (
            ["extension", "add", str(extension), "--dev", "--force"],
            ["preset", "update", "clone-preset", "--dev", str(preset)],
            ["workflow", "add", str(workflow), "--dev"],
            ["workflow", "step", "add", "clone-step"],
        ):
            result = runner.invoke(app, command, catch_exceptions=False)
            assert result.exit_code == 0, result.output
        for name, content in edits.items():
            assert (clone / name).read_text(encoding="utf-8") == content
        for name, expected in zip(missing[:2], ("extension restored", "preset restored"), strict=True):
            assert subprocess.check_output([sys.executable, str(clone / name)], text=True).strip() == expected
        restored = runner.invoke(app, ["workflow", "run", str(clone / missing[2]), "--json"])
        assert restored.exit_code == 0, restored.output
        assert json.loads(restored.stdout)["status"] == "completed"
        assert subprocess.check_output(["git", "-C", str(clone), "rev-parse", "HEAD"], text=True).strip() == head
        assert subprocess.check_output(["git", "-C", str(clone), "diff", "--cached", "--name-only"], text=True) == ""
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_link_clone_without_machine_record_preserves_shared_files(external_project, tmp_path, monkeypatch):
    original, workspace, _ = external_project
    # The first link completes the workspace and the ignore line.
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    clone = tmp_path / "cloned repository"
    shutil.copytree(original, clone)
    record = clone / ".specify/checkout.json"
    record.unlink()
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


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True, text=True).stdout


def _status(directory: Path) -> str:
    return _git(directory, "status", "--porcelain", "--untracked-files=all")


@pytest.fixture
def private_project(external_project, tmp_path, monkeypatch):
    """A committed code repository with a private claude project, plus a worktree and a clone."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Team")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "team@example.com")
    main, workspace = tmp_path / "main", tmp_path / "private workspace"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_text("team\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team")
    monkeypatch.chdir(main)
    result = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(workspace), "--private", "--script", "sh", "--no-workspace-git",
    ])
    assert result.exit_code == 0, result.output
    _git(main, "worktree", "add", "-q", str(tmp_path / "worktree"))
    _git(tmp_path, "clone", "-q", str(main), str(tmp_path / "clone"))
    return main, workspace.resolve(), tmp_path / "worktree", tmp_path / "clone"


@pytest.mark.parametrize("checkout", ["worktree", "clone"])
def test_link_attaches_checkout_to_private_workspace_with_clean_status(private_project, monkeypatch, checkout):
    main, workspace, worktree, clone = private_project
    target = worktree if checkout == "worktree" else clone
    monkeypatch.chdir(target)
    result = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert result.exit_code == 0, result.output
    assert f"Workspace: {workspace}" in result.output
    assert (json.loads((target / ".specify/project.json").read_text())["project_id"]
            == json.loads((main / ".specify/project.json").read_text())["project_id"])
    assert (target / ".claude/skills/speckit-plan/SKILL.md").is_file()
    assert _status(target) == "" and _status(main) == ""
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert json.loads(info.stdout)["workspace_root"] == str(workspace)


@pytest.mark.parametrize("fault", ["not-a-workspace", "tracked-target"])
def test_link_attach_refusals_write_nothing(private_project, tmp_path, monkeypatch, fault):
    _, workspace, _, clone = private_project
    message = "is not a Spec Kit workspace."
    if fault == "not-a-workspace":
        workspace = tmp_path / "nothing"
        workspace.mkdir()
    else:
        (clone / ".claude/skills/speckit-plan").mkdir(parents=True)
        (clone / ".claude/skills/speckit-plan/SKILL.md").write_text("team\n", encoding="utf-8")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-q", "-m", "team skill")
        message = "Spec Kit would change tracked file .claude/skills/speckit-plan/SKILL.md."
    before = snapshot(tmp_path)
    monkeypatch.chdir(clone)
    result = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert result.exit_code == 1
    assert message in result.output.replace("\n", "")
    assert snapshot(tmp_path) == before


def test_link_reconciles_attached_checkout_with_workspace(private_project, monkeypatch):
    main, workspace, worktree, _ = private_project
    monkeypatch.chdir(worktree)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    state_path = workspace / ".specify/integration.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    state["installed_integrations"] = ["claude", "codex"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    assert (worktree / ".specify/integrations/codex.manifest.json").is_file()
    assert (worktree / ".agents/skills/speckit-plan/SKILL.md").is_file()
    assert _status(worktree) == "" and _status(main) == ""

    state["installed_integrations"] = ["claude"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    assert not (worktree / ".specify/integrations/codex.manifest.json").exists()
    assert not (worktree / ".agents").exists()

    checkout_manifest = worktree / ".specify/integrations/claude.manifest.json"
    data = json.loads(checkout_manifest.read_text(encoding="utf-8"))
    checkout_manifest.write_text(json.dumps({**data, "version": "0.0.0"}), encoding="utf-8")
    (worktree / ".claude/skills/speckit-plan/SKILL.md").unlink()
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    shared = json.loads((workspace / ".specify/integrations/claude.manifest.json").read_text(encoding="utf-8"))
    assert json.loads(checkout_manifest.read_text(encoding="utf-8"))["version"] == shared["version"]
    assert (worktree / ".claude/skills/speckit-plan/SKILL.md").is_file()
    assert _status(worktree) == "" and _status(main) == ""


def test_attached_link_refusal_leaves_machine_record_unchanged(private_project, monkeypatch):
    main, workspace, worktree, _ = private_project
    monkeypatch.chdir(worktree)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    (worktree / ".agents/skills/speckit-plan").mkdir(parents=True)
    (worktree / ".agents/skills/speckit-plan/SKILL.md").write_text("team\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-q", "-m", "team skill")
    state_path = workspace / ".specify/integration.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state_path.write_text(json.dumps({**state, "installed_integrations": ["claude", "codex"]}), encoding="utf-8")
    record = worktree / ".specify/checkout.json"
    record.write_text(json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": "gone"}))
    before = record.read_bytes()

    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 1
    assert "Spec Kit would change tracked file .agents/skills/speckit-plan/SKILL.md." in result.output.replace("\n", "")
    assert record.read_bytes() == before
    assert not (worktree / ".specify/integrations/codex.manifest.json").exists()


def tree(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under *root*: a created and left-behind empty folder counts as a change."""
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
    }


def _link(workspace: Path):
    return CliRunner().invoke(app, ["project", "link", str(workspace)])


def _drop_ignore_line(checkout: Path) -> None:
    ignore = checkout / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8").replace("/.specify/checkout.json\n", ""), encoding="utf-8")


@pytest.fixture
def shared_project(external_project, tmp_path, monkeypatch):
    """A committed non-private claude project with a committed workspace, plus a worktree and a clone."""
    for name in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(name, "Team")
    for name in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(name, "team@example.com")
    main, workspace = tmp_path / "main", tmp_path / "team workspace"
    main.mkdir()
    _git(main, "init", "-q")
    monkeypatch.chdir(main)
    result = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(workspace), "--script", "sh",
    ])
    assert result.exit_code == 0, result.output
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team")
    assert _status(workspace) == ""
    _git(main, "worktree", "add", "-q", str(tmp_path / "worktree"))
    _git(tmp_path, "clone", "-q", str(main), str(tmp_path / "clone"))
    return main, workspace.resolve(), tmp_path / "worktree", tmp_path / "clone"


def test_link_restores_worktree_commands_and_the_helper_runs(shared_project, monkeypatch):
    main, workspace, worktree, _ = shared_project
    skills = {path.relative_to(main): path.read_bytes() for path in (main / ".claude/skills").rglob("SKILL.md")}
    assert skills and not (worktree / ".claude").exists()
    monkeypatch.chdir(worktree)

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert f"Restored: {len(skills)} files" in result.output
    assert {name: (worktree / name).read_bytes() for name in skills} == skills
    feature = workspace / "specs/001-demo"
    feature.mkdir(parents=True)
    helper = subprocess.run(
        ["bash", str(workspace / ".specify/scripts/bash/check-prerequisites.sh"), "--json", "--paths-only"],
        cwd=worktree, env={**os.environ, "SPECIFY_FEATURE_DIRECTORY": "specs/001-demo"},
        capture_output=True, text=True, check=True,
    )
    assert Path(json.loads(helper.stdout)["FEATURE_DIR"]) == feature
    assert _status(worktree) == "" and _status(main) == ""


def test_link_restores_a_cloned_workspace_on_a_second_machine(shared_project, tmp_path, monkeypatch):
    _, workspace, _, clone = shared_project
    copy = tmp_path / "workspace clone"
    _git(tmp_path, "clone", "-q", str(workspace), str(copy))
    workflow = ".specify/workflows/speckit/workflow.yml"
    assert (workspace / workflow).is_file() and not (copy / workflow).exists()
    assert not (copy / ".specify/scripts").exists() and not (copy / ".specify/templates").exists()
    home = tmp_path / "second home"
    for name, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data", "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.chdir(clone)

    result = _link(copy)

    assert result.exit_code == 0, result.output
    for folder in (".specify/scripts", ".specify/templates", ".specify/workflows/speckit"):
        expected = {path.relative_to(workspace): path.read_bytes() for path in (workspace / folder).rglob("*") if path.is_file()}
        assert expected and {name: (copy / name).read_bytes() for name in expected} == expected
    assert "Missing package files" not in result.output
    assert (clone / ".claude/skills/speckit-plan/SKILL.md").is_file()
    assert _status(copy) == "" and _status(clone) == ""
    loaded = CliRunner().invoke(app, ["workflow", "info", "speckit"])
    assert loaded.exit_code == 0, loaded.output
    assert "Full SDD Cycle" in loaded.output


def test_link_keeps_a_modified_skill_and_restores_a_deleted_one(shared_project, monkeypatch):
    main, workspace, worktree, _ = shared_project
    monkeypatch.chdir(worktree)
    assert _link(workspace).exit_code == 0
    edited = worktree / ".claude/skills/speckit-plan/SKILL.md"
    edited.write_text("# My plan\n", encoding="utf-8")
    shutil.rmtree(worktree / ".claude/skills/speckit-tasks")

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert "Restored: 1 files\n" in result.output
    assert (
        "Kept modified file: .claude/skills/speckit-plan/SKILL.md\n"
        "Repair: specify integration upgrade claude --force\n"
    ) in result.output
    assert edited.read_text(encoding="utf-8") == "# My plan\n"
    restored = ".claude/skills/speckit-tasks/SKILL.md"
    assert (worktree / restored).read_bytes() == (main / restored).read_bytes()
    assert _status(workspace) == ""


def test_second_link_changes_no_file(shared_project, tmp_path, monkeypatch):
    _, workspace, worktree, _ = shared_project
    monkeypatch.chdir(worktree)
    assert _link(workspace).exit_code == 0
    before = tree(tmp_path)

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert "Restored" not in result.output and "Updated" not in result.output
    assert tree(tmp_path) == before
    assert _status(workspace) == "" and _status(worktree) == ""


@pytest.mark.parametrize("attached", [True, False], ids=["relink", "unattached"])
@pytest.mark.parametrize("fault", ["error-after-first-file", "interrupt-before-record"])
def test_link_failure_rolls_back_every_write(shared_project, tmp_path, monkeypatch, attached, fault):
    import specify_cli.private_checkout as private

    _, workspace, worktree, _ = shared_project
    monkeypatch.chdir(worktree)
    record = worktree / ".specify/checkout.json"
    if attached:
        assert _link(workspace).exit_code == 0
        shutil.rmtree(worktree / ".claude/skills/speckit-tasks")
        record.write_text(json.dumps({"schema_version": 1, "workspace": "elsewhere", "active_feature": None}))
    else:
        shutil.rmtree(worktree / ".specify")
    # Missing files in the checkout, the workspace, and a bundled package, a manifest hash to update, and no ignore line.
    (workspace / ".specify/scripts/bash/common.sh").unlink()
    (workspace / ".specify/workflows/speckit/workflow.yml").unlink()
    manifest = workspace / ".specify/integrations/claude.manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"][".claude/skills/speckit-tasks/SKILL.md"] = "0" * 64
    manifest.write_text(json.dumps(data), encoding="utf-8")
    _drop_ignore_line(worktree)
    if fault == "error-after-first-file":
        make_parents, calls = private._make_parents, []

        def fail_second_file(path, created):
            if path.name != "project.json":
                calls.append(path)
                if len(calls) == 2:
                    raise OSError("injected failure")
            make_parents(path, created)

        monkeypatch.setattr(private, "_make_parents", fail_second_file)
    else:
        write = private.atomic_json

        def interrupt_record(path, data, **options):
            if path.resolve() == record.resolve():
                raise KeyboardInterrupt
            write(path, data, **options)

        monkeypatch.setattr(private, "atomic_json", interrupt_record)
    before = tree(tmp_path)

    result = _link(workspace)

    assert result.exit_code == (1 if fault == "error-after-first-file" else 130), result.output
    assert tree(tmp_path) == before


def test_link_append_failure_keeps_the_new_record(shared_project, monkeypatch):
    import specify_cli.integrations.manifest as manifest

    _, workspace, worktree, _ = shared_project
    monkeypatch.chdir(worktree)
    _drop_ignore_line(worktree)
    append = manifest.append_gitignore

    def refuse(root, relative):
        if root == worktree.resolve():  # The staged run still writes its own ignore file.
            raise OSError("disk full")
        return append(root, relative)

    monkeypatch.setattr(manifest, "append_gitignore", refuse)
    result = _link(workspace)

    assert result.exit_code == 1
    assert result.stderr == "Error: disk full Add /.specify/checkout.json to .gitignore by hand.\n"
    assert json.loads((worktree / ".specify/checkout.json").read_text(encoding="utf-8"))["workspace"] == str(workspace)
    assert (worktree / ".claude/skills/speckit-plan/SKILL.md").is_file()


def test_link_checks_or_takes_the_workspace_project_id(shared_project, tmp_path, monkeypatch):
    _, workspace, worktree, clone = shared_project
    foreign = tmp_path / "foreign workspace"
    shutil.copytree(workspace / ".specify", foreign / ".specify")
    identity = json.loads((foreign / ".specify/workspace.json").read_text(encoding="utf-8"))
    (foreign / ".specify/workspace.json").write_text(json.dumps({**identity, "project_id": FOREIGN_ID}))
    monkeypatch.chdir(worktree)
    before = tree(tmp_path)
    refused = _link(foreign)
    assert refused.exit_code == 1
    assert "Workspace belongs to another project" in refused.stderr
    assert tree(tmp_path) == before

    shutil.rmtree(clone / ".specify")
    monkeypatch.chdir(clone)
    attached = _link(foreign)

    assert attached.exit_code == 0, attached.output
    assert json.loads((clone / ".specify/project.json").read_text(encoding="utf-8"))["project_id"] == FOREIGN_ID
    assert "Locator written: .specify/project.json. Commit it so teammates can link.\n" in attached.output
    assert (clone / ".claude/skills/speckit-plan/SKILL.md").is_file()


def test_link_never_changes_another_checkout(shared_project, tmp_path, monkeypatch):
    main, workspace, worktree, _ = shared_project
    second = tmp_path / "second"
    _git(main, "worktree", "add", "-q", str(second))
    for checkout in (second, worktree):
        monkeypatch.chdir(checkout)
        assert _link(workspace).exit_code == 0
    shutil.rmtree(worktree / ".claude/skills/speckit-plan")
    _drop_ignore_line(worktree)
    others = {root: tree(root) for root in (main, second, workspace)}

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert "Restored: 1 files" in result.output
    assert {root: tree(root) for root in others} == others


def test_concurrent_links_keep_the_ignore_line(shared_project, tmp_path, monkeypatch):
    _, workspace, worktree, _ = shared_project
    monkeypatch.chdir(worktree)
    assert "Updated" not in _link(workspace).output
    _drop_ignore_line(worktree)
    updated = _link(workspace)
    assert "Updated: .gitignore. Commit it so other checkouts get the ignore line.\n" in updated.output
    assert "Updated" not in _link(workspace).output
    _drop_ignore_line(worktree)
    gate = tmp_path / "gate"
    gate.mkdir()
    # Both runs read the ignore file before either appends.
    script = (
        "import sys, time\nfrom pathlib import Path\nimport specify_cli.integrations.manifest as manifest\n"
        "gate, name = Path(sys.argv[1]), sys.argv[2]\nsys.argv[1:] = sys.argv[3:]\nappend = manifest.append_gitignore\n"
        "def barrier(root, relative):\n    (gate / name).touch()\n    deadline = time.monotonic() + 10\n"
        "    while len(list(gate.iterdir())) < 2 and time.monotonic() < deadline:\n        time.sleep(0.01)\n"
        "    return append(root, relative)\n"
        "manifest.append_gitignore = barrier\nfrom specify_cli import main\nmain()\n"
    )
    runs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(gate), name, "project", "link", str(workspace)],
            cwd=worktree, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for name in ("first", "second")
    ]
    outputs = [run.communicate(timeout=60)[0] for run in runs]

    assert [run.returncode for run in runs] == [0, 0], outputs
    assert "/.specify/checkout.json" in (worktree / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert _git(worktree, "check-ignore", ".specify/checkout.json").strip() == ".specify/checkout.json"


def test_link_ignores_recovery_leftovers(shared_project, monkeypatch):
    _, workspace, worktree, _ = shared_project
    leftovers = {
        workspace / ".specify/naming-recovery-x/manifest.json": b"{not json",
        workspace / "specs/.merge-specs.lock": b"\x00lock",
        workspace / "specs/.merge-specs-recovery-x/journal.json": b"partial",
    }
    for path, data in leftovers.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.chdir(worktree)

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert {path: path.read_bytes() for path in leftovers} == leftovers
    assert "naming-recovery-x" not in result.output and ".merge-specs" not in result.output


def test_link_restores_only_verified_bundled_extension_files(shared_project, monkeypatch):
    main, workspace, worktree, _ = shared_project
    monkeypatch.chdir(main)
    assert CliRunner().invoke(app, ["extension", "add", "git"]).exit_code == 0
    registry = workspace / ".specify/extensions/.registry"
    restored = ".specify/extensions/git/scripts/bash/git-common.sh"
    unverified = ".specify/extensions/git/README.md"
    expected = (workspace / restored).read_bytes()
    data = json.loads(registry.read_text(encoding="utf-8"))
    data["extensions"]["git"]["generated_files"][unverified] = "0" * 64
    registry.write_text(json.dumps(data), encoding="utf-8")
    before = registry.read_bytes()
    for name in (restored, unverified):
        (workspace / name).unlink()
    monkeypatch.chdir(worktree)

    result = _link(workspace)

    assert result.exit_code == 0, result.output
    assert (workspace / restored).read_bytes() == expected
    assert not (workspace / unverified).exists()
    assert "Missing package files: git\nRepair: specify extension add git --force\n" in result.output
    assert registry.read_bytes() == before
    assert (worktree / ".claude/skills/speckit-git-commit/SKILL.md").is_file()


def test_link_suggests_select_only_in_automatic_mode_without_a_saved_feature(external_project):
    _, workspace, record = external_project
    hint = "Next: specify project select <feature>\n"
    first = _link(workspace)
    assert first.exit_code == 0, first.output
    assert first.output.endswith(hint)
    record.write_text(json.dumps({**json.loads(record.read_text()), "active_feature": "specs/001-existing"}))
    kept = _link(workspace)
    assert kept.exit_code == 0 and hint not in kept.output
    options = workspace / ".specify/init-options.json"
    options.write_text(json.dumps({**json.loads(options.read_text()), "feature_selection": "context"}))
    record.write_text(json.dumps({**json.loads(record.read_text()), "active_feature": None}))
    context = _link(workspace)
    assert context.exit_code == 0 and hint not in context.output
