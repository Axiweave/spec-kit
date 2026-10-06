"""The specify project select command."""
from __future__ import annotations

import typer

from ..workspace import confined, feature_selection_mode, resolve_project, save_active_feature

_CONTEXT_MODE = (
    "Saved selection is ignored in context mode. Pass SPECIFY_FEATURE_DIRECTORY, "
    "or set feature_selection to automatic."
)


def register(app: typer.Typer) -> None:
    @app.command("select")
    def select(feature: str = typer.Argument(..., help="Feature directory, for example specs/<feature>.")) -> None:
        """Save the active feature for this checkout."""
        try:
            project = resolve_project(select_feature=False)
            if feature_selection_mode(project.workspace_root) == "context":
                raise ValueError(_CONTEXT_MODE)
            root = project.workspace_root.resolve()
            if (root / feature).is_symlink():
                raise ValueError(f"Feature directory must not be a symlink: {feature}")
            path = confined(root, feature)
            if not path.is_dir():
                raise ValueError(f"Feature directory not found: {feature}")
            save_active_feature(project, path)
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Selected: {path.relative_to(root).as_posix()}")
