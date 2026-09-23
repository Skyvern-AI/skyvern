from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import FailedToSendWebhook
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.services import task_v2_service
from skyvern.services import webhook_delivery as webhook_delivery_module
from tests.unit.scoped_asyncio import ScopedAsyncio


def _make_task() -> MagicMock:
    task = MagicMock()
    task.task_id = "tsk_1"
    task.organization_id = "o_1"
    task.webhook_callback_url = "https://example.com/hook"
    return task


@pytest.fixture
def task_webhook_agent(monkeypatch: pytest.MonkeyPatch) -> tuple[ForgeAgent, AsyncMock]:
    agent = ForgeAgent()
    update_task = AsyncMock()

    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "get_latest_step", AsyncMock(return_value=None))
    monkeypatch.setattr(agent_module.app.DATABASE.tasks, "update_task", update_task)
    monkeypatch.setattr(
        agent,
        "build_task_response",
        AsyncMock(return_value=SimpleNamespace(model_dump_json=lambda **_kw: "{}")),
    )
    monkeypatch.setattr(agent_module.run_service, "get_run_response", AsyncMock(return_value=None))
    monkeypatch.setattr(
        agent_module,
        "generate_skyvern_webhook_signature",
        lambda payload, api_key: SimpleNamespace(
            headers={"x-skyvern-signature": "sig"},
            signed_payload='{"signed":true}',
        ),
    )
    return agent, update_task


