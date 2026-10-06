"""Stage and commit a verified move from local project storage."""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from uuid import uuid4

from specify_cli.integrations.manifest import append_gitignore
from specify_cli.workspace import (
    Project, atomic_json, checkout_record_path, confined, is_private, read_json, workspace_root_for,
)
from specify_cli.workspace_git import WorkspaceGitError, initialize_workspace_git, preflight_workspace_git, run_git


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
    no_workspace_git: bool
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
        raise ValueError(
            f"Project already selects external storage: {repository}. "
            "Use specify project link to relink it, or specify project unlink to detach it."
        )
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


def prepare_move(repository: Path, destination: Path, *, no_workspace_git: bool = False) -> PreparedMove:
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
    if not no_workspace_git:
        preflight_workspace_git(workspace)
    roots, active = _local_roots(repository)
    source = _inventory(repository, roots)
    project_id = str(uuid4())
    record = checkout_record_path(repository)
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
        tuple(entry.path for entry in source if entry.digest is not None),
        no_workspace_git, roots, source, staged,
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


def commit_move(prepared: PreparedMove, *, report: dict[str, str] | None = None) -> Project:
    """Recheck the verified stage and make a recoverable external-storage cutover."""
    from . import _command_move_managed

    repository, workspace = prepared.repository_root, prepared.workspace_root
    roots, active = _local_roots(repository)
    if roots != prepared._roots or active != prepared.active_feature or _inventory(repository, roots) != prepared._source:
        raise ValueError(f"Source changed after preparation: {repository}. Keep the staged copy and prepare a new move.")
    if _inventory(workspace) != prepared._stage:
        raise ValueError(f"Staged copy changed after preparation: {workspace}. Keep both copies and prepare a new move.")
    record = checkout_record_path(repository)
    if record.exists() or record.is_symlink():
        raise ValueError(f"Project record now exists: {record}. Keep both copies and prepare a new move.")

    native = {}
    absent_parents = set()
    for path in _command_move_managed.native_paths(repository):
        path = Path(path)
        if not path.is_absolute():
            path = repository / path
        native_root = _command_move_managed.native_path_root(repository, path)
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
    committed = False
    commit_id = None
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
        _command_move_managed.refresh_commands(repository)
        append_gitignore(repository, ".specify/checkout.json")
        if not prepared.no_workspace_git:
            commit_id = initialize_workspace_git(workspace)
            committed = True
        # Source originals remain recoverable until every cutover step succeeds.
        shutil.rmtree(recovery)
    except (Exception, KeyboardInterrupt) as exc:
        committed = committed or (isinstance(exc, WorkspaceGitError) and exc.committed)
        failures = []
        protected = set()
        try:
            for path, data in reversed(attempted):
                if committed and path.is_relative_to(workspace):
                    continue
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
                        native_root = _command_move_managed.native_path_root(repository, path)
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
            if not committed:
                try:
                    _restore_metadata(prepared, contents, protected)
                except (ValueError, OSError) as rollback_error:
                    failures.append(str(rollback_error))
            try:
                _restore_sources(prepared, recovery, moved, contents)
            except (ValueError, OSError) as rollback_error:
                failures.append(str(rollback_error))
        except KeyboardInterrupt:
            # Renames keep each original whole: it is either back in the repository or still in recovery.
            raise KeyboardInterrupt(
                f"Rollback stopped by a second interrupt. Each original file is in {repository} or in {recovery}. "
                f"Staged copy remains at {workspace}."
            ) from exc
        detail = (
            f" Recovery needs attention at {recovery}: {' '.join(failures)}"
            if failures else f" Source remains usable at {repository}."
        )
        error, reason = (KeyboardInterrupt, "interrupted") if isinstance(exc, KeyboardInterrupt) else (ValueError, exc)
        if committed:
            raise error(
                f"Move failed after the workspace commit: {reason}.{detail} "
                f"The external workspace keeps its initial commit and identity at {workspace}. "
                "Inspect both locations, then use specify project link to relink if needed."
            ) from exc
        raise error(
            f"Move failed: {reason}.{detail} Staged copy remains at {workspace}. "
            "Inspect both copies, then choose an empty destination to retry."
        ) from exc
    if report is not None and commit_id is not None:
        report["commit_id"] = commit_id
    return Project(repository, workspace, prepared.project_id, workspace / active if active else None)


