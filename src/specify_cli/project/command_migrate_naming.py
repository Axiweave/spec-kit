"""The specify project migrate-naming command."""
from __future__ import annotations

import base64
import json
import re
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NoReturn
from uuid import UUID

import typer

from ..workspace import find_repository

TOKEN_VERSION = 1
MAX_TOKEN = 16 * 1024
TOKEN_TEXT = re.compile(r"[A-Za-z0-9_-]+")
CLOCK_TEXT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")
DIGEST_TEXT = re.compile(r"[0-9a-f]{64}")
TOKEN_FIELDS = frozenset({
    "version", "repository_root", "workspace_root", "project_id",
    "target_scheme", "clock", "snapshot_digest", "mapping_digest",
})
PRINTED = ("preview", "applied", "noop", "cancelled")  # statuses that text mode shows on stdout


class Scheme(str, Enum):
    sequential = "sequential"
    timestamp = "timestamp"


SCHEMES = tuple(scheme.value for scheme in Scheme)


class _BadToken(ValueError):
    """An approval token failed validation."""


# ----------------------------------------------------------------- approval token


def _stamp(clock: datetime) -> str:
    return clock.isoformat(timespec="seconds")


def _token(preview) -> str:
    """Bind a conflict-free preview. The token holds identities and digests, never mappings or bytes."""
    data = {
        "version": TOKEN_VERSION,
        "repository_root": str(preview.repository_root),
        "workspace_root": str(preview.workspace_root),
        "project_id": preview.project_id,
        "target_scheme": preview.target_scheme,
        "clock": _stamp(preview.clock),
        "snapshot_digest": preview.snapshot_digest,
        "mapping_digest": preview.mapping_digest,
    }
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _clock(value) -> datetime | None:
    if type(value) is str and CLOCK_TEXT.fullmatch(value):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _canonical_uuid(value) -> bool:
    if value is None:
        return True
    try:
        return type(value) is str and str(UUID(value)) == value
    except ValueError:
        return False


def _decode(text: str) -> dict:
    """Validate an untrusted token. Return its fields with the clock parsed."""
    if len(text) > MAX_TOKEN or not TOKEN_TEXT.fullmatch(text):
        raise _BadToken("The text is too large or is not URL-safe base64.")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8"))
    except (ValueError, RecursionError):
        raise _BadToken("The text does not decode to JSON.") from None
    if not isinstance(data, dict) or set(data) != TOKEN_FIELDS:
        raise _BadToken("The fields are missing or unknown.")
    if type(data["version"]) is not int or data["version"] != TOKEN_VERSION:
        raise _BadToken("The version is not supported.")
    clock = _clock(data["clock"])
    if not (
        all(type(data[key]) is str and Path(data[key]).is_absolute() for key in ("repository_root", "workspace_root"))
        and _canonical_uuid(data["project_id"])
        and type(data["target_scheme"]) is str and data["target_scheme"] in SCHEMES
        and clock is not None
        and all(type(data[key]) is str and DIGEST_TEXT.fullmatch(data[key]) for key in ("snapshot_digest", "mapping_digest"))
    ):
        raise _BadToken("A field has an unsupported value.")
    return {**data, "clock": clock}


# -------------------------------------------------------------------------- result


def _text(value) -> str | None:
    return None if value is None else str(value)


def _rows(items, *keys: str) -> list[dict]:
    return [{key: _text(item.get(key)) for key in keys} for item in items]


def _conflict(code: str, message: str, path: str = ".") -> dict:
    return {"path": path, "code": code, "message": message}


def _document(target, status, preview=None, *, proposed=True, conflicts=None, recovery=None, token=None) -> dict:
    """Build the contract's JSON object.

    Without a preview, or when the preview is not proposed, the lists stay empty.
    """
    shown = preview if proposed else None

    def part(name):
        return getattr(shown, name, ())

    return {
        "schema_version": 1,
        "status": status,
        "repository_root": _text(getattr(preview, "repository_root", None)),
        "workspace_root": _text(getattr(preview, "workspace_root", None)),
        "project_id": getattr(preview, "project_id", None),
        "current_scheme": getattr(preview, "current_scheme", None),
        "target_scheme": target,
        "clock": None if preview is None else _stamp(preview.clock),
        "mappings": [{"from": old, "to": new} for old, new in part("mappings")],
        "changed_references": [
            {"owner": edit.owner, "path": str(edit.path), "new_path": str(edit.new_path), "location": edit.location}
            for edit in part("reference_edits")
        ],
        "preference_changed": getattr(shown, "preference_edit", None) is not None,
        "skipped": _rows(part("skipped"), "path", "reason"),
        "conflicts": _rows(
            getattr(preview, "conflicts", ()) if conflicts is None else conflicts, "path", "code", "message"
        ),
        "caller_notices": _rows(part("caller_notices"), "kind", "path", "location", "message"),
        "approval_token": token,
        "recovery": None if not recovery else {
            "directory": str(recovery["directory"]),
            "remaining_operations": _rows(recovery["remaining_operations"], "operation", "from", "to", "message"),
        },
    }


