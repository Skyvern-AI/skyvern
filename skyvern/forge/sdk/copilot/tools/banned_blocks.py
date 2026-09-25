from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple

import yaml

from skyvern.forge.sdk.copilot.author_time_block import BANNED_BLOCKS_BLOCK_ID, AuthorTimeBlock
from skyvern.forge.sdk.copilot.block_type_aliases import normalize_copilot_block_type_alias
from skyvern.forge.sdk.copilot.config import (
    AGENT_BLOCKS_ONLY,
    ALL_BLOCK_FAMILIES,
    CODE_BLOCKS_ONLY,
    AuthoringCapability,
    BlockAuthoringPolicy,
    authoring_capability_from_policy,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.workflow_yaml import dump_workflow_yaml
from skyvern.forge.sdk.schemas.credentials import CredentialType, TotpType
from skyvern.utils.yaml_loader import safe_load_no_dates

from ._shared import _parse_workflow_blocks


class CredentialCodeAccessors(NamedTuple):
    fields: tuple[str, ...]
    otp: str | None = None
    magic_link: str | None = None


# What authored code can read off a bound credential parameter, by the saved record's type;
# credit-card accessors are not advertised here.
CREDENTIAL_CODE_ACCESSORS: Mapping[CredentialType, CredentialCodeAccessors] = {
    CredentialType.PASSWORD: CredentialCodeAccessors(
        fields=("<key>.username", "<key>.password"),
        otp="await <key>.otp()",
        magic_link="await <key>.magic_link(page)",
    ),
    CredentialType.SECRET: CredentialCodeAccessors(fields=("<key>.secret_value",)),
}
ONE_TIME_CODE_TOTP_TYPES = frozenset({TotpType.AUTHENTICATOR, TotpType.EMAIL, TotpType.TEXT})


def credential_code_accessors(credential_type: CredentialType, totp_type: TotpType) -> tuple[str, ...]:
    accessors = CREDENTIAL_CODE_ACCESSORS.get(credential_type)
    if accessors is None:
        return ()
    if totp_type not in ONE_TIME_CODE_TOTP_TYPES:
        return accessors.fields
    return accessors.fields + tuple(name for name in (accessors.otp, accessors.magic_link) if name)


class CopilotBlockPolicyStatus(StrEnum):
    BANNED = "banned"
    CODE_NATIVE_PENDING = "code_native_pending"
    DECLARED_AI_LEAF = "declared_ai_leaf"


class CopilotBlockPolicyScope(StrEnum):
    ALWAYS = "always"
    WITHOUT_AGENT_BLOCKS = "without_agent_blocks"
    WITHOUT_CODE_BLOCKS = "without_code_blocks"


@dataclass(frozen=True)
class CopilotBlockPolicy:
    status: CopilotBlockPolicyStatus
    scope: CopilotBlockPolicyScope
    required_capability: str
    guidance: str


_P = CopilotBlockPolicy
_ALWAYS = CopilotBlockPolicyScope.ALWAYS
_WITHOUT_AGENT_BLOCKS = CopilotBlockPolicyScope.WITHOUT_AGENT_BLOCKS
_WITHOUT_CODE_BLOCKS = CopilotBlockPolicyScope.WITHOUT_CODE_BLOCKS
_BANNED = CopilotBlockPolicyStatus.BANNED
_PENDING = CopilotBlockPolicyStatus.CODE_NATIVE_PENDING
_AI_LEAF = CopilotBlockPolicyStatus.DECLARED_AI_LEAF

_AGENT_FAMILY_FOCUSED_CODE_BLOCK_TYPES = (
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
        _WITHOUT_AGENT_BLOCKS,
        "agent-block authoring",
        (
            "The task agent is unavailable while only code may be authored; decompose the goal into focused "
            "code blocks instead."
        ),
    ),
    "task_v2": _P(
        _AI_LEAF,
        _ALWAYS,
        "declared AI leaf support",
        (
            "The legacy task_v2 agent is not available in the workflow copilot; decompose the goal into explicit "
            "workflow blocks or focused code blocks instead."
        ),
    ),
    "code": _P(
        _BANNED,
        _WITHOUT_CODE_BLOCKS,
        "code-block authoring access",
        "Use engine-less deterministic blocks or a supported task block pinned to `skyvern-3.0`.",
    ),
    **{
        block_type: _P(
            _BANNED,
            _WITHOUT_AGENT_BLOCKS,
            "focused `code` blocks for durable browser/page work",
            "Use focused `code` blocks with concrete selectors, text anchors, outputs, and postconditions.",
        )
        for block_type in _AGENT_FAMILY_FOCUSED_CODE_BLOCK_TYPES
    },
    "login": _P(
        _PENDING,
        _WITHOUT_AGENT_BLOCKS,
        "credential-typed code synthesis with runtime credential resolution",
        (
            "Use credential-typed code: scout saved-credential fields with fill_credential_field, bind the "
            "credential as a credential_id workflow parameter, and read the resolved credential object in code."
        ),
    ),
    "file_download": _P(
        _PENDING,
        _WITHOUT_AGENT_BLOCKS,
        "code-block download registration and output chaining",
        (
            "Download chains require code-block download registration before downstream file_url_parser or "
            "http_request file references can consume the output."
        ),
    ),
    "file_upload": _P(
        _PENDING,
        _WITHOUT_AGENT_BLOCKS,
        "page attachment of a declared file_url input",
        (
            "Attach a declared file_url parameter or a claimed download with "
            "await attach_authorized_file(page, <file_or_download>, <observed_selector>); exporting run files "
            "has no code-only route yet."
        ),
    ),
}


