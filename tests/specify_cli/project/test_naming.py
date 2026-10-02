"""Naming migration invariants: allocation, ownership, staleness, and rollback."""
from __future__ import annotations

import dataclasses
import errno
import json
import os
import random
import re
import shutil
import stat
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

import pytest

SEED = 1539
MAX_NUMBER = 2**63 - 1
CLOCK = datetime(2026, 5, 6, 7, 8, 9)
STAMP = 1_700_000_000_000_000_000
RUNS = ".specify/workflows/runs"
CONTEXT_CONFIG_PATH = ".specify/extensions/agent-context/agent-context-config.yml"
CONTEXT_CONFIG = "context_files:\n  - AGENTS.md\n"
DIGEST = re.compile("[0-9a-f]{64}", re.IGNORECASE)


def put(root: Path, name: str, content: bytes | str = b"") -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def feature(root: Path, rel: str, files: dict[str, bytes | str] | None = None) -> Path:
    """Create an eligible feature: a directory with a specification and extra files."""
    put(root, f"{rel}/spec.md", f"# {rel.rsplit('/', 1)[-1]}\n")
    for name, content in (files or {}).items():
        put(root, f"{rel}/{name}", content)
    return root / rel


def tree(root: Path) -> dict[str, bytes | None]:
    """Map every path under root to its bytes (None for a directory)."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def stamps(root: Path) -> dict[str, int]:
    return {path.relative_to(root).as_posix(): path.lstat().st_mtime_ns for path in root.rglob("*")}


def outside(root: Path, *projects: Path) -> dict[str, bytes | None]:
    """Everything under root beyond the project trees. Recovery data must not outlive a success or rollback."""
    return {
        rel: content
        for rel, content in tree(root).items()
        if not any((root / rel).is_relative_to(project) for project in projects)
    }


def section(plan: str) -> str:
    """The managed agent-context section as the extension writes it."""
    return (
        "<!-- SPECKIT START -->\n"
        "For additional context about technologies to be used, project structure,\n"
        "shell commands, and other important information, read the current plan\n"
        f"at {plan}\n"
        "<!-- SPECKIT END -->\n"
    )


def rel(path: str, root: Path) -> str:
    """A reported path relative to the workspace root, however the domain writes it."""
    candidate = Path(path)
    return (candidate.relative_to(root) if candidate.is_absolute() else candidate).as_posix()


def blocked(outcome, root: Path) -> list[str]:
    return sorted(rel(conflict["path"], root) for conflict in outcome.conflicts)


def skipped(preview, root: Path) -> set[str]:
    assert all(isinstance(entry["reason"], str) and entry["reason"] for entry in preview.skipped)
    return {rel(entry["path"], root) for entry in preview.skipped}


@pytest.fixture
def naming():
    from specify_cli.project import naming as module

    return module


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_CONFIG_HOME": home / "config",
        "XDG_DATA_HOME": home / "data",
        "APPDATA": home / "config",
        "LOCALAPPDATA": home / "data",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # keeps temporary data out of the real system directory
    return tmp_path


@pytest.fixture
def local(isolated):
    repo = isolated / "code repository"
    (repo / ".specify").mkdir(parents=True)
    return repo


@pytest.fixture
def external(isolated):
    repo, workspace = isolated / "code repository", isolated / "external workspace"
    (repo / ".specify").mkdir(parents=True)
    (workspace / ".specify").mkdir(parents=True)
    identity = str(uuid4())
    put(repo, ".specify/project.json", json.dumps({"schema_version": 1, "project_id": identity, "storage": "external"}))
    put(workspace, ".specify/workspace.json", json.dumps({"schema_version": 1, "project_id": identity}))
    record = put(
        isolated / "home/data/specify/projects",
        f"{identity}.json",
        json.dumps({"schema_version": 1, "workspace": str(workspace), "active_feature": None}),
    )
    return repo, workspace, record, identity


def prepare(naming, repo: Path, target: str, clock: datetime = CLOCK):
    return naming.prepare_naming_migration(repo, target, clock=clock)


def inject(monkeypatch, name: str, within: Path, should_fail, error: BaseException | None = None) -> list:
    """Wrap os.rename or os.replace and fail the calls into `within` that should_fail(src, dst, calls) selects."""
    real, calls = getattr(os, name), []

    def wrapper(src, dst, *args, **kwargs):
        source, target = Path(src), Path(dst)
        if target.is_relative_to(within):
            calls.append((source, target))
            if should_fail(source, target, calls):
                raise error or OSError(errno.EIO, "injected failure", str(dst))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, name, wrapper)
    return calls


# --- A plain-loop reference allocator and a seeded project generator -------------------------

_TIMESTAMP = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})([0-9]{2})-(.*)")
_SEQUENTIAL = re.compile(r"([0-9]{3,})-(.*)")
SCOPES = [
    "specs",
    "specs/team a",
    "specs/team-b/nested",
    "specs/050-numbered scope",
    "specs/20260301-120003-dated scope",
    "specs/日本語",
]
SUFFIXES = ["alpha", "beta gamma", "dé-lta", "x", "long-descriptive-name-for-a-feature", "日本"]
CUSTOM = ["notes", "v2-plan", "12-two-digits", "1234", "legacy draft"]


def classify(name: str):
    """Read a directory name without using the implementation: (scheme, ordering key, suffix)."""
    match = _TIMESTAMP.fullmatch(name)
    if match:
        return "timestamp", datetime(*map(int, match.groups()[:6])), match.group(7)
    match = _SEQUENTIAL.fullmatch(name)
    if match:
        return "sequential", int(match.group(1)), match.group(2)
    return "custom", None, ""


def expected_mappings(dirs, features, target, clock):
    """Allocate with plain loops over the generator's own ground truth."""
    taken_numbers = {key for kind, key, _ in map(classify, dirs) if kind == "sequential"}
    taken_moments = {key for kind, key, _ in map(classify, dirs) if kind == "timestamp"}
    sources = []
    for path in features:
        scope, name = path.rsplit("/", 1)
        kind, key, suffix = classify(name)
        if kind not in (target, "custom"):
            sources.append((key, path, scope, suffix))
    sources.sort()
    pairs, number, moment = [], max(taken_numbers, default=0), clock
    for _, path, scope, suffix in sources:
        if target == "sequential":
            number += 1
            pairs.append((path, f"{scope}/{number:03d}-{suffix}"))
        else:
            while moment in taken_moments:
                moment += timedelta(seconds=1)
            taken_moments.add(moment)
            pairs.append((path, f"{scope}/{moment:%Y%m%d-%H%M%S}-{suffix}"))
    return pairs


def generate(rng: random.Random):
    target = rng.choice(["sequential", "timestamp"])
    entries, seen = [], set()
    for _ in range(rng.choice([0, 1, 2, 4, 8, 12])):
        scope = rng.choice(SCOPES)
        kind = rng.choice(["sequential", "sequential", "timestamp", "timestamp", "custom"])
        suffix = rng.choice(SUFFIXES)
        if kind == "sequential":
            number = rng.choice([rng.randrange(1, 13), rng.randrange(995, 1004), rng.randrange(10**6, 10**6 + 4)])
            name = f"{number:03d}-{suffix}"
        elif kind == "timestamp":
            name = f"{datetime(2026, 3, 1, 12) + timedelta(seconds=rng.randrange(30)):%Y%m%d-%H%M%S}-{suffix}"
        else:
            name = rng.choice(CUSTOM)
        path = f"{scope}/{name}"
        if path not in seen:
            seen.add(path)
            entries.append((path, rng.random() < 0.85))
    return target, entries


def build(root: Path, entries, rng: random.Random):
    """Create a generated layout. Return the feature paths and the names of all directories under specs."""
    features, dirs = [], []
    for index, (path, has_spec) in enumerate(entries):
        if has_spec:
            feature(root, path, {"plan.md": f"plan {index}\n", "contracts/blob.bin": rng.randbytes(rng.randrange(0, 64))})
            features.append(path)
        else:
            put(root, f"{path}/notes.txt", "reserved without a specification\n")
        dirs += path.split("/")[1:]
    numbering = rng.choice(["sequential", "timestamp"])
    put(root, ".specify/init-options.json", json.dumps({"feature_numbering": numbering, "feature_selection": "automatic"}))
    return features, dirs


