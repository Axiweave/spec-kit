"""specify project recover: report leftovers, and restore only what a stopped owner left behind."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specify_cli import app

NON_JSON = b"Another merge attempt owns this lock.\n"  # the bytes of the merge lock race test

RENAME_STOP = """
import os, sys
from pathlib import Path
from specify_cli.project import naming
preview = naming.prepare_naming_migration(Path(sys.argv[1]), "timestamp")
assert preview.mappings and not preview.conflicts, preview.conflicts
write = naming._write
def stop(*args):
    write(*args)
    os._exit(9)  # A forced stop after the first file edit: no rollback and no cleanup.
naming._write = stop
naming.apply_naming_migration(preview)
"""

MERGE_STOP = """
import os, sys
from specify_cli.project import spec_merge
source, destination = sys.argv[1:3]
inspection = spec_merge.inspect_spec_sets(
    {"source": source, "source_kind": "set", "destination": destination, "destination_kind": "set"}
)
preview = spec_merge.prepare_spec_merge(inspection, {
    "snapshot_digest": inspection.snapshot_digest,
    "relationship": {"relationship": "same_project", "evidence": "One project."},
    "correspondences": [{"source_feature": "001-a", "destination_feature": "001-a", "relation": "revision"}],
    "artifact_decisions": [{"path": "001-a/spec.md", "resolution": "copy_source"}],
    "dependent_features": [],
})
assert preview.operations and not preview.conflicts, preview.conflicts
replace = os.replace
def stop(source, target, *args, **kwargs):
    replace(source, target, *args, **kwargs)
    if os.path.basename(target) == "spec.md":
        os._exit(9)  # A forced stop after the artifact write and before the journal marks it completed.
