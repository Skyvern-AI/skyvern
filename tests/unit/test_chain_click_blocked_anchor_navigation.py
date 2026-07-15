"""Tests for the blocked-anchor direct-navigation gate in chain_click.

When Playwright cannot click an ``<a href>`` because an untracked/non-interactable
overlay sits on top of it, dispatching a coordinate click into the overlay is
unsafe: overlay JS handlers can fire and navigate to an unintended URL.  The
guard added here follows the anchor's ``href`` directly and skips the coordinate
fallback for the ``blocking_element is None and blocked is True`` branch only.
"""

from __future__ import annotations

from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.webeye.utils import dom as dom_module
from skyvern.webeye.utils.dom import InteractiveElement, SkyvernElement


class _ChainClickRun(NamedTuple):
    results: list
    coordinate_click: AsyncMock
    try_navigate: AsyncMock
    click_in_javascript: AsyncMock
    blocking_click: AsyncMock | None


def _make_anchor_element(
    href: str | None = "/target",
    tag: str = InteractiveElement.A,
    target: str | None = None,
    frame_url: str | None = None,
) -> SkyvernElement:
    """Build a `SkyvernElement` stub configured as an anchor by default.

    ``object.__new__`` bypasses ``__init__`` — only the methods exercised by the
    new href-navigation helper are stubbed.  Tests that care about the
    browser-normalized href patch ``SkyvernFrame.evaluate`` via monkeypatch.
    ``frame_url`` defaults to ``None`` so ``resolve_http_href``'s fallback
    lands on ``page.url``; set it to simulate a cross-origin iframe.
    """
    elem = object.__new__(SkyvernElement)
    elem.get_tag_name = MagicMock(return_value=tag)  # type: ignore[method-assign]
    elem.get_id = MagicMock(return_value="AAA3")  # type: ignore[method-assign]

    async def _get_attr(name: str, mode: str = "dynamic", **_: object) -> str | None:
        if name == "href":
            return href
        if name == "target":
            return target
        return None

    elem.get_attr = AsyncMock(side_effect=_get_attr)  # type: ignore[method-assign]
    elem.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
    frame = MagicMock()
    frame.url = frame_url
    frame.goto = AsyncMock(return_value=None)
    elem.get_frame = MagicMock(return_value=frame)  # type: ignore[method-assign]
    return elem


def _make_page(url: str = "https://portal.example.com/dashboard") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock(return_value=None)
    return page


