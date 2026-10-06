"""Hash-tracked installation manifest for integrations.

Each installed integration records the files it created together with
their SHA-256 hashes.  On uninstall only files whose hash still matches
the recorded value are removed — modified files are left in place and
reported to the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..workspace import is_private, workspace_root_for


def _sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_rel_path(rel: Path, root: Path, *, resolve_root: bool = True) -> Path:
    """Resolve *rel* against *root* and verify it stays within *root*.

    Raises ``ValueError`` if *rel* is absolute, contains ``..`` segments
    that escape *root*, or otherwise resolves outside the project root.
    """
    if rel.is_absolute():
        raise ValueError(
            f"Absolute paths are not allowed in manifests: {rel}"
        )
    resolved = (root / rel).resolve()
    root_resolved = root.resolve() if resolve_root else root.absolute()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(
            f"Path {rel} resolves to {resolved} which is outside "
            f"the project root {root_resolved}"
        ) from None
    return resolved


def _manifest_path_label(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _ensure_safe_manifest_directory(root: Path, directory: Path) -> None:
    """Create a manifest directory without following symlinked parents."""
    root_resolved = root.resolve()
    try:
        rel = directory.relative_to(root)
    except ValueError:
        label = _manifest_path_label(root, directory)
        raise ValueError(f"Integration manifest directory escapes project root: {label}") from None

    current = root
    for part in rel.parts:
        current = current / part
        label = _manifest_path_label(root, current)
        if current.is_symlink():
            raise ValueError(f"Refusing to use symlinked integration manifest directory: {label}")
        if current.exists():
            if not current.is_dir():
                raise ValueError(f"Integration manifest directory path is not a directory: {label}")
            try:
                current.resolve().relative_to(root_resolved)
            except (OSError, ValueError):
                raise ValueError(f"Integration manifest directory escapes project root: {label}") from None
            continue
        current.mkdir()
        try:
            current.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            raise ValueError(f"Integration manifest directory escapes project root: {label}") from None


def _ensure_safe_manifest_destination(root: Path, path: Path) -> None:
    """Refuse manifest writes that would escape the project or follow symlinks."""
    root_resolved = root.resolve()
    _ensure_safe_manifest_directory(root, path.parent)
    label = _manifest_path_label(root, path)
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked integration manifest path: {label}")
    if path.exists():
        if not path.is_file():
            raise ValueError(f"Integration manifest path is not a file: {label}")
        try:
            path.resolve().relative_to(root_resolved)
        except (OSError, ValueError):
            raise ValueError(f"Integration manifest path escapes project root: {label}") from None


def _read_manifest(path: Path) -> dict[str, Any]:
    """Read and validate one manifest file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"Integration manifest at {path} is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Integration manifest at {path} contains invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"Integration manifest at {path} must be a JSON object, got {type(data).__name__}"
        )
    files = data.get("files", {})
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ValueError(
            f"Integration manifest 'files' at {path} must be a "
            "mapping of string paths to string hashes"
        )
    for field in ("recovered_files", "excluded"):
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise ValueError(
                f"Integration manifest '{field}' at {path} must be a list of string paths"
            )
    return data


def _write_manifest(root: Path, path: Path, data: dict[str, Any]) -> None:
    content = json.dumps(data, indent=2) + "\n"
    _ensure_safe_manifest_destination(root, path)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        temp_path.chmod(0o644)
        _ensure_safe_manifest_destination(root, path)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


