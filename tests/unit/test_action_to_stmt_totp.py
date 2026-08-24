"""
Regression test for totp_verification_url key in _action_to_stmt (#SKY-9804).

When a task block has totp_verification_url set and an action has
totp_code_required=True, the generated fill() call must include the
totp_url= kwarg so that the cached script polls for the verification code
instead of reusing the stale literal value from the original run.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import libcst as cst

from skyvern.core.script_generations.generate_script import _action_to_stmt, _build_block_fn


def _render(stmt: cst.BaseStatement) -> str:
    return cst.Module(body=[stmt]).code


def _render_fn(fn: cst.FunctionDef) -> str:
    return cst.Module(body=[fn]).code


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


class TestActionToStmtGotoUrl:
    """A recorded GOTO_URL action must emit a call, not crash script generation (SKY-14056)."""

    def test_goto_url_action_does_not_raise_key_error(self) -> None:
        recorded_url = "https://example.com/dashboard"
        act: dict[str, Any] = {"action_type": "goto_url", "url": recorded_url}

        stmt = _action_to_stmt(act, {})
        code = _render(stmt)

        assert "page.goto(" in code, code
        # nosemgrep: incomplete-url-substring-sanitization  # searching generated source, not a URL
        assert recorded_url in code, code

    def test_magic_link_goto_emits_magic_link_and_drops_the_single_use_url(self) -> None:
        act: dict[str, Any] = {
            "action_type": "goto_url",
            "url": "https://mail.example.com/signin?token=one-time-secret-value",
            "is_magic_link": True,
        }
        task: dict[str, Any] = {"totp_identifier": "inbox@example.com"}

        stmt = _action_to_stmt(act, task)
        code = _render(stmt)

        assert "page.magic_link(" in code, code
        assert "totp_identifier" in code, code
        assert "inbox@example.com" in code, code
        # The recorded link is single-use: replaying it would navigate to a spent token.
        assert "one-time-secret-value" not in code, code
        assert "url=" not in code, code

    def test_magic_link_falls_back_to_the_runtime_credential_identifier(self) -> None:
        act: dict[str, Any] = {
            "action_type": "goto_url",
            "url": "https://mail.example.com/signin?token=abc",
            "is_magic_link": True,
        }

        stmt = _action_to_stmt(
            act,
            {},
            goal_template="log in with {{ portal_credential }}",
            credential_param_keys=frozenset({"portal_credential"}),
        )
        code = _render(stmt)

        assert "page.magic_link(" in code, code
        assert "context.credential_totp_identifier(" in code, code
        assert "portal_credential" in code, code


class TestMagicLinkBlockGeneration:
    """Whole-block generation, not one statement: a recorded magic-link login must yield a
    cached script, since on main the same actions raised KeyError and the script was dropped."""

    @staticmethod
    def _build(actions: list[dict[str, Any]]) -> str:
        block = {
            "label": "sign_in",
            "block_type": "task",
            "navigation_goal": "Sign in to the member portal.",
            "parameters": [{"key": "portal_credential"}],
        }
        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.AGENT_FUNCTION.build_ats_pipeline_block_fn = MagicMock(return_value=None)
            fn = _build_block_fn(
                block=block,
                actions=actions,
                use_semantic_selectors=True,
                credential_param_keys=frozenset({"portal_credential"}),
            )
        return _render_fn(fn)

    def test_recorded_magic_link_login_generates_a_block_that_re_requests_the_link(self) -> None:
        code = self._build(
            [
                {
                    "action_type": "input_text",
                    "xpath": "//input[@id='email']",
                    "text": "demo_business_user",
                    "css_selector": "#email",
                    "attributes": {"id": "email"},
                },
                {
                    "action_type": "goto_url",
                    "url": "https://portal.example.com/verify?token=single-use-token-value",
                    "is_magic_link": True,
                },
            ]
        )

        assert "page.magic_link(" in code, code
        assert "context.credential_totp_identifier(" in code, code
        # The captured link is spent by replay time; emitting it would navigate to a dead token.
        assert "single-use-token-value" not in code, code

    def test_a_plain_recorded_navigation_still_generates_a_goto(self) -> None:
        recorded_url = "https://portal.example.com/dashboard"
        code = self._build([{"action_type": "goto_url", "url": recorded_url}])

        assert "page.goto(" in code, code
        # nosemgrep: incomplete-url-substring-sanitization  # searching generated source, not a URL
        assert recorded_url in code, code
        assert "page.magic_link(" not in code, code