def _scope_applies(scope: CopilotBlockPolicyScope, capability: AuthoringCapability) -> bool:
    if scope == CopilotBlockPolicyScope.ALWAYS:
        return True
    if scope == CopilotBlockPolicyScope.WITHOUT_AGENT_BLOCKS:
        return not capability.agent_blocks
    return not capability.code_blocks


def _banned_block_types_for_capability(capability: AuthoringCapability) -> frozenset[str]:
    return frozenset(
        block_type
        for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
        if _scope_applies(policy.scope, capability)
    )


_COPILOT_BANNED_BLOCK_TYPES: frozenset[str] = _banned_block_types_for_capability(ALL_BLOCK_FAMILIES)
_CODE_BLOCKS_ONLY_BANNED_BLOCK_TYPES: frozenset[str] = _banned_block_types_for_capability(CODE_BLOCKS_ONLY)
# The agent block family: every one of these must be pinned to the Task V3 engine.
AGENT_FAMILY_BLOCK_SUMMARIES: dict[str, str] = {
    "task": "one agent step that both acts and extracts; prefer navigation or extraction when the step does only one",
    "navigation": "one agent step that acts on a page: fills forms, clicks, works through a multi-step flow",
    "extraction": "one agent step that reads values off a page and returns them",
    "action": "one agent step that performs a single interaction on a page",
    "validation": "one agent step that checks a page shows the expected end state",
    "login": "one agent step that signs in with a bound credential; a `code` block signs in with the same credential",
    "file_download": "one agent step that downloads a file from a page",
}
# Carries its own "when" clause so it still separates from `navigation` in a listing that
# AUTHORING_FAMILY_GUIDANCE is absent from — ADR-0041 gives that constant a deletion trigger.
CODE_BLOCK_SUMMARY = (
    "Python that drives the browser directly with Playwright when the steps are the same every run, "
    "and transforms data it already holds"
)
_AGENT_FAMILY_BLOCK_TYPES: frozenset[str] = frozenset(AGENT_FAMILY_BLOCK_SUMMARIES)
_TASK_V3_ENGINE = "skyvern-3.0"

# Reaches the model on the three authoring tool descriptions, the block-type list and the
# choosing_a_block knowledge topic, and only when both families may be authored.
AUTHORING_FAMILY_GUIDANCE = (
    "Write a `code` block for browser work: that is the default. Write an agent block (engine `skyvern-3.0`) "
    "only when the user asks for one, when the site or page is only known at run time, or when the page has "
    "been shown to change so much between visits that fixed code is not practical. An unclear item in the "
    "request is settled by scouting the site or by asking, not by an agent block. Among agent blocks, a "
    "sign-in is a `login` block, not a hand-built `navigation`. Both families belong in one workflow."
)

# Every capability carries this: the runtime facts the deleted code-mode prompt used to state are now
# only returned by get_block_schema, so a turn that is never told to read it never sees them.
SCHEMA_FIRST_GUIDANCE = (
    "Call `get_block_schema` for a block type before authoring that type this turn, and follow the "
    "field names and nesting it returns rather than guessing the YAML shape."
)


class AuthoringViolationCode(StrEnum):
    BLOCK_TYPE_UNAVAILABLE = "block_type_unavailable"
    ENGINE_NOT_SKYVERN_V3 = "engine_not_skyvern_v3"
    UNSUPPORTED_V3_COMBINATION = "unsupported_v3_combination"
    SYNTHETIC_TASK_CONTROL_FLOW = "synthetic_task_control_flow"


@dataclass(frozen=True)
class AuthoringPolicyViolation:
    label: str
    block_type: str
    code: AuthoringViolationCode
    guidance: str

    def as_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "block_type": self.block_type,
            "code": self.code.value,
            "guidance": self.guidance,
        }


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


def _copilot_authoring_capability(ctx: AgentContext | None) -> AuthoringCapability:
    """A turn with no context authors nothing privileged, so it falls back to agent blocks only.

    Replay stubs and other duck-typed carriers hold only the wire spelling, so the bridge stands in
    for the carrier property until the enum is retired.
    """
    if ctx is None:
        return AGENT_BLOCKS_ONLY
    capability = getattr(ctx, "authoring_capability", None)
    if isinstance(capability, AuthoringCapability):
        return capability
    policy = getattr(ctx, "block_authoring_policy", None)
    if not isinstance(policy, (BlockAuthoringPolicy, str)):
        return AGENT_BLOCKS_ONLY
    return authoring_capability_from_policy(policy)


