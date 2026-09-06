import pytest

from skyvern.forge.sdk.browser_egress_policy import DestinationBlockedError
from skyvern.forge.sdk.workflow import web_search
from skyvern.forge.sdk.workflow.web_search import (
    FetchedPage,
    RunBrowserTransport,
    SearchResult,
    search_web,
)
from tests.unit.conftest import FakeSearchBrowserContext

ENDPOINT = "https://search.example.test/html/"
RESULTS = [
    SearchResult(title="First", url="https://first.example/about", snippet="first snippet"),
    SearchResult(title="Second", url="https://second.example/", snippet="second snippet"),
    SearchResult(title="Third", url="https://third.example/team", snippet="third snippet"),
]


class StubProvider:
    """Stands in for a deployment's configured engine. Extraction is the provider's job, so the
    orchestration is tested against a fixed result set rather than any engine's markup."""

    name = "stub"

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self._results = RESULTS if results is None else results

    def result_page_url(self, query: str) -> str:
        return f"{ENDPOINT}?q={query}"

    def extract_results(self, html: str) -> list[SearchResult]:
        return list(self._results) if html else []


class StubTransport:
    def __init__(self, page: FetchedPage) -> None:
        self._page = page
        self.requested_url: str | None = None

    async def fetch(self, url: str) -> FetchedPage:
        self.requested_url = url
        return self._page


def served(*, status: int | None = 200, truncated: bool = False, title: str = "results") -> StubTransport:
    return StubTransport(
        FetchedPage(url=ENDPOINT, title=title, html="<html>page</html>", http_status=status, truncated=truncated)
    )


@pytest.fixture(autouse=True)
def allow_every_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow(_url: str) -> str | None:
        return None

    monkeypatch.setattr(web_search, "classify_url_async", allow)


@pytest.mark.asyncio
async def test_a_page_that_was_not_served_extracts_nothing_and_says_why() -> None:
    """An error page carries no results, and reporting that as "no matches" is the false
    negative this helper exists to remove."""
    observed = await search_web(StubProvider(), served(status=503), "anything")

    assert observed["http_status"] == 503
    assert observed["results"] == []
    assert observed["extracted_count"] == 0


@pytest.mark.asyncio
async def test_a_transport_failure_reports_its_kind_not_an_empty_result_set() -> None:
    transport = StubTransport(FetchedPage(url=ENDPOINT, title="", html="", error_kind="TimeoutError"))

    observed = await search_web(StubProvider(), transport, "anything")

    assert observed["error_kind"] == "TimeoutError"
    assert observed["extracted_count"] == 0


@pytest.mark.asyncio
async def test_a_transport_that_reports_no_status_still_extracts() -> None:
    observed = await search_web(StubProvider(), served(status=None), "anything")

    assert observed["http_status"] is None
    assert observed["extracted_count"] == 3


@pytest.mark.asyncio
async def test_a_page_carrying_no_results_is_reported_with_its_title_and_no_verdict() -> None:
    observed = await search_web(StubProvider(results=[]), served(title="Just a moment..."), "anything")

    assert observed["results"] == []
    assert observed["extracted_count"] == 0
    assert observed["withheld_count"] == 0
    assert observed["page_title"] == "Just a moment..."


@pytest.mark.asyncio
async def test_a_truncated_capture_is_reported() -> None:
    observed = await search_web(StubProvider(), served(truncated=True), "anything")

    assert observed["capture_truncated"] is True


@pytest.mark.asyncio
async def test_withheld_results_are_counted_rather_than_read_as_an_empty_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def block_every_result(url: str) -> str | None:
        return None if url.startswith(ENDPOINT) else "blocked egress to internal address"

    monkeypatch.setattr(web_search, "classify_url_async", block_every_result)

    observed = await search_web(StubProvider(), served(), "anything")

    assert observed["results"] == []
    assert observed["extracted_count"] == 3
    assert observed["withheld_count"] == 3


