"""
Just an example unit test for now. Will expand later.
"""

import typing as t

from skyvern.services.browser_recording.service import Processor
from skyvern.services.browser_recording.types import (
    ExfiltratedConsoleEvent,
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
        event_name="user-interaction",
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
