"""Deterministic synthesis of a copilot `code` block from a scout trajectory.

Pure module: ``synthesize_code_block`` is a function of its input trajectory
only — no LLM, no I/O, byte-identical output per trajectory. It turns the
scout's captured interaction sequence into a bounded, linear Playwright snippet
that runs on the raw ``page`` object the copilot code block executes against.
"""

from __future__ import annotations

import json
import keyword
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from skyvern.forge.sdk.copilot.composition_evidence import SCOUT_INTERACTION_EVIDENCE_TOOL

_MAX_STEPS = 20
_INDENT = "    "

CREDENTIAL_FILL_TOOL_NAME = "fill_credential_field"
_CREDENTIAL_FIELDS = frozenset({"username", "password", "totp"})

_SYNTHESIZED_BLOCK_LABEL = "scout_synthesized_browser_steps"

# Names the code-block executor reserves in its exec() namespace (block.py build_safe_vars
# plus the injected `page`). A parameter key colliding with one of these is silently dropped
# at bind time, so the synthesized fill would stringify the builtin instead of the user value.
# "username"/"password"/"totp"/"totp_identifier" are reserved too: CodeBlock.execute also
# injects a bound credential's fields under those bare names, so a plain parameter named
# `password` would resolve to the credential's secret value instead of the user input.
_RESERVED_PARAM_NAMES = frozenset(
    {
        "page",
        "username",
        "password",
        "totp",
        "totp_identifier",
        "print",
        "len",
        "range",
        "str",
        "int",
        "float",
        "dict",
        "list",
        "tuple",
        "set",
        "bool",
        "isinstance",
        "enumerate",
        "any",
        "all",
        "max",
        "min",
        "sum",
        "sorted",
        "sleep",
        "asyncio",
        "re",
        "json",
        "html",
        "Exception",
    }
)

# role=<role>[name="<name>"] optionally followed by `>> nth=<n>` or other engines.
_ROLE_NAME_RE = re.compile(r'^role=([a-zA-Z]+)(?:\[name="((?:[^"\\]|\\.)*)"\])?(.*)$')

# Positional/index engines whose match depends on document order, not element identity. A captured
# selector containing one of these is fragile, so an ARIA role/name anchor (when available) is preferred.
_POSITIONAL_RE = re.compile(
    r":nth-of-type\(|:nth-child\(|:nth-last-of-type\(|:nth-last-child\(|>>\s*nth=|:first-child|:last-child"
)


@dataclass
class SynthesizedCodeBlock:
    code: str
    parameters: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# str.splitlines() and several parsers treat these codepoints as line boundaries, so a raw one in a
# captured accessible name or option value would split the emitted one-line literal across lines and
# corrupt the block. repr() does not escape U+2028/U+2029, so they (and the C0/C1 controls below) are
# escaped explicitly to keep every emitted literal single-line.
_EXTRA_LINE_SEPARATORS = ("\u2028", "\u2029")
_CONTROL_CODEPOINTS = frozenset(
    chr(cp) for cp in (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)) if chr(cp) not in ("\n", "\r")
)


