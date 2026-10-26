"""The feature report gives one stage, its blockers, and one next step per feature, and changes nothing."""
from __future__ import annotations

import json
import os
import random
from pathlib import Path

import pytest

from specify_cli.features import todo
from specify_cli.features.todo import STAGES, report

SEED = 8008
_TASKS_5 = "".join(f"- [ ] T{i:03} Task\n" for i in range(1, 6))


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


@pytest.fixture
def unlistable():
    """Remove permissions from a folder (all of them by default) and restore them after the test."""
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("folder permissions do not deny listing on Windows or as root")
    locked: list[Path] = []

    def lock(path: Path, mode: int = 0) -> None:
        path.chmod(mode)
        locked.append(path)

    yield lock
    for path in locked:
        path.chmod(0o755)


def put(root: Path, rel: str, text: str = "# x\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tasks(checked: int, total: int) -> str:
    return "".join(f"- [{'x' if i <= checked else ' '}] T{i:03} Task\n" for i in range(1, total + 1))


def features(root: Path) -> dict[str, dict]:
    return {item["path"]: item for item in report(root)["features"]}


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


# --- discovery -------------------------------------------------------------------------------


def test_discovery_lists_features_and_prefixed_folders_without_a_regular_spec(tmp_path):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/web/002-b/spec.md")
    put(tmp_path, "specs/003-c/plan.md")
    put(tmp_path, "specs/billing-portal/roadmap.md")
    put(tmp_path, "specs/.merge-specs-recovery-x/004-d/spec.md")
    (tmp_path / "specs/005-e").mkdir()
    put(tmp_path, "specs/notes.md")

    assert todo._discover(tmp_path) == [
        ("specs/001-a", None),
        ("specs/003-c", todo.Finding("spec.md", "no-spec")),
        ("specs/005-e", todo.Finding("spec.md", "no-spec")),
        ("specs/web/002-b", None),
    ]


def test_a_symlinked_spec_is_reported_and_never_read(tmp_path, monkeypatch):
    put(tmp_path, "elsewhere.md", "[NEEDS CLARIFICATION: outside]\n")
    (tmp_path / "specs/001-link").mkdir(parents=True)
    _symlink_or_skip(tmp_path / "specs/001-link/spec.md", tmp_path / "elsewhere.md")
    read = Path.read_text

    def guarded(self, *args, **kwargs):
        assert self.name != "spec.md", "the symlinked spec.md was read"
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)

    row = features(tmp_path)["specs/001-link"]

    assert (row["stage"], row["next_step"], row["markers"]) == ("unreadable", None, [])
    assert row["findings"] == [{"file": "spec.md", "reason": "symlink"}]


def test_a_symlinked_feature_folder_is_not_followed(tmp_path):
    put(tmp_path, "elsewhere/001-a/spec.md")
    (tmp_path / "specs").mkdir()
    _symlink_or_skip(tmp_path / "specs/001-a", tmp_path / "elsewhere/001-a", target_is_directory=True)

    assert report(tmp_path)["features"] == []


def test_a_symlinked_specs_root_is_refused(tmp_path):
    put(tmp_path, "elsewhere/001-a/spec.md")
    _symlink_or_skip(tmp_path / "specs", tmp_path / "elsewhere", target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        report(tmp_path)


@pytest.mark.parametrize("make", [lambda root: None, lambda root: (root / "specs").mkdir()], ids=["missing", "empty"])
def test_no_specs_root_gives_no_features_and_recommends_specify(tmp_path, make):
    make(tmp_path)

    data = report(tmp_path)

    assert data["features"] == []
    assert data["recommendation"] == {"feature": None, "next_step": "speckit.specify", "rule": "no-unfinished"}


# --- safe reads ------------------------------------------------------------------------------


def test_a_symlinked_plan_is_unreadable(tmp_path):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "real-plan.md")
    _symlink_or_skip(tmp_path / "specs/001-a/plan.md", tmp_path / "real-plan.md")

    row = features(tmp_path)["specs/001-a"]

    assert (row["stage"], row["blocked"], row["next_step"]) == ("unreadable", True, None)
    assert row["findings"] == [{"file": "plan.md", "reason": "symlink"}]


