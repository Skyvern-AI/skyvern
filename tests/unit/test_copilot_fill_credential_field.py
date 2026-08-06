"""Tests for the copilot `fill_credential_field` scouting tool.

OSS-synced: only example.* / authenticationtest.com fixtures. Secret values in
fixtures are fake and exist to assert they never surface in any tool result,
recorded interaction, or error string.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from skyvern.forge import app
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.build_phase import _BROWSER_PRIMITIVE_TOOLS
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, _ground_user_provided_sites
from skyvern.forge.sdk.copilot.tools import credential_fill as credential_fill_module
from skyvern.forge.sdk.copilot.tools import mcp_hooks as mcp_hooks_module
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.schemas.credentials import CredentialType, CredentialVaultType, PasswordCredential, TotpType
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage, WorkflowCopilotChatSender

_FAKE_PASSWORD = "fake-test-password-7x9"
_FAKE_USERNAME = "qa.user@example.test"
_FAKE_TOTP_SEED = "JBSWY3DPEHPK3PXP"
_FIXTURE_LOGIN_URL = "https://authenticationtest.com/simpleFormAuth/"


def _resolved_credential(
    credential_id: str = "cred_123", tested_url: str | None = _FIXTURE_LOGIN_URL
) -> SimpleNamespace:
    return SimpleNamespace(credential_id=credential_id, name="authtest simple", tested_url=tested_url)


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
        prior_fill_carry=[],
        fill_carry_rebound_done=False,
        observed_browser_urls=[],
        pending_scout_source_url=None,
        pending_scout_download_snapshot=None,
        pending_browser_interaction_observation=None,
        discovery_mcp_server=None,
        secret_scrub_values=[],
        scouted_credential_field_inventory_by_credential_id={},
        org_credentials_for_turn=None,
        vault_login_uris_by_credential_id={},
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


class TestCredentialFillPolicyGate:
    def test_origin_comparison_uses_browser_origin_semantics(self) -> None:
        assert credential_fill_module._still_on_admitted_site(
            "https://example.com/account",
            "https://example.com:443/login",
        )
        assert credential_fill_module._still_on_admitted_site(
            "https://example.com/account",
            "example.com/login",
        )
        assert not credential_fill_module._still_on_admitted_site(
            "http://example.com/account",
            "https://example.com/login",
        )
        assert not credential_fill_module._still_on_admitted_site(
            "https://example.com:8443/account",
            "https://example.com/login",
        )

    def test_rejects_outside_code_only_mode(self) -> None:
        ctx = _ctx(block_authoring_policy=BlockAuthoringPolicy.STANDARD)
        error = tools_module._credential_fill_prerequisite_error(ctx, "cred_123")
        assert error is not None
        assert "login" in error

    def test_rejects_without_request_policy(self) -> None:
        ctx = _ctx(request_policy=None)
        assert tools_module._credential_fill_prerequisite_error(ctx, "cred_123") is not None

    def test_rejects_when_run_blocks_not_allowed(self) -> None:
        ctx = _ctx(request_policy=_policy(allow_run_blocks=False))
        assert tools_module._credential_fill_prerequisite_error(ctx, "cred_123") is not None

    def test_rejects_credential_outside_resolved_set(self) -> None:
        ctx = _ctx()
        error = tools_module._credential_fill_authority_error(ctx, "cred_999")
        assert error is not None
        assert "cred_999" in error
        assert "resolved" in error

    def test_discovered_credential_is_not_run_authorized(self) -> None:
        policy = _policy()
        policy.discovered_credentials = [_resolved_credential("cred_discovered")]
        ctx = _ctx(request_policy=policy)
        assert tools_module._credential_fill_authority_error(ctx, "cred_discovered") is not None

    def test_allows_resolved_credential_in_code_only_mode(self) -> None:
        ctx = _ctx()
        assert tools_module._credential_fill_authority_error(ctx, "cred_123") is None


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
        monkeypatch.setattr(
            app_instance,
            "AGENT_FUNCTION",
            SimpleNamespace(parse_enterprise_totp_secret=AsyncMock(return_value=None)),
            raising=False,
        )

    @pytest.mark.asyncio
    async def test_resolves_username_and_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        value, name, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "username")
        assert (value, name, error) == (_FAKE_USERNAME, "authtest simple", None)

        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "password")
        assert (value, error) == (_FAKE_PASSWORD, None)

    @pytest.mark.asyncio
    async def test_resolve_records_live_scout_field_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None))
        ctx = _ctx()
        _, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "username")
        assert error is None
        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }

    @pytest.mark.asyncio
    async def test_resolve_inventory_includes_totp_when_seed_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(
            monkeypatch,
            PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=_FAKE_TOTP_SEED),
        )
        ctx = _ctx()
        _, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "username")
        assert error is None
        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password", "totp"})
        }

    @pytest.mark.asyncio
    async def test_resolve_inventory_excludes_totp_for_runtime_only_otp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(
            monkeypatch,
            PasswordCredential(
                username=_FAKE_USERNAME,
                password=_FAKE_PASSWORD,
                totp=None,
                totp_type=TotpType.EMAIL,
                totp_identifier="ops@example.com",
            ),
        )
        ctx = _ctx()
        _, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "username")
        assert error is None
        assert ctx.scouted_credential_field_inventory_by_credential_id == {
            "cred_123": frozenset({"username", "password"})
        }

    @pytest.mark.asyncio
    async def test_resolve_inventory_excludes_empty_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password="", totp=None))
        ctx = _ctx()
        _, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "username")
        assert error is None
        assert ctx.scouted_credential_field_inventory_by_credential_id == {"cred_123": frozenset({"username"})}

    @pytest.mark.asyncio
    async def test_resolve_error_records_no_inventory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._wire_vault(monkeypatch, PasswordCredential(username=_FAKE_USERNAME, password="", totp=None))
        ctx = _ctx()
        value, _, error = await tools_module._resolve_credential_fill_value(ctx, "cred_123", "password")
        assert value is None
        assert error is not None
        assert ctx.scouted_credential_field_inventory_by_credential_id == {}

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
    @pytest.mark.parametrize(
        ("totp_type", "totp_identifier"),
        [
            pytest.param(TotpType.EMAIL, "otp@example.test", id="email-otp"),
            pytest.param(TotpType.TEXT, "+15550101111", id="text-otp"),
        ],
    )
    async def test_otp_credential_returns_runtime_otp_steer(
        self, monkeypatch: pytest.MonkeyPatch, totp_type: TotpType, totp_identifier: str
    ) -> None:
        self._wire_vault(
            monkeypatch,
            PasswordCredential(
                username=_FAKE_USERNAME,
                password=_FAKE_PASSWORD,
                totp=None,
                totp_type=totp_type,
                totp_identifier=totp_identifier,
            ),
        )
        value, _, error = await tools_module._resolve_credential_fill_value(_ctx(), "cred_123", "totp")
        assert value is None
        assert error is not None
        assert "await <credential_parameter>.otp()" in error
        assert "workflow run" in error
        assert totp_identifier not in error

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
    def __init__(
        self,
        fill_error: Exception | None = None,
        url: str = _FIXTURE_LOGIN_URL,
        release_url: str | None = None,
    ) -> None:
        self.url = url
        self.release_url = release_url
        self.fill_calls: list[tuple[Any, ...]] = []
        self.fill_kwargs: list[dict[str, Any]] = []
        self._fill_error = fill_error

    async def fill(self, *args: Any, **kwargs: Any) -> None:
        release_guard = kwargs.get("_direct_fill_release_guard")
        if release_guard is not None:
            release_guard(self.release_url if self.release_url is not None else self.url)
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

    async def fake_register(*_args: object, **_kwargs: object) -> tuple[int, None]:
        return 3, None

    monkeypatch.setattr(credential_fill_module, "_register_scout_interaction_observation", fake_register)


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


def _org_credential(
    credential_id: str,
    name: str,
    tested_url: str | None,
    credential_type: CredentialType = CredentialType.PASSWORD,
) -> SimpleNamespace:
    return SimpleNamespace(
        credential_id=credential_id, name=name, tested_url=tested_url, credential_type=credential_type
    )


class TestCredentialFillLivePageAdmission:
    """The gate consults the login page the scout reached before refusing.

    Every case starts from an empty resolved set — the state a prompt that never mentioned a
    login leaves behind once the wall turns up mid-turn.
    """

    async def _gate(
        self,
        *,
        credential_id: str,
        page_url: str | None,
        org_credentials: list[SimpleNamespace],
        policy: RequestPolicy | None = None,
        block_authoring_policy: BlockAuthoringPolicy = BlockAuthoringPolicy.CODE_ONLY_BROWSER,
    ) -> tuple[str | None, RequestPolicy, AsyncMock]:
        policy = policy if policy is not None else RequestPolicy()
        ctx = _ctx(request_policy=policy, block_authoring_policy=block_authoring_policy)
        load_mock = AsyncMock(return_value=org_credentials)
        with (
            patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
            patch.object(credential_fill_module, "_live_working_page_url", AsyncMock(return_value=page_url)),
        ):
            _, error = await credential_fill_module._credential_fill_origin_grant(ctx, credential_id)
        return error, policy, load_mock

    @pytest.mark.asyncio
    async def test_page_matched_credential_passes_the_gate(self) -> None:
        error, policy, _ = await self._gate(
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/login?next=%2Fweb",
            org_credentials=[_org_credential("cred_analytics", "analytics", "https://analytics.example.com/login")],
        )

        assert error is None
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]

    @pytest.mark.asyncio
    async def test_gate_still_refuses_a_credential_the_page_does_not_vouch_for(self) -> None:
        error, policy, _ = await self._gate(
            credential_id="cred_unrelated",
            page_url="https://analytics.example.com/login",
            org_credentials=[_org_credential("cred_unrelated", "unrelated", "https://billing.example.com/login")],
        )

        assert error is not None
        assert "cred_unrelated" in error
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_prerequisite_failures_never_consult_the_page(self) -> None:
        error, _, load_mock = await self._gate(
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/login",
            org_credentials=[_org_credential("cred_analytics", "analytics", "https://analytics.example.com/login")],
            block_authoring_policy=BlockAuthoringPolicy.STANDARD,
        )

        assert error is not None
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_turn_without_run_authority_is_not_reopened_by_the_page(self) -> None:
        error, policy, load_mock = await self._gate(
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/login",
            org_credentials=[_org_credential("cred_analytics", "analytics", "https://analytics.example.com/login")],
            policy=RequestPolicy(allow_run_blocks=False),
        )

        assert error is not None
        assert policy.resolved_credentials == []
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_resolved_credential_uses_its_tested_url_without_a_page_read(self) -> None:
        policy = RequestPolicy(resolved_credentials=[_resolved_credential()])
        ctx = _ctx(request_policy=policy)
        load_mock = AsyncMock()
        with (
            patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
            patch.object(
                credential_fill_module,
                "_live_working_page_url",
                AsyncMock(return_value="https://analytics.example.com/login"),
            ),
        ):
            grant, error = await credential_fill_module._credential_fill_origin_grant(ctx, "cred_123")

        assert error is None
        assert grant is not None
        assert grant.intended_url == _FIXTURE_LOGIN_URL
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolved_credential_is_blocked_on_a_different_live_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation guard: deleting the release-time origin comparison must make this fail."""
        page = _FakePage(url="https://elsewhere.example.com/collect")
        _wire_impl(monkeypatch, page)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_resolved_credential_redirect_immediately_before_fill_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation guard: moving the comparison before the final awaited work must make this fail."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)

        async def redirect_after_approval(_ctx: Any) -> None:
            page.url = "https://elsewhere.example.com/collect"

        monkeypatch.setattr(credential_fill_module, "_capture_scout_source_url", redirect_after_approval)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_resolved_credential_navigation_during_target_resolution_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation guard: a caller-side check before locator auto-wait must make this fail."""
        page = _FakePage(
            url=_FIXTURE_LOGIN_URL,
            release_url="https://elsewhere.example.com/collect",
        )
        _wire_impl(monkeypatch, page)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_resolved_credential_without_a_user_provided_site_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation guard: changing missing-origin denial back to a skip must make this fail."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx(request_policy=RequestPolicy(resolved_credentials=[_resolved_credential(tested_url=None)]))

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert "cannot be filled" in result["error"]
        assert page.fill_calls == []

    async def _unbound_grant(
        self,
        *,
        page_url: str | None,
        user_urls: list[str] | None = None,
        user_text: str = "",
        named: bool = False,
        also_resolved: list[str] | None = None,
        org_credentials: list[SimpleNamespace] | None = None,
    ) -> tuple[Any, str | None]:
        """Grant for a resolved credential carrying no saved login URL."""
        resolved: list[Any] = [_resolved_credential(tested_url=None)]
        resolved.extend(SimpleNamespace(credential_id=extra, tested_url=None) for extra in also_resolved or [])
        policy = RequestPolicy(resolved_credentials=resolved)
        policy.user_provided_site_urls = list(user_urls or [])
        if named:
            policy.current_turn_named_credential_ids = {"cred_123"}
        ctx = _ctx(request_policy=policy)
        with (
            patch(
                "skyvern.forge.app.DATABASE.credentials.get_credentials",
                new=AsyncMock(return_value=org_credentials or []),
            ),
            patch.object(credential_fill_module, "_live_working_page_url", AsyncMock(return_value=page_url)),
        ):
            return await credential_fill_module._credential_fill_origin_grant(ctx, "cred_123")

    @pytest.mark.asyncio
    async def test_a_site_the_user_pasted_releases_the_named_credential(self) -> None:
        grant, error = await self._unbound_grant(
            page_url=_FIXTURE_LOGIN_URL,
            user_urls=[_FIXTURE_LOGIN_URL],
            named=True,
        )

        assert error is None
        assert grant is not None and grant.whole_site

    @pytest.mark.asyncio
    async def test_a_site_the_user_pasted_in_an_earlier_turn_still_releases(self) -> None:
        """The production dead end: the user pasted the URL one turn after naming the credential; a
        user-provided site now counts from any message of the chat."""
        grant, error = await self._unbound_grant(
            page_url="https://us.pathfold.com/login",
            user_urls=["https://us.pathfold.com/project/1234/dashboard/5678"],
        )

        assert error is None
        assert grant is not None

    @pytest.mark.asyncio
    async def test_a_site_the_user_never_provided_refuses_and_names_the_page(self) -> None:
        grant, error = await self._unbound_grant(
            page_url="https://evil.example.net/login",
            user_urls=["https://us.pathfold.com/"],
            user_text="log into pathfold",
            named=True,
        )

        assert grant is None
        assert error is not None
        assert "has not named this site" in error

    @pytest.mark.asyncio
    async def test_a_lookalike_domain_does_not_match_the_user_site(self) -> None:
        grant, error = await self._unbound_grant(
            page_url="https://authenticationtest.com.example.net/simpleFormAuth/",
            user_urls=[_FIXTURE_LOGIN_URL],
            named=True,
        )

        assert grant is None
        assert error is not None

    @pytest.mark.asyncio
    async def test_either_of_two_user_provided_sites_releases(self) -> None:
        """Reversal of the old sole-origin rule: both sites are the user's own words, so standing on
        either releases; a site the user never gave still refuses."""
        for page_url in (_FIXTURE_LOGIN_URL, "https://tracker-b.example/login"):
            grant, error = await self._unbound_grant(
                page_url=page_url,
                user_urls=[_FIXTURE_LOGIN_URL, "https://tracker-b.example/login"],
                named=True,
            )

            assert error is None, page_url
            assert grant is not None

    @pytest.mark.asyncio
    async def test_a_localhost_site_the_user_pasted_releases_origin_scoped(self) -> None:
        """No public-suffix site exists for localhost/internal hosts; the exact origin the user
        pasted still releases, scoped to that origin."""
        grant, error = await self._unbound_grant(
            page_url="http://localhost:8901/analytics_console/pathfold/",
            user_urls=["http://localhost:8901/analytics_console/pathfold/?date_from=-7d"],
            named=True,
        )

        assert error is None
        assert grant is not None and not grant.whole_site

    @pytest.mark.asyncio
    async def test_a_login_target_the_user_never_wrote_cannot_vouch(self) -> None:
        """The classifier (or any model) does not get to author the site a password reaches."""
        policy = RequestPolicy(
            resolved_credentials=[_resolved_credential(tested_url=None)],
            login_page_urls=[_FIXTURE_LOGIN_URL],
        )
        policy.current_turn_named_credential_ids = {"cred_123"}
        ctx = _ctx(request_policy=policy)
        with (
            patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=[])),
            patch.object(credential_fill_module, "_live_working_page_url", AsyncMock(return_value=_FIXTURE_LOGIN_URL)),
        ):
            grant, error = await credential_fill_module._credential_fill_origin_grant(ctx, "cred_123")

        assert grant is None
        assert error is not None

    @pytest.mark.asyncio
    async def test_the_sole_resolved_credential_needs_no_renaming(self) -> None:
        """Never re-ask what's already answered: one credential resolved for the request (e.g. the
        card answer, carried) is settled even when this turn's message never names it."""
        grant, error = await self._unbound_grant(
            page_url=_FIXTURE_LOGIN_URL,
            user_urls=[_FIXTURE_LOGIN_URL],
            named=False,
        )

        assert error is None
        assert grant is not None

    @pytest.mark.asyncio
    async def test_two_resolved_credentials_with_none_named_ask_rather_than_guess(self) -> None:
        grant, error = await self._unbound_grant(
            page_url=_FIXTURE_LOGIN_URL,
            user_urls=[_FIXTURE_LOGIN_URL],
            named=False,
            also_resolved=["cred_other"],
        )

        assert grant is None
        assert error is not None
        # Only an exact name or a cred_ id is read back off the next message, so an ask that would
        # settle for "yes" re-asks forever — the loop this seam exists to end.
        assert "exact name" in error and "cred_" in error

    @pytest.mark.asyncio
    async def test_naming_this_turn_settles_among_several_resolved(self) -> None:
        grant, error = await self._unbound_grant(
            page_url=_FIXTURE_LOGIN_URL,
            user_urls=[_FIXTURE_LOGIN_URL],
            named=True,
            also_resolved=["cred_other"],
        )

        assert error is None
        assert grant is not None

    @pytest.mark.asyncio
    async def test_the_only_saved_org_password_settles_by_elimination(self) -> None:
        grant, error = await self._unbound_grant(
            page_url=_FIXTURE_LOGIN_URL,
            user_urls=[_FIXTURE_LOGIN_URL],
            named=False,
            also_resolved=["cred_other"],
            org_credentials=[
                _org_credential("cred_123", "authtest simple", None),
                _org_credential("cred_card", "company card", None, CredentialType.CREDIT_CARD),
            ],
        )

        assert error is None
        assert grant is not None

    @pytest.mark.asyncio
    async def test_the_production_transcript_reaches_a_grant(self) -> None:
        """Acceptance: prose ask -> card answer (sole resolved, carried) -> URL reply."""
        policy = RequestPolicy(resolved_credentials=[_resolved_credential(tested_url=None)])
        history = [
            WorkflowCopilotChatHistoryMessage(
                sender=WorkflowCopilotChatSender.USER,
                content=(
                    "cred_123\n\ncan you use this credential to log into the pathfold website and tell me "
                    "how many website visitors skyvern got in the past 7 days?"
                ),
                created_at=datetime.now(UTC),
            ),
            WorkflowCopilotChatHistoryMessage(
                sender=WorkflowCopilotChatSender.AI,
                content="I created a draft workflow with 1 block and tested it, but the test failed.",
                created_at=datetime.now(UTC),
            ),
        ]
        _ground_user_provided_sites(
            policy, "here ist he url: https://us.pathfold.com/project/1234/dashboard/5678", history
        )
        ctx = _ctx(request_policy=policy)
        with (
            patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=AsyncMock(return_value=[])),
            patch.object(
                credential_fill_module,
                "_live_working_page_url",
                AsyncMock(return_value="https://us.pathfold.com/login"),
            ),
        ):
            grant, error = await credential_fill_module._credential_fill_origin_grant(ctx, "cred_123")

        assert error is None
        assert grant is not None

    @pytest.mark.asyncio
    async def test_live_page_admitted_credential_short_circuits_without_an_org_lookup(self) -> None:
        error, _, load_mock = await self._gate(
            credential_id="cred_123",
            page_url="https://analytics.example.com/login",
            org_credentials=[],
            policy=RequestPolicy(
                resolved_credentials=[_resolved_credential(tested_url=None)],
                live_page_admitted_urls={"cred_123": _FIXTURE_LOGIN_URL},
            ),
        )

        assert error is None
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_page_matched_credential_reaches_the_vault_and_fills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The end-to-end seam: admission has to unblock the actual fill, not just the gate."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_analytics", "password")

        assert result["ok"] is True
        assert page.fill_calls == [("#passwordInput", _FAKE_PASSWORD)]
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]

    @pytest.mark.asyncio
    async def test_a_card_connected_credential_with_no_tested_url_fills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The resume stamps the ask's origin, so a credential created from the card can sign in."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        policy = RequestPolicy(
            resolved_credentials=[_resolved_credential(tested_url=None)],
            live_page_admitted_urls={"cred_123": _FIXTURE_LOGIN_URL},
        )
        ctx = _ctx(request_policy=policy)

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(side_effect=AssertionError("a stamped credential needs no org scan")),
        ):
            result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is True
        assert page.fill_calls == [("#passwordInput", _FAKE_PASSWORD)]

    @pytest.mark.asyncio
    async def test_a_card_connected_credential_still_cannot_follow_a_redirect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        policy = RequestPolicy(
            resolved_credentials=[_resolved_credential(tested_url=None)],
            live_page_admitted_urls={"cred_123": _FIXTURE_LOGIN_URL},
        )
        ctx = _ctx(request_policy=policy)

        async def redirect_then_capture(_ctx: Any) -> None:
            page.url = "https://elsewhere.example.com/collect"

        monkeypatch.setattr(credential_fill_module, "_capture_scout_source_url", redirect_then_capture)

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_a_redirect_after_admission_stops_the_secret_reaching_the_new_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Vault reads sit between the page match and the fill, so the page is re-checked."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        async def redirect_then_capture(_ctx: Any) -> None:
            page.url = "https://elsewhere.example.com/collect"

        monkeypatch.setattr(credential_fill_module, "_capture_scout_source_url", redirect_then_capture)

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_analytics", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_the_second_fill_of_an_admitted_credential_is_still_page_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Username then password is the ordinary flow, and the page can move in between."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx(request_policy=RequestPolicy())

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            first = await tools_module._fill_credential_field_impl(ctx, "#user", "cred_analytics", "username")
            page.url = "https://evil.example.com/harvest"
            second = await tools_module._fill_credential_field_impl(ctx, "#pass", "cred_analytics", "password")

        assert first["ok"] is True
        assert second["ok"] is False
        assert page.fill_calls == [("#user", _FAKE_PASSWORD)]

    @pytest.mark.asyncio
    async def test_a_refusable_call_never_reads_the_org_credentials(self) -> None:
        """The refusals that need no lookup must not page the credential table in first."""
        policy = RequestPolicy(login_intent=True, email_signin_intent=True, credential_input_kind="none")
        error, _, load_mock = await self._gate(
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/login",
            org_credentials=[_org_credential("cred_analytics", "analytics", "https://analytics.example.com/login")],
            policy=policy,
        )

        assert error is not None
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_second_step_on_the_same_site_still_fills(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real sign-ins walk email -> password -> one-time code across paths of the same site;
        the live PostHog case moved to a 2FA path between the password and the code."""
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx(request_policy=RequestPolicy())

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            first = await tools_module._fill_credential_field_impl(ctx, "#user", "cred_analytics", "username")
            page.url = "https://authenticationtest.com/simpleFormAuth/verify?step=2fa"
            second = await tools_module._fill_credential_field_impl(ctx, "#code", "cred_analytics", "totp")

        assert first["ok"] is True
        assert second["ok"] is True
        assert len(page.fill_calls) == 2


_DECOY_LOGIN_URL = "https://billing.example.com/login"


class TestObservationSeamCredentialBinding:
    """Page observation binds the sole URL-matched credential before any fill is attempted.

    Every case starts from an empty resolved set, which is what a prompt with no login wording
    leaves behind when the scout walks into a sign-in wall.
    """

    async def _observe_navigate(
        self,
        ctx: SimpleNamespace,
        url: str,
        org_credentials: list[SimpleNamespace],
    ) -> tuple[dict[str, Any], AsyncMock]:
        load_mock = AsyncMock(return_value=org_credentials)
        with patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock):
            result = await tools_module._navigate_post_hook({"ok": True, "data": {"url": url}}, {}, ctx)
        return result, load_mock

    @pytest.mark.asyncio
    async def test_navigating_to_a_matched_login_page_binds_and_surfaces_the_id(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        result, _ = await self._observe_navigate(
            ctx,
            _FIXTURE_LOGIN_URL,
            [
                _org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL),
                _org_credential("cred_billing", "billing", _DECOY_LOGIN_URL),
                _org_credential("cred_urlless", "urlless", None),
            ],
        )

        assert result["resolved_login_credential_id"] == "cred_analytics"
        assert result["resolved_login_credential_name"] == "analytics"
        assert "candidate_login_credentials" not in result

    @pytest.mark.asyncio
    async def test_the_surfaced_id_passes_the_fill_gate_without_user_confirmation(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        result, _ = await self._observe_navigate(
            ctx, _FIXTURE_LOGIN_URL, [_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]
        )

        with patch.object(credential_fill_module, "_live_working_page_url", AsyncMock(return_value=_FIXTURE_LOGIN_URL)):
            grant, error = await credential_fill_module._credential_fill_origin_grant(
                ctx, result["resolved_login_credential_id"]
            )

        assert error is None
        assert grant is not None
        assert grant.intended_url == _FIXTURE_LOGIN_URL

    @pytest.mark.asyncio
    async def test_leaving_the_admitted_origin_after_the_seam_bind_refuses_the_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx(request_policy=RequestPolicy())
        await self._observe_navigate(
            ctx, _FIXTURE_LOGIN_URL, [_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]
        )
        page.url = "https://elsewhere.example.com/collect"

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_analytics", "password")

        assert result["ok"] is False
        assert page.fill_calls == []

    @pytest.mark.asyncio
    async def test_a_placeholder_observation_url_is_resolved_to_the_live_page_before_binding(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        result: dict[str, Any] = {"ok": True}

        with (
            patch.object(mcp_hooks_module, "_fallback_page_info", AsyncMock(return_value=(_FIXTURE_LOGIN_URL, ""))),
            patch(
                "skyvern.forge.app.DATABASE.credentials.get_credentials",
                new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
            ),
        ):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, "current_page", result)

        assert result["resolved_login_credential_id"] == "cred_analytics"
        assert result["resolved_login_page_url"] == _FIXTURE_LOGIN_URL
        assert policy.live_page_admitted_urls == {"cred_analytics": _FIXTURE_LOGIN_URL}

    @pytest.mark.asyncio
    async def test_a_resolved_page_matching_nothing_still_binds_nothing(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        result: dict[str, Any] = {"ok": True}

        with (
            patch.object(mcp_hooks_module, "_fallback_page_info", AsyncMock(return_value=(_FIXTURE_LOGIN_URL, ""))),
            patch(
                "skyvern.forge.app.DATABASE.credentials.get_credentials",
                new=AsyncMock(return_value=[_org_credential("cred_billing", "billing", _DECOY_LOGIN_URL)]),
            ),
        ):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, "current_page", result)

        assert "resolved_login_credential_id" not in result
        assert "resolved_login_page_url" not in result
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_a_resolved_page_matching_two_credentials_offers_both_and_binds_neither(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        result: dict[str, Any] = {"ok": True}

        with (
            patch.object(mcp_hooks_module, "_fallback_page_info", AsyncMock(return_value=(_FIXTURE_LOGIN_URL, ""))),
            patch(
                "skyvern.forge.app.DATABASE.credentials.get_credentials",
                new=AsyncMock(
                    return_value=[
                        _org_credential("cred_one", "analytics one", _FIXTURE_LOGIN_URL),
                        _org_credential("cred_two", "analytics two", _FIXTURE_LOGIN_URL),
                    ]
                ),
            ),
        ):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, "current_page", result)

        assert result["candidate_login_credentials"] == [
            {"credential_id": "cred_one", "name": "analytics one"},
            {"credential_id": "cred_two", "name": "analytics two"},
        ]
        assert "resolved_login_credential_id" not in result
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    async def test_a_turn_without_run_authority_never_reads_the_live_page(self) -> None:
        policy = RequestPolicy(allow_run_blocks=False)
        ctx = _ctx(request_policy=policy)
        reread = AsyncMock(return_value=(_FIXTURE_LOGIN_URL, ""))

        with patch.object(mcp_hooks_module, "_fallback_page_info", reread):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, "current_page", {"ok": True})

        reread.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("observed_url", ["current_page", "http://[", "http://[::1"])
    # No URL at all is absence of evidence; one that is not a page is evidence about no page. The
    # seam separates them, and neither may become a claim about the org's credentials.
    @pytest.mark.parametrize(("reread_url", "outcome"), [("", "declined"), ("about:blank", "abstain")])
    async def test_an_unresolvable_observation_url_reads_no_credentials_at_all(
        self, observed_url: str, reread_url: str, outcome: str
    ) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        result: dict[str, Any] = {"ok": True}
        load_mock = AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)])

        with (
            capture_logs() as logs,
            patch.object(mcp_hooks_module, "_fallback_page_info", AsyncMock(return_value=(reread_url, ""))),
            patch("skyvern.forge.app.DATABASE.credentials.get_credentials", new=load_mock),
        ):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, observed_url, result)

        emitted = [entry for entry in logs if entry["event"] == "copilot credential live-page admission"]
        assert [(entry["seam"], entry["outcome"]) for entry in emitted] == [("page_observation", outcome)]
        assert "resolved_login_credential_id" not in result
        assert policy.resolved_credentials == []
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_two_matches_surface_both_ids_and_bind_nothing(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        result, _ = await self._observe_navigate(
            ctx,
            _FIXTURE_LOGIN_URL,
            [
                _org_credential("cred_one", "analytics one", _FIXTURE_LOGIN_URL),
                _org_credential("cred_two", "analytics two", _FIXTURE_LOGIN_URL),
            ],
        )

        assert result["candidate_login_credentials"] == [
            {"credential_id": "cred_one", "name": "analytics one"},
            {"credential_id": "cred_two", "name": "analytics two"},
        ]
        assert "resolved_login_credential_id" not in result
        assert policy.resolved_credentials == []
        assert policy.live_page_admitted_urls == {}

    @pytest.mark.asyncio
    async def test_no_match_leaves_the_observation_exactly_as_it_was(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        result, _ = await self._observe_navigate(
            ctx,
            _FIXTURE_LOGIN_URL,
            [_org_credential("cred_billing", "billing", _DECOY_LOGIN_URL), _org_credential("cred_urlless", "u", None)],
        )

        assert result["ok"] is True
        assert "resolved_login_credential_id" not in result
        assert "resolved_login_credential_name" not in result
        assert "candidate_login_credentials" not in result
        assert policy.resolved_credentials == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "org_credentials",
        [
            # Each arm carries a non-matching credential whose own tested_url is the decoy, so the
            # decoy assertion below can actually fail: a serializer that leaked saved URLs would
            # carry this one even though the page never matched it.
            [
                _org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL),
                _org_credential("cred_decoy", "decoy", _DECOY_LOGIN_URL),
            ],
            [
                _org_credential("cred_one", "analytics one", _FIXTURE_LOGIN_URL),
                _org_credential("cred_two", "analytics two", _FIXTURE_LOGIN_URL),
                _org_credential("cred_decoy", "decoy", _DECOY_LOGIN_URL),
            ],
        ],
    )
    async def test_no_saved_login_url_reaches_the_model(self, org_credentials: list[SimpleNamespace]) -> None:
        ctx = _ctx(request_policy=RequestPolicy())

        result, _ = await self._observe_navigate(ctx, _FIXTURE_LOGIN_URL, org_credentials)

        serialized = json.dumps(result)
        assert "tested_url" not in serialized
        assert _DECOY_LOGIN_URL not in serialized

    @pytest.mark.asyncio
    async def test_a_click_that_lands_on_the_login_wall_binds(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(
            request_policy=policy,
            pending_scout_role_name=None,
            pending_scout_click_selector=None,
            pending_scout_ambiguous=None,
            pending_scout_reanchor=None,
            pending_scout_dynamic_row=None,
            last_scout_act_observe_outcome=None,
            last_scout_act_observe_packet=None,
        )

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            result = await tools_module._click_post_hook(
                {"ok": True, "data": {"selector": "#sign-in"}},
                {"browser_context": {"url": _FIXTURE_LOGIN_URL, "title": "Sign in"}},
                ctx,
            )

        assert result["resolved_login_credential_id"] == "cred_analytics"
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]

    @pytest.mark.asyncio
    async def test_an_enter_press_that_lands_on_the_login_wall_binds(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(return_value=[_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]),
        ):
            result = await tools_module._press_key_post_hook(
                {"ok": True, "data": {"key": "Enter", "selector": "#search"}},
                {"browser_context": {"url": _FIXTURE_LOGIN_URL, "title": "Sign in"}},
                ctx,
            )

        assert result["resolved_login_credential_id"] == "cred_analytics"
        assert [c.credential_id for c in policy.resolved_credentials] == ["cred_analytics"]

    @pytest.mark.asyncio
    async def test_a_credential_loader_failure_leaves_the_tool_result_untouched(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)

        with patch(
            "skyvern.forge.app.DATABASE.credentials.get_credentials",
            new=AsyncMock(side_effect=RuntimeError("credential table unavailable")),
        ):
            result = await tools_module._navigate_post_hook({"ok": True, "data": {"url": _FIXTURE_LOGIN_URL}}, {}, ctx)

        assert result["ok"] is True
        assert "resolved_login_credential_id" not in result
        assert policy.resolved_credentials == []
        assert policy.live_page_admitted_urls == {}

    @pytest.mark.asyncio
    async def test_the_lite_lane_never_reads_credentials(self) -> None:
        ctx = _ctx(request_policy=None)
        result, load_mock = await self._observe_navigate(
            ctx, _FIXTURE_LOGIN_URL, [_org_credential("cred_analytics", "analytics", _FIXTURE_LOGIN_URL)]
        )

        assert "resolved_login_credential_id" not in result
        load_mock.assert_not_awaited()


class TestVaultNamedSiteGrant:
    """A credential goes where its own vault entry says it belongs, without a test run first."""

    async def _grant(self, *, vault_uris: list[str], page_url: str) -> tuple[Any, str | None]:
        policy = RequestPolicy(resolved_credentials=[_resolved_credential(tested_url=None)])
        ctx = _ctx(request_policy=policy, vault_login_uris_by_credential_id={"cred_123": vault_uris})
        with patch.object(credential_fill_module, "_live_working_page_url", AsyncMock(return_value=page_url)):
            return await credential_fill_module._credential_fill_origin_grant(ctx, "cred_123")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "page_url",
        [
            "https://example.com/login",
            "https://eu.example.com/login",
            "https://usercontent.example.com/uploads/x",
        ],
    )
    async def test_the_vault_site_covers_the_whole_site(self, page_url: str) -> None:
        grant, error = await self._grant(vault_uris=["https://example.com"], page_url=page_url)

        assert error is None
        assert grant is not None and grant.whole_site is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "page_url",
        [
            "https://example.com.evil.test/login",
            "https://example-login.test/login",
            "https://bob.github.io/login",
        ],
    )
    async def test_another_site_is_refused(self, page_url: str) -> None:
        grant, error = await self._grant(
            vault_uris=["https://example.com", "https://alice.github.io"], page_url=page_url
        )

        assert grant is None
        assert error is not None

    @pytest.mark.asyncio
    async def test_a_whole_site_grant_still_cannot_leave_that_site(self) -> None:
        grant, _ = await self._grant(vault_uris=["https://example.com"], page_url="https://eu.example.com/login")

        assert grant is not None
        assert credential_fill_module._within_grant("https://other.example.com/step2", grant)
        assert not credential_fill_module._within_grant("https://example.com.evil.test/step2", grant)

    @pytest.mark.parametrize(
        "page_url,reachable",
        [
            ("https://eu.example.com/login", True),
            ("https://usercontent.example.com/x", True),
            ("http://example.com/login", False),
            ("http://eu.example.com/login", False),
            ("https://example.com:8443/login", False),
            ("https://example.com.evil.test/login", False),
        ],
    )
    def test_a_site_wide_grant_moves_between_hosts_but_not_schemes_or_ports(
        self, page_url: str, reachable: bool
    ) -> None:
        """Host mobility is the point; reaching the same site in cleartext or on another port is not."""
        grant = credential_fill_module._CredentialFillOriginGrant("https://example.com/login", whole_site=True)

        assert credential_fill_module._within_grant(page_url, grant) is reachable

    def test_a_tested_credential_keeps_the_tighter_scope(self) -> None:
        """Evidence naming one page grants one origin; only a site-level entry grants a site."""
        page_grant = credential_fill_module._CredentialFillOriginGrant("https://eu.example.com/login")

        assert not credential_fill_module._within_grant("https://other.example.com/login", page_grant)
        assert credential_fill_module._within_grant("https://eu.example.com/step2", page_grant)
