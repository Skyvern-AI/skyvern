import ast
import asyncio
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
    TRANSIENT_TEXT_EVENT_LIMIT,
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
async def test_transient_text_observer_preserves_error_when_window_closes_itself(chromium_browser: Browser) -> None:
    now = datetime.now(UTC)
    task = make_task(
        now,
        make_organization(now),
        error_code_mapping={"data_not_downloadable": "archive could not be generated"},
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)
    context = await chromium_browser.new_context()
    try:
        opener = await context.new_page()
        async with context.expect_page() as child_info:
            await opener.evaluate("() => { window.open('about:blank'); }")
        child = await child_info.value
        await child.set_content('<div id="host"></div>')
        observer = TransientPageTextObserver(child)
        await observer.start(scan_initial_visible_state=False)
        async with child.expect_event("close", timeout=10000):
            await child.evaluate(
                """
                () => {
                  setTimeout(() => {
                    const toast = document.createElement('div');
                    toast.setAttribute('role', 'alert');
                    toast.textContent = 'The archive could not be generated';
                    document.getElementById('host').appendChild(toast);
                    setTimeout(() => window.close(), 800);
                  }, 300);
                }
                """
            )
        assert child.is_closed()
        assert [event["text"] for event in observer.events] == ["The archive could not be generated"]
        errors = match_user_defined_errors_from_transient_text(task, step, observer.events)
        assert [error.error_code for error in errors] == ["data_not_downloadable"]
        await observer.stop()
    finally:
        await context.close()


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
        "eventLimit": TRANSIENT_TEXT_EVENT_LIMIT,
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
async def test_transient_text_observer_captures_overlay_that_animates_in_after_insertion(
    chromium_browser: Browser,
) -> None:
    # Production-shaped miss: an error toast is inserted invisible and animates in (opacity 0 -> 1) with
    # no further observed mutation, so a point-in-time visibility check at the insertion mutation drops
    # it and it never becomes step evidence.
    page = await chromium_browser.new_page()
    await page.set_content(
        """
        <style>
          @keyframes skyReveal { from { opacity: 0; } to { opacity: 1; } }
          .sky-toast {
            position: fixed; top: 10px; right: 10px; width: 240px; height: 64px;
            animation: skyReveal 200ms linear 80ms both;
          }
        </style>
        <div id="host"></div>
        """
    )
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=True)
    await page.evaluate(
        """
        () => document.getElementById("host").insertAdjacentHTML(
          "beforeend",
          '<div class="sky-toast" role="alert">Fehlermeldung Beim Herunterladen der Datei ist ein Fehler aufgetreten. Bitte versuchen Sie es spater noch einmal.</div>'
        )
        """
    )
    await page.wait_for_timeout(700)
    await observer.stop()

    assert any(
        "Beim Herunterladen der Datei ist ein Fehler aufgetreten" in event["text"] for event in observer.events
    ), observer.events


@pytest.mark.asyncio
async def test_transient_text_observer_ignores_element_that_never_becomes_visible(
    chromium_browser: Browser,
) -> None:
    # Negative coverage: a stale/hidden overlay that never becomes visible must not be captured, so a
    # successful download (no error surfaced) or an unrelated invisible node cannot cause a false match.
    page = await chromium_browser.new_page()
    await page.set_content('<div id="host"></div>')
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=True)
    await page.evaluate(
        """
        () => document.getElementById("host").insertAdjacentHTML(
          "beforeend",
          '<div style="position:fixed;top:10px;right:10px;width:240px;height:64px;opacity:0" role="alert">Hidden overlay text that never becomes visible</div>'
        )
        """
    )
    await page.wait_for_timeout(700)
    await observer.stop()

    assert observer.events == []


