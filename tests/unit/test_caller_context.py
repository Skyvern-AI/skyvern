"""Tests for the get_current_caller_context auth dependency.

Focused on the dispatch contracts:
  - x-api-key takes precedence over Authorization (matches get_current_org).
  - JWT path requires BOTH auth callbacks to be configured.
  - x-user-agent: skyvern-ui flips API key callers to CallerType.USER.
  - 403 (not 401) on auth failure.

The underlying helpers (authenticate_helper, authenticate_user_helper,
get_current_org_cached) are stubbed via monkeypatch so these tests only
exercise the dispatch logic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from skyvern.forge.sdk.schemas.organizations import Organization
from skyvern.forge.sdk.services import org_auth_service as caller_context_module
from skyvern.forge.sdk.services.org_auth_service import (
    CallerContext,
    get_current_caller_context,
)
from skyvern.forge.sdk.workflow.models.tags import CallerType


def _org(org_id: str = "o_test", name: str = "Test Org") -> Organization:
    now = datetime.now(timezone.utc)
    return Organization(
        organization_id=org_id,
        organization_name=name,
        webhook_callback_url=None,
        max_steps_per_run=None,
        max_retries_per_step=None,
        domain=None,
        created_at=now,
        modified_at=now,
    )


async def _async_return(value):
    return value


@pytest.fixture
def patched_app(monkeypatch: pytest.MonkeyPatch):
    """Stub app.authentication_function / authenticate_user_function /
    get_current_org_cached / authenticate_helper / authenticate_user_helper.
    """
    from skyvern.forge import app

    # AppHolder proxies setattr to its inner instance but doesn't support
    # delattr cleanly, so use save/restore instead of monkeypatch for the
    # auth-callback attributes on app itself.
    prior_auth = getattr(app, "authentication_function", None) if hasattr(app, "authentication_function") else None
    prior_user_auth = (
        getattr(app, "authenticate_user_function", None) if hasattr(app, "authenticate_user_function") else None
    )
    app.authentication_function = lambda token: _async_return(_org())
    app.authenticate_user_function = lambda token: _async_return("user_42")

    async def fakeget_current_org_cached(x_api_key: str, db) -> Organization:
        return _org()

    async def fakeauthenticate_helper(authorization: str) -> Organization:
        return _org()

    async def fakeauthenticate_user_helper(authorization: str) -> str:
        return "user_42"

    monkeypatch.setattr(
        caller_context_module,
        "get_current_org_cached",
        fakeget_current_org_cached,
    )
    monkeypatch.setattr(
        caller_context_module,
        "authenticate_helper",
        fakeauthenticate_helper,
    )
    monkeypatch.setattr(
        caller_context_module,
        "authenticate_user_helper",
        fakeauthenticate_user_helper,
    )

    yield
    app.authentication_function = prior_auth
    app.authenticate_user_function = prior_user_auth


@pytest.mark.asyncio
async def test_no_headers_raises_403(patched_app) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_current_caller_context(x_api_key=None, authorization=None, x_user_agent=None)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_api_key_only_returns_api_key_caller(patched_app) -> None:
    ctx = await get_current_caller_context(x_api_key="key", authorization=None, x_user_agent=None)
    assert isinstance(ctx, CallerContext)
    assert ctx.caller_type == CallerType.API_KEY
    assert ctx.caller_id == "o_test"
    assert ctx.organization.organization_id == "o_test"


@pytest.mark.asyncio
async def test_api_key_plus_skyvern_ui_returns_user_caller(patched_app) -> None:
    ctx = await get_current_caller_context(
        x_api_key="key",
        authorization=None,
        x_user_agent="skyvern-ui",
    )
    assert ctx.caller_type == CallerType.USER
    assert ctx.caller_id == "o_test_user"


@pytest.mark.asyncio
async def test_jwt_only_returns_user_caller(patched_app) -> None:
    ctx = await get_current_caller_context(
        x_api_key=None,
        authorization="Bearer abc",
        x_user_agent=None,
    )
    assert ctx.caller_type == CallerType.USER
    assert ctx.caller_id == "user_42"


@pytest.mark.asyncio
async def test_x_api_key_takes_precedence_when_both_headers_present(
    patched_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client sending both x-api-key (valid) and Authorization (anything)
    must continue to auth via the API key, matching get_current_org's
    precedence. The JWT helpers must not be invoked.
    """
    user_calls = {"n": 0}
    org_jwt_calls = {"n": 0}

    async def should_not_run_user(_: str) -> str:
        user_calls["n"] += 1
        raise AssertionError("JWT user-auth path must not run when x-api-key is present")

    async def should_not_run_org_jwt(_: str) -> Organization:
        org_jwt_calls["n"] += 1
        raise AssertionError("JWT org-auth path must not run when x-api-key is present")

    monkeypatch.setattr(caller_context_module, "authenticate_user_helper", should_not_run_user)
    monkeypatch.setattr(caller_context_module, "authenticate_helper", should_not_run_org_jwt)

    ctx = await get_current_caller_context(
        x_api_key="key",
        authorization="Bearer something",
        x_user_agent=None,
    )
    assert ctx.caller_type == CallerType.API_KEY
    assert user_calls["n"] == 0
    assert org_jwt_calls["n"] == 0


@pytest.mark.asyncio
async def test_jwt_path_requires_both_callbacks_configured(patched_app, monkeypatch: pytest.MonkeyPatch) -> None:
    """If only authenticate_user_function is wired (no authentication_function),
    the JWT path must not be entered — otherwise we'd resolve a user_id and
    then 403 on the org step, confusing the caller.
    """
    from skyvern.forge import app

    app.authentication_function = None
    with pytest.raises(HTTPException) as exc:
        await get_current_caller_context(
            x_api_key=None,
            authorization="Bearer abc",
            x_user_agent=None,
        )
    assert exc.value.status_code == 403
