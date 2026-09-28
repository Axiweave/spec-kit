from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.bundles import BundlerError
from specify_cli.bundles.catalog_config import add_source, remove_source
from specify_cli.bundles.catalogs import Scope, load_source_stack
from specify_cli.bundles.project import active_integration, find_project_root
from specify_cli.bundles.records import load_records, save_records
from tests.specify_cli.bundles.helpers import (
    catalog_entry_dict,
    make_project,
    valid_manifest_dict,
    write_catalog_file,
)
from tests.specify_cli.bundles.test_command_install import _build_mini
from tests.specify_cli.test_workspace import external

runner = CliRunner()


@pytest.mark.parametrize("storage", ["local", "external"])
def test_bundle_metadata_lifecycle(tmp_path: Path, monkeypatch, storage: str):
    if storage == "external":
        repository, workspace, _ = external(tmp_path, monkeypatch)
    else:
        monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
        repository = workspace = make_project(tmp_path / "repo")
    monkeypatch.chdir(repository)
    (repository / ".github/agents").mkdir(parents=True)
    (workspace / ".specify/integration.json").write_text(
        '{"default_integration": "copilot"}', encoding="utf-8"
    )
    artifact = _build_mini(tmp_path)

    installed = runner.invoke(app, ["bundle", "install", str(artifact), "--offline"])
    assert installed.exit_code == 0, installed.output
    assert find_project_root() == repository
    assert Path.cwd() == repository
    assert (workspace / ".specify/extensions/agent-context/extension.yml").is_file()
    native_command = repository / ".github/agents/speckit.agent-context.update.agent.md"
    assert native_command.is_file()
    if storage == "external":
        assert not (workspace / ".github").exists()
    records = json.loads((workspace / ".specify/bundle-records.json").read_text())
    assert [record["bundle_id"] for record in records["bundles"]] == ["mini"]
    listed = runner.invoke(app, ["bundle", "list"])
    assert listed.exit_code == 0, listed.output
    assert "mini" in listed.output
    removed = runner.invoke(app, ["bundle", "remove", "mini"])
    assert removed.exit_code == 0, removed.output
    assert load_records(repository) == []
    assert not (workspace / ".specify/extensions/agent-context").exists()
    assert not native_command.exists()

    catalog = repository / "local-catalog.json"
    write_catalog_file(catalog, {"demo": catalog_entry_dict("demo")})
    added = runner.invoke(
        app, ["bundle", "catalog", "add", "./local-catalog.json", "--id", "local"]
    )
    assert added.exit_code == 0, added.output
    config_path = workspace / ".specify/bundle-catalogs.yml"
    config = yaml.safe_load(config_path.read_text())
    assert config["catalogs"][0]["url"] == str(catalog)
    sources = [source for source in load_source_stack(repository) if source.scope == Scope.PROJECT]
    assert [(source.id, source.url) for source in sources] == [("local", str(catalog))]
    listed = runner.invoke(app, ["bundle", "catalog", "list"])
    assert listed.exit_code == 0, listed.output
    assert "local" in listed.output
    removed = runner.invoke(app, ["bundle", "catalog", "remove", "local"])
    assert removed.exit_code == 0, removed.output
    assert yaml.safe_load(config_path.read_text())["catalogs"] == []
    assert not any(source.scope == Scope.PROJECT for source in load_source_stack(repository))
    if storage == "external":
        assert sorted(path.name for path in (repository / ".specify").iterdir()) == ["project.json"]


def test_external_integration_is_authoritative(tmp_path: Path, monkeypatch):
    repository, workspace, _ = external(tmp_path, monkeypatch)
    monkeypatch.chdir(repository)
    (workspace / ".specify/integration.json").write_text(
        '{"default_integration": "copilot", "integration": "claude"}', encoding="utf-8"
    )
    assert active_integration(repository) == "copilot"
    bundle = tmp_path / "claude-bundle"
    bundle.mkdir()
    (bundle / "bundle.yml").write_text(
        yaml.safe_dump(valid_manifest_dict(integration={"id": "claude"})), encoding="utf-8"
    )
    (bundle / "README.md").write_text("# Claude bundle\n", encoding="utf-8")
    result = runner.invoke(
        app, ["bundle", "install", str(bundle), "--integration", "claude", "--offline"]
    )
    assert result.exit_code == 1
    assert "claude" in result.output and "copilot" in result.output
    assert not (workspace / ".specify/bundle-records.json").exists()
    assert sorted(path.name for path in (repository / ".specify").iterdir()) == ["project.json"]


