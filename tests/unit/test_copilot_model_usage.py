from __future__ import annotations

import asyncio
from dataclasses import fields as dataclass_fields
from typing import Any

import pytest

from skyvern.forge.sdk.api.llm.copilot_model_usage import (
    CopilotModelUsageEvent,
    _emit_copilot_model_usage,
    _emit_direct_copilot_model_usage,
    is_workflow_copilot_prompt_name,
    normalize_gen_ai_provider,
)
from skyvern.forge.sdk.copilot.cost_telemetry import (
    copilot_cost_scope,
    current_copilot_cost,
    exclude_workflow_block_cost,
)


class CapturingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


@pytest.mark.parametrize(
    "prompt_name",
    [
        "workflow-copilot",
        "workflow-copilot-narration",
        "workflow-copilot-page-evidence-vision",
        "workflow-copilot-raw-secret-safety",
        "workflow-copilot-future-call",
    ],
)
def test_workflow_copilot_prompt_namespace_accepts_exact_and_prefixed_names(prompt_name: str) -> None:
    assert is_workflow_copilot_prompt_name(prompt_name)


@pytest.mark.parametrize(
    "prompt_name",
    [None, "", "workflow", "workflow-copilotish", "other-workflow-copilot", "check-user-goal"],
)
def test_workflow_copilot_prompt_namespace_rejects_unrelated_names(prompt_name: str | None) -> None:
    assert not is_workflow_copilot_prompt_name(prompt_name)


def test_direct_event_preserves_zeroes_and_omits_unavailable_optional_fields() -> None:
    logger = CapturingLogger()
    event = CopilotModelUsageEvent(
        request_model="openai/gpt-5.6-sol",
        response_model="gpt-5.6-sol-2026-07-09",
        provider_name="openai",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_creation_tokens=None,
        cost=0.0,
        prompt_name="workflow-copilot-narration",
    )

    assert _emit_direct_copilot_model_usage(event, logger=logger)

    assert logger.events == [
        (
            "Copilot model usage",
            {
                "log_code": "copilot_model_usage",
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "openai/gpt-5.6-sol",
                "gen_ai.response.model": "gpt-5.6-sol-2026-07-09",
                "gen_ai.provider.name": "openai",
                "gen_ai.usage.input_tokens": 0,
                "gen_ai.usage.output_tokens": 0,
                "gen_ai.usage.cache_read.input_tokens": 0,
                "operation.cost": 0.0,
                "copilot.prompt_name": "workflow-copilot-narration",
            },
        )
    ]


def test_direct_event_is_a_closed_content_free_scalar_schema() -> None:
    logger = CapturingLogger()
    event = CopilotModelUsageEvent(
        request_model="vertex_ai/gemini-3-flash-preview",
        response_model="gemini-3-flash-preview",
        provider_name="vertex_ai",
        input_tokens=12,
        output_tokens=3,
        cache_creation_tokens=4,
        prompt_name="workflow-copilot-raw-secret-safety",
    )

    _emit_direct_copilot_model_usage(event, logger=logger)

    fields = logger.events[0][1]
    assert set(fields) == {
        "log_code",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.provider.name",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.cache_creation.input_tokens",
        "copilot.prompt_name",
    }
    assert all(isinstance(value, (str, int, float)) for value in fields.values())
    assert {field.name for field in dataclass_fields(CopilotModelUsageEvent)} == {
        "request_model",
        "response_model",
        "provider_name",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "cost",
        "cost_source",
        "prompt_name",
        "model_call_index",
        "cache_mode",
        "cache_breakpoint_count",
        "cache_stable_prefix_chars",
        "ref_tool_outputs_escaped",
    }
    assert normalize_gen_ai_provider(event.provider_name, event.response_model) == "gcp.vertex_ai"


def test_cost_ledger_records_actor_and_safety_cost_without_filling_unknowns() -> None:
    logger = CapturingLogger()
    with copilot_cost_scope():
        _emit_copilot_model_usage(
            CopilotModelUsageEvent(request_model="model", cost=0.03, cost_source="litellm_estimate"), logger=logger
        )
        _emit_direct_copilot_model_usage(
            CopilotModelUsageEvent(
                request_model="model",
                prompt_name="workflow-copilot-raw-secret-safety",
                cost=0.0,
                cost_source="provider_reported",
            ),
            logger=logger,
        )
        _emit_copilot_model_usage(CopilotModelUsageEvent(request_model="model"), logger=logger)
        snapshot = current_copilot_cost()
        assert snapshot is not None
        assert snapshot["known_cost_usd"] == pytest.approx(0.03)
        assert snapshot["status"] == "partial"
        assert snapshot["observed_call_count"] == 3
        assert snapshot["priced_call_count"] == 2
        assert snapshot["unknown_call_count"] == 1
        assert snapshot["pricing_sources"] == ["litellm_estimate", "provider_reported"]
        assert "workflow_replay" in snapshot["excluded"]
    assert current_copilot_cost() is None


