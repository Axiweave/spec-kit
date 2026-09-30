"""Command-focused workflow tests."""

from __future__ import annotations

import os

import pytest



class TestWorkflowStepAddCLI:
    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks are unavailable")
    def test_add_rejects_symlinked_steps_base_dir(self, project_dir, monkeypatch):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.chdir(project_dir)
        outside = project_dir.parent / "outside-steps"
        outside.mkdir(parents=True, exist_ok=True)
        steps_link = project_dir / ".specify" / "workflows" / "steps"
        steps_link.symlink_to(outside, target_is_directory=True)

        def _fake_get_step_info(self, step_id):
            return {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
            }

        monkeypatch.setattr(StepCatalog, "get_step_info", _fake_get_step_info)

        runner = CliRunner()
        result = runner.invoke(app, ["workflow", "step", "add", "my-step"])

        assert result.exit_code != 0
        assert "Refusing to use symlinked step directory" in result.output

    def test_add_rejects_oversized_step_response(self, project_dir, monkeypatch):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows import _commands as wf_commands
        from specify_cli.workflows.step.catalog import StepCatalog
        from specify_cli.authentication import http as auth_http

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(wf_commands, "_MAX_WORKFLOW_YAML_BYTES", 100)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
            },
        )

        class _FakeResponse:
            def __init__(self, url):
                self.url = url
                self.body = b"x" * 500
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def getheader(self, name):
                return None

            def geturl(self):
                return self.url

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body) - self.offset
                chunk = self.body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda url, timeout=30, redirect_validator=None: _FakeResponse(url),
        )

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code != 0
        assert (
            "responseexceedsthe100-byteworkflowsizelimit"
            in "".join(result.output.split())
        )
        assert not (
            project_dir / ".specify" / "workflows" / "steps" / "my-step"
        ).exists()

    @pytest.mark.parametrize(
        "step_yml_body", [b"[]", b"false", b"0", b"''", b"null", b"~", b"NULL"]
    )
    def test_add_rejects_falsy_non_mapping_step_yml(
        self, project_dir, monkeypatch, step_yml_body
    ):
        """A FALSY non-mapping step.yml document ([], false, 0, '') must be
        reported as "step.yml must be a YAML mapping", not silently coerced by
        ``or {}`` into {} and then misreported as the unrelated "missing
        'step.type_key'" error — matching how a TRUTHY non-mapping document
        (e.g. a bare string) already reports the mapping-shape error. An
        explicit null scalar (null/~/NULL) parses to the same ``None`` as a
        genuinely empty document, so it must be distinguished (via
        ``yaml.compose``) and rejected too, rather than defaulting to {}."""
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepCatalog
        from specify_cli.authentication import http as auth_http

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
            },
        )

        class _FakeResponse:
            def __init__(self, url):
                self.url = url
                self.body = step_yml_body if url.endswith("step.yml") else b""
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def getheader(self, name):
                return None

            def geturl(self):
                return self.url

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body) - self.offset
                chunk = self.body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda url, timeout=30, redirect_validator=None: _FakeResponse(url),
        )

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code != 0
        assert "step.yml must be a YAML mapping" in result.output
        assert not (
            project_dir / ".specify" / "workflows" / "steps" / "my-step"
        ).exists()

    @pytest.mark.parametrize(
        ("catalog_fields", "expected"),
        [
            ({"url": 123}, "malformed step.yml URL"),
            (
                {
                    "step_yml_url": [],
                    "url": "https://example.com/step.yml",
                },
                "malformed step.yml URL",
            ),
            (
                {
                    "url": "https://example.com/step.yml",
                    "init_url": 123,
                },
                "malformed __init__.py URL",
            ),
        ],
    )
    def test_add_rejects_non_string_required_urls_before_network(
        self, project_dir, monkeypatch, catalog_fields, expected
    ):
        from typer.testing import CliRunner

        from specify_cli import app
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "_install_allowed": True,
                **catalog_fields,
            },
        )
        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("download should not start")
            ),
        )

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert expected in result.output
        assert not (
            project_dir / ".specify" / "workflows" / "steps" / "my-step"
        ).exists()

    @pytest.mark.parametrize(
        ("alias", "protected_name"),
        [
            ("./step.yml", "step.yml"),
            ("step.yml/", "step.yml"),
            ("STEP.YML", "step.yml"),
            (".\\step.yml", "step.yml"),
            ("./__init__.py", "__init__.py"),
            ("__init__.py/", "__init__.py"),
            ("__INIT__.PY", "__init__.py"),
            (".\\__init__.py", "__init__.py"),
        ],
    )
    def test_add_does_not_overwrite_required_files_through_path_aliases(
        self, project_dir, monkeypatch, alias, protected_name
    ):
        from typer.testing import CliRunner

        from specify_cli import app
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.chdir(project_dir)
        alias_url = "https://example.com/overwrite"
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {alias: alias_url},
            },
        )
        bodies = {
            "https://example.com/step.yml": b"step:\n  type_key: my-step\n",
            "https://example.com/__init__.py": b"# trusted init\n",
        }
        requested_urls: list[str] = []

        class _FakeResponse:
            def __init__(self, url):
                self.url = url
                self.body = bodies[url]
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def geturl(self):
                return self.url

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body) - self.offset
                chunk = self.body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        def fake_open_url(url, timeout=30, redirect_validator=None):
            requested_urls.append(url)
            return _FakeResponse(url)

        monkeypatch.setattr(auth_http, "open_url", fake_open_url)

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code == 0, result.output
        assert alias_url not in requested_urls
        installed_dir = (
            project_dir / ".specify" / "workflows" / "steps" / "my-step"
        )
        assert (installed_dir / protected_name).read_bytes() == bodies[
            f"https://example.com/{protected_name}"
        ]

    def test_add_rejects_too_many_package_files_before_network(
        self, project_dir, monkeypatch
    ):
        from typer.testing import CliRunner

        from specify_cli import app
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step import _helpers as step_helpers
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(step_helpers, "_MAX_STEP_PACKAGE_FILES", 3)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {
                    "one.py": "https://example.com/one.py",
                    "two.py": "https://example.com/two.py",
                },
            },
        )
        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("download should not start")
            ),
        )

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "exceeding the 3-file limit" in result.output
        steps_dir = project_dir / ".specify" / "workflows" / "steps"
        assert not (steps_dir / "my-step").exists()
        assert list(steps_dir.glob("speckit_step_tmp_*")) == []

    def test_add_rejects_package_over_cumulative_size_and_cleans_staging(
        self, project_dir, monkeypatch
    ):
        from typer.testing import CliRunner

        from specify_cli import app
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step import _helpers as step_helpers
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(step_helpers, "_MAX_STEP_PACKAGE_BYTES", 40)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {
                    "helper.py": "https://example.com/helper.py",
                },
            },
        )

        bodies = {
            "https://example.com/step.yml": b"step:\n  type_key: my-step\n",
            "https://example.com/__init__.py": b"# init\n",
            "https://example.com/helper.py": b"0123456789",
        }

        class _FakeResponse:
            def __init__(self, url):
                self.url = url
                self.body = bodies[url]
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def getheader(self, name):
                return None

            def geturl(self):
                return self.url

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body) - self.offset
                chunk = self.body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda url, timeout=30, redirect_validator=None: _FakeResponse(url),
        )

        result = CliRunner().invoke(
            app, ["workflow", "step", "add", "my-step"]
        )

        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "40-byte total size limit" in result.output
        steps_dir = project_dir / ".specify" / "workflows" / "steps"
        assert not (steps_dir / "my-step").exists()
        assert list(steps_dir.glob("speckit_step_tmp_*")) == []

    def test_add_rejects_non_string_extra_files_key(self, project_dir, monkeypatch):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepCatalog
        from specify_cli.authentication import http as auth_http

        monkeypatch.chdir(project_dir)

        def _fake_get_step_info(self, step_id):
            return {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {
                    123: "https://example.com/helper.py",
                },
            }

        class _FakeResponse:
            def __init__(self, url: str):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                if getattr(self, "_read", False):
                    return b""
                self._read = True
                if self.url.endswith("/step.yml"):
                    return b"step:\n  type_key: my-step\n"
                return b""

            def geturl(self):
                return self.url

        def _fake_open_url(url, timeout=30, redirect_validator=None):
            return _FakeResponse(url)

        monkeypatch.setattr(StepCatalog, "get_step_info", _fake_get_step_info)
        monkeypatch.setattr(auth_http, "open_url", _fake_open_url)

        runner = CliRunner()
        result = runner.invoke(app, ["workflow", "step", "add", "my-step"])

        assert result.exit_code != 0
        assert "non-string path key" in result.output

    @pytest.mark.parametrize(
        "rel_path,expected",
        [
            ("", "empty or non-string path key"),
            (".", "not a valid relative file path"),
            ("..", "not a valid relative file path"),
            ("sub/../x", "not a valid relative file path"),
        ],
    )
    def test_add_rejects_invalid_extra_files_path(
        self, project_dir, monkeypatch, rel_path, expected
    ):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepCatalog
        from specify_cli.authentication import http as auth_http

        monkeypatch.chdir(project_dir)

        def _fake_get_step_info(self, step_id):
            return {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {rel_path: "https://example.com/helper.py"},
            }

        class _FakeResponse:
            def __init__(self, url: str):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                if getattr(self, "_read", False):
                    return b""
                self._read = True
                if self.url.endswith("/step.yml"):
                    return b"step:\n  type_key: my-step\n"
                return b""

            def geturl(self):
                return self.url

        def _fake_open_url(url, timeout=30, redirect_validator=None):
            return _FakeResponse(url)

        monkeypatch.setattr(StepCatalog, "get_step_info", _fake_get_step_info)
        monkeypatch.setattr(auth_http, "open_url", _fake_open_url)

        runner = CliRunner()
        result = runner.invoke(app, ["workflow", "step", "add", "my-step"])

        assert result.exit_code != 0
        assert expected in result.output

    def test_add_rejects_non_string_extra_files_url(self, project_dir, monkeypatch):
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepCatalog
        from specify_cli.authentication import http as auth_http

        monkeypatch.chdir(project_dir)

        def _fake_get_step_info(self, step_id):
            return {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {"helper.py": None},
            }

        class _FakeResponse:
            def __init__(self, url: str):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self, size=-1):
                if getattr(self, "_read", False):
                    return b""
                self._read = True
                if self.url.endswith("/step.yml"):
                    return b"step:\n  type_key: my-step\n"
                return b""

            def geturl(self):
                return self.url

        def _fake_open_url(url, timeout=30, redirect_validator=None):
            return _FakeResponse(url)

        monkeypatch.setattr(StepCatalog, "get_step_info", _fake_get_step_info)
        monkeypatch.setattr(auth_http, "open_url", _fake_open_url)

        runner = CliRunner()
        result = runner.invoke(app, ["workflow", "step", "add", "my-step"])

        assert result.exit_code != 0
        assert "empty or non-string URL" in result.output


