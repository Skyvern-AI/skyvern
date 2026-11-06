"""Shared copilot runtime types and helpers."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast
from urllib.parse import urlsplit

import structlog
from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError

from skyvern.cli.core.api_key_hash import hash_api_key_for_cache
from skyvern.cli.core.client import (
    get_active_api_key,
    get_skyvern,
    reset_api_key_override,
    set_api_key_override,
)
from skyvern.cli.core.copilot_sessions import register_copilot_session, unregister_copilot_session
from skyvern.cli.core.result import BrowserContext as MCPBrowserContext
from skyvern.cli.core.session_manager import (
    RequestFailureLog,
    SessionState,
    scoped_session,
)
from skyvern.config import settings
from skyvern.exceptions import TooManyStartingBrowserSessions
from skyvern.forge import app
from skyvern.forge.sdk.copilot.agent_context import (
    AgentContext,
    AttachedBrowserDriver,
    BuildTestPrestart,
    PrestartedBrowser,
    SdkActionWorkflowRunCacheKey,
)
from skyvern.forge.sdk.copilot.build_test_connect_failure import (
    BROWSER_TOOLS_UNAVAILABLE_ERROR,
    RAW_SECRET_BROWSER_ERROR,
    SUPERSEDED_BY_NEWER_TEST_REASON,
    BuildTestConnectFailure,
    build_test_connect_failure_sentence,
)
from skyvern.forge.sdk.copilot.config import (
    TOTAL_TIMEOUT_SECONDS,
)
from skyvern.forge.sdk.copilot.human_input_wait import turn_clock_seconds
from skyvern.forge.sdk.copilot.secret_scrub import clear_session_scrub_values
from skyvern.forge.sdk.copilot.tracing_setup import copilot_span
from skyvern.forge.sdk.copilot.turn_origin import (
    HealAdoptionFailed,
    TurnOrigin,
    is_self_heal_session_id,
    make_self_heal_session_id,
)
from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.db.enums import WorkflowRunStatus
from skyvern.library.skyvern_browser import SkyvernBrowser
from skyvern.schemas.browser_session_close import BrowserSessionCloseReason
from skyvern.schemas.browser_session_kind import COPILOT_PRESTART_RUNNABLE_TYPE, BrowserSessionKind
from skyvern.schemas.proxy_location import ProxyLocation, ProxyLocationInput
from skyvern.webeye.browser_engine import is_any_engine_error
from skyvern.webeye.browser_errors import (
    BrowserCdpAcquisitionError,
    BrowserCdpConnectionError,
    BrowserTargetClosedError,
    is_target_closed_message,
)
from skyvern.webeye.browser_retirement import BrowserOperationRejected, BrowserRetirement, BrowserRetirementReason
from skyvern.webeye.browser_state import BrowserState
from skyvern.webeye.persistent_session_errors import BrowserSessionCreditAdmissionRefusal

if TYPE_CHECKING:
    from playwright.async_api import Page

    from skyvern.forge.sdk.schemas.persistent_browser_sessions import FreshExitReceipt, PersistentBrowserSession

LOG = structlog.get_logger()

_SESSION_CLEANUP_TIMEOUT_SECONDS = 5.0
# Browser contexts can lag the persistent-session row under load; this keeps
# Copilot from handing a not-yet-attachable session to the next MCP tool.
_BROWSER_BOOT_WAIT_SECONDS = 30.0
_BROWSER_BOOT_POLL_INTERVAL_SECONDS = 0.25
# A session this close to a deadline its infrastructure will not move is not worth attaching to:
# a vendor replacement was measured booting in 13.5s, so this leaves room to have one ready.
_FIXED_DEADLINE_REPLACEMENT_MARGIN_SECONDS = 60.0
_ABANDONED_BROWSER_STATE_RESOLVES: set[asyncio.Task[BrowserState | None]] = set()
_DETACHED_TASKS: set[asyncio.Task[bool] | asyncio.Task[None] | asyncio.Task[PrestartedBrowser | None]] = set()
# Per session: attached turns in this process, and the newest generation any of them attached.
# Only the last one out releases, and it retires that newest generation rather than its own.
_ATTACHED_TURNS_PER_SESSION: dict[str, tuple[int, BrowserState]] = {}
# Closed sessions whose scrub values wait for the last attached turn's release: that turn may still be
# scrubbing a readback it took before the close.
_SCRUB_VALUES_CLEARED_ON_RELEASE: set[str] = set()
# Set while a last-out release has popped its ledger entry but the evict has not finished; an attach
# in that window would record the generation the evict is about to retire.
_DRIVER_RELEASES_IN_FLIGHT: dict[str, asyncio.Event] = {}
# Bumped per session when a release starts, so a lookup can tell one began and finished while it
# was out. Never pruned: one int per session id this process has ever released.
_DRIVER_RELEASE_EPOCHS: dict[str, int] = {}
# A positive closed report that landed after its caller gave up, keyed by session to the release epoch
# it was determined under. ponytail: pod-local, so a determination finished on another pod is lost.
_ABANDONED_CLOSED_FACTS: dict[str, int] = {}
# How long a superseded run's owner gets to drop the lease. The worker releases only after its own
# cooperative cancel check, so a cleared runnable_id is the evidence its browser work stopped.
_SUPERSEDE_RELEASE_WAIT_SECONDS = 15.0
_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS = 0.5
# Time the typed occupied report needs to reach the user once the release wait gives up. The turn's
# own deadline must not fire inside that window, or outer cancellation replaces the typed report.
_SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS = 5.0


@dataclass(frozen=True)
class _ActiveMcpBrowserContext:
    task: asyncio.Task[Any]
    ctx: AgentContext
    session_id: str
    retirement: BrowserRetirement


_ACTIVE_MCP_BROWSER_CONTEXT: ContextVar[_ActiveMcpBrowserContext | None] = ContextVar(
    "active_mcp_browser_context",
    default=None,
)


class CopilotBrowserSessionUnavailable(RuntimeError):
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__("No browser context for copilot session")


class CopilotBrowserGenerationRetired(RuntimeError):
    def __init__(
        self,
        session_id: str,
        retirement_reason: BrowserRetirementReason = BrowserRetirementReason.replacement,
    ) -> None:
        self.session_id = session_id
        self.retirement_reason = retirement_reason
        detail = "ended" if retirement_reason is BrowserRetirementReason.session_ending else "was replaced"
        super().__init__(f"The browser connection used by this operation {detail}")


class CopilotBrowserLivenessUndetermined(RuntimeError):
    def __init__(self) -> None:
        # Distinct wording on purpose: _is_unrecoverable_browser_session_error reads this text, and
        # an undetermined signal must not count toward aborting the turn as session loss.
        super().__init__("Browser liveness for this copilot session could not be determined")


def _playwright_private_impl(browser_context: object) -> object | None:
    if not hasattr(browser_context, "_impl_obj"):
        return None
    return browser_context._impl_obj  # type: ignore[attr-defined]


def _object_bool_attr(value: object | None, attr_name: str) -> bool:
    return getattr(value, attr_name, False) is True


class BrowserProbeOutcome(StrEnum):
    attachable = "attachable"
    positively_unreachable = "positively_unreachable"
    could_not_determine = "could_not_determine"


def _browser_context_attachability(browser_context: object | None) -> BrowserProbeOutcome:
    if browser_context is None:
        return BrowserProbeOutcome.positively_unreachable

    # These private attrs are a best-effort early guard that also tolerates
    # Playwright-like contexts without is_closed(); fallback defaults keep future
    # Playwright changes from breaking the public browser.is_connected probe.
    impl = _playwright_private_impl(browser_context)
    if _object_bool_attr(impl, "_closing_or_closed") or _object_bool_attr(impl, "_closed"):
        return BrowserProbeOutcome.positively_unreachable

    # Test doubles and older Playwright-like wrappers may omit the public
    # browser property. Treat that as attachable after the private close check.
    browser = getattr(browser_context, "browser", None)
    if browser is not None:
        try:
            if not browser.is_connected():
                return BrowserProbeOutcome.positively_unreachable
        except Exception:
            # The connectivity signal itself failed. That is not an answer about the browser.
            return BrowserProbeOutcome.could_not_determine

    return BrowserProbeOutcome.attachable


def _browser_context_is_attachable(browser_context: object | None) -> bool:
    return _browser_context_attachability(browser_context) == BrowserProbeOutcome.attachable


def _copilot_session_can_access_localhost() -> bool:
    return settings.ENV == "local"


def mcp_to_copilot(mcp_result: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP result dict to the copilot {ok, data, error} format."""
    error = mcp_result.get("error")
    # Default ok=False when error is present so an upstream tool that returns
    # an error-shaped response without an explicit `ok` field doesn't produce
    # the contradictory {"ok": True, "error": "..."} envelope.
    result: dict[str, Any] = {"ok": mcp_result.get("ok", error is None)}

    data = mcp_result.get("data")
    if data is not None:
        result["data"] = data

    if error is not None:
        if isinstance(error, dict):
            # The server's hint is written for its own tool surface and names tools Copilot may not have.
            result["error"] = error.get("message", "Unknown error")
            error_code = error.get("code")
            if isinstance(error_code, str) and error_code:
                result["error_code"] = error_code
            # The message is flattened to a string here, so the driver's typed verdicts are lifted out
            # of details to survive as values rather than as prose.
            details = error.get("details")
            if isinstance(details, dict):
                for key in ("nav_error_code", "element_state"):
                    value = details.get(key)
                    if isinstance(value, str) and value:
                        result[key] = value
                observed_options = details.get("observed_options")
                if isinstance(observed_options, list) and all(isinstance(option, str) for option in observed_options):
                    result["observed_options"] = observed_options
        else:
            result["error"] = str(error)

    warnings = mcp_result.get("warnings")
    if warnings:
        result["warnings"] = warnings

    return result


_HEAL_ADOPTION_FAILURE_REASONS = frozenset(
    {
        "injected_browser_state_missing",
        "injected_browser_context_unusable",
        "injected_working_page_unavailable",
        "self_heal_workflow_run_id_missing",
    }
)


