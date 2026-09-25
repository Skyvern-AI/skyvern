from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Literal, cast

from structlog.stdlib import BoundLogger

from skyvern.forge.sdk.core import skyvern_context
from skyvern.forge.sdk.core.skyvern_context import SkyvernContext
from skyvern.webeye.browser_acquisition_sample import (
    BrowserRuntime,
    acquire_first_try_fields,
    acquired_browser_runtime,
    resolve_acquire_mode,
)

AcquireMode = Literal["create", "attach", "reuse"]
ScreenshotFailureOutcome = Literal["timeout", "target_closed", "other_error"]
DisconnectKind = Literal[
    "intentional_teardown", "driver_transport_loss", "browser_disconnected", "context_closed", "connection_unusable"
]
DisconnectEvidence = Literal[
    "teardown_intent",
    "transport_error_future",
    "browser_event",
    "context_event",
    "liveness_state",
    "round_trip_failure",
    "round_trip_timeout",
]
RuntimeEndReason = Literal[
    "normal_close", "deliberate_detach", "driver_release", "context_recreation", "driver_replacement"
]
# Whether the attributed run still held the browser when the observation was made.
RunPhase = Literal["active", "after_release"]
RuntimeEventReason = (
    Literal[
        "ready",
        "acquire_error",
        "cancelled",
        "page_crash",
        "context_closed",
        "browser_disconnected",
        "connection_unusable",
    ]
    | RuntimeEndReason
)


@dataclass(frozen=True)
class BrowserRuntimeLogContext:
    workflow_run_id: str | None = None
    task_id: str | None = None
    browser_session_id: str | None = None
    organization_id: str | None = None
    browser_engine: str | None = None
    # Who operates a browser this process did not launch: the infrastructure provider for a persistent
    # session, the dispatched vendor family for a vendor browser. None for a local browser.
    browser_vendor: str | None = None
    browser_runtime: BrowserRuntime | None = None
    # Delayed callbacks retain the owner's log destination, never its mutable context or cleanup registries.
    _owner_log: list[dict] | None = field(default=None, repr=False, compare=False)

    @classmethod
    def current(cls) -> BrowserRuntimeLogContext:
        bound = _bound_runtime_log_context.get()
        if bound is not None:
            return bound
        context = skyvern_context.current()
        if context is None:
            return cls()
        return cls(
            workflow_run_id=context.workflow_run_id,
            task_id=context.task_id,
            browser_session_id=context.browser_session_id,
            organization_id=context.organization_id,
            _owner_log=context.log,
        )

    @classmethod
    def for_run(
        cls,
        *,
        workflow_run_id: str | None = None,
        task_id: str | None = None,
        browser_session_id: str | None = None,
        organization_id: str | None = None,
    ) -> BrowserRuntimeLogContext:
        current = cls.current()
        bound = _bound_runtime_log_context.get()
        if workflow_run_id is None and task_id is not None and bound is not None and task_id == bound.task_id:
            workflow_run_id = bound.workflow_run_id
        explicit_run = workflow_run_id is not None or task_id is not None
        same_owner = not explicit_run or (
            workflow_run_id == current.workflow_run_id and (workflow_run_id is not None or task_id == current.task_id)
        )
        return cls(
            workflow_run_id=workflow_run_id if explicit_run else current.workflow_run_id,
            task_id=task_id if explicit_run else current.task_id,
            browser_session_id=browser_session_id,
            organization_id=organization_id or current.organization_id,
            _owner_log=current._owner_log if same_owner else None,
        )

    def with_browser_dimensions_of(self, source: BrowserRuntimeLogContext | None) -> BrowserRuntimeLogContext:
        if source is None:
            return self
        return replace(
            self,
            browser_engine=source.browser_engine or self.browser_engine,
            browser_vendor=source.browser_vendor or self.browser_vendor,
            browser_runtime=source.browser_runtime or self.browser_runtime,
        )

    def browser_dimension_fields(self) -> dict[str, str]:
        dimensions = {
            "browser_engine": self.browser_engine,
            "browser_runtime": self.browser_runtime,
            "browser_vendor": self.browser_vendor,
        }
        return {name: value for name, value in dimensions.items() if value is not None}


def with_acquired_browser_runtime(context: BrowserRuntimeLogContext) -> BrowserRuntimeLogContext:
    """Stamp the runtime the open acquisition's creator recorded. Without a record (a pre-dispatch
    failure, or a factory that records none) it is classified as the cloud factory would classify a
    browser this process launches or dials itself."""
    acquired = acquired_browser_runtime()
    if acquired is not None:
        runtime, vendor = acquired
        return replace(context, browser_runtime=runtime, browser_vendor=vendor)
    if context.browser_runtime is not None:
        return context
    return replace(context, browser_runtime="pbs" if context.browser_session_id else "local")


