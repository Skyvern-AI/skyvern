from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.enforcement import PROBABLE_SITE_BLOCK_STREAK_STOP_AT
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import _iter_yaml_blocks, _parse_workflow_blocks


class CopilotBlockPolicyStatus(StrEnum):
    BANNED = "banned"
    CODE_NATIVE_PENDING = "code_native_pending"
    DECLARED_AI_LEAF = "declared_ai_leaf"


class CopilotBlockPolicyScope(StrEnum):
    ALL = "all"
    CODE_ONLY_BROWSER = "code_only_browser"


@dataclass(frozen=True)
class CopilotBlockPolicy:
    status: CopilotBlockPolicyStatus
    scope: CopilotBlockPolicyScope
    required_capability: str
    guidance: str


_P = CopilotBlockPolicy
_ALL = CopilotBlockPolicyScope.ALL
_CODE_ONLY = CopilotBlockPolicyScope.CODE_ONLY_BROWSER
_BANNED = CopilotBlockPolicyStatus.BANNED
_PENDING = CopilotBlockPolicyStatus.CODE_NATIVE_PENDING
_AI_LEAF = CopilotBlockPolicyStatus.DECLARED_AI_LEAF

_CODE_ONLY_FOCUSED_CODE_BLOCK_TYPES = (
    "action",
    "browser_task",
    "extraction",
    "goto_url",
    "navigation",
    "print_page",
    "validation",
)

_COPILOT_BLOCK_TYPE_POLICIES: dict[str, CopilotBlockPolicy] = {
    "task": _P(
        _AI_LEAF,
        _ALL,
        "declared AI leaf support",
        (
            "The legacy task agent is not available in the workflow copilot; decompose the goal into explicit "
            "workflow blocks or focused code blocks instead."
        ),
    ),
    "task_v2": _P(
        _AI_LEAF,
        _ALL,
        "declared AI leaf support",
        (
            "The legacy task_v2 agent is not available in the workflow copilot; decompose the goal into explicit "
            "workflow blocks or focused code blocks instead."
        ),
    ),
    **{
        block_type: _P(
            _BANNED,
            _CODE_ONLY,
            "focused `code` blocks for durable browser/page work",
            "Use focused `code` blocks with concrete selectors, text anchors, outputs, and postconditions.",
        )
        for block_type in _CODE_ONLY_FOCUSED_CODE_BLOCK_TYPES
    },
    "login": _P(
        _PENDING,
        _CODE_ONLY,
        "credential-typed code synthesis with runtime credential resolution",
        (
            "Use credential-typed code: scout saved-credential fields with fill_credential_field, bind the "
            "credential as a credential_id workflow parameter, and read the resolved credential object in code."
        ),
    ),
    "file_download": _P(
        _PENDING,
        _CODE_ONLY,
        "code-block download registration and output chaining",
        (
            "Download chains require code-block download registration before downstream file_url_parser or "
            "http_request file references can consume the output."
        ),
    ),
    "file_upload": _P(
        _PENDING,
        _CODE_ONLY,
        "same-run file path threading or workflow file materialization",
        (
            "Use code-native upload only when a local same-run path exists; workflow file parameters still need "
            "file materialization before this rung is complete."
        ),
    ),
}

_COPILOT_BANNED_BLOCK_TYPES: frozenset[str] = frozenset(
    block_type
    for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
    if policy.scope == CopilotBlockPolicyScope.ALL
)
_COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES: frozenset[str] = frozenset(_COPILOT_BLOCK_TYPE_POLICIES.keys())

# Shared suffix across every LLM-facing rejection message for banned
# block emission — the pre-hook (schema-lookup reject) and the post-
# emission detector both steer the LLM toward the same alternatives.
_COPILOT_BANNED_BLOCK_ALTERNATIVES = (
    "Use `navigation` for page actions (filling forms, clicking, multi-step flows), "
    "`extraction` for data extraction, `validation` for completion checks, "
    "`login` for authentication, or `goto_url` for pure URL navigation."
)
_CODE_ONLY_TARGET_EVIDENCE_KEYS = frozenset(
    {
        "buttons",
        "fields",
        "forms",
        "inputs",
        "links",
        "options",
        "result",
        "results",
        "rows",
        "selects",
        "tables",
        "textareas",
        "url",
    }
)
_CODE_ONLY_SELECTOR_ACTION_TOOLS = frozenset({"click", "type_text", "select_option", "press_key"})


