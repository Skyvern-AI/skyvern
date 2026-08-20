"""SKY-12939 Lane B: explicit-submit bypass of the dropdown sequential-click rescrape.

Positive allowlist only — exact ``button[type=submit]`` / ``input[type=submit]``
after target resolution/retargeting. Every ambiguous, dropdown, link, checkbox,
custom control, missing-type, or read-error case must fall through to the
existing ``handle_sequential_click_for_dropdown`` path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import skyvern.webeye.actions.handler as handler_module
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.handler import (
    handle_click_action,
    handle_sequential_click_with_submit_bypass,
)
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.utils.dom import SkyvernElement


def _el(*, element_id: str, tag_name: str, attributes: dict | None = None) -> SkyvernElement:
    static = {"id": element_id, "tagName": tag_name, "attributes": attributes or {}}
    return SkyvernElement(MagicMock(), MagicMock(), static)


class TestSkyvernElementIsExplicitSubmit:
    @pytest.mark.asyncio
    async def test_button_type_submit_is_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="button", attributes={"type": "submit"})
        assert await el.is_explicit_submit() is True

    @pytest.mark.asyncio
    async def test_input_type_submit_is_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="input", attributes={"type": "submit"})
        assert await el.is_explicit_submit() is True

    @pytest.mark.asyncio
    async def test_button_type_submit_case_insensitive(self) -> None:
        el = _el(element_id="E1", tag_name="BUTTON", attributes={"type": "Submit"})
        assert await el.is_explicit_submit() is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("padded", [" submit ", "submit ", " submit", "\tsubmit", "submit\n"])
    async def test_whitespace_padded_type_is_not_explicit(self, padded: str) -> None:
        # Strict raw-attribute allowlist: exact match with no whitespace trimming,
        # so a padded token never qualifies regardless of HTML default semantics.
        assert await _el(element_id="E1", tag_name="input", attributes={"type": padded}).is_explicit_submit() is False
        assert await _el(element_id="E1", tag_name="button", attributes={"type": padded}).is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_empty_type_is_not_explicit(self) -> None:
        assert await _el(element_id="E1", tag_name="button", attributes={"type": ""}).is_explicit_submit() is False
        assert await _el(element_id="E1", tag_name="input", attributes={"type": ""}).is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_button_without_type_is_not_explicit(self) -> None:
        # The missing-type HTML button default must NOT be inferred.
        assert await _el(element_id="E1", tag_name="button").is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_button_type_button_is_not_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="button", attributes={"type": "button"})
        assert await el.is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_input_type_checkbox_is_not_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="input", attributes={"type": "checkbox"})
        assert await el.is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_input_type_text_is_not_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="input", attributes={"type": "text"})
        assert await el.is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_div_type_submit_is_not_explicit(self) -> None:
        # role=button / tag alone must not qualify.
        el = _el(element_id="E1", tag_name="div", attributes={"type": "submit"})
        assert await el.is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_anchor_link_is_not_explicit(self) -> None:
        assert await _el(element_id="E1", tag_name="a").is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_custom_combobox_is_not_explicit(self) -> None:
        assert await _el(element_id="E1", tag_name="select").is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_non_string_type_is_not_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="button", attributes={"type": 42})
        assert await el.is_explicit_submit() is False

    @pytest.mark.asyncio
    async def test_attr_read_error_is_not_explicit(self) -> None:
        el = _el(element_id="E1", tag_name="button", attributes={"type": "submit"})
        el.get_attr = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        assert await el.is_explicit_submit() is False


class TestSequentialClickSubmitBypass:
    def _kwargs(self, anchor_element: SkyvernElement) -> dict:
        return {
            "action": MagicMock(),
            "action_history": [],
            "anchor_element": anchor_element,
            "dom": MagicMock(),
            "page": MagicMock(),
            "skyvern_frame": MagicMock(),
            "scraped_page": MagicMock(),
            "incremental_scraped": MagicMock(),
            "task": MagicMock(),
            "step": MagicMock(),
        }

    @pytest.mark.asyncio
    async def test_button_submit_bypasses_sequential_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inner = AsyncMock(return_value=MagicMock(name="sequential_result"))
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="button", attributes={"type": "submit"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is None
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_input_submit_bypasses_sequential_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        inner = AsyncMock(return_value=MagicMock(name="sequential_result"))
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="input", attributes={"type": "submit"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is None
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_button_without_type_invokes_sequential_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = MagicMock(name="sequential_result")
        inner = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="button")

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is sentinel
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_custom_dropdown_invokes_sequential_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = MagicMock(name="sequential_result")
        inner = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="div", attributes={"role": "button"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is sentinel
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_attr_read_error_invokes_sequential_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = MagicMock(name="sequential_result")
        inner = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="button", attributes={"type": "submit"})
        el.get_attr = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is sentinel
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_retargeted_submit_child_recomputes_and_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Retargeting reassigned the click target to the deepest submit child; the
        # wrapper must decide from the element handed in, never stale parent metadata.
        inner = AsyncMock(return_value=MagicMock(name="sequential_result"))
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        child = _el(element_id="CHILD", tag_name="button", attributes={"type": "submit"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(child))

        assert result is None
        inner.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("padded", [" submit ", "submit ", " submit"])
    async def test_whitespace_padded_type_invokes_sequential_handler(
        self, monkeypatch: pytest.MonkeyPatch, padded: str
    ) -> None:
        sentinel = MagicMock(name="sequential_result")
        inner = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        el = _el(element_id="E1", tag_name="input", attributes={"type": padded})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is sentinel
        inner.assert_called_once()


class TestHandleClickActionIntegration:
    """Integration-shaped: drive ``handle_click_action`` end to end so the call-site
    wiring (not just the wrapper) is exercised — the real click owner still runs,
    the dropdown rescrape is bypassed for an explicit submit, and the incremental
    listener is cleaned up. Production-reachable mocks only; no live browser."""

    def _clickable_element(self, *, tag_name: str, type_value: str | None) -> SkyvernElement:
        attributes = {"type": type_value} if type_value is not None else {}
        static = {"id": "E1", "tagName": tag_name, "attributes": attributes}
        el = SkyvernElement(MagicMock(), MagicMock(), static)
        el.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        el.scroll_into_view = AsyncMock()  # type: ignore[method-assign]
        el.get_frame = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        el.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        return el

    def _wire_handler(
        self, monkeypatch: pytest.MonkeyPatch, element: SkyvernElement
    ) -> tuple[AsyncMock, AsyncMock, MagicMock]:
        dom_mock = MagicMock()
        dom_mock.get_skyvern_element_by_id = AsyncMock(return_value=element)
        monkeypatch.setattr(handler_module, "DomUtil", MagicMock(return_value=dom_mock))
        monkeypatch.setattr(handler_module, "get_or_create_wait_config", AsyncMock(return_value=MagicMock()))
        monkeypatch.setattr(handler_module, "get_wait_time", MagicMock(return_value=0))
        monkeypatch.setattr(handler_module.SkyvernFrame, "create_instance", AsyncMock(return_value=MagicMock()))

        incremental = MagicMock()
        incremental.start_listen_dom_increment = AsyncMock()
        incremental.stop_listen_dom_increment = AsyncMock()
        monkeypatch.setattr(handler_module, "IncrementalScrapePage", MagicMock(return_value=incremental))

        chain_click_mock = AsyncMock(return_value=[ActionSuccess()])
        monkeypatch.setattr(handler_module, "chain_click", chain_click_mock)
        sequential_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(handler_module, "handle_sequential_click_for_dropdown", sequential_mock)
        return chain_click_mock, sequential_mock, incremental

    def _page(self) -> MagicMock:
        page = MagicMock()
        page.url = "https://example.com/form"
        page.evaluate = AsyncMock(return_value=False)
        return page

    @pytest.mark.asyncio
    async def test_explicit_submit_clicks_and_bypasses_dropdown_rescrape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        submit_el = self._clickable_element(tag_name="button", type_value="submit")
        chain_click_mock, sequential_mock, incremental = self._wire_handler(monkeypatch, submit_el)

        results = await handle_click_action(
            ClickAction(element_id="E1"), self._page(), MagicMock(), MagicMock(), MagicMock()
        )

        # The actual click owner ran and produced the success result.
        chain_click_mock.assert_awaited_once()
        # The expensive dropdown full-rescrape was bypassed.
        sequential_mock.assert_not_called()
        # Incremental listener cleanup still ran in the finally block.
        incremental.stop_listen_dom_increment.assert_awaited_once()
        # Result is exactly chain_click's output — no synthesized business-success result appended.
        assert results == chain_click_mock.return_value
        assert len(results) == 1
        assert isinstance(results[-1], ActionSuccess)

    @pytest.mark.asyncio
    async def test_non_submit_button_still_invokes_dropdown_rescrape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Positive control: identical wiring, only tag/type differ — proves the
        # bypass above is caused by the submit semantics, not the harness.
        button_el = self._clickable_element(tag_name="button", type_value=None)
        chain_click_mock, sequential_mock, incremental = self._wire_handler(monkeypatch, button_el)

        results = await handle_click_action(
            ClickAction(element_id="E1"), self._page(), MagicMock(), MagicMock(), MagicMock()
        )

        chain_click_mock.assert_awaited_once()
        sequential_mock.assert_awaited_once()
        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert isinstance(results[-1], ActionSuccess)
