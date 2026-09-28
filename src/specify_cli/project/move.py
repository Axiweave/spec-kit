"""Stage and commit a verified move from local project storage."""
from __future__ import annotations

import hashlib
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from specify_cli.workspace import (
    Project, atomic_json, confined, project_record_path, read_json, workspace_root_for,
)


@dataclass(frozen=True)
class _Entry:
    path: str
    mode: int
    digest: str | None


@dataclass(frozen=True)
class PreparedMove:
    repository_root: Path
    workspace_root: Path
    project_id: str
    active_feature: str | None
    files: tuple[str, ...]
    _roots: tuple[str, ...] = field(repr=False)
    _source: tuple[_Entry, ...] = field(repr=False)
    _stage: tuple[_Entry, ...] = field(repr=False)


def _inventory(root: Path, roots: tuple[str, ...] = (".",)) -> tuple[_Entry, ...]:
    entries = []

    def visit(path: Path) -> None:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"Move cannot preserve a symlink safely: {path}. Replace it with a local copy first.")
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            if path != root:
                entries.append(_Entry(path.relative_to(root).as_posix(), mode, None))
            for child in sorted(path.iterdir()):
                visit(child)
        elif stat.S_ISREG(info.st_mode):
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            entries.append(_Entry(path.relative_to(root).as_posix(), mode, digest))
        else:
            raise ValueError(f"Move requires ordinary files and directories: {path}")

    if root.resolve() != root:
        raise ValueError(f"Move root changed or contains a symlink: {root}")
    for name in roots:
        path = root / name
        if path.exists() or path.is_symlink():
            visit(path)
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _local_roots(repository: Path) -> tuple[tuple[str, ...], str | None]:
    metadata = repository / ".specify"
    if metadata.is_symlink() or not metadata.is_dir():
        raise ValueError(f"Move requires a local .specify directory: {repository}")
    locator = metadata / "project.json"
    if locator.exists() or locator.is_symlink():
        workspace_root_for(repository)
        raise ValueError(f"Project already selects external storage: {repository}. Use specify project link to relink it.")
    identity = metadata / "workspace.json"
    if identity.exists() or identity.is_symlink():
        raise ValueError(f"The source is already a workspace: {repository}. Select its local code repository instead.")
    for name in ("init-options.json", "integration.json"):
        path = metadata / name
        if path.exists() or path.is_symlink():
            read_json(path)
    roots = [".specify", "specs"]
    active = None
    pointer = metadata / "feature.json"
    if pointer.exists() or pointer.is_symlink():
        value = read_json(pointer).get("feature_directory")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing feature_directory: {pointer}. Select an existing local feature first.")
        raw = Path(value)
        if ".." in raw.parts:
            raise ValueError(f"Active feature contains traversal: {value}. Select an existing local feature first.")
        selected = (raw if raw.is_absolute() else repository / raw).resolve()
        if not selected.is_relative_to(repository):
            raise ValueError(
                f"Saved feature is outside the repository: {value}. "
                "Move cannot claim external data. Copy the feature into the repository and select that copy first."
            )
        relative = selected.relative_to(repository)
        if not relative.parts or ".git" in relative.parts or relative.parts[0] == ".specify" or relative == Path("specs"):
            raise ValueError(f"Active feature selects an ambiguous project root: {value}. Select a feature directory instead.")
        candidate = repository / raw if not raw.is_absolute() else raw
        for ancestor in candidate.parents:
            if ancestor.resolve() == repository:
                local = candidate.relative_to(ancestor)
                if any((repository / parent).is_symlink() for parent in (local, *local.parents)):
                    raise ValueError(f"Active feature contains a symlink: {value}. Select a local directory instead.")
                break
        if not selected.is_dir():
            raise ValueError(f"Active feature is missing: {selected}. Select an existing feature before the move.")
        active = relative.as_posix()
        if relative.parts[0] != "specs":
            roots.append(active)
    for name in roots:
        path = repository / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"Move requires a local directory: {path}")
    return tuple(roots), active


