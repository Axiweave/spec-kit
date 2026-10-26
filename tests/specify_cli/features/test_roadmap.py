"""Roadmap tables and back-references parse the documented forms, and the drift and ready rules hold."""
from __future__ import annotations

import random

import pytest

from specify_cli.features.roadmap import COLUMNS, BackRef, check, parent_ref, parse
from specify_cli.features.todo import STAGES

SEED = 9009
_HEAD = ["ID", "Sub-feature", "Intent", "Scope boundary", "Depends on", "Status", "Sub-spec"]


def table(*rows: str, header: str = "| " + " | ".join(_HEAD) + " |") -> str:
    return "\n".join(["# Roadmap", "", header, "|" + "---|" * 7, *rows]) + "\n"


def row(entry_id: str, *, deps: str = "—", status: str = "planned", sub: str = "—") -> str:
    return f"| {entry_id} | name {entry_id} | intent | boundary | {deps} | {status} | {sub} |"


# --- back-reference ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, path, entry_id", [
    ("**Input**: Parent roadmap: `specs/billing-portal/roadmap.md` → entry **R2**. Text", "specs/billing-portal/roadmap.md", "R2"),
    ("Parent roadmap: specs/a/roadmap.md -> entry R3.", "specs/a/roadmap.md", "R3"),
    ("Parent roadmap: `specs/a/roadmap.md` → entry **R2.1**.", "specs/a/roadmap.md", "R2.1"),
    ("Parent roadmap: specs/a/roadmap.md → entry auth_v2-x", "specs/a/roadmap.md", "auth_v2-x"),
])
def test_back_reference_forms(line, path, entry_id):
    assert parent_ref(f"# Spec\r\n\r\n{line}\r\n") == BackRef(path, entry_id, 3)


def test_first_back_reference_outside_fences_wins():
    text = "```\nParent roadmap: specs/x/roadmap.md → entry R9\n```\nParent roadmap: specs/a/roadmap.md → entry R1\nParent roadmap: specs/b/roadmap.md → entry R2\n"
    assert parent_ref(text) == BackRef("specs/a/roadmap.md", "R1", 4)


def test_no_back_reference():
    assert parent_ref("# Spec\n\nParent roadmap is mentioned in prose only.\n") is None


# --- table ------------------------------------------------------------------------------------


def _random_value(rng: random.Random, words: list[str]) -> str:
    return " ".join(rng.choice(words) for _ in range(rng.randint(1, 3)))


def test_generated_tables_round_trip():
    rng = random.Random(SEED)
    print(f"seed={SEED}")
    for case in range(200):
        ids = [f"R{n}" for n in rng.sample(range(1, 50), rng.randint(0, 8))]
        expected = []
        for entry_id in ids:
            deps = rng.sample(ids, rng.randint(0, min(2, len(ids))))
            sub = rng.choice([None, f"specs/{rng.randint(1, 99):03}-x"])
            expected.append({
                "id": entry_id, "name": _random_value(rng, ["Invoice", "list", "PDF"]),
                "depends_on": deps, "status": rng.choice(["planned", "In-Progress", "DONE", "weird"]), "sub_spec": sub,
            })
        columns = [*_HEAD, *(["Notes"] if rng.random() < 0.5 else [])]
        rng.shuffle(columns)

        def cell(item: dict, column: str) -> str:
            value = {
                "ID": item["id"], "Sub-feature": item["name"], "Intent": "i", "Scope boundary": "b",
                "Depends on": ", ".join(item["depends_on"]) or rng.choice(["—", "–", "-", ""]),
                "Status": item["status"],
                "Sub-spec": (item["sub_spec"] + rng.choice(["", "/"])) if item["sub_spec"] else rng.choice(["—", ""]),
                "Notes": "n",
            }[column]
            return rng.choice(["{}", "**{}**", "`{}`"]).format(value) if value else value

        header = "| " + " | ".join(f"**{c}**" if rng.random() < 0.3 else c for c in columns) + " |"
        body = ["| " + " | ".join(cell(item, c) for c in columns) + " |" for item in expected]
        text = "\n".join(["# R", "", header, "|" + "|".join(":---" for _ in columns) + "|", *body, "", "After."])
        result = parse(text)
        context = f"seed={SEED} case={case}\n{text}"
        assert isinstance(result, list), context
        assert [(line, cells["id"]) for line, cells in result] == [(5 + i, e["id"]) for i, e in enumerate(expected)], context
        roadmaps, _, _ = check({"specs/e/roadmap.md": text}, {}, {})
        for got, want in zip(roadmaps[0].entries, expected):
            assert (got.name, got.depends_on, got.status, got.sub_spec) == (
                want["name"], want["depends_on"], want["status"].lower(), want["sub_spec"]), context


