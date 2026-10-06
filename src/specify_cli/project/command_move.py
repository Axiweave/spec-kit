"""The specify project move command."""
from __future__ import annotations

import os
from pathlib import Path

import typer

from ..workspace import find_repository
from .move import commit_local, commit_move, prepare_local, prepare_move


def register(app: typer.Typer) -> None:
    @app.command("move")
    def move(
        workspace: Path | None = typer.Argument(None, help="Empty destination for the external workspace."),
        to_local: bool = typer.Option(
            False, "--to-local",
            help="Copy the external project back into this checkout. The workspace does not change.",
        ),
        confirm_remove_local: bool = typer.Option(
            False, "--confirm-remove-local",
            help="Remove local originals after verification without another confirmation.",
        ),
        no_workspace_git: bool = typer.Option(
            False, "--no-workspace-git",
            help="Skip creating a Git history for the external workspace.",
        ),
    ) -> None:
        """Move Spec Kit work between local storage and an external workspace."""
        if (workspace is not None) == to_local:
            typer.echo("Error: Pass exactly one of WORKSPACE and --to-local.", err=True)
            raise typer.Exit(1)
        if to_local:
            try:
                prepared = prepare_local(find_repository())
                verified, shared = commit_local(prepared)
            except KeyboardInterrupt as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(130) from None
            except (OSError, ValueError) as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1) from exc
            typer.echo(
                f"Copied {len(prepared.files)} files from {prepared.workspace_root}. "
                f"Verified {verified} files. The workspace was not changed."
            )
            if shared:
                typer.echo(f"Shared files outside the repository keep the workspace note: {os.path.commonpath(shared)}")
            return
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
                    typer.echo(f"The verified staged copy remains at {prepared.workspace_root}.", err=True)
                    typer.echo("To retry, delete the staged copy or choose another empty destination.", err=True)
                    raise typer.Exit(1)
            report: dict[str, str] = {}
            try:
                project = commit_move(prepared, report=report)
            except KeyboardInterrupt as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(130) from None
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