@pytest.mark.asyncio
async def test_the_caller_limit_is_filled_from_later_results_when_an_earlier_one_is_withheld(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission runs before the limit, so a withheld result costs the caller nothing and both
    execution paths return the same set for the same arguments."""

    async def block_the_first(url: str) -> str | None:
        return "blocked" if url == "https://first.example/about" else None

    monkeypatch.setattr(web_search, "classify_url_async", block_the_first)

    observed = await search_web(StubProvider(), served(), "anything", max_results=2)

    assert [result["url"] for result in observed["results"]] == [
        "https://second.example/",
        "https://third.example/team",
    ]
    assert observed["withheld_count"] == 1


@pytest.mark.asyncio
async def test_one_unresolvable_result_does_not_discard_its_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname the resolver cannot encode raises UnicodeError, which is not an OSError and
    escapes the shared classifier. It drops that result, not the whole search."""

    async def raise_for_the_first(url: str) -> str | None:
        if url == "https://first.example/about":
            raise UnicodeError("label too long")
        return None

    monkeypatch.setattr(web_search, "classify_url_async", raise_for_the_first)

    observed = await search_web(StubProvider(), served(), "anything")

    assert [result["url"] for result in observed["results"]] == [
        "https://second.example/",
        "https://third.example/team",
    ]
    assert observed["withheld_count"] == 1


@pytest.mark.asyncio
async def test_a_result_the_classifier_rejects_outright_is_withheld_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def reject(url: str) -> str | None:
        if url.startswith(ENDPOINT):
            return None
        raise DestinationBlockedError("blocked egress: URL has no host")

    monkeypatch.setattr(web_search, "classify_url_async", reject)

    observed = await search_web(StubProvider(), served(), "anything")

    assert observed["results"] == []
    assert observed["withheld_count"] == 3


@pytest.mark.asyncio
async def test_no_limit_returns_every_admitted_result() -> None:
    """The secure runner cannot carry the caller's limit across the operation boundary, so it
    admits them all and the sandbox slices the admitted set."""
    observed = await search_web(StubProvider(), served(), "anything", max_results=None)

    assert len(observed["results"]) == 3


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_says_so_rather_than_reporting_an_empty_search() -> None:
    observed = await search_web(None, served(), "anything")

    assert observed["error_kind"] == "not_configured"
    assert observed["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, 7, "", "   "])
async def test_an_unusable_query_is_rejected_before_anything_is_fetched(query: object) -> None:
    transport = served()

    with pytest.raises(ValueError):
        await search_web(StubProvider(), transport, query)  # type: ignore[arg-type]

    assert transport.requested_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("max_results", [0, -1, "3", 2.0, True])
async def test_an_unusable_result_limit_is_rejected(max_results: object) -> None:
    with pytest.raises(ValueError):
        await search_web(StubProvider(), served(), "anything", max_results)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_provider_builds_the_url_the_transport_fetches() -> None:
    transport = served()

    await search_web(StubProvider(), transport, "roofing")

    assert transport.requested_url == f"{ENDPOINT}?q=roofing"


@pytest.mark.asyncio
async def test_the_run_browser_transport_reports_the_served_status_and_closes_its_tab() -> None:
    context = FakeSearchBrowserContext(html="<html>page</html>", page_title="results", http_status=200)

    page = await RunBrowserTransport(context).fetch(ENDPOINT)

    assert page.http_status == 200
    assert page.title == "results"
    assert context.page.closed is True


@pytest.mark.asyncio
async def test_the_run_browser_transport_reports_a_navigation_failure_without_its_message() -> None:
    context = FakeSearchBrowserContext(goto_error=TimeoutError("navigating to https://engine/?q=secret"))

    page = await RunBrowserTransport(context).fetch(ENDPOINT)

    assert page.error_kind == "TimeoutError"
    assert "secret" not in repr(page)
    assert context.page.closed is True


@pytest.mark.asyncio
async def test_a_blocked_endpoint_is_reported_and_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint is screened by the same policy as the results, before any navigation."""

    async def block_the_endpoint(url: str) -> str | None:
        return "blocked egress to internal address" if url.startswith(ENDPOINT) else None

    monkeypatch.setattr(web_search, "classify_url_async", block_the_endpoint)
    transport = served()

    observed = await search_web(StubProvider(), transport, "anything")

    assert observed["error_kind"] == "endpoint_blocked"
    assert observed["results"] == []
    assert transport.requested_url is None


@pytest.mark.asyncio
async def test_the_caller_limit_truncates_a_longer_admitted_set() -> None:
    """Pins the inline slice: without it the caller asking for one result gets three."""
    observed = await search_web(StubProvider(), served(), "anything", max_results=1)

    assert [result["url"] for result in observed["results"]] == ["https://first.example/about"]
    assert observed["extracted_count"] == 3
    assert observed["withheld_count"] == 0