def test_allocation_matches_the_reference_for_generated_projects(naming, isolated):
    """Invariant: every direction allocates what a simple reference allocator allocates."""
    rng, clock = random.Random(SEED), datetime(2026, 3, 1, 12, 0, 10)
    for trial in range(60):
        target, entries = generate(rng)
        root = isolated / f"trial {trial}"
        (root / ".specify").mkdir(parents=True)
        features, dirs = build(root, entries, rng)
        context = f"seed={SEED} trial={trial} target={target} entries={entries!r}"
        preview = naming.prepare_naming_migration(root, target, clock=clock)
        assert preview.conflicts == (), context
        assert list(preview.mappings) == expected_mappings(dirs, features, target, clock), context
        targets = [to.casefold() for _, to in preview.mappings]
        assert len(set(targets)) == len(targets), context


def test_applying_generated_projects_conserves_every_artifact(naming, isolated):
    """Invariant: apply renames directories and nothing else, then a second request changes nothing."""
    rng, clock = random.Random(SEED), datetime(2026, 3, 1, 12, 0, 10)
    for trial in range(15):
        target, entries = generate(rng)
        root = isolated / f"trial {trial}"
        (root / ".specify").mkdir(parents=True)
        build(root, entries, rng)
        context = f"seed={SEED} trial={trial} target={target} entries={entries!r}"
        specs = root / "specs"
        before = tree(specs) if specs.exists() else {}
        preview = naming.prepare_naming_migration(root, target, clock=clock)
        result = naming.apply_naming_migration(preview)
        assert result.status == ("applied" if preview.mappings or preview.preference_edit else "noop"), context
        expected = {}
        for path, content in before.items():
            full = f"specs/{path}"
            for old, new in preview.mappings:
                if full == old or full.startswith(old + "/"):
                    full = new + full[len(old):]
                    break
            expected[full.removeprefix("specs/")] = content
        assert (tree(specs) if specs.exists() else {}) == expected, context
        options = json.loads((root / ".specify/init-options.json").read_text())
        assert options["feature_numbering"] == target and options["feature_selection"] == "automatic", context
        again = naming.prepare_naming_migration(root, target, clock=clock)
        assert (again.mappings, again.preference_edit, again.conflicts) == ((), None, ()), context


# --- Allocation boundaries --------------------------------------------------------------------


def test_conversion_to_sequential_orders_by_time_then_path_after_the_highest_number(naming, local):
    for path in (
        "specs/20260102-030405-beta",
        "specs/20260101-000000-zeta",
        "specs/team/20260101-000000-eta",
        "specs/001-alpha",
        "specs/custom-name",
    ):
        feature(local, path)
    put(local, "specs/004-reserved/notes.txt", "no specification, but the number stays occupied\n")

    preview = prepare(naming, local, "sequential")

    assert preview.mappings == (
        ("specs/20260101-000000-zeta", "specs/005-zeta"),
        ("specs/team/20260101-000000-eta", "specs/team/006-eta"),
        ("specs/20260102-030405-beta", "specs/007-beta"),
    )
    assert skipped(preview, local) == {"specs/001-alpha", "specs/custom-name"}
    assert preview.conflicts == () and preview.preference_edit is None
    assert (preview.current_scheme, preview.target_scheme) == ("sequential", "sequential")


def test_conversion_to_timestamp_orders_by_number_then_path_and_skips_occupied_seconds(naming, local):
    for path in ("specs/002-b", "specs/001-a", "specs/team/001-a", "specs/20260401-100000-existing", "specs/custom"):
        feature(local, path)
    (local / "specs/20260401-100001-reserved").mkdir()
    put(local, ".specify/init-options.json", '{"feature_numbering": "sequential"}')
    clock = datetime(2026, 4, 1, 10, 0, 0)

    preview = prepare(naming, local, "timestamp", clock=clock)

    assert preview.mappings == (
        ("specs/001-a", "specs/20260401-100002-a"),
        ("specs/team/001-a", "specs/team/20260401-100003-a"),
        ("specs/002-b", "specs/20260401-100004-b"),
    )
    assert preview.clock == clock and preview.conflicts == ()
    assert preview.current_scheme == "sequential" and preview.preference_edit is not None


def test_sequential_numbers_continue_past_999(naming, local):
    for path in ("specs/999-top", "specs/20260101-000000-a", "specs/20260101-000001-b"):
        feature(local, path)

    preview = prepare(naming, local, "sequential")

    assert preview.mappings == (
        ("specs/20260101-000000-a", "specs/1000-a"),
        ("specs/20260101-000001-b", "specs/1001-b"),
    )


@pytest.mark.parametrize(
    ("top", "sources", "fits"),
    [(MAX_NUMBER - 2, 2, True), (MAX_NUMBER - 1, 2, False), (MAX_NUMBER, 1, False)],
)
def test_sequential_numbers_stop_at_the_signed_64_bit_limit(naming, local, top, sources, fits):
    (local / f"specs/{top}-edge").mkdir(parents=True)  # no specification: it still reserves the number
    for index in range(sources):
        feature(local, f"specs/20260101-00000{index}-f{index}")
    before = tree(local)

    preview = prepare(naming, local, "sequential")

    if fits:
        assert preview.conflicts == ()
        assert preview.mappings[-1][1] == f"specs/{MAX_NUMBER}-f{sources - 1}"
        return
    assert preview.conflicts
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected" and tree(local) == before


def test_timestamps_roll_the_calendar_and_stop_at_the_last_second(naming, local):
    for number in (1, 2, 3):
        feature(local, f"specs/00{number}-f")

    rollover = prepare(naming, local, "timestamp", clock=datetime(2026, 12, 31, 23, 59, 58, 999999))
    leap = prepare(naming, local, "timestamp", clock=datetime(2024, 2, 28, 23, 59, 59))
    last = prepare(naming, local, "timestamp", clock=datetime(9999, 12, 31, 23, 59, 59))

    assert rollover.clock == datetime(2026, 12, 31, 23, 59, 58)
    assert [to for _, to in rollover.mappings] == [
        "specs/20261231-235958-f",
        "specs/20261231-235959-f",
        "specs/20270101-000000-f",
    ]
    assert [to for _, to in leap.mappings][:2] == ["specs/20240228-235959-f", "specs/20240229-000000-f"]
    before = tree(local)
    assert last.conflicts  # only one second remains for three features
    assert naming.apply_naming_migration(last).status == "rejected" and tree(local) == before


@pytest.mark.parametrize(
    "name",
    [
        "20261301-000000-month-13",
        "20260230-120000-february-30",
        "20230229-120000-not-a-leap-year",
        "20260101-240000-hour-24",
        "20260101-000060-second-60",
        f"{MAX_NUMBER + 1}-beyond-64-bits",
    ],
)
def test_invalid_prefixes_block_application(naming, local, name):
    feature(local, "specs/001-fine")
    feature(local, f"specs/{name}")
    before = tree(local)

    preview = prepare(naming, local, "timestamp")

    assert blocked(preview, local) == [f"specs/{name}"]
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected" and blocked(result, local) == blocked(preview, local)
    assert tree(local) == before


@pytest.mark.parametrize(
    "name, expected",
    [
        ("001-alpha", ("sequential", 1, "alpha")),
        ("000-zero", ("sequential", 0, "zero")),
        (f"000{MAX_NUMBER}-edge", ("sequential", MAX_NUMBER, "edge")),
        ("20240229-235959-leap", ("timestamp", datetime(2024, 2, 29, 23, 59, 59), "leap")),
        ("99991231-235959-last", ("timestamp", datetime.max.replace(microsecond=0), "last")),
        ("12-short", ("custom", None, "12-short")),
        ("1234", ("custom", None, "1234")),
        ("notes", ("custom", None, "notes")),
    ],
)
def test_public_classifier_preserves_prefix_boundaries(naming, name, expected):
    assert naming.classify_feature_name(name) == expected


@pytest.mark.parametrize("name", [f"{MAX_NUMBER + 1}-overflow", "20230229-120000-invalid", "20260101-240000-invalid"])
def test_public_classifier_rejects_unusable_prefixes(naming, name):
    with pytest.raises(ValueError):
        naming.classify_feature_name(name)


