"""SKY-11618: a native ``<select>`` must be driven via ``select_option`` (normal_select) first,
so an unrelated overlay (a consent/opt-out/FCRA modal) covering the control can no longer hijack
the selection into click-navigation.

Root cause: the select handler treated ANY element overlapping the ``<select>``'s center as a
"blocking" custom dropdown and click-navigated it — so a modal painted over the control was
click-navigated instead of the value being committed. The fix tries ``normal_select``
(``select_option``, which commits the native value via the DOM regardless of the overlay) first,
and only falls back to click-navigating an overlapping element when that genuinely fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.webeye.utils.dom import InteractiveElement, SkyvernElement


class TestSelectOptionFirst:
    """The select handler calls ``normal_select`` first; an overlapping element is only
    click-navigated when the native ``select_option`` fails."""

    @staticmethod
    def _make_select_element(is_visible: bool = True, selected: str | None = None) -> SkyvernElement:
        elem = object.__new__(SkyvernElement)
        elem.get_tag_name = MagicMock(return_value=InteractiveElement.SELECT)  # type: ignore[method-assign]
        elem.get_id = MagicMock(return_value="SEL1")  # type: ignore[method-assign]
        elem.is_custom_option = AsyncMock(return_value=False)  # type: ignore[method-assign]
        elem.is_selectable = AsyncMock(return_value=True)  # type: ignore[method-assign]
        elem.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        elem.is_visible = AsyncMock(return_value=is_visible)  # type: ignore[method-assign]
        elem.get_attr = AsyncMock(return_value=selected)  # type: ignore[method-assign]
        elem.scroll_into_view = AsyncMock(return_value=None)  # type: ignore[method-assign]
        elem.find_blocking_element = AsyncMock()  # type: ignore[method-assign]
        return elem

    async def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        normal_success: bool,
        blocking_return: tuple = (None, False),
        blocker_is_checkbox: bool = False,
        normal_raises: bool = False,
        blocker_is_surrogate: bool = True,
        select_visible: bool = True,
        preselected: str | None = None,
    ) -> tuple:
        from skyvern.webeye.actions import handler as handler_module
        from skyvern.webeye.actions.actions import SelectOption, SelectOptionAction
        from skyvern.webeye.actions.responses import ActionFailure, ActionSuccess

        select_element = self._make_select_element(is_visible=select_visible, selected=preselected)
        select_element.find_blocking_element = AsyncMock(return_value=blocking_return)  # type: ignore[method-assign]

        dom_instance = MagicMock()
        dom_instance.scraped_page = MagicMock()
        dom_instance.get_skyvern_element_by_id = AsyncMock(return_value=select_element)
        monkeypatch.setattr(handler_module, "DomUtil", MagicMock(return_value=dom_instance))

        if normal_raises:
            normal_select = AsyncMock(side_effect=Exception("LLM provider error"))
        else:
            normal_result = [ActionSuccess()] if normal_success else [ActionFailure(Exception("no option"))]
            normal_select = AsyncMock(return_value=normal_result)
        monkeypatch.setattr(handler_module, "normal_select", normal_select)

        checkbox_routed = ["CHECKBOX_ROUTED"]
        handle_checkbox = AsyncMock(return_value=checkbox_routed)
        monkeypatch.setattr(handler_module, "handle_checkbox_action", handle_checkbox)

        blocking_element = blocking_return[0]
        if blocking_element is not None:
            blocking_element.get_id = MagicMock(return_value="BLK1")
            blocking_element.is_checkbox = AsyncMock(return_value=blocker_is_checkbox)
            blocking_element.is_radio = AsyncMock(return_value=False)
            blocking_element.is_btn_input = AsyncMock(return_value=False)
            # dropdown surrogate -> reassign & click-navigate; otherwise (a modal) -> honest failure
            blocking_element.get_tag_name = MagicMock(return_value="div")
            blocking_element.get_attr = AsyncMock(return_value="combobox" if blocker_is_surrogate else "dialog")

        scraped_page = MagicMock()
        scraped_page.id_to_element_dict = {"SEL1": {"id": "SEL1", "tagName": "select"}}

        action = SelectOptionAction(element_id="SEL1", option=SelectOption(label="California", value="CA"))

        results = await handler_module.handle_select_option_action(
            action=action,
            page=MagicMock(),
            scraped_page=scraped_page,
            task=MagicMock(),
            step=MagicMock(),
        )
        return results, normal_select, select_element, handle_checkbox

    @pytest.mark.asyncio
    async def test_overlay_present_but_select_option_succeeds_no_click_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The modal-hijack scenario: normal_select (select_option) commits the value, so the
        # handler returns immediately and never inspects/click-navigates the overlapping element.
        results, normal_select, select_element, _ = await self._run(monkeypatch, normal_success=True)

        normal_select.assert_awaited_once()
        assert normal_select.await_args.kwargs["skyvern_element"] is select_element
        select_element.find_blocking_element.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionSuccess"

    @pytest.mark.asyncio
    async def test_select_option_fails_no_blocker_returns_normal_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results, normal_select, select_element, handle_checkbox = await self._run(
            monkeypatch, normal_success=False, blocking_return=(None, False)
        )

        normal_select.assert_awaited_once()
        select_element.find_blocking_element.assert_awaited()  # looked for a blocker, found none
        handle_checkbox.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionFailure"

    @pytest.mark.asyncio
    async def test_select_option_fails_with_blocker_falls_back_to_click_navigation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When select_option fails AND an element overlaps the control, reassign to it (the styled
        # custom-dropdown fallback). Routed here to a checkbox stub to prove the reassignment path.
        blocker = MagicMock()
        results, normal_select, _, handle_checkbox = await self._run(
            monkeypatch, normal_success=False, blocking_return=(blocker, True), blocker_is_checkbox=True
        )

        normal_select.assert_awaited_once()
        handle_checkbox.assert_awaited_once()
        assert results == ["CHECKBOX_ROUTED"]

    @pytest.mark.asyncio
    async def test_select_option_fails_with_modal_blocker_no_hijack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # select_option failed AND the overlapping element is an unrelated modal (not a dropdown
        # surrogate): do NOT retarget onto it — return the honest native-select failure.
        blocker = MagicMock()
        results, normal_select, _, handle_checkbox = await self._run(
            monkeypatch,
            normal_success=False,
            blocking_return=(blocker, True),
            blocker_is_checkbox=True,
            blocker_is_surrogate=False,
        )

        handle_checkbox.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionFailure"

    @pytest.mark.asyncio
    async def test_normal_select_raises_still_falls_back_to_blocker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # normal_select can raise (e.g. an LLM/provider error). The styled-dropdown fallback must
        # still run instead of the whole action erroring out.
        blocker = MagicMock()
        results, normal_select, _, handle_checkbox = await self._run(
            monkeypatch,
            normal_success=False,
            normal_raises=True,
            blocking_return=(blocker, True),
            blocker_is_checkbox=True,
        )

        normal_select.assert_awaited_once()
        handle_checkbox.assert_awaited_once()
        assert results == ["CHECKBOX_ROUTED"]

    @pytest.mark.asyncio
    async def test_hidden_backing_select_click_navigates_widget_even_without_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A hidden backing <select> (display:none) behind a styled dropdown must NOT burn
        # select_option visibility timeouts, and the overlapping widget IS the intended target — so
        # click-navigate it even when the hit node itself carries no dropdown role (role on ancestor).
        blocker = MagicMock()
        results, normal_select, _, handle_checkbox = await self._run(
            monkeypatch,
            normal_success=False,
            select_visible=False,
            blocking_return=(blocker, True),
            blocker_is_checkbox=True,
            blocker_is_surrogate=False,
        )

        normal_select.assert_not_awaited()
        handle_checkbox.assert_awaited_once()
        assert results == ["CHECKBOX_ROUTED"]

    @pytest.mark.asyncio
    async def test_hidden_select_no_surrogate_fails_fast_without_running_select_option(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hidden <select> with no styled widget: fail fast; never run select_option on a hidden node.
        results, normal_select, _, _ = await self._run(
            monkeypatch, normal_success=False, select_visible=False, blocking_return=(None, False)
        )

        normal_select.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionFailure"

    @pytest.mark.asyncio
    async def test_already_selected_hidden_select_is_idempotent_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A hidden <select> already holding the requested value is a no-op success — must not fail
        # or drive a blocker just because the visibility gate skips normal_select.
        results, normal_select, select_element, handle_checkbox = await self._run(
            monkeypatch, normal_success=False, select_visible=False, preselected="CA"
        )

        normal_select.assert_not_awaited()
        select_element.find_blocking_element.assert_not_awaited()
        handle_checkbox.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionSuccess"

    @pytest.mark.asyncio
    async def test_normal_select_raises_no_blocker_returns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results, normal_select, select_element, handle_checkbox = await self._run(
            monkeypatch, normal_success=False, normal_raises=True, blocking_return=(None, False)
        )

        select_element.find_blocking_element.assert_awaited()
        handle_checkbox.assert_not_awaited()
        assert len(results) == 1 and results[0].__class__.__name__ == "ActionFailure"


class TestIsDropdownSurrogateBlocker:
    """The visible-<select> fallback guard: click-navigate a blocker only if it's a genuine
    dropdown surrogate, never an unrelated modal. Locks the role/aria-haspopup matrix."""

    @staticmethod
    def _elem(*, tag: str = "div", role: str | None = None, haspopup: str | None = None) -> MagicMock:
        elem = MagicMock()
        elem.get_tag_name = MagicMock(return_value=tag)
        attrs = {"role": role, "aria-haspopup": haspopup}
        elem.get_attr = AsyncMock(side_effect=lambda name, *a, **k: attrs.get(name))
        return elem

    @pytest.mark.parametrize(
        ("tag", "role", "haspopup", "expected"),
        [
            pytest.param("select", None, None, True, id="native_select"),
            pytest.param("div", "combobox", None, True, id="role_combobox"),
            pytest.param("ul", "listbox", None, True, id="role_listbox"),
            pytest.param("button", None, "listbox", True, id="haspopup_listbox"),
            pytest.param("button", None, "menu", True, id="haspopup_menu"),
            pytest.param("button", None, "true", True, id="haspopup_true_is_menu"),
            pytest.param("button", None, "dialog", False, id="haspopup_dialog_excluded"),
            pytest.param("button", None, "false", False, id="haspopup_false_excluded"),
            pytest.param("div", "dialog", None, False, id="role_dialog_modal"),
            pytest.param("button", None, None, False, id="plain_modal_button"),
        ],
    )
    @pytest.mark.asyncio
    async def test_matrix(self, tag: str, role: str | None, haspopup: str | None, expected: bool) -> None:
        from skyvern.webeye.actions import handler as handler_module

        elem = self._elem(tag=tag, role=role, haspopup=haspopup)
        assert await handler_module._is_dropdown_surrogate_blocker(elem) is expected


class TestSelectCommitsUnderOverlay:
    """The value must still commit when an overlay intercepts the focus-click before
    ``select_option`` — the pre-click is best-effort; ``select_option`` sets the native value
    via the DOM regardless."""

    @pytest.mark.asyncio
    async def test_best_effort_focus_click_swallows_intercepted_click(self) -> None:
        from skyvern.webeye.actions import handler as handler_module
        from skyvern.webeye.actions.actions import SelectOption, SelectOptionAction

        locator = MagicMock()
        locator.click = AsyncMock(side_effect=Exception("click intercepted by overlay"))
        action = SelectOptionAction(element_id="SEL1", option=SelectOption(label="California", value="CA"))

        # Must not raise — an intercepted focus-click cannot abort the selection.
        await handler_module._best_effort_focus_click_before_select(locator=locator, action=action)
        locator.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deterministic_select_commits_when_preclick_intercepted(self) -> None:
        from skyvern.webeye.actions import handler as handler_module
        from skyvern.webeye.actions.actions import SelectOption, SelectOptionAction
        from skyvern.webeye.actions.responses import ActionSuccess

        locator = MagicMock()
        locator.click = AsyncMock(side_effect=Exception("click intercepted by overlay"))
        locator.select_option = AsyncMock(return_value=None)  # DOM select succeeds regardless

        skyvern_element = MagicMock()
        skyvern_element.get_options = MagicMock(return_value=[{"value": "CA", "label": "California"}])

        action = SelectOptionAction(element_id="SEL1", option=SelectOption(label="California", value="CA"))

        result = await handler_module._select_deterministic_normal_option(
            action=action,
            skyvern_element=skyvern_element,
            locator=locator,
            matched_label="California",
            matched_value="CA",
            matched_index=0,
        )

        # The intercepted pre-click did not abort: select_option ran and the value committed.
        locator.click.assert_awaited_once()
        locator.select_option.assert_awaited_once()
        assert locator.select_option.await_args.kwargs["value"] == "CA"
        assert len(result) == 1 and isinstance(result[0], ActionSuccess)
