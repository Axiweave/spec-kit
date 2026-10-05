"""``specify artifact`` parity between local and external storage modes."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.presets import PresetRegistry
from tests.conftest import install_preset
from tests.specify_cli.artifacts.helpers import install_extension_with_hooks


def _project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str) -> tuple[Path, Path]:
    """Return ``(repo, workspace)``; they are the same directory in local mode."""
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repo = tmp_path / "repo"
    (repo / ".specify").mkdir(parents=True)
    if storage == "local":
        return repo, repo
    workspace = tmp_path / "workspace"
    (workspace / ".specify").mkdir(parents=True)
    identity = str(uuid4())
    (repo / ".specify/project.json").write_text(
        json.dumps({"schema_version": 1, "project_id": identity, "storage": "external"})
    )
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": identity})
    )
    record = tmp_path / "data/specify/projects" / f"{identity}.json"
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None})
    )
    return repo, workspace


def _install(repo: Path, workspace: Path) -> None:
    """Install a preset with a materialized skill and an extension declaring a hook."""
    pack = install_preset(
        workspace,
        "lean",
        {
            "commands": [{"name": "speckit.lean.plan", "description": "Lean plan"}],
            "templates": [{"name": "lean-template", "description": "Lean template"}],
        },
    )
    (pack / "commands").mkdir()
    (pack / "commands" / "speckit.lean.plan.md").write_text(
        "---\ndescription: Lean plan\n---\nbody\n", encoding="utf-8"
    )
    (pack / "templates").mkdir()
    (pack / "templates" / "lean-template.md").write_text(
        "---\ndescription: Lean template\n---\n", encoding="utf-8"
    )
    PresetRegistry(workspace / ".specify" / "presets").update(
        "lean", {"registered_skills": {"copilot": ["speckit-lean-plan"]}}
    )
    skill = repo / ".github" / "skills" / "speckit-lean-plan" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: speckit-lean-plan\n---\n", encoding="utf-8")
    install_extension_with_hooks(
        workspace,
        "git",
        {"after_specify": [{"command": "speckit.git.commit", "description": "Commit"}]},
    )


def _run(*args: str) -> dict | list:
    result = CliRunner().invoke(app, ["artifact", *args, "--json"])
    assert result.exit_code == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("storage", ["local", "external"])
def test_artifact_commands_report_workspace_paths_like_local_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: str
):
    repo, workspace = _project(tmp_path, monkeypatch, storage)
    _install(repo, workspace)
    monkeypatch.chdir(repo)

    rows = {row["id"]: row for row in _run("list")}
    command = _run("info", "command:speckit.lean.plan")
    template = _run("info", "template:lean-template")
    hook = _run("info", "hook:after_specify:speckit.git.commit")
    contribution = _run("lookup", "preset:lean:command:speckit.lean.plan")
    hook_contribution = _run("lookup", hook["stack"][0]["lookupId"])

    assert rows["command:speckit.lean.plan"]["stack"] == command["stack"]
    preset_layer = command["stack"][0]
    assert preset_layer["manifestPath"] == ".specify/presets/lean/preset.yml"
    # Agent output stays repository-relative in both modes.
    assert preset_layer["sourcePath"] == ".github/skills/speckit-lean-plan/SKILL.md"
    assert template["stack"][0]["sourcePath"] == (
        ".specify/presets/lean/templates/lean-template.md"
    )
    assert hook["stack"][0]["manifestPath"] == ".specify/extensions/git/extension.yml"
    assert contribution["manifestPath"] == ".specify/presets/lean/preset.yml"
    assert contribution["sourcePath"] == ".specify/presets/lean/commands/speckit.lean.plan.md"
    assert hook_contribution["manifestPath"] == ".specify/extensions/git/extension.yml"
