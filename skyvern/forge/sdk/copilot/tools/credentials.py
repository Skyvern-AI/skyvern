from __future__ import annotations

import re
from typing import Any

import structlog
import yaml

from skyvern.forge import app
from skyvern.forge.sdk.copilot.credential_resolution import (
    credential_reference_spans,
    grounded_credential_references,
    load_credentials,
)
from skyvern.forge.sdk.copilot.request_policy import RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.workflow_credential_utils import workflow_blocks
from skyvern.forge.sdk.schemas.credentials import Credential
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

    return list(dict.fromkeys(found))


def _extract_credential_ids_from_workflow_definition(workflow_definition: Any) -> list[str]:
    definition = _workflow_definition_as_dict(workflow_definition)
    found = _extract_credential_ids_from_workflow_parameters(definition.get("parameters"))
    for block in workflow_blocks({"workflow_definition": definition}):
        found.extend(_extract_credential_ids_from_workflow_parameters(block.get("parameters")))
        found.extend(_extract_credential_ids_from_tool_value(block.get("credential_id")))
    return list(dict.fromkeys(found))


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
        "Stop before creating, updating, or running the workflow. Ask the user to provide/select a valid "
        "credential ID, create the credential in the Credentials UI and return with its ID, or explicitly "
        "choose an unvalidated draft workflow that will not be run until credentials are available."
    )


def _unapproved_credential_reference_tool_error(unapproved_credential_ids: list[str]) -> str:
    formatted_ids = ", ".join(f"`{credential_id}`" for credential_id in unapproved_credential_ids)
    id_word = "ID" if len(unapproved_credential_ids) == 1 else "IDs"
    return (
        "Credential approval blocked this Copilot run before dispatch. "
        f"Reason codes: unapproved_credential_reference. Unapproved credential {id_word}: {formatted_ids}. "
        "Ask the user to select or confirm the saved credential for this request before running the workflow."
    )


def _approved_run_credential_ids(request_policy: RequestPolicy | None) -> set[str]:
    if request_policy is None:
        return set()
    return {
        credential.credential_id
        for credential in request_policy.resolved_credentials
        if isinstance(getattr(credential, "credential_id", None), str)
    }


def _credential_run_approval_error(credential_ids: list[str], request_policy: RequestPolicy | None) -> str | None:
    if not credential_ids:
        return None
    approved_ids = _approved_run_credential_ids(request_policy)
    unapproved_ids = [credential_id for credential_id in credential_ids if credential_id not in approved_ids]
    if not unapproved_ids:
        return None
    return _unapproved_credential_reference_tool_error(unapproved_ids)


async def _credential_ids_validation_error(credential_ids: list[str], ctx: AgentContext) -> str | None:
    if not credential_ids:
        return None
    try:
        existing_credentials = await app.DATABASE.credentials.get_credentials_by_ids(
            credential_ids,
            organization_id=ctx.organization_id,
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

    found_ids = {credential.credential_id for credential in existing_credentials}
    missing_ids = [credential_id for credential_id in credential_ids if credential_id not in found_ids]
    if not missing_ids:
        return None
    return _missing_credential_reference_tool_error(missing_ids)


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
    elif credential.card_last4:
        entry["card_last_four"] = credential.card_last4
        entry["card_brand"] = credential.card_brand
    elif credential.secret_label:
        entry["secret_label"] = credential.secret_label
    return entry


def _reference_is_typed_resume(reference: str, policy: RequestPolicy) -> bool:
    return any(
        credential.credential_id in policy.current_turn_named_credential_ids
        and reference in {credential.credential_id, credential.name}
        for credential in policy.resolved_credentials
    )


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
    typed_resume = _reference_is_typed_resume(reference, policy)
    # The agent owns natural-language interpretation. This boundary verifies only
    # objective provenance and identity: the proposed exact reference must be a
    # complete saved reference in the literal current turn. It deliberately does
    # not implement a second English policy language beside the agent.
    if not typed_resume and reference not in grounded_references and (matches or not literal_reference):
        return {
            "ok": False,
            "data": {
                "status": "denied",
                "reference": reference,
                "reason": "reference_not_literal_in_current_user_turn",
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
    policy.current_turn_named_credential_ids.add(credential.credential_id)
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
