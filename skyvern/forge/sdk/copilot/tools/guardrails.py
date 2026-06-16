from __future__ import annotations

import json
from typing import Any

import structlog
from agents import ToolGuardrailFunctionOutput, ToolInputGuardrail, ToolInputGuardrailData

from skyvern.config import settings
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal, RecoveryHint
from skyvern.forge.sdk.copilot.build_phase import _phase_blocker_signal
from skyvern.forge.sdk.copilot.composition_evidence import (
    composition_page_evidence_error,
    turn_has_scout_interaction,
    workflow_target_url,
)
from skyvern.forge.sdk.copilot.config import BlockAuthoringPolicy
from skyvern.forge.sdk.copilot.loop_detection import record_consecutive_tool_result_boundary_for_ctx
from skyvern.forge.sdk.copilot.output_policy import (
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
)
from skyvern.forge.sdk.copilot.reached_download_target import ReachedDownloadTarget, code_is_download_intent
from skyvern.forge.sdk.copilot.request_policy import CREDENTIAL_DEFERRED_DRAFT_REASONS, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.turn_intent import (
    NO_MUTATION_TURN_INTENT_MODES,
    READ_CONTEXT_DENIED_MODES,
    UNRESOLVED_BLOCK_REF_TARGET_ENTITY,
    TurnIntent,
    TurnIntentMode,
)
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow

from ._shared import (
    ANSWER_ONLY_CONTEXT_TOOLS,
    BLOCK_RUNNING_TOOLS,
    CREDENTIAL_METADATA_TOOLS,
    PAGE_SCHEMA_CONTEXT_TOOLS,
    WORKFLOW_MUTATION_TOOLS,
    _emit_tool_blocker_signal,
    _workflow_yaml_blocks_by_label,
)
from .banned_blocks import _copilot_block_authoring_policy

LOG = structlog.get_logger()


def _guardrail_tool_arguments(tool_context: Any) -> tuple[dict[str, Any], Any]:
    raw_arguments = getattr(tool_context, "tool_arguments", "")
    try:
        # Agents SDK guardrails may hand us either raw JSON or an already parsed mapping.
        parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError:
        parsed_arguments = {}
    return parsed_arguments if isinstance(parsed_arguments, dict) else {}, raw_arguments


def _workflow_yaml_output_policy_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    tool_context = data.context
    tool_arguments, raw_arguments = _guardrail_tool_arguments(tool_context)
    if not raw_arguments:
        LOG.warning(
            "workflow YAML output policy guardrail received no tool arguments",
            tool_name=getattr(tool_context, "tool_name", None),
            tool_call_id=getattr(tool_context, "tool_call_id", None),
        )
    workflow_yaml_value = tool_arguments.get("workflow_yaml")
    workflow_yaml = workflow_yaml_value if isinstance(workflow_yaml_value, str) else None
    verdict = evaluate_output_policy(
        request_policy=getattr(getattr(tool_context, "context", None), "request_policy", None),
        workflow_yaml=workflow_yaml,
        tool_arguments=tool_arguments or raw_arguments,
    )
    trace_data = output_policy_verdict_to_trace_data(
        verdict,
        surface="tool_input",
        tool_name=getattr(tool_context, "tool_name", None),
    )
    LOG.info("copilot output policy tool guardrail verdict", **trace_data)
    if not verdict.allowed:
        error = format_output_policy_tool_error(verdict)
        tool_name = getattr(tool_context, "tool_name", None)
        if isinstance(tool_name, str) and tool_name:
            record_consecutive_tool_result_boundary_for_ctx(
                getattr(tool_context, "context", None),
                tool_name,
                {"ok": False, "error": error},
            )
        return ToolGuardrailFunctionOutput.reject_content(error, output_info=trace_data)
    return ToolGuardrailFunctionOutput.allow(output_info=trace_data)


_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL = ToolInputGuardrail(
    guardrail_function=_workflow_yaml_output_policy_guardrail,
    name="workflow_yaml_output_policy_guardrail",
)

_COMPOSITION_EVIDENCE_PRECHECK_TRACE_DATA = {
    "surface": "tool_pre_side_effect",
    "tool_name": "update_and_run_blocks",
    "reason": "composition_page_evidence",
}


