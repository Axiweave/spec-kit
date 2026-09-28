"""Refresh managed assets and preserve edited native prompts during a move."""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from pathlib import Path

import yaml

from ..toml_string import escape_toml_basic
from ..agents import CommandRegistrar
from ..events import events_stale_exclusions, refresh_integration_events
from ..integration_state import installed_integration_keys, try_read_integration_json
from ..integration_runtime import (
    invoke_prefix_for_integration, invoke_separator_for_integration,
    resolve_integration_options, resolve_integration_script_type,
)
from ..integrations import get_integration
from ..integrations.base import IntegrationBase, yaml_quote
from ..integrations.manifest import IntegrationManifest
from ..workspace import atomic_json, confined, read_json, workspace_root_for


def _safe_path(root: Path, relative: str | Path, *, directory: bool = False) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or rel.anchor or ".." in rel.parts or not rel.parts:
        raise ValueError(f"Native path must be relative and confined: {relative}")
    current = root
    for part in rel.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Native path must not contain a symlink: {current}")
        if current != root / rel and current.exists() and not current.is_dir():
            raise ValueError(f"Native path parent is not a directory: {current}")
    path = confined(root, rel)
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"Native path has the wrong file type: {path}")
    return path


def _inventory(repository: Path) -> tuple[set[Path], set[Path], list[IntegrationManifest]]:
    """Find commands separately from settings, scripts, and event targets."""
    workspace = workspace_root_for(repository)
    _safe_path(workspace, ".specify/integration.json")
    state, error = try_read_integration_json(repository)
    if error:
        raise ValueError(f"Cannot read integration state: {error.detail or error.kind}")
    keys = set(installed_integration_keys(state or {}))
    manifest_dir = _safe_path(workspace, ".specify/integrations", directory=True)
    manifests = []
    paths = {_safe_path(repository, ".gitignore")}
    commands: set[Path] = set()

    def command(path: Path) -> None:
        root = native_path_root(repository, path)
        path = _safe_path(root, path.relative_to(root))
        if path.is_file():
            if path.suffix not in {".md", ".mdc", ".toml", ".yaml", ".yml"}:
                raise ValueError(f"Unsupported native command format: {path}")
            paths.add(path)
            commands.add(path)

    if manifest_dir.exists():
        for path in sorted(manifest_dir.glob("*.manifest.json")):
            key = path.name.removesuffix(".manifest.json")
            manifest = IntegrationManifest.load(key, repository)
            manifests.append(manifest)
            keys.add(key)
            for relative in manifest.files:
                owned = manifest.file_path(relative)
                if relative.startswith(".specify/"):
                    _safe_path(workspace, relative)
                    continue
                owned = _safe_path(repository, relative)
                paths.add(owned)
                # Settings and executable companions are not prompt documents.
                if owned.name == "SKILL.md" or (
                    owned.name.startswith(("speckit.", "speckit-"))
                    and owned.suffix in {".md", ".mdc", ".toml", ".yaml", ".yml"}
                ):
                    command(owned)

    registrar = CommandRegistrar()
    registrations = []
    for kind in ("extensions", "presets"):
        registry = _safe_path(workspace, f".specify/{kind}/.registry")
        if not registry.exists():
            continue
        entries = read_json(registry).get(kind, {})
        if not isinstance(entries, dict):
            raise ValueError(f"Invalid command registry: {registry}")
        for entry in entries.values():
            if not isinstance(entry, dict):
                raise ValueError(f"Invalid command registration: {registry}")
            registered = entry.get("registered_commands", {})
            skills = entry.get("registered_skills", {})
            if (
                not isinstance(registered, dict)
                or not all(isinstance(key, str) for key in registered)
                or not isinstance(skills, (dict, list))
                or (isinstance(skills, dict) and not all(isinstance(key, str) for key in skills))
            ):
                raise ValueError(f"Invalid command registration: {registry}")
            keys.update(registered)
            if isinstance(skills, dict):
                keys.update(skills)
            elif skills:
                # Old extension registries did not record the skills' agent.
                keys.update(registrar.AGENT_CONFIGS)
            registrations.append((registered, skills))

    roots: dict[str, list[Path]] = {}
    for key in sorted(keys):
        integration = get_integration(key)
        if integration is None:
            continue
        for relative in events_stale_exclusions(key):
            if relative.startswith(".specify/"):
                _safe_path(workspace, relative)
            else:
                paths.add(_safe_path(repository, relative))
        config = registrar.AGENT_CONFIGS.get(key, integration.registrar_config) or {}
        directories = {config.get("dir"), config.get("legacy_dir")}
        folder = integration.config.get("folder", "")
        if folder:
            directories.add(str(Path(folder) / integration.config.get("commands_subdir", "commands")))
            directories.add(str(Path(folder) / "skills"))
        directories.add(getattr(integration, "legacy_flat_command_dir", None))
        if key == "generic":
            _, options = resolve_integration_options(integration, state or {}, key, None)
            directories.add(integration.resolve_commands_dir(options, state or {}))
        roots[key] = []
        for relative in sorted(value for value in directories if value):
            if relative.startswith("~") or Path(relative).is_absolute():
                if key != "hermes" or relative != config.get("dir"):
                    continue
                from ..integrations.hermes import HermesIntegration

                root = native_path_root(repository, HermesIntegration.global_skills_dir())
            else:
                root = _safe_path(repository, relative, directory=True)
            roots[key].append(root)
            if not root.exists():
                continue
            for candidate in root.iterdir():
                if not candidate.name.startswith(("speckit.", "speckit-")):
                    continue
                if candidate.is_symlink():
                    raise ValueError(f"Native command must not be a symlink: {candidate}")
                if candidate.is_dir():
                    command(candidate / "SKILL.md")
                elif candidate.suffix in {".md", ".mdc", ".toml", ".yaml", ".yml"}:
                    command(candidate)

    for registered, skills in registrations:
        for key, names in registered.items():
            config = registrar.AGENT_CONFIGS.get(key)
            if config is None:
                continue
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError(f"Invalid registered commands for {key}")
            for name in names:
                output = registrar.compute_output_name(key, name, config)
                for root in roots.get(key, []):
                    command(root / f"{output}{config['extension']}")
                    if key == "copilot":
                        command(repository / ".github/prompts" / f"{name}.prompt.md")
        skill_map = skills if isinstance(skills, dict) else dict.fromkeys(keys, skills)
        for key, names in skill_map.items():
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ValueError(f"Invalid registered skills for {key}")
            for name in names:
                for root in roots.get(key, []):
                    command(root / name / "SKILL.md")
    return paths, commands, manifests


