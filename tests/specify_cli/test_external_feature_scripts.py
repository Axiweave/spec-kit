"""Public script behavior for repository-local and external workspaces."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tests.conftest import requires_bash
from tests.parity_helpers import (
    HAS_POWERSHELL,
    POWERSHELL_EXE,
    bash_cmd,
    clean_env,
    install_composition_stack,
    install_scripts,
    ps_cmd,
    py_cmd,
    run,
    write_feature_json,
)

SCRIPTS = (
    "create-new-feature",
    "setup-plan",
    "setup-tasks",
    "check-prerequisites",
    "resolve-template",
)
TEMPLATES = {
    "spec-template": "# Workspace specification\n",
    "plan-template": "# Workspace implementation plan\n",
    "tasks-template": "# Workspace tasks\n",
}
PS_OPTIONS = {
    "--json": "-Json",
    "--short-name": "-ShortName",
    "--number": "-Number",
    "--timestamp": "-Timestamp",
    "--paths-only": "-PathsOnly",
    "--require-spec": "-RequireSpec",
    "--require-tasks": "-RequireTasks",
    "--include-tasks": "-IncludeTasks",
    "--template": "-Template",
}


@pytest.fixture(params=[
    pytest.param("bash", marks=requires_bash),
    "python",
    pytest.param(
        "powershell",
        marks=pytest.mark.skipif(not HAS_POWERSHELL, reason="no PowerShell available"),
    ),
])
def variant(request):
    return request.param


@pytest.fixture
def script_env(tmp_path):
    env = clean_env()
    for key in list(env):
        if key.startswith(("SPECKIT_", "GIT_")):
            env.pop(key)
    home = tmp_path / "home"
    home.mkdir()
    env.update({
        "PATH": str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "APPDATA": str(tmp_path / "config"),
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "External Setup Test",
        "GIT_AUTHOR_EMAIL": "setup@example.test",
        "GIT_COMMITTER_NAME": "External Setup Test",
        "GIT_COMMITTER_EMAIL": "setup@example.test",
    })
    return env


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _make_project(tmp_path, external, policy="automatic"):
    repo = tmp_path / "code project"
    workspace = tmp_path / "external workspace" if external else repo
    (repo / ".specify").mkdir(parents=True)
    record = None
    if external:
        project_id = str(uuid4())
        _write_json(repo / ".specify/project.json", {
            "schema_version": 1, "project_id": project_id, "storage": "external",
        })
        _write_json(workspace / ".specify/workspace.json", {
            "schema_version": 1, "project_id": project_id,
        })
        record = tmp_path / "data/specify/projects" / f"{project_id}.json"
        _write_json(record, {
            "schema_version": 1, "workspace": str(workspace), "active_feature": None,
        })
    for script in SCRIPTS:
        install_scripts(workspace, script)
    templates = workspace / ".specify/templates"
    templates.mkdir(parents=True)
    for name, content in TEMPLATES.items():
        (templates / f"{name}.md").write_text(content, encoding="utf-8")
    if policy is not None:
        _write_json(workspace / ".specify/init-options.json", {"feature_selection": policy})
    return repo, workspace, record


@pytest.fixture(params=[False, True], ids=["local", "external"])
def project(tmp_path, request):
    return _make_project(tmp_path, request.param)


def _invoke(variant, workspace, cwd, env, script, *args):
    command = {"bash": bash_cmd, "python": py_cmd, "powershell": ps_cmd}[variant]
    if variant == "powershell":
        args = tuple(PS_OPTIONS.get(arg, arg) for arg in args)
    return run(command(workspace, script, *args), cwd, env=env, timeout=30)


def _success(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _select(project, relative="specs/007-saved-feature"):
    repo, workspace, record = project
    feature = workspace / relative
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Existing specification\n", encoding="utf-8")
    if record is None:
        write_feature_json(repo, relative)
    else:
        data = json.loads(record.read_text(encoding="utf-8"))
        data["active_feature"] = relative
        _write_json(record, data)
    return feature


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def test_feature_creation_uses_workspace_and_persists_selection(
    project, variant, script_env,
):
    repo, workspace, record = project
    before = _snapshot(repo)
    result = _invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", "--number", "3", "--short-name", "external-storage",
        "Store project specifications",
    )
    output = _success(result)
    feature = workspace / "specs/003-external-storage"
    assert Path(output["SPEC_FILE"]) == feature / "spec.md"
    assert (feature / "spec.md").read_text(encoding="utf-8") == TEMPLATES["spec-template"]
    if record is None:
        state = json.loads((repo / ".specify/feature.json").read_text(encoding="utf-8"))
        assert state["feature_directory"] == "specs/003-external-storage"
    else:
        state = json.loads(record.read_text(encoding="utf-8"))
        assert state["active_feature"] == "specs/003-external-storage"
        assert state["workspace"] == str(workspace)
        assert _snapshot(repo) == before
    paths = _success(_invoke(
        variant, workspace, repo, script_env, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(paths["FEATURE_DIR"]) == feature
    assert Path(paths["REPO_ROOT"]) == repo


def test_fresh_session_plan_tasks_and_prerequisites_use_saved_feature(
    project, variant, script_env,
):
    repo, workspace, _ = project
    feature = _select(project)
    nested = repo / "src/nested"
    nested.mkdir(parents=True)
    before = _snapshot(repo)
    plan = _success(_invoke(variant, workspace, nested, script_env, "setup-plan", "--json"))
    assert Path(plan["FEATURE_DIR"]) == feature
    assert Path(plan["FEATURE_SPEC"]) == feature / "spec.md"
    assert Path(plan["IMPL_PLAN"]) == feature / "plan.md"
    assert (feature / "plan.md").read_text(encoding="utf-8") == TEMPLATES["plan-template"]

    (feature / "plan.md").write_text("# User plan\n", encoding="utf-8")
    _success(_invoke(variant, workspace, nested, script_env, "setup-plan", "--json"))
    assert (feature / "plan.md").read_text(encoding="utf-8") == "# User plan\n"
    (feature / "research.md").write_text("# Research\n", encoding="utf-8")
    tasks = _success(_invoke(variant, workspace, nested, script_env, "setup-tasks", "--json"))
    assert Path(tasks["FEATURE_DIR"]) == feature
    assert Path(tasks["TASKS_TEMPLATE"]) == workspace / ".specify/templates/tasks-template.md"
    assert tasks["TASKS_TEMPLATE_CONTENT"] == TEMPLATES["tasks-template"]
    assert tasks["AVAILABLE_DOCS"] == ["research.md"]

    paths = _success(_invoke(
        variant, workspace, nested, script_env, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(paths["REPO_ROOT"]) == repo
    assert Path(paths["TASKS"]) == feature / "tasks.md"
    # The agent, not setup-tasks, writes the returned template to the tasks path.
    Path(paths["TASKS"]).write_text(tasks["TASKS_TEMPLATE_CONTENT"], encoding="utf-8")
    checked = _success(_invoke(
        variant, workspace, nested, script_env, "check-prerequisites",
        "--json", "--require-spec", "--require-tasks", "--include-tasks",
        "--template", "tasks-template",
    ))
    assert Path(checked["FEATURE_DIR"]) == feature
    assert checked["AVAILABLE_DOCS"] == ["research.md", "tasks.md"]
    assert checked["TEMPLATE_CONTENT"] == TEMPLATES["tasks-template"]
    if workspace != repo:
        assert _snapshot(repo) == before


@pytest.mark.parametrize("layer", ["composition", "override"])
def test_template_resolution_uses_workspace_layers_without_active_feature(
    project, variant, script_env, layer,
):
    repo, workspace, _ = project
    expected = install_composition_stack(workspace, "plan-template", "# Core plan\n")
    if layer == "override":
        override = workspace / ".specify/templates/overrides/plan-template.md"
        override.parent.mkdir()
        expected = "# Selected project override\n"
        override.write_text(expected, encoding="utf-8")
    before = _snapshot(repo)
    output = _success(_invoke(
        variant, workspace, repo, script_env, "resolve-template", "plan-template", "--json",
    ))
    assert output["TEMPLATE_NAME"] == "plan-template"
    assert output["TEMPLATE_CONTENT"] == expected
    assert _snapshot(repo) == before


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_explicit_feature_override_wins_over_saved_selection(
    project, variant, script_env, absolute,
):
    repo, workspace, record = project
    saved = _select(project)
    selected = workspace / "specs/008-explicit-feature"
    selected.mkdir()
    script_env["SPECIFY_FEATURE_DIRECTORY"] = str(selected) if absolute else "specs/008-explicit-feature"
    script_env["SPECIFY_FEATURE"] = "unrelated-branch-name"
    state = record if record is not None else repo / ".specify/feature.json"
    before = state.read_bytes()
    paths = _success(_invoke(
        variant, workspace, repo, script_env, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(paths["FEATURE_DIR"]) == selected
    assert Path(paths["REPO_ROOT"]) == repo
    assert state.read_bytes() == before
    assert (saved / "spec.md").read_text(encoding="utf-8") == "# Existing specification\n"

    _success(_invoke(variant, workspace, repo, script_env, "setup-plan", "--json"))
    assert (selected / "plan.md").read_text(encoding="utf-8") == TEMPLATES["plan-template"]
    assert not (saved / "plan.md").exists()
    if record is not None:
        assert sorted(path.name for path in (repo / ".specify").iterdir()) == ["project.json"]


def test_local_absolute_feature_override_can_remain_outside_repository(
    tmp_path, variant, script_env,
):
    repo, workspace, _ = _make_project(tmp_path, False)
    outside = tmp_path / "legacy feature"
    outside.mkdir()
    script_env["SPECIFY_FEATURE_DIRECTORY"] = str(outside)
    plan = _success(_invoke(variant, workspace, repo, script_env, "setup-plan", "--json"))
    assert Path(plan["IMPL_PLAN"]) == outside / "plan.md"
    assert (outside / "plan.md").read_text(encoding="utf-8") == TEMPLATES["plan-template"]
    assert not (repo / "specs").exists()


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("fault", ["missing-mapping", "foreign-workspace", "missing-workspace"])
def test_invalid_external_workspace_fails_before_any_content_write(
    tmp_path, variant, script_env, script, fault,
):
    project = _make_project(tmp_path, True)
    repo, workspace, record = project
    _select(project)
    if fault == "missing-mapping":
        record.unlink()
    elif fault == "foreign-workspace":
        _write_json(workspace / ".specify/workspace.json", {
            "schema_version": 1, "project_id": str(uuid4()),
        })
    else:
        data = json.loads(record.read_text(encoding="utf-8"))
        data["workspace"] = str(tmp_path / "missing workspace")
        _write_json(record, data)
    _write_json(tmp_path / "config/specify/config.json", {"storage_root": str(tmp_path / "default storage")})
    roots = (repo, workspace, tmp_path / "data", tmp_path / "default storage", tmp_path / "missing workspace")
    before = [_snapshot(root) for root in roots]
    args = ["--json"]
    if script == "create-new-feature":
        args.append("Create a feature")
    elif script == "resolve-template":
        args.append("plan-template")
    result = _invoke(variant, workspace, repo, script_env, script, *args)
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize("policy", ["automatic", "context"])
@pytest.mark.parametrize("override", ["relative-escape", "absolute-escape", "symlink-escape"])
def test_external_feature_override_cannot_write_outside_workspace(
    tmp_path, variant, script_env, override, policy,
):
    project = _make_project(tmp_path, True, policy)
    repo, workspace, _ = project
    _select(project)
    outside = tmp_path / "outside"
    outside.mkdir()
    if override == "relative-escape":
        value = "../outside"
    elif override == "absolute-escape":
        value = str(outside)
    else:
        link = workspace / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks are not available")
        value = "escape"
    script_env["SPECIFY_FEATURE_DIRECTORY"] = value
    roots = (repo, workspace, outside, tmp_path / "data")
    before = [_snapshot(root) for root in roots]
    result = _invoke(variant, workspace, repo, script_env, "setup-plan", "--json")
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip()
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize("script", ["setup-plan", "setup-tasks", "check-prerequisites"])
def test_missing_saved_feature_is_not_recreated(tmp_path, variant, script_env, script):
    project = _make_project(tmp_path, True)
    repo, workspace, record = project
    data = json.loads(record.read_text(encoding="utf-8"))
    data["active_feature"] = "specs/009-removed-feature"
    _write_json(record, data)
    roots = (repo, workspace, tmp_path / "data")
    before = [_snapshot(root) for root in roots]
    result = _invoke(variant, workspace, repo, script_env, script, "--json")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "009-removed-feature" in result.stderr
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize("selection", ["new", "missing-saved", "escape"])
def test_common_helper_validates_before_feature_creation(
    tmp_path, variant, script_env, selection,
):
    """Preflight changes no bytes. Only a confined explicit new path can proceed."""
    project = _make_project(tmp_path, True)
    repo, workspace, record = project
    _select(project)
    relative = "specs/008-new-feature"
    if selection == "missing-saved":
        data = json.loads(record.read_text(encoding="utf-8"))
        data["active_feature"] = relative
        _write_json(record, data)
    else:
        script_env["SPECIFY_FEATURE_DIRECTORY"] = relative if selection == "new" else "../outside-feature"
    script_env["SPECIFY_FEATURE_NO_PERSIST"] = "1"
    if variant == "bash":
        args = [
            "bash", "-c",
            'source "$1"; paths=$(get_feature_paths) || exit 1; eval "$paths"; printf "%s\\n" "$FEATURE_DIR"',
            "_", str(workspace / ".specify/scripts/bash/common.sh"),
        ]
    elif variant == "python":
        args = [
            sys.executable, "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); from common import get_feature_paths; print(get_feature_paths().feature_dir)",
            str(workspace / ".specify/scripts/python"),
        ]
    else:
        common = str(workspace / ".specify/scripts/powershell/common.ps1").replace("'", "''")
        args = [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command",
                f". '{common}'; (Get-FeaturePathsEnv).FEATURE_DIR"]
    roots = (repo, workspace, tmp_path / "data")
    before = [_snapshot(root) for root in roots]

    result = run(args, repo, env=script_env)

    assert [_snapshot(root) for root in roots] == before
    assert not (tmp_path / "outside-feature").exists()
    if selection != "new":
        assert result.returncode != 0
        return
    assert result.returncode == 0, result.stderr
    feature = workspace / relative
    assert Path(result.stdout.strip()) == feature
    assert not feature.exists()
    feature.mkdir(parents=True)
    (feature / "spec.md").write_bytes((workspace / ".specify/templates/spec-template.md").read_bytes())
    script_env.pop("SPECIFY_FEATURE_NO_PERSIST")
    script_env["SPECIFY_FEATURE_DIRECTORY"] = str(feature)
    persisted = run(args, repo, env=script_env)
    assert persisted.returncode == 0, persisted.stderr
    assert json.loads(record.read_text(encoding="utf-8"))["active_feature"] == relative
    script_env.pop("SPECIFY_FEATURE_DIRECTORY")
    recovered = run(args, repo, env=script_env)
    assert recovered.returncode == 0, recovered.stderr
    assert Path(recovered.stdout.strip()) == feature


@pytest.mark.parametrize("xdg", [None, "", "distinct"])
def test_python_windows_record_policy_agrees_with_cli(tmp_path, script_env, xdg):
    """Windows policy prefers nonempty XDG_DATA_HOME, then LOCALAPPDATA."""
    repo, workspace, record = _make_project(tmp_path, True)
    if xdg != "distinct":
        if xdg is None:
            script_env.pop("XDG_DATA_HOME")
        else:
            script_env["XDG_DATA_HOME"] = ""
        target = Path(script_env["LOCALAPPDATA"]) / "specify/projects" / record.name
        target.parent.mkdir(parents=True)
        record.rename(target)
    code = (
        "import os, sys; from pathlib import Path; from types import SimpleNamespace; "
        "sys.path.insert(0, sys.argv[1]); import common; "
        "from specify_cli import workspace; "
        "common.os = workspace.os = SimpleNamespace(**(vars(os) | {'name': 'nt'})); "
        "resolved = common.get_workspace_root(Path(sys.argv[2])); "
        "assert list((workspace.user_data_dir() / 'projects').glob('*.json')); print(resolved)"
    )
    result = run(
        [sys.executable, "-c", code, str(workspace / ".specify/scripts/python"), str(repo)],
        repo, env=script_env,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == workspace


def test_distinct_data_directories_survive_init_link_and_fresh_discovery(tmp_path, variant, script_env):
    """Setup, relinking, and installed helpers must share one machine record."""
    repo, workspace = tmp_path / "repository", tmp_path / "workspace"
    cli = [sys.executable, "-c", "from specify_cli import app; app()"]
    script_type = {"bash": "sh", "python": "py", "powershell": "ps"}[variant]
    initialized = run([
        *cli, "init", str(repo), "--workspace", str(workspace), "--integration", "omp",
        "--script", script_type, "--feature-selection", "automatic", "--offline",
        "--non-interactive", "--ignore-agent-tools",
    ], tmp_path, env=script_env)
    assert initialized.returncode == 0, initialized.stderr
    created = _success(_invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", "--short-name", "record", "Record directory",
    ))
    feature = Path(created["SPEC_FILE"]).parent.relative_to(workspace)
    relocated = tmp_path / "relocated workspace"
    workspace.rename(relocated)
    linked = run([*cli, "project", "link", str(relocated)], repo, env=script_env)
    assert linked.returncode == 0, linked.stderr
    paths = _success(_invoke(
        variant, relocated, repo, script_env, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(paths["FEATURE_DIR"]) == relocated / feature
    assert len(list((Path(script_env["XDG_DATA_HOME"]) / "specify/projects").glob("*.json"))) == 1
    assert not list(Path(script_env["LOCALAPPDATA"]).rglob("projects/*.json"))


@pytest.mark.parametrize("options", [
    None, [], [{}], [{}, {}], "", "sequential", 0, False,
    *({"feature_numbering": value} for value in (
        None, False, [], ["sequential"], {}, "invalid", "TIMESTAMP", "sequential\n",
    )),
])
def test_invalid_saved_numbering_preserves_files_and_selection(project, variant, script_env, options):
    """Invariant: invalid saved choices cannot create paths or change the selected feature."""
    repo, workspace, record = project
    _select(project, "existing-feature")
    _write_json(workspace / ".specify/init-options.json", options)
    roots = {repo, workspace}
    if record is not None:
        roots.add(record.parent)
    before = {root: _snapshot(root) for root in roots}

    result = _invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", "--short-name", "rejected", "Rejected numbering",
    )

    assert result.returncode != 0, result.stdout
    for root in roots:
        after = _snapshot(root)
        assert after.keys() == before[root].keys()
        for path, content in before[root].items():
            assert after[path] == content, path


@pytest.mark.parametrize("options,choice,expected", [
    ({}, None, "001"),
    ({"FEATURE_NUMBERING": "invalid"}, None, "001"),
    ({"feature_numbering": "sequential"}, None, "001"),
    ({"feature_numbering": "timestamp"}, None, "timestamp"),
    ({"feature_numbering": "invalid"}, "number", "000"),
])
def test_numbering_defaults_and_explicit_choices(project, variant, script_env, options, choice, expected):
    """Saved valid choices apply only when the caller supplies no explicit choice."""
    import re

    repo, workspace, _ = project
    _write_json(workspace / ".specify/init-options.json", options)
    arguments = {"number": ("--number", "0"), "timestamp": ("--timestamp",), None: ()}[choice]
    data = _success(_invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", *arguments, "--short-name", "choice", "Numbering choice",
    ))
    if expected == "timestamp":
        assert re.fullmatch(r"\d{8}-\d{6}-choice", data["BRANCH_NAME"])
    else:
        assert data["BRANCH_NAME"] == f"{expected}-choice"
    assert Path(data["SPEC_FILE"]).is_file()
    assert json.loads((workspace / ".specify/init-options.json").read_text(encoding="utf-8")) == options


def _selection_state(project):
    repo, _, record = project
    return record if record is not None else repo / ".specify/feature.json"


def _roots(tmp_path, project):
    repo, workspace, record = project
    return (repo, workspace) + ((tmp_path / "data",) if record is not None else ())


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
@pytest.mark.parametrize("policy", [None, "context"])
def test_context_policy_creation_keeps_the_saved_selection(
    tmp_path, variant, script_env, external, policy,
):
    """A missing policy means context: creating a feature never selects it."""
    project = _make_project(tmp_path, external, policy)
    repo, workspace, _ = project
    _select(project)
    state = _selection_state(project)
    before = state.read_bytes()

    created = _success(_invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", "--number", "5", "--short-name", "context-create", "Create in context",
    ))

    assert Path(created["SPEC_FILE"]) == workspace / "specs/005-context-create/spec.md"
    assert state.read_bytes() == before


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
def test_context_policy_creation_saves_no_selection(tmp_path, variant, script_env, external):
    project = _make_project(tmp_path, external, None)
    repo, workspace, record = project

    _success(_invoke(
        variant, workspace, repo, script_env, "create-new-feature",
        "--json", "--short-name", "unsaved", "Unsaved feature",
    ))

    if record is None:
        assert not (repo / ".specify/feature.json").exists()
    else:
        assert json.loads(record.read_text(encoding="utf-8"))["active_feature"] is None


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
@pytest.mark.parametrize("script", ["setup-plan", "setup-tasks", "check-prerequisites"])
def test_context_policy_ignores_the_saved_selection(
    tmp_path, variant, script_env, external, script,
):
    """Context scripts need a feature named for this command, never the saved one."""
    project = _make_project(tmp_path, external, "context")
    repo, workspace, _ = project
    _select(project)
    roots = _roots(tmp_path, project)
    before = [_snapshot(root) for root in roots]

    result = _invoke(variant, workspace, repo, script_env, script, "--json")

    assert result.returncode != 0
    assert result.stdout == ""
    assert "Feature directory not found" in result.stderr
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
def test_context_policy_explicit_features_never_change_the_saved_selection(
    tmp_path, variant, script_env, external,
):
    project = _make_project(tmp_path, external, "context")
    repo, workspace, _ = project
    saved = _select(project)
    state = _selection_state(project)
    before = state.read_bytes()
    for name in ("008-first", "009-second"):
        selected = workspace / "specs" / name
        selected.mkdir()
        script_env["SPECIFY_FEATURE_DIRECTORY"] = f"specs/{name}"
        for script, args in (
            ("setup-plan", ("--json",)),
            ("check-prerequisites", ("--json", "--paths-only")),
        ):
            paths = _success(_invoke(variant, workspace, repo, script_env, script, *args))
            assert Path(paths["FEATURE_DIR"]) == selected
            assert state.read_bytes() == before
        assert (selected / "plan.md").read_text(encoding="utf-8") == TEMPLATES["plan-template"]
    assert not (saved / "plan.md").exists()


INVALID_POLICY_OPTIONS = [
    [], "context", None,
    *({"feature_selection": value} for value in (
        "Context", "AUTOMATIC", "auto", "automatic\n", "", None, ["context"],
    )),
]


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
@pytest.mark.parametrize("script", ["create-new-feature", "setup-plan"])
@pytest.mark.parametrize("options", INVALID_POLICY_OPTIONS)
def test_invalid_policy_fails_before_any_write(
    tmp_path, variant, script_env, external, script, options,
):
    project = _make_project(tmp_path, external, None)
    repo, workspace, _ = project
    _select(project)
    _write_json(workspace / ".specify/init-options.json", options)
    args = ("--json",)
    if script == "create-new-feature":
        args = ("--json", "--short-name", "rejected", "Rejected policy")
    else:
        script_env["SPECIFY_FEATURE_DIRECTORY"] = "specs/008-explicit"
    roots = _roots(tmp_path, project)
    before = [_snapshot(root) for root in roots]

    result = _invoke(variant, workspace, repo, script_env, script, *args)

    assert result.returncode != 0, result.stdout
    assert "feature_selection" in result.stderr or "project choices" in result.stderr
    assert [_snapshot(root) for root in roots] == before


@pytest.mark.parametrize("options", [{"feature_selection": "context"}, {"feature_selection": "automatic"}, []])
def test_bash_policy_without_a_json_parser_refuses_without_writes(tmp_path, script_env, options):
    """A missing parser cannot turn unreadable policy into an implicit default."""
    repo, workspace, record = _make_project(tmp_path, False, None)
    _write_json(workspace / ".specify/init-options.json", options)
    before = _snapshot(repo)
    common = workspace / ".specify/scripts/bash/common.sh"
    result = subprocess.run(
        ["/bin/bash", "-c",
         'source "$1"; _python3_command() { return 1; }; PATH=/nonexistent; get_feature_selection_mode "$2"',
         "policy", str(common), str(workspace)],
        cwd=repo, env=script_env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Install jq or Python 3" in result.stderr
    assert _snapshot(repo) == before


@pytest.mark.parametrize("policy", ["automatic", "context"])
def test_bash_policy_reads_with_py_launcher_version_argument(tmp_path, script_env, policy):
    """`_python3_command` prints `py` and `-3` on separate lines; both must reach the launcher."""
    repo, workspace, _ = _make_project(tmp_path, False, policy)
    common = workspace / ".specify/scripts/bash/common.sh"
    result = subprocess.run(
        ["/bin/bash", "-c",
         'source "$1"; PY="$3"; jq() { return 1; }; _python3_command() { printf "%s\\n" py -3; }; '
         'py() { [ "$1" = "-3" ] || return 1; shift; "$PY" "$@"; }; get_feature_selection_mode "$2"',
         "policy", str(common), str(workspace), sys.executable],
        cwd=repo, env=script_env, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{policy}\n"


@pytest.mark.parametrize("policy", ["automatic", "context"])
@pytest.mark.parametrize("precreation_flag", [None, "1", "true"])
def test_external_missing_feature_requires_explicit_precreation_flag(
    tmp_path, variant, script_env, policy, precreation_flag,
):
    """Invariant: no selection mode grants permission to create a missing feature."""
    project = _make_project(tmp_path, True, policy)
    repo, workspace, _ = project
    script_env["SPECIFY_FEATURE_DIRECTORY"] = "specs/999-missing"
    before = [_snapshot(root) for root in _roots(tmp_path, project)]
    if precreation_flag is not None:
        script_env["SPECIFY_FEATURE_NO_PERSIST"] = precreation_flag
    result = _invoke(variant, workspace, repo, script_env, "setup-plan", "--json")
    if precreation_flag is not None:
        data = _success(result)
        assert Path(data["FEATURE_DIR"]) == workspace / "specs/999-missing"
        assert (workspace / "specs/999-missing/plan.md").is_file()
    else:
        assert result.returncode != 0
        assert "Selected feature directory does not exist" in result.stderr
        assert [_snapshot(root) for root in _roots(tmp_path, project)] == before


@pytest.mark.parametrize("script", ["setup-plan", "check-prerequisites"])
def test_missing_feature_override_keeps_the_saved_selection(tmp_path, variant, script_env, script):
    """A rejected override never replaces the saved feature with a missing directory."""
    project = _make_project(tmp_path, True)
    repo, workspace, _ = project
    _select(project)
    script_env["SPECIFY_FEATURE_DIRECTORY"] = "specs/999-missing"
    before = [_snapshot(root) for root in _roots(tmp_path, project)]
    result = _invoke(variant, workspace, repo, script_env, script, "--json")
    assert result.returncode != 0
    assert "999-missing" in result.stderr
    assert [_snapshot(root) for root in _roots(tmp_path, project)] == before


@pytest.mark.parametrize("target,contained", [
    ("../../shared/spec.md", True),
    ("../../shared/missing.md", True),
    ("../../../outside/spec.md", False),
], ids=["inside", "dangling-inside", "outside"])
def test_external_file_symlink_is_confined_by_its_target(
    tmp_path, variant, script_env, target, contained,
):
    project = _make_project(tmp_path, True)
    repo, workspace, _ = project
    feature = _select(project)
    for shared in (workspace / "shared/spec.md", tmp_path / "outside/spec.md"):
        shared.parent.mkdir()
        shared.write_text("# Shared specification\n", encoding="utf-8")
    (feature / "spec.md").unlink()
    try:
        (feature / "spec.md").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are not available")
    result = _invoke(
        variant, workspace, repo, script_env, "check-prerequisites", "--json", "--paths-only",
    )
    if contained:
        assert Path(_success(result)["FEATURE_SPEC"]) == feature / "spec.md"
    else:
        assert result.returncode != 0
        assert result.stdout == ""


@pytest.mark.parametrize("selection", ["cwd", "init-dir"])
def test_repository_with_project_record_may_also_hold_workspace_identity(
    tmp_path, variant, script_env, selection,
):
    """Only a workspace without a project record is refused as the code repository."""
    project = _make_project(tmp_path, True)
    repo, workspace, _ = project
    feature = _select(project)
    (repo / ".specify/workspace.json").write_bytes((workspace / ".specify/workspace.json").read_bytes())
    cwd = repo
    if selection == "init-dir":
        script_env["SPECIFY_INIT_DIR"] = str(repo)
        cwd = tmp_path
    data = _success(_invoke(
        variant, workspace, cwd, script_env, "check-prerequisites", "--json", "--paths-only",
    ))
    assert Path(data["REPO_ROOT"]) == repo
    assert Path(data["FEATURE_DIR"]) == feature


@pytest.mark.parametrize("file_value", ["../outside.md", "/nonexistent/plan-template.md", "..", "templates/.."])
def test_unsafe_preset_template_path_fails_only_in_external_storage(
    project, variant, script_env, file_value,
):
    """External workspaces refuse unsafe manifest paths. Local projects skip that layer."""
    if variant == "powershell" and not file_value.startswith(("/", "../")):
        pytest.skip("PowerShell rejects only '..' followed by a separator")
    repo, workspace, record = project
    install_composition_stack(workspace, "plan-template", "# Core plan\n")
    manifest = workspace / ".specify/presets/wrap-pack/preset.yml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "file: templates/plan-template.md", f"file: {json.dumps(file_value)}",
        ),
        encoding="utf-8",
    )
    result = _invoke(variant, workspace, repo, script_env, "resolve-template", "plan-template", "--json")
    if record is None:
        assert "## Wrapper" not in _success(result)["TEMPLATE_CONTENT"]
    else:
        assert result.returncode != 0
        assert "Invalid template path" in result.stderr
