"""The specify feature list command."""
from __future__ import annotations

import json
from typing import Any

import typer

from ..workspace import resolve_project
from .todo import STAGES, report


def _first_finding(feature: dict[str, Any]) -> str:
    finding = feature["findings"][0]
    return f"{finding['file']}: {finding['reason']}"


def _next_line(data: dict[str, Any]) -> str:
    recommendation = data["recommendation"]
    if recommendation["feature"] is None:
        return f"Next: {recommendation['next_step']} ({recommendation['rule']})"
    target = recommendation["next_step"]
    if target is None:
        feature = next(item for item in data["features"] if item["path"] == recommendation["feature"])
        target = _first_finding(feature)
    return f"Next: {recommendation['feature']} -> {target} ({recommendation['rule']})"


def _row(feature: dict[str, Any]) -> str:
    parts = [feature["path"], STAGES[feature["stage"]][0]]
    tasks = feature["tasks"]
    if tasks is not None and tasks["total"]:
        ids = f" ({', '.join(tasks['open_ids'])})" if tasks["open_ids"] else ""
        parts.append(f"{tasks['checked']}/{tasks['total']}{ids}")
    markers = feature["markers"]
    if markers:
        shown = ", ".join(f"{item['file']}:{item['line']}" for item in markers[:3])
        parts.append(f"{len(markers)} marker{'s' if len(markers) != 1 else ''} ({shown})")
    requirements = feature["requirements"]
    if requirements is not None and requirements["open"]:
        parts.append(f"{requirements['file']}: {requirements['open']} open")
    if tasks is not None and tasks["total"] == 0:
        parts.append("tasks.md: empty task list")
    parts.extend(f"{item['file']}: {item['reason']}" for item in feature["findings"])
    if feature["next_step"] is not None:
        parts.append(f"next: {feature['next_step']}")
    return "  ".join(parts)


def register(app: typer.Typer) -> None:
    @app.command("list")
    def list_features(json_output: bool = typer.Option(False, "--json", help="Print the report as JSON.")) -> None:
        """List each feature with its stage, blockers, and next step. Change nothing."""
        try:
            data = report(resolve_project().workspace_root)
        except (ValueError, OSError) as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(1) from exc
        if json_output:
            typer.echo(json.dumps(data))
            return
        typer.echo(_next_line(data))
        entry = data["roadmap_recommendation"]
        if entry is not None:
            typer.echo(f"Next roadmap entry: {entry['roadmap']} {entry['entry']} -> {entry['next_step']}")
        unfinished = [feature for feature in data["features"] if feature["stage"] != "done"]
        done = [feature["path"] for feature in data["features"] if feature["stage"] == "done"]
        for feature in unfinished:
            typer.echo(_row(feature))
        for feature in unfinished:
            for item in feature["custom_checklists"]:
                if item["open"]:
                    typer.echo(f"{feature['path']}  {item['file']}: {item['open']} open (owner: reviewer)")
        if done:
            typer.echo(f"Done ({len(done)}): {', '.join(done)}")
        for group in data["duplicate_prefixes"]:
            typer.echo(f"Duplicate feature prefix: {', '.join(group)}")
        for item in data["roadmap_findings"]:
            line = "-" if item["line"] is None else item["line"]
            typer.echo(f"Roadmap {item['file']}:{line}  {item['kind']}  {item['entry'] or '-'}  {item['message']}")
