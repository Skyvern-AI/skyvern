"""Unit tests for the Task V3 auth tools (verification-code handling)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

import skyvern.webeye.navigation as navigation_module
from skyvern.exceptions import (
    BlockedHost,
    FailedToGetTOTPVerificationCode,
    InvalidUrl,
    NoTOTPVerificationCodeFound,
    UnresolvableHost,
)
from skyvern.forge.log_redaction import REDACTED
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.forge_log import redact_sensitive_event_fields
from skyvern.forge.sdk.schemas.totp_codes import OTPType, TOTPCode
from skyvern.forge.sdk.services import credentials as credentials_module
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.taskv3 import auth_tools
from skyvern.forge.taskv3 import loop as taskv3_loop
from skyvern.forge.taskv3.tools import NO_ONE_TIME_CODE_SOURCE, OBSERVE_URL_MAX_CHARS
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPValue
from skyvern.utils.secret_redaction import redact_secrets_from_bytes
from tests.unit.scoped_asyncio import ScopedAsyncio

_TASK_CREATED_AT = datetime(2026, 9, 30, 17, 50, 0)


def _task(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "task_id": "tsk_1",
        "url": "https://example.com/login",
        "organization_id": "o_1",
        "workflow_run_id": None,
        "totp_verification_url": None,
        "totp_identifier": None,
        "navigation_payload": None,
        "created_at": _TASK_CREATED_AT,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_auth_tools_bare_task_no_workflow_lookup() -> None:
    # Unmocked: a bare task (workflow_run_id=None) builds without any workflow-run-context lookup --
    # has_credential_totp_candidate short-circuits on the falsy run id and never reaches the getter
    # that raises when a context isn't registered.
    tools, guidance = auth_tools.build_auth_tools(_task())
    assert [t.name for t in tools] == ["get_verification_code"] and "get_verification_code" in guidance


def test_has_credential_totp_candidate_unregistered_context_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-None workflow_run_id with no registered context returns False without raising: the getter
    # raises WorkflowRunContextNotInitialized, so the gate checks has_workflow_run_context first.
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    assert otp_service.has_credential_totp_candidate("wr_unregistered") is False


def test_try_generate_totp_from_credential_unregistered_context_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same dead-guard class as has_credential_totp_candidate, and it fires first inside resolve_otp_value:
    # an unregistered context must yield None, not raise (the raise would escape into the v1/CUA
    # get_verification_code path, whose callers don't catch WorkflowRunContextNotInitialized).
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    assert otp_service.try_generate_totp_from_credential("wr_unregistered") is None


def test_build_auth_tools_present_for_verification_url_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # A totp_verification_url task now runs on v3 (the dispatch gates are gone), so the URL source alone
    # must offer the tool — otherwise the run reaches the 2FA screen with no way to fetch the code.
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "verification code" in guidance.lower()


_OTP_SOURCE_CASES: list[tuple[str, dict[str, Any], bool]] = [
    ("none", {}, False),
    ("unrelated_payload", {"navigation_payload": {"unrelated_field": "value"}}, False),
    ("magic_link_payload", {"navigation_payload": {"verification_link": "https://example.test/x"}}, False),
    ("totp_payload", {"navigation_payload": {"mfa_code": "123456"}}, True),
    ("identifier", {"totp_identifier": "user@example.com"}, True),
    ("verification_url", {"totp_verification_url": "https://totp.example"}, True),
    ("credential", {"workflow_run_id": "wr_cred"}, True),
    ("url_without_org", {"totp_verification_url": "https://totp.example", "organization_id": None}, False),
    (
        "magic_link_payload_plus_url",
        {
            "navigation_payload": {"verification_link": "https://example.test/x"},
            "totp_verification_url": "https://totp.example",
        },
        True,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "overrides", "expected"), _OTP_SOURCE_CASES, ids=[c[0] for c in _OTP_SOURCE_CASES])
async def test_build_auth_tools_offered_iff_resolve_otp_value_has_a_source(
    monkeypatch: pytest.MonkeyPatch, case: str, overrides: dict[str, Any], expected: bool
) -> None:
    # The tool's per-call source check and the resolver must not desync: has_otp_source is True exactly
    # when resolve_otp_value yields a TOTP value or polls for one. Each row exercises both sides against
    # the same task, so adding a source to one and not the other fails here.
    task = _task(**overrides)
    has_credential = case == "credential"
    credential_value = OTPValue(value="424242", type=OTPType.TOTP)
    monkeypatch.setattr(
        otp_service, "has_credential_totp_candidate", lambda run_id, *a, **k: has_credential and bool(run_id)
    )
    monkeypatch.setattr(
        otp_service,
        "try_generate_totp_from_credential",
        lambda run_id, *a, **k: credential_value if has_credential and run_id else None,
    )
    poll = AsyncMock(return_value=OTPValue(value="111111", type=OTPType.TOTP))
    monkeypatch.setattr(otp_service, "poll_otp_value", poll)
    monkeypatch.setattr(
        otp_service.app,
        "DATABASE",
        SimpleNamespace(workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=None))),
    )

    tools, _ = auth_tools.build_auth_tools(task)
    resolved = await otp_service.resolve_otp_value(task, expected_otp_type=OTPType.TOTP)
    resolver_has_source = poll.await_count > 0 or (resolved is not None and resolved.get_otp_type() == OTPType.TOTP)

    assert [t.name for t in tools] == ["get_verification_code"]
    assert resolver_has_source is expected
    assert otp_service.has_otp_source(task, expected_otp_type=OTPType.TOTP) is expected


@pytest.mark.asyncio
async def test_get_verification_code_polling_budget_bounds_a_never_answering_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source that never answers must not let the model re-poll for N x the poll window: each call
    # polls for at most the per-call cap and returns "not yet", the cumulative polling is capped at
    # VERIFICATION_CODE_POLLING_TIMEOUT_MINS, and once spent the tool says so without awaiting the
    # resolver again.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=0.1 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.005)
    monkeypatch.setattr(auth_tools, "_MIN_SLICE_SECONDS", 0.0)
    resolver_calls = 0
    caps: list[float] = []

    async def _never_answers(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        nonlocal resolver_calls
        resolver_calls += 1
        caps.append(max_wait_seconds)
        await asyncio.sleep(max_wait_seconds)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _never_answers)
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    handler = tools[0].handler

    with capture_logs() as logs:
        results = [await handler({}) for _ in range(30)]

    not_yet = [r for r in results if "code has arrived yet" in r.content]
    exhausted = [r for r in results if r.content == auth_tools._CODE_BUDGET_SPENT]
    assert all(r.status == "error" for r in results)
    assert len(not_yet) >= 1 and not_yet[0] is results[0]
    assert len(exhausted) >= 2 and exhausted[-1] is results[-1]
    assert results.index(exhausted[0]) == len(not_yet)
    # Exhaustion refuses before touching the resolver.
    assert resolver_calls == len(not_yet) + 1
    # Every refusal narrates and is counted. The record this replaced was gated to one per task, so it
    # reported a source that failed repeatedly exactly as loudly as one that failed once.
    armings = [e for e in logs if e.get("event") == "task_v3 verification source failed"]
    assert len(armings) == len(exhausted)
    assert {e["reason"] for e in armings} == {"budget_exhausted"}
    assert {e["tool"] for e in armings} == {"get_verification_code"}
    assert [e["arming_count"] for e in armings] == list(range(1, len(exhausted) + 1))
    assert all(cap <= 0.005 for cap in caps) and sum(caps) <= 0.1 + 0.005


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "lookup_error_streak",
        "no_code_streak",
        "no_link_streak",
        "page_unavailable",
        "link_rejected",
        "link_refused",
        "link_unvalidatable",
        "link_unreachable",
        "link_unreachable_while_origin_page_keeps_fetching",
        "webhook_failing_streak",
    ],
)
async def test_only_a_terminal_link_answer_refuses_a_completed_verdict(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # A link the source never sent, or one the browser could not be sent to, refuses a completed verdict.
    # The code tool's answers never do: it reports, and the model decides with the page in front of it.
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    if case == "lookup_error_streak":
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(side_effect=RuntimeError("boom")))
        tools, _ = auth_tools.build_auth_tools(task, state=state)
    elif case == "no_code_streak":
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
        tools, _ = auth_tools.build_auth_tools(task, state=state)
    elif case == "webhook_failing_streak":
        failing = FailedToGetTOTPVerificationCode(task_id="tsk_1", reason="HTTP 500")
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(side_effect=failing))
        tools, _ = auth_tools.build_auth_tools(task, state=state)
    else:
        link = OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=link))
        monkeypatch.setattr(auth_tools, "validate_fetch_url", lambda url: url)
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
        if case == "no_link_streak":
            monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
            provider = _provider(_FakePage())
        elif case == "page_unavailable":
            provider = AsyncMock(side_effect=RuntimeError("no page"))
        elif case == "link_rejected":
            provider = _provider(_FakePage(status=410))
        elif case == "link_unreachable":
            provider = _provider(_FakePage(goto_error=RuntimeError("net::ERR_CONNECTION_REFUSED")))
        elif case == "link_unreachable_while_origin_page_keeps_fetching":
            # The origin document's own beacons answer during a hanging navigation; they are not a
            # response from the link.
            page = _FakePage(goto_error=TimeoutError("navigation"))
            page.subresource_on_goto_error = True
            provider = _provider(page)
        else:
            provider = _provider(_FakePage())
            validator_error: Exception = (
                BlockedHost("example.test") if case == "link_refused" else RuntimeError("validator down")
            )

            def _reject(url: str) -> str:
                raise validator_error

            monkeypatch.setattr(auth_tools, "validate_fetch_url", _reject)
        tools, _ = auth_tools.build_auth_tools(task, provider, state=state)
    handlers = {t.name: t.handler for t in tools}
    code_tool = case in {"lookup_error_streak", "no_code_streak", "webhook_failing_streak"}
    handler = handlers["get_verification_code" if code_tool else "open_verification_link"]
    expected = {
        "lookup_error_streak": "returned an error (verification lookup failed: RuntimeError)",
        "no_code_streak": "no verification code has arrived yet",
        "no_link_streak": auth_tools._NO_LINK_AVAILABLE,
        "page_unavailable": auth_tools._PAGE_UNAVAILABLE,
        "link_rejected": "rejected the sign-in link (HTTP 410)",
        "link_refused": auth_tools._LINK_REFUSED,
        "link_unvalidatable": "nothing was signed in",
        "link_unreachable": "nothing was signed in",
        "link_unreachable_while_origin_page_keeps_fetching": "nothing was signed in",
        "webhook_failing_streak": "returned an error (FailedToGetTOTPVerificationCode: HTTP 500)",
    }[case]

    assert await state.block_finish("completed") is None
    result = await handler({})
    assert result.status == "error"
    if case.endswith("_streak"):
        # A single empty answer is a blip, not a verdict on the source; the second in a row is.
        assert await state.block_finish("completed") is None
        result = await handler({})
        assert result.status == "error"
    assert expected in result.content
    expected_refusal = None if code_tool else auth_tools._COMPLETION_BLOCKED
    assert await state.block_finish("completed") == expected_refusal


# Every condition the module can arm with, and the record each must produce. `source_failed` is written
# at exactly one place (`VerificationState.arm`), so what this list has to cover is the set of REASONS,
# which is checked against the enum below rather than asserted here. `link_unreachable` and its
# origin-page-keeps-fetching variant are two routes into one site; `link_unvalidatable` is the
# neighbouring branch, split out because our own validator rejecting the URL is a different fact from
# the site being unreachable. The two "value_not_a_*" rows are defensive branches unreachable while
# `OTPValue.get_otp_type()` returns only TOTP or MAGIC_LINK, so they are driven with a stand-in type
# rather than assumed to work.
_CODE = "get_verification_code"
_LINK_TOOL = "open_verification_link"
# (case, reason the arming must narrate, tool the record must name, records the drive must emit).
_ARMING_CASES: list[tuple[str, str, str, int]] = [
    ("budget_exhausted", "budget_exhausted", _CODE, 1),
    ("budget_exhausted_on_the_link_tool", "budget_exhausted", _LINK_TOOL, 1),
    ("no_code_streak", "no_code_twice", _CODE, 1),
    ("no_link_streak", "no_link_twice", _LINK_TOOL, 1),
    ("webhook_failing_streak", "source_errored_twice", _CODE, 1),
    ("lookup_error_streak", "lookup_failed_twice", _CODE, 1),
    ("magic_link_unsupported", "magic_link_unsupported", _CODE, 1),
    # Only reachable on the call after the uncached site armed, so the drive emits both records.
    ("magic_link_unsupported_cached", "magic_link_unsupported_cached", _CODE, 2),
    ("value_not_a_code", "value_not_a_code", _CODE, 1),
    ("value_not_a_link", "value_not_a_link", _LINK_TOOL, 1),
    ("page_unavailable", "page_unavailable", _LINK_TOOL, 1),
    ("link_refused", "link_refused", _LINK_TOOL, 1),
    ("link_unvalidatable", "link_url_unusable", _LINK_TOOL, 1),
    ("link_unreachable", "link_open_failed", _LINK_TOOL, 1),
    ("link_unreachable_while_origin_page_keeps_fetching", "link_open_failed", _LINK_TOOL, 1),
    ("link_rejected", "link_rejected_by_site", _LINK_TOOL, 1),
]


def _arming_setup(
    monkeypatch: pytest.MonkeyPatch, case: str, state: auth_tools.VerificationState, task: Any
) -> tuple[Any, int]:
    """Wire the module so that calling the returned handler `n` times reaches `case`'s arming site."""
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    not_a_known_type = SimpleNamespace(value="x", get_otp_type=lambda: "neither")
    link = OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)

    if case in {"budget_exhausted", "budget_exhausted_on_the_link_tool"}:
        monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=0.1 / 60))
        monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.005)
        monkeypatch.setattr(auth_tools, "_MIN_SLICE_SECONDS", 0.0)

        async def _never_answers(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
            await asyncio.sleep(max_wait_seconds)
            raise NoTOTPVerificationCodeFound(task_id="tsk_1")

        monkeypatch.setattr(auth_tools, "resolve_otp_value", _never_answers)
        # The budget is shared by both tools, so which one drained it is answerable only from the
        # record's `tool` field. The link tool needs a page provider to be offered at all.
        link_tool = case == "budget_exhausted_on_the_link_tool"
        tools, _ = auth_tools.build_auth_tools(task, _provider(_FakePage()) if link_tool else None, state=state)
        return {t.name: t.handler for t in tools}[_LINK_TOOL if link_tool else _CODE], 30
    if case in {"no_code_streak", "no_link_streak"}:
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
    elif case == "webhook_failing_streak":
        monkeypatch.setattr(
            auth_tools,
            "resolve_otp_value",
            AsyncMock(side_effect=FailedToGetTOTPVerificationCode(task_id="tsk_1", reason="HTTP 500")),
        )
    elif case == "lookup_error_streak":
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(side_effect=RuntimeError("boom")))
    elif case in {"value_not_a_code", "value_not_a_link"}:
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=not_a_known_type))
    else:
        monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=link))
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())

    # A magic link with no link tool on offer is the one case that must NOT be handed a page provider.
    if case in {"magic_link_unsupported", "magic_link_unsupported_cached"}:
        tools, _ = auth_tools.build_auth_tools(task, state=state)
        handler = {t.name: t.handler for t in tools}["get_verification_code"]
        return handler, 2 if case == "magic_link_unsupported_cached" else 1

    if case == "page_unavailable":
        provider: Any = AsyncMock(side_effect=RuntimeError("no page"))
    elif case == "link_rejected":
        provider = _provider(_FakePage(status=410))
    elif case == "link_unreachable":
        provider = _provider(_FakePage(goto_error=RuntimeError("net::ERR_CONNECTION_REFUSED")))
    elif case == "link_unreachable_while_origin_page_keeps_fetching":
        page = _FakePage(goto_error=TimeoutError("navigation"))
        page.subresource_on_goto_error = True
        provider = _provider(page)
    elif case in {"link_refused", "link_unvalidatable"}:
        provider = _provider(_FakePage())
        validator_error: Exception = (
            BlockedHost("example.test") if case == "link_refused" else RuntimeError("validator down")
        )

        def _reject(url: str) -> str:
            raise validator_error

        monkeypatch.setattr(auth_tools, "validate_fetch_url", _reject)
    else:
        provider = _provider(_FakePage())

    tools, _ = auth_tools.build_auth_tools(task, provider, state=state)
    handlers = {t.name: t.handler for t in tools}
    code_tool = case in {"lookup_error_streak", "no_code_streak", "webhook_failing_streak", "value_not_a_code"}
    handler = handlers["get_verification_code" if code_tool else "open_verification_link"]
    return handler, 2 if case.endswith("_streak") else 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "reason", "tool", "records"), _ARMING_CASES, ids=[c[0] for c in _ARMING_CASES])
