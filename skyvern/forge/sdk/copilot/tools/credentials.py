from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from typing import Any

import structlog
import yaml

from skyvern.forge import app
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.credential_resolution import (
    credential_reference_spans,
    grounded_credential_references,
    grounded_references,
    load_credentials,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    block_credential_ids,
    credential_param_ids,
    saved_credential_ids,
    workflow_blocks,
)
from skyvern.forge.sdk.copilot.workflow_yaml import dump_workflow_yaml
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice
from skyvern.forge.sdk.schemas.credentials import Credential, TotpType
from skyvern.forge.sdk.schemas.google_oauth import GoogleOAuthCredentialBase
from skyvern.forge.sdk.services import google_oauth_service
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._shared import _iter_yaml_blocks, _workflow_definition_as_dict

LOG = structlog.get_logger()


_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")


def _extract_credential_ids_from_tool_value(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            found.extend(_CREDENTIAL_ID_RE.findall(item))
            # Google connection IDs are accepted only as an exact structured slot value. Do not
            # search prose for them: account authority comes from a verified click, a persisted
            # workflow binding, or a selected native Sheets binding cited from this turn's
            # server-owned integration result. This extractor is only the later validation seam.
            if item.startswith("goac_"):
                found.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple, set)):
            for nested in item:
                visit(nested)
        elif hasattr(item, "model_dump"):
            try:
                visit(item.model_dump(mode="json"))
            except Exception:
                return

    visit(value)
    return list(dict.fromkeys(found))


def _credential_parameter_slot_field(parameter: Any) -> str | None:
    """Return the field name that legitimately carries a `cred_xxx` value for
    this parameter dict, or None if the parameter is not a credential-binding
    slot. Two shapes resolve a credential at runtime: a top-level or block-level
    `parameter_type: credential` with the ID in `credential_id`, and a
    `parameter_type: workflow` + `workflow_parameter_type: credential_id` with
    the ID in `default_value`.
    """
    if not isinstance(parameter, dict):
        return None
    parameter_type = str(parameter.get("parameter_type") or "").lower()
    if parameter_type == "credential":
        return "credential_id"
    workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
    if parameter_type == "workflow" and workflow_parameter_type == WorkflowParameterType.CREDENTIAL_ID.value:
        return "default_value"
    return None


def _extract_credential_ids_from_workflow_parameters(parameters: Any) -> list[str]:
    if not isinstance(parameters, list):
        return []

    found: list[str] = []
    for parameter in parameters:
        slot_field = _credential_parameter_slot_field(parameter)
        if slot_field is None:
            continue
        found.extend(_extract_credential_ids_from_tool_value(parameter.get(slot_field)))
        if slot_field == "credential_id":
            found.extend(_extract_credential_ids_from_tool_value(parameter.get("credential_ids")))
            found.extend(_extract_credential_ids_from_tool_value(parameter.get("fallback_credential_ids")))

    return list(dict.fromkeys(found))


def _extract_credential_ids_from_workflow_definition(workflow_definition: Any) -> list[str]:
    definition = _workflow_definition_as_dict(workflow_definition)
    found = _extract_credential_ids_from_workflow_parameters(definition.get("parameters"))
    for block in workflow_blocks({"workflow_definition": definition}):
        found.extend(_extract_credential_ids_from_workflow_parameters(block.get("parameters")))
        found.extend(_extract_credential_ids_from_tool_value(block.get("credential_id")))
    return list(dict.fromkeys(found))


