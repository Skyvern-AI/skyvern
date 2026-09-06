"""Reduced SKY-10711 — skip-renavigation URL matching + the recapture loop's
doomed-raw-scrape trim. (The build-time page-evidence cache was removed: it never
served in a real scout because the agent acts between inspects.)
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from skyvern.config import settings
from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error,
    has_bounded_page_schema,
    parse_composition_structured,
)
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.request_policy import CompletionCriterion, RequestPolicy
from skyvern.forge.sdk.copilot.tools import _normalized_inspect_url, _same_inspect_target
from skyvern.forge.sdk.copilot.tools import _shared as shared_module
from skyvern.forge.sdk.copilot.tools._shared import (
    _composition_get_structured_evidence_result,
    admitted_requested_output_reads,
)
from skyvern.forge.sdk.copilot.tools.scouting import _page_evidence_location_fingerprint
from tests.unit.copilot_test_helpers import make_copilot_ctx
from tests.unit.scoped_asyncio import ScopedAsyncio


class _AsyncioSleepProxy:
    def __init__(self, sleep: AsyncMock) -> None:
        self.sleep = sleep

    def __getattr__(self, name: str):
        return getattr(asyncio, name)


def test_normalized_inspect_url_preserves_distinguishing_parts() -> None:
    assert _normalized_inspect_url("https://Example.com/Search?q=a#frag") == "https://example.com/Search?q=a#frag"
    # query distinguishes search states; scheme and trailing slash are significant
    assert _normalized_inspect_url("https://h/s?q=a") != _normalized_inspect_url("https://h/s?q=b")
    assert _normalized_inspect_url("http://h/p") != _normalized_inspect_url("https://h/p")
    assert _normalized_inspect_url("https://h/p") != _normalized_inspect_url("https://h/p/")
    # empty root path collapses to "/"
    assert _normalized_inspect_url("https://h") == _normalized_inspect_url("https://h/")


def test_normalized_inspect_url_rejects_non_http() -> None:
    for value in ("", None, "current_page", "about:blank", "file:///tmp/x.html"):
        assert _normalized_inspect_url(value) is None


def test_same_inspect_target_is_strict() -> None:
    assert _same_inspect_target("https://h/p?q=1", "https://h/p?q=1") is True
    assert _same_inspect_target("https://h/p?q=1", "https://h/p?q=2") is False
    assert _same_inspect_target("https://h/p", "https://h/p/") is False
    assert _same_inspect_target("current_page", "https://h/p") is False


@pytest.mark.asyncio
async def test_navigation_to_evaluate_session_replacement_records_mixed_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_copilot_ctx(browser_session_id="pbs_before")
    packet = parse_composition_structured(
        {
            "page_title": "Results",
            "body_has_markup": True,
            "forms": [{"fields": [{"selector": "#q", "name": "q"}], "submit_controls": []}],
        },
        inspected_url="https://example.com/results",
        current_url="https://example.com/results",
    )
    assert packet is not None

    async def _page_info(_ctx: object, _session_id: str | None = None) -> tuple[str, str]:
        return "https://example.com/start", "Start"

    async def _navigate(_ctx: object, _url: str, **_kwargs: object) -> dict[str, object]:
        ctx.browser_session_id = "pbs_after"
        ctx.browser_session_continuity_generation += 1
        return {"ok": True, "data": {"url": "https://example.com/results"}}

    async def _capture(_ctx: object, **_kwargs: object) -> tuple[dict[str, object], None]:
        return dict(packet), None

    monkeypatch.setattr(tools.composition_capture, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools.composition_capture, "_fallback_page_info", _page_info)
    monkeypatch.setattr(tools.composition_capture, "_discovery_navigate", _navigate)
    monkeypatch.setattr(tools.composition_capture, "_capture_composition_evidence", _capture)

    result = await tools.composition_capture._inspect_page_for_composition_impl(
        ctx,
        "https://example.com/results",
    )

    assert result["ok"] is True
    assert ctx.composition_page_evidence is not None
    assert "mixed_browser_session_provenance" in ctx.composition_page_evidence["inspection_warnings"]
    assert ctx.composition_page_evidence["browser_session_provenance"] == {
        "mixed": True,
        "start_browser_session_id": "pbs_before",
        "end_browser_session_id": "pbs_after",
        "start_generation": 0,
        "end_generation": 1,
    }


def test_inspection_regression_guard_uses_safe_query_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECRET_KEY", "test-page-evidence-key")
    page_url = "https://example.com/search?q=first"
    evidence = parse_composition_structured(
        {"page_title": "Results", "forms": [{"fields": [{"selector": "#q"}]}]},
        inspected_url="https://example.com/search",
        current_url="https://example.com/search",
    )
    assert evidence is not None
    evidence["current_url_location_fingerprint"] = _page_evidence_location_fingerprint(page_url)
    ctx = SimpleNamespace(
        flow_evidence=[{"step": 3, "reached_via": "interaction", "had_bounded_schema": True, "evidence": evidence}]
    )

    assert tools.composition_capture._non_current_inspection_regression_error(ctx, entry_url=page_url) is None
    assert (
        tools.composition_capture._non_current_inspection_regression_error(
            ctx, entry_url="https://example.com/search?q=second"
        )
        is not None
    )


_HOLLOW_HTML = "<div>loading</div>"
_BOUNDED_HTML = "<form><input name='q'><button type='submit'>Go</button></form>"


@pytest.mark.asyncio
async def test_recapture_reuses_rendered_style_snapshot_without_raw_get_html(monkeypatch: pytest.MonkeyPatch) -> None:
    """Composition recapture must preserve rendered-style facts without serializing the raw DOM."""
    raw_calls = {"n": 0}
    stripped_calls = {"n": 0}
    stripped_payloads = iter([_HOLLOW_HTML, _BOUNDED_HTML])

    async def fake_raw(ctx: object) -> dict:
        raw_calls["n"] += 1
        return {"ok": True, "data": {}}  # cap-dropped: no html payload -> forces stripped fallback

    async def fake_stripped(ctx: object) -> tuple[str, bool]:
        stripped_calls["n"] += 1
        return next(stripped_payloads), False

    async def unavailable_structured(ctx: object, **_kwargs: object) -> tuple[None, None]:
        # This test isolates the HTML cap-drop recapture path; a real structured failure is now
        # reported instead of silently selecting that path.
        return None, None

    async def identity(ctx: object, evidence: dict) -> dict:
        return evidence

    monkeypatch.setattr(tools._shared, "_discovery_get_html", fake_raw)
    monkeypatch.setattr(tools._shared, "_composition_get_stripped_html", fake_stripped)
    monkeypatch.setattr(
        tools.composition_capture, "_composition_get_structured_evidence_result", unavailable_structured
    )
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    settle_sleep = AsyncMock()
    monkeypatch.setattr(tools.composition_capture, "asyncio", _AsyncioSleepProxy(settle_sleep))

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/s", current_url="https://example.com/s"
    )

    assert html_error is None
    assert evidence is not None
    assert tools.has_bounded_page_schema(evidence)
    # Both observations use the bounded rendered snapshot needed for computed-style
    # obstruction facts; neither serializes a raw DOM that may exceed the MCP cap.
    assert stripped_calls["n"] == 2
    assert raw_calls["n"] == 0
    settle_sleep.assert_awaited_once_with(tools.composition_capture._COMPOSITION_HOLLOW_RECAPTURE_DELAY_SECONDS)


@pytest.mark.asyncio
async def test_late_structured_error_retains_valid_hollow_packet(monkeypatch: pytest.MonkeyPatch) -> None:
    first = parse_composition_structured(
        {"page_title": "Loading", "forms": []},
        inspected_url="https://example.com/loading",
        current_url="https://example.com/loading",
    )
    assert first is not None
    capture = AsyncMock(side_effect=[(first, None), (None, "structured extraction timed out")])
    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", capture)
    monkeypatch.setattr(tools.composition_capture, "asyncio", ScopedAsyncio(sleep=AsyncMock()))

    evidence, error = await tools._capture_composition_evidence(
        SimpleNamespace(),
        inspected_url="https://example.com/loading",
        current_url="https://example.com/loading",
    )

    assert error is None
    assert evidence is not None
    assert evidence["page_title"] == first["page_title"]
    assert evidence["current_url"] == first["current_url"]
    assert capture.await_count == 2


def _challenge_signalled_structured_payload(*, with_form: bool = True) -> dict:
    """Anti-bot token in the title only, no rendered challenge control: signalled, no carrier."""
    payload: dict = {
        "page_title": "Just a moment...",
        "anti_bot_indicators": ["just a moment"],
        "challenge_controls": [],
        "body_has_markup": True,
        "forms": [],
    }
    if with_form:
        payload["forms"] = [
            {
                "fields": [{"name": "email", "label": "Email", "type": "text", "selector": "#email"}],
                "submit_controls": [{"text": "Log in", "type": "submit", "selector": "#go"}],
            }
        ]
    return payload


@pytest.mark.asyncio
async def test_unrendered_challenge_keeps_structured_packet_when_relooks_run_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_html reads body only, so re-parsing there drops a title-derived challenge signal.
    Exhausting the re-looks must keep the structured packet instead of trading down to it."""
    packet = parse_composition_structured(
        _challenge_signalled_structured_payload(),
        inspected_url="https://example.com/login",
        current_url="https://example.com/login",
    )

    async def fake_structured(ctx: object, **_kwargs: object) -> tuple[dict, None]:
        return dict(packet), None

    async def identity(ctx: object, evidence: dict) -> dict:
        return evidence

    async def visual_identity(ctx: object, evidence: dict) -> tuple[dict, None]:
        return evidence, None

    # Body-only read: the anti-bot token lives in <title>, so it is absent here by construction.
    get_html = AsyncMock(return_value=(_BOUNDED_HTML, None, False, False))
    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", fake_structured)
    monkeypatch.setattr(tools.composition_capture, "_composition_get_html", get_html)
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_visual_fallback", visual_identity
    )
    settle_sleep = AsyncMock()
    monkeypatch.setattr(tools.composition_capture, "asyncio", _AsyncioSleepProxy(settle_sleep))

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/login", current_url="https://example.com/login"
    )

    assert html_error is None
    assert evidence is not None
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["indicators"] == ["just a moment"]
    # The body-only re-read must not happen at all; it is what erased the signal.
    get_html.assert_not_awaited()
    assert settle_sleep.await_count == tools.composition_capture._COMPOSITION_HOLLOW_RECAPTURE_RETRIES


