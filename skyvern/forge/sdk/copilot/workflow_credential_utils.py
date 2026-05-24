from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from skyvern.utils.yaml_loader import safe_load_no_dates

_NESTED_BLOCK_LIST_KEYS = ("loop_blocks", "blocks")
_BRANCH_LIST_KEYS = ("branch_conditions", "branches", "ordered_branches")
URL_CANDIDATE_RE = re.compile(r"\b(?:https?://[^\s)>,]+|www\.[^\s)>,]+)", re.IGNORECASE)


def parse_workflow_yaml(workflow_yaml: str) -> Any:
    try:
        return safe_load_no_dates(workflow_yaml)
    except Exception:
        return None


def url_origin(url: str) -> str | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        return None
    # Keep scheme in the origin. http:// and https:// are different security
    # contexts, so crossing between them is treated as scope broadening.
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


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


def workflow_blocks(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return []

    collected: list[dict[str, Any]] = []

    def visit_branch(branch: dict[str, Any]) -> None:
        for key in _NESTED_BLOCK_LIST_KEYS:
            visit(branch.get(key))
        for branch_key in _BRANCH_LIST_KEYS:
            branches = branch.get(branch_key)
            if not isinstance(branches, list):
                continue
            for nested_branch in branches:
                if isinstance(nested_branch, dict):
                    visit_branch(nested_branch)

    def visit(blocks: Any) -> None:
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            collected.append(block)
            for key in _NESTED_BLOCK_LIST_KEYS:
                visit(block.get(key))
            for branch_key in _BRANCH_LIST_KEYS:
                branches = block.get(branch_key)
                if not isinstance(branches, list):
                    continue
                for branch in branches:
                    if isinstance(branch, dict):
                        visit_branch(branch)

    visit(workflow_definition.get("blocks"))
    return collected


def block_credential_ids(block: dict[str, Any], credential_params_by_key: dict[str, str]) -> set[str]:
    credential_ids: set[str] = set()
    parameter_keys = block.get("parameter_keys")
    if isinstance(parameter_keys, list):
        for key in parameter_keys:
            if isinstance(key, str) and key in credential_params_by_key:
                credential_ids.add(credential_params_by_key[key])
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

    credential_params_by_key = credential_params(workflow_definition.get("parameters"))
    credential_ids = set(credential_params_by_key.values())
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

    credential_params_by_key = credential_params(workflow_definition.get("parameters"))
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
