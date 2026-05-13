"""
Just an example unit test for now. Will expand later.
"""

import typing as t

from skyvern.services.browser_recording.service import Processor, summarize_exfiltrated_recording_events
from skyvern.services.browser_recording.types import (
    ExfiltratedCdpEvent,
    ExfiltratedConsoleEvent,
    ExfiltratedEventCdpParams,
)

ORG_ID = "org_123"
PBS_ID = "pbs_123"
WP_ID = "wpid_123"


def make_console_event(
    params: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    default_params = {
        "url": "https://example.com",
        "activeElement": {
            "tagName": "BUTTON",
        },
        "window": {
            "height": 800,
            "width": 1200,
            "scrollX": 0,
            "scrollY": 0,
        },
        "mousePosition": {"xp": 0.5, "yp": 0.5},
    }

    params = {**default_params, **params}

    return ExfiltratedConsoleEvent(
        kind="exfiltrated-event",
        source="console",
        event_name="user_interaction",
        params=params,
        timestamp=timestamp,
    )


def make_mouseenter_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "mouseenter",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def make_mouseleave_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "mouseleave",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def make_click_event(
    target: dict[str, t.Any],
    timestamp: float,
) -> ExfiltratedConsoleEvent:
    params: dict[str, t.Any] = {
        "type": "click",
        "target": target,
        "timestamp": timestamp,
    }

    return make_console_event(
        params=params,
        timestamp=timestamp,
    )


def test_click() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])

    event = make_click_event(
        target=target,
        timestamp=1000.0,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions([event])

    assert len(actions) == 1
    assert actions[0].kind == "click"
    assert actions[0].target.sky_id == "sky-123"


def test_hover() -> None:
    target = dict(id="button-1", skyId="sky-123", text=["Click me"])

    event1 = make_mouseenter_event(
        target=target,
        timestamp=1000.0,
    )

    event2 = make_mouseleave_event(
        target=target,
        timestamp=4000.0,
    )

    processor = Processor(PBS_ID, ORG_ID, WP_ID)
    actions = processor.events_to_actions([event1, event2])

    assert len(actions) == 1


def test_summarize_exfiltrated_recording_events_mixed() -> None:
    target = dict(id="button-1", skyId="sky-123", tagName="BUTTON", text=["Click me"])
    click = make_click_event(target=target, timestamp=1000.0)
    keypress = make_console_event(
        params={
            "type": "keypress",
            "target": target,
            "timestamp": 1001.0,
        },
        timestamp=1001.0,
    )
    cdp_nav = ExfiltratedCdpEvent(
        kind="exfiltrated-event",
        event_name="nav:frame_navigated",
        params=ExfiltratedEventCdpParams(),
        source="cdp",
        timestamp=999.0,
    )
    cdp_nav_2 = ExfiltratedCdpEvent(
        kind="exfiltrated-event",
        event_name="nav:frame_navigated",
        params=ExfiltratedEventCdpParams(),
        source="cdp",
        timestamp=1002.0,
    )

    summary = summarize_exfiltrated_recording_events([cdp_nav, click, keypress, cdp_nav_2])

    assert summary["recording_exfil_total_events"] == 4
    assert summary["recording_exfil_cdp_event_count"] == 2
    assert summary["recording_exfil_console_event_count"] == 2
    assert summary["recording_exfil_cdp_event_name_counts"] == {"nav:frame_navigated": 2}
    assert summary["recording_exfil_console_dom_type_counts"] == {"click": 1, "keypress": 1}
    assert summary["recording_exfil_console_exfil_event_name_counts"] == {"user_interaction": 2}
