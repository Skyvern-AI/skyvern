from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from skyvern.forge.sdk.browser_action_policy import canonicalize_origin
from skyvern.forge.sdk.copilot.workflow_block_traversal import workflow_block_locations
from skyvern.forge.sdk.workflow.models.parameter import ParameterType
from skyvern.utils.yaml_loader import safe_load_no_dates

URL_CANDIDATE_RE = re.compile(r"\b(?:https?://[^\s)>,]+|www\.[^\s)>,]+)", re.IGNORECASE)
# The shape of every issued credential ID (see generate_credential_id).
_ISSUED_CREDENTIAL_ID_RE = re.compile(r"cred_\d+")
# A login in the organization's connected Bitwarden vault, named by its item id. It is never a row in the
# credentials table, so every seam that reads one must branch on this prefix before a table or vault lookup.
CONNECTED_VAULT_ID_PREFIX = "bwitem_"
# The placeholders the workflow editor writes; at run time the organization's connected account is tried first.
BITWARDEN_LOGIN_PARAMETER_SECRET_KEYS = {
    "bitwarden_client_id_aws_secret_key": "SKYVERN_BITWARDEN_CLIENT_ID",
    "bitwarden_client_secret_aws_secret_key": "SKYVERN_BITWARDEN_CLIENT_SECRET",
    "bitwarden_master_password_aws_secret_key": "SKYVERN_BITWARDEN_MASTER_PASSWORD",
}


def connected_vault_credential_id(item_id: str) -> str:
    return f"{CONNECTED_VAULT_ID_PREFIX}{item_id}"


def connected_vault_item_id(credential_id: str) -> str | None:
    if not credential_id.startswith(CONNECTED_VAULT_ID_PREFIX):
        return None
    return credential_id.removeprefix(CONNECTED_VAULT_ID_PREFIX)


def bitwarden_login_parameter_credential_id(parameter: Any) -> str | None:
    """The connected-vault id a `bitwarden_login_credential` parameter binds, else None. A missing, templated or
    key-referencing item id still yields an id, so the binding stays visible to approval and validation."""
    if not isinstance(parameter, dict):
        return None
    if str(parameter.get("parameter_type") or "").lower() != ParameterType.BITWARDEN_LOGIN_CREDENTIAL:
        return None
    item_id = parameter.get("bitwarden_item_id")
    return connected_vault_credential_id(item_id.strip() if isinstance(item_id, str) else "")


# Bitwarden identity and card bindings. Copilot lists and approves only logins, so it never adds or edits these.
_BITWARDEN_SECRET_PARAMETER_TYPES = frozenset(
    {ParameterType.BITWARDEN_SENSITIVE_INFORMATION.value, ParameterType.BITWARDEN_CREDIT_CARD_DATA.value}
)
_BITWARDEN_SECRET_BINDING_FIELDS = (
    "bitwarden_collection_id",
    "bitwarden_item_id",
    "bitwarden_identity_key",
    "bitwarden_identity_fields",
    *BITWARDEN_LOGIN_PARAMETER_SECRET_KEYS,
)


def bitwarden_secret_bindings(workflow_yaml: str) -> set[str]:
    parsed = parse_workflow_yaml(workflow_yaml) if workflow_yaml else None
    if not isinstance(parsed, dict):
        return set()
    return {
        json.dumps(
            {
                "parameter_type": parameter_type,
                **{name: parameter.get(name) for name in _BITWARDEN_SECRET_BINDING_FIELDS},
            },
            sort_keys=True,
        )
        for parameter in declared_parameters(parsed)
        if isinstance(parameter, dict)
        and (parameter_type := str(parameter.get("parameter_type") or "").lower()) in _BITWARDEN_SECRET_PARAMETER_TYPES
    }


def parse_workflow_yaml(workflow_yaml: str) -> Any:
    try:
        return safe_load_no_dates(workflow_yaml)
    except Exception:
        return None


def url_origin(url: str) -> str | None:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
    except ValueError:
        # A bracket `urlparse` cannot read as an IPv6 literal raises. Redaction runs this over
        # arbitrary text, where declining collapses the span to `[URL]` and raising loses the scrub.
        return None
    if not parsed.netloc or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port is not None else host
    # Keep scheme in the origin. http:// and https:// are different security
    # contexts, so crossing between them is treated as scope broadening.
    return f"{parsed.scheme.lower()}://{netloc}"


def saved_credential_ids(candidates: Iterable[str]) -> set[str]:
    # Saved credential IDs are issued with the cred_ prefix; skip anything else defensively.
    return {candidate for candidate in candidates if isinstance(candidate, str) and candidate.startswith("cred_")}


def declared_parameters(parsed: dict[str, Any]) -> list[Any]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return []
    parameters = list(workflow_definition.get("parameters") or [])
    for block in workflow_blocks(parsed):
        block_parameters = block.get("parameters")
        if isinstance(block_parameters, list):
            parameters.extend(block_parameters)
    return parameters


