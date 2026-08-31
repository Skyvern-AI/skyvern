"""Unit tests for the Task V3 auth tools (verification-code handling)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from skyvern.exceptions import (
    BlockedHost,
    FailedToGetTOTPVerificationCode,
    InvalidUrl,
    NoTOTPVerificationCodeFound,
    UnresolvableHost,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.totp_codes import OTPType
from skyvern.forge.sdk.workflow import context_manager as cm
from skyvern.forge.sdk.workflow.context_manager import WorkflowContextManager, WorkflowRunContext
from skyvern.forge.sdk.workflow.models.parameter import CredentialParameter
from skyvern.forge.taskv3 import auth_tools
from skyvern.forge.taskv3.tools import OBSERVE_URL_MAX_CHARS
from skyvern.services import otp_service
from skyvern.services.otp_service import OTPValue
from skyvern.utils.secret_redaction import redact_secrets_from_bytes


def _task(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "task_id": "tsk_1",
        "organization_id": "o_1",
        "workflow_run_id": None,
        "totp_verification_url": None,
        "totp_identifier": None,
        "navigation_payload": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_auth_tools_absent_without_code_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task())
    assert tools == [] and guidance == ""


def test_build_auth_tools_bare_task_no_workflow_lookup() -> None:
    # Unmocked: a bare task (workflow_run_id=None) with no code source returns no tool without any
    # workflow-run-context lookup — has_credential_totp_candidate short-circuits on the falsy run id and
    # never reaches the getter that raises when a context isn't registered.
    tools, guidance = auth_tools.build_auth_tools(_task())
    assert tools == [] and guidance == ""


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
    # The offering condition and the resolver must not desync: the tool is offered exactly when
    # resolve_otp_value yields a TOTP value or polls for one. Each row exercises both sides against
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

    assert (len(tools) == 1) is expected
    assert resolver_has_source is expected
    assert otp_service.has_otp_source(task, expected_otp_type=OTPType.TOTP) is expected


@pytest.mark.asyncio
async def test_get_verification_code_polling_budget_bounds_a_never_answering_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source that never answers must not let the model re-poll for N x the poll window: each call
    # polls for at most the per-call cap and returns "not yet", the cumulative polling is capped at
    # VERIFICATION_CODE_POLLING_TIMEOUT_MINS, and once spent the tool refuses with stop guidance
    # (warning once) without awaiting the resolver again.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=1 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
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

    not_yet = [r for r in results if "available yet" in r.content]
    exhausted = [r for r in results if "budget exhausted" in r.content]
    assert all(r.status == "error" for r in results)
    assert len(not_yet) >= 1 and not_yet[0] is results[0]
    assert len(exhausted) >= 2 and exhausted[-1] is results[-1]
    assert results.index(exhausted[0]) == len(not_yet)
    # Exhaustion refuses before touching the resolver.
    assert resolver_calls == len(not_yet) + 1
    warnings = [e for e in logs if e.get("event") == "task_v3 verification code polling budget exhausted"]
    assert len(warnings) == 1 and warnings[0]["tool"] == "get_verification_code"
    assert warnings[0]["call_count"] == len(not_yet) + 1
    assert all(cap <= 0.05 for cap in caps) and sum(caps) <= 1.0 + 0.05


@pytest.mark.asyncio
async def test_verification_state_blocks_completion_only_when_the_budget_ran_dry_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A budget exhaustion that delivered nothing must block a completed verdict; a code delivered
    # first (even from an otherwise-exhausted source) must not.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=1 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(auth_tools, "_MIN_SLICE_SECONDS", 0.0)

    async def _never_answers(*_a: Any, max_wait_seconds: float, **_k: Any) -> OTPValue | None:
        await asyncio.sleep(max_wait_seconds)
        raise NoTOTPVerificationCodeFound(task_id="tsk_1")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _never_answers)
    state = auth_tools.VerificationState()
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"), state=state)
    handler = tools[0].handler

    result = None
    for _ in range(30):
        result = await handler({})
        if "budget exhausted" in result.content:
            break
    assert result is not None and "budget exhausted" in result.content
    assert await state.block_completion() == auth_tools._COMPLETION_BLOCKED

    delivered_state = auth_tools.VerificationState()
    monkeypatch.setattr(
        auth_tools, "resolve_otp_value", AsyncMock(return_value=OTPValue(value="123456", type=OTPType.TOTP))
    )
    delivered_tools, _ = auth_tools.build_auth_tools(_task(totp_identifier="user@example.com"), state=delivered_state)
    ctx = SkyvernContext(task_id="tsk_1")
    skyvern_context.set(ctx)
    try:
        delivered_result = await delivered_tools[0].handler({})
    finally:
        skyvern_context.reset()
    assert delivered_result.status == "ok"
    delivered_state.source_failed = True
    assert await delivered_state.block_completion() is None


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
async def test_verification_state_blocks_completion_after_a_refused_or_errored_source(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    # Every terminal non-delivery answer must arm the finish gate, not only budget exhaustion: a source
    # that keeps erroring or returning nothing, or hands over a link the browser could not be sent to,
    # has delivered nothing, so a completed verdict after it is the same false completion.
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    state = auth_tools.VerificationState()
    task = _task(totp_verification_url="https://totp.example")
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
        "lookup_error_streak": "lookup failed: RuntimeError repeatedly",
        "no_code_streak": auth_tools._NO_CODE_AVAILABLE,
        "no_link_streak": auth_tools._NO_LINK_AVAILABLE,
        "page_unavailable": auth_tools._PAGE_UNAVAILABLE,
        "link_rejected": "rejected the sign-in link (HTTP 410)",
        "link_refused": auth_tools._LINK_REFUSED,
        "link_unvalidatable": "nothing was signed in",
        "link_unreachable": "nothing was signed in",
        "link_unreachable_while_origin_page_keeps_fetching": "nothing was signed in",
        "webhook_failing_streak": "kept failing (FailedToGetTOTPVerificationCode: HTTP 500)",
    }[case]

    assert await state.block_completion() is None
    result = await handler({})
    assert result.status == "error"
    if case.endswith("_streak"):
        # A single empty answer is a blip, not a verdict on the source; the second in a row is.
        assert "again" in result.content and "trigger it first" not in result.content
        assert await state.block_completion() is None
        result = await handler({})
        assert result.status == "error"
    assert expected in result.content
    assert await state.block_completion() == auth_tools._COMPLETION_BLOCKED


@pytest.mark.asyncio
async def test_verification_state_does_not_block_after_a_retryable_not_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    # "Not yet" and a lone lookup error are retryable, not a source failure: a speculative poll on a
    # page that never ends up asking for a code must not turn a legitimate completion into a failure.
    # A later delivery also disarms a gate an error streak had armed.
    monkeypatch.setattr(
        auth_tools,
        "settings",
        SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=5, BROWSER_LOADING_TIMEOUT_MS=1000),
    )
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
    state = auth_tools.VerificationState()
    task = _task(totp_verification_url="https://totp.example")
    answers: list[Any] = [
        NoTOTPVerificationCodeFound(task_id="tsk_1"),
        RuntimeError("boom"),
        NoTOTPVerificationCodeFound(task_id="tsk_1"),
        RuntimeError("boom"),
        RuntimeError("boom"),
        OTPValue(value="123456", type=OTPType.TOTP),
    ]
    monkeypatch.setattr(auth_tools, "resolve_otp_value", AsyncMock(side_effect=answers))
    tools, _ = auth_tools.build_auth_tools(task, state=state)
    handler = tools[0].handler

    assert "available yet" in (await handler({})).content
    assert await state.block_completion() is None
    assert "lookup failed" in (await handler({})).content
    assert await state.block_completion() is None
    # A healthy answer in between resets the streak.
    assert "available yet" in (await handler({})).content
    assert "lookup failed" in (await handler({})).content
    assert await state.block_completion() is None
    assert "lookup failed" in (await handler({})).content
    assert await state.block_completion() == auth_tools._COMPLETION_BLOCKED
    assert (await handler({})).status == "ok"
    assert await state.block_completion() is None


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
    state = auth_tools.VerificationState()
    tools, _ = auth_tools.build_auth_tools(
        _task(totp_verification_url="https://totp.example"),
        _provider(_FakePage(goto_error=TimeoutError("load"), cookie_on_goto_error=True)),
        state=state,
    )
    handlers = {t.name: t.handler for t in tools}

    assert "lookup failed" in (await handlers["get_verification_code"]({})).content
    assert (await handlers["get_verification_code"]({})).content == auth_tools._MAGIC_LINK_REDIRECT
    assert "failed to open" in (await handlers["open_verification_link"]({})).content
    assert "lookup failed" in (await handlers["get_verification_code"]({})).content
    assert await state.block_completion() is None


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
    state = auth_tools.VerificationState()
    tools, _ = auth_tools.build_auth_tools(
        _task(totp_verification_url="https://totp.example"), _provider(page), state=state
    )
    handler = {t.name: t.handler for t in tools}["open_verification_link"]

    result = await handler({})
    assert result.status == "error" and expected in result.content
    assert await state.block_completion() is None


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
    assert "available yet" in first.content and "http_status=204x3" in first.content
    assert "budget exhausted" in second.content
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
    # A timeout raised inside the resolver (DB, HTTP) reads as a lookup failure, not as this tool's
    # wait cap or budget exhaustion: the first invites a retry, the second in a row is terminal.
    async def _inner_timeout(*_a: Any, **_k: Any) -> OTPValue | None:
        raise TimeoutError("pool acquire")

    monkeypatch.setattr(auth_tools, "resolve_otp_value", _inner_timeout)
    tools, _ = auth_tools.build_auth_tools(_task(totp_verification_url="https://totp.example"))
    first = await tools[0].handler({})
    second = await tools[0].handler({})
    assert "lookup failed: TimeoutError" in first.content and "call get_verification_code again" in first.content
    assert "lookup failed: TimeoutError repeatedly" in second.content


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


def test_build_auth_tools_absent_with_no_code_source_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(_task(navigation_payload={"unrelated_field": "value"}))
    assert tools == [] and guidance == ""


def test_build_auth_tools_absent_with_magic_link_only_payload_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # A payload-embedded URL resolves to a magic link, not a TOTP code; get_verification_code hard-rejects
    # non-TOTP values, so offering the tool here would be guaranteed to error.
    monkeypatch.setattr(otp_service, "has_credential_totp_candidate", lambda *_a, **_k: False)
    tools, guidance = auth_tools.build_auth_tools(
        _task(navigation_payload={"verification_link": "https://example.test/x"})
    )
    assert tools == [] and guidance == ""


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
    # A sign-in link is not a code and this engine cannot follow it: the tool must say so on the first
    # call (not burn the poll budget as "no code yet"), count it with one structured warning, and never
    # let the URL reach the model, the logs, or the secret registry.
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
    assert first.status == "error" and "sign-in link" in first.content and "finish" in first.content
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


@pytest.mark.asyncio
async def test_verification_tools_share_one_polling_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two tools over one verification source must not double the wait a never-answering source can buy.
    monkeypatch.setattr(auth_tools, "settings", SimpleNamespace(VERIFICATION_CODE_POLLING_TIMEOUT_MINS=1 / 60))
    monkeypatch.setattr(auth_tools, "_PER_CALL_WAIT_SECONDS", 0.05)
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
    assert code_result.status == "error" and "budget exhausted" in code_result.content
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
