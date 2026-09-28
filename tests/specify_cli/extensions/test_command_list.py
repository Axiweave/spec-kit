"""Tests for ``specify extension list``.

Mirrors ``specify_cli.extensions.command_list``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from specify_cli import app
from specify_cli.extensions import (
    ExtensionManager,
)
from tests.conftest import strip_ansi


class TestExtensionListCLI:
    """Test extension list CLI output format."""

    def test_list_shows_extension_id(self, extension_dir, project_dir):
        """extension list should display the extension ID."""

        runner = CliRunner()

        # Install the extension using the manager
        manager = ExtensionManager(project_dir)
        manager.install_from_directory(extension_dir, "0.1.0", register_commands=False)

        with patch.object(Path, "cwd", return_value=project_dir):
            result = runner.invoke(app, ["extension", "list"])

        assert result.exit_code == 0, result.output
        plain = strip_ansi(result.output)
        # Verify the extension ID is shown in the output
        assert "test-ext" in plain
        # Verify name and version are also shown
        assert "Test Extension" in plain
        assert "1.0.0" in plain


class TestExtensionListPriorityCLI:
    """Priority display coverage for ``extension list``."""

    def test_list_shows_priority(self, extension_dir, project_dir):
        """Test extension list shows priority."""

        runner = CliRunner()

        # Install extension with priority
        manager = ExtensionManager(project_dir)
        manager.install_from_directory(extension_dir, "0.1.0", register_commands=False, priority=7)

        with patch.object(Path, "cwd", return_value=project_dir):
            result = runner.invoke(app, ["extension", "list"])

        assert result.exit_code == 0, result.output
        plain = strip_ansi(result.output)
        assert "Priority: 7" in plain
