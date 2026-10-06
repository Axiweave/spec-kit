"""Implementation of the ``specify preset update`` command."""

from __future__ import annotations

import typer

from ..terminal import console
from . import _commands
from ._commands import preset_app


@preset_app.command("update")
def preset_update(
    preset_id: str = typer.Argument(..., help="Installed preset ID to replace"),
    from_url: str = typer.Option(
        None,
        "--from",
        help="Install the replacement from a .zip, .tar.gz, or .tgz URL",
    ),
    dev: str = typer.Option(
        None,
        "--dev",
        help="Install the replacement from a local directory (development mode)",
    ),
    priority: int = typer.Option(
        10,
        "--priority",
        help="Resolution priority for the replacement (default 10)",
    ),
):
    """Restore an installed preset and preserve custom files."""

    if from_url is not None and dev is not None:
        console.print("[red]Error:[/red] --from and --dev are mutually exclusive")
        raise typer.Exit(1)
    if from_url == "":
        console.print("[red]Error:[/red] --from must not be empty")
        raise typer.Exit(1)
    if dev == "":
        console.print("[red]Error:[/red] --dev must not be empty")
        raise typer.Exit(1)

    _commands._install_preset(
        preset_id=preset_id,
        from_url=from_url,
        dev=dev,
        priority=priority,
        force=True,
    )