def _extract_credential_ids_for_labels(
    workflow_definition: Any,
    labels: Collection[str],
    *,
    excluded_block_types: Collection[str] = (),
) -> list[str]:
    """Credential IDs reachable from the blocks named by `labels` and their descendants, plus any
    top-level credential parameter no block claims; falls back to the whole-document set when the
    label set is empty or a label does not resolve. Sound only while the runtime exposes a
    credential solely to blocks that declare it (WorkflowRunContext.credential_template_entries)."""
    definition = _workflow_definition_as_dict(workflow_definition)
    selected_labels = set(labels)
    if not selected_labels:
        return _extract_credential_ids_from_workflow_definition(definition)

    parsed = {"workflow_definition": definition}
    selected_blocks = workflow_blocks(parsed, selected_labels=selected_labels)
    if selected_labels - {block.get("label") for block in selected_blocks}:
        return _extract_credential_ids_from_workflow_definition(definition)

    credential_params_by_key = credential_param_ids(definition.get("parameters"))
    excluded_types = set(excluded_block_types)

    def block_ids(block: dict[str, Any]) -> list[str]:
        return _extract_credential_ids_from_workflow_parameters(block.get("parameters")) + sorted(
            block_credential_ids(block, credential_params_by_key)
        )

    claimed_by_any_block = {credential_id for block in workflow_blocks(parsed) for credential_id in block_ids(block)}
    found = [
        credential_id
        for credential_id in _extract_credential_ids_from_workflow_parameters(definition.get("parameters"))
        if credential_id not in claimed_by_any_block
    ]
    for block in selected_blocks:
        if block.get("block_type") in excluded_types:
            continue
        found.extend(block_ids(block))
    return list(dict.fromkeys(found))


def _google_sheet_connection_bindings_from_workflow_definition(
    workflow_definition: Any,
    *,
    selected_labels: Collection[str] | None = None,
) -> list[tuple[str, str]]:
    definition = _workflow_definition_as_dict(workflow_definition)
    return [
        (label, connection_id)
        for block in workflow_blocks(
            {"workflow_definition": definition},
            selected_labels=set(selected_labels) if selected_labels is not None else None,
        )
        if block.get("block_type") in {"google_sheets_read", "google_sheets_write"}
        and isinstance((label := block.get("label")), str)
        and isinstance((connection_id := block.get("credential_id")), str)
    ]


_GOOGLE_SHEETS_BLOCK_TYPES = {"google_sheets_read", "google_sheets_write"}
_GOOGLE_CONNECTION_PREFIX = "goac_"


def _is_templated_credential_value(value: str) -> bool:
    return "{{" in value or "{%" in value


_TEMPLATE_KEY_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _connection_labels(connections: Sequence[GoogleOAuthCredentialBase]) -> list[str]:
    return [
        label.casefold()
        for connection in connections
        for label in (connection.credential_name, connection.email_address)
        if label
    ]


def _google_connection_reference_is_cited(message: str, reference: str, labels: Sequence[str]) -> bool:
    """Verify a citation; never interpret one. The exact literal must stand as its own token in the
    current turn, compared the way the resolver matches rows: casefold. A label that only occurs
    inside a longer sibling label ("Marketing" within "Marketing Archive") is not cited on its own."""
    if not message or not reference:
        return False
    return reference.casefold() in grounded_references(message.casefold(), [reference.casefold(), *labels])


