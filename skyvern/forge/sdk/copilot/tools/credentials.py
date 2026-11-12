from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
import yaml
from fastapi import HTTPException
from pydantic import BaseModel

from skyvern.forge import app
from skyvern.forge.sdk.copilot.blocker_signal import (
    CopilotToolBlockerSignal,
    clear_tool_blocker_signals_for_reason_codes,
    stash_blocker_signal,
)
from skyvern.forge.sdk.copilot.connected_accounts import (
    connection_is_cited,
    listed_connection_ids,
    reference_is_cited,
    request_user_id,
    resolve_connection_reference,
)
from skyvern.forge.sdk.copilot.credential_resolution import (
    credential_reference_spans,
    grounded_references,
    load_credentials,
    loggable_origin,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.secret_scrub import scrub_secrets_from_structure
from skyvern.forge.sdk.copilot.vault_logins import (
    VAULT_LOGIN_REF_PREFIX,
    load_vault_logins,
    vault_login_credential,
)
from skyvern.forge.sdk.copilot.workflow_credential_utils import (
    LEGACY_BITWARDEN_PARAM_ID_PREFIX,
    agent_step_account_text,
    bitwarden_login_parameter_credential_id,
    bitwarden_secret_bindings,
    block_credential_ids,
    credential_param_ids,
    declared_non_credential_keys,
    parse_workflow_yaml,
    saved_credential_ids,
    url_origin,
    workflow_blocks,
    workflow_connection_refs,
    workflow_credential_ids_from_parsed,
)
from skyvern.forge.sdk.copilot.workflow_yaml import dump_workflow_yaml
from skyvern.forge.sdk.credential_site_policy import describe_login_sites
from skyvern.forge.sdk.db.id import GOOGLE_OAUTH_CREDENTIAL_PREFIX, MICROSOFT_OAUTH_CREDENTIAL_PREFIX
from skyvern.forge.sdk.integrations.connected_accounts import (
    IncompleteConnectionRead,
    OrgConnection,
    connected_account_fields,
    org_connections,
    org_connections_by_id,
)
from skyvern.forge.sdk.schemas.copilot_turn_outcome import ConnectedAccountChoice
from skyvern.forge.sdk.schemas.credentials import Credential, CredentialType, CredentialVaultType, TotpType
from skyvern.forge.sdk.services import google_sheets_service
from skyvern.forge.sdk.services.credential.credential_creation_service import VaultItemImport, import_vault_items
from skyvern.forge.sdk.services.credential.credential_vault_service import (
    LISTED_VAULT_TYPES,
    VaultLogin,
    VaultLoginListing,
)
from skyvern.forge.sdk.services.request_principal import get_request_principal, human_user_id
from skyvern.forge.sdk.workflow.models.parameter import WorkflowParameterType
from skyvern.schemas.google_sheets import extract_sheet_gid, extract_spreadsheet_id, is_plain_cell_range
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._shared import _iter_yaml_blocks, _workflow_definition_as_dict
from .banned_blocks import CREDENTIAL_CODE_ACCESSORS, ONE_TIME_CODE_TOTP_TYPES, credential_code_accessors

LOG = structlog.get_logger()


LIST_CREDENTIALS_MAX_PAGE_SIZE = 50
_CREDENTIAL_ID_RE = re.compile(r"\bcred_[A-Za-z0-9][A-Za-z0-9_-]*\b")


def _extract_credential_ids_from_tool_value(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            found.extend(_CREDENTIAL_ID_RE.findall(item))
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
        vault_id = bitwarden_login_parameter_credential_id(parameter)
        if vault_id is not None:
            found.append(vault_id)
            continue
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


_GOOGLE_SHEETS_BLOCK_TYPES = {"google_sheets_read", "google_sheets_write"}
_UNRESOLVED_CONNECTION_SLOT_NOTE = (
    "The slot was left as written, and a slot still holding an unresolved reference cannot run."
)
_UNRESOLVED_CONNECTION_TEXT = {
    "ambiguous": "More than one of the organization's connected accounts matches this reference.",
    "not_found": "None of the organization's connected accounts matches this reference.",
    "inactive": "This reference matches one connected account, but that account is not active.",
    "not_cited": "The user's message for this turn does not name this reference on its own, so it was not resolved.",
    "lookup_failed": (
        "The organization's connected accounts could not be read, so this is not evidence that the account is missing."
    ),
}
UNAPPROVED_CONNECTION_REASON_CODE = "unapproved_connected_account_reference"


def _is_templated_credential_value(value: str) -> bool:
    return "{{" in value or "{%" in value


async def canonicalize_named_connection_bindings(
    workflow_yaml: str,
    ctx: AgentContext,
) -> tuple[str, list[dict[str, Any]]]:
    """Rewrite a name or email the user cited in this turn to the id of the one active connection it resolves to;
    every other reference is left in place and reported as facts."""
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml, []
    if not isinstance(parsed, dict):
        return workflow_yaml, []
    slots = [
        (block, field)
        for block in workflow_blocks(parsed)
        for field in connected_account_fields(block)
        if isinstance((value := block.get(field)), str) and value.strip() and not _is_templated_credential_value(value)
    ]
    if not slots:
        return workflow_yaml, []

    policy = ctx.request_policy
    message = policy.canonical_user_message if isinstance(policy, RequestPolicy) else ""
    try:
        connections: list[OrgConnection] | None = await org_connections(
            ctx.organization_id, request_user_id(ctx.organization_id)
        )
    except Exception:
        LOG.warning(
            "copilot_connection_canonicalization_lookup_failed", organization_id=ctx.organization_id, exc_info=True
        )
        connections = None
    known_ids = {connection.id for connection in connections or []}

    facts: list[dict[str, Any]] = []
    changed = False
    for block, field in slots:
        reference = str(block[field]).strip()
        if reference in known_ids:
            continue
        label = block.get("label")
        fact: dict[str, Any] = {
            "label": label if isinstance(label, str) else None,
            "field": field,
            "reference": reference,
            "canonicalized": False,
        }
        if connections is None:
            fact["status"] = "lookup_failed"
        elif not reference_is_cited(message, reference, connections):
            fact["status"] = "not_cited"
        else:
            resolution = resolve_connection_reference(connections, reference)
            fact["status"] = resolution.status
            fact["candidates"] = [candidate.facts() for candidate in resolution.candidates]
            connection = resolution.connection
            if connection is not None:
                fact.update(connection.facts())
                if connection.active:
                    block[field] = connection.id
                    fact["canonicalized"] = True
                    changed = True
                else:
                    fact["status"] = "inactive"
        if not fact["canonicalized"]:
            fact["active_connections"] = [connection.facts() for connection in connections or [] if connection.active]
            text = _UNRESOLVED_CONNECTION_TEXT.get(fact["status"])
            fact["detail"] = f"{text} {_UNRESOLVED_CONNECTION_SLOT_NOTE}" if text else _UNRESOLVED_CONNECTION_SLOT_NOTE
        facts.append(fact)

    if changed:
        workflow_yaml = dump_workflow_yaml(parsed)
        LOG.info(
            "copilot_connection_name_canonicalized",
            organization_id=ctx.organization_id,
            connection_ids=[fact["connection_id"] for fact in facts if fact["canonicalized"]],
        )
    scrubbed: list[dict[str, Any]] = scrub_secrets_from_structure(ctx, facts)
    return workflow_yaml, scrubbed


_GID_TAB_LOOKUP_TIMEOUT_SECONDS = 5.0


async def _fetch_sheet_tabs(organization_id: str, credential_id: str, url: str) -> list[google_sheets_service.SheetTab]:
    token = await app.AGENT_FUNCTION.get_google_sheets_credentials(organization_id, credential_id)
    if token is None:
        raise RuntimeError("no Google Sheets access token")
    return await google_sheets_service.get_spreadsheet_tabs(token, url)


async def resolve_google_sheet_tabs_from_gid(
    workflow_yaml: str,
    ctx: AgentContext,
) -> tuple[str, list[dict[str, Any]]]:
    """Fill an empty Sheets `sheet_name` with the title of the tab whose id is the `gid` in the block's
    literal `spreadsheet_url`, read through the block's bound connection. Never falls back to another tab."""
    try:
        parsed = safe_load_no_dates(workflow_yaml)
    except yaml.YAMLError:
        return workflow_yaml, []
    if not isinstance(parsed, dict):
        return workflow_yaml, []
    candidates = [
        (block, url, gid)
        for block in workflow_blocks(parsed)
        if block.get("block_type") in _GOOGLE_SHEETS_BLOCK_TYPES
        and not str(block.get("sheet_name") or "").strip()
        and isinstance((url := block.get("spreadsheet_url")), str)
        and not _is_templated_credential_value(url)
        and (gid := extract_sheet_gid(url)) is not None
    ]
    if not candidates:
        return workflow_yaml, []

    try:
        active_ids: set[str] | None = {
            connection.id
            for connection in await org_connections_by_id(ctx.organization_id, request_user_id(ctx.organization_id))
            if connection.active
        }
    except Exception:
        LOG.warning("copilot_google_sheet_tab_lookup_failed", organization_id=ctx.organization_id, exc_info=True)
        active_ids = None

    # Reading tab titles discloses spreadsheet metadata, so it needs the same turn approval dispatch requires.
    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    approved_ids = set(policy.run_approved_connection_ids) if policy is not None else set()
    approved_ids.update(
        await admitted_connection_ids(
            [str(block.get("credential_id")) for block, _, _ in candidates],
            tool_activity=ctx.tool_activity,
            organization_id=ctx.organization_id,
            request_policy=policy,
        )
    )

    # A slow Sheets API must not stall the save: one bounded lookup per spreadsheet, failures included.
    tabs_by_spreadsheet: dict[tuple[str, str], list[google_sheets_service.SheetTab] | None] = {}
    facts: list[dict[str, Any]] = []
    changed = False
    for block, url, gid in candidates:
        label = block.get("label")
        credential_id = block.get("credential_id")
        cell_range = block.get("range")
        fact: dict[str, Any] = {"label": label if isinstance(label, str) else None, "gid": gid, "resolved": False}
        facts.append(fact)
        has_range = cell_range is not None and str(cell_range).strip() != ""
        if has_range and not (isinstance(cell_range, str) and is_plain_cell_range(cell_range)):
            fact.update(status="ineligible", reason="range_not_plain_cells")
            continue
        if not isinstance(credential_id, str) or not credential_id.strip():
            fact["status"] = "no_connection"
            continue
        if _is_templated_credential_value(credential_id):
            fact.update(status="ineligible", reason="templated_credential")
            continue
        if active_ids is None:
            fact["status"] = "lookup_failed"
            continue
        if credential_id not in active_ids:
            fact.update(status="ineligible", reason="not_active_connection")
            continue
        if credential_id not in approved_ids:
            fact.update(status="ineligible", reason="connection_not_approved")
            continue
        try:
            lookup_key = (credential_id, extract_spreadsheet_id(url))
            if lookup_key not in tabs_by_spreadsheet:
                tabs_by_spreadsheet[lookup_key] = None
                tabs_by_spreadsheet[lookup_key] = await asyncio.wait_for(
                    _fetch_sheet_tabs(ctx.organization_id, credential_id, url),
                    timeout=_GID_TAB_LOOKUP_TIMEOUT_SECONDS,
                )
            tabs = tabs_by_spreadsheet[lookup_key]
            if tabs is None:
                raise RuntimeError("tab lookup already failed for this spreadsheet")
        except Exception:
            LOG.warning(
                "copilot_google_sheet_tab_lookup_failed",
                organization_id=ctx.organization_id,
                connection_id=credential_id,
                exc_info=True,
            )
            fact["status"] = "lookup_failed"
            continue
        fact["available_tabs"] = [tab.title for tab in tabs]
        match = next((tab for tab in tabs if tab.sheet_id == gid), None)
        if match is None:
            fact["status"] = "gid_not_found"
            continue
        block["sheet_name"] = match.title
        fact.update(status="resolved", resolved=True, sheet_name=match.title)
        changed = True

    if changed:
        workflow_yaml = dump_workflow_yaml(parsed)
        LOG.info(
            "copilot_google_sheet_tab_resolved_from_gid",
            organization_id=ctx.organization_id,
            labels=[fact["label"] for fact in facts if fact["resolved"]],
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
    declared_keys = declared_non_credential_keys({"workflow_definition": workflow_definition})

    def _scan_value(value: Any, location: str, field: str) -> None:
        if isinstance(value, str):
            for credential_id in _CREDENTIAL_ID_RE.findall(value):
                if credential_id in declared_keys:
                    continue
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
        "Credentials UI and return with its ID."
    )


def _unapproved_credential_reference_tool_error(unapproved_credential_ids: list[str]) -> str:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in unapproved_credential_ids)
    id_word = "ID" if len(unapproved_credential_ids) == 1 else "IDs"
    return (
        "Credential approval blocked this Copilot run before dispatch. "
        f"Reason codes: unapproved_credential_reference. Unapproved credential {id_word}: {formatted_ids}. "
        "Ask the user to select or confirm the saved credential for this request before running the workflow."
        + (
            " A `bitwarden_login_credential` parameter runs only once the saved workflow binds it; bind a "
            "credential from `list_credentials` instead."
            if any(
                credential_id.startswith(LEGACY_BITWARDEN_PARAM_ID_PREFIX)
                for credential_id in unapproved_credential_ids
            )
            else ""
        )
    )


def _saved_workflow_credential_ids(request_policy: RequestPolicy) -> set[str]:
    # Read from the saved workflow row at turn start. Deliberately not the submitted YAML: that is the
    # live canvas, which carries a copilot proposal until the user accepts or rejects it, so a binding
    # the model staged would come back as authority on the next turn.
    return saved_credential_ids(request_policy.persisted_workflow_credential_ids) | {
        credential_id
        for credential_id in request_policy.persisted_workflow_credential_ids
        if credential_id.startswith(LEGACY_BITWARDEN_PARAM_ID_PREFIX)
    }


def _approved_run_credential_ids(request_policy: RequestPolicy | None) -> set[str]:
    if request_policy is None:
        return set()
    resolved = {
        credential.credential_id
        for credential in request_policy.resolved_credentials
        if isinstance(getattr(credential, "credential_id", None), str)
    }
    return resolved | _saved_workflow_credential_ids(request_policy) | set(request_policy.run_approved_connection_ids)


async def admitted_connection_ids(
    refs: Collection[str],
    *,
    tool_activity: Sequence[dict[str, Any]],
    organization_id: str,
    request_policy: RequestPolicy | None,
    listing_admits: Collection[str] | None = None,
) -> set[str]:
    """Bound connections this turn's listing returned or the user named, that belong to the org and are active.
    A listing admits only the refs in `listing_admits` when given; every other ref needs the user to name it."""
    if request_policy is None:
        return set()
    already_approved = set(request_policy.run_approved_connection_ids) - set(request_policy.reads_only_connection_ids)
    candidates = [ref for ref in dict.fromkeys(refs) if ref not in already_approved]
    listed = listed_connection_ids(tool_activity)
    if listing_admits is not None:
        listed &= set(listing_admits)
    message = request_policy.canonical_user_message
    if not candidates or (not listed and not message):
        return set()
    complete = True
    try:
        connections = await org_connections(organization_id, request_user_id(organization_id))
    except IncompleteConnectionRead as incomplete:
        connections, complete = incomplete.connections, False
    except Exception:
        LOG.warning("copilot_connection_authority_lookup_failed", organization_id=organization_id, exc_info=True)
        return set()
    active = {connection.id: connection for connection in connections if connection.active}
    return {
        ref
        for ref in candidates
        if ref in active and (ref in listed or (complete and connection_is_cited(message, active[ref], connections)))
    }


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


def _connection_approval_blocker_signal(
    refs: Collection[str],
    request_policy: RequestPolicy | None,
    *,
    admitted_ids: Collection[str],
    action: str,
    blocked_tool: str,
    acting_refs: Collection[str] = (),
) -> CopilotToolBlockerSignal | None:
    """`acting_refs` need a citation, a card pick or the saved workflow; a reads-only approval does not cover them."""
    approved_ids = _approved_run_credential_ids(request_policy) | set(admitted_ids)
    reads_only = set(request_policy.reads_only_connection_ids) - set(admitted_ids) if request_policy else set()
    unapproved = [
        ref for ref in dict.fromkeys(refs) if ref not in approved_ids or (ref in reads_only and ref in acting_refs)
    ]
    if not unapproved:
        return None
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text="Ask the user to choose one of the server-provided connected accounts.",
        user_facing_reason=f"Choose one of the connected accounts below so I can {action}.",
        recovery_hint="ask_user_clarifying",
        preserves_workflow_draft=True,
        internal_reason_code=UNAPPROVED_CONNECTION_REASON_CODE,
        blocked_tool=blocked_tool,
        # A count, not the references: an unresolved one can be a name or a template the trace does not carry.
        extra={"unapproved_connection_reference_count": len(unapproved)},
    )


