"""Merge CLI transport and approval binding against real disposable artifact sets."""
from __future__ import annotations

import base64
import copy
import errno
import json
import os
import stat
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner


SOURCE_TEXT = b"# Source\r\n\r\nPreserve the source requirement.\r\n"
DESTINATION_TEXT = b"# Destination\n\nPreserve the destination requirement.\n"
COMBINED_TEXT = b"# Combined\n\nPreserve both requirements.\n"


def put(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def state(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None,
            stat.S_IMODE(path.stat().st_mode),
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    }


@pytest.fixture
def cli():
    from specify_cli.project.command_merge_specs import register

    app = typer.Typer()
    register(app)
    # Keep this boundary independent of project-wide registration, owned by T016.
    @app.command("unrelated")
    def unrelated():
        pass

    return lambda *args, input=None: CliRunner().invoke(app, ["merge-specs", *args], input=input)


@pytest.fixture
def world(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("SPECIFY_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    source, destination = tmp_path / "source", tmp_path / "destination"
    spec = put(source, "002-new/spec.md", SOURCE_TEXT)
    spec.chmod(0o640)
    os.utime(spec, ns=(0, 0))
    put(source, "002-new/custom.bin", b"\x00\xff\xfeopaque")
    put(destination, "001-existing/spec.md", DESTINATION_TEXT)
    principles = put(tmp_path, "principles.md", b"PRIVATE-GOVERNANCE-CONTEXT")
    return {
        "source": str(source), "source_kind": "set",
        "destination": str(destination), "destination_kind": "set",
        "destination_numbering": "sequential", "destination_principles": str(principles),
    }


def selectors(world):
    return [value for key, value in world.items() for value in ("--" + key.replace("_", "-"), value)]


def document(result):
    data = json.loads(result.stdout)
    assert isinstance(data, dict), result.output
    assert "Traceback" not in result.stdout + result.stderr
    assert "PRIVATE-GOVERNANCE-CONTEXT" not in result.stdout + result.stderr
    return data


def proposal(cli, world, *, revision=False):
    inspected = cli(*selectors(world), "--json")
    assert inspected.exit_code == 0, inspected.output
    inspection = document(inspected)
    return {
        "selections": inspection["selections"],
        "snapshot_digest": inspection["snapshot_digest"],
        "relationship": {"relationship": "same_project", "evidence": "The maintainer confirmed fixture copies of one project."},
        "correspondences": [{
            "source_feature": "002-new",
            "destination_feature": "001-existing" if revision else "002-new",
            "relation": "revision" if revision else "independent",
            "evidence": "The maintainer confirmed the feature relationship.",
        }],
        "artifact_decisions": ([{
            "path": "001-existing/spec.md", "resolution": "replace",
            "content": base64.b64encode(COMBINED_TEXT).decode("ascii"),
            "mode": 0o640, "mtime_ns": 0,
        }, {
            "path": "001-existing/custom.bin", "source_path": "002-new/custom.bin",
            "resolution": "copy_source",
        }] if revision else []),
        "dependent_features": [],
    }


def preview(cli, decisions):
    result = cli("--proposal", "-", "--json", input=json.dumps(decisions))
    assert result.exit_code == 0, result.output
    data = document(result)
    assert not data["conflicts"], data
    return data


def apply(cli, replay):
    return cli("--apply", "--json", input=json.dumps(replay))


def test_inspect_and_proposal_are_read_only_then_replay_copies_exact_bytes(cli, world):
    source, destination = Path(world["source"]), Path(world["destination"])
    before_source, before_destination = state(source), state(destination)
    decisions = proposal(cli, world)
    reviewed = preview(cli, decisions)
    assert state(source) == before_source
    assert state(destination) == before_destination
    replay = reviewed["replay_inputs"]
    assert replay["snapshot_digest"] == decisions["snapshot_digest"]
    assert replay["proposal_digest"] == reviewed["proposal_digest"]
    assert not {"operations", "inventory", "source", "destination"} & replay.keys()
    assert replay["correspondences"][0]["destination_feature"] == "002-new"
    assert replay["temporary_resources"] == reviewed["temporary_resources"]

    result = apply(cli, replay)
    assert result.exit_code == 0, result.output
    outcome = document(result)
    assert outcome["transfer"] == "completed"
    assert outcome["data_outcome"] == "applied"
    assert outcome["cleanup_required"] is False
    assert state(source) == before_source
    assert (destination / "001-existing/spec.md").read_bytes() == DESTINATION_TEXT
    assert (destination / "002-new/spec.md").read_bytes() == SOURCE_TEXT
    assert (destination / "002-new/custom.bin").read_bytes() == b"\x00\xff\xfeopaque"
    assert (destination / "002-new/spec.md").stat().st_mtime_ns == 0
    assert stat.S_IMODE((destination / "002-new/spec.md").stat().st_mode) == 0o640
    assert not any(path.name.startswith(".merge-specs-recovery-") for path in destination.iterdir())
    assert not (destination / ".merge-specs.lock").exists()


def test_authored_base64_replacement_and_zero_metadata_replay_exactly(cli, world):
    decisions = proposal(cli, world, revision=True)
    reviewed = preview(cli, decisions)
    result = apply(cli, reviewed["replay_inputs"])
    assert result.exit_code == 0, result.output
    assert document(result)["data_outcome"] == "applied"
    artifact = Path(world["destination"]) / "001-existing/spec.md"
    assert artifact.read_bytes() == COMBINED_TEXT
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o640
    assert artifact.stat().st_mtime_ns == 0


def test_collision_allocation_becomes_an_explicit_replay_name(cli, world):
    destination = Path(world["destination"])
    put(destination, "002-reserved/spec.md", b"# Retain the occupied prefix\n")
    decisions = proposal(cli, world)
    del decisions["correspondences"][0]["destination_feature"]
    reviewed = preview(cli, decisions)
    replay = reviewed["replay_inputs"]
    assert replay["correspondences"][0]["destination_feature"] == "003-new"
    resources = copy.deepcopy(replay["temporary_resources"])
    result = apply(cli, replay)
    assert result.exit_code == 0, result.output
    assert document(result)["data_outcome"] == "applied"
    assert (destination / "003-new/spec.md").read_bytes() == SOURCE_TEXT
    assert (destination / "002-reserved/spec.md").read_bytes() == b"# Retain the occupied prefix\n"
    assert not (destination / "002-new").exists()
    assert not (destination / resources["lock"]).exists()
    assert not (destination / resources["recovery_directory"]).exists()


@pytest.mark.parametrize("phase", ["proposal", "apply"])
def test_original_snapshot_refuses_same_size_same_time_changes(cli, world, phase):
    decisions = proposal(cli, world)
    payload = preview(cli, decisions)["replay_inputs"] if phase == "apply" else decisions
    artifact = Path(world["source"]) / "002-new/spec.md"
    saved = artifact.stat()
    artifact.write_bytes(SOURCE_TEXT.replace(b"Source", b"Edited"))
    os.utime(artifact, ns=(saved.st_atime_ns, saved.st_mtime_ns))
    before = state(Path(world["destination"]))
    flags = ["--apply"] if phase == "apply" else ["--proposal", "-"]
    result = cli(*flags, "--json", input=json.dumps(payload))
    assert result.exit_code != 0
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


def test_apply_compares_original_proposal_digest_before_writes(cli, world):
    replay = preview(cli, proposal(cli, world, revision=True))["replay_inputs"]
    replay["artifact_decisions"][0]["content"] = base64.b64encode(b"Unapproved replacement").decode("ascii")
    before = state(Path(world["destination"]))
    result = apply(cli, replay)
    assert result.exit_code != 0
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


@pytest.mark.parametrize("raw", [
    "", "{", "[]", "null", "true", "42", "{}{}", '{"snapshot_digest":NaN}',
    '{"snapshot_digest":Infinity}', '{"snapshot_digest":"a","snapshot_digest":"b"}',
])
@pytest.mark.parametrize("mode", [["--proposal", "-"], ["--apply"]])
def test_invalid_json_refuses_without_traceback_or_writes(cli, world, raw, mode):
    before = state(Path(world["destination"]))
    result = cli(*mode, "--json", input=raw)
    assert result.exit_code != 0
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


@pytest.mark.parametrize("field,value", [
    ("operations", []), ("inventory", {}), ("files", {}), ("source", {}),
    ("snapshot_digest", None), ("snapshot_digest", "fresh"),
    ("relationship", {"relationship": "same_project"}),
    ("artifact_decisions", [{"path": "../escape", "resolution": "replace", "content": "YQ=="}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "replace", "content": [65]}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "replace", "content": "YQ==\n"}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "delete"}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "replace", "content": "YQ==", "mode": True}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "replace", "content": "YQ==", "mode": 0o4755}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "replace", "content": "YQ==", "mtime_ns": False}]),
    ("artifact_decisions", [{"path": "001-existing/spec.md", "resolution": "copy_source", "source_path": "002-new/spec.md", "replacements": [{"old": "", "new": "x"}]}]),
])
def test_untrusted_decisions_refuse_without_writes(cli, world, field, value):
    decisions = proposal(cli, world)
    decisions[field] = value
    before = state(Path(world["destination"]))
    result = cli("--proposal", "-", "--json", input=json.dumps(decisions))
    assert result.exit_code != 0
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


