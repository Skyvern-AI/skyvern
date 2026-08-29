import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.webeye.utils.document import get_main_document_loader_id


def page(session):
    raw = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)))
    return SimpleNamespace(page=raw)


@pytest.mark.asyncio
async def test_success_returns_id_and_detaches_once():
    session = SimpleNamespace(
        send=AsyncMock(return_value={"frameTree": {"frame": {"loaderId": "L1"}}}), detach=AsyncMock()
    )
    assert await get_main_document_loader_id(page(session)) == "L1"
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_attach_failure_returns_none():
    raw = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(side_effect=RuntimeError())))
    assert await get_main_document_loader_id(SimpleNamespace(page=raw)) is None


@pytest.mark.asyncio
async def test_send_failure_returns_none_and_detaches():
    session = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError()), detach=AsyncMock())
    assert await get_main_document_loader_id(page(session)) is None
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_cancellation_detaches_and_propagates_same_cancellation():
    cancellation = asyncio.CancelledError()
    session = SimpleNamespace(send=AsyncMock(side_effect=cancellation), detach=AsyncMock())

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await get_main_document_loader_id(page(session))

    assert exc_info.value is cancellation
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_cancellation_survives_detach_failure():
    cancellation = asyncio.CancelledError()
    session = SimpleNamespace(
        send=AsyncMock(side_effect=cancellation),
        detach=AsyncMock(side_effect=RuntimeError("detach failed")),
    )

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await get_main_document_loader_id(page(session))

    assert exc_info.value is cancellation
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_response_returns_none_and_detaches():
    session = SimpleNamespace(send=AsyncMock(return_value={"frameTree": {}}), detach=AsyncMock())
    assert await get_main_document_loader_id(page(session)) is None
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_detach_failure_returns_none():
    session = SimpleNamespace(
        send=AsyncMock(return_value={"frameTree": {"frame": {"loaderId": "L1"}}}),
        detach=AsyncMock(side_effect=RuntimeError()),
    )
    assert await get_main_document_loader_id(page(session)) is None