os.replace = stop
spec_merge.apply_spec_merge(preview)
"""


def put(root: Path, name: str, content: bytes | str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def tree(root: Path) -> dict[str, tuple]:
    """Every path under root with its bytes (None for a directory) and permission bits."""
    return {
        path.relative_to(root).as_posix(): (path.read_bytes() if path.is_file() else None, path.stat().st_mode & 0o777)
        for path in sorted(root.rglob("*"))
    }


def recover(*args: str):
    return CliRunner().invoke(app, ["project", "recover", *args])


def stop_child(script: str, *args: Path) -> None:
    child = subprocess.run([sys.executable, "-c", script, *map(str, args)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 9, child.stderr


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    put(repo, "specs/001-a/spec.md", "# A\n")
    put(repo, "specs/001-a/plan.md", "see [b](../002-b/spec.md)\n")
    put(repo, "specs/002-b/spec.md", "# B\n")
    put(repo, ".specify/init-options.json", '{"feature_numbering": "sequential"}\n')
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def stopped_pid() -> int:
    """The ID of a process that ran and was waited for."""
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=True)
    return int(child.stdout)


def merge_lock(pid: int) -> bytes:
    return json.dumps({"operation": "merge-specs", "pid": pid}).encode()


def test_diagnosis_lists_leftovers_and_exits_1_only_when_some_exist(project, stopped_pid):
    clean, clean_json = recover(), recover("--json")
    assert (clean.exit_code, clean.stdout) == (0, "No leftover resources.\n")
    assert (clean_json.exit_code, json.loads(clean_json.stdout)) == (0, {"resources": []})

    lock = put(project, "specs/.merge-specs.lock", merge_lock(stopped_pid))
    before = tree(project)
    text, document = recover(), recover("--json")

    assert text.exit_code == document.exit_code == 1
    assert f"merge-lock: {lock}" in text.stdout and f"owner_pid: {stopped_pid}" in text.stdout
    assert "owner: stopped" in text.stdout and "specify project recover --apply" in text.stdout
    assert json.loads(document.stdout) == {"resources": [{
        "kind": "merge-lock", "path": str(lock), "owner_pid": stopped_pid, "owner": "stopped",
        "choices": ["specify project recover --apply"],
    }]}
    assert tree(project) == before
    applied = recover("--apply")
    assert applied.exit_code == 0, applied.output
    assert not lock.exists()


def test_a_running_owner_is_refused_and_nothing_changes(project):
    lock = put(project, "specs/.merge-specs.lock", merge_lock(os.getpid()))
    put(project, "specs/.merge-specs-recovery-x/journal.json", json.dumps({"pid": os.getpid(), "operations": []}))
    before = tree(project)

    result = recover("--apply")

    assert result.exit_code == 1
    assert f"Owner process {os.getpid()} still runs: {lock}" in result.stderr
    assert tree(project) == before


@pytest.mark.parametrize("leftover", ["rename", "merge"])
def test_a_stopped_owner_is_restored_byte_identically_by_a_new_process(project, tmp_path, leftover):
    before = tree(project)
    if leftover == "rename":
        stop_child(RENAME_STOP, project)
        [backup] = (project / ".specify").glob("naming-recovery-*")
        # The backup outlives the stopped process and names it, so a new process can restore it.
        assert json.loads((backup / "manifest.json").read_text())["pid"] != os.getpid()
        assert (project / ".specify/naming-migration.lock").exists()
    else:
        put(tmp_path / "source", "001-a/spec.md", "# A from the source\n")
        stop_child(MERGE_STOP, tmp_path / "source", project / "specs")
        assert list((project / "specs").glob(".merge-specs-recovery-*/journal.json"))
    assert tree(project) != before

    diagnosis = recover("--json")
    assert {row["owner"] for row in json.loads(diagnosis.stdout)["resources"]} == {"stopped"}
    result = subprocess.run(
        [sys.executable, "-c", "from specify_cli import app; app()", "project", "recover", "--apply"],
        cwd=project, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert tree(project) == before


@pytest.mark.parametrize("leftover", ["rename", "merge"])
def test_a_file_changed_after_the_stop_is_refused_and_named(project, tmp_path, leftover):
    if leftover == "rename":
        stop_child(RENAME_STOP, project)
        [changed] = (project / "specs").rglob("plan.md")
    else:
        put(tmp_path / "source", "001-a/spec.md", "# A from the source\n")
        stop_child(MERGE_STOP, tmp_path / "source", project / "specs")
        changed = project / "specs/001-a/spec.md"
    changed.write_bytes(b"Edited after the stop.\n")
    before = tree(project)

    result = recover("--apply")

    assert result.exit_code == 1
    assert f"Changed after the stop, not restored: {changed.resolve()}" in result.stderr
    assert tree(project) == before


@pytest.mark.parametrize(("name", "content"), [
    ("specs/.merge-specs.lock", NON_JSON),
    ("specs/.merge-specs.lock", b'{"operation": "merge-specs"}'),
    ("specs/.merge-specs.lock", b'{"operation": "naming", "pid": 1}'),
    (".specify/naming-migration.lock", b'{"pid": 1}'),
    (".specify/naming-recovery-x/manifest.json", b'{"pid": "1"}'),
])
def test_an_unknown_owner_stays_byte_identical(project, name, content):
    put(project, name, content)
    before = tree(project)

    diagnosis = recover("--json")
    [row] = json.loads(diagnosis.stdout)["resources"]
    applied = recover("--apply")

    assert diagnosis.exit_code == 1 and (row["owner_pid"], row["owner"]) == (None, "unknown")
    assert applied.exit_code == 1
    assert f"Cannot confirm that the owner stopped: {row['path']}. Stop every writer" in applied.stderr
    assert tree(project) == before


def test_a_move_recovery_folder_is_report_only(project):
    folder = put(project, ".specify-move-x/specs/001-a/spec.md", "# Original A\n").parents[2]
    before = tree(project)

    diagnosis = recover("--json")
    applied = recover("--apply")

    [row] = json.loads(diagnosis.stdout)["resources"]
    assert (row["kind"], row["path"], row["owner"]) == ("move-recovery", str(folder), "unknown")
    assert "delete the folder" in row["choices"][0]
    assert applied.exit_code == 1 and f"Not changed: {folder}" in applied.stderr
    assert tree(project) == before
