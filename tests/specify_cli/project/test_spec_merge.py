"""Read-only set inspection, availability, confinement, and stale-input baselines."""
from __future__ import annotations

import base64
import dataclasses
import errno
import json
import os
import random
import stat
from pathlib import Path
from uuid import uuid4

import pytest

SEED = 1539


def put(root: Path, name: str, content: bytes | str = b"") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def state(root: Path):
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes() if path.is_file() else None,
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_mtime_ns,
        )
        for path in [root, *root.rglob("*")]
    }


@pytest.fixture
def merge():
    from specify_cli.project import spec_merge

    return spec_merge


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    return tmp_path


def raw(source: Path | None, destination: Path, **fields):
    return {
        **({"source": str(source), "source_kind": "set"} if source else {}),
        "destination": str(destination),
        "destination_kind": "set",
        **fields,
    }


def external(root: Path, name: str, workspace: Path, identity: str):
    repo = root / name
    put(repo, ".specify/project.json", json.dumps({"schema_version": 1, "project_id": identity, "storage": "external"}))
    put(workspace, ".specify/workspace.json", json.dumps({"schema_version": 1, "project_id": identity}))
    put(root / "data/specify/projects", f"{identity}.json", json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": "specs/999-do-not-select", "private": "context-secret"}))
    put(workspace, ".specify/init-options.json", '{"feature_numbering": "timestamp"}')
    put(workspace, ".specify/memory/constitution.md", "Destination principles")
    put(workspace, "specs/001-feature/spec.md", "# Specification")
    return repo


def test_inspection_captures_bytes_without_writes_or_private_json(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    content = b"\xef\xbb\xbf# Specification\r\n\x00opaque"
    file = put(source, "001-feature/spec.md", content)
    file.chmod(0o640)
    os.utime(file, ns=(0, 0))
    put(source, "001-feature/custom.bin", b"\xffprivate-artifact")
    put(destination, "002-other/spec.md", "# Other")
    principles = put(isolated, "principles.md", "private-principles")
    before = state(isolated)

    inspection = merge.inspect_spec_sets(raw(source, destination, destination_numbering="sequential", destination_principles=str(principles)))

    assert state(isolated) == before
    assert inspection.transfer_needed is True
    assert inspection.relationship["relationship"] == "unconfirmed"
    assert inspection.source.files["001-feature/spec.md"].content == content
    assert inspection.source.files["001-feature/spec.md"].mode == stat.S_IMODE(file.stat().st_mode)
    assert inspection.source.files["001-feature/spec.md"].mtime_ns == 0
    assert inspection.destination.numbering == "sequential"
    with pytest.raises(dataclasses.FrozenInstanceError):
        inspection.snapshot_digest = "changed"
    with pytest.raises(TypeError):
        inspection.source.files["replacement"] = None
    output = json.dumps(inspection.to_json())
    assert "private-artifact" not in output
    assert "private-principles" not in output
    assert "content" not in inspection.to_json()["source"]
    assert inspection.to_json()["snapshot_digest"] == inspection.snapshot_digest
    assert inspection.to_json()["selections"]["destination"] == str(destination.resolve())


def test_shared_canonical_roots_need_no_transfer(merge, isolated):
    root = isolated / "specs"
    put(root, "001-feature/spec.md", "# Feature")
    alias = isolated / "alias"
    alias.symlink_to(root, target_is_directory=True)
    inspection = merge.inspect_spec_sets(raw(alias, root))
    assert inspection.transfer_needed is False
    assert inspection.transfer == "not_needed"
    assert inspection.source.spec_root == inspection.destination.spec_root == root.resolve()


def test_real_external_locators_distinguish_separate_and_shared_sets(merge, isolated):
    identity = str(uuid4())
    workspace = isolated / "global/one"
    source = external(isolated, "source-repo", workspace, identity)
    destination = external(isolated, "destination-repo", workspace, identity)
    before = state(isolated)
    shared = merge.inspect_spec_sets({"source": str(source), "destination": str(destination)})
    assert shared.transfer_needed is False
    assert shared.relationship["relationship"] == "same_project"
    assert shared.destination.numbering == "timestamp"
    assert state(isolated) == before
    assert "context-secret" not in json.dumps(shared.to_json())

    other = external(isolated, "other-repo", isolated / "global/two", str(uuid4()))
    separate = merge.inspect_spec_sets({"source": str(source), "destination": str(other)})
    assert separate.transfer_needed is True
    assert separate.relationship["relationship"] == "unconfirmed"
    assert separate.source.spec_root != separate.destination.spec_root


def test_missing_external_mapping_refuses_without_local_fallback(merge, isolated):
    repo = isolated / "repo"
    put(repo, ".specify/project.json", json.dumps({"schema_version": 1, "project_id": str(uuid4()), "storage": "external"}))
    put(repo, "specs/001-local/spec.md", "Must not use this")
    before = state(isolated)
    with pytest.raises(ValueError, match="[Mm]apping"):
        merge.inspect_spec_sets({"destination": str(repo)})
    assert state(isolated) == before


def test_raw_destination_never_infers_ancestor_metadata(merge, isolated):
    workspace = isolated / "external"
    repo = external(isolated, "repository", workspace, str(uuid4()))
    inspection = merge.inspect_spec_sets(raw(None, workspace / "specs"))
    assert inspection.destination.project_id is None
    assert inspection.destination.repository_root is None
    assert inspection.destination.workspace_root is None
    assert inspection.destination.numbering is None
    assert inspection.destination.principles_path is None
    assert inspection.destination.destination_context is None
    assert inspection.destination.context_inputs == ()
    assert inspection.to_json()["review"] == "incomplete_review"
    assert repo.is_dir()


@pytest.mark.parametrize("kind", ["nested", "symlink", "fifo", "traversal"])
def test_unsafe_roots_and_descendants_refuse_without_writes(merge, isolated, kind):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source")
    put(destination, "001-feature/spec.md", "# Destination")
    if kind == "nested":
        destination = source / "child"
        destination.mkdir()
    elif kind == "symlink":
        (source / "001-feature/escape").symlink_to(destination, target_is_directory=True)
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("The platform has no FIFO support")
        os.mkfifo(source / "001-feature/special")
    else:
        source = source / ".." / "source"
    with pytest.raises(ValueError, match="[Uu]nsafe|[Nn]ested|[Tt]raversal|[Oo]verlap"):
        merge.inspect_spec_sets(raw(source, destination))
    assert not (destination / ".merge-specs.lock").exists()


def test_pending_resources_and_markers_are_not_semantic_artifacts(merge, isolated):
    destination = isolated / "destination"
    put(destination, "001-feature/spec.md", "# Feature")
    marker = put(destination, "001-feature/.merge-specs.json", b'{"private":"marker"}')
    put(destination, ".merge-specs.lock", "private-lock")
    put(destination, ".merge-specs-recovery-old/spec.md", "Not a feature")
    put(destination, ".merge-specs-recovery-old/originals/000000.bin", "Do not inspect")
    inspection = merge.inspect_spec_sets(raw(None, destination))
    selected = inspection.destination
    assert {feature.path for feature in selected.features} == {"001-feature"}
    assert selected.features[0].artifacts == ("001-feature/spec.md",)
    assert selected.markers["001-feature/.merge-specs.json"].content == marker.read_bytes()
    assert selected.pending_resources == (".merge-specs-recovery-old", ".merge-specs.lock")
    assert not any("000000" in path for path in selected.files)
    assert inspection.to_json()["review"] == "incomplete_review"
    digest = inspection.snapshot_digest
    marker.write_bytes(b'{"private":"changed"}')
    assert merge.inspect_spec_sets(raw(None, destination)).snapshot_digest != digest


def test_partial_planning_and_unreadable_spec_have_distinct_availability(merge, isolated, monkeypatch):
    destination = isolated / "destination"
    readable = put(destination, "001-feature/spec.md", "# Feature")
    unreadable = put(destination, "002-feature/spec.md", "# Unreadable")
    put(destination, "003-unplanned/notes.md", "Missing specification")
    principles = put(isolated, "principles.md", "Principles")
    real = Path.read_bytes

    def read(path):
        if path == unreadable:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    inspection = merge.inspect_spec_sets(raw(None, destination, destination_principles=str(principles)))
    features = {feature.path: feature for feature in inspection.destination.features}
    assert features["001-feature"].specification == "readable"
    assert features["001-feature"].plan == features["001-feature"].tasks == "missing"
    assert features["002-feature"].specification == "unavailable"
    assert features["003-unplanned"].specification == "missing"
    assert inspection.to_json()["review"] == "incomplete_review"
    assert readable.read_bytes() == b"# Feature"


def test_snapshot_binds_bytes_metadata_membership_and_used_context(merge, isolated):
    workspace = isolated / "workspace"
    repo = external(isolated, "repo", workspace, str(uuid4()))
    target = workspace / "specs/001-feature/spec.md"
    selection = {"destination": str(repo)}
    baseline = merge.inspect_spec_sets(selection).snapshot_digest
    info = target.stat()
    target.write_bytes(b"# Speciflcation")
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert merge.inspect_spec_sets(selection).snapshot_digest != baseline
    target.write_bytes(b"# Specification")
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    baseline = merge.inspect_spec_sets(selection).snapshot_digest
    target.chmod(0o444)
    assert merge.inspect_spec_sets(selection).snapshot_digest != baseline
    baseline = merge.inspect_spec_sets(selection).snapshot_digest
    (workspace / "specs/001-feature/empty").mkdir()
    assert merge.inspect_spec_sets(selection).snapshot_digest != baseline
    baseline = merge.inspect_spec_sets(selection).snapshot_digest
    put(workspace, ".specify/memory/constitution.md", "Changed principles")
    assert merge.inspect_spec_sets(selection).snapshot_digest != baseline
    baseline = merge.inspect_spec_sets(selection).snapshot_digest
    put(repo, ".specify/feature.json", '{"feature_directory":"unrelated"}')
    assert merge.inspect_spec_sets(selection).snapshot_digest == baseline


def test_specification_only_is_incomplete_planning_not_incomplete_review(merge, isolated):
    destination = isolated / "destination"
    put(destination, "001-feature/spec.md", "# Feature")
    principles = put(isolated, "principles.md", "Principles")
    inspection = merge.inspect_spec_sets(raw(None, destination, destination_principles=str(principles)))
    assert inspection.to_json()["review"] == "not_performed"
    assert inspection.to_json()["planning"] == [
        {"feature": "001-feature", "specification": "readable", "plan": "missing", "tasks": "missing", "complete": False}
    ]
    assert inspection.transfer == "not_requested"


def test_unavailable_root_refuses_and_raw_context_does_not_read_code(merge, isolated):
    destination = isolated / "destination"
    with pytest.raises(ValueError, match="[Uu]navailable|[Mm]issing"):
        merge.inspect_spec_sets(raw(None, destination))
    put(destination, "001-feature/spec.md", "# Feature")
    context = isolated / "code"
    put(context, "private.py", "Do not read code")
    inspection = merge.inspect_spec_sets(raw(None, destination, destination_context=str(context)))
    assert inspection.destination.destination_context == context.resolve()
    assert not any("private.py" in value.path for value in inspection.destination.context_inputs)


def test_seeded_reservations_include_directories_without_specifications(merge, isolated):
    rng = random.Random(SEED)
    for case in range(12):
        destination = isolated / f"case-{case}"
        names = sorted({f"{rng.randint(1, 999):03d}-feature-{i}" for i in range(rng.randint(1, 8))})
        for name in names:
            (destination / name).mkdir(parents=True)
        inspection = merge.inspect_spec_sets(raw(None, destination))
        expected = {int(name.split("-", 1)[0]) for name in names}
        assert set(inspection.destination.occupied_prefixes["sequential"]) == expected, f"seed={SEED} case={case} names={names!r}"
        assert set(inspection.destination.occupied_names["."]) == {name.casefold() for name in names}, f"seed={SEED} case={case} names={names!r}"


def test_selector_errors_are_explicit(merge, isolated):
    destination = isolated / "destination"
    put(destination, "001-feature/spec.md", "# Feature")
    with pytest.raises(ValueError, match="[Rr]epository|[Ss]pec Kit"):
        merge.inspect_spec_sets({"destination": str(destination)})
    with pytest.raises(ValueError, match="[Rr]aw|[Ss]et"):
        merge.inspect_spec_sets({"destination": str(destination), "destination_numbering": "sequential"})
    with pytest.raises(ValueError, match="[Nn]umbering|[Ss]cheme"):
        merge.inspect_spec_sets(raw(None, destination, destination_numbering="invalid"))
    with pytest.raises(ValueError, match="[Bb]ranch"):
        merge.inspect_spec_sets({"source": str(destination), "source_kind": "branch", "source_branch": "main", "destination": str(destination), "destination_kind": "set"})


def git(repository: Path, *arguments: str):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    ).stdout


def recorded_source(root: Path):
    repository = root / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    put(repository, "specs/001-feature/spec.md", "# Recorded specification\n")
    git(repository, "add", "specs")
    git(repository, "commit", "-m", "Create fixture artifacts")
    return repository


def test_branch_inspection_reads_recorded_objects_not_worktree(merge, isolated):
    repository = recorded_source(isolated)
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    put(repository, "specs/001-feature/spec.md", "# Uncommitted worktree version\n")
    put(repository, "specs/999-unrecorded/spec.md", "# Not in history\n")
    before = state(isolated)

    inspection = merge.inspect_spec_sets({
        **raw(repository, destination),
        "source_kind": "branch",
        "source_branch": "main",
    })

    assert inspection.source.spec_root is None
    assert inspection.source.files["001-feature/spec.md"].content == b"# Recorded specification\n"
    assert "999-unrecorded/spec.md" not in inspection.source.files
    assert inspection.source.commit == git(repository, "rev-parse", "main").decode().strip()
    assert inspection.source.branch_ref == "refs/heads/main"
    assert inspection.to_json()["source"]["spec_root"] is None
    assert state(isolated) == before


def test_branch_identity_never_matches_current_filesystem_set(merge, isolated):
    repository = recorded_source(isolated)
    inspection = merge.inspect_spec_sets({
        **raw(repository, repository / "specs"),
        "source_kind": "branch",
        "source_branch": "main",
    })
    assert inspection.transfer_needed is True
    assert inspection.source.spec_root is None


def test_selected_branch_ref_change_invalidates_the_snapshot(merge, isolated):
    repository = recorded_source(isolated)
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    selection = {**raw(repository, destination), "source_kind": "branch", "source_branch": "main"}
    original = merge.inspect_spec_sets(selection)
    put(repository, "specs/001-feature/spec.md", "# Later recorded version\n")
    git(repository, "add", "specs")
    git(repository, "commit", "-m", "Change fixture artifacts")
    changed = merge.inspect_spec_sets(selection)
    assert changed.snapshot_digest != original.snapshot_digest


def test_branch_symlink_artifact_refuses_without_git_mutation(merge, isolated):
    repository = recorded_source(isolated)
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    (repository / "specs/001-feature/link").symlink_to("spec.md")
    git(repository, "add", "specs")
    git(repository, "commit", "-m", "Record fixture symlink")
    before = state(isolated)
    with pytest.raises(ValueError, match="[Ss]ymlink|[Uu]nsafe|ordinary"):
        merge.inspect_spec_sets({
            **raw(repository, destination),
            "source_kind": "branch",
            "source_branch": "main",
        })
    assert state(isolated) == before


def test_tracking_reads_only_selected_artifacts_without_index_writes(merge, isolated):
    repository = recorded_source(isolated)
    put(repository, "specs/001-feature/untracked.bin", b"\x00opaque")
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    index = repository / ".git/index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    inspected = merge.inspect_spec_sets(raw(repository / "specs", destination))
    assert inspected.source.tracking["001-feature/spec.md"] == "tracked"
    assert inspected.source.tracking["001-feature/untracked.bin"] == "untracked"
    assert inspected.source.tracking_context["repository"] == str(repository.resolve())
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_missing_git_keeps_filesystem_inspection_available(merge, isolated, monkeypatch):
    import subprocess

    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Destination\n")

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Fixture Git is unavailable.")

    monkeypatch.setattr(subprocess, "run", unavailable)
    inspection = merge.inspect_spec_sets(raw(source, destination))
    assert inspection.source.tracking["001-feature/spec.md"] == "unknown"
    assert inspection.transfer_needed is True


def test_custom_nested_specification_filename_remains_an_artifact(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Feature\n")
    put(source, "001-feature/contracts/spec.md", "# Custom contract\n")
    put(destination, "002-retained/spec.md", "# Destination\n")
    inspection = merge.inspect_spec_sets(raw(source, destination))
    assert [feature.path for feature in inspection.source.features] == ["001-feature"]
    assert "001-feature/contracts/spec.md" in inspection.source.features[0].artifacts


def approved(inspection, correspondences, **fields):
    return {
        "snapshot_digest": inspection.snapshot_digest,
        "relationship": {"relationship": "same_project", "evidence": "The maintainer confirmed one project."},
        "correspondences": correspondences,
        "artifact_decisions": [],
        "dependent_features": [],
        **fields,
    }


def test_approved_additive_transfer_preserves_inputs_and_replays_compactly(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    copied = put(source, "001-import/spec.md", b"\xef\xbb\xbf# Imported\r\n")
    copied.chmod(0o640)
    os.utime(copied, ns=(0, 0))
    put(source, "001-import/custom.bin", b"\xff\x00opaque")
    retained = put(destination, "002-retained/spec.md", "# Retained\n")
    source_before, retained_before = state(source), state(destination / "002-retained")
    inspection = merge.inspect_spec_sets(raw(source, destination, destination_numbering="sequential"))
    destination_before = state(destination)

    preview = merge.prepare_spec_merge(
        inspection,
        approved(inspection, [{"source_feature": "001-import", "relation": "independent"}]),
    )

    assert not preview.conflicts
    assert state(source) == source_before
    assert state(destination) == destination_before
    public = preview.to_json()
    assert public["temporary_resources"]["lock"] == ".merge-specs.lock"
    replay = public["replay_inputs"]
    assert replay["snapshot_digest"] == inspection.snapshot_digest
    assert replay["proposal_digest"] == public["proposal_digest"]
    assert not {"operations", "files", "directories", "inventory"} & set(replay)
    assert replay["artifact_decisions"] == []
    reconstructed = merge.prepare_spec_merge(merge.inspect_spec_sets(replay["selections"]), replay)
    assert reconstructed.proposal_digest == preview.proposal_digest

    result = merge.apply_spec_merge(reconstructed)

    assert result.to_json()["transfer"] == "completed"
    assert result.to_json()["data_outcome"] == "applied"
    imported = destination / "001-import/spec.md"
    assert imported.read_bytes() == b"\xef\xbb\xbf# Imported\r\n"
    assert stat.S_IMODE(imported.stat().st_mode) == stat.S_IMODE(copied.stat().st_mode)
    assert imported.stat().st_mtime_ns == 0
    assert (destination / "001-import/custom.bin").read_bytes() == b"\xff\x00opaque"
    assert retained.read_bytes() == b"# Retained\n"
    assert state(destination / "002-retained") == retained_before
    assert state(source) == source_before
    assert not (destination / ".merge-specs.lock").exists()
    assert not list(destination.glob(".merge-specs-recovery-*"))
    marker = destination / "001-import/.merge-specs.json"
    assert marker.is_file()
    after = state(destination)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    assert not repeated.operations
    assert not repeated.conflicts
    assert merge.apply_spec_merge(repeated).to_json()["data_outcome"] == "unchanged"
    assert state(destination) == after


def test_unresolved_relationship_and_correspondence_refuse_without_writes(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Identical\n")
    put(destination, "001-feature/spec.md", "# Identical\n")
    before = state(isolated)
    inspection = merge.inspect_spec_sets(raw(source, destination))
    unresolved = merge.prepare_spec_merge(inspection, {})
    assert {item["kind"] for item in unresolved.conflicts} >= {"relationship", "correspondence"}
    result = merge.apply_spec_merge(unresolved)
    assert result.to_json()["transfer"] == "refused"
    assert result.to_json()["data_outcome"] == "unchanged"
    assert result.to_json()["originals"] == []
    assert state(isolated) == before


def preview_for(merge, source, destination, correspondences, *, artifacts=(), dependents=(), **selection):
    inspection = merge.inspect_spec_sets(raw(source, destination, **selection))
    return merge.prepare_spec_merge(
        inspection,
        approved(inspection, correspondences, artifact_decisions=list(artifacts), dependent_features=list(dependents)),
    )


def replacement(path, content, **fields):
    return {"path": path, "resolution": "replace", "content": base64.b64encode(content).decode("ascii"), **fields}


def revision(source="001-feature", destination="001-feature", **fields):
    return {"source_feature": source, "destination_feature": destination, "relation": "revision", **fields}


@pytest.mark.parametrize("relationship", ["unconfirmed", "unrelated"])
def test_relationship_refusal_conserves_all_contents_and_resources(merge, isolated, relationship):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Destination\n")
    inspection = merge.inspect_spec_sets(raw(source, destination))
    before = state(isolated)
    preview = merge.prepare_spec_merge(
        inspection,
        approved(inspection, [{"source_feature": "001-feature", "relation": "independent"}],
                 relationship={"relationship": relationship, "evidence": "The maintainer decision."}),
    )
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "refused"
    assert result["data_outcome"] == "unchanged"
    assert result["originals"] == []
    assert state(isolated) == before


def test_different_recorded_identities_accept_explicit_relationship_confirmation(merge, isolated):
    source_repo = external(isolated, "source-repo", isolated / "workspace-source", str(uuid4()))
    destination_repo = external(isolated, "destination-repo", isolated / "workspace-destination", str(uuid4()))
    inspection = merge.inspect_spec_sets({"source": str(source_repo), "destination": str(destination_repo)})
    protected = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in isolated.rglob("*") if path.is_file() and ".specify" in path.parts
    }
    source_before = state(inspection.source.spec_root)
    target = "20261001-010203-independent"
    preview = merge.prepare_spec_merge(
        inspection,
        approved(inspection, [{"source_feature": "001-feature", "destination_feature": target, "relation": "independent"}]),
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert (inspection.destination.spec_root / target / "spec.md").read_bytes() == b"# Specification"
    assert state(inspection.source.spec_root) == source_before
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == value for path, value in protected.items())


def test_invalid_local_locator_refuses_without_writes(merge, isolated):
    source = isolated / "source"
    destination_repo = external(isolated, "destination-repo", isolated / "workspace", str(uuid4()))
    put(source, ".specify/project.json", json.dumps({"schema_version": 1, "project_id": str(uuid4()), "storage": "local"}))
    put(source, "specs/002-source/spec.md", "# Source\n")
    before = state(isolated)
    with pytest.raises(ValueError, match="Invalid storage mode"):
        merge.inspect_spec_sets({"source": str(source), "destination": str(destination_repo)})
    assert state(isolated) == before


def test_same_feature_conflicts_show_complete_text_and_opaque_differences(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    source_text = "".join(f"source meaning {index}\n" for index in range(80))
    destination_text = "".join(f"destination meaning {index}\n" for index in range(80))
    put(source, "001-feature/spec.md", source_text)
    put(source, "001-feature/custom.bin", b"\xffsource")
    put(destination, "001-feature/spec.md", destination_text)
    put(destination, "001-feature/custom.bin", b"\x00destination")
    put(destination, "001-feature/retained.md", "# Destination-only meaning\n")
    before = state(isolated)
    preview = preview_for(merge, source, destination, [revision()])
    differences = {row["path"]: row["difference"] for row in preview.to_json()["artifacts"]}
    assert "+source meaning 0\n" in differences["001-feature/spec.md"]
    assert "+source meaning 79\n" in differences["001-feature/spec.md"]
    assert "-destination meaning 79\n" in differences["001-feature/spec.md"]
    assert "7 bytes" in differences["001-feature/custom.bin"]
    assert "12 bytes" in differences["001-feature/custom.bin"]
    assert {row["path"] for row in preview.conflicts if row["kind"] == "content"} == {
        "001-feature/spec.md", "001-feature/custom.bin",
    }
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


def test_reviewed_combined_bytes_keep_destination_only_artifacts_and_force_repeat_noop(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source meaning\n")
    put(source, "001-feature/custom.bin", b"\xffsource")
    kept = put(destination, "003-renamed/spec.md", "# Destination meaning\n")
    put(destination, "003-renamed/custom.bin", b"\x00destination")
    put(destination, "003-renamed/destination-only.md", "Do not delete\n")
    kept.chmod(0o640)
    os.utime(kept, ns=(0, 0))
    kept_mode = stat.S_IMODE(kept.stat().st_mode)
    only_before = state(destination / "003-renamed")["destination-only.md"]
    combined = b"\xef\xbb\xbf# Source meaning and destination meaning\r\n"
    preview = preview_for(
        merge, source, destination, [revision(destination="003-renamed")],
        artifacts=[replacement("003-renamed/spec.md", combined),
                   {"path": "003-renamed/custom.bin", "resolution": "keep_destination"}],
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert kept.read_bytes() == combined
    assert (stat.S_IMODE(kept.stat().st_mode), kept.stat().st_mtime_ns) == (kept_mode, 0)
    assert state(destination / "003-renamed")["destination-only.md"] == only_before
    assert (destination / "003-renamed/custom.bin").read_bytes() == b"\x00destination"
    before = state(isolated)
    repeat = preview_for(
        merge, source, destination,
        [{"source_feature": "001-feature", "destination_feature": "999-unapproved-duplicate", "relation": "independent"}],
        artifacts=[replacement("999-unapproved-duplicate/spec.md", b"Must not duplicate\n")],
    )
    assert not repeat.operations
    assert not repeat.conflicts
    assert repeat.correspondences[0]["destination_feature"] == "003-renamed"
    assert merge.apply_spec_merge(repeat).data_outcome == "unchanged"
    assert state(isolated) == before


def test_exact_reference_recipes_preserve_bom_crlf_opaque_bytes_and_external_links(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    source_context, destination_context = isolated / "source-code", isolated / "destination-code"
    source_context.mkdir()
    destination_context.mkdir()
    old = f"{source}/001-feature/spec.md"
    old_code = f"{source_context}/module.py"
    content = f"\ufeff# Feature\r\n[spec]({old})\r\n[code]({old_code})\r\n[external](https://example.invalid/001-feature)\r\n"
    put(source, "001-feature/spec.md", content)
    put(source, "001-feature/blob.bin", b"\x00\xffopaque\r\n")
    put(destination, "001-retained/spec.md", "# Retained\n")
    replacements = [
        {"old": old, "new": f"{destination}/002-feature/spec.md", "count": 1},
        {"old": old_code, "new": f"{destination_context}/module.py", "count": 1},
    ]
    preview = preview_for(
        merge, source, destination,
        [{"source_feature": "001-feature", "relation": "independent"}],
        artifacts=[{"path": "002-feature/spec.md", "source_path": "001-feature/spec.md", "resolution": "copy_source",
                    "replacements": replacements}],
        destination_numbering="sequential", destination_context=str(destination_context),
    )
    assert not preview.conflicts
    replay = preview.to_json()["replay_inputs"]
    again = merge.prepare_spec_merge(merge.inspect_spec_sets(replay["selections"]), replay)
    assert again.proposal_digest == preview.proposal_digest
    assert merge.apply_spec_merge(again).data_outcome == "applied"
    expected = f"\ufeff# Feature\r\n[spec]({destination}/002-feature/spec.md)\r\n[code]({destination_context}/module.py)\r\n[external](https://example.invalid/001-feature)\r\n".encode()
    assert (destination / "002-feature/spec.md").read_bytes() == expected
    assert (destination / "002-feature/blob.bin").read_bytes() == b"\x00\xffopaque\r\n"
    before = state(isolated)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    assert not repeated.operations
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert state(isolated) == before


def test_literal_reference_replacements_use_original_bytes_not_cascaded_results(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"AA BB\r\n")
    destination.mkdir()
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source", "source_path": "001-feature/spec.md",
                    "replacements": [{"old": "AA", "new": "BB"}, {"old": "BB", "new": "CC"}]}],
    )
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert (destination / "001-feature/spec.md").read_bytes() == b"BB CC\r\n"


@pytest.mark.parametrize("replacements", [
    [{"old": "AA", "new": "BB", "count": 2}],
    [{"old": "missing", "new": "BB"}],
    [{"old": "AA", "new": "BB"}, {"old": "A", "new": "C"}],
])
def test_invalid_or_overlapping_reference_recipes_refuse_without_writes(merge, isolated, replacements):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"AA BB")
    destination.mkdir()
    before = state(isolated)
    with pytest.raises(ValueError, match="[Rr]eference|overlap|occurrence"):
        preview_for(
            merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
            artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source", "replacements": replacements}],
        )
    assert state(isolated) == before


def test_ambiguous_references_and_invalid_dependents_refuse_without_writes(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "[ambiguous](../001-feature/spec.md)\n")
    put(destination, "001-existing/spec.md", "# Destination\n")
    before = state(isolated)
    preview = preview_for(
        merge, source, destination,
        [{"source_feature": "001-feature", "relation": "independent"}],
        artifacts=[{"path": "002-feature/spec.md", "source_path": "001-feature/spec.md", "resolution": "copy_source",
                    "unresolved": ["This reference can name either retained feature."]}],
        destination_numbering="sequential",
    )
    assert any(row["kind"] == "reference" and row["path"] == "002-feature/spec.md" for row in preview.conflicts)
    assert merge.apply_spec_merge(preview).transfer == "refused"
    with pytest.raises(ValueError, match="[Dd]ependent|inventory"):
        preview_for(merge, source, destination, [], dependents=["999-not-in-inventory"])
    assert state(isolated) == before


@pytest.mark.parametrize("case", range(12))
def test_seeded_import_allocation_retains_free_names_and_workspace_reservations(merge, isolated, case):
    rng = random.Random(SEED)
    source, destination = isolated / "source", isolated / "destination"
    for _ in range(case + 1):
        reservations = sorted(rng.sample(range(1, 32), rng.randrange(2, 7)))
        collided = rng.choice(reservations)
    for number in reservations:
        (destination / "group" / f"{number:03d}-reserved").mkdir(parents=True)
    colliding = f"{collided:03d}-import"
    free = f"{max(reservations) + 5:03d}-free"
    put(source, f"{colliding}/spec.md", f"Imported {case}\n")
    put(source, f"{free}/spec.md", "Retain this free name\n")
    before = state(destination)
    preview = preview_for(
        merge, source, destination,
        [{"source_feature": feature, "relation": "independent"} for feature in (colliding, free)],
        destination_numbering="sequential",
    )
    expected = f"{max(reservations) + 6:03d}-import"
    message = f"seed={SEED} case={case} names={reservations!r} decisions={preview.to_json()['correspondences']!r} minimal_witness={[collided, free]!r}"
    assert not preview.conflicts, message
    mappings = {row["source_feature"]: row["destination_feature"] for row in preview.correspondences}
    assert mappings == {colliding: expected, free: free}, message
    assert merge.apply_spec_merge(preview).data_outcome == "applied", message
    assert (destination / expected / "spec.md").read_bytes() == f"Imported {case}\n".encode(), message
    assert (destination / free / "spec.md").read_bytes() == b"Retain this free name\n", message
    assert all(state(destination)[path] == value for path, value in before.items() if path != "."), message


def test_casefolded_name_collision_uses_destination_policy_without_renaming_existing_work(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "notes/spec.md", "# Imported notes\n")
    existing = put(destination, "Notes/spec.md", "# Existing notes\n")
    (destination / "010-prefix-reservation").mkdir()
    preview = preview_for(
        merge, source, destination, [{"source_feature": "notes", "relation": "independent"}],
        destination_numbering="sequential",
    )
    assert preview.correspondences[0]["destination_feature"] == "011-notes"
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert existing.read_bytes() == b"# Existing notes\n"
    assert (destination / "011-notes/spec.md").read_bytes() == b"# Imported notes\n"


@pytest.mark.parametrize(("source_name", "destination_name", "numbering", "expected"), [
    ("20261231-235959-source", "20261231-235959-reserved", "timestamp", "20270101-000000-source"),
    ("9223372036854775807-source", "9223372036854775807-reserved", "sequential", None),
    ("99991231-235959-source", "99991231-235959-reserved", "timestamp", None),
    ("9223372036854775808-source", "001-retained", "sequential", None),
    ("20260230-010203-source", "001-retained", "timestamp", None),
])
def test_seeded_numbering_boundaries_and_invalid_prefixes(merge, isolated, source_name, destination_name, numbering, expected):
    source, destination = isolated / "source", isolated / "destination"
    put(source, f"{source_name}/spec.md", "# Source\n")
    (destination / destination_name).mkdir(parents=True)
    before = state(isolated)
    preview = preview_for(
        merge, source, destination, [{"source_feature": source_name, "relation": "independent"}],
        destination_numbering=numbering,
    )
    message = f"seed={SEED} minimal_tree={(source_name, destination_name)!r} decisions={preview.to_json()['correspondences']!r}"
    if expected is None:
        assert any(row["kind"] == "name" for row in preview.conflicts), message
        assert merge.apply_spec_merge(preview).transfer == "refused", message
        assert state(isolated) == before, message
    else:
        assert preview.correspondences[0]["destination_feature"] == expected, message
        assert merge.apply_spec_merge(preview).data_outcome == "applied", message
        assert (destination / expected / "spec.md").read_bytes() == b"# Source\n", message


def test_missing_numbering_blocks_only_collision_allocation(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Imported\n")
    put(destination, "001-retained/spec.md", "# Retained\n")
    before = state(isolated)
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    assert any(row["kind"] == "numbering" for row in preview.conflicts)
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


@pytest.mark.parametrize("path", ["../escape.md", "/absolute.md", "001-feature/../escape.md", "C:\\escape.md", "001-feature//file.md"])
def test_unsafe_authored_paths_and_invalid_base64_never_create_resources(merge, isolated, path):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    before = state(isolated)
    with pytest.raises(ValueError, match="[Ss]afe|[Tt]raversal|absolute"):
        preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
                    artifacts=[replacement(path, b"Unsafe\n")])
    with pytest.raises(ValueError, match="Base64"):
        preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
                    artifacts=[{"path": "001-feature/spec.md", "resolution": "replace", "content": "!not-base64!"}])
    assert state(isolated) == before


def test_complete_mapped_comparison_requires_explicit_correspondence_and_opaque_equality(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Identical\n")
    put(source, "001-feature/custom.bin", b"\xffopaque")
    put(destination, "005-delivered/spec.md", "# Identical\n")
    put(destination, "005-delivered/custom.bin", b"\xffopaque")
    put(destination, "005-delivered/destination-only.md", "# Keep\n")
    before = state(isolated)
    unknown = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {
        "relationship": {"relationship": "same_project", "evidence": "The maintainer confirmed one project."},
    })
    assert any(row["kind"] == "correspondence" for row in unknown.conflicts)
    assert unknown.correspondences[0]["delivery_candidates"] == ("005-delivered",)
    reviewed = preview_for(merge, source, destination, [{
        "source_feature": "001-feature", "destination_feature": "005-delivered", "relation": "already_delivered",
    }])
    assert reviewed.correspondences[0]["delivery_candidates"] == ("005-delivered",)
    assert not reviewed.operations
    assert merge.apply_spec_merge(reviewed).data_outcome == "unchanged"
    assert state(isolated) == before
    (destination / "005-delivered/custom.bin").write_bytes(b"\xffchanged")
    changed = preview_for(merge, source, destination, [{
        "source_feature": "001-feature", "destination_feature": "005-delivered", "relation": "already_delivered",
    }])
    assert any(row["kind"] == "correspondence" for row in changed.conflicts)
    assert merge.apply_spec_merge(changed).transfer == "refused"


def delivered(merge, source, destination, *, target="001-feature"):
    preview = preview_for(
        merge, source, destination,
        [{"source_feature": "001-feature", "destination_feature": target, "relation": "independent"}],
        destination_numbering="sequential",
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    return destination / target / ".merge-specs.json"


@pytest.mark.parametrize("change", ["source_bytes", "destination_bytes", "marker_bytes", "membership", "metadata", "context"])
def test_original_preview_refuses_input_drift_without_new_artifact_changes(merge, isolated, change):
    source, destination = isolated / "source", isolated / "destination"
    source_file = put(source, "001-feature/spec.md", "# Source\n")
    destination_file = put(destination, "002-retained/spec.md", "# Retain\n")
    principles = put(isolated, "principles.md", "Principles\n")
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        destination_numbering="sequential", destination_principles=str(principles),
    )
    if change in {"source_bytes", "destination_bytes"}:
        target = source_file if change == "source_bytes" else destination_file
        info = target.stat()
        target.write_bytes(b"# Changed")
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif change == "marker_bytes":
        put(destination, "002-retained/.merge-specs.json", '{"custom":"new state"}')
    elif change == "membership":
        (destination / "empty-new-directory").mkdir()
    elif change == "metadata":
        source_file.chmod(0o444)
    else:
        principles.write_text("New principles\n")
    changed = state(isolated)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "refused"
    assert result["data_outcome"] == "unchanged"
    assert result["originals"] == []
    assert result["recovery_directory"] is None
    assert state(isolated) == changed


def test_original_preview_refuses_selected_tracking_and_branch_drift(merge, isolated):
    repository = recorded_source(isolated)
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    filesystem = preview_for(
        merge, repository / "specs", destination,
        [{"source_feature": "001-feature", "relation": "independent"}], destination_numbering="sequential",
    )
    git(repository, "rm", "--cached", "specs/001-feature/spec.md")
    changed = state(isolated)
    assert merge.apply_spec_merge(filesystem).transfer == "refused"
    assert state(isolated) == changed
    git(repository, "add", "specs")
    branch_inspection = merge.inspect_spec_sets({**raw(repository, destination), "source_kind": "branch", "source_branch": "main"})
    branch = merge.prepare_spec_merge(branch_inspection, approved(branch_inspection, [{
        "source_feature": "001-feature", "relation": "independent",
    }]))
    put(repository, "specs/001-feature/spec.md", "# Changed branch\n")
    git(repository, "add", "specs")
    git(repository, "commit", "-m", "Advance selected fixture reference")
    changed = state(isolated)
    assert merge.apply_spec_merge(branch).transfer == "refused"
    assert state(isolated) == changed


def test_changed_operation_and_compact_replay_payload_bindings_refuse(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        artifacts=[replacement("001-feature/spec.md", b"# Approved\n")],
    )
    before = state(isolated)
    operations = tuple(
        dataclasses.replace(operation, content=b"# Unapproved\n") if operation.path == "001-feature/spec.md" else operation
        for operation in preview.operations
    )
    changed_preview = dataclasses.replace(preview, operations=operations)
    assert merge.apply_spec_merge(changed_preview).transfer == "refused"
    replay = preview.to_json()["replay_inputs"]
    replay["artifact_decisions"][0]["content"] = base64.b64encode(b"# Unapproved\n").decode("ascii")
    with pytest.raises(ValueError, match="[Pp]roposal|approved"):
        merge.prepare_spec_merge(merge.inspect_spec_sets(replay["selections"]), replay)
    replay = preview.to_json()["replay_inputs"]
    replay["temporary_resources"]["recovery_directory"] = ".merge-specs-recovery-" + uuid4().hex
    with pytest.raises(ValueError, match="[Pp]roposal|approved"):
        merge.prepare_spec_merge(merge.inspect_spec_sets(replay["selections"]), replay)
    assert state(isolated) == before


def test_under_lock_revalidation_refuses_new_drift_and_claims_no_originals(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    changed_file = put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Destination\n")
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        destination_numbering="sequential",
    )
    real_open = os.open
    drifted = False

    def open_lock(path, flags, *args, **kwargs):
        nonlocal drifted
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == destination / ".merge-specs.lock" and not drifted:
            info = changed_file.stat()
            changed_file.write_bytes(b"# Edited\n")
            os.utime(changed_file, ns=(info.st_atime_ns, info.st_mtime_ns))
            drifted = True
        return descriptor

    monkeypatch.setattr(os, "open", open_lock)
    destination_before = state(destination)
    result = merge.apply_spec_merge(preview).to_json()
    assert drifted
    assert result["transfer"] == "refused"
    assert result["data_outcome"] == "unchanged"
    assert result["originals"] == []
    assert not result["cleanup_required"]
    assert state(destination) == destination_before
    assert changed_file.read_bytes() == b"# Edited\n"


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("resource", [".merge-specs.lock", ".merge-specs-recovery-unfinished"])
def test_pending_resources_prevent_transfer_and_remain_untouched(merge, isolated, side, resource):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Destination\n")
    selected = source if side == "source" else destination
    put(selected, resource if resource.endswith(".lock") else resource + "/originals/000000.bin", b"\xffpending")
    before = state(isolated)
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        destination_numbering="sequential",
    )
    assert any(row["kind"] == "pending_resources" for row in preview.conflicts)
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


@pytest.mark.parametrize("resource", [".merge-specs.lock", ".merge-specs-recovery-abcd/journal.json"])
def test_branch_pending_resources_prevent_transfer_like_filesystem_sets(merge, isolated, resource):
    repository = recorded_source(isolated)
    put(repository, f"specs/{resource}", b"\xffpending")
    git(repository, "add", "specs")
    git(repository, "commit", "-m", "Record fixture merge resource")
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    pending = resource.split("/")[0]
    inspection = merge.inspect_spec_sets({**raw(repository, destination), "source_kind": "branch", "source_branch": "main"})
    assert inspection.source.pending_resources == (pending,)
    assert [feature.path for feature in inspection.source.features] == ["001-feature"]
    assert not any(path.startswith(pending) for path in inspection.source.files)
    assert inspection.to_json()["review"] == "incomplete_review"
    before = state(isolated)
    preview = preview_for(
        merge, repository, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        source_kind="branch", source_branch="main", destination_numbering="sequential",
    )
    assert {"role": "source", "path": pending} in next(row for row in preview.conflicts if row["kind"] == "pending_resources")["resources"]
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


def test_exclusive_fixed_lock_refuses_another_attempt_without_removing_its_lock(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Destination\n")
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    real_open = os.open

    def race(path, flags, *args, **kwargs):
        if Path(path) == destination / ".merge-specs.lock":
            Path(path).write_bytes(b"Another merge attempt owns this lock.\n")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "refused"
    assert result["data_outcome"] == "unchanged"
    assert (destination / ".merge-specs.lock").read_bytes() == b"Another merge attempt owns this lock.\n"
    assert not (destination / "001-feature").exists()
    assert not list(destination.glob(".merge-specs-recovery-*"))
    assert result["originals"] == []


def test_preserved_originals_precede_first_mutation_and_complete_rollback_restores_metadata(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"\xef\xbb\xbf# Source\r\n")
    put(source, "001-feature/plan.md", "# Source plan\n")
    spec = put(destination, "001-feature/spec.md", b"\xef\xbb\xbf# Original\r\n")
    plan = put(destination, "001-feature/plan.md", "# Original plan\n")
    spec.chmod(0o640)
    plan.chmod(0o600)
    os.utime(spec, ns=(0, 0))
    os.utime(plan, ns=(1539, 1539))
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": path, "resolution": "copy_source"} for path in ("001-feature/spec.md", "001-feature/plan.md")],
    )
    before = state(isolated)
    real_replace = os.replace
    witnessed = {}

    def fail_spec(source_path, destination_path):
        recovery = Path(source_path).parent.parent
        if Path(destination_path) == destination / "001-feature/.merge-specs.json":
            journal = json.loads((recovery / "journal.json").read_text())
            originals = {row["path"]: (recovery / row["original"]).read_bytes()
                         for row in journal["operations"] if "original" in row}
            witnessed.update(originals)
            assert all(path.suffix == ".bin" and path.stem.isdigit() for path in (recovery / "originals").iterdir())
            assert not list(recovery.rglob("spec.md"))
            assert [row["index"] for row in journal["operations"]] == list(range(len(journal["operations"])))
        if Path(destination_path) == spec:
            raise OSError("Injected artifact write failure.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_spec)
    result = merge.apply_spec_merge(preview).to_json()
    assert witnessed == {"001-feature/spec.md": b"\xef\xbb\xbf# Original\r\n", "001-feature/plan.md": b"# Original plan\n"}
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "completely_restored"
    assert result["review"] == "not_performed"
    assert result["remaining_operations"] == []
    assert result["originals"] == []
    assert not result["cleanup_required"]
    assert state(isolated) == before


@pytest.mark.parametrize("unexpected_writer", [False, True])
def test_incomplete_restoration_preserves_originals_and_unexpected_writer_content(merge, isolated, monkeypatch, unexpected_writer):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(source, "001-feature/plan.md", "# Source plan\n")
    spec = put(destination, "001-feature/spec.md", "# Original\n")
    plan = put(destination, "001-feature/plan.md", "# Original plan\n")
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": path, "resolution": "copy_source"} for path in ("001-feature/spec.md", "001-feature/plan.md")],
    )
    source_before = state(source)
    real_replace = os.replace
    failed = False

    def fail_and_preserve(source_path, destination_path):
        nonlocal failed
        if Path(destination_path) == spec and not failed:
            failed = True
            if unexpected_writer:
                plan.write_bytes(b"Unexpected writer content.\n")
            raise OSError("Injected application failure.")
        if failed and Path(destination_path) == plan:
            raise OSError("Injected restoration failure.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_and_preserve)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "recovery_required"
    assert result["review"] == "incomplete_review"
    restore = next(row for row in result["remaining_operations"] if row["operation"] == "restore-file" and row["path"] == "001-feature/plan.md")
    recovery = Path(result["recovery_directory"])
    assert (recovery / restore["original"]).read_bytes() == b"# Original plan\n"
    assert plan.read_bytes() == (b"Unexpected writer content.\n" if unexpected_writer else b"# Source plan\n")
    assert spec.read_bytes() == b"# Original\n"
    assert state(source) == source_before
    assert not (destination / ".merge-specs.lock").exists()
    assert all(Path(row["original"]).read_bytes() == b"# Original plan\n" for row in result["originals"])
    inventory = merge.inspect_spec_sets(raw(None, destination)).to_json()
    assert {row["path"] for row in inventory["destination"]["features"]} == {"001-feature"}
    assert inventory["review"] == "incomplete_review"
    journal = json.loads((recovery / "journal.json").read_text())
    assert journal["remaining_operations"] == result["remaining_operations"]


def test_unexpected_writer_on_created_file_prevents_rollback_deletion(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(source, "001-feature/plan.md", "# Source plan\n")
    destination.mkdir()
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    real_replace = os.replace
    plan = destination / "001-feature/plan.md"

    def writer(source_path, destination_path):
        if Path(destination_path) == destination / "001-feature/spec.md":
            plan.write_bytes(b"New writer work.\n")
            raise OSError("Injected later write failure.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", writer)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["data_outcome"] == "recovery_required"
    assert plan.read_bytes() == b"New writer work.\n"
    assert any(row["operation"] == "remove-created-file" and row["path"] == "001-feature/plan.md" for row in result["remaining_operations"])
    assert result["originals"] == []
    assert not (destination / "001-feature/.merge-specs.json").exists()


@pytest.mark.parametrize("fail_application", [False, True])
def test_lock_cleanup_failure_keeps_true_applied_or_restored_data_outcome(merge, isolated, monkeypatch, fail_application):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(source, "001-feature/plan.md", "# Source plan\n")
    spec = put(destination, "001-feature/spec.md", "# Original\n")
    plan = put(destination, "001-feature/plan.md", "# Original plan\n")
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": path, "resolution": "copy_source"} for path in ("001-feature/spec.md", "001-feature/plan.md")],
    )
    real_unlink, real_replace = os.unlink, os.replace

    def fail_lock(path, *args, **kwargs):
        if Path(path) == destination / ".merge-specs.lock":
            raise PermissionError("Injected lock cleanup failure.")
        return real_unlink(path, *args, **kwargs)

    def fail_spec(source_path, destination_path):
        if fail_application and Path(destination_path) == spec:
            raise OSError("Injected application failure.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "unlink", fail_lock)
    monkeypatch.setattr(os, "replace", fail_spec)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == ("failed" if fail_application else "completed")
    assert result["data_outcome"] == ("completely_restored" if fail_application else "applied")
    assert spec.read_bytes() == (b"# Original\n" if fail_application else b"# Source\n")
    assert plan.read_bytes() == (b"# Original plan\n" if fail_application else b"# Source plan\n")
    assert result["cleanup_required"] is True
    assert [row["operation"] for row in result["remaining_operations"]] == ["remove-lock"]
    assert result["originals"] == []
    assert result["recovery_directory"] is None
    assert (destination / ".merge-specs.lock").exists()


def test_recovery_cleanup_failure_is_not_a_data_rollback_request(merge, isolated, monkeypatch):
    import shutil

    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])

    def fail_cleanup(*args, **kwargs):
        raise PermissionError("Injected recovery cleanup failure.")

    monkeypatch.setattr(shutil, "rmtree", fail_cleanup)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "completed"
    assert result["data_outcome"] == "applied"
    assert result["cleanup_required"]
    assert [row["operation"] for row in result["remaining_operations"]] == ["remove-recovery-directory"]
    assert result["originals"] == []
    assert (destination / "001-feature/spec.md").read_bytes() == b"# Source\n"
    assert not (destination / ".merge-specs.lock").exists()


def test_zero_directory_times_and_readonly_source_modes_survive_copy(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/contracts/spec.md", "# Contract\n")
    put(source, "001-feature/spec.md", "# Feature\n")
    source_directory = source / "001-feature"
    contracts = source_directory / "contracts"
    source_directory.chmod(0o550)
    contracts.chmod(0o750)
    os.utime(source_directory, ns=(0, 0))
    os.utime(contracts, ns=(1539, 1539))
    source_mode = stat.S_IMODE(source_directory.stat().st_mode)
    contracts_metadata = (stat.S_IMODE(contracts.stat().st_mode), contracts.stat().st_mtime_ns)
    destination.mkdir()
    before = state(source)
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert state(source) == before
    imported = destination / "001-feature"
    assert (stat.S_IMODE(imported.stat().st_mode), imported.stat().st_mtime_ns) == (source_mode, 0)
    assert (stat.S_IMODE((imported / "contracts").stat().st_mode), (imported / "contracts").stat().st_mtime_ns) == contracts_metadata


def test_metadata_only_changes_and_canonical_source_aliases_keep_current_marker_unchanged(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    original = put(source, "001-feature/spec.md", "# Feature\n")
    destination.mkdir()
    marker = delivered(merge, source, destination)
    recorded = (marker.read_bytes(), marker.stat().st_mtime_ns, stat.S_IMODE(marker.stat().st_mode))
    original.chmod(0o600)
    os.utime(original, ns=(1539, 1539))
    imported = destination / "001-feature/spec.md"
    imported.chmod(0o640)
    os.utime(imported, ns=(123, 123))
    alias = isolated / "canonical-alias"
    alias.symlink_to(source, target_is_directory=True)
    before = state(isolated)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(alias, destination)), {})
    assert not repeated.conflicts
    assert repeated.operations == ()
    assert repeated.temporary_resources == {}
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert (marker.read_bytes(), marker.stat().st_mtime_ns, stat.S_IMODE(marker.stat().st_mode)) == recorded
    assert state(isolated) == before


def test_repository_and_raw_selections_use_one_filesystem_delivery_origin(merge, isolated):
    repository = isolated / "repository"
    (repository / ".specify").mkdir(parents=True)
    put(repository, "specs/001-feature/spec.md", "# Feature\n")
    destination = isolated / "destination"
    destination.mkdir()
    marker = delivered(merge, repository / "specs", destination)
    before = state(isolated)
    inspection = merge.inspect_spec_sets({
        "source": str(repository), "source_kind": "repository",
        "destination": str(destination), "destination_kind": "set",
    })
    preview = merge.prepare_spec_merge(inspection, {})
    assert not preview.operations
    assert not preview.conflicts
    assert json.loads(marker.read_bytes())["origins"][0]["source"] == {
        "kind": "filesystem", "spec_root": str((repository / "specs").resolve()),
    }
    assert merge.apply_spec_merge(preview).data_outcome == "unchanged"
    assert state(isolated) == before


@pytest.mark.parametrize("drift", ["source", "destination", "source_name", "source_location", "destination_reference"])
def test_delivery_drift_requires_containing_feature_review_before_new_allocation(merge, isolated, drift):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    delivered(merge, source, destination, target="005-renamed")
    if drift == "source":
        (source / "001-feature/spec.md").write_bytes(b"# Changed source\n")
    elif drift in {"destination", "destination_reference"}:
        (destination / "005-renamed/spec.md").write_bytes(
            b"# Changed destination\n" if drift == "destination" else b"[context](../006-new-context/spec.md)\n"
        )
    elif drift == "source_name":
        (source / "001-feature").rename(source / "002-renamed-source")
    else:
        moved = isolated / "moved-source"
        source.rename(moved)
        source = moved
    before = state(isolated)
    feature = "002-renamed-source" if drift == "source_name" else "001-feature"
    preview = preview_for(
        merge, source, destination, [{"source_feature": feature, "relation": "independent"}],
        destination_numbering="sequential",
    )
    issue = next(row for row in preview.conflicts if row["kind"] == "delivery_state")
    assert issue["delivery_candidates"] == ("005-renamed",)
    assert not preview.operations
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


def test_marker_containing_directory_stays_authoritative_after_destination_rename(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    marker = delivered(merge, source, destination)
    recorded = marker.read_bytes()
    (destination / "001-feature").rename(destination / "20261001-010203-feature")
    before = state(isolated)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    assert repeated.correspondences[0]["destination_feature"] == "20261001-010203-feature"
    assert not repeated.operations
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert (destination / "20261001-010203-feature/.merge-specs.json").read_bytes() == recorded
    assert state(isolated) == before


def test_several_sources_keep_current_origins_and_replace_an_approved_rebind(merge, isolated, monkeypatch):
    source_a, source_b, destination = isolated / "source-a", isolated / "source-b", isolated / "destination"
    put(source_a, "001-feature/spec.md", "# Source A\n")
    put(source_b, "001-feature/spec.md", "# Source B\n")
    put(destination, "010-combined/spec.md", "# Destination\n")
    first = preview_for(
        merge, source_a, destination, [revision(destination="010-combined")],
        artifacts=[replacement("010-combined/spec.md", b"# A and destination\n")],
    )
    assert merge.apply_spec_merge(first).data_outcome == "applied"
    second = preview_for(
        merge, source_b, destination, [revision(destination="010-combined", delivery_review="additional")],
        artifacts=[replacement("010-combined/spec.md", b"# A, B, and destination\n")],
    )
    assert not second.conflicts
    assert merge.apply_spec_merge(second).data_outcome == "applied"
    marker = destination / "010-combined/.merge-specs.json"
    current = json.loads(marker.read_bytes())
    expected = {str(source_a): "001-feature", str(source_b): "001-feature"}
    assert {entry["source"]["spec_root"]: entry["source_feature"] for entry in current["origins"]} == expected
    before = state(isolated)
    for source in (source_a, source_b):
        repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
        assert not repeated.operations
        assert not repeated.conflicts
        assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert state(isolated) == before
    previous_a = next(entry for entry in current["origins"] if entry["source"]["spec_root"] == str(source_a))
    previous_b = next(entry for entry in current["origins"] if entry["source"]["spec_root"] == str(source_b))
    (source_a / "001-feature/spec.md").write_bytes(b"# New source A\n")
    updated = preview_for(
        merge, source_a, destination, [revision(destination="010-combined", delivery_review="confirmed")],
        artifacts=[replacement("010-combined/spec.md", b"# New A, B, and destination\n")],
    )
    assert merge.apply_spec_merge(updated).data_outcome == "applied"
    changed = json.loads(marker.read_bytes())
    new_a = next(entry for entry in changed["origins"] if entry["source"]["spec_root"] == str(source_a))
    assert new_a["source_state_digest"] != previous_a["source_state_digest"]
    assert next(entry for entry in changed["origins"] if entry["source"]["spec_root"] == str(source_b)) == previous_b
    assert {entry["source"]["spec_root"] for entry in changed["origins"]} == {str(source_a), str(source_b)}
    moved = isolated / "moved-source-a"
    source_a.rename(moved)
    real_read = Path.read_bytes

    def selected_reads(path):
        if path.is_relative_to(source_a):
            raise AssertionError("A marker must not authorize reads from an unselected source location.")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", selected_reads)
    rebind = preview_for(
        merge, moved, destination,
        [revision(destination="010-combined", delivery_review="confirmed",
                  rebind_origin={"source": new_a["source"], "source_feature": new_a["source_feature"]})],
        artifacts=[replacement("010-combined/spec.md", b"# New A, B, and destination\n")],
    )
    assert not rebind.conflicts
    assert {operation.path for operation in rebind.operations} == {"010-combined/.merge-specs.json"}
    assert merge.apply_spec_merge(rebind).data_outcome == "applied"
    rebound = json.loads(marker.read_bytes())
    assert {entry["source"]["spec_root"] for entry in rebound["origins"]} == {str(moved), str(source_b)}
    assert (destination / "010-combined/spec.md").read_bytes() == b"# New A, B, and destination\n"
    before = state(destination)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(moved, destination)), {})
    assert not repeated.operations
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert state(destination) == before