def _describe(target: str, preview) -> dict:
    """Report a prepared preview. Only a conflict-free preview with work to do gets a token."""
    if preview.conflicts:
        return _document(target, "preview", preview)
    if not (preview.mappings or preview.preference_edit):
        return _document(target, "noop", preview)
    return _document(target, "preview", preview, token=_token(preview))


# ---------------------------------------------------------------------- text output


def _say(line: str = "", *, err: bool = False) -> None:
    """Print one line. Untrusted names cannot add lines or terminal controls."""
    typer.echo("".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in line), err=err)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _section(title: str, rows: list[str]) -> None:
    _say(f"{title}: {len(rows) or 'none'}")
    for row in rows:
        _say(f"  {row}")


def _notice(notice: dict) -> str:
    where = [part for part in (notice["path"], f"({notice['location']})" if notice["location"] else None) if part]
    return f"[{notice['kind']}] " + (" ".join(where) + ": " if where else "") + notice["message"]


def _print(doc: dict, footer: tuple[str, ...] = ()) -> None:
    status, target = doc["status"], doc["target_scheme"]
    if status == "noop":
        _say(f"Feature naming already uses {target}. No files were changed.")
        _section("Skipped", [f"{item['path']}: {item['reason']}" for item in doc["skipped"]])
        _section("Caller notices", [_notice(item) for item in doc["caller_notices"]])
        return
    if status == "cancelled":
        _say("Migration cancelled. No files were changed.")
        return
    _say(f"Feature naming migration {'applied' if status == 'applied' else 'preview'}: "
         f"{doc['current_scheme']} -> {target}")
    _say(f"Repository: {doc['repository_root']}")
    _say(f"Workspace: {doc['workspace_root']}")
    if doc["project_id"]:
        _say(f"Project ID: {doc['project_id']}")
    _section("Renames", [f"{item['from']} -> {item['to']}" for item in doc["mappings"]])
    edits = doc["changed_references"]
    if edits:
        _say(f"Reference updates: {_count(len(edits), 'edit')} in {_count(len({e['path'] for e in edits}), 'file')}")
        for edit in edits:
            where = edit["path"] if edit["path"] == edit["new_path"] else f"{edit['path']} -> {edit['new_path']}"
            _say(f"  [{edit['owner']}] {where} ({edit['location']})")
    else:
        _say("Reference updates: none")
    _say(f"Preference: feature_numbering {doc['current_scheme']} -> {target}"
         if doc["preference_changed"] else "Preference: unchanged")
    _section("Skipped", [f"{item['path']}: {item['reason']}" for item in doc["skipped"]])
    _section("Conflicts", [f"[{item['code']}] {item['path']}: {item['message']}" for item in doc["conflicts"]])
    _section("Caller notices", [_notice(item) for item in doc["caller_notices"]])
    if status == "applied":
        _say("Not changed by this command: other worktrees, external callers, source files, and Git history.")
    for line in footer:
        _say(line)


def _guidance(target: str) -> tuple[str, ...]:
    command = f"specify project migrate-naming --feature-numbering {target}"
    return (
        "No files were changed.",
        "To apply the changes, run this command again in a terminal and answer the prompt.",
        f"To approve without a terminal, run: {command} --dry-run --json",
        "Then run the next command with the approval_token value from that output:",
        f"  {command} --apply APPROVAL_TOKEN",
    )


def _explain(doc: dict, listed: bool) -> None:
    """Write the diagnostics for a result that is not a success."""
    status, conflicts, recovery = doc["status"], doc["conflicts"], doc["recovery"]
    headline = {
        "rejected": "Migration rejected. No files were changed.",
        "rolled_back": "Migration failed. All completed changes were rolled back.",
        "recovery_required": "Migration requires recovery. Complete the reported operations.",
    }.get(status)
    if status == "preview" and conflicts:
        headline = f"The preview has {_count(len(conflicts), 'blocking conflict')}. No approval token was issued."
    if headline is None:
        return
    _say(f"Error: {headline}", err=True)
    if not listed:
        for item in conflicts:
            _say(f"  [{item['code']}] {item['path']}: {item['message']}", err=True)
    if recovery:
        operations = recovery["remaining_operations"]
        _say(f"Recovery directory: {recovery['directory']}", err=True)
        _say(f"Remaining operations: {len(operations)}", err=True)
        for item in operations:
            _say(f"  {item['operation']}: {item['from']} -> {item['to']}", err=True)
            _say(f"    {item['message']}", err=True)
        _say("Complete the remaining operations by hand. Keep any backup files until recovery is complete.", err=True)


