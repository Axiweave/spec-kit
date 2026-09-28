"""The personal config CLI works without a project and preserves unrelated data."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith(("SPECIFY_", "SPECKIT_", "PI_", "OMP_")):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    cwd = tmp_path / "not-a-project"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    base = tmp_path / ("roaming" if os.name == "nt" else "config")
    return base / "specify/config.json"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("key,value", [
    ("storage_root", "~/spec storage"),
    ("feature_numbering", "timestamp"),
    ("integration", "omp"),
    ("script", "py"),
])
def test_config_round_trip_outside_project_preserves_other_settings(config_file, key, value):
    runner = CliRunner()
    saved = {
        "storage_root": "/saved/specs",
        "feature_numbering": "sequential",
        "integration": "copilot",
        "script": "sh",
        "future": {"enabled": True, "items": [1, "keep"]},
    }
    save(config_file, saved)

    result = runner.invoke(app, ["config", "set", key, value])
    assert result.exit_code == 0, result.output
    assert json.loads(config_file.read_text()) == saved | {key: value}
    before = config_file.read_bytes()
    result = runner.invoke(app, ["config", "get", key])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == value
    assert config_file.read_bytes() == before

    result = runner.invoke(app, ["config", "set", key, ""])
    assert result.exit_code == 0, result.output
    assert json.loads(config_file.read_text()) == saved | {key: ""}
    result = runner.invoke(app, ["config", "get", key])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""

    result = runner.invoke(app, ["config", "clear", key])
    assert result.exit_code == 0, result.output
    assert json.loads(config_file.read_text()) == {k: v for k, v in saved.items() if k != key}
    result = runner.invoke(app, ["config", "get", key])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("key", ["storage_root", "feature_numbering", "integration", "script"])
def test_get_missing_default_is_empty_and_read_only(config_file, key):
    result = CliRunner().invoke(app, ["config", "get", key])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert not config_file.parent.exists()
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args,key", [
    (["get", "unknown"], "unknown"),
    (["set", "unknown", "value"], "unknown"),
    (["clear", "unknown"], "unknown"),
    (["set", "feature_numbering", "random"], "feature_numbering"),
    (["set", "integration", "not-a-registered-integration"], "integration"),
    (["set", "script", "bash"], "script"),
])
def test_invalid_arguments_report_key_without_changing_config(config_file, args, key):
    save(config_file, {"script": "sh", "unknown": {"keep": True}})
    before = config_file.read_bytes()
    result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code != 0
    assert key in result.output
    assert config_file.read_bytes() == before
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args", [["get", "script"], ["set", "script", "py"], ["clear", "script"]])
@pytest.mark.parametrize("content", [b'{"script":', b"[]", b"\xff"])
def test_malformed_config_fails_without_replacing_bytes(config_file, args, content):
    config_file.parent.mkdir(parents=True)
    config_file.write_bytes(content)
    result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code != 0
    assert config_file.read_bytes() == content
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args", [["get", "script"], ["set", "script", "py"], ["clear", "script"]])
def test_unreadable_config_fails_without_changing_saved_data(config_file, monkeypatch, args):
    save(config_file, {"script": "sh", "future": "keep"})
    before = config_file.read_bytes()
    original_open = Path.open

    def unreadable(path, *positional, **keywords):
        if path == config_file:
            raise PermissionError("Config read denied")
        return original_open(path, *positional, **keywords)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", unreadable)
        result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code != 0
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("value", ["invalid", None, False, ["sh"]])
@pytest.mark.parametrize("args,expected", [
    (["set", "script", "py"], {"script": "py", "future": "keep"}),
    (["clear", "script"], {"future": "keep"}),
])
def test_selected_invalid_field_can_be_repaired_without_losing_unknown_fields(config_file, value, args, expected):
    save(config_file, {"script": value, "future": "keep"})
    result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code == 0, result.output
    assert json.loads(config_file.read_text()) == expected


@pytest.mark.parametrize("args", [["set", "script", "py"], ["clear", "script"]])
def test_config_command_refuses_symlink_escape(config_file, tmp_path, args):
    outside = tmp_path / "outside.json"
    outside.write_text('{"script":"sh","future":"keep"}', encoding="utf-8")
    before = outside.read_bytes()
    config_file.parent.mkdir(parents=True)
    config_file.symlink_to(outside)

    result = CliRunner().invoke(app, ["config", *args])

    assert result.exit_code != 0
    assert outside.read_bytes() == before
    assert config_file.is_symlink()