def _update_and_run_blocks_composition_evidence_precheck(
    copilot_ctx: Any,
    workflow_yaml: str | None,
    normalized_block_observation_refs: dict[str, int],
    raw_block_observation_refs: Any,
) -> str | None:
    if copilot_ctx is None or workflow_yaml is None:
        LOG.warning(
            "update_and_run_blocks composition evidence precheck missing context or workflow yaml",
            has_context=copilot_ctx is not None,
            has_workflow_yaml=workflow_yaml is not None,
        )
        return None

    evidence_error = composition_page_evidence_error(
        copilot_ctx,
        workflow_yaml,
        block_observation_refs=normalized_block_observation_refs,
        raw_block_observation_refs=raw_block_observation_refs,
    )

    if evidence_error:
        LOG.info(
            "copilot composition page evidence pre-side-effect rejected workflow",
            workflow_permanent_id=getattr(copilot_ctx, "workflow_permanent_id", None),
            target_url=workflow_target_url(workflow_yaml),
            surface="tool_pre_side_effect",
        )
        return evidence_error

    return None


_DOWNLOAD_SCOUT_REQUIRED_STEERING = (
    "This block downloads a file, and the terminal download step is compiled from the observed "
    "download affordance. Scout it first: reach the page that exposes the download control and "
    "call skyvern_evaluate to observe it (skyvern_evaluate cannot click — use the click tool to "
    "act), then re-author the block. The terminal download step will be compiled for you from the "
    "observed target, so you do not author the expect_download idiom yourself."
)
# After this many scout-act rejections in a turn, stop blocking and let the model halt honestly: a
# download whose affordance the scout never resolves is not authorable, so looping author->scout
# wastes turns. A genuinely scoutable download clears the gate on the first re-author.
_DOWNLOAD_SCOUT_REQUIRED_MAX_REJECTIONS = 3
_DOWNLOAD_SCOUT_UNREACHABLE_HALT = (
    "Repeated scout-acts did not resolve a single download affordance on this page, so the download "
    "step cannot be compiled. Tell the user you could not locate the download control and ask them to "
    "confirm where the file downloads from; do not author a hand-rolled download block."
)


def _download_intent_block_labels(workflow_yaml: str | None) -> list[str]:
    labels: list[str] = []
    for label, block in _workflow_yaml_blocks_by_label(workflow_yaml).items():
        if block.get("block_type") != "code":
            continue
        code = block.get("code")
        if isinstance(code, str) and code_is_download_intent(code):
            labels.append(label)
    return labels


def _has_reached_download_target(ctx: Any) -> bool:
    target = getattr(ctx, "reached_download_target", None)
    if not isinstance(target, ReachedDownloadTarget):
        return False
    return target.already_registered or bool(target.selector.strip())