def _named_google_sheet_blocks(
    parsed: dict[str, Any],
    *,
    selected_labels: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Sheets blocks whose `credential_id` still holds an unresolved connection reference.
    Saved-credential ids are excluded because they carry the saved-credential approval error instead."""
    return [
        block
        for block in workflow_blocks(parsed, selected_labels=selected_labels)
        if block.get("block_type") in _GOOGLE_SHEETS_BLOCK_TYPES
        and isinstance((reference := block.get("credential_id")), str)
        and reference.strip()
        and not reference.startswith(_GOOGLE_CONNECTION_PREFIX)
        and not _is_templated_credential_value(reference)
        and not _CREDENTIAL_ID_RE.fullmatch(reference.strip())
    ]


def _unbacked_templated_google_sheet_slots(parsed: dict[str, Any], labels: Collection[str]) -> list[str]:
    """Templated Sheets `credential_id` slots not backed by credential-typed workflow parameters.
    A slot is backed only when it is a bare `{{ key }}` for such a parameter; a dotted path, a
    filter, or a `{% %}` expression names no parameter the boundary can vouch for, so it renders at
    run time with no citation, approval, state, or scope check and carries no authority of its own."""
    definition = parsed.get("workflow_definition")
    parameters = definition.get("parameters") if isinstance(definition, dict) else None
    credential_keys = set(credential_param_ids(parameters))
    return [
        reference
        for block in workflow_blocks(parsed, selected_labels=set(labels))
        if block.get("block_type") in _GOOGLE_SHEETS_BLOCK_TYPES
        and isinstance((reference := block.get("credential_id")), str)
        and _is_templated_credential_value(reference)
        and not (
            _TEMPLATE_KEY_RE.fullmatch(reference.strip())
            and set(_TEMPLATE_KEY_RE.findall(reference)) <= credential_keys
        )
    ]


def _google_connection_reference_ids(workflow_definition: Any, labels: Collection[str]) -> list[str]:
    parsed = {"workflow_definition": _workflow_definition_as_dict(workflow_definition)}
    return list(
        dict.fromkeys(
            [
                *(
                    str(block["credential_id"]).strip()
                    for block in _named_google_sheet_blocks(parsed, selected_labels=set(labels))
                ),
                *_unbacked_templated_google_sheet_slots(parsed, labels),
            ]
        )
    )


def _connection_row_facts(credential: GoogleOAuthCredentialBase) -> dict[str, Any]:
    return {
        "connection_id": credential.id,
        "name": credential.credential_name,
        "email_address": credential.email_address,
        "state": credential.state,
        "scopes_granted": list(credential.scopes_granted),
    }


async def canonicalize_named_google_sheet_bindings(
    workflow_yaml: str,
    ctx: AgentContext,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite a cited Google connection name in a Sheets `credential_id` slot to its stored id.

    Only a literal the current user turn contains, resolving to exactly one active Sheets-scoped
    row, is rewritten; every other reference is left in place and reported as facts."""
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml, []
    if not isinstance(parsed, dict):
        return workflow_yaml, []
    named_blocks = _named_google_sheet_blocks(parsed)
    if not named_blocks:
        return workflow_yaml, []

    policy = ctx.request_policy
    message = policy.canonical_user_message if isinstance(policy, RequestPolicy) else ""
    try:
        visible: list[GoogleOAuthCredentialBase] | None = await google_oauth_service.get_visible_credentials_for_org(
            ctx.organization_id
        )
    except Exception:
        LOG.warning(
            "copilot_google_connection_canonicalization_lookup_failed",
            organization_id=ctx.organization_id,
            exc_info=True,
        )
        visible = None

    eligible = [
        credential
        for credential in (visible or [])
        if credential.state == google_oauth_service.STATE_ACTIVE
        and google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE in credential.scopes_granted
    ]
    eligible_ids = {credential.id for credential in eligible}

    facts: list[dict[str, Any]] = []
    changed = False
    for block in named_blocks:
        reference = str(block["credential_id"]).strip()
        label = block.get("label")
        fact: dict[str, Any] = {
            "label": label if isinstance(label, str) else None,
            "provider": "google",
            "reference": reference,
            "canonicalized": False,
        }
        if visible is None:
            fact["status"] = "lookup_failed"
        elif not _google_connection_reference_is_cited(message, reference, _connection_labels(visible)):
            fact["status"] = "not_cited"
        else:
            resolution = google_oauth_service.resolve_connection_reference(visible, reference)
            fact["status"] = resolution.status
            fact["candidates"] = [_connection_row_facts(candidate) for candidate in resolution.candidates]
            credential = resolution.credential
            if credential is not None:
                fact["connection_id"] = credential.id
                fact["state"] = credential.state
                fact["scopes_granted"] = list(credential.scopes_granted)
                if credential.id in eligible_ids:
                    block["credential_id"] = credential.id
                    fact["canonicalized"] = True
                    changed = True
                else:
                    fact["status"] = "ineligible"
        if not fact["canonicalized"]:
            fact["eligible_connections"] = [_connection_row_facts(candidate) for candidate in eligible]
        facts.append(fact)

    if changed:
        workflow_yaml = dump_workflow_yaml(parsed)
        LOG.info(
            "copilot_google_connection_name_canonicalized",
            organization_id=ctx.organization_id,
            connection_ids=[fact["connection_id"] for fact in facts if fact["canonicalized"]],
        )
    scrubbed: list[dict[str, Any]] = scrub_secrets_from_structure(ctx, facts)
    return workflow_yaml, scrubbed


def _parsed_workflow_definition(workflow_yaml: str | None) -> dict[str, Any] | None:
    if not workflow_yaml:
        return None
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return None
    return workflow_definition


def _extract_credential_ids_from_workflow_yaml(workflow_yaml: str | None) -> list[str]:
    workflow_definition = _parsed_workflow_definition(workflow_yaml)
    if workflow_definition is None:
        return []
    return _extract_credential_ids_from_workflow_definition(workflow_definition)


_MISBINDING_WORKFLOW_LOCATION = "workflow"


def _credential_id_misbinding_findings(workflow_yaml: str | None) -> list[dict[str, str]]:
    workflow_definition = _parsed_workflow_definition(workflow_yaml)
    if workflow_definition is None:
        return []

    findings: list[dict[str, str]] = []

    def _scan_value(value: Any, location: str, field: str) -> None:
        if isinstance(value, str):
            for credential_id in _CREDENTIAL_ID_RE.findall(value):
                findings.append({"location": location, "field": field, "credential_id": credential_id})
        elif isinstance(value, list):
            for item in value:
                _scan_value(item, location, field)
        elif isinstance(value, dict):
            for nested_field, nested_value in value.items():
                _scan_value(nested_value, location, str(nested_field))

    def _scan_parameter(parameter: Any, location: str) -> None:
        if not isinstance(parameter, dict):
            return
        legal_slot_field = _credential_parameter_slot_field(parameter)
        legal_slot_fields = {legal_slot_field} if legal_slot_field else set()
        if legal_slot_field == "credential_id":
            legal_slot_fields.add("credential_ids")
        for field_name, field_value in parameter.items():
            if field_name in legal_slot_fields:
                continue
            _scan_value(field_value, location, str(field_name))

    for parameter in workflow_definition.get("parameters") or []:
        _scan_parameter(parameter, _MISBINDING_WORKFLOW_LOCATION)

    for block in _iter_yaml_blocks(workflow_definition.get("blocks")):
        label = str(block.get("label") or "<unlabeled>")
        for field_name, field_value in block.items():
            if field_name == "parameters":
                if isinstance(field_value, list):
                    for parameter in field_value:
                        _scan_parameter(parameter, label)
                continue
            if field_name == "loop_blocks":
                continue
            _scan_value(field_value, label, str(field_name))

    return findings


def _missing_credential_reference_tool_error(missing_credential_ids: list[str]) -> str:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in missing_credential_ids)
    id_word = "ID" if len(missing_credential_ids) == 1 else "IDs"
    was_word = "was" if len(missing_credential_ids) == 1 else "were"
    return (
        f"The credential {id_word} {formatted_ids} {was_word} not found in this organization. "
        "Stop before creating, updating, or running the workflow. Call `request_credential` with the sign-in "
        "page URL so the user can add or pick one in chat, or, failing that, ask them to create it in the "
        "Credentials UI and return with its ID, or explicitly choose an unvalidated draft workflow that will "
        "not be run until credentials are available."
    )