def _copilot_block_authoring_policy(ctx: AgentContext | None) -> BlockAuthoringPolicy:
    if ctx is None:
        return BlockAuthoringPolicy.STANDARD
    return normalize_block_authoring_policy(getattr(ctx, "block_authoring_policy", None))


def _copilot_banned_block_types(ctx: AgentContext | None) -> frozenset[str]:
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES
    return _COPILOT_BANNED_BLOCK_TYPES


def _active_policy_scopes(ctx: AgentContext | None) -> frozenset[CopilotBlockPolicyScope]:
    scopes = {CopilotBlockPolicyScope.ALL}
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        scopes.add(CopilotBlockPolicyScope.CODE_ONLY_BROWSER)
    return frozenset(scopes)


def _copilot_block_policy(
    block_type: str,
    ctx: AgentContext | None,
) -> tuple[str, CopilotBlockPolicy] | None:
    normalized = normalize_copilot_block_type_alias(block_type.strip().lower())
    policy = _COPILOT_BLOCK_TYPE_POLICIES.get(normalized)
    if policy is not None and policy.scope in _active_policy_scopes(ctx):
        return normalized, policy
    return None


def _render_block_policy_detail(block_type: str, policy: CopilotBlockPolicy) -> str:
    return f"`{block_type}` is {policy.status.value} and requires {policy.required_capability}. {policy.guidance}"


def _code_only_browser_unavailable_types() -> list[str]:
    return sorted(
        block_type
        for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
        if policy.scope == CopilotBlockPolicyScope.CODE_ONLY_BROWSER
    )


def _code_only_browser_pending_details() -> list[str]:
    return [
        _render_block_policy_detail(block_type, policy)
        for block_type, policy in sorted(_COPILOT_BLOCK_TYPE_POLICIES.items())
        if policy.scope == CopilotBlockPolicyScope.CODE_ONLY_BROWSER
        and policy.status == CopilotBlockPolicyStatus.CODE_NATIVE_PENDING
    ]


def _code_only_browser_unavailable_summary() -> str:
    unavailable = ", ".join(f"`{block_type}`" for block_type in _code_only_browser_unavailable_types())
    return (
        f"Browser/page workflow block types are unavailable in code-only browser mode: {unavailable}. "
        "Use focused `code` blocks for durable page or browser-session work."
    )


def _code_only_browser_validation_guidance() -> str:
    return (
        "Use validate_block only for allowed non-browser helper blocks. Do not use validate_block for `code` "
        "blocks, dummy/probe code blocks, or browser/page native block types; validate real code blocks through "
        "update_and_run_blocks."
    )


def _code_only_browser_schema_guidance() -> list[str]:
    return [
        "Use one focused code block per durable browser goal, such as open, search, submit, expand, or extract.",
        _code_only_browser_unavailable_summary(),
        "Use concrete selectors and text anchors found during exploration. If only intent targeting is available, inspect the page again before mutating.",
        _code_only_browser_validation_guidance(),
        "Keep block outputs JSON-safe and include visible evidence text when extracting records, products, totals, confirmations, or identifiers.",
        "For saved credentials: bind the credential as a workflow parameter with workflow_parameter_type credential_id and the credential ID in default_value. At runtime the parameter key resolves to a credential object — read <key>.username, <key>.password, and <key>.totp (a fresh one-time code generated when the block starts). Never put literal secret values in code; scout credential fields with fill_credential_field.",
    ]


def _code_only_browser_authoring_prompt() -> str:
    pending = "\n".join(f"- {detail}" for detail in _code_only_browser_pending_details())
    return f"""
ACTIVE BLOCK AUTHORING POLICY: CODE-ONLY BROWSER MODE

{_code_only_browser_unavailable_summary()}

Rules:
- Browser/page/session durable steps must be focused `code` blocks.
- Allowed non-browser helper blocks remain available: `conditional`, `for_loop`,
  `while_loop`, `send_email`, S3/Google Sheets helpers, file parsers, and triggers.
- {_code_only_browser_validation_guidance()}
- Do not call `get_run_results` before a real workflow run exists.

Code-native capabilities still pending plumbing:
{pending}

Runtime facts:
- `code` is async Python with a Playwright `page` object and workflow parameters by key.
- Valid Python identifier parameter keys are local variables; normalize before page inputs.
- Use deterministic, bounded Playwright calls: `goto`, `click`, `fill`, `press`,
  `wait_for_load_state`, and `evaluate`.
- A `credential_id` workflow parameter resolves to a credential object with
  `<key>.username`, `<key>.password`, and fresh `<key>.totp`; scout fields with
  `fill_credential_field`, never embed literal secrets.
- Return JSON-safe structured data plus visible evidence text for records, totals,
  confirmations, and identifiers.
- Use YAML block scalars (`code: |`) and pass complete workflow YAML to update tools.
""".strip()


