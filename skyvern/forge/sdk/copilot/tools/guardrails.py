from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from agents import ToolGuardrailFunctionOutput, ToolInputGuardrail, ToolInputGuardrailData

from skyvern.forge.sdk.copilot.author_time_block import CREDENTIAL_SCOUT_BLOCK_ID, AuthorTimeBlock
from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
from skyvern.forge.sdk.copilot.build_test_outcome import (
    record_build_test_outcome,
    recorded_outcome_from_author_time_reject,
)
from skyvern.forge.sdk.copilot.output_policy import (
    OutputPolicyVerdict,
    demote_author_time_steer_reasons,
    evaluate_output_policy,
    format_output_policy_tool_error,
    output_policy_verdict_to_trace_data,
)
from skyvern.forge.sdk.copilot.request_policy import CREDENTIAL_DEFERRED_DRAFT_REASONS, RequestPolicy
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.turn_origin import TurnOrigin
from skyvern.forge.sdk.workflow.models.parameter import (
    OutputParameter,
    WorkflowParameter,
    WorkflowParameterType,
)
from skyvern.forge.sdk.workflow.models.workflow import Workflow

from ._shared import _emit_tool_blocker_signal

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

    effective_yaml = workflow_yaml

    verdict = evaluate_output_policy(
        request_policy=getattr(getattr(tool_context, "context", None), "request_policy", None),
        workflow_yaml=effective_yaml,
        tool_arguments=tool_arguments or raw_arguments,
    )
    steered_reasons = demote_author_time_steer_reasons(verdict)
    trace_data = output_policy_verdict_to_trace_data(
        verdict,
        surface="tool_input",
        tool_name=getattr(tool_context, "tool_name", None),
    )
    if steered_reasons:
        trace_data = {**trace_data, "steered_reason_codes": [reason.value for reason in steered_reasons]}
    if verdict.allowed:
        LOG.info("copilot output policy tool guardrail verdict", **trace_data)
        return ToolGuardrailFunctionOutput.allow(output_info=trace_data)
    LOG.info("copilot output policy tool guardrail verdict", **trace_data)
    block = AuthorTimeBlock(block_id=CREDENTIAL_SCOUT_BLOCK_ID, error=format_output_policy_tool_error(verdict))
    trace_data = {**trace_data, "block_id": block.block_id}
    tool_name = getattr(tool_context, "tool_name", None)
    if isinstance(tool_name, str) and tool_name:
        _record_output_policy_guardrail_outcome(
            getattr(tool_context, "context", None), tool_name, effective_yaml, verdict
        )
    return _guardrail_block(tool_context, tool_arguments, block, trace_data)


def _guardrail_block(
    tool_context: Any,
    tool_arguments: dict[str, Any],
    block: AuthorTimeBlock,
    trace_data: dict[str, Any],
) -> ToolGuardrailFunctionOutput:
    """The tool-input half of the author-time decision point: only an ``AuthorTimeBlock``
    turns a guardrail verdict into a rejection the model cannot author past."""
    return ToolGuardrailFunctionOutput.reject_content(block.error, output_info=trace_data)


def _record_output_policy_guardrail_outcome(
    ctx: object, tool_name: str, workflow_yaml: str | None, verdict: OutputPolicyVerdict
) -> None:
    if not isinstance(ctx, AgentContext):
        return
    reason_code_set = frozenset(reason.value for reason in verdict.reason_codes)
    structural_payload = {
        "surface": "output_policy_tool_input",
        "tool": tool_name,
        "reason_codes": sorted(reason_code_set),
        "workflow_yaml_hash": hashlib.sha256((workflow_yaml or "").encode("utf-8")).hexdigest(),
    }
    record_build_test_outcome(
        ctx,
        recorded_outcome_from_author_time_reject(
            reason_code="output_policy_reject",
            attempted_tool=tool_name,
            structural_payload=structural_payload,
        ),
    )


_WORKFLOW_YAML_OUTPUT_POLICY_GUARDRAIL = ToolInputGuardrail(
    guardrail_function=_workflow_yaml_output_policy_guardrail,
    name="workflow_yaml_output_policy_guardrail",
)


def _credential_deferred_draft_requires_skipped_run(ctx: AgentContext) -> bool:
    policy = getattr(ctx, "request_policy", None)
    if not isinstance(policy, RequestPolicy):
        return False
    if policy.raw_secret_detected and policy.raw_secret_handling == "redacted_draft":
        return True
    return policy.allow_missing_credentials_in_draft and (
        policy.clarification_reason in CREDENTIAL_DEFERRED_DRAFT_REASONS
    )


def _update_and_run_requires_skipped_run(ctx: AgentContext, tool_name: str) -> bool:
    return tool_name in {"update_and_run_blocks", "edit_block_and_run"} and (
        _credential_deferred_draft_requires_skipped_run(ctx)
    )


def _authority_tool_error(
    ctx: AgentContext,
    tool_name: str,
) -> str | None:
    if ctx.turn_origin == TurnOrigin.runtime_self_heal:
        return _emit_tool_blocker_signal(
            ctx,
            CopilotToolBlockerSignal(
                blocker_kind="tool_error",
                blocked_tool=tool_name,
                classifier_mode="runtime_self_heal",
                internal_reason_code="runtime_self_heal_native_tool_blocked",
                agent_steering_text=(
                    "Runtime self-heal allows browser MCP tools only; do not call native copilot tools."
                ),
                user_facing_reason="Runtime self-heal cannot use this tool.",
                recovery_hint="stop",
                renders_final_reply=False,
            ),
        )
    policy = ctx.request_policy
    if (
        tool_name
        in {
            "run_blocks_and_collect_debug",
            "edit_block_and_run",
            "discover_workflow_entrypoint",
            "search_web",
        }
        and isinstance(policy, RequestPolicy)
        and policy.raw_secret_detected
    ):
        return _emit_tool_blocker_signal(
            ctx,
            CopilotToolBlockerSignal(
                blocker_kind="tool_error",
                blocked_tool=tool_name,
                classifier_mode="raw_secret_safety",
                internal_reason_code="raw_secret_browser_action_blocked",
                agent_steering_text=(
                    "This turn contains a redacted raw secret. Do not use the browser; persist only the "
                    "redacted draft and ask the user to save the secret as a credential before testing."
                ),
                user_facing_reason=(
                    "I saved only a redacted draft and did not use the browser because the request contained a raw secret."
                ),
                recovery_hint="retry_with_different_tool",
                renders_final_reply=False,
            ),
        )
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