def _safe_heal_adoption_failure_reason(exc: HealAdoptionFailed) -> str:
    try:
        reason = object.__getattribute__(exc, "message")
    except BaseException:
        return "injected_browser_context_unusable"
    return (
        reason
        if type(reason) is str and reason in _HEAL_ADOPTION_FAILURE_REASONS
        else "injected_browser_context_unusable"
    )


def _redacted_heal_adoption_failure_reason(exc: HealAdoptionFailed, parameters: dict[str, Any]) -> str:
    reason = _safe_heal_adoption_failure_reason(exc)
    if not parameters:
        return reason
    try:
        candidate = app.AGENT_FUNCTION.redact_codeblock_parameter_values(reason, parameters)
        return candidate if isinstance(candidate, str) else ""
    except BaseException:
        return ""


async def _resolve_self_heal_browser_state(ctx: AgentContext) -> tuple[str, BrowserState, Page]:
    propagated_error: BaseException
    adoption_message: str | None = None
    try:
        return await _resolve_self_heal_browser_state_inner(ctx)
    except BaseException as exc:
        if type(exc) is HealAdoptionFailed:
            adoption_message = _redacted_heal_adoption_failure_reason(
                cast(HealAdoptionFailed, exc), ctx.codeblock_redaction_parameters
            )
        elif app.AGENT_FUNCTION.prepare_codeblock_control_flow_exception(exc):
            propagated_error = exc.with_traceback(None)
        else:
            adoption_message = "injected_browser_context_unusable"
        del ctx, exc
    if adoption_message is not None:
        raise HealAdoptionFailed(adoption_message) from None
    raise propagated_error from None


async def _resolve_self_heal_browser_state_inner(ctx: AgentContext) -> tuple[str, BrowserState, Page]:
    browser_state = ctx.injected_browser_state
    if browser_state is None:
        raise HealAdoptionFailed("injected_browser_state_missing")
    if not _browser_context_is_attachable(browser_state.browser_context):
        raise HealAdoptionFailed("injected_browser_context_unusable")
    try:
        page = await browser_state.get_working_page()
    except Exception:
        LOG.warning(
            "Self-heal browser adoption failed while probing working page",
            organization_id=ctx.organization_id,
        )
        adoption_failed = True
    else:
        adoption_failed = False
    if adoption_failed:
        raise HealAdoptionFailed("injected_working_page_unavailable")
    if page is None:
        raise HealAdoptionFailed("injected_working_page_unavailable")
    workflow_run_id = ctx.heal_workflow_run_id
    if not workflow_run_id:
        raise HealAdoptionFailed("self_heal_workflow_run_id_missing")
    session_id = make_self_heal_session_id(workflow_run_id)
    return session_id, browser_state, page


_CALL_BROWSER_SESSION_ID: ContextVar[str | None] = ContextVar("copilot_call_browser_session_id", default=None)


@dataclass(frozen=True)
class DemonstratedAction:
    selector: str
    source_url: str
    packet: dict[str, Any] | None
    synthesized_result: dict[str, Any]
    readback: str | None = None
    target_packet: dict[str, Any] | None = None


_DEMONSTRATED_ACTION: ContextVar[DemonstratedAction | None] = ContextVar("copilot_demonstrated_action", default=None)


@contextmanager
def staged_demonstrated_action(action: DemonstratedAction) -> Iterator[None]:
    token = _DEMONSTRATED_ACTION.set(action)
    try:
        yield
    finally:
        _DEMONSTRATED_ACTION.reset(token)


def current_demonstrated_action() -> DemonstratedAction | None:
    return _DEMONSTRATED_ACTION.get()


@contextmanager
def bound_call_browser_session(session_id: str | None) -> Iterator[None]:
    """Bind one tool call's browser for everything it reaches: hooks, preparation, dispatch.

    A ContextVar rather than a field on the shared context: sibling tool calls from one model turn
    run as separate tasks, so each keeps its own binding instead of reading another call's.
    """
    if session_id is None:
        yield
        return
    token = _CALL_BROWSER_SESSION_ID.set(session_id)
    try:
        yield
    finally:
        _CALL_BROWSER_SESSION_ID.reset(token)


def current_call_browser_session_override() -> str | None:
    """The browser this call was explicitly targeted at, or None when it named none."""
    return _CALL_BROWSER_SESSION_ID.get()


def effective_browser_session_id(ctx: AgentContext) -> str | None:
    """The browser the current tool call acts in — its target when it named one, else the chat's.

    Anything resolved inside a call's dynamic extent reads this: a helper that goes straight to
    ``ctx.browser_session_id`` describes the chat's page while the call acts on another.
    """
    return _CALL_BROWSER_SESSION_ID.get() or ctx.browser_session_id


def raw_secret_browser_denied(ctx: AgentContext) -> bool:
    return ctx.request_policy is not None and ctx.request_policy.raw_secret_detected


def browser_session_lock(ctx: AgentContext, *, session_id: str | None = None) -> asyncio.Lock:
    lock_key = session_id or effective_browser_session_id(ctx) or "__unbound_browser_session__"
    locks = getattr(ctx, "browser_session_locks_by_session_id", None)
    if not isinstance(locks, dict):
        locks = {}
        ctx.browser_session_locks_by_session_id = locks
    lock = locks.get(lock_key)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        locks[lock_key] = lock
    return lock


class RunSecretsNotRegistered(RuntimeError):
    def __init__(self, workflow_run_id: str) -> None:
        self.workflow_run_id = workflow_run_id
        super().__init__(
            f"Test run {workflow_run_id} used a one-time code or a secret resolved only inside the run in this "
            "browser, and has not handed those values over yet, so this browser cannot be read or driven without "
            "risking them. Nothing was done. The run hands them over when it reaches a final status "
            "(`get_run_results` shows it); this call works after that. If the run has already finished and this "
            "keeps failing, the handoff was lost: this browser stays withheld for the rest of the turn, and other "
            "browsers are not affected."
        )


@asynccontextmanager
async def browser_session_turn(ctx: AgentContext, *, session_id: str | None = None) -> AsyncIterator[None]:
    """Hold a browser for one tool call, first registering any secrets its runs still owed so the
    call's result is scrubbed of them. A run that still owes them after that retry withholds the call."""
    async with browser_session_lock(ctx, session_id=session_id):
        handoffs = getattr(ctx, "awaited_run_secret_handoffs", None)
        if isinstance(handoffs, dict):
            held_session_id = session_id or effective_browser_session_id(ctx) or ""
            for owed, settle in list(handoffs.items()):
                if owed[0] != held_session_id:
                    continue
                await settle()
                if owed in handoffs:
                    raise RunSecretsNotRegistered(owed[1])
        yield


def browser_evidence_commit_lock(ctx: AgentContext) -> asyncio.Lock:
    lock = getattr(ctx, "browser_evidence_commit_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        ctx.browser_evidence_commit_lock = lock
    return lock


async def live_working_page(ctx: AgentContext) -> Page | None:
    """The page the browser's tool calls act on, read without letting the read close a tab."""
    session_id = effective_browser_session_id(ctx)
    if not session_id:
        return None
    try:
        browser_state = await resolve_browser_state_for_context(ctx, session_id=session_id)
        if browser_state is None:
            return None
        return await browser_state.get_working_page(prune_excess_pages=False)
    except Exception:
        return None


async def tab_switch_refusal(ctx: AgentContext, *, tab_id: str | None, index: int | None) -> str | None:
    """Why a switch would not carry into the next call: the browser state honours a pinned tab only
    while it is an http(s) or blank page, so switching to any other tab would silently snap back."""
    session_id = effective_browser_session_id(ctx)
    if not session_id:
        return None
    try:
        browser_state = await resolve_browser_state_for_context(ctx, session_id=session_id)
        if browser_state is None or browser_state.browser_context is None:
            return None
        pages = list(browser_state.browser_context.pages)
        if tab_id is not None:
            target = next((page for page in pages if str(id(page)) == tab_id), None)
        elif index is not None and 0 <= index < len(pages):
            target = pages[index]
        else:
            target = None
        if target is None or target.is_closed() or target in await browser_state.list_valid_pages(0):
            return None
        scheme = urlsplit(target.url).scheme or "unknown"
        return (
            f"Tab {pages.index(target)} is a {scheme}: page; the browser tools act only on http(s) tabs, so "
            "switching to it would not carry into the next call. Pick an http(s) tab from skyvern_tab_list."
        )
    except Exception:
        return None


async def browser_valid_tab_count(ctx: AgentContext) -> int | None:
    """Pages the browser state counts against BROWSER_MAX_PAGES_NUMBER, or None when unreadable."""
    session_id = effective_browser_session_id(ctx)
    if not session_id:
        return None
    try:
        browser_state = await resolve_browser_state_for_context(ctx, session_id=session_id)
        if browser_state is None:
            return None
        return len(await browser_state.list_valid_pages(0))
    except Exception:
        return None


async def resolve_browser_state_for_context(
    ctx: AgentContext,
    *,
    session_id: str | None = None,
) -> BrowserState | None:
    if current_demonstrated_action() is not None:
        return None
    resolved_session_id = session_id if session_id is not None else _CALL_BROWSER_SESSION_ID.get()
    if resolved_session_id is None:
        resolved_session_id = ctx.browser_session_id
    if not resolved_session_id:
        return None
    if ctx.turn_origin == TurnOrigin.code_block_ai_fallback or is_self_heal_session_id(resolved_session_id):
        try:
            _resolved_session_id, injected_state, _ = await _resolve_self_heal_browser_state(ctx)
            if _resolved_session_id != resolved_session_id:
                LOG.info(
                    "Resolved self-heal browser session id differs from requested",
                    requested_session_id=resolved_session_id,
                    resolved_session_id=_resolved_session_id,
                    organization_id=ctx.organization_id,
                )
            track_page_crashes(ctx, _resolved_session_id, injected_state)
            return injected_state
        except HealAdoptionFailed:
            return None
    browser_state = await resolve_persistent_browser_state(
        session_id=resolved_session_id,
        organization_id=ctx.organization_id,
    )
    if browser_state is not None:
        # Only this branch's state lives in the pod's cache; the self-heal branch above returns the
        # healer's injected state, which the turn-exit release would look for and never find.
        record_attached_browser_driver(ctx, resolved_session_id, browser_state)
        track_page_crashes(ctx, resolved_session_id, browser_state)
    return browser_state


def _exception_causes(exc: BaseException) -> list[BaseException]:
    causes: list[BaseException] = []
    cause = exc.__cause__
    while cause is not None and cause not in causes:
        causes.append(cause)
        cause = cause.__cause__
    return causes


def _is_native_target_closed(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightTargetClosedError):
        return True
    return is_any_engine_error(exc) and is_target_closed_message(str(exc))


def is_positive_browser_closed_fact(exc: BrowserTargetClosedError) -> bool:
    """Only a measurement counts as death. The manager reports closed with no cause when a completed
    connect's first roundtrip fails, and a native target-closed cause is the protocol saying the target
    is gone. A failed connect wraps whatever the transport raised as the cause, and the copilot dials a
    relay, so a refused or dropped connection says nothing about the browser behind it."""
    causes = _exception_causes(exc)
    if not causes:
        return True
    return any(_is_native_target_closed(cause) for cause in causes)


def _abandonable_browser_state_resolve(session_id: str, organization_id: str) -> asyncio.Task[BrowserState | None]:
    """A fresh determination per caller, owned by the module rather than by the awaiting task."""
    task = asyncio.ensure_future(
        app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
            session_id=session_id,
            organization_id=organization_id,
        )
    )
    _ABANDONED_BROWSER_STATE_RESOLVES.add(task)

    def _release(finished: asyncio.Task[BrowserState | None]) -> None:
        _ABANDONED_BROWSER_STATE_RESOLVES.discard(finished)
        # Nobody is necessarily still waiting, and an abandoned determination must not surface as
        # "exception was never retrieved".
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(_release)
    return task


