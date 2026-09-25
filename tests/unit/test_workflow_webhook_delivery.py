from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from skyvern.exceptions import InvalidUrl
from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowRunAttemptModel, WorkflowRunModel
from skyvern.forge.sdk.db.repositories import workflow_runs as repository_module
from skyvern.forge.sdk.db.repositories.workflow_run_attempts import WorkflowRunAttemptsRepository
from skyvern.forge.sdk.db.repositories.workflow_runs import WorkflowRunsRepository
from skyvern.forge.sdk.db.repositories.workflows import WorkflowsRepository
from skyvern.forge.sdk.executor.background_task_executor import BackgroundTaskExecutor
from skyvern.forge.sdk.workflow import service as service_module
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowDefinition, WorkflowRun, WorkflowRunStatus
from skyvern.forge.sdk.workflow.retry_policy import (
    LEASE_SAFETY_MARGIN_SECONDS,
    LEASE_TAKEOVER_SECONDS,
    RETRY_DECISION_FINAL,
    RETRY_DECISION_REVOKED,
    WEBHOOK_FALLBACK_GRACE_SECONDS,
    WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS,
    RetryDecision,
    _attempt_from_record,
    _get_retry_policy,
    evaluate_retry_policy,
    on_terminal_transition,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.run_enums import WebhookDeliveryStatus, resolve_webhook_delivery_projection
from skyvern.schemas.workflows import WorkflowRetryPolicy
from skyvern.services import webhook_delivery as webhook_delivery_module
from skyvern.services import webhook_service as replay_service
from tests.unit.scoped_asyncio import ScopedAsyncio


class _StatusResponse:
    def __init__(self, extra_payload: dict | None = None) -> None:
        now = datetime.now(UTC)
        self.status = WorkflowRunStatus.completed
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
        self.attempt = 1
        self.retry_pending = False
        self.next_attempt_at = None
        self.attempts: list = []

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
    run.status = WorkflowRunStatus.completed
    run.finished_at = datetime(2026, 1, 1, tzinfo=UTC)
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
    monkeypatch.setattr(service_module.app.DATABASE.workflow_runs, "update_workflow_webhook_delivery", update_run)

    async def save_progress(
        workflow_run_id: str,
        attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime,
        progress: dict[str, Any],
    ) -> bool:
        rows = await service_module.app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
        row = next((row for row in rows if row.attempt_number == attempt_number), None)
        if row is None or row.side_effects_released_at != expected_claim_at:
            return False
        checkpoint = getattr(row, f"{kind}_side_effects_progress", None) or {}
        setattr(row, f"{kind}_side_effects_progress", {**checkpoint, **progress})
        return True

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "save_side_effect_progress", save_progress, raising=False
    )

    async def reserve_delivery(
        workflow_run_id: str,
        attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime | None,
        max_attempts: int,
        final_exhausted_projection: WebhookDeliveryStatus | None = None,
        expected_status: str | None = None,
        expected_finished_at: datetime | None = None,
    ) -> int | None:
        rows = await service_module.app.DATABASE.workflow_run_attempts.get_attempts(workflow_run_id)
        row = next((row for row in rows if row.attempt_number == attempt_number), None)
        if row is None or getattr(row, "side_effects_released_at", None) != expected_claim_at:
            return None
        checkpoint = getattr(row, f"{kind}_side_effects_progress", None) or {}
        attempts = checkpoint.get("webhook_delivery_attempts", 0) + 1
        assert attempts <= max_attempts
        setattr(row, f"{kind}_side_effects_progress", {**checkpoint, "webhook_delivery_attempts": attempts})
        return attempts

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "reserve_attempt_webhook_delivery",
        reserve_delivery,
        raising=False,
    )

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
async def test_non2xx_webhook_log_exposes_canonical_status_code_and_error_reason(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    body = "synthetic-endpoint-secret"
    deliver = AsyncMock(return_value=_response(503, body))
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    monkeypatch.setattr(webhook_delivery_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run())

    failures = [event for event in logs if event["event"] == "Webhook failed"]
    retries = [event for event in logs if event["event"] == "Retrying webhook delivery after transient failure"]
    assert len(failures) == 1
    assert failures[0]["resp_code"] == 503
    assert failures[0]["resp_text"] == body
    assert failures[0]["status_code"] == 503
    assert update_run.await_args.kwargs["webhook_failure_reason"] == (
        f"Webhook failed with status code 503, error message: {body}"
    )
    assert update_run.await_args.kwargs["webhook_failure_reason"] == webhook_delivery_module.format_http_failure_reason(
        503, body
    )
    assert failures[0]["error_reason"] == "Webhook failed with status code 503"
    assert failures[0]["error_reason"] == webhook_delivery_module.format_http_log_reason(503)
    assert body not in failures[0]["error_reason"]
    assert len(retries) == webhook_delivery_module.WEBHOOK_DELIVERY_MAX_ATTEMPTS - 1
    for attempt, retry in enumerate(retries, start=1):
        assert retry["error_reason"] == failures[0]["error_reason"]
        assert retry["status_code"] == 503
        assert retry["attempt"] == attempt
        assert retry["max_attempts"] == webhook_delivery_module.WEBHOOK_DELIVERY_MAX_ATTEMPTS
        assert retry["error"] is None
        assert body not in str(retry)


@pytest.mark.asyncio
async def test_exception_webhook_log_exposes_canonical_status_code_and_error_reason(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    deliver = AsyncMock(side_effect=httpx.ConnectError("customer endpoint unreachable"))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run())

    failures = [
        event for event in logs if event["event"] == "Workflow webhook delivery failed after attempting delivery"
    ]
    assert len(failures) == 1
    # No response was received, so the canonical status_code is null.
    assert failures[0]["status_code"] is None
    assert "ConnectError" in failures[0]["error_reason"]
    # Legacy error field retained.
    assert "error" in failures[0]


@pytest.mark.asyncio
async def test_exception_webhook_log_derives_status_code_from_http_status_error(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    error = httpx.HTTPStatusError(
        "proxy hop rejected the webhook",
        request=httpx.Request("POST", "https://proxy.example/webhook"),
        response=_response(502, "bad gateway"),
    )
    deliver = AsyncMock(side_effect=error)
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run())

    failures = [
        event for event in logs if event["event"] == "Workflow webhook delivery failed after attempting delivery"
    ]
    assert len(failures) == 1
    # Canonical status_code derives from the exception response (NAT-proxy hop failure).
    assert failures[0]["status_code"] == 502
    # Terminal HTTPStatusError log reason is the status-only HTTP reason, matching the retry log.
    assert failures[0]["error_reason"] == webhook_delivery_module.format_http_log_reason(502)
    assert "bad gateway" not in failures[0]["error_reason"]
    # Persisted reason keeps the legacy no-response wording (persistence unchanged).
    assert update_run.await_args.kwargs["webhook_failure_reason"].startswith(
        "Webhook delivery failed before receiving a response:"
    )


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
    update_run.assert_awaited_once()
    assert update_run.await_args.kwargs["workflow_run_id"] == "wr_abc"
    assert (
        update_run.await_args.kwargs["webhook_failure_reason"]
        == "Webhook failed with status code 400, error message: bad request"
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
    update_run.assert_awaited_once()
    assert update_run.await_args.kwargs["workflow_run_id"] == "wr_abc"
    assert update_run.await_args.kwargs["webhook_failure_reason"] == ""
    assert update_run.await_args.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.delivered


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


@pytest.mark.asyncio
async def test_policy_webhook_retries_delivery_when_process_stops_before_recording_claim(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    attempt = SimpleNamespace(attempt_number=1, webhook_sent_at=None, interim_webhook_sent_at=None)
    get_attempts = AsyncMock(return_value=[attempt])
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim)

    deliver = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)
    with pytest.raises(asyncio.CancelledError):
        await svc.execute_workflow_webhook(_workflow_run())
    claim.assert_not_awaited()

    deliver.side_effect = None
    await svc.execute_workflow_webhook(_workflow_run())

    assert deliver.await_count == 2
    claim.assert_awaited_once_with("wr_abc", 1, kind="final", delivery_attempted=True)


@pytest.mark.asyncio
async def test_policy_webhook_skips_when_claim_was_already_recorded(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=datetime.now(UTC),
        interim_webhook_sent_at=None,
    )
    get_attempts = AsyncMock(return_value=[attempt])
    claim = AsyncMock(return_value=True)
    deliver = AsyncMock()
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim)
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)

    await svc.execute_workflow_webhook(_workflow_run())

    deliver.assert_not_awaited()
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_policy_release_records_final_marker_when_no_webhook_url(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    workflow_run.webhook_callback_url = None
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    claim_side_effects = AsyncMock(return_value=True)
    claim_webhook = AsyncMock(return_value=True)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        claim_webhook,
    )
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_schedule_workflow_run_terminal_hooks", MagicMock())
    monkeypatch.setattr(svc, "_schedule_credential_fallback_retry", MagicMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())

    await svc.run_terminal_side_effects(workflow_run, RetryDecision(False, 1, 0, True, "budget_exhausted"))

    claim_webhook.assert_awaited_once_with("wr_abc", 1, kind="final")


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_decision", [RETRY_DECISION_REVOKED, RETRY_DECISION_FINAL])
async def test_final_release_skips_credential_fallback_for_a_canceled_retry(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    retry_decision: str,
) -> None:
    """A cancel during the retry delay keeps the last attempt's failed status, so only the
    revoked decision can stop the credential fallback from starting a new run."""
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    workflow_run.webhook_callback_url = None
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision=retry_decision,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_side_effects", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", AsyncMock(return_value=True)
    )
    credential_fallback = AsyncMock()
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", credential_fallback)
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    reason = "cancel" if retry_decision == RETRY_DECISION_REVOKED else "no_match"

    outcome = await svc.run_terminal_side_effects(workflow_run, RetryDecision(False, 1, 0, True, reason))

    assert outcome == "released"
    assert credential_fallback.await_count == (0 if retry_decision == RETRY_DECISION_REVOKED else 1)


@pytest.mark.asyncio
async def test_policy_final_webhook_retries_after_retry_after_and_records_marker(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    claim_at = datetime.now(UTC).replace(tzinfo=None)
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )

    async def record_webhook_marker(
        _workflow_run_id: str,
        _attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime | None = None,
        delivery_attempted: bool = False,
    ) -> bool:
        assert kind == "final"
        assert expected_claim_at == claim_at
        attempt.webhook_sent_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        record_webhook_marker,
    )
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())

    responses = [
        httpx.Response(503, headers={"Retry-After": "0"}),
        httpx.Response(200, content=b"ok"),
    ]
    deliver = AsyncMock(side_effect=responses)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    original_fenced_effect = svc._run_fenced_terminal_side_effect
    effect_timeouts: dict[str, float | None] = {}

    async def record_fenced_effect(**kwargs: Any) -> bool:
        effect_timeouts[kwargs["effect_name"]] = kwargs.get("timeout_seconds")
        return await original_fenced_effect(**kwargs)

    monkeypatch.setattr(svc, "_run_fenced_terminal_side_effect", record_fenced_effect)

    await svc.run_terminal_side_effects(workflow_run, RetryDecision(False, 1, 0, True, "budget_exhausted"))

    assert deliver.await_count == 2
    assert attempt.webhook_sent_at is not None
    assert effect_timeouts["final workflow webhook"] == WEBHOOK_FALLBACK_GRACE_SECONDS


