from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

import structlog

from skyvern.forge.sdk.browser_egress_policy import DestinationBlockedError, classify_url_async
from skyvern.utils.contained_effects import contained_effect

LOG = structlog.get_logger()


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


class WebSearchObservation(TypedDict):
    """What the search actually produced. Deliberately not a verdict: the caller reads the
    facts and decides. A refused page, an error page and a page with no matches are different
    observations, and no wording heuristic has to tell them apart."""

    provider: str
    http_status: int | None
    error_kind: str | None
    page_title: str
    results: list[SearchResult]
    extracted_count: int
    withheld_count: int
    capture_truncated: bool


WEB_SEARCH_HELPER_CONTRACT: dict[str, Any] = {
    "call": "await search_web(query, max_results=10)",
    "parameters": {
        "query": {"accepted_type": "str"},
        "max_results": {"accepted_type": "int", "minimum": 1},
    },
    "returns": {
        "results": ["title", "url", "snippet"],
        "http_status": "status the search page was served with; null when the transport does not report one",
        "error_kind": "exception class when the fetch itself failed, else null",
        "page_title": "title of the page that was served",
        "extracted_count": "results found on the page before any were withheld",
        "withheld_count": "results withheld because their destination is not allowed",
        "capture_truncated": "true when the page was too large to read in full",
    },
    "reading_the_result": (
        "results is what you can use. extracted_count == 0 with a 2xx http_status means the page "
        "carried no results, which is a refusal page as often as it is a genuine miss -- read "
        "page_title. A non-2xx http_status or a non-null error_kind means the fetch failed and "
        "says nothing about whether matches exist. withheld_count > 0 with an empty results list "
        "means the page had results and they were filtered, not that the query found nothing."
    ),
}


@dataclass(frozen=True)
class FetchedPage:
    """One page as a transport actually saw it."""

    url: str
    title: str
    html: str
    http_status: int | None = None
    truncated: bool = False
    error_kind: str | None = None


class SearchTransport(Protocol):
    """Fetches a URL on whichever browser the caller already owns."""

    async def fetch(self, url: str) -> FetchedPage: ...


class WebSearchProvider(Protocol):
    """A search engine: how to ask it, and how to read its answer. Provider-specific parsing
    lives with the provider so the generic path never guesses at result shape from anchors."""

    name: str

    def result_page_url(self, query: str) -> str: ...

    def extract_results(self, html: str) -> list[SearchResult]: ...


async def destination_allowed(url: str) -> bool:
    """One result may not veto its siblings: a hostname the resolver cannot even encode is
    dropped rather than raised, so a single malformed result does not discard the whole page."""
    try:
        return await classify_url_async(url) is None
    except (DestinationBlockedError, UnicodeError, ValueError):
        return False


async def admit_results(results: list[SearchResult]) -> list[SearchResult]:
    """Classify every result URL. Grouping by hostname would key the admitted set on a
    different parse than the one that produced the verdict."""
    verdicts = await asyncio.gather(*(destination_allowed(result["url"]) for result in results))
    return [result for result, allowed in zip(results, verdicts) if allowed]


def _validated_max_results(value: object) -> int | None:
    """None means "every admitted result": the secure runner cannot carry the caller's limit
    across the operation boundary, so it takes them all and the sandbox applies the limit --
    after admission on both paths, so a withheld result never costs the caller a slot."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_results must be a positive integer")
    return value


def _validated_query(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("query must be a non-empty string")
    return value


async def search_web(
    provider: WebSearchProvider | None,
    transport: SearchTransport,
    query: str,
    max_results: int | None = 10,
) -> WebSearchObservation:
    """Fetch a search page and report what came back. Extraction runs only on a page that was
    actually served, admission runs on every extracted result, and the caller's limit is applied
    last so a withheld result is replaced by the next allowed one rather than lost."""
    query = _validated_query(query)
    max_results = _validated_max_results(max_results)
    if provider is None:
        return WebSearchObservation(
            provider="",
            http_status=None,
            error_kind="not_configured",
            page_title="",
            results=[],
            extracted_count=0,
            withheld_count=0,
            capture_truncated=False,
        )
    result_page_url = provider.result_page_url(query)
    if not await destination_allowed(result_page_url):
        return WebSearchObservation(
            provider=provider.name,
            http_status=None,
            error_kind="endpoint_blocked",
            page_title="",
            results=[],
            extracted_count=0,
            withheld_count=0,
            capture_truncated=False,
        )
    page = await transport.fetch(result_page_url)
    served = page.error_kind is None and (page.http_status is None or 200 <= page.http_status < 300)
    extracted = provider.extract_results(page.html) if served else []
    admitted = await admit_results(extracted)
    observation = WebSearchObservation(
        provider=provider.name,
        http_status=page.http_status,
        error_kind=page.error_kind,
        page_title=page.title,
        results=admitted if max_results is None else admitted[:max_results],
        extracted_count=len(extracted),
        withheld_count=len(extracted) - len(admitted),
        capture_truncated=page.truncated,
    )
    LOG.info(
        "web_search.observed",
        provider=observation["provider"],
        http_status=observation["http_status"],
        error_kind=observation["error_kind"],
        extracted_count=observation["extracted_count"],
        withheld_count=observation["withheld_count"],
        returned_count=len(observation["results"]),
        capture_truncated=observation["capture_truncated"],
        query_len=len(query),
    )
    return observation


_NAVIGATION_TIMEOUT_MS = 20_000


class ResponseLike(Protocol):
    @property
    def status(self) -> int: ...


class PageLike(Protocol):
    async def goto(self, url: str, timeout: float) -> ResponseLike | None: ...

    async def title(self) -> str: ...

    async def content(self) -> str: ...

    async def close(self) -> None: ...


class BrowserContextLike(Protocol):
    async def new_page(self) -> PageLike: ...


class RunBrowserTransport:
    """Fetches on the run's own browser context rather than opening a separate network path, so
    the fetch inherits whatever that context is configured with. It is not itself a guard: the
    endpoint and every returned result are screened by the egress policy in `search_web`."""

    def __init__(self, context: BrowserContextLike) -> None:
        self._context = context

    async def fetch(self, url: str) -> FetchedPage:
        page = None
        try:
            page = await self._context.new_page()
            response = await page.goto(url, timeout=_NAVIGATION_TIMEOUT_MS)
            return FetchedPage(
                url=url,
                title=await page.title(),
                html=await page.content(),
                http_status=response.status if response is not None else None,
            )
        except Exception as exc:
            # The class name, never the message: a navigation error embeds the target URL,
            # which carries the query, and this is persisted as ordinary block output.
            return FetchedPage(url=url, title="", html="", error_kind=type(exc).__name__)
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    with contained_effect("search_web page close failure"):
                        LOG.warning("web_search temporary page close failed")