def prepare_move(repository: Path, destination: Path) -> PreparedMove:
    """Copy and verify owned artifacts without changing source or project state."""
    repository = repository.expanduser().resolve()
    raw = destination.expanduser()
    if ".." in raw.parts:
        raise ValueError(f"Workspace path contains traversal: {destination}")
    workspace = raw.resolve()
    if workspace.is_relative_to(repository) or repository.is_relative_to(workspace):
        raise ValueError(f"Workspace overlaps the repository: {workspace}. Choose a separate empty directory.")
    if workspace.exists() and (not workspace.is_dir() or any(workspace.iterdir())):
        raise ValueError(f"Workspace is occupied: {workspace}. Choose an absent or empty directory.")
    roots, active = _local_roots(repository)
    source = _inventory(repository, roots)
    project_id = str(uuid4())
    record = project_record_path(project_id)
    if record.exists() or record.is_symlink():
        raise ValueError(f"Project record already exists: {record}. Retry with a new move.")
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        for name in roots:
            origin, target = repository / name, workspace / name
            if origin.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(origin, target)
        if _inventory(repository, roots) != source:
            raise ValueError("Source changed during the copy.")
        if _inventory(workspace, roots) != source:
            raise ValueError("Staged copy does not match the source.")
        staged = _inventory(workspace)
    except (ValueError, OSError) as exc:
        raise ValueError(
            f"Move preparation failed: {exc} Source remains intact at {repository}. "
            f"Inspect the staged copy at {workspace}, then choose an empty destination to retry."
        ) from exc
    return PreparedMove(
        repository, workspace, project_id, active,
        tuple(entry.path for entry in source if entry.digest is not None), roots, source, staged,
    )


def _relocate_workflow_dirs(prepared: PreparedMove) -> None:
    """Relocate only resource directories in the verified copy."""
    directories = {entry.path for entry in prepared._source if entry.digest is None}
    runs = prepared.workspace_root / ".specify/workflows/runs"
    for path in runs.glob("*/state.json"):
        data = read_json(path)
        if data.get("installed_registry_root") is not None:
            continue
        value = data.get("workflow_dir")
        if not isinstance(value, str) or not value:
            continue
        source = Path(value)
        if not source.is_absolute():
            source = confined(prepared.repository_root, source)
        relocated = str(source)
        if source.is_relative_to(prepared.repository_root):
            relative = source.relative_to(prepared.repository_root).as_posix()
            if relative in directories:
                relocated = relative
        if relocated != value:
            data["workflow_dir"] = relocated
            atomic_json(path, data)


def _restore_metadata(prepared: PreparedMove, contents: dict[str, bytes], protected: set[Path]) -> None:
    """Restore staged metadata that command refresh can change."""
    workspace = prepared.workspace_root
    expected = {entry.path: entry for entry in prepared._source if entry.path.startswith(".specify/") or entry.path == ".specify"}
    for entry in reversed(_inventory(workspace, (".specify",))):
        path = workspace / entry.path
        if entry.path not in expected and path not in protected:
            path.rmdir() if entry.digest is None else path.unlink()
    for entry in expected.values():
        path = workspace / entry.path
        if entry.digest is None:
            path.mkdir(exist_ok=True)
        elif not path.exists() or path.read_bytes() != contents[entry.path]:
            path.write_bytes(contents[entry.path])
        path.chmod(entry.mode)