@pytest.mark.asyncio
async def test_final_tail_restart_does_not_repeat_once_only_hook_after_early_refusal(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    owner_token = datetime(2026, 9, 7, 16, 0, 1, tzinfo=UTC)
    takeover_token = datetime(2026, 9, 7, 16, 0, 2, tzinfo=UTC)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="final",
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    claim_count = 0

    async def claim_side_effects(*_args: Any, expected_claim_at: datetime | None = None, **_kwargs: Any) -> datetime:
        nonlocal claim_count
        claim_count += 1
        if claim_count == 1:
            assert expected_claim_at is None
            token = owner_token
        else:
            assert expected_claim_at == owner_token
            token = takeover_token
        attempt.side_effects_released_at = token
        return token

    async def record_final_webhook(*_args: Any, **_kwargs: Any) -> None:
        attempt.webhook_sent_at = datetime.now(UTC)

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    delete_attachments = AsyncMock()
    credential_fallback = AsyncMock()
    completion_tags = AsyncMock()
    terminal_hooks = AsyncMock()
    final_hook = AsyncMock()
    final_webhook = AsyncMock(side_effect=record_final_webhook)
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", credential_fallback)
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", completion_tags)
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", terminal_hooks)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", final_hook)
    monkeypatch.setattr(svc, "execute_workflow_webhook", final_webhook)
    lease_checks = AsyncMock(side_effect=[True, True, True, True, False, True, True, True, True, True])
    monkeypatch.setattr(svc, "_side_effects_lease_is_current", lease_checks)
    monkeypatch.setattr(svc, "_terminal_side_effect_outcome_after_fence", AsyncMock(return_value="fenced_out"))

    decision = RetryDecision(False, 1, 0, True, "budget_exhausted")
    first_outcome = await svc.run_terminal_side_effects(workflow_run, decision)
    second_outcome = await svc.run_terminal_side_effects(
        workflow_run,
        decision,
        side_effects_claim_at=owner_token,
        side_effects_stale_before=owner_token + timedelta(seconds=1),
    )

    assert first_outcome == "fenced_out"
    assert second_outcome == "released"
    assert final_hook.await_count == 1
    final_webhook.assert_awaited_once()
    assert attempt.webhook_sent_at is not None
    assert lease_checks.await_count == 6
    assert delete_attachments.await_count == 1
    assert credential_fallback.await_count == 1
    assert completion_tags.await_count == 1
    assert terminal_hooks.await_count == 1


@pytest.mark.asyncio
async def test_final_tail_guard_prevents_refusal_between_hook_and_webhook(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    claim_at = datetime(2026, 9, 7, 16, 0, 1, tzinfo=UTC)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="final",
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    final_hook = AsyncMock()
    final_webhook = AsyncMock()
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", final_hook)
    monkeypatch.setattr(svc, "execute_workflow_webhook", final_webhook)
    lease_checks = AsyncMock(side_effect=[True, True, True, True, True])
    monkeypatch.setattr(svc, "_side_effects_lease_is_current", lease_checks)

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(False, 1, 0, True, "budget_exhausted"),
    )

    assert outcome == "released"
    final_hook.assert_awaited_once()
    final_webhook.assert_awaited_once()
    assert lease_checks.await_count == 5


@pytest.mark.asyncio
async def test_final_tail_fences_owner_before_once_only_hook_after_takeover(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    claim_at = datetime.now(UTC)
    takeover_token = claim_at + timedelta(seconds=1)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="final",
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    get_attempts = AsyncMock(return_value=[attempt])
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    claim_webhook = AsyncMock()
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim_webhook)
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    final_hook = AsyncMock()
    final_webhook = AsyncMock()
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", final_hook)
    monkeypatch.setattr(svc, "execute_workflow_webhook", final_webhook)

    # The owner passes the total-tail bound check. The fresh row read immediately before the
    # once-only hook observes the takeover token and fences the paused owner out.
    async def take_over_at_tail(**_kwargs: Any) -> bool:
        if lease_checks.await_count == 5:
            attempt.side_effects_released_at = takeover_token
        return True

    lease_checks = AsyncMock(side_effect=take_over_at_tail)
    monkeypatch.setattr(svc, "_side_effects_lease_is_current", lease_checks)

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(False, 1, 0, True, "budget_exhausted"),
    )

    assert outcome == "lease_held_by_other_young"
    final_hook.assert_not_awaited()
    final_webhook.assert_not_awaited()
    claim_webhook.assert_not_awaited()
    assert lease_checks.await_count == 5
    assert get_attempts.await_count == 8


@pytest.mark.asyncio
async def test_policy_release_reports_a_foreign_young_lease_as_retryable(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    now = datetime(2026, 9, 7, 16, 0, tzinfo=UTC).replace(tzinfo=None)
    foreign_claim = now - timedelta(seconds=400)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="final",
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=foreign_claim,
    )
    monkeypatch.setattr(service_module, "naive_utc_now", lambda: now)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=None),
    )

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(False, 1, 0, True, "budget_exhausted"),
        side_effects_claim_at=foreign_claim,
        side_effects_stale_before=now - timedelta(seconds=LEASE_TAKEOVER_SECONDS),
    )

    assert outcome == "lease_held_by_other_young"


@pytest.mark.asyncio
async def test_policy_retry_without_webhook_records_interim_marker(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    claim_side_effects = AsyncMock(return_value=True)
    claim_webhook = AsyncMock(return_value=True)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        claim_webhook,
    )

    await svc.run_terminal_side_effects(workflow_run, RetryDecision(True, 1, 0, False, "matched"))

    claim_side_effects.assert_awaited_once_with(
        "wr_abc",
        1,
        kind="interim",
        expected_retry_decision="retry",
    )
    claim_webhook.assert_awaited_once_with("wr_abc", 1, kind="interim")


@pytest.mark.asyncio
async def test_interim_payload_preparation_failure_is_not_recorded_as_release(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    claim_at = datetime.now(UTC).replace(tzinfo=None)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="retry",
        next_attempt_prepared_at=None,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    clear_lease = AsyncMock()
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "clear_failed_interim_side_effect_lease",
        clear_lease,
        raising=False,
    )
    monkeypatch.setattr(svc, "prepare_workflow_webhook", AsyncMock(side_effect=RuntimeError("payload unavailable")))
    claim_webhook = AsyncMock()
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim_webhook)

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(True, 1, 0, True, "matched"),
    )

    assert outcome == "effect_failed"
    assert attempt.interim_webhook_sent_at is None
    assert attempt.next_attempt_prepared_at is None
    claim_webhook.assert_not_awaited()
    clear_lease.assert_awaited_once_with("wr_abc", 1, claim_at)


@pytest.mark.asyncio
async def test_failed_interim_release_is_retried_before_preparing_next_attempt(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    now = datetime.now(UTC).replace(tzinfo=None)
    claim_tokens = [now, now + timedelta(seconds=1)]
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="retry",
        next_attempt_prepared_at=None,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )

    async def claim_side_effects(*_args: Any, **_kwargs: Any) -> datetime:
        token = claim_tokens.pop(0)
        attempt.side_effects_released_at = token
        return token

    async def clear_lease(*_args: Any, **_kwargs: Any) -> bool:
        attempt.side_effects_released_at = None
        return True

    async def claim_webhook(
        _workflow_run_id: str,
        _attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime | None = None,
        delivery_attempted: bool = False,
    ) -> bool:
        assert kind == "interim"
        assert expected_claim_at == attempt.side_effects_released_at
        attempt.interim_webhook_sent_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "clear_failed_interim_side_effect_lease",
        clear_lease,
        raising=False,
    )
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim_webhook)
    monkeypatch.setattr(
        svc,
        "prepare_workflow_webhook",
        AsyncMock(side_effect=[RuntimeError("payload unavailable"), object()]),
    )
    deliver = AsyncMock()
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)

    monkeypatch.setattr(svc, "execute_workflow", AsyncMock(return_value=workflow_run))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs,
        "get_workflow_run",
        AsyncMock(return_value=workflow_run),
    )
    decisions = iter(
        [
            RetryDecision(True, 1, 0, True, "matched"),
            RetryDecision(True, 1, 0, True, "matched"),
            RetryDecision(False, 2, 0, True, "budget_exhausted"),
        ]
    )
    monkeypatch.setattr(service_module, "get_recorded_decision", AsyncMock(side_effect=decisions))
    workflow_run.depends_on_workflow_run_id = None
    prepare = AsyncMock(
        return_value=SimpleNamespace(status="inserted", pinned_browser_session_id=None, serialized_identity=False)
    )
    monkeypatch.setattr(service_module, "prepare_next_attempt_result", prepare)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_prepared_attempt_execution",
        AsyncMock(return_value=True),
        raising=False,
    )
    original_terminal_effects = svc.run_terminal_side_effects
    final_effects = AsyncMock()

    async def run_terminal_effects(*args: Any, **kwargs: Any) -> Any:
        decision_arg = args[1]
        if decision_arg.retry:
            return await original_terminal_effects(*args, **kwargs)
        await final_effects(*args, **kwargs)
        return "released"

    monkeypatch.setattr(svc, "run_terminal_side_effects", run_terminal_effects)

    result = await svc.execute_workflow_with_retries(
        workflow_run_id="wr_abc",
        api_key="api-key",
        organization=SimpleNamespace(organization_id="o_abc"),
    )

    assert result is workflow_run
    assert attempt.interim_webhook_sent_at is not None
    assert deliver.await_count == 1
    prepare.assert_awaited_once_with(
        workflow_run_id="wr_abc",
        organization_id="o_abc",
        from_attempt=1,
        clear_browser_address=False,
    )
    final_effects.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_policy_terminal_releasers_deliver_final_webhook_once(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    get_attempts = AsyncMock(return_value=[attempt])
    claim_call_count = 0
    both_claimers_entered = asyncio.Event()
    release_claimers = asyncio.Event()

    async def claim_side_effects(_workflow_run_id: str, _attempt_number: int) -> bool:
        nonlocal claim_call_count
        claim_call_count += 1
        claim_number = claim_call_count
        if claim_call_count == 2:
            both_claimers_entered.set()
        await release_claimers.wait()
        return claim_number == 1

    async def claim_webhook(
        _workflow_run_id: str, _attempt_number: int, *, kind: str, delivery_attempted: bool = False
    ) -> bool:
        assert kind == "final"
        attempt.webhook_sent_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", get_attempts)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        claim_webhook,
    )
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_schedule_workflow_run_terminal_hooks", MagicMock())
    monkeypatch.setattr(svc, "_schedule_credential_fallback_retry", MagicMock())
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    monkeypatch.setattr(svc, "prepare_workflow_webhook", AsyncMock(return_value=object()))
    deliver = AsyncMock()
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)

    decision = RetryDecision(False, 1, 0, True, "budget_exhausted")
    releasers = asyncio.gather(
        svc.run_terminal_side_effects(workflow_run, decision),
        svc.run_terminal_side_effects(workflow_run, decision),
    )
    await asyncio.wait_for(both_claimers_entered.wait(), timeout=1)
    release_claimers.set()
    await releasers

    assert claim_call_count == 2
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_side_effect_owner_stops_before_recovery_grace_expires(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    now = datetime(2026, 9, 7, 16, 0, tzinfo=UTC).replace(tzinfo=None)
    claim_at = now - timedelta(seconds=LEASE_TAKEOVER_SECONDS - LEASE_SAFETY_MARGIN_SECONDS)
    monkeypatch.setattr(service_module, "naive_utc_now", lambda: now)
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    delete_attachments = AsyncMock()
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)

    await svc.run_terminal_side_effects(workflow_run, RetryDecision(False, 1, 0, True, "budget_exhausted"))

    delete_attachments.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_age_seconds", "webhook_starts"),
    [(539, False), (300, True)],
)
async def test_final_webhook_owner_must_have_room_for_its_delivery_window(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    lease_age_seconds: int,
    webhook_starts: bool,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    now = datetime(2026, 9, 7, 16, 0, tzinfo=UTC).replace(tzinfo=None)
    claim_at = now - timedelta(seconds=lease_age_seconds)
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    monkeypatch.setattr(service_module, "naive_utc_now", lambda: now)
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    final_webhook = AsyncMock()
    monkeypatch.setattr(svc, "execute_workflow_webhook", final_webhook)

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(False, 1, 0, True, "budget_exhausted"),
    )

    assert (final_webhook.await_count > 0) is webhook_starts
    if not webhook_starts:
        assert outcome == "fenced_out"
        assert attempt.webhook_sent_at is None


