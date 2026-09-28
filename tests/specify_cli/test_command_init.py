"""Tests for the ``specify init`` command adapter.

`command_init.py` interpolated the project name, `--integration`/`--script`
values and paths straight into Rich markup f-strings. A name containing a
tag-shaped bracket run was therefore consumed as markup:

* ``specify init "proj [v2]"`` succeeded and created the directory, but the
  Next Steps panel printed ``cd proj`` -- a command that fails when pasted.
* ``specify init "app[/red]x"`` created the directory and then died with
  ``MarkupError``, so the user saw a traceback for a project that had in fact
  been scaffolded.

Every sibling CLI module (extensions, presets, workflows, integrations) already
escapes user-controlled display values; init.py was the outlier.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.command_init import _shell_quote_arg
from specify_cli.workspace import resolve_project

from tests.conftest import requires_bash

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def isolated_init_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep setup defaults and machine records outside the real user home."""
    home = tmp_path / "home"
    home.mkdir()
    for variable, directory in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_DATA_HOME": home / ".local" / "share",
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
    }.items():
        monkeypatch.setenv(variable, str(directory))
    for variable in (
        "SPECKIT_INTEGRATION_DEFAULT",
        "OMP_PROFILE",
        "PI_PROFILE",
        "PI_CONFIG_DIR",
        "PI_CODING_AGENT_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("COLUMNS", "240")
    return home


def test_command_init_importable():
    mod = importlib.import_module("specify_cli.command_init")
    assert hasattr(mod, "register")
    assert callable(mod.register)


def test_transitional_commands_package_removed():
    import specify_cli

    package_root = Path(specify_cli.__file__).parent
    assert not (package_root / "commands" / "__init__.py").exists()
    assert not (package_root / "commands" / "init.py").exists()


def test_init_command_registered():
    callback_names = [
        cmd.callback.__name__ for cmd in app.registered_commands if cmd.callback
    ]
    assert callback_names == ["init", "check", "version"]




def _strip(text: str) -> str:
    return _ANSI.sub("", text or "")


def _init(
    tmp_path: Path,
    name: str,
    *options: str,
    integration: str | None = "generic",
    interactive: bool = False,
):
    """Run offline init with optional defaults and interactive choice input."""
    integration_options = []
    if integration is not None:
        integration_options = ["--integration", integration]
        if integration == "generic":
            integration_options += [
                "--integration-options", "--commands-dir .agent/commands"
            ]
    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        return CliRunner().invoke(
            app,
            [
                "init",
                name,
                *integration_options,
                "--ignore-agent-tools",
                "--offline",
                *([] if interactive else ["--non-interactive"]),
                *options,
            ],
            catch_exceptions=True,
        )
    finally:
        os.chdir(previous)


@pytest.mark.parametrize("name", ["proj [v2]", "my[bold]app"])
def test_next_steps_cd_shows_the_real_project_name(tmp_path: Path, name: str):
    """The `cd` line must name the directory that was actually created."""
    result = _init(tmp_path, name)
    assert result.exit_code == 0, _strip(result.stdout)
    assert (tmp_path / name).is_dir()

    out = _strip(result.stdout)
    cd_lines = [line for line in out.splitlines() if "cd " in line]
    assert cd_lines, out
    assert f"cd {_shell_quote_arg(name)}" in " ".join(cd_lines), cd_lines


def test_closing_tag_in_project_name_does_not_crash(tmp_path: Path):
    """A name forming a closing tag raised MarkupError *after* the project had
    been created, so init reported failure for work it had completed."""
    name = "app[/red]x"
    result = _init(tmp_path, name)

    assert result.exception is None or not isinstance(
        result.exception, Exception
    ) or "MarkupError" not in type(result.exception).__name__, (
        f"unexpected {type(result.exception).__name__}: {result.exception}"
    )
    assert result.exit_code == 0, _strip(result.stdout)
    assert (tmp_path / name).is_dir()
    assert f"cd {_shell_quote_arg(name)}" in _strip(result.stdout)


def test_invalid_integration_value_is_rendered_literally(tmp_path: Path):
    """An invalid `--integration` value is echoed back; it must not be parsed as
    markup (nor raise) when it contains a bracket run."""
    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(
            app,
            ["init", "proj", "--integration", "nope[/red]", "--ignore-agent-tools"],
            catch_exceptions=True,
        )
    finally:
        os.chdir(previous)

    assert result.exit_code != 0
    assert "nope[/red]" in _strip(result.stdout)


def _cd_argument(stdout: str) -> str:
    """Return the argument of the printed `cd` command, verbatim.

    The line is rendered inside a Rich panel, so the trailing box-drawing
    border and its padding are stripped before the argument is compared.
    """
    marker = "Go to the project folder: cd "
    for line in _strip(stdout).splitlines():
        if marker in line:
            return line.split(marker, 1)[1].rstrip().rstrip("│").rstrip()
    raise AssertionError(f"no cd line in output:\n{stdout}")


@pytest.mark.parametrize("name", ["proj v2", "my project"])
def test_cd_line_quotes_a_name_containing_whitespace(tmp_path: Path, name: str):
    """Rich-escaping alone left `cd proj v2`, which every shell reads as two
    arguments, so the copy-pasted command did not enter the directory."""
    result = _init(tmp_path, name)
    assert result.exit_code == 0, _strip(result.stdout)
    assert (tmp_path / name).is_dir()

    printed = _cd_argument(result.stdout)
    assert printed != name, "a whitespace-bearing name must be quoted"
    assert name in printed, printed
    assert printed == _shell_quote_arg(name)


def test_ordinary_name_is_not_quoted(tmp_path: Path):
    """The common case must stay byte-identical: no gratuitous quoting."""
    result = _init(tmp_path, "my-project")
    assert result.exit_code == 0, _strip(result.stdout)
    assert _cd_argument(result.stdout) == "my-project"


@requires_bash
@pytest.mark.parametrize("name", ["proj v2", "proj [v2]", "my-project"])
def test_printed_cd_command_actually_changes_directory(tmp_path: Path, name: str):
    """Execute the printed command rather than only inspecting it.

    This is the assertion the string comparisons cannot make: the rendered
    `cd <arg>` is fed to a real shell and must land in the created directory.
    """
    result = _init(tmp_path, name)
    assert result.exit_code == 0, _strip(result.stdout)
    target = tmp_path / name
    assert target.is_dir()

    printed = _cd_argument(result.stdout)
    proc = subprocess.run(
        ["bash", "-c", f"cd {printed} && pwd"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"cd {printed!r} failed: {proc.stderr}"
    assert Path(proc.stdout.strip()).name == name, proc.stdout


def test_shell_quote_arg_is_host_appropriate():
    """The helper follows `_version._render_argv`: list2cmdline on Windows,
    shlex.quote elsewhere. Names needing no quoting round-trip unchanged."""
    assert _shell_quote_arg("my-project") == "my-project"
    quoted = _shell_quote_arg("my project")
    assert quoted != "my project"
    if os.name == "nt":
        assert quoted == '"my project"'
    else:
        assert quoted == "'my project'"


def test_external_init_separates_same_name_projects(
    tmp_path: Path, isolated_init_home: Path
):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    first = _init(left, "same-name", "--storage", "external")
    assert first.exit_code == 0, _strip(first.output)
    first_project = resolve_project(left / "same-name")
    constitution = first_project.workspace_root / ".specify/memory/constitution.md"
    constitution.write_text("First project's constitution\n", encoding="utf-8")

    second = _init(right, "same-name", "--storage", "external")
    assert second.exit_code == 0, _strip(second.output)
    second_project = resolve_project(right / "same-name")

    assert first_project.project_id != second_project.project_id
    assert first_project.workspace_root != second_project.workspace_root
    storage_root = (isolated_init_home / "speckit-specs").resolve()
    assert first_project.workspace_root.parent == storage_root
    assert second_project.workspace_root.parent == storage_root
    assert first_project.repository_root == (left / "same-name").resolve()
    assert second_project.repository_root == (right / "same-name").resolve()
    assert constitution.read_text(encoding="utf-8") == "First project's constitution\n"
    assert resolve_project(left / "same-name") == first_project


@pytest.mark.parametrize("storage_options", [(), ("--storage", "external")], ids=["implicit", "explicit"])
def test_external_init_uses_exact_workspace_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage_options: tuple[str, ...]
):
    def refuse_network(*args, **kwargs):
        raise OSError("The network is unavailable.")

    monkeypatch.setattr(socket.socket, "connect", refuse_network)
    monkeypatch.setattr(socket, "create_connection", refuse_network)
    workspace = tmp_path / "external work" / "chosen"
    result = _init(
        tmp_path, "project", *storage_options, "--workspace", str(workspace), "--script", "py"
    )
    assert result.exit_code == 0, _strip(result.output)
    repository = tmp_path / "project"
    project = resolve_project(repository)
    assert project.repository_root == repository.resolve()
    assert project.workspace_root == workspace.resolve()
    assert project.storage == "external"
    assert project.feature_dir is None
    assert str(workspace) in _strip(result.output)

    locator = json.loads((repository / ".specify/project.json").read_text(encoding="utf-8"))
    assert locator == {
        "schema_version": 1,
        "project_id": project.project_id,
        "storage": "external",
    }
    identity = json.loads((workspace / ".specify/workspace.json").read_text(encoding="utf-8"))
    assert identity["project_id"] == project.project_id
    assert not (workspace / ".specify/project.json").exists()
    assert {path.name for path in (repository / ".specify").iterdir()} == {"project.json"}
    assert {path.name for path in repository.iterdir()} <= {".specify", ".agent", ".gitignore"}
    assert not (repository / "specs").exists()
    for command in ("specify", "plan", "tasks"):
        assert (repository / ".agent/commands" / f"speckit.{command}.md").is_file()
    for relative in (
        ".specify/memory/constitution.md",
        ".specify/templates/spec-template.md",
        ".specify/templates/plan-template.md",
        ".specify/templates/tasks-template.md",
        ".specify/scripts/python/create_new_feature.py",
        ".specify/scripts/python/setup_plan.py",
        ".specify/scripts/python/setup_tasks.py",
        ".specify/workflows/speckit/workflow.yml",
    ):
        assert (workspace / relative).is_file(), relative
    options = json.loads((workspace / ".specify/init-options.json").read_text(encoding="utf-8"))
    assert options["integration"] == "generic"
    assert options["script"] == "py"
    assert options["command_scope"] == "project"


def test_init_explicit_local_keeps_assets_in_repository(
    tmp_path: Path, isolated_init_home: Path
):
    result = _init(tmp_path, "local-project", "--storage", "local", "--script", "py")
    assert result.exit_code == 0, _strip(result.output)
    repository = tmp_path / "local-project"
    project = resolve_project(repository)
    assert project.storage == "local"
    assert project.workspace_root == project.repository_root == repository.resolve()
    assert (repository / ".specify/memory/constitution.md").is_file()
    assert (repository / ".specify/templates/spec-template.md").is_file()
    assert (repository / ".specify/scripts/python/create_new_feature.py").is_file()
    assert (repository / ".agent/commands/speckit.specify.md").is_file()
    assert not (repository / ".specify/project.json").exists()
    assert not (isolated_init_home / "speckit-specs").exists()
    assert not list(isolated_init_home.rglob("projects/*.json"))


@pytest.mark.parametrize("occupied", ["file", "unrelated-directory", "foreign-workspace"])
def test_external_init_preserves_occupied_workspace(tmp_path: Path, occupied: str):
    workspace = tmp_path / "claimed-workspace"
    if occupied == "file":
        workspace.write_bytes(b"Unrelated file\n")
        before = workspace.read_bytes()
    else:
        workspace.mkdir()
        (workspace / "notes.txt").write_bytes(b"Unrelated notes\n")
        if occupied == "foreign-workspace":
            (workspace / ".specify").mkdir()
            (workspace / ".specify/workspace.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "project_id": "550e8400-e29b-41d4-a716-446655440000",
                }),
                encoding="utf-8",
            )
        before = {
            path.relative_to(workspace): path.read_bytes() if path.is_file() else None
            for path in workspace.rglob("*")
        }
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "source.txt").write_bytes(b"Existing project source\n")

    result = _init(repository, ".", "--force", "--workspace", str(workspace))

    assert result.exit_code != 0
    assert workspace.name in _strip(result.output)
    assert {path.name for path in repository.iterdir()} == {"source.txt"}
    assert (repository / "source.txt").read_bytes() == b"Existing project source\n"
    if occupied == "file":
        assert workspace.read_bytes() == before
    else:
        assert {
            path.relative_to(workspace): path.read_bytes() if path.is_file() else None
            for path in workspace.rglob("*")
        } == before


