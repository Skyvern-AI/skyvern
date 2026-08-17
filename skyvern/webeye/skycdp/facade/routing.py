"""URL matching and handler dispatch for network routing.

Matching happens here, in Python, and never in Chrome's own request filter. Chrome's `urlPattern`
cannot express Playwright's glob -- its `*` crosses `/`, it has no `{a,b}` alternation, and it cannot
express a predicate at all -- so the browser is asked to pause everything and the decision is made
locally. Filtering at the browser would silently widen or narrow what a handler sees.

Dispatch is last-registered-first, and a handler that declines by calling ``fallback()`` hands the
request to the next match rather than ending the chain. Two production guards depend on both
properties and fail *open* without raising if either is wrong: ``codeblock/egress_policy.py`` exists
in its current shape only because ``continue_()`` short-circuits while ``fallback()`` does not, and
``cloud/agent_functions.py`` registers its candidate guard last precisely so it runs first.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Pattern

URLMatcher = str | Pattern[str] | Callable[[str], bool]
RouteHandler = Callable[..., Any]

_sequence = count()


def glob_to_regex(pattern: str) -> str:
    """Translate a Playwright URL glob into an anchored regex.

    Deliberately narrow, and matching Playwright's own translation rather than a general globber:
    ``*`` stops at a path separator, ``**`` does not, ``{a,b}`` alternates, and ``?`` is a literal --
    query strings are full of them, and treating one as a wildcard would silently widen every pattern
    that names a URL with parameters.
    """
    result = ["^"]
    index = 0
    in_group = False
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            result.append(re.escape(pattern[index + 1]))
            index += 2
            continue
        if character == "*":
            following = pattern[index + 1] if index + 1 < len(pattern) else ""
            if following == "*":
                # `**/` should also match zero segments, so `a/**/b` matches `a/b`.
                if pattern[index + 2 : index + 3] == "/":
                    result.append("(?:.*/)?")
                    index += 3
                    continue
                result.append(".*")
                index += 2
                continue
            result.append("[^/]*")
            index += 1
            continue
        if character == "{":
            in_group = True
            result.append("(?:")
        elif character == "}":
            in_group = False
            result.append(")")
        elif character == "," and in_group:
            result.append("|")
        else:
            result.append(re.escape(character))
        index += 1
    result.append("$")
    return "".join(result)


def url_matches(matcher: URLMatcher, url: str) -> bool:
    """Whether ``url`` is selected by a glob, a compiled regex, or a predicate."""
    if callable(matcher) and not isinstance(matcher, (str, re.Pattern)):
        return bool(matcher(url))
    if isinstance(matcher, re.Pattern):
        # `search`, not `fullmatch`: a regex route is written to spot a fragment of the URL.
        return bool(matcher.search(url))
    return bool(re.match(glob_to_regex(matcher), url))


@dataclass(frozen=True)
class RouteEntry:
    matcher: URLMatcher
    handler: RouteHandler
    order: int = field(default_factory=lambda: next(_sequence))


class RouteTable:
    """The registered routes for one context or page, newest first."""

    def __init__(self) -> None:
        self._entries: list[RouteEntry] = []

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, matcher: URLMatcher, handler: RouteHandler) -> None:
        # Prepended, not appended: the most recently registered handler must be consulted first.
        self._entries.insert(0, RouteEntry(matcher=matcher, handler=handler))

    def remove(self, matcher: URLMatcher, handler: RouteHandler | None = None) -> None:
        """Drop routes for ``matcher``; with no handler, drop every route registered for it."""
        self._entries = [
            entry
            for entry in self._entries
            if not (_same_matcher(entry.matcher, matcher) and (handler is None or entry.handler is handler))
        ]

    def clear(self) -> None:
        self._entries = []

    def matching(self, url: str) -> Iterator[RouteEntry]:
        for entry in list(self._entries):
            if url_matches(entry.matcher, url):
                yield entry


def _same_matcher(left: URLMatcher, right: URLMatcher) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, re.Pattern) and isinstance(right, re.Pattern):
        return left.pattern == right.pattern
    return left is right
