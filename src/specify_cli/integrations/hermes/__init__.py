"""Hermes Agent integration — skills-based agent.

Hermes Agent (https://github.com/NousResearch/hermes-agent) is an open-source
AI agent framework by Nous Research.  It stores skills in
``~/.hermes/skills/`` (user-global) rather than a project-local directory.

Usage::

    specify init my-project --integration hermes
    specify init --here --integration hermes
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..base import IntegrationOption, SkillsIntegration, yaml_quote
from ..manifest import IntegrationManifest


class HermesIntegration(SkillsIntegration):
    """Integration for Hermes Agent skills.

    Hermes loads skills from ``~/.hermes/skills/`` (user home directory)
    rather than a project-local path.  Skills are installed directly to
    the global directory — no project-local copies are created since
    Hermes discovers them globally.  A project-local marker directory
    (``.hermes/skills/`` empty) is created so extension commands (e.g.
    git) can detect Hermes as an active integration. Uninstall removes
    the marker and unchanged owned global skill files. It preserves
    edited files and unowned content.
    """

    key = "hermes"
    config = {
        "name": "Hermes Agent",
        "folder": ".hermes/",
        "commands_subdir": "skills",
        "install_url": "https://github.com/NousResearch/hermes-agent",
        "requires_cli": True,
    }
    registrar_config = {
        "dir": "~/.hermes/skills",
        "detect_dir": ".hermes/skills",
        "format": "markdown",
        "args": "$ARGUMENTS",
        "extension": "/SKILL.md",
    }

    # -- Helpers -----------------------------------------------------------

    @staticmethod
    def global_skills_dir() -> Path:
        """Return ``~/.hermes/skills/`` — the global skills directory."""
        home = Path.home().resolve()
        root = home / ".hermes" / "skills"
        for directory in (root.parent, root):
            if directory.is_symlink():
                raise ValueError(f"Hermes skill directory must not be a symlink: {directory}")
        return root

    def _global_manifest(self) -> IntegrationManifest:
        """Load independent ownership for the shared skill directory."""
        root = self.global_skills_dir()
        manifest = IntegrationManifest(self.key, root)
        if manifest.metadata_root != root:
            raise ValueError(f"Hermes global skills cannot use an external workspace: {root}")
        path = manifest.file_path(Path(".specify/integrations") / manifest.manifest_path.name)
        if path.exists():
            return IntegrationManifest.load(self.key, root)
        return manifest

    def post_process_skill_content(self, content: str) -> str:
        """Resolve shared skill assets from the invoking project's workspace."""
        return self.add_workspace_note(super().post_process_skill_content(content))

    # -- Options -----------------------------------------------------------

    @classmethod
    def options(cls) -> list[IntegrationOption]:
        return [
            IntegrationOption(
                "--skills",
                is_flag=True,
                default=True,
                help="Install as agent skills (default for Hermes Agent)",
            ),
        ]

    # -- Setup -------------------------------------------------------------

    def setup(
        self,
        project_root: Path,
        manifest: IntegrationManifest,
        parsed_options: dict[str, Any] | None = None,
        **opts: Any,
    ) -> list[Path]:
        """Install command templates as global Hermes skills.

        Writes each skill directly to
        ``~/.hermes/skills/speckit-<name>/SKILL.md`` where Hermes
        discovers them at runtime.  No project-local SKILL.md copies are
        created — the global directory is the single source of truth.
        A project-local marker (``.hermes/skills/`` empty) is created
        so extension commands (e.g. git) can detect Hermes as an active
        integration.
        """
        from ...agents import CommandRegistrar

        templates = self.list_command_templates()
        if not templates:
            return []

        # Safety check: verify manifest project_root matches (standard pattern)
        project_root_resolved = project_root.resolve()
        if manifest.project_root != project_root_resolved:
            raise ValueError(
                f"manifest.project_root ({manifest.project_root}) does not match "
                f"project_root ({project_root_resolved})"
            )

        script_type = opts.get("script_type", "sh")
        arg_placeholder = (
            self.registrar_config.get("args", "$ARGUMENTS")
            if self.registrar_config
            else "$ARGUMENTS"
        )

        global_manifest = self._global_manifest()
        skill_paths = [
            Path(f"speckit-{source.stem.replace('.', '-')}") / "SKILL.md"
            for source in templates
        ]
        # Validate every destination before the first write.
        for relative in skill_paths:
            global_manifest.file_path(relative)
        marker = manifest.file_path(".hermes/skills")
        modified = set(global_manifest.check_modified())
        owned = global_manifest.files

        created: list[Path] = []

        for src_file, relative in zip(templates, skill_paths):
            skill_file = global_manifest.file_path(relative)
            if relative.as_posix() not in owned and skill_file.parent.exists():
                continue
            if skill_file.exists() and (
                relative.as_posix() in modified
                or global_manifest.is_recovered(relative)
            ):
                continue
            raw = src_file.read_text(encoding="utf-8")

            # Derive the skill name from the template stem
            command_name = src_file.stem  # e.g. "plan"
            skill_name = f"speckit-{command_name.replace('.', '-')}"

            # Parse frontmatter for description. Locate the closing ``---`` on
            # its own line rather than with ``raw.split("---", 2)`` — a bare
            # substring split stops at the first ``---`` *anywhere*, including
            # one inside a value such as ``description: Separate sections
            # with ---``, which truncates the frontmatter and drops later keys.
            # The block between the delimiters is parsed unstripped so trailing
            # newlines in literal (``|``) block scalars survive.
            frontmatter: dict[str, Any] = {}
            if raw.startswith("---"):
                fm_lines = raw.splitlines(keepends=True)
                fm_close = next(
                    (
                        i
                        for i in range(1, len(fm_lines))
                        if fm_lines[i].rstrip() == "---"
                    ),
                    None,
                )
                if fm_close is not None:
                    try:
                        fm = yaml.safe_load("".join(fm_lines[1:fm_close]))
                        if isinstance(fm, dict):
                            frontmatter = fm
                    except yaml.YAMLError:
                        pass

            # Process body through the standard template pipeline
            processed_body = self.process_template(
                raw,
                self.key,
                script_type,
                arg_placeholder,
                invoke_separator=self.invoke_separator,
            )
            # Strip the processed frontmatter — we rebuild it for skills.
            # Scan for the closing ``---`` on its own line rather than
            # ``split("---", 2)`` so a ``---`` embedded in a value does not
            # truncate the frontmatter and spill it into the body.
            if processed_body.startswith("---"):
                body_lines = processed_body.splitlines(keepends=True)
                close_idx = next(
                    (
                        i
                        for i in range(1, len(body_lines))
                        if body_lines[i].rstrip() == "---"
                    ),
                    None,
                )
                if close_idx is not None:
                    # Keep whatever trails the ``---`` marker on the closing
                    # line so the body stays byte-for-byte identical to
                    # ``split("---", 2)[2]`` for well-formed templates.
                    processed_body = body_lines[close_idx][3:] + "".join(
                        body_lines[close_idx + 1 :]
                    )

            # Select description
            description = frontmatter.get("description", "")
            if not description:
                description = f"Spec Kit: {command_name} workflow"
            policy: dict[str, Any] = {}
            CommandRegistrar.apply_invocation_policy(frontmatter, policy)

            # Build SKILL.md with manually formatted frontmatter. yaml_quote
            # escapes newlines and control characters that a plain quoted
            # f-string cannot carry.
            skill_content = (
                f"---\n"
                f"name: {yaml_quote(skill_name)}\n"
                f"description: {yaml_quote(description)}\n"
                f"compatibility: "
                f"{yaml_quote('Requires spec-kit project structure with .specify/ directory')}\n"
                f"{yaml.safe_dump(policy) if policy else ''}"
                f"metadata:\n"
                f"  author: {yaml_quote('github-spec-kit')}\n"
                f"  source: {yaml_quote('templates/commands/' + src_file.name)}\n"
                f"---\n"
                f"{processed_body}"
            )

            skill_content = self.post_process_skill_content(skill_content)

            normalized = skill_content.replace("\r\n", "\n")
            if not skill_file.exists() or skill_file.read_bytes() != normalized.encode("utf-8"):
                global_manifest.record_file(relative, normalized)
            created.append(skill_file)

        if global_manifest.files != owned:
            global_manifest.save()

        # Create project-local marker directory so extension commands
        # (e.g. git) can detect Hermes as an active integration.
        # Hermes itself ignores this directory — skills live globally.
        marker.mkdir(parents=True, exist_ok=True)

        return created

    # -- Uninstall ---------------------------------------------------------

    def teardown(
        self,
        project_root: Path,
        manifest: IntegrationManifest,
        *,
        force: bool = False,
    ) -> tuple[list[Path], list[Path]]:
        """Remove unchanged owned skills and the empty project marker.

        Force permits removal of edited owned files and final symlinks.
        It never permits cleanup through a symlinked parent.
        """
        global_manifest = self._global_manifest()
        for relative in global_manifest.files:
            global_manifest.file_path(relative, allow_symlink=True)
        local_skills_dir = manifest.file_path(".hermes/skills")

        removed, skipped = manifest.uninstall(project_root, force=force)
        global_removed, global_skipped = global_manifest.uninstall(force=force)
        removed.extend(global_removed)
        skipped.extend(global_skipped)

        if local_skills_dir.is_dir() and not any(local_skills_dir.iterdir()):
            local_skills_dir.rmdir()
            hermes_dir = local_skills_dir.parent
            if not any(hermes_dir.iterdir()):
                hermes_dir.rmdir()

        return removed, skipped

    # -- CLI dispatch ------------------------------------------------------

    def build_exec_args(
        self,
        prompt: str,
        *,
        model: str | None = None,
        output_json: bool = True,
        integration_args: Sequence[str] | None = None,
        integration_options: Mapping[str, Any] | None = None,
        project_root: Path | None = None,
    ) -> list[str] | None:
        """Build Hermes CLI invocation for programmatic dispatch.

        Uses ``hermes chat -Q -q`` for one-shot queries in quiet mode,
        mapping slash-command invocations to the appropriate skill-based
        dispatch.
        """
        self.validate_runtime_config(integration_args, integration_options)
        args = [self._resolve_executable(), "chat", "-Q"]

        # Operator-supplied SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS go here —
        # after the base command but before Spec Kit's canonical -m/--json/-s/-q
        # flags — so they can't displace or clobber them (mirrors opencode).
        self._apply_extra_args_env_var(args)

        if model:
            args.extend(["-m", model])
        if output_json:
            args.append("--json")

        # If prompt starts with a slash command, pass it directly
        # so Hermes can dispatch to the appropriate skill.
        if prompt.startswith("/"):
            command, _, remainder = prompt[1:].partition(" ")
            if command:
                args.extend(["-s", command])
                if remainder:
                    args.extend(["-q", remainder])
            else:
                args.extend(["-q", prompt])
        else:
            args.extend(["-q", prompt])

        return args
