from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest

from skyvern.forge.sdk.services import powerbi_query_service as powerbi
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal
from tests.unit.scoped_asyncio import ScopedAsyncio

ORG = "org_test"
RUN = "wr_test"
BLOCK = "wfrb_test"
CONNECTION = "moc_test"
QUERY = "EVALUATE VALUES('Sales')"
TOKEN = "access-token-canary"
DATASET_ID = "cfafbeb1-8037-4d0c-896e-a46fb27ff229"
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _rows(rows: list[dict[str, Any]]) -> dict[str, object]:
    return {"results": [{"tables": [{"rows": rows}]}]}


def _request_context() -> dict[str, str]:
    return {
        "organization_id": ORG,
        "credential_id": "moc_test",
        "connection_id": CONNECTION,
        "dataset_id": DATASET_ID,
        "query": QUERY,
        "workflow_run_id": RUN,
        "workflow_run_block_id": BLOCK,
    }


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: _record_request(requests, request, handler))

    def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(powerbi.httpx, "AsyncClient", fake_client)
    return requests


async def _record_request(requests: list[httpx.Request], request: httpx.Request, handler: Any) -> httpx.Response:
    requests.append(request)
    response = handler(request)
    return await response if hasattr(response, "__await__") else response


def _install_hook(
    monkeypatch: pytest.MonkeyPatch, token_values: list[str | None] | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    tokens = iter(token_values or [TOKEN])

    async def get_credentials(*args: Any, **kwargs: Any) -> str | None:
        calls.append({"args": args, **kwargs})
        return next(tokens, None)

    monkeypatch.setattr(powerbi.app, "AGENT_FUNCTION", SimpleNamespace(get_microsoft_credentials=get_credentials))
    return calls


@pytest.fixture
def principal() -> RequestPrincipal:
    return RequestPrincipal(ORG, AuthKind.bearer, "user_test")


@pytest.fixture
def audit_events() -> list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]]:
    return []


@pytest.fixture
def audit_writer(audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]]) -> Any:
    async def writer(principal: RequestPrincipal | None, event: powerbi.PowerBIExecutionAudit) -> None:
        audit_events.append((principal, event))

    return writer


async def _execute(
    *,
    principal: RequestPrincipal | None,
    audit_writer: Any,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    return await powerbi.execute_audited_powerbi_query(
        **(_request_context() | kwargs), principal=principal, audit_writer=audit_writer
    )


@pytest.mark.asyncio
async def test_success_is_fixed_endpoint_audited_and_preserves_distinct_column_labels(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
) -> None:
    hooks = _install_hook(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_rows([{"[Name]": "A", "Name": "B"}]))

    requests = _install_transport(monkeypatch, handler)
    rows = await _execute(principal=principal, audit_writer=audit_writer)

    assert rows == [{"[Name]": "A", "Name": "B"}]
    assert str(requests[0].url) == f"https://api.powerbi.com/v1.0/myorg/datasets/{DATASET_ID}/executeQueries"
    assert json.loads(requests[0].content) == {
        "queries": [{"query": QUERY}],
        "serializerSettings": {"includeNulls": True},
    }
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert [
        (event.outcome, event.organization_id, event.workflow_run_id, event.workflow_run_block_id, event.connection_id)
        for _, event in audit_events
    ] == [
        ("started", ORG, RUN, BLOCK, CONNECTION),
        ("succeeded", ORG, RUN, BLOCK, CONNECTION),
    ]
    assert all(actor == principal for actor, _ in audit_events)
    assert hooks[0]["required_scopes"] == ["Dataset.Read.All"]
    assert hooks[0]["scope_profile"] == "powerbi_dataset_read"
    assert hooks[0]["principal"] is principal
    assert hooks[0]["force_refresh"] is False
    assert TOKEN not in repr(audit_events)
    assert QUERY not in repr(audit_events)
    assert "Sales" not in repr(audit_events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": "provider body secret"}},
        {"results": [{"error": {"message": "provider body secret"}, "tables": []}]},
        {"results": [{"tables": [{"error": {"message": "provider body secret"}, "rows": []}]}]},
    ],
)
async def test_http_200_provider_errors_at_each_level_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
    payload: dict[str, Any],
) -> None:
    _install_hook(monkeypatch)
    requests = _install_transport(monkeypatch, lambda _: httpx.Response(200, json=payload))

    with pytest.raises(powerbi.PowerBIQueryError, match="provider_response_error") as error:
        await _execute(principal=principal, audit_writer=audit_writer)

    assert "provider body secret" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"results":[]}',
        b'{"results":[{"tables":[]}]}',
        b'{"results":[{"tables":[{"rows":[{"a":1,"a":2}]}]}]}',
        b'{"results":[{"tables":[{"rows":[{"a":{"nested":true}}]}]}]}',
    ],
)
async def test_malformed_or_ambiguous_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
    body: bytes,
) -> None:
    _install_hook(monkeypatch)
    _install_transport(monkeypatch, lambda _: httpx.Response(200, content=body))

    with pytest.raises(powerbi.PowerBIQueryError, match="invalid_response"):
        await _execute(principal=principal, audit_writer=audit_writer)


