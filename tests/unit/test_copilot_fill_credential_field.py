"""Tests for the copilot `fill_credential_field` scouting tool.

OSS-synced: only example.* / authenticationtest.com fixtures. Secret values in
fixtures are fake and exist to assert they never surface in any tool result,
recorded interaction, or error string.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from skyvern.forge import app
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.build_phase import _BROWSER_PRIMITIVE_TOOLS
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.tools import credential_fill as credential_fill_module
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.schemas.credentials import CredentialVaultType, PasswordCredential

_FAKE_PASSWORD = "fake-test-password-7x9"
_FAKE_USERNAME = "qa.user@example.test"
_FAKE_TOTP_SEED = "JBSWY3DPEHPK3PXP"


def _resolved_credential(credential_id: str = "cred_123") -> SimpleNamespace:
    return SimpleNamespace(credential_id=credential_id, name="authtest simple")


def _policy(**overrides: Any) -> RequestPolicy:
    policy = RequestPolicy(resolved_credentials=[_resolved_credential()])
    for key, value in overrides.items():
        setattr(policy, key, value)
    return policy


def _ctx(**overrides: Any) -> SimpleNamespace:
    ns = SimpleNamespace(
        organization_id="o_1",
        request_policy=_policy(),
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        browser_session_id="pbs_1",
        scouted_interactions=[],
        scout_trajectory=[],
        observed_browser_urls=[],
        pending_scout_source_url=None,
        pending_browser_interaction_observation=None,
        discovery_mcp_server=None,
        secret_scrub_values=[],
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class TestCredentialFillPolicyGate:
    def test_rejects_outside_code_only_mode(self) -> None:
        ctx = _ctx(block_authoring_policy=BlockAuthoringPolicy.STANDARD)
        error = tools_module._credential_fill_policy_error(ctx, "cred_123")
        assert error is not None
        assert "login" in error

    def test_rejects_without_request_policy(self) -> None:
        ctx = _ctx(request_policy=None)
        assert tools_module._credential_fill_policy_error(ctx, "cred_123") is not None

    def test_rejects_when_run_blocks_not_allowed(self) -> None:
        ctx = _ctx(request_policy=_policy(allow_run_blocks=False))
        assert tools_module._credential_fill_policy_error(ctx, "cred_123") is not None

    def test_rejects_credential_outside_resolved_set(self) -> None:
        ctx = _ctx()
        error = tools_module._credential_fill_policy_error(ctx, "cred_999")
        assert error is not None
        assert "cred_999" in error
        assert "resolved" in error

    def test_discovered_credential_is_not_run_authorized(self) -> None:
        policy = _policy()
        policy.discovered_credentials = [_resolved_credential("cred_discovered")]
        ctx = _ctx(request_policy=policy)
        assert tools_module._credential_fill_policy_error(ctx, "cred_discovered") is not None

    def test_allows_resolved_credential_in_code_only_mode(self) -> None:
        ctx = _ctx()
        assert tools_module._credential_fill_policy_error(ctx, "cred_123") is None


class TestResolveCredentialFillValue:
    def _wire_vault(
        self,
        monkeypatch: pytest.MonkeyPatch,
        credential: Any,
        *,
        name: str = "authtest simple",
    ) -> None:
        db_credential = SimpleNamespace(vault_type=CredentialVaultType.BITWARDEN)
        monkeypatch.setattr(
            app.DATABASE,
            "credentials",
            SimpleNamespace(get_credential=AsyncMock(return_value=db_credential)),
            raising=False,
        )
        vault = SimpleNamespace(
            get_credential_item=AsyncMock(return_value=SimpleNamespace(name=name, credential=credential))
        )
        # `app` is an AppHolder proxy without __delattr__; patch the underlying instance
        # so monkeypatch teardown can delete the attribute it set.
        app_instance = object.__getattribute__(app, "_inst")
        monkeypatch.setattr(
            app_instance, "CREDENTIAL_VAULT_SERVICES", {CredentialVaultType.BITWARDEN: vault}, raising=False
        )

    @pytest.mark.asyncio
    async def test_resolves_username_and_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        value, name, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "username")
        assert (value, name, error) == (_FAKE_USERNAME, "authtest simple", None)

        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "password")
        assert (value, error) == (_FAKE_PASSWORD, None)

    @pytest.mark.asyncio
    async def test_totp_mints_fresh_code_not_the_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(
            monkeypatch,
            PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=_FAKE_TOTP_SEED),
        )
        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "totp")
        assert error is None
        assert value is not None
        assert value.isdigit()
        assert len(value) == 6
        assert value != _FAKE_TOTP_SEED

    @pytest.mark.asyncio
    async def test_password_resolve_registers_scrub_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        ctx = _ctx()
        value, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "password")
        assert (value, error) == (_FAKE_PASSWORD, None)
        assert ctx.secret_scrub_values == [_FAKE_PASSWORD]

    @pytest.mark.asyncio
    async def test_username_resolve_does_not_register_scrub_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        ctx = _ctx()
        value, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "username")
        assert (value, error) == (_FAKE_USERNAME, None)
        assert ctx.secret_scrub_values == []

    @pytest.mark.asyncio
    async def test_minted_otp_is_registered_at_mint_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(
            monkeypatch,
            PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=_FAKE_TOTP_SEED),
        )
        ctx = _ctx()
        value, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "totp")
        assert error is None
        assert ctx.secret_scrub_values == [value]
        assert _FAKE_TOTP_SEED not in ctx.secret_scrub_values

    @pytest.mark.asyncio
    async def test_totp_without_seed_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "totp")
        assert value is None
        assert error is not None
        assert "TOTP" in error

    @pytest.mark.asyncio
    async def test_missing_credential_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            app.DATABASE,
            "credentials",
            SimpleNamespace(get_credential=AsyncMock(return_value=None)),
            raising=False,
        )
        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "username")
        assert value is None
        assert error is not None
        assert "cred_123" in error

    @pytest.mark.asyncio
    async def test_vault_exception_error_carries_no_secret_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        db_credential = SimpleNamespace(vault_type=CredentialVaultType.BITWARDEN)
        monkeypatch.setattr(
            app.DATABASE,
            "credentials",
            SimpleNamespace(get_credential=AsyncMock(return_value=db_credential)),
            raising=False,
        )
        vault = SimpleNamespace(get_credential_item=AsyncMock(side_effect=RuntimeError(f"vault said {_FAKE_PASSWORD}")))
        app_instance = object.__getattribute__(app, "_inst")
        monkeypatch.setattr(
            app_instance, "CREDENTIAL_VAULT_SERVICES", {CredentialVaultType.BITWARDEN: vault}, raising=False
        )
        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "password")
        assert value is None
        assert error is not None
        assert _FAKE_PASSWORD not in error


class _FakePage:
    def __init__(self, fill_error: Exception | None = None) -> None:
        self.fill_calls: list[tuple[Any, ...]] = []
        self.fill_kwargs: list[dict[str, Any]] = []
        self._fill_error = fill_error

    async def fill(self, *args: Any, **kwargs: Any) -> None:
        self.fill_calls.append(args)
        self.fill_kwargs.append(kwargs)
        if self._fill_error is not None:
            raise self._fill_error


def _wire_impl(
    monkeypatch: pytest.MonkeyPatch,
    page: _FakePage,
    *,
    secret_value: str = _FAKE_PASSWORD,
    credential_name: str = "authtest simple",
) -> None:
    async def fake_resolve(_ctx: Any, _credential_id: str, _field: str) -> tuple[str, str, None]:
        return secret_value, credential_name, None

    async def fake_ensure(_ctx: Any) -> None:
        return None

    @asynccontextmanager
    async def fake_browser_context(_ctx: Any) -> AsyncIterator[None]:
        yield

    async def fake_get_page(session_id: str | None = None) -> tuple[_FakePage, None]:
        return page, None

    async def fake_verify(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fake_url(_ctx: Any) -> str:
        return "https://authenticationtest.com/simpleFormAuth/"

    async def fake_role_name(*_args: Any, **_kwargs: Any) -> tuple[str, str]:
        return "textbox", "Password"

    monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", fake_resolve)
    monkeypatch.setattr(credential_fill_module, "ensure_browser_session", fake_ensure)
    monkeypatch.setattr(credential_fill_module, "mcp_browser_context", fake_browser_context)
    monkeypatch.setattr(credential_fill_module, "get_page", fake_get_page)
    monkeypatch.setattr(credential_fill_module, "_verify_scout_type_landed", fake_verify)
    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", fake_url)
    monkeypatch.setattr(scouting_module, "_live_working_page_url", fake_url)
    monkeypatch.setattr(credential_fill_module, "_resolve_scout_role_name", fake_role_name)
    monkeypatch.setattr(credential_fill_module, "_tool_loop_error", lambda *a, **k: None)
    monkeypatch.setattr(credential_fill_module, "_authority_tool_error", lambda *a, **k: None)
    monkeypatch.setattr(credential_fill_module, "record_tool_step_result_for_ctx", lambda *a, **k: None)
    monkeypatch.setattr(credential_fill_module, "_register_scout_interaction_observation", lambda *a, **k: 3)


class TestFillCredentialFieldImpl:
    @pytest.mark.asyncio
    async def test_happy_path_fills_and_records_value_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is True
        assert page.fill_calls == [("#passwordInput", _FAKE_PASSWORD)]
        assert page.fill_kwargs[0]["mode"] == "direct"
        assert result["data"]["typed_length"] == len(_FAKE_PASSWORD)
        assert result["data"]["credential_id"] == "cred_123"
        assert result["data"]["field"] == "password"
        assert result["data"]["observation_step"] == 3
        assert _FAKE_PASSWORD not in json.dumps(result)

        assert len(ctx.scouted_interactions) == 1
        recorded = ctx.scouted_interactions[0]
        assert recorded["tool_name"] == "fill_credential_field"
        assert recorded["credential_id"] == "cred_123"
        assert recorded["credential_field"] == "password"
        assert recorded["credential_name"] == "authtest simple"
        assert recorded["typed_length"] == len(_FAKE_PASSWORD)
        assert _FAKE_PASSWORD not in json.dumps(recorded)
        assert _FAKE_PASSWORD not in json.dumps(ctx.scout_trajectory)

    @pytest.mark.asyncio
    async def test_fill_error_text_is_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage(fill_error=RuntimeError(f"could not type {_FAKE_PASSWORD} into element"))
        _wire_impl(monkeypatch, page)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert _FAKE_PASSWORD not in result["error"]
        assert "[REDACTED_SECRET]" in result["error"]

    @pytest.mark.asyncio
    async def test_rejects_unknown_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#cvv", "cred_123", "cvv")

        assert result["ok"] is False
        assert "username, password, totp" in result["error"]
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_rejects_empty_selector(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)

        result = await tools_module._fill_credential_field_impl(_ctx(), "   ", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_unresolved_credential_never_reaches_vault_or_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        resolver = AsyncMock()
        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", resolver)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_999", "password")

        assert result["ok"] is False
        assert "cred_999" in result["error"]
        resolver.assert_not_awaited()
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_standard_mode_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx(block_authoring_policy=BlockAuthoringPolicy.STANDARD)

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_readback_failure_surfaces_and_skips_recording(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)

        async def failing_verify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"ok": False, "error": "field is still empty"}

        monkeypatch.setattr(credential_fill_module, "_verify_scout_type_landed", failing_verify)
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result == {"ok": False, "error": "field is still empty"}
        assert ctx.scouted_interactions == []


class TestConsecutiveLoopGuardExemption:
    def test_three_consecutive_fills_are_not_loop_blocked(self) -> None:
        ctx = _ctx(consecutive_tool_tracker=[])
        for field in ("username", "password", "totp"):
            error = tools_module._tool_loop_error(
                ctx, "fill_credential_field", {"selector": f"#{field}", "field": field}
            )
            assert error is None
        assert ctx.consecutive_tool_tracker == []

    def test_exemption_set_membership(self) -> None:
        assert "fill_credential_field" in tools_module._CONSECUTIVE_LOOP_GUARD_EXEMPT_TOOLS


class TestToolRegistration:
    def test_tool_is_registered_native(self) -> None:
        names = [tool.name for tool in tools_module.NATIVE_TOOLS]
        assert "fill_credential_field" in names

    def test_tool_description_states_value_free_contract(self) -> None:
        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        description = tool.description or ""
        assert "server-side" in description
        assert "never" in description
        assert "type_text" in description

    def test_tool_is_phase_gated_as_browser_primitive(self) -> None:
        assert "fill_credential_field" in _BROWSER_PRIMITIVE_TOOLS
