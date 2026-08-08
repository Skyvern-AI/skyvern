import asyncio
import dataclasses
from typing import get_args

import pytest

from skyvern.forge.sdk.core.http_request_authorization import (
    RedirectHopAuthorization,
    RedirectHopAuthorizer,
    RedirectHopDispatcher,
    authorize_request_hop_once,
)


def test_redirect_hop_authorization_contract_is_immutable_and_dispatch_bound() -> None:
    assert dataclasses.is_dataclass(RedirectHopAuthorization)
    assert RedirectHopAuthorization.__dataclass_params__.frozen
    assert tuple(field.name for field in dataclasses.fields(RedirectHopAuthorization)) == (
        "source_url",
        "target_url",
        "method",
    )

    callback_parameters, _callback_result = get_args(RedirectHopAuthorizer)
    assert callback_parameters[0] is RedirectHopAuthorization
    dispatcher_parameters, _dispatcher_result = get_args(callback_parameters[1])
    assert dispatcher_parameters == [tuple[str, ...]]


@pytest.mark.asyncio
async def test_authorized_redirect_hop_dispatcher_is_single_use() -> None:
    attempts: list[tuple[str, ...]] = []

    async def dispatch(resolved_values: tuple[str, ...]) -> str:
        attempts.append(resolved_values)
        return "dispatched"

    async def authorize(
        _authorization: RedirectHopAuthorization,
        guarded_dispatch: RedirectHopDispatcher[str],
    ) -> str:
        result = await guarded_dispatch(("first",))
        with pytest.raises(RuntimeError, match="only be invoked once"):
            await guarded_dispatch(("second",))
        return result

    result = await authorize_request_hop_once(
        authorize,
        RedirectHopAuthorization(None, "https://example.com", "POST"),
        dispatch,
    )

    assert result == "dispatched"
    assert attempts == [("first",)]


@pytest.mark.asyncio
async def test_authorized_redirect_hop_dispatcher_cannot_escape_callback_scope() -> None:
    retained: list[RedirectHopDispatcher[str]] = []
    attempts: list[tuple[str, ...]] = []

    async def dispatch(resolved_values: tuple[str, ...]) -> str:
        attempts.append(resolved_values)
        return "dispatched"

    async def authorize(
        _authorization: RedirectHopAuthorization,
        guarded_dispatch: RedirectHopDispatcher[str],
    ) -> str:
        retained.append(guarded_dispatch)
        return "not-dispatched"

    result = await authorize_request_hop_once(
        authorize,
        RedirectHopAuthorization(None, "https://example.com", "GET"),
        dispatch,
    )

    assert result == "not-dispatched"
    with pytest.raises(RuntimeError, match="no longer active"):
        await retained[0](())
    assert attempts == []


@pytest.mark.asyncio
async def test_authorized_redirect_hop_dispatcher_rejects_background_tasks() -> None:
    attempts: list[tuple[str, ...]] = []

    async def dispatch(resolved_values: tuple[str, ...]) -> str:
        attempts.append(resolved_values)
        return "dispatched"

    async def authorize(
        _authorization: RedirectHopAuthorization,
        guarded_dispatch: RedirectHopDispatcher[str],
    ) -> str:
        with pytest.raises(RuntimeError, match="authorizing task"):
            await asyncio.create_task(guarded_dispatch(()))
        return "blocked"

    result = await authorize_request_hop_once(
        authorize,
        RedirectHopAuthorization(None, "https://example.com", "GET"),
        dispatch,
    )

    assert result == "blocked"
    assert attempts == []
