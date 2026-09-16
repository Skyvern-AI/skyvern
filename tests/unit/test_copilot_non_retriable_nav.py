"""Tests for non-retriable navigation error handling in the copilot layer.

A failure blaming the target (DNS / cert / SSL / invalid URL) must surface the real error
instead of "Unknown error", must not keep retrying, and must fail deterministically even if
the model tries to narrate a completion. A failure inside Skyvern's own proxy hop must not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock

import pytest

import skyvern.forge.sdk.workflow.models.block as block_module
from skyvern.config import settings
from skyvern.constants import SKIP_INNER_NAV_RETRY_ERRORS
from skyvern.exceptions import (
    NO_ADDRESS_RECORD_NAV_ERROR_CODE,
    FailedToNavigateToUrl,
    WorkflowRunContextNotInitialized,
)
from skyvern.forge.sdk.copilot import agent as agent_module
from skyvern.forge.sdk.copilot.context import AgentResult, CopilotContext
from skyvern.forge.sdk.copilot.diagnosis_repair_contract import (
    DiagnosisFailureType,
    RepairNextAction,
    _next_action,
    author_time_levers,
)
from skyvern.forge.sdk.copilot.enforcement import (
    CopilotNonRetriableNavError,
    _extract_url_from_nav_error,
    _maybe_raise_non_retriable_nav,
    proxy_hop_failure_reason,
)
from skyvern.forge.sdk.copilot.nav_attribution import (
    PROXY_TRANSPORT_NAV_ERROR_CODES,
    TERMINAL_NAV_ERROR_CODES,
    block_nav_error_codes,
    proxy_owns_nav_codes,
)
from skyvern.forge.sdk.copilot.output_utils import BUILD_TEST_PACKET_KEY
from skyvern.forge.sdk.copilot.run_outcome import _DISPLAY_REASON_MAX_CHARS
from skyvern.forge.sdk.copilot.runtime import mcp_to_copilot
from skyvern.forge.sdk.copilot.runtime_authoring_repair import _error_text_requires_stop
from skyvern.forge.sdk.copilot.secret_scrub import register_secret_scrub_value
from skyvern.forge.sdk.copilot.tools import (
    _detect_non_retriable_nav_error,
    _record_run_blocks_result,
    _record_workflow_update_result,
)
from skyvern.forge.sdk.copilot.tools import _shared as shared_module
from skyvern.forge.sdk.copilot.tools import mcp_hooks as mcp_hooks_module
from skyvern.forge.sdk.copilot.tools._shared import _discovery_navigate
from skyvern.forge.sdk.copilot.tools.mcp_hooks import _navigate_post_hook
from skyvern.forge.sdk.copilot.tools.run_execution import (
    _commit_run_blocks_record,
    _run_blocks_and_collect_debug,
    finalize_build_test_result,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.forge.sdk.schemas.tasks import TaskStatus
from skyvern.forge.sdk.schemas.workflow_runs import WorkflowRunBlock
from skyvern.schemas.proxy_location import GeoTarget, ProxyLocationInput
from skyvern.schemas.runs import ProxyLocation
from skyvern.schemas.workflows import BlockType
from skyvern.webeye.navigation import driver_nav_error_code
from tests.unit.copilot_test_helpers import install_run_blocks_harness, make_copilot_ctx
from tests.unit.force_stub_app import admit_block_dispatch
from tests.unit.test_missing_starter_url import _mock_block_execute_deps, _output_parameter

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
# The task-level failure_reason skyvern/forge/agent.py builds from FailedToNavigateToUrl. Its
# separator differs from the exception's own, and it is the reason a TaskBlock reports.
_TASK_FAILURE_REASON_TUNNEL = (
    "Failed to navigate to URL. URL:https://proxy.example, Error:net::ERR_TUNNEL_CONNECTION_FAILED"
)
_TASK_FAILURE_REASON_DNS = (
    "Failed to navigate to URL. URL:https://www.example.invalid, Error:net::ERR_NAME_NOT_RESOLVED"
)
# A wrapped navigation failure quotes an inner one; the code the outer producer reports is the one it
# ends on, and a proxy code planted in the quoted URL is not the machine's verdict.
_WRAPPED_FAILURE_QUOTING_A_PROXY_CODE_EARLIER = (
    "Failed to navigate to url https://a.test. Error message: "
    "Failed to navigate to url https://b.test/net::ERR_TUNNEL_CONNECTION_FAILED. "
    "Error message: net::ERR_ABORTED"
)

_DRIVER_CODE_BY_REASON = {
    _TUNNEL_FAILURE_REASON: "net::ERR_TUNNEL_CONNECTION_FAILED",
    _SOCKS_FAILURE_REASON: "net::ERR_SOCKS_CONNECTION_FAILED",
    _SOCKS_HOST_UNREACHABLE_REASON: "net::ERR_SOCKS_CONNECTION_HOST_UNREACHABLE",
    _DNS_FAILURE_REASON: "net::ERR_NAME_NOT_RESOLVED",
    _CERT_FAILURE_REASON: "net::ERR_CERT_DATE_INVALID",
    # The planted token is in the requested URL; the driver's verdict is the code it ended on.
    _DNS_FAILURE_WITH_PROXY_TOKEN_IN_URL: "net::ERR_NAME_NOT_RESOLVED",
    _CERT_FAILURE_WITH_PROXY_TOKEN_IN_URL: "net::ERR_CERT_DATE_INVALID",
    _TASK_FAILURE_REASON_TUNNEL: "net::ERR_TUNNEL_CONNECTION_FAILED",
    _TASK_FAILURE_REASON_DNS: "net::ERR_NAME_NOT_RESOLVED",
}


_TARGET_CODE_PLANTED_IN_THE_URL = (
    "Failed to navigate to url https://example.test/net::ERR_NAME_NOT_RESOLVED. "
    "Error message: net::ERR_TUNNEL_CONNECTION_FAILED"
)


def _proxy_hop_label(proxy_location: ProxyLocation | str | None) -> str:
    if not proxy_location:
        return "Skyvern proxy hop failed: "
    name = proxy_location.value if isinstance(proxy_location, ProxyLocation) else proxy_location
    return f"Skyvern proxy hop failed (proxy_location={name}): "


_SHARED_FAILURE_REASON = "The step did not complete."


def _driver_codes_for(reason: str) -> list[str]:
    """The code the driver would have reported for a fixture reason, and nothing for the rest."""
    code = _DRIVER_CODE_BY_REASON.get(reason)
    return [code] if code else []


_GENERIC_FAILURE_REASON = "Timeout waiting for element #submit"
# A page can put this text in front of the model, and the model's finish text becomes the run's
# failure_reason, so prose quoting the code must never mint blame on our own egress.
_MODEL_PROSE_QUOTING_A_PROXY_CODE = (
    "The page reported net::ERR_TUNNEL_CONNECTION_FAILED, so I stopped before filling the form."
)
# Every separator a producer uses is ordinary punctuation a page or a model also writes, so prose
# carrying one must not mint the attribution at any site that reads the reason.
_PROSE_QUOTING_A_TASK_SEPARATOR = "The site said: Connection error, Error: net::ERR_TUNNEL_CONNECTION_FAILED"
_PROSE_QUOTING_A_GOTO_SEPARATOR = (
    'I stopped because the page rendered the text "Page.goto: net::ERR_SOCKS_CONNECTION_FAILED"'
)
_PROSE_QUOTING_A_NAV_SEPARATOR = "Terminated: the banner read. Error message: net::ERR_TUNNEL_CONNECTION_FAILED"
# A task's failure_reason can be model-authored prose verbatim (skyvern/forge/agent.py assigns
# persisted_action.reasoning to it), so a bare code carries no provenance: a page that renders one
# must not be able to blame Skyvern's egress and suppress the authoring repair the block deserves.
_BARE_PROXY_CODE_WITHOUT_A_PRODUCER = "net::ERR_TUNNEL_CONNECTION_FAILED"
_PROSE_QUOTING_THE_ATTRIBUTION_PREFIX = (
    "Skyvern proxy hop failed (proxy_location=US): the page said net::ERR_TUNNEL_CONNECTION_FAILED"
)
# The reply site reads a reason the run seam already attributed, so the prefix is a producer wrapper.
_ALREADY_ATTRIBUTED_TUNNEL_FAILURE = f"Skyvern proxy hop failed (proxy_location=US): {_TUNNEL_FAILURE_REASON}"

# The proxy code leads and the target-owned code trails past both display clamps (240 in the chat
# reply, 160 in the run observation), so a predicate reading clamped text would mislabel this.
_MIXED_CODE_TARGET_CODE_PAST_THE_CLAMPS = (
    "Failed to navigate to url https://proxy.example. Error message: net::ERR_TUNNEL_CONNECTION_FAILED "
    + "retrying the connect hop " * 12
    + "net::ERR_CERT_DATE_INVALID"
)

# A long path pushes the proxy code past the clamp while the origin rewrite pulls it back inside, so
# a predicate reading clamped text would drop a label the displayed sentence still has room for.
_PROXY_CODE_PAST_THE_CLAMP = (
    "Failed to navigate to url "
    "https://shop.example/catalog/category/outdoor/tents/four-season/filters?sort=price&page=12&session=abcdef. "
    "Error message: net::ERR_TUNNEL_CONNECTION_FAILED"
)

_PROXY_RUN_WORKFLOW_YAML = """
title: proxy run example
workflow_definition:
  parameters: []
  blocks:
    - block_type: navigation
      label: open_page
      url: https://proxy.example
      navigation_goal: open the page
