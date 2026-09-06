"""SKY-12939 Lane B: explicit-submit bypass of the dropdown sequential-click rescrape.

Positive allowlist only — exact ``button[type=submit]`` / ``input[type=submit]``
after target resolution/retargeting. Every ambiguous, dropdown, link, checkbox,
custom control, missing-type, or read-error case must fall through to the
existing ``handle_sequential_click_for_dropdown`` path.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import skyvern.webeye.actions.handler as handler_module
from skyvern.forge.agent_functions import AgentFunction
from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye.actions.actions import ClickAction
from skyvern.webeye.actions.handler import (
    ActionHandler,
    handle_click_action,
    handle_sequential_click_with_submit_bypass,
)
from skyvern.webeye.actions.responses import ActionSuccess
from skyvern.webeye.utils.dom import SkyvernElement
from tests.unit.helpers import make_organization, make_step, make_task


def _el(*, element_id: str, tag_name: str, attributes: dict | None = None) -> SkyvernElement:
    static = {"id": element_id, "tagName": tag_name, "attributes": attributes or {}}
    return SkyvernElement(MagicMock(), MagicMock(), static)


@pytest.fixture(autouse=True)
def _oss_neutral_submit_recognizer(monkeypatch: pytest.MonkeyPatch) -> None:
    # The stub app's AGENT_FUNCTION auto-mocks unset methods as truthy AsyncMocks, which would make
    # the recognizer seam bypass everything. Pin the OSS-neutral default (recognizes nothing) here;
    # tests needing recognition override this method afterwards.
    monkeypatch.setattr(
        handler_module.app.AGENT_FUNCTION, "is_recognized_submit_control", AsyncMock(return_value=False)
    )


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
    async def test_agent_function_recognized_submit_bypasses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When the AGENT_FUNCTION seam recognizes the control (cloud recognizes a JS-wired submit),
        # the wrapper bypasses the rescrape exactly like an explicit type=submit. The anchor here is
        # an ordinary type=button with no hooks — only the seam's verdict drives the bypass.
        inner = AsyncMock(return_value=MagicMock(name="sequential_result"))
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        monkeypatch.setattr(
            handler_module.app.AGENT_FUNCTION, "is_recognized_submit_control", AsyncMock(return_value=True)
        )
        el = _el(element_id="E1", tag_name="button", attributes={"type": "button"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is None
        inner.assert_not_called()

    @pytest.mark.asyncio
    async def test_oss_base_recognizes_no_submit_control_and_rescrapes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # OSS neutrality: bind the REAL base AgentFunction — it recognizes no submit control, so a
        # click runs the normal rescrape. The hook-specific recognizer is a deployment-layer contract
        # tested elsewhere; no submit fingerprint lives in skyvern/ or tests/unit.
        sentinel = MagicMock(name="sequential_result")
        inner = AsyncMock(return_value=sentinel)
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", inner)
        monkeypatch.setattr(
            handler_module.app.AGENT_FUNCTION,
            "is_recognized_submit_control",
            AgentFunction().is_recognized_submit_control,
        )
        el = _el(element_id="E1", tag_name="button", attributes={"type": "button"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is sentinel
        inner.assert_called_once()

    @pytest.mark.asyncio
    async def test_bypass_log_marks_recognizer_arm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Observability: the bypass log must carry recognized_submit_control=True when the deployment
        # recognizer arm (not the legacy explicit type=submit) caused the bypass. Message unchanged.
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", AsyncMock())
        monkeypatch.setattr(
            handler_module.app.AGENT_FUNCTION, "is_recognized_submit_control", AsyncMock(return_value=True)
        )
        log_mock = MagicMock()
        monkeypatch.setattr(handler_module, "LOG", log_mock)
        el = _el(element_id="E1", tag_name="button", attributes={"type": "button"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is None
        log_mock.info.assert_called_once()
        args, kwargs = log_mock.info.call_args
        assert args[0] == "Explicit submit click; bypassing the dropdown sequential-click rescrape"
        assert kwargs["recognized_submit_control"] is True

    @pytest.mark.asyncio
    async def test_bypass_log_marks_legacy_explicit_submit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The legacy explicit type=submit arm logs recognized_submit_control=False, and the recognizer
        # is never consulted (short-circuit preserved). Message unchanged.
        monkeypatch.setattr("skyvern.webeye.actions.handler.handle_sequential_click_for_dropdown", AsyncMock())
        recognizer = AsyncMock(side_effect=AssertionError("recognizer must not be called for explicit submit"))
        monkeypatch.setattr(handler_module.app.AGENT_FUNCTION, "is_recognized_submit_control", recognizer)
        log_mock = MagicMock()
        monkeypatch.setattr(handler_module, "LOG", log_mock)
        el = _el(element_id="E1", tag_name="button", attributes={"type": "submit"})

        result = await handle_sequential_click_with_submit_bypass(**self._kwargs(el))

        assert result is None
        recognizer.assert_not_called()
        log_mock.info.assert_called_once()
        args, kwargs = log_mock.info.call_args
        assert args[0] == "Explicit submit click; bypassing the dropdown sequential-click rescrape"
        assert kwargs["recognized_submit_control"] is False

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
    async def test_seam_recognized_submit_clicks_and_bypasses_dropdown_rescrape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end at the handle_click_action entry: when the AGENT_FUNCTION seam recognizes the
        # control (cloud recognizes a JS-wired submit), the click runs and the expensive rescrape is
        # bypassed. The anchor is an ordinary type=button — only the seam verdict drives the bypass.
        submit_el = self._clickable_element(tag_name="button", type_value="button")
        chain_click_mock, sequential_mock, incremental = self._wire_handler(monkeypatch, submit_el)
        monkeypatch.setattr(
            handler_module.app.AGENT_FUNCTION, "is_recognized_submit_control", AsyncMock(return_value=True)
        )

        results = await handle_click_action(
            ClickAction(element_id="E1"), self._page(), MagicMock(), MagicMock(), MagicMock()
        )

        chain_click_mock.assert_awaited_once()
        sequential_mock.assert_not_called()
        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert results == chain_click_mock.return_value
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


class _FakePage:
    """Minimal Playwright-page stand-in that records event listeners and can emit
    to them, so a synthesized download during the click window is observable and
    listener-leak assertions are exact."""

    def __init__(self, url: str = "https://example.com/form") -> None:
        self.url = url
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str, callback: Callable) -> None:
        self._listeners[event].append(callback)

    def off(self, event: str, callback: Callable) -> None:
        if callback in self._listeners[event]:
            self._listeners[event].remove(callback)

    remove_listener = off

    async def evaluate(self, *args: object, **kwargs: object) -> bool:
        return False

    def emit(self, event: str, arg: object) -> None:
        for callback in list(self._listeners[event]):
            callback(arg)

    def listener_count(self, event: str) -> int:
        return len(self._listeners[event])


@pytest.fixture
def false_click_eligible() -> object:
    token = handler_module._false_click_download_eligible.set(True)
    yield
    handler_module._false_click_download_eligible.reset(token)


class TestFileDownloadFalseClickBypass:
    """A file-download block observing a same-action download should skip the expensive
    post-click dropdown/custom-select rescrape, gated by the ``file_download_false_click_eligible``
    authority. It must not fabricate download registration and must leak no listeners."""

    def _clickable_element(self) -> SkyvernElement:
        static = {"id": "E1", "tagName": "button", "attributes": {}}
        el = SkyvernElement(MagicMock(), MagicMock(), static)
        el.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        el.scroll_into_view = AsyncMock()  # type: ignore[method-assign]
        el.get_frame = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        el.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        return el

    def _wire(self, monkeypatch: pytest.MonkeyPatch, element: SkyvernElement) -> tuple[AsyncMock, AsyncMock, MagicMock]:
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

    async def _run(self, page: _FakePage) -> list:
        return await handle_click_action(ClickAction(element_id="E1"), page, MagicMock(), MagicMock(), MagicMock())

    @pytest.mark.asyncio
    async def test_same_page_download_bypasses_sequential(
        self, monkeypatch: pytest.MonkeyPatch, false_click_eligible: object
    ) -> None:
        page = _FakePage()
        chain_click_mock, sequential_mock, incremental = self._wire(monkeypatch, self._clickable_element())

        async def emit_download(*args: object, **kwargs: object) -> list:
            page.emit("download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_download

        results = await self._run(page)

        chain_click_mock.assert_awaited_once()
        sequential_mock.assert_not_called()
        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert results == chain_click_mock.return_value
        assert results[-1].download_triggered is None
        assert results[-1].downloaded_files is None
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0

    @pytest.mark.asyncio
    async def test_popup_download_bypasses_sequential(
        self, monkeypatch: pytest.MonkeyPatch, false_click_eligible: object
    ) -> None:
        page = _FakePage()
        popup = _FakePage("https://example.com/popup")
        chain_click_mock, sequential_mock, _ = self._wire(monkeypatch, self._clickable_element())

        async def emit_popup_download(*args: object, **kwargs: object) -> list:
            page.emit("popup", popup)
            popup.emit("download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_popup_download

        results = await self._run(page)

        sequential_mock.assert_not_called()
        assert results == chain_click_mock.return_value
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0
        assert popup.listener_count("download") == 0

    @pytest.mark.asyncio
    async def test_no_download_runs_sequential(
        self, monkeypatch: pytest.MonkeyPatch, false_click_eligible: object
    ) -> None:
        page = _FakePage()
        chain_click_mock, sequential_mock, _ = self._wire(monkeypatch, self._clickable_element())

        results = await self._run(page)

        chain_click_mock.assert_awaited_once()
        sequential_mock.assert_awaited_once()
        assert isinstance(results[-1], ActionSuccess)
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0

    @pytest.mark.asyncio
    async def test_not_eligible_ignores_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No ``false_click_eligible`` fixture: an ordinary (non-file-download) click that happens
        # to emit a download must still run the standard sequential path — the bypass is gated.
        page = _FakePage()
        chain_click_mock, sequential_mock, _ = self._wire(monkeypatch, self._clickable_element())

        async def emit_download(*args: object, **kwargs: object) -> list:
            page.emit("download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_download

        await self._run(page)

        sequential_mock.assert_awaited_once()
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0

    @pytest.mark.asyncio
    async def test_download_queued_after_click_bypasses_sequential(
        self, monkeypatch: pytest.MonkeyPatch, false_click_eligible: object
    ) -> None:
        # Real Playwright delivers the ``download`` event on a later event-loop turn, after the
        # click await resolves — the common case for a dynamically-registered handler with no
        # static ``onclick`` attribute (``has_onclick_attr`` False, so no 1s animation wait yields
        # between the two bypass checks). Schedule the emit via ``call_soon`` instead of emitting
        # synchronously inside the click, and the bypass must still fire.
        import asyncio

        page = _FakePage()
        chain_click_mock, sequential_mock, incremental = self._wire(monkeypatch, self._clickable_element())

        async def emit_download_next_turn(*args: object, **kwargs: object) -> list:
            asyncio.get_running_loop().call_soon(page.emit, "download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_download_next_turn

        results = await self._run(page)

        chain_click_mock.assert_awaited_once()
        sequential_mock.assert_not_called()
        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert results == chain_click_mock.return_value
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0

    @pytest.mark.asyncio
    async def test_listeners_removed_on_exception(
        self, monkeypatch: pytest.MonkeyPatch, false_click_eligible: object
    ) -> None:
        page = _FakePage()
        chain_click_mock, _, incremental = self._wire(monkeypatch, self._clickable_element())
        chain_click_mock.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await self._run(page)

        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0


class TestHandleActionPublicPathFalseClickBypass:
    """Drive the real public ``ActionHandler.handle_action`` entry point with the popup-grace
    setting at its production default of 0. The fixture-based tests above preset the ContextVar
    directly, so they cannot catch a wiring regression where ``handle_action`` only arms the
    bypass probe under grace > 0 — the activation defect the reviewer flagged. Here the probe
    must be armed purely from ``file_download_false_click_eligible``, independent of grace."""

    def _clickable_element(self) -> SkyvernElement:
        static = {"id": "E1", "tagName": "button", "attributes": {}}
        el = SkyvernElement(MagicMock(), MagicMock(), static)
        el.is_disabled = AsyncMock(return_value=False)  # type: ignore[method-assign]
        el.scroll_into_view = AsyncMock()  # type: ignore[method-assign]
        el.get_frame = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
        el.get_element_handler = AsyncMock(return_value=MagicMock())  # type: ignore[method-assign]
        return el

    def _wire_click_internals(
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

    def _wire_action_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Importing ``cloud`` (e.g. via a tests/cloud file collected earlier in the shard)
        # registers CLICK setup/teardown hooks on these class-level dicts, which would
        # short-circuit ``_handle_action`` before the real click handler. Isolate the
        # public-path tests from that global registration, matching the idiom in
        # test_action_execution_timeout.py / test_input_text_tel_card_routing.py.
        monkeypatch.setattr(ActionHandler, "_setup_action_types", {})
        monkeypatch.setattr(ActionHandler, "_teardown_action_types", {})
        app_mock = MagicMock()
        app_mock.BROWSER_MANAGER.get_for_task.return_value = MagicMock()
        app_mock.AGENT_FUNCTION.wait_for_challenge_solver = AsyncMock()
        app_mock.AGENT_FUNCTION.is_recognized_submit_control = AsyncMock(return_value=False)
        app_mock.DATABASE.workflow_params.create_action = AsyncMock(return_value=SimpleNamespace(action_id="a-1"))
        monkeypatch.setattr(handler_module, "app", app_mock)
        monkeypatch.setattr(handler_module, "preflight_action", MagicMock(return_value=None))
        # Pin the production default explicitly so the test proves the bypass no longer depends on grace.
        monkeypatch.setattr(handler_module.settings, "FILE_DOWNLOAD_FALSE_CLICK_POPUP_GRACE_SECONDS", 0)

    def _context(self) -> tuple:
        now = datetime.now(UTC)
        organization = make_organization(now)
        task = make_task(now, organization)
        step = make_step(now, task, step_id="step-1", status=StepStatus.created, order=0, output=None)
        return task, step

    def _scraped_page(self) -> MagicMock:
        scraped_page = MagicMock()
        scraped_page.id_to_element_dict = {"E1": {"id": "E1"}}
        return scraped_page

    @pytest.mark.asyncio
    async def test_grace_zero_same_page_download_bypasses_sequential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        task, step = self._context()
        action = ClickAction(element_id="E1", download=False)
        chain_click_mock, sequential_mock, incremental = self._wire_click_internals(
            monkeypatch, self._clickable_element()
        )
        self._wire_action_wrapper(monkeypatch)
        page = _FakePage()

        async def emit_download(*args: object, **kwargs: object) -> list:
            page.emit("download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_download

        results = await ActionHandler.handle_action(
            self._scraped_page(), task, step, page, action, file_download_false_click_eligible=True
        )

        chain_click_mock.assert_awaited_once()
        # The expensive dropdown/custom-select rescrape was skipped because the same-action download
        # was observed — reachable through the public path even with grace at its default of 0.
        sequential_mock.assert_not_called()
        incremental.stop_listen_dom_increment.assert_awaited_once()
        assert results == chain_click_mock.return_value
        # No download registration was fabricated; persistence stays owned by the download path.
        assert results[-1].download_triggered is None
        assert results[-1].downloaded_files is None
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0
        # ContextVar was reset on exit — no leak into the next action in this task.
        assert handler_module._false_click_download_eligible.get() is False

    @pytest.mark.asyncio
    async def test_grace_zero_not_eligible_runs_sequential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Control: without the file-download false-click authority, an ordinary click that happens to
        # emit a download must still run the standard sequential path, and the probe stays disarmed.
        task, step = self._context()
        action = ClickAction(element_id="E1", download=False)
        chain_click_mock, sequential_mock, _ = self._wire_click_internals(monkeypatch, self._clickable_element())
        self._wire_action_wrapper(monkeypatch)
        page = _FakePage()

        async def emit_download(*args: object, **kwargs: object) -> list:
            page.emit("download", MagicMock(name="download"))
            return [ActionSuccess()]

        chain_click_mock.side_effect = emit_download

        await ActionHandler.handle_action(
            self._scraped_page(), task, step, page, action, file_download_false_click_eligible=False
        )

        sequential_mock.assert_awaited_once()
        assert page.listener_count("download") == 0
        assert page.listener_count("popup") == 0
        assert handler_module._false_click_download_eligible.get() is False