@pytest.mark.asyncio
async def test_policy_interim_webhook_keeps_full_delivery_window_and_records_marker(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    claim_at = datetime.now(UTC).replace(tzinfo=None)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="retry",
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )

    async def record_interim_marker(
        _workflow_run_id: str,
        _attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime | None = None,
        delivery_attempted: bool = False,
    ) -> bool:
        assert kind == "interim"
        assert expected_claim_at == claim_at
        attempt.interim_webhook_sent_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        AsyncMock(return_value=claim_at),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        record_interim_marker,
    )
    deliver = AsyncMock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "30"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    monkeypatch.setattr(webhook_delivery_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    original_fenced_effect = svc._run_fenced_terminal_side_effect
    effect_timeouts: dict[str, float | None] = {}

    async def record_fenced_effect(**kwargs: Any) -> bool:
        effect_timeouts[kwargs["effect_name"]] = kwargs.get("timeout_seconds")
        return await original_fenced_effect(**kwargs)

    monkeypatch.setattr(svc, "_run_fenced_terminal_side_effect", record_fenced_effect)

    outcome = await svc.run_terminal_side_effects(
        workflow_run,
        RetryDecision(True, 1, 0, True, "matched"),
    )

    assert outcome == "released"
    assert deliver.await_count == 2
    assert attempt.interim_webhook_sent_at is not None
    assert effect_timeouts["interim workflow webhook"] == WEBHOOK_FALLBACK_GRACE_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lease_age_seconds", "expected_fallback_ready"),
    [(400, False), (601, True)],
)
async def test_webhook_fallback_waits_for_the_shared_lease_takeover_bound(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    lease_age_seconds: int,
    expected_fallback_ready: bool,
) -> None:
    svc, _build_response, _update_run = webhook_service
    claim_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=lease_age_seconds)
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        side_effects_released_at=claim_at,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )

    fallback_ready = await svc._workflow_webhook_fallback_is_current(
        workflow_run_id="wr_abc",
        attempt_number=1,
        expected_claim_at=claim_at,
    )

    assert fallback_ready is expected_fallback_ready


@pytest.mark.asyncio
async def test_stale_terminal_side_effect_owner_is_fenced_after_recovery_takeover(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.failed
    owner_a_token = datetime.now(UTC)
    takeover_token = datetime.now(UTC)
    attempt = SimpleNamespace(
        attempt_number=1,
        webhook_sent_at=None,
        interim_webhook_sent_at=None,
        side_effects_released_at=None,
    )
    delete_calls = 0
    delete_owners: list[str] = []
    active_owner = "A"
    first_effect_started = asyncio.Event()
    allow_owner_a_to_resume = asyncio.Event()
    marker_tokens: list[datetime | None] = []

    async def claim_side_effects(
        _workflow_run_id: str,
        _attempt_number: int,
        *,
        kind: str = "final",
        expected_claim_at: datetime | None = None,
        stale_before: datetime | None = None,
    ) -> datetime | None:
        assert kind == "final"
        if expected_claim_at is None:
            assert attempt.side_effects_released_at is None
            attempt.side_effects_released_at = owner_a_token
            return owner_a_token
        assert expected_claim_at == owner_a_token
        if attempt.side_effects_released_at != expected_claim_at:
            return None
        assert stale_before is not None
        attempt.side_effects_released_at = takeover_token
        return takeover_token

    async def delete_attachments(*, run_id: str, timeout_seconds: float) -> None:
        nonlocal delete_calls
        assert run_id == workflow_run.workflow_run_id
        assert timeout_seconds > 0
        delete_calls += 1
        delete_owners.append(active_owner)
        if active_owner == "A" and delete_calls == 1:
            first_effect_started.set()
            await allow_owner_a_to_resume.wait()

    async def claim_webhook(
        _workflow_run_id: str,
        _attempt_number: int,
        *,
        kind: str,
        expected_claim_at: datetime | None = None,
        delivery_attempted: bool = False,
    ) -> bool:
        assert kind == "final"
        assert expected_claim_at == takeover_token
        marker_tokens.append(expected_claim_at)
        if attempt.webhook_sent_at is not None:
            return False
        attempt.webhook_sent_at = datetime.now(UTC)
        return True

    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "get_attempts",
        AsyncMock(return_value=[attempt]),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_side_effects",
        claim_side_effects,
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "claim_attempt_webhook",
        claim_webhook,
    )
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", delete_attachments)
    on_final = AsyncMock()
    on_completed = AsyncMock()
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", on_final)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_completed", on_completed)
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "prepare_workflow_webhook", AsyncMock(return_value=object()))
    deliver = AsyncMock()
    monkeypatch.setattr(svc, "deliver_prepared_workflow_webhook", deliver)

    decision = RetryDecision(False, 1, 0, True, "budget_exhausted")
    # Owner A pauses inside its first effect. Recovery takes over while it is paused; the fresh
    # fence must stop A before any later effect after it resumes.
    owner_a = asyncio.create_task(svc.run_terminal_side_effects(workflow_run, decision))
    await asyncio.wait_for(first_effect_started.wait(), timeout=1)

    active_owner = "B"
    takeover = asyncio.create_task(
        svc.run_terminal_side_effects(
            workflow_run,
            decision,
            side_effects_claim_at=owner_a_token,
            side_effects_stale_before=owner_a_token + timedelta(seconds=1),
        )
    )
    await asyncio.wait_for(takeover, timeout=1)
    active_owner = "A"
    allow_owner_a_to_resume.set()
    await asyncio.wait_for(owner_a, timeout=1)

    assert delete_calls == 2
    assert delete_owners == ["A", "B"]
    on_final.assert_awaited_once()
    on_completed.assert_awaited_once()
    deliver.assert_awaited_once()
    assert marker_tokens == [takeover_token]
    assert attempt.webhook_sent_at is not None
    assert attempt.side_effects_released_at == takeover_token


@pytest.mark.asyncio
@pytest.mark.parametrize("competing_owner", [False, True])
@pytest.mark.parametrize("failed_effect", ["attachments", "hook", "webhook"])
async def test_final_release_retries_keep_own_claim_and_completed_hook(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    competing_owner: bool,
    failed_effect: str,
) -> None:
    svc, build_response, _update_run = webhook_service
    workflow_run = _workflow_run()
    workflow_run.status = WorkflowRunStatus.canceled
    owner_token = datetime.now(UTC).replace(tzinfo=None)
    competing_token = owner_token + timedelta(seconds=1)
    attempt = SimpleNamespace(
        attempt_number=1,
        retry_decision="revoked",
        webhook_sent_at=None,
        interim_webhook_sent_at=owner_token - timedelta(seconds=1),
        side_effects_released_at=None,
    )
    claims: list[datetime | None] = []

    async def claim(*_args: Any, expected_claim_at: datetime | None = None, **_kwargs: Any) -> datetime | None:
        claims.append(expected_claim_at)
        if attempt.side_effects_released_at != expected_claim_at:
            return None
        attempt.side_effects_released_at = owner_token
        return owner_token

    async def mark_webhook(*_args: Any, expected_claim_at: datetime | None = None, **_kwargs: Any) -> bool:
        if attempt.side_effects_released_at != expected_claim_at:
            return False
        attempt.webhook_sent_at = owner_token
        return True

    preparation_calls = 0

    async def prepare(*_args: Any, **_kwargs: Any) -> _StatusResponse:
        nonlocal preparation_calls
        preparation_calls += 1
        if preparation_calls == 1 and failed_effect == "webhook":
            if competing_owner:
                attempt.side_effects_released_at = competing_token
            raise RuntimeError("transient webhook preparation failure")
        return _StatusResponse()

    build_response.side_effect = prepare
    repository = service_module.app.DATABASE.workflow_run_attempts
    monkeypatch.setattr(repository, "get_attempts", AsyncMock(return_value=[attempt]))
    monkeypatch.setattr(repository, "claim_attempt_side_effects", claim)
    monkeypatch.setattr(repository, "claim_attempt_webhook", mark_webhook)
    effects = [AsyncMock() for _ in range(5)]

    async def delete_attachments(**_kwargs: Any) -> None:
        if effects[0].await_count == 1 and failed_effect == "attachments":
            if competing_owner:
                attempt.side_effects_released_at = competing_token
            raise RuntimeError("transient attachment cleanup failure")

    effects[0].side_effect = delete_attachments
    if failed_effect == "hook":
        effects[4].side_effect = RuntimeError("final hook failed after emitting an effect")
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", effects[0])
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", effects[1])
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", effects[2])
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", effects[3])
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", effects[4])
    deliver = AsyncMock(return_value=httpx.Response(200))
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)

    outcome = await svc._run_terminal_side_effects_with_retries(
        workflow_run, RetryDecision(False, 1, 0, True, "canceled")
    )

    assert claims == ([None] if failed_effect == "hook" else [None, owner_token])
    assert effects[0].await_count == (2 if failed_effect == "attachments" and not competing_owner else 1)
    # The attempt is revoked (canceled during its retry delay), so the credential fallback is the
    # one final effect that must stay silent.
    assert effects[1].await_count == 0
    for effect in effects[2:]:
        assert effect.await_count == (0 if failed_effect == "attachments" and competing_owner else 1)
    if failed_effect == "hook":
        assert outcome == "effect_failed"
        assert attempt.webhook_sent_at is None
        deliver.assert_not_awaited()
    elif competing_owner:
        assert outcome in {"fenced_out", "lease_held_by_other_young"}
        assert attempt.webhook_sent_at is None
        deliver.assert_not_awaited()
    else:
        assert outcome == "released"
        assert preparation_calls == (2 if failed_effect == "webhook" else 1)
        assert attempt.webhook_sent_at is not None
        deliver.assert_awaited_once()


