"""Move work products only after a verified copy, with local rollback on failure."""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from specify_cli.workspace import project_record_path, resolve_project


ACTIVE = "specs/002-active"
FOREIGN_ID = "a60e8400-e29b-41d4-a716-446655440001"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def put(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def local_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "config",
        "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(key, str(value))
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Migration Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "migration@example.test")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    repo = tmp_path / "code repository"
    workspace = tmp_path / "external workspace"
    assets = {
        ".specify/memory/constitution.md": b"# Edited constitution\r\n\r\nKeep local decisions.\r\n",
        ".specify/templates/spec-template.md": b"# Custom template\nUser-added section: caf\xc3\xa9\n",
        ".specify/templates/commands/custom.md": b"Run the user's custom command.\n",
        ".specify/scripts/custom.sh": b"#!/bin/sh\nprintf 'custom script\\n'\n",
        ".specify/workflows/workflow-registry.json": b'{ "schema_version": "1.0", "workflows": {}, "user_note": "keep" }\n',
        ".specify/extensions/.registry": b'{ "extensions": {}, "user_note": "edited" }\n',
        ".specify/presets/.registry": b'{ "presets": {}, "user_note": "edited" }\n',
        ".specify/init-options.json": (
            b'{ "integration": "omp", "script": "sh", "feature_numbering": "timestamp",'
            b' "feature_selection": "automatic" }\n'
        ),
        ".specify/integration.json": b'{ "integration": "omp", "custom": true }\n',
        ".specify/.gitignore": b"*.local\n",
        ".specify/feature.json": b'{ "feature_directory": "specs/002-active" }\n',
        "specs/001-completed/spec.md": b"# Completed specification\n",
        "specs/001-completed/tasks.md": b"- [x] Preserve this completed work.\n",
        "specs/001-completed/contracts/api.json": b'{"openapi":"3.1.0"}\n',
        "specs/002-active/spec.md": b"# Active specification\n",
        "specs/002-active/plan.md": b"# Active plan\n",
        "specs/002-active/tasks.md": b"- [ ] Continue this work after the move.\n",
        "src/application.py": b"VALUE = 42\n",
        "README.md": b"Repository documentation stays here.\n",
        ".gitignore": b".specify/\nspecs/\n",
        ".omp/commands/user-command.md": b"Unrelated agent command stays here.\n",
    }
    rng = random.Random(35009)
    for index, size in enumerate((0, 1, 257, 8193)):
        assets[f".specify/user assets/custom-{index}.bin"] = rng.randbytes(size)
    for name, content in assets.items():
        put(repo, name, content)
    put(home, "config/specify/config.json", b'{ "storage_root": "/unused/default" }\n')
    put(home, "data/specify/projects/unrelated.json", b'{ "unrelated": true }\n')
    monkeypatch.chdir(repo)
    return repo, workspace, home


def assert_local(repo: Path, before: dict[str, bytes], active: str | None = ACTIVE) -> None:
    assert snapshot(repo) == before
    assert not (repo / ".specify/project.json").exists()
    project = resolve_project(repo)
    assert project.storage == "local"
    assert project.repository_root == repo
    assert project.workspace_root == repo
    assert project.feature_dir == (repo / active if active else None)
    if active:
        assert project.feature_dir.is_dir()
        assert (project.feature_dir / "spec.md").is_file()


@pytest.mark.parametrize(
    ("feature_count", "empty_destination"), [(0, False), (0, True), (1, False), (2, True)]
)
def test_prepare_conserves_source_and_commit_moves_every_work_product(
    local_project, feature_count, empty_destination
):
    from specify_cli.project.move import commit_move, prepare_move

    repo, workspace, home = local_project
    if feature_count < 2:
        shutil.rmtree(repo / "specs/001-completed")
    if feature_count == 0:
        shutil.rmtree(repo / "specs")
        (repo / ".specify/feature.json").unlink()
    if empty_destination:
        workspace.mkdir()
    active = ACTIVE if feature_count else None
    before, home_before = snapshot(repo), snapshot(home)
    work_products = {
        name: content for name, content in before.items()
        if name.startswith((".specify/", "specs/"))
    }
    unrelated = {name: content for name, content in before.items() if name not in work_products}

    prepared = prepare_move(repo, workspace)

    assert prepared.repository_root == repo
    assert prepared.workspace_root == workspace
    assert prepared.active_feature == active
    assert isinstance(prepared.files, tuple)
    assert set(prepared.files) == set(work_products)
    assert len(prepared.files) == len(work_products)
    for name, content in work_products.items():
        assert (workspace / name).read_bytes() == content
    assert_local(repo, before, active)
    assert snapshot(home) == home_before
    assert not project_record_path(prepared.project_id).exists()
    assert not (workspace / ".specify/project.json").exists()

    project = commit_move(prepared)

    assert project == resolve_project(repo)
    assert project.storage == "external"
    assert project.repository_root == repo
    assert project.workspace_root == workspace
    assert project.project_id == prepared.project_id
    assert project.feature_dir == (workspace / active if active else None)
    for name, content in work_products.items():
        if name != ".specify/feature.json":
            assert (workspace / name).read_bytes() == content
        assert not (repo / name).exists()
    assert not (repo / "specs").exists()
    locator = repo / ".specify/project.json"
    assert snapshot(repo) == {**unrelated, ".specify/project.json": locator.read_bytes()}
    assert {path.name for path in (repo / ".specify").iterdir()} == {"project.json"}
    assert json.loads(locator.read_text(encoding="utf-8")) == {
        "schema_version": 1, "project_id": prepared.project_id, "storage": "external",
    }
    assert json.loads((workspace / ".specify/workspace.json").read_text(encoding="utf-8")) == {
        "schema_version": 1, "project_id": prepared.project_id,
    }
    assert json.loads(project_record_path(prepared.project_id).read_text(encoding="utf-8")) == {
        "schema_version": 1, "workspace": str(workspace), "active_feature": active,
    }
    assert not (workspace / ".specify/project.json").exists()
    for name, content in home_before.items():
        assert (home / name).read_bytes() == content


