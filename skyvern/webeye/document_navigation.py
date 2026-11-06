from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from playwright.async_api import Request

# A longer redirect chain is not followed: its document's request is left unknown.
MAX_NAVIGATION_HOPS = 20


@dataclass(frozen=True)
class DocumentNavigation:
    """The request that committed a page's main-frame document, in the terms the page's HAR records it.

    ``chain`` is its redirect chain as (method, url), oldest first; the last hop committed the document. ``ordinal`` is
    how many requests with that hop's method and url the page had sent, counting it, and ``earlier_starts`` are when
    the earlier ones started, oldest first; ``hop_starts`` holds the same starts, up to and including each, for every
    earlier hop. Starts are Playwright's request timing (epoch ms), which the recording's own time for the same request
    never follows. Held in memory only.
    """

    chain: tuple[tuple[str, str], ...]
    ordinal: int
    started_ms: float
    earlier_starts: tuple[float, ...] = ()
    hop_starts: tuple[tuple[float, ...], ...] = ()

    @property
    def starts(self) -> tuple[float, ...] | None:
        """Every such request's start, oldest first, or None when they are not all known."""
        starts = (*self.earlier_starts, self.started_ms)
        return starts if len(starts) == self.ordinal else None

    @classmethod
    def of(
        cls, sent: Sequence[Request], hop_sent: Sequence[Sequence[Request] | None] = ()
    ) -> DocumentNavigation | None:
        """From every request the page sent with the committing request's method and url, oldest first, and the same for
        each earlier hop of its redirect chain, oldest hop first; a hop whose requests are not known leaves it unknown."""
        known_hops = [earlier for earlier in hop_sent if earlier]
        if not sent:
            return None
        try:
            starts = [[request.timing["startTime"] for request in requests] for requests in (*known_hops, sent)]
            requests: list[Request] = []
            hop: Request | None = sent[-1]
            while hop is not None and len(requests) < MAX_NAVIGATION_HOPS:
                requests.append(hop)
                hop = hop.redirected_from
            chain = tuple((request.method, request.url) for request in reversed(requests))
        except Exception:
            return None
        # Each earlier hop's history must be known and end with that very hop. A request that never got its response
        # has no start time (-1).
        if hop is not None or [earlier[-1] for earlier in known_hops] != list(reversed(requests))[:-1]:
            return None
        if not all(isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 for each in starts for t in each):
            return None
        if not all(isinstance(method, str) and isinstance(url, str) for method, url in chain):
            return None
        floats = [tuple(float(t) for t in each) for each in starts]
        return cls(chain, len(floats[-1]), floats[-1][-1], floats[-1][:-1], tuple(floats[:-1]))
