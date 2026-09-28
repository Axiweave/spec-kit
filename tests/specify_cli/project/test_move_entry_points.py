"""Migration keeps edited command content and redirects native asset reads."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.integrations import get_integration


@pytest.fixture(autouse=True)
def isolated_user(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("SPECIFY_", "SPECKIT_", "OMP_", "PI_")):
            monkeypatch.delenv(key)
    for key, path in {
        "HOME": tmp_path / "home", "USERPROFILE": tmp_path / "home",
        "XDG_CONFIG_HOME": tmp_path / "config", "APPDATA": tmp_path / "config",
        "XDG_DATA_HOME": tmp_path / "data", "LOCALAPPDATA": tmp_path / "data",
        "PI_CODING_AGENT_DIR": tmp_path / "pi",
    }.items():
        monkeypatch.setenv(key, str(path))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("agent", ["omp", "gemini", "goose", "hermes", "codex"])
@pytest.mark.parametrize("edited", [False, True])
def test_native_command_keeps_content_and_finds_external_workspace(tmp_path, monkeypatch, agent, edited):
    repository = tmp_path / "code"
    result = CliRunner().invoke(app, [
        "init", str(repository), "--storage", "local", "--integration", agent,
        "--script", "py", "--feature-numbering", "sequential",
        "--non-interactive", "--ignore-agent-tools",
    ])
    assert result.exit_code == 0, result.output
    integration = get_integration(agent)
    assert integration is not None
    command = integration.commands_dest(repository) / integration.command_filename("plan")
    if agent == "hermes":
        command = tmp_path / "home/.hermes/skills/speckit-plan/SKILL.md"
        # An older skill has no workspace instructions.
        command.write_text(
            "---\nname: speckit-plan\ndescription: Plan the feature.\n---\n\n"
            "Read `.specify/memory/constitution.md` before planning.\n",
            encoding="utf-8",
        )
    elif agent == "codex":
        command = repository / ".agents/skills/speckit-plan/SKILL.md"
    marker = "Keep my edited planning instructions."
    if edited:
        if agent == "gemini":
            command.write_text(f'description = "Mine"\nprompt = "{marker}"\nuser_option = "Keep me"\n# User comment\n')
        elif agent == "goose":
            command.write_text(yaml.safe_dump({
                "version": "1.0.0", "title": "Mine", "description": "Mine",
                "prompt": marker, "user_option": "Keep me",
            }) + "# User comment\n")
        else:
            command.write_text(command.read_text() + "\n" + marker + "\n")
    before = command.read_text()
    constitution = repository / ".specify/memory/constitution.md"
    constitution.write_text("# Edited project constitution\nPreserve this context.\n")
    saved_context = constitution.read_bytes()
    choices = (repository / ".specify/init-options.json").read_bytes()
    feature = "specs/003-current"
    (repository / feature).mkdir(parents=True)
    artifacts = {"spec.md": b"# Current specification\n", "tasks.md": b"- [x] Preserve completed work\n"}
    for name, content in artifacts.items():
        (repository / feature / name).write_bytes(content)
    (repository / ".specify/feature.json").write_text(
        json.dumps({"feature_directory": feature}), encoding="utf-8",
    )
    from specify_cli.project.move import commit_move, prepare_move

    workspace = tmp_path / "external workspace"
    commit_move(prepare_move(repository, workspace))
    after = command.read_text()
    if agent == "gemini":
        old, new = tomllib.loads(before), tomllib.loads(after)
        assert old["prompt"] in new["prompt"]
        assert {key: value for key, value in old.items() if key != "prompt"} == {
            key: value for key, value in new.items() if key != "prompt"
        }
        body = new["prompt"]
    elif agent == "goose":
        old, new = yaml.safe_load(before), yaml.safe_load(after)
        assert old["prompt"] in new["prompt"]
        assert {key: value for key, value in old.items() if key != "prompt"} == {
            key: value for key, value in new.items() if key != "prompt"
        }
        body = new["prompt"]
    else:
        body = after
        assert before.split("---", 2)[-1] in after
    assert "specify project info --json" in body
    assert "workspace_root" in body
    if edited:
        assert marker in body
        if agent in {"gemini", "goose"}:
            assert "# User comment" in after
    assert (workspace / ".specify/memory/constitution.md").read_bytes() == saved_context
    assert (workspace / ".specify/init-options.json").read_bytes() == choices
    for name, content in artifacts.items():
        assert (workspace / feature / name).read_bytes() == content
    assert set(path.name for path in (repository / ".specify").iterdir()) == {"project.json"}
    monkeypatch.chdir(repository)
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert info.exit_code == 0, info.output
    assert json.loads(info.stdout)["workspace_root"] == str(workspace)
    assert json.loads(info.stdout)["active_feature"] == feature
    continued = subprocess.run(
        [sys.executable, str(workspace / ".specify/scripts/python/check_prerequisites.py"),
         "--json", "--paths-only"],
        cwd=repository, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert continued.returncode == 0, continued.stderr
    paths = json.loads(continued.stdout)
    assert paths["REPO_ROOT"] == str(repository)
    assert paths["FEATURE_DIR"] == str(workspace / feature)


@pytest.mark.parametrize("failure", ["refresh", "linked_skills"])
def test_hermes_failed_move_preserves_shared_skill_bytes(tmp_path, monkeypatch, failure):
    """Invariant: a refused or interrupted move preserves shared skill and project bytes."""
    from specify_cli.project import _move_commands
    from specify_cli.project.move import commit_move, prepare_move

    repository = tmp_path / "code"
    result = CliRunner().invoke(app, [
        "init", str(repository), "--storage", "local", "--integration", "hermes",
        "--script", "py", "--offline", "--non-interactive", "--ignore-agent-tools",
    ])
    assert result.exit_code == 0, result.output
    skills = tmp_path / "home/.hermes/skills"
    skill = skills / "speckit-plan/SKILL.md"
    skill.write_text("---\nname: speckit-plan\n---\nKeep my instructions.\n", encoding="utf-8")
    before = {p.relative_to(skills): p.read_bytes() for p in skills.rglob("*") if p.is_file()}
    metadata = {p.relative_to(repository): p.read_bytes() for p in (repository / ".specify").rglob("*") if p.is_file()}
    prepared = prepare_move(repository, tmp_path / "workspace")
    if failure == "linked_skills":
        target = tmp_path / "shared-skills"
        skills.rename(target)
        skills.symlink_to(target, target_is_directory=True)
    else:
        refresh = _move_commands.refresh_commands

        def fail_after_refresh(root):
            refresh(root)
            raise OSError("Interrupted after native refresh")

        monkeypatch.setattr(_move_commands, "refresh_commands", fail_after_refresh)

    with pytest.raises(ValueError, match="symlink|Interrupted after native refresh"):
        commit_move(prepared)

    assert {p.relative_to(skills): p.read_bytes() for p in skills.rglob("*") if p.is_file()} == before
    assert {p.relative_to(repository): p.read_bytes() for p in (repository / ".specify").rglob("*") if p.is_file()} == metadata