def _restore_sources(prepared: PreparedMove, recovery: Path, moved: list[str], contents: dict[str, bytes]) -> None:
    """Fill partial recovery copies, then restore without replacing new files."""
    for entry in prepared._source:
        if not any(Path(entry.path).is_relative_to(name) for name in moved):
            continue
        target = recovery / entry.path
        if target.exists() or target.is_symlink():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.digest is None:
            target.mkdir()
        elif entry.path in contents:
            target.write_bytes(contents[entry.path])
        else:
            source = prepared.workspace_root / entry.path
            with source.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != entry.digest:
                    raise ValueError(f"Recovery copy changed: {source}. Preserve both copies for manual recovery.")
            shutil.copy2(source, target)
        target.chmod(entry.mode)

    def restore(origin: Path, target: Path) -> None:
        if target.is_symlink():
            raise ValueError(f"Recovery path is now a symlink: {target}. Original files remain at {recovery}.")
        if not target.exists():
            origin.rename(target)
        elif origin.is_dir() and target.is_dir():
            mode = stat.S_IMODE(origin.stat().st_mode)
            for child in origin.iterdir():
                restore(child, target / child.name)
            target.chmod(mode)
            origin.rmdir()
        else:
            raise ValueError(f"Recovery will not overwrite a new file: {target}. Original files remain at {recovery}.")

    for name in moved:
        target = prepared.repository_root / name
        if confined(prepared.repository_root, name) != target:
            raise ValueError(f"Recovery path changed: {target}. Original files remain at {recovery}.")
        restore(recovery / name, target)
    for path in sorted(recovery.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        path.rmdir()
    recovery.rmdir()


def commit_move(prepared: PreparedMove) -> Project:
    """Recheck the verified stage and make a recoverable external-storage cutover."""
    from . import _move_commands

    repository, workspace = prepared.repository_root, prepared.workspace_root
    roots, active = _local_roots(repository)
    if roots != prepared._roots or active != prepared.active_feature or _inventory(repository, roots) != prepared._source:
        raise ValueError(f"Source changed after preparation: {repository}. Keep the staged copy and prepare a new move.")
    if _inventory(workspace) != prepared._stage:
        raise ValueError(f"Staged copy changed after preparation: {workspace}. Keep both copies and prepare a new move.")
    record = project_record_path(prepared.project_id)
    if record.exists() or record.is_symlink():
        raise ValueError(f"Project record now exists: {record}. Keep both copies and prepare a new move.")

    native = {}
    absent_parents = set()
    parent = record.parent
    while not parent.exists():
        absent_parents.add(parent)
        parent = parent.parent
    for path in _move_commands.native_paths(repository):
        path = Path(path)
        if not path.is_absolute():
            path = repository / path
        native_root = _move_commands.native_path_root(repository, path)
        if confined(native_root, path) != path or path.is_symlink():
            raise ValueError(f"Native command path contains a symlink: {path}")
        if path == repository or path.is_relative_to(repository / ".git"):
            raise ValueError(f"Native command path overlaps repository state: {path}")
        if any(path.is_relative_to(repository / name) for name in roots):
            raise ValueError(f"Native command path overlaps moved artifacts: {path}")
        native[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        parent = path.parent
        while not parent.exists():
            absent_parents.add(parent)
            parent = parent.parent
    # ponytail: O(metadata size) rollback memory. Use a disk journal for large extension packages.
    contents = {
        entry.path: (workspace / entry.path).read_bytes()
        for entry in prepared._source
        if entry.path.startswith(".specify/") and entry.digest is not None
    }
    states = (
        (workspace / ".specify/workspace.json", {"schema_version": 1, "project_id": prepared.project_id}),
        (record, {"schema_version": 1, "workspace": str(workspace), "active_feature": active}),
        (repository / ".specify/project.json", {
            "schema_version": 1, "project_id": prepared.project_id, "storage": "external",
        }),
    )
    recovery = Path(tempfile.mkdtemp(prefix=".specify-move-", dir=repository))
    moved = []
    attempted = []
    refreshed = False
    try:
        _relocate_workflow_dirs(prepared)
        for name in roots:
            origin = repository / name
            if origin.exists():
                target = recovery / name
                target.parent.mkdir(parents=True, exist_ok=True)
                origin.rename(target)
                moved.append(name)
        for path, data in states:
            attempted.append((path, data))
            atomic_json(path, data)
        pointer = workspace / ".specify/feature.json"
        if pointer.exists():
            pointer.unlink()
        if workspace_root_for(repository) != workspace:
            raise ValueError("The project does not resolve to the staged workspace.")
        refreshed = True
        _move_commands.refresh_commands(repository)
        # Source originals remain recoverable until every cutover step succeeds.
        shutil.rmtree(recovery)
    except Exception as exc:
        failures = []
        protected = set()
        for path, data in reversed(attempted):
            try:
                if path.exists() or path.is_symlink():
                    if path.is_symlink() or read_json(path) != data:
                        raise ValueError(f"Recovery will not remove changed project state: {path}")
                    path.unlink()
            except (ValueError, OSError) as rollback_error:
                failures.append(str(rollback_error))
                protected.add(path)
        if refreshed:
            for path, original in native.items():
                try:
                    native_root = _move_commands.native_path_root(repository, path)
                    if path.is_symlink() or confined(native_root, path) != path:
                        raise ValueError(f"Recovery will not replace a symlink: {path}")
                    if original is None:
                        if path.exists():
                            path.unlink()
                    else:
                        data, mode = original
                        if not path.exists() or path.read_bytes() != data:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(data)
                        path.chmod(mode)
                except (ValueError, OSError) as rollback_error:
                    failures.append(str(rollback_error))
        for parent in sorted(absent_parents, key=lambda path: len(path.parts), reverse=True):
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError as rollback_error:
                failures.append(str(rollback_error))
        try:
            _restore_metadata(prepared, contents, protected)
        except (ValueError, OSError) as rollback_error:
            failures.append(str(rollback_error))
        try:
            _restore_sources(prepared, recovery, moved, contents)
        except (ValueError, OSError) as rollback_error:
            failures.append(str(rollback_error))
        detail = (
            f" Recovery needs attention at {recovery}: {' '.join(failures)}"
            if failures else f" Source remains usable at {repository}."
        )
        raise ValueError(
            f"Move failed: {exc}.{detail} Staged copy remains at {workspace}. "
            "Inspect both copies, then choose an empty destination to retry."
        ) from exc
    return Project(repository, workspace, prepared.project_id, workspace / active if active else None)
