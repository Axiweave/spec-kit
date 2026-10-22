"""Report the stage, blockers, and next step of each feature under the workspace specs/ root.

report() reads the feature files and writes nothing. The same files give the same result.
The stage model is documented in docs/reference/agentic-sdd.md.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..project.naming import classify_feature_name, duplicate_prefixes, feature_dirs

# Stage -> (display name, canonical next step), in rule precedence order.
STAGES: dict[str, tuple[str, str | None]] = {
    "unreadable": ("Unreadable", None),
    "no-spec": ("No spec", None),
    "needs-clarification": ("Needs clarification", "speckit.clarify"),
    "done": ("Done", None),
    "in-progress": ("In progress", "speckit.implement"),
    "tasks-ready": ("Tasks ready", "speckit.implement"),
    "planned": ("Planned", "speckit.tasks"),
    "spec-ready": ("Spec ready", "speckit.plan"),
}
_RANK = {"in-progress": 0, "tasks-ready": 1, "planned": 2, "spec-ready": 3}

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")
_MARKER = re.compile(r"\[NEEDS CLARIFICATION")
_PLAN_MARKER = re.compile(r"^\s*\*\*[^*]+\*\*:\s*NEEDS CLARIFICATION")
_TASK = re.compile(r"^\s*- \[([ xX])\] (T\d+)\b")
_ITEM = re.compile(r"^\s*- \[([ xX])\]")


@dataclass(frozen=True)
class Location:
    file: str
    line: int


@dataclass(frozen=True)
class Finding:
    file: str
    reason: str  # no-spec, unreadable, or symlink


@dataclass(frozen=True)
class ChecklistCount:
    file: str
    open: int
    total: int


@dataclass(frozen=True)
class TaskProgress:
    checked: int
    total: int
    open_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Feature:
    path: str
    stage: str
    blocked: bool
    next_step: str | None
    markers: list[Location]
    requirements: ChecklistCount | None
    custom_checklists: list[ChecklistCount]
    tasks: TaskProgress | None
    findings: list[Finding]


@dataclass(frozen=True)
class Recommendation:
    feature: str | None
    next_step: str | None
    rule: str  # finish-before-start, resolve-blocker, or no-unfinished


def _prefixed(name: str) -> bool:
    try:
        return classify_feature_name(name)[0] != "custom"
    except ValueError:  # a prefix that is too large or not a real time still names a feature
        return True


def _discover(workspace: Path) -> list[tuple[str, Finding | None]]:
    """Return (path, finding) for each feature. A finding marks a prefixed folder without a regular spec.md."""
    root = workspace / "specs"
    if root.is_symlink():
        raise ValueError(f"The specs root is a symlink. Spec Kit does not follow it: {root}")
    if not root.is_dir():
        return []
    unlistable: set[str] = set()

    def skip(rel: str) -> None:
        if rel == "specs":
            raise  # re-raise the active scandir error: a specs root that cannot be listed stops the report
        unlistable.add(rel)

    features = set(feature_dirs(root, on_error=skip))
    found: list[tuple[str, Finding | None]] = [(rel, None) for rel in features]
    found += [(rel, Finding(".", "unreadable")) for rel in unlistable]

    def walk(path: str, rel: str) -> None:
        for child in os.scandir(path):
            if not child.is_dir(follow_symlinks=False) or (rel == "specs" and child.name.startswith(".merge-specs")):
                continue
            child_rel = f"{rel}/{child.name}"
            if child_rel in features or child_rel in unlistable:
                continue
            # ponytail: O(folders x features) prefix scan, index by parent if specs/ grows past thousands
            if any(feature.startswith(child_rel + "/") for feature in features):
                walk(child.path, child_rel)
            elif _prefixed(child.name):
                spec = Path(child.path) / "spec.md"
                reason = "symlink" if spec.is_symlink() else "unreadable" if spec.exists() else "no-spec"
                found.append((child_rel, Finding("spec.md", reason)))
            else:
                walk(child.path, child_rel)

    walk(str(root), "specs")
    return sorted(found, key=lambda entry: entry[0])


def _read(folder: Path, name: str) -> str | Finding | None:
    """Return the text, a Finding for a symlink or an unreadable file, or None when the file is absent."""
    path = folder / name
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return Finding(name, "unreadable")
    if stat.S_ISLNK(info.st_mode):
        return Finding(name, "symlink")
    if not stat.S_ISREG(info.st_mode):
        return Finding(name, "unreadable")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return Finding(name, "unreadable")


def _markers(text: str, *, plan: bool) -> list[int]:
    """Return the 1-based lines with a clarification marker, outside fenced blocks and inline code."""
    lines: list[int] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = _CODE_SPAN.sub("", line)
        if _MARKER.search(line) or (plan and _PLAN_MARKER.match(line)):
            lines.append(number)
    return lines


def _tasks(text: str) -> TaskProgress:
    checked = 0
    open_ids: list[str] = []
    for line in text.splitlines():
        match = _TASK.match(line)
        if match is None:
            continue
        if match.group(1) == " ":
            open_ids.append(match.group(2))
        else:
            checked += 1
    return TaskProgress(checked, checked + len(open_ids), open_ids if 1 <= len(open_ids) <= 3 else [])


def _checklist(text: str, file: str) -> ChecklistCount:
    marks = [match.group(1) for match in (_ITEM.match(line) for line in text.splitlines()) if match]
    return ChecklistCount(file, marks.count(" "), len(marks))


def _feature(workspace: Path, rel: str, finding: Finding | None) -> Feature:
    folder = workspace / rel
    findings = [finding] if finding else []
    if finding is not None and finding.file == ".":  # the folder cannot be listed, so nothing inside can be read
        return Feature(rel, "unreadable", True, None, [], None, [], None, findings)
    texts: dict[str, str] = {}
    for name in ("plan.md", "tasks.md") if finding else ("spec.md", "plan.md", "tasks.md"):
        value = _read(folder, name)
        if isinstance(value, Finding):
            findings.append(value)
        elif value is not None:
            texts[name] = value

    requirements: ChecklistCount | None = None
    custom: list[ChecklistCount] = []
    checklists = folder / "checklists"
    if checklists.is_symlink():
        findings.append(Finding("checklists", "symlink"))
    elif checklists.is_dir():
        try:
            names = sorted(entry.name for entry in os.scandir(checklists) if entry.name.endswith(".md"))
        except OSError:
            names = []
            findings.append(Finding("checklists", "unreadable"))
        for name in names:
            file = f"checklists/{name}"
            value = _read(folder, file)
            if isinstance(value, Finding):
                findings.append(value)
            elif value is not None:
                count = _checklist(value, file)
                if name == "requirements.md":
                    requirements = count
                else:
                    custom.append(count)

    markers = [
        Location(name, line) for name in ("spec.md", "plan.md") if name in texts
        for line in _markers(texts[name], plan=name == "plan.md")
    ]
    tasks = _tasks(texts["tasks.md"]) if "tasks.md" in texts else None

    if any(item.reason != "no-spec" for item in findings):
        stage = "unreadable"
    elif findings:
        stage = "no-spec"
    elif markers:
        stage = "needs-clarification"
    elif tasks is not None and tasks.total and tasks.checked == tasks.total:
        stage = "done"
    elif tasks is not None and tasks.checked:
        stage = "in-progress"
    elif tasks is not None:
        stage = "tasks-ready"
    elif "plan.md" in texts:
        stage = "planned"
    else:
        stage = "spec-ready"

    next_step = STAGES[stage][1]
    blocked = stage in ("unreadable", "no-spec", "needs-clarification")
    if stage in _RANK:
        if tasks is not None and tasks.total == 0:
            blocked, next_step = True, "speckit.tasks"
        if requirements is not None and requirements.open:
            blocked, next_step = True, "speckit.clarify"
    return Feature(rel, stage, blocked, next_step, markers, requirements, custom, tasks, findings)


def _sort_key(feature: Feature) -> tuple[int, int, str]:
    if feature.stage == "done":
        return 3, 0, feature.path
    if not feature.blocked:
        return 0, _RANK[feature.stage], feature.path
    if feature.next_step is not None:
        return 1, -1 if feature.stage == "needs-clarification" else _RANK[feature.stage], feature.path
    return 2, 0 if feature.stage == "no-spec" else 1, feature.path


def _recommend(features: list[Feature]) -> Recommendation:
    first = next((feature for feature in features if feature.stage != "done"), None)
    if first is None:
        return Recommendation(None, "speckit.specify", "no-unfinished")
    return Recommendation(first.path, first.next_step, "resolve-blocker" if first.blocked else "finish-before-start")


def report(workspace: Path) -> dict[str, Any]:
    """Return the report for the workspace: the recommendation, every feature in report order, and duplicate prefixes."""
    features = sorted((_feature(workspace, rel, finding) for rel, finding in _discover(workspace)), key=_sort_key)
    return {
        "workspace_root": str(workspace),
        "recommendation": asdict(_recommend(features)),
        "features": [asdict(feature) for feature in features],
        "duplicate_prefixes": duplicate_prefixes(workspace, on_error=lambda rel: None),
    }