async def resolve_persistent_browser_state(
    *,
    session_id: str,
    organization_id: str,
) -> BrowserState | None:
    """The manager bounds this work; Copilot waits for the determination it asked for.

    Cancelling the manager's teardown-and-reattach mid-flight leaves it undone, so the next call
    repeats it from zero and a session whose determination outlasts one caller is never judged. A
    caller cancelled from outside therefore leaves the determination running: the manager
    serializes per session, so the next caller queues behind the work completing rather than
    redoing it.

    Every caller still issues its OWN lookup. A determination that is merely stuck must never be
    inherited by a later attach, which is the oracle and has to be free to answer on its own.
    """
    while True:
        release_in_flight = _DRIVER_RELEASES_IN_FLIGHT.get(session_id)
        if release_in_flight is not None:
            await release_in_flight.wait()
        epoch_before_lookup = _DRIVER_RELEASE_EPOCHS.get(session_id, 0)
        resolve = _abandonable_browser_state_resolve(session_id, organization_id)
        try:
            browser_state = await asyncio.shield(resolve)
        except asyncio.CancelledError:
            _settle_abandoned_browser_state_resolve(resolve, session_id, organization_id, epoch_before_lookup)
            raise
        except Exception as exc:
            positive = isinstance(exc, BrowserTargetClosedError) and is_positive_browser_closed_fact(exc)
            # The manager answers closed once, then drops the handle: a closed report an earlier, abandoned
            # lookup received is the only positive fact this ambiguous failure can carry.
            if not _take_abandoned_closed_fact(session_id, epoch_before_lookup) and not positive:
                raise
            LOG.info(
                "Persistent browser reported closed; treating the session as unreachable",
                session_id=session_id,
                organization_id=organization_id,
                error_type=type(exc).__name__,
                cause_types=[type(cause).__name__ for cause in _exception_causes(exc)],
            )
            browser_state = None
        else:
            _ABANDONED_CLOSED_FACTS.pop(session_id, None)
        # A release that began while this lookup was out may have retired the generation it
        # returned; the caller records synchronously after this returns, so re-issuing is enough.
        if _DRIVER_RELEASE_EPOCHS.get(session_id, 0) == epoch_before_lookup:
            return browser_state


def _take_abandoned_closed_fact(session_id: str, epoch: int) -> bool:
    return _ABANDONED_CLOSED_FACTS.pop(session_id, None) == epoch


def _settle_abandoned_browser_state_resolve(
    resolve: asyncio.Task[BrowserState | None], session_id: str, organization_id: str, epoch: int
) -> None:
    """A caller cancelled mid-resolve never sees what the shielded lookup went on to find: without this
    the driver it attached has no owner to release it, and a positive closed report is lost."""

    def _release(finished: asyncio.Task[BrowserState | None]) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            if isinstance(exc, BrowserTargetClosedError) and is_positive_browser_closed_fact(exc):
                _ABANDONED_CLOSED_FACTS[session_id] = epoch
            return
        browser_state = finished.result()
        if browser_state is None:
            return
        _ABANDONED_CLOSED_FACTS.pop(session_id, None)
        _hold_attached_browser_driver(session_id, browser_state, new_holder=True)
        start_browser_driver_release(organization_id, AttachedBrowserDriver(session_id, browser_state))

    resolve.add_done_callback(_release)


def start_browser_driver_release(
    organization_id: str,
    attached: AttachedBrowserDriver,
    *,
    unwinding_operations: set[asyncio.Task[object]] | None = None,
) -> asyncio.Task[None]:
    """The release runs as its own task so a cancel landing on the caller's await cannot stop it
    before it hands back the attach count; a stuck count skips every later release on that session."""
    release = asyncio.ensure_future(
        release_browser_driver_quietly(organization_id, attached, unwinding_operations=unwinding_operations)
    )
    _DETACHED_TASKS.add(release)
    release.add_done_callback(_discard_finished_detached_task)
    return release


async def wait_for_driver_releases(releases: Iterable[asyncio.Task[None]]) -> None:
    """Bounded here, not in the release: a slow unwind delays the detach, never the turn's exit."""
    pending = [release for release in releases if not release.done()]
    if pending:
        await asyncio.wait(pending, timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS)


_Created = TypeVar("_Created")


async def _admitted_create(organization_id: str, start: Callable[[], Awaitable[_Created]]) -> _Created:
    # Run inside the caller's shielded future, so a cancelled turn keeps its slot until the create itself returns.
    async with app.AGENT_FUNCTION.browser_session_create_admission(organization_id, copilot=True):
        return await start()


def _start_capacity_failure(refusal: TooManyStartingBrowserSessions) -> BuildTestConnectFailure:
    return BuildTestConnectFailure(
        state="browser_start_capacity",
        starting_sessions=refusal.starting_sessions,
        max_starting_sessions=refusal.max_starting_sessions,
        retry_after_seconds=refusal.retry_after_seconds,
    )


def _close_session_once_created(
    organization_id: str,
    create: asyncio.Future[_Created],
    session_id_of: Callable[[_Created], str | None],
) -> None:
    async def _close() -> None:
        try:
            session_id = session_id_of(await create)
        except Exception:
            return
        if session_id is None:
            return
        closed = await close_browser_session_quietly(
            organization_id, session_id, reason=BrowserSessionCloseReason.turn_ended
        )
        log = LOG.info if closed else LOG.warning
        log(
            "Requested close of browser session created after its turn was cancelled",
            session_id=session_id,
            organization_id=organization_id,
            closed=closed,
        )

    closer = asyncio.ensure_future(_close())
    _DETACHED_TASKS.add(closer)
    closer.add_done_callback(_discard_finished_detached_task)


async def close_browser_session_quietly(
    organization_id: str,
    session_id: str,
    *,
    reason: BrowserSessionCloseReason = BrowserSessionCloseReason.aborted,
) -> bool:
    """Bounded: the session-manager backend is often the reason we are closing at all, so an
    unbounded close could hang the request. Returns whether the close landed; a caller that needs
    the browser gone, not merely asked to go, has nothing else to tell it."""
    try:
        await asyncio.wait_for(
            app.PERSISTENT_SESSIONS_MANAGER.close_session(organization_id, session_id, reason=reason),
            timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS,
        )
    except Exception:
        LOG.debug("Failed to close browser session", session_id=session_id, exc_info=True)
        return False
    _clear_closed_session_scrub_values(session_id)
    return True


def _clear_closed_session_scrub_values(session_id: str) -> None:
    """Only a landed close, never a retired id: a retired session can still be alive and reattached."""
    if session_id in _ATTACHED_TURNS_PER_SESSION:
        _SCRUB_VALUES_CLEARED_ON_RELEASE.add(session_id)
    else:
        clear_session_scrub_values(session_id)


def _discard_finished_detached_task(
    task: asyncio.Task[bool] | asyncio.Task[None] | asyncio.Task[PrestartedBrowser | None],
) -> None:
    _DETACHED_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


def _hold_attached_browser_driver(session_id: str, browser_state: BrowserState, *, new_holder: bool) -> None:
    turns, _ = _ATTACHED_TURNS_PER_SESSION.get(session_id, (0, browser_state))
    _ATTACHED_TURNS_PER_SESSION[session_id] = (turns + 1 if new_holder else turns, browser_state)


def track_page_crashes(ctx: AgentContext, session_id: str, browser_state: BrowserState) -> None:
    ctx.page_crash_records_by_browser_session[session_id] = browser_state.page_crash_records


def record_attached_browser_driver(ctx: AgentContext, session_id: str, browser_state: BrowserState) -> None:
    _hold_attached_browser_driver(session_id, browser_state, new_holder=session_id not in ctx.attached_browser_drivers)
    ctx.attached_browser_drivers[session_id] = AttachedBrowserDriver(session_id, browser_state)