def _retire_stale_connection_denial(
    ctx: AgentContext, *, workflow_refs: Collection[str], admitted_ids: set[str]
) -> None:
    """Drop an earlier attempt's denial once nothing it could have named is unapproved any more. The clear removes
    every signal holding this reason code, so any reference still unapproved in the definition or the saved
    workflow keeps them all standing."""
    stale = ctx.blocker_signal
    if (
        not isinstance(stale, CopilotToolBlockerSignal)
        or stale.internal_reason_code != UNAPPROVED_CONNECTION_REASON_CODE
    ):
        return
    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    saved_refs = set(policy.persisted_workflow_connection_ids) if policy is not None else set()
    if (set(workflow_refs) | saved_refs) - _approved_run_credential_ids(policy) - admitted_ids:
        return
    clear_tool_blocker_signals_for_reason_codes(ctx, frozenset({UNAPPROVED_CONNECTION_REASON_CODE}))
    ctx.connected_account_recovery_choices = []


async def connected_account_choices(organization_id: str) -> list[ConnectedAccountChoice] | None:
    try:
        connections = await org_connections_by_id(organization_id, request_user_id(organization_id))
    except Exception:
        LOG.warning("copilot_connected_account_recovery_lookup_failed", organization_id=organization_id, exc_info=True)
        return None
    return [connection.choice() for connection in connections] or None