@pytest.mark.asyncio
async def test_settled_structured_packet_pays_no_extra_relook(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bounded page with no challenge signal is already settled: no settle, no second capture."""
    packet = parse_composition_structured(
        {
            "page_title": "Results",
            "body_has_markup": True,
            "forms": [
                {
                    "fields": [{"name": "q", "label": "Query", "type": "text", "selector": "#q"}],
                    "submit_controls": [{"text": "Go", "type": "submit", "selector": "#go"}],
                }
            ],
        },
        inspected_url="https://example.com/s",
        current_url="https://example.com/s",
    )
    calls = {"n": 0}

    async def fake_structured(ctx: object, **_kwargs: object) -> tuple[dict, None]:
        calls["n"] += 1
        return dict(packet), None

    async def identity(ctx: object, evidence: dict) -> dict:
        return evidence

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", fake_structured)
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    settle_sleep = AsyncMock()
    monkeypatch.setattr(tools.composition_capture, "asyncio", _AsyncioSleepProxy(settle_sleep))

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/s", current_url="https://example.com/s"
    )

    assert html_error is None
    assert tools.has_bounded_page_schema(evidence)
    assert calls["n"] == 1
    settle_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_signalled_packet_survives_extractor_blinking_mid_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A later attempt whose extractor returns None must not clobber the retained packet with a
    body-only reparse: an interstitial that reloads while we re-look fails skyvern_evaluate."""
    packet = parse_composition_structured(
        _challenge_signalled_structured_payload(),
        inspected_url="https://example.com/login",
        current_url="https://example.com/login",
    )
    payloads = iter([dict(packet), None, None])

    async def fake_structured(ctx: object, **_kwargs: object) -> tuple[dict | None, None]:
        return next(payloads), None

    async def identity(ctx: object, evidence: dict) -> dict:
        return evidence

    async def visual_identity(ctx: object, evidence: dict) -> tuple[dict, None]:
        return evidence, None

    get_html = AsyncMock(return_value=(_BOUNDED_HTML, None, False, False))
    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", fake_structured)
    monkeypatch.setattr(tools.composition_capture, "_composition_get_html", get_html)
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_visual_fallback", visual_identity
    )
    monkeypatch.setattr(tools.composition_capture, "asyncio", _AsyncioSleepProxy(AsyncMock()))

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/login", current_url="https://example.com/login"
    )

    assert html_error is None
    assert evidence is not None
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["indicators"] == ["just a moment"]
    get_html.assert_not_awaited()


