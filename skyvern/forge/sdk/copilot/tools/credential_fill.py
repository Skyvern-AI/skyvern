from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import structlog

from skyvern.cli.core.session_manager import get_page
from skyvern.forge import app
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.copilot.blocker_signal import (
    CREDENTIAL_ORIGIN_RECOVERY_DECLINED_REASON_CODE,
    CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE,
    CopilotToolBlockerSignal,
    clear_tool_blocker_signals_for_reason_codes,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, CopilotConfig
from skyvern.forge.sdk.copilot.context import CopilotContext
from skyvern.forge.sdk.copilot.credential_fill_fields import CREDENTIAL_FILL_FIELDS
from skyvern.forge.sdk.copilot.credential_pause import (
    RAW_SECRET_CONNECTED_NEXT,
    CredentialPauseResolution,
    credential_pause_transport_ready,
    defang_card_text,
    raw_secret_card_origin,
    request_credential_pause,
)
from skyvern.forge.sdk.copilot.credential_resolution import is_resolved_page_url, load_credentials, url_parts
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.page_identity import safe_page_origin
from skyvern.forge.sdk.copilot.request_policy import (
    QuestionResponseSiteURLSource,
    RequestPolicy,
    SiteURLSource,
    UserMessageSiteURLSource,
    admit_credential_for_live_page,
    loggable_origin,
)
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    CredentialOriginDeclineStatus,
    CredentialOriginRecovery,
    ScoutedSelectorCandidate,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    effective_browser_session_id,
    ensure_browser_session,
    mcp_browser_context,
    sensitive_origin_page_facts_withheld,
)
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    register_secret_scrub_value,
    scrub_secrets_from_text,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    saved_credential_ids,
    workflow_credential_ids,
    workflow_credential_origins,
)
from skyvern.forge.sdk.credential_site_policy import same_release_scope, same_site
from skyvern.forge.sdk.schemas.credentials import (
    Credential,
    CredentialItem,
    CredentialType,
    CredentialVaultType,
    PasswordCredential,
    TotpType,
)
from skyvern.forge.sdk.services.credentials import generate_totp_code, normalize_totp_config
from skyvern.webeye.utils.dom import is_post_dispatch_click_timeout

from ._shared import _emit_tool_blocker_signal
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
    def __init__(self, target_url: str | None) -> None:
        super().__init__()
        self.target_url = target_url


def _credential_fill_origin_mismatch_error() -> str:
    return (
        "The browser left this credential's intended login origin before it could be filled. "
        "Re-inspect the current page and fill again if the sign-in is still in progress there."
    )


def _credential_origin_declined_text(origin: str) -> str:
    return (
        f"No login authorized for {origin} was connected, and the saved login will not be released on that site. "
        f"Tell the user the sign-in on {origin} needs its own saved login."
    )


def _credential_origin_recovery_error(recovery: CredentialOriginRecovery) -> str:
    if recovery.state == "declined":
        return _credential_origin_declined_text(recovery.origin)
    return (
        f"The sign-in continues on {recovery.origin}, which the selected credential is not authorized for. "
        f"Connect a login for {recovery.origin} through request_credential."
    )


def _start_credential_origin_recovery(copilot_ctx: AgentContext, origin: str, credential_id: str) -> dict[str, Any]:
    current = copilot_ctx.credential_origin_recovery
    if current is not None and (current.state != "pending" or current.origin != origin):
        return {"ok": False, "error": _credential_origin_recovery_error(current)}
    copilot_ctx.credential_origin_recovery = current or CredentialOriginRecovery(origin, "pending", credential_id)
    steering = _emit_tool_blocker_signal(
        copilot_ctx,
        CopilotToolBlockerSignal(
            blocker_kind="authority_denied",
            blocked_tool="fill_credential_field",
            internal_reason_code=CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE,
            agent_steering_text=(
                f"Nothing was filled: this credential is not authorized for {origin}, where this sign-in field is. "
                f"Call request_credential with the sign-in page URL on {origin} so the user can connect a login "
                "for that site before testing the workflow."
            ),
            user_facing_reason=f"The sign-in continues on {origin}, which this saved login is not authorized for.",
            recovery_hint="ask_user_clarifying",
            preserves_workflow_draft=True,
            renders_final_reply=False,
            extra={"observed_origin": origin},
        ),
    )
    return {
        "ok": False,
        "error": steering,
        "recovery": {"observed_origin": origin, "next_action": "request_credential"},
    }


