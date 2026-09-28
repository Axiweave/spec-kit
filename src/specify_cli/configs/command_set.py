"""The specify config set command."""
from __future__ import annotations

import typer

from ..user_config import set_default


def register(app: typer.Typer) -> None:
    @app.command("set")
    def set_value(
        key: str = typer.Argument(help="Personal default name."),
        value: str = typer.Argument(help="Saved value. An empty string restores interactive prompting."),
    ) -> None:
        """Save one personal default without changing other settings."""
        try:
            set_default(key, value)
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
