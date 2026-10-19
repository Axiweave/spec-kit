"""Shared project-resolution helpers for the Specify CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import NoReturn

import typer
from rich.markup import escape

from ._file_policy import ProjectPolicyError, project_files
from .file_helper import FileHelper
from .terminal import err_console
from .workspace import Project, ProjectNotFoundError, find_repository, resolve_project, workspace_root_for

try:
    from typer._click.globals import get_current_context
except ModuleNotFoundError as error:
    if error.name != "typer._click":
        raise
    from click import get_current_context


class ProjectResolutionError(RuntimeError):
    """A project-root error that callers can render for their own surface."""


def _resolve_init_dir_override_unrendered() -> Path | None:
    """Resolve ``SPECIFY_INIT_DIR`` without emitting user-facing output."""
    raw = os.environ.get("SPECIFY_INIT_DIR", "")
    if not raw:
        return None
    try:
        init_root = (Path.cwd() / raw).resolve()
    except (OSError, RuntimeError) as exc:
        raise ProjectResolutionError(
            f"Cannot resolve SPECIFY_INIT_DIR '{raw}': {exc}"
        ) from exc
    if not init_root.is_dir():
        raise ProjectResolutionError(
            f"SPECIFY_INIT_DIR does not point to an existing directory: {raw}"
        )
    preflight_project_policy(init_root)
    if not (init_root / ".specify").is_dir():
        raise ProjectResolutionError(
            "SPECIFY_INIT_DIR is not a Spec Kit project "
            f"(no .specify/ directory): {init_root}"
        )
    return init_root


def preflight_project_policy(project_root: Path) -> None:
    """Validate at the resolved project boundary without printing or caching."""
    try:
        project_files(project_root)
    except ProjectPolicyError as exc:
        marker = project_root / ".specify"
        if marker.is_symlink():
            raise ProjectResolutionError(
                f"Refusing to use symlinked .specify path: {marker}. {exc}"
            ) from exc
        if marker.exists() and not marker.is_dir():
            raise ProjectResolutionError(
                f"Not a Spec Kit project: .specify path exists but is not a directory: {marker}"
            ) from exc
        raise ProjectResolutionError(str(exc)) from exc


def _resolve_init_dir_override() -> Path | None:
    """Resolve the ``SPECIFY_INIT_DIR`` project override for the Python CLI.

    Applies the same validation rules as the shell resolver
    (``resolve_specify_init_dir`` in ``scripts/bash/common.sh``): the value names
    the project root — the directory *containing* ``.specify/`` — and is strict.
    Relative paths resolve against the current directory; the path must exist and
    contain ``.specify/``, otherwise this hard-errors with no fallback to cwd
    (which would silently operate on the wrong project's files). The error
    messages mirror the shell resolver's wording (rendered here as a Rich
    ``Error:`` line, plain ``ERROR:`` in the shell) so the two surfaces read
    consistently.

    Returns the validated absolute project root, or ``None`` when the variable is
    unset/empty, in which case callers keep their existing cwd-based behavior.

    Note: this canonicalizes symlinks via :meth:`Path.resolve` (physical path),
    whereas the shell ``cd -- "$X" && pwd`` keeps the logical path. The two agree
    for non-symlinked paths; a symlinked ``SPECIFY_INIT_DIR`` can resolve to
    different strings across the surfaces. The canonical form is the safer choice
    here (a stable project identity), so this is a deliberate, documented variance,
    not a parity guarantee on the resolved string.
    """
    try:
        return _resolve_init_dir_override_unrendered()
    except ProjectResolutionError as error:
        render_project_error(error)


def _find_repository(override: Path | None, *, allow_standalone: bool = False) -> Path | None:
    """Find the repository root and validate its file policy at the boundary.

    Returns ``None`` for a standalone cwd when *allow_standalone* is set.
    """
    try:
        repository = find_repository(override)
    except ProjectNotFoundError as error:
        if override is None:
            preflight_project_policy(Path.cwd())
            if allow_standalone:
                return None
        raise ProjectResolutionError(
            "Not a Spec Kit project (no .specify/ directory)"
        ) from error
    except ValueError as error:
        raise ProjectResolutionError(str(error)) from error
    preflight_project_policy(repository)
    return repository


def _resolve_project(*, allow_standalone: bool = False) -> Project:
    """Resolve both roots, with a cwd fallback only outside a project."""
    override = _resolve_init_dir_override_unrendered()
    repository = _find_repository(override, allow_standalone=allow_standalone)
    if repository is None:
        root = Path.cwd()
        return Project(root, root, None, None)
    try:
        return resolve_project(repository)
    except (OSError, ValueError) as error:
        raise ProjectResolutionError(str(error)) from error


def resolve_specify_project_root() -> Path:
    """Verify repository/workspace roots without requiring feature selection."""
    repository = _find_repository(_resolve_init_dir_override_unrendered())
    try:
        workspace_root_for(repository)
    except (OSError, ValueError) as error:
        raise ProjectResolutionError(str(error)) from error
    return repository


def render_project_error(error: ProjectResolutionError) -> NoReturn:
    """Render a project failure on the current command's error surface."""
    context = get_current_context(silent=True)
    if context is not None and (
        context.params.get("json_output") is True or context.params.get("as_json") is True
    ):
        typer.echo(json.dumps({"error": str(error)}, ensure_ascii=False), err=True)
    else:
        err_console.print(f"[red]Error:[/red] {escape(str(error))}")
    raise typer.Exit(1) from error


def resolve_specify_project_files() -> FileHelper:
    """Resolve the active project and hand its current policy to domain consumers."""
    return project_files(resolve_specify_project_root())