def test_empty_metadata_project_can_move_without_a_feature(local_project):
    from specify_cli.project.move import commit_move, prepare_move

    repo, workspace, _ = local_project
    shutil.rmtree(repo / ".specify")
    shutil.rmtree(repo / "specs")
    (repo / ".specify").mkdir()
    before = snapshot(repo)

    prepared = prepare_move(repo, workspace)
    assert_local(repo, before, None)
    project = commit_move(prepared)

    assert project.storage == "external"
    assert project.feature_dir is None
    assert resolve_project(repo) == project
    for name, content in before.items():
        assert (repo / name).read_bytes() == content


@pytest.mark.parametrize("absolute_pointer", [False, True])
def test_custom_local_active_feature_moves_without_its_unrelated_sibling(local_project, absolute_pointer):
    from specify_cli.project.move import commit_move, prepare_move

    repo, workspace, _ = local_project
    custom = "design work/current feature"
    put(repo, f"{custom}/spec.md", b"# Custom active specification\n")
    put(repo, f"{custom}/tasks.md", b"- [ ] Continue custom feature.\n")
    sibling = put(repo, "design work/notes.txt", b"Not a Spec Kit work product.\n")
    (repo / ".specify/feature.json").write_text(json.dumps({
        "feature_directory": str(repo / custom) if absolute_pointer else custom,
    }), encoding="utf-8")
    before = snapshot(repo)

    prepared = prepare_move(repo, workspace)

    assert prepared.active_feature == custom
    assert snapshot(repo) == before
    assert (workspace / custom / "spec.md").read_bytes() == before[f"{custom}/spec.md"]
    project = commit_move(prepared)
    assert project.feature_dir == workspace / custom
    assert resolve_project(repo).feature_dir == workspace / custom
    assert (project.feature_dir / "tasks.md").read_bytes() == before[f"{custom}/tasks.md"]
    assert not (repo / custom).exists()
    assert sibling.read_bytes() == b"Not a Spec Kit work product.\n"
    assert not (workspace / "design work/notes.txt").exists()


def test_saved_active_feature_remains_independent_of_git_branch(local_project):
    from specify_cli.project.move import commit_move, prepare_move

    if shutil.which("git") is None:
        pytest.skip("Git is not installed.")
    repo, workspace, _ = local_project
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/001-completed"], check=True)
    git_before = snapshot(repo / ".git")

    prepared = prepare_move(repo, workspace)
    assert prepared.active_feature == ACTIVE
    commit_move(prepared)

    assert snapshot(repo / ".git") == git_before
    assert resolve_project(repo).feature_dir == workspace / ACTIVE
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/unrelated-branch"], check=True)
    assert resolve_project(repo).feature_dir == workspace / ACTIVE


