"""The specify project link command."""
from __future__ import annotations

import os
from pathlib import Path

import typer

from ..workspace import (
    atomic_json, checkout_record_path, confined, feature_selection_mode, find_repository, is_private, read_json,
    verify_workspace,
)
from ..workspace_git import run_git


def _unattached_checkout() -> Path | None:
    """Return the Git checkout root when it has no project locator."""
    try:
        top = run_git(Path.cwd(), "rev-parse", "--show-toplevel", allowed_codes=(0, 128))
    except OSError:
        return None
    if not top or (Path(top) / ".specify/project.json").exists():
        return None
    return Path(top).resolve()


def _attach(repository: Path, workspace: Path):
    """Attach a checkout without a locator to the workspace. Return the workspace, the restore result, and no feature."""
    from ..assets import get_speckit_version
    from ..private_checkout import (
        Restore, attach_integrations, plan_checkout_files, refuse_tracked, regenerate_exclude_block, restore_checkout,
    )
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
        return workspace, restore_checkout(repository, workspace, None), None
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
    atomic_json(checkout_record_path(repository), {
        "schema_version": 1, "workspace": str(workspace), "active_feature": None,
    })
    attach_integrations(repository, keys, version=version)
    regenerate_exclude_block(repository)
    return workspace, Restore(0, {}, False, False, None), None


def _relink(workspace: Path):
    """Relink a checkout with a locator. Return the workspace, the restore result, and the kept feature."""
    from ..assets import get_speckit_version
    from ..private_checkout import Restore, plan_reconcile, reconcile_checkout, restore_checkout

    repository = find_repository()
    if (repository / ".specify").is_symlink():
        raise ValueError("The repository locator directory must not be a symlink.")
    locator = read_json(repository / ".specify/project.json")
    if type(locator.get("schema_version")) is not int or locator["schema_version"] != 1 or locator.get("storage") != "external":
        raise ValueError("The project locator must select external storage with schema version 1.")
    project_id = locator.get("project_id")
    if not isinstance(project_id, str):
        raise ValueError("The project locator must contain a valid project ID.")
    record_path = checkout_record_path(repository)
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
    if not is_private(workspace):
        return workspace, restore_checkout(repository, workspace, active), active
    version = get_speckit_version()
    remove, install, modified = plan_reconcile(repository, workspace, version=version)
    atomic_json(record_path, {"schema_version": 1, "workspace": str(workspace), "active_feature": active})
    reconcile_checkout(repository, remove, install, version=version)
    return workspace, Restore(0, modified, False, False, None), active


def register(app: typer.Typer) -> None:
    @app.command("link")
    def link(workspace: Path = typer.Argument(help="Existing external workspace for this project.")) -> None:
        """Link this checkout to an existing project workspace.

        A checkout without a project locator takes the project from the workspace.
        Missing agent commands, helpers, templates, and bundled package files come back. Modified files stay.
        """
        try:
            unattached = None if os.environ.get("SPECIFY_INIT_DIR") else _unattached_checkout()
            workspace, result, active = _attach(unattached, workspace) if unattached is not None else _relink(workspace)
            select_next = active is None and feature_selection_mode(workspace) == "automatic"
        except KeyboardInterrupt:
            typer.echo("Error: Interrupted.", err=True)
            raise typer.Exit(130) from None
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Workspace: {workspace}")
        if result.restored:
            typer.echo(f"Restored: {result.restored} files")
        for key, files in result.modified.items():
            for name in files:
                typer.echo(f"Kept modified file: {name}")
            typer.echo(f"Repair: specify integration upgrade {key} --force")
        for repair, package in result.packages.items():
            typer.echo(f"Missing package files: {package}")
            typer.echo(f"Repair: {repair}")
        if result.locator_written:
            typer.echo("Locator written: .specify/project.json. Commit it so teammates can link.")
        if result.ignore_updated:
            typer.echo("Updated: .gitignore. Commit it so other checkouts get the ignore line.")
        if result.ignore_error:
            typer.echo(f"Error: {result.ignore_error} Add /.specify/checkout.json to .gitignore by hand.", err=True)
            raise typer.Exit(1)
        if select_next:
            typer.echo("Next: specify project select <feature>")
