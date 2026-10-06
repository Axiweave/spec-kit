"""Command-focused workflow tests."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_migration_git_identity(monkeypatch):
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Migration Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "migration@example.test")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _resource_command(relative: str) -> str:
    script = (
        "import os, sys; from pathlib import Path; "
        f"print((Path(os.environ['SPECKIT_WORKFLOW_DIR']) / {relative!r}).read_text(encoding='utf-8'), end=''); "
        f"print((Path(sys.argv[1]) / {relative!r}).read_text(encoding='utf-8'), end=''); "
        "print(Path.cwd())"
    )
    argv = [sys.executable, "-X", "utf8", "-c", script, "{{ context.workflow_dir }}"]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)



class TestWorkflowJsonOutput:
    """Test the --json machine-readable output for run/resume/status."""

    _WF = """
schema_version: "1.0"
workflow:
  id: "json-wf"
  name: "JSON WF"
  version: "1.0.0"
steps:
  - id: ask
    type: gate
    message: "Review"
    options: [approve, reject]
  - id: after
    type: shell
    run: "echo done"
"""

    def _write_wf(self, project_dir, text, name):
        path = project_dir / f"{name}.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def _invoke(self, project_dir, args):
        from typer.testing import CliRunner
        from unittest.mock import patch
        from specify_cli import app

        runner = CliRunner()
        with patch.object(Path, "cwd", return_value=project_dir):
            return runner.invoke(app, args, catch_exceptions=False)

    def test_resume_json(self, project_dir):
        wf = self._write_wf(project_dir, self._WF, "gated3")
        rid = json.loads(
            self._invoke(project_dir, ["workflow", "run", str(wf), "--json"]).stdout
        )["run_id"]
        # Non-interactive resume re-runs the gate, which pauses again.
        resumed = json.loads(
            self._invoke(project_dir, ["workflow", "resume", rid, "--json"]).stdout
        )
        assert resumed["run_id"] == rid
        assert resumed["status"] == "paused"



class TestResumeWithInputs:
    """Test that `workflow resume` can accept updated workflow inputs."""

    _WF_NUM = """
schema_version: "1.0"
workflow:
  id: "resume-num-wf"
  name: "Resume Num WF"
  version: "1.0.0"
inputs:
  count:
    type: number
    default: 1
steps:
  - id: gate
    type: gate
    message: "Review"
    options: [approve, reject]
"""

    def _engine(self, project_dir):
        from specify_cli.workflows.engine import WorkflowEngine
        return WorkflowEngine(project_dir)

    def test_cli_resume_input_invalid_format_errors(self, project_dir):
        from typer.testing import CliRunner
        from unittest.mock import patch
        from specify_cli import app
        from specify_cli.workflows.engine import WorkflowDefinition

        definition = WorkflowDefinition.from_string(self._WF_NUM)
        state = self._engine(project_dir).execute(definition)

        runner = CliRunner()
        with patch.object(Path, "cwd", return_value=project_dir):
            result = runner.invoke(
                app, ["workflow", "resume", state.run_id, "--input", "bogus"]
            )
        assert result.exit_code == 1
        assert "Invalid input format" in result.stdout



class TestWorkflowStepStartProgressLine:
    """The `run`/`resume` step-progress line must render the step id literally.

    The line is built as `  ▸ [<id>] <label> …`, so Rich parsed the bracketed id
    as a style tag: it silently swallowed the id (the only identifying content
    on the line), applied it as formatting when the id happened to be a real
    style like `bold`, and raised MarkupError — failing the whole run — when the
    id formed a closing tag such as `/`. `validate_workflow` places no charset
    restriction on step ids, so all of these are accepted workflows.
    """

    def test_resume_progress_line_shows_step_id(self, tmp_path, monkeypatch):
        """`workflow resume` installs its own copy of the same callback, so it
        needs independent coverage — a one-line fix would miss the twin."""
        import json as _json

        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(tmp_path)
        path = tmp_path / "wf.yml"
        path.write_text(
            'schema_version: "1.0"\n'
            "workflow:\n"
            '  id: "probe-resume"\n'
            '  name: "Probe"\n'
            '  version: "1.0.0"\n'
            "steps:\n"
            "  - id: boom\n"
            "    type: shell\n"
            '    run: "exit 1"\n',
            encoding="utf-8",
        )
        runner = CliRunner()
        first = runner.invoke(app, ["workflow", "run", str(path), "--json"])
        run_id = _json.loads(first.stdout).get("run_id")
        assert run_id

        resumed = runner.invoke(app, ["workflow", "resume", run_id])
        assert "[boom]" in resumed.stdout



class TestWorkflowRunExitCodes:
    """CLI-level tests for the run/resume process exit codes."""

    _WF_FAIL = """