def test_external_init_rejects_unavailable_storage_without_local_fallback(tmp_path: Path):
    blocked_root = tmp_path / "blocked-root"
    blocked_root.write_bytes(b"This file cannot contain a workspace.\n")
    workspace = blocked_root / "unavailable-workspace"
    result = _init(tmp_path, "project", "--workspace", str(workspace))

    assert result.exit_code != 0
    assert workspace.name in _strip(result.output)
    assert blocked_root.read_bytes() == b"This file cannot contain a workspace.\n"
    assert not (tmp_path / "project").exists()


def test_init_rejects_local_storage_with_workspace_before_changes(tmp_path: Path):
    local = _init(tmp_path, "local-project", "--storage", "local")
    assert local.exit_code == 0, _strip(local.output)
    repository = tmp_path / "local-project"
    before = {
        path.relative_to(repository): path.read_bytes() if path.is_file() else None
        for path in repository.rglob("*")
    }
    workspace = tmp_path / "conflicting-workspace"
    result = _init(
        repository, ".", "--force", "--storage", "local", "--workspace", str(workspace)
    )

    assert result.exit_code != 0
    assert "--storage" in _strip(result.output)
    assert "--workspace" in _strip(result.output)
    assert not workspace.exists()
    assert {
        path.relative_to(repository): path.read_bytes() if path.is_file() else None
        for path in repository.rglob("*")
    } == before