async def test_every_arming_narrates_the_condition_that_armed_the_latch(
    monkeypatch: pytest.MonkeyPatch, case: str, reason: str, tool: str, records: int
) -> None:
    # V1 narrates this failure with the org and the identifier attached
    # (skyvern/forge/agent.py, "TOTP polling timed out — terminating task"). Every arming must emit
    # exactly one record naming WHICH condition armed it.
    task = _task(
        totp_verification_url="https://totp.example/poll?key=abc",
        totp_identifier="jane.doe@example.test",
        workflow_run_id="wr_1",
    )
    state = auth_tools.VerificationState(task=task)
    handler, calls = _arming_setup(monkeypatch, case, state, task)
    assert state.task is task

    with capture_logs() as logs:
        for _ in range(calls):
            armed = any(entry.get("event") == "task_v3 verification source failed" for entry in logs)
            # The cached-magic-link site is only reachable on a call AFTER the first arming.
            if armed and case != "magic_link_unsupported_cached":
                break
            await handler({})

    armings = [entry for entry in logs if entry.get("event") == "task_v3 verification source failed"]
    assert len(armings) == records, f"{case} emitted {len(armings)} records, expected {records}"
    record = armings[-1]
    assert record["reason"] == reason
    # A `StrEnum` member compares equal to its value, so only the type proves the record carries a
    # plain string — which is what keeps the swallow in `arm` from hiding a caller that passed a
    # non-member, where reading `.value` would raise and lose the record with the latch already set.
    assert type(record["reason"]) is str
    assert record["tool"] == tool
    assert record["log_level"] == "warning"
    assert record["values_delivered"] == 0
    assert state.link_failed is (tool == _LINK_TOOL)
    # The identifying fields V1 carries, so the two engines' failures are triaged the same way. These
    # are the raw kwargs: `capture_logs` swaps out the processor chain, so what production actually
    # renders is pinned by test_the_rendered_record_masks_the_identifier_and_keeps_the_endpoint.
    assert record["task_id"] == "tsk_1"
    assert record["organization_id"] == "o_1"
    assert record["workflow_run_id"] == "wr_1"
    assert record["totp_identifier"] == "jane.doe@example.test"
    # The polling URL is a secret-bearing endpoint: the query must be stripped, as V1 strips it.
    assert record["totp_verification_url"] == "https://totp.example/poll"
    assert record["totp_verification_url_configured"] is True
    assert record["arming_count"] == records


@pytest.mark.asyncio
async def test_every_arming_is_counted_not_collapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A source that keeps returning nothing arms on every call after the first pair, and every one of
    # them reports. The record this replaces was gated to one per task, so it answered "did this task
    # hit the condition" and could not answer "how often" at all — `arming_count` now carries the true
    # running total, which is the whole difference between a signal and a flag.
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    handler = tools[0].handler

    with capture_logs() as logs:
        for _ in range(4):
            await handler({})

    armings = [entry for entry in logs if entry.get("event") == "task_v3 verification source failed"]
    assert [entry["arming_count"] for entry in armings] == [1, 2, 3]
    assert {entry["reason"] for entry in armings} == {"no_code_twice"}


@pytest.mark.asyncio
async def test_an_arming_a_later_delivery_unblocks_is_still_narrated(monkeypatch: pytest.MonkeyPatch) -> None:
    # `values_delivered` on the record is the count at THAT arming, not a verdict on the run: a value
    # arriving afterwards writes no record of its own, and clears the status the run would fail with.
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        auth_tools,
        "resolve_otp_value",
        AsyncMock(side_effect=[None, None, OTPValue(value="123456", type=OTPType.TOTP)]),
    )
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    handler = tools[0].handler

    with capture_logs() as logs:
        await handler({})
        await handler({})
        skyvern_context.set(SkyvernContext(task_id="tsk_1"))
        try:
            delivered = await handler({})
        finally:
            skyvern_context.reset()

    armings = [entry for entry in logs if entry.get("event") == "task_v3 verification source failed"]
    assert [entry["values_delivered"] for entry in armings] == [0]
    assert delivered.status == "ok" and state.last_status is None

    # The other order is the one the field exists to separate. Without this the field is only ever
    # observed as 0, so a regression to a constant would read as green.
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
    with capture_logs() as after_delivery:
        await handler({})
        await handler({})
    late = [entry for entry in after_delivery if entry.get("event") == "task_v3 verification source failed"]
    assert [entry["values_delivered"] for entry in late] == [1]
    assert await state.block_finish("completed") is None


