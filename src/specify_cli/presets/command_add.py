"""Implementation of the ``specify preset add`` command."""

from __future__ import annotations

import typer
from rich.markup import escape as _escape_markup

from .._console import console
from . import _commands
from ._commands import preset_app


def _warn_unmet_extension_dependencies(manager, manifest) -> None:
    """Warn when a preset's declared extension dependencies are unsatisfied.

    A preset whose command overrides call into an extension is inert without
    it, but the overrides still fall through to the core workflow, so nothing
    breaks -- it just silently does less than the user expects. Naming the
    missing extension and the command that installs it turns that silence into
    something actionable. See issue #4231.
    """
    from ..extensions._commands import _command_safe_id

    unmet = manager.find_unmet_extension_dependencies(manifest)
    if not unmet:
        return

    console.print()
    console.print(
        "[yellow]![/yellow]  This preset depends on extensions that are not satisfied:"
    )
    needs_catalog = False
    for dep in unmet:
        uses_catalog = False
        extension_id = _escape_markup(dep["id"])
        # The displayed id only needs Rich escaping, but a suggested command
        # has to survive Typer's parser: `^[a-z0-9-]+$` admits a leading
        # hyphen, so an id like `--force` would render as an option rather
        # than the positional argument. _command_safe_id substitutes a
        # placeholder in that case, the same way extension commands do.
        command_id = _command_safe_id(dep["id"])
        reason = dep["reason"]
        # The remediation has to match the reason. `extension add` refuses an
        # already-installed extension without --force, and `extension update`
        # only moves forward to the catalog release. A general PEP 440
        # constraint may require an exact version, an upper bound, or a
        # downgrade, so do not promise that update will satisfy it.
        if reason == "missing":
            console.print(f"    [yellow]{extension_id}[/yellow] is not installed")
            label, remedy = "Install with", f"specify extension add {command_id}"
            uses_catalog = True
        elif reason == "corrupt":
            console.print(
                f"    [yellow]{extension_id}[/yellow] has an unreadable registry entry"
            )
            # is_installed() still counts the key, so a plain add is refused.
            label = "Reinstall with"
            remedy = f"specify extension add {command_id} --force"
            uses_catalog = True
        elif reason == "stale":
            console.print(
                f"    [yellow]{extension_id}[/yellow] is registered but its "
                "files are missing"
            )
            label = "Reinstall with"
            remedy = f"specify extension add {command_id} --force"
            uses_catalog = True
        elif reason == "disabled":
            console.print(
                f"    [yellow]{extension_id}[/yellow] is installed but disabled"
            )
            label, remedy = "Enable with", f"specify extension enable {command_id}"
        else:
            console.print(
                f"    [yellow]{extension_id}[/yellow] "
                f"{_escape_markup(dep['installed'])} does not satisfy "
                f"{_escape_markup(dep['version'])}"
            )
            label = "Needs"
            remedy = (
                f"a release of {command_id} satisfying {_escape_markup(dep['version'])}"
            )
        console.print(f"      {label}: {remedy}")
        needs_catalog = needs_catalog or uses_catalog
    console.print()
    # The consequence differs by reason and must not be overstated. An
    # unavailable extension contributes nothing, so those features are simply
    # inert. A version mismatch is the opposite: the extension is installed and
    # enabled, so the preset does invoke it -- the combination is just untested
    # against the declared constraint, which is not the same as "safe".
    console.print("[dim]The preset is installed.[/dim]")
    if any(
        dep["reason"] in ("missing", "corrupt", "stale", "disabled") for dep in unmet
    ):
        console.print(
            "[dim]Anything relying on an unavailable extension does nothing "
            "until that is resolved.[/dim]"
        )
    if any(dep["reason"] == "version" for dep in unmet):
        console.print(
            "[dim]Where only a version constraint is unmet the extension is "
            "still used, so it may not behave as the preset expects.[/dim]"
        )
    if needs_catalog:
        # `extension add <id>` resolves through the catalogs, and the default
        # community catalog is discovery-only, so installing by id is refused
        # for anything listed only there -- true of every extension motivating
        # this feature. Knowing which applies would mean a catalog fetch, and
        # this runs on an install path that touches no network, so describe
        # the outcome instead of asserting the command succeeds. The rejection
        # itself prints the exact --from form, so this is a signpost rather
        # than a dead end.
        console.print(
            "[dim]If an extension is listed only in a discovery-only catalog, "
            "that command is refused and prints the "
            "--from <archive-url> form to use instead.[/dim]"
        )


# ===== Preset Commands =====


@preset_app.command("add")
def preset_add(
    preset_id: str = typer.Argument(None, help="Preset ID to install from catalog"),
    from_url: str = typer.Option(
        None,
        "--from",
        help="Install from a .zip, .tar.gz, or .tgz URL",
    ),
    dev: str = typer.Option(
        None, "--dev", help="Install from local directory (development mode)"
    ),
    priority: int = typer.Option(
        10,
        "--priority",
        help="Resolution priority (lower = higher precedence, default 10)",
    ),
):
    """Install a preset."""
    _commands._install_preset(preset_id, from_url, dev, priority)
