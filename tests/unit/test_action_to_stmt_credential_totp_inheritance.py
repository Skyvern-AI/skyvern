"""SKY-9822 regression tests: generator emits a runtime credential TOTP lookup
when the block has no explicit totp_identifier but a credential is in scope."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import libcst as cst

from skyvern.core.script_generations.generate_script import _action_to_stmt, _build_block_fn


def _render(stmt: cst.BaseStatement) -> str:
    return cst.Module(body=[stmt]).code


def _render_fn(fn: cst.FunctionDef) -> str:
    return cst.Module(body=[fn]).code


def _make_input_action(*, totp_code_required: bool = True) -> dict[str, Any]:
    return {
        "action_type": "input_text",
        "xpath": "//input[@id='InputOTPCode']",
        "text": "476981",
        "totp_code_required": totp_code_required,
        "css_selector": "#InputOTPCode",
        "attributes": {"id": "InputOTPCode"},
    }


class TestActionToStmtCredentialTOTPInheritance:
    def test_emits_credential_totp_call_when_block_has_no_identifier_and_one_credential(self) -> None:
        # Workflow definition's OTP-entry block has no explicit totp_identifier,
        # but the run has a single credential parameter whose underlying
        # credential carries a TOTP identifier. Generator must emit a runtime
        # lookup against that credential rather than skipping.
        act = _make_input_action()
        task: dict[str, Any] = {}  # block-level totp_identifier / totp_verification_url absent

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"SiteCredentials"}),
        )
        code = _render(stmt)

        assert "totp_identifier" in code, f"totp_identifier kwarg missing:\n{code}"
        assert "context.credential_totp_identifier" in code, f"credential lookup call missing:\n{code}"
        assert "'SiteCredentials'" in code, f"credential key missing:\n{code}"

    def test_no_emission_when_zero_credentials(self) -> None:
        # No credential to look up — generator should not invent a call. Same
        # as today's behavior; preserves backcompat for workflows without
        # credential params.
        act = _make_input_action()
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset(),
        )
        code = _render(stmt)

        assert "credential_totp_identifier" not in code, (
            f"unexpected credential lookup emitted with no credentials:\n{code}"
        )

    def test_no_emission_when_multiple_credentials_no_jinja_reference(self) -> None:
        # Multiple credentials AND no Jinja reference in the goal_template to
        # disambiguate — skip the emit so we don't pick a credential
        # arbitrarily. The customer can disambiguate by setting
        # `totp_identifier=` explicitly on the block. Mirrors the agentic
        # safety in `try_generate_totp_from_credential`'s multi-no-active
        # branch.
        act = _make_input_action()
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"CredA", "CredB"}),
        )
        code = _render(stmt)

        assert "credential_totp_identifier" not in code, (
            f"unexpected credential lookup emitted with multi-credential ambiguity:\n{code}"
        )

    def test_picks_correct_credential_when_block_goal_template_jinja_references_one(self) -> None:
        # Real-world shape: workflow has two login blocks each consuming a
        # different credential. The block's goal_template references THIS
        # block's credential via `{{Cred1}}`, so we disambiguate by the
        # Jinja root referenced in the goal. Without this, multi-login
        # workflows skip emission entirely.
        act = _make_input_action()
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            goal_template="Enter the 2FA code.\n{'creds': '{{Cred1}}'}",
            credential_param_keys=frozenset({"Cred1", "Cred2"}),
        )
        code = _render(stmt)

        assert "context.credential_totp_identifier" in code, (
            f"credential lookup missing when Jinja reference disambiguates:\n{code}"
        )
        assert "'Cred1'" in code, f"wrong credential key chosen:\n{code}"
        assert "'Cred2'" not in code, f"unreferenced credential key emitted:\n{code}"

    def test_no_emission_when_multiple_credentials_and_both_jinja_referenced(self) -> None:
        # Both credentials referenced in the goal — still ambiguous, refuse
        # to pick. Customer has set up a flow where one block touches both
        # credentials; can't safely guess which one owns the OTP.
        act = _make_input_action()
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            goal_template="Use {{CredA}} or {{CredB}} to log in.",
            credential_param_keys=frozenset({"CredA", "CredB"}),
        )
        code = _render(stmt)

        assert "credential_totp_identifier" not in code, (
            f"unexpected credential lookup with both-referenced ambiguity:\n{code}"
        )

    def test_block_level_identifier_takes_precedence(self) -> None:
        # If the customer DID set totp_identifier on the block (e.g. a
        # parameter reference like 'email'), keep emitting that literal. The
        # credential auto-resolve is a fallback for blocks the customer didn't
        # explicitly configure — it must not override authored config.
        act = _make_input_action()
        task = {"totp_identifier": "email"}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"SiteCredentials"}),
        )
        code = _render(stmt)

        # CST emits kwargs as `name = value` (with surrounding spaces). Look for
        # the identifier kwarg and the literal value rather than a glued form.
        assert "totp_identifier" in code and "'email'" in code, (
            f"customer's authored block-level identifier should be preserved:\n{code}"
        )
        assert "credential_totp_identifier" not in code, (
            f"credential lookup must not override block-level identifier:\n{code}"
        )

    def test_url_only_block_does_not_get_credential_emission(self) -> None:
        # Customer configured totp_verification_url but not totp_identifier.
        # The URL alone is sufficient — runtime polls via URL. Don't also emit
        # a credential identifier, which would compete.
        act = _make_input_action()
        task = {"totp_verification_url": "https://example.invalid/totp"}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"SiteCredentials"}),
        )
        code = _render(stmt)

        assert "totp_url" in code, f"existing totp_url emission must survive:\n{code}"
        assert "credential_totp_identifier" not in code, (
            f"credential lookup must not be added when URL is configured:\n{code}"
        )

    def test_no_emission_when_totp_code_not_required(self) -> None:
        # The whole emission block is gated on totp_code_required=True. A
        # regular username/password fill with a credential param in scope
        # must NOT get a TOTP identifier injected.
        act = _make_input_action(totp_code_required=False)
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"SiteCredentials"}),
        )
        code = _render(stmt)

        assert "totp_identifier" not in code, f"non-TOTP fill got TOTP identifier injected:\n{code}"

    def test_per_block_filtering_picks_block_bound_credential_without_jinja(self) -> None:
        # Multi-login workflow: two task blocks, each binds ONE credential via
        # block.parameters but neither references it in goal_template. Workflow-
        # wide credential_param_keys = {Cred1, Cred2} would skip emission; per-
        # block filtering selects each block's own credential. Mirrors the
        # agentic walk over `block.get_all_parameters(...)`.
        block = {
            "label": "login_a",
            "block_type": "task",
            "navigation_goal": "Enter the 2FA code.",
            "parameters": [{"key": "Cred1"}],
        }
        actions = [_make_input_action()]

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.AGENT_FUNCTION.build_ats_pipeline_block_fn = MagicMock(return_value=None)
            fn = _build_block_fn(
                block=block,
                actions=actions,
                use_semantic_selectors=True,
                credential_param_keys=frozenset({"Cred1", "Cred2"}),
            )

        code = _render_fn(fn)
        assert "context.credential_totp_identifier" in code, (
            f"per-block-bound credential should drive emission:\n{code}"
        )
        assert "'Cred1'" in code, f"block's bound credential 'Cred1' missing:\n{code}"
        assert "'Cred2'" not in code, f"unrelated workflow credential 'Cred2' must not leak:\n{code}"

    def test_per_block_filtering_skips_when_block_binds_no_credential(self) -> None:
        # Block has no credential parameter bound — matches the agentic case
        # where get_all_parameters returns no credential, so nothing inherits.
        # Generator must skip emission even if other blocks in the workflow
        # bind credentials.
        block = {
            "label": "otp_only",
            "block_type": "task",
            "navigation_goal": "Enter the 2FA code.",
            "parameters": [{"key": "SomeOtherParam"}],
        }
        actions = [_make_input_action()]

        with patch("skyvern.core.script_generations.generate_script.app") as mock_app:
            mock_app.AGENT_FUNCTION.build_ats_pipeline_block_fn = MagicMock(return_value=None)
            fn = _build_block_fn(
                block=block,
                actions=actions,
                use_semantic_selectors=True,
                credential_param_keys=frozenset({"Cred1"}),
            )

        code = _render_fn(fn)
        assert "credential_totp_identifier" not in code, (
            f"block with no credential binding must not inherit from workflow scope:\n{code}"
        )

    def test_no_emission_when_is_totp_sequence(self) -> None:
        # Multi-field OTP forms split the 6-digit code across single-digit
        # inputs. Each fill receives one digit via get_totp_digit. Emitting a
        # totp_identifier on these fills triggers the runtime poll path, which
        # overwrites the single-digit value with the full code — corrupting
        # every input. Skip emission for is_totp_sequence actions.
        act = _make_input_action()
        act["totp_timing_info"] = {"is_totp_sequence": True, "action_index": 0}
        task: dict[str, Any] = {}

        stmt = _action_to_stmt(
            act,
            task,
            use_semantic_selectors=True,
            credential_param_keys=frozenset({"SiteCredentials"}),
        )
        code = _render(stmt)

        assert "credential_totp_identifier" not in code, (
            f"is_totp_sequence fills must not get credential lookup (would overwrite single-digit value):\n{code}"
        )
