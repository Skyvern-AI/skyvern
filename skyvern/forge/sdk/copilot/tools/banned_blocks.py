from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy, normalize_block_authoring_policy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span

from ._shared import _parse_workflow_blocks


class CopilotBlockPolicyStatus(StrEnum):
    BANNED = "banned"
    CODE_NATIVE_PENDING = "code_native_pending"
    DECLARED_AI_LEAF = "declared_ai_leaf"


class CopilotBlockPolicyScope(StrEnum):
    ALL = "all"
    CODE_ONLY_BROWSER = "code_only_browser"
    TASK_V3_PURE = "task_v3_pure"


@dataclass(frozen=True)
class CopilotBlockPolicy:
    status: CopilotBlockPolicyStatus
    scope: CopilotBlockPolicyScope
    required_capability: str
    guidance: str


_P = CopilotBlockPolicy
_ALL = CopilotBlockPolicyScope.ALL
_CODE_ONLY = CopilotBlockPolicyScope.CODE_ONLY_BROWSER
_TASK_V3_PURE = CopilotBlockPolicyScope.TASK_V3_PURE
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
    "code": _P(
        _BANNED,
        _TASK_V3_PURE,
        "Task V3-only execution",
        "Use engine-less deterministic blocks or a supported task block pinned to `skyvern-3.0`.",
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
_COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES: frozenset[str] = frozenset(
    block_type
    for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
    if policy.scope in {CopilotBlockPolicyScope.ALL, CopilotBlockPolicyScope.CODE_ONLY_BROWSER}
)
_TASK_V3_PURE_TASK_BLOCK_TYPES: frozenset[str] = frozenset(
    {"task", "navigation", "login", "action", "validation", "extraction", "file_download"}
)
_TASK_V3_PURE_BANNED_BLOCK_TYPES: frozenset[str] = frozenset(
    block_type
    for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
    if policy.scope == CopilotBlockPolicyScope.TASK_V3_PURE
    or (policy.status == CopilotBlockPolicyStatus.DECLARED_AI_LEAF and block_type not in _TASK_V3_PURE_TASK_BLOCK_TYPES)
)
_TASK_V3_ENGINE = "skyvern-3.0"


class TaskV3PureViolationCode(StrEnum):
    BLOCK_TYPE_UNAVAILABLE = "block_type_unavailable"
    ENGINE_NOT_SKYVERN_V3 = "engine_not_skyvern_v3"
    UNSUPPORTED_V3_COMBINATION = "unsupported_v3_combination"
    SYNTHETIC_TASK_CONTROL_FLOW = "synthetic_task_control_flow"


@dataclass(frozen=True)
class TaskV3PurePolicyViolation:
    label: str
    block_type: str
    code: TaskV3PureViolationCode
    guidance: str

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "block_type": self.block_type,
            "code": self.code.value,
            "guidance": self.guidance,
        }


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
    policy = _copilot_block_authoring_policy(ctx)
    if policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES
    if policy == BlockAuthoringPolicy.TASK_V3_PURE:
        return _TASK_V3_PURE_BANNED_BLOCK_TYPES
    return _COPILOT_BANNED_BLOCK_TYPES


def _active_policy_scopes(ctx: AgentContext | None) -> frozenset[CopilotBlockPolicyScope]:
    scopes = {CopilotBlockPolicyScope.ALL}
    policy = _copilot_block_authoring_policy(ctx)
    if policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        scopes.add(CopilotBlockPolicyScope.CODE_ONLY_BROWSER)
    elif policy == BlockAuthoringPolicy.TASK_V3_PURE:
        scopes.add(CopilotBlockPolicyScope.TASK_V3_PURE)
    return frozenset(scopes)


def _copilot_block_policy(
    block_type: str,
    ctx: AgentContext | None,
) -> tuple[str, CopilotBlockPolicy] | None:
    normalized = normalize_copilot_block_type_alias(block_type.strip().lower())
    if normalized == "task" and _copilot_block_authoring_policy(ctx) == BlockAuthoringPolicy.TASK_V3_PURE:
        return None
    policy = _COPILOT_BLOCK_TYPE_POLICIES.get(normalized)
    if policy is not None and policy.scope in _active_policy_scopes(ctx):
        return normalized, policy
    return None


def _render_block_policy_detail(block_type: str, policy: CopilotBlockPolicy) -> str:
    return f"`{block_type}` is {policy.status.value} and requires {policy.required_capability}. {policy.guidance}"


