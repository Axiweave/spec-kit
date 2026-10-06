"""Registration for project commands."""
from __future__ import annotations

import typer

from . import (
    command_command, command_info, command_link, command_merge_specs, command_migrate_naming, command_move,
    command_recover, command_select, command_unlink,
)

project_app = typer.Typer(help="Inspect and manage project storage and feature naming.", add_completion=False)
command_info.register(project_app)
command_link.register(project_app)
command_unlink.register(project_app)
command_select.register(project_app)
command_command.register(project_app)
command_move.register(project_app)
command_migrate_naming.register(project_app)
command_merge_specs.register(project_app)
command_recover.register(project_app)


def register(app: typer.Typer) -> None:
    app.add_typer(project_app, name="project")