"""

_NON_PROXY_FAILURE_REPLY = "The target URL could not be reached. Error: {reason}. Please verify the URL and try again."
# A host with no address record still carries the proxy's error code, because a proxied browser
# delegates resolution to the proxy. UnresolvableNavigationHost prefixes the real cause.
_NO_ADDRESS_RECORD_FAILURE_REASON = (
    "Failed to navigate to url https://gone.example/. Error message: gone.example has no DNS "
    "address record: net::ERR_TUNNEL_CONNECTION_FAILED"
)
_NO_ADDRESS_RECORD_REPLY = (
    "The site's domain has no DNS record, so it cannot be reached from any network. "
    "Error: {reason}. Please check the URL for a typo, or confirm the site is still online."
)


def _fresh_context() -> CopilotContext:
    return CopilotContext(
        organization_id="o",
        workflow_id="w",
        workflow_permanent_id="wp",
        workflow_yaml="",
        browser_session_id=None,
        stream=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _record_and_render_terminal_reply(
    reason: str, *, block_type: str, codes: list[str] | None = None
) -> tuple[CopilotContext, AgentResult]:
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
                        "error_codes": _driver_codes_for(reason) if codes is None else codes,
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


# Each arm declares the code its driver reported. Deriving it from the reason text here would
# reproduce the step this module exists to remove, and the test would pass either way.
_TARGET_OWNED_ARMS = [
    pytest.param(_DNS_FAILURE_REASON, "net::ERR_NAME_NOT_RESOLVED", id="dns_standard_format"),
    pytest.param(
        "Failed to navigate to url not-a-url. Error message: net::ERR_INVALID_URL",
        "net::ERR_INVALID_URL",
        id="invalid_url_standard_format",
    ),
    pytest.param(
        "net::ERR_NAME_RESOLUTION_FAILED happened mid-flight",
        "net::ERR_NAME_RESOLUTION_FAILED",
        id="name_resolution_mid_string",
    ),
    pytest.param("SSL error: net::ERR_SSL_PROTOCOL_ERROR", "net::ERR_SSL_PROTOCOL_ERROR", id="ssl_prefixed"),
    pytest.param(_DNS_FAILURE_WITH_PROXY_TOKEN_IN_URL, "net::ERR_NAME_NOT_RESOLVED", id="dns_alongside_proxy_code"),
    pytest.param(
        "net::ERR_SOCKS_CONNECTION_FAILED then net::ERR_CERT_DATE_INVALID on retry",
        "net::ERR_CERT_DATE_INVALID",
        id="proxy_code_alongside_cert",
    ),
]


@pytest.mark.parametrize(("reason", "code"), _TARGET_OWNED_ARMS)
def test_detect_matches_error_in_block_failure_reason(reason: str, code: str) -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason, "error_codes": [code]}]}}
    assert _detect_non_retriable_nav_error(result) == reason


@pytest.mark.parametrize(("reason", "code"), _TARGET_OWNED_ARMS)
def test_a_failure_sentence_alone_cannot_mint_a_terminal_stop(reason: str, code: str) -> None:
    """The same sentences, with no code from the driver behind them.

    A terminal stop discards the user's staged workflow and tells them to check a URL, so a page or
    a model that reproduces this wording must not be able to trigger one.
    """
    del code
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason}]}}
    assert _detect_non_retriable_nav_error(result) is None


def test_detect_matches_cert_error_in_run_level_failure_reason() -> None:
    result = {
        "ok": False,
        "data": {
            "failure_reason": _CERT_FAILURE_REASON,
            "error_codes": ["net::ERR_CERT_DATE_INVALID"],
            "blocks": [],
        },
    }
    assert _detect_non_retriable_nav_error(result) == _CERT_FAILURE_REASON


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(_GENERIC_FAILURE_REASON, id="generic"),
        pytest.param(_TUNNEL_FAILURE_REASON, id="tunnel_connection_failed"),
        pytest.param(_SOCKS_FAILURE_REASON, id="socks_connection_failed"),
        pytest.param(_SOCKS_HOST_UNREACHABLE_REASON, id="socks_host_unreachable"),
    ],
)
def test_detect_returns_none_for_generic_failure(reason: str) -> None:
    result = {"ok": False, "data": {"blocks": [{"failure_reason": reason}]}}
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
            "error_codes": ["net::ERR_NAME_NOT_RESOLVED"],
            "blocks": [{"failure_reason": _CERT_FAILURE_REASON, "error_codes": ["net::ERR_CERT_DATE_INVALID"]}],
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
            "data": {
                "blocks": [{"failure_reason": _DNS_FAILURE_REASON, "error_codes": ["net::ERR_NAME_NOT_RESOLVED"]}]
            },
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
    ("reason", "code"),
    [
        pytest.param(_DNS_FAILURE_REASON, "net::ERR_NAME_NOT_RESOLVED", id="dns"),
        pytest.param(_CERT_FAILURE_REASON, "net::ERR_CERT_DATE_INVALID", id="certificate"),
    ],
)
def test_full_flow_record_then_check_then_raise(reason: str, code: str) -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {"blocks": [{"failure_reason": reason, "error_codes": [code]}]},
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


def test_proxy_codes_are_subtracted_from_the_browser_skip_set() -> None:
    """Renaming one of these in skyvern/constants.py would silently make the proxy family terminal again,
    or drop a target-owned code out of the stop set entirely."""
    assert set(PROXY_TRANSPORT_NAV_ERROR_CODES) <= set(SKIP_INNER_NAV_RETRY_ERRORS)
    assert set(TERMINAL_NAV_ERROR_CODES).isdisjoint(PROXY_TRANSPORT_NAV_ERROR_CODES)
    assert set(TERMINAL_NAV_ERROR_CODES) | set(PROXY_TRANSPORT_NAV_ERROR_CODES) == set(SKIP_INNER_NAV_RETRY_ERRORS)


@pytest.mark.parametrize(
    ("codes", "proxy_owned"),
    [
        pytest.param(["net::ERR_TUNNEL_CONNECTION_FAILED"], True, id="driver_reported_a_proxy_code"),
        pytest.param(["net::ERR_SOCKS_CONNECTION_FAILED"], True, id="driver_reported_socks"),
        pytest.param(["net::ERR_NAME_NOT_RESOLVED"], False, id="driver_reported_a_target_code"),
        pytest.param(
            ["net::ERR_TUNNEL_CONNECTION_FAILED", "net::ERR_CERT_DATE_INVALID"],
            False,
            id="a_target_code_alongside_disqualifies",
        ),
        pytest.param(["FILE_PARSER_ERROR"], False, id="an_unrelated_block_code"),
        pytest.param([], False, id="the_driver_reported_nothing"),
    ],
)
def test_ownership_comes_from_the_codes_the_driver_reported(codes: list[str], proxy_owned: bool) -> None:
    assert proxy_owns_nav_codes(codes) is proxy_owned


@pytest.mark.parametrize(
    ("reason", "block_type"),
    [
        pytest.param(_SOCKS_FAILURE_REASON, "navigation", id="navigation_tool_socks"),
        pytest.param(_TUNNEL_FAILURE_REASON, "code", id="secure_runner_goto_tunnel"),
        pytest.param(_SOCKS_HOST_UNREACHABLE_REASON, "code", id="secure_runner_goto_host_unreachable"),
    ],
)
def test_proxy_transport_run_keeps_proposal_and_reaches_repair(reason: str, block_type: str) -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.effective_workflow_proxy_location = ProxyLocation.RESIDENTIAL_ES
    ctx.has_staged_proposal = True
    ctx.staged_workflow_yaml = "title: staged"
    outcome = _record_run_blocks_result(
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
                        "error_codes": _driver_codes_for(reason),
                    }
                ]
            },
        },
    )

    assert ctx.last_test_non_retriable_nav_error is None
    _maybe_raise_non_retriable_nav(ctx)
    assert _next_action(DiagnosisFailureType.REPAIRABLE_BLOCK_FAILURE, ctx, {}, None) is RepairNextAction.REPAIR
    # The turn stays repairable, but the block is not what broke: authoring repair must not rewrite
    # working code to chase a fault in Skyvern's own egress.
    assert _error_text_requires_stop(ctx, {"failure_reason": reason}) is True
    assert ctx.has_staged_proposal is True
    assert ctx.staged_workflow_yaml == "title: staged"
    assert ctx.last_test_ok is False
    assert ctx.last_full_workflow_test_ok is False
    assert ctx.verified_terminal_proposal_ready is False

    result = agent_module._make_agent_result(
        ctx,
        narrative_payload={},
        user_response="Skyvern's proxy hop failed; re-testing on another node.",
        updated_workflow=SimpleNamespace(workflow_id="wf"),
        workflow_yaml="title: staged",
    )
    assert result.updated_workflow is not None
    assert result.proposal_disposition == "review_untested"

    proxy_prefix = "Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): "
    assert outcome.display_reason is not None
    assert outcome.display_reason.startswith(proxy_prefix)
    assert len(outcome.display_reason) <= _DISPLAY_REASON_MAX_CHARS
    error_code = reason.rsplit(" ", 1)[-1]
    assert error_code.startswith("net::ERR_")
    assert error_code in outcome.display_reason
    assert "verify the URL" not in outcome.display_reason


@pytest.mark.parametrize(
    ("blocks", "run_codes", "connect_failure_reason", "proxy_location", "labelled", "displayed", "stop"),
    [
        pytest.param(
            [{"failure_reason": _GENERIC_FAILURE_REASON}],
            ["net::ERR_TUNNEL_CONNECTION_FAILED"],
            _TUNNEL_FAILURE_REASON,
            ProxyLocation.RESIDENTIAL_ES,
            True,
            None,
            None,
            id="a_connect_failure_outside_any_block_carries_the_runs_code",
        ),
        pytest.param(
            [{"failure_reason": _SOCKS_FAILURE_REASON, "error_codes": ["net::ERR_SOCKS_CONNECTION_FAILED"]}],
            [],
            None,
            None,
            True,
            None,
            None,
            id="an_unresolvable_proxy_location_still_names_the_hop",
        ),
        pytest.param(
            [{"failure_reason": _PROXY_CODE_PAST_THE_CLAMP, "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"]}],
            [],
            None,
            ProxyLocation.RESIDENTIAL_ES,
            True,
            None,
            None,
            id="the_proxy_code_trails_past_the_display_clamp",
        ),
        pytest.param(
            [
                {
                    "label": "failed_navigation",
                    "block_type": "navigation",
                    "status": "failed",
                    "failure_reason": _TARGET_CODE_PLANTED_IN_THE_URL,
                    "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                }
            ],
            [],
            None,
            None,
            True,
            None,
            None,
            id="a_target_code_planted_in_the_requested_url_is_not_the_verdict",
        ),
        pytest.param(
            [
                {
                    "label": "first",
                    "failure_reason": _TUNNEL_FAILURE_REASON,
                    "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                },
                {"label": "second", "failure_reason": _GENERIC_FAILURE_REASON},
            ],
            [],
            None,
            ProxyLocation.RESIDENTIAL_ES,
            False,
            _GENERIC_FAILURE_REASON,
            None,
            id="a_later_unrelated_block_is_never_labelled_as_the_hop",
        ),
        pytest.param(
            [{"label": "first", "failure_reason": _GENERIC_FAILURE_REASON}],
            [],
            None,
            ProxyLocation.RESIDENTIAL_ES,
            False,
            _GENERIC_FAILURE_REASON,
            None,
            id="an_unrelated_failure_alone",
        ),
        pytest.param(
            [{"label": "first", "failure_reason": _TUNNEL_FAILURE_REASON}],
            [],
            None,
            ProxyLocation.RESIDENTIAL_ES,
            False,
            _TUNNEL_FAILURE_REASON,
            None,
            id="the_producers_own_sentence_without_a_code_attributes_nothing",
        ),
        *(
            pytest.param(
                [{"label": "first", "failure_reason": reason}],
                [],
                None,
                ProxyLocation.RESIDENTIAL_ES,
                False,
                reason,
                None,
                id=f"prose_quoting_a_proxy_code_{index}",
            )
            for index, reason in enumerate(
                (
                    _MODEL_PROSE_QUOTING_A_PROXY_CODE,
                    _PROSE_QUOTING_A_TASK_SEPARATOR,
                    _PROSE_QUOTING_A_GOTO_SEPARATOR,
                    _PROSE_QUOTING_A_NAV_SEPARATOR,
                )
            )
        ),
        *(
            pytest.param(
                [
                    {"label": "first", "failure_reason": first, "error_codes": _driver_codes_for(first)},
                    {"label": "second", "failure_reason": second, "error_codes": _driver_codes_for(second)},
                ],
                [],
                None,
                ProxyLocation.RESIDENTIAL_ES,
                False,
                None,
                _CERT_FAILURE_REASON,
                id=f"a_target_owned_sibling_block_refuses_the_label_{order}",
            )
            for order, (first, second) in enumerate(
                ((_CERT_FAILURE_REASON, _TUNNEL_FAILURE_REASON), (_TUNNEL_FAILURE_REASON, _CERT_FAILURE_REASON))
            )
        ),
        pytest.param(
            [
                {
                    "label": "first",
                    "failure_reason": _TUNNEL_FAILURE_REASON,
                    "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                }
            ],
            [],
            None,
            ProxyLocation.RESIDENTIAL.value,
            True,
            None,
            None,
            id="a_plain_string_proxy_location_still_labels_the_hop",
        ),
    ],
)
def test_the_run_observation_labels_only_a_hop_the_driver_reported(
    blocks: list[dict[str, object]],
    run_codes: list[str],
    connect_failure_reason: str | None,
    proxy_location: ProxyLocation | str | None,
    labelled: bool,
    displayed: str | None,
    stop: str | None,
) -> None:
    """The label and the terminal stop both key on the codes the driver reported, never on the
    sentence a model or page can write."""
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.effective_workflow_proxy_location = proxy_location

    outcome = _record_run_blocks_result(
        ctx,
        {"ok": False, "data": {"blocks": blocks, "error_codes": run_codes}},
        connect_failure_reason=connect_failure_reason,
    )

    display_reason = outcome.display_reason or ""
    assert display_reason.startswith(_proxy_hop_label(proxy_location)) is labelled
    assert display_reason.startswith("Skyvern proxy hop failed") is labelled
    assert ctx.last_test_non_retriable_nav_error == stop
    if displayed is not None:
        assert display_reason == displayed
    if labelled:
        # The label drives the reply copy and holds authoring repair off the block it names.
        assert ctx.last_test_proxy_owned_failure is True
        # The clamp shortens the sentence, so the code has to survive it whole for the label to mean
        # anything: a truncated token names no verdict.
        reported = [*run_codes, *(code for block in blocks for code in block.get("error_codes") or [])]
        assert any(code in display_reason for code in reported)
        assert len(display_reason) <= _DISPLAY_REASON_MAX_CHARS
    elif stop is None:
        # An unlabelled run with no stop is nobody's: it must not be recorded as proxy-owned either.
        assert ctx.last_test_proxy_owned_failure is False
    if connect_failure_reason is not None:
        assert _GENERIC_FAILURE_REASON not in display_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "driver_code", "session_proxy", "workflow_proxy", "expected_error"),
    [
        *(
            pytest.param(
                reason,
                _DRIVER_CODE_BY_REASON[reason],
                ProxyLocation.RESIDENTIAL_ES,
                None,
                f"Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): {reason}",
                id=f"the_driver_reported_the_hop_{index}",
            )
            for index, reason in enumerate(
                (_TUNNEL_FAILURE_REASON, _SOCKS_FAILURE_REASON, _SOCKS_HOST_UNREACHABLE_REASON)
            )
        ),
        pytest.param(
            _TUNNEL_FAILURE_REASON,
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            None,
            None,
            f"Skyvern proxy hop failed: {_TUNNEL_FAILURE_REASON}",
            id="an_unreadable_proxy_label_keeps_the_attribution",
        ),
        pytest.param(
            _TUNNEL_FAILURE_REASON,
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            ProxyLocation.RESIDENTIAL,
            ProxyLocation.RESIDENTIAL_ES,
            f"Skyvern proxy hop failed (proxy_location=RESIDENTIAL): {_TUNNEL_FAILURE_REASON}",
            id="the_session_proxy_wins_over_the_declared_workflow_proxy",
        ),
        *(
            pytest.param(reason, None, ProxyLocation.RESIDENTIAL_ES, None, reason, id=f"unattributable_{index}")
            for index, reason in enumerate(
                (
                    _DNS_FAILURE_REASON,
                    _CERT_FAILURE_REASON,
                    _DNS_FAILURE_WITH_PROXY_TOKEN_IN_URL,
                    _MIXED_CODE_TARGET_CODE_PAST_THE_CLAMPS,
                    _MODEL_PROSE_QUOTING_A_PROXY_CODE,
                    _PROSE_QUOTING_A_TASK_SEPARATOR,
                    _PROSE_QUOTING_A_GOTO_SEPARATOR,
                    _PROSE_QUOTING_A_NAV_SEPARATOR,
                    _GENERIC_FAILURE_REASON,
                )
            )
        ),
    ],
)
async def test_a_scout_navigation_failure_names_only_a_hop_the_driver_reported(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    driver_code: str | None,
    session_proxy: ProxyLocation | None,
    workflow_proxy: ProxyLocation | None,
    expected_error: str,
) -> None:
    ctx = _scout_ctx(monkeypatch, nav_error=reason, session_proxy=session_proxy, driver_code=driver_code)
    if workflow_proxy is not None:
        ctx.effective_workflow_proxy_location = workflow_proxy

    result = await _discovery_navigate(ctx, "https://example.test/")

    assert result["error"] == expected_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "driver_code", "expected_error"),
    [
        pytest.param(
            _TUNNEL_FAILURE_REASON,
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            f"Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): {_TUNNEL_FAILURE_REASON}",
            id="the_driver_reported_the_hop",
        ),
        *(
            pytest.param(reason, None, reason, id=f"unattributable_{index}")
            for index, reason in enumerate(
                (
                    _DNS_FAILURE_REASON,
                    _MODEL_PROSE_QUOTING_A_PROXY_CODE,
                    _PROSE_QUOTING_A_TASK_SEPARATOR,
                    _PROSE_QUOTING_A_GOTO_SEPARATOR,
                    _PROSE_QUOTING_A_NAV_SEPARATOR,
                )
            )
        ),
    ],
)
async def test_a_model_navigation_failure_names_only_a_hop_the_driver_reported(
    monkeypatch: pytest.MonkeyPatch, reason: str, driver_code: str | None, expected_error: str
) -> None:
    ctx = _scout_ctx(monkeypatch, nav_error=reason, session_proxy=ProxyLocation.RESIDENTIAL_ES, driver_code=driver_code)
    monkeypatch.setattr(mcp_hooks_module, "_capture_post_interaction_screenshot", AsyncMock(return_value=None))
    tool_result: dict[str, object] = {"ok": False, "error": reason}
    if driver_code:
        tool_result["nav_error_code"] = driver_code

    result = await _navigate_post_hook(tool_result, {}, ctx)

    assert result["error"] == expected_error


@pytest.mark.parametrize(
    "reason",
    [
        pytest.param(_MODEL_PROSE_QUOTING_A_PROXY_CODE, id="prose_without_a_separator"),
        pytest.param(_PROSE_QUOTING_A_TASK_SEPARATOR, id="prose_quoting_the_task_separator"),
        pytest.param(_PROSE_QUOTING_A_GOTO_SEPARATOR, id="prose_quoting_the_goto_separator"),
        pytest.param(_PROSE_QUOTING_A_NAV_SEPARATOR, id="prose_quoting_the_navigation_separator"),
    ],
)
def test_prose_quoting_a_proxy_code_still_blames_the_target_in_the_terminal_reply(reason: str) -> None:
    reply = agent_module._non_retriable_nav_reply(reason)

    assert reply == _NON_PROXY_FAILURE_REPLY.format(reason=reason)


def test_mixed_code_run_stops_on_the_target_owned_code() -> None:
    reason = (
        "Failed to navigate to url https://x.test. "
        "Error message: net::ERR_SOCKS_CONNECTION_FAILED after net::ERR_CERT_DATE_INVALID"
    )
    ctx, result = _record_and_render_terminal_reply(
        reason,
        block_type="navigation",
        codes=["net::ERR_SOCKS_CONNECTION_FAILED", "net::ERR_CERT_DATE_INVALID"],
    )

    assert ctx.last_test_non_retriable_nav_error == reason
    assert result.user_response == _NON_PROXY_FAILURE_REPLY.format(reason=reason)
    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "non_retriable_nav"
    assert result.proposal_disposition == "no_proposal"
    assert result.updated_workflow is None
    assert ctx.last_run_outcome is not None
    assert not (ctx.last_run_outcome.display_reason or "").startswith("Skyvern proxy hop failed")


def test_chat_reply_refuses_the_proxy_label_when_the_target_code_trails_past_the_clamp() -> None:
    reason = _MIXED_CODE_TARGET_CODE_PAST_THE_CLAMPS
    assert "net::ERR_CERT_DATE_INVALID" not in agent_module._normalize_failure_reason(reason)
    ctx = _fresh_context()
    ctx.last_test_ok = False
    ctx.last_update_block_count = 2
    ctx.last_workflow = SimpleNamespace(workflow_id="wf")
    ctx.last_workflow_yaml = "title: draft"
    ctx.effective_workflow_proxy_location = ProxyLocation.RESIDENTIAL_ES
    ctx.last_failure_category_top = "NAVIGATION_FAILURE"
    ctx.last_test_failure_reason = reason

    reply = agent_module._rewrite_failed_test_response("model text", ctx)

    assert "Skyvern proxy hop failed" not in reply


def test_run_observation_refuses_the_proxy_label_when_the_target_code_trails_past_the_clamp() -> None:
    reason = _MIXED_CODE_TARGET_CODE_PAST_THE_CLAMPS
    # Both codes were reported; the target one is only clamped out of the displayed sentence, and
    # attribution reads the codes rather than that sentence.
    ctx, result = _record_and_render_terminal_reply(
        reason,
        block_type="navigation",
        codes=["net::ERR_TUNNEL_CONNECTION_FAILED", "net::ERR_CERT_DATE_INVALID"],
    )

    assert ctx.last_run_outcome is not None
    assert not (ctx.last_run_outcome.display_reason or "").startswith("Skyvern proxy hop failed")
    assert result.user_response == _NON_PROXY_FAILURE_REPLY.format(reason=reason)


@pytest.mark.parametrize(
    ("failure_category", "failure_reason", "driver_codes", "expects_proxy_label"),
    [
        pytest.param(
            "PROXY_ERROR",
            "no proxy available for location RESIDENTIAL_ES",
            [],
            False,
            id="no_proxy_available",
        ),
        pytest.param(
            "PROXY_ERROR",
            "The page displayed: Proxy unavailable, please contact your administrator.",
            [],
            False,
            id="page_prose_matching_the_proxy_keywords",
        ),
        pytest.param(
            "PROXY_ERROR",
            _PROSE_QUOTING_A_TASK_SEPARATOR,
            [],
            False,
            id="prose_quoting_the_task_separator",
        ),
        pytest.param(
            "PROXY_ERROR",
            _PROSE_QUOTING_A_GOTO_SEPARATOR,
            [],
            False,
            id="prose_quoting_the_goto_separator",
        ),
        pytest.param(
            "PROXY_ERROR",
            _PROSE_QUOTING_A_NAV_SEPARATOR,
            [],
            False,
            id="prose_quoting_the_navigation_separator",
        ),
        pytest.param(
            "NAVIGATION_FAILURE",
            _TUNNEL_FAILURE_REASON,
            ["net::ERR_TUNNEL_CONNECTION_FAILED"],
            True,
            id="the_driver_reported_the_proxy_code",
        ),
        pytest.param("DATA_EXTRACTION_FAILURE", _GENERIC_FAILURE_REASON, [], False, id="unrelated_block_failure"),
    ],
)
def test_the_proxy_label_requires_a_code_the_driver_reported(
    failure_category: str, failure_reason: str, driver_codes: list[str], expects_proxy_label: bool
) -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_update_block_count = 2
    ctx.last_workflow = SimpleNamespace(workflow_id="wf")
    ctx.last_workflow_yaml = "title: draft"
    ctx.effective_workflow_proxy_location = ProxyLocation.RESIDENTIAL_ES
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "blocks": [
                    {
                        "label": "open_page",
                        "status": "failed",
                        "failure_reason": failure_reason,
                        "error_codes": driver_codes,
                    }
                ]
            },
        },
    )
    ctx.last_failure_category_top = failure_category

    reply = agent_module._rewrite_failed_test_response("model text", ctx)

    assert ("Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES)" in reply) is expects_proxy_label
    assert (agent_module._SKYVERN_EGRESS_FOLLOW_UP.strip() in reply) is expects_proxy_label
    expects_neutral_ask = not expects_proxy_label and failure_category == "PROXY_ERROR"
    assert (agent_module._FAILURE_FOLLOW_UP["PROXY_ERROR"].strip() in reply) is expects_neutral_ask
    assert "verify the URL" not in reply
    assert "confirm the URL is correct" not in reply
    assert ctx.last_test_non_retriable_nav_error is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_proxy_location", "run_session_proxy_location", "expected_label"),
    [
        pytest.param(
            ProxyLocation.RESIDENTIAL_ES,
            ProxyLocation.RESIDENTIAL,
            "RESIDENTIAL",
            id="the_attached_session_overrides_the_run_row",
        ),
        pytest.param(
            ProxyLocation.RESIDENTIAL_ES,
            None,
            None,
            id="a_session_naming_no_proxy_is_not_papered_over_by_the_run_row",
        ),
        pytest.param(
            None,
            None,
            None,
            id="a_session_naming_no_proxy_does_not_borrow_the_runtime_default",
        ),
    ],
)
async def test_test_run_only_turn_labels_the_proxy_the_run_acted_through(
    monkeypatch: pytest.MonkeyPatch,
    run_proxy_location: ProxyLocation | None,
    run_session_proxy_location: ProxyLocation | None,
    expected_label: str | None,
) -> None:
    monkeypatch.setattr(settings, "RUNTIME_PROXY_DEFAULT_NONE_ENABLED", False)
    harness = await install_run_blocks_harness(
        monkeypatch,
        workflow_yaml=_PROXY_RUN_WORKFLOW_YAML,
        polled_status="failed",
        run_proxy_location=run_proxy_location,
        run_session_proxy_location=run_session_proxy_location,
        terminal_blocks=[
            WorkflowRunBlock(
                label="open_page",
                block_type=BlockType.NAVIGATION,
                status="failed",
                failure_reason=_TUNNEL_FAILURE_REASON,
                error_codes=["net::ERR_TUNNEL_CONNECTION_FAILED"],
                workflow_run_block_id="wrb_open_page",
                workflow_run_id="wr_paused",
                organization_id="org-1",
                created_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
                modified_at=datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
            )
        ],
    )
    ctx = make_copilot_ctx(browser_session_id="pbs_chat")
    ctx.staged_workflow = harness["workflow"]
    ctx.frontier_resume_session_id = "pbs_run"
    assert ctx.effective_workflow_proxy_location is None
    assert ctx.last_workflow is None

    await _run_blocks_and_collect_debug({"block_labels": ["open_page"], "parameters": {}}, ctx)

    assert ctx.last_workflow is None
    outcome = ctx.last_run_outcome
    assert outcome is not None
    assert outcome.display_reason is not None
    expected_hop = (
        "Skyvern proxy hop failed: "
        if expected_label is None
        else f"Skyvern proxy hop failed (proxy_location={expected_label}): "
    )
    assert outcome.display_reason.startswith(expected_hop)
    proxy_lever = next(lever for lever in author_time_levers(ctx) if lever.mechanism == "proxy_location")
    assert ctx.effective_workflow_proxy_location is None
    assert proxy_lever.availability is None


def test_a_budget_exit_reports_the_proxy_hop_the_recorded_run_named() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.effective_workflow_proxy_location = ProxyLocation.RESIDENTIAL_ES
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "blocks": [
                    {
                        "label": "open_page",
                        "status": "failed",
                        "failure_reason": _TUNNEL_FAILURE_REASON,
                        "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                    }
                ]
            },
        },
    )

    reason, _ = agent_module._recorded_failure_summary(ctx)

    assert reason.startswith("Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): ")


# A proxy code left on the browser state by an earlier hop. Nothing in an MCP call clears it.
_STALE_BROWSER_STATE_CODE = "net::ERR_TUNNEL_CONNECTION_FAILED"


def _scout_ctx(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nav_error: str,
    session_proxy: ProxyLocationInput,
    driver_code: str | None = None,
) -> CopilotContext:
    """A scout turn whose navigation failed.

    ``nav_error`` is the sentence the tool returned and ``driver_code`` is what the browser itself
    reported for this call. They are separate arguments because that is the whole contract: a page
    or a model can write the sentence, only the driver can set the code.

    The browser state carries a different, stale code on purpose. It is cleared only by a successful
    state-managed navigation, which an MCP hop never performs, so a reader that consults it would
    attach an earlier failure's verdict to this call.
    """
    ctx = make_copilot_ctx(browser_session_id="pbs_scout")
    tool_result: dict[str, object] = {"ok": False, "error": nav_error}
    if driver_code:
        tool_result["nav_error_code"] = driver_code
    ctx.discovery_mcp_server = SimpleNamespace(call_internal_tool=AsyncMock(return_value=tool_result))
    browser_state = SimpleNamespace(
        last_navigation_error_code=_STALE_BROWSER_STATE_CODE,
        built_with_proxy_location=session_proxy,
    )
    monkeypatch.setattr(shared_module, "resolve_browser_state_for_context", AsyncMock(return_value=browser_state))
    return ctx


@pytest.mark.parametrize(
    ("proxy_location", "expected_hop_label", "expected_lever_label"),
    [
        pytest.param(
            {"url": "http://user:hunter2@proxy.example.test:8080"},
            "custom proxy",
            "custom proxy",
            id="custom_proxy_dict",
        ),
        pytest.param({"country": "US", "subdivision": "CA"}, "custom proxy", "custom proxy", id="geo_shaped_dict"),
        pytest.param(
            GeoTarget(country="US", subdivision="CA", city="San Jose"), "US", "US-CA-San Jose", id="geo_target"
        ),
        pytest.param(ProxyLocation.RESIDENTIAL_ES, "RESIDENTIAL_ES", "RESIDENTIAL_ES", id="proxy_location_enum"),
        pytest.param(ProxyLocation.NONE, None, None, id="no_proxy"),
    ],
)
def test_the_repair_lever_keeps_the_geo_detail_the_hop_label_drops(
    proxy_location: ProxyLocationInput,
    expected_hop_label: str | None,
    expected_lever_label: str | None,
) -> None:
    ctx = _fresh_context()
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.effective_workflow_proxy_location = proxy_location

    hop_reason = proxy_hop_failure_reason(ctx, _TUNNEL_FAILURE_REASON)
    lever = next(lever for lever in author_time_levers(ctx) if lever.mechanism == "proxy_location")

    assert lever.availability == expected_lever_label
    if expected_hop_label is None:
        assert hop_reason.startswith("Skyvern proxy hop failed: ")
    else:
        assert hop_reason.startswith(f"Skyvern proxy hop failed (proxy_location={expected_hop_label}): ")
    assert "hunter2" not in hop_reason
    assert "hunter2" not in (lever.availability or "")


def test_a_proxy_failure_on_a_challenge_page_carries_both_facts_to_the_model() -> None:
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    ctx.last_test_anti_bot = "Extracted data reported anti-bot blocker: Verify you are human"
    ctx.captcha_solver_available = True
    ctx.effective_workflow_proxy_location = ProxyLocation.RESIDENTIAL_ES
    result = {
        "ok": False,
        "data": {
            "workflow_run_id": "wr_proxy_challenge",
            "overall_status": "failed",
            "failure_reason": _TUNNEL_FAILURE_REASON,
            "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
            "failure_categories": [{"category": "ANTI_BOT_DETECTION", "evidence_source": "challenge_state"}],
            "blocks": [],
        },
    }

    outcome = _record_run_blocks_result(ctx, result)
    finalize_build_test_result(ctx, source_tool="update_and_run_blocks", result=result, workflow_updated=True)
    packet = result["data"][BUILD_TEST_PACKET_KEY]

    assert ctx.last_test_non_retriable_nav_error is None
    assert outcome.display_reason is not None
    assert outcome.display_reason.startswith("Skyvern proxy hop failed (proxy_location=RESIDENTIAL_ES): ")
    assert packet["challenge"] is not None
    proxy_lever = next(lever for lever in packet["levers"] if lever["mechanism"] == "proxy_location")
    assert proxy_lever["availability"] == "RESIDENTIAL_ES"
    assert packet["challenge_notices"]
    assert ctx.last_full_workflow_test_ok is not True


DRIVER_NAV_MESSAGES = [
    # Where the drivers write a code (message shapes: Playwright 1.58 on Chromium, skycdp facade).
    ("Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x.test/net::ERR_CERT_REVOKED", "net::ERR_NAME_NOT_RESOLVED"),
    ("Page.reload: net::ERR_ABORTED; maybe frame was detached?", "net::ERR_ABORTED"),
    (": net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/", "net::ERR_TUNNEL_CONNECTION_FAILED"),
    (
        "navigation to https://x.test/?e=net::ERR_CERT_REVOKED failed: net::ERR_TUNNEL_CONNECTION_FAILED",
        "net::ERR_TUNNEL_CONNECTION_FAILED",
    ),
    # Codes spelled by a URL a page or an author chose, with no driver verdict behind them.
    ("Page.goto: Timeout 30000ms exceeded at https://x.test/?q=net::ERR_TUNNEL_CONNECTION_FAILED", None),
    (
        'Page.reload: Timeout 30000ms exceeded.\nCall log:\n  - navigated to "https://x.test/?e=net::ERR_CERT_REVOKED"\n',
        None,
    ),
    (
        'Page.goto: Navigation to "https://x.test/login" is interrupted by another navigation to '
        '"https://x.test/login net::ERR_NAME_NOT_RESOLVED"',
        None,
    ),
    # Chromium strips tabs from the requested URL, joining a code the author split across them.
    ('Page.goto: Timeout 1ms exceeded.\nCall log:\n  - navigating to "about:blank net::ERR_CERT_REVOKED"\n', None),
    ("navigation to https://x.test/ failed: net::ERR_CERT_REVOKED failed: Cannot navigate to invalid URL", None),
]


@pytest.mark.parametrize(("message", "expected"), DRIVER_NAV_MESSAGES)
def test_a_code_counts_only_where_the_driver_writes_it(message: str, expected: str | None) -> None:
    assert driver_nav_error_code(message) == expected


def test_a_driver_code_survives_the_run_commit_scrub_of_a_value_that_spells_part_of_it() -> None:
    """Workflow parameter values are registered for scrubbing whole, so "net" is enough to corrupt the
    block's code before the ownership checks read it, and a proxy outage then reads as a code defect."""
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    register_secret_scrub_value(ctx, "net")
    register_secret_scrub_value(ctx, "482913")
    # A registered value spelled like a code stays scrubbed: the position is not a licence to write a
    # secret back, and the certificate family is a prefix rather than a closed list.
    register_secret_scrub_value(ctx, "net::ERR_CERT_HUNTER2")
    result = {
        "ok": False,
        "data": {
            "blocks": [
                {
                    "label": "nav",
                    "status": "failed",
                    "failure_reason": "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/",
                    "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                    # Code-shaped text anywhere else is still scrubbed: the exemption is the position.
                    "output": {"result": "net::ERR_482913"},
                },
                {"label": "earlier", "error_codes": ["net::ERR_CERT_HUNTER2"]},
            ]
        },
    }

    _commit_run_blocks_record(ctx, result)
    assert ctx.last_test_proxy_owned_failure is True
    # The model reads the finalized result and packet, which are scrubbed again.
    finalize_build_test_result(ctx, source_tool="update_and_run_blocks", result=result, workflow_updated=True)

    block = result["data"]["blocks"][0]
    assert block["error_codes"] == ["net::ERR_TUNNEL_CONNECTION_FAILED"]
    assert result["data"][BUILD_TEST_PACKET_KEY]["failure"]["error_codes"] == ["net::ERR_TUNNEL_CONNECTION_FAILED"]
    assert "net::" not in block["failure_reason"]
    assert "482913" not in str(result)
    assert "HUNTER2" not in str(result)


