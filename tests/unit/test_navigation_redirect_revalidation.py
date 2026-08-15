"""`page.goto` has to return something the SSRF redirect guard can walk.

`navigate_with_retry` calls `_revalidate_navigation_response(response)` on every navigation, because
`page.goto` follows redirects at the network layer and a public entry point can still land on an
internal host (SKY-13112). The guard reads `response.request` and walks `redirected_from`.

An engine whose `goto` returns None does not FAIL that check -- it empties it. `_navigation_hop_urls`
breaks on the first None, the guard validates zero hops, and it passes without having looked. Nothing
raises, nothing logs, and the control reads as working.

That is what skycdp did until `goto` was changed to return the main frame's document response. The
defect was found by recording which Playwright members production actually calls at runtime and
diffing that against what the engine implements -- static analysis had it as a return-type difference
with no consequence attached.

These tests are engine-agnostic on purpose: the guard's contract is "the navigate result exposes a
request chain", and any engine that breaks it silently disables an SSRF control.
"""

from __future__ import annotations

from typing import Any

import pytest

from skyvern.webeye.navigation import _navigation_hop_urls, revalidate_redirect_chain


class _Request:
    def __init__(self, url: str, redirected_from: Any = None) -> None:
        self.url = url
        self.redirected_from = redirected_from


class _Response:
    def __init__(self, request: _Request) -> None:
        self.request = request


def test_a_none_result_yields_no_hops_which_is_why_returning_none_is_dangerous() -> None:
    """Pins the failure mode rather than the fix: this is what an empty guard looks like."""
    assert _navigation_hop_urls(None) == []


def test_a_redirect_chain_is_walked_to_its_origin() -> None:
    first = _Request("http://public.example/start")
    second = _Request("http://evil.internal/", redirected_from=first)
    assert _navigation_hop_urls(_Response(second)) == ["http://evil.internal/", "http://public.example/start"]


@pytest.mark.asyncio
async def test_every_hop_is_revalidated_not_just_the_final_url() -> None:
    """The guard exists because the FINAL url can be innocuous while a hop was internal."""
    seen: list[str] = []
    internal_hop = "http://10.0.0.7/admin"
    chain = _Response(_Request("http://public.example/end", redirected_from=_Request(internal_hop)))

    await revalidate_redirect_chain(chain, seen.append)

    # Exact, ordered comparison rather than a membership check: it pins that BOTH hops are validated
    # and in which order, and it avoids reading as URL substring sanitisation.
    assert seen == ["http://public.example/end", internal_hop], "an internal hop was never revalidated"


@pytest.mark.asyncio
async def test_a_navigate_result_with_no_request_chain_validates_nothing() -> None:
    """The regression in one assertion. An engine returning None makes this list empty, and the guard
    reports success — so this test is what stands between a silent SSRF hole and a caught one."""
    seen: list[str] = []
    await revalidate_redirect_chain(None, seen.append)
    assert seen == [], "sanity: None really does validate nothing"

    seen.clear()
    await revalidate_redirect_chain(_Response(_Request("http://evil.internal/")), seen.append)
    assert seen == ["http://evil.internal/"], "a real navigate result must produce at least one hop to check"


def test_skycdp_goto_is_declared_to_return_the_navigate_result() -> None:
    """A return annotation of None is how this shipped, and it is the cheapest place to catch it.

    Imported lazily so this file stays runnable where the engine package is absent.
    """
    import inspect

    from skyvern.webeye.skycdp.facade.page import Page

    annotation = inspect.signature(Page.goto).return_annotation
    assert annotation is not None and annotation != "None", (
        "skycdp Page.goto declares it returns None; the SSRF redirect guard then validates zero hops"
    )