def _unapproved_credential_reference_tool_error(unapproved_credential_ids: list[str]) -> str:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in unapproved_credential_ids)
    id_word = "ID" if len(unapproved_credential_ids) == 1 else "IDs"
    return (
        "Credential approval blocked this Copilot run before dispatch. "
        f"Reason codes: unapproved_credential_reference. Unapproved credential {id_word}: {formatted_ids}. "
        "Ask the user to select or confirm the saved credential for this request before running the workflow."
    )


def _saved_workflow_credential_ids(request_policy: RequestPolicy) -> set[str]:
    # Read from the saved workflow row at turn start. Deliberately not the submitted YAML: that is the
    # live canvas, which carries a copilot proposal until the user accepts or rejects it, so a binding
    # the model staged would come back as authority on the next turn.
    return saved_credential_ids(request_policy.persisted_workflow_credential_ids)


def _approved_run_credential_ids(request_policy: RequestPolicy | None) -> set[str]:
    if request_policy is None:
        return set()
    resolved = {
        credential.credential_id
        for credential in request_policy.resolved_credentials
        if isinstance(getattr(credential, "credential_id", None), str)
    }
    return (
        resolved
        | _saved_workflow_credential_ids(request_policy)
        | set(request_policy.run_approved_google_connection_ids)
    )