@pytest.mark.parametrize(
    "status, rule_status, rule_codes, errors, retries_used, expected",
    [
        ("canceled", "canceled", None, set(), 0, (False, "canceled")),
        ("failed", "failed", None, set(), 1, (True, "matched")),
        ("failed", "failed", None, set(), 2, (False, "budget_exhausted")),
        ("failed", "failed", None, set(), 3, (False, "budget_exhausted")),
        ("failed", "failed", [], {"ANY"}, 0, (True, "matched")),
        ("failed", "failed", ["A", "B"], {"B", "C"}, 0, (True, "matched")),
        ("failed", "failed", ["A", "B"], {"C"}, 0, (False, "no_match")),
        ("failed", "failed", ["A"], set(), 0, (False, "no_match")),
        ("completed", "failed", None, set(), 0, (False, "no_match")),
    ],
)
def test_evaluate_retry_policy_decisions(status, rule_status, rule_codes, errors, retries_used, expected) -> None:
    policy = WorkflowRetryPolicy.model_validate(
        {"max_retries": 2, "retry_on": [{"status": rule_status, "error_codes": rule_codes}]}
    )
    assert evaluate_retry_policy(policy, WorkflowRunStatus(status), errors, retries_used) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("version_exists", [True, False], ids=["soft_deleted", "missing"])
async def test_terminal_retry_resolves_deleted_pinned_workflow(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, version_exists: bool
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    run = WorkflowRun(
        workflow_run_id="wr_deleted_policy",
        workflow_id="w_deleted_policy",
        workflow_permanent_id="wpid_deleted_policy",
        organization_id="o_test",
        status=WorkflowRunStatus.failed,
        created_at=now,
        modified_at=now,
    )
    policy = WorkflowRetryPolicy(max_retries=2, delay_seconds=7, retry_on=[{"status": "failed"}])
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    workflows = WorkflowsRepository(session_factory)
    attempts = WorkflowRunAttemptsRepository(session_factory)
    async with session_factory() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=run.workflow_run_id,
                workflow_id=run.workflow_id,
                workflow_permanent_id=run.workflow_permanent_id,
                organization_id=run.organization_id,
                status="failed",
            )
        )
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=run.workflow_run_id,
                organization_id=run.organization_id,
                attempt_number=1,
                status="failed",
            )
        )
        if version_exists:
            session.add(
                WorkflowModel(
                    workflow_id=run.workflow_id,
                    workflow_permanent_id=run.workflow_permanent_id,
                    organization_id=run.organization_id,
                    title="Deleted pinned policy",
                    version=1,
                    is_saved_task=False,
                    deleted_at=now,
                    workflow_definition=WorkflowDefinition(parameters=[], blocks=[], retry_policy=policy).model_dump(
                        mode="json"
                    ),
                )
            )
        await session.commit()

    monkeypatch.setattr(service_module.app.DATABASE, "workflows", workflows)
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", attempts)
    monkeypatch.setattr(service_module.app, "WORKFLOW_SERVICE", WorkflowService())
    monkeypatch.setattr(service_module.app.DATABASE.tasks, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(service_module.app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))

    assert await _get_retry_policy(run) == (policy if version_exists else None)
    decision = await on_terminal_transition(run, WorkflowRunStatus.failed, None, None)
    assert decision.retry is version_exists
    assert decision.delay_seconds == (7 if version_exists else 0)
    recorded_attempts = await attempts.get_attempts(run.workflow_run_id)
    assert recorded_attempts[0].retry_decision == ("retry" if version_exists else "final")


@pytest.mark.asyncio
async def test_second_terminal_transition_refreshes_attempt_finished_at_but_keeps_the_decision(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A finally block reopens the run and terminalizes it again; compute cost is priced through the
    # attempt's finished_at, so the frozen decision must keep its verdict while the end time moves.
    first_finish = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5)
    run = WorkflowRun(
        workflow_run_id="wr_finally",
        workflow_id="w_finally",
        workflow_permanent_id="wpid_finally",
        organization_id="o_test",
        status=WorkflowRunStatus.failed,
        created_at=first_finish,
        modified_at=first_finish,
    )
    session_factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    attempts = WorkflowRunAttemptsRepository(session_factory)
    async with session_factory() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id=run.workflow_run_id,
                workflow_id=run.workflow_id,
                workflow_permanent_id=run.workflow_permanent_id,
                organization_id=run.organization_id,
                status="failed",
            )
        )
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=run.workflow_run_id,
                organization_id=run.organization_id,
                attempt_number=1,
                status="failed",
                retry_decision="final",
                decision_reason="no_match",
                finished_at=first_finish,
            )
        )
        await session.commit()
    monkeypatch.setattr(service_module.app.DATABASE, "workflows", WorkflowsRepository(session_factory))
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", attempts)
    monkeypatch.setattr(service_module.app.DATABASE.tasks, "get_tasks_by_workflow_run_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(service_module.app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))

    decision = await on_terminal_transition(run, WorkflowRunStatus.failed, None, None, attempt_number=1)

    assert decision.retry is False
    recorded = (await attempts.get_attempts(run.workflow_run_id))[0]
    assert (recorded.retry_decision, recorded.decision_reason) == ("final", "no_match")
    assert recorded.finished_at > first_finish


@pytest.mark.asyncio
@pytest.mark.parametrize("webhook_on_retry", ["every_attempt", "final_only"])
@pytest.mark.parametrize("delivery", ["sent", "no_url", "no_signing_key", "rejected"])
async def test_webhook_producer_to_projection(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    webhook_on_retry: Literal["every_attempt", "final_only"],
    delivery: str,
) -> None:
    svc, _build_response, _update_run = webhook_service
    now = datetime.now(UTC).replace(tzinfo=None)
    run = WorkflowRun(
        workflow_run_id="wr_abc",
        workflow_id="w_abc",
        workflow_permanent_id="wpid_abc",
        organization_id="o_abc",
        status=WorkflowRunStatus.failed,
        webhook_callback_url=None if delivery == "no_url" else "https://example.com/hook",
        created_at=now,
        modified_at=now,
        finished_at=now,
    )
    repository = WorkflowRunAttemptsRepository(async_sessionmaker(sqlite_engine, expire_on_commit=False))
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", repository)
    if delivery == "no_signing_key":
        monkeypatch.setattr(
            service_module.app.DATABASE.organizations, "get_valid_org_auth_token", AsyncMock(return_value=None)
        )
    deliver = AsyncMock(return_value=_response(400 if delivery == "rejected" else 200))
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", AsyncMock())
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())

    for number, retry in [(1, True), (2, False)]:
        run.status = WorkflowRunStatus.failed if retry else WorkflowRunStatus.completed
        async with repository.Session() as session:
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id=run.workflow_run_id,
                    organization_id=run.organization_id,
                    attempt_number=number,
                    status=run.status.value,
                    retry_decision="retry" if retry else "final",
                    finished_at=now,
                    next_attempt_prepared_at=None if not retry else now,
                )
            )
            await session.commit()
        outcome = await svc.run_terminal_side_effects(
            run, RetryDecision(retry, number, 0, not retry or webhook_on_retry == "every_attempt", "matched")
        )
        failed_delivery = delivery == "rejected" and (not retry or webhook_on_retry == "every_attempt")
        assert outcome == ("effect_failed" if failed_delivery else "released")

    attempts = await repository.get_attempts(run.workflow_run_id)
    assert (attempts[0].interim_webhook_sent_at is None) == (
        delivery == "rejected" and webhook_on_retry == "every_attempt"
    )
    assert (attempts[1].webhook_sent_at is None) == (delivery == "rejected")
    attempted = delivery in {"sent", "rejected"}
    assert deliver.await_count == ((2 if webhook_on_retry == "every_attempt" else 1) if attempted else 0)
    expected = [
        attempts[0].interim_webhook_sent_at if delivery == "sent" and webhook_on_retry == "every_attempt" else None,
        attempts[1].webhook_sent_at if delivery == "sent" else None,
    ]
    monkeypatch.setattr(
        svc, "build_workflow_run_status_response", WorkflowService.build_workflow_run_status_response.__get__(svc)
    )
    monkeypatch.setattr(svc, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_output_parameter_workflow_run_output_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(svc, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs, "get_workflow_run_retried_by", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service_module.app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))

    for current_policy in [webhook_on_retry, "final_only", "every_attempt", None]:
        workflow = Workflow(
            workflow_id=run.workflow_id,
            workflow_permanent_id=run.workflow_permanent_id,
            organization_id=run.organization_id,
            title="Webhook timestamps",
            version=1,
            is_saved_task=False,
            workflow_definition=WorkflowDefinition(
                parameters=[],
                blocks=[],
                retry_policy=WorkflowRetryPolicy(webhook_on_retry=current_policy, retry_on=[{"status": "failed"}])
                if current_policy is not None
                else None,
            ),
            created_at=now,
            modified_at=now,
        )
        monkeypatch.setattr(
            svc, "_gather_with_max_in_flight", AsyncMock(return_value=(workflow, run, None, [], [], attempts))
        )
        response = await svc.build_workflow_run_status_response(
            run.workflow_permanent_id, run.workflow_run_id, run.organization_id
        )
        assert [attempt.webhook_sent_at for attempt in response.attempts] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("webhook_on_retry", ["final_only", "every_attempt", None])
