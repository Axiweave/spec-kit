"""Public CLI contracts for shared OMP command ownership and project lifecycles."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.integrations import get_integration


CORE_COMMANDS = {
    f"speckit.{template.stem}.md"
    for template in get_integration("omp").list_command_templates()
}


@pytest.fixture(autouse=True)
def isolated_omp_environment(tmp_path, monkeypatch):
    for name in (
        "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR", "OMP_PROFILE", "PI_PROFILE",
        "SPECKIT_INTEGRATION_DEFAULT",
    ):
        monkeypatch.delenv(name, raising=False)
    outside = tmp_path / "outside-project"
    outside.mkdir()
    monkeypatch.chdir(outside)


def invoke(*args: str):
    return CliRunner().invoke(app, list(args), catch_exceptions=False)


def succeed(*args: str):
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    return result


def default_commands() -> Path:
    return Path(os.environ["HOME"]) / ".omp/agent/commands"


def command_files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.glob("*.md")}


def init_project(repository: Path, workspace: Path | None = None) -> None:
    storage = ["--workspace", str(workspace)] if workspace else ["--storage", "local"]
    succeed(
        "init", str(repository), "--integration", "omp", "--script", "sh",
        "--global-commands", "--non-interactive", "--ignore-agent-tools",
        *storage,
    )


def test_global_install_outside_project_is_idempotent_and_explicitly_removable():
    commands = default_commands()
    commands.mkdir(parents=True)
    unrelated = commands / "personal.md"
    unrelated.write_text("My own command.\n", encoding="utf-8")

    succeed("integration", "install", "omp", "--global")
    installed = command_files(commands)
    assert CORE_COMMANDS <= installed.keys()
    assert not (Path.cwd() / ".specify").exists()
    assert not (Path.cwd() / ".omp").exists()

    succeed("integration", "install", "omp", "--global")
    assert command_files(commands) == installed
    succeed("integration", "upgrade", "omp", "--global")
    assert command_files(commands) == installed

    succeed("integration", "uninstall", "omp", "--global")
    assert command_files(commands) == {"personal.md": b"My own command.\n"}
    succeed("integration", "uninstall", "omp", "--global")
    assert unrelated.read_text(encoding="utf-8") == "My own command.\n"


@pytest.mark.parametrize("action", ["install", "upgrade", "uninstall"])
@pytest.mark.parametrize("force", [False, True], ids=["safe", "force"])
def test_global_lifecycle_preserves_modified_managed_files(action, force):
    succeed("integration", "install", "omp", "--global")
    commands = default_commands()
    modified = commands / "speckit.plan.md"
    modified.write_text("My edited plan command.\n", encoding="utf-8")
    unrelated = commands / "speckit.personal.md"
    unrelated.write_text("My unrelated command.\n", encoding="utf-8")
    (commands / "speckit.specify.md").unlink()

    result = invoke("integration", action, "omp", "--global", *(["--force"] if force else []))

    assert modified.read_text(encoding="utf-8") == "My edited plan command.\n"
    assert unrelated.read_text(encoding="utf-8") == "My unrelated command.\n"
    if action == "uninstall":
        assert result.exit_code != 0
        assert modified.name in re.sub(r"[\s\u2500-\u257f]", "", result.output)
        assert not (commands / "speckit.tasks.md").exists()
    else:
        # One edited launcher never blocks the other launchers.
        assert result.exit_code == 0, result.output
        assert (commands / "speckit.specify.md").is_file()
        if action == "upgrade":
            assert modified.name in re.sub(r"[\s\u2500-\u257f]", "", result.output)
        invoke("integration", "uninstall", "omp", "--global")
        assert modified.read_text(encoding="utf-8") == "My edited plan command.\n"
        assert unrelated.read_text(encoding="utf-8") == "My unrelated command.\n"
        assert not (commands / "speckit.specify.md").exists()


def test_global_upgrade_skips_unowned_name_and_installs_the_rest():
    commands = default_commands()
    commands.mkdir(parents=True)
    user_command = commands / "speckit.plan.md"
    user_command.write_text("This name already belongs to me.\n", encoding="utf-8")

    result = succeed("integration", "upgrade", "omp", "--global")

    assert user_command.name in re.sub(r"[\s\u2500-\u257f]", "", result.output)
    assert user_command.read_text(encoding="utf-8") == "This name already belongs to me.\n"
    assert CORE_COMMANDS - {user_command.name} <= command_files(commands).keys()
    succeed("integration", "uninstall", "omp", "--global")
    assert command_files(commands) == {user_command.name: b"This name already belongs to me.\n"}


def test_project_init_refreshes_unedited_launcher_from_an_older_release(tmp_path, monkeypatch):
    from specify_cli.integrations.omp import global_commands

    current = global_commands._launcher
    monkeypatch.setattr(global_commands, "_launcher", lambda name: b"old release\n")
    succeed("integration", "install", "omp", "--global")
    monkeypatch.setattr(global_commands, "_launcher", current)
    edited = default_commands() / "speckit.plan.md"
    edited.write_bytes(b"old release\nMy edit.\n")

    init_project(tmp_path / "project")

    files = command_files(default_commands())
    assert files.pop("speckit.plan.md") == b"old release\nMy edit.\n"
    assert files == {name: current(name[:-3]) for name in files}



def test_conflicting_user_command_is_never_claimed_or_removed():
    commands = default_commands()
    commands.mkdir(parents=True)
    user_command = commands / "speckit.plan.md"
    user_command.write_text("This name already belongs to me.\n", encoding="utf-8")

    conflict = invoke("integration", "install", "omp", "--global")

    assert conflict.exit_code != 0
    assert user_command.name in re.sub(r"[\s\u2500-\u257f]", "", conflict.output)
    assert user_command.read_text(encoding="utf-8") == "This name already belongs to me.\n"
    invoke("integration", "uninstall", "omp", "--global")
    assert user_command.read_text(encoding="utf-8") == "This name already belongs to me.\n"
    assert not (Path.cwd() / ".omp").exists()


def test_agent_directory_override_installs_and_removes_only_its_command_set(tmp_path, monkeypatch):
    succeed("integration", "install", "omp", "--global")
    original = command_files(default_commands())
    agent_dir = tmp_path / "custom agent directory"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))

    succeed("integration", "install", "omp", "--global")
    assert CORE_COMMANDS <= command_files(agent_dir / "commands").keys()
    succeed("integration", "uninstall", "omp", "--global")

    assert command_files(agent_dir / "commands") == {}
    assert command_files(default_commands()) == original


# OMP's packages/utils/src/dirs.ts:62-90,118-120,346-352 defines these paths.
# Named profiles ignore PI_CODING_AGENT_DIR. OMP_PROFILE wins over PI_PROFILE.
@pytest.mark.parametrize(
    ("environment", "relative_directory"),
    [
        ({"OMP_PROFILE": "work"}, ".omp/profiles/work/agent/commands"),
        ({"PI_PROFILE": "legacy"}, ".omp/profiles/legacy/agent/commands"),
        ({"OMP_PROFILE": "work", "PI_PROFILE": "legacy"}, ".omp/profiles/work/agent/commands"),
        ({"OMP_PROFILE": "", "PI_PROFILE": "legacy"}, ".omp/agent/commands"),
        ({"OMP_PROFILE": "default", "PI_PROFILE": "legacy"}, ".omp/agent/commands"),
    ],
)
def test_global_install_matches_native_omp_profile_selection(environment, relative_directory, monkeypatch):
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    if "/profiles/" in relative_directory:
        override = Path(os.environ["HOME"]) / "ignored-agent-override"
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(override))

    succeed("integration", "install", "omp", "--global")

    home = Path(os.environ["HOME"])
    selected = home / relative_directory
    assert CORE_COMMANDS <= command_files(selected).keys()
    discovered = {path.parent for path in home.rglob("speckit.specify.md")}
    assert discovered == {selected}
    succeed("integration", "uninstall", "omp", "--global")
    assert command_files(selected) == {}


def test_profile_uninstall_preserves_another_profiles_ownership(monkeypatch):
    from specify_cli.workspace import user_data_dir

    home = Path(os.environ["HOME"])
    monkeypatch.setenv("OMP_PROFILE", "work")
    succeed("integration", "install", "omp", "--global")
    work = home / ".omp/profiles/work/agent/commands"
    original = command_files(work)
    monkeypatch.setenv("OMP_PROFILE", "personal")
    succeed("integration", "install", "omp", "--global")
    personal = home / ".omp/profiles/personal/agent/commands"
    assert CORE_COMMANDS <= command_files(personal).keys()
    # One plain ownership record maps each command directory to its files.
    record = user_data_dir() / "integrations/omp/global-commands.json"
    assert [path.name for path in record.parent.iterdir()] == [record.name]
    assert set(json.loads(record.read_text(encoding="utf-8"))) == {str(work.resolve()), str(personal.resolve())}

    succeed("integration", "uninstall", "omp", "--global")

    assert command_files(personal) == {}
    assert command_files(work) == original
    monkeypatch.setenv("OMP_PROFILE", "work")
    succeed("integration", "uninstall", "omp", "--global")
    assert command_files(work) == {}


@pytest.mark.parametrize("action", ["install", "upgrade", "uninstall"])
def test_unsupported_global_integration_never_falls_back_to_local(action):
    result = invoke("integration", action, "claude", "--global")

    assert result.exit_code != 0
    assert "claude" in result.output.lower()
    assert "global" in result.output.lower()
    assert not (Path.cwd() / ".claude").exists()
    assert not (Path.cwd() / ".specify").exists()
    assert not (Path(os.environ["HOME"]) / ".claude").exists()
    assert not default_commands().exists()


def test_init_rejects_unsupported_global_integration_without_local_fallback(tmp_path):
    repository = tmp_path / "unsupported-project"

    result = invoke(
        "init", str(repository), "--integration", "claude", "--script", "sh",
        "--global-commands", "--non-interactive", "--ignore-agent-tools",
    )

    assert result.exit_code != 0
    assert "claude" in result.output.lower()
    assert "global" in result.output.lower()
    assert not (repository / ".claude").exists()
    assert not default_commands().exists()


@pytest.mark.parametrize("external", [False, True], ids=["local", "external"])
def test_global_init_reuses_commands_and_project_removal_preserves_shared_set(tmp_path, monkeypatch, external):
    first, second = tmp_path / "first", tmp_path / "second"
    init_project(first, tmp_path / "first-workspace" if external else None)
    shared = command_files(default_commands())
    assert CORE_COMMANDS <= shared.keys()
    init_project(second, tmp_path / "second-workspace" if external else None)
    assert command_files(default_commands()) == shared
    assert not (first / ".omp/commands").exists()
    assert not (second / ".omp/commands").exists()
    if external:
        assert not (tmp_path / "first-workspace/.omp/commands").exists()
        assert not (tmp_path / "second-workspace/.omp/commands").exists()

    monkeypatch.chdir(first)
    info = json.loads(succeed("project", "info", "--json").stdout)
    assert info["command_scope"] == "global"
    succeed("integration", "uninstall", "omp")
    assert command_files(default_commands()) == shared
    monkeypatch.chdir(second)
    shutil.rmtree(first)

    resolved = succeed("project", "command", "speckit.specify", "--json")
    json.loads(resolved.stdout)
    assert command_files(default_commands()) == shared


def test_removed_extension_keeps_shared_launcher_for_other_project(tmp_path, monkeypatch):
    source = tmp_path / "shared-check"
    (source / "commands").mkdir(parents=True)
    (source / "commands/hello.md").write_text(
        "---\ndescription: Shared extension check\n---\nReport the current project.\n",
        encoding="utf-8",
    )
    (source / "extension.yml").write_text(yaml.safe_dump({
        "schema_version": "1.0",
        "extension": {
            "id": "shared-check", "name": "Shared Check", "version": "1.0.0",
            "description": "Check shared command ownership", "author": "Test Author",
        },
        "requires": {"speckit_version": ">=0.1.0"},
        "provides": {"commands": [{
            "name": "speckit.shared-check.hello", "file": "commands/hello.md",
            "description": "Check the current project",
        }]},
    }), encoding="utf-8")
    first, second = tmp_path / "first", tmp_path / "second"
    for repository in (first, second):
        init_project(repository, tmp_path / f"{repository.name}-workspace")
        monkeypatch.chdir(repository)
        succeed("extension", "add", str(source), "--dev")
        assert not (repository / ".omp/commands").exists()

    launcher = default_commands() / "speckit.shared-check.hello.md"
    assert launcher.is_file()
    original = launcher.read_bytes()
    monkeypatch.chdir(first)
    succeed("extension", "remove", "shared-check", "--force")
    assert launcher.read_bytes() == original
    missing = invoke("project", "command", "speckit.shared-check.hello", "--json")
    assert missing.exit_code != 0
    assert missing.stdout == ""
    assert "speckit.shared-check.hello" in missing.stderr

    monkeypatch.chdir(second)
    resolved = succeed("project", "command", "speckit.shared-check.hello", "--json")
    json.loads(resolved.stdout)
    assert launcher.read_bytes() == original
    succeed("extension", "remove", "shared-check", "--force")
    assert launcher.read_bytes() == original
    succeed("integration", "uninstall", "omp", "--global")
    assert not launcher.exists()


@pytest.mark.parametrize("failure", ["command-write", "manifest-write"])
def test_partial_global_install_rolls_back_only_new_files(failure, monkeypatch):
    commands = default_commands()
    commands.mkdir(parents=True)
    unrelated = commands / "speckit.personal.md"
    unrelated.write_text("Keep my command.\n", encoding="utf-8")
    original_open = Path.open
    original_replace = os.replace
    writes = 0

    def fail_command_write(path, mode="r", *args, **kwargs):
        nonlocal writes
        if path.parent.resolve() == commands.resolve() and "x" in mode:
            writes += 1
            if writes == 2:
                raise OSError("Simulated command write failure")
        return original_open(path, mode, *args, **kwargs)

    def fail_manifest_write(source, destination, *args, **kwargs):
        if Path(destination).suffix == ".json":
            raise OSError("Simulated manifest write failure")
        return original_replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as patch:
        if failure == "command-write":
            patch.setattr(Path, "open", fail_command_write)
        else:
            patch.setattr(os, "replace", fail_manifest_write)
        result = invoke("integration", "install", "omp", "--global")

    assert result.exit_code != 0
    assert "Simulated" in result.output
    assert command_files(commands) == {"speckit.personal.md": b"Keep my command.\n"}
    assert not (Path.cwd() / ".omp").exists()
    succeed("integration", "install", "omp", "--global")
    succeed("integration", "uninstall", "omp", "--global")
    assert command_files(commands) == {"speckit.personal.md": b"Keep my command.\n"}


def test_public_extension_update_preserves_shared_ownership_and_other_project(tmp_path, monkeypatch):
    """An update changes only the invoking project's extension and adds only new shared command names."""
    from specify_cli.extensions import ExtensionCatalog, ExtensionManager
    from specify_cli.workspace import user_data_dir
    from tests.specify_cli.extensions.test_command_update import TestExtensionUpdateCLI

    sources = []
    for version, extra in (("1.0.0", "legacy"), ("2.0.0", "current")):
        source = TestExtensionUpdateCLI._create_extension_source(tmp_path, version, include_config=True)
        manifest_path = source / "extension.yml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["provides"]["commands"].append({
            "name": f"speckit.test-ext.{extra}", "file": f"commands/{extra}.md",
        })
        manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        for name in ("hello", extra):
            (source / f"commands/{name}.md").write_text(
                f"---\ndescription: Versioned command\n---\nRelease {version}: {name}.\n",
                encoding="utf-8",
            )
        sources.append(source)

    first, second = tmp_path / "first", tmp_path / "second"
    first_workspace, second_workspace = tmp_path / "first-workspace", tmp_path / "second-workspace"
    for repository, workspace in ((first, first_workspace), (second, second_workspace)):
        init_project(repository, workspace)
        monkeypatch.chdir(repository)
        succeed("extension", "add", str(sources[0]), "--dev")

    commands = default_commands()
    (commands / "speckit.plan.md").write_text("Keep my edited global plan.\n", encoding="utf-8")
    (commands / "personal.md").write_text("Keep my unrelated command.\n", encoding="utf-8")
    shared_before = command_files(commands)
    ownership_path = user_data_dir() / "integrations/omp/global-commands.json"
    ownership_before = json.loads(ownership_path.read_text(encoding="utf-8"))[str(commands.resolve())]
    user_config = first_workspace / ".specify/extensions/test-ext/linear-config.yml"
    user_config.write_text("custom: true\nvalue: user-edited\n", encoding="utf-8")
    config_before = user_config.read_bytes()
    other_before = {
        path: path.read_bytes()
        for root in (second, second_workspace) for path in root.rglob("*") if path.is_file()
    }
    archive = Path(shutil.make_archive(str(tmp_path / "replacement"), "zip", sources[1]))
    monkeypatch.setattr(ExtensionCatalog, "get_extension_info", lambda self, identifier: {
        "id": "test-ext", "name": "Test Extension", "version": "2.0.0",
        "_install_allowed": True,
    })
    monkeypatch.setattr(ExtensionCatalog, "download_extension", lambda self, identifier: archive)
    monkeypatch.chdir(first)

    updated = CliRunner().invoke(app, ["extension", "update", "test-ext"], input="y\n")

    assert updated.exit_code == 0, updated.output
    assert ExtensionManager(first).registry.get("test-ext")["version"] == "2.0.0"
    assert user_config.read_bytes() == config_before
    assert "Release 2.0.0: hello." in json.loads(
        succeed("project", "command", "speckit.test-ext.hello", "--json").stdout
    )["content"]
    assert "Release 2.0.0: current." in json.loads(
        succeed("project", "command", "speckit.test-ext.current", "--json").stdout
    )["content"]
    assert invoke("project", "command", "speckit.test-ext.legacy", "--json").exit_code == 1
    assert (commands / "speckit.test-ext.current.md").is_file()
    assert {name: (commands / name).read_bytes() for name in shared_before} == shared_before
    ownership_after = json.loads(ownership_path.read_text(encoding="utf-8"))[str(commands.resolve())]
    assert {name: ownership_after[name] for name in ownership_before} == ownership_before
    assert ownership_after.keys() - ownership_before.keys() == {"speckit.test-ext.current.md"}
    assert "personal.md" not in ownership_after

    monkeypatch.chdir(second)
    assert "Release 1.0.0: hello." in json.loads(
        succeed("project", "command", "speckit.test-ext.hello", "--json").stdout
    )["content"]
    assert "Release 1.0.0: legacy." in json.loads(
        succeed("project", "command", "speckit.test-ext.legacy", "--json").stdout
    )["content"]
    assert invoke("project", "command", "speckit.test-ext.current", "--json").exit_code == 1
    assert {path: path.read_bytes() for path in other_before} == other_before
    assert not (first / ".omp/commands").exists()
    assert not (first_workspace / ".omp/commands").exists()


