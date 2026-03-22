from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.core.script_generations.skyvern_page import SkyvernPage
from skyvern.forge.sdk.core import skyvern_context
from skyvern.services.script_service import _prepare_cached_block_inputs
from skyvern.webeye.actions.action_types import ActionType
from skyvern.webeye.actions.actions import Action, InputOrSelectContext, SelectOption


def _make_select_action() -> Action:
    return Action(
        action_type=ActionType.SELECT_OPTION,
        intention="Select the state",
        input_or_select_context=InputOrSelectContext(intention="Choose the applicant's state"),
        option=SelectOption(label="California", value="CA", index=1),
        skyvern_element_data={
            "options": [
                {"text": "California", "value": "CA", "optionIndex": 1},
                {"text": "Nevada", "value": "NV", "optionIndex": 2},
            ]
        },
    )


@pytest.fixture(autouse=True)
def reset_skyvern_context() -> None:
    skyvern_context.reset()
    yield
    skyvern_context.reset()


@pytest.mark.asyncio
async def test_prepare_cached_block_inputs_supports_select_option_overrides() -> None:
    ctx = skyvern_context.SkyvernContext(organization_id="org-123", script_revision_id="rev-123")
    skyvern_context.set(ctx)

    run_context = SimpleNamespace(parameters={"existing": "value"})
    database = SimpleNamespace(
        get_script_block_by_label=AsyncMock(
            return_value=SimpleNamespace(input_fields=["full_name", "state"], workflow_run_block_id="block-run-1")
        ),
        get_workflow_run_block=AsyncMock(return_value=SimpleNamespace(task_id="task-123")),
        get_task_actions_hydrated=AsyncMock(
            return_value=[
                Action(action_type=ActionType.INPUT_TEXT, intention="Enter the applicant name"),
                _make_select_action(),
            ]
        ),
    )
    llm_handler = AsyncMock(return_value={"full_name": "Ada Lovelace", "state": "California"})
    app_mock = SimpleNamespace(DATABASE=database, SCRIPT_GENERATION_LLM_API_HANDLER=llm_handler)

    with (
        patch("skyvern.services.script_service.app", app_mock),
        patch(
            "skyvern.services.script_service.script_run_context_manager.get_run_context",
            return_value=run_context,
        ),
    ):
        await _prepare_cached_block_inputs("profile_block", "Fill in the profile")

    assert run_context.parameters["full_name"] == "Ada Lovelace"
    assert run_context.parameters["state"] == "CA"
    assert ctx.action_ai_overrides["profile_block"] == {1: "fallback", 2: "fallback"}
    assert ctx.action_counters["profile_block"] == 0

    merged_prompt = llm_handler.await_args.kwargs["prompt"]
    assert "available_options" in merged_prompt
    assert "California" in merged_prompt
    assert "CA" in merged_prompt


@pytest.mark.asyncio
async def test_prepare_cached_block_inputs_falls_back_when_select_value_is_invalid() -> None:
    ctx = skyvern_context.SkyvernContext(organization_id="org-123", script_revision_id="rev-123")
    skyvern_context.set(ctx)

    run_context = SimpleNamespace(parameters={})
    database = SimpleNamespace(
        get_script_block_by_label=AsyncMock(
            return_value=SimpleNamespace(input_fields=["state"], workflow_run_block_id="block-run-1")
        ),
        get_workflow_run_block=AsyncMock(return_value=SimpleNamespace(task_id="task-123")),
        get_task_actions_hydrated=AsyncMock(return_value=[_make_select_action()]),
    )
    app_mock = SimpleNamespace(
        DATABASE=database,
        SCRIPT_GENERATION_LLM_API_HANDLER=AsyncMock(return_value={"state": "Texas"}),
    )

    with (
        patch("skyvern.services.script_service.app", app_mock),
        patch(
            "skyvern.services.script_service.script_run_context_manager.get_run_context",
            return_value=run_context,
        ),
    ):
        await _prepare_cached_block_inputs("profile_block", "Fill in the profile")

    assert "state" not in run_context.parameters
    assert ctx.action_ai_overrides["profile_block"] == {1: "proactive"}
    assert ctx.action_counters["profile_block"] == 0


@pytest.mark.asyncio
async def test_decorate_call_applies_only_supported_ai_overrides() -> None:
    ctx = skyvern_context.SkyvernContext(
        ai_mode_override="proactive",
        action_ai_overrides={"profile_block": {1: "fallback"}},
        action_counters={"profile_block": 0},
    )
    skyvern_context.set(ctx)

    page = object.__new__(SkyvernPage)
    page.page = SimpleNamespace()
    page.current_label = "profile_block"

    seen_overrides: list[str | None] = []

    async def sample_fn(*args: object, **kwargs: object) -> str:
        seen_overrides.append(skyvern_context.ensure_context().ai_mode_override)
        return "ok"

    result = await page._decorate_call(sample_fn, ActionType.SELECT_OPTION)
    assert result == "ok"
    assert seen_overrides == ["fallback"]
    assert ctx.action_counters["profile_block"] == 1
    assert ctx.ai_mode_override == "proactive"

    ctx.action_counters["profile_block"] = 0
    seen_overrides.clear()

    result = await page._decorate_call(sample_fn, ActionType.CLICK)
    assert result == "ok"
    assert seen_overrides == ["proactive"]
    assert ctx.action_counters["profile_block"] == 0
    assert ctx.ai_mode_override == "proactive"
