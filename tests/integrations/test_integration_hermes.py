"""Tests for HermesIntegration.

Hermes is special among SkillsIntegration subclasses: it writes skills
to ``~/.hermes/skills/`` (global) rather than the project-local
``.hermes/skills/`` directory.  A project-local marker (empty directory)
is created so extension commands (e.g. git) can detect Hermes.

All tests that touch ``~/.hermes/`` use ``monkeypatch`` to isolate
``Path.home()`` to a temp directory so the test suite is hermetic and
non-destructive to a developer's real Hermes installation.
"""

from pathlib import Path

import pytest

from specify_cli.integrations import get_integration
from specify_cli.integrations.manifest import IntegrationManifest

from .test_integration_base_skills import SkillsIntegrationTests


def _fake_home(tmp_path: Path) -> Path:
    """Create and return an isolated home directory under *tmp_path*."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def _symlink(link: Path, target: Path) -> None:
    """Create a symlink when the platform permits it."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")


class TestHermesIntegration(SkillsIntegrationTests):
    KEY = "hermes"
    FOLDER = ".hermes/"
    COMMANDS_SUBDIR = "skills"
    REGISTRAR_DIR = "~/.hermes/skills"

    # -- Hermes-specific setup: skills go to ~/.hermes/skills/ -------------

    def test_setup_writes_to_global_skills_dir(self, tmp_path, monkeypatch):
        """Skills are written to ~/.hermes/skills/, not project-local."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        created = i.setup(tmp_path, m)
        skill_files = [f for f in created if "scripts" not in f.parts]

        assert len(skill_files) > 0, "No skill files were created"
        for f in skill_files:
            # Every skill file should be under ~/.hermes/skills/speckit-*/
            expected_prefix = str(home / ".hermes" / "skills")
            assert str(f).startswith(expected_prefix), (
                f"{f} is not under ~/.hermes/skills/"
            )

    def test_local_marker_dir_created(self, tmp_path, monkeypatch):
        """Project-local .hermes/skills/ should exist but be empty."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        i.setup(tmp_path, m)
        marker = tmp_path / ".hermes" / "skills"
        assert marker.is_dir(), "Marker directory was not created"
        # Should be empty (no SKILL.md files)
        children = list(marker.iterdir())
        assert children == [], f"Marker directory should be empty, got: {children}"

    # -- Override shared tests that assume project-local skills ------------

    def test_setup_writes_to_correct_directory(self, tmp_path, monkeypatch):
        """Override: Hermes writes to global, not project-local."""
        self.test_setup_writes_to_global_skills_dir(tmp_path, monkeypatch)

    def test_plan_skill_has_no_context_placeholder(self, tmp_path, monkeypatch):
        """The core plan skill must not carry a context-file placeholder —
        agent context files are owned by the opt-in agent-context extension."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        i.setup(tmp_path, m)
        # Find the plan skill in global ~/.hermes/skills/
        plan_file = home / ".hermes" / "skills" / "speckit-plan" / "SKILL.md"
        assert plan_file.exists(), f"Plan skill {plan_file} not created globally"
        content = plan_file.read_text(encoding="utf-8")
        assert "__CONTEXT_FILE__" not in content, (
            "Plan skill has unprocessed __CONTEXT_FILE__ placeholder"
        )

    def test_all_files_tracked_in_manifest(self, tmp_path, monkeypatch):
        """Persisted global ownership works with a fresh project manifest."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        created = integration.setup(tmp_path, IntegrationManifest(self.KEY, tmp_path))

        removed, skipped = integration.teardown(
            tmp_path, IntegrationManifest(self.KEY, tmp_path)
        )

        assert set(created) <= set(removed)
        assert skipped == []
        assert all(not path.exists() for path in created)

    def test_install_uninstall_roundtrip(self, tmp_path, monkeypatch):
        """Override: Hermes uninstall removes global skills + local marker."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        created = i.install(tmp_path, m)
        assert len(created) > 0
        m.save()
        # All SKILL.md files should exist globally
        for f in created:
            if "SKILL.md" in str(f):
                assert f.exists(), f"{f} does not exist"
        # Global skills are removed on teardown without needing force
        removed, skipped = i.teardown(tmp_path, m, force=False)
        for f in created:
            if "SKILL.md" in str(f):
                assert not f.exists(), f"{f} should have been removed"
        # Local marker should be gone
        assert not (tmp_path / ".hermes" / "skills").exists()

    def test_modified_file_survives_uninstall(self, tmp_path, monkeypatch):
        """User edits survive reinstall, upgrade, and default uninstall."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        template = tmp_path / "review.md"
        template.write_text("---\ndescription: Review\n---\nOriginal workflow.\n", encoding="utf-8")
        monkeypatch.setattr(integration, "list_command_templates", lambda: [template])
        manifest = IntegrationManifest(self.KEY, tmp_path)
        [skill] = integration.setup(tmp_path, manifest)
        skill.write_bytes(b"User workflow.\r\n")

        integration.setup(tmp_path, IntegrationManifest(self.KEY, tmp_path))
        template.write_text("---\ndescription: Review\n---\nUpgraded workflow.\n", encoding="utf-8")
        integration.setup(tmp_path, IntegrationManifest(self.KEY, tmp_path))
        removed, skipped = integration.teardown(tmp_path, manifest)

        assert skill.read_bytes() == b"User workflow.\r\n"
        assert skill in skipped
        assert skill not in removed

    def test_force_removes_only_owned_skills(self, tmp_path, monkeypatch):
        """Force removes edited owned files but preserves other skill content."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        manifest = IntegrationManifest(self.KEY, tmp_path)
        created = integration.setup(tmp_path, manifest)
        skill = created[0]
        skill.write_bytes(b"User edit.\n")
        companion = skill.parent / "notes.txt"
        companion.write_bytes(b"Private notes.\n")
        foreign = integration.global_skills_dir() / "speckit-private" / "SKILL.md"
        foreign.parent.mkdir()
        foreign.write_bytes(b"Private skill.\n")

        removed, skipped = integration.teardown(tmp_path, manifest, force=True)

        assert skill in removed
        assert not skill.exists()
        assert skipped == []
        assert companion.read_bytes() == b"Private notes.\n"
        assert foreign.read_bytes() == b"Private skill.\n"

    @pytest.mark.parametrize("name", ["speckit-plan", "speckit-private", "other-tool"])
    @pytest.mark.parametrize("force", [False, True])
    def test_pre_existing_skills_not_removed(self, tmp_path, monkeypatch, name, force):
        """Unowned skills survive reinstall and uninstall, even with a core name."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        foreign = integration.global_skills_dir() / name / "SKILL.md"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b"Private workflow.\r\n")
        manifest = IntegrationManifest(self.KEY, tmp_path)

        integration.setup(tmp_path, manifest)
        integration.setup(tmp_path, IntegrationManifest(self.KEY, tmp_path))
        removed, skipped = integration.teardown(tmp_path, manifest, force=force)

        assert foreign.read_bytes() == b"Private workflow.\r\n"
        assert foreign not in removed
        assert foreign not in skipped

    @pytest.mark.parametrize("with_notes", [False, True])
    def test_unowned_core_skill_directory_not_claimed(self, tmp_path, monkeypatch, with_notes):
        """An unowned core skill directory stays unowned, even when it is empty."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        template = tmp_path / "review.md"
        template.write_text("---\ndescription: Review\n---\nWorkflow.\n", encoding="utf-8")
        monkeypatch.setattr(integration, "list_command_templates", lambda: [template])
        directory = integration.global_skills_dir() / "speckit-review"
        directory.mkdir(parents=True)
        if with_notes:
            (directory / "notes.txt").write_bytes(b"Private notes.\n")
        manifest = IntegrationManifest(self.KEY, tmp_path)

        integration.setup(tmp_path, manifest)
        integration.teardown(tmp_path, manifest, force=True)

        assert directory.is_dir()
        assert not (directory / "SKILL.md").exists()
        if with_notes:
            assert (directory / "notes.txt").read_bytes() == b"Private notes.\n"

    def test_unchanged_owned_skill_upgrades_across_projects(self, tmp_path, monkeypatch):
        """Shared ownership updates unchanged skills without needless rewrites."""
        import yaml

        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        template = tmp_path / "review.md"
        template.write_text(
            "---\ndescription: Review\ndisable-model-invocation: true\n---\nOriginal workflow.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(integration, "list_command_templates", lambda: [template])
        first = tmp_path / "first"
        first.mkdir()
        [skill] = integration.setup(first, IntegrationManifest(self.KEY, first))
        previous_mtime = skill.stat().st_mtime_ns
        second = tmp_path / "second"
        second.mkdir()
        manifest = IntegrationManifest(self.KEY, second)
        integration.setup(second, manifest)
        assert skill.stat().st_mtime_ns == previous_mtime

        template.write_text(
            "---\ndescription: Review\ndisable-model-invocation: false\n---\nUpdated workflow.\n",
            encoding="utf-8",
        )
        integration.setup(second, manifest)
        content = skill.read_text(encoding="utf-8")
        assert "Updated workflow." in content
        assert "Original workflow." not in content
        assert yaml.safe_load(content.split("---", 2)[1])["disable-model-invocation"] is False

        removed, skipped = integration.teardown(second, manifest)
        assert removed == [skill]
        assert skipped == []
        assert not skill.exists()

    @pytest.mark.parametrize("action", ["setup", "teardown"])
    def test_marker_symlink_refuses_writes_and_cleanup(self, tmp_path, monkeypatch, action):
        """The project marker cannot redirect setup or teardown outside the project."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        external = tmp_path / "external"
        external.mkdir()
        note = external / "notes.txt"
        note.write_bytes(b"External notes.\n")
        _symlink(tmp_path / ".hermes", external)

        with pytest.raises(ValueError, match="symlink"):
            getattr(integration, action)(tmp_path, IntegrationManifest(self.KEY, tmp_path))

        assert note.read_bytes() == b"External notes.\n"
        assert not integration.global_skills_dir().exists()
        assert (tmp_path / ".hermes").is_symlink()

    @pytest.mark.parametrize("link_kind", ["directory", "file"])
    def test_setup_refuses_descendant_symlinks_before_writes(
        self, tmp_path, monkeypatch, link_kind
    ):
        """A symlinked destination cannot write external bytes or earlier skills."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        safe = tmp_path / "safe.md"
        unsafe = tmp_path / "unsafe.md"
        for template in (safe, unsafe):
            template.write_text("---\ndescription: Review\n---\nWorkflow.\n", encoding="utf-8")
        monkeypatch.setattr(integration, "list_command_templates", lambda: [safe, unsafe])
        external = tmp_path / "external"
        external.mkdir()
        target = external / "SKILL.md"
        target.write_bytes(b"External workflow.\n")
        skill_dir = integration.global_skills_dir() / "speckit-unsafe"
        skill_dir.parent.mkdir(parents=True)
        if link_kind == "directory":
            _symlink(skill_dir, external)
        else:
            skill_dir.mkdir()
            _symlink(skill_dir / "SKILL.md", target)

        with pytest.raises(ValueError, match="symlink"):
            integration.setup(tmp_path, IntegrationManifest(self.KEY, tmp_path))

        assert target.read_bytes() == b"External workflow.\n"
        assert not (skill_dir.parent / "speckit-safe").exists()
        assert not (tmp_path / ".hermes").exists()

    @pytest.mark.parametrize("force", [False, True])
    def test_teardown_refuses_symlinked_parent_before_cleanup(self, tmp_path, monkeypatch, force):
        """A tampered skill directory cannot cause external or partial cleanup."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        manifest = IntegrationManifest(self.KEY, tmp_path)
        created = integration.setup(tmp_path, manifest)
        skill = created[-1]
        external = tmp_path / "external"
        external.mkdir()
        target = external / "SKILL.md"
        target.write_bytes(b"External workflow.\n")
        skill.unlink()
        skill.parent.rmdir()
        _symlink(skill.parent, external)

        with pytest.raises(ValueError, match="symlink"):
            integration.teardown(tmp_path, manifest, force=force)

        assert target.read_bytes() == b"External workflow.\n"
        assert skill.parent.is_symlink()
        assert created[0].is_file()
        assert (tmp_path / ".hermes" / "skills").is_dir()

    @pytest.mark.parametrize("force", [False, True])
    def test_teardown_never_follows_owned_file_symlink(self, tmp_path, monkeypatch, force):
        """Force can unlink an owned file symlink, but cannot change its target."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        manifest = IntegrationManifest(self.KEY, tmp_path)
        created = integration.setup(tmp_path, manifest)
        skill = created[0]
        target = tmp_path / "external.md"
        target.write_bytes(b"External workflow.\n")
        skill.unlink()
        _symlink(skill, target)

        removed, skipped = integration.teardown(tmp_path, manifest, force=force)

        assert target.read_bytes() == b"External workflow.\n"
        if force:
            assert skill in removed
            assert not skill.is_symlink()
        else:
            assert skill in skipped
            assert skill.is_symlink()

    def test_teardown_preserves_unowned_project_marker_content(self, tmp_path, monkeypatch):
        """The project marker directory is shared with user content."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)
        integration = get_integration(self.KEY)
        manifest = IntegrationManifest(self.KEY, tmp_path)
        integration.setup(tmp_path, manifest)
        note = tmp_path / ".hermes" / "skills" / "notes.txt"
        note.write_bytes(b"Project notes.\n")

        integration.teardown(tmp_path, manifest)

        assert note.read_bytes() == b"Project notes.\n"

    def test_hook_sections_explain_dotted_command_conversion(self, tmp_path, monkeypatch):
        """Override: Hermes skills live in global ~/.hermes/skills/."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        i.setup(tmp_path, m)
        specify_skill = home / ".hermes" / "skills" / "speckit-specify" / "SKILL.md"
        assert specify_skill.exists()
        content = specify_skill.read_text(encoding="utf-8")
        assert "replace dots" in content, (
            "speckit-specify should explain dotted hook command conversion"
        )
        assert content.count("replace dots") == content.count(
            "- For each executable hook, output the following"
        )

    def test_complete_file_inventory_sh(self, tmp_path, monkeypatch):
        """Override: Hermes init produces no local SKILL.md files,
        only the empty .hermes/skills/ marker."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        from typer.testing import CliRunner
        from specify_cli import app

        project = tmp_path / f"inventory-sh-{self.KEY}"
        project.mkdir()
        old_cwd = Path.cwd()
        import os
        try:
            os.chdir(project)
            result = CliRunner().invoke(app, [
                "init", "--here", "--integration", self.KEY,
                "--script", "sh", "--ignore-agent-tools",
            ], catch_exceptions=False)
        finally:
            os.chdir(old_cwd)
        assert result.exit_code == 0, f"init failed: {result.output}"
        actual = sorted(
            p.relative_to(project).as_posix()
            for p in project.rglob("*") if p.is_file()
        )
        # Ensure no core .hermes/skills/speckit-*/SKILL.md in project dir
        # (extension-installed skills like agent-context-update may appear)
        hermes_skill_files = [
            f for f in actual
            if f.startswith(".hermes/skills/speckit-")
            and "agent-context" not in f
        ]
        assert hermes_skill_files == [], (
            f"Expected no local core SKILL.md files, found: {hermes_skill_files}"
        )
        # Ensure the marker exists (empty dir won't appear in file listing)
        assert (project / ".hermes" / "skills").is_dir()

    def test_complete_file_inventory_ps(self, tmp_path, monkeypatch):
        """Override: Same as sh variant but for PowerShell script type."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        from typer.testing import CliRunner
        from specify_cli import app

        project = tmp_path / f"inventory-ps-{self.KEY}"
        project.mkdir()
        old_cwd = Path.cwd()
        import os
        try:
            os.chdir(project)
            result = CliRunner().invoke(app, [
                "init", "--here", "--integration", self.KEY,
                "--script", "ps", "--ignore-agent-tools",
            ], catch_exceptions=False)
        finally:
            os.chdir(old_cwd)
        assert result.exit_code == 0, f"init failed: {result.output}"
        actual = sorted(
            p.relative_to(project).as_posix()
            for p in project.rglob("*") if p.is_file()
        )
        # Ensure no core .hermes/skills/speckit-*/SKILL.md in project dir
        # (extension-installed skills like agent-context-update may appear)
        hermes_skill_files = [
            f for f in actual
            if f.startswith(".hermes/skills/speckit-")
            and "agent-context" not in f
        ]
        assert hermes_skill_files == [], (
            f"Expected no local core SKILL.md files, found: {hermes_skill_files}"
        )
        assert (project / ".hermes" / "skills").is_dir()

    def test_install_uninstall_cleanup(self, tmp_path, monkeypatch):
        """Verify global skills are cleaned and local marker is removed."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        created = i.setup(tmp_path, m)

        # Verify global skills exist
        global_skills = [
            f for f in created
            if "SKILL.md" in str(f)
            and str(f).startswith(str(home / ".hermes"))
        ]
        assert len(global_skills) > 0
        for f in global_skills:
            assert f.exists()

        # Verify local marker exists
        assert (tmp_path / ".hermes" / "skills").is_dir()

        # Teardown — global skills removed without needing force=True
        removed, skipped = i.teardown(tmp_path, m, force=False)

        # Global skills removed
        for f in global_skills:
            assert not f.exists(), f"{f} should have been removed"

        # Local marker removed
        assert not (tmp_path / ".hermes" / "skills").exists(), (
            "Local marker should be removed on teardown"
        )


