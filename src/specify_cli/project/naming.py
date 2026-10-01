"""Migrate feature directory names between the sequential and timestamp schemes.

prepare_naming_migration() reads the project and writes nothing.
apply_naming_migration() checks a preview again under an exclusive workspace lock,
renames the features, edits the owned references, and saves the preference last.
A handled failure restores every completed step. Backups stay outside the project.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import posixpath
import re
import runpy
import shutil
import stat
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote

from specify_cli.workspace import project_record_path, read_json, workspace_root_for

_SCHEMES = ("sequential", "timestamp")
_MAX_NUMBER = 2**63 - 1  # the creation helper's signed 64-bit limit
_MAX_NAME = 255  # bytes in one directory name
_TIMESTAMP = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})([0-9]{2})-")
_SEQUENTIAL = re.compile(r"([0-9]{3,})-")
_RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_PUNCTUATION = r"[!-/:-@\[-`{-~]"  # ASCII punctuation: a backslash before it makes it literal in Markdown
_ESCAPE = re.compile(rf"\\({_PUNCTUATION})")
_ANGLE = re.compile(r"<(?:[^<>\n\\]|\\.)*>")
_INLINE = re.compile(r"\]\([ \t]*")  # the start of an inline link destination
_DEFINITION = re.compile(r"^ {0,3}\[[^\]\n]+\]:[ \t]*", re.MULTILINE)  # the start of a definition destination
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:|//")
_SEPARATOR = re.compile(rf"(\\[/\\]|/|\\(?!{_PUNCTUATION}))")
_SPACE = re.compile(r"\s*")
_EXTERNAL = (
    "Scripts, notes, and conversations outside this project can still use the old feature paths. "
    "Update them with the reported mappings."
)
_WORKTREE = (
    "Other worktrees that share this workspace keep their own native context files. "
    "Update each one with the reported mappings."
)
_OPAQUE = (
    "This workflow value can still refer to an old feature path. "
    "The command does not change it. Review it before you resume the run."
)


@dataclasses.dataclass(frozen=True)
class ReferenceEdit:
    """One edit to a file that owns a reference. original and replacement hold the whole file."""

    owner: str
    path: Path
    new_path: Path
    location: str
    original: bytes = dataclasses.field(repr=False)
    replacement: bytes = dataclasses.field(repr=False)
    mode: int
    mtime_ns: int | None


@dataclasses.dataclass(frozen=True)
class MigrationPreview:
    repository_root: Path
    workspace_root: Path
    project_id: str | None
    current_scheme: str
    target_scheme: str
    clock: datetime
    mappings: tuple[tuple[str, str], ...]
    reference_edits: tuple[ReferenceEdit, ...]
    preference_edit: ReferenceEdit | None
    skipped: tuple[Mapping[str, str], ...]
    conflicts: tuple[Mapping[str, str], ...]
    caller_notices: tuple[Mapping[str, str | None], ...]
    snapshot_digest: str
    mapping_digest: str

    def __post_init__(self) -> None:
        for name in ("skipped", "conflicts", "caller_notices"):
            object.__setattr__(self, name, _frozen(getattr(self, name)))


@dataclasses.dataclass(frozen=True)
class MigrationResult:
    preview: MigrationPreview
    status: str
    recovery: Mapping[str, Any] | None = None
    conflicts: tuple[Mapping[str, str], ...] = ()
    transaction_status: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "conflicts", _frozen(self.conflicts))
        if self.recovery is not None:
            operations = _frozen(self.recovery["remaining_operations"])
            object.__setattr__(self, "recovery", MappingProxyType({**self.recovery, "remaining_operations": operations}))


# ------------------------------------------------------------------------------ small helpers


def _conflict(code: str, path: str, message: str) -> dict:
    return {"path": path, "code": code, "message": message}


def _notice(kind: str, message: str, path: str | None = None, location: str | None = None) -> dict:
    return {"kind": kind, "path": path, "location": location, "message": message}


def _frozen(rows) -> tuple[Mapping[str, Any], ...]:
    """Copy rows into read-only mappings, so a caller cannot change a record through them."""
    return tuple(MappingProxyType(dict(row)) for row in rows)


def _display(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def _clock(value: datetime | None) -> datetime:
    value = value or datetime.now()
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def _stamp(moment: datetime) -> str:
    return (
        f"{moment.year:04d}{moment.month:02d}{moment.day:02d}-"
        f"{moment.hour:02d}{moment.minute:02d}{moment.second:02d}"
    )


def _next_second(moment: datetime) -> datetime | None:
    try:
        return moment + timedelta(seconds=1)
    except OverflowError:
        return None


def _load_object(data: bytes, path: Path) -> dict:
    try:
        value = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"Cannot read metadata at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Metadata must be an object: {path}")
    return value


def _replace_member(text: str, key: str, value: str) -> str:
    """Replace the value of one top-level string member of a JSON object. Every other byte stays."""
    decoder = json.JSONDecoder()
    at, span = _SPACE.match(text).end() + 1, None  # the first character after the opening brace
    while True:
        at = _SPACE.match(text, at).end()
        if text[at] == "}":
            break
        name, at = decoder.raw_decode(text, at)
        at = _SPACE.match(text, _SPACE.match(text, at).end() + 1).end()  # skip the colon
        _, end = decoder.raw_decode(text, at)
        if name == key:
            span = (at, end)
        at = _SPACE.match(text, end).end()
        if text[at] == ",":
            at += 1
    start, end = span
    return text[:start] + json.dumps(value, ensure_ascii=text[start:end].isascii()) + text[end:]


def _reader(refs: list, roots: tuple[Path, ...]):
    """Return read(path) -> (bytes or None, mode, mtime_ns). Each read joins the fingerprint, absence too.

    No directory between a root and the file may be a symlink. The roots are already canonical,
    so a link above them, such as /var on macOS, does not matter.
    """

    def read(path: Path):
        try:
            for root in roots:
                if path.is_relative_to(root):
                    here = root
                    for part in path.relative_to(root).parts[:-1]:
                        here /= part
                        if stat.S_ISLNK(os.lstat(here).st_mode):
                            refs.append([str(path), "linked"])
                            raise ValueError(f"Metadata must not sit behind a symlink: {here}")
                    break
            info = os.lstat(path)
        except (FileNotFoundError, NotADirectoryError):
            refs.append([str(path), None])
            return None, 0o644, None
        if not stat.S_ISREG(info.st_mode):
            refs.append([str(path), "irregular"])
            raise ValueError(f"Metadata must be a regular file: {path}")
        data, mode = path.read_bytes(), stat.S_IMODE(info.st_mode)
        refs.append([str(path), mode, info.st_mtime_ns, hashlib.sha256(data).hexdigest()])
        return data, mode, info.st_mtime_ns

    return read


def _moved(value: str, root: Path, renames: dict) -> str | None:
    """Spell a stored path for the renamed feature that holds it. None when no renamed feature holds it."""
    path = Path(value)
    if path.is_absolute():
        try:
            rel = path.relative_to(root)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root)
            except (ValueError, OSError, RuntimeError):
                return None
    else:
        rel = Path(posixpath.normpath(value))
        if rel.parts[:1] == ("..",):
            return None
    parts = rel.parts
    for end in range(1, len(parts) + 1):
        if parts[:end] in renames:
            moved = (*parts[: end - 1], renames[parts[:end]], *parts[end:])
            return str(root.joinpath(*moved)) if path.is_absolute() else "/".join(moved)
    return None


def _after(path: Path, root: Path, renames: dict) -> Path:
    """Return where a file sits after the feature renames."""
    moved = _moved(str(path), root, renames)
    return Path(moved) if moved else path


# -------------------------------------------------------------------------------- discovery


@dataclasses.dataclass
class _Survey:
    entries: list = dataclasses.field(default_factory=list)  # fingerprint records for specs/
    dirs: list = dataclasses.field(default_factory=list)  # (path, name) of directories outside features
    features: list = dataclasses.field(default_factory=list)
    names: dict = dataclasses.field(default_factory=dict)  # scope -> casefolded names of its entries
    markdown: dict = dataclasses.field(default_factory=dict)  # path -> (bytes, mode, mtime_ns) in features
    conflicts: list = dataclasses.field(default_factory=list)


def _survey(workspace: Path) -> _Survey:
    """Walk specs/ with lstat. Symlinks and special files become conflicts. Nothing is followed."""
    found = _Survey()
    root = workspace / "specs"

    def refuse(code: str, rel: str, message: str) -> None:
        found.conflicts.append(_conflict(code, rel, message))

    def visit(path: Path, rel: str, feature: str | None) -> None:
        children = sorted(os.scandir(path), key=lambda entry: entry.name)
        if rel != "specs" and any(c.name == "spec.md" and c.is_file(follow_symlinks=False) for c in children):
            if feature is None:
                feature = rel
                found.features.append(rel)
            else:
                refuse("nested-feature", rel, "A feature directory cannot contain another feature.")
        names = found.names.setdefault(rel, set()) if feature is None else None
        for child in children:
            child_rel, info = f"{rel}/{child.name}", child.stat(follow_symlinks=False)
            kind, mode = stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)
            if names is not None:
                names.add(child.name.casefold())
            if kind == stat.S_IFDIR:
                found.entries.append([child_rel, "d", mode])
                if feature is None:
                    found.dirs.append((child_rel, child.name))
                visit(Path(child.path), child_rel, feature)
            elif kind == stat.S_IFREG:
                if feature is not None and child.name.lower().endswith(".md"):
                    data = Path(child.path).read_bytes()
                    digest = hashlib.sha256(data).hexdigest()
                    found.markdown[child_rel] = (data, mode, info.st_mtime_ns)
                else:
                    with open(child.path, "rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                found.entries.append([child_rel, "f", mode, info.st_size, info.st_mtime_ns, digest])
            elif kind == stat.S_IFLNK:
                found.entries.append([child_rel, "l"])
                refuse("unsafe-symlink", child_rel, "A symlink under specs cannot move safely. Replace it with a copy.")
            else:
                found.entries.append([child_rel, "o"])
                refuse("unsupported-file-type", child_rel, "Only ordinary files and directories can move.")

    try:
        info = root.lstat()
    except FileNotFoundError:
        return found
    if stat.S_ISLNK(info.st_mode):
        found.entries.append(["specs", "l"])
        refuse("unsafe-symlink", "specs", "The specs directory is a symlink. Replace it with a directory.")
    elif not stat.S_ISDIR(info.st_mode):
        found.entries.append(["specs", "o"])
        refuse("unsupported-file-type", "specs", "The specs path is not a directory.")
    else:
        found.entries.append(["specs", "d", stat.S_IMODE(info.st_mode)])
        visit(root, "specs", None)
    return found


def _classify(name: str) -> tuple[str, object, str]:
    """Read a directory name as (scheme, order key, suffix). Raise ValueError for an unusable prefix."""
    match = _TIMESTAMP.match(name)
    if match:
        try:
            return "timestamp", datetime(*map(int, match.groups())), name[match.end():]
        except ValueError:
            raise ValueError("The timestamp prefix is not a valid date and time.") from None
    match = _SEQUENTIAL.match(name)
    if match:
        digits = match.group(1).lstrip("0") or "0"
        if len(digits) > 19 or int(digits) > _MAX_NUMBER:
            raise ValueError("The sequential number is larger than 2**63 - 1.")
        return "sequential", int(digits), name[match.end():]
    return "custom", None, name


def _allocate(found: _Survey, target: str, clock: datetime, conflicts: list) -> tuple[list, list]:
    """Assign every source feature a new name in the target scheme. Return (mappings, skipped)."""
    features = set(found.features)
    numbers, moments, sources, skipped = set(), set(), [], []
    for rel, name in found.dirs:
        try:
            scheme, key, suffix = _classify(name)
        except ValueError as exc:
            if rel in features:  # another directory cannot hold a prefix that no allocation can reach
                conflicts.append(_conflict("invalid-prefix", rel, str(exc)))
            continue
        if scheme == "sequential":
            numbers.add(key)
        elif scheme == "timestamp":
            moments.add(key)
        if rel not in features:
            continue  # a directory without a specification only reserves its prefix
        if scheme in (target, "custom"):
            reason = "Custom name without a numbering prefix." if scheme == "custom" else "Already uses the target scheme."
            skipped.append({"path": rel, "reason": reason})
        else:
            sources.append((key, rel, suffix))
    sources.sort()
    mappings, number, moment = [], max(numbers, default=0), clock
    for _, rel, suffix in sources:
        if target == "sequential":
            number += 1
            name = f"{number:03d}-{suffix}" if number <= _MAX_NUMBER else None
        else:
            while moment is not None and moment in moments:
                moment = _next_second(moment)
            name = None if moment is None else f"{_stamp(moment)}-{suffix}"
            if moment is not None:
                moments.add(moment)
        scope = rel.rsplit("/", 1)[0]
        if name is None:
            conflicts.append(_conflict("overflow", rel, "No unused prefix remains in the number or calendar range."))
        elif len(os.fsencode(name)) > _MAX_NAME:
            conflicts.append(_conflict("name-too-long", rel, "The new name is longer than the file system allows."))
        elif name.casefold() in found.names[scope]:
            conflicts.append(_conflict("occupied-target", rel, "Another entry already has the new name."))
        else:
            found.names[scope].add(name.casefold())
            mappings.append((rel, f"{scope}/{name}"))
    return mappings, skipped


# ------------------------------------------------------------------------ owned references


def _splice(owner: str, path: Path, new_path: Path, data: bytes, text: str, spans: list, mode: int, mtime: int):
    """Turn the replacement spans of one decoded file into edits. Each edit carries the whole new file."""
    spans.sort()
    out, last = [], 0
    for start, end, new, _ in spans:
        out += (text[last:start], new)
        last = end
    replacement = ("".join(out) + text[last:]).encode()
    return [
        ReferenceEdit(owner, path, new_path, f"line {text.count(chr(10), 0, start) + 1}: {old} -> {new}",
                      data, replacement, mode, mtime)
        for start, _, new, old in spans
    ]


def _token_pattern(workspace: Path, renames: dict) -> re.Pattern:
    """Match a complete old feature path: workspace-relative, or under the absolute workspace root."""
    olds = sorted(("/".join(parts) for parts in renames), key=len, reverse=True)
    root, names = re.escape(workspace.as_posix()), "|".join(map(re.escape, olds))
    return re.compile(rf"(?<![\w.~%/-])((?:{root}/)?)({names})(?![\w-]|\.\w)")


def _decoded(text: str) -> str:
    """Read a destination the way Markdown does: backslash escapes first, then percent encoding."""
    return unquote(_ESCAPE.sub(r"\1", text))


def _spelled(raw: str, old: str, new: str) -> str:
    """Write the new directory name the way the old one was written. Only the numbering prefix changes."""
    prefix = (_TIMESTAMP.match(old) or _SEQUENTIAL.match(old)).end()
    head = len(new) - len(old) + prefix  # the suffix is the same, so only the numbering prefix differs
    for end in range(prefix, len(raw) + 1):
        if _decoded(raw[:end]) == old[:prefix]:
            return new[:head] + raw[end:]
    return new


def _destination(text: str, start: int) -> int | None:
    """Return where the Markdown link destination at start ends. None when no destination starts there."""
    if text.startswith("<", start):
        angle = _ANGLE.match(text, start)
        return angle.end() if angle else None
    at, depth = start, 0
    while at < len(text) and text[at] > " " and text[at] != "\x7f":
        escape = _ESCAPE.match(text, at)
        if escape:
            at = escape.end()
            continue
        if text[at] == "(":
            depth += 1
        elif text[at] == ")":
            if not depth:
                break
            depth -= 1
        at += 1
    return at if at > start and not depth else None


def _rebase(raw: str, doc_dir: tuple, renames: dict, workspace: Path) -> tuple[str | None, bool]:
    """Rewrite one Markdown destination. Return (new text or None, reads two ways).

    Walk the segments from the document's old directory. Each segment that completes a renamed
    feature path gets the new name. Everything else, including fragments, stays as written.
    """
    angle = raw.startswith("<")
    body = raw[1:-1] if angle else raw
    path = re.match(r"[^#?]*", body).group()
    if not path or path[0] in "/\\" or _SCHEME.match(path):
        return None, False
    parts = _SEPARATOR.split(path)

    def walk(stack: list) -> dict:
        hits = {}
        for index in range(0, len(parts), 2):
            name = _decoded(parts[index])
            if name in ("", "."):
                continue
            if name == "..":
                if not stack:
                    return {}
                stack.pop()
                continue
            stack.append(name)
            if tuple(stack) in renames:
                hits[index] = renames[tuple(stack)]
        return hits

    relative = walk(list(doc_dir))
    absolute = walk([]) if _decoded(parts[0]) == "specs" else {}
    if absolute:
        shadow = posixpath.normpath(posixpath.join(*doc_dir, _decoded(path)))
        if relative or (not shadow.startswith("..") and os.path.lexists(workspace / shadow)):
            return None, True
    hits = absolute or relative
    if not hits:
        return None, False
    for index, name in hits.items():
        parts[index] = _spelled(parts[index], _decoded(parts[index]), name)
    new = "".join(parts) + body[len(path):]
    return (f"<{new}>" if angle else new), False


def _plan_markdown(workspace: Path, found: _Survey, renames: dict, new_of: dict, token, conflicts: list) -> list:
    edits = []
    for rel, (data, mode, mtime) in sorted(found.markdown.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        doc_dir, spans, taken = tuple(rel.split("/")[:-1]), [], []
        for match in (*_INLINE.finditer(text), *_DEFINITION.finditer(text)):
            start = match.end()
            end = _destination(text, start)
            if end is None or any(a < end and start < b for a, b in taken):
                continue
            raw = text[start:end]
            new, ambiguous = _rebase(raw, doc_dir, renames, workspace)
            if ambiguous:
                conflicts.append(_conflict(
                    "ambiguous-reference", rel, "A link reads as a document path and as a workspace path. Make it explicit."
                ))
            elif new is not None:
                spans.append((start, end, new, raw))
            if not raw.lstrip("<").startswith(("/", "\\")):
                taken.append((start, end))  # a rooted destination can still hold a complete absolute path token
        for match in token.finditer(text):
            if not any(a < match.end() and match.start() < b for a, b in taken):
                spans.append((match.start(), match.end(), match.group(1) + new_of[match.group(2)], match.group(0)))
        if spans:
            path = workspace / rel
            edits += _splice("feature-artifact", path, _after(path, workspace, renames), data, text, spans, mode, mtime)
    return edits


def _plan_selection(workspace: Path, project_id: str | None, renames: dict, read, conflicts: list) -> list:
    """Follow an existing saved pointer into a renamed feature. Never create or select one."""
    if project_id:
        key, path = "active_feature", project_record_path(project_id)
    else:
        key, path = "feature_directory", workspace / ".specify/feature.json"
    try:
        data, mode, mtime = read(path)
        record = None if data is None else _load_object(data, path)
    except ValueError:
        conflicts.append(_conflict(
            "invalid-selection", _display(path, workspace), "The saved feature selection cannot be read. Fix or remove it."
        ))
        return []
    value = record.get(key) if record else None
    usable = isinstance(value, str) and value and not (project_id and Path(value).is_absolute())
    moved = _moved(value, workspace, renames) if usable else None
    if moved is None:
        return []
    replacement = _replace_member(data.decode("utf-8"), key, moved).encode()
    return [ReferenceEdit("selection", path, path, f"/{key}", data, replacement, mode, mtime)]


def _strings(value, pointer: str = ""):
    """Yield (JSON pointer, text) for every string in a JSON value."""
    if isinstance(value, str):
        yield pointer, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{pointer}/{index}")


def _plan_workflows(workspace: Path, token, renames: dict, read, conflicts: list, opaque: list) -> list:
    """Move workflow_dir of paused and failed runs. Report every other mention without a repair."""
    workflows = workspace / ".specify/workflows"
    runs = workflows / "runs"
    for directory in (workflows, runs):
        if directory.is_symlink():
            conflicts.append(_conflict(
                "unsafe-symlink", _display(directory, workspace), "A workflow directory is a symlink. Replace it with a directory."
            ))
            return []
    if not runs.is_dir():
        return []
    from specify_cli.workflows.engine import RunState  # loaded on use: the engine is large

    edits = []
    for run in sorted(runs.iterdir()):
        if run.is_symlink():
            conflicts.append(_conflict(
                "unsafe-symlink", _display(run, workspace), "A workflow run is a symlink. Replace it with a directory."
            ))
            continue
        if not _RUN_ID.fullmatch(run.name) or not run.is_dir():
            continue
        state_path, inputs_path = run / "state.json", run / "inputs.json"
        try:
            data, mode, mtime = read(state_path)
        except ValueError:
            conflicts.append(_conflict(
                "invalid-workflow-state", _display(state_path, workspace),
                "A workflow record is not an ordinary file. The command does not follow symlinks.",
            ))
            continue
        try:
            inputs = read(inputs_path)[0]
        except ValueError:
            conflicts.append(_conflict(
                "invalid-workflow-state", _display(inputs_path, workspace),
                "Workflow inputs must be an ordinary file. The command does not follow symlinks.",
            ))
            continue
        for filename in ("workflow.yml", "log.jsonl"):
            path = run / filename
            try:
                raw = read(path)[0]
            except ValueError:
                conflicts.append(_conflict(
                    "invalid-workflow-state", _display(path, workspace),
                    "A workflow caller file must be ordinary. The command does not follow symlinks.",
                ))
                continue
            if raw is None:
                continue
            locations = set()
            for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                mentioned = token.search(line)
                if not mentioned and filename == "log.jsonl":
                    try:
                        mentioned = any(token.search(value) for _, value in _strings(json.loads(line)))
                    except ValueError:
                        pass
                if mentioned:
                    locations.add(number)
            if filename == "workflow.yml":
                import yaml

                try:
                    for scalar in yaml.scan(raw):
                        if isinstance(scalar, yaml.tokens.ScalarToken) and token.search(scalar.value):
                            locations.add(scalar.start_mark.line + 1)
                except yaml.YAMLError:
                    pass  # Opaque malformed text still has the line-based notices above.
            opaque.extend(
                _notice("opaque-workflow-value", _OPAQUE, str(path), f"line {number}")
                for number in sorted(locations)
            )
        if data is None:
            continue
        shown = _display(state_path, workspace)
        try:
            state, document = RunState.load(run.name, workspace), json.loads(data)
        except Exception:  # ponytail: the loader raises many types. Any failure means a malformed record.
            if token.search(data.decode("utf-8", "replace")):
                conflicts.append(_conflict(
                    "invalid-workflow-state", shown, "A workflow record that mentions a renamed feature cannot be read."
                ))
            continue
        inside = _moved(state.workflow_dir, workspace, renames) if state.workflow_dir else None
        repaired = ""  # the one field of a run that this command owns
        if inside is not None and state.status.value == "running":
            conflicts.append(_conflict(
                "running-workflow", shown, "A running workflow uses a renamed feature. Stop it before the migration."
            ))
        elif inside is not None and state.status.value in ("paused", "failed"):
            value = _moved(document["workflow_dir"], workspace, renames)
            if value is None:
                conflicts.append(_conflict(
                    "invalid-workflow-state", shown, "The workflow directory reaches a renamed feature by an indirect path."
                ))
            else:
                replacement = _replace_member(data.decode("utf-8"), "workflow_dir", value).encode()
                edits.append(ReferenceEdit(
                    "workflow-resource", state_path, state_path, "/workflow_dir", data, replacement, mode, mtime
                ))
                repaired = "/workflow_dir"
        for path, raw, skip in ((state_path, data, repaired), (inputs_path, inputs, "")):
            try:
                value = None if raw is None else json.loads(raw)
            except ValueError:
                continue
            for pointer, text in _strings(value):
                if pointer != skip and token.search(text):
                    opaque.append(_notice("opaque-workflow-value", _OPAQUE, str(path), pointer))
    return edits


def _plan_owned_sections(
    workspace: Path, repo: Path, token, renames: dict, new_of: dict, options: dict, read, conflicts: list
) -> list:
    """Apply generic reference replacements within regions supplied by their extension owner."""
    from specify_cli import locate_bundled_extension

    bundled = locate_bundled_extension("agent-context")
    if bundled is None:
        raise ValueError("The bundled reference owner is unavailable. Reinstall the CLI.")
    provider = runpy.run_path(str(bundled / "scripts/python/update_agent_context.py"))
    sections, issues = provider["discover_managed_sections"](
        workspace, repo, options.get("integration") or options.get("ai"),
        read_file=read, mentions_reference=lambda text: bool(token.search(text)),
    )
    conflicts.extend(issues)
    edits = []
    for path, original, mode, mtime, first, last in sections:
        text = original.decode("utf-8")
        spans = [
            (m.start(), m.end(), m.group(1) + new_of[m.group(2)], m.group(0))
            for m in token.finditer(text, first, last)
        ]
        if spans:
            edits += _splice("agent-context", path, _after(path, workspace, renames), original, text, spans, mode, mtime)
    return edits


# ------------------------------------------------------------------------------ preparation


def prepare_naming_migration(
    repository: Path, target_scheme: str, *, clock: datetime | None = None
) -> MigrationPreview:
    """Plan the migration. Write nothing. Raise ValueError or OSError for an unusable project."""
    if target_scheme not in _SCHEMES:
        raise ValueError(f"Unsupported naming scheme: {target_scheme!r}. Use sequential or timestamp.")
    repo = Path(repository).resolve()
    if not (repo / ".specify").is_dir():
        raise ValueError(f"No Spec Kit project found at {repo}")
    if os.path.lexists(repo / ".specify/workspace.json"):
        raise ValueError(f"An external workspace is not a code repository: {repo}")
    workspace = workspace_root_for(repo)
    locator = repo / ".specify/project.json"
    project_id = read_json(locator)["project_id"] if os.path.lexists(locator) else None
    clock = _clock(clock)
    refs: list = []
    read = _reader(refs, (repo, workspace))
    found = _survey(workspace)

    init_path = workspace / ".specify/init-options.json"
    data, mode, mtime = read(init_path)
    options = {} if data is None else _load_object(data, init_path)
    current = options.get("feature_numbering", "sequential")
    if current not in _SCHEMES:
        raise ValueError(f"feature_numbering must be sequential or timestamp: {init_path}")

    conflicts = list(found.conflicts)
    mappings, skipped = _allocate(found, target_scheme, clock, conflicts)
    renames = {tuple(old.split("/")): new.rsplit("/", 1)[1] for old, new in mappings}
    new_of = dict(mappings)
    edits: list[ReferenceEdit] = []
    opaque: list[dict] = []
    if mappings:
        token = _token_pattern(workspace, renames)
        edits += _plan_selection(workspace, project_id, renames, read, conflicts)
        edits += _plan_markdown(workspace, found, renames, new_of, token, conflicts)
        edits += _plan_workflows(workspace, token, renames, read, conflicts, opaque)
        edits += _plan_owned_sections(workspace, repo, token, renames, new_of, options, read, conflicts)
    preference = None
    if current != target_scheme:
        text = (
            _replace_member(data.decode("utf-8"), "feature_numbering", target_scheme)
            if "feature_numbering" in options
            else json.dumps({**options, "feature_numbering": target_scheme}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        preference = ReferenceEdit(
            "preference", init_path, init_path, "/feature_numbering", data or b"", text.encode(), mode, mtime
        )
    proposed = [*edits, *([preference] if preference else [])]
    owners: dict[Path, str] = {}
    for edit in proposed:
        if owners.setdefault(edit.path, edit.owner) != edit.owner:
            conflicts.append(_conflict(
                "ambiguous-reference", _display(edit.path, workspace), "Two reference owners edit this file."
            ))

    notices = []
    if mappings:
        notices.append(_notice("external-caller", _EXTERNAL))
        if project_id:
            notices.append(_notice("other-worktree", _WORKTREE))
    notices += sorted({(n["path"], n["location"]): n for n in opaque}.values(), key=lambda n: (n["path"], n["location"]))
    unique = sorted({(c["path"], c["code"], c["message"]) for c in conflicts})
    return MigrationPreview(
        repository_root=repo,
        workspace_root=workspace,
        project_id=project_id,
        current_scheme=current,
        target_scheme=target_scheme,
        clock=clock,
        mappings=tuple(mappings),
        reference_edits=tuple(edits),
        preference_edit=preference,
        skipped=tuple(sorted(skipped, key=lambda entry: entry["path"])),
        conflicts=tuple({"path": p, "code": c, "message": m} for p, c, m in unique),
        caller_notices=tuple(notices),
        snapshot_digest=_digest(
            str(repo), str(workspace), project_id, target_scheme, clock.isoformat(), found.entries, refs
        ),
        mapping_digest=_digest(
            target_scheme,
            mappings,
            [[e.owner, str(e.path), str(e.new_path), e.location, hashlib.sha256(e.replacement).hexdigest()] for e in proposed],
        ),
    )


# ------------------------------------------------------------------------------ application


def _write(path: Path, data: bytes, mode: int, mtime_ns: int | None) -> None:
    """Replace one file atomically. The new file keeps the mode and the modification time."""
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".naming-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        if mtime_ns is not None:
            os.utime(name, ns=(mtime_ns, mtime_ns))
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _files(preview: MigrationPreview) -> dict[Path, ReferenceEdit]:
    """One edit per file, in application order. The preference comes last."""
    files: dict[Path, ReferenceEdit] = {}
    for edit in (*preview.reference_edits, *filter(None, [preview.preference_edit])):
        files.setdefault(edit.path, edit)
    return files


def _backup(preview: MigrationPreview, files: dict, recovery: Path) -> dict[Path, Path]:
    """Save the original bytes of every edited file and an operation manifest before any change."""
    folder = recovery / "backups"
    folder.mkdir(mode=0o700)
    backups, listing = {}, []
    for index, (path, edit) in enumerate(files.items()):
        if edit.original:
            backups[path] = folder / str(index)
            backups[path].write_bytes(edit.original)
        listing.append({
            "path": str(path),
            "new_path": str(edit.new_path),
            "backup": str(backups[path]) if path in backups else None,
            "mode": edit.mode,
            "mtime_ns": edit.mtime_ns,
        })
    manifest = {
        "repository_root": str(preview.repository_root),
        "workspace_root": str(preview.workspace_root),
        "renames": [{"from": old, "to": new} for old, new in preview.mappings],
        "files": listing,
    }
    (recovery / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return backups


def _forward(preview: MigrationPreview, files: dict, backups: dict, done: dict) -> None:
    """Rename the features, replace the edited files, and save the preference. Log each step before it runs."""
    root = preview.workspace_root
    for old, new in preview.mappings:
        source, target = root / old, root / new
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "The new name is taken", str(target))
        done["renames"].append((source, target))
        os.rename(source, target)
    for path, edit in files.items():
        if path in backups:
            done["writes"].append((path, backups[path], edit.mode, edit.mtime_ns))
        else:
            done["created"].append(path)
        _write(edit.new_path, edit.replacement, edit.mode, edit.mtime_ns)


def _verify(preview: MigrationPreview, files: dict) -> None:
    root = preview.workspace_root
    for old, new in preview.mappings:
        if os.path.lexists(root / old) or not (root / new).is_dir():
            raise RuntimeError(f"The rename of {old} did not complete.")
    for edit in files.values():
        if edit.new_path.read_bytes() != edit.replacement:
            raise RuntimeError(f"The edit of {edit.path} did not complete.")


def _rollback(done: dict) -> list[dict]:
    """Undo every logged step that took effect. Directories first, so each file is back at its original path."""
    remaining: list[dict] = []

    def attempt(operation: str, source: Path, target: Path, action, *args) -> None:
        try:
            action(*args)
        except Exception as exc:
            remaining.append({
                "operation": operation, "from": str(source), "to": str(target),
                "message": f"{type(exc).__name__}: {exc}",
            })

    def restore(path: Path, backup: Path, mode: int, mtime_ns: int) -> None:
        original = backup.read_bytes()
        with contextlib.suppress(OSError):
            info = path.lstat()
            if path.read_bytes() == original and (stat.S_IMODE(info.st_mode), info.st_mtime_ns) == (mode, mtime_ns):
                return  # the replacement never happened
        _write(path, original, mode, mtime_ns)

    def remove(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)

    for source, target in reversed(done["renames"]):
        if not os.path.lexists(source):  # a rename that failed leaves its source in place
            attempt("restore-rename", target, source, os.rename, target, source)
    for path, backup, mode, mtime_ns in reversed(done["writes"]):
        attempt("restore-bytes", backup, path, restore, path, backup, mode, mtime_ns)
    for path in reversed(done["created"]):
        attempt("remove-created", path, path, remove, path)
    return remaining


def _failure(exc: Exception, root: Path) -> dict:
    where = getattr(exc, "filename", None)
    return _conflict("apply-failed", _display(Path(os.fsdecode(where)), root) if where else ".", f"{type(exc).__name__}: {exc}")


def _transact(preview: MigrationPreview, recovery: Path) -> MigrationResult:
    """Validate the preview again under the lock, then apply it with rollback."""
    try:
        fresh = prepare_naming_migration(preview.repository_root, preview.target_scheme, clock=preview.clock)
    except (OSError, ValueError) as exc:
        return MigrationResult(preview, "rejected", None, (_conflict("invalid-project", ".", str(exc)),))
    if fresh != preview:
        stale = _conflict("stale-preview", ".", "The project changed after the preview. Request a new preview.")
        return MigrationResult(preview, "rejected", None, (stale, *fresh.conflicts))
    done: dict[str, list] = {"renames": [], "writes": [], "created": []}
    try:
        files = _files(fresh)
        backups = _backup(fresh, files, recovery)
        _forward(fresh, files, backups, done)
        _verify(fresh, files)
    except BaseException as exc:
        remaining = _rollback(done)
        if remaining:
            with contextlib.suppress(OSError, ValueError):
                manifest = recovery / "manifest.json"
                saved = json.loads(manifest.read_text(encoding="utf-8"))
                saved["remaining_operations"] = remaining
                manifest.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        if not isinstance(exc, Exception):
            if remaining:
                exc.add_note(f"Recovery data stays in {recovery}")
            else:
                shutil.rmtree(recovery, ignore_errors=True)
            raise
        failure = (_failure(exc, fresh.workspace_root),)
        if remaining:
            info = {"directory": str(recovery), "remaining_operations": remaining}
            return MigrationResult(preview, "recovery_required", info, failure)
        return MigrationResult(preview, "rolled_back", None, failure)
    return MigrationResult(preview, "applied")


def apply_naming_migration(preview: MigrationPreview) -> MigrationResult:
    """Apply a preview. The lock, a fresh preparation, and the rollback all stay inside this call."""
    if preview.conflicts:
        return MigrationResult(preview, "rejected", None, tuple(preview.conflicts))
    if not (preview.mappings or preview.preference_edit):
        return MigrationResult(preview, "noop")
    try:
        recovery = Path(tempfile.mkdtemp(prefix="specify-naming-")).resolve()
    except OSError as exc:
        return MigrationResult(preview, "rejected", None, (_conflict("recovery-unavailable", ".", str(exc)),))
    if recovery.is_relative_to(preview.repository_root) or recovery.is_relative_to(preview.workspace_root):
        shutil.rmtree(recovery, ignore_errors=True)
        inside = _conflict("recovery-unavailable", ".", "The temporary directory is inside the project. Move TMPDIR out.")
        return MigrationResult(preview, "rejected", None, (inside,))
    lock = preview.workspace_root / ".specify" / "naming-migration.lock"
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        shutil.rmtree(recovery, ignore_errors=True)
        code = "lock-held" if isinstance(exc, FileExistsError) else "lock-unavailable"
        message = (
            "Another naming migration holds the workspace lock. Stop every writer before removing a stale lock."
            if code == "lock-held" else str(exc)
        )
        return MigrationResult(preview, "rejected", None, (_conflict(code, _display(lock, preview.workspace_root), message),))
    result = None
    started = False
    interrupted = None
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump({"pid": os.getpid()}, stream)
        started = True
        result = _transact(preview, recovery)
    except OSError as exc:
        result = MigrationResult(preview, "rejected", None, (_conflict("lock-unavailable", _display(lock, preview.workspace_root), str(exc)),))
    except BaseException as exc:
        interrupted = exc
        raise
    finally:
        try:
            os.unlink(lock)
        except OSError as exc:
            message = "Stop every writer. Then remove the reported lock."
            if interrupted is not None:
                interrupted.add_note(f"{message} Lock: {lock}")
            else:
                outcome = {
                    "applied": "The command applied the migration changes. Do not reverse them.",
                    "rejected": "The command rejected the migration. No project data changed.",
                    "rolled_back": "The command rolled back all migration changes.",
                    "recovery_required": "The migration still needs data recovery.",
                }[result.status]
                message = f"{outcome} {message}"
                operations = [
                    *([] if result.recovery is None else result.recovery["remaining_operations"]),
                    {"operation": "remove-lock", "from": str(lock), "to": str(lock), "message": message},
                ]
                with contextlib.suppress(OSError, ValueError):
                    manifest = recovery / "manifest.json"
                    saved = json.loads(manifest.read_text()) if manifest.exists() else {}
                    saved["remaining_operations"] = [dict(operation) for operation in operations]
                    manifest.write_text(json.dumps(saved, indent=2), encoding="utf-8")
                failure = _conflict("lock-release-failed", _display(lock, preview.workspace_root), f"{exc}. {message}")
                result = MigrationResult(
                    preview, "recovery_required",
                    {"directory": str(recovery), "remaining_operations": operations},
                    (*result.conflicts, failure),
                    transaction_status=result.status,
                )
        else:
            if (result is not None and result.recovery is None) or (interrupted is not None and not started):
                shutil.rmtree(recovery, ignore_errors=True)
    return result