def test_external_init_canonicalizes_explicit_workspace_ancestor(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    result = _init(tmp_path, "project", "--workspace", str(alias / "workspace"))
    assert result.exit_code == 0, _strip(result.output)
    project = resolve_project(tmp_path / "project")
    assert project.workspace_root == real / "workspace"
    assert (real / "workspace/.specify/workspace.json").is_file()
    assert {p.name for p in (tmp_path / "project/.specify").iterdir()} == {"project.json"}


@pytest.fixture
def saved_init_defaults(isolated_init_home: Path):
    config_home = Path(os.environ["APPDATA"] if os.name == "nt" else os.environ["XDG_CONFIG_HOME"])
    config = config_home / "specify/config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({
            "storage_root": "~/saved-workspaces",
            "feature_numbering": "timestamp",
            "integration": "omp",
            "script": "py",
        }),
        encoding="utf-8",
    )
    return config


def _init_choices(workspace: Path):
    options = json.loads((workspace / ".specify/init-options.json").read_text(encoding="utf-8"))
    return {key: options[key] for key in ("feature_numbering", "integration", "script")}


@pytest.mark.parametrize("interactive", [False, True], ids=["noninteractive", "interactive"])
def test_saved_defaults_create_isolated_same_name_projects(
    tmp_path: Path,
    isolated_init_home: Path,
    saved_init_defaults: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
):
    def refuse_prompt(*args, **kwargs):
        pytest.fail("Saved nonempty choices must not prompt.")

    monkeypatch.setattr("specify_cli.command_init._prompts_allowed", lambda non_interactive: not non_interactive)
    monkeypatch.setattr("specify_cli.command_init.select_with_arrows", refuse_prompt)
    monkeypatch.setattr("specify_cli.command_init.typer.prompt", refuse_prompt)
    config_before = saved_init_defaults.read_bytes()
    projects = []
    for name in ("left", "right"):
        parent = tmp_path / name
        parent.mkdir()
        result = _init(parent, "same-name", integration=None, interactive=interactive)
        assert result.exit_code == 0, _strip(result.output)
        project = resolve_project(parent / "same-name")
        assert project.storage == "external"
        assert project.repository_root == (parent / "same-name").resolve()
        assert project.workspace_root.parent == (isolated_init_home / "saved-workspaces").resolve()
        assert _init_choices(project.workspace_root) == {
            "feature_numbering": "timestamp", "integration": "omp", "script": "py",
        }
        locator = json.loads((project.repository_root / ".specify/project.json").read_text(encoding="utf-8"))
        identity = json.loads((project.workspace_root / ".specify/workspace.json").read_text(encoding="utf-8"))
        assert locator["project_id"] == identity["project_id"] == project.project_id
        assert {path.name for path in (project.repository_root / ".specify").iterdir()} == {"project.json"}
        assert not (project.workspace_root / ".specify/project.json").exists()
        (project.workspace_root / ".specify/memory/constitution.md").write_text(
            f"{name} project constitution\n", encoding="utf-8"
        )
        projects.append(project)

    first, second = projects
    assert first.project_id != second.project_id
    assert first.workspace_root != second.workspace_root
    assert (first.workspace_root / ".specify/memory/constitution.md").read_text(encoding="utf-8") == "left project constitution\n"
    assert (second.workspace_root / ".specify/memory/constitution.md").read_text(encoding="utf-8") == "right project constitution\n"
    assert saved_init_defaults.read_bytes() == config_before


