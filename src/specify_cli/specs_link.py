"""The `specs` symlink from a checkout with external storage to its workspace specs."""
from __future__ import annotations

import os
from pathlib import Path


def is_specs_link(repository: Path, workspace: Path) -> bool:
    link = repository / "specs"
    return link.is_symlink() and Path(os.readlink(link)) == workspace / "specs"


def link_specs(repository: Path, workspace: Path) -> str | None:
    """Point `<repository>/specs` at `<workspace>/specs` unless the `specs_link` default is false.

    Git does not see the link: the managed exclude block hides it. Return a line for the user, or None.
    """
    from .private_checkout import regenerate_exclude_block
    from .user_config import load_defaults

    if load_defaults().get("specs_link") == "false":
        return None
    link, target = repository / "specs", workspace / "specs"
    if is_specs_link(repository, workspace):
        return None
    if link.is_symlink() or link.exists():
        return f"Kept {link}. Remove it to get the link to {target}."
    try:
        target.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target, target_is_directory=True)
        regenerate_exclude_block(repository)
    except (OSError, ValueError) as exc:
        return f"Could not link specs: {exc}"
    return f"Linked: specs -> {target}"


def unlink_specs(repository: Path) -> None:
    """Remove the `specs` link to the workspace that this checkout's record names."""
    from .workspace import checkout_record_path, read_json

    record = checkout_record_path(repository)
    if record.is_file() and isinstance(workspace := read_json(record).get("workspace"), str):
        if is_specs_link(repository, Path(workspace)):
            (repository / "specs").unlink()