def test_a_file_that_fails_to_read_is_unreadable(tmp_path, monkeypatch):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/001-a/tasks.md", tasks(1, 2))
    read = Path.read_text

    def failing(self, *args, **kwargs):
        if self.name == "tasks.md":
            raise PermissionError("denied")
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)

    row = features(tmp_path)["specs/001-a"]

    assert (row["stage"], row["tasks"]) == ("unreadable", None)
    assert row["findings"] == [{"file": "tasks.md", "reason": "unreadable"}]


def test_a_non_utf8_file_is_unreadable(tmp_path):
    put(tmp_path, "specs/001-a/spec.md")
    (tmp_path / "specs/001-a/plan.md").write_bytes(b"\xff\xfe\x00bad")

    assert features(tmp_path)["specs/001-a"]["findings"] == [{"file": "plan.md", "reason": "unreadable"}]


def test_an_unlistable_checklists_folder_is_a_finding(tmp_path, unlistable):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/001-a/checklists/ux.md", "- [ ] a\n")
    put(tmp_path, "specs/002-b/spec.md")
    unlistable(tmp_path / "specs/001-a/checklists")

    rows = features(tmp_path)

    assert (rows["specs/001-a"]["stage"], rows["specs/001-a"]["next_step"]) == ("unreadable", None)
    assert rows["specs/001-a"]["findings"] == [{"file": "checklists", "reason": "unreadable"}]
    assert rows["specs/002-b"]["stage"] == "spec-ready"


def test_unlistable_folders_are_findings_and_the_rest_is_reported(tmp_path, unlistable):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/008-x/spec.md")
    put(tmp_path, "specs/008-y/spec.md")
    put(tmp_path, "specs/003-locked/spec.md")
    put(tmp_path, "specs/web/004-hidden/spec.md")
    unlistable(tmp_path / "specs/003-locked")
    unlistable(tmp_path / "specs/web")

    data = report(tmp_path)
    rows = {row["path"]: row for row in data["features"]}

    for path in ("specs/003-locked", "specs/web"):
        assert (rows[path]["stage"], rows[path]["next_step"]) == ("unreadable", None)
        assert rows[path]["findings"] == [{"file": ".", "reason": "unreadable"}]
    assert rows["specs/001-a"]["stage"] == "spec-ready"
    assert data["duplicate_prefixes"] == [["specs/008-x", "specs/008-y"]]
    assert (data["roadmaps"], data["roadmap_findings"]) == ([], [])


def test_an_unlistable_specs_root_is_an_error(tmp_path, unlistable):
    put(tmp_path, "specs/001-a/spec.md")
    unlistable(tmp_path / "specs")

    with pytest.raises(PermissionError):
        report(tmp_path)


# --- parsers ---------------------------------------------------------------------------------


def test_markers_count_real_forms_only():
    spec = (
        "# Spec\n"
        "- FR-1: use [NEEDS CLARIFICATION: which auth?]\n"
        "The words NEEDS CLARIFICATION in prose do not count.\n"
        "Quoted `[NEEDS CLARIFICATION: x]` does not count.\n"
        "```text\n"
        "[NEEDS CLARIFICATION: inside a fence]\n"
        "```\n"
        "- FR-2: keep [NEEDS CLARIFICATION: retention?]\n"
    )
    plan = (
        "**Language/Version**: [e.g., Python 3.11 or NEEDS CLARIFICATION]\n"
        "**Testing**: NEEDS CLARIFICATION\n"
        "**Storage**: files\n"
    )

    assert todo._markers(spec, plan=False) == [2, 8]
    assert todo._markers(plan, plan=True) == [2]
    assert todo._markers("**Testing**: NEEDS CLARIFICATION\n", plan=False) == []


def test_task_lines_need_a_checkbox_and_an_id():
    text = "- [ ] T001 a\n- [x] T002 b\n- [X] T003 c\n    - [ ] T004 nested\n- [ ] no id\n* [ ] T005 star\n"

    assert todo._tasks(text) == todo.TaskProgress(2, 4, ["T001", "T004"])


def test_checklist_counts_follow_the_implement_gate():
    text = "- [x] a\n- [X] b\n- [ ] c\n  - [ ] d\n- plain\n"

    assert todo._checklist(text, "checklists/x.md") == todo.ChecklistCount("checklists/x.md", 2, 4)


# --- stage rules -----------------------------------------------------------------------------


