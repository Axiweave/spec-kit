"""The specify project merge-specs command and compact JSON replay boundary."""
from __future__ import annotations

import json
import re
import sys
from typing import NoReturn

import typer
from click import UsageError
from typer.core import TyperCommand


_SELECTORS = frozenset({
    "source", "source_kind", "source_branch", "destination", "destination_kind",
    "destination_numbering", "destination_principles", "destination_context",
})
_PROPOSAL_FIELDS = frozenset({
    "selections", "snapshot_digest", "relationship", "correspondences",
    "artifact_decisions", "dependent_features",
})
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _say(message: str, *, err: bool = False) -> None:
    typer.echo("".join(char if char.isprintable() else char.encode("unicode_escape").decode("ascii")
                       for char in message), err=err)


def _refuse(error: Exception, json_output: bool) -> NoReturn:
    message = str(error).strip() or error.__class__.__name__
    if json_output:
        typer.echo(json.dumps({"error": message}, ensure_ascii=False))
    _say(f"Error: {message}", err=True)
    raise typer.Exit(1) from None


class _MergeSpecsCommand(TyperCommand):
    """Keep argument parse failures on the command's JSON transport."""

    def make_context(self, info_name, args, parent=None, **extra):
        json_output = "--json" in args
        try:
            valued_options = {"--" + key.replace("_", "-") for key in _SELECTORS} | {"--proposal"}
            for index, argument in enumerate(args):
                if argument in valued_options and index + 1 < len(args) and args[index + 1].startswith("--"):
                    raise UsageError(f"Option {argument} requires a value. Use '=' for a value that starts with '--'.")
            return super().make_context(info_name, args, parent=parent, **extra)
        except Exception as error:
            # Click and Typer usage errors share this public exit-code contract.
            if json_output and getattr(error, "exit_code", None) == 2:
                _refuse(error, True)
            raise


def _validate_selectors(selection: dict, *, replay: bool = False) -> None:
    if not isinstance(selection, dict) or set(selection) - _SELECTORS:
        raise ValueError("Selections must contain only supported selector fields.")
    for key, value in selection.items():
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"The {key} selector must contain a nonempty string.")
    source_kind = selection.get("source_kind")
    destination_kind = selection.get("destination_kind")
    if source_kind is not None and source_kind not in ("repository", "set", "branch"):
        raise ValueError("Source kind must be repository, set, or branch.")
    if destination_kind is not None and destination_kind not in ("repository", "set"):
        raise ValueError("Destination kind must be repository or set.")
    numbering = selection.get("destination_numbering")
    if numbering is not None and numbering not in ("sequential", "timestamp"):
        raise ValueError("Destination numbering must be sequential or timestamp.")
    if selection.get("source") is None and (source_kind is not None or selection.get("source_branch") is not None):
        raise ValueError("Source kind and branch selectors require an explicit source path.")
    if source_kind == "branch" and selection.get("source_branch") is None:
        raise ValueError("A branch source requires an explicit source branch.")
    if selection.get("source_branch") is not None and source_kind != "branch":
        raise ValueError("A source branch requires source kind branch.")
    if destination_kind == "set" and selection.get("destination") is None:
        raise ValueError("A raw destination set requires an explicit destination path.")
    raw_context = ("destination_numbering", "destination_principles", "destination_context")
    if destination_kind != "set" and any(selection.get(key) is not None for key in raw_context):
        raise ValueError("Destination numbering, principles, and context flags require destination kind set.")
    if replay:
        from pathlib import Path

        if selection.get("destination") is None or destination_kind is None:
            raise ValueError("Replay selections require the resolved destination path and kind.")
        if selection.get("source") is not None and source_kind is None:
            raise ValueError("Replay selections require an explicit source kind.")
        for key in ("source", "destination", "destination_principles", "destination_context"):
            if selection.get(key) is not None and not Path(selection[key]).is_absolute():
                raise ValueError(f"Replay selections require an absolute {key} path.")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON contains a duplicate field: {key}.")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON does not permit {value}.")