def _download_scout_required_error(copilot_ctx: Any, workflow_yaml: str | None) -> str | None:
    """Reject authoring a download-intent code block until the affordance has been scout-acted
    this turn, so the skyvern_evaluate post-hook can populate the reached-download target and the
    synthesizer can compile the terminal download step. Active in code-only browser mode."""
    if _copilot_block_authoring_policy(copilot_ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    if not settings.COPILOT_REACHED_DOWNLOAD_TARGET_AUTHOR_STEER_ENABLED:
        # The gate is cleared by the reached-download scout interaction, which is only registered
        # when author-steer is on; enforcing it without that flag deadlocks until the retry cap.
        return None
    if copilot_ctx is None or workflow_yaml is None:
        return None
    download_labels = _download_intent_block_labels(workflow_yaml)
    if not download_labels:
        return None
    if _has_reached_download_target(copilot_ctx):
        return None
    if turn_has_scout_interaction(copilot_ctx):
        return None
    prior_rejections = int(getattr(copilot_ctx, "download_scout_required_rejections", 0) or 0)
    if prior_rejections >= _DOWNLOAD_SCOUT_REQUIRED_MAX_REJECTIONS:
        LOG.info(
            "copilot download scout-act gate exhausted retries; halting honestly",
            workflow_permanent_id=getattr(copilot_ctx, "workflow_permanent_id", None),
            download_intent_block_labels=download_labels,
            download_scout_required_rejections=prior_rejections,
            surface="tool_pre_side_effect",
        )
        return _DOWNLOAD_SCOUT_UNREACHABLE_HALT
    if hasattr(copilot_ctx, "download_scout_required_rejections"):
        copilot_ctx.download_scout_required_rejections = prior_rejections + 1
    LOG.info(
        "copilot download scout-act pre-side-effect rejected workflow",
        workflow_permanent_id=getattr(copilot_ctx, "workflow_permanent_id", None),
        download_intent_block_labels=download_labels,
        download_scout_required_rejections=prior_rejections + 1,
        surface="tool_pre_side_effect",
    )
    return _DOWNLOAD_SCOUT_REQUIRED_STEERING


def _submitted_code_blocks(workflow_yaml: str | None) -> list[str]:
    codes: list[str] = []
    for block in _workflow_yaml_blocks_by_label(workflow_yaml).values():
        if block.get("block_type") != "code":
            continue
        code = block.get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


_DOWNLOAD_BINDING_REQUIRED_STEERING = (
    "A correct scout-act reached a download affordance on the current page, but the authored block "
    "does not fire the browser download. Author ONE terminal download code block that clicks the "
    "captured target with the expect_download idiom; the captured selector is `{selector}`"
    "{affordance_hint}. The terminal download step is compiled for you when you scout-act the "
    "affordance, so re-author the block against the reached target rather than a static fetch."
)
_DOWNLOAD_BINDING_UNREACHABLE_HALT = (
    "The reached download affordance could not be bound to a terminal download step after repeated "
    "attempts. Tell the user you reached the download control but could not author the download step, "
    "and ask them to confirm where the file downloads from; do not author a download-less block."
)


def _download_binding_required_error(ctx: AgentContext | None, workflow_yaml: str | None) -> str | None:
    """Reject a code block authored after the scout reached a download affordance when that block does
    not fire the browser download. Keyed on the typed `ctx.reached_download_target` the model cannot
    edit, not on the submitted code form. Active in code-only browser mode."""
    if _copilot_block_authoring_policy(ctx) != BlockAuthoringPolicy.CODE_ONLY_BROWSER:
        return None
    if ctx is None or workflow_yaml is None:
        return None
    target = getattr(ctx, "reached_download_target", None)
    if not isinstance(target, ReachedDownloadTarget) or target.already_registered:
        return None
    if any(code_is_download_intent(code) for code in _submitted_code_blocks(workflow_yaml)):
        return None
    # Shared per-turn download-steering budget: this gate and _download_scout_required_error draw down the
    # same ctx.download_scout_required_rejections counter (combined cap, not 3 per gate).
    prior_rejections = int(getattr(ctx, "download_scout_required_rejections", 0) or 0)
    if prior_rejections >= _DOWNLOAD_SCOUT_REQUIRED_MAX_REJECTIONS:
        LOG.info(
            "copilot download binding gate exhausted retries; halting honestly",
            workflow_permanent_id=getattr(ctx, "workflow_permanent_id", None),
            reached_download_selector=target.selector,
            download_scout_required_rejections=prior_rejections,
            surface="tool_pre_side_effect",
        )
        return _DOWNLOAD_BINDING_UNREACHABLE_HALT
    if hasattr(ctx, "download_scout_required_rejections"):
        ctx.download_scout_required_rejections = prior_rejections + 1
    LOG.info(
        "copilot download binding pre-side-effect rejected workflow",
        workflow_permanent_id=getattr(ctx, "workflow_permanent_id", None),
        reached_download_selector=target.selector,
        download_scout_required_rejections=prior_rejections + 1,
        surface="tool_pre_side_effect",
    )
    affordance_hint = f" (affordance text: {target.affordance_text})" if target.affordance_text else ""
    return _DOWNLOAD_BINDING_REQUIRED_STEERING.format(selector=target.selector, affordance_hint=affordance_hint)


def _request_policy_tool_error(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    policy = getattr(ctx, "request_policy", None)
    if not isinstance(policy, RequestPolicy):
        return None

    agent_steering: str | None = None
    user_facing: str | None = None
    reason_code: str | None = None
    recovery_hint: RecoveryHint = "report_blocker_to_user"
    cleared_by: frozenset[str] = frozenset()

    if tool_name == "update_workflow" and not policy.allow_update_workflow:
        reason_code = "request_policy_blocks_update_workflow"
        agent_steering = (
            "Request policy blocks workflow updates for the latest user message. "
            "Ask the user for safe stored credential metadata instead."
        )
        user_facing = "I need stored credential metadata before I can update this workflow."
        recovery_hint = "ask_user_clarifying"
    elif tool_name in BLOCK_RUNNING_TOOLS and not policy.allow_run_blocks:
        if policy.testing_intent == "skip_test":
            reason_code = "request_policy_blocks_run_blocks_skip_test"
            agent_steering = (
                "Request policy says the latest user message asked for an untested draft. Use update_workflow only."
            )
            user_facing = "I'll save this as an untested draft because the request asked me not to run it."
            recovery_hint = "retry_with_different_tool"
            cleared_by = frozenset({"update_workflow"})
        elif policy.clarification_reason == "workflow_credential_inputs_unbound":
            reason_code = "request_policy_blocks_run_blocks_credential_unbound"
            agent_steering = (
                "Skipped test run: the existing workflow references credential parameters "
                "whose keys point to workflow inputs that are not configured. REPLY to the user "
                "with: 'I applied your requested change. I couldn't test the modified workflow "
                "because I couldn't find the required credentials — please add them via the "
                "Credentials UI, then I can try again.' Keep the unvalidated draft surfaced."
            )
            user_facing = (
                "I applied your requested change. I couldn't test the modified workflow because "
                "the required credentials aren't set up. Add them in the Credentials UI and ask "
                "me to test it."
            )
            recovery_hint = "report_blocker_to_user"
        else:
            reason_code = "request_policy_blocks_run_blocks_generic"
            agent_steering = (
                "Request policy blocks block-running tools for the latest user message. "
                "Ask the user for the required safe credential or clarification before testing."
            )
            user_facing = "I need a credential or clarification from you before I can test this workflow."
            recovery_hint = "ask_user_clarifying"

    if reason_code is None or agent_steering is None or user_facing is None:
        return None

    LOG.info(
        "copilot authority gate evaluated tool",
        authority_gate_layer="request_policy",
        blocked_tool=tool_name,
        request_policy_allow_update_workflow=policy.allow_update_workflow,
        request_policy_allow_run_blocks=policy.allow_run_blocks,
        request_policy_testing_intent=policy.testing_intent,
        request_policy_clarification_reason=policy.clarification_reason,
        safe_reason_code=reason_code,
    )
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text=agent_steering,
        user_facing_reason=user_facing,
        recovery_hint=recovery_hint,
        cleared_by_tools=cleared_by,
        internal_reason_code=reason_code,
        blocked_tool=tool_name,
    )


def _request_policy_allows_credential_deferred_draft(ctx: AgentContext) -> bool:
    policy = getattr(ctx, "request_policy", None)
    return (
        isinstance(policy, RequestPolicy)
        and policy.allow_update_workflow
        and not policy.allow_run_blocks
        and policy.allow_missing_credentials_in_draft
        and policy.clarification_reason in CREDENTIAL_DEFERRED_DRAFT_REASONS
    )


def _request_policy_allows_update_and_skip_run(ctx: AgentContext, tool_name: str) -> bool:
    return tool_name == "update_and_run_blocks" and _request_policy_allows_credential_deferred_draft(ctx)


def _turn_intent_has_edit_target(intent: TurnIntent) -> bool:
    # Keep this aligned with TurnIntent target kinds that make an edit specific enough to mutate safely.
    if any(
        intent.target_entities.get(entity_type)
        for entity_type in (
            "block",
            "run",
            "proposed_workflow",
            "latest_assistant_proposal",
            "proposal",
            "workflow_change",
        )
    ):
        return True
    return any(target != "current_workflow" for target in intent.target_entities.get("workflow", []))


def _turn_intent_tool_error(ctx: AgentContext, tool_name: str) -> CopilotToolBlockerSignal | None:
    intent = getattr(ctx, "turn_intent", None)
    if not isinstance(intent, TurnIntent):
        return None

    authority = intent.authority
    unresolved_refs = intent.target_entities.get(UNRESOLVED_BLOCK_REF_TARGET_ENTITY, [])
    if intent.mode == TurnIntentMode.EDIT and tool_name in WORKFLOW_MUTATION_TOOLS and unresolved_refs:
        reason_code = "turn_intent_unresolved_edit_target"
        labels = sorted(_workflow_yaml_blocks_by_label(getattr(ctx, "workflow_yaml", None)))
        label_hint = ", ".join(labels[:8]) if labels else "no labeled blocks"
        LOG.info(
            "copilot authority gate evaluated tool",
            authority_gate_layer="turn_intent",
            turn_intent_mode=intent.mode.value,
            turn_intent_target_entity_types=sorted(intent.target_entities),
            turn_intent_unresolved_refs=unresolved_refs,
            blocked_tool=tool_name,
            safe_reason_code=reason_code,
        )
        unresolved_str = ", ".join(unresolved_refs)
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                f"The latest user message references workflow/block identifier(s) that are not present in the "
                f"current workflow: {unresolved_str}. Current workflow labels include: {label_hint}. Ask the user "
                f"which current block should change before mutating or running blocks."
            ),
            user_facing_reason=(
                f"I couldn't find the block(s) you mentioned ({unresolved_str}). "
                f"Tell me which existing block to change."
            ),
            recovery_hint="ask_user_clarifying",
        )

    if (
        intent.mode == TurnIntentMode.EDIT
        and tool_name in WORKFLOW_MUTATION_TOOLS
        and not _turn_intent_has_edit_target(intent)
    ):
        reason_code = "turn_intent_missing_edit_target"
        LOG.info(
            "copilot authority gate evaluated tool",
            authority_gate_layer="turn_intent",
            turn_intent_mode=intent.mode.value,
            turn_intent_target_entity_types=sorted(intent.target_entities),
            blocked_tool=tool_name,
            safe_reason_code=reason_code,
        )
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Could not identify a specific workflow edit target. Ask the user which workflow/block should "
                "change before mutating."
            ),
            user_facing_reason="Tell me which block or workflow you'd like me to change.",
            recovery_hint="ask_user_clarifying",
        )

    blocks_update = tool_name in WORKFLOW_MUTATION_TOOLS and not authority.may_update_workflow
    blocks_run = tool_name in BLOCK_RUNNING_TOOLS and not authority.may_run_blocks
    blocks_page_inspection = tool_name in PAGE_SCHEMA_CONTEXT_TOOLS and (
        intent.mode in NO_MUTATION_TURN_INTENT_MODES or not authority.may_update_workflow
    )
    blocks_credential_metadata = tool_name in CREDENTIAL_METADATA_TOOLS and not (
        authority.may_update_workflow or authority.may_run_blocks
    )
    # Two paths grant read access to ANSWER_ONLY_CONTEXT_TOOLS, both excluded for DOCS_ANSWER/REFUSE/CLARIFY:
    #  (1) authority.may_read_run_context — classifier-derived (DIAGNOSE turns)
    #  (2) pending_reconciliation_run_id — within-turn override anchored to the run-blocks watchdog
    may_read_run_context = authority.may_read_run_context and intent.mode not in READ_CONTEXT_DENIED_MODES
    blocks_context_read = tool_name in ANSWER_ONLY_CONTEXT_TOOLS and not may_read_run_context

    within_turn_read_override = False
    if blocks_context_read and intent.mode not in READ_CONTEXT_DENIED_MODES:
        pending_run_id = getattr(ctx, "pending_reconciliation_run_id", None)
        if isinstance(pending_run_id, str) and pending_run_id:
            blocks_context_read = False
            within_turn_read_override = True
        else:
            same_turn_run_id = getattr(ctx, "last_successful_run_blocks_workflow_run_id", None) or getattr(
                ctx,
                "last_run_blocks_workflow_run_id",
                None,
            )
            if isinstance(same_turn_run_id, str) and same_turn_run_id:
                blocks_context_read = False
                within_turn_read_override = True

    if blocks_run and not blocks_update and _request_policy_allows_update_and_skip_run(ctx, tool_name):
        return None
    if (
        not blocks_update
        and not blocks_run
        and not blocks_context_read
        and not blocks_page_inspection
        and not blocks_credential_metadata
    ):
        if within_turn_read_override:
            LOG.info(
                "copilot authority gate allowed tool via within-turn read override",
                authority_gate_layer="turn_intent",
                turn_intent_mode=intent.mode.value,
                tool_name=tool_name,
                turn_intent_within_turn_read_override=True,
            )
        return None

    if intent.mode in NO_MUTATION_TURN_INTENT_MODES and blocks_run:
        reason_code = "turn_intent_no_mutation_run_blocked"
    elif intent.mode in NO_MUTATION_TURN_INTENT_MODES and blocks_update:
        reason_code = "turn_intent_no_mutation_update_blocked"
    elif blocks_update and blocks_run:
        reason_code = "turn_intent_no_mutation_run_blocked"
    elif blocks_update:
        reason_code = "turn_intent_update_blocked"
    elif blocks_page_inspection:
        reason_code = "turn_intent_page_inspection_blocked"
    elif blocks_context_read:
        reason_code = "turn_intent_context_read_blocked"
    elif blocks_credential_metadata:
        reason_code = "turn_intent_credential_metadata_blocked"
    else:
        reason_code = "turn_intent_run_blocked"
    LOG.info(
        "copilot authority gate evaluated tool",
        authority_gate_layer="turn_intent",
        turn_intent_mode=intent.mode.value,
        turn_intent_target_entity_types=sorted(intent.target_entities),
        turn_intent_may_update_workflow=authority.may_update_workflow,
        turn_intent_may_run_blocks=authority.may_run_blocks,
        turn_intent_may_read_run_context=authority.may_read_run_context,
        blocked_tool=tool_name,
        safe_reason_code=reason_code,
    )
    action = "ask the user" if authority.requires_user_input else "answer the user"
    detail = f" Ask: {intent.missing_context_question}" if intent.missing_context_question else ""

    if blocks_run and not blocks_update and authority.may_update_workflow:
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Browser blocks may not run for the latest user message. Use update_workflow only and keep the "
                f"draft unvalidated.{detail}"
            ),
            user_facing_reason="I'll save the change as a draft without running it.",
            recovery_hint="retry_with_different_tool",
            cleared_by_tools=frozenset({"update_workflow"}),
        )
    if blocks_context_read and not blocks_update and not blocks_run:
        return _build_turn_intent_signal(
            tool_name=tool_name,
            classifier_mode=intent.mode.value,
            reason_code=reason_code,
            agent_steering_text=(
                "Run context may not be read for the latest user message. Answer using the context already "
                f"provided.{detail}"
            ),
            user_facing_reason="I'll answer with the information I already have.",
            recovery_hint="report_blocker_to_user",
        )
    return _build_turn_intent_signal(
        tool_name=tool_name,
        classifier_mode=intent.mode.value,
        reason_code=reason_code,
        agent_steering_text=(
            "This tool is not allowed for the latest user message. Do not update workflow YAML or run browser "
            "blocks, and do not fetch additional run context with tools; "
            f"{action} using the available context instead.{detail}"
        ),
        user_facing_reason="I'll respond with the information I already have.",
        recovery_hint="ask_user_clarifying" if authority.requires_user_input else "report_blocker_to_user",
    )


