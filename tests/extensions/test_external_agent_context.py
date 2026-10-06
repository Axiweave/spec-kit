"""CLI-free native context updates use the selected external workspace."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.extensions.test_extension_agent_context import (
    BASH,
    EXT_DIR,
    POWERSHELL,
    PROJECT_ROOT,
    _bundled_script_env,
)

SCRIPTS = {
    "python": ("update_agent_context.py", "common.py"),
    "bash": ("update-agent-context.sh", "common.sh"),
    "powershell": ("update-agent-context.ps1", "common.ps1"),
}
PREFIX = "# User instructions\n\nKeep this exact spacing.  \n\n"
SUFFIX = "\n## User notes\nDo not replace these notes.\n"
EXISTING = PREFIX + "<!-- SPECKIT START -->\nOld pointer\n<!-- SPECKIT END -->\n" + SUFFIX


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }


@pytest.fixture(params=[
    "python",
    pytest.param("bash", marks=pytest.mark.skipif(
        not BASH or os.name == "nt", reason="POSIX bash required",
    )),
    pytest.param("powershell", marks=pytest.mark.skipif(
        not POWERSHELL, reason="no PowerShell available",
    )),
])
def variant(request):
    return request.param


@pytest.fixture(params=["source", "installed"])
def project(tmp_path, variant, request):
    repo = tmp_path / "code project"
    workspace = tmp_path / "external workspace"
    repo.mkdir()
    project_id = str(uuid4())
    write_json(repo / ".specify/project.json", {
        "schema_version": 1, "project_id": project_id, "storage": "external",
    })
    write_json(workspace / ".specify/workspace.json", {
        "schema_version": 1, "project_id": project_id,
    })
    record = repo / ".specify/checkout.json"
    write_json(record, {
        "schema_version": 1, "workspace": str(workspace.resolve()),
        "active_feature": "specs/007-selected",
    })
    config = workspace / ".specify/extensions/agent-context/agent-context-config.yml"
    write_json(config, {"context_file": "AGENTS.md"})
    write_json(workspace / ".specify/init-options.json", {"feature_selection": "automatic"})
    for name in ("007-selected", "099-unselected"):
        feature = workspace / "specs" / name
        feature.mkdir(parents=True)
        (feature / "plan.md").write_text(f"# Private plan {name}\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(EXISTING, encoding="utf-8")
    filename, common = SCRIPTS[variant]
    script = EXT_DIR / "scripts" / variant / filename
    if request.param == "installed":
        extension = workspace / ".specify/extensions/agent-context"
        shutil.copytree(EXT_DIR / "scripts", extension / "scripts")
        core = workspace / ".specify/scripts" / variant
        core.mkdir(parents=True)
        shutil.copy2(PROJECT_ROOT / "scripts" / variant / common, core / common)
        script = extension / "scripts" / variant / filename
    env = _bundled_script_env(repo, for_bash=variant == "bash")
    for key in list(env):
        if key.startswith("SPECIFY_"):
            env.pop(key)
    env.update({
        "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LOCALAPPDATA": str(tmp_path / "data"),
        "PYTHONPATH": "",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    command = {
        "python": [sys.executable],
        "bash": [BASH],
        "powershell": [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    }[variant] + [str(script)]
    return repo, workspace, record, config, env, command


def invoke(project, *args, cwd=None):
    repo, _, _, _, env, command = project
    return subprocess.run(
        [*command, *args], cwd=cwd or repo, env=env,
        capture_output=True, text=True, timeout=30,
    )


def test_external_context_points_to_saved_plan_without_copying_artifacts(project):
    repo, workspace, record, _, _, _ = project
    nested = repo / "src/nested"
    nested.mkdir(parents=True)
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    before_record = record.read_bytes()
    result = invoke(project, cwd=nested)
    assert result.returncode == 0, result.stderr
    content = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert content.startswith(PREFIX)
    assert content.endswith(SUFFIX)
    assert f"at {(workspace / 'specs/007-selected/plan.md').resolve().as_posix()}" in content
    assert "Private plan" not in content
    assert "Old pointer" not in content
    assert snapshot(repo) == {**before_repo, "AGENTS.md": content.encode("utf-8")}
    assert snapshot(workspace) == before_workspace
    assert record.read_bytes() == before_record


@pytest.mark.parametrize("override", ["feature", "plan"])
def test_external_explicit_selection_overrides_saved_feature(project, override):
    repo, workspace, record, _, env, _ = project
    before_record = record.read_bytes()
    args = ()
    if override == "feature":
        env["SPECIFY_FEATURE_DIRECTORY"] = "specs/099-unselected"
    else:
        args = ("specs/099-unselected/plan.md",)
    result = invoke(project, *args)
    assert result.returncode == 0, result.stderr
    content = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert f"at {(workspace / 'specs/099-unselected/plan.md').resolve().as_posix()}" in content
    assert "007-selected" not in content
    assert record.read_bytes() == before_record


def test_external_context_ignores_stale_repository_config_and_selection(project):
    repo, workspace, _, _, _, _ = project
    write_json(repo / ".specify/extensions/agent-context/agent-context-config.yml", {
        "context_file": "STALE.md",
    })
    write_json(repo / ".specify/feature.json", {"feature_directory": "specs/001-stale"})
    stale = repo / "specs/001-stale/plan.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("# Stale local plan\n", encoding="utf-8")
    result = invoke(project)
    assert result.returncode == 0, result.stderr
    assert not (repo / "STALE.md").exists()
    assert str((workspace / "specs/007-selected/plan.md").resolve().as_posix()) in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_external_context_self_seeds_from_workspace_configuration(project):
    repo, workspace, _, config, _, _ = project
    write_json(config, {})
    write_json(workspace / ".specify/init-options.json", {"integration": "claude", "feature_selection": "automatic"})
    write_json(config.parent / "agent-context-defaults.json", {"agents": {"claude": "CLAUDE.md"}})
    result = invoke(project)
    assert result.returncode == 0, result.stderr
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == EXISTING
    assert (workspace / "specs/007-selected/plan.md").resolve().as_posix() in (
        repo / "CLAUDE.md"
    ).read_text(encoding="utf-8")
    assert not (workspace / "CLAUDE.md").exists()


@pytest.mark.parametrize("failure", ["missing-mapping", "foreign-workspace", "no-selection", "missing-workspace"])
def test_external_context_errors_without_fallback_or_writes(project, failure):
    repo, workspace, record, _, _, _ = project
    if failure == "missing-mapping":
        record.unlink()
    elif failure == "foreign-workspace":
        write_json(workspace / ".specify/workspace.json", {
            "schema_version": 1, "project_id": str(uuid4()),
        })
    else:
        data = json.loads(record.read_text(encoding="utf-8"))
        if failure == "no-selection":
            data["active_feature"] = None
        else:
            data["workspace"] = str(workspace / "missing")
        write_json(record, data)
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    result = invoke(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stderr.strip()
    assert snapshot(repo) == before_repo
    assert snapshot(workspace) == before_workspace


def test_external_context_rejects_output_outside_repository(project):
    repo, workspace, _, config, _, _ = project
    write_json(config, {"context_file": "../external workspace/UNRELATED.md"})
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    result = invoke(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert snapshot(repo) == before_repo
    assert snapshot(workspace) == before_workspace


def test_standalone_python_context_follows_the_default_policy(tmp_path):
    """Without the core helper the policy file is unreadable, so context applies."""
    repo = tmp_path / "local project"
    config = repo / ".specify/extensions/agent-context/agent-context-config.yml"
    write_json(config, {"context_file": "AGENTS.md"})
    plan = repo / "specs/001-local/plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Local plan\n", encoding="utf-8")
    script = tmp_path / "standalone/update_agent_context.py"
    script.parent.mkdir()
    shutil.copy2(EXT_DIR / "scripts/python/update_agent_context.py", script)
    env = _bundled_script_env(repo)
    refused = subprocess.run(
        [sys.executable, str(script)], cwd=repo,
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert refused.returncode == 1
    assert "SPECIFY_FEATURE_DIRECTORY" in refused.stderr
    assert not (repo / "AGENTS.md").exists()
    named = subprocess.run(
        [sys.executable, str(script)], cwd=repo,
        env={**env, "SPECIFY_FEATURE_DIRECTORY": "specs/001-local"},
        capture_output=True, text=True, timeout=30,
    )
    assert named.returncode == 0, named.stderr
    assert "at specs/001-local/plan.md" in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_external_context_honors_explicit_repository_root(project, tmp_path):
    repo, workspace, _, _, env, _ = project
    elsewhere = tmp_path / "unrelated working directory"
    elsewhere.mkdir()
    env["SPECIFY_INIT_DIR"] = str(repo)
    result = invoke(project, cwd=elsewhere)
    assert result.returncode == 0, result.stderr
    assert (workspace / "specs/007-selected/plan.md").resolve().as_posix() in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert snapshot(elsewhere) == {}


@pytest.mark.parametrize("override", ["feature", "plan"])
def test_external_context_rejects_selection_outside_workspace(project, override):
    repo, workspace, _, _, env, _ = project
    args = ()
    if override == "feature":
        env["SPECIFY_FEATURE_DIRECTORY"] = "../code project"
    else:
        args = ("../code project/plan.md",)
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    result = invoke(project, *args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert snapshot(repo) == before_repo
    assert snapshot(workspace) == before_workspace


def choose_policy(workspace: Path, mode: str | None) -> None:
    path = workspace / ".specify/init-options.json"
    if mode is None:
        path.unlink()
    else:
        write_json(path, {"feature_selection": mode})


@pytest.mark.parametrize("mode", [None, "context"])
def test_external_context_policy_ignores_the_saved_feature(project, mode):
    repo, workspace, record, _, _, _ = project
    choose_policy(workspace, mode)
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    before_record = record.read_bytes()
    result = invoke(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "SPECIFY_FEATURE_DIRECTORY" in (result.stderr + result.stdout)
    assert snapshot(repo) == before_repo
    assert snapshot(workspace) == before_workspace
    assert record.read_bytes() == before_record


@pytest.mark.parametrize("override", ["feature", "plan"])
@pytest.mark.parametrize("mode", [None, "context"])
def test_external_context_policy_uses_explicit_selection_and_keeps_the_record(
    project, mode, override
):
    repo, workspace, record, _, env, _ = project
    choose_policy(workspace, mode)
    before_record = record.read_bytes()
    args = ()
    if override == "feature":
        env["SPECIFY_FEATURE_DIRECTORY"] = "specs/099-unselected"
    else:
        args = ("specs/099-unselected/plan.md",)
    result = invoke(project, *args)
    assert result.returncode == 0, result.stderr
    content = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert f"at {(workspace / 'specs/099-unselected/plan.md').resolve().as_posix()}" in content
    assert "007-selected" not in content
    assert record.read_bytes() == before_record


def test_external_invalid_policy_stops_before_any_write(project):
    repo, workspace, record, _, env, _ = project
    choose_policy(workspace, "Context")
    env["SPECIFY_FEATURE_DIRECTORY"] = "specs/099-unselected"
    before_repo = snapshot(repo)
    before_workspace = snapshot(workspace)
    before_record = record.read_bytes()
    result = invoke(project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "feature_selection" in (result.stderr + result.stdout)
    assert snapshot(repo) == before_repo
    assert snapshot(workspace) == before_workspace
    assert record.read_bytes() == before_record