def _copilot_banned_block_types(ctx: AgentContext | None) -> frozenset[str]:
    return _banned_block_types_for_capability(_copilot_authoring_capability(ctx))


def _copilot_block_policy(
    block_type: str,
    ctx: AgentContext | None,
) -> tuple[str, CopilotBlockPolicy] | None:
    normalized = normalize_copilot_block_type_alias(block_type.strip().lower())
    policy = _COPILOT_BLOCK_TYPE_POLICIES.get(normalized)
    if policy is not None and _scope_applies(policy.scope, _copilot_authoring_capability(ctx)):
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


def _agent_family_unavailable_types() -> list[str]:
    return sorted(
        block_type
        for block_type, policy in _COPILOT_BLOCK_TYPE_POLICIES.items()
        if policy.scope == CopilotBlockPolicyScope.WITHOUT_AGENT_BLOCKS
    )


def _code_only_browser_unavailable_summary() -> str:
    unavailable = ", ".join(f"`{block_type}`" for block_type in _agent_family_unavailable_types())
    return (
        f"These workflow block types are unavailable while only code may be authored: {unavailable}. "
        "Use focused `code` blocks for durable page or browser-session work."
    )


def _code_only_browser_validation_guidance(*, agent_blocks: bool = False) -> str:
    if agent_blocks:
        return (
            "validate_block is never for `code` blocks or dummy/probe code blocks; validate real code blocks "
            "through update_and_run_blocks."
        )
    return (
        "validate_block is only for allowed non-browser helper blocks, never for `code` blocks, dummy/probe "
        "code blocks, or browser/page native block types; validate real code blocks through update_and_run_blocks."
    )


def _saved_credential_guidance() -> str:
    accessors = CREDENTIAL_CODE_ACCESSORS[CredentialType.PASSWORD]
    username, password = accessors.fields
    otp, magic_link = accessors.otp, accessors.magic_link
    return (
        "For saved credentials: bind the credential as a workflow parameter with workflow_parameter_type "
        "credential_id and the credential ID in default_value. At runtime the parameter key resolves to a credential "
        f"object. For a `password` credential, read {username} and {password}, use {otp} for authenticator, email, "
        f"or SMS one-time codes, and use {magic_link} when the scouted page offers an emailed sign-in link; that "
        "broker navigates the page without exposing the sign-in link to authored code. Never put literal secret "
        "values in code; scout password-credential fields with fill_credential_field, which does not fill secrets."
    )


def _secret_credential_guidance() -> str:
    (secret_value,) = CREDENTIAL_CODE_ACCESSORS[CredentialType.SECRET].fields
    return f"A `secret` credential is read with {secret_value}; it carries no username or password."


def _code_only_browser_schema_guidance(*, agent_blocks: bool = False) -> list[str]:
    """The `code` schema response. Two entries answer "what else may this turn author", so they read
    as a closed list and have to change when the agent family is authorable too."""
    availability = (
        "Agent blocks and non-browser helper blocks are available alongside `code` in the same workflow."
        if agent_blocks
        else "Non-browser helper blocks stay available: `conditional`, `for_loop`, `while_loop`, `send_email`, `human_interaction`, S3/Google Sheets helpers, file parsers, and triggers."
    )
    return [
        "Use one focused code block per durable browser goal, such as open, search, submit, expand, or extract.",
        "`code` is async Python with a Playwright `page` object and workflow parameters by key. Helper namespaces are pre-injected: no `import` statements, no dunder (`__name__`) names or attributes. Normalize parameter values before page inputs. Use YAML block scalars (`code: |`) and pass complete workflow YAML to update tools.",
        WRAPPER_SCOPE_RUNTIME_FACT,
        "When a scouting tool offers a SYNTHESIZED CODE BLOCK it already encodes the interactions you scouted as deterministic Playwright: persist it verbatim and hand-author only the steps it does not cover. Direct browser evaluate is a scouting tool; a persisted code block must not use page.evaluate, page.evaluate_handle, page.request, or page.context. Use locators and locator DOM-reading methods such as inner_text, text_content, get_attribute, count, and is_visible instead.",
        "For an extraction-intent `code` block, derive a typed `extraction_schema` from the goal and the scouted page, carry it as `code_artifact_metadata.extraction_schema`, and conform the block's `return` to it.",
        availability,
        "Use concrete selectors and text anchors found during exploration. If only intent targeting is available, inspect the page again before mutating.",
        "A saved run executes this block against a page it loads itself, without the interactions performed while scouting. Whatever the page requires before the target is reachable is part of what the block does, not a condition it inherits.",
        _code_only_browser_validation_guidance(agent_blocks=agent_blocks),
        "Keep block outputs JSON-safe and include visible evidence text when extracting records, products, totals, confirmations, or identifiers.",
        "Wait for the value the block returns, not for a URL or a navigation. A page reaches its final URL while it is still rendering, so a URL check passes before the value exists and a navigation wait fails on a page that has already arrived.",
        _saved_credential_guidance(),
        _secret_credential_guidance(),
        "The Code runtime provides await solve_captcha(page) for a platform-managed verification challenge observed while scouting; this is an available capability, not a required step for every login.",
        "The Code runtime provides await clear_browser_data(page) when a site needs a clean session before it will sign in: it drops every cookie in the run's browser and all stored data for every origin it has a page or frame open on, and returns nothing. Read page.url first and navigate back to it afterwards. Browser settings pages (chrome://...) cannot be navigated to; this helper is the way to clear state. A workflow parameter named clear_browser_data shadows the helper in both executors. Before calling the helper in that case, rename the parameter to an unused name, preserve its value/default, and update its block bindings, code/template references, and caller-supplied run input keys.",
        "For file attachment: bind the file as a workflow parameter with workflow_parameter_type file_url, then call await attach_authorized_file(page, <file_parameter>, <observed_selector>). The parameter is a handle, not a path: pass it only to that helper. Attaching puts the file's contents in the page, where page scripts and page.evaluate can read them, so attach it only to the page that should receive it. It accepts only that run's materialized file, uploads at most 10 MB, and returns filename and size. To upload a file this block downloads, claim it with async with page.expect_download() as info: and pass await info.value to the same helper, never its path.",
    ]


