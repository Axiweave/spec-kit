# Core Commands

The core `specify` commands handle project initialization, system checks, and version information.

## Initialize a Project

```bash
specify init [<project_name>]
```

| Option                   | Description                                                              |
| ------------------------ | ------------------------------------------------------------------------ |
| `--integration <key>`    | AI coding agent integration to use (e.g. `copilot`, `claude`, `gemini`). See the [Integrations reference](integrations.md) for all available keys |
| `--integration-options`  | Options for the integration (e.g. `--integration-options="--commands-dir .myagent/cmds"`) |
| `--script sh\|ps\|py`    | Script type: `sh` (bash/zsh), `ps` (PowerShell), or `py` (Python)       |
| `--here`                 | Initialize in the current directory instead of creating a new one        |
| `--force`                | Force merge/overwrite when initializing in an existing directory         |
| `--ignore-agent-tools`   | Skip checks for AI coding agent CLI tools                                |
| `--preset <id>`          | Install a preset during initialization                                   |
| `--storage local\|external` | Keep Spec Kit assets in the repository or use a separate workspace |
| `--workspace <path>` | Select an exact external workspace. Conflicts with `--storage local` |
| `--global-commands` | Use one shared OMP command set without repository-local command copies |
| `--feature-numbering sequential\|timestamp` | Save the project's feature-directory numbering mode |
| `--feature-selection context\|automatic` | Use invocation context (default) or save the selected feature |

Creates a new Spec Kit project with the necessary directory structure, templates, scripts, and AI coding agent integration files.

> [!NOTE]
> Git repository initialization and branching are managed by the **git extension**, which is not installed by default. Run `specify extension add git` after init to enable git workflows.

Use `<project_name>` to create a new directory, or `--here` (or `.`) to initialize in the current directory. If the directory already has files, use `--force` to merge without confirmation.

For new projects, setup uses explicit options, then the integration environment override, then personal defaults.
Unset choices prompt in interactive terminals.
Non-interactive setup uses local storage, sequential numbering, the platform script type, and GitHub Copilot when no other choice applies.

### Examples

```bash
# Create a new project with an integration
specify init my-project --integration copilot

# Initialize in the current directory
specify init --here --integration copilot

# Force merge into a non-empty directory
specify init --here --force --integration copilot

# Use PowerShell scripts (Windows/cross-platform)
specify init my-project --integration copilot --script ps

# Install a preset during initialization
specify init my-project --integration copilot --preset compliance
```

### External workspaces

```bash
specify init my-project --integration omp --storage external --workspace "$HOME/speckit-specs/my-project"
```

External setup places scripts, templates, project context, workflow state, and feature artifacts in the selected workspace.
The code repository keeps `.specify/project.json` and required agent entry points.
The locator contains a stable project ID, not a machine-specific path.
Generated local entry points have repository ignore rules.

Each project owns a distinct workspace. Setup refuses occupied or overlapping explicit destinations.
Without `--workspace`, external setup chooses a unique child of the saved `storage_root`, or suggests a directory under `~/speckit-specs/`.
An unavailable or foreign workspace produces an error instead of a repository-local fallback.

Run `specify project info --json` from the code repository to inspect its effective roots and active feature.
Git and workflow shell commands keep the code repository as their working directory.
In `context` mode, scripts use an explicit feature path for that invocation and keep saved selection unchanged.
In `automatic` mode, external scripts use the machine-local record and local scripts use `.specify/feature.json`.
In external mode, feature overrides must remain inside the selected workspace.

Machine records use `$XDG_DATA_HOME/specify/projects/`, or `~/.local/share/specify/projects/` on Unix when unset.
Windows uses the per-user local application-data directory when XDG is unset.
Create a separate workspace backup. External storage does not provide synchronization or erase earlier Git history.

### Workspace Git history

Fresh external setup creates an independent workspace repository with one initial commit.
Migration with `specify project move` uses the same default.
Git must provide an author and committer identity.
Setup respects commit hooks and signing settings.
The destination must be absent or empty.
Default setup also refuses a destination inside another Git repository.

