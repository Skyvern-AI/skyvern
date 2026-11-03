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
"""

from __future__ import annotations


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