@pytest.mark.parametrize(
    "destination_kind",
    [
        "file", "occupied", "foreign", "repository", "repository-child",
        "metadata-child", "feature-child", "ancestor", "traversal",
    ],
)
def test_invalid_destination_fails_before_any_write(local_project, tmp_path, destination_kind):
    from specify_cli.project.move import prepare_move

    repo, workspace, _ = local_project
    destination = workspace
    if destination_kind == "file":
        workspace.write_bytes(b"Existing unrelated file.\n")
    elif destination_kind == "occupied":
        put(workspace, "user-notes.txt", b"Existing unrelated workspace data.\n")
    elif destination_kind == "foreign":
        put(workspace, ".specify/workspace.json", json.dumps({
            "schema_version": 1, "project_id": FOREIGN_ID,
        }).encode())
        put(workspace, "specs/001-foreign/spec.md", b"Foreign project work.\n")
    elif destination_kind == "repository":
        destination = repo
    elif destination_kind == "repository-child":
        destination = repo / "new workspace"
    elif destination_kind == "metadata-child":
        destination = repo / ".specify/new workspace"
    elif destination_kind == "feature-child":
        destination = repo / "specs/new workspace"
    elif destination_kind == "ancestor":
        destination = tmp_path
    elif destination_kind == "traversal":
        (tmp_path / "intermediate").mkdir()
        destination = tmp_path / "intermediate/../external workspace"
    before, paths_before = snapshot(tmp_path), set(tmp_path.rglob("*"))

    with pytest.raises((ValueError, OSError)):
        prepare_move(repo, destination)

    assert snapshot(tmp_path) == before
    assert set(tmp_path.rglob("*")) == paths_before
    assert resolve_project(repo).feature_dir == repo / ACTIVE


@pytest.mark.parametrize("selection", ["traversal", "outside-absolute", "missing"])
def test_invalid_active_selection_does_not_stage_or_remove_content(local_project, tmp_path, selection):
    from specify_cli.project.move import prepare_move

    repo, workspace, _ = local_project
    outside = put(tmp_path, "outside/spec.md", b"Unrelated outside feature.\n").parent
    value = {
        "traversal": "../outside",
        "outside-absolute": str(outside),
        "missing": "specs/003-missing",
    }[selection]
    (repo / ".specify/feature.json").write_text(json.dumps({"feature_directory": value}), encoding="utf-8")
    before, paths_before = snapshot(tmp_path), set(tmp_path.rglob("*"))

    with pytest.raises((ValueError, OSError)):
        prepare_move(repo, workspace)

    assert snapshot(tmp_path) == before
    assert set(tmp_path.rglob("*")) == paths_before
    assert not workspace.exists()


@pytest.mark.parametrize("escape", ["asset-file", "feature-directory", "metadata-root", "specs-root", "destination-alias"])
def test_symlink_escape_fails_before_any_write(local_project, tmp_path, escape):
    from specify_cli.project.move import prepare_move

    repo, workspace, _ = local_project
    if escape == "destination-alias":
        link, target = tmp_path / "destination alias", repo / "specs"
        destination = link / "new workspace"
    else:
        source = {
            "asset-file": repo / ".specify/memory/constitution.md",
            "feature-directory": repo / ACTIVE,
            "metadata-root": repo / ".specify",
            "specs-root": repo / "specs",
        }[escape]
        target = source.rename(tmp_path / "outside content")
        link, destination = source, workspace
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("The platform does not support test symlinks.")
    before, paths_before = snapshot(tmp_path), set(tmp_path.rglob("*"))

    with pytest.raises((ValueError, OSError)):
        prepare_move(repo, destination)

    assert link.is_symlink()
    assert snapshot(tmp_path) == before
    assert set(tmp_path.rglob("*")) == paths_before


@pytest.mark.parametrize("location", ["source", "stage"])
@pytest.mark.parametrize("change", ["edit", "add", "delete", "selection"])
def test_commit_rejects_changed_source_or_stage_without_further_changes(local_project, location, change):
    from specify_cli.project.move import commit_move, prepare_move

    repo, workspace, home = local_project
    prepared = prepare_move(repo, workspace)
    root = repo if location == "source" else workspace
    target = root / ACTIVE / "tasks.md"
    if change == "edit":
        target.write_bytes(b"- [x] New work after preparation.\n")
    elif change == "add":
        put(root, f"{ACTIVE}/new research.md", b"New research after preparation.\n")
    elif change == "delete":
        target.unlink()
    else:
        (root / ".specify/feature.json").write_text(
            '{"feature_directory":"specs/001-completed"}', encoding="utf-8"
        )
    before, staged, home_before = snapshot(repo), snapshot(workspace), snapshot(home)
    active = "specs/001-completed" if location == "source" and change == "selection" else ACTIVE

    with pytest.raises((ValueError, OSError)):
        commit_move(prepared)

    assert_local(repo, before, active)
    assert snapshot(workspace) == staged
    assert snapshot(home) == home_before
    assert not project_record_path(prepared.project_id).exists()