def _build_turn_intent_signal(
    *,
    tool_name: str,
    classifier_mode: str,
    reason_code: str,
    agent_steering_text: str,
    user_facing_reason: str,
    recovery_hint: RecoveryHint,
    cleared_by_tools: frozenset[str] = frozenset(),
) -> CopilotToolBlockerSignal:
    return CopilotToolBlockerSignal(
        blocker_kind="authority_denied",
        agent_steering_text=agent_steering_text,
        user_facing_reason=user_facing_reason,
        recovery_hint=recovery_hint,
        cleared_by_tools=cleared_by_tools,
        internal_reason_code=reason_code,
        blocked_tool=tool_name,
        classifier_mode=classifier_mode,
    )


def _authority_tool_error(
    ctx: AgentContext,
    tool_name: str,
    *,
    ignore_request_policy_error: bool = False,
) -> str | None:
    # Request-policy precedes turn-intent unless explicitly ignored.
    turn_intent_signal = _turn_intent_tool_error(ctx, tool_name)
    request_policy_signal = _request_policy_tool_error(ctx, tool_name)
    if turn_intent_signal is not None and request_policy_signal is not None:
        LOG.info(
            "copilot authority gate blocked tool",
            authority_gate_layer="both",
            blocked_tool=tool_name,
        )
    chosen = (
        request_policy_signal
        if (request_policy_signal is not None and not ignore_request_policy_error)
        else turn_intent_signal
    )
    if chosen is not None:
        return _emit_tool_blocker_signal(ctx, chosen)
    phase_signal = _phase_blocker_signal(ctx, tool_name)
    if phase_signal is not None:
        LOG.info(
            "copilot authority gate blocked tool",
            authority_gate_layer="build_phase",
            blocked_tool=tool_name,
            build_phase=getattr(getattr(ctx, "build_phase", None), "value", None),
        )
        return _emit_tool_blocker_signal(ctx, phase_signal)
    return None


