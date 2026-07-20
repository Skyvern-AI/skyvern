from __future__ import annotations

import pytest

from skyvern.proxy.core.session import Principal, ProxySession


@pytest.mark.parametrize(
    ("session_id", "upstream_ws_url"),
    [("", "ws://localhost:1"), ("session", ""), ("  ", "ws://localhost:1")],
)
def test_proxy_session_rejects_empty_routing_fields(session_id: str, upstream_ws_url: str) -> None:
    with pytest.raises(ValueError):
        ProxySession(session_id=session_id, upstream_ws_url=upstream_ws_url)


def test_proxy_session_tracks_flat_cdp_sessions() -> None:
    session = ProxySession(session_id="session", upstream_ws_url="ws://localhost:1")

    assert session.attach_cdp_session("page-session") is True
    assert session.attach_cdp_session("page-session") is False
    assert session.has_cdp_session("page-session") is True
    assert session.detach_cdp_session("page-session") is True
    assert session.detach_cdp_session("page-session") is False
    assert session.has_cdp_session("page-session") is False


def test_proxy_session_rejects_empty_cdp_session_ids() -> None:
    session = ProxySession(session_id="session", upstream_ws_url="ws://localhost:1")

    with pytest.raises(ValueError):
        session.attach_cdp_session("")
    with pytest.raises(ValueError):
        session.has_cdp_session(" ")
    with pytest.raises(ValueError):
        session.detach_cdp_session("")


def test_proxy_session_access_rule_is_principal_based() -> None:
    owner = Principal(principal_id="owner", organization_id="organization")
    session = ProxySession(session_id="session", upstream_ws_url="ws://localhost:1", principal=owner)

    assert session.allows_principal(owner)
    assert not session.allows_principal(Principal(principal_id="other", organization_id="organization"))
    assert ProxySession(session_id="open", upstream_ws_url="ws://localhost:1").allows_principal(
        Principal(principal_id="anyone")
    )


def test_principal_rejects_empty_identity() -> None:
    with pytest.raises(ValueError):
        Principal(principal_id="")


def test_proxy_session_repr_never_leaks_url_or_headers() -> None:
    session = ProxySession(
        session_id="s1",
        upstream_ws_url="ws://upstream.internal:9222/devtools/browser/abc?token=secret-token",
        connect_headers={"authorization": "Bearer secret-header"},
    )
    assert "secret-token" not in repr(session)
    assert session.upstream_ws_url not in repr(session)
    assert "secret-header" not in repr(session)
