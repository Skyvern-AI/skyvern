from __future__ import annotations

from typing import Any

import structlog

from skyvern.forge.sdk.copilot.blocker_signal import CopilotToolBlockerSignal
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


def _authority_tool_error(
    ctx: AgentContext,
    tool_name: str,
) -> str | None:
    if ctx.turn_origin == TurnOrigin.code_block_ai_fallback:
        return _emit_tool_blocker_signal(
            ctx,
            CopilotToolBlockerSignal(
                blocker_kind="tool_error",
                blocked_tool=tool_name,
                classifier_mode="code_block_ai_fallback",
                internal_reason_code="code_block_ai_fallback_native_tool_blocked",
                agent_steering_text=(
                    "The code block AI fallback can use only the browser tools; this tool is not available here."
                ),
                user_facing_reason="The code block AI fallback cannot use this tool.",
                recovery_hint="stop",
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