def _record_code_native_pending_capability(ctx: AgentContext | None, policy: CopilotBlockPolicy) -> None:
    if (
        ctx is not None
        and policy.status == CopilotBlockPolicyStatus.CODE_NATIVE_PENDING
        and ctx.code_native_pending_capability is None
    ):
        ctx.code_native_pending_capability = policy.required_capability


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
        "A saved run executes this block against a page it loads itself, without the interactions performed while scouting. Whatever the page requires before the target is reachable is part of what the block does, not a condition it inherits.",
        _code_only_browser_validation_guidance(),
        "Keep block outputs JSON-safe and include visible evidence text when extracting records, products, totals, confirmations, or identifiers.",
        "Wait for the value the block returns, not for a URL or a navigation. A page reaches its final URL while it is still rendering, so a URL check passes before the value exists and a navigation wait fails on a page that has already arrived.",
        "For saved credentials: bind the credential as a workflow parameter with workflow_parameter_type credential_id and the credential ID in default_value. At runtime the parameter key resolves to a credential object — read <key>.username and <key>.password, use await <key>.otp() for authenticator, email, or SMS one-time codes, and use await <key>.magic_link(page) when the scouted page offers an emailed sign-in link; that broker navigates the page without exposing the sign-in link to authored code. Never put literal secret values in code; scout credential fields with fill_credential_field.",
        "The Code runtime provides await solve_captcha(page) for a platform-managed verification challenge observed while scouting; this is an available capability, not a required step for every login.",
    ]


def _code_only_browser_authoring_prompt() -> str:
    pending = "\n".join(f"- {detail}" for detail in _code_only_browser_pending_details())
    return f"""
ACTIVE BLOCK AUTHORING POLICY: CODE-ONLY BROWSER MODE

{_code_only_browser_unavailable_summary()}

Rules:
- Browser/page/session durable steps must be focused `code` blocks.
- In code-only browser mode, before authoring the first `code` block this turn,
  call `get_block_schema` with `block_type: code` and follow its returned field
  names and nesting exactly; do not guess the YAML shape from memory.
- Allowed non-browser helper blocks remain available: `conditional`, `for_loop`,
  `while_loop`, `send_email`, `human_interaction`, S3/Google Sheets helpers, file
  parsers, and triggers.
- {_code_only_browser_validation_guidance()}

Code-native capabilities still pending plumbing:
{pending}

Runtime facts:
- `code` is async Python with a Playwright `page` object and workflow parameters by key.
- The runtime pre-injects its helper namespaces; do not write `import` statements and do
  not access dunder (`__name__`) names or attributes.
- Valid Python identifier parameter keys are local variables; normalize values before page inputs.
- Use deterministic, bounded Playwright calls and selectors observed while scouting.
- For browser reads, prefer visible anchors, locator text, block outputs, and
  MCP/scout evidence gathered before authoring.
- Return JSON-safe structured data plus visible evidence text for records, totals,
  confirmations, and identifiers.
- For an extraction-intent `code` block, derive a typed `extraction_schema` (named
  fields with types) from the goal and the scouted page, carry it as
  `code_artifact_metadata.extraction_schema`, and conform the block's `return` to it.
- Use YAML block scalars (`code: |`) and pass complete workflow YAML to update tools.
""".strip()


def _copilot_banned_block_alternatives(ctx: AgentContext | None) -> str:
    policy = _copilot_block_authoring_policy(ctx)
    if policy == BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return _code_only_browser_unavailable_summary()
    if policy == BlockAuthoringPolicy.TASK_V3_PURE:
        return (
            "Use engine-less workflow blocks for deterministic orchestration and integrations, or one of "
            "`task`, `navigation`, `login`, `action`, `validation`, `extraction`, and `file_download` with "
            "`engine: skyvern-3.0`."
        )
    return _COPILOT_BANNED_BLOCK_ALTERNATIVES


def _task_v3_pure_policy_violations(workflow_yaml: str) -> list[TaskV3PurePolicyViolation]:
    """Validate the complete proposed definition, including pre-existing leaves.

    Task-V3-pure mode deliberately does not grandfather legacy blocks: accepting
    any edit means the resulting workflow is pure, rather than only the edited
    labels being pure.
    """
    blocks = _parse_workflow_blocks(workflow_yaml)
    if blocks is None:
        return []
    violations: list[TaskV3PurePolicyViolation] = []
    for block in blocks:
        if isinstance(block, Mapping):
            violations.extend(_task_v3_pure_block_violations(block))
    return violations


