import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.webeye.utils import document
from skyvern.webeye.utils.document import get_main_document_loader_id


def page(session):
    raw = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)))
    return SimpleNamespace(page=raw)


def never_returns(entered: asyncio.Event):
    async def call(*_args):
        entered.set()
        await asyncio.Event().wait()

    return call


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
async def test_unanswered_frame_tree_returns_none_and_detaches(monkeypatch):
    monkeypatch.setattr(document, "LOADER_ID_FRAME_TREE_TIMEOUT_SECONDS", 0.05)
    session = SimpleNamespace(send=never_returns(asyncio.Event()), detach=AsyncMock())

    assert await asyncio.wait_for(get_main_document_loader_id(page(session)), timeout=5) is None
    session.detach.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_during_unanswered_frame_tree_is_not_held_by_an_unanswered_detach(monkeypatch):
    monkeypatch.setattr(document, "LOADER_ID_DETACH_TIMEOUT_SECONDS", 0.05)
    in_send = asyncio.Event()
    session = SimpleNamespace(send=never_returns(in_send), detach=never_returns(asyncio.Event()))
    read = asyncio.ensure_future(get_main_document_loader_id(page(session)))
    await asyncio.wait_for(in_send.wait(), timeout=5)

    read.cancel()
    done, _ = await asyncio.wait({read}, timeout=5)

    try:
        assert read in done and read.cancelled()
    finally:
        read.cancel()


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