class TestHermesInitFlow:
    """--integration hermes creates expected files."""

    def test_integration_hermes_creates_global_skills(self, tmp_path, monkeypatch):
        """--integration hermes should create global skills and a local marker."""
        home = _fake_home(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: home)

        from typer.testing import CliRunner
        from specify_cli import app

        runner = CliRunner()
        target = tmp_path / "test-proj"
        result = runner.invoke(app, [
            "init", str(target),
            "--integration", "hermes",
            "--ignore-agent-tools",
            "--script", "sh",
        ])

        assert result.exit_code == 0, f"init --integration hermes failed: {result.output}"
        # Skills should be in global ~/.hermes/skills/
        assert (home / ".hermes" / "skills" / "speckit-plan" / "SKILL.md").exists()
        # Local marker should exist
        assert (target / ".hermes" / "skills").is_dir()
        # No core SKILL.md files in project-local dir
        # (extension-installed skills like agent-context-update may appear)
        local_skills = [
            d for d in (target / ".hermes" / "skills").iterdir()
            if "agent-context" not in d.name
        ]
        assert local_skills == [], f"Local skills dir should be empty, got: {local_skills}"


class TestHermesBuildExecArgs:
    """CLI dispatch argv, including the operator extra-args env hook."""

    def test_build_exec_args_default_shape(self):
        i = get_integration("hermes")
        assert i.build_exec_args("/speckit-plan hi", output_json=True) == [
            "hermes", "chat", "-Q", "--json", "-s", "speckit-plan", "-q", "hi",
        ]

    def test_build_exec_args_honors_extra_args(self, monkeypatch):
        """SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS is injected before the
        canonical -m/--json/-s/-q flags (same env hook as codex/opencode/
        devin; hermes previously skipped _apply_extra_args_env_var entirely).
        """
        monkeypatch.setenv(
            "SPECKIT_INTEGRATION_HERMES_EXTRA_ARGS", "--temperature 0.2"
        )
        i = get_integration("hermes")
        args = i.build_exec_args("/speckit-plan hi", output_json=True)
        assert args == [
            "hermes", "chat", "-Q", "--temperature", "0.2",
            "--json", "-s", "speckit-plan", "-q", "hi",
        ]
        # Injected before the canonical flags so it can't displace them.
        assert args.index("--temperature") < args.index("--json")
        assert args.index("--temperature") < args.index("-s")

    def test_build_exec_args_honors_executable_override(self, monkeypatch):
        monkeypatch.setenv(
            "SPECKIT_INTEGRATION_HERMES_EXECUTABLE", "/custom/hermes"
        )
        i = get_integration("hermes")
        assert i.build_exec_args("/speckit-plan hi")[0] == "/custom/hermes"