def test_the_latch_cannot_be_armed_without_narrating_a_named_condition() -> None:
    # `link_failed` is derived from the recorded armings rather than settable on its own, the arming
    # list is not an __init__ parameter, and the reason is a closed type, so no site can latch silently.
    state = auth_tools.VerificationState(task=_task(navigation_payload={"ssn": "000-00-0000"}))
    # `block_finish` is handed to the loop as a bound method, so anything formatting that callback
    # renders this repr; the task must not be in it.
    assert "ssn" not in repr(state) and "ssn" not in repr(state.block_finish)
    with pytest.raises(AttributeError):
        state.link_failed = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        auth_tools.VerificationState(task=_task(), _armings=[])  # type: ignore[call-arg]

    with capture_logs() as logs:
        state.arm(auth_tools.VerificationFailure.NO_CODE_TWICE, "get_verification_code")
        assert state.link_failed is False
        state.arm(auth_tools.VerificationFailure.PAGE_UNAVAILABLE, "open_verification_link")
    assert state.link_failed is True
    assert [entry["reason"] for entry in logs if entry.get("event") == "task_v3 verification source failed"] == [
        "no_code_twice",
        "page_unavailable",
    ]


def test_narration_never_costs_the_tool_its_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # A record that could raise would leave the latch set, the failure unnarrated, and the model
    # holding a bare tool_error instead of the tool's status. Behaviour outranks observability.
    def _explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("log pipeline down")

    monkeypatch.setattr(auth_tools.LOG, "warning", _explode)
    state = auth_tools.VerificationState(task=_task())
    state.arm(auth_tools.VerificationFailure.LINK_REFUSED, "open_verification_link")
    assert state.link_failed is True


def test_a_polling_url_the_parser_rejects_costs_the_field_not_the_record() -> None:
    # `Task.totp_verification_url` is an unvalidated string on a hydrated task — the validator sits on
    # the request model, not here — so a malformed URL must not be able to take the record down with
    # it. It is also a plausible root cause of a source that never delivers, so "configured but
    # unparseable" has to be distinguishable from "not configured".
    state = auth_tools.VerificationState(task=_task(totp_verification_url="https://totp.example:99999/poll?key=s"))
    with capture_logs() as logs:
        state.arm(auth_tools.VerificationFailure.BUDGET_EXHAUSTED, "get_verification_code")
    record = next(entry for entry in logs if entry.get("event") == "task_v3 verification source failed")
    assert record["totp_verification_url"] is None
    assert record["totp_verification_url_configured"] is True

    scheme_less = auth_tools.VerificationState(task=_task(totp_verification_url="totp.example/poll?key=abc"))
    with capture_logs() as logs:
        scheme_less.arm(auth_tools.VerificationFailure.BUDGET_EXHAUSTED, "get_verification_code")
    record = next(entry for entry in logs if entry.get("event") == "task_v3 verification source failed")
    # `strip_query_params` answers "" here rather than raising, which would otherwise log as an empty
    # string and read as a URL that is present and blank.
    assert record["totp_verification_url"] is None
    assert record["totp_verification_url_configured"] is True

    unconfigured = auth_tools.VerificationState(task=_task())
    with capture_logs() as logs:
        unconfigured.arm(auth_tools.VerificationFailure.BUDGET_EXHAUSTED, "get_verification_code")
    record = next(entry for entry in logs if entry.get("event") == "task_v3 verification source failed")
    assert record["totp_verification_url"] is None
    assert record["totp_verification_url_configured"] is False

    # The query is what carries the caller's secret, and it is what gets stripped.
    configured = auth_tools.VerificationState(task=_task(totp_verification_url="https://totp.example/poll?key=abc"))
    with capture_logs() as logs:
        configured.arm(auth_tools.VerificationFailure.BUDGET_EXHAUSTED, "get_verification_code")
    record = next(entry for entry in logs if entry.get("event") == "task_v3 verification source failed")
    assert record["totp_verification_url"] == "https://totp.example/poll"


def test_the_rendered_record_masks_the_identifier_and_keeps_the_endpoint() -> None:
    # Every other log assertion here runs under `capture_logs`, which replaces the processor chain, so
    # none of them can see what production emits. `totp_identifier` is in `SENSITIVE_FIELDS` and is
    # masked before rendering — the record is not a new PII surface, and the identifier is not the
    # triage handle it looks like. The query-stripped endpoint is, and it must survive.
    state = auth_tools.VerificationState(
        task=_task(totp_identifier="jane.doe@example.test", totp_verification_url="https://totp.example/poll?key=abc")
    )
    with capture_logs() as logs:
        state.arm(auth_tools.VerificationFailure.BUDGET_EXHAUSTED, "get_verification_code")
    record = next(entry for entry in logs if entry.get("event") == "task_v3 verification source failed")

    rendered = redact_sensitive_event_fields(logging.getLogger(__name__), "warning", dict(record))
    assert rendered["totp_identifier"] == REDACTED
    assert rendered["totp_verification_url"] == "https://totp.example/poll"
    assert rendered["reason"] == "budget_exhausted" and rendered["task_id"] == "tsk_1"


def test_every_condition_the_module_can_arm_with_is_covered_by_a_driven_case() -> None:
    # The reason is a closed type, so the inventory is the enum and not a hand-kept list — which is
    # exactly how the earlier 8-site inventory came to be missing six. Adding an arming site means
    # adding a member here, and this fails until a case drives it.
    assert {reason for _case, reason, _tool, _records in _ARMING_CASES} == set(auth_tools.VerificationFailure)


@pytest.mark.asyncio
async def test_lookup_error_streak_resets_on_a_usable_answer_that_is_not_a_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source that answers usably between two errors is alive; the two errors are separate blips,
    # not a streak, even though neither answer delivered a value.
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "validate_fetch_url", lambda url: url)
    monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
    link = OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)
    monkeypatch.setattr(
        auth_tools, "resolve_otp_value", AsyncMock(side_effect=[RuntimeError("boom"), link, RuntimeError("boom")])
    )
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(
        task,
        _provider(_FakePage(goto_error=TimeoutError("load"), cookie_on_goto_error=True)),
        state=state,
    )
    handlers = {t.name: t.handler for t in tools}

    with capture_logs() as logs:
        assert "lookup failed" in (await handlers["get_verification_code"]({})).content
        assert (await handlers["get_verification_code"]({})).content == auth_tools._MAGIC_LINK_REDIRECT
        assert "failed to open" in (await handlers["open_verification_link"]({})).content
        assert "lookup failed" in (await handlers["get_verification_code"]({})).content
    assert [e for e in logs if e.get("event") == "task_v3 verification source failed"] == []
    assert await state.block_finish("completed") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "goto_error_after_cookie",
        "url_moved_no_cookie",
        "response_seen_same_url",
        "later_hop_refused",
        "cookies_unreadable",
    ],
)
async def test_open_verification_link_failure_after_a_possible_sign_in_keeps_completion_open(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # A goto that raised after a cookie landed (a load timeout, a download-triggering landing, a later
    # hop refused) may already have signed in; the model is told to observe, so the gate must not
    # pre-empt the completed verdict it may find. An unreadable jar fails open the same way.
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    link = OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=link))
    monkeypatch.setattr(auth_tools, "validate_fetch_url", lambda url: url)
    if failure == "goto_error_after_cookie":
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
        page = _FakePage(goto_error=RuntimeError("Download is starting"), cookie_on_goto_error=True)
        expected = "if it did not sign in"
    elif failure == "url_moved_no_cookie":
        # A fragment-token SPA sign-in keeps its session in storage, not a cookie.
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
        page = _FakePage(goto_error=TimeoutError("load"), url_after_goto_error="https://app.test/home#token=abc")
        expected = "if it did not sign in"
    elif failure == "response_seen_same_url":
        # The link redirected back to the page it came from and refreshed the existing server session.
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
        page = _FakePage(goto_error=TimeoutError("load"))
        page.response_on_goto_error = True
        expected = "if it did not sign in"
    elif failure == "cookies_unreadable":
        monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", AsyncMock())
        page = _FakePage(goto_error=RuntimeError("net::ERR_CONNECTION_REFUSED"))
        page.context = SimpleNamespace(cookies=AsyncMock(side_effect=RuntimeError("page closed")))
        expected = "if it did not sign in"
    else:
        monkeypatch.setattr(
            auth_tools, "revalidate_redirect_chain", AsyncMock(side_effect=BlockedHost("tracker.example"))
        )
        page = _FakePage()
        expected = auth_tools._LATER_HOP_REFUSED
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, _provider(page), state=state)
    handler = {t.name: t.handler for t in tools}["open_verification_link"]

    result = await handler({})
    assert result.status == "error" and expected in result.content
    assert await state.block_finish("completed") is None


@pytest.mark.asyncio
async def test_get_verification_code_tail_shorter_than_a_poll_interval_counts_as_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The poll loop cannot fetch inside a slice shorter than its sleep, so a remaining budget below
    # that is exhaustion, not another "not yet" round trip through the resolver.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=20 / 60))
    clock = [0.0]
    monkeypatch.setattr(auth_tools, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    resolver_calls = 0

    async def _spends_12s(*_a: Any, **_k: Any) -> OTPValue | None:
        nonlocal resolver_calls
        resolver_calls += 1
        clock[0] += 12.0
        raise NoTOTPVerificationCodeFound(task_id="tsk_1", webhook_diagnostics="http_status=204x3")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _spends_12s)
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    first = await tools[0].handler({})
    second = await tools[0].handler({})
    assert first.content == f"{auth_tools._CODE_BUDGET_SPENT} It answered: http_status=204x3."
    assert second.content == auth_tools._CODE_BUDGET_SPENT
    assert resolver_calls == 1


@pytest.mark.asyncio
async def test_get_verification_code_slices_share_the_first_polls_email_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare task has no run start to anchor the email search on, so every slice must search from the
    # FIRST slice's start; re-anchoring per slice would skip a message that landed between slices.
    anchors: list[Any] = []

    async def _never_answers(*_a: Any, poll_started_at: Any, **_k: Any) -> OTPValue | None:
        anchors.append(poll_started_at)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _never_answers)
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="otp@example.test"))
    for _ in range(3):
        await tools[0].handler({})
    assert len(anchors) == 3 and anchors[0] is not None
    assert all(anchor == anchors[0] for anchor in anchors)