WRAPPER_SCOPE_RUNTIME_FACT = (
    "The body runs inside a wrapper function: identifier parameter keys and top-level names are its "
    "locals, so `global` never reaches them. Accumulate in a flat loop; a nested helper updates one "
    "with `nonlocal`, a return value, or a mutable accumulator."
)


def _copilot_banned_block_alternatives(ctx: AgentContext | None) -> str:
    capability = _copilot_authoring_capability(ctx)
    if capability.code_blocks and not capability.agent_blocks:
        return _code_only_browser_unavailable_summary()
    if capability.agent_blocks and not capability.code_blocks:
        return (
            "Use engine-less workflow blocks for deterministic orchestration and integrations, or one of "
            "`task`, `navigation`, `login`, `action`, `validation`, `extraction`, and `file_download` with "
            "`engine: skyvern-3.0`."
        )
    return (
        "Use a `code` block for deterministic work on a page you have scouted, or one of `task`, "
        "`navigation`, `login`, `action`, `validation`, `extraction`, and `file_download` with "
        "`engine: skyvern-3.0` when the page or the judgement only arrives at run time."
    )


def _block_authoring_violations(
    block: Mapping[str, object],
    capability: AuthoringCapability,
    *,
    recurse: bool = True,
    run_names: frozenset[str] = frozenset(),
) -> list[AuthoringPolicyViolation]:
    raw_type = block.get("block_type")
    if not isinstance(raw_type, str):
        return []
    block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
    raw_label = block.get("label")
    label = raw_label if isinstance(raw_label, str) else "(unlabeled)"
    violations: list[AuthoringPolicyViolation] = []

    if block_type in _banned_block_types_for_capability(capability):
        violations.append(
            AuthoringPolicyViolation(
                label=label,
                block_type=block_type,
                code=AuthoringViolationCode.BLOCK_TYPE_UNAVAILABLE,
                guidance=_render_block_policy_detail(block_type, _COPILOT_BLOCK_TYPE_POLICIES[block_type]),
            )
        )
    elif block_type in _AGENT_FAMILY_BLOCK_TYPES:
        # An omitted engine is filled in at the write seam; only a different engine is a refusal.
        if block.get("engine") and block.get("engine") != _TASK_V3_ENGINE:
            violations.append(
                AuthoringPolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=AuthoringViolationCode.ENGINE_NOT_SKYVERN_V3,
                    guidance="Set the submitted block engine exactly to `skyvern-3.0`.",
                )
            )
        if block_type == "validation" and block.get("complete_on_download") is True:
            violations.append(
                AuthoringPolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=AuthoringViolationCode.UNSUPPORTED_V3_COMBINATION,
                    guidance="Use a separate file_download block; Task V3 does not support download-gated validation.",
                )
            )

    if block_type == "for_loop":
        loop_variable_reference = block.get("loop_variable_reference")
        loop_over_parameter_key = block.get("loop_over_parameter_key")
        # The run renders the reference against its values and only synthesizes an extraction task when
        # nothing resolves, so a reference rooted at a name the run will hold is data, not a task.
        reference_root = (
            loop_variable_reference.strip(" {}").split(".", 1)[0].split("[", 1)[0]
            if isinstance(loop_variable_reference, str)
            else None
        )
        names_run_data = reference_root is not None and (
            reference_root == loop_over_parameter_key or reference_root in run_names | _LOOP_RUN_NAMES
        )
        if loop_variable_reference not in (None, "") and not names_run_data:
            violations.append(
                AuthoringPolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=AuthoringViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
                    guidance=(
                        "Point the loop at a workflow input or an earlier block by name (`<label>` or "
                        "`<label>_output`, or `loop_over_parameter_key`); free-form loop input creates a "
                        "synthetic task."
                    ),
                )
            )
    elif block_type == "while_loop":
        condition = block.get("condition")
        if isinstance(condition, Mapping) and condition.get("criteria_type", "jinja2_template") != "jinja2_template":
            violations.append(
                AuthoringPolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=AuthoringViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
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
                AuthoringPolicyViolation(
                    label=label,
                    block_type=block_type,
                    code=AuthoringViolationCode.SYNTHETIC_TASK_CONTROL_FLOW,
                    guidance="Use `jinja2_template` branch criteria; prompt criteria create a synthetic task.",
                )
            )

    loop_blocks = block.get("loop_blocks")
    if recurse and isinstance(loop_blocks, list):
        for nested in loop_blocks:
            if isinstance(nested, Mapping):
                violations.extend(_block_authoring_violations(nested, capability, run_names=run_names))
    return violations


