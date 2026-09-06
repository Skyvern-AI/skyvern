from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

import structlog

from skyvern.cli.core.session_manager import get_page
from skyvern.forge import app
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_fill_fields import CREDENTIAL_FILL_FIELDS
from skyvern.forge.sdk.copilot.credential_pause import (
    credential_pause_transport_ready,
    defang_card_text,
    request_credential_pause,
)
from skyvern.forge.sdk.copilot.credential_resolution import load_credentials, url_parts
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.page_identity import safe_page_origin
from skyvern.forge.sdk.copilot.request_policy import (
    RequestPolicy,
    admit_credential_for_live_page,
    loggable_origin,
)
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    ScoutedSelectorCandidate,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    ensure_browser_session,
    mcp_browser_context,
    sensitive_origin_page_facts_withheld,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    register_secret_scrub_value,
    scrub_secrets_from_text,
)
from skyvern.forge.sdk.credential_site_policy import same_site
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credentials import generate_totp_code, normalize_totp_config
from skyvern.webeye.utils.dom import is_post_dispatch_click_timeout

from .banned_blocks import _copilot_block_authoring_policy
from .credentials import _missing_credential_reference_tool_error
from .guardrails import _authority_tool_error
from .mcp_hooks import (
    _TYPE_READBACK_SETTLE_SECONDS,
    ScoutReadbackOutcome,
    _scout_readback_outcome,
    _scout_type_landing_failure,
)
from .scouting import (
    _attach_scout_observation_step,
    _attach_scout_page_summary,
    _capture_element_fingerprint,
    _capture_enclosing_form_submits,
    _capture_post_interaction_screenshot,
    _capture_scout_selector_candidates,
    _capture_scout_source_url,
    _clear_pending_browser_interaction_observation,
    _consume_scout_source_url,
    _live_working_page_url,
    _mark_pending_browser_interaction_observation,
    _record_scouted_interaction,
    _register_scout_interaction_observation,
    _resolve_scout_role_name,
    _role_name_match_count,
    _selector_live_match_count,
)

if TYPE_CHECKING:
    from skyvern.library.skyvern_browser_page import SkyvernBrowserPage

LOG = structlog.get_logger()

_CREDENTIAL_FILL_FIELDS = CREDENTIAL_FILL_FIELDS
_CREDENTIAL_FILL_TIMEOUT_MS = 15000
_CREDENTIAL_FILL_READBACK_TIMEOUT_SECONDS = 3.0
_CREDENTIAL_SUBMIT_TIMEOUT_MS = 5000

_CREDENTIAL_FILL_ROLLBACK_FIELDS = (
    "flow_evidence",
    "composition_page_evidence",
    "workflow_verification_evidence",
    "pending_browser_interaction_observation",
    "scouted_interactions",
    "scout_trajectory",
    "pending_scout_source_url",
    "post_run_page_observation_tool",
    "post_run_page_observation_url",
    "post_run_page_observation_workflow_run_id",
    "post_run_page_observation_after_failed_test",
    "post_run_page_observation_generation",
    "latest_recorded_build_test_outcome",
    "recorded_build_test_outcome_history",
    "recorded_persisted_block_run_workflow_run_id",
    "last_scout_observation_trajectory_index",
    "last_scout_observation_has_password_control",
    "scouted_output_covered_paths",
    "scout_observed_terminal_criterion_ids",
    "scout_observation_contract",
    "last_scout_act_observe_outcome",
    "last_scout_act_observe_packet",
)


@dataclass(frozen=True)
class _CredentialFillOriginGrant:
    intended_url: str
    # A vault entry names a site, not a page, so a grant it produced travels that whole site. One
    # earned from a tested URL or a page match stays on the origin that proved it.
    whole_site: bool = False


async def _normalize_totp_config_for_organization(totp_secret: str, organization_id: str) -> str:
    enterprise_totp_secret = await app.AGENT_FUNCTION.parse_enterprise_totp_secret(
        totp_secret,
        organization_id=organization_id,
    )
    if enterprise_totp_secret is not None:
        return enterprise_totp_secret
    return normalize_totp_config(totp_secret)


def _runtime_otp_steering_error(credential_id: str) -> str:
    return (
        f"Credential `{credential_id}` receives one-time codes by email/SMS, so `fill_credential_field` cannot "
        "safely retrieve the code during scouting without a workflow run/task context to anchor polling. "
        "Persist the OTP step in a code block as `await <credential_parameter>.otp()` after the action that "
        "triggers delivery; the runtime will poll for the fresh code during the workflow run without exposing it."
    )


def _scrub_secret_from_text(text: str, secret_value: str) -> str:
    if not secret_value:
        return text
    return text.replace(secret_value, REDACTED_SECRET_PLACEHOLDER)