def _task_v3_pure_block_violations(block: Mapping[str, object]) -> list[TaskV3PurePolicyViolation]:
    raw_type = block.get("block_type")
    if not isinstance(raw_type, str):
        return []
    block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
    raw_label = block.get("label")
    label = raw_label if isinstance(raw_label, str) else "(unlabeled)"
    violations: list[TaskV3PurePolicyViolation] = []

    if block_type in _TASK_V3_PURE_BANNED_BLOCK_TYPES:
        violations.append(
            TaskV3PurePolicyViolation(
                label=label,
                block_type=block_type,
                code=TaskV3PureViolationCode.BLOCK_TYPE_UNAVAILABLE,
                guidance="Task-V3-pure mode does not allow code or task_v2 blocks.",
            )
        )
    elif block_type in _TASK_V3_PURE_TASK_BLOCK_TYPES:
        if block.get("engine") != _TASK_V3_ENGINE:
            violations.append(
                TaskV3PurePolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=TaskV3PureViolationCode.ENGINE_NOT_SKYVERN_V3,
                    guidance="Set the submitted block engine exactly to `skyvern-3.0`.",
                )
            )
        if block_type == "validation" and block.get("complete_on_download") is True:
            violations.append(
                TaskV3PurePolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=TaskV3PureViolationCode.UNSUPPORTED_V3_COMBINATION,
                    guidance="Use a separate file_download block; Task V3 does not support download-gated validation.",
                )
            )

    if block_type == "for_loop":
        loop_variable_reference = block.get("loop_variable_reference")
        loop_over_parameter_key = block.get("loop_over_parameter_key")
        has_free_form_loop_reference = (
            not isinstance(loop_variable_reference, str)
            or loop_variable_reference.strip(" {}") != loop_over_parameter_key
        )
        if loop_variable_reference not in (None, "") and has_free_form_loop_reference:
            violations.append(
                TaskV3PurePolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=TaskV3PureViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
                    guidance="Use `loop_over_parameter_key`; free-form loop input creates a synthetic task.",
                )
            )
    elif block_type == "while_loop":
        condition = block.get("condition")
        if isinstance(condition, Mapping) and condition.get("criteria_type", "jinja2_template") != "jinja2_template":
            violations.append(
                TaskV3PurePolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=TaskV3PureViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
                    guidance="Use a `jinja2_template` condition; prompt criteria create a synthetic task.",
                )
            )
    elif block_type == "conditional":
        branch_conditions = block.get("branch_conditions")
        if isinstance(branch_conditions, list) and any(
            isinstance(branch, Mapping)
            and isinstance(branch.get("criteria"), Mapping)
            and branch["criteria"].get("criteria_type", "jinja2_template") != "jinja2_template"
            for branch in branch_conditions
        ):
            violations.append(
                TaskV3PurePolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=TaskV3PureViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
                    guidance="Use `jinja2_template` branch criteria; prompt criteria create a synthetic task.",
                )
            )

    loop_blocks = block.get("loop_blocks")
    if isinstance(loop_blocks, list):
        for nested in loop_blocks:
            if isinstance(nested, Mapping):
                violations.extend(_task_v3_pure_block_violations(nested))
    return violations


def _task_v3_pure_reject_message(violations: list[TaskV3PurePolicyViolation]) -> str:
    details = " ".join(
        f"{violation.label} ({violation.block_type}, {violation.code.value}): {violation.guidance}"
        for violation in violations
    )
    return f"The submitted workflow violates the active Task-V3-pure block policy. {details}"


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
        _record_code_native_pending_capability(ctx, policy)
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


def _blocks_with_default_labels(blocks: list[Any]) -> list[Any]:
    normalized_blocks: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            normalized_blocks.append(block)
            continue
        normalized = dict(block)
        if not isinstance(normalized.get("label"), str):
            normalized["label"] = "(unlabeled)"
        loop_blocks = normalized.get("loop_blocks")
        if isinstance(loop_blocks, list):
            normalized["loop_blocks"] = _blocks_with_default_labels(loop_blocks)
        normalized_blocks.append(normalized)
    return normalized_blocks


def collect_code_only_banned_items(blocks: list[Any]) -> list[tuple[str, str]]:
    """Banned (label, block_type) pairs under code-only browser mode; unlabeled blocks included."""
    return _collect_banned_block_items(
        _blocks_with_default_labels(blocks),
        _COPILOT_CODE_ONLY_BROWSER_BANNED_BLOCK_TYPES,
    )


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