@pytest.mark.parametrize("location", ["local", "external"])
@pytest.mark.parametrize("reserved", [".merge-specs.lock", ".merge-specs-recovery-", ".merge-specs-recovery-leftover"])
@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "pipe"])
def test_pending_merge_resources_block_naming_without_writes(naming, request, isolated, location, reserved, kind):
    project = request.getfixturevalue(location)
    repo, workspace = (project, project) if location == "local" else project[:2]
    feature(workspace, "specs/001-a", {"payload.bin": b"\x00\xff"})
    put(workspace, ".specify/init-options.json", '{"feature_numbering": "sequential"}\n')
    pending = workspace / "specs" / reserved
    if kind == "file":
        pending.write_bytes(b"")
    elif kind == "directory":
        pending.mkdir()
    elif kind == "symlink":
        try:
            pending.symlink_to(workspace / "missing-target")
        except OSError:
            pytest.skip("symlinks are unavailable")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("named pipes are unavailable")
        os.mkfifo(pending)

    def snapshot():
        result = {}
        for path in sorted(isolated.rglob("*")):
            info = path.lstat()
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
            result[path.relative_to(isolated).as_posix()] = (info.st_mode, info.st_mtime_ns, data)
        return result

    before = snapshot()
    preview = prepare(naming, repo, "timestamp")
    assert snapshot() == before
    assert any(
        conflict["code"] == "pending-merge" and rel(conflict["path"], workspace) == f"specs/{reserved}"
        for conflict in preview.conflicts
    )
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected"
    assert snapshot() == before


@pytest.mark.parametrize("reserved", [".merge-specs.lock", ".merge-specs-recovery-leftover"])
def test_pending_merge_resource_after_preview_refuses_naming(naming, local, reserved):
    feature(local, "specs/001-a")
    put(local, ".specify/init-options.json", '{"feature_numbering": "sequential"}\n')
    preview = prepare(naming, local, "timestamp")
    (local / "specs" / reserved).mkdir()
    before = tree(local)
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected"
    assert any(conflict["code"] == "pending-merge" for conflict in result.conflicts)
    assert tree(local) == before


def test_feature_merge_marker_does_not_block_naming(naming, local):
    marker = b'{"origins": [], "destination_state_digest": "retained"}\n'
    feature(local, "specs/001-a", {".merge-specs.json": marker})
    preview = prepare(naming, local, "timestamp")
    assert not preview.conflicts
    assert naming.apply_naming_migration(preview).status == "applied"
    assert (local / "specs/20260506-070809-a/.merge-specs.json").read_bytes() == marker


# --- Preview, preference, and no-op behavior --------------------------------------------------


def test_preview_writes_nothing_and_is_repeatable(naming, local, isolated):
    feature(local, "specs/001-a", {"plan.md": "see [b](../002-b/spec.md)\n", "data.bin": b"\x00\xff"})
    feature(local, "specs/002-b")
    put(local, ".specify/init-options.json", '{"feature_numbering": "sequential"}')
    before, stamped = tree(isolated), stamps(isolated)

    first, second = prepare(naming, local, "timestamp"), prepare(naming, local, "timestamp")
    later = prepare(naming, local, "timestamp", clock=CLOCK + timedelta(seconds=1))

    assert tree(isolated) == before and stamps(isolated) == stamped
    assert first == second
    assert first.repository_root == local and first.workspace_root == local and first.project_id is None
    for digest in (first.snapshot_digest, first.mapping_digest):
        assert DIGEST.fullmatch(digest)
    assert (later.snapshot_digest, later.mapping_digest) != (first.snapshot_digest, first.mapping_digest)


def test_an_empty_project_changes_only_the_preference(naming, local):
    original = {"integration": "omp", "feature_selection": "automatic", "feature_numbering": "sequential"}
    put(local, ".specify/init-options.json", json.dumps(original))
    before = tree(local)

    preview = prepare(naming, local, "timestamp")

    assert (preview.mappings, preview.reference_edits, preview.conflicts) == ((), (), ())
    assert preview.current_scheme == "sequential" and preview.preference_edit is not None
    assert naming.apply_naming_migration(preview).status == "applied"
    options = json.loads((local / ".specify/init-options.json").read_text())
    assert options == {**original, "feature_numbering": "timestamp"}
    assert set(tree(local)) == set(before)


@pytest.mark.parametrize("scheme", ["sequential", "timestamp"])
def test_requesting_the_current_scheme_is_a_noop(naming, local, scheme):
    names = {
        "sequential": ["specs/001-a", "specs/team/0100-b"],
        "timestamp": ["specs/20260101-000000-a", "specs/team/20260102-030405-b"],
    }[scheme]
    for path in (*names, "specs/custom"):
        feature(local, path)
    put(local, ".specify/init-options.json", json.dumps({"feature_numbering": scheme}))
    before, stamped = tree(local), stamps(local)

    preview = prepare(naming, local, scheme)

    assert (preview.mappings, preview.reference_edits, preview.conflicts) == ((), (), ())
    assert preview.preference_edit is None
    assert skipped(preview, local) == {*names, "specs/custom"}
    result = naming.apply_naming_migration(preview)
    assert result.status == "noop" and tree(local) == before and stamps(local) == stamped


def test_a_missing_preference_file_means_sequential_and_is_created_only_for_a_change(naming, local):
    unchanged = prepare(naming, local, "sequential")
    assert unchanged.preference_edit is None and naming.apply_naming_migration(unchanged).status == "noop"
    assert not (local / ".specify/init-options.json").exists()

    changed = prepare(naming, local, "timestamp")
    assert naming.apply_naming_migration(changed).status == "applied"
    assert json.loads((local / ".specify/init-options.json").read_text()) == {"feature_numbering": "timestamp"}


@pytest.mark.parametrize("content", ["not json", "[]", '{"feature_numbering": "bogus"}'])
def test_malformed_preference_metadata_is_refused_before_any_write(naming, local, content):
    put(local, ".specify/init-options.json", content)
    feature(local, "specs/001-a")
    before = tree(local)

    with pytest.raises((ValueError, OSError)):
        prepare(naming, local, "timestamp")

    assert tree(local) == before


def test_unsupported_schemes_and_non_projects_are_refused(naming, local, isolated):
    (isolated / "plain").mkdir()
    with pytest.raises((ValueError, OSError)):
        prepare(naming, local, "decimal")
    for path in (isolated / "plain", isolated / "missing"):
        with pytest.raises((ValueError, OSError)):
            prepare(naming, path, "timestamp")


# --- Owned references -------------------------------------------------------------------------

A_SPEC = (
    "# A\r\n"
    'Plan [plan](./plan.md#goals) and [B](../002-b/spec.md#top "title").\r\n'
    "Image ![diagram](../002-b/img/pic%20one.png) and [spaced](../003-my%20feature/spec.md#goals).\r\n"
    "[ref]: ../002-b/plan.md\r\n"
    'Angle [x](<../003-my feature/spec.md> "t") and workspace link [w](specs/002-b/spec.md).\r\n'
    "Tokens `specs/002-b/plan.md`, specs/003-my feature/spec.md and (specs/001-a).\r\n"
    "Untouched [site](https://example.com/specs/002-b) [out](../../README.md) [same](spec.md) "
    "002-b specs/002-b-extra myspecs/002-b **Feature Branch**: 002-b\r\n"
)
A_SPEC_NEW = (
    "# A\r\n"
    'Plan [plan](./plan.md#goals) and [B](../20260506-070810-b/spec.md#top "title").\r\n'
    "Image ![diagram](../20260506-070810-b/img/pic%20one.png) "
    "and [spaced](../20260506-070811-my%20feature/spec.md#goals).\r\n"
    "[ref]: ../20260506-070810-b/plan.md\r\n"
    'Angle [x](<../20260506-070811-my feature/spec.md> "t") '
    "and workspace link [w](specs/20260506-070810-b/spec.md).\r\n"
    "Tokens `specs/20260506-070810-b/plan.md`, specs/20260506-070811-my feature/spec.md "
    "and (specs/20260506-070809-a).\r\n"
    "Untouched [site](https://example.com/specs/002-b) [out](../../README.md) [same](spec.md) "
    "002-b specs/002-b-extra myspecs/002-b **Feature Branch**: 002-b\r\n"
)