async def _structured_evidence(server: SimpleNamespace) -> tuple[dict | None, str | None]:
    return await _composition_get_structured_evidence_result(
        SimpleNamespace(discovery_mcp_server=server),
        inspected_url="https://example.com/",
        current_url="https://example.com/",
    )


@pytest.mark.asyncio
async def test_structured_evidence_rejection_returns_the_underlying_error() -> None:
    server = SimpleNamespace(
        call_internal_tool=AsyncMock(return_value={"ok": False, "error": "SecurityError: blocked a frame with origin"})
    )

    evidence, error = await _structured_evidence(server)

    assert evidence is None
    assert error is not None
    assert "SecurityError: blocked a frame with origin" in error
    assert "structured page evidence failed: evaluate returned an error" not in error


class _HostileStr(Exception):
    def __str__(self) -> str:
        raise RuntimeError("boom")


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", ["error_payload", "raised_exception"])
async def test_a_hostile_dunder_str_does_not_escape_the_evidence_error(arm: str) -> None:
    """Both arms return the graceful (None, message); reading the value must not raise on either."""
    server = SimpleNamespace(
        call_internal_tool=AsyncMock(return_value={"ok": False, "error": _HostileStr()})
        if arm == "error_payload"
        else AsyncMock(side_effect=_HostileStr())
    )

    evidence, error = await _structured_evidence(server)

    assert evidence is None
    assert isinstance(error, str)


@pytest.mark.asyncio
async def test_structured_evidence_non_mapping_result_does_not_raise() -> None:
    evidence, error = await _structured_evidence(
        SimpleNamespace(call_internal_tool=AsyncMock(return_value="not-a-mapping"))
    )

    assert evidence is None
    assert error == (
        "skyvern_evaluate returned an error while capturing structured page evidence, "
        "and the result carried no error detail"
    )
    assert "structured page evidence failed: evaluate returned an error" not in error


@pytest.mark.asyncio
async def test_structured_evidence_exception_returns_the_underlying_error() -> None:
    evidence, error = await _structured_evidence(
        SimpleNamespace(call_internal_tool=AsyncMock(side_effect=RuntimeError("CDP target detached")))
    )

    assert evidence is None
    assert error is not None
    assert "CDP target detached" in error


@pytest.mark.asyncio
async def test_structured_evidence_error_is_bounded_and_redacted() -> None:
    server = SimpleNamespace(
        call_internal_tool=AsyncMock(return_value={"ok": False, "error": "api_key=zzzz1111yyyy2222 " + "e" * 4000})
    )

    evidence, error = await _structured_evidence(server)

    assert evidence is None
    assert error is not None
    assert "zzzz1111yyyy2222" not in error
    assert len(error) < 400


# The delay sits strictly between the two deadlines, so the only difference between the arms is
# which deadline governs the same server response and the same packet bytes.
_SLOW_EXTRACTOR_INNER_DEADLINE_SECONDS = 0.05
_SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS = 0.2
_SLOW_EXTRACTOR_OUTER_DEADLINE_SECONDS = 2.0


def _bounded_example_page_payload() -> dict:
    return {
        "page_title": "Example Domain",
        "forms": [
            {
                "id": "lookupForm",
                "name": "",
                "action": "/results",
                "method": "get",
                "fields": [
                    {
                        "name": "q",
                        "id": "q",
                        "label": "Search term",
                        "type": "text",
                        "value": "",
                        "class": [],
                        "placeholder": "search",
                        "required": True,
                        "disabled": False,
                        "checked": False,
                        "options": [],
                        "selector": "#q",
                    }
                ],
                "submit_controls": [{"text": "Search", "selector": "#go"}],
            }
        ],
    }


