"""Registration for personal configuration commands."""
from __future__ import annotations

import typer

from . import command_clear, command_get, command_set

config_app = typer.Typer(help="Manage personal defaults for new projects.", add_completion=False)
command_get.register(config_app)
command_set.register(config_app)
command_clear.register(config_app)


def register(app: typer.Typer) -> None:
    app.add_typer(config_app, name="config")
