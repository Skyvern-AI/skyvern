from __future__ import annotations

from skyvern.webeye import vnc_streaming


def test_builds_routed_vnc_url_from_browser_address() -> None:
    assert (
        vnc_streaming.build_routed_vnc_url(
            "wss://browser.example.com/pbs_123/token_456/devtools/browser/browser_789",
        )
        == "wss://browser.example.com/vnc/pbs_123/token_456"
    )


def test_local_vnc_mode_supports_session_without_remote_address(monkeypatch) -> None:
    monkeypatch.setattr(vnc_streaming.settings, "BROWSER_STREAMING_MODE", "vnc")

    assert vnc_streaming.browser_session_supports_vnc_streaming(
        browser_address=None,
        ip_address=None,
    )
    assert (
        vnc_streaming.build_vnc_streaming_url(
            browser_address=None,
            ip_address=None,
            vnc_port=6080,
        )
        == "ws://127.0.0.1:6080"
    )


def test_cdp_mode_does_not_advertise_vnc_for_local_session(monkeypatch) -> None:
    monkeypatch.setattr(vnc_streaming.settings, "BROWSER_STREAMING_MODE", "cdp")

    assert not vnc_streaming.browser_session_supports_vnc_streaming(
        browser_address=None,
        ip_address=None,
    )
    assert (
        vnc_streaming.build_vnc_streaming_url(
            browser_address=None,
            ip_address=None,
            vnc_port=6080,
        )
        is None
    )


def test_ip_address_takes_precedence_over_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(vnc_streaming.settings, "BROWSER_STREAMING_MODE", "vnc")

    assert (
        vnc_streaming.build_vnc_streaming_url(
            browser_address=None,
            ip_address="10.0.0.5:9222",
            vnc_port=6080,
        )
        == "ws://10.0.0.5:6080"
    )
