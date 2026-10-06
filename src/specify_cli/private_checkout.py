"""Private mode: hide Spec Kit files in a code checkout with Git's local exclude file."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from rich.console import Console

from .workspace import atomic_json, checkout_record_path, confined, read_json
from .workspace_git import run_git

BEGIN = "# BEGIN Spec Kit private mode (managed; do not edit)"
END = "# END Spec Kit private mode"
LOCATOR_RULE = "/.specify/"


def _rule(relative: str) -> str:
    return "/" + relative.replace("\\", "\\\\").replace(" ", "\\ ")


def _git_root(checkout: Path) -> bool:
    try:
        return run_git(checkout, "rev-parse", "--is-inside-work-tree", allowed_codes=(0, 128)) == "true"
    except OSError:
        return False


def _worktrees(checkout: Path) -> list[Path]:
    listing = run_git(checkout, "worktree", "list", "--porcelain")
    return [Path(line[len("worktree "):]) for line in listing.splitlines() if line.startswith("worktree ")]


def exclude_path(checkout: Path) -> Path:
    path = Path(run_git(checkout, "rev-parse", "--git-path", "info/exclude"))
    return path if path.is_absolute() else checkout / path


def checkout_manifest_keys(checkout: Path) -> set[str]:
    """Return every checkout path that this checkout's manifests own or hide."""
    keys: set[str] = set()
    for manifest in sorted((checkout / ".specify/integrations").glob("*.manifest.json")):
        data = read_json(manifest)
        keys.update(data.get("files", {}))
        keys.update(data.get("excluded", []))
    return {key for key in keys if not key.startswith(".specify/")}


