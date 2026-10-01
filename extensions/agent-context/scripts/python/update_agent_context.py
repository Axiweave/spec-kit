#!/usr/bin/env python3
"""Refresh the managed Spec Kit section in the coding agent's context file(s).

Python port of ``update-agent-context.sh`` / ``update-agent-context.ps1``.

Reads ``context_files`` or ``context_file``, plus ``context_markers.{start,end}``,
from the agent-context extension config:
    .specify/extensions/agent-context/agent-context-config.yml

Usage: update_agent_context.py [plan_path]

When ``plan_path`` is omitted, the script uses the project choice
``feature_selection`` from ``.specify/init-options.json``:

- ``context`` (the default): the plan of the feature in
  ``SPECIFY_FEATURE_DIRECTORY``. The script never reads ``.specify/feature.json``
  and never scans ``specs/``. It stops with an error when the variable is not set.
- ``automatic``: use ``SPECIFY_FEATURE_DIRECTORY`` when set. Otherwise, use
  ``.specify/feature.json`` (written by /speckit-specify), then the newest
  ``plan.md`` under ``specs/`` when the saved plan does not exist.

External projects read configuration and the selected plan from their workspace.
The native context files remain in the repository. External selection does not
use the local modification-time fallback.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_START = "<!-- SPECKIT START -->"
DEFAULT_END = "<!-- SPECKIT END -->"


def _load_core_common():
    """Load the core helper beside the source or installed extension."""
    path = (
        Path(__file__).resolve().parent / "../../../../scripts/python/common.py"
    ).resolve()
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("speckit_core_common", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _get_str(obj: object, *keys: str) -> str:
    node = obj
    for key in keys:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return ""
    return node if isinstance(node, str) else ""


def _configured_context_files(data: dict, *, case_insensitive: bool = False) -> list[str]:
    """Read configured targets without reading defaults or writing configuration."""
    context_files: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        candidate = value.strip()
        if not candidate:
            return
        key = candidate.casefold() if case_insensitive else candidate
        if key in seen:
            return
        context_files.append(candidate)
        seen.add(key)

    raw_files = data.get("context_files")
    if isinstance(raw_files, list):
        for value in raw_files:
            add(value)
    if not context_files:
        add(_get_str(data, "context_file"))
    return context_files


def _collect_context_files(data: dict, workspace_root: str) -> list[str]:
    """Resolve the managed context files from config, mirroring the bash logic."""
    context_files = _configured_context_files(
        data, case_insensitive=sys.platform.startswith(("win32", "cygwin", "msys"))
    )
    if not context_files:
        # Self-seed: when the config declares no target, derive one from the
        # active integration recorded in init-options.json, mapped through the
        # bundled agent-context-defaults.json file. Independent of the Specify
        # CLI by design.
        integration_key = ""
        try:
            with open(
                f"{workspace_root}/.specify/init-options.json", "r", encoding="utf-8"
            ) as fh:
                opts = json.load(fh)
            if isinstance(opts, dict):
                value = opts.get("integration") or opts.get("ai") or ""
                integration_key = value if isinstance(value, str) else ""
        except Exception:
            integration_key = ""
        if integration_key:
            defaults_path = (
                f"{workspace_root}/.specify/extensions/agent-context/"
                "agent-context-defaults.json"
            )
            mapping = {}
            try:
                with open(defaults_path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                agents = loaded.get("agents", {}) if isinstance(loaded, dict) else {}
                mapping = agents if isinstance(agents, dict) else {}
            except Exception:
                _err(
                    "agent-context: unable to read %s; cannot self-seed the context "
                    "file. Set context_file in the extension config." % defaults_path
                )
                mapping = {}
            context_files = _configured_context_files({
                "context_file": mapping.get(integration_key, "") or ""
            })
            if not context_files:
                _err(
                    "agent-context: no default context file is known for integration "
                    "%s. Set context_file in the extension config to choose one."
                    % integration_key
                )
    return context_files


def discover_managed_sections(
    workspace: Path,
    repository: Path,
    integration: str | None,
    *,
    read_file,
    mentions_reference,
) -> tuple[list, list]:
    """Find existing managed regions without writing or seeding configuration.

    ``read_file`` supplies bytes, mode, and nanosecond modification time. It must
    reject unsafe paths with ``ValueError``. Each section contains the path,
    original bytes, metadata, and UTF-8 text offsets including both markers.
    """
    import yaml

    base = workspace / ".specify/extensions/agent-context"
    config = base / "agent-context-config.yml"
    conflicts = []

    def conflict(code: str, path: Path, message: str) -> None:
        shown = path.relative_to(workspace).as_posix() if path.is_relative_to(workspace) else str(path)
        conflicts.append({"path": shown, "code": code, "message": message})

    try:
        raw = read_file(config)[0]
        data = None if raw is None else yaml.safe_load(raw)
    except (ValueError, yaml.YAMLError):
        conflict("invalid-context-config", config, "The agent-context configuration cannot be read.")
        return [], conflicts
    if raw is None:
        return [], conflicts
    data = data if isinstance(data, dict) else {}
    names = _configured_context_files(data)
    if not names and isinstance(integration, str):
        defaults = base / "agent-context-defaults.json"
        try:
            listing = read_file(defaults)[0]
        except ValueError:
            conflict("invalid-context-config", defaults, "The agent-context defaults cannot be read safely.")
            return [], conflicts
        try:
            agents = json.loads(listing or b"{}").get("agents", {})
        except (ValueError, AttributeError):
            agents = {}
        names = _configured_context_files({
            "context_file": agents.get(integration) if isinstance(agents, dict) else None
        })
    bad = [
        name for name in names
        if name.startswith("/") or re.match(r"[A-Za-z]:", name) or "\\" in name or ".." in name.split("/")
    ]
    for name in bad:
        conflict("invalid-context-config", config, f"The context file path leaves the project: {name}")
    if bad:
        return [], conflicts
    start = _get_str(data, "context_markers", "start") or DEFAULT_START
    end = _get_str(data, "context_markers", "end") or DEFAULT_END
    sections = []
    for name in names:
        path = repository / name
        try:
            raw, mode, mtime = read_file(path)
        except ValueError:
            conflict(
                "unsupported-file-type", path,
                "The context file, or a directory in its path, is not ordinary. Configure the real file instead.",
            )
            continue
        if raw is None:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        first = text.find(start)
        last = text.find(end, max(first, 0))
        if first == -1 and last == -1:
            continue
        if first == -1 or last < first:
            if mentions_reference(text):
                conflict("ambiguous-context-section", path, "The managed section markers do not pair up.")
            continue
        sections.append((path, raw, mode, mtime, first, last + len(end)))
    return sections, conflicts


def _validate_context_file(project_root: str, context_file: str) -> str | None:
    """Return an error message when the path escapes the project root."""
    if context_file.startswith("/") or re.match(r"^[A-Za-z]:", context_file):
        return (
            "agent-context: context files must be project-relative paths; "
            f"got '{context_file}'."
        )
    if "\\" in context_file:
        return (
            "agent-context: context files must not contain backslash separators; "
            f"got '{context_file}'."
        )
    if ".." in context_file.split("/"):
        return (
            "agent-context: context files must not contain '..' path segments; "
            f"got '{context_file}'."
        )
    root = Path(project_root).resolve()
    target = (root / context_file).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return (
            "agent-context: context file path resolves outside the project root; "
            f"got '{context_file}'."
        )
    return None


def _resolve_plan_path(project_root: str, automatic: bool) -> str:
    """Derive the plan path from the selected feature.

    An invocation feature wins in both modes. Only automatic projects without
    an invocation feature use feature.json, then the mtime fallback.
    """
    plan_path = ""
    feature_dir = os.environ.get("SPECIFY_FEATURE_DIRECTORY", "")
    feature_json = Path(project_root) / ".specify" / "feature.json"
    if not feature_dir and automatic and feature_json.is_file():
        try:
            with open(feature_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            value = data.get("feature_directory", "")
            feature_dir = value if isinstance(value, str) else ""
        except Exception:
            feature_dir = ""
    # Normalize backslashes (written by PS on Windows) before path ops.
    feature_dir = feature_dir.replace("\\", "/").rstrip("/")
    if feature_dir:
        # feature_directory may be relative or absolute (absolute paths
        # outside the project root are preserved as-is), including
        # drive-qualified paths (C:/...) written by PowerShell on Windows.
        if feature_dir.startswith("/") or re.match(r"^[A-Za-z]:/", feature_dir):
            candidate = Path(feature_dir) / "plan.md"
        else:
            candidate = Path(project_root) / feature_dir / "plan.md"
        if candidate.is_file():
            # Resolve symlinks before comparing so paths like /var/… vs
            # /private/var/… (macOS) are treated as equivalent.
            root = Path(project_root).resolve()
            resolved = candidate.resolve()
            try:
                plan_path = resolved.relative_to(root).as_posix()
            except ValueError:
                plan_path = resolved.as_posix()

    if not plan_path and automatic and not os.environ.get("SPECIFY_FEATURE_DIRECTORY"):
        root = Path(project_root).resolve()
        specs = root / "specs"

        def _resolved_rel(p: Path) -> Path | None:
            # Resolve symlinks before checking containment: relative_to() is
            # lexical and would otherwise accept a plan reached through a specs/
            # symlink that points outside the project, emitting an
            # in-project-looking path for an out-of-project file (or picking it
            # as "most recent").
            try:
                return p.resolve().relative_to(root)
            except (OSError, ValueError):
                return None

        # Recurse (rather than the old one-level specs/*/plan.md glob) so scoped
        # layouts created via SPECIFY_FEATURE_DIRECTORY, e.g.
        # specs/<scope>/<feature>/plan.md, are still discovered when
        # feature.json is absent (#3024). Mirrors the bash and PowerShell twins.
        candidates = []
        for p in specs.rglob("plan.md"):
            rel = _resolved_rel(p)
            if rel is not None:
                candidates.append((p, rel))
        candidates.sort(key=lambda pr: pr[0].stat().st_mtime, reverse=True)
        if candidates:
            plan_path = candidates[0][1].as_posix()
    return plan_path


def _build_section(marker_start: str, marker_end: str, plan_path: str) -> str:
    lines = [
        marker_start,
        "For additional context about technologies to be used, project structure,",
        "shell commands, and other important information, read the current plan",
    ]
    if plan_path:
        lines.append(f"at {plan_path}")
    lines.append(marker_end)
    return "\n".join(lines) + "\n"


def ensure_mdc_frontmatter(content: str) -> str:
    """Ensure ``.mdc`` content has YAML frontmatter with ``alwaysApply: true``.

    Cursor only auto-loads ``.mdc`` rule files that carry frontmatter with
    ``alwaysApply: true``. Prepend it when missing, or repair the value while
    preserving any existing frontmatter comments/formatting.
    """
    leading_ws = len(content) - len(content.lstrip())
    leading = content[:leading_ws]
    stripped = content[leading_ws:]

    if not stripped.startswith("---"):
        return "---\nalwaysApply: true\n---\n\n" + content

    match = re.match(
        r"^(---[ \t]*\r?\n)(.*?)(\r?\n---[ \t]*)(\r?\n|$)(.*)",
        stripped,
        re.DOTALL,
    )
    if not match:
        return "---\nalwaysApply: true\n---\n\n" + content

    opening, fm_text, closing, sep, rest = match.groups()
    newline = "\r\n" if "\r\n" in opening else "\n"

    if re.search(r"(?m)^[ \t]*alwaysApply[ \t]*:[ \t]*true[ \t]*(?:#.*)?$", fm_text):
        return content

    if re.search(r"(?m)^[ \t]*alwaysApply[ \t]*:", fm_text):
        fm_text = re.sub(
            r"(?m)^([ \t]*)alwaysApply[ \t]*:.*?([ \t]*(?:#.*)?)$",
            r"\1alwaysApply: true\2",
            fm_text,
            count=1,
        )
    elif fm_text.strip():
        fm_text = fm_text + newline + "alwaysApply: true"
    else:
        fm_text = "alwaysApply: true"

    return f"{leading}{opening}{fm_text}{closing}{sep}{rest}"


def _upsert_section(
    ctx_path: str, marker_start: str, marker_end: str, section: str
) -> None:
    """Insert or replace the managed section, then normalize and write."""
    if os.path.exists(ctx_path):
        with open(ctx_path, "r", encoding="utf-8-sig") as fh:
            content = fh.read()
        s = content.find(marker_start)
        e = content.find(marker_end, s if s != -1 else 0)
        if s != -1 and e != -1 and e > s:
            end_of_marker = e + len(marker_end)
            if end_of_marker < len(content) and content[end_of_marker] == "\r":
                end_of_marker += 1
            if end_of_marker < len(content) and content[end_of_marker] == "\n":
                end_of_marker += 1
            new_content = content[:s] + section + content[end_of_marker:]
        elif s != -1:
            new_content = content[:s] + section
        elif e != -1:
            end_of_marker = e + len(marker_end)
            if end_of_marker < len(content) and content[end_of_marker] == "\r":
                end_of_marker += 1
            if end_of_marker < len(content) and content[end_of_marker] == "\n":
                end_of_marker += 1
            new_content = section + content[end_of_marker:]
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            new_content = (content + "\n" + section) if content else section
    else:
        new_content = section

    new_content = new_content.replace("\r\n", "\n").replace("\r", "\n")
    if ctx_path.casefold().endswith(".mdc"):
        new_content = ensure_mdc_frontmatter(new_content)
    with open(ctx_path, "wb") as fh:
        fh.write(new_content.encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    core = _load_core_common()
    project_root = str(core.get_repo_root()) if core else os.getcwd()
    if core is None:
        start = Path(os.environ.get("SPECIFY_INIT_DIR") or project_root).resolve()
        if any(
            os.path.lexists(root / ".specify/project.json")
            for root in (start, *start.parents)
        ):
            _err("agent-context: external storage requires the installed core common.py helper.")
            return 1
    workspace_root = (
        core.get_workspace_root(Path(project_root)) if core else Path(project_root)
    )
    external = (Path(project_root) / ".specify/project.json").exists()
    ext_config = (
        f"{workspace_root}/.specify/extensions/agent-context/agent-context-config.yml"
    )

    if not os.path.isfile(ext_config):
        _err(f"agent-context: {ext_config} not found; nothing to do.")
        return 0

    try:
        import yaml
    except ImportError:
        _err(
            "agent-context: PyYAML is required to parse extension config but is "
            "not available in the current Python environment.\n"
            "  To resolve: pip install pyyaml (or install it into the environment "
            "used by python3).\n"
            "  Context file will not be updated until PyYAML is importable."
        )
        _err("agent-context: skipping update (see above for details).")
        return 0

    try:
        with open(ext_config, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        _err(
            f"agent-context: unable to parse {ext_config} ({exc}); "
            "cannot update context."
        )
        _err("agent-context: skipping update (see above for details).")
        return 0
    if not isinstance(data, dict):
        data = {}

    context_files = _collect_context_files(data, str(workspace_root))
    if not context_files:
        _err(
            "agent-context: context_files/context_file not set in extension config; "
            "nothing to do."
        )
        return 0

    for context_file in context_files:
        error = _validate_context_file(project_root, context_file)
        if error:
            _err(error)
            return 1

    marker_start = _get_str(data, "context_markers", "start") or DEFAULT_START
    marker_end = _get_str(data, "context_markers", "end") or DEFAULT_END

    plan_path = args[0] if args else ""
    # Without the core helper the policy file is unreadable, so the default (context) applies.
    automatic = (
        core is not None and core.feature_selection_mode(workspace_root) == "automatic"
    )
    if not plan_path and not automatic and not os.environ.get("SPECIFY_FEATURE_DIRECTORY"):
        _err(
            "agent-context: Feature directory not found. Pass a plan path or set "
            "SPECIFY_FEATURE_DIRECTORY for this command. This project uses "
            "feature_selection context, so the script ignores the saved feature "
            "and the newest plan."
        )
        return 1
    if external:
        if plan_path:
            print(f"[specify] Workspace: {workspace_root}", file=sys.stderr)
            plan_path = core.confined_workspace_path(workspace_root, plan_path).as_posix()
        else:
            plan_path = core.get_feature_paths(no_persist=True).impl_plan.as_posix()
    elif not plan_path:
        plan_path = _resolve_plan_path(project_root, automatic)

    section = _build_section(marker_start, marker_end, plan_path)

    for context_file in context_files:
        ctx_path = os.path.join(project_root, context_file)
        os.makedirs(os.path.dirname(ctx_path) or ".", exist_ok=True)
        _upsert_section(ctx_path, marker_start, marker_end, section)
        print(f"agent-context: updated {context_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
