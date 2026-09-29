import asyncio

import pytest

from skyvern.forge.sdk.browser_egress_policy import DestinationBlockedError
from skyvern.forge.sdk.settings_manager import SettingsManager
from skyvern.forge.sdk.workflow import web_search
from skyvern.forge.sdk.workflow.web_search import search_web
from tests.unit.conftest import SearchApiReply, arm_search_api, serpapi_page

FIRST, SECOND, THIRD = "https://first.example/about", "https://second.example/", "https://third.example/team"
PAGE = serpapi_page(FIRST, SECOND, THIRD)


@pytest.mark.asyncio
async def test_a_served_search_reports_the_api_status_and_its_results(monkeypatch: pytest.MonkeyPatch) -> None:
    arm_search_api(monkeypatch, (200, PAGE))

    observed = await search_web("roofing")

    assert observed == {
        "provider": "google",
        "http_status": 200,
        "error_kind": None,
        "page_title": "",
        "results": [
            {"title": f"Title {link}", "url": link, "snippet": f"About {link}"} for link in (FIRST, SECOND, THIRD)
        ],
        "extracted_count": 3,
        "withheld_count": 0,
        "capture_truncated": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("google_status", "exa_key", "query", "exa_reply"),
    [
        (401, None, "roofing", None),
        (429, "exa-test-key", "roofing site:example.com/path", None),
        (401, "exa-test-key", "roofing", TimeoutError()),
    ],
    ids=["exa-unconfigured", "exa-rejects-site-path", "exa-never-completes"],
)
async def test_a_failed_fallback_reports_the_provider_and_status_of_the_last_completed_request(
    monkeypatch: pytest.MonkeyPatch,
    google_status: int,
    exa_key: str | None,
    query: str,
    exa_reply: BaseException | None,
) -> None:
    replies = [(google_status, {"error": "refused"}), *([exa_reply] if exa_reply else [])]
    arm_search_api(monkeypatch, *replies, exa_key=exa_key)

    observed = await search_web(query)

    assert (observed["provider"], observed["http_status"]) == ("google", google_status)
    assert observed["error_kind"] is not None
    assert observed["results"] == []


@pytest.mark.asyncio
async def test_a_malformed_body_reports_the_status_it_was_served_with(monkeypatch: pytest.MonkeyPatch) -> None:
    arm_search_api(monkeypatch, (200, ["not", "an", "object"]))

    observed = await search_web("roofing")

    assert observed["error_kind"] == "WebSearchError"
    assert observed["http_status"] == 200