class TestWorkflowStepGeneratedFilesProvenance:
    """`generated_files` records the installer's own produced bytes (T005)."""

    def test_add_catalog_records_generated_files_for_every_produced_file(
        self, project_dir, monkeypatch
    ):
        import hashlib

        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step.catalog import StepCatalog, StepRegistry

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {"helper.py": "https://example.com/helper.py"},
            },
        )

        bodies = {
            "https://example.com/step.yml": b"step:\n  type_key: my-step\n",
            "https://example.com/__init__.py": b"# init\n",
            "https://example.com/helper.py": b"print('helper')\n",
        }

        class _FakeResponse:
            def __init__(self, url):
                self.url = url
                self.body = bodies[url]
                self.offset = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def getheader(self, name):
                return None

            def geturl(self):
                return self.url

            def read(self, size=-1):
                if size < 0:
                    size = len(self.body) - self.offset
                chunk = self.body[self.offset : self.offset + size]
                self.offset += len(chunk)
                return chunk

        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda url, timeout=30, redirect_validator=None: _FakeResponse(url),
        )

        result = CliRunner().invoke(app, ["workflow", "step", "add", "my-step"])
        assert result.exit_code == 0, result.output

        entry = StepRegistry(project_dir).get("my-step")
        assert entry["generated_files"] == {
            ".specify/workflows/steps/my-step/step.yml": hashlib.sha256(
                bodies["https://example.com/step.yml"]
            ).hexdigest(),
            ".specify/workflows/steps/my-step/__init__.py": hashlib.sha256(
                bodies["https://example.com/__init__.py"]
            ).hexdigest(),
            ".specify/workflows/steps/my-step/helper.py": hashlib.sha256(
                bodies["https://example.com/helper.py"]
            ).hexdigest(),
        }


