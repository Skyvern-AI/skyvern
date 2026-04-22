import importlib
from datetime import timedelta

import pytest
from temporalio.exceptions import ActivityError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.exceptions import TimeoutType

from workers.run_parameters import RunSkyvernWorkflowParams, SendWorkflowWebhookParams

importlib.import_module("cloud")
workflow_module = importlib.import_module("workers.temporal_v2_worker.workflows")
RunWorkflowWorkflowV2 = workflow_module.RunWorkflowWorkflowV2


def _activity_name(activity_fn: object) -> str:
    return getattr(activity_fn, "__name__", repr(activity_fn))


def _heartbeat_timeout_error() -> ActivityError:
    err = ActivityError(
        "activity timed out",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test-worker",
        activity_type="run_workflow_activity",
        activity_id="act_1",
        retry_state=None,
    )
    err.__cause__ = TemporalTimeoutError(
        "simulated heartbeat timeout",
        type=TimeoutType.HEARTBEAT,
        last_heartbeat_details=[],
    )
    return err


@pytest.mark.asyncio
async def test_run_workflow_v2_sends_webhook_on_heartbeat_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    async def fake_execute_activity(activity_fn: object, arg: object, **_kwargs: object) -> None:
        name = _activity_name(activity_fn)
        calls.append((name, arg))
        if name == "run_workflow_activity":
            raise _heartbeat_timeout_error()

    monkeypatch.setattr(workflow_module.workflow, "execute_activity", fake_execute_activity)
    monkeypatch.setattr(workflow_module.workflow, "upsert_search_attributes", lambda *_args, **_kwargs: None)

    with pytest.raises(ActivityError):
        await RunWorkflowWorkflowV2().run(RunSkyvernWorkflowParams(organization_id="o_1", workflow_run_id="wr_1"))

    assert [name for name, _arg in calls] == [
        "run_workflow_activity",
        "timeout_workflow_run_activity",
        "signal_dependent_workflows_activity",
        "send_workflow_webhook_activity",
    ]
    send_params = calls[-1][1]
    assert isinstance(send_params, SendWorkflowWebhookParams)
    assert send_params.workflow_run_id == "wr_1"
    assert send_params.organization_id == "o_1"


@pytest.mark.asyncio
async def test_run_workflow_v2_sends_webhook_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object, object]] = []

    async def fake_execute_activity(activity_fn: object, arg: object, **kwargs: object) -> None:
        calls.append((_activity_name(activity_fn), arg, kwargs))

    monkeypatch.setattr(workflow_module.workflow, "execute_activity", fake_execute_activity)
    monkeypatch.setattr(workflow_module.workflow, "upsert_search_attributes", lambda *_args, **_kwargs: None)

    await RunWorkflowWorkflowV2().run(RunSkyvernWorkflowParams(organization_id="o_1", workflow_run_id="wr_ok"))

    assert [name for name, _arg, _kwargs in calls] == [
        "run_workflow_activity",
        "signal_dependent_workflows_activity",
        "send_workflow_webhook_activity",
    ]
    send_params = calls[-1][1]
    send_kwargs = calls[-1][2]
    assert isinstance(send_params, SendWorkflowWebhookParams)
    assert send_params.workflow_run_id == "wr_ok"
    assert send_kwargs["start_to_close_timeout"] == timedelta(seconds=60)
    assert send_kwargs["retry_policy"].maximum_attempts == 1