@pytest.mark.asyncio
async def test_cost_ledger_isolates_concurrent_turns_and_closes_inherited_background_scope() -> None:
    logger = CapturingLogger()
    background_ready = asyncio.Event()
    background_release = asyncio.Event()

    async def record_later() -> None:
        background_ready.set()
        await background_release.wait()
        _emit_copilot_model_usage(
            CopilotModelUsageEvent(request_model="model", cost=10.0, cost_source="litellm_estimate"), logger=logger
        )
        assert current_copilot_cost() is None

    async def turn(amount: float) -> dict[str, object] | None:
        with copilot_cost_scope():
            _emit_copilot_model_usage(
                CopilotModelUsageEvent(request_model="model", cost=amount, cost_source="litellm_estimate"),
                logger=logger,
            )
            await asyncio.sleep(0)
            return current_copilot_cost()

    left, right = await asyncio.gather(turn(0.1), turn(0.2))
    assert left is not None and left["known_cost_usd"] == pytest.approx(0.1)
    assert right is not None and right["known_cost_usd"] == pytest.approx(0.2)
    with copilot_cost_scope() as ledger:
        task = asyncio.create_task(record_later())
        await background_ready.wait()
    background_release.set()
    await task
    assert ledger.observed_call_count == 0


@pytest.mark.asyncio
async def test_workflow_block_tasks_cannot_contribute_to_the_parent_copilot_cost() -> None:
    logger = CapturingLogger()

    async def execute_block() -> None:
        await asyncio.sleep(0)
        assert current_copilot_cost() is None
        _emit_copilot_model_usage(
            CopilotModelUsageEvent(request_model="model", cost=100.0, cost_source="litellm_estimate"), logger=logger
        )
        _emit_direct_copilot_model_usage(
            CopilotModelUsageEvent(
                request_model="model",
                prompt_name="workflow-copilot-raw-secret-safety",
                cost=20.0,
                cost_source="litellm_estimate",
            ),
            logger=logger,
        )

    with copilot_cost_scope() as ledger:
        with exclude_workflow_block_cost():
            task = asyncio.create_task(execute_block())
        _emit_direct_copilot_model_usage(
            CopilotModelUsageEvent(
                request_model="model",
                prompt_name="workflow-copilot-raw-secret-safety",
                cost=0.01,
                cost_source="litellm_estimate",
            ),
            logger=logger,
        )
        await task
        assert ledger.snapshot()["known_cost_usd"] == pytest.approx(0.01)
        assert ledger.snapshot()["observed_call_count"] == 1
        assert "workflow_block_execution" in ledger.snapshot()["excluded"]


@pytest.mark.parametrize("cost", [None, -1.0, float("nan"), float("inf"), True])
def test_cost_ledger_preserves_invalid_cost_as_unknown(cost: float | None) -> None:
    with copilot_cost_scope() as ledger:
        ledger.record(CopilotModelUsageEvent(request_model="model", cost=cost, cost_source="litellm_estimate"))
        assert ledger.snapshot()["known_cost_usd"] is None
        assert ledger.snapshot()["unknown_call_count"] == 1


def test_event_provider_falls_back_to_qualified_request_model() -> None:
    event = CopilotModelUsageEvent(
        request_model="anthropic/claude-sonnet-4-5",
        response_model="claude-sonnet-4-5",
    )

    assert event.log_fields()["gen_ai.provider.name"] == "anthropic"


def test_qualified_request_provider_precedes_bare_response_model_inference() -> None:
    event = CopilotModelUsageEvent(
        request_model="azure/gpt-4.1",
        response_model="gpt-4.1",
    )

    assert event.log_fields()["gen_ai.provider.name"] == "azure.ai.openai"


def test_qualified_response_provider_precedes_request_model_fallback() -> None:
    event = CopilotModelUsageEvent(
        request_model="azure/gpt-4.1",
        response_model="anthropic/claude-sonnet-4-6",
    )

    assert event.log_fields()["gen_ai.provider.name"] == "anthropic"


def test_unrelated_direct_event_does_not_log() -> None:
    logger = CapturingLogger()
    event = CopilotModelUsageEvent(
        request_model="gpt-4.1-mini",
        prompt_name="extract-actions",
    )

    assert not _emit_direct_copilot_model_usage(event, logger=logger)
    assert logger.events == []


@pytest.mark.parametrize(
    ("provider_name", "model", "expected"),
    [
        ("vertex_ai", "gemini-3-flash-preview", "gcp.vertex_ai"),
        ("bedrock", "anthropic.claude", "aws.bedrock"),
        ("Azure", "gpt-5.6", "azure.ai.openai"),
        ("Cloudflare", "anthropic/claude-sonnet-4-6", "anthropic"),
        (None, "openai/gpt-5.6", "openai"),
        (None, "unknown-model", None),
    ],
)
def test_provider_normalization_prefers_known_provider_or_response_model(
    provider_name: str | None, model: str, expected: str | None
) -> None:
    assert normalize_gen_ai_provider(provider_name, model) == expected
