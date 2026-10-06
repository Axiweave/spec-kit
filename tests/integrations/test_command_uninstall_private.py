"""Private mode: integration commands change only the current checkout's files."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app

SKILL = ".claude/skills/speckit-plan/SKILL.md"


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True, text=True).stdout


def _run(monkeypatch, directory: Path, *args: str):
    monkeypatch.chdir(directory)
    result = CliRunner().invoke(app, list(args))
    return result.exit_code, " ".join(result.output.split())


def _files(directory: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in directory.rglob("*") if p.is_file() and ".git" not in p.parts}


def _clean(*checkouts: Path) -> bool:
    return all(_git(c, "status", "--porcelain", "--untracked-files=all") == "" for c in checkouts)


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    """A private claude project in a code repository and an attached linked worktree."""
    home = tmp_path / "home"
    for name, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data", "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
        "GIT_AUTHOR_NAME": "Team", "GIT_COMMITTER_NAME": "Team",
        "GIT_AUTHOR_EMAIL": "team@example.com", "GIT_COMMITTER_EMAIL": "team@example.com",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    main, worktree, workspace = tmp_path / "main", tmp_path / "worktree", tmp_path / "ws"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_text("team\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team")
    code, output = _run(
        monkeypatch, main, "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools",
        "--offline", "--non-interactive", "--workspace", str(workspace), "--private", "--script", "sh",
        "--no-workspace-git",
    )
    assert code == 0, output
    _git(main, "worktree", "add", "-q", str(worktree))
    code, output = _run(monkeypatch, worktree, "project", "link", str(workspace))
    assert code == 0, output
    return main, worktree, workspace.resolve()


def _listed(workspace: Path) -> list[str]:
    path = workspace / ".specify/integration.json"
    return json.loads(path.read_text(encoding="utf-8")).get("installed_integrations", []) if path.exists() else []


def test_default_uninstall_removes_only_this_checkout(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    exclude = Path(_git(main, "rev-parse", "--git-path", "info/exclude").strip())
    exclude = exclude if exclude.is_absolute() else main / exclude
    exclude.write_text(exclude.read_text(encoding="utf-8") + "/.claude/settings.local.json\n", encoding="utf-8")
    (worktree / ".claude/settings.local.json").write_text('{"user": 1}\n', encoding="utf-8")
    main_before = _files(main)
    shared_before = _files(workspace / ".specify")

    code, output = _run(monkeypatch, worktree, "integration", "uninstall", "claude")

    assert code == 0, output
    assert (
        "Removed claude from this checkout. The project still lists claude. To remove it from the project, "
        "run: specify integration uninstall claude --project"
    ) in output
    assert not (worktree / SKILL).exists()
    assert not (worktree / ".specify/integrations/claude.manifest.json").exists()
    assert _files(main) == main_before
    assert _files(workspace / ".specify") == shared_before
    assert _listed(workspace) == ["claude"]
    before, _, rest = exclude.read_text(encoding="utf-8").partition("# BEGIN")
    assert "/.claude/settings.local.json\n" in before + rest.partition("# END Spec Kit private mode\n")[2]
    assert _clean(main, worktree)
    code, output = _run(monkeypatch, main, "integration", "list")
    assert code == 0 and "claude" in output


def test_default_uninstall_without_checkout_files_succeeds(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    assert _run(monkeypatch, worktree, "integration", "uninstall", "claude")[0] == 0

    code, output = _run(monkeypatch, worktree, "integration", "uninstall", "claude")

    assert code == 0, output
    assert "Removed claude from this checkout." in output
    assert _listed(workspace) == ["claude"]
    assert _clean(main, worktree)

def test_default_uninstall_keeps_dispatcher_and_other_checkout_hooks(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    (workspace / ".specify/integration-events.yml").write_text(
        "integrations:\n  claude:\n    events:\n      stop:\n        command: speckit.plan\n", encoding="utf-8",
    )
    for checkout in (main, worktree):
        assert _run(monkeypatch, checkout, "integration", "uninstall", "claude")[0] == 0
        code, output = _run(monkeypatch, checkout, "integration", "install", "claude")
        assert code == 0, output
    hooks = main / ".claude/settings.local.json"
    dispatcher = workspace / ".specify/events.py"
    assert "Stop" in json.loads(hooks.read_text(encoding="utf-8"))["hooks"]
    assert (worktree / ".claude/settings.local.json").is_file()
    hooks_before, dispatcher_before = hooks.read_bytes(), dispatcher.read_bytes()

    code, output = _run(monkeypatch, worktree, "integration", "uninstall", "claude")

    assert code == 0, output
    assert not (worktree / ".claude/settings.local.json").exists()
    assert dispatcher.read_bytes() == dispatcher_before
    assert hooks.read_bytes() == hooks_before
    assert _clean(main, worktree)

def test_project_uninstall_removes_shared_state_and_link_cleans_other_checkout(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    code, output = _run(monkeypatch, worktree, "integration", "uninstall", "claude", "--project")
    assert code == 0, output
    assert "Other checkouts keep their claude files. Run specify project link in each one to remove them." in output
    assert not (workspace / ".specify/integrations/claude.manifest.json").exists()
    assert "claude" not in _listed(workspace)
    assert not (worktree / SKILL).exists()
    assert (main / SKILL).exists()
    assert _clean(main, worktree)

    code, output = _run(monkeypatch, main, "project", "link", str(workspace))
    assert code == 0, output
    assert not (main / SKILL).exists()
    assert not (main / ".specify/integrations/claude.manifest.json").exists()
    assert _clean(main, worktree)


def test_project_option_is_rejected_in_default_mode(tmp_path, monkeypatch):
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(tmp_path / "home"))
    code, output = _run(
        monkeypatch, tmp_path, "init", "plain", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(tmp_path / "ws"), "--script", "sh", "--no-workspace-git",
    )
    assert code == 0, output
    code, output = _run(monkeypatch, tmp_path / "plain", "integration", "uninstall", "claude", "--project")
    assert code == 1
    assert "--project applies only to private mode." in output
    assert (tmp_path / "plain" / SKILL).exists()


def test_switch_and_use_change_only_this_checkout(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    main_before = _files(main)
    code, output = _run(monkeypatch, worktree, "integration", "switch", "codex")
    assert code == 0, output
    assert not (worktree / SKILL).exists()
    assert (worktree / ".agents/skills/speckit-plan/SKILL.md").exists()
    assert set(_listed(workspace)) == {"claude", "codex"}
    assert _files(main) == main_before
    assert _clean(main, worktree)

    code, output = _run(monkeypatch, worktree, "integration", "use", "codex")
    assert code == 0, output
    assert _files(main) == main_before
    assert _clean(main, worktree)


def test_upgrade_and_reinstall_leave_other_checkout_byte_identical(checkouts, monkeypatch):
    main, worktree, workspace = checkouts
    main_before = _files(main)
    code, output = _run(monkeypatch, worktree, "integration", "upgrade", "claude")
    assert code == 0, output
    assert _files(main) == main_before
    assert _clean(main, worktree)

    assert _run(monkeypatch, worktree, "integration", "uninstall", "claude")[0] == 0
    code, output = _run(monkeypatch, worktree, "integration", "install", "claude")
    assert code == 0, output
    assert (worktree / SKILL).exists()
    assert _files(main) == main_before
    assert _clean(main, worktree)
