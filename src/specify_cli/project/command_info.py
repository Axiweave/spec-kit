"""The specify project info command."""
from __future__ import annotations

import json
from pathlib import Path

import typer

from ..workspace import ProjectNotFoundError, feature_selection_mode, read_json, resolve_project
from ..workspace_git import run_git
from .naming import classify_feature_name, duplicate_prefixes

_LINK_HINT = "If this checkout belongs to a project with external storage, run specify project link PATH."


def _inside_git() -> bool:
    try:
        return run_git(Path.cwd(), "rev-parse", "--is-inside-work-tree", allowed_codes=(0, 128)) == "true"
    except OSError:
        return False


def register(app: typer.Typer) -> None:
    @app.command("info")
    def info(json_output: bool = typer.Option(False, "--json", help="Print project paths as JSON.")) -> None:
        """Show the current project's workspace and choices."""
        try:
            try:
                project = resolve_project()
            except ProjectNotFoundError as exc:
                hint = f". {_LINK_HINT}" if _inside_git() else ""
                typer.echo(f"Error: {exc}{hint}", err=True)
                raise typer.Exit(1) from exc
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
                "feature_selection": feature_selection_mode(project.workspace_root),
                "integration": options.get("integration", options.get("ai")),
                "script": options.get("script"),
                "command_scope": options.get("command_scope", "project"),
                "duplicate_prefixes": duplicate_prefixes(project.workspace_root),
            }
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        if json_output:
            typer.echo(json.dumps(result))
            return
        groups = result.pop("duplicate_prefixes")
        for key, value in result.items():
            typer.echo(f"{key}: {value}")
        if not groups:
            return
        typer.echo("Duplicate feature prefixes:")
        for group in groups:
            typer.echo(f"  {', '.join(group)}")
        schemes = {classify_feature_name(group[0].rsplit("/", 1)[1])[0] for group in groups}
        if "sequential" in schemes:
            typer.echo("Rename with: specify project migrate-naming --feature-numbering timestamp --dry-run")
        if "timestamp" in schemes:
            # migrate-naming skips features that already use the timestamp scheme
            typer.echo("Rename one directory of each timestamp group by hand to a free timestamp prefix.")
