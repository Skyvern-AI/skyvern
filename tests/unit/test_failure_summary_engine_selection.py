import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.api.llm.exceptions import LLM_PROVIDER_ERROR_TYPE
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.utils.challenge_solve_record import ChallengeSolveReceipt
from skyvern.webeye.utils.page import SkyvernFrame

_METHODS = [
    ("summary_failure_reason_for_max_steps", "LLM_API_HANDLER", {}),
    ("summary_failure_reason_for_max_retries", "SECONDARY_LLM_API_HANDLER", {"max_retries": 1}),
]


def _task() -> MagicMock:
    return MagicMock(
        task_id="task-1",
        workflow_run_id="run-1",
        organization_id="org-1",
        navigation_goal="goal",
        navigation_payload=None,
        error_code_mapping=None,
        workflow_system_prompt=None,
    )


def _patch_frame_and_handler(monkeypatch: pytest.MonkeyPatch, handler_name: str) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setattr(app.DATABASE.tasks, "get_task_steps", AsyncMock(return_value=[]))
    monkeypatch.setattr(skyvern_context, "ensure_context", lambda: SimpleNamespace(tz_info=UTC))
    frame = MagicMock(get_content=AsyncMock(return_value="<html></html>"))
    create_instance = AsyncMock(return_value=frame)
    monkeypatch.setattr(SkyvernFrame, "create_instance", create_instance)
    take_screenshots = AsyncMock(return_value=[])
    monkeypatch.setattr(SkyvernFrame, "take_split_screenshots", take_screenshots)
    monkeypatch.setattr(
        app,
        handler_name,
        AsyncMock(return_value={"page_info": "", "reasoning": "summary", "errors": []}),
    )
    return create_instance, take_screenshots


@pytest.mark.parametrize(("method_name", "handler_name", "extra_kwargs"), _METHODS)
@pytest.mark.asyncio
async def test_failure_summary_threads_pinned_selection_without_reresolving(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    handler_name: str,
    extra_kwargs: dict[str, int],
) -> None:
    selection = MagicMock()
    # A live manager that, if consulted at teardown, would hand back a DIFFERENT selection. The
    # summary must thread the caller's already-pinned selection and never re-resolve, so this
    # drift can't leak into the screenshots.
    drifted_manager = MagicMock(get_for_task=MagicMock(return_value=MagicMock(engine_selection=MagicMock())))
    monkeypatch.setattr(app, "BROWSER_MANAGER", drifted_manager)
    create_instance, take_screenshots = _patch_frame_and_handler(monkeypatch, handler_name)

    response = await getattr(ForgeAgent(), method_name)(
        organization=MagicMock(organization_id="org-1"),
        task=_task(),
        step=MagicMock(),
        page=MagicMock(url="https://example.test"),
        engine_selection=selection,
        **extra_kwargs,
    )

    assert response.reasoning == "summary"
    drifted_manager.get_for_task.assert_not_called()
    assert take_screenshots.await_args.kwargs["engine_selection"] is selection
    if create_instance.await_args is not None:
        assert create_instance.await_args.kwargs["engine_selection"] is selection


@pytest.mark.parametrize(("method_name", "handler_name", "extra_kwargs"), _METHODS)
@pytest.mark.asyncio
async def test_failure_summary_preserves_explicit_none_selection(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    handler_name: str,
    extra_kwargs: dict[str, int],
) -> None:
    manager = MagicMock(get_for_task=MagicMock(return_value=MagicMock(engine_selection=MagicMock())))
    monkeypatch.setattr(app, "BROWSER_MANAGER", manager)
    create_instance, take_screenshots = _patch_frame_and_handler(monkeypatch, handler_name)

    await getattr(ForgeAgent(), method_name)(
        organization=MagicMock(organization_id="org-1"),
        task=_task(),
        step=MagicMock(),
        page=MagicMock(url="https://example.test"),
        engine_selection=None,
        **extra_kwargs,
    )

    manager.get_for_task.assert_not_called()
    assert take_screenshots.await_args.kwargs["engine_selection"] is None
    if create_instance.await_args is not None:
        assert create_instance.await_args.kwargs["engine_selection"] is None


MAPPING = {"BOT_PAGE": "The site showed a bot check the agent could not pass"}