class TestTryNavigateViaHref:
    """Unit tests for ``SkyvernElement.try_navigate_via_href``."""

    @pytest.fixture(autouse=True)
    def _stub_skyvern_frame_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default: normalized-href evaluate is unavailable, so the static
        # ``urljoin`` fallback path is exercised.  Tests that specifically
        # need a normalized value (or an evaluate failure) re-patch this
        # attribute via their own ``monkeypatch`` fixture.
        monkeypatch.setattr(
            dom_module.SkyvernFrame,
            "evaluate",
            AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_browser_normalized_href_is_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            dom_module.SkyvernFrame,
            "evaluate",
            AsyncMock(return_value="https://base.example.net/root/relative/path"),
        )
        elem = _make_anchor_element(href="relative/path")
        page = _make_page("https://portal.example.com/dashboard")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://base.example.net/root/relative/path"
        elem.get_frame().goto.assert_awaited_once_with(
            "https://base.example.net/root/relative/path",
            timeout=settings.BROWSER_LOADING_TIMEOUT_MS,
        )
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_evaluate_failure_falls_back_to_static_href_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            dom_module.SkyvernFrame,
            "evaluate",
            AsyncMock(side_effect=RuntimeError("evaluate failed")),
        )
        elem = _make_anchor_element(href="/billpay?requestedFlow=S")
        page = _make_page("https://portal.example.com/dashboard")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/billpay?requestedFlow=S"
        elem.get_frame().goto.assert_awaited_once()
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_relative_href_resolves_and_navigates(self) -> None:
        elem = _make_anchor_element(href="/billpay?requestedFlow=S")
        page = _make_page("https://portal.example.com/dashboard")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/billpay?requestedFlow=S"
        elem.get_frame().goto.assert_awaited_once()
        page.goto.assert_not_called()
        call_args = elem.get_frame().goto.await_args
        assert call_args.args[0] == "https://portal.example.com/billpay?requestedFlow=S"

    @pytest.mark.asyncio
    async def test_fallback_resolves_root_href_against_owning_frame_url(self) -> None:
        # An anchor inside a cross-origin iframe with a root-relative href must
        # resolve against the frame's origin (where the anchor lives), not the
        # top-level page's origin.  Only exercised when browser-normalization
        # evaluate returns None so we fall through to ``urljoin``.
        elem = _make_anchor_element(
            href="/deep/path",
            frame_url="https://iframe.example.net/inner/section",
        )
        page = _make_page("https://top.example.com/foo/bar")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://iframe.example.net/deep/path"
        elem.get_frame().goto.assert_awaited_once_with(
            "https://iframe.example.net/deep/path",
            timeout=settings.BROWSER_LOADING_TIMEOUT_MS,
        )
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_root_href_replaces_base_url_subpath(self) -> None:
        # ``urljoin`` correctly treats a root-relative href as absolute against
        # the origin: ``/billpay`` against ``https://host/foo/bar/baz`` must
        # resolve to ``https://host/billpay`` — never concatenated under the
        # base URL's subpath.
        elem = _make_anchor_element(href="/billpay")
        page = _make_page("https://portal.example.com/foo/bar/baz")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/billpay"
        elem.get_frame().goto.assert_awaited_once_with(
            "https://portal.example.com/billpay",
            timeout=settings.BROWSER_LOADING_TIMEOUT_MS,
        )
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_absolute_same_origin_href_navigates(self) -> None:
        elem = _make_anchor_element(href="https://portal.example.com/billpay")
        page = _make_page("https://portal.example.com/dashboard")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/billpay"
        elem.get_frame().goto.assert_awaited_once_with(
            "https://portal.example.com/billpay",
            timeout=settings.BROWSER_LOADING_TIMEOUT_MS,
        )
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_absolute_cross_origin_href_navigates(self) -> None:
        elem = _make_anchor_element(href="https://other.example.net/foo")
        page = _make_page("https://portal.example.com/dashboard")

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://other.example.net/foo"
        elem.get_frame().goto.assert_awaited_once()
        page.goto.assert_not_called()

    @pytest.mark.parametrize("target", ["_blank", "_top", "_parent", "resultsFrame"])
    @pytest.mark.asyncio
    async def test_non_self_target_returns_none_without_goto(self, target: str) -> None:
        elem = _make_anchor_element(href="/billpay", target=target)
        page = _make_page("https://portal.example.com/dashboard")

        assert await elem.try_navigate_via_href(page=page) is None
        elem.get_frame().goto.assert_not_called()
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_anchor_returns_none(self) -> None:
        elem = _make_anchor_element(tag=InteractiveElement.BUTTON, href="/target")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_href_returns_none(self) -> None:
        elem = _make_anchor_element(href="")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_href_returns_none(self) -> None:
        elem = _make_anchor_element(href=None)
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_fragment_only_href_returns_none(self) -> None:
        elem = _make_anchor_element(href="#section")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_javascript_scheme_returns_none(self) -> None:
        elem = _make_anchor_element(href="javascript:void(0)")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_mailto_scheme_returns_none(self) -> None:
        elem = _make_anchor_element(href="mailto:support@example.com")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_tel_scheme_returns_none(self) -> None:
        elem = _make_anchor_element(href="tel:+15551234567")
        page = _make_page()

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_goto_generic_failure_returns_none(self) -> None:
        elem = _make_anchor_element(href="/target")
        page = _make_page()
        elem.get_frame().goto = AsyncMock(side_effect=RuntimeError("something else"))

        assert await elem.try_navigate_via_href(page=page) is None
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_goto_err_aborted_is_treated_as_navigated(self) -> None:
        # Mirrors ``navigate_to_a_href``: download-triggering anchors surface as
        # net::ERR_ABORTED but the navigation intent was honored.
        elem = _make_anchor_element(href="/download.pdf")
        page = _make_page()
        elem.get_frame().goto = AsyncMock(side_effect=RuntimeError("net::ERR_ABORTED"))

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/download.pdf"
        page.goto.assert_not_called()

    @pytest.mark.asyncio
    async def test_goto_download_starting_is_treated_as_navigated(self) -> None:
        elem = _make_anchor_element(href="/statement.pdf")
        page = _make_page()
        elem.get_frame().goto = AsyncMock(side_effect=RuntimeError("Frame.goto: Download is starting"))

        result = await elem.try_navigate_via_href(page=page)

        assert result == "https://portal.example.com/statement.pdf"
        page.goto.assert_not_called()


