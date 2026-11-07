from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.services import powerbi_query_service as powerbi
from skyvern.forge.sdk.services.request_principal import AuthKind, RequestPrincipal

ORG = "org_test"
RUN = "wr_test"
BLOCK = "wfrb_test"
CONNECTION = "moc_test"
QUERY = "EVALUATE VALUES('Sales')"
DATASET_ID = "cfafbeb1-8037-4d0c-896e-a46fb27ff229"


def _rows(rows: list[dict[str, Any]]) -> dict[str, object]:
    return {"results": [{"tables": [{"rows": rows}]}]}


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
    response_reader: Callable[[], Awaitable[bytes]] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    async def read_response() -> bytes:
        return json.dumps(_rows([{"ok": True}])).encode()

    context = {
        "organization_id": ORG,
        "connection_id": CONNECTION,
        "dataset_id": DATASET_ID,
        "query": QUERY,
        "workflow_run_id": RUN,
        "workflow_run_block_id": BLOCK,
    }
    return await powerbi.execute_audited_powerbi_response(
        **(context | kwargs),
        principal=principal,
        audit_writer=audit_writer,
        response_reader=response_reader or read_response,
    )


@pytest.mark.asyncio
async def test_response_is_audited_before_dispatch_and_preserves_column_labels(
    principal: RequestPrincipal,
    audit_writer: Any,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
) -> None:
    async def read_response() -> bytes:
        assert [event.outcome for _, event in audit_events] == ["started"]
        return json.dumps(_rows([{"[Name]": "A", "Name": "B"}])).encode()

    rows = await _execute(principal=principal, audit_writer=audit_writer, response_reader=read_response)

    assert rows == [{"[Name]": "A", "Name": "B"}]
    assert [
        (event.outcome, event.organization_id, event.workflow_run_id, event.workflow_run_block_id, event.connection_id)
        for _, event in audit_events
    ] == [
        ("started", ORG, RUN, BLOCK, CONNECTION),
        ("succeeded", ORG, RUN, BLOCK, CONNECTION),
    ]
    assert all(actor == principal for actor, _ in audit_events)
    assert QUERY not in repr(audit_events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"message": "provider body secret"}},
        {"results": [{"error": {"message": "provider body secret"}, "tables": []}]},
        {"results": [{"tables": [{"error": {"message": "provider body secret"}, "rows": []}]}]},
    ],
)
async def test_provider_errors_at_each_level_are_redacted(
    principal: RequestPrincipal, audit_writer: Any, payload: dict[str, Any]
) -> None:
    reader = AsyncMock(return_value=json.dumps(payload).encode())
    with pytest.raises(powerbi.PowerBIQueryError, match="provider_response_error") as error:
        await _execute(principal=principal, audit_writer=audit_writer, response_reader=reader)
    assert "provider body secret" not in str(error.value)


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
    principal: RequestPrincipal, audit_writer: Any, body: bytes
) -> None:
    with pytest.raises(powerbi.PowerBIQueryError, match="invalid_response"):
        await _execute(principal=principal, audit_writer=audit_writer, response_reader=AsyncMock(return_value=body))