def test_a_proxy_failure_does_not_latch_authoring_repair_off_for_later_runs() -> None:
    """The flag is only assigned past the success-path return, so it has to be cleared per run:
    otherwise one proxy failure holds authoring repair off every later failure in the session."""
    ctx = _fresh_context()
    ctx.test_after_update_done = True
    _record_run_blocks_result(
        ctx,
        {
            "ok": False,
            "data": {
                "blocks": [
                    {
                        "label": "nav",
                        "failure_reason": _TUNNEL_FAILURE_REASON,
                        "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                    }
                ]
            },
        },
    )
    assert ctx.last_test_proxy_owned_failure is True

    # A later run that SUCCEEDS returns before the flag is reassigned, so only a per-run clear
    # releases it. A failing run would reassign and hide the leak.
    _record_run_blocks_result(ctx, {"ok": True, "data": {"blocks": [{"label": "nav", "status": "completed"}]}})

    assert ctx.last_test_proxy_owned_failure is False


def test_a_handled_navigation_failure_is_keyed_to_the_task_that_raised_it() -> None:
    """A task block that handles the failure instead of raising still has to report the code.

    Keyed by task rather than last-one-wins because one browser state serves every block in a run: a
    later block that fails without navigating must not inherit an earlier block's code and be
    reported as a network failure.
    """
    context = SkyvernContext(run_id="wr_1")
    context.task_nav_error_codes["tsk_navigated"] = "net::ERR_NAME_NOT_RESOLVED"

    run_context = SimpleNamespace(secrets={})
    with mock.patch.object(skyvern_context, "current", return_value=context):
        assert block_module._recorded_task_nav_error_codes("tsk_navigated", run_context) == [
            "net::ERR_NAME_NOT_RESOLVED"
        ]
        assert block_module._recorded_task_nav_error_codes("tsk_never_navigated", run_context) is None
        # A code that is a stored credential is never carried onto the block row.
        secret_context = SimpleNamespace(secrets={"login": "net::ERR_NAME_NOT_RESOLVED"})
        assert block_module._recorded_task_nav_error_codes("tsk_navigated", secret_context) is None


@pytest.mark.parametrize(
    ("parameter_value", "expected"),
    [
        # The mask replaces every occurrence, so a value inside a code cuts the verdict out of it
        # before any later reader sees it: no proxy attribution, no terminal stop.
        pytest.param("net", ["net::ERR_TUNNEL_CONNECTION_FAILED"], id="a_value_inside_the_code"),
        # A code that is itself a parameter value is left to the mask.
        pytest.param("net::ERR_TUNNEL_CONNECTION_FAILED", ["[redacted]"], id="a_value_that_is_the_code"),
    ],
)
def test_a_driver_code_survives_the_code_block_result_scrub(
    monkeypatch: pytest.MonkeyPatch, parameter_value: str, expected: list[str]
) -> None:
    """The heal path masks the whole block result before the run result is assembled."""

    def redact(value: object, parameters: dict[str, object]) -> object:
        secret = str(next(iter(parameters.values())))

        def walk(node: object) -> object:
            if isinstance(node, str):
                return node.replace(secret, "[redacted]")
            if isinstance(node, list):
                return [walk(item) for item in node]
            return node

        return walk(value)

    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "redact_codeblock_parameter_values", redact)
    result = block_module.BlockResult(
        success=False,
        failure_reason="Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED",
        error_codes=["net::ERR_TUNNEL_CONNECTION_FAILED"],
        status=block_module.BlockStatus.failed,
        output_parameter=_output_parameter(),
        workflow_run_block_id="wrb_test",
    )

    scrubbed = block_module._redact_codeblock_result(result, {"site": parameter_value})

    assert scrubbed.error_codes == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(TaskStatus.terminated, ["net::ERR_NAME_NOT_RESOLVED"]), (TaskStatus.completed, None)],
)
async def test_a_task_that_terminates_after_a_failed_navigation_reports_its_code(
    status: TaskStatus, expected: list[str] | None
) -> None:
    """The action handler keeps the code when the agent terminates right after a failed navigation; the
    block has to carry it too, or Copilot never sees the verdict the task ended on."""
    block = block_module.TaskBlock(
        label="open", output_parameter=_output_parameter(), title="open", url="https://x.test/"
    )
    context = SkyvernContext(run_id="wr_missing_starter_url_test")
    context.task_nav_error_codes["tsk_test"] = "net::ERR_NAME_NOT_RESOLVED"
    built: dict[str, object] = {}

    async def build_block_result(_self: object, **kwargs: object) -> SimpleNamespace:
        built.update(kwargs)
        return SimpleNamespace(**kwargs)

    with _mock_block_execute_deps(working_page_url="https://x.test/") as deps:
        deps["tasks_db"].get_task = AsyncMock(
            return_value=SimpleNamespace(
                task_id="tsk_test", status=status, failure_reason="The site cannot be reached."
            )
        )
        block_module.app.WORKFLOW_SERVICE.get_recent_task_screenshot_artifacts = AsyncMock(return_value=[])
        block_module.app.WORKFLOW_SERVICE.get_recent_workflow_screenshot_artifacts = AsyncMock(return_value=[])
        block_module.app.STORAGE.get_downloaded_files = AsyncMock(return_value=[])
        block_module.app.STORAGE.get_current_attempt_downloaded_files = AsyncMock(return_value=[])
        with (
            mock.patch.object(skyvern_context, "current", return_value=context),
            mock.patch.object(block_module.Block, "build_block_result", build_block_result),
            mock.patch.object(block_module.Block, "record_output_parameter_value", AsyncMock()),
            mock.patch.object(block_module.TaskOutput, "from_task", return_value=mock.MagicMock(model_dump=dict)),
        ):
            await block.execute(
                workflow_run_id="wr_missing_starter_url_test", workflow_run_block_id="wrb", organization_id="o_test"
            )

    assert built["error_codes"] == expected


