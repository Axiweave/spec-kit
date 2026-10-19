#!/usr/bin/env python3
"""Resolve the active feature and its tasks.md for the github extension.

The github extension owns this script so that ``speckit.github.taskstoissues``
keeps working when the core ``taskstoissues`` command (and its
``check_prerequisites`` helper invocation) is deprecated and removed. It uses
core ``common.py`` to resolve the active feature, requires ``plan.md`` and
``tasks.md`` exactly as core's ``--require-tasks --include-tasks`` invocation
does, and reports the optional design docs that sit next to them. Core does not
require ``spec.md`` for this command, so neither does this script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HELP_TEXT = """Usage: resolve_tasks.py [OPTIONS]

Resolve the active feature and its tasks.md for the github extension.

OPTIONS:
  --json      Output in JSON format
  --help, -h  Show this help message

EXAMPLES:
  ./resolve_tasks.py --json
"""


def _status_marker() -> str:
    """Return the status glyph, downgraded to ASCII when stdout cannot encode it.

    On Windows sys.stdout falls back to the ANSI code page whenever it is not a
    console - a pipe or a file redirect, which is how agents and workflow steps
    invoke these scripts - and U+2713 is unencodable in cp1252, so printing it
    raises UnicodeEncodeError and aborts the report right after
    "AVAILABLE_DOCS:". Mirrors core's _status_marker in
    scripts/python/check_prerequisites.py; "[OK]" is also what this script's
    PowerShell twin emits.
    """
    glyph = "✓"
    try:
        glyph.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except (LookupError, UnicodeEncodeError):
        return "[OK]"
    return glyph


def _die(*lines: str) -> "None":
    for line in lines:
        print(line, file=sys.stderr)
    raise SystemExit(1)


def _load_core_common():
    """Load core common.py from the same source or installed layout as this script.

    Source: <repo>/extensions/github/scripts/python -> <repo>/scripts/python.
    Installed: .specify/extensions/github/scripts/python -> .specify/scripts/python.
    """
    candidate = Path(__file__).resolve().parents[4] / "scripts" / "python" / "common.py"
    if not candidate.is_file():
        _die(f"ERROR: Spec Kit core scripts not found: {candidate}")
    spec = importlib.util.spec_from_file_location("speckit_core_common", candidate)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _dir_has_entries(path: Path) -> bool:
    """Whether path is a non-empty directory, treating an error as "no".

    Mirrors core's _dir_has_entries: probing an optional doc directory must
    never abort a resolution that is otherwise valid, which is also what the
    bash (``ls -A 2>/dev/null``) and PowerShell (``-ErrorAction
    SilentlyContinue``) twins do.
    """
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def main(argv: list[str]) -> int:
    json_mode = False
    for arg in argv:
        if arg == "--json":
            json_mode = True
        elif arg in ("--help", "-h"):
            print(HELP_TEXT, end="")
            return 0
        else:
            print(
                f"ERROR: Unknown option '{arg}'. Use --help for usage information.",
                file=sys.stderr,
            )
            return 1

    # Core resolution handles external workspaces, feature_selection and
    # SPECIFY_FEATURE_NO_PERSIST exactly as core check-prerequisites does.
    feature_dir = _load_core_common().get_feature_paths(script_file=Path(__file__)).feature_dir

    if not feature_dir.is_dir():
        _die(
            f"ERROR: Feature directory not found: {feature_dir}",
            "Run the Spec Kit specify command first to "
            "create the feature structure.",
        )

    impl_plan = feature_dir / "plan.md"
    if not impl_plan.is_file():
        _die(
            f"ERROR: plan.md not found in {feature_dir}",
            "Run the Spec Kit plan command first to "
            "create the implementation plan.",
        )

    tasks = feature_dir / "tasks.md"
    if not tasks.is_file():
        _die(
            f"ERROR: tasks.md not found in {feature_dir}",
            "Run the Spec Kit tasks command first to "
            "create the task list.",
        )

    docs: list[str] = []
    if (feature_dir / "research.md").is_file():
        docs.append("research.md")
    if (feature_dir / "data-model.md").is_file():
        docs.append("data-model.md")
    if _dir_has_entries(feature_dir / "contracts"):
        docs.append("contracts/")
    if (feature_dir / "quickstart.md").is_file():
        docs.append("quickstart.md")
    docs.append("tasks.md")

    if json_mode:
        payload = {
            "FEATURE_DIR": str(feature_dir),
            "TASKS": str(tasks),
            "AVAILABLE_DOCS": docs,
        }
        # ASCII escapes decode to the same values and, unlike raw non-ASCII,
        # survive a legacy stdout such as cp1252 on a redirected Windows pipe.
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    else:
        print(f"FEATURE_DIR:{feature_dir}")
        print(f"TASKS:{tasks}")
        print("AVAILABLE_DOCS:")
        marker = _status_marker()
        for doc in docs:
            print(f"  {marker} {doc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
