"""Regression tests: script-path ``ai_extract`` must honor the current block's
``ignore_workflow_system_prompt`` opt-out, matching agent-path behavior.

Without this, a cached script-path extraction still injects the workflow prompt
even when the block has opted out — the two execution modes diverge for the
same block (SKY-9147).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from skyvern.core.script_generations import real_skyvern_page_ai as module
from skyvern.core.script_generations.skyvern_page_ai import SYSTEM_PROMPT_UNSET
from skyvern.forge.sdk.workflow.models.block import ExtractionBlock
from skyvern.forge.sdk.workflow.models.parameter import OutputParameter, ParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition


def _make_output_parameter() -> OutputParameter:
    now = datetime.now(timezone.utc)
    return OutputParameter(
        parameter_type=ParameterType.OUTPUT,
        key="extract1_output",
        description="test output",
        output_parameter_id="op_extract1",
        workflow_id="w_test",
        created_at=now,
        modified_at=now,
    )


def _make_workflow_with_block(*, ignore_workflow_system_prompt: bool, workflow_system_prompt: str | None) -> Workflow:
    block = ExtractionBlock(
        label="extract1",
        output_parameter=_make_output_parameter(),
        data_extraction_goal="Extract things",
        ignore_workflow_system_prompt=ignore_workflow_system_prompt,
    )
    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id="w_test",
        organization_id="o_test",
        title="test",
        workflow_permanent_id="wpid_test",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(
            parameters=[],
            blocks=[block],
            workflow_system_prompt=workflow_system_prompt,
        ),
        created_at=now,
        modified_at=now,
    )


def _run_ai_extract(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow: Workflow | None,
    current_label: str | None,
    extra_kwargs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Drive ``RealSkyvernPageAi.ai_extract`` with mocks that capture both the
    cache-key ``system_prompt`` and the LLM-handler ``system_prompt``. Returns
    a dict with keys ``cache_system_prompt`` and ``llm_system_prompt``."""

    captured: dict[str, Any] = {}

    def fake_compute_cache_key(**kwargs: Any) -> str:
        captured["cache_system_prompt"] = kwargs.get("workflow_system_prompt")
        return "fake-cache-key"

    def fake_lookup(*_args: Any, **_kwargs: Any) -> None:
        # Cache miss — let the flow fall through to the handler.
        return None

    def fake_load_prompt(**_kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "rendered-prompt", {
            "extracted_text": None,
            "extracted_information_schema": None,
        }

    async def fake_handler(*, system_prompt: Any = None, **_ignored: Any) -> dict[str, Any]:
        captured["llm_system_prompt"] = system_prompt
        return {}

    # Build a minimal fake ``WorkflowRunContext``. The single-block workflows
    # this helper builds go through ``_apply_workflow_system_prompt`` in the
    # real script-path dispatch, so simulate that by pre-recording the block's
    # effective value — ``None`` when the block opts out, the rendered prompt
    # otherwise. ``ai_extract`` reads this recorded value verbatim, which is
    # exactly the contract Andrew's fix enforces.
    if workflow is not None:
        block = workflow.workflow_definition.blocks[0] if workflow.workflow_definition.blocks else None
        workflow_prompt = workflow.workflow_definition.workflow_system_prompt
        recorded_value: str | None = (
            None if (block is not None and getattr(block, "ignore_workflow_system_prompt", False)) else workflow_prompt
        )
        has_block_record = block is not None and current_label == block.label
        ctx = MagicMock()
        ctx.workflow = workflow
        ctx.resolve_effective_workflow_system_prompt = MagicMock(return_value=workflow_prompt)
        ctx.get_block_workflow_system_prompt = MagicMock(
            return_value=(has_block_record, recorded_value),
        )

        def get_run_ctx(_run_id: str) -> Any:
            return ctx

        monkeypatch.setattr(
            module.app.WORKFLOW_CONTEXT_MANAGER,
            "get_workflow_run_context",
            get_run_ctx,
        )

    skyvern_ctx = MagicMock()
    skyvern_ctx.workflow_run_id = "wr_test"
    skyvern_ctx.tz_info = None
    skyvern_ctx.organization_id = None
    skyvern_ctx.task_id = None
    skyvern_ctx.step_id = None
    skyvern_ctx.script_mode = False

    monkeypatch.setattr(module.skyvern_context, "current", lambda: skyvern_ctx)
    monkeypatch.setattr(module, "load_prompt_with_elements_tracked", fake_load_prompt)
    monkeypatch.setattr(module.extraction_cache, "compute_cache_key", fake_compute_cache_key)
    monkeypatch.setattr(module.extraction_cache, "lookup", fake_lookup)
    monkeypatch.setattr(module.app, "EXTRACTION_LLM_API_HANDLER", fake_handler)

    scraped_page = MagicMock()
    scraped_page.url = "https://example.test"
    scraped_page.extracted_text = "page text"
    scraped_page.screenshots = []
    scraped_page.build_element_tree = MagicMock(return_value="<a>link</a>")
    scraped_page.support_economy_elements_tree = MagicMock(return_value=False)
    scraped_page.last_used_element_tree_html = None

    page = module.RealSkyvernPageAi.__new__(module.RealSkyvernPageAi)
    page.scraped_page = scraped_page
    page.current_label = current_label

    async def fake_refresh(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(page, "_refresh_scraped_page", fake_refresh)

    asyncio.run(
        page.ai_extract(
            prompt="Extract things",
            schema={"type": "object"},
            **(extra_kwargs or {}),
        )
    )

    return captured


# ---------------------------------------------------------------------------
# The bug Andrew flagged: block opts out, but script-path still injects prompt.
# ---------------------------------------------------------------------------


def test_opted_out_block_sends_no_system_prompt_to_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=True,
        workflow_system_prompt="WORKFLOW RULES.",
    )
    captured = _run_ai_extract(monkeypatch, workflow=workflow, current_label="extract1")

    assert captured["llm_system_prompt"] is None


def test_opted_out_block_cache_key_omits_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache-key parity with agent path: an opted-out block's extractions must
    hash the same way whether the workflow has a prompt set or not. Otherwise a
    user toggling ``workflow_system_prompt`` at the workflow level silently
    invalidates caches for blocks that explicitly opted out."""
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=True,
        workflow_system_prompt="WORKFLOW RULES.",
    )
    captured = _run_ai_extract(monkeypatch, workflow=workflow, current_label="extract1")

    assert captured["cache_system_prompt"] is None


# ---------------------------------------------------------------------------
# Non-opted-out block still inherits the workflow prompt (no regression of the
# base feature).
# ---------------------------------------------------------------------------


def test_non_opted_out_block_receives_workflow_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=False,
        workflow_system_prompt="WORKFLOW RULES.",
    )
    captured = _run_ai_extract(monkeypatch, workflow=workflow, current_label="extract1")

    assert captured["llm_system_prompt"] == "WORKFLOW RULES."
    assert captured["cache_system_prompt"] == "WORKFLOW RULES."


# ---------------------------------------------------------------------------
# Explicit parameter overrides: caller can pass ``system_prompt`` directly,
# bypassing the block-flag lookup. Includes the "explicit None" escape hatch.
# ---------------------------------------------------------------------------


def test_explicit_system_prompt_overrides_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=False,
        workflow_system_prompt="WORKFLOW RULES.",
    )
    captured = _run_ai_extract(
        monkeypatch,
        workflow=workflow,
        current_label="extract1",
        extra_kwargs={"system_prompt": "EXPLICIT RULES."},
    )

    assert captured["llm_system_prompt"] == "EXPLICIT RULES."
    assert captured["cache_system_prompt"] == "EXPLICIT RULES."


def test_explicit_none_opts_out_even_when_workflow_has_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit ``None`` is the escape hatch: caller says "send no system prompt"
    regardless of the workflow or block state."""
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=False,
        workflow_system_prompt="WORKFLOW RULES.",
    )
    captured = _run_ai_extract(
        monkeypatch,
        workflow=workflow,
        current_label="extract1",
        extra_kwargs={"system_prompt": None},
    )

    assert captured["llm_system_prompt"] is None
    assert captured["cache_system_prompt"] is None


def test_sentinel_default_is_not_leaked_to_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sentinel must never reach the LLM handler — if the fallback path
    silently forwarded it, the handler would see a non-None, non-string object
    and fail or pass a bogus ``system_prompt`` to the model."""
    workflow = _make_workflow_with_block(
        ignore_workflow_system_prompt=False,
        workflow_system_prompt=None,  # no prompt at all
    )
    captured = _run_ai_extract(monkeypatch, workflow=workflow, current_label="extract1")

    assert captured["llm_system_prompt"] is None
    assert captured["llm_system_prompt"] is not SYSTEM_PROMPT_UNSET
    assert captured["cache_system_prompt"] is None
