"""Read-only specification inspection and approved additive file transfers."""
from __future__ import annotations

import base64
import binascii
import contextlib
import dataclasses
import hashlib
import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

from specify_cli.workspace import find_repository, project_record_path, workspace_root_for

from .naming import classify_feature_name


@dataclasses.dataclass(frozen=True)
class FileSnapshot:
    path: str
    content: bytes | None
    digest: str | None
    mode: int | None
    size: int | None
    mtime_ns: int | None
    availability: str

    def baseline(self) -> tuple:
        return (self.path, self.digest, self.mode, self.size, self.mtime_ns, self.availability)


@dataclasses.dataclass(frozen=True)
class DirectorySnapshot:
    path: str
    mode: int
    size: int
    mtime_ns: int
    entries: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class Availability:
    path: str
    condition: str
    purpose: str

    def to_json(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class FeatureInventory:
    path: str
    artifacts: tuple[str, ...]
    specification: str
    plan: str
    tasks: str

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "artifacts": list(self.artifacts),
            "specification": self.specification,
            "plan": self.plan,
            "tasks": self.tasks,
        }


@dataclasses.dataclass(frozen=True)
class SpecSet:
    kind: str
    repository_root: Path | None
    workspace_root: Path | None
    spec_root: Path | None
    project_id: str | None
    numbering: str | None
    principles_path: Path | None
    destination_context: Path | None
    context_inputs: tuple[FileSnapshot, ...]
    files: Mapping[str, FileSnapshot]
    directories: Mapping[str, DirectorySnapshot]
    features: tuple[FeatureInventory, ...]
    occupied_names: Mapping[str, tuple[str, ...]]
    occupied_prefixes: Mapping[str, tuple[Any, ...]]
    availability: tuple[Availability, ...]
    markers: Mapping[str, FileSnapshot]
    pending_resources: tuple[str, ...]
    filesystem_identity: tuple[int, int] | None
    branch_ref: str | None = None
    artifact_prefix: str | None = None
    commit: str | None = None
    tracking: Mapping[str, str] = dataclasses.field(default_factory=lambda: MappingProxyType({}))
    tracking_context: Mapping[str, Any] = dataclasses.field(default_factory=lambda: MappingProxyType({}))

    def to_json(self) -> dict:
        """Project public inventory without original bytes or private record values."""
        return {
            "kind": self.kind,
            "repository_root": str(self.repository_root) if self.repository_root else None,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "spec_root": str(self.spec_root) if self.spec_root is not None else None,
            "project_id": self.project_id,
            "numbering": self.numbering,
            "principles_path": str(self.principles_path) if self.principles_path else None,
            "destination_context": str(self.destination_context) if self.destination_context else None,
            "context_inputs": [value.path for value in self.context_inputs],
            "features": [feature.to_json() for feature in self.features],
            "artifacts": sorted(path for path in self.files if path not in self.markers),
            "availability": [value.to_json() for value in self.availability],
            "pending_resources": list(self.pending_resources),
            "branch_ref": self.branch_ref,
            "artifact_prefix": self.artifact_prefix,
            "commit": self.commit,
            "tracking": dict(self.tracking),
            "tracking_context": {"repository": self.tracking_context.get("repository")},
        }

    def baseline(self) -> tuple:
        return (
            self.kind, str(self.repository_root), str(self.workspace_root), str(self.spec_root),
            self.project_id, self.numbering, str(self.principles_path), str(self.destination_context),
            self.filesystem_identity,
            tuple(value.baseline() for value in self.context_inputs),
            tuple(value.baseline() for value in self.files.values()),
            tuple(dataclasses.astuple(value) for value in self.directories.values()),
            self.pending_resources,
            tuple(dataclasses.astuple(value) for value in self.availability),
            self.branch_ref, self.artifact_prefix, self.commit,
            tuple(self.tracking.items()), tuple(self.tracking_context.items()),
        )


@dataclasses.dataclass(frozen=True)
class SpecInspection:
    source: SpecSet | None
    destination: SpecSet
    selections: Mapping[str, Any]
    relationship: Mapping[str, Any]
    transfer_needed: bool
    transfer: str
    snapshot_digest: str

    def to_json(self) -> dict:
        destination = self.destination
        incomplete = (
            bool(destination.pending_resources)
            or destination.principles_path is None
            or any(
                value.condition != "readable"
                and value.purpose in {"artifacts", "specification", "governance", "delivery_state"}
                for selected in (self.source, destination) if selected is not None for value in selected.availability
            )
        )
        for selected in (self.source, destination):
            if selected is None:
                continue
            incomplete |= bool(selected.pending_resources)
            for marker in selected.markers.values():
                try:
                    _delivery_marker(marker)
                except ValueError:
                    incomplete = True
        return {
            "source": self.source.to_json() if self.source else None,
            "destination": destination.to_json(),
            "selections": dict(self.selections),
            "snapshot_digest": self.snapshot_digest,
            "relationship": dict(self.relationship),
            "transfer_needed": self.transfer_needed,
            "transfer": self.transfer,
            "review": "incomplete_review" if incomplete else "not_performed",
            "planning": [
                {
                    "feature": feature.path,
                    "specification": feature.specification,
                    "plan": feature.plan,
                    "tasks": feature.tasks,
                    "complete": all(value == "readable" for value in (feature.specification, feature.plan, feature.tasks)),
                }
                for feature in destination.features
            ],
        }


def _path(value: str | Path, purpose: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"Select a valid {purpose} path.")
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise ValueError(f"Traversal is unsafe in the {purpose} path: {value}")
    path = path.resolve()
    if sys.platform == "darwin" and path.is_dir():
        import fcntl

        try:
            descriptor = os.open(path, getattr(os, "O_EVTONLY", os.O_RDONLY))
        except OSError:
            return path
        try:
            path = Path(os.fsdecode(fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0]))
        finally:
            os.close(descriptor)
    return path


def _ordinary(path: Path, root: Path) -> None:
    """Check context descendants without following symlinks."""
    if not path.is_relative_to(root):
        raise ValueError(f"Unsafe context path outside its selected root: {path}")
    for candidate in reversed((path, *path.parents)):
        if candidate == root or not candidate.is_relative_to(root):
            continue
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"Unsafe context entry: {candidate}")
        if candidate != path and not stat.S_ISDIR(mode):
            raise ValueError(f"Unsafe context parent: {candidate}")


def _capture(path: Path, label: str, root: Path) -> FileSnapshot:
    _ordinary(path, root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return FileSnapshot(label, None, None, None, None, None, "missing")
    except OSError:
        return FileSnapshot(label, None, None, None, None, None, "unavailable")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Unsafe input. Select an ordinary file: {path}")
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError:
        return FileSnapshot(label, None, None, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, "unavailable")
    before_state = (info.st_mode, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)
    after_state = (after.st_mode, after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino)
    if before_state != after_state or len(content) != info.st_size:
        raise ValueError(f"The input changed during inspection. Inspect it again: {path}")
    return FileSnapshot(label, content, hashlib.sha256(content).hexdigest(), stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, "readable")


def _object(value: FileSnapshot) -> dict:
    if value.availability != "readable":
        raise ValueError(f"Required metadata is unavailable: {value.path}")
    try:
        result = json.loads(value.content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"Invalid metadata JSON: {value.path}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"Metadata must contain an object: {value.path}")
    return result


def _inventory(root: Path):
    files, directories, names, markers = {}, {}, {}, {}
    reservations = {"sequential": set(), "timestamp": set()}
    availability, pending = [], []

    def visit(path: Path, relative: str) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Unsafe specification directory: {path}")
        try:
            children = sorted(path.iterdir(), key=lambda child: child.name)
        except OSError:
            availability.append(Availability(relative, "unavailable", "artifacts"))
            directories[relative] = DirectorySnapshot(relative, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, ())
            return
        entries = []
        names[relative] = tuple(sorted({child.name.casefold() for child in children}))
        for child in children:
            rel = child.name if relative == "." else f"{relative}/{child.name}"
            if relative == "." and (child.name == ".merge-specs.lock" or child.name.startswith(".merge-specs-recovery-")):
                pending.append(rel)
                entries.append((child.name, "pending_resource"))
                continue
            try:
                child_info = child.lstat()
            except OSError:
                entries.append((child.name, "unavailable"))
                availability.append(Availability(rel, "unavailable", "artifacts"))
                continue
            if child.name.casefold() == ".specify":
                raise ValueError(f"Project metadata is not a specification artifact. Select its artifact directory: {child}")
            if child.name.casefold() == ".git":
                if not (stat.S_ISDIR(child_info.st_mode) or stat.S_ISREG(child_info.st_mode)):
                    raise ValueError(f"Unsafe Git metadata entry: {child}")
                entries.append((child.name, "git_metadata"))
                continue
            if stat.S_ISDIR(child_info.st_mode):
                entries.append((child.name, "directory"))
                try:
                    scheme, order, _ = classify_feature_name(child.name)
                except ValueError:
                    availability.append(Availability(rel, "invalid_name", "numbering"))
                else:
                    if scheme in reservations:
                        reservations[scheme].add(order)
                visit(child, rel)
            elif stat.S_ISREG(child_info.st_mode):
                entries.append((child.name, "file"))
                value = _capture(child, rel, root)
                files[rel] = value
                if child.name == ".merge-specs.json":
                    markers[rel] = value
                if value.availability != "readable":
                    availability.append(Availability(rel, value.availability, "delivery_state" if child.name == ".merge-specs.json" else "artifacts"))
            else:
                raise ValueError(f"Unsafe specification entry. Use ordinary files and directories: {child}")
        after = path.lstat()
        if (info.st_mode, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino) != (after.st_mode, after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino):
            raise ValueError(f"Directory membership changed during inspection. Inspect it again: {path}")
        directories[relative] = DirectorySnapshot(relative, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns, tuple(entries))

    visit(root, ".")
    features = _feature_inventory(files, directories, markers, availability)
    return (
        MappingProxyType(dict(sorted(files.items()))), MappingProxyType(dict(sorted(directories.items()))),
        tuple(features), MappingProxyType(dict(sorted(names.items()))),
        MappingProxyType({key: tuple(sorted(value)) for key, value in reservations.items()}),
        tuple(sorted(availability, key=lambda value: (value.path, value.purpose))),
        MappingProxyType(dict(sorted(markers.items()))), tuple(sorted(pending)),
    )


def _feature_inventory(files, directories, markers, availability):
    primary = {
        rel for rel, directory in directories.items()
        if rel != "." and any(
            name in {"spec.md", "plan.md", "tasks.md", ".merge-specs.json"} and kind == "file"
            for name, kind in directory.entries
        )
    }
    selected = []
    for rel, directory in sorted(directories.items(), key=lambda item: (item[0].count("/"), item[0])):
        if rel == "." or any(rel.startswith(parent + "/") for parent in selected):
            continue
        if rel not in primary:
            if any(other.startswith(rel + "/") for other in primary):
                continue
            try:
                scheme, _, _ = classify_feature_name(Path(rel).name)
            except ValueError:
                scheme = "custom"
            if scheme == "custom" and not any(kind == "file" for _, kind in directory.entries):
                continue
        selected.append(rel)
    features = []
    for rel in sorted(selected):
        conditions = []
        for name in ("spec.md", "plan.md", "tasks.md"):
            label = f"{rel}/{name}"
            value = files.get(label)
            condition = value.availability if value else "missing"
            conditions.append(condition)
            if condition == "missing":
                availability.append(Availability(label, condition, "specification" if name == "spec.md" else "planning"))
        artifacts = tuple(sorted(path for path in files if path.startswith(rel + "/") and path not in markers))
        features.append(FeatureInventory(rel, artifacts, *conditions))
    return tuple(features)


def _git(repository: Path, *arguments: str, input: bytes | None = None) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repository), *arguments], input=input,
            capture_output=True, check=True,
            env={**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                 "GIT_OPTIONAL_LOCKS": "0", "GIT_LITERAL_PATHSPECS": "1"},
        ).stdout
    except FileNotFoundError as exc:
        raise ValueError("Git is required for branch sources. Select a filesystem source instead.") from exc
    except subprocess.CalledProcessError as exc:
        reason = exc.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Cannot read the selected Git branch or tracking state: {reason}") from exc


