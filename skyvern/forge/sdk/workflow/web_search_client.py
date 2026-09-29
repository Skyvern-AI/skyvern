from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
from urllib.parse import SplitResult, parse_qs, urlencode, urlsplit

import structlog
from opentelemetry.context import _SUPPRESS_HTTP_INSTRUMENTATION_KEY, attach, detach, set_value

from skyvern.config import settings
from skyvern.forge.failure_classifier import FailureCategory
from skyvern.forge.sdk.core.aiohttp_helper import aiohttp_request
from skyvern.utils.contained_effects import contained_effect
from skyvern.utils.secret_redaction import redact_secrets_from_text

LOG = structlog.get_logger()

SearchProvider = Literal["google", "exa"]
ProviderChoice = Literal["auto", "google", "exa"]
SEARCH_TIMEOUT_SECONDS = 180


def _site_restriction(query: str) -> tuple[str, str] | None:
    if "OR" in query.split() or any(character in query for character in '|"“”()'):
        return None
    tokens = [
        token
        for token in query.split()
        if re.search(r"(?<!\w)site:", token, re.IGNORECASE) and not token.lower().startswith("-site:")
    ]
    if len(tokens) != 1 or not re.fullmatch(r"site:([a-z0-9-]+\.)+[a-z0-9-]+(/\S*)?", tokens[0], re.IGNORECASE):
        return None
    host, separator, path = tokens[0][5:].lower().partition("/")
    return host, separator + path if path else ""


def _http_url(link: Any) -> SplitResult | None:
    try:
        parts = urlsplit(link) if isinstance(link, str) else None
    except ValueError:
        return None
    return parts if parts is not None and parts.scheme in {"http", "https"} and parts.hostname else None


def _within_site(parts: SplitResult, restriction: tuple[str, str]) -> bool:
    host, path = restriction
    hostname = (parts.hostname or "").removesuffix(".")
    return (hostname == host or hostname.endswith("." + host)) and parts.path.lower().startswith(path)


def _all_results_outside_site(items: Any, restriction: tuple[str, str]) -> bool:
    if not isinstance(items, list):
        return False
    urls = [url for item in items if isinstance(item, dict) and (url := _http_url(item.get("link"))) is not None]
    return bool(urls) and not any(_within_site(url, restriction) for url in urls)


class WebSearchError(Exception):
    def __init__(self, message: str, category: FailureCategory = FailureCategory.INFRASTRUCTURE_ERROR) -> None:
        super().__init__(message)
        self.category = category


class SearchResult(TypedDict):
    title: str
    link: str
    snippet: str
    display_link: str
    position: int


@dataclass
class SearchResponse:
    query: str
    provider: SearchProvider
    results: list[SearchResult] = field(default_factory=list)
    pages: list[dict[str, Any]] = field(default_factory=list)
    prompt_output: Any = None
    withheld_count: int = 0
    # Status and provider of the last API request that completed, set together before the status is
    # judged, so a request that fails on its status still reports it.
    http_status: int | None = None
    http_status_provider: SearchProvider | None = None
    fallback_reason: str | None = None

    def output(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "results": self.results,
            "total_count": len(self.results),
            "prompt_output": self.prompt_output,
            "raw_response": {"pages": self.pages},
        }


def redact_keys(value: Any, secret_values: Collection[str] = ()) -> Any:
    if isinstance(value, str):
        keys = [key for key in (*secret_values, settings.SERPAPI_API_KEY, settings.EXA_API_KEY) if key]
        redacted = redact_secrets_from_text(value, keys)
        normalized = re.sub(r"%[0-9a-fA-F]{2}", lambda match: match[0].upper(), redacted)
        masked = redact_secrets_from_text(normalized, keys)
        return masked if masked != normalized else redacted
    if isinstance(value, dict):
        return {redact_keys(key, secret_values): redact_keys(item, secret_values) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_keys(item, secret_values) for item in value]
    return value


