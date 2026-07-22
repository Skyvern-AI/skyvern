from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from skyvern.cli.core import action_log
from skyvern.cli.core.client import reset_api_key_override, set_api_key_override
from skyvern.cli.core.result import BrowserContext
from skyvern.cli.mcp_tools import browser as mcp_browser
from skyvern.forge import app as forge_app
from skyvern.forge.sdk.artifact.manager import ArtifactManager
from skyvern.forge.sdk.artifact.models import Artifact, ArtifactType
from skyvern.forge.sdk.routes import browser_sessions, routers  # noqa: F401  # register action-log routes
from skyvern.forge.sdk.services import org_auth_service
from skyvern.library.skyvern import Skyvern
from skyvern.schemas.action_log import (
    ACTION_LOG_MAX_BODY_BYTES,
    ACTION_LOG_MAX_EVENTS_PER_BATCH,
    ACTION_LOG_MAX_TOOL_LENGTH,
    ActionLogEvent,
    ActionLogOutcome,
    project_action_event,
)

ORG_ID = "org_test"
SESSION_ID = "pbs_test"


def _principal(
    *responses: httpx.Response | Exception, base_url: str = "https://backend.test"
) -> tuple[Skyvern, AsyncMock]:
    request = AsyncMock(side_effect=responses)
    wrapper = SimpleNamespace(
        httpx_client=SimpleNamespace(request=request),
        get_base_url=lambda: base_url,
    )
    return cast(Skyvern, SimpleNamespace(_client_wrapper=wrapper)), request


def _queue_entry(
    principal: Skyvern,
    *,
    number: int = 1,
    origin: str = "https://backend.test",
) -> action_log.ActionLogQueueEntry:
    event = _event(number)
    return action_log.ActionLogQueueEntry(
        browser_session_id=SESSION_ID,
        event=event,
        principal=principal,
        origin=origin,
        encoded_bytes=len(event.model_dump_json().encode()),
    )


def _event(
    number: int = 1,
    *,
    occurred_at: datetime | None = None,
    typed_text: str | None = None,
    source_url: str | None = None,
) -> ActionLogEvent:
    return project_action_event(
        event_id=UUID(int=number),
        tool="skyvern_type" if typed_text is not None else "skyvern_click",
        selector="#field",
        typed_text=typed_text,
        source_url=source_url,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        timing_ms={"total": 3},
        outcome=ActionLogOutcome.SUCCESS,
        index=number,
    )


def _artifact(number: int, *, created_at: datetime) -> Artifact:
    event_id = UUID(int=number)
    return Artifact(
        artifact_id=f"a_{number}",
        artifact_type=ArtifactType.BROWSER_SESSION_ACTION_LOG,
        uri=f"s3://bucket/browser_sessions/{SESSION_ID}/browser_session_action_log/v1-{event_id}.json",
        organization_id=ORG_ID,
        browser_session_id=SESSION_ID,
        created_at=created_at,
        modified_at=created_at,
    )


def _route_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock, AsyncMock]:
    get_session = AsyncMock(return_value=SimpleNamespace(browser_session_id=SESSION_ID, organization_id=ORG_ID))
    create_artifact = AsyncMock(return_value="a_test")
    monkeypatch.setattr(forge_app.PERSISTENT_SESSIONS_MANAGER, "get_session", get_session)
    monkeypatch.setattr(
        forge_app.ARTIFACT_MANAGER,
        "create_browser_session_action_log_artifact",
        create_artifact,
    )

    app = FastAPI()
    app.include_router(routers.base_router, prefix="/v1")
    app.dependency_overrides[org_auth_service.get_current_org] = lambda: SimpleNamespace(organization_id=ORG_ID)
    return TestClient(app), get_session, create_artifact