@pytest.mark.asyncio
async def test_get_verification_code_inner_timeout_does_not_spend_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # A timeout raised inside the resolver (DB, HTTP) reads as a source error, not as this tool's wait
    # cap or budget exhaustion.
    async def _inner_timeout(*_a: Any, **_k: Any) -> OTPValue | None:
        raise TimeoutError("pool acquire")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _inner_timeout)
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    first = await tools[0].handler({})
    second = await tools[0].handler({})
    for result in (first, second):
        assert "lookup failed: TimeoutError" in result.content and "minutes of polling time remain" in result.content


def test_build_auth_tools_present_with_totp_identifier() -> None:
    tools, guidance = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "verification code" in guidance.lower()


def test_build_auth_tools_present_with_payload_only_totp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # navigation_payload is resolve_otp_value's first waterfall source; the tool must be offered
    # from it alone, with no totp_identifier and no credential candidate.
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(navigation_payload={"mfa_code": "123456"}))
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "verification code" in guidance.lower()


@pytest.mark.asyncio
async def test_get_verification_code_resolves_and_registers_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    )
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert result.status == "ok" and "123456" in result.content
    # Registered for redaction on the task context (task-scoped, so a bare task is covered).
    assert "123456" in ctx.runtime_secret_values


@pytest.mark.asyncio
async def test_get_verification_code_no_code_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=None))
    tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert result.status == "error"
    assert ctx.runtime_secret_values == set()


@pytest.mark.asyncio
async def test_get_verification_code_fails_fast_on_a_magic_link(monkeypatch: pytest.MonkeyPatch) -> None:
    # A sign-in link is not a code and a page-free run cannot follow it: the tool must say so on the
    # first call (not burn the poll budget as "no code yet"), count it with one structured warning, and
    # never let the URL reach the model, the logs, or the secret registry.
    link = "https://example.com/signin?token=abc"
    resolver = AsyncMock(return_value=OTPValue(value=link, type=None))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            first = await tools[0].handler({})
            second = await tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert first.status == "error" and "sign-in link" in first.content and "returned an error" in first.content
    assert second.content == first.content and resolver.await_count == 1
    assert link not in first.content and "token=abc" not in str(logs)
    warnings = [e for e in logs if e.get("event") == "task_v3 verification source returned a magic link"]
    assert len(warnings) == 1
    assert warnings[0]["tool"] == "get_verification_code" and warnings[0]["otp_type"] == OTPType.MAGIC_LINK.value
    assert ctx.runtime_secret_values == set()


def test_registered_code_scrubbed_when_redaction_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    # The enabled global flag exercises the same gate used by bare-task artifact persistence.
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("482913")
    skyvern_context.set(ctx)
    try:
        secret_values = wcm.get_secret_values_for_run(None)
        payload = b'{"role": "tool", "content": "verification_code: 482913"}, {"type": "482913"}'
        redacted = redact_secrets_from_bytes(payload, secret_values)
    finally:
        skyvern_context.reset()
    assert b"482913" not in redacted


def test_get_secret_values_for_run_standalone_task_uses_global_artifact_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", True)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("987654")
    ctx.register_secret_value("12")  # too short to redact
    skyvern_context.set(ctx)
    try:
        assert wcm.get_secret_values_for_run(None) == {"987654"}
        assert wcm.get_secret_values_for_run(None, exclude_runtime_otp=True) == set()
    finally:
        skyvern_context.reset()


def _credential_parameter(key: str) -> CredentialParameter:
    now = datetime.now(timezone.utc)
    return CredentialParameter(
        key=key,
        credential_parameter_id=f"cp_{key}",
        workflow_id="w_test",
        credential_id=f"cred_{key}",
        created_at=now,
        modified_at=now,
    )


def _workflow_run_context_with_totp_credentials() -> WorkflowRunContext:
    workflow_run_context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_test",
        workflow_permanent_id="wp_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    workflow_run_context.values["cred_1"] = {"totp": "totp_id_1"}
    workflow_run_context.values["cred_2"] = {"totp": "totp_id_2"}
    workflow_run_context.secrets["totp_id_1_value"] = "SEED_ONE"
    workflow_run_context.secrets["totp_id_2_value"] = "SEED_TWO"
    workflow_run_context.parameters["cred_1"] = _credential_parameter("cred_1")
    workflow_run_context.parameters["cred_2"] = _credential_parameter("cred_2")
    return workflow_run_context


def test_try_generate_totp_from_credential_disambiguates_via_active_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two TOTP-bearing credentials in the same run: with no active credential, resolution is
    # ambiguous and yields nothing; active_credential_parameter_key (set by _execute_task_v3 for a
    # single-login-credential block) must select exactly that credential's TOTP secret, not the other.
    monkeypatch.setattr(otp_service, "generate_totp_code", lambda secret: f"code::{secret}")
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_test"] = _workflow_run_context_with_totp_credentials()
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", manager)

    skyvern_context.set(SkyvernContext(workflow_run_id="wr_test"))
    try:
        assert otp_service.try_generate_totp_from_credential("wr_test") is None
    finally:
        skyvern_context.reset()

    skyvern_context.set(SkyvernContext(workflow_run_id="wr_test", active_credential_parameter_key="cred_2"))
    try:
        otp = otp_service.try_generate_totp_from_credential("wr_test")
    finally:
        skyvern_context.reset()
    assert otp is not None
    assert otp.value == "code::SEED_TWO"


_VALID_SEED = "JBSWY3DPEHPK3PXP"
_TOTP_FIELD = "placeholder_Zz9_totp"
# A TOTP step boundary: 30 * 33_333_334. The clock below sits `offset` seconds into that step.
_STEP_START = 1_000_000_020


def _credential_totp_context(*, seed: str | None = _VALID_SEED) -> WorkflowRunContext:
    workflow_run_context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_test",
        workflow_permanent_id="wp_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    workflow_run_context.parameters["login"] = _credential_parameter("login")
    workflow_run_context.values["login"] = {"totp": _TOTP_FIELD}
    workflow_run_context.secrets[_TOTP_FIELD] = "BW_TOTP"
    if seed is not None:
        workflow_run_context.secrets[workflow_run_context.totp_secret_value_key(_TOTP_FIELD)] = seed
    return workflow_run_context


def _clock_in_step(monkeypatch: pytest.MonkeyPatch, offset: int) -> AsyncMock:
    """Start the TOTP clock `offset` seconds into a 30s step; the recorded window sleep advances it instantly."""
    assert _STEP_START % 30 == 0
    clock = {"now": float(_STEP_START + offset)}
    monkeypatch.setattr(credentials_module, "time", SimpleNamespace(time=lambda: clock["now"]))

    def _advance(seconds: float) -> None:
        clock["now"] += seconds

    sleep = AsyncMock(side_effect=_advance)
    monkeypatch.setattr(credentials_module, "asyncio", ScopedAsyncio(sleep=sleep))
    return sleep


def _install_run(monkeypatch: pytest.MonkeyPatch, run_context: WorkflowRunContext) -> None:
    manager = WorkflowContextManager()
    manager.workflow_run_contexts["wr_test"] = run_context
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", manager)


@pytest.mark.asyncio
@pytest.mark.parametrize(("offset", "expected_wait"), [(25, 5.0), (3, None)])
@pytest.mark.parametrize("entry", ["get_verification_code", "totp_placeholder"])
async def test_a_v3_credential_code_waits_out_a_nearly_spent_totp_window(
    monkeypatch: pytest.MonkeyPatch, entry: str, offset: int, expected_wait: float | None
) -> None:
    # A code minted with 5s left can expire before the site checks it. Both v3 entry points into the
    # credential's TOTP wait for the next step when fewer than the configured seconds remain, and
    # only then.
    sleep = _clock_in_step(monkeypatch, offset)
    _install_run(monkeypatch, _credential_totp_context())
    task = _task(workflow_run_id="wr_test")
    state = auth_tools.VerificationState(task=task, totp_min_remaining_seconds=20)
    tools, _ = auth_tools.build_auth_tools(task, state=state, allowed_credential_parameter_keys=["login"])
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test"))
    try:
        if entry == "get_verification_code":
            result = await tools[0].handler({})
            assert result.status == "ok"
        else:
            await state.resolve_totp_placeholder(_TOTP_FIELD)
    finally:
        skyvern_context.reset()
    if expected_wait is None:
        sleep.assert_not_awaited()
    else:
        sleep.assert_awaited_once_with(expected_wait)
    assert state.values_delivered == 1


@pytest.mark.asyncio
async def test_the_step_engine_credential_code_never_waits_for_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # The step engine's callers pass nothing, so a code 5s from expiry is still minted immediately.
    sleep = _clock_in_step(monkeypatch, 25)
    _install_run(monkeypatch, _credential_totp_context())
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test"))
    try:
        otp_value = await otp_service.resolve_otp_value(
            _task(workflow_run_id="wr_test"), expected_otp_type=OTPType.TOTP
        )
    finally:
        skyvern_context.reset()
    assert otp_value is not None and otp_value.from_credential_seed
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_sign_in_link_poll_never_waits_on_a_credential_totp_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # The credential's TOTP cannot answer a link poll, so waiting out its window only spends the shared
    # polling budget on a code that is thrown away.
    sleep = _clock_in_step(monkeypatch, 25)
    _install_run(monkeypatch, _credential_totp_context())
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test"))
    try:
        otp_value = await otp_service.resolve_otp_value(
            _task(workflow_run_id="wr_test"), expected_otp_type=OTPType.MAGIC_LINK, min_remaining_seconds=20
        )
    finally:
        skyvern_context.reset()
    assert otp_value is None
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seed", "owned_by_credential", "polling_source"),
    [
        (None, True, False),
        ("not a totp secret!", True, False),
        (_VALID_SEED, False, False),
        (None, True, True),
    ],
    ids=["no_secret", "unparseable_secret", "no_owning_credential", "no_secret_but_a_polling_source"],
)
async def test_a_totp_placeholder_with_no_usable_secret_is_refused_as_no_source_only_when_the_task_has_none(
    monkeypatch: pytest.MonkeyPatch, seed: str | None, owned_by_credential: bool, polling_source: bool
) -> None:
    _clock_in_step(monkeypatch, 3)
    run_context = _credential_totp_context(seed=seed)
    if not owned_by_credential:
        run_context.values["login"] = {}
    _install_run(monkeypatch, run_context)
    task = _task(workflow_run_id="wr_test", totp_verification_url="https://totp.example" if polling_source else None)
    state = auth_tools.VerificationState(task=task)
    ctx = SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs, pytest.raises(taskv3_loop.ToolRefusal) as excinfo:
            await state.resolve_totp_placeholder(_TOTP_FIELD)
    finally:
        skyvern_context.reset()
    if polling_source:
        assert str(excinfo.value) == auth_tools._CREDENTIAL_HAS_NO_CODE_SECRET
        assert "can be fetched" not in str(excinfo.value)
        assert state.last_status is None
    else:
        assert str(excinfo.value) == NO_ONE_TIME_CODE_SOURCE
        assert state.last_status == NO_ONE_TIME_CODE_SOURCE
    assert state.values_delivered == 0
    assert ctx.runtime_secret_values == set()
    assert _TOTP_FIELD not in str(excinfo.value) and _TOTP_FIELD not in str(logs)
    assert "BW_TOTP" not in str(excinfo.value) and "BW_TOTP" not in str(logs)


