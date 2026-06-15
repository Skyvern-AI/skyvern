"""Tests for browser exfiltration channel helpers."""

from __future__ import annotations

import asyncio
import gc
import json
import typing as t
import weakref
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltratedEventSource,
    ExfiltrationChannel,
    PageConsoleCapture,
)


def _make_vnc_channel() -> MagicMock:
    browser_session = MagicMock()
    browser_session.browser_address = "http://localhost:9222"
    browser_session.persistent_browser_session_id = "pbs_123"

    vnc_channel = MagicMock()
    vnc_channel.browser_session = browser_session
    vnc_channel.identity = {
        "client_id": "client-1",
        "browser_session_id": browser_session.persistent_browser_session_id,
    }

    return vnc_channel


def _make_event_data() -> dict[str, object]:
    return {
        "type": "click",
        "timestamp": 1234,
        "url": "https://example.com",
        "target": {
            "tagName": "BUTTON",
            "id": "submit",
            "skyId": "sky-1",
            "text": ["Submit"],
        },
    }


def _make_page(url: str = "https://example.com") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.context = MagicMock()
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    page.add_init_script = AsyncMock()
    page.evaluate = AsyncMock()
    page.expose_binding = AsyncMock()
    return page


def _make_channel(on_event: MagicMock | None = None) -> tuple[ExfiltrationChannel, MagicMock]:
    event_callback = on_event or MagicMock()
    return ExfiltrationChannel(on_event=event_callback, vnc_channel=_make_vnc_channel()), event_callback


@pytest.fixture(autouse=True)
def restore_exfiltration_channel_class_state() -> t.Iterator[None]:
    active_binding_channels = weakref.WeakKeyDictionary(ExfiltrationChannel._active_binding_channels)
    binding_registered_pages = weakref.WeakSet(ExfiltrationChannel._binding_registered_pages)
    adorn_init_script_pages = weakref.WeakSet(ExfiltrationChannel._adorn_init_script_pages)
    rearm_in_flight_pages = weakref.WeakKeyDictionary(ExfiltrationChannel._rearm_in_flight_pages)
    rearm_pending_full_nav_pages = weakref.WeakSet(ExfiltrationChannel._rearm_pending_full_nav_pages)

    yield

    ExfiltrationChannel._active_binding_channels = active_binding_channels
    ExfiltrationChannel._binding_registered_pages = binding_registered_pages
    ExfiltrationChannel._adorn_init_script_pages = adorn_init_script_pages
    ExfiltrationChannel._rearm_in_flight_pages = rearm_in_flight_pages
    ExfiltrationChannel._rearm_pending_full_nav_pages = rearm_pending_full_nav_pages