# Workspace state that a local project does not take (research D9). `*` also matches `/`.
_LEFT_OUT = (".specify/workspace.json", ".specify/naming-recovery-*", "specs/.merge-specs-recovery-*")


def _left_out(relative: str) -> bool:
    return any(fnmatchcase(relative, pattern) for pattern in _LEFT_OUT)


def unlink_checkout(repository: Path) -> tuple[tuple[str, ...], bool]:
    """Remove this checkout's record and locator. Never read or change the workspace.

    Return the removed relative paths and whether Git tracks the locator.
    """
    metadata = repository / ".specify"
    locator = metadata / "project.json"
    if metadata.is_symlink():
        raise ValueError(f"Project metadata must not be a symlink: {metadata}")
    if not locator.exists() and not locator.is_symlink():
        raise ValueError(f"No project locator in {repository}. Unlink applies only to a checkout with external storage.")
    if read_json(locator).get("storage") != "external":
        raise ValueError(f"The project locator does not select external storage: {locator}")
    try:
        # Git exits 128 outside a work tree. Without Git, nothing tracks the locator.
        tracked = bool(run_git(repository, "ls-files", "--", ".specify/project.json", allowed_codes=(0, 128)))
    except OSError:
        tracked = False
    removed = []
    for path in (checkout_record_path(repository), locator):
        if path.exists() or path.is_symlink():
            path.unlink()
            removed.append(path.relative_to(repository).as_posix())
    return tuple(removed), tracked


@dataclass(frozen=True)
class PreparedLocal:
    repository_root: Path
    workspace_root: Path
    active_feature: str | None
    files: tuple[str, ...]
    _roots: tuple[str, ...] = field(repr=False)
    _staging: Path = field(repr=False)