@pytest.mark.parametrize("unreadable", [False, True])
def test_malformed_or_unreadable_marker_requires_review_instead_of_import(merge, isolated, monkeypatch, unreadable):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    marker = delivered(merge, source, destination)
    real_read = Path.read_bytes
    if unreadable:
        def read(path):
            if path == marker:
                raise PermissionError("Injected unreadable marker.")
            return real_read(path)
        monkeypatch.setattr(Path, "read_bytes", read)
    else:
        marker.write_bytes(b"\xffcustom reserved content")
    marker_before = (real_read(marker), marker.stat().st_mtime_ns)
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
        destination_numbering="sequential",
    )
    assert any(row["kind"] == "delivery_state" and row["path"] == "001-feature/.merge-specs.json" for row in preview.conflicts)
    assert preview.to_json()["review"] == "incomplete_review"
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert (real_read(marker), marker.stat().st_mtime_ns) == marker_before
    assert not (destination / "002-feature").exists()
    assert not (destination / ".merge-specs.lock").exists()


def test_ambiguous_current_origins_require_review_of_every_containing_feature(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    marker = delivered(merge, source, destination)
    put(destination, "009-duplicate/spec.md", "# Source\n")
    put(destination, "009-duplicate/.merge-specs.json", marker.read_bytes())
    before = state(isolated)
    preview = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    issue = next(row for row in preview.conflicts if row["kind"] == "delivery_state")
    assert issue["delivery_candidates"] == ("001-feature", "009-duplicate")
    assert not preview.operations
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


@pytest.mark.parametrize("side", ["source", "destination", "nested_source"])
def test_reserved_name_custom_bytes_need_explicit_preservation_before_transfer(merge, isolated, side):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "001-feature/spec.md", "# Destination\n")
    location = source if side != "destination" else destination
    relative = "001-feature/contracts/.merge-specs.json" if side == "nested_source" else "001-feature/.merge-specs.json"
    put(location, relative, b"\xffcustom bookkeeping\r\n")
    before = state(isolated)
    unresolved = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}],
    )
    assert any(row["kind"] == "delivery_state" for row in unresolved.conflicts)
    assert merge.apply_spec_merge(unresolved).transfer == "refused"
    assert state(isolated) == before
    row = revision()
    choices = [{"path": "001-feature/spec.md", "resolution": "copy_source"}]
    if side == "destination":
        row["preserve_marker_as"] = "custom-bookkeeping.bin"
    elif side == "source":
        row["preserve_source_marker_as"] = "custom-bookkeeping.bin"
    else:
        choices.append({"path": relative, "source_path": relative, "resolution": "preserve_marker",
                        "preserve_path": "001-feature/custom-bookkeeping.bin"})
    reviewed = preview_for(merge, source, destination, [row], artifacts=choices)
    assert not reviewed.conflicts
    assert merge.apply_spec_merge(reviewed).data_outcome == "applied"
    assert (destination / "001-feature/custom-bookkeeping.bin").read_bytes() == b"\xffcustom bookkeeping\r\n"
    assert (destination / "001-feature/spec.md").read_bytes() == b"# Source\n"
    current = json.loads((destination / "001-feature/.merge-specs.json").read_bytes())
    assert current["origins"][0]["source"]["spec_root"] == str(source)
    if side != "destination":
        assert (source / relative).read_bytes() == b"\xffcustom bookkeeping\r\n"