def declared_non_credential_keys(parsed: dict[str, Any]) -> set[str]:
    """Parameter keys that are only names, so a `cred_`-prefixed key is not reported as a credential ID. A
    key with the issued-ID shape, or one also bound as a credential value, is still reported."""
    parameters = declared_parameters(parsed)
    keys = {
        parameter["key"]
        for parameter in parameters
        if isinstance(parameter, dict) and isinstance(parameter.get("key"), str)
    }
    bound_ids = workflow_credential_ids_from_parsed(parsed)
    for ids in credential_param_ids(parameters).values():
        bound_ids.update(ids)
    return {key for key in keys if not _ISSUED_CREDENTIAL_ID_RE.fullmatch(key) and key not in bound_ids}


def credential_params(parameters: Any) -> dict[str, str]:
    if not isinstance(parameters, list):
        return {}
    out: dict[str, str] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        if not isinstance(key, str):
            continue
        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type == "credential" and isinstance(parameter.get("credential_id"), str):
            out[key] = parameter["credential_id"]
        elif (vault_id := bitwarden_login_parameter_credential_id(parameter)) is not None:
            out[key] = vault_id
        elif (
            parameter_type == "workflow"
            and workflow_parameter_type == "credential_id"
            and isinstance(parameter.get("default_value"), str)
        ):
            out[key] = parameter["default_value"]
    return out


def credential_param_ids(parameters: Any) -> dict[str, set[str]]:
    if not isinstance(parameters, list):
        return {}
    out: dict[str, set[str]] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        key = parameter.get("key")
        if not isinstance(key, str):
            continue
        parameter_type = str(parameter.get("parameter_type") or "").lower()
        workflow_parameter_type = str(parameter.get("workflow_parameter_type") or "").lower()
        if parameter_type == "credential":
            ids: set[str] = set()
            credential_ids = parameter.get("credential_ids")
            if isinstance(credential_ids, list):
                ids.update(item for item in credential_ids if isinstance(item, str))
            if isinstance(parameter.get("credential_id"), str):
                ids.add(parameter["credential_id"])
            if ids:
                out[key] = ids
        elif (vault_id := bitwarden_login_parameter_credential_id(parameter)) is not None:
            out[key] = {vault_id}
        elif (
            parameter_type == "workflow"
            and workflow_parameter_type == "credential_id"
            and isinstance(parameter.get("default_value"), str)
        ):
            out[key] = {parameter["default_value"]}
    return out


def workflow_blocks(parsed: dict[str, Any], selected_labels: set[str] | None = None) -> list[dict[str, Any]]:
    """With `selected_labels`, collect only blocks whose label is in the set plus their
    descendants — a selected `for_loop` drags its `loop_blocks` in, since loop children are
    not themselves named in an executing label set."""
    return [location.block for location in workflow_block_locations(parsed, selected_labels)]


def block_credential_ids(block: dict[str, Any], credential_params_by_key: Mapping[str, str | set[str]]) -> set[str]:
    credential_ids: set[str] = set()
    parameter_keys = block.get("parameter_keys")
    if isinstance(parameter_keys, list):
        for key in parameter_keys:
            if isinstance(key, str) and key in credential_params_by_key:
                ids = credential_params_by_key[key]
                if isinstance(ids, str):
                    credential_ids.add(ids)
                else:
                    credential_ids.update(ids)
    direct_credential_id = block.get("credential_id")
    if isinstance(direct_credential_id, str):
        credential_ids.add(direct_credential_id)
    return credential_ids


def workflow_credential_ids(workflow_yaml: str) -> set[str]:
    if not workflow_yaml:
        return set()
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return set()
    return workflow_credential_ids_from_parsed(parsed)


def workflow_credential_ids_from_parsed(parsed: dict[str, Any]) -> set[str]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return set()

    credential_params_by_key = credential_param_ids(workflow_definition.get("parameters"))
    credential_ids: set[str] = set()
    for ids in credential_params_by_key.values():
        credential_ids.update(ids)
    for block in workflow_blocks(parsed):
        credential_ids.update(block_credential_ids(block, credential_params_by_key))
    return credential_ids


def workflow_credential_origins(workflow_yaml: str, *, require_canonical_origin: bool = False) -> dict[str, set[str]]:
    if not workflow_yaml:
        return {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return {}
    return workflow_credential_origins_from_parsed(parsed, require_canonical_origin=require_canonical_origin)


def workflow_credential_origins_from_parsed(
    parsed: dict[str, Any], *, require_canonical_origin: bool = False
) -> dict[str, set[str]]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return {}

    credential_params_by_key = credential_param_ids(workflow_definition.get("parameters"))
    origins_by_id: dict[str, set[str]] = {}
    for block in workflow_blocks(parsed):
        credential_ids = block_credential_ids(block, credential_params_by_key)
        if not credential_ids:
            continue
        block_url = block.get("url")
        if not isinstance(block_url, str) or not block_url.strip():
            continue
        # Release authorization requires an unambiguous raw URL. Output scope comparison
        # must retain other parseable origins: omitting them would hide a changed destination.
        if require_canonical_origin:
            canonical_origin = canonicalize_origin(block_url)
            origin = canonical_origin.canonical if canonical_origin is not None else None
        else:
            origin = url_origin(block_url)
        if origin is None:
            continue
        for credential_id in credential_ids:
            origins_by_id.setdefault(credential_id, set()).add(origin)
    return origins_by_id
