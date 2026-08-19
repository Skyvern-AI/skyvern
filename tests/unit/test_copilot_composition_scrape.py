"""Reduced SKY-10711 — skip-renavigation URL matching + the recapture loop's
doomed-raw-scrape trim. (The build-time page-evidence cache was removed: it never
served in a real scout because the agent acts between inspects.)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.config import settings
from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.copilot.composition_evidence import parse_composition_structured
from skyvern.forge.sdk.copilot.tools import _normalized_inspect_url, _same_inspect_target
from skyvern.forge.sdk.copilot.tools.scouting import _page_evidence_location_fingerprint


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
async def test_recapture_skips_raw_get_html_after_cap_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a heavy page the raw get_html is dropped over the MCP size cap; the settle retry
    must re-read via the stripped path only, not re-serialize the full DOM."""
    raw_calls = {"n": 0}
    stripped_payloads = iter([_HOLLOW_HTML, _BOUNDED_HTML])

    async def fake_raw(ctx: object) -> dict:
        raw_calls["n"] += 1
        return {"ok": True, "data": {}}  # cap-dropped: no html payload -> forces stripped fallback

    async def fake_stripped(ctx: object) -> tuple[str, bool]:
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
    # First iteration's raw read is cap-dropped; the settle retry skips it entirely.
    assert raw_calls["n"] == 1
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
    monkeypatch.setattr(tools.composition_capture.asyncio, "sleep", AsyncMock())

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

    # Body-only read: the anti-bot token lives in <title>, so it is absent here by construction.
    get_html = AsyncMock(return_value=(_BOUNDED_HTML, None, False, False))
    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", fake_structured)
    monkeypatch.setattr(tools.composition_capture, "_composition_get_html", get_html)
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    monkeypatch.setattr(tools.composition_capture, "_augment_composition_evidence_with_visual_fallback", identity)
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

    get_html = AsyncMock(return_value=(_BOUNDED_HTML, None, False, False))
    monkeypatch.setattr(tools.composition_capture, "_composition_get_structured_evidence_result", fake_structured)
    monkeypatch.setattr(tools.composition_capture, "_composition_get_html", get_html)
    monkeypatch.setattr(
        tools.composition_capture, "_augment_composition_evidence_with_computed_obstruction_candidates", identity
    )
    monkeypatch.setattr(tools.composition_capture, "_augment_composition_evidence_with_visual_fallback", identity)
    monkeypatch.setattr(tools.composition_capture, "asyncio", _AsyncioSleepProxy(AsyncMock()))

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/login", current_url="https://example.com/login"
    )

    assert html_error is None
    assert evidence is not None
    assert evidence["challenge_state"]["detected"] is True
    assert evidence["challenge_state"]["indicators"] == ["just a moment"]
    get_html.assert_not_awaited()