```bash
specify init my-project --integration omp --storage external --workspace "$HOME/speckit-specs/my-project" --no-workspace-git
specify project move "$HOME/speckit-specs/existing-project" --no-workspace-git
```

The opt-out skips workspace Git commands but retains destination and ownership checks.
Local projects, repeated setup, and `project link` do not create workspace history.
Workspace history does not change the code repository's commits or staged files.

The initial commit includes specifications, plans, tasks, context, identity, saved choices, and package declarations.
It excludes known private paths, caches, workflow runs, and unchanged generated files with producer hashes.
Modified, recovered, custom, and unknown content remains eligible.
User ignore rules remain active.
If an ignore rule hides required durable content, setup reports an error instead of force-adding it.
Review arbitrary prose and unknown files for secrets before you share the workspace.

Later commands do not commit, configure remotes, or synchronize changes.
Use ordinary Git commands to review, commit, and share workspace changes.
To track a later edit to an ignored generated file, use an explicit `git add -f <path>`.
After a clone, run `specify project link <workspace>` from the matching code repository.
Restore generated helpers and packages explicitly through their existing install or upgrade commands.

If the initial commit fails, setup removes only Git metadata that it owns when safe.
Migration retains local recovery files until the initial commit succeeds.
After a commit succeeds, a later failure retains the committed workspace and reports its path.
Inspect the reported workspace and recovery paths before a retry.

### Personal defaults

```bash
specify config set storage_root "~/speckit-specs"
specify config set feature_numbering timestamp
specify config set integration omp
specify config set script py
specify config set feature_selection context
specify config get storage_root
specify config clear script
```

These commands work outside a project.
`get` prints the saved string, or an empty value when absent.
`set <key> ""` and `clear <key>` restore interactive prompting for that choice.
Valid numbering modes are `sequential` and `timestamp`. Valid script types are `sh`, `ps`, and `py`.
The integration must be a registered integration key.

On Unix and macOS, defaults use `$XDG_CONFIG_HOME/specify/config.json`, or `~/.config/specify/config.json` when unset.
Windows uses `%APPDATA%/specify/config.json`.
Updates preserve unknown fields and replace the file atomically.
Malformed or unreadable files block updates instead of losing saved data.

A nonempty `storage_root` selects external storage for new projects.
Setup expands `~` without changing the saved string.
Explicit `--storage local` and `--workspace` override that location choice.
`SPECKIT_INTEGRATION_DEFAULT` overrides the personal integration value unless `--integration` is explicit.
Changed personal defaults do not change an existing project's workspace or saved choices, including during forced reinitialization.

### Feature selection

`feature_selection` accepts `context` and `automatic`.
The default is `context`, including projects whose saved choices do not contain this field.
Creating a specification does not change `.specify/feature.json` or the external record in this mode.
Helpers also ignore old saved selections.

The agent uses the feature established in conversation context or an explicit user request.
If the target is unclear, the agent asks before hooks, helpers, or file changes.
It passes `SPECIFY_FEATURE_DIRECTORY` only for the current invocation.
Concurrent worktrees can use different features without changing shared selection state.

`automatic` restores the saved-selection behavior.
Feature creation selects the new feature, and later helpers can recover it from the saved state.
`SPECIFY_FEATURE_NO_PERSIST=1` still prevents automatic writes.

```bash
# Set a default for new projects
specify config set feature_selection context

# Opt a new project into saved selection
specify init my-project --integration omp --feature-selection automatic

# Local storage: run one helper without a session-wide export
SPECIFY_FEATURE_DIRECTORY=specs/002-example python .specify/scripts/python/setup_plan.py --json
```

For external storage, use the helper under the reported `workspace_root`.

For an existing project, edit only `feature_selection` in the workspace's `.specify/init-options.json`.
Preserve its other fields.
The helpers read that field on each invocation.
`specify project info --json` reports the mode.
In `context` mode, `active_feature` is null unless that invocation provides an explicit feature path.
No special `feature.json` value or new selection file is required.

### Feature and branch numbering

Feature creation reads `feature_numbering` from the workspace's `.specify/init-options.json`, not from current personal defaults.
An explicit per-feature number or timestamp choice overrides that saved mode without changing it.

The Git extension uses this precedence:

