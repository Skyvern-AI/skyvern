from __future__ import annotations

import asyncio
import functools
import json
import weakref
from dataclasses import dataclass
from typing import Any

import structlog
from playwright.async_api import BrowserContext, Dialog, Page

from skyvern.constants import DIALOG_LLM_TIMEOUT
from skyvern.forge import app
from skyvern.forge.prompts import prompt_engine
from skyvern.forge.sdk.api.llm.api_handler_factory import get_org_aware_secondary_llm_api_handler
from skyvern.forge.sdk.browser_action_preflight import preflight_dialog_response
from skyvern.forge.sdk.core import skyvern_context
from skyvern.webeye.browser_errors import is_target_closed_message

LOG = structlog.get_logger()

_DIALOG_GONE_MESSAGE_MARKER = "no dialog is showing"


def _is_dialog_gone_error(exc: BaseException) -> bool:
    """Whether the dialog was already gone by the time we answered it — the page navigated, closed,
    or something else dismissed it first.

    Matched on message text rather than exception class: the deployed driver is patchright, whose
    error type is not the ``playwright.async_api`` one this module imports, so a class-based
    ``except`` would silently miss every one of these.
    """
    message = str(exc)
    return _DIALOG_GONE_MESSAGE_MARKER in message.lower() or is_target_closed_message(message)


# Track contexts that already have a dialog handler to avoid duplicate registration
# when the same BrowserContext is returned by CDP reconnect paths.
_registered_contexts: weakref.WeakSet[BrowserContext] = weakref.WeakSet()
# Per-page guard: the bare _handle_dialog used to dedupe by listener identity in pyee, which a
# fresh functools.partial per registration no longer provides. A double-registered page would
# answer one dialog twice ("already handled") and pay a duplicate LLM round-trip.
_registered_pages: weakref.WeakSet[Page] = weakref.WeakSet()


@dataclass(frozen=True)
class DialogPolicy:
    action: str
    prompt_text: str | None = None


DIALOG_POLICY_ACTIONS: frozenset[str] = frozenset({"accept", "dismiss"})

# Cap on records a block accumulates between reads, keeping the most recent. Deliberately not
# skyvern_context.MAX_RECENT_DIALOG_MESSAGES: that one is tuned against the LLM prompt budget, and
# retuning it there must not resize a CodeBlock return value.
MAX_DIALOG_POLICY_RECORDS = 5

DIALOG_POLICY_HELPER_CONTRACT: dict[str, Any] = {
    "call": "await set_dialog_policy(page, 'accept'|'dismiss', prompt_text=None)",
    "when_to_use": (
        "The supported way for a code block to answer a native JS dialog: declare the answer as data "
        "rather than registering a listener. On the secure CodeBlock runner page.on('dialog', ...) and "
        "the rest of the listener family are refused outright."
    ),
    "parameters": {
        "page": {"accepted_type": "the block's own page object"},
        "action": {"accepted_type": "str", "one_of": ["accept", "dismiss"]},
        "prompt_text": {"accepted_type": "str", "note": "typed into a prompt(); only valid with 'accept'"},
    },
    "returns": (
        f"list of {{type, message}} for the dialogs OBSERVED since this block's previous call, "
        f"capped at the {MAX_DIALOG_POLICY_RECORDS} most recent (older ones are dropped)"
    ),
    "reading_the_result": (
        "The declaring call runs before any dialog fires, so records come back on the NEXT call: "
        "declare, drive the page, then call again to read what fired. An alert is recorded but "
        "answered by its own branch rather than by the policy."
    ),
    "lifetime": (
        "The policy covers the whole browser context for the rest of the block -- sibling and popup "
        "pages included -- and is revoked at block end, restoring default dialog handling. It answers "
        "confirm and prompt only: beforeunload is always accepted so that a declared 'dismiss' cannot "
        "cancel the block's own navigation, and is recorded like any other dialog. Revocation wins a "
        "tie: a dialog the page schedules as the block ends may be answered by default handling and "
        "go unrecorded, so do not rely on the policy for a dialog that races block end."
    ),
}

# Keyed on the RAW BrowserContext: a recording proxy builds a fresh context wrapper on every
# attribute access, so a weak key taken from one would be dead before the next dialog fired.
_dialog_policies: weakref.WeakKeyDictionary[BrowserContext, DialogPolicy] = weakref.WeakKeyDictionary()
_dialog_records: weakref.WeakKeyDictionary[BrowserContext, list[dict[str, str]]] = weakref.WeakKeyDictionary()


