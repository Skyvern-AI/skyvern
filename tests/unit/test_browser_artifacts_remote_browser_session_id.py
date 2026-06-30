"""``BrowserArtifacts.remote_browser_session_id`` is an opaque identifier for
a remote browser session. The OSS package carries only the id; downstream
creators populate it. This test pins the default (``None``) and the contract
that consumers can read/write it through normal pydantic semantics — nothing
more.
"""

from __future__ import annotations

from skyvern.webeye.browser_artifacts import BrowserArtifacts


def test_remote_browser_session_id_defaults_to_none() -> None:
    artifacts = BrowserArtifacts()
    assert artifacts.remote_browser_session_id is None


def test_remote_browser_session_id_round_trips() -> None:
    artifacts = BrowserArtifacts()
    artifacts.remote_browser_session_id = "opaque-session-handle"
    assert artifacts.remote_browser_session_id == "opaque-session-handle"