def _decline_credential_origin_recovery(
    copilot_ctx: AgentContext, recovery: CredentialOriginRecovery, status: CredentialOriginDeclineStatus
) -> dict[str, Any]:
    origin = recovery.origin
    copilot_ctx.credential_origin_recovery = replace(recovery, state="declined")
    clear_tool_blocker_signals_for_reason_codes(
        copilot_ctx, frozenset({CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE})
    )
    declined = CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        blocked_tool="request_credential",
        internal_reason_code=CREDENTIAL_ORIGIN_RECOVERY_DECLINED_REASON_CODE,
        agent_steering_text=_credential_origin_declined_text(origin),
        user_facing_reason=(
            f"The sign-in continues on {origin}, and no saved login authorized for that site was connected. "
            f"Add a login for {origin} and ask me again."
        ),
        recovery_hint="report_blocker_to_user",
        preserves_workflow_draft=True,
        renders_final_reply=False,
        extra={"observed_origin": origin},
    )
    _emit_tool_blocker_signal(copilot_ctx, declined)
    return {"ok": True, "status": status, "next": _credential_origin_declined_text(origin)}


async def _credential_evidence_admits_origin(
    copilot_ctx: AgentContext, recovery: CredentialOriginRecovery, credential: Credential
) -> bool:
    """Whether the credential itself places it on the recovery origin, and nowhere else."""
    if credential.credential_id == recovery.refused_credential_id:
        return False
    vault_sites = await _read_vault_named_sites(copilot_ctx, credential.credential_id)
    if vault_sites is None:
        return False
    evidence = ([credential.tested_url] if credential.tested_url else []) + vault_sites
    return bool(evidence) and all(same_release_scope(url, recovery.origin) for url in evidence)


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
            raise _CredentialFillOriginMismatchError(target_url)

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
            "cannot be filled into the live browser yet. Use `list_credentials(exact_reference=...)` "
            "for a credential the user already chose or the saved workflow binds. If the login is still "
            "unresolved, call `request_credential` with the sign-in page URL so the user can pick or add "
            "a credential in chat, then continue."
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
    return await _read_vault_named_sites(copilot_ctx, credential_id) or []


async def _read_vault_named_sites(copilot_ctx: AgentContext, credential_id: str) -> list[str] | None:
    """Sites the credential's own vault entry names (only Bitwarden items carry any), or None when the read failed."""
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
        return None
    copilot_ctx.vault_login_uris_by_credential_id[credential_id] = uris
    return uris


def _missing_credential_origin_error(credential_id: str, page_url: str | None) -> str:
    if page_url:
        origin = loggable_origin(page_url)
        return (
            f"Credential `{credential_id}` has no established login origin for {origin}. "
            f"Call `request_credential` with the sign-in page URL on {origin} so the user can "
            "select or add its login in chat, then continue."
        )
    return (
        f"Credential `{credential_id}` cannot be filled: no live page is open. "
        "Navigate to the sign-in page first, then retry."
    )


