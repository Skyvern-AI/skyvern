"""Tests for the copilot `fill_credential_field` scouting tool.

OSS-synced: only example.* / authenticationtest.com fixtures. Secret values in
fixtures are fake and exist to assert they never surface in any tool result,
recorded interaction, or error string.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest
from agents import RunContextWrapper
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from structlog.testing import capture_logs

from skyvern.cli.mcp_tools.browser import skyvern_evaluate
from skyvern.forge import app
from skyvern.forge.sdk.copilot import mcp_adapter
from skyvern.forge.sdk.copilot import runtime as copilot_runtime
from skyvern.forge.sdk.copilot import tools as tools_module
from skyvern.forge.sdk.copilot.blocker_signal import CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.mcp_adapter import SkyvernOverlayMCPServer
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    _build_request_policy_bootstrap,
    _ground_user_provided_sites,
    _seed_prior_approved_credentials,
    admit_credential_for_live_page,
)
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    CredentialOriginRecovery,
    OriginRunRedactionRegistry,
    browser_page_custody_lock,
    browser_session_recovery,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    clear_session_scrub_values,
    register_secret_scrub_value,
    scrub_secrets_from_structure,
)
from skyvern.forge.sdk.copilot.tools import credential_fill as credential_fill_module
from skyvern.forge.sdk.copilot.tools import mcp_hooks as mcp_hooks_module
from skyvern.forge.sdk.copilot.tools import scouting as scouting_module
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.schemas.credentials import CredentialType, CredentialVaultType, PasswordCredential, TotpType
from skyvern.forge.sdk.schemas.workflow_copilot import WorkflowCopilotChatHistoryMessage, WorkflowCopilotChatSender
from skyvern.library.skyvern_browser_page import SkyvernBrowserPage
from skyvern.webeye.browser_retirement import BrowserOperationRejected, BrowserRetirementReason
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.persistent_sessions_manager import BrowserOperation, BrowserRetirement
from tests.unit.conftest import make_copilot_context
from tests.unit.copilot_test_helpers import (
    SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS,
    make_copilot_ctx,
    remove_sensitive_disclosure_prerequisite,
    taint_by_terminal_run,
    wire_credential_vault,
)

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
        persisted_workflow_yaml=None,
        turn_origin=TurnOrigin.interactive,
        request_policy=_policy(),
        block_authoring_policy=BlockAuthoringPolicy.CODE_ONLY_BROWSER,
        browser_session_id="pbs_1",
        browser_session_recovery_lock=asyncio.Lock(),
        browser_session_recovery_owner=None,
        browser_session_recovery_depth=0,
        last_run_blocks_workflow_run_id=None,
        scouted_interactions=[],
        scout_trajectory=[],
        prior_carried_trajectory=[],
        carried_trajectory_rebound_done=False,
        observed_browser_urls=[],
        pending_scout_source_url=None,
        pending_taint_sources={},
        pending_scout_download_snapshot=None,
        pending_scout_download=False,
        pending_scout_download_detachers=[],
        pending_scout_popup=None,
        pending_scout_popup_content_type=None,
        pending_scout_challenge_frames=[],
        pending_scout_challenge_detachers=[],
        pending_scout_challenge_prior_frames=[],
        last_scout_act_observe_recapture_attempted=False,
        pending_scout_challenge_armed_at=None,
        pending_scout_click_pre_frame=None,
        authoring_parameter_binding_snapshot=None,
        pending_browser_interaction_observation=None,
        discovery_mcp_server=None,
        secret_scrub_values=[],
        scouted_credential_field_inventory_by_credential_id={},
        org_credentials_for_turn=None,
        vault_login_uris_by_credential_id={},
        signed_out_page_observations=[],
        signed_out_page_observation_attempts=[],
        credential_origin_recovery=None,
        blocker_signal=None,
        codeblock_redaction_parameters={},
        supports_vision=False,
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

    def test_rejects_when_code_blocks_cannot_be_authored(self) -> None:
        ctx = _ctx(block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE)
        error = tools_module._credential_fill_prerequisite_error(ctx, "cred_123")
        assert error is not None
        assert "login" in error

    def test_rejects_without_request_policy(self) -> None:
        ctx = _ctx(request_policy=None)
        assert tools_module._credential_fill_prerequisite_error(ctx, "cred_123") is not None

    def test_generic_run_flag_does_not_gate_credential_fill(self) -> None:
        ctx = _ctx(request_policy=_policy(allow_run_blocks=False))
        assert tools_module._credential_fill_prerequisite_error(ctx, "cred_123") is None

    def test_rejects_raw_secret_turn_at_credential_fill_boundary(self) -> None:
        ctx = _ctx(request_policy=_policy(raw_secret_detected=True))
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
        credential: PasswordCredential,
        *,
        name: str = "authtest simple",
    ) -> None:
        wire_credential_vault(monkeypatch, credential, credential_id="cred_123", name=name)
        monkeypatch.setattr(
            object.__getattribute__(app, "_inst"),
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
        assert value is None and isinstance(error, credential_fill_module.MissingAuthenticator)

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


class _FakeLocator:
    """Mirrors Playwright strict mode: `input_value()` is only reachable through `.first`."""

    def __init__(self, page: _FakePage, selector: str, narrowed: bool = False) -> None:
        self._page = page
        self._selector = selector
        self._narrowed = narrowed

    @property
    def first(self) -> _FakeLocator:
        return _FakeLocator(self._page, self._selector, narrowed=True)

    async def input_value(self) -> str:
        if not self._narrowed and self._page.selector_match_count > 1:
            raise RuntimeError("strict mode violation: locator resolved to 2 elements")
        return await self._page.read_value(self._selector)


class _FakePage:
    engine_selection = None

    def __init__(
        self,
        fill_error: Exception | None = None,
        url: str = _FIXTURE_LOGIN_URL,
        release_url: str | None = None,
        readback: str | None = None,
        click_error: Exception | None = None,
    ) -> None:
        self.url = url
        self.release_url = release_url
        self.fill_calls: list[tuple[Any, ...]] = []
        self.fill_kwargs: list[dict[str, Any]] = []
        self.read_calls: list[str] = []
        self.values: dict[str, str] = {}
        self.selector_match_count = 1
        self.click_calls: list[tuple[Any, ...]] = []
        self.click_kwargs: list[dict[str, Any]] = []
        self._fill_error = fill_error
        self._readback = readback
        self._click_error = click_error

    async def fill(self, *args: Any, **kwargs: Any) -> None:
        release_guard = kwargs.get("_direct_fill_release_guard")
        if release_guard is not None:
            release_guard(self.release_url if self.release_url is not None else self.url)
        self.fill_calls.append(args)
        self.fill_kwargs.append(kwargs)
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
            self.values[args[0]] = args[1]
        if self._fill_error is not None:
            raise self._fill_error

    async def read_value(self, selector: str) -> str:
        self.read_calls.append(selector)
        if self._readback is not None:
            return self._readback
        return self.values.get(selector, "")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(self, selector)

    async def click(self, *args: Any, **kwargs: Any) -> str | None:
        self.click_calls.append(args)
        self.click_kwargs.append(kwargs)
        if self._click_error is not None:
            raise self._click_error
        return args[0] if args else None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "secret_value"),
    [("totp", "123456"), ("password", "short-pass-13")],
)
async def test_the_readback_comes_from_the_page_not_the_tool_layer(
    monkeypatch: pytest.MonkeyPatch, field: str, secret_value: str
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page, secret_value=secret_value)
    ctx = _ctx()

    selector = f"#{field}"
    result = await tools_module._fill_credential_field_impl(ctx, selector, "cred_123", field)

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "exact_match"
    # Through the tool layer a registered secret returns as `[REDACTED_SECRET]`, so only a
    # read taken on the page handle that filled the field sees what was actually typed.
    assert page.read_calls == [selector]
    assert ctx.scout_trajectory[-1]["credential_field"] == field


def _fake_probe_facts(
    monkeypatch: pytest.MonkeyPatch,
    packet: dict[str, Any],
    *,
    events: list[tuple[str, str]] | None = None,
) -> None:
    async def probe(_ctx: AgentContext, selector: str, *, fingerprint: bool) -> tuple[dict[str, Any], dict[str, str]]:
        if events is not None:
            events.append(("probe", selector))
        return packet, {}

    monkeypatch.setattr(credential_fill_module, "_probe_target_facts", probe)


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
    async def fake_browser_context(_ctx: object, **_kwargs: object) -> AsyncIterator[None]:
        yield

    async def fake_get_page(session_id: str | None = None) -> tuple[_FakePage, None]:
        return page, None

    async def fake_url(_ctx: Any) -> str:
        return "https://authenticationtest.com/simpleFormAuth/"

    monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", fake_resolve)
    monkeypatch.setattr(credential_fill_module, "ensure_browser_session", fake_ensure)
    monkeypatch.setattr(credential_fill_module, "mcp_browser_context", fake_browser_context)
    monkeypatch.setattr(credential_fill_module, "get_page", fake_get_page)
    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", fake_url)
    monkeypatch.setattr(scouting_module, "_live_working_page_url", fake_url)
    _fake_probe_facts(monkeypatch, {"role_name": {"role": "textbox", "accessible_name": "Password"}})
    monkeypatch.setattr(credential_fill_module, "_authority_tool_error", lambda *a, **k: None)
    monkeypatch.setattr(credential_fill_module, "record_tool_step_result_for_ctx", lambda *a, **k: None)

    async def fake_register(*_args: object, **_kwargs: object) -> tuple[int, None]:
        return 3, None

    monkeypatch.setattr(credential_fill_module, "_register_scout_interaction_observation", fake_register)


@pytest.mark.asyncio
async def test_target_identity_is_captured_before_fill_and_effect_afterward(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class OrderedPage(_FakePage):
        async def fill(self, *args: Any, **kwargs: Any) -> None:
            events.append("fill")
            await super().fill(*args, **kwargs)

        async def read_value(self, selector: str) -> str:
            events.append("post_effect")
            return await super().read_value(selector)

    page = OrderedPage()
    _wire_impl(monkeypatch, page)

    async def probe(_ctx: AgentContext, _selector: str, *, fingerprint: bool) -> tuple[dict[str, Any], dict[str, str]]:
        events.append("probe")
        candidates = [{"selector": 'input[name="password"]', "source": "name"}]
        return {"selector_match_count": 1, "selector_candidates": candidates}, {}

    monkeypatch.setattr(credential_fill_module, "_probe_target_facts", probe)

    ctx = _ctx()
    result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

    assert result["ok"] is True
    assert events.index("probe") < events.index("fill") < events.index("post_effect")
    assert ctx.scout_trajectory[-1]["selector_match_count"] == 1
    assert ctx.scout_trajectory[-1]["observed_effects"]["value_landed"] is True
    assert ctx.scout_trajectory[-1]["selector_candidates"] == [
        {"selector": "#passwordInput", "source": "requested", "match_count": None},
        {"selector": 'input[name="password"]', "source": "name", "match_count": None},
    ]


def test_tool_layer_readback_cannot_verify_a_short_secret() -> None:
    """Why the outcome is classified at the fill site, pinned against the real scrubber: a
    registered secret returns from the tool layer as the placeholder, which no code recognises."""
    otp = "mk-one"
    ctx = SimpleNamespace(secret_scrub_values=[], browser_session_id=None)
    register_secret_scrub_value(ctx, otp)
    readback = scrub_secrets_from_structure(ctx, {"ok": True, "data": {"value": otp}})["data"]["value"]

    assert mcp_hooks_module._scout_readback_outcome(readback, otp) is mcp_hooks_module.ScoutReadbackOutcome.DIFFERENT
    assert mcp_hooks_module._scout_readback_outcome(otp, otp) is mcp_hooks_module.ScoutReadbackOutcome.EXACT_MATCH


@pytest.mark.asyncio
async def test_multi_match_selector_still_reaches_an_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading the un-narrowed locator raises Playwright strict mode, which would leave the
    field unread; the readback narrows the way the fill did."""
    page = _FakePage(readback="8675309" + "123456")
    page.selector_match_count = 2
    _wire_impl(monkeypatch, page, secret_value="123456")
    ctx = _ctx()

    result = await tools_module._fill_credential_field_impl(ctx, "input.otp", "cred_123", "totp")

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "different"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secret_value", "readback"),
    [
        ("marker-value-one", "marker-val"),
        ("alphabetagamma", "alpha beta gamma"),
        ("marker-value-longer", "marker-value-lo"),
    ],
)
async def test_a_field_not_holding_what_was_typed_is_reported_without_failing_or_claiming_a_wrong_field(
    monkeypatch: pytest.MonkeyPatch, secret_value: str, readback: str
) -> None:
    page = _FakePage(readback=readback)
    _wire_impl(monkeypatch, page, secret_value=secret_value)
    ctx = _ctx()

    result = await tools_module._fill_credential_field_impl(ctx, "#totp", "cred_123", "totp")

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "different"
    assert result["data"]["landing_inferred_from_navigation"] is False
    assert "value_landed" not in ctx.scouted_interactions[0].get("observed_effects", {})
    assert "wrong" not in json.dumps(result)
    assert "different input" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unreadable_field_records_the_fill_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable readback must not fail a fill that may well have landed, but must be visible."""

    class UnreadablePage(_FakePage):
        async def read_value(self, selector: str) -> str:
            raise RuntimeError("element is not an <input>")

    page = UnreadablePage()
    _wire_impl(monkeypatch, page, secret_value="123456")
    ctx = _ctx()

    with capture_logs() as logs:
        result = await tools_module._fill_credential_field_impl(ctx, "#totp", "cred_123", "totp")

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "unavailable"
    assert result["data"]["landing_inferred_from_navigation"] is False
    assert ctx.scout_trajectory[-1]["credential_field"] == "totp"
    assert "value_landed" not in ctx.scouted_interactions[0].get("observed_effects", {})
    assert any(
        entry.get("event") == "copilot fill_credential_field readback outcome" and entry.get("outcome") == "unavailable"
        for entry in logs
    )


@pytest.mark.asyncio
async def test_happy_path_fills_and_records_value_free(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert result["data"]["credential_name"] == "authtest simple"
    assert result["data"]["readback_outcome"] == "exact_match"
    assert result["data"]["landing_inferred_from_navigation"] is False
    assert "credential_parameter" not in result["data"]
    assert _FAKE_PASSWORD not in json.dumps(result)

    assert len(ctx.scouted_interactions) == 1
    recorded = ctx.scouted_interactions[0]
    assert recorded["tool_name"] == "fill_credential_field"
    assert recorded["credential_id"] == "cred_123"
    assert recorded["credential_field"] == "password"
    assert recorded["credential_name"] == "authtest simple"
    assert recorded["typed_length"] == len(_FAKE_PASSWORD)
    assert recorded["observed_effects"]["value_landed"] is True
    assert _FAKE_PASSWORD not in json.dumps(recorded)
    assert _FAKE_PASSWORD not in json.dumps(ctx.scout_trajectory)


@pytest.mark.asyncio
async def test_email_otp_scout_failure_returns_only_factual_credential_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)

    async def runtime_only_otp(_ctx: Any, _credential_id: str, _field: str) -> tuple[None, str, str]:
        return None, "authtest simple", "Email OTP requires workflow-run polling."

    monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", runtime_only_otp)

    result = await tools_module._fill_credential_field_impl(_ctx(), "#otp", "cred_123", "totp")

    assert result["ok"] is False
    assert result["data"] == {
        "credential_id": "cred_123",
        "credential_name": "authtest simple",
        "credential_field": "totp",
    }
    assert page.fill_calls == []


@pytest.mark.asyncio
async def test_fill_error_text_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(fill_error=RuntimeError(f"could not type {_FAKE_PASSWORD} into element"))
    _wire_impl(monkeypatch, page)

    result = await tools_module._fill_credential_field_impl(_ctx(), "#passwordInput", "cred_123", "password")

    assert result["ok"] is False
    assert _FAKE_PASSWORD not in result["error"]
    assert "[REDACTED_SECRET]" in result["error"]


@pytest.mark.asyncio
async def test_rejects_unknown_field(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)

    result = await tools_module._fill_credential_field_impl(_ctx(), "#cvv", "cred_123", "cvv")

    assert result["ok"] is False
    assert "username, password, totp" in result["error"]
    assert page.fill_calls == []


@pytest.mark.asyncio
async def test_rejects_empty_selector(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)

    result = await tools_module._fill_credential_field_impl(_ctx(), "   ", "cred_123", "password")

    assert result["ok"] is False
    assert page.fill_calls == []


@pytest.mark.asyncio
async def test_unresolved_credential_never_reaches_vault_or_page(monkeypatch: pytest.MonkeyPatch) -> None:
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
async def test_agent_blocks_only_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)
    ctx = _ctx(block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE)

    result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

    assert result["ok"] is False
    assert page.fill_calls == []


@pytest.mark.asyncio
async def test_readback_failure_surfaces_and_skips_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(readback="")
    _wire_impl(monkeypatch, page)
    ctx = _ctx()

    with capture_logs() as logs:
        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

    assert result["ok"] is False
    assert "still empty" in result["error"]
    assert result["data"]["readback_outcome"] == "empty"
    assert result["data"]["selector"] == "#passwordInput"
    assert result["data"]["typed_length"] == len(_FAKE_PASSWORD)
    assert result["data"]["landing_inferred_from_navigation"] is False
    assert ctx.scouted_interactions == []
    assert any(
        entry.get("event") == "copilot fill_credential_field readback outcome" and entry.get("outcome") == "empty"
        for entry in logs
    )


@pytest.mark.asyncio
async def test_a_field_cleared_by_its_own_submit_is_not_reported_as_a_lost_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(readback="")
    _wire_impl(monkeypatch, page, secret_value="123456")

    async def navigated(_ctx: Any) -> str:
        return _FIXTURE_LOGIN_URL + "verified/"

    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", navigated)
    ctx = _ctx()

    result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp")

    # The form committed on the last digit and cleared its own field. Calling that a lost fill
    # sends the model back to re-type a code into a page the sign-in has already left.
    assert result["ok"] is True
    assert [entry["tool_name"] for entry in ctx.scouted_interactions] == ["fill_credential_field"]
    # The readback said the field was empty. The model is told the landing was inferred, not
    # that anyone saw the value sitting there.
    assert ctx.scouted_interactions[0]["observed_effects"]["landing_inferred_from_navigation"] is True
    assert "value_landed" not in ctx.scouted_interactions[0].get("observed_effects", {})
    assert result["data"]["readback_outcome"] == "empty"
    assert result["data"]["landing_inferred_from_navigation"] is True


@pytest.mark.asyncio
async def test_a_navigation_cannot_stand_in_for_a_readback_nobody_took(monkeypatch: pytest.MonkeyPatch) -> None:
    class UnreadablePage(_FakePage):
        async def read_value(self, selector: str) -> str:
            raise RuntimeError("execution context was destroyed")

    page = UnreadablePage()
    _wire_impl(monkeypatch, page, secret_value="mk-one")

    async def navigated(_ctx: Any) -> str:
        return _FIXTURE_LOGIN_URL + "verified/"

    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", navigated)
    ctx = _ctx()

    result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp")

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "unavailable"
    assert "value_landed" not in ctx.scouted_interactions[0].get("observed_effects", {})
    assert "landing_inferred_from_navigation" not in ctx.scouted_interactions[0].get("observed_effects", {})


@pytest.mark.asyncio
async def test_a_rejected_code_re_rendering_the_same_page_is_still_a_lost_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(readback="")
    _wire_impl(monkeypatch, page, secret_value="123456")

    async def same_page_with_error(_ctx: Any) -> str:
        return _FIXTURE_LOGIN_URL + "?error=invalid"

    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", same_page_with_error)

    result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp")

    # A rejected code re-renders the same page with an error param. That is not the form having
    # carried the code away, so the empty field still means the fill did not land.
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_a_verified_fill_is_still_reported_as_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage(readback=_FAKE_PASSWORD)
    _wire_impl(monkeypatch, page)
    ctx = _ctx()

    await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

    assert "landing_inferred_from_navigation" not in ctx.scouted_interactions[0]["observed_effects"]


class TestCredentialFillInCallSubmit:
    """A one-time code ages from the moment it is minted, so the mint sits after every live-page
    probe and the submit click happens in the same call."""

    def _log_probes(self, monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, str]]) -> None:
        async def selector_matches(_ctx: Any, selector: str) -> int:
            events.append(("selector_match_count", selector))
            return 1

        async def mint(_ctx: Any, _credential_id: str, _field: str) -> tuple[str, str, None]:
            events.append(("mint", "totp"))
            return "123456", "authtest simple", None

        _fake_probe_facts(
            monkeypatch,
            {
                "role_name": {"role": "button", "accessible_name": "Verify"},
                "selector_match_count": 1,
                "role_name_match_count": 1,
            },
            events=events,
        )
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", selector_matches)
        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", mint)

    @pytest.mark.asyncio
    async def test_both_targets_are_probed_before_the_code_is_minted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list[tuple[str, str]] = []

        class OrderedPage(_FakePage):
            async def fill(self, *args: Any, **kwargs: Any) -> None:
                events.append(("fill", str(args[0])))
                await super().fill(*args, **kwargs)

            async def click(self, *args: Any, **kwargs: Any) -> str | None:
                events.append(("click", str(args[0])))
                return await super().click(*args, **kwargs)

        page = OrderedPage()
        _wire_impl(monkeypatch, page)
        self._log_probes(monkeypatch, events)

        result = await tools_module._fill_credential_field_impl(
            _ctx(), "#totpCode", "cred_123", "totp", "#verifyButton"
        )

        assert result["ok"] is True
        names = [name for name, _ in events]
        mint_at = names.index("mint")
        assert events[:mint_at] == [("probe", "#totpCode"), ("probe", "#verifyButton")]
        assert names[mint_at:] == ["mint", "fill", "selector_match_count", "click"]
        assert events[-2] == ("selector_match_count", "#verifyButton")

    @pytest.mark.asyncio
    async def test_supplied_submit_selector_is_clicked_once_and_recorded_after_the_fill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert page.click_calls == [("#verifyButton",)]
        assert page.click_kwargs[0]["mode"] == "direct"
        assert result["data"]["submit_selector"] == "#verifyButton"
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field", "click"]
        assert ctx.scout_trajectory[-1]["selector"] == "#verifyButton"
        assert ctx.scout_trajectory[-1]["role"] == "textbox"
        assert "123456" not in json.dumps(ctx.scout_trajectory)

    @pytest.mark.asyncio
    async def test_a_field_not_holding_what_was_typed_is_not_submitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Submitting a code the field does not hold voids it, so it must not reach the submit click."""

        page = _FakePage(readback="prior-text-marker-value-one")
        _wire_impl(monkeypatch, page, secret_value="marker-value-one")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert result["data"]["readback_outcome"] == "different"
        assert result["data"]["submitted"] is False
        assert page.click_calls == []
        assert "submit_skipped" in result["data"]
        assert "marker-value-one" not in json.dumps(result)

    @pytest.mark.asyncio
    async def test_a_password_field_that_reformats_the_value_still_submits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A page that trims or reformats what it accepts reads back different on a fill that landed,
        and nothing a sign-in can void is at stake, so the in-call submit still runs."""

        page = _FakePage(readback=" marker-value-two ")
        _wire_impl(monkeypatch, page, secret_value="marker-value-two")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#password", "cred_123", "password", "#signIn")

        assert result["ok"] is True
        assert result["data"]["readback_outcome"] == "different"
        assert page.click_calls == [("#signIn",)]
        assert result["data"]["submitted"] is True

    @pytest.mark.asyncio
    async def test_no_submit_selector_leaves_the_page_unclicked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp")

        assert result["ok"] is True
        assert page.click_calls == []
        assert page.click_kwargs == []
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field"]
        assert [entry["tool_name"] for entry in ctx.scouted_interactions] == ["fill_credential_field"]
        assert "submit_selector" not in result["data"]

    @pytest.mark.asyncio
    async def test_leaving_the_granted_origin_after_the_fill_skips_the_submit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        async def navigated_away(_ctx: Any) -> str:
            return "https://elsewhere.example.com/collect"

        monkeypatch.setattr(credential_fill_module, "_live_working_page_url", navigated_away)

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert page.click_calls == []
        assert "submit_skipped" in result["data"]
        # The fill already landed here, so the notice must not send the model back to fill again —
        # that would mint and type a second live code onto whatever page the browser moved to.
        notice = result["data"]["submit_skipped"]
        assert "was filled" in notice
        assert "before it could be filled" not in notice
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field"]

    @pytest.mark.asyncio
    async def test_a_submit_control_that_vanished_is_reported_as_already_submitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        # The pre-fill probe comes back unreadable here: an explicit zero at dispatch still
        # means the control is gone, whatever the earlier read could or could not see.
        async def gone(_ctx: AgentContext, _selector: str) -> int:
            return 0

        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", gone)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#verify")

        assert result["ok"] is True
        assert page.click_calls == []
        assert "submit_error" not in result["data"]
        assert "may already have been submitted" in result["data"]["submit_skipped"]

    @pytest.mark.asyncio
    async def test_a_selector_that_never_matched_is_not_called_an_already_submitted_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def never_matched(_ctx: Any, _selector: str) -> int:
            return 0

        _fake_probe_facts(monkeypatch, {"selector_match_count": 0})
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", never_matched)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#nope")

        # Zero before the fill AND after it is a wrong selector. Reporting a login that never
        # happened sends the model away while the code it just minted ages out.
        assert page.click_calls == []
        notice = result["data"]["submit_skipped"]
        assert "#nope" in notice
        assert "already been submitted" not in notice

    @pytest.mark.asyncio
    async def test_an_in_call_submit_still_observes_the_fill_under_its_own_tool_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        observed: list[tuple[str, str]] = []

        async def record_observation(
            _ctx: Any, *, tool_name: str, selector: str, source_url: str, url: str
        ) -> tuple[int, None]:
            observed.append((tool_name, selector))
            return 3, None

        monkeypatch.setattr(credential_fill_module, "_register_scout_interaction_observation", record_observation)

        await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#verifyButton")

        assert observed == [("fill_credential_field", "#totpCode"), ("click", "#verifyButton")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("submit_selector", "submit_role_name"),
        [("#verifyButton", ("", "")), ('role=button[name="Verify"]', ("button", "Verify"))],
    )
    async def test_a_hung_probe_read_drops_every_fact_and_still_submits(
        self, monkeypatch: pytest.MonkeyPatch, submit_selector: str, submit_role_name: tuple[str, str]
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        monkeypatch.setattr(credential_fill_module, "_probe_target_facts", scouting_module._probe_target_facts)
        monkeypatch.setattr(scouting_module, "_DISCOVERY_PER_CALL_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(scouting_module, "_PRE_NAVIGATION_ROLE_NAME_TIMEOUT_SECONDS", 0.05)

        async def hang(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            await asyncio.Event().wait()
            return {}

        stale = [{"selector": "#stale", "source": "id", "match_count": 1}]
        ctx = _ctx(
            discovery_mcp_server=SimpleNamespace(call_internal_tool=hang), pending_scout_selector_candidates=stale
        )

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", submit_selector)

        assert result["data"]["submitted"] is True
        assert page.click_calls == [(submit_selector,)]
        assert ctx.pending_scout_selector_candidates is None
        fill_record, submit_record = ctx.scout_trajectory
        assert not any(key.startswith("element_fingerprint") for key in fill_record)
        assert not fill_record.get("role")
        assert (submit_record.get("role", ""), submit_record.get("accessible_name", "")) == submit_role_name
        for record, selector in ((fill_record, "#totpCode"), (submit_record, submit_selector)):
            assert record.get("selector_match_count") is None
            assert record.get("role_name_match_count") is None
            assert record["selector_candidates"] == [{"selector": selector, "source": "requested", "match_count": None}]

    @pytest.mark.asyncio
    async def test_an_unreadable_match_count_still_submits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def unreadable(_ctx: AgentContext, _selector: str) -> None:
            return None

        _fake_probe_facts(monkeypatch, {"selector_match_count": 1})
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", unreadable)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#verify")

        # None means the page could not be read, not that the control is gone. Treating it as gone
        # would strand a fresh code and hand the expiry problem back to the next turn.
        assert page.click_calls == [("#verify",)]
        assert "submit_skipped" not in result["data"]

    @pytest.mark.asyncio
    async def test_an_ambiguous_submit_selector_is_not_clicked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def two_matches(_ctx: Any, _selector: str) -> int:
            return 2

        _fake_probe_facts(monkeypatch, {"selector_match_count": 2})
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", two_matches)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "button.x")

        # A direct click takes .first, and next to a submit control that is often "Resend code",
        # which would void the code just typed. Guessing is worse than declining.
        assert page.click_calls == []
        assert result["data"]["submitted"] is False
        assert "matches 2 controls" in result["data"]["submit_skipped"]

    @pytest.mark.asyncio
    async def test_a_selector_that_never_matched_is_not_clicked_when_the_dispatch_read_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def unreadable(_ctx: AgentContext, _selector: str) -> None:
            return None

        _fake_probe_facts(monkeypatch, {"selector_match_count": 0})
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", unreadable)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#nope")

        # The probe already saw the selector match nothing; an unreadable dispatch read is not a
        # reason to spend the click timeout on it and report a failure.
        assert page.click_calls == []
        assert "#nope" in result["data"]["submit_skipped"]

    @pytest.mark.asyncio
    async def test_an_ambiguous_selector_is_declined_even_when_the_dispatch_read_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def unreadable(_ctx: AgentContext, _selector: str) -> None:
            return None

        _fake_probe_facts(monkeypatch, {"selector_match_count": 2})
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", unreadable)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "button.x")

        # An unreadable count at dispatch does not unsee what the pre-fill probe already counted, and
        # "Resend code" is just as adjacent whichever read spotted the second control.
        assert page.click_calls == []
        assert "matches 2 controls" in result["data"]["submit_skipped"]

    @pytest.mark.asyncio
    async def test_a_failed_submit_click_never_costs_the_fill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage(click_error=RuntimeError("no element matched #verifyButton for 123456"))
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert result["data"]["typed_length"] == 6
        assert "123456" not in json.dumps(result)
        assert "[REDACTED_SECRET]" in result["data"]["submit_error"]
        # The click raised, so whether it reached the page is unknown. Saying only "not submitted"
        # would read as safe to retry, and a retry spends a second code.
        assert result["data"]["submitted"] is False
        assert result["data"]["submit_uncertain"] is True
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field"]

    @pytest.mark.asyncio
    async def test_a_form_that_committed_itself_is_not_clicked_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage(readback="")
        _wire_impl(monkeypatch, page, secret_value="123456")

        async def navigated(_ctx: Any) -> str:
            return _FIXTURE_LOGIN_URL + "verified/"

        monkeypatch.setattr(credential_fill_module, "_live_working_page_url", navigated)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#totpCode", "cred_123", "totp", "#verify")

        # The probed control belongs to the page the fill left, so clicking now acts on a different one.
        assert page.click_calls == []
        assert result["data"]["submitted"] is False
        assert "submitted itself" in result["data"]["submit_skipped"]

    @pytest.mark.asyncio
    async def test_a_navigating_submit_is_not_reported_as_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _FakePage(
            click_error=PlaywrightTimeoutError("Timeout 5000ms exceeded waiting for scheduled navigations")
        )
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert "submit_error" not in result["data"]
        assert result["data"]["submit_selector"] == "#verifyButton"
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field", "click"]

    @pytest.mark.asyncio
    async def test_an_unverified_landing_still_submits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An auto-submitting 2FA form is the canonical unreadable readback, and it is exactly the
        form whose submit still has to be clicked, so `unknown` proceeds like `landed`."""

        class UnreadablePage(_FakePage):
            async def read_value(self, selector: str) -> str:
                raise RuntimeError("execution context was destroyed")

        page = UnreadablePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#totpCode", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is True
        assert page.click_calls == [("#verifyButton",)]
        assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field", "click"]

    @pytest.mark.asyncio
    async def test_the_reported_duration_spans_only_the_mint_to_the_click(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = {"now": 0.0}

        def advance(seconds: float) -> None:
            clock["now"] += seconds

        class TimedPage(_FakePage):
            async def fill(self, *args: Any, **kwargs: Any) -> None:
                advance(0.5)
                await super().fill(*args, **kwargs)

            async def click(self, *args: Any, **kwargs: Any) -> str | None:
                advance(0.75)
                return await super().click(*args, **kwargs)

        page = TimedPage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        monkeypatch.setattr(credential_fill_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

        async def slow_probe(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], dict[str, str]]:
            advance(10.0)
            return {"selector_match_count": 1}, {}

        async def slow_live_count(_ctx: AgentContext, _selector: str) -> int:
            advance(10.0)
            return 1

        async def slow_readback(_page: Any, _selector: str) -> str:
            advance(0.25)
            return "123456"

        url_reads = {"count": 0}

        async def counted_url(_ctx: Any) -> str:
            url_reads["count"] += 1
            advance(0.1 if url_reads["count"] == 1 else 100.0)
            return _FIXTURE_LOGIN_URL

        monkeypatch.setattr(credential_fill_module, "_probe_target_facts", slow_probe)
        monkeypatch.setattr(credential_fill_module, "_selector_live_match_count", slow_live_count)
        monkeypatch.setattr(credential_fill_module, "_read_filled_field_value", slow_readback)
        monkeypatch.setattr(credential_fill_module, "_live_working_page_url", counted_url)

        with capture_logs() as logs:
            result = await tools_module._fill_credential_field_impl(
                _ctx(), "#totpCode", "cred_123", "totp", "#verifyButton"
            )

        assert result["ok"] is True
        filled = next(entry for entry in logs if "filled a saved credential field" in entry["event"])
        # 10s of it is the pre-click check that the submit control is still there. The two pre-mint
        # probes (10s each) and the 100s post-click URL read stay outside the window.
        assert filled["totp_mint_to_submit_ms"] == 11600

    @pytest.mark.asyncio
    async def test_an_unresolvable_credential_still_returns_its_identity_and_never_submits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)

        async def runtime_only_otp(_ctx: Any, _credential_id: str, _field: str) -> tuple[None, str, str]:
            return None, "authtest simple", "Email OTP requires workflow-run polling."

        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", runtime_only_otp)

        result = await tools_module._fill_credential_field_impl(_ctx(), "#otp", "cred_123", "totp", "#verifyButton")

        assert result["ok"] is False
        assert result["data"] == {
            "credential_id": "cred_123",
            "credential_name": "authtest simple",
            "credential_field": "totp",
        }
        assert page.fill_calls == []
        assert page.click_calls == []