def _copilot_banned_block_alternatives(ctx: AgentContext | None) -> str:
    if _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _code_only_browser_unavailable_summary()
    return _COPILOT_BANNED_BLOCK_ALTERNATIVES


def _banned_block_reject_message(items: list[tuple[str, str]], ctx: AgentContext | None = None) -> str:
    """Uniform error text for the post-emission reject, sharing the
    alternatives suffix with the schema pre-hook."""
    grouped: dict[str, list[str]] = {}
    for label, block_type in items:
        normalized = normalize_copilot_block_type_alias(block_type.strip().lower())
        grouped.setdefault(normalized, []).append(label)
    labels = ", ".join(sorted({label for label, _ in items}))
    types = sorted(grouped)
    types_part = " / ".join(repr(t) for t in types)
    details = []
    for block_type in types:
        policy_entry = _copilot_block_policy(block_type, ctx)
        if policy_entry is None:
            continue
        _normalized, policy = policy_entry
        type_labels = ", ".join(sorted(grouped[block_type]))
        details.append(f"{block_type} [{type_labels}]: {_render_block_policy_detail(block_type, policy)}")
    details_part = " ".join(details)
    return (
        f"Block type {types_part} is not available in the workflow copilot. "
        f"Offending labels: [{labels}]. "
        f"{details_part} "
        f"{_copilot_banned_block_alternatives(ctx)}"
    )


def _record_banned_block_reject_span(source_tool: str, items: list[tuple[str, str]]) -> None:
    """Emit the dedicated ``update_workflow_banned_block_reject`` span used
    by post-rollout logfire trend queries."""
    with copilot_span(
        "update_workflow_banned_block_reject",
        data={
            "labels": [label for label, _ in items],
            "block_types": sorted({block_type for _, block_type in items}),
            "source_tool": source_tool,
        },
    ):
        pass


