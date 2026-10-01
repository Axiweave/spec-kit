"""Runtime commands use the invoking project's current workspace content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import pytest
from typer.testing import CliRunner
import yaml

from specify_cli import app
from specify_cli.extensions import ExtensionRegistry
from tests.conftest import install_preset


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".omp/agent"))
    for name in ("OMP_PROFILE", "PI_PROFILE", "PI_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)


def external_project(tmp_path, name, *, active_feature="specs/001-first", feature_selection="automatic"):
    repository = tmp_path / f"{name}-repo"
    workspace = tmp_path / f"{name} workspace"
    (repository / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    project_id = str(uuid4())
    (repository / ".specify/project.json").write_text(json.dumps({
        "schema_version": 1, "project_id": project_id, "storage": "external",
    }), encoding="utf-8")
    (workspace / ".specify/workspace.json").write_text(json.dumps({
        "schema_version": 1, "project_id": project_id,
    }), encoding="utf-8")
    (workspace / ".specify/init-options.json").write_text(json.dumps({
        "integration": "omp", "ai": "omp", "script": "sh",
        "command_scope": "global", "feature_numbering": "sequential",
        "feature_selection": feature_selection,
    }), encoding="utf-8")
    record = Path(os.environ["XDG_DATA_HOME"]) / "specify/projects" / f"{project_id}.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({
        "schema_version": 1, "workspace": str(workspace),
        "active_feature": active_feature,
    }), encoding="utf-8")
    if active_feature is not None:
        feature = workspace / active_feature
        feature.mkdir(parents=True)
        (feature / "spec.md").write_text(f"# {name} feature\n", encoding="utf-8")
    commands = workspace / ".specify/templates/commands"
    commands.mkdir(parents=True)
    (commands / "specify.md").write_text(f"Create a {name} feature from $ARGUMENTS.\n", encoding="utf-8")
    (commands / "plan.md").write_text("---\nscripts:\n  sh: scripts/bash/setup-plan.sh --json\n---\n"
    f"{name} core plan. Run {{SCRIPT}}.\n", encoding="utf-8")
    scripts = workspace / ".specify/scripts/bash"
    scripts.mkdir(parents=True)
    (scripts / "setup-plan.sh").write_text("#!/bin/sh\nprintf '{}\\n'\n", encoding="utf-8")
    return repository, workspace, record


def add_stack(workspace, name):
    preset = install_preset(workspace, "team", {"commands": [
        {"name": "speckit.plan", "strategy": "wrap"},
        {"name": "speckit.shared.inspect", "strategy": "append"},
    ]})
    (preset / "commands").mkdir()
    (preset / "commands/speckit.plan.md").write_text(f"{name} preset before.\n{{CORE_TEMPLATE}}\n{name} preset after.\n", encoding="utf-8")
    (preset / "commands/speckit.shared.inspect.md").write_text(f"{name} inspection policy.\n", encoding="utf-8")
    extension = workspace / ".specify/extensions/shared"
    (extension / "commands").mkdir(parents=True)
    (extension / "extension.yml").write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "extension": {
            "id": "shared", "name": "Shared inspection", "version": "1.0.0",
            "description": "Project inspection", "author": "test",
            "repository": "https://example.com/shared", "license": "MIT",
        },
        "requires": {"speckit_version": ">=0.2.0"},
        "provides": {"commands": [{
            "name": "speckit.shared.inspect", "file": "commands/inspect.md",
        }]},
    }), encoding="utf-8")
    (extension / "scripts").mkdir()
    (extension / "scripts/inspect.sh").write_text("#!/bin/sh\nprintf '{}\\n'\n", encoding="utf-8")
    (extension / "commands/inspect.md").write_text("---\nscripts:\n  sh: scripts/inspect.sh --json\n---\n"
    f"{name} extension inspection. Run {{SCRIPT}}.\n", encoding="utf-8")
    ExtensionRegistry(workspace / ".specify/extensions").add(
        "shared", {"version": "1.0.0", "enabled": True, "priority": 10},
    )
    return preset, extension


def command(name):
    result = CliRunner().invoke(app, ["project", "command", name, "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert isinstance(data["content"], str)
    return data


def files_under(*roots):
    return {
        str(path): path.read_bytes()
        for root in roots for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("name", ["speckit.plan", "speckit.shared.inspect"])
def test_same_command_uses_each_projects_composed_stack(tmp_path, monkeypatch, name):
    projects = {label: external_project(tmp_path, label) for label in ("Alpha", "Beta")}
    for label, (_, workspace, _) in projects.items():
        add_stack(workspace, label)
    before = files_under(tmp_path)

    for label, foreign in (("Alpha", "Beta"), ("Beta", "Alpha"), ("Alpha", "Beta")):
        repository, workspace, _ = projects[label]
        nested = repository / ".specify"
        monkeypatch.chdir(nested)
        data = command(name)
        assert data["repository_root"] == str(repository)
        assert data["workspace_root"] == str(workspace)
        content = data["content"]
        assert foreign not in content
        if name == "speckit.plan":
            assert content.index(f"{label} preset before") < content.index(f"{label} core plan")
            assert content.index(f"{label} core plan") < content.index(f"{label} preset after")
            assert str(workspace / ".specify/scripts/bash/setup-plan.sh") in content
            assert "{SCRIPT}" not in content
        else:
            assert content.index(f"{label} extension inspection") < content.index(f"{label} inspection policy")
            assert str(workspace / ".specify/extensions/shared/scripts/inspect.sh") in content
            assert "{SCRIPT}" not in content

    assert files_under(tmp_path) == before


def test_initial_specify_needs_no_selected_feature_and_does_not_create_one(tmp_path, monkeypatch):
    repository, workspace, record = external_project(tmp_path, "Initial", active_feature=None)
    monkeypatch.chdir(repository)
    before = files_under(tmp_path)

    data = command("speckit.specify")

    assert "Create a Initial feature" in data["content"]
    assert data["repository_root"] == str(repository)
    assert data["workspace_root"] == str(workspace)
    assert data["feature_dir"] is None
    assert json.loads(record.read_text(encoding="utf-8"))["active_feature"] is None
    assert not (workspace / "specs").exists()
    assert files_under(tmp_path) == before


@pytest.mark.parametrize("absolute", [False, True])
def test_explicit_feature_wins_without_changing_saved_selection(tmp_path, monkeypatch, absolute):
    repository, workspace, record = external_project(tmp_path, "Selected")
    second = workspace / "specs/002-second"
    second.mkdir()
    (second / "spec.md").write_text("# Second feature\n", encoding="utf-8")
    monkeypatch.chdir(repository)
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", str(second) if absolute else "specs/002-second")
    before = record.read_bytes()

    data = command("speckit.plan")

    assert data["feature_dir"] == str(second)
    assert record.read_bytes() == before
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY")
    assert command("speckit.plan")["feature_dir"] == str(workspace / "specs/001-first")


def test_context_command_ignores_saved_selection_and_preserves_all_state(tmp_path, monkeypatch):
    repository, workspace, record = external_project(
        tmp_path, "Context", feature_selection="context",
    )
    monkeypatch.chdir(repository)
    before = files_under(tmp_path)

    data = command("speckit.plan")

    assert data["feature_selection"] == "context"
    assert data["feature_dir"] is None
    assert json.loads(record.read_text(encoding="utf-8"))["active_feature"] == "specs/001-first"
    assert files_under(tmp_path) == before


@pytest.mark.parametrize("selection", ["context", "automatic"])
def test_explicit_command_context_does_not_leak_to_the_next_invocation(
    tmp_path, monkeypatch, selection,
):
    repository, workspace, record = external_project(
        tmp_path, "Invocation", feature_selection=selection,
    )
    second = workspace / "specs/002-second"
    second.mkdir()
    monkeypatch.chdir(repository)
    before = files_under(tmp_path)
    for relative in ("specs/002-second", "specs/001-first", "specs/002-second"):
        monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", relative)
        assert command("speckit.plan")["feature_dir"] == str(workspace / relative)
        assert files_under(tmp_path) == before
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY")
    expected = str(workspace / "specs/001-first") if selection == "automatic" else None
    assert command("speckit.plan")["feature_dir"] == expected
    assert record.read_bytes() == before[str(record)]


def test_concurrent_git_worktrees_resolve_features_without_shared_selection_writes(
    tmp_path, monkeypatch,
):
    """Invariant: concurrent worktrees retain independent invocation context and shared state bytes."""
    repository, workspace, record = external_project(
        tmp_path, "Worktrees", feature_selection="context",
    )
    second = workspace / "specs/002-second"
    second.mkdir()
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Worktree Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "worktree@example.test")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "add", ".specify/project.json"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Workspace locator"], cwd=repository, check=True, capture_output=True)
    worktrees = [tmp_path / name for name in ("first-worktree", "second-worktree")]
    for worktree in worktrees:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree)],
            cwd=repository, check=True, capture_output=True,
        )
    before = files_under(workspace, record.parent)

    def resolve(index):
        relative = ("specs/001-first", "specs/002-second")[index]
        env = {**os.environ, "SPECIFY_FEATURE_DIRECTORY": relative}
        result = subprocess.run(
            [sys.executable, "-c", "from specify_cli import main; main()",
             "project", "command", "speckit.plan", "--json"],
            cwd=worktrees[index], env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        data = json.loads(result.stdout)
        assert data["repository_root"] == str(worktrees[index])
        assert data["workspace_root"] == str(workspace)
        assert data["feature_dir"] == str(workspace / relative)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(resolve, [0, 1]))
    assert files_under(workspace, record.parent) == before



def test_agent_context_configuration_does_not_change_public_command_resolution(tmp_path, monkeypatch):
    """Invariant: extension-owned context configuration does not alter CLI command content."""
    repository, workspace, _ = external_project(tmp_path, "ContextOwner")
    plan = workspace / ".specify/templates/commands/plan.md"
    plan.write_text(plan.read_text(encoding="utf-8") + "\nRead __CONTEXT_FILE__.\n", encoding="utf-8")
    monkeypatch.chdir(repository)
    baseline = command("speckit.plan")
    assert "__CONTEXT_FILE__" in baseline["content"]
    config = workspace / ".specify/extensions/agent-context/agent-context-config.yml"
    config.parent.mkdir(parents=True)
    for filename in ("FIRST.md", "nested/SECOND.md"):
        config.write_text(yaml.safe_dump({
            "context_file": filename, "context_files": [filename],
        }), encoding="utf-8")
        before = files_under(tmp_path)
        assert command("speckit.plan") == baseline
        assert files_under(tmp_path) == before

def test_absent_command_does_not_use_another_projects_extension(tmp_path, monkeypatch):
    first, workspace, _ = external_project(tmp_path, "Owner")
    second, _, _ = external_project(tmp_path, "Other")
    add_stack(workspace, "Owner")
    monkeypatch.chdir(first)
    assert "Owner extension inspection" in command("speckit.shared.inspect")["content"]
    monkeypatch.chdir(second)
    before = files_under(tmp_path)

    result = CliRunner().invoke(app, ["project", "command", "speckit.shared.inspect", "--json"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert "speckit.shared.inspect" in result.stderr
    assert files_under(tmp_path) == before


@pytest.mark.parametrize("fault", ["missing-project", "missing-workspace", "missing-feature"])
def test_missing_context_fails_without_json_or_writes(tmp_path, monkeypatch, fault):
    repository, workspace, _ = external_project(tmp_path, "Broken", active_feature=None)
    if fault == "missing-project":
        monkeypatch.chdir(tmp_path)
    else:
        monkeypatch.chdir(repository)
        if fault == "missing-workspace":
            workspace.rename(tmp_path / "moved workspace")
    before = files_under(tmp_path)

    result = CliRunner().invoke(app, ["project", "command", "speckit.plan", "--json"])

    assert result.exit_code != 0
    assert result.stdout == ""
    assert fault.removeprefix("missing-") in result.stderr.lower()
    assert files_under(tmp_path) == before


def test_command_edits_are_visible_without_restarting_cli(tmp_path, monkeypatch):
    repository, workspace, _ = external_project(tmp_path, "Live")
    preset, extension = add_stack(workspace, "Live")
    monkeypatch.chdir(repository)
    assert "Live extension inspection" in command("speckit.shared.inspect")["content"]
    (extension / "commands/inspect.md").write_text("Updated inspection instructions.\n", encoding="utf-8")
    (preset / "commands/speckit.shared.inspect.md").write_text("Updated inspection policy.\n", encoding="utf-8")
    before = files_under(tmp_path)

    content = command("speckit.shared.inspect")["content"]

    assert "Updated inspection instructions." in content
    assert "Updated inspection policy." in content
    assert "Live extension inspection" not in content
    assert "Live inspection policy" not in content
    assert files_under(tmp_path) == before


def test_fresh_process_reads_saved_selection_and_latest_project_content(tmp_path, monkeypatch):
    repository, workspace, record = external_project(tmp_path, "Fresh")
    preset, _ = add_stack(workspace, "Fresh")
    monkeypatch.chdir(repository)
    assert "Fresh preset before" in command("speckit.plan")["content"]
    second = workspace / "specs/002-second"
    second.mkdir()
    (second / "spec.md").write_text("# Latest feature\n", encoding="utf-8")
    state = json.loads(record.read_text(encoding="utf-8"))
    state["active_feature"] = "specs/002-second"
    record.write_text(json.dumps(state), encoding="utf-8")
    (preset / "commands/speckit.plan.md").write_text("Latest plan context.\n{CORE_TEMPLATE}\n", encoding="utf-8")
    environment = os.environ.copy()
    source = str(Path(__file__).resolve().parents[3] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, environment.get("PYTHONPATH"))))
    before = files_under(tmp_path)

    result = subprocess.run(
        [sys.executable, "-c", "from specify_cli import app; app()",
         "project", "command", "speckit.plan", "--json"],
        cwd=repository, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["repository_root"] == str(repository)
    assert data["workspace_root"] == str(workspace)
    assert data["feature_dir"] == str(second)
    assert "Latest plan context." in data["content"]
    assert "Fresh core plan" in data["content"]
    assert "Fresh preset before" not in data["content"]
    assert str(workspace / ".specify/scripts/bash/setup-plan.sh") in data["content"]
    assert files_under(tmp_path) == before


@pytest.mark.parametrize("script_type", ["sh", "ps"])
@pytest.mark.parametrize("separator", [" ", "\t"])
@pytest.mark.parametrize("arguments", [[], ["--json"], ["two words", "", "O'Reilly", "資料"]])
def test_rendered_inline_command_preserves_arguments(
    tmp_path, monkeypatch, script_type, separator, arguments
):
    """Invariant: path relocation preserves the executable's argument vector."""
    from tests.parity_helpers import POWERSHELL_EXE

    executable = shutil.which("bash") if script_type == "sh" else POWERSHELL_EXE
    if executable is None or (script_type == "sh" and os.name == "nt"):
        pytest.skip("The native script interpreter is unavailable.")
    repository, workspace, _ = external_project(tmp_path, "Quoted '資料")
    options = workspace / ".specify/init-options.json"
    data = json.loads(options.read_text(encoding="utf-8"))
    data["script"] = script_type
    options.write_text(json.dumps(data), encoding="utf-8")
    script = workspace / (".specify/arguments.sh" if script_type == "sh" else ".specify/arguments.ps1")
    if script_type == "sh":
        script.write_text('#!/bin/sh\nprintf "%s\\n" "$#"\nfor value; do printf "%s\\n" "$value"; done\n', encoding="utf-8")
        argument_text = shlex.join(arguments)
    else:
        script.write_text(
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "$args.Count\n$args | ForEach-Object { Write-Output $_ }\n",
            encoding="utf-8",
        )
        argument_text = " ".join("'" + value.replace("'", "''") + "'" for value in arguments)
    script.chmod(0o755)
    source = script.relative_to(workspace).as_posix() + separator + argument_text
    (workspace / ".specify/templates/commands/specify.md").write_text(f"RUN: `{source}`\n", encoding="utf-8")
    monkeypatch.chdir(repository)

    rendered = command("speckit.specify")["content"]
    match = re.search(r"RUN: `([^`]+)`", rendered)
    assert match is not None
    flags = ["-c"] if script_type == "sh" else ["-NoProfile", "-NonInteractive", "-Command"]
    result = subprocess.run(
        [executable, *flags, match.group(1)],
        cwd=repository, capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{len(arguments)}\n" + "".join(f"{value}\n" for value in arguments)


@pytest.mark.parametrize("script_type", ["sh", "ps"])
@pytest.mark.parametrize("relative", [".specify/memory/team notes.md", ".specify/templates/資料 notes.md"])
def test_inline_resource_reference_preserves_spaced_filename(
    tmp_path, monkeypatch, script_type, relative
):
    """Invariant: a rendered resource reference still selects the same file bytes."""
    from tests.parity_helpers import POWERSHELL_EXE

    executable = shutil.which("bash") if script_type == "sh" else POWERSHELL_EXE
    if executable is None or (script_type == "sh" and os.name == "nt"):
        pytest.skip("The native script interpreter is unavailable.")
    repository, workspace, _ = external_project(tmp_path, "Resource '資料")
    options = workspace / ".specify/init-options.json"
    data = json.loads(options.read_text(encoding="utf-8"))
    data["script"] = script_type
    options.write_text(json.dumps(data), encoding="utf-8")
    resource = workspace / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("Project resource 資料\n", encoding="utf-8")
    (workspace / ".specify/templates/commands/specify.md").write_text(f"RESOURCE: `{relative}`\n", encoding="utf-8")
    monkeypatch.chdir(repository)

    rendered = command("speckit.specify")["content"]
    match = re.search(r"RESOURCE: `([^`]+)`", rendered)
    assert match is not None
    if script_type == "sh":
        arguments = ["-c", "cat " + match.group(1)]
    else:
        arguments = [
            "-NoProfile", "-NonInteractive", "-Command",
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
            "[Console]::Write([IO.File]::ReadAllText(" + match.group(1) + ", [Text.Encoding]::UTF8))",
        ]
    result = subprocess.run(
        [executable, *arguments], cwd=repository, capture_output=True, text=True, encoding="utf-8", check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Project resource 資料\n"


@pytest.mark.parametrize("script_type", ["sh", "ps"])
@pytest.mark.parametrize("filename", ["arguments.py", "argument list.py"])
@pytest.mark.parametrize("flags", ["", " -u"])
@pytest.mark.parametrize("arguments", [[], ["two words", "", "O'Reilly", "資料"]])
def test_interpreter_prefixed_command_preserves_arguments(
    tmp_path, monkeypatch, script_type, filename, flags, arguments,
):
    """Relocation changes only the script path, not interpreter options or script arguments."""
    from tests.parity_helpers import POWERSHELL_EXE

    shell = shutil.which("bash") if script_type == "sh" else POWERSHELL_EXE
    if shell is None or (script_type == "sh" and os.name == "nt"):
        pytest.skip("The native script interpreter is unavailable.")
    repository, workspace, _ = external_project(tmp_path, "Interpreter '資料")
    options = workspace / ".specify/init-options.json"
    saved = json.loads(options.read_text(encoding="utf-8"))
    saved["script"] = script_type
    options.write_text(json.dumps(saved), encoding="utf-8")
    script = workspace / ".specify" / filename
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    relative = script.relative_to(workspace).as_posix()
    def quote(value):
        return "'" + value.replace("'", "''") + "'" if script_type == "ps" else shlex.quote(value)

    script_token = quote(relative) if " " in relative else relative
    invocation = f"python{flags} {script_token} " + " ".join(quote(value) for value in arguments)
    prose = "Unrelated prose: python .specify/not-a-script.py"
    template = workspace / ".specify/templates/commands/specify.md"
    template.write_text(f"{prose}\nRUN: `{invocation}`\n", encoding="utf-8")
    monkeypatch.chdir(repository)

    rendered = command("speckit.specify")["content"]

    assert prose in rendered
    invocation = re.search(r"RUN: `([^`]+)`", rendered).group(1)
    assert invocation.startswith(f"python{flags} ")
    shell_flags = ["-c"] if script_type == "sh" else ["-NoProfile", "-NonInteractive", "-Command"]
    environment = os.environ | {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        [shell, *shell_flags, invocation], cwd=repository, env=environment,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments


def test_public_preset_update_changes_runtime_commands_without_rewriting_shared_launchers(tmp_path, monkeypatch):
    """Preset replacement changes one project's runtime stack and preserves shared ownership."""
    from specify_cli.presets import PresetManager
    from specify_cli.workspace import user_data_dir

    sources = []
    for version, extra in (("1.0.0", "legacy"), ("2.0.0", "current")):
        names = ("speckit.team.hello", f"speckit.team.{extra}")
        source = install_preset(tmp_path / version, "team", {
            "commands": [{"name": name} for name in names],
        })
        manifest_path = source / "preset.yml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["preset"]["version"] = version
        manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        (source / "commands").mkdir()
        for name in names:
            (source / f"commands/{name}.md").write_text(
                f"---\ndescription: Versioned preset command\n---\nRelease {version}: {name}.\n",
                encoding="utf-8",
            )
        sources.append(source)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    installed = runner.invoke(app, ["integration", "install", "omp", "--global"])
    assert installed.exit_code == 0, installed.output
    first, workspace, _ = external_project(tmp_path, "Updated")
    second, other_workspace, _ = external_project(tmp_path, "Untouched")
    for repository in (first, second):
        monkeypatch.chdir(repository)
        added = runner.invoke(app, ["preset", "add", "team", "--dev", str(sources[0])])
        assert added.exit_code == 0, added.output
        assert "Release 1.0.0" in command("speckit.team.hello")["content"]

    shared = Path(os.environ["PI_CODING_AGENT_DIR"]) / "commands"
    (shared / "speckit.plan.md").write_text("Keep my global plan edits.\n", encoding="utf-8")
    (shared / "personal.md").write_text("Keep my personal command.\n", encoding="utf-8")
    shared_before = files_under(shared)
    ownership_path = next((user_data_dir() / "integrations/omp").glob("*.json"))
    ownership_before = json.loads(ownership_path.read_text(encoding="utf-8"))["files"]
    other_before = files_under(second, other_workspace)
    monkeypatch.chdir(first)

    updated = runner.invoke(app, ["preset", "update", "team", "--dev", str(sources[1])])

    assert updated.exit_code == 0, updated.output
    assert PresetManager(first).registry.get("team")["version"] == "2.0.0"
    assert "Release 2.0.0" in command("speckit.team.hello")["content"]
    assert "Release 2.0.0" in command("speckit.team.current")["content"]
    assert runner.invoke(app, ["project", "command", "speckit.team.legacy"]).exit_code == 1
    assert (shared / "speckit.team.current.md").is_file()
    assert {path: Path(path).read_bytes() for path in shared_before} == shared_before
    ownership_after = json.loads(ownership_path.read_text(encoding="utf-8"))["files"]
    assert {name: ownership_after[name] for name in ownership_before} == ownership_before
    assert ownership_after.keys() - ownership_before.keys() == {"speckit.team.current.md"}
    assert "personal.md" not in ownership_after

    environment = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[3] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, environment.get("PYTHONPATH"))))
    fresh = subprocess.run(
        [sys.executable, "-c", "from specify_cli import app; app()",
         "project", "command", "speckit.team.hello", "--json"],
        cwd=first, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert fresh.returncode == 0, fresh.stderr
    data = json.loads(fresh.stdout)
    assert "Release 2.0.0" in data["content"]
    assert "Release 1.0.0" not in data["content"]
    assert data["workspace_root"] == str(workspace)
    assert data["feature_dir"] == str(workspace / "specs/001-first")
    monkeypatch.chdir(second)
    assert "Release 1.0.0" in command("speckit.team.hello")["content"]
    assert "Release 1.0.0" in command("speckit.team.legacy")["content"]
    assert runner.invoke(app, ["project", "command", "speckit.team.current"]).exit_code == 1
    assert files_under(second, other_workspace) == other_before
    assert not (first / ".omp/commands").exists()
    assert not (workspace / ".omp/commands").exists()