@pytest.mark.asyncio
async def test_in_flight_sensitive_taint_suppresses_fill_result_screenshot_and_recorded_page_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)
    ctx = _ctx()
    existing_interaction = {"tool_name": "click", "selector": "#existing"}
    existing_trajectory = {**existing_interaction, "trajectory_index": 0}
    existing_flow = {"step": 1, "evidence": {"source_tool": "existing"}}
    ctx.scouted_interactions = [existing_interaction]
    ctx.scout_trajectory = [existing_trajectory]
    ctx.flow_evidence = [existing_flow]
    ctx.scouted_output_covered_paths = {"output.existing"}
    ctx.scout_observation_contract = {"existing": True}
    ctx.pending_browser_interaction_observation = SimpleNamespace(tool_name="click", url="https://existing.test")
    unrelated_commit: asyncio.Task[None] | None = None

    async def append_unrelated_evidence() -> None:
        async with browser_page_custody_lock(ctx):
            ctx.scouted_interactions.append({"tool_name": "click", "selector": "#parallel"})
            ctx.scout_trajectory.append({"tool_name": "click", "selector": "#parallel", "trajectory_index": 1})
            ctx.flow_evidence.append({"step": 2, "evidence": {"source_tool": "parallel"}})

    async def taint_before_screenshot(*_args: Any, **_kwargs: Any) -> bool:
        nonlocal unrelated_commit
        unrelated_commit = asyncio.create_task(append_unrelated_evidence())
        await asyncio.sleep(0)
        assert not unrelated_commit.done()
        ctx.sensitive_origin_browser_session_ids = {"pbs_1"}
        return False

    screenshot = AsyncMock(side_effect=taint_before_screenshot)
    monkeypatch.setattr(credential_fill_module, "_capture_post_interaction_screenshot", screenshot)

    result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")
    assert unrelated_commit is not None
    await unrelated_commit

    assert result["ok"] is False
    assert "specific named URL" in result["error"]
    screenshot.assert_awaited_once()
    assert ctx.scouted_interactions == [existing_interaction, {"tool_name": "click", "selector": "#parallel"}]
    assert ctx.scout_trajectory == [
        existing_trajectory,
        {"tool_name": "click", "selector": "#parallel", "trajectory_index": 1},
    ]
    assert ctx.flow_evidence == [existing_flow, {"step": 2, "evidence": {"source_tool": "parallel"}}]
    assert ctx.scouted_output_covered_paths == {"output.existing"}
    assert ctx.scout_observation_contract == {"existing": True}
    assert ctx.pending_browser_interaction_observation == SimpleNamespace(
        tool_name="click", url="https://existing.test"
    )


