# Shared Workspaces

External storage keeps specs, plans, tasks, and Spec Kit state in a workspace
folder outside the code repository. This guide shows how a team shares one
workspace through Git. Follow sections 1 to 5 in order to get a working team
setup. The other sections are for later tasks.

## The Sharing Model

A project with external storage uses two Git repositories:

| Repository | Holds | Shared through |
| --- | --- | --- |
| Code repository | Your source code, `.gitignore`, and the locator `.specify/project.json` | Your usual code remote |
| Workspace repository | `specs/`, `.specify/memory/`, project state, and package declarations. Generated scripts and templates stay out of its history, and `specify project link` restores them on each machine | A second remote that you create |

- The locator holds the project ID. Commit it, so that each teammate can link a
  checkout to the workspace.
- Each checkout has its own record, `.specify/checkout.json`. The record holds
  the workspace path and the saved feature of this checkout only. Git ignores
  it. A clone, a worktree, and a second machine each get their own record.
- `specify project link` attaches one checkout. It writes the record of this
  checkout and restores missing generated agent files. It never changes a file
  or a record of another checkout.
- Spec Kit does not commit, pull, or push the workspace. You sync it with
  ordinary Git commands.

## Before You Start

You need Git, a Git host for two remotes, and the Spec Kit CLI on each machine:

```bash
uv tool install specify-cli --from git+https://github.com/Axiweave/spec-kit.git
```

The examples use the `claude` integration and the workspace path
`~/speckit-specs/my-project`. Replace them with your integration key and path.

## 1. Create the Workspace (Team Lead)

1. Go to the root of the code repository:

   ```bash
   cd my-project
   ```

2. Initialize Spec Kit with an external workspace:

   ```bash
   specify init --here --integration claude --workspace ~/speckit-specs/my-project
   ```

   When the repository already has files, add `--force`. Init asks no questions
   when you also pass `--script sh` and `--non-interactive`.

   `--workspace` implies `--storage external`. Init creates the workspace and
   its first Git commit. New external projects use timestamp feature names,
   for example `specs/20261006-101500-login`.

3. Commit the locator and the `.gitignore` lines:

   ```bash
   git add .gitignore .specify/project.json
   git commit -m "Add Spec Kit locator"
   git push
   ```

   The `.gitignore` file holds `/.specify/checkout.json` and the generated
   agent files. Commit it, so that every checkout ignores its own record.

4. Create an empty repository on your Git host for the workspace. Then push
   the workspace to it:

   ```bash
   cd ~/speckit-specs/my-project
   git remote add origin <workspace-url>
   git push -u origin HEAD
   ```

   Init makes the first workspace commit on your Git default branch
   (`git config init.defaultBranch`). Make sure the remote uses the same
   branch as its default. Otherwise a teammate's clone is empty and link fails.

5. Create features with your agent, for example `/speckit-specify`. The agent
   writes them to `specs/` in the workspace.

## 2. Join the Project (Each Teammate)

1. Clone the code repository:

   ```bash
   git clone <code-url> my-project
   ```

2. Clone the workspace repository:

   ```bash
   git clone <workspace-url> ~/speckit-specs/my-project
   ```

3. Link the checkout to the workspace:

   ```bash
   cd my-project
   specify project link ~/speckit-specs/my-project
   ```

   The output names the workspace and the number of restored files. The
   number depends on the integration:

   ```text
   Workspace: /home/you/speckit-specs/my-project
   Restored: 22 files
   ```

4. Make sure that the link works:

   ```bash
   specify project info
   ```

   The `workspace_root` line shows your workspace path.

Link can print more lines. Do what each line tells you:

| Output line | Action |
| --- | --- |
| `Updated: .gitignore. Commit it so other checkouts get the ignore line.` | Commit `.gitignore` |
| `Locator written: .specify/project.json. Commit it so teammates can link.` | Commit `.specify/project.json` |
| `Kept modified file: <relative path>` | Link kept your edit of a generated file |
| `Repair: specify integration upgrade <key> --force` | Run it to replace the edited files with the managed copy |
| `Next: specify project select <feature>` | Select a feature for this checkout |

## 3. Add a Worktree or Another Clone

Each checkout needs its own link. For a worktree on the same machine:

```bash
git worktree add ../my-project-b
cd ../my-project-b
specify project link ~/speckit-specs/my-project
```

The worktree and the first checkout use the same workspace folder. Each keeps
its own record, so each can work on a different feature.

In the default `context` selection mode, the agent finds the feature from the
conversation. You can also set `SPECIFY_FEATURE_DIRECTORY` for one command. In
a project that uses `--feature-selection automatic`, save the feature of this
checkout:

