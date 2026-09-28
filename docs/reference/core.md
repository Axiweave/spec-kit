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
External scripts recover feature selection from the machine-local project record.
Local projects retain `.specify/feature.json` and existing absolute feature overrides.
In external mode, feature overrides must remain inside the selected workspace.

Machine records use `$XDG_DATA_HOME/specify/projects/`, or `~/.local/share/specify/projects/` on Unix when unset.
Windows uses the per-user local application-data directory when XDG is unset.
Create a separate workspace backup. External storage does not provide synchronization or erase earlier Git history.

### Personal defaults

```bash
specify config set storage_root "~/speckit-specs"
specify config set feature_numbering timestamp
specify config set integration omp
specify config set script py
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
The move preserves personal defaults and does not rewrite Git history.

During cutover, Spec Kit refreshes unchanged managed helpers and stock feature-selection instructions from the installed CLI.
It preserves edited context, templates, command prose, and native metadata.
It does not claim ownership of user edits.
Edited or unowned helpers must already match the current bundle.
Otherwise, the move fails and keeps both copies for recovery.
Save helper edits separately before you restore managed helpers and retry.

A refusal leaves the local project usable and retains the verified staged copy.
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
| `SPECIFY_FEATURE_DIRECTORY` | Override saved feature selection. Relative paths resolve under the workspace. External selections must stay inside that workspace. Local mode retains absolute overrides. |
| `SPECIFY_FEATURE` | Override the reported feature label, independently of Git. This does not select a feature directory. Use `SPECIFY_FEATURE_DIRECTORY` or saved feature selection for that purpose. |
| `SPECIFY_FEATURE_NO_PERSIST` | Set to `1` or `true` to prevent scripts from saving active-feature changes. Applies to local `.specify/feature.json` and external machine-local records. Use it for independent concurrent selections. |

> **Three resolution axes.** `SPECIFY_INIT_DIR` selects the code repository. Its storage locator selects the workspace. Explicit or saved feature selection selects the feature. Local projects use the repository as their workspace.
>
> **Version control.** `specify init` scaffolds a managed `.specify/.gitignore` that excludes machine-local state — `feature.json` (the current-feature pointer, rewritten on every feature switch) and per-machine extension `extensions/*/local-config.yml` overrides — while leaving everything else under `.specify/` (constitution, templates, scripts, extension config) shareable so teams stay aligned. Like the rest of `.specify/`'s shared scripts and templates, the file is tracked in the shared-infrastructure manifest: your edits are preserved on re-init and `specify init --here --force` restores the managed content. It is intentionally left in place by `specify integration uninstall`, which only removes the uninstalled agent's own files.
>
> **Symlinked project roots.** `SPECIFY_INIT_DIR` relocates *where* the project is, not *how* a command treats symlinks: each command keeps its existing cwd-path stance. Commands that traverse and write project files through broad input paths (`bundle`, `workflow run <file>`) refuse a symlinked `.specify/` to preserve write confinement. Other project-scoped commands keep their existing behavior when `SPECIFY_INIT_DIR` points at a project root, which may include following a symlinked `.specify/`.

## Naming Features with the Helper Scripts

When calling the bundled `create-new-feature` helper scripts directly, generated
names retain only ASCII letters and digits. A description entirely in a non-Latin
script, or made only of punctuation, can therefore produce an empty suffix such
as `001-`. The scripts warn on stderr when this happens, including during a dry
run; JSON output remains parseable.

Keep the original description and supply a readable ASCII short name:

```bash
bash .specify/scripts/bash/create-new-feature.sh --json --short-name user-auth "添加用户"
```

The Python helper also accepts `--short-name`; the PowerShell helper uses
`-ShortName`. A supplied short name is cleaned by the same rules, so it must
contain at least one ASCII letter or digit.

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