class TestPublicToolCall:
    @pytest.mark.asyncio
    async def test_the_public_tool_forwards_the_submit_selector_and_serializes_the_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="123456")
        ctx = RunContextWrapper(_ctx())
        # Supplied by the agent runner in production; without it the SDK's own invoke path raises.
        ctx.tool_name = "fill_credential_field"

        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        payload = await tool.on_invoke_tool(
            ctx,
            json.dumps(
                {
                    "selector": "#totpCode",
                    "credential_id": "cred_123",
                    "field": "totp",
                    "submit_selector": "#verifyButton",
                }
            ),
        )

        # The behaviour tests drive the private impl, so nothing else proves the selector survives
        # the model's own call path, or that the secret never reaches the serialized result.
        result = json.loads(payload)
        assert page.click_calls == [("#verifyButton",)]
        assert result["ok"] is True
        assert result["data"]["submitted"] is True
        assert result["data"]["submit_selector"] == "#verifyButton"
        assert "123456" not in payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("target", "acting_session"), [("debug", "pbs_debug"), ("last_run", "pbs_run")])
    async def test_the_fill_and_its_submit_land_in_the_browser_the_call_named(
        self, monkeypatch: pytest.MonkeyPatch, target: str, acting_session: str
    ) -> None:
        """A run that minted its own browser leaves two pages on the same login form. A `last_run`
        cell followed by a fill that silently acts on the chat's browser submits a credential there."""
        pages = {"pbs_debug": _FakePage(), "pbs_run": _FakePage()}
        _wire_impl(monkeypatch, pages["pbs_debug"], secret_value="123456")
        provisioned: list[bool] = []

        async def get_page_for(session_id: str | None = None) -> tuple[_FakePage, None]:
            return pages[session_id or ""], None

        async def ensure(_ctx: object) -> None:
            provisioned.append(True)

        monkeypatch.setattr(credential_fill_module, "get_page", get_page_for)
        monkeypatch.setattr(credential_fill_module, "ensure_browser_session", ensure)
        ctx = RunContextWrapper(_ctx(browser_session_id="pbs_debug", last_run_blocks_browser_session_id="pbs_run"))
        ctx.tool_name = "fill_credential_field"

        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        payload = await tool.on_invoke_tool(
            ctx,
            json.dumps(
                {
                    "selector": "#totpCode",
                    "credential_id": "cred_123",
                    "field": "totp",
                    "submit_selector": "#verifyButton",
                    "target": target,
                }
            ),
        )

        result = json.loads(payload)
        idle_session = next(s for s in pages if s != acting_session)
        assert result["ok"] is True and result["browser_target"] == target
        assert pages[acting_session].fill_calls and pages[acting_session].click_calls == [("#verifyButton",)]
        assert not pages[idle_session].fill_calls and not pages[idle_session].click_calls
        assert provisioned == ([True] if target == "debug" else [])

    @pytest.mark.asyncio
    async def test_missing_authenticator_points_to_the_update_ask_in_the_browser_the_call_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_resolve = credential_fill_module._resolve_credential_fill_value
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", real_resolve)
        wire_credential_vault(
            monkeypatch,
            PasswordCredential(username=_FAKE_USERNAME, password=_FAKE_PASSWORD, totp=None),
            credential_id="cred_123",
            name="authtest simple",
        )
        ctx = RunContextWrapper(_ctx(browser_session_id="pbs_debug", last_run_blocks_browser_session_id="pbs_run"))
        ctx.tool_name = "fill_credential_field"

        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        payload = await tool.on_invoke_tool(
            ctx,
            json.dumps({"selector": "#totpCode", "credential_id": "cred_123", "field": "totp", "target": "last_run"}),
        )

        result = json.loads(payload)
        assert (result["ok"], result["status"], result["browser_target"]) == (
            False,
            credential_fill_module.MISSING_AUTHENTICATOR,
            "last_run",
        )
        assert result["data"] == {
            "credential_id": "cred_123",
            "credential_name": "authtest simple",
            "credential_field": "totp",
        }
        assert result["update_ask"] == "available"
        assert not page.fill_calls

    @pytest.mark.parametrize("already_asked", [False, True])
    def test_missing_authenticator_steers_to_the_update_ask_only_once(self, already_asked: bool) -> None:
        ctx = make_copilot_context()
        ctx.credential_totp_update_asked = already_asked

        result = credential_fill_module._missing_authenticator_fill_error(ctx, "cred_123", "authtest simple")

        assert result["update_ask"] == ("already_asked" if already_asked else "available")

    @pytest.mark.asyncio
    async def test_a_credential_short_enough_to_sit_inside_an_outcome_word_cannot_forge_another_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole result crosses the substring scrubber, which has no length floor, so a credential
        that is itself a substring of an outcome word redacts part of it — visibly, never into another
        outcome the model would act on."""
        page = _FakePage()
        _wire_impl(monkeypatch, page, secret_value="act")
        copilot_ctx = _ctx(browser_session_id=None)
        register_secret_scrub_value(copilot_ctx, "act")
        ctx = RunContextWrapper(copilot_ctx)
        ctx.tool_name = "fill_credential_field"

        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        payload = await tool.on_invoke_tool(
            ctx,
            json.dumps({"selector": "#totpCode", "credential_id": "cred_123", "field": "totp"}),
        )

        result = json.loads(payload)
        outcome = result["data"]["readback_outcome"]
        assert result["ok"] is True
        assert outcome == "ex[REDACTED_SECRET]_match"
        assert outcome not in {"exact_match", "empty", "different", "unavailable"}


class TestToolRegistration:
    def test_tool_is_registered_native(self) -> None:
        names = [tool.name for tool in tools_module.NATIVE_TOOLS]
        assert "fill_credential_field" in names

    def test_tool_description_states_value_free_contract(self) -> None:
        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        description = tool.description or ""
        assert "server-side" in description
        assert "never" in description
        assert "rather than typing the value yourself" in description
        assert "code_artifact_metadata.input_bindings" in description
        assert "credential_parameter.key" not in description
        assert "credential_parameter.otp_accessor" not in description

    def test_tool_description_no_longer_promises_it_never_submits(self) -> None:
        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        description = tool.description or ""
        assert "only fills; it never clicks or submits" not in description
        assert "submit_selector" in description

    def test_optional_submit_selector_reaches_the_model_schema(self) -> None:
        tool = next(t for t in tools_module.NATIVE_TOOLS if t.name == "fill_credential_field")
        schema = tool.params_json_schema
        assert "submit_selector" in schema["properties"]
        assert "submit_selector" not in schema.get("required", [])


def _org_credential(
    credential_id: str,
    name: str,
    tested_url: str | None,
    credential_type: CredentialType = CredentialType.PASSWORD,
    *,
    totp_type: TotpType = TotpType.NONE,
) -> SimpleNamespace:
    return SimpleNamespace(
        credential_id=credential_id,
        name=name,
        tested_url=tested_url,
        credential_type=credential_type,
        totp_type=totp_type,
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
            block_authoring_policy=BlockAuthoringPolicy.TASK_V3_PURE,
        )

        assert error is not None
        load_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generic_run_flag_does_not_gate_live_page_credential_evidence(self) -> None:
        error, policy, load_mock = await self._gate(
            credential_id="cred_analytics",
            page_url="https://analytics.example.com/login",
            org_credentials=[_org_credential("cred_analytics", "analytics", "https://analytics.example.com/login")],
            policy=RequestPolicy(allow_run_blocks=False),
        )

        assert error is None
        assert [credential.credential_id for credential in policy.resolved_credentials] == ["cred_analytics"]
        load_mock.assert_awaited_once()

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
    async def test_a_fill_refused_on_another_origin_opens_recovery_carrying_only_that_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage(
            url=_FIXTURE_LOGIN_URL,
            release_url="https://idp.example.test/oauth/authorize?state=opaque-state#login_hint=someone",
        )
        _wire_impl(monkeypatch, page)
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert result["recovery"] == {
            "observed_origin": "https://idp.example.test",
            "next_action": "request_credential",
        }
        assert page.fill_calls == []
        assert ctx.credential_origin_recovery == CredentialOriginRecovery(
            "https://idp.example.test", "pending", refused_credential_id="cred_123"
        )
        signal = ctx.blocker_signal
        assert signal.internal_reason_code == CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE
        assert signal.recovery_hint == "ask_user_clarifying"
        assert signal.renders_final_reply is False
        assert dict(signal.extra) == {"observed_origin": "https://idp.example.test"}
        serialized = json.dumps([signal.model_dump(mode="json"), result])
        assert "opaque-state" not in serialized
        assert "login_hint" not in serialized
        assert "/oauth/authorize" not in serialized
        assert _FAKE_PASSWORD not in serialized

    @pytest.mark.asyncio
    async def test_a_refusal_on_a_second_origin_keeps_the_pending_recovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage(url=_FIXTURE_LOGIN_URL, release_url="https://idp.example.test/login")
        _wire_impl(monkeypatch, page)
        first = CredentialOriginRecovery("https://first-idp.example.test", "pending", refused_credential_id="cred_123")
        ctx = _ctx(credential_origin_recovery=first)

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result["ok"] is False
        assert result["error"] == credential_fill_module._credential_origin_recovery_error(first)
        assert ctx.credential_origin_recovery == first
        assert ctx.blocker_signal is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("release_url", ["about:blank", "https://sso.authenticationtest.com/login"])
    async def test_a_refusal_off_another_site_keeps_the_retry_hint_and_opens_no_recovery(
        self, monkeypatch: pytest.MonkeyPatch, release_url: str
    ) -> None:
        page = _FakePage(url=_FIXTURE_LOGIN_URL, release_url=release_url)
        _wire_impl(monkeypatch, page)
        ctx = _ctx()

        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")

        assert result == {"ok": False, "error": credential_fill_module._credential_fill_origin_mismatch_error()}
        assert ctx.credential_origin_recovery is None
        assert ctx.blocker_signal is None

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
        assert "request_credential" in result["error"]
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
        assert "request_credential" in error

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
        assert "request_credential" in error

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
    async def test_parallel_credential_fills_are_serialized_per_copilot_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = _FakePage()
        _wire_impl(monkeypatch, page)
        ctx = _ctx()
        active_resolutions = 0
        maximum_active_resolutions = 0

        async def resolve(_ctx: Any, _credential_id: str, _field: str) -> tuple[str, str, None]:
            nonlocal active_resolutions, maximum_active_resolutions
            active_resolutions += 1
            maximum_active_resolutions = max(maximum_active_resolutions, active_resolutions)
            await asyncio.sleep(0)
            active_resolutions -= 1
            return _FAKE_PASSWORD, "analytics", None

        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", resolve)

        username, password = await asyncio.gather(
            tools_module._fill_credential_field_impl(ctx, "#user", "cred_123", "username"),
            tools_module._fill_credential_field_impl(ctx, "#pass", "cred_123", "password"),
        )

        assert username["ok"] is True
        assert password["ok"] is True
        assert maximum_active_resolutions == 1

    @pytest.mark.asyncio
    async def test_direct_credential_fill_waits_for_source_promotion_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _ctx(browser_session_recovery_lock=asyncio.Lock())
        provisioning_started = asyncio.Event()

        async def grant(*_args: object, **_kwargs: object) -> tuple[SimpleNamespace, None]:
            return SimpleNamespace(), None

        async def provision(provision_ctx: object, **_kwargs: object) -> dict[str, object]:
            async with browser_session_recovery(provision_ctx):
                provisioning_started.set()
                return {"ok": False, "error": "stop after provisioning"}

        monkeypatch.setattr(credential_fill_module, "_credential_fill_origin_grant", grant)
        monkeypatch.setattr(credential_fill_module, "ensure_browser_session", provision)

        async with ctx.browser_session_recovery_lock:
            fill = asyncio.create_task(
                tools_module._fill_credential_field_impl(ctx, "#password", "cred_123", "password")
            )
            await asyncio.sleep(0)
            assert provisioning_started.is_set() is False
            assert fill.done() is False

        assert await fill == {"ok": False, "error": "stop after provisioning"}
        assert provisioning_started.is_set() is True

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
    async def test_live_page_resolution_surfaces_secret_safe_totp_metadata(self) -> None:
        policy = RequestPolicy()
        ctx = _ctx(request_policy=policy)
        fake_totp_identifier = "fake-otp-channel@example.test"
        fake_current_otp = "987654"
        credential = _org_credential(
            "cred_analytics",
            "analytics",
            _FIXTURE_LOGIN_URL,
            totp_type=TotpType.AUTHENTICATOR,
        )
        credential.username = _FAKE_USERNAME
        credential.password = _FAKE_PASSWORD
        credential.totp = _FAKE_TOTP_SEED
        credential.totp_identifier = fake_totp_identifier
        credential.current_otp = fake_current_otp

        result, _ = await self._observe_navigate(
            ctx,
            _FIXTURE_LOGIN_URL,
            [credential],
        )

        assert result["resolved_login_credential_totp_type"] == "authenticator"
        serialized = json.dumps(result)
        assert "tested_url" not in serialized
        assert _FAKE_USERNAME not in serialized
        assert _FAKE_PASSWORD not in serialized
        assert _FAKE_TOTP_SEED not in serialized
        assert fake_totp_identifier not in serialized
        assert fake_current_otp not in serialized

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
    async def test_generic_run_flag_does_not_suppress_live_page_observation(self) -> None:
        policy = RequestPolicy(allow_run_blocks=False)
        ctx = _ctx(request_policy=policy)
        reread = AsyncMock(return_value=(_FIXTURE_LOGIN_URL, ""))

        with patch.object(mcp_hooks_module, "_fallback_page_info", reread):
            await mcp_hooks_module._bind_login_credential_for_observed_url(ctx, "current_page", {"ok": True})

        reread.assert_awaited_once()

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


_RUN_OTP = "424242"
_RUN_PASSWORD = "Sp1r!t-Level-2026"
_LANDED_URL_WITH_OTP = f"{_FIXTURE_LOGIN_URL}?code={_RUN_OTP}"


def _terminal_credential_run_ctx(**overrides: Any) -> SimpleNamespace:
    ctx = _ctx(**overrides)
    ctx.flow_evidence = []
    ctx.composition_page_evidence = None
    clear_session_scrub_values(ctx.browser_session_id)
    ctx.last_run_blocks_workflow_run_id = "wr_credential"
    ctx.last_run_blocks_browser_session_id = ctx.browser_session_id
    taint_by_terminal_run(ctx, workflow_run_id="wr_credential", session_id=ctx.browser_session_id)
    ctx.origin_run_redaction_registry = OriginRunRedactionRegistry(
        workflow_run_id="wr_credential",
        parameters={"password": _RUN_PASSWORD, "totp": _RUN_OTP},
        contains_sensitive_values=True,
        contains_all_sensitive_values=True,
    )
    return ctx


def _echo_run_secret_in_probe_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_url(_ctx: Any) -> str:
        return _LANDED_URL_WITH_OTP

    _fake_probe_facts(
        monkeypatch,
        {"role_name": {"role": "textbox", "accessible_name": f"One-time code {_RUN_OTP} for {_RUN_PASSWORD}"}},
    )
    monkeypatch.setattr(credential_fill_module, "_live_working_page_url", fake_url)
    monkeypatch.setattr(scouting_module, "_live_working_page_url", fake_url)


@pytest.mark.asyncio
async def test_the_authorized_fill_and_submit_run_on_the_page_a_terminal_credential_run_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page, secret_value=_RUN_OTP)
    _echo_run_secret_in_probe_facts(monkeypatch)
    ctx = _terminal_credential_run_ctx()

    result = await tools_module._fill_credential_field_impl(ctx, "#token", "cred_123", "totp", "#verifyButton")

    assert result["ok"] is True
    assert result["data"]["readback_outcome"] == "exact_match"
    assert page.read_calls == ["#token"]
    assert page.click_calls == [("#verifyButton",)]
    assert result["data"]["submitted"] is True
    assert [entry["tool_name"] for entry in ctx.scout_trajectory] == ["fill_credential_field", "click"]
    retained = json.dumps(
        [ctx.scout_trajectory, ctx.scouted_interactions, ctx.flow_evidence, ctx.composition_page_evidence]
    )
    assert _RUN_OTP not in retained
    assert _RUN_PASSWORD not in retained
    assert REDACTED_SECRET_PLACEHOLDER in retained
    model_facing = json.dumps(scrub_secrets_from_structure(ctx, result))
    assert _RUN_OTP not in model_facing
    assert _RUN_PASSWORD not in model_facing


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", SENSITIVE_DISCLOSURE_WITHHOLDING_ARMS)
async def test_the_fill_is_refused_on_the_same_page_when_a_prerequisite_is_absent(
    monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page, secret_value=_RUN_OTP)
    ctx = _terminal_credential_run_ctx()
    remove_sensitive_disclosure_prerequisite(ctx, arm)

    result = await tools_module._fill_credential_field_impl(ctx, "#token", "cred_123", "totp", "#verifyButton")

    assert result["ok"] is False
    assert result["error"] == SENSITIVE_ORIGIN_PAGE_ERROR
    assert page.fill_calls == []
    assert page.click_calls == []


@pytest.mark.asyncio
async def test_a_terminal_credential_run_does_not_authorize_a_credential_without_an_origin_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page, secret_value=_RUN_OTP)
    ctx = _terminal_credential_run_ctx(request_policy=RequestPolicy(resolved_credentials=[]))

    result = await tools_module._fill_credential_field_impl(ctx, "#token", "cred_unbound", "totp", "#verifyButton")

    assert result["ok"] is False
    assert result["error"] != SENSITIVE_ORIGIN_PAGE_ERROR
    assert page.fill_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin_arm",
    [
        "saved_login",
        "vault",
        "tested",
        "wrong_origin",
        "unavailable",
        "canvas_only",
        "ambiguous_authority",
        "saved_other_user_site",
        "saved_other_named",
        "saved_other_approved",
    ],
)
async def test_saved_workflow_login_fills_without_repeating_the_credential_choice(
    monkeypatch: pytest.MonkeyPatch,
    origin_arm: str,
) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)
    credential = _org_credential("cred_123", "authtest simple", _FIXTURE_LOGIN_URL if origin_arm == "tested" else None)
    saved_yaml = f"""