async def release_browser_driver_quietly(
    organization_id: str,
    attached: AttachedBrowserDriver,
    *,
    unwinding_operations: set[asyncio.Task[object]] | None = None,
) -> None:
    """Detach the driver this turn attached, leaving the persistent session itself running. The
    evict is shielded because it pops the cache entry before awaiting the detach, so a cancelled
    wait would strand the popped driver."""
    # The manager refuses to retire a generation with an admitted operation, and this turn's own
    # tool task can still be unwinding one; a refusal taken as done would leave the driver cached.
    # Unbounded on purpose: an operation that never ends is still using the driver, and this task
    # runs off the turn, whose exit is bounded by its caller.
    pending = {task for task in unwinding_operations or () if task is not asyncio.current_task() and not task.done()}
    if pending:
        await asyncio.wait(pending)
    turns, latest_browser_state = _ATTACHED_TURNS_PER_SESSION.get(attached.session_id, (1, attached.browser_state))
    still_attached = turns - 1
    if still_attached > 0:
        _ATTACHED_TURNS_PER_SESSION[attached.session_id] = (still_attached, latest_browser_state)
        LOG.info(
            "copilot_browser_driver_released",
            session_id=attached.session_id,
            had_cached_driver=False,
            concurrent_turns_still_attached=still_attached,
        )
        return
    _ATTACHED_TURNS_PER_SESSION.pop(attached.session_id, None)
    if attached.session_id in _SCRUB_VALUES_CLEARED_ON_RELEASE:
        _SCRUB_VALUES_CLEARED_ON_RELEASE.discard(attached.session_id)
        clear_session_scrub_values(attached.session_id)
    manager = app.PERSISTENT_SESSIONS_MANAGER
    if not manager.supports_evict_and_reconnect():
        # This process drives the browser itself; detaching its driver would strand the browser
        # and leave the session uncacheable for the rest of its life.
        return
    evict: asyncio.Task[bool] | None = None
    try:
        evict = asyncio.ensure_future(
            manager.evict_cached_browser_state(
                attached.session_id,
                organization_id,
                expected=latest_browser_state,
                detach_remote_driver=True,
                only_if_unleased=True,
            )
        )
        _DETACHED_TASKS.add(evict)
        evict.add_done_callback(_discard_finished_detached_task)
        # The marker must outlive this wait: a resolve admitted after a timed-out wait but before
        # the evict pops the entry would still be handed the generation being retired.
        _DRIVER_RELEASE_EPOCHS[attached.session_id] = _DRIVER_RELEASE_EPOCHS.get(attached.session_id, 0) + 1
        releasing = asyncio.Event()
        _DRIVER_RELEASES_IN_FLIGHT[attached.session_id] = releasing

        def _release_marker(_task: asyncio.Task[bool]) -> None:
            if _DRIVER_RELEASES_IN_FLIGHT.get(attached.session_id) is releasing:
                del _DRIVER_RELEASES_IN_FLIGHT[attached.session_id]
            releasing.set()

        evict.add_done_callback(_release_marker)
        had_cached_driver = await asyncio.wait_for(asyncio.shield(evict), timeout=_SESSION_CLEANUP_TIMEOUT_SECONDS)
    except (asyncio.CancelledError, Exception) as exc:
        LOG.warning(
            "Failed to release browser driver",
            session_id=attached.session_id,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        # A cancel the evict did not raise itself is this turn being cancelled mid-cleanup, and
        # swallowing it would report a task that finished when the caller asked it to stop.
        if isinstance(exc, asyncio.CancelledError) and (evict is None or not evict.cancelled()):
            raise
        return
    LOG.info(
        "copilot_browser_driver_released",
        session_id=attached.session_id,
        had_cached_driver=had_cached_driver,
    )


@asynccontextmanager
async def browser_session_recovery(ctx: AgentContext) -> AsyncIterator[None]:
    """Serialize every interactive browser-session mutation with source promotion.

    Recovery transactions call lower mutation helpers recursively, so ownership is reentrant for
    the current task while sibling tasks still wait on the one context lock.
    """
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Browser session recovery requires an asyncio task")
    lock = ctx.browser_session_recovery_lock
    if ctx.browser_session_recovery_owner is task:
        ctx.browser_session_recovery_depth += 1
        try:
            yield
        finally:
            ctx.browser_session_recovery_depth -= 1
        return
    async with lock:
        ctx.browser_session_recovery_owner = task
        ctx.browser_session_recovery_depth = 1
        try:
            yield
        finally:
            ctx.browser_session_recovery_depth = 0
            ctx.browser_session_recovery_owner = None


async def retire_browser_session_id(ctx: AgentContext, examined_session_id: str | None) -> None:
    """Retire an unusable id at the browser-session mutation spine.

    Compare-and-swap protects a concurrent replacement; the recovery scope protects source
    promotion from any session mutation while exact validated bytes are being persisted.
    """
    async with browser_session_recovery(ctx):
        if ctx.browser_session_id == examined_session_id:
            ctx.browser_session_id = None


@asynccontextmanager
async def _mcp_browser_context_impl(
    ctx: AgentContext,
    *,
    session_id_override: str | None = None,
    resolved_browser_state: tuple[BrowserState | None] | None = None,
) -> AsyncIterator[None]:
    """Push copilot browser state into the MCP session ContextVar for tool calls.

    ``session_id_override`` carries one call's resolved browser so a tool the model targeted at the
    last run's browser resolves there, without a sibling call reading it off the shared context.
    """
    browser_session_id = session_id_override or ctx.browser_session_id
    # Equality, not identity: a plain-string origin must still route to the fail-closed heal branch.
    if ctx.turn_origin != TurnOrigin.code_block_ai_fallback and not browser_session_id:
        raise RuntimeError("No browser_session_id set on agent context")
    if browser_session_id is None:
        # Self-heal only; always overwritten below before use. Just satisfies the
        # str-tuple typing of sdk_action_workflow_run_cache_key.
        browser_session_id = ""
    sdk_action_workflow_run_cache_key: SdkActionWorkflowRunCacheKey = (ctx.organization_id, browser_session_id)
    # Validate api_key at the boundary, before touching any backend.
    #
    # The copilot FastAPI route runs outside MCPAPIKeyMiddleware, so the CLI
    # falls back to settings.SKYVERN_API_KEY — the server default, not the
    # authenticated caller's key — unless we install set_api_key_override
    # below. Silently skipping the override when ctx.api_key is missing
    # would re-open the exact coarse-grained-auth hole the override exists
    # to close. Fail loudly instead. The copilot route is always behind
    # auth, so this is an assertion, not a runtime branch.
    if not ctx.api_key:
        LOG.warning(
            "mcp_browser_context invoked without api_key",
            session_id=browser_session_id,
            organization_id=ctx.organization_id,
        )
        raise RuntimeError("Copilot agent context missing api_key")

    browser_state: BrowserState | None
    working_page: Page | None = None
    if ctx.turn_origin == TurnOrigin.code_block_ai_fallback:
        browser_session_id, browser_state, working_page = await _resolve_self_heal_browser_state(ctx)
        ctx.browser_session_id = browser_session_id
        sdk_action_workflow_run_cache_key = (ctx.organization_id, browser_session_id)
    else:
        browser_state = (
            resolved_browser_state[0]
            if resolved_browser_state is not None
            else await resolve_browser_state_for_context(ctx, session_id=browser_session_id)
        )
        attachability = (
            BrowserProbeOutcome.positively_unreachable
            if browser_state is None
            else _browser_context_attachability(browser_state.browser_context)
        )
        if browser_state is None or attachability != BrowserProbeOutcome.attachable:
            # Keep the session id out of the raised message -- it can propagate
            # to LLM- or user-visible output -- but log it for operators.
            retiring = attachability == BrowserProbeOutcome.positively_unreachable
            LOG.warning(
                "No browser context for copilot session",
                session_id=browser_session_id,
                organization_id=ctx.organization_id,
                attachability=attachability.value,
                # Whether the next call reuses this session or cold-boots a replacement, losing
                # whatever page state it held. The tool error alone does not say which.
                session_retired=retiring,
            )
            # A completed resolve that found nothing attachable is the positive evidence the probe
            # structurally cannot get, so this is where a dead id gets retired. An unavailable
            # connectivity signal is not that evidence, so the call fails but the id survives.
            if retiring:
                await retire_browser_session_id(ctx, browser_session_id)
                raise CopilotBrowserSessionUnavailable(browser_session_id)
            raise CopilotBrowserLivenessUndetermined()
        working_page = await browser_state.get_working_page(prune_excess_pages=False)

    override_token = set_api_key_override(ctx.api_key)
    try:
        skyvern_client = get_skyvern()
        skyvern_browser = SkyvernBrowser(
            skyvern_client,
            browser_state.browser_context,
            browser_session_id=browser_session_id,
        )
        skyvern_browser.workflow_run_id = ctx.sdk_action_workflow_run_ids_by_browser_session.get(
            sdk_action_workflow_run_cache_key
        )
        mcp_ctx = MCPBrowserContext(
            mode="cloud_session",
            session_id=browser_session_id,
            can_access_localhost=_copilot_session_can_access_localhost(),
        )
        active_key = get_active_api_key()
        state = SessionState(
            browser=skyvern_browser,
            context=mcp_ctx,
            api_key_hash=hash_api_key_for_cache(active_key) if active_key else None,
            organization_id=ctx.organization_id,
            request_failures=ctx.request_failure_logs_by_browser_session.setdefault(
                browser_session_id, RequestFailureLog()
            ),
        )
        track_page_crashes(ctx, browser_session_id, browser_state)
        if working_page is not None:
            # The browser state's pin outlives this per-call SessionState, so every tool call
            # starts on the page the last tab operation selected instead of pages[-1].
            state._active_page = working_page
        register_copilot_session(browser_session_id, state, organization_id=ctx.organization_id)
        if is_self_heal_session_id(browser_session_id):
            LOG.info(
                "registered self-heal browser session",
                session_id=browser_session_id,
                organization_id=ctx.organization_id,
            )
        try:
            async with scoped_session(state):
                yield
        finally:
            with suppress(Exception):
                await _persist_selected_page(browser_state, state, seeded=working_page)
            if skyvern_browser.workflow_run_id:
                ctx.sdk_action_workflow_run_ids_by_browser_session[sdk_action_workflow_run_cache_key] = (
                    skyvern_browser.workflow_run_id
                )
            else:
                ctx.sdk_action_workflow_run_ids_by_browser_session.pop(sdk_action_workflow_run_cache_key, None)
            unregister_copilot_session(
                browser_session_id,
                organization_id=ctx.organization_id,
                expected_state=state,
            )
            if is_self_heal_session_id(browser_session_id):
                LOG.info("unregistered self-heal browser session", session_id=browser_session_id)
    finally:
        reset_api_key_override(override_token)


async def _persist_selected_page(browser_state: BrowserState, state: SessionState, *, seeded: Page | None) -> None:
    """Pin a tab the call selected; a call that kept its page leaves the pin alone, so a popup it
    opened takes focus on the next call exactly as before tab tools existed."""
    selected = state._active_page
    if selected is None or selected is seeded or selected.is_closed():
        return
    await browser_state.set_active_page(selected, prune_excess_pages=False)


def _raise_if_browser_generation_retired(
    operation_task: asyncio.Task[Any],
    retirement: BrowserRetirement,
    session_id: str,
) -> None:
    if not retirement.started.is_set():
        return
    retirement.consume_cancellation(operation_task)
    if operation_task.cancelling():
        raise asyncio.CancelledError
    raise CopilotBrowserGenerationRetired(
        session_id,
        retirement.reason or BrowserRetirementReason.replacement,
    )


@asynccontextmanager
async def mcp_browser_context(ctx: AgentContext, *, session_id_override: str | None = None) -> AsyncIterator[None]:
    """Run one MCP browser dispatch on one admitted persistent-session generation."""
    browser_session_id = session_id_override or ctx.browser_session_id
    operation_task = asyncio.current_task()
    active_context = _ACTIVE_MCP_BROWSER_CONTEXT.get()
    if (
        active_context is not None
        and operation_task is active_context.task
        and ctx is active_context.ctx
        and browser_session_id == active_context.session_id
    ):
        _raise_if_browser_generation_retired(
            active_context.task,
            active_context.retirement,
            active_context.session_id,
        )
        # Browser pre-hooks use an internal MCP read in the same task. The outer scope already
        # owns the exact generation, API-key override, SessionState, and process registration.
        # Re-entering those owners would let the inner exit unregister the real action.
        yield
        _raise_if_browser_generation_retired(
            active_context.task,
            active_context.retirement,
            active_context.session_id,
        )
        return
    if ctx.turn_origin == TurnOrigin.code_block_ai_fallback or not browser_session_id or not ctx.api_key:
        async with _mcp_browser_context_impl(ctx, session_id_override=session_id_override):
            yield
        return

    browser_state = await resolve_browser_state_for_context(ctx, session_id=browser_session_id)
    if browser_state is None:
        async with _mcp_browser_context_impl(
            ctx,
            session_id_override=browser_session_id,
            resolved_browser_state=(None,),
        ):
            yield
        return

    async with app.PERSISTENT_SESSIONS_MANAGER.browser_operation(
        browser_session_id,
        browser_state,
    ) as operation:
        if operation is None:
            raise CopilotBrowserGenerationRetired(
                browser_session_id,
                BrowserRetirementReason.session_ending,
            )
        if isinstance(operation, BrowserOperationRejected):
            raise CopilotBrowserGenerationRetired(browser_session_id, operation.reason)
        if operation_task is None:
            raise RuntimeError("A Copilot browser context requires an asyncio task")
        ctx.admitted_browser_operations.setdefault(browser_session_id, set()).add(operation_task)
        scope_token = _ACTIVE_MCP_BROWSER_CONTEXT.set(
            _ActiveMcpBrowserContext(
                task=operation_task,
                ctx=ctx,
                session_id=browser_session_id,
                retirement=operation.retirement,
            )
        )
        try:
            try:
                async with _mcp_browser_context_impl(
                    ctx,
                    session_id_override=browser_session_id,
                    resolved_browser_state=(operation.browser_state,),
                ):
                    yield
            except asyncio.CancelledError:
                if not operation.retirement_started.is_set():
                    raise
                _raise_if_browser_generation_retired(operation_task, operation.retirement, browser_session_id)
            _raise_if_browser_generation_retired(operation_task, operation.retirement, browser_session_id)
        finally:
            _ACTIVE_MCP_BROWSER_CONTEXT.reset(scope_token)
            ctx.admitted_browser_operations.get(browser_session_id, set()).discard(operation_task)


async def _attach_current_browser_generation(ctx: AgentContext) -> None:
    """Follow one exact-generation replacement while verifying a stable session id."""
    try:
        async with mcp_browser_context(ctx):
            return
    except CopilotBrowserGenerationRetired:
        async with mcp_browser_context(ctx):
            return


async def _drop_browser_session_id_at_its_fixed_deadline(ctx: AgentContext) -> None:
    """Drop a held id whose infrastructure is about to end it, so a replacement is created instead.

    The attach in mcp_browser_context stays the oracle for a session that is already gone. This
    answers what the attach structurally cannot: a session in its final minute attaches fine and
    then dies mid-call, because its provider pinned the deadline when it created the browser and
    no renewal moves it (SKY-15044). Infrastructure that can serve a later deadline answers None
    and nothing here applies to it.
    """
    session_id = ctx.browser_session_id
    if not session_id:
        return
    try:
        remaining_seconds = await app.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline(
            session_id, ctx.organization_id
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # Not an answer about the session, and the attach that follows is one. Keeping the id costs
        # a failed call at worst; dropping it discards a live browser and its page state on a blip.
        LOG.warning(
            "Could not read the browser session's deadline; keeping the held session",
            session_id=session_id,
            organization_id=ctx.organization_id,
            exc_info=True,
        )
        return
    if remaining_seconds is None or remaining_seconds >= _FIXED_DEADLINE_REPLACEMENT_MARGIN_SECONDS:
        return
    LOG.info(
        "Held browser session is at the deadline its infrastructure fixes; replacing it before use",
        session_id=session_id,
        organization_id=ctx.organization_id,
        remaining_seconds=remaining_seconds,
    )
    # Stop using it, do not close it: the studio browser pane streams this same session, and it is
    # going to be torn down on its own deadline within the minute either way.
    await retire_browser_session_id(ctx, session_id)


def _build_test_connect_failure_result(failure: BuildTestConnectFailure) -> dict[str, Any]:
    sentence = build_test_connect_failure_sentence(failure)
    explicit_absence = failure.retry_action is None
    return {
        "ok": False,
        "error": sentence,
        "data": {
            "overall_status": "setup_failed",
            "failure_reason": sentence,
            "browser_session_id": failure.browser_session_id,
            "build_test_connect_failure": failure.model_dump(mode="json", exclude_none=not explicit_absence),
            "blocks": [],
        },
    }


def _browser_session_denial(ctx: AgentContext) -> BuildTestConnectFailure | None:
    # Belt and braces: the tool surface already withholds every browser tool on such a turn.
    if ctx.copilot_config is not None and not ctx.copilot_config.browser_tools_available:
        return BuildTestConnectFailure(
            state="provisioning_unavailable",
            browser_session_id=ctx.browser_session_id,
            diagnostic=BROWSER_TOOLS_UNAVAILABLE_ERROR,
        )
    if raw_secret_browser_denied(ctx):
        return BuildTestConnectFailure(
            state="provisioning_unavailable",
            browser_session_id=ctx.browser_session_id,
            diagnostic=RAW_SECRET_BROWSER_ERROR,
        )
    return None


async def acquire_fresh_exit_browser_session(
    ctx: AgentContext,
    *,
    prior_browser_session_id: str,
    proxy_location: ProxyLocationInput,
    browser_profile_id: str | None,
) -> FreshExitReceipt | dict[str, Any]:
    """The platform's verified-new-exit browser for a build test, or the same typed failure envelope as any
    other build-test browser acquisition."""
    denial = _browser_session_denial(ctx)
    if denial is not None:
        return _browser_session_acquisition_failure_result(denial)
    allocation_id = uuid.uuid4().hex

    async def _start() -> FreshExitReceipt:
        if ctx.browser_allocation_recorder is not None:
            await ctx.browser_allocation_recorder(allocation_id, None)
        return await app.PERSISTENT_SESSIONS_MANAGER.create_fresh_exit_session(
            organization_id=ctx.organization_id,
            prior_browser_session_id=prior_browser_session_id,
            proxy_location=proxy_location,
            browser_profile_id=browser_profile_id,
            session_kind=BrowserSessionKind.copilot,
            created_for_workflow_permanent_id=ctx.workflow_permanent_id,
        )

    try:
        create = asyncio.ensure_future(_admitted_create(ctx.organization_id, _start))
        try:
            receipt = await asyncio.shield(create)
        except asyncio.CancelledError:
            _close_session_once_created(ctx.organization_id, create, lambda receipt: receipt.new_browser_session_id)
            raise
        if receipt.new_browser_session_id:
            await _record_minted_browser(ctx, receipt.new_browser_session_id)
            if ctx.browser_allocation_recorder is not None:
                await ctx.browser_allocation_recorder(allocation_id, receipt.new_browser_session_id)
        return receipt
    except BrowserSessionCreditAdmissionRefusal:
        return _browser_session_acquisition_failure_result(
            BuildTestConnectFailure(state="billing_credit_admission_refusal", retry_action=None)
        )
    except TooManyStartingBrowserSessions as refusal:
        return _browser_session_acquisition_failure_result(_start_capacity_failure(refusal))
    except Exception:
        LOG.warning("Failed to create a fresh-exit browser session for a build test", exc_info=True)
        return _browser_session_acquisition_failure_result(BuildTestConnectFailure(state="provisioning_unavailable"))


def browser_session_acquisition_failure_sentence(failure: BuildTestConnectFailure) -> str:
    if failure.diagnostic in (BROWSER_TOOLS_UNAVAILABLE_ERROR, RAW_SECRET_BROWSER_ERROR):
        return failure.diagnostic
    if failure.state == "billing_credit_admission_refusal":
        return "Browser session did not start because credits are exhausted. Upgrade your plan in Billing."
    if failure.state == "browser_start_capacity":
        return build_test_connect_failure_sentence(failure)
    return "Failed to create browser session"


def _browser_session_acquisition_failure_result(failure: BuildTestConnectFailure) -> dict[str, Any]:
    """Keep the generic acquisition envelope while retaining its typed, actionable cause."""
    sentence = browser_session_acquisition_failure_sentence(failure)
    if failure.state == "billing_credit_admission_refusal":
        return {
            "ok": False,
            "error": sentence,
            "data": {
                "browser_session_acquisition_failure": {
                    "state": failure.state,
                    "retry_action": failure.retry_action,
                }
            },
        }
    if failure.state == "browser_start_capacity":
        return {
            "ok": False,
            "error": sentence,
            "data": {"browser_session_acquisition_failure": failure.model_dump(mode="json", exclude_none=True)},
        }
    return {"ok": False, "error": sentence}


def carries_browser_session_acquisition_failure(result: Mapping[str, object]) -> bool:
    data = result.get("data")
    return isinstance(data, Mapping) and "browser_session_acquisition_failure" in data


@dataclass(frozen=True)
class BuildTestBrowserSeed:
    browser_profile_id: str
    proxy_session_id: str | None


class _SeededCreateSessionKwargs(TypedDict, total=False):
    browser_profile_id: str
    profile_read_only: bool
    generate_browser_profile: bool
    proxy_session_id: str
    proxy_location: ProxyLocationInput
    inherit_profile_proxy: bool
    runnable_type: str


def _seeded_create_session_kwargs(seed: BuildTestBrowserSeed | None) -> _SeededCreateSessionKwargs:
    if seed is None:
        return {}
    # An exporter that cannot read or predates profile_read_only then saves under the session id, never the bp_.
    kwargs: _SeededCreateSessionKwargs = {
        "browser_profile_id": seed.browser_profile_id,
        "profile_read_only": True,
        "generate_browser_profile": True,
    }
    if seed.proxy_session_id:
        kwargs["proxy_session_id"] = seed.proxy_session_id
        kwargs["proxy_location"] = ProxyLocation.RESIDENTIAL_ISP
    else:
        kwargs["inherit_profile_proxy"] = True
    return kwargs


# A turn that hangs outside its model-stream deadline never reaches the turn-end release, so its live process would
# stamp the browser up to the session cap. Measured on the turn's own clock, which a healthy turn cannot pass.
MINTED_BROWSER_RENEWAL_TURN_SECONDS = TOTAL_TIMEOUT_SECONDS + 10 * 60
MINTED_BROWSER_RENEWAL_CHECK_SECONDS = 60.0


async def _record_minted_browser(ctx: AgentContext, session_id: str) -> None:
    if session_id in ctx.minted_browser_sessions:
        return
    # Ledgered first: the turn ends renewal for every ledgered id, and begin takes its refcount before it can raise.
    ctx.minted_browser_sessions[session_id] = None
    _arm_minted_browser_renewal_check(ctx, session_id)
    try:
        await app.BROWSER_MANAGER.begin_session_activity_renewal(session_id)
    except Exception:
        LOG.warning(
            "Failed to start activity renewal for a copilot browser session",
            browser_session_id=session_id,
            organization_id=ctx.organization_id,
            exc_info=True,
        )


def _arm_minted_browser_renewal_check(ctx: AgentContext, session_id: str) -> None:
    ctx.minted_browser_renewal_checks[session_id] = asyncio.get_running_loop().call_later(
        MINTED_BROWSER_RENEWAL_CHECK_SECONDS, _check_minted_browser_renewal, ctx, session_id
    )


def _check_minted_browser_renewal(ctx: AgentContext, session_id: str) -> None:
    started_at = ctx.copilot_run_start_monotonic
    if (
        started_at is not None
        and turn_clock_seconds(ctx, started_at, time.monotonic()) >= MINTED_BROWSER_RENEWAL_TURN_SECONDS
    ):
        end_minted_browser_renewal(ctx, session_id)
    else:
        _arm_minted_browser_renewal_check(ctx, session_id)


def end_minted_browser_renewal(ctx: AgentContext, session_id: str) -> None:
    check = ctx.minted_browser_renewal_checks.pop(session_id, None)
    if check is None:
        return
    check.cancel()
    app.BROWSER_MANAGER.end_session_activity_renewal(session_id)


def end_minted_browser_renewals(ctx: AgentContext) -> None:
    for session_id in list(ctx.minted_browser_renewal_checks):
        end_minted_browser_renewal(ctx, session_id)


async def _provision_browser_session(
    ctx: AgentContext,
    *,
    seed: BuildTestBrowserSeed | None = None,
    proxy_location: ProxyLocationInput = None,
    runnable_type: str | None = None,
) -> BuildTestConnectFailure | None:
    """Create a browser session if the context holds none.

    Returns an immutable acquisition fact on failure and ``None`` on success. Generic callers
    translate that fact back to their established error shape; build-test callers retain it.

    An existing id is not probed here: the attach in mcp_browser_context is the oracle, and a
    session it finds gone is retired and replaced where that is discovered. The one thing the
    attach cannot see is a deadline landing mid-call, which is read from the manager instead.

    Exception: the self-heal path raises HealAdoptionFailed instead of returning an
    failure fact, so a failed adoption aborts the turn rather than degrading to a normal
    tool-level error. Callers must let it propagate.
    """
    denial = _browser_session_denial(ctx)
    if denial is not None:
        return denial

    if ctx.turn_origin == TurnOrigin.code_block_ai_fallback:
        browser_session_id, _, _ = await _resolve_self_heal_browser_state(ctx)
        ctx.browser_session_id = browser_session_id
        return None

    if is_self_heal_session_id(ctx.browser_session_id):
        LOG.warning(
            "Supplied self-heal browser_session_id on interactive path; auto-creating",
            session_id=ctx.browser_session_id,
            organization_id=ctx.organization_id,
        )
        ctx.browser_session_id = None

    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if ctx.browser_session_id:
        return None

    session = None
    installed_session_id: str | None = None
    try:
        allocation_id = uuid.uuid4().hex
        with copilot_span(
            "browser_session_create",
            data={
                "organization_id": ctx.organization_id,
                "prestart": runnable_type == COPILOT_PRESTART_RUNNABLE_TYPE,
            },
        ):
            creation_kwargs = _seeded_create_session_kwargs(seed)
            if proxy_location is not None:
                creation_kwargs.setdefault("proxy_location", proxy_location)
            if runnable_type is not None:
                creation_kwargs["runnable_type"] = runnable_type

            async def _start() -> PersistentBrowserSession:
                if ctx.browser_allocation_recorder is not None:
                    await ctx.browser_allocation_recorder(allocation_id, None)
                # The manager may apply a shorter budget for copilot-kind sessions.
                return await app.PERSISTENT_SESSIONS_MANAGER.create_session(
                    organization_id=ctx.organization_id,
                    timeout_minutes=settings.DEBUG_SESSION_TIMEOUT_MINUTES,
                    created_by="copilot",
                    session_kind=BrowserSessionKind.copilot,
                    created_for_workflow_permanent_id=ctx.workflow_permanent_id,
                    **creation_kwargs,
                )

            create = asyncio.ensure_future(_admitted_create(ctx.organization_id, _start))
            # The manager does not clean up a create cancelled mid-startup, so the browser it still
            # produces is closed here once the id exists.
            try:
                session = await asyncio.shield(create)
            except asyncio.CancelledError:
                _close_session_once_created(
                    ctx.organization_id, create, lambda session: session.persistent_browser_session_id
                )
                raise
        if ctx.browser_allocation_recorder is not None:
            await ctx.browser_allocation_recorder(allocation_id, session.persistent_browser_session_id)
        if ctx.browser_session_id:
            # A sibling call installed a session while this create was in flight. Adopt theirs and
            # close ours: assigning over it would leave a live browser referenced by nobody until
            # its timeout. The boot wait below then runs against the session that survived.
            LOG.info(
                "Closing a duplicate browser session; a concurrent call already installed one",
                session_id=session.persistent_browser_session_id,
                installed_session_id=ctx.browser_session_id,
                organization_id=ctx.organization_id,
            )
            await close_browser_session_quietly(
                ctx.organization_id,
                session.persistent_browser_session_id,
                reason=BrowserSessionCloseReason.user_requested,
            )
            session = None
        else:
            ctx.browser_session_id = session.persistent_browser_session_id
            installed_session_id = ctx.browser_session_id
            # Recorded before the boot wait: a session that never became attachable is still a live
            # browser this turn is the only reader of.
            await _record_minted_browser(ctx, installed_session_id)

        # DefaultPersistentSessionsManager schedules chromium in a background
        # task and returns from create_session before browser_context is set,
        # so the next mcp_browser_context lookup raises. Wait for it.
        async with asyncio.timeout(_BROWSER_BOOT_WAIT_SECONDS):
            while True:
                state = await app.PERSISTENT_SESSIONS_MANAGER.get_browser_state(
                    session_id=ctx.browser_session_id,
                    organization_id=ctx.organization_id,
                )
                if state and _browser_context_is_attachable(state.browser_context):
                    record_attached_browser_driver(ctx, ctx.browser_session_id, state)
                    break
                await asyncio.sleep(_BROWSER_BOOT_POLL_INTERVAL_SECONDS)

        sc = skyvern_context.current()
        if sc:
            sc.run_id = ctx.browser_session_id

        LOG.info(
            "Auto-created browser session for copilot",
            session_id=ctx.browser_session_id,
        )
        return None
    except asyncio.CancelledError:
        if session is not None:
            await close_browser_session_quietly(ctx.organization_id, session.persistent_browser_session_id)
        await retire_browser_session_id(ctx, installed_session_id)
        raise
    except BrowserSessionCreditAdmissionRefusal:
        return BuildTestConnectFailure(
            state="billing_credit_admission_refusal",
            retry_action=None,
        )
    except TooManyStartingBrowserSessions as refusal:
        return _start_capacity_failure(refusal)
    except Exception as e:
        LOG.warning("Failed to auto-create browser session", error=str(e), exc_info=True)
        # Cleanup keys off the local `session`, not ctx.browser_session_id --
        # if the failure happened between create_session returning and the
        # attribute assignment, ctx still reads None but the session is live.
        # Wrap in wait_for because create_session likely failed due to a
        # degraded session-manager backend, and close_session hitting the
        # same backend could hang the whole request if left unbounded.
        if session is not None:
            await close_browser_session_quietly(ctx.organization_id, session.persistent_browser_session_id)
        # Only clear an id this call installed; a sibling's session is not ours to drop.
        await retire_browser_session_id(ctx, installed_session_id)
        # Detail stays in the log above (exc_info=True). The returned string
        # flows back through the tool/agent path and could end up in
        # LLM-visible or user-visible output, so strip raw exception text
        # that may carry internal URLs, paths, or backend identifiers.
        failed_session_id = session.persistent_browser_session_id if session is not None else installed_session_id
        return BuildTestConnectFailure(
            state="provisioning_unavailable",
            browser_session_id=failed_session_id,
        )


async def ensure_browser_session(ctx: AgentContext) -> dict[str, Any] | None:
    async with browser_session_recovery(ctx):
        failure = await _provision_browser_session(ctx)
        return None if failure is None else _browser_session_acquisition_failure_result(failure)


async def ensure_build_test_browser_session(
    ctx: AgentContext, *, seed: BuildTestBrowserSeed | None = None, proxy_location: ProxyLocationInput = None
) -> dict[str, Any] | None:
    async with browser_session_recovery(ctx):
        failure = await _provision_browser_session(ctx, seed=seed, proxy_location=proxy_location)
        return None if failure is None else _build_test_connect_failure_result(failure)


def start_build_test_prestart(ctx: AgentContext, proxy_location: ProxyLocationInput) -> None:
    # ENV=local: the session manager closes every open session in the org before a create, the failed test's included.
    if (
        ctx.build_test_prestart is not None
        or ctx.prestart_teardown_started
        or settings.ENV == "local"
        or ctx.turn_origin == TurnOrigin.code_block_ai_fallback
    ):
        return
    prestart_ctx = replace(ctx)
    prestart_ctx.browser_session_id = None
    task = asyncio.create_task(_prestart_build_test_browser(ctx, prestart_ctx, proxy_location))
    _DETACHED_TASKS.add(task)
    task.add_done_callback(_discard_finished_detached_task)
    ctx.pending_build_test_prestarts[task] = prestart_ctx
    task.add_done_callback(lambda done: ctx.pending_build_test_prestarts.pop(done, None))
    ctx.build_test_prestart = BuildTestPrestart(task=task, proxy_location=proxy_location)


def cancel_ledgered_build_test_prestarts(ctx: AgentContext) -> None:
    # Only once its browser is ledgered: a cancel that lands mid-close then still leaves the turn-end sweep to close it.
    for task, prestart_ctx in list(ctx.pending_build_test_prestarts.items()):
        session_id = prestart_ctx.browser_session_id
        if not task.done() and session_id is not None and session_id in ctx.minted_browser_sessions:
            ctx.cancelled_build_test_prestarts[task] = session_id
            task.cancel()


async def _prestart_build_test_browser(
    ctx: AgentContext, prestart_ctx: AgentContext, proxy_location: ProxyLocationInput
) -> PrestartedBrowser | None:
    chat_context = skyvern_context.current()
    # Its own context, so the provision's run_id write never lands on the chat's.
    with skyvern_context.scoped(
        skyvern_context.SkyvernContext(
            organization_id=ctx.organization_id,
            copilot_session_id=chat_context.copilot_session_id if chat_context else None,
        )
    ):
        try:
            failure = await _provision_browser_session(
                prestart_ctx, proxy_location=proxy_location, runnable_type=COPILOT_PRESTART_RUNNABLE_TYPE
            )
        except Exception:
            LOG.warning(
                "copilot_build_test_browser_prestart_failed",
                prestart_browser_session_id=prestart_ctx.browser_session_id,
                exc_info=True,
            )
            raise
    session_id = prestart_ctx.browser_session_id if failure is None else failure.browser_session_id
    # No await since the provision's last registration, so a teardown that has not started yet will sweep it.
    if ctx.prestart_teardown_started:
        if session_id:
            await _close_build_test_prestart(ctx, session_id, reason=BrowserSessionCloseReason.turn_ended)
        return None
    if failure is not None or session_id is None:
        LOG.info(
            "copilot_build_test_browser_prestart_failed",
            state=failure.state if failure is not None else None,
            prestart_browser_session_id=session_id,
        )
        return None
    LOG.info("copilot_build_test_browser_prestarted", prestart_browser_session_id=session_id)
    return PrestartedBrowser(session_id=session_id, ready_monotonic=time.monotonic())


async def _close_build_test_prestart(ctx: AgentContext, session_id: str, *, reason: BrowserSessionCloseReason) -> None:
    end_minted_browser_renewal(ctx, session_id)
    attached = ctx.attached_browser_drivers.pop(session_id, None)
    if attached is not None:
        await wait_for_driver_releases([start_browser_driver_release(ctx.organization_id, attached)])
    await close_browser_session_quietly(ctx.organization_id, session_id, reason=reason)
    ctx.minted_browser_sessions.pop(session_id, None)


def discard_build_test_prestart(ctx: AgentContext, reason: str) -> None:
    """Closes the pending pre-start in the background; the caller never waits on its start."""
    prestart = ctx.build_test_prestart
    if prestart is None:
        return
    ctx.build_test_prestart = None
    _start_prestart_discard(ctx, prestart.task, reason)


def _start_prestart_discard(ctx: AgentContext, started: asyncio.Task[PrestartedBrowser | None], reason: str) -> None:
    async def _discard() -> None:
        session_id: str | None = None
        try:
            prestarted = await started
        except asyncio.CancelledError:
            if not started.cancelled():
                raise
            prestarted = None
            session_id = ctx.cancelled_build_test_prestarts.pop(started, None)
        except Exception:
            prestarted = None
        if prestarted is not None:
            session_id = prestarted.session_id
            await _close_build_test_prestart(ctx, session_id, reason=BrowserSessionCloseReason.aborted)
        LOG.info("copilot_build_test_browser_prestart_discarded", reason=reason, prestart_browser_session_id=session_id)

    cleanup = asyncio.ensure_future(_discard())
    _DETACHED_TASKS.add(cleanup)
    cleanup.add_done_callback(_discard_finished_detached_task)


async def claim_build_test_prestart(ctx: AgentContext, proxy_location: ProxyLocationInput) -> str | None:
    # Only the swap takes the recovery lock: the start runs on a ctx copy sharing it, so awaiting it there deadlocks.
    prestart = ctx.build_test_prestart
    if prestart is None:
        return None
    if prestart.proxy_location != proxy_location:
        discard_build_test_prestart(ctx, "proxy_change")
        return None
    ctx.build_test_prestart = None
    prestarted: PrestartedBrowser | None = None
    try:
        # Shielded so a cancelled test leaves the start to finish and be closed with the turn's other minted browsers.
        prestarted = await asyncio.shield(prestart.task)
        if prestarted is None:
            LOG.info(
                "copilot_build_test_browser_prestart_discarded", reason="start_failed", prestart_browser_session_id=None
            )
            return None
        try:
            remaining_seconds = await app.PERSISTENT_SESSIONS_MANAGER.seconds_until_fixed_deadline(
                prestarted.session_id, ctx.organization_id
            )
        except Exception:
            LOG.warning(
                "Could not read the pre-started browser's deadline; claiming it anyway",
                prestart_browser_session_id=prestarted.session_id,
                exc_info=True,
            )
            remaining_seconds = None
        if remaining_seconds is not None and remaining_seconds < _FIXED_DEADLINE_REPLACEMENT_MARGIN_SECONDS:
            _start_prestart_discard(ctx, prestart.task, "unavailable")
            return None
        async with browser_session_recovery(ctx):
            claimed = await app.DATABASE.browser_sessions.claim_copilot_prestart(
                session_id=prestarted.session_id, organization_id=ctx.organization_id
            )
    except asyncio.CancelledError:
        current = asyncio.current_task()
        # A start cancelled by turn end surfaces here through the shield; the test itself was not cancelled.
        if prestart.task.cancelled() and current is not None and not current.cancelling():
            LOG.info(
                "copilot_build_test_browser_prestart_discarded", reason="turn_ended", prestart_browser_session_id=None
            )
            return None
        # The pre-start is already off ctx, so without this a later failed test could start a second one.
        _start_prestart_discard(ctx, prestart.task, "cancelled")
        raise
    except Exception:
        LOG.warning(
            "copilot_build_test_browser_prestart_claim_failed",
            prestart_browser_session_id=prestarted.session_id if prestarted is not None else None,
            exc_info=True,
        )
        _start_prestart_discard(ctx, prestart.task, "start_failed" if prestarted is None else "claim_error")
        return None
    if not claimed:
        _start_prestart_discard(ctx, prestart.task, "unavailable")
        return None
    LOG.info(
        "copilot_build_test_browser_prestart_claimed",
        prestart_browser_session_id=prestarted.session_id,
        idle_seconds=round(max(0.0, time.monotonic() - prestarted.ready_monotonic), 1),
    )
    return prestarted.session_id


@dataclass(frozen=True)
class BrowserSessionReplacement:
    old_session_id: str | None
    new_session_id: str
    old_closed: bool
    pane_kept_old: bool


async def _browser_session_is_studio_pane(ctx: AgentContext, session_id: str) -> bool:
    try:
        debug_session = await app.DATABASE.debug.get_debug_session_by_browser_session_id(
            browser_session_id=session_id,
            organization_id=ctx.organization_id,
        )
    except Exception:
        LOG.warning("Could not tell whether the studio pane streams this browser; keeping it", exc_info=True)
        return True
    return debug_session is not None


async def replace_browser_session(ctx: AgentContext) -> BrowserSessionReplacement | BuildTestConnectFailure:
    """Retire the chat's browser and provision a new one, keeping the old one when the new one cannot start."""
    async with (
        browser_session_lock(ctx, session_id=ctx.browser_session_id),
        browser_evidence_commit_lock(ctx),
        browser_session_recovery(ctx),
    ):
        old_session_id = ctx.browser_session_id
        await retire_browser_session_id(ctx, old_session_id)
        failure = await _provision_browser_session(ctx)
        new_session_id = ctx.browser_session_id
        if failure is not None or not new_session_id:
            ctx.browser_session_id = old_session_id
            return failure or BuildTestConnectFailure(state="provisioning_unavailable")
        if old_session_id is None:
            return BrowserSessionReplacement(None, new_session_id, old_closed=False, pane_kept_old=False)
        # The studio pane streams its session and re-sends it next turn, so that session is left running.
        pane_kept_old = await _browser_session_is_studio_pane(ctx, old_session_id)
        old_closed = not pane_kept_old and await close_browser_session_quietly(
            ctx.organization_id,
            old_session_id,
            reason=BrowserSessionCloseReason.user_requested,
        )
        ctx.browser_session_replacements[old_session_id] = new_session_id
        ctx.browser_session_continuity_generation += 1
        ctx.browser_session_continuity_disposition = "reestablished"
        ctx.browser_session_continuity_deadline_expired = False
        ctx.browser_session_continuity_acquisition_failure = None
        LOG.info(
            "Copilot replaced its browser session",
            old_session_id=old_session_id,
            new_session_id=new_session_id,
            old_closed=old_closed,
            pane_kept_old=pane_kept_old,
        )
        return BrowserSessionReplacement(old_session_id, new_session_id, old_closed, pane_kept_old)


async def verify_browser_session_by_attaching(ctx: AgentContext) -> dict[str, Any] | None:
    """For callers that hand the id to an out-of-process run without attaching: one attach here is
    the oracle. A session the manager says is gone is replaced; an attach that could not complete
    returns the facts instead of forwarding an unverified id.

    The attach cannot see a deadline that lands after it, and this path hands the id to a run that
    outlives the check by far more than the tool calls do, so the same pre-emption runs here."""
    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if not ctx.browser_session_id:
        return await ensure_browser_session(ctx)
    examined_session_id = ctx.browser_session_id
    try:
        await _attach_current_browser_generation(ctx)
        return None
    except CopilotBrowserSessionUnavailable:
        await retire_browser_session_id(ctx, examined_session_id)
        return await ensure_browser_session(ctx)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOG.warning(
            "Browser session attach could not complete; liveness undetermined",
            session_id=examined_session_id,
            organization_id=ctx.organization_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": (
                f"The browser session could not be verified: the attach raised {type(exc).__name__}. "
                "An indeterminate attach is not evidence the browser is dead."
            ),
            "probe_error_type": type(exc).__name__,
        }


def _supersede_release_budget_seconds(ctx: AgentContext) -> float:
    """Seconds the release poll may spend before the turn's own deadline needs what is left to
    terminalize. A deadline suspended for a user decision reports no ``when()`` and imposes no clamp."""
    deadline = ctx.model_stream_deadline
    when = deadline.when() if deadline is not None else None
    if when is None:
        return _SUPERSEDE_RELEASE_WAIT_SECONDS
    remaining = when - asyncio.get_running_loop().time() - _SUPERSEDE_TERMINALIZATION_HEADROOM_SECONDS
    return min(_SUPERSEDE_RELEASE_WAIT_SECONDS, remaining)


async def _build_test_session_occupancy_stop(
    ctx: AgentContext,
    *,
    session_id: str,
    copilot_chat_id: str | None,
    copilot_turn_id: str | None,
) -> BuildTestConnectFailure | None:
    """Whether a live lease on the attached session blocks dispatching a build test into it.

    ``None`` means the session was free when read: nothing held it, or this chat's own older test
    run was superseded and its owner dropped the lease. It is a read, not a reservation — the new
    run takes the lease later, in the worker, so a third party can still win that gap and the run
    dies on the shared lease exactly as it does today.
    """
    session = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
        session_id=session_id,
        organization_id=ctx.organization_id,
    )
    if session is None or not session.runnable_id:
        return None
    holder_id = session.runnable_id

    def _stop(reason: str) -> BuildTestConnectFailure:
        LOG.info(
            "copilot_build_test_session_occupied",
            session_id=session_id,
            organization_id=ctx.organization_id,
            occupier_runnable_id=holder_id,
            occupier_runnable_type=session.runnable_type,
            reason=reason,
        )
        return BuildTestConnectFailure(
            state="occupied",
            browser_session_id=session_id,
            occupier_run_id=holder_id if session.runnable_type == "workflow_run" else None,
        )

    if session.runnable_type != "workflow_run":
        return _stop("non_run_owner")
    holder = await app.DATABASE.workflow_runs.get_workflow_run(
        workflow_run_id=holder_id,
        organization_id=ctx.organization_id,
    )
    if holder is None:
        return _stop("owner_run_missing")
    if holder.status.is_final():
        return _stop("owner_run_terminal")
    if not copilot_chat_id or holder.copilot_session_id != copilot_chat_id:
        return _stop("foreign_chat")
    # Descendant runs inherit the chat id from their parent, so a child would otherwise pass every
    # guard: cancelling it leaves the parent running and misreporting a failed child block.
    if holder.parent_workflow_run_id is not None:
        return _stop("owner_run_is_child")
    # A run the user is answering is live work, and ``is_final`` does not cover ``paused``.
    if holder.status == WorkflowRunStatus.paused:
        return _stop("owner_run_paused")
    if ctx.build_test_tool_calls_in_model_response > 1:
        return _stop("sibling_dispatch_this_turn")

    # Cancelling without time left to see the lease drop would destroy the older test and start
    # nothing in its place, so a spent budget reports occupied and leaves the holder running.
    release_budget = _supersede_release_budget_seconds(ctx)
    if release_budget <= 0:
        return _stop("supersede_release_budget_exhausted")
    try:
        canceled = await app.WORKFLOW_SERVICE.mark_workflow_run_as_canceled_if_not_final(
            holder_id,
            failure_reason=SUPERSEDED_BY_NEWER_TEST_REASON,
        )
    except Exception:
        LOG.warning(
            "copilot_build_test_supersede_cancel_failed",
            session_id=session_id,
            organization_id=ctx.organization_id,
            superseded_workflow_run_id=holder_id,
            exc_info=True,
        )
        return _stop("supersede_cancel_failed")
    # A no-op cancel means the holder reached its own terminal state in the gap after the status
    # read, so this turn superseded nothing and must not claim it did.
    if canceled is not None and copilot_chat_id and copilot_turn_id:
        # The reason on the run row is the fast path, but the superseded run's own finalizer can
        # still overwrite it; this record is copilot's and nothing else writes it.
        await app.DATABASE.workflow_params.record_superseded_build_test_run(
            organization_id=ctx.organization_id,
            workflow_copilot_chat_id=copilot_chat_id,
            turn_id=copilot_turn_id,
            workflow_run_id=holder_id,
        )
    LOG.info(
        "copilot_build_test_session_superseded",
        session_id=session_id,
        organization_id=ctx.organization_id,
        superseded_workflow_run_id=holder_id,
    )
    deadline = time.monotonic() + release_budget
    while time.monotonic() < deadline:
        await asyncio.sleep(_SUPERSEDE_RELEASE_POLL_INTERVAL_SECONDS)
        current = await app.PERSISTENT_SESSIONS_MANAGER.get_session(
            session_id=session_id,
            organization_id=ctx.organization_id,
        )
        if current is None:
            return _stop("session_row_gone_after_supersede")
        if not current.runnable_id:
            return None
        if current.runnable_id != holder_id:
            return _stop("relet_after_supersede")
    return _stop("supersede_release_timed_out")


