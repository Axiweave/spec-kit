"""Registration for feature commands."""
from __future__ import annotations

import typer

from . import command_list

feature_app = typer.Typer(help="List the features of this project.", add_completion=False)
command_list.register(feature_app)


def register(app: typer.Typer) -> None:
    app.add_typer(feature_app, name="feature")
