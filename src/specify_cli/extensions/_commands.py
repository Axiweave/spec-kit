"""Shared infrastructure and registration for ``specify extension`` commands.

Command handlers belong in ``command_*.py`` modules. Keep helpers here only
when multiple commands share them. Flows that other packages use live in
public modules such as ``install.py``. Compatibility shims re-fetch package
helpers at call time so existing monkeypatch paths keep working. Cohesive
private phases use ``_command_<name>_*.py`` modules.
"""
from __future__ import annotations

from typing import Optional

import typer
import yaml as yaml
from rich.markup import escape as _escape_markup
from rich.table import Table

from ..terminal import console
from .. import get_speckit_version as get_speckit_version

extension_app = typer.Typer(
    name="extension",
    help="Manage spec-kit extensions",
    add_completion=False,
)

# Root helpers re-fetched at call time so test monkeypatching of
# `specify_cli.<name>` keeps working after the move.
def _require_specify_project(*args, **kwargs):
    from .. import require_specify_project as _f
    return _f(*args, **kwargs)


def load_init_options(*args, **kwargs):
    from .. import load_init_options as _f
    return _f(*args, **kwargs)


def _display_project_path(*args, **kwargs):
    from .. import _display_project_path as _f
    return _f(*args, **kwargs)


def _command_safe_id(raw_id: object, placeholder: str = "<extension-id>") -> str:
    """Return an extension ID that is safe to embed in a suggested shell command.

    Catalog entries (especially from discovery-only catalogs) are untrusted:
    their keys are not validated during catalog merge, so an ``id`` like
    ``foo; rm -rf ~`` could otherwise be interpolated into a command we
    explicitly encourage the user to copy and run. ``rich.markup.escape`` only
    neutralizes Rich markup, not shell metacharacters, so it is not sufficient
    here. Only emit the real ID when it matches the same
    lowercase-alphanumeric-and-hyphen rule ``ExtensionManifest`` enforces
    (``^[a-z0-9-]+$``); otherwise fall back to a literal placeholder so the
    printed command never carries catalog-controlled shell text.

    A leading hyphen is additionally rejected: an ID like ``--force`` satisfies
    the pattern but Typer would parse it as an option rather than the positional
    extension argument, yielding a non-copyable or option-altering command.
    """
    from . import VALID_EXTENSION_ARTIFACT_NAME_PATTERN

    text = str(raw_id)
    if text.startswith("-"):
        return placeholder
    if VALID_EXTENSION_ARTIFACT_NAME_PATTERN.match(text):
        return text
    return placeholder


def _bundled_update_source(*args, **kwargs):
    """Forward calls to the update command's bundled-source helper."""
    from ._command_update_discovery import _bundled_update_source as _helper

    return _helper(*args, **kwargs)


def _archive_extension_directory(*args, **kwargs):
    """Forward calls to the update command's archive helper."""
    from ._command_update_artifacts import _archive_extension_directory as _helper

    return _helper(*args, **kwargs)


def _resolve_installed_extension(
    argument: str,
    installed_extensions: list,
    command_name: str = "command",
    allow_not_found: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """Resolve an extension argument (ID or display name) to an installed extension.

    Args:
        argument: Extension ID or display name provided by user
        installed_extensions: List of installed extension dicts from manager.list_installed()
        command_name: Name of the command for error messages (e.g., "enable", "disable")
        allow_not_found: If True, return (None, None) when not found instead of raising

    Returns:
        Tuple of (extension_id, display_name), or (None, None) if allow_not_found=True and not found

    Raises:
        typer.Exit: If extension not found (and allow_not_found=False) or name is ambiguous
    """
    # First, try exact ID match
    for ext in installed_extensions:
        if ext["id"] == argument:
            return (ext["id"], ext["name"])

    # If not found by ID, try display name match
    name_matches = [ext for ext in installed_extensions if ext["name"].lower() == argument.lower()]

    if len(name_matches) == 1:
        # Unique display-name match
        return (name_matches[0]["id"], name_matches[0]["name"])
    elif len(name_matches) > 1:
        # Ambiguous display-name match
        console.print(
            f"[red]Error:[/red] Extension name '{_escape_markup(argument)}' is ambiguous. "
            "Multiple installed extensions share this name:"
        )
        table = Table(title="Matching extensions")
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Name", style="white")
        table.add_column("Version", style="green")
        for ext in name_matches:
            table.add_row(
                _escape_markup(str(ext.get("id", ""))),
                _escape_markup(str(ext.get("name", ""))),
                _escape_markup(str(ext.get("version", ""))),
            )
        console.print(table)
        console.print("\nPlease rerun using the extension ID:")
        console.print(f"  [bold]specify extension {command_name} <extension-id>[/bold]")
        raise typer.Exit(1)
    else:
        # No match by ID or display name
        if allow_not_found:
            return (None, None)
        console.print(f"[red]Error:[/red] Extension '{_escape_markup(argument)}' is not installed")
        raise typer.Exit(1)


def extension_info(*args, **kwargs):
    """Forward direct calls to the extracted info command handler."""
    from .command_info import extension_info as _extension_info

    return _extension_info(*args, **kwargs)


def _print_extension_info(*args, **kwargs):
    """Forward calls to the extracted catalog-info renderer."""
    from .command_info import _print_extension_info as _renderer

    return _renderer(*args, **kwargs)


def register(app: typer.Typer) -> None:
    """Attach the extension command group to the root Typer app."""
    from .catalog import register as register_catalog

    register_catalog(extension_app)

    from . import command_add  # noqa: F401 — registers handler via decorator
    from . import command_disable  # noqa: F401 — registers handler via decorator
    from . import command_enable  # noqa: F401 — registers handler via decorator
    from . import command_info  # noqa: F401 — registers handler via decorator
    from . import command_list  # noqa: F401 — registers handler via decorator
    from . import command_remove  # noqa: F401 — registers handler via decorator
    from . import command_search  # noqa: F401 — registers handler via decorator
    from . import command_set_priority  # noqa: F401 — registers handler via decorator
    from . import command_update  # noqa: F401 — registers handler via decorator

    app.add_typer(extension_app, name="extension")
