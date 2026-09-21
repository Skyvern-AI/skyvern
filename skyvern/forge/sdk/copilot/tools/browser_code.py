"""Persistent Python browser code for the copilot, run in the deployment's isolated interpreter."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from agents import FunctionTool
from agents.run_context import RunContextWrapper
from playwright.async_api import Page

from skyvern.cli.core.session_manager import get_page
from skyvern.forge import app
from skyvern.forge.sdk.copilot.browser_code_contract import (
    DEMONSTRATED_OPERATIONS,
    BrowserCodeCellResult,
    BrowserCodeHost,
    BrowserCodeOperation,
    BrowserCodeSession,
    BrowserCodeSessionUnavailableError,
    ExecutedBrowserCodeSource,
    ExecutedBrowserCodeSourceResolution,
)
from skyvern.forge.sdk.copilot.browser_target import (
    BROWSER_TARGET_PARAM,
    BROWSER_TARGET_PARAM_NAME,
    BrowserSessionBinding,
    last_run_facts,
    resolve_browser_session_binding,
)
from skyvern.forge.sdk.copilot.enforcement import HARD_BACKSTOP_ALLOWANCE_SECONDS, TOTAL_TIMEOUT_SECONDS
from skyvern.forge.sdk.copilot.loop_detection import record_tool_step_result_for_ctx
from skyvern.forge.sdk.copilot.mcp_adapter import (
    _browser_call_outcome_from_mapping,
    _browser_session_error_disposition,
    _browser_session_loss_result,
    _prepare_browser_session_for_dispatch,
    _record_browser_call_outcome,
    scrub_model_facing_tool_result,
)
from skyvern.forge.sdk.copilot.runtime import (
    SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR,
    SENSITIVE_ORIGIN_PAGE_ERROR,
    AgentContext,
    CopilotBrowserGenerationRetired,
    CopilotBrowserSessionUnavailable,
    bound_call_browser_session,
    browser_evidence_commit_lock,
    browser_page_custody_lock,
    browser_session_recovery,
    clear_sensitive_origin_page_taint,
    effective_browser_session_id,
    mcp_browser_context,
    navigation_replaced_document,
    sensitive_origin_page_facts_withheld,
    sensitive_origin_page_has_active_run,
    sensitive_origin_page_is_tainted,
)
from skyvern.webeye.browser_errors import BrowserAutomationError

from .guardrails import _authority_tool_error
from .scouting import _mark_pending_browser_interaction_observation, _record_scouted_interaction

LOG = structlog.get_logger()

TOOL_NAME = "run_browser_code"

# The live URL rides in every cell result, so an unreasonable one is omitted rather than returned.
CURRENT_URL_MAX_CHARS = 4096
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 300.0
MAX_CODE_CHARS = 20_000
MAX_VALUE_CHARS = 32_000
SESSION_LIFETIME_SECONDS = float(TOTAL_TIMEOUT_SECONDS + HARD_BACKSTOP_ALLOWANCE_SECONDS)

TOOL_DESCRIPTION = """Run Python against the live browser in a persistent interpreter.

`target` names the browser: 'debug' (default) is the one this chat drives; 'last_run' is the one the
most recent test run executed in, when that run minted its own. A continuation on the page a run
stopped on needs 'last_run'; acting there changes that page and any submit it triggers is real.

Top-level `await` works. Variables, functions, and classes defined in one call stay available to later
run_browser_code calls in this chat turn. They are gone when the turn ends or when a result reports
`session_restarted` or `session_ended`. Set `fresh_namespace` to true to run a complete candidate without
globals or functions from earlier calls while keeping the current browser. The value of a final bare
expression is returned as `value`; `print` output is returned as `stdout`.

Browser API (async, Playwright-shaped):
- `page` is the current tab. Locators: `page.locator(css)`, `page.get_by_role(role, name=...)`,
  `get_by_text`, `get_by_label`, `get_by_placeholder`, `.first`, `.last`, `.nth(i)`, `.filter(...)`.
- Actions: `.click()`, `.fill(text)`, `.press(key)`, `.check()`, `.select_option(...)`, `.hover()`,
  `page.keyboard.press(key)`.
