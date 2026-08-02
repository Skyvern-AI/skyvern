from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog

from skyvern.cli.core.session_manager import get_page
from skyvern.forge import app
from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.credential_resolution import load_credentials, url_parts
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy, admit_credential_for_live_page
from skyvern.forge.sdk.copilot.runtime import AgentContext, ensure_browser_session, mcp_browser_context
from skyvern.forge.sdk.copilot.secret_scrub import (
    REDACTED_SECRET_PLACEHOLDER,
    register_secret_scrub_value,
    scrub_secrets_from_text,
)
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialVaultType, PasswordCredential, TotpType
from skyvern.forge.sdk.services.credentials import generate_totp_code, normalize_totp_config

from .banned_blocks import _copilot_block_authoring_policy
from .blockers import _tool_loop_error
from .credentials import _missing_credential_reference_tool_error
from .guardrails import _authority_tool_error
from .mcp_hooks import _verify_scout_type_landed
from .scouting import (
    _capture_element_fingerprint,
    _capture_enclosing_form_submits,
    _capture_post_interaction_screenshot,
    _capture_scout_source_url,
    _clear_pending_browser_interaction_observation,
    _consume_scout_source_url,
    _live_working_page_url,
    _mark_pending_browser_interaction_observation,
    _record_scouted_interaction,
    _register_scout_interaction_observation,
    _resolve_scout_role_name,
)

LOG = structlog.get_logger()

_CREDENTIAL_FILL_FIELDS = frozenset({"username", "password", "totp"})
_CREDENTIAL_FILL_TIMEOUT_MS = 15000


@dataclass(frozen=True)
class _CredentialFillOriginGrant:
    intended_url: str


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
    if not isinstance(policy, RequestPolicy) or not policy.allow_run_blocks:
        return (
            "Saved-credential scouting is not authorized for this request. "
            "Ask the user for the required credential or clarification before filling credential fields."
        )
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


class _CredentialFillOriginMismatchError(Exception):
    pass


def _credential_fill_origin_mismatch_error() -> str:
    return (
        "The browser left this credential's intended login origin before it could be filled. "
        "Re-inspect the current page and fill again if the sign-in is still in progress there."
    )


def _credential_fill_release_guard(intended_url: str) -> Callable[[str | None], None]:
    """Bind the fill grant to the resolved element's document at the release seam."""

    def guard(target_url: str | None) -> None:
        if not _still_on_admitted_site(target_url, intended_url):
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


def _missing_credential_origin_error(credential_id: str) -> str:
    return (
        f"Credential `{credential_id}` has no intended login origin, so it cannot be filled into the live browser. "
        "Test the saved credential against its login page to bind it to that site, then inspect the page and retry."
    )


async def _credential_fill_origin_grant(
    copilot_ctx: AgentContext, credential_id: str
) -> tuple[_CredentialFillOriginGrant | None, str | None]:
    """Authorize a fill only when it has a concrete intended origin.

    The returned grant is the capability consumed at the release seam. A credential that reached
    ``resolved_credentials`` through a path without either a tested URL or a live-page admission
    receives no grant, so a missing origin cannot silently disable the final comparison.
    """
    prerequisite_error = _credential_fill_prerequisite_error(copilot_ctx, credential_id)
    if prerequisite_error:
        return None, prerequisite_error
    policy = copilot_ctx.request_policy
    authority_error = _credential_fill_authority_error(copilot_ctx, credential_id)
    if not authority_error:
        if not isinstance(policy, RequestPolicy):
            return None, _missing_credential_origin_error(credential_id)
        intended_url = _resolved_credential_intended_url(policy, credential_id)
        if not intended_url:
            # API/import-created credentials remain unbound until tested against a login page.
            # Denying that onboarding state is intentional: there is no origin safe to release to.
            return None, _missing_credential_origin_error(credential_id)
        return _CredentialFillOriginGrant(intended_url), None

    if not isinstance(policy, RequestPolicy):
        return None, authority_error

    async def load_once() -> list[Credential]:
        if copilot_ctx.org_credentials_for_turn is None:
            copilot_ctx.org_credentials_for_turn = await load_credentials(copilot_ctx.organization_id)
        return copilot_ctx.org_credentials_for_turn

    admission = await admit_credential_for_live_page(
        policy,
        organization_id=copilot_ctx.organization_id,
        credential_id=credential_id,
        page_url=await _live_working_page_url(copilot_ctx) or "",
        load_org_credentials=load_once,
    )
    if admission.admitted and admission.page_url:
        return _CredentialFillOriginGrant(admission.page_url), None
    if admission.admitted:
        return None, _missing_credential_origin_error(credential_id)
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
                return None, "", _runtime_otp_steering_error(credential_id)
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


