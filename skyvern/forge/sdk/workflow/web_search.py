from __future__ import annotations

import asyncio
from typing import Any, TypedDict

import structlog

from skyvern.config import settings
from skyvern.forge.sdk.browser_egress_policy import DestinationBlockedError, classify_url_async
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow import web_search_client
from skyvern.forge.sdk.workflow.web_search_client import SEARCH_TIMEOUT_SECONDS, SearchResponse, WebSearchError

LOG = structlog.get_logger()

MAX_RESULTS_LIMIT = 100
# A limit below this still fetches this many, so a withheld result is replaced from the headroom; at
# or above it nothing extra is fetched and a withheld result costs the caller a slot.
_MIN_FETCHED_RESULTS = 10


class SearchResult(TypedDict):
    title: str
    url: str
    snippet: str


class WebSearchObservation(TypedDict):
    """What the search actually produced. Deliberately not a verdict: the caller reads the
    facts and decides."""

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
        "max_results": {"accepted_type": "int", "minimum": 1, "maximum": MAX_RESULTS_LIMIT},
    },
    "returns": {
        "results": ["title", "url", "snippet"],
        "http_status": (
            "status of the search API request whose results are reported; a failed best-effort follow-up "
            "request does not change it; null when no request completed"
        ),
        "error_kind": "exception class when the search failed without returning any result, else null",
        "page_title": "always empty",
        "extracted_count": "results the search returned before any were withheld",
        "withheld_count": (
            "results withheld because they fall outside the query's site: filter or their destination is not allowed"
        ),
        "capture_truncated": "always false",
    },
    "reading_the_result": (
        "results is what you can use. extracted_count == 0 with a null error_kind means the search "
        "found nothing for this query. A non-null error_kind means the search failed and says nothing "
        "about whether matches exist. withheld_count > 0 with an empty results list means the search "
        "had results and they were filtered, not that the query found nothing. Fewer results than "
        "max_results is not a complete list of what exists."
    ),
}


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


def _validated_max_results(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS_LIMIT:
        raise ValueError(f"max_results must be an integer from 1 to {MAX_RESULTS_LIMIT}")
    return value


def _validated_query(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("query must be a non-empty string")
    return value


async def search_web(query: str, max_results: int = 10) -> WebSearchObservation:
    """Ask the search API and report what came back. Admission runs on every returned result and
    the caller's limit is applied after it."""
    query = _validated_query(query)
    max_results = _validated_max_results(max_results)
    # The deployment's settings (CloudSettings in cloud) own this default; the module-level settings do not.
    if not SettingsManager.get_settings().ENABLE_SEARCH_WEB or (
        not settings.SERPAPI_API_KEY and not settings.EXA_API_KEY
    ):
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
    response = SearchResponse(query=query, provider="google")
    error_kind = None
    extracted: list[SearchResult] = []
    admitted: list[SearchResult] = []
    try:
        async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
            await web_search_client.search(
                response,
                "auto",
                max(_MIN_FETCHED_RESULTS, max_results),
                # One billed Google page per call (plus the client's site: re-fetches); Exa returns
                # max_results in its one request.
                max_pages=1,
            )
            extracted = [
                SearchResult(title=result["title"], url=result["link"], snippet=result["snippet"])
                for result in response.results
            ]
            admitted = await admit_results(extracted)
    except (TimeoutError, WebSearchError) as exc:
        error_kind = type(exc).__name__
        extracted, admitted = [], []
    off_site_count = 0 if error_kind else response.withheld_count
    observation = WebSearchObservation(
        provider=response.http_status_provider or response.provider,
        http_status=response.http_status,
        error_kind=error_kind,
        page_title="",
        results=admitted[:max_results],
        extracted_count=len(extracted) + off_site_count,
        withheld_count=len(extracted) - len(admitted) + off_site_count,
        capture_truncated=False,
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
