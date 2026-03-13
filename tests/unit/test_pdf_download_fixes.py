"""
Unit tests for PDF download fixes:
1. Relaxed about:blank check in scraper (allows pages with child frames)
2. PDF iframe detection in ScrapedPage (Edge PDF interstitial pages)
"""

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skyvern.exceptions import ScrapingFailedBlankPage
from skyvern.webeye.scraper.scraped_page import ScrapedPage


# ---------------------------------------------------------------------------
# Helper: build a mock Playwright Page
# ---------------------------------------------------------------------------
def _make_mock_page(url: str = "https://example.com", child_frames: list | None = None) -> MagicMock:
    """Create a mock Playwright Page with configurable URL and child frames."""
    page = MagicMock()
    page.url = url
    main_frame = MagicMock()
    main_frame.child_frames = child_frames or []
    page.main_frame = main_frame
    return page


def _make_scraped_page(
    elements: list | None = None,
    browser_state: MagicMock | None = None,
) -> ScrapedPage:
    """Create a ScrapedPage with defaults suitable for testing."""
    if browser_state is None:
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=None)

    return ScrapedPage(
        elements=elements or [],
        element_tree=[],
        element_tree_trimmed=[],
        _browser_state=browser_state,
        _clean_up_func=AsyncMock(return_value=[]),
        _scrape_exclude=None,
    )


# ===================================================================
# Tests for Approach 1: Relaxed about:blank check in scraper
# ===================================================================
class TestAboutBlankCheck:
    """Tests for the relaxed about:blank check in scrape_web_unsafe."""

    @pytest.mark.asyncio
    async def test_about_blank_without_child_frames_raises(self) -> None:
        """about:blank pages with no child frames should still raise ScrapingFailedBlankPage."""
        page = _make_mock_page(url="about:blank", child_frames=[])
        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=page)

        from skyvern.webeye.scraper.scraper import scrape_web_unsafe

        with pytest.raises(ScrapingFailedBlankPage):
            await scrape_web_unsafe(
                browser_state=browser_state,
                url="",
                cleanup_element_tree=AsyncMock(return_value=[]),
            )

    @pytest.mark.asyncio
    async def test_about_blank_with_only_blank_child_frames_raises(self) -> None:
        """about:blank pages with only empty/blank child frames should still raise ScrapingFailedBlankPage."""
        blank_child = MagicMock()
        blank_child.url = "about:blank"
        empty_child = MagicMock()
        empty_child.url = ""
        none_child = MagicMock()
        none_child.url = None

        page = _make_mock_page(url="about:blank", child_frames=[blank_child, empty_child, none_child])
        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=page)

        from skyvern.webeye.scraper.scraper import scrape_web_unsafe

        with pytest.raises(ScrapingFailedBlankPage):
            await scrape_web_unsafe(
                browser_state=browser_state,
                url="",
                cleanup_element_tree=AsyncMock(return_value=[]),
            )

    @pytest.mark.asyncio
    async def test_about_blank_with_child_frames_does_not_raise(self) -> None:
        """about:blank pages WITH meaningful child frames should NOT raise (e.g., Edge PDF interstitial)."""
        child_frame = MagicMock()
        child_frame.url = "data:application/pdf;base64,JVBERi..."
        child_frame.is_detached.return_value = False

        page = _make_mock_page(url="about:blank", child_frames=[child_frame])
        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=page)

        from skyvern.webeye.scraper.scraper import scrape_web_unsafe

        with patch("skyvern.webeye.scraper.scraper.SkyvernFrame") as mock_skyvern_frame:
            mock_instance = AsyncMock()
            mock_instance.safe_wait_for_animation_end = AsyncMock()
            mock_skyvern_frame.create_instance = AsyncMock(return_value=mock_instance)

            with patch(
                "skyvern.webeye.scraper.scraper.get_interactable_element_tree",
                new_callable=AsyncMock,
                return_value=(
                    [{"id": "btn", "tagName": "button"}],
                    [{"id": "btn", "tagName": "button"}],
                ),
            ):
                # Should NOT raise ScrapingFailedBlankPage.
                # It may raise other exceptions downstream, but NOT ScrapingFailedBlankPage.
                try:
                    await scrape_web_unsafe(
                        browser_state=browser_state,
                        url="",
                        cleanup_element_tree=AsyncMock(return_value=[{"id": "btn", "tagName": "button"}]),
                    )
                except ScrapingFailedBlankPage:
                    pytest.fail("ScrapingFailedBlankPage should not be raised when child frames exist")
                except Exception:
                    # Other exceptions (e.g., screenshot-related) are expected in unit test context
                    pass

    @pytest.mark.asyncio
    async def test_about_blank_with_support_empty_page_does_not_raise(self) -> None:
        """about:blank with support_empty_page=True should not raise regardless of child frames."""
        page = _make_mock_page(url="about:blank", child_frames=[])
        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=page)

        from skyvern.webeye.scraper.scraper import scrape_web_unsafe

        with patch("skyvern.webeye.scraper.scraper.SkyvernFrame") as mock_skyvern_frame:
            mock_instance = AsyncMock()
            mock_instance.safe_wait_for_animation_end = AsyncMock()
            mock_skyvern_frame.create_instance = AsyncMock(return_value=mock_instance)

            with patch(
                "skyvern.webeye.scraper.scraper.get_interactable_element_tree",
                new_callable=AsyncMock,
                return_value=([], []),
            ):
                try:
                    await scrape_web_unsafe(
                        browser_state=browser_state,
                        url="",
                        cleanup_element_tree=AsyncMock(return_value=[]),
                        support_empty_page=True,
                    )
                except ScrapingFailedBlankPage:
                    pytest.fail("ScrapingFailedBlankPage should not be raised with support_empty_page=True")
                except Exception:
                    pass

    @pytest.mark.asyncio
    async def test_non_blank_page_not_affected(self) -> None:
        """Regular pages (non about:blank) should not be affected by the check."""
        page = _make_mock_page(url="https://example.com")
        browser_state = MagicMock()
        browser_state.must_get_working_page = AsyncMock(return_value=page)

        from skyvern.webeye.scraper.scraper import scrape_web_unsafe

        with patch("skyvern.webeye.scraper.scraper.SkyvernFrame") as mock_skyvern_frame:
            mock_instance = AsyncMock()
            mock_instance.safe_wait_for_animation_end = AsyncMock()
            mock_skyvern_frame.create_instance = AsyncMock(return_value=mock_instance)

            with patch(
                "skyvern.webeye.scraper.scraper.get_interactable_element_tree",
                new_callable=AsyncMock,
                return_value=([{"id": "1", "tagName": "button"}], [{"id": "1", "tagName": "button"}]),
            ):
                try:
                    await scrape_web_unsafe(
                        browser_state=browser_state,
                        url="",
                        cleanup_element_tree=AsyncMock(return_value=[{"id": "1", "tagName": "button"}]),
                    )
                except ScrapingFailedBlankPage:
                    pytest.fail("ScrapingFailedBlankPage should not be raised for non-blank pages")
                except Exception:
                    pass