def test_current_marker_bytes_and_metadata_restore_after_failed_update(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Original delivered source\n")
    destination.mkdir()
    marker = delivered(merge, source, destination)
    marker.chmod(0o640)
    os.utime(marker, ns=(0, 0))
    marker_mode = stat.S_IMODE(marker.stat().st_mode)
    (source / "001-feature/spec.md").write_bytes(b"# New source\n")
    preview = preview_for(
        merge, source, destination, [revision(delivery_review="confirmed")],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}],
    )
    before = state(isolated)
    real_replace = os.replace

    def fail_spec(source_path, destination_path):
        if Path(destination_path) == destination / "001-feature/spec.md":
            raise OSError("Injected update failure after marker replacement.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", fail_spec)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "completely_restored"
    assert state(isolated) == before
    assert (stat.S_IMODE(marker.stat().st_mode), marker.stat().st_mtime_ns) == (marker_mode, 0)


def test_shared_standalone_and_empty_sets_need_no_resources_and_state_the_review_scope(merge, isolated):
    destination = isolated / "destination"
    put(destination, "001-feature/spec.md", "# Feature\n")
    put(destination, "002-dependent/spec.md", "# Dependent\n")
    principles = put(isolated, "principles.md", "Principles\n")
    alias = isolated / "alias"
    alias.symlink_to(destination, target_is_directory=True)
    before = state(isolated)
    shared = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(alias, destination, destination_principles=str(principles))), {})
    assert not shared.conflicts
    assert not shared.operations
    assert shared.to_json()["review_scope"] == ["001-feature", "002-dependent"]
    assert shared.to_json()["review"] == "not_performed"
    assert merge.apply_spec_merge(shared).transfer == "not_needed"
    standalone = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(None, destination, destination_principles=str(principles))),
                                         {"dependent_features": ["001-feature", "002-dependent"]})
    assert merge.apply_spec_merge(standalone).transfer == "not_requested"
    assert standalone.to_json()["planning"] == [
        {"feature": "001-feature", "specification": "readable", "plan": "missing", "tasks": "missing", "complete": False},
        {"feature": "002-dependent", "specification": "readable", "plan": "missing", "tasks": "missing", "complete": False},
    ]
    assert standalone.temporary_resources == {}
    assert state(isolated) == before
    empty_source, empty_destination = isolated / "empty-source", isolated / "empty-destination"
    empty_source.mkdir()
    empty_destination.mkdir()
    before = state(isolated)
    empty = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(empty_source, empty_destination)), {})
    assert not empty.operations
    assert merge.apply_spec_merge(empty).data_outcome == "unchanged"
    assert state(isolated) == before


