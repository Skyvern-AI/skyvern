"""Tests for multi-placeholder secret resolution in WorkflowRunContext."""

from __future__ import annotations

import pytest

from skyvern.exceptions import ImaginarySecretValue
from skyvern.forge.sdk.workflow.context_manager import WorkflowRunContext


def _make_context(secrets: dict[str, str]) -> WorkflowRunContext:
    ctx = WorkflowRunContext.__new__(WorkflowRunContext)
    ctx.secrets = dict(secrets)
    ctx.values = {}
    ctx.parameters = {}
    ctx.credential_totp_identifiers = {}
    return ctx


class TestSinglePlaceholderRegression:
    def test_exact_match_resolves(self) -> None:
        ctx = _make_context({"placeholder_VAZA_card_number": "4111111111111111"})
        assert ctx.get_original_secret_value_or_none("placeholder_VAZA_card_number") == "4111111111111111"

    def test_unknown_single_placeholder_raises(self) -> None:
        ctx = _make_context({"placeholder_VAZA_card_number": "4111111111111111"})
        with pytest.raises(ImaginarySecretValue):
            ctx.get_original_secret_value_or_none("placeholder_XXXX_unknown")

    def test_non_placeholder_string_returns_none(self) -> None:
        ctx = _make_context({"placeholder_VAZA_card_number": "4111111111111111"})
        assert ctx.get_original_secret_value_or_none("some_regular_text") is None

    def test_empty_secrets_returns_none(self) -> None:
        ctx = _make_context({})
        assert ctx.get_original_secret_value_or_none("placeholder_VAZA_card_number") is None


class TestMultiPlaceholderResolution:
    def test_combined_month_year_resolves_with_separator(self) -> None:
        ctx = _make_context(
            {
                "placeholder_zqHf_card_exp_month": "05",
                "placeholder_CDFg_card_exp_year": "29",
            }
        )
        result = ctx.get_original_secret_value_or_none(
            "placeholder_zqHf_card_exp_month / placeholder_CDFg_card_exp_year"
        )
        assert result == "05 / 29"

    def test_combined_without_spaces(self) -> None:
        ctx = _make_context(
            {
                "placeholder_aaaa_month": "12",
                "placeholder_bbbb_year": "2027",
            }
        )
        result = ctx.get_original_secret_value_or_none("placeholder_aaaa_month/placeholder_bbbb_year")
        assert result == "12/2027"

    def test_repeated_placeholder_replaced_consistently(self) -> None:
        ctx = _make_context({"placeholder_aaaa_val": "hello"})
        result = ctx.get_original_secret_value_or_none("placeholder_aaaa_val and placeholder_aaaa_val")
        assert result == "hello and hello"

    def test_overlapping_keys_chooses_longest(self) -> None:
        ctx = _make_context(
            {
                "placeholder_ab_x": "short",
                "placeholder_ab_x_extended": "long",
            }
        )
        result = ctx.get_original_secret_value_or_none("placeholder_ab_x_extended")
        assert result == "long"

    def test_mixed_known_unknown_starting_with_placeholder_raises(self) -> None:
        ctx = _make_context({"placeholder_aaaa_month": "05"})
        with pytest.raises(ImaginarySecretValue):
            ctx.get_original_secret_value_or_none("placeholder_aaaa_month / placeholder_XXXX_unknown")

    def test_mixed_known_unknown_with_prefix_text_raises(self) -> None:
        ctx = _make_context({"placeholder_aaaa_month": "05"})
        with pytest.raises(ImaginarySecretValue):
            ctx.get_original_secret_value_or_none("Prefix placeholder_aaaa_month / placeholder_XXXX_unknown")

    def test_preserves_separator_and_trailing_text(self) -> None:
        ctx = _make_context(
            {
                "placeholder_aaaa_first": "John",
                "placeholder_bbbb_last": "Doe",
            }
        )
        result = ctx.get_original_secret_value_or_none("placeholder_aaaa_first - placeholder_bbbb_last")
        assert result == "John - Doe"

    def test_preserves_surrounding_text(self) -> None:
        ctx = _make_context(
            {
                "placeholder_aaaa_first": "John",
                "placeholder_bbbb_last": "Doe",
            }
        )
        result = ctx.get_original_secret_value_or_none("Name: placeholder_aaaa_first placeholder_bbbb_last (verified)")
        assert result == "Name: John Doe (verified)"