```bash
specify project select specs/20261006-101500-login
```

## 4. Sync Through Git

Do these steps in the workspace folder.

1. Get the work of your teammates before you start:

   ```bash
   cd ~/speckit-specs/my-project
   git pull --no-rebase
   ```

2. Run your Spec Kit skills in the agent as usual.

3. Commit and push your changes:

   ```bash
   git add -A
   git commit -m "Update login spec"
   git push
   ```

If `git push` fails because the remote has new commits, do step 1 again, then
push.

## 5. Resolve a Same-File Conflict

Two teammates can change the same file, for example
`specs/20261006-101500-login/spec.md`. Then `git pull --no-rebase` stops with
`CONFLICT (content): Merge conflict in specs/20261006-101500-login/spec.md`.

1. Open the file in an editor.
2. Find each block between `<<<<<<<` and `>>>>>>>`. The text above `=======`
   is yours. The text below it is your teammate's.
3. Write the correct text for each block, and remove the three marker lines.
4. Mark the file as resolved:

   ```bash
   git add specs/20261006-101500-login/spec.md
   ```

5. Complete the merge and push it:

   ```bash
   git commit --no-edit
   git push
   ```

To stop the merge and go back to the state before the pull, run
`git merge --abort`.

## Duplicate Feature Numbers

The create-feature helper reserves each feature folder before it writes to it.
Parallel runs on one workspace folder get different names. Separate clones of
the workspace can still make the same number, for example `specs/008-login`
and `specs/008-billing`. After a pull, `specify project info` shows them:

```text
Duplicate feature prefixes:
  specs/008-billing, specs/008-login
Rename with: specify project migrate-naming --feature-numbering timestamp --dry-run
```

- Sequential names: run the command from the hint. It shows a preview before
  it changes a name.
- Timestamp names: the hint is
  `Rename one directory of each timestamp group by hand to a free timestamp prefix.`

Commit and push the rename from the workspace folder.

## What `merge-specs` Does Not Do

`specify project merge-specs` and `/speckit.merge` transfer reviewed artifacts
from one specification set to another. They do not replace Git sync:

- **No Git sync.** They do not commit, pull, or push. Commit the result by
  hand.
- **No three-way merge.** When both sides changed one file, the proposal
  reports `These artifact bytes differ.` You must give the full replacement
  content. Use Git for same-file conflicts in a shared workspace.
- **No deletion propagation.** A file that the source deleted stays in the
  destination. Delete it by hand.

## Recover After a Forced Stop

A forced stop of a rename, a merge, or a move can leave a lock or a recovery
folder. Run this from the code checkout:

```bash
specify project recover
```

The command changes nothing. It exits 0 when nothing is left, and exits 1 when
it lists leftovers. For each leftover, it shows the owner process and its
state.

To restore the leftovers whose owner stopped, run:

```bash
specify project recover --apply
```

`--apply` refuses a leftover when the owner still runs, when it cannot confirm
that the owner stopped, or when a target file changed after the stop. It
never changes a `move-recovery` leftover. Follow the manual steps in the
output.

## Leave External Storage

### Detach One Checkout

```bash
specify project unlink
```

Unlink removes `.specify/checkout.json` and `.specify/project.json` from this
checkout. It never reads or changes the workspace, so it also works when the
workspace is lost.

When Git tracks the locator, unlink prints
`Warning: Git tracks .specify/project.json. Committing this deletion detaches every teammate's checkout.`
Do not commit that deletion unless the whole team leaves the workspace.

After unlink, use `specify init --here --storage local` to start again with
local storage. To use a backup of the workspace, use
`specify project link <backup>` instead.

### Lost Workspace

When the workspace folder is missing or broken, each project command names
`specify project unlink` and `specify project link <backup>`. Clone the
workspace again from its remote, then link to the clone:

```bash
git clone <workspace-url> ~/speckit-specs/my-project
specify project link ~/speckit-specs/my-project
```

### Convert the Project to Local Storage

```bash
specify project move --to-local
```

The command copies `specs/` and `.specify/` from the workspace into the code
repository. It checks the copied bytes, removes the locator and the record,
and never writes to the workspace. It rolls back on an error or Ctrl-C. The
output ends with `The workspace was not changed.`

The command refuses when the repository already has `specs/`, or when
`.specify/` holds other files than the locator, the record, and private
checkout manifests. Review `git status` after the move, then commit the
result.

## Projects Without Git

External storage also works in a project folder that is not a Git repository.
The `.gitignore` line then has no effect. The record `.specify/checkout.json`
is a local file for one checkout on one machine. Do not copy it to another
machine. Run `specify project link` on each machine instead. When you copy a
checkout folder, run `specify project link` in the copy.
