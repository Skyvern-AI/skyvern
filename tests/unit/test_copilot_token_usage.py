"""Tests for copilot token-usage accumulation."""

from __future__ import annotations

from unittest.mock import MagicMock

from agents.run_context import RunContextWrapper
from agents.usage import InputTokensDetails, OutputTokensDetails, Usage

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import _accumulate_usage
from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config


def _make_ctx() -> CopilotContext:
    return CopilotContext(
        organization_id="o_1",
        workflow_id="w_1",
        workflow_permanent_id="wpid_1",
        workflow_yaml="",
        browser_session_id=None,
        stream=MagicMock(),
    )


def _result_with_usage(usage: Usage) -> MagicMock:
    wrapper = RunContextWrapper(context=None, usage=usage)
    result = MagicMock()
    result.context_wrapper = wrapper
    return result


def _usage(*, requests: int = 1, input_tokens: int = 0, output_tokens: int = 0) -> Usage:
    return Usage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=InputTokensDetails(cached_tokens=0),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
    )


def test_accumulate_usage_records_first_iteration_tokens() -> None:
    ctx = _make_ctx()
    assert ctx.total_tokens_used is None

    _accumulate_usage(_result_with_usage(_usage(input_tokens=120, output_tokens=80)), ctx)

    assert ctx.input_tokens_used == 120
    assert ctx.output_tokens_used == 80
    assert ctx.total_tokens_used == 200


def test_accumulate_usage_sums_across_enforcement_iterations() -> None:
    ctx = _make_ctx()

    _accumulate_usage(_result_with_usage(_usage(input_tokens=100, output_tokens=50)), ctx)
    _accumulate_usage(_result_with_usage(_usage(input_tokens=200, output_tokens=25)), ctx)

    assert ctx.input_tokens_used == 300
    assert ctx.output_tokens_used == 75
    assert ctx.total_tokens_used == 375


def test_accumulate_usage_leaves_none_when_provider_reports_no_usage() -> None:
    ctx = _make_ctx()

    _accumulate_usage(_result_with_usage(Usage()), ctx)

    assert ctx.total_tokens_used is None
    assert ctx.input_tokens_used is None
    assert ctx.output_tokens_used is None


def test_accumulate_usage_leaves_none_when_only_requests_counter_advanced() -> None:
    ctx = _make_ctx()

    _accumulate_usage(_result_with_usage(_usage(requests=1, input_tokens=0, output_tokens=0)), ctx)

    assert ctx.total_tokens_used is None
    assert ctx.input_tokens_used is None
    assert ctx.output_tokens_used is None


def test_accumulate_usage_handles_missing_context_wrapper() -> None:
    ctx = _make_ctx()
    result = MagicMock(spec=[])

    _accumulate_usage(result, ctx)

    assert ctx.total_tokens_used is None


def test_accumulate_usage_no_ops_on_context_without_token_fields() -> None:
    foreign_ctx = object()

    _accumulate_usage(_result_with_usage(_usage(input_tokens=10, output_tokens=5)), foreign_ctx)

    assert not hasattr(foreign_ctx, "total_tokens_used")


def test_resolve_model_config_sets_include_usage_true() -> None:
    handler = MagicMock()
    handler.llm_key = None

    _, run_config, _, _ = resolve_model_config(handler)

    assert run_config.model_settings.include_usage is True