@pytest.mark.asyncio
async def test_a_request_that_never_completes_reports_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    arm_search_api(monkeypatch, TimeoutError(), exa_key="exa-test-key")

    observed = await search_web("roofing")

    assert observed["error_kind"] == "TimeoutError"
    assert observed["http_status"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stalls", ["search", "admission"])
async def test_the_whole_call_is_bounded(monkeypatch: pytest.MonkeyPatch, stalls: str) -> None:
    async def never_answers(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    arm_search_api(monkeypatch, (200, PAGE))
    if stalls == "search":
        monkeypatch.setattr(web_search.web_search_client, "aiohttp_request", never_answers)
    else:
        monkeypatch.setattr(web_search, "classify_url_async", never_answers)
    monkeypatch.setattr(web_search, "SEARCH_TIMEOUT_SECONDS", 0.05)

    observed = await asyncio.wait_for(search_web("roofing"), timeout=5)

    assert observed["error_kind"] == "TimeoutError"
    assert (observed["results"], observed["extracted_count"], observed["withheld_count"]) == ([], 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("contents_reply", "snippet"),
    [((200, {"results": [{"url": SECOND, "highlights": ["about second"]}]}), "about second"), ((500, {}), "")],
    ids=["highlights", "highlights-fail"],
)
async def test_google_failing_falls_back_to_exa(
    monkeypatch: pytest.MonkeyPatch, contents_reply: SearchApiReply, snippet: str
) -> None:
    exa_body = {"results": [{"url": SECOND, "title": "Second"}]}
    api = arm_search_api(monkeypatch, (500, {}), (200, exa_body), contents_reply, exa_key="exa-test-key")

    observed = await search_web("roofing")

    assert observed["provider"] == "exa"
    assert observed["http_status"] == 200
    assert observed["error_kind"] is None
    assert observed["results"] == [{"title": "Second", "url": SECOND, "snippet": snippet}]
    assert api.urls == [api.urls[0], "https://api.exa.ai/search", "https://api.exa.ai/contents"]


@pytest.mark.asyncio
async def test_exa_alone_serves_a_deployment_without_a_serpapi_key(monkeypatch: pytest.MonkeyPatch) -> None:
    exa_body = {"results": [{"url": FIRST, "title": "First", "highlights": []}]}
    arm_search_api(monkeypatch, (200, exa_body), serpapi_key=None, exa_key="exa-test-key")

    observed = await search_web("roofing")

    assert observed["provider"] == "exa"
    assert [result["url"] for result in observed["results"]] == [FIRST]


@pytest.mark.asyncio
async def test_a_call_bills_one_google_page_whatever_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    first_page = serpapi_page(*(f"https://site{index}.example/" for index in range(10)), next_start=10)
    api = arm_search_api(monkeypatch, (200, first_page), (200, first_page))

    observed = await search_web("roofing", max_results=100)

    assert len(api.urls) == 1
    assert len(observed["results"]) == 10


@pytest.mark.asyncio
async def test_off_site_results_count_as_withheld_not_as_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    off_site = serpapi_page("https://elsewhere.test/a", "https://elsewhere.test/b")
    arm_search_api(monkeypatch, (200, off_site), (200, off_site), (200, off_site))

    observed = await search_web("site:example.com roofing")

    assert observed["error_kind"] is None
    assert (observed["results"], observed["extracted_count"], observed["withheld_count"]) == ([], 2, 2)


@pytest.mark.asyncio
async def test_unused_pagination_metadata_cannot_drop_the_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    short_page = serpapi_page("https://site.example/a", next_start=0)
    arm_search_api(monkeypatch, (200, short_page))

    observed = await search_web("roofing")

    assert (observed["error_kind"], len(observed["results"])) == (None, 1)


@pytest.mark.asyncio
async def test_a_failed_site_refetch_keeps_the_status_of_the_page_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    mixed = serpapi_page("https://elsewhere.test/a")
    arm_search_api(monkeypatch, (200, mixed), (429, {}))

    observed = await search_web("site:example.com roofing")

    assert (observed["http_status"], observed["error_kind"]) == (200, None)


@pytest.mark.asyncio
async def test_search_web_switched_off_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    api = arm_search_api(monkeypatch, (200, PAGE))
    monkeypatch.setattr(SettingsManager.get_settings(), "ENABLE_SEARCH_WEB", False)

    observed = await search_web("roofing")

    assert api.urls == []
    assert observed["error_kind"] == "not_configured"


@pytest.mark.asyncio
async def test_a_withheld_result_is_replaced_from_the_fetched_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    async def block_the_first(url: str) -> str | None:
        return "blocked" if url == FIRST else None

    arm_search_api(monkeypatch, (200, PAGE))
    monkeypatch.setattr(web_search, "classify_url_async", block_the_first)

    observed = await search_web("roofing", max_results=2)

    assert [result["url"] for result in observed["results"]] == [SECOND, THIRD]
    assert observed["extracted_count"] == 3
    assert observed["withheld_count"] == 1


@pytest.mark.asyncio
async def test_withheld_results_are_counted_rather_than_read_as_an_empty_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def block_everything(_url: str) -> str | None:
        return "blocked egress to internal address"

    arm_search_api(monkeypatch, (200, PAGE))
    monkeypatch.setattr(web_search, "classify_url_async", block_everything)

    observed = await search_web("roofing")

    assert observed["results"] == []
    assert observed["extracted_count"] == 3
    assert observed["withheld_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [UnicodeError("label too long"), DestinationBlockedError("blocked egress: URL has no host")]
)
async def test_one_unclassifiable_result_does_not_discard_its_siblings(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    async def raise_for_the_first(url: str) -> str | None:
        if url == FIRST:
            raise failure
        return None

    arm_search_api(monkeypatch, (200, PAGE))
    monkeypatch.setattr(web_search, "classify_url_async", raise_for_the_first)

    observed = await search_web("roofing")

    assert [result["url"] for result in observed["results"]] == [SECOND, THIRD]
    assert observed["withheld_count"] == 1


@pytest.mark.asyncio
async def test_the_api_key_never_reaches_the_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    page = serpapi_page(FIRST)
    page["organic_results"][0]["snippet"] = "echoed serp-test-key back"
    arm_search_api(monkeypatch, (200, page))

    observed = await search_web("roofing")

    assert "serp-test-key" not in repr(observed)


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_says_so_rather_than_reporting_an_empty_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = arm_search_api(monkeypatch, (200, PAGE), serpapi_key=None, exa_key=None)

    observed = await search_web("roofing")

    assert observed["error_kind"] == "not_configured"
    assert observed["results"] == []
    assert api.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, 7, "", "   "])
async def test_an_unusable_query_is_rejected_before_anything_is_fetched(
    monkeypatch: pytest.MonkeyPatch, query: object
) -> None:
    api = arm_search_api(monkeypatch, (200, PAGE))

    with pytest.raises(ValueError):
        await search_web(query)  # type: ignore[arg-type]

    assert api.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("max_results", [0, -1, 101, "3", 2.0, True, None])
async def test_an_unusable_result_limit_is_rejected(monkeypatch: pytest.MonkeyPatch, max_results: object) -> None:
    arm_search_api(monkeypatch, (200, PAGE))

    with pytest.raises(ValueError):
        await search_web("roofing", max_results)  # type: ignore[arg-type]
