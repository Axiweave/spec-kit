"""Symlinks from a checkout with external storage to workspace folders: `specs` and `.specify/memory`."""
from __future__ import annotations

import os
from pathlib import Path

LINKS = ("specs", ".specify/memory")


def is_workspace_link(repository: Path, workspace: Path, name: str) -> bool:
    link = repository / name
    return link.is_symlink() and Path(os.readlink(link)) == workspace / name


def owned_links(repository: Path) -> list[Path]:
    """Return the links that point into the workspace that this checkout's record names."""
    from .workspace import checkout_record_path, read_json

    record = checkout_record_path(repository)
    if not record.is_file() or not isinstance(workspace := read_json(record).get("workspace"), str):
        return []
    return [repository / name for name in LINKS if is_workspace_link(repository, Path(workspace), name)]


def link_workspace(repository: Path, workspace: Path) -> list[str]:
    """Link each name in LINKS to the workspace unless the `workspace_links` default is false.

    A link to an older workspace path is replaced. Other files stay. The managed
    exclude block hides the links from Git. Return lines for the user.
    """
    from .private_checkout import regenerate_exclude_block
    from .user_config import load_defaults

    if load_defaults().get("workspace_links") == "false":
        return []
    notes = []
    for name in LINKS:
        link, target = repository / name, workspace / name
        if is_workspace_link(repository, workspace, name):
            continue
        # Replace only a dangling link to the same path, such as one left by a workspace move.
        stale = link.is_symlink() and not link.exists() and (
            Path(os.readlink(link)).parts[-len(Path(name).parts):] == Path(name).parts
        )
        if os.path.lexists(link) and not stale:
            notes.append(f"Kept {link}. Remove it to get the link to {target}.")
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            if stale:
                link.unlink()
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            notes.append(f"Could not link {name}: {exc}")
            continue
        notes.append(f"Linked: {name} -> {target}")
    try:  # Also when a link was kept: a real folder here drops the shared rule.
        regenerate_exclude_block(repository)
    except (OSError, ValueError) as exc:
        notes.append(f"Could not update the Git exclude file: {exc}")
    return notes


def unlink_workspace(repository: Path) -> None:
    for link in owned_links(repository):
        link.unlink()
