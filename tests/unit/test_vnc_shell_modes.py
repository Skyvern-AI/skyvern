import os
import subprocess
import time
from pathlib import Path
from typing import Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _install_command_stubs(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    stub = bin_dir / "stub"
    stub.write_text(
        """#!/bin/bash
name=${0##*/}
printf '%s|DISPLAY=%s|%s\\n' "$name" "${DISPLAY-}" "$*" >> "$COMMAND_LOG"
if [[ "$name" == "pgrep" ]]; then
  exit "${PGREP_EXIT_CODE:-1}"
fi
if [[ "$name" == "xdpyinfo" ]]; then
  exit "${XDPYINFO_EXIT_CODE:-0}"
fi
exit 0
"""
    )
    stub.chmod(0o755)
    for name in ("alembic", "python", "Xvfb", "xterm", "xdpyinfo", "x11vnc", "websockify", "pgrep"):
        (bin_dir / name).symlink_to(stub)
    return bin_dir, command_log


def _read_command_log(command_log: Path) -> list[str]:
    if not command_log.exists():
        return []
    return command_log.read_text().splitlines()


def _invoke_shell_script(
    tmp_path: Path,
    script: Path,
    *,
    streaming_mode: str | None,
    default_display: int | str,
    extra_environment: Mapping[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir, command_log = _install_command_stubs(tmp_path)
    credentials_file = tmp_path / "credentials.toml"
    credentials_file.write_text("[skyvern]\n")
    log_dir = tmp_path / "logs"

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_dir}:{environment['PATH']}",
            "COMMAND_LOG": str(command_log),
            "DATABASE_STRING": "",
            "SKYVERN_CREDENTIALS_FILE": str(credentials_file),
            "SKYVERN_DEFAULT_DISPLAY": str(default_display),
            "LOG_PATH": str(log_dir),
            "XDPYINFO_EXIT_CODE": "0" if script.name == "entrypoint-skyvern.sh" else "1",
        }
    )
    if streaming_mode is None:
        environment.pop("BROWSER_STREAMING_MODE", None)
    else:
        environment["BROWSER_STREAMING_MODE"] = streaming_mode
    if extra_environment is not None:
        environment.update(extra_environment)

    result = subprocess.run(
        ["bash", str(script)],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return result, _read_command_log(command_log)
    # start_vnc_streaming.sh backgrounds Xvfb, so let the command stub flush
    # its invocation before inspecting the log.
    for _ in range(100):
        lines = _read_command_log(command_log)
        if any(line.startswith("Xvfb|") for line in lines):
            return result, lines
        time.sleep(0.01)
    return result, _read_command_log(command_log)


def _run_shell_script(
    tmp_path: Path,
    script: Path,
    *,
    streaming_mode: str | None,
    default_display: int | str,
    extra_environment: Mapping[str, str] | None = None,
) -> list[str]:
    result, lines = _invoke_shell_script(
        tmp_path,
        script,
        streaming_mode=streaming_mode,
        default_display=default_display,
        extra_environment=extra_environment,
    )
    result.check_returncode()
    return lines


def _commands(lines: list[str], command: str) -> list[str]:
    return [line for line in lines if line.startswith(f"{command}|")]


@pytest.mark.parametrize("streaming_mode", [None, "cdp"])
def test_entrypoint_preserves_static_streaming_stack_outside_exact_vnc_mode(
    tmp_path: Path,
    streaming_mode: str | None,
) -> None:
    lines = _run_shell_script(
        tmp_path,
        REPO_ROOT / "entrypoint-skyvern.sh",
        streaming_mode=streaming_mode,
        default_display=101,
    )

    assert any(":101 -screen 0 1920x1080x16" in line for line in _commands(lines, "Xvfb"))
    assert any("-display :101" in line and "-rfbport 5900" in line for line in _commands(lines, "x11vnc"))
    assert any("6080 localhost:5900 --daemon" in line for line in _commands(lines, "websockify"))


def test_entrypoint_exact_vnc_mode_only_starts_base_xvfb(tmp_path: Path) -> None:
    lines = _run_shell_script(
        tmp_path,
        REPO_ROOT / "entrypoint-skyvern.sh",
        streaming_mode="vnc",
        default_display=102,
    )

    assert any(":102 -screen 0 1920x1080x16" in line for line in _commands(lines, "Xvfb"))
    assert not _commands(lines, "x11vnc")
    assert not _commands(lines, "websockify")


@pytest.mark.parametrize("streaming_mode", [None, "cdp"])
def test_vnc_helper_preserves_static_stack_outside_exact_vnc_mode(
    tmp_path: Path,
    streaming_mode: str | None,
) -> None:
    lines = _run_shell_script(
        tmp_path,
        REPO_ROOT / "scripts/start_vnc_streaming.sh",
        streaming_mode=streaming_mode,
        default_display=103,
    )

    assert any(":103 -screen 0 1920x1080x24" in line for line in _commands(lines, "Xvfb"))
    assert any("-display :103" in line for line in _commands(lines, "x11vnc"))
    assert any("6080 localhost:5900 --daemon" in line for line in _commands(lines, "websockify"))


def test_vnc_helper_exact_vnc_mode_only_starts_base_xvfb(tmp_path: Path) -> None:
    lines = _run_shell_script(
        tmp_path,
        REPO_ROOT / "scripts/start_vnc_streaming.sh",
        streaming_mode="vnc",
        default_display=104,
    )

    assert any(":104 -screen 0 1920x1080x24" in line for line in _commands(lines, "Xvfb"))
    assert not _commands(lines, "x11vnc")
    assert not _commands(lines, "websockify")


@pytest.mark.parametrize(
    "script",
    [REPO_ROOT / "entrypoint-skyvern.sh", REPO_ROOT / "scripts/start_vnc_streaming.sh"],
    ids=["entrypoint", "helper"],
)
def test_invalid_default_display_is_rejected_without_command_injection(tmp_path: Path, script: Path) -> None:
    injected_file = tmp_path / "injected"
    result, lines = _invoke_shell_script(
        tmp_path,
        script,
        streaming_mode="vnc",
        default_display=f"99; touch {injected_file}; #",
    )

    assert not injected_file.exists()
    assert result.returncode != 0
    assert "SKYVERN_DEFAULT_DISPLAY must be an unsigned integer" in result.stderr
    assert not _commands(lines, "Xvfb")


def test_vnc_helper_does_not_treat_other_xvfb_as_configured_display(tmp_path: Path) -> None:
    # pgrep success represents an Xvfb on another display; this display's
    # xdpyinfo probe still fails, so the configured base must be started.
    lines = _run_shell_script(
        tmp_path,
        REPO_ROOT / "scripts/start_vnc_streaming.sh",
        streaming_mode="vnc",
        default_display=105,
        extra_environment={"PGREP_EXIT_CODE": "0", "XDPYINFO_EXIT_CODE": "1"},
    )

    assert any("-display :105" in line for line in _commands(lines, "xdpyinfo"))
    assert any(":105 -screen 0 1920x1080x24" in line for line in _commands(lines, "Xvfb"))


def test_vnc_helper_does_not_use_eval() -> None:
    script = (REPO_ROOT / "scripts/start_vnc_streaming.sh").read_text()

    assert "eval " not in script