_PARAMETER_TYPE_PLACEHOLDERS: dict[WorkflowParameterType, Any] = {
    WorkflowParameterType.STRING: "",
    WorkflowParameterType.INTEGER: 0,
    WorkflowParameterType.FLOAT: 0.0,
    WorkflowParameterType.BOOLEAN: False,
    WorkflowParameterType.JSON: {},
    WorkflowParameterType.FILE_URL: "",
}


def _placeholder_for_parameter_type(param_type: WorkflowParameterType) -> Any:
    return _PARAMETER_TYPE_PLACEHOLDERS.get(param_type)


def _parameter_binding_invariant_error(
    workflow: Workflow,
    persisted_workflow_params: list[WorkflowParameter],
    persisted_output_params: list[OutputParameter],
) -> tuple[str, dict[str, list[str]], dict[str, list[str]]] | None:
    """Return a ``(summary, missing_persisted, missing_from_definition)`` tuple
    when ``workflow.workflow_definition`` disagrees with persisted
    definition-parameter rows for runtime-relevant classes. Returns ``None``
    when aligned.

    Compares ``WorkflowParameter`` rows by ``(key, workflow_parameter_type)``
    and ``OutputParameter`` rows by ``key``. Secret/credential and context
    parameters are intentionally out of scope — runtime reads those from the
    definition JSON.
    """
    definition = getattr(workflow, "workflow_definition", None)
    parameters = getattr(definition, "parameters", None) if definition else None
    parameters = list(parameters) if parameters else []

    def_workflow_ids: set[tuple[str, str]] = set()
    def_output_keys: set[str] = set()
    for parameter in parameters:
        if isinstance(parameter, WorkflowParameter):
            def_workflow_ids.add((parameter.key, parameter.workflow_parameter_type.value))
        elif isinstance(parameter, OutputParameter):
            def_output_keys.add(parameter.key)

    persisted_workflow_ids: set[tuple[str, str]] = {
        (wp.key, wp.workflow_parameter_type.value) for wp in persisted_workflow_params
    }
    persisted_output_keys: set[str] = {op.key for op in persisted_output_params}

    missing_persisted_workflow = sorted(
        f"{key} ({ptype})" for (key, ptype) in def_workflow_ids - persisted_workflow_ids
    )
    extra_persisted_workflow = sorted(f"{key} ({ptype})" for (key, ptype) in persisted_workflow_ids - def_workflow_ids)
    missing_persisted_output = sorted(def_output_keys - persisted_output_keys)
    extra_persisted_output = sorted(persisted_output_keys - def_output_keys)

    if (
        not missing_persisted_workflow
        and not extra_persisted_workflow
        and not missing_persisted_output
        and not extra_persisted_output
    ):
        return None

    summary = (
        "Pre-run invariant: workflow_definition and persisted parameter rows disagree. "
        f"workflow missing persisted: {missing_persisted_workflow or '[]'}; "
        f"workflow missing from definition: {extra_persisted_workflow or '[]'}; "
        f"output missing persisted: {missing_persisted_output or '[]'}; "
        f"output missing from definition: {extra_persisted_output or '[]'}"
    )
    return (
        summary,
        {"workflow": missing_persisted_workflow, "output": missing_persisted_output},
        {"workflow": extra_persisted_workflow, "output": extra_persisted_output},
    )
