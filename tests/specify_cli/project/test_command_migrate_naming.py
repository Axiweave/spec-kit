"""``specify project migrate-naming`` previews first and applies only the approved mapping.

The tests drive the public CLI against the real naming module in disposable projects.
They assert exit codes, the JSON contract, and the resulting files, not message wording.
Failure injection replaces only ``os.rename`` and ``os.replace``, the primitives that move
feature directories and replace files. Generated cases use the fixed seed 1539 and print it
with the failing input.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import json
import os
import random
import re
import select
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from specify_cli import app
from specify_cli.workspace import resolve_project, save_active_feature

SEED = 1539
PROJECT_ID = "550e8400-e29b-41d4-a716-446655440000"
FIELDS = frozenset({
    "schema_version", "status", "repository_root", "workspace_root", "project_id",
    "current_scheme", "target_scheme", "clock", "mappings", "changed_references",
    "preference_changed", "skipped", "conflicts", "caller_notices", "approval_token", "recovery",
})
OBJECTS = {
    "mappings": {"from", "to"},
    "changed_references": {"owner", "path", "new_path", "location"},
    "skipped": {"path", "reason"},
    "conflicts": {"path", "code", "message"},
    "caller_notices": {"kind", "path", "location", "message"},
}
STATUSES = {"preview", "applied", "noop", "cancelled", "rejected", "rolled_back", "recovery_required"}
CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
ALPHA = (
    b"# Alpha\n\nSECRET-ALPHA-BODY\n\n"
    b"Read the [plan](plan.md), then [Beta goals](../002-beta/spec.md#goals).\n"
    b"Also see [Beta](../002-beta/spec.md).\n"
)
BETA = b"# Beta\n\n## Goals\n\nSECRET-BETA-BODY\n\nSee [Alpha](../001-alpha/spec.md).\n"
SECRETS = ("SECRET-ALPHA-BODY", "SECRET-BETA-BODY", "SECRET-OPTIONS-VALUE")
OPTIONS = {
    "integration": "omp", "script": "sh", "feature_numbering": "sequential",
    "feature_selection": "context", "future_setting": {"kept": "SECRET-OPTIONS-VALUE"},
}
SKIPPED = {"specs/custom-name", "specs/20200101-000000-existing"}


# --------------------------------------------------------------------------- helpers


class TerminalRunner(CliRunner):
    """A runner whose stdin reports a terminal, so the command may ask for approval."""

    @contextlib.contextmanager
    def isolation(self, *args, **kwargs):
        with super().isolation(*args, **kwargs) as streams:
            sys.stdin.isatty = lambda: True
            yield streams


def run(*args: str, input=None, tty: bool = False):
    runner = TerminalRunner() if tty else CliRunner()
    return runner.invoke(app, ["project", "migrate-naming", *args], input=input)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y%m%d-%H%M%S")


def files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def snapshot(root: Path) -> dict[str, tuple]:
    """Record names, bytes, permissions, and file times, but not directory times."""
    state: dict[str, tuple] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            state[relative.as_posix()] = ("link", os.readlink(path))
        elif path.is_dir():
            state[relative.as_posix()] = ("dir", mode)
        else:
            state[relative.as_posix()] = ("file", path.read_bytes(), mode, info.st_mtime_ns)
    return state


def state(world) -> list[dict]:
    return [snapshot(root) for root in world.watched]


def payload(result) -> dict:
    """Return the one JSON object on stdout after checking the contract's shape."""
    data = json.loads(result.stdout)
    assert isinstance(data, dict), result.output
    assert set(data) == FIELDS, sorted(set(data) ^ FIELDS)
    assert data["schema_version"] == 1 and type(data["schema_version"]) is int
    assert data["status"] in STATUSES
    assert data["target_scheme"] in ("sequential", "timestamp")
    assert type(data["preference_changed"]) is bool
    assert data["clock"] is None or CLOCK.fullmatch(data["clock"])
    for key, names in OBJECTS.items():
        assert isinstance(data[key], list)
        for item in data[key]:
            assert set(item) == names, (key, item)
    for mapping in data["mappings"]:
        assert "\\" not in mapping["from"] + mapping["to"]
        assert not Path(mapping["from"]).is_absolute() and not Path(mapping["to"]).is_absolute()
    recovery = data["recovery"]
    if recovery is not None:
        assert set(recovery) == {"directory", "remaining_operations"}
        for operation in recovery["remaining_operations"]:
            assert set(operation) == {"operation", "from", "to", "message"}
    return data


def codes(data: dict) -> set[str]:
    return {conflict["code"] for conflict in data["conflicts"]}


def review(target: str = "timestamp") -> dict:
    """Request a machine preview that can be approved."""
    result = run("--feature-numbering", target, "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    data = payload(result)
    assert data["status"] == "preview" and data["approval_token"], data
    return data


def approve(token: str, target: str = "timestamp", *flags: str):
    return run("--feature-numbering", target, "--apply", token, *flags)


def migrate(target: str) -> dict:
    reviewed = review(target)
    result = approve(reviewed["approval_token"], target, "--json")
    assert result.exit_code == 0, result.output
    applied = payload(result)
    assert applied["status"] == "applied", applied
    return applied


def decode_token(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def encode_token(data) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


MISSING = object()


def forge(token: str, **changes) -> str:
    """Re-encode a reviewed token with changed, removed, or extra fields."""
    data = decode_token(token)
    for key, value in changes.items():
        if value is MISSING:
            del data[key]
        else:
            data[key] = value
    return encode_token(data)


def assert_no_content(result) -> None:
    for secret in SECRETS:
        assert secret not in result.stdout + result.stderr, secret


def linked(content: bytes, old: str, new: str) -> bytes:
    """The artifact after one feature directory it links to was renamed."""
    return content.replace(f"../{old}/".encode(), f"../{new}/".encode())


def assert_migrated(root: Path, before: dict[str, bytes], mappings: list[dict], edited: set[str],
                    context: str = "") -> None:
    """Artifacts moved with their features, and only the listed files differ from their originals."""
    renames = {mapping["from"]: mapping["to"] for mapping in mappings}
    expected = {}
    for relative, content in before.items():
        for old, new in renames.items():
            if relative.startswith(old + "/"):
                relative = new + relative[len(old):]
                break
        expected[relative] = content
    after = files(root)
    assert set(after) == set(expected), (sorted(set(after) ^ set(expected)), context)
    for relative, content in expected.items():
        if relative not in edited:
            assert after[relative] == content, (relative, context)


def assert_local_migration(root: Path, before: dict[str, bytes], mappings: list[dict]) -> None:
    """The local fixture after conversion to timestamp names, with working links and a new preference."""
    moved = {mapping["from"]: mapping["to"] for mapping in mappings}
    alpha, beta = moved["specs/001-alpha"], moved["specs/002-beta"]
    assert_migrated(root, before, mappings, {f"{alpha}/spec.md", f"{beta}/spec.md", ".specify/init-options.json"})
    assert (root / alpha / "spec.md").read_bytes() == linked(ALPHA, "002-beta", Path(beta).name)
    assert (root / beta / "spec.md").read_bytes() == linked(BETA, "001-alpha", Path(alpha).name)
    options = json.loads((root / ".specify/init-options.json").read_text(encoding="utf-8"))
    assert options == {**OPTIONS, "feature_numbering": "timestamp"}


def mappings_on_disk(root: Path) -> list[dict]:
    """The renames of the local fixture, read back from the directories that now exist."""
    names = sorted(path.name for path in (root / "specs").iterdir())
    return [
        {"from": f"specs/{old}", "to": "specs/" + next(n for n in names if re.fullmatch(rf"\d{{8}}-\d{{6}}-{suffix}", n))}
        for old, suffix in (("001-alpha", "alpha"), ("002-beta", "beta"))
    ]


# ------------------------------------------------------------- fixtures and injection


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolate machine state, Git identity, and temporary files from the developer's environment."""
    home = tmp_path / "home"
    scratch = tmp_path / "temporary files"
    home.mkdir()
    scratch.mkdir()
    for name in tuple(os.environ):
        if name.startswith(("SPECIFY_", "GIT_")):
            monkeypatch.delenv(name, raising=False)
    for name, path in {
        "HOME": home, "XDG_CONFIG_HOME": home / "config", "XDG_DATA_HOME": home / "data",
        "XDG_CACHE_HOME": home / "cache", "TMPDIR": scratch,
    }.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Naming Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "naming@example.invalid")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("COLUMNS", "240")
    source = str(Path(__file__).resolve().parents[3] / "src")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (source, os.environ.get("PYTHONPATH")))))
    return SimpleNamespace(home=home)