workflow_definition:
  parameters:
    - parameter_type: credential
      key: login
      credential_id: cred_123
  blocks:
    - block_type: login
      label: login
      url: {_FIXTURE_LOGIN_URL if origin_arm == "saved_login" else "https://public.example.org/book"}
      parameter_keys: [login]
"""
    if origin_arm == "ambiguous_authority":
        saved_yaml = saved_yaml.replace(
            "https://public.example.org/book", r"https://trusted.example\@authenticationtest.com/simpleFormAuth/"
        )
    policy = await _build_request_policy_bootstrap(
        user_message="Run the whole workflow again",
        workflow_yaml=saved_yaml.replace("https://public.example.org/book", _FIXTURE_LOGIN_URL),
        persisted_workflow_yaml=saved_yaml if origin_arm != "canvas_only" else None,
        chat_history=[],
        global_llm_context="",
        organization_id="o_1",
    )
    ctx = make_copilot_context()
    ctx.organization_id = "o_1"
    ctx.browser_session_id = "pbs_1"
    ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
    ctx.request_policy = policy
    ctx.persisted_workflow_yaml = saved_yaml if origin_arm != "canvas_only" else None
    ctx.vault_login_uris_by_credential_id = {"cred_123": [_FIXTURE_LOGIN_URL] if origin_arm == "vault" else []}
    if origin_arm == "wrong_origin":
        credential.tested_url = "https://wrong.example.net/login"
    candidates = [credential]
    if origin_arm in {"saved_other_user_site", "saved_other_named", "saved_other_approved"}:
        policy.user_provided_site_urls = [_FIXTURE_LOGIN_URL]
        candidates.append(_org_credential("cred_other", "other login", _FIXTURE_LOGIN_URL))
        if origin_arm == "saved_other_named":
            policy.current_turn_named_credential_ids = {"cred_123"}
        if origin_arm == "saved_other_approved":
            with patch(
                "skyvern.forge.sdk.copilot.request_policy.app.DATABASE.credentials.get_credentials_by_ids",
                AsyncMock(return_value=[credential]),
            ):
                await _seed_prior_approved_credentials(
                    policy,
                    organization_id="o_1",
                    global_llm_context=json.dumps({"approved_credentials": [{"credential_id": "cred_123"}]}),
                )
    with patch.object(credential_fill_module, "load_credentials", AsyncMock(return_value=candidates)):
        result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")
    allowed = origin_arm in {"saved_login", "vault", "tested", "saved_other_named", "saved_other_approved"}
    assert result["ok"] is allowed
    assert bool(page.fill_calls) is allowed
    assert _FAKE_PASSWORD not in json.dumps(result)


@pytest.mark.asyncio
async def test_unresolved_fill_names_the_in_chat_selection_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _wire_impl(monkeypatch, page)
    ctx = _ctx(request_policy=RequestPolicy(), org_credentials_for_turn=[])
    result = await tools_module._fill_credential_field_impl(ctx, "#passwordInput", "cred_123", "password")
    assert result["ok"] is False
    assert "request_credential" in result["error"]
    assert page.fill_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_live_admission_exposes_the_existing_selection_path(ambiguous: bool) -> None:
    candidates = [_org_credential("cred_site", "site login", _FIXTURE_LOGIN_URL)]
    if ambiguous:
        candidates.append(_org_credential("cred_other", "other login", _FIXTURE_LOGIN_URL))
    admission = await admit_credential_for_live_page(
        RequestPolicy(),
        organization_id="o_1",
        credential_id="cred_unselected",
        page_url=_FIXTURE_LOGIN_URL,
        load_org_credentials=AsyncMock(return_value=candidates),
    )
    assert admission.admitted is False
    assert admission.steer is not None
    assert "request_credential" in admission.steer if ambiguous else "cred_site" in admission.steer
    assert "Ask the user which" not in admission.steer


_ACQUISITION_DELAY_SECONDS = 0.2
_ARM_TOTP = "246810"
_ARM_LOGIN_ORIGIN = "https://authenticationtest.com"
_ARM_SSO_ORIGIN = "https://sso.example.com"
_ARM_SECRETS = (_FAKE_USERNAME, _FAKE_PASSWORD, _ARM_TOTP)

_ARM_LOGIN_HTML = """<!DOCTYPE html><html><head><title>Sign in</title></head><body>
<form id="login" method="post" action="/totp/">
  <label for="email">Email</label><input id="email" name="email" type="email" />
  <label for="password">Password</label><input id="password" name="password" type="password" />
  <button id="signIn" type="submit">Sign in</button>