def native_path_root(repository: Path, path: Path) -> Path:
    """Confine native writes to the repository or Hermes-owned skill directory."""
    if path.is_relative_to(repository):
        return repository
    from ..integrations.hermes import HermesIntegration

    home = Path.home().resolve()
    root = _safe_path(home, HermesIntegration.global_skills_dir().relative_to(home), directory=True)
    if not path.is_relative_to(root):
        raise ValueError(f"Native command path is outside its owner directory: {path}")
    return root


def native_paths(repository: Path) -> tuple[Path, ...]:
    """Return native write targets, including Hermes skills and absent event files.

    This read-only inventory rejects unsafe owned paths before the move starts.
    Workspace metadata and global OMP launchers do not belong to this backup set.
    """
    paths, _, _ = _inventory(repository.resolve())
    return tuple(sorted(paths))


def _workspace_note(content: str) -> str:
    content = IntegrationBase.add_workspace_note(content)
    note = (
        "\n## External feature selection\n\n"
        "In external mode, read the active selection from `specify project info --json`.\n"
        "Use the installed workspace feature helpers to update the selection.\n"
        "Never read or write `.specify/feature.json` directly in external mode, even if older instructions below say otherwise.\n\n"
    )
    if "\r\n" in content:
        note = note.replace("\n", "\r\n")
    if note in content:
        return content
    lines = content.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for index in range(1, len(lines)):
            if lines[index].rstrip("\r\n") == "---":
                return "".join(lines[:index + 1]) + note + "".join(lines[index + 1:])
    return note + content


