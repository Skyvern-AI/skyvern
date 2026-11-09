from __future__ import annotations

import asyncio
import json
import threading
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from skyvern.forge.sdk.services.request_principal import RequestPrincipal, human_user_id

DEFAULT_MAX_ROWS = 1_000
MAX_PROVIDER_ROWS = 100_000
MAX_PROVIDER_VALUES = 1_000_000
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
AUDIT_WRITE_TIMEOUT_SECONDS = 5.0
# shortcut: 16 outstanding Power BI audit writes per event loop, reassess if workflow concurrency exceeds it.
MAX_OUTSTANDING_AUDIT_WRITES = 16
POWERBI_QUERY_BLOCK_TIMEOUT_SECONDS = 60.0


class PowerBIQueryError(Exception):
    """Safe error code for Power BI reads; never carries provider bodies or query data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Power BI query failed ({code})")


@dataclass(frozen=True)
class PowerBIExecutionAudit:
    organization_id: str
    workflow_run_id: str
    workflow_run_block_id: str
    connection_id: str
    outcome: Literal["started", "succeeded", "failed", "cancelled"]
    denial_reason: str | None = None


SAFE_AUDIT_FAILURE_REASONS = frozenset(
    {
        "execution_context_invalid",
        "literal_connection_required",
        "integration_unavailable",
        "principal_unavailable",
        "membership_unconfirmed",
        "trigger_parent_owner_mismatch",
        "trigger_parent_source_unconfirmed",
        "unsafe_query_input_source",
        "event_trigger_run",
        "copilot_test_run_unsupported",
        "workflow_owner_resave_required",
        "connection_unavailable",
        "invalid_credential_source",
        "credential_required",
        "dataset_required",
        "query_required",
        "invalid_max_rows",
        "invalid_response",
        "provider_response_error",
        "row_limit_exceeded",
        "response_too_large",
        "provider_unavailable",
        "provider_unauthorized",
        "provider_http_error",
    }
)
AuditWriter = Callable[[RequestPrincipal | None, PowerBIExecutionAudit], Awaitable[None]]


@dataclass
class _AuditWriteState:
    slots: asyncio.Semaphore
    pending: set[asyncio.Future[None]]


_AUDIT_WRITE_STATES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _AuditWriteState] = (
    weakref.WeakKeyDictionary()
)
_AUDIT_WRITE_STATES_LOCK = threading.Lock()


def _audit_write_state() -> _AuditWriteState:
    loop = asyncio.get_running_loop()
    with _AUDIT_WRITE_STATES_LOCK:
        state = _AUDIT_WRITE_STATES.get(loop)
        if state is None:
            state = _AuditWriteState(asyncio.Semaphore(MAX_OUTSTANDING_AUDIT_WRITES), set())
            _AUDIT_WRITE_STATES[loop] = state
        return state


def _audit_write_finished(state: _AuditWriteState, task: asyncio.Future[None]) -> None:
    state.pending.discard(task)
    state.slots.release()
    if not task.cancelled():
        task.exception()


async def write_powerbi_audit(
    audit_writer: AuditWriter,
    principal: RequestPrincipal | None,
    event: PowerBIExecutionAudit,
) -> None:
    """Bound strict audit latency and retain/cap ownership of writes that ignore cancellation."""
    state = _audit_write_state()
    deadline = asyncio.get_running_loop().time() + AUDIT_WRITE_TIMEOUT_SECONDS
    try:
        await asyncio.wait_for(state.slots.acquire(), timeout=AUDIT_WRITE_TIMEOUT_SECONDS)
    except TimeoutError:
        raise PowerBIQueryError("audit_unavailable") from None

    try:
        write = asyncio.ensure_future(audit_writer(principal, event))
    except Exception:  # noqa: BLE001 - audit setup failure must not escape with storage details.
        state.slots.release()
        raise PowerBIQueryError("audit_unavailable") from None
    state.pending.add(write)
    write.add_done_callback(lambda task: _audit_write_finished(state, task))

    try:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.wait_for(asyncio.shield(write), timeout=remaining)
    except TimeoutError:
        write.cancel()
        raise PowerBIQueryError("audit_unavailable") from None
    except asyncio.CancelledError:
        write.cancel()
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        raise PowerBIQueryError("audit_unavailable") from None
    except Exception:  # noqa: BLE001 - audit failures must not expose storage details.
        raise PowerBIQueryError("audit_unavailable") from None


def _audit_event(
    *,
    organization_id: str,
    workflow_run_id: str,
    workflow_run_block_id: str,
    connection_id: str,
    outcome: Literal["started", "succeeded", "failed", "cancelled"],
    denial_reason: str | None = None,
) -> PowerBIExecutionAudit:
    if denial_reason not in SAFE_AUDIT_FAILURE_REASONS:
        denial_reason = "provider_http_error" if denial_reason and denial_reason.startswith("provider_http_") else None
    return PowerBIExecutionAudit(
        organization_id=organization_id,
        workflow_run_id=workflow_run_id,
        workflow_run_block_id=workflow_run_block_id,
        connection_id=connection_id,
        outcome=outcome,
        denial_reason=denial_reason,
    )


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _invalid_json_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")


def _parse_rows_payload(payload: object, max_rows: int) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("error") is not None:
        raise PowerBIQueryError("provider_response_error")
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise PowerBIQueryError("invalid_response")
    result = results[0]
    if result.get("error") is not None:
        raise PowerBIQueryError("provider_response_error")
    tables = result.get("tables")
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], dict):
        raise PowerBIQueryError("invalid_response")
    table = tables[0]
    if table.get("error") is not None:
        raise PowerBIQueryError("provider_response_error")
    rows = table.get("rows")
    if not isinstance(rows, list):
        raise PowerBIQueryError("invalid_response")
    if not rows:
        return []

    first_row = rows[0]
    if (
        not isinstance(first_row, dict)
        or not first_row
        or any(not isinstance(name, str) or not name for name in first_row)
    ):
        raise PowerBIQueryError("invalid_response")
    columns = tuple(first_row)
    if len(columns) != len(set(columns)):
        raise PowerBIQueryError("invalid_response")
    for row in rows:
        if not isinstance(row, dict) or row.keys() != first_row.keys():
            raise PowerBIQueryError("invalid_response")
        if any(isinstance(value, (dict, list)) for value in row.values()):
            raise PowerBIQueryError("invalid_response")

    provider_row_limit = min(MAX_PROVIDER_ROWS, MAX_PROVIDER_VALUES // len(columns))
    # Reaching the provider cap can hide a truncated result.
    if len(rows) > max_rows or len(rows) >= provider_row_limit:
        raise PowerBIQueryError("row_limit_exceeded")

    # Preserve provider labels verbatim; normalization could collapse distinct DAX aliases.
    return [{column: row[column] for column in columns} for row in rows]


def _parse_rows(body: bytes, max_rows: int) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, ValueError):
        raise PowerBIQueryError("invalid_response") from None
    return _parse_rows_payload(payload, max_rows)


def parse_powerbi_hop_rows(body: bytes, max_rows: int) -> list[dict[str, Any]]:
    """Validate the canonical rows returned by the audited API hop."""
    try:
        rows = json.loads(body, object_pairs_hook=_json_object, parse_constant=_invalid_json_constant)
    except (UnicodeDecodeError, ValueError):
        raise PowerBIQueryError("invalid_response") from None
    if not isinstance(rows, list):
        raise PowerBIQueryError("invalid_response")
    if not rows:
        return []
    first_row = rows[0]
    if (
        not isinstance(first_row, dict)
        or not first_row
        or any(not isinstance(key, str) or not key for key in first_row)
    ):
        raise PowerBIQueryError("invalid_response")
    columns = tuple(first_row)
    for row in rows:
        if not isinstance(row, dict) or row.keys() != first_row.keys():
            raise PowerBIQueryError("invalid_response")
        if any(not isinstance(value, (str, int, float, bool, type(None))) for value in row.values()):
            raise PowerBIQueryError("invalid_response")
    provider_row_limit = min(MAX_PROVIDER_ROWS, MAX_PROVIDER_VALUES // len(columns))
    if len(rows) > max_rows or len(rows) >= provider_row_limit:
        raise PowerBIQueryError("row_limit_exceeded")
    return rows


def _validate_audit_context(
    organization_id: str,
    workflow_run_id: str,
    workflow_run_block_id: str,
    connection_id: str,
) -> None:
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (organization_id, workflow_run_id, workflow_run_block_id, connection_id)
    ):
        raise PowerBIQueryError("execution_context_required")
    if "{{" in connection_id or "{%" in connection_id:
        raise PowerBIQueryError("literal_connection_required")


def _validate_query_inputs(credential_id: str, dataset_id: str, query: str, max_rows: int) -> None:
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise PowerBIQueryError("credential_required")
    if "{{" in credential_id or "{%" in credential_id:
        raise PowerBIQueryError("literal_connection_required")
    if not isinstance(dataset_id, str) or len(dataset_id) != 36:
        raise PowerBIQueryError("dataset_required")
    try:
        if str(UUID(dataset_id)) != dataset_id.lower():
            raise ValueError
    except ValueError:
        raise PowerBIQueryError("dataset_required")
    if not isinstance(query, str) or not query.strip():
        raise PowerBIQueryError("query_required")
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= MAX_PROVIDER_ROWS:
        raise PowerBIQueryError("invalid_max_rows")


async def execute_audited_powerbi_response(
    *,
    organization_id: str,
    principal: RequestPrincipal | None,
    connection_id: str,
    dataset_id: str,
    query: str,
    workflow_run_id: str,
    workflow_run_block_id: str,
    response_reader: Callable[[], Awaitable[bytes]],
    audit_writer: AuditWriter,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Audit and validate a bounded Power BI response returned by a trusted Cloud transport."""
    _validate_audit_context(organization_id, workflow_run_id, workflow_run_block_id, connection_id)
    await write_powerbi_audit(
        audit_writer,
        principal,
        _audit_event(
            organization_id=organization_id,
            workflow_run_id=workflow_run_id,
            workflow_run_block_id=workflow_run_block_id,
            connection_id=connection_id,
            outcome="started",
        ),
    )

    async def record_terminal(
        outcome: Literal["succeeded", "failed", "cancelled"],
        denial_reason: str | None = None,
    ) -> None:
        await write_powerbi_audit(
            audit_writer,
            principal,
            _audit_event(
                organization_id=organization_id,
                workflow_run_id=workflow_run_id,
                workflow_run_block_id=workflow_run_block_id,
                connection_id=connection_id,
                outcome=outcome,
                denial_reason=denial_reason,
            ),
        )

    try:
        _validate_query_inputs(connection_id, dataset_id, query, max_rows)
        if not human_user_id(principal, organization_id):
            raise PowerBIQueryError("principal_unavailable")
        rows = _parse_rows(await response_reader(), max_rows)
    except asyncio.CancelledError:
        try:
            await record_terminal("cancelled")
        except PowerBIQueryError:
            pass
        raise
    except PowerBIQueryError as exc:
        await record_terminal("failed", exc.code)
        raise
    except Exception:  # noqa: BLE001 - sanitize broker and provider failures.
        await record_terminal("failed")
        raise PowerBIQueryError("provider_unavailable") from None

    await record_terminal("succeeded")
    return rows
