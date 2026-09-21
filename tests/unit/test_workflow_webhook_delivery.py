from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from structlog.testing import capture_logs

from skyvern.forge.sdk.core.security import generate_skyvern_webhook_signature
from skyvern.forge.sdk.db.models import WorkflowModel, WorkflowRunAttemptModel, WorkflowRunModel
from skyvern.forge.sdk.db.repositories.workflow_run_attempts import WorkflowRunAttemptsRepository
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
    RetryDecision,
    _attempt_from_record,
    _get_retry_policy,
    evaluate_retry_policy,
    on_terminal_transition,
)
from skyvern.forge.sdk.workflow.service import WorkflowService
from skyvern.schemas.workflows import WorkflowRetryPolicy
from skyvern.services import webhook_delivery as webhook_delivery_module
from tests.unit.scoped_asyncio import ScopedAsyncio


class _StatusResponse:
    def __init__(self, extra_payload: dict | None = None) -> None:
        now = datetime.now(UTC)
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
