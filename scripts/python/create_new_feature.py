#!/usr/bin/env python3
"""Create a new feature directory and spec file."""

from __future__ import annotations

import datetime
import json
import random
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path

try:
    from common import (
        TemplateResolutionError,
        confined_workspace_path,
        feature_selection_mode,
        get_repo_root,
        get_workspace_root,
        may_persist_feature_selection,
        persist_feature_json,
        resolve_template_content,
    )
except ImportError:  # pragma: no cover - direct execution from unusual cwd
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (
        TemplateResolutionError,
        confined_workspace_path,
        feature_selection_mode,
        get_repo_root,
        get_workspace_root,
        may_persist_feature_selection,
        persist_feature_json,
        resolve_template_content,
    )


def _json_line(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


_STOP_WORDS = frozenset(
    """
    i a an the to for of in on at by with from is are was were be been being
    have has had do does did will would should could can may might must shall
    this that these those my your our their want need add get set
    """.split()
)

_MAX_BRANCH_LENGTH = 244
_MAX_FEATURE_NUMBER = 2**63 - 1
_ASCII_LOWER = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
)


def _int64_from_digits(value: str) -> int | None:
    normalized = value.lstrip("0") or "0"
    maximum = str(_MAX_FEATURE_NUMBER)
    if len(normalized) > len(maximum) or (
        len(normalized) == len(maximum) and normalized > maximum
    ):
        return None
    return int(normalized, 10)


def _persistence_assignments(
    branch_name: str, feature_dir: str, *, powershell: bool
) -> tuple[str, str]:
    if powershell:
        quoted_branch = "'" + branch_name.replace("'", "''") + "'"
        quoted_dir = "'" + feature_dir.replace("'", "''") + "'"
        return (
            f"$env:SPECIFY_FEATURE = {quoted_branch}",
            f"$env:SPECIFY_FEATURE_DIRECTORY = {quoted_dir}",
        )
    return (
        f"export SPECIFY_FEATURE={shlex.quote(branch_name)}",
        f"export SPECIFY_FEATURE_DIRECTORY={shlex.quote(feature_dir)}",
    )


def _usage(argv0: str) -> str:
    return (
        f"Usage: {argv0} [--json] [--dry-run] [--allow-existing-branch] "
        "[--short-name <name>] [--number N] [--timestamp] <feature_description>"
    )


def _help_text(argv0: str) -> str:
    return f"""{_usage(argv0)}

Options:
  --json              Output in JSON format
  --dry-run           Compute feature name and paths without creating directories or files
  --allow-existing-branch  Reuse an existing feature directory if it already exists
  --short-name <name> Provide a custom short name (2-4 words) for the feature
  --number N          Prefer a feature number (auto-corrected if its specs prefix exists)
  --timestamp         Use timestamp prefix (YYYYMMDD-HHMMSS) instead of sequential numbering
  --help, -h          Show this help message

Examples:
  {argv0} 'Add user authentication system' --short-name 'user-auth'
  {argv0} 'Implement OAuth2 integration for API' --number 5
  {argv0} --timestamp --short-name 'user-auth' 'Add user authentication'
"""


@dataclass(frozen=True)
class Args:
    json_mode: bool = False
    dry_run: bool = False
    allow_existing: bool = False
    short_name: str = ""
    branch_number: str = ""
    use_timestamp: bool = False
    description: str = ""


def _parse_args(argv: list[str], argv0: str) -> Args:
    json_mode = False
    dry_run = False
    allow_existing = False
    short_name = ""
    branch_number = ""
    use_timestamp = False
    rest: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            json_mode = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--allow-existing-branch":
            allow_existing = True
        elif arg in {"--short-name", "--number"}:
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                print(f"Error: {arg} requires a value", file=sys.stderr)
                raise SystemExit(1)
            i += 1
            if arg == "--short-name":
                short_name = argv[i]
            else:
                branch_number = argv[i]
        elif arg == "--timestamp":
            use_timestamp = True
        elif arg in {"--help", "-h"}:
            sys.stdout.write(_help_text(argv0))
            raise SystemExit(0)
        else:
            rest.append(arg)
        i += 1

    description = " ".join(rest).strip()
    if not description:
        if rest:
            print(
                "Error: Feature description cannot be empty or contain only whitespace",
                file=sys.stderr,
            )
        else:
            print(_usage(argv0), file=sys.stderr)
        raise SystemExit(1)

    return Args(
        json_mode=json_mode,
        dry_run=dry_run,
        allow_existing=allow_existing,
        short_name=short_name,
        branch_number=branch_number,
        use_timestamp=use_timestamp,
        description=description,
    )


