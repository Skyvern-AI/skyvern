"""Shared input validation guards for both MCP and CLI surfaces."""

from __future__ import annotations

import re

PASSWORD_PATTERN = re.compile(
    r"\bpass(?:word|phrase|code)s?\b|\bsecret\b|\bcredential\b|\bpin\s*(?:code)?\b|\bpwd\b|\bpasswd\b",
    re.IGNORECASE,
)

JS_PASSWORD_PATTERN = re.compile(
    r"""(?:type\s*=\s*['"]?password|\.type\s*===?\s*['"]password|input\[type=password\]).*?\.value\s*=""",
    re.IGNORECASE,
)

CREDENTIAL_HINT = (
    "Use skyvern_login with a stored credential to authenticate. "
    "Create credentials via CLI: skyvern credentials add. "
    "Never pass passwords through tool calls."
)

VALID_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle", "commit")
VALID_BUTTONS = ("left", "right", "middle")
VALID_ELEMENT_STATES = ("visible", "hidden", "attached", "detached")


class GuardError(Exception):
    """Raised when an input guard blocks an operation."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def check_password_prompt(text: str) -> None:
    """Block prompts containing password/credential terms."""
    if PASSWORD_PATTERN.search(text):
        raise GuardError(
            "Cannot perform password/credential actions — credentials must not be passed through tool calls",
            CREDENTIAL_HINT,
        )


def check_js_password(expression: str) -> None:
    """Block JS expressions that set password field values."""
    if JS_PASSWORD_PATTERN.search(expression):
        raise GuardError(
            "Cannot set password field values via JavaScript — credentials must not be passed through tool calls",
            CREDENTIAL_HINT,
        )


def validate_wait_until(value: str | None) -> None:
    if value is not None and value not in VALID_WAIT_UNTIL:
        raise GuardError(
            f"Invalid wait_until: {value}",
            "Use load, domcontentloaded, networkidle, or commit",
        )


def validate_button(value: str | None) -> None:
    if value is not None and value not in VALID_BUTTONS:
        raise GuardError(f"Invalid button: {value}", "Use left, right, or middle")


def resolve_ai_mode(
    selector: str | None,
    intent: str | None,
) -> tuple[str | None, str | None]:
    """Determine AI mode from selector/intent combination.

    Returns (ai_mode, error_code) -- if error_code is set, the call should fail.
    """
    if intent and not selector:
        return "proactive", None
    if intent and selector:
        return "fallback", None
    if selector and not intent:
        return None, None
    return None, "INVALID_INPUT"