def test_markdown_links_and_complete_paths_follow_renamed_features(naming, local):
    for path in ("specs/001-a", "specs/002-b", "specs/003-my feature", "specs/custom"):
        feature(local, path)
    put(local, "specs/001-a/spec.md", A_SPEC)
    put(local, "specs/001-a/plan.md", "Back to [self](../001-a/spec.md) and [sibling](./spec.md).\n")
    put(local, "specs/001-a/notes.txt", "see specs/002-b/spec.md\n")
    put(local, "specs/002-b/spec.md", "# B\n[A](../001-a/spec.md) and [out](../../README.md)\nRead specs/001-a.\n")
    put(local, "specs/003-my feature/spec.md", "# my\n[a](../001-a/spec.md)\n")
    put(local, "specs/custom/spec.md", "# custom\n[a](../001-a/spec.md#x) and [b](../002-b/plan.md)\n")
    put(local, "README.md", "See specs/002-b/spec.md\n")
    put(local, "src/app.py", "PATH = 'specs/002-b'\n")
    stamped = local / "specs/002-b/spec.md"
    os.utime(stamped, ns=(STAMP, STAMP))
    os.chmod(stamped, 0o640)

    preview = prepare(naming, local, "timestamp")

    assert preview.conflicts == ()
    edits = [e for e in preview.reference_edits if e.owner == "feature-artifact"]
    assert {e.path for e in edits} == {
        local / "specs/001-a/spec.md",
        local / "specs/001-a/plan.md",
        local / "specs/002-b/spec.md",
        local / "specs/003-my feature/spec.md",
        local / "specs/custom/spec.md",
    }
    first = next(e for e in edits if e.path == local / "specs/001-a/spec.md")
    assert first.new_path == local / "specs/20260506-070809-a/spec.md"

    assert naming.apply_naming_migration(preview).status == "applied"

    a, b, m = "20260506-070809-a", "20260506-070810-b", "20260506-070811-my feature"
    expected = {
        f"specs/{a}/spec.md": A_SPEC_NEW,
        f"specs/{a}/plan.md": f"Back to [self](../{a}/spec.md) and [sibling](./spec.md).\n",
        f"specs/{a}/notes.txt": "see specs/002-b/spec.md\n",
        f"specs/{b}/spec.md": f"# B\n[A](../{a}/spec.md) and [out](../../README.md)\nRead specs/{a}.\n",
        f"specs/{m}/spec.md": f"# my\n[a](../{a}/spec.md)\n",
        "specs/custom/spec.md": f"# custom\n[a](../{a}/spec.md#x) and [b](../{b}/plan.md)\n",
        "README.md": "See specs/002-b/spec.md\n",
        "src/app.py": "PATH = 'specs/002-b'\n",
    }
    for path, text in expected.items():
        assert (local / path).read_bytes() == text.encode(), path
    for doc, dest in [
        (f"specs/{a}/spec.md", f"../{b}/spec.md"),
        (f"specs/{a}/spec.md", f"../{m}/spec.md"),
        (f"specs/{a}/plan.md", f"../{a}/spec.md"),
        ("specs/custom/spec.md", f"../{a}/spec.md"),
    ]:
        assert (local / doc).parent.joinpath(unquote(dest)).resolve().is_file(), (doc, dest)
    moved = (local / f"specs/{b}/spec.md").stat()
    assert moved.st_mtime_ns == STAMP
    assert moved.st_mode & 0o777 == 0o640


def test_a_link_that_reads_two_ways_blocks_application(naming, local):
    feature(local, "specs/001-a", {"plan.md": "[b](specs/002-b/plan.md)\n"})
    feature(local, "specs/002-b")
    put(local, "specs/001-a/specs/002-b/plan.md", "the same text also resolves from the document\n")
    before = tree(local)

    preview = prepare(naming, local, "timestamp")

    assert blocked(preview, local) == ["specs/001-a/plan.md"]
    assert naming.apply_naming_migration(preview).status == "rejected" and tree(local) == before


@pytest.mark.parametrize("mode", ["context", "automatic"])
def test_a_saved_local_pointer_follows_its_renamed_feature(naming, local, mode):
    from specify_cli.workspace import resolve_project

    feature(local, "specs/001-a")
    feature(local, "specs/002-b")
    put(local, ".specify/init-options.json", json.dumps({"feature_selection": mode, "feature_numbering": "sequential"}))
    put(local, ".specify/feature.json", '{ "feature_directory": "specs/002-b", "kept": [1, 2] }\n')

    preview = prepare(naming, local, "timestamp")

    new = dict(preview.mappings)["specs/002-b"]
    assert [(e.owner, e.location) for e in preview.reference_edits if e.owner == "selection"] == [
        ("selection", "/feature_directory")
    ]
    assert naming.apply_naming_migration(preview).status == "applied"
    assert (local / ".specify/feature.json").read_text() == f'{{ "feature_directory": "{new}", "kept": [1, 2] }}\n'
    assert json.loads((local / ".specify/init-options.json").read_text())["feature_selection"] == mode
    if mode == "automatic":
        assert resolve_project(local).feature_dir == local / new


@pytest.mark.parametrize("mode", ["context", "automatic"])
@pytest.mark.parametrize(
    "pointer",
    [None, '{"feature_directory": null}', '{"feature_directory": "specs/custom"}', '{"feature_directory": "specs/gone"}'],
    ids=["absent", "null", "unrelated", "missing"],
)
def test_other_local_pointers_are_never_created_or_changed(naming, local, monkeypatch, pointer, mode):
    feature(local, "specs/001-a")
    feature(local, "specs/custom")
    put(local, ".specify/init-options.json", json.dumps({"feature_selection": mode, "feature_numbering": "sequential"}))
    path = local / ".specify/feature.json"
    if pointer is not None:
        put(local, ".specify/feature.json", pointer)
    monkeypatch.setenv("SPECIFY_FEATURE_DIRECTORY", "specs/001-a")  # an invocation context neither limits nor is saved
    before = path.read_bytes() if pointer is not None else None

    preview = prepare(naming, local, "timestamp")

    assert [m[0] for m in preview.mappings] == ["specs/001-a"]
    assert naming.apply_naming_migration(preview).status == "applied"
    assert (path.read_bytes() if path.exists() else None) == before
    assert json.loads((local / ".specify/init-options.json").read_text())["feature_selection"] == mode


@pytest.mark.parametrize("mode", ["context", "automatic"])
@pytest.mark.parametrize("saved", ["mapped", "absent", "null", "unrelated"])
def test_external_selection_follows_a_renamed_feature_without_copies(naming, external, saved, mode):
    from specify_cli.workspace import resolve_project

    repo, workspace, record, identity = external
    feature(workspace, "specs/001-a")
    feature(workspace, "specs/team/002-b")
    feature(workspace, "specs/custom")
    put(workspace, ".specify/init-options.json", json.dumps({"feature_selection": mode, "feature_numbering": "sequential"}))
    content = {"schema_version": 1, "workspace": str(workspace), "keep": "me"}
    if saved != "absent":
        content["active_feature"] = {"mapped": "specs/team/002-b", "null": None, "unrelated": "specs/custom"}[saved]
    original = json.dumps(content)
    record.write_text(original)

    preview = prepare(naming, repo, "timestamp")

    assert preview.project_id == identity
    assert (preview.repository_root, preview.workspace_root) == (repo.resolve(), workspace.resolve())
    assert naming.apply_naming_migration(preview).status == "applied"
    new = dict(preview.mappings)["specs/team/002-b"]
    assert (workspace / new / "spec.md").is_file() and not (repo / "specs").exists()
    if saved == "mapped":
        assert json.loads(record.read_text()) == json.loads(original.replace("specs/team/002-b", new))
        if mode == "automatic":
            assert resolve_project(repo).feature_dir == workspace / new
    else:
        assert record.read_text() == original
    assert json.loads((workspace / ".specify/init-options.json").read_text())["feature_selection"] == mode


def run(repo: Path, run_id: str, status: str, workflow_dir: Path | None, **owner) -> Path:
    """Save a run through the public RunState, then add the sibling files a real run leaves."""
    from specify_cli.workflows.base import RunStatus
    from specify_cli.workflows.engine import RunState

    state = RunState(run_id=run_id, workflow_id="demo-flow", project_root=repo, **owner)
    state.status = RunStatus(status)
    state.workflow_dir = str(workflow_dir) if workflow_dir else None
    state.step_results = {"plan": {"status": "completed", "output": {"path": "specs/001-a/plan.md"}}}
    state.inputs = {"spec": "specs/001-a/spec.md"}
    state.save()
    directory = repo / RUNS / run_id
    put(directory, "workflow.yml", "workflow:\n  id: demo-flow\n# specs/001-a/workflows\n")
    put(directory, "log.jsonl", '{"event": "x", "path": "specs/001-a"}\n')
    return directory


