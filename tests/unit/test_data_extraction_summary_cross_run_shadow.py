"""Shadow scheduling for data-extraction-summary cross-run cache hits.

Pins helper argument forwarding, logger discriminator binding, gate
suppression, and that ``ForgeAgent.create_extract_action`` schedules a
shadow on cross-run hits and only on cross-run hits.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from skyvern.forge import agent as agent_module
from skyvern.forge.sdk.cache import extraction_shadow


def _make_task() -> MagicMock:
    task = MagicMock()
    task.workflow_run_id = "wfr_summary_shadow"
    task.workflow_permanent_id = "wpid_summary_shadow"
    task.task_id = "tsk_summary_shadow"
    task.organization_id = "o_test"
    task.workflow_system_prompt = "system-prompt-for-test"
    task.llm_key = None
    task.data_extraction_goal = "Extract documents"
    task.extracted_information_schema = {"type": "object"}
    return task


def _install_capturing_summary_llm(monkeypatch) -> list[dict]:
    """Replace ``app.EXTRACTION_LLM_API_HANDLER`` with a capturing async stub.

    Matches the miss path's invocation pattern at ``create_extract_action`` —
    the helper calls the global handler directly, NOT through
    ``get_override_llm_api_handler``, so the test must stub the global to
    observe forwarded kwargs.
    """
    captured: list[dict] = []

    async def capturing_llm(**kwargs: object) -> dict:
        captured.append(kwargs)
        return {"summary": "shadow_summary"}

    monkeypatch.setattr(agent_module.app, "EXTRACTION_LLM_API_HANDLER", capturing_llm)
    return captured


def test_summary_helper_schedules_shadow_with_summary_prompt(monkeypatch) -> None:
    captured_llm_kwargs = _install_capturing_summary_llm(monkeypatch)
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=True),
    )
    schedule_mock = MagicMock(return_value=None)
    monkeypatch.setattr(agent_module.extraction_shadow, "schedule_shadow_check", schedule_mock)

    task = _make_task()
    cached_value = {"summary": "cached_summary"}

    agent_module._schedule_summary_shadow_check_for_hit(
        task=task,
        workflow_run_id=task.workflow_run_id,
        cache_key="ck_summary",
        cached_value=cached_value,
        cached_age_seconds=extraction_shadow.UNKNOWN_CACHE_AGE_SENTINEL,
        summary_prompt="rendered-summary-prompt",
    )

    assert schedule_mock.call_count == 1
    kwargs = schedule_mock.call_args.kwargs
    assert kwargs["cache_key"] == "ck_summary"
    assert kwargs["workflow_run_id"] == task.workflow_run_id
    assert kwargs["cached_value"] == cached_value
    assert kwargs["cached_age_seconds"] == -1.0
    # Schema is None because the summary cache key omits schema-shape inputs;
    # strict mode is correct.
    assert kwargs["schema"] is None

    asyncio.run(kwargs["llm_call"]())
    assert len(captured_llm_kwargs) == 1
    forwarded = captured_llm_kwargs[0]
    assert forwarded["prompt"] == "rendered-summary-prompt"
    assert forwarded["prompt_name"] == "data-extraction-summary"
    assert forwarded["system_prompt"] == task.workflow_system_prompt
    assert forwarded["step"] is None
    # Summary path has no screenshots in the cache key, so passing them would
    # diverge from the miss-path invocation at agent.py:create_extract_action.
    assert "screenshots" not in forwarded


def test_summary_helper_binds_prompt_name_and_cache_path_on_logger(monkeypatch) -> None:
    """Shadow events must carry a prompt/cache discriminator so Datadog can split
    the shared ``extract_information.shadow_comparison`` stream by call site."""
    _install_capturing_summary_llm(monkeypatch)
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=True),
    )
    schedule_mock = MagicMock(return_value=None)
    monkeypatch.setattr(agent_module.extraction_shadow, "schedule_shadow_check", schedule_mock)

    task = _make_task()

    agent_module._schedule_summary_shadow_check_for_hit(
        task=task,
        workflow_run_id=task.workflow_run_id,
        cache_key="ck_summary_logger",
        cached_value={"summary": "cached"},
        cached_age_seconds=extraction_shadow.UNKNOWN_CACHE_AGE_SENTINEL,
        summary_prompt="rendered-summary-prompt",
    )

    kwargs = schedule_mock.call_args.kwargs
    bound_logger = kwargs["logger"]
    # structlog BoundLoggerBase exposes bound context via ``_context``.
    context = getattr(bound_logger, "_context", None) or {}
    assert context.get("prompt_name") == "data-extraction-summary"
    assert context.get("cache_path") == "agent"


def test_summary_helper_gate_disables_llm_call(monkeypatch) -> None:
    """When the PostHog gate returns False the LLM call must not fire."""
    llm_calls: list[int] = []

    async def counting_llm(**_kwargs: object) -> dict:
        llm_calls.append(1)
        return {"summary": "should-not-be-called"}

    monkeypatch.setattr(agent_module.app, "EXTRACTION_LLM_API_HANDLER", counting_llm)
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=False),
    )

    task = _make_task()

    async def _drive() -> None:
        agent_module._schedule_summary_shadow_check_for_hit(
            task=task,
            workflow_run_id=task.workflow_run_id,
            cache_key="ck_summary_gate",
            cached_value={"summary": "cached"},
            cached_age_seconds=extraction_shadow.UNKNOWN_CACHE_AGE_SENTINEL,
            summary_prompt="rendered-summary-prompt",
        )
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(_drive())
    assert llm_calls == []


def test_create_extract_action_cross_run_hit_schedules_shadow(monkeypatch) -> None:
    """Integration test: ``create_extract_action`` schedules a shadow on
    cross-run hits and not on in-run hits (negative control for the
    intentionally-absent in-run-tier coverage).
    """
    schedule_mock = MagicMock(return_value=None)
    monkeypatch.setattr(agent_module.extraction_shadow, "schedule_shadow_check", schedule_mock)

    _install_capturing_summary_llm(monkeypatch)
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "should_shadow_extraction_cache_hit",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(agent_module.extraction_cache, "compute_cache_key", lambda **_: "ck_create_extract")

    # truncate_extraction_schema is a passthrough so the test doesn't need
    # to model schema truncation.
    monkeypatch.setattr(agent_module, "truncate_extraction_schema", lambda x: x)

    # prompt_engine.load_prompt + enforce_prompt_ceiling_tracked: return a
    # deterministic rendered prompt so the helper closure can be exercised.
    monkeypatch.setattr(agent_module.prompt_engine, "load_prompt", lambda *a, **kw: "rendered-summary-prompt")
    monkeypatch.setattr(
        agent_module,
        "enforce_prompt_ceiling_tracked",
        lambda prompt, **kwargs: ("rendered-summary-prompt", kwargs.get("kwargs", {})),
    )

    # Pin context so workflow_run_id is set and the helper guard is satisfied.
    ctx = MagicMock()
    ctx.workflow_run_id = "wfr_create_extract"
    ctx.workflow_permanent_id = "wpid_create_extract"
    ctx.tz_info = None
    monkeypatch.setattr(agent_module.skyvern_context, "ensure_context", lambda: ctx)

    task = _make_task()
    task.workflow_run_id = "wfr_create_extract"
    task.workflow_permanent_id = "wpid_create_extract"

    step = MagicMock(step_id="stp_create_extract")

    # In-run lookup returns a miss so we walk into the cross-run branch.
    monkeypatch.setattr(agent_module.extraction_cache, "lookup", lambda wfr_id, key: None)
    monkeypatch.setattr(
        agent_module.app.AGENT_FUNCTION,
        "lookup_cross_run_extraction_cache",
        AsyncMock(return_value={"summary": "cross_run_cached"}),
    )

    scraped_page = MagicMock()
    asyncio.run(agent_module.ForgeAgent.create_extract_action(task=task, step=step, scraped_page=scraped_page))

    assert schedule_mock.call_count == 1, "cross-run hit must schedule exactly one shadow"
    kwargs = schedule_mock.call_args.kwargs
    assert kwargs["cached_value"] == {"summary": "cross_run_cached"}
    assert kwargs["cached_age_seconds"] == -1.0

    # In-run lookup returns a hit; the cross-run branch is unreachable.
    schedule_mock.reset_mock()
    in_run_hit = agent_module.extraction_cache.LookupResult(
        hit=True,
        value={"summary": "in_run_cached"},
        scope=agent_module.extraction_cache.SCOPE_RUN,
        age_seconds=42.0,
        fallback_reason=None,
    )
    monkeypatch.setattr(agent_module.extraction_cache, "lookup", lambda wfr_id, key: in_run_hit)

    asyncio.run(agent_module.ForgeAgent.create_extract_action(task=task, step=step, scraped_page=scraped_page))

    assert schedule_mock.call_count == 0, (
        "in-run summary cache hit must NOT schedule a shadow — the in-run tier is "
        "workflow_run_id-keyed and structurally cannot exhibit cross-record collisions"
    )
