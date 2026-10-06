"""The specify project link command."""
from __future__ import annotations

import os
from pathlib import Path

import typer

from ..workspace import (
    atomic_json, confined, find_repository, is_private, project_record_path, read_json, verify_workspace,
)
from ..workspace_git import _git


def _unattached_checkout() -> Path | None:
    """Return the Git checkout root when it has no project locator."""
    try:
        top = _git(Path.cwd(), "rev-parse", "--show-toplevel", allowed_codes=(0, 128))
    except OSError:
        return None
    if not top or (Path(top) / ".specify/project.json").exists():
        return None
    return Path(top).resolve()


def _attach(repository: Path, workspace: Path) -> Path:
    """Attach a checkout without a locator to a private workspace. Return the workspace."""
    from .._assets import get_speckit_version
    from .._private_checkout import attach_integrations, plan_checkout_files, refuse_tracked, regenerate_exclude_block
    from ..integration_state import installed_integration_keys, try_read_integration_json

    workspace = workspace.expanduser().resolve()
    identity = workspace / ".specify/workspace.json"
    if not identity.is_file():
        raise ValueError(f"{workspace} is not a Spec Kit workspace.")
    project_id = read_json(identity).get("project_id")
    if not isinstance(project_id, str):
        raise ValueError(f"{workspace} is not a Spec Kit workspace.")
    workspace = verify_workspace(workspace, project_id)
    if not is_private(workspace):
        raise ValueError(
            "This workspace does not use private mode. Copy the project locator into this checkout, then run link again."
        )
    state, error = try_read_integration_json(workspace)
    if error is not None:
        raise ValueError(f"Cannot read the workspace integration state: {error.detail}")
    keys = installed_integration_keys(state or {})
    version = get_speckit_version()
    refuse_tracked(repository, plan_checkout_files(
        lambda staged: attach_integrations(staged, keys, version=version), workspace=workspace,
    ))
    atomic_json(repository / ".specify/project.json", {
        "schema_version": 1, "project_id": project_id, "storage": "external",
    }, exclusive=True)
    atomic_json(project_record_path(project_id), {
        "schema_version": 1, "workspace": str(workspace), "active_feature": None,
    })
    attach_integrations(repository, keys, version=version)
    regenerate_exclude_block(repository)
    return workspace


def register(app: typer.Typer) -> None:
    @app.command("link")
    def link(workspace: Path = typer.Argument(help="Existing external workspace for this project.")) -> None:
        """Link this machine to an existing project workspace.

        A checkout without a project locator attaches to a private-mode workspace.
        """
        try:
            unattached = None if os.environ.get("SPECIFY_INIT_DIR") else _unattached_checkout()
            if unattached is not None:
                workspace = _attach(unattached, workspace)
                typer.echo(f"Workspace: {workspace}")
                return
            repository = find_repository()
            if (repository / ".specify").is_symlink():
                raise ValueError("The repository locator directory must not be a symlink.")
            locator = read_json(repository / ".specify/project.json")
            if type(locator.get("schema_version")) is not int or locator["schema_version"] != 1 or locator.get("storage") != "external":
                raise ValueError("The project locator must select external storage with schema version 1.")
            project_id = locator.get("project_id")
            if not isinstance(project_id, str):
                raise ValueError("The project locator must contain a valid project ID.")
            record_path = project_record_path(project_id)
            workspace = verify_workspace(workspace, project_id)
            active = None
            if record_path.is_file():
                try:
                    previous = read_json(record_path)
                    selected = previous.get("active_feature")
                    if isinstance(selected, str) and selected and not Path(selected).is_absolute():
                        if confined(workspace, selected).is_dir():
                            active = selected
                except ValueError:
                    # An explicit link repairs a broken machine record after identity validation.
                    pass
            atomic_json(record_path, {"schema_version": 1, "workspace": str(workspace), "active_feature": active})
            if is_private(workspace):
                from .._assets import get_speckit_version
                from .._private_checkout import reconcile_checkout
                reconcile_checkout(repository, version=get_speckit_version())
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Workspace: {workspace}")