def _fixture(root: Path) -> None:
    """The quickstart.md section 1 project."""
    marked = "".join(f"- FR-{i}: [NEEDS CLARIFICATION: q{i}]\n" for i in range(3))
    put(root, "specs/001-clarify/spec.md", marked)
    put(root, "specs/002-spec/spec.md")
    for name in ("003-planned", "004-ready", "005-almost", "006-done", "007-empty"):
        put(root, f"specs/{name}/spec.md")
        put(root, f"specs/{name}/plan.md", "**Testing**: [e.g., pytest or NEEDS CLARIFICATION]\n")
    put(root, "specs/004-ready/tasks.md", _TASKS_5)
    almost = tasks(106, 106).replace("- [x] T065", "- [ ] T065")
    put(root, "specs/005-almost/tasks.md", almost)
    put(root, "specs/006-done/tasks.md", tasks(3, 3))
    put(root, "specs/007-empty/tasks.md", "# Tasks\n\nNone yet.\n")
    put(root, "specs/008-review/spec.md")
    put(root, "specs/008-review/checklists/security.md", "- [ ] a\n- [ ] b\n- [x] c\n")
    put(root, "specs/009-reqs/spec.md")
    put(root, "specs/009-reqs/checklists/requirements.md", "- [x] a\n- [ ] b\n")
    put(root, "specs/010-nospec/plan.md")
    put(root, "specs/roadmap/roadmap.md")


@pytest.mark.parametrize("path, stage, blocked, next_step", [
    ("specs/001-clarify", "needs-clarification", True, "speckit.clarify"),
    ("specs/002-spec", "spec-ready", False, "speckit.plan"),
    ("specs/003-planned", "planned", False, "speckit.tasks"),
    ("specs/004-ready", "tasks-ready", False, "speckit.implement"),
    ("specs/005-almost", "in-progress", False, "speckit.implement"),
    ("specs/006-done", "done", False, None),
    ("specs/007-empty", "tasks-ready", True, "speckit.tasks"),
    ("specs/008-review", "spec-ready", False, "speckit.plan"),
    ("specs/009-reqs", "spec-ready", True, "speckit.clarify"),
    ("specs/010-nospec", "no-spec", True, None),
])
def test_each_fixture_feature_gets_its_stage_and_next_step(tmp_path, path, stage, blocked, next_step):
    _fixture(tmp_path)

    row = features(tmp_path)[path]

    assert (row["stage"], row["blocked"], row["next_step"]) == (stage, blocked, next_step)


def test_fixture_details(tmp_path):
    _fixture(tmp_path)

    rows = features(tmp_path)

    assert "specs/roadmap" not in rows
    assert [m["line"] for m in rows["specs/001-clarify"]["markers"]] == [1, 2, 3]
    assert rows["specs/005-almost"]["tasks"] == {"checked": 105, "total": 106, "open_ids": ["T065"]}
    assert rows["specs/007-empty"]["tasks"] == {"checked": 0, "total": 0, "open_ids": []}
    assert rows["specs/008-review"]["custom_checklists"] == [{"file": "checklists/security.md", "open": 2, "total": 3}]
    assert rows["specs/009-reqs"]["requirements"] == {"file": "checklists/requirements.md", "open": 1, "total": 2}
    assert rows["specs/010-nospec"]["findings"] == [{"file": "spec.md", "reason": "no-spec"}]


def test_precedence_and_routing(tmp_path):
    put(tmp_path, "specs/001-stale/spec.md", "[NEEDS CLARIFICATION: left over]\n")
    put(tmp_path, "specs/001-stale/tasks.md", tasks(2, 2))
    put(tmp_path, "specs/002-both/spec.md")
    put(tmp_path, "specs/002-both/tasks.md", "")
    put(tmp_path, "specs/002-both/checklists/requirements.md", "- [ ] open\n")
    put(tmp_path, "specs/003-done-review/spec.md")
    put(tmp_path, "specs/003-done-review/tasks.md", tasks(1, 1))
    put(tmp_path, "specs/003-done-review/checklists/ux.md", "- [ ] open\n")

    rows = features(tmp_path)

    assert rows["specs/001-stale"]["stage"] == "needs-clarification"
    assert (rows["specs/002-both"]["stage"], rows["specs/002-both"]["next_step"]) == ("tasks-ready", "speckit.clarify")
    assert (rows["specs/003-done-review"]["stage"], rows["specs/003-done-review"]["blocked"]) == ("done", False)


