"""
Regression test for totp_verification_url key in _action_to_stmt (#SKY-9804).

When a task block has totp_verification_url set and an action has
totp_code_required=True, the generated fill() call must include the
totp_url= kwarg so that the cached script polls for the verification code
instead of reusing the stale literal value from the original run.
"""

from typing import Any

import libcst as cst

from skyvern.core.script_generations.generate_script import _action_to_stmt


def _render(stmt: cst.BaseStatement) -> str:
    return cst.Module(body=[stmt]).code


class TestActionToStmtTotpVerificationUrl:
    """Ensure totp_verification_url is emitted into fill() calls."""

    def test_totp_verification_url_emitted_when_totp_code_required(self) -> None:
        """fill() call includes totp_url= when task has totp_verification_url."""
        act: dict[str, Any] = {
            "action_type": "input_text",
            "xpath": "//input[@name='code']",
            "text": "123456",
            "totp_code_required": True,
            "css_selector": "[name='code']",
            "attributes": {"name": "code"},
        }
        task: dict[str, Any] = {
            "totp_verification_url": "https://example.com/totp",
        }

        stmt = _action_to_stmt(act, task, use_semantic_selectors=True)
        code = _render(stmt)

        assert "totp_url" in code, f"totp_url kwarg missing from generated code:\n{code}"
        assert "https://example.com/totp" in code, f"URL missing from generated code:\n{code}"

    def test_totp_verification_url_not_emitted_when_absent(self) -> None:
        """fill() call does not include totp_url= when task has no totp_verification_url."""
        act: dict[str, Any] = {
            "action_type": "input_text",
            "xpath": "//input[@name='email']",
            "text": "user@example.com",
            "totp_code_required": False,
            "css_selector": "[name='email']",
            "attributes": {"name": "email"},
        }
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(act, task, use_semantic_selectors=True)
        code = _render(stmt)

        assert "totp_url" not in code, f"totp_url kwarg unexpectedly present:\n{code}"

    def test_totp_url_key_not_used_as_condition(self) -> None:
        """Regression: task with only totp_verification_url (not totp_url) still emits totp_url=."""
        act: dict[str, Any] = {
            "action_type": "input_text",
            "xpath": "//input[@id='mfa']",
            "text": "654321",
            "totp_code_required": True,
            "css_selector": "[id='mfa']",
            "attributes": {"id": "mfa"},
        }
        # task has "totp_verification_url" (correct field name from DB),
        # NOT "totp_url" (old wrong field name the bug checked against)
        task: dict[str, Any] = {
            "totp_verification_url": "https://verify.example.com/code",
            # deliberately no "totp_url" key to prove the fix
        }

        stmt = _action_to_stmt(act, task, use_semantic_selectors=True)
        code = _render(stmt)

        assert "totp_url" in code, (
            "totp_url kwarg missing — bug regression: condition was checking 'totp_url' "
            f"instead of 'totp_verification_url':\n{code}"
        )