_ACCOUNT_ID_RE = re.compile(
    rf"(?<![\w-])(?:{GOOGLE_OAUTH_CREDENTIAL_PREFIX}|{MICROSOFT_OAUTH_CREDENTIAL_PREFIX})_[A-Za-z0-9]+(?![\w-])"
)


async def _agent_step_connection_ids(
    organization_id: str,
    parsed: dict[str, Any],
    *,
    selected_labels: set[str] | None,
    run_parameters: dict[str, Any] | None,
) -> list[str]:
    text = agent_step_account_text(parsed, selected_labels=selected_labels, run_parameters=run_parameters)
    if text is None:
        return []
    try:
        connections = await org_connections(organization_id, request_user_id(organization_id))
    except IncompleteConnectionRead:
        # A provider that could not be read may hold any id-shaped token, so every one is checked.
        return list(dict.fromkeys(_ACCOUNT_ID_RE.findall(text)))
    return [
        connection.id
        for connection in connections
        if re.search(rf"(?<![\w-]){re.escape(connection.id)}(?![\w-])", text)
    ]


@dataclass(frozen=True)
class CredentialGateResult:
    error: str | None = None
    blocker: CopilotToolBlockerSignal | None = None
    # Saved-credential ids the executing blocks and run parameters carry, and those the whole definition carries.
    credential_ids: tuple[str, ...] = ()
    definition_credential_ids: tuple[str, ...] = ()


