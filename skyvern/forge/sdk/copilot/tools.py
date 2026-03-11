"""Copilot agent tools — native handlers, hooks, and registration."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections import defaultdict
from typing import Any

import structlog
import yaml
from agents import function_tool
from agents.run_context import RunContextWrapper
from pydantic import ValidationError

from skyvern.forge import app
from skyvern.forge.sdk.artifact.models import ArtifactType
from skyvern.forge.sdk.copilot.loop_detection import detect_tool_loop
from skyvern.forge.sdk.copilot.mcp_adapter import SchemaOverlay
from skyvern.forge.sdk.copilot.output_utils import (
    sanitize_tool_result_for_llm,
    truncate_output,
)
from skyvern.forge.sdk.copilot.runtime import AgentContext
from skyvern.forge.sdk.copilot.screenshot_utils import enqueue_screenshot_from_result
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.workflow.exceptions import BaseWorkflowHTTPException
from skyvern.forge.sdk.workflow.models.parameter import ParameterType, WorkflowParameterType
from skyvern.forge.sdk.workflow.models.workflow import Workflow, WorkflowRunStatus
from skyvern.forge.sdk.workflow.workflow_definition_converter import convert_workflow_definition
from skyvern.schemas.workflows import (
    LoginBlockYAML,
    WorkflowCreateYAMLRequest,
)

LOG = structlog.get_logger()

_FAILED_BLOCK_STATUSES = frozenset({"failed", "terminated", "canceled", "timed_out"})
RUN_BLOCKS_DEBUG_TIMEOUT_SECONDS = 180


async def _attach_action_traces(
    blocks: list,
    results: list[dict[str, Any]],
    organization_id: str,
) -> None:
    """For non-success blocks with a task_id, fetch and attach a compact action trace."""
    failed_task_ids = [
        b.task_id for b, r in zip(blocks, results) if b.task_id and r.get("status") in _FAILED_BLOCK_STATUSES
    ]
    if not failed_task_ids:
        return

    rows = await app.DATABASE.get_recent_actions_for_tasks(
        task_ids=failed_task_ids,
        organization_id=organization_id,
        per_task_limit=15,
    )

    actions_by_task: dict[str, list] = defaultdict(list)
    for row in rows:
        actions_by_task[row.task_id].append(row)

    for block, block_result in zip(blocks, results):
        if block_result.get("status") not in _FAILED_BLOCK_STATUSES or not block.task_id:
            continue
        task_actions = actions_by_task.get(block.task_id, [])
        block_result["action_trace"] = [
            {
                "action": a.action_type,
                "status": a.status,
                "reasoning": a.reasoning[:150] if a.reasoning else None,
                "element": a.element_id,
            }
            for a in task_actions
        ]


def _tool_loop_error(ctx: AgentContext, tool_name: str) -> str | None:
    tracker = getattr(ctx, "consecutive_tool_tracker", None)
    if not isinstance(tracker, list):
        return None
    return detect_tool_loop(tracker, tool_name)


def _placeholder_for_parameter_type(param_type: WorkflowParameterType) -> Any:
    _PLACEHOLDERS: dict[WorkflowParameterType, Any] = {
        WorkflowParameterType.STRING: "",
        WorkflowParameterType.INTEGER: 0,
        WorkflowParameterType.FLOAT: 0.0,
        WorkflowParameterType.BOOLEAN: False,
        WorkflowParameterType.JSON: {},
        WorkflowParameterType.FILE_URL: "",
    }
    return _PLACEHOLDERS.get(param_type)


def process_workflow_yaml(
    workflow_id: str,
    workflow_permanent_id: str,
    organization_id: str,
    workflow_yaml: str,
) -> Workflow:
    from datetime import datetime, timezone

    parsed_yaml = yaml.safe_load(workflow_yaml)

    workflow_definition = parsed_yaml.get("workflow_definition")
    if workflow_definition:
        blocks = workflow_definition.get("blocks", [])
        for block in blocks:
            block["title"] = block.get("title", "")

    workflow_yaml_request = WorkflowCreateYAMLRequest.model_validate(parsed_yaml)

    for block in workflow_yaml_request.workflow_definition.blocks:
        if isinstance(block, LoginBlockYAML) and not block.navigation_goal:
            from skyvern.forge.sdk.routes.run_blocks import DEFAULT_LOGIN_PROMPT

            block.navigation_goal = DEFAULT_LOGIN_PROMPT

    workflow_yaml_request.workflow_definition.parameters = [
        p for p in workflow_yaml_request.workflow_definition.parameters if p.parameter_type != ParameterType.OUTPUT
    ]

    updated_workflow_definition = convert_workflow_definition(
        workflow_definition_yaml=workflow_yaml_request.workflow_definition,
        workflow_id=workflow_id,
    )

    now = datetime.now(timezone.utc)
    return Workflow(
        workflow_id=workflow_id,
        organization_id=organization_id,
        title=workflow_yaml_request.title or "",
        workflow_permanent_id=workflow_permanent_id,
        version=1,
        is_saved_task=workflow_yaml_request.is_saved_task,
        description=workflow_yaml_request.description,
        workflow_definition=updated_workflow_definition,
        proxy_location=workflow_yaml_request.proxy_location,
        webhook_callback_url=workflow_yaml_request.webhook_callback_url,
        persist_browser_session=workflow_yaml_request.persist_browser_session or False,
        model=workflow_yaml_request.model,
        max_screenshot_scrolls=workflow_yaml_request.max_screenshot_scrolls,
        extra_http_headers=workflow_yaml_request.extra_http_headers,
        run_with=workflow_yaml_request.run_with,
        ai_fallback=workflow_yaml_request.ai_fallback,
        cache_key=workflow_yaml_request.cache_key,
        run_sequentially=workflow_yaml_request.run_sequentially,
        sequential_key=workflow_yaml_request.sequential_key,
        created_at=now,
        modified_at=now,
    )


async def _update_workflow(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    workflow_yaml = params["workflow_yaml"]
    try:
        workflow = process_workflow_yaml(
            workflow_id=ctx.workflow_id,
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            workflow_yaml=workflow_yaml,
        )
        await app.WORKFLOW_SERVICE.update_workflow_definition(
            workflow_id=ctx.workflow_id,
            organization_id=ctx.organization_id,
            title=workflow.title,
            description=workflow.description,
            workflow_definition=workflow.workflow_definition,
        )
        ctx.workflow_yaml = workflow_yaml
        return {
            "ok": True,
            "data": {
                "message": "Workflow updated successfully.",
                "block_count": len(workflow.workflow_definition.blocks) if workflow.workflow_definition else 0,
            },
            "_workflow": workflow,
        }
    except (yaml.YAMLError, ValidationError, BaseWorkflowHTTPException) as e:
        return {
            "ok": False,
            "error": f"Workflow validation failed: {e}",
        }


async def _list_credentials(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    page = params.get("page", 1)
    page_size = min(params.get("page_size", 10), 50)
    credentials = await app.DATABASE.get_credentials(
        organization_id=ctx.organization_id,
        page=page,
        page_size=page_size,
    )
    serialized = []
    for cred in credentials:
        entry: dict[str, Any] = {
            "credential_id": cred.credential_id,
            "name": cred.name,
            "credential_type": str(cred.credential_type),
        }
        if cred.username:
            entry["username"] = cred.username
            entry["totp_type"] = str(cred.totp_type) if cred.totp_type else None
        elif cred.card_last4:
            entry["card_last_four"] = cred.card_last4
            entry["card_brand"] = cred.card_brand
        elif cred.secret_label:
            entry["secret_label"] = cred.secret_label
        serialized.append(entry)
    return {
        "ok": True,
        "data": {
            "credentials": serialized,
            "page": page,
            "page_size": page_size,
            "count": len(serialized),
            "has_more": len(serialized) == page_size,
        },
    }


async def _run_blocks_and_collect_debug(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    block_labels = params["block_labels"]
    if not block_labels:
        return {"ok": False, "error": "block_labels must not be empty"}

    workflow = await app.DATABASE.get_workflow_by_permanent_id(
        workflow_permanent_id=ctx.workflow_permanent_id,
        organization_id=ctx.organization_id,
    )
    if not workflow:
        return {"ok": False, "error": f"Workflow not found: {ctx.workflow_permanent_id}"}

    for label in block_labels:
        if not workflow.get_output_parameter(label):
            return {"ok": False, "error": f"Block label not found in saved workflow: {label!r}"}

    from skyvern.forge.sdk.schemas.organizations import Organization
    from skyvern.forge.sdk.workflow.models.workflow import WorkflowRequestBody
    from skyvern.services import workflow_service

    org = await app.DATABASE.get_organization(organization_id=ctx.organization_id)
    if not org:
        return {"ok": False, "error": "Organization not found"}

    organization = Organization.model_validate(org)

    user_params: dict[str, Any] = params.get("parameters") or {}
    all_workflow_params = await app.WORKFLOW_SERVICE.get_workflow_parameters(
        workflow_id=workflow.workflow_id,
    )

    data: dict[str, Any] = {}
    for wp in all_workflow_params:
        if wp.key in user_params:
            data[wp.key] = user_params[wp.key]
        elif wp.default_value is not None:
            data[wp.key] = wp.default_value
        else:
            placeholder = _placeholder_for_parameter_type(wp.workflow_parameter_type)
            if placeholder is not None:
                data[wp.key] = placeholder
                LOG.info(
                    "Auto-filled missing workflow parameter for copilot test run",
                    parameter_key=wp.key,
                    parameter_type=str(wp.workflow_parameter_type),
                )

    workflow_request = WorkflowRequestBody(
        data=data if data else None,
        browser_session_id=ctx.browser_session_id,
    )

    workflow_run = await workflow_service.prepare_workflow(
        workflow_id=ctx.workflow_permanent_id,
        organization=organization,
        workflow_request=workflow_request,
        template=False,
        version=None,
        max_steps=None,
        request_id=None,
    )

    from skyvern.utils.files import initialize_skyvern_state_file

    await initialize_skyvern_state_file(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=ctx.organization_id,
    )

    run_task = asyncio.create_task(
        app.WORKFLOW_SERVICE.execute_workflow(
            workflow_run_id=workflow_run.workflow_run_id,
            api_key="copilot-agent",
            organization=organization,
            browser_session_id=ctx.browser_session_id,
            block_labels=block_labels,
        )
    )

    max_poll = RUN_BLOCKS_DEBUG_TIMEOUT_SECONDS
    poll_interval = 2.0
    elapsed = 0.0
    final_status = None

    while elapsed < max_poll:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        if run_task.done():
            run = await app.DATABASE.get_workflow_run(
                workflow_run_id=workflow_run.workflow_run_id,
                organization_id=ctx.organization_id,
            )
            if run and WorkflowRunStatus(run.status).is_final():
                final_status = run.status
            break

        if await ctx.stream.is_disconnected():
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(
                    workflow_run_id=workflow_run.workflow_run_id,
                )
            except Exception:
                LOG.warning(
                    "Failed to cancel workflow run on disconnect",
                    workflow_run_id=workflow_run.workflow_run_id,
                    exc_info=True,
                )
            return {"ok": False, "error": "Client disconnected during block execution."}

        run = await app.DATABASE.get_workflow_run(
            workflow_run_id=workflow_run.workflow_run_id,
            organization_id=ctx.organization_id,
        )
        if run and WorkflowRunStatus(run.status).is_final():
            final_status = run.status
            break

    if final_status is None:
        run_task.cancel()
        try:
            await asyncio.wait_for(run_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        try:
            await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled(
                workflow_run_id=workflow_run.workflow_run_id,
            )
        except Exception:
            LOG.warning(
                "Failed to cancel workflow run on timeout",
                workflow_run_id=workflow_run.workflow_run_id,
                exc_info=True,
            )
        timeout_msg = (
            f"Block execution timed out after {max_poll}s. "
            f"Run ID: {workflow_run.workflow_run_id}. "
            f"The task was likely stuck repeating failing actions. "
            f"Consider: simplifying the navigation_goal, using a more specific URL, "
            f"adding a dismiss-popup step, or concluding the site is not automatable."
        )
        if ctx.browser_session_id:
            try:
                browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    session_id=ctx.browser_session_id,
                    organization_id=ctx.organization_id,
                )
                if browser_state:
                    page = await browser_state.get_or_create_page()
                    timeout_msg += f" Browser was on: {page.url}"
            except Exception:
                pass
        return {"ok": False, "error": timeout_msg}

    if run and run.browser_session_id:
        ctx.browser_session_id = run.browser_session_id

    blocks = await app.DATABASE.get_workflow_run_blocks(
        workflow_run_id=workflow_run.workflow_run_id,
        organization_id=ctx.organization_id,
    )

    results = []
    for block in blocks:
        block_result: dict[str, Any] = {
            "label": block.label,
            "block_type": block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type),
            "status": block.status,
        }
        if block.failure_reason:
            block_result["failure_reason"] = block.failure_reason
        if hasattr(block, "output") and block.output:
            block_result["extracted_data"] = block.output
        results.append(block_result)

    await _attach_action_traces(blocks, results, ctx.organization_id)

    artifacts = await app.DATABASE.get_artifacts_for_run(
        run_id=workflow_run.workflow_run_id,
        organization_id=ctx.organization_id,
        artifact_types=[ArtifactType.VISIBLE_ELEMENTS_TREE],
    )
    html = None
    if isinstance(artifacts, list) and artifacts:
        artifact_bytes = await app.ARTIFACT_MANAGER.retrieve_artifact(artifacts[0])
        if artifact_bytes:
            html = artifact_bytes.decode("utf-8")

    screenshot_b64 = None
    if ctx.browser_session_id:
        try:
            browser_state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            if browser_state:
                page = await browser_state.get_or_create_page()
                screenshot_bytes = await page.screenshot(type="png")
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
        except Exception:
            LOG.debug("Failed to capture post-run screenshot", exc_info=True)

    run_ok = WorkflowRunStatus(final_status) == WorkflowRunStatus.completed
    return {
        "ok": run_ok,
        "data": {
            "workflow_run_id": workflow_run.workflow_run_id,
            "browser_session_id": ctx.browser_session_id,
            "overall_status": final_status,
            "blocks": results,
            "visible_elements_html": html,
            "screenshot_base64": screenshot_b64,
        },
    }


async def _get_run_results(params: dict[str, Any], ctx: AgentContext) -> dict[str, Any]:
    workflow_run_id = params.get("workflow_run_id")

    if not workflow_run_id:
        runs = await app.WORKFLOW_SERVICE.get_workflow_runs_for_workflow_permanent_id(
            workflow_permanent_id=ctx.workflow_permanent_id,
            organization_id=ctx.organization_id,
            page=1,
            page_size=1,
            status=[WorkflowRunStatus.completed],
        )
        if not runs:
            return {"ok": False, "error": "No runs found for this workflow."}
        workflow_run_id = runs[0].workflow_run_id

    run = await app.DATABASE.get_workflow_run(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
    )
    if not run:
        return {"ok": False, "error": f"Workflow run not found: {workflow_run_id}"}

    blocks = await app.DATABASE.get_workflow_run_blocks(
        workflow_run_id=workflow_run_id,
        organization_id=ctx.organization_id,
    )

    results = []
    for block in blocks:
        block_result: dict[str, Any] = {
            "label": block.label,
            "block_type": block.block_type.name if hasattr(block.block_type, "name") else str(block.block_type),
            "status": block.status,
        }
        if block.failure_reason:
            block_result["failure_reason"] = block.failure_reason
        output = truncate_output(getattr(block, "output", None))
        if output:
            block_result["output"] = output
        results.append(block_result)

    await _attach_action_traces(blocks, results, ctx.organization_id)

    return {
        "ok": True,
        "data": {
            "workflow_run_id": workflow_run_id,
            "overall_status": run.status,
            "blocks": results,
        },
    }


async def _fallback_page_info(ctx: AgentContext) -> tuple[str, str]:
    if not ctx.browser_session_id:
        return "", ""
    try:
        from skyvern.cli.core.session_manager import get_page

        page, _ = await get_page(session_id=ctx.browser_session_id)
        if page:
            return page.url, await page.title()
    except Exception:
        pass
    return "", ""


async def _evaluate_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    expr = params.get("expression", "").lower()
    if ".click()" in expr or ".click(" in expr:
        return {
            "ok": False,
            "error": "Do not use evaluate to click elements. Use the 'click' tool with a CSS selector instead.",
        }
    return None


_JQUERY_SELECTOR_RE = re.compile(
    r":(?:contains|eq|first|last|gt|lt|nth|has|visible|hidden|checked)\s*\(", re.IGNORECASE
)


async def _click_pre_hook(
    params: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any] | None:
    selector = params.get("selector", "")
    if not selector:
        return None
    if _JQUERY_SELECTOR_RE.search(selector):
        return {
            "ok": False,
            "error": (
                f"Invalid selector: {selector!r}. "
                "jQuery pseudo-selectors like :contains(), :eq(), :first, :visible are NOT valid CSS. "
                "Use standard CSS selectors instead. Examples: "
                "nth-of-type() instead of :eq(), "
                "[data-attr] or tag.class for filtering, "
                "or use the 'evaluate' tool with JS: "
                "document.querySelectorAll('button').forEach(b => {{ if (b.textContent.includes('Download')) b.click() }})"
            ),
        }
    return None


async def _navigate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok"):
        data = result.pop("data", {})
        result["url"] = data.get("url", "")
        result["next_step"] = (
            "Page loaded. You MUST now use evaluate, "
            "get_browser_screenshot, or click to inspect page content "
            "before responding."
        )
    return result


async def _screenshot_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url = raw.get("browser_context", {}).get("url", "")
        title = raw.get("browser_context", {}).get("title", "")
        if not url:
            url, fallback_title = await _fallback_page_info(ctx)
            if fallback_title:
                title = fallback_title
        result["data"] = {
            "screenshot_base64": data.get("data", ""),
            "url": url,
            "title": title,
        }
    return result


async def _click_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        browser_ctx = raw.get("browser_context", {})
        url = browser_ctx.get("url", "")
        title = browser_ctx.get("title", "")
        if not url:
            url, fallback_title = await _fallback_page_info(ctx)
            if fallback_title:
                title = fallback_title
        result["data"] = {
            "selector": data.get("selector", ""),
            "url": url,
            "title": title,
        }
    return result


async def _type_text_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        data = result["data"]
        url = raw.get("browser_context", {}).get("url", "")
        if not url:
            url, _ = await _fallback_page_info(ctx)
        result["data"] = {
            "selector": data.get("selector", ""),
            "typed_length": data.get("text_length", 0),
            "url": url,
        }
    return result


async def _evaluate_post_hook(
    result: dict[str, Any],
    raw: dict[str, Any],
    ctx: AgentContext,
) -> dict[str, Any]:
    if result.get("ok") and result.get("data"):
        result["data"].pop("sdk_equivalent", None)
        if "url" not in result["data"]:
            url, _ = await _fallback_page_info(ctx)
            if url:
                result["data"]["url"] = url
    return result


def get_skyvern_mcp_alias_map() -> dict[str, str]:
    return {
        "get_block_schema": "skyvern_block_schema",
        "validate_block": "skyvern_block_validate",
        "navigate_browser": "skyvern_navigate",
        "get_browser_screenshot": "skyvern_screenshot",
        "evaluate": "skyvern_evaluate",
        "click": "skyvern_click",
        "type_text": "skyvern_type",
    }


def _build_skyvern_mcp_overlays() -> dict[str, SchemaOverlay]:
    return {
        "get_block_schema": SchemaOverlay(),
        "validate_block": SchemaOverlay(),
        "navigate_browser": SchemaOverlay(
            description=(
                "Navigate the debug browser to a URL. "
                "Use this to reset browser state or navigate to a starting page before running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            post_hook=_navigate_post_hook,
        ),
        "get_browser_screenshot": SchemaOverlay(
            description=(
                "Take a screenshot of the current debug browser session. "
                "Returns a base64-encoded PNG image. "
                "Use this to see what the browser looks like after running blocks."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "selector"}),
            forced_args={"inline": True},
            requires_browser=True,
            post_hook=_screenshot_post_hook,
        ),
        "evaluate": SchemaOverlay(
            description=(
                "Execute JavaScript in the browser and return the result. "
                "Use this to inspect DOM state, read values, or run arbitrary JS."
            ),
            hide_params=frozenset({"session_id", "cdp_url"}),
            requires_browser=True,
            timeout=30,
            pre_hook=_evaluate_pre_hook,
            post_hook=_evaluate_post_hook,
        ),
        "click": SchemaOverlay(
            description=(
                "Click an element in the browser by standard CSS selector. "
                "IMPORTANT: Only valid CSS selectors work. jQuery pseudo-selectors "
                "like :contains(), :eq(), :first, :visible are NOT supported. "
                "Use tag names, classes, IDs, and attribute selectors: "
                "e.g. 'button.download', 'a[href*=\"pdf\"]', '#submit-btn', "
                "'table tr:nth-of-type(2) td a'. "
                "If you need to match by text content, use 'evaluate' with JS instead."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "intent", "button", "click_count"}),
            required_overrides=["selector"],
            requires_browser=True,
            timeout=15,
            pre_hook=_click_pre_hook,
            post_hook=_click_post_hook,
        ),
        "type_text": SchemaOverlay(
            description=(
                "Type text into an input element by CSS selector. "
                "Optionally clear the field first. Use this for form filling."
            ),
            hide_params=frozenset({"session_id", "cdp_url", "intent", "delay"}),
            required_overrides=["selector", "text"],
            arg_transforms={"clear_first": "clear"},
            requires_browser=True,
            timeout=15,
            post_hook=_type_text_post_hook,
        ),
    }


def _record_workflow_update_result(copilot_ctx: Any, result: dict[str, Any]) -> None:
    if not (result.get("ok") and "_workflow" in result):
        return

    wf = result["_workflow"]
    copilot_ctx.last_workflow = wf
    copilot_ctx.last_workflow_yaml = copilot_ctx.workflow_yaml or None
    data = result.get("data")
    if isinstance(data, dict):
        block_count = data.get("block_count")
        if isinstance(block_count, int):
            copilot_ctx.last_update_block_count = block_count
    copilot_ctx.last_test_ok = None
    copilot_ctx.last_test_failure_reason = None
    copilot_ctx.workflow_persisted = True


def _record_run_blocks_result(copilot_ctx: Any, result: dict[str, Any]) -> None:
    run_ok = bool(result.get("ok", False))
    copilot_ctx.last_test_ok = run_ok
    copilot_ctx.last_test_failure_reason = None
    if run_ok:
        copilot_ctx.failed_test_nudge_count = 0
        copilot_ctx.last_failed_workflow_yaml = None
        return

    copilot_ctx.last_failed_workflow_yaml = getattr(copilot_ctx, "workflow_yaml", None)

    data = result.get("data")
    if isinstance(data, dict):
        blocks = data.get("blocks")
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("failure_reason"):
                    copilot_ctx.last_test_failure_reason = str(block["failure_reason"])
                    return
    if result.get("error"):
        copilot_ctx.last_test_failure_reason = str(result["error"])


@function_tool(name_override="update_workflow")
async def update_workflow_tool(
    ctx: RunContextWrapper,
    workflow_yaml: str,
) -> str:
    """Validate and update the workflow YAML definition.
    Provide the complete workflow YAML as a string.
    Returns the validated workflow or validation errors.
    """
    copilot_ctx = ctx.context
    loop_error = _tool_loop_error(copilot_ctx, "update_workflow")
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    with copilot_span("update_workflow", data={"yaml_length": len(workflow_yaml)}):
        result = await _update_workflow({"workflow_yaml": workflow_yaml}, copilot_ctx)
        _record_workflow_update_result(copilot_ctx, result)
    sanitized = sanitize_tool_result_for_llm("update_workflow", result)
    return json.dumps(sanitized)


@function_tool(name_override="list_credentials")
async def list_credentials_tool(
    ctx: RunContextWrapper,
    page: int = 1,
    page_size: int = 10,
) -> str:
    """List stored credentials (metadata only — never passwords or secrets).
    Use this to find credential IDs for login blocks.
    """
    copilot_ctx = ctx.context
    loop_error = _tool_loop_error(copilot_ctx, "list_credentials")
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    result = await _list_credentials({"page": page, "page_size": page_size}, copilot_ctx)
    sanitized = sanitize_tool_result_for_llm("list_credentials", result)
    return json.dumps(sanitized)


@function_tool(
    name_override="run_blocks_and_collect_debug",
    timeout=RUN_BLOCKS_DEBUG_TIMEOUT_SECONDS,
    strict_mode=False,
)
async def run_blocks_tool(
    ctx: RunContextWrapper,
    block_labels: list[str],
    parameters: dict[str, Any] | None = None,
) -> Any:
    """Run one or more blocks of the current workflow, wait for completion,
    and return compact debug output (status, failure reason, visible elements).
    The workflow must be saved before running blocks.
    Block labels must match labels in the saved workflow.
    """
    copilot_ctx = ctx.context
    loop_error = _tool_loop_error(copilot_ctx, "run_blocks_and_collect_debug")
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    with copilot_span(
        "run_blocks",
        data={"block_labels": block_labels, "block_count": len(block_labels)},
    ):
        result = await _run_blocks_and_collect_debug(
            {"block_labels": block_labels, "parameters": parameters or {}},
            copilot_ctx,
        )
        _record_run_blocks_result(copilot_ctx, result)
        enqueue_screenshot_from_result(copilot_ctx, result)

    sanitized = sanitize_tool_result_for_llm("run_blocks_and_collect_debug", result)
    return json.dumps(sanitized)


@function_tool(name_override="get_run_results")
async def get_run_results_tool(
    ctx: RunContextWrapper,
    workflow_run_id: str | None = None,
) -> str:
    """Fetch results from a previous workflow run.
    Returns block statuses, failure reasons, and output data.
    If workflow_run_id is omitted, fetches the most recent completed run.
    """
    copilot_ctx = ctx.context
    loop_error = _tool_loop_error(copilot_ctx, "get_run_results")
    if loop_error:
        return json.dumps({"ok": False, "error": loop_error})

    params: dict[str, Any] = {}
    if workflow_run_id:
        params["workflow_run_id"] = workflow_run_id
    result = await _get_run_results(params, copilot_ctx)
    sanitized = sanitize_tool_result_for_llm("get_run_results", result)
    return json.dumps(sanitized)


NATIVE_TOOLS = [update_workflow_tool, list_credentials_tool, run_blocks_tool, get_run_results_tool]
