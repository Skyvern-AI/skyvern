"""Tests for the blocked-anchor direct-navigation gate in chain_click.

When Playwright cannot click an ``<a href>`` because an untracked/non-interactable
overlay sits on top of it, dispatching a coordinate click into the overlay is
unsafe: overlay JS handlers can fire and navigate to an unintended URL.  The
guard added here follows the anchor's ``href`` directly and skips the coordinate
fallback for the ``blocking_element is None and blocked is True`` branch only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.config import settings
from skyvern.webeye.utils import dom as dom_module
from skyvern.webeye.utils.dom import InteractiveElement, SkyvernElement


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
        is_checkbox: bool = False,
        is_checkbox_mock: AsyncMock | None = None,
        checked_states: list[bool | None] | None = None,
        coordinate_raises: bool = False,
        js_mock: AsyncMock | None = None,
        is_checked_mock: AsyncMock | None = None,
        repeat: int = 1,
        blocking_element: SkyvernElement | None = None,
    ) -> tuple[list, AsyncMock, AsyncMock]:
        """Invoke chain_click through the ``blocking_element is None`` path
        with tightly-scoped stubs.  Returns (results, coordinate_click_mock,
        try_navigate_mock)."""
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
        elem.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        elem.locator = MagicMock()

        async def _get_attr(name: str, mode: str = "dynamic", **_: object) -> str | None:
            if name == "href":
                return href
            if name == "onclick":
                return None
            return None

        elem.get_attr = AsyncMock(side_effect=_get_attr)  # type: ignore[method-assign]
        elem.navigate_to_a_href = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.find_bound_label_by_attr_id = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.find_bound_label_by_direct_parent = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.is_visible = AsyncMock(return_value=True)  # type: ignore[method-assign]
        elem.find_blocking_element = AsyncMock(return_value=(blocking_element, blocked))  # type: ignore[method-assign]
        elem.coordinate_click = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("coordinate click failed") if coordinate_raises else None
        )
        elem.click_in_javascript = js_mock if js_mock is not None else AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.is_checkbox = is_checkbox_mock or AsyncMock(return_value=is_checkbox)  # type: ignore[method-assign]
        if is_checked_mock is not None:
            elem.is_checked = is_checked_mock  # type: ignore[method-assign]
        elif checked_states is not None:
            elem.is_checked = AsyncMock(side_effect=list(checked_states))  # type: ignore[method-assign]
        else:
            elem.is_checked = AsyncMock(return_value=None)  # type: ignore[method-assign]

        try_navigate = AsyncMock(return_value=navigate_result)
        elem.try_navigate_via_href = try_navigate  # type: ignore[method-assign]

        page = MagicMock()
        page.url = "https://portal.example.com/dashboard"
        page.goto = AsyncMock(return_value=None)
        page.on = MagicMock()

        task = MagicMock()
        task.organization_id = "org_test"
        scraped_page = MagicMock()
        action = ClickAction(
            element_id="AAA3",
            file_url=file_url,
            download=action_download,
            x=action_x,
            y=action_y,
            repeat=repeat,
        )

        # The blocker's checkbox-safety probe runs through the unified
        # ``SkyvernFrame.evaluate`` entry point; resolve it to the outcome stashed
        # on the blocker's element handle.
        if blocking_element is not None:

            async def _blocker_probe(*, frame: object, expression: str, arg: object, **_: object) -> object:
                outcome = getattr(arg, "probe_outcome", None)
                if outcome == "raise":
                    raise RuntimeError("direct blocker probe failed")
                return outcome

            monkeypatch.setattr(dom_module.SkyvernFrame, "evaluate", AsyncMock(side_effect=_blocker_probe))

        results = await handler_module.chain_click(
            task=task,
            scraped_page=scraped_page,
            page=page,
            action=action,
            skyvern_element=elem,
        )
        return results, elem.coordinate_click, try_navigate

    @pytest.mark.asyncio
    async def test_blocked_anchor_navigates_via_href_and_skips_coordinate_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
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
        results, coord_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.A,
            href="javascript:void(0)",
            navigate_result=None,
        )

        navigate.assert_awaited_once()
        coord_click.assert_awaited_once()


def test_dom_module_exports_try_navigate_via_href() -> None:
    """Sanity check that the helper is a method on ``SkyvernElement``."""
    assert hasattr(dom_module.SkyvernElement, "try_navigate_via_href")


class TestChainClickCheckboxCoordinateVerification:
    """Slice 2 wired through the real ``chain_click`` production branch: a
    checkbox that reaches the coordinate fallback is state-verified, and
    non-checkbox elements keep the plain coordinate behavior."""

    _run_chain_click = staticmethod(TestChainClickBlockedAnchorNavigation._run_chain_click)

    @pytest.mark.asyncio
    async def test_checkbox_coordinate_noop_recovers_via_js(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_checkbox_coordinate_toggle_skips_double_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=False,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, True],
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_checkbox_provable_noop_reports_failure_not_false_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionFailure

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, False],
            js_mock=js_mock,
        )
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionFailure)

    @pytest.mark.asyncio
    async def test_non_checkbox_keeps_plain_coordinate_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=False,
            tag=InteractiveElement.BUTTON,
            href=None,
            is_checkbox=False,
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_multi_click_checkbox_skips_state_verified_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A double-click toggles a checkbox twice and lands on the original state.
        # The single-click state-verified fallback would misread that net-zero
        # change as a no-op and add a wrong third JS toggle, so click_count > 1
        # must keep the plain coordinate path and never consult is_checked.
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        is_checked_mock = AsyncMock(side_effect=[False, False, True])
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=False,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            repeat=2,
            js_mock=js_mock,
            is_checked_mock=is_checked_mock,
        )
        coord_click.assert_awaited_once()
        is_checked_mock.assert_not_called()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_checkbox_initially_checked_toggles_off_skips_js(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A checkbox that starts checked and flips off is a confirmed toggle:
        # success is decided by the state change, not a hardcoded ``checked is True``.
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[True, False],
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_checkbox_unknown_post_state_reports_success_without_js(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real coordinate click ran without error but the post-click state is
        # unreadable (detached/navigated): the legacy contract treats this as a
        # success and never risks a second toggle. Only the unsafe-blocker path
        # (which skips the coordinate click) fails closed here.
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, None],
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_checkbox_coordinate_raises_and_unknown_state_fails_without_js(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Coordinate click raised and the post-click state is unreadable: the
        # click may or may not have landed, so a JS retry risks a double toggle.
        # Report the coordinate failure and stop.
        from skyvern.webeye.actions.responses import ActionFailure

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, None],
            coordinate_raises=True,
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_not_called()
        assert results and isinstance(results[-1], ActionFailure)
        assert "coordinate_click" in results[-1].exception_message

    @pytest.mark.asyncio
    async def test_checkbox_coordinate_raises_but_provable_noop_recovers_via_js(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Coordinate click raised yet the state is provably unchanged (both reads
        # known and equal): a single JS toggle is the missing click, not a double.
        from skyvern.webeye.actions.responses import ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            coordinate_raises=True,
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_non_checkbox_coordinate_raises_then_js_succeeds_two_result_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Parity guard for the shared ladder: a non-checkbox whose coordinate
        # click raises but JS click succeeds must still append the coordinate
        # failure *and* the JS success, in that order.
        from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess

        js_mock = AsyncMock(return_value=None)
        results, coord_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=False,
            tag=InteractiveElement.BUTTON,
            href=None,
            is_checkbox=False,
            coordinate_raises=True,
            js_mock=js_mock,
        )
        coord_click.assert_awaited_once()
        js_mock.assert_awaited_once()
        assert isinstance(results[-2], ActionFailure)
        assert "coordinate_click" in results[-2].exception_message
        assert isinstance(results[-1], ActionSuccess)


def _make_blocking_element(
    *,
    tag: str,
    interactive_descendant: str | None,
    direct_interactive: bool | None = False,
    direct_probe_error: bool = False,
    descendant_probe_error: bool = False,
) -> tuple[SkyvernElement, MagicMock]:
    locator = MagicMock()
    locator.click = AsyncMock(return_value=None)
    # ``locator.evaluate`` is retained only as a spy: the direct-blocker probe now
    # goes through ``SkyvernFrame.evaluate`` (see ``_run_chain_click``), so this
    # must never be awaited by production code.
    if direct_probe_error:
        locator.evaluate = AsyncMock(side_effect=RuntimeError("direct blocker probe failed"))
    else:
        locator.evaluate = AsyncMock(return_value=direct_interactive)
    element_handle = MagicMock()
    element_handle.probe_outcome = "raise" if direct_probe_error else direct_interactive
    locator.element_handle = AsyncMock(return_value=element_handle)

    def _descendant_locator(selector: str) -> MagicMock:
        descendant_locator = MagicMock()
        if descendant_probe_error:
            descendant_locator.count = AsyncMock(side_effect=RuntimeError("descendant probe failed"))
        else:
            requested_descendants = {part.strip() for part in selector.split(",")}
            descendant_locator.count = AsyncMock(return_value=int(interactive_descendant in requested_descendants))
        return descendant_locator

    locator.locator = MagicMock(side_effect=_descendant_locator)
    blocker = SkyvernElement(locator, MagicMock(), {"id": "BLOCKER", "tagName": tag})
    blocker.is_parent_of = AsyncMock(return_value=True)  # type: ignore[method-assign]
    blocker.is_sibling_of = AsyncMock(return_value=False)  # type: ignore[method-assign]
    return blocker, locator


class TestChainClickBlockingLabelGuard:
    _run_chain_click = staticmethod(TestChainClickBlockedAnchorNavigation._run_chain_click)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("descendant", ["a[href]", "button"])
    async def test_unsafe_label_blocker_uses_js_only_verified_toggle(
        self, monkeypatch: pytest.MonkeyPatch, descendant: str
    ) -> None:
        # An unsafe wrapping label (nested actionable descendant) must not receive
        # any real mouse event: neither the blocker click nor the checkbox
        # coordinate click. The coordinate click is skipped and the shared JS
        # fallback toggles + verifies, so a known before->after change succeeds.
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant=descendant)
        js_mock = AsyncMock(return_value=None)
        is_checkbox_mock = AsyncMock(return_value=True)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            is_checkbox_mock=is_checkbox_mock,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        selector = blocker.get_locator().locator.call_args.args[0]  # type: ignore[union-attr]
        assert descendant in selector
        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        is_checkbox_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_unsafe_label_blocker_unchanged_state_fails_without_real_mouse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Known before==after through the shared JS fallback: the toggle was a
        # provable no-op. Report failure instead of a false success, and never
        # dispatch a real mouse click.
        from skyvern.webeye.actions.responses import ActionFailure

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="a[href]")
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, False],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        # The direct-blocker probe no longer calls ``locator.evaluate``; it goes
        # through the unified ``SkyvernFrame.evaluate`` entry point.
        blocker_locator.evaluate.assert_not_awaited()
        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionFailure)

    @pytest.mark.asyncio
    async def test_unsafe_label_blocker_unknown_post_state_fails_without_retry_or_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Known pre-state, JS toggle, then post-state unreadable (detached/navigated):
        # fail without a second toggle and without any real mouse click.
        from skyvern.webeye.actions.responses import ActionFailure

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="a[href]")
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, None],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        assert results and isinstance(results[-1], ActionFailure)

    @pytest.mark.asyncio
    async def test_unsafe_blocker_unknown_pre_state_fails_without_js_or_coordinate(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The state is unreadable with no coordinate click having run: without a
        # known baseline the fallback fails closed with no coordinate click and no
        # JS click (the skipped-coordinate path never claims a false success).
        from skyvern.webeye.actions.responses import ActionFailure

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="a[href]")
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[None, None],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_not_awaited()
        navigate.assert_not_awaited()
        assert results and isinstance(results[-1], ActionFailure)

    @pytest.mark.asyncio
    async def test_unsafe_blocker_js_exception_fails_without_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The JS toggle itself raises: report the failure and never retry with a
        # real mouse click.
        from skyvern.webeye.actions.responses import ActionFailure

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="a[href]")
        js_mock = AsyncMock(side_effect=RuntimeError("js click failed"))

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        assert results and isinstance(results[-1], ActionFailure)

    @pytest.mark.asyncio
    async def test_label_descendant_probe_error_uses_js_only_verified_toggle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Descendant probe raises -> blocker safety is unknown. Treat as unsafe:
        # no real mouse click, JS toggle only, verified by a known state change.
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag="label", interactive_descendant=None, descendant_probe_error=True
        )
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_unrelated_unsafe_blocker_uses_js_only_verified_toggle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.BUTTON,
            interactive_descendant=None,
            direct_interactive=True,
        )
        blocker.is_parent_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("relationship probe must not run")
        )
        blocker.is_sibling_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("relationship probe must not run")
        )
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        blocker.is_parent_of.assert_not_awaited()
        blocker.is_sibling_of.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_same_parent_non_label_unsafe_sibling_uses_js_only_verified_toggle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A,
            interactive_descendant=None,
            direct_interactive=True,
        )
        blocker.is_parent_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("relationship probe must not run")
        )
        blocker.is_sibling_of = AsyncMock(  # type: ignore[method-assign]
            side_effect=AssertionError("relationship probe must not run")
        )
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        blocker.is_parent_of.assert_not_awaited()
        blocker.is_sibling_of.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_unsafe_sibling_under_shared_label_uses_js_only_verified_toggle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A,
            interactive_descendant=None,
            direct_interactive=True,
        )
        blocker.is_parent_of = AsyncMock(return_value=False)  # type: ignore[method-assign]
        blocker.is_sibling_of = AsyncMock(return_value=True)  # type: ignore[method-assign]
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_unsafe_label_blocker_for_non_checkbox_keeps_direct_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="button")

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=False,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_awaited_once()
        blocker_locator.locator.assert_not_called()
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_unsafe_label_blocker_for_repeated_checkbox_click_keeps_direct_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant="a[href]")

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            repeat=2,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_awaited_once()
        blocker_locator.locator.assert_not_called()
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_safe_label_blocker_keeps_direct_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(tag="label", interactive_descendant=None)

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_awaited_once()
        blocker_locator.locator.assert_called_once_with("a[href], button")
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_non_label_blocker_keeps_direct_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(tag="div", interactive_descendant="a[href]")

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_awaited_once()
        blocker_locator.locator.assert_not_called()
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tag", "direct_interactive"),
        [(InteractiveElement.A, True), (InteractiveElement.BUTTON, True)],
    )
    async def test_blocker_itself_interactive_uses_js_only_verified_toggle(
        self, monkeypatch: pytest.MonkeyPatch, tag: str, direct_interactive: bool
    ) -> None:
        # The blocker is itself an a[href]/button: dispatching a coordinate click
        # at the checkbox could still land on it and navigate/submit. Use the
        # JS-only verified toggle with no real mouse event.
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=tag,
            interactive_descendant=None,
            direct_interactive=direct_interactive,
        )
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, navigate = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        navigate.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("direct_interactive", "direct_probe_error"),
        [(None, False), (False, True)],
    )
    async def test_blocker_itself_actionability_unknown_uses_js_only_verified_toggle(
        self,
        monkeypatch: pytest.MonkeyPatch,
        direct_interactive: bool | None,
        direct_probe_error: bool,
    ) -> None:
        # Blocker safety is unknown (non-boolean probe result or probe exception):
        # fail closed on the real mouse click and toggle via JS only, verified by
        # a known state change.
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A,
            interactive_descendant=None,
            direct_interactive=direct_interactive,
            direct_probe_error=direct_probe_error,
        )
        js_mock = AsyncMock(return_value=None)

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            checked_states=[False, False, True],
            js_mock=js_mock,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_not_awaited()
        coordinate_click.assert_not_awaited()
        js_mock.assert_awaited_once()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_plain_anchor_blocker_keeps_direct_click(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A,
            interactive_descendant=None,
            direct_interactive=False,
        )

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=True,
            blocking_element=blocker,
        )

        blocker_locator.click.assert_awaited_once()
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("is_checkbox", "repeat"), [(False, 1), (True, 2)])
    async def test_direct_interactive_blocker_parity_outside_single_checkbox(
        self, monkeypatch: pytest.MonkeyPatch, is_checkbox: bool, repeat: int
    ) -> None:
        from skyvern.webeye.actions.responses import ActionSuccess

        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.BUTTON,
            interactive_descendant=None,
            direct_interactive=True,
        )

        results, coordinate_click, _ = await self._run_chain_click(
            monkeypatch,
            blocked=True,
            tag=InteractiveElement.INPUT,
            href=None,
            is_checkbox=is_checkbox,
            repeat=repeat,
            blocking_element=blocker,
        )

        blocker_locator.evaluate.assert_not_awaited()
        blocker_locator.click.assert_awaited_once()
        coordinate_click.assert_not_awaited()
        assert results and isinstance(results[-1], ActionSuccess)


class TestIsSafeForCheckboxDirectClickUsesSkyvernFrameEvaluate:
    """The direct-blocker interactivity probe must dispatch through the unified
    ``SkyvernFrame.evaluate`` entry point (repo-standard timeout/dispatch/
    navigation recovery, handle marshalling) rather than ``Locator.evaluate``,
    while preserving fail-closed semantics."""

    @pytest.mark.asyncio
    async def test_probe_routes_through_skyvernframe_evaluate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.BUTTON, interactive_descendant=None, direct_interactive=True
        )
        frame_eval = AsyncMock(return_value=True)
        monkeypatch.setattr(dom_module.SkyvernFrame, "evaluate", frame_eval)

        result = await blocker.is_safe_for_checkbox_direct_click()

        assert result is False  # interactive blocker => unsafe
        frame_eval.assert_awaited_once()
        call = frame_eval.await_args
        assert call.kwargs["expression"] == "(element) => element.matches('a[href], button')"
        assert call.kwargs["frame"] is blocker.get_frame()
        assert call.kwargs["arg"] is await blocker.get_element_handler()
        blocker_locator.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_exception_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A, interactive_descendant=None, direct_interactive=False
        )
        monkeypatch.setattr(dom_module.SkyvernFrame, "evaluate", AsyncMock(side_effect=RuntimeError("probe failed")))

        assert await blocker.is_safe_for_checkbox_direct_click() is False
        blocker_locator.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_non_boolean_result_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        blocker, blocker_locator = _make_blocking_element(
            tag=InteractiveElement.A, interactive_descendant=None, direct_interactive=False
        )
        monkeypatch.setattr(dom_module.SkyvernFrame, "evaluate", AsyncMock(return_value=None))

        assert await blocker.is_safe_for_checkbox_direct_click() is False
        blocker_locator.evaluate.assert_not_awaited()
