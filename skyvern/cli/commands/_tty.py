"""TTY detection utilities for agent-aware non-interactive mode."""

from __future__ import annotations

import os
import sys
from typing import Any

from ._output import output_error

_TRUTHY_VALUES = {"1", "true", "yes"}


def is_interactive() -> bool:
    """Return False if running in a CI or non-interactive context.

    Checks CI and SKYVERN_NON_INTERACTIVE against explicit truthy values
    (1, true, yes) so that setting them to "0" or "false" does not
    accidentally trigger non-interactive mode.
    """
    if os.environ.get("CI", "").lower() in _TRUTHY_VALUES:
        return False
    if os.environ.get("SKYVERN_NON_INTERACTIVE", "").lower() in _TRUTHY_VALUES:
        return False
    return sys.stdin.isatty()


def require_interactive_or_flag(
    flag_value: Any,
    *,
    flag_name: str,
    message: str,
    env_var_prefix: str = "SKYVERN_CRED_",
    action: str = "",
    json_mode: bool = False,
) -> Any:
    """Return flag_value if provided, else check if interactive.

    If interactive (TTY), returns None so the caller can proceed with
    an interactive prompt. If non-interactive and no flag provided,
    exits with an actionable error message in the appropriate format.
    """
    if flag_value is not None:
        return flag_value
    if is_interactive():
        return None
    env_var = f"{env_var_prefix}{flag_name.upper().replace('-', '_')}"
    output_error(
        f"{message} Running in non-interactive mode.",
        hint=f"Set {env_var} or pass --{flag_name} <value>, or run in an interactive terminal. "
        f"Unset SKYVERN_NON_INTERACTIVE to re-enable prompts.",
        action=action,
        json_mode=json_mode,
    )
    raise SystemExit(1)  # unreachable — output_error is NoReturn