</form></body></html>"""

_ARM_TOTP_HTML = """<!DOCTYPE html><html><head><title>Two-factor</title></head><body>
<form id="otp" method="post" action="/welcome/">
  <label for="totpCode">Authenticator code</label><input id="totpCode" name="code" inputmode="numeric" />
  <button id="verifyButton" type="submit">Verify</button>
</form>{script}</body></html>"""

_ARM_AUTO_SUBMIT_SCRIPT = """<script>
document.getElementById('totpCode').addEventListener('input', (event) => {
  if (event.target.value.length === 6) event.target.form.submit();
});
</script>"""

_ARM_WELCOME_HTML = (
    '<!DOCTYPE html><html><head><title>Welcome</title></head><body><h1 id="welcome">Welcome</h1></body></html>'
)

_ARM_SSO_HTML = """<!DOCTYPE html><html><head><title>Single sign-on</title></head><body>
<form method="post" action="/collect/">
  <label for="password">Password</label><input id="password" name="password" type="password" />
  <button id="signIn" type="submit">Sign in</button>
</form></body></html>"""


class _AcquisitionArm:
    """A real Chromium page behind the real overlay server and runtime browser context, with a
    fixed delay and a trace at the persistent-session `browser_operation` seam every read enters."""

    def __init__(self, page: Page, *, auto_submit_totp: bool) -> None:
        self.page = page
        self.auto_submit_totp = auto_submit_totp
        self.trace: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, str]] = []
        self.rejected = False
        self.on_acquire: Callable[[], Awaitable[None]] | None = None
        self.after_mint: Callable[[], Awaitable[None]] | None = None

    def event(self, name: str, **facts: str | int) -> None:
        self.trace.append({"event": name, **facts})

    @asynccontextmanager
    async def browser_operation(
        self, _session_id: str, resolved_state: BrowserState
    ) -> AsyncIterator[BrowserOperation | BrowserOperationRejected]:
        await asyncio.sleep(_ACQUISITION_DELAY_SECONDS)
        self.event("acquire")
        if self.on_acquire is not None:
            hook, self.on_acquire = self.on_acquire, None
            await hook()
        if self.rejected:
            yield BrowserOperationRejected(BrowserRetirementReason.replacement)
            return
        yield BrowserOperation(resolved_state, BrowserRetirement())

    async def route(self, route: Route) -> None:
        request = route.request
        self.requests.append((request.method, request.url, request.post_data or ""))
        parts = urlsplit(request.url)
        if f"{parts.scheme}://{parts.netloc}" == _ARM_SSO_ORIGIN:
            body = _ARM_SSO_HTML
        elif f"{parts.scheme}://{parts.netloc}" != _ARM_LOGIN_ORIGIN:
            await route.abort()
            return
        elif parts.path == "/totp/":
            body = _ARM_TOTP_HTML.format(script=_ARM_AUTO_SUBMIT_SCRIPT if self.auto_submit_totp else "")
        elif parts.path == "/welcome/":
            body = _ARM_WELCOME_HTML
        else:
            body = _ARM_LOGIN_HTML
        await route.fulfill(status=200, content_type="text/html", body=body)

    def posts_to(self, path: str) -> int:
        return sum(1 for method, url, _ in self.requests if method == "POST" and urlsplit(url).path == path)


class _LocalEvaluateClient:
    def __init__(self, arm: _AcquisitionArm) -> None:
        self._arm = arm

    async def call_tool(self, name: str, args: dict[str, Any], raise_on_error: bool = False) -> SimpleNamespace:
        self._arm.event("dispatch", tool=name)
        payload = (
            await skyvern_evaluate(**args)
            if name == "skyvern_evaluate"
            else {"ok": False, "error": f"{name} is not served by this arm"}
        )
        return SimpleNamespace(structured_content=payload, is_error=payload.get("ok", True) is not True, content=[])


@asynccontextmanager
async def _acquisition_arm(
    monkeypatch: pytest.MonkeyPatch, *, auto_submit_totp: bool = False
) -> AsyncIterator[tuple[_AcquisitionArm, CopilotContext]]:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except PlaywrightError:
            pytest.skip("Requires Playwright Chromium (run: playwright install chromium)")
        context = await browser.new_context()
        page = await context.new_page()
        arm = _AcquisitionArm(page, auto_submit_totp=auto_submit_totp)
        await context.route("**/*", arm.route)
        await page.goto(f"{_ARM_LOGIN_ORIGIN}/login/")

        browser_state = MagicMock(browser_context=context)
        browser_state.get_working_page = AsyncMock(return_value=page)
        browser_state.get_or_create_page = AsyncMock(return_value=page)
        manager = MagicMock()
        manager.get_browser_state = AsyncMock(return_value=browser_state)
        manager.browser_operation = arm.browser_operation
        monkeypatch.setattr(copilot_runtime.app, "PERSISTENT_SESSIONS_MANAGER", manager)
        monkeypatch.setattr(copilot_runtime, "get_skyvern", MagicMock(return_value=MagicMock()))

        async def prepared(_ctx: AgentContext, **_kwargs: Any) -> tuple[None, None, None]:
            return None, None, None

        async def session_ready(_ctx: AgentContext) -> None:
            return None

        async def grant(
            _ctx: AgentContext, _credential_id: str
        ) -> tuple[credential_fill_module._CredentialFillOriginGrant, None]:
            return credential_fill_module._CredentialFillOriginGrant(f"{_ARM_LOGIN_ORIGIN}/login/"), None

        async def resolve(ctx: AgentContext, _credential_id: str, field: str) -> tuple[str, str, None]:
            value = {"username": _FAKE_USERNAME, "password": _FAKE_PASSWORD, "totp": _ARM_TOTP}[field]
            if field != "username":
                register_secret_scrub_value(ctx, value)
            if arm.after_mint is not None:
                arm.on_acquire, arm.after_mint = arm.after_mint, None
            return value, "authtest simple", None

        original_fill = SkyvernBrowserPage.fill
        original_click = SkyvernBrowserPage.click

        async def traced_fill(self: SkyvernBrowserPage, selector: str, *args: Any, **kwargs: Any) -> str:
            arm.event("fill", selector=selector)
            return await original_fill(self, selector, *args, **kwargs)

        async def traced_click(self: SkyvernBrowserPage, selector: str, *args: Any, **kwargs: Any) -> str | None:
            arm.event("click", selector=selector)
            return await original_click(self, selector, *args, **kwargs)

        monkeypatch.setattr(mcp_adapter, "_prepare_browser_session_for_dispatch", prepared)
        monkeypatch.setattr(credential_fill_module, "ensure_browser_session", session_ready)
        monkeypatch.setattr(credential_fill_module, "_credential_fill_origin_grant", grant)
        monkeypatch.setattr(credential_fill_module, "_resolve_credential_fill_value", resolve)
        monkeypatch.setattr(SkyvernBrowserPage, "fill", traced_fill)
        monkeypatch.setattr(SkyvernBrowserPage, "click", traced_click)

        ctx = make_copilot_ctx(browser_session_id="pbs_acquisition_arm")
        ctx.api_key = "test-api-key"
        ctx.supports_vision = False
        ctx.block_authoring_policy = BlockAuthoringPolicy.CODE_ONLY_BROWSER
        ctx.request_policy = _policy()
        server = SkyvernOverlayMCPServer(
            transport=object(),
            overlays={},
            alias_map={"evaluate": "skyvern_evaluate"},
            allowlist=frozenset({"skyvern_evaluate"}),
            context_provider=lambda: ctx,
        )
        server._client = _LocalEvaluateClient(arm)  # type: ignore[assignment]
        ctx.discovery_mcp_server = server
        try:
            yield arm, ctx
        finally:
            clear_session_scrub_values(ctx.browser_session_id)
            await context.close()
            await browser.close()


async def _arm_step(
    arm: _AcquisitionArm, ctx: CopilotContext, selector: str, field: str, submit_selector: str | None = None
) -> dict[str, Any]:
    arm.event("step", field=field)
    return await tools_module._fill_credential_field_impl(ctx, selector, "cred_123", field, submit_selector)


def _arm_steps(trace: list[dict[str, Any]]) -> dict[str, list[str]]:
    steps: dict[str, list[str]] = {}
    for entry in trace:
        if entry["event"] == "step":
            current = steps.setdefault(str(entry["field"]), [])
        else:
            current.append(str(entry["event"]))
    return steps


def _acquisitions_before_fill(events: list[str]) -> int:
    return events[: events.index("fill")].count("acquire")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auto_submit_totp",
    [pytest.param(False, id="submit-button"), pytest.param(True, id="auto-submitting-authenticator")],
)
async def test_acquisition_arm_pays_one_acquisition_per_target_read(
    monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None], auto_submit_totp: bool
) -> None:
    async with _acquisition_arm(monkeypatch, auto_submit_totp=auto_submit_totp) as (arm, ctx):
        with capture_logs() as logs:
            username = await _arm_step(arm, ctx, "#email", "username")
            password = await _arm_step(arm, ctx, "#password", "password", "#signIn")
            totp = await _arm_step(arm, ctx, "#totpCode", "totp", "#verifyButton")
        landed_url = arm.page.url
        totp_posts = arm.posts_to("/totp/")
        welcome_posts = arm.posts_to("/welcome/")

    steps = _arm_steps(arm.trace)
    trace = json.dumps(steps)
    record_property("acquisition_trace", trace)
    record_property(
        "acquisitions_before_fill", json.dumps({field: _acquisitions_before_fill(steps[field]) for field in steps})
    )

    # One read per probed target (the email field alone; the password field and its submit), and one
    # more for the fill's own browser scope.
    assert _acquisitions_before_fill(steps["username"]) == 2, trace
    assert _acquisitions_before_fill(steps["password"]) == 3, trace
    assert _acquisitions_before_fill(steps["totp"]) == 3, trace
    assert steps["password"][: steps["password"].index("fill")] == ["acquire", "dispatch"] * 2 + ["acquire"], trace
    fill_at, click_at = steps["password"].index("fill"), steps["password"].index("click")
    assert steps["password"][fill_at + 1 : click_at] == ["acquire", "dispatch", "acquire"], trace

    assert [username["ok"], password["ok"], totp["ok"]] == [True, True, True]
    assert username["data"]["readback_outcome"] == "exact_match"
    assert password["data"]["readback_outcome"] == "exact_match"
    assert password["data"]["submitted"] is True
    assert totp_posts == 1
    assert welcome_posts == 1
    assert urlsplit(landed_url).path == "/welcome/"
    if auto_submit_totp:
        assert totp["data"]["submitted"] is False
        assert "click" not in steps["totp"]
    else:
        assert totp["data"]["readback_outcome"] == "exact_match"
        assert totp["data"]["submitted"] is True

    records = {(entry["tool_name"], entry["selector"]): entry for entry in ctx.scouted_interactions}
    password_record = records[("fill_credential_field", "#password")]
    assert password_record["selector_match_count"] == 1
    assert (password_record["role"], password_record["accessible_name"]) == ("textbox", "Password")
    assert password_record["role_name_match_count"] == 1
    assert (password_record["element_fingerprint_id"], password_record["element_fingerprint_type"]) == (
        "password",
        "password",
    )
    assert password_record["element_fingerprint_probed"]
    assert records[("click", "#signIn")]["selector_match_count"] == 1
    assert records[("click", "#signIn")]["accessible_name"] == "Sign in"

    retained = json.dumps(
        [arm.trace, username, password, totp, ctx.scouted_interactions, ctx.scout_trajectory, logs], default=str
    )
    for secret in _ARM_SECRETS:
        assert secret not in retained


@pytest.mark.asyncio
async def test_acquisition_arm_counts_a_padded_role_name_as_the_name_it_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _acquisition_arm(monkeypatch) as (_arm, ctx):
        probe = await credential_fill_module._probe_scout_target(
            ctx, 'role=button[name=" Sign in "]', fingerprint=False
        )

    assert (probe.role, probe.accessible_name, probe.role_name_match_count) == ("button", "Sign in", 1)


@pytest.mark.asyncio
async def test_acquisition_arm_origin_departure_before_the_fill_releases_nothing_to_the_new_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _acquisition_arm(monkeypatch) as (arm, ctx):
        prior_step = {"tool_name": "click", "selector": "#earlier", "trajectory_index": 0}
        ctx.scout_trajectory = [dict(prior_step)]

        async def depart() -> None:
            await arm.page.goto(f"{_ARM_SSO_ORIGIN}/login/")

        arm.after_mint = depart
        result = await _arm_step(arm, ctx, "#password", "password", "#signIn")
        sso_field = await arm.page.evaluate("document.querySelector('#password').value")
        later = await ctx.discovery_mcp_server.call_internal_tool("skyvern_evaluate", {"expression": "location.host"})
        sso_traffic = [entry for entry in arm.requests if urlsplit(entry[1]).hostname == "sso.example.com"]

    assert result["ok"] is False
    assert sso_field == ""
    assert sso_traffic
    assert all(_FAKE_PASSWORD not in url and _FAKE_PASSWORD not in body for _, url, body in sso_traffic)
    assert not any(method == "POST" for method, _, _ in arm.requests)
    assert "fill" in _arm_steps(arm.trace)["password"]
    assert ctx.scouted_interactions == []
    assert ctx.scout_trajectory == [prior_step]
    assert later["ok"] is True
    assert later["data"]["result"] == "sso.example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_at", ["probe", "after_mint"])
async def test_acquisition_arm_retired_generation_types_nothing_and_leaves_later_reads_working(
    monkeypatch: pytest.MonkeyPatch, retired_at: str
) -> None:
    async with _acquisition_arm(monkeypatch) as (arm, ctx):
        prior_record = {"tool_name": "click", "selector": "#earlier", "url": f"{_ARM_LOGIN_ORIGIN}/login/"}
        ctx.scouted_interactions = [dict(prior_record)]
        prior_step = {"tool_name": "click", "selector": "#earlier", "trajectory_index": 0}
        ctx.scout_trajectory = [dict(prior_step)]

        async def retire() -> None:
            arm.rejected = True

        if retired_at == "probe":
            arm.on_acquire = retire
        else:
            arm.after_mint = retire
        result = await _arm_step(arm, ctx, "#password", "password", "#signIn")
        field_value = await arm.page.evaluate("document.querySelector('#password').value")
        arm.rejected = False
        later = await ctx.discovery_mcp_server.call_internal_tool("skyvern_evaluate", {"expression": "document.title"})

    assert result["ok"] is False
    assert _FAKE_PASSWORD not in json.dumps(result)
    assert field_value == ""
    assert "fill" not in _arm_steps(arm.trace)["password"]
    assert not any(method == "POST" for method, _, _ in arm.requests)
    assert ctx.scouted_interactions == [prior_record]
    assert ctx.scout_trajectory == [prior_step]
    assert later["ok"] is True
    assert later["data"]["result"] == "Sign in"
