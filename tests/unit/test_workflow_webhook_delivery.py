from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from structlog.testing import capture_logs

from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.service import WorkflowService


class _StatusResponse:
    def __init__(self, extra_payload: dict | None = None) -> None:
        now = datetime.now(timezone.utc)
        self.status = "completed"
        self.outputs: dict = {}
        self.downloaded_files: list = []
        self.recording_url = None
        self.screenshot_urls: list = []
        self.failure_reason = None
        self.script_run = None
        self.workflow_title = "Workflow"
        self.parameters: dict = {}
        self.errors: list = []
        self.total_steps = 1
        self.extra_payload = extra_payload or {}
        self.created_at = now
        self.modified_at = now
        self.queued_at = now
        self.started_at = now
        self.finished_at = now

    def model_dump_json(self) -> str:
        return json.dumps({"workflow_run_id": "wr_abc", "status": "completed", **self.extra_payload})


class _WebhookRunResponse:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def model_dump_json(self) -> str:
        return '{"run_id":"wr_abc","run_type":"workflow_run"}'


def _workflow_run() -> MagicMock:
    run = MagicMock()
    run.workflow_id = "w_abc"
    run.workflow_permanent_id = "wpid_abc"
    run.workflow_run_id = "wr_abc"
    run.organization_id = "o_abc"
    run.webhook_callback_url = " https://example.com/hook "
    run.proxy_location = "NONE"
    run.totp_verification_url = None
    run.totp_identifier = None
    return run


def _response(status_code: int, body: str = "") -> httpx.Response:
    return httpx.Response(status_code=status_code, content=body.encode("utf-8"))


@pytest.fixture
def webhook_service(monkeypatch: pytest.MonkeyPatch) -> tuple[WorkflowService, AsyncMock, AsyncMock]:
    svc = WorkflowService()
    build_response = AsyncMock(return_value=_StatusResponse())
    update_run = AsyncMock()

    monkeypatch.setattr(svc, "build_workflow_run_status_response", build_response)
    monkeypatch.setattr(service_module, "WorkflowRunResponse", _WebhookRunResponse)
    monkeypatch.setattr(
        service_module,
        "generate_skyvern_webhook_signature",
        lambda payload, api_key: SimpleNamespace(
            headers={"x-skyvern-signature": "sig"},
            signed_payload='{"signed":true}',
        ),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.organizations,
        "get_valid_org_auth_token",
        AsyncMock(return_value=SimpleNamespace(token="api-key")),
    )
    monkeypatch.setattr(service_module.app.DATABASE.workflow_runs, "update_workflow_run", update_run)

    return svc, build_response, update_run


@pytest.mark.asyncio
async def test_prepare_workflow_webhook_builds_request_without_delivery(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    deliver = AsyncMock()
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    webhook = await svc.prepare_workflow_webhook(_workflow_run())

    assert webhook is not None
    assert webhook.workflow_id == "w_abc"
    assert webhook.workflow_run_id == "wr_abc"
    assert webhook.organization_id == "o_abc"
    assert webhook.webhook_callback_url == "https://example.com/hook"
    assert webhook.signed_payload == '{"signed":true}'
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_webhook_logs_named_fields_without_the_payload_object(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, build_response, _update_run = webhook_service
    synthetic_credential = "synthetic-webhook-credential"
    build_response.return_value = _StatusResponse({"output": {"destinations": [{"signing_key": synthetic_credential}]}})
    monkeypatch.setattr(service_module, "generate_skyvern_webhook_signature", generate_skyvern_webhook_signature)
    deliver = AsyncMock(return_value=_response(200, "ok"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run())

    webhook_events = [
        event
        for event in logs
        if event["event"]
        in {
            "Prepared webhook run status for webhook callback url",
            "Sending webhook run status to webhook callback url",
        }
    ]
    assert len(webhook_events) == 2
    assert all("payload" not in event for event in webhook_events)
    assert all(event["workflow_run_id"] == "wr_abc" for event in webhook_events)
    assert all(event["webhook_callback_url"] == "https://example.com/hook" for event in webhook_events)
    assert synthetic_credential not in json.dumps(logs)
    dispatched_payload = deliver.await_args.kwargs["payload"]
    assert synthetic_credential in dispatched_payload
    expected_signature = hmac.new(b"api-key", dispatched_payload.encode("utf-8"), hashlib.sha256).hexdigest()
    assert deliver.await_args.kwargs["headers"]["x-skyvern-signature"] == expected_signature


@pytest.mark.asyncio
async def test_failed_webhook_logs_no_payload_copy(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, build_response, _update_run = webhook_service
    synthetic_credential = "synthetic-webhook-credential"
    build_response.return_value = _StatusResponse({"output": {"destinations": [{"signing_key": synthetic_credential}]}})
    monkeypatch.setattr(service_module, "generate_skyvern_webhook_signature", generate_skyvern_webhook_signature)
    deliver = AsyncMock(return_value=_response(400, "bad request"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run())

    failures = [event for event in logs if event["event"] == "Webhook failed"]
    assert len(failures) == 1
    assert failures[0]["resp_code"] == 400
    assert "webhook_data" not in failures[0]
    # No default=str: serializability doubles as the guard against logging raw response objects.
    assert synthetic_credential not in json.dumps(logs)
    assert synthetic_credential in deliver.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_execute_workflow_webhook_records_customer_failure_without_raising(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    deliver = AsyncMock(return_value=_response(400, "bad request"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run())

    deliver.assert_awaited_once()
    update_run.assert_awaited_once_with(
        workflow_run_id="wr_abc",
        webhook_failure_reason="Webhook failed with status code 400, error message: bad request",
    )


@pytest.mark.asyncio
async def test_execute_workflow_webhook_records_delivery_exception_without_raising(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    deliver = AsyncMock(side_effect=httpx.ConnectError("customer endpoint unreachable"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run())

    deliver.assert_awaited_once()
    update_run.assert_awaited_once()
    assert "customer endpoint unreachable" in update_run.await_args.kwargs["webhook_failure_reason"]


@pytest.mark.asyncio
async def test_execute_workflow_webhook_does_not_raise_if_post_delivery_recording_fails(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    update_run.side_effect = RuntimeError("db pool exhausted after delivery")
    deliver = AsyncMock(return_value=_response(200, "ok"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run())

    deliver.assert_awaited_once()
    update_run.assert_awaited_once_with(workflow_run_id="wr_abc", webhook_failure_reason="")


@pytest.mark.asyncio
async def test_execute_workflow_webhook_propagates_pre_delivery_infra_failure(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, build_response, update_run = webhook_service
    build_response.side_effect = RuntimeError("db pool exhausted before delivery")
    deliver = AsyncMock()
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    with pytest.raises(RuntimeError, match="db pool exhausted before delivery"):
        await svc.execute_workflow_webhook(_workflow_run())

    deliver.assert_not_awaited()
    update_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_workflow_webhook_records_exception_type_for_empty_message(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    deliver = AsyncMock(side_effect=httpx.ReadTimeout(""))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run())

    update_run.assert_awaited_once()
    assert "ReadTimeout" in update_run.await_args.kwargs["webhook_failure_reason"]