def _credential_fill_prerequisite_error(copilot_ctx: AgentContext, credential_id: str) -> str | None:
    if _copilot_block_authoring_policy(copilot_ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return (
            "fill_credential_field is only available in code-only browser authoring mode. "
            "Author a `login` block bound to the credential parameter instead."
        )
    policy = getattr(copilot_ctx, "request_policy", None)
    if not isinstance(policy, RequestPolicy) or policy.raw_secret_detected:
        return "Saved-credential scouting is unavailable because this turn has no safe credential provenance."
    return None


def _still_on_admitted_site(current_url: str | None, admitted_url: str) -> bool:
    """Whether the browser is still on the origin whose login page granted this credential.

    Compared at origin, not at the tier that granted it: a real sign-in walks email -> password ->
    one-time code across several paths of the same site, and refusing those would refuse the login
    the grant exists for. What it still stops is the secret following a redirect off the site.
    """
    admitted_parts = url_parts(admitted_url)
    current_parts = url_parts(current_url or "")
    admitted_origin = canonicalize_origin(admitted_parts[2]) if admitted_parts else None
    current_origin = canonicalize_origin(current_parts[2]) if current_parts else None
    return bool(current_origin and admitted_origin and current_origin == admitted_origin)


# One site policy, shared with code-block credential release.
_same_site = same_site


def _within_grant(current_url: str | None, grant: _CredentialFillOriginGrant) -> bool:
    if grant.whole_site:
        return _same_site(current_url, grant.intended_url)
    return _still_on_admitted_site(current_url, grant.intended_url)


class _CredentialFillOriginMismatchError(Exception):
    pass


def _credential_fill_origin_mismatch_error() -> str:
    return (
        "The browser left this credential's intended login origin before it could be filled. "
        "Re-inspect the current page and fill again if the sign-in is still in progress there."
    )


def _credential_submit_origin_mismatch_notice() -> str:
    return (
        "The field was filled, but the browser left this credential's login origin before the submit "
        "control could be clicked, so it was not clicked. The form may already have been submitted — "
        "inspect the current page before filling this field again."
    )


def _credential_submit_target_gone_notice() -> str:
    return (
        "The field was filled, but the submit control was no longer on the page, so it was not clicked. "
        "A form that submits itself once the code is complete may already have been submitted, though a "
        "re-render or an error state can also remove the control — inspect the current page before "
        "filling this field again."
    )


def _credential_submit_already_committed_notice() -> str:
    return (
        "The field was filled and the page moved on before the submit control could be clicked, so it "
        "was not clicked: the form submitted itself. Inspect the current page to see where the sign-in "
        "got to rather than filling this field again."
    )


def _credential_submit_unconfirmed_readback_notice() -> str:
    # Deliberately does not tell the model to read the field: a field that differs usually holds a
    # mutation of the secret, which the scrubber cannot match and so would not redact.
    return (
        "The code field does not hold what was typed, so the submit control was not clicked: submitting "
        "a code the field does not hold voids it. Fill again with a selector for the intended field, or "
        "click the submit control yourself if this page reformats the code on the way in."
    )


def _credential_submit_ambiguous_notice(selector: str, match_count: int) -> str:
    return (
        f"The field was filled, but the submit selector {selector} matches {match_count} controls, so "
        "nothing was clicked rather than guessing between them — on a one-time-code form the wrong one "
        "can resend the code and void the one just typed. Fill again with a selector that matches only "
        "the submit control."
    )


def _credential_submit_selector_never_matched_notice(selector: str) -> str:
    return (
        f"The field was filled, but the submit selector {selector} matched nothing on this page either "
        "before or after the fill, so nothing was clicked and the form was not submitted. Inspect the "
        "page for the real submit control; a one-time code is still waiting to be submitted."
    )


def _credential_submit_page_unreadable_notice() -> str:
    return (
        "The field was filled, but the page could not be read to confirm the browser was still on this "
        "credential's login origin, so the submit control was not clicked. Inspect the current page; a "
        "one-time code is still waiting to be submitted."
    )


def _credential_fill_release_guard(grant: _CredentialFillOriginGrant) -> Callable[[str | None], None]:
    """Bind the fill grant to the resolved element's document at the release seam."""

    def guard(target_url: str | None) -> None:
        if not _within_grant(target_url, grant):
            raise _CredentialFillOriginMismatchError

    return guard


def _credential_fill_authority_error(copilot_ctx: AgentContext, credential_id: str) -> str | None:
    policy = copilot_ctx.request_policy
    resolved_ids = {
        credential.credential_id
        for credential in (policy.resolved_credentials if isinstance(policy, RequestPolicy) else [])
    }
    if credential_id not in resolved_ids:
        return (
            f"The credential `{credential_id}` is not in the credentials resolved for this request, so it "
            "cannot be filled into the live browser. Only credentials the user referenced (listed under "
            "`resolved_credentials` in the request policy) may be scouted. Ask the user which saved "
            "credential to use, or bind the credential as an untested draft parameter without running it."
        )
    return None


def _resolved_credential_intended_url(policy: RequestPolicy, credential_id: str) -> str | None:
    admitted_url = policy.live_page_admitted_urls.get(credential_id)
    if admitted_url:
        return admitted_url
    return next(
        (
            credential.tested_url
            for credential in policy.resolved_credentials
            if credential.credential_id == credential_id and credential.tested_url
        ),
        None,
    )


async def _vault_named_sites(copilot_ctx: AgentContext, credential_id: str) -> list[str]:
    """The sites this credential's own vault entry names, as the user saved them.

    Read straight from the vault rather than the DB row, so a credential nobody has run the test flow
    against still knows where it belongs. Only Bitwarden items carry these; the other vaults store no
    URL for an item, so their credentials fall through to the request-grounded route below.
    """
    cached = copilot_ctx.vault_login_uris_by_credential_id.get(credential_id)
    if cached is not None:
        return cached
    uris: list[str] = []
    try:
        db_credential = await app.DATABASE.credentials.get_credential(
            credential_id, organization_id=copilot_ctx.organization_id
        )
        if db_credential is not None:
            service = app.CREDENTIAL_VAULT_SERVICES.get(db_credential.vault_type or CredentialVaultType.BITWARDEN)
            if service is not None:
                uris = list((await service.get_credential_item(db_credential)).login_uris)
    except Exception:
        # Not cached: a vault read that failed once says nothing about where the credential belongs,
        # and caching the empty answer would refuse every later fill this turn for a transient fault.
        LOG.info("copilot could not read the vault entry's sites", credential_id=credential_id, exc_info=True)
        return []
    copilot_ctx.vault_login_uris_by_credential_id[credential_id] = uris
    return uris


def _missing_credential_origin_error(credential_id: str, page_url: str | None) -> str:
    if page_url:
        origin = loggable_origin(page_url)
        return (
            f"Credential `{credential_id}` cannot be filled on {origin}: the user has not named this site "
            f"in this chat. Ask the user to confirm the sign-in site by pasting its URL — {origin} — "
            "then retry."
        )
    return (
        f"Credential `{credential_id}` cannot be filled: no live page is open. "
        "Navigate to the sign-in page first, then retry."
    )


def _log_fill_grant(route: str, url: str, credential_id: str, source_message: int | None = None) -> None:
    LOG.info(
        "copilot credential fill grant",
        route=route,
        page_origin=loggable_origin(url),
        credential_id=credential_id,
        source_user_message=source_message,
    )


def _user_provided_site_url_match(policy: RequestPolicy, page_url: str) -> tuple[str | None, bool]:
    """Match the live page against a site the user pasted, reporting whether the match was
    site-level. Hosts outside the public suffix list (internal domains, localhost) have no
    registrable site to compare, so they fall back to an exact-origin match.
    """
    for url in policy.user_provided_site_urls:
        if _same_site(page_url, url):
            return url, True
        if _still_on_admitted_site(page_url, url):
            return url, False
    return None, False


_CREDENTIAL_CARD_FALLBACK = (
    "Ask the user in prose to add the login on the Credentials page and reply with its exact saved name."
)


def _unprovided_login_page_error(login_page_url: str) -> str:
    origin = loggable_origin(login_page_url) if login_page_url else "that page"
    return (
        f"No credential can be requested for {origin}: that site has not been named in this chat. "
        "The sign-in page URL has to come from the user before a login can be requested for it."
    )


async def _request_credential(login_page_url: str, reason: str, copilot_ctx: CopilotContext) -> dict[str, Any]:
    policy = copilot_ctx.request_policy
    if not isinstance(policy, RequestPolicy) or _user_provided_site_url_match(policy, login_page_url)[0] is None:
        return {"ok": False, "error": _unprovided_login_page_error(login_page_url)}

    if copilot_ctx.credential_pause_used:
        return {
            "ok": True,
            "status": "already_asked",
            "outcome": copilot_ctx.credential_pause_outcome or "unanswered",
            "next": "Continue without re-asking this turn.",
        }

    config = copilot_ctx.copilot_config
    if config is None or not credential_pause_transport_ready(copilot_ctx, config):
        LOG.info(
            "copilot_credential_card_unavailable",
            flag_enabled=config is not None and config.credential_pause_enabled,
            client_supports=copilot_ctx.client_supports_credential_pause,
            shared_cache=getattr(getattr(app, "CACHE", None), "is_shared", False),
        )
        return {
            "ok": True,
            "status": "unavailable",
            "detail": "The in-chat credential card cannot be shown on this turn.",
            "fallback": _CREDENTIAL_CARD_FALLBACK,
        }

    policy.credential_ask_login_page_urls = [login_page_url]
    resolution = await request_credential_pause(
        copilot_ctx,
        login_page_url=login_page_url,
        message=defang_card_text(reason),
        stream=copilot_ctx.stream,
        copilot_config=config,
    )
    credential = resolution.credential if resolution is not None else None
    if resolution is None or (resolution.action == "connected" and credential is None):
        return {
            "ok": True,
            "status": "unanswered",
            "outcome": copilot_ctx.credential_pause_outcome or "timeout",
            "next": (
                "The user did not answer the card. Continue without the credential: keep the credential "
                "parameter placeholder in the draft and do not ask again this turn."
            ),
        }
    if credential is None:
        return {
            "ok": True,
            "status": "skipped",
            "next": (
                "The user chose not to connect a credential now. Keep the credential parameter placeholder "
                "in the draft, do not ask again this turn, and say a test run may stop at the login step."
            ),
        }
    return {
        "ok": True,
        "status": "connected",
        "credential_id": credential.credential_id,
        "credential_name": credential.name,
        "next": (
            "Bind this credential as the workflow's credential parameter and continue the build; run the "
            "blocks that were waiting on the login."
        ),
    }


def _request_settled_credential(policy: RequestPolicy, credential_id: str) -> bool:
    """A non-model signal already answered which credential: the user named it this turn, or it is
    the only credential resolved for this request (e.g. the one card answer, carried)."""
    if policy.current_turn_named_credential_ids == {credential_id}:
        return True
    return {credential.credential_id for credential in policy.resolved_credentials} == {credential_id}


async def _sole_org_password_credential_id(
    load_org_credentials: Callable[[], Awaitable[list[Credential]]],
) -> str | None:
    """The one saved password credential carrying no login URL, where elimination answers the
    which-credential question a name would otherwise have to. Counts the pool turn start resolves from
    (``allow_urlless_sole``), so a credential resolved there is never re-asked about here.
    """
    unbound = [
        credential
        for credential in await load_org_credentials()
        if credential.credential_type == CredentialType.PASSWORD and not credential.tested_url
    ]
    return unbound[0].credential_id if len(unbound) == 1 else None


def _ambiguous_unbound_credential_steer(credential_id: str, page_url: str) -> str:
    origin = loggable_origin(page_url)
    return (
        f"`{credential_id}` has no saved login page, so it is not established that it belongs to {origin}. "
        "Ask the user to say which saved credential to use *and* to name the sign-in page, for example "
        f'"use <exact name or cred_ id> at {origin}". A reply carrying only the credential grounds no '
        "origin and refuses again, and a bare yes authorizes nothing."
    )


async def _credential_fill_origin_grant(
    copilot_ctx: AgentContext, credential_id: str
) -> tuple[_CredentialFillOriginGrant | None, str | None]:
    """Authorize a fill only when something other than the model binds this credential to an origin; the
    grant is consumed at the release seam, so a credential nothing vouches for gets none and cannot release.
    """
    prerequisite_error = _credential_fill_prerequisite_error(copilot_ctx, credential_id)
    if prerequisite_error:
        return None, prerequisite_error
    policy = copilot_ctx.request_policy
    authority_error = _credential_fill_authority_error(copilot_ctx, credential_id)

    async def load_once() -> list[Credential]:
        if copilot_ctx.org_credentials_for_turn is None:
            copilot_ctx.org_credentials_for_turn = await load_credentials(copilot_ctx.organization_id)
        return copilot_ctx.org_credentials_for_turn

    if not authority_error:
        if not isinstance(policy, RequestPolicy):
            return None, _missing_credential_origin_error(credential_id, None)
        intended_url = _resolved_credential_intended_url(policy, credential_id)
        if intended_url:
            _log_fill_grant("admitted_or_tested", intended_url, credential_id)
            return _CredentialFillOriginGrant(intended_url), None
        page_url = await _live_working_page_url(copilot_ctx) or ""
        # The vault entry names the site the user filed this credential under, so it answers where
        # the secret belongs without anyone having to run the test flow first.
        if page_url and any(_same_site(page_url, uri) for uri in await _vault_named_sites(copilot_ctx, credential_id)):
            _log_fill_grant("vault_site", page_url, credential_id)
            return _CredentialFillOriginGrant(page_url, whole_site=True), None
        if page_url:
            matched_url, site_level = _user_provided_site_url_match(policy, page_url)
            if matched_url is not None:
                if _request_settled_credential(
                    policy, credential_id
                ) or credential_id == await _sole_org_password_credential_id(load_once):
                    # An origin-only match (no registrable site) keeps the origin-scoped grant so the
                    # release guard can still compare it; site matches travel the whole site.
                    _log_fill_grant("user_url", page_url, credential_id, policy.user_site_url_sources.get(matched_url))
                    return _CredentialFillOriginGrant(page_url, whole_site=site_level), None
                return None, _ambiguous_unbound_credential_steer(credential_id, page_url)
        return None, _missing_credential_origin_error(credential_id, page_url or None)

    if not isinstance(policy, RequestPolicy):
        return None, authority_error

    admission = await admit_credential_for_live_page(
        policy,
        organization_id=copilot_ctx.organization_id,
        credential_id=credential_id,
        page_url=await _live_working_page_url(copilot_ctx) or "",
        load_org_credentials=load_once,
    )
    if admission.admitted and admission.page_url:
        _log_fill_grant("live_page_admission", admission.page_url, credential_id)
        return _CredentialFillOriginGrant(admission.page_url), None
    if admission.admitted:
        return None, _missing_credential_origin_error(credential_id, None)
    return None, admission.steer or authority_error


async def _resolve_credential_fill_value(
    copilot_ctx: AgentContext,
    credential_id: str,
    field: str,
) -> tuple[str | None, str, str | None]:
    """Resolve (secret_value, credential_name, error) for one credential field, server-side only."""
    try:
        db_credential = await app.DATABASE.credentials.get_credential(
            credential_id, organization_id=copilot_ctx.organization_id
        )
    except Exception:
        LOG.warning(
            "fill_credential_field could not read the credential record",
            credential_id=credential_id,
            organization_id=copilot_ctx.organization_id,
            exc_info=True,
        )
        return None, "", f"Could not read credential `{credential_id}`. Ask the user to verify it exists."
    if db_credential is None:
        return None, "", _missing_credential_reference_tool_error([credential_id])

    vault_type = db_credential.vault_type or CredentialVaultType.BITWARDEN
    credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
    if credential_service is None:
        return None, "", f"The credential vault for `{credential_id}` is not configured on this deployment."
    try:
        credential_item = await credential_service.get_credential_item(db_credential)
    except Exception as exc:
        LOG.warning(
            "fill_credential_field could not fetch the credential from the vault",
            credential_id=credential_id,
            vault_type=str(vault_type),
            exc_info=True,
        )
        return None, "", f"Could not fetch credential `{credential_id}` from the vault: {type(exc).__name__}."
    credential = credential_item.credential
    if not isinstance(credential, PasswordCredential):
        return None, "", f"Credential `{credential_id}` is not a username/password credential."

    if field == "username":
        value: str | None = credential.username
    elif field == "password":
        value = credential.password
        register_secret_scrub_value(copilot_ctx, value)
    else:
        if not credential.totp:
            # A saved OTP identifier means the code is delivered out-of-band;
            # only runtime polling has the run/task context needed to resolve it.
            if credential.totp_identifier or credential.totp_type in {TotpType.EMAIL, TotpType.TEXT}:
                return None, credential_item.name, _runtime_otp_steering_error(credential_id)
            return None, "", f"Credential `{credential_id}` has no TOTP secret configured."
        try:
            value = generate_totp_code(
                await _normalize_totp_config_for_organization(
                    credential.totp,
                    copilot_ctx.organization_id,
                )
            )
        except Exception:
            LOG.warning(
                "fill_credential_field could not generate a TOTP code",
                credential_id=credential_id,
                exc_info=True,
            )
            return None, "", f"Could not generate a TOTP code for credential `{credential_id}`."
        register_secret_scrub_value(copilot_ctx, value)
    if not value:
        return None, "", f"Credential `{credential_id}` has no `{field}` value."
    copilot_ctx.scouted_credential_field_inventory_by_credential_id[credential_id] = frozenset(
        field_name
        for field_name, field_value in (
            ("username", credential.username),
            ("password", credential.password),
            ("totp", credential.totp),
        )
        if field_value
    )
    return value, credential_item.name, None


async def _read_filled_field_value(page: SkyvernBrowserPage, selector: str) -> str | None:
    """Read a field back on the same page handle that filled it.

    The tool-layer read path replaces registered secrets with a placeholder, so a credential
    readback taken through it describes the scrubber rather than the field.
    """

    async def read() -> str | None:
        try:
            # `.first` matches the direct fill's narrowing — the un-narrowed locator raises strict
            # mode on a multi-match, and a read that stalls means an auto-submit already moved on.
            value = await asyncio.wait_for(
                page.locator(selector).first.input_value(), timeout=_CREDENTIAL_FILL_READBACK_TIMEOUT_SECONDS
            )
        except Exception:
            LOG.debug("credential fill readback failed; leaving the value unread")
            return None
        return value if isinstance(value, str) else None

    value = await read()
    if value is not None and value.strip() == "":
        # A controlled/React input can mirror its value asynchronously, so a first read may be
        # transiently empty; settle briefly and re-read once before declaring the fill lost.
        await asyncio.sleep(_TYPE_READBACK_SETTLE_SECONDS)
        value = await read()
    return value


@dataclass(frozen=True)
class _ScoutTargetProbe:
    """A target's factual identity, read before the secret-bearing action reaches the page."""

    selector: str
    selector_candidates: list[ScoutedSelectorCandidate]
    selector_match_count: int | None = None
    role: str = ""
    accessible_name: str = ""
    role_name_match_count: int | None = None
    fingerprint: dict[str, str] = dataclass_field(default_factory=dict)


def _fill_observed_effects(outcome: ScoutReadbackOutcome, *, landing_inferred_from_navigation: bool) -> dict[str, bool]:
    """A landing is recorded only where one was observed, so `value_landed` is absent rather than
    False whenever the field did not read back what was typed."""
    if landing_inferred_from_navigation:
        return {"landing_inferred_from_navigation": True}
    return {"value_landed": True} if outcome is ScoutReadbackOutcome.EXACT_MATCH else {}


async def _probe_scout_target(copilot_ctx: AgentContext, selector: str, *, fingerprint: bool) -> _ScoutTargetProbe:
    await _capture_scout_selector_candidates(copilot_ctx, selector)
    captured_selector_candidates = copilot_ctx.pending_scout_selector_candidates
    copilot_ctx.pending_scout_selector_candidates = None
    selector_candidates: list[ScoutedSelectorCandidate] = [
        {"selector": selector, "source": "requested", "match_count": None}
    ]
    for candidate in captured_selector_candidates or []:
        existing = next(
            (item for item in selector_candidates if item["selector"] == candidate["selector"]),
            None,
        )
        if existing is None:
            selector_candidates.append(candidate)
        elif existing["match_count"] is None:
            existing["match_count"] = candidate["match_count"]
    role, accessible_name = await _resolve_scout_role_name(copilot_ctx, selector)
    return _ScoutTargetProbe(
        selector=selector,
        selector_candidates=selector_candidates,
        selector_match_count=await _selector_live_match_count(copilot_ctx, selector),
        role=role,
        accessible_name=accessible_name,
        role_name_match_count=(
            await _role_name_match_count(copilot_ctx, role, accessible_name) if role and accessible_name else None
        ),
        fingerprint=await _capture_element_fingerprint(copilot_ctx, selector) if fingerprint else {},
    )


@dataclass(frozen=True)
class _CredentialSubmitOutcome:
    clicked: bool
    result_url: str = ""
    mint_to_submit_ms: int | None = None
    skipped: str | None = None
    error: str | None = None
    live_match_count: int | None = None


async def _submit_after_credential_fill(
    copilot_ctx: AgentContext,
    *,
    probe: _ScoutTargetProbe,
    grant: _CredentialFillOriginGrant,
    source_url: str,
    mint_started: float,
    secret_value: str,
) -> _CredentialSubmitOutcome:
    """Click the submit control the fill was aimed at, without ever costing the fill itself."""
    if not _within_grant(source_url, grant):
        # An unreadable page is not a page that moved; both fail closed, but only one of them knows
        # where the browser went, and the model acts on what this says.
        notice = (
            _credential_submit_origin_mismatch_notice() if source_url else _credential_submit_page_unreadable_notice()
        )
        return _CredentialSubmitOutcome(clicked=False, skipped=notice)
    # A form that submits itself on the last digit takes its own submit control off the page. Clicking
    # anyway spends the full click timeout and reports a failure for a login that already succeeded.
    # None is an unreadable page, not an absent control: skipping on it would strand a fresh code.
    live_match_count = await _selector_live_match_count(copilot_ctx, probe.selector)
    # Whichever read actually saw the page decides. An unreadable count at dispatch does not unsee
    # what the probe counted before the fill, and neither read being able to see is not a verdict.
    known_match_count = live_match_count if live_match_count is not None else probe.selector_match_count
    if known_match_count == 0:
        # A control that was never there is a wrong selector, not a form that submitted itself. Both
        # skip the click, but telling the model the login went through when it did not strands the code.
        gone = (
            _credential_submit_selector_never_matched_notice(probe.selector)
            if probe.selector_match_count == 0
            else _credential_submit_target_gone_notice()
        )
        return _CredentialSubmitOutcome(clicked=False, skipped=gone, live_match_count=live_match_count)
    # A direct click resolves to `.first`, so an ambiguous selector picks by document order. On a
    # one-time-code form the neighbour is often "Resend code", which would void the code just typed.
    if known_match_count is not None and known_match_count > 1:
        return _CredentialSubmitOutcome(
            clicked=False,
            skipped=_credential_submit_ambiguous_notice(probe.selector, known_match_count),
            live_match_count=live_match_count,
        )
    engine_selection = None
    error_text: str | None = None
    clicked = False
    try:
        async with mcp_browser_context(copilot_ctx):
            page, _ = await get_page(session_id=copilot_ctx.browser_session_id)
            engine_selection = page.engine_selection
            try:
                await page.click(probe.selector, mode="direct", timeout=_CREDENTIAL_SUBMIT_TIMEOUT_MS)
                clicked = True
            except Exception as exc:
                # A submit that navigates away leaves the post-click auto-wait timing out after the
                # click was already dispatched; the form was submitted, so this is not a failure.
                if is_post_dispatch_click_timeout(exc, engine_selection):
                    clicked = True
                else:
                    error_text = scrub_secrets_from_text(copilot_ctx, _scrub_secret_from_text(str(exc), secret_value))
    except Exception as exc:
        # Raised entering or leaving the browser context rather than by the click. Reporting a failure
        # for a click that already went out invites the model to submit the form a second time.
        if not clicked:
            error_text = scrub_secrets_from_text(copilot_ctx, _scrub_secret_from_text(str(exc), secret_value))
    if not clicked:
        LOG.info(
            "copilot fill_credential_field submit click failed",
            selector=probe.selector,
        )
        return _CredentialSubmitOutcome(
            clicked=False,
            error=(
                f"{error_text or 'The submit control could not be clicked.'} Whether the click reached "
                "the page before this failed is not known, so the form may already have been submitted. "
                "Inspect the current page before submitting again — a second attempt spends another code."
            ),
            live_match_count=live_match_count,
        )
    mint_to_submit_ms = int((time.monotonic() - mint_started) * 1000)
    return _CredentialSubmitOutcome(
        clicked=True,
        result_url=await _live_working_page_url(copilot_ctx) or "",
        mint_to_submit_ms=mint_to_submit_ms,
        live_match_count=live_match_count,
    )


async def _fill_credential_field_impl(
    copilot_ctx: AgentContext,
    selector: str,
    credential_id: str,
    field: str,
    submit_selector: str | None = None,
) -> dict[str, Any]:
    lock = getattr(copilot_ctx, "credential_fill_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        copilot_ctx.credential_fill_lock = lock
    async with lock:
        async with browser_page_custody_lock(copilot_ctx):
            async with browser_evidence_commit_lock(copilot_ctx):
                return await _fill_credential_field_impl_serial(
                    copilot_ctx, selector, credential_id, field, submit_selector
                )


async def _fill_credential_field_impl_serial(
    copilot_ctx: AgentContext,
    selector: str,
    credential_id: str,
    field: str,
    submit_selector: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"selector": selector, "credential_id": credential_id, "field": field}
    if submit_selector:
        arguments["submit_selector"] = submit_selector
    context_values = vars(copilot_ctx)
    rollback_snapshot = {
        field_name: deepcopy(context_values[field_name])
        for field_name in _CREDENTIAL_FILL_ROLLBACK_FIELDS
        if field_name in context_values
    }
    origin_run_id = copilot_ctx.last_run_blocks_workflow_run_id

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
            # The custody lock excludes every other model browser evidence commit while this call
            # runs, so restoring this transaction cannot remove facts owned by a parallel tool.
            for field_name, value in rollback_snapshot.items():
                setattr(copilot_ctx, field_name, deepcopy(value))
            result = {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR}
        record_tool_step_result_for_ctx(copilot_ctx, "fill_credential_field", arguments, result)
        return result

    authority_error = _authority_tool_error(copilot_ctx, "fill_credential_field")
    if authority_error:
        return finish({"ok": False, "error": authority_error})
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})

    selector = (selector or "").strip()
    submit_selector = (submit_selector or "").strip()
    field = (field or "").strip().lower()
    credential_id = (credential_id or "").strip()
    if not selector:
        return finish({"ok": False, "error": "fill_credential_field requires a CSS selector for the input field."})
    if field not in _CREDENTIAL_FILL_FIELDS:
        return finish({"ok": False, "error": "fill_credential_field `field` must be one of: username, password, totp."})
    origin_grant, policy_error = await _credential_fill_origin_grant(copilot_ctx, credential_id)
    if policy_error or origin_grant is None:
        LOG.info(
            "copilot fill_credential_field rejected tool-side",
            credential_id=credential_id,
            field=field,
            organization_id=copilot_ctx.organization_id,
        )
        return finish({"ok": False, "error": policy_error or _missing_credential_origin_error(credential_id, None)})

    session_error = await ensure_browser_session(copilot_ctx)
    if session_error:
        return finish(session_error)
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    await _capture_scout_source_url(copilot_ctx)
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    # Capture each target's factual identity before the secret-bearing action; a fill can change
    # attributes, trigger framework replacement, or navigate, so a later read describes a different
    # element. These facts never include the credential value.
    fill_probe = await _probe_scout_target(copilot_ctx, selector, fingerprint=True)
    submit_probe = (
        await _probe_scout_target(copilot_ctx, submit_selector, fingerprint=False) if submit_selector else None
    )
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    fingerprint = fill_probe.fingerprint

    value, credential_name, resolve_error = await _resolve_credential_fill_value(copilot_ctx, credential_id, field)
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    # Started here rather than before the resolver, whose credential read and enterprise-secret
    # normalization precede the generation and would be counted as code age they are not. Nothing
    # between the generation and this line touches the network. From here only the fill's own readback
    # and the reads the submit needs to re-check its origin and target may sit before the click.
    mint_started = time.monotonic()
    if resolve_error or value is None:
        error_result: dict[str, Any] = {
            "ok": False,
            "error": resolve_error or "Could not resolve the credential value.",
        }
        if credential_name:
            error_result["data"] = {
                "credential_id": credential_id,
                "credential_name": credential_name,
                "credential_field": field,
            }
        return finish(error_result)
    fill_outcome: ScoutReadbackOutcome | None = None
    try:
        async with mcp_browser_context(copilot_ctx):
            page, _ = await get_page(session_id=copilot_ctx.browser_session_id)
            await page.fill(
                selector,
                value,
                mode="direct",
                timeout=_CREDENTIAL_FILL_TIMEOUT_MS,
                _direct_fill_release_guard=_credential_fill_release_guard(origin_grant),
            )
            readback = await _read_filled_field_value(page, selector)
            fill_outcome = _scout_readback_outcome(readback, value)
    except _CredentialFillOriginMismatchError:
        return finish({"ok": False, "error": _credential_fill_origin_mismatch_error()})
    except Exception as exc:
        error_text = scrub_secrets_from_text(copilot_ctx, _scrub_secret_from_text(str(exc), value))
        LOG.info(
            "copilot fill_credential_field fill failed",
            selector=selector,
            credential_id=credential_id,
            field=field,
            error_type=type(exc).__name__,
        )
        return finish(
            {
                "ok": False,
                "error": (
                    f"fill_credential_field could not fill {selector!r}: {error_text} "
                    "Verify the selector matches a single visible, editable input on the current page "
                    "(inspect the page again if needed), then retry."
                ),
            }
        )

    assert fill_outcome is not None
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})

    _clear_pending_browser_interaction_observation(copilot_ctx)
    source_url = _consume_scout_source_url(copilot_ctx)
    landing_failure = _scout_type_landing_failure(
        fill_outcome,
        tool_name="fill_credential_field",
        selector=selector,
    )
    landing_inferred_from_navigation = False
    if landing_failure is not None and fill_outcome is ScoutReadbackOutcome.EMPTY:
        # A form that commits on the last character clears its own field and moves on, so the field
        # reads empty because the fill worked, not because it was lost. Only the page having left the
        # one the fill acted on distinguishes the two, and it is read here rather than up front
        # because every other path reaches this point having already landed.
        landed_url = await _live_working_page_url(copilot_ctx) or ""
        if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
        # Compared at origin+path, not as raw strings: a rejected code re-renders the same page at
        # ?error=..., and reading that as a navigation would report a fill nobody saw land.
        landed_parts = url_parts(landed_url) if landed_url else None
        source_parts = url_parts(source_url) if source_url else None
        landed_page = landed_parts[1] if landed_parts else None
        source_page = source_parts[1] if source_parts else None
        if landed_page and source_page and landed_page != source_page:
            LOG.info(
                "copilot fill_credential_field field cleared by a navigation, treating the fill as landed",
                selector=selector,
                credential_id=credential_id,
                field=field,
            )
            landing_failure = None
            landing_inferred_from_navigation = True
    LOG.info(
        "copilot fill_credential_field readback outcome",
        selector=selector,
        credential_id=credential_id,
        field=field,
        outcome=fill_outcome.value,
        inferred_from_navigation=landing_inferred_from_navigation,
    )
    if landing_failure is not None:
        landing_failure["data"] = {
            "selector": selector,
            "credential_id": credential_id,
            "field": field,
            "typed_length": len(value),
            "readback_outcome": fill_outcome.value,
            "landing_inferred_from_navigation": landing_inferred_from_navigation,
        }
        return finish(landing_failure)
    url = await _live_working_page_url(copilot_ctx) or ""
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    _record_scouted_interaction(
        copilot_ctx,
        tool_name="fill_credential_field",
        selector=selector,
        selector_candidates=fill_probe.selector_candidates,
        selector_match_count=fill_probe.selector_match_count,
        source_url=source_url,
        result_url=url,
        observed_effects=_fill_observed_effects(
            fill_outcome, landing_inferred_from_navigation=landing_inferred_from_navigation
        ),
        typed_length=len(value),
        role=fill_probe.role,
        accessible_name=fill_probe.accessible_name,
        role_name_match_count=fill_probe.role_name_match_count,
        credential_id=credential_id,
        credential_field=field,
        credential_name=credential_name,
        element_fingerprint_id=fingerprint.get("id"),
        element_fingerprint_name=fingerprint.get("name"),
        element_fingerprint_type=fingerprint.get("type"),
        element_fingerprint_placeholder=fingerprint.get("placeholder"),
        element_fingerprint_label=fingerprint.get("label"),
        element_fingerprint_test_id=fingerprint.get("test_id"),
        element_fingerprint_tag=fingerprint.get("tag"),
        element_fingerprint_probed=fingerprint.get("probed"),
    )
    if fingerprint:
        LOG.info(
            "element_fingerprint_captured",
            selector=selector,
            fingerprint_keys=list(fingerprint.keys()),
        )
    data: dict[str, Any] = {
        "selector": selector,
        "credential_id": credential_id,
        "field": field,
        "typed_length": len(value),
        "url": url,
        "credential_name": credential_name,
        "readback_outcome": fill_outcome.value,
        "landing_inferred_from_navigation": landing_inferred_from_navigation,
        # Stated rather than left to be read off which other keys are present: whether the form went
        # in is the one thing here the model must not have to infer.
        "submitted": False,
    }
    submit: _CredentialSubmitOutcome | None = None
    if submit_probe is not None and landing_inferred_from_navigation:
        # The page moved on under the fill, which is the form having committed itself. The probed
        # control belongs to the page that is gone, so clicking now would act on a different one.
        data["submit_skipped"] = _credential_submit_already_committed_notice()
    elif submit_probe is not None and fill_outcome is ScoutReadbackOutcome.DIFFERENT and field == "totp":
        # Submitting a code the field does not hold voids it, which no retry recovers; a username or
        # password that submits wrong just fails the sign-in, and the run says so. A field that
        # reformats what it accepts also reads back different, so this trades one round trip for the
        # code, and is scoped to the field where that trade is worth making.
        data["submit_skipped"] = _credential_submit_unconfirmed_readback_notice()
    elif submit_probe is not None:
        submit = await _submit_after_credential_fill(
            copilot_ctx,
            probe=submit_probe,
            grant=origin_grant,
            source_url=url,
            mint_started=mint_started,
            secret_value=value,
        )
        if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
        if submit.clicked:
            data["submitted"] = True
            data["submit_selector"] = submit_probe.selector
            # The fill can render a second matching control, so the count at dispatch is what the
            # click actually faced; the pre-fill count is only a fallback when that read failed. No
            # `ambiguous` flag rides along because a click only happens once both reads agree the
            # selector is singular — an ambiguous one is declined rather than clicked and re-anchored.
            _record_scouted_interaction(
                copilot_ctx,
                tool_name="click",
                selector=submit_probe.selector,
                selector_candidates=submit_probe.selector_candidates,
                selector_match_count=(
                    submit.live_match_count
                    if submit.live_match_count is not None
                    else submit_probe.selector_match_count
                ),
                source_url=url,
                result_url=submit.result_url,
                role=submit_probe.role,
                accessible_name=submit_probe.accessible_name,
                role_name_match_count=submit_probe.role_name_match_count,
            )
        elif submit.skipped is not None:
            data["submit_skipped"] = submit.skipped
        elif submit.error is not None:
            data["submit_error"] = submit.error
            # `submitted: false` on its own reads as "nothing went out, retry freely". A click that
            # raised may still have reached the page, and a blind retry spends a second code.
            data["submit_uncertain"] = True

    submitted = submit is not None and submit.clicked
    observed_tool = "click" if submitted else "fill_credential_field"
    observed_selector = submit_probe.selector if submitted and submit_probe is not None else selector
    observed_source_url = url if submitted else source_url
    observed_url = submit.result_url if submitted and submit is not None else url
    if submitted:
        # The submit mints no page evidence of its own, so without this observation the fill and
        # its submit are invisible to anything that reconstructs what the scout touched.
        await _register_scout_interaction_observation(
            copilot_ctx,
            tool_name="fill_credential_field",
            selector=selector,
            source_url=source_url,
            url=url,
        )
        if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    _mark_pending_browser_interaction_observation(copilot_ctx, tool_name=observed_tool, url=observed_url)
    # An act-observe that cannot reach the discovery server leaves the previous click's outcome in
    # place, which would be stamped onto this submit's evidence as though it described this page.
    copilot_ctx.last_scout_act_observe_outcome = None
    copilot_ctx.last_scout_act_observe_packet = None
    observation_step, page_evidence = await _register_scout_interaction_observation(
        copilot_ctx,
        tool_name=observed_tool,
        selector=observed_selector,
        source_url=observed_source_url,
        url=observed_url,
    )
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    _attach_scout_observation_step(
        copilot_ctx,
        tool_name=observed_tool,
        selector=observed_selector,
        observation_step=observation_step,
    )
    result: dict[str, Any] = {"ok": True, "data": data}
    if observation_step is not None:
        result["observation_step"] = observation_step
        data["observation_step"] = observation_step
    if submitted:
        data["submit_url"] = safe_page_origin(observed_url) or ""
        if page_evidence is not None:
            _attach_scout_page_summary(copilot_ctx, result, page_evidence)
    else:
        form_submits = await _capture_enclosing_form_submits(copilot_ctx, selector)
        if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
            return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
        if form_submits:
            data["form_submit_controls"] = form_submits
    await _capture_post_interaction_screenshot(
        copilot_ctx,
        source_tool=observed_tool,
        captured_url=observed_url,
        observation_step=observation_step,
    )
    if sensitive_origin_page_facts_withheld(copilot_ctx, origin_run_id):
        return finish({"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR})
    LOG.info(
        "copilot fill_credential_field filled a saved credential field",
        selector=selector,
        credential_id=credential_id,
        field=field,
        typed_length=len(value),
        url=url or None,
        submit_selector=submit_probe.selector if submit_probe is not None else None,
        totp_mint_to_submit_ms=submit.mint_to_submit_ms if submit is not None else None,
    )
    return finish(result)
