---
description: "Create a feature branch with sequential or timestamp numbering"
---

# Create Feature Branch

Create and switch to a new git feature branch for the given specification. This command handles **branch creation only** — the spec directory and files are created by the core `__SPECKIT_COMMAND_SPECIFY__` workflow.

## User Input

```text
$ARGUMENTS
```

You **MUST** consider the user input before proceeding (if not empty).

## Environment Variable Override

If the user explicitly provided `GIT_BRANCH_NAME` (e.g., via environment variable, argument, or in their request), pass it through to the script by setting the `GIT_BRANCH_NAME` environment variable before invoking the script. When `GIT_BRANCH_NAME` is set:
- The script uses the exact value as the branch name, bypassing all prefix/suffix generation
- `--short-name`, `--number`, and `--timestamp` flags are ignored
- `FEATURE_NUM` is extracted when the final path segment starts with a numeric or timestamp feature marker (for example `042-name`, `feat/042-name`, or `jdoe/app/042-name`), otherwise set to the full branch name

## Prerequisites

- Verify Git is available by running `git rev-parse --is-inside-work-tree 2>/dev/null`
- If Git is not available, warn the user and skip branch creation

## Branch Numbering Mode

Let the script resolve saved numbering from the workspace configuration:

1. Use a nonempty `branch_numbering` from `.specify/extensions/git/git-config.yml` as an explicit branch override.
2. Otherwise, use `feature_numbering` from `.specify/init-options.json`.
3. If the project has no saved mode, use `sequential`.

Generated extension configuration leaves `branch_numbering` empty so it inherits the project mode.
Do not read personal setup defaults during branch creation.
Only pass a number or timestamp flag when the user explicitly requests that per-feature choice.
An explicit timestamp beats an explicit number. `GIT_BRANCH_NAME` overrides both.
Branch creation does not select or change the active feature directory.

## Branch Name Template

Check `.specify/extensions/git/git-config.yml` for an optional `branch_template` value. If it is empty or missing, use the default branch shape `{number}-{slug}`. If it is set, `{slug}` must not appear before `{number}`, its final path segment must start with `{number}-`, and the script expands these tokens:

- `{author}`: sanitized Git config author (`user.name`, falling back to the email local part)
- `{app}`: sanitized Spec Kit init directory name
- `{number}`: sequential number or timestamp
- `{slug}`: generated short branch slug

For monorepos, a template such as `{author}/{app}/{number}-{slug}` creates names like `jdoe/web/008-guided-tour` while preserving per-project feature numbering.

The script also accepts `branch_prefix` as a shorthand for simple namespaces; it expands to `<branch_prefix>/{number}-{slug}`.

## Execution

Generate a concise short name (2-4 words) for the branch:
- Analyze the feature description and extract the most meaningful keywords
- Use action-noun format when possible (e.g., "add-user-auth", "fix-payment-bug")
- Preserve technical terms and acronyms (OAuth2, API, JWT, etc.)

Use the project's saved script type.
Run its workspace script from the code repository:

- **Bash**: `.specify/extensions/git/scripts/bash/create-new-feature-branch.sh --json --short-name "<short-name>" "<feature description>"`
- **PowerShell**: `.specify/extensions/git/scripts/powershell/create-new-feature-branch.ps1 -Json -ShortName "<short-name>" "<feature description>"`
- **Python**: `python3 .specify/extensions/git/scripts/python/create_new_feature_branch.py --json --short-name "<short-name>" "<feature description>"`

**IMPORTANT**:
- Unless the user requests an override, let the script choose the next number from workspace features and repository branches.
- For an explicit override, pass `--number` or `--timestamp`. PowerShell uses `-Number` or `-Timestamp`.
- Always include `--json` for Bash or Python, or `-Json` for PowerShell.
- You must only ever run this script once per feature
- The JSON output will contain `BRANCH_NAME` and `FEATURE_NUM`
- Do not manually expand `branch_template`; the script reads the git extension config and applies it consistently

## Graceful Degradation

If Git is not installed or the current directory is not a Git repository:
- Branch creation is skipped with a warning: `[specify] Warning: Git repository not detected; skipped branch creation`
- The script still outputs `BRANCH_NAME` and `FEATURE_NUM` so the caller can reference them

## Output

The script outputs JSON with:
- `BRANCH_NAME`: The branch name (e.g., `003-user-auth`, `20260319-143022-user-auth`, or `jdoe/web/003-user-auth`)
- `FEATURE_NUM`: The numeric or timestamp prefix used