@pytest.mark.parametrize("field", ["snapshot_digest", "proposal_digest", "temporary_resources"])
def test_apply_requires_original_digests_and_fixed_resources(cli, world, field):
    replay = copy.deepcopy(preview(cli, proposal(cli, world))["replay_inputs"])
    del replay[field]
    before = state(Path(world["destination"]))
    result = apply(cli, replay)
    assert result.exit_code != 0
    document(result)
    assert state(Path(world["destination"])) == before


@pytest.mark.parametrize("flags", [
    ["--proposal", "-", "--apply"], ["--proposal", "payload.json"],
    ["--proposal", "-", "--source-kind", "repository"],
    ["--apply", "--destination-kind", "repository"],
    ["--apply", "--destination-numbering", "sequential"],
    ["--source-kind", "invalid"], ["--destination-kind", "branch"],
    ["--destination-numbering", "invalid"],
    ["--source-kind", "set"], ["--source-kind", "branch"],
    ["--source", "/selected", "--source-kind", "branch"],
    ["--source", "/selected", "--source-branch", "main"],
    ["--destination-kind", "set"],
    ["--destination-numbering", "sequential"],
    ["--destination-principles", "/principles"],
    ["--destination-context", "/context"],
    ["--unknown-option"], ["--source"], ["--proposal"],
    ["--unknown-option=value"], ["--apply=value"], ["unexpected-argument"],
])
def test_conflicting_modes_selector_overrides_and_enum_values_refuse(cli, world, flags):
    before = state(Path(world["destination"]))
    result = cli(*flags, "--json", input="{}")
    assert result.exit_code != 0
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