async def run_credential_gate(
    ctx: AgentContext,
    workflow_definition: BaseModel | dict[str, Any],
    *,
    labels: Collection[str] | None,
    run_parameters: dict[str, Any] | None,
    action: str = "run the workflow",
    blocked_tool: str = "update_and_run_blocks",
    listing_admits_reads: bool = True,
) -> CredentialGateResult:
    """Every credential and connected account a dispatch would run with must carry this chat's approval. `labels`
    names the blocks that may execute; None means the whole definition. This turn's listing admits an account only
    for fields that read, and not at all without `listing_admits_reads`. An account id an executing agent step can
    read counts as acting, since the step can send through it."""
    definition = _workflow_definition_as_dict(workflow_definition)
    parsed = {"workflow_definition": definition}
    parameters = run_parameters if isinstance(run_parameters, dict) else None
    workflow_refs = workflow_connection_refs(parsed, run_parameters=parameters)
    selected = set(labels or ())
    resolved_labels = {block.get("label") for block in workflow_blocks(parsed, selected_labels=selected)}
    executing_labels = selected if selected and selected <= resolved_labels else None
    executing_refs = workflow_connection_refs(parsed, selected_labels=executing_labels, run_parameters=parameters)
    agent_refs = await _agent_step_connection_ids(
        ctx.organization_id, parsed, selected_labels=executing_labels, run_parameters=parameters
    )
    executing_refs = list(dict.fromkeys(executing_refs + agent_refs))
    acting_refs = (
        workflow_connection_refs(parsed, selected_labels=executing_labels, run_parameters=parameters, acting_only=True)
        + agent_refs
    )
    policy = ctx.request_policy if isinstance(ctx.request_policy, RequestPolicy) else None
    admitted = await admitted_connection_ids(
        executing_refs,
        tool_activity=ctx.tool_activity,
        organization_id=ctx.organization_id,
        request_policy=policy,
        listing_admits=set(executing_refs) - set(acting_refs) if listing_admits_reads else (),
    )
    blocker = _connection_approval_blocker_signal(
        executing_refs,
        policy,
        admitted_ids=admitted,
        action=action,
        blocked_tool=blocked_tool,
        acting_refs=acting_refs if listing_admits_reads else executing_refs,
    )
    if blocker is not None:
        ctx.connected_account_recovery_choices = await connected_account_choices(ctx.organization_id) or []
        return CredentialGateResult(error=stash_blocker_signal(ctx, blocker), blocker=blocker)
    _retire_stale_connection_denial(ctx, workflow_refs=workflow_refs, admitted_ids=admitted)

    # An id an executing connected-account field resolves to was gated above, even when a credential parameter
    # carries it; a field in a block that does not run gates nothing, so its id still needs credential approval.
    gated_refs = set(executing_refs)
    connection_refs = set(workflow_refs)
    tool_ids = _extract_credential_ids_from_tool_value(run_parameters or {})
    definition_ids = (
        _extract_credential_ids_for_labels(definition, labels)
        if labels is not None
        else _extract_credential_ids_from_workflow_definition(definition)
    )
    approval_ids = [i for i in dict.fromkeys(tool_ids + definition_ids) if i not in gated_refs]
    approval_error = _credential_run_approval_error(approval_ids, policy)
    if approval_error is not None:
        return CredentialGateResult(error=approval_error)
    return CredentialGateResult(
        credential_ids=tuple(approval_ids),
        definition_credential_ids=tuple(
            i
            for i in dict.fromkeys(tool_ids + _extract_credential_ids_from_workflow_definition(definition))
            if i not in connection_refs
        ),
    )


