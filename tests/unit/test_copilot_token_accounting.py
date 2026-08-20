"""Tests for copilot token accounting: estimation and usage accumulation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from agents.run_context import RunContextWrapper
from agents.usage import InputTokensDetails, OutputTokensDetails, Usage

from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    TOKENS_PER_RESIZED_IMAGE,
    _accumulate_usage,
    _sanitize_for_token_estimation,
    estimate_tokens,
)
from skyvern.forge.sdk.copilot.model_resolver import resolve_model_config


class TestEstimateTokens:
    def test_empty_input(self):
        assert estimate_tokens([]) == 0

    def test_plain_text_items(self):
        items = [
            {"role": "user", "content": "Hello, how are you?"},
            {"role": "assistant", "content": "I am fine, thanks."},
        ]
        result = estimate_tokens(items)
        assert result > 0
        assert isinstance(result, int)

    def test_image_estimate_independent_of_base64_size(self):
        """Tiny vs huge base64 must produce the exact same estimate."""
        small = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,abc",
                "detail": "high",
            }
        ]
        large = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + "A" * 200_000,
                "detail": "high",
            }
        ]
        assert estimate_tokens(small) == estimate_tokens(large)

    def test_mixed_text_and_images(self):
        items = [
            {"role": "user", "content": "Describe this image."},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Here is a screenshot."},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + "A" * 10000,
                        "detail": "high",
                    },
                ],
            },
        ]
        result = estimate_tokens(items)
        assert result >= TOKENS_PER_RESIZED_IMAGE
        assert result > 10

    def test_base64_excluded_from_text_tokens(self):
        """The text-token component must not include base64 data.

        We subtract the flat image cost to isolate the text-token portion
        and verify it stays small regardless of image_url size.
        """
        items = [
            {
                "type": "input_image",
                "image_url": "data:image/png;base64," + "A" * 200_000,
                "detail": "high",
            }
        ]
        total = estimate_tokens(items)
        text_portion = total - TOKENS_PER_RESIZED_IMAGE
        # The sanitized JSON is ~60 chars ('[{"type":"input_image","image_url":"[image]","detail":"high"}]')
        # so text tokens should be well under 100.
        assert text_portion < 100

    def test_image_metadata_preserved(self):
        """type and detail fields should be included, only image_url replaced."""
        item = {
            "type": "input_image",
            "image_url": "data:image/png;base64," + "X" * 1000,
            "detail": "high",
        }
        sanitized, count = _sanitize_for_token_estimation(item)
        assert count == 1
        assert sanitized["type"] == "input_image"
        assert sanitized["detail"] == "high"
        assert sanitized["image_url"] == "[image]"

    def test_nested_dict_structures(self):
        items = [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "outer"},
                    {"nested": {"deep": {"value": "hello"}}},
                ],
            }
        ]
        result = estimate_tokens(items)
        assert result > 0

    def test_structure_overhead(self):
        """Structured payload should estimate higher than leaf strings alone."""
        leaf_text = "hello world"
        structured = [{"key1": leaf_text, "key2": {"nested_key": leaf_text}}]
        plain = [leaf_text]
        structured_tokens = estimate_tokens(structured)
        plain_tokens = estimate_tokens(plain)
        assert structured_tokens > plain_tokens

    def test_non_serializable_object(self):
        """Custom objects should not raise; handled via str() fallback."""

        class Custom:
            def __str__(self) -> str:
                return "custom-object-repr"

        items = [{"data": Custom(), "text": "normal"}]
        result = estimate_tokens(items)
        assert result > 0


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


class TestAccumulateUsage:
    def test_accumulate_usage_records_first_iteration_tokens(self) -> None:
        ctx = _make_ctx()
        assert ctx.total_tokens_used is None

        _accumulate_usage(_result_with_usage(_usage(input_tokens=120, output_tokens=80)), ctx)

        assert ctx.input_tokens_used == 120
        assert ctx.output_tokens_used == 80
        assert ctx.total_tokens_used == 200

    def test_accumulate_usage_sums_across_enforcement_iterations(self) -> None:
        ctx = _make_ctx()

        _accumulate_usage(_result_with_usage(_usage(input_tokens=100, output_tokens=50)), ctx)
        _accumulate_usage(_result_with_usage(_usage(input_tokens=200, output_tokens=25)), ctx)

        assert ctx.input_tokens_used == 300
        assert ctx.output_tokens_used == 75
        assert ctx.total_tokens_used == 375

    def test_accumulate_usage_leaves_none_when_provider_reports_no_usage(self) -> None:
        ctx = _make_ctx()

        _accumulate_usage(_result_with_usage(Usage()), ctx)

        assert ctx.total_tokens_used is None
        assert ctx.input_tokens_used is None
        assert ctx.output_tokens_used is None

    def test_accumulate_usage_leaves_none_when_only_requests_counter_advanced(self) -> None:
        ctx = _make_ctx()

        _accumulate_usage(_result_with_usage(_usage(requests=1, input_tokens=0, output_tokens=0)), ctx)

        assert ctx.total_tokens_used is None
        assert ctx.input_tokens_used is None
        assert ctx.output_tokens_used is None

    def test_accumulate_usage_handles_missing_context_wrapper(self) -> None:
        ctx = _make_ctx()
        result = MagicMock(spec=[])

        _accumulate_usage(result, ctx)

        assert ctx.total_tokens_used is None

    def test_accumulate_usage_no_ops_on_context_without_token_fields(self) -> None:
        foreign_ctx = object()

        _accumulate_usage(_result_with_usage(_usage(input_tokens=10, output_tokens=5)), foreign_ctx)

        assert not hasattr(foreign_ctx, "total_tokens_used")


def test_resolve_model_config_sets_include_usage_true(monkeypatch: pytest.MonkeyPatch) -> None:
    # settings.LLM_KEY defaults to an OpenAI registry alias that isn't registered when
    # ENABLE_OPENAI is off (bare unit env); point it at a resolvable litellm model so the
    # None-llm_key fallback path resolves without tripping the SKY-12322 alias guard.
    monkeypatch.setattr("skyvern.forge.sdk.copilot.model_resolver.settings.LLM_KEY", "gpt-4o")
    handler = MagicMock()
    handler.llm_key = None

    _, run_config, _, _ = resolve_model_config(handler)

    assert run_config.model_settings.include_usage is True
