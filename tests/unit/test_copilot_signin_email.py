from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from skyvern.forge.sdk.copilot.signin_email import _MAX_CREDENTIALS_TRIED, connected_gmail_address

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _credential(credential_id: str, scopes: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=credential_id, scopes_granted=GMAIL_SCOPES if scopes is None else scopes)


def _patches(credentials: list[SimpleNamespace], profile: AsyncMock | None = None) -> list:
    return [
        patch(
            "skyvern.forge.sdk.copilot.signin_email.google_oauth_service.get_credentials_for_org",
            new=AsyncMock(return_value=credentials),
        ),
        patch(
            "skyvern.forge.sdk.copilot.signin_email.google_oauth_service.scopes_for_profile",
            return_value=GMAIL_SCOPES,
        ),
        patch(
            "skyvern.forge.sdk.copilot.signin_email.google_oauth_service.load_credential_secrets",
            new=AsyncMock(return_value=SimpleNamespace(refresh_token="rt")),
        ),
        patch(
            "skyvern.forge.sdk.copilot.signin_email.google_oauth_service.access_token_from_secrets",
            new=AsyncMock(return_value="token"),
        ),
        patch(
            "skyvern.forge.sdk.copilot.signin_email.get_json",
            new=profile or AsyncMock(return_value={"emailAddress": "connected@example.com"}),
        ),
    ]


async def _resolve(credentials: list[SimpleNamespace], profile: AsyncMock | None = None) -> str | None:
    patches = _patches(credentials, profile)
    for p in patches:
        p.start()
    try:
        return await connected_gmail_address("o_test")
    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_returns_the_address_of_a_gmail_scoped_connection() -> None:
    assert await _resolve([_credential("gc_1")]) == "connected@example.com"


@pytest.mark.asyncio
async def test_skips_a_connection_without_gmail_scope() -> None:
    assert await _resolve([_credential("gc_1", scopes=["https://www.googleapis.com/auth/spreadsheets"])]) is None


@pytest.mark.asyncio
async def test_a_failing_profile_lookup_resolves_to_no_address_rather_than_raising() -> None:
    # The caller treats None as "no connected account" and falls back to the request;
    # an exception escaping here would abort the whole policy build.
    assert await _resolve([_credential("gc_1")], profile=AsyncMock(side_effect=RuntimeError("boom"))) is None


@pytest.mark.asyncio
async def test_a_hanging_lookup_gives_up_instead_of_stalling_the_turn() -> None:
    async def _never_returns(*args: object, **kwargs: object) -> dict[str, str]:
        await asyncio.sleep(3600)
        return {}

    with patch("skyvern.forge.sdk.copilot.signin_email._LOOKUP_DEADLINE_SECONDS", 0.05):
        assert await _resolve([_credential("gc_1")], profile=AsyncMock(side_effect=_never_returns)) is None


@pytest.mark.asyncio
async def test_only_a_bounded_number_of_connections_are_tried() -> None:
    profile = AsyncMock(return_value={"emailAddress": ""})
    await _resolve([_credential(f"gc_{n}") for n in range(_MAX_CREDENTIALS_TRIED + 4)], profile=profile)

    assert profile.await_count == _MAX_CREDENTIALS_TRIED


@pytest.mark.asyncio
async def test_a_gmail_connection_is_found_behind_connections_without_gmail_scope() -> None:
    sheets = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = [_credential(f"gc_sheets_{n}", scopes=sheets) for n in range(_MAX_CREDENTIALS_TRIED + 1)]
    credentials.append(_credential("gc_gmail"))

    assert await _resolve(credentials) == "connected@example.com"
