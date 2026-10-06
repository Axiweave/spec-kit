"""Private mode: hide Spec Kit files in a code checkout with Git's local exclude file."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .workspace import atomic_json, project_record_path, read_json
from .workspace_git import _git

BEGIN = "# BEGIN Spec Kit private mode (managed; do not edit)"
END = "# END Spec Kit private mode"
LOCATOR_RULE = "/.specify/"


def _rule(relative: str) -> str:
    return "/" + relative.replace("\\", "\\\\").replace(" ", "\\ ")


def _git_root(checkout: Path) -> bool:
    try:
        return _git(checkout, "rev-parse", "--is-inside-work-tree", allowed_codes=(0, 128)) == "true"
    except OSError:
        return False


def _worktrees(checkout: Path) -> list[Path]:
    listing = _git(checkout, "worktree", "list", "--porcelain")
    return [Path(line[len("worktree "):]) for line in listing.splitlines() if line.startswith("worktree ")]


def exclude_path(checkout: Path) -> Path:
    path = Path(_git(checkout, "rev-parse", "--git-path", "info/exclude"))
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
    kept, inside = [], False
    for line in text.splitlines():
        if line == BEGIN:
            inside = True
        elif line == END:
            inside = False
        elif not inside:
            kept.append(line)
    rules: set[str] = set()
    for root in _worktrees(checkout):
        if (root / ".specify/project.json").is_file():
            rules.add(LOCATOR_RULE)
            rules.update(_rule(key) for key in checkout_manifest_keys(root))
    while kept and not kept[-1]:
        kept.pop()
    if rules:
        kept += [BEGIN, *sorted(rules), END]
    content = newline.join(kept) + newline if kept else ""
    if content != text:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")


def refuse_tracked(checkout: Path, paths: Iterable[str]) -> None:
    """Stop before any write when Spec Kit would change a file that Git tracks."""
    paths = sorted(set(paths))
    if not paths or not _git_root(checkout):
        return
    tracked = _git(checkout, "ls-files", "-z", "--cached", "--", *(f":(literal){p}" for p in paths))
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


def plan_checkout_files(
    install: Callable[[Path], None], *, workspace: Path | None = None,
) -> list[str]:
    """Return the checkout files that *install* writes, from a dry run in a staged copy.

    The staged copy is a private-mode project: a temporary checkout with a
    locator, a temporary workspace (a copy of *workspace*'s ``.specify``, or a
    new one), and a machine record under a temporary home. *install* receives
    the staged checkout. Nothing outside the temporary directory changes.
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
            "schema_version": 1, "project_id": project_id, "private": True,
        })
        atomic_json(checkout / ".specify/project.json", {
            "schema_version": 1, "project_id": project_id, "storage": "external",
        })
        with _isolated_user_dirs(root / "home"):
            atomic_json(project_record_path(project_id), {
                "schema_version": 1, "workspace": str(staged.resolve()), "active_feature": None,
            })
            install(checkout)
        return sorted(
            path.relative_to(checkout).as_posix()
            for path in checkout.rglob("*")
            if path.is_file() or path.is_symlink()
        )


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
    from .integrations._helpers import _register_extensions_for_agent, _register_presets_for_agent

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
        _register_extensions_for_agent(checkout, default, force=True, continuing=continuing)
        _register_presets_for_agent(checkout, default, continuing=continuing)


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


def reconcile_checkout(checkout: Path, *, version: str) -> None:
    """Make *checkout*'s integration files match the integrations that the workspace lists.

    Install the missing ones, reinstall the ones whose version differs from the
    workspace manifest, and remove the ones that the workspace no longer lists.
    """
    from .integration_state import installed_integration_keys, try_read_integration_json
    from .workspace import workspace_root_for

    state, error = try_read_integration_json(checkout)
    if error is not None:
        raise ValueError(f"Cannot read the workspace integration state: {error.detail}")
    listed = installed_integration_keys(state or {})
    shared = workspace_root_for(checkout) / ".specify/integrations"
    present = {path.name.removesuffix(".manifest.json"): read_json(path).get("version")
               for path in (checkout / ".specify/integrations").glob("*.manifest.json")}
    stale = [
        key for key, local_version in present.items()
        if key in listed and (shared / f"{key}.manifest.json").is_file()
        and read_json(shared / f"{key}.manifest.json").get("version") != local_version
    ]
    install = [key for key in listed if key not in present or key in stale]
    if install:
        refuse_tracked(checkout, plan_checkout_files(
            lambda staged: attach_integrations(staged, install, version=version),
            workspace=workspace_root_for(checkout),
        ))
    for key in sorted(set(present) - set(listed)) + stale:
        remove_checkout_integration(checkout, key)
    attach_integrations(checkout, install, version=version)
    regenerate_exclude_block(checkout)
