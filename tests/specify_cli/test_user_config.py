"""Personal defaults preserve user data and validate values before writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from specify_cli import user_config, workspace


FIELDS = ("storage_root", "feature_numbering", "integration", "script")
VALID_VALUES = (
    ("storage_root", "~/spec storage"),
    ("feature_numbering", "sequential"),
    ("feature_numbering", "timestamp"),
    ("integration", "omp"),
    ("integration", "copilot"),
    ("script", "sh"),
    ("script", "ps"),
    ("script", "py"),
)


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
    monkeypatch.setattr(user_config, "os", SimpleNamespace(**(vars(os) | {"name": "posix"})))
    return tmp_path / "config/specify/config.json"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("key,value", VALID_VALUES)
def test_set_get_clear_round_trip_preserves_unknown_values(config_file, key, value):
    unknown = {"future": {"enabled": True, "items": [1, "keep"]}, "catalogs": ["private"]}
    save(config_file, unknown)

    user_config.set_default(key, value)

    assert user_config.get_default(key) == value
    assert json.loads(config_file.read_text()) == unknown | {key: value}
    user_config.clear_default(key)
    assert user_config.get_default(key) == ""
    assert json.loads(config_file.read_text()) == unknown


def test_missing_config_reads_empty_without_creating_files(config_file):
    assert user_config.load_defaults() == {}
    for key in FIELDS:
        assert user_config.get_default(key) == ""
    assert not config_file.parent.exists()


@pytest.mark.parametrize("key", FIELDS)
def test_empty_value_and_absent_value_both_mean_no_default(config_file, key):
    user_config.set_default(key, "")
    assert user_config.get_default(key) == ""
    assert user_config.load_defaults().get(key, "") == ""
    assert json.loads(config_file.read_text()) == {key: ""}

    user_config.clear_default(key)
    user_config.clear_default(key)
    assert user_config.get_default(key) == ""
    assert key not in json.loads(config_file.read_text())


@pytest.mark.parametrize("key", ["unknown", "", "../config", None, 7])
def test_invalid_key_cannot_read_or_change_saved_data(config_file, key):
    save(config_file, {"script": "sh", "unknown": "preserve"})
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        user_config.get_default(key)
    with pytest.raises(ValueError):
        user_config.set_default(key, "py")
    with pytest.raises(ValueError):
        user_config.clear_default(key)
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("key,value", [
    ("feature_numbering", "random"),
    ("integration", "not-a-registered-integration"),
    ("script", "bash"),
])
def test_invalid_nonempty_choice_preserves_saved_data(config_file, key, value):
    save(config_file, {"script": "sh", "future": {"keep": True}})
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        user_config.set_default(key, value)
    assert config_file.read_bytes() == before

    save(config_file, {key: value, "future": {"keep": True}})
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        user_config.load_defaults()
    assert config_file.read_bytes() == before
    user_config.clear_default(key)
    assert json.loads(config_file.read_text()) == {"future": {"keep": True}}


@pytest.mark.parametrize("key", FIELDS)
@pytest.mark.parametrize("value", [None, False, 7, [], {}])
def test_defaults_require_strings_on_write_and_read(config_file, key, value):
    save(config_file, {"future": "keep"})
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        user_config.set_default(key, value)
    assert config_file.read_bytes() == before

    save(config_file, {key: value, "future": "keep"})
    before = config_file.read_bytes()
    with pytest.raises(ValueError):
        user_config.load_defaults()
    assert config_file.read_bytes() == before
    user_config.set_default(key, "")
    assert json.loads(config_file.read_text()) == {key: "", "future": "keep"}
    save(config_file, {key: value, "future": "keep"})
    user_config.clear_default(key)
    assert json.loads(config_file.read_text()) == {"future": "keep"}


@pytest.mark.parametrize("content", [b'{"script":', b"[]", b"null", b'"text"', b"\xff"])
def test_malformed_config_blocks_reads_and_writes_without_losing_bytes(config_file, content):
    config_file.parent.mkdir(parents=True)
    config_file.write_bytes(content)
    with pytest.raises(ValueError):
        user_config.load_defaults()
    with pytest.raises(ValueError):
        user_config.get_default("script")
    with pytest.raises(ValueError):
        user_config.set_default("script", "py")
    with pytest.raises(ValueError):
        user_config.clear_default("script")
    assert config_file.read_bytes() == content


def test_unreadable_config_blocks_updates_without_losing_bytes(config_file, monkeypatch):
    save(config_file, {"script": "sh", "future": "keep"})
    before = config_file.read_bytes()
    original_open = Path.open

    def unreadable(path, *args, **kwargs):
        if path == config_file:
            raise PermissionError("Config read denied")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", unreadable)
        with pytest.raises((ValueError, OSError)):
            user_config.load_defaults()
        with pytest.raises((ValueError, OSError)):
            user_config.set_default("script", "py")
        with pytest.raises((ValueError, OSError)):
            user_config.clear_default("script")
    assert config_file.read_bytes() == before


@pytest.mark.parametrize("xdg", [None, "", "custom", "~/settings"])
def test_unix_config_path_uses_xdg_or_home(tmp_path, monkeypatch, xdg):
    if xdg is None:
        monkeypatch.delenv("XDG_CONFIG_HOME")
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom") if xdg == "custom" else xdg)
    if xdg == "custom":
        expected = tmp_path / "custom/specify/config.json"
    elif xdg == "~/settings":
        expected = tmp_path / "home/settings/specify/config.json"
    else:
        expected = tmp_path / "home/.config/specify/config.json"
    assert user_config.config_path() == expected
    user_config.set_default("script", "py")
    assert json.loads(expected.read_text()) == {"script": "py"}


def test_windows_config_path_uses_roaming_appdata(tmp_path, monkeypatch):
    monkeypatch.setattr(user_config, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    expected = tmp_path / "roaming/specify/config.json"
    assert user_config.config_path() == expected
    user_config.set_default("script", "ps")
    assert json.loads(expected.read_text()) == {"script": "ps"}
    assert not (tmp_path / "config/specify/config.json").exists()
    assert not (tmp_path / "local/specify/config.json").exists()


def test_storage_root_reads_preserve_home_notation_without_creating_workspace(config_file, tmp_path):
    user_config.set_default("storage_root", "~/spec storage")
    before = config_file.read_bytes()
    assert user_config.get_default("storage_root") == "~/spec storage"
    assert user_config.load_defaults()["storage_root"] == "~/spec storage"
    assert config_file.read_bytes() == before
    assert not (tmp_path / "home/spec storage").exists()


@pytest.mark.parametrize("operation,existing", [("set", False), ("set", True), ("clear", True)])
def test_interrupted_atomic_update_keeps_previous_file(config_file, monkeypatch, operation, existing):
    if existing:
        save(config_file, {"script": "sh", "future": "keep"})
    before = config_file.read_bytes() if existing else None

    def interrupt(source, destination):
        raise OSError("Atomic replacement interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(workspace.os, "replace", interrupt)
        with pytest.raises((ValueError, OSError), match="Atomic replacement interrupted"):
            if operation == "set":
                user_config.set_default("script", "py")
            else:
                user_config.clear_default("script")
    assert (config_file.read_bytes() if config_file.exists() else None) == before
    assert set(config_file.parent.iterdir()) == ({config_file} if existing else set())


@pytest.mark.parametrize("link_kind", ["file", "parent"])
def test_config_symlink_escape_cannot_read_or_modify_outside_data(config_file, tmp_path, link_kind):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "config.json"
    target.write_text('{"script":"sh","future":"keep"}', encoding="utf-8")
    before = target.read_bytes()
    if link_kind == "file":
        config_file.parent.mkdir(parents=True)
        config_file.symlink_to(target)
        link = config_file
    else:
        config_file.parent.parent.mkdir(parents=True)
        config_file.parent.symlink_to(outside, target_is_directory=True)
        link = config_file.parent

    with pytest.raises(ValueError):
        user_config.load_defaults()
    with pytest.raises(ValueError):
        user_config.set_default("script", "py")
    with pytest.raises(ValueError):
        user_config.clear_default("script")
    assert target.read_bytes() == before
    assert link.is_symlink()
    assert set(outside.iterdir()) == {target}