def _log_fill_grant(
    route: str,
    url: str,
    credential_id: str,
    source: SiteURLSource | None = None,
) -> None:
    source_fields: dict[str, str | int] = {}
    if isinstance(source, UserMessageSiteURLSource):
        source_fields = {"source_kind": source.kind, "source_user_message": source.message_index}
    elif isinstance(source, QuestionResponseSiteURLSource):
        source_fields = {"source_kind": source.kind, "source_interaction_id": source.interaction_id}
    LOG.info(
        "copilot credential fill grant",
        route=route,
        page_origin=loggable_origin(url),
        credential_id=credential_id,
        **source_fields,
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


def _missing_totp_card_fallback(credential_name: str) -> str:
    return (
        f"Ask the user in prose to edit the saved credential {defang_card_text(credential_name)} on the "
        "Credentials page and add its authenticator (2FA), then say when it is done. Never ask for the "
        "authenticator secret or a code in chat."
    )


def _password_totp_method(credential: PasswordCredential) -> Literal["authenticator", "out_of_band", "none"]:
    if credential.totp:
        return "authenticator"
    # A saved OTP identifier means the code is delivered out-of-band; only runtime polling has the
    # run/task context needed to resolve it.
    if credential.totp_identifier or credential.totp_type in {TotpType.EMAIL, TotpType.TEXT}:
        return "out_of_band"
    return "none"


def _rejected_credential_card_fallback(credential_name: str) -> str:
    return (
        f"Ask the user in prose to update the saved credential {defang_card_text(credential_name)} on the "
        "Credentials page, then say when it is done. Never ask for a password, secret, or code in chat."
    )


async def _update_ask_target(
    copilot_ctx: CopilotContext, credential_id: str, *, site_rejected: bool = False
) -> tuple[str | None, dict[str, Any] | None]:
    """The credential's saved name when an update card should ask about it, else the tool result.

    A missing-authenticator ask needs a credential with no one-time-code method at all. A site-rejected
    one asks whatever methods it has, since the saved values themselves were turned away."""
    policy = copilot_ctx.request_policy
    # A saved workflow's own binding, read from its row at turn start, is authority to edit that record
    # even when the chat never named it; the card grants no origin, so no fill grant is needed.
    bound_by_saved_workflow = (
        isinstance(policy, RequestPolicy) and credential_id in policy.persisted_workflow_credential_ids
    )
    authority_error = None if bound_by_saved_workflow else _credential_fill_authority_error(copilot_ctx, credential_id)
    if authority_error:
        return None, {"ok": False, "error": authority_error}
    credential_item, load_error = await _load_vault_credential_item(copilot_ctx, credential_id)
    if credential_item is None:
        return None, {"ok": False, "error": load_error}
    credential = credential_item.credential
    if not isinstance(credential, PasswordCredential):
        return None, {"ok": False, "error": f"Credential `{credential_id}` is not a username/password credential."}
    if site_rejected:
        return credential_item.name, None
    method = _password_totp_method(credential)
    if method == "authenticator":
        return None, {
            "ok": True,
            "status": "has_code_method",
            "method": "authenticator",
            "next": (
                f"Fill the code with `fill_credential_field` field=totp for `{credential_id}`, passing the "
                "same `target` that reached the verification step."
            ),
        }
    if method == "out_of_band":
        return None, {
            "ok": True,
            "status": "has_code_method",
            "method": "email_or_text",
            "next": _runtime_otp_steering_error(credential_id),
        }
    return credential_item.name, None


def _log_credential_card_unavailable(copilot_ctx: CopilotContext, config: CopilotConfig | None) -> None:
    LOG.info(
        "copilot_credential_card_unavailable",
        flag_enabled=config is not None and config.credential_pause_enabled,
        client_supports=copilot_ctx.client_supports_credential_pause,
        shared_cache=getattr(getattr(app, "CACHE", None), "is_shared", False),
    )


def raw_secret_connected_credential(copilot_ctx: CopilotContext) -> tuple[str, bool] | None:
    """The card-connected credential's display name and whether the staged draft binds it."""
    policy = copilot_ctx.request_policy
    if policy is None or not policy.raw_secret_redacted_draft:
        return None
    connected_id = copilot_ctx.credential_pause_connected_credential_id
    name = next((c.name for c in policy.resolved_credentials if c.credential_id == connected_id), None)
    if name is None or connected_id is None:
        return None
    bound = connected_id in saved_credential_ids(workflow_credential_ids(copilot_ctx.last_workflow_yaml or ""))
    return defang_card_text(name), bound


async def _request_credential(
    login_page_url: str,
    reason: str,
    copilot_ctx: CopilotContext,
    credential_id: str | None = None,
    rejected_by_site: bool = False,
) -> dict[str, Any]:
    policy = copilot_ctx.request_policy
    if not isinstance(policy, RequestPolicy) or (policy.raw_secret_detected and not policy.raw_secret_redacted_draft):
        return {"ok": False, "error": "Credential selection is unavailable on a raw-secret or ungrounded turn."}
    ask_origin = canonicalize_origin(login_page_url)
    if not is_resolved_page_url(login_page_url) or ask_origin is None:
        return {"ok": False, "error": "Provide the absolute HTTP(S) sign-in page URL for the credential card."}
    if policy.raw_secret_redacted_draft:
        if credential_id:
            return {
                "ok": False,
                "error": "Updating a saved credential (authenticator or rejected values) is unavailable on a raw-secret turn.",
            }
        # Only the user's own site may reach the card on a raw-secret turn: a model or page URL can point elsewhere.
        user_url, _ = _user_provided_site_url_match(policy, login_page_url)
        login_page_url = raw_secret_card_origin(user_url) if user_url else ""
        if not login_page_url:
            return {
                "ok": False,
                "error": (
                    "This turn has a redacted secret, so the credential card opens only for a sign-in site the "
                    "user gave. Call `ask_user` for the site's sign-in URL, then call `request_credential` with it."
                ),
            }

    recovery_open = copilot_ctx.credential_origin_recovery
    # The authenticator update card grants no origin, so it has its own latch and never spends the recovery's.
    recovery = (
        recovery_open
        if not credential_id
        and recovery_open is not None
        and recovery_open.state == "pending"
        and ask_origin.canonical == recovery_open.origin
        else None
    )
    # The spent card, if any, asked for a different site; this ask has a refused fill behind it.
    handback = recovery is not None and recovery.origin not in copilot_ctx.credential_origin_recovery_carded

    update_name: str | None = None
    site_rejected = bool(credential_id) and rejected_by_site
    if credential_id:
        update_name, early_result = await _update_ask_target(copilot_ctx, credential_id, site_rejected=site_rejected)
        if early_result is not None:
            return early_result
    update_ask = update_name is not None

    # The update ask awaits the vault above, so a card raised meanwhile by a parallel call must still win.
    already_asked = copilot_ctx.credential_ask_in_flight or (
        copilot_ctx.credential_totp_update_asked if update_ask else copilot_ctx.credential_pause_used and not handback
    )
    if already_asked and (recovery is None or copilot_ctx.credential_ask_in_flight):
        already: dict[str, Any] = {
            "ok": True,
            "status": "already_asked",
            "next": "Continue without re-asking this turn.",
        }
        # The outcome belongs to the pick card; the update card keeps none.
        if update_name is None:
            already["outcome"] = copilot_ctx.credential_pause_outcome or "unanswered"
        return already

    config = copilot_ctx.copilot_config
    if config is None or not credential_pause_transport_ready(
        copilot_ctx, config, allow_second_ask=update_ask or handback
    ):
        if recovery is not None and not handback:
            return _decline_credential_origin_recovery(copilot_ctx, recovery, "already_asked")
        _log_credential_card_unavailable(copilot_ctx, config)
        if recovery is not None:
            return _decline_credential_origin_recovery(copilot_ctx, recovery, "unavailable")
        return {
            "ok": True,
            "status": "unavailable",
            "detail": "The in-chat credential card cannot be shown on this turn.",
            "fallback": (
                _CREDENTIAL_CARD_FALLBACK
                if update_name is None
                else _rejected_credential_card_fallback(update_name)
                if site_rejected
                else _missing_totp_card_fallback(update_name)
            ),
        }

    admit_connected = None
    if recovery is not None:
        login_page_url = urlunsplit(urlsplit(login_page_url)._replace(query="", fragment=""))
        copilot_ctx.credential_origin_recovery = replace(recovery, state="asked")
        copilot_ctx.credential_origin_recovery_carded.add(recovery.origin)
        admit_connected = partial(_credential_evidence_admits_origin, copilot_ctx, recovery)
    named_before_ask = set(policy.current_turn_named_credential_ids)
    if update_name is None:
        policy.credential_ask_login_page_urls = [login_page_url]
    try:
        resolution = await request_credential_pause(
            copilot_ctx,
            login_page_url=login_page_url,
            message=defang_card_text(reason),
            stream=copilot_ctx.stream,
            copilot_config=config,
            update_credential_id=credential_id if update_name is not None else None,
            update_reason="credential_rejected_by_site" if site_rejected else "credential_missing_totp",
            admit_connected=admit_connected,
            allow_second_ask=handback,
        )
    except BaseException:
        if recovery is not None:
            _decline_credential_origin_recovery(copilot_ctx, recovery, "error")
        raise
    credential = resolution.credential if resolution is not None else None
    if update_name is not None and site_rejected:
        return _rejected_credential_ask_outcome(credential, update_name, answered=resolution)
    if update_name is not None:
        return await _missing_totp_ask_outcome(copilot_ctx, credential, update_name, answered=resolution)
    if recovery is not None:
        bound_origin = (
            canonicalize_origin(policy.live_page_admitted_urls.get(credential.credential_id))
            if credential is not None and copilot_ctx.credential_pause_outcome == "connected"
            else None
        )
        if bound_origin is None or bound_origin.canonical != recovery.origin:
            status: CredentialOriginDeclineStatus
            if resolution is None:
                status = "unanswered"
            elif resolution.action == "skip":
                status = "skipped"
            elif credential is None:
                status = "connected_unresolved"
            else:
                status = "connected_other_site"
            return _decline_credential_origin_recovery(copilot_ctx, recovery, status)
        copilot_ctx.credential_origin_recovery = None
        policy.origin_recovery_kept_named_credential_ids |= named_before_ask & {recovery.refused_credential_id}
        clear_tool_blocker_signals_for_reason_codes(
            copilot_ctx, frozenset({CREDENTIAL_ORIGIN_RECOVERY_PENDING_REASON_CODE})
        )
    if resolution is None:
        return {
            "ok": True,
            "status": "unanswered",
            "outcome": copilot_ctx.credential_pause_outcome or "timeout",
            "next": (
                "The user did not answer the card. Continue without the credential: keep the credential "
                "parameter placeholder in the draft and do not ask again this turn."
            ),
        }
    if resolution.action == "connected" and credential is None:
        return {
            "ok": True,
            "status": "connected_unresolved",
            "next": (
                "The credential the user connected could not be loaded. Continue without it: keep the credential "
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
            RAW_SECRET_CONNECTED_NEXT
            if policy.raw_secret_redacted_draft
            else "Bind this credential as the workflow's credential parameter and continue the build; run the "
            "blocks that were waiting on the login."
        ),
    }


async def _missing_totp_ask_outcome(
    copilot_ctx: CopilotContext,
    credential: Credential | None,
    credential_name: str,
    *,
    answered: CredentialPauseResolution | None,
) -> dict[str, Any]:
    """Report the update card from the saved record, never from the answer alone: a save can leave 2FA unset."""
    not_added_next = (
        f"{defang_card_text(credential_name)} still has no authenticator. Keep it as the workflow's credential "
        "and do not ask again this turn; say the verification step needs an authenticator on that credential "
        "before a run can pass it."
    )
    if credential is None:
        return {"ok": True, "status": "unanswered" if answered is None else "skipped", "next": not_added_next}
    still_missing_name, rechecked = await _update_ask_target(copilot_ctx, credential.credential_id)
    if rechecked is None:
        return {
            "ok": True,
            "status": "saved_without_authenticator",
            "credential_id": credential.credential_id,
            "credential_name": still_missing_name,
            "next": not_added_next,
        }
    if rechecked.get("status") != "has_code_method":
        return rechecked
    return {
        **rechecked,
        "status": "authenticator_added",
        "credential_id": credential.credential_id,
        "credential_name": credential.name,
    }


def _rejected_credential_ask_outcome(
    credential: Credential | None, credential_name: str, *, answered: CredentialPauseResolution | None
) -> dict[str, Any]:
    if credential is None:
        return {
            "ok": True,
            "status": "unanswered" if answered is None else "skipped",
            "next": (
                f"{defang_card_text(credential_name)} was not updated. Keep it as the workflow's credential, do not "
                "ask again this turn, and say the workflow keeps its saved sign-in."
            ),
        }
    return {
        "ok": True,
        "status": "updated",
        "credential_id": credential.credential_id,
        "credential_name": credential.name,
        "next": "The user saved this credential. Re-run the sign-in block to test the updated values.",
    }


def _request_settled_credential(policy: RequestPolicy, credential_id: str) -> bool:
    """A user selection settles identity; a saved binding alone remains scoped to its saved origin."""
    if policy.current_turn_named_credential_ids == {credential_id}:
        return True
    if credential_id in policy.persisted_workflow_credential_ids:
        return credential_id in policy.prior_approved_credential_ids
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
        f"Call `request_credential` with the sign-in page URL on {origin}; the user's card selection "
        "settles both the credential and the site so the build can continue."
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

    async def load_once() -> list[Credential]:
        if copilot_ctx.org_credentials_for_turn is None:
            copilot_ctx.org_credentials_for_turn = await load_credentials(copilot_ctx.organization_id)
        return copilot_ctx.org_credentials_for_turn

    if (
        isinstance(policy, RequestPolicy)
        and credential_id in policy.persisted_workflow_credential_ids
        and credential_id not in {credential.credential_id for credential in policy.resolved_credentials}
    ):
        saved = next((item for item in await load_once() if item.credential_id == credential_id), None)
        if saved is not None:
            policy.resolved_credentials.append(saved)
    authority_error = _credential_fill_authority_error(copilot_ctx, credential_id)

    if not authority_error:
        if not isinstance(policy, RequestPolicy):
            return None, _missing_credential_origin_error(credential_id, None)
        intended_url = _resolved_credential_intended_url(policy, credential_id)
        if intended_url:
            _log_fill_grant("admitted_or_tested", intended_url, credential_id)
            return _CredentialFillOriginGrant(intended_url), None
        page_url = await _live_working_page_url(copilot_ctx) or ""
        if page_url and any(
            _still_on_admitted_site(page_url, origin)
            for origin in workflow_credential_origins(
                copilot_ctx.persisted_workflow_yaml or "", require_canonical_origin=True
            ).get(credential_id, [])
        ):
            _log_fill_grant("saved_workflow", page_url, credential_id)
            return _CredentialFillOriginGrant(page_url), None
        # The vault entry names the site the user filed this credential under, so it answers where
        # the secret belongs without anyone having to run the test flow first.
        if page_url and any(_same_site(page_url, uri) for uri in await _vault_named_sites(copilot_ctx, credential_id)):
            _log_fill_grant("vault_site", page_url, credential_id)
            return _CredentialFillOriginGrant(page_url, whole_site=True), None
        if page_url:
            matched_url, site_level = _user_provided_site_url_match(policy, page_url)
            if matched_url is not None:
                settled = _request_settled_credential(policy, credential_id)
                if not settled and credential_id in policy.persisted_workflow_credential_ids:
                    admission = await admit_credential_for_live_page(
                        policy,
                        organization_id=copilot_ctx.organization_id,
                        credential_id=credential_id,
                        page_url=page_url,
                        load_org_credentials=load_once,
                    )
                    if admission.steer:
                        return None, admission.steer
                if settled or credential_id == await _sole_org_password_credential_id(load_once):
                    # An origin-only match (no registrable site) keeps the origin-scoped grant so the
                    # release guard can still compare it; site matches travel the whole site.
                    _log_fill_grant(
                        "user_url",
                        page_url,
                        credential_id,
                        policy.user_site_url_sources.get(matched_url),
                    )
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


async def _load_vault_credential_item(
    copilot_ctx: AgentContext, credential_id: str
) -> tuple[CredentialItem | None, str]:
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
        return None, f"Could not read credential `{credential_id}`. Ask the user to verify it exists."
    if db_credential is None:
        return None, _missing_credential_reference_tool_error([credential_id])

    vault_type = db_credential.vault_type or CredentialVaultType.BITWARDEN
    credential_service = app.CREDENTIAL_VAULT_SERVICES.get(vault_type)
    if credential_service is None:
        return None, f"The credential vault for `{credential_id}` is not configured on this deployment."
    try:
        return await credential_service.get_credential_item(db_credential), ""
    except Exception as exc:
        LOG.warning(
            "fill_credential_field could not fetch the credential from the vault",
            credential_id=credential_id,
            vault_type=str(vault_type),
            exc_info=True,
        )
        return None, f"Could not fetch credential `{credential_id}` from the vault: {type(exc).__name__}."


MISSING_AUTHENTICATOR = "missing_authenticator"


@dataclass(frozen=True)
class MissingAuthenticator:
    """The resolver's non-prose failure: the credential has no one-time-code method at all."""


def _missing_authenticator_fill_error(
    copilot_ctx: AgentContext, credential_id: str, credential_name: str
) -> dict[str, Any]:
    already_asked = isinstance(copilot_ctx, CopilotContext) and copilot_ctx.credential_totp_update_asked
    if already_asked:
        next_step = (
            "The user was already asked this turn to update this credential. Do not call "
            "`request_credential` again; say the verification step needs an authenticator on it."
        )
    else:
        next_step = (
            f"Call `request_credential` with credential_id `{credential_id}` so the user can add its "
            "authenticator, then retry this fill with the same `target`."
        )
    return {
        "ok": False,
        "status": MISSING_AUTHENTICATOR,
        "error": "The saved credential has no authenticator (2FA) method.",
        "next": next_step,
        "update_ask": "already_asked" if already_asked else "available",
        "data": {"credential_id": credential_id, "credential_name": credential_name, "credential_field": "totp"},
    }


async def _resolve_credential_fill_value(
    copilot_ctx: AgentContext,
    credential_id: str,
    field: str,
) -> tuple[str | None, str, str | MissingAuthenticator | None]:
    """Resolve (secret_value, credential_name, error) for one credential field, server-side only."""
    credential_item, load_error = await _load_vault_credential_item(copilot_ctx, credential_id)
    if credential_item is None:
        return None, "", load_error
    credential = credential_item.credential
    if not isinstance(credential, PasswordCredential):
        return None, "", f"Credential `{credential_id}` is not a username/password credential."

    if field == "username":
        value: str | None = credential.username
    elif field == "password":
        value = credential.password
        register_secret_scrub_value(copilot_ctx, value)
    else:
        method = _password_totp_method(credential)
        if method == "out_of_band":
            return None, credential_item.name, _runtime_otp_steering_error(credential_id)
        if not credential.totp:
            return None, credential_item.name, MissingAuthenticator()
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
        session_id = effective_browser_session_id(copilot_ctx)
        async with mcp_browser_context(copilot_ctx, session_id_override=session_id):
            page, _ = await get_page(session_id=session_id)
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
    async with lock, browser_page_custody_lock(copilot_ctx), browser_evidence_commit_lock(copilot_ctx):
        return await _fill_credential_field_impl_serial(copilot_ctx, selector, credential_id, field, submit_selector)


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

    # A fill aimed at a run's own browser must not provision the chat's; that browser is checked by the fill.
    if effective_browser_session_id(copilot_ctx) == copilot_ctx.browser_session_id:
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
    if isinstance(resolve_error, MissingAuthenticator):
        return finish(_missing_authenticator_fill_error(copilot_ctx, credential_id, credential_name))
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
        session_id = effective_browser_session_id(copilot_ctx)
        async with mcp_browser_context(copilot_ctx, session_id_override=session_id):
            page, _ = await get_page(session_id=session_id)
            await page.fill(
                selector,
                value,
                mode="direct",
                timeout=_CREDENTIAL_FILL_TIMEOUT_MS,
                _direct_fill_release_guard=_credential_fill_release_guard(origin_grant),
            )
            readback = await _read_filled_field_value(page, selector)
            fill_outcome = _scout_readback_outcome(readback, value)
    except _CredentialFillOriginMismatchError as mismatch:
        observed = canonicalize_origin(mismatch.target_url)
        if observed is None or same_release_scope(mismatch.target_url, origin_grant.intended_url):
            return finish({"ok": False, "error": _credential_fill_origin_mismatch_error()})
        return finish(_start_credential_origin_recovery(copilot_ctx, observed.canonical, credential_id))
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