class TestChainClickBlockedAnchorNavigation:
    """Integration tests: chain_click must call the new helper only in the
    ``blocking_element is None and blocked=True`` branch, and only for anchors
    whose href resolves to http/https."""

    @staticmethod
    async def _run_chain_click(
        monkeypatch: pytest.MonkeyPatch,
        *,
        blocked: bool,
        tag: str,
        href: str | None,
        file_url: str | None = None,
        action_x: int | None = None,
        action_y: int | None = None,
        action_download: bool = False,
        navigate_result: str | None = "https://portal.example.com/billpay",
        with_incremental: bool = False,
        page_responded: bool | None = True,
        input_type: str | None = None,
        js_click_url: str | None = None,
        blocking_is_parent: bool | None = None,
        blocking_is_sibling: bool | None = None,
        blocking_rect: dict[str, float] | None = None,
        blocking_viewport: tuple[float, float] = (1920, 1080),
        blocking_inside_modal: bool = False,
    ) -> _ChainClickRun:
        """Invoke chain_click through the ``find_blocking_element`` fallback with
        tightly-scoped stubs.

        ``with_incremental`` supplies an ``incremental_scraped`` observer (as the
        real ClickAction path does) and monkeypatches ``_did_page_respond`` to
        return ``page_responded`` so the intended-element JS-dispatch fallback can
        be verified.  When ``blocking_is_parent``/``blocking_is_sibling`` are set,
        ``find_blocking_element`` returns a tracked blocking element with those
        relationships (Path 2); otherwise it returns ``(None, blocked)`` (Path 1).
        """
        from skyvern.webeye.actions import handler as handler_module
        from skyvern.webeye.actions.actions import ClickAction

        # Stub out the entire pre-blocking-check block so we jump straight to
        # find_blocking_element.  In chain_click, the first Playwright click
        # is invoked via EventStrategyFactory.click_element; make it raise so
        # we enter the fallback chain.
        monkeypatch.setattr(
            handler_module.EventStrategyFactory,
            "click_element",
            AsyncMock(side_effect=RuntimeError("first click failed")),
        )

        # Stub file chooser + skyvern_context.
        monkeypatch.setattr(handler_module.skyvern_context, "current", MagicMock(return_value=None))
        # ``file_url`` on the action triggers upstream download+secret lookups
        # that require app state we don't provide.  Stub them to no-ops so the
        # test can reach the blocking-element branch we actually care about.
        monkeypatch.setattr(
            handler_module,
            "get_actual_value_of_parameter_if_secret_with_task",
            MagicMock(side_effect=lambda _task, p: p),
        )
        monkeypatch.setattr(
            handler_module.handler_utils,
            "download_file",
            AsyncMock(return_value="/tmp/mock-file"),
        )

        # Build a minimal element with the stubs chain_click reaches.
        elem = object.__new__(SkyvernElement)
        # chain_click's LOG.info calls invoke ``str(skyvern_element)`` which
        # goes through ``SkyvernElement.__repr__``; that reads the private
        # ``__static_element`` attribute set by ``__init__``.  Since we bypass
        # ``__init__`` here, seed the name-mangled attribute directly.
        elem._SkyvernElement__static_element = {"id": "AAA3", "tagName": tag}  # type: ignore[attr-defined]
        elem.get_tag_name = MagicMock(return_value=tag)  # type: ignore[method-assign]
        elem.get_id = MagicMock(return_value="AAA3")  # type: ignore[method-assign]
        elem.locator = MagicMock()

        async def _get_attr(name: str, mode: str = "dynamic", **_: object) -> str | None:
            if name == "href":
                return href
            if name == "onclick":
                return None
            if name == "type":
                return input_type
            return None

        elem.get_attr = AsyncMock(side_effect=_get_attr)  # type: ignore[method-assign]
        elem.navigate_to_a_href = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.find_bound_label_by_attr_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.find_bound_label_by_direct_parent = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.is_visible = AsyncMock(return_value=True)  # type: ignore[method-assign]
        elem.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        elem.coordinate_click = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.click_in_javascript = AsyncMock(return_value=None)  # type: ignore[method-assign]

        blocking_click: AsyncMock | None = None
        if blocking_is_parent is not None or blocking_is_sibling is not None:
            blocking_element = MagicMock()
            blocking_element.get_id = MagicMock(return_value="BLK1")
            blocking_element.is_parent_of = AsyncMock(return_value=bool(blocking_is_parent))
            blocking_element.is_sibling_of = AsyncMock(return_value=bool(blocking_is_sibling))
            blocking_click = AsyncMock(return_value=None)
            blocking_locator = MagicMock()
            blocking_locator.click = blocking_click
            blocking_locator.evaluate = AsyncMock(
                return_value=(
                    {
                        "width": blocking_rect["width"],
                        "height": blocking_rect["height"],
                        "viewport_width": blocking_viewport[0],
                        "viewport_height": blocking_viewport[1],
                        "inside_modal": blocking_inside_modal,
                    }
                    if blocking_rect is not None
                    else None
                )
            )
            blocking_element.get_locator = MagicMock(return_value=blocking_locator)
            elem.find_blocking_element = AsyncMock(return_value=(blocking_element, blocked))  # type: ignore[method-assign]
        else:
            elem.find_blocking_element = AsyncMock(return_value=(None, blocked))  # type: ignore[method-assign]

        try_navigate = AsyncMock(return_value=navigate_result)
        elem.try_navigate_via_href = try_navigate  # type: ignore[method-assign]

        page = MagicMock()
        page.url = "https://portal.example.com/dashboard"
        page.goto = AsyncMock(return_value=None)
        page.on = MagicMock()
        if js_click_url is not None:

            async def _navigate_on_js_click() -> None:
                page.url = js_click_url

            elem.click_in_javascript = AsyncMock(side_effect=_navigate_on_js_click)  # type: ignore[method-assign]

        task = MagicMock()
        task.organization_id = "org_test"
        scraped_page = MagicMock()
        action = ClickAction(
            element_id="AAA3",
            file_url=file_url,
            download=action_download,
            x=action_x,
            y=action_y,
        )

        incremental = None
        skyvern_frame = None
        if with_incremental:
            incremental = MagicMock()
            skyvern_frame = MagicMock()
            skyvern_frame.get_frame.return_value = page
            if page_responded is None:
                incremental.get_incremental_elements_num = AsyncMock(return_value=0)
                skyvern_frame.safe_wait_for_animation_end = AsyncMock(return_value=None)
            else:
                monkeypatch.setattr(
                    handler_module,
                    "_did_page_respond",
                    AsyncMock(return_value=page_responded),
                )

        results = await handler_module.chain_click(
            task=task,
            scraped_page=scraped_page,
            page=page,
            action=action,
            skyvern_element=elem,
            incremental_scraped=incremental,
            skyvern_frame=skyvern_frame,
        )
        return _ChainClickRun(results, elem.coordinate_click, try_navigate, elem.click_in_javascript, blocking_click)

    @pytest.mark.asyncio
    async def test_blocked_anchor_navigates_via_href_and_skips_coordinate_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="/billpay?requestedFlow=S",
        )

        navigate.assert_awaited_once()
        coord_click.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_blocked_non_anchor_still_uses_coordinate_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
        )

        navigate.assert_not_called()
        coord_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unblocked_anchor_still_uses_coordinate_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # blocked=False = the transient-instability React re-render case; the
        # coordinate fallback is safe there.  We must not divert to href nav.
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=False,
            tag=InteractiveElement.A,
            href="/target",
        )

        navigate.assert_not_called()
        coord_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_anchor_with_upload_action_skips_href_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="/target",
            file_url="s3://bucket/file.pdf",
        )

        navigate.assert_not_called()
        coord_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_anchor_with_download_action_skips_href_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # JS-driven downloads (onclick builds a blob/POST) would fetch the
        # wrong static resource if we diverted to ``frame.goto(href)``.
        # Downloads must keep going through the normal click path.
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="/target",
            action_download=True,
        )

        navigate.assert_not_called()
        coord_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_anchor_with_explicit_coordinates_skips_href_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="/target",
            action_x=100,
            action_y=200,
        )

        navigate.assert_not_called()
        coord_click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_anchor_where_helper_returns_none_falls_through_to_coordinate_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the helper cannot resolve a safe URL (e.g. javascript:) it returns
        # None; the existing coordinate fallback must still run.
        results, coord_click, navigate, *_ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="javascript:void(0)",
            navigate_result=None,
        )

        navigate.assert_awaited_once()
        coord_click.assert_awaited_once()