def _collect_banned_block_items(
    blocks: list[Any],
    banned_types: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Recursively walk ``blocks`` (mirroring
    :func:`skyvern.forge.sdk.copilot.block_goal_wrapping._wrap_blocks_in_place`)
    and return ``(label, normalized_block_type)`` for every block whose type is
    in :data:`_COPILOT_BANNED_BLOCK_TYPES`. Blocks missing ``label`` are
    skipped — the downstream Pydantic validator surfaces those errors on its
    own."""
    active_banned_types = banned_types or _COPILOT_BANNED_BLOCK_TYPES
    items: list[tuple[str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        raw_type = block.get("block_type")
        if isinstance(raw_type, str):
            normalized = raw_type.strip().lower()
            raw_normalized = normalize_copilot_block_type_alias(normalized)
            if normalized in active_banned_types or raw_normalized in active_banned_types:
                label = block.get("label")
                if isinstance(label, str):
                    items.append((label, raw_normalized))
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            items.extend(_collect_banned_block_items(loop_blocks, active_banned_types))
    return items


def _detect_new_banned_blocks(
    submitted_yaml: str,
    prior_workflow_yaml: str | None,
    *,
    banned_types: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Return ``[(label, block_type), ...]`` for every banned-type block in
    ``submitted_yaml`` whose label is NOT present as a banned-type block in
    ``prior_workflow_yaml``. Pure: no I/O, no logging.

    Recurses into ``for_loop.loop_blocks`` mirroring
    :func:`skyvern.forge.sdk.copilot.block_goal_wrapping._wrap_blocks_in_place`.
    Legacy workflows that carry ``task`` / ``task_v2`` blocks under unchanged
    labels produce an empty list and therefore do not reject.

    Malformed YAML, missing ``workflow_definition``, or a non-list ``blocks``
    all produce an empty list — the downstream Pydantic validation in
    ``_process_workflow_yaml`` surfaces the specific parse / shape error on
    its own path.
    """
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    active_banned_types = banned_types or _COPILOT_BANNED_BLOCK_TYPES
    submitted_items = _collect_banned_block_items(submitted_blocks, active_banned_types)
    if not submitted_items:
        return []
    prior_blocks = _parse_workflow_blocks(prior_workflow_yaml)
    prior_labels = {label for label, _ in _collect_banned_block_items(prior_blocks or [], active_banned_types)}
    return [(label, block_type) for label, block_type in submitted_items if label not in prior_labels]


_CHALLENGE_WAIT_PATTERN = re.compile(
    r"\b(anti[-_\s]?bot|bot[-_\s]?block|captcha|challenge|human[-_\s]?verification|ip[-_\s]?block|waf)\b",
    re.IGNORECASE,
)


def _has_confirmed_waf_or_site_block(ctx: Any) -> bool:
    if getattr(ctx, "last_test_anti_bot", None):
        return True
    return _get_int_attr(ctx, "probable_site_block_streak_count") >= PROBABLE_SITE_BLOCK_STREAK_STOP_AT


def _get_int_attr(ctx: Any, name: str, default: int = 0) -> int:
    value = getattr(ctx, name, default)
    return value if isinstance(value, int) else default


def _block_challenge_wait_text(block: dict[str, Any]) -> str:
    values = []
    for key in ("label", "title", "description", "navigation_goal", "complete_criterion"):
        value = block.get(key)
        if isinstance(value, str):
            values.append(value)
    return " ".join(values)


def _detect_timing_only_challenge_wait_blocks(submitted_yaml: str | None) -> list[str]:
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    labels: list[str] = []
    for block in _iter_yaml_blocks(submitted_blocks):
        raw_type = block.get("block_type")
        if not isinstance(raw_type, str) or raw_type.strip().lower() != "wait":
            continue
        label = block.get("label")
        if not isinstance(label, str):
            continue
        if _CHALLENGE_WAIT_PATTERN.search(_block_challenge_wait_text(block)):
            labels.append(label)
    return labels


def _composition_evidence_has_challenge(ctx: AgentContext) -> bool:
    evidence = getattr(ctx, "composition_page_evidence", None)
    if not isinstance(evidence, dict):
        return False
    if evidence.get("anti_bot_indicators") or evidence.get("challenge_controls"):
        return True
    challenge_state = evidence.get("challenge_state")
    return isinstance(challenge_state, dict) and challenge_state.get("detected") is True


def _detect_new_http_request_blocks(submitted_yaml: str | None, prior_workflow_yaml: str | None) -> list[str]:
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    prior_blocks = _parse_workflow_blocks(prior_workflow_yaml)
    prior_labels: set[str] = set()
    for block in _iter_yaml_blocks(prior_blocks or []):
        if str(block.get("block_type") or "").strip().lower() != "http_request":
            continue
        label = block.get("label")
        if isinstance(label, str):
            prior_labels.add(label)
    labels: list[str] = []
    for block in _iter_yaml_blocks(submitted_blocks):
        if str(block.get("block_type") or "").strip().lower() != "http_request":
            continue
        label = block.get("label")
        if isinstance(label, str) and label not in prior_labels:
            labels.append(label)
    return labels


def _challenge_http_request_reject_message(
    ctx: AgentContext, submitted_yaml: str | None, prior_workflow_yaml: str | None
) -> str | None:
    if not _composition_evidence_has_challenge(ctx):
        return None
    labels = _detect_new_http_request_blocks(submitted_yaml, prior_workflow_yaml)
    if not labels:
        return None
    labels_text = ", ".join(sorted(set(labels)))
    return (
        "Workflow validation failed: raw http_request blocks are not allowed for a page with observed "
        "anti-bot or human-verification challenge evidence. "
        f"Offending labels: [{labels_text}]. "
        "Use browser workflow blocks grounded in the observed page, include challenge handling only when visible, "
        "or stop and report the observed challenge blocker if it cannot be completed."
    )


def _timing_only_challenge_wait_reject_message(ctx: Any, submitted_yaml: str | None) -> str | None:
    if not _has_confirmed_waf_or_site_block(ctx):
        return None
    labels = _detect_timing_only_challenge_wait_blocks(submitted_yaml)
    if not labels:
        return None
    labels_text = ", ".join(sorted(set(labels)))
    return (
        "Workflow validation failed: timing-only challenge wait blocks are not allowed after confirmed "
        "anti-bot/WAF or repeated site-block evidence. "
        f"Offending labels: [{labels_text}]. "
        "Do not add wait/delay-only blocks for this blocker; use a conditional challenge check that takes a "
        "real action, try a materially different proxy/source if allowed, or stop and explain the blocker."
    )
