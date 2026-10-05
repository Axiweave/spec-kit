"""`specify config get` reads personal settings without a project or side effects."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app
from tests.specify_cli.configs.conftest import save


@pytest.mark.parametrize("key", ["storage_root", "feature_numbering", "feature_selection", "integration", "script"])
def test_get_missing_default_is_empty_and_read_only(config_file, key):
    result = CliRunner().invoke(app, ["config", "get", key])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == ""
    assert not config_file.parent.exists()
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args,key", [
    (["get", "unknown"], "unknown"),
])
def test_invalid_arguments_report_key_without_changing_config(config_file, args, key):
    save(config_file, {"script": "sh", "unknown": {"keep": True}})
    before = config_file.read_bytes()
    result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code != 0
    assert key in result.output
    assert config_file.read_bytes() == before
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args", [["get", "script"]])
@pytest.mark.parametrize("content", [b'{"script":', b"[]", b"\xff"])
def test_malformed_config_fails_without_replacing_bytes(config_file, args, content):
    config_file.parent.mkdir(parents=True)
    config_file.write_bytes(content)
    result = CliRunner().invoke(app, ["config", *args])
    assert result.exit_code != 0
    assert config_file.read_bytes() == content
    assert list(Path.cwd().iterdir()) == []


@pytest.mark.parametrize("args", [["get", "script"]])
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
