from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException

from skyvern.forge import app
from skyvern.forge.sdk.routes import feedback as feedback_routes
from skyvern.forge.sdk.schemas.feedback import RunFeedback, RunFeedbackRequest
from skyvern.forge.sdk.schemas.organizations import Organization

ORG = Organization(
    organization_id="o_1",
    organization_name="Org",
    created_at=datetime(2026, 1, 1, tzinfo=UTC),
    modified_at=datetime(2026, 1, 1, tzinfo=UTC),
)


def _stored(rating: str = "up") -> RunFeedback:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return RunFeedback(
        run_feedback_id="fb_1",
        organization_id="o_1",
        target_type="workflow_run",
        target_id="wr_1",
        context_id="wpid_1",
        rating=rating,
        created_at=now,
        modified_at=now,
    )


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_run: object | None,
    hook: AsyncMock,
    previous: RunFeedback | None = None,
) -> SimpleNamespace:
    run_feedback = SimpleNamespace(
        upsert_run_feedback=AsyncMock(return_value=_stored()),
        delete_run_feedback=AsyncMock(return_value=None),
        get_run_feedback=AsyncMock(return_value=previous),
    )
    database = SimpleNamespace(
        workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=workflow_run)),
        tasks=SimpleNamespace(get_task=AsyncMock(return_value=None)),
        run_feedback=run_feedback,
    )
    monkeypatch.setattr(app, "DATABASE", database)
    monkeypatch.setattr(app, "AGENT_FUNCTION", SimpleNamespace(on_feedback_submitted=hook))
    return run_feedback


@pytest.mark.asyncio
async def test_feedback_on_a_run_outside_the_org_is_404_and_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = AsyncMock()
    run_feedback = _install_fakes(monkeypatch, workflow_run=None, hook=hook)

    with pytest.raises(HTTPException) as exc:
        await feedback_routes.submit_run_feedback(
            RunFeedbackRequest(target_type="workflow_run", target_id="wr_other", rating="down"), BackgroundTasks(), ORG
        )

    assert exc.value.status_code == 404
    run_feedback.upsert_run_feedback.assert_not_awaited()
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_rating_is_stored_with_run_context_and_fan_out_runs_after_the_response_and_swallows_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = AsyncMock(side_effect=RuntimeError("slack down"))
    run_feedback = _install_fakes(
        monkeypatch,
        workflow_run=SimpleNamespace(workflow_permanent_id="wpid_1"),
        hook=hook,
        previous=_stored("down"),
    )
    background_tasks = BackgroundTasks()

    result = await feedback_routes.submit_run_feedback(
        RunFeedbackRequest(
            target_type="workflow_run",
            target_id="wr_1",
            rating="down",
            reason="stuck on login",
            needs_support=True,
            submitted_by="user@example.com",
        ),
        background_tasks,
        ORG,
    )

    assert result == _stored()
    hook.assert_not_awaited()
    await background_tasks()
    stored_kwargs = run_feedback.upsert_run_feedback.await_args.kwargs
    assert stored_kwargs["organization_id"] == "o_1"
    assert stored_kwargs["context_id"] == "wpid_1"
    assert stored_kwargs["needs_support"] is True
    event = hook.await_args.kwargs["event"]
    assert (event.target_type, event.target_id, event.rating, event.needs_support) == (
        "workflow_run",
        "wr_1",
        "down",
        True,
    )
    assert event.context_id == "wpid_1"
    assert event.previous_rating == "down"


@pytest.mark.asyncio
async def test_resaving_the_same_state_stores_but_does_not_fan_out(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = AsyncMock()
    previous = _stored("down").model_copy(update={"reason": "stuck on login", "needs_support": True})
    run_feedback = _install_fakes(
        monkeypatch, workflow_run=SimpleNamespace(workflow_permanent_id="wpid_1"), hook=hook, previous=previous
    )
    background_tasks = BackgroundTasks()

    await feedback_routes.submit_run_feedback(
        RunFeedbackRequest(
            target_type="workflow_run", target_id="wr_1", rating="down", reason="stuck on login", needs_support=True
        ),
        background_tasks,
        ORG,
    )
    await background_tasks()

    run_feedback.upsert_run_feedback.assert_awaited_once()
    hook.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_rating_clears_the_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    hook = AsyncMock()
    run_feedback = _install_fakes(
        monkeypatch, workflow_run=SimpleNamespace(workflow_permanent_id="wpid_1"), hook=hook, previous=_stored("up")
    )
    background_tasks = BackgroundTasks()

    result = await feedback_routes.submit_run_feedback(
        RunFeedbackRequest(target_type="workflow_run", target_id="wr_1", rating=None), background_tasks, ORG
    )
    await background_tasks()

    assert result is None
    run_feedback.delete_run_feedback.assert_awaited_once()
    run_feedback.upsert_run_feedback.assert_not_awaited()
    event = hook.await_args.kwargs["event"]
    assert (event.rating, event.previous_rating) == (None, "up")
