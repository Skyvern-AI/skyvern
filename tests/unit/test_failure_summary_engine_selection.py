from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge import app
from skyvern.forge.agent import ForgeAgent
from skyvern.forge.sdk.core import skyvern_context
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
