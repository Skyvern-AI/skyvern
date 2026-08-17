"""URL matching and dispatch order for network routing.

Two production security guards depend on the exact semantics here -- `codeblock/egress_policy.py` and
`cloud/webeye/copilot_candidate_network.py` -- and both fail *open* if the details are wrong, without
raising anything. So the details are pinned rather than described:

- Playwright's glob is not Chrome's. `*` does not cross `/`, `**` does, and `?` is a literal, not a
  wildcard. Matching happens in Python for that reason; Chrome's own matcher cannot express it.
- Handlers dispatch last-registered-first, and a handler that calls `fallback()` hands down to the
  next one. `cloud/agent_functions.py` registers the candidate guard last specifically so it runs
  first, and the whole passthrough mechanism in `egress_policy.py` exists because `continue_()`
  short-circuits the chain while `fallback()` does not.
"""

from __future__ import annotations

import re

import pytest

from skyvern.webeye.skycdp.facade.routing import RouteTable, glob_to_regex, url_matches


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("**/*", "https://example.invalid/a/b", True),
        ("https://*.example.invalid/**", "https://api.example.invalid/x/y", True),
        ("https://*.example.invalid/**", "https://example.invalid/x", False),
        # A single star must not cross a path separator; a double star must.
        ("https://host.invalid/*", "https://host.invalid/one", True),
        ("https://host.invalid/*", "https://host.invalid/one/two", False),
        ("https://host.invalid/**", "https://host.invalid/one/two", True),
        # Brace alternation.
        ("https://host.invalid/{a,b}", "https://host.invalid/b", True),
        ("https://host.invalid/{a,b}", "https://host.invalid/c", False),
        # A question mark is a literal, not a single-character wildcard.
        ("https://host.invalid/a?b", "https://host.invalid/a?b", True),
        ("https://host.invalid/a?b", "https://host.invalid/axb", False),
        # Anchored at both ends.
        ("https://host.invalid/a", "https://host.invalid/ab", False),
    ],
)
def test_playwright_glob_semantics(pattern: str, url: str, expected: bool) -> None:
    assert bool(re.match(glob_to_regex(pattern), url)) is expected


def test_a_callable_pattern_is_asked_about_the_url() -> None:
    """`cloud/webeye/favicon_blocker.py` passes a predicate precisely to avoid regex-on-full-URL."""
    assert url_matches(lambda url: url.endswith("favicon.ico"), "https://h.invalid/favicon.ico") is True
    assert url_matches(lambda url: url.endswith("favicon.ico"), "https://h.invalid/index.html") is False


def test_a_regex_pattern_is_searched_not_fullmatched() -> None:
    assert url_matches(re.compile(r"/api/"), "https://h.invalid/api/v1") is True
    assert url_matches(re.compile(r"/api/"), "https://h.invalid/web") is False


def test_handlers_run_last_registered_first() -> None:
    """LIFO. cloud/agent_functions.py relies on it to keep the candidate guard ahead of the blocker."""
    table = RouteTable()
    order: list[str] = []

    def first(route: object) -> None:
        order.append("first")

    def second(route: object) -> None:
        order.append("second")

    table.add("**/*", first)
    table.add("**/*", second)

    assert [entry.handler for entry in table.matching("https://h.invalid/x")] == [second, first]


def test_unroute_removes_only_the_named_pattern() -> None:
    table = RouteTable()

    def handler(route: object) -> None:
        return None

    table.add("**/*", handler)
    table.add("https://other.invalid/**", handler)
    table.remove("https://other.invalid/**")

    assert len(list(table.matching("https://other.invalid/x"))) == 1


def test_unroute_without_a_handler_removes_every_handler_for_the_pattern() -> None:
    table = RouteTable()

    def one(route: object) -> None:
        return None

    def two(route: object) -> None:
        return None

    table.add("**/*", one)
    table.add("**/*", two)
    table.remove("**/*")
    assert list(table.matching("https://h.invalid/x")) == []


def test_unroute_with_a_handler_removes_only_that_handler() -> None:
    table = RouteTable()

    def one(route: object) -> None:
        return None

    def two(route: object) -> None:
        return None

    table.add("**/*", one)
    table.add("**/*", two)
    table.remove("**/*", one)

    assert [entry.handler for entry in table.matching("https://h.invalid/x")] == [two]
