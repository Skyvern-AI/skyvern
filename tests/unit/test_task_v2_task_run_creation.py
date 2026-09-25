from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.hashing import generate_url_hash
from skyvern.schemas.runs import RunStatus, RunType
from skyvern.services import task_v2_service
from skyvern.services.task_v2_service import DEFAULT_WORKFLOW_TITLE, initialize_task_v2


@pytest.mark.asyncio
async def test_initialize_task_v2_populates_task_run_url_when_user_url_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("skyvern.services.task_v2_service.validate_fetch_url", lambda url: url)
    organization = SimpleNamespace(organization_id="org_123")
    user_url = "https://example.com"

    app.DATABASE.observer.create_task_v2.return_value = SimpleNamespace(
        observer_cruise_id="tsk_123",
        workflow_run_id=None,
        url=user_url,
    )
    app.WORKFLOW_SERVICE.create_empty_workflow.return_value = SimpleNamespace(
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        title=DEFAULT_WORKFLOW_TITLE,
    )
    app.WORKFLOW_SERVICE.setup_workflow_run.return_value = SimpleNamespace(workflow_run_id="wr_123")
    app.DATABASE.observer.update_task_v2.return_value = SimpleNamespace(
        observer_cruise_id="tsk_123",
        workflow_run_id="wr_123",
        workflow_id="wf_123",
        workflow_permanent_id="wpid_123",
        url=user_url,
    )
    app.DATABASE.tasks.create_task_run.return_value = SimpleNamespace(run_id="tsk_123")

    await initialize_task_v2(
        organization=organization,
        user_prompt="Open the page",
        user_url=user_url,
        create_task_run=True,
        extra_http_headers={"X-Request": "synthetic-run-value"},
        cdp_connect_headers={"X-Request-CDP": "synthetic-cdp-value"},
    )

    app.DATABASE.tasks.create_task_run.assert_awaited_once_with(
        task_run_type=RunType.task_v2,
        organization_id="org_123",
        run_id="tsk_123",
        title=DEFAULT_WORKFLOW_TITLE,
        url=user_url,
        url_hash=generate_url_hash(user_url),
        status=RunStatus.queued,
    )

    workflow_settings = app.WORKFLOW_SERVICE.create_empty_workflow.await_args.kwargs
    assert workflow_settings["extra_http_headers"] == {"X-Request": "synthetic-run-value"}
    assert workflow_settings["cdp_connect_headers"] == {"X-Request-CDP": "synthetic-cdp-value"}
    run_request = app.WORKFLOW_SERVICE.setup_workflow_run.await_args.kwargs["workflow_request"]
    assert run_request.extra_http_headers == {"X-Request": "synthetic-run-value"}
    assert run_request.cdp_connect_headers == {"X-Request-CDP": "synthetic-cdp-value"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent_bucket", "created_at", "expected"),
    [
        ("first_week", datetime(2020, 1, 1, tzinfo=timezone.utc), "first_week"),
        ("unknown", datetime(2020, 1, 1, tzinfo=timezone.utc), "unknown"),
        (None, datetime(2020, 1, 1, tzinfo=timezone.utc), "established"),
        (None, None, "unknown"),
    ],
    ids=["preserve-parent", "preserve-unknown", "derive-age", "none-timestamp"],
)
async def test_run_task_v2_keeps_org_age_bucket_in_execution_context(
    monkeypatch: pytest.MonkeyPatch, parent_bucket: str | None, created_at: datetime | None, expected: str
) -> None:
    organization = SimpleNamespace(
        organization_id="org_test",
        organization_name="Test Org",
        default_llm_key=None,
        default_secondary_llm_key=None,
        created_at=created_at,
    )
    task_v2 = SimpleNamespace(model=None, workflow_id=None, workflow_run_id=None)
    observed: list[str | None] = []

    async def capture_context(**_kwargs: object) -> tuple[None, None, SimpleNamespace]:
        context = skyvern_context.current()
        observed.append(context.org_age_bucket if context else None)
        return None, None, task_v2

    monkeypatch.setattr(app.DATABASE.observer, "get_task_v2", AsyncMock(return_value=task_v2))
    monkeypatch.setattr(task_v2_service, "run_task_v2_helper", capture_context)
    skyvern_context.reset()
    if parent_bucket is not None:
        skyvern_context.set(skyvern_context.SkyvernContext(org_age_bucket=parent_bucket))
    try:
        result = await task_v2_service.run_task_v2(organization=organization, task_v2_id="tsk_test")
    finally:
        skyvern_context.reset()

    assert result is task_v2
    assert observed == [expected]