def test_action_log_ingest_rejects_invalid_inputs_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_session, create_artifact = _route_client(monkeypatch)
    valid = _event().model_dump(mode="json")
    oversized_field = {**valid, "tool": "x" * (ACTION_LOG_MAX_TOOL_LENGTH + 1)}
    bad_outcome = {**valid, "outcome": "unknown"}
    stale_timestamp = {
        **valid,
        "occurred_at": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),
    }

    responses = [
        client.post(
            f"/v1/browser_sessions/{SESSION_ID}/action_logs",
            json={"events": [valid] * (ACTION_LOG_MAX_EVENTS_PER_BATCH + 1)},
        ),
        client.post(f"/v1/browser_sessions/{SESSION_ID}/action_logs", json={"events": [oversized_field]}),
        client.post(f"/v1/browser_sessions/{SESSION_ID}/action_logs", json={"events": [bad_outcome]}),
        client.post(f"/v1/browser_sessions/{SESSION_ID}/action_logs", json={"events": [stale_timestamp]}),
        client.post(
            f"/v1/browser_sessions/{SESSION_ID}/action_logs",
            content=b"x" * (ACTION_LOG_MAX_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        ),
    ]

    assert [response.status_code for response in responses] == [422, 422, 422, 422, 413]
    get_session.assert_not_awaited()
    create_artifact.assert_not_awaited()


def test_action_log_ingest_rejects_wrong_tenant_before_write(monkeypatch: pytest.MonkeyPatch) -> None:
    client, get_session, create_artifact = _route_client(monkeypatch)
    get_session.return_value = None

    response = client.post(
        f"/v1/browser_sessions/{SESSION_ID}/action_logs",
        json={"events": [_event().model_dump(mode="json")]},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "browser_session_not_found"}
    create_artifact.assert_not_awaited()


def test_action_log_ingest_accepts_duplicate_delivery_but_readback_is_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, create_artifact = _route_client(monkeypatch)
    now = datetime.now(timezone.utc)
    later = _event(1, occurred_at=now)
    earlier = _event(2, occurred_at=now - timedelta(seconds=1))

    response = client.post(
        f"/v1/browser_sessions/{SESSION_ID}/action_logs",
        json={
            "events": [later.model_dump(mode="json"), later.model_dump(mode="json"), earlier.model_dump(mode="json")]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 3}
    assert [call.kwargs["event"].event_id for call in create_artifact.await_args_list] == [
        later.event_id,
        later.event_id,
        earlier.event_id,
    ]

    now = datetime.now(timezone.utc)
    first_artifact = _artifact(1, created_at=now)
    duplicate_artifact = first_artifact.model_copy(
        update={
            "artifact_id": "a_duplicate",
            "created_at": now + timedelta(milliseconds=1),
            "modified_at": now + timedelta(milliseconds=1),
        }
    )
    artifacts = [first_artifact, duplicate_artifact, _artifact(2, created_at=now + timedelta(milliseconds=2))]
    monkeypatch.setattr(
        forge_app.DATABASE.artifacts,
        "list_artifacts_for_browser_session_by_type_after",
        AsyncMock(return_value=artifacts),
    )
    monkeypatch.setattr(
        forge_app.ARTIFACT_MANAGER,
        "retrieve_artifact",
        AsyncMock(
            side_effect=[
                later.model_dump_json().encode(),
                later.model_dump_json().encode(),
                earlier.model_dump_json().encode(),
            ]
        ),
    )

    readback = client.get(f"/v1/browser_sessions/{SESSION_ID}/action_logs?page_size=3")

    assert readback.status_code == 200, readback.text
    assert [event["event_id"] for event in readback.json()["events"]] == [
        str(earlier.event_id),
        str(later.event_id),
    ]


def test_action_log_read_cursor_advances_without_replaying_prior_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _route_client(monkeypatch)
    now = datetime.now(timezone.utc)
    artifacts = [_artifact(number, created_at=now + timedelta(milliseconds=number)) for number in (1, 2, 3)]
    events = [
        _event(1, occurred_at=now + timedelta(seconds=2)),
        _event(2, occurred_at=now),
        _event(3, occurred_at=now + timedelta(seconds=1)),
    ]
    list_artifacts = AsyncMock(side_effect=[artifacts[:2], [artifacts[2]]])
    retrieve_artifact = AsyncMock(side_effect=[event.model_dump_json().encode() for event in events])
    monkeypatch.setattr(
        forge_app.DATABASE.artifacts,
        "list_artifacts_for_browser_session_by_type_after",
        list_artifacts,
    )
    monkeypatch.setattr(forge_app.ARTIFACT_MANAGER, "retrieve_artifact", retrieve_artifact)

    first = client.get(f"/v1/browser_sessions/{SESSION_ID}/action_logs?page_size=2")
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert [event["event_id"] for event in first_body["events"]] == [str(events[1].event_id), str(events[0].event_id)]
    assert isinstance(first_body["next_cursor"], str)

    second = client.get(
        f"/v1/browser_sessions/{SESSION_ID}/action_logs",
        params={"page_size": 2, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    assert [event["event_id"] for event in second.json()["events"]] == [str(events[2].event_id)]

    assert list_artifacts.await_args_list[0].kwargs == {
        "browser_session_id": SESSION_ID,
        "organization_id": ORG_ID,
        "artifact_type": ArtifactType.BROWSER_SESSION_ACTION_LOG,
        "created_after": None,
        "artifact_id_after": None,
        "limit": 2,
    }
    expected_created_after = artifacts[1].created_at.astimezone(timezone.utc).replace(tzinfo=None)
    assert list_artifacts.await_args_list[1].kwargs["created_after"] == expected_created_after
    assert list_artifacts.await_args_list[1].kwargs["artifact_id_after"] == artifacts[1].artifact_id


@pytest.mark.asyncio
async def test_action_log_artifacts_skip_dedup_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    event = _event()
    uri = f"s3://bucket/browser_sessions/{SESSION_ID}/browser_session_action_log/v1-{event.event_id}.json"
    find_existing = AsyncMock(return_value=SimpleNamespace(artifact_id="a_existing"))
    create_row = AsyncMock()
    monkeypatch.setattr(forge_app.STORAGE, "sync_browser_session_file", AsyncMock(return_value=uri))
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "find_artifact_for_browser_session", find_existing)
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "create_artifact", create_row)
    monkeypatch.setattr("skyvern.forge.sdk.artifact.manager.generate_artifact_id", lambda: "a_new")

    artifact_id = await ArtifactManager().create_browser_session_action_log_artifact(
        organization_id=ORG_ID,
        browser_session_id=SESSION_ID,
        event=event,
    )

    assert artifact_id == "a_new"
    find_existing.assert_not_awaited()
    create_row.assert_awaited_once()


@pytest.mark.asyncio
async def test_action_log_privacy_holds_at_every_durable_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typed_secret = "sk-test-secret-value"
    url_secret = "query-secret-value"
    path_secret = "person@example.test:opaque-value"
    event = _event(
        typed_text=typed_secret,
        source_url=(
            f"https://example.test/products/verify/{path_secret}/confirm"
            f"?token={url_secret}&q={typed_secret}&plain=allowed"
        ),
    )
    persisted_bodies: list[bytes] = []
    uri = f"s3://bucket/browser_sessions/{SESSION_ID}/browser_session_action_log/v1-{event.event_id}.json"

    async def sync_file(**kwargs: object) -> str:
        persisted_bodies.append(Path(str(kwargs["local_file_path"])).read_bytes())
        return uri

    find_existing = AsyncMock(return_value=None)
    create_row = AsyncMock()
    monkeypatch.setattr(forge_app.STORAGE, "sync_browser_session_file", sync_file)
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "find_artifact_for_browser_session", find_existing)
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "create_artifact", create_row)
    monkeypatch.setattr(
        "skyvern.forge.sdk.artifact.manager.generate_artifact_id",
        Mock(side_effect=["a_first", "a_duplicate"]),
    )

    manager = ArtifactManager()
    with capture_logs() as logs:
        first = await manager.create_browser_session_action_log_artifact(
            organization_id=ORG_ID,
            browser_session_id=SESSION_ID,
            event=event,
        )
        duplicate = await manager.create_browser_session_action_log_artifact(
            organization_id=ORG_ID,
            browser_session_id=SESSION_ID,
            event=event,
        )

    assert create_row.await_args is not None
    durable = "\n".join(
        [
            event.model_dump_json(),
            *(body.decode() for body in persisted_bodies),
            uri,
            json.dumps([call.kwargs for call in create_row.await_args_list], default=str),
            json.dumps(logs, default=str),
        ]
    )
    assert typed_secret not in durable
    assert url_secret not in durable
    assert path_secret not in durable
    assert "/products/verify/__redacted__/confirm" in durable
    assert "__redacted__=__redacted__" in durable
    assert "q=__redacted__" in durable
    assert (first, duplicate) == ("a_first", "a_duplicate")
    find_existing.assert_not_awaited()
    assert create_row.await_count == 2


