---
name: specify-init
description: Initialize a Spec Kit project or repeat its setup. Interview the user about setup choices, run specify init, and verify the installed project.
---

# Specify Init

Interview first. Use the answers to run `specify init`, not to produce a command for the user to execute.
Keep product specifications, implementation, commits, and publishing outside this skill.

## 1. Inspect

1. Resolve the project directory from the request and current directory.
2. Find the installed Specify CLI or this worktree's `.venv/bin/specify`.
3. Read its `init --help`, `project info --help`, and `check` output.
4. Inspect existing project files, saved setup choices, and repository status without changing them.
5. For an initialized project, run `specify project info --json` from its code repository.

Use the installed CLI as the source of truth for supported options and integrations.
Keep the selected executable's absolute path for commands that change their working directory.
For a missing CLI, read [the installation command](../../README.md#get-started) and get installation approval.
After approval, install the CLI, resolve its executable, and read its help before setup.
If a repository checkout is available, follow its environment setup rules instead.

**Complete when:** the target, CLI, existing setup, available tools, and overwrite risks are known.

## 2. Interview

Ask only unresolved choices. Treat explicit request details and saved project choices as answers.
Batch two to five related questions when the question tool supports it.
Give short options, a recommendation, and the tradeoff for each choice.
Use these topics in order:

| Topic | Choices and recommendation |
| --- | --- |
| Target | Current directory or a named new directory. Confirm the exact path. |
| Integration | Offer supported integrations from the CLI. Recommend the user's active coding agent when supported. |
| Storage | External workspace keeps work products outside code. Local storage keeps them together. Recommend external for fresh setup. |
| Workspace | For external storage, use the CLI-selected unique directory or an exact user-selected path. |
| Workspace history | For fresh external setup, create the initial Git commit or select `--no-workspace-git`. Explain the commit before approval. |
| Scripts | Offer the CLI's supported script types. Recommend the platform shell, or Python for cross-platform use. |
| Numbering | Sequential numbers support an ordered feature list. Timestamps reduce shared-number conflicts. Preserve an existing choice. |
| Feature selection | If supported, recommend `context` for independent sessions and worktrees. `automatic` saves the newest selected feature for later commands. |
| Extras | Install no preset or extension unless requested. Resolve requested sources before setup. |

For OMP, ask about project-local commands versus shared commands only when that choice matters.
For integration-specific delivery options, inspect that integration's documented options before asking.
For a generic integration, get its required commands directory.
For repeated setup, retain existing storage and identity.
If the user wants different storage for an existing project, use the documented project migration workflow instead.

**Complete when:** every applicable choice has an answer or an accepted recommendation.

## 3. Approve the setup

1. Show the code directory, workspace choice, integration, script type, numbering, feature selection, and requested extras.
2. Show the exact command with each argument safely quoted.
3. Explain any initial workspace commit, global command writes, package installation, or overwrite risk.
4. Get approval for unresolved side effects and the exact target paths.

Use prior explicit authorization when it covers the exact action, target, and values.
For a nonempty code directory, get explicit merge approval before adding `--force`.
`--force` can overwrite templates and shared infrastructure. It is not an automatic retry option.
Keep occupied workspace destinations unchanged. Select another destination when setup refuses one.
Get source-specific approval before using `--trust-extension-urls`.
Use `--ignore-agent-tools` only after the user explicitly accepts the missing tool.
Keep Git identity, signing, hooks, credentials, and existing staged changes unchanged.

**Complete when:** the command and its side effects have authorization.

## 4. Run setup

1. Run the selected CLI with `--non-interactive` and the agreed options.
2. Use `--here` from the target directory, or pass the agreed new directory as the positional argument.
3. Pass the integration, storage, script type, and numbering explicitly.
4. For an exact external destination, also pass `--workspace` with that path.
5. For the external history opt-out, add `--no-workspace-git`.
6. If supported, pass `--feature-selection` with the approved mode.
7. Add only the approved integration options, preset, extensions, and risk-related flags.
8. Capture the exit status, setup notices, warnings, and recovery paths.

Pass arguments separately when the execution tool supports argument arrays.
If setup fails, inspect its reported cause before another action.
After a post-commit error, preserve the workspace and inspect its reported recovery paths.
Do not replace a failed setup with silent local storage or an automatic history opt-out.

**Complete when:** setup succeeds, or a specific failure and safe next action are established.

## 5. Verify

1. Run `specify project info --json` from the code directory with the selected executable.
2. Compare the reported roots and saved choices with the approved setup.
3. Check the installed integration entry points and shared scripts at their reported locations.
4. Run an installed read-only helper with its documented arguments when one is available.
5. For default fresh external setup, verify the workspace repository has exactly one commit.
6. For the history opt-out, check that setup created no workspace `.git` without invoking workspace Git commands.
7. Compare the code repository's history and staged changes with the pre-setup state.

Local setup and repeated setup do not create workspace history.
Distinguish installed commands from commands exercised through a real coding agent.
Report optional package failures and missing tools instead of claiming full setup success.

**Complete when:** the installed paths and choices match, and each applicable safety check passes.

## Report

Show the code directory, workspace directory, selected integration, and verification result.
Include the initial commit ID or the history opt-out when applicable.
Name any unresolved warning or missing prerequisite.
Give the integration's installed constitution command as the next step.
Leave that command to a separate user request.
