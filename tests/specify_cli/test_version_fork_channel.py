"""`specify self upgrade` / `self check` must use the Axiweave fork, not upstream."""

import urllib.error
from unittest.mock import patch

from specify_cli import app

from tests.specify_cli.self_upgrade_helpers import (
    route_opener_open_through_urlopen,  # noqa: F401 (autouse fixture)
    _completed_process,
    mock_urlopen_response,
    runner,
    strip_ansi,
)

FORK_SOURCE = "git+https://github.com/Axiweave/spec-kit.git"
FORK_INSTALL = f"uv tool install specify-cli --force --from {FORK_SOURCE}"


def _not_found(request, *args, **kwargs):
    raise urllib.error.HTTPError(
        url=request.full_url, code=404, msg="Not Found", hdrs={}, fp=None  # type: ignore[arg-type]
    )


def _requested_urls(mock_urlopen) -> list[str]:
    return [call.args[0].full_url for call in mock_urlopen.call_args_list]


def _flat_output(result) -> str:
    """Rich wraps long lines at the terminal width; compare on collapsed whitespace."""
    return " ".join(strip_ansi(result.output).split())


def test_upgrade_without_tag_reports_fork_has_no_release(uv_tool_argv0, clean_environ):
    with patch(
        "specify_cli.authentication.http.urllib.request.urlopen", side_effect=_not_found
    ) as mock_urlopen, patch("specify_cli._version.shutil.which", return_value="uv"), patch(
        "specify_cli._version.subprocess.run"
    ) as mock_run, patch("specify_cli._version._get_installed_version", return_value="1.0.13.dev0"):
        result = runner.invoke(app, ["self", "upgrade"])

    assert result.exit_code == 1
    out = _flat_output(result)
    assert _requested_urls(mock_urlopen) == [
        "https://api.github.com/repos/Axiweave/spec-kit/releases/latest"
    ]
    assert "Axiweave/spec-kit publishes no releases" in out
    assert FORK_INSTALL in out
    assert "HTTP 404" not in out
    mock_run.assert_not_called()


def test_check_without_fork_release_gives_install_command(clean_environ):
    with patch(
        "specify_cli.authentication.http.urllib.request.urlopen", side_effect=_not_found
    ), patch("specify_cli._version._get_installed_version", return_value="1.0.13.dev0"):
        result = runner.invoke(app, ["self", "check"])

    assert result.exit_code == 0
    out = _flat_output(result)
    assert "Axiweave/spec-kit publishes no releases" in out
    assert FORK_INSTALL in out


def test_dry_run_tag_targets_fork(uv_tool_argv0, clean_environ):
    with patch("specify_cli._version.shutil.which", return_value="uv"), patch(
        "specify_cli._version._get_installed_version", return_value="1.0.13.dev0"
    ):
        result = runner.invoke(app, ["self", "upgrade", "--dry-run", "--tag", "v1.0.13"])

    assert result.exit_code == 0
    assert f"--from {FORK_SOURCE}@v1.0.13" in _flat_output(result)


def test_missing_fork_tag_explains_failed_install(uv_tool_argv0, clean_environ):
    with patch(
        "specify_cli.authentication.http.urllib.request.urlopen", side_effect=_not_found
    ) as mock_urlopen, patch("specify_cli._version.shutil.which", return_value="uv"), patch(
        "specify_cli._version.subprocess.run", side_effect=[_completed_process(1)]
    ), patch("specify_cli._version._get_installed_version", return_value="1.0.12"):
        result = runner.invoke(app, ["self", "upgrade", "--tag", "v1.0.13"])

    assert result.exit_code == 1
    out = _flat_output(result)
    assert _requested_urls(mock_urlopen) == [
        "https://api.github.com/repos/Axiweave/spec-kit/git/ref/tags/v1.0.13"
    ]
    assert (
        "Tag v1.0.13 does not exist in https://github.com/Axiweave/spec-kit; "
        "see https://github.com/Axiweave/spec-kit/tags for available tags."
    ) in out


def test_existing_tag_failure_does_not_claim_tag_is_missing(uv_tool_argv0, clean_environ):
    with patch("specify_cli.authentication.http.urllib.request.urlopen") as mock_urlopen, patch(
        "specify_cli._version.shutil.which", return_value="uv"
    ), patch(
        "specify_cli._version.subprocess.run", side_effect=[_completed_process(1)]
    ), patch("specify_cli._version._get_installed_version", return_value="1.0.12"):
        mock_urlopen.return_value = mock_urlopen_response({"ref": "refs/tags/v1.0.13"})
        result = runner.invoke(app, ["self", "upgrade", "--tag", "v1.0.13"])

    assert result.exit_code == 1
    assert "does not exist" not in _flat_output(result)
