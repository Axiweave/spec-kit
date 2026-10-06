"""The specify project unlink command."""
from __future__ import annotations

import typer

from ..workspace import find_repository
from .move import unlink_checkout


def register(app: typer.Typer) -> None:
    @app.command("unlink")
    def unlink() -> None:
        """Detach this checkout from its external workspace. The workspace does not change."""
        try:
            removed, tracked = unlink_checkout(find_repository())
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        for relative in removed:
            typer.echo(f"Removed: {relative}")
        if tracked:
            typer.echo(
                "Warning: Git tracks .specify/project.json. "
                "Committing this deletion detaches every teammate's checkout."
            )
        typer.echo("Next: specify init --here --storage local, or specify project link <backup>")