@pytest.mark.parametrize("fault", ["copy-error", "corrupt-copy"])
def test_copy_or_verification_failure_keeps_source_and_staged_evidence(local_project, monkeypatch, fault):
    from specify_cli.project.move import prepare_move

    repo, workspace, home = local_project
    before, home_before = snapshot(repo), snapshot(home)
    original = shutil.copyfile
    victim = ".specify/memory/constitution.md"
    injected = False

    def copyfile(source, destination, *args, **kwargs):
        nonlocal injected
        result = original(source, destination, *args, **kwargs)
        if Path(source) == repo / victim:
            injected = True
            if fault == "copy-error":
                raise OSError("Injected copy failure after a partial copy.")
            data = Path(destination).read_bytes()
            Path(destination).write_bytes(bytes(value ^ 0xFF for value in data))
        return result

    monkeypatch.setattr(shutil, "copyfile", copyfile)

    with pytest.raises((ValueError, OSError)):
        prepare_move(repo, workspace)

    assert injected
    assert_local(repo, before)
    assert snapshot(home) == home_before
    assert (workspace / victim).is_file()
    if fault == "corrupt-copy":
        assert (workspace / victim).read_bytes() != before[victim]


def test_partial_source_cleanup_failure_restores_local_project(local_project, monkeypatch):
    from specify_cli.project.move import commit_move, prepare_move

    repo, workspace, home = local_project
    before, home_before = snapshot(repo), snapshot(home)
    prepared = prepare_move(repo, workspace)
    original = shutil.rmtree
    removed = []
    committed_snapshot = {}

    def rmtree(path, *args, **kwargs):
        root = Path(path)
        if not removed:
            committed_snapshot.update(snapshot(workspace))
            victim = next(candidate for candidate in root.rglob("*") if candidate.is_file())
            removed.append(victim)
            victim.unlink()
            raise OSError("Injected cleanup failure after removal of one source file.")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)

    with pytest.raises((ValueError, OSError)):
        commit_move(prepared)

    assert removed
    assert_local(repo, before)
    assert snapshot(home) == home_before
    assert not project_record_path(prepared.project_id).exists()
    assert snapshot(workspace) == committed_snapshot
    assert not (workspace / ".specify/feature.json").exists()
    assert subprocess.check_output(
        ["git", "-C", str(workspace), "rev-list", "--count", "HEAD"], text=True,
    ).strip() == "1"


@pytest.mark.parametrize("target", ["machine-record", "locator"])
@pytest.mark.parametrize("after_write", [False, True])
def test_state_write_failure_rolls_back_local_files_and_machine_state(
    local_project, monkeypatch, target, after_write
):
    from specify_cli.project import move

    repo, workspace, home = local_project
    before, home_before = snapshot(repo), snapshot(home)
    prepared = move.prepare_move(repo, workspace)
    record = project_record_path(prepared.project_id)
    destination = record if target == "machine-record" else repo / ".specify/project.json"
    original = move.atomic_json
    injected = False

    def atomic_json(path, data):
        nonlocal injected
        if Path(path) == destination and not injected:
            injected = True
            if after_write:
                original(path, data)
            raise OSError("Injected state write failure.")
        return original(path, data)

    monkeypatch.setattr(move, "atomic_json", atomic_json)

    with pytest.raises((ValueError, OSError)):
        move.commit_move(prepared)

    assert injected
    assert_local(repo, before)
    assert snapshot(home) == home_before
    assert not record.exists()
    for name, content in before.items():
        if name.startswith((".specify/", "specs/")) and name != ".specify/feature.json":
            assert (workspace / name).read_bytes() == content


@pytest.mark.parametrize("after_write", [False, True])
def test_workflow_path_relocation_failure_preserves_source(local_project, monkeypatch, after_write):
    from specify_cli.project import move
    from specify_cli.workflows.engine import RunState

    repo, workspace, home = local_project
    resource = repo / ".specify/workflows/resource-wf"
    resource.mkdir()
    (resource / "message.txt").write_bytes(b"Keep this resource.\n")
    state = RunState(run_id="paused-run", workflow_id="resource-wf", project_root=repo)
    state.workflow_dir = str(resource)
    state.save()
    relative = ".specify/workflows/runs/paused-run/state.json"
    legacy = json.loads((repo / relative).read_text(encoding="utf-8"))
    legacy["workflow_dir"] = str(resource)
    (repo / relative).write_text(json.dumps(legacy), encoding="utf-8")
    before, home_before = snapshot(repo), snapshot(home)
    prepared = move.prepare_move(repo, workspace)
    staged = snapshot(workspace)
    original = move.atomic_json

    def atomic_json(path, data):
        if path == workspace / relative:
            if after_write:
                original(path, data)
            raise OSError("Injected workflow state write failure.")
        original(path, data)

    monkeypatch.setattr(move, "atomic_json", atomic_json)
    with pytest.raises(ValueError, match="Injected workflow state write failure"):
        move.commit_move(prepared)

    assert_local(repo, before)
    assert snapshot(workspace) == staged
    assert snapshot(home) == home_before
    assert RunState.load(state.run_id, repo).workflow_dir == str(resource)