_LOOP_RUN_NAMES = frozenset({"current_value", "current_item", "current_index"})


def workflow_run_names(workflow_yaml: str | None) -> frozenset[str]:
    """The names a run of this workflow holds values under: each input's key, and each block's label
    and `<label>_output`."""
    try:
        parsed = safe_load_no_dates(workflow_yaml) if workflow_yaml else None
    except yaml.YAMLError:
        return frozenset()
    definition = parsed.get("workflow_definition") if isinstance(parsed, dict) else None
    if not isinstance(definition, dict):
        return frozenset()
    parameters = definition.get("parameters")
    names = {
        parameter["key"]
        for parameter in (parameters if isinstance(parameters, list) else [])
        if isinstance(parameter, dict) and isinstance(parameter.get("key"), str)
    }
    blocks = definition.get("blocks")
    for label, _block in _walk_labelled_blocks(blocks if isinstance(blocks, list) else []):
        if label:
            names.update({label, f"{label}_output"})
    return frozenset(names)


def _authoring_violation_reject_message(
    violations: list[AuthoringPolicyViolation],
    ctx: AgentContext | None,
) -> str:
    details = " ".join(
        f"{violation.label} ({violation.block_type}, {violation.code.value}): {violation.guidance}"
        for violation in violations
    )
    return (
        f"The submitted workflow authors a block this turn may not author. {details} "
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
        _CODE_BLOCKS_ONLY_BANNED_BLOCK_TYPES,
    )


def _has_goal_prompt(block: Mapping[str, object]) -> bool:
    prompt = block.get("prompt")
    return isinstance(prompt, str) and bool(prompt.strip())


def _validator_relevant_fingerprint(block: Mapping[str, object]) -> tuple[object, ...]:
    """Only the fields :func:`_block_authoring_violations` reads. The prior YAML is the
    server-canonicalized draft, so a whole-mapping compare would report parameter binding and
    inherited settings as model edits."""
    raw_type = block.get("block_type")
    block_type = normalize_copilot_block_type_alias(raw_type.strip().lower()) if isinstance(raw_type, str) else None
    condition = block.get("condition")
    branch_conditions = block.get("branch_conditions")
    branch_criteria_types: tuple[object, ...] = ()
    if isinstance(branch_conditions, list):
        branch_criteria_types = tuple(
            branch["criteria"].get("criteria_type", "jinja2_template")
            if isinstance(branch, Mapping) and isinstance(branch.get("criteria"), Mapping)
            else None
            for branch in branch_conditions
        )
    return (
        block_type,
        block.get("engine"),
        block.get("complete_on_download"),
        block.get("loop_variable_reference"),
        block.get("loop_over_parameter_key"),
        condition.get("criteria_type", "jinja2_template") if isinstance(condition, Mapping) else None,
        branch_criteria_types,
        _has_goal_prompt(block),
    )


def _walk_labelled_blocks(blocks: list[Any]) -> list[tuple[str | None, Mapping[str, object]]]:
    walked: list[tuple[str | None, Mapping[str, object]]] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        raw_label = block.get("label")
        walked.append((raw_label if isinstance(raw_label, str) else None, block))
        loop_blocks = block.get("loop_blocks")
        if isinstance(loop_blocks, list):
            walked.extend(_walk_labelled_blocks(loop_blocks))
    return walked


