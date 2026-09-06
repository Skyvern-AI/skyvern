from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

from skyvern.forge.sdk.copilot.workflow_block_traversal import workflow_block_locations
from skyvern.utils.yaml_loader import safe_load_no_dates

URL_CANDIDATE_RE = re.compile(r"\b(?:https?://[^\s)>,]+|www\.[^\s)>,]+)", re.IGNORECASE)


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


def workflow_credential_origins(workflow_yaml: str) -> dict[str, set[str]]:
    if not workflow_yaml:
        return {}
    parsed = parse_workflow_yaml(workflow_yaml)
    if not isinstance(parsed, dict):
        return {}
    return workflow_credential_origins_from_parsed(parsed)


def workflow_credential_origins_from_parsed(parsed: dict[str, Any]) -> dict[str, set[str]]:
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
        origin = url_origin(block_url)
        if not origin:
            continue
        for credential_id in credential_ids:
            origins_by_id.setdefault(credential_id, set()).add(origin)
    return origins_by_id
