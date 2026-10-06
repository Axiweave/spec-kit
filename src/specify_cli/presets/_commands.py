"""Typer command group and shared CLI infrastructure for ``specify preset``."""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.markup import escape as _escape_markup


from ..terminal import console
from ..download_security import read_response_limited as _read_response_limited
from ..download_security import (
    archive_format_from_name,
    archive_suffix,
    detect_archive_format,
    is_https_or_localhost_http,
    is_safe_download_redirect,
)

read_response_limited = _read_response_limited

preset_app = typer.Typer(
    name="preset",
    help="Manage spec-kit presets",
    add_completion=False,
)

# Lowest priority a user may request. Lower numbers win resolution, so the
# stack is anchored at 1 rather than 0 to leave no unreachable slot above the
# highest-precedence preset.
MINIMUM_PRESET_PRIORITY = 1


def _render_powershell_argv(argv: list[str]) -> str:
    """Render argv as a copy-pastable PowerShell command."""

    def quote_arg(arg: str) -> str:
        return "'" + arg.replace("'", "''") + "'"

    return "& " + " ".join(quote_arg(arg) for arg in argv)


def _validate_priority(priority: int) -> None:
    """Reject a non-positive priority before destructive work begins."""
    if priority < MINIMUM_PRESET_PRIORITY:
        console.print(
            "[red]Error:[/red] Priority must be a positive integer "
            f"({MINIMUM_PRESET_PRIORITY} or higher)"
        )
        raise typer.Exit(1)


def _warn_unmet_extension_dependencies(manager, manifest) -> None:
    """Preserve the legacy helper import path for external consumers."""
    from .command_add import _warn_unmet_extension_dependencies as implementation

    implementation(manager, manifest)


def preset_add(*args, **kwargs):
    """Preserve the legacy add-handler import path for external consumers."""
    from .command_add import preset_add as implementation

    return implementation(*args, **kwargs)


def preset_remove(*args, **kwargs):
    """Preserve the legacy remove-handler import path for external consumers."""
    from .command_remove import preset_remove as implementation

    return implementation(*args, **kwargs)


def preset_update(*args, **kwargs):
    """Preserve the legacy update-handler import path for external consumers."""
    from .command_update import preset_update as implementation

    return implementation(*args, **kwargs)