def test_human_preview_shows_full_artifact_difference_without_private_context(cli, world):
    decisions = proposal(cli, world, revision=True)
    result = cli("--proposal", "-", input=json.dumps(decisions))
    assert result.exit_code == 0, result.output
    assert "Preserve the destination requirement." in result.stdout
    assert "Preserve both requirements." in result.stdout
    assert "001-existing/spec.md" in result.stdout
    assert "PRIVATE-GOVERNANCE-CONTEXT" not in result.stdout + result.stderr
    assert (Path(world["destination"]) / "001-existing/spec.md").read_bytes() == DESTINATION_TEXT


def test_cleanup_failure_returns_nonzero_without_hiding_applied_data(cli, world, monkeypatch):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    lock = Path(world["destination"]) / replay["temporary_resources"]["lock"]
    original_unlink = os.unlink

    def fail_lock_removal(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError("The fixture denied lock removal.")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_lock_removal)
    result = apply(cli, replay)
    assert result.exit_code != 0
    outcome = document(result)
    assert outcome["data_outcome"] == "applied"
    assert outcome["cleanup_required"] is True
    assert "002-new/spec.md" in outcome["changed_paths"]
    assert outcome["remaining_operations"]
    assert (Path(world["destination"]) / "002-new/spec.md").read_bytes() == SOURCE_TEXT
    assert result.stderr


@pytest.mark.parametrize("field", [
    "selections", "snapshot_digest", "relationship", "correspondences", "artifact_decisions", "dependent_features",
])
def test_proposal_requires_complete_compact_decisions(cli, world, field):
    decisions = proposal(cli, world)
    del decisions[field]
    before = state(Path(world["destination"]))
    result = cli("--proposal", "-", "--json", input=json.dumps(decisions))
    assert result.exit_code == 1
    assert document(result)["error"]
    assert result.stderr
    assert state(Path(world["destination"])) == before


def test_apply_rejects_full_preview_instead_of_replay_inputs(cli, world):
    reviewed = preview(cli, proposal(cli, world))
    before = state(Path(world["destination"]))
    result = apply(cli, reviewed)
    assert result.exit_code == 1
    assert document(result)["error"]
    assert result.stderr
    assert state(Path(world["destination"])) == before


