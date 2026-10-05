# Merge Upstream Release

Merge the latest published stable release from `github/spec-kit` into this fork's `main` branch.
Invocation authorizes a local merge commit after verification. Do not push.

## 1. Prepare

1. Read `AGENTS.md`, `CONTRIBUTING.md`, the `Unreleased` section of `CHANGELOG.md`, and `README.md` under Install.
2. Inspect the current branch, working tree, index, and remote URLs.
3. Identify the upstream remote by its URL (`github.com/github/spec-kit`), not its name.
   If no remote has that URL, add it as `upstream` with `https://github.com/github/spec-kit.git`.
4. If the branch is not `main`, stop and ask before switching branches.
5. If tracked changes or an unfinished Git operation exist, stop and ask how to preserve them.
6. Leave unrelated untracked files untouched, including `.omp/config.yml`, generated `.omp/commands/speckit.*.md`, and `specs/`.

Never reset, discard, automatically stash, or commit unrelated user work.

7. Fetch upstream release tags: `git fetch <upstream> 'refs/tags/v*:refs/tags/v*'`.
8. Find the newest upstream release already in `HEAD`. Upstream tags a version-bump commit beside `main`,
   so before the first tag merge `git describe` finds only old tags. Accept a tag when it or its parent is in `HEAD`:

   ```bash
   for t in $(git tag -l 'v[0-9]*' --sort=-v:refname); do
     git merge-base --is-ancestor "$t^" HEAD && { echo "$t"; break; }
   done
   ```

9. Before merging, force the local tag `u<version>` to the current `HEAD`.

   ```bash
   git tag -f "u<version>" HEAD
   ```

This tag records the fork head before the next upstream merge. For example, `v0.12.11` in `HEAD` produces `u0.12.11`.

## 2. Select the Release

1. Query the latest published release:

   ```bash
   gh release view --repo github/spec-kit --json tagName,publishedAt,isPrerelease,url
   ```

2. Require a stable release, not a prerelease or a tag selected only by local sorting.
3. If the local tag conflicts with upstream, stop and report the mismatch instead of forcing replacement.
4. If the tag is already an ancestor of `HEAD`, report that the release is already merged and stop.
5. Record the pre-merge commit and inspect incoming commits and changed paths from the merge base.
6. List the fork-owned commits with `git log --no-merges --oneline <tag>...HEAD --right-only`.
   They and the `CHANGELOG.md` `Unreleased` entries are the fork decisions to preserve.

## 3. Merge

1. Run `git merge --no-ff --no-commit <tag>`.
2. If conflicts occur, read the `resolving-merge-conflicts` skill before resolving them.
3. Preserve the fork decisions while integrating upstream changes. These always stay fork-specific:
   - `_GITHUB_REPO = "Axiweave/spec-kit"` in `src/specify_cli/_version.py`.
   - Install commands that use `https://github.com/Axiweave/spec-kit.git` in `README.md`, every `README.*.md`, and `docs/`.
   - The fork `Unreleased` entries in `CHANGELOG.md`, above the merged upstream release entries.
   - External storage, workspace history, feature selection, project move, and merge-specs behavior.
4. Inspect automatic merges in fork-specific paths as carefully as explicit conflicts.
5. Check new upstream code that reads or writes `.specify/` against the external-storage contract.
   Workspace-owned files must resolve through `workspace_root_for`, not the repository root.
6. Check new upstream install links and commands, and point them at the fork.

Split independent fork-area reviews between subagents when useful. Keep shared-file edits and final verification under one owner.
Subagents must skip builds, formatters, linters, and tests during concurrent edits.

## 4. Verify

1. Run `uv sync --extra test` in this checkout.
2. Run the full suite through this checkout's virtualenv. Read its own exit status, never a filtered pipeline's.

   ```bash
   .venv/bin/python -m pytest tests -q -p no:cacheprovider
   ```

3. Run `git ls-files -z -- '*.sh' | xargs -0 shellcheck --severity=error`.
4. Run `uvx ruff check` on the Python files that the merge changed in fork-owned paths.
5. Smoke-test an external project with this checkout's CLI in an isolated temporary directory:

   ```bash
   S="$PWD/.venv/bin/specify"
   T=$(mktemp -d)
   export HOME="$T/h" XDG_CONFIG_HOME="$T/c" XDG_DATA_HOME="$T/d" GIT_CONFIG_GLOBAL="$T/gitconfig"
   mkdir -p "$T/repo" && cd "$T/repo" && git init -q
   "$S" init --here --force --storage external --workspace "$T/ws" --integration claude \
     --script sh --non-interactive --ignore-agent-tools --no-workspace-git
   "$S" project info --json
   "$S" integration status
   "$S" artifact list --json
   ```

   Run the snippet in a subshell so the exported variables and `cd` do not leak. Remove `$T` afterward.
6. From the checkout, run `.venv/bin/specify self check` and confirm it reports the Axiweave fork, not upstream.
   This checks the merged checkout, not the `specify` launcher on `PATH`.

Diagnose failed checks before repeating them. Do not suppress failures.
If a required check remains blocked, leave the merge uncommitted and report the exact blocker.
PowerShell tests skip when `pwsh` is absent. Report them as skipped, never as passing.

### Separating a regression from ambient state

1. A red test proves nothing by itself. Measure the same test at the pre-merge commit before you call it a regression.
2. Add a baseline worktree when a verdict needs one: `git worktree add /tmp/speckit-premerge <pre-merge-commit>`,
   then run `uv sync --extra test` in it and use its own `.venv/bin/python`.
3. Remove the baseline worktree when the merge is done.

## 5. Complete

1. Remove temporary verification scripts and smoke projects.
2. Inspect generated changes and the staging scope before committing.
3. Stage only merge resolutions, alongside the changes Git staged for the merge.
4. Create the merge commit with the subject `Merge tag '<tag>'` and an `Assisted-by:` trailer as `CONTRIBUTING.md` requires.
5. Report the release, commit, conflict outcome, verification results, and any warnings.
6. State that unrelated user files remain untouched and that the merge was not pushed.