async def _fill_credential_field_impl(
    copilot_ctx: AgentContext,
    selector: str,
    credential_id: str,
    field: str,
) -> dict[str, Any]:
    arguments = {"selector": selector, "credential_id": credential_id, "field": field}

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        record_tool_step_result_for_ctx(copilot_ctx, "fill_credential_field", arguments, result)
        return result

    loop_error = _tool_loop_error(copilot_ctx, "fill_credential_field", arguments)
    if loop_error:
        return {"ok": False, "error": loop_error}
    authority_error = _authority_tool_error(copilot_ctx, "fill_credential_field")
    if authority_error:
        return finish({"ok": False, "error": authority_error})

    selector = (selector or "").strip()
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
        return finish({"ok": False, "error": policy_error or _missing_credential_origin_error(credential_id)})

    value, credential_name, resolve_error = await _resolve_credential_fill_value(copilot_ctx, credential_id, field)
    if resolve_error or value is None:
        return finish({"ok": False, "error": resolve_error or "Could not resolve the credential value."})

    session_error = await ensure_browser_session(copilot_ctx)
    if session_error:
        return finish(session_error)
    await _capture_scout_source_url(copilot_ctx)
    try:
        async with mcp_browser_context(copilot_ctx):
            page, _ = await get_page(session_id=copilot_ctx.browser_session_id)
            await page.fill(
                selector,
                value,
                mode="direct",
                timeout=_CREDENTIAL_FILL_TIMEOUT_MS,
                _direct_fill_release_guard=_credential_fill_release_guard(origin_grant.intended_url),
            )
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

    _clear_pending_browser_interaction_observation(copilot_ctx)
    source_url = _consume_scout_source_url(copilot_ctx)
    landing_failure = await _verify_scout_type_landed(copilot_ctx, selector=selector, typed_length=len(value))
    if landing_failure is not None:
        LOG.info(
            "copilot fill_credential_field did not land",
            selector=selector,
            credential_id=credential_id,
            field=field,
        )
        return finish(landing_failure)
    url = await _live_working_page_url(copilot_ctx) or ""
    _mark_pending_browser_interaction_observation(copilot_ctx, tool_name="fill_credential_field", url=url)
    role, accessible_name = await _resolve_scout_role_name(copilot_ctx, selector)
    fingerprint = await _capture_element_fingerprint(copilot_ctx, selector)
    _record_scouted_interaction(
        copilot_ctx,
        tool_name="fill_credential_field",
        selector=selector,
        source_url=source_url,
        typed_length=len(value),
        role=role,
        accessible_name=accessible_name,
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
    observation_step, _ = await _register_scout_interaction_observation(
        copilot_ctx, tool_name="fill_credential_field", selector=selector, source_url=source_url, url=url
    )
    data: dict[str, Any] = {
        "selector": selector,
        "credential_id": credential_id,
        "field": field,
        "typed_length": len(value),
        "url": url,
    }
    form_submits = await _capture_enclosing_form_submits(copilot_ctx, selector)
    if form_submits:
        data["form_submit_controls"] = form_submits
    await _capture_post_interaction_screenshot(copilot_ctx)
    result: dict[str, Any] = {"ok": True, "data": data}
    if observation_step is not None:
        result["observation_step"] = observation_step
        data["observation_step"] = observation_step
    LOG.info(
        "copilot fill_credential_field filled a saved credential field",
        selector=selector,
        credential_id=credential_id,
        field=field,
        typed_length=len(value),
        url=url or None,
    )
    return finish(result)