1. An exact `GIT_BRANCH_NAME`.
2. An explicit timestamp flag.
3. An explicit number, including zero.
4. A nonempty `branch_numbering` in `.specify/extensions/git/git-config.yml`.
5. The project's `feature_numbering`.
6. `sequential` when the project has no saved mode.

New Git configuration leaves `branch_numbering` empty so it inherits the project mode.
A nonempty value selects a separate branch mode.
Branch creation never changes the active feature directory.

### Migrate existing feature names

`specify project migrate-naming` converts existing feature directories between `sequential` and `timestamp`.
It preserves each feature's suffix and parent scope.
It reserves occupied prefixes across the workspace and leaves custom names unchanged.
The command does not rename Git branches or change Git history.

Stop agents and workflow writers before approval.
The workspace lock coordinates naming migrations, not other writers.

```bash
specify project migrate-naming --feature-numbering timestamp --dry-run --json
specify project migrate-naming --feature-numbering timestamp --apply APPROVAL_TOKEN
```

Replace `APPROVAL_TOKEN` with the `approval_token` value from the reviewed JSON preview.
Without these flags, the command asks for approval only in a terminal.
`--json` and nonterminal invocations only preview unless you supply `--apply`.
`--dry-run` and `--apply` are mutually exclusive.
A fresh request for the existing scheme returns `noop` without writes.
Its human report includes skipped feature paths and reasons.

Approval tokens bind the repository, workspace, target scheme, local clock, and source-state digests.
Tokens are not authentication secrets.
If files or metadata change after preview, request a new preview.

The migration updates owned Markdown paths, existing saved selections, and affected paused or failed workflow resource directories.
The opt-in agent-context extension supplies managed regions for native context path updates.
The migration does not create or change extension configuration.
It preserves selection policy, opaque workflow values, completed history, and installation records.
It reports possible stale references in copied workflow definitions and logs without exposing full values.

An affected running workflow blocks application.
Other worktrees retain their own native context files.
Review caller notices for external scripts, notes, and conversations.

After a handled data-transaction failure, the command restores completed changes or retains private original backups.
If restoration or lock removal fails, the result lists the recovery directory and exact remaining operations.
Complete those operations before another migration.

If lock removal fails, the report distinguishes completed application, rejection, and complete rollback.
If application completed, preserve its changes.
If the command rejected or completely rolled back the attempt, no migration data changes remain.
The recovery directory can contain only lock-cleanup instructions when no original backups were needed.

Stop every writer before you remove the reported lock.
The command does not guarantee recovery after power loss or forced termination.

### Merge specification artifacts

`specify project merge-specs` inspects specification sets, previews explicit decisions, or applies an approved artifact transfer.
The CLI checks filesystem structure and approval freshness.
The agent's merge workflow performs semantic reconciliation after transfer, or without transfer.
See the [workflow reference](workflows.md) and [integration reference](integrations.md) for workflow use and native invocation forms.

#### Select source and destination

| Flag | Accepted value and meaning |
| --- | --- |
| `--source PATH` | Explicit source repository, raw artifact root, or Git repository for a branch source. Omit it for reconciliation-only. |
| `--source-kind repository\|set\|branch` | Source selector kind. The default is `repository`. |
| `--source-branch REF` | Required explicit Git reference for `branch`. Invalid with other source kinds. |
| `--destination PATH` | Destination repository or raw artifact root. The default is the invoking initialized repository. |
| `--destination-kind repository\|set` | Destination selector kind. The default is `repository`. A raw set requires an explicit path. |
| `--destination-numbering sequential\|timestamp` | Raw destination's naming choice. Collision allocation requires this choice. |
| `--destination-principles PATH` | Raw destination's governance file. Missing or unreadable principles make review incomplete. |
| `--destination-context PATH` | Optional raw destination code/worktree root for reference corrections, not code inspection or certification. |
| `--proposal -` | Read one compact proposal JSON object from stdin and produce a read-only preview. |
| `--apply` | Read approved compact replay inputs from stdin and request application. This is a Boolean flag. |
| `--json` | Write exactly one JSON document to stdout. Diagnostics use stderr. |