def prepare_local(repository: Path) -> PreparedLocal:
    """Copy and verify the workspace into a staging folder in the repository. Change nothing else."""
    repository = repository.expanduser().resolve()
    workspace = workspace_root_for(repository)
    if workspace == repository:
        raise ValueError(f"Project already uses local storage: {repository}")
    metadata = repository / ".specify"
    allowed = {metadata / "project.json", checkout_record_path(repository)}
    manifests = []
    if is_private(workspace):
        manifests = sorted((metadata / "integrations").glob("*.manifest.json"))
        allowed |= {metadata / "integrations", *manifests}
    extra = sorted(set(metadata.rglob("*")) - allowed)
    if extra:
        raise ValueError(f"The checkout has other Spec Kit files: {extra[0]}. Move them out of {metadata}, then retry.")
    record_path = checkout_record_path(repository)
    active = read_json(record_path).get("active_feature")
    roots = [".specify", "specs"]
    if active is not None:
        raw = Path(active) if isinstance(active, str) and active else None
        if raw is None or raw.is_absolute() or ".git" in raw.parts:
            raise ValueError(f"Active feature must be a workspace-relative path: {record_path}")
        selected = confined(workspace, raw)
        if not selected.is_dir():
            raise ValueError(f"Active feature is missing: {selected}. Select an existing feature, then retry.")
        active = selected.relative_to(workspace).as_posix()
        if active.split("/")[0] not in (".specify", "specs"):
            roots.append(active)
    for name in roots[1:]:
        target = repository / name
        if target.exists() or target.is_symlink():
            raise ValueError(f"The repository already has {target}. Move it out of the repository, then retry.")
        if confined(repository, name) != target:
            raise ValueError(f"Local path contains a symlink: {target}")
    staging = Path(tempfile.mkdtemp(prefix=".specify-convert-", dir=repository))
    stage = staging / "stage"
    try:
        source = tuple(entry for entry in _inventory(workspace, tuple(roots)) if not _left_out(entry.path))
        for name in roots:
            if (workspace / name).exists():
                (stage / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(workspace / name, stage / name, ignore=lambda directory, names: [
                    child for child in names if _left_out(Path(directory, child).relative_to(workspace).as_posix())
                ])
        if _inventory(stage, tuple(roots)) != source:
            raise ValueError("The staged copy does not match the workspace.")
        # Private checkouts keep their own manifest. Local storage has one manifest per integration.
        for path in manifests:
            own = read_json(path)
            target = stage / ".specify/integrations" / path.name
            shared = read_json(target) if target.is_file() else {}
            merged = {
                **own, **shared,
                "files": {**shared.get("files", {}), **own.get("files", {})},
                "recovered_files": sorted({*shared.get("recovered_files", []), *own.get("recovered_files", [])}),
            }
            merged.pop("excluded", None)
            if not merged["recovered_files"]:
                del merged["recovered_files"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    except (Exception, KeyboardInterrupt) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, KeyboardInterrupt):
            raise KeyboardInterrupt("Conversion interrupted. The checkout and the workspace were not changed.") from exc
        raise ValueError(f"Conversion preparation failed: {exc} The checkout and the workspace were not changed.") from exc
    return PreparedLocal(
        repository, workspace, active, tuple(entry.path for entry in source if entry.digest is not None),
        tuple(roots), staging,
    )


def commit_local(prepared: PreparedLocal) -> tuple[int, tuple[Path, ...]]:
    """Replace the external storage link with the staged copy, then verify it. Never write to the workspace.

    Any error or interrupt rolls back the checkout. Return the number of verified
    files and the shared commands that keep the workspace note.
    """
    from . import _command_move_managed

    repository, workspace, staging = prepared.repository_root, prepared.workspace_root, prepared._staging
    stage, saved = staging / "stage", staging / ".specify"
    native: dict[Path, bytes] = {}
    placed: list[str] = []
    created: list[Path] = []
    try:
        native = {
            path: path.read_bytes() for path in _command_move_managed.native_paths(repository)
            if path.is_relative_to(repository) and path.is_file()
        }
        (repository / ".specify").rename(saved)
        for name in prepared._roots:
            if not (stage / name).exists():
                continue
            parent = (repository / name).parent
            while not parent.exists():
                created.append(parent)
                parent = parent.parent
            (repository / name).parent.mkdir(parents=True, exist_ok=True)
            placed.append(name)
            (stage / name).rename(repository / name)
        if prepared.active_feature:
            atomic_json(repository / ".specify/feature.json", {"feature_directory": prepared.active_feature})
        shared = _command_move_managed.remove_workspace_notes(repository)
        compared = tuple(name for name in prepared._roots if name != ".specify")
        local = _inventory(repository, compared)
        if local != tuple(entry for entry in _inventory(workspace, compared) if not _left_out(entry.path)):
            raise ValueError("The local copy does not match the workspace.")
        shutil.rmtree(staging)
    except (Exception, KeyboardInterrupt) as exc:
        failures = []
        try:
            for path, data in native.items():
                try:
                    if path.is_symlink():
                        raise ValueError(f"Recovery will not replace a symlink: {path}")
                    if not path.is_file() or path.read_bytes() != data:
                        path.write_bytes(data)
                except (ValueError, OSError) as rollback_error:
                    failures.append(str(rollback_error))
            for name in reversed(placed):
                try:
                    if not (stage / name).exists():
                        (repository / name).rename(stage / name)
                except OSError as rollback_error:
                    failures.append(str(rollback_error))
            try:
                if saved.exists():
                    saved.rename(repository / ".specify")
            except OSError as rollback_error:
                failures.append(str(rollback_error))
            for parent in created:
                try:
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                except OSError as rollback_error:
                    failures.append(str(rollback_error))
            if not failures:
                shutil.rmtree(staging, ignore_errors=True)
        except KeyboardInterrupt:
            raise KeyboardInterrupt(
                f"Rollback stopped by a second interrupt. The checkout files are in {repository} or in {staging}. "
                "The workspace was not changed."
            ) from exc
        state = (
            f" Recovery needs attention at {staging}: {' '.join(failures)}"
            if failures else f" The checkout still uses {workspace}."
        )
        error, reason = (
            (KeyboardInterrupt, "interrupted") if isinstance(exc, KeyboardInterrupt) else (ValueError, str(exc).rstrip("."))
        )
        raise error(f"Conversion to local storage failed: {reason}.{state} The workspace was not changed.") from exc
    return sum(entry.digest is not None for entry in local), shared
