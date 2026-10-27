---
description: Split a large piece of work into a roadmap of sub-features in specs/<name>/roadmap.md.
---

## User Input

```text
$ARGUMENTS
```

The user input names the work to split. When the input starts with a path `specs/<name>/roadmap.md`, the rest of the input is the description. The findings, decisions, owners, and sources about that work in the current conversation are also part of the description. If the input is empty and the conversation names no work, ask the user for a description and stop.

## Goal and boundaries

Write one brief and one new roadmap file. Each entry is one sub-feature that `__SPECKIT_COMMAND_SPECIFY__` can start alone.

- Create only `specs/<name>/brief.md` and `specs/<name>/roadmap.md`. Do not create feature folders, specs, or branches.
- Do not change any other file, the saved feature selection, or Git state.
- Do not run hooks or other Spec Kit commands. Run only the `specify` commands in the steps below.

## Steps

1. Run `specify feature list --json` from the code repository. If it exits with a non-zero status, show its stderr and stop. Read `workspace_root`, `features`, and `roadmaps` from the JSON.
2. Judge the size of the work from the user input and the conversation together, not from the input alone. If the work holds only one sub-feature, write no file. Tell the user to run `__SPECKIT_COMMAND_SPECIFY__` with the description, and stop. A sub-feature is work that a team can specify, plan, implement, and ship alone.
3. Select the roadmap folder name `<name>`:
   - If the input starts with `specs/<name>/roadmap.md`, use that `<name>`.
   - Otherwise, make a 2-4 word name from the description, for example `billing-portal`.
   - Use only lowercase letters, digits, and `-`. Do not start the name with `.` or `-`.
   - If `<workspace_root>/specs/<name>/roadmap.md` exists, stop and tell the user to edit that file.
   - If `<workspace_root>/specs/<name>/brief.md` exists, use that brief in step 5. Do not write it again.
   - If `<workspace_root>/specs/<name>` exists with neither file, stop and name it. Tell the user to give another name as `specs/<name>/roadmap.md`.
4. Run `specify preset resolve roadmap-template`. Its first line is `roadmap-template: <path>` after leading spaces, with the path of the active template. Read the file at that path.
5. Write the brief `<workspace_root>/specs/<name>/brief.md`. Skip this step when step 3 found a brief.
   - Take the content from the user input and the current conversation.
   - Use these headings: `# Brief: <work>`, `## Findings`, `## Decisions`, and `## Sources`. Leave out a heading with no content.
   - **Findings**: one bullet for each finding: what is wrong or missing, the owner when the work waits on someone, and the source.
   - **Decisions**: one bullet for each decision: a title, the choice, each rejected option with a one-line reason, and a source as a PR number or a file path. A decision number is never the only name of a decision, because projects renumber decisions.
   - When a later decision replaces an earlier finding, keep only the later decision.
   - If part of the conversation was compacted, write only the findings that you can see. Do not invent the lost findings. Remember this for the report.
6. Read the brief again. From now on, use only the brief as the description. Split it into entries:
   - Each entry is one sub-feature.
   - Put each finding of the brief in the scope boundary of one entry. Keep a list of the findings that fit no entry. Use as few entries as the work permits.
   - If a feature in `features` already does part of the work, leave that part out. Name that feature in the report.
   - Give the IDs `R1`, `R2`, and so on, in row order. Put each entry after the entries that it depends on.
   - **Sub-feature**: a short name.
   - **Intent**: one sentence about the value of the sub-feature.
   - **Scope boundary**: what is in the entry, and what a sibling entry does, by ID. For example: "Read-only views. Editing is in R3."
   - **Depends on**: a comma-separated list of the IDs that must be done first, or `—`. Name only earlier IDs, so that there is no cycle. Put only roadmap IDs here.
   - **Waits on**: the external change, person, or team that must act first, with the owner when the brief names one, for example `PR #15 (alice)`. Otherwise `—`.
   - **Status**: `planned`. **Sub-spec**: `—`.
   - Do not use the `|` character in a cell.
7. Write `<workspace_root>/specs/<name>/roadmap.md` from the template:
   - Replace `[EPIC NAME]` and the summary placeholder.
   - Keep the status legend, the ID rule, the table header, and the column order.
   - Replace the example row with one row for each entry.
   - Fill `## Context` from the brief: the problem, the reason for doing the work now, `### Decisions` in the template form, and `### Sources`. Leave out a part with no content.
   - Do not add an out-of-scope list or any other section. A finding that fits no entry goes only into the report of step 9.
8. Run `specify feature list --json` again. Make sure that these conditions are true:
   - `roadmaps` has an item with the `path` `specs/<name>/roadmap.md` and all the entries.
   - No `roadmap_findings` item has `specs/<name>/roadmap.md` as its `roadmap` or its `file`.

   If a condition is false, fix the file and do this step again.
9. Report:
   - The brief path and the roadmap path.
   - A table with the ID, Sub-feature, Depends on, and Waits on of each entry.
   - Each finding of the brief that fits no entry. Ask the user to place it in an entry or to drop it from the brief.
   - The existing features that already do part of the work, from step 6.
   - When part of the conversation was compacted, say that the brief can miss findings, and that the user can add them to the brief and run this command again after deleting `roadmap.md`.
   - For each entry whose `ready` is true, the next command: `__SPECKIT_COMMAND_SPECIFY__ specs/<name>/roadmap.md <ID>`. When no entry is ready, say so and name what each entry waits on.