async def _approve_server_verified_google_sheet_bindings(
    bindings: Collection[tuple[str, str]],
    *,
    tool_activity: Sequence[dict[str, Any]],
    organization_id: str,
    request_policy: RequestPolicy | None,
) -> list[str]:
    """Admit selected Sheets citations backed by this turn's server-owned listing or user citation."""
    if request_policy is None:
        return []

    listed_ids = _same_turn_listed_google_sheet_ids(tool_activity)
    canonical_message = request_policy.canonical_user_message
    already_approved = set(request_policy.run_approved_google_connection_ids)
    candidates = [
        connection_id
        for connection_id in dict.fromkeys(connection_id for _, connection_id in bindings)
        if connection_id not in already_approved
    ]
    if not candidates or (not listed_ids and not canonical_message):
        return []
    try:
        visible_connections = await google_oauth_service.get_visible_credentials_for_org(organization_id)
    except Exception:
        LOG.warning(
            "copilot cited Google Sheets binding authority lookup failed",
            organization_id=organization_id,
            exc_info=True,
        )
        return []

    # Citation is judged against every visible row, the same set author time sees, so a longer
    # sibling name in the error state still shadows the shorter one; only active rows are eligible.
    eligible = [
        connection
        for connection in visible_connections
        if connection.state == google_oauth_service.STATE_ACTIVE
        and google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE in connection.scopes_granted
    ]
    eligible_ids = {connection.id for connection in eligible}
    return [
        connection_id
        for connection_id in candidates
        if connection_id in eligible_ids
        and (
            connection_id in listed_ids
            or _connection_is_cited_by_the_user(connection_id, eligible, visible_connections, canonical_message)
        )
    ]


def _same_turn_listed_google_sheet_ids(tool_activity: Sequence[dict[str, Any]]) -> set[str]:
    for activity in reversed(tool_activity):
        if activity.get("tool") != "list_integrations":
            continue
        integrations = activity.get("integrations")
        if not isinstance(integrations, list):
            return set()
        return {
            connection_id
            for integration in integrations
            if isinstance(integration, dict)
            and integration.get("provider") == "google"
            and integration.get("state") == "active"
            and isinstance((connection_id := integration.get("connection_id")), str)
            and isinstance(integration.get("scopes_granted"), list)
            and google_oauth_service.GOOGLE_SHEETS_DATA_SCOPE in integration["scopes_granted"]
        }
    return set()