def write_features(root: Path) -> None:
    for relative, content in {
        "specs/001-alpha/spec.md": ALPHA,
        "specs/001-alpha/plan.md": b"# Alpha plan\n",
        "specs/002-beta/spec.md": BETA,
        "specs/custom-name/spec.md": b"# Custom\n",
        "specs/20200101-000000-existing/spec.md": b"# Existing timestamp feature\n",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


@pytest.fixture
def project(sandbox, tmp_path, monkeypatch):
    """A local project with two linked sequential features, one custom name, and one timestamp feature."""
    root = tmp_path / "local project"
    (root / ".specify").mkdir(parents=True)
    (root / ".specify/init-options.json").write_text(json.dumps(OPTIONS, indent=2) + "\n", encoding="utf-8")
    write_features(root)
    (root / "source.py").write_text("OLD = 'specs/001-alpha'\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return SimpleNamespace(root=root, home=sandbox.home, watched=(root, sandbox.home))


def inject(monkeypatch, fails) -> None:
    """Raise OSError from os.rename and os.replace when fails(kind, src, dst) is true."""
    for kind in ("rename", "replace"):
        original = getattr(os, kind)

        def wrapper(src, dst, *args, _kind=kind, _original=original, **kwargs):
            if fails(_kind, Path(os.fspath(src)), Path(os.fspath(dst))):
                raise OSError(errno.EIO, "Injected failure")
            return _original(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, kind, wrapper)


def nth(count: int, predicate):
    """Fail only the count-th matching call, so a later restoration of the same path can succeed."""
    seen: list[Path] = []

    def fails(kind, src, dst):
        if predicate(kind, src, dst):
            seen.append(dst)
            return len(seen) == count
        return False

    return fails


def forward_rename(kind: str, src: Path, dst: Path) -> bool:
    """A sequential feature directory moving to its new name, not a restoration."""
    return kind == "rename" and re.match(r"\d{3}-", src.name) is not None


def spec_write(kind: str, src: Path, dst: Path) -> bool:
    return kind == "replace" and dst.name == "spec.md"


def preference_write(kind: str, src: Path, dst: Path) -> bool:
    return kind == "replace" and dst.name == "init-options.json"


ROLLED_BACK = {
    "second-rename": lambda: nth(2, forward_rename),
    "first-reference-write": lambda: nth(1, spec_write),
    "second-reference-write": lambda: nth(2, spec_write),
    "preference-write": lambda: nth(1, preference_write),
}


def break_rename_restoration():
    """Fail the second forward rename, then fail every rename that moves a migrated directory back."""
    forward = nth(2, forward_rename)

    def fails(kind, src, dst):
        return forward(kind, src, dst) or (kind == "rename" and re.match(r"\d{8}-\d{6}-", src.name) is not None)

    return fails


def break_byte_restoration():
    """Fail the second reference write, then fail every later write to a file that was written before."""
    written: list[str] = []

    def fails(kind, src, dst):
        if not spec_write(kind, src, dst):
            return False
        feature = dst.parent.name.rsplit("-", 1)[-1]  # The same feature before and after its rename.
        if feature in written:
            return True
        written.append(feature)
        return len(written) == 2

    return fails


# ----------------------------------------------------------------------- invocation


USAGE_ERRORS = {
    "missing-target": [],
    "unsupported-target": ["--feature-numbering", "weekly"],
    "dry-run-with-apply": ["--feature-numbering", "timestamp", "--dry-run", "--apply", "TOKEN"],
    "apply-without-token": ["--feature-numbering", "timestamp", "--apply"],
    "approval-shortcut": ["--feature-numbering", "timestamp", "--yes"],
    "feature-selector": ["--feature-numbering", "timestamp", "specs/001-alpha"],
}


@pytest.mark.parametrize("case", USAGE_ERRORS)
def test_usage_errors_exit_two_and_change_nothing(project, case):
    before = state(project)
    result = run(*USAGE_ERRORS[case])
    assert result.exit_code == 2, result.output
    assert state(project) == before


# ------------------------------------------------- preview, cancellation, and approval


@pytest.mark.parametrize("where", [".", "specs/001-alpha"], ids=["root", "feature-directory"])
def test_dry_run_json_reports_the_exact_preview_without_writing(project, monkeypatch, where):
    """Invariant: a machine preview is complete, matches the contract, and changes nothing."""
    monkeypatch.chdir(project.root / where)
    before = state(project)

    result = run("--feature-numbering", "timestamp", "--dry-run", "--json")

    assert result.exit_code == 0, result.output
    data = payload(result)
    clock = datetime.fromisoformat(data["clock"])
    assert timedelta(seconds=-1) <= datetime.now() - clock < timedelta(minutes=1)
    assert data["status"] == "preview"
    assert (data["repository_root"], data["workspace_root"], data["project_id"]) == (
        str(project.root), str(project.root), None,
    )
    assert (data["current_scheme"], data["target_scheme"]) == ("sequential", "timestamp")
    assert data["mappings"] == [
        {"from": "specs/001-alpha", "to": f"specs/{stamp(clock)}-alpha"},
        {"from": "specs/002-beta", "to": f"specs/{stamp(clock + timedelta(seconds=1))}-beta"},
    ]
    alpha, beta = data["mappings"][0]["to"], data["mappings"][1]["to"]
    assert {(edit["owner"], edit["path"], edit["new_path"]) for edit in data["changed_references"]} == {
        ("feature-artifact", str(project.root / "specs/001-alpha/spec.md"), str(project.root / alpha / "spec.md")),
        ("feature-artifact", str(project.root / "specs/002-beta/spec.md"), str(project.root / beta / "spec.md")),
    }
    assert all(edit["location"] for edit in data["changed_references"])
    assert data["preference_changed"] is True
    skipped = {entry["path"] for entry in data["skipped"]}
    assert "specs/custom-name" in skipped and skipped <= SKIPPED
    assert data["conflicts"] == []
    kinds = {notice["kind"] for notice in data["caller_notices"]}
    assert "external-caller" in kinds and "other-worktree" not in kinds
    assert re.fullmatch(r"[A-Za-z0-9_-]+", data["approval_token"])
    assert len(data["approval_token"]) <= 16 * 1024
    assert data["recovery"] is None
    assert_no_content(result)
    assert state(project) == before


def test_json_without_apply_stays_preview_only_with_a_terminal(project):
    """Invariant: --json asks nothing and applies nothing unless a token is supplied."""
    before = state(project)
    result = run("--feature-numbering", "timestamp", "--json", input="y\n", tty=True)
    assert result.exit_code == 0, result.output
    data = payload(result)
    assert data["status"] == "preview" and data["approval_token"]
    assert state(project) == before


def test_without_a_terminal_the_human_command_only_previews(project):
    """Invariant: a piped yes is not approval, and the preview names every feature it would move."""
    before = state(project)
    result = run("--feature-numbering", "timestamp", input="y\n")
    assert result.exit_code == 0, result.output
    for path in ("specs/001-alpha", "specs/002-beta", "specs/custom-name"):
        assert path in result.stdout, path
    assert_no_content(result)
    assert state(project) == before


def test_dry_run_never_asks_even_with_a_terminal(project):
    before = state(project)
    result = run("--feature-numbering", "timestamp", "--dry-run", input="y\n", tty=True)
    assert result.exit_code == 0, result.output
    assert "specs/001-alpha" in result.stdout
    assert state(project) == before


@pytest.mark.parametrize("answer", ["n\n", "\n", ""], ids=["no", "default", "end-of-input"])
def test_declining_approval_succeeds_and_changes_nothing(project, answer):
    """Invariant: cancellation exits successfully after showing the preview."""
    before = state(project)
    result = run("--feature-numbering", "timestamp", input=answer, tty=True)
    assert result.exit_code == 0, result.output
    assert "specs/001-alpha" in result.stdout
    assert state(project) == before


def test_approval_at_a_terminal_applies_and_reports_the_new_names(project):
    """Invariant: after a yes, the directories on disk are the ones the command reported."""
    before = files(project.root)
    result = run("--feature-numbering", "timestamp", input="y\n", tty=True)
    assert result.exit_code == 0, result.output
    mappings = mappings_on_disk(project.root)
    assert_local_migration(project.root, before, mappings)
    for mapping in mappings:
        assert mapping["from"] in result.stdout and mapping["to"] in result.stdout, mapping
    assert_no_content(result)


def test_blocking_conflicts_are_reported_and_cannot_be_approved(project):
    """Invariant: a conflicted preview exits 1 with structured conflicts and no token, in every mode."""
    broken = project.root / "specs/20261301-250000-broken"
    broken.mkdir()
    (broken / "spec.md").write_text("# Broken calendar prefix\n", encoding="utf-8")
    before = state(project)

    machine = run("--feature-numbering", "timestamp", "--dry-run", "--json")
    assert machine.exit_code == 1, machine.output
    data = payload(machine)
    assert data["status"] == "preview" and data["approval_token"] is None
    assert any("20261301-250000-broken" in c["path"] + c["message"] for c in data["conflicts"])
    assert machine.stderr.strip()

    human = run("--feature-numbering", "timestamp", input="y\n", tty=True)
    assert human.exit_code == 1, human.output
    assert "20261301-250000-broken" in human.stdout + human.stderr
    assert state(project) == before


def test_unresolvable_projects_are_refused_with_a_structured_result(tmp_path, sandbox, monkeypatch):
    """Invariant: with no project to describe, roots are null, stdout is still JSON, and stderr explains."""
    empty = tmp_path / "not a project"
    empty.mkdir()
    monkeypatch.chdir(empty)
    result = run("--feature-numbering", "timestamp", "--dry-run", "--json")
    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and data["target_scheme"] == "timestamp"
    for key in ("repository_root", "workspace_root", "project_id", "current_scheme", "clock",
                "approval_token", "recovery"):
        assert data[key] is None, key
    assert (data["mappings"], data["changed_references"]) == ([], [])
    assert codes(data) == {"invalid-project"}
    assert result.stderr.strip()
    assert run("--feature-numbering", "timestamp").exit_code == 1


def test_malformed_project_metadata_is_refused_without_writes(project):
    (project.root / ".specify/init-options.json").write_text("{ not json", encoding="utf-8")
    before = state(project)
    result = run("--feature-numbering", "timestamp", "--dry-run", "--json")
    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["approval_token"] is None and data["conflicts"]
    assert result.stderr.strip()
    assert state(project) == before


# ------------------------------------------------------------------ approval tokens


def test_token_applies_the_reviewed_mapping(project):
    """Invariant: --apply performs exactly the reviewed mapping and reports it with the contract fields."""
    before = files(project.root)
    reviewed = review("timestamp")

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 0, result.output
    data = payload(result)
    assert data["status"] == "applied"
    for key in ("repository_root", "workspace_root", "project_id", "current_scheme", "target_scheme",
                "clock", "mappings", "changed_references", "preference_changed", "skipped", "caller_notices"):
        assert data[key] == reviewed[key], key
    assert data["conflicts"] == [] and data["approval_token"] is None and data["recovery"] is None
    assert_local_migration(project.root, before, data["mappings"])
    assert (project.root / "source.py").read_text(encoding="utf-8") == "OLD = 'specs/001-alpha'\n"
    assert_no_content(result)


def test_human_application_reports_the_reviewed_work(project):
    """Invariant: the text report carries each mapping, changed file, skip, and caller notice."""
    reviewed = review("timestamp")
    result = approve(reviewed["approval_token"])
    assert result.exit_code == 0, result.output
    out = result.stdout
    for mapping in reviewed["mappings"]:
        assert mapping["from"] in out and mapping["to"] in out, mapping
    for edit in reviewed["changed_references"]:
        assert edit["path"] in out and edit["new_path"] in out, edit
    for entry in reviewed["skipped"]:
        assert entry["path"] in out, entry
    for notice in reviewed["caller_notices"]:
        assert notice["message"] in out, notice
    assert_no_content(result)


def test_a_used_token_is_stale_and_changes_nothing(project):
    """Invariant: success changes the source fingerprint, so the old token cannot repeat the work."""
    reviewed = review("timestamp")
    assert approve(reviewed["approval_token"], "timestamp", "--json").exit_code == 0
    after = state(project)

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and data["approval_token"] is None
    assert "stale-preview" in codes(data)
    assert state(project) == after


def test_token_pins_the_preview_clock_across_later_seconds(project):
    """Invariant: application rebuilds the mapping from the token's clock, not from the current time."""
    reviewed = review("timestamp")
    clock = datetime.fromisoformat(reviewed["clock"])
    while datetime.now() < clock + timedelta(seconds=2):
        time.sleep(0.05)

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 0, result.output
    data = payload(result)
    assert data["clock"] == reviewed["clock"] and data["mappings"] == reviewed["mappings"]
    assert (project.root / f"specs/{stamp(clock)}-alpha/spec.md").is_file()
    assert (project.root / f"specs/{stamp(clock + timedelta(seconds=1))}-beta/spec.md").is_file()


def same_size_edit(project, reviewed):
    (project.root / "specs/002-beta/spec.md").write_bytes(BETA.replace(b"Goals", b"Goalz"))


def occupy_target(project, reviewed):
    (project.root / reviewed["mappings"][0]["to"]).mkdir()


def change_preference(project, reviewed):
    path = project.root / ".specify/init-options.json"
    path.write_text(json.dumps({**OPTIONS, "script": "ps"}, indent=2) + "\n", encoding="utf-8")


def save_selection(project, reviewed):
    (project.root / ".specify/feature.json").write_text('{"feature_directory": "specs/002-beta"}\n')


MUTATIONS = {
    "same-size-source-edit": same_size_edit,
    "newly-occupied-target": occupy_target,
    "changed-preference": change_preference,
    "new-saved-selection": save_selection,
}


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_any_change_after_review_makes_the_token_stale(project, mutation):
    """Invariant: a changed source, target, preference, or selection is refused and left alone."""
    reviewed = review("timestamp")
    MUTATIONS[mutation](project, reviewed)
    changed = state(project)

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and "stale-preview" in codes(data)
    assert state(project) == changed


def test_token_from_another_repository_is_refused(project, tmp_path, monkeypatch):
    """Invariant: a token binds the repository that was reviewed."""
    twin = tmp_path / "twin project"
    shutil.copytree(project.root, twin)
    reviewed = review("timestamp")
    monkeypatch.chdir(twin)
    before = (snapshot(project.root), snapshot(twin))

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and data["repository_root"] == str(twin)
    assert "root-mismatch" in codes(data)
    assert (snapshot(project.root), snapshot(twin)) == before


def test_token_for_another_target_scheme_is_refused(project):
    reviewed = review("timestamp")
    before = state(project)
    result = approve(reviewed["approval_token"], "sequential", "--json")
    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and data["target_scheme"] == "sequential"
    assert "target-mismatch" in codes(data)
    assert state(project) == before


FORGERIES = {
    "snapshot-digest": ({"snapshot_digest": "0" * 64}, "stale-preview"),
    "mapping-digest": ({"mapping_digest": "f" * 64}, "stale-preview"),
    "other-clock": ({"clock": "2001-02-03T04:05:06"}, "stale-preview"),
    "calendar-limit-clock": ({"clock": "9999-12-31T23:59:59"}, "stale-preview"),
    "other-repository": ({"repository_root": "/elsewhere/repository"}, "root-mismatch"),
    "other-workspace": ({"workspace_root": "/elsewhere/workspace"}, "root-mismatch"),
    "other-project": ({"project_id": "a60e8400-e29b-41d4-a716-446655440001"}, "root-mismatch"),
}


def test_well_formed_forgeries_are_refused(project):
    """Invariant: every bound field must match the recomputed migration, not the token's claim."""
    token = review("timestamp")["approval_token"]
    before = state(project)
    for name, (changes, code) in FORGERIES.items():
        result = approve(forge(token, **changes), "timestamp", "--json")
        assert result.exit_code == 1, (name, result.output)
        data = payload(result)
        assert data["status"] == "rejected" and code in codes(data), (name, data["conflicts"])
    assert state(project) == before


TEXT_FAULTS = {
    "oversized": lambda token: "A" * (16 * 1024 + 1),
    "not-url-safe": lambda token: "not a token!",
    "truncated": lambda token: token[: len(token) // 2],
    "not-utf8": lambda token: base64.urlsafe_b64encode(b"\xff\xfe\x00").decode(),
    "not-json": lambda token: base64.urlsafe_b64encode(b"hello").decode(),
    "json-array": lambda token: base64.urlsafe_b64encode(b"[]").decode(),
    "deeply-nested": lambda token: base64.urlsafe_b64encode(b"[" * 6000 + b"]" * 6000).decode(),
}


@pytest.mark.parametrize("fault", TEXT_FAULTS)
def test_undecodable_tokens_are_refused_and_never_echoed(project, fault):
    """Invariant: untrusted token text is bounded, decoded safely, never echoed, and never applied."""
    bad = TEXT_FAULTS[fault](review("timestamp")["approval_token"])
    before = state(project)

    result = approve(bad, "timestamp", "--json")

    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "rejected" and data["approval_token"] is None
    assert codes(data) == {"invalid-token"}
    assert (data["mappings"], data["changed_references"], data["preference_changed"]) == ([], [], False)
    assert bad not in result.stdout + result.stderr
    assert state(project) == before


BAD_VALUES = {
    "version": [0, 2, True, "1", 1.0, None],
    "repository_root": [7, "", "relative/root", None, ["/x"]],
    "workspace_root": [7, "", "relative/root", None],
    "project_id": [7, "not-a-uuid", PROJECT_ID.upper(), ""],
    "target_scheme": ["weekly", "Timestamp", ["timestamp"], None],
    "clock": [
        "2026-02-30T00:00:00", "2026-10-01 12:00:00", "2026-10-01T12:00:00.5", "2026-10-01T12:00:00Z",
        "2026-10-01T25:00:00", 5, None,
    ],
    "snapshot_digest": ["A" * 64, "a" * 63, "a" * 65, "g" * 64, 7, None],
    "mapping_digest": ["F" * 64, "a" * 64 + "0", 7, None],
}
EXTRA_FIELDS = {
    "mappings": [{"from": "specs/001-alpha", "to": "specs/999-evil"}],
    "replacement": "SECRET",
}


def test_token_fields_are_validated_by_type_and_shape(project):
    """Invariant: a missing, extra, or badly typed field is refused and never reaches the project."""
    token = review("timestamp")["approval_token"]
    before = state(project)
    cases = [
        (f"missing {field}", {field: MISSING}) for field in BAD_VALUES
    ] + [
        (f"extra {field}", {field: value}) for field, value in EXTRA_FIELDS.items()
    ] + [
        (f"{field}={value!r}", {field: value}) for field, values in BAD_VALUES.items() for value in values
    ]
    for name, changes in cases:
        forged = forge(token, **changes)
        result = approve(forged, "timestamp", "--json")
        context = f"seed={SEED} case={name}\n{result.output}"
        assert result.exit_code == 1, context
        data = payload(result)
        assert data["status"] == "rejected" and codes(data) == {"invalid-token"}, context
        assert forged not in result.stdout + result.stderr, context
    assert state(project) == before


def test_single_character_changes_to_a_token_never_apply(project):
    """Invariant: no one-character change to a reviewed token is ever accepted."""
    token = review("timestamp")["approval_token"]
    rng = random.Random(SEED)
    alphabet = string.ascii_letters + string.digits + "-_"
    before = state(project)
    for _ in range(15):
        # The last character can hold unused bits, so changing only it can leave the same bytes.
        index = rng.randrange(len(token) - 1)
        replacement = rng.choice([char for char in alphabet if char != token[index]])
        bad = token[:index] + replacement + token[index + 1:]
        result = approve(bad, "timestamp", "--json")
        context = f"seed={SEED} index={index} replacement={replacement!r}\n{result.output}"
        assert result.exit_code == 1, context
        assert payload(result)["status"] == "rejected", context
    assert state(project) == before


# ------------------------------------------------------- completed work and no-ops


def test_repeating_a_completed_migration_is_a_noop(project):
    """Invariant: the already-correct scheme succeeds with no token and no writes, in every mode."""
    migrate("timestamp")
    before = state(project)

    for flags in (["--json"], ["--dry-run", "--json"]):
        result = run("--feature-numbering", "timestamp", *flags)
        assert result.exit_code == 0, result.output
        data = payload(result)
        assert data["status"] == "noop" and data["approval_token"] is None
        assert (data["mappings"], data["changed_references"], data["conflicts"]) == ([], [], [])
        assert data["preference_changed"] is False
        assert data["current_scheme"] == data["target_scheme"] == "timestamp"

    human = run("--feature-numbering", "timestamp", input="y\n", tty=True)
    assert human.exit_code == 0, human.output
    assert state(project) == before


def test_noop_reports_all_skipped_features_without_writes(project):
    """Invariant: no-op reports retain every skipped path and reason without changing isolated project state."""
    migrate("timestamp")
    before = state(project)
    expected_paths = {
        path.relative_to(project.root).as_posix()
        for path in (project.root / "specs").iterdir()
        if path.is_dir()
    }

    machine = run("--feature-numbering", "timestamp", "--json")
    assert machine.exit_code == 0, machine.output
    data = payload(machine)
    assert data["status"] == "noop"
    assert {item["path"] for item in data["skipped"]} == expected_paths
    assert SKIPPED <= expected_paths
    assert all(item["reason"] for item in data["skipped"])
    assert state(project) == before

    for flags in ((), ("--dry-run",)):
        human = run("--feature-numbering", "timestamp", *flags)
        assert human.exit_code == 0, human.output
        for item in data["skipped"]:
            assert any(
                item["path"] in line and item["reason"] in line
                for line in human.stdout.splitlines()
            ), (item, human.output)
        for notice in data["caller_notices"]:
            assert notice["message"] in human.stdout
        assert state(project) == before


def test_conversion_back_uses_the_same_flow(project):
    """Invariant: converting back keeps each suffix, keeps links working, and restores the sequential preference."""
    migrate("timestamp")
    middle = files(project.root)
    reviewed = review("sequential")
    assert (reviewed["current_scheme"], reviewed["target_scheme"]) == ("timestamp", "sequential")

    applied = approve(reviewed["approval_token"], "sequential", "--json")

    assert applied.exit_code == 0, applied.output
    data = payload(applied)
    assert data["status"] == "applied"
    moved = {mapping["from"]: mapping["to"] for mapping in data["mappings"]}
    assert {Path(old).name.split("-", 2)[2] for old in moved} == {"alpha", "beta", "existing"}
    assert all(re.fullmatch(r"specs/\d{3,}-[a-z]+", new) for new in moved.values())
    alpha, beta = (next(new for new in moved.values() if new.endswith(suffix)) for suffix in ("-alpha", "-beta"))
    assert_migrated(project.root, middle, data["mappings"], {f"{alpha}/spec.md", f"{beta}/spec.md", ".specify/init-options.json"})
    assert (project.root / alpha / "spec.md").read_bytes().count(f"../{Path(beta).name}/".encode()) == 2
    assert (project.root / beta / "spec.md").read_bytes().count(f"../{Path(alpha).name}/".encode()) == 1
    options = json.loads((project.root / ".specify/init-options.json").read_text(encoding="utf-8"))
    assert options == {**OPTIONS, "feature_numbering": "sequential"}


# ----------------------------------------------------- refusal and recovery (T024)

HOLD = """
import os, pathlib, sys, time
gate, ready = (pathlib.Path(arg) for arg in sys.argv[1:3])
real = {name: getattr(os, name) for name in ("rename", "replace")}
def owned(path):
    parts = pathlib.Path(os.fspath(path)).parts
    return "specs" in parts or parts[-1] == "init-options.json"
def hold(name):
    def wrapper(src, dst, *args, **kwargs):
        if owned(src) or owned(dst):
            ready.write_text("held")
            deadline = time.monotonic() + 60
            while not gate.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        return real[name](src, dst, *args, **kwargs)
    return wrapper
for name in real:
    setattr(os, name, hold(name))
sys.argv = ["specify", *sys.argv[3:]]
from specify_cli import app
app()
"""


def wait_for(path: Path, holder: subprocess.Popen) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert holder.poll() is None, holder.communicate()
        assert time.monotonic() < deadline, "The held migration never reached its first change."
        time.sleep(0.02)


def test_a_second_migration_is_refused_while_the_first_one_holds_the_workspace(project, tmp_path):
    """Invariant: a real migration paused at its first change blocks a second one, then finishes intact."""
    before = files(project.root)
    reviewed = review("timestamp")
    token = reviewed["approval_token"]
    gate, ready = tmp_path / "release", tmp_path / "held"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(gate), str(ready),
         "project", "migrate-naming", "--feature-numbering", "timestamp", "--apply", token, "--json"],
        cwd=project.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        wait_for(ready, holder)
        refused = approve(token, "timestamp", "--json")
        assert refused.exit_code == 1, refused.output
        data = payload(refused)
        assert data["status"] == "rejected" and data["conflicts"] and "stale-preview" not in codes(data)
        assert data["mappings"] == reviewed["mappings"]
        paused = files(project.root)
        assert all(paused.get(path) == content for path, content in before.items())
    finally:
        gate.write_text("release")
        out, err = holder.communicate(timeout=60)
    assert holder.returncode == 0, err
    finished = json.loads(out)
    assert finished["status"] == "applied" and finished["mappings"] == reviewed["mappings"]
    assert_local_migration(project.root, before, reviewed["mappings"])


@pytest.mark.parametrize("failure", ROLLED_BACK)
def test_a_handled_failure_rolls_back_every_change(project, monkeypatch, failure):
    """Invariant: after a recoverable failure the names, bytes, and times equal the original state."""
    reviewed = review("timestamp")
    before = state(project)
    inject(monkeypatch, ROLLED_BACK[failure]())

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 1, result.output
    assert result.stderr.strip()
    data = payload(result)
    assert data["status"] == "rolled_back" and data["conflicts"]
    assert data["mappings"] == reviewed["mappings"]
    assert data["changed_references"] == reviewed["changed_references"]
    assert data["preference_changed"] is True
    assert data["recovery"] is None and data["approval_token"] is None
    assert state(project) == before


def test_a_failure_in_text_mode_exits_one_and_restores_the_project(project, monkeypatch):
    reviewed = review("timestamp")
    before = state(project)
    inject(monkeypatch, ROLLED_BACK["second-reference-write"]())
    result = approve(reviewed["approval_token"])
    assert result.exit_code == 1, result.output
    assert result.stderr.strip()
    assert state(project) == before


@pytest.mark.parametrize("how", ["rename", "bytes"])
def test_incomplete_restoration_reports_exact_recovery_work(project, monkeypatch, how):
    """Invariant: when restoration fails, original bytes survive and the result lists the remaining work."""
    before = files(project.root)
    reviewed = review("timestamp")
    inject(monkeypatch, break_rename_restoration() if how == "rename" else break_byte_restoration())

    result = approve(reviewed["approval_token"], "timestamp", "--json")

    assert result.exit_code == 1, result.output
    data = payload(result)
    assert data["status"] == "recovery_required" and data["conflicts"]
    assert data["mappings"] == reviewed["mappings"]
    assert data["changed_references"] == reviewed["changed_references"]
    assert data["approval_token"] is None
    recovery = data["recovery"]
    directory = Path(recovery["directory"])
    assert directory.is_dir() and directory.name.startswith("naming-recovery-")
    assert directory.parent.resolve() == (project.root / ".specify").resolve()
    if os.name == "posix":
        assert stat.S_IMODE(directory.stat().st_mode) & 0o077 == 0
    remaining = recovery["remaining_operations"]
    assert remaining and all(item["operation"] and item["message"] for item in remaining)
    for item in remaining:
        if how == "rename":
            assert Path(item["from"]).is_dir() and not Path(item["to"]).exists(), item
            assert Path(item["to"]).name in ("001-alpha", "002-beta"), item
        else:
            backup = directory / item["from"]  # An absolute path stays absolute.
            assert backup.is_relative_to(directory) and backup.read_bytes() in (ALPHA, BETA), item
            assert Path(item["to"]).name == "spec.md", item
    assert Counter(before.values()) <= Counter(files(project.root).values()) + Counter(files(directory).values())
    assert_no_content(result)


def test_incomplete_restoration_lists_the_remaining_work_in_text_mode(project, monkeypatch):
    """Invariant: the person who must finish the rollback can see which directories to move back."""
    reviewed = review("timestamp")
    inject(monkeypatch, break_rename_restoration())
    result = approve(reviewed["approval_token"])
    assert result.exit_code == 1, result.output
    assert any(
        Path(mapping["from"]).name in result.stderr and Path(mapping["to"]).name in result.stderr
        for mapping in reviewed["mappings"]
    ), result.stderr
    assert_no_content(result)


# ------------------------------------------------------ generated sets (seed 1539)

SUFFIXES = ["plain", "with space", "日本語", "café", "a-b-c", "long-" + "x" * 60]
SCOPES = ["", "", "payments", "team a"]
NUMBERS = [f"{number:03d}" for number in (1, 2, 3, 7, 10, 42, 998, 999, 1000, 1001)]
TIMESTAMPS = [
    "20240229-120000", "20251231-235959", "20260101-000000", "20260101-000001",
    "20261001-120000", "20300615-081500",
]
RESERVED_SECONDS = (-1, 0, 1, 3, 4, 5, 8, 9, 10, 11)
FEATURE_SECONDS = (2, 6)


def generate(rng: random.Random, size: int, prefixes: list[str]) -> list[str]:
    """Distinct feature directories over nested scopes, with repeated prefixes and unusual suffixes."""
    found: set[str] = set()
    while len(found) < size:
        name = f"{rng.choice(prefixes)}-{rng.choice(SUFFIXES)}"
        found.add("/".join(filter(None, ["specs", rng.choice(SCOPES), name])))
    return sorted(found)


def make_project(tmp_path: Path, name: str, scheme: str, directories: list[str]) -> Path:
    root = tmp_path / name
    (root / ".specify").mkdir(parents=True)
    options = {**OPTIONS, "feature_numbering": scheme}
    (root / ".specify/init-options.json").write_text(json.dumps(options, indent=2) + "\n", encoding="utf-8")
    for relative in directories:
        (root / relative).mkdir(parents=True)
        (root / relative / "spec.md").write_text("# Generated feature\n", encoding="utf-8")
    return root


def as_map(mappings: list[dict]) -> dict[str, str]:
    found = {item["from"]: item["to"] for item in mappings}
    assert len(found) == len(mappings), mappings
    return found


def reference_timestamps(sources: list[str], occupied: set[str], clock: datetime) -> list[dict]:
    """Allocate by ascending number, then path, using the first free second at or after the clock."""
    taken, second, mapping = set(occupied), clock, []
    for source in sorted(sources, key=lambda path: (int(Path(path).name.split("-", 1)[0]), path)):
        while stamp(second) in taken:
            second += timedelta(seconds=1)
        taken.add(stamp(second))
        suffix = Path(source).name.split("-", 1)[1]
        mapping.append({"from": source, "to": f"{Path(source).parent.as_posix()}/{stamp(second)}-{suffix}"})
        second += timedelta(seconds=1)
    return mapping


def reference_numbers(sources: list[str], highest: int) -> list[dict]:
    """Allocate after the highest occupied number, by parsed time, then path, with three digits or more."""
    ordered = sorted(sources, key=lambda path: (Path(path).name[:15], path))
    return [
        {"from": source, "to": f"{Path(source).parent.as_posix()}/{number:03d}-{Path(source).name[16:]}"}
        for number, source in enumerate(ordered, start=highest + 1)
    ]


@pytest.mark.parametrize("size", [0, 1, 5])
def test_generated_sequential_sets_follow_the_reference_allocator(sandbox, tmp_path, monkeypatch, size):
    """Invariant: previewed timestamps match an independent allocator, and approval applies exactly them."""
    rng = random.Random(SEED)
    sources = generate(rng, size, NUMBERS)
    now = datetime.now().replace(microsecond=0)
    second = lambda offset: stamp(now + timedelta(seconds=offset))  # noqa: E731
    existing = [f"specs/{second(offset)}-existing" for offset in FEATURE_SECONDS]
    root = make_project(tmp_path, "generated sequential", "sequential", [*sources, *existing])
    for offset in RESERVED_SECONDS:  # A directory without a specification still reserves its prefix.
        (root / "specs" / f"{second(offset)}-notes").mkdir()
    occupied = {second(offset) for offset in (*RESERVED_SECONDS, *FEATURE_SECONDS)}
    monkeypatch.chdir(root)
    before, tree = files(root), snapshot(root)
    context = f"seed={SEED} size={size} sources={sources}"

    reviewed = review("timestamp")

    expected = as_map(reference_timestamps(sources, occupied, datetime.fromisoformat(reviewed["clock"])))
    assert as_map(reviewed["mappings"]) == expected, context
    assert reviewed["changed_references"] == [] and reviewed["preference_changed"] is True, context
    assert snapshot(root) == tree, context
    applied = approve(reviewed["approval_token"], "timestamp", "--json")
    assert applied.exit_code == 0, context + applied.output
    assert as_map(payload(applied)["mappings"]) == expected, context
    assert_migrated(root, before, reviewed["mappings"], {".specify/init-options.json"}, context)


@pytest.mark.parametrize("size", [0, 2, 5])
def test_generated_timestamp_sets_follow_the_reference_allocator(sandbox, tmp_path, monkeypatch, size):
    """Invariant: numbers continue after the highest occupied number in any scope, including past 999."""
    rng = random.Random(SEED)
    sources = generate(rng, size, TIMESTAMPS)
    root = make_project(tmp_path, "generated timestamp", "timestamp", [*sources, "specs/998-kept", "specs/payments/999-edge"])
    monkeypatch.chdir(root)
    before = files(root)
    context = f"seed={SEED} size={size} sources={sources}"

    reviewed = review("sequential")

    expected = as_map(reference_numbers(sources, highest=999))
    assert as_map(reviewed["mappings"]) == expected, context
    assert reviewed["changed_references"] == [] and reviewed["preference_changed"] is True, context
    applied = approve(reviewed["approval_token"], "sequential", "--json")
    assert applied.exit_code == 0, context + applied.output
    assert as_map(payload(applied)["mappings"]) == expected, context
    assert_migrated(root, before, reviewed["mappings"], {".specify/init-options.json"}, context)


@pytest.mark.parametrize("scheme", ["sequential", "timestamp"])
def test_migration_to_timestamp_gives_synced_duplicate_prefixes_unique_names(sandbox, tmp_path, monkeypatch, scheme):
    """Invariant: the rename that `project info` suggests leaves no shared prefix and keeps every feature."""
    duplicates = ["specs/008-billing", "specs/008-login", "specs/team/008-nested"]
    root = make_project(tmp_path, "synced duplicates", scheme, [*duplicates, "specs/custom"])
    assert project_info(root, monkeypatch)["duplicate_prefixes"] == [duplicates]

    reviewed = review("timestamp")
    applied = approve(reviewed["approval_token"], "timestamp", "--json")

    assert applied.exit_code == 0, applied.output
    renamed = sorted(as_map(payload(applied)["mappings"]).values())
    assert len(renamed) == 3 and len({Path(path).name[:15] for path in renamed}) == 3
    assert all((root / path / "spec.md").is_file() for path in [*renamed, "specs/custom"])
    assert project_info(root, monkeypatch)["duplicate_prefixes"] == []


LEADS = ["a", "b", " ", "é", "日", "'"]
HAZARDS = ["\n", "\r", "\x1b[2J", "\x07", "\u202e", "\u2028", "\u0085"]


def hazardous_names(rng: random.Random, count: int) -> list[str]:
    """Names that put a forged report line and terminal controls inside one path component."""
    return [
        rng.choice(LEADS) + "\nFORGED-" + str(index) + rng.choice(HAZARDS) + rng.choice(HAZARDS) + "x"
        for index in range(count)
    ]


def test_untrusted_names_cannot_forge_report_lines_or_terminal_controls(project, tmp_path, monkeypatch):
    """Invariant: control characters in names never start a report line or reach the terminal."""
    names = hazardous_names(random.Random(SEED), 4)
    root = tmp_path / "hazard project"
    shutil.copytree(project.root, root)
    try:
        for index, name in enumerate(names, start=3):
            (root / "specs" / f"{index:03d}-{name}").mkdir()
            (root / "specs" / f"{index:03d}-{name}" / "spec.md").write_text("# Hazard\n", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"This file system rejects the generated names: {exc}")
    monkeypatch.chdir(root)
    context = f"seed={SEED} names={names!r}"

    human = run("--feature-numbering", "timestamp")
    machine = run("--feature-numbering", "timestamp", "--dry-run", "--json")

    assert human.exit_code in (0, 1) and machine.exit_code in (0, 1), context
    payload(machine)
    for text in (human.stdout, human.stderr, machine.stderr):
        assert all(char == "\n" or char.isprintable() for char in text), context
        assert not [line for line in text.splitlines() if line.lstrip().startswith("FORGED")], context


# ------------------------------------------------ shared workspace with real worktrees


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def history(*roots: Path) -> list[str]:
    """Refs, checked-out commits, reachable commits, worktrees, and local configuration."""
    views = (
        ("for-each-ref", "--format=%(refname) %(objectname)"), ("rev-parse", "HEAD"),
        ("log", "--all", "--format=%H %T %P %s"), ("worktree", "list", "--porcelain"),
        ("config", "--local", "--list"),
    )
    return [git(root, *view) for root in roots for view in views]


def project_info(root: Path, monkeypatch) -> dict:
    monkeypatch.chdir(root)
    result = CliRunner().invoke(app, ["project", "info", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.fixture
def shared(sandbox, tmp_path, monkeypatch):
    """One Git-backed external workspace, linked by `specify project link` from two real worktrees."""
    if shutil.which("git") is None:
        pytest.skip("Git is not installed.")
    workspace, first, second = (tmp_path / name for name in ("shared workspace", "first worktree", "second worktree"))
    (workspace / ".specify").mkdir(parents=True)
    (workspace / ".specify/workspace.json").write_text(
        json.dumps({"schema_version": 1, "project_id": PROJECT_ID}), encoding="utf-8"
    )
    (workspace / ".specify/init-options.json").write_text(
        json.dumps({**OPTIONS, "feature_selection": "automatic"}, indent=2) + "\n", encoding="utf-8"
    )
    write_features(workspace)
    (first / ".specify").mkdir(parents=True)
    (first / ".specify/project.json").write_text(
        json.dumps({"schema_version": 1, "storage": "external", "project_id": PROJECT_ID}), encoding="utf-8"
    )
    (first / "source.py").write_text("OLD = 'specs/001-alpha'\n", encoding="utf-8")
    for root in (workspace, first):
        git(root, "init", "-q", "-b", "main")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "Baseline")
    git(first, "worktree", "add", "-q", "-b", "second", str(second))
    monkeypatch.chdir(first)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    save_active_feature(resolve_project(first), workspace / "specs/001-alpha")
    monkeypatch.chdir(second)
    assert CliRunner().invoke(app, ["project", "link", str(workspace)]).exit_code == 0
    return SimpleNamespace(workspace=workspace, first=first, second=second, home=sandbox.home)


def test_worktrees_of_one_workspace_migrate_once_and_leave_git_alone(shared, monkeypatch):
    """Invariant: worktrees share features, not selections. A token binds one worktree. Git is untouched."""
    first = project_info(shared.first, monkeypatch)
    second = project_info(shared.second, monkeypatch)
    assert first["workspace_root"] == second["workspace_root"] == str(shared.workspace)
    assert first["project_id"] == second["project_id"] == PROJECT_ID
    assert (first["repository_root"], second["repository_root"]) == (str(shared.first), str(shared.second))
    assert (first["active_feature"], second["active_feature"]) == ("specs/001-alpha", None)

    before = files(shared.workspace)
    code = [snapshot(shared.first), snapshot(shared.second)]
    machine_before = snapshot(shared.home)
    git_before = history(shared.first, shared.workspace)

    monkeypatch.chdir(shared.first)
    reviewed = review("timestamp")
    assert (reviewed["repository_root"], reviewed["workspace_root"], reviewed["project_id"]) == (
        str(shared.first), str(shared.workspace), PROJECT_ID,
    )
    assert {"external-caller", "other-worktree"} <= {notice["kind"] for notice in reviewed["caller_notices"]}
    assert "selection" in {edit["owner"] for edit in reviewed["changed_references"]}
    assert files(shared.workspace) == before and snapshot(shared.home) == machine_before

    monkeypatch.chdir(shared.second)
    refused = approve(reviewed["approval_token"], "timestamp", "--json")
    assert refused.exit_code == 1, refused.output
    rejected = payload(refused)
    assert rejected["status"] == "rejected" and rejected["repository_root"] == str(shared.second)
    assert "root-mismatch" in codes(rejected)
    assert files(shared.workspace) == before and snapshot(shared.home) == machine_before

    monkeypatch.chdir(shared.first)
    applied = approve(reviewed["approval_token"])
    assert applied.exit_code == 0, applied.output
    for notice in reviewed["caller_notices"]:
        assert notice["message"] in applied.stdout, notice

    moved = {mapping["from"]: mapping["to"] for mapping in reviewed["mappings"]}
    alpha, beta = moved["specs/001-alpha"], moved["specs/002-beta"]
    edited = {f"{alpha}/spec.md", f"{beta}/spec.md", ".specify/init-options.json"}
    assert_migrated(shared.workspace, before, reviewed["mappings"], edited)
    assert (shared.workspace / alpha / "spec.md").read_bytes() == linked(ALPHA, "002-beta", Path(beta).name)
    for root, active in ((shared.first, alpha), (shared.second, None)):
        info = project_info(root, monkeypatch)
        assert info["workspace_root"] == str(shared.workspace) and info["project_id"] == PROJECT_ID
        assert info["active_feature"] == active and info["feature_numbering"] == "timestamp"
    after = [snapshot(shared.first), snapshot(shared.second)]
    # Only the first checkout's record follows its renamed selection.
    del after[0][".specify/checkout.json"], code[0][".specify/checkout.json"]
    assert after == code
    assert history(shared.first, shared.workspace) == git_before
    assert git(shared.workspace, "diff", "--cached", "--name-only") == ""

    monkeypatch.chdir(shared.second)
    again = run("--feature-numbering", "timestamp", "--json")
    assert again.exit_code == 0, again.output
    assert payload(again)["status"] == "noop"


# ----------------------------------------------------------- real process boundaries

COMMAND = [sys.executable, "-c", "from specify_cli import main; main()", "project", "migrate-naming"]


@pytest.fixture
def terminal():
    """A pseudo-terminal pair for a child's stdin. The test skips where the platform has none."""
    pty = pytest.importorskip("pty")
    try:
        master, slave = pty.openpty()
    except OSError as exc:
        pytest.skip(f"No pseudo-terminal is available: {exc}")
    yield master, slave
    for descriptor in (slave, master):
        with contextlib.suppress(OSError):
            os.close(descriptor)


def test_real_process_without_a_terminal_only_previews(project):
    """Invariant: a closed stdin is not approval, observed without replacing the terminal check."""
    before = state(project)
    result = subprocess.run(
        [*COMMAND, "--feature-numbering", "timestamp"], cwd=project.root, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "specs/001-alpha" in result.stdout
    assert state(project) == before


@pytest.mark.parametrize("answer, applied", [(b"n\n", False), (b"y\n", True)], ids=["decline", "accept"])
def test_real_terminal_asks_before_applying(project, terminal, answer, applied):
    """Invariant: on a real terminal only an explicit yes applies."""
    master, slave = terminal
    before = files(project.root)
    os.write(master, answer)
    result = subprocess.run(
        [*COMMAND, "--feature-numbering", "timestamp"], cwd=project.root, stdin=slave,
        capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode()
    if applied:
        assert_local_migration(project.root, before, mappings_on_disk(project.root))
    else:
        assert files(project.root) == before


def test_a_change_made_while_the_prompt_waits_is_refused(project, terminal):
    """Invariant: approval applies the previewed plan, so an edit made while the prompt waits is refused."""
    master, slave = terminal
    before = files(project.root)
    beta = project.root / "specs/002-beta/spec.md"
    edited = BETA.replace(b"Goals", b"Goalz")
    child = subprocess.Popen(
        [*COMMAND, "--feature-numbering", "timestamp"], cwd=project.root, stdin=slave,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        shown, deadline = b"", time.monotonic() + 60
        while b"specs/002-beta" not in shown:  # The preview names the feature before the prompt waits.
            assert time.monotonic() < deadline, shown
            if select.select([child.stdout], [], [], 0.1)[0]:
                chunk = os.read(child.stdout.fileno(), 4096)
                assert chunk, child.communicate()
                shown += chunk
        beta.write_bytes(edited)
        os.write(master, b"y\n")
        _, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == 1, err.decode()
    assert files(project.root) == {**before, "specs/002-beta/spec.md": edited}


@pytest.mark.parametrize("failure", ["record", "release"])
def test_lock_io_failures_keep_structured_results_and_report_actual_state(project, monkeypatch, failure):
    """Isolation or conservation: lock errors report the actual unchanged or committed project state."""
    reviewed = review("timestamp")
    before = state(project)
    lock = project.root / ".specify/naming-migration.lock"
    real_unlink = os.unlink
    if failure == "record":
        def fail_record(*args, **kwargs):
            raise OSError(errno.ENOSPC, "Injected lock write failure")

        monkeypatch.setattr(json, "dump", fail_record)
    else:
        def fail_release(path, *args, **kwargs):
            if Path(path) == lock:
                raise PermissionError(errno.EACCES, "Injected lock removal failure", str(path))
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", fail_release)
    result = approve(reviewed["approval_token"], "timestamp", "--json")
    assert result.exit_code == 1
    document = payload(result)
    if failure == "record":
        assert document["status"] == "rejected" and codes(document) == {"lock-unavailable"}
        assert state(project) == before
    else:
        assert document["status"] == "recovery_required" and codes(document) == {"lock-release-failed"}
        assert document["mappings"] == reviewed["mappings"]
        assert all((project.root / row["to"] / "spec.md").is_file() for row in reviewed["mappings"])
        assert json.loads((project.root / ".specify/init-options.json").read_text())["feature_numbering"] == "timestamp"
        assert [(row["operation"], row["from"]) for row in document["recovery"]["remaining_operations"]] == [
            ("remove-lock", str(lock))
        ]
        monkeypatch.setattr(os, "unlink", real_unlink)
        os.unlink(lock)
        shutil.rmtree(document["recovery"]["directory"])


def test_human_lock_release_failure_reports_the_committed_work(project, monkeypatch):
    """Conservation: a retained lock cannot hide the mappings or reverse committed feature data."""
    reviewed = review("timestamp")
    before = files(project.root)
    lock = project.root / ".specify/naming-migration.lock"
    real_unlink = os.unlink
    recovery = None

    def fail_release(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError(errno.EACCES, "Injected lock removal failure", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_release)
    try:
        result = approve(reviewed["approval_token"])
        match = re.search(r"^Recovery directory: (.+)$", result.stderr, re.MULTILINE)
        recovery = Path(match[1]) if match else None
        assert result.exit_code == 1 and lock.is_file()
        for mapping in reviewed["mappings"]:
            assert mapping["from"] in result.stdout and mapping["to"] in result.stdout, mapping
        for edit in reviewed["changed_references"]:
            assert edit["path"] in result.stdout and edit["new_path"] in result.stdout, edit
        for entry in reviewed["skipped"]:
            assert entry["path"] in result.stdout and entry["reason"] in result.stdout, entry
        for notice in reviewed["caller_notices"]:
            assert notice["message"] in result.stdout, notice
        assert "timestamp" in result.stdout and str(lock) in result.stderr
        assert_no_content(result)
        assert [recovery.resolve()] == [path.resolve() for path in (project.root / ".specify").glob("naming-recovery-*")]
    finally:
        monkeypatch.setattr(os, "unlink", real_unlink)
        if lock.exists():
            real_unlink(lock)
        for leftover in (project.root / ".specify").glob("naming-recovery-*"):
            shutil.rmtree(leftover)
    assert_local_migration(project.root, before, reviewed["mappings"])
    retry = run("--feature-numbering", "timestamp", "--dry-run", "--json")
    assert retry.exit_code == 0 and payload(retry)["status"] == "noop"


@pytest.mark.parametrize("json_output", [False, True], ids=["human", "json"])
@pytest.mark.parametrize("failure", ["record", "rollback"])
def test_combined_lock_failures_report_unchanged_or_restored_data(project, monkeypatch, failure, json_output):
    """Isolation: retained-lock diagnostics identify the refused or restored transaction without a success report."""
    reviewed = review("timestamp")
    before = state(project)
    lock = project.root / ".specify/naming-migration.lock"
    real_unlink = os.unlink
    recovery = None
    if failure == "record":
        def fail_record(*args, **kwargs):
            raise OSError(errno.ENOSPC, "Injected lock write failure")

        monkeypatch.setattr(json, "dump", fail_record)
    else:
        inject(monkeypatch, nth(1, preference_write))

    def fail_release(path, *args, **kwargs):
        if Path(path) == lock:
            raise PermissionError(errno.EACCES, "Injected lock removal failure", str(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", fail_release)
    try:
        result = approve(reviewed["approval_token"], "timestamp", *(["--json"] if json_output else []))
        match = re.search(r"^Recovery directory: (.+)$", result.stderr, re.MULTILINE)
        recovery = Path(match[1]) if match else None
        assert result.exit_code == 1 and lock.is_file()
        outcome = "rejected" if failure == "record" else "rolled back"
        assert outcome in result.stderr.lower()
        if json_output:
            document = payload(result)
            assert document["status"] == "recovery_required"
            assert codes(document) == {"lock-release-failed", "lock-unavailable" if failure == "record" else "apply-failed"}
            operations = document["recovery"]["remaining_operations"]
            assert [(row["operation"], row["from"], row["to"]) for row in operations] == [
                ("remove-lock", str(lock), str(lock))
            ]
            assert outcome in operations[0]["message"].lower()
        else:
            assert result.stdout == ""
        assert_no_content(result)
        assert [recovery.resolve()] == [path.resolve() for path in (project.root / ".specify").glob("naming-recovery-*")]
    finally:
        monkeypatch.setattr(os, "unlink", real_unlink)
        if lock.exists():
            real_unlink(lock)
        for leftover in (project.root / ".specify").glob("naming-recovery-*"):
            shutil.rmtree(leftover)
    assert state(project) == before