def _rewrites_a_banned_block(
    block: Mapping[str, object],
    prior_block: Mapping[str, object] | None,
    capability: AuthoringCapability,
) -> bool:
    """Whether a submission rewrites the content of a block whose type this capability bans.

    Grandfathering compares only the fields the validator reads, so a rewrite that leaves all of them
    alone — new code in a `code` block, a new goal on an agent block — would otherwise carry a banned
    type past the ban. Only the fields the submission states are compared: the prior YAML is the
    server-canonicalized draft, and what the server added to it is not the model's edit.
    """
    if prior_block is None:
        return False
    raw_type = block.get("block_type")
    if not isinstance(raw_type, str):
        return False
    block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
    if block_type not in _banned_block_types_for_capability(capability):
        return False
    return _states_new_content(block, prior_block)


def _states_new_content(block: Mapping[str, object], prior_block: Mapping[str, object]) -> bool:
    return any(
        key not in _RENAME_IGNORED_FIELDS and _stated_value(prior_block.get(key)) != _stated_value(value)
        for key, value in block.items()
    )


def _stated_value(value: object) -> object:
    # A `|` block scalar ends in a newline that `|-` does not, so trailing whitespace is YAML's, not an edit.
    return value.rstrip() if isinstance(value, str) else value


def _changed_blocks(
    submitted_yaml: str,
    prior_workflow_yaml: str | None,
    capability: AuthoringCapability = ALL_BLOCK_FAMILIES,
) -> list[tuple[str, Mapping[str, object]]]:
    submitted_blocks = _parse_workflow_blocks(submitted_yaml)
    if submitted_blocks is None:
        return []
    return _changed_blocks_in(submitted_blocks, prior_workflow_yaml, capability)


def _changed_blocks_in(
    submitted_blocks: list[Any],
    prior_workflow_yaml: str | None,
    capability: AuthoringCapability = ALL_BLOCK_FAMILIES,
) -> list[tuple[str, Mapping[str, object]]]:
    """Blocks the turn introduces or changes: a new label, or the same label with a different
    answer on a field the validator reads. An unlabeled block counts as new. The returned mappings
    are the submitted objects themselves, so a caller may edit them in place.

    A new label carrying the content of a prior block that disappeared in the same write is a
    rename, not an introduction. Refusing a rename would push the model to migrate a working block's
    engine to recover from a change the user never asked for."""
    prior_blocks = _walk_labelled_blocks(_parse_workflow_blocks(prior_workflow_yaml) or [])
    prior_fingerprints = {
        label: _validator_relevant_fingerprint(block) for label, block in prior_blocks if label is not None
    }
    prior_by_label = {label: block for label, block in prior_blocks if label is not None}
    submitted_walk = _walk_labelled_blocks(submitted_blocks)
    surviving_labels = {label for label, _ in submitted_walk if label is not None}
    # A rename is matched on the whole block, not the validator's fingerprint: type and engine alone
    # would let a rewrite drop legacy block A and add an unrelated B of the same type past the pin.
    vanished = Counter(
        _rename_identity(block) for label, block in prior_blocks if label is not None and label not in surviving_labels
    )
    changed: list[tuple[str, Mapping[str, object]]] = []
    for label, block in submitted_walk:
        if label is None:
            changed.append(("(unlabeled)", block))
            continue
        prior = prior_fingerprints.get(label)
        if prior is not None:
            if prior != _validator_relevant_fingerprint(block) or _rewrites_a_banned_block(
                block, prior_by_label.get(label), capability
            ):
                changed.append((label, block))
            continue
        identity = _rename_identity(block)
        if vanished[identity]:
            vanished[identity] -= 1
            continue
        changed.append((label, block))
    return changed


_RENAME_IGNORED_FIELDS = frozenset({"label", "next_block_label", "loop_blocks"})


def _rename_identity(block: Mapping[str, object]) -> str:
    """Everything about a block except its name and its links, canonically serialized."""
    return json.dumps(
        {key: value for key, value in block.items() if key not in _RENAME_IGNORED_FIELDS},
        sort_keys=True,
        default=str,
    )


def _legacy_engine_block_labels(
    prior_workflow_yaml: str | None,
    submitted_yaml: str,
    changed_labels: set[str],
) -> list[str]:
    """Untouched agent-family blocks that predate the Task V3 engine and survive this write. A fact
    for the write result, never an instruction."""
    surviving = {
        label for label, _ in _walk_labelled_blocks(_parse_workflow_blocks(submitted_yaml) or []) if label is not None
    }
    return sorted(
        {
            label
            for label, block in _walk_labelled_blocks(_parse_workflow_blocks(prior_workflow_yaml) or [])
            if label is not None
            and label in surviving
            and label not in changed_labels
            and isinstance(block.get("block_type"), str)
            and normalize_copilot_block_type_alias(str(block["block_type"]).strip().lower())
            in _AGENT_FAMILY_BLOCK_TYPES
            and block.get("engine") != _TASK_V3_ENGINE
        }
    )