def _connection_is_cited_by_the_user(
    connection_id: str,
    eligible: Sequence[GoogleOAuthCredentialBase],
    known: Sequence[GoogleOAuthCredentialBase],
    canonical_message: str,
) -> bool:
    """The user's own verbatim naming of a connection stands in for a same-turn listing, but only
    when that literal resolves to this one row across every known row: a name shared with a
    connection that merely lacks the Sheets scope is still ambiguous, and admits nothing."""
    bound = next((connection for connection in eligible if connection.id == connection_id), None)
    if bound is None:
        return False
    labels = _connection_labels(known)
    for label in (bound.credential_name, bound.email_address):
        if not label or not _google_connection_reference_is_cited(canonical_message, label, labels):
            continue
        resolution = google_oauth_service.resolve_connection_reference(known, label)
        if resolution.credential is not None and resolution.credential.id == connection_id:
            return True
    return False


def _credential_run_approval_error(
    credential_ids: list[str],
    request_policy: RequestPolicy | None,
    *,
    additional_approved_ids: Collection[str] = (),
) -> str | None:
    if not credential_ids:
        return None
    approved_ids = _approved_run_credential_ids(request_policy) | set(additional_approved_ids)
    unapproved_ids = [credential_id for credential_id in credential_ids if credential_id not in approved_ids]
    if not unapproved_ids:
        return None
    return _unapproved_credential_reference_tool_error(unapproved_ids)


def _credential_run_approval_blocker_signal(
    credential_ids: list[str],
    request_policy: RequestPolicy | None,
    *,
    additional_approved_ids: Collection[str] = (),
    google_reference_ids: Collection[str] = (),
) -> CopilotToolBlockerSignal | None:
    approved_ids = _approved_run_credential_ids(request_policy) | set(additional_approved_ids)
    references = [
        *(credential_id for credential_id in credential_ids if credential_id.startswith(_GOOGLE_CONNECTION_PREFIX)),
        *google_reference_ids,
    ]
    unapproved_google_ids = [reference for reference in dict.fromkeys(references) if reference not in approved_ids]
    if not unapproved_google_ids:
        return None
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Ask the user to choose one of the server-provided connected Google accounts.",
        user_facing_reason=(
            "Choose one of the connected Google accounts below so I can run the workflow. "
            "Reconnect any unavailable account on the Integrations page first."
        ),
        recovery_hint="ask_user_clarifying",
        preserves_workflow_draft=True,
        internal_reason_code="unapproved_google_connection_reference",
        blocked_tool="update_and_run_blocks",
    )


async def _server_verified_google_account_choices(
    organization_id: str,
) -> list[ConnectedAccountChoice] | None:
    try:
        visible = await google_oauth_service.get_visible_credentials_for_org(organization_id)
    except Exception:
        LOG.warning(
            "copilot_connected_account_recovery_lookup_failed",
            organization_id=organization_id,
            exc_info=True,
        )
        return None
    choices = [
        ConnectedAccountChoice(
            connection_id=credential.id,
            name=credential.credential_name,
            state=credential.state,
            email_address=credential.email_address,
        )
        for credential in visible
    ]
    return choices or None


async def _credential_ids_validation_error(credential_ids: list[str], ctx: AgentContext) -> str | None:
    if not credential_ids:
        return None
    google_connection_ids = [credential_id for credential_id in credential_ids if credential_id.startswith("goac_")]
    password_credential_ids = [
        credential_id for credential_id in credential_ids if not credential_id.startswith("goac_")
    ]
    try:
        existing_credentials = (
            await app.DATABASE.credentials.get_credentials_by_ids(
                password_credential_ids,
                organization_id=ctx.organization_id,
            )
            if password_credential_ids
            else []
        )
        active_google_connections = (
            await google_oauth_service.get_credentials_for_org(ctx.organization_id) if google_connection_ids else []
        )
    except Exception:
        LOG.warning(
            "Copilot tool failed to validate credential IDs",
            organization_id=ctx.organization_id,
            credential_ids=credential_ids,
            exc_info=True,
        )
        return (
            "Credential ID validation failed, so the workflow cannot be created, updated, or run safely. "
            "Ask the user to provide/select a valid credential ID or explicitly choose an unvalidated draft "
            "workflow that will not be run until credentials are available."
        )

    found_password_ids = {credential.credential_id for credential in existing_credentials}
    missing_password_ids = [
        credential_id for credential_id in password_credential_ids if credential_id not in found_password_ids
    ]
    if missing_password_ids:
        return _missing_credential_reference_tool_error(missing_password_ids)
    active_google_ids = {connection.id for connection in active_google_connections}
    if any(connection_id not in active_google_ids for connection_id in google_connection_ids):
        return (
            "The selected Google account is unavailable or needs to be reconnected. "
            "Stop before running the workflow and ask the user to reconnect or select an active account "
            "on the Integrations page."
        )
    return None