def _install_preset(
    preset_id: str | None,
    from_url: str | None,
    dev: str | None,
    priority: int,
    *,
    force: bool = False,
) -> None:
    """Install or restore a preset through the same validated source paths."""
    from .. import _locate_bundled_preset, require_specify_project, get_speckit_version
    from . import (
        PresetCatalog,
        PresetCompatibilityError,
        PresetError,
        PresetManager,
        PresetValidationError,
    )

    project_root = require_specify_project()
    _validate_priority(priority)

    manager = PresetManager(project_root)
    speckit_version = get_speckit_version()
    if force and not manager.registry.is_installed(preset_id):
        console.print(f"[red]Error:[/red] Preset '{_escape_markup(str(preset_id))}' is not installed")
        raise typer.Exit(1)
    install_options = {"force": True, "expected_id": preset_id} if force else {}

    try:
        if dev:
            dev_path = Path(dev).resolve()
            if not dev_path.exists():
                console.print(f"[red]Error:[/red] Directory not found: {dev}")
                raise typer.Exit(1)

            console.print(f"Installing preset from [cyan]{dev_path}[/cyan]...")
            manifest = manager.install_from_directory(
                dev_path, speckit_version, priority,
                **install_options,
            )
            console.print(
                f"[green]✓[/green] Preset '{manifest.name}' v{manifest.version} installed (priority {priority})"
            )

        elif from_url:
            # Validate URL scheme before downloading
            from urllib.parse import urlparse as _urlparse

            try:
                _parsed = _urlparse(from_url)
                _ = _parsed.port
            except ValueError:
                console.print(
                    f"[red]Error:[/red] Invalid URL: {_escape_markup(from_url)}"
                )
                raise typer.Exit(1)

            def _validate_download_redirect(old_url, new_url):
                if not is_safe_download_redirect(old_url, new_url):
                    import urllib.error

                    raise urllib.error.URLError(
                        "redirect target must use HTTPS without entering a local "
                        "target, or stay within loopback over HTTP"
                    )

            if not is_https_or_localhost_http(from_url):
                console.print(
                    "[red]Error:[/red] URL must use HTTPS with a hostname and be "
                    "a valid URL with a host. HTTP is only allowed for localhost, "
                    "127.0.0.1, and ::1."
                )
                raise typer.Exit(1)

            console.print(
                f"Installing preset from [cyan]{_escape_markup(from_url)}[/cyan]..."
            )
            import tempfile
            import urllib.error

            with tempfile.TemporaryDirectory() as tmpdir:
                archive_path = Path(tmpdir) / "preset.archive"
                try:
                    from specify_cli.authentication.github_http import (
                        resolve_github_release_asset_api_url,
                    )
                    from specify_cli.authentication.http import github_provider_hosts
                    from specify_cli.authentication.http import open_url as _open_url

                    _preset_extra_headers = None
                    _resolved_from_url = resolve_github_release_asset_api_url(
                        from_url, _open_url, github_hosts=github_provider_hosts()
                    )
                    if _resolved_from_url:
                        from_url = _resolved_from_url
                        _preset_extra_headers = {"Accept": "application/octet-stream"}

                    with _open_url(
                        from_url,
                        timeout=60,
                        extra_headers=_preset_extra_headers,
                        redirect_validator=_validate_download_redirect,
                    ) as response:
                        final_url = (
                            response.geturl()
                            if hasattr(response, "geturl")
                            else from_url
                        )
                        if not is_https_or_localhost_http(final_url):
                            console.print(
                                "[red]Error:[/red] Preset URL redirected to a disallowed URL: "
                                f"{final_url}. Redirect targets must use HTTPS with a hostname, "
                                "or HTTP for localhost (127.0.0.1, ::1)."
                            )
                            raise typer.Exit(1)
                        archive_data = read_response_limited(
                            response,
                            error_type=PresetError,
                            label=f"preset {from_url}",
                        )
                        content_type = (
                            response.getheader("Content-Type")
                            if hasattr(response, "getheader")
                            else None
                        )
                    archive_path.write_bytes(archive_data)
                    format_source = (
                        final_url
                        if archive_format_from_name(final_url) is not None
                        else from_url
                    )
                    archive_format = detect_archive_format(
                        archive_path,
                        source_name=format_source,
                        content_type=content_type,
                        error_type=PresetError,
                    )
                    detected_path = archive_path.with_suffix(
                        archive_suffix(archive_format)
                    )
                    os.replace(archive_path, detected_path)
                    archive_path = detected_path
                except (urllib.error.URLError, PresetError) as e:
                    console.print(
                        f"[red]Error:[/red] Failed to download: "
                        f"{_escape_markup(str(e))}"
                    )
                    raise typer.Exit(1)

                manifest = manager.install_from_zip(
                    archive_path,
                    speckit_version,
                    priority,
                    **install_options,
                )

            console.print(
                f"[green]✓[/green] Preset '{manifest.name}' v{manifest.version} installed (priority {priority})"
            )

        elif preset_id:
            # Try bundled preset first, then catalog
            bundled_path = _locate_bundled_preset(preset_id)
            if bundled_path:
                console.print(f"Installing bundled preset [cyan]{preset_id}[/cyan]...")
                manifest = manager.install_from_directory(
                    bundled_path, speckit_version, priority,
                    **install_options,
                )
                console.print(
                    f"[green]✓[/green] Preset '{manifest.name}' v{manifest.version} installed (priority {priority})"
                )
            else:
                catalog = PresetCatalog(project_root)
                pack_info = catalog.get_pack_info(preset_id)

                if not pack_info:
                    console.print(
                        f"[red]Error:[/red] Preset '{preset_id}' not found in catalog"
                    )
                    raise typer.Exit(1)

                # Bundled presets should have been caught above; if we reach
                # here the bundled files are missing from the installation.
                if pack_info.get("bundled") and not pack_info.get("download_url"):
                    from ..extensions import REINSTALL_COMMAND

                    console.print(
                        f"[red]Error:[/red] Preset '{preset_id}' is bundled with spec-kit "
                        f"but could not be found in the installed package."
                    )
                    console.print(
                        "\nThis usually means the spec-kit installation is incomplete or corrupted."
                    )
                    console.print("Try reinstalling spec-kit:")
                    console.print(f"  {REINSTALL_COMMAND}")
                    raise typer.Exit(1)

                if not pack_info.get("_install_allowed", True):
                    catalog_name = pack_info.get("_catalog_name", "unknown")
                    console.print(
                        f"[red]Error:[/red] Preset '{preset_id}' is from the '{catalog_name}' catalog which is discovery-only (install not allowed)."
                    )
                    console.print(
                        "Add the catalog with --install-allowed or install from the preset's repository directly with --from."
                    )
                    raise typer.Exit(1)

                console.print(
                    f"Installing preset [cyan]{pack_info.get('name', preset_id)}[/cyan]..."
                )

                try:
                    archive_path = catalog.download_pack(preset_id)
                    manifest = manager.install_from_zip(
                        archive_path,
                        speckit_version,
                        priority,
                        **install_options,
                        catalog_name=pack_info.get("_catalog_name"),
                    )
                    console.print(
                        f"[green]✓[/green] Preset '{manifest.name}' v{manifest.version} installed (priority {priority})"
                    )
                finally:
                    if "archive_path" in locals() and archive_path.exists():
                        archive_path.unlink(missing_ok=True)
        else:
            console.print(
                "[red]Error:[/red] Specify a preset ID, --from URL, or --dev path"
            )
            raise typer.Exit(1)

        # Every install path above binds `manifest` and the no-source branch
        # exits, so one call here covers --dev, --from, and catalog installs
        # alike. Warns rather than fails: the preset is installed and its
        # overrides fall through to the core workflow without the extension.
        _warn_unmet_extension_dependencies(manager, manifest)

    except PresetCompatibilityError as e:
        console.print(f"[red]Compatibility Error:[/red] {_escape_markup(str(e))}")
        raise typer.Exit(1)
    except PresetValidationError as e:
        console.print(f"[red]Validation Error:[/red] {_escape_markup(str(e))}")
        raise typer.Exit(1)
    except (PresetError, OSError) as e:
        console.print(f"[red]Error:[/red] {_escape_markup(str(e))}")
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Register preset commands on the parent application."""
    # Imports are intentionally ordered to preserve command help output.
    from . import command_list as _command_list
    from . import command_add as _command_add
    from . import command_remove as _command_remove
    from . import command_update as _command_update
    from . import command_search as _command_search
    from . import command_resolve as _command_resolve
    from . import command_info as _command_info
    from . import command_set_priority as _command_set_priority
    from . import command_enable as _command_enable
    from . import command_disable as _command_disable
    from .catalog import register as register_catalog

    _ = (
        _command_list,
        _command_add,
        _command_remove,
        _command_update,
        _command_search,
        _command_resolve,
        _command_info,
        _command_set_priority,
        _command_enable,
        _command_disable,
    )
    register_catalog(preset_app)
    app.add_typer(preset_app, name="preset")