async def import_vault_login(organization_id: str, login: VaultLogin) -> str:
    """The saved credential id for a listed vault login, importing it first when it has none."""
    if login.credential_id:
        return login.credential_id
    try:
        [(credential, _created)] = await import_vault_items(
            organization_id,
            login.vault_type,
            [
                VaultItemImport(
                    vault_id=login.vault_id,
                    item_id=login.item_id,
                    name=login.title,
                    credential_type=CredentialType.PASSWORD,
                    tested_url=login.tested_url,
                )
            ],
            created_by=human_user_id(principal, organization_id) if (principal := get_request_principal()) else None,
        )
    except HTTPException as exc:
        raise PermissionError(f"vault login import refused with status {exc.status_code}") from exc
    return credential.credential_id


async def import_bound_vault_logins(workflow_yaml: str, ctx: AgentContext) -> tuple[str, str | None]:
    """Import each listed vault login the draft binds and bind its saved credential in its place. Nothing is
    imported unless each login's own sites cover every block URL it is bound at, and it is bound at one."""
    parsed = parse_workflow_yaml(workflow_yaml)
    definition = parsed.get("workflow_definition") if isinstance(parsed, dict) else None
    if not isinstance(definition, dict):
        return workflow_yaml, None
    params_by_key = credential_param_ids(definition.get("parameters"))
    block_urls: dict[str, set[str]] = {}
    for block in workflow_blocks(parsed):
        url = block.get("url")
        if isinstance(url, str) and url.strip():
            for credential_id in block_credential_ids(block, params_by_key):
                block_urls.setdefault(credential_id, set()).add(url.strip())
    policy = ctx.request_policy
    prior_origins = policy.existing_workflow_credential_origins if isinstance(policy, RequestPolicy) else {}
    refs = sorted(
        credential_id
        for credential_id in workflow_credential_ids_from_parsed(parsed)
        if credential_id.startswith(VAULT_LOGIN_REF_PREFIX)
    )
    new_placements = {
        credential_id: urls
        for credential_id in block_urls
        if credential_id.startswith(("cred_", LEGACY_BITWARDEN_PARAM_ID_PREFIX))
        and (
            urls := {
                url for url in block_urls[credential_id] if url_origin(url) not in prior_origins.get(credential_id, [])
            }
        )
    }
    if not refs and not new_placements:
        return workflow_yaml, None
    logins, listing = await load_vault_logins(ctx.organization_id, policy)
    # A `bitwarden_login_credential` parameter names an item in the connected Bitwarden vault.
    logins = logins | {
        f"{LEGACY_BITWARDEN_PARAM_ID_PREFIX}{login.item_id}": login
        for login in logins.values()
        if login.vault_type == CredentialVaultType.BITWARDEN_CONNECTED
    }
    unread_parameters = [
        credential_id
        for credential_id in new_placements
        if credential_id.startswith(LEGACY_BITWARDEN_PARAM_ID_PREFIX) and credential_id not in logins
    ]
    if unread_parameters:
        return workflow_yaml, (
            "A `bitwarden_login_credential` parameter is bound at a new site, and its item is not a login the "
            "connected Bitwarden vault lists now, so the websites it is saved for cannot be checked. Keep it on the "
            "sites the workflow already used it on, or bind a credential from `list_credentials` instead."
        )
    unlisted = [ref for ref in refs if ref not in logins]
    if unlisted:
        return workflow_yaml, (
            f"{', '.join(f'`{ref}`' for ref in unlisted)} is not a login `list_credentials` lists now. "
            "Call `list_credentials` and bind a `credential_id` it returns."
        )
    pageless = [ref for ref in refs if not block_urls.get(ref)]
    if pageless:
        return workflow_yaml, (
            f"{', '.join(f'`{ref}`' for ref in pageless)} is not bound at any block with a literal login page URL, "
            "so its vault sites cannot be checked. Bind it at the block whose `url` is the sign-in page."
        )
    # An imported login without a tested_url is scoped by its vault sites alone, so no tested_url check covers it.
    placements = {ref: block_urls[ref] for ref in refs} | {
        credential_id: urls
        for credential_id, urls in new_placements.items()
        if credential_id in logins and logins[credential_id].tested_url is None
    }
    off_site = sorted(
        (credential_id, url)
        for credential_id, urls in placements.items()
        for url in urls
        if not any(site.admits(url) for site in logins[credential_id].sites)
    )
    if off_site:
        return workflow_yaml, " ".join(
            [
                *(
                    f"`{credential_id}` is bound at {loggable_origin(url) or url}, but as saved in its vault it fills "
                    f"only on {describe_login_sites(logins[credential_id].sites)}."
                    for credential_id, url in off_site
                ),
                "Bind a vault login only at a block on a site it fills on.",
            ]
        )
    unlisted_ids = [credential_id for credential_id in new_placements if credential_id not in logins]
    unseen_vaults = LISTED_VAULT_TYPES if listing.hidden else frozenset(listing.unreadable)
    if unseen_vaults and unlisted_ids:
        unchecked = [
            credential.credential_id
            for credential in await load_credentials(ctx.organization_id, credential_ids=unlisted_ids)
            if credential.vault_type in unseen_vaults and credential.tested_url is None
        ]
        if unchecked:
            names = ", ".join(f"`{credential_id}`" for credential_id in unchecked)
            if listing.hidden:
                return workflow_yaml, (
                    f"{names} comes from a vault whose logins credential folder access hides from this user, so the "
                    "websites it is saved for cannot be checked at a new site. Keep it on the sites the workflow "
                    "already uses it on, or ask a user no folder restricts to make this change."
                )
            return workflow_yaml, (
                f"{names} comes from a vault that cannot be read now, so the websites it is saved for cannot be "
                "checked. Ask the user to check that vault's connection on the Integrations page."
            )
    if not refs:
        return workflow_yaml, None
    imported: dict[str, str] = {}
    for ref in refs:
        try:
            imported[ref] = await import_vault_login(ctx.organization_id, logins[ref])
        except PermissionError:
            return workflow_yaml, (
                f"`{ref}` cannot be imported from the vault for this user, so it cannot be bound. Bind a saved "
                "`credential_id` from `list_credentials`, or ask an admin to import the login."
            )
        if isinstance(policy, RequestPolicy):
            policy.adopt_imported_vault_login(ref, imported[ref])
    for parameter in definition.get("parameters") or []:
        if isinstance(parameter, dict) and parameter.get("key") in params_by_key:
            for field in ("credential_id", "default_value"):
                value = parameter.get(field)
                if isinstance(value, str):
                    parameter[field] = imported.get(value, value)
            credential_ids = parameter.get("credential_ids")
            if isinstance(credential_ids, list):
                parameter["credential_ids"] = [
                    imported.get(item, item) if isinstance(item, str) else item for item in credential_ids
                ]
    for block in workflow_blocks(parsed):
        value = block.get("credential_id")
        if isinstance(value, str):
            block["credential_id"] = imported.get(value, value)
    return dump_workflow_yaml(parsed), None


