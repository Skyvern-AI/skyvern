"""Deterministic field-name picker for workflow-parameter synthesis.

This is the replacement for `_generate_field_names_with_llm` in
`generate_workflow_parameters.py`. In Phase 1 it ships as a pure,
side-effect-free function that is NOT yet wired into the hot path — the
LLM call remains the source of truth until the validation guard in
`generate_script.py` gives us the production signal to flip the switch.

The picker implements three rules in priority order for every INPUT_TEXT,
UPLOAD_FILE, and SELECT_OPTION action observed during a workflow run. See
SKY-8965 for the motivating smoke-test repro (phantom
`preprint_search_term` on a single-block search workflow whose navigation
goal embedded the search term as a literal).

Rule precedence:
    1. Jinja-reference rule  — the unrendered `navigation_goal` template
       contains `{{ key }}` where `key` is in the valid-keys set
       (declared workflow parameters ∪ upstream block schema keys).
       Use `key` directly; no synthesis.
    2. Upstream-schema rule  — the action's value equals a literal value
       associated with an upstream block's `data_schema.properties` key.
       Use that key; no synthesis.
    3. Intention-derived rule — deterministic snake_case sanitization of
       the action's `intention` text. Last-resort synthesis; guaranteed
       deterministic across runs with the same input.

Rules 1 and 2 both produce field names that are already in the valid-keys
set and therefore will never trip the validation guard. Rule 3 produces
synthesized names that the guard will eventually reject in Phase 2, forcing
workflow authors to declare their parameters up front or accept an
ai='proactive' fallback in the script.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from skyvern.forge.sdk.workflow.context_manager import RANDOM_SECRET_ID_PREFIX
from skyvern.webeye.actions.actions import ActionType

CUSTOM_FIELD_ACTIONS: tuple[ActionType, ...] = (
    ActionType.INPUT_TEXT,
    ActionType.UPLOAD_FILE,
    ActionType.SELECT_OPTION,
)


@dataclass(frozen=True)
class FieldPick:
    """Result of picking a field strategy for a single action.

    `rule` is the rule that fired: "jinja_ref" | "upstream_schema" |
    "intention_derived" | "existing_assignment". The caller can use this to
    emit telemetry on rule-distribution.
    """

    field_name: str
    rule: str
    description: str | None = None


# `{{ name }}` or `{{ name.attr }}` or `{{ name | filter }}` — root identifier only.
_JINJA_ROOT_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\b")

# `{{ root.sub }}` — captures both the root and one level of attribute,
# used to detect dotted references like `{{credentials.username}}` for
# nested-subscript emission. `\s*` around the dot tolerates the whitespace
# Jinja allows in attribute access (e.g. `{{ credentials . username }}`).
_JINJA_DOTTED_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\b")

# `placeholder_<random>_<subkey>` — extracts the trailing sub-key from a
# substitution token recorded in action.text. The random part is fixed-width
# (4 chars) per `WorkflowRunContext.generate_random_secret_id`, but using a
# permissive `[A-Za-z0-9]+` keeps the parser tolerant of future format tweaks.
_PLACEHOLDER_TOKEN_RE = re.compile(rf"^{re.escape(RANDOM_SECRET_ID_PREFIX)}[A-Za-z0-9]+_(.+)$")

# Canonical sub-keys we will emit in `context.parameters[<root>][<sub>]`.
# TOTP excluded: it has dedicated emission paths (get_totp_digit,
# totp_identifier) and racing with sequence annotation is out of scope.
KNOWN_CREDENTIAL_SUBKEYS: frozenset[str] = frozenset({"username", "password"})

# Sub-key name variants → canonical key. Anything outside KNOWN_CREDENTIAL_SUBKEYS
# and this map is treated as unsupported — emitting it would KeyError at runtime.
_CREDENTIAL_SUBKEY_ALIASES: dict[str, str] = {
    "email": "username",
    "email_address": "username",
    "user": "username",
    "login": "username",
    "user_name": "username",
    "pass": "password",
    "passwd": "password",
}

# Intention keywords that map to canonical credential sub-keys.
_USERNAME_KEYWORDS: tuple[str, ...] = ("username", "email", "login", "user id", "userid")
_PASSWORD_KEYWORDS: tuple[str, ...] = ("password", "passcode", "passphrase")

# TOTP-flavored intentions must NOT route to credential subscript — substring
# matches like "one-time password" would otherwise hit `_PASSWORD_KEYWORDS`
# and emit a nested subscript that bypasses get_totp_digit / totp_identifier.
_TOTP_INTENTION_DENYLIST: tuple[str, ...] = (
    "verification code",
    "one-time",
    "one time",
    "otp",
    "2fa",
    "two-factor",
    "totp",
    "mfa code",
    "auth code",
)

# Sanitize an intention string down to a valid snake_case identifier.
_NON_IDENT = re.compile(r"[^a-z0-9_]+")
_REPEAT_UNDERSCORE = re.compile(r"_+")


def sanitize_intention_to_field_name(intention: str, fallback: str = "unknown_field") -> str:
    """Deterministic sanitization of an intention string to a Python identifier.

    Mirrors the fallback pattern commented out in
    `hydrate_input_text_actions_with_field_names`, now promoted to first-class.
    """
    if not intention:
        return fallback
    lowered = intention.lower().strip()
    # Replace anything that isn't a-z0-9_ with underscores
    cleaned = _NON_IDENT.sub("_", lowered)
    # Collapse repeated underscores, strip leading/trailing
    cleaned = _REPEAT_UNDERSCORE.sub("_", cleaned).strip("_")
    if not cleaned:
        return fallback
    # Ensure it doesn't start with a digit
    if cleaned[0].isdigit():
        cleaned = f"f_{cleaned}"
    # Cap length — intentions can be paragraphs
    return cleaned[:60]


def extract_jinja_root_names(template: str) -> set[str]:
    """Return the set of root identifiers referenced in `{{ ... }}` blocks.

    Example:
        >>> extract_jinja_root_names("Search for {{ query }} then {{ user.name }}")
        {"query", "user"}
    """
    if not template:
        return set()
    return set(_JINJA_ROOT_RE.findall(template))


def extract_jinja_dotted_pairs(template: str) -> set[tuple[str, str]]:
    """Return `(root, sub)` pairs from dotted `{{root.sub}}` Jinja references.

    Example:
        >>> extract_jinja_dotted_pairs("Email: {{credentials.email}}")
        {("credentials", "email")}
    """
    if not template:
        return set()
    return set(_JINJA_DOTTED_RE.findall(template))


def normalize_credential_subkey(raw: str) -> str | None:
    """Translate a sub-key name to its canonical credential dict key, or
    None if the raw name doesn't map to anything in the runtime dict."""
    raw_lower = raw.lower()
    if raw_lower in KNOWN_CREDENTIAL_SUBKEYS:
        return raw_lower
    return _CREDENTIAL_SUBKEY_ALIASES.get(raw_lower)