def _refresh_core_assets(
    repository: Path, manifests: list[IntegrationManifest], modified: dict[str, set[str]],
) -> None:
    from .. import console, ensure_executable_scripts, get_speckit_version
    from ..artifacts.catalog import locate_shared_asset_dir
    from ..integration_state import default_integration_key
    from ..shared_infra import install_shared_infra

    workspace = workspace_root_for(repository)
    script_types = {
        script_type for variant, script_type in (("bash", "sh"), ("powershell", "ps"), ("python", "py"))
        if (workspace / ".specify/scripts" / variant).is_dir()
    }
    if "py" in script_types:
        script_types.discard("ps" if os.name == "nt" else "sh")
    state, _ = try_read_integration_json(repository)
    state = state or {}
    key = default_integration_key(state)
    integration = get_integration(key) if key else None
    separator, prefix = ".", "/"
    if integration is not None:
        _, options = resolve_integration_options(integration, state, key, None)
        separator = invoke_separator_for_integration(integration, state, key, options, repository)
        prefix = invoke_prefix_for_integration(integration, key, options, repository)
    for script_type in sorted(script_types):
        install_shared_infra(
            repository, script_type, version=get_speckit_version(),
            console=console,
            invoke_separator=separator, invoke_prefix=prefix,
            refresh_managed=True, require_current_scripts=True,
        )
    if script_types:
        refreshed = IntegrationManifest.load("speckit", repository)
        preserved = set(refreshed.check_modified()) | refreshed.recovered_files
        scripts = {
            refreshed.file_path(relative) for relative in refreshed.files
            if relative.startswith(".specify/scripts/bash/") and relative.endswith(".sh")
            and relative not in preserved
        }
        ensure_executable_scripts(repository, script_paths=scripts)
        if os.name != "nt":
            for script in scripts:
                if not os.access(script, os.X_OK):
                    raise ValueError(f"Core helper is not executable: {script}. Restore execute permission before retrying the move.")
    # An old managed project template otherwise masks the current bundle.
    shared = next((item for item in manifests if item.key == "speckit"), None)
    template = ".specify/templates/commands/specify.md"
    if shared and template in shared.files and template not in modified[shared.key]:
        source = locate_shared_asset_dir("commands")
        if source is None or not (source / "specify.md").is_file():
            raise ValueError("The installed CLI lacks the specify command template. Reinstall the CLI before retrying the move.")
        shared = IntegrationManifest.load("speckit", repository)
        shared.record_file(template, (source / "specify.md").read_bytes())
        shared.save()


def _stock_specify_body(
    repository: Path, path: Path, manifests: list[IntegrationManifest], modified: dict[str, set[str]],
) -> str | None:
    """Render only the managed core feature-selection protocol."""
    if not path.is_relative_to(repository):
        return None
    from ..presets import PresetResolver

    relative = path.relative_to(repository).as_posix()
    for manifest in manifests:
        if relative not in manifest.files or relative in modified[manifest.key]:
            continue
        integration = get_integration(manifest.key)
        if integration is None:
            continue
        if path.name != integration.command_filename("specify") and not (
            path.name == "SKILL.md" and path.parent.name in {"speckit-specify", "speckit.specify"}
        ):
            continue
        raw = PresetResolver(repository).resolve_content(
            "speckit.specify", "command", rewrite_extension_paths=False
        )
        if raw is None:
            raise ValueError("The specify command template is unavailable. Restore it before retrying the move.")
        state, _ = try_read_integration_json(repository)
        state = state or {}
        script_type = resolve_integration_script_type(repository, state, manifest.key)
        config = integration.registrar_config or {}
        processed = integration.process_template(
            raw, manifest.key, script_type, config.get("args", "$ARGUMENTS"),
            invoke_separator=invoke_separator_for_integration(integration, state, manifest.key, project_root=repository),
            project_root=repository,
        )
        return CommandRegistrar.parse_frontmatter(processed)[1]
    return None


def _toml_prompt(content: str, replacement: str | None = None) -> str:
    data = tomllib.loads(content)
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("A native TOML command must have a string prompt.")
    updated = _workspace_note(prompt if replacement is None else replacement)
    if updated == prompt:
        return content
    expected = {**data, "prompt": updated}
    # Parse candidate string spans, rather than serializing unrelated TOML fields.
    for match in re.finditer(r'''(?m)^[ \t]*(?:prompt|"prompt"|'prompt')[ \t]*=[ \t]*''', content):
        start = match.end()
        if content[start:start + 1] not in {"'", '"'}:
            continue
        quote = content[start]
        delimiter = quote * (3 if content.startswith(quote * 3, start) else 1)
        for closing in re.finditer(re.escape(delimiter), content[start + len(delimiter):]):
            end = start + len(delimiter) + closing.end()
            if len(delimiter) == 3:
                while end < len(content) and content[end] == quote:
                    end += 1
            try:
                if tomllib.loads("prompt = " + content[start:end]).get("prompt") != prompt:
                    continue
                result = content[:start] + escape_toml_basic(updated) + content[end:]
                if tomllib.loads(result) == expected:
                    return result
            except tomllib.TOMLDecodeError:
                continue
    raise ValueError("Cannot safely locate the native TOML prompt string.")