async def test_attempt_response_does_not_infer_delivery_from_legacy_completion_markers(
    monkeypatch: pytest.MonkeyPatch, webhook_on_retry: Literal["final_only", "every_attempt"] | None
) -> None:
    interim_sent_at = datetime.now(UTC)
    final_sent_at = interim_sent_at + timedelta(seconds=1)
    run = WorkflowRun(
        workflow_run_id="wr_webhook",
        workflow_id="wf_webhook",
        workflow_permanent_id="wpid_webhook",
        organization_id="org_test",
        status=WorkflowRunStatus.completed,
        created_at=interim_sent_at,
        modified_at=final_sent_at,
        finished_at=final_sent_at,
    )
    policy = (
        WorkflowRetryPolicy(webhook_on_retry=webhook_on_retry, retry_on=[{"status": "failed"}])
        if webhook_on_retry is not None
        else None
    )
    workflow = Workflow(
        workflow_id=run.workflow_id,
        workflow_permanent_id=run.workflow_permanent_id,
        organization_id=run.organization_id,
        title="Webhook timestamps",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[], retry_policy=policy),
        created_at=interim_sent_at,
        modified_at=final_sent_at,
    )
    attempts = [
        WorkflowRunAttemptModel(
            workflow_run_id=run.workflow_run_id,
            organization_id=run.organization_id,
            attempt_number=1,
            status="failed",
            retry_decision="retry",
            finished_at=interim_sent_at,
            interim_webhook_sent_at=interim_sent_at,
        ),
        WorkflowRunAttemptModel(
            workflow_run_id=run.workflow_run_id,
            organization_id=run.organization_id,
            attempt_number=2,
            status="completed",
            retry_decision="final",
            finished_at=final_sent_at,
            interim_webhook_sent_at=interim_sent_at,
            webhook_sent_at=final_sent_at,
        ),
    ]
    svc = WorkflowService()
    monkeypatch.setattr(
        svc, "_gather_with_max_in_flight", AsyncMock(return_value=(workflow, run, None, [], [], attempts))
    )
    monkeypatch.setattr(svc, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_output_parameter_workflow_run_output_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(svc, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_runs, "get_workflow_run_retried_by", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(service_module.app.DATABASE.observer, "get_workflow_run_blocks", AsyncMock(return_value=[]))

    response = await svc.build_workflow_run_status_response(
        run.workflow_permanent_id, run.workflow_run_id, run.organization_id
    )

    assert response.attempts[0].webhook_sent_at is None
    assert response.attempts[1].webhook_sent_at is None
    assert attempts[0].interim_webhook_sent_at == interim_sent_at


@pytest.mark.asyncio
@pytest.mark.parametrize("cap_output_values", [False, True])
async def test_attempt_failure_reason_uses_response_text_cap(
    monkeypatch: pytest.MonkeyPatch, cap_output_values: bool
) -> None:
    now = datetime.now(UTC)
    reason = "failure " * (18 * 1024 * 1024 // 8)
    run = WorkflowRun(
        workflow_run_id="wr_reason",
        workflow_id="wf_reason",
        workflow_permanent_id="wpid_reason",
        organization_id="org_test",
        status=WorkflowRunStatus.failed,
        failure_reason=reason,
        created_at=now,
        modified_at=now,
    )
    workflow = Workflow(
        workflow_id=run.workflow_id,
        workflow_permanent_id=run.workflow_permanent_id,
        organization_id=run.organization_id,
        title="Retry reason",
        version=1,
        is_saved_task=False,
        workflow_definition=WorkflowDefinition(parameters=[], blocks=[]),
        created_at=now,
        modified_at=now,
    )
    attempt = WorkflowRunAttemptModel(
        workflow_run_id=run.workflow_run_id,
        organization_id=run.organization_id,
        attempt_number=1,
        status="failed",
        retry_decision="final",
        failure_reason=reason,
    )
    monkeypatch.setattr(
        service_module.app,
        "DATABASE",
        SimpleNamespace(
            workflows=SimpleNamespace(get_workflow_for_workflow_run=AsyncMock(return_value=workflow)),
            observer=SimpleNamespace(
                get_task_v2_by_workflow_run_id=AsyncMock(return_value=None),
                get_workflow_run_blocks=AsyncMock(return_value=[]),
            ),
            tasks=SimpleNamespace(get_tasks_by_workflow_run_id=AsyncMock(return_value=[])),
            workflow_runs=SimpleNamespace(
                get_workflow_run_parameters=AsyncMock(return_value=[]),
                get_workflow_run_block_errors=AsyncMock(return_value=[]),
                get_workflow_run_retried_by=AsyncMock(return_value=None),
            ),
            workflow_run_attempts=SimpleNamespace(get_attempts=AsyncMock(return_value=[attempt])),
        ),
    )
    svc = WorkflowService()
    monkeypatch.setattr(svc, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(svc, "get_recent_workflow_screenshot_urls", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "get_output_parameter_workflow_run_output_parameter_tuples", AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_fetch_recording_urls", AsyncMock(return_value=([], False)))
    monkeypatch.setattr(svc, "_fetch_downloaded_files", AsyncMock(return_value=([], None)))
    response = await svc.build_workflow_run_status_response(
        run.workflow_permanent_id,
        run.workflow_run_id,
        run.organization_id,
        cap_output_values=cap_output_values,
    )
    assert response.attempts[0].failure_reason == response.failure_reason
    if cap_output_values:
        assert len(response.attempts[0].failure_reason.encode("utf-8")) <= service_module.RUN_RESPONSE_MAX_VALUE_BYTES
        assert response.attempts[0].failure_reason != reason
    else:
        assert response.attempts[0].failure_reason == reason
    assert attempt.failure_reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["interim", "final"])
@pytest.mark.parametrize("failure", ["non_2xx", "exception", "none", "permanent"])
async def test_webhook_claim_requires_successful_delivery(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure: str,
) -> None:
    svc, _build_response, _update_run = webhook_service
    run = _workflow_run()
    run.status = WorkflowRunStatus.failed
    repository = WorkflowRunAttemptsRepository(async_sessionmaker(sqlite_engine, expire_on_commit=False))
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", repository)
    async with repository.Session() as session:
        session.add(
            WorkflowRunAttemptModel(
                workflow_run_id=run.workflow_run_id,
                organization_id=run.organization_id,
                attempt_number=1,
                status="failed",
                retry_decision="retry" if kind == "interim" else "final",
                finished_at=datetime.now(UTC).replace(tzinfo=None),
                next_attempt_prepared_at=datetime.now(UTC).replace(tzinfo=None) if kind == "interim" else None,
            )
        )
        await session.commit()
    monkeypatch.setattr(service_module.uploaded_file_service, "delete_files_attached_to_run", AsyncMock())
    final_hook = AsyncMock()
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "on_workflow_run_final", final_hook)
    monkeypatch.setattr(svc, "_start_credential_fallback_retry_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_apply_completion_run_tags_best_effort", AsyncMock())
    monkeypatch.setattr(svc, "_run_workflow_run_terminal_hooks", AsyncMock())
    monkeypatch.setattr(webhook_delivery_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))
    delivery_calls = 0

    async def deliver(**_kwargs: object) -> httpx.Response:
        nonlocal delivery_calls
        delivery_calls += 1
        if failure != "none" and (
            failure == "permanent" or delivery_calls <= webhook_delivery_module.WEBHOOK_DELIVERY_MAX_ATTEMPTS
        ):
            if failure == "exception":
                raise httpx.ConnectError("endpoint unreachable")
            return _response(503)
        return _response(204)

    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    decision = RetryDecision(kind == "interim", 1, 0, True, "matched")
    outcome = await svc.run_terminal_side_effects(run, decision)
    attempt = (await repository.get_attempts(run.workflow_run_id))[0]
    marker = attempt.interim_webhook_sent_at if kind == "interim" else attempt.webhook_sent_at
    progress = attempt.interim_side_effects_progress if kind == "interim" else attempt.final_side_effects_progress
    if failure != "none":
        assert outcome == "effect_failed"
        assert marker is None
        assert not (progress or {}).get("webhook_delivery_attempted")
        assert f"{kind} workflow webhook" not in (progress or {}).get("completed_effects", [])
        assert delivery_calls == webhook_delivery_module.WEBHOOK_DELIVERY_MAX_ATTEMPTS
        outcome = await svc._run_terminal_side_effects_with_retries(
            run,
            decision,
            side_effects_claim_at=attempt.side_effects_released_at,
            side_effects_stale_before=datetime.now(UTC).replace(tzinfo=None),
        )
        attempt = (await repository.get_attempts(run.workflow_run_id))[0]
        marker = attempt.interim_webhook_sent_at if kind == "interim" else attempt.webhook_sent_at
        progress = attempt.interim_side_effects_progress if kind == "interim" else attempt.final_side_effects_progress
    if failure == "permanent":
        assert outcome == "released"
        assert marker is not None
        assert not (progress or {}).get("webhook_delivery_attempted")
        assert progress["webhook_delivery_attempts"] == 15
        assert progress["webhook_delivery_exhausted_at"]
        assert delivery_calls == 45
        assert _attempt_from_record(attempt).webhook_sent_at is None
        aged = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=LEASE_TAKEOVER_SECONDS + 1)
        async with repository.Session() as session:
            row = await session.get(WorkflowRunAttemptModel, (run.workflow_run_id, 1))
            row.side_effects_released_at = aged
            row.modified_at = aged
            await session.commit()
        monkeypatch.setattr(service_module.app, "WORKFLOW_SERVICE", svc)
        await BackgroundTaskExecutor()._recover_pending_retries_once(resume_fresh=False)
        assert await repository.list_attempts_needing_recovery(datetime.now(UTC).replace(tzinfo=None)) == []
        await svc.execute_workflow_webhook(run, claim_kind=kind, attempt_number=1)
        assert delivery_calls == 45
        attempt = (await repository.get_attempts(run.workflow_run_id))[0]
        assert _attempt_from_record(attempt).webhook_sent_at is None
    else:
        assert outcome == "released"
        assert marker is not None
        assert progress["webhook_delivery_attempted"] is True
        assert delivery_calls == (1 if failure == "none" else webhook_delivery_module.WEBHOOK_DELIVERY_MAX_ATTEMPTS + 1)
    assert final_hook.await_count == int(kind == "final")


@pytest.mark.asyncio
@pytest.mark.parametrize("later_attempt_delivered", ["final", "interim", None])
async def test_interim_recovery_skips_delivery_once_a_later_attempt_finished(
    monkeypatch: pytest.MonkeyPatch, later_attempt_delivered: str | None
) -> None:
    run = SimpleNamespace(workflow_run_id="wr_1", organization_id="o_1")
    monkeypatch.setattr(service_module.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(
        service_module,
        "get_recorded_decision",
        AsyncMock(return_value=service_module.RetryDecision(True, 1, 0, True, "matched")),
    )
    delivered_at = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        SimpleNamespace(attempt_number=1, webhook_sent_at=None, interim_webhook_sent_at=None),
        SimpleNamespace(
            attempt_number=2,
            webhook_sent_at=delivered_at if later_attempt_delivered == "final" else None,
            interim_webhook_sent_at=delivered_at if later_attempt_delivered == "interim" else None,
        ),
    ]
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=rows))
    claim = AsyncMock(return_value=True)
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", claim)
    monkeypatch.setattr(service_module, "_get_recovery_api_key", AsyncMock(return_value="token"))
    release = AsyncMock(return_value="released")
    monkeypatch.setattr(service_module.app.WORKFLOW_SERVICE, "_run_terminal_side_effects_with_retries", release)
    pending = SimpleNamespace(
        workflow_run_id="wr_1",
        organization_id="o_1",
        attempt_number=1,
        side_effects_released_at=None,
        decision_reason="matched",
    )

    await service_module._recover_unreleased_terminal_attempt(pending, interim=True)

    if later_attempt_delivered:
        release.assert_not_awaited()
        claim.assert_awaited_once_with("wr_1", 1, "interim")
    else:
        release.assert_awaited_once()
        claim.assert_not_awaited()


def _delivery_projection_call(update_run: AsyncMock) -> Any | None:
    for call in update_run.await_args_list:
        if "webhook_delivery_status" in call.kwargs:
            return call
    return None


