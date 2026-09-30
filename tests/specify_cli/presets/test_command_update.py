"""Behavior checks for safe preset restoration through the public command."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.presets import PresetManager, PresetValidationError
from specify_cli.presets._commands import _render_powershell_argv
from tests.specify_cli.presets._helpers import make_convention_constitution_preset


@pytest.fixture
def installed_preset(project_dir, tmp_path, monkeypatch):
    monkeypatch.chdir(project_dir)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    source = make_convention_constitution_preset(tmp_path / "source")
    manager = PresetManager(project_dir)
    manifest = manager.install_from_directory(source, "1.0.0")
    destination = manager.presets_dir / manifest.id
    (destination / "templates/spec-template.md").write_text("# Custom template\n", encoding="utf-8")
    (destination / "custom-note.md").write_text("Keep this note.\n", encoding="utf-8")
    return source, manager, manifest.id


def snapshot(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("case", [
    "missing-source", "empty-source", "empty-url", "conflicting-sources",
    "zero-priority", "negative-priority", "unknown-preset", "mismatched-id", "invalid-manifest",
])
def test_update_rejection_conserves_custom_files_and_registry(installed_preset, case, tmp_path):
    """Conservation: rejected updates change no installed bytes or registry fields."""
    source, manager, preset_id = installed_preset
    before = snapshot(manager.presets_dir)
    arguments = ["preset", "update", preset_id, "--dev", str(source)]
    if case == "missing-source":
        arguments[-1] = str(tmp_path / "missing source")
    elif case == "empty-source":
        arguments[-1] = ""
    elif case == "empty-url":
        arguments = ["preset", "update", preset_id, "--from", ""]
    elif case == "conflicting-sources":
        arguments.extend(["--from", "https://example.invalid/preset.zip"])
    elif case in ("zero-priority", "negative-priority"):
        arguments.extend(["--priority", "0" if case == "zero-priority" else "-1"])
    elif case == "unknown-preset":
        arguments[2] = "unknown-preset"
    elif case == "mismatched-id":
        manifest = source / "preset.yml"
        manifest.write_text(manifest.read_text().replace(preset_id, "foreign-preset"))
    elif case == "invalid-manifest":
        (source / "preset.yml").write_text("{}\n", encoding="utf-8")
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code != 0, result.output
    assert snapshot(manager.presets_dir) == before


@pytest.mark.parametrize("format", ["directory", "zip", "tar"])
def test_replacement_target_check_precedes_force_removal(installed_preset, tmp_path, format):
    """Conservation: all source formats reject a different target before removal."""
    source, manager, _ = installed_preset
    before = snapshot(manager.presets_dir)
    options = {"force": True, "expected_id": "foreign-preset"}
    with pytest.raises(PresetValidationError):
        if format == "directory":
            manager.install_from_directory(source, "1.0.0", **options)
        elif format == "zip":
            archive = tmp_path / "preset.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for path in source.rglob("*"):
                    if path.is_file():
                        output.write(path, path.relative_to(source))
            manager.install_from_zip(archive, "1.0.0", **options)
        else:
            archive = tmp_path / "preset.tar.gz"
            with tarfile.open(archive, "w:gz") as output:
                output.add(source, arcname="package")
            manager.install_from_archive(archive, "1.0.0", **options)
    assert snapshot(manager.presets_dir) == before


def test_powershell_retry_renderer_preserves_literal_arguments():
    """Round-trip: PowerShell preserves the rendered argument values."""
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not available")
    arguments = [
        "https://example.com/archive.zip?one=1&two=$value",
        r"C:\owner's presets",
    ]
    rendered = _render_powershell_argv([
        sys.executable, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *arguments,
    ])
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", rendered], check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == arguments