class TestActiveCredentialParameterKey:
    def test_single_credential_sets_key(self) -> None:
        from unittest.mock import MagicMock, patch

        from skyvern.forge.sdk.core import skyvern_context
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext

        ctx = _make_context(
            {
                "placeholder_aaaa_month": "05",
                "placeholder_bbbb_year": "29",
            }
        )
        ctx.values = {
            "credit_card_cred": {
                "card_exp_month": "placeholder_aaaa_month",
                "card_exp_year": "placeholder_bbbb_year",
            },
        }
        ctx.parameters = {"credit_card_cred": MagicMock()}

        from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

        ctx.parameters["credit_card_cred"].__class__ = CredentialParameter

        sky_ctx = SkyvernContext()
        skyvern_context.set(sky_ctx)
        try:
            with patch("skyvern.webeye.actions.handler.app") as mock_app:
                mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx

                from skyvern.webeye.actions.handler import get_actual_value_of_parameter_if_secret

                result = get_actual_value_of_parameter_if_secret(
                    "wr_test", "placeholder_aaaa_month / placeholder_bbbb_year"
                )
                assert result == "05 / 29"
                assert sky_ctx.active_credential_parameter_key == "credit_card_cred"
        finally:
            skyvern_context.reset()

    def test_cross_credential_does_not_set_key(self) -> None:
        from unittest.mock import MagicMock, patch

        from skyvern.forge.sdk.core import skyvern_context
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
        from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter

        ctx = _make_context(
            {
                "placeholder_aaaa_month": "05",
                "placeholder_bbbb_year": "29",
            }
        )
        ctx.values = {
            "cred_A": {"card_exp_month": "placeholder_aaaa_month"},
            "cred_B": {"card_exp_year": "placeholder_bbbb_year"},
        }
        ctx.parameters = {
            "cred_A": MagicMock(),
            "cred_B": MagicMock(),
        }
        ctx.parameters["cred_A"].__class__ = CredentialParameter
        ctx.parameters["cred_B"].__class__ = CredentialParameter

        sky_ctx = SkyvernContext()
        skyvern_context.set(sky_ctx)
        try:
            with patch("skyvern.webeye.actions.handler.app") as mock_app:
                mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.return_value = ctx

                from skyvern.webeye.actions.handler import get_actual_value_of_parameter_if_secret

                result = get_actual_value_of_parameter_if_secret(
                    "wr_test", "placeholder_aaaa_month / placeholder_bbbb_year"
                )
                assert result == "05 / 29"
                assert sky_ctx.active_credential_parameter_key is None
        finally:
            skyvern_context.reset()


class TestTaskV1Guard:
    def test_task_v1_does_not_use_embedded_resolver(self) -> None:
        from dataclasses import dataclass

        from skyvern.forge.sdk.core import skyvern_context
        from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
        from skyvern.webeye.actions.handler import get_actual_value_of_parameter_if_secret_with_task

        @dataclass
        class _FakeTask:
            workflow_run_id: str | None = None

        task = _FakeTask(workflow_run_id=None)
        ctx = SkyvernContext()
        skyvern_context.set(ctx)
        try:
            result = get_actual_value_of_parameter_if_secret_with_task(
                task,  # type: ignore[arg-type]
                "placeholder_aaaa_month / placeholder_bbbb_year",
            )
            assert result == "placeholder_aaaa_month / placeholder_bbbb_year"
        finally:
            skyvern_context.reset()