def test_the_guidance_is_one_line_whatever_code_source_the_run_has(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    _, without_source = auth_tools.build_auth_tools(_task())
    _, with_source = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"))
    assert without_source == with_source == auth_tools._GUIDANCE
    assert "Never invent or guess a code" in with_source and "get_verification_code" in with_source
    assert "finish" not in with_source


def test_get_secret_values_for_run_standalone_task_respects_disabled_global_artifact_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm.settings, "ENABLE_SECRET_ARTIFACT_REDACTION", False)
    wcm = WorkflowContextManager()
    ctx = SkyvernContext(task_id="tsk_1")
    ctx.register_secret_value("987654")
    skyvern_context.set(ctx)
    try:
        assert wcm.get_secret_values_for_run(None) == set()
        assert wcm.get_secret_values_for_run(None, respect_artifact_redaction_flag=False) == {"987654"}
        assert (
            wcm.get_secret_values_for_run(
                None,
                exclude_runtime_otp=True,
                respect_artifact_redaction_flag=False,
            )
            == set()
        )
    finally:
        skyvern_context.reset()


_LINK = "https://example.test/magic?token=synthetictoken0123"
_LINK_TOKEN = "synthetictoken0123"


@pytest.fixture(autouse=True)
def _fetch_validator_accepts_synthetic_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real validator resolves DNS and .test never resolves; the SSRF tests re-patch this to raise,
    # so the gate itself stays under test.
    monkeypatch.setattr(auth_tools, "validate_fetch_url", lambda url: url)


class _FakePage:
    """Playwright page stand-in for the sign-in-link tool: records navigations and serves the landing
    text the tool scans for close-page signals."""

    def __init__(
        self,
        *,
        url: str = "https://app.test/login",
        status: int = 200,
        body_text: str = "you are signed in",
        goto_error: Exception | None = None,
        url_after_goto_error: str | None = None,
        cookie_on_goto_error: bool = False,
    ) -> None:
        self.url = url
        self.origin_url = url
        self.status = status
        self.body_text = body_text
        self.goto_error = goto_error
        self.url_after_goto_error = url_after_goto_error
        self.cookie_on_goto_error = cookie_on_goto_error
        self.goto_calls: list[str] = []
        self.goto_timeouts: list[float | None] = []
        self.cookies: list[dict[str, str]] = [{"domain": "app.test", "path": "/", "name": "csrf", "value": "1"}]
        self.context = SimpleNamespace(cookies=self._cookies)
        self.response_on_goto_error = False
        self.subresource_on_goto_error = False
        self.main_frame = object()
        self._listeners: list[Any] = []

    def _fire(self, status: int, navigation: bool, main_frame: bool = True) -> None:
        request = SimpleNamespace(
            is_navigation_request=lambda: navigation, frame=self.main_frame if main_frame else object()
        )
        for listener in self._listeners:
            listener(SimpleNamespace(status=status, request=request))

    def on(self, event: str, handler: Any) -> None:
        self._listeners.append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self._listeners.remove(handler)

    async def _cookies(self) -> list[dict[str, str]]:
        return list(self.cookies)

    async def goto(self, url: str, timeout: float | None = None) -> Any:
        # ``goto_error`` models a link that fails to open; navigating back to the page the run came
        # from still works, so a restore attempt is observable.
        self.goto_calls.append(url)
        self.goto_timeouts.append(timeout)
        if self.goto_error is not None and url != self.origin_url:
            if self.url_after_goto_error is not None:
                self.url = self.url_after_goto_error
            if self.cookie_on_goto_error:
                self.cookies.append({"domain": "app.test", "path": "/", "name": "sess", "value": "abc"})
            if self.subresource_on_goto_error:
                self._fire(200, navigation=False)
            if self.response_on_goto_error:
                self._fire(302, navigation=True)
            raise self.goto_error
        self.url = url
        self._fire(self.status, navigation=True)
        if url != self.origin_url:
            self.cookies.append({"domain": "app.test", "path": "/", "name": "sess", "value": "abc"})
        return SimpleNamespace(status=self.status)

    async def inner_text(self, selector: str, timeout: float | None = None) -> str:
        return self.body_text


def _provider(page: Any) -> Any:
    async def _get_page() -> Any:
        return page

    return _get_page


def _link_tools(page: Any) -> dict[str, Any]:
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"), _provider(page))
    return {t.name: t.handler for t in tools}


def test_build_auth_tools_offers_the_link_tool_only_with_a_link_source_and_a_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(_FakePage())
    # A credential authenticator can only ever produce a code, so a page alone must not offer link-following.
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: True)
    tools, guidance = auth_tools.build_auth_tools(_task(workflow_run_id="wr_1"), provider)
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "open_verification_link" not in guidance

    tools, guidance = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"), provider)
    assert [t.name for t in tools] == ["get_verification_code", "open_verification_link"]
    assert "open_verification_link" in guidance

    # No page to navigate (page-free run): the navigating tool must not be offered.
    tools, guidance = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"), None)
    assert [t.name for t in tools] == ["get_verification_code"]
    assert "open_verification_link" not in guidance


@pytest.mark.asyncio
async def test_open_verification_link_opens_it_backend_side_without_exposing_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model must learn only that the link was opened: the URL (and its token) must reach the browser
    # and nothing else — not the tool result, not the logs — and must be registered for redaction.
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=_LINK, type=None)))
    page = _FakePage()
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            result = await _link_tools(page)["open_verification_link"]({})
    finally:
        skyvern_context.reset()

    assert result.status == "ok" and result.data == {"page_state_changed": True}
    assert page.goto_calls == [_LINK]
    # A goto with no timeout blocks on the default 30s; the step engine's goto budget applies here too.
    assert page.goto_timeouts == [auth_tools.settings.BROWSER_LOADING_TIMEOUT_MS]
    for leak in (_LINK, _LINK_TOKEN, "example.test", "/magic"):
        assert leak not in result.content
        assert leak not in str(logs)
    assert {_LINK, _LINK_TOKEN} <= ctx.model_hidden_values
    assert {_LINK, _LINK_TOKEN} <= ctx.runtime_secret_values