@pytest.mark.parametrize(
    ("option", "value", "changed_choice"),
    [
        ("--storage", "local", None),
        ("--workspace", "exact-workspace", None),
        ("--feature-numbering", "sequential", "feature_numbering"),
        ("--integration", "copilot", "integration"),
        ("--script", "sh", "script"),
    ],
)
def test_explicit_init_choice_overrides_only_its_saved_default(
    tmp_path: Path,
    isolated_init_home: Path,
    saved_init_defaults: Path,
    option: str,
    value: str,
    changed_choice: str | None,
):
    config_before = saved_init_defaults.read_bytes()
    if option == "--workspace":
        value = str(tmp_path / value)
    result = _init(tmp_path, "project", option, value, integration=None)
    assert result.exit_code == 0, _strip(result.output)
    project = resolve_project(tmp_path / "project")
    expected = {"feature_numbering": "timestamp", "integration": "omp", "script": "py"}
    if changed_choice:
        expected[changed_choice] = value
    assert _init_choices(project.workspace_root) == expected
    if option == "--storage":
        assert project.storage == "local"
        assert project.workspace_root == project.repository_root == (tmp_path / "project").resolve()
        assert not (project.repository_root / ".specify/project.json").exists()
        assert not (isolated_init_home / "saved-workspaces").exists()
    else:
        assert project.storage == "external"
        assert project.workspace_root != project.repository_root
        if option == "--workspace":
            assert project.workspace_root == Path(value).resolve()
            assert not (isolated_init_home / "saved-workspaces").exists()
        else:
            assert project.workspace_root.parent == (isolated_init_home / "saved-workspaces").resolve()
        assert not (project.repository_root / ".specify/init-options.json").exists()
    assert saved_init_defaults.read_bytes() == config_before