@pytest.mark.skipif(os.name == "nt", reason="symlink semantics differ on Windows")
@pytest.mark.parametrize("filename", ["bundle-records.json", "bundle-catalogs.yml", "integration.json"])
def test_external_metadata_refuses_workspace_symlink_escape(tmp_path: Path, monkeypatch, filename: str):
    repository, workspace, _ = external(tmp_path, monkeypatch)
    outside = tmp_path / filename
    outside.write_text(
        '{"schema_version": "1.0", "bundles": [], "catalogs": [], "integration": "leaked"}',
        encoding="utf-8",
    )
    original = outside.read_bytes()
    (workspace / ".specify" / filename).symlink_to(outside)
    if filename == "integration.json":
        assert active_integration(repository) is None
    elif filename == "bundle-records.json":
        with pytest.raises(BundlerError, match="escapes the allowed root"):
            load_records(repository)
        with pytest.raises(BundlerError, match="escapes the allowed root"):
            save_records(repository, [])
    else:
        with pytest.raises(BundlerError, match="escapes the allowed root"):
            load_source_stack(repository)
        with pytest.raises(BundlerError, match="escapes the allowed root"):
            add_source(
                repository, "https://example.com/catalog.json",
                policy="install-allowed", priority=10,
            )
        with pytest.raises(BundlerError, match="escapes the allowed root"):
            remove_source(repository, "local")
    assert outside.read_bytes() == original
    assert sorted(path.name for path in (repository / ".specify").iterdir()) == ["project.json"]


@pytest.mark.parametrize("command", [["list"], ["catalog", "list"], ["catalog", "add", "./catalog.json"]])
def test_missing_external_mapping_fails_without_repository_metadata(tmp_path: Path, monkeypatch, command):
    repository, _, mapping = external(tmp_path, monkeypatch)
    monkeypatch.chdir(repository)
    mapping.unlink()
    result = runner.invoke(app, ["bundle", *command])
    assert result.exit_code == 1
    assert "Missing workspace mapping" in result.output
    assert sorted(path.name for path in (repository / ".specify").iterdir()) == ["project.json"]


@pytest.mark.parametrize("operation", ["init", "install"])
def test_automatic_setup_uses_all_personal_defaults(tmp_path, monkeypatch, operation):
    """Invariant: automatic setup preserves defaults and applies all four choices."""
    home = tmp_path / "home"
    home.mkdir()
    for key, directory in {
        "HOME": home, "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / "config", "APPDATA": home / "config",
        "XDG_DATA_HOME": home / "data", "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(key, str(directory))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECKIT_INTEGRATION_DEFAULT", raising=False)
    repository = tmp_path / "new-project"
    repository.mkdir()
    monkeypatch.chdir(repository)
    defaults = {
        "storage_root": str(tmp_path / "workspaces"),
        "feature_numbering": "timestamp", "integration": "omp", "script": "py",
    }
    for key, value in defaults.items():
        configured = runner.invoke(app, ["config", "set", key, value])
        assert configured.exit_code == 0, configured.output
    config = home / "config/specify/config.json"
    original = config.read_bytes()
    arguments = ["bundle", operation]
    if operation == "install":
        arguments.append(str(_build_mini(tmp_path)))

    result = runner.invoke(app, [*arguments, "--offline"])

    assert result.exit_code == 0, result.output
    inspected = runner.invoke(app, ["project", "info", "--json"])
    assert inspected.exit_code == 0, inspected.output
    info = json.loads(inspected.stdout)
    assert info["storage"] == "external"
    workspace = Path(info["workspace_root"])
    options = json.loads((workspace / ".specify/init-options.json").read_text())
    assert (options["integration"], options["script"], options["feature_numbering"]) == (
        "omp", "py", "timestamp",
    )
    assert sorted(path.name for path in (repository / ".specify").iterdir()) == ["project.json"]
    assert config.read_bytes() == original


