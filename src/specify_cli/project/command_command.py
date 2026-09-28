"""The specify project command runtime resolver."""
from __future__ import annotations

import json

import typer

from ..agents import CommandRegistrar
from ..presets import PresetValidationError
from ..workspace import resolve_project


_FEATURE_COMMANDS = frozenset({
    "speckit.analyze", "speckit.checklist", "speckit.clarify", "speckit.converge",
    "speckit.implement", "speckit.plan", "speckit.tasks", "speckit.taskstoissues",
})


def register(app: typer.Typer) -> None:
    @app.command("command")
    def command(
        name: str = typer.Argument(help="Canonical command name, such as speckit.plan."),
        json_output: bool = typer.Option(False, "--json", help="Print command content and paths as JSON."),
    ) -> None:
        """Resolve a command from the current project's active preset and extension stack."""
        try:
            project = resolve_project()
            content = CommandRegistrar().render_project_command(name, project.repository_root)
            feature = project.feature_dir
            if name in _FEATURE_COMMANDS:
                if feature is None:
                    raise ValueError("No active feature. Run speckit.specify or set SPECIFY_FEATURE_DIRECTORY.")
                if not feature.is_dir():
                    raise ValueError(f"Feature directory is unavailable: {feature}")
            result = {
                "content": content,
                "repository_root": str(project.repository_root),
                "workspace_root": str(project.workspace_root),
                "feature_dir": str(feature.resolve()) if feature else None,
            }
        except (ValueError, OSError, PresetValidationError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(json.dumps(result) if json_output else content)