_bound_runtime_log_context: ContextVar[BrowserRuntimeLogContext | None] = ContextVar(
    "browser_runtime_log_context", default=None
)


@contextmanager
def browser_runtime_log_context(context: BrowserRuntimeLogContext) -> Iterator[None]:
    """Carry existing Run identity across PBS calls without changing download or lease context."""
    token = _bound_runtime_log_context.set(context)
    try:
        yield
    finally:
        _bound_runtime_log_context.reset(token)


def log_browser_runtime_event(
    logger: BoundLogger,
    message: str,
    *,
    context: BrowserRuntimeLogContext,
    event: Literal["acquire_result", "page_crash", "runtime_ended"],
    reason: RuntimeEventReason,
    observation_source: Literal["acquisition", "page_event", "browser_event", "liveness_probe", "driver_event"],
    expected: bool,
    outcome: Literal["success", "failure"] | None = None,
    acquire_mode: AcquireMode | None = None,
    run_phase: RunPhase | None = None,
    **diagnostic_fields: object,
) -> None:
    fields: dict[str, object] = {
        **diagnostic_fields,
        "browser_runtime_event": event,
        "workflow_run_id": context.workflow_run_id,
        "task_id": context.task_id,
        "browser_session_id": context.browser_session_id,
        "observation_source": observation_source,
        "reason": reason,
        "expected": expected,
    }
    if outcome is not None:
        fields["outcome"] = outcome
    if acquire_mode is not None:
        # Resolve to the acquisition mode a vendor branch recorded at dispatch time (before its first
        # attempt), so a create that ignores a fallback browser_address is labeled create on BOTH its
        # success and its terminal failure/cancellation, not the pre-dispatch address heuristic. With
        # no such record the pre-dispatch mode stands, so attach and reuse are untouched.
        if event == "acquire_result":
            acquire_mode = cast(AcquireMode, resolve_acquire_mode(acquire_mode))
        fields["acquire_mode"] = acquire_mode
    # First-try success enrichment rides on the canonical create-acquisition event only, so the
    # metric's denominator is session-creation attempts (acquire_mode=create) — attach/reuse are
    # excluded. Empty when no acquisition scope is open, so non-create emitters are unaffected.
    if event == "acquire_result" and acquire_mode == "create":
        fields.update(acquire_first_try_fields(outcome_success=outcome == "success"))
    if run_phase is not None:
        fields["run_phase"] = run_phase
    fields.update(context.browser_dimension_fields())
    # The log processor prefers ambient IDs over kwargs. Scope only this synchronous emission,
    # with fresh cleanup registries, so delayed driver callbacks cannot borrow another run's IDs.
    with skyvern_context.scoped(
        SkyvernContext(
            workflow_run_id=context.workflow_run_id,
            task_id=context.task_id,
            browser_session_id=context.browser_session_id,
            organization_id=context.organization_id,
            log=context._owner_log if context._owner_log is not None else [],
        )
    ):
        log = logger.info if expected else logger.warning
        log(message, **fields)


def log_screenshot_failure(
    logger: BoundLogger,
    *,
    context: BrowserRuntimeLogContext,
    outcome: ScreenshotFailureOutcome,
    timeout_ms: float,
    elapsed_ms: float,
) -> None:
    # Scope only the emission: ambient identity must not overwrite the request snapshot,
    # and cleanup registries must remain owned by the caller's context.
    with skyvern_context.scoped(
        SkyvernContext(
            workflow_run_id=context.workflow_run_id,
            task_id=context.task_id,
            browser_session_id=context.browser_session_id,
            organization_id=context.organization_id,
            log=context._owner_log if context._owner_log is not None else [],
        )
    ):
        logger.warning(
            "Screenshot request failed",
            browser_runtime_event="screenshot_failure",
            workflow_run_id=context.workflow_run_id,
            task_id=context.task_id,
            browser_session_id=context.browser_session_id,
            outcome=outcome,
            screenshot_phase="scrolling_screenshot",
            timeout_ms=timeout_ms,
            elapsed_ms=max(0.0, elapsed_ms),
            **context.browser_dimension_fields(),
        )


@contextmanager
def log_browser_acquisition_failure(
    logger: BoundLogger,
    context: BrowserRuntimeLogContext | Callable[[], BrowserRuntimeLogContext],
    acquire_mode: AcquireMode,
) -> Iterator[None]:
    try:
        yield
    except (Exception, asyncio.CancelledError) as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        log_browser_runtime_event(
            logger,
            "Browser acquisition failed",
            context=context() if callable(context) else context,
            event="acquire_result",
            outcome="failure",
            acquire_mode=acquire_mode,
            reason="cancelled" if cancelled else "acquire_error",
            observation_source="acquisition",
            expected=cancelled,
        )
        raise
