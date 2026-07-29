import ast
import inspect
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from playwright.async_api import Browser, async_playwright

from skyvern.forge.sdk.models import StepStatus
from skyvern.webeye import transient_page_observer
from skyvern.webeye.transient_page_observer import (
    TRANSIENT_TEXT_BINDING_NAME,
    TRANSIENT_TEXT_MATCH_CONFIDENCE,
    TRANSIENT_TEXT_MAX_LENGTH,
    TRANSIENT_TEXT_MIN_LENGTH,
    TRANSIENT_TEXT_OBSERVER_STATE_KEY,
    TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT,
    TransientPageTextObserver,
    _append_text_event,
    _format_observed_text_reasoning,
    _has_meaningful_text_overlap,
    match_user_defined_errors_from_transient_text,
)
from tests.unit.helpers import make_organization, make_step, make_task


class _FakePage:
    def __init__(self) -> None:
        self.expose_binding = AsyncMock()
        self.evaluate = AsyncMock()


@pytest.mark.asyncio
async def test_transient_text_observer_start_uses_skyvern_frame_evaluate() -> None:
    page = _FakePage()
    observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch("skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await observer.start(scan_initial_visible_state=False)

    evaluate.assert_awaited_once()
    call = evaluate.await_args
    assert call.kwargs["frame"] is page
    assert "new MutationObserver" in call.kwargs["expression"]
    assert "visibleSemanticTexts" in call.kwargs["expression"]
    assert call.kwargs["arg"] == {
        "bindingName": TRANSIENT_TEXT_BINDING_NAME,
        "stateKey": TRANSIENT_TEXT_OBSERVER_STATE_KEY,
        "minLength": TRANSIENT_TEXT_MIN_LENGTH,
        "maxLength": TRANSIENT_TEXT_MAX_LENGTH,
        "scanInitialVisibleState": False,
    }


@pytest.mark.asyncio
async def test_transient_text_observer_stop_uses_skyvern_frame_evaluate() -> None:
    page = _FakePage()
    observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch("skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate", new_callable=AsyncMock) as evaluate:
        await observer.start()
        evaluate.reset_mock()
        await observer.stop()

    evaluate.assert_awaited_once()
    call = evaluate.await_args
    assert call.kwargs["frame"] is page
    assert "state.observer?.disconnect?.()" in call.kwargs["expression"]
    assert "delete window[stateKey]" in call.kwargs["expression"]
    assert call.kwargs["arg"] == {
        "bindingName": TRANSIENT_TEXT_BINDING_NAME,
        "stateKey": TRANSIENT_TEXT_OBSERVER_STATE_KEY,
    }


@pytest_asyncio.fixture
async def chromium_browser() -> AsyncIterator[Browser]:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:
            error = str(exc)
            if "Executable doesn't exist" in error or (
                "MachPortRendezvousServer" in error and "Permission denied" in error
            ):
                pytest.skip("Chromium unavailable in this environment")
            raise

        try:
            yield browser
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_transient_text_observer_reuses_binding_and_hands_off_active_observer() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["name"] = name
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding

    first_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]
    await first_observer.start()
    captured["callback"]({}, {"text": "First transient error", "timestamp_ms": 1})
    await first_observer.stop()

    second_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]
    await second_observer.start()
    captured["callback"]({}, {"text": "Second transient error", "timestamp_ms": 2})
    await second_observer.stop()

    assert captured["name"] == TRANSIENT_TEXT_BINDING_NAME
    page.expose_binding.assert_awaited_once()
    assert [event["text"] for event in first_observer.events] == ["First transient error"]
    assert [event["text"] for event in second_observer.events] == ["Second transient error"]


@pytest.mark.asyncio
async def test_transient_text_observer_scans_visible_state_when_installed(chromium_browser: Browser) -> None:
    page = await chromium_browser.new_page()
    await page.set_content('<div role="alert">A generated document is unavailable</div>')
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=True)
    await page.wait_for_timeout(50)
    await observer.stop()

    assert [event["text"] for event in observer.events] == ["A generated document is unavailable"]


@pytest.mark.asyncio
async def test_transient_text_observer_can_observe_mutations_without_scanning_visible_state(
    chromium_browser: Browser,
) -> None:
    page = await chromium_browser.new_page()
    await page.set_content('<div role="alert">A stale generated document error</div>')
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=False)
    await page.wait_for_timeout(50)
    assert observer.events == []

    await observer.start(scan_initial_visible_state=True)
    await page.wait_for_timeout(50)
    assert observer.events == []

    await page.locator("[role='alert']").evaluate("element => element.textContent = 'A new generated document error'")
    await page.wait_for_timeout(50)
    await observer.stop()

    assert [event["text"] for event in observer.events] == ["A new generated document error"]


