"""``scrape_website`` timeout classification must follow the per-run selected browser engine's
timeout family, not a hard-coded stock-Playwright ``isinstance`` — so a run pinned to a different
engine still routes its native page-analysis timeouts to PAGE_LOAD_TIMEOUT, while a foreign
exception (including another engine's timeout) is not misclassified as a timeout.

These stay driver-agnostic: they pin fake engine selections and mock ``scrape_web_unsafe`` so they
hold on an image shipping only stock Playwright.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from skyvern.config import settings
from skyvern.exceptions import NoElementFound, ScrapingFailed, ScrapingFailedBlankPage, SkyvernPageAnalysisTimeout
from skyvern.webeye.browser_engine import BrowserEngineMetadata, BrowserEngineSelection
from skyvern.webeye.scraper import scraper
from skyvern.webeye.scraper.scraped_page import ScrapedPage
from tests.unit.scoped_asyncio import ScopedAsyncio

_TIMEOUT_REASON_MARKER = "page-analysis timeout"


class _EngineAError(Exception):
    pass


class _EngineATimeout(_EngineAError):
    pass


async def _never_start():  # pragma: no cover - never awaited in these tests
    raise AssertionError("start_driver must not be called")


def _selection(name: str, error_type: type[BaseException], timeout_type: type[BaseException]) -> BrowserEngineSelection:
    return BrowserEngineSelection(
        name=name,
        start_driver=_never_start,
        error_type=error_type,
        timeout_error_type=timeout_type,
        metadata=BrowserEngineMetadata(name=name, version="0.0.0"),
        selection_reason="test",
    )


def _browser_state(selection: BrowserEngineSelection | None) -> SimpleNamespace:
    return SimpleNamespace(engine_selection=selection, get_working_page=AsyncMock(return_value=None))


async def _run_scrape_and_capture(browser_state: SimpleNamespace, error: BaseException) -> ScrapingFailed:
    with patch.object(scraper, "scrape_web_unsafe", AsyncMock(side_effect=error)):
        with pytest.raises(ScrapingFailed) as exc_info:
            await scraper.scrape_website(
                browser_state,  # type: ignore[arg-type]
                "https://example.com/path?token=secret",
                cleanup_element_tree=AsyncMock(),
                max_retries=0,
            )
    return exc_info.value


def _scraped_page() -> ScrapedPage:
    return ScrapedPage(
        elements=[],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=MagicMock(),
        _clean_up_func=AsyncMock(),
        _scrape_exclude=None,
    )


class TestScrapeWebsiteEmptyTreeRecovery:
    def _rig(
        self,
        monkeypatch: pytest.MonkeyPatch,
        results: list[object],
        *,
        support_empty_page: bool = False,
    ) -> SimpleNamespace:
        page = SimpleNamespace(goto=AsyncMock(), url="https://example.test/path")
        browser_state = SimpleNamespace(
            engine_selection=None,
            get_working_page=AsyncMock(return_value=None),
            must_get_working_page=AsyncMock(return_value=page),
        )
        cleanup_element_tree = AsyncMock()
        scrape_exclude = AsyncMock()
        scrape_web_unsafe = AsyncMock(side_effect=results)
        sleep = AsyncMock()
        log = MagicMock()
        monkeypatch.setattr(scraper, "scrape_web_unsafe", scrape_web_unsafe)
        monkeypatch.setattr(scraper, "asyncio", ScopedAsyncio(sleep=sleep))
        monkeypatch.setattr(scraper, "LOG", log)

        unsafe_kwargs = {
            "browser_state": browser_state,
            "url": "https://example.test/path",
            "cleanup_element_tree": cleanup_element_tree,
            "scrape_exclude": scrape_exclude,
            "take_screenshots": False,
            "draw_boxes": False,
            "max_screenshot_number": 2,
            "scroll": False,
            "support_empty_page": support_empty_page,
            "wait_seconds": 1.25,
            "must_included_tags": ["button"],
            "allow_transient_ui_suppression": True,
        }
        return SimpleNamespace(
            browser_state=browser_state,
            log=log,
            page=page,
            scrape_web_unsafe=scrape_web_unsafe,
            sleep=sleep,
            unsafe_kwargs=unsafe_kwargs,
            website_kwargs={**unsafe_kwargs, "max_retries": 0},
        )

    @pytest.mark.asyncio
    async def test_settle_rescrape_returns_without_refetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scraped_page = _scraped_page()
        rig = self._rig(monkeypatch, [NoElementFound(), scraped_page])

        result = await scraper.scrape_website(**rig.website_kwargs)

        assert result is scraped_page
        assert rig.scrape_web_unsafe.await_args_list == [call(**rig.unsafe_kwargs), call(**rig.unsafe_kwargs)]
        rig.page.goto.assert_not_awaited()
        rig.sleep.assert_awaited_once_with(3)
        assert rig.log.info.call_args_list == [
            call("Retrying scrape after empty element tree", url=rig.unsafe_kwargs["url"], attempt=1)
        ]

    @pytest.mark.asyncio
    async def test_refetch_rescrape_returns_after_one_refetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scraped_page = _scraped_page()
        rig = self._rig(monkeypatch, [NoElementFound(), NoElementFound(), scraped_page])

        result = await scraper.scrape_website(**rig.website_kwargs)

        assert result is scraped_page
        assert rig.scrape_web_unsafe.await_args_list == [call(**rig.unsafe_kwargs)] * 3
        rig.browser_state.must_get_working_page.assert_awaited_once_with()
        rig.page.goto.assert_awaited_once_with("https://example.test/path", timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
        assert rig.sleep.await_args_list == [call(3), call(3)]
        assert rig.log.info.call_args_list == [
            call("Retrying scrape after empty element tree", url=rig.unsafe_kwargs["url"], attempt=1),
            call("Retrying scrape after empty element tree", url=rig.unsafe_kwargs["url"], attempt=2),
        ]

    @pytest.mark.asyncio
    async def test_terminal_empty_tree_wraps_like_any_scrape_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        errors = [NoElementFound(), NoElementFound(), NoElementFound()]
        rig = self._rig(monkeypatch, errors)
        monkeypatch.setattr(scraper, "build_scraping_failed_reason", AsyncMock(return_value="reason"))

        with pytest.raises(ScrapingFailed) as exc_info:
            await scraper.scrape_website(**rig.website_kwargs)

        assert exc_info.value.__cause__ is errors[-1]
        assert rig.scrape_web_unsafe.await_args_list == [call(**rig.unsafe_kwargs)] * 3
        rig.page.goto.assert_awaited_once_with("https://example.test/path", timeout=settings.BROWSER_LOADING_TIMEOUT_MS)
        assert rig.sleep.await_args_list == [call(3), call(3)]

    @pytest.mark.asyncio
    async def test_blank_page_failure_bypasses_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = ScrapingFailedBlankPage()
        rig = self._rig(monkeypatch, [error])

        with pytest.raises(ScrapingFailedBlankPage) as exc_info:
            await scraper.scrape_website(**rig.website_kwargs)

        assert exc_info.value is error
        rig.scrape_web_unsafe.assert_awaited_once_with(**rig.unsafe_kwargs)
        rig.browser_state.must_get_working_page.assert_not_awaited()
        rig.page.goto.assert_not_awaited()
        rig.sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_support_empty_page_returns_without_recovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scraped_page = _scraped_page()
        rig = self._rig(monkeypatch, [scraped_page], support_empty_page=True)

        result = await scraper.scrape_website(**rig.website_kwargs)

        assert result is scraped_page
        rig.scrape_web_unsafe.assert_awaited_once_with(**rig.unsafe_kwargs)
        rig.browser_state.must_get_working_page.assert_not_awaited()
        rig.page.goto.assert_not_awaited()
        rig.sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_selected_engine_native_timeout_is_classified_as_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), _EngineATimeout("deadline exceeded"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")
    assert isinstance(failure.__cause__, _EngineATimeout)


@pytest.mark.asyncio
async def test_skyvern_page_analysis_timeout_is_engine_neutral() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(
        _browser_state(selection), SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page")
    )
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")
    assert isinstance(failure.__cause__, SkyvernPageAnalysisTimeout)


@pytest.mark.asyncio
async def test_incremental_element_tree_retries_without_wait_after_skyvern_page_analysis_timeout() -> None:
    frame = SimpleNamespace(url="https://example.com")
    get_incremental_element_tree = AsyncMock(
        side_effect=[
            SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"),
            ([], []),
        ]
    )
    skyvern_frame = SimpleNamespace(
        get_frame=lambda: frame,
        get_incremental_element_tree=get_incremental_element_tree,
    )
    cleanup_element_tree = AsyncMock(return_value=[])

    incremental_page = scraper.IncrementalScrapePage(skyvern_frame=skyvern_frame)  # type: ignore[arg-type]
    result = await incremental_page.get_incremental_element_tree(cleanup_element_tree)

    assert result == []
    assert get_incremental_element_tree.await_args_list == [
        call(wait_until_finished=True),
        call(wait_until_finished=False),
    ]
    cleanup_element_tree.assert_awaited_once_with(frame, frame.url, [])


def _incremental_page(
    selection: BrowserEngineSelection | None, side_effect: list
) -> tuple[scraper.IncrementalScrapePage, AsyncMock]:
    frame = SimpleNamespace(url="https://example.com")
    get_incremental_element_tree = AsyncMock(side_effect=side_effect)
    skyvern_frame = SimpleNamespace(
        get_frame=lambda: frame,
        get_incremental_element_tree=get_incremental_element_tree,
    )
    page = scraper.IncrementalScrapePage(skyvern_frame=skyvern_frame, engine_selection=selection)  # type: ignore[arg-type]
    return page, get_incremental_element_tree


@pytest.mark.asyncio
async def test_incremental_tree_retries_once_on_selected_engine_native_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [_EngineATimeout("deadline exceeded"), ([], [])])
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_does_not_retry_on_foreign_timeout_under_nonplaywright_selection() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [PlaywrightTimeoutError("pw timeout"), ([], [])])
    with pytest.raises(PlaywrightTimeoutError):
        await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert get_tree.await_args_list == [call(wait_until_finished=True)]


@pytest.mark.asyncio
async def test_incremental_tree_retries_on_stock_timeout_when_selection_missing() -> None:
    page, get_tree = _incremental_page(None, [PlaywrightTimeoutError("pw timeout"), ([], [])])
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_retries_on_skyvern_timeout_under_nonplaywright_selection() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(
        selection, [SkyvernPageAnalysisTimeout("Skyvern timed out trying to analyze the page"), ([], [])]
    )
    result = await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert result == []
    assert get_tree.await_args_list == [call(wait_until_finished=True), call(wait_until_finished=False)]


@pytest.mark.asyncio
async def test_incremental_tree_propagates_non_timeout_without_retry() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    page, get_tree = _incremental_page(selection, [ValueError("boom"), ([], [])])
    with pytest.raises(ValueError):
        await page.get_incremental_element_tree(AsyncMock(return_value=[]))
    assert get_tree.await_args_list == [call(wait_until_finished=True)]


def test_resolve_engine_selection_for_task_reads_live_browser_state() -> None:
    from skyvern.webeye.browser_engine import resolve_engine_selection_for_task

    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    get_for_task = MagicMock(return_value=SimpleNamespace(engine_selection=selection))
    browser_manager = SimpleNamespace(get_for_task=get_for_task)
    assert resolve_engine_selection_for_task(task, browser_manager) is selection  # type: ignore[arg-type]
    get_for_task.assert_called_once_with("tsk_1", workflow_run_id="wr_1")


def test_resolve_engine_selection_for_task_returns_none_when_no_browser_state() -> None:
    from skyvern.webeye.browser_engine import resolve_engine_selection_for_task

    task = SimpleNamespace(task_id="tsk_1", workflow_run_id="wr_1")
    browser_manager = SimpleNamespace(get_for_task=MagicMock(return_value=None))
    assert resolve_engine_selection_for_task(task, browser_manager) is None  # type: ignore[arg-type]


def test_resolve_engine_selection_for_task_returns_none_without_task() -> None:
    from skyvern.webeye.browser_engine import resolve_engine_selection_for_task

    get_for_task = MagicMock()
    browser_manager = SimpleNamespace(get_for_task=get_for_task)
    assert resolve_engine_selection_for_task(None, browser_manager) is None  # type: ignore[arg-type]
    get_for_task.assert_not_called()


@pytest.mark.asyncio
async def test_foreign_engine_timeout_is_not_classified_as_timeout() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
    assert isinstance(failure.__cause__, PlaywrightTimeoutError)


@pytest.mark.asyncio
async def test_unrelated_exception_is_not_classified_as_timeout_and_is_not_swallowed() -> None:
    selection = _selection("engine-a", _EngineAError, _EngineATimeout)
    failure = await _run_scrape_and_capture(_browser_state(selection), ValueError("boom"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
    assert isinstance(failure.__cause__, ValueError)


@pytest.mark.asyncio
async def test_playwright_selection_preserves_stock_timeout_classification() -> None:
    selection = _selection("playwright", PlaywrightError, PlaywrightTimeoutError)
    failure = await _run_scrape_and_capture(_browser_state(selection), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")


@pytest.mark.asyncio
async def test_no_selection_falls_back_to_stock_playwright_timeout_identity() -> None:
    failure = await _run_scrape_and_capture(_browser_state(None), PlaywrightTimeoutError("pw timeout"))
    assert _TIMEOUT_REASON_MARKER in (failure.reason or "")


@pytest.mark.asyncio
async def test_no_selection_non_timeout_is_not_classified_as_timeout() -> None:
    failure = await _run_scrape_and_capture(_browser_state(None), PlaywrightError("navigated away"))
    assert _TIMEOUT_REASON_MARKER not in (failure.reason or "")
