"""Project inspection is read-only and reports the invoking project."""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from specify_cli import app


def test_info_reports_local_project_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/one"}')
    (tmp_path / ".specify/init-options.json").write_text('{"feature_selection":"automatic"}')
    monkeypatch.chdir(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["storage"] == "local"
    assert data["repository_root"] == data["workspace_root"] == str(tmp_path)
    assert data["active_feature"] == "specs/one"
    assert data["feature_selection"] == "automatic"
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_info_errors_do_not_corrupt_json_stream(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code != 0
    assert result.stdout == ""
    assert "Spec Kit project" in result.stderr


def test_extension_listing_does_not_depend_on_feature_selection(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".specify").mkdir()
    pointer = tmp_path / ".specify/feature.json"
    pointer.write_text("malformed feature state")
    result = CliRunner().invoke(app, ["extension", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert pointer.read_text() == "malformed feature state"


def _local_project(tmp_path, monkeypatch, options):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/one"}')
    if options is not None:
        (tmp_path / ".specify/init-options.json").write_text(json.dumps(options))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("options, saved_is_used", [
    (None, False),
    ({}, False),
    ({"feature_selection": "context"}, False),
    ({"feature_selection": "automatic", "future_setting": {"kept": True}}, True),
], ids=["no-file", "no-field", "context", "automatic"])
@pytest.mark.parametrize("override", [None, "specs/two"])
def test_info_reports_saved_feature_only_in_automatic_mode(tmp_path, monkeypatch, options, saved_is_used, override):
    _local_project(tmp_path, monkeypatch, options)
    if override:
        monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", override)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["feature_selection"] == ("automatic" if saved_is_used else "context")
    assert data["active_feature"] == (override or ("specs/one" if saved_is_used else None))
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("value", ["random", "AUTOMATIC", None, 1])
def test_info_rejects_unknown_selection_mode_without_json_output(tmp_path, monkeypatch, value):
    _local_project(tmp_path, monkeypatch, {"feature_selection": value})
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "feature_selection" in result.stderr