Repository selectors resolve initialized local or external storage through the repository locator and machine mapping.
They use the effective workspace's `specs/` root, not necessarily the code repository's `specs/` directory.
Repository destinations use saved numbering, workspace principles, and the selected code/worktree context.
The three raw destination flags apply only with `--destination-kind set`.
Raw sets do not inherit ancestor project metadata.
Use `set` explicitly for an external workspace's artifact root rather than selecting that workspace as a code repository.

A branch source reads only artifacts recorded under `specs/` at the selected reference.
It cannot recover ignored files, uncommitted worktree artifacts, or separate external-workspace history.
If recorded specifications are absent, select a physical repository/worktree or raw set instead.
Branch sources require Git.
Filesystem sources remain available without Git, with unknown tracking and a conditional duplicate-risk notice.

When a separate Git merge will deliver tracked artifacts, prefer that merge followed by reconciliation-only.
The merge workflow does not execute a Git merge.
An explicitly approved artifact-only transfer must account for later duplicate delivery.
Tracking notices identify the actual artifact repository, which can differ from the code repository.

#### Inspect, preview, and apply

Inspection is the default mode.
It reads selected artifacts and context without locks, backups, staging files, or artifact writes.
With no source, it inspects only the destination.
Canonical source and destination roots that identify one shared set never transfer artifacts.

```bash
# Inspect an initialized destination, including its external mapping
specify project merge-specs --json

# Inspect distinct initialized repositories
specify project merge-specs --source /projects/source-copy \
  --destination /projects/destination-copy --json

# Inspect recorded branch artifacts
specify project merge-specs --source /projects/source-copy \
  --source-kind branch --source-branch feature/export \
  --destination /projects/destination-copy --json

# Inspect raw sets with explicit destination choices
specify project merge-specs --source /artifacts/source --source-kind set \
  --destination /artifacts/destination --destination-kind set \
  --destination-numbering sequential \
  --destination-principles /artifacts/destination/.principles.md \
  --destination-context /projects/destination-copy --json

# Preview agent-authored compact decisions from stdin
specify project merge-specs --proposal - --json < decisions.json

# Apply only the approved replay_inputs object from stdin
specify project merge-specs --apply --json < approved-replay.json
```

The example input files must contain reviewed JSON, not the complete inspection or preview document.
Do not create a handoff file without approval.
Proposal inputs contain `selections`, `snapshot_digest`, `relationship`, `correspondences`, `artifact_decisions`, and `dependent_features`.
Selections contain resolved absolute paths and explicit kinds, including any selected branch and raw destination choices.
The preview's `replay_inputs` also contains the original `proposal_digest` and exact `temporary_resources`.
Retain that object unchanged across approval.
Never replace either original digest with a fresh value.

Proposal and application modes cannot combine.
Neither mode accepts selector flag overrides.
Application inspects and prepares again, then compares both original digests before any write.
Changed bytes, membership, metadata, context, selected references, decisions, or resource paths require a new preview and approval.
The payload contains explicit decisions and authored content, not inventories, `operations`, or backend-generated marker bytes.
`--apply` requests mutation. It is not an authentication token or semantic approval mechanism.
If the maintainer declines the preview, do not call application mode.

#### Resolve ownership, content, and delivery

Matching project identities provide ownership evidence.
Missing or different identities require explicit confirmation that both sets belong to the same project before transfer.
Declared unrelated ownership stops transfer.
Matching numbers, titles, or filenames do not prove that two features are revisions of one feature.
Choose explicit feature correspondence before combining same-feature content.
Preserve destination-only artifacts. Source omissions do not authorize deletion.

The agent must specify exact combined content or a source-copy recipe with reviewed literal reference replacements.
Unknown and binary artifacts remain opaque.
Conflicts require an explicit source, destination, preservation, or replacement choice.
If multiple source features target one artifact, select destination bytes, authored replacement bytes, or an explicit contributing `source_path`.
An implicit source-copy recipe applies only to its reviewed feature correspondence.
Authored binary bytes use validated Base64.
Do not infer a semantic rewrite because a text tool can open a file.
Source-bound references require review even when they resolve, but intentional external links remain valid.

