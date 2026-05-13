from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEventSource, ExfiltrationChannel


def make_channel(on_event: MagicMock | None = None) -> tuple[ExfiltrationChannel, MagicMock]:
    vnc_channel = MagicMock()
    vnc_channel.identity = {"client_id": "client-1"}
    vnc_channel.x_api_key = "api-key"

    event_callback = on_event or MagicMock()
    return ExfiltrationChannel(on_event=event_callback, vnc_channel=vnc_channel), event_callback


def make_page(url: str = "https://example.com") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.context = MagicMock()
    page.on = MagicMock()
    page.remove_listener = MagicMock()
    page.add_init_script = AsyncMock()
    page.evaluate = AsyncMock()
    return page


def make_event_data() -> dict[str, object]:
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
        "mousePosition": {
            "xp": 0.5,
            "yp": 0.25,
        },
        "activeElement": {
            "tagName": "BUTTON",
        },
        "window": {
            "width": 1280,
            "height": 720,
            "scrollX": 0,
            "scrollY": 0,
        },
    }


class TestExfiltrationChannel:
    @pytest.mark.asyncio
    async def test_runtime_console_api_called_emits_console_event(self) -> None:
        channel, on_event = make_channel()
        event_data = make_event_data()

        await channel._handle_runtime_console_event_async(
            {
                "args": [
                    {"type": "string", "value": "[EXFIL]"},
                    {"type": "string", "value": json.dumps(event_data)},
                ]
            }
        )

        on_event.assert_called_once()
        emitted = on_event.call_args.args[0]
        assert len(emitted) == 1
        assert emitted[0].source == ExfiltratedEventSource.CONSOLE
        assert emitted[0].event_name == "user_interaction"
        assert emitted[0].params == event_data

    @pytest.mark.asyncio
    async def test_duplicate_playwright_and_runtime_console_events_emit_once(self) -> None:
        channel, on_event = make_channel()
        event_data = make_event_data()

        marker = MagicMock()
        marker.json_value = AsyncMock(return_value="[EXFIL]")
        payload = MagicMock()
        payload.json_value = AsyncMock(return_value=json.dumps(event_data))

        message = MagicMock()
        message.args = [marker, payload]
        message.text = f"[EXFIL] {json.dumps(event_data)}"

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
    async def test_exfiltrate_injects_scripts_and_registers_runtime_listener(self) -> None:
        channel, _ = make_channel()
        page = make_page()
        cdp_session = MagicMock()
        cdp_session.send = AsyncMock()
        cdp_session.on = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp_session)

        result = await channel.exfiltrate(page)

        assert result is channel
        page.on.assert_called_once()
        page.add_init_script.assert_awaited_once_with(channel.js("exfiltrate"))
        page.evaluate.assert_awaited_once_with(channel.js("exfiltrate"))
        cdp_session.send.assert_awaited_once_with("Runtime.enable")
        event_names = [call.args[0] for call in cdp_session.on.call_args_list]
        assert event_names == ["Runtime.consoleAPICalled"]

    @pytest.mark.asyncio
    async def test_exfiltrate_keeps_playwright_fallback_when_cdp_setup_fails(self) -> None:
        channel, _ = make_channel()
        page = make_page()
        page.context.new_cdp_session = AsyncMock(side_effect=RuntimeError("boom"))

        result = await channel.exfiltrate(page)

        assert result is channel
        page.on.assert_called_once()
        page.add_init_script.assert_awaited_once_with(channel.js("exfiltrate"))
        page.evaluate.assert_awaited_once_with(channel.js("exfiltrate"))
        assert channel._page_console_captures[id(page)].cdp_session is None

    @pytest.mark.asyncio
    async def test_playwright_console_falls_back_to_text_payload(self) -> None:
        channel, on_event = make_channel()
        event_data = make_event_data()

        broken_arg = MagicMock()
        broken_arg.json_value = AsyncMock(side_effect=RuntimeError("cannot serialize"))

        message = MagicMock()
        message.args = [broken_arg]
        message.text = f"[EXFIL] {json.dumps(event_data)}"

        await channel._handle_console_event_async(message)

        on_event.assert_called_once()
        emitted = on_event.call_args.args[0]
        assert emitted[0].params == event_data
