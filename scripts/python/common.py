"""Shared helpers for Spec Kit Python scripts."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from uuid import UUID
from dataclasses import dataclass
from pathlib import Path


def _trim_trailing_separators(value: Path) -> str:
    text = str(value)
    while len(text) > 1 and text.endswith((os.sep, "/")):
        text = text[:-1]
    return text


def find_specify_root(start_dir: Path | None = None) -> Path | None:
    current = (start_dir or Path.cwd()).resolve()
    while True:
        if (current / ".specify").is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def resolve_specify_init_dir() -> Path:
    raw = os.environ.get("SPECIFY_INIT_DIR", "")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        init_root = candidate.resolve(strict=True)
    except OSError:
        print(
            f"ERROR: SPECIFY_INIT_DIR does not point to an existing directory: {raw}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not init_root.is_dir():
        print(
            f"ERROR: SPECIFY_INIT_DIR does not point to an existing directory: {raw}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if not (init_root / ".specify").is_dir():
        print(
            "ERROR: SPECIFY_INIT_DIR is not a Spec Kit project "
            f"(no .specify/ directory): {init_root}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if (init_root / ".specify" / "workspace.json").exists():
        _storage_error(f"SPECIFY_INIT_DIR selects a workspace, not a code repository: {init_root}")
    return init_root


def get_repo_root(script_file: Path | None = None) -> Path:
    if os.environ.get("SPECIFY_INIT_DIR"):
        return resolve_specify_init_dir()

    specify_root = find_specify_root()
    if specify_root is not None:
        if (specify_root / ".specify" / "workspace.json").exists():
            _storage_error(f"Run this command from the code repository, not the workspace: {specify_root}")
        return specify_root

    if script_file is not None:
        script_root = find_specify_root(script_file.resolve().parent)
        if script_root is not None:
            if (script_root / ".specify" / "workspace.json").exists():
                _storage_error(
                    f"Cannot discover the code repository for workspace {script_root}. "
                    "Run from the repository or set SPECIFY_INIT_DIR."
                )
            return script_root

        # Installed scripts live at .specify/scripts/python/<script>.py.
        return script_file.resolve().parents[3]
    return Path.cwd().resolve()


def _storage_error(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def confined_workspace_path(workspace: Path, value: str | Path) -> Path:
    """Reject traversal and paths that resolve outside the selected workspace."""
    path = Path(value)
    if ".." in path.parts:
        _storage_error(f"Path contains traversal in workspace {workspace}: {value}")
    if not path.is_absolute():
        path = workspace / path
    try:
        resolved = path.resolve()
        resolved.relative_to(workspace.resolve())
    except (OSError, RuntimeError, ValueError):
        _storage_error(f"Path escapes workspace {workspace}: {value}")
    return resolved


def _read_storage_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        _storage_error(f"Missing or unsafe storage record: {path}. Relink the project workspace.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _storage_error(f"Cannot read storage record {path}: {exc}. Relink the project workspace.")
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        _storage_error(f"Invalid storage schema in {path}. Relink the project workspace.")
    return data


def _external_project_record(repo_root: Path) -> tuple[Path, dict] | None:
    locator = repo_root / ".specify" / "project.json"
    if not os.path.lexists(locator):
        return None
    confined_workspace_path(repo_root, locator)
    project = _read_storage_json(locator)
    project_id = project.get("project_id")
    try:
        if not isinstance(project_id, str) or str(UUID(project_id)) != project_id.lower():
            raise ValueError
    except (ValueError, AttributeError):
        _storage_error(f"Invalid project ID in {locator}. Relink the project workspace.")
    if project.get("storage") != "external":
        _storage_error(f"Invalid storage mode in {locator}. Relink the project workspace.")
    if os.environ.get("XDG_DATA_HOME"):
        data_home = Path(os.environ["XDG_DATA_HOME"])
    elif os.name == "nt":
        data_home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        data_home = Path.home() / ".local" / "share"
    record_path = data_home / "specify" / "projects" / f"{project_id}.json"
    confined_workspace_path(data_home, record_path)
    record = _read_storage_json(record_path)
    raw_workspace = record.get("workspace")
    if not isinstance(raw_workspace, str) or not Path(raw_workspace).is_absolute():
        _storage_error(f"Invalid workspace path in {record_path}. Relink the project workspace.")
    workspace = Path(raw_workspace)
    if not workspace.is_dir() or not os.access(workspace, os.R_OK | os.W_OK | os.X_OK):
        _storage_error(f"Workspace is missing or inaccessible: {workspace}. Relink the project workspace.")
    workspace = workspace.resolve()
    identity_path = workspace / ".specify" / "workspace.json"
    confined_workspace_path(workspace, identity_path)
    identity = _read_storage_json(identity_path)
    if identity.get("project_id") != project_id:
        _storage_error(f"Workspace belongs to another project: {workspace}. Relink the project workspace.")
    active = record.get("active_feature")
    if active is not None and (not isinstance(active, str) or not active or Path(active).is_absolute()):
        _storage_error(f"Invalid active feature in {record_path}. Select a workspace-relative feature.")
    return record_path, record


def get_workspace_root(repo_root: Path, *, report: bool = False) -> Path:
    record = _external_project_record(repo_root)
    if record is None:
        return repo_root
    workspace = Path(record[1]["workspace"]).resolve()
    if report:
        print(f"[specify] Workspace: {workspace}", file=sys.stderr)
    return workspace


FEATURE_SELECTION_MODES = ("context", "automatic")


def feature_selection_mode(workspace_root: Path) -> str:
    """Return the project policy for saved features: "context" (default) or "automatic"."""
    path = workspace_root / ".specify" / "init-options.json"
    if not os.path.lexists(path):
        return "context"
    try:
        if path.is_symlink():
            raise ValueError("Project choices must not be a symlink.")
        if (workspace_root / ".specify" / "workspace.json").exists():
            confined_workspace_path(workspace_root, path)
        options = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(options, dict):
            raise ValueError("Project choices must be a JSON object.")
    except (OSError, UnicodeError, ValueError) as exc:
        _storage_error(f"Cannot read project choices at {path}: {exc}")
    mode = options.get("feature_selection", "context")
    if mode not in FEATURE_SELECTION_MODES:
        _storage_error(f"feature_selection must be context or automatic: {path}")
    return mode


def may_persist_feature_selection(mode: str, *, no_persist: bool = False) -> bool:
    """Only automatic projects save the active feature, and no-persist always wins.

    SPECIFY_FEATURE_NO_PERSIST is the environment-level equivalent of no_persist=True,
    letting an orchestrator (multi-agent runner, CI matrix) guarantee that no
    script invocation in the process tree writes the saved feature, even scripts
    that don't pass no_persist themselves (#4128).
    """
    no_persist = no_persist or os.environ.get("SPECIFY_FEATURE_NO_PERSIST", "") in ("1", "true")
    return mode == "automatic" and not no_persist


def get_current_branch() -> str:
    return os.environ.get("SPECIFY_FEATURE", "")


def read_feature_json_feature_directory(repo_root: Path) -> str:
    feature_json = repo_root / ".specify" / "feature.json"
    if not feature_json.is_file():
        return ""
    try:
        data = json.loads(feature_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    value = data.get("feature_directory") if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""


def _json_dump(data: dict[str, str]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"


def persist_feature_json(repo_root: Path, feature_dir_value: str) -> None:
    external = _external_project_record(repo_root)
    if external is not None:
        record_path, record = external
        workspace = Path(record["workspace"]).resolve()
        feature_dir = confined_workspace_path(workspace, feature_dir_value)
        value = feature_dir.relative_to(workspace).as_posix()
        if record.get("active_feature") == value:
            return
        record["active_feature"] = value
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=record_path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(_json_dump(record).encode("utf-8"))
            os.replace(temporary, record_path)
        except OSError as exc:
            _storage_error(f"Cannot save the active feature for workspace {workspace}: {exc}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return
    # Strip the repo root prefix lexically (no resolve()) to mirror the
    # Bash/PowerShell helpers: with a symlinked <repo>/specs, resolve() would
    # escape the repo and persist a machine-specific absolute path instead of
    # the relative "specs/NNN-name" the other variants store.
    value = feature_dir_value
    relative = Path(value)
    if relative.is_absolute():
        try:
            value = relative.relative_to(repo_root).as_posix()
        except ValueError:
            value = str(relative)

    current = read_feature_json_feature_directory(repo_root)
    if current == value:
        return

    specify_dir = repo_root / ".specify"
    specify_dir.mkdir(parents=True, exist_ok=True)
    (specify_dir / "feature.json").write_bytes(
        _json_dump({"feature_directory": value}).encode("utf-8")
    )


@dataclass(frozen=True)
class FeaturePaths:
    repo_root: Path
    workspace_root: Path
    current_branch: str
    feature_dir: Path
    feature_spec: Path
    impl_plan: Path
    tasks: Path
    research: Path
    data_model: Path
    quickstart: Path
    contracts_dir: Path


def get_feature_paths(
    *, no_persist: bool = False, script_file: Path | None = None
) -> FeaturePaths:
    repo_root = get_repo_root(script_file)
    external = _external_project_record(repo_root)
    workspace_root = Path(external[1]["workspace"]).resolve() if external else repo_root
    if external:
        print(f"[specify] Workspace: {workspace_root}", file=sys.stderr)
    selection_mode = feature_selection_mode(workspace_root)
    current_branch = get_current_branch()

    feature_dir_raw = os.environ.get("SPECIFY_FEATURE_DIRECTORY", "")
    if feature_dir_raw:
        if external:
            feature_dir = confined_workspace_path(workspace_root, feature_dir_raw)
        else:
            feature_dir = Path(feature_dir_raw)
            if not feature_dir.is_absolute():
                feature_dir = repo_root / feature_dir
    elif selection_mode != "automatic":
        # Context projects never read the saved feature, so every command names its own.
        print(
            "ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY for this command. "
            "This project uses feature_selection context, so scripts ignore the saved feature.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    elif external:
        stored = external[1].get("active_feature")
        if not stored:
            _storage_error(
                f"No active feature in workspace {workspace_root}. "
                "Set SPECIFY_FEATURE_DIRECTORY or create a feature."
            )
        feature_dir = confined_workspace_path(workspace_root, stored)
        if not feature_dir.is_dir():
            _storage_error(
                f"Saved feature does not exist: {feature_dir}. Select an existing feature."
            )
    elif (repo_root / ".specify" / "feature.json").is_file():
        stored = read_feature_json_feature_directory(repo_root)
        if not stored:
            print(
                "ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY "
                "or ensure .specify/feature.json contains feature_directory.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        feature_dir = Path(stored)
        if not feature_dir.is_absolute():
            feature_dir = repo_root / feature_dir
    else:
        print(
            "ERROR: Feature directory not found. Set SPECIFY_FEATURE_DIRECTORY "
            "or run the specify command to create .specify/feature.json.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if external:
        for name in ("spec.md", "plan.md", "tasks.md", "research.md", "data-model.md", "quickstart.md", "contracts"):
            confined_workspace_path(workspace_root, feature_dir / name)
    if feature_dir_raw and may_persist_feature_selection(selection_mode, no_persist=no_persist):
        persist_feature_json(repo_root, feature_dir_raw)

    if not current_branch:
        current_branch = Path(_trim_trailing_separators(feature_dir)).name

    return FeaturePaths(
        repo_root=repo_root,
        workspace_root=workspace_root,
        current_branch=current_branch,
        feature_dir=feature_dir,
        feature_spec=feature_dir / "spec.md",
        impl_plan=feature_dir / "plan.md",
        tasks=feature_dir / "tasks.md",
        research=feature_dir / "research.md",
        data_model=feature_dir / "data-model.md",
        quickstart=feature_dir / "quickstart.md",
        contracts_dir=feature_dir / "contracts",
    )


_SAFE_COMPONENT_PATTERN = re.compile(r"[a-z0-9-]+")


def _is_safe_component(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_COMPONENT_PATTERN.fullmatch(value) is not None
    )


def _normalize_priority(value: object) -> int:
    if isinstance(value, bool):
        return 10
    try:
        priority = int(value)
    except (TypeError, ValueError, OverflowError):
        return 10
    return priority if priority >= 1 else 10


def _sorted_preset_ids(presets_dir: Path) -> list[str]:
    registry = presets_dir / ".registry"
    if registry.is_file():
        # Invalid JSON or registry shapes fall back to the directory scan below.
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
            presets = data.get("presets", {})
            return [
                pid
                for pid, meta in sorted(
                    presets.items(),
                    key=lambda kv: (
                        _normalize_priority(kv[1].get("priority"))
                        if isinstance(kv[1], dict)
                        else 10,
                        kv[0],
                    ),
                )
                if (
                    _is_safe_component(pid)
                    and isinstance(meta, dict)
                    and bool(meta.get("enabled", True))
                )
            ]
        except Exception:
            pass
    try:
        return sorted(
            p.name
            for p in presets_dir.iterdir()
            if p.is_dir() and _is_safe_component(p.name)
        )
    except OSError:
        return []


def _sorted_extension_ids(extensions_dir: Path) -> list[str]:
    registry = extensions_dir / ".registry"
    registered_ids: set[str] = set()
    extensions: dict[object, object] = {}
    if os.path.lexists(registry):
        if not registry.is_file():
            raise TemplateResolutionError(
                f"Invalid extension registry {registry}: not a regular file"
            )
        try:
            data = json.loads(registry.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TemplateResolutionError(
                f"Failed to parse extension registry {registry}: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise TemplateResolutionError(
                f"Invalid extension registry {registry}: root must be a mapping"
            )
        raw_extensions = data.get("extensions", {})
        if not isinstance(raw_extensions, dict):
            raise TemplateResolutionError(
                f"Invalid extension registry {registry}: "
                "'extensions' must be a mapping"
            )
        extensions = raw_extensions
        registered_ids = {
            ext_id for ext_id in extensions if isinstance(ext_id, str)
        }

    ranked: list[tuple[int, str]] = []
    for ext_id, metadata in extensions.items():
        if (
            _is_safe_component(ext_id)
            and isinstance(metadata, dict)
            and bool(metadata.get("enabled", True))
        ):
            ranked.append((_normalize_priority(metadata.get("priority")), ext_id))

    try:
        ranked.extend(
            (10, path.name)
            for path in extensions_dir.iterdir()
            if (
                path.is_dir()
                and _is_safe_component(path.name)
                and path.name not in registered_ids
            )
        )
    except OSError:
        pass
    return [ext_id for _, ext_id in sorted(ranked)]


def _conventional_template(
    base_dir: Path, template_name: str, workspace: Path | None = None
) -> Path | None:
    for candidate in (
        base_dir / "templates" / f"{template_name}.md",
        base_dir / f"{template_name}.md",
    ):
        if workspace is not None:
            confined_workspace_path(workspace, candidate)
        if candidate.is_file():
            return candidate
    return None


def resolve_template(template_name: str, repo_root: Path) -> Path | None:
    """Resolve a template name to a file path using the priority stack.

    Order (mirrors resolve_template in scripts/bash/common.sh):
      1. .specify/templates/overrides/
      2. .specify/presets/<preset-id>/templates/ (sorted by .registry priority)
      3. .specify/extensions/<ext-id>/templates/ (hidden directories skipped)
      4. .specify/templates/ (core)
    """
    if not _is_safe_component(template_name):
        return None

    workspace_root = get_workspace_root(repo_root)
    workspace = workspace_root if workspace_root != repo_root else None
    repo_root = workspace_root

    base = repo_root / ".specify" / "templates"

    override = base / "overrides" / f"{template_name}.md"
    if workspace is not None:
        confined_workspace_path(workspace, override)
    if override.is_file():
        return override

    presets_dir = repo_root / ".specify" / "presets"
    if workspace is not None:
        confined_workspace_path(workspace, presets_dir / ".registry")
    if presets_dir.is_dir():
        for preset_id in _sorted_preset_ids(presets_dir):
            candidate = _conventional_template(
                presets_dir / preset_id, template_name, workspace
            )
            if candidate is not None:
                return candidate

    ext_dir = repo_root / ".specify" / "extensions"
    if workspace is not None:
        confined_workspace_path(workspace, ext_dir / ".registry")
    if ext_dir.is_dir():
        for extension_id in _sorted_extension_ids(ext_dir):
            ext = ext_dir / extension_id
            candidate = _conventional_template(ext, template_name, workspace)
            if candidate is not None:
                return candidate

    core = base / f"{template_name}.md"
    if workspace is not None:
        confined_workspace_path(workspace, core)
    if core.is_file():
        return core
    return None


class TemplateResolutionError(RuntimeError):
    """Raised when template layers exist but cannot be composed safely."""


# Mirror the canonical PresetManifest contract (see src/specify_cli/presets)
# so runtime resolution rejects the same structurally malformed manifests.
_VALID_TEMPLATE_TYPES = ("template", "command", "script")
_VALID_TEMPLATE_STRATEGIES = ("replace", "prepend", "append", "wrap")
_VALID_SCRIPT_STRATEGIES = ("replace", "wrap")


def _validate_manifest_template_entry(entry: object) -> None:
    """Validate a single manifest template entry against the canonical rules."""
    if not isinstance(entry, dict):
        raise ValueError("manifest template entries must be mappings")
    if "type" not in entry or "name" not in entry or "file" not in entry:
        raise ValueError("manifest template entry missing type, name, or file")
    for field in ("type", "name", "file"):
        if not isinstance(entry[field], str):
            raise ValueError(f"manifest template {field} must be a string")
    if entry["type"] not in _VALID_TEMPLATE_TYPES:
        raise ValueError(f"invalid manifest template type '{entry['type']}'")
    strategy = entry.get("strategy", "replace")
    if not isinstance(strategy, str):
        raise ValueError("manifest template strategy must be a string")
    strategy = strategy.lower()
    if strategy not in _VALID_TEMPLATE_STRATEGIES:
        raise ValueError(f"invalid manifest template strategy '{strategy}'")
    if entry["type"] == "script" and strategy not in _VALID_SCRIPT_STRATEGIES:
        raise ValueError(
            f"invalid manifest script strategy '{strategy}'"
        )


def _preset_template_layer(
    preset_dir: Path, template_name: str, workspace: Path | None = None
) -> tuple[Path, str] | None:
    """Return the preset template path and composition strategy."""
    manifest_path = preset_dir / "preset.yml"
    if workspace is not None:
        confined_workspace_path(workspace, manifest_path)
    conventional = _conventional_template(preset_dir, template_name, workspace)

    try:
        import yaml
    except ImportError as exc:
        if manifest_path.is_file():
            raise TemplateResolutionError(
                "PyYAML is required to resolve preset template composition"
            ) from exc
        return (conventional, "replace") if conventional is not None else None

    if manifest_path.is_file():
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be a mapping")
            if "provides" not in manifest:
                raise ValueError("manifest missing provides section")
            provides = manifest["provides"]
            if not isinstance(provides, dict):
                raise ValueError("manifest provides must be a mapping")
            if "templates" not in provides:
                raise ValueError("manifest provides missing templates")
            templates = provides["templates"]
            if not isinstance(templates, list):
                raise ValueError("manifest templates must be a list")
            if not templates:
                raise ValueError("manifest must provide at least one template")
            for entry in templates:
                _validate_manifest_template_entry(entry)
            for entry in templates:
                if (
                    entry.get("name") != template_name
                    or entry.get("type", "template") != "template"
                ):
                    continue
                file_value = entry.get("file", "")
                strategy = entry.get("strategy", "replace")
                relative = Path(file_value)
                if (
                    not relative
                    or relative.is_absolute()
                    or ".." in relative.parts
                ):
                    if workspace is not None:
                        _storage_error(f"Invalid template path in workspace {workspace}: {file_value}")
                    return None
                candidate = preset_dir / relative
                if workspace is not None:
                    confined_workspace_path(workspace, candidate)
                if not candidate.is_file():
                    return None
                return candidate, strategy.lower()
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            raise TemplateResolutionError(
                f"Failed to parse preset manifest {manifest_path}: {exc}"
            ) from exc

    return (conventional, "replace") if conventional is not None else None


def resolve_template_content(template_name: str, repo_root: Path) -> str | None:
    """Resolve and compose template content through the project layer stack."""
    if not _is_safe_component(template_name):
        return None

    workspace_root = get_workspace_root(repo_root)
    workspace = workspace_root if workspace_root != repo_root else None
    repo_root = workspace_root

    layers: list[tuple[Path, str]] = []

    def compose_from_base() -> str:
        try:
            content = layers[-1][0].read_bytes().decode("utf-8")
            for path, strategy in reversed(layers[:-1]):
                layer_content = path.read_bytes().decode("utf-8")
                if strategy == "prepend":
                    content = f"{layer_content}\n\n{content}"
                elif strategy == "append":
                    content = f"{content}\n\n{layer_content}"
                elif strategy == "wrap":
                    placeholder = "{CORE_TEMPLATE}"
                    if placeholder not in layer_content:
                        raise TemplateResolutionError(
                            f"Wrap layer {path} is missing {placeholder}"
                        )
                    content = layer_content.replace(placeholder, content)
                else:
                    raise TemplateResolutionError(
                        f"Unknown template composition strategy '{strategy}' in {path}"
                    )
        except (OSError, UnicodeError) as exc:
            raise TemplateResolutionError(
                f"Failed to read template layer for '{template_name}': {exc}"
            ) from exc
        return content

    override = (
        repo_root
        / ".specify"
        / "templates"
        / "overrides"
        / f"{template_name}.md"
    )
    if workspace is not None:
        confined_workspace_path(workspace, override)
    if override.is_file():
        layers.append((override, "replace"))
        return compose_from_base()

    presets_dir = repo_root / ".specify" / "presets"
    if workspace is not None:
        confined_workspace_path(workspace, presets_dir / ".registry")
    for preset_id in _sorted_preset_ids(presets_dir):
        layer = _preset_template_layer(presets_dir / preset_id, template_name, workspace)
        if layer is not None:
            layers.append(layer)
            if layer[1] == "replace":
                return compose_from_base()

    extensions_dir = repo_root / ".specify" / "extensions"
    if workspace is not None:
        confined_workspace_path(workspace, extensions_dir / ".registry")
    for extension_id in _sorted_extension_ids(extensions_dir):
        extension_dir = extensions_dir / extension_id
        candidate = _conventional_template(extension_dir, template_name, workspace)
        if candidate is not None:
            layers.append((candidate, "replace"))
            return compose_from_base()

    core = repo_root / ".specify" / "templates" / f"{template_name}.md"
    if workspace is not None:
        confined_workspace_path(workspace, core)
    if core.is_file():
        layers.append((core, "replace"))
        return compose_from_base()

    if not layers:
        return None

    raise TemplateResolutionError(
        f"Template '{template_name}' has composing layers but no replace base"
    )


def get_invoke_separator(repo_root: Path) -> str:
    workspace_root = get_workspace_root(repo_root)
    integration_json = workspace_root / ".specify" / "integration.json"
    if workspace_root != repo_root:
        confined_workspace_path(workspace_root, integration_json)
    if not integration_json.is_file():
        return "."
    # Split the parse out of the lookup and guard the top-level shape, matching
    # read_feature_json_feature_directory above and the bash/PowerShell twins,
    # which both fall back to "." for any unusable integration.json:
    #   * a non-mapping top level ([], "forge", 42, null) is valid JSON, so
    #     json.JSONDecodeError never fires and state.get(...) raised
    #     AttributeError;
    #   * a non-UTF-8 file raises UnicodeDecodeError, which is a ValueError --
    #     not an OSError -- so it escaped the except tuple. Realistic on
    #     Windows, where PowerShell 5.1's Out-File/`>` default to UTF-16.
    try:
        state = json.loads(integration_json.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "."
    if not isinstance(state, dict):
        return "."
    key = state.get("default_integration") or state.get("integration") or ""
    settings = state.get("integration_settings")
    if isinstance(key, str) and isinstance(settings, dict):
        entry = settings.get(key)
        if isinstance(entry, dict) and entry.get("invoke_separator") in {".", "-"}:
            return entry["invoke_separator"]
    return "."


def format_speckit_command(command_name: str, repo_root: Path) -> str:
    separator = get_invoke_separator(repo_root)
    name = command_name.lstrip("/")
    if name.startswith("speckit."):
        name = name[len("speckit.") :]
    elif name.startswith("speckit-"):
        name = name[len("speckit-") :]
    name = name.replace(".", separator)
    return f"/speckit{separator}{name}"