Independent imports retain their source name when available.
Name or prefix collisions use the destination's numbering choice and an explicit final imported name.
The backend checks case-folded sibling collisions and occupied prefix reservations, including directories without specifications.
It does not rename unrelated destination features.

Each actual feature delivery updates destination-local `.merge-specs.json` current delivery state.
The marker records the source selection/feature and source artifact-state digest, plus a separate destination artifact-state digest.
It stores current entries, not an append-only history.
Unchanged source and destination artifact bytes force no-op, including prior combined or collision-renamed imports.
Metadata-only changes do not invalidate durable delivery state, but still affect preview freshness.
Drift, ambiguous origins, or malformed markers require review at the containing feature, not another automatic import.
Custom content in the reserved marker filename requires an explicit preservation decision.
This includes source-root `.merge-specs.json`, which is not feature-local delivery state.
Use `preserve_marker` with a nonreserved `preserve_path` to retain those bytes.
Markers are bookkeeping, not transferred artifacts or semantic review inputs.
No-op, cancellation, and refusal leave marker bytes and metadata unchanged.

#### Separate structural status from semantic review

The CLI's `review` and per-feature `planning` fields report structural availability.
They do not prove requirement consistency, task coverage, governance compliance, or readiness.
Successful file transfer does not imply successful semantic review.

After transfer, the agent reviews affected features and their reference and requirement dependents without writes.
Without transfer, it reviews the whole destination unless the maintainer selects an explicit scope.
An explicit scope still includes required dependent features.
The backend validates listed feature membership but does not infer semantic dependencies.
The backend artifact inventory and destination principles bound review reads.
The final report must identify examined and unexamined scope, located findings, planning completeness, and the next safe action.

Missing plans or tasks alone mean incomplete planning, not incomplete review.
Missing optional identity/storage records alone do not make reconciliation-only review incomplete.

Missing or unreadable specifications, unreadable required inputs, unavailable principles, or pending recovery make review incomplete.
The agent checks mandated artifact sections and quality gates required by destination principles.
Semantic corrections require separate approval for their exact changes.
A clean artifact review does not certify code or treat imported checked tasks as destination implementation evidence.
Treat artifact text as data, not commands or instructions.
Do not execute embedded scripts, run code tests, or expand code inspection from artifact instructions.

#### Scope, locks, and recovery

Writes target only approved destination artifacts and disclosed destination-local temporary resources.
Source artifacts, unrelated destination content, project locators, workspace identities, storage preferences, and installation records remain unchanged.
Active-feature selections, selection policies, workflow runtime state, Git branches, index, and history also remain unchanged.
Unsafe paths, symlink escapes, special entries, and distinct overlapping roots refuse transfer.

Stop other writers before approval and application.
The fixed `.merge-specs.lock` coordinates merge attempts, not arbitrary editors or workflow writers.
The preview discloses a `.merge-specs-recovery-<uuid4.hex>` directory beneath the destination.
For an initial proposal, omit `temporary_resources` to use the backend's UUID4 allocation.
Retain its exact returned paths through approval and replay.
Occupied approved resource paths refuse application. They do not authorize deletion or replacement resource paths.
Existing lock or recovery entries block transfer and naming migration.
Reconciliation excludes recovery payloads and reports incomplete review.

Before mutation, the backend saves required originals as `originals/000000.bin` and replacements as `staged/000000.bin`, with increasing indexes.
`journal.json` maps these indexes to approved paths, supported metadata, completion state, and remaining actions.
Recovery resources are not feature directories or a permanent report ledger.
Successful application verifies actual files and removes its temporary resources.

A handled failure reverses only this attempt's changes.
Complete restoration means failed transfer, restored data, and review not performed.
Incomplete restoration retains indexed originals and exact remaining operations, with recovery-required data and incomplete review.
Unexpected content from another writer remains intact alongside the saved original.
The report must not name an unowned directory as an attempt-created removal target.

An `inspect-directory` action requires manual inspection, not removal of the current directory.
Preserve the current directory.
Inspect the recovery journal before you decide which owned paths need recovery.
An identity-capture failure after successful directory creation leaves a pending `inspect-directory` action.
The result does not claim complete restoration while that directory remains unresolved.