async def run_input_vault_placement_error(
    workflow_definition: object, run_parameters: object, ctx: AgentContext
) -> str | None:
    """A run input binds a credential as a parameter default does, so it gets the same vault-site placement check."""
    if not isinstance(run_parameters, dict) or not run_parameters:
        return None
    refs = [
        value
        for value in run_parameters.values()
        if isinstance(value, str) and value.startswith(VAULT_LOGIN_REF_PREFIX)
    ]
    if refs:
        return (
            f"{', '.join(f'`{ref}`' for ref in refs)} is a vault login that is not a saved credential yet, so a run "
            "input cannot carry it. Bind it in the workflow, which imports it once its sites are checked."
        )
    parsed = {"workflow_definition": copy.deepcopy(_workflow_definition_as_dict(workflow_definition))}
    for parameter in parsed["workflow_definition"].get("parameters") or []:
        key = parameter.get("key") if isinstance(parameter, dict) else None
        if isinstance(key, str) and isinstance(run_parameters.get(key), str):
            parameter["default_value"] = run_parameters[key]
    # A stored definition lists each block's parameters instead of their keys.
    for block in workflow_blocks(parsed):
        embedded = block.get("parameters")
        if "parameter_keys" not in block and isinstance(embedded, list):
            block["parameter_keys"] = [item["key"] for item in embedded if isinstance(item, dict) and "key" in item]
    _, error = await import_bound_vault_logins(dump_workflow_yaml(parsed), ctx)
    return error