def _slow_structured_server() -> SimpleNamespace:
    async def call_internal_tool(_tool_name: str, _arguments: dict) -> dict:
        await asyncio.sleep(_SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS)
        return {"ok": True, "data": {"result": json.dumps(_bounded_example_page_payload())}}

    return SimpleNamespace(call_internal_tool=call_internal_tool)


@pytest.mark.asyncio
async def test_structured_timeout_arm_a_nested_deadline_discards_a_slow_read() -> None:
    """A nested deadline shorter than the response discards the whole packet."""
    assert _SLOW_EXTRACTOR_INNER_DEADLINE_SECONDS < _SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS

    started = time.monotonic()
    evidence, error = await _composition_get_structured_evidence_result(
        SimpleNamespace(discovery_mcp_server=_slow_structured_server()),
        inspected_url="https://example.com/",
        current_url="https://example.com/",
        timeout_seconds=_SLOW_EXTRACTOR_INNER_DEADLINE_SECONDS,
    )
    elapsed = time.monotonic() - started

    assert evidence is None
    assert error == (
        f"skyvern_evaluate timed out after {_SLOW_EXTRACTOR_INNER_DEADLINE_SECONDS:g}s "
        "while capturing structured page evidence"
    )
    assert elapsed < _SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS


class _WaitForRecorder:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def __getattr__(self, name: str):
        return getattr(asyncio, name)

    def wait_for(self, awaitable, timeout):  # type: ignore[no-untyped-def]
        self.timeouts.append(timeout)
        return asyncio.wait_for(awaitable, timeout)