- Waits: `page.wait_for_selector(...)`, `page.wait_for_url(...)`, `page.wait_for_load_state(...)`,
  `locator.wait_for(...)`.
- DOM reads: `.text_content()`, `.inner_text()`, `.get_attribute(name)`, `.input_value()`, `.count()`,
  `.is_visible()`, `page.content()`, `page.evaluate(js)`, `page.title()`, and `page.url`.
- Navigation: `page.goto(url)`, `page.go_back()`, `page.reload()`. Internal, private-network, and
  non-web destinations are refused.
- Frames: `page.frames`, `page.main_frame`, `page.frame_locator(css)`.
- After a sensitive sign-in on this page, screenshots and `page.evaluate` are refused for the rest of
  the turn. Reading text still works; that is the way to inspect such a page.
- Tabs and popups: each call starts on the browser's current tab, the one the direct browser tools act on.
  `await tabs()` lists open tabs; `await switch_tab(index)` makes that tab `page` for the rest of the call;
  `await click_and_wait_for_popup(selector)` clicks and returns the new tab's `index` and `url`.
- Downloads and files: `await click_and_download(selector)` clicks and returns `{file_id, name, size}`
  for the downloaded file; `await files.read(file_id)` returns its bytes; `await files.write(name, data)`
  stores text or bytes as a new file (at most 1 MiB of text or 768 KiB of bytes per call);
  `await files.upload(selector, [file_id, ...])` sets files on a file input (at most 50 MiB in total).
  Files belong to this chat turn.
- Saved credentials are not filled from here. Call `fill_credential_field` between calls to this tool,
  with the same `target`; every call starts on the tab that tool acts on.
- workbench-only, not valid in a saved block: `tabs`, `switch_tab`, `click_and_wait_for_popup`,
  `click_and_download`, and `files`.

Not available: imports, names starting with `_`, event listeners (`page.on`, `expect_*`), cookies,
request interception, and new browser contexts. Using one returns an error that says so.

Every result lists the browser operations the call sent, in order. A failed call returns the error,
the failing line, and the operations that already ran; nothing is retried. A call that exceeds
`timeout_seconds` (default 60, at most 300) or is cancelled stops the interpreter; when an operation
reached the browser without a reply, the result names it and the page must be read again before
acting on its state. Every executed cell also returns an opaque `executed_source_reference` for the
exact submitted bytes and observed outcome. Save a cell you ran as a block by passing its reference
instead of retyping the code: `update_and_run_blocks` takes `executed_source_references` for a new
block, and `edit_block_and_run` takes `executed_source_reference` for an existing one. A reference
does not mean the code is correct, and the saved workflow run remains the final test. A top-level `return` ends the cell with
that value, exactly as it ends a saved CodeBlock, so a complete candidate can be written the way the
block should read and promoted unchanged.

