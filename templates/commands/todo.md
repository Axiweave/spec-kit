---
description: List unfinished features with their stage, blockers, and next step, and recommend one feature to continue.
---

## User Input

```text
$ARGUMENTS
```

Consider the user input before you start. It can ask for one part of the report, but the facts still come only from the command output.

## Goal and boundaries

Show the state of every feature in the current checkout's specs root, so the user knows what to do next.

- Take every stage, count, blocker, and recommendation from the JSON below. Do not add facts from this conversation, from memory, or from other worktrees to the report.
- Do not change any file, the saved feature selection, or Git state.
- Do not run hooks, tests, or other Spec Kit commands.

## Steps

1. Run `specify feature list --json` from the code repository.
2. If it exits with a non-zero status, show its stderr and stop.
3. Write the report from the JSON as described below.

## Command names

The JSON uses canonical command names. Write each one in this integration's form:

| JSON `next_step` | Write |
|---|---|
| `speckit.specify` | `__SPECKIT_COMMAND_SPECIFY__` |
| `speckit.clarify` | `__SPECKIT_COMMAND_CLARIFY__` |
| `speckit.plan` | `__SPECKIT_COMMAND_PLAN__` |
| `speckit.tasks` | `__SPECKIT_COMMAND_TASKS__` |
| `speckit.implement` | `__SPECKIT_COMMAND_IMPLEMENT__` |

## Stage names

Write each `stage` with its display name: `unreadable` Unreadable, `no-spec` No spec, `needs-clarification` Needs clarification, `done` Done, `in-progress` In progress, `tasks-ready` Tasks ready, `planned` Planned, `spec-ready` Spec ready.

## Report

1. **Recommendation** (first line). Use `recommendation`:
   - `finish-before-start`: name the feature path and the next command. Say that it is the unblocked feature that is furthest along.
   - `resolve-blocker`: name the feature path and the next command. Say that every unfinished feature is blocked and that this blocker comes first. When `next_step` is null, name the first finding of that feature instead of a command.
   - `no-unfinished`: say that all features are done, or that no features exist when `features` is empty. Name the specify command for new work.
2. **Table** of the features whose stage is not `done`, in JSON order:

   | Feature | Stage | Progress | Blockers | Next step |
   |---|---|---|---|---|

   - Feature: the last part of `path`.
   - Stage: the display name.
   - Progress: `checked/total` when `tasks` is not null, followed by the `open_ids` when the list is not empty. Otherwise "—".
   - Blockers: the number of `markers` with the first 3 as `file:line`, the open count of `requirements` when it is not zero, "empty task list (`tasks.md`)" when `tasks.total` is 0, and each finding as `file: reason`. Write "—" when there is no blocker.
   - Next step: the command followed by the feature `path`, for example `__SPECKIT_COMMAND_PLAN__ specs/008-example`. When `next_step` is null, write the action for the finding with no command:
     - `no-spec`: add `spec.md` to the folder or delete the folder.
     - `symlink`: replace the symlink with a regular file.
     - `unreadable`: fix the file so that it can be read as UTF-8. When the finding `file` is `.` or `checklists`, fix the folder permissions so that the folder can be listed.
3. **Reviewer items**: for each `custom_checklists` entry with open items, write one line with the feature, the file, the open count, and "owner: reviewer". These items have no command.
4. **Done**: one line, "Done (N): name, name, …", with the features whose stage is `done`.
5. **Duplicate prefixes**: when `duplicate_prefixes` is not empty, list each group and say that features with the same prefix are hard to select.
6. **Notes** (optional): put any comment of your own after the report, under a separate heading. Do not mix it into the table.

An open task always keeps a feature unfinished. To drop a task, the developer deletes it from `tasks.md`.