@pytest.mark.parametrize("text, line", [
    ("# Roadmap\n\nNo table.\n", None),
    ("```\n| " + " | ".join(_HEAD) + " |\n|---|---|---|---|---|---|---|\n```\n", None),
    ("| " + " | ".join(_HEAD[:-1]) + " |\n|---|---|---|---|---|---|\n", None),
    ("| " + " | ".join(_HEAD) + " |\n| R1 | a | b | c | — | planned | — |\n", 1),
    (table(row("R1"), "| R2 | x |"), 6),
    (table(row("R1"), row("")), 6),
    (table(row("R1"), row("R2"), row("R1")), 7),
])
def test_unreadable_tables_name_the_line(text, line):
    result = parse(text)
    assert isinstance(result, tuple) and result[0] == line, result
    _, findings, _ = check({"specs/e/roadmap.md": text}, {}, {})
    assert [(f.kind, f.line) for f in findings] == [("unreadable-roadmap", line)]


def test_a_header_with_no_rows_is_an_empty_roadmap():
    assert parse(table()) == []


def test_an_entry_id_is_accepted_exactly_when_a_back_reference_can_name_it():
    rng = random.Random(SEED)
    print(f"seed={SEED}")
    for case in range(500):
        entry_id = "".join(rng.choice("Rr09-_.#/ é") for _ in range(rng.randint(1, 6))).strip()
        if not entry_id:
            continue
        accepted = isinstance(parse(table(row(entry_id))), list)
        named = parent_ref(f"Parent roadmap: `{EPIC}` → entry **{entry_id}**. Text")
        assert accepted == (named is not None and named.entry == entry_id), f"seed={SEED} case={case} id={entry_id!r}"


# --- rules ------------------------------------------------------------------------------------

EPIC = "specs/epic/roadmap.md"


def ref(entry_id: str, roadmap: str = EPIC) -> BackRef:
    return BackRef(roadmap, entry_id, 3)


def run(rows: list[str], features: dict | None = None, *, unreadable: dict | None = None):
    return check({EPIC: table(*rows)}, unreadable or {}, features or {})


def kinds(findings) -> list[tuple[str, str, str | None]]:
    return [(f.kind, f.file, f.entry) for f in findings]


@pytest.mark.parametrize("stage", list(STAGES))
def test_expected_status_follows_the_sub_spec_stage(stage):
    want = "done" if stage == "done" else "in-progress"
    roadmaps, findings, _ = run([row("R1", status=want, sub="specs/001-a")], {"specs/001-a": (stage, ref("R1"))})
    assert roadmaps[0].entries[0].expected_status == want
    assert findings == []
    roadmaps, findings, _ = run([row("R1", status="planned", sub="specs/001-a")], {"specs/001-a": (stage, ref("R1"))})
    assert kinds(findings) == [("status-drift", EPIC, "R1")]
    assert findings[0].message == f"Stored status planned, expected {want}."


@pytest.mark.parametrize("status, drift", [("planned", False), ("PLANNED", False), ("done", True), ("todo", True), ("", True)])
def test_status_drift_without_a_sub_spec(status, drift):
    _, findings, _ = run([row("R1", status=status)])
    assert bool(findings) is drift and all(f.kind == "status-drift" for f in findings)


@pytest.mark.parametrize("sub", ["../x", "/abs", "specs/a/../b", "specs//b", "specs/999-missing", "other/001-a"])
def test_a_bad_sub_spec_is_a_broken_link_and_skips_the_drift_check(sub):
    _, findings, _ = run([row("R1", status="nonsense", sub=sub)], {"specs/001-a": ("done", ref("R1"))})
    assert ("broken-link", EPIC, "R1") in kinds(findings)
    assert not any(f.kind == "status-drift" for f in findings)


@pytest.mark.parametrize("back", [None, ref("R2"), ref("R1", "specs/other/roadmap.md")])
def test_a_sub_spec_that_does_not_point_back_is_a_broken_link(back):
    _, findings, _ = run([row("R1", status="in-progress", sub="specs/001-a"), row("R2")], {"specs/001-a": ("planned", back)})
    assert ("broken-link", EPIC, "R1") in kinds(findings)


@pytest.mark.parametrize("back, kind", [
    (ref("R1", "specs/epic/../x/roadmap.md"), "broken-link"),
    (ref("R1", "elsewhere/roadmap.md"), "broken-link"),
    (ref("R1", "specs/nope/roadmap.md"), "missing-entry"),
    (ref("R9"), "missing-entry"),
    (ref("R1"), "broken-link"),  # R1 has no sub-spec
])
def test_spec_side_findings_sit_at_the_spec_line(back, kind):
    _, findings, _ = run([row("R1")], {"specs/001-a": ("spec-ready", back)})
    spec_findings = [f for f in findings if f.file == "specs/001-a/spec.md"]
    assert [(f.kind, f.line) for f in spec_findings] == [(kind, 3)]


