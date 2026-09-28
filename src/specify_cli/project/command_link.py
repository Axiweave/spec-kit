"""The specify project link command."""
from __future__ import annotations

from pathlib import Path

import typer

from ..workspace import atomic_json, confined, find_repository, project_record_path, read_json, verify_workspace


def register(app: typer.Typer) -> None:
    @app.command("link")
    def link(workspace: Path = typer.Argument(help="Existing external workspace for this project.")) -> None:
        """Link this machine to an existing project workspace."""
        try:
            repository = find_repository()
            if (repository / ".specify").is_symlink():
                raise ValueError("The repository locator directory must not be a symlink.")
            locator = read_json(repository / ".specify/project.json")
            if type(locator.get("schema_version")) is not int or locator["schema_version"] != 1 or locator.get("storage") != "external":
                raise ValueError("The project locator must select external storage with schema version 1.")
            project_id = locator.get("project_id")
            if not isinstance(project_id, str):
                raise ValueError("The project locator must contain a valid project ID.")
            record_path = project_record_path(project_id)
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
            atomic_json(record_path, {"schema_version": 1, "workspace": str(workspace), "active_feature": active})
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Workspace: {workspace}")