def _finish(doc: dict, json_output: bool, footer: tuple[str, ...] = ()) -> NoReturn:
    """Print the result, explain a failure on stderr, and exit with the contract's code."""
    if json_output:
        typer.echo(json.dumps(doc))
    elif doc["status"] in PRINTED:
        _print(doc, footer)
    _explain(doc, listed=not json_output and doc["status"] in PRINTED)
    done = doc["status"] in ("applied", "noop", "cancelled") or (doc["status"] == "preview" and not doc["conflicts"])
    raise typer.Exit(0 if done else 1)


def _refuse(target: str, code: str, message: str, json_output: bool, preview=None) -> NoReturn:
    """End with a rejected result that proposes nothing."""
    _finish(_document(target, "rejected", preview, proposed=False, conflicts=[_conflict(code, message)]), json_output)


# ----------------------------------------------------------------------- the command


def _interactive() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _confirmed() -> bool:
    try:
        return typer.confirm("Apply these changes?", default=False)
    except (typer.Abort, EOFError):
        typer.echo()
        return False


def _prepare(target: str, clock: datetime | None = None):
    from . import naming  # Loaded on use so that other commands start fast.

    return naming.prepare_naming_migration(find_repository(), target, clock=clock)


def _apply(preview):
    from . import naming

    return naming.apply_naming_migration(preview)


def _conclude(target: str, result, json_output: bool) -> NoReturn:
    done = result.status in ("applied", "noop")
    if not json_output and result.transaction_status == "applied":
        _print(_document(target, "applied", result.preview))
    _finish(
        _document(
            target, result.status, result.preview,
            conflicts=None if done else (result.conflicts or None), recovery=result.recovery,
        ),
        json_output,
    )


def _apply_token(target: str, text: str, json_output: bool) -> NoReturn:
    try:
        token = _decode(text)
    except _BadToken as exc:
        _refuse(target, "invalid-token", f"The approval token is not valid. {exc}", json_output)
    if token["target_scheme"] != target:
        _refuse(target, "target-mismatch", "The approval token was issued for the other naming scheme.", json_output)
    try:
        preview = _prepare(target, token["clock"])
    except (OSError, ValueError) as exc:
        _refuse(target, "invalid-project", str(exc), json_output)
    roots = (str(preview.repository_root), str(preview.workspace_root), preview.project_id)
    if roots != tuple(token[key] for key in ("repository_root", "workspace_root", "project_id")):
        _refuse(target, "root-mismatch", "The approval token belongs to another repository or workspace.",
                json_output, preview)
    if (preview.snapshot_digest, preview.mapping_digest) != (token["snapshot_digest"], token["mapping_digest"]):
        stale = _conflict("stale-preview", "The project changed after the preview. Request a new preview.")
        _finish(_document(target, "rejected", preview, proposed=False, conflicts=[stale, *preview.conflicts]),
                json_output)
    _conclude(target, _apply(preview), json_output)


def _preview(target: str, dry_run: bool, json_output: bool) -> NoReturn:
    try:
        preview = _prepare(target)
    except (OSError, ValueError) as exc:
        _refuse(target, "invalid-project", str(exc), json_output)
    doc = _describe(target, preview)
    ready = doc["status"] == "preview" and not doc["conflicts"]
    if json_output or dry_run or not ready:
        _finish(doc, json_output, ("Dry run: no files were changed.",) if dry_run and ready else ())
    if not _interactive():
        _finish(doc, False, _guidance(target))
    _print(doc)
    if not _confirmed():
        _finish(_document(target, "cancelled", preview), False)
    _conclude(target, _apply(preview), False)


def register(app: typer.Typer) -> None:
    @app.command("migrate-naming")
    def migrate_naming(
        ctx: typer.Context,
        feature_numbering: Scheme = typer.Option(..., "--feature-numbering", help="Target naming scheme."),
        dry_run: bool = typer.Option(False, "--dry-run", help="Show the preview. Never ask for approval."),
        apply_token: str | None = typer.Option(
            None, "--apply", metavar="APPROVAL_TOKEN", help="Apply the preview that this token approves.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Print one JSON object. Without --apply, only preview."),
    ) -> None:
        """Convert feature directory names between sequential and timestamp prefixes.

        Shows a preview first. Applies it only after approval in a terminal or with an approval token.
        """
        if dry_run and apply_token is not None:
            ctx.fail("--dry-run and --apply are mutually exclusive.")
        target = feature_numbering.value
        if apply_token is not None:
            _apply_token(target, apply_token, json_output)
        _preview(target, dry_run, json_output)