@pytest.mark.asyncio
async def test_structured_evidence_arm_b_same_slow_read_completes_under_the_outer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identical response and bytes, governed only by an owning outer deadline."""
    assert _SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS < _SLOW_EXTRACTOR_OUTER_DEADLINE_SECONDS
    recorder = _WaitForRecorder()
    monkeypatch.setattr(shared_module, "asyncio", recorder)

    started = time.monotonic()
    evidence, error = await asyncio.wait_for(
        _composition_get_structured_evidence_result(
            SimpleNamespace(discovery_mcp_server=_slow_structured_server()),
            inspected_url="https://example.com/",
            current_url="https://example.com/",
        ),
        timeout=_SLOW_EXTRACTOR_OUTER_DEADLINE_SECONDS,
    )
    elapsed = time.monotonic() - started

    assert recorder.timeouts == []
    assert error is None
    assert evidence == parse_composition_structured(
        _bounded_example_page_payload(),
        inspected_url="https://example.com/",
        current_url="https://example.com/",
    )
    assert evidence["forms"][0]["fields"][0]["label"] == "Search term"
    assert elapsed >= _SLOW_EXTRACTOR_RESPONSE_DELAY_SECONDS


def _bounded_packet(url: str) -> dict:
    packet = parse_composition_structured(
        {"page_title": "Results", "forms": [{"fields": [{"selector": "#q", "name": "q"}], "submit_controls": []}]},
        inspected_url=url,
        current_url=url,
    )
    assert packet is not None
    return packet


def _scout_interaction_packet(url: str) -> dict:
    return {
        "inspected_url": url,
        "current_url": url,
        "source_tool": "scout_interaction",
        "interaction_tool": "click",
        "interaction_selector": "#go",
    }


def _flow_entry(packet: dict, *, reached_via: str, step: int) -> dict:
    return {
        "evidence": packet,
        "reached_via": reached_via,
        "had_bounded_schema": has_bounded_page_schema(packet),
        "step": step,
    }


def _patch_inspection_seam(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_url: str,
    packet: dict,
    navigations: list[str],
) -> None:
    async def _page_info(_ctx: object, _session_id: str | None = None) -> tuple[str, str]:
        return current_url, "Page"

    async def _navigate(_ctx: object, url: str, **_kwargs: object) -> dict[str, object]:
        navigations.append(url)
        return {"ok": True, "data": {"url": url}}

    async def _capture(_ctx: object, **_kwargs: object) -> tuple[dict, None]:
        return dict(packet), None

    monkeypatch.setattr(tools.composition_capture, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools.composition_capture, "_fallback_page_info", _page_info)
    monkeypatch.setattr(tools.composition_capture, "_discovery_navigate", _navigate)
    monkeypatch.setattr(tools.composition_capture, "_capture_composition_evidence", _capture)


@pytest.mark.asyncio
async def test_current_page_inspect_after_schema_less_interaction_grounds_a_page_dependent_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_url = "https://example.com/results"
    ctx = make_copilot_ctx()
    ctx.flow_evidence = [
        _flow_entry(_bounded_packet("https://example.com/"), reached_via="navigate", step=0),
        _flow_entry(_scout_interaction_packet(reached_url), reached_via="interaction", step=1),
    ]
    navigations: list[str] = []
    _patch_inspection_seam(
        monkeypatch,
        current_url=reached_url,
        packet=_bounded_packet(reached_url),
        navigations=navigations,
    )

    result = await tools.composition_capture._inspect_page_for_composition_impl(ctx, "current_page")

    assert result["ok"] is True
    observation_step = ctx.flow_evidence[-1]["step"]
    assert isinstance(observation_step, int)
    assert result["observation_step"] == observation_step
    assert navigations == []

    workflow_yaml = yaml.safe_dump(
        {
            "title": "wf",
            "workflow_definition": {
                "parameters": [],
                "blocks": [
                    {"block_type": "goto_url", "label": "open_home", "url": "https://example.com/"},
                    {"block_type": "action", "label": "open_results", "navigation_goal": "Open the results page."},
                    {"block_type": "action", "label": "read_results", "navigation_goal": "Read the first result."},
                ],
            },
        }
    )
    assert (
        composition_page_evidence_error(
            ctx,
            workflow_yaml,
            block_observation_refs={"open_results": 1, "read_results": observation_step},
        )
        is None
    )


@pytest.mark.asyncio
async def test_explicit_non_current_url_refuses_and_names_the_schema_less_reached_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_url = "https://example.com/cart"
    ctx = make_copilot_ctx()
    ctx.flow_evidence = [
        _flow_entry(_bounded_packet("https://example.com/results"), reached_via="interaction", step=0),
        _flow_entry(_scout_interaction_packet(reached_url), reached_via="interaction", step=1),
    ]
    navigations: list[str] = []
    _patch_inspection_seam(
        monkeypatch,
        current_url=reached_url,
        packet=_bounded_packet(reached_url),
        navigations=navigations,
    )

    result = await tools.composition_capture._inspect_page_for_composition_impl(
        ctx,
        "https://example.com/checkout",
    )

    assert result["ok"] is False
    assert navigations == []
    assert result["data"]["current_url"] == reached_url
    assert result["data"]["observation_step"] == 1


@pytest.mark.asyncio
async def test_explicit_non_current_url_after_a_current_page_reread_still_names_the_reached_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached_url = "https://example.com/results"
    ctx = make_copilot_ctx()
    ctx.flow_evidence = [
        _flow_entry(_bounded_packet("https://example.com/"), reached_via="navigate", step=0),
        _flow_entry(_scout_interaction_packet(reached_url), reached_via="interaction", step=1),
    ]
    navigations: list[str] = []
    _patch_inspection_seam(
        monkeypatch,
        current_url=reached_url,
        packet=_bounded_packet(reached_url),
        navigations=navigations,
    )

    reread = await tools.composition_capture._inspect_page_for_composition_impl(ctx, "current_page")
    assert reread["ok"] is True
    assert reread["observation_step"] == ctx.flow_evidence[-1]["step"]

    refused = await tools.composition_capture._inspect_page_for_composition_impl(
        ctx,
        "https://example.com/checkout",
    )

    assert refused["ok"] is False
    assert navigations == []
    assert refused["data"]["current_url"] == reached_url
    assert refused["data"]["observation_step"] == 1


@pytest.mark.parametrize(
    "reached_packet",
    [_scout_interaction_packet("https://example.com/results"), _bounded_packet("https://example.com/results")],
    ids=["schema_less", "bounded"],
)
def test_inspection_regression_guard_ignores_a_reached_page_left_by_a_navigation(reached_packet: dict) -> None:
    ctx = SimpleNamespace(
        flow_evidence=[
            _flow_entry(reached_packet, reached_via="interaction", step=0),
            _flow_entry(_bounded_packet("https://example.com/cart"), reached_via="navigate", step=1),
        ]
    )

    assert (
        tools.composition_capture._non_current_inspection_regression_error(ctx, entry_url="https://example.com/cart")
        is None
    )


def test_inspection_regression_guard_uses_current_location_after_leave_and_return() -> None:
    reached_url = "https://example.com/results"
    ctx = SimpleNamespace(
        flow_evidence=[
            _flow_entry(_scout_interaction_packet(reached_url), reached_via="interaction", step=0),
            _flow_entry(_bounded_packet("https://example.com/cart"), reached_via="navigate", step=1),
            _flow_entry(_bounded_packet(reached_url), reached_via="navigate", step=2),
            _flow_entry(_bounded_packet(reached_url), reached_via="current_page", step=3),
        ]
    )

    refusal = tools.composition_capture._non_current_inspection_regression_error(
        ctx,
        entry_url="https://example.com/checkout",
    )

    assert refusal is not None
    assert refusal["data"]["current_url"] == reached_url
    assert refusal["data"]["observation_step"] == 0


def _tile_packet(*, with_relation: bool, match_count: int = 1, position: int = 0) -> dict:
    relations = (
        [
            {
                "key_text": "Sessions Started",
                "value_text": "72.51k",
                "container_selector": "div.query-value__container",
                "container_match_count": match_count,
                "container_position": position,
                "value_child_index": 0,
                "direct_child_count": 1,
                "visible": True,
                "value_visible": True,
                "selector_candidates": [],
                "identity": {},
            }
        ]
        if with_relation
        else []
    )
    packet = parse_composition_structured(
        {
            "page_title": "Fleet Metrics",
            "forms": [],
            "navigation_targets": [{"text": "Overview", "href": "https://example.com/overview", "selector": "a"}],
            "key_value_relations": relations,
        },
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )
    assert packet is not None
    return packet


def _requested_tile_ctx() -> CopilotContext:
    ctx = make_copilot_ctx()
    ctx.request_policy = RequestPolicy(
        completion_criteria=[
            CompletionCriterion(
                id="c0",
                outcome="return the sessions started figure",
                output_path="output.sessions_started",
                requested_output_label="Sessions Started",
            )
        ]
    )
    return ctx


def _install_tile_vision(
    monkeypatch: pytest.MonkeyPatch, calls: dict[str, int], label: str = "Sessions Started"
) -> None:
    async def screenshot(ctx: object, *, dispatch_session_id: object) -> dict:
        calls["screenshot"] += 1
        return {"ok": True, "data": {"screenshot_base64": base64.b64encode(b"frame").decode()}}

    async def handler(**_kwargs: object) -> dict:
        calls["vision"] += 1
        return {
            "summary": "One metric panel over a dashboard grid.",
            "requested_values": [{"label": label, "value": "72.51k"}],
        }

    async def visual_handler(ctx: object) -> object:
        return handler

    monkeypatch.setattr(tools.composition_capture, "_composition_get_screenshot", screenshot)
    monkeypatch.setattr(tools.composition_capture, "_composition_visual_handler", visual_handler)


def _install_witness_read(ctx: CopilotContext, returns: str | None = "72.51k") -> list[str]:
    expressions: list[str] = []

    async def call_internal_tool(_tool_name: str, arguments: dict) -> dict:
        expressions.append(arguments["expression"])
        return {"ok": True, "data": {"result": returns}}

    ctx.discovery_mcp_server = SimpleNamespace(call_internal_tool=call_internal_tool)
    return expressions


@pytest.mark.asyncio
async def test_screenshot_read_value_addresses_the_tile_the_label_alone_could_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"screenshot": 0, "vision": 0}
    witnessed_per_call: list[tuple[str, ...]] = []

    async def structured(ctx: object, **kwargs: object) -> tuple[dict, None]:
        witnessed = tuple(kwargs.get("witnessed_values") or ())
        witnessed_per_call.append(witnessed)
        return _tile_packet(with_relation=bool(witnessed)), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls)
    ctx = _requested_tile_ctx()
    reads = _install_witness_read(ctx)

    evidence, error = await tools._capture_composition_evidence(
        ctx,
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )

    assert error is None
    assert evidence is not None
    assert (calls["screenshot"], calls["vision"]) == (1, 1)
    assert witnessed_per_call == [(), ("72.51k",)]
    assert evidence["requested_values"] == [{"label": "Sessions Started", "value": "72.51k"}]
    assert [
        (witness["label"], witness["value"], witness["output_path"]) for witness in evidence["value_witnesses"]
    ] == [("Sessions Started", "72.51k", "output.sessions_started")]
    assert len(reads) == 1
    assert "div.query-value__container" in reads[0]
    projected = tools.composition_capture.model_visible_composition_evidence(evidence)
    assert "expression" not in projected["value_witnesses"][0]


@pytest.mark.asyncio
async def test_witness_read_that_does_not_return_the_witnessed_value_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"screenshot": 0, "vision": 0}

    async def structured(ctx: object, **kwargs: object) -> tuple[dict, None]:
        return _tile_packet(with_relation=bool(kwargs.get("witnessed_values"))), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls)
    ctx = _requested_tile_ctx()
    reads = _install_witness_read(ctx, returns="72.7k")

    evidence, error = await tools._capture_composition_evidence(
        ctx,
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )

    assert error is None
    assert evidence is not None
    assert len(reads) == 1
    assert "value_witnesses" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        {"value_truncated": True},
        {"container_match_count": 1, "container_position": 3},
        {"direct_child_count": 1, "value_child_index": 4},
    ],
)
async def test_a_relation_whose_channel_or_coordinates_are_broken_witnesses_nothing(damage: dict) -> None:
    relation = {
        "key_text": "Sessions Started",
        "value_text": "72.51k",
        "container_selector": "div.query-value__container",
        "container_match_count": 2,
        "container_position": 1,
        "value_child_index": 0,
        "direct_child_count": 1,
        "visible": True,
        "value_visible": True,
        **damage,
    }
    ctx = _requested_tile_ctx()
    reads = _install_witness_read(ctx)

    records = await tools.composition_capture._value_witness_records(
        ctx,
        {"key_value_relations": [relation]},
        [tools.composition_capture.ValueWitness("Sessions Started", "72.51k", "output.sessions_started")],
    )

    assert records == []
    assert reads == []


@pytest.mark.asyncio
async def test_witness_read_pins_the_selector_cardinality_it_was_observed_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"screenshot": 0, "vision": 0}

    async def structured(ctx: object, **kwargs: object) -> tuple[dict, None]:
        return _tile_packet(with_relation=bool(kwargs.get("witnessed_values")), match_count=4, position=2), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls)
    ctx = _requested_tile_ctx()
    reads = _install_witness_read(ctx)

    evidence, error = await tools._capture_composition_evidence(
        ctx,
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )

    assert error is None
    assert evidence is not None
    assert len(reads) == 1
    expression = reads[0]
    assert "nodes.length !== 4" in expression
    assert "nodes[2]" in expression
    projected = tools.composition_capture.model_visible_composition_evidence(evidence)
    assert "expression" not in projected["value_witnesses"][0]


@pytest.mark.asyncio
async def test_failed_second_look_keeps_the_evidence_the_first_one_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"screenshot": 0, "vision": 0}

    async def structured(ctx: object, **kwargs: object) -> tuple[dict | None, str | None]:
        if kwargs.get("witnessed_values"):
            return None, "skyvern_evaluate timed out"
        return _tile_packet(with_relation=False), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls)

    evidence, error = await tools._capture_composition_evidence(
        _requested_tile_ctx(),
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )

    assert error is None
    assert evidence is not None
    assert evidence["page_title"] == "Fleet Metrics"
    assert "value_witnesses" not in evidence
    assert "value_witness_extract_failed" not in (evidence.get("inspection_warnings") or [])


@pytest.mark.asyncio
async def test_label_the_page_already_answers_takes_no_screenshot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"screenshot": 0, "vision": 0}

    async def structured(ctx: object, **_kwargs: object) -> tuple[dict, None]:
        return _tile_packet(with_relation=True), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls)

    evidence, error = await tools._capture_composition_evidence(
        _requested_tile_ctx(),
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
    )

    assert error is None
    assert evidence is not None
    assert (calls["screenshot"], calls["vision"]) == (0, 0)
    assert "value_witnesses" not in evidence


@pytest.mark.asyncio
async def test_structured_evidence_read_carries_the_witnessed_values_into_the_page_expression() -> None:
    expressions: list[str] = []

    async def call_internal_tool(_tool_name: str, arguments: dict) -> dict:
        expressions.append(arguments["expression"])
        return {"ok": True, "data": {"result": json.dumps(_bounded_example_page_payload())}}

    await _composition_get_structured_evidence_result(
        SimpleNamespace(discovery_mcp_server=SimpleNamespace(call_internal_tool=call_internal_tool)),
        inspected_url="https://example.com/",
        current_url="https://example.com/",
        witnessed_values=("72.51k",),
    )

    assert 'const WITNESSED_VALUES=["72.51k"];' in expressions[0]


def _witness_packet(**extra: object) -> dict:
    packet: dict = {
        "value_witnesses": [
            {
                "label": "Sessions Started",
                "value": "72.51k",
                "output_path": "output.sessions_started",
                "expression": "x",
            }
        ],
        "requested_values": [{"label": "Sessions Started", "value": "72.51k"}],
    }
    packet.update(extra)
    return packet


@pytest.mark.parametrize(
    ("packet", "capture_session_id"),
    [
        (_witness_packet(browser_session_provenance={"mixed": True}), None),
        (_witness_packet(), "a-different-session"),
    ],
)
def test_witness_the_capture_cannot_record_leaves_no_witness_in_the_packet(
    monkeypatch: pytest.MonkeyPatch, packet: dict, capture_session_id: str | None
) -> None:
    recorded: list[str] = []
    monkeypatch.setattr(
        tools.composition_capture,
        "_record_scouted_read",
        lambda ctx, *, expression, data, url, declared_output_path=None: recorded.append(expression),
    )
    ctx = _requested_tile_ctx()

    tools.composition_capture._record_value_witnesses(
        ctx,
        packet,
        url="https://example.com/metrics",
        capture_session_id=capture_session_id,
        run_page_source_session_id=None,
    )

    assert recorded == []
    assert "value_witnesses" not in packet
    assert "requested_values" not in packet
    assert tools._witnessed_output_paths(packet) == frozenset()


def test_a_read_bound_to_another_path_leaves_its_path_to_the_designation_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tools.composition_capture,
        "_record_scouted_read",
        lambda ctx, *, expression, data, url, declared_output_path=None: {
            "tool_name": "read_value",
            "read_expression": expression,
            "read_output_path": "output.a_different_field",
        },
    )
    ctx = _requested_tile_ctx()
    packet = _witness_packet()

    tools.composition_capture._record_value_witnesses(
        ctx,
        packet,
        url="https://example.com/metrics",
        capture_session_id=ctx.browser_session_id,
        run_page_source_session_id=None,
    )

    assert "value_witnesses" not in packet
    assert "requested_values" not in packet
    assert tools._witnessed_output_paths(packet) == frozenset()


def test_a_declined_recording_leaves_its_path_to_the_designation_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tools.composition_capture,
        "_record_scouted_read",
        lambda ctx, *, expression, data, url, declared_output_path=None: None,
    )
    ctx = _requested_tile_ctx()
    packet = _witness_packet()

    tools.composition_capture._record_value_witnesses(
        ctx,
        packet,
        url="https://example.com/metrics",
        capture_session_id=ctx.browser_session_id,
        run_page_source_session_id=None,
    )

    assert "value_witnesses" not in packet
    assert "requested_values" not in packet
    assert tools._witnessed_output_paths(packet) == frozenset()


def test_two_labels_reading_one_value_witness_nothing() -> None:
    summary = {
        "requested_values": [
            {"label": "Sessions Started", "value": "72.51k"},
            {"label": "Retries Queued", "value": "72.51k"},
        ]
    }
    owned = {"Sessions Started": "output.a", "Retries Queued": "output.b"}

    witnesses, dropped = tools.composition_capture._witnesses_from_visual_summary(
        summary, owned, ("Sessions Started", "Retries Queued")
    )

    assert witnesses == []
    assert dropped == 2


def test_two_names_for_one_output_reading_one_value_witness_it_once() -> None:
    summary = {
        "requested_values": [
            {"label": "Sessions Started", "value": "72.51k"},
            {"label": "panel metric", "value": "72.51k"},
        ]
    }
    owned = {"Sessions Started": "output.sessions_started", "panel metric": "output.sessions_started"}

    witnesses, dropped = tools.composition_capture._witnesses_from_visual_summary(
        summary, owned, ("Sessions Started", "panel metric")
    )

    assert [(witness.value, witness.output_path) for witness in witnesses] == [("72.51k", "output.sessions_started")]
    assert dropped == 1


def test_a_designated_label_seeds_a_capture_target_and_never_a_witness_value() -> None:
    reads = [
        {"output_path": f"output.metric_{index}", "value_text": f"value {index}", "label": f"label {index}"}
        for index in range(9)
    ]
    admitted, rejected = admitted_requested_output_reads(reads)

    targets = tools.composition_capture._seeded_capture_targets(make_copilot_ctx(), admitted)

    assert len(admitted) == 8
    assert rejected[0]["reason"] == "only-first-8-reads-verified"
    assert targets == tuple(f"label {index}" for index in range(8))
    assert not hasattr(tools.composition_capture, "_designated_values")


@pytest.mark.asyncio
async def test_designated_read_seeds_the_screenshot_pass_when_the_turn_declared_no_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"screenshot": 0, "vision": 0}
    seeded_targets: list[tuple[str, ...]] = []
    seeded_values: list[tuple[str, ...]] = []

    async def structured(ctx: object, **kwargs: object) -> tuple[dict, None]:
        seeded_targets.append(tuple(kwargs.get("requested_targets") or ()))
        seeded_values.append(tuple(kwargs.get("witnessed_values") or ()))
        return _tile_packet(with_relation=bool(kwargs.get("witnessed_values"))), None

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    _install_tile_vision(monkeypatch, calls, label="panel metric")
    admitted, _ = admitted_requested_output_reads(
        [{"output_path": "output.sessions_started", "value_text": "Sessions Started", "label": "panel metric"}]
    )
    ctx = make_copilot_ctx()
    _install_witness_read(ctx)

    evidence, error = await tools._capture_composition_evidence(
        ctx,
        inspected_url="https://example.com/metrics",
        current_url="https://example.com/metrics",
        requested_reads=admitted,
    )

    assert error is None
    assert evidence is not None
    assert seeded_targets[0] == ("panel metric",)
    assert seeded_values[0] == ()
    assert (calls["screenshot"], calls["vision"]) == (1, 1)
    assert [(witness["value"], witness["output_path"]) for witness in evidence["value_witnesses"]] == [
        ("72.51k", "output.sessions_started")
    ]


@pytest.mark.asyncio
async def test_inspect_tool_forwards_the_designated_read_into_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    forwarded: list[tuple[str, ...]] = []
    ctx = make_copilot_ctx()

    async def capture(_ctx: object, **kwargs: object) -> tuple[dict, None]:
        reads = kwargs.get("requested_reads") or ()
        forwarded.append(tuple(read.output_path for read in reads))
        return _tile_packet(with_relation=True), None

    async def page_info(_ctx: object, _session_id: str | None = None) -> tuple[str, str]:
        return "https://example.com/metrics", "Fleet Metrics"

    async def navigate(_ctx: object, url: str, **_kwargs: object) -> dict[str, object]:
        return {"ok": True, "data": {"url": url}}

    monkeypatch.setattr(tools, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools.composition_capture, "_authority_tool_error", lambda *_args: None)
    monkeypatch.setattr(tools.composition_capture, "_fallback_page_info", page_info)
    monkeypatch.setattr(tools.composition_capture, "_discovery_navigate", navigate)
    monkeypatch.setattr(tools.composition_capture, "_capture_composition_evidence", capture)

    for target_url in ("current_page", "https://example.com/other"):
        raw = await tools.inspect_page_for_composition_tool.on_invoke_tool(
            SimpleNamespace(context=ctx, tool_name="inspect_page_for_composition"),
            json.dumps(
                {
                    "target_url": target_url,
                    "requested_output_reads": [
                        {"output_path": "sessions_started", "value_text": "Sessions Started", "label": "panel metric"}
                    ],
                }
            ),
        )
        assert json.loads(raw)["ok"] is True

    assert forwarded == [("output.sessions_started",), ("output.sessions_started",)]


@pytest.mark.asyncio
async def test_witnessed_output_path_is_not_probed_again_by_the_designation_verifier() -> None:
    probes: list[str] = []

    async def call_internal_tool(_tool_name: str, arguments: dict) -> dict:
        probes.append(arguments["expression"])
        return {"ok": True, "data": {"result": json.dumps({"text": "72.51k", "selector_candidates": ["#v"]})}}

    ctx = make_copilot_ctx()
    ctx.discovery_mcp_server = SimpleNamespace(call_internal_tool=call_internal_tool)
    reads = [
        {"output_path": "output.sessions_started", "value_text": "72.51k", "label": "Sessions Started"},
        {"output_path": "output.failures", "value_text": "3", "label": "Failures"},
    ]

    verified, unverified = await tools._verify_requested_output_reads(
        ctx, reads, frozenset({"output.sessions_started"})
    )

    assert [fact["output_path"] for fact in verified] == ["output.failures"]
    assert unverified == [{"output_path": "output.sessions_started", "reason": "superseded-by-value-witness"}]
    assert len(probes) == 1


@pytest.mark.asyncio
async def test_capture_after_a_failed_navigation_still_asks_for_the_designated_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded_targets: list[tuple[str, ...]] = []
    seeded_values: list[tuple[str, ...]] = []

    async def structured(ctx: object, **kwargs: object) -> tuple[dict, None]:
        seeded_targets.append(tuple(kwargs.get("requested_targets") or ()))
        seeded_values.append(tuple(kwargs.get("witnessed_values") or ()))
        return _tile_packet(with_relation=True), None

    async def page_info(_ctx: object, _session_id: str | None = None) -> tuple[str, str]:
        return "https://example.com/metrics", "Fleet Metrics"

    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", structured)
    monkeypatch.setattr(tools.composition_capture, "_fallback_page_info", page_info)
    admitted, _ = admitted_requested_output_reads(
        [{"output_path": "output.sessions_started", "value_text": "Sessions Started", "label": "panel metric"}]
    )

    captured = await tools.composition_capture._composition_evidence_after_navigation_failure(
        make_copilot_ctx(),
        inspected_url="https://example.com/metrics",
        navigation_error="timeout",
        requested_reads=admitted,
    )

    assert captured is not None
    assert seeded_targets == [("panel metric",)]
    # A designated value is the model's own claim, so it never keys the page's value-first pass.
    assert seeded_values == [()]


def test_one_output_whose_names_read_different_values_witnesses_nothing() -> None:
    summary = {
        "requested_values": [
            {"label": "Sessions Started", "value": "72.51k"},
            {"label": "panel metric", "value": "0.4%"},
        ]
    }
    owned = {"Sessions Started": "output.sessions_started", "panel metric": "output.sessions_started"}

    witnesses, dropped = tools.composition_capture._witnesses_from_visual_summary(
        summary, owned, ("Sessions Started", "panel metric")
    )

    assert witnesses == []
    assert dropped == 2
