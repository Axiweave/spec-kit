"""Private mode: the workspace flag, the managed exclude block, and the tracked-file preflight."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from specify_cli.private_checkout import (
    BEGIN, END, _isolated_user_dirs, attach_integrations, exclude_path, install_integration, plan_checkout_files,
    plan_reconcile, refuse_tracked, regenerate_exclude_block, restore_checkout, restore_workspace_files,
)
from specify_cli.integration_runtime import parse_integration_options
from specify_cli.integrations import INTEGRATION_REGISTRY
from specify_cli.workspace import is_private, verify_workspace
from tests.conftest import install_preset


def _identity(workspace: Path, **extra) -> str:
    project_id = str(uuid4())
    (workspace / ".specify").mkdir(parents=True, exist_ok=True)
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": project_id, **extra}), encoding="utf-8"
    )
    return project_id


@pytest.mark.parametrize(("extra", "expected"), [({}, False), ({"private": False}, False), ({"private": True}, True)])
def test_private_flag_values(tmp_path: Path, extra: dict, expected: bool):
    project_id = _identity(tmp_path, **extra)
    assert is_private(tmp_path) is expected
    assert verify_workspace(tmp_path, project_id) == tmp_path.resolve()


def test_missing_identity_is_not_private(tmp_path: Path):
    assert is_private(tmp_path) is False


@pytest.mark.parametrize("value", ["true", 1, 0, None, [], {}])
def test_malformed_private_flag_never_falls_back_to_default(tmp_path: Path, value):
    project_id = _identity(tmp_path, private=value)
    message = "Invalid private flag in .*: use true or false."
    with pytest.raises(ValueError, match=message):
        is_private(tmp_path)
    with pytest.raises(ValueError, match=message):
        verify_workspace(tmp_path, project_id)


def _git(directory: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=directory, env=env, check=True, capture_output=True, text=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def _attach(checkout: Path, key: str = "claude", files=(), excluded=()) -> None:
    """Give *checkout* a private locator and a checkout manifest with *files*."""
    integrations = checkout / ".specify/integrations"
    integrations.mkdir(parents=True, exist_ok=True)
    (checkout / ".specify/project.json").write_text("{}", encoding="utf-8")
    data = {"integration": key, "version": "1", "files": {f: "0" * 64 for f in files}}
    if excluded:
        data["excluded"] = list(excluded)
    (integrations / f"{key}.manifest.json").write_text(json.dumps(data), encoding="utf-8")


def _block(checkout: Path) -> list[str]:
    lines = exclude_path(checkout).read_text(encoding="utf-8").splitlines()
    if BEGIN not in lines:
        return []
    return lines[lines.index(BEGIN) + 1 : lines.index(END)]


def test_block_is_union_of_checkout_manifests_across_worktrees(tmp_path: Path):
    main = _repo(tmp_path / "main")
    _git(main, "worktree", "add", "-q", str(tmp_path / "wt"))
    wt = tmp_path / "wt"
    _attach(main, files=[".claude/skills/a/SKILL.md", ".specify/events.py"])
    _attach(wt, files=[".claude/skills/b/SKILL.md"], excluded=[".claude/skills/ext/SKILL.md"])
    _attach(wt, key="copilot", files=[".github/agents/x.agent.md"])
    regenerate_exclude_block(wt)
    assert exclude_path(wt) == exclude_path(main)
    assert _block(main) == [
        "/.claude/skills/a/SKILL.md",
        "/.claude/skills/b/SKILL.md",
        "/.claude/skills/ext/SKILL.md",
        "/.github/agents/x.agent.md",
        "/.specify/",
    ]
    assert _git(main, "status", "--porcelain", "--untracked-files=all") == ""


def test_block_drops_rules_whose_manifest_is_gone_even_if_path_exists(tmp_path: Path):
    main = _repo(tmp_path / "main")
    _attach(main, files=["x.md"])
    regenerate_exclude_block(main)
    (main / ".specify/integrations/claude.manifest.json").unlink()
    (main / "x.md").write_text("user file", encoding="utf-8")
    regenerate_exclude_block(main)
    assert _block(main) == ["/.specify/"]


def test_regeneration_is_idempotent_and_keeps_user_lines_and_crlf(tmp_path: Path):
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    path.write_bytes(b"# user\r\n/x.md\r\n")
    _attach(main, files=["x.md", "dir with space/a\\b.md"])
    regenerate_exclude_block(main)
    first = path.read_bytes()
    regenerate_exclude_block(main)
    assert path.read_bytes() == first
    assert first.startswith(b"# user\r\n/x.md\r\n")
    assert b"\n" not in first.replace(b"\r\n", b"")
    assert "/dir\\ with\\ space/a\\\\b.md" in _block(main)
    (main / ".specify/integrations/claude.manifest.json").unlink()
    (main / ".specify/project.json").unlink()
    regenerate_exclude_block(main)
    assert path.read_bytes() == b"# user\r\n/x.md\r\n"


@pytest.mark.parametrize("user", [b"", b"\n\n", b"# a\n\n", b"# no newline", b"# a\n\n\n# b\n"])
def test_outside_block_bytes_survive_add_rebuild_and_remove(tmp_path: Path, user: bytes):
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    path.write_bytes(user)
    _attach(main, files=["x.md"])
    regenerate_exclude_block(main)
    text = path.read_bytes()
    start = text.index(BEGIN.encode())
    path.write_bytes(text[:start] + b"# mid\n\n" + text[start:] + b"\n# after\n")
    regenerate_exclude_block(main)
    rebuilt = path.read_bytes()
    assert rebuilt.startswith(text[:start] + b"# mid\n\n" + BEGIN.encode())
    assert rebuilt.endswith(END.encode() + b"\n\n# after\n")
    (main / ".specify/project.json").unlink()
    regenerate_exclude_block(main)
    expected = (user + b"\n" if user and not user.endswith(b"\n") else user) + b"# mid\n\n\n# after\n"
    assert path.read_bytes() == expected

def test_missing_info_directory_is_created(tmp_path: Path):
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    path.unlink()
    path.parent.rmdir()
    _attach(main)
    regenerate_exclude_block(main)
    assert _block(main) == ["/.specify/"]


def test_symlinked_exclude_file_gets_the_block_in_its_target(tmp_path: Path):
    """Git follows a symlinked exclude file, so the block goes into the target and the link stays."""
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    target = main / ".gitignore.local"
    target.write_text(".gitignore.local\nnotes/\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(Path("../..") / ".gitignore.local")
    _attach(main)
    regenerate_exclude_block(main)
    assert path.is_symlink()
    assert target.read_text(encoding="utf-8").startswith(".gitignore.local\nnotes/\n")
    assert _block(main) == ["/.specify/"]
    assert _git(main, "status", "--porcelain", "--untracked-files=all") == ""


def test_not_a_git_repository_is_a_no_op(tmp_path: Path):
    _attach(tmp_path, files=["x.md"])
    regenerate_exclude_block(tmp_path)
    assert not (tmp_path / ".gitignore").exists()
    assert not (tmp_path / ".git").exists()


def test_refuse_tracked_names_a_tracked_path(tmp_path: Path):
    main = _repo(tmp_path / "main")
    (main / ".claude/skills/a").mkdir(parents=True)
    (main / ".claude/skills/a/SKILL.md").write_text("team", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team skill")
    refuse_tracked(main, [".claude/skills/b/SKILL.md", ".specify/project.json"])
    with pytest.raises(ValueError, match=r"tracked file \.claude/skills/a/SKILL\.md"):
        refuse_tracked(main, [".claude/skills/b/SKILL.md", ".claude/skills/a/SKILL.md"])
    refuse_tracked(tmp_path / "not-a-repo", [".claude/skills/a/SKILL.md"])


def _snapshot(*roots: Path) -> dict[Path, bytes | None]:
    return {
        path: (None if path.is_dir() else path.read_bytes())
        for root in roots if root.exists()
        for path in root.rglob("*")
    }


def _private_workspace(path: Path, key: str) -> Path:
    """A private workspace that lists *key* and has one extension command and one custom preset command."""
    _identity(path, private=True)
    specify = path / ".specify"
    (specify / "integration.json").write_text(
        json.dumps({"integration": key, "installed_integrations": [key]}), encoding="utf-8"
    )
    (specify / "init-options.json").write_text(
        json.dumps({"ai": key, "integration": key, "script": "sh"}), encoding="utf-8"
    )
    extension = specify / "extensions/external"
    (extension / "commands").mkdir(parents=True)
    (extension / "extension.yml").write_text(
        "schema_version: '1.0'\n"
        "extension:\n  id: external\n  name: External\n  version: 1.0.0\n  description: Check\n"
        "requires:\n  speckit_version: '>=0.1'\n"
        "provides:\n  commands:\n    - name: speckit.external.boot\n      file: commands/boot.md\n",
        encoding="utf-8",
    )
    (extension / "commands/boot.md").write_text("---\ndescription: Boot\n---\nBoot.\n", encoding="utf-8")
    (specify / "extensions/.registry").write_text(
        json.dumps({"extensions": {"external": {"enabled": True, "version": "1.0.0"}}}), encoding="utf-8"
    )
    preset = install_preset(path, "team", {"commands": [{"name": "speckit.team.review"}]})
    (preset / "commands").mkdir()
    (preset / "commands/speckit.team.review.md").write_text(
        "---\ndescription: Review\n---\nReview.\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    for name, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data", "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    return home


@pytest.mark.parametrize("mode", ["init", "link"])
@pytest.mark.parametrize("key", sorted(INTEGRATION_REGISTRY))
def test_preflight_dry_run_changes_nothing_and_lists_private_targets(tmp_path, isolated_home, key, mode):
    checkout = _repo(tmp_path / "code")
    workspace = _private_workspace(tmp_path / "ws", key) if mode == "link" else None
    integration = INTEGRATION_REGISTRY[key]
    before = _snapshot(checkout, isolated_home, tmp_path / "ws")
    environment = dict(os.environ)
    raw = "--commands-dir .myagent/commands" if key == "generic" else None
    if mode == "init":
        parsed = parse_integration_options(integration, raw) if raw else None
        planned = plan_checkout_files(lambda staged: install_integration(
            staged, integration, version="1", script_type="sh", raw_options=raw, parsed_options=parsed,
        ))
    else:
        if raw:
            state = json.loads((workspace / ".specify/integration.json").read_text(encoding="utf-8"))
            state["integration_settings"] = {key: {"raw_options": raw}}
            (workspace / ".specify/integration.json").write_text(json.dumps(state), encoding="utf-8")
            before = _snapshot(checkout, isolated_home, tmp_path / "ws")
        planned = plan_checkout_files(lambda staged: attach_integrations(staged, [key], version="1"), workspace=workspace)
    assert _snapshot(checkout, isolated_home, tmp_path / "ws") == before
    assert dict(os.environ) == environment
    assert ".specify/project.json" in planned
    assert f".specify/integrations/{key}.manifest.json" in planned
    assert not {".claude/settings.json", ".vscode/settings.json"} & set(planned)
    if mode == "link" and key != "hermes":  # Hermes installs its skills in the home directory.
        assert any("external" in path for path in planned), planned
        if key != "generic":  # Preset registration has no command directory for generic.
            assert any("team" in path and "review" in path for path in planned), planned


def test_reconcile_keeps_a_modified_file_of_a_stale_integration(tmp_path, isolated_home, monkeypatch):
    from typer.testing import CliRunner

    from specify_cli import app

    workspace = _private_workspace(tmp_path / "ws", "claude").resolve()
    project_id = json.loads((workspace / ".specify/workspace.json").read_text(encoding="utf-8"))["project_id"]
    checkout = _repo(tmp_path / "code")
    (checkout / ".specify").mkdir()
    (checkout / ".specify/project.json").write_text(
        json.dumps({"schema_version": 1, "project_id": project_id, "storage": "external"}), encoding="utf-8",
    )
    (checkout / ".specify/checkout.json").write_text(
        json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None}), encoding="utf-8",
    )
    attach_integrations(checkout, ["claude"], version="1")
    local = checkout / ".specify/integrations/claude.manifest.json"
    local.write_text(json.dumps({**json.loads(local.read_text(encoding="utf-8")), "version": "0"}), encoding="utf-8")
    assert plan_reconcile(checkout, workspace, version="1") == (["claude"], ["claude"], {})
    skill = checkout / ".claude/skills/speckit-plan/SKILL.md"
    skill.write_text("# Mine\n", encoding="utf-8")

    assert plan_reconcile(checkout, workspace, version="1") == ([], [], {"claude": [".claude/skills/speckit-plan/SKILL.md"]})
    monkeypatch.chdir(checkout)
    result = CliRunner().invoke(app, ["project", "link", str(workspace)])

    assert result.exit_code == 0, result.output
    assert (
        "Kept modified file: .claude/skills/speckit-plan/SKILL.md\n"
        "Repair: specify integration upgrade claude --force\n"
    ) in result.output
    assert skill.read_text(encoding="utf-8") == "# Mine\n"
    assert json.loads(local.read_text(encoding="utf-8"))["version"] == "0"


def _cli(monkeypatch, directory: Path, *args: str):
    from typer.testing import CliRunner

    from specify_cli import app

    monkeypatch.chdir(directory)
    return CliRunner().invoke(app, list(args))


def _init(monkeypatch, checkout: Path, workspace: Path, *options: str):
    return _cli(
        monkeypatch, checkout, "init", "--here", "--force", "--ignore-agent-tools", "--offline", "--non-interactive",
        "--workspace", str(workspace), "--script", "sh", "--no-workspace-git", *options,
    )


def test_begin_marker_without_end_refuses_and_keeps_user_lines(tmp_path: Path):
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    original = f"# user\n{BEGIN}\n/.specify/\n# later user line\n/build/\n".encode()
    path.write_bytes(original)
    _attach(main)
    with pytest.raises(ValueError, match=r"exclude.*Fix these markers"):
        regenerate_exclude_block(main)
    assert path.read_bytes() == original


def test_rules_match_names_with_glob_characters_literally(tmp_path: Path):
    main = _repo(tmp_path / "main")
    names = ["a*.md", "[x].md", "#c.md", "!d.md"]
    _attach(main, files=names)
    regenerate_exclude_block(main)
    for name in [*names, "ab.md", "x.md"]:
        (main / name).write_text("x", encoding="utf-8")
    status = _git(main, "status", "--porcelain", "--untracked-files=all")
    assert sorted(line[3:] for line in status.splitlines()) == ["ab.md", "x.md"]


def test_private_mode_refuses_a_git_subdirectory(tmp_path: Path, isolated_home, monkeypatch):
    main = _repo(tmp_path / "main")
    sub = main / "sub"
    sub.mkdir()
    refuse_tracked(main, [])
    with pytest.raises(ValueError, match="Git top level"):
        refuse_tracked(sub, [])

    result = _init(monkeypatch, sub, tmp_path / "ws", "--integration", "claude", "--private")
    assert result.exit_code != 0
    assert "Git top level" in " ".join(result.output.split())
    assert list(sub.iterdir()) == []

    workspace = _private_workspace(tmp_path / "linked", "claude")
    with pytest.raises(ValueError, match="Git top level"):
        plan_reconcile(sub, workspace, version="1")


def test_dry_run_keeps_omp_out_of_the_real_agent_directories(tmp_path, isolated_home, monkeypatch):
    from specify_cli.integrations.omp.global_commands import command_directory

    agent, config = tmp_path / "pi-agent", tmp_path / "pi-config"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent))
    monkeypatch.setenv("PI_CONFIG_DIR", str(config))
    environment = dict(os.environ)
    with _isolated_user_dirs(tmp_path / "sandbox"):
        assert command_directory().is_relative_to(tmp_path / "sandbox")
    plan_checkout_files(lambda staged: install_integration(
        staged, INTEGRATION_REGISTRY["omp"], version="1", script_type="sh", global_commands=True,
    ))
    assert dict(os.environ) == environment
    assert not agent.exists() and not (isolated_home / "pi-config").exists()


def test_restore_installs_user_wide_commands_in_the_real_home_only(tmp_path, isolated_home, monkeypatch):
    from specify_cli.integrations.hermes import HermesIntegration
    from specify_cli.integrations.omp.global_commands import command_directory, uninstall_global_commands

    checkout, workspace = _repo(tmp_path / "code"), tmp_path / "ws"
    result = _init(monkeypatch, checkout, workspace, "--integration", "omp", "--global-commands")
    assert result.exit_code == 0, result.output
    result = _cli(monkeypatch, checkout, "integration", "install", "hermes", "--force")
    assert result.exit_code == 0, result.output
    # A second machine: a new home, and user data folders outside it.
    home = tmp_path / "second home"
    home.mkdir()
    for name, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": tmp_path / "xdg/config",
        "XDG_DATA_HOME": tmp_path / "xdg/data", "APPDATA": tmp_path / "xdg/config", "LOCALAPPDATA": tmp_path / "xdg/data",
    }.items():
        monkeypatch.setenv(name, str(value))

    restore_checkout(checkout, workspace.resolve(), None)

    launchers = sorted(path.name for path in command_directory().glob("*.md"))
    assert "speckit.plan.md" in launchers
    assert (HermesIntegration.global_skills_dir() / "speckit-plan/SKILL.md").is_file()
    assert {path.relative_to(home).parts[0] for path in home.rglob("*")} == {".omp", ".hermes"}
    removed, conflicts = uninstall_global_commands()  # The launchers have a real ownership record.
    assert sorted(path.name for path in removed) == launchers and conflicts == []


def test_restore_workspace_files_restores_missing_helpers_only(tmp_path, isolated_home, monkeypatch):
    checkout, workspace = _repo(tmp_path / "code"), tmp_path / "ws"
    result = _init(monkeypatch, checkout, workspace, "--integration", "claude", "--private")
    assert result.exit_code == 0, result.output
    workspace = workspace.resolve()
    missing = [".specify/scripts/bash/common.sh", ".specify/templates/plan-template.md"]
    original = {name: (workspace / name).read_bytes() for name in missing}
    for name in missing:
        (workspace / name).unlink()
    edited = workspace / ".specify/templates/spec-template.md"
    edited.write_text("# Mine\n", encoding="utf-8")
    checkout_before = _snapshot(checkout)

    assert sorted(restore_workspace_files(workspace)) == missing

    assert {name: (workspace / name).read_bytes() for name in missing} == original
    assert edited.read_text(encoding="utf-8") == "# Mine\n"
    assert _snapshot(checkout) == checkout_before
    before = _snapshot(workspace)
    assert restore_workspace_files(workspace) == []
    assert _snapshot(workspace) == before


@pytest.mark.parametrize(("key", "layout"), [
    ("claude", ".claude/skills/speckit-{}/SKILL.md"), ("gemini", ".gemini/commands/speckit.{}.toml"),
])
@pytest.mark.parametrize("route", ["init", "extension-add"])
def test_private_extension_commands_refuse_a_tracked_target(tmp_path, isolated_home, monkeypatch, route, key, layout):
    checkout, workspace = _repo(tmp_path / "code"), tmp_path / "ws"
    name = layout.format("git-feature" if key == "claude" else "git.feature")
    tracked = checkout / name
    tracked.parent.mkdir(parents=True)
    tracked.write_text("team\n", encoding="utf-8")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "team command")
    options = ("--integration", key, "--private")

    if route == "init":
        _init(monkeypatch, checkout, workspace, *options, "--extension", "git")
    else:
        assert _init(monkeypatch, checkout, workspace, *options).exit_code == 0
        result = _cli(monkeypatch, checkout, "extension", "add", "git")
        assert result.exit_code == 1
        assert f"tracked file {name}" in " ".join(result.output.split())

    assert tracked.read_text(encoding="utf-8") == "team\n"
    assert not (checkout / layout.format("git-commit" if key == "claude" else "git.commit")).exists()
    assert (checkout / layout.format("plan")).is_file()
    assert not (workspace / ".specify/extensions/git").exists()
    registry = workspace / ".specify/extensions/.registry"
    assert "git" not in (json.loads(registry.read_text(encoding="utf-8")).get("extensions", {}) if registry.exists() else {})


@pytest.mark.parametrize("tracked", [True, False])
def test_private_preset_skills_refuse_a_tracked_target_and_hide_the_rest(tmp_path, isolated_home, monkeypatch, tracked):
    import yaml

    checkout, workspace = _repo(tmp_path / "code"), tmp_path / "ws"
    skill = checkout / ".github/skills/speckit-team-review/SKILL.md"
    if tracked:
        skill.parent.mkdir(parents=True)
        skill.write_text("team\n", encoding="utf-8")
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-q", "-m", "team skill")
    result = _init(
        monkeypatch, checkout, workspace, "--integration", "copilot", "--integration-options", "--skills", "--private",
    )
    assert result.exit_code == 0, result.output
    source = tmp_path / "preset"
    (source / "commands").mkdir(parents=True)
    (source / "commands/speckit.team.review.md").write_text("---\ndescription: Review\n---\nReview.\n", encoding="utf-8")
    (source / "preset.yml").write_text(yaml.dump({
        "schema_version": "1.0",
        "preset": {"id": "team", "name": "Team", "version": "1.0.0", "description": "Team commands"},
        "requires": {"speckit_version": ">=0.1.0"},
        "provides": {"templates": [
            {"type": "command", "name": "speckit.team.review", "file": "commands/speckit.team.review.md"},
        ]},
    }), encoding="utf-8")

    result = _cli(monkeypatch, checkout, "preset", "add", "--dev", str(source))

    registry = workspace / ".specify/presets/.registry"
    installed = json.loads(registry.read_text(encoding="utf-8")).get("presets", {}) if registry.exists() else {}
    if tracked:
        assert result.exit_code == 1
        assert "tracked file .github/skills/speckit-team-review/SKILL.md" in " ".join(result.output.split())
        assert skill.read_text(encoding="utf-8") == "team\n"
        assert not (workspace / ".specify/presets/team").exists() and "team" not in installed
    else:
        assert result.exit_code == 0, result.output
        assert skill.is_file() and "team" in installed
        assert _git(checkout, "status", "--porcelain", "--untracked-files=all") == ""
