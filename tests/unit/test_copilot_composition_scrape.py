"""Reduced SKY-10711 — skip-renavigation URL matching + the recapture loop's
doomed-raw-scrape trim. (The build-time page-evidence cache was removed: it never
served in a real scout because the agent acts between inspects.)
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from skyvern.forge.sdk.copilot import tools
from skyvern.forge.sdk.copilot.tools import _normalized_inspect_url, _same_inspect_target


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

    async def identity(ctx: object, evidence: dict) -> dict:
        return evidence

    monkeypatch.setattr(tools, "_discovery_get_html", fake_raw)
    monkeypatch.setattr(tools, "_composition_get_stripped_html", fake_stripped)
    monkeypatch.setattr(tools, "_augment_composition_evidence_with_computed_obstruction_candidates", identity)

    evidence, html_error = await tools._capture_composition_evidence(
        SimpleNamespace(), inspected_url="https://example.com/s", current_url="https://example.com/s"
    )

    assert html_error is None
    assert evidence is not None
    assert tools.has_bounded_page_schema(evidence)
    # First iteration's raw read is cap-dropped; the settle retry skips it entirely.
    assert raw_calls["n"] == 1
