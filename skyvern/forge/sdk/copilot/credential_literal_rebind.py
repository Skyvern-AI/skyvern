from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import yaml

from skyvern.forge.sdk.copilot.code_block_synthesis import _CREDENTIAL_FIELDS, CREDENTIAL_FILL_TOOL_NAME
from skyvern.forge.sdk.copilot.workflow_credential_utils import credential_param_ids

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_STRING_ASSIGN_TMPL = r"^[ \t]*{name}[ \t]*=[ \t]*(?P<q>['\"])(?:\\.|(?!(?P=q)).)*(?P=q)[ \t]*\n?"


@dataclass(frozen=True)
class CredentialRebindResult:
    workflow_yaml: str
    changed: bool
    rebound: tuple[str, ...]


def scouted_credential_targets(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, tuple[str, str]]:
    """Map each scouted selector to the (credential_id, field) that `fill_credential_field` typed into it."""
    targets: dict[str, tuple[str, str]] = {}
    for interaction in scout_trajectory or []:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(interaction.get("selector") or "").strip()
        credential_id = str(interaction.get("credential_id") or "").strip()
        field = str(interaction.get("credential_field") or "").strip()
        if not selector or not credential_id or field not in _CREDENTIAL_FIELDS:
            continue
        targets.setdefault(selector, (credential_id, field))
    return targets


def _existing_param_key(parameters: Sequence[object], credential_id: str) -> str | None:
    for key, credential_ids in credential_param_ids(parameters).items():
        if credential_id in credential_ids:
            return key
    return None


def _mint_param_key(credential_id: str, used: set[str]) -> str:
    suffix = re.sub(r"\W", "", credential_id)[-8:] or "login"
    candidate = f"credential_{suffix}"
    index = 2
    while candidate in used:
        candidate = f"credential_{suffix}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _drop_dead_string_assignment(code: str, name: str) -> str:
    pattern = re.compile(_STRING_ASSIGN_TMPL.format(name=re.escape(name)), re.MULTILINE)
    match = pattern.search(code)
    if not match:
        return code
    remaining = code[: match.start()] + code[match.end() :]
    # `credential_x.username` must not count as a use of a local named `username`.
    if re.search(r"(?<![\w.])" + re.escape(name) + r"\b", remaining):
        return code
    return remaining


def _rebind_block_code(
    code: str, targets: Mapping[str, tuple[str, str]], param_key_for: Callable[[str], str]
) -> tuple[str, list[str], set[str]]:
    rebound: list[str] = []
    used_params: set[str] = set()
    replaced_identifiers: set[str] = set()
    new_code = code

    for selector, (credential_id, field) in targets.items():
        param_key = param_key_for(credential_id)
        access = f"{param_key}.{field}"
        selector_pattern = re.escape(selector)
        patterns = (
            re.compile(r"(\.(?:fill|type)\(\s*['\"]" + selector_pattern + r"['\"]\s*,\s*)([^),]+?)(\s*\))"),
            re.compile(
                r"(\.locator\(\s*['\"]" + selector_pattern + r"['\"]\s*\)\s*\.(?:fill|type)\(\s*)([^),]+?)(\s*\))"
            ),
        )

        def _substitute(match: re.Match[str]) -> str:
            argument = match.group(2).strip()
            if argument == access:
                used_params.add(param_key)
                return match.group(0)
            if _IDENTIFIER_RE.fullmatch(argument):
                replaced_identifiers.add(argument)
            rebound.append(f"{param_key}.{field}")
            used_params.add(param_key)
            return match.group(1) + access + match.group(3)

        for pattern in patterns:
            new_code = pattern.sub(_substitute, new_code)

    for identifier in replaced_identifiers:
        new_code = _drop_dead_string_assignment(new_code, identifier)

    return new_code, rebound, used_params


def rebind_scouted_credential_literals(
    workflow_yaml: str | None, scout_trajectory: Sequence[Mapping[str, Any]] | None
) -> CredentialRebindResult:
    """Rewrite raw credential literals in authored code blocks into credential-parameter access.

    Any fill/type whose selector was scouted by `fill_credential_field` is rebound to
    `<credential_param>.<field>` regardless of which author wrote the code, so a leaked literal
    cannot reach persistence.
    """
    empty = CredentialRebindResult(workflow_yaml=workflow_yaml or "", changed=False, rebound=())
    if not workflow_yaml or not workflow_yaml.strip():
        return empty
    targets = scouted_credential_targets(scout_trajectory)
    if not targets:
        return empty
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return empty
    if not isinstance(parsed, dict):
        return empty
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return empty
    blocks = workflow_definition.get("blocks")
    if not isinstance(blocks, list):
        return empty

    parameters = workflow_definition.get("parameters")
    parameters = parameters if isinstance(parameters, list) else []
    used_keys = {str(p.get("key")) for p in parameters if isinstance(p, dict) and p.get("key")}
    assigned: dict[str, str] = {}

    def param_key_for(credential_id: str) -> str:
        if credential_id in assigned:
            return assigned[credential_id]
        key = _existing_param_key(parameters, credential_id) or _mint_param_key(credential_id, used_keys)
        assigned[credential_id] = key
        return key

    all_rebound: list[str] = []
    minted_for: set[str] = set()

    for block in blocks:
        if not isinstance(block, dict):
            continue
        code = block.get("code")
        if not isinstance(code, str) or not code.strip():
            continue
        new_code, rebound, used_params = _rebind_block_code(code, targets, param_key_for)
        if not rebound:
            continue
        block["code"] = new_code
        all_rebound.extend(rebound)
        parameter_keys = block.get("parameter_keys")
        parameter_keys = list(parameter_keys) if isinstance(parameter_keys, list) else []
        for key in sorted(used_params):
            if key not in parameter_keys:
                parameter_keys.append(key)
        block["parameter_keys"] = parameter_keys
        minted_for.update(used_params)

    if not all_rebound:
        return empty

    for credential_id, key in assigned.items():
        if key not in minted_for:
            continue
        if _existing_param_key(parameters, credential_id):
            continue
        parameters.append({"key": key, "parameter_type": "credential", "credential_id": credential_id})
    workflow_definition["parameters"] = parameters

    return CredentialRebindResult(
        workflow_yaml=yaml.safe_dump(parsed, sort_keys=False),
        changed=True,
        rebound=tuple(all_rebound),
    )