def _random_feature(rng: random.Random, root: Path, name: str) -> str:
    """Write a random mix of artifacts and return a description for failure messages."""
    parts = []
    if rng.random() < 0.9:
        put(root, f"specs/{name}/spec.md", "[NEEDS CLARIFICATION: q]\n" if rng.random() < 0.2 else "# s\n")
        parts.append("spec")
    if rng.random() < 0.6:
        put(root, f"specs/{name}/plan.md", "**Testing**: NEEDS CLARIFICATION\n" if rng.random() < 0.1 else "# p\n")
        parts.append("plan")
    if rng.random() < 0.5:
        total = rng.randint(0, 6)
        checked = rng.randint(0, total)
        put(root, f"specs/{name}/tasks.md", tasks(checked, total))
        parts.append(f"tasks {checked}/{total}")
    for checklist in ("requirements.md", "review.md"):
        if rng.random() < 0.3:
            put(root, f"specs/{name}/checklists/{checklist}", "- [ ] a\n" if rng.random() < 0.5 else "- [x] a\n")
            parts.append(checklist)
    (root / "specs" / name).mkdir(parents=True, exist_ok=True)
    return f"{name}: {', '.join(parts) or 'empty'}"


def test_generated_features_keep_the_stage_invariants(tmp_path):
    rng = random.Random(SEED)
    for case in range(40):
        root = tmp_path / str(case)
        made = [_random_feature(rng, root, f"{i:03}-f") for i in range(1, 5)]
        for row in report(root)["features"]:
            context = f"seed={SEED} case={case} files={made} row={row}"
            stage, next_step, row_tasks = row["stage"], row["next_step"], row["tasks"]
            assert stage in STAGES, context
            assert (next_step is None) == (stage in ("done", "no-spec", "unreadable")), context
            assert (stage == "done") == (
                not row["markers"] and not row["findings"] and row_tasks is not None
                and row_tasks["total"] >= 1 and row_tasks["checked"] == row_tasks["total"]
            ), context
            if row_tasks is not None and row_tasks["total"] == 0:
                assert stage != "done" and row["blocked"], context
            if row["custom_checklists"] and not row["blocked"]:
                assert next_step == STAGES[stage][1], context


# --- recommendation --------------------------------------------------------------------------


def test_recommendation_prefers_the_furthest_unblocked_feature(tmp_path):
    _fixture(tmp_path)

    data = report(tmp_path)
    order = [row["path"] for row in data["features"]]

    assert data["recommendation"] == {
        "feature": "specs/005-almost", "next_step": "speckit.implement", "rule": "finish-before-start",
    }
    assert order == [
        "specs/005-almost", "specs/004-ready", "specs/003-planned", "specs/002-spec", "specs/008-review",
        "specs/001-clarify", "specs/007-empty", "specs/009-reqs", "specs/010-nospec", "specs/006-done",
    ]


def test_all_blocked_projects_resolve_a_blocker_first(tmp_path):
    put(tmp_path, "specs/002-clarify/spec.md", "[NEEDS CLARIFICATION: q]\n")
    put(tmp_path, "specs/001-nospec/plan.md")

    assert report(tmp_path)["recommendation"] == {
        "feature": "specs/002-clarify", "next_step": "speckit.clarify", "rule": "resolve-blocker",
    }

    (tmp_path / "specs/002-clarify/spec.md").unlink()
    (tmp_path / "specs/002-clarify").rmdir()

    assert report(tmp_path)["recommendation"] == {
        "feature": "specs/001-nospec", "next_step": None, "rule": "resolve-blocker",
    }


def test_all_done_recommends_specify(tmp_path):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/001-a/tasks.md", tasks(2, 2))

    assert report(tmp_path)["recommendation"] == {"feature": None, "next_step": "speckit.specify", "rule": "no-unfinished"}