async def request(
    response: SearchResponse, provider: SearchProvider, url: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    # SerpAPI authenticates in the URL; the HTTP instrumentor does not redact api_key.
    token = attach(set_value(_SUPPRESS_HTTP_INSTRUMENTATION_KEY, True))
    try:
        status, _, body = await aiohttp_request(
            method="GET" if provider == "google" else "POST",
            url=url,
            headers={"x-api-key": settings.EXA_API_KEY or ""} if provider == "exa" else None,
            json_data=payload,
            timeout=30,
            follow_redirects=False,
        )
    except TimeoutError:
        raise TimeoutError(f"{provider.title()} search timed out after 30 seconds.") from None
    except Exception:
        raise WebSearchError(f"{provider.title()} search request failed.") from None
    finally:
        detach(token)

    response.http_status = status
    response.http_status_provider = provider
    if status in {401, 403}:
        raise WebSearchError(f"{provider.title()} search rejected the platform API key (HTTP {status}).")
    if status == 429:
        raise WebSearchError(f"{provider.title()} search quota or rate limit was exceeded (HTTP 429).")
    if not 200 <= status < 300:
        raise WebSearchError(f"{provider.title()} search failed (HTTP {status}).")
    if not isinstance(body, dict):
        raise WebSearchError(f"{provider.title()} search returned an invalid JSON response.")
    return redact_keys(body)


def append_results(
    response: SearchResponse,
    items: Any,
    num_results: int,
    start: int = 0,
    restriction: tuple[str, str] | None = None,
) -> None:
    if not isinstance(items, list):
        raise WebSearchError(f"{response.provider.title()} search returned an invalid results list.")
    validated_results: list[SearchResult] = []
    seen = {result["link"] for result in response.results}
    remaining = num_results - len(response.results)
    for index, item in enumerate(items):
        if len(validated_results) >= remaining:
            break
        if not isinstance(item, dict):
            raise WebSearchError(f"{response.provider.title()} search returned an invalid result.")
        link = item.get("link") if response.provider == "google" else item.get("url")
        if not isinstance(link, str):
            raise WebSearchError(f"{response.provider.title()} search returned a result without a URL.")
        parsed = _http_url(link)
        if parsed is None:
            raise WebSearchError(f"{response.provider.title()} search returned an invalid result URL.")
        if restriction is not None and not _within_site(parsed, restriction):
            response.withheld_count += 1
            continue
        if link in seen:
            continue
        seen.add(link)
        title = item.get("title")
        snippet = item.get("snippet") if response.provider == "google" else ""
        display_link = item.get("displayed_link")
        validated_results.append(
            SearchResult(
                title=title if isinstance(title, str) else "",
                link=link,
                snippet=snippet if isinstance(snippet, str) else "",
                display_link=display_link if isinstance(display_link, str) else (parsed.hostname or ""),
                position=start + index + 1,
            )
        )

    response.results.extend(validated_results)


async def google_search(response: SearchResponse, num_results: int, max_pages: int = 10) -> None:
    if not settings.SERPAPI_API_KEY:
        raise WebSearchError("Google search is not configured on this server (SERPAPI_API_KEY is not set).")
    restriction = _site_restriction(response.query)
    refetches = 0
    refetch_stopped_reason = None
    start = 0
    try:
        for page_index in range(max_pages):
            params = {"engine": "google", "q": response.query, "start": start, "api_key": settings.SERPAPI_API_KEY}
            body = await request(response, "google", f"https://serpapi.com/search.json?{urlencode(params)}")
            response.pages.append(body)
            metadata = body.get("search_metadata")
            if not isinstance(metadata, dict) or metadata.get("status") != "Success":
                raise WebSearchError("Google search did not complete successfully.")
            items = body.get("organic_results", [])
            outside_site = restriction is not None and _all_results_outside_site(items, restriction)
            while outside_site and refetches < 2:
                refetches += 1
                query = urlencode({**params, "no_cache": "true"})
                page_status = (response.http_status, response.http_status_provider)
                try:
                    fresh_body = await request(response, "google", f"https://serpapi.com/search.json?{query}")
                except Exception:  # noqa: BLE001
                    # The page already in hand is what gets reported, so its status stays the reported one.
                    response.http_status, response.http_status_provider = page_status
                    refetch_stopped_reason = "request_failed"
                    break
                metadata = fresh_body.get("search_metadata")
                if not isinstance(metadata, dict) or metadata.get("status") != "Success":
                    refetch_stopped_reason = "status_not_success"
                    break
                body = fresh_body
                response.pages[-1] = body
                items = body.get("organic_results", [])
                outside_site = restriction is not None and _all_results_outside_site(items, restriction)
            if outside_site and refetches == 2 and refetch_stopped_reason is None:
                refetch_stopped_reason = "budget_spent"
            append_results(response, items, num_results, start, restriction)
            last_page = page_index == max_pages - 1
            if last_page or outside_site or not items or len(response.results) >= num_results:
                return
            pagination = body.get("serpapi_pagination")
            next_page = pagination.get("next") if isinstance(pagination, dict) else None
            if not isinstance(next_page, str):
                return
            try:
                next_start = int(parse_qs(urlsplit(next_page).query)["start"][0])
            except (KeyError, IndexError, ValueError):
                raise WebSearchError("Google search returned invalid pagination metadata.") from None
            if next_start <= start or next_start > 1000:
                raise WebSearchError("Google search returned a non-advancing page offset.")
            start = next_start
    finally:
        if refetches > 0 or response.withheld_count > 0:
            with contained_effect("log Google search site restriction"):
                LOG.info(
                    "Google search site restriction applied",
                    refetches=refetches,
                    withheld_count=response.withheld_count,
                    results_returned=len(response.results),
                    refetch_stopped_reason=refetch_stopped_reason,
                )


async def exa_search(response: SearchResponse, num_results: int) -> None:
    if not settings.EXA_API_KEY:
        raise WebSearchError("Exa search is not configured on this server (EXA_API_KEY is not set).")
    query = response.query
    payload: dict[str, Any] = {
        "query": query,
        "type": "auto",
        "numResults": num_results,
    }
    site_tokens = [token for token in query.split() if re.search(r"(?<!\w)site:", token, re.IGNORECASE)]
    if site_tokens:
        if len(site_tokens) != 1 or not re.fullmatch(r"site:([a-z0-9-]+\.)+[a-z0-9-]+", site_tokens[0], re.IGNORECASE):
            raise WebSearchError(
                "Exa supports a single site:domain filter. Select Google for other site expressions.",
                FailureCategory.PARAMETER_BINDING_ERROR,
            )
        payload["includeDomains"] = [site_tokens[0][5:]]
        payload["query"] = query.replace(site_tokens[0], "", 1).strip()
        if not payload["query"]:
            raise WebSearchError(
                "Add search terms after the site:domain filter for Exa.", FailureCategory.PARAMETER_BINDING_ERROR
            )
    body = await request(response, "exa", "https://api.exa.ai/search", payload)
    response.pages.append(body)
    if body.get("error"):
        raise WebSearchError("Exa search did not complete successfully.")
    append_results(response, body.get("results"), num_results)
    if not response.results:
        return
    # The snippet lookup is best-effort; the reported status stays the search's.
    search_status = (response.http_status, response.http_status_provider)
    try:
        contents = await request(
            response,
            "exa",
            "https://api.exa.ai/contents",
            {
                "urls": [result["link"] for result in response.results],
                "highlights": {"maxCharacters": 1000, "query": payload["query"]},
                "maxAgeHours": -1,
            },
        )
        if contents.get("error"):
            raise WebSearchError("Exa highlights request did not complete successfully.")
        items = contents.get("results")
        if not isinstance(items, list):
            raise WebSearchError("Exa highlights request returned an invalid results list.")
        snippets = {result["link"]: "" for result in response.results}
        unmatched_count = 0
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                raise WebSearchError("Exa highlights request returned an invalid result URL.")
            if item["url"] in snippets:
                highlights = item.get("highlights")
                snippets[item["url"]] = (
                    "\n".join(text for text in highlights if isinstance(text, str))
                    if isinstance(highlights, list)
                    else ""
                )
            else:
                unmatched_count += 1
    except Exception as exc:  # noqa: BLE001
        with contained_effect("log Exa highlights failure"):
            LOG.warning(
                "Exa highlights request failed; results keep empty snippets",
                error_type=type(exc).__name__,
                reason=str(exc) if isinstance(exc, (WebSearchError, TimeoutError)) else None,
            )
        return
    finally:
        response.http_status, response.http_status_provider = search_status
    for result in response.results:
        result["snippet"] = snippets[result["link"]]
    if unmatched_count:
        with contained_effect("log Exa highlights mismatch"):
            LOG.warning(
                "Exa highlights returned pages that match no search result",
                unmatched_count=unmatched_count,
                result_count=len(response.results),
            )


async def search(response: SearchResponse, provider: ProviderChoice, num_results: int, max_pages: int = 10) -> None:
    """Under "auto", a Google failure that returned nothing falls back to Exa; one that already
    returned results raises with them kept on `response`."""
    if provider == "exa":
        await exa_search(response, num_results)
        return
    try:
        await google_search(response, num_results, max_pages)
    except (TimeoutError, WebSearchError) as exc:
        if provider != "auto" or response.results or not settings.EXA_API_KEY:
            raise
        response.fallback_reason = str(exc) or "Google search timed out after 30 seconds."
        response.provider = "exa"
        response.pages.clear()
        await exa_search(response, num_results)