`inspect-recovery-directory` and `inspect-lock` also require manual inspection, not removal.
Preserve the current resource.
If a resource changed identity, locate the attempt-owned resource before recovery.
The backend does not claim saved originals at a foreign path.
A lock identity failure closes the descriptor and reports the retained lock.

A lock-cleanup failure reports the actual applied, unchanged, or restored data outcome separately.
Preserve completed application when only lock cleanup remains.
Do not assume that cleanup-only recovery contains original backups.
Continue read-only reconciliation after applied data with cleanup failure.
Report incomplete review and blocked readiness until cleanup and a new reconciliation finish.

If interruption prevents an application result, the helper retains the recovery journal and payloads.
It does not claim automatic restoration.
Inspect the retained resources before another merge.

Follow the result's exact remaining operations before another transfer or a clean readiness claim.
Stop every writer before removing a reported lock.
The command provides no automatic recovery guarantee after power loss or forced termination.
There is no separate merge recovery or status command.

### Move an existing local project

Stop active agents and workflows before a move.
Choose an absent or empty destination outside the code repository.

```bash
# Interactive cleanup consent
specify project move "$HOME/speckit-specs/my-project"

# Alternative: explicit consent for noninteractive use
specify project move "$HOME/speckit-specs/my-project" --confirm-remove-local
```

Spec Kit copies and verifies local metadata, feature artifacts, and the saved active feature before it asks to remove originals.
The preview lists every copied file and its verified destination.
The explicit flag supplies cleanup consent after verification.
Successful moves leave only the repository locator and required native agent files.
The move preserves personal defaults and the code repository's Git history.
It creates workspace history unless you use `--no-workspace-git`.

During cutover, Spec Kit refreshes unchanged managed helpers and stock feature-selection instructions from the installed CLI.
It preserves edited context, templates, command prose, and native metadata.
It does not claim ownership of user edits.
Edited or unowned helpers must already match the current bundle.
Otherwise, the move fails and keeps both copies for recovery.
Save helper edits separately before you restore managed helpers and retry.

After staging, a refusal leaves the local project usable and retains the verified staged copy.
Inspect that copy before retrying with an empty destination.
Cutover failures restore local state and report recovery paths.
If recovery itself fails, keep both copies and the reported recovery directory.

Migration refuses source symlinks and saved active features outside the repository before staging.
Replace source symlinks with local copies before migration.
For an outside feature, copy it into the repository and update local `.specify/feature.json` to select that copy.
Projects that remain local retain their existing absolute feature overrides.

### Relink a workspace

```bash
specify project link "/path/to/existing/workspace"
specify project info --json
```

Run these commands from the code repository after a clone, rename, or machine change.
The link command verifies the workspace identity before it updates the machine-local record.
It does not change the shared locator or read personal setup defaults.
A new machine record has no active feature. Select a feature explicitly before a feature-dependent command.

### Shared OMP commands

```bash
specify integration install omp --global
specify init my-project --integration omp --global-commands --storage external
specify project command speckit.specify --json
```

Global installation works outside a project and targets the active OMP profile's command directory.
The launchers contain no project content.
Each invocation resolves the current project's command, configuration, presets, extensions, and feature selection.
The JSON response contains `content`, `repository_root`, `workspace_root`, and `feature_dir`.

Use `specify integration upgrade omp --global` or `specify integration uninstall omp --global` to manage the shared set.
These commands preserve unrelated files and edited launchers, including with `--force`.
Project or add-on removal does not remove shared launchers that another project can use.
Other integrations do not support `--global` or `--global-commands`.

### Environment Variables

