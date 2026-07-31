"""Where a credential can hide inside a CDP or live-view address.

The owning definition of a grammar two components must agree on: the router's ``_parse_request``
(cloud/cdp_proxy/adapters/websocket_server.py) pulls a client's credential *out* of an incoming
address, and ``redact_cdp_url`` (skyvern/webeye/cdp_connection.py) masks every position that
parser would read. A position one accepts and the other misses writes a live session token to
the logs.

The proxy adapter cannot import this module — `.importlinter`'s proxy-runner-isolation contract
forbids that package from importing `skyvern` at all, and `skyvern` may not import `cloud` — so
it restates the two tuples locally. That copy is not maintained by hand: the contract tests in
tests/cloud/cdp_proxy/test_websocket_server.py pin it equal to this one *and* assert the
behavior end to end, so a change here that is not mirrored there fails CI rather than quietly
unmasking a token.
"""

from __future__ import annotations

from collections.abc import Sequence

# Query parameter names a client may use to carry its credential when it cannot set a WS
# header (e.g. puppeteer).
CREDENTIAL_QUERY_PARAMS = ("x-api-key", "api-key", "api_key", "apikey", "token", "access_token")

# Path markers: the segment immediately after any of these is the credential, letting a
# header-less client dial /<marker>/<secret>/<session_id>.
CREDENTIAL_PATH_MARKERS = ("apikey", "api-key", "api_key", "key", "token")

# Live view rides the router under this path segment: /vnc/<session_id> with the token in the
# query, or — at the legacy nginx edge — /vnc/<session_id>/<token>.
LIVE_VIEW_PATH_SEGMENT = "vnc"
LIVE_VIEW_PATH_PREFIX = f"/{LIVE_VIEW_PATH_SEGMENT}/"


def marked_credential_segment(segments: Sequence[str]) -> int | None:
    """Index of the credential in a path split into non-empty segments, or None.

    First marker wins, matching the parser: a later marker's segment is never read as the
    credential, so a redactor honoring this answer masks exactly what the parser would have
    consumed — no more, and no less.
    """
    for index in range(len(segments) - 1):
        if segments[index].lower() in CREDENTIAL_PATH_MARKERS:
            return index + 1
    return None