async def _credential_ids_validation_error(credential_ids: list[str], ctx: AgentContext) -> str | None:
    if not credential_ids:
        return None
    # A `bitwarden_login_credential` parameter binds a vault item, not a row; the run gate decides it.
    password_credential_ids = [
        credential_id
        for credential_id in credential_ids
        if not credential_id.startswith(LEGACY_BITWARDEN_PARAM_ID_PREFIX)
    ]
    try:
        existing_credentials = (
            await load_credentials(ctx.organization_id, credential_ids=password_credential_ids)
            if password_credential_ids
            else []
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
            "Ask the user to provide/select a valid credential ID."
        )

    found_password_ids = {credential.credential_id for credential in existing_credentials}
    missing_password_ids = [
        credential_id for credential_id in password_credential_ids if credential_id not in found_password_ids
    ]
    if missing_password_ids:
        return _missing_credential_reference_tool_error(missing_password_ids)
    return None


def bitwarden_binding_change_error(workflow_yaml: str, policy: RequestPolicy | None) -> str | None:
    existing = policy.existing_bitwarden_secret_bindings if policy is not None else set()
    if not bitwarden_secret_bindings(workflow_yaml) - existing:
        return None
    return (
        "Copilot cannot add or change a `bitwarden_login_credential`, `bitwarden_sensitive_information` or "
        "`bitwarden_credit_card_data` parameter. Bind a login from `list_credentials` as a `credential` "
        "parameter instead; the user can add a Bitwarden parameter in the workflow editor."
    )


async def _credential_reference_validation_error(value: Any, ctx: AgentContext) -> str | None:
    if not isinstance(value, str):
        return await _credential_ids_validation_error(_extract_credential_ids_from_tool_value(value), ctx)
    parsed = parse_workflow_yaml(value)
    connection_refs = set(workflow_connection_refs(parsed)) if isinstance(parsed, dict) else set()
    return await _credential_ids_validation_error(
        [i for i in _extract_credential_ids_from_workflow_yaml(value) if i not in connection_refs], ctx
    )


def _serialize_credential(credential: Credential) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "credential_id": credential.credential_id,
        "name": credential.name,
        "credential_type": str(credential.credential_type),
        "tested_url": credential.tested_url,
    }
    if credential.extra_site_urls:
        entry["also_saved_for"] = list(credential.extra_site_urls)
    accessors = credential_code_accessors(credential.credential_type, credential.totp_type)
    if accessors:
        entry["code"] = {"workflow_parameter_type": "credential_id", "accessors": list(accessors)}
    if credential.username:
        entry["username"] = credential.username
        entry["totp_type"] = str(credential.totp_type) if credential.totp_type else None
        if credential.totp_identifier:
            entry["totp_identifier"] = credential.totp_identifier
        if credential.totp_type in ONE_TIME_CODE_TOTP_TYPES:
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
                    "accessor": CREDENTIAL_CODE_ACCESSORS[CredentialType.PASSWORD].otp,
                },
            }
    elif credential.card_last4:
        entry["card_last_four"] = credential.card_last4
        entry["card_brand"] = credential.card_brand
    elif credential.secret_label:
        entry["secret_label"] = credential.secret_label
    return entry


_DENIED_PASS_ROUTES = ["typed_resume", "request_credential_tool", "literal_credential_id"]


def _typed_resume_arm(reference: str, policy: RequestPolicy, matches: list[Credential]) -> str | None:
    """Which server record already answered which credential this reference names, if any. Each arm is
    a server-owned record of an identifier, never an interpretation of prose: the user settled the id
    this turn, the server bound it from a login page it admitted, or the user approved it earlier in
    this chat. ``matches`` is what the live inventory resolves the reference to.
    """

    def keyed(credential: Credential) -> bool:
        # A name on a stored record can have moved to another credential since it was written, so an
        # arm vouches only for the one the live inventory resolved. Passing when the inventory did not
        # resolve exactly one leaves the not-found and ambiguous answers below to report themselves.
        return len(matches) != 1 or matches[0].credential_id == credential.credential_id

    named_this_turn = policy.current_turn_named_credential_ids | policy.credential_pause_connected_credential_ids
    if any(
        credential.credential_id in named_this_turn
        and reference in {credential.credential_id, credential.name}
        and keyed(credential)
        for credential in policy.resolved_credentials
    ):
        return "user_named_this_turn"
    if any(
        reference in {credential.credential_id, credential.name} and keyed(credential)
        for credential in policy.auto_bound_credentials
    ):
        return "server_auto_bound"
    # Durable approval records the user's own answer, not a page's vouch; the recorder skips a
    # page-stamped id except the carried proposal a citation passed, which keeps its admitting origin.
    # It is recorded by id, so it reads the resolved id directly rather than any stored name.
    if len(matches) == 1 and matches[0].credential_id in policy.prior_approved_credential_ids:
        return "user_approved_earlier_turn"
    return None