def test_other_default_integration_keeps_global_omp_scope(tmp_path, monkeypatch):
    repository = tmp_path / "project"
    init_project(repository)
    monkeypatch.chdir(repository)
    succeed("integration", "install", "claude", "--force")
    succeed("integration", "use", "claude")

    succeed("integration", "upgrade", "omp")

    assert not (repository / ".omp/commands").exists()
    assert CORE_COMMANDS <= command_files(default_commands()).keys()


def test_switch_to_global_scope_removes_unchanged_local_commands(tmp_path):
    repository = tmp_path / "project"
    options = ("--integration", "omp", "--script", "sh", "--non-interactive", "--ignore-agent-tools", "--storage", "local")
    succeed("init", str(repository), *options)
    local = repository / ".omp/commands"
    assert CORE_COMMANDS <= command_files(local).keys()
    (local / "speckit.plan.md").write_text("My local plan.\n", encoding="utf-8")

    succeed("init", str(repository), "--force", "--global-commands", *options)

    assert command_files(local) == {"speckit.plan.md": b"My local plan.\n"}
    assert CORE_COMMANDS <= command_files(default_commands()).keys()


def test_status_reports_missing_and_modified_global_commands(tmp_path, monkeypatch):
    repository = tmp_path / "project"
    init_project(repository)
    monkeypatch.chdir(repository)

    def findings() -> dict[str, dict]:
        report = json.loads(invoke("integration", "status", "--json").stdout)
        return {finding["code"]: finding for finding in report["findings"]}

    assert not {"global-commands-missing", "global-commands-modified"} & findings().keys()
    commands = default_commands()
    (commands / "speckit.specify.md").unlink()
    (commands / "speckit.plan.md").write_text("My edited plan.\n", encoding="utf-8")

    reported = findings()
    assert "speckit.specify.md" in reported["global-commands-missing"]["message"]
    assert "integration install omp --global" in reported["global-commands-missing"]["suggestion"]
    assert "speckit.plan.md" in reported["global-commands-modified"]["message"]
    assert "speckit.specify.md" not in reported["global-commands-modified"]["message"]

    succeed("integration", "install", "omp", "--global")
    assert "global-commands-missing" not in findings()
