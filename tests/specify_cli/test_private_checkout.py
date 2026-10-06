"""Private mode: the workspace flag, the managed exclude block, and the tracked-file preflight."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from specify_cli._private_checkout import (
    BEGIN, END, attach_integrations, exclude_path, install_integration, plan_checkout_files,
    refuse_tracked, regenerate_exclude_block,
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


def test_symlinked_exclude_file_is_refused(tmp_path: Path):
    main = _repo(tmp_path / "main")
    path = exclude_path(main)
    target = tmp_path / "elsewhere"
    target.write_text("", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    _attach(main)
    with pytest.raises(ValueError, match="symlinked exclude file"):
        regenerate_exclude_block(main)
    assert target.read_text(encoding="utf-8") == ""


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