@pytest.mark.asyncio
async def test_non_action_log_browser_session_artifacts_still_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uri = f"s3://bucket/browser_sessions/{SESSION_ID}/downloads/report.pdf"
    find_existing = AsyncMock(return_value=SimpleNamespace(artifact_id="a_existing"))
    create_row = AsyncMock()
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "find_artifact_for_browser_session", find_existing)
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "create_artifact", create_row)

    artifact_id = await ArtifactManager().create_browser_session_download_artifact(
        organization_id=ORG_ID,
        browser_session_id=SESSION_ID,
        uri=uri,
        filename="report.pdf",
    )

    assert artifact_id == "a_existing"
    find_existing.assert_awaited_once_with(
        organization_id=ORG_ID,
        browser_session_id=SESSION_ID,
        uri=uri,
        artifact_type=ArtifactType.DOWNLOAD,
    )
    create_row.assert_not_awaited()


def test_action_log_ingest_reprojects_hostile_strings_before_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, create_artifact = _route_client(monkeypatch)
    secrets = {
        "selector": "sk-test-selector-secret",
        "value": "sk-test-value-secret",
        "source_url_scheme": "sk-qrstuvwxyzabcdef",
        "artifact_ref": "sk-test-artifact-secret",
        "tool": "skyvern_not_instrumented",
        "error_code": "SECRET",
        "timing_key": "sk-abcdefghijklmnop",
    }
    raw_event = _event().model_dump(mode="json") | {
        "selector": f'input[value="{secrets["selector"]}"]',
        "value": secrets["value"],
        "source_url": f"{secrets['source_url_scheme']}://example.test/path",
        "artifact_ref": secrets["artifact_ref"],
    }
    persisted_bodies: list[bytes] = []
    persisted_uris: list[str] = []

    async def sync_file(**kwargs: object) -> str:
        persisted_bodies.append(Path(str(kwargs["local_file_path"])).read_bytes())
        uri = f"s3://bucket/{kwargs['remote_path']}"
        persisted_uris.append(uri)
        return uri

    create_row = AsyncMock()
    monkeypatch.setattr(forge_app.STORAGE, "sync_browser_session_file", sync_file)
    monkeypatch.setattr(
        forge_app.DATABASE.artifacts,
        "find_artifact_for_browser_session",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(forge_app.DATABASE.artifacts, "create_artifact", create_row)
    create_artifact.side_effect = ArtifactManager().create_browser_session_action_log_artifact

    invalid_events = (
        raw_event | {"tool": secrets["tool"]},
        raw_event | {"outcome": "error", "error_code": secrets["error_code"]},
        raw_event | {"timing_ms": {secrets["timing_key"]: 1}},
    )
    with capture_logs() as logs:
        rejected = [
            client.post(
                f"/v1/browser_sessions/{SESSION_ID}/action_logs",
                json={"events": [event]},
            )
            for event in invalid_events
        ]
        response = client.post(
            f"/v1/browser_sessions/{SESSION_ID}/action_logs",
            json={"events": [raw_event]},
        )

    assert [item.status_code for item in rejected] == [422, 422, 422]
    assert response.status_code == 200, response.text
    durable = "\n".join(
        [
            *(item.text for item in rejected),
            response.text,
            *(body.decode() for body in persisted_bodies),
            *persisted_uris,
            json.dumps(create_artifact.await_args.kwargs, default=str),
            json.dumps(create_row.await_args.kwargs, default=str),
            json.dumps(logs, default=str),
        ]
    )
    assert all(secret not in durable for secret in secrets.values())
    persisted_event = json.loads(persisted_bodies[0])
    assert persisted_event["selector"] == 'input[value="__redacted__"]'
    assert persisted_event["value"] == "__redacted__"
    assert persisted_event["source_url"] == "__redacted__"
    assert persisted_event["artifact_ref"] == "__redacted__"


def test_action_log_client_drops_invalid_tool_error_code_and_timing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    principal, _ = _principal(httpx.Response(202))
    enqueue = Mock()
    capture_drop = Mock()
    monkeypatch.setattr(action_log.action_log_worker, "enqueue", enqueue)
    monkeypatch.setattr(action_log.action_log_worker, "capture_drop", capture_drop)
    monkeypatch.setattr(action_log, "get_active_api_key", lambda: "principal")
    monkeypatch.setattr(action_log, "get_skyvern", lambda: principal)
    monkeypatch.setattr(action_log, "is_stateless_http_mode", lambda: False)

    with capture_logs() as logs:
        action_log.enqueue_action_event(
            BrowserContext(mode="cloud_session", session_id=SESSION_ID),
            tool="skyvern_not_instrumented",
            timing_ms={"total": 1},
            ok=True,
        )
        action_log.enqueue_action_event(
            BrowserContext(mode="cloud_session", session_id=SESSION_ID),
            tool="skyvern_click",
            timing_ms={"total": 1, "sk-abcdefghijklmnop": 2},
            ok=True,
        )
        action_log.enqueue_action_event(
            BrowserContext(mode="cloud_session", session_id=SESSION_ID),
            tool="skyvern_click",
            timing_ms={"total": 1},
            ok=False,
            error_code="SECRET",
        )

    assert [call.args for call in capture_drop.call_args_list] == [
        ("projection_error",),
        ("projection_error",),
    ]
    enqueue.assert_called_once()
    assert enqueue.call_args.args[0].event.timing_ms == {"total": 1}
    assert all(secret not in json.dumps(logs, default=str) for secret in ("skyvern_not_instrumented", "SECRET"))


def test_replay_trajectory_entry_remains_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    append = Mock()
    monkeypatch.setattr(mcp_browser, "append_trajectory_entry", append)
    monkeypatch.setattr(mcp_browser, "current_api_key_hash", lambda: "principal_hash")

    mcp_browser._record_trajectory_entry(
        SimpleNamespace(mode="cloud_session", session_id=SESSION_ID),
        tool_name="type_text",
        selector="#search",
        typed_text="widget",
        source_url="https://example.test/catalog?session=secret&q=widgets",
    )

    append.assert_called_once_with(
        api_key_hash="principal_hash",
        session_id=SESSION_ID,
        entry={
            "tool_name": "type_text",
            "selector": "#search",
            "source_url": "https://example.test/catalog?session=__redacted__&q=widgets",
            "typed_length": 6,
            "typed_value": "widget",
        },
    )


def test_terminal_result_observer_is_failure_isolated_and_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_secret = "sk-test-terminal-secret"
    context = BrowserContext(mode="cloud_session", session_id=SESSION_ID)
    result_kwargs = {
        "ok": False,
        "browser_context": context,
        "timing_ms": {"total": 4},
        "error": {"code": "ACTION_FAILED", "message": "safe"},
    }
    expected = mcp_browser.make_result("skyvern_type", **result_kwargs)
    monkeypatch.setattr(
        mcp_browser,
        "enqueue_action_event",
        Mock(side_effect=RuntimeError(raw_secret)),
    )

    with capture_logs() as logs:
        actual = mcp_browser._action_result_factory(
            ctx=context,
            page=SimpleNamespace(url=f"https://example.test/path?token={raw_secret}"),
            selector="#field",
            typed_text=raw_secret,
        )("skyvern_type", **result_kwargs)

    assert actual == expected
    assert raw_secret not in json.dumps(logs, default=str)


@pytest.mark.asyncio
async def test_two_principal_hosted_queue_drains_with_captured_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_a, http_a = _principal(httpx.Response(202))
    principal_b, http_b = _principal(httpx.Response(202))
    principals = {"principal-a": principal_a, "principal-b": principal_b}
    worker = action_log.ActionLogWorker()
    monkeypatch.setattr(action_log, "action_log_worker", worker)
    monkeypatch.setattr(action_log, "is_stateless_http_mode", lambda: True)
    monkeypatch.setattr(action_log, "_resolve_self_base_url", lambda: "http://127.0.0.1:8000")
    monkeypatch.setattr(
        action_log,
        "get_skyvern",
        lambda: principals[cast(str, action_log.get_active_api_key())],
    )
    start_worker = worker._ensure_task
    monkeypatch.setattr(worker, "_ensure_task", lambda: None)

    for principal_name in principals:
        token = set_api_key_override(principal_name)
        try:
            action_log.enqueue_action_event(
                BrowserContext(mode="cloud_session", session_id=SESSION_ID),
                tool="skyvern_type",
                selector="#field",
                typed_text="sk-test-secret-value" if principal_name == "principal-a" else "visible",
                source_url="https://example.test/path?token=query-secret-value",
                timing_ms={"total": 1},
                ok=True,
            )
        finally:
            reset_api_key_override(token)

    assert worker._queue.qsize() == 2

    def fail_ambient() -> NoReturn:
        raise AssertionError("worker read ambient request state while draining")

    monkeypatch.setattr(action_log, "get_active_api_key", fail_ambient)
    monkeypatch.setattr(action_log, "get_skyvern", fail_ambient)
    monkeypatch.setattr(worker, "_ensure_task", start_worker)
    worker._ensure_task()
    await worker.drain()
    await worker.shutdown()

    assert http_a.await_count == http_b.await_count == 1
    for call in [*http_a.await_args_list, *http_b.await_args_list]:
        path, kwargs = call.args[0], call.kwargs
        assert path == f"v1/browser_sessions/{SESSION_ID}/action_logs"
        assert kwargs["base_url"] == "http://127.0.0.1:8000"
        assert kwargs["retries"] == 0
        assert kwargs["request_options"] == {
            "timeout_in_seconds": action_log.ACTION_LOG_HTTP_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
        serialized = json.dumps(kwargs["json"], default=str)
        assert "sk-test-secret-value" not in serialized
        assert "query-secret-value" not in serialized


@pytest.mark.asyncio
async def test_action_log_worker_queue_saturation_is_a_counted_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(action_log, "ACTION_LOG_QUEUE_MAX_ENTRIES", 1)
    worker = action_log.ActionLogWorker()
    principal, _ = _principal(httpx.Response(202))
    monkeypatch.setattr(worker, "_ensure_task", lambda: None)

    worker.enqueue(replace(_queue_entry(principal, number=0), encoded_bytes=ACTION_LOG_MAX_BODY_BYTES + 1))
    worker.enqueue(_queue_entry(principal, number=1))
    worker.enqueue(_queue_entry(principal, number=2))

    assert worker._queue.qsize() == 1
    assert worker.drop_count == 2


@pytest.mark.asyncio
async def test_action_log_worker_timeout_is_passive_and_shutdown_is_clean() -> None:
    principal, http_client = _principal(httpx.ReadTimeout("timed out"))
    worker = action_log.ActionLogWorker()

    worker.enqueue(_queue_entry(principal))
    await asyncio.wait_for(worker.drain(), timeout=1)
    await worker.shutdown()

    assert http_client.await_count == 1
    assert worker.drop_count == 1
    assert worker._task is None
    assert worker._queue.empty()


@pytest.mark.asyncio
async def test_action_log_worker_uses_bounded_client_batches() -> None:
    client_cap = action_log.ACTION_LOG_CLIENT_MAX_EVENTS_PER_BATCH
    principal, http_client = _principal(*[httpx.Response(202) for _ in range(2)])
    worker = action_log.ActionLogWorker()
    start_worker = worker._ensure_task
    worker._ensure_task = lambda: None  # type: ignore[method-assign]
    for number in range(1, client_cap + 2):
        worker.enqueue(_queue_entry(principal, number=number))
    worker._ensure_task = start_worker  # type: ignore[method-assign]

    worker._ensure_task()
    await worker.drain()
    await worker.shutdown()

    assert [len(call.kwargs["json"]["events"]) for call in http_client.await_args_list] == [client_cap, 1]
    assert client_cap == 10 < ACTION_LOG_MAX_EVENTS_PER_BATCH


def test_action_log_indexes_are_session_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    principal, _ = _principal(httpx.Response(202))
    enqueue = Mock()
    monkeypatch.setattr(action_log.action_log_worker, "enqueue", enqueue)
    monkeypatch.setattr(action_log, "get_active_api_key", lambda: "principal")
    monkeypatch.setattr(action_log, "get_skyvern", lambda: principal)
    monkeypatch.setattr(action_log, "is_stateless_http_mode", lambda: False)
    action_log._event_indexes.clear()

    for session_id in ("pbs_a", "pbs_b", "pbs_a"):
        action_log.enqueue_action_event(
            BrowserContext(mode="cloud_session", session_id=session_id),
            tool="skyvern_click",
            timing_ms={"total": 1},
            ok=True,
        )

    assert [call.args[0].event.index for call in enqueue.call_args_list] == [0, 0, 1]

    monkeypatch.setattr(action_log, "ACTION_LOG_SESSION_INDEX_CAP", 2)
    action_log._event_indexes.clear()
    for session_id in ("pbs_a", "pbs_b", "pbs_c"):
        action_log._next_event_index(session_id)
    assert list(action_log._event_indexes) == ["pbs_b", "pbs_c"]


@pytest.mark.asyncio
async def test_action_log_worker_distinguishes_route_and_session_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_origin = "https://old-backend.test"
    route_principal, _ = _principal(httpx.Response(404, json={"detail": "Not Found"}))
    route_worker = action_log.ActionLogWorker()
    route_entry = _queue_entry(route_principal, origin=route_origin)
    await route_worker._send_batch([route_entry])
    monkeypatch.setattr(route_worker, "_ensure_task", lambda: None)
    route_worker.enqueue(route_entry)

    resource_origin = "https://current-backend.test"
    resource_principal, _ = _principal(
        httpx.Response(404, json={"detail": {"code": "browser_session_not_found"}}),
    )
    resource_worker = action_log.ActionLogWorker()
    resource_entry = _queue_entry(resource_principal, origin=resource_origin)
    await resource_worker._send_batch([resource_entry])
    monkeypatch.setattr(resource_worker, "_ensure_task", lambda: None)
    resource_worker.enqueue(resource_entry)

    assert route_origin in route_worker._unsupported_origins  # nosemgrep: incomplete-url-substring-sanitization
    assert route_worker._queue.empty()
    assert resource_origin not in resource_worker._unsupported_origins
    assert resource_worker._queue.qsize() == 1
