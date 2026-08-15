import asyncio
import dataclasses
from typing import get_args
from unittest.mock import AsyncMock

import pytest

from skyvern.forge.sdk.core.http_request_authorization import (
    RedirectHopAuthorization,
    RedirectHopAuthorizer,
    RedirectHopDispatcher,
    RunScopedRedirectHopAuthorizer,
    authorize_request_hop_once,
    deny_unenrolled_redirect_hop,
)


def test_redirect_hop_authorization_contract_is_immutable_and_dispatch_bound() -> None:
    assert dataclasses.is_dataclass(RedirectHopAuthorization)
    assert RedirectHopAuthorization.__dataclass_params__.frozen
    assert tuple(field.name for field in dataclasses.fields(RedirectHopAuthorization)) == (
        "source_url",
        "target_url",
        "method",
        "download_scope",
        "initial_url",
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


def test_run_scoped_authorizer_requires_nonempty_immutable_scope() -> None:
    with pytest.raises(ValueError, match="nonempty download scope"):
        RunScopedRedirectHopAuthorizer("")

    authorizer = RunScopedRedirectHopAuthorizer("wr_1")

    assert dataclasses.is_dataclass(authorizer)
    assert authorizer.download_scope == "wr_1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        authorizer.download_scope = "wr_2"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_run_scoped_authorizer_dispatches_get_once() -> None:
    authorizer = RunScopedRedirectHopAuthorizer("wr_1")
    attempts: list[tuple[str, ...]] = []

    async def dispatch(resolved_values: tuple[str, ...]) -> str:
        attempts.append(resolved_values)
        return "dispatched"

    result = await authorize_request_hop_once(
        authorizer,
        RedirectHopAuthorization(
            None,
            "https://example.com/report.pdf",
            "GET",
            download_scope="wr_1",
            initial_url="https://example.com/report.pdf",
        ),
        dispatch,
    )

    assert result == "dispatched"
    assert attempts == [()]


@pytest.mark.asyncio
async def test_run_scoped_authorizer_allows_a_prevalidated_redirect_target() -> None:
    authorizer = RunScopedRedirectHopAuthorizer("wr_1")
    dispatch = AsyncMock(return_value="dispatched")

    result = await authorize_request_hop_once(
        authorizer,
        RedirectHopAuthorization(
            "https://example.com/report.pdf",
            "https://downloads.example-cdn.com/signed-report.pdf",
            "GET",
            download_scope="wr_1",
            initial_url="https://example.com/report.pdf",
        ),
        dispatch,
    )

    assert result == "dispatched"
    dispatch.assert_awaited_once_with(())


@pytest.mark.asyncio
async def test_run_scoped_authorizer_rejects_non_get_without_dispatch() -> None:
    authorizer = RunScopedRedirectHopAuthorizer("wr_1")
    dispatch = AsyncMock(return_value="dispatched")

    with pytest.raises(PermissionError, match="GET requests"):
        await authorize_request_hop_once(
            authorizer,
            RedirectHopAuthorization(
                None,
                "https://example.com/report.pdf",
                "POST",
                download_scope="wr_1",
                initial_url="https://example.com/report.pdf",
            ),
            dispatch,
        )

    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("download_scope", "initial_url"),
    [
        pytest.param("wr_stale", "https://example.com/report.pdf", id="stale-scope"),
        pytest.param("wr_1", "https://example.com/other.pdf", id="wrong-event-url"),
        pytest.param(None, None, id="unbound"),
    ],
)
async def test_run_scoped_authorizer_rejects_unbound_or_mismatched_download(
    download_scope: str | None,
    initial_url: str | None,
) -> None:
    authorizer = RunScopedRedirectHopAuthorizer("wr_1")
    dispatch = AsyncMock(return_value="dispatched")

    with pytest.raises(PermissionError, match="run-scoped browser download"):
        await authorize_request_hop_once(
            authorizer,
            RedirectHopAuthorization(
                None,
                "https://example.com/report.pdf",
                "GET",
                download_scope=download_scope,
                initial_url=initial_url,
            ),
            dispatch,
        )

    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unenrolled_authorizer_still_fails_closed_without_dispatch() -> None:
    dispatch = AsyncMock(return_value="dispatched")

    with pytest.raises(RuntimeError, match="not enrolled"):
        await authorize_request_hop_once(
            deny_unenrolled_redirect_hop,
            RedirectHopAuthorization(None, "https://example.com/report.pdf", "GET"),
            dispatch,
        )

    dispatch.assert_not_awaited()