@pytest.mark.parametrize("kind", ["workflows", "steps"])
def test_primitive_lifecycle_keeps_external_ownership(tmp_path, monkeypatch, kind):
    """Invariant: refresh preserves ownership, and failed refresh preserves installed bytes."""
    import io

    from specify_cli.authentication import http
    from specify_cli.bundles.adapters import DefaultPrimitiveInstaller
    from specify_cli.bundles.manifest import ComponentRef
    from specify_cli.bundles.references import make_reference_checker
    from specify_cli.workflows.catalog import StepRegistry, WorkflowRegistry

    repository, workspace, _ = external(tmp_path, monkeypatch)
    monkeypatch.chdir(repository)
    installer = DefaultPrimitiveInstaller()
    registry_type = WorkflowRegistry if kind == "workflows" else StepRegistry
    base = workspace / ".specify/workflows"
    if kind == "steps":
        base /= "steps"
    package = base / "storage-probe"
    bodies = {}

    class Response(io.BytesIO):
        def __init__(self, url):
            super().__init__(bodies[url])
            self.url = url

        def getheader(self, name):
            return None

        def geturl(self):
            return self.url

    monkeypatch.setattr(http, "open_url", lambda url, **kwargs: Response(url))
    for version in ("1.0.0", "2.0.0", "3.0.0"):
        url = f"https://example.invalid/{version}"
        catalog_url = f"{url}/catalog.json"
        entry = {"name": "Storage probe", "version": version}
        if kind == "workflows":
            entry["url"] = f"{url}/workflow.yml"
            document = {
                "schema_version": "1.0",
                "workflow": {"id": "storage-probe", "name": "Storage probe", "version": version},
                "steps": [{"id": "report", "type": "shell", "run": "echo workspace-probe"}],
            }
        else:
            entry.update(url=f"{url}/step.yml", init_url=f"{url}/__init__.py")
            document = {"schema_version": "1.0", "step": {
                "type_key": "storage-probe", "name": "Storage probe", "version": version,
            }}
            bodies[entry["init_url"]] = (
                "from specify_cli.workflows.base import StepBase, StepResult\n"
                "class StorageProbe(StepBase):\n"
                "    type_key = 'storage-probe'\n"
                "    def execute(self, config, context):\n"
                f"        return StepResult(output={{'version': '{version}'}})\n"
            ).encode()
        bodies[entry["url"]] = yaml.safe_dump(
            document if version != "3.0.0" else []
        ).encode()
        bodies[catalog_url] = json.dumps({kind: {"storage-probe": entry}}).encode()
        variable = "SPECKIT_WORKFLOW_CATALOG_URL" if kind == "workflows" else "SPECKIT_STEP_CATALOG_URL"
        monkeypatch.setenv(variable, catalog_url)
        component = ComponentRef(kind=kind, id="storage-probe", version=version)

        if version == "1.0.0":
            warnings = []
            checker = make_reference_checker(repository, allow_network=True, warnings=warnings)
            assert checker(component) is None
            assert warnings == []
            assert not (repository / ".specify/workflows").exists()
            installer.install(repository, component)
        elif version == "2.0.0":
            installer.refresh(repository, component)
        else:
            before = {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()}
            metadata = registry_type(workspace).get(component.id)
            with pytest.raises(BundlerError):
                installer.refresh(repository, component)
            assert {p.relative_to(package): p.read_bytes() for p in package.rglob("*") if p.is_file()} == before
            assert registry_type(workspace).get(component.id) == metadata
            break

        assert Path.cwd() == repository
        assert installer.is_installed(repository, component)
        assert registry_type(workspace).get(component.id)["version"] == version
        warnings = []
        assert make_reference_checker(repository, allow_network=False, warnings=warnings)(component) is None
        assert warnings == []
        assert not (repository / ".specify/workflows").exists()

    installer.remove(repository, component)
    assert not installer.is_installed(repository, component)
    assert not registry_type(workspace).is_installed(component.id)
    assert not package.exists()
    assert sorted(p.name for p in (repository / ".specify").iterdir()) == ["project.json"]
