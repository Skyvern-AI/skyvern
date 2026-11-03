"""Regression tests for 1Password organization and instance-default token resolution."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.services import onepassword_token_service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "organization_token, instance_token, allowed, expected_source, expected_token, expected_denied_reason",
    [
        (SimpleNamespace(token="org-token", modified_at=None), "env-token", False, "organization", "org-token", None),
        (None, "env-token", True, "instance_default", "env-token", None),
        (None, "env-token", False, None, None, "not_allowlisted"),
        (None, None, True, None, None, "no_instance_default"),
    ],
)
async def test_resolve_onepassword_token(
    monkeypatch,
    organization_token,
    instance_token,
    allowed,
    expected_source,
    expected_token,
    expected_denied_reason,
):
    monkeypatch.setattr(
        app,
        "DATABASE",
        SimpleNamespace(
            organizations=SimpleNamespace(
                get_valid_org_auth_token=AsyncMock(return_value=organization_token),
            )
        ),
    )
    monkeypatch.setattr(onepassword_token_service.settings, "OP_SERVICE_ACCOUNT_TOKEN", instance_token)
    monkeypatch.setattr(app.AGENT_FUNCTION, "is_onepassword_instance_default_allowed", AsyncMock(return_value=allowed))
    monkeypatch.setattr(app.AGENT_FUNCTION, "onepassword_instance_default_policy_mode", lambda: "allowlist")

    resolution = await onepassword_token_service.resolve_onepassword_token("org-id")

    assert resolution.source == expected_source
    assert resolution.token == expected_token
    assert resolution.denied_reason == expected_denied_reason