def test_resolve_webhook_delivery_projection_is_monotonic() -> None:
    delivered = WebhookDeliveryStatus.delivered
    exhausted = WebhookDeliveryStatus.exhausted_unattributed
    customer = WebhookDeliveryStatus.exhausted_customer_config

    # First terminal write from an unknown (NULL) state records whatever arrives.
    assert resolve_webhook_delivery_projection(None, delivered) == delivered
    assert resolve_webhook_delivery_projection(None, exhausted) == exhausted

    # delivered is the highest terminal state and is never downgraded.
    assert resolve_webhook_delivery_projection(delivered, exhausted) == delivered
    assert resolve_webhook_delivery_projection(delivered, customer) == delivered
    assert resolve_webhook_delivery_projection(delivered, delivered) == delivered

    # A later successful replay upgrades an exhausted result to delivered.
    assert resolve_webhook_delivery_projection(exhausted, delivered) == delivered

    # A fresh exhausted classification may refine an earlier exhausted one.
    assert resolve_webhook_delivery_projection(exhausted, customer) == customer


def test_classify_exhausted_webhook_delivery_by_url_structure() -> None:
    assert (
        webhook_delivery_module.classify_exhausted_webhook_delivery("https://example.com/hook")
        == WebhookDeliveryStatus.exhausted_unattributed
    )
    assert (
        webhook_delivery_module.classify_exhausted_webhook_delivery("not-a-valid-url")
        == WebhookDeliveryStatus.exhausted_customer_config
    )
    assert (
        webhook_delivery_module.classify_exhausted_webhook_delivery("http://")
        == WebhookDeliveryStatus.exhausted_customer_config
    )
    assert (
        webhook_delivery_module.classify_exhausted_webhook_delivery(None)
        == WebhookDeliveryStatus.exhausted_customer_config
    )


@pytest.mark.asyncio
async def test_final_delivery_projects_delivered_status(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", AsyncMock(return_value=_response(200, "ok")))

    await svc.execute_workflow_webhook(_workflow_run(), claim_kind=None)

    projection = _delivery_projection_call(update_run)
    assert projection is not None
    assert projection.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.delivered
    assert projection.kwargs["webhook_delivery_finalized_at"] is not None


@pytest.mark.asyncio
async def test_interim_delivery_does_not_project_final_status(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    attempt = SimpleNamespace(
        attempt_number=1, webhook_sent_at=None, interim_webhook_sent_at=None, side_effects_released_at=None
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(svc, "_workflow_webhook_fallback_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "reserve_attempt_webhook_delivery",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", AsyncMock(return_value=_response(200, "ok")))

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run(), claim_kind="interim", attempt_number=1)

    assert _delivery_projection_call(update_run) is None
    assert _finalized_events(logs) == []


@pytest.mark.asyncio
async def test_no_webhook_configured_projects_no_status(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    run = _workflow_run()
    run.webhook_callback_url = None
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", AsyncMock())

    await svc.execute_workflow_webhook(run, claim_kind=None)

    assert _delivery_projection_call(update_run) is None


@pytest.mark.asyncio
async def test_terminal_failure_projects_exhausted_unattributed(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    monkeypatch.setattr(
        service_module, "deliver_webhook_with_retries", AsyncMock(return_value=_response(400, "bad request"))
    )

    await svc.execute_workflow_webhook(_workflow_run(), claim_kind=None)

    projection = _delivery_projection_call(update_run)
    assert projection is not None
    assert projection.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.exhausted_unattributed
    assert projection.kwargs["webhook_delivery_finalized_at"] is not None


@pytest.mark.asyncio
async def test_terminal_failure_invalid_url_projects_customer_config(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    # A scheme-less URL passes prepare (its validator prepends a scheme) but delivery rejects the
    # raw value, so this is the reachable path to a deterministic client/config classification.
    run = _workflow_run()
    run.webhook_callback_url = "example.com/hook"
    monkeypatch.setattr(
        service_module, "deliver_webhook_with_retries", AsyncMock(side_effect=InvalidUrl("example.com/hook"))
    )

    await svc.execute_workflow_webhook(run, claim_kind=None)

    projection = _delivery_projection_call(update_run)
    assert projection is not None
    assert projection.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.exhausted_customer_config


@pytest.mark.asyncio
async def test_transient_final_failure_before_exhaustion_projects_no_status(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    attempt = SimpleNamespace(
        attempt_number=1, webhook_sent_at=None, interim_webhook_sent_at=None, side_effects_released_at=None
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(svc, "_workflow_webhook_fallback_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "reserve_attempt_webhook_delivery",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", AsyncMock(return_value=_response(503)))

    with capture_logs() as logs, pytest.raises(RuntimeError, match="attempt delivery remains pending"):
        await svc.execute_workflow_webhook(_workflow_run(), claim_kind="final", attempt_number=1)

    assert _delivery_projection_call(update_run) is None
    assert _finalized_events(logs) == []


@pytest.mark.asyncio
async def test_final_outer_exhaustion_projects_exhausted_status(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _build_response, update_run = webhook_service
    attempt = SimpleNamespace(
        attempt_number=1, webhook_sent_at=None, interim_webhook_sent_at=None, side_effects_released_at=None
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[attempt])
    )
    monkeypatch.setattr(svc, "_workflow_webhook_fallback_is_current", AsyncMock(return_value=True))
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts,
        "reserve_attempt_webhook_delivery",
        AsyncMock(return_value=WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS),
    )
    monkeypatch.setattr(
        service_module.app.DATABASE.workflow_run_attempts, "claim_attempt_webhook", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", AsyncMock(return_value=_response(503)))

    await svc.execute_workflow_webhook(_workflow_run(), claim_kind="final", attempt_number=1)

    projection = _delivery_projection_call(update_run)
    assert projection is not None
    assert projection.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.exhausted_unattributed
    assert projection.kwargs["webhook_delivery_finalized_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_kind", ["final", "interim"])
@pytest.mark.parametrize("raises", [False, True])
async def test_no_attempt_failure_projects_only_final_exhaustion(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    claim_kind: Literal["final", "interim"],
    raises: bool,
) -> None:
    svc, _build_response, update_run = webhook_service
    monkeypatch.setattr(service_module.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    deliver = AsyncMock(side_effect=httpx.ReadTimeout("timeout")) if raises else AsyncMock(return_value=_response(503))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run(), claim_kind=claim_kind)

    deliver.assert_awaited_once()
    update_run.assert_awaited_once()
    projection = _delivery_projection_call(update_run)
    if claim_kind == "final":
        assert projection is not None
        assert projection.kwargs["webhook_delivery_status"] == WebhookDeliveryStatus.exhausted_unattributed
        assert projection.kwargs["webhook_delivery_finalized_at"] is not None
    else:
        assert projection is None


def _finalized_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event for event in logs if event["event"] == "Workflow webhook delivery finalized"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (_response(200, "ok"), ("delivered", "2xx", 1)),
        (_response(503, "synthetic-endpoint-secret"), ("exhausted_unattributed", "5xx", 3)),
        (httpx.ConnectError("connection refused"), ("exhausted_unattributed", "no_response", 3)),
    ],
    ids=["delivered", "final-http-failure", "no-response-exhaustion"],
)
async def test_final_delivery_outcome_logs_exactly_one_bounded_line(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    outcome: httpx.Response | Exception,
    expected: tuple[str, str, int],
) -> None:
    # Result-delivery rate is read off this line, so it is unsampled and carries only bounded fields:
    # no URL, headers or response body.
    svc, _build_response, _update_run = webhook_service
    deliver = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    monkeypatch.setattr(webhook_delivery_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))

    with capture_logs() as logs:
        await svc.execute_workflow_webhook(_workflow_run(), claim_kind=None)

    [event] = _finalized_events(logs)
    assert (event["delivery_outcome"], event["http_status_class"], event["attempts"]) == expected
    assert event["delivery_seconds"] > 0
    assert event["replay"] is False
    assert set(event) == {
        "event",
        "log_level",
        "workflow_run_id",
        "delivery_outcome",
        "http_status_class",
        "attempts",
        "delivery_seconds",
        "replay",
    }


DELIVERED = WebhookDeliveryStatus.delivered
EXHAUSTED = WebhookDeliveryStatus.exhausted_unattributed
FINALIZED_AT = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
DEFAULT_URL = "https://example.com/webhook"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["final", "interim"])
@pytest.mark.parametrize("callback_url", [DEFAULT_URL, "not-a-valid-url"])
@pytest.mark.parametrize("current,stale_claim", [(None, False), (DELIVERED, False), (None, True)])
@pytest.mark.parametrize("execution_change", ["matching", "reset", "refinished"])
async def test_webhook_reservation_recovery_projects_only_final_exhaustion(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["final", "interim"],
    callback_url: str,
    current: WebhookDeliveryStatus | None,
    stale_claim: bool,
    execution_change: str,
) -> None:
    svc, _build_response, _update_run = webhook_service
    run = _workflow_run()
    run.webhook_callback_url = callback_url
    sessions = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    runs = WorkflowRunsRepository(sessions)
    attempts = WorkflowRunAttemptsRepository(sessions)
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_runs", runs)
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", attempts)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    deliver = AsyncMock()
    monkeypatch.setattr(service_module.app.AGENT_FUNCTION, "deliver_webhook", deliver)
    progress = {"webhook_delivery_attempts": WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS}
    async with sessions() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id=run.workflow_run_id,
                    workflow_id=run.workflow_id,
                    workflow_permanent_id=run.workflow_permanent_id,
                    organization_id=run.organization_id,
                    status="created" if execution_change == "reset" else "completed",
                    webhook_callback_url=callback_url,
                    finished_at=None
                    if execution_change == "reset"
                    else FINALIZED_AT + timedelta(microseconds=1 if execution_change == "refinished" else 0),
                    webhook_delivery_status=current if execution_change == "matching" else None,
                    webhook_delivery_finalized_at=FINALIZED_AT if current and execution_change == "matching" else None,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id=run.workflow_run_id,
                    organization_id=run.organization_id,
                    attempt_number=1,
                    status="failed",
                    retry_decision="final" if kind == "final" else "retry",
                    final_side_effects_progress=progress if kind == "final" else None,
                    interim_side_effects_progress=progress if kind == "interim" else None,
                ),
            ]
        )
        await session.commit()

    reservations: list[int | None] = []
    reserve = attempts.reserve_attempt_webhook_delivery
    expected = current if execution_change == "matching" else None
    if kind == "final" and current is None and not stale_claim and execution_change == "matching":
        expected = EXHAUSTED if callback_url == DEFAULT_URL else WebhookDeliveryStatus.exhausted_customer_config

    async def record_reservation(*args: Any, **kwargs: Any) -> int | None:
        result = await reserve(*args, **kwargs)
        # A worker can stop here, before the service executes another statement.
        async with sessions() as session:
            persisted_run = await session.get(WorkflowRunModel, run.workflow_run_id)
            assert persisted_run is not None
            assert persisted_run.webhook_delivery_status == expected
        reservations.append(result)
        return result

    monkeypatch.setattr(attempts, "reserve_attempt_webhook_delivery", record_reservation)
    await svc.execute_workflow_webhook(
        run,
        claim_kind=kind,
        attempt_number=1,
        side_effects_claim_at=FINALIZED_AT if stale_claim else None,
        skip_side_effects_lease_check=stale_claim,
    )

    assert reservations == [None if stale_claim else 0]
    deliver.assert_not_awaited()
    attempt = (await attempts.get_attempts(run.workflow_run_id))[0]
    recorded_progress = (
        attempt.final_side_effects_progress if kind == "final" else attempt.interim_side_effects_progress
    )
    assert recorded_progress["webhook_delivery_attempts"] == WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS
    assert bool(recorded_progress.get("webhook_delivery_exhausted_at")) is not stale_claim
    assert not recorded_progress.get("webhook_delivery_attempted")
    marker = attempt.webhook_sent_at if kind == "final" else attempt.interim_webhook_sent_at
    assert (marker is not None) is not stale_claim
    assert _attempt_from_record(attempt).webhook_sent_at is None
    async with sessions() as session:
        row = await session.get(WorkflowRunModel, run.workflow_run_id)
        assert row is not None
        assert row.webhook_delivery_status == expected
        if current == DELIVERED and execution_change == "matching":
            assert row.webhook_delivery_finalized_at == FINALIZED_AT
        else:
            assert (row.webhook_delivery_finalized_at is not None) is (expected is not None)
            if expected is not None:
                assert row.webhook_delivery_finalized_at == marker


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, EXHAUSTED, DELIVERED])
@pytest.mark.parametrize("incoming", [EXHAUSTED, DELIVERED])
async def test_projection_update_locks_before_read_and_preserves_delivery(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    current: WebhookDeliveryStatus | None,
    incoming: WebhookDeliveryStatus,
) -> None:
    statements: list[str] = []

    class RecordingSession(AsyncSession):
        async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return await super().scalars(statement, *args, **kwargs)

    sessions = async_sessionmaker(sqlite_engine, class_=RecordingSession, expire_on_commit=False)
    repository = WorkflowRunsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_projection",
                workflow_id="w_projection",
                workflow_permanent_id="wpid_projection",
                organization_id="o_projection",
                status="completed",
                webhook_delivery_status=current,
                webhook_delivery_finalized_at=FINALIZED_AT if current else None,
            )
        )
        await session.commit()

    new_time = FINALIZED_AT + timedelta(seconds=1)
    await repository.update_workflow_run(
        "wr_projection",
        webhook_failure_reason="legacy reason",
        webhook_delivery_status=incoming,
        webhook_delivery_finalized_at=new_time,
    )

    # SQLite exercises persistence; the actual SELECT must also lock on PostgreSQL.
    assert len(statements) == 1
    assert "FOR UPDATE" in statements[0]
    expected = DELIVERED if current == DELIVERED else incoming
    expected_time = new_time if expected != current else FINALIZED_AT if current else None
    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_projection")
        assert row is not None
        assert row.webhook_delivery_status == expected
        assert row.webhook_delivery_finalized_at == expected_time
        assert row.webhook_failure_reason == "legacy reason"


