from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import shutil
import subprocess
from pathlib import Path


_GIT_PATH_VARIABLES = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
}


class WorkspaceGitError(ValueError):
    """Report whether an initial commit already made workspace data durable."""

    def __init__(self, message: str, *, committed: bool = False):
        super().__init__(message)
        self.committed = committed


def _git(
    directory: Path, *args: str, write: bool = False, input_text: str | None = None,
    allowed_codes: tuple[int, ...] = (0,),
) -> str:
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_PATH_VARIABLES}
    environment["LC_ALL"] = "C"  # Untranslated messages let preflight recognize "not a git repository".
    try:
        result = subprocess.run(
            ["git", *args], cwd=directory, env=environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120 if write else 15, input=input_text,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OSError(
            f"Workspace Git failed at {directory}: {exc}. "
            "Inspect retained files. Retry with an empty destination or use --no-workspace-git."
        ) from exc
    if result.returncode not in allowed_codes:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ValueError(
            f"Workspace Git {' '.join(args[:2])} failed at {directory}: {detail}. "
            "Check Git identity, hooks, and signing. Use --no-workspace-git to skip workspace history."
        )
    return result.stdout.rstrip("\n")


def _safe_directory(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Workspace path contains a symlink: {candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"Workspace path is not a directory: {candidate}")


def preflight_workspace_git(destination: Path) -> None:
    """Validate default workspace history before ownership or filesystem writes."""
    destination = destination.expanduser().absolute()
    if ".." in destination.parts:
        raise ValueError(f"Workspace path contains traversal: {destination}")
    _safe_directory(destination)
    if shutil.which("git") is None:
        raise ValueError("Git is unavailable. Install Git or use --no-workspace-git.")
    for ancestor in (destination, *destination.parents):
        if (ancestor / ".git").exists() or (ancestor / ".git").is_symlink():
            raise ValueError(
                f"Workspace destination belongs to a Git repository: {ancestor}. "
                "Choose a separate empty destination or use --no-workspace-git."
            )
    existing = next(parent for parent in (destination, *destination.parents) if parent.exists())
    try:
        inside = _git(existing, "rev-parse", "--absolute-git-dir")
    except ValueError as exc:
        if "not a git repository" not in str(exc):
            raise ValueError(
                f"Git refused to inspect the workspace destination, which may belong to a Git repository "
                f"(for example, a bare repository blocked by safe.bareRepository): {existing}. "
                "Choose a destination outside any repository or use --no-workspace-git."
            ) from exc
        inside = ""
    if inside:
        raise ValueError(f"Workspace destination belongs to a Git repository: {existing}. Choose a separate destination.")
    for identity in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        _git(existing, "var", identity)


# Anchored workspace-relative rules, emitted as `/<rule>` in .gitignore. `*` never crosses `/`.
_PRIVATE_PATTERNS = (
    ".specify/feature.json", ".specify/auth.json",
    ".specify/extensions/*/local-config.yml",
    ".specify/extensions/*/*-config.local.yml",
    ".specify/extensions/.cache/*", ".specify/extensions/.backup/*",
    ".specify/extensions/.rescue-staging-*/*",
    ".specify/presets/.cache/*",
    ".specify/extensions/.reinstall-staging-*/*",
    ".specify/presets/.reinstall-staging-*/*",
    ".specify/workflows/.cache/*", ".specify/workflows/steps/.cache/*",
    ".specify/workflows/runs/*", ".specify/workflows/.workflow-install.lock",
    ".specify/workflows/.*.installing-*/*", ".specify/workflows/.*.backup-*/*",
    ".specify/workflows/.*.failed-*/*",
    ".specify/workflows/steps/speckit_step_tmp_*/*",
    ".specify/workflows/.workflow-registry.json.*.tmp",
    ".specify/scripts/python/__pycache__/*",
    ".specify/extensions/*/scripts/python/__pycache__/*",
)
_PRIVATE_RULES = tuple(
    re.compile("".join("[^/]*" if char == "*" else re.escape(char) for char in pattern))
    for pattern in _PRIVATE_PATTERNS
)
_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template"}


def _private(name: str) -> bool:
    """Apply the emitted ignore rules with Git semantics: an ignored directory hides its contents."""
    parts = name.split("/")
    for index, leaf in enumerate(parts, 1):
        if (leaf == ".env" or leaf.startswith(".env.")) and leaf not in _ENV_TEMPLATES:
            return True
        if any(rule.fullmatch("/".join(parts[:index])) for rule in _PRIVATE_RULES):
            return True
    return False


def _files(root: Path) -> list[str]:
    result = []

    def fail_walk(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(root, followlinks=False, onerror=fail_walk):
        for name in [*directories, *files]:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Workspace requires confined regular files: {path}")
        if ".git" in directories or ".git" in files:
            raise ValueError(f"Workspace already contains Git metadata: {directory}")
        result.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
    return sorted(result)


def _producer_hashes(root: Path) -> dict[str, str]:
    records: list[tuple[dict, set[str]]] = []
    for path in (root / ".specify/integrations").glob("*.manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            recovered = data.get("recovered_files", [])
            if not isinstance(recovered, list) or not all(isinstance(name, str) for name in recovered):
                raise ValueError("Recovered provenance paths must be a list of strings.")
            records.append((data.get("files", {}), set(recovered)))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid producer provenance at {path}: {exc}") from exc
    for name, key in (
        (".specify/extensions/.registry", "extensions"),
        (".specify/presets/.registry", "presets"),
        (".specify/workflows/workflow-registry.json", "workflows"),
        (".specify/workflows/steps/step-registry.json", "steps"),
    ):
        path = root / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for metadata in data.get(key, {}).values():
                records.append((metadata.get("generated_files", {}), set()))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"Invalid producer provenance at {path}: {exc}") from exc
    result = {}
    for files, recovered in records:
        if not isinstance(files, dict):
            raise ValueError("Invalid producer provenance: files must be a mapping.")
        for name, digest in files.items():
            if not isinstance(name, str) or not name or name in {".", ".."}:
                raise ValueError("Invalid producer provenance path.")
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
                raise ValueError(f"Invalid producer provenance path: {name}")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"Invalid producer provenance digest at {name}")
            if name in recovered:
                continue
            if name in result and result[name] != digest:
                raise ValueError(f"Conflicting producer provenance at {name}")
            result[name] = digest
    return result


def _required(name: str) -> bool:
    return (
        name in {".gitignore", ".specify/.gitignore", ".specify/workspace.json",
                 ".specify/init-options.json", ".specify/integration.json"}
        or name.startswith((".specify/memory/", "specs/"))
        or name.endswith(("/spec.md", "/plan.md", "/tasks.md", ".manifest.json", "/.registry"))
        or name.rsplit("/", 1)[-1] in {
            "extension.yml", "preset.yml", "workflow-registry.json", "step-registry.json",
        }
    )


def _escape_ignore(name: str) -> str:
    return "/" + "".join("\\" + char if char in "\\*?[]!# " else char for char in name)


def initialize_workspace_git(workspace: Path) -> str:
    """Create one independent initial commit for an already claimed workspace."""
    workspace = workspace.absolute()
    _safe_directory(workspace)
    files = _files(workspace)
    hashes = _producer_hashes(workspace)
    generated = {
        name for name in files if name in hashes and not _required(name)
        and hashlib.sha256((workspace / name).read_bytes()).hexdigest() == hashes[name]
    }
    eligible = [name for name in files if not _private(name) and name not in generated]
    ignore = workspace / ".gitignore"
    ignore_existed = ignore.exists()
    original = ignore.read_bytes() if ignore_existed else b""
    newline = "\r\n" if b"\r\n" in original else "\n"
    rules = ["/" + pattern for pattern in _PRIVATE_PATTERNS]
    rules += ["**/.env", "**/.env.*", "!**/.env.example", "!**/.env.sample", "!**/.env.template"]
    rules += [_escape_ignore(name) for name in sorted(generated)]
    additions = newline.join(rule for rule in rules if rule.encode() not in original.splitlines())
    updated = original + (newline.encode() if original and not original.endswith(b"\n") else b"")
    updated += additions.encode() + newline.encode() if additions else b""
    git_dir = workspace / ".git"
    git_dir.mkdir()
    git_info = git_dir.stat()
    committed = False
    try:
        _git(workspace, "init", write=True)
        actual_root = Path(_git(workspace, "rev-parse", "--show-toplevel")).resolve()
        if actual_root != workspace.resolve():
            raise ValueError(f"Workspace Git worktree root differs from the owned destination: {actual_root}")
        if additions:
            ignore.write_bytes(updated)
        if ".gitignore" not in eligible:
            eligible.append(".gitignore")
        ignored = set(_git(
            workspace, "check-ignore", "--stdin", "-z",
            input_text="".join(name + "\0" for name in eligible), allowed_codes=(0, 1),
        ).split("\0"))
        for name in eligible:
            if name in ignored and _required(name):
                raise ValueError(f"Required durable content is ignored: {name}. Adjust the user ignore rule.")
        selected = [name for name in eligible if name not in ignored]
        _safe_directory(workspace)
        current = (workspace / ".git").lstat()
        if (current.st_dev, current.st_ino) != (git_info.st_dev, git_info.st_ino) or stat.S_ISLNK(current.st_mode):
            raise ValueError(f"Workspace Git ownership changed: {workspace}")
        for name in selected:
            path = workspace / name
            _safe_directory(path.parent)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"Workspace file changed before staging: {path}")
        paths = "".join(name + "\0" for name in selected)
        _git(workspace, "--literal-pathspecs", "add", "--pathspec-from-file=-", "--pathspec-file-nul",
             write=True, input_text=paths)
        _git(workspace, "--literal-pathspecs", "commit", "--only", "--message=Initialize Spec Kit workspace",
             "--pathspec-from-file=-", "--pathspec-file-nul", write=True, input_text=paths)
        committed = True
        return _git(workspace, "rev-parse", "HEAD")
    except (OSError, ValueError) as exc:
        try:
            current = git_dir.lstat()
            owned = (
                not stat.S_ISLNK(current.st_mode)
                and (current.st_dev, current.st_ino) == (git_info.st_dev, git_info.st_ino)
            )
            # A hook or a timed-out commit can create history before returning an error.
            has_history = owned and any(path.is_file() for path in (git_dir / "refs").rglob("*"))
            has_history = has_history or (owned and (git_dir / "packed-refs").exists())
            committed = committed or has_history
            if owned and not committed:
                if ignore.is_file() and not ignore.is_symlink() and ignore.read_bytes() == updated:
                    if ignore_existed:
                        ignore.write_bytes(original)
                    else:
                        ignore.unlink()
                shutil.rmtree(git_dir)
        except OSError as cleanup_error:
            exc.add_note(f"Workspace Git recovery needs attention at {workspace}: {cleanup_error}")
        raise WorkspaceGitError(
            f"Workspace history setup failed: {exc}. Partial files remain at {workspace}. "
            "Inspect the workspace before retrying with an empty destination.",
            committed=committed,
        ) from exc