@pytest.mark.asyncio
async def test_open_verification_link_returns_to_the_original_page_after_a_close_this_window_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Many link flows land on a dead-end "you may now close this window" page; the run must be back on
    # the page it came from, or the model observes a page it can do nothing with.
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=_LINK, type=None)))
    page = _FakePage(url="https://app.test/login", body_text="Verified! You may now close this window.")
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        result = await _link_tools(page)["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert result.status == "ok"
    assert page.goto_calls == [_LINK, "https://app.test/login"]


@pytest.mark.parametrize("status", [200, 410])
@pytest.mark.asyncio
async def test_open_verification_link_consumes_the_link_so_the_next_call_polls_for_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    # A link is single-use whether the site accepted it or rejected it, so it must not be replayed:
    # the second call has to fetch a fresh one. An expired link must also not read as a sign-in.
    resolver = AsyncMock(return_value=OTPValue(value=_LINK, type=None))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    page = _FakePage(status=status)
    handlers = _link_tools(page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        with capture_logs() as logs:
            first = await handlers["open_verification_link"]({})
            second = await handlers["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    if status == 410:
        assert first.status == "error" and "rejected" in first.content and "expired" in first.content
    else:
        assert first.status == "ok"
    # The navigation happened either way, so the loop's action-loop guard must be told the page moved.
    assert first.data == {"page_state_changed": True}
    assert second.status == first.status
    assert resolver.await_count == 2
    assert page.goto_calls == [_LINK, _LINK]
    for leak in (_LINK, _LINK_TOKEN):
        assert leak not in first.content and leak not in str(logs)


@pytest.mark.asyncio
async def test_open_verification_link_keeps_the_driver_code_through_the_return_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The return navigation that puts the tab back is cleanup, not the navigation the task chose, so its
    # success must not wipe the code of the link that failed.
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=_LINK, type=None)))
    monkeypatch.setattr(navigation_module, "host_has_no_address_record", lambda host: False)
    page = _FakePage(
        goto_error=Exception(f"net::ERR_TUNNEL_CONNECTION_FAILED at {_LINK}"),
        url_after_goto_error="https://example.test/interstitial",
    )
    handlers = _link_tools(page)
    context = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(context)
    try:
        result = await handlers["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert result.status == "error"
    assert page.goto_calls[:2] == [_LINK, "https://app.test/login"]
    assert context.task_nav_error_codes == {"tsk_1": "net::ERR_TUNNEL_CONNECTION_FAILED"}


@pytest.mark.asyncio
async def test_open_verification_link_navigation_failure_restores_the_page_and_spends_the_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Playwright embeds the target URL in its error message, so neither the model-facing result nor the
    # log line may carry the exception text. A goto that failed mid-navigation left the tab elsewhere,
    # so the run must be put back; and the link reached the browser, so the retry polls for a fresh one
    # instead of replaying a link the site may have already burned.
    resolver = AsyncMock(return_value=OTPValue(value=_LINK, type=None))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    page = _FakePage(
        goto_error=Exception(f"net::ERR_FAILED at {_LINK}"),
        url_after_goto_error="https://example.test/interstitial",
    )
    handlers = _link_tools(page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        with capture_logs() as logs:
            result = await handlers["open_verification_link"]({})
            page.goto_error = None
            retry = await handlers["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert result.status == "error" and "failed to open the sign-in link" in result.content
    assert result.data == {"page_state_changed": True}
    assert page.goto_calls[:2] == [_LINK, "https://app.test/login"]
    assert retry.status == "ok"
    assert resolver.await_count == 2
    for leak in (_LINK, _LINK_TOKEN, "example.test"):
        assert leak not in result.content and leak not in str(logs)
    # A refused/failed attempt still spends the anchor, so the retry's poll looks for a link newer
    # than the one that just failed rather than replaying the same anchor.
    first_poll_started_at = resolver.await_args_list[0].kwargs["poll_started_at"]
    second_poll_started_at = resolver.await_args_list[1].kwargs["poll_started_at"]
    assert second_poll_started_at is not None
    assert second_poll_started_at > first_poll_started_at


@pytest.mark.parametrize(
    ("error", "expected_snippet", "unexpected_snippet"),
    [
        (InvalidUrl(url=_LINK), "refused", "failed to open"),
        # UnresolvableHost is a BlockedHost subclass but means the worker could not resolve DNS (the
        # browser resolves through the run proxy), so it must not be reported as a policy refusal.
        (UnresolvableHost(host="example.test"), "failed to open the sign-in link", "not allowed"),
    ],
    ids=["policy-refusal", "worker-dns-failure"],
)
@pytest.mark.asyncio
async def test_open_verification_link_when_the_fetch_validator_rejects_the_url(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_snippet: str,
    unexpected_snippet: str,
) -> None:
    # The link comes from an email the target site controls, so it is untrusted input: it must clear the
    # same SSRF gate as the step engine's goto, and the refusal must not echo the URL it names. Nothing
    # navigated, so the page did not move -- but the link is still spent, so a retry polls again.
    resolver = AsyncMock(return_value=OTPValue(value=_LINK, type=None))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)

    def _refuse(url: str) -> str:
        raise error

    monkeypatch.setattr(auth_tools, "validate_fetch_url", _refuse)
    page = _FakePage()
    handlers = _link_tools(page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        with capture_logs() as logs:
            result = await handlers["open_verification_link"]({})
            await handlers["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert result.status == "error" and expected_snippet in result.content
    assert unexpected_snippet not in result.content
    assert result.data is None
    assert page.goto_calls == []
    assert resolver.await_count == 2
    for leak in (_LINK, _LINK_TOKEN, "example.test"):
        assert leak not in result.content and leak not in str(logs)


@pytest.mark.asyncio
async def test_open_verification_link_refuses_a_redirect_hop_that_lands_on_a_blocked_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A public-looking link can redirect onto an internal host, so the followed chain is revalidated
    # exactly like the step engine's goto.
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=_LINK, type=None)))

    # The real helper resets the tab to about:blank before re-raising, so the run is left staring at a
    # blank page unless the tool puts it back.
    async def _refuse_chain(*_a: Any, **_k: Any) -> None:
        page.url = "about:blank"
        raise BlockedHost(host="internal.example.test")

    monkeypatch.setattr(auth_tools, "revalidate_redirect_chain", _refuse_chain)
    page = _FakePage()
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        with capture_logs() as logs:
            result = await _link_tools(page)["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert result.status == "error" and "refused" in result.content
    assert result.data == {"page_state_changed": True}
    assert page.goto_calls == [_LINK, "https://app.test/login"]
    for leak in (_LINK, _LINK_TOKEN, "example.test"):
        assert leak not in result.content and leak not in str(logs)


@pytest.mark.asyncio
async def test_open_verification_link_hides_only_the_opaque_parts_of_the_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Redaction is exact-match and global for the run, so registering readable query values (an address,
    # a return URL, a landing path, a name, a language flag) would blank that text everywhere the model
    # looks. Real links percent-encode those values, so the encoded form must not read as opaque either.
    link = (
        "https://example.test/magic?token=synthetictoken0123456789"
        "&email=user%40example.test"
        "&reply_to=user@example.test"
        "&redirect_to=https%3A%2F%2Fexample.test%2Fhome"
        "&next=%2Fhome%2Fdashboard%2Fsettings"
        "&name=Jane+Doe+Example"
        "&tok2=abc%2Bdef0123456789"
        "&lang=en"
    )
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=link, type=None)))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        await _link_tools(_FakePage())["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    # A token is hidden in both the form the page echoes and the form the browser reports back.
    assert {link, "synthetictoken0123456789", "abc%2Bdef0123456789", "abc+def0123456789"} <= ctx.model_hidden_values
    for readable in (
        "user@example.test",
        "user%40example.test",
        "https://example.test/home",
        "https%3A%2F%2Fexample.test%2Fhome",
        "/home/dashboard/settings",
        "%2Fhome%2Fdashboard%2Fsettings",
        "Jane Doe Example",
        "Jane+Doe+Example",
        "en",
    ):
        assert readable not in ctx.model_hidden_values


@pytest.mark.asyncio
async def test_open_verification_link_hides_a_bare_token_shaped_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fragment segment with no "=" (an SPA hash router carrying the token bare, not as key=value)
    # is the value itself, not the empty string partition("=") would otherwise yield.
    link = "https://example.test/callback#tok0123456789abcdefghij"
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=link, type=None)))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        await _link_tools(_FakePage())["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert "tok0123456789abcdefghij" in ctx.model_hidden_values


@pytest.mark.asyncio
async def test_open_verification_link_does_not_hide_a_bare_readable_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = "https://example.test/callback#section-overview"
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=link, type=None)))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        await _link_tools(_FakePage())["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert "section-overview" not in ctx.model_hidden_values


@pytest.mark.asyncio
async def test_open_verification_link_hides_the_url_the_validator_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The validator can hand back a normalised URL, and that is the one the browser navigates to and
    # reports back, so both forms must be hidden.
    link = "https://example.test/magic/synthetictoken0123456789"
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=link, type=None)))
    monkeypatch.setattr(auth_tools, "validate_fetch_url", lambda url: url + "/")
    page = _FakePage()
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        await _link_tools(page)["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert {link, link + "/"} <= ctx.model_hidden_values
    assert page.goto_calls == [link + "/"]


@pytest.mark.asyncio
async def test_open_verification_link_hides_the_truncated_url_observe_would_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # observe echoes page.url truncated to OBSERVE_URL_MAX_CHARS; exact-match redaction misses that
    # prefix unless it is registered too.
    link = "https://example.test/magic?token=" + "a" * 400
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value=link, type=None)))
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        await _link_tools(_FakePage())["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert link[:OBSERVE_URL_MAX_CHARS] in ctx.model_hidden_values


@pytest.mark.asyncio
async def test_verification_code_tool_hands_a_sign_in_link_to_the_link_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The webhook source does not filter by type, so the code tool can receive a link: it must redirect
    # the model to the link tool, which then opens the link it already fetched instead of re-polling.
    resolver = AsyncMock(return_value=OTPValue(value=_LINK, type=None))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    page = _FakePage()
    handlers = _link_tools(page)
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        with capture_logs() as logs:
            redirect = await handlers["get_verification_code"]({})
            opened = await handlers["open_verification_link"]({})
    finally:
        skyvern_context.reset()
    assert redirect.status == "error" and "open_verification_link" in redirect.content
    assert opened.status == "ok" and page.goto_calls == [_LINK]
    assert resolver.await_count == 1
    for leak in (_LINK, _LINK_TOKEN):
        assert leak not in redirect.content and leak not in str(logs)
    # The unsupported-engine warning is a production metric: it must not fire when the link IS followed.
    assert [e for e in logs if e.get("event") == "task_v3 verification source returned a magic link"] == []


@pytest.mark.asyncio
async def test_link_tool_hands_a_verification_code_back_to_the_code_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    # The mirror case: a code arriving at the link tool must not be dropped — the code tool serves it
    # without paying for another poll, and nothing is navigated.
    resolver = AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    page = _FakePage()
    handlers = _link_tools(page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        handoff = await handlers["open_verification_link"]({})
        code = await handlers["get_verification_code"]({})
    finally:
        skyvern_context.reset()
    assert handoff.status == "error" and "get_verification_code" in handoff.content
    assert page.goto_calls == []
    assert code.status == "ok" and "verification_code: 123456" in code.content
    assert resolver.await_count == 1


_RUN_ID = "wr_link"
_STORED_IDENTIFIER = "application_synthetic0001"
_ACTIVATION_LINK = "https://example.test/careers/activate/synthetictoken4567/"
_RESENT_ACTIVATION_LINK = "https://example.test/careers/activate/synthetictoken8901/"
# A slice that has to time out; one that delivers returns on the first tick of the default 5 s slice.
_TIMEOUT_SLICE_SECONDS = 0.3


def _stored_row(
    totp_code_id: str,
    code: str,
    otp_type: OTPType,
    created_at: datetime,
    *,
    workflow_run_id: str | None = _RUN_ID,
) -> TOTPCode:
    return TOTPCode(
        totp_code_id=totp_code_id,
        totp_identifier=_STORED_IDENTIFIER,
        organization_id="o_1",
        content=code,
        code=code,
        otp_type=otp_type,
        workflow_run_id=workflow_run_id,
        created_at=created_at,
        modified_at=created_at,
    )


def _stored_code_tools(
    monkeypatch: pytest.MonkeyPatch, rows: list[TOTPCode], page: _FakePage, *, page_offered: bool = True
) -> dict[str, Any]:
    """Both tools over a workflow-run task whose only source is the stored-code table, read through the real
    resolve_otp_value -> poll_otp_value -> _get_otp_value_from_db chain."""

    async def _get_otp_codes(**_kwargs: Any) -> list[TOTPCode]:
        return list(rows)

    async def _brief_sleep(_seconds: float) -> None:
        await asyncio.sleep(0.01)

    run = SimpleNamespace(
        workflow_id="w_1", workflow_permanent_id="wpid_1", started_at=_TASK_CREATED_AT - timedelta(minutes=10)
    )
    monkeypatch.setattr(otp_service.app, "WORKFLOW_CONTEXT_MANAGER", WorkflowContextManager())
    monkeypatch.setattr(
        otp_service.app,
        "DATABASE",
        SimpleNamespace(
            otp=SimpleNamespace(get_otp_codes=_get_otp_codes, get_raw_otp_codes=AsyncMock(return_value=[])),
            workflow_runs=SimpleNamespace(get_workflow_run=AsyncMock(return_value=run)),
        ),
    )
    monkeypatch.setattr(otp_service, "_get_otp_value_from_email", AsyncMock(return_value=None))
    # The LLM seam: neither a bare link nor these rows' content holds a code.
    monkeypatch.setattr(otp_service, "parse_otp_login", AsyncMock(return_value=None))
    monkeypatch.setattr(otp_service, "asyncio", ScopedAsyncio(sleep=_brief_sleep))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 5.0)
    task = _task(workflow_run_id=_RUN_ID, totp_identifier=_STORED_IDENTIFIER)
    tools, _ = auth_tools.build_auth_tools(task, _provider(page) if page_offered else None)
    return {t.name: t.handler for t in tools}


@pytest.mark.asyncio
async def test_code_tool_hands_a_stored_run_scoped_sign_in_link_to_the_link_tool_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A site that verifies by link while the model asks for a code: the newest stored link for this run must
    # send the model to the link tool instead of waiting out the code budget. Once opened, neither it nor the
    # older link its resend replaced may be handed back, since the page may then ask for a real code.
    page = _FakePage()
    rows = [
        _stored_row("otp_link", _ACTIVATION_LINK, OTPType.MAGIC_LINK, _TASK_CREATED_AT + timedelta(seconds=40)),
        _stored_row(
            "otp_resent", _RESENT_ACTIVATION_LINK, OTPType.MAGIC_LINK, _TASK_CREATED_AT + timedelta(seconds=90)
        ),
    ]
    handlers = _stored_code_tools(monkeypatch, rows, page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        redirect = await handlers["get_verification_code"]({})
        opened = await handlers["open_verification_link"]({})
        monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", _TIMEOUT_SLICE_SECONDS)
        after_open = await handlers["get_verification_code"]({})
    finally:
        skyvern_context.reset()

    assert redirect.status == "error" and redirect.content == auth_tools._MAGIC_LINK_REDIRECT
    assert opened.status == "ok"
    assert "code has arrived yet" in after_open.content
    assert page.goto_calls == [_RESENT_ACTIVATION_LINK]


@pytest.mark.asyncio
async def test_code_tool_called_again_instead_of_opening_the_link_polls_for_a_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model sent to the link tool that asks for a code again may be on a page that wants one (a site that
    # mails a code and a link): it must get the stored code, not the same redirect on every call.
    page = _FakePage()
    rows = [
        _stored_row("otp_code", "482913", OTPType.TOTP, _TASK_CREATED_AT + timedelta(seconds=30)),
        _stored_row("otp_link", _ACTIVATION_LINK, OTPType.MAGIC_LINK, _TASK_CREATED_AT + timedelta(seconds=40)),
    ]
    handlers = _stored_code_tools(monkeypatch, rows, page)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        redirect = await handlers["get_verification_code"]({})
        repeat = await handlers["get_verification_code"]({})
    finally:
        skyvern_context.reset()

    assert redirect.content == auth_tools._MAGIC_LINK_REDIRECT
    assert repeat.content == "verification_code: 482913"
    assert page.goto_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rows", "page_offered", "expected"),
    [
        pytest.param(
            [
                _stored_row(
                    "otp_link",
                    "https://other.example.test/activate/synthetictoken8901/",
                    OTPType.MAGIC_LINK,
                    _TASK_CREATED_AT + timedelta(seconds=60),
                    workflow_run_id=None,
                ),
                _stored_row("otp_code", "482913", OTPType.TOTP, _TASK_CREATED_AT + timedelta(seconds=30)),
            ],
            True,
            "verification_code: 482913",
            id="unscoped_link_on_a_shared_identifier",
        ),
        pytest.param(
            [_stored_row("otp_link", _ACTIVATION_LINK, OTPType.MAGIC_LINK, _TASK_CREATED_AT - timedelta(seconds=60))],
            True,
            "code has arrived yet",
            id="run_scoped_link_from_before_this_task",
        ),
        pytest.param(
            [_stored_row("otp_link", _ACTIVATION_LINK, OTPType.MAGIC_LINK, _TASK_CREATED_AT + timedelta(seconds=40))],
            False,
            "code has arrived yet",
            id="page_free_run_cannot_open_a_link",
        ),
    ],
)
async def test_code_tool_never_redirects_to_a_stored_link_this_task_must_not_follow(
    monkeypatch: pytest.MonkeyPatch, rows: list[TOTPCode], page_offered: bool, expected: str
) -> None:
    # An identifier shared across runs stores other sites' links unscoped, an earlier block's link is not
    # this page's, and a page-free run has no link tool: none of them may turn a code wait into a redirect
    # or a terminal "unsupported link", and a code the page asked for is still delivered.
    page = _FakePage()
    handlers = _stored_code_tools(monkeypatch, rows, page, page_offered=page_offered)
    if expected == "code has arrived yet":
        monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", _TIMEOUT_SLICE_SECONDS)
    skyvern_context.set(SkyvernContext(task_id="tsk_1"))
    try:
        result = await handlers["get_verification_code"]({})
    finally:
        skyvern_context.reset()

    assert expected in result.content
    assert result.content != auth_tools._MAGIC_LINK_REDIRECT
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_verification_tools_share_one_polling_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two tools over one verification source must not double the wait a never-answering source can buy.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=0.1 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.005)
    monkeypatch.setattr(auth_tools, "_MIN_SLICE_SECONDS", 0.0)
    resolver_calls = 0

    async def _never_answers(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        nonlocal resolver_calls
        resolver_calls += 1
        await asyncio.sleep(max_wait_seconds)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _never_answers)
    handlers = _link_tools(_FakePage())
    for _ in range(30):
        spent = await handlers["open_verification_link"]({})
    assert "budget exhausted" in spent.content
    calls_after_link_tool = resolver_calls
    code_result = await handlers["get_verification_code"]({})
    assert code_result.status == "error" and code_result.content == auth_tools._CODE_BUDGET_SPENT
    assert resolver_calls == calls_after_link_tool


def test_block_credential_parameter_keys_script_mode_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """SKY-15181 review: script-built blocks carry no parameters=, so deriving scope from them would
    silently disable credential-TOTP for cached-script logins; script-mode runs stay legacy (None)."""
    from types import SimpleNamespace as NS

    from skyvern.forge import agent as agent_module

    block = NS(parameters=[])
    monkeypatch.setattr(
        agent_module.app,
        "WORKFLOW_CONTEXT_MANAGER",
        NS(has_workflow_run_context=lambda _id: True, get_workflow_run_context=lambda _id: NS()),
    )
    with skyvern_context.scoped(SkyvernContext(script_mode=True)):
        assert agent_module.block_credential_parameter_keys(block, "wr_test") is None
    with skyvern_context.scoped(SkyvernContext()):
        assert agent_module.block_credential_parameter_keys(NS(parameters=[]), "wr_test") == []


_ORDER_WORDS = re.compile(r"\b(finish|call|do not|don't|must|should|trigger)\b", re.IGNORECASE)


def _two_credential_context(*, seed_a: str | None, seed_b: str | None) -> WorkflowRunContext:
    workflow_run_context = WorkflowRunContext(
        workflow_title="t",
        workflow_id="w_test",
        workflow_permanent_id="wp_test",
        workflow_run_id="wr_test",
        aws_client=MagicMock(),
    )
    for key, seed in (("cred_a", seed_a), ("cred_b", seed_b)):
        placeholder = f"placeholder_{key}_totp"
        workflow_run_context.parameters[key] = _credential_parameter(key)
        workflow_run_context.values[key] = {"totp": placeholder}
        workflow_run_context.secrets[placeholder] = "BW_TOTP"
        if seed is not None:
            workflow_run_context.secrets[workflow_run_context.totp_secret_value_key(placeholder)] = seed
    return workflow_run_context


_STATUS_CASES: list[tuple[str, Any, str]] = [
    ("empty_answer_from_a_working_source", None, "no verification code has arrived yet; about"),
    ("timed_out_slice", NoTOTPVerificationCodeFound(task_id="tsk_1"), "no verification code has arrived yet; about"),
    (
        "webhook_answering_http_500",
        NoTOTPVerificationCodeFound(task_id="tsk_1", webhook_diagnostics="http_status=500x12"),
        "no verification code has arrived yet (http_status=500x12); about",
    ),
    (
        "transport_error",
        FailedToGetTOTPVerificationCode(task_id="tsk_1", reason="request failed exception_type=ConnectError"),
        "returned an error (FailedToGetTOTPVerificationCode: request failed exception_type=ConnectError); about",
    ),
    ("lookup_raised", RuntimeError("boom"), "returned an error (verification lookup failed: RuntimeError); about"),
    (
        "link_with_no_link_tool",
        OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK),
        "returned an error (it sent a sign-in link, which this run cannot open); about",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("case", "answer", "expected"), _STATUS_CASES, ids=[c[0] for c in _STATUS_CASES])
async def test_each_code_tool_answer_is_one_plain_status_the_run_keeps_as_its_last(
    monkeypatch: pytest.MonkeyPatch, case: str, answer: Any, expected: str
) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    resolver = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(return_value=answer)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)

    result = await tools[0].handler({})

    assert result.status == "error" and expected in result.content
    assert not _ORDER_WORDS.search(result.content)
    assert state.last_status == result.content
    assert await state.block_finish("completed") is None