def test_preview_warns_about_actual_artifact_repository_and_unknown_tracking(merge, isolated, monkeypatch):
    import subprocess

    repository = recorded_source(isolated)
    destination = isolated / "destination"
    put(destination, "001-collision/spec.md", "# Destination\n")
    tracked = preview_for(
        merge, repository / "specs", destination,
        [{"source_feature": "001-feature", "relation": "independent"}], destination_numbering="sequential",
    )
    warning = next(row for row in tracked.notices if row["kind"] == "later_git_merge")
    assert warning["repository"] == str(repository)
    assert warning["selected_paths"] == ("001-feature/spec.md",)
    assert "reconciliation-only" in warning["message"]
    duplicate = next(row for row in tracked.notices if row["kind"] == "renamed_duplicate_risk")
    assert duplicate["mappings"] == ({"source_feature": "001-feature", "destination_feature": "002-feature"},)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("Injected unavailable Git.")

    monkeypatch.setattr(subprocess, "run", unavailable)
    unknown = preview_for(
        merge, repository / "specs", destination,
        [{"source_feature": "001-feature", "relation": "independent"}], destination_numbering="sequential",
    )
    warning = next(row for row in unknown.notices if row["kind"] == "unknown_tracking")
    assert "If these artifacts are tracked" in warning["message"]
    assert merge.apply_spec_merge(unknown).data_outcome == "applied"