async def _credential_reference_validation_error(value: Any, ctx: AgentContext) -> str | None:
    if isinstance(value, str):
        credential_ids = _extract_credential_ids_from_workflow_yaml(value)
    else:
        credential_ids = _extract_credential_ids_from_tool_value(value)
    return await _credential_ids_validation_error(credential_ids, ctx)


def _serialize_credential(credential: Credential) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "credential_id": credential.credential_id,
        "name": credential.name,
        "credential_type": str(credential.credential_type),
        "tested_url": credential.tested_url,
    }
    if credential.username:
        entry["username"] = credential.username
        entry["totp_type"] = str(credential.totp_type) if credential.totp_type else None
        if credential.totp_identifier:
            entry["totp_identifier"] = credential.totp_identifier
        if credential.totp_type in {TotpType.AUTHENTICATOR, TotpType.EMAIL, TotpType.TEXT}:
            scouting: dict[str, Any]
            if credential.totp_type == TotpType.AUTHENTICATOR:
                scouting = {
                    "tool": "fill_credential_field",
                    "credential_id": credential.credential_id,
                    "field": "totp",
                }
            else:
                scouting = {"available": False, "reason": "workflow_run_context_required"}
            entry["one_time_code"] = {
                "available": True,
                "source": str(credential.totp_type),
                "scouting": scouting,
                "code": {
                    "workflow_parameter_type": "credential_id",
                    "accessor": "await <credential_parameter_key>.otp()",
                },
            }
    elif credential.card_last4:
        entry["card_last_four"] = credential.card_last4
        entry["card_brand"] = credential.card_brand
    elif credential.secret_label:
        entry["secret_label"] = credential.secret_label
    return entry


_DENIED_PASS_ROUTES = ["typed_resume", "request_credential_tool", "literal_credential_id"]


def _typed_resume_arm(reference: str, policy: RequestPolicy) -> str | None:
    """Which server record already answered which credential this reference names, if any. Both arms
    are server-owned records of an identifier, never an interpretation of prose: the user settled the
    id this turn, or the server bound it from a login page it admitted.
    """
    if any(
        credential.credential_id in policy.current_turn_named_credential_ids
        and reference in {credential.credential_id, credential.name}
        for credential in policy.resolved_credentials
    ):
        return "user_named_this_turn"
    if any(reference in {credential.credential_id, credential.name} for credential in policy.auto_bound_credentials):
        return "server_auto_bound"
    return None