@pytest.mark.parametrize("explicit", [False, True], ids=["environment", "explicit"])
def test_integration_environment_overrides_defaults_but_not_explicit_choice(
    tmp_path: Path,
    isolated_init_home: Path,
    saved_init_defaults: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
):
    config_before = saved_init_defaults.read_bytes()
    monkeypatch.setenv("SPECKIT_INTEGRATION_DEFAULT", "copilot")
    options = ("--integration", "claude") if explicit else ()
    result = _init(tmp_path, "project", *options, integration=None)
    assert result.exit_code == 0, _strip(result.output)
    project = resolve_project(tmp_path / "project")
    assert project.workspace_root.parent == (isolated_init_home / "saved-workspaces").resolve()
    assert _init_choices(project.workspace_root) == {
        "feature_numbering": "timestamp",
        "integration": "claude" if explicit else "copilot",
        "script": "py",
    }
    assert saved_init_defaults.read_bytes() == config_before


@pytest.mark.parametrize("empty", [False, True], ids=["absent", "empty"])
@pytest.mark.parametrize("choice", ["storage_root", "feature_numbering", "integration", "script"])
def test_each_missing_init_choice_accepts_interactive_input_independently(
    tmp_path: Path,
    isolated_init_home: Path,
    saved_init_defaults: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
    empty: bool,
):
    defaults = json.loads(saved_init_defaults.read_text(encoding="utf-8"))
    if empty:
        defaults[choice] = ""
    else:
        del defaults[choice]
    saved_init_defaults.write_text(json.dumps(defaults), encoding="utf-8")
    config_before = saved_init_defaults.read_bytes()
    selected = {
        "storage_root": "external",
        "feature_numbering": "sequential",
        "integration": "copilot",
        "script": "sh",
    }[choice]
    workspace = tmp_path / "interactive-workspace"

    def choose(choices, *args, **kwargs):
        if choice == "storage_root":
            assert set(choices) == {"local", "external"}
        elif choice == "feature_numbering":
            assert set(choices) == {"sequential", "timestamp"}
        elif choice == "script":
            assert set(choices) == {"sh", "ps", "py"}
        else:
            assert "copilot" in choices and "omp" in choices
        return selected

    def enter_workspace(*args, **kwargs):
        assert choice == "storage_root", "A saved storage root must not prompt."
        return str(workspace)

    monkeypatch.setattr("specify_cli.command_init._prompts_allowed", lambda non_interactive: not non_interactive)
    monkeypatch.setattr("specify_cli.command_init.select_with_arrows", choose)
    monkeypatch.setattr("specify_cli.command_init.typer.prompt", enter_workspace)
    result = _init(tmp_path, "project", integration=None, interactive=True)
    assert result.exit_code == 0, _strip(result.output)
    project = resolve_project(tmp_path / "project")
    assert project.storage == "external"
    expected = {"feature_numbering": "timestamp", "integration": "omp", "script": "py"}
    if choice == "storage_root":
        assert project.workspace_root == workspace.resolve()
    else:
        expected[choice] = selected
        assert project.workspace_root.parent == (isolated_init_home / "saved-workspaces").resolve()
    assert _init_choices(project.workspace_root) == expected
    assert saved_init_defaults.read_bytes() == config_before