@pytest.mark.asyncio
async def test_task_webhook_delivery_exception_persists_failure_reason(
    task_webhook_agent: tuple[ForgeAgent, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, update_task = task_webhook_agent
    monkeypatch.setattr(
        agent_module,
        "deliver_webhook_with_retries",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    with pytest.raises(FailedToSendWebhook):
        await agent.execute_task_webhook(task=_make_task(), api_key="api-key")

    update_task.assert_awaited_once()
    reason = update_task.await_args.kwargs["webhook_failure_reason"]
    assert "ReadTimeout" in reason


@pytest.mark.asyncio
async def test_task_webhook_still_raises_when_failure_reason_recording_fails(
    task_webhook_agent: tuple[ForgeAgent, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, update_task = task_webhook_agent
    update_task.side_effect = RuntimeError("db pool exhausted")
    monkeypatch.setattr(
        agent_module,
        "deliver_webhook_with_retries",
        AsyncMock(side_effect=httpx.ConnectError("unreachable")),
    )

    with pytest.raises(FailedToSendWebhook):
        await agent.execute_task_webhook(task=_make_task(), api_key="api-key")


@pytest.mark.asyncio
async def test_task_webhook_non_delivery_exception_does_not_persist_failure_reason(
    task_webhook_agent: tuple[ForgeAgent, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, update_task = task_webhook_agent
    deliver = AsyncMock()
    monkeypatch.setattr(agent_module, "deliver_webhook_with_retries", deliver)

    def _broken_signature(payload: object, api_key: str) -> None:
        raise TypeError("payload not serializable")

    monkeypatch.setattr(agent_module, "generate_skyvern_webhook_signature", _broken_signature)

    with pytest.raises(FailedToSendWebhook):
        await agent.execute_task_webhook(task=_make_task(), api_key="api-key")

    deliver.assert_not_awaited()
    update_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_webhook_exception_log_exposes_canonical_fields(
    task_webhook_agent: tuple[ForgeAgent, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, update_task = task_webhook_agent
    error = httpx.HTTPStatusError(
        "proxy hop rejected the webhook",
        request=httpx.Request("POST", "https://proxy.example/webhook"),
        response=httpx.Response(status_code=502, content=b"bad gateway"),
    )
    monkeypatch.setattr(agent_module, "deliver_webhook_with_retries", AsyncMock(side_effect=error))

    with capture_logs() as logs:
        with pytest.raises(FailedToSendWebhook):
            await agent.execute_task_webhook(task=_make_task(), api_key="api-key")

    failures = [event for event in logs if event["event"] == "Task webhook delivery failed after attempting delivery"]
    assert len(failures) == 1
    # Canonical status_code derives from the exception response (NAT-proxy hop failure).
    assert failures[0]["status_code"] == 502
    # Persisted reason wording is preserved regardless of the proxy-hop status.
    assert failures[0]["error_reason"] == webhook_delivery_module.format_http_log_reason(502)
    assert "bad gateway" not in failures[0]["error_reason"]
    assert update_task.await_args.kwargs["webhook_failure_reason"].startswith(
        "Webhook delivery failed before receiving a response:"
    )


def _make_task_v2() -> MagicMock:
    task_v2 = MagicMock()
    task_v2.observer_cruise_id = "oc_1"
    task_v2.organization_id = "o_1"
    task_v2.webhook_callback_url = "https://example.com/hook"
    task_v2.model_dump_json = lambda **_kw: "{}"
    return task_v2


@pytest.fixture
def task_v2_webhook_update(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncMock:
    update_task_v2 = AsyncMock()
    monkeypatch.setattr(
        task_v2_service.app.DATABASE.organizations,
        "get_valid_org_auth_token",
        AsyncMock(return_value=SimpleNamespace(token="api-key")),
    )
    monkeypatch.setattr(task_v2_service.app.DATABASE.observer, "update_task_v2", update_task_v2)
    monkeypatch.setattr(
        task_v2_service,
        "build_task_v2_run_response",
        AsyncMock(return_value=SimpleNamespace(model_dump_json=lambda **_kw: "{}")),
    )
    monkeypatch.setattr(
        task_v2_service,
        "generate_skyvern_webhook_signature",
        lambda payload, api_key: SimpleNamespace(
            headers={"x-skyvern-signature": "sig"},
            signed_payload='{"signed":true}',
        ),
    )
    return update_task_v2


@pytest.mark.asyncio
async def test_task_v2_webhook_delivery_exception_persists_failure_reason(
    task_v2_webhook_update: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_v2_service,
        "deliver_webhook_with_retries",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    with pytest.raises(FailedToSendWebhook):
        await task_v2_service.send_task_v2_webhook(_make_task_v2())

    task_v2_webhook_update.assert_awaited_once()
    reason = task_v2_webhook_update.await_args.kwargs["webhook_failure_reason"]
    assert "ReadTimeout" in reason


@pytest.mark.asyncio
async def test_task_v2_webhook_exception_log_exposes_canonical_fields(
    task_v2_webhook_update: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = httpx.HTTPStatusError(
        "proxy hop rejected the webhook",
        request=httpx.Request("POST", "https://proxy.example/webhook"),
        response=httpx.Response(status_code=502, content=b"bad gateway"),
    )
    monkeypatch.setattr(task_v2_service, "deliver_webhook_with_retries", AsyncMock(side_effect=error))

    with capture_logs() as logs:
        with pytest.raises(FailedToSendWebhook):
            await task_v2_service.send_task_v2_webhook(_make_task_v2())

    failures = [
        event for event in logs if event["event"] == "Task v2 webhook delivery failed after attempting delivery"
    ]
    assert len(failures) == 1
    assert failures[0]["status_code"] == 502
    assert failures[0]["error_reason"] == webhook_delivery_module.format_http_log_reason(502)
    assert "bad gateway" not in failures[0]["error_reason"]
    assert task_v2_webhook_update.await_args.kwargs["webhook_failure_reason"].startswith(
        "Webhook delivery failed before receiving a response:"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("task_version", ["v1", "v2"])
async def test_task_http_failure_logs_do_not_copy_response_body(
    task_version: str,
    task_webhook_agent: tuple[ForgeAgent, AsyncMock],
    task_v2_webhook_update: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, update_task = task_webhook_agent
    body = "synthetic-endpoint-secret"
    monkeypatch.setattr(
        webhook_delivery_module.app.AGENT_FUNCTION,
        "deliver_webhook",
        AsyncMock(return_value=httpx.Response(503, text=body)),
    )
    monkeypatch.setattr(webhook_delivery_module, "asyncio", ScopedAsyncio(sleep=AsyncMock()))

    with capture_logs() as logs:
        if task_version == "v1":
            await agent.execute_task_webhook(task=_make_task(), api_key="api-key", enable_retries=True)
        else:
            await task_v2_service.send_task_v2_webhook(_make_task_v2())

    update = update_task if task_version == "v1" else task_v2_webhook_update
    failures = [event for event in logs if event["event"] in ("Webhook failed", "Task v2 webhook failed")]
    retries = [event for event in logs if event["event"] == "Retrying webhook delivery after transient failure"]
    assert len(failures) == 1
    assert failures[0]["resp_code"] == 503
    assert failures[0]["resp_text"] == body
    assert failures[0]["status_code"] == 503
    assert update.await_args.kwargs["webhook_failure_reason"] == (
        f"Webhook failed with status code 503, error message: {body}"
    )
    assert update.await_args.kwargs["webhook_failure_reason"] == webhook_delivery_module.format_http_failure_reason(
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