Limits: code up to 20,000 characters; `value` up to 32,000 characters of JSON and `stdout` up to 16 KB,
cut beyond that, so return or print a summary; the first 50 operations are listed; a chat turn holds at
most 32 files of 16 MB each."""

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "Python source to run in the persistent interpreter."},
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": MAX_TIMEOUT_SECONDS,
            "description": "Wall-clock limit for this call, in seconds.",
        },
        "fresh_namespace": {
            "type": "boolean",
            "description": "Start a new Python namespace for this complete candidate; the current browser remains.",
        },
        BROWSER_TARGET_PARAM_NAME: BROWSER_TARGET_PARAM,
    },
    "required": ["code"],
    "additionalProperties": False,
}

_EXECUTED_SOURCE_REFERENCE_PREFIX = "browser_code_source"


def _source_owner_fingerprint(copilot_ctx: AgentContext) -> str:
    owner = f"{copilot_ctx.organization_id}\0{copilot_ctx.workflow_permanent_id}"
    return hashlib.sha256(owner.encode()).hexdigest()


def retain_executed_browser_code_source(
    copilot_ctx: AgentContext,
    source: str,
    cell: BrowserCodeCellResult,
    *,
    browser_session_id: str | None,
    browser_session_generation: int,
    last_run_workflow_run_id: str | None = None,
) -> str:
    host = copilot_ctx.browser_code_host
    owner_fingerprint = _source_owner_fingerprint(copilot_ctx)
    reference = ":".join(
        (
            _EXECUTED_SOURCE_REFERENCE_PREFIX,
            owner_fingerprint,
            host.source_turn_token,
            str(browser_session_generation),
            uuid.uuid4().hex,
        )
    )
    host.executed_sources[reference] = ExecutedBrowserCodeSource(
        source=source,
        execution_ok=cell.ok,
        execution_error_code=cell.error_code,
        owner_fingerprint=owner_fingerprint,
        turn_token=host.source_turn_token,
        browser_session_id=browser_session_id,
        browser_session_generation=browser_session_generation,
        last_run_workflow_run_id=last_run_workflow_run_id,
    )
    return reference


def resolve_executed_browser_code_source(
    copilot_ctx: AgentContext, reference: str
) -> ExecutedBrowserCodeSourceResolution:
    parts = reference.split(":")
    if len(parts) != 5 or parts[0] != _EXECUTED_SOURCE_REFERENCE_PREFIX:
        return ExecutedBrowserCodeSourceResolution(status="missing")
    _, owner_fingerprint, turn_token, generation, _nonce = parts
    host = copilot_ctx.browser_code_host
    if owner_fingerprint != _source_owner_fingerprint(copilot_ctx):
        return ExecutedBrowserCodeSourceResolution(status="wrong_owner")
    if turn_token != host.source_turn_token:
        return ExecutedBrowserCodeSourceResolution(status="wrong_turn")
    if reference in host.expired_source_references:
        return ExecutedBrowserCodeSourceResolution(status="expired")
    retained = host.executed_sources.get(reference)
    if retained is None:
        return ExecutedBrowserCodeSourceResolution(status="missing")
    if retained.owner_fingerprint != owner_fingerprint:
        return ExecutedBrowserCodeSourceResolution(status="wrong_owner")
    if retained.turn_token != turn_token:
        return ExecutedBrowserCodeSourceResolution(status="wrong_turn")
    if retained.last_run_workflow_run_id is not None:
        # A last-run cell is pinned to that run's browser, not the chat's, so it is checked against the
        # run it was pinned to; the chat's continuity generation does not apply to it.
        if copilot_ctx.last_run_blocks_workflow_run_id != retained.last_run_workflow_run_id:
            return ExecutedBrowserCodeSourceResolution(status="wrong_session")
        expected_generation = 0
        expected_session_id = copilot_ctx.last_run_blocks_browser_session_id
    else:
        expected_generation = copilot_ctx.browser_session_continuity_generation
        expected_session_id = effective_browser_session_id(copilot_ctx)
    if generation != str(expected_generation) or retained.browser_session_generation != expected_generation:
        return ExecutedBrowserCodeSourceResolution(status="wrong_generation")
    if retained.browser_session_id != expected_session_id:
        return ExecutedBrowserCodeSourceResolution(status="wrong_session")
    return ExecutedBrowserCodeSourceResolution(
        status="valid",
        source=retained.source,
        execution_ok=retained.execution_ok,
        execution_error_code=retained.execution_error_code,
    )


def expire_executed_browser_code_sources(host: BrowserCodeHost) -> None:
    host.expired_source_references.update(host.executed_sources)
    host.executed_sources.clear()


SENSITIVE_ORIGIN_RECOVERY_HINT = (
    ' On this surface that navigation is a cell containing only `await page.goto("<url>")`; it returns the'
    " resulting URL and nothing else."
)


def _navigation_only_cell_url(code: str) -> str | None:
    """The URL when the cell is exactly one ``await page.goto("<literal>")``, else None.

    This is the one cell shape a withheld page accepts: it can carry nothing off the page, and a
    successful fresh navigation is what lifts the withholding, as it does for the direct tool.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr) or not isinstance(tree.body[0].value, ast.Await):
        return None
    call = tree.body[0].value.value
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "goto"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "page"
        and not call.keywords
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ):
        return None
    return call.args[0].value