def regenerate_exclude_block(checkout: Path) -> None:
    """Rewrite the managed block from the locators and checkout manifests of every worktree.

    Linked worktrees share one exclude file, so the block is the union over
    all of them. User lines outside the block never change.
    """
    if not _git_root(checkout):
        return
    path = exclude_path(checkout)
    if path.is_symlink():
        raise ValueError(f"Refusing a symlinked exclude file: {path}")
    text = path.read_bytes().decode("utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in text else "\n"
    before, after, inside, found = [], [], False, False
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if bare == BEGIN:
            inside = found = True
        elif bare == END:
            inside = False
        elif not inside:
            (after if found else before).append(line)
    rules: set[str] = set()
    for root in _worktrees(checkout):
        if (root / ".specify/project.json").is_file():
            rules.add(LOCATOR_RULE)
            rules.update(_rule(key) for key in checkout_manifest_keys(root))
    block = "".join(f"{line}{newline}" for line in (BEGIN, *sorted(rules), END)) if rules else ""
    if block and before and not before[-1].endswith("\n"):
        before[-1] += newline
    content = "".join(before) + block + "".join(after)
    if content != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


def refuse_tracked(checkout: Path, paths: Iterable[str]) -> None:
    """Stop before any write when Spec Kit would change a file that Git tracks."""
    paths = sorted(set(paths))
    if not paths or not _git_root(checkout):
        return
    tracked = run_git(checkout, "ls-files", "-z", "--cached", "--", *(f":(literal){p}" for p in paths))
    if tracked:
        first = tracked.split("\0")[0]
        raise ValueError(
            f"Spec Kit would change tracked file {first}. Remove it from Git or choose another integration."
        )


@contextmanager
def _isolated_user_dirs(home: Path) -> Iterator[None]:
    names = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA")
    saved = {name: os.environ.get(name) for name in names}
    values = {
        "HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data", "APPDATA": home / "config", "LOCALAPPDATA": home / "data",
    }
    try:
        os.environ.update({name: str(value) for name, value in values.items()})
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _staged_pair(workspace: Path | None, *, private: bool) -> Iterator[tuple[Path, Path]]:
    """Yield a staged checkout and workspace under a temporary home.

    The staged checkout has a locator and a record. The staged workspace is a
    copy of *workspace*'s ``.specify``, or a new one. Nothing outside the
    temporary directory changes. The home folder is ``<checkout>/../home``.
    """
    with tempfile.TemporaryDirectory(prefix="speckit-plan-") as temp:
        root = Path(temp).resolve()
        checkout, staged = root / "checkout", root / "workspace"
        checkout.mkdir()
        if workspace is not None:
            shutil.copytree(workspace / ".specify", staged / ".specify", symlinks=True)
            project_id = read_json(staged / ".specify/workspace.json")["project_id"]
        else:
            (staged / ".specify").mkdir(parents=True)
            project_id = str(uuid4())
        atomic_json(staged / ".specify/workspace.json", {
            "schema_version": 1, "project_id": project_id, "private": private,
        })
        atomic_json(checkout / ".specify/project.json", {
            "schema_version": 1, "project_id": project_id, "storage": "external",
        })
        atomic_json(checkout_record_path(checkout), {
            "schema_version": 1, "workspace": str(staged.resolve()), "active_feature": None,
        })
        with _isolated_user_dirs(root / "home"):
            yield checkout, staged


def plan_checkout_files(
    install: Callable[[Path], None], *, workspace: Path | None = None,
) -> list[str]:
    """Return the checkout files that *install* writes, from a dry run in a staged private-mode copy."""
    with _staged_pair(workspace, private=True) as (checkout, _):
        install(checkout)
        return sorted(
            path.relative_to(checkout).as_posix()
            for path in checkout.rglob("*")
            if path.is_file() or path.is_symlink()
        )


def _modified_files(checkout: Path, workspace: Path, key: str) -> list[str]:
    """Return the files of integration *key* whose bytes differ from its workspace and checkout manifests.

    Uses only the explicit roots: ``.specify/`` keys belong to *workspace*, other keys to *checkout*.
    A missing file is not modified. A symlink or a path that leaves its root is.
    """
    from .integrations.manifest import sha256_file

    files: dict[str, str] = {}
    for root in (workspace, checkout):
        path = root / ".specify/integrations" / f"{key}.manifest.json"
        if path.is_file():
            files.update(read_json(path).get("files", {}))
    modified = []
    for relative, digest in sorted(files.items()):
        root = workspace if relative.startswith(".specify/") else checkout
        path = root / relative
        if not os.path.lexists(path):
            continue
        try:
            changed = path.is_symlink() or not confined(root, relative).is_file() or sha256_file(path) != digest
        except (OSError, ValueError):
            changed = True
        if changed:
            modified.append(relative)
    return modified


@dataclass(frozen=True)
class Restore:
    """What `restore_checkout` changed. *ignore_error* is set when the ``.gitignore`` append failed.

    *packages* maps the repair command of each package that still misses a file to the package ID.
    """

    restored: int
    modified: dict[str, list[str]]
    locator_written: bool
    ignore_updated: bool
    ignore_error: str | None
    packages: dict[str, str] = field(default_factory=dict)


_PACKAGES = (  # registry file, registry key, bundled-package locator in `assets`, repair command
    (".specify/extensions/.registry", "extensions", "locate_bundled_extension", "specify extension add {} --force"),
    (".specify/presets/.registry", "presets", "_locate_bundled_preset", "specify preset update {}"),
    (".specify/workflows/workflow-registry.json", "workflows", "_locate_bundled_workflow", "specify workflow add {}"),
    (".specify/workflows/steps/step-registry.json", "steps", None, "specify workflow step add {} --force"),
)


def _stage_bundled_files(staged: Path) -> dict[str, str]:
    """Copy the missing generated files of the packages in workspace *staged* from the CLI's bundled packages.

    A file comes back only when the bundled bytes match its recorded hash, so no
    installer runs and no registry changes. Files that workspace Git never
    shares, such as caches, do not count. Return the repair command of every
    package that still misses a file, mapped to the package ID.
    """
    from . import assets
    from .integrations.manifest import sha256_file
    from .workspace_git import _private

    repairs = {}
    for registry, key, locate, repair in _PACKAGES:
        path = staged / registry
        entries = read_json(path).get(key) if path.is_file() else None
        for package, metadata in entries.items() if isinstance(entries, dict) else ():
            files = metadata.get("generated_files") if isinstance(metadata, dict) else None
            folder = f"{Path(registry).parent.as_posix()}/{package}/"
            bundled = getattr(assets, locate)(package) if locate else None
            for name, digest in files.items() if isinstance(files, dict) else ():
                confined(staged, name)
                target = staged / name
                if _private(name) or os.path.lexists(target):
                    continue
                source = bundled / name.removeprefix(folder) if bundled and name.startswith(folder) else None
                if source is not None and source.is_file() and not source.is_symlink() and sha256_file(source) == digest:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                else:
                    repairs[repair.format(package)] = package  # An extension and a preset may share an ID.
    return repairs


def _unescape(line: str) -> str:
    return re.sub(r"\\(.)", r"\1", line[1:])


def _missing(root: Path, relative: str) -> Path | None:
    """Return ``root/relative`` when it is absent. Refuse a write through a symlinked folder."""
    path = root / relative
    if os.path.lexists(path):
        return None
    for parent in reversed(path.relative_to(root).parents[:-1]):
        if (root / parent).is_symlink():
            raise ValueError(f"Refusing to write through a symlinked folder: {root / parent}")
    return path


def _make_parents(path: Path, created: list[Path]) -> None:
    missing = []
    parent = path.parent
    while not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            continue  # A concurrent link created it, so it is not ours to remove.
        created.append(directory)


def restore_checkout(repository: Path, workspace: Path, active_feature: str | None) -> Restore:
    """Create the missing generated files of a non-private checkout, then write its record (research D2).

    Phase 1 builds the files in a staged copy and changes nothing real. Phase 2
    writes the locator when absent, creates each missing file exclusively,
    updates a manifest hash only when the restored bytes differ, and writes the
    record last. An error or interrupt in phase 2 rolls every write back. The
    root ``.gitignore`` append comes after the record and is never rolled back.
    Every path is explicit: nothing reads the real record or `workspace_root_for`.
    """
    from . import ensure_executable_scripts
    from .assets import get_speckit_version
    from .terminal import StepTracker
    from .integration_runtime import (
        invoke_prefix_for_integration, invoke_separator_for_integration,
        resolve_integration_options, resolve_integration_script_type,
    )
    from .integration_state import default_integration_key, installed_integration_keys, try_read_integration_json
    from .integrations import get_integration
    from .integrations.manifest import write_manifest, append_gitignore
    from .shared_infra import install_shared_infra

    version = get_speckit_version()
    state, error = try_read_integration_json(workspace)
    if error is not None:
        raise ValueError(f"Cannot read the workspace integration state: {error.detail}")
    state = state or {}
    keys = installed_integration_keys(state)
    modified = {key: files for key in keys if (files := _modified_files(repository, workspace, key))}
    ignore = repository / ".gitignore"
    if ignore.is_symlink():
        raise ValueError(f"Refusing a symlinked ignore file: {ignore}")
    present = set(ignore.read_text(encoding="utf-8").splitlines()) if ignore.exists() else set()
    home = Path.home()
    restore: list[tuple[Path, bytes, int]] = []
    updates: dict[Path, dict] = {}
    with _staged_pair(workspace, private=False) as (checkout, staged):
        packages = _stage_bundled_files(staged)  # Before attach, which registers package commands.
        attach_integrations(checkout, keys, version=version)
        default = default_integration_key(state)
        integration = get_integration(default) if default else None
        separator, prefix = ".", "/"
        if integration is not None:
            _, options = resolve_integration_options(integration, state, default, None)
            separator = invoke_separator_for_integration(integration, state, default, options, checkout)
            prefix = invoke_prefix_for_integration(integration, default, options, checkout)
        shared = staged / ".specify/integrations/speckit.manifest.json"
        variants = {"bash": "sh", "powershell": "ps", "python": "py"}
        script_types = {resolve_integration_script_type(checkout, state, default or "", None)} | {
            variants[parts[2]] for parts in (Path(name).parts for name in (
                read_json(shared).get("files", {}) if shared.is_file() else {}
            )) if parts[:2] == (".specify", "scripts") and len(parts) > 3 and parts[2] in variants
        }
        for script_type in sorted(script_types):
            install_shared_infra(
                checkout, script_type, version=version, console=Console(quiet=True),
                invoke_separator=separator, invoke_prefix=prefix,
            )
        ensure_executable_scripts(checkout, StepTracker("restore"))
        staged_ignore = checkout / ".gitignore"
        lines = [
            _unescape(line) for line in staged_ignore.read_text(encoding="utf-8").splitlines()
            if line.startswith("/") and line not in present
        ] if staged_ignore.is_file() else []
        restored: set[str] = set()
        for base, real, skip in (
            (checkout, repository, {".gitignore", ".specify/project.json", ".specify/checkout.json"}),
            (staged, workspace, {".specify/workspace.json"}),
            (checkout.parent / "home", home, set()),
        ):
            for path in sorted(base.rglob("*")):
                relative = path.relative_to(base).as_posix()
                if path.is_symlink() or not path.is_file() or relative in skip:
                    continue
                target = _missing(real, relative)
                if target is not None:
                    restore.append((target, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))
                    restored.add(relative)
        for path in sorted((staged / ".specify/integrations").glob("*.manifest.json")):
            real = workspace / ".specify/integrations" / path.name
            if real.is_symlink() or not real.is_file():
                continue  # A missing manifest is on the restore list.
            files, data = read_json(path).get("files", {}), read_json(real)
            recorded = data.get("files", {})
            changes = {name: files[name] for name in restored & files.keys() if recorded.get(name) != files[name]}
            if changes:
                updates[real] = {**data, "files": {**recorded, **changes}}
    if (lines or "/.specify/checkout.json" not in present) and not os.access(
        ignore if ignore.exists() else repository, os.W_OK,
    ):
        raise ValueError(f"Cannot write {ignore}. Make it writable, then run link again.")

    locator = repository / ".specify/project.json"
    record = checkout_record_path(repository)
    saved = {path: path.read_bytes() if path.is_file() else None for path in (*updates, record)}
    write_locator = not os.path.lexists(locator)
    touched: list[Path] = []  # Saved paths that this run rewrote.
    created: list[Path] = []  # Files and folders that this run created, in order.
    count = 0
    try:
        if write_locator:
            project_id = read_json(workspace / ".specify/workspace.json")["project_id"]
            _make_parents(locator, created)
            atomic_json(locator, {"schema_version": 1, "project_id": project_id, "storage": "external"}, exclusive=True)
            created.append(locator)
        for target, data, mode in restore:
            _make_parents(target, created)
            try:
                handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
            except FileExistsError:
                continue  # A concurrent link created it. Keep its bytes.
            created.append(target)
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            count += 1
        for path, data in updates.items():
            touched.append(path)
            write_manifest(workspace, path, data)
        touched.append(record)
        atomic_json(record, {"schema_version": 1, "workspace": str(workspace), "active_feature": active_feature})
    except (Exception, KeyboardInterrupt):
        for path in touched:
            if saved[path] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(saved[path])
        for path in reversed(created):
            with suppress(OSError):
                path.rmdir() if path.is_dir() else path.unlink()
        raise

    updated, failure = False, None
    try:
        for relative in (*lines, ".specify/checkout.json"):
            updated = append_gitignore(repository, relative) or updated
    except (OSError, ValueError) as exc:
        failure = str(exc)
    return Restore(count, modified, write_locator, updated, failure, packages)


def install_integration(
    checkout: Path, integration, *, version: str, script_type: str,
    raw_options: str | None = None, parsed_options: dict | None = None, global_commands: bool = False,
):
    """Write one integration's files into *checkout* and save its manifest."""
    from .events import resolve_events
    from .integrations.manifest import IntegrationManifest

    manifest = IntegrationManifest(integration.key, checkout, version=version)
    integration.setup(
        checkout,
        manifest,
        parsed_options=parsed_options or None,
        script_type=script_type,
        raw_options=raw_options,
        events=resolve_events(integration.key, integration.config, checkout, parsed_options or None),
        global_commands=global_commands,
    )
    manifest.save()
    return manifest


def attach_integrations(checkout: Path, keys: Iterable[str], *, version: str) -> None:
    """Install the workspace's integrations *keys* into *checkout* with their saved settings.

    The default integration also gets the enabled extension and preset commands.
    """
    from .integration_runtime import resolve_integration_options, resolve_integration_script_type
    from .integration_state import default_integration_key, try_read_integration_json
    from .integrations import get_integration
    from .integrations.helpers import register_extensions_for_agent, register_presets_for_agent

    state, error = try_read_integration_json(checkout)
    if error is not None:
        raise ValueError(f"Cannot read the workspace integration state: {error.detail}")
    state = state or {}
    keys = list(keys)
    for key in keys:
        integration = get_integration(key)
        if integration is None:
            raise ValueError(f"The workspace lists an unknown integration: {key}")
        raw, parsed = resolve_integration_options(integration, state, key, None)
        install_integration(
            checkout, integration, version=version,
            script_type=resolve_integration_script_type(checkout, state, key, None),
            raw_options=raw, parsed_options=parsed,
        )
    default = default_integration_key(state)
    if default in keys:
        continuing = "The checkout is attached, but installed extensions or presets may need re-registration."
        register_extensions_for_agent(checkout, default, force=True, continuing=continuing)
        register_presets_for_agent(checkout, default, continuing=continuing)


def remove_checkout_integration(checkout: Path, key: str, *, force: bool = False):
    """Remove one integration's files from *checkout* only. The shared workspace state stays."""
    from .integrations import get_integration
    from .integrations.manifest import IntegrationManifest

    manifest = IntegrationManifest.load(key, checkout)
    manifest.checkout_only = True
    integration = get_integration(key)
    if integration is None:
        return manifest.uninstall(checkout, force=force)
    return integration.teardown(checkout, manifest, force=force)


def plan_reconcile(
    checkout: Path, workspace: Path, *, version: str,
) -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Return the integration keys to remove from and install into *checkout*, and the kept modified files.

    Remove the ones that the workspace no longer lists and the ones whose version
    differs from the workspace manifest. Install the missing and the stale ones.
    A stale key with a modified file is not reinstalled: its files stay, and the
    third value names them per key. Refuse before any write when an install
    target is tracked.
    """
    from .integration_state import installed_integration_keys, try_read_integration_json

    state, error = try_read_integration_json(workspace)
    if error is not None:
        raise ValueError(f"Cannot read the workspace integration state: {error.detail}")
    listed = installed_integration_keys(state or {})
    shared = workspace / ".specify/integrations"
    present = {path.name.removesuffix(".manifest.json"): read_json(path).get("version")
               for path in (checkout / ".specify/integrations").glob("*.manifest.json")}
    stale = [
        key for key, local_version in present.items()
        if key in listed and (shared / f"{key}.manifest.json").is_file()
        and read_json(shared / f"{key}.manifest.json").get("version") != local_version
    ]
    modified = {key: files for key in stale if (files := _modified_files(checkout, workspace, key))}
    stale = [key for key in stale if key not in modified]
    install = [key for key in listed if key not in present or key in stale]
    if install:
        refuse_tracked(checkout, plan_checkout_files(
            lambda staged: attach_integrations(staged, install, version=version), workspace=workspace,
        ))
    return sorted(set(present) - set(listed)) + stale, install, modified


def reconcile_checkout(checkout: Path, remove: Iterable[str], install: list[str], *, version: str) -> None:
    """Apply a plan from `plan_reconcile` to *checkout*."""
    for key in remove:
        remove_checkout_integration(checkout, key)
    attach_integrations(checkout, install, version=version)
    regenerate_exclude_block(checkout)