@pytest.mark.asyncio
async def test_a_spent_polling_budget_is_reported_without_polling_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    state.polling_spent_seconds = state.budget_seconds

    result = await tools[0].handler({})

    assert result.content == auth_tools._CODE_BUDGET_SPENT and not _ORDER_WORDS.search(result.content)
    assert state.last_status == auth_tools._CODE_BUDGET_SPENT
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_source_that_answers_at_once_still_spends_the_polling_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=1))
    resolver = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)

    answers = [(await tools[0].handler({})).content for _ in range(8)]

    assert answers[-1] == auth_tools._CODE_BUDGET_SPENT
    assert resolver.await_count <= 60 / auth_tools._MIN_SLICE_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "bare_task_with_no_source",
        "magic_link_only_payload",
        "credential_seed_unusable",
        "two_credentials_no_usable_seed",
    ],
)
async def test_a_run_with_no_code_source_is_told_so_at_once_without_polling(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    resolver = AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    task = _task()
    if case == "magic_link_only_payload":
        task = _task(navigation_payload={"verification_link": "https://example.test/x"})
    elif case == "credential_seed_unusable":
        _install_run(monkeypatch, _credential_totp_context(seed="not a totp secret!"))
        task = _task(workflow_run_id="wr_test")
    elif case == "two_credentials_no_usable_seed":
        _install_run(monkeypatch, _two_credential_context(seed_a=None, seed_b="not a totp secret!"))
        task = _task(workflow_run_id="wr_test")
    state = auth_tools.VerificationState(task=task)
    tools, guidance = auth_tools.build_auth_tools(task, state=state)
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id=task.workflow_run_id))
    try:
        result = await tools[0].handler({})
    finally:
        skyvern_context.reset()

    assert [t.name for t in tools] == ["get_verification_code"] and "get_verification_code" in guidance
    assert result.status == "error" and result.content == NO_ONE_TIME_CODE_SOURCE
    assert state.last_status == NO_ONE_TIME_CODE_SOURCE
    assert state.polling_spent_seconds == 0.0
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("seed_b", [_VALID_SEED, None], ids=["both_seeded", "one_seeded"])
async def test_several_code_credentials_with_none_in_use_are_not_reported_as_no_code_source(
    monkeypatch: pytest.MonkeyPatch, seed_b: str | None
) -> None:
    resolver = AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    monkeypatch.setattr(auth_tools, "resolve_otp_value", resolver)
    _install_run(monkeypatch, _two_credential_context(seed_a=_VALID_SEED, seed_b=seed_b))
    task = _task(workflow_run_id="wr_test")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    skyvern_context.set(SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test"))
    try:
        result = await tools[0].handler({})
        refusal = None
        if seed_b is None:
            with pytest.raises(taskv3_loop.ToolRefusal) as refused:
                await state.resolve_totp_placeholder("placeholder_cred_b_totp")
            refusal = str(refused.value)
    finally:
        skyvern_context.reset()

    assert result.status == "error" and result.content == auth_tools._CODE_CREDENTIAL_NOT_IN_USE
    assert state.last_status != NO_ONE_TIME_CODE_SOURCE
    assert refusal == (None if seed_b else auth_tools._CREDENTIAL_HAS_NO_CODE_SECRET)
    assert state.polling_spent_seconds == 0.0
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_code_source_is_judged_per_call_as_the_active_credential_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_run(monkeypatch, _two_credential_context(seed_a=_VALID_SEED, seed_b=None))
    task = _task(workflow_run_id="wr_test")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    handler = tools[0].handler

    answers = []
    for active in ("cred_a", "cred_b", "cred_a"):
        skyvern_context.set(
            SkyvernContext(task_id="tsk_1", workflow_run_id="wr_test", active_credential_parameter_key=active)
        )
        try:
            answers.append((await handler({}), state.last_status))
        finally:
            skyvern_context.reset()

    (first, after_first), (second, after_second), (third, after_third) = answers
    assert first.status == "ok" and first.content.startswith("verification_code: ") and after_first is None
    assert second.content == NO_ONE_TIME_CODE_SOURCE and after_second == NO_ONE_TIME_CODE_SOURCE
    assert third.status == "ok" and after_third is None
    assert state.values_delivered == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [True, False], ids=["link_handoff", "no_handoff"])
