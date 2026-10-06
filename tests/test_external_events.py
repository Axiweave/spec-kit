"""External event assets use the workspace while native hooks use the repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest
import yaml

from specify_cli.events import (
    collect_extension_events,
    install_integration_events,
    remove_integration_events,
    resolve_and_run_event_command,
    resolve_events,
)
from specify_cli.integrations import get_integration
from specify_cli.integrations.manifest import IntegrationManifest


PROJECT_ID = "c0202a2a-8266-48f9-bd03-48ed07196270"
EVENTS = {"pre_tool_use": [{"command": "speckit.external.boot"}]}
PAYLOAD = '{"message": "café"}'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def external_project(tmp_path, monkeypatch):
    repository = tmp_path / "code repo"
    workspace = tmp_path / "editor's workspace"
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    write_json(repository / ".specify/project.json", {
        "schema_version": 1, "project_id": PROJECT_ID, "storage": "external",
    })
    write_json(workspace / ".specify/workspace.json", {
        "schema_version": 1, "project_id": PROJECT_ID,
    })
    record = repository / ".specify/checkout.json"
    write_json(record, {"schema_version": 1, "workspace": str(workspace), "active_feature": None})
    write_json(workspace / ".specify/init-options.json", {"script": "py"})
    monkeypatch.chdir(repository)
    return repository, workspace, record


def add_command(workspace, *, extension=False, shell_handler=False):
    base = workspace / ".specify"
    template = base / "templates/commands/external.boot.md"
    if extension:
        base = base / "extensions/external"
        template = base / "commands/boot.md"
        base.mkdir(parents=True)
        (base / "extension.yml").write_text(
            "schema_version: '1.0'\n"
            "extension:\n  id: external\n  name: External\n  version: 1.0.0\n  description: Event check\n"
            "requires:\n  speckit_version: '>=0.1'\n"
            "provides:\n  commands:\n    - name: speckit.external.boot\n      file: commands/boot.md\n"
            "events:\n  pre_tool_use:\n    command: speckit.external.boot\n",
            encoding="utf-8",
        )
        write_json(workspace / ".specify/extensions/.registry", {"extensions": {"external": {"enabled": True}}})
    template.parent.mkdir(parents=True, exist_ok=True)
    script = base / ("scripts/boot.sh" if shell_handler else "scripts/boot.py")
    script.parent.mkdir(parents=True)
    template.write_text(
        f"---\nscripts:\n  {'sh' if shell_handler else 'py'}: scripts/{script.name}\n---\nRun the event.\n",
        encoding="utf-8",
    )
    script.write_text(
        "#!/bin/sh\nprintf '%s' \"$PWD\" > event-cwd.txt\ncat > event-payload.txt\n"
        if shell_handler else
        "from pathlib import Path\nimport sys\n"
        "Path('event-cwd.txt').write_text(str(Path.cwd()), encoding='utf-8')\n"
        "Path('event-payload.txt').write_bytes(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    if shell_handler:
        write_json(workspace / ".specify/init-options.json", {"script": "sh"})
    return script


def install(repository, key="claude", events=EVENTS):
    integration = get_integration(key)
    manifest = IntegrationManifest(key, repository)
    install_integration_events(integration, repository, manifest, events)
    manifest.save()
    return integration, manifest


def assert_execution(repository, workspace):
    assert (repository / "event-cwd.txt").read_text() == str(repository)
    assert (repository / "event-payload.txt").read_text(encoding="utf-8") == PAYLOAD
    assert not (workspace / "event-cwd.txt").exists()


@pytest.mark.parametrize("extension", [False, True], ids=["core", "extension"])
def test_package_dispatch_reads_workspace_assets(external_project, extension):
    repository, workspace, _ = external_project
    add_command(workspace, extension=extension)
    assert resolve_and_run_event_command("speckit.external.boot", "pre_tool_use", PAYLOAD, repository) == 0
    assert_execution(repository, workspace)
    if extension:
        assert collect_extension_events(repository) == EVENTS
        write_json(workspace / ".specify/extensions/.registry", {"extensions": {"external": {"enabled": False}}})
        assert collect_extension_events(repository) == {}


def test_workspace_override_controls_native_install(external_project):
    repository, workspace, _ = external_project
    add_command(workspace, extension=True)
    (workspace / ".specify/integration-events.yml").write_text(
        yaml.safe_dump({"integrations": {"claude": {"events": {"stop": {"command": "speckit.external.boot"}}}}}),
        encoding="utf-8",
    )
    events = resolve_events("claude", None, repository, None)
    assert events == {"stop": [{"command": "speckit.external.boot"}]}
    install(repository, events=events)
    native = json.loads((repository / ".claude/settings.json").read_text())
    assert set(native["hooks"]) == {"Stop"}


@pytest.mark.parametrize("extension", [False, True], ids=["core", "extension"])
def test_standalone_dispatch_runs_shell_from_repository(external_project, extension):
    if os.name == "nt":
        pytest.skip("This check runs a POSIX shell handler.")
    repository, workspace, _ = external_project
    add_command(workspace, extension=extension, shell_handler=True)
    install(repository)
    dispatcher = workspace / ".specify/events.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(dispatcher), "speckit.external.boot", "pre_tool_use"],
        cwd=repository, input=PAYLOAD, text=True, encoding="utf-8", capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert_execution(repository, workspace)
    assert not (repository / ".specify/events.py").exists()
    metadata = json.loads((workspace / ".specify/integrations/claude.manifest.json").read_text())
    assert ".specify/events.py" in metadata["files"]
    assert all(not Path(key).is_absolute() for key in metadata["files"])
    assert not (repository / ".specify/integrations").exists()


@pytest.mark.parametrize("key", ["claude", "codex", "copilot", "cursor-agent", "devin", "gemini", "qwen", "tabnine", "vibe"])
def test_native_hook_invokes_external_dispatcher(external_project, key):
    if os.name == "nt":
        pytest.skip("This check executes the POSIX native command.")
    repository, workspace, _ = external_project
    add_command(workspace)
    integration, _ = install(repository, key)
    native_path = repository / integration.events_config_file
    config = tomllib.loads(native_path.read_text()) if native_path.suffix == ".toml" else json.loads(native_path.read_text())
    native_event = integration.CANONICAL_TO_NATIVE["pre_tool_use"]
    if integration.events_format == "toml-vibe":
        command = config["hooks"][0]["command"]
    else:
        entry = (config if integration.events_format == "json-root-nested" else config["hooks"])[native_event][0]
        if "hooks" in entry:
            entry = entry["hooks"][0]
        command = entry.get("bash", entry.get("command"))
    result = subprocess.run(
        ["/bin/sh", "-c", command], cwd=repository, input=PAYLOAD, text=True, encoding="utf-8", capture_output=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repository)},
    )
    assert result.returncode == 0, result.stderr
    assert_execution(repository, workspace)
    assert not (workspace / integration.events_config_file).exists()


def test_opencode_plugin_uses_workspace_with_repository_directory(external_project, tmp_path):
    bun = shutil.which("bun")
    if not bun:
        pytest.skip("Bun is required to execute the native TypeScript plugin.")
    repository, workspace, _ = external_project
    add_command(workspace)
    install(repository, "opencode")
    plugin = repository / ".opencode/plugin/speckit-events.ts"
    runner = tmp_path / "run-plugin.ts"
    runner.write_text(
        f"import plugin from {json.dumps(str(plugin))};\n"
        f"const hooks = await plugin({{ directory: {json.dumps(str(repository))} }});\n"
        'await hooks["tool.execute.before"]({ tool: "read" }, {});\n',
        encoding="utf-8",
    )
    result = subprocess.run([bun, str(runner)], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (repository / "event-cwd.txt").read_text() == str(repository)
    assert json.loads((repository / "event-payload.txt").read_text()) == {"input": {"tool": "read"}, "output": {}}
    assert not (workspace / "event-cwd.txt").exists()


def test_copilot_powershell_hook_quotes_workspace(external_project):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is required to execute the native PowerShell hook.")
    repository, workspace, _ = external_project
    add_command(workspace)
    integration, _ = install(repository, "copilot")
    config = json.loads((repository / integration.events_config_file).read_text())
    command = config["hooks"]["preToolUse"][0]["powershell"]
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", command], cwd=repository,
        input=PAYLOAD, text=True, encoding="utf-8", capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert_execution(repository, workspace)


@pytest.mark.parametrize("standalone", [False, True], ids=["package", "standalone"])
def test_dispatch_rejects_foreign_mapping(external_project, standalone):
    repository, workspace, record = external_project
    add_command(workspace)
    install(repository)
    foreign = workspace.parent / "foreign"
    write_json(foreign / ".specify/workspace.json", {"schema_version": 1, "project_id": "b97317f6-bd1e-45b1-a905-12ab11f64db3"})
    add_command(foreign)
    write_json(record, {"schema_version": 1, "workspace": str(foreign), "active_feature": None})
    if standalone:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(workspace / ".specify/events.py"), "speckit.external.boot", "pre_tool_use"],
            cwd=repository, input=PAYLOAD, text=True, capture_output=True,
        )
        assert result.returncode != 0
    else:
        with pytest.raises(ValueError, match="another project"):
            resolve_and_run_event_command("speckit.external.boot", "pre_tool_use", PAYLOAD, repository)
    assert not (repository / "event-cwd.txt").exists()
    assert not (workspace / "event-cwd.txt").exists()
    assert not (foreign / "event-cwd.txt").exists()


def test_teardown_preserves_native_user_content_and_edited_plugin(external_project):
    repository, workspace, _ = external_project
    config_path = repository / ".claude/settings.json"
    user_hook = {"command": "echo user", "type": "command"}
    write_json(config_path, {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [user_hook]}]}})
    claude, manifest = install(repository)
    remove_integration_events(claude, repository, manifest)
    manifest.uninstall()
    assert json.loads(config_path.read_text()) == {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [{"hooks": [user_hook]}]}}
    assert not (workspace / ".specify/events.py").exists()
    opencode, manifest = install(repository, "opencode")
    plugin = repository / ".opencode/plugin/speckit-events.ts"
    edited = plugin.read_text() + "\n// User customization\n"
    plugin.write_text(edited)
    with pytest.raises(ValueError, match="user content"):
        install_integration_events(opencode, repository, IntegrationManifest("opencode", repository), EVENTS)
    assert plugin.read_text() == edited
    unrelated = plugin.with_name("user.ts")
    unrelated.write_text("export default {};\n")
    remove_integration_events(opencode, repository, manifest)
    manifest.uninstall()
    assert plugin.read_text() == edited
    assert unrelated.read_text() == "export default {};\n"


def test_teardown_rejects_foreign_mapping_before_native_changes(external_project):
    repository, workspace, _ = external_project
    integration, manifest = install(repository)
    native_path = repository / integration.events_config_file
    original = native_path.read_bytes()
    write_json(workspace / ".specify/workspace.json", {
        "schema_version": 1, "project_id": "b97317f6-bd1e-45b1-a905-12ab11f64db3",
    })
    with pytest.raises(ValueError, match="another project"):
        remove_integration_events(integration, repository, manifest)
    assert native_path.read_bytes() == original


@pytest.fixture
def private_project(external_project):
    repository, workspace, record = external_project
    write_json(workspace / ".specify/workspace.json", {
        "schema_version": 1, "project_id": PROJECT_ID, "private": True,
    })
    return repository, workspace, record


def test_private_claude_hooks_use_personal_settings_and_keep_user_keys(private_project):
    repository, workspace, _ = private_project
    shared = repository / ".claude/settings.json"
    write_json(shared, {"team": True})
    shared_bytes = shared.read_bytes()
    personal = repository / ".claude/settings.local.json"
    write_json(personal, {"permissions": {"allow": ["Read"]}})
    claude, manifest = install(repository)
    assert shared.read_bytes() == shared_bytes
    hooks = json.loads(personal.read_text())["hooks"]
    assert "PreToolUse" in hooks
    assert ".claude/settings.local.json" in manifest.files
    remove_integration_events(claude, repository, manifest)
    assert json.loads(personal.read_text()) == {"permissions": {"allow": ["Read"]}}
    assert shared.read_bytes() == shared_bytes


def test_private_copilot_hooks_keep_own_file(private_project):
    repository, _, _ = private_project
    install(repository, "copilot")
    assert (repository / ".github/hooks/speckit.json").is_file()


@pytest.mark.parametrize("key", ["gemini", "qwen", "tabnine", "devin", "cursor-agent", "codex", "vibe", "opencode"])
def test_private_mode_skips_shared_native_hooks(private_project, key, capsys):
    repository, _, _ = private_project
    integration, manifest = install(repository, key)
    assert not (repository / integration.events_config_file).exists()
    assert not (repository / ".opencode").exists()
    assert f"Native hooks skipped for {key}: its hook settings are shared." in capsys.readouterr().err
    assert all(key.startswith(".specify/") for key in manifest.files)
