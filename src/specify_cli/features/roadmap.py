"""Roadmap tables, spec back-references, and the drift and ready rules between them.

This module does no I/O. ``features.todo`` reads the files and passes their text in.
The rules are documented in ``docs/reference/agentic-sdd.md#roadmaps``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STATUSES = ("planned", "in-progress", "done")
FINDING_KINDS = ("unreadable-roadmap", "broken-link", "missing-entry", "status-drift")
COLUMNS = ("id", "sub-feature", "intent", "scope boundary", "depends on", "status", "sub-spec")

_NONE = frozenset({"", "—", "–", "-"})
_FENCE = re.compile(r"^\s*(```|~~~)")  # same rule as todo._FENCE
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*"
_VALID_ID = re.compile(_ID)
_BACKREF = re.compile(
    r"Parent roadmap:\s*`?(?P<path>[^`\s]+)`?\s*(?:→|->)\s*entry\s+(?:\*\*)?(?P<id>" + _ID + ")"
)
_ROADMAP_PATH = re.compile(r"specs/[^/.][^/]*/roadmap\.md")
_SEPARATOR_CELL = re.compile(r":?-+:?")


@dataclass(frozen=True)
class BackRef:
    roadmap: str
    entry: str
    line: int


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    line: int
    depends_on: list[str]
    status: str
    expected_status: str
    sub_spec: str | None
    ready: bool


@dataclass(frozen=True)
class Roadmap:
    path: str
    entries: list[Entry]


@dataclass(frozen=True)
class RoadmapFinding:
    kind: str
    file: str
    line: int | None
    roadmap: str
    entry: str | None
    message: str


@dataclass(frozen=True)
class RoadmapRecommendation:
    roadmap: str
    entry: str
    next_step: str = "speckit.specify"


def _unfenced(lines: list[str]):
    fenced = False
    for number, line in enumerate(lines, 1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            yield number, line


def parent_ref(text: str) -> BackRef | None:
    """Return the first ``Parent roadmap: <path> → entry <ID>`` outside fenced blocks."""
    for number, line in _unfenced(text.splitlines()):
        match = _BACKREF.search(line)
        if match:
            return BackRef(match["path"], match["id"], number)
    return None


def _cells(line: str) -> list[str]:
    body = line.strip()
    body = body[1:] if body.startswith("|") else body
    body = body[:-1] if body.endswith("|") else body
    # ponytail: a `|` inside a cell splits it; handle escaped pipes if roadmaps need them.
    return [cell.strip() for cell in body.split("|")]


def _clean(cell: str) -> str:
    return cell.strip().strip("*`").strip()


def _column(cell: str) -> str:
    return " ".join(_clean(cell).lower().split())


def parse(text: str) -> list[tuple[int, dict[str, str]]] | tuple[int | None, str]:
    """Return the entry rows as ``(line, cells by column)``, or ``(line, message)`` when unreadable."""
    lines = text.splitlines()
    for number, line in _unfenced(lines):
        if not line.lstrip().startswith("|"):
            continue
        names = [_column(cell) for cell in _cells(line)]
        if not set(COLUMNS) <= set(names):
            continue
        separator = lines[number] if number < len(lines) else ""
        cells = _cells(separator)
        if len(cells) != len(names) or not all(_SEPARATOR_CELL.fullmatch(c) for c in cells):
            return number, "The roadmap table has no separator row after its header."
        rows: list[tuple[int, dict[str, str]]] = []
        seen: set[str] = set()
        for row_number in range(number + 2, len(lines) + 1):
            row = lines[row_number - 1]
            if not row.lstrip().startswith("|"):
                break
            values = _cells(row)
            if len(values) != len(names):
                return row_number, f"The row has {len(values)} cells, the header has {len(names)}."
            cells_by_column = {name: _clean(value) for name, value in zip(names, values) if name in COLUMNS}
            entry_id = cells_by_column["id"]
            if not entry_id:
                return row_number, "The row has no ID."
            if not _VALID_ID.fullmatch(entry_id):
                return row_number, f"The ID {entry_id} is not valid. Use letters, digits, '-', '_', and '.'."
            if entry_id in seen:
                return row_number, f"The ID {entry_id} is used twice."
            seen.add(entry_id)
            rows.append((row_number, cells_by_column))
        return rows
    return None, "The file has no roadmap table header."


def _none(value: str) -> bool:
    return value in _NONE


def _valid_sub_spec(path: str) -> bool:
    parts = path.split("/")
    return parts[0] == "specs" and len(parts) > 1 and all(p and p not in (".", "..") for p in parts)


@dataclass
class _Raw:
    id: str
    name: str
    line: int
    depends_on: list[str]
    status: str
    sub_spec: str | None
    expected: str = "planned"


def check(
    texts: dict[str, str],
    unreadable: dict[str, str],
    features: dict[str, tuple[str, BackRef | None]],
) -> tuple[list[Roadmap], list[RoadmapFinding], RoadmapRecommendation | None]:
    """Check every roadmap against the features.

    ``texts`` maps a roadmap path to its text, ``unreadable`` maps a roadmap path to the reason
    it was not read, and ``features`` maps a feature path to its stage and back-reference.
    """
    findings: list[RoadmapFinding] = []
    entries: dict[str, list[_Raw]] = {path: [] for path in unreadable}
    broken = set(unreadable)
    for path, reason in unreadable.items():
        findings.append(RoadmapFinding("unreadable-roadmap", path, None, path, None, f"The roadmap is not read ({reason})."))
    for path, text in texts.items():
        result = parse(text)
        if isinstance(result, tuple):
            broken.add(path)
            entries[path] = []
            findings.append(RoadmapFinding("unreadable-roadmap", path, result[0], path, None, result[1]))
            continue
        entries[path] = [
            _Raw(
                id=cells["id"],
                name=cells["sub-feature"],
                line=line,
                depends_on=[] if _none(cells["depends on"]) else [
                    _clean(d) for d in cells["depends on"].split(",") if not _none(_clean(d))
                ],
                status=cells["status"].lower(),
                sub_spec=None if _none(cells["sub-spec"]) else cells["sub-spec"].rstrip("/"),
            )
            for line, cells in result
        ]

    for feature_path, (_stage, ref) in features.items():
        if ref is None or ref.roadmap in broken:
            continue
        kind, message = _spec_side(ref, feature_path, entries)
        if kind:
            findings.append(RoadmapFinding(kind, f"{feature_path}/spec.md", ref.line, ref.roadmap, ref.entry, message))

    for path, rows in entries.items():
        for raw in rows:
            message = None
            if raw.sub_spec is not None:
                feature = features.get(raw.sub_spec)
                ref = feature[1] if feature else None
                if not _valid_sub_spec(raw.sub_spec):
                    message = f"Sub-spec {raw.sub_spec} is not a relative path under specs/."
                elif feature is None:
                    message = f"Sub-spec {raw.sub_spec} is not a feature."
                elif ref is None or (ref.roadmap, ref.entry) != (path, raw.id):
                    message = f"The spec of {raw.sub_spec} does not point back to entry {raw.id}."
                raw.expected = "done" if feature and feature[0] == "done" else "in-progress"
            if message:
                findings.append(RoadmapFinding("broken-link", path, raw.line, path, raw.id, message))
            elif raw.status != raw.expected:
                findings.append(RoadmapFinding(
                    "status-drift", path, raw.line, path, raw.id,
                    f"Stored status {raw.status or '(empty)'}, expected {raw.expected}.",
                ))

    flagged = {(f.roadmap, f.entry) for f in findings}
    roadmaps: list[Roadmap] = []
    recommendation = None
    for path in sorted(entries):
        expected = {raw.id: raw.expected for raw in entries[path]}
        looped = _in_cycle(entries[path])
        built = []
        for raw in entries[path]:
            ready = (
                raw.expected == "planned"
                and (path, raw.id) not in flagged
                and raw.id not in looped
                and all(expected.get(dep) == "done" for dep in raw.depends_on)
            )
            if ready and recommendation is None:
                recommendation = RoadmapRecommendation(path, raw.id)
            built.append(Entry(raw.id, raw.name, raw.line, raw.depends_on, raw.status, raw.expected, raw.sub_spec, ready))
        roadmaps.append(Roadmap(path, built))
    findings.sort(key=lambda f: (f.file, f.line or 0, f.kind, f.entry or ""))
    return roadmaps, findings, recommendation


def _in_cycle(rows: list[_Raw]) -> set[str]:
    """Return the IDs whose dependencies lead back to themselves. Unknown IDs end a path."""
    deps = {raw.id: raw.depends_on for raw in rows}
    looped: set[str] = set()
    for start in deps:
        seen: set[str] = set()
        stack = list(deps[start])
        while stack:
            node = stack.pop()
            if node == start:
                looped.add(start)
                break
            if node in deps and node not in seen:
                seen.add(node)
                stack.extend(deps[node])
    return looped


def _spec_side(ref: BackRef, feature_path: str, entries: dict[str, list[_Raw]]) -> tuple[str | None, str]:
    if not _ROADMAP_PATH.fullmatch(ref.roadmap):
        return "broken-link", f"The back-reference path {ref.roadmap} is not specs/<name>/roadmap.md."
    if ref.roadmap not in entries:
        return "missing-entry", f"The roadmap {ref.roadmap} does not exist or cannot be read."
    target = next((e for e in entries[ref.roadmap] if e.id == ref.entry), None)
    if target is None:
        return "missing-entry", f"The roadmap has no entry {ref.entry}."
    if target.sub_spec != feature_path:
        return "broken-link", f"Entry {ref.entry} names sub-spec {target.sub_spec or '—'}, not this feature."
    return None, ""
