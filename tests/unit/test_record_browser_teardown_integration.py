"""Integration coverage for Record Browser teardown when the browser target closes.

Reproduces SKY-12366 across the components that ``message.py`` wires together on
``END_EXFILTRATION``: the exfiltration channel is stopped (message.py:727) and then
the live interpretation session is flushed (message.py:731). When the page target has
already closed (take-control swaps, navigations, bot-detection pages), the channel's
``undecorate`` used to raise ``TargetClosedError`` out of ``stop()`` — so the flush
never ran, the accumulated drafts were lost, and the recording produced no blocks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright._impl._errors import TargetClosedError

from skyvern.forge import app
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import ExfiltratedEvent as StreamingExfiltratedEvent
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltratedEventSource as StreamingExfiltratedEventSource,
)
from skyvern.forge.sdk.routes.streaming.channels.exfiltration import (
    ExfiltrationChannel,
)
from skyvern.services.browser_recording.service import Processor
from skyvern.services.browser_recording.session_registry import RecordingInterpretationSessionRegistry

ORG_ID = "org_123"
PBS_ID = "pbs_123"
WP_ID = "wpid_123"


def _click_event(*, capture_seq: int, sky_id: str, target_id: str) -> StreamingExfiltratedEvent:
    return StreamingExfiltratedEvent(
        event_name="user_interaction",
        source=StreamingExfiltratedEventSource.CONSOLE,
        timestamp=1000.0 + capture_seq,
        capture_seq=capture_seq,
        params={
            "type": "click",
            "url": "https://example.com",
            "timestamp": 1000.0 + capture_seq,
            "target": {"tagName": "BUTTON", "id": target_id, "text": ["Submit"], "skyId": sky_id},
            "mousePosition": {"xp": 0.5, "yp": 0.5},
            "activeElement": {"tagName": "BUTTON"},
            "window": {"height": 800, "width": 1200, "scrollX": 0, "scrollY": 0},
        },
    )


def _vnc_channel() -> MagicMock:
    vnc_channel = MagicMock()
    vnc_channel.browser_session = MagicMock(
        browser_address="http://localhost:9222",
        persistent_browser_session_id=PBS_ID,
    )
    vnc_channel.identity = {"client_id": "client-1", "browser_session_id": PBS_ID}
    return vnc_channel


def _channel_with_closed_page() -> ExfiltrationChannel:
    """An exfiltration channel whose only page's target is already closed."""
    channel = ExfiltrationChannel(on_event=lambda _messages: None, vnc_channel=_vnc_channel())

    closed_page = MagicMock()
    closed_page.url = "https://example.com"
    closed_page.remove_listener = MagicMock()
    closed_page.evaluate = AsyncMock()
    closed_page.add_init_script = AsyncMock(
        side_effect=TargetClosedError("Page.add_init_script: Target page, context or browser has been closed")
    )

    browser_context = MagicMock()
    browser_context.pages = [closed_page]
    channel.browser_context = browser_context
    return channel


@pytest.mark.asyncio
async def test_end_exfiltration_on_closed_target_preserves_drafts_into_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_llm(*args: object, **kwargs: object) -> dict[str, object]:
        return {"block_label": "click_submit", "title": "Click Submit", "prompt": "Click the submit button."}

    monkeypatch.setattr(app, "LLM_API_HANDLER", fake_llm)

    # A live recording that has accumulated drafts (what the user sees in the panel).
    registry = RecordingInterpretationSessionRegistry()
    registry.start_session(
        browser_session_id=PBS_ID,
        organization_id=ORG_ID,
        workflow_permanent_id=WP_ID,
        on_update=lambda _update: None,
        recording_attempt_id="attempt-1",
    )
    session = registry._sessions[PBS_ID]
    registry.ingest_events(
        PBS_ID,
        [
            _click_event(capture_seq=0, sky_id="sky-a", target_id="a"),
            _click_event(capture_seq=1, sky_id="sky-b", target_id="b"),
        ],
    )
    await session._interpret(finalized=False)
    assert session.steps, "precondition: the recording accumulated drafts"

    # Replay the END_EXFILTRATION handler sequence against a closed browser target.
    channel = _channel_with_closed_page()
    await channel.stop()  # message.py:727 — must not raise on a closed target
    drafts = await registry.stop_session(PBS_ID)  # message.py:731 — only reached if stop() didn't crash

    # The drafts survived teardown and convert into real workflow blocks.
    assert drafts, "teardown dropped the recorded drafts"
    blocks = Processor(PBS_ID, ORG_ID, WP_ID).drafts_to_blocks(drafts)
    assert blocks, "surviving drafts did not produce workflow blocks"