def test_generated_reports_recommend_the_first_unfinished_row_whatever_the_creation_order(tmp_path):
    rng = random.Random(SEED)
    for case in range(25):
        specs = [(f"{i:03}-f", rng.random()) for i in range(1, 7)]
        dumps = []
        for order in (specs, list(reversed(specs))):
            root = tmp_path / f"{case}-{len(dumps)}"
            for name, salt in order:
                _random_feature(random.Random(salt), root, name)
            data = report(root)
            data.pop("workspace_root")
            dumps.append(json.dumps(data))
        context = f"seed={SEED} case={case}"
        assert dumps[0] == dumps[1], context
        data = json.loads(dumps[0])
        first = next((row for row in data["features"] if row["stage"] != "done"), None)
        if first is None:
            assert data["recommendation"]["rule"] == "no-unfinished", context
        else:
            assert data["recommendation"]["feature"] == first["path"], context
            assert data["recommendation"]["next_step"] == first["next_step"], context


# --- stable and read-only --------------------------------------------------------------------


def test_report_is_stable_and_changes_nothing(tmp_path):
    _fixture(tmp_path)
    put(tmp_path, ".specify/feature.json", '{"feature_directory": "specs/002-spec"}\n')
    before = snapshot(tmp_path)

    first, second = json.dumps(report(tmp_path)), json.dumps(report(tmp_path))

    assert first == second
    assert snapshot(tmp_path) == before


def test_fifty_features_list_every_unfinished_row_before_the_done_ones(tmp_path):
    rng = random.Random(SEED)
    done = set()
    for i in range(1, 51):
        name = f"{i:03}-f"
        put(tmp_path, f"specs/{name}/spec.md")
        if rng.random() < 0.5:
            put(tmp_path, f"specs/{name}/tasks.md", tasks(3, 3))
            done.add(f"specs/{name}")

    stages = [(row["path"], row["stage"]) for row in report(tmp_path)["features"]]

    assert len(stages) == 50, f"seed={SEED}"
    assert [path for path, stage in stages if stage == "done"] == sorted(done), f"seed={SEED}"
    assert all(stage != "done" for _, stage in stages[: 50 - len(done)]), f"seed={SEED}"


# --- open task IDs ---------------------------------------------------------------------------


@pytest.mark.parametrize("open_count, ids", [(0, []), (1, ["T005"]), (3, ["T003", "T004", "T005"]), (4, [])])
def test_open_ids_appear_only_for_one_to_three_open_tasks(tmp_path, open_count, ids):
    put(tmp_path, "specs/001-a/spec.md")
    put(tmp_path, "specs/001-a/tasks.md", tasks(5 - open_count, 5))

    row = features(tmp_path)["specs/001-a"]

    assert row["tasks"]["open_ids"] == ids
    assert (row["stage"] == "done") == (open_count == 0)


def test_generated_task_lists_name_open_ids_in_file_order():
    rng = random.Random(SEED)
    for case in range(200):
        marks = [rng.choice(" xX") for _ in range(rng.randint(0, 8))]
        rng.shuffle(marks)
        text = "".join(f"- [{mark}] T{i:03} t\n" for i, mark in enumerate(marks, 1))
        expected_open = [f"T{i:03}" for i, mark in enumerate(marks, 1) if mark == " "]
        progress = todo._tasks(text)
        context = f"seed={SEED} case={case} marks={marks}"
        assert progress.total == len(marks), context
        assert progress.total - progress.checked == len(expected_open), context
        assert progress.open_ids == (expected_open if 1 <= len(expected_open) <= 3 else []), context


# --- roadmaps --------------------------------------------------------------------------------

_ROADMAP_HEAD = "| ID | Sub-feature | Intent | Scope boundary | Depends on | Status | Sub-spec |\n|---|---|---|---|---|---|---|\n"


def test_roadmaps_are_found_in_direct_child_folders_only(tmp_path):
    put(tmp_path, "specs/b-epic/roadmap.md", _ROADMAP_HEAD + "| R1 | a | i | b | — | planned | — |\n")
    put(tmp_path, "specs/a-epic/roadmap.md", _ROADMAP_HEAD)
    put(tmp_path, "specs/.merge-specs-x/roadmap.md", "# no table\n")
    put(tmp_path, "specs/.drafts/roadmap.md", "# a dot folder is never a roadmap folder\n")
    put(tmp_path, "specs/billing portal/roadmap.md", "# a back-reference cannot name a folder with whitespace\n")
    put(tmp_path, "specs/b-epic/001-sub/roadmap.md", "# nested below the root is not a roadmap\n")
    put(tmp_path, "outside/roadmap.md", "# no table\n")
    put(tmp_path, "specs/linked/spec.md")
    _symlink_or_skip(tmp_path / "specs/linked/roadmap.md", tmp_path / "outside/roadmap.md")
    _symlink_or_skip(tmp_path / "specs/dirlink", tmp_path / "outside", target_is_directory=True)

    data = report(tmp_path)

    assert [item["path"] for item in data["roadmaps"]] == [
        "specs/a-epic/roadmap.md", "specs/b-epic/roadmap.md", "specs/linked/roadmap.md",
    ]
    assert data["roadmap_findings"] == [{
        "kind": "unreadable-roadmap", "file": "specs/linked/roadmap.md", "line": None,
        "roadmap": "specs/linked/roadmap.md", "entry": None, "message": "The roadmap is not read (symlink).",
    }]
    assert data["roadmap_recommendation"] == {"roadmap": "specs/b-epic/roadmap.md", "entry": "R1", "next_step": "speckit.specify"}


