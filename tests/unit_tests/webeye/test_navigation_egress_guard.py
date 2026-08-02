from __future__ import annotations

import asyncio
from typing import Any

import pytest

from skyvern.webeye.navigation_egress_guard import (
    NavigationEgressGuard,
    install_navigation_egress_guard,
)


class _FakeCDPSession:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.listeners: dict[str, Any] = {}
        self._fail_on = fail_on

    def on(self, event: str, handler: Any) -> None:
        self.listeners[event] = handler

    async def send(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._fail_on == method:
            raise RuntimeError("cdp session detached")
        self.sent.append((method, params or {}))

    def verdicts(self) -> list[tuple[str, str]]:
        return [
            (method, params.get("requestId", ""))
            for method, params in self.sent
            if method.startswith("Fetch.") and method != "Fetch.enable"
        ]


class _FakeContext:
    def __init__(self, session: _FakeCDPSession | None, *, raises: bool = False) -> None:
        self._session = session
        self._raises = raises

    async def new_cdp_session(self, page: Any) -> _FakeCDPSession:
        if self._raises or self._session is None:
            raise RuntimeError("cdp sessions unsupported")
        return self._session


class _FakePage:
    def __init__(self, session: _FakeCDPSession | None, *, raises: bool = False) -> None:
        self.context = _FakeContext(session, raises=raises)


def _paused(url: str, request_id: str = "req-1") -> dict[str, Any]:
    return {"requestId": request_id, "resourceType": "Document", "request": {"url": url}}


async def _decide(session: _FakeCDPSession, event: dict[str, Any]) -> None:
    guard = NavigationEgressGuard(session)
    guard.on_request_paused(event)
    # on_request_paused schedules the verdict; let the scheduled task run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


BLOCKED_HOPS = [
    pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud-metadata-ip"),
    pytest.param("http://127.0.0.1:9000/", id="loopback-ip"),
    pytest.param("http://10.0.0.5/", id="private-10"),
    pytest.param("http://192.168.0.10/admin", id="private-192"),
    pytest.param("http://localhost:8000/admin", id="localhost"),
    pytest.param("http://metadata.google.internal/computeMetadata/v1/", id="metadata-hostname"),
    pytest.param("http://kubernetes.default.svc/api", id="cluster-internal-svc"),
    pytest.param("http://2130706433/", id="decimal-ip-loopback"),
    pytest.param("http://0xa9fea9fe/", id="hex-ip-metadata"),
    pytest.param("file:///etc/passwd", id="local-file"),
]


@pytest.mark.parametrize("url", BLOCKED_HOPS)
@pytest.mark.asyncio
async def test_internal_hop_is_failed_before_the_browser_connects(url: str) -> None:
    session = _FakeCDPSession()
    await _decide(session, _paused(url))
    assert session.verdicts() == [("Fetch.failRequest", "req-1")]
    assert session.sent[0][1]["errorReason"] == "AddressUnreachable"


@pytest.mark.parametrize("url", ["https://example.com/", "http://93.184.216.34/", "https://sub.example.co.uk/a?b=c"])
@pytest.mark.asyncio
async def test_public_hop_continues(url: str) -> None:
    session = _FakeCDPSession()
    await _decide(session, _paused(url))
    assert session.verdicts() == [("Fetch.continueRequest", "req-1")]


@pytest.mark.asyncio
async def test_unexpected_classifier_failure_blocks_rather_than_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(url: str) -> None:
        raise ValueError("classifier blew up")

    monkeypatch.setattr("skyvern.webeye.navigation_egress_guard.validate_navigation_destination", explode)
    session = _FakeCDPSession()
    await _decide(session, _paused("https://example.com/"))
    # A paused hop with no verdict hangs the navigation, so the only safe answer is to block.
    assert session.verdicts() == [("Fetch.failRequest", "req-1")]


@pytest.mark.asyncio
async def test_detached_session_does_not_raise_out_of_the_handler() -> None:
    session = _FakeCDPSession(fail_on="Fetch.failRequest")
    await _decide(session, _paused("http://169.254.169.254/"))
    assert session.verdicts() == []


@pytest.mark.asyncio
async def test_install_enables_document_stage_interception() -> None:
    session = _FakeCDPSession()
    page = _FakePage(session)
    await install_navigation_egress_guard(page)

    assert "Fetch.requestPaused" in session.listeners
    patterns = dict(session.sent)["Fetch.enable"]["patterns"]
    # Request stage is what makes the pause happen *before* the hop leaves the browser, and
    # Document scoping is what keeps subresources out of the guard. Both are load-bearing.
    assert patterns == [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]


@pytest.mark.asyncio
async def test_install_is_idempotent_per_page() -> None:
    session = _FakeCDPSession()
    page = _FakePage(session)
    await install_navigation_egress_guard(page)
    await install_navigation_egress_guard(page)
    assert [method for method, _ in session.sent].count("Fetch.enable") == 1


@pytest.mark.asyncio
async def test_install_failure_leaves_navigation_usable() -> None:
    page = _FakePage(None, raises=True)
    await install_navigation_egress_guard(page)  # must not raise


class _FakeBrowserContext:
    """Duck-types the slice of BrowserContext that context-level arming uses."""

    def __init__(self, pages: list[Any] | None = None) -> None:
        self.pages = pages or []
        self._handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self._handlers[event] = handler

    def emit_page(self, page: Any) -> None:
        self._handlers["page"](page)


@pytest.mark.asyncio
async def test_arming_the_context_covers_pages_that_never_call_a_navigation_helper() -> None:
    """Popups, target=_blank, and generated scripts never reach a navigation helper.

    Binding the guard only to a call site leaves every one of them unguarded.
    """
    from skyvern.webeye.navigation_egress_guard import arm_navigation_egress_guard

    existing_session = _FakeCDPSession()
    popup_session = _FakeCDPSession()
    context = _FakeBrowserContext(pages=[_FakePage(existing_session)])

    arm_navigation_egress_guard(context)
    context.emit_page(_FakePage(popup_session))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert "Fetch.enable" in dict(existing_session.sent), "page open at arm time was left unguarded"
    assert "Fetch.enable" in dict(popup_session.sent), "page opened by the site was left unguarded"


@pytest.mark.asyncio
async def test_arming_never_breaks_browser_context_creation() -> None:
    """Arming runs inside create_browser_context, where a raise fails the whole browser launch.

    A guard layered on top of two existing validators must degrade, never take the browser down.
    """
    from skyvern.webeye.navigation_egress_guard import arm_navigation_egress_guard

    class _ContextWithoutEvents:
        pages: list[Any] = []

    arm_navigation_egress_guard(_ContextWithoutEvents())  # must not raise
