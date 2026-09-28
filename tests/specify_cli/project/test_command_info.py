"""Project inspection is read-only and reports the invoking project."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from specify_cli import app


def test_info_reports_local_project_without_writing(tmp_path, monkeypatch):
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    (tmp_path / ".specify").mkdir()
    (tmp_path / ".specify/feature.json").write_text('{"feature_directory":"specs/one"}')
    monkeypatch.chdir(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["storage"] == "local"
    assert data["repository_root"] == data["workspace_root"] == str(tmp_path)
    assert data["active_feature"] == "specs/one"
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