def _code_block_goal_findings(changed: list[tuple[str, Mapping[str, object]]]) -> list[str]:
    """Absent code_artifact_metadata is the normalizer's own finding; only the Goal is reported here."""
    missing = [
        label
        for label, block in changed
        if isinstance(block.get("block_type"), str)
        and normalize_copilot_block_type_alias(str(block["block_type"]).strip().lower()) == "code"
        and not _has_goal_prompt(block)
    ]
    if not missing:
        return []
    return [
        f"Code blocks {', '.join(sorted(missing))} carry no `prompt`, which is the plain-language Goal "
        "shown in the editor. Read get_block_schema with block_type `code` for what it must say."
    ]


@dataclass(frozen=True)
class AuthoringValidation:
    reject: AuthorTimeBlock | None = None
    legacy_engine_blocks: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    workflow_yaml: str = ""


def _prior_block_engines(prior_workflow_yaml: str | None) -> dict[str, str]:
    """The engine each prior label already runs on."""
    engines: dict[str, str] = {}
    for label, block in _walk_labelled_blocks(_parse_workflow_blocks(prior_workflow_yaml) or []):
        engine = block.get("engine")
        if label is not None and isinstance(engine, str) and engine:
            engines[label] = engine
    return engines


def _pin_agent_block_engines(
    changed: list[tuple[str, Mapping[str, object]]],
    prior_engines: Mapping[str, str],
) -> bool:
    """Fill the engine on every changed agent-family block that names none.

    A label the workflow already had keeps the engine it already ran on: omitting the field is how a
    whole-document write carries a block it did not touch, not a request to move it to another
    engine. Only a label the workflow did not have is pinned to Task V3.
    """
    pinned = False
    for label, block in changed:
        raw_type = block.get("block_type")
        if not isinstance(raw_type, str) or not isinstance(block, dict):
            continue
        block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
        if block_type in _AGENT_FAMILY_BLOCK_TYPES and not block.get("engine"):
            block["engine"] = prior_engines.get(label, _TASK_V3_ENGINE)
            pinned = True
    return pinned


def reject_authoring_violations(
    ctx: AgentContext | None,
    submitted_yaml: str,
    source_tool: str,
    *,
    prior_workflow_yaml: str | None = None,
) -> AuthoringValidation:
    """The one author-time authoring-policy pass, shared by every write path.

    Only blocks the turn introduces or changes are validated, so an untouched legacy block does not
    force a rewrite before any edit is accepted. An accepted write carries ``workflow_yaml`` with
    the changed agent blocks pinned to the Task V3 engine; the caller persists that string.
    """
    capability = _copilot_authoring_capability(ctx)
    prior_yaml = prior_workflow_yaml if prior_workflow_yaml is not None else (ctx.workflow_yaml if ctx else None)
    try:
        parsed = safe_load_no_dates(submitted_yaml) if submitted_yaml else None
    except yaml.YAMLError:
        parsed = None
    definition = parsed.get("workflow_definition") if isinstance(parsed, dict) else None
    blocks = definition.get("blocks") if isinstance(definition, dict) else None
    changed = _changed_blocks_in(blocks, prior_yaml, capability) if isinstance(blocks, list) else []
    violations: list[AuthoringPolicyViolation] = []
    run_names = workflow_run_names(submitted_yaml)
    for _label, block in changed:
        # _changed_blocks already walked loop_blocks, so a second recursion would double-report.
        violations.extend(_block_authoring_violations(block, capability, recurse=False, run_names=run_names))
    legacy_engine_blocks = tuple(
        _legacy_engine_block_labels(prior_yaml, submitted_yaml, {label for label, _ in changed})
    )
    if not violations:
        workflow_yaml = submitted_yaml
        if isinstance(parsed, dict) and _pin_agent_block_engines(changed, _prior_block_engines(prior_yaml)):
            workflow_yaml = dump_workflow_yaml(parsed)
        prior_blocks = {
            label: block
            for label, block in _walk_labelled_blocks(_parse_workflow_blocks(prior_yaml) or [])
            if label is not None
        }
        changed_ids = {id(block) for _label, block in changed}
        rewritten = [
            (label, block)
            for label, block in _walk_labelled_blocks(blocks if isinstance(blocks, list) else [])
            if label in prior_blocks
            and id(block) not in changed_ids
            and _states_new_content(block, prior_blocks[label])
        ]
        _record_authored_families(ctx, changed + rewritten, prior_blocks)
        return AuthoringValidation(
            legacy_engine_blocks=legacy_engine_blocks,
            findings=tuple(_code_block_goal_findings(changed)),
            workflow_yaml=workflow_yaml,
        )
    for violation in violations:
        policy = _COPILOT_BLOCK_TYPE_POLICIES.get(violation.block_type)
        if policy is not None:
            _record_code_native_pending_capability(ctx, policy)
    _record_banned_block_reject_span(source_tool, [(v.label, v.block_type) for v in violations])
    return AuthoringValidation(
        reject=AuthorTimeBlock(
            block_id=BANNED_BLOCKS_BLOCK_ID,
            error=_authoring_violation_reject_message(violations, ctx),
            data={"violations": [violation.as_dict() for violation in violations]},
        ),
        legacy_engine_blocks=legacy_engine_blocks,
        workflow_yaml=submitted_yaml,
    )