def _recovery_navigated(cell: BrowserCodeCellResult) -> str | None:
    """The URL a tainted page was replaced by, from the worker's own record, else None.

    The cell's text proves nothing: a persisted rebinding of `page.goto` runs no operation at all.
    Only the broker's record of exactly one successful `goto` whose document differs from the one it
    started on shows that the sensitive document is gone.
    """
    if not cell.ok or cell.operations_omitted or len(cell.operations) != 1:
        return None
    op = cell.operations[0]
    if op.operation != "goto" or op.succeeded is not True or not op.source_url or not op.result_url:
        return None
    if not navigation_replaced_document(op.source_url, op.result_url):
        return None
    return op.result_url


def _timeout_seconds(value: object) -> float | None:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value) or not 0 < value <= MAX_TIMEOUT_SECONDS:
        return None
    return float(value)


def _operation_fact(operation: BrowserCodeOperation) -> dict[str, Any]:
    status = "ok" if operation.succeeded else "failed" if operation.succeeded is False else "no_reply"
    fact: dict[str, Any] = {"operation": operation.operation, "status": status}
    if operation.selector:
        fact["selector"] = operation.selector
    if operation.error:
        fact["error"] = operation.error
    return fact


def _cell_payload(cell: BrowserCodeCellResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": cell.ok}
    # A page can push a URL of any length, and this one is copied into every result.
    if cell.current_url and len(cell.current_url) <= CURRENT_URL_MAX_CHARS:
        payload["current_url"] = cell.current_url
    if cell.ok and cell.value is not None:
        rendered = json.dumps(cell.value, default=str)
        if len(rendered) > MAX_VALUE_CHARS:
            LOG.info("copilot_browser_code_value_truncated", value_chars=len(rendered), limit_chars=MAX_VALUE_CHARS)
            payload["value"] = rendered[:MAX_VALUE_CHARS]
            payload["value_truncated"] = (
                f"value was cut at {MAX_VALUE_CHARS} characters; return a smaller value or print a summary"
            )
        else:
            payload["value"] = cell.value
    if cell.stdout:
        payload["stdout"] = cell.stdout
    if cell.stdout_truncated:
        payload["stdout_truncated"] = True
    if not cell.ok:
        payload["error"] = cell.error
        payload["error_code"] = cell.error_code
        if cell.failing_line is not None:
            payload["failing_line"] = cell.failing_line
    payload["operations"] = [_operation_fact(operation) for operation in cell.operations]
    if cell.operations_omitted:
        payload["operations_omitted"] = cell.operations_omitted
    if not cell.session_alive:
        payload["session_ended"] = "the interpreter stopped; values and functions from earlier calls are gone"
    return payload


def _take_interruption_note(host: BrowserCodeHost) -> dict[str, Any]:
    if not host.interrupted:
        return {}
    operation = host.interrupted_operation
    host.interrupted = False
    host.interrupted_operation = None
    note: dict[str, Any] = {"state": "the previous run_browser_code call was cancelled and its interpreter stopped"}
    if operation is not None:
        note["last_operation"] = _operation_fact(operation)
        if operation.succeeded is None:
            note["page_state"] = "unknown: that operation reached the browser without a reply"
    return {"previous_call_interrupted": note}


async def _discard_session(host: BrowserCodeHost) -> None:
    session = host.session
    if session is None:
        return
    LOG.info("Copilot browser code session closing", browser_code_session_id=session.session_id)
    try:
        await session.close()
    except asyncio.CancelledError:
        # Keep the session reachable so turn-final cleanup can retry an interrupted close.
        raise
    except Exception:
        LOG.warning(
            "Failed to close the copilot browser code session",
            browser_code_session_id=session.session_id,
            exc_info=True,
        )
    if host.session is session:
        host.session = None


async def _session_for_page(
    copilot_ctx: AgentContext,
    page: Page,
    *,
    browser_session_id: str | None,
    generation: int,
    notes: dict[str, Any],
) -> BrowserCodeSession:
    host = copilot_ctx.browser_code_host
    session = host.session
    if session is not None and host.browser_session_id != browser_session_id:
        # A rebind moves the interpreter to a tab, not to a different browser. The worker resolves tabs
        # against the browser session it was started with, so a replaced one has to reopen.
        await _discard_session(host)
        session = None
    if session is not None and (
        session.page is not page
        or (host.browser_session_id, host.browser_session_generation) != (browser_session_id, generation)
    ):
        try:
            await session.rebind(page)
        except BrowserCodeSessionUnavailableError:
            await _discard_session(host)
            raise
        notes["handles_invalidated"] = (
            "the browser tab or session changed; page, locator, and frame objects from earlier calls no longer "
            "work, other values are kept"
        )
    elif session is None:
        session = await app.AGENT_FUNCTION.open_copilot_browser_code_session(
            page=page,
            lifetime_seconds=SESSION_LIFETIME_SECONDS,
            organization_id=copilot_ctx.organization_id,
            chat_id=copilot_ctx.workflow_permanent_id,
            turn_id=host.turn_key,
            browser_session_id=browser_session_id,
        )
        host.session = session
        LOG.info(
            "Copilot browser code session opened",
            browser_code_session_id=session.session_id,
            organization_id=copilot_ctx.organization_id,
            workflow_permanent_id=copilot_ctx.workflow_permanent_id,
        )
        if host.sessions_opened:
            notes["session_restarted"] = "this call started a new interpreter; values from earlier calls are gone"
        host.sessions_opened += 1
    host.browser_session_id = browser_session_id
    host.browser_session_generation = generation
    return session


async def _run_cell(
    host: BrowserCodeHost, session: BrowserCodeSession, code: str, timeout: float, *, deny_pixels: bool = False
) -> BrowserCodeCellResult:
    try:
        return await session.run_cell(code, timeout_seconds=timeout, deny_pixels=deny_pixels)
    except asyncio.CancelledError:
        host.interrupted = True
        host.interrupted_operation = session.last_dispatched_operation()
        await asyncio.shield(_discard_session(host))
        raise


def _was_demonstrated(operation: BrowserCodeOperation) -> bool:
    """Whether this action left something the workbench can promote.

    A handle-backed locator has no selector, so nothing is recorded for it, and the evidence that
    follows must not then be credited to an interaction there is no saved form of.
    """
    return bool(operation.succeeded and operation.selector and operation.operation in DEMONSTRATED_OPERATIONS)


def _record_outcome(
    copilot_ctx: AgentContext,
    cell: BrowserCodeCellResult,
    result: dict[str, Any],
    *,
    browser_session_id: str | None,
    generation: int,
) -> None:
    _record_browser_call_outcome(
        copilot_ctx,
        _browser_call_outcome_from_mapping(
            raw_tool_name=TOOL_NAME,
            source_browser_session_id=browser_session_id,
            source_browser_session_generation=generation,
            raw_result=result,
        ),
        call_path="model",
    )
    recorded = [operation for operation in cell.actions if _was_demonstrated(operation)]
    for operation in recorded:
        typed = operation.input_value or ""
        _record_scouted_interaction(
            copilot_ctx,
            tool_name=f"{TOOL_NAME}.{operation.operation}",
            selector=operation.selector or "",
            source_url=operation.source_url,
            result_url=operation.result_url,
            input_id=f"input_{uuid.uuid4().hex}" if typed else "",
            input_value=typed,
            typed_length=len(typed),
        )
    if recorded:
        _mark_pending_browser_interaction_observation(
            copilot_ctx, tool_name=f"{TOOL_NAME}.{recorded[-1].operation}", url=cell.current_url or ""
        )


async def run_browser_code(
    copilot_ctx: AgentContext,
    code: object,
    timeout_seconds: object = None,
    fresh_namespace: bool = False,
    target: object = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "code": code,
        "timeout_seconds": timeout_seconds,
        "fresh_namespace": fresh_namespace,
        BROWSER_TARGET_PARAM_NAME: target,
    }

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        source_reference = result.get("executed_source_reference")
        retained_reference = (
            source_reference
            if isinstance(source_reference, str) and source_reference in copilot_ctx.browser_code_host.executed_sources
            else None
        )
        result_to_scrub = (
            {key: value for key, value in result.items() if key != "executed_source_reference"}
            if retained_reference is not None
            else result
        )
        scrubbed = scrub_model_facing_tool_result(copilot_ctx, result_to_scrub)
        if retained_reference is not None:
            # This server-generated capability is not derived from credential data. Scrubbing a coincidental
            # secret substring would corrupt the lookup key and make exact-source promotion impossible.
            scrubbed["executed_source_reference"] = retained_reference
        record_tool_step_result_for_ctx(copilot_ctx, TOOL_NAME, arguments, scrubbed)
        return scrubbed

    authority_error = _authority_tool_error(copilot_ctx, TOOL_NAME)
    if authority_error:
        return finish({"ok": False, "error": authority_error})
    if not isinstance(code, str) or not code.strip():
        return finish({"ok": False, "error": "code must be non-empty Python source."})
    if len(code) > MAX_CODE_CHARS:
        return finish({"ok": False, "error": f"code is limited to {MAX_CODE_CHARS} characters."})
    if not isinstance(fresh_namespace, bool):
        return finish({"ok": False, "error": "fresh_namespace must be true or false."})
    timeout = _timeout_seconds(timeout_seconds)
    if timeout is None:
        return finish(
            {"ok": False, "error": f"timeout_seconds must be a number above 0 and at most {MAX_TIMEOUT_SECONDS:g}."}
        )
    binding = resolve_browser_session_binding(copilot_ctx, {BROWSER_TARGET_PARAM_NAME: target})
    if binding.unavailable_reason:
        return finish({"ok": False, "error": binding.unavailable_reason, **binding.provenance()})
    # The binding scopes everything below: preparation, the lease, the page lookup and the
    # interpreter all read the targeted browser rather than the chat's.
    with bound_call_browser_session(binding.session_id_override):
        return await _run_bound_cell(copilot_ctx, code, timeout, finish, binding, fresh_namespace)


async def _run_bound_cell(
    copilot_ctx: AgentContext,
    code: str,
    timeout: float,
    finish: Callable[[dict[str, Any]], dict[str, Any]],
    binding: BrowserSessionBinding,
    fresh_namespace: bool,
) -> dict[str, Any]:
    call_browser_session_id = binding.session_id_for(copilot_ctx)
    err, continuity_result, _disposition = await _prepare_browser_session_for_dispatch(
        copilot_ctx,
        tool_name=TOOL_NAME,
        call_path="model",
        observed_generation=copilot_ctx.browser_session_continuity_generation,
    )
    session_failure = err if err is not None else continuity_result
    if session_failure is not None:
        return finish({**session_failure, **last_run_facts(copilot_ctx, call_browser_session_id)})
    host = copilot_ctx.browser_code_host
    # The same three, in the same order, as the direct credential fill takes. No cell fills a credential
    # any more, but that tool types into this browser between calls, and these are what stop a cell
    # driving the page while it does — which is what makes "every call starts on the tab that tool acts
    # on" a fact rather than the usual case.
    async with (
        copilot_ctx.credential_fill_lock,
        browser_page_custody_lock(copilot_ctx),
        browser_evidence_commit_lock(copilot_ctx),
        browser_session_recovery(copilot_ctx),
    ):
        notes = _take_interruption_note(host)
        session_to_reset = host.session if fresh_namespace else None
        run_id = copilot_ctx.last_run_blocks_workflow_run_id
        recovering = False
        if sensitive_origin_page_facts_withheld(copilot_ctx, run_id):
            if sensitive_origin_page_has_active_run(copilot_ctx):
                return finish({"ok": False, "error": SENSITIVE_ORIGIN_ACTIVE_RUN_PAGE_ERROR, **notes})
            if _navigation_only_cell_url(code) is None:
                return finish(
                    {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR + SENSITIVE_ORIGIN_RECOVERY_HINT, **notes}
                )
            # The recovery runs in a fresh interpreter: a `page.goto` rebound before the page turned
            # sensitive could otherwise read the withheld page's URL, which the recorder does not log,
            # navigate once so the recovery passes, and hand the value to a later cell.
            await _discard_session(host)
            notes["session_ended"] = "the interpreter stopped; values and functions from earlier calls are gone"
            recovering = True
        browser_session_id = effective_browser_session_id(copilot_ctx)
        # The continuity generation counts re-establishments of the chat's own browser. A cell pinned to
        # another browser does not follow it, so a debug re-establishment must not rebind that cell's
        # interpreter; a real tab change there is still caught by the page identity check.
        generation = copilot_ctx.browser_session_continuity_generation if binding.session_id_override is None else 0
        entered_browser = False
        try:
            # The lease is held for the whole cell so a browser retirement cancels it the way it cancels a
            # direct tool, instead of letting the cell act on a closing browser for up to its timeout.
            async with mcp_browser_context(copilot_ctx, session_id_override=binding.session_id_override):
                entered_browser = True
                current_page, _browser_context = await get_page(session_id=browser_session_id)
                session = await _session_for_page(
                    copilot_ctx,
                    current_page.page,
                    browser_session_id=browser_session_id,
                    generation=generation,
                    notes=notes,
                )
                # Replacing the browser opens a new interpreter in _session_for_page. It already has
                # a fresh namespace, so only reset the exact session that existed when this call began.
                if session_to_reset is not None and session is session_to_reset:
                    try:
                        await session.reset_namespace()
                    except asyncio.CancelledError:
                        host.interrupted = True
                        # Reset dispatches no browser operation. Reusing the prior cell's last operation here
                        # would report that already-completed effect as part of this cancelled call.
                        host.interrupted_operation = None
                        await asyncio.shield(_discard_session(host))
                        raise
                    except BrowserCodeSessionUnavailableError:
                        await _discard_session(host)
                        raise
                if fresh_namespace:
                    notes["fresh_namespace"] = "this call started a new interpreter; earlier Python values are absent"
                # A page this turn visited a sensitive origin on stays tainted after its facts stop
                # being withheld. Scrubbing works on values, and pixels are not values, so a cell that
                # can screenshot it can carry the page out of here a chunk at a time.
                pixels_denied = sensitive_origin_page_is_tainted(copilot_ctx)
                cell = await _run_cell(host, session, code, timeout, deny_pixels=pixels_denied)
        except BrowserCodeSessionUnavailableError as exc:
            if exc.session_ended:
                # Dropped now, or the retry this refusal asks for is spent rediscovering that the
                # session is over, and only the call after that reopens one.
                await _discard_session(host)
            refusal: dict[str, Any] = {"ok": False, "error": str(exc), "error_code": exc.error_code, **notes}
            if exc.retry_after_seconds is not None:
                refusal["retry_after_seconds"] = exc.retry_after_seconds
            return finish(refusal)
        except (CopilotBrowserGenerationRetired, CopilotBrowserSessionUnavailable) as exc:
            disposition = await _browser_session_error_disposition(
                copilot_ctx, exc, tool_name=TOOL_NAME, call_path="model"
            )
            return finish(
                {
                    **_browser_session_loss_result(
                        dict(notes),
                        disposition=disposition,
                        deadline_expired=copilot_ctx.browser_session_continuity_deadline_expired,
                    ),
                    **last_run_facts(copilot_ctx, call_browser_session_id),
                }
            )
        except Exception as exc:
            if entered_browser:
                raise
            # Only a classified error's message has been through CDP-endpoint redaction; an
            # unclassified one is named by type, and its text stays in the log.
            detail = (str(exc).rstrip(".") if isinstance(exc, BrowserAutomationError) else "") or type(exc).__name__
            LOG.warning(
                "Browser-code cell could not enter its browser",
                browser_session_id=call_browser_session_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return finish(
                {
                    "ok": False,
                    "error": (
                        f"{TOOL_NAME} could not reach its browser: {detail}. "
                        "The cell was never sent to the browser, so it had no effect."
                    ),
                    "browser_session_id": call_browser_session_id,
                    **binding.provenance(),
                    **notes,
                    **last_run_facts(copilot_ctx, call_browser_session_id),
                }
            )
        source_reference = retain_executed_browser_code_source(
            copilot_ctx,
            code,
            cell,
            browser_session_id=browser_session_id,
            browser_session_generation=generation,
            last_run_workflow_run_id=(
                copilot_ctx.last_run_blocks_workflow_run_id if binding.session_id_override is not None else None
            ),
        )
        if cell.interpreter_restarted:
            # The host this call holds is unchanged, so a restart on the far side is invisible here
            # unless it is said. Claiming a continuity the interpreter cannot honour is the failure.
            notes["session_restarted"] = "this call started a new interpreter; values from earlier calls are gone"
        if cell.handles_invalidated:
            # A move this side asked for inside the call — after dropping a tab report it could not
            # confirm — drops the cell's handles the same as one the model asked for, and is said the same way.
            notes.setdefault(
                "handles_invalidated",
                "the browser tab or session changed; page, locator, and frame objects from earlier calls no longer "
                "work, other values are kept",
            )
        if not cell.session_alive:
            # A session that ended under the cell can return a result with no operations at all — a
            # fallback written over one that had landed. The session still knows what last reached the
            # browser; said now, before it is discarded, or the model retries an action that happened.
            last = session.last_dispatched_operation()
            if last is not None and not cell.operations:
                ended: dict[str, Any] = {"last_operation": _operation_fact(last)}
                if last.succeeded is None:
                    ended["page_state"] = "unknown: that operation reached the browser without a reply"
                notes["session_ended_during_call"] = ended
            await _discard_session(host)
        if recovering:
            replaced_by = _recovery_navigated(cell)
            # A failed recovery leaves the page on the sensitive origin, so not even its URL is returned;
            # a successful one returns only the new document's URL, never a value or stdout.
            if replaced_by is None:
                return finish(
                    {"ok": False, "error": SENSITIVE_ORIGIN_PAGE_ERROR + SENSITIVE_ORIGIN_RECOVERY_HINT, **notes}
                )
            clear_sensitive_origin_page_taint(copilot_ctx)
            return finish({"ok": True, "current_url": replaced_by, **notes})
        if sensitive_origin_page_facts_withheld(copilot_ctx, run_id):
            operations_sent = len(cell.operations) + cell.operations_omitted
            return finish(
                {
                    "ok": False,
                    "error": SENSITIVE_ORIGIN_PAGE_ERROR + SENSITIVE_ORIGIN_RECOVERY_HINT,
                    "operations_sent": operations_sent,
                    "executed_source_reference": source_reference,
                    **notes,
                }
            )
        if pixels_denied and _recovery_navigated(cell) is not None:
            # On the replace surface no other tool navigates, so without this a finished run's taint
            # denies pixels on this browser for the rest of the chat.
            clear_sensitive_origin_page_taint(copilot_ctx)
        result = finish(
            {**_cell_payload(cell), "executed_source_reference": source_reference, **notes, **binding.provenance()}
        )
        _record_outcome(copilot_ctx, cell, result, browser_session_id=browser_session_id, generation=generation)
        return result


async def close_browser_code_session(copilot_ctx: AgentContext) -> None:
    host = copilot_ctx.browser_code_host
    try:
        await _discard_session(host)
    finally:
        expire_executed_browser_code_sources(host)


async def _run_browser_code_invoke(ctx: RunContextWrapper, arguments: str) -> str:
    try:
        parsed = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    result = await run_browser_code(
        ctx.context,
        parsed.get("code"),
        parsed.get("timeout_seconds"),
        parsed.get("fresh_namespace", False),
        parsed.get(BROWSER_TARGET_PARAM_NAME),
    )
    return json.dumps(result, default=str)


run_browser_code_tool = FunctionTool(
    name=TOOL_NAME,
    description=TOOL_DESCRIPTION,
    params_json_schema=TOOL_SCHEMA,
    on_invoke_tool=_run_browser_code_invoke,
    strict_json_schema=False,
)