@pytest.mark.asyncio
async def test_projection_omitted_update_leaves_projection_unchanged(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []

    class RecordingSession(AsyncSession):
        async def scalars(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return await super().scalars(statement, *args, **kwargs)

    sessions = async_sessionmaker(sqlite_engine, class_=RecordingSession, expire_on_commit=False)
    repository = WorkflowRunsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_omitted",
                workflow_id="w_projection",
                workflow_permanent_id="wpid_projection",
                organization_id="o_projection",
                status="running",
                webhook_delivery_status=DELIVERED,
                webhook_delivery_finalized_at=FINALIZED_AT,
            )
        )
        await session.commit()

    # An unrelated update that omits the projection kwargs must neither lock nor touch projection.
    await repository.update_workflow_run("wr_omitted", webhook_failure_reason="something else")

    assert len(statements) == 1
    assert "FOR UPDATE" not in statements[0]
    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_omitted")
        assert row is not None
        assert row.webhook_delivery_status == DELIVERED
        assert row.webhook_delivery_finalized_at == FINALIZED_AT
        assert row.webhook_failure_reason == "something else"


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [DELIVERED, EXHAUSTED, None])
async def test_projection_explicit_none_clears_both_fields(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, current: WebhookDeliveryStatus | None
) -> None:
    sessions = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repository = WorkflowRunsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_clear",
                workflow_id="w_projection",
                workflow_permanent_id="wpid_projection",
                organization_id="o_projection",
                status="created",
                webhook_delivery_status=current,
                webhook_delivery_finalized_at=FINALIZED_AT if current else None,
            )
        )
        await session.commit()

    # Explicit None is the reset/rerun clear: it wipes even a monotonic `delivered`.
    await repository.update_workflow_run("wr_clear", webhook_delivery_status=None, webhook_delivery_finalized_at=None)

    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_clear")
        assert row is not None
        assert row.webhook_delivery_status is None
        assert row.webhook_delivery_finalized_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["run_update", "attempt_update", "commit"])
async def test_reservation_recovery_exhaustion_projection_is_atomic_with_marker(
    sqlite_engine: AsyncEngine,
    failure_point: str,
) -> None:
    class FailingCommitSession(AsyncSession):
        async def commit(self) -> None:
            if failure_point == "commit":
                await self.flush()
                raise RuntimeError("injected commit failure")
            await super().commit()

    def fail_after_write(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        table = {"run_update": "workflow_runs", "attempt_update": "workflow_run_attempts"}.get(failure_point)
        if table is not None and statement.startswith(f"UPDATE {table} "):
            raise RuntimeError("injected update failure")

    attempts = WorkflowRunAttemptsRepository(
        async_sessionmaker(sqlite_engine, class_=FailingCommitSession, expire_on_commit=False)
    )
    seed = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    progress = {"webhook_delivery_attempts": WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS}
    async with seed() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_atomic",
                    workflow_id="w_atomic",
                    workflow_permanent_id="wpid_atomic",
                    organization_id="o_atomic",
                    status="completed",
                    finished_at=FINALIZED_AT,
                    webhook_delivery_status=None,
                    webhook_delivery_finalized_at=None,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_atomic",
                    organization_id="o_atomic",
                    attempt_number=1,
                    status="failed",
                    retry_decision="final",
                    final_side_effects_progress=progress,
                ),
            ]
        )
        await session.commit()

    event.listen(sqlite_engine.sync_engine, "after_cursor_execute", fail_after_write)
    try:
        with pytest.raises(RuntimeError, match="injected (commit|update) failure"):
            await attempts.reserve_attempt_webhook_delivery(
                "wr_atomic",
                1,
                kind="final",
                expected_claim_at=None,
                max_attempts=WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS,
                final_exhausted_projection=EXHAUSTED,
                expected_status="completed",
                expected_finished_at=FINALIZED_AT,
            )
    finally:
        event.remove(sqlite_engine.sync_engine, "after_cursor_execute", fail_after_write)

    async with seed() as session:
        run = await session.get(WorkflowRunModel, "wr_atomic")
        assert run is not None
        assert run.webhook_delivery_status is None
        assert run.webhook_delivery_finalized_at is None
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_atomic", 1))
        assert attempt is not None
        assert attempt.webhook_sent_at is None
        assert attempt.final_side_effects_progress == progress

    result = await WorkflowRunAttemptsRepository(seed).reserve_attempt_webhook_delivery(
        "wr_atomic",
        1,
        kind="final",
        expected_claim_at=None,
        max_attempts=WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS,
        final_exhausted_projection=EXHAUSTED,
        expected_status="completed",
        expected_finished_at=FINALIZED_AT,
    )
    assert result == 0
    async with seed() as session:
        run = await session.get(WorkflowRunModel, "wr_atomic")
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_atomic", 1))
        assert run is not None and attempt is not None
        assert run.webhook_delivery_status == EXHAUSTED
        assert run.webhook_delivery_finalized_at is not None
        assert attempt.webhook_sent_at == run.webhook_delivery_finalized_at
        assert (
            attempt.final_side_effects_progress["webhook_delivery_exhausted_at"] == attempt.webhook_sent_at.isoformat()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [None, EXHAUSTED])