def _clean_branch_name(name: str) -> str:
    cleaned = _unicode_words(name, "-")
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned.strip("-")


def _unicode_words(name: str, separator: str) -> str:
    return "".join(
        char if char.isalpha() or char.isdecimal() else separator
        for char in name.translate(_ASCII_LOWER)
    )


def _generate_branch_name(description: str) -> str:
    clean = _unicode_words(description, " ")
    meaningful: list[str] = []
    for word in clean.split():
        if word in _STOP_WORDS:
            continue
        if len(word) >= 3 or not word.isascii():
            meaningful.append(word)
        # Keep short words that appear as an uppercase acronym in the original,
        # mirroring the bash twin's case-sensitive `grep -qw` check.
        elif re.search(
            rf"(?<![0-9A-Za-z_]){re.escape(word.upper())}(?![0-9A-Za-z_])",
            description,
        ):
            meaningful.append(word)

    if meaningful:
        max_words = 4 if len(meaningful) == 4 else 3
        return "-".join(meaningful[:max_words])

    cleaned = _clean_branch_name(description)
    return "-".join([part for part in cleaned.split("-") if part][:3])


def _get_highest_from_specs(specs_dir: Path) -> int:
    highest = 0
    if not specs_dir.is_dir():
        return highest
    for entry in specs_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        # Match sequential prefixes (>=3 digits), but skip timestamp dirs.
        if re.match(r"^[0-9]{3,}-", name) and not re.match(
            r"^[0-9]{8}-[0-9]{6}-", name
        ):
            number = _int64_from_digits(re.match(r"^[0-9]+", name).group())
            if number is not None:
                highest = max(highest, number)
    return highest


def _fit_branch_name(feature_num: str, branch_suffix: str) -> str:
    """Fit a feature prefix and suffix within GitHub's branch-name limit."""
    branch_name = f"{feature_num}-{branch_suffix}"
    if len(branch_name.encode("utf-8")) <= _MAX_BRANCH_LENGTH:
        return branch_name

    max_suffix_length = _MAX_BRANCH_LENGTH - (len(feature_num) + 1)
    truncated_suffix = branch_suffix.encode("utf-8")[:max_suffix_length].decode(
        "utf-8", errors="ignore"
    ).rstrip("-")
    return f"{feature_num}-{truncated_suffix}"


def _spec_prefix_exists(
    specs_dir: Path, feature_num: str, own: Path | None = None
) -> bool:
    """Return whether a spec directory other than *own* owns the given prefix.

    This is also the tie rescan after an exclusive claim.
    """
    try:
        return any(
            entry.is_dir()
            and entry.name.startswith(f"{feature_num}-")
            and entry != own
            for entry in specs_dir.iterdir()
        )
    except OSError:
        # Match Bash globbing and PowerShell's ErrorAction=SilentlyContinue.
        return False


def _has_spec_prefix_conflict(
    specs_dir: Path,
    feature_num: str,
    requested_dir: Path,
    *,
    allow_existing: bool,
) -> bool:
    """Return whether another spec directory owns the requested prefix."""
    if allow_existing and requested_dir.is_dir():
        return False

    return _spec_prefix_exists(specs_dir, feature_num)