def test_raw_git_root_inventory_and_transfer_exclude_git_metadata(merge, isolated):
    repository = isolated / "artifact-repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    put(repository, "001-feature/spec.md", "# Tracked artifact\n")
    git(repository, "add", "001-feature/spec.md")
    git(repository, "commit", "-m", "Create artifact repository")
    destination = isolated / "destination"
    put(destination, "002-retained/spec.md", "# Destination\n")
    source_before = state(repository)
    inspection = merge.inspect_spec_sets(raw(repository, destination, destination_numbering="sequential"))
    assert set(inspection.source.files) == {"001-feature/spec.md"}
    assert {feature.path for feature in inspection.source.features} == {"001-feature"}
    assert inspection.source.tracking == {"001-feature/spec.md": "tracked"}
    preview = merge.prepare_spec_merge(
        inspection, approved(inspection, [{"source_feature": "001-feature", "relation": "independent"}]),
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert (destination / "001-feature/spec.md").read_bytes() == b"# Tracked artifact\n"
    assert not (destination / ".git").exists()
    assert state(repository) == source_before


@pytest.mark.parametrize("branch", [False, True])
def test_git_environment_cannot_redirect_selected_source(merge, isolated, monkeypatch, branch):
    repository = recorded_source(isolated)
    alternate = isolated / "alternate"
    alternate.mkdir()
    import subprocess

    subprocess.run(["git", "init", str(alternate)], check=True, capture_output=True)
    destination = isolated / "destination"
    destination.mkdir()
    monkeypatch.setenv("GIT_DIR", str(alternate / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(alternate))
    selections = raw(repository / "specs", destination)
    if branch:
        selections.update(source=str(repository), source_kind="branch", source_branch="HEAD")
    inspected = merge.inspect_spec_sets(selections)
    assert inspected.source.files["001-feature/spec.md"].content == b"# Recorded specification\n"
    assert inspected.source.tracking["001-feature/spec.md"] == "tracked"


def test_case_aliases_preserve_overlap_and_current_origin_identity(merge, isolated):
    source = isolated / "case-source"
    put(source, "001-feature/spec.md", "# Source\n")
    alias = source.with_name(source.name.upper())
    if not alias.exists() or not alias.samefile(source):
        pytest.skip("The fixture filesystem distinguishes case.")
    nested = source / "destination"
    nested.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        merge.inspect_spec_sets(raw(alias, nested))
    nested.rmdir()
    destination = isolated / "destination"
    destination.mkdir()
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    before = state(destination)
    repeat = preview_for(merge, alias, destination, [{"source_feature": "001-feature", "relation": "independent", "destination_feature": "999-duplicate"}])
    assert not repeat.conflicts
    assert not repeat.operations
    assert merge.apply_spec_merge(repeat).transfer == "not_needed"
    assert state(destination) == before


@pytest.mark.parametrize("override", ["repository", "index", "objects", "configuration"])
def test_caller_git_overrides_cannot_redirect_branch_or_cached_tracking(merge, isolated, monkeypatch, override):
    selected_root, alternate_root = isolated / "selected", isolated / "alternate"
    selected_root.mkdir()
    alternate_root.mkdir()
    selected = recorded_source(selected_root)
    alternate = recorded_source(alternate_root)
    (alternate / "specs/001-feature").rename(alternate / "specs/001-other")
    put(alternate, "specs/001-other/spec.md", "# Unselected repository\n")
    git(alternate, "add", "-A", "specs")
    git(alternate, "commit", "-m", "Create different alternate artifacts")
    selected_commit = git(selected, "rev-parse", "main").decode().strip()
    before = state(isolated)
    if override == "repository":
        monkeypatch.setenv("GIT_DIR", str(alternate / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(alternate))
        monkeypatch.setenv("GIT_COMMON_DIR", str(alternate / ".git"))
    elif override == "index":
        monkeypatch.setenv("GIT_INDEX_FILE", str(alternate / ".git/index"))
    elif override == "objects":
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(alternate / ".git/objects"))
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(alternate / ".git/objects"))
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(alternate))
    inspection = merge.inspect_spec_sets({
        **raw(selected, selected / "specs"), "source_kind": "branch", "source_branch": "main",
    })
    assert inspection.source.repository_root == selected.resolve()
    assert inspection.source.commit == selected_commit
    assert set(inspection.source.files) == {"001-feature/spec.md"}
    assert inspection.source.files["001-feature/spec.md"].content == b"# Recorded specification\n"
    filesystem = merge.inspect_spec_sets(raw(selected / "specs", alternate / "specs"))
    assert filesystem.source.tracking == {"001-feature/spec.md": "tracked"}
    assert filesystem.source.tracking_context["repository"] == str(selected.resolve())
    assert state(isolated) == before