async def _resolve_exact_credential(reference: str, ctx: AgentContext) -> dict[str, Any]:
    policy = ctx.request_policy
    if not isinstance(policy, RequestPolicy):
        return {
            "ok": False,
            "data": {
                "status": "denied",
                "reference": reference,
                "reason": "canonical_user_request_missing",
            },
        }

    credentials = await load_credentials(ctx.organization_id)
    grounded_references = grounded_credential_references(policy.canonical_user_message, credentials)
    matches_by_id = {
        credential.credential_id: credential
        for credential in credentials
        if credential.credential_id == reference or credential.name == reference
    }
    matches = list(matches_by_id.values())
    literal_reference = bool(credential_reference_spans(policy.canonical_user_message, reference))
    typed_resume_arm = _typed_resume_arm(reference, policy)
    # The agent owns natural-language interpretation. This boundary verifies only
    # objective provenance and identity: the proposed exact reference must be a
    # complete saved reference in the literal current turn. It deliberately does
    # not implement a second English policy language beside the agent.
    if typed_resume_arm is None and reference not in grounded_references and (matches or not literal_reference):
        LOG.info(
            "copilot_credential_reference_not_literal",
            reference_is_saved_credential=bool(matches),
            named_credential_id_count=len(policy.current_turn_named_credential_ids),
            auto_bound_credential_id_count=len(policy.auto_bound_credentials),
            pass_routes=_DENIED_PASS_ROUTES,
        )
        return {
            "ok": False,
            "data": {
                "status": "denied",
                "reference": reference,
                "reason": "reference_not_literal_in_current_user_turn",
                "pass_routes": _DENIED_PASS_ROUTES,
            },
        }
    if len(matches) != 1:
        status = "not_found" if not matches else "ambiguous"
        return {
            "ok": False,
            "data": {
                "status": status,
                "reference": reference,
                "candidates": [_serialize_credential(credential) for credential in matches],
            },
        }

    credential = matches[0]
    policy.resolved_credentials = [
        *[item for item in policy.resolved_credentials if item.credential_id != credential.credential_id],
        credential,
    ]
    user_named = typed_resume_arm is None or reference in grounded_references or literal_reference
    if user_named:
        policy.current_turn_named_credential_ids.add(credential.credential_id)
        # A carried origin answers where the secret belongs only while nobody better has. The user
        # naming the credential for a site they gave this turn is better, and the fill seam reads the
        # origin ahead of every other route, so a stale one would refuse the site they just named.
        if credential.credential_id in policy.seeded_proposal_credential_ids:
            policy.live_page_admitted_urls.pop(credential.credential_id, None)
    if typed_resume_arm == "server_auto_bound" and credential.credential_id in policy.seeded_proposal_credential_ids:
        # The carry passed this citation. Whether it stands as the user's answer is decided at turn
        # end, when what they settled is final — the card resume and a second resolve both write that
        # set after this point, so deciding here would read it before the answer landed.
        policy.carry_cited_credential_ids.add(credential.credential_id)
    LOG.info(
        "copilot_credential_reference_resolved",
        credential_id=credential.credential_id,
        pass_arm=typed_resume_arm or "literal_in_current_user_turn",
    )
    return {
        "ok": True,
        "data": {
            "status": "resolved",
            "reference": reference,
            "credential": _serialize_credential(credential),
        },
    }


async def _list_credentials(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    exact_reference = params.get("exact_reference")
    if exact_reference is not None:
        if not isinstance(exact_reference, str) or not exact_reference:
            return {"ok": False, "error": "exact_reference must be a non-empty exact saved name or credential ID"}
        return await _resolve_exact_credential(exact_reference, ctx)

    page = max(1, params.get("page", 1))
    page_size = min(max(1, params.get("page_size") or 10), 50)
    credentials = await app.DATABASE.credentials.get_credentials(
        organization_id=ctx.organization_id,
        page=page,
        page_size=page_size,
    )
    serialized = [_serialize_credential(credential) for credential in credentials]
    _record_discovered_credentials_on_policy(ctx, credentials)
    return {
        "ok": True,
        "data": {
            "credentials": serialized,
            "page": page,
            "page_size": page_size,
            "count": len(serialized),
            "has_more": len(serialized) == page_size,
        },
    }


def _record_discovered_credentials_on_policy(ctx: AgentContext, credentials: list[Credential]) -> None:
    request_policy = ctx.request_policy
    if not isinstance(request_policy, RequestPolicy):
        return
    known_ids = {credential.credential_id for credential in request_policy.discovered_credentials}
    for credential in credentials:
        if credential.credential_id not in known_ids:
            request_policy.discovered_credentials.append(credential)