def _policy_key(browser_context: BrowserContext) -> BrowserContext | None:
    """None for an object that cannot be weakly referenced, and so can hold no policy."""
    try:
        weakref.ref(browser_context)
    except TypeError:
        return None
    return browser_context


def set_dialog_policy(browser_context: BrowserContext, action: str, prompt_text: str | None = None) -> None:
    """Declare how dialogs on this context are answered until the policy is cleared; strict about
    the key, so a context that cannot hold one raises rather than arming what teardown cannot find."""
    if action not in DIALOG_POLICY_ACTIONS:
        raise ValueError(f"unsupported dialog policy action: {action!r}")
    _dialog_policies[browser_context] = DialogPolicy(action=action, prompt_text=prompt_text)
    if browser_context not in _dialog_records:
        _dialog_records[browser_context] = []


def clear_dialog_policy(browser_context: BrowserContext) -> None:
    """Revert this context to default dialog handling and drop any undelivered records. In-flight
    listeners are deliberately not drained -- they run as their own tasks, so waiting would make
    teardown depend on a page-driven callback; revocation wins a tie and the policy never outlives
    its block."""
    key = _policy_key(browser_context)
    if key is None:
        return
    _dialog_policies.pop(key, None)
    _dialog_records.pop(key, None)


def take_dialog_records(browser_context: BrowserContext) -> list[dict[str, str]]:
    """Dialogs OBSERVED since the previous take, as plain data — never a live Dialog. An alert is
    observed but not answered by the policy: it has one possible response and takes its own branch."""
    key = _policy_key(browser_context)
    if key is None:
        return []
    records = _dialog_records.get(key)
    if not records:
        return []
    _dialog_records[key] = []
    return records


async def _handle_dialog(dialog: Dialog, page: Page | None = None) -> None:
    """Handle a JavaScript dialog (alert/confirm/prompt/beforeunload) using LLM-based decision making.

    For alert and beforeunload dialogs, always accepts without calling the LLM.
    For confirm/prompt dialogs with no task context, auto-accepts (no LLM round-trip needed).
    For confirm/prompt dialogs with task context, calls the secondary LLM handler to decide.
    Falls back to accept on any decision error (safer than dismiss for form submissions), except
    when the dialog itself is already gone — nothing is answerable then.

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

    policy: DialogPolicy | None = None
    policy_context = _policy_key(page.context) if page is not None else None
    if policy_context is not None:
        policy = _dialog_policies.get(policy_context)
        if policy is not None:
            recorded_message = dialog_message
            if len(recorded_message) > skyvern_context.MAX_DIALOG_MESSAGE_CHARS:
                recorded_message = recorded_message[: skyvern_context.MAX_DIALOG_MESSAGE_CHARS] + "…"
            records = _dialog_records.setdefault(policy_context, [])
            records.append({"type": dialog_type, "message": recorded_message})
            del records[:-MAX_DIALOG_POLICY_RECORDS]

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

    # Answering races the page, and every response path below can lose it. This runs as a pyee
    # listener, so an escaping error lands in asyncio's default exception handler at error level
    # with none of the context bound above; the race is an expected outcome, everything else is not.
    try:
        # Alert (no choice to preflight) auto-accepts directly; beforeunload accept commits a
        # pending navigation, so its acceptance goes through the choke point.
        if dialog_type == "alert":
            log.info("Dialog auto-accepted", dialog_type=dialog_type)
            await dialog.accept()
            return
        # Ahead of the policy branch: a declared `dismiss` would otherwise cancel the navigation a
        # block starts right after handling a confirm, stalling it until the block's own timeout.
        if dialog_type == "beforeunload":
            log.info("Dialog auto-accepted", dialog_type=dialog_type)
            await _respond("accept")
            return
        if policy is not None:
            log.info("Dialog answered by declared policy", policy_action=policy.action)
            await _respond(policy.action, policy.prompt_text)
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

        except Exception as exc:
            # A dialog that is already gone cannot be answered, so the fallback accept would both
            # overrule a deliberate dismissal and raise a second time. Let the guard below name it.
            if _is_dialog_gone_error(exc):
                raise
            log.exception("Dialog handler error, falling back to accept")
            await _respond("accept", default_value or "")

    except Exception as exc:
        if not _is_dialog_gone_error(exc):
            raise
        log.warning("Dialog was gone before it could be answered", error=str(exc))


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
