---
description: Split a large piece of work into a roadmap of sub-features in specs/<name>/roadmap.md.
---

## User Input

```text
$ARGUMENTS
```

The user input is the description of the work to split. When the input starts with a path `specs/<name>/roadmap.md`, the rest of the input is the description. If the description is empty, ask the user for it and stop.

## Goal and boundaries

Write one new roadmap file. Each entry is one sub-feature that `__SPECKIT_COMMAND_SPECIFY__` can start alone.

- Create only `specs/<name>/roadmap.md`. Do not create feature folders, specs, or branches.
- Do not change any other file, the saved feature selection, or Git state.
- Do not run hooks or other Spec Kit commands. Run only the `specify` commands in the steps below.

## Steps

1. Run `specify feature list --json` from the code repository. If it exits with a non-zero status, show its stderr and stop. Read `workspace_root`, `features`, and `roadmaps` from the JSON.
2. If the description holds only one sub-feature, write no file. Tell the user to run `__SPECKIT_COMMAND_SPECIFY__` with the description, and stop. A sub-feature is work that a team can specify, plan, implement, and ship alone.
3. Select the roadmap folder name `<name>`:
   - If the input starts with `specs/<name>/roadmap.md`, use that `<name>`.
   - Otherwise, make a 2-4 word name from the description, for example `billing-portal`.
   - Use only lowercase letters, digits, and `-`. Do not start the name with `.` or `-`.
   - If `<workspace_root>/specs/<name>` exists, stop and name it. When the folder has a `roadmap.md`, tell the user to edit that file. Otherwise, tell the user to give another name as `specs/<name>/roadmap.md`.
4. Run `specify preset resolve roadmap-template`. The output has a `roadmap-template:` line, then the path of the active template, then a `(top layer from: …)` line. A long path can wrap over two or more lines, so join the path lines. Read the file at that path.
5. Split the description into entries:
   - Each entry is one sub-feature.
   - Add only the work that the description asks for. Use as few entries as the work permits.
   - If a feature in `features` already does part of the work, leave that part out. Name that feature in the report.
   - Give the IDs `R1`, `R2`, and so on, in row order. Put each entry after the entries that it depends on.
   - **Sub-feature**: a short name.
   - **Intent**: one sentence about the value of the sub-feature.
   - **Scope boundary**: what is in the entry, and what a sibling entry does, by ID. For example: "Read-only views. Editing is in R3."
   - **Depends on**: a comma-separated list of the IDs that must be done first, or `—`. Name only earlier IDs, so that there is no cycle.
   - **Status**: `planned`. **Sub-spec**: `—`.
   - Do not use the `|` character in a cell.
6. Write `<workspace_root>/specs/<name>/roadmap.md` from the template:
   - Replace `[EPIC NAME]` and the summary placeholder.
   - Keep the status legend, the ID rule, the table header, and the column order.
   - Replace the example row with one row for each entry.
7. Run `specify feature list --json` again. Make sure that these conditions are true:
   - `roadmaps` has an item with the `path` `specs/<name>/roadmap.md` and all the entries.
   - No `roadmap_findings` item has `specs/<name>/roadmap.md` as its `roadmap` or its `file`.

   If a condition is false, fix the file and do this step again.
8. Report:
   - The roadmap path.
   - A table with the ID, Sub-feature, and Depends on of each entry.
   - The existing features that already do part of the work, from step 5.
   - For each entry whose `ready` is true, the next command: `__SPECKIT_COMMAND_SPECIFY__ specs/<name>/roadmap.md <ID>`.
