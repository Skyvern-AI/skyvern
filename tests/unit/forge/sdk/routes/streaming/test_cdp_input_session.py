"""A transient page-resolution failure must not drop an already-bound CDP session: the dispatch loop
treats None as "no active page" and silently skips the event while the channel stays open, so the
user keeps interacting with a surface that no longer receives input."""

from typing import Any

import pytest

from skyvern.forge.sdk.routes.streaming import cdp_input


class _FakeSession:
    def __init__(self, name: str) -> None:
        self.name = name
        self.detached = False

    async def detach(self) -> None:
        self.detached = True


class _FakeContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def new_cdp_session(self, page: object) -> _FakeSession:
        return self._session


class _FakePage:
    def __init__(self, session: _FakeSession, url: str = "https://example.test/") -> None:
        self.context = _FakeContext(session)
        self.url = url


def _build(monkeypatch: pytest.MonkeyPatch, page: Any) -> tuple[cdp_input.ActivePageCdpInputSession, list[Any]]:
    resolved: list[Any] = [page]

    async def _resolve(*args: Any, **kwargs: Any) -> Any:
        return resolved[0]

    monkeypatch.setattr(cdp_input, "_resolve_working_page", _resolve)
    input_session = cdp_input.ActivePageCdpInputSession(
        browser_state=object(),  # type: ignore[arg-type]
        entity_id="wr_test",
        entity_type="workflow_run",
    )
    return input_session, resolved


@pytest.mark.asyncio
async def test_bound_session_survives_a_transient_resolution_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _FakeSession("first")
    input_session, resolved = _build(monkeypatch, _FakePage(session))

    assert await input_session.get_session() is session

    resolved[0] = None

    assert await input_session.get_session(force_refresh=True) is session
    assert input_session.page_resolution_failed is False
    assert session.detached is False


@pytest.mark.asyncio
async def test_resolution_failure_before_any_bind_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    input_session, _ = _build(monkeypatch, None)

    assert await input_session.get_session() is None
    assert input_session.page_resolution_failed is True


@pytest.mark.asyncio
async def test_rebinds_when_the_working_page_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = _FakeSession("first"), _FakeSession("second")
    input_session, resolved = _build(monkeypatch, _FakePage(first))

    assert await input_session.get_session() is first

    resolved[0] = _FakePage(second)

    assert await input_session.get_session(force_refresh=True) is second
    assert first.detached is True
