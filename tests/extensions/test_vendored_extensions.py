"""Tests for the vendored community extensions (memory, archive, brownfield).

They ship with spec-kit, so ``specify extension add <id>`` must install the
vendored copy without the network, keep the upstream license and provenance
note in every installed copy, and never be replaced by a community release
through ``specify extension update``.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.extensions import ExtensionCatalog, ExtensionManager

REPO_ROOT = Path(__file__).resolve().parents[2]

VENDORED = {
    "memory": {
        "commands": {"speckit.memory.recall", "speckit.memory.list-related-specs"},
        "hooks": {
            "before_specify": ("speckit.memory.recall", False),
            "before_plan": ("speckit.memory.recall", False),
            "after_plan": ("speckit.memory.list-related-specs", False),
        },
    },
    "archive": {"commands": {"speckit.archive.run"}, "hooks": {}},
    "brownfield": {
        "commands": {
            "speckit.brownfield.scan",
            "speckit.brownfield.bootstrap",
            "speckit.brownfield.validate",
            "speckit.brownfield.migrate",
        },
        "hooks": {"after_init": ("speckit.brownfield.scan", True)},
    },
}


def _no_network(*_args, **_kwargs):
    raise AssertionError("vendored extensions must not touch the network")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / ".specify" / "extensions").mkdir(parents=True)
    return project


def _invoke(project: Path, args: list[str]):
    with patch.object(Path, "cwd", return_value=project), \
         patch.object(ExtensionCatalog, "fetch_catalog", _no_network), \
         patch.object(ExtensionCatalog, "download_extension", _no_network), \
         patch.object(ExtensionCatalog, "download_extension_info", _no_network):
        return CliRunner().invoke(app, args, catch_exceptions=True)


@pytest.mark.parametrize("ext_id", sorted(VENDORED))
def test_add_by_name_installs_vendored_copy_offline(tmp_path: Path, ext_id: str):
    project = _project(tmp_path)

    result = _invoke(project, ["extension", "add", ext_id])

    assert result.exit_code == 0, result.output
    manager = ExtensionManager(project)
    assert manager.registry.is_installed(ext_id)

    installed = project / ".specify" / "extensions" / ext_id
    manifest = yaml.safe_load((installed / "extension.yml").read_text(encoding="utf-8"))
    assert {c["name"] for c in manifest["provides"]["commands"]} == VENDORED[ext_id]["commands"]
    assert (installed / "LICENSE").is_file()
    assert (installed / "UPSTREAM.md").is_file()

    hooks_file = project / ".specify" / "extensions.yml"
    registered = (
        yaml.safe_load(hooks_file.read_text(encoding="utf-8")).get("hooks", {})
        if hooks_file.is_file()
        else {}
    )
    ours = {
        event: [(h["command"], h["optional"]) for h in entries if h.get("extension") == ext_id]
        for event, entries in registered.items()
    }
    ours = {event: hooks for event, hooks in ours.items() if hooks}
    assert ours == {event: [hook] for event, hook in VENDORED[ext_id]["hooks"].items()}

    if ext_id == "memory":
        assert (installed / "memory-config.yml").is_file()


@pytest.mark.parametrize("ext_id", sorted(VENDORED))
def test_provenance_note_matches_vendored_manifest(ext_id: str):
    ext_dir = REPO_ROOT / "extensions" / ext_id
    manifest = yaml.safe_load((ext_dir / "extension.yml").read_text(encoding="utf-8"))
    note = (ext_dir / "UPSTREAM.md").read_text(encoding="utf-8")

    match = re.search(r"^\| Upstream version \| `([^`]+)`", note, re.MULTILINE)
    assert match, f"extensions/{ext_id}/UPSTREAM.md has no upstream version row"
    assert match.group(1) == manifest["extension"]["version"]


def test_newer_community_release_does_not_replace_vendored_copy(tmp_path: Path):
    from specify_cli._assets import get_speckit_version

    project = _project(tmp_path)
    ExtensionManager(project).install_from_directory(
        REPO_ROOT / "extensions" / "memory", get_speckit_version()
    )
    community_entry = {
        "id": "memory",
        "version": "99.0.0",
        "download_url": "https://example.com/spec-kit-memory-99.0.0.zip",
        "_install_allowed": False,
        "_catalog_name": "community",
    }

    with patch.object(ExtensionCatalog, "get_extension_info", return_value=community_entry):
        result = _invoke(project, ["extension", "update", "memory"])

    flat = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Updates not allowed from 'community'" in flat, flat
    assert ExtensionManager(project).registry.get("memory")["version"] == "0.3.0"
