from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agents import function_tool
from agents.run_context import RunContextWrapper
from pydantic import JsonValue

from skyvern.forge import app
from skyvern.forge.agent_functions import CopilotTriggerResult
from skyvern.forge.sdk.copilot.context import CREATE_WEBHOOK_TRIGGER_TOOL_NAME, CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

from .errors import copilot_tool_failure
from .guardrails import _authority_tool_error

TEST_TRIGGER_MAPPING_TOOL_NAME = "test_trigger_mapping"
LIST_TRIGGERS_TOOL_NAME = "list_workflow_triggers"

SECRET_NOT_SHOWN = (
    "The signing secret is not shown in this chat. Until the user clicks Rotate secret on this trigger in the "
    "Studio Triggers panel and gives their sender the new secret, every event sent to the trigger is rejected."
)
CREATE_WEBHOOK_TRIGGER_DESCRIPTION = (
    "Create a webhook trigger: each accepted signed JSON event POSTed to it starts one run of the saved workflow.\n"
    "\n"
    '`mapping` maps each workflow input key to a JSON Pointer into the event body, e.g. {"order_id": "/order_id"}.\n'
    "Creating starts no run. The trigger rejects every event (`accepting_events: false`) until the user clicks Rotate\n"
    "secret on it in the Studio Triggers panel and gives their sender the new secret; this chat never sees the secret."
)
TEST_TRIGGER_MAPPING_DESCRIPTION = (
    "Test a trigger mapping on a sample event body. Creates nothing: no trigger, delivery or run.\n"
    "\n"
    "Resolves `mapping` (workflow input key to JSON Pointer) on `sample_payload` as a real event would be, then\n"
    "validates the inputs against this workflow's saved inputs. A rejection's `reason_code` is `mapping_path_missing`\n"
    "(a pointer finds nothing), `invalid_mapping` (a value is not a JSON Pointer), `invalid_parameters` (inputs unknown,\n"
    "missing or invalid for the saved workflow) or `owner_lookup_failed` (role lookup failed; retry later). The user\n"
    "sees each resolved `input = value` pair in this step's result row."
)
LIST_TRIGGERS_DESCRIPTION = (
    "List this workflow's event triggers.\n"
    "\n"
    "`enabled: true` does not mean events are accepted: only events signed with the trigger's current secret are, and\n"
    "a trigger whose secret the user never saw rejects every event until they click Rotate secret on it in the Studio\n"
    "Triggers panel."
)
SAVED_WORKFLOW_NOTE = "Validated against this workflow's saved inputs; inputs in an unaccepted proposal do not count."


def _finish(ctx: CopilotContext, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
    record_tool_step_result_for_ctx(ctx, tool_name, arguments, result)
    return json.dumps(sanitize_tool_result_for_llm(tool_name, result))


def _failure(result: CopilotTriggerResult) -> dict[str, Any] | None:
    return {"ok": False, "error": result.error} if result.error is not None else None


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=CREATE_WEBHOOK_TRIGGER_TOOL_NAME,
    description_override=CREATE_WEBHOOK_TRIGGER_DESCRIPTION,
    strict_mode=False,
)
async def create_webhook_trigger_tool(ctx: RunContextWrapper[CopilotContext], mapping: dict[str, str]) -> str:
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {"mapping": mapping}
    authority_error = _authority_tool_error(copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME)
    if authority_error:
        return _finish(
            copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME, arguments, {"ok": False, "error": authority_error}
        )
    created = await app.AGENT_FUNCTION.create_copilot_webhook_trigger(
        organization_id=copilot_ctx.organization_id,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        mapping=mapping,
    )
    result = _failure(created) or {
        "ok": True,
        "data": {
            "trigger": asdict(created.triggers[0]),
            "accepting_events": False,
            "signing_secret_status": SECRET_NOT_SHOWN,
        },
    }
    return _finish(copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME, arguments, result)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=TEST_TRIGGER_MAPPING_TOOL_NAME,
    description_override=TEST_TRIGGER_MAPPING_DESCRIPTION,
    strict_mode=False,
)
async def test_trigger_mapping_tool(
    ctx: RunContextWrapper[CopilotContext], mapping: dict[str, str], sample_payload: dict[str, JsonValue]
) -> str:
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {"mapping": mapping, "sample_payload": sample_payload}
    authority_error = _authority_tool_error(copilot_ctx, TEST_TRIGGER_MAPPING_TOOL_NAME)
    if authority_error:
        return _finish(copilot_ctx, TEST_TRIGGER_MAPPING_TOOL_NAME, arguments, {"ok": False, "error": authority_error})
    tested = await app.AGENT_FUNCTION.dry_run_copilot_trigger_mapping(
        organization_id=copilot_ctx.organization_id,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        mapping=mapping,
        payload=sample_payload,
    )
    outcome: dict[str, Any] = (
        {"accepted": False, "reason_code": tested.reason_code}
        if tested.reason_code is not None
        else {"accepted": True, "resolved_parameters": tested.resolved_parameters}
    )
    result = _failure(tested) or {"ok": True, "data": {**outcome, "note": SAVED_WORKFLOW_NOTE}}
    return _finish(copilot_ctx, TEST_TRIGGER_MAPPING_TOOL_NAME, arguments, result)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=LIST_TRIGGERS_TOOL_NAME,
    description_override=LIST_TRIGGERS_DESCRIPTION,
)
async def list_workflow_triggers_tool(ctx: RunContextWrapper[CopilotContext]) -> str:
    copilot_ctx = ctx.context
    authority_error = _authority_tool_error(copilot_ctx, LIST_TRIGGERS_TOOL_NAME)
    if authority_error:
        return _finish(copilot_ctx, LIST_TRIGGERS_TOOL_NAME, {}, {"ok": False, "error": authority_error})
    listed = await app.AGENT_FUNCTION.list_copilot_workflow_triggers(
        organization_id=copilot_ctx.organization_id,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
    )
    result = _failure(listed) or {"ok": True, "data": {"triggers": [asdict(trigger) for trigger in listed.triggers]}}
    return _finish(copilot_ctx, LIST_TRIGGERS_TOOL_NAME, {}, result)