@pytest.mark.asyncio
async def test_row_and_response_byte_limits_fail_instead_of_returning_a_subset(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
) -> None:
    _install_hook(monkeypatch)
    _install_transport(monkeypatch, lambda _: httpx.Response(200, json=_rows([{"a": 1}, {"a": 2}])))
    with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
        await _execute(
            principal=principal,
            audit_writer=audit_writer,
            max_rows=2,
        )

    monkeypatch.setattr(powerbi, "MAX_PROVIDER_VALUES", 2)
    _install_hook(monkeypatch)
    _install_transport(monkeypatch, lambda _: httpx.Response(200, json=_rows([{"a": 1}, {"a": 2}])))
    with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
        await _execute(principal=principal, audit_writer=audit_writer, max_rows=10)

    _install_hook(monkeypatch)
    monkeypatch.setattr(powerbi, "MAX_RESPONSE_BYTES", 8)
    _install_transport(monkeypatch, lambda _: httpx.Response(200, content=b"", headers={"Content-Length": "9"}))
    with pytest.raises(powerbi.PowerBIQueryError, match="response_too_large"):
        await _execute(principal=principal, audit_writer=audit_writer)


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset_id", [".", "..", "not-a-guid", "00000000-0000-0000-0000-../../../../"])
async def test_invalid_dataset_id_fails_before_token_or_provider_access(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
    dataset_id: str,
) -> None:
    hooks = _install_hook(monkeypatch)
    requests = _install_transport(monkeypatch, lambda _: httpx.Response(200, json=_rows([])))

    with pytest.raises(powerbi.PowerBIQueryError, match="dataset_required"):
        await _execute(principal=principal, audit_writer=audit_writer, dataset_id=dataset_id)

    assert hooks == [] and requests == []
    assert audit_events[-1][1].denial_reason == "dataset_required"


@pytest.mark.asyncio
async def test_redirect_is_not_followed(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
) -> None:
    _install_hook(monkeypatch)
    requests = _install_transport(
        monkeypatch,
        lambda _: httpx.Response(302, headers={"Location": "https://attacker.invalid/collect"}),
    )

    with pytest.raises(powerbi.PowerBIQueryError, match="provider_http_302"):
        await _execute(principal=principal, audit_writer=audit_writer)
    assert audit_events[-1][1].denial_reason == "provider_http_error"
    assert len(requests) == 1
    parsed_url = urlparse(str(requests[0].url))
    assert parsed_url.scheme == "https" and parsed_url.hostname == "api.powerbi.com"