@pytest.mark.asyncio
async def test_an_unreadable_session_costs_the_proxy_name_not_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving the session is how the hop gets named, not how the failure is reported.

    A session store that is down must not replace the navigation failure with its own exception: the
    model would lose the fault entirely and see a lookup error instead.
    """
    ctx = _scout_ctx(
        monkeypatch,
        nav_error=_TUNNEL_FAILURE_REASON,
        session_proxy=ProxyLocation.RESIDENTIAL_ES,
        driver_code="net::ERR_TUNNEL_CONNECTION_FAILED",
    )
    monkeypatch.setattr(
        shared_module,
        "resolve_browser_state_for_context",
        AsyncMock(side_effect=RuntimeError("session store unavailable")),
    )

    attributed = await shared_module.attribute_navigation_failure(
        ctx,
        {"ok": False, "error": _TUNNEL_FAILURE_REASON, "nav_error_code": "net::ERR_TUNNEL_CONNECTION_FAILED"},
    )

    assert _TUNNEL_FAILURE_REASON in attributed["error"]
    assert "Skyvern proxy hop failed" in attributed["error"]
    assert "proxy_location=" not in attributed["error"]


@pytest.mark.parametrize(
    ("blocks", "expected_stop"),
    [
        pytest.param(
            [
                {
                    "failure_reason": _CERT_FAILURE_REASON,
                    "error_codes": ["net::ERR_CERT_DATE_INVALID"],
                    "output": {"declared_error_code": "net::ERR_CERT_DATE_INVALID"},
                }
            ],
            None,
            id="a_code_the_block_author_declared_is_not_a_browser_verdict",
        ),
        pytest.param(
            [
                {
                    "label": "first",
                    "failure_reason": _SHARED_FAILURE_REASON,
                    "error_codes": ["net::ERR_CERT_DATE_INVALID"],
                },
                {
                    "label": "second",
                    "failure_reason": _SHARED_FAILURE_REASON,
                    "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                },
            ],
            _SHARED_FAILURE_REASON,
            id="two_blocks_sharing_a_sentence_are_judged_on_their_own_codes",
        ),
    ],
)
def test_the_terminal_stop_reads_each_blocks_own_codes(
    blocks: list[dict[str, object]], expected_stop: str | None
) -> None:
    """Reason text neither identifies a block nor says who wrote it: a sentence can repeat across
    blocks, and an author can declare a code that spells a browser verdict."""
    assert _detect_non_retriable_nav_error({"ok": False, "data": {"blocks": blocks}}) == expected_stop


def test_a_run_level_reason_answers_with_the_block_the_run_ended_on() -> None:
    """A block that continued on failure leaves its verdict behind it. Reading every code the result
    carries would stop the run against a sentence whose own block never had that code, and repair
    would never reach the block that actually ended the run."""
    result = {
        "ok": False,
        "data": {
            "failure_reason": _GENERIC_FAILURE_REASON,
            "blocks": [
                {
                    "label": "first",
                    "failure_reason": _CERT_FAILURE_REASON,
                    "error_codes": ["net::ERR_CERT_DATE_INVALID"],
                },
                {"label": "second", "failure_reason": _GENERIC_FAILURE_REASON},
            ],
        },
    }

    # Neither sentence answers with the other's code, and a block the run continued past does not
    # stop a run that ended somewhere else.
    assert _detect_non_retriable_nav_error(result) is None

    # A run whose newest row is a tail the runner appended answers with the newest block that has a
    # reason, so the stop and the sentence a reader is shown come from the same block.
    result["data"]["blocks"] = [
        {"label": "nav", "failure_reason": _CERT_FAILURE_REASON, "error_codes": ["net::ERR_CERT_DATE_INVALID"]},
        {"label": "tail", "status": "skipped"},
    ]
    assert _detect_non_retriable_nav_error(result) == _CERT_FAILURE_REASON

    # A block that ended the run with a code but no sentence of its own is still answered for: the
    # run's sentence carries its verdict, or the stop is lost and nothing reports the failure.
    result["data"]["blocks"] = [{"label": "nav", "error_codes": ["net::ERR_CERT_DATE_INVALID"]}]
    assert _detect_non_retriable_nav_error(result) == _GENERIC_FAILURE_REASON


@pytest.mark.parametrize(
    ("details", "expected_code"),
    [
        pytest.param(
            {"nav_error_code": "net::ERR_TUNNEL_CONNECTION_FAILED"},
            "net::ERR_TUNNEL_CONNECTION_FAILED",
            id="the_driver_reported_a_code",
        ),
        pytest.param({"nav_error_code": None}, None, id="the_message_only_quotes_one"),
    ],
)
def test_the_mcp_flattener_carries_only_a_code_the_driver_reported(
    details: dict[str, object], expected_code: str | None
) -> None:
    """``mcp_to_copilot`` flattens the error to a sentence, so a code left only in that message is
    lost and any reader of it is reading prose."""
    flattened = mcp_to_copilot(
        {
            "ok": False,
            "error": {
                "code": "ACTION_FAILED",
                "message": "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/",
                "hint": "Check that the URL is valid and accessible",
                "details": details,
            },
        }
    )

    assert flattened.get("nav_error_code") == expected_code
    # Absent rather than present-and-empty: a reader that tests the key must not see one.
    assert ("nav_error_code" in flattened) is (expected_code is not None)
    assert proxy_owns_nav_codes([flattened.get("nav_error_code")]) is (expected_code is not None)


@pytest.mark.parametrize(
    ("block", "expected_codes"),
    [
        pytest.param(
            {"error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"]},
            ["net::ERR_TUNNEL_CONNECTION_FAILED"],
            id="the_driver_reported_it",
        ),
        pytest.param(
            {
                "label": "extract",
                "failure_reason": "The catalogue page did not list a price.",
                "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                "output": {
                    "status": "failed",
                    "failure_reason": "The catalogue page did not list a price.",
                    "failure_category": [{"category": "user_defined"}],
                    "declared_error_code": "net::ERR_TUNNEL_CONNECTION_FAILED",
                },
            },
            [],
            id="the_block_author_declared_it",
        ),
        pytest.param(
            {
                "label": "login",
                "block_type": "TASK",
                "failure_reason": _TASK_FAILURE_REASON_TUNNEL,
                "error_codes": ["net::ERR_TUNNEL_CONNECTION_FAILED"],
                # Every failed task carries a failure_category, so treating that as the mark of an
                # authored code would discard the driver codes this change exists to carry.
                "output": {
                    "status": "failed",
                    "failure_reason": _TASK_FAILURE_REASON_TUNNEL,
                    "failure_category": [{"category": "unknown"}],
                },
            },
            ["net::ERR_TUNNEL_CONNECTION_FAILED"],
            id="a_failed_task_block_still_reports_its_driver_code",
        ),
    ],
)
def test_a_blocks_codes_exclude_only_the_one_its_author_declared(
    block: dict[str, object], expected_codes: list[str]
) -> None:
    result = {"ok": False, "data": {"blocks": [block]}}

    assert block_nav_error_codes(result) == expected_codes
    assert proxy_owns_nav_codes(block_nav_error_codes(result)) is bool(expected_codes)


@pytest.mark.asyncio
async def test_a_torn_down_run_still_reports_the_navigation_failure() -> None:
    """The run's secrets are what clear a code for reporting, and they are read inside the failure
    handler -- so a run torn down while the block awaited must degrade to reporting the failure
    without the code, never to raising in place of the failure it was handling."""
    block = block_module.TaskBlock(
        label="open", output_parameter=_output_parameter(), title="open", url="https://x.test/"
    )
    navigation_failure = FailedToNavigateToUrl(
        url="https://x.test/",
        error_message="Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.test/",
        nav_error_code="net::ERR_TUNNEL_CONNECTION_FAILED",
    )

    # The run is alive when the block starts and gone by the time it fails. Recorded output is what
    # sends the handler past _invalidate_stale_output_on_failure, whose own context read would
    # otherwise raise first and hide which guard is under test.
    async def fail_after_recording_output(self: block_module.Block, *args: object, **kwargs: object) -> None:
        self._output_recorded_this_execution = True
        raise navigation_failure

    live_context = mock.MagicMock(secrets={})
    live_context.cancel_failure_evidence_capture = AsyncMock()
    reads: list[str] = []

    def read_run_context(workflow_run_id: str) -> object:
        reads.append(workflow_run_id)
        if len(reads) > 1:
            raise WorkflowRunContextNotInitialized(workflow_run_id)
        return live_context

    with (
        mock.patch("skyvern.forge.sdk.workflow.models.block.app") as mock_app,
        mock.patch.object(block_module.BaseTaskBlock, "execute", fail_after_recording_output),
        mock.patch.object(block_module.Block, "_generate_workflow_run_block_description", new_callable=AsyncMock),
    ):
        mock_app.DATABASE.observer.create_workflow_run_block = AsyncMock(return_value=mock.MagicMock())
        mock_app.DATABASE.observer.update_workflow_run_block = AsyncMock()
        mock_app.DATABASE.workflow_runs.admit_workflow_run_block_dispatch = admit_block_dispatch()
        mock_app.BROWSER_MANAGER.get_for_workflow_run.return_value = None
        mock_app.WORKFLOW_CONTEXT_MANAGER.artifact_redaction_enabled = lambda *_a, **_k: False
        mock_app.WORKFLOW_CONTEXT_MANAGER.get_workflow_run_context.side_effect = read_run_context

        result = await block.execute_safe(workflow_run_id="wr_test", current_index=None)

    assert result.success is False
    assert result.status == block_module.BlockStatus.failed
    assert "x.test" in (result.failure_reason or "")
    assert "net::ERR_TUNNEL_CONNECTION_FAILED" not in (result.error_codes or [])
    assert len(reads) > 1


def test_a_parameter_walk_that_runs_out_leaves_every_code_to_the_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preservation asks whether a code is one of the run's parameter values. A walk that hits its cap
    cannot answer that, and a partial answer would keep a code the mask was right to remove."""
    deep: dict[str, object] = {"leaf": "net::ERR_TUNNEL_CONNECTION_FAILED"}
    for _ in range(block_module._PARAMETER_STRING_WALK_LIMIT):
        deep = {"next": deep}

    def redact(value: object, parameters: dict[str, object]) -> object:
        if isinstance(value, list):
            return ["[redacted]" for _ in value]
        return value

    monkeypatch.setattr(block_module.app.AGENT_FUNCTION, "redact_codeblock_parameter_values", redact)
    result = block_module.BlockResult(
        success=False,
        failure_reason="Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED",
        error_codes=["net::ERR_TUNNEL_CONNECTION_FAILED"],
        status=block_module.BlockStatus.failed,
        output_parameter=_output_parameter(),
        workflow_run_block_id="wrb_test",
    )

    assert block_module.parameter_strings({"deep": deep}) is None
    assert block_module._redact_codeblock_result(result, {"deep": deep}).error_codes == ["[redacted]"]


def test_a_host_with_no_address_record_is_not_answered_with_contact_support() -> None:
    """UnresolvableNavigationHost carries its own typed code (exceptions.py), never derived from
    the reason text: the driver's borrowed tunnel code never reaches error_codes for this case."""
    _, result = _record_and_render_terminal_reply(
        _NO_ADDRESS_RECORD_FAILURE_REASON,
        block_type="navigation",
        codes=[NO_ADDRESS_RECORD_NAV_ERROR_CODE],
    )

    assert result.user_response == _NO_ADDRESS_RECORD_REPLY.format(reason=_NO_ADDRESS_RECORD_FAILURE_REASON)
    assert "Skyvern Support" not in result.user_response
    assert result.turn_outcome is not None
    assert result.turn_outcome.terminal_reason == "non_retriable_nav"


def test_the_no_address_record_marker_alone_cannot_mint_a_terminal_stop() -> None:
    """A page or model quoting the marker text, with no typed code behind it, must not stop the run."""
    result = {"ok": False, "data": {"blocks": [{"failure_reason": _NO_ADDRESS_RECORD_FAILURE_REASON}]}}
    assert _detect_non_retriable_nav_error(result) is None
