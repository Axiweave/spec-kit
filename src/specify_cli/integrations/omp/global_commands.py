"""User-wide OMP launchers with independent hash ownership."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from collections.abc import Iterable

from ...workspace import atomic_json, confined, read_json, user_data_dir, workspace_root_for


_COMMAND_NAME = re.compile(r"speckit\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESERVED_PROFILE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.I)


def _profile(value: str) -> str:
    value = value.strip()
    if not value or value == "default":
        return ""
    if not _PROFILE_NAME.fullmatch(value) or value.endswith(".") or _RESERVED_PROFILE.fullmatch(value):
        raise ValueError(f"Invalid OMP profile: {value!r}. Select a valid OMP_PROFILE or PI_PROFILE.")
    return value


def command_directory() -> Path:
    """Match OMP's native profile and agent-directory environment precedence."""
    profile = _profile(os.environ.get("OMP_PROFILE", os.environ.get("PI_PROFILE", "")))
    config = os.environ.get("PI_CONFIG_DIR") or ".omp"
    # Node's path.join keeps an absolute second component beneath the first.
    root = Path(os.path.normpath(str(Path.home()) + os.sep + config))
    agent = root / "profiles" / profile / "agent" if profile else root / "agent"
    override = os.environ.get("PI_CODING_AGENT_DIR")
    if not profile and override:
        try:
            inherited_profile = _profile(os.environ.get("PI_PROFILE", ""))
        except ValueError:
            inherited_profile = ""
        inherited = root / "profiles" / inherited_profile / "agent"
        if not inherited_profile or override != str(inherited):
            agent = Path(override).absolute()
    directory = agent / "commands"
    if directory.is_symlink():
        raise ValueError(f"OMP command directory must not be a symlink: {directory}")
    if directory.exists() and not directory.is_dir():
        raise ValueError(f"OMP command directory is not a directory: {directory}")
    return confined(agent, directory)


def global_commands_enabled(repository: Path) -> bool:
    """Read the project's saved command scope without a feature override."""
    workspace = workspace_root_for(repository)
    path = workspace / ".specify/init-options.json"
    confined(workspace, path)
    if not path.exists() and not path.is_symlink():
        return False
    return read_json(path).get("command_scope") == "global"


def _manifest(directory: Path) -> tuple[Path, dict[str, str]]:
    identity = hashlib.sha256(os.fsencode(str(directory))).hexdigest()
    root = user_data_dir().resolve()
    path = root / "integrations" / "omp" / f"{identity}.json"
    confined(root, path)
    if not path.exists() and not path.is_symlink():
        return path, {}
    data = read_json(path)
    if (type(data.get("schema_version")) is not int or data["schema_version"] != 1
            or data.get("command_directory") != str(directory)):
        raise ValueError(f"Invalid global OMP manifest: {path}")
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"Invalid global OMP manifest files: {path}")
    for name, digest in files.items():
        if (not name.endswith(".md") or not _COMMAND_NAME.fullmatch(name[:-3])
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"Invalid global OMP manifest entry: {path}: {name}")
    return path, files


def _save_manifest(path: Path, directory: Path, files: dict[str, str]) -> None:
    atomic_json(path, {"schema_version": 1, "command_directory": str(directory), "files": files})


def _launcher(name: str) -> bytes:
    return (
        f"---\ndescription: Run {name} for the current Spec Kit project\n---\n\n"
        "## User arguments\n\n$ARGUMENTS\n\n"
        "## Project command\n\n"
        "1. Keep the invoking project's working directory and the original user arguments above.\n"
        f"2. Run `specify project command {name} --json` from that working directory.\n"
        "3. If the command fails, report its error and stop. Do not use another project's content.\n"
        "4. Read the JSON response's `content`, `repository_root`, `workspace_root`, and `feature_dir`.\n"
        "5. Follow the returned `content` as this command's instructions, with the original user arguments.\n"
        "6. Replace the returned content's argument placeholder with the original user arguments. Do not run arguments as shell code.\n"
        "7. Keep `repository_root` as the working directory. Use the returned workspace and feature paths for Spec Kit artifacts.\n"
    ).encode("utf-8")


def install_global_commands(names: Iterable[str] | None = None, *, upgrade: bool = False) -> list[Path]:
    """Install shared names without claiming or replacing user-owned content."""
    from . import OmpIntegration

    directory = command_directory()
    manifest_path, files = _manifest(directory)
    selected = list(names) if names is not None else [
        f"speckit.{path.stem}" for path in OmpIntegration().list_command_templates()
    ]
    if upgrade:
        selected.extend(name[:-3] for name in files)
    for name in selected:
        if not isinstance(name, str) or not _COMMAND_NAME.fullmatch(name):
            raise ValueError(f"Invalid global OMP command name: {name!r}")
    selected = sorted(set(selected))
    conflicts = []
    pending = []
    for name in selected:
        path = directory / f"{name}.md"
        previous = None
        if path.is_symlink() or (path.exists() and not path.is_file()):
            conflicts.append(path)
            continue
        if path.exists():
            previous = path.read_bytes()
            if files.get(path.name) != hashlib.sha256(previous).hexdigest():
                conflicts.append(path)
                continue
        content = _launcher(name)
        if previous is None or (upgrade and previous != content):
            pending.append((path, previous, content))
    if conflicts:
        raise ValueError("Global OMP command conflicts. Move or restore these files before retrying:\n" +
                         "\n".join(str(path) for path in conflicts))
    written = []
    try:
        if pending:
            directory.mkdir(parents=True, exist_ok=True)
        updated = dict(files)
        for path, previous, content in pending:
            # Exclusive creation prevents a new user file from being claimed.
            with path.open("xb" if previous is None else "wb") as handle:
                written.append((path, previous))
                handle.write(content)
            updated[path.name] = hashlib.sha256(content).hexdigest()
        if updated != files:
            _save_manifest(manifest_path, directory, updated)
    except Exception:
        for path, previous in reversed(written):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise
    return [directory / f"{name}.md" for name in selected]


def uninstall_global_commands() -> tuple[list[Path], list[Path]]:
    """Remove only unchanged managed launchers in the active command directory."""
    directory = command_directory()
    manifest_path, files = _manifest(directory)
    removed, conflicts = [], []
    remaining = dict(files)
    for name, digest in files.items():
        path = directory / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            conflicts.append(path)
        elif not path.exists():
            remaining.pop(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            conflicts.append(path)
        else:
            path.unlink()
            removed.append(path)
            remaining.pop(name)
    if remaining != files:
        if remaining:
            _save_manifest(manifest_path, directory, remaining)
        else:
            manifest_path.unlink(missing_ok=True)
    return removed, conflicts


def run_global_command(key: str | None, action: str) -> None:
    """Handle the standalone CLI route before any project lookup."""
    import typer
    from ...terminal import console

    if key != "omp":
        console.print(f"Global integration {action} supports only omp, not {key!r}.", style="red", markup=False)
        raise typer.Exit(1)
    try:
        if action == "uninstall":
            removed, conflicts = uninstall_global_commands()
            console.print(f"Removed {len(removed)} global OMP command(s).")
            if conflicts:
                console.print("Preserved modified global OMP commands:", style="yellow")
                for path in conflicts:
                    console.print(str(path), markup=False)
                raise typer.Exit(1)
        else:
            installed = install_global_commands(upgrade=action == "upgrade")
            console.print(f"Global OMP commands ready: {len(installed)} in {command_directory()}", markup=False)
    except (OSError, ValueError) as exc:
        console.print(f"Global OMP {action} failed: {exc}", style="red", markup=False)
        raise typer.Exit(1) from None
