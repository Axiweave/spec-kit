"""Extension install flow shared by ``specify extension`` commands and ``specify init``.

Resolves catalog entries, downloads URL archives through the hardened cache,
and refreshes native event config after an extension changes.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from uuid import uuid4

import typer
from rich.markup import escape as _escape_markup
from rich.table import Table

from ..download_security import (
    archive_format_from_name,
    detect_archive_format,
    is_https_or_localhost_http,
    read_response_limited,
)
from ..terminal import console


def refresh_events_and_warn(project_root: Path) -> None:
    """Refresh native event config and surface failures (R3).

    The extension has already been added/removed/enabled/disabled by the time
    this runs, so a refresh failure must not abort the command — but it must
    be surfaced, because a stale native hook may still be active (e.g. a
    disabled extension's hook still resolves and runs). Prints a warning with
    the per-integration failures so the user knows deactivation was incomplete.
    """
    from ..events import EventRefreshError, refresh_integration_events

    try:
        refresh_integration_events(project_root)
    except EventRefreshError as exc:
        console.print(
            f"\n[yellow]⚠[/yellow]  Extension updated, but event refresh failed "
            f"for {len(exc.failures)} integration(s); a stale native hook may "
            f"still be active. Re-run [cyan]specify integration upgrade "
            f"<key>[cyan][/cyan][/cyan] to retry."
        )
        for key, detail in exc.failures:
            console.print(f"    {key}: {_escape_markup(detail)}")


def install_extension_from_url(
    manager,
    project_root: Path,
    url: str,
    speckit_version: str,
    *,
    priority: int = 10,
    force: bool = False,
):
    """Download an archive from *url* and install it, reusing the hardened path.

    Shares the same download hardening as ``extension add --from``:
    HTTPS enforcement, the catalog's authenticated + redirect-guarded
    ``_open_url`` fetch, a bounded (50 MiB) response read, archive-format
    detection (ZIP or tar.gz/tgz), and a TOCTOU-safe transient download file
    consumed directly by ``install_from_zip``.

    Returns the installed manifest. Raises ``ExtensionError`` on any failure so
    callers can present a uniform message without a second downloader.
    """
    import urllib.error

    from . import ExtensionCatalog, ExtensionError

    if not is_https_or_localhost_http(url):
        raise ExtensionError(
            "URL must use HTTPS (HTTP is only allowed for localhost)"
        )

    download_dir = _validate_safe_cache_dir(project_root)
    archive_filename = f"extension-url-download-{uuid4().hex}.archive"
    # Only used for diagnostic messages: the real archive is a transient inode
    # (unlinked on POSIX, O_TEMPORARY on Windows) consumed via ``archive_file``
    # below, so this path is never opened again.
    archive_path = download_dir / archive_filename

    try:
        dl_catalog = ExtensionCatalog(project_root)
        download_url = url
        extra_headers = None
        resolved_url = dl_catalog._resolve_github_release_asset_api_url(download_url)
        if resolved_url:
            download_url = resolved_url
            extra_headers = {"Accept": "application/octet-stream"}

        with dl_catalog._open_url(
            download_url, timeout=60, extra_headers=extra_headers
        ) as response:
            archive_data = read_response_limited(
                response,
                error_type=ExtensionError,
                label=f"extension {url}",
            )
            final_url = (
                response.geturl() if hasattr(response, "geturl") else download_url
            )
            content_type = (
                response.getheader("Content-Type")
                if hasattr(response, "getheader")
                else None
            )
    except urllib.error.URLError as exc:
        raise ExtensionError(f"Failed to download from {url}: {exc}") from exc

    download_fd = -1
    download_file = None
    try:
        try:
            download_fd = _safe_open_download_zip(
                project_root, download_dir, archive_filename
            )
        except OSError as exc:
            raise ExtensionError(
                f"Could not safely create download file: {exc}"
            ) from exc

        try:
            download_file = os.fdopen(download_fd, "w+b")
            download_fd = -1
            download_file.write(archive_data)
            download_file.flush()
            download_file.seek(0)
        except OSError as exc:
            raise ExtensionError(
                f"Could not safely write download file: {exc}"
            ) from exc

        format_source = (
            final_url
            if archive_format_from_name(final_url) is not None
            else url
        )
        try:
            detect_archive_format(
                archive_path,
                archive_file=download_file,
                source_name=format_source,
                content_type=content_type,
                error_type=ExtensionError,
            )
        except ExtensionError as exc:
            raise ExtensionError(
                f"{url} did not return a ZIP archive or tar.gz/tgz archive "
                f"(got {len(archive_data)} bytes). This usually means the request "
                "was not authenticated and a login/HTML page was returned. "
                "Verify the URL and configured credentials."
            ) from exc

        # Consume the transient inode reserved above rather than reopening the
        # cache pathname during extraction.
        try:
            return manager.install_from_zip(
                archive_path,
                speckit_version,
                priority=priority,
                force=force,
                archive_file=download_file,
            )
        except OSError as exc:
            raise ExtensionError(
                f"Could not install extension from downloaded archive: {exc}"
            ) from exc
    finally:
        if download_file is not None:
            try:
                download_file.close()
            except OSError:
                pass
        elif download_fd >= 0:
            try:
                os.close(download_fd)
            except OSError:
                pass


def resolve_catalog_extension(
    argument: str,
    catalog,
    command_name: str = "info",
) -> tuple[dict | None, Exception | None]:
    """Resolve an extension argument (ID or display name) from the catalog.

    Args:
        argument: Extension ID or display name provided by user
        catalog: ExtensionCatalog instance
        command_name: Name of the command for error messages

    Returns:
        Tuple of (extension_info, catalog_error)
        - If found: (ext_info_dict, None)
        - If catalog error: (None, error)
        - If not found: (None, None)
    """
    from . import ExtensionError

    try:
        # First try by ID
        ext_info = catalog.get_extension_info(argument)
        if ext_info:
            return (ext_info, None)

        # Try by display name - search using argument as query, then filter for exact match.
        # Coerce name defensively: catalog JSON is user-editable, so a hand-authored
        # non-string/missing name must not crash the match (the ambiguous-match display
        # below already str()-coerces name for the same reason).
        search_results = catalog.search()
        argument_lower = argument.lower()
        name_matches = [
            ext
            for ext in search_results
            if str(ext.get("name", "")).lower() == argument_lower
        ]

        if len(name_matches) == 1:
            return (name_matches[0], None)
        elif len(name_matches) > 1:
            # Ambiguous display-name match in catalog
            console.print(
                f"[red]Error:[/red] Extension name '{_escape_markup(argument)}' is ambiguous. "
                "Multiple catalog extensions share this name:"
            )
            table = Table(title="Matching extensions")
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Name", style="white")
            table.add_column("Version", style="green")
            table.add_column("Catalog", style="dim")
            for ext in name_matches:
                table.add_row(
                    _escape_markup(str(ext.get("id", ""))),
                    _escape_markup(str(ext.get("name", ""))),
                    _escape_markup(str(ext.get("version", ""))),
                    _escape_markup(str(ext.get("_catalog_name", ""))),
                )
            console.print(table)
            console.print("\nPlease rerun using the extension ID:")
            console.print(f"  [bold]specify extension {command_name} <extension-id>[/bold]")
            raise typer.Exit(1)

        # Not found
        return (None, None)

    except ExtensionError as e:
        return (None, e)


# Relative path, below the project root, of the extension URL download cache.
_CACHE_REL_PARTS = (".specify", "extensions", ".cache", "downloads")


def _has_secure_dir_fd() -> bool:
    """Whether this platform supports the strongest (POSIX) hardening path.

    The descriptor-anchored walk needs ``O_NOFOLLOW`` plus ``dir_fd`` support
    for ``os.open``/``os.mkdir``/``os.unlink``. When any of those is missing
    (notably on Windows) the caller falls back to the portable path-wise walk,
    which reproduces the same guarantees using symlink/reparse-point rejection,
    resolve-under-root containment checks, and post-open inode-identity
    verification instead of file descriptors.
    """
    return bool(
        getattr(os, "O_NOFOLLOW", 0)
        and os.open in os.supports_dir_fd
        and os.mkdir in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


def _is_symlink_refusal_errno(exc: OSError) -> bool:
    """Whether an ``os.open``/``os.mkdir`` error means a component is a symlink.

    Opening an ``O_NOFOLLOW`` path whose final component is a symlink raises
    ``ELOOP`` on Linux and ``EMLINK`` on some BSDs, while a symlinked component
    that no longer resolves to a directory surfaces as ``ENOTDIR``.
    """
    return exc.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1))


def _verify_leaf_identity(fd: int, path: Path) -> None:
    """Confirm ``fd`` still refers to the regular file at ``path``.

    Mirrors the workflow installer's staged-file check: comparing the open
    descriptor's ``fstat`` against a ``lstat`` of the pathname detects a leaf
    that was swapped for a symlink/reparse point between creation and use, so
    the portable (dir_fd-less) path is not vulnerable to an ancestor swap race.
    """
    path_stat = path.stat(follow_symlinks=False)
    open_stat = os.fstat(fd)
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_dev != open_stat.st_dev
        or path_stat.st_ino != open_stat.st_ino
    ):
        raise OSError(
            errno.ENOTDIR, "Download file changed between creation and open"
        )


def _validate_safe_cache_dir(project_root: Path) -> Path:
    """Create and validate the extension URL download cache one component at a
    time, refusing symlinked/junctioned components on every supported platform."""
    from ..workspace import workspace_root_for

    project_root = workspace_root_for(project_root)
    download_dir = project_root.joinpath(*_CACHE_REL_PARTS)
    try:
        if _has_secure_dir_fd():
            _validate_cache_dir_via_dir_fd(project_root, download_dir)
        else:
            _validate_cache_dir_via_paths(project_root, download_dir)
    except typer.Exit:
        raise
    except FileExistsError:
        console.print(
            "[red]Error:[/red] Refusing to use symlinked download cache directory"
        )
        raise typer.Exit(1)
    except OSError as exc:
        if _is_symlink_refusal_errno(exc):
            console.print(
                "[red]Error:[/red] Refusing to use symlinked download cache directory"
            )
            raise typer.Exit(1)
        console.print(
            "[red]Error:[/red] Could not prepare download cache directory: "
            f"{_escape_markup(str(exc))}"
        )
        raise typer.Exit(1)

    return download_dir


def _validate_cache_dir_via_dir_fd(project_root: Path, download_dir: Path) -> None:
    """POSIX cache-dir walk anchored on ``dir_fd`` + ``O_NOFOLLOW`` descriptors."""
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    o_cloexec = getattr(os, "O_CLOEXEC", 0)
    walk_flags = os.O_RDONLY | o_directory | o_nofollow | o_cloexec

    project_root_resolved = project_root.resolve()
    parent_fd = os.open(project_root, walk_flags)
    current_path = project_root
    try:
        for part in _CACHE_REL_PARTS:
            current_path = current_path / part

            try:
                child_fd = os.open(part, walk_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(part, walk_flags, dir_fd=parent_fd)

            try:
                current_path.resolve().relative_to(project_root_resolved)
            except (OSError, ValueError):
                try:
                    os.close(child_fd)
                except OSError:
                    pass
                console.print(
                    "[red]Error:[/red] Download cache directory escapes project root"
                )
                raise typer.Exit(1)

            os.close(parent_fd)
            parent_fd = child_fd
    finally:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _validate_cache_dir_via_paths(project_root: Path, download_dir: Path) -> None:
    """Portable cache-dir walk for platforms without ``dir_fd`` (e.g. Windows).

    Each component is created individually while a symlink/junction is rejected
    both before and after creation, and every component is required to resolve
    back under the project root so a mount-point alias or reparse point cannot
    redirect the cache outside the project.
    """
    project_root_resolved = project_root.resolve()
    current_path = project_root
    for part in _CACHE_REL_PARTS:
        current_path = current_path / part

        if current_path.is_symlink():
            console.print(
                "[red]Error:[/red] Refusing to use symlinked download cache directory"
            )
            raise typer.Exit(1)

        try:
            current_path.mkdir()
        except FileExistsError:
            pass

        # Re-check after creation: a component swapped for a symlink/junction
        # (or an existing non-directory) between the check and mkdir is caught
        # here before the walk descends into it.
        if current_path.is_symlink() or not current_path.is_dir():
            console.print(
                "[red]Error:[/red] Refusing to use symlinked download cache directory"
            )
            raise typer.Exit(1)

        try:
            current_path.resolve().relative_to(project_root_resolved)
        except (OSError, ValueError):
            console.print(
                "[red]Error:[/red] Download cache directory escapes project root"
            )
            raise typer.Exit(1)


def _safe_open_download_zip(
    project_root: Path, download_dir: Path, zip_filename: str
) -> int:
    """Exclusively create a download ZIP and return an owned descriptor.

    The archive never persists as a nameable on-disk file: the POSIX path
    unlinks the leaf immediately after exclusive creation (anonymous inode),
    while the portable path opens it with ``O_TEMPORARY`` so the OS deletes it
    when the last handle closes. Installation proceeds entirely through the
    returned descriptor, removing the pathname-reopen and cleanup-walk TOCTOU
    classes on every supported platform.
    """
    from ..workspace import workspace_root_for

    project_root = workspace_root_for(project_root)
    if _has_secure_dir_fd():
        return _open_download_zip_via_dir_fd(
            project_root, download_dir, zip_filename
        )
    return _open_download_zip_via_paths(project_root, download_dir, zip_filename)


def _open_download_zip_via_dir_fd(
    project_root: Path, download_dir: Path, zip_filename: str
) -> int:
    """POSIX leaf create: descriptor walk, ``O_EXCL`` create, immediate unlink."""
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)
    o_directory = getattr(os, "O_DIRECTORY", 0)
    o_cloexec = getattr(os, "O_CLOEXEC", 0)
    walk_flags = os.O_RDONLY | o_directory | o_nofollow | o_cloexec

    rel_parts = download_dir.relative_to(project_root).parts
    parent_fd = os.open(project_root, walk_flags)
    try:
        for part in rel_parts:
            new_fd = os.open(part, walk_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = new_fd

        download_fd = os.open(
            zip_filename,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | o_nofollow | o_cloexec,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            os.unlink(zip_filename, dir_fd=parent_fd)
        except OSError:
            os.close(download_fd)
            raise
        return download_fd
    finally:
        os.close(parent_fd)


def _open_download_zip_via_paths(
    project_root: Path, download_dir: Path, zip_filename: str
) -> int:
    """Portable leaf create for platforms without ``dir_fd`` (e.g. Windows).

    The cache directory is re-validated (real directory, under the project
    root) immediately before an exclusive create. ``O_EXCL`` guarantees an
    attacker cannot pre-stage the leaf as a symlink/junction, ``O_TEMPORARY``
    makes the OS delete it on close, and a post-open inode-identity check
    detects a leaf swapped underneath us. The returned descriptor is the only
    handle installation ever uses, so the cache pathname is never reopened.
    """
    zip_path = download_dir / zip_filename
    project_root_resolved = project_root.resolve()

    if download_dir.is_symlink() or not download_dir.is_dir():
        raise OSError(
            errno.ENOTDIR, "Download cache directory is not a real directory"
        )
    try:
        download_dir.resolve().relative_to(project_root_resolved)
    except (OSError, ValueError):
        raise OSError(errno.ENOTDIR, "Download cache directory escapes project root")
    if zip_path.is_symlink():
        raise OSError(errno.ELOOP, "Refusing to write through a symlinked download file")

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    o_temporary = getattr(os, "O_TEMPORARY", 0)
    flags |= o_temporary

    download_fd = os.open(zip_path, flags, 0o600)
    try:
        _verify_leaf_identity(download_fd, zip_path)
    except OSError:
        os.close(download_fd)
        # Without O_TEMPORARY the leaf is not auto-deleted, so remove the file
        # we just exclusively created (best effort, never through a symlink).
        if not o_temporary:
            try:
                if not zip_path.is_symlink():
                    zip_path.unlink()
            except OSError:
                pass
        raise
    return download_fd
