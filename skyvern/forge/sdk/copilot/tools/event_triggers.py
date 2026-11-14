from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from agents import function_tool
from agents.run_context import RunContextWrapper
from pydantic import JsonValue

from skyvern.forge import app
from skyvern.forge.agent_functions import CopilotTriggerResult, CopilotTriggerSource
from skyvern.forge.sdk.copilot.context import CREATE_WEBHOOK_TRIGGER_TOOL_NAME, CopilotContext
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.output_utils import sanitize_tool_result_for_llm

from .errors import copilot_tool_failure
from .guardrails import _authority_tool_error

TEST_TRIGGER_MAPPING_TOOL_NAME = "test_trigger_mapping"
LIST_TRIGGERS_TOOL_NAME = "list_workflow_triggers"

CREATE_WEBHOOK_TRIGGER_DESCRIPTION = (
    "Create an event trigger on the saved workflow: each accepted event from the source starts one run.\n"
    "\n"
    "`source_kind` is a source below that can be created here; `settings` must fit that source's settings schema.\n"
    '`mapping` maps each workflow input key to a JSON Pointer into the event body, e.g. {"order_id": "/order_id"}.\n'
    "The trigger is created switched off and starts no run. A card shown in this chat lets the user turn it on (a\n"
    "source that fires on its own) or open it in the Studio Triggers panel; no tool turns a trigger on. A source that\n"
    "can't be created here is set up where its entry says."
)
TEST_TRIGGER_MAPPING_DESCRIPTION = (
    "Test a trigger mapping on a sample event body. Creates nothing: no trigger, delivery or run.\n"
    "\n"
    "Resolves `mapping` (workflow input key to JSON Pointer) on `sample_payload` as a real event from `source_kind`\n"
    "would be (omit `sample_payload` to use that source's made-up example event, listed in the\n"
    f"`{CREATE_WEBHOOK_TRIGGER_TOOL_NAME}` description; `sample` then says `source_example_event`), then\n"
    "validates the inputs against this workflow's saved inputs. A rejection's `reason_code` is `mapping_path_missing`\n"
    "(a pointer finds nothing), `invalid_mapping` (a value is not a JSON Pointer), `invalid_parameters` (inputs unknown,\n"
    "missing or invalid for the saved workflow), `owner_lookup_failed` (role lookup failed; retry later) or\n"
    "`owner_not_authorized` (the trigger owner cannot access a required credential; fix credential access). The user\n"
    "sees each resolved `input = value` pair in this step's result row."
)
LIST_TRIGGERS_DESCRIPTION = (
    "List this workflow's event triggers with each one's `source_kind`, `settings` and `mapping`. A trigger's\n"
    "`note`, when present, says what its source needs before events are accepted; `enabled: true` alone does not."
)
SAVED_WORKFLOW_NOTE = "Validated against this workflow's saved inputs; inputs in an unaccepted proposal do not count."


def _finish(ctx: CopilotContext, tool_name: str, arguments: dict[str, Any], result: dict[str, Any]) -> str:
    record_tool_step_result_for_ctx(ctx, tool_name, arguments, result)
    return json.dumps(sanitize_tool_result_for_llm(tool_name, result))


def _failure(result: CopilotTriggerResult) -> dict[str, Any] | None:
    return {"ok": False, "error": result.error} if result.error is not None else None


def render_trigger_sources(sources: list[CopilotTriggerSource]) -> str:
    if not sources:
        return ""
    lines = ["Trigger sources:"]
    for source in sources:
        where = "can be created here" if source.creatable_in_copilot else f"set up through {source.set_up_in}"
        lines.append(
            f"- `{source.source_kind}` ({where}; fires on its own: {'yes' if source.fires_on_its_own else 'no'}): "
            f"{source.description} Settings schema: {json.dumps(source.settings_schema)}. "
            f"Example event: {json.dumps(source.example_event)}"
        )
    return "\n".join(lines)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=CREATE_WEBHOOK_TRIGGER_TOOL_NAME,
    description_override=CREATE_WEBHOOK_TRIGGER_DESCRIPTION,
    strict_mode=False,
)
async def create_webhook_trigger_tool(
    ctx: RunContextWrapper[CopilotContext],
    source_kind: str,
    mapping: dict[str, str],
    settings: dict[str, JsonValue] | None = None,
) -> str:
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {"source_kind": source_kind, "mapping": mapping, "settings": settings or {}}
    authority_error = _authority_tool_error(copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME)
    if authority_error:
        return _finish(
            copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME, arguments, {"ok": False, "error": authority_error}
        )
    created = await app.AGENT_FUNCTION.create_copilot_trigger(
        organization_id=copilot_ctx.organization_id,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        source_kind=source_kind,
        settings=settings or {},
        mapping=mapping,
        # Copilot never turns a trigger on; the user does, from the chat card or the Studio Triggers panel.
        enabled=False,
    )
    if created.triggers:
        trigger = created.triggers[0]
        copilot_ctx.created_triggers.append(
            {
                "eventTriggerId": trigger.event_trigger_id,
                "sourceKind": trigger.source_kind,
                "filters": [{"label": row["label"], "value": row["value"]} for row in trigger.filters],
                "firesOnItsOwn": trigger.fires_on_its_own,
                "connectionId": trigger.account_id,
            }
        )
    # `filters` restates the settings in words for the user's card; the model reads the settings themselves.
    data: dict[str, Any] = (
        {"trigger": {key: value for key, value in asdict(created.triggers[0]).items() if key != "filters"}}
        if created.triggers
        else {}
    )
    if created.after_create:
        data["after_create"] = created.after_create
    result = _failure(created) or {"ok": True, "data": data}
    return _finish(copilot_ctx, CREATE_WEBHOOK_TRIGGER_TOOL_NAME, arguments, result)


@function_tool(
    failure_error_function=copilot_tool_failure,
    name_override=TEST_TRIGGER_MAPPING_TOOL_NAME,
    description_override=TEST_TRIGGER_MAPPING_DESCRIPTION,
    strict_mode=False,
)
async def test_trigger_mapping_tool(
    ctx: RunContextWrapper[CopilotContext],
    source_kind: str,
    mapping: dict[str, str],
    sample_payload: dict[str, JsonValue] | None = None,
) -> str:
    copilot_ctx = ctx.context
    arguments: dict[str, Any] = {"source_kind": source_kind, "mapping": mapping, "sample_payload": sample_payload}
    authority_error = _authority_tool_error(copilot_ctx, TEST_TRIGGER_MAPPING_TOOL_NAME)
    if authority_error:
        return _finish(copilot_ctx, TEST_TRIGGER_MAPPING_TOOL_NAME, arguments, {"ok": False, "error": authority_error})
    tested = await app.AGENT_FUNCTION.dry_run_copilot_trigger_mapping(
        organization_id=copilot_ctx.organization_id,
        workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        source_kind=source_kind,
        mapping=mapping,
        payload=sample_payload,
    )
    outcome: dict[str, Any] = (
        {"accepted": False, "reason_code": tested.reason_code}
        if tested.reason_code is not None
        else {"accepted": True, "resolved_parameters": tested.resolved_parameters}
    )
    sample = "provided" if sample_payload is not None else "source_example_event"
    result = _failure(tested) or {"ok": True, "data": {**outcome, "sample": sample, "note": SAVED_WORKFLOW_NOTE}}
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
