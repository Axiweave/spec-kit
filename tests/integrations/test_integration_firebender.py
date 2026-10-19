"""Tests for FirebenderIntegration."""

from specify_cli.integrations import get_integration
from specify_cli.integrations.manifest import IntegrationManifest

from .test_integration_base_markdown import MarkdownIntegrationTests


class TestFirebenderIntegration(MarkdownIntegrationTests):
    KEY = "firebender"
    FOLDER = ".firebender/"
    COMMANDS_SUBDIR = "commands"
    REGISTRAR_DIR = ".firebender/commands"

    # Firebender reads custom slash commands from ``.firebender/commands/*.mdc``,
    # so this integration uses the ``.mdc`` extension instead of the ``.md``
    # default the base mixin assumes. Override the two extension-specific tests.
    def test_registrar_config(self):
        i = get_integration(self.KEY)
        assert i.registrar_config["dir"] == self.REGISTRAR_DIR
        assert i.registrar_config["format"] == "markdown"
        assert i.registrar_config["args"] == "$ARGUMENTS"
        assert i.registrar_config["extension"] == ".mdc"

    def test_setup_creates_files(self, tmp_path):
        i = get_integration(self.KEY)
        m = IntegrationManifest(self.KEY, tmp_path)
        created = i.setup(tmp_path, m)
        assert len(created) > 0
        cmd_files = [f for f in created if "scripts" not in f.parts]
        for f in cmd_files:
            assert f.exists()
            assert f.name.startswith("speckit.")
            assert f.name.endswith(".mdc")
