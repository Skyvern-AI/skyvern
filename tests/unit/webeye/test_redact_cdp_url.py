"""What of a CDP address may be written down (SKY-13287).

A connect URL carries a credential in five places: the query (a session token, a vendor api
key), userinfo, the segment after a path credential marker, the legacy CDP routing token in the
second path segment, and the legacy live-view token trailing ``/vnc/{session_id}``. A session
token alone is enough to drive that session's browser, so a log line holding one hands a live
browser to anyone who can read logs. What survives redaction is what a debugger needs and an
attacker cannot use: scheme, host, port, parameter names, and the session id.
"""

from __future__ import annotations

import pytest

from skyvern.webeye.cdp_connection import REDACTED, redact_cdp_url
from skyvern.webeye.cdp_credentials import CREDENTIAL_PATH_MARKERS

SESSION_ID = "pbs_live"
TOKEN = f"{SESSION_ID}.minted-secret"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            f"wss://session-router.skyvern.com/{SESSION_ID}?token={TOKEN}",
            f"wss://session-router.skyvern.com/{SESSION_ID}?token={REDACTED}",
            id="router_address",
        ),
        pytest.param(
            f"wss://session-router.skyvern.com/vnc/{SESSION_ID}?token={TOKEN}",
            f"wss://session-router.skyvern.com/vnc/{SESSION_ID}?token={REDACTED}",
            id="live_view_address",
        ),
        pytest.param(
            f"wss://sessions.skyvern.com/{SESSION_ID}/payload.signature/devtools/browser/b-1",
            f"wss://sessions.skyvern.com/{SESSION_ID}/{REDACTED}/devtools/browser/b-1",
            id="legacy_routing_token",
        ),
        pytest.param(
            f"wss://sessions.skyvern.com/vnc/{SESSION_ID}/{TOKEN}",
            f"wss://sessions.skyvern.com/vnc/{SESSION_ID}/{REDACTED}",
            id="legacy_live_view_token",
        ),
        pytest.param(
            f"wss://session-router.skyvern.com/token/{TOKEN}/{SESSION_ID}",
            f"wss://session-router.skyvern.com/token/{REDACTED}/{SESSION_ID}",
            id="marked_path_credential",
        ),
        pytest.param(
            "wss://connect.browserbase.com?apiKey=bb-secret&sessionId=abc",
            f"wss://connect.browserbase.com?apiKey={REDACTED}&sessionId={REDACTED}",
            id="vendor_api_key",
        ),
        pytest.param(
            "wss://token-as-userinfo@connect.vendor.example/cdp",
            f"wss://{REDACTED}@connect.vendor.example/cdp",
            id="userinfo",
        ),
        pytest.param(
            "ws://10.0.0.7:9223/devtools/browser/b-1",
            "ws://10.0.0.7:9223/devtools/browser/b-1",
            id="no_credential_survives_intact",
        ),
        pytest.param("", "", id="empty"),
        pytest.param(None, "", id="missing"),
    ],
)
def test_redaction_masks_credentials_and_keeps_the_rest(url: str | None, expected: str) -> None:
    assert redact_cdp_url(url) == expected


@pytest.mark.parametrize("marker", CREDENTIAL_PATH_MARKERS)
def test_every_path_credential_marker_masks_the_segment_it_marks(marker: str) -> None:
    """The router accepts a credential after any of these markers, so a redactor that knew only
    some of them would write a live token for the rest."""
    redacted = redact_cdp_url(f"wss://session-router.skyvern.com/{marker}/{TOKEN}/{SESSION_ID}")

    assert redacted == f"wss://session-router.skyvern.com/{marker}/{REDACTED}/{SESSION_ID}"
    assert TOKEN not in redacted


def test_the_token_value_never_survives_in_any_form() -> None:
    """Asserted on the value rather than the shape: a future change that merely reorders or
    re-encodes the query must still not leave the secret in the string."""
    redacted = redact_cdp_url(f"wss://session-router.skyvern.com/{SESSION_ID}?token={TOKEN}&region=eu")

    assert TOKEN not in redacted
    assert "minted-secret" not in redacted
    # Still useful: the session it names and the parameters it carried survive.
    assert SESSION_ID in redacted
    assert redacted.count(REDACTED) == 2
    assert "region" in redacted