@pytest.mark.asyncio
async def test_row_limits_fail_instead_of_returning_a_subset(
    monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal, audit_writer: Any
) -> None:
    rows = [{"a": 1}, {"a": 2}]
    reader = AsyncMock(return_value=json.dumps(_rows(rows)).encode())
    hop_body = json.dumps(rows).encode()

    assert await _execute(principal=principal, audit_writer=audit_writer, response_reader=reader, max_rows=2) == rows
    assert powerbi.parse_powerbi_hop_rows(hop_body, 2) == rows

    with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
        await _execute(principal=principal, audit_writer=audit_writer, response_reader=reader, max_rows=1)
    with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
        powerbi.parse_powerbi_hop_rows(hop_body, 1)

    for cap in ("MAX_PROVIDER_ROWS", "MAX_PROVIDER_VALUES"):
        with monkeypatch.context() as patch:
            patch.setattr(powerbi, cap, 2)
            with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
                await _execute(principal=principal, audit_writer=audit_writer, response_reader=reader, max_rows=2)
            with pytest.raises(powerbi.PowerBIQueryError, match="row_limit_exceeded"):
                powerbi.parse_powerbi_hop_rows(hop_body, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset_id", [".", "..", "not-a-guid", "00000000-0000-0000-0000-../../../../"])
async def test_invalid_dataset_id_fails_before_response_access(
    principal: RequestPrincipal,
    audit_writer: Any,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
    dataset_id: str,
) -> None:
    reader = AsyncMock()
    with pytest.raises(powerbi.PowerBIQueryError, match="dataset_required"):
        await _execute(principal=principal, audit_writer=audit_writer, response_reader=reader, dataset_id=dataset_id)
    reader.assert_not_awaited()
    assert audit_events[-1][1].denial_reason == "dataset_required"


@pytest.mark.asyncio
async def test_pre_audit_failure_and_missing_principal_prevent_response_access(principal: RequestPrincipal) -> None:
    reader = AsyncMock()

    async def broken_audit(*_: Any) -> None:
        raise RuntimeError("sensitive audit failure")

    with pytest.raises(powerbi.PowerBIQueryError, match="audit_unavailable"):
        await _execute(principal=principal, audit_writer=broken_audit, response_reader=reader)
    reader.assert_not_awaited()

    events: list[powerbi.PowerBIExecutionAudit] = []

    async def writer(_principal: RequestPrincipal | None, event: powerbi.PowerBIExecutionAudit) -> None:
        events.append(event)

    with pytest.raises(powerbi.PowerBIQueryError, match="principal_unavailable"):
        await _execute(principal=None, audit_writer=writer, response_reader=reader)
    reader.assert_not_awaited()
    assert [event.outcome for event in events] == ["started", "failed"]
    assert events[-1].denial_reason == "principal_unavailable"


@pytest.mark.asyncio
async def test_hung_started_audits_time_out_without_calls_and_cap_pending_writes(
    monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal
) -> None:
    reader = AsyncMock()
    monkeypatch.setattr(powerbi, "AUDIT_WRITE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(powerbi, "MAX_OUTSTANDING_AUDIT_WRITES", 1)
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
        _execute(principal=principal, audit_writer=hung_writer, response_reader=reader),
        _execute(principal=principal, audit_writer=hung_writer, response_reader=reader),
        return_exceptions=True,
    )
    assert all(
        isinstance(outcome, powerbi.PowerBIQueryError) and outcome.code == "audit_unavailable" for outcome in outcomes
    )
    assert entered.is_set() and writer_calls == 1
    reader.assert_not_awaited()
    state = powerbi._AUDIT_WRITE_STATES[asyncio.get_running_loop()]
    assert len(state.pending) == 1
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.wait_for(_wait_for_no_pending(state), timeout=0.1)
    assert not state.pending


@pytest.mark.asyncio
async def test_cancelling_a_hung_pre_audit_is_prompt_and_keeps_bounded_ownership(
    monkeypatch: pytest.MonkeyPatch, principal: RequestPrincipal
) -> None:
    reader = AsyncMock()
    monkeypatch.setattr(powerbi, "AUDIT_WRITE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(powerbi, "MAX_OUTSTANDING_AUDIT_WRITES", 1)
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

    task = asyncio.create_task(_execute(principal=principal, audit_writer=hung_writer, response_reader=reader))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.1)

    state = powerbi._AUDIT_WRITE_STATES[asyncio.get_running_loop()]
    reader.assert_not_awaited()
    assert len(state.pending) == 1
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.wait_for(_wait_for_no_pending(state), timeout=0.1)
    assert not state.pending


async def _wait_for_no_pending(state: Any) -> None:
    while state.pending:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_terminal_audit_failure_withholds_rows_without_repeating_response(principal: RequestPrincipal) -> None:
    reader = AsyncMock(return_value=json.dumps(_rows([{"secret": "row"}])).encode())

    async def writer(_principal: RequestPrincipal | None, event: powerbi.PowerBIExecutionAudit) -> None:
        if event.outcome == "succeeded":
            raise RuntimeError("audit store details")

    with pytest.raises(powerbi.PowerBIQueryError, match="audit_unavailable"):
        await _execute(principal=principal, audit_writer=writer, response_reader=reader)
    reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_records_terminal_outcome_without_returning_data(
    principal: RequestPrincipal,
    audit_events: list[tuple[RequestPrincipal | None, powerbi.PowerBIExecutionAudit]],
    audit_writer: Any,
) -> None:
    entered = asyncio.Event()

    async def read_response() -> bytes:
        entered.set()
        await asyncio.Event().wait()
        return json.dumps(_rows([{"never": "returned"}])).encode()

    task = asyncio.create_task(_execute(principal=principal, audit_writer=audit_writer, response_reader=read_response))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [event.outcome for _, event in audit_events] == ["started", "cancelled"]