def _branch_set(selection: Mapping) -> SpecSet:
    value, ref = selection.get("source"), selection.get("source_branch")
    if not isinstance(ref, str) or not ref:
        raise ValueError("Select an explicit source branch reference.")
    repository = _path(value, "source repository")
    repository = Path(_git(repository, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    commit = _git(repository, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    normalized = _git(repository, "rev-parse", "--symbolic-full-name", "--verify", "--end-of-options", ref).decode().strip()
    normalized = normalized or commit
    entries = _git(repository, "ls-tree", "-r", "-z", "--full-tree", commit, "--", "specs")
    objects = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, path = entry.split(b"\t", 1)
        mode, kind, object_id = header.split()
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ValueError(f"Unsafe branch artifact. Only ordinary files are supported: {os.fsdecode(path)}")
        if not path.startswith(b"specs/"):
            continue
        relative = os.fsdecode(path[len(b"specs/"):])
        if not relative or any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative:
            raise ValueError(f"Unsafe branch artifact path: {relative}")
        objects.append((relative, int(mode, 8) & 0o777, object_id))
    files, markers = {}, {}
    memberships = {".": {}}
    if objects:
        raw = _git(repository, "cat-file", "--batch", input=b"".join(object_id + b"\n" for _, _, object_id in objects))
        position = 0
        for relative, mode, object_id in objects:
            end = raw.index(b"\n", position)
            received, kind, length = raw[position:end].split()
            if received != object_id or kind != b"blob":
                raise ValueError(f"Git returned an unexpected selected artifact: {relative}")
            length = int(length)
            content = raw[end + 1:end + 1 + length]
            position = end + length + 2
            files[relative] = FileSnapshot(relative, content, hashlib.sha256(content).hexdigest(), mode, length, 0, "readable")
            if Path(relative).name == ".merge-specs.json":
                markers[relative] = files[relative]
            parts = relative.split("/")
            for index, name in enumerate(parts):
                parent = "/".join(parts[:index]) or "."
                memberships.setdefault(parent, {})[name] = "file" if index == len(parts) - 1 else "directory"
                if index < len(parts) - 1:
                    memberships.setdefault("/".join(parts[:index + 1]), {})
    directories = {
        path: DirectorySnapshot(path, 0o755, 0, 0, tuple(sorted(children.items())))
        for path, children in sorted(memberships.items())
    }
    reservations = {"sequential": set(), "timestamp": set()}
    availability = []
    for path in directories:
        if path == ".":
            continue
        try:
            scheme, order, _ = classify_feature_name(Path(path).name)
        except ValueError:
            availability.append(Availability(path, "invalid_name", "numbering"))
        else:
            if scheme in reservations:
                reservations[scheme].add(order)
    features = _feature_inventory(files, directories, markers, availability)
    if not objects:
        availability.append(Availability("specs", "missing", "source"))
    return SpecSet(
        "branch", repository, None, None, None, None, None, None, (),
        MappingProxyType(dict(sorted(files.items()))), MappingProxyType(directories), features,
        MappingProxyType({path: tuple(sorted(name.casefold() for name in children)) for path, children in memberships.items()}),
        MappingProxyType({key: tuple(sorted(values)) for key, values in reservations.items()}),
        tuple(availability), MappingProxyType(markers), (), None,
        branch_ref=normalized, artifact_prefix="specs", commit=commit,
        tracking=MappingProxyType({path: "tracked" for path in files}),
        tracking_context=MappingProxyType({"repository": str(repository), "commit": commit}),
    )


def _filesystem_tracking(source: SpecSet) -> SpecSet:
    paths = tuple(source.files)
    if not paths:
        return source
    try:
        repository = Path(_git(source.spec_root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        relative_paths = [str((source.spec_root / path).relative_to(repository)) for path in paths]
        records = _git(repository, "ls-files", "--cached", "--stage", "-z", "--", *relative_paths)
    except ValueError:
        return dataclasses.replace(source, tracking=MappingProxyType({path: "unknown" for path in paths}))
    selected = set(relative_paths)
    tracked = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        name = os.fsdecode(path)
        if name in selected:
            tracked.setdefault(name, []).append(metadata.decode("ascii"))
    return dataclasses.replace(
        source,
        tracking=MappingProxyType({path: "tracked" if relative in tracked else "untracked" for path, relative in zip(paths, relative_paths)}),
        tracking_context=MappingProxyType({
            "repository": str(repository),
            "entries": tuple((path, tuple(values)) for path, values in sorted(tracked.items())),
        }),
    )


def _resolve(selection: Mapping, role: str) -> SpecSet:
    kind = selection.get(f"{role}_kind") or "repository"
    value = selection.get(role)
    if kind == "branch" and role == "source":
        return _branch_set(selection)
    if kind not in ("repository", "set"):
        raise ValueError(f"Invalid {role} kind. Use repository or set.")
    if kind == "set" and value is None:
        raise ValueError(f"A raw {role} set needs an explicit path.")
    if kind == "repository" and role == "destination" and any(selection.get(key) is not None for key in ("destination_numbering", "destination_principles", "destination_context")):
        raise ValueError("Raw destination flags require destination_kind set.")
    context, conditions = [], []
    repository = workspace = principles = code = None
    identity = numbering = None

    def capture(path: Path, boundary: Path) -> FileSnapshot:
        value = _capture(path, str(path), boundary)
        context.append(value)
        return value

    if kind == "repository":
        repository = _path(value, role) if value is not None else find_repository()
        if not (repository / ".specify").is_dir():
            raise ValueError(f"No Spec Kit repository found at {repository}. Select a raw set explicitly.")
        _ordinary(repository / ".specify", repository)
        if os.path.lexists(repository / ".specify/workspace.json"):
            raise ValueError(f"An external workspace is not a code repository: {repository}")
        locator = capture(repository / ".specify/project.json", repository)
        workspace = _path(workspace_root_for(repository), "workspace")
        if locator.availability != "missing":
            identity = _object(locator).get("project_id")
            record = project_record_path(identity)
            capture(record, record.parent)
            capture(workspace / ".specify/workspace.json", workspace)
        root = _path(workspace / "specs", "specification root")
        if role == "destination":
            options = capture(workspace / ".specify/init-options.json", workspace)
            if options.availability == "unavailable":
                conditions.append(Availability(options.path, "unavailable", "numbering"))
            else:
                numbering = (_object(options) if options.availability != "missing" else {}).get("feature_numbering", "sequential")
            principles = workspace / ".specify/memory/constitution.md"
            code = repository
    else:
        root = _path(value, role)
        if role == "destination":
            numbering = selection.get("destination_numbering")
            if selection.get("destination_principles") is not None:
                principles = _path(selection["destination_principles"], "principles")
            if selection.get("destination_context") is not None:
                code = _path(selection["destination_context"], "destination context")
                if not code.is_dir():
                    raise ValueError(f"Destination context is unavailable: {code}")
    if any(part.casefold() == ".git" for part in root.parts) or (
        kind == "set" and any(part.casefold() == ".specify" for part in root.parts)
    ) or any(root.is_relative_to(selected / ".specify") for selected in (repository, workspace) if selected is not None):
        raise ValueError("A project metadata directory is not a specification set. Select the artifact root.")
    if numbering is not None and numbering not in ("sequential", "timestamp"):
        raise ValueError("Destination numbering must be sequential or timestamp.")
    if principles is not None:
        value = capture(principles, workspace if workspace is not None else principles.parent)
        conditions.append(Availability(str(principles), value.availability, "governance"))
    elif role == "destination":
        conditions.append(Availability("principles", "missing", "governance"))
    try:
        info = root.lstat()
    except OSError as exc:
        raise ValueError(f"Specification set is unavailable: {root}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Unsafe specification root. Select a directory: {root}")
    files, directories, features, names, prefixes, availability, markers, pending = _inventory(root)
    # Make sure public resolution and captured metadata describe the same inputs.
    for entry in context:
        path = Path(entry.path)
        boundary = repository if repository is not None and path.is_relative_to(repository) else workspace if workspace is not None and path.is_relative_to(workspace) else path.parent
        if _capture(path, entry.path, boundary) != entry:
            raise ValueError(f"Context changed during inspection. Inspect it again: {path}")
    return SpecSet(
        kind, repository, workspace, root, identity, numbering, principles, code,
        tuple(context), files, directories, features, names, prefixes,
        tuple([*availability, *conditions]), markers, pending, (info.st_dev, info.st_ino),
    )


def _physical_ancestor(root: Path, identity: tuple[int, int]) -> bool:
    for parent in root.parents:
        try:
            info = parent.stat()
        except OSError as exc:
            raise ValueError(f"Cannot verify the selected root boundary: {parent}") from exc
        if (info.st_dev, info.st_ino) == identity:
            return True
    return False


def inspect_spec_sets(selection: Mapping[str, Any]) -> SpecInspection:
    """Resolve selected sets and capture a frozen baseline without writes."""
    if not isinstance(selection, Mapping):
        raise ValueError("Specification selections must contain an object.")
    supported = {
        "source", "source_kind", "source_branch", "destination", "destination_kind",
        "destination_numbering", "destination_principles", "destination_context",
    }
    if any(key not in supported for key in selection):
        raise ValueError("Specification selections contain an unknown selector.")
    if selection.get("source") is None and any(selection.get(key) is not None for key in ("source_kind", "source_branch")):
        raise ValueError("Source kind and branch selectors need an explicit source path.")
    if selection.get("source_branch") is not None and selection.get("source_kind") != "branch":
        raise ValueError("A source branch needs source_kind branch.")
    destination = _resolve(selection, "destination")
    source = _resolve(selection, "source") if selection.get("source") is not None else None
    if source is not None and source.kind != "branch":
        source = _filesystem_tracking(source)
    shared = source is not None and source.kind != "branch" and (
        source.spec_root == destination.spec_root or source.filesystem_identity == destination.filesystem_identity
    )
    if source is not None and source.kind != "branch" and not shared and (
        source.spec_root.is_relative_to(destination.spec_root) or destination.spec_root.is_relative_to(source.spec_root)
        or _physical_ancestor(source.spec_root, destination.filesystem_identity)
        or _physical_ancestor(destination.spec_root, source.filesystem_identity)
    ):
        raise ValueError("Distinct nested specification roots overlap. Select separate roots.")
    same_project = source is not None and (shared or source.project_id is not None and source.project_id == destination.project_id)
    relationship = MappingProxyType({
        "relationship": "same_project" if same_project else "unconfirmed",
        "evidence": "shared_filesystem" if shared else "matching_project_id" if same_project else "different_or_missing_project_id",
        "confirmation": None if same_project or source is None else "Confirm that both sets belong to the same project.",
    })
    resolved = {
        "destination": str(destination.repository_root if destination.kind == "repository" else destination.spec_root),
        "destination_kind": destination.kind,
    }
    if destination.kind == "set":
        resolved.update({
            "destination_numbering": destination.numbering,
            "destination_principles": str(destination.principles_path) if destination.principles_path else None,
            "destination_context": str(destination.destination_context) if destination.destination_context else None,
        })
    if source is not None:
        resolved.update({"source": str(source.repository_root if source.kind in ("repository", "branch") else source.spec_root), "source_kind": source.kind})
        if source.kind == "branch":
            resolved["source_branch"] = source.branch_ref
    if shared and (source.files != destination.files or source.directories != destination.directories):
        raise ValueError("The shared set changed during inspection. Inspect it again.")
    baseline = (source.baseline() if source else None, destination.baseline())
    digest = hashlib.sha256(json.dumps(baseline, default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value), separators=(",", ":")).encode()).hexdigest()
    return SpecInspection(source, destination, MappingProxyType(resolved), relationship, source is not None and not shared, "not_needed" if shared else "not_requested", digest)


def _json(value):
    if isinstance(value, Mapping):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _binding(value) -> str:
    return hashlib.sha256(json.dumps(_json(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _issue(kind: str, path: str, message: str, **details) -> dict:
    return {"kind": kind, "path": path, "message": message, **details}


def _relative(value, purpose: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value or ":" in value:
        raise ValueError(f"Select a safe relative {purpose} path.")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"Traversal or an absolute path is unsafe for {purpose}: {value}")
    if any(len(os.fsencode(part)) > 255 for part in parts):
        raise ValueError(f"A {purpose} name exceeds the filesystem limit: {value}")
    if any(part.casefold() in {".git", ".specify"} for part in parts):
        raise ValueError(f"Project metadata is not an approved {purpose} path: {value}")
    if parts[0] == ".merge-specs.lock" or parts[0].startswith(".merge-specs-recovery-"):
        raise ValueError(f"The {purpose} path uses the temporary resource namespace: {value}")
    return value


def _rows(value, name: str) -> list[dict]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"{name} must contain a list of objects.")
    return [dict(row) for row in value]


def _bytes(value, purpose: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{purpose} must contain a Base64 string.")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"{purpose} contains invalid Base64.") from exc


def _metadata(decision: Mapping, fallback: FileSnapshot | DirectorySnapshot | None) -> tuple[int, int]:
    mode = decision.get("mode", fallback.mode if fallback is not None else 0o666 if os.name == "nt" else 0o644)
    mtime = decision.get("mtime_ns", fallback.mtime_ns if fallback is not None else 0)
    if type(mode) is not int or not 0 <= mode <= 0o7777:
        raise ValueError("The approved mode must be an integer between 0 and 4095.")
    if os.name == "nt":
        supported = {0o555, 0o777} if isinstance(fallback, DirectorySnapshot) else {0o444, 0o666}
        if "mode" in decision and mode not in supported:
            raise ValueError("Select a mode that this platform supports.")
        if "mode" not in decision:
            mode = (0o777 if mode & 0o200 else 0o555) if isinstance(fallback, DirectorySnapshot) else (0o666 if mode & 0o200 else 0o444)
    if type(mtime) is not int or not -(2**63) <= mtime <= 2**63 - 1:
        raise ValueError("The approved modification time must be a signed 64-bit integer.")
    if os.name == "nt" and mtime % 100:
        raise ValueError("Use a modification time with 100-nanosecond precision on this platform.")
    return mode, mtime


def _replacements(content: bytes, value, path: str) -> bytes:
    rows = _rows(value, "replacements")
    spans = []
    for row in rows:
        if set(row) not in ({"old", "new"}, {"old", "new", "count"}):
            raise ValueError("Each reference replacement needs old and new, with an optional count.")
        old, new, count = row["old"], row["new"], row.get("count")
        if not isinstance(old, str) or not old or not isinstance(new, str) or count is not None and (type(count) is not int or count < 1):
            raise ValueError(f"A reference replacement has invalid literal values: {path}")
        old_bytes, new_bytes = old.encode("utf-8"), new.encode("utf-8")
        start, found = 0, 0
        while (position := content.find(old_bytes, start)) >= 0:
            spans.append((position, position + len(old_bytes), new_bytes))
            found += 1
            start = position + len(old_bytes)
        if not found or count is not None and found != count:
            raise ValueError(f"The reference occurrence count changed. Review the replacement: {path}")
    spans.sort()
    output, start = [], 0
    for left, right, replacement in spans:
        if left < start:
            raise ValueError(f"Reference replacements overlap. Review their exact ranges: {path}")
        output.extend((content[start:left], replacement))
        start = right
    output.append(content[start:])
    return b"".join(output)


def _difference(before: bytes | None, after: bytes | None, path: str) -> str:
    before, after = before or b"", after or b""
    try:
        if b"\0" in before or b"\0" in after:
            raise UnicodeError
        original, replacement = before.decode("utf-8"), after.decode("utf-8")
    except UnicodeError:
        return (
            f"Binary difference: {path}\n"
            f"Before: {len(before)} bytes, SHA-256 {hashlib.sha256(before).hexdigest()}\n"
            f"After: {len(after)} bytes, SHA-256 {hashlib.sha256(after).hexdigest()}\n"
        )
    rows = difflib.unified_diff(original.splitlines(keepends=True), replacement.splitlines(keepends=True),
                                fromfile=f"destination/{path}", tofile=f"proposed/{path}")
    return "".join(row if row.endswith(("\n", "\r")) else row + "\n\\ No newline at end of file\n" for row in rows)


@dataclasses.dataclass(frozen=True)
class MergeOperation:
    kind: str
    path: str
    content: bytes | None
    mode: int
    mtime_ns: int
    resolution: str
    source_path: str | None = None
    replacements: tuple = ()
    difference: str = ""

    def to_json(self) -> dict:
        value = {
            "kind": self.kind, "path": self.path, "mode": self.mode, "mtime_ns": self.mtime_ns,
            "resolution": self.resolution, "difference": self.difference,
        }
        if self.content is not None:
            value.update(size=len(self.content), fingerprint=hashlib.sha256(self.content).hexdigest())
        if self.source_path is not None:
            value.update(source_path=self.source_path, replacements=_json(self.replacements))
        elif self.resolution == "replace":
            value["content"] = base64.b64encode(self.content).decode("ascii")
        return value


@dataclasses.dataclass(frozen=True)
class SpecMergePreview:
    inspection: SpecInspection
    relationship: Mapping
    correspondences: tuple
    operations: tuple[MergeOperation, ...]
    artifacts: tuple
    conflicts: tuple
    temporary_resources: Mapping
    notices: tuple
    affected_features: tuple[str, ...]
    dependent_features: tuple[str, ...]
    proposal_digest: str
    replay_inputs: Mapping

    @property
    def snapshot_digest(self) -> str:
        return self.inspection.snapshot_digest

    def to_json(self) -> dict:
        return {
            "selections": dict(self.inspection.selections), "snapshot_digest": self.snapshot_digest,
            "relationship": _json(self.relationship), "correspondences": _json(self.correspondences),
            "operations": [operation.to_json() for operation in self.operations],
            "artifacts": _json(self.artifacts), "conflicts": _json(self.conflicts),
            "temporary_resources": _json(self.temporary_resources), "notices": _json(self.notices),
            "affected_features": list(self.affected_features), "dependent_features": list(self.dependent_features),
            "review": self.inspection.to_json()["review"], "planning": self.inspection.to_json()["planning"],
            "review_scope": sorted(set(self.affected_features) | set(self.dependent_features)) if self.operations else (
                list(self.dependent_features) if self.dependent_features else [feature.path for feature in self.inspection.destination.features]
            ),
            "proposal_digest": self.proposal_digest, "replay_inputs": _json(self.replay_inputs),
        }


@dataclasses.dataclass(frozen=True)
class MergeResult:
    transfer: str
    data_outcome: str
    changed_paths: tuple[str, ...] = ()
    cleanup_required: bool = False
    recovery_directory: str | None = None
    remaining_operations: tuple = ()
    originals: tuple = ()
    conflicts: tuple = ()

    def to_json(self) -> dict:
        return {
            "transfer": self.transfer, "data_outcome": self.data_outcome,
            "changed_paths": list(self.changed_paths), "cleanup_required": self.cleanup_required,
            "recovery_directory": self.recovery_directory, "remaining_operations": _json(self.remaining_operations),
            "originals": _json(self.originals), "conflicts": _json(self.conflicts),
            "review": "incomplete_review" if self.data_outcome == "recovery_required" or self.cleanup_required else "not_performed",
        }


def _feature_files(selected: SpecSet, feature: str) -> dict[str, FileSnapshot]:
    return {
        path[len(feature) + 1:]: value for path, value in selected.files.items()
        if path.startswith(feature + "/") and path not in selected.markers
    }


def _artifact_state(files: Mapping[str, bytes | FileSnapshot]) -> str:
    return _binding([
        (path, hashlib.sha256(value.content if isinstance(value, FileSnapshot) else value).hexdigest())
        for path, value in sorted(files.items())
    ])


def _origin(source: SpecSet) -> dict:
    if source.kind == "branch":
        return {"kind": "branch", "repository": str(source.repository_root), "ref": source.branch_ref, "artifact_prefix": source.artifact_prefix}
    return {"kind": "filesystem", "spec_root": str(source.spec_root)}


def _origin_key(value: Mapping) -> str:
    return _binding((value["source"], value["source_feature"]))


def _delivery_marker(value: FileSnapshot) -> dict:
    marker = _object(value)
    if set(marker) != {"origins", "destination_state_digest"} or not re.fullmatch(r"[0-9a-f]{64}", str(marker["destination_state_digest"])):
        raise ValueError(f"Current delivery state is malformed. Review this marker: {value.path}")
    origins = _rows(marker["origins"], "origins")
    keys = set()
    for origin in origins:
        if set(origin) != {"source", "source_feature", "source_state_digest"}:
            raise ValueError(f"Current delivery state has an invalid origin: {value.path}")
        source = origin["source"]
        if not isinstance(source, dict) or source.get("kind") not in ("filesystem", "branch"):
            raise ValueError(f"Current delivery state has an invalid selection: {value.path}")
        expected = {"kind", "spec_root"} if source["kind"] == "filesystem" else {"kind", "repository", "ref", "artifact_prefix"}
        if set(source) != expected or any(not isinstance(item, str) or not item for item in source.values()):
            raise ValueError(f"Current delivery state has invalid selection fields: {value.path}")
        root = source["spec_root"] if source["kind"] == "filesystem" else source["repository"]
        if not Path(root).is_absolute() or ".." in Path(root).parts:
            raise ValueError(f"Current delivery state has an unsafe origin label: {value.path}")
        _relative(origin["source_feature"], "stored source feature")
        if not re.fullmatch(r"[0-9a-f]{64}", str(origin["source_state_digest"])):
            raise ValueError(f"Current delivery state has an invalid source fingerprint: {value.path}")
        key = _origin_key(origin)
        if key in keys:
            raise ValueError(f"Current delivery state repeats one origin: {value.path}")
        keys.add(key)
    if not origins:
        raise ValueError(f"Current delivery state has no current origin: {value.path}")
    return {"origins": origins, "destination_state_digest": marker["destination_state_digest"]}


def _resource_paths(value, needed: bool) -> dict:
    if value is None:
        return {"lock": ".merge-specs.lock", "recovery_directory": f".merge-specs-recovery-{uuid4().hex}"} if needed else {}
    if not isinstance(value, Mapping) or (value and set(value) != {"lock", "recovery_directory"}):
        raise ValueError("Temporary resources need exact lock and recovery paths.")
    if not needed:
        if value:
            raise ValueError("A no-operation proposal must not reserve temporary resources.")
        return {}
    if value.get("lock") != ".merge-specs.lock":
        raise ValueError("The merge lock must use the fixed .merge-specs.lock path.")
    recovery = value.get("recovery_directory")
    if not isinstance(recovery, str) or not re.fullmatch(r"\.merge-specs-recovery-[0-9a-f]{32}", recovery):
        raise ValueError("The recovery path must use .merge-specs-recovery- followed by UUID4 hex.")
    if UUID(hex=recovery.rsplit("-", 1)[1]).version != 4:
        raise ValueError("The recovery directory must use an approved UUID4 value.")
    return dict(value)


def _proposal_binding(inspection, relationship, correspondences, operations, artifacts, conflicts, resources, dependents, replay) -> str:
    return _binding({
        "snapshot": inspection.snapshot_digest, "relationship": relationship,
        "correspondences": correspondences, "operations": [operation.to_json() for operation in operations],
        "artifacts": artifacts, "conflicts": conflicts, "temporary_resources": resources,
        "dependent_features": dependents,
        "decisions": {key: value for key, value in replay.items() if key != "proposal_digest"},
    })


def _notices(inspection: SpecInspection, correspondences) -> list[dict]:
    source, notices = inspection.source, []
    if source is None:
        return notices
    if source.kind == "branch":
        notices.append(_issue("branch_limits", str(source.repository_root),
                              "Branch history cannot supply ignored or unrecorded artifacts. Select a worktree or workspace for those artifacts."))
    tracked = [path for path, status in source.tracking.items() if status == "tracked"]
    unknown = [path for path, status in source.tracking.items() if status == "unknown"]
    repository = source.tracking_context.get("repository")
    renamed = [row for row in correspondences if row.get("destination_feature") != row.get("source_feature") and row.get("relation") == "independent"]
    if tracked:
        notices.append(_issue(
            "later_git_merge", str(repository),
            "A later Git merge in the artifact repository can transfer original paths. Prefer that separate Git merge, then reconciliation-only.",
            repository=repository, selected_paths=tracked, renamed_features=_json(renamed),
        ))
        if renamed:
            notices.append(_issue("renamed_duplicate_risk", str(repository),
                                  "A later Git merge can duplicate these renamed imports. This command does not merge Git history.",
                                  mappings=[{"source_feature": row["source_feature"], "destination_feature": row["destination_feature"]} for row in renamed]))
        if source.repository_root is not None and repository != str(source.repository_root):
            notices.append(_issue("separate_artifact_repository", str(repository),
                                  "The artifact repository differs from the code repository. A code-repository merge cannot deliver its tracked artifacts."))
    if unknown:
        notices.append(_issue(
            "unknown_tracking", str(source.spec_root),
            "Git tracking is unknown. If these artifacts are tracked, a later Git merge can transfer original paths and duplicate renamed imports.",
            repository=repository, selected_paths=unknown,
        ))
    return notices


def _mapped_candidates(files: Mapping[str, FileSnapshot], destination: SpecSet, choices: Mapping, target: str | None) -> list[str]:
    candidates = []
    for feature in destination.features:
        existing = feature.path
        if f"{existing}/.merge-specs.json" in destination.markers and existing != target:
            continue
        complete = bool(files) or existing == target
        for relative, value in files.items():
            path = f"{existing}/{relative}"
            choice = choices.get(path, {})
            if choice.get("source_path") != value.path:
                choice = {}
            proposed = _replacements(value.content, choice.get("replacements", []), value.path)
            observed = destination.files.get(path)
            if observed is None or observed.content != proposed:
                complete = False
                break
        if complete:
            candidates.append(existing)
    return candidates


def _allocate_imports(inspection: SpecInspection, rows: list[dict], conflicts: list[dict]) -> None:
    destination, source = inspection.destination, inspection.source
    names = {scope: set(values) for scope, values in destination.occupied_names.items()}
    prefixes = {scheme: set(values) for scheme, values in destination.occupied_prefixes.items()}
    for row in rows:
        if row["relation"] == "independent":
            for path in source.directories:
                if path.startswith(row["source_feature"] + "/"):
                    try:
                        scheme, order, _ = classify_feature_name(Path(path).name)
                    except ValueError:
                        continue
                    if scheme in prefixes:
                        prefixes[scheme].add(order)
    pending = []

    def claim(path: str) -> bool:
        scope, _, name = path.rpartition("/")
        scope = scope or "."
        try:
            scheme, order, _ = classify_feature_name(name)
        except ValueError as exc:
            conflicts.append(_issue("name", path, str(exc)))
            return False
        occupied = name.casefold() in names.setdefault(scope, set()) or scheme in prefixes and order in prefixes[scheme]
        if occupied:
            return False
        names[scope].add(name.casefold())
        if scheme in prefixes:
            prefixes[scheme].add(order)
        return True

    for row in rows:
        if row["relation"] != "independent":
            continue
        original = row["source_feature"]
        try:
            original_scheme, original_order, _ = classify_feature_name(Path(original).name)
        except ValueError as exc:
            conflicts.append(_issue("name", original, str(exc)))
            continue
        chosen = row.get("destination_feature")
        if chosen is not None:
            _relative(chosen, "destination feature")
            original_scope, _, original_name = original.rpartition("/")
            original_collision = original_name.casefold() in names.get(original_scope or ".", set()) or (
                original_scheme in prefixes and original_order in prefixes[original_scheme]
            )
            if original_collision and destination.numbering is None:
                conflicts.append(_issue("numbering", original, "Select destination numbering before approving a collision replacement."))
                continue
            if original_collision:
                try:
                    chosen_scheme, _, _ = classify_feature_name(Path(chosen).name)
                except ValueError as exc:
                    conflicts.append(_issue("name", chosen, str(exc)))
                    continue
                if chosen_scheme != destination.numbering:
                    conflicts.append(_issue("numbering", chosen, "The collision replacement must use the destination numbering choice."))
                    continue
            if not claim(chosen):
                conflicts.append(_issue("name", chosen, "The approved imported name or prefix is occupied. Review a new name."))
        else:
            try:
                classify_feature_name(Path(original).name)
            except ValueError as exc:
                conflicts.append(_issue("name", original, str(exc)))
                continue
            if claim(original):
                row["destination_feature"] = original
            else:
                pending.append(row)

    for row in pending:
        original = row["source_feature"]
        if destination.numbering is None:
            conflicts.append(_issue("numbering", original, "Select destination numbering before allocating a collision replacement."))
            continue
        scheme, order, suffix = classify_feature_name(Path(original).name)
        if destination.numbering == "sequential":
            next_number = max(prefixes["sequential"], default=0) + 1
            if next_number > 2**63 - 1:
                conflicts.append(_issue("name", original, "No sequential prefix remains in the signed 64-bit range."))
                continue
            name = f"{next_number:03d}-{suffix}"
        else:
            if prefixes["timestamp"]:
                moment = max(prefixes["timestamp"])
                advance = True
            elif scheme == "timestamp":
                moment, advance = order, True
            else:
                times = [value.mtime_ns for value in _feature_files(source, original).values() if value.mtime_ns is not None]
                moment = datetime.fromtimestamp(max(times, default=0) // 1_000_000_000, timezone.utc).replace(tzinfo=None)
                advance = False
            try:
                if advance:
                    moment += timedelta(seconds=1)
            except OverflowError:
                conflicts.append(_issue("name", original, "No timestamp prefix remains in the calendar range."))
                continue
            name = f"{moment.year:04d}{moment.month:02d}{moment.day:02d}-{moment.hour:02d}{moment.minute:02d}{moment.second:02d}-{suffix}"
        scope = original.rpartition("/")[0]
        chosen = f"{scope}/{name}" if scope else name
        try:
            _relative(chosen, "imported feature")
        except ValueError as exc:
            conflicts.append(_issue("name", chosen, str(exc)))
            continue
        if claim(chosen):
            row["destination_feature"] = chosen
        else:
            conflicts.append(_issue("name", chosen, "The allocated imported name or prefix is occupied. Review an explicit replacement."))


def prepare_spec_merge(inspection: SpecInspection, decisions: Mapping[str, Any]) -> SpecMergePreview:
    """Validate exact additive changes. Keep bytes and per-file baselines in this process."""
    if not isinstance(inspection, SpecInspection) or not isinstance(decisions, Mapping):
        raise ValueError("Prepare a merge from an inspection and a decision object.")
    supported = {
        "selections", "snapshot_digest", "proposal_digest", "relationship", "correspondences",
        "artifact_decisions", "dependent_features", "temporary_resources",
    }
    if any(key not in supported for key in decisions):
        raise ValueError("The merge proposal contains an unknown field. Submit compact decisions, not an inventory or operations.")
    try:
        decisions = json.loads(json.dumps(_json(decisions), allow_nan=False))
    except (ValueError, TypeError) as exc:
        raise ValueError("The merge decisions must contain JSON data only.") from exc
    if decisions.get("selections", dict(inspection.selections)) != dict(inspection.selections):
        raise ValueError("The proposal selections differ from the reviewed selections.")
    if decisions.get("snapshot_digest", inspection.snapshot_digest) != inspection.snapshot_digest:
        raise ValueError("The original snapshot is stale. Inspect the selected sets and review again.")
    if inspect_spec_sets(inspection.selections).snapshot_digest != inspection.snapshot_digest:
        raise ValueError("The reviewed inputs changed. Inspect the selected sets and review again.")
    source, destination = inspection.source, inspection.destination
    source_features = {feature.path: feature for feature in source.features} if source else {}
    destination_features = {feature.path: feature for feature in destination.features}
    conflicts, artifacts, operations, rows = [], [], {}, []
    supplied_rows = _rows(decisions.get("correspondences", []), "correspondences")
    supplied = {}
    for row in supplied_rows:
        allowed = {"source_feature", "destination_feature", "relation", "evidence", "delivery_review",
                   "rebind_origin", "preserve_marker_as", "preserve_source_marker_as"}
        if set(row) - allowed:
            raise ValueError("A correspondence contains an unknown decision field.")
        feature = _relative(row.get("source_feature"), "source feature")
        if feature not in source_features or feature in supplied:
            raise ValueError(f"Select each inventoried source feature at most once: {feature}")
        if row.get("relation") not in ("independent", "revision", "already_delivered", "skipped"):
            raise ValueError(f"Select an explicit feature correspondence: {feature}")
        if row.get("destination_feature") is not None:
            _relative(row["destination_feature"], "destination feature")
        if row.get("delivery_review") not in (None, "confirmed", "independent", "additional"):
            raise ValueError(f"Select an explicit current-delivery review decision: {feature}")
        rebind = row.get("rebind_origin")
        if rebind is not None and (not isinstance(rebind, dict) or set(rebind) != {"source", "source_feature"}):
            raise ValueError("An origin rebind needs the exact previous source and source_feature fields.")
        supplied[feature] = row
    artifact_rows = _rows(decisions.get("artifact_decisions", []), "artifact_decisions")
    artifact_choices = {}
    for row in artifact_rows:
        if set(row) - {"path", "source_path", "resolution", "content", "replacements", "mode", "mtime_ns", "unresolved", "preserve_path"}:
            raise ValueError("An artifact decision contains an unknown field.")
        path = _relative(row.get("path"), "destination artifact")
        if path in artifact_choices:
            raise ValueError(f"Select each destination artifact at most once: {path}")
        if row.get("resolution") not in ("copy_source", "keep_destination", "replace", "preserve_marker"):
            raise ValueError(f"Select an explicit artifact resolution: {path}")
        if row.get("source_path") is not None:
            selected_path = _relative(row["source_path"], "source artifact")
            if source is None or selected_path not in source.files:
                raise ValueError(f"The source-copy path is not in the selected source: {selected_path}")
        if row["resolution"] == "replace":
            _bytes(row.get("content"), f"Replacement for {path}")
        elif "content" in row:
            raise ValueError(f"Only an authored replacement can contain Base64 bytes: {path}")
        if "replacements" in row:
            _rows(row["replacements"], "replacements")
            if row["resolution"] != "copy_source":
                raise ValueError(f"Literal replacements require a source-copy recipe: {path}")
        unresolved = row.get("unresolved", [])
        if not isinstance(unresolved, list) or any(not isinstance(reason, str) or not reason for reason in unresolved):
            raise ValueError("Unresolved references must contain a list of review reasons.")
        _metadata(row, None)
        choice = dict(row)
        if row["resolution"] == "copy_source" and row.get("source_path") is None:
            reviewed_sources = {
                f"{correspondence['source_feature']}/{path[len(target) + 1:]}"
                for correspondence in supplied.values()
                if (target := correspondence.get("destination_feature") or correspondence["source_feature"])
                and path.startswith(target + "/") and correspondence["relation"] != "skipped"
            }
            reviewed_sources.intersection_update(source.files if source else ())
            if len(reviewed_sources) == 1:
                choice["source_path"] = reviewed_sources.pop()
        artifact_choices[path] = choice
    dependents = decisions.get("dependent_features", [])
    if not isinstance(dependents, list) or any(not isinstance(feature, str) for feature in dependents):
        raise ValueError("Dependent features must contain a list of inventoried destination paths.")
    for feature in dependents:
        _relative(feature, "dependent feature")
        if feature not in destination_features:
            raise ValueError(f"The dependent feature is not in the destination inventory: {feature}")
    dependents = tuple(sorted(set(dependents)))
    relationship = decisions.get("relationship", dict(inspection.relationship))
    if not isinstance(relationship, dict) or relationship.get("relationship") not in ("same_project", "unrelated", "unconfirmed"):
        raise ValueError("Select same_project, unrelated, or unconfirmed as the project relationship.")
    if set(relationship) - {"relationship", "evidence", "confirmation"}:
        raise ValueError("The project relationship contains an unknown field.")
    if source is not None and relationship["relationship"] == "unrelated":
        conflicts.append(_issue("relationship", ".", "These projects are unrelated. The helper refuses transfer."))

    markers, invalid_markers = {}, {}
    for path, value in destination.markers.items():
        try:
            markers[path] = _delivery_marker(value)
        except ValueError as exc:
            invalid_markers[path] = str(exc)
    consumed, overridden = set(), set()
    active = []
    origin = _origin(source) if source else None
    for feature in sorted(source_features) if inspection.transfer_needed else []:
        row = dict(supplied.get(feature, {"source_feature": feature}))
        files = _feature_files(source, feature)
        if any(value.content is None for value in files.values()):
            conflicts.append(_issue("availability", feature, "A selected source artifact is unreadable. Restore access and inspect again."))
            rows.append(row)
            continue
        source_digest = _artifact_state(files)
        key = _origin_key({"source": origin, "source_feature": feature})
        exact, candidates = [], []
        for path, marker in markers.items():
            containing = path.rsplit("/", 1)[0] if "/" in path else "."
            for previous in marker["origins"]:
                if _origin_key(previous) == key:
                    exact.append((containing, marker, previous))
                elif previous["source_feature"] == feature or (
                    previous["source"] == origin and previous["source_feature"] not in source_features
                ) or previous["source_state_digest"] == source_digest and previous["source_feature"] not in source_features:
                    candidates.append((containing, marker, previous))
        if len(exact) == 1:
            containing, marker, previous = exact[0]
            destination_files = _feature_files(destination, containing)
            unchanged = all(value.content is not None for value in destination_files.values()) and marker["destination_state_digest"] == _artifact_state(destination_files)
            if previous["source_state_digest"] == source_digest and unchanged and containing in destination_features:
                targets = {containing, row.get("destination_feature")}
                for path, choice in artifact_choices.items():
                    if any(target and path.startswith(target + "/") for target in targets) or (
                        choice.get("source_path") and choice["source_path"].startswith(feature + "/")
                    ):
                        consumed.add(path)
                        overridden.add(path)
                for relative in destination_files:
                    artifacts.append(_issue("artifact", f"{containing}/{relative}", "Retain the unchanged previously delivered artifact.",
                                            classification="identical", resolution="keep_destination", difference=""))
                rows.append({
                    "source_feature": feature, "destination_feature": containing, "relation": "already_delivered",
                    "evidence": "unchanged_current_delivery_state",
                    "delivery_candidates": [containing],
                })
                continue
        row["delivery_candidates"] = sorted(
            {item[0] for item in (*exact, *candidates)}
            | set(_mapped_candidates(files, destination, artifact_choices, row.get("destination_feature")))
        )
        if row.get("rebind_origin") is not None:
            rebind_key = _origin_key(row["rebind_origin"])
            matching_rebinds = [item for item in (*exact, *candidates) if item[0] == row.get("destination_feature") and _origin_key(item[2]) == rebind_key]
            if len(matching_rebinds) != 1 or exact and rebind_key != key:
                conflicts.append(_issue("delivery_state", feature, "Select the exact previous origin in the reviewed containing feature before rebinding."))
                rows.append(row)
                continue
        reviewed = row.get("delivery_review") == "confirmed"
        selected_target = row.get("destination_feature")
        if exact:
            if len(exact) != 1 or not reviewed or selected_target != exact[0][0] or row.get("relation") not in {"revision", "already_delivered", "skipped"}:
                conflicts.append(_issue("delivery_state", feature, "Current delivery state changed or is ambiguous. Review the containing destination feature.",
                                        delivery_candidates=sorted({item[0] for item in exact})))
                rows.append(row)
                continue
        elif candidates:
            reviewed_target = [item for item in candidates if item[0] == selected_target]
            rebind = row.get("rebind_origin")
            rebind_matches = [item for item in reviewed_target if isinstance(rebind, dict) and _origin_key(rebind) == _origin_key(item[2])]
            if row.get("relation") == "skipped":
                pass
            elif row.get("relation") == "independent" and row.get("delivery_review") == "independent":
                pass
            elif row.get("relation") in {"revision", "already_delivered"} and row.get("delivery_review") == "additional" and reviewed_target:
                pass
            elif not reviewed or len(rebind_matches) != 1:
                conflicts.append(_issue("delivery_state", feature, "A moved or renamed origin requires an explicit correspondence and origin rebind.",
                                        delivery_candidates=sorted({item[0] for item in candidates})))
                rows.append(row)
                continue
        if row.get("relation") is None:
            conflicts.append(_issue("correspondence", feature, "Confirm whether this feature is independent, a revision, already delivered, or skipped."))
            rows.append(row)
            continue
        if row["relation"] == "skipped":
            rows.append(row)
            continue
        if row["relation"] in {"revision", "already_delivered"} and selected_target not in destination_features:
            conflicts.append(_issue("correspondence", feature, "Select an existing destination feature for this correspondence."))
            rows.append(row)
            continue
        for source_marker_path, source_marker in source.markers.items():
            if not source_marker_path.startswith(feature + "/"):
                continue
            try:
                if source_marker_path != f"{feature}/.merge-specs.json":
                    raise ValueError("A nested reserved-name file is not feature-local delivery state.")
                _delivery_marker(source_marker)
            except ValueError:
                preserved = source_marker_path == f"{feature}/.merge-specs.json" and row.get("preserve_source_marker_as")
                preserved = preserved or any(
                    choice.get("resolution") == "preserve_marker" and choice.get("source_path") == source_marker_path
                    for choice in artifact_choices.values()
                )
                if not preserved:
                    conflicts.append(_issue("delivery_state", source_marker_path, "Reserved-name source content needs an explicit preservation path before transfer."))
        active.append(row)
        rows.append(row)
    if inspection.transfer_needed and ".merge-specs.json" in source.markers:
        preserved = any(
            choice.get("resolution") == "preserve_marker" and choice.get("source_path") == ".merge-specs.json"
            for choice in artifact_choices.values()
        )
        if not preserved:
            conflicts.append(_issue(
                "delivery_state", ".merge-specs.json", "Reserved-name source-root content needs an explicit preservation path before transfer.",
            ))
    evidence = relationship.get("evidence")
    if active and inspection.relationship["relationship"] == "unconfirmed":
        if relationship["relationship"] != "same_project" or not isinstance(evidence, str) or not evidence.strip():
            conflicts.append(_issue("relationship", ".", "Confirm that both sets belong to the same project before transfer."))

    for path, message in invalid_markers.items() if inspection.transfer_needed else []:
        choice = artifact_choices.get(path, {})
        targeted = [row for row in active if row.get("destination_feature") == path.rsplit("/", 1)[0]]
        preserve = bool(targeted and targeted[0].get("preserve_marker_as"))
        if choice.get("resolution") == "keep_destination" and not targeted:
            consumed.add(path)
        elif not preserve and choice.get("resolution") != "preserve_marker":
            conflicts.append(_issue("delivery_state", path, message + " Approve preservation of custom content before transfer."))
    if inspection.transfer_needed:
        pending = [{"role": role, "path": path} for role, selected in (("source", source), ("destination", destination))
                   for path in selected.pending_resources]
        if pending:
            conflicts.append(_issue("pending_resources", ".", "Existing merge resources prevent transfer. Complete recovery and review again.",
                                    resources=pending))
    if source is not None and source.kind == "branch" and not source.files:
        conflicts.append(_issue("source", str(source.repository_root), "The branch contains no available specification set. Select a worktree or workspace."))
    if inspection.transfer_needed:
        unavailable = [value.path for selected in (source, destination) for value in selected.availability
                       if value.condition == "unavailable" and value.purpose == "artifacts"]
        if unavailable:
            conflicts.append(_issue("availability", ".", "A selected artifact or directory is unreadable. Restore access and inspect again.", paths=unavailable))
        _allocate_imports(inspection, active, conflicts)
    elif supplied_rows or artifact_rows:
        raise ValueError("Shared-set and reconciliation-only requests must not propose artifact transfers.")

    def destination_feature(path: str) -> str | None:
        possible = set(destination_features) | {row["destination_feature"] for row in active if row.get("destination_feature")}
        matches = [feature for feature in possible if path.startswith(feature + "/")]
        return max(matches, key=len) if matches else None

    def add_directory(path: str, fallback: DirectorySnapshot | None = None) -> None:
        if path == "." or path in destination.directories:
            return
        if path in operations:
            if fallback is not None and operations[path].kind == "create_directory":
                mode, mtime = _metadata({}, fallback)
                operations[path] = dataclasses.replace(operations[path], mode=mode, mtime_ns=mtime)
            return
        _relative(path, "destination directory")
        parent = path.rpartition("/")[0] or "."
        add_directory(parent)
        if path in destination.files:
            conflicts.append(_issue("scope", path, "A destination file occupies a required directory."))
            return
        mode, mtime = _metadata({}, fallback) if fallback else (0o777 if os.name == "nt" else 0o755, 0)
        operations[path] = MergeOperation("create_directory", path, None, mode, mtime, "create_directory")

    contributions = {}
    for row in active if inspection.transfer_needed else []:
        target = row.get("destination_feature")
        if target and row["relation"] != "already_delivered":
            for relative, value in _feature_files(source, row["source_feature"]).items():
                contributions.setdefault(f"{target}/{relative}", []).append(value)
    conflicting_contributions = set()
    resolved_contributions = set()
    for path, values in contributions.items():
        if len({value.content for value in values}) <= 1:
            continue
        choice = artifact_choices.get(path, {})
        resolved = choice.get("resolution") in {"keep_destination", "replace"} or (
            choice.get("resolution") == "copy_source" and choice.get("source_path") in {value.path for value in values}
        )
        if not resolved:
            conflicting_contributions.add(path)
            conflicts.append(_issue(
                "content", path, "Source contributions differ. Keep destination bytes, approve replacement bytes, or select one source path.",
                source_paths=[value.path for value in values],
            ))
        else:
            resolved_contributions.add(path)

    def candidate(path: str, source_path: str | None, fallback: FileSnapshot | None, classification: str) -> None:
        _relative(path, "destination artifact")
        if path in conflicting_contributions:
            return
        original = destination.files.get(path)
        choice = artifact_choices.get(path)
        if choice:
            consumed.add(path)
        if path in destination.directories:
            conflicts.append(_issue("scope", path, "A directory occupies this destination artifact path."))
            return
        if original is not None and original.content is None:
            conflicts.append(_issue("availability", path, "The destination artifact is unreadable. Restore access before transfer."))
            return
        resolution = choice.get("resolution") if choice else None
        selected_source = choice.get("source_path", source_path) if choice else source_path
        selected = source.files.get(selected_source) if source and selected_source else fallback
        for reason in choice.get("unresolved", []) if choice else []:
            conflicts.append(_issue("reference", path, reason))
        if resolution == "keep_destination":
            if original is None:
                raise ValueError(f"Cannot keep a missing destination artifact: {path}")
            comparison = _difference(original.content, selected.content, path) if selected is not None and selected.content is not None else ""
            artifacts.append(_issue("artifact", path, "Keep the destination artifact.", classification=classification,
                                    resolution=resolution, difference="", source_difference=comparison))
            return
        if resolution == "preserve_marker":
            return
        if resolution == "replace":
            content = _bytes(choice.get("content"), f"Replacement for {path}")
            recipe = ()
        else:
            if selected is None or selected.content is None:
                raise ValueError(f"A copy decision needs a readable selected-source artifact: {path}")
            if source is not None and selected_source in source.markers:
                raise ValueError(f"Delivery markers are not source-copy artifacts: {selected_source}")
            recipe = _freeze(choice.get("replacements", []) if choice else [])
            content = _replacements(selected.content, recipe, selected_source or path)
            if original is not None and original.content != content and resolution is None:
                conflicts.append(_issue("content", path, "These artifact bytes differ. Select source bytes, destination bytes, or exact authored replacement bytes."))
                artifacts.append(_issue("artifact", path, "Review differing artifact content.", classification="conflicting",
                                        difference=_difference(original.content, content, path)))
                return
            resolution = resolution or "copy_source"
        mode, mtime = _metadata(choice or {}, (original or selected) if resolution == "replace" else selected)
        difference = _difference(original.content if original else None, content, path)
        fixed_metadata_change = choice is not None and ("mode" in choice or "mtime_ns" in choice) and original is not None and (
            original.mode != mode or original.mtime_ns != mtime
        )
        if original is not None and original.content == content and not fixed_metadata_change:
            artifacts.append(_issue("artifact", path, "Retain identical destination bytes.", classification="identical",
                                    resolution="keep_destination", difference=""))
            return
        if destination.principles_path is not None and destination.spec_root / path == destination.principles_path:
            raise ValueError("Artifact decisions must not change destination project principles.")
        for context in destination.context_inputs:
            if str(destination.spec_root / path) == context.path:
                raise ValueError(f"Artifact decisions must not change project context records: {path}")
        add_directory(path.rpartition("/")[0] or ".")
        operations[path] = MergeOperation(
            "write", path, content, mode, mtime, resolution,
            selected_source if resolution == "copy_source" else None, recipe, difference,
        )
        artifacts.append(_issue("artifact", path, "Write the approved artifact bytes.", classification=classification,
                                resolution=resolution, difference=difference))

    transferred = set()
    for row in active if inspection.transfer_needed else []:
        feature, target = row["source_feature"], row.get("destination_feature")
        if not target:
            continue
        for other in rows:
            other_target = other.get("destination_feature")
            if other is not row and other_target and other.get("relation") != "skipped" and (target.startswith(other_target + "/") or other_target.startswith(target + "/")):
                conflicts.append(_issue("scope", target, "Imported and matched feature directories must not overlap."))
        if row["relation"] == "independent" and target in destination.directories:
            continue
        source_files = _feature_files(source, feature)
        if row["relation"] == "already_delivered":
            if target not in _mapped_candidates(source_files, destination, artifact_choices, target):
                conflicts.append(_issue("correspondence", feature, "The complete mapped source content does not match the selected delivery candidate."))
            else:
                consumed.update(f"{target}/{relative}" for relative in source_files if f"{target}/{relative}" in artifact_choices)
                for relative in source_files:
                    artifacts.append(_issue("artifact", f"{target}/{relative}", "Retain the complete mapped delivery.",
                                            classification="identical", resolution="keep_destination", difference=""))
                if row.get("rebind_origin") is not None or row.get("delivery_review") in {"confirmed", "additional"}:
                    transferred.add(target)
            continue
        start_paths = set(operations)
        for relative, value in source_files.items():
            path = f"{target}/{relative}"
            choice = artifact_choices.get(path)
            if choice and choice.get("source_path", value.path) not in {item.path for item in contributions[path]}:
                raise ValueError(f"A source-copy recipe must follow its reviewed feature correspondence: {path}")
            candidate(path, value.path, value, "renamed" if target != feature else "changed" if path in destination.files else "source_only")
        for relative, value in _feature_files(destination, target).items():
            if relative not in source_files:
                artifacts.append(_issue("artifact", f"{target}/{relative}", "Retain destination-only artifact bytes.",
                                        classification="destination_only", resolution="keep_destination", difference=""))
        for path, directory in source.directories.items():
            if path == feature or path.startswith(feature + "/"):
                relative = path[len(feature):]
                add_directory(target + relative, directory)
        if set(operations) != start_paths or row.get("rebind_origin") is not None or (
            row.get("delivery_review") in {"confirmed", "additional"}
            or any(f"{target}/{relative}" in resolved_contributions for relative in source_files)
        ):
            transferred.add(target)
        for field, selected_marker, label in (
            ("preserve_marker_as", destination.markers.get(f"{target}/.merge-specs.json"), "preserve_destination_marker"),
            ("preserve_source_marker_as", source.markers.get(f"{feature}/.merge-specs.json"), "preserve_source_marker"),
        ):
            if row.get(field):
                relative = _relative(row[field], "preserved marker")
                if relative == ".merge-specs.json" or selected_marker is None or selected_marker.content is None:
                    raise ValueError("A marker preservation decision needs readable custom bytes and a different destination filename.")
                preserved = f"{target}/{relative}"
                if preserved in destination.files or preserved in destination.directories or preserved in operations:
                    raise ValueError(f"The approved custom-marker preservation path is occupied: {preserved}")
                add_directory(preserved.rpartition("/")[0])
                mode, mtime = _metadata({}, selected_marker)
                operations[preserved] = MergeOperation("write", preserved, selected_marker.content, mode, mtime,
                                                      label, difference=_difference(None, selected_marker.content, preserved))
                artifacts.append(_issue("artifact", preserved, "Preserve reserved-name custom content.", classification="source_only",
                                        resolution=label, difference=operations[preserved].difference))
                transferred.add(target)

    if inspection.transfer_needed:
        covered_source_paths = {path for feature in source_features.values() for path in feature.artifacts} | set(source.markers)
        for path, value in source.files.items():
            if path not in covered_source_paths:
                candidate(path, path, value, "changed" if path in destination.files else "source_only")
        for path, choice in artifact_choices.items():
            if path in consumed:
                continue
            owner = destination_feature(path)
            if choice["resolution"] == "preserve_marker":
                selected_source = choice.get("source_path")
                source_marker = source.markers.get(selected_source) if selected_source else None
                marker = source_marker or destination.markers.get(path)
                preserved = _relative(choice.get("preserve_path"), "preserved marker")
                if marker is None or marker.content is None or preserved in destination.files or preserved in destination.directories or preserved in operations:
                    raise ValueError(f"Select readable marker bytes and an available preservation path: {path}")
                source_root_marker = selected_source == ".merge-specs.json" and source_marker is not None
                if Path(preserved).name == ".merge-specs.json" or (
                    not source_root_marker and (owner is None or not preserved.startswith(owner + "/"))
                ):
                    raise ValueError("Custom marker preservation must remain in its containing feature or approved source-root scope.")
                add_directory(preserved.rpartition("/")[0] or ".")
                resolution = "preserve_source_marker" if source_marker is not None else "preserve_destination_marker"
                mode, mtime = _metadata({}, marker)
                operations[preserved] = MergeOperation("write", preserved, marker.content, mode, mtime, resolution,
                                                      difference=_difference(None, marker.content, preserved))
                artifacts.append(_issue("artifact", preserved, "Preserve reserved-name custom content.", classification="source_only",
                                        resolution=resolution, difference=operations[preserved].difference))
                consumed.add(path)
                if source_root_marker:
                    continue
                if source_marker is not None and owner in {row.get("destination_feature") for row in active}:
                    transferred.add(owner)
                elif owner not in transferred:
                    conflicts.append(_issue("delivery_state", path, "Preserve this custom marker through an explicit feature correspondence before replacing its reserved name."))
                continue
            if owner not in dependents and owner not in {row.get("destination_feature") for row in active}:
                raise ValueError(f"An artifact decision has no transferred or dependent feature scope: {path}")
            candidate(path, choice.get("source_path"), None, "changed" if path in destination.files else "source_only")
            transferred.add(owner)
        for feature in sorted(transferred):
            feature_rows = [row for row in active if row.get("destination_feature") == feature]
            if not feature_rows:
                continue
            marker_path = f"{feature}/.merge-specs.json"
            previous = markers.get(marker_path, {"origins": []})
            origins = { _origin_key(item): item for item in previous["origins"] }
            for row in feature_rows:
                if row.get("rebind_origin") is not None:
                    origins.pop(_origin_key(row["rebind_origin"]), None)
                current = {"source": origin, "source_feature": row["source_feature"], "source_state_digest": _artifact_state(_feature_files(source, row["source_feature"]))}
                origins[_origin_key(current)] = current
            final_files = {relative: value.content for relative, value in _feature_files(destination, feature).items()}
            for path, operation in operations.items():
                if operation.kind == "write" and path.startswith(feature + "/") and path != marker_path:
                    final_files[path[len(feature) + 1:]] = operation.content
            if any(value is None for value in final_files.values()):
                conflicts.append(_issue("delivery_state", feature, "Unreadable destination artifacts prevent a complete current-state fingerprint."))
                continue
            marker = {"origins": [origins[key] for key in sorted(origins)], "destination_state_digest": _artifact_state(final_files)}
            content = (json.dumps(marker, sort_keys=True, indent=2) + "\n").encode("utf-8")
            original = destination.markers.get(marker_path)
            mode, mtime = _metadata({}, original)
            add_directory(feature)
            operations[marker_path] = MergeOperation("write", marker_path, content, mode, mtime, "delivery_state",
                                                   difference=_difference(original.content if original else None, content, marker_path))
            artifacts.append(_issue("artifact", marker_path, "Store approved current delivery state, not history.",
                                    classification="changed" if original else "source_only", resolution="delivery_state",
                                    difference=operations[marker_path].difference))
    represented = {artifact["path"] for artifact in artifacts}
    for path in destination.files:
        if path not in destination.markers and path not in represented:
            artifacts.append(_issue("artifact", path, "Retain destination-only artifact bytes.",
                                    classification="destination_only", resolution="keep_destination", difference=""))

    changed_features = {feature for feature in transferred if any(path.startswith(feature + "/") for path in operations)}
    if inspection.transfer_needed and (active or operations or any(row.get("relation") is None for row in rows)) and relationship["relationship"] != "same_project":
        if relationship["relationship"] != "unrelated":
            conflicts.append(_issue("relationship", ".", "Confirm that both selected sets belong to the same project before transfer."))
    if inspection.transfer_needed and operations and not active and inspection.relationship["relationship"] == "unconfirmed":
        if relationship["relationship"] == "same_project" and (not isinstance(evidence, str) or not evidence.strip()):
            conflicts.append(_issue("relationship", ".", "Confirm that both sets belong to the same project before transfer."))
    for path, operation in operations.items():
        parent, _, name = path.rpartition("/")
        parent = parent or "."
        existing = {entry for entry, _ in destination.directories[parent].entries} if parent in destination.directories else set()
        proposed = {Path(other).name for other in operations if (other.rpartition("/")[0] or ".") == parent and other != path}
        if any(other.casefold() == name.casefold() and other != name for other in existing | proposed):
            conflicts.append(_issue("name", path, "A case-folded sibling name collides with this approved path."))
        for ancestor in Path(path).parents:
            if ancestor.as_posix() != "." and ancestor.as_posix() in destination.files:
                conflicts.append(_issue("scope", path, "A destination file occupies an approved path's parent directory."))
    ordered_operations = tuple(sorted(operations.values(), key=lambda operation: (operation.kind != "create_directory", operation.path.count("/"), operation.path)))
    resources = _resource_paths(decisions.get("temporary_resources"), bool(ordered_operations))
    if resources and resources["recovery_directory"] in destination.pending_resources:
        conflicts.append(_issue("pending_resources", resources["recovery_directory"], "The approved recovery directory is occupied. Request a new preview."))
    replay_rows = [
        {key: value for key, value in row.items() if key != "delivery_candidates"}
        for row in rows if row.get("relation") is not None
    ]
    replay = {
        "selections": dict(inspection.selections), "snapshot_digest": inspection.snapshot_digest,
        "relationship": relationship, "correspondences": replay_rows,
        "artifact_decisions": [row for row in artifact_rows if row["path"] not in overridden],
        "dependent_features": list(dependents), "temporary_resources": resources,
    }
    proposal = _proposal_binding(inspection, relationship, rows, ordered_operations, artifacts, conflicts, resources, dependents, replay)
    if decisions.get("proposal_digest", proposal) != proposal:
        raise ValueError("The approved proposal changed. Review the original content, mappings, and resources again.")
    replay["proposal_digest"] = proposal
    return SpecMergePreview(
        inspection, _freeze(relationship), _freeze(rows), ordered_operations, _freeze(artifacts), _freeze(conflicts),
        _freeze(resources), _freeze(_notices(inspection, rows)), tuple(sorted(changed_features)), dependents,
        proposal, _freeze(replay),
    )


def _merge_root(preview: SpecMergePreview) -> Path:
    root = preview.inspection.destination.spec_root
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != preview.inspection.destination.filesystem_identity:
        raise ValueError("The approved destination root changed. Restore its location and review again.")
    return root


def _checked_file(path: Path, expected: FileSnapshot | MergeOperation | None, root: Path, descriptor: int | None = None) -> bool:
    _ordinary(path, root)
    if descriptor is None:
        current = _capture(path, path.relative_to(root).as_posix(), root)
        if expected is None:
            return current.availability == "missing"
        return current.availability == "readable" and (
            current.content, current.mode, current.mtime_ns
        ) == (expected.content, expected.mode, expected.mtime_ns)
    info = path.lstat()
    held = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
        return False
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        stream.seek(0)
        content = stream.read()
    after = path.lstat()
    return expected is not None and (
        info.st_mode, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino
    ) == (
        after.st_mode, after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino
    ) and (content, stat.S_IMODE(info.st_mode), info.st_mtime_ns) == (expected.content, expected.mode, expected.mtime_ns)


def _under_lock_inspection(preview: SpecMergePreview, resource_state: os.stat_result) -> None:
    _merge_root(preview)
    fresh = inspect_spec_sets(preview.inspection.selections)
    original = preview.inspection.destination
    current = fresh.destination
    own = set(preview.temporary_resources.values())
    root = current.directories["."]
    if (root.mode, root.size, root.mtime_ns) != (stat.S_IMODE(resource_state.st_mode), resource_state.st_size, resource_state.st_mtime_ns):
        raise ValueError("Destination root metadata changed after resource creation. Inspect and review again.")
    reviewed_root = original.directories["."]
    normalized_root = dataclasses.replace(
        root, size=reviewed_root.size, mtime_ns=reviewed_root.mtime_ns,
        entries=tuple((name, kind) for name, kind in root.entries if name not in own),
    )
    directories = {**current.directories, ".": normalized_root}
    normalized = dataclasses.replace(
        current, directories=MappingProxyType(directories),
        pending_resources=tuple(path for path in current.pending_resources if path not in own),
    )
    if normalized.baseline() != original.baseline() or (fresh.source.baseline() if fresh.source else None) != (
        preview.inspection.source.baseline() if preview.inspection.source else None
    ):
        raise ValueError("The reviewed inputs changed under the merge lock. Inspect and review again.")


def _check_recovery(recovery: Path, identity: tuple[int, int] | None) -> None:
    _ordinary(recovery, recovery.parent)
    info = recovery.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        raise ValueError("The recovery directory ownership changed. Preserve it and inspect the attempt's original recovery directory.")


def _journal_write(recovery: Path, journal: dict, identity: tuple[int, int] | None) -> None:
    # This file belongs only to the disclosed recovery directory, not to a feature tree.
    _check_recovery(recovery, identity)
    _ordinary(recovery / "journal.json", recovery.parent)
    (recovery / "journal.json").write_text(json.dumps(journal, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _payload_write(path: Path, content: bytes, mode: int, mtime_ns: int, root: Path,
                   recovery_identity: tuple[int, int] | None, *, retain: bool = False) -> int | None:
    _check_recovery(path.parent.parent, recovery_identity)
    _ordinary(path, root)
    descriptor = None
    try:
        with path.open("x+b") as stream:
            stream.write(content)
            if retain:
                stream.flush()
                descriptor = os.dup(stream.fileno())
        path.chmod(mode)
        os.utime(path, ns=(mtime_ns, mtime_ns))
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    return descriptor


def _directory_metadata(preview: SpecMergePreview, created: Mapping, *, restored: bool) -> list[dict]:
    root = preview.inspection.destination.spec_root
    original = preview.inspection.destination.directories
    affected = {"."}
    for operation in preview.operations:
        affected.update(parent.as_posix() for parent in Path(operation.path).parents)
        if operation.kind == "create_directory":
            affected.add(operation.path)
    new_directories = {operation.path: operation for operation in preview.operations if operation.kind == "create_directory"}
    remaining = []
    for relative in sorted(affected, key=lambda path: (-path.count("/"), path)):
        if relative == ".":
            continue  # Resource removal changes the root. Restore its time after cleanup.
        directory = original.get(relative) if restored or relative not in created else new_directories.get(relative)
        if directory is None:
            continue
        path = root / relative
        try:
            _merge_root(preview)
            _ordinary(path, root)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("An unexpected writer changed this directory.")
            if relative in created and (info.st_dev, info.st_ino) != created[relative]:
                raise ValueError("An unexpected writer replaced this directory.")
            if restored and relative in original:
                expected = original[relative].entries
                observed = []
                for child in path.iterdir():
                    kind = child.lstat().st_mode
                    if not (stat.S_ISDIR(kind) or stat.S_ISREG(kind)):
                        raise ValueError("An unexpected unsafe entry prevents directory metadata restoration.")
                    observed.append((child.name, "git_metadata" if child.name.casefold() == ".git" else "directory" if stat.S_ISDIR(kind) else "file"))
                observed = tuple(sorted(observed))
                if observed != expected:
                    raise ValueError("Unexpected directory membership prevents metadata restoration.")
            path.chmod(directory.mode)
            os.utime(path, ns=(directory.mtime_ns, directory.mtime_ns))
        except (OSError, ValueError) as exc:
            remaining.append({"operation": "restore-directory-metadata", "path": relative, "mode": directory.mode,
                              "mtime_ns": directory.mtime_ns, "message": str(exc)})
    return remaining


def _rollback_merge(preview: SpecMergePreview, recovery: Path, journal: dict, created: dict,
                    descriptors: Mapping[str, int], recovery_identity: tuple[int, int] | None) -> list[dict]:
    root, remaining = preview.inspection.destination.spec_root, []
    operations = {operation.path: operation for operation in preview.operations}
    for relative, identity in created.items():
        path = root / relative
        try:
            _merge_root(preview)
            _ordinary(path, root)
            info = path.lstat()
            if (info.st_dev, info.st_ino) != identity or not stat.S_ISDIR(info.st_mode):
                continue
            operation = operations[relative]
            if stat.S_IMODE(info.st_mode) in (operation.mode, operation.mode | 0o700):
                _merge_root(preview)
                path.chmod(operation.mode | 0o700)
        except (OSError, ValueError):
            pass
    for row in reversed(journal["operations"]):
        if row["state"] not in {"attempting", "completed"}:
            continue
        operation = operations[row["path"]]
        path = root / operation.path
        if operation.kind == "create_directory":
            if operation.path not in created:
                row["state"] = "not_created"
                continue
            action = "inspect-directory"
            try:
                _merge_root(preview)
                _ordinary(path, root)
                if not path.exists():
                    row["state"] = "restored"
                    continue
                if created[operation.path] is None:
                    raise ValueError("The created directory identity is unverified. Preserve it and inspect the recovery journal.")
                info = path.lstat()
                if (info.st_dev, info.st_ino) != created[operation.path] or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("An unexpected writer replaced the created directory. Preserve it and inspect the recovery journal.")
                action = "remove-created-directory"
                path.rmdir()
                row["state"] = "restored"
            except (OSError, ValueError) as exc:
                remaining.append({"operation": action, "path": operation.path, "message": str(exc)})
            continue
        original = preview.inspection.destination.files.get(operation.path)
        restore = {
            "operation": "restore-file" if original is not None else "remove-created-file",
            "path": operation.path, "expected_fingerprint": hashlib.sha256(operation.content).hexdigest(),
        }
        if original is not None:
            restore.update(original=f"originals/{row['index']:06d}.bin", mode=original.mode, mtime_ns=original.mtime_ns)
        try:
            _merge_root(preview)
            if _checked_file(path, original, root):
                row["state"] = "restored"
                continue
            if row.get("relaxed_original_mode") and original is not None:
                relaxed = dataclasses.replace(original, mode=0o666)
                if _checked_file(path, relaxed, root):
                    restore["expected_fingerprint"] = original.digest
                    path.chmod(original.mode)
                    if not _checked_file(path, original, root):
                        raise OSError("The restored artifact metadata differs from its saved original.")
                    row["state"] = "restored"
                    continue
            if not _checked_file(path, operation, root, descriptors.get(operation.path)):
                raise ValueError("An unexpected writer changed this artifact. Preserve current content and the saved original before recovery.")
            if original is not None:
                _check_recovery(recovery, recovery_identity)
            if os.name == "nt" and not operation.mode & 0o200:
                path.chmod(0o666)
            if original is None:
                os.unlink(path)
            else:
                staged = recovery / "staged" / f"{row['index']:06d}.bin"
                _check_recovery(recovery, recovery_identity)
                _ordinary(staged, root)
                if staged.exists():
                    if os.name == "nt":
                        staged.chmod(0o666)
                    staged.unlink()
                _check_recovery(recovery, recovery_identity)
                original_path = recovery / restore["original"]
                _ordinary(original_path, root)
                saved = original_path.read_bytes()
                if saved != original.content:
                    raise ValueError("The saved original changed. Preserve it and recover from the reviewed source.")
                descriptor = _payload_write(staged, saved, original.mode, original.mtime_ns, root,
                                            recovery_identity, retain=not original.mode & 0o400)
                try:
                    _check_recovery(recovery, recovery_identity)
                    os.replace(staged, path)
                    if not _checked_file(path, original, root, descriptor):
                        raise OSError("The restored artifact does not match its saved original.")
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
            row["state"] = "restored"
        except (OSError, ValueError) as exc:
            restore["message"] = str(exc)
            remaining.append(restore)
    remaining.extend(_directory_metadata(preview, created, restored=True))
    journal["remaining_operations"] = remaining
    with contextlib.suppress(OSError, ValueError):
        _journal_write(recovery, journal, recovery_identity)
    return remaining


def _discard_recovery(recovery: Path, known: set[str], identity: tuple[int, int] | None) -> None:
    _check_recovery(recovery, identity)
    current = {path.relative_to(recovery).as_posix() for path in recovery.rglob("*")}
    if current - known:
        raise ValueError("The recovery directory contains unexpected entries. Preserve them before cleanup.")
    for path in recovery.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("The recovery directory contains an unsafe entry. Preserve it before cleanup.")
    if os.name == "nt":
        for path in recovery.rglob("*"):
            _check_recovery(recovery, identity)
            path.chmod(stat.S_IMODE(path.lstat().st_mode) | 0o222)
    _check_recovery(recovery, identity)
    shutil.rmtree(recovery)


def apply_spec_merge(preview: SpecMergePreview) -> MergeResult:
    """Recheck the approved binding, lock exclusively, and restore handled failures."""
    if not isinstance(preview, SpecMergePreview):
        raise ValueError("Apply a validated in-process merge preview.")
    if preview.conflicts:
        return MergeResult("refused", "unchanged", conflicts=preview.conflicts)
    try:
        expected = _proposal_binding(
            preview.inspection, preview.relationship, preview.correspondences, preview.operations, preview.artifacts,
            preview.conflicts, preview.temporary_resources, preview.dependent_features, preview.replay_inputs,
        )
        if expected != preview.proposal_digest:
            raise ValueError("The preview operation binding changed. Review a new proposal.")
        fresh = inspect_spec_sets(preview.inspection.selections)
        if fresh.snapshot_digest != preview.snapshot_digest:
            raise ValueError("The approved snapshot is stale. Inspect the selected sets and review again.")
        reconstructed = prepare_spec_merge(fresh, preview.replay_inputs)
        if reconstructed.proposal_digest != preview.proposal_digest:
            raise ValueError("The approved proposal changed. Review the exact operations again.")
    except (OSError, ValueError) as exc:
        return MergeResult("refused", "unchanged", conflicts=(_freeze(_issue("stale", ".", str(exc))),))
    if not preview.operations:
        transfer = "not_requested" if preview.inspection.source is None else "not_needed"
        return MergeResult(transfer, "unchanged")
    root = preview.inspection.destination.spec_root
    lock, recovery = (root / preview.temporary_resources[name] for name in ("lock", "recovery_directory"))
    original_root = preview.inspection.destination.directories["."]
    lock_owned, recovery_owned = False, False
    lock_identity = None
    recovery_identity = None
    known = {"originals", "staged", "journal.json"}
    created, changed = {}, []
    descriptors = {}
    journal = {
        "snapshot_digest": preview.snapshot_digest, "proposal_digest": preview.proposal_digest,
        "operations": [], "remaining_operations": [],
    }
    result = None
    content_started = False
    try:
        _merge_root(preview)
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        lock_owned = True
        try:
            stream = os.fdopen(descriptor, "wb")
        except OSError:
            os.close(descriptor)
            raise
        with stream:
            lock_info = os.fstat(descriptor)
            lock_identity = (lock_info.st_dev, lock_info.st_ino)
            stream.write(b'{"operation":"merge-specs"}\n')
        info = lock.lstat()
        if (info.st_dev, info.st_ino) != lock_identity:
            raise ValueError("The lock ownership changed. Preserve the current lock and inspect it.")
        _merge_root(preview)
        recovery.mkdir(mode=0o700)
        recovery_owned = True
        info = recovery.lstat()
        recovery_identity = (info.st_dev, info.st_ino)
        resource_state = root.lstat()
        _under_lock_inspection(preview, resource_state)
        _check_recovery(recovery, recovery_identity)
        (recovery / "originals").mkdir(mode=0o700)
        _check_recovery(recovery, recovery_identity)
        (recovery / "staged").mkdir(mode=0o700)
        for index, operation in enumerate(preview.operations):
            _merge_root(preview)
            original = preview.inspection.destination.files.get(operation.path)
            row = {
                "index": index, "kind": operation.kind, "path": operation.path,
                "mode": operation.mode, "mtime_ns": operation.mtime_ns, "state": "staged",
            }
            journal["operations"].append(row)
            if operation.kind == "write":
                row["fingerprint"] = hashlib.sha256(operation.content).hexdigest()
                row["staged"] = f"staged/{index:06d}.bin"
                known.add(row["staged"])
                descriptor = _payload_write(recovery / row["staged"], operation.content, operation.mode, operation.mtime_ns, root,
                                            recovery_identity, retain=not operation.mode & 0o400)
                if descriptor is not None:
                    descriptors[operation.path] = descriptor
                if original is not None:
                    row.update(original=f"originals/{index:06d}.bin", original_fingerprint=original.digest,
                               original_mode=original.mode, original_mtime_ns=original.mtime_ns)
                    known.add(row["original"])
                    _payload_write(recovery / row["original"], original.content, 0o600, original.mtime_ns, root, recovery_identity)
        _journal_write(recovery, journal, recovery_identity)
        for row, operation in zip(journal["operations"], preview.operations):
            _merge_root(preview)
            path = root / operation.path
            _ordinary(path, root)
            original = preview.inspection.destination.files.get(operation.path)
            if operation.kind == "write" and not _checked_file(path, original, root):
                raise ValueError(f"An unexpected writer changed an approved target: {operation.path}")
            row["state"] = "attempting"
            _journal_write(recovery, journal, recovery_identity)
            if operation.kind == "create_directory":
                path.mkdir(mode=operation.mode | 0o700)
                created[operation.path] = None
                info = path.lstat()
                created[operation.path] = (info.st_dev, info.st_ino)
            else:
                if os.name == "nt" and original is not None and not original.mode & 0o200:
                    row["relaxed_original_mode"] = True
                    _journal_write(recovery, journal, recovery_identity)
                    path.chmod(0o666)
                    if not _checked_file(path, dataclasses.replace(original, mode=0o666), root):
                        raise ValueError("An unexpected writer changed the artifact after its temporary mode change.")
                _check_recovery(recovery, recovery_identity)
                os.replace(recovery / row["staged"], path)
                changed.append(operation.path)
            content_started = True
            row["state"] = "completed"
            _journal_write(recovery, journal, recovery_identity)
        metadata_errors = _directory_metadata(preview, created, restored=False)
        if metadata_errors:
            raise OSError(metadata_errors[0]["message"])
        for operation in preview.operations:
            if operation.kind == "write" and not _checked_file(root / operation.path, operation, root, descriptors.get(operation.path)):
                raise OSError(f"The written artifact differs from the approved operation: {operation.path}")
        result = MergeResult("completed", "applied", tuple(changed))
    except (OSError, ValueError) as exc:
        if content_started or any(row["state"] in {"attempting", "completed"} for row in journal["operations"]):
            for row in journal["operations"]:
                if row["state"] == "attempting" and row["kind"] == "write" and row["path"] not in changed:
                    operation = next(operation for operation in preview.operations if operation.path == row["path"])
                    with contextlib.suppress(OSError, ValueError):
                        if not (recovery / row["staged"]).exists() and _checked_file(root / row["path"], operation, root, descriptors.get(row["path"])):
                            changed.append(row["path"])
            remaining = _rollback_merge(preview, recovery, journal, created, descriptors, recovery_identity)
            originals = tuple(
                _freeze({"path": action["path"], "original": str(recovery / action["original"])})
                for action in remaining if action.get("original")
            )
            result = MergeResult(
                "failed", "recovery_required" if remaining else "completely_restored", tuple(changed),
                recovery_directory=str(recovery) if remaining else None,
                remaining_operations=_freeze(remaining), originals=originals,
                conflicts=(_freeze(_issue("application", ".", str(exc))),),
            )
        else:
            result = MergeResult("refused", "unchanged", conflicts=(_freeze(_issue("application", ".", str(exc))),))
    finally:
        result_known = result is not None
        for descriptor in descriptors.values():
            os.close(descriptor)
        if result is None:
            result = MergeResult("failed", "recovery_required" if content_started else "unchanged",
                                 tuple(changed), recovery_directory=str(recovery) if recovery_owned else None)
        remaining = list(_json(result.remaining_operations))
        cleanup = []
        if recovery_owned and result_known and result.data_outcome != "recovery_required":
            try:
                _merge_root(preview)
                _discard_recovery(recovery, known, recovery_identity)
            except (OSError, ValueError) as exc:
                action = "inspect-recovery-directory"
                try:
                    _check_recovery(recovery, recovery_identity)
                except (OSError, ValueError):
                    pass
                else:
                    action = "remove-recovery-directory"
                cleanup.append({"operation": action, "path": str(recovery), "message": str(exc)})
        elif recovery_owned and result_known and result.data_outcome == "recovery_required":
            try:
                _check_recovery(recovery, recovery_identity)
                needed = {action.get("original") for action in remaining if action.get("original")}
                for row in journal["operations"]:
                    for field in ("original", "staged"):
                        relative = row.get(field)
                        if relative and relative not in needed:
                            with contextlib.suppress(OSError, ValueError):
                                _merge_root(preview)
                                _check_recovery(recovery, recovery_identity)
                                _ordinary(recovery / relative, root)
                                (recovery / relative).unlink()
                _check_recovery(recovery, recovery_identity)
            except (OSError, ValueError) as exc:
                for action in remaining:
                    action.pop("original", None)
                result = dataclasses.replace(result, cleanup_required=True, originals=(), remaining_operations=_freeze(remaining))
                if not any(action["operation"] == "inspect-recovery-directory" for action in remaining):
                    cleanup.append({"operation": "inspect-recovery-directory", "path": str(recovery), "message": str(exc)})
        if lock_owned:
            action = "inspect-lock"
            try:
                _merge_root(preview)
                if lock_identity is None:
                    raise ValueError("The lock identity is unverified. Preserve the lock and inspect it before cleanup.")
                info = lock.lstat()
                if (info.st_dev, info.st_ino) != lock_identity or not b'{"operation":"merge-specs"}\n'.startswith(lock.read_bytes()):
                    raise ValueError("An unexpected writer changed the lock. Preserve it before cleanup.")
                action = "remove-lock"
                os.unlink(lock)
            except (OSError, ValueError) as exc:
                cleanup.append({"operation": action, "path": str(lock), "message": str(exc)})
        if lock_owned:
            try:
                _merge_root(preview)
                root.chmod(original_root.mode)
                os.utime(root, ns=(original_root.mtime_ns, original_root.mtime_ns))
            except (OSError, ValueError) as exc:
                cleanup.append({"operation": "restore-directory-metadata", "path": ".", "mode": original_root.mode,
                                "mtime_ns": original_root.mtime_ns, "message": str(exc)})
        if cleanup:
            remaining.extend(cleanup)
            result = dataclasses.replace(
                result, cleanup_required=True, remaining_operations=_freeze(remaining),
                recovery_directory=(
                    None if any(item["operation"] == "inspect-recovery-directory" for item in remaining)
                    else str(recovery) if recovery.exists() and recovery_owned else result.recovery_directory
                ),
                conflicts=(*result.conflicts, *(_freeze(_issue("cleanup", item["path"], item["message"])) for item in cleanup)),
            )
        if recovery_owned and result_known and recovery.exists():
            journal["remaining_operations"] = remaining
            with contextlib.suppress(OSError, ValueError):
                _journal_write(recovery, journal, recovery_identity)
    return result
