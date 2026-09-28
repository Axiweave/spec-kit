"""The specify config get command."""
from __future__ import annotations

import typer

from ..user_config import get_default


def register(app: typer.Typer) -> None:
    @app.command("get")
    def get(key: str = typer.Argument(help="Personal default name.")) -> None:
        """Print a saved default, or an empty value when absent."""
        try:
            value = get_default(key)
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(value)