def test_only_paused_and_failed_runs_inside_renamed_features_get_a_new_workflow_dir(naming, local):
    from specify_cli.workflows.engine import RunState

    feature(local, "specs/001-a", {"workflows/workflow.yml": "workflow: {}\n"})
    feature(local, "specs/002-b", {"workflows/workflow.yml": "workflow: {}\n"})
    other = local.parent / "other project"
    (other / ".specify").mkdir(parents=True)
    runs = {
        "paused-in": run(local, "paused-in", "paused", local / "specs/001-a/workflows"),
        "failed-in": run(local, "failed-in", "failed", local / "specs/002-b/workflows"),
        "completed-in": run(local, "completed-in", "completed", local / "specs/001-a/workflows"),
        "paused-out": run(local, "paused-out", "paused", local / ".specify/workflows/library"),
        "paused-foreign": run(
            local, "paused-foreign", "paused", other / "specs/001-a/workflows",
            installed_workflow_id="demo-flow", installed_registry_root=str(other),
        ),
    }
    before = {name: tree(path) for name, path in runs.items()}

    preview = prepare(naming, local, "timestamp")

    owned = [e for e in preview.reference_edits if e.owner == "workflow-resource"]
    assert preview.conflicts == ()
    assert sorted(e.path.parent.name for e in owned) == ["failed-in", "paused-in"]
    assert {e.location for e in owned} == {"/workflow_dir"}
    notices = [n for n in preview.caller_notices if n["kind"] == "opaque-workflow-value"]
    places = {(Path(n["path"]).parent.name, Path(n["path"]).name) for n in notices}
    assert {
        ("completed-in", "inputs.json"), ("completed-in", "state.json"),
        ("completed-in", "workflow.yml"), ("completed-in", "log.jsonl"),
    } <= places
    assert all("specs/001-a" not in n["message"] + (n["location"] or "") for n in notices)

    assert naming.apply_naming_migration(preview).status == "applied"

    new = dict(preview.mappings)
    for name, path in runs.items():
        after = tree(path)
        if name in ("paused-in", "failed-in"):
            old_rel = "specs/001-a" if name == "paused-in" else "specs/002-b"
            old, replacement = f'"{old_rel}/workflows"'.encode(), f'"{new[old_rel]}/workflows"'.encode()
            assert after["state.json"] == before[name]["state.json"].replace(old, replacement), name
            after["state.json"] = before[name]["state.json"]
        assert after == before[name], name  # inputs, cached definitions, logs, and other fields stay verbatim
    state = RunState.load("paused-in", local)
    assert state.workflow_dir == str(local / new["specs/001-a"] / "workflows") and state.status.value == "paused"


def test_affected_running_and_malformed_runs_block_application_but_unrelated_ones_do_not(naming, local):
    feature(local, "specs/001-a", {"workflows/workflow.yml": "workflow: {}\n"})
    inside = local / "specs/001-a/workflows"
    run(local, "running-in", "running", inside)
    run(local, "running-out", "running", local / ".specify/workflows/library")
    broken = run(local, "broken-in", "paused", inside)
    state = json.loads((broken / "state.json").read_text())
    state["step_results"] = []
    (broken / "state.json").write_text(json.dumps(state))
    put(local, f"{RUNS}/truncated-in/state.json", '{"workflow_dir": "specs/001-a/workflows"')
    put(local, f"{RUNS}/garbled-out/state.json", "{not json")

    preview = prepare(naming, local, "timestamp")

    assert blocked(preview, local) == [
        f"{RUNS}/broken-in/state.json",
        f"{RUNS}/running-in/state.json",
        f"{RUNS}/truncated-in/state.json",
    ]
    before = tree(local)
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected" and tree(local) == before