class IntegrationManifest:
    """Tracks files installed by a single integration.

    Parameters:
        key:          Integration identifier (e.g. ``"copilot"``).
        project_root: Absolute path to the project directory.
        version:      CLI version string recorded in the manifest.
        resolve_project_root: Resolve ``project_root`` before using it.
    """

    def __init__(
        self,
        key: str,
        project_root: Path,
        version: str = "",
        *,
        resolve_project_root: bool = True,
    ) -> None:
        self.key = key
        self.project_root = (
            project_root.resolve()
            if resolve_project_root
            else project_root.absolute()
        )
        self.metadata_root = workspace_root_for(self.project_root)
        # Private mode: checkout files get their own manifest in the checkout (research D3).
        self.private = self.metadata_root != self.project_root and is_private(self.metadata_root)
        # Private mode: teardown removes only this checkout's files and keeps the shared workspace state.
        self.checkout_only = False
        self.version = version
        self._files: dict[str, str] = {}  # rel_path → sha256 hex
        self._recovered_files: set[str] = set()
        self._installed_at: str = ""

    # -- Manifest file location -------------------------------------------

    @property
    def manifest_path(self) -> Path:
        """Path to the on-disk manifest JSON."""
        return self.metadata_root / ".specify" / "integrations" / f"{self.key}.manifest.json"

    @property
    def checkout_manifest_path(self) -> Path:
        """Private mode only: the manifest of the files in this checkout."""
        return self.project_root / ".specify" / "integrations" / f"{self.key}.manifest.json"

    def _checkout_manifest_file(self) -> Path:
        current = self.project_root
        for part in (".specify", "integrations", self.checkout_manifest_path.name):
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Refusing to use symlinked manifest path: {current}")
        return current

    def _entry_root(self, rel: Path) -> Path:
        return self.metadata_root if rel.parts[:1] == (".specify",) else self.project_root

    def file_path(self, rel_path: str | Path, *, allow_symlink: bool = False) -> Path:
        """Resolve a relative key under its asset root without symlink ancestors.

        ``.specify`` entries belong to the workspace. Other entries belong to
        the repository. ``allow_symlink`` permits only a final symlink, so
        uninstall can remove that link without following its target.
        """
        rel = Path(rel_path)
        if rel.is_absolute() or rel.anchor:
            raise ValueError(f"Absolute paths are not allowed in manifests: {rel}")
        if not rel.parts or ".." in rel.parts:
            raise ValueError(
                f"Manifest paths must be canonical and cannot point outside their root: {rel}"
            )
        root = self._entry_root(rel)
        current = root
        for part in rel.parts[:-1] if allow_symlink else rel.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Refusing to use symlinked manifest path: {rel}")
        # Both asset roots already follow the constructor's resolution policy.
        _validate_rel_path(rel.parent if allow_symlink else rel, root, resolve_root=False)
        return root / rel

    def _ignore_entry_point(self, relative: str) -> None:
        if self.private or self.metadata_root == self.project_root or relative.startswith(".specify/"):
            return
        ignore = self.project_root / ".gitignore"
        if ignore.is_symlink():
            raise ValueError(f"Refusing a symlinked ignore file: {ignore}")
        content = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
        entry = "/" + relative.replace("\\", "\\\\").replace(" ", "\\ ")
        if entry not in content.splitlines():
            ignore.write_text(content.rstrip("\n") + "\n" + entry + "\n", encoding="utf-8")

    def exclude(self, relatives: list[str]) -> None:
        """Hide checkout paths that Spec Kit writes outside this manifest's hash list.

        Default mode appends them to ``.gitignore``. Private mode records them in
        the checkout manifest's ``excluded`` list, which feeds the exclude block.
        """
        if not self.private:
            for relative in relatives:
                self._ignore_entry_point(relative)
            return
        path = self._checkout_manifest_file()
        data = (
            _read_manifest(path) if path.exists()
            else {"integration": self.key, "version": self.version, "files": {}}
        )
        data["excluded"] = sorted(set(data.get("excluded", [])) | set(relatives))
        _write_manifest(self.project_root, path, data)
        from .._private_checkout import regenerate_exclude_block
        regenerate_exclude_block(self.project_root)

    # -- Recording files --------------------------------------------------

    def record_file(self, rel_path: str | Path, content: bytes | str) -> Path:
        """Write *content* under its asset root and record its relative key and hash.

        Creates parent directories as needed.  Returns the absolute path
        of the written file.
        If the path was previously marked as recovered via
        ``record_existing(recovered=True)``, the recovered marker is
        cleared because the bytes are now produced, not merely observed.

        Raises ``ValueError`` for unsafe relative paths or symlinks.
        """
        rel = Path(rel_path)
        abs_path = self.file_path(rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(content, str):
            content = content.encode("utf-8")
        abs_path.write_bytes(content)

        normalized = rel.as_posix()
        self._files[normalized] = hashlib.sha256(content).hexdigest()
        # ``record_file`` writes *produced* content, so any prior
        # recovered marker for this path is no longer accurate.
        self._recovered_files.discard(normalized)
        self._ignore_entry_point(normalized)
        return abs_path

    def record_existing(self, rel_path: str | Path, *, recovered: bool = False) -> None:
        """Record the hash of an already-existing regular file at *rel_path*.

        When ``recovered=True``, the path is also marked in the manifest's
        ``recovered_files`` list to signal that the file's on-disk hash was
        *observed* during install (because the file already existed and was not
        overwritten), not *produced* by the install. Future ``refresh_managed``
        runs should consult ``is_recovered`` before treating the recorded hash
        as a managed baseline.

        Raises:
            ValueError: if *rel_path* resolves outside the project root, is
                a symlink, or is not a regular file. A directory or other
                non-file path cannot be silently recorded — its hash would
                be meaningless and ``check_modified``/``uninstall`` would
                treat the entry as permanently broken.
            OSError: if the underlying filesystem call (``is_symlink``,
                ``is_file``, or the file-read used to compute the hash)
                fails — for example a ``PermissionError`` on the path.
                Callers should be prepared to handle ``OSError`` (and its
                subclasses such as ``PermissionError``) in addition to
                ``ValueError``.
        """
        rel = Path(rel_path)
        abs_path = self.file_path(rel)
        if not abs_path.is_file():
            raise ValueError(
                f"Manifest path is not a regular file: {rel}"
            )
        normalized = rel.as_posix()
        self._files[normalized] = _sha256(abs_path)
        if recovered:
            self._recovered_files.add(normalized)
        else:
            # ``recovered=False`` means the caller is asserting this path is
            # managed-baseline now, not merely observed; drop any stale
            # recovered marker so future is_recovered() queries reflect the
            # transition. ``discard`` is a no-op when the key is absent.
            self._recovered_files.discard(normalized)
        if not recovered:
            self._ignore_entry_point(normalized)

    def remove(self, rel_path: str | Path) -> bool:
        """Drop *rel_path* from the tracked file set and any recovered marker.

        Operates purely on the manifest's recorded key; it does NOT touch the
        file on disk. Returns ``True`` if an entry was present and removed.
        Used to keep the manifest consistent after a caller deletes a stale
        managed file that the current install no longer ships.

        Input is normalized through the same lexical pipeline as
        ``record_existing`` / ``is_recovered``: absolute paths and paths
        containing ``..`` segments are rejected (return ``False``) — such paths
        can never be canonical manifest keys, so there is nothing to remove.
        """
        rel = Path(rel_path)
        if rel.anchor or ".." in rel.parts or not rel.parts:
            return False
        normalized = rel.as_posix()
        self._recovered_files.discard(normalized)
        return self._files.pop(normalized, None) is not None

    # -- Querying ---------------------------------------------------------

    @property
    def files(self) -> dict[str, str]:
        """Return a copy of the ``{rel_path: sha256}`` mapping."""
        return dict(self._files)

    @property
    def recovered_files(self) -> set[str]:
        """Return a copy of the set of paths recorded with ``recovered=True``.

        These entries had their hashes observed (not produced) during install
        because the file already existed on disk and the install skipped it.
        Their on-disk bytes may be user customizations — callers that would
        overwrite based on hash equality (e.g. ``refresh_managed``) MUST check
        ``is_recovered`` first.
        """
        return set(self._recovered_files)

    def is_recovered(self, rel_path: str | Path) -> bool:
        """Return True if *rel_path* was recorded via ``record_existing(recovered=True)``.

        This query uses only the relative key, not the file on disk.
        Absolute paths, empty paths, and paths with ``..`` segments return
        ``False`` because they cannot be canonical manifest keys.
        """
        rel = Path(rel_path)
        if rel.anchor or ".." in rel.parts or not rel.parts:
            return False
        normalized = rel.as_posix()
        return normalized in self._recovered_files

    def check_modified(self) -> list[str]:
        """Return relative paths of tracked files whose content changed on disk."""
        modified: list[str] = []
        for rel, expected_hash in self._files.items():
            rel_path = Path(rel)
            # Ignore invalid keys without reading outside either asset root.
            if rel_path.anchor or ".." in rel_path.parts or not rel_path.parts:
                continue
            try:
                abs_path = self.file_path(rel_path, allow_symlink=True)
            except (ValueError, OSError):
                modified.append(rel)
                continue
            if not abs_path.exists() and not abs_path.is_symlink():
                continue
            # Treat symlinks and non-regular-files as modified
            if abs_path.is_symlink() or not abs_path.is_file():
                modified.append(rel)
                continue
            try:
                changed = _sha256(abs_path) != expected_hash
            except OSError:
                # Unreadable regular file (e.g. permission denied): treat as
                # modified, consistent with the symlink / non-regular-file
                # handling above, rather than letting the OSError escape.
                changed = True
            if changed:
                modified.append(rel)
        return modified

    # -- Uninstall --------------------------------------------------------

    def uninstall(
        self,
        project_root: Path | None = None,
        *,
        force: bool = False,
        remove_manifest: bool = True,
    ) -> tuple[list[Path], list[Path]]:
        """Remove tracked files whose hash still matches.

        Parameters:
            project_root:    Override for the project root.
            force:           If ``True``, remove files even if modified.
            remove_manifest: If ``True`` (default), also delete this
                integration's ``{key}.manifest.json``. Set ``False`` for
                *partial* cleanups (e.g. the upgrade stale-file pass, which
                builds a throwaway manifest over a subset of files) so the
                real, freshly-saved manifest for the same key is not destroyed.

        Returns:
            ``(removed, skipped)`` — absolute paths.
        """
        resolver = self
        if project_root is not None and project_root.resolve() != self.project_root:
            resolver = IntegrationManifest(self.key, project_root)
        removed: list[Path] = []
        skipped: list[Path] = []

        for rel, expected_hash in self._files.items():
            rel_path = Path(rel)
            if rel_path.is_absolute() or rel_path.anchor or ".." in rel_path.parts or not rel_path.parts:
                continue
            if self.checkout_only and rel_path.parts[0] == ".specify":
                continue
            root = resolver._entry_root(rel_path)
            path = root / rel_path
            try:
                path = resolver.file_path(rel_path, allow_symlink=True)
            except (ValueError, OSError):
                skipped.append(path)
                continue
            if not path.exists() and not path.is_symlink():
                continue
            # Skip directories — manifest only tracks files
            if not path.is_file() and not path.is_symlink():
                skipped.append(path)
                continue
            # Never follow symlinks when comparing hashes. Only remove
            # symlinks when forced, to avoid acting on tampered entries.
            if path.is_symlink():
                if not force:
                    skipped.append(path)
                    continue
            else:
                if not force:
                    try:
                        matches = _sha256(path) == expected_hash
                    except OSError:
                        # Unreadable: can't verify it's ours, so preserve it
                        # (mirrors the path.unlink() OSError guard below).
                        skipped.append(path)
                        continue
                    if not matches:
                        skipped.append(path)
                        continue
            try:
                path.unlink()
            except OSError:
                skipped.append(path)
                continue
            removed.append(path)
            # Clean up empty parent directories up to project root
            parent = path.parent
            while parent != root:
                try:
                    parent.rmdir()  # only succeeds if empty
                except OSError:
                    break
                parent = parent.parent

        if remove_manifest:
            manifests: list[tuple[Path, Path]] = []
            try:
                if not self.checkout_only:
                    manifests.append((self.file_path(
                        Path(".specify/integrations") / self.manifest_path.name, allow_symlink=True
                    ), self.metadata_root))
                if self.private:
                    manifests.append((self._checkout_manifest_file(), self.project_root))
            except (ValueError, OSError):
                skipped.append(self.manifest_path if not manifests else self.checkout_manifest_path)
                return removed, skipped
            for manifest, root in manifests:
                if not manifest.exists():
                    continue
                try:
                    manifest.unlink()
                except OSError:
                    # An undeletable manifest (read-only file, a directory left at
                    # the path, a Windows lock) must not abort the uninstall after
                    # the tracked files were already removed: the caller would lose
                    # the (removed, skipped) result and never run its post-uninstall
                    # bookkeeping. Report it like any other file we could not
                    # remove, mirroring the path.unlink() guard above. The
                    # empty-parent cleanup below is left unconditional: with the
                    # manifest still on disk its parent is non-empty, so the first
                    # rmdir() raises and breaks immediately.
                    skipped.append(manifest)
                parent = manifest.parent
                while parent != root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        if self.private:
            from .._private_checkout import regenerate_exclude_block
            regenerate_exclude_block(self.project_root)

        return removed, skipped

    # -- Persistence ------------------------------------------------------

    def save(self) -> Path:
        """Write the manifest to disk.  Returns the manifest path.

        Private mode writes ``.specify/*`` keys to the workspace manifest and
        all other keys to the checkout manifest, then regenerates the exclude block.
        """
        self._installed_at = self._installed_at or datetime.now(timezone.utc).isoformat()

        def data(files: dict[str, str]) -> dict[str, Any]:
            recovered = sorted(self._recovered_files & files.keys())
            return {
                "integration": self.key,
                "version": self.version,
                "installed_at": self._installed_at,
                "files": files,
                **({"recovered_files": recovered} if recovered else {}),
            }

        if not self.private:
            _write_manifest(self.metadata_root, self.manifest_path, data(self._files))
            return self.manifest_path
        shared = {k: v for k, v in self._files.items() if k.startswith(".specify/")}
        local = {k: v for k, v in self._files.items() if k not in shared}
        _write_manifest(self.metadata_root, self.manifest_path, data(shared))
        checkout = self._checkout_manifest_file()
        local_data = data(local)
        # Paths hidden by exclude() stay hidden until this checkout drops the integration.
        excluded = _read_manifest(checkout).get("excluded", []) if checkout.exists() else []
        if excluded:
            local_data["excluded"] = excluded
        _write_manifest(self.project_root, checkout, local_data)
        from .._private_checkout import regenerate_exclude_block
        regenerate_exclude_block(self.project_root)
        return self.manifest_path

    @classmethod
    def load(
        cls,
        key: str,
        project_root: Path,
        *,
        resolve_project_root: bool = True,
    ) -> IntegrationManifest:
        """Load an existing manifest from disk.

        In private mode, merge the workspace and checkout manifests. Either
        may be absent. Raises ``FileNotFoundError`` if no manifest exists.
        """
        inst = cls(key, project_root, resolve_project_root=resolve_project_root)
        path = inst.file_path(Path(".specify/integrations") / inst.manifest_path.name)
        paths = [path]
        if inst.private:
            paths = [p for p in (path, inst._checkout_manifest_file()) if p.exists()]
            if not paths:
                raise FileNotFoundError(path)
        for index, path in enumerate(paths):
            data = _read_manifest(path)
            stored_key = data.get("integration", "")
            if stored_key and stored_key != key:
                raise ValueError(
                    f"Manifest at {path} belongs to integration {stored_key!r}, "
                    f"not {key!r}"
                )
            if index == 0:
                inst.version = data.get("version", "")
                inst._installed_at = data.get("installed_at", "")
            inst._files.update(data.get("files", {}))
            inst._recovered_files.update(data.get("recovered_files", []))
        # Drop any recovered_files entries that don't correspond to tracked
        # files — defensive against externally-edited or partially-corrupted
        # manifests. Inconsistent state self-corrects on next save().
        inst._recovered_files &= set(inst._files.keys())
        return inst