@pytest.mark.asyncio
async def test_transient_retry_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
) -> None:
    _install_hook(monkeypatch)
    original_sleep = asyncio.sleep

    async def no_sleep(_: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(powerbi, "asyncio", ScopedAsyncio(sleep=no_sleep))
    requests = _install_transport(monkeypatch, lambda _: httpx.Response(503))

    with pytest.raises(powerbi.PowerBIQueryError, match="provider_unavailable"):
        await _execute(principal=principal, audit_writer=audit_writer)
    assert len(requests) == 1 + powerbi.MAX_TRANSIENT_RETRIES


@pytest.mark.asyncio
async def test_401_refreshes_once_and_retries_with_the_refreshed_token(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
) -> None:
    hooks = _install_hook(monkeypatch, ["old-token", "new-token"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer old-token":
            return httpx.Response(401)
        return httpx.Response(200, json=_rows([{"ok": True}]))

    requests = _install_transport(monkeypatch, handler)
    assert await _execute(principal=principal, audit_writer=audit_writer) == [{"ok": True}]
    assert [call["force_refresh"] for call in hooks] == [False, True]
    assert len(requests) == 2
    assert requests[-1].headers["authorization"] == "Bearer new-token"


@pytest.mark.asyncio
async def test_a_second_401_fails_without_another_refresh(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_writer: Any,
) -> None:
    hooks = _install_hook(monkeypatch, ["old-token", "new-token"])
    requests = _install_transport(monkeypatch, lambda _: httpx.Response(401))

    with pytest.raises(powerbi.PowerBIQueryError, match="provider_unauthorized"):
        await _execute(principal=principal, audit_writer=audit_writer)

    assert [call["force_refresh"] for call in hooks] == [False, True]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_pre_audit_failure_and_missing_principal_prevent_token_and_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
) -> None:
    hooks = _install_hook(monkeypatch)

    async def broken_audit(*_: Any) -> None:
        raise RuntimeError("sensitive audit failure")

    monkeypatch.setattr(powerbi.httpx, "AsyncClient", lambda *_args, **_kwargs: pytest.fail("HTTP must not start"))
    with pytest.raises(powerbi.PowerBIQueryError, match="audit_unavailable"):
        await _execute(principal=principal, audit_writer=broken_audit)
    assert hooks == []

    events: list[powerbi.PowerBIExecutionAudit] = []

    async def writer(_principal: RequestPrincipal | None, event: powerbi.PowerBIExecutionAudit) -> None:
        events.append(event)

    with pytest.raises(powerbi.PowerBIQueryError, match="principal_unavailable"):
        await _execute(principal=None, audit_writer=writer)
    assert hooks == []
    assert [event.outcome for event in events] == ["started", "failed"]
    assert events[-1].denial_reason == "principal_unavailable"


@pytest.mark.asyncio
async def test_hung_started_audits_time_out_without_calls_and_cap_pending_writes(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
) -> None:
    hooks = _install_hook(monkeypatch)
    monkeypatch.setattr(powerbi, "AUDIT_WRITE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(powerbi, "MAX_OUTSTANDING_AUDIT_WRITES", 1)
    monkeypatch.setattr(powerbi.httpx, "AsyncClient", lambda *_args, **_kwargs: pytest.fail("HTTP must not start"))
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    writer_calls = 0

    async def hung_writer(_principal: RequestPrincipal | None, _event: powerbi.PowerBIExecutionAudit) -> None:
        nonlocal writer_calls
        writer_calls += 1
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    outcomes = await asyncio.gather(
        _execute(principal=principal, audit_writer=hung_writer),
        _execute(principal=principal, audit_writer=hung_writer),
        return_exceptions=True,
    )

    assert all(
        isinstance(outcome, powerbi.PowerBIQueryError) and outcome.code == "audit_unavailable" for outcome in outcomes
    )
    assert entered.is_set() and writer_calls == 1
    assert hooks == []
    state = powerbi._AUDIT_WRITE_STATES[asyncio.get_running_loop()]
    assert len(state.pending) == 1
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.wait_for(_wait_for_no_pending(state), timeout=0.1)
    assert not state.pending


@pytest.mark.asyncio
async def test_cancelling_a_hung_pre_audit_is_prompt_and_keeps_bounded_ownership(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
) -> None:
    hooks = _install_hook(monkeypatch)
    monkeypatch.setattr(powerbi, "AUDIT_WRITE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(powerbi, "MAX_OUTSTANDING_AUDIT_WRITES", 1)
    monkeypatch.setattr(powerbi.httpx, "AsyncClient", lambda *_args, **_kwargs: pytest.fail("HTTP must not start"))
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def hung_writer(_principal: RequestPrincipal | None, _event: powerbi.PowerBIExecutionAudit) -> None:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    task = asyncio.create_task(_execute(principal=principal, audit_writer=hung_writer))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)

    state = powerbi._AUDIT_WRITE_STATES[asyncio.get_running_loop()]
    assert hooks == [] and len(state.pending) == 1
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.wait_for(_wait_for_no_pending(state), timeout=0.1)
    assert not state.pending


async def _wait_for_no_pending(state: Any) -> None:
    while state.pending:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_terminal_audit_failure_withholds_rows_without_repeating_query(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
) -> None:
    _install_hook(monkeypatch)
    requests = _install_transport(monkeypatch, lambda _: httpx.Response(200, json=_rows([{"secret": "row"}])))

    async def writer(_principal: RequestPrincipal | None, event: powerbi.PowerBIExecutionAudit) -> None:
        if event.outcome == "succeeded":
            raise RuntimeError("audit store details")

    with pytest.raises(powerbi.PowerBIQueryError, match="audit_unavailable"):
        await _execute(principal=principal, audit_writer=writer)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cancellation_records_terminal_outcome_without_returning_data(
    monkeypatch: pytest.MonkeyPatch,
    principal: RequestPrincipal,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
    audit_writer: Any,
) -> None:
    _install_hook(monkeypatch)
    entered = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json=_rows([{"never": "returned"}]))

    _install_transport(monkeypatch, handler)
    task = asyncio.create_task(_execute(principal=principal, audit_writer=audit_writer))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event.outcome for _, event in audit_events] == ["started", "cancelled"]
