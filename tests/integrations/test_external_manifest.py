"""Integration manifests keep workspace assets separate from native entrypoints."""

import json
from uuid import uuid4

import pytest

from specify_cli.integrations.manifest import IntegrationManifest


@pytest.fixture
def external_project(tmp_path):
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    (repo / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    identity = str(uuid4())
    (repo / ".specify/project.json").write_text(json.dumps({
        "schema_version": 1, "project_id": identity, "storage": "external",
    }), encoding="utf-8")
    (workspace / ".specify/workspace.json").write_text(json.dumps({
        "schema_version": 1, "project_id": identity,
    }), encoding="utf-8")
    record = repo / ".specify/checkout.json"
    record.write_text(json.dumps({
        "schema_version": 1, "workspace": str(workspace), "active_feature": None,
    }), encoding="utf-8")
    return repo, workspace


def test_mixed_root_manifest_round_trip_and_uninstall_preserve_edits_and_identity(external_project):
    repo, workspace = external_project
    locator = (repo / ".specify/project.json").read_bytes()
    identity = (workspace / ".specify/workspace.json").read_bytes()
    manifest = IntegrationManifest("test", repo, version="1.2")
    dispatcher = manifest.record_file(".specify/events.py", "dispatcher")
    native = manifest.record_file(".agent/commands/speckit.plan.md", "launcher")
    assert dispatcher == workspace / ".specify/events.py"
    assert native == repo / ".agent/commands/speckit.plan.md"
    assert dispatcher.read_text() == "dispatcher"
    assert native.read_text() == "launcher"
    assert manifest.save() == workspace / ".specify/integrations/test.manifest.json"
    data = json.loads(manifest.manifest_path.read_text())
    assert set(data["files"]) == {".specify/events.py", ".agent/commands/speckit.plan.md"}
    assert str(workspace) not in manifest.manifest_path.read_text()
    assert str(repo) not in manifest.manifest_path.read_text()
    assert {line for line in (repo / ".gitignore").read_text().splitlines() if line} == {
        "/.agent/commands/speckit.plan.md",
    }
    assert sorted(p.name for p in (repo / ".specify").iterdir()) == ["checkout.json", "project.json"]

    loaded = IntegrationManifest.load("test", repo)
    assert loaded.version == "1.2"
    assert loaded.check_modified() == []
    dispatcher.write_text("custom dispatcher")
    assert loaded.check_modified() == [".specify/events.py"]
    removed, skipped = loaded.uninstall(repo, remove_manifest=False)
    assert removed == [native]
    assert skipped == [dispatcher]
    assert dispatcher.read_text() == "custom dispatcher"
    assert loaded.manifest_path.exists()
    removed, skipped = loaded.uninstall(repo, force=True)
    assert removed == [dispatcher]
    assert skipped == []
    assert not loaded.manifest_path.exists()
    assert (repo / ".specify/project.json").read_bytes() == locator
    assert (workspace / ".specify/workspace.json").read_bytes() == identity


def test_record_existing_uses_relative_keys_and_recovered_lifecycle(external_project):
    repo, workspace = external_project
    asset = workspace / ".specify/events.py"
    native = repo / ".agent/settings.json"
    native.parent.mkdir()
    asset.write_text("existing dispatcher")
    native.write_text("user settings")
    manifest = IntegrationManifest("test", repo)
    manifest.record_existing(".specify/events.py", recovered=True)
    manifest.record_existing(".agent/settings.json", recovered=True)
    assert manifest.recovered_files == {".specify/events.py", ".agent/settings.json"}
    assert not (repo / ".gitignore").exists()
    manifest.save()
    loaded = IntegrationManifest.load("test", repo)
    assert loaded.is_recovered(".specify/events.py")
    assert loaded.is_recovered(".agent/settings.json")
    loaded.record_file(".specify/events.py", "generated dispatcher")
    assert not loaded.is_recovered(".specify/events.py")
    native.write_text("edited user settings")
    assert loaded.check_modified() == [".agent/settings.json"]
    assert loaded.remove(".specify/events.py")
    assert asset.read_text() == "generated dispatcher"
    assert ".specify/events.py" not in loaded.files
    removed, skipped = loaded.uninstall()
    assert removed == []
    assert skipped == [native]
    assert native.read_text() == "edited user settings"


@pytest.mark.parametrize("method", ["record_file", "record_existing"])
@pytest.mark.parametrize("relative", ["../escape", ".specify/../../escape", ".specify/../native", ".agent/../native"])
def test_record_rejects_noncanonical_relative_paths(external_project, method, relative):
    repo, workspace = external_project
    manifest = IntegrationManifest("test", repo)
    with pytest.raises(ValueError):
        if method == "record_file":
            manifest.record_file(relative, "unsafe")
        else:
            manifest.record_existing(relative)
    assert manifest.files == {}
    assert not (repo / "native").exists()
    assert not (workspace / "native").exists()


@pytest.mark.parametrize("method", ["record_file", "record_existing"])
@pytest.mark.parametrize("relative", [".specify/linked/file.txt", ".agent/linked/file.txt"])
def test_record_rejects_symlink_ancestors_in_either_root(external_project, tmp_path, method, relative):
    repo, workspace = external_project
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("keep")
    root = workspace if relative.startswith(".specify/") else repo
    link = (root / relative).parent
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    manifest = IntegrationManifest("test", repo)
    with pytest.raises(ValueError):
        if method == "record_file":
            manifest.record_file(relative, "unsafe")
        else:
            manifest.record_existing(relative)
    assert target.read_text() == "keep"
    assert manifest.files == {}


@pytest.mark.parametrize("relative", [".specify/managed/file.txt", ".agent/managed/file.txt"])
def test_tampered_ancestor_is_modified_and_force_uninstall_cannot_escape(external_project, tmp_path, relative):
    repo, workspace = external_project
    root = workspace if relative.startswith(".specify/") else repo
    tracked = root / relative
    tracked.parent.mkdir(parents=True)
    tracked.write_text("managed")
    manifest = IntegrationManifest("test", repo)
    manifest.record_existing(relative)
    manifest.save()
    outside = tmp_path / "outside"
    tracked.parent.rename(outside)
    tracked.parent.symlink_to(outside, target_is_directory=True)
    loaded = IntegrationManifest.load("test", repo)
    assert loaded.check_modified() == [relative]
    removed, skipped = loaded.uninstall(force=True)
    assert removed == []
    assert skipped == [tracked]
    assert (outside / "file.txt").read_text() == "managed"
    assert tracked.parent.is_symlink()


@pytest.mark.parametrize("relative", [".specify/events.py", ".agent/settings.json"])
def test_absolute_record_existing_key_is_rejected(external_project, relative):
    repo, workspace = external_project
    root = workspace if relative.startswith(".specify/") else repo
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing")
    manifest = IntegrationManifest("test", repo)
    with pytest.raises(ValueError):
        manifest.record_existing(path)
    assert manifest.files == {}


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("condition", ["valid", "unresolvable", "linked-state"])
def test_state_read_preserves_files_and_reports_invalid_roots(
    external_project, tmp_path, monkeypatch, external, condition
):
    """Invariant: a read cannot change files or accept an unvalidated state path."""
    from pathlib import Path
    from specify_cli.integration_state import try_read_integration_json

    repo, workspace = external_project
    if not external:
        repo = workspace = tmp_path / "local"
        (repo / ".specify").mkdir(parents=True)
    state_path = workspace / ".specify/integration.json"
    content = '{"integration": "copilot"}'
    if condition == "linked-state":
        target = tmp_path / "unrelated-state.json"
        target.write_text(content, encoding="utf-8")
        try:
            state_path.symlink_to(target)
        except OSError:
            pytest.skip("The host does not permit test symlinks.")
    else:
        state_path.write_text(content, encoding="utf-8")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    if condition == "unresolvable":
        original = Path.resolve

        def unavailable(path, *args, **kwargs):
            if path == repo:
                raise OSError("Project root cannot be resolved")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", unavailable)

    state, error = try_read_integration_json(repo)

    if condition == "valid":
        assert error is None
        assert state["integration"] == "copilot"
    else:
        assert state is None
        assert error.kind == "os"
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("relative", [".agent/commands/plan.md", ".specify/integrations/test/scripts/helper.py"])
@pytest.mark.parametrize("condition", ["valid", "linked-file", "linked-parent"])
def test_native_writer_validates_before_mutation(
    external_project, tmp_path, external, relative, condition
):
    """Invariant: rejected writes preserve every file and manifest claim."""
    from specify_cli.integrations.base import IntegrationBase

    repo, workspace = external_project
    if not external:
        repo = workspace = tmp_path / "local"
        repo.mkdir()
    manifest = IntegrationManifest("test", repo)
    root = workspace if relative.startswith(".specify/") else repo
    dest = root / relative
    if condition != "valid":
        unrelated = tmp_path / "unrelated"
        unrelated.mkdir()
        retained = unrelated / dest.name
        retained.write_text("Keep this user content\n", encoding="utf-8")
        try:
            if condition == "linked-file":
                dest.parent.mkdir(parents=True)
                dest.symlink_to(retained)
            else:
                dest.parent.parent.mkdir(parents=True)
                dest.parent.symlink_to(unrelated, target_is_directory=True)
        except OSError:
            pytest.skip("The host does not permit test symlinks.")
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        with pytest.raises(ValueError):
            IntegrationBase.write_file_and_record("Replacement\n", dest, repo, manifest)
        assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
        assert manifest.files == {}
    else:
        written = IntegrationBase.write_file_and_record("First\r\nSecond\r\n", dest, repo, manifest)
        assert written == dest
        assert dest.read_bytes() == b"First\nSecond\n"
        assert manifest.check_modified() == []
        assert set(manifest.files) == {relative}


@pytest.fixture
def private_checkouts(external_project, tmp_path):
    """Two Git checkouts of one private-mode project that share one workspace."""
    import subprocess

    repo, workspace = external_project
    identity = json.loads((workspace / ".specify/workspace.json").read_text())
    (workspace / ".specify/workspace.json").write_text(json.dumps({**identity, "private": True}))
    other = tmp_path / "other"
    (other / ".specify").mkdir(parents=True)
    for name in ("project.json", "checkout.json"):
        (other / ".specify" / name).write_bytes((repo / ".specify" / name).read_bytes())
    for checkout in (repo, other):
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    return repo, other, workspace


def _install(checkout, name):
    manifest = IntegrationManifest("test", checkout, version="1")
    manifest.record_file(".specify/events.py", "dispatcher")
    manifest.record_file(f".agent/commands/{name}.md", name)
    manifest.save()
    return manifest


def _exclude(checkout):
    return (checkout / ".git/info/exclude").read_text().splitlines()


def test_private_save_splits_manifest_by_root_and_hides_checkout_files(private_checkouts):
    repo, _, workspace = private_checkouts
    _install(repo, "plan")
    shared = json.loads((workspace / ".specify/integrations/test.manifest.json").read_text())
    local = json.loads((repo / ".specify/integrations/test.manifest.json").read_text())
    assert set(shared["files"]) == {".specify/events.py"}
    assert set(local["files"]) == {".agent/commands/plan.md"}
    assert set(IntegrationManifest.load("test", repo).files) == {".specify/events.py", ".agent/commands/plan.md"}
    assert not (repo / ".gitignore").exists()
    assert "/.agent/commands/plan.md" in _exclude(repo)
    assert "/.specify/" in _exclude(repo)


def test_private_checkout_uninstall_keeps_shared_state_and_other_checkout(private_checkouts):
    repo, other, workspace = private_checkouts
    _install(repo, "plan")
    _install(other, "tasks")
    other_before = {p: p.read_bytes() for p in other.rglob("*") if p.is_file() and ".git" not in p.parts}
    manifest = IntegrationManifest.load("test", repo)
    manifest.checkout_only = True
    removed, skipped = manifest.uninstall(repo)
    assert removed == [repo / ".agent/commands/plan.md"]
    assert skipped == []
    assert not (repo / ".specify/integrations/test.manifest.json").exists()
    assert (workspace / ".specify/events.py").read_text() == "dispatcher"
    assert (workspace / ".specify/integrations/test.manifest.json").exists()
    assert {p: p.read_bytes() for p in other.rglob("*") if p.is_file() and ".git" not in p.parts} == other_before
    assert "/.agent/commands/plan.md" not in _exclude(repo)
    assert "/.specify/" in _exclude(repo)


def test_private_project_uninstall_removes_shared_state_and_other_checkout_reloads(private_checkouts):
    repo, other, workspace = private_checkouts
    _install(repo, "plan")
    _install(other, "tasks")
    IntegrationManifest.load("test", repo).uninstall(repo)
    assert not (workspace / ".specify/events.py").exists()
    assert not (workspace / ".specify/integrations/test.manifest.json").exists()
    assert not (repo / ".specify/integrations/test.manifest.json").exists()
    leftover = IntegrationManifest.load("test", other)
    assert set(leftover.files) == {".agent/commands/tasks.md"}
    leftover.checkout_only = True
    removed, _ = leftover.uninstall(other)
    assert removed == [other / ".agent/commands/tasks.md"]
    with pytest.raises(FileNotFoundError):
        IntegrationManifest.load("test", other)


def test_exclude_records_paths_without_claiming_them(private_checkouts, external_project, tmp_path):
    repo, _, workspace = private_checkouts
    _install(repo, "plan")
    IntegrationManifest("test", repo).exclude([".agent/commands/ext.md"])
    local = json.loads((repo / ".specify/integrations/test.manifest.json").read_text())
    assert set(local["files"]) == {".agent/commands/plan.md"}
    assert local["excluded"] == [".agent/commands/ext.md"]
    assert "/.agent/commands/ext.md" in _exclude(repo)
    assert set(IntegrationManifest.load("test", repo).files) == {".specify/events.py", ".agent/commands/plan.md"}


def test_default_mode_exclude_writes_gitignore(external_project):
    repo, _ = external_project
    IntegrationManifest("test", repo).exclude([".agent/commands/ext.md"])
    assert "/.agent/commands/ext.md" in (repo / ".gitignore").read_text().splitlines()