async def test_reservation_recovery_refreshes_a_stale_bound_session(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, current: WebhookDeliveryStatus | None
) -> None:
    statements: list[str] = []

    class RecordingSession(AsyncSession):
        async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
            return await super().scalar(statement, *args, **kwargs)

    sessions = async_sessionmaker(sqlite_engine, class_=RecordingSession, expire_on_commit=False)
    runs = WorkflowRunsRepository(sessions)
    attempts = WorkflowRunAttemptsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    async with sessions() as session:
        session.add_all(
            [
                WorkflowRunModel(
                    workflow_run_id="wr_stale_recovery",
                    workflow_id="w_projection",
                    workflow_permanent_id="wpid_projection",
                    organization_id="o_projection",
                    status="completed",
                    finished_at=FINALIZED_AT,
                    webhook_delivery_status=current,
                    webhook_delivery_finalized_at=FINALIZED_AT if current else None,
                ),
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_stale_recovery",
                    organization_id="o_projection",
                    attempt_number=1,
                    status="failed",
                    retry_decision="final",
                    final_side_effects_progress={"webhook_delivery_attempts": WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS},
                ),
            ]
        )
        await session.commit()

    async with sessions() as stale_session:
        stale = await stale_session.get(WorkflowRunModel, "wr_stale_recovery")
        assert stale is not None
        await runs.update_workflow_run(
            "wr_stale_recovery", webhook_delivery_status=DELIVERED, webhook_delivery_finalized_at=FINALIZED_AT
        )
        assert stale.webhook_delivery_status == current
        monkeypatch.setattr(attempts, "Session", lambda: nullcontext(stale_session))
        result = await attempts.reserve_attempt_webhook_delivery(
            "wr_stale_recovery",
            1,
            kind="final",
            expected_claim_at=None,
            max_attempts=WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS,
            final_exhausted_projection=EXHAUSTED,
            expected_status="completed",
            expected_finished_at=FINALIZED_AT,
        )

    assert result == 0
    locked_tables = [statement.split("FROM ", 1)[1].split()[0] for statement in statements if "FOR UPDATE" in statement]
    assert locked_tables == ["workflow_runs", "workflow_run_attempts"]
    run_queries = [statement for statement in statements if "FROM workflow_runs" in statement]
    assert len(run_queries) == 1
    assert "FOR UPDATE" in run_queries[0]
    async with sessions() as session:
        run = await session.get(WorkflowRunModel, "wr_stale_recovery")
        attempt = await session.get(WorkflowRunAttemptModel, ("wr_stale_recovery", 1))
        assert run is not None and attempt is not None
        assert run.webhook_delivery_status == DELIVERED
        assert run.webhook_delivery_finalized_at == FINALIZED_AT
        assert attempt.webhook_sent_at is not None
        assert (
            attempt.final_side_effects_progress["webhook_delivery_exhausted_at"] == attempt.webhook_sent_at.isoformat()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", [False, True])
async def test_projection_update_refreshes_a_stale_bound_session(
    sqlite_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, fenced: bool
) -> None:
    sessions = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repository = WorkflowRunsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_stale_projection",
                workflow_id="w_projection",
                workflow_permanent_id="wpid_projection",
                organization_id="o_projection",
                status="completed",
                finished_at=FINALIZED_AT,
            )
        )
        await session.commit()

    async with sessions() as stale_session:
        stale = await stale_session.get(WorkflowRunModel, "wr_stale_projection")
        assert stale is not None
        assert stale.webhook_delivery_status is None
        await repository.update_workflow_run(
            "wr_stale_projection", webhook_delivery_status=DELIVERED, webhook_delivery_finalized_at=FINALIZED_AT
        )
        # Bound repository sessions may already hold a row read before the other writer committed.
        monkeypatch.setattr(repository, "Session", lambda: nullcontext(stale_session))
        update = repository.update_workflow_webhook_delivery if fenced else repository.update_workflow_run
        fence = (
            {
                "expected_status": WorkflowRunStatus.completed,
                "expected_finished_at": (FINALIZED_AT + timedelta(hours=8)).replace(
                    tzinfo=timezone(timedelta(hours=8))
                ),
            }
            if fenced
            else {}
        )
        updated = await update(
            "wr_stale_projection",
            webhook_delivery_status=EXHAUSTED,
            webhook_delivery_finalized_at=FINALIZED_AT + timedelta(seconds=1),
            **fence,
        )
        if fenced:
            assert updated is True

    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_stale_projection")
        assert row is not None
        assert row.webhook_delivery_status == DELIVERED
        assert row.webhook_delivery_finalized_at == FINALIZED_AT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target_url,status_code,run_status,run_type,projects",
    [
        (None, 200, "completed", "workflow_run", True),
        (None, 204, "failed", "workflow_run", True),
        ("", 299, "completed", "workflow_run", True),
        ("https://example.com/override", 200, "completed", "workflow_run", False),
        (DEFAULT_URL, 200, "completed", "workflow_run", False),
        (None, 302, "completed", "workflow_run", False),
        (None, 503, "completed", "workflow_run", False),
        (None, None, "completed", "workflow_run", False),
        (None, 200, "running", "workflow_run", False),
        (None, 200, "completed", "task_v1", False),
        (None, 200, "completed", "task_v2", False),
    ],
)
async def test_replay_projects_only_successful_default_final_workflow_delivery(
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    target_url: str | None,
    status_code: int | None,
    run_status: str,
    run_type: str,
    projects: bool,
) -> None:
    sessions = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    repository = WorkflowRunsRepository(sessions)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    monkeypatch.setattr(replay_service.app.DATABASE, "workflow_runs", repository)
    monkeypatch.setattr(replay_service.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_replay",
                workflow_id="w_replay",
                workflow_permanent_id="wpid_replay",
                organization_id="o_replay",
                status=run_status,
                finished_at=FINALIZED_AT,
                webhook_callback_url=DEFAULT_URL,
                webhook_delivery_status=EXHAUSTED,
                webhook_delivery_finalized_at=FINALIZED_AT,
                webhook_failure_reason="original failure",
            )
        )
        await session.commit()
    if run_type != "workflow_run":
        monkeypatch.setattr(repository, "get_workflow_run", AsyncMock(return_value=None))
    monkeypatch.setattr(
        replay_service,
        "_build_webhook_payload",
        AsyncMock(
            return_value=replay_service._WebhookPayload("wr_replay", run_type, {"status": run_status}, DEFAULT_URL)
        ),
    )
    monkeypatch.setattr(
        replay_service,
        "_validate_target_url",
        AsyncMock(return_value=(target_url or DEFAULT_URL, ("93.184.216.34",))),
    )
    error = "timeout" if status_code is None else None
    monkeypatch.setattr(replay_service, "_deliver_webhook", AsyncMock(return_value=(status_code, 1, "body", error)))

    before_replay = datetime.now(UTC).replace(tzinfo=None)
    with capture_logs() as logs:
        response = await replay_service.replay_run_webhook("o_replay", "wr_replay", target_url, api_key="test-key")

    # A replay logs a final outcome exactly when it records one, so override targets and failures stay out.
    assert [(event["delivery_outcome"], event["replay"]) for event in _finalized_events(logs)] == (
        [("delivered", True)] if projects else []
    )

    assert response.status_code == status_code
    assert response.error == error
    assert response.target_webhook_url == (target_url or DEFAULT_URL)
    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_replay")
        assert row is not None
        assert row.webhook_delivery_status == (DELIVERED if projects else EXHAUSTED)
        if projects:
            assert row.webhook_delivery_finalized_at >= before_replay
        else:
            assert row.webhook_delivery_finalized_at == FINALIZED_AT
        assert row.webhook_failure_reason == "original failure"


@pytest.mark.asyncio
async def test_replay_projection_write_failure_preserves_http_result(monkeypatch: pytest.MonkeyPatch) -> None:
    run = WorkflowRun.model_construct(
        workflow_run_id="wr_replay", status=WorkflowRunStatus.completed, finished_at=FINALIZED_AT
    )
    monkeypatch.setattr(replay_service.app.DATABASE.workflow_runs, "get_workflow_run", AsyncMock(return_value=run))
    monkeypatch.setattr(replay_service.app.DATABASE.workflow_run_attempts, "get_attempts", AsyncMock(return_value=[]))
    update = AsyncMock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(replay_service.app.DATABASE.workflow_runs, "update_workflow_webhook_delivery", update)
    monkeypatch.setattr(
        replay_service,
        "_build_webhook_payload",
        AsyncMock(return_value=replay_service._WebhookPayload("wr_replay", "workflow_run", {}, DEFAULT_URL)),
    )
    monkeypatch.setattr(
        replay_service, "_validate_target_url", AsyncMock(return_value=(DEFAULT_URL, ("93.184.216.34",)))
    )
    monkeypatch.setattr(replay_service, "_deliver_webhook", AsyncMock(return_value=(200, 1, "ok", None)))

    response = await replay_service.replay_run_webhook("o_replay", "wr_replay", None, api_key="test-key")

    update.assert_awaited_once()
    assert response.status_code == 200
    assert response.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_path", ["replay", "automatic", "fallback"])
@pytest.mark.parametrize("lifecycle_change", ["matching", "reset", "refinished", "status_changed", "missing_finished"])
@pytest.mark.parametrize("status_code", [200, 400])
async def test_webhook_projection_is_fenced_to_execution_during_http(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    sqlite_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    delivery_path: str,
    lifecycle_change: str,
    status_code: int,
) -> None:
    svc, _build_response, _update_run = webhook_service
    sessions = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    runs = WorkflowRunsRepository(sessions)
    attempts = WorkflowRunAttemptsRepository(sessions)
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_runs", runs)
    monkeypatch.setattr(service_module.app.DATABASE, "workflow_run_attempts", attempts)
    monkeypatch.setattr(repository_module, "save_workflow_run_logs", AsyncMock())
    monkeypatch.setattr(svc, "_workflow_webhook_fallback_is_current", AsyncMock(return_value=True))
    async with sessions() as session:
        session.add(
            WorkflowRunModel(
                workflow_run_id="wr_fenced",
                workflow_id="w_fenced",
                workflow_permanent_id="wpid_fenced",
                organization_id="o_fenced",
                status="completed",
                finished_at=None if lifecycle_change == "missing_finished" else FINALIZED_AT,
                webhook_callback_url=DEFAULT_URL,
                webhook_failure_reason="",
            )
        )
        if delivery_path == "fallback":
            session.add(
                WorkflowRunAttemptModel(
                    workflow_run_id="wr_fenced",
                    organization_id="o_fenced",
                    attempt_number=1,
                    status="completed",
                    retry_decision="final",
                    final_side_effects_progress={
                        "webhook_delivery_attempts": WORKFLOW_WEBHOOK_DELIVERY_MAX_ATTEMPTS - 1
                    },
                )
            )
        await session.commit()

    async def during_http(**_kwargs: Any) -> httpx.Response | tuple[int, int, str, None]:
        if lifecycle_change in {"reset", "refinished", "status_changed"}:
            await runs.update_workflow_run(
                "wr_fenced",
                status=WorkflowRunStatus.created
                if lifecycle_change == "reset"
                else WorkflowRunStatus.failed
                if lifecycle_change == "status_changed"
                else WorkflowRunStatus.completed,
                finished_at=None
                if lifecycle_change == "reset"
                else FINALIZED_AT
                if lifecycle_change == "status_changed"
                else FINALIZED_AT + timedelta(microseconds=1),
                webhook_delivery_status=None,
                webhook_failure_reason="",
            )
        if delivery_path == "replay":
            return status_code, 1, "response", None
        return _response(status_code, "response")

    deliver = AsyncMock(side_effect=during_http)
    if delivery_path == "replay":
        monkeypatch.setattr(
            replay_service,
            "_build_webhook_payload",
            AsyncMock(return_value=replay_service._WebhookPayload("wr_fenced", "workflow_run", {}, DEFAULT_URL)),
        )
        monkeypatch.setattr(
            replay_service, "_validate_target_url", AsyncMock(return_value=(DEFAULT_URL, ("93.184.216.34",)))
        )
        monkeypatch.setattr(replay_service, "_deliver_webhook", deliver)
        response = await replay_service.replay_run_webhook("o_fenced", "wr_fenced", None, api_key="test-key")
        assert response.status_code == status_code
        assert response.error is None
    else:
        monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)
        run = await runs.get_workflow_run("wr_fenced")
        assert run is not None
        await svc.execute_workflow_webhook(run, claim_kind="final" if delivery_path == "fallback" else None)

    deliver.assert_awaited_once()
    expected = None
    if lifecycle_change == "matching":
        expected = DELIVERED if status_code == 200 else EXHAUSTED if delivery_path != "replay" else None
    async with sessions() as session:
        row = await session.get(WorkflowRunModel, "wr_fenced")
        assert row is not None
        assert row.webhook_delivery_status == expected
        assert (row.webhook_delivery_finalized_at is not None) is (expected is not None)
        if lifecycle_change != "matching" or delivery_path == "replay":
            assert row.webhook_failure_reason == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [WorkflowRunStatus.created, WorkflowRunStatus.running])
@pytest.mark.parametrize("kind", [None, "final", "interim"])
async def test_final_webhook_skips_nonterminal_execution(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    status: WorkflowRunStatus,
    kind: Literal["final", "interim"] | None,
) -> None:
    svc, _build_response, update_run = webhook_service
    run = _workflow_run()
    run.status = status
    run.finished_at = None
    deliver = AsyncMock(return_value=_response(200))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(run, claim_kind=kind)

    if kind == "interim":
        deliver.assert_awaited_once()
        assert update_run.await_args.kwargs["webhook_failure_reason"] == ""
    else:
        deliver.assert_not_awaited()
        update_run.assert_not_awaited()
    assert _delivery_projection_call(update_run) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [None, "final", "interim"])
async def test_final_webhook_skips_reset_during_prepare(
    webhook_service: tuple[WorkflowService, AsyncMock, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
    kind: Literal["final", "interim"] | None,
) -> None:
    svc, build_response, update_run = webhook_service
    build_response.return_value.status = WorkflowRunStatus.created
    build_response.return_value.finished_at = None
    deliver = AsyncMock(return_value=_response(200))
    monkeypatch.setattr(service_module, "deliver_webhook_with_retries", deliver)

    await svc.execute_workflow_webhook(_workflow_run(), claim_kind=kind)

    if kind == "interim":
        deliver.assert_awaited_once()
    else:
        deliver.assert_not_awaited()
        update_run.assert_not_awaited()
    assert _delivery_projection_call(update_run) is None