def test_case_aliases_cannot_hide_physically_nested_sets(merge, isolated):
    source = isolated / "case-overlap"
    destination = source / "destination"
    put(source, "001-source/spec.md", "# Source\n")
    put(destination, "002-destination/spec.md", "# Destination\n")
    upper = source.with_name(source.name.upper())
    if not upper.exists():
        pytest.skip("The filesystem distinguishes these case variants.")
    before = state(isolated)
    with pytest.raises(ValueError, match="[Nn]ested|[Oo]verlap"):
        merge.inspect_spec_sets(raw(upper, destination))
    with pytest.raises(ValueError, match="[Nn]ested|[Oo]verlap"):
        merge.inspect_spec_sets(raw(destination, upper))
    same = merge.inspect_spec_sets(raw(upper, source))
    assert same.transfer_needed is False
    assert same.transfer == "not_needed"
    assert state(isolated) == before


def test_case_alias_repeat_uses_the_same_durable_filesystem_origin(merge, isolated):
    source, destination = isolated / "case-source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    upper = source.with_name(source.name.upper())
    if not upper.exists():
        pytest.skip("The filesystem distinguishes these case variants.")
    marker = delivered(merge, source, destination)
    before = state(isolated)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(upper, destination)), {})
    assert not repeated.conflicts
    assert not repeated.operations
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert json.loads(marker.read_bytes())["origins"][0]["source"]["spec_root"] == str(source.resolve())
    assert state(isolated) == before


def test_raw_project_metadata_and_authored_metadata_targets_refuse_without_record_reads(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-feature/spec.md", "# Destination\n")
    record = put(destination, ".specify/project.json", '{"private":"project record"}')
    put(destination, ".specify/workflows/runs/current.json", '{"private":"runtime record"}')
    before = state(isolated)
    real_read = Path.read_bytes

    def artifact_reads(path):
        if ".specify" in path.relative_to(isolated).parts:
            raise AssertionError("A raw set must not read project or runtime records as artifacts.")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", artifact_reads)
    with pytest.raises(ValueError, match="[Mm]etadata|[Aa]rtifact|[Ss]pecification"):
        merge.inspect_spec_sets(raw(source, destination))
    assert real_read(record) == b'{"private":"project record"}'
    monkeypatch.setattr(Path, "read_bytes", real_read)
    for root in (destination / ".specify", destination / ".specify/workflows/runs"):
        with pytest.raises(ValueError, match="[Mm]etadata|[Aa]rtifact|[Ss]pecification"):
            merge.inspect_spec_sets(raw(source, root))
    assert state(isolated) == before
    clean = isolated / "clean-destination"
    clean.mkdir()
    for path in (".git/index", ".specify/project.json", ".specify/feature.json", ".specify/workflows/runs/current.json"):
        with pytest.raises(ValueError, match="[Mm]etadata|[Pp]rotected|[Aa]pproved"):
            preview_for(merge, source, clean, [{"source_feature": "001-feature", "relation": "independent"}],
                        artifacts=[replacement(path, b"Must not change metadata.\n")])
    assert list(clean.iterdir()) == []


@pytest.mark.parametrize("field", ["relationship", "correspondence", "artifact", "mode"])
def test_malformed_decision_values_return_actionable_refusals_without_writes(merge, isolated, field):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    inspection = merge.inspect_spec_sets(raw(source, destination))
    decisions = approved(inspection, [{"source_feature": "001-feature", "relation": "independent"}])
    if field == "relationship":
        decisions["relationship"] = {"relationship": ["same_project"]}
    elif field == "correspondence":
        decisions["correspondences"][0]["relation"] = ["independent"]
    elif field == "artifact":
        decisions["artifact_decisions"] = [{"path": "001-feature/spec.md", "resolution": ["copy_source"]}]
    else:
        decisions["artifact_decisions"] = [{"path": "001-feature/spec.md", "resolution": "copy_source", "mode": True}]
    before = state(isolated)
    with pytest.raises(ValueError):
        merge.prepare_spec_merge(inspection, decisions)
    assert state(isolated) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support colons in directory names.")
def test_tracking_uses_literal_paths_inside_the_selected_git_repository(merge, isolated, monkeypatch):
    repository = isolated / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    source = repository / ":(top)selected"
    put(source, "001-feature/spec.md", "# Selected literal path\n")
    put(repository, "selected/001-feature/spec.md", "# Not the selected path\n")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "Record literal and misleading paths")
    destination = isolated / "destination"
    destination.mkdir()
    before = state(isolated)
    monkeypatch.delenv("GIT_LITERAL_PATHSPECS", raising=False)
    inspected = merge.inspect_spec_sets(raw(source, destination))
    assert inspected.source.files["001-feature/spec.md"].content == b"# Selected literal path\n"
    assert inspected.source.tracking == {"001-feature/spec.md": "tracked"}
    assert inspected.source.tracking_context["repository"] == str(repository.resolve())
    assert state(isolated) == before


@pytest.mark.skipif(os.name == "nt", reason="Windows does not support a mode without owner read access.")
@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_approved_unreadable_mode_applies_or_restores_the_saved_original(merge, isolated, monkeypatch, fail_after_replace):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    target = put(destination, "001-feature/spec.md", "# Original destination\n")
    before = state(isolated)
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[replacement("001-feature/spec.md", b"# Approved replacement\n", mode=0, mtime_ns=0)],
    )
    real_replace = os.replace
    failed = False

    def replace_then_fail(source_path, destination_path):
        nonlocal failed
        real_replace(source_path, destination_path)
        if Path(destination_path) == target and not failed:
            failed = True
            raise OSError("Injected failure after the artifact replacement.")

    if fail_after_replace:
        monkeypatch.setattr(os, "replace", replace_then_fail)
    result = merge.apply_spec_merge(preview)
    assert "001-feature/spec.md" in result.changed_paths
    assert result.cleanup_required is False
    assert not result.remaining_operations
    if fail_after_replace:
        assert result.data_outcome == "completely_restored"
        assert state(isolated) == before
    else:
        assert result.data_outcome == "applied"
        assert stat.S_IMODE(target.stat().st_mode) == 0
        assert target.stat().st_mtime_ns == 0
        target.chmod(0o600)
        assert target.read_bytes() == b"# Approved replacement\n"


def test_recorded_branch_transfer_copies_supported_metadata_without_git_writes(merge, isolated):
    repository = recorded_source(isolated)
    put(repository, "specs/001-feature/spec.md", "# Uncommitted content must not transfer\n")
    destination = isolated / "destination"
    destination.mkdir()
    native_file = put(isolated, "native-mode-reference", b"")
    native_mode = stat.S_IMODE(native_file.stat().st_mode)
    source_before = state(repository)
    inspected = merge.inspect_spec_sets({**raw(repository, destination), "source_kind": "branch", "source_branch": "main"})
    preview = merge.prepare_spec_merge(inspected, approved(inspected, [{
        "source_feature": "001-feature", "relation": "independent",
    }]))
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    target = destination / "001-feature/spec.md"
    marker = destination / "001-feature/.merge-specs.json"
    assert target.read_bytes() == b"# Recorded specification\n"
    assert stat.S_IMODE(target.stat().st_mode) == native_mode
    assert stat.S_IMODE(marker.stat().st_mode) == native_mode
    assert target.stat().st_mtime_ns == marker.stat().st_mtime_ns == 0
    assert state(repository) == source_before