def _infer_credential_subkey_from_action(action: dict[str, Any]) -> str | None:
    """Identify which credential sub-field an action targets.

    Primary structural signal: a placeholder token of the form
    ``placeholder_<random>_<subkey>`` in the recorded action text (built in
    ``WorkflowRunContext.register_credential_parameter_value``). The sub-key
    is extracted by stripping the prefix and validated against
    ``KNOWN_CREDENTIAL_SUBKEYS`` so a future credential schema with a key
    like ``backup_username`` wouldn't be silently routed to ``username``.

    Fallback: intention keywords, used when the recorded text isn't a
    placeholder token (rare — empty text or post-redaction). The TOTP
    denylist short-circuits before the password-keyword match.
    """
    text = action.get("text") or ""
    if text.startswith(RANDOM_SECRET_ID_PREFIX):
        match = _PLACEHOLDER_TOKEN_RE.match(text)
        if match:
            subkey = match.group(1)
            if subkey in KNOWN_CREDENTIAL_SUBKEYS:
                return subkey
            # Token matched but sub-key isn't canonical (e.g. totp). Return None
            # rather than falling through to intention guessing — the placeholder
            # is authoritative, and an intention like "enter passcode" would
            # otherwise route the user's password into the OTP field.
            return None

    intention = (action.get("intention") or "").lower()
    if not intention:
        return None
    if any(kw in intention for kw in _TOTP_INTENTION_DENYLIST):
        return None
    if any(kw in intention for kw in _PASSWORD_KEYWORDS):
        return "password"
    if any(kw in intention for kw in _USERNAME_KEYWORDS):
        return "username"
    return None