schema_version: "1.0"
workflow:
  id: "exit-fail"
  name: "Exit Fail"
  version: "1.0.0"
steps:
  - id: boom
    type: shell
    run: "exit 1"
"""

    def _write(self, tmp_path, content):
        path = tmp_path / "wf.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_resume_failed_run_exits_nonzero(self, tmp_path, monkeypatch):
        # End-to-end coverage for the `workflow resume` exit-code mapping:
        # resuming a run whose outcome is still `failed` must exit non-zero,
        # mirroring `workflow run`. Resume re-executes the failed step, which
        # fails again, so the resumed outcome stays `failed`.
        import json as _json
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(tmp_path)
        (tmp_path / ".specify").mkdir()  # `workflow resume` requires a project
        runner = CliRunner()
        run = runner.invoke(
            app,
            ["workflow", "run", str(self._write(tmp_path, self._WF_FAIL)), "--json"],
        )
        assert run.exit_code == 1, run.stdout
        run_id = _json.loads(run.stdout)["run_id"]

        resumed = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert resumed.exit_code == 1, resumed.stdout
        payload = _json.loads(resumed.stdout)
        assert payload["status"] == "failed"



class TestWorkflowCliAlignment:
    """CLI alignment with extension/preset commands (#2342)."""

    _GATED_WORKFLOW_YAML = """
schema_version: "1.0"
workflow:
  id: "gated-wf"
  name: "Gated Workflow"
  version: "1.0.0"
steps:
  - id: ask
    type: gate
    message: "Review"
    options: [approve, reject]
"""

    def _install_and_run_gated(self, runner, app, project_dir):
        """Install a gate-step workflow and run it to a paused state.

        Returns the run_id. The gate step pauses without any interactive
        input, giving a resumable run tied to an installed workflow ID.
        """
        src = project_dir / "gated-src"
        src.mkdir(exist_ok=True)
        (src / "workflow.yml").write_text(self._GATED_WORKFLOW_YAML, encoding="utf-8")
        result = runner.invoke(app, ["workflow", "add", str(src), "--dev"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workflow", "run", "gated-wf", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "paused"
        return payload["run_id"]

    def test_resume_blocks_when_installed_workflow_disabled(
        self, project_dir, monkeypatch
    ):
        """A run started from an installed workflow must not resume once
        that workflow is disabled. engine.resume() replays the persisted
        run directly from disk with no registry awareness at all, so the
        installed workflow's origin (id + owning registry root) is
        persisted at run start and re-checked against the registry's
        *current* state before resuming, mirroring `workflow run`'s
        disabled guard."""
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        result = runner.invoke(app, ["workflow", "disable", "gated-wf"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert "disabled" in result.output

        # Re-enabling must unblock the exact same run.
        result = runner.invoke(app, ["workflow", "enable", "gated-wf"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert result.exit_code == 0, result.output
        resumed = json.loads(result.stdout)
        assert resumed["run_id"] == run_id

    def test_resume_rejects_corrupted_registry_entry(
        self, project_dir, monkeypatch
    ):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.catalog import WorkflowRegistry

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        registry = WorkflowRegistry(project_dir)
        registry.data["workflows"]["gated-wf"] = "corrupted"
        registry.save()

        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert "corrupted" in result.output

    def test_resume_preload_io_error_is_reported_cleanly(
        self, project_dir, monkeypatch
    ):
        from unittest.mock import patch
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.engine import RunState

        monkeypatch.chdir(project_dir)
        with patch.object(
            RunState, "load", side_effect=OSError("permission [denied]")
        ):
            result = CliRunner().invoke(
                app, ["workflow", "resume", "unreadable-run"]
            )

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Resume failed" in result.output
        assert "permission [denied]" in result.output

    @pytest.mark.parametrize("malformation", ["non-object", "missing-run-id"])
    def test_resume_preload_rejects_malformed_state_cleanly(
        self, project_dir, monkeypatch, malformation
    ):
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)
        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )

        if malformation == "non-object":
            state_path.write_text("[]", encoding="utf-8")
        else:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            data.pop("run_id")
            state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "resume", run_id])

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Invalid run state" in result.output

    def test_resume_rejects_out_of_range_current_step_index(
        self, project_dir, monkeypatch
    ):
        """An out-of-range positive index must fail cleanly, not silently
        complete the run with no steps executed.

        ``resume()`` slices ``definition.steps[state.current_step_index:]``;
        for any index >= len(steps) that slice is an empty list, so the run
        would otherwise finish with status "completed" having executed
        nothing.
        """
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)
        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data["current_step_index"] = 5
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "resume", run_id])

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Invalid run state" in result.output
        assert "out of range" in result.output

        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["status"] == "paused"

    def test_resume_legacy_run_respects_current_disabled_state(
        self, project_dir, monkeypatch
    ):
        """Legacy runs infer same-project registry ownership before resume."""
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data.pop("installed_workflow_id", None)
        data.pop("installed_registry_root", None)
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "disable", "gated-wf"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert result.exit_code != 0
        assert "disabled" in result.output

    def test_resume_migrates_legacy_installed_origin_metadata(
        self, project_dir, monkeypatch
    ):
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data.pop("installed_workflow_id", None)
        data.pop("installed_registry_root", None)
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert result.exit_code == 0, result.output

        migrated = json.loads(state_path.read_text(encoding="utf-8"))
        assert migrated["installed_workflow_id"] == "gated-wf"
        assert migrated["installed_registry_root"] is None

    def test_resume_blocks_after_project_moved_following_disable(
        self, temp_dir, monkeypatch
    ):
        """Renaming/moving the entire project after starting a run must not
        let a subsequent disable-then-resume bypass the guard. Persisting
        the run's *creation-time absolute* project path would make resume
        open a now-nonexistent old root (WorkflowRegistry falls back to an
        empty default there), missing the disabled entry that actually
        lives in the *current* (moved) project's registry. The common,
        same-project case must instead re-derive the owning root from the
        project's current location on every resume."""
        from typer.testing import CliRunner
        from specify_cli import app

        project_v1 = temp_dir / "project-v1"
        (project_v1 / ".specify" / "workflows").mkdir(parents=True)
        monkeypatch.chdir(project_v1)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_v1)

        project_v2 = temp_dir / "project-v2"
        monkeypatch.chdir(temp_dir)
        shutil.move(str(project_v1), str(project_v2))
        monkeypatch.chdir(project_v2)

        result = runner.invoke(app, ["workflow", "disable", "gated-wf"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert "disabled" in result.output

    def test_resume_after_project_moved_still_works_when_enabled(
        self, temp_dir, monkeypatch
    ):
        """The inverse of the move regression: an enabled workflow's run
        must still resume normally after the project is moved -- the
        current-project fallback must not itself block legitimate
        resumes."""
        from typer.testing import CliRunner
        from specify_cli import app

        project_v1 = temp_dir / "project-v1-ok"
        (project_v1 / ".specify" / "workflows").mkdir(parents=True)
        monkeypatch.chdir(project_v1)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_v1)

        project_v2 = temp_dir / "project-v2-ok"
        monkeypatch.chdir(temp_dir)
        shutil.move(str(project_v1), str(project_v2))
        monkeypatch.chdir(project_v2)

        result = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize("legacy_state", [False, True])
    def test_cross_project_resources_follow_owner_move_and_relink(
        self, tmp_path, monkeypatch, legacy_state
    ):
        import yaml
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.project.move import commit_move, prepare_move
        from specify_cli.workflows.engine import RunState

        owner, consumer = tmp_path / "owner", tmp_path / "consumer"
        owner_workspace = tmp_path / "owner workspace"
        consumer_workspace = tmp_path / "consumer workspace"
        for repo in (owner, consumer):
            (repo / ".specify/workflows").mkdir(parents=True)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
        monkeypatch.chdir(owner)
        source = tmp_path / "package"
        source.mkdir()
        (source / "message.txt").write_text("owner resource\n", encoding="utf-8")
        workflow = yaml.safe_load(self._GATED_WORKFLOW_YAML)
        workflow["inputs"] = {
            name: {"type": "string", "default": ""} for name in ("verdict", "second_verdict")
        }
        resource = {
            "type": "shell",
            "run": _resource_command("message.txt"),
        }
        workflow["steps"][0]["verdict_input"] = "verdict"
        workflow["steps"].extend([
            {"id": "first-resource", **resource},
            {"id": "second-review", "type": "gate", "message": "Review again", "verdict_input": "second_verdict"},
            {"id": "second-resource", **resource},
        ])
        (source / "workflow.yml").write_text(yaml.safe_dump(workflow), encoding="utf-8")
        runner = CliRunner()
        installed = runner.invoke(app, ["workflow", "add", str(source), "--dev"])
        assert installed.exit_code == 0, installed.output
        monkeypatch.chdir(consumer)
        installed_path = owner / ".specify/workflows/gated-wf/workflow.yml"
        started = runner.invoke(app, ["workflow", "run", str(installed_path), "--json"])
        assert started.exit_code == 0, started.output
        run_id = json.loads(started.stdout)["run_id"]
        state_path = consumer / ".specify/workflows/runs" / run_id / "state.json"
        if legacy_state:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["workflow_dir"] = str(installed_path.parent)
            state_path.write_text(json.dumps(state), encoding="utf-8")
        commit_move(prepare_move(consumer, consumer_workspace))
        commit_move(prepare_move(owner, owner_workspace))
        resumed = runner.invoke(app, ["workflow", "resume", run_id, "--input", "verdict=approve", "--json"])
        assert resumed.exit_code == 0, resumed.output
        assert json.loads(resumed.stdout)["status"] == "paused"
        first = RunState.load(run_id, consumer_workspace)
        expected = ["owner resource", "owner resource", str(consumer)]
        assert first.step_results["first-resource"]["output"]["stdout"].splitlines() == expected
        relocated = tmp_path / "relocated owner workspace"
        shutil.move(owner_workspace, relocated)
        monkeypatch.chdir(owner)
        linked = runner.invoke(app, ["project", "link", str(relocated)])
        assert linked.exit_code == 0, linked.output
        monkeypatch.chdir(consumer)
        finished = runner.invoke(app, [
            "workflow", "resume", run_id, "--input", "second_verdict=approve", "--json",
        ])
        assert finished.exit_code == 0, finished.output
        assert json.loads(finished.stdout)["status"] == "completed"
        final = RunState.load(run_id, consumer_workspace)
        assert final.step_results["second-resource"]["output"]["stdout"].splitlines() == expected
        assert final.step_results["first-resource"] == first.step_results["first-resource"]
        assert final.workflow_dir == str(relocated / ".specify/workflows/gated-wf")

    def test_resume_checks_migrated_cross_project_owner(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.project.move import commit_move, prepare_move

        owner = tmp_path / "owner"
        consumer = tmp_path / "consumer"
        (owner / ".specify/workflows").mkdir(parents=True)
        (consumer / ".specify").mkdir(parents=True)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
        monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
        monkeypatch.chdir(owner)
        runner = CliRunner()
        self._install_and_run_gated(runner, app, owner)
        monkeypatch.chdir(consumer)
        source = owner / ".specify/workflows/gated-wf/workflow.yml"
        started = runner.invoke(app, ["workflow", "run", str(source), "--json"])
        assert started.exit_code == 0, started.output
        run_id = json.loads(started.stdout)["run_id"]
        commit_move(prepare_move(owner, tmp_path / "workspace"))
        monkeypatch.chdir(owner)
        disabled = runner.invoke(app, ["workflow", "disable", "gated-wf"])
        assert disabled.exit_code == 0, disabled.output
        monkeypatch.chdir(consumer)
        resumed = runner.invoke(app, ["workflow", "resume", run_id])
        assert resumed.exit_code != 0
        assert "disabled" in resumed.output
        state = consumer / ".specify/workflows/runs" / run_id / "state.json"
        assert json.loads(state.read_text(encoding="utf-8"))["status"] == "paused"

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
    def test_resume_respects_cross_project_registry_root(
        self, temp_dir, monkeypatch
    ):
        """A run started via a direct workflow.yml path belonging to a
        different project than the cwd used for `workflow run`/`workflow
        resume` must still gate resuming on *that* owning project's
        registry, not the cwd project's (which has no entry for this ID
        at all). This is the genuine cross-project case that must remain
        unaffected by only special-casing the common same-project one."""
        from typer.testing import CliRunner
        from specify_cli import app

        owner_project = temp_dir / "owner-project"
        (owner_project / ".specify" / "workflows").mkdir(parents=True)
        monkeypatch.chdir(owner_project)
        runner = CliRunner()
        src = owner_project / "gated-src"
        src.mkdir()
        (src / "workflow.yml").write_text(self._GATED_WORKFLOW_YAML, encoding="utf-8")
        result = runner.invoke(app, ["workflow", "add", str(src), "--dev"])
        assert result.exit_code == 0, result.output

        unrelated_cwd = temp_dir / "unrelated-cwd"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        owner_alias = temp_dir / "owner-project-alias"
        owner_alias.symlink_to(owner_project, target_is_directory=True)
        target = owner_alias / ".specify" / "workflows" / "gated-wf" / "workflow.yml"
        result = runner.invoke(app, ["workflow", "run", str(target), "--json"])
        assert result.exit_code == 0, result.output
        run_id = json.loads(result.stdout)["run_id"]
        state_path = (
            unrelated_cwd
            / ".specify"
            / "workflows"
            / "runs"
            / run_id
            / "state.json"
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["installed_registry_root"] == str(owner_project.resolve())

        monkeypatch.chdir(owner_project)
        result = runner.invoke(app, ["workflow", "disable", "gated-wf"])
        assert result.exit_code == 0, result.output

        # Resume must run from unrelated_cwd (where this run's own
        # state.json actually lives) yet still be blocked by the owner
        # project's disabled entry.
        monkeypatch.chdir(unrelated_cwd)
        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert "disabled" in result.output

    def test_resume_rejects_missing_cross_project_owner_root(
        self, temp_dir, monkeypatch
    ):
        """A vanished explicit cross-project owner cannot be safely
        rediscovered, so resume must fail closed instead of consulting the
        unrelated project that stores the run state."""
        from typer.testing import CliRunner
        from specify_cli import app

        owner_project = temp_dir / "owner-project-2"
        (owner_project / ".specify" / "workflows").mkdir(parents=True)
        monkeypatch.chdir(owner_project)
        runner = CliRunner()
        src = owner_project / "gated-src"
        src.mkdir()
        (src / "workflow.yml").write_text(self._GATED_WORKFLOW_YAML, encoding="utf-8")
        result = runner.invoke(app, ["workflow", "add", str(src), "--dev"])
        assert result.exit_code == 0, result.output

        unrelated_cwd = temp_dir / "unrelated-cwd-2"
        unrelated_cwd.mkdir()
        monkeypatch.chdir(unrelated_cwd)

        target = owner_project / ".specify" / "workflows" / "gated-wf" / "workflow.yml"
        result = runner.invoke(app, ["workflow", "run", str(target), "--json"])
        assert result.exit_code == 0, result.output
        run_id = json.loads(result.stdout)["run_id"]

        # owner_project vanishes entirely -- its persisted absolute root
        # is now dangling.
        shutil.rmtree(owner_project)

        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert "owner" in result.output.lower()
        assert "unavailable" in result.output.lower()

    @pytest.mark.parametrize(
        "field, bad_value",
        [
            ("installed_workflow_id", 123),
            ("installed_workflow_id", ["gated-wf"]),
            ("installed_workflow_id", {"id": "gated-wf"}),
            ("installed_workflow_id", True),
            ("installed_workflow_id", ""),
            ("installed_workflow_id", "gated-wf\n"),
            ("installed_registry_root", 123),
            ("installed_registry_root", ["."]),
            ("installed_registry_root", {"root": "."}),
            ("installed_registry_root", False),
            ("installed_registry_root", ""),
            ("installed_registry_root", "relative-owner"),
            ("workflow_dir", 123),
            ("workflow_dir", ""),
            ("workflow_dir", "../outside"),
        ],
    )
    def test_resume_rejects_malformed_run_state_metadata(
        self, project_dir, monkeypatch, field, bad_value
    ):
        """Reject malformed ownership and resource paths before the run resumes."""
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data[field] = bad_value
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "resume", run_id])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "Error" in result.output

    @pytest.mark.parametrize("command", ["resume", "status"])
    def test_state_load_errors_escape_rich_markup(
        self, project_dir, monkeypatch, command
    ):
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        malicious_status = "[bold red]forged[/bold red]"
        data["status"] = malicious_status
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", command, run_id])

        assert result.exit_code != 0
        assert malicious_status in result.output

    @pytest.mark.parametrize(
        "installed_workflow_id, installed_registry_root",
        [
            (None, None),
            ("gated-wf", None),
        ],
    )
    def test_resume_accepts_valid_run_state_origin_fields(
        self, project_dir, monkeypatch, installed_workflow_id, installed_registry_root
    ):
        """Valid installed-origin values continue to load and resume."""
        from typer.testing import CliRunner
        from specify_cli import app

        monkeypatch.chdir(project_dir)
        runner = CliRunner()
        run_id = self._install_and_run_gated(runner, app, project_dir)

        state_path = (
            project_dir / ".specify" / "workflows" / "runs" / run_id / "state.json"
        )
        data = json.loads(state_path.read_text(encoding="utf-8"))
        data["installed_workflow_id"] = installed_workflow_id
        data["installed_registry_root"] = installed_registry_root
        state_path.write_text(json.dumps(data), encoding="utf-8")

        result = runner.invoke(app, ["workflow", "resume", run_id, "--json"])
        assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    ("source_kind", "legacy_state", "relink"),
    [
        ("installed", False, False),
        ("installed", True, False),
        ("installed", False, True),
        ("metadata", True, True),
        ("specs", True, False),
        ("active-feature", True, False),
        ("repository", False, False),
        ("outside", True, True),
    ],
)
def test_resource_resume_survives_storage_transition(
    tmp_path, monkeypatch, source_kind, legacy_state, relink
):
    from typer.testing import CliRunner
    from specify_cli import app
    from specify_cli.project.move import commit_move, prepare_move
    from specify_cli.workflows.engine import RunState

    repo = tmp_path / "repository"
    workspace = tmp_path / "workspace"
    (repo / ".specify/workflows").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SPECIFY_INIT_DIR", raising=False)
    monkeypatch.delenv("SPECIFY_FEATURE_DIRECTORY", raising=False)
    monkeypatch.chdir(repo)
    source = {
        "installed": tmp_path / "package",
        "metadata": repo / ".specify/custom/nested",
        "specs": repo / "specs/001-feature/resources",
        "active-feature": repo / "design/current/resources",
        "repository": repo / "standalone",
        "outside": tmp_path / "standalone",
    }[source_kind]
    (source / "assets").mkdir(parents=True)
    (source / "assets/message.txt").write_text("copied resource\n", encoding="utf-8")
    argv = [sys.executable, "-X", "utf8", "-c", "import sys; print(sys.argv[1], end='')", "{{ inputs.note }}"]
    remember = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    workflow = source / "workflow.yml"
    workflow.write_text(
        f"""
schema_version: "1.0"
workflow:
  id: resource-wf
  name: Resource workflow
  version: "1.0.0"
inputs:
  verdict:
    type: string
    default: ""
  note:
    type: string
    default: ""
steps:
  - id: remember
    type: shell
    run: {json.dumps(remember)}
  - id: review
    type: gate
    message: Review
    verdict_input: verdict
  - id: resource
    type: shell
    run: {json.dumps(_resource_command("assets/message.txt"))}
""",
        encoding="utf-8",
    )
    if source_kind == "active-feature":
        (repo / ".specify/feature.json").write_text(
            json.dumps({"feature_directory": "design/current"}), encoding="utf-8"
        )
    runner = CliRunner()
    if source_kind == "installed":
        installed = runner.invoke(app, ["workflow", "add", str(source), "--dev"])
        assert installed.exit_code == 0, installed.output
        source = repo / ".specify/workflows/resource-wf"
        command = "resource-wf"
    else:
        command = str(workflow)
    run_root = repo
    if source_kind == "installed" and relink:
        commit_move(prepare_move(repo, workspace))
        source = workspace / source.relative_to(repo)
        run_root = workspace
    note = str(repo / ".specify/user-supplied-path")
    started = runner.invoke(app, ["workflow", "run", command, "--input", f"note={note}", "--json"])
    assert started.exit_code == 0, started.output
    payload = json.loads(started.stdout)
    assert payload["status"] == "paused"
    run_id = payload["run_id"]
    run_relative = Path(".specify/workflows/runs") / run_id
    state_path = run_root / run_relative / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if legacy_state:
        state["workflow_dir"] = str(source)
        state_path.write_text(json.dumps(state), encoding="utf-8")
    inputs_before = (run_root / run_relative / "inputs.json").read_bytes()
    workflow_before = (run_root / run_relative / "workflow.yml").read_bytes()
    if run_root == repo:
        prepared = prepare_move(repo, workspace)
        assert (workspace / run_relative / "state.json").read_bytes() == state_path.read_bytes()
        commit_move(prepared)
    if relink:
        relocated = tmp_path / "relocated workspace"
        shutil.move(workspace, relocated)
        linked = runner.invoke(app, ["project", "link", str(relocated)])
        assert linked.exit_code == 0, linked.output
        workspace = relocated
    moved = source_kind not in ("repository", "outside")
    expected_source = workspace / source.relative_to(run_root) if moved else source
    restored = RunState.load(run_id, workspace)
    assert restored.workflow_dir == str(expected_source)
    assert restored.step_results["remember"] == state["step_results"]["remember"]
    assert (workspace / run_relative / "inputs.json").read_bytes() == inputs_before
    assert (workspace / run_relative / "workflow.yml").read_bytes() == workflow_before
    resumed = runner.invoke(
        app, ["workflow", "resume", run_id, "--input", "verdict=approve", "--json"]
    )
    assert resumed.exit_code == 0, resumed.output
    assert json.loads(resumed.stdout)["status"] == "completed"
    finished = RunState.load(run_id, workspace)
    assert finished.step_results["resource"]["output"]["stdout"].splitlines() == [
        "copied resource", "copied resource", str(repo),
    ]
    assert finished.inputs["note"] == note
    assert finished.step_results["remember"] == state["step_results"]["remember"]
    assert {path.name for path in (repo / ".specify").iterdir()} == {"checkout.json", "project.json"}
    if moved:
        assert not source.exists()
    else:
        assert (source / "assets/message.txt").read_text(encoding="utf-8") == "copied resource\n"