@pytest.mark.asyncio
async def test_transient_text_observer_captures_alert_inserted_near_an_earlier_node_deadline(
    chromium_browser: Browser,
) -> None:
    # A first invisible node is admitted early; a real alert then appears ~850ms later (near the first
    # node's recheck budget) and animates in. Each admitted node must carry its own recheck deadline, so
    # the late alert still gets its full window instead of being dropped by a schedule shared with the
    # earlier node.
    page = await chromium_browser.new_page()
    await page.set_content(
        """
        <style>
          @keyframes skyLateReveal { from { opacity: 0; } to { opacity: 1; } }
          .sky-late-toast {
            position: fixed; top: 90px; right: 10px; width: 240px; height: 64px;
            animation: skyLateReveal 200ms linear 250ms both;
          }
        </style>
        <div id="host"></div>
        """
    )
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=True)
    await page.evaluate(
        """
        () => document.getElementById("host").insertAdjacentHTML(
          "beforeend",
          '<div style="position:fixed;top:10px;right:10px;width:240px;height:64px;opacity:0" role="status">Earlier node that stays invisible the whole time</div>'
        )
        """
    )
    await page.wait_for_timeout(850)
    await page.evaluate(
        """
        () => document.getElementById("host").insertAdjacentHTML(
          "beforeend",
          '<div class="sky-late-toast" role="alert">Late arriving download error alert text</div>'
        )
        """
    )
    await page.wait_for_timeout(800)
    await observer.stop()

    texts = [event["text"] for event in observer.events]
    assert any("Late arriving download error alert text" in text for text in texts), texts
    assert all("Earlier node that stays invisible" not in text for text in texts), texts


@pytest.mark.asyncio
async def test_transient_text_observer_captures_overlay_straddling_post_action_reinstall(
    chromium_browser: Browser,
) -> None:
    # Mirrors the download handler lifecycle: the observer is installed before the action
    # (scan_initial_visible_state=False), then reinstalled after the action
    # (scan_initial_visible_state=True) before the download wait. An error overlay inserted
    # invisible during the action, still mid animation-delay when the reinstall lands, must
    # survive the reinstall and be captured once it animates into visibility.
    page = await chromium_browser.new_page()
    await page.set_content(
        """
        <style>
          @keyframes skyStraddleReveal { from { opacity: 0; } to { opacity: 1; } }
          .sky-straddle-toast {
            position: fixed; top: 10px; right: 10px; width: 240px; height: 64px;
            animation: skyStraddleReveal 200ms linear 250ms both;
          }
        </style>
        <div id="host"></div>
        """
    )
    observer = TransientPageTextObserver(page)

    await observer.start(scan_initial_visible_state=False)
    await page.evaluate(
        """
        () => document.getElementById("host").insertAdjacentHTML(
          "beforeend",
          '<div class="sky-straddle-toast" role="alert">Straddle download error alert text</div>'
        )
        """
    )
    await observer.start(scan_initial_visible_state=True)
    await page.wait_for_timeout(700)
    await observer.stop()

    assert any("Straddle download error alert text" in event["text"] for event in observer.events), observer.events


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


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_scan", [False, True])
async def test_transient_text_observer_bounds_batches_and_preserves_latest_history(
    chromium_browser: Browser, monkeypatch: pytest.MonkeyPatch, initial_scan: bool
) -> None:
    page = await chromium_browser.new_page()
    await page.set_content('<div id="host"></div>')
    batch_sizes: list[int] = []
    expose_binding = page.expose_binding

    async def count_binding(name: str, callback: Callable[..., None]) -> None:
        def record(source: dict[str, Any], payload: Any) -> None:
            batch_sizes.append(len(payload) if isinstance(payload, list) else 1)
            callback(source, payload)

        await expose_binding(name, record)

    monkeypatch.setattr(page, "expose_binding", count_binding)
    observer = TransientPageTextObserver(page)
    if not initial_scan:
        await observer.start(scan_initial_visible_state=False)
    await page.evaluate(
        """
        () => {
          const host = document.getElementById('host');
          for (let i = 0; i < 1000; i++) {
            const node = document.createElement('div');
            node.setAttribute('role', 'status');
            node.textContent = 'Status row ' + i + ' visible content';
            host.appendChild(node);
            node.className = 'status';
            node.style.opacity = '1';
          }
        }
        """
    )
    if initial_scan:
        await observer.start()
    expected = [f"Status row {i} visible content" for i in range(900, 1000)]
    async with asyncio.timeout(10):
        while [event["text"] for event in observer.events] != expected:
            await asyncio.sleep(0.01)
    await observer.stop()
    assert 0 < len(batch_sizes) < 40
    assert max(batch_sizes) <= TRANSIENT_TEXT_EVENT_LIMIT
    assert [event["text"] for event in observer.events] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("reinstall", [False, True])