@pytest.mark.parametrize("field", ["snapshot_digest", "proposal_digest"])
def test_apply_rejects_forged_original_digest(cli, world, field):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    replay[field] = "0" * 64
    before = state(Path(world["destination"]))
    result = apply(cli, replay)
    assert result.exit_code == 1
    assert document(result)["error"]
    assert result.stderr
    assert state(Path(world["destination"])) == before


def test_apply_rejects_changed_resource_paths_before_writes(cli, world):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    replay["temporary_resources"]["recovery_directory"] = ".merge-specs-recovery-11111111111141118111111111111111"
    before = state(Path(world["destination"]))
    result = apply(cli, replay)
    assert result.exit_code == 1
    document(result)
    assert result.stderr
    assert state(Path(world["destination"])) == before


def test_apply_rejects_changed_embedded_destination(cli, world, tmp_path):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    other = tmp_path / "other-destination"
    put(other, "001-existing/spec.md", DESTINATION_TEXT)
    replay["selections"]["destination"] = str(other)
    before = state(Path(world["destination"])), state(other)
    result = apply(cli, replay)
    assert result.exit_code == 1
    assert document(result)["error"]
    assert result.stderr
    assert (state(Path(world["destination"])), state(other)) == before


def test_replaced_recovery_directory_returns_nonzero_and_preserves_applied_data(cli, world, tmp_path, monkeypatch):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    source, destination = Path(world["source"]), Path(world["destination"])
    recovery = destination / replay["temporary_resources"]["recovery_directory"]
    displaced = tmp_path / "owned-recovery"
    foreign_bytes = b"Foreign journal\xff\x00\r\n"
    source_before = state(source)
    retained_before = state(destination / "001-existing")
    real_utime = os.utime

    def replace_recovery(path, *args, **kwargs):
        result = real_utime(path, *args, **kwargs)
        if Path(path) == destination / "002-new" and not displaced.exists():
            recovery.rename(displaced)
            recovery.mkdir()
            put(recovery, "journal.json", foreign_bytes)
        return result

    monkeypatch.setattr(os, "utime", replace_recovery)
    result = apply(cli, replay)
    outcome = document(result)
    assert result.exit_code == 1
    assert outcome["transfer"] == "completed"
    assert outcome["data_outcome"] == "applied"
    assert outcome["cleanup_required"] is True
    assert outcome["review"] == "incomplete_review"
    assert outcome["originals"] == []
    assert outcome["recovery_directory"] is None
    assert any(row["operation"] == "inspect-recovery-directory" and row["path"] == str(recovery)
               for row in outcome["remaining_operations"])
    assert (recovery / "journal.json").read_bytes() == foreign_bytes
    assert sorted(path.name for path in recovery.iterdir()) == ["journal.json"]
    assert (destination / "002-new/spec.md").read_bytes() == SOURCE_TEXT
    assert (destination / "002-new/custom.bin").read_bytes() == b"\x00\xff\xfeopaque"
    assert state(source) == source_before
    assert state(destination / "001-existing") == retained_before
    assert not (destination / ".merge-specs.lock").exists()