class TestChainClickOverlayBlockedSubmit:
    """A primary-submit click intercepted by a consent / opt-out / FCRA overlay
    must not be silently redirected onto the overlay.  On the observer-backed
    ClickAction path (``incremental_scraped`` present), chain_click dispatches on
    the *intended* element (bypassing hit-testing) instead of coordinate-clicking
    into whatever pixel is on top, and never retargets onto a mere sibling
    overlay.  Callers without an observer (custom checkbox/radio retarget) keep
    their existing coordinate-click / sibling-retarget behavior.
    """

    _run_chain_click = staticmethod(TestChainClickBlockedAnchorNavigation._run_chain_click)

    @staticmethod
    def _has_success(results: list) -> bool:
        from skyvern.webeye.actions.responses import ActionSuccess

        return any(isinstance(r, ActionSuccess) for r in results)

    @staticmethod
    def _has_failure(results: list) -> bool:
        from skyvern.webeye.actions.responses import ActionFailure

        return any(isinstance(r, ActionFailure) for r in results)

    @pytest.mark.asyncio
    async def test_untracked_overlay_dispatches_on_intended_element_not_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            page_responded=True,
        )

        run.click_in_javascript.assert_awaited_once()
        run.coordinate_click.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_untracked_overlay_no_page_response_fails_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            page_responded=False,
        )

        run.click_in_javascript.assert_awaited_once()
        run.coordinate_click.assert_not_called()
        assert self._has_failure(run.results)
        assert not self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_untracked_overlay_input_submit_dispatches_on_intended_element(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            with_incremental=True,
            page_responded=True,
        )

        run.click_in_javascript.assert_awaited_once()
        run.coordinate_click.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_untracked_overlay_navigation_after_dispatch_counts_as_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            page_responded=None,
            js_click_url="https://portal.example.com/submitted",
        )

        run.click_in_javascript.assert_awaited_once()
        run.coordinate_click.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_explicit_coordinate_click_still_coordinate_clicks_under_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An action with explicit x/y is a deliberate coordinate click; keep it.
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            action_x=100,
            action_y=200,
        )

        run.coordinate_click.assert_awaited_once()
        run.click_in_javascript.assert_not_called()

    @pytest.mark.asyncio
    async def test_parent_container_blocker_still_retargets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            blocking_is_parent=True,
            blocking_is_sibling=False,
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_awaited_once()
        run.click_in_javascript.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_sibling_overlay_with_observer_not_retargeted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=True,
            page_responded=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_not_called()
        run.click_in_javascript.assert_awaited_once()
        assert self._has_success(run.results)

    @pytest.mark.parametrize("input_type", ["checkbox", "radio"])
    @pytest.mark.asyncio
    async def test_sibling_form_control_with_observer_still_retargets(
        self, monkeypatch: pytest.MonkeyPatch, input_type: str
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            input_type=input_type,
            href=None,
            with_incremental=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
            blocking_rect={"x": 0, "y": 0, "width": 24, "height": 24},
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_awaited_once()
        run.click_in_javascript.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_small_custom_sibling_with_observer_still_retargets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag="div",
            href=None,
            with_incremental=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
            blocking_rect={"x": 0, "y": 0, "width": 24, "height": 24},
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_awaited_once()
        run.click_in_javascript.assert_not_called()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_full_cover_sibling_overlay_over_checkbox_is_not_retargeted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            input_type="checkbox",
            href=None,
            with_incremental=True,
            page_responded=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
            blocking_rect={"x": 0, "y": 0, "width": 1920, "height": 1080},
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_not_called()
        run.click_in_javascript.assert_awaited_once()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_small_semantic_modal_sibling_over_checkbox_is_not_retargeted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            input_type="checkbox",
            href=None,
            with_incremental=True,
            page_responded=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
            blocking_rect={"x": 0, "y": 0, "width": 600, "height": 400},
            blocking_inside_modal=True,
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_not_called()
        run.click_in_javascript.assert_awaited_once()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_iframe_covering_sibling_overlay_over_checkbox_is_not_retargeted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            input_type="checkbox",
            href=None,
            with_incremental=True,
            page_responded=True,
            blocking_is_parent=False,
            blocking_is_sibling=True,
            blocking_rect={"x": 0, "y": 0, "width": 800, "height": 500},
            blocking_viewport=(800, 500),
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_not_called()
        run.click_in_javascript.assert_awaited_once()
        assert self._has_success(run.results)

    @pytest.mark.asyncio
    async def test_sibling_blocker_without_observer_still_retargets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No observer = the custom checkbox/radio retarget callers; a sibling
        # decoration (e.g. a styled span over the real control) must still be
        # clicked to activate the intended control.
        run = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.BUTTON,
            href=None,
            with_incremental=False,
            blocking_is_parent=False,
            blocking_is_sibling=True,
        )

        assert run.blocking_click is not None
        run.blocking_click.assert_awaited_once()
        assert self._has_success(run.results)


def test_dom_module_exports_try_navigate_via_href() -> None:
    """Sanity check that the helper is a method on ``SkyvernElement``."""
    assert hasattr(dom_module.SkyvernElement, "try_navigate_via_href")
