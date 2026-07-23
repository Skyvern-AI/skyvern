from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

import yaml

from skyvern.forge.sdk.copilot.code_block_synthesis import _CREDENTIAL_FIELDS, CREDENTIAL_FILL_TOOL_NAME
from skyvern.forge.sdk.copilot.workflow_credential_utils import credential_param_ids, workflow_blocks

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_STRING_LITERAL_RE = re.compile(r"(?P<q>['\"])(?:\\.|(?!(?P=q)).)*(?P=q)")
_STRING_ASSIGN_TMPL = r"^[ \t]*{name}[ \t]*=[ \t]*" + _STRING_LITERAL_RE.pattern + r"[ \t]*\n?"
_GOTO_RE = re.compile(r"\.goto\(\s*['\"]([^'\"]+)['\"]")


def _normalize_page_url(url: str) -> str:
    return url.split("#", 1)[0].strip().rstrip("/")


@dataclass(frozen=True)
class CredentialRebindResult:
    workflow_yaml: str
    changed: bool
    rebound: tuple[str, ...]


def scouted_credential_targets(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, tuple[str, str]]:
    """Map each scouted selector to the (credential_id, field) `fill_credential_field` typed into it, dropping any selector scouted with conflicting credential/field targets so its literals fall to the fail-closed refusal backstop rather than being rebound to an ambiguous parameter."""
    targets: dict[str, tuple[str, str]] = {}
    conflicted: set[str] = set()
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
        mapping = (credential_id, field)
        existing = targets.get(selector)
        if existing is not None and existing != mapping:
            conflicted.add(selector)
            continue
        targets.setdefault(selector, mapping)
    for selector in conflicted:
        targets.pop(selector, None)
    return targets


def scouted_selector_source_urls(scout_trajectory: Sequence[Mapping[str, Any]] | None) -> dict[str, frozenset[str]]:
    """Map each `fill_credential_field` selector to the normalized page URL(s) it was scouted on, so a
    block that navigates elsewhere cannot rebind a same-named selector to a credential scouted on a
    different page."""
    by_selector: dict[str, set[str]] = {}
    for interaction in scout_trajectory or []:
        if not isinstance(interaction, Mapping):
            continue
        if str(interaction.get("tool_name") or "").strip() != CREDENTIAL_FILL_TOOL_NAME:
            continue
        selector = str(interaction.get("selector") or "").strip()
        source_url = _normalize_page_url(str(interaction.get("source_url") or ""))
        if not selector or not source_url:
            continue
        by_selector.setdefault(selector, set()).add(source_url)
    return {selector: frozenset(urls) for selector, urls in by_selector.items()}


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


def _identifier_bound_to_string_literal(code: str, name: str) -> bool:
    pattern = re.compile(_STRING_ASSIGN_TMPL.format(name=re.escape(name)), re.MULTILINE)
    return pattern.search(code) is not None


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


def _block_page_incompatible(block_goto_urls: set[str], target_source_urls: frozenset[str]) -> bool:
    # A block counts as a different page only when it navigates yet lands on none of the scout pages;
    # scoping is block-level, so a multi-goto block that mixes pages leans on the refusal backstop.
    return bool(block_goto_urls) and bool(target_source_urls) and not (block_goto_urls & target_source_urls)


def _rebind_block_code(
    code: str,
    targets: Mapping[str, tuple[str, str]],
    param_key_for: Callable[[str], str],
    source_urls_by_selector: Mapping[str, frozenset[str]],
) -> tuple[str, list[str], set[str]]:
    rebound: list[str] = []
    used_params: set[str] = set()
    replaced_identifiers: set[str] = set()
    new_code = code
    block_goto_urls = {_normalize_page_url(url) for url in _GOTO_RE.findall(code)}

    for selector, (credential_id, field) in targets.items():
        if _block_page_incompatible(block_goto_urls, source_urls_by_selector.get(selector, frozenset())):
            continue
        param_key = param_key_for(credential_id)
        access = f"{param_key}.{field}"
        selector_pattern = re.escape(selector)
        patterns = (
            re.compile(
                r"(\.(?:fill|type|press_sequentially)\(\s*['\"]" + selector_pattern + r"['\"]\s*,\s*)([^),]+?)(\s*\))"
            ),
            re.compile(
                r"(\.locator\(\s*['\"]"
                + selector_pattern
                + r"['\"]\s*\)\s*\.(?:fill|type|press_sequentially)\(\s*)([^),]+?)(\s*\))"
            ),
        )

        def _substitute(match: re.Match[str]) -> str:
            argument = match.group(2).strip()
            if argument == access:
                used_params.add(param_key)
                return match.group(0)
            bound_identifier = _IDENTIFIER_RE.fullmatch(argument) is not None and _identifier_bound_to_string_literal(
                code, argument
            )
            if not _STRING_LITERAL_RE.fullmatch(argument) and not bound_identifier:
                return match.group(0)
            if bound_identifier:
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
    source_urls_by_selector = scouted_selector_source_urls(scout_trajectory)
    try:
        parsed = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError:
        return empty
    if not isinstance(parsed, dict):
        return empty
    workflow_definition = parsed.get("workflow_definition")
    if not isinstance(workflow_definition, dict):
        return empty
    blocks = workflow_blocks(parsed)
    if not blocks:
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
        new_code, rebound, used_params = _rebind_block_code(code, targets, param_key_for, source_urls_by_selector)
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