# ===================================================================
# Tests for Approach 2: PDF iframe detection
# ===================================================================
class TestCheckPdfIframe:
    """Tests for ScrapedPage.check_pdf_iframe()."""

    @pytest.mark.asyncio
    async def test_no_page_returns_none(self) -> None:
        """Returns None when browser state has no working page."""
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=None)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result is None

    @pytest.mark.asyncio
    async def test_no_child_frames_returns_none(self) -> None:
        """Returns None when the page has no child frames."""
        page = _make_mock_page(url="about:blank", child_frames=[])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result is None

    @pytest.mark.asyncio
    async def test_child_frame_non_pdf_returns_none(self) -> None:
        """Returns None when child frames don't have PDF data URIs."""
        child = MagicMock()
        child.url = "https://example.com/page"
        page = _make_mock_page(url="about:blank", child_frames=[child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result is None

    @pytest.mark.asyncio
    async def test_child_frame_with_pdf_data_uri_returns_src(self) -> None:
        """Returns the data URI when a child frame has PDF content."""
        pdf_bytes = b"%PDF-1.4 test content"
        b64_data = base64.b64encode(pdf_bytes).decode()
        data_uri = f"data:application/pdf;base64,{b64_data}"

        child = MagicMock()
        child.url = data_uri
        page = _make_mock_page(url="about:blank", child_frames=[child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result == data_uri

    @pytest.mark.asyncio
    async def test_multiple_frames_returns_first_pdf(self) -> None:
        """Returns the first PDF data URI when multiple child frames exist."""
        non_pdf_child = MagicMock()
        non_pdf_child.url = "https://example.com/something"

        pdf_bytes = b"%PDF-1.4 content"
        b64_data = base64.b64encode(pdf_bytes).decode()
        pdf_data_uri = f"data:application/pdf;base64,{b64_data}"
        pdf_child = MagicMock()
        pdf_child.url = pdf_data_uri

        page = _make_mock_page(url="about:blank", child_frames=[non_pdf_child, pdf_child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result == pdf_data_uri

    @pytest.mark.asyncio
    async def test_child_frame_with_empty_url_returns_none(self) -> None:
        """Returns None when a child frame has an empty URL."""
        child = MagicMock()
        child.url = ""
        page = _make_mock_page(url="about:blank", child_frames=[child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result is None

    @pytest.mark.asyncio
    async def test_child_frame_with_none_url_returns_none(self) -> None:
        """Returns None when a child frame has a None URL."""
        child = MagicMock()
        child.url = None
        page = _make_mock_page(url="about:blank", child_frames=[child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result is None

    @pytest.mark.asyncio
    async def test_data_uri_with_charset_param(self) -> None:
        """Handles data URIs with extra parameters like charset."""
        data_uri = "data:application/pdf;charset=utf-8;base64,JVBERi0xLjQ="
        child = MagicMock()
        child.url = data_uri
        page = _make_mock_page(url="about:blank", child_frames=[child])
        browser_state = MagicMock()
        browser_state.get_working_page = AsyncMock(return_value=page)
        sp = _make_scraped_page(browser_state=browser_state)

        result = await sp.check_pdf_iframe()
        assert result == data_uri


# ===================================================================
# Tests for check_pdf_viewer_embed (existing behavior preserved)
# ===================================================================
class TestCheckPdfViewerEmbed:
    """Tests to confirm existing check_pdf_viewer_embed behavior is preserved."""

    def test_single_embed_element_with_pdf_type(self) -> None:
        """Returns the src when page has exactly one embed element with type=application/pdf."""
        sp = _make_scraped_page(
            elements=[
                {
                    "tagName": "embed",
                    "attributes": {"type": "application/pdf", "src": "https://example.com/file.pdf"},
                }
            ],
        )
        result = sp.check_pdf_viewer_embed()
        assert result == "https://example.com/file.pdf"

    def test_single_embed_element_with_base64_src(self) -> None:
        """Returns the base64 data URI for embed with base64 PDF src."""
        data_uri = "data:application/pdf;base64,JVBERi0xLjQ="
        sp = _make_scraped_page(
            elements=[
                {
                    "tagName": "embed",
                    "attributes": {"type": "application/pdf", "src": data_uri},
                }
            ],
        )
        result = sp.check_pdf_viewer_embed()
        assert result == data_uri

    def test_multiple_elements_returns_none(self) -> None:
        """Returns None when page has more than one element (not a pure PDF viewer)."""
        sp = _make_scraped_page(
            elements=[
                {"tagName": "embed", "attributes": {"type": "application/pdf", "src": "file.pdf"}},
                {"tagName": "button", "attributes": {}},
            ],
        )
        result = sp.check_pdf_viewer_embed()
        assert result is None

    def test_no_elements_returns_none(self) -> None:
        """Returns None when page has no elements."""
        sp = _make_scraped_page(elements=[])
        result = sp.check_pdf_viewer_embed()
        assert result is None

    def test_non_embed_element_returns_none(self) -> None:
        """Returns None when the single element is not an embed."""
        sp = _make_scraped_page(
            elements=[{"tagName": "iframe", "attributes": {"src": "data:application/pdf;base64,..."}}],
        )
        result = sp.check_pdf_viewer_embed()
        assert result is None

    def test_embed_without_pdf_type_returns_none(self) -> None:
        """Returns None when embed has a non-PDF type."""
        sp = _make_scraped_page(
            elements=[{"tagName": "embed", "attributes": {"type": "text/html", "src": "page.html"}}],
        )
        result = sp.check_pdf_viewer_embed()
        assert result is None

    def test_embed_without_type_returns_none(self) -> None:
        """Returns None when embed has no type attribute."""
        sp = _make_scraped_page(
            elements=[{"tagName": "embed", "attributes": {"src": "file.pdf"}}],
        )
        result = sp.check_pdf_viewer_embed()
        assert result is None

    def test_embed_without_attributes_returns_none(self) -> None:
        """Returns None when embed has no attributes."""
        sp = _make_scraped_page(
            elements=[{"tagName": "embed", "attributes": {}}],
        )
        result = sp.check_pdf_viewer_embed()
        assert result is None