@pytest.mark.parametrize("defaults", [None, {}, {
    "storage_root": "", "feature_numbering": "", "integration": "", "script": "",
}], ids=["missing-file", "absent-keys", "empty-values"])
def test_noninteractive_init_uses_fallbacks_without_personal_choices(
    tmp_path: Path,
    saved_init_defaults: Path,
    monkeypatch: pytest.MonkeyPatch,
    defaults: dict | None,
):
    if defaults is None:
        saved_init_defaults.unlink()
    else:
        saved_init_defaults.write_text(json.dumps(defaults), encoding="utf-8")

    def refuse_prompt(*args, **kwargs):
        pytest.fail("Non-interactive setup must not prompt.")

    monkeypatch.setattr("specify_cli.command_init.select_with_arrows", refuse_prompt)
    monkeypatch.setattr("specify_cli.command_init.typer.prompt", refuse_prompt)
    result = _init(tmp_path, "project", integration=None)
    assert result.exit_code == 0, _strip(result.output)
    project = resolve_project(tmp_path / "project")
    assert project.storage == "local"
    assert project.workspace_root == project.repository_root == (tmp_path / "project").resolve()
    assert _init_choices(project.workspace_root) == {
        "feature_numbering": "sequential",
        "integration": "copilot",
        "script": "ps" if os.name == "nt" else "sh",
    }
    assert not (project.repository_root / ".specify/project.json").exists()