def test_lock_identity_failure_reports_retained_lock_without_changing_artifacts(cli, world, monkeypatch):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    source, destination = Path(world["source"]), Path(world["destination"])
    lock = destination / ".merge-specs.lock"
    source_before = state(source)
    retained_before = state(destination / "001-existing")
    real_open, real_fstat = os.open, os.fstat
    held = {}

    def open_lock(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == lock:
            held["descriptor"] = descriptor
        return descriptor

    def fail_identity(descriptor):
        if descriptor == held.get("descriptor"):
            raise OSError("Injected lock identity capture failure.")
        return real_fstat(descriptor)

    monkeypatch.setattr(os, "open", open_lock)
    monkeypatch.setattr(os, "fstat", fail_identity)
    result = apply(cli, replay)
    try:
        real_fstat(held["descriptor"])
    except OSError as exc:
        closed = exc.errno == errno.EBADF
    else:
        closed = False
        os.close(held["descriptor"])
    outcome = document(result)
    assert result.exit_code == 1
    assert closed
    assert outcome["data_outcome"] == "unchanged"
    assert outcome["cleanup_required"] is True
    assert outcome["originals"] == []
    assert outcome["recovery_directory"] is None
    assert [(row["operation"], row["path"]) for row in outcome["remaining_operations"]] == [
        ("inspect-lock", str(lock)),
    ]
    assert state(source) == source_before
    assert state(destination / "001-existing") == retained_before
    assert lock.read_bytes() == b""
    assert not (destination / "002-new").exists()
    assert not list(destination.glob(".merge-specs-recovery-*"))


def test_rollback_recovery_replacement_preserves_foreign_payloads_and_reports_pending_actions(cli, world, tmp_path, monkeypatch):
    replay = preview(cli, proposal(cli, world, revision=True))["replay_inputs"]
    source, destination = Path(world["source"]), Path(world["destination"])
    target = destination / "001-existing/spec.md"
    recovery = destination / replay["temporary_resources"]["recovery_directory"]
    displaced = tmp_path / "owned-recovery"
    source_before = state(source)
    real_replace, real_utime = os.replace, os.utime
    failed = False
    foreign_state = {}

    def replace(source_path, destination_path):
        nonlocal failed
        result = real_replace(source_path, destination_path)
        if Path(destination_path) == target and not failed:
            failed = True
            raise OSError("Injected application failure after replacement.")
        return result

    def utime(path, *args, **kwargs):
        result = real_utime(path, *args, **kwargs)
        if failed and Path(path).parent == recovery / "staged" and not displaced.exists():
            staged_name = Path(path).name
            recovery.rename(displaced)
            put(recovery, f"staged/{staged_name}", b"Foreign staged payload\x00\xff\r\n")
            put(recovery, "journal.json", b"Foreign journal\xff\r\n")
            foreign_state.update(state(recovery))
        return result

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "utime", utime)
    result = apply(cli, replay)
    outcome = document(result)

    assert result.exit_code == 1
    assert state(recovery) == foreign_state
    assert target.read_bytes() == COMBINED_TEXT
    assert state(source) == source_before
    assert outcome["transfer"] == "failed"
    assert outcome["data_outcome"] == "recovery_required"
    assert outcome["cleanup_required"] is True
    assert outcome["review"] == "incomplete_review"
    assert outcome["recovery_directory"] is None
    assert outcome["originals"] == []
    assert [(row["operation"], row["path"]) for row in outcome["remaining_operations"]] == [
        ("restore-file", "001-existing/spec.md"), ("inspect-recovery-directory", str(recovery)),
    ]
    journal = json.loads((displaced / "journal.json").read_text())
    assert [(row["path"], (displaced / row["original"]).read_bytes())
            for row in journal["operations"] if "original" in row] == [
        ("001-existing/spec.md", DESTINATION_TEXT),
    ]
    assert not (destination / "001-existing/custom.bin").exists()
    assert not (destination / "001-existing/.merge-specs.json").exists()
    assert not (destination / ".merge-specs.lock").exists()


def test_created_directory_identity_failure_reports_nonzero_pending_inspection(cli, world, monkeypatch):
    replay = preview(cli, proposal(cli, world))["replay_inputs"]
    source, destination = Path(world["source"]), Path(world["destination"])
    target = destination / "002-new"
    source_before, retained_before = state(source), state(destination / "001-existing")
    real_mkdir, real_lstat = Path.mkdir, Path.lstat
    created, failed = False, False

    def mkdir(path, *args, **kwargs):
        nonlocal created
        result = real_mkdir(path, *args, **kwargs)
        if path == target:
            created = True
        return result

    def lstat(path, *args, **kwargs):
        nonlocal failed
        if path == target and created and not failed:
            failed = True
            raise OSError("Injected created-directory identity failure.")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "lstat", lstat)
    result = apply(cli, replay)
    outcome = document(result)

    assert result.exit_code == 1
    assert outcome["transfer"] == "failed"
    assert outcome["data_outcome"] == "recovery_required"
    assert outcome["review"] == "incomplete_review"
    assert [(row["operation"], row["path"]) for row in outcome["remaining_operations"]] == [
        ("inspect-directory", "002-new"),
    ]
    assert outcome["originals"] == []
    assert target.is_dir() and list(target.iterdir()) == []
    assert state(source) == source_before
    assert state(destination / "001-existing") == retained_before
    assert not (destination / ".merge-specs.lock").exists()
    recovery = Path(outcome["recovery_directory"])
    journal = json.loads((recovery / "journal.json").read_text())
    assert journal["remaining_operations"] == outcome["remaining_operations"]