def _failed_test_labels(ctx: AgentContext | None) -> set[str]:
    history = getattr(ctx, "recorded_build_test_outcome_history", None)
    labels: set[str] = set()
    for entry in history if isinstance(history, list) else []:
        if isinstance(entry, Mapping) and entry.get("verdict") == "repairable_failure":
            labels.update(label for label in (entry.get("block_labels") or []) if isinstance(label, str))
            if isinstance(entry.get("attempted_block_label"), str):
                labels.add(entry["attempted_block_label"])
    return labels


def _block_type_and_family(block: Mapping[str, object]) -> tuple[str, str] | None:
    raw_type = block.get("block_type")
    if not isinstance(raw_type, str):
        return None
    block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
    family = "code" if block_type == "code" else "agent" if block_type in _AGENT_FAMILY_BLOCK_TYPES else None
    return (block_type, family) if family else None


def _record_authored_families(
    ctx: AgentContext | None,
    written: list[tuple[str, Mapping[str, object]]],
    prior_blocks: Mapping[str, Mapping[str, object]],
) -> None:
    """Per write, the family each written block was in and whether a test of that block had failed first.
    Start-versus-end diffing sees neither a block that changed family within one turn nor one rewritten
    in the same family after each failed test; a block that predates the turn starts from its saved type."""
    families = getattr(ctx, "authored_block_families", None)
    if not isinstance(families, dict):
        return
    failed = _failed_test_labels(ctx)
    for label, block in written:
        typed = _block_type_and_family(block)
        if label == "(unlabeled)" or typed is None:
            continue
        block_type, family = typed
        if label not in families:
            families[label] = []
            prior_typed = _block_type_and_family(prior_blocks[label]) if label in prior_blocks else None
            if prior_typed is not None:
                families[label].append(
                    {"block_type": prior_typed[0], "family": prior_typed[1], "after_failed_test": False}
                )
        entries = families[label]
        after_failed_test = label in failed
        if entries and entries[-1]["block_type"] == block_type and not after_failed_test:
            continue
        entries.append({"block_type": block_type, "family": family, "after_failed_test": after_failed_test})


def authoring_turn_summary(
    turn_start_yaml: str | None,
    final_yaml: str | None,
    authored_block_families: Mapping[str, list[dict[str, object]]] | None = None,
) -> dict[str, list[str] | dict[str, str] | dict[str, int]]:
    """What the turn authored, by block family. Log-only."""
    prior = {
        label: block
        for label, block in _walk_labelled_blocks(_parse_workflow_blocks(turn_start_yaml) or [])
        if label is not None
    }
    introduced_code: list[str] = []
    introduced_agent: dict[str, str] = {}
    type_changes: dict[str, str] = {}
    for label, block in _walk_labelled_blocks(_parse_workflow_blocks(final_yaml) or []):
        raw_type = block.get("block_type")
        if label is None or not isinstance(raw_type, str):
            continue
        block_type = normalize_copilot_block_type_alias(raw_type.strip().lower())
        prior_block = prior.get(label)
        if prior_block is None:
            if block_type == "code":
                introduced_code.append(label)
            elif block_type in _AGENT_FAMILY_BLOCK_TYPES:
                introduced_agent[label] = block_type
            continue
        prior_raw_type = prior_block.get("block_type")
        prior_type = (
            normalize_copilot_block_type_alias(prior_raw_type.strip().lower())
            if isinstance(prior_raw_type, str)
            else None
        )
        if prior_type is not None and prior_type != block_type:
            type_changes[label] = f"{prior_type}->{block_type}"
    mid_turn_switches: dict[str, str] = {}
    switches_after_failed_test: list[str] = []
    same_family_rewrites_after_failed_test: dict[str, int] = {}
    for label, entries in (authored_block_families or {}).items():
        steps = list(zip(entries, entries[1:]))
        kept = sum(
            1 for before, after in steps if after.get("after_failed_test") and after["family"] == before["family"]
        )
        if kept:
            same_family_rewrites_after_failed_test[label] = kept
        if len({entry.get("family") for entry in entries}) < 2:
            continue
        mid_turn_switches[label] = f"{entries[0]['block_type']}->{entries[-1]['block_type']}"
        if any(after.get("after_failed_test") and after["family"] != before["family"] for before, after in steps):
            switches_after_failed_test.append(label)
    return {
        "introduced_code_blocks": sorted(introduced_code),
        "introduced_agent_blocks": introduced_agent,
        "block_type_changes": type_changes,
        "mid_turn_family_switches": mid_turn_switches,
        "switches_after_failed_test": sorted(switches_after_failed_test),
        "same_family_rewrites_after_failed_test": same_family_rewrites_after_failed_test,
    }
