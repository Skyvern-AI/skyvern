from __future__ import annotations

import asyncio
import functools
import json
import weakref

import structlog
from playwright.async_api import BrowserContext, Dialog, Page

from skyvern.constants import DIALOG_LLM_TIMEOUT
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_secondary_llm_api_handler
from skyvern.forge.sdk.browser_action_preflight import preflight_dialog_response
from skyvern.forge.sdk.core import skyvern_context

LOG = structlog.get_logger()

# Track contexts that already have a dialog handler to avoid duplicate registration
# when the same BrowserContext is returned by CDP reconnect paths.
_registered_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
# Per-page guard: the bare _handle_dialog used to dedupe by listener identity in pyee, which a
# fresh functools.partial per registration no longer provides. A double-registered page would
# answer one dialog twice ("already handled") and pay a duplicate LLM round-trip.
_registered_pages: weakref.WeakSet[Page] = weakref.WeakSet()


async def _handle_dialog(dialog: Dialog, page: Page | None = None) -> None:
    """Handle a JavaScript dialog (alert/confirm/prompt/beforeunload) using LLM-based decision making.

    For alert and beforeunload dialogs, always accepts without calling the LLM.
    For confirm/prompt dialogs with no task context, auto-accepts (no LLM round-trip needed).
    For confirm/prompt dialogs with task context, calls the secondary LLM handler to decide.
    Falls back to accept on any error (safer than dismiss for form submissions).

    ``page`` is the page this handler was registered on — the dialog's originating page. Every
    response this handler gives except an alert's (which has no alternative) routes through
    ``_respond`` so the observe-only preflight (SKY-12875) sees all of them — accepts AND
    dismissals, because a page can branch on the choice; the result is discarded and never
    changes the response. The one other dialog listener in this tree, the CLI inspection handler
    in skyvern/cli/mcp_tools/inspection.py, dismisses — a capability — and is out of scope only
    because CLI sessions carry no enrolled SkyvernContext, so the preflight no-ops there; that
    claim is probed, and the listener set is pinned by test.
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

    # Record alert only — beforeunload is informational ("Changes you made may not
    # be saved") and would misfit the "field rejection" prompt copy; confirm/prompt
    # are handled deliberately by the LLM and would be mislabeled as rejections.
    if ctx is not None and dialog_type == "alert":
        try:
            ctx.record_dialog_message(dialog_type, dialog_message)
        except Exception:
            log.exception("Failed to record dialog message into context")

    # THE ONE CHOKE POINT for answering a dialog: the observe-only preflight sees every response
    # this handler ever gives, and its result is discarded — the answer must be identical whether
    # the policy observes or not. Only alert stays outside: with a single possible response there
    # is no choice, and THE CHOICE IS THE CAPABILITY (a page can branch on accept vs dismiss —
    # a real-Chromium probe fired an exfil POST specifically on dismiss).
    async def _respond(response: str, prompt_text: str | None = None) -> None:
        preflight_dialog_response(page, dialog_type=dialog_type, response=response, site="dialog_handler")
        if response == "dismiss":
            await dialog.dismiss()
        elif prompt_text is None:
            await dialog.accept()
        else:
            await dialog.accept(prompt_text)

    # Alert (no choice to preflight) auto-accepts directly; beforeunload accept commits a pending
    # navigation, so its acceptance goes through the choke point.
    if dialog_type == "alert":
        log.info("Dialog auto-accepted", dialog_type=dialog_type)
        await dialog.accept()
        return
    if dialog_type == "beforeunload":
        log.info("Dialog auto-accepted", dialog_type=dialog_type)
        await _respond("accept")
        return

    if not navigation_goal and not navigation_payload:
        log.info("Dialog auto-accepted (no task context)", dialog_type=dialog_type)
        await _respond("accept", default_value or "")
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
            get_org_aware_secondary_llm_api_handler(default=app.SECONDARY_LLM_API_HANDLER)(
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
            await _respond("dismiss")
        else:
            await _respond("accept", prompt_text if prompt_text is not None else (default_value or ""))

    except asyncio.TimeoutError:
        log.warning("Dialog LLM call timed out, falling back to accept")
        await _respond("accept", default_value or "")

    except Exception:
        log.exception("Dialog handler error, falling back to accept")
        await _respond("accept", default_value or "")


def set_dialog_handler(browser_context: BrowserContext) -> None:
    """Register a dialog handler on all pages in the browser context.

    Hooks into browser_context.on("page", ...) to register the handler
    on every new page, including popups and new tabs. Also registers on
    any pages that already exist in the context.

    Uses a WeakSet to skip registration if the same BrowserContext is
    returned again (e.g., CDP reconnect reusing contexts[0]).

    Playwright-Python schedules async callbacks as tasks internally, so a
    coroutine-returning listener is GC-safe. The partial binds the page the
    listener was registered on — the dialog's originating page — which is what
    the acceptance preflight (SKY-12875) evaluates; the page's listener list
    holds the partial, and that cycle is ordinary collectable garbage.
    """
    if browser_context in _registered_contexts:
        return
    _registered_contexts.add(browser_context)

    def _on_page(page: Page) -> None:
        if page in _registered_pages:
            return
        _registered_pages.add(page)
        page.on("dialog", functools.partial(_handle_dialog, page=page))

    # Register on pages that already exist
    for page in browser_context.pages:
        _on_page(page)

    # Register on future pages
    browser_context.on("page", _on_page)