def _yaml_prompt(content: str, replacement: str | None = None) -> str:
    try:
        data = yaml.safe_load(content)
        node = yaml.compose(content)
        if not isinstance(data, dict) or not isinstance(node, yaml.MappingNode):
            raise ValueError("A native YAML command must be a mapping.")
        field = "instructions" if "instructions" in data else "prompt"
        prompt = data.get(field)
        if not isinstance(prompt, str):
            raise ValueError("A native YAML command must have string instructions.")
        updated = _workspace_note(prompt if replacement is None else replacement)
        if updated == prompt:
            return content
        candidates = [value for key, value in node.value if key.value == field]
        if len(candidates) != 1 or not isinstance(candidates[0], yaml.ScalarNode):
            raise ValueError("Cannot safely locate the native YAML instructions.")
        scalar = candidates[0]
        start, end = scalar.start_mark.index, scalar.end_mark.index
        original = content[start:end]
        replacement = yaml_quote(updated)
        if scalar.style in {"|", ">"}:
            header = original.splitlines()[0]
            if "#" in header:
                replacement += " " + header[header.index("#"):]
            if original.endswith("\n"):
                replacement += "\r\n" if original.endswith("\r\n") else "\n"
        result = content[:start] + replacement + content[end:]
        if yaml.safe_load(result) != {**data, field: updated}:
            raise ValueError("Native YAML aliases prevent a safe instructions update.")
        return result
    except yaml.YAMLError as exc:
        raise ValueError(f"Cannot read native YAML instructions: {exc}") from exc


def refresh_commands(repository: Path) -> None:
    """Refresh managed core assets, native prompts, and events after cutover.

    The caller owns rollback for native files and workspace metadata. Edited or
    recovered files keep their original ownership hashes. This prevents a move
    from turning user content into files that uninstall can delete.
    """
    repository = repository.resolve()
    workspace = workspace_root_for(repository)
    if workspace == repository:
        raise ValueError("Native command refresh requires an external workspace.")
    _, commands, manifests = _inventory(repository)
    originals = {manifest.key: read_json(manifest.manifest_path) for manifest in manifests}
    modified = {
        manifest.key: set(manifest.check_modified()) | manifest.recovered_files
        for manifest in manifests
    }
    event_manifests = [
        manifest for manifest in manifests
        if (integration := get_integration(manifest.key)) is not None and integration.supports_events()
    ]
    dispatcher = workspace / ".specify/events.py"
    if event_manifests and dispatcher.is_file():
        digest = hashlib.sha256(dispatcher.read_bytes()).hexdigest()
        if not any(
            manifest.files.get(".specify/events.py") == digest
            and not manifest.is_recovered(".specify/events.py")
            for manifest in event_manifests
        ):
            raise ValueError(
                "The event dispatcher contains user content. "
                "Save its edits separately and restore the managed dispatcher before retrying the move."
            )
    _refresh_core_assets(repository, manifests, modified)
    replacements = {}
    for path in sorted(commands):
        content = path.read_bytes().decode("utf-8")
        original_content = content
        replacement = _stock_specify_body(repository, path, manifests, modified)
        if path.suffix == ".toml":
            updated = _toml_prompt(content, replacement)
        elif path.suffix in {".yaml", ".yml"}:
            updated = _yaml_prompt(content, replacement)
        else:
            if replacement is not None:
                header = re.match(r"\A---[ \t]*\r?\n.*?^---[ \t]*(?:\r?\n|$)", content, re.M | re.S)
                content = (header.group(0) if header else "") + "\n" + replacement + "\n"
            updated = _workspace_note(content)
        if updated != original_content:
            replacements[path] = updated.encode("utf-8")
    for path, content in replacements.items():
        path.write_bytes(content)
    try:
        refresh_integration_events(repository)
    except Exception as exc:
        raise ValueError(f"Cannot refresh native events: {exc}") from exc
    for manifest in manifests:
        current = read_json(manifest.manifest_path)
        original = originals[manifest.key]
        files = current.get("files", {})
        for relative in original.get("files", {}):
            if relative in modified[manifest.key]:
                if relative in files:
                    files[relative] = original["files"][relative]
                continue
            path = manifest.file_path(relative)
            if path.is_file() and relative in files:
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
                if path in replacements:
                    manifest.record_existing(relative)
        current = {**original, **current, "files": files}
        if current != original:
            atomic_json(manifest.manifest_path, current)
