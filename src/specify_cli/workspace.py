"""Repository, workspace, and active-feature paths for local and external projects."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


def user_data_dir() -> Path:
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]).expanduser() / "specify"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "specify"
    return Path.home() / ".local/share/specify"


def confined(root: Path, value: str | Path) -> Path:
    root = root.resolve()
    raw = Path(value)
    if ".." in raw.parts:
        raise ValueError(f"Path contains traversal: {value}")
    path = (raw if raw.is_absolute() else root / raw).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"Path must remain inside {root}: {value}")
    return path


def read_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"Metadata must not be a symlink: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read metadata at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Metadata must be an object: {path}")
    return data


def atomic_json(path: Path, data: dict, *, exclusive: bool = False) -> None:
    if path.is_symlink():
        raise ValueError(f"Metadata must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(name, path)
        else:
            os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def checkout_record_path(repository: Path) -> Path:
    """Return this checkout's machine record: its workspace path and active feature."""
    return repository / ".specify/checkout.json"


def _version(data: dict, path: Path) -> None:
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError(f"Unsupported metadata version: {path}")


def verify_workspace(workspace: Path, project_id: str) -> Path:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace is unavailable: {workspace}")
    identity_path = confined(workspace, ".specify/workspace.json")
    identity = read_json(identity_path)
    _version(identity, identity_path)
    if identity.get("project_id") != project_id:
        raise ValueError(f"Workspace belongs to another project: {workspace}")
    _private_flag(identity, identity_path)
    return workspace


def _private_flag(identity: dict, path: Path) -> bool:
    value = identity.get("private", False)
    if type(value) is not bool:
        raise ValueError(f"Invalid private flag in {path}: use true or false.")
    return value


def is_private(workspace: Path) -> bool:
    """Return True when the external workspace at *workspace* uses private mode."""
    identity_path = workspace / ".specify/workspace.json"
    if not identity_path.exists() and not identity_path.is_symlink():
        return False
    return _private_flag(read_json(identity_path), identity_path)


@dataclass(frozen=True)
class Project:
    repository_root: Path
    workspace_root: Path
    project_id: str | None
    feature_dir: Path | None

    @property
    def storage(self) -> str:
        return "external" if self.project_id else "local"


class ProjectNotFoundError(ValueError):
    """No Spec Kit repository exists at or above the discovery path."""


def find_repository(start: Path | None = None) -> Path:
    override = os.environ.get("SPECIFY_INIT_DIR")
    current = (
        Path(override).expanduser().resolve()
        if override
        else start.resolve() if start is not None else Path.cwd()
    )
    if override:
        if not current.is_dir() or not (current / ".specify").is_dir():
            raise ValueError(f"SPECIFY_INIT_DIR is not a Spec Kit project: {current}")
    for candidate in ((current,) if override else (current, *current.parents)):
        if (candidate / ".specify").is_dir():
            identity = candidate / ".specify/workspace.json"
            if identity.exists() or identity.is_symlink():
                raise ValueError(
                    f"External workspace is not a code repository: {candidate}. "
                    "Run from the code repository or set SPECIFY_INIT_DIR to its path."
                )
            return candidate
    raise ProjectNotFoundError(f"No Spec Kit project found from {current}")


def resolve_project(start: Path | None = None, *, select_feature: bool = True) -> Project:
    """Resolve both roots and this call's feature: the env override, else the saved one in automatic mode."""
    return _load_project(find_repository(start), select_feature=select_feature)


def workspace_root_for(repository: Path) -> Path:
    """Resolve assets for an explicit repository without environment overrides."""
    locator = repository / ".specify/project.json"
    if not locator.exists() and not locator.is_symlink():
        return repository
    return _load_project(repository.resolve(), select_feature=False).workspace_root


def feature_selection_mode(workspace_root: Path) -> str:
    """Return the project's saved-feature policy: "context" (default) or "automatic"."""
    path = workspace_root / ".specify/init-options.json"
    if not path.exists() and not path.is_symlink():
        return "context"
    mode = read_json(path).get("feature_selection", "context")
    if mode not in ("context", "automatic"):
        raise ValueError(f"feature_selection must be context or automatic: {path}")
    return mode


def _load_project(repository: Path, *, select_feature: bool = True) -> Project:
    locator_path = repository / ".specify/project.json"
    workspace, project_id, saved = repository, None, None
    if locator_path.exists() or locator_path.is_symlink():
        if (repository / ".specify").is_symlink():
            raise ValueError(f"Project metadata must not be a symlink: {repository / '.specify'}")
        locator = read_json(locator_path)
        _version(locator, locator_path)
        if locator.get("storage") != "external":
            raise ValueError(f"Invalid storage mode: {locator_path}")
        project_id = locator.get("project_id")
        if not isinstance(project_id, str):
            raise ValueError(f"Invalid project ID: {locator_path}")
        record_path = checkout_record_path(repository)
        if not record_path.is_file():
            raise ValueError(f"Missing workspace mapping for {repository}. Use specify project link PATH.")
        record = read_json(record_path)
        _version(record, record_path)
        value = record.get("workspace")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"Workspace must be an absolute path: {record_path}")
        try:
            workspace = verify_workspace(Path(value), project_id)
        except ValueError as exc:
            raise ValueError(
                f"{exc}. Run specify project unlink to detach this checkout, or specify project link <backup>."
            ) from exc
        if select_feature and feature_selection_mode(workspace) == "automatic":
            saved = record.get("active_feature")
            if saved is not None and (not isinstance(saved, str) or not saved or Path(saved).is_absolute()):
                raise ValueError(f"Active feature must be a workspace-relative path: {record_path}")
    elif select_feature and feature_selection_mode(repository) == "automatic":
        pointer = repository / ".specify/feature.json"
        if pointer.is_file():
            saved = read_json(pointer).get("feature_directory")
            if not isinstance(saved, str) or not saved:
                raise ValueError(f"Missing feature_directory: {pointer}")
    selection = (os.environ.get("SPECIFY_FEATURE_DIRECTORY") or saved) if select_feature else None
    feature = None
    if selection:
        if project_id:
            feature = confined(workspace, selection)
        else:
            raw = Path(selection)
            feature = raw if raw.is_absolute() else repository / raw
    return Project(repository, workspace, project_id, feature)


def save_active_feature(project: Project, feature: Path) -> None:
    if project.project_id:
        selected = confined(project.workspace_root, feature)
        record_path = checkout_record_path(project.repository_root)
        record = read_json(record_path)
        record["active_feature"] = selected.relative_to(project.workspace_root).as_posix()
        atomic_json(record_path, record)
    else:
        raw = feature if feature.is_absolute() else project.repository_root / feature
        value = str(raw)
        if raw.is_relative_to(project.repository_root):
            value = raw.relative_to(project.repository_root).as_posix()
        atomic_json(project.repository_root / ".specify/feature.json", {"feature_directory": value})
