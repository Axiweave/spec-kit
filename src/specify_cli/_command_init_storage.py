"""Validate and claim storage for project initialization."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from .workspace import Project, _load_project, atomic_json, project_record_path, read_json


def _validate_directory(path: Path) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError(f"Workspace path must not contain a symlink: {path}")
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"Workspace path is not a directory: {path}")


def select_storage(
    repository: Path,
    storage: str | None,
    workspace: Path | None,
    *,
    storage_root: Path | None = None,
) -> Project:
    """Resolve storage without creating or changing files."""
    if storage not in (None, "local", "external"):
        raise ValueError("--storage must be local or external.")
    if workspace is not None and storage == "local":
        raise ValueError("--workspace conflicts with --storage local.")
    repository = repository.resolve()
    metadata = repository / ".specify"
    locator = metadata / "project.json"
    if locator.exists() or locator.is_symlink():
        project = _load_project(repository)
        if storage == "local":
            raise ValueError("This project uses external storage. Use specify project move to change storage.")
        if workspace is not None and workspace.expanduser().resolve() != project.workspace_root:
            raise ValueError("--workspace cannot replace an existing project workspace. Use specify project link or move.")
        if repository.is_relative_to(project.workspace_root) or project.workspace_root.is_relative_to(repository):
            raise ValueError(f"Workspace must not overlap the repository: {project.workspace_root}")
        _validate_directory(project.workspace_root / ".specify")
        for path in (project.workspace_root / ".specify").rglob("*"):
            if path.is_symlink():
                raise ValueError(f"Workspace assets must not contain a symlink: {path}")
        return project
    if workspace is None and storage != "external":
        return Project(repository, repository, None, None)
    if metadata.is_symlink() or (metadata.exists() and (not metadata.is_dir() or any(metadata.iterdir()))):
        raise ValueError("The repository has local Spec Kit assets. Use specify project move instead of init.")
    project_id = str(uuid4())
    if workspace is None:
        root = storage_root.expanduser() if storage_root is not None else Path.home() / "speckit-specs"
        workspace = root / repository.name
        while workspace.exists() or workspace.is_symlink():
            workspace = root / f"{repository.name}-{uuid4().hex[:8]}"
    workspace = workspace.expanduser().absolute()
    if ".." in workspace.parts:
        raise ValueError(f"Workspace path must not contain traversal: {workspace}")
    workspace = workspace.resolve()
    _validate_directory(workspace)
    if repository.is_relative_to(workspace) or workspace.is_relative_to(repository):
        raise ValueError(f"Workspace must not overlap the repository: {workspace}")
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError(f"Workspace is occupied. Choose an empty directory: {workspace}")
    _validate_directory(project_record_path(project_id).parent)
    return Project(repository, workspace, project_id, None)


@contextmanager
def claim_storage(project: Project) -> Iterator[None]:
    """Claim external storage and remove only newly owned state after failure."""
    locator = project.repository_root / ".specify/project.json"
    if not project.project_id:
        yield
        return
    if locator.exists():
        if read_json(locator).get("project_id") != project.project_id:
            raise ValueError(f"Another project owns the repository: {project.repository_root}")
        yield
        return
    workspace = project.workspace_root
    _validate_directory(workspace)
    _validate_directory(locator.parent)
    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError(f"Workspace is occupied. Choose an empty directory: {workspace}")
    record = project_record_path(project.project_id)
    claimed: list[tuple[Path, dict]] = []
    directories: list[Path] = []
    try:
        if not workspace.exists():
            workspace.mkdir(parents=True)
            directories.append(workspace)
        metadata = workspace / ".specify"
        metadata.mkdir()
        directories.append(metadata)
        if any(path != metadata for path in workspace.iterdir()):
            raise ValueError(f"Workspace changed before the claim: {workspace}")
        if not locator.parent.exists():
            locator.parent.mkdir(parents=True)
            directories.append(locator.parent)
        for path, data in (
            (metadata / "workspace.json", {
                "schema_version": 1, "project_id": project.project_id,
            }),
            (record, {
                "schema_version": 1, "workspace": str(workspace), "active_feature": None,
            }),
            (locator, {
                "schema_version": 1, "project_id": project.project_id, "storage": "external",
            }),
        ):
            atomic_json(path, data, exclusive=True)
            claimed.append((path, data))
        yield
    except BaseException as exc:
        # A caller that already made history durable (see WorkspaceGitError.committed)
        # must keep its claim: unclaiming would orphan a workspace with real commits.
        if not getattr(exc, "committed", False):
            for path, data in reversed(claimed):
                try:
                    if read_json(path) == data:
                        path.unlink()
                except (OSError, ValueError) as cleanup_error:
                    exc.add_note(f"Could not remove claimed metadata at {path}: {cleanup_error}")
            # Keep partial assets and unrelated content. Remove only empty directories.
            for directory in reversed(directories):
                with suppress(OSError):
                    directory.rmdir()
        raise
