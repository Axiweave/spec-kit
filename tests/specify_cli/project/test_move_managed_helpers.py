"""Migration upgrades owned helper generations without claiming user changes."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.integrations.manifest import IntegrationManifest
from specify_cli.workspace import resolve_project
from tests.conftest import requires_bash
from tests.parity_helpers import install_scripts
from tests.specify_cli.project.test_command_move import (
    ACTIVE_FEATURE,
    assert_local,
    assert_recovery,
    git_state,
    local_project as _local_project,
    snapshot,
)
from tests.specify_cli.test_external_feature_scripts import (
    _invoke,
    _success,
    variant as _variant,
)

local_project = _local_project
variant = _variant


def own_helpers(repository: Path, *, obsolete: bool = False) -> IntegrationManifest:
    manifest = IntegrationManifest("speckit", repository, version="obsolete-test-generation")
    for path in (repository / ".specify/scripts").rglob("*"):
        if not path.is_file():
            continue
        if obsolete and path.stem not in {"common", "check-prerequisites", "check_prerequisites"}:
            # Distinct owned bytes model an obsolete generation, not a historical release.
            path.write_text({
                ".py": 'raise SystemExit("Obsolete managed helper generation")\n',
                ".sh": 'echo "Obsolete managed helper generation" >&2\nexit 19\n',
                ".ps1": 'throw "Obsolete managed helper generation"\n',
            }[path.suffix], encoding="utf-8")
        manifest.record_existing(path.relative_to(repository).as_posix())
    manifest.save()
    return manifest


def test_move_upgrades_installed_helpers_and_preserves_user_work(local_project, variant):
    repository, workspace, _ = local_project
    for script in ("create-new-feature", "setup-tasks", "resolve-template"):
        install_scripts(repository, script)
    manifest = own_helpers(repository, obsolete=True)
    for relative, edited in {
        ".specify/templates/spec-template.md": b"# Edited specification template\n",
        ".specify/templates/tasks-template.md": b"# Edited tasks template\n",
        ".specify/templates/plan-template.md": b"# Edited plan template\n",
        ".specify/memory/constitution.md": b"# Edited constitution\nKeep local decisions.\n",
    }.items():
        path = repository / relative
        path.write_bytes(b"Original managed content\n")
        manifest.record_existing(relative)
        path.write_bytes(edited)
    manifest.save()
    (repository / ACTIVE_FEATURE / "plan.md").write_bytes(b"# Active user plan\n")
    (repository / ACTIVE_FEATURE / "tasks.md").write_bytes(b"- [ ] Active user task\n")
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code == 0, result.output
    for relative, content in before.items():
        if relative.startswith(("specs/", ".specify/templates/", ".specify/memory/")):
            assert (workspace / relative).read_bytes() == content
    repository_after_move = snapshot(repository)
    plan = _success(_invoke(variant, workspace, repository, None, "setup-plan", "--json"))
    assert Path(plan["IMPL_PLAN"]) == workspace / ACTIVE_FEATURE / "plan.md"
    assert (workspace / ACTIVE_FEATURE / "plan.md").read_bytes() == b"# Active user plan\n"
    tasks = _success(_invoke(variant, workspace, repository, None, "setup-tasks", "--json"))
    assert Path(tasks["FEATURE_DIR"]) == workspace / ACTIVE_FEATURE
    assert tasks["TASKS_TEMPLATE_CONTENT"] == "# Edited tasks template\n"
    assert (workspace / ACTIVE_FEATURE / "tasks.md").read_bytes() == b"- [ ] Active user task\n"

    created = _success(_invoke(
        variant, workspace, repository, None, "create-new-feature",
        "--json", "--number", "8", "--short-name", "after-move", "Continue migrated work",
    ))
    feature = workspace / "specs/008-after-move"
    assert Path(created["SPEC_FILE"]) == feature / "spec.md"
    assert (feature / "spec.md").read_bytes() == b"# Edited specification template\n"
    assert resolve_project(repository).feature_dir == feature
    plan = _success(_invoke(variant, workspace, repository, None, "setup-plan", "--json"))
    assert Path(plan["IMPL_PLAN"]) == feature / "plan.md"
    assert (feature / "plan.md").read_bytes() == b"# Edited plan template\n"
    tasks = _success(_invoke(variant, workspace, repository, None, "setup-tasks", "--json"))
    assert Path(tasks["FEATURE_DIR"]) == feature
    paths = _success(_invoke(
        variant, workspace, repository, None, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(paths["REPO_ROOT"]) == repository
    assert Path(paths["TASKS"]) == feature / "tasks.md"
    Path(paths["TASKS"]).write_text(tasks["TASKS_TEMPLATE_CONTENT"], encoding="utf-8")
    checked = _success(_invoke(
        variant, workspace, repository, None, "check-prerequisites",
        "--json", "--require-spec", "--require-tasks", "--include-tasks",
    ))
    assert Path(checked["FEATURE_DIR"]) == feature
    assert "tasks.md" in checked["AVAILABLE_DOCS"]
    assert snapshot(repository) == repository_after_move
    assert git_state(repository) == history
    assert not (repository / "specs").exists()
    assert set(snapshot(repository / ".specify")) == {"project.json"}
    for relative, content in before.items():
        if relative.startswith(("specs/", ".specify/templates/", ".specify/memory/")):
            assert (workspace / relative).read_bytes() == content


@pytest.mark.parametrize("relative", [
    ".specify/scripts/bash/setup-plan.sh",
    ".specify/scripts/python/setup_plan.py",
    ".specify/scripts/powershell/setup-plan.ps1",
])
def test_move_refuses_edited_managed_helpers_without_overwriting_either_copy(local_project, relative):
    repository, workspace, home = local_project
    own_helpers(repository, obsolete=True)
    helper = repository / relative
    helper.write_bytes(helper.read_bytes() + b"# User helper customization\n")
    before, history = snapshot(repository), git_state(repository)

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code != 0
    assert relative in result.output
    assert_local(repository, home, before, history)
    assert snapshot(workspace) == {
        name: content for name, content in before.items() if name.startswith((".specify/", "specs/"))
    }
    assert_recovery(result.output, repository, workspace)
    for root in (repository, workspace):
        paths = _success(_invoke(
            "python", root, root, None, "check-prerequisites", "--json", "--paths-only",
        ))
        assert Path(paths["FEATURE_DIR"]) == root / ACTIVE_FEATURE


def test_commit_failure_after_helper_upgrade_restores_staged_originals(local_project, monkeypatch):
    repository, workspace, home = local_project
    own_helpers(repository, obsolete=True)
    before, history = snapshot(repository), git_state(repository)
    introduced = workspace / ".specify/scripts/python/create_new_feature.py"
    injected = False

    def fail_commit(path):
        nonlocal injected
        assert Path(path) == workspace
        assert introduced.is_file()
        injected = True
        raise OSError("Injected failure after helper upgrade")

    monkeypatch.setattr("specify_cli.project.move.initialize_workspace_git", fail_commit)
    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert injected, result.output
    assert result.exit_code != 0
    assert "Injected failure after helper upgrade" in result.output
    assert_local(repository, home, before, history)
    assert snapshot(workspace) == {
        name: content for name, content in before.items() if name.startswith((".specify/", "specs/"))
    }
    assert not introduced.exists()
    assert_recovery(result.output, repository, workspace)
    for root in (repository, workspace):
        paths = _success(_invoke(
            "python", root, root, None, "check-prerequisites", "--json", "--paths-only",
        ))
        assert Path(paths["FEATURE_DIR"]) == root / ACTIVE_FEATURE


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
@requires_bash
def test_new_bash_helper_runs_directly_without_changing_user_script_mode(local_project):
    repository, workspace, _ = local_project
    own_helpers(repository, obsolete=True)
    user_script = repository / ".specify/scripts/bash/user-tool.sh"
    user_bytes = b"#!/bin/sh\nprintf 'User helper\\n'\n"
    user_script.write_bytes(user_bytes)
    user_script.chmod(0o640)
    (repository / ACTIVE_FEATURE / "plan.md").write_bytes(b"# Existing user plan\n")
    introduced = ".specify/scripts/bash/setup-tasks.sh"
    assert not (repository / introduced).exists()

    result = CliRunner().invoke(app, ["project", "move", str(workspace), "--confirm-remove-local"])

    assert result.exit_code == 0, result.output
    before = snapshot(repository)
    tasks = _success(subprocess.run(
        [str(workspace / introduced), "--json"], cwd=repository,
        capture_output=True, text=True, timeout=30,
    ))
    assert Path(tasks["FEATURE_DIR"]) == workspace / ACTIVE_FEATURE
    assert Path(tasks["TASKS_TEMPLATE"]) == workspace / ".specify/templates/tasks-template.md"
    assert snapshot(repository) == before
    migrated_user_script = workspace / user_script.relative_to(repository)
    assert migrated_user_script.read_bytes() == user_bytes
    assert stat.S_IMODE(migrated_user_script.stat().st_mode) == 0o640