@pytest.mark.asyncio
async def test_transient_text_observer_failed_reinstall_preserves_routing_and_cleanup_ownership() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(_name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding
    observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch(
        "skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate",
        new_callable=AsyncMock,
        side_effect=[None, RuntimeError("synthetic reinstall failure"), None],
    ) as evaluate:
        await observer.start()
        await observer.start()
        captured["callback"]({}, {"text": "Transient error after failed reinstall"})
        await observer.stop()

    assert [event["text"] for event in observer.events] == ["Transient error after failed reinstall"]
    assert evaluate.await_count == 3


@pytest.mark.asyncio
async def test_transient_text_observer_first_install_failure_has_no_routing_or_cleanup_ownership() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(_name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding
    observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch(
        "skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate",
        new_callable=AsyncMock,
        side_effect=RuntimeError("synthetic first install failure"),
    ) as evaluate:
        await observer.start()
        captured["callback"]({}, {"text": "Transient error after failed first install"})
        await observer.stop()

    assert observer.events == []
    assert evaluate.await_count == 1


@pytest.mark.asyncio
async def test_transient_text_observer_stop_failure_releases_routing_and_cleanup_ownership() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(_name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding
    first_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]
    second_observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch(
        "skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate",
        new_callable=AsyncMock,
        side_effect=[None, RuntimeError("synthetic stop failure"), None, None],
    ) as evaluate:
        await first_observer.start()
        await first_observer.stop()
        captured["callback"]({}, {"text": "Transient error after failed stop"})
        await second_observer.start()
        captured["callback"]({}, {"text": "Transient error after ownership release"})
        await second_observer.stop()

    assert first_observer.events == []
    assert [event["text"] for event in second_observer.events] == ["Transient error after ownership release"]
    assert page.expose_binding.await_count == 1
    assert evaluate.await_count == 4


def test_transient_text_observer_has_no_direct_page_evaluate_calls() -> None:
    source = inspect.getsource(transient_page_observer)
    tree = ast.parse(source)
    evaluate_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "evaluate"
    ]

    assert evaluate_calls
    assert all(
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "SkyvernFrame"
        for call in evaluate_calls
    )


@pytest.mark.asyncio
async def test_transient_text_observer_info_diagnostics_exclude_raw_text() -> None:
    page = _FakePage()
    captured: dict[str, Any] = {}

    async def expose_binding(_name: str, callback: Callable[[dict[str, Any], Any], None]) -> None:
        captured["callback"] = callback

    page.expose_binding.side_effect = expose_binding
    observer = TransientPageTextObserver(page)  # type: ignore[arg-type]

    with patch("skyvern.webeye.transient_page_observer.LOG.info") as info:
        await observer.start()
        captured["callback"]({}, {"text": "Synthetic private transient message", "timestamp_ms": 1})
        await observer.stop()

    rendered_logs = repr(info.call_args_list)
    assert "Synthetic private transient message" not in rendered_logs
    assert any(call.kwargs.get("accepted_event_count") == 1 for call in info.call_args_list)


def test_transient_text_overlap_requires_longer_word_window() -> None:
    assert not _has_meaningful_text_overlap(
        "the file is ready to download now",
        "show an error when the file is ready for review",
    )
    assert _has_meaningful_text_overlap(
        "Example download failure says the generated archive could not be saved",
        "Return this error if the page displays download failure says the generated archive could not be saved",
    )


def test_transient_text_overlap_normalizes_inputs() -> None:
    assert _has_meaningful_text_overlap(
        "DOWNLOAD FAILURE SAYS THE GENERATED ARCHIVE COULD NOT BE SAVED",
        "Return this error if the page displays download failure says the generated archive could not be saved",
    )


def test_append_text_event_omits_absent_metadata() -> None:
    events: list[dict[str, Any]] = []

    _append_text_event(
        events,
        {
            "text": "Download failure says the generated archive could not be saved",
            "tag": None,
            "role": "alert",
        },
    )

    assert events == [
        {
            "tag": None,
            "role": "alert",
            "text": "Download failure says the generated archive could not be saved",
        }
    ]


def test_format_observed_text_reasoning_truncates_snippets() -> None:
    text = "x" * (TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT + 20)

    reasoning = _format_observed_text_reasoning([text])

    assert reasoning == f"{'x' * TRANSIENT_TEXT_REASONING_SNIPPET_LIMIT}..."


def test_match_user_defined_error_from_transient_text_uses_heuristic_confidence() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "data_not_downloadable": (
                "Return this error if the page displays download failure says the generated archive could not be saved"
            ),
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    errors = match_user_defined_errors_from_transient_text(
        task,
        step,
        [{"text": "Example download failure says the generated archive could not be saved"}],
    )

    assert len(errors) == 1
    assert errors[0].error_code == "data_not_downloadable"
    assert errors[0].confidence_float == TRANSIENT_TEXT_MATCH_CONFIDENCE


def test_match_user_defined_error_reasoning_includes_only_text_matching_selected_mapping() -> None:
    now = datetime.now(UTC)
    organization = make_organization(now)
    task = make_task(
        now,
        organization,
        error_code_mapping={
            "data_not_downloadable": "generated archive could not be saved",
            "other_error": "unrelated status update",
        },
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)

    errors = match_user_defined_errors_from_transient_text(
        task,
        step,
        [
            {"text": "Unrelated status update that must remain private"},
            {"text": "The generated archive could not be saved"},
        ],
    )

    assert [error.error_code for error in errors] == ["data_not_downloadable"]
    assert "generated archive could not be saved" in errors[0].reasoning
    assert "Unrelated status update" not in errors[0].reasoning
