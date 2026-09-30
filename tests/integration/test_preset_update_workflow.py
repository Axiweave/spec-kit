"""Public preset update checks with real package sources and registry state."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.presets import PresetManager

PRESET_ID = "update-workflow-demo"


def make_project(root: Path) -> Path:
    """Create a minimal Spec Kit project with the core templates presets need."""
    templates = root / ".specify" / "templates"
    (templates / "commands").mkdir(parents=True, exist_ok=True)
    (templates / "spec-template.md").write_text(
        "# Core Spec Template\n", encoding="utf-8"
    )
    return root


def write_preset(
    directory: Path,
    *,
    version: str,
    body: str,
    extra_file: str | None = None,
    preset_id: str = PRESET_ID,
) -> Path:
    """Write a minimal single-template preset under *directory*.

    ``extra_file`` adds a version-specific file so a later install can be
    distinguished from a stale copy of the previous one that was never removed.
    """
    templates = directory / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "spec-template.md").write_text(body, encoding="utf-8")
    if extra_file is not None:
        (directory / extra_file).write_text("marker\n", encoding="utf-8")

    (directory / "preset.yml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "preset": {
                    "id": preset_id,
                    "name": "Update Workflow Demo",
                    "version": version,
                    "description": "Fixture preset for update workflow tests",
                    "author": "Spec Kit tests",
                    "license": "MIT",
                },
                "requires": {"speckit_version": ">=0.1.0"},
                "provides": {
                    "templates": [
                        {
                            "type": "template",
                            "name": "spec-template",
                            "file": "templates/spec-template.md",
                            "description": "Replacement spec template",
                            "replaces": "spec-template",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    return directory




@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Path:
    root = make_project(tmp_path / "proj")
    monkeypatch.chdir(root)
    return root




def test_preset_update_replaces_installed_preset(project: Path, tmp_path: Path):
    """A successful update really swaps the installed preset on disk."""
    runner = CliRunner()

    original = write_preset(
        tmp_path / "v1",
        version="1.0.0",
        body="# Version One Template\n",
        extra_file="only-in-v1.md",
    )
    replacement = write_preset(
        tmp_path / "v2",
        version="2.0.0",
        body="# Version Two Template\n",
        extra_file="only-in-v2.md",
    )

    install = runner.invoke(
        app, ["preset", "add", "--dev", str(original), "--priority", "20"]
    )
    assert install.exit_code == 0, install.output

    installed_dir = project / ".specify" / "presets" / PRESET_ID
    assert (installed_dir / "only-in-v1.md").exists()

    update = runner.invoke(
        app,
        ["preset", "update", PRESET_ID, "--dev", str(replacement), "--priority", "5"],
    )
    assert update.exit_code == 0, update.output

    metadata = PresetManager(project).registry.get(PRESET_ID)
    assert metadata is not None, "update must leave the preset installed"
    assert metadata["version"] == "2.0.0"
    assert metadata["priority"] == 5

    template = installed_dir / "templates" / "spec-template.md"
    assert template.read_text(encoding="utf-8") == "# Version Two Template\n"
    assert (installed_dir / "only-in-v2.md").exists()
    assert not (installed_dir / "only-in-v1.md").exists(), (
        "the previous preset's files must be removed, not merged with the "
        "replacement"
    )
