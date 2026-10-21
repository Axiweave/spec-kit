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
SETUP_COMMAND = "speckit.init"  # The specify-init skill as a command. It works outside a project.


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


def _manifest_path() -> Path:
    root = user_data_dir().resolve()
    return confined(root, root / "integrations" / "omp" / "global-commands.json")


def _manifest(directory: Path) -> tuple[dict, dict[str, str]]:
    """Return the whole ownership record and the files owned in *directory*."""
    path = _manifest_path()
    data = read_json(path) if path.exists() or path.is_symlink() else {}
    files = data.get(str(directory), {})
    if not isinstance(files, dict):
        raise ValueError(f"Invalid global OMP manifest: {path}")
    for name, digest in files.items():
        if (not name.endswith(".md") or not _COMMAND_NAME.fullmatch(name[:-3])
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"Invalid global OMP manifest entry: {path}: {name}")
    return data, files


def _save_manifest(data: dict, directory: Path, files: dict[str, str]) -> None:
    path = _manifest_path()
    data = {key: value for key, value in data.items() if key != str(directory)}
    if files:
        data[str(directory)] = files
    if data:
        atomic_json(path, data)
    else:
        path.unlink(missing_ok=True)


def _setup_skill() -> Path:
    """Return the specify-init skill: bundled in a wheel, or `skills/` in a source checkout."""
    package = Path(__file__).resolve().parents[2]
    for candidate in (package / "core_pack/skills", package.parents[1] / "skills"):
        if (path := candidate / "specify-init/SKILL.md").is_file():
            return path
    raise ValueError("The specify-init skill is missing from this Specify CLI installation.")


def _setup_command() -> bytes:
    """Turn the specify-init skill into a command. It runs before a project exists, so it needs no project lookup."""
    text = _setup_skill().read_text(encoding="utf-8")
    _, front, body = text.split("---\n", 2)
    description = next(line for line in front.splitlines() if line.startswith("description:"))
    return f"---\n{description}\n---\n\n## User arguments\n\n$ARGUMENTS\n{body}".encode("utf-8")


def _default_names() -> list[str]:
    from . import OmpIntegration

    return [SETUP_COMMAND, *(f"speckit.{path.stem}" for path in OmpIntegration().list_command_templates())]


def _launcher(name: str) -> bytes:
    if name == SETUP_COMMAND:
        return _setup_command()
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
    """Install shared launchers and return the edited or unowned paths that were skipped.

    Install and upgrade refresh unchanged managed files and keep edited ones.
    Install refuses unmanaged names. Upgrade skips them.
    """
    directory = command_directory()
    data, files = _manifest(directory)
    selected = list(names) if names is not None else _default_names()
    if upgrade:
        selected.extend(name[:-3] for name in files)
    for name in selected:
        if not isinstance(name, str) or not _COMMAND_NAME.fullmatch(name):
            raise ValueError(f"Invalid global OMP command name: {name!r}")
    selected = sorted(set(selected))
    skipped = []
    pending = []
    for name in selected:
        path = directory / f"{name}.md"
        content = _launcher(name)
        if not path.exists() and not path.is_symlink():
            pending.append((path, None, content))
        elif path.name not in files:
            skipped.append(path)
        elif path.is_symlink() or not path.is_file():
            skipped.append(path)
        elif hashlib.sha256(previous := path.read_bytes()).hexdigest() != files[path.name]:
            skipped.append(path)
        elif previous != content:
            pending.append((path, previous, content))
    unmanaged = [path for path in skipped if path.name not in files]
    if unmanaged and not upgrade:
        raise ValueError("Global OMP command conflicts. Move or restore these files before retrying:\n" +
                         "\n".join(str(path) for path in unmanaged))
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
            _save_manifest(data, directory, updated)
    except Exception:
        for path, previous in reversed(written):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise
    return skipped


def uninstall_global_commands() -> tuple[list[Path], list[Path]]:
    """Remove only unchanged managed launchers in the active command directory."""
    directory = command_directory()
    data, files = _manifest(directory)
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
        _save_manifest(data, directory, remaining)
    return removed, conflicts


def check_global_commands() -> tuple[list[Path], list[Path]]:
    """Return the missing launchers and the edited or unowned launchers in the active command directory."""
    directory = command_directory()
    _, files = _manifest(directory)
    names = {f"{name}.md" for name in _default_names()} | files.keys()
    missing, modified = [], []
    for name in sorted(names):
        path = directory / name
        if not path.exists() and not path.is_symlink():
            missing.append(path)
        elif (name not in files or path.is_symlink() or not path.is_file()
              or hashlib.sha256(path.read_bytes()).hexdigest() != files[name]):
            modified.append(path)
    return missing, modified


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
            skipped = install_global_commands(upgrade=action == "upgrade")
            console.print(f"Global OMP commands ready in {command_directory()}", markup=False)
            if skipped:
                console.print("Skipped modified or unowned global OMP commands:", style="yellow")
                for path in skipped:
                    console.print(str(path), markup=False)
    except (OSError, ValueError) as exc:
        console.print(f"Global OMP {action} failed: {exc}", style="red", markup=False)
        raise typer.Exit(1) from None