def _read_payload(apply: bool) -> dict:
    try:
        payload = json.loads(sys.stdin.read(), object_pairs_hook=_json_object, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
        raise ValueError("Read one valid JSON object from stdin.") from error
    if not isinstance(payload, dict):
        raise ValueError("The proposal or replay input must contain a JSON object.")
    required = _PROPOSAL_FIELDS | ({"proposal_digest", "temporary_resources"} if apply else set())
    allowed = required | {"temporary_resources"}
    if set(payload) - allowed:
        raise ValueError("Use compact decisions or replay inputs, not inventories or backend-generated preview fields.")
    missing = required - set(payload)
    if missing:
        raise ValueError(f"The input lacks required fields: {', '.join(sorted(missing))}.")
    for key in ("snapshot_digest", "proposal_digest") if apply else ("snapshot_digest",):
        if not isinstance(payload[key], str) or not _DIGEST.fullmatch(payload[key]):
            raise ValueError(f"The {key} field must contain the original SHA-256 digest.")
    _validate_selectors(payload["selections"], replay=True)
    if not isinstance(payload["relationship"], dict):
        raise ValueError("The relationship decision must contain an object.")
    for key in ("correspondences", "artifact_decisions", "dependent_features"):
        if not isinstance(payload[key], list):
            raise ValueError(f"The {key} field must contain an array.")
    if "temporary_resources" in payload and not isinstance(payload["temporary_resources"], dict):
        raise ValueError("Temporary resources must contain the exact approved resource paths.")
    return payload


def _diagnostics(document: dict) -> None:
    for item in (*document.get("notices", ()), *document.get("conflicts", ()), *document.get("messages", ())):
        if isinstance(item, dict):
            location = item.get("path")
            message = item.get("message") or item.get("reason") or item.get("code") or item.get("kind")
            if message:
                _say(f"{location}: {message}" if location else str(message), err=True)
        elif item:
            _say(str(item), err=True)


def _human(document: dict, mode: str) -> None:
    _say(f"Specification merge {mode}")
    if mode == "inspection":
        for role in ("source", "destination"):
            selected = document.get(role)
            if selected:
                _say(f"{role.capitalize()}: {selected.get('spec_root') or selected.get('repository_root')}")
                for feature in selected.get("features", ()):
                    _say(f"  {feature['path']}")
        _say(f"Transfer: {document.get('transfer', 'not_requested')}")
        return
    if mode == "preview":
        for correspondence in document.get("correspondences", ()):
            _say(f"Feature decision: {json.dumps(correspondence, ensure_ascii=False)}")
        for operation in document.get("operations", ()):
            _say(f"{operation.get('operation', operation.get('kind', 'write'))}: {operation.get('path', '.')}")
            for key in ("source_path", "mode", "mtime_ns"):
                if key in operation:
                    _say(f"  {key}: {operation[key]}")
            difference = operation.get("difference")
            if difference:
                for line in difference.splitlines():
                    _say(line)
        for key in ("affected_features", "dependent_features"):
            _say(f"{key}: {json.dumps(document.get(key, []), ensure_ascii=False)}")
        _say(f"Temporary resources: {json.dumps(document.get('temporary_resources', {}), ensure_ascii=False)}")
        _say(f"Proposal digest: {document.get('proposal_digest')}")
        return
    for key in ("transfer", "data_outcome", "cleanup_required", "recovery_directory"):
        if key in document:
            _say(f"{key}: {document[key]}")
    for key in ("changed_paths", "remaining_operations", "originals"):
        for item in document.get(key, ()):
            _say(f"{key}: {json.dumps(item, ensure_ascii=False)}")


def register(app: typer.Typer) -> None:
    @app.command("merge-specs", cls=_MergeSpecsCommand)
    def merge_specs(
        source: str | None = typer.Option(None, "--source", help="Source repository or artifact set path."),
        source_kind: str | None = typer.Option(None, "--source-kind", help="repository, set, or branch."),
        source_branch: str | None = typer.Option(None, "--source-branch", help="Explicit source Git reference."),
        destination: str | None = typer.Option(None, "--destination", help="Destination repository or artifact set path."),
        destination_kind: str | None = typer.Option(None, "--destination-kind", help="repository or set."),
        destination_numbering: str | None = typer.Option(None, "--destination-numbering", help="Raw set numbering: sequential or timestamp."),
        destination_principles: str | None = typer.Option(None, "--destination-principles", help="Raw set governance file path."),
        destination_context: str | None = typer.Option(None, "--destination-context", help="Raw set code or worktree context path."),
        proposal: str | None = typer.Option(None, "--proposal", help="Use - to read proposal decisions from stdin."),
        apply: bool = typer.Option(False, "--apply", help="Apply approved compact replay inputs from stdin."),
        json_output: bool = typer.Option(False, "--json", help="Write exactly one JSON document to stdout."),
    ) -> None:
        """Inspect specification sets, preview decisions, or apply an approved replay."""
        selection = {
            "source": source, "source_kind": source_kind, "source_branch": source_branch,
            "destination": destination, "destination_kind": destination_kind,
            "destination_numbering": destination_numbering, "destination_principles": destination_principles,
            "destination_context": destination_context,
        }
        try:
            if proposal is not None and apply:
                raise ValueError("Proposal and application modes cannot be combined.")
            if proposal is not None and proposal != "-":
                raise ValueError("Use --proposal - to read decisions from stdin.")
            if (proposal is not None or apply) and any(value is not None for value in selection.values()):
                raise ValueError("Proposal and application modes cannot override embedded selections with selector flags.")
            from .spec_merge import inspect_spec_sets

            if proposal is None and not apply:
                _validate_selectors(selection)
                document = inspect_spec_sets(selection).to_json()
                mode = "inspection"
            else:
                from .spec_merge import apply_spec_merge, prepare_spec_merge

                payload = _read_payload(apply)
                inspection = inspect_spec_sets(payload["selections"])
                if inspection.to_json()["snapshot_digest"] != payload["snapshot_digest"]:
                    raise ValueError("The original snapshot is stale. Inspect the selected sets and prepare a new proposal.")
                preview = prepare_spec_merge(inspection, payload)
                document = preview.to_json()
                if document["snapshot_digest"] != payload["snapshot_digest"]:
                    raise ValueError("The prepared snapshot differs from the original snapshot. Prepare a new proposal.")
                mode = "preview"
                if apply:
                    if document["proposal_digest"] != payload["proposal_digest"]:
                        raise ValueError("The proposal differs from the approved proposal. Review and approve a new preview.")
                    if not document.get("conflicts"):
                        document = apply_spec_merge(preview).to_json()
                        mode = "application"
        except (OSError, ValueError, TypeError, RecursionError) as error:
            _refuse(error, json_output)
        if json_output:
            typer.echo(json.dumps(document, ensure_ascii=False))
        else:
            _human(document, mode)
        _diagnostics(document)
        failed = bool(document.get("conflicts"))
        if mode == "application":
            failed = failed or document.get("transfer") not in ("completed", "not_needed", "not_requested", "cancelled")
            failed = failed or bool(document.get("cleanup_required")) or bool(document.get("remaining_operations"))
            failed = failed or document.get("data_outcome") in ("restored", "recovery_required")
        if failed:
            _say("The merge did not complete. Inspect the result for conflicts or remaining recovery and cleanup actions.", err=True)
            raise typer.Exit(1)