@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_readonly_destination_replacement_restores_supported_original_metadata(merge, isolated, monkeypatch, fail_after_replace):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source replacement\n")
    target = put(destination, "001-feature/spec.md", "# Original readonly destination\n")
    target.chmod(0o444)
    os.utime(target, ns=(0, 0))
    before = state(isolated)
    preview = preview_for(merge, source, destination, [revision()],
                          artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}])
    real_replace = os.replace
    failed = False

    def replace_then_fail(source_path, destination_path):
        nonlocal failed
        real_replace(source_path, destination_path)
        if Path(destination_path) == target and not failed:
            failed = True
            raise OSError("Injected failure after the readonly artifact replacement.")

    if fail_after_replace:
        monkeypatch.setattr(os, "replace", replace_then_fail)
    result = merge.apply_spec_merge(preview)
    assert result.cleanup_required is False
    assert not result.remaining_operations
    if fail_after_replace:
        assert result.data_outcome == "completely_restored"
        assert state(isolated) == before
    else:
        assert result.data_outcome == "applied"
        assert target.read_bytes() == b"# Source replacement\n"
        assert stat.S_IMODE(target.stat().st_mode) == stat.S_IMODE((source / "001-feature/spec.md").stat().st_mode)


def test_root_artifact_transfer_needs_relationship_evidence_but_noop_does_not(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "notes.md", "# Project notes\n")
    destination.mkdir()
    inspected = merge.inspect_spec_sets(raw(source, destination))
    before = state(isolated)
    refused = merge.prepare_spec_merge(inspected, {"relationship": {"relationship": "same_project"}})
    assert any(row["kind"] == "relationship" for row in refused.conflicts)
    assert merge.apply_spec_merge(refused).data_outcome == "unchanged"
    assert state(isolated) == before
    approved_preview = merge.prepare_spec_merge(inspected, approved(inspected, []))
    assert merge.apply_spec_merge(approved_preview).data_outcome == "applied"
    before = state(isolated)
    noop = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    assert not noop.conflicts
    assert merge.apply_spec_merge(noop).data_outcome == "unchanged"
    assert state(isolated) == before


@pytest.mark.parametrize("decision", [None, {"path": "010-combined/shared.bin", "resolution": "copy_source"}])
def test_same_target_differing_source_contributions_require_explicit_resolution(merge, isolated, decision):
    source, destination = isolated / "source", isolated / "destination"
    for feature, content in (("001-a", b"A"), ("002-b", b"B")):
        put(source, f"{feature}/spec.md", "# Shared\n")
        put(source, f"{feature}/shared.bin", content)
    put(destination, "010-combined/spec.md", "# Shared\n")
    before = state(isolated)
    preview = preview_for(
        merge, source, destination, [revision("001-a", "010-combined"), revision("002-b", "010-combined")],
        artifacts=[] if decision is None else [decision],
    )
    assert any(row["kind"] == "content" and row["path"] == "010-combined/shared.bin" for row in preview.conflicts)
    assert merge.apply_spec_merge(preview).transfer == "refused"
    assert state(isolated) == before


@pytest.mark.parametrize("resolution", ["keep_destination", "replace", "copy_a", "copy_b"])
@pytest.mark.parametrize("additive", [False, True])
def test_same_target_source_contributions_use_reviewed_resolution_and_keep_all_origins(merge, isolated, resolution, additive):
    source, destination = isolated / "source", isolated / "destination"
    for feature, content in (("001-a", b"A"), ("002-b", b"B")):
        put(source, f"{feature}/spec.md", "# Shared\n")
        put(source, f"{feature}/shared.bin", content)
        if additive:
            put(source, f"{feature}/{feature}.txt", feature)
    put(destination, "010-combined/spec.md", "# Shared\n")
    put(destination, "010-combined/shared.bin", b"Destination")
    if resolution == "replace":
        choice = replacement("010-combined/shared.bin", b"A and B")
        expected = b"A and B"
    elif resolution == "keep_destination":
        choice = {"path": "010-combined/shared.bin", "resolution": "keep_destination"}
        expected = b"Destination"
    else:
        selected = "001-a" if resolution == "copy_a" else "002-b"
        choice = {"path": "010-combined/shared.bin", "resolution": "copy_source", "source_path": f"{selected}/shared.bin"}
        expected = b"A" if resolution == "copy_a" else b"B"
    source_before = state(source)
    preview = preview_for(
        merge, source, destination, [revision("001-a", "010-combined"), revision("002-b", "010-combined")],
        artifacts=[choice],
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert (destination / "010-combined/shared.bin").read_bytes() == expected
    if additive:
        assert (destination / "010-combined/001-a.txt").read_bytes() == b"001-a"
        assert (destination / "010-combined/002-b.txt").read_bytes() == b"002-b"
    marker = json.loads((destination / "010-combined/.merge-specs.json").read_bytes())
    assert {row["source_feature"] for row in marker["origins"]} == {"001-a", "002-b"}
    assert state(source) == source_before
    before = state(isolated)
    repeated = merge.prepare_spec_merge(merge.inspect_spec_sets(raw(source, destination)), {})
    assert not repeated.conflicts
    assert merge.apply_spec_merge(repeated).data_outcome == "unchanged"
    assert state(isolated) == before


def test_same_target_nonconflicting_source_contributions_join_without_resolution(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    for feature in ("001-a", "002-b"):
        put(source, f"{feature}/spec.md", "# Shared\n")
        put(source, f"{feature}/shared.bin", b"\xffsame\r\n")
        put(source, f"{feature}/{feature}.txt", feature)
    put(destination, "010-combined/spec.md", "# Shared\n")
    preview = preview_for(
        merge, source, destination, [revision("001-a", "010-combined"), revision("002-b", "010-combined")],
    )
    assert not preview.conflicts
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    assert (destination / "010-combined/shared.bin").read_bytes() == b"\xffsame\r\n"
    assert (destination / "010-combined/001-a.txt").read_bytes() == b"001-a"
    assert (destination / "010-combined/002-b.txt").read_bytes() == b"002-b"
    marker = json.loads((destination / "010-combined/.merge-specs.json").read_bytes())
    assert {row["source_feature"] for row in marker["origins"]} == {"001-a", "002-b"}


def test_implicit_source_replacements_only_affect_the_reviewed_source_artifact(merge, isolated):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-a/spec.md", "# A\n[spec](source-target)\n")
    put(source, "002-b/spec.md", "# B without the selected reference\n")
    put(destination, "010-a/spec.md", "# Existing A\n")
    rows = [revision("001-a", "010-a"), {"source_feature": "002-b", "relation": "independent"}]
    choice = {"path": "010-a/spec.md", "resolution": "copy_source",
              "replacements": [{"old": "source-target", "new": "destination-target", "count": 1}]}
    before = state(isolated)
    implicit = preview_for(merge, source, destination, rows, artifacts=[choice])
    explicit = preview_for(
        merge, source, destination, rows, artifacts=[{**choice, "source_path": "001-a/spec.md"}],
    )
    assert not implicit.conflicts
    assert not explicit.conflicts
    assert [(row.path, row.content) for row in implicit.operations] == [(row.path, row.content) for row in explicit.operations]
    assert state(isolated) == before
    assert merge.apply_spec_merge(implicit).data_outcome == "applied"
    assert (destination / "010-a/spec.md").read_bytes() == b"# A\n[spec](destination-target)\n"
    assert (destination / "002-b/spec.md").read_bytes() == b"# B without the selected reference\n"


@pytest.mark.parametrize("explicit", [False, True])
def test_selected_source_reference_occurrence_count_still_refuses(merge, isolated, explicit):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-a/spec.md", "# A\nsource-target\n")
    put(source, "002-b/spec.md", "# B\n")
    put(destination, "010-a/spec.md", "# Existing A\n")
    choice = {"path": "010-a/spec.md", "resolution": "copy_source",
              "replacements": [{"old": "source-target", "new": "destination-target", "count": 2}]}
    if explicit:
        choice["source_path"] = "001-a/spec.md"
    before = state(isolated)
    with pytest.raises(ValueError, match="occurrence count"):
        preview_for(
            merge, source, destination,
            [revision("001-a", "010-a"), {"source_feature": "002-b", "relation": "independent"}],
            artifacts=[choice],
        )
    assert state(isolated) == before


@pytest.mark.parametrize("with_feature", [False, True])
def test_source_root_custom_marker_bytes_require_preservation_and_are_not_generated_state(merge, isolated, with_feature):
    source, destination = isolated / "source", isolated / "destination"
    custom = b"\xffCUSTOM source-root bytes\r\n"
    root_marker = put(source, ".merge-specs.json", custom)
    rows = []
    if with_feature:
        put(source, "001-a/spec.md", "# A\n")
        rows = [{"source_feature": "001-a", "destination_feature": "001-a", "relation": "independent"}]
    destination.mkdir()
    inspection = merge.inspect_spec_sets(raw(source, destination))
    assert set(inspection.source.markers) == {".merge-specs.json"}
    assert inspection.source.files[".merge-specs.json"].content == custom
    if not with_feature:
        assert inspection.source.features == ()
    before = state(isolated)
    unresolved = merge.prepare_spec_merge(inspection, approved(inspection, rows))
    assert any(row["kind"] == "delivery_state" and row["path"] == ".merge-specs.json" for row in unresolved.conflicts)
    assert all(operation.content != custom for operation in unresolved.operations)
    if with_feature:
        generated = next(operation for operation in unresolved.operations if operation.path == "001-a/.merge-specs.json")
        assert set(json.loads(generated.content)) == {"origins", "destination_state_digest"}
        assert generated.content != custom
    assert merge.apply_spec_merge(unresolved).transfer == "refused"
    assert state(isolated) == before
    choice = {"path": ".merge-specs.json", "source_path": ".merge-specs.json",
              "resolution": "preserve_marker", "preserve_path": "source-root-custom.bin"}
    reviewed = merge.prepare_spec_merge(inspection, approved(inspection, rows, artifact_decisions=[choice]))
    assert not reviewed.conflicts
    assert merge.apply_spec_merge(reviewed).data_outcome == "applied"
    assert (destination / "source-root-custom.bin").read_bytes() == custom
    assert root_marker.read_bytes() == custom
    assert not (destination / ".merge-specs.json").exists()
    if with_feature:
        assert (destination / "001-a/spec.md").read_bytes() == b"# A\n"
        assert (destination / "001-a/.merge-specs.json").read_bytes() != custom
    else:
        assert not list(destination.glob("*/.merge-specs.json"))


def test_valid_feature_local_source_marker_stays_excluded_from_artifact_transfer(merge, isolated):
    upstream, source, destination = isolated / "upstream", isolated / "source", isolated / "destination"
    put(upstream, "001-feature/spec.md", "# Feature\n")
    source.mkdir()
    delivered(merge, upstream, source)
    valid_state = (source / "001-feature/.merge-specs.json").read_bytes()
    destination.mkdir()
    source_before = state(source)
    preview = preview_for(
        merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}],
    )
    assert not preview.conflicts
    assert all(operation.content != valid_state for operation in preview.operations)
    assert merge.apply_spec_merge(preview).data_outcome == "applied"
    current = json.loads((destination / "001-feature/.merge-specs.json").read_bytes())
    assert current["origins"][0]["source"]["spec_root"] == str(source)
    assert (destination / "001-feature/spec.md").read_bytes() == b"# Feature\n"
    assert state(source) == source_before


def test_foreign_directory_created_before_import_mkdir_has_no_attempt_removal_action(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    source_before = state(source)
    target = destination / "001-feature"
    real_mkdir = Path.mkdir
    foreign = {}

    def mkdir_after_foreign_writer(path, *args, **kwargs):
        if path == target:
            real_mkdir(path)
            (path / "foreign.txt").write_bytes(b"Foreign writer bytes\n")
            foreign.update(state(path))
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir_after_foreign_writer)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "completely_restored"
    assert result["review"] == "not_performed"
    assert result["changed_paths"] == []
    assert result["remaining_operations"] == []
    assert result["originals"] == []
    assert result["recovery_directory"] is None
    assert not result["cleanup_required"]
    assert state(target) == foreign
    assert state(source) == source_before
    assert not (destination / ".merge-specs.lock").exists()
    assert not list(destination.glob(".merge-specs-recovery-*"))