async def test_transient_text_observer_preserves_repeats_after_history_eviction(
    chromium_browser: Browser, reinstall: bool
) -> None:
    page = await chromium_browser.new_page()
    await page.set_content('<div id="host"></div>')
    observer = TransientPageTextObserver(page)
    await observer.start(scan_initial_visible_state=False)
    expected: list[dict[str, Any]] = []

    async def capture(texts: list[str]) -> None:
        for text in texts:
            _append_text_event(expected, {"text": text})
        await page.evaluate(
            """
            texts => {
              for (const text of texts) {
                const node = document.createElement('div');
                node.textContent = text;
                document.getElementById('host').appendChild(node);
              }
            }
            """,
            texts,
        )
        async with asyncio.timeout(5):
            while [event["text"] for event in observer.events] != [event["text"] for event in expected]:
                await asyncio.sleep(0.01)

    initial = [f"Initial status number {i}" for i in range(100)]
    await capture(initial)
    if reinstall:
        await observer.start()
    # The first repeat is skipped by Python, then the intervening text evicts it.
    await capture([initial[0], "Intervening status number 0", initial[0]])
    await capture([f"Next status number {i}" for i in range(150)])
    await capture([initial[0], initial[0], "Final status message"])
    if reinstall:
        await page.goto("about:blank")
        await page.set_content('<div id="host"></div>')
        await observer.start()
        await capture([f"New document status {i}" for i in range(101)] + [initial[0]])
    await observer.stop()
    assert [event["text"] for event in observer.events] == [event["text"] for event in expected]
    assert len(observer.events) == TRANSIENT_TEXT_EVENT_LIMIT


@pytest.mark.asyncio
async def test_transient_text_observer_preserves_error_after_navigation(chromium_browser: Browser) -> None:
    page = await chromium_browser.new_page()
    await page.set_content("<button>Continue</button>")
    observer = TransientPageTextObserver(page)
    await observer.start(scan_initial_visible_state=False)
    await page.evaluate(
        """
        () => {
          document.querySelector('button').onclick = () => {
            document.body.insertAdjacentHTML('beforeend', '<div role="alert">Archive could not be generated</div>');
            setTimeout(() => { location.href = 'about:blank'; }, 100);
          };
        }
        """
    )
    async with page.expect_navigation():
        await page.get_by_role("button").click()
    await observer.start()
    await observer.stop()
    assert [event["text"] for event in observer.events] == ["Archive could not be generated"]


@pytest.mark.asyncio
async def test_transient_text_observer_failed_stop_does_not_replay_prior_history(chromium_browser: Browser) -> None:
    page = await chromium_browser.new_page()
    await page.set_content('<div id="host"></div>')
    first = TransientPageTextObserver(page)
    await first.start(scan_initial_visible_state=False)
    await page.evaluate(
        "() => document.getElementById('host').insertAdjacentHTML('beforeend', "
        "'<div role=alert>Prior action error message</div>')"
    )
    await page.wait_for_timeout(100)
    with patch(
        "skyvern.webeye.transient_page_observer.SkyvernFrame.evaluate",
        new_callable=AsyncMock,
        side_effect=RuntimeError("synthetic disconnect failure"),
    ):
        await first.stop()
    second = TransientPageTextObserver(page)
    await second.start(scan_initial_visible_state=False)
    await page.goto("about:blank")
    await second.stop()
    assert [event["text"] for event in first.events] == ["Prior action error message"]
    assert second.events == []


@pytest.mark.asyncio
async def test_transient_text_observer_successful_download_has_no_error_match(chromium_browser: Browser) -> None:
    now = datetime.now(UTC)
    task = make_task(
        now,
        make_organization(now),
        error_code_mapping={"data_not_downloadable": "archive could not be generated"},
    )
    step = make_step(now, task, step_id="step-1", status=StepStatus.running, order=1, output=None)
    page = await chromium_browser.new_page()
    await page.set_content('<a download="report.txt" href="data:text/plain,completed">Download report</a>')
    observer = TransientPageTextObserver(page)
    await observer.start(scan_initial_visible_state=False)
    async with page.expect_download() as download_info:
        await page.get_by_role("link", name="Download report").click()
    download = await download_info.value
    assert await download.failure() is None
    assert download.suggested_filename == "report.txt"
    await observer.stop()
    assert match_user_defined_errors_from_transient_text(task, step, observer.events) == []
