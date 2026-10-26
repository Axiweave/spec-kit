"""specify feature list reports the invoking project's features on stdout and changes nothing."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=directory, check=True, capture_output=True)


def put(root: Path, rel: str, text: str = "# x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config", "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
        "GIT_AUTHOR_NAME": "Team", "GIT_COMMITTER_NAME": "Team",
        "GIT_AUTHOR_EMAIL": "team@example.com", "GIT_COMMITTER_EMAIL": "team@example.com",
    }.items():
        monkeypatch.setenv(key, str(value))
    for key in ("SPECIFY_INIT_DIR", "SPECIFY_FEATURE_DIRECTORY"):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture
def project(tmp_path, monkeypatch, isolated_home):
    root = tmp_path / "repo"
    (root / ".specify").mkdir(parents=True)
    (root / ".specify/init-options.json").write_text('{"feature_numbering": "sequential"}', encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def invoke(*args: str):
    return CliRunner().invoke(app, ["feature", "list", *args])


def test_json_is_one_document_on_stdout(project):
    marked = "".join(f"- [NEEDS CLARIFICATION: q{i}]\n" for i in range(5))
    put(project, "specs/001-a/spec.md", marked)

    result = invoke("--json")

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    data = json.loads(result.stdout)
    assert list(data) == [
        "workspace_root", "recommendation", "features", "duplicate_prefixes",
        "roadmaps", "roadmap_findings", "roadmap_recommendation",
    ]
    assert (data["roadmaps"], data["roadmap_findings"], data["roadmap_recommendation"]) == ([], [], None)
    assert [m["line"] for m in data["features"][0]["markers"]] == [1, 2, 3, 4, 5]


def test_no_project_is_an_error_on_stderr(tmp_path, monkeypatch, isolated_home):
    (tmp_path / "plain").mkdir()
    monkeypatch.chdir(tmp_path / "plain")

    result = invoke("--json")

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: ")


def test_human_output_lists_unfinished_rows_then_one_done_line(project):
    put(project, "specs/001-done/spec.md")
    put(project, "specs/001-done/tasks.md", "- [x] T001 a\n")
    put(project, "specs/002-work/spec.md")
    put(project, "specs/002-work/tasks.md", "- [x] T001 a\n- [ ] T002 b\n")
    put(project, "specs/002-work/checklists/ux.md", "- [ ] a\n")
    put(project, "specs/003-nospec/plan.md")
    put(project, "specs/004-done/spec.md")
    put(project, "specs/004-done/tasks.md", "- [X] T001 a\n")

    result = invoke()

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "Next: specs/002-work -> speckit.implement (finish-before-start)",
        "specs/002-work  In progress  1/2 (T002)  next: speckit.implement",
        "specs/003-nospec  No spec  spec.md: no-spec",
        "specs/002-work  checklists/ux.md: 1 open (owner: reviewer)",
        "Done (2): specs/001-done, specs/004-done",
    ]


def test_a_no_command_recommendation_names_the_finding(project):
    put(project, "specs/001-nospec/plan.md")

    assert invoke().stdout.splitlines()[0] == "Next: specs/001-nospec -> spec.md: no-spec (resolve-blocker)"


_HEAD = "| ID | Sub-feature | Intent | Scope boundary | Depends on | Status | Sub-spec |\n|---|---|---|---|---|---|---|\n"


def test_human_output_adds_the_roadmap_entry_line_and_finding_lines(project):
    put(project, "specs/001-a/spec.md", "# A\n\n**Input**: Parent roadmap: `specs/epic/roadmap.md` → entry **R1**. A\n")
    put(project, "specs/001-a/tasks.md", "- [x] T001 a\n")
    put(project, "specs/epic/roadmap.md", _HEAD + "| R1 | a | i | b | — | in-progress | specs/001-a |\n| R2 | b | i | b | R1 | planned | — |\n")
    put(project, "specs/other/roadmap.md", "# no table\n")

    lines = invoke().stdout.splitlines()

    assert lines == [
        "Next: speckit.specify (no-unfinished)",
        "Next roadmap entry: specs/epic/roadmap.md R2 -> speckit.specify",
        "Done (1): specs/001-a",
        "Roadmap specs/epic/roadmap.md:3  status-drift  R1  Stored status in-progress, expected done.",
        "Roadmap specs/other/roadmap.md:-  unreadable-roadmap  -  The file has no roadmap table header.",
    ]


def test_human_output_without_roadmaps_has_no_roadmap_lines(project):
    put(project, "specs/001-a/spec.md")
    put(project, "specs/notes/roadmap-ideas.md")

    assert not any("oadmap" in line for line in invoke().stdout.splitlines())


def test_duplicate_prefixes_match_project_info(project):
    for name in ("008-login", "008-billing", "009-unique"):
        put(project, f"specs/{name}/spec.md")

    listed = json.loads(invoke("--json").stdout)["duplicate_prefixes"]
    info = json.loads(CliRunner().invoke(app, ["project", "info", "--json"]).stdout)["duplicate_prefixes"]

    assert listed == info == [["specs/008-billing", "specs/008-login"]]
    assert "Duplicate feature prefix: specs/008-billing, specs/008-login" in invoke().stdout


def test_linked_worktrees_of_external_storage_report_the_same_features(tmp_path, monkeypatch, isolated_home):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "README.md").write_text("team\n", encoding="utf-8")
    _git(main, "add", "-A")
    _git(main, "commit", "-q", "-m", "team")
    monkeypatch.chdir(main)
    workspace = tmp_path / "ws"
    init = CliRunner().invoke(app, [
        "init", "--here", "--force", "--integration", "claude", "--ignore-agent-tools", "--offline",
        "--non-interactive", "--workspace", str(workspace), "--script", "sh", "--no-workspace-git",
    ])
    assert init.exit_code == 0, init.output
    put(workspace, "specs/001-shared/spec.md")
    worktree = tmp_path / "worktree"
    _git(main, "worktree", "add", "-q", str(worktree))
    monkeypatch.chdir(worktree)
    link = CliRunner().invoke(app, ["project", "link", str(workspace)])
    assert link.exit_code == 0, link.output

    in_worktree = json.loads(invoke("--json").stdout)
    monkeypatch.chdir(main)
    in_main = json.loads(invoke("--json").stdout)

    assert [row["path"] for row in in_main["features"]] == ["specs/001-shared"]
    assert in_worktree["features"] == in_main["features"]
