from __future__ import annotations

import subprocess
from unittest.mock import patch

from skyvern.cli.core.ngrok import (
    check_ngrok_auth,
    detect_ngrok,
    detect_os,
    offer_install_ngrok,
    offer_setup_auth,
)


def test_detect_ngrok_found() -> None:
    with patch("skyvern.cli.core.ngrok.shutil.which", return_value="/usr/local/bin/ngrok"):
        assert detect_ngrok() == "/usr/local/bin/ngrok"


def test_detect_ngrok_missing() -> None:
    with patch("skyvern.cli.core.ngrok.shutil.which", return_value=None):
        assert detect_ngrok() is None


def test_detect_os_platforms() -> None:
    for system, expected in [("Darwin", "macos"), ("Linux", "linux"), ("Windows", "windows")]:
        with patch("skyvern.cli.core.ngrok.platform.system", return_value=system):
            assert detect_os() == expected


def test_check_ngrok_auth_success() -> None:
    with patch("skyvern.cli.core.ngrok.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        assert check_ngrok_auth("/usr/local/bin/ngrok") is True


def test_check_ngrok_auth_failures() -> None:
    # Non-zero exit
    with patch("skyvern.cli.core.ngrok.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
        assert check_ngrok_auth("/usr/local/bin/ngrok") is False

    # Timeout
    with patch(
        "skyvern.cli.core.ngrok.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ngrok", timeout=5),
    ):
        assert check_ngrok_auth("/usr/local/bin/ngrok") is False


def test_offer_install_brew_success() -> None:
    with (
        patch("skyvern.cli.core.ngrok.detect_os", return_value="macos"),
        patch("skyvern.cli.core.ngrok.shutil.which", side_effect=["/opt/homebrew/bin/brew", "/opt/homebrew/bin/ngrok"]),
        patch("skyvern.cli.core.ngrok.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0)),
        patch("skyvern.cli.core.ngrok.Confirm.ask", return_value=True),
    ):
        assert offer_install_ngrok() == "/opt/homebrew/bin/ngrok"


def test_offer_install_brew_declined() -> None:
    """When user declines brew, should show manual install info without redundant 'ngrok not found'."""
    with (
        patch("skyvern.cli.core.ngrok.detect_os", return_value="macos"),
        patch("skyvern.cli.core.ngrok.shutil.which", return_value="/opt/homebrew/bin/brew"),
        patch("skyvern.cli.core.ngrok.Confirm.ask", side_effect=[False, False]),
        patch("skyvern.cli.core.ngrok.console") as mock_console,
    ):
        result = offer_install_ngrok()
        assert result is None
        # Should NOT print "ngrok not found" again after user declined
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "ngrok not found" not in printed


def test_offer_install_non_interactive() -> None:
    """Non-interactive mode (--tunnel in CI) prints error and returns None without prompting."""
    with patch("skyvern.cli.core.ngrok.console") as mock_console:
        result = offer_install_ngrok(interactive=False)
        assert result is None
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "ngrok not found" in printed


def test_offer_setup_auth_success() -> None:
    with (
        patch("skyvern.cli.core.ngrok.subprocess.run", return_value=subprocess.CompletedProcess(args=[], returncode=0)),
        patch("skyvern.cli.core.ngrok.open_url"),
        patch("skyvern.cli.core.ngrok.Confirm.ask", return_value=True),
        patch("skyvern.cli.core.ngrok.Prompt.ask", return_value="my-token"),
    ):
        assert offer_setup_auth("/usr/local/bin/ngrok") is True


def test_offer_setup_auth_skipped() -> None:
    with (
        patch("skyvern.cli.core.ngrok.open_url"),
        patch("skyvern.cli.core.ngrok.Confirm.ask", return_value=False),
        patch("skyvern.cli.core.ngrok.Prompt.ask", return_value=""),
    ):
        assert offer_setup_auth("/usr/local/bin/ngrok") is False


def test_offer_setup_auth_non_interactive() -> None:
    """Non-interactive mode prints instructions and returns False without prompting."""
    with patch("skyvern.cli.core.ngrok.console") as mock_console:
        result = offer_setup_auth("/usr/local/bin/ngrok", interactive=False)
        assert result is False
        printed = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "auth token not configured" in printed
        assert "add-authtoken" in printed