def main(argv: list[str] | None = None) -> int:
    argv0 = sys.argv[0]
    args = _parse_args(list(argv if argv is not None else sys.argv[1:]), argv0)

    repo_root = get_repo_root(Path(__file__))
    workspace_root = get_workspace_root(repo_root, report=True)
    # Read the policy before any write so an invalid choice creates nothing.
    selection_mode = feature_selection_mode(workspace_root)
    use_timestamp = args.use_timestamp
    if not use_timestamp and not args.branch_number:
        options_path = workspace_root / ".specify" / "init-options.json"
        if workspace_root != repo_root:
            confined_workspace_path(workspace_root, options_path)
        if options_path.is_file():
            try:
                options = json.loads(options_path.read_text(encoding="utf-8"))
                if not isinstance(options, dict):
                    raise ValueError("Project choices must be a JSON object.")
                numbering = options.get("feature_numbering", "sequential")
                if numbering not in ("sequential", "timestamp"):
                    raise ValueError("Feature numbering must be sequential or timestamp.")
                use_timestamp = numbering == "timestamp"
            except (OSError, UnicodeError, ValueError) as exc:
                print(f"ERROR: Cannot read project choices at {options_path}: {exc}", file=sys.stderr)
                return 1
    specs_dir = workspace_root / "specs"
    if workspace_root != repo_root:
        confined_workspace_path(workspace_root, specs_dir)
    if not args.dry_run:
        specs_dir.mkdir(parents=True, exist_ok=True)

    if args.short_name:
        branch_suffix = _clean_branch_name(args.short_name)
    else:
        branch_suffix = _generate_branch_name(args.description)

    if not branch_suffix:
        print(
            "[specify] Warning: Feature name is empty after removing unsupported characters. "
            "Use --short-name with letters or digits (for example, user-auth).",
            file=sys.stderr,
        )

    branch_number = args.branch_number
    if args.use_timestamp and branch_number:
        print(
            "[specify] Warning: --number is ignored when --timestamp is used",
            file=sys.stderr,
        )
        branch_number = ""

    if use_timestamp:
        feature_num = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        if branch_number:
            # Mirrors bash: $((10#$BRANCH_NUMBER)) only accepts unsigned
            # decimal digits, rejecting signs, whitespace, and other
            # characters that int() would otherwise tolerate.
            if not re.fullmatch(r"[0-9]+", branch_number):
                print(
                    "Error: --number must be an unsigned integer, "
                    f"got '{branch_number}'",
                    file=sys.stderr,
                )
                return 1
            number = _int64_from_digits(branch_number)
            if number is None:
                print(
                    "Error: --number must be between 0 and "
                    f"{_MAX_FEATURE_NUMBER}, got '{branch_number}'",
                    file=sys.stderr,
                )
                return 1
        else:
            number = _get_highest_from_specs(specs_dir) + 1
        if number > _MAX_FEATURE_NUMBER:
            rejected_number = branch_number or str(number)
            number_label = "--number" if branch_number else "feature number"
            print(
                f"Error: {number_label} must be between 0 and "
                f"{_MAX_FEATURE_NUMBER}, got '{rejected_number}'",
                file=sys.stderr,
            )
            return 1
        feature_num = f"{number:03d}"

        # Treat an explicit number as a preference when its prefix is already used
        # by a feature directory. Auto-detected numbers are already conflict-free.
        if branch_number:
            requested_branch_name = _fit_branch_name(feature_num, branch_suffix)
            requested_dir = specs_dir / requested_branch_name
            spec_conflict = _has_spec_prefix_conflict(
                specs_dir,
                feature_num,
                requested_dir,
                allow_existing=args.allow_existing,
            )
            if spec_conflict:
                requested_num = feature_num
                number = _get_highest_from_specs(specs_dir)
                while True:
                    number += 1
                    if number > _MAX_FEATURE_NUMBER:
                        print(
                            f"Error: feature number must be between 0 and "
                            f"{_MAX_FEATURE_NUMBER}, got '{number}'",
                            file=sys.stderr,
                        )
                        return 1
                    feature_num = f"{number:03d}"
                    if not _spec_prefix_exists(specs_dir, feature_num):
                        break
                print(
                    f"[specify] Warning: --number {requested_num} conflicts with "
                    f"an existing spec directory; using {feature_num} instead",
                    file=sys.stderr,
                )

    max_suffix_length = _MAX_BRANCH_LENGTH - (len(feature_num) + 1)
    if max_suffix_length <= 0:
        print("Error: feature number is too long for a branch name", file=sys.stderr)
        return 1

    original_branch_name = f"{feature_num}-{branch_suffix}"
    branch_name = _fit_branch_name(feature_num, branch_suffix)

    # GitHub enforces a 244-byte limit on branch names.
    if branch_name != original_branch_name:
        print(
            "[specify] Warning: Branch name exceeded GitHub's 244-byte limit",
            file=sys.stderr,
        )
        print(
            f"[specify] Original: {original_branch_name} "
            f"({len(original_branch_name.encode('utf-8'))} bytes)",
            file=sys.stderr,
        )
        print(
            f"[specify] Truncated to: {branch_name} "
            f"({len(branch_name.encode('utf-8'))} bytes)",
            file=sys.stderr,
        )

    feature_dir = specs_dir / branch_name
    spec_file = feature_dir / "spec.md"
    if workspace_root != repo_root:
        confined_workspace_path(workspace_root, feature_dir)
        confined_workspace_path(workspace_root, spec_file)

    if not args.dry_run:
        template_content = None
        needs_spec = not spec_file.is_file()
        if needs_spec:
            try:
                template_content = resolve_template_content(
                    "spec-template", repo_root
                )
            except TemplateResolutionError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1

        # Claim the directory exclusively before any write. An auto-numbered run
        # loses when any other directory has its prefix, removes its own empty
        # directory, and retries with a new prefix.
        auto_numbered = not branch_number and not args.allow_existing
        for attempt in range(1, 21):
            try:
                feature_dir.mkdir()
            except FileExistsError:
                if args.allow_existing and feature_dir.is_dir():
                    break
                if use_timestamp:
                    print(
                        f"Error: Feature directory '{feature_dir}' already exists. "
                        "Rerun to get a new timestamp or use a different --short-name.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Error: Feature directory '{feature_dir}' already exists. "
                        "Please use a different feature name or specify a different "
                        "number with --number.",
                        file=sys.stderr,
                    )
                return 1
            if not auto_numbered or not _spec_prefix_exists(
                specs_dir, feature_num, feature_dir
            ):
                break
            feature_dir.rmdir()
            if attempt == 20:
                print(
                    "ERROR: Could not reserve a feature directory after 20 tries.",
                    file=sys.stderr,
                )
                return 1
            time.sleep(random.random())
            if use_timestamp:
                lost_num = feature_num
                while (
                    feature_num := datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                ) <= lost_num:
                    time.sleep(0.1)
            else:
                number = _get_highest_from_specs(specs_dir) + 1
                if number > _MAX_FEATURE_NUMBER:
                    print(
                        f"Error: feature number must be between 0 and "
                        f"{_MAX_FEATURE_NUMBER}, got '{number}'",
                        file=sys.stderr,
                    )
                    return 1
                feature_num = f"{number:03d}"
            branch_name = _fit_branch_name(feature_num, branch_suffix)
            feature_dir = specs_dir / branch_name
            spec_file = feature_dir / "spec.md"
            if workspace_root != repo_root:
                confined_workspace_path(workspace_root, feature_dir)
                confined_workspace_path(workspace_root, spec_file)

        if needs_spec:
            if template_content is not None:
                spec_file.write_bytes(template_content.encode("utf-8"))
            else:
                print(
                    "Warning: Spec template not found; created empty spec file",
                    file=sys.stderr,
                )
                spec_file.touch()

        # Only automatic projects save the active feature. Context projects keep
        # the saved feature as it is, and SPECIFY_FEATURE_NO_PERSIST always wins.
        if may_persist_feature_selection(selection_mode):
            persist_feature_json(repo_root, f"specs/{branch_name}")

        # Inform the user how to set feature state in their own shell.
        feature_assignment, directory_assignment = _persistence_assignments(
            branch_name,
            str(feature_dir),
            powershell=sys.platform == "win32",
        )
        print(f"# To select this feature: {directory_assignment}", file=sys.stderr)
        print(f"# Optional label:         {feature_assignment}", file=sys.stderr)

    if args.json_mode:
        payload: dict[str, object] = {
            "BRANCH_NAME": branch_name,
            "SPEC_FILE": str(spec_file),
            "FEATURE_NUM": feature_num,
        }
        if args.dry_run:
            payload["DRY_RUN"] = True
        sys.stdout.write(_json_line(payload))
    else:
        print(f"BRANCH_NAME: {branch_name}")
        print(f"SPEC_FILE: {spec_file}")
        print(f"FEATURE_NUM: {feature_num}")
        if not args.dry_run:
            print(f"# To select this feature: {directory_assignment}")
            print(f"# Optional label:         {feature_assignment}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
