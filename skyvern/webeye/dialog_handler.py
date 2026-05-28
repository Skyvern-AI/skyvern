from __future__ import annotations

import asyncio
import json
import weakref

import structlog
from playwright.async_api import BrowserContext, Dialog, Page

from skyvern.constants import DIALOG_LLM_TIMEOUT
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.core import skyvern_context

LOG = structlog.get_logger()

# Track contexts that already have a dialog handler to avoid duplicate registration
# when the same BrowserContext is returned by CDP reconnect paths.
_registered_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()


async def _handle_dialog(dialog: Dialog) -> None:
    """Handle a JavaScript dialog (alert/confirm/prompt/beforeunload) using LLM-based decision making.

    For alert and beforeunload dialogs, always accepts without calling the LLM.
    For confirm/prompt dialogs with no task context, auto-accepts (no LLM round-trip needed).
    For confirm/prompt dialogs with task context, calls the secondary LLM handler to decide.
    Falls back to accept on any error (safer than dismiss for form submissions).
    """
    dialog_type = dialog.type
    dialog_message = dialog.message
    default_value = dialog.default_value

    ctx = skyvern_context.current()
    organization_id = ctx.organization_id if ctx else None
    navigation_goal = (ctx.navigation_goal or ctx.prompt) if ctx else None
    navigation_payload = ctx.navigation_payload if ctx else None
    task_id = ctx.task_id if ctx else None
    workflow_run_id = ctx.workflow_run_id if ctx else None

    log = LOG.bind(
        dialog_type=dialog_type,
        dialog_message=dialog_message,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
        organization_id=organization_id,
    )

    # Alert, beforeunload, or no task context — auto-accept without LLM call
    if dialog_type in ("alert", "beforeunload"):
        log.info("Dialog auto-accepted", dialog_type=dialog_type)
        await dialog.accept()
        return

    if not navigation_goal and not navigation_payload:
        log.info("Dialog auto-accepted (no task context)", dialog_type=dialog_type)
        await dialog.accept(default_value or "")
        return

    # For confirm/prompt with task context, call LLM to decide
    try:
        prompt = prompt_engine.load_prompt(
            "handle-dialog",
            dialog_type=dialog_type,
            dialog_message=dialog_message,
            default_value=default_value,
            navigation_goal=navigation_goal,
            navigation_payload=json.dumps(navigation_payload) if navigation_payload else None,
        )

        # JS dialogs block the page's JS thread while open. We need a hard timeout
        # to ensure the page doesn't stay frozen indefinitely if the LLM call is slow.
        response = await asyncio.wait_for(
            app.SECONDARY_LLM_API_HANDLER(
                prompt=prompt,
                prompt_name="handle-dialog",
                organization_id=organization_id,
            ),
            timeout=DIALOG_LLM_TIMEOUT,
        )

        action = str(response.get("action", "accept")).lower()
        prompt_text = response.get("prompt_text")

        if action not in ("accept", "dismiss"):
            log.warning("Dialog LLM returned unexpected action, defaulting to accept", llm_action=action)
            action = "accept"

        log.info(
            "Dialog handled via LLM",
            action=action,
            has_prompt_text=prompt_text is not None,
        )

        if action == "dismiss":
            await dialog.dismiss()
        else:
            await dialog.accept(prompt_text if prompt_text is not None else (default_value or ""))

    except asyncio.TimeoutError:
        log.warning("Dialog LLM call timed out, falling back to accept")
        await dialog.accept(default_value or "")

    except Exception:
        log.exception("Dialog handler error, falling back to accept")
        await dialog.accept(default_value or "")


def set_dialog_handler(browser_context: BrowserContext) -> None:
    """Register a dialog handler on all pages in the browser context.

    Hooks into browser_context.on("page", ...) to register the handler
    on every new page, including popups and new tabs. Also registers on
    any pages that already exist in the context.

    Uses a WeakSet to skip registration if the same BrowserContext is
    returned again (e.g., CDP reconnect reusing contexts[0]).

    Playwright-Python schedules async callbacks as tasks internally,
    so registering _handle_dialog directly is GC-safe.
    """
    if browser_context in _registered_contexts:
        return
    _registered_contexts.add(browser_context)

    def _on_page(page: Page) -> None:
        page.on("dialog", _handle_dialog)

    # Register on pages that already exist
    for page in browser_context.pages:
        _on_page(page)

    # Register on future pages
    browser_context.on("page", _on_page)
