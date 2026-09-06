"""Tests for non-retriable navigation error handling in the copilot layer.

Covers SKY-9136: when the browser layer raises ``FailedToNavigateToUrl`` with
a DNS / cert / SSL / invalid-URL pattern (``SKIP_INNER_NAV_RETRY_ERRORS``),
the copilot must surface the real error instead of "Unknown error", must not
keep retrying, and must fail deterministically even if the model tries to
narrate a completion.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.enforcement import (
    CopilotNonRetriableNavError,
    _extract_url_from_nav_error,
    _maybe_raise_non_retriable_nav,
)
from skyvern.forge.sdk.copilot.tools import (
    _detect_non_retriable_nav_error,
    _record_run_blocks_result,
    _record_workflow_update_result,
)
from skyvern.schemas.runs import ProxyLocation

_DNS_FAILURE_REASON = (
    "Failed to navigate to url https://www.example.invalid/path. Error message: net::ERR_NAME_NOT_RESOLVED"
)
_CERT_FAILURE_REASON = "Failed to navigate to url https://expired.example. Error message: net::ERR_CERT_DATE_INVALID"
_TUNNEL_FAILURE_REASON = (
    "Failed to navigate to url https://proxy.example. Error message: net::ERR_TUNNEL_CONNECTION_FAILED"
)
_SOCKS_FAILURE_REASON = (
    "Failed to navigate to url https://www.example.test/. Error message: net::ERR_SOCKS_CONNECTION_FAILED"
)
_SOCKS_HOST_UNREACHABLE_REASON = (
    "Failed to navigate to url https://host.example. Error message: net::ERR_SOCKS_CONNECTION_HOST_UNREACHABLE"
)
_DNS_FAILURE_WITH_PROXY_TOKEN_IN_URL = (
    "Failed to navigate to url https://example.test/net::ERR_TUNNEL_CONNECTION_FAILED. "
    "Error message: net::ERR_NAME_NOT_RESOLVED"
)
_CERT_FAILURE_WITH_PROXY_TOKEN_IN_URL = (
    "Failed to navigate to url https://example.test/net::ERR_SOCKS_CONNECTION_FAILED. "
    "Error message: net::ERR_CERT_DATE_INVALID"
)
_GENERIC_FAILURE_REASON = "Timeout waiting for element #submit"

_PROXY_FAILURE_REPLY = (
    "The site could not be reached through Skyvern's browser network. Error: {reason}. "
    "Please try again later, or contact Skyvern Support if the problem continues."
)
_NON_PROXY_FAILURE_REPLY = "The target URL could not be reached. Error: {reason}. Please verify the URL and try again."


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _record_and_render_terminal_reply(reason: str, *, block_type: str) -> tuple[CopilotContext, AgentResult]:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "blocks": [
                    {
                        "label": "failed_navigation",
                        "block_type": block_type,
                        "status": "failed",
                        "failure_reason": reason,
                    }
                ]
            },
        },
    )
    with pytest.raises(CopilotNonRetriableNavError) as excinfo:
        _maybe_raise_non_retriable_nav(ctx)
    result = agent_module._build_non_retriable_nav_exit_result(ctx, None, excinfo.value)
    return ctx, result


# ---------------------------------------------------------------------------
# _detect_non_retriable_nav_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(_DNS_FAILURE_REASON, id="dns_standard_format"),
        pytest.param(
            "Failed to navigate to url not-a-url. Error message: net::ERR_INVALID_URL",
            id="invalid_url_standard_format",
        ),
        pytest.param("net::ERR_NAME_RESOLUTION_FAILED happened mid-flight", id="name_resolution_mid_string"),
        pytest.param("SSL error: net::ERR_SSL_PROTOCOL_ERROR", id="ssl_prefixed"),
        pytest.param(_TUNNEL_FAILURE_REASON, id="tunnel_connection_failed"),
        pytest.param(_SOCKS_FAILURE_REASON, id="socks_connection_failed"),
        pytest.param(
            "Failed to navigate to url https://x.test. Error message: net::ERR_SOCKS_CONNECTION_HOST_UNREACHABLE",
            id="socks_host_unreachable",
        ),
    ],
)
def test_detect_matches_error_in_block_failure_reason(reason: str) -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason}]}}
    assert _detect_non_retriable_nav_error(result) == reason


def test_detect_matches_cert_error_in_run_level_failure_reason() -> None:
    result = {"ok": False, "data": {"failure_reason": _CERT_FAILURE_REASON, "blocks": []}}
    assert _detect_non_retriable_nav_error(result) == _CERT_FAILURE_REASON


def test_detect_returns_none_for_generic_failure() -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]}}
    assert _detect_non_retriable_nav_error(result) is None


def test_detect_returns_none_for_missing_data() -> None:
    assert _detect_non_retriable_nav_error({"ok": False}) is None


def test_detect_returns_none_for_empty_blocks() -> None:
    assert _detect_non_retriable_nav_error({"ok": False, "data": {"blocks": []}}) is None


def test_detect_prefers_run_level_over_block_level() -> None:
    # When both match, the run-level reason wins (it is the authoritative
    # aggregate failure the workflow service recorded).
    result = {
        "ok": False,
        "data": {
            "failure_reason": _DNS_FAILURE_REASON,
            "blocks": [{"failure_reason": _CERT_FAILURE_REASON}],
        },
    }
    assert _detect_non_retriable_nav_error(result) == _DNS_FAILURE_REASON


# ---------------------------------------------------------------------------
# _record_run_blocks_result — context flag plumbing
# ---------------------------------------------------------------------------


def test_record_sets_flag_on_dns_failure() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error == _DNS_FAILURE_REASON
    assert ctx.last_test_ok is False


def test_record_leaves_flag_none_for_generic_failure() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None


def test_record_clears_flag_on_every_call() -> None:
    # Stale state from a prior run must not leak into the next run's context.
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_non_retriable_nav_error = "stale DNS error"
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": _GENERIC_FAILURE_REASON}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None


def test_workflow_update_clears_non_retriable_flag() -> None:
    # Codex review P2-2: after a DNS-failed run, if the agent edits the
    # workflow (e.g. fixing the URL), the stale flag must not survive the
    # edit — otherwise an exhausted POST_UPDATE_NUDGE on the new draft
    # would raise CopilotNonRetriableNavError with the OLD run's error
    # message, telling the user to verify a URL they just corrected.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False
    ctx.workflow_yaml = "updated yaml"

    _record_workflow_update_result(
        ctx,
        {
            "ok": True,
            "data": {"block_count": 2},
            "_workflow": SimpleNamespace(workflow_id="wf_new"),
        },
    )
    assert ctx.last_test_non_retriable_nav_error is None
    # Consistency check: the other per-test fields are also reset (pre-existing behavior).
    assert ctx.last_test_ok is None
    assert ctx.last_test_failure_reason is None


@pytest.mark.parametrize(
    ("rollout_enabled", "workflow_proxy_location", "expected_proxy_location"),
    [
        (False, None, ProxyLocation.RESIDENTIAL),
        (True, None, ProxyLocation.NONE),
        (False, ProxyLocation.RESIDENTIAL_GB, ProxyLocation.RESIDENTIAL_GB),
        (False, ProxyLocation.NONE, ProxyLocation.NONE),
    ],
)
def test_workflow_update_records_runtime_proxy_default(
    monkeypatch: pytest.MonkeyPatch,
    rollout_enabled: bool,
    workflow_proxy_location: ProxyLocation | None,
    expected_proxy_location: ProxyLocation,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", rollout_enabled)
    ctx = _fresh_context()

    _record_workflow_update_result(
        ctx,
        {
            "ok": True,
            "data": {"block_count": 1},
            "_workflow": SimpleNamespace(workflow_id="wf_new", proxy_location=workflow_proxy_location),
        },
    )

    assert ctx.effective_workflow_proxy_location == expected_proxy_location


def test_workflow_update_does_not_clear_flag_on_failed_update() -> None:
    # Only a SUCCESSFUL update invalidates prior test state — a failed
    # validation attempt leaves the existing flags alone.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False

    _record_workflow_update_result(
        ctx,
        {"ok": False, "error": "validation failed"},
    )
    assert ctx.last_test_non_retriable_nav_error == _DNS_FAILURE_REASON


# ---------------------------------------------------------------------------
# _extract_url_from_nav_error
# ---------------------------------------------------------------------------


def test_extract_url_parses_standard_format() -> None:
    url = _extract_url_from_nav_error(_DNS_FAILURE_REASON)
    assert url == "https://www.example.invalid/path"


def test_extract_url_returns_none_on_malformed_message() -> None:
    assert _extract_url_from_nav_error("some unrelated error text") is None


# ---------------------------------------------------------------------------
# _maybe_raise_non_retriable_nav — deterministic exit-path
# ---------------------------------------------------------------------------


def test_maybe_raise_noops_when_flag_unset() -> None:
    ctx = _fresh_context()
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


def test_maybe_raise_noops_when_last_test_is_ok() -> None:
    # A prior successful run does NOT suppress the exception (per CORR-3),
    # but the MOST RECENT run being a real success does — because that
    # means this turn's test did not hit the non-retriable path.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = True
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


def test_maybe_raise_raises_when_flag_and_last_test_failed() -> None:
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False
    with pytest.raises(CopilotNonRetriableNavError) as excinfo:
        _maybe_raise_non_retriable_nav(ctx)
    assert excinfo.value.error_message == _DNS_FAILURE_REASON
    assert excinfo.value.url == "https://www.example.invalid/path"


def test_maybe_raise_raises_when_last_test_ok_is_none() -> None:
    # The guard is ``last_test_ok is not True`` (not ``is False``), so an
    # ambiguous None (e.g. a suspicious-success run) with the flag set still
    # surfaces the cached nav failure rather than letting the loop return
    # silently. Locks in the tri-state semantics.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = None
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def test_maybe_raise_raises_when_prior_run_succeeded_but_current_failed() -> None:
    # Codex CORR-3: the guard must NOT be gated on session history. A fresh
    # non-retriable nav failure on the most recent run still raises, even if
    # an earlier run in the same session succeeded.
    ctx = _fresh_context()
    ctx.last_test_non_retriable_nav_error = _DNS_FAILURE_REASON
    ctx.last_test_ok = False  # most recent run
    # Simulate a prior successful run in the session — there is no
    # `any_test_ok_ever` flag; the helper only inspects current state.
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


# ---------------------------------------------------------------------------
# Sanity: exception carries the expected attributes for the agent handler
# ---------------------------------------------------------------------------


def test_exception_carries_url_and_error_message() -> None:
    exc = CopilotNonRetriableNavError(url="https://x.test", error_message="some reason")
    assert exc.url == "https://x.test"
    assert exc.error_message == "some reason"
    assert "some reason" in str(exc)


# ---------------------------------------------------------------------------
# Sanity: when flag is set, no failure-nudge branch competes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Integration-ish: record -> check -> exception flow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(_DNS_FAILURE_REASON, id="dns"),
        pytest.param(_SOCKS_FAILURE_REASON, id="socks_connection_failed"),
    ],
)
def test_full_flow_record_then_check_then_raise(reason: str) -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": reason}]},
        },
    )
    assert ctx.last_test_non_retriable_nav_error == reason
    # The exit-path guard raises because last_test_ok is still False.
    with pytest.raises(CopilotNonRetriableNavError):
        _maybe_raise_non_retriable_nav(ctx)


def test_full_flow_cleared_after_successful_run() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {"ok": False, "data": {"blocks": [{"failure_reason": _DNS_FAILURE_REASON}]}},
    )
    # Then a real success happens.
    _record_run_blocks_result(
        ctx,
        {
            "ok": True,
            "data": {
                "blocks": [
                    {
                        "label": "extract",
                        "block_type": "extraction",
                        "status": "completed",
                        "extracted_data": [{"x": 1}],
                    }
                ],
            },
        },
    )
    # Last-test fields now reflect success; the exit-path guard does nothing.
    _maybe_raise_non_retriable_nav(ctx)  # must not raise


@pytest.mark.parametrize(
    ("reason", "block_type"),
    [
        pytest.param(_SOCKS_FAILURE_REASON, "navigation", id="navigation_tool_socks"),
        pytest.param(_TUNNEL_FAILURE_REASON, "code", id="secure_runner_goto_tunnel"),
        pytest.param(_SOCKS_HOST_UNREACHABLE_REASON, "code", id="secure_runner_goto_host_unreachable"),
    ],
)
def test_proxy_transport_full_flow_uses_browser_network_reply(reason: str, block_type: str) -> None:
    ctx, result = _record_and_render_terminal_reply(reason, block_type=block_type)

    assert result.user_response == _PROXY_FAILURE_REPLY.format(reason=reason)
    assert "verify the URL" not in result.user_response
    assert result.updated_workflow is None
    assert result.workflow_yaml is None
    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "non_retriable_nav"
    assert result.terminal_envelope is not None
    assert result.terminal_envelope["next_state"] == "stopped"
    assert result.terminal_envelope["response_kind"] == "stopped"
    assert result.terminal_envelope["proposal_present"] is False
    assert ctx.last_test_non_retriable_nav_error == reason


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(_DNS_FAILURE_REASON, id="dns"),
        pytest.param(_CERT_FAILURE_REASON, id="certificate"),
        pytest.param(_DNS_FAILURE_WITH_PROXY_TOKEN_IN_URL, id="dns_with_proxy_token_in_url"),
        pytest.param(_CERT_FAILURE_WITH_PROXY_TOKEN_IN_URL, id="certificate_with_proxy_token_in_url"),
    ],
)
def test_non_proxy_full_flow_preserves_existing_reply(reason: str) -> None:
    _, result = _record_and_render_terminal_reply(reason, block_type="navigation")

    assert result.user_response == _NON_PROXY_FAILURE_REPLY.format(reason=reason)
    assert result.updated_workflow is None
    assert result.workflow_yaml is None
    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "non_retriable_nav"


# ---------------------------------------------------------------------------
# Within-turn fail-fast guard — _tool_loop_error
# ---------------------------------------------------------------------------