def test_an_unreadable_roadmap_gives_no_spec_side_finding():
    for texts, unreadable in (({EPIC: "no table"}, {}), ({}, {EPIC: "symlink"})):
        _, findings, _ = check(texts, unreadable, {"specs/001-a": ("spec-ready", ref("R1"))})
        assert kinds(findings) == [("unreadable-roadmap", EPIC, None)]


def test_two_specs_claiming_one_entry_flag_only_the_unnamed_one():
    features = {"specs/001-a": ("planned", ref("R1")), "specs/002-b": ("planned", ref("R1"))}
    _, findings, _ = run([row("R1", status="in-progress", sub="specs/001-a")], features)
    assert kinds(findings) == [("broken-link", "specs/002-b/spec.md", "R1")]


def test_ready_rule_on_generated_roadmaps():
    rng = random.Random(SEED)
    print(f"seed={SEED}")
    for case in range(300):
        ids = [f"R{n}" for n in range(1, rng.randint(1, 8) + 1)]
        rows, features, done = [], {}, set()
        for n, entry_id in enumerate(ids):
            deps = rng.sample([*ids, "R99"], rng.randint(0, min(3, len(ids) + 1)))
            stage = rng.choice([None, "done", "planned"])
            if stage:
                features[f"specs/{n:03}-f"] = (stage, ref(entry_id))
                done |= {entry_id} if stage == "done" else set()
            status = rng.choice(["planned", "in-progress", "done"])
            rows.append(row(entry_id, deps=", ".join(deps) or "—", status=status, sub=f"specs/{n:03}-f" if stage else "—"))
        if rng.random() < 0.3:  # a stray spec that claims an entry
            features["specs/900-stray"] = ("spec-ready", ref(rng.choice(ids)))
        roadmaps, findings, recommendation = run(rows, features)
        context = f"seed={SEED} case={case}\n" + "\n".join(rows)
        entries = {e.id: e for e in roadmaps[0].entries}
        flagged = {f.entry for f in findings}
        reach = {e.id: {d for d in e.depends_on if d in entries} for e in entries.values()}
        while True:  # transitive closure by fixed point, a slow reference for the cycle rule
            grown = {k: v.union(*(reach[d] for d in v)) for k, v in reach.items()}
            if grown == reach:
                break
            reach = grown
        for entry in entries.values():
            deps_done = all(dep in done for dep in entry.depends_on)
            looped = entry.id in reach[entry.id]
            assert entry.ready == (
                entry.expected_status == "planned" and entry.id not in flagged and deps_done and not looped
            ), context
            if "R99" in entry.depends_on or looped:
                assert not entry.ready, context
        ready = [e.id for e in roadmaps[0].entries if e.ready]
        assert (recommendation.entry if recommendation else None) == (ready[0] if ready else None), context


def test_planned_entries_in_a_cycle_are_never_ready():
    roadmaps, _, recommendation = run([row("R1", deps="R2"), row("R2", deps="R1"), row("R3", deps="R3")])
    assert [e.ready for e in roadmaps[0].entries] == [False, False, False]
    assert recommendation is None


def test_an_entry_in_a_cycle_is_never_ready_even_when_the_other_entry_is_done():
    features = {"specs/002-b": ("done", ref("R2"))}
    roadmaps, findings, recommendation = run(
        [row("R1", deps="R2"), row("R2", deps="R1", status="done", sub="specs/002-b")], features,
    )
    assert findings == []
    assert [(e.id, e.expected_status, e.ready) for e in roadmaps[0].entries] == [("R1", "planned", False), ("R2", "done", False)]
    assert recommendation is None


def test_recommendation_is_the_first_ready_entry_by_path_then_row():
    texts = {"specs/b/roadmap.md": table(row("R1")), "specs/a/roadmap.md": table(row("R5", deps="R4"), row("R4"), row("R6"))}
    _, _, recommendation = check(texts, {}, {})
    assert recommendation is not None
    assert (recommendation.roadmap, recommendation.entry, recommendation.next_step) == ("specs/a/roadmap.md", "R4", "speckit.specify")


def test_results_do_not_depend_on_input_order():
    texts = {f"specs/{c}/roadmap.md": table(row("R1", status="done"), row("R2", sub="specs/001-x")) for c in "abc"}
    features = {f"specs/00{i}-x": ("planned", ref("R2", f"specs/{c}/roadmap.md")) for i, c in enumerate("abc", 1)}
    first = check(texts, {"specs/z/roadmap.md": "symlink"}, features)
    for _ in range(10):
        items = list(texts.items())
        random.Random(SEED).shuffle(items)
        assert check(dict(items), {"specs/z/roadmap.md": "symlink"}, dict(reversed(features.items()))) == first


def test_columns_match_the_documented_set():
    assert {" ".join(c.lower().split()) for c in _HEAD} == set(COLUMNS)