def test_foreign_replacement_of_successfully_created_directory_retains_owned_recovery_scope(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    destination.mkdir()
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    target = destination / "001-feature"
    real_replace = os.replace
    foreign = {}

    def replace_after_directory_writer(source_path, destination_path):
        if Path(destination_path) == target / ".merge-specs.json":
            target.rename(isolated / "attempt-created-directory")
            target.mkdir()
            put(target, "foreign.txt", b"Foreign replacement bytes\n")
            foreign.update(state(target))
            raise OSError("Injected write failure after foreign directory replacement.")
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", replace_after_directory_writer)
    result = merge.apply_spec_merge(preview).to_json()
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "recovery_required"
    assert result["review"] == "incomplete_review"
    assert [(row["operation"], row["path"]) for row in result["remaining_operations"]] == [
        ("inspect-directory", "001-feature"),
    ]
    assert result["originals"] == []
    assert state(target) == foreign
    recovery = Path(result["recovery_directory"])
    journal = json.loads((recovery / "journal.json").read_text())
    assert journal["remaining_operations"] == result["remaining_operations"]


@pytest.mark.parametrize("phase", ["staging", "journal", "rollback", "cleanup"])
def test_replaced_recovery_directory_preserves_foreign_bytes_and_saved_originals(merge, isolated, monkeypatch, phase):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"# Source\r\n")
    put(destination, "001-feature/spec.md", b"# Original\r\n")
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}],
    )
    recovery = destination / preview.temporary_resources["recovery_directory"]
    displaced = isolated / "owned-recovery"
    foreign_bytes = b"Foreign recovery journal\x00\xff\r\n"
    source_before = state(source)
    real_mkdir, real_replace, real_utime = Path.mkdir, os.replace, os.utime

    def replace_recovery():
        recovery.rename(displaced)
        real_mkdir(recovery)
        put(recovery, "journal.json", foreign_bytes)

    def mkdir(path, *args, **kwargs):
        result = real_mkdir(path, *args, **kwargs)
        if phase == "staging" and path == recovery / "staged":
            replace_recovery()
        return result

    def replace(source_path, destination_path):
        result = real_replace(source_path, destination_path)
        if phase == "journal" and Path(destination_path) == destination / "001-feature/spec.md":
            replace_recovery()
        if phase == "rollback" and Path(destination_path) == destination / "001-feature/.merge-specs.json":
            replace_recovery()
        return result

    def utime(path, *args, **kwargs):
        result = real_utime(path, *args, **kwargs)
        if phase == "cleanup" and Path(path) == destination / "001-feature" and not displaced.exists():
            replace_recovery()
        return result

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "utime", utime)
    result = merge.apply_spec_merge(preview).to_json()

    assert (recovery / "journal.json").read_bytes() == foreign_bytes
    assert sorted(path.name for path in recovery.iterdir()) == ["journal.json"]
    assert state(source) == source_before
    assert result["cleanup_required"] is True
    assert result["review"] == "incomplete_review"
    assert result["originals"] == []
    assert result["recovery_directory"] is None
    assert any(row["operation"] == "inspect-recovery-directory" and row["path"] == str(recovery)
               for row in result["remaining_operations"])
    assert not any(row["operation"] == "remove-recovery-directory" for row in result["remaining_operations"])
    assert not (destination / ".merge-specs.lock").exists()
    expected = {"staging": ("refused", "unchanged"), "journal": ("failed", "recovery_required"),
                "rollback": ("failed", "completely_restored"), "cleanup": ("completed", "applied")}
    assert (result["transfer"], result["data_outcome"]) == expected[phase]
    assert (destination / "001-feature/spec.md").read_bytes() == (
        b"# Original\r\n" if phase in {"staging", "rollback"} else b"# Source\r\n"
    )
    if phase in {"journal", "rollback"}:
        assert not (destination / "001-feature/.merge-specs.json").exists()
    if phase == "journal":
        assert any(row["operation"] == "restore-file" and row["path"] == "001-feature/spec.md"
                   for row in result["remaining_operations"])
        assert all("original" not in row for row in result["remaining_operations"])
    if phase != "staging":
        journal = json.loads((displaced / "journal.json").read_text())
        originals = [displaced / row["original"] for row in journal["operations"] if "original" in row]
        assert [path.read_bytes() for path in originals] == [b"# Original\r\n"]


@pytest.mark.parametrize("failure", ["stat", "stream", "write", "replacement"])
def test_lock_initialization_closes_descriptor_and_reports_only_owned_cleanup(merge, isolated, monkeypatch, failure):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", "# Source\n")
    put(destination, "002-retained/spec.md", "# Retained\n")
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    lock = destination / ".merge-specs.lock"
    source_before = state(source)
    retained_before = state(destination / "002-retained")
    real_open, real_fstat, real_fdopen = os.open, os.fstat, os.fdopen
    held = {}
    lock_bytes = b'{"operation":"merge-specs"}\n'

    def open_lock(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == lock:
            held["descriptor"] = descriptor
        return descriptor

    def fstat(descriptor):
        if failure == "stat" and descriptor == held.get("descriptor"):
            raise OSError("Injected lock identity capture failure.")
        return real_fstat(descriptor)

    def fdopen(descriptor, *args, **kwargs):
        if failure == "stream" and descriptor == held.get("descriptor"):
            raise OSError("Injected lock stream creation failure.")
        stream = real_fdopen(descriptor, *args, **kwargs)
        if descriptor == held.get("descriptor"):
            real_write = stream.write

            def write(content):
                if failure == "write":
                    raise OSError("Injected lock write failure.")
                count = real_write(content)
                if failure == "replacement":
                    stream.flush()
                    lock.rename(isolated / "owned-lock")
                    lock.write_bytes(lock_bytes)
                return count

            stream.write = write
        return stream

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_lock)
        patch.setattr(os, "fstat", fstat)
        patch.setattr(os, "fdopen", fdopen)
        result = merge.apply_spec_merge(preview).to_json()
    try:
        real_fstat(held["descriptor"])
    except OSError as exc:
        closed = exc.errno == errno.EBADF
    else:
        closed = False
        os.close(held["descriptor"])

    assert closed
    assert result["transfer"] == "refused"
    assert result["data_outcome"] == "unchanged"
    assert result["originals"] == []
    assert state(source) == source_before
    assert state(destination / "002-retained") == retained_before
    assert not (destination / "001-feature").exists()
    assert not list(destination.glob(".merge-specs-recovery-*"))
    if failure == "write":
        assert not lock.exists()
        assert result["remaining_operations"] == []
        assert not result["cleanup_required"]
    else:
        assert lock.read_bytes() == (lock_bytes if failure == "replacement" else b"")
        assert result["cleanup_required"] is True
        assert [(row["operation"], row["path"]) for row in result["remaining_operations"]] == [
            ("inspect-lock", str(lock)),
        ]
        inventory = merge.inspect_spec_sets(raw(None, destination)).to_json()
        assert inventory["destination"]["pending_resources"] == [".merge-specs.lock"]
        assert inventory["review"] == "incomplete_review"
        lock.unlink()  # The fixture owner clears only this verified disposable lock.
    fresh = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    assert merge.apply_spec_merge(fresh).data_outcome == "applied"
    assert (destination / "001-feature/spec.md").read_bytes() == b"# Source\n"


def test_recovery_replacement_after_rollback_staging_preserves_foreign_payloads(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"# Source\r\n")
    target = put(destination, "001-feature/spec.md", b"# Original\r\n")
    put(destination, "002-retained/spec.md", b"# Retained\n")
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}],
    )
    recovery = destination / preview.temporary_resources["recovery_directory"]
    displaced = isolated / "owned-recovery"
    source_before, retained_before = state(source), state(destination / "002-retained")
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
    result = merge.apply_spec_merge(preview).to_json()

    assert state(recovery) == foreign_state
    assert target.read_bytes() == b"# Source\r\n"
    assert state(source) == source_before
    assert state(destination / "002-retained") == retained_before
    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "recovery_required"
    assert result["cleanup_required"] is True
    assert result["review"] == "incomplete_review"
    assert result["recovery_directory"] is None
    assert result["originals"] == []
    assert [(row["operation"], row["path"]) for row in result["remaining_operations"]] == [
        ("restore-file", "001-feature/spec.md"), ("inspect-recovery-directory", str(recovery)),
    ]
    assert all("original" not in row for row in result["remaining_operations"])
    journal = json.loads((displaced / "journal.json").read_text())
    assert [(row["path"], (displaced / row["original"]).read_bytes())
            for row in journal["operations"] if "original" in row] == [
        ("001-feature/spec.md", b"# Original\r\n"),
    ]
    assert not (destination / "001-feature/.merge-specs.json").exists()
    assert not (destination / ".merge-specs.lock").exists()


def test_created_directory_identity_failure_reports_pending_inspection(merge, isolated, monkeypatch):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"# Source\r\n")
    put(destination, "002-retained/spec.md", b"# Retained\n")
    preview = preview_for(merge, source, destination, [{"source_feature": "001-feature", "relation": "independent"}])
    target = destination / "001-feature"
    source_before, retained_before = state(source), state(destination / "002-retained")
    real_mkdir, real_lstat = Path.mkdir, Path.lstat
    created, failed = False, False
    directory_metadata = None

    def mkdir(path, *args, **kwargs):
        nonlocal created, directory_metadata
        result = real_mkdir(path, *args, **kwargs)
        if path == target:
            created = True
            info = real_lstat(path)
            directory_metadata = (stat.S_IMODE(info.st_mode), info.st_mtime_ns)
        return result

    def lstat(path, *args, **kwargs):
        nonlocal failed
        if path == target and created and not failed:
            failed = True
            raise OSError("Injected created-directory identity failure.")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "lstat", lstat)
    result = merge.apply_spec_merge(preview).to_json()

    assert result["transfer"] == "failed"
    assert result["data_outcome"] == "recovery_required"
    assert result["review"] == "incomplete_review"
    assert [(row["operation"], row["path"]) for row in result["remaining_operations"]] == [
        ("inspect-directory", "001-feature"),
    ]
    assert result["originals"] == []
    assert target.is_dir() and list(target.iterdir()) == []
    assert (stat.S_IMODE(target.stat().st_mode), target.stat().st_mtime_ns) == directory_metadata
    assert state(source) == source_before
    assert state(destination / "002-retained") == retained_before
    assert not (destination / ".merge-specs.lock").exists()
    recovery = Path(result["recovery_directory"])
    journal = json.loads((recovery / "journal.json").read_text())
    assert journal["remaining_operations"] == result["remaining_operations"]
    inspection = merge.inspect_spec_sets(raw(None, destination)).to_json()
    assert inspection["review"] == "incomplete_review"
    assert inspection["destination"]["pending_resources"] == [recovery.name]


@pytest.mark.parametrize("interrupt_after", ["marker", "spec"])
def test_interrupted_transfer_preserves_originals_and_journal_for_manual_recovery(merge, isolated, monkeypatch, interrupt_after):
    source, destination = isolated / "source", isolated / "destination"
    put(source, "001-feature/spec.md", b"# Source\r\n")
    original = put(destination, "001-feature/spec.md", b"# Original\r\n")
    original.chmod(0o600)
    os.utime(original, ns=(0, 0))
    preview = preview_for(
        merge, source, destination, [revision()],
        artifacts=[{"path": "001-feature/spec.md", "resolution": "copy_source"}],
    )
    recovery = destination / preview.temporary_resources["recovery_directory"]
    marker = destination / "001-feature/.merge-specs.json"
    stop = marker if interrupt_after == "marker" else original
    source_before = state(source)
    real_replace, real_open = os.replace, os.open
    held = {}

    def replace(source_path, destination_path):
        result = real_replace(source_path, destination_path)
        if Path(destination_path) == stop:
            raise KeyboardInterrupt("Injected transfer interruption after replacement.")
        return result

    def open_lock(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if Path(path) == destination / ".merge-specs.lock":
            held["descriptor"] = descriptor
        return descriptor

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace)
        patch.setattr(os, "open", open_lock)
        with pytest.raises(KeyboardInterrupt):
            merge.apply_spec_merge(preview)

    journal = json.loads((recovery / "journal.json").read_text())
    originals = [(row["path"], (recovery / row["original"]).read_bytes())
                 for row in journal["operations"] if "original" in row]
    assert originals == [("001-feature/spec.md", b"# Original\r\n")]
    interrupted = next(row for row in journal["operations"] if row["path"] == stop.relative_to(destination).as_posix())
    assert interrupted["state"] == "attempting"
    for row in journal["operations"]:
        if row["state"] == "staged":
            assert (recovery / row["staged"]).read_bytes() == b"# Source\r\n"
    assert original.read_bytes() == (b"# Original\r\n" if interrupt_after == "marker" else b"# Source\r\n")
    assert marker.is_file()
    assert state(source) == source_before
    assert not (destination / ".merge-specs.lock").exists()
    with pytest.raises(OSError) as closed:
        os.fstat(held["descriptor"])
    assert closed.value.errno == errno.EBADF
    inspection = merge.inspect_spec_sets(raw(None, destination)).to_json()
    assert inspection["review"] == "incomplete_review"
    assert inspection["destination"]["pending_resources"] == [recovery.name]
