"""The specify project recover command."""
from __future__ import annotations

import json

import typer

from ..workspace import find_repository, workspace_root_for
from . import _recover


def register(app: typer.Typer) -> None:
    @app.command("recover")
    def recover(
        apply: bool = typer.Option(False, "--apply", help="Restore rename and merge leftovers whose owner stopped."),
        json_output: bool = typer.Option(False, "--json", help="Print the leftover resources as JSON."),
    ) -> None:
        """Report, or restore, what an interrupted rename, merge, or move left behind."""
        try:
            repository = find_repository()
            workspace = workspace_root_for(repository)
            rows = _recover.apply(repository, workspace) if apply else _recover.scan(repository, workspace)
        except (OSError, ValueError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        failed = any(row["refusals"] for row in rows) if apply else bool(rows)
        if json_output:
            typer.echo(json.dumps({"resources": rows}))
            raise typer.Exit(1 if failed else 0)
        if not rows:
            typer.echo("No leftover resources.")
        for row in rows:
            if not apply:
                typer.echo(f"{row['kind']}: {row['path']}")
                typer.echo(f"  owner_pid: {'null' if row['owner_pid'] is None else row['owner_pid']}")
                typer.echo(f"  owner: {row['owner']}")
                for choice in row["choices"]:
                    typer.echo(f"  choice: {choice}")
            elif row["refusals"]:
                for refusal in row["refusals"]:
                    typer.echo(f"Error: {refusal}", err=True)
            else:
                typer.echo(f"{'Removed' if row['kind'].endswith('-lock') else 'Restored'}: {row['path']}")
        raise typer.Exit(1 if failed else 0)