def test_agent_context_edits_only_the_owned_section_of_configured_files(naming, local):
    feature(local, "specs/001-a")
    put(local, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    outside_text = "Outside: specs/001-a/plan.md\n"
    put(local, "AGENTS.md", f"# Agents\n{outside_text}{section('specs/001-a/plan.md')}{outside_text}")
    put(local, "CLAUDE.md", f"# Claude\n{section('specs/001-a/plan.md')}")
    producer = put(local, ".specify/integration.json", '{"integration": "omp", "note": "specs/001-a/plan.md"}\n')
    producer_before = producer.read_bytes()

    preview = prepare(naming, local, "timestamp")

    assert [(e.owner, e.path, e.new_path) for e in preview.reference_edits] == [
        ("agent-context", local / "AGENTS.md", local / "AGENTS.md")
    ]
    assert naming.apply_naming_migration(preview).status == "applied"
    new = dict(preview.mappings)["specs/001-a"]
    assert (local / "AGENTS.md").read_text() == f"# Agents\n{outside_text}{section(f'{new}/plan.md')}{outside_text}"
    assert (local / "CLAUDE.md").read_text() == f"# Claude\n{section('specs/001-a/plan.md')}"
    assert producer.read_bytes() == producer_before


def test_agent_context_without_a_configured_file_uses_the_extension_defaults(naming, local):
    feature(local, "specs/001-a")
    put(local, CONTEXT_CONFIG_PATH, "context_file: ''\ncontext_files: []\n")
    put(local, ".specify/extensions/agent-context/agent-context-defaults.json", '{"agents": {"claude": "CLAUDE.md"}}')
    put(local, ".specify/init-options.json", '{"integration": "claude", "feature_numbering": "sequential"}')
    put(local, "CLAUDE.md", section("specs/001-a/plan.md"))
    put(local, "AGENTS.md", section("specs/001-a/plan.md"))

    preview = prepare(naming, local, "timestamp")
    assert naming.apply_naming_migration(preview).status == "applied"

    new = dict(preview.mappings)["specs/001-a"]
    assert (local / "CLAUDE.md").read_text() == section(f"{new}/plan.md")
    assert (local / "AGENTS.md").read_text() == section("specs/001-a/plan.md")


@pytest.mark.parametrize("kind", ["absolute", "parent", "backslash", "drive"])
def test_agent_context_paths_that_leave_the_repository_block_application(naming, local, isolated, kind):
    feature(local, "specs/001-a")
    elsewhere = put(isolated, "outside.md", section("specs/001-a/plan.md"))
    target = {"absolute": str(elsewhere), "parent": "../outside.md", "backslash": "dir\\file.md", "drive": "C:file.md"}[kind]
    put(local, CONTEXT_CONFIG_PATH, json.dumps({"context_files": [target]}))
    before, outer = tree(local), elsewhere.read_bytes()

    preview = prepare(naming, local, "timestamp")

    assert preview.conflicts and not any(e.owner == "agent-context" for e in preview.reference_edits)
    assert naming.apply_naming_migration(preview).status == "rejected"
    assert tree(local) == before and elsewhere.read_bytes() == outer


def test_without_agent_context_configuration_nothing_is_edited_or_created(naming, local):
    feature(local, "specs/001-a")
    put(local, "AGENTS.md", section("specs/001-a/plan.md"))

    preview = prepare(naming, local, "timestamp")

    assert not any(e.owner == "agent-context" for e in preview.reference_edits)
    assert naming.apply_naming_migration(preview).status == "applied"
    assert (local / "AGENTS.md").read_text() == section("specs/001-a/plan.md")
    assert not (local / ".specify/extensions").exists()


def test_external_context_edits_the_invoking_repository_and_reports_other_callers(naming, external):
    repo, workspace, record, identity = external
    feature(workspace, "specs/001-a")
    put(workspace, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    root = workspace.resolve().as_posix()
    put(repo, "AGENTS.md", f"# Agents\n{section(f'{root}/specs/001-a/plan.md')}")

    preview = prepare(naming, repo, "timestamp")

    assert naming.apply_naming_migration(preview).status == "applied"
    new = dict(preview.mappings)["specs/001-a"]
    assert (repo / "AGENTS.md").read_text() == f"# Agents\n{section(f'{root}/{new}/plan.md')}"
    assert not (repo / "specs").exists()
    assert {"external-caller", "other-worktree"} <= {n["kind"] for n in preview.caller_notices}


def test_two_worktrees_share_one_feature_set_and_only_one_migration_applies(naming, external):
    repo, workspace, record, identity = external
    second = repo.parent / "second worktree"
    put(second, ".specify/project.json", (repo / ".specify/project.json").read_bytes())
    feature(workspace, "specs/001-a")
    feature(workspace, "specs/002-b")

    first, other = prepare(naming, repo, "timestamp"), prepare(naming, second, "timestamp")

    assert first.mappings == other.mappings and first.mappings
    assert first.workspace_root == other.workspace_root == workspace.resolve()
    assert first.project_id == other.project_id == identity
    assert first.repository_root != other.repository_root and first.snapshot_digest != other.snapshot_digest
    assert naming.apply_naming_migration(first).status == "applied"
    assert naming.apply_naming_migration(other).status == "rejected"
    assert not (repo / "specs").exists() and not (second / "specs").exists()


def test_a_second_migration_is_refused_while_the_first_runs_and_the_workspace_is_released_after(
    naming, external, monkeypatch
):
    repo, workspace, record, identity = external
    second = repo.parent / "second worktree"
    put(second, ".specify/project.json", (repo / ".specify/project.json").read_bytes())
    feature(workspace, "specs/001-a")
    feature(workspace, "specs/002-b")
    first, other = prepare(naming, repo, "timestamp"), prepare(naming, second, "timestamp")
    entered, release, calls, finished = threading.Event(), threading.Event(), [], []
    real = os.rename

    def hold_first_rename(src, dst, *args, **kwargs):
        calls.append(dst)
        if len(calls) == 1:
            entered.set()
            assert release.wait(30)
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", hold_first_rename)
    worker = threading.Thread(target=lambda: finished.append(naming.apply_naming_migration(first)))
    worker.start()
    try:
        assert entered.wait(30)
        held = tree(workspace)
        assert naming.apply_naming_migration(other).status == "rejected"
        assert tree(workspace) == held and len(calls) == 1
    finally:
        release.set()
        worker.join(30)

    assert [outcome.status for outcome in finished] == ["applied"]
    back = prepare(naming, repo, "sequential")
    assert back.mappings and naming.apply_naming_migration(back).status == "applied"


@pytest.mark.parametrize("failure", ["write", "close"])
def test_lock_record_failure_refuses_without_changing_project_data(naming, local, monkeypatch, failure):
    """Isolation: lock I/O failure changes no project bytes or feature paths."""
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    before = tree(local)
    real_fdopen = os.fdopen

    class FailedLockStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def write(self, value):
            if failure == "write":
                raise OSError(errno.ENOSPC, "Injected lock write failure")
            return self.stream.write(value)

        def __exit__(self, *args):
            self.stream.close()
            if failure == "close":
                raise OSError(errno.EIO, "Injected lock close failure")

    def fdopen(handle, mode, *args, **kwargs):
        stream = real_fdopen(handle, mode, *args, **kwargs)
        return FailedLockStream(stream) if mode == "w" else stream

    monkeypatch.setattr(os, "fdopen", fdopen)
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected"
    assert {row["code"] for row in result.conflicts} == {"lock-unavailable"}
    assert tree(local) == before


def test_lock_release_failure_reports_remaining_work_without_reversing_applied_changes(
    naming, local, monkeypatch
):
    """Conservation: committed artifacts survive a failed lock removal and reported recovery."""
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    lock = local / ".specify/naming-migration.lock"
    real_unlink = os.unlink

    def unlink(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError(errno.EACCES, "Injected lock removal failure", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    result = naming.apply_naming_migration(preview)
    assert result.status == "recovery_required" and result.recovery is not None
    assert {row["code"] for row in result.conflicts} == {"lock-release-failed"}
    assert all((local / new / "spec.md").is_file() and not (local / old).exists() for old, new in preview.mappings)
    assert json.loads((local / ".specify/init-options.json").read_text())["feature_numbering"] == "timestamp"
    operations = result.recovery["remaining_operations"]
    assert [(row["operation"], row["from"], row["to"]) for row in operations] == [
        ("remove-lock", str(lock), str(lock))
    ]
    recovery = Path(result.recovery["directory"])
    assert recovery.is_dir() and not recovery.is_relative_to(local)
    assert json.loads((recovery / "manifest.json").read_text())["remaining_operations"] == [dict(row) for row in operations]
    monkeypatch.setattr(os, "unlink", real_unlink)
    os.unlink(operations[0]["from"])
    assert naming.apply_naming_migration(prepare(naming, local, "timestamp")).status == "noop"
    shutil.rmtree(recovery)

# --- Unsafe layouts and stale previews --------------------------------------------------------


def _nested(root):
    feature(root, "specs/001-outer")
    feature(root, "specs/001-outer/inner")
    return "timestamp", ["specs/001-outer/inner"]


def _symlinked_file(root):
    feature(root, "specs/001-a")
    put(root, "README.md", "outside the feature\n")
    os.symlink(root / "README.md", root / "specs/001-a/link")
    return "timestamp", ["specs/001-a/link"]


def _symlinked_feature(root):
    feature(root, "elsewhere/001-a")
    (root / "specs").mkdir()
    os.symlink(root / "elsewhere/001-a", root / "specs/001-a")
    return "timestamp", ["specs/001-a"]


def _symlinked_specs(root):
    feature(root, "elsewhere/001-a")
    os.symlink(root / "elsewhere", root / "specs")
    return "timestamp", ["specs"]


def _pipe(root):
    if not hasattr(os, "mkfifo"):
        pytest.skip("named pipes are unavailable")
    feature(root, "specs/001-a")
    os.mkfifo(root / "specs/001-a/pipe")
    return "timestamp", ["specs/001-a/pipe"]


def _occupied_by_a_file(root):
    feature(root, "specs/20260101-000000-a")
    put(root, "specs/001-a", "a file owns the target name\n")
    return "sequential", None


def _case_alias(root):
    feature(root, "specs/20260101-000000-a")
    put(root, "specs/001-A", "differs from the target only by case\n")
    return "sequential", None


def _name_too_long(root):
    feature(root, f"specs/001-{'x' * 245}")
    return "timestamp", None


@pytest.mark.parametrize(
    "build_layout",
    [_nested, _symlinked_file, _symlinked_feature, _symlinked_specs, _pipe, _occupied_by_a_file, _case_alias, _name_too_long],
    ids=lambda function: function.__name__.lstrip("_"),
)
def test_unsafe_layouts_are_reported_and_block_application(naming, local, build_layout):
    try:
        target, expected = build_layout(local)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"the filesystem cannot build the layout: {exc}")
    before = tree(local)

    preview = prepare(naming, local, target)

    assert preview.conflicts
    if expected is not None:
        assert blocked(preview, local) == expected
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected" and blocked(result, local) == blocked(preview, local)
    assert tree(local) == before


def stale_project(root: Path) -> None:
    feature(root, "specs/001-a", {"plan.md": "see [b](../002-b/spec.md)\n"})
    feature(root, "specs/002-b")
    put(root, ".specify/init-options.json", '{"feature_numbering": "sequential", "feature_selection": "automatic"}\n')
    put(root, ".specify/feature.json", '{"feature_directory": "specs/001-a"}\n')
    put(root, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    put(root, "AGENTS.md", section("specs/001-a/plan.md"))


def same_size_edit(path: Path, content: bytes) -> None:
    """Change bytes without changing size or modification time."""
    before = path.stat()
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


MUTATIONS = {
    "same-size artifact": lambda r: same_size_edit(r / "specs/002-b/spec.md", b"# 002-X\n"),
    "new artifact": lambda r: put(r, "specs/001-a/extra.md", "new\n"),
    "removed artifact": lambda r: (r / "specs/001-a/plan.md").unlink(),
    "changed permissions": lambda r: os.chmod(r / "specs/002-b/spec.md", 0o444),
    "changed modification time": lambda r: os.utime(r / "specs/002-b/spec.md", ns=(STAMP, STAMP)),
    "file beside the features": lambda r: put(r, "specs/README.md", "not a feature\n"),
    "file in a non-feature directory": lambda r: put(r, "specs/scratch/notes.txt", "not a feature\n"),
    "occupied second": lambda r: (r / "specs/20260506-070809-reserved").mkdir(),
    "target name taken": lambda r: put(r, "specs/20260506-070809-a", "a file takes the first target\n"),
    "changed reference": lambda r: same_size_edit(r / "specs/001-a/plan.md", b"see [b](../002-c/spec.md)\n"),
    "changed preference": lambda r: put(r, ".specify/init-options.json", '{"feature_selection": "context"}\n'),
    "changed selection": lambda r: put(r, ".specify/feature.json", '{"feature_directory": "specs/002-b"}\n'),
    "changed context": lambda r: same_size_edit(
        r / "AGENTS.md", (r / "AGENTS.md").read_bytes().replace(b"technologies", b"tekhnologies")
    ),
}


@pytest.mark.parametrize("change", sorted(MUTATIONS))
def test_a_preview_goes_stale_when_any_input_changes(naming, local, change):
    stale_project(local)
    preview = prepare(naming, local, "timestamp")
    assert preview.conflicts == () and preview.reference_edits

    MUTATIONS[change](local)
    changed = tree(local)
    result = naming.apply_naming_migration(preview)

    assert result.status == "rejected" and result.conflicts
    assert tree(local) == changed


@pytest.mark.parametrize("forgery", ["mapping", "edit"])
def test_a_hand_built_preview_cannot_supply_its_own_paths_or_bytes(naming, local, forgery):
    stale_project(local)
    preview = prepare(naming, local, "timestamp")
    if forgery == "mapping":
        forged = dataclasses.replace(preview, mappings=(("specs/001-a", "specs/escaped"), *preview.mappings[1:]))
    else:
        edit = dataclasses.replace(preview.reference_edits[0], replacement=b"tampered bytes\n")
        forged = dataclasses.replace(preview, reference_edits=(edit, *preview.reference_edits[1:]))
    before = tree(local)

    result = naming.apply_naming_migration(forged)

    assert result.status in ("rejected", "applied")
    assert not (local / "specs/escaped").exists()
    assert all(b"tampered" not in (content or b"") for content in tree(local).values())
    if result.status == "rejected":
        assert tree(local) == before


def test_a_used_preview_is_stale_and_the_new_state_is_a_noop(naming, local):
    stale_project(local)
    preview = prepare(naming, local, "timestamp")

    assert naming.apply_naming_migration(preview).status == "applied"
    applied = tree(local)
    assert naming.apply_naming_migration(preview).status == "rejected"
    assert tree(local) == applied

    again = prepare(naming, local, "timestamp")
    assert (again.mappings, again.preference_edit, again.conflicts) == ((), None, ())
    assert naming.apply_naming_migration(again).status == "noop" and tree(local) == applied


# --- Failure injection ------------------------------------------------------------------------


def transaction_project(root: Path) -> None:
    feature(root, "specs/001-a", {"plan.md": "see [b](../002-b/spec.md)\n"})
    feature(root, "specs/002-b")
    put(root, ".specify/init-options.json", '{"feature_numbering": "sequential", "feature_selection": "automatic"}\n')
    put(root, ".specify/feature.json", '{"feature_directory": "specs/001-a"}\n')
    plan = root / "specs/001-a/plan.md"
    os.utime(plan, ns=(STAMP, STAMP))
    os.chmod(plan, 0o640)


FAILURES = {
    "second rename": ("rename", lambda src, dst, calls: len(calls) == 2),
    "first replacement": ("replace", lambda src, dst, calls: len(calls) == 1),
    "preference write": ("replace", lambda src, dst, calls: dst.name == "init-options.json"),
}


@pytest.mark.parametrize("stamp", [0, STAMP])
@pytest.mark.parametrize("case", sorted(FAILURES))
def test_a_handled_failure_restores_every_path_byte_mode_and_time(naming, local, isolated, monkeypatch, case, stamp):
    transaction_project(local)
    os.utime(local / "specs/001-a/plan.md", ns=(stamp, stamp))
    preview = prepare(naming, local, "timestamp")
    before, elsewhere = tree(local), outside(isolated, local)
    kind, should_fail = FAILURES[case]
    inject(monkeypatch, kind, local, should_fail)

    result = naming.apply_naming_migration(preview)

    assert result.status == "rolled_back" and result.recovery is None
    assert tree(local) == before
    assert prepare(naming, local, "timestamp") == preview  # the fingerprint covers modes and modification times
    assert outside(isolated, local) == elsewhere  # no recovery data outlives a complete rollback


def test_an_interrupt_restores_everything_and_propagates(naming, local, isolated, monkeypatch):
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    before, elsewhere = tree(local), outside(isolated, local)
    inject(monkeypatch, "replace", local, lambda src, dst, calls: len(calls) == 1, error=KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        naming.apply_naming_migration(preview)

    assert tree(local) == before and outside(isolated, local) == elsewhere


def test_failed_restoration_retains_original_bytes_and_lists_the_remaining_rename(naming, local, monkeypatch):
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    old_a, new_a = local / "specs/001-a", local / dict(preview.mappings)["specs/001-a"]
    original = (old_a / "plan.md").read_bytes()
    before = tree(local)
    armed = {"on": True}
    inject(monkeypatch, "rename", local, lambda src, dst, calls: armed["on"] and (len(calls) == 2 or dst == old_a))

    result = naming.apply_naming_migration(preview)

    assert result.status == "recovery_required" and result.recovery is not None
    directory = Path(result.recovery["directory"])
    assert directory.is_absolute() and directory.is_dir() and not directory.is_relative_to(local)
    assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    operations = result.recovery["remaining_operations"]
    assert [(o["from"], o["to"]) for o in operations] == [(str(new_a), str(old_a))]
    assert all(o["operation"] and o["message"] for o in operations)
    assert any(path.read_bytes() == original for path in directory.rglob("*") if path.is_file())
    assert new_a.is_dir() and not old_a.exists()
    armed["on"] = False
    os.rename(new_a, old_a)  # performing the reported operation completes the recovery
    assert tree(local) == before


def test_failed_restoration_retains_original_bytes_and_lists_the_remaining_copy(naming, local, monkeypatch):
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    plan = local / "specs/001-a/plan.md"
    original = plan.read_bytes()
    before = tree(local)
    armed = {"on": True}
    # The preference write starts the rollback. Then the restoration of the plan, at its original path, fails.
    inject(monkeypatch, "replace", local, lambda src, dst, calls: armed["on"] and (dst.name == "init-options.json" or dst == plan))

    result = naming.apply_naming_migration(preview)

    assert result.status == "recovery_required"
    directory = Path(result.recovery["directory"])
    operations = result.recovery["remaining_operations"]
    assert [o["to"] for o in operations] == [str(plan)]
    assert Path(operations[0]["from"]).is_relative_to(directory)
    assert Path(operations[0]["from"]).read_bytes() == original
    armed["on"] = False
    shutil.copyfile(operations[0]["from"], operations[0]["to"])  # performing the reported operation
    assert tree(local) == before


# --- Read-only records, Markdown spellings, and metadata behind symlinks -----------------------


def assert_read_only(row) -> None:
    """A reported row is a read-only mapping. Every attempt to change it fails and its values stay."""
    snapshot = dict(row)
    first = next(iter(snapshot))
    with pytest.raises(TypeError):
        row[first] = "changed"
    with pytest.raises(TypeError):
        del row[first]
    for attempt in (
        lambda: row.update(message="changed"),
        lambda: row.pop(first),
        lambda: row.setdefault("extra", "added"),
        lambda: row.clear(),
    ):
        with pytest.raises((TypeError, AttributeError)):
            attempt()
    assert dict(row) == snapshot


def test_reported_rows_are_read_only_snapshots(naming, local):
    feature(local, "specs/001-a")
    feature(local, "specs/custom")
    os.symlink(local / "specs/custom", local / "specs/001-a/link")  # a conflict that keeps the mappings and notices
    preview = prepare(naming, local, "timestamp")
    rejected = naming.apply_naming_migration(preview)

    for rows in (preview.skipped, preview.conflicts, preview.caller_notices, rejected.conflicts):
        assert rows
        for row in rows:
            assert_read_only(row)


def test_recovery_details_are_read_only(naming, local, monkeypatch):
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    old_a = local / "specs/001-a"
    inject(monkeypatch, "rename", local, lambda src, dst, calls: len(calls) == 2 or dst == old_a)

    result = naming.apply_naming_migration(preview)

    assert result.status == "recovery_required"
    with pytest.raises(TypeError):
        result.recovery["directory"] = "elsewhere"
    operations = result.recovery["remaining_operations"]
    assert isinstance(operations, tuple) and operations
    for row in (*result.conflicts, *operations):
        assert_read_only(row)


PAREN_SUFFIXES = ["a(b)", "(lead)", "tail(x)", "m(a)(b)", "nest(a(b))", "é(日本)", "snake_(x)"]
DESTINATION_STYLES = ["balanced", "escaped", "angle", "definition", "escaped definition"]
TITLES = ["", ' "title"', " 'single'", " (paren title)"]
FRAGMENTS = ["", "#goals", "?v=1#top"]
PUNCTUATION = re.compile(r"[!-/:-@\[-`{-~]")


def link_line(style: str, prefix: str, suffix: str, fragment: str, title: str) -> str:
    """One link to a sibling feature's plan, written in a Markdown destination style."""
    escaped = PUNCTUATION.sub(lambda match: "\\" + match.group(), suffix) if "escaped" in style else suffix
    path = f"../{prefix}{escaped}/plan.md{fragment}"
    if style == "angle":
        return f"[go](<{path}>{title})"
    return f"[go]: {path}{title}" if "definition" in style else f"[go]({path}{title})"


def test_markdown_destinations_with_parentheses_keep_their_spelling_and_resolve(naming, isolated):
    """Invariant: balanced or escaped parentheses in a destination follow the renamed directory in the same style."""
    rng = random.Random(SEED)
    text = "Before (not a link) and [out](../../README.md).\n{}\nAfter (a parenthetical) [same](./spec.md).\n"
    for trial in range(40):
        suffix, style = rng.choice(PAREN_SUFFIXES), rng.choice(DESTINATION_STYLES)
        fragment, title = rng.choice(FRAGMENTS), rng.choice(TITLES)
        context = f"seed={SEED} trial={trial} suffix={suffix!r} style={style} fragment={fragment!r} title={title!r}"
        repo = isolated / f"project {trial}"
        (repo / ".specify").mkdir(parents=True)
        feature(repo, f"specs/001-{suffix}", {"plan.md": "the plan\n"})
        feature(repo, "specs/002-b")
        put(repo, "specs/002-b/plan.md", text.format(link_line(style, "001-", suffix, fragment, title)))

        preview = prepare(naming, repo, "timestamp")
        assert preview.conflicts == (), context
        assert naming.apply_naming_migration(preview).status == "applied", context

        document = repo / "specs/20260506-070810-b/plan.md"
        expected = text.format(link_line(style, "20260506-070809-", suffix, fragment, title))
        assert document.read_text(encoding="utf-8") == expected, context
        assert (repo / f"specs/20260506-070809-{suffix}/plan.md").is_file(), context


LINKED = {
    "extensions": ".specify/extensions",
    "agent-context": ".specify/extensions/agent-context",
    "workflows": ".specify/workflows",
    "runs": RUNS,
    "run": f"{RUNS}/paused-in",
    "state": f"{RUNS}/paused-in/state.json",
    "inputs": f"{RUNS}/paused-in/inputs.json",
    "definition": f"{RUNS}/paused-in/workflow.yml",
    "log": f"{RUNS}/paused-in/log.jsonl",
}


@pytest.mark.parametrize("linked", sorted(LINKED))
def test_metadata_behind_a_symlink_blocks_application_without_changing_outside_bytes(naming, local, isolated, linked):
    feature(local, "specs/001-a", {"workflows/workflow.yml": "workflow: {}\n"})
    put(local, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    put(local, "AGENTS.md", section("specs/001-a/plan.md"))
    run(local, "paused-in", "paused", local / "specs/001-a/workflows")
    elsewhere = isolated / "elsewhere"
    elsewhere.mkdir()
    shutil.move(local / LINKED[linked], elsewhere / linked)  # the real files live outside the project
    os.symlink(elsewhere / linked, local / LINKED[linked])
    before, outer = tree(local), tree(elsewhere)

    preview = prepare(naming, local, "timestamp")

    assert preview.conflicts
    result = naming.apply_naming_migration(preview)
    assert result.status == "rejected" and result.conflicts
    assert tree(local) == before and tree(elsewhere) == outer


def test_a_symlinked_directory_above_the_project_is_not_a_refusal(naming, isolated):
    real = isolated / "real"
    repo = real / "code repository"
    (repo / ".specify").mkdir(parents=True)
    feature(repo, "specs/001-a", {"plan.md": "the plan\n"})
    put(repo, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    put(repo, "AGENTS.md", section("specs/001-a/plan.md"))
    os.symlink(real, isolated / "alias")  # like /var above /private/var on macOS

    preview = prepare(naming, isolated / "alias" / "code repository", "timestamp")

    assert preview.conflicts == () and preview.repository_root == repo.resolve()
    assert naming.apply_naming_migration(preview).status == "applied"
    new = dict(preview.mappings)["specs/001-a"]
    assert (repo / "AGENTS.md").read_text() == section(f"{new}/plan.md")


def test_opaque_workflow_notices_decode_encoded_paths_without_exposing_values(naming, local):
    """Ownership: encoded caller paths produce notices while every opaque byte remains unchanged."""
    source = "specs/001-日本語"
    feature(local, source, {"workflows/workflow.yml": "workflow: {}\n"})
    directory = run(local, "encoded", "paused", local / source / "workflows")
    definition = put(
        directory, "workflow.yml",
        'workflow:\n  id: demo-flow\n  path: "specs/001-\\u65e5\\u672c\\u8a9e PRIVATE-CALLER"\n',
    )
    log = put(directory, "log.jsonl", json.dumps({"path": source, "secret": "PRIVATE-CALLER"}) + "\n")
    original = {path: path.read_bytes() for path in (definition, log)}
    preview = prepare(naming, local, "timestamp")
    notices = [row for row in preview.caller_notices if Path(row["path"] or "") in original]
    assert {Path(row["path"]) for row in notices} == set(original)
    assert all("PRIVATE-CALLER" not in row["message"] + (row["location"] or "") for row in notices)
    assert naming.apply_naming_migration(preview).status == "applied"
    assert {path: path.read_bytes() for path in original} == original


def test_existing_epoch_metadata_and_new_preference_timestamps_are_distinct(naming, local):
    """Metadata conservation: existing epoch files keep zero, while new files get creation time."""
    feature(local, "specs/001-a", {"plan.md": "See specs/002-b/spec.md\n"})
    feature(local, "specs/002-b")
    original = local / "specs/001-a/plan.md"
    os.utime(original, ns=(0, 0))
    preview = prepare(naming, local, "timestamp")
    assert naming.apply_naming_migration(preview).status == "applied"
    moved = local / dict(preview.mappings)["specs/001-a"] / "plan.md"
    assert moved.stat().st_mtime_ns == 0
    assert (local / ".specify/init-options.json").stat().st_mtime_ns > 0


def test_context_bom_crlf_bytes_and_epoch_metadata_survive_application(naming, local):
    """Ownership and metadata conservation hold for byte-encoded native context outside managed markers."""
    feature(local, "specs/001-a")
    put(local, CONTEXT_CONFIG_PATH, CONTEXT_CONFIG)
    before = b"\xef\xbb\xbfOutside specs/001-a\r\n<!-- SPECKIT START -->\r\nspecs/001-a/spec.md\r\n<!-- SPECKIT END -->\r\nTail\r\n"
    context = put(local, "AGENTS.md", before)
    os.utime(context, ns=(0, 0))
    os.chmod(context, 0o640)
    original_mode = stat.S_IMODE(context.stat().st_mode)
    preview = prepare(naming, local, "timestamp")
    assert naming.apply_naming_migration(preview).status == "applied"
    expected = before.replace(b"\r\nspecs/001-a/spec.md\r\n", b"\r\nspecs/20260506-070809-a/spec.md\r\n")
    assert context.read_bytes() == expected
    assert context.stat().st_mtime_ns == 0 and stat.S_IMODE(context.stat().st_mode) == original_mode


@pytest.mark.parametrize("failure", ["record", "rollback"])
def test_lock_cleanup_preserves_a_refused_or_restored_transaction(naming, local, monkeypatch, failure):
    """Isolation: a retained lock reports the data outcome without leaving migration changes."""
    transaction_project(local)
    preview = prepare(naming, local, "timestamp")
    before = tree(local)
    lock = local / ".specify/naming-migration.lock"
    real_unlink = os.unlink
    result = None
    if failure == "record":
        def fail_record(*args, **kwargs):
            raise OSError(errno.ENOSPC, "Injected lock write failure")

        monkeypatch.setattr(json, "dump", fail_record)
    else:
        inject(
            monkeypatch, "replace", local,
            lambda src, dst, calls: dst.name == "init-options.json"
            and sum(target.name == "init-options.json" for _, target in calls) == 1,
        )

    def fail_release(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError(errno.EACCES, "Injected lock removal failure", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_release)
    try:
        result = naming.apply_naming_migration(preview)
        assert result.status == "recovery_required"
        assert result.transaction_status == ("rejected" if failure == "record" else "rolled_back")
        operations = result.recovery["remaining_operations"]
        assert [(row["operation"], row["from"], row["to"]) for row in operations] == [
            ("remove-lock", str(lock), str(lock))
        ]
        outcome = "rejected" if failure == "record" else "rolled back"
        assert outcome in operations[0]["message"].lower()
    finally:
        monkeypatch.setattr(os, "unlink", real_unlink)
        if lock.exists():
            real_unlink(lock)
        if result is not None and result.recovery is not None:
            shutil.rmtree(result.recovery["directory"])
    assert tree(local) == before
