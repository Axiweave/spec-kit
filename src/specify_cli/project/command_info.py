"""The specify project info command."""
from __future__ import annotations

import json

import typer

from ..workspace import read_json, resolve_project


def register(app: typer.Typer) -> None:
    @app.command("info")
    def info(json_output: bool = typer.Option(False, "--json", help="Print project paths as JSON.")) -> None:
        """Show the current project's workspace and choices."""
        try:
            project = resolve_project()
            options_path = project.workspace_root / ".specify/init-options.json"
            options = read_json(options_path) if options_path.is_file() else {}
            feature = project.feature_dir
            if feature and feature.is_relative_to(project.workspace_root):
                selected = feature.relative_to(project.workspace_root).as_posix()
            else:
                selected = str(feature) if feature else None
            result = {
                "project_id": project.project_id,
                "storage": project.storage,
                "repository_root": str(project.repository_root),
                "workspace_root": str(project.workspace_root),
                "active_feature": selected,
                "feature_numbering": options.get("feature_numbering", "sequential"),
                "integration": options.get("integration", options.get("ai")),
                "script": options.get("script"),
                "command_scope": options.get("command_scope", "project"),
            }
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        if json_output:
            typer.echo(json.dumps(result))
        else:
            for key, value in result.items():
                typer.echo(f"{key}: {value}")