def test_a_roadmap_in_a_folder_that_cannot_be_searched_is_unreadable(tmp_path, unlistable):
    put(tmp_path, "specs/epic/roadmap.md", _ROADMAP_HEAD)
    put(tmp_path, "specs/locked/notes.md")
    unlistable(tmp_path / "specs/epic", 0o444)  # listed, so roadmap.md is known, but not searchable
    unlistable(tmp_path / "specs/locked", 0o444)  # listed, but holds no roadmap.md

    data = report(tmp_path)

    assert [(f["kind"], f["file"], f["line"]) for f in data["roadmap_findings"]] == [
        ("unreadable-roadmap", "specs/epic/roadmap.md", None),
    ]



def test_a_folder_with_a_roadmap_is_an_epic_exactly_when_it_has_no_feature_file(tmp_path):
    names = ("spec.md", "plan.md", "tasks.md", "checklists/ux.md")
    subsets = [tuple(n for bit, n in enumerate(names) if mask >> bit & 1) for mask in range(16)]
    for number, subset in enumerate(subsets, 10):
        put(tmp_path, f"specs/0{number}-s/roadmap.md", _ROADMAP_HEAD)
        put(tmp_path, f"specs/0{number}-s/README.md")
        for name in subset:
            put(tmp_path, f"specs/0{number}-s/{name}", "- [ ] T001 open\n")

    rows = features(tmp_path)

    for number, subset in enumerate(subsets, 10):
        assert (f"specs/0{number}-s" in rows) == bool(subset), f"files={subset}"


def test_other_prefixed_folders_with_a_roadmap_stay_feature_rows(tmp_path):
    (tmp_path / "specs/011-empty").mkdir(parents=True)
    put(tmp_path, "outside.md")
    put(tmp_path, "specs/013-linked/README.md")
    _symlink_or_skip(tmp_path / "specs/013-linked/roadmap.md", tmp_path / "outside.md")
    put(tmp_path, "specs/web/014-deep/roadmap.md", _ROADMAP_HEAD)
    put(tmp_path, "specs/010-epic/roadmap.md", _ROADMAP_HEAD)
    put(tmp_path, "specs/010-epic/011-sub/plan.md")
    put(tmp_path, "specs/010-epic/011-sub/tasks.md", "- [ ] T001 open\n")
    put(tmp_path, "specs/001-my epic/roadmap.md", _ROADMAP_HEAD)  # not a roadmap folder, so not an epic folder

    rows = features(tmp_path)

    assert {path: row["stage"] for path, row in rows.items()} == {
        "specs/011-empty": "no-spec", "specs/013-linked": "no-spec", "specs/001-my epic": "no-spec",
        "specs/web/014-deep": "no-spec", "specs/010-epic/011-sub": "no-spec",
    }


def test_only_the_exact_name_roadmap_md_is_a_roadmap(tmp_path):
    # A case-insensitive file system (macOS, Windows) opens ROADMAP.md as roadmap.md. Linux does not.
    names = ("ROADMAP.md", "Roadmap.md", "roadmap.MD")
    for number, name in enumerate(names, 10):
        put(tmp_path, f"specs/0{number}-epic/{name}", _ROADMAP_HEAD + "| R1 | a | i | b | — | planned | — |\n")
        put(tmp_path, f"specs/epic-{number}/{name}", _ROADMAP_HEAD + "| R1 | a | i | b | — | planned | — |\n")

    data = report(tmp_path)

    assert [row["path"] for row in data["features"]] == ["specs/010-epic", "specs/011-epic", "specs/012-epic"]
    assert {row["stage"] for row in data["features"]} == {"no-spec"}
    assert (data["roadmaps"], data["roadmap_findings"]) == ([], [])


