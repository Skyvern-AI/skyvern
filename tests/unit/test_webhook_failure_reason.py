from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from skyvern.exceptions import FailedToSendWebhook
from skyvern.forge import agent as agent_module
from skyvern.forge.agent import ForgeAgent
from skyvern.services import task_v2_service


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


def _make_task_v2() -> MagicMock:
    task_v2 = MagicMock()
    task_v2.observer_cruise_id = "oc_1"
    task_v2.organization_id = "o_1"
    task_v2.webhook_callback_url = "https://example.com/hook"
    task_v2.model_dump_json = lambda **_kw: "{}"
    return task_v2


@pytest.mark.asyncio
async def test_task_v2_webhook_delivery_exception_persists_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        task_v2_service,
        "deliver_webhook_with_retries",
        AsyncMock(side_effect=httpx.ReadTimeout("")),
    )

    with pytest.raises(FailedToSendWebhook):
        await task_v2_service.send_task_v2_webhook(_make_task_v2())

    update_task_v2.assert_awaited_once()
    reason = update_task_v2.await_args.kwargs["webhook_failure_reason"]
    assert "ReadTimeout" in reason