@pytest.mark.parametrize(("method_name", "handler_name", "extra_kwargs"), _METHODS)
@pytest.mark.parametrize("mapping", [MAPPING, None], ids=["mapping", "no-mapping"])
@pytest.mark.asyncio
async def test_the_existing_summary_call_reads_the_runs_solve_receipts_and_maps_codes_in_that_same_call(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    handler_name: str,
    extra_kwargs: dict[str, int],
    mapping: dict[str, str] | None,
) -> None:
    monkeypatch.setattr(app, "BROWSER_MANAGER", MagicMock())
    _patch_frame_and_handler(monkeypatch, handler_name)
    calls: list[dict[str, Any]] = []

    async def handler(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        errors = [
            {"error_code": code, "reasoning": "the bot check stayed unsolved", "confidence_float": 0.9}
            for code in ("BOT_PAGE", "RECAPTCHA_ERROR")
        ]
        return {"page_info": "", "reasoning": "summary", "errors": errors}

    for name in ("LLM_API_HANDLER", "SECONDARY_LLM_API_HANDLER"):
        monkeypatch.setattr(app, name, handler)
    task = _task()
    task.error_code_mapping = mapping
    task.started_at, task.created_at = datetime(2026, 8, 1, 0, 0), datetime(2026, 8, 1, 0, 0)
    receipts = [
        ChallengeSolveReceipt("task-1", datetime(2026, 8, 1, 0, 5, tzinfo=UTC), "recaptcha", "unsolved", "unknown"),
        ChallengeSolveReceipt("run-other", datetime(2026, 8, 1, 0, 6, tzinfo=UTC), "hcaptcha", "unsolved", "unknown"),
    ]

    with skyvern_context.scoped(SkyvernContext(task_id="task-1")) as context:
        context.challenge_solve_receipts.extend(receipts)
        response = await getattr(ForgeAgent(), method_name)(
            organization=MagicMock(organization_id="org-1"),
            task=task,
            step=MagicMock(),
            page=MagicMock(url="https://example.test"),
            engine_selection=None,
            **extra_kwargs,
        )

    # One existing call, no extra: the evidence rides in its prompt only when there are codes to match.
    [call] = calls
    prompt = call["prompt"]
    if mapping:
        evidence = json.loads(prompt.split("never supports an error code on its own:\n", 1)[1].split("\n", 1)[0])
        assert evidence == {
            "challenge_solves": [
                {
                    "ref": "solve:1",
                    "service": "recaptcha",
                    "outcome": "unsolved",
                    "page_state": "unknown",
                    "unresolved": True,
                }
            ],
            "truncated": False,
            "network_recording": "not_available_before_the_run_ends",
        }
    else:
        assert "challenge_solves" not in prompt
    # The model cannot hand back a system code as a user row; only a mapped key survives.
    assert [e.error_code for e in response.errors] == (["BOT_PAGE"] if mapping else [])


@pytest.mark.parametrize(("method_name", "handler_name", "extra_kwargs"), _METHODS)
@pytest.mark.asyncio
async def test_a_summary_over_provider_errors_makes_no_call_even_with_codes_and_recorded_solves(
    monkeypatch: pytest.MonkeyPatch, method_name: str, handler_name: str, extra_kwargs: dict[str, int]
) -> None:
    monkeypatch.setattr(app, "BROWSER_MANAGER", MagicMock())
    _patch_frame_and_handler(monkeypatch, handler_name)
    failed = SimpleNamespace(
        success=False, exception_type=LLM_PROVIDER_ERROR_TYPE, exception_message="LLMProvider down"
    )
    action = SimpleNamespace(reasoning="r", action_type="click")
    output = SimpleNamespace(errors=[], actions_and_results=[(action, [failed])], step_exception=None)
    step = SimpleNamespace(output=output, status=StepStatus.failed)
    monkeypatch.setattr(app.DATABASE.tasks, "get_task_steps", AsyncMock(return_value=[step]))
    calls: list[dict[str, Any]] = []

    async def handler(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"page_info": "", "reasoning": "summary", "errors": []}

    for name in ("LLM_API_HANDLER", "SECONDARY_LLM_API_HANDLER"):
        monkeypatch.setattr(app, name, handler)
    task = _task()
    task.error_code_mapping = MAPPING
    task.started_at, task.created_at = datetime(2026, 8, 1), datetime(2026, 8, 1)
    receipt = ChallengeSolveReceipt("task-1", datetime(2026, 8, 1, 0, 5, tzinfo=UTC), "recaptcha", "unsolved", "x")

    with skyvern_context.scoped(SkyvernContext(task_id="task-1")) as context:
        context.challenge_solve_receipts.append(receipt)
        response = await getattr(ForgeAgent(), method_name)(
            organization=MagicMock(organization_id="org-1"),
            task=task,
            step=MagicMock(),
            page=MagicMock(url="https://example.test"),
            engine_selection=None,
            **extra_kwargs,
        )

    assert calls == [] and "LLM" in response.reasoning