@pytest.mark.parametrize("storage", ["local", "external"])
def test_changed_defaults_do_not_change_existing_project_choices(
    tmp_path: Path,
    saved_init_defaults: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage: str,
):
    result = _init(tmp_path, "project", "--storage", storage, integration=None)
    assert result.exit_code == 0, _strip(result.output)
    repository = tmp_path / "project"
    project = resolve_project(repository)
    choices = _init_choices(project.workspace_root)
    new_storage = tmp_path / "new-default-workspaces"
    saved_init_defaults.write_text(json.dumps({
        "storage_root": str(new_storage),
        "feature_numbering": "sequential",
        "integration": "copilot",
        "script": "sh",
    }), encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    monkeypatch.chdir(repository)
    info = CliRunner().invoke(app, ["project", "info", "--json"])
    assert info.exit_code == 0, _strip(info.output)
    reported = json.loads(info.stdout)
    assert reported["storage"] == storage
    assert reported["workspace_root"] == str(project.workspace_root)
    assert reported["repository_root"] == str(project.repository_root)
    assert reported["project_id"] == project.project_id
    assert {key: reported[key] for key in choices} == choices
    assert {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    } == before

    refreshed = _init(repository, ".", "--force", integration=None)
    assert refreshed.exit_code == 0, _strip(refreshed.output)
    assert resolve_project(repository) == project
    assert _init_choices(project.workspace_root) == choices
    assert not new_storage.exists()


@pytest.mark.parametrize("invalid", [
    "{",
    "[]",
    '{"feature_numbering": "random"}',
    '{"integration": "not-an-integration"}',
    '{"script": "ruby"}',
    '{"storage_root": 42}',
    '{"storage_root": "\\u0000invalid"}',
])
def test_invalid_personal_defaults_fail_before_any_file_changes(
    tmp_path: Path, saved_init_defaults: Path, invalid: str
):
    saved_init_defaults.write_text(invalid, encoding="utf-8")
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "source.txt").write_bytes(b"Existing project source\n")
    before = {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    result = _init(repository, ".", "--force", integration=None)
    assert result.exit_code != 0
    assert {
        path.relative_to(tmp_path): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    } == before


@pytest.mark.parametrize("condition", ["occupied", "contended", "interrupted"])
def test_storage_claim_preserves_unrelated_content(tmp_path: Path, condition: str):
    """A failed claim cannot change unrelated bytes or another project's ownership."""
    storage = importlib.import_module("specify_cli._command_init_storage")
    workspace = tmp_path / "workspace"
    project = storage.select_storage(tmp_path / "first", "external", workspace)
    if condition == "occupied":
        workspace.mkdir()
        notes = workspace / "notes.txt"
        notes.write_bytes(b"Unrelated content\n")
        with pytest.raises((OSError, ValueError)):
            with storage.claim_storage(project):
                pytest.fail("An occupied destination must not be claimed")
        assert notes.read_bytes() == b"Unrelated content\n"
    elif condition == "contended":
        other = storage.select_storage(tmp_path / "second", "external", workspace)
        with storage.claim_storage(other):
            before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
            with pytest.raises((OSError, ValueError)):
                with storage.claim_storage(project):
                    pytest.fail("A second claim must not replace the first owner")
            assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
        assert resolve_project(other.repository_root) == other
    else:
        notes = workspace / "notes.txt"
        with pytest.raises(RuntimeError, match="Interrupted setup"):
            with storage.claim_storage(project):
                notes.write_bytes(b"Unrelated content\n")
                raise RuntimeError("Interrupted setup")
        assert notes.read_bytes() == b"Unrelated content\n"
        assert not (workspace / ".specify/workspace.json").exists()
    assert not (project.repository_root / ".specify/project.json").exists()
    assert not storage.project_record_path(project.project_id).exists()


@pytest.mark.parametrize("existing_workspace", [False, True])
def test_concurrent_storage_claims_preserve_one_owner(tmp_path: Path, existing_workspace: bool):
    """Two racing claims leave one owner and preserve unrelated repository and workspace bytes."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    storage = importlib.import_module("specify_cli._command_init_storage")
    workspace = tmp_path / "workspace"
    if existing_workspace:
        workspace.mkdir()
    projects = [
        storage.select_storage(tmp_path / name, "external", workspace)
        for name in ("first", "second")
    ]
    for project in projects:
        project.repository_root.mkdir()
        (project.repository_root / "source.txt").write_bytes(b"Existing source\n")
    start = Barrier(2)

    def claim(project):
        start.wait(timeout=10)
        try:
            with storage.claim_storage(project):
                (workspace / "notes.txt").write_bytes(b"Unrelated content\n")
            return project
        except (OSError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim, project) for project in projects]
        winners = [result for future in futures if (result := future.result(timeout=20)) is not None]
    assert len(winners) == 1
    winner = winners[0]
    assert resolve_project(winner.repository_root) == winner
    loser = next(project for project in projects if project != winner)
    assert not (loser.repository_root / ".specify/project.json").exists()
    assert not storage.project_record_path(loser.project_id).exists()
    assert (workspace / "notes.txt").read_bytes() == b"Unrelated content\n"
    for project in projects:
        assert (project.repository_root / "source.txt").read_bytes() == b"Existing source\n"


@pytest.mark.parametrize("failed_write", [1, 2, 3])
def test_init_reports_storage_claim_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_write: int
):
    """Every initial metadata failure preserves source bytes and reports a handled error."""
    storage = importlib.import_module("specify_cli._command_init_storage")
    atomic_json = storage.atomic_json
    writes = 0

    def fail_write(path, data, **options):
        nonlocal writes
        writes += 1
        if writes == failed_write:
            raise PermissionError("Storage write denied")
        return atomic_json(path, data, **options)

    monkeypatch.setattr(storage, "atomic_json", fail_write)
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "source.txt"
    source.write_bytes(b"Existing source\n")
    workspace = tmp_path / "workspace"

    result = _init(repository, ".", "--force", "--workspace", str(workspace))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "Storage write denied" in _strip(result.output)
    assert "Traceback" not in result.output
    assert source.read_bytes() == b"Existing source\n"
    assert not (repository / ".specify/project.json").exists()
    assert not list(tmp_path.rglob("projects/*.json"))


@pytest.mark.parametrize("storage", ["local", "external"])
@pytest.mark.parametrize("kind,identifier,command_name", [
    ("preset", "self-test", "speckit.specify"),
    ("extension", "git", "speckit.git.feature"),
])
def test_init_addons_keep_native_commands_in_repository(
    tmp_path: Path, storage: str, kind: str, identifier: str, command_name: str,
):
    """Add-on assets follow storage, but project-scoped native commands stay in the repository."""
    result = _init(
        tmp_path, "project", "--storage", storage, "--script", "py",
        f"--{kind}", identifier, integration="omp",
    )
    assert result.exit_code == 0, _strip(result.output)
    repository = tmp_path / "project"
    project = resolve_project(repository)
    native = repository / ".omp/commands" / f"{command_name}.md"
    assert native.is_file(), str(native)
    if kind == "preset":
        has_override = "<!-- preset:self-test -->" in native.read_text(encoding="utf-8")
        assert has_override
    assert (project.workspace_root / ".specify" / f"{kind}s" / identifier).is_dir()
    if storage == "external":
        assert not (project.workspace_root / ".omp").exists()
        assert {path.name for path in (repository / ".specify").iterdir()} == {"project.json"}


def test_reinit_explicit_target_does_not_use_other_project_override(tmp_path, monkeypatch):
    """Reinitialization changes only the explicit target, even with another valid discovery override."""
    projects = []
    for name in ("target", "other"):
        result = _init(tmp_path, name, "--storage", "external", "--script", "py", integration="omp")
        assert result.exit_code == 0, _strip(result.output)
        projects.append(resolve_project(tmp_path / name))
    target, other = projects
    before = {
        path: path.read_bytes()
        for root in (other.repository_root, other.workspace_root)
        for path in root.rglob("*") if path.is_file()
    }
    monkeypatch.setenv("SPECIFY_INIT_DIR", str(other.repository_root))

    result = _init(
        tmp_path, str(target.repository_root), "--force",
        "--feature-numbering", "timestamp", integration="omp",
    )

    assert result.exit_code == 0, _strip(result.output)
    options = json.loads((target.workspace_root / ".specify/init-options.json").read_text(encoding="utf-8"))
    assert options["feature_numbering"] == "timestamp"
    assert {
        path: path.read_bytes()
        for root in (other.repository_root, other.workspace_root)
        for path in root.rglob("*") if path.is_file()
    } == before
    monkeypatch.delenv("SPECIFY_INIT_DIR")
    assert resolve_project(target.repository_root) == target
    assert resolve_project(other.repository_root) == other


@pytest.mark.parametrize("global_commands", [False, True])
def test_git_python_command_uses_workspace_in_native_and_global_rendering(tmp_path, monkeypatch, global_commands):
    """Both delivery routes point the shipped Git interpreter command at the same workspace asset."""
    import shlex

    result = _init(
        tmp_path, "project", "--storage", "external", "--script", "py", "--extension", "git",
        *(["--global-commands"] if global_commands else []), integration="omp",
    )
    assert result.exit_code == 0, _strip(result.output)
    repository = tmp_path / "project"
    project = resolve_project(repository)
    if global_commands:
        monkeypatch.chdir(repository)
        rendered = CliRunner().invoke(app, ["project", "command", "speckit.git.feature", "--json"])
        assert rendered.exit_code == 0, rendered.output
        content = json.loads(rendered.stdout)["content"]
    else:
        content = (repository / ".omp/commands/speckit.git.feature.md").read_text(encoding="utf-8")
    invocation = re.search(r"`(python3 [^`]+)`", content).group(1)
    arguments = shlex.split(invocation)
    script = project.workspace_root / ".specify/extensions/git/scripts/python/create_new_feature_branch.py"
    assert arguments[:2] == ["python3", str(script)]
    assert arguments[2:] == ["--json", "--short-name", "<short-name>", "<feature description>"]
    assert script.is_file()
