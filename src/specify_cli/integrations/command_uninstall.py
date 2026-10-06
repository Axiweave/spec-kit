"""The ``specify integration uninstall`` command."""
from __future__ import annotations


import typer

from .._console import console
from .._utils import _display_project_path
from ..integration_state import default_integration_key as _default_integration_key, installed_integration_keys as _installed_integration_keys, integration_settings as _integration_settings
from ._commands import integration_app
from ._helpers import _MANIFEST_READ_ERRORS, _clear_init_options_for_integration, _read_integration_json, _remove_integration_json, _resolve_integration_options, _set_default_integration_or_exit, _write_integration_json


@integration_app.command("uninstall")
def integration_uninstall(
    key: str = typer.Argument(None, help="Integration key to uninstall (default: current integration)"),
    force: bool = typer.Option(False, "--force", help="Remove files even if modified"),
    global_install: bool = typer.Option(False, "--global", help="Remove unchanged shared OMP commands without a project"),
    project: bool = typer.Option(
        False, "--project", help="Private mode: also remove the integration from the project. Other checkouts keep their files.",
    ),
):
    """Uninstall an integration, safely preserving modified files."""
    from . import get_integration
    from .manifest import IntegrationManifest
    from .. import _require_specify_project

    if global_install:
        from .omp.global_commands import run_global_command
        run_global_command(key, "uninstall")
        return

    project_root = _require_specify_project()
    from ..workspace import is_private, workspace_root_for
    workspace = workspace_root_for(project_root)
    private = workspace != project_root and is_private(workspace)
    if project and not private:
        console.print("[red]Error:[/red] --project applies only to private mode.")
        raise typer.Exit(1)
    current = _read_integration_json(project_root)
    default_key = _default_integration_key(current)
    installed_keys = _installed_integration_keys(current)

    if key is None:
        if not default_key:
            console.print("[yellow]No integration is currently installed.[/yellow]")
            raise typer.Exit(0)
        key = default_key

    if key not in installed_keys:
        console.print(f"[red]Error:[/red] Integration '{key}' is not installed.")
        raise typer.Exit(1)

    integration = get_integration(key)
    if private and not project:
        from .._private_checkout import remove_checkout_integration
        try:
            removed, skipped = remove_checkout_integration(project_root, key, force=force)
        except _MANIFEST_READ_ERRORS as exc:
            console.print(f"[red]Error:[/red] Integration manifest for '{key}' is unreadable: {exc}")
            raise typer.Exit(1)
        console.print(
            f"Removed {key} from this checkout. The project still lists {key}. "
            f"To remove it from the project, run: specify integration uninstall {key} --project",
            markup=False,
        )
        _print_removal(project_root, removed, skipped)
        return

    manifest_path = IntegrationManifest(key, project_root).manifest_path
    if not manifest_path.exists():
        console.print(f"[yellow]No manifest found for integration '{key}'. Nothing to uninstall.[/yellow]")
        remaining = [installed for installed in installed_keys if installed != key]
        new_default = default_key if default_key != key else (remaining[0] if remaining else None)
        if remaining:
            if default_key == key and new_default and (new_integration := get_integration(new_default)):
                raw_options, parsed_options = _resolve_integration_options(
                    new_integration, current, new_default, None
                )
                _set_default_integration_or_exit(
                    project_root,
                    current,
                    new_default,
                    new_integration,
                    remaining,
                    raw_options=raw_options,
                    parsed_options=parsed_options,
                )
            else:
                _write_integration_json(
                    project_root, new_default, remaining, _integration_settings(current)
                )
        else:
            _remove_integration_json(project_root)
        if default_key == key:
            _clear_init_options_for_integration(project_root, key)
        raise typer.Exit(0)

    try:
        manifest = IntegrationManifest.load(key, project_root)
    except _MANIFEST_READ_ERRORS as exc:
        console.print(f"[red]Error:[/red] Integration manifest for '{key}' is unreadable.")
        console.print(f"Manifest: {manifest_path}")
        console.print(
            f"To recover, delete the unreadable manifest, run "
            f"[cyan]specify integration uninstall {key}[/cyan] to clear stale metadata, "
            f"then run [cyan]specify integration install {key}[/cyan] to regenerate."
        )
        console.print(f"[dim]Details:[/dim] {exc}")
        raise typer.Exit(1)

    if not integration:
        console.print(
            f"[yellow]Warning:[/yellow] Integration '{key}' not found "
            "in registry. Falling back to manifest-based cleanup."
        )
        removed, skipped = manifest.uninstall(project_root, force=force)
    else:
        removed, skipped = integration.teardown(project_root, manifest, force=force)

    remaining = [installed for installed in installed_keys if installed != key]
    new_default = default_key if default_key != key else (remaining[0] if remaining else None)
    if remaining:
        if default_key == key and new_default and (new_integration := get_integration(new_default)):
            raw_options, parsed_options = _resolve_integration_options(
                new_integration, current, new_default, None
            )
            _set_default_integration_or_exit(
                project_root,
                current,
                new_default,
                new_integration,
                remaining,
                raw_options=raw_options,
                parsed_options=parsed_options,
            )
        else:
            _write_integration_json(
                project_root, new_default, remaining, _integration_settings(current)
            )
    else:
        _remove_integration_json(project_root)

    if default_key == key:
        _clear_init_options_for_integration(project_root, key)

    name = (integration.config or {}).get("name", key) if integration else key
    console.print(f"\n[green]✓[/green] Integration '{name}' uninstalled")
    _print_removal(project_root, removed, skipped)
    if project:
        console.print(
            f"Other checkouts keep their {key} files. Run specify project link in each one to remove them.",
            markup=False,
        )


def _print_removal(project_root, removed, skipped) -> None:
    if removed:
        console.print(f"  Removed {len(removed)} file(s)")
    if skipped:
        console.print(f"\n[yellow]⚠[/yellow]  {len(skipped)} modified file(s) were preserved:")
        for path in skipped:
            rel = _display_project_path(project_root, path)
            console.print(f"    {rel}")