async def verify_build_test_browser_session_by_attaching(
    ctx: AgentContext,
    *,
    copilot_chat_id: str | None = None,
    copilot_turn_id: str | None = None,
) -> dict[str, Any] | None:
    """Attach once for a build test; report supported acquisition state without replacing it."""
    await _drop_browser_session_id_at_its_fixed_deadline(ctx)
    if not ctx.browser_session_id:
        return await ensure_build_test_browser_session(ctx)
    examined_session_id = ctx.browser_session_id
    try:
        await _attach_current_browser_generation(ctx)
    except CopilotBrowserSessionUnavailable:
        await retire_browser_session_id(ctx, examined_session_id)
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="already_closed", browser_session_id=examined_session_id)
        )
    except BrowserTargetClosedError as exc:
        if not is_positive_browser_closed_fact(exc):
            return _build_test_connect_failure_result(
                BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id=examined_session_id)
            )
        await retire_browser_session_id(ctx, examined_session_id)
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="already_closed", browser_session_id=examined_session_id)
        )
    except BrowserCdpAcquisitionError as exc:
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(
                state="cdp_connect_failed",
                browser_session_id=examined_session_id,
                diagnostic=str(exc),
            )
        )
    except BrowserCdpConnectionError:
        # A failed attach does not prove the remote session is dead; keep its id as
        # evidence and let the explicit fresh-session action choose replacement.
        return _build_test_connect_failure_result(
            BuildTestConnectFailure(state="cdp_connect_failed", browser_session_id=examined_session_id)
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOG.warning(
            "Build-test browser attach could not be classified",
            session_id=examined_session_id,
            organization_id=ctx.organization_id,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return {
            "ok": False,
            "error": "The build-test browser attach could not be classified.",
            "probe_error_type": type(exc).__name__,
        }

    occupancy = await _build_test_session_occupancy_stop(
        ctx,
        session_id=examined_session_id,
        copilot_chat_id=copilot_chat_id,
        copilot_turn_id=copilot_turn_id,
    )
    return None if occupancy is None else _build_test_connect_failure_result(occupancy)