async def _unimported_vault_logins(ctx: AgentContext) -> tuple[list[tuple[str, VaultLogin]], VaultLoginListing]:
    logins, listing = await load_vault_logins(ctx.organization_id, ctx.request_policy)
    return [(ref, login) for ref, login in logins.items() if login.credential_id is None], listing


def _unseen_vault_login_facts(listing: VaultLoginListing | None) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if listing is None:
        return facts
    if listing.unreadable:
        facts["unreadable_vaults"] = [str(vault_type) for vault_type in listing.unreadable]
    if listing.hidden:
        facts["vault_logins_hidden_by_folder_access"] = True
    return facts


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

    saved_credentials = await load_credentials(ctx.organization_id)
    # A login title can look like a saved credential id, so only a real one skips the vault listing.
    vault_logins, listing = (
        ([], None)
        if any(credential.credential_id == reference for credential in saved_credentials)
        else await _unimported_vault_logins(ctx)
    )
    vault_credentials = [vault_login_credential(ctx.organization_id, ref, login) for ref, login in vault_logins]
    # A vault login is named by its title only: its ref is an internal id, never something a user types.
    grounded = grounded_references(
        policy.canonical_user_message,
        [
            *(value for credential in saved_credentials for value in (credential.name, credential.credential_id)),
            *(credential.name for credential in vault_credentials),
        ],
    )
    matches_by_id = {
        credential.credential_id: credential
        for credential in [*saved_credentials, *vault_credentials]
        if credential.name == reference
        or (credential.credential_id == reference and not reference.startswith(VAULT_LOGIN_REF_PREFIX))
    }
    matches = list(matches_by_id.values())
    literal_reference = bool(credential_reference_spans(policy.canonical_user_message, reference))
    typed_resume_arm = _typed_resume_arm(reference, policy, matches)
    if len(matches) == 1 and matches[0].credential_id in _saved_workflow_credential_ids(policy):
        typed_resume_arm = typed_resume_arm or "saved_workflow"
    # The agent owns natural-language interpretation. This boundary verifies only
    # objective provenance and identity: an exact reference is grounded in the current
    # turn, a structured selection, or the server-saved workflow. It deliberately
    # does not implement a second English policy language beside the agent.
    if typed_resume_arm is None and reference not in grounded and (matches or not literal_reference):
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
        data: dict[str, Any] = {
            "status": "ambiguous" if matches else "not_found",
            "reference": reference,
            "candidates": [_serialize_credential(credential) for credential in matches],
            **_unseen_vault_login_facts(listing),
        }
        if not matches:
            withdrawn = policy.record_named_login_not_found()
            data["page_login_binding"] = "off_for_this_turn"
            data["withdrawn_credential_ids"] = withdrawn
            LOG.info("copilot_named_login_not_found", withdrawn_credential_ids=withdrawn)
        return {"ok": False, "data": data}

    credential = matches[0]
    policy.resolved_credentials = [
        *[item for item in policy.resolved_credentials if item.credential_id != credential.credential_id],
        credential,
    ]
    # A literal span the grounded check dropped — a shorter saved name sitting inside a longer one the
    # user actually typed — is not the user naming this credential; no arm here already implies grounded.
    user_named = typed_resume_arm is None or reference in grounded
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

    vault_logins, listing = await _unimported_vault_logins(ctx)
    page = max(1, params.get("page", 1))
    page_size = min(max(1, params.get("page_size") or 10), LIST_CREDENTIALS_MAX_PAGE_SIZE)
    credentials = await load_credentials(ctx.organization_id, page=page, page_size=page_size)
    serialized = [_serialize_credential(credential) for credential in credentials]
    _record_discovered_credentials_on_policy(ctx, credentials)
    data: dict[str, Any] = {
        "credentials": serialized,
        "vault_logins": [
            {
                "credential_id": ref,
                "name": login.title,
                "site": loggable_origin(login.urls[0]) if login.urls else None,
            }
            for ref, login in vault_logins[(page - 1) * page_size : page * page_size]
        ],
        "page": page,
        "page_size": page_size,
        "count": len(serialized),
        "has_more": len(serialized) == page_size or len(vault_logins) > page * page_size,
        **_unseen_vault_login_facts(listing),
    }
    return {"ok": True, "data": data}


def _record_discovered_credentials_on_policy(ctx: AgentContext, credentials: list[Credential]) -> None:
    request_policy = ctx.request_policy
    if not isinstance(request_policy, RequestPolicy):
        return
    known_ids = {credential.credential_id for credential in request_policy.discovered_credentials}
    for credential in credentials:
        if credential.credential_id not in known_ids:
            request_policy.discovered_credentials.append(credential)
