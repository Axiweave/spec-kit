---
description: Audit all current specs and the shared code for cross-feature conflicts, regressions, and drift. Change nothing.
---

## User Input

```text
$ARGUMENTS
```

Consider the user input before you start. It can set a scope, ask to save the report, or name test commands that you may run (see [Input](#input)).

## Goal and boundaries

Answer one question for the current checkout: do the current specs agree with each other, and does the code at the subjects that two or more features share still meet the current requirements of all of them?

`__SPECKIT_COMMAND_ANALYZE__` and `__SPECKIT_COMMAND_CONVERGE__` check one feature. This command checks the places where features meet. It does not check a requirement that belongs to one feature only. It counts such requirements as "left to converge".

- **Change nothing.** Do not create, change, or delete any file in the code repository or in the specs root. Do not change git refs, the index, the stash, worktrees, or the saved feature selection. The only exception is a saved report that the input asks for (see [Save](#save)).
- **Run only these commands**:
  - `specify feature list --json`, and `specify project info --json` when the integration runs it first.
  - Your file read, directory list, and search tools.
  - Read-only git in the code repository: `rev-parse`, `branch --list`, `branch -a --list`, `branch --show-current`, `merge-base --is-ancestor`, `log`, `show`, `blame`, `ls-files`, and `diff` with no output option.
  - In shared storage, also `git -C <workspace_root> log`.
- **Do not run** `git fetch`, `pull`, `checkout`, `switch`, `stash`, `worktree`, or `gc`. Do not run any other Spec Kit command, and do not run hooks. Do not run builds or tests, unless the input names the test command. Then run only that command.
- **Subagents** get these same rules. Copy this list into each brief.
- **Evidence only.** A checked task, a "done" status, a passed checklist, and an earlier converge or audit result are never evidence that a requirement is met.
- **The chat is the output.** Do not write the report to a file unless the input asks for it.

## Input

Read these parts from the input, in any order:

| Part | Example | Effect |
|---|---|---|
| Feature references | `011`, `003 007`, `specs/009-spec-roadmap` | Focused run. Check the named features against every current feature that shares a subject with them. A number matches the feature folder whose name starts with `<number>-`. |
| Subject words | `roadmap table`, `feature.json` | Focused run on the shared subjects that match the words. |
| Save request | `save the report to audit.md` | Write the report after the run (see [Save](#save)). |
| Test request | `run pytest tests/features` | Allows only the named command. |
| Nothing | — | Check the whole repository. |

## Steps

### 1. Discover

1. Run `specify feature list --json`. If it exits with a non-zero status, show its stderr and stop.
2. Read `workspace_root`, each `features[].path` and `features[].stage`, and `roadmaps`.
3. Get the code repository root with `git rev-parse --show-toplevel`. If git is not available, continue without the git steps. Say so in the coverage table.
4. Stop with one message in these cases:
   - `workspace_root` does not exist: name the missing location.
   - No feature has a `spec.md`: "No feature has a spec. Run `__SPECKIT_COMMAND_SPECIFY__` first."
   - A feature reference matches no folder: name the reference and list the feature paths.
   - A feature reference matches two or more folders: name the matching paths and ask for the full path.
5. Skip a feature with stage `no-spec` or `unreadable`. Count it as "not checked", with the stage as the reason.
6. Always name a requirement with its feature folder and its ID, for example `specs/003-export FR-010`. Each feature has its own `FR-001`.

### 2. Decide the checkout state of each feature

Each feature gets one state for this checkout: `here` (its work is in this checkout), `elsewhere` (its work is only on a branch that this checkout does not contain), or `unknown`.

- **Local storage** (`workspace_root` is inside the code repository): every feature is `here`, because git keeps the specs of other branches out of this checkout.
- **Shared storage** (`workspace_root` is outside the code repository): use these sources in order. The first source that gives evidence decides the state.
  1. **Git.** Find the candidate branches: the `**Feature Branch**` value of the spec when it names a branch, and `git branch -a --list "*<folder-name>*"`.
     - `git merge-base --is-ancestor <branch> HEAD` succeeds for a candidate: `here`.
     - `git log -1 --format='%h %s' --fixed-strings --grep=<folder-name> HEAD` finds a commit: `here`. This covers squash merges.
     - Otherwise, when a candidate branch exists: `elsewhere`.
  2. **Code.** A file that `plan.md` or `tasks.md` says the feature creates is in `git ls-files`: `here`. A file that the feature only changes exists without the feature, so it is not evidence. Missing code is not evidence, because the feature can be in progress.
  3. **Branch field.** The `**Feature Branch**` value is the current branch (`git branch --show-current`) or a branch that is an ancestor of `HEAD`: `here`. Another named branch: `elsewhere`. "Not created", an empty value, or a value that is not a branch name gives no evidence.
  4. No evidence: `unknown`.
- Record one evidence line for each feature, for example `merged: 002-note-status is an ancestor of HEAD`, `commit a1b2c3d names 002-note-status`, or `creates notes/store.py, tracked`.
- Never decide the state from the feature number or from the storage location.

Use the states like this:

- Check code only against the requirements of `here` features.
- An `elsewhere` feature never gives drift, regression, or missing-code findings. Compare its spec with the `here` specs, and put each conflict in **Merge risks**.
- An `unknown` feature is never evidence for a finding. List it in the coverage table.

### 3. Load the rules

1. Read `/memory/constitution.md`. When it is missing, or it is still the unfilled template (it has `[PRINCIPLE_1_NAME]`-style placeholders), skip it and write "skipped: template" in the coverage table.
2. Collect each MUST and SHOULD rule of the constitution.
3. A **project guide** is a file that the constitution names as guidance for development. Read only those files.
4. When the constitution or a project guide names a persistence model for this project (flow-back, flow-forward, or living spec), record the model and its source. Do not guess a model.

### 4. Build the inventory

For each feature in scope, read `spec.md` and, when present, `plan.md`, `data-model.md`, the files in `contracts/`, and `tasks.md`. Keep one short line for each requirement (FR, SC, and acceptance scenario): its reference, its subject terms, and the paths that it names. Do not keep the full text.

Also collect:

- The entity and field names of each `data-model.md` and Key Entities section.
- The command, file, and interface names of each contract.
- The paths that each `plan.md` and `tasks.md` names. Build an index from each path to the features that name it.
- The `## Context` of each roadmap. Read it as background, not as requirements.

A **shared subject** is a command, file or path, entity, field, format, state, or invariant that two or more features name. Two features that use different words for one subject share it. A similar name alone does not make a subject shared, so read the requirement to confirm it.

A requirement that names no shared subject belongs to one feature only. Count it as "left to converge". Do not check it.

When only one feature has a spec, say in the answer sentence that cross-spec checks need two features. Then put all of its requirements in one group, and check its code against them and against the constitution in step 7b.

### 5. Decide authority

A requirement is **current** unless a source states in words that it is replaced or changed. The statement can be in any file of the feature: `spec.md` with its Clarifications, `plan.md`, `research.md`, `data-model.md`, or `contracts/`. Look for "replaces", "supersedes", "changes FR-004 of feature 001", "no longer", "(changed default)", Clarifications answers that remove an earlier rule, and scope lines that limit an earlier rule to some cases ("FR-001 to FR-012 apply to private mode only").

- The statement counts only when its source is a `here` feature, the constitution, or a project guide. A replacement that an `elsewhere` or `unknown` feature states does not apply to this checkout. The old requirement stays current.
- When a statement counts, the replaced requirement is **historical**. Do not check it, and list it under **Superseded requirements**.
- Apply the recorded persistence model when it decides which text is current. Name the model in the report.
- A higher feature number, a later date, or a later merge is never proof that a requirement replaces another.
- Two current requirements that conflict, with no source that states which one wins, give one `authority-unclear` finding. Such a pair never gives a `regression` or `drift` finding.

### 6. Make the groups

1. Join the shared subjects that have features in common into one group.
2. Split a group that has more than **8 features or 60 requirements**. This limit is a calibration knob: make it smaller when a group does not fit in your context.
3. Order the groups by the number of features, largest first.
4. For a focused run, keep only the groups that hold a named feature or a named subject. List the features that you skipped. When a named feature is `elsewhere`, compare only its spec, and say in the Scope line that you did not check its code.

### 7. Check each group

When you can start subagents, give each subagent one group. Give it the boundaries of this command, the requirement references and full text of its group, its code paths, the checkout states, the authority results, and the [Findings](#findings) rules. A subagent returns findings only. When you cannot start subagents, check the groups one after another.

For each group, reread the full text of only its requirements, then do these checks.

**a. Compare the specs.** For each shared subject, compare the current requirements of the features:

- `contract-mismatch`: two features expect a different shape, name, value, or order of the same interface, file, or field.
- `duplicate`: two features define the same thing with different values.
- `constitution`: a requirement or a plan clearly breaks a MUST rule of the constitution. When the constitution asks for a reason and any file of the feature gives one, there is no finding.
- `broken-reference`: a requirement names a feature, requirement, or file that does not exist.
- `dependency`: a `here` feature needs a requirement that is historical or not built, or a feature that is `elsewhere` or `unknown`.

Count the shared subjects that you compared, also when you find nothing.

Compare requirements (FR, SC, and acceptance scenarios), not the narrative sentences of a user story. Two rules that cover different cases do not conflict, for example a rule limited to handled failures and a later rule for forced stops.

**b. Check the shared code.** Read the code and tests at the paths of the group. Check them against the current requirements of every `here` feature in the group and against the MUST rules of the constitution:

- `regression`: code that serves one feature breaks a current requirement of another feature. When `git log` or `git blame` shows the commit that brought the change, name it.
- `drift`: the code does not meet a current requirement, and no newer requirement explains it. A requirement of a feature that is not finished (any stage except `done`) and is not yet built is not drift.
- `undocumented-change`: the code changes a behavior that a current requirement defines, and no spec describes the change.
- `coverage-gap`: a flow that crosses two features has no test that runs both features.
- `constitution`: the code clearly breaks a MUST rule of the constitution.

In shared storage, when `git -C <workspace_root> log` shows that a spec of a `here` feature changed after the code of this checkout, mark the difference `ambiguous`. Do not report it as drift.

A requirement that only a run can check is `unverifiable`, unless the input names a test command that checks it. Do not guess.

Code that no feature describes is not a finding, unless it changes a behavior that a current requirement defines.

A code finding names the behavior that a user or a caller sees break. A code list or a comment that is incomplete but changes no behavior is not a finding. A choice that a feature's `plan.md` or `research.md` records as deliberate is part of that feature's current text, so it is not drift. A case that no current requirement names (an error path, or a mode that the checkout has left) is not a finding.

### 8. Join the results

1. Merge the findings of the same issue that two groups found. Keep all the references.
2. Apply the evidence rules of [Findings](#findings). Downgrade a `verified` finding that does not meet them to `suspected`.
3. Move every finding that involves an `elsewhere` feature to **Merge risks**.
4. Drop every finding whose only evidence comes from an `unknown` feature.
5. Fill the coverage table. Every Checked and Total cell is a number, and "Requirements left to converge" is a number. When you cannot finish a count, check the missing part or write the number that you have and name the missing part with its reason. Missing coverage makes the answer `not determined` unless a `verified` finding already makes it `no`.
6. Decide the answer (see [Answer](#answer)).

## Findings

Each finding has these fields:

| Field | Values |
|---|---|
| ID | `A1`, `A2`, … for findings, and `M1`, `M2`, … for merge risks |
| Kind | `contract-mismatch`, `duplicate`, `constitution`, `broken-reference`, `dependency`, `regression`, `drift`, `undocumented-change`, `coverage-gap`, `authority-unclear` |
| Status | `verified`, `suspected`, `ambiguous`, `unverifiable` |
| Severity | `CRITICAL`, `HIGH`, `MEDIUM`, `LOW` |
| Confidence | `high`, `medium`, `low` (your own estimate, which never changes the status) |
| Requirements | Each requirement reference with the checkout state of its feature |
| Location | `path:line` for each code location |
| Expected, Observed | One sentence each |
| Evidence | Verbatim quotes of each requirement, and the code lines or git facts |
| Fix | The spec or code to change, and the command to use, for example "edit `specs/003-export/spec.md` FR-010" or "`__SPECKIT_COMMAND_CONVERGE__` for `specs/003-export`" |

**Status rules:**

- `verified`: you quote each requirement with its reference. A code finding also has a `path:line` location and the code line.
- `suspected`: the evidence is not complete.
- `ambiguous`: the requirements allow two readings, or the spec may describe a version that this checkout does not follow.
- `unverifiable`: the check needs a run or data that you do not have.
- A name match alone is never `verified`.
- An `authority-unclear` finding with both requirements quoted is `verified`. The conflict is proven, and only the winner is unclear.

**Severity rules:**

- `CRITICAL`: the finding breaks a MUST rule of the constitution.
- `HIGH`: a regression or a contract mismatch on a P1 user story or a core requirement.
- `MEDIUM`: an `authority-unclear` finding, or a regression or drift on a secondary requirement.
- `LOW`: a coverage gap, a duplicate with no effect yet, or a broken reference in background text.

## Answer

| Answer | Condition |
|---|---|
| `yes` | You checked every shared subject in scope, no `unknown` feature shares a subject with a `here` feature, and no `verified` or `suspected` finding of MEDIUM or higher exists outside Merge risks. |
| `no` | At least one `verified` finding of MEDIUM or higher exists outside Merge risks. |
| `not determined` | All other cases. Give the reason: the coverage that is missing, the `unknown` features, or the `suspected` findings. |

Merge risks never change the answer.

## Report

Write the report in this order. Leave out a section with no content, except the answer and **Coverage**.

````markdown
## Audit answer: <yes | no | not determined>

<One sentence with the reason.> The answer covers shared subjects and the constitution. <N> requirements that belong to one feature only are left to `__SPECKIT_COMMAND_CONVERGE__`.

Scope: <whole repository | focused on …>. Storage: <local | shared (<workspace_root>)>. Persistence model: <name and source | not stated>.

### Findings

| ID | Kind | Status | Severity | Confidence | Requirements | Location | Summary |
|---|---|---|---|---|---|---|---|

#### A1 — <summary>

- **Expected**: …
- **Observed**: …
- **Evidence**:
  - `specs/<folder> <ID>`: "<verbatim quote>"
  - `<path:line>`: `<code line>`
- **Fix**: …

### Merge risks (features on other branches)

| ID | Kind | Requirements | Summary | Fix |
|---|---|---|---|---|

### Superseded requirements

| Replaced | Replacement | Source |
|---|---|---|

### Checkout states

| Feature | State | Evidence |
|---|---|---|

### Coverage

| Part | Checked | Total | Not checked (reason) |
|---|---|---|---|
| Features (here / elsewhere / unknown) | | | |
| Requirements on shared subjects | | | |
| Requirements left to converge | — | | — |
| Shared subjects | | | |
| Code areas | | | |
| Constitution rules | | | |
````

- Sort the findings by severity (CRITICAL first), then by status (`verified`, `suspected`, `ambiguous`, `unverifiable`).
- Give a detail block to each `verified` finding and to each finding of MEDIUM or higher. Any other LOW finding gets only its table row.
- In local storage, write **Checkout states** as one line: "All N features are in this checkout (local storage)."
- A clean run still shows **Coverage** with the counts that you checked.
- Take each count in the answer sentence from the tables. Count again before you finish.
- Put any comment of your own after the report, under a separate heading.

## Save

Do this only when the input asks to save the report.

1. Take the path from the input. When the input asks for a save and names no path, ask for a path and do not write.
2. Resolve the path from the code repository root. Refuse the path when it is outside both the code repository and `workspace_root`, when it contains `..`, or when a part of it is a symlink.
3. Refuse to overwrite an existing file, unless the input says "overwrite".
4. Write the same Markdown that you show in the chat, and nothing else. Then name the written path.