async def test_a_code_status_does_not_outlive_a_later_link_failure(
    monkeypatch: pytest.MonkeyPatch, handoff: bool
) -> None:
    link = OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(side_effect=[None, link if handoff else None]))
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, page_provider=AsyncMock(return_value=None), state=state)
    handlers = {t.name: t.handler for t in tools}

    not_yet = await handlers["get_verification_code"]({})
    assert state.last_status == not_yet.content
    if handoff:
        redirect = await handlers["get_verification_code"]({})
        assert redirect.content == auth_tools._MAGIC_LINK_REDIRECT and not _ORDER_WORDS.search(redirect.content)
    failed = await handlers["open_verification_link"]({})

    assert failed.content == (auth_tools._PAGE_UNAVAILABLE if handoff else auth_tools._NO_LINK_AVAILABLE)
    assert state.last_status is None
    assert await state.block_finish("failed") is None


@pytest.mark.asyncio
async def test_a_not_yet_reports_the_polling_time_left_at_that_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two slices far enough apart that the recorded spends differ at the record's own resolution.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.2)

    async def _not_yet(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        await asyncio.sleep(max_wait_seconds)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _not_yet)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)

    with capture_logs() as logs:
        first = await tools[0].handler({})
        second = await tools[0].handler({})

    slices = [e for e in logs if e.get("event") == "task_v3 verification poll slice timed out"]
    assert [e["tool"] for e in slices] == ["get_verification_code", "get_verification_code"]
    assert slices[0]["polling_spent_seconds"] < slices[1]["polling_spent_seconds"]
    for result, record in zip((first, second), slices):
        assert record["polling_spent_seconds"] + record["remaining_seconds"] == pytest.approx(600.0, abs=0.1)
        minutes = float(re.search(r"about ([\d.]+) minutes of polling time remain", result.content).group(1))
        assert minutes == pytest.approx((600.0 - record["polling_spent_seconds"]) / 60.0, abs=0.05)
    assert state.awaiting_link_since is None
    assert [e for e in logs if e.get("event") == "task_v3 verification source failed"] == []


@pytest.mark.asyncio
async def test_a_slice_that_spends_the_budget_says_so_and_narrates_without_refusing_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=0.55 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(auth_tools, "_MIN_SLICE_SECONDS", 0.5)

    async def _not_yet(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        await asyncio.sleep(max_wait_seconds)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _not_yet)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, state=state)

    with capture_logs() as logs:
        first = await tools[0].handler({})
        second = await tools[0].handler({})

    assert 0 < state.remaining_budget_seconds < auth_tools._MIN_SLICE_SECONDS
    assert first.content == second.content == auth_tools._CODE_BUDGET_SPENT
    armings = [e for e in logs if e.get("event") == "task_v3 verification source failed"]
    assert [e["reason"] for e in armings] == ["budget_exhausted", "budget_exhausted"]
    assert await state.block_finish("completed") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_result", ["returns_nothing", "source_errored", "lookup_raised"])
async def test_only_the_link_tool_holds_a_conversion_and_a_give_up_and_refuses_a_completion(
    monkeypatch: pytest.MonkeyPatch, resolver_result: str
) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)

    async def _answer(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        await asyncio.sleep(max_wait_seconds)
        if resolver_result == "returns_nothing":
            return None
        if resolver_result == "source_errored":
            raise FailedToGetTOTPVerificationCode(task_id="tsk_1", reason="http_status=500")
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _answer)
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    converted = taskv3_loop.CONVERSION_VERIFICATION_STATUS
    for tool_name in ("get_verification_code", "open_verification_link"):
        task = _task(totp_verification_url="https://totp.example")
        state = auth_tools.VerificationState(task=task)
        tools, _ = auth_tools.build_auth_tools(task, page_provider=AsyncMock(), state=state)
        handler = {t.name: t.handler for t in tools}[tool_name]
        link = tool_name == "open_verification_link"

        await handler({})
        assert (await state.block_finish(converted) is not None) is link
        assert await state.block_finish("completed") is None
        held = await state.block_finish("failed")
        assert (held is not None) is link
        if link:
            assert "open_verification_link" in held and "do not act on the page" in held
        await handler({})
        assert (await state.block_finish("completed") is not None) is link
        assert await state.block_finish("failed") is None
        state.record_delivery(tool_name)
        assert await state.block_finish("completed") is None and await state.block_finish(converted) is None


@pytest.mark.parametrize("awaiting", [True, False], ids=["awaiting", "not_awaiting"])
@pytest.mark.parametrize("armed", [True, False], ids=["source_failed", "source_healthy"])
@pytest.mark.parametrize("stale", [True, False], ids=["stale", "recent"])
@pytest.mark.parametrize("spent", [True, False], ids=["budget_spent", "budget_left"])
@pytest.mark.asyncio
async def test_a_give_up_is_held_exactly_while_a_sign_in_link_it_has_budget_for_is_awaited(
    awaiting: bool, armed: bool, stale: bool, spent: bool
) -> None:
    state = auth_tools.VerificationState(task=_task())
    state.budget_seconds = 900.0
    state.polling_spent_seconds = 900.0 - (auth_tools._MIN_SLICE_SECONDS / 2 if spent else 600.0)
    if awaiting:
        state.link_wait_since = time.monotonic() - (auth_tools._NOT_YET_RECENCY_SECONDS + 1 if stale else 0.0)
    if armed:
        state.arm(auth_tools.VerificationFailure.NO_LINK_TWICE, "open_verification_link")

    held = await state.block_finish("failed")

    assert (held is not None) is (awaiting and not armed and not stale and not spent)


@pytest.mark.asyncio
async def test_a_code_call_after_a_link_wait_releases_the_give_up_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    monkeypatch.setattr(
        auth_tools, "resolve_otp_value", AsyncMock(side_effect=[None, NoTOTPVerificationCodeFound(task_id="tsk_1")])
    )
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, page_provider=AsyncMock(), state=state)
    handlers = {t.name: t.handler for t in tools}

    await handlers["open_verification_link"]({})
    assert state.link_wait_since is not None
    await handlers["get_verification_code"]({})

    assert state.link_wait_since is None
    assert await state.block_finish("terminated") is None


def test_the_give_up_headroom_reservation_still_covers_a_whole_poll_slice() -> None:
    # loop.py cannot import auth_tools (auth_tools imports loop), so the relation is pinned here.
    assert taskv3_loop.VERIFICATION_GIVEUP_MIN_DEADLINE_HEADROOM_SECONDS == (
        auth_tools._PER_CALL_WAIT_SECONDS + taskv3_loop.FAILURE_EVIDENCE_MIN_DEADLINE_HEADROOM_SECONDS
    )


@pytest.mark.asyncio
async def test_a_value_that_arrives_but_is_not_delivered_stops_the_link_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=10))
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    state.awaiting_link_since = time.monotonic()
    monkeypatch.setattr(
        auth_tools,
        "resolve_otp_value",
        AsyncMock(return_value=OTPValue(value="https://example.test/magic?token=abc", type=OTPType.MAGIC_LINK)),
    )
    tools, _ = auth_tools.build_auth_tools(task, page_provider=AsyncMock(), state=state)

    result = await next(t for t in tools if t.name == "get_verification_code").handler({})

    assert result.content == auth_tools._MAGIC_LINK_REDIRECT and state.values_delivered == 0
    assert state.awaiting_link_since is None
    assert await state.block_finish(taskv3_loop.CONVERSION_VERIFICATION_STATUS) is None


@pytest.mark.asyncio
async def test_the_spent_budget_answer_names_the_link_tool_on_a_link_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=20 / 60))
    clock = [0.0]
    monkeypatch.setattr(auth_tools, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)

    async def _spends_12s(*_a: Any, **_k: Any) -> OTPValue | None:
        clock[0] += 12.0
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _spends_12s)
    task = _task(totp_verification_url="https://totp.example")
    state = auth_tools.VerificationState(task=task)
    tools, _ = auth_tools.build_auth_tools(task, page_provider=AsyncMock(), state=state)

    result = await next(t for t in tools if t.name == "open_verification_link").handler({})

    assert "no sign-in link available yet" in result.content
    assert "Do not call open_verification_link again" in result.content
    assert "get_verification_code" not in result.content
    assert state.link_failed is True
