import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.exceptions import FailedToSendWebhook
from skyvern.forge.sdk.workflow.models.workflow import WorkflowRun, WorkflowRunStatus
from workers.run_parameters import RunSkyvernWorkflowParams, SendWorkflowWebhookParams

importlib.import_module("cloud")
activities_module = importlib.import_module("workers.temporal_v2_worker.activities")
send_workflow_webhook_activity = activities_module.send_workflow_webhook_activity


def _make_wr(
    status: str = "timed_out",
    webhook_callback_url: str | None = "https://example.com/hook",
) -> WorkflowRun:
    now = datetime.now(timezone.utc)
    return WorkflowRun(
        workflow_run_id="wr_abc",
        workflow_id="wf_abc",
        workflow_permanent_id="wpid_abc",
        organization_id="o_xyz",
        status=WorkflowRunStatus(status),
        webhook_callback_url=webhook_callback_url,
        created_at=now,
        modified_at=now,
    )


def test_send_workflow_webhook_params_accepts_required_fields() -> None:
    params = SendWorkflowWebhookParams(
        workflow_run_id="wr_520069195913377474",
        organization_id="o_482380747875719746",
    )
    assert params.workflow_run_id == "wr_520069195913377474"
    assert params.organization_id == "o_482380747875719746"


@pytest.mark.asyncio
async def test_send_workflow_webhook_activity_delegates_to_service() -> None:
    params = SendWorkflowWebhookParams(
        workflow_run_id="wr_abc",
        organization_id="o_xyz",
    )
    fake_run = _make_wr(status="timed_out")

    with (
        patch("skyvern.forge.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(return_value=fake_run)),
        patch("skyvern.forge.app.WORKFLOW_SERVICE.execute_workflow_webhook", AsyncMock()) as send_mock,
    ):
        await send_workflow_webhook_activity(params)

    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["workflow_run"] is fake_run


@pytest.mark.asyncio
async def test_send_workflow_webhook_activity_returns_silently_when_wr_missing() -> None:
    params = SendWorkflowWebhookParams(workflow_run_id="wr_gone", organization_id="o_xyz")
    with (
        patch("skyvern.forge.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(return_value=None)),
        patch("skyvern.forge.app.WORKFLOW_SERVICE.execute_workflow_webhook", AsyncMock()) as send_mock,
    ):
        await send_workflow_webhook_activity(params)
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_workflow_webhook_activity_propagates_delivery_errors() -> None:
    params = SendWorkflowWebhookParams(workflow_run_id="wr_abc", organization_id="o_xyz")
    fake_run = _make_wr(status="timed_out")
    fake_token = type("T", (), {"token": "k"})()
    with (
        patch("skyvern.forge.app.DATABASE.workflow_runs.get_workflow_run", AsyncMock(return_value=fake_run)),
        patch("skyvern.forge.app.DATABASE.organizations.get_valid_org_auth_token", AsyncMock(return_value=fake_token)),
        patch(
            "skyvern.forge.app.WORKFLOW_SERVICE.execute_workflow_webhook",
            AsyncMock(side_effect=FailedToSendWebhook(workflow_id="w_1", workflow_run_id="wr_abc")),
        ),
    ):
        with pytest.raises(FailedToSendWebhook):
            await send_workflow_webhook_activity(params)


@pytest.mark.asyncio
async def test_run_workflow_activity_disables_inline_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    execute_mock = AsyncMock()

    async def fake_heartbeat_loop(_run_id: str, stop) -> None:
        await stop.wait()

    monkeypatch.setattr("scripts.run_workflow.execute_workflow", execute_mock)
    monkeypatch.setattr(activities_module, "heartbeat_loop", fake_heartbeat_loop)
    monkeypatch.setattr(activities_module, "activity_teardown", AsyncMock())
    monkeypatch.setattr(activities_module.activity, "heartbeat", lambda *_args, **_kwargs: None)

    await activities_module.run_workflow_activity(
        RunSkyvernWorkflowParams(
            organization_id="o_xyz",
            workflow_run_id="wr_abc",
        )
    )

    execute_mock.assert_awaited_once()
    assert execute_mock.await_args.kwargs["need_call_webhook"] is False