| Variable          | Description                                                              |
| ----------------- | ------------------------------------------------------------------------ |
| `SPECKIT_INTEGRATION_DEFAULT` | Override the fallback integration used by `specify init` when `--integration` is omitted (interactive prompt default and non-interactive fallback). Set it to any registered integration key (e.g. `gemini`, `claude`). An unrecognized value is ignored with a warning and the built-in default (`copilot`) is used. An explicit `--integration <key>` always takes precedence. |
| `SPECIFY_INIT_DIR` | Select the code repository explicitly, including from a monorepo root. Relative values resolve from cwd. The directory must contain `.specify/`. Invalid values fail without fallback. Without the override, core scripts and project CLI commands search upward from cwd. |
| `SPECIFY_FEATURE_DIRECTORY` | Override saved feature selection. Relative paths join the workspace. External selections must stay inside that workspace. Local mode does not confine the value: a relative path such as `../shared-feature` or an absolute path can select a directory outside the project. |
| `SPECIFY_FEATURE` | Override the reported feature label, independently of Git. This does not select a feature directory. Use `SPECIFY_FEATURE_DIRECTORY` or saved feature selection for that purpose. |
| `SPECIFY_FEATURE_NO_PERSIST` | Set to `1` or `true` to prevent scripts from saving active-feature changes. Applies to local `.specify/feature.json` and external machine-local records. Use it for independent concurrent selections. |

> **Three resolution axes.** `SPECIFY_INIT_DIR` selects the code repository. Its storage locator selects the workspace. Explicit or saved feature selection selects the feature. Local projects use the repository as their workspace. A local feature defaults to `specs/`, but an explicit selection may live outside the project.
>
> **Version control.** `specify init` scaffolds a managed `.specify/.gitignore` that excludes machine-local state — `feature.json` (the current-feature pointer, rewritten on every feature switch) and per-machine extension `extensions/*/local-config.yml` overrides — while leaving everything else under `.specify/` (constitution, templates, scripts, extension config) shareable so teams stay aligned. Like the rest of `.specify/`'s shared scripts and templates, the file is tracked in the shared-infrastructure manifest: your edits are preserved on re-init and `specify init --here --force` restores the managed content. It is intentionally left in place by `specify integration uninstall`, which only removes the uninstalled agent's own files.
>
> **Symlinked project roots.** `SPECIFY_INIT_DIR` relocates *where* the project is, not *how* a command treats symlinks: each command keeps its existing cwd-path stance. Commands that traverse and write project files through broad input paths (`bundle`, `workflow run <file>`) refuse a symlinked `.specify/` to preserve write confinement. Other project-scoped commands keep their existing behavior when `SPECIFY_INIT_DIR` points at a project root, which may include following a symlinked `.specify/`.

## Naming Features with the Helper Scripts

When calling the bundled `create-new-feature` helper scripts directly, generated
names retain Unicode letters and decimal digits in UTF-8, so a description such as
`添加用户` produces `001-添加用户`. Descriptions made only of punctuation can still
produce an empty suffix such as `001-`; the scripts warn on stderr when this
happens, including during a dry run. JSON output remains parseable.

To choose a different name, keep the original description and supply a short name:

```bash
bash .specify/scripts/bash/create-new-feature.sh --json --short-name 用户管理 "添加用户"
```

The Python helper also accepts `--short-name`; the PowerShell helper uses
`-ShortName`. A supplied short name is cleaned by the same rules, so it must
contain at least one letter or digit. For non-ASCII names, the Bash helper needs
an installed UTF-8 locale and a Python 3 interpreter for Unicode classification.
ASCII input, including tabs and newlines, is sanitized without either requirement.
If `LC_ALL` is non-empty, Bash uses that locale rather than selecting another:
Unicode names fail with an error if the selected locale is not usable for UTF-8
names. With `LC_ALL` unset or empty, Bash selects an installed UTF-8 locale even
when `LANG` or `LC_CTYPE` names a non-UTF-8 locale.
ASCII capitals are lowercased; non-ASCII letter casing is preserved across the
script variants.

## Check Installed Tools

```bash
specify check
```

Checks that CLI-based AI coding agents are available on your system. IDE-based agents are skipped since they don't require a CLI tool.

This command stays offline. If a command behaves like an older Spec Kit version or an expected CLI feature is missing, run `specify self check` to check whether your local CLI is behind the latest release.

## Version Information

```bash
specify version
```

Displays the Spec Kit CLI version, Python version, platform, and architecture.

To inspect local CLI capabilities without checking the network:

```bash
specify version --features
specify version --features --json
```

The JSON form is intended for scripts and coding agents that need to choose a
workflow based on the installed CLI's supported features.

A quick version check is also available via:

```bash
specify --version
specify -V
```