def test_only_exact_feature_file_names_keep_a_roadmap_folder_a_feature(tmp_path):
    # A regular file named checklists is not the checklists/ folder, so it does not make a feature either.
    for number, name in enumerate(("SPEC.md", "PLAN.md", "Tasks.md", "Checklists/ux.md", "checklists"), 10):
        put(tmp_path, f"specs/0{number}-epic/roadmap.md", _ROADMAP_HEAD)
        put(tmp_path, f"specs/0{number}-epic/{name}")

    data = report(tmp_path)

    assert data["features"] == []
    assert [roadmap["path"] for roadmap in data["roadmaps"]] == [f"specs/0{n}-epic/roadmap.md" for n in range(10, 15)]


def test_a_back_reference_to_a_missing_roadmap_is_a_finding_with_no_roadmap(tmp_path):
    put(tmp_path, "specs/001-a/spec.md", "# a\n\n**Input**: Parent roadmap: `specs/epic/roadmap.md` → entry **R1**. x\n")

    data = report(tmp_path)

    assert data["roadmaps"] == []
    assert [(f["kind"], f["file"], f["line"], f["entry"]) for f in data["roadmap_findings"]] == [
        ("missing-entry", "specs/001-a/spec.md", 3, "R1"),
    ]


def test_no_roadmap_adds_empty_roadmap_keys_last(tmp_path):
    _fixture(tmp_path)
    (tmp_path / "specs/roadmap/roadmap.md").unlink()

    data = report(tmp_path)

    assert list(data)[-3:] == ["roadmaps", "roadmap_findings", "roadmap_recommendation"]
    assert (data["roadmaps"], data["roadmap_findings"], data["roadmap_recommendation"]) == ([], [], None)


def test_generated_roadmaps_never_change_features_or_the_recommendation(tmp_path):
    rng = random.Random(SEED)
    for case in range(30):
        root = tmp_path / str(case)
        made = [_random_feature(rng, root, f"{i:03}-f") for i in range(1, 5)]
        before = report(root)
        rows = []
        for i in range(1, rng.randint(1, 6)):
            sub = rng.choice(["—", f"specs/{rng.randint(1, 5):03}-f", "../x"])
            deps = rng.choice(["—", f"R{rng.randint(1, 6)}"])
            rows.append(f"| R{i} | n | i | b | {deps} | {rng.choice(['planned', 'done', 'x'])} | {sub} |\n")
        put(root, "specs/epic/roadmap.md", _ROADMAP_HEAD + "".join(rows))
        after = report(root)
        context = f"seed={SEED} case={case} files={made} rows={rows}"
        assert (after["features"], after["recommendation"]) == (before["features"], before["recommendation"]), context
        assert after["roadmaps"][0]["path"] == "specs/epic/roadmap.md", context


def test_a_report_with_roadmaps_is_stable_and_changes_nothing(tmp_path):
    put(tmp_path, "specs/001-a/spec.md", "**Input**: Parent roadmap: `specs/epic/roadmap.md` → entry **R1**.\n")
    put(tmp_path, "specs/001-a/tasks.md", tasks(1, 1))
    put(tmp_path, "specs/002-b/spec.md", "Parent roadmap: specs/epic/roadmap.md -> entry R9\n")
    put(tmp_path, "specs/epic/roadmap.md", _ROADMAP_HEAD + "| R1 | a | i | b | — | in-progress | specs/001-a/ |\n| R2 | b | i | b | R1 | planned | — |\n")
    before = snapshot(tmp_path)

    first, second = json.dumps(report(tmp_path)), json.dumps(report(tmp_path))
    data = json.loads(first)

    assert first == second
    assert snapshot(tmp_path) == before
    assert [(f["kind"], f["file"], f["line"]) for f in data["roadmap_findings"]] == [
        ("missing-entry", "specs/002-b/spec.md", 1),
        ("status-drift", "specs/epic/roadmap.md", 3),
    ]
    assert [(e["id"], e["expected_status"], e["ready"]) for e in data["roadmaps"][0]["entries"]] == [
        ("R1", "done", False), ("R2", "planned", True),
    ]