class TestExfiltrationChannelEvents:
    def test_binding_event_emits_user_interaction(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page()
        event_data = _make_event_data()
        ExfiltrationChannel._active_binding_channels[page] = channel

        channel._handle_binding_event({"page": page}, event_data)

        on_event.assert_called_once()
        emitted = on_event.call_args.args[0]
        assert len(emitted) == 1
        assert emitted[0].source == ExfiltratedEventSource.CONSOLE
        assert emitted[0].event_name == "user_interaction"
        assert emitted[0].params == event_data

    def test_page_tracking_releases_collected_pages(self) -> None:
        channel, _ = _make_channel()
        page = _make_page()
        page_ref = weakref.ref(page)

        ExfiltrationChannel._active_binding_channels[page] = channel
        ExfiltrationChannel._binding_registered_pages.add(page)
        channel._page_console_captures[page] = PageConsoleCapture(console_listener=MagicMock())

        del page
        gc.collect()

        assert page_ref() is None
        assert not ExfiltrationChannel._active_binding_channels
        assert not ExfiltrationChannel._binding_registered_pages
        assert not channel._page_console_captures

    @pytest.mark.asyncio
    async def test_playwright_console_text_payload_emits_user_interaction(self) -> None:
        channel, on_event = _make_channel()
        event_data = _make_event_data()

        message = MagicMock()
        message.args = []
        message.text = f"[EXFIL] {json.dumps(event_data)}"

        await channel._handle_console_event_async(message)

        on_event.assert_called_once()
        assert on_event.call_args.args[0][0].params == event_data

    @pytest.mark.asyncio
    async def test_playwright_console_listener_tracks_event_task(self) -> None:
        channel, _ = _make_channel()
        started = asyncio.Event()
        release = asyncio.Event()

        async def handle_console_event(_: object) -> None:
            started.set()
            await release.wait()

        channel._handle_console_event_async = handle_console_event  # type: ignore[method-assign]

        channel._handle_console_event(MagicMock())
        await started.wait()

        tasks = list(channel._pending_event_tasks)
        assert len(tasks) == 1

        release.set()
        await asyncio.gather(*tasks)
        assert not channel._pending_event_tasks

    @pytest.mark.asyncio
    async def test_playwright_console_args_payload_emits_when_text_format_differs(self) -> None:
        channel, on_event = _make_channel()
        event_data = _make_event_data()

        marker = MagicMock()
        marker.json_value = AsyncMock(return_value="[EXFIL]")
        payload = MagicMock()
        payload.json_value = AsyncMock(return_value=json.dumps(event_data))
        message = MagicMock()
        message.args = [marker, payload]
        message.text = "[EXFIL] JSHandle@object"

        await channel._handle_console_event_async(message)

        on_event.assert_called_once()
        assert on_event.call_args.args[0][0].params == event_data

    @pytest.mark.asyncio
    async def test_duplicate_binding_console_and_runtime_events_emit_once(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page()
        event_data = _make_event_data()
        ExfiltrationChannel._active_binding_channels[page] = channel

        message = MagicMock()
        message.args = []
        message.text = f"[EXFIL] {json.dumps(event_data)}"

        channel._handle_binding_event({"page": page}, event_data)
        await channel._handle_console_event_async(message)
        await channel._handle_runtime_console_event_async(
            {
                "args": [
                    {"type": "string", "value": "[EXFIL]"},
                    {"type": "string", "value": json.dumps(event_data)},
                ]
            }
        )

        on_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_exfiltrate_registers_binding_console_script_and_cdp_fallback(self) -> None:
        channel, _ = _make_channel()
        page = _make_page()
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        cdp_session.on = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        result = await channel.exfiltrate(page)

        assert result is channel
        page.expose_binding.assert_awaited_once_with(channel.BINDING_NAME, channel._handle_binding_event)
        page.on.assert_called_once()
        assert page.add_init_script.await_count == 2
        assert page.evaluate.await_count == 2
        assert channel._page_console_captures[page].cdp_session is cdp_session
        cdp_session.send.assert_awaited_once_with("Runtime.enable")
        cdp_session.on.assert_called_once()

    @pytest.mark.asyncio
    async def test_page_cdp_console_callback_tracks_event_task(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page()
        event_data = _make_event_data()
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        cdp_session.on = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        await channel._attach_page_cdp_console_capture(page)

        event_name, callback = cdp_session.on.call_args.args
        assert event_name == "Runtime.consoleAPICalled"

        callback(
            {
                "args": [
                    {"type": "string", "value": "[EXFIL]"},
                    {"type": "string", "value": json.dumps(event_data)},
                ]
            }
        )
        tasks = list(channel._pending_event_tasks)
        assert len(tasks) == 1

        await asyncio.gather(*tasks)
        assert not channel._pending_event_tasks
        on_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_exfiltrate_keeps_binding_and_console_when_cdp_fallback_fails(self) -> None:
        channel, _ = _make_channel()
        page = _make_page()
        page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("boom"))

        result = await channel.exfiltrate(page)

        assert result is channel
        page.expose_binding.assert_awaited_once_with(channel.BINDING_NAME, channel._handle_binding_event)
        page.on.assert_called_once()
        assert channel._page_console_captures[page].cdp_session is None

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_network_activity_flush_task(self) -> None:
        events: list[object] = []
        channel, _ = _make_channel(on_event=lambda messages: events.extend(messages))
        channel.NETWORK_ACTIVITY_THROTTLE_SECONDS = 0.05

        channel._handle_network_activity()
        assert len(events) == 1

        channel._handle_network_activity()
        assert len(events) == 1
        assert channel._network_activity_flush_task is not None
        assert not channel._network_activity_flush_task.done()

        await channel.stop()

        assert channel._network_activity_flush_task is None
        await asyncio.sleep(0.1)
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_exfiltrate_rearms_existing_page_without_duplicate_listeners(self) -> None:
        channel, _ = _make_channel()
        page = _make_page()
        page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("boom"))

        await channel.exfiltrate(page)
        page.on.reset_mock()
        page.add_init_script.reset_mock()
        page.evaluate.reset_mock()

        result = await channel.exfiltrate(page)

        assert result is channel
        page.on.assert_not_called()
        page.add_init_script.assert_not_awaited()
        assert page.evaluate.await_count == 2


class TestNavigationReExfiltration:
    @pytest.mark.asyncio
    async def test_frame_navigated_waits_for_load_before_rearm(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page("https://example.com/next")
        page.wait_for_load_state = AsyncMock()
        channel.page = page

        channel._ensure_binding = AsyncMock()
        channel.exfiltrate = AsyncMock(return_value=channel)
        channel.adorn = AsyncMock(return_value=channel)

        channel._handle_cdp_event("nav:frame_navigated", {"frame": {"url": "https://example.com/next"}})

        on_event.assert_called_once()
        if channel._pending_event_tasks:
            await asyncio.gather(*channel._pending_event_tasks)

        page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=10_000)
        channel._ensure_binding.assert_awaited_once_with(page)
        channel.exfiltrate.assert_awaited_once_with(page)
        channel.adorn.assert_awaited_once_with(page)

    @pytest.mark.asyncio
    async def test_navigated_within_document_rearms_without_load_wait(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page("https://example.com/app#section")
        page.wait_for_load_state = AsyncMock()
        channel.page = page
        channel._ensure_binding = AsyncMock()
        channel.exfiltrate = AsyncMock(return_value=channel)
        channel.adorn = AsyncMock(return_value=channel)

        channel._handle_cdp_event("nav:navigated_within_document", {"url": "https://example.com/app#section"})

        on_event.assert_called_once()
        if channel._pending_event_tasks:
            await asyncio.gather(*channel._pending_event_tasks)

        page.wait_for_load_state.assert_not_awaited()
        channel._ensure_binding.assert_awaited_once_with(page)
        channel.exfiltrate.assert_awaited_once_with(page)
        channel.adorn.assert_awaited_once_with(page)

    @pytest.mark.asyncio
    async def test_frame_navigated_queues_full_rearm_when_same_document_rearm_in_flight(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page("https://example.com/app")
        channel.page = page
        channel._rearm_in_flight_pages[page] = False

        channel._handle_cdp_event("nav:frame_navigated", {"frame": {"url": "https://example.com/next"}})

        on_event.assert_called_once()
        assert page in channel._rearm_pending_full_nav_pages
        assert not channel._pending_event_tasks

    @pytest.mark.asyncio
    async def test_pending_full_nav_rearm_runs_after_same_document_rearm_finishes(self) -> None:
        channel, _on_event = _make_channel()
        page = _make_page("https://example.com/next")
        page.wait_for_load_state = AsyncMock()
        channel._ensure_binding = AsyncMock()
        channel.exfiltrate = AsyncMock(return_value=channel)
        channel.adorn = AsyncMock(return_value=channel)
        channel._rearm_pending_full_nav_pages.add(page)

        await channel._rearm_page_after_navigation(
            page,
            event_name="nav:navigated_within_document",
            wait_for_load=False,
        )
        if channel._pending_event_tasks:
            await asyncio.gather(*channel._pending_event_tasks)

        page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=10_000)
        channel._ensure_binding.assert_awaited()
        assert channel._ensure_binding.await_count == 2
        assert channel.exfiltrate.await_count == 2
        assert channel.adorn.await_count == 2

    def test_non_navigation_cdp_event_does_not_rearm(self) -> None:
        channel, on_event = _make_channel()
        page = _make_page()
        browser_context = MagicMock()
        browser_context.pages = [page]
        channel.browser_context = browser_context

        channel.exfiltrate = AsyncMock(return_value=channel)
        channel.adorn = AsyncMock(return_value=channel)

        channel._handle_cdp_event("nav:frame_started_navigating", {"url": "https://example.com/next"})

        on_event.assert_called_once()
        channel.exfiltrate.assert_not_called()
        channel.adorn.assert_not_called()