class TestWorkflowStepRestorationPreservesCustomContent:
    """Restoration preserves custom files and restores missing produced files."""

    BODIES = {
        "https://example.com/step.yml": b"step:\n  type_key: my-step\n",
        "https://example.com/__init__.py": b"# original init\n",
        "https://example.com/helper.py": b"print('helper')\n",
    }

    class _FakeResponse:
        def __init__(self, url):
            self.url = url
            self.body = TestWorkflowStepRestorationPreservesCustomContent.BODIES[url]
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def getheader(self, name):
            return None

        def geturl(self):
            return self.url

        def read(self, size=-1):
            if size < 0:
                size = len(self.body) - self.offset
            chunk = self.body[self.offset : self.offset + size]
            self.offset += len(chunk)
            return chunk

    def _mock_catalog(self, monkeypatch):
        from specify_cli.authentication import http as auth_http
        from specify_cli.workflows.step.catalog import StepCatalog

        monkeypatch.setattr(
            StepCatalog,
            "get_step_info",
            lambda self, step_id: {
                "id": step_id,
                "name": "Test Step",
                "url": "https://example.com/step.yml",
                "init_url": "https://example.com/__init__.py",
                "_install_allowed": True,
                "extra_files": {"helper.py": "https://example.com/helper.py"},
            },
        )
        monkeypatch.setattr(
            auth_http,
            "open_url",
            lambda url, timeout=30, redirect_validator=None: self._FakeResponse(url),
        )

    def test_add_restores_directory_deleted_after_clone_while_registry_entry_survives(
        self, project_dir, monkeypatch
    ):
        """Every produced file was excluded from workspace history (all
        matched their producer hash); after a clone the directory is gone
        entirely, but the durable registry entry survived. Re-adding the
        same step type must restore it instead of refusing."""
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepRegistry

        monkeypatch.chdir(project_dir)
        self._mock_catalog(monkeypatch)
        runner = CliRunner()
        first = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert first.exit_code == 0, first.output

        step_dir = project_dir / ".specify" / "workflows" / "steps" / "my-step"
        import shutil

        shutil.rmtree(step_dir)
        assert StepRegistry(project_dir).is_installed("my-step")

        second = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert second.exit_code == 0, second.output
        assert (step_dir / "step.yml").read_bytes() == self.BODIES[
            "https://example.com/step.yml"
        ]
        assert (step_dir / "__init__.py").read_bytes() == self.BODIES[
            "https://example.com/__init__.py"
        ]
        assert (step_dir / "helper.py").read_bytes() == self.BODIES[
            "https://example.com/helper.py"
        ]

    def test_add_restores_missing_generated_file_while_preserving_edited_file(
        self, project_dir, monkeypatch
    ):
        """A user hand-edited ``__init__.py`` and force-added it, so it
        survived the clone; ``step.yml`` matched its producer hash and was
        excluded, so it is absent. Restoring must recreate ``step.yml``
        without reverting the edited ``__init__.py``."""
        import hashlib

        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepRegistry

        monkeypatch.chdir(project_dir)
        self._mock_catalog(monkeypatch)
        runner = CliRunner()
        first = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert first.exit_code == 0, first.output

        step_dir = project_dir / ".specify" / "workflows" / "steps" / "my-step"
        original_baseline = StepRegistry(project_dir).get("my-step")[
            "generated_files"
        ][".specify/workflows/steps/my-step/__init__.py"]
        (step_dir / "__init__.py").write_bytes(b"# user customized init\n")
        (step_dir / "step.yml").unlink()

        second = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert second.exit_code == 0, second.output

        assert (step_dir / "step.yml").read_bytes() == self.BODIES[
            "https://example.com/step.yml"
        ]
        assert (step_dir / "__init__.py").read_bytes() == b"# user customized init\n"

        generated = StepRegistry(project_dir).get("my-step")["generated_files"]
        assert generated[".specify/workflows/steps/my-step/step.yml"] == (
            hashlib.sha256(self.BODIES["https://example.com/step.yml"]).hexdigest()
        )
        # The preserved edit keeps its original baseline so a later
        # workspace-history hash comparison still detects the edit.
        assert (
            generated[".specify/workflows/steps/my-step/__init__.py"]
            == original_baseline
        )

    def test_add_reinstall_preserves_unrelated_registry_fields(
        self, project_dir, monkeypatch
    ):
        """registry.add() replaces the stored entry wholesale, so a
        restore/reinstall must build the new entry from the existing
        record (not a bare literal) -- otherwise a field this code
        doesn't know about (from a newer Spec Kit version, or set by
        another tool) is silently dropped on every restore."""
        from typer.testing import CliRunner
        from specify_cli import app
        from specify_cli.workflows.step.catalog import StepRegistry

        monkeypatch.chdir(project_dir)
        self._mock_catalog(monkeypatch)
        runner = CliRunner()
        first = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert first.exit_code == 0, first.output

        registry = StepRegistry(project_dir)
        entry = dict(registry.get("my-step"))
        entry["future_field"] = "set by a newer spec-kit or another tool"
        registry.add("my-step", entry)

        step_dir = project_dir / ".specify" / "workflows" / "steps" / "my-step"
        import shutil

        shutil.rmtree(step_dir)

        second = runner.invoke(app, ["workflow", "step", "add", "my-step"])
        assert second.exit_code == 0, second.output

        assert StepRegistry(project_dir).get("my-step")["future_field"] == (
            "set by a newer spec-kit or another tool"
        )