def _py_str(value: str) -> str:
    """A deterministic double-quoted Python string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    escaped = "".join(f"\\x{ord(ch):02x}" if ch in _CONTROL_CODEPOINTS else ch for ch in escaped)
    for separator in _EXTRA_LINE_SEPARATORS:
        escaped = escaped.replace(separator, f"\\u{ord(separator):04x}")
    return f'"{escaped}"'


def _unescape_role_name(value: str) -> str:
    # ref_to_selector escapes only `"` as `\"`; reverse it for the get_by_role name.
    return value.replace('\\"', '"')


def _parse_role_name(selector: str) -> tuple[str, str | None, str] | None:
    """Parse a `role=...[name="..."]` selector. Returns (role, name, suffix) or None.

    ``suffix`` is the residual engine chain (e.g. ` >> nth=2`); a non-empty suffix
    means the selector cannot be expressed as a plain get_by_role and the caller
    falls back to page.locator.
    """
    match = _ROLE_NAME_RE.match(selector)
    if not match:
        return None
    role, raw_name, suffix = match.group(1), match.group(2), match.group(3)
    name = _unescape_role_name(raw_name) if raw_name is not None else None
    return role, name, suffix.strip()


def _is_positional_selector(selector: str) -> bool:
    """True when the captured selector's match depends on document position, not element identity.

    Stable anchors (id, [name=...], [data-testid=...], [aria-label=...], a non-indexed CSS path) are
    preferred verbatim; only a positional/index selector is worth trading for an ARIA role/name anchor.
    """
    return bool(_POSITIONAL_RE.search(selector))


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    if cleaned and cleaned[0].isdigit():
        cleaned = f"field_{cleaned}"
    return cleaned or "field"


def _safe_param_base(value: str) -> str:
    base = _slug(value)
    if keyword.iskeyword(base) or base in _RESERVED_PARAM_NAMES:
        return f"{base}_field"
    return base


def _get_by_role_expr(role: str, name: str) -> str:
    if name:
        return f"page.get_by_role({_py_str(role)}, name={_py_str(name)})"
    return f"page.get_by_role({_py_str(role)})"


def _locator_expr(
    interaction: Mapping[str, Any],
    notes: list[str],
) -> str:
    """Selector-first: emit the scout's captured working selector verbatim, and only fall back to an
    ARIA get_by_role anchor when that selector is positional/index-based (and a role/name is available).

    The accessible name is read on the scout/MCP surface; a `code:` block runs on a raw Playwright page
    where get_by_role's native name computation may not reproduce it. The captured selector is the proven,
    timing-independent locator the scout actually drove, so it wins for any stable selector.
    """
    selector = str(interaction.get("selector") or "").strip()
    role = str(interaction.get("role") or "").strip()
    name = str(interaction.get("accessible_name") or "").strip()

    parsed = _parse_role_name(selector) if selector else None
    if parsed is not None:
        parsed_role, parsed_name, suffix = parsed
        # A `role=...` selector is itself an ARIA anchor (ref_to_selector form), not a native CSS path —
        # there is no underlying stable selector to prefer, so express it as get_by_role.
        if not suffix:
            return _get_by_role_expr(role or parsed_role, name or (parsed_name or ""))
        # Residual engine chain (e.g. `>> nth=`) makes the parsed form positional; anchor on role/name.
        anchor_role = role or parsed_role
        anchor_name = name or (parsed_name or "")
        if anchor_role and anchor_name:
            return _get_by_role_expr(anchor_role, anchor_name)
        return f"page.locator({_py_str(selector)})"

    if selector:
        if _is_positional_selector(selector) and role and name:
            return _get_by_role_expr(role, name)
        if _is_positional_selector(selector):
            notes.append(f"low-confidence locator: positional selector {selector!r} with no role/name to anchor on")
        return f"page.locator({_py_str(selector)})"

    if role and name:
        return _get_by_role_expr(role, name)

    notes.append("dropped an interaction with no selector and no role/name")
    return ""


def _unique_key(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _param_key(interaction: Mapping[str, Any], used: set[str]) -> str:
    name = str(interaction.get("accessible_name") or "").strip()
    role = str(interaction.get("role") or "").strip()
    return _unique_key(_safe_param_base(name or role or "value"), used)


def _credential_param_key(interaction: Mapping[str, Any], used: set[str]) -> str:
    name = str(interaction.get("credential_name") or "").strip()
    return _unique_key(_safe_param_base(name or "credential"), used)


def synthesize_code_block(trajectory: Sequence[Mapping[str, Any]]) -> SynthesizedCodeBlock | None:
    """Deterministically synthesize a code block from a scout trajectory, or None if empty."""
    if not trajectory:
        return None

    lines: list[str] = []
    notes: list[str] = []
    parameters: list[dict[str, str]] = []
    used_param_keys: set[str] = set()
    credential_param_keys: dict[str, str] = {}

    entry_url = ""
    entry_index = -1
    for index, interaction in enumerate(trajectory):
        candidate = str(interaction.get("source_url") or "").strip()
        if candidate:
            entry_url = candidate
            entry_index = index
            break
    if entry_url:
        if entry_index > 0:
            notes.append("entry URL taken from a later interaction; earlier steps had no source_url")
        lines.append(f"{_INDENT}await page.goto({_py_str(entry_url)})")
        lines.append(f'{_INDENT}await page.wait_for_load_state("load")')

    emitted = 0
    for interaction in trajectory:
        if emitted >= _MAX_STEPS:
            notes.append(f"trajectory truncated at {_MAX_STEPS} steps")
            break
        tool_name = str(interaction.get("tool_name") or "")

        if tool_name == "press_key":
            key = str(interaction.get("key") or "").strip()
            if not key:
                continue
            locator = _locator_expr(interaction, notes) if interaction.get("selector") else ""
            if locator:
                lines.append(f"{_INDENT}await {locator}.press({_py_str(key)})")
            else:
                lines.append(f"{_INDENT}await page.keyboard.press({_py_str(key)})")
            lines.append(f'{_INDENT}await page.wait_for_load_state("load")')
            emitted += 1
            continue

        locator = _locator_expr(interaction, notes)
        if not locator:
            continue

        if tool_name == "click":
            lines.append(f"{_INDENT}await {locator}.click()")
            lines.append(f'{_INDENT}await page.wait_for_load_state("load")')
        elif tool_name == "type_text":
            param_key = _param_key(interaction, used_param_keys)
            parameters.append({"key": param_key})
            lines.append(f"{_INDENT}await {locator}.fill(str({param_key}))")
        elif tool_name == CREDENTIAL_FILL_TOOL_NAME:
            credential_id = str(interaction.get("credential_id") or "").strip()
            credential_field = str(interaction.get("credential_field") or "").strip()
            if not credential_id or credential_field not in _CREDENTIAL_FIELDS:
                notes.append("dropped a credential fill with no usable credential reference")
                continue
            credential_param_key = credential_param_keys.get(credential_id)
            if credential_param_key is None:
                credential_param_key = _credential_param_key(interaction, used_param_keys)
                credential_param_keys[credential_id] = credential_param_key
                parameters.append({"key": credential_param_key, "credential_id": credential_id})
            lines.append(f"{_INDENT}await {locator}.fill({credential_param_key}.{credential_field})")
        elif tool_name == "select_option":
            value = str(interaction.get("value") or "").strip()
            if not value:
                notes.append("dropped a select_option interaction with no recorded value")
                continue
            lines.append(f"{_INDENT}await {locator}.select_option({_py_str(value)})")
            lines.append(f'{_INDENT}await page.wait_for_load_state("load")')
        else:
            notes.append(f"skipped unsupported interaction tool_name={tool_name!r}")
            continue
        emitted += 1

    if not lines:
        return None

    code = "\n".join(lines) + "\n"
    return SynthesizedCodeBlock(code=code, parameters=parameters, notes=notes)


# Model-owned slots the synthesizer cannot prove; the model fills these.
_FILL_DECLARED_GOAL = "<fill: the durable goal this block accomplishes>"
_FILL_CLAIM_ID = "claim:<fill>"
_FILL_CLAIM_TEXT = "<fill: the user-facing outcome this block claims>"
_FILL_CRITERION_ID = "criterion:<fill>"
_FILL_CRITERION_TEXT = "<fill: the terminal completion criterion>"


def artifact_dependency_id(block_label: str) -> str:
    return f"dependency:{_slug(block_label)}_reached"


def artifact_observation_ref_id(block_label: str) -> str:
    return f"observation:{_slug(block_label)}_scout"


def build_artifact_metadata_skeleton(
    trajectory: Sequence[Mapping[str, Any]],
    *,
    block_label: str,
) -> dict[str, Any]:
    """Fill only the scout-proven evidence shape (`observed_not_verified`); the terminal goal and
    outcomes stay as `<fill>` placeholders the model owns."""
    dependency_id = artifact_dependency_id(block_label)
    observation_ref_id = artifact_observation_ref_id(block_label)
    # First recorded source_url is the page the synthesized block's leading `page.goto` lands on.
    # The trajectory carries only pre-action source pages, not post-action reached URLs, so this is
    # an advisory entry-page hint, never a reached-page identity (which SPAs would mis-key anyway).
    entry_url_hint = next(
        (url for url in (str(interaction.get("source_url") or "").strip() for interaction in trajectory) if url),
        None,
    )

    page_dependency: dict[str, Any] = {
        "id": dependency_id,
        "scope": "page",
        "status": "observed_not_verified",
        "observation_refs": [observation_ref_id],
    }
    if entry_url_hint:
        page_dependency["url_hint"] = entry_url_hint

    observation_ref: dict[str, Any] = {
        "observation_ref": observation_ref_id,
        "dependency_id": dependency_id,
        "status": "observed_not_verified",
        "source_tool": SCOUT_INTERACTION_EVIDENCE_TOOL,
    }

    return {
        "block_label": block_label,
        "declared_goal": _FILL_DECLARED_GOAL,
        "page_dependencies": [page_dependency],
        "observation_refs": [observation_ref],
        "claimed_outcomes": [
            {
                "id": _FILL_CLAIM_ID,
                "scope": "outcome",
                "text": _FILL_CLAIM_TEXT,
                "status": "observed_not_verified",
                "depends_on": [dependency_id],
                "covered_criteria": [_FILL_CRITERION_ID],
                "observation_refs": [observation_ref_id],
            }
        ],
        "completion_criteria": [
            {
                "id": _FILL_CRITERION_ID,
                "text": _FILL_CRITERION_TEXT,
                "level": "terminal",
                "terminal": True,
            }
        ],
        "terminal_verifier_expectations": [
            {
                "id": "expectation:<fill>",
                "text": "<fill: what terminal verification must observe>",
                "criteria_ids": [_FILL_CRITERION_ID],
            }
        ],
    }


def build_synthesized_artifact_metadata(trajectory: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return build_artifact_metadata_skeleton(trajectory, block_label=_SYNTHESIZED_BLOCK_LABEL)


def _render_artifact_metadata_block(metadata: Mapping[str, Any]) -> str:
    return json.dumps(metadata, indent=2, sort_keys=True)


def render_synthesized_offer_text(
    synthesized: SynthesizedCodeBlock,
    trajectory: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render the offer body the copilot sees for a synthesized block (pure)."""
    param_keys = [p.get("key", "") for p in synthesized.parameters if p.get("key") and not p.get("credential_id")]
    credential_parameters = [p for p in synthesized.parameters if p.get("key") and p.get("credential_id")]
    parts = [
        "SYNTHESIZED CODE BLOCK (offered once). The page interactions you scouted were compiled into a "
        "deterministic Playwright snippet. Persist it VERBATIM as a `code` block labeled "
        f"`{_SYNTHESIZED_BLOCK_LABEL}` via update_workflow / update_and_run_blocks; only hand-author the "
        "extract/report steps it does not cover.",
        "```python",
        synthesized.code.rstrip("\n"),
        "```",
    ]
    if param_keys:
        parts.append("Workflow parameters referenced (bind these): " + ", ".join(param_keys) + ".")
    if credential_parameters:
        bindings = ", ".join(f"`{p['key']}` -> `{p['credential_id']}`" for p in credential_parameters)
        parts.append(
            "Credential parameters referenced: "
            + bindings
            + ". Bind each as a workflow parameter with `workflow_parameter_type: credential_id` and the "
            "credential ID in `default_value`; at runtime the key resolves to a credential object whose "
            "`.username` / `.password` / `.totp` attributes the snippet reads (`.totp` is a fresh one-time "
            "code generated when the block starts). Never replace these attribute reads with literal values."
        )
    if synthesized.notes:
        parts.append("Synthesis notes: " + "; ".join(synthesized.notes) + ".")
    if trajectory:
        metadata = build_synthesized_artifact_metadata(trajectory)
        parts.append(
            "Pass this `code_artifact_metadata` for the block (the scouted page evidence is filled in; "
            "replace the `<fill: ...>` slots with the terminal goal and outcome this block delivers, then "
            "submit it whole — the validator returns every remaining violation at once):"
        )
        parts.append("```json")
        parts.append(_render_artifact_metadata_block(metadata))
        parts.append("```")
    return "\n".join(parts)
