"""The specify project move command."""
from __future__ import annotations

from pathlib import Path

import typer

from ..workspace import find_repository
from .move import commit_move, prepare_move


def register(app: typer.Typer) -> None:
    @app.command("move")
    def move(
        workspace: Path = typer.Argument(help="Empty destination for the external workspace."),
        confirm_remove_local: bool = typer.Option(
            False, "--confirm-remove-local",
            help="Remove local originals after verification without another confirmation.",
        ),
        no_workspace_git: bool = typer.Option(
            False, "--no-workspace-git",
            help="Skip creating a Git history for the external workspace.",
        ),
    ) -> None:
        """Move local Spec Kit work to a verified external workspace."""
        try:
            repository = find_repository()
            typer.echo(f"Source: {repository}")
            typer.echo(f"Destination: {workspace}")
            prepared = prepare_move(repository, workspace, no_workspace_git=no_workspace_git)
            for relative in prepared.files:
                typer.echo(f"  {relative}")
            typer.echo(f"Verified {len(prepared.files)} files in {prepared.workspace_root}.")
            if not confirm_remove_local:
                try:
                    confirm_remove_local = typer.confirm(
                        "Remove the verified local originals and use this external workspace?",
                        default=False,
                    )
                except (typer.Abort, EOFError):
                    confirm_remove_local = False
                if not confirm_remove_local:
                    typer.echo(f"Move cancelled. The local project remains usable at {repository}.", err=True)
                    typer.echo(f"Spec Kit retained the verified staged copy at {prepared.workspace_root}.", err=True)
                    typer.echo(
                        "Inspect the staged copy. To complete the move, retry with an empty destination "
                        "and --confirm-remove-local.", err=True,
                    )
                    raise typer.Exit(1)
            report: dict[str, str] = {}
            project = commit_move(prepared, report=report)
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            typer.echo(
                "Spec Kit retains staged copies after a failed move. "
                "Inspect the source and destination before retrying.", err=True,
            )
            raise typer.Exit(1) from exc
        typer.echo(f"Workspace: {project.workspace_root}")
        typer.echo(
            "Spec Kit removed the local work products. "
            "The code repository's Git history remains unchanged."
        )
        if no_workspace_git:
            typer.echo("Workspace Git history setup was skipped (--no-workspace-git).")
        else:
            typer.echo(f"Workspace Git history: initial commit {report['commit_id']}.")