def pick_field_name_for_action(
    *,
    action: dict[str, Any],
    goal_template: str,
    declared_param_keys: frozenset[str],
    upstream_schema_keys: frozenset[str],
    existing_field_name: str | None = None,
) -> FieldPick:
    """Pick a field name for a single custom-field action.

    Args:
        action: INPUT_TEXT / UPLOAD_FILE / SELECT_OPTION action dict. Must
            carry `intention` and (depending on action_type) `text` / `file_url` /
            `option`.
        goal_template: Unrendered `navigation_goal` string for the task this
            action belongs to. Must NOT be the rendered form — otherwise the
            jinja-reference rule cannot tell a real parameter from a literal.
        declared_param_keys: Workflow-declared parameter keys
            (`workflow.workflow_definition.parameters[].key`).
        upstream_schema_keys: Keys collected from upstream blocks'
            `data_schema.properties` (output-parameter keys are collected
            separately via `_collect_declared_param_keys` at the workflow level).
        existing_field_name: Pre-existing assignment that must be preserved
            across regenerations (e.g., from a cached block's schema). When set,
            it wins unconditionally — regenerations must not rename fields that
            cached code references.

    Returns:
        FieldPick with the chosen name, the rule that fired, and (when the
        picker used jinja/schema rules) an optional description for the
        generated Pydantic field.

    Never raises. The fallback path is guaranteed to produce a valid
    identifier from the action's intention.
    """
    # Preservation wins: cached code already references this name.
    if existing_field_name:
        return FieldPick(field_name=existing_field_name, rule="existing_assignment")

    valid_keys = declared_param_keys | upstream_schema_keys
    referenced_keys = extract_jinja_root_names(goal_template)

    # Rule 1: jinja reference to a declared or schema key.
    # Only fires when exactly ONE valid key is referenced in the goal — otherwise
    # we can't disambiguate which INPUT_TEXT action targets which key and would
    # collapse multiple fields onto the same name (CORR-1 from debate review).
    intersection = referenced_keys & valid_keys
    if len(intersection) == 1:
        (match,) = intersection
        return FieldPick(field_name=match, rule="jinja_ref")

    # Rule 2: value matches an upstream schema key name or is clearly keyed
    # by intention to one. Sorted by descending key length so that `invoice_id`
    # is tried before `id` — prevents short keys from shadowing longer,
    # more-specific keys via substring match (andrewneilson review feedback).
    # Keys that aren't valid Python identifiers are sanitized before returning
    # (JSON Schema allows hyphens, reserved words, etc. that would produce
    # invalid Pydantic class bodies).
    intention = (action.get("intention") or "").lower()
    for key in sorted(upstream_schema_keys, key=len, reverse=True):
        if key.lower() in intention:
            safe_key = sanitize_intention_to_field_name(key, fallback=key)
            return FieldPick(field_name=safe_key, rule="upstream_schema")

    # Rule 3: deterministic fallback derived from intention.
    name = sanitize_intention_to_field_name(intention)
    description = action.get("intention") or f"Value for {name}"
    return FieldPick(field_name=name, rule="intention_derived", description=description)


def pick_field_names_for_actions(
    *,
    actions_by_task: dict[str, list[dict[str, Any]]],
    goal_template_by_task: dict[str, str],
    declared_param_keys: frozenset[str],
    upstream_schema_keys: frozenset[str],
    existing_field_assignments: dict[int, str] | None = None,
) -> dict[str, FieldPick]:
    """Bulk version: pick field names for every custom-field action.

    Returns a mapping `"{task_id}:{action_id}" → FieldPick`. Actions without
    a usable value are skipped — same behaviour as the existing LLM path in
    `generate_workflow_parameters_schema`.

    This function is pure. It does no I/O and makes no LLM calls.
    """
    existing_field_assignments = existing_field_assignments or {}
    picks: dict[str, FieldPick] = {}
    action_counter = 0

    for task_id, actions in actions_by_task.items():
        goal = goal_template_by_task.get(task_id, "")
        for action in actions:
            action_type = action.get("action_type", "")
            if action_type not in CUSTOM_FIELD_ACTIONS:
                continue

            if action_type == ActionType.INPUT_TEXT:
                value = action.get("text", "")
            elif action_type == ActionType.UPLOAD_FILE:
                value = action.get("file_url", "")
            elif action_type == ActionType.SELECT_OPTION:
                value = action.get("option", "")
            else:
                value = ""

            if not value:
                continue

            action_counter += 1
            existing = existing_field_assignments.get(action_counter)

            pick = pick_field_name_for_action(
                action=action,
                goal_template=goal,
                declared_param_keys=declared_param_keys,
                upstream_schema_keys=upstream_schema_keys,
                existing_field_name=existing,
            )
            key = f"{task_id}:{action.get('action_id', '')}"
            picks[key] = pick

    return picks


def infer_credential_subscript_for_emit(
    *,
    action: dict[str, Any],
    goal_template: str,
    block_type: str | None,
    credential_param_keys: frozenset[str],
) -> tuple[str, str] | None:
    """Return `(root, sub)` if a login-block fill should emit nested
    `context.parameters[<root>][<sub>]`, else None. Multi-credential
    workflows are disambiguated by `{{<root>.<sub>}}` in the goal template."""
    if block_type != "login" or not credential_param_keys:
        return None

    inferred_sub = _infer_credential_subkey_from_action(action)
    if inferred_sub is None:
        return None

    matching_roots_from_jinja = {
        root
        for root, sub in extract_jinja_dotted_pairs(goal_template)
        if root in credential_param_keys and normalize_credential_subkey(sub) == inferred_sub
    }
    if len(matching_roots_from_jinja) == 1:
        return (next(iter(matching_roots_from_jinja)), inferred_sub)
    if len(matching_roots_from_jinja) > 1:
        return None

    if len(credential_param_keys) == 1:
        return (next(iter(credential_param_keys)), inferred_sub)

    return None
