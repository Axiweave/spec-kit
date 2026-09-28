"""The specify config clear command."""
from __future__ import annotations

import typer

from ..user_config import clear_default


def register(app: typer.Typer) -> None:
    @app.command("clear")
    def clear(key: str = typer.Argument(help="Personal default name.")) -> None:
        """Remove one personal default without changing other settings."""
        try:
            clear_default(key)
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
